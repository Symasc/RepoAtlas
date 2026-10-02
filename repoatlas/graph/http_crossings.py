"""Requests joined to the handlers another repository serves them with (HTTP linkage).

Two claims meet here, both recorded per file and both source-agnostic
(:mod:`repoatlas.parsers.http_facts`):

* an :data:`~repoatlas.parsers.http_facts.HTTP_EXPOSED_ENDPOINT` claim: a handler,
  the method and route it serves, and the base location its application is published
  at, with the kind of source that stated the base and the lines that prove each part;
* an :data:`~repoatlas.parsers.http_facts.HTTP_CLIENT_CALL` claim: a request, its
  method, its origin and its path shape, with the lines that state them.

A request is joined to a handler only when all three agree: **the service identity**
(the request's origin is the endpoint's, exactly — no alias, no localhost, no ``http``
for ``https``), **the method**, and **the route shape**
(:func:`repoatlas.models.http.endpoint_matches`, under which the published base path
is ordinary literal segments). Exactly one handler must agree. Two — two routes of one
service, or two repositories publishing the same identity — is ambiguous, and nothing
is chosen.

A request matched inside its own repository is joined too: its routes there meet it by
route shape alone, which cannot tell one service's ``/health`` from another's, and the
identity is what says which one it reaches. A join becomes one
:attr:`~repoatlas.models.parsing.EdgeType.CALLS_HTTP_HANDLER` edge, *consumer caller →
provider handler*, provenanced by the request's own file and lines, whose resolution
records the evidence on both sides under ``http``. That record is what lets the edge
join two repositories in a change-impact walk (:mod:`repoatlas.graph.impact`), as a
package link's record lets a ``USES`` edge.

This says that the source on both sides names the same operation. It does not say that
the request is sent at run time, or that it reaches this deployment of the handler.

Everything here is pure: claims in, edges and decisions out. The global pass applies
the edges with its other output, replaces them on every run, and never reads them back
(ARCHITECTURE.md §9).
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, replace

from repoatlas.graph import http_chain as hops
from repoatlas.graph.http_chain import Hop
from repoatlas.models import http
from repoatlas.models.parsing import EdgeType
from repoatlas.parsers.http_facts import (
    BASE_UNKNOWN,
    HTTP_CLIENT_CALL,
    HTTP_EXPOSED_ENDPOINT,
    HTTP_UNRESOLVED_PATTERN,
    HTTP_UNSOURCED_IDENTITY,
    PENDING_FACT,
)
from repoatlas.storage.protocols import (
    EdgeRecord,
    FileRow,
    HttpCallDecisionRow,
    UnresolvedRefRecord,
)

#: The key of the evidence record in a ``CALLS_HTTP_HANDLER`` edge's resolution.
HTTP_CROSSING = "http"

#: Decisions, one per request claim. Stable: tests and diagnostics read them.
LINKED = "linked"
PROVIDER_AMBIGUOUS = "http-provider-ambiguous"
ROUTE_UNMATCHED = "http-route-unmatched"
SERVICE_UNMATCHED = "http-service-unmatched"
#: A request whose path is read but whose host the source does not state (D2): counted
#: and listed, never joined.
UNKNOWN_BASE = "http-base-unknown"
UNREAD = "http-claim-unread"
#: A request whose origin is an identity a deployment resolves, but which reaches no code:
#: the chain stops at the source tie.
SOURCE_UNRESOLVED = "http-deployment-source-unresolved"
#: A request whose origin fits the hosts an Ingress template gives under a deployment no
#: local source establishes: the chain stops at the deployment binding.
BINDING_UNRESOLVED = "http-deployment-binding-unresolved"

_DEFAULT_PORTS = {"http": 80, "https": 443}
#: What a pattern's hole may stand for in a host the request states (already a valid
#: hostname): part of one label, or whole labels with the dots between them.
_IN_LABEL = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-_")
_ACROSS_LABELS = _IN_LABEL | {"."}


@dataclass(frozen=True, slots=True)
class _Exposure:
    claim: UnresolvedRefRecord
    endpoint: str
    method: str
    origin: str
    path: str
    deployment: str
    #: The published base path by segment, empty for a base at the origin's root.
    base: tuple[str, ...]
    meta: dict[str, str]


@dataclass(frozen=True, slots=True)
class _Request:
    claim: UnresolvedRefRecord
    method: str
    origin: str
    segments: tuple[str | None, ...]
    display: str
    meta: dict[str, str]


@dataclass(frozen=True, slots=True)
class _Pattern:
    claim: UnresolvedRefRecord
    scheme: str
    port: int
    #: One token per host character: a literal character, or the characters a hole's
    #: next character may be; :attr:`repeats` marks the tokens a hole may repeat.
    tokens: tuple[str | frozenset[str], ...]
    repeats: frozenset[int]
    scope: str

    def fits(self, origin: str) -> bool:
        scheme, _, authority = origin.partition("://")
        host, colon, port = authority.partition(":")
        if host.startswith("[") or scheme != self.scheme:
            return False
        number = int(port) if colon else _DEFAULT_PORTS.get(scheme)
        return number == self.port and self._matches(host)

    def _matches(self, host: str) -> bool:
        """Whether the host fits, by stepping every position the pattern could be at
        together: time linear in the host times the pattern, whatever the holes."""
        states = self._close({0})
        for char in host:
            moved = {
                index if index in self.repeats else index + 1
                for index in states
                if index < len(self.tokens)
                and (
                    char == self.tokens[index]
                    if isinstance(self.tokens[index], str)
                    else char in self.tokens[index]
                )
            }
            if not moved:
                return False
            states = self._close(moved)
        return len(self.tokens) in states

    def _close(self, states: set[int]) -> set[int]:
        """Add the positions a repeat may be skipped to."""
        found, pending = set(states), list(states)
        while pending:
            index = pending.pop()
            if index in self.repeats and index + 1 not in found:
                found.add(index + 1)
                pending.append(index + 1)
        return found


@dataclass(frozen=True, slots=True)
class HttpCallDecision:
    """The join's decision for one request: one per request claim, and per injected root.

    Literal alternatives of a base are claims of their own; the requests one injected
    transport makes are derived per root and share their claim's id, so the ``variant``
    tells them apart. None overwrites another.
    """

    #: Opaque and deterministic: the same request, written the same way, has the same ref
    #: after any sync. Never a row or claim id.
    ref: str
    claim: UnresolvedRefRecord
    #: ``""``, ``alternative:<base_at>`` or ``root:<injected_at>|<base_in>``.
    variant: str
    decision: str
    #: The hops the decision rests on, flat, as M4 recorded them: every hop a link proves,
    #: or for a refusal the hops proven before (and beside) the one that failed.
    evidence: dict[str, str]
    #: The same, as the ordered chain (:mod:`repoatlas.graph.http_chain`).
    chain: tuple[Hop, ...]
    #: The ``CALLS_HTTP_HANDLER`` edge a link emits, carrying this ref; ``None`` otherwise.
    edge: EdgeRecord | None = None


@dataclass(frozen=True, slots=True)
class HttpCrossingPlan:
    """What the join established, computed but not applied."""

    edges: tuple[EdgeRecord, ...] = ()
    #: Every request's decision, in a stable order. Materialized by the global commit
    #: (``http_call_decisions``); diagnostics and surface, never identity.
    calls: tuple[HttpCallDecision, ...] = ()

    @property
    def detail(self) -> str:
        linked = sum(1 for call in self.calls if call.decision == LINKED)
        ambiguous = sum(1 for call in self.calls if call.decision == PROVIDER_AMBIGUOUS)
        return (
            f"{linked} request(s) joined to a handler in another repository, {ambiguous} ambiguous"
        )


def _meta(claim: UnresolvedRefRecord) -> dict[str, str]:
    if not claim.meta_json:
        return {}
    decoded = json.loads(claim.meta_json)
    return {str(k): str(v) for k, v in decoded.items()} if isinstance(decoded, dict) else {}


def _exposure(claim: UnresolvedRefRecord, meta: dict[str, str]) -> _Exposure | None:
    method, _, url = claim.target_key.partition(" ")
    target = http.client_target(url)
    base = http.client_target(meta.get("deployment", ""))
    if (
        http.canonical_endpoint(claim.target_key) is None
        or method != meta.get("method")
        or target is None
        or target.origin is None
        or base is None
        or base.origin != target.origin
    ):
        return None
    base_path = base.path.rstrip("/")
    return _Exposure(
        claim=claim,
        endpoint=claim.target_key,
        method=method,
        origin=target.origin,
        path=target.path,
        deployment=meta["deployment"],
        base=tuple(base_path[1:].split("/")) if base_path else (),
        meta=meta,
    )


def _segments(meta: dict[str, str]) -> tuple[str | None, ...] | None:
    """A request claim's path shape, or ``None`` when it is not one."""
    try:
        segments = json.loads(meta.get("segments", "null"))
    except json.JSONDecodeError:
        return None
    if (
        not isinstance(segments, list)
        or not segments
        or not all(s is None or isinstance(s, str) for s in segments)
        or http.http_method(meta.get("method", "")) is None
    ):
        return None
    return tuple(segments)


def _request(claim: UnresolvedRefRecord, meta: dict[str, str]) -> _Request | None:
    method = http.http_method(meta.get("method", ""))
    origin = http.client_target(meta.get("origin", "") + "/")
    segments = _segments(meta)
    if (
        method is None
        or origin is None
        or origin.origin is None
        or origin.origin != meta.get("origin")
        or segments is None
    ):
        return None
    display = claim.target_key.partition(" ")[2]
    return _Request(claim, method, origin.origin, segments, display, meta)


def _pattern(claim: UnresolvedRefRecord, meta: dict[str, str]) -> _Pattern | None:
    """An Ingress host pattern: a hole is one character of its kind, then any more."""
    port = meta.get("port", "")
    try:
        parts = json.loads(meta.get("host", "[]"))
    except json.JSONDecodeError:
        return None
    if meta.get("scheme") not in _DEFAULT_PORTS or not port.isdigit() or not parts:
        return None
    tokens: list[str | frozenset[str]] = []
    repeats: set[int] = set()
    for kind, value in parts:
        if kind == "text":
            tokens.extend(str(value))
        elif kind in {"label", "labels"}:
            chars = _IN_LABEL if kind == "label" else _ACROSS_LABELS
            tokens.extend((chars, chars))
            repeats.add(len(tokens) - 1)
        else:
            return None
    return _Pattern(
        claim, meta["scheme"], int(port), tuple(tokens), frozenset(repeats), meta.get("scope", "")
    )


def _covers(exposure: _Exposure, request: _Request) -> bool:
    """Whether the request is addressed to the service the exposure publishes."""
    prefix = request.segments[: len(exposure.base)]
    return exposure.origin == request.origin and tuple(prefix) == exposure.base


def _line(value: str | None) -> int | None:
    return int(value) if value is not None and value.isdigit() else None


def plan_http_crossings(
    claims: Iterable[UnresolvedRefRecord], files: Iterable[FileRow], *, producer: str
) -> HttpCrossingPlan:
    """Decide every request claim: joined to at most one handler, or refused at a hop."""
    by_file = {row.file_id: row for row in files}
    exposures: dict[tuple[str, str], list[_Exposure]] = defaultdict(list)
    served: list[_Exposure] = []
    #: Origin → the identities resolved to it that reach no code.
    unsourced: dict[str, list[UnresolvedRefRecord]] = defaultdict(list)
    patterns: list[_Pattern] = []
    #: Every request claim, with its meta and, when its origin is read, the request.
    asked: list[tuple[UnresolvedRefRecord, dict[str, str], _Request | None]] = []
    for claim in claims:
        meta = _meta(claim)
        role = meta.get(PENDING_FACT)
        if role == HTTP_EXPOSED_ENDPOINT:
            exposure = _exposure(claim, meta)
            if exposure is not None and claim.file_id in by_file:
                exposures[(exposure.method, exposure.origin)].append(exposure)
                served.append(exposure)
        elif role == HTTP_UNSOURCED_IDENTITY:
            target = http.client_target(meta.get("deployment", "") + "/")
            if target is not None and target.origin is not None and claim.file_id in by_file:
                unsourced[target.origin].append(claim)
        elif role == HTTP_UNRESOLVED_PATTERN:
            pattern = _pattern(claim, meta)
            if pattern is not None and claim.file_id in by_file:
                patterns.append(pattern)
        elif role == HTTP_CLIENT_CALL:
            unknown = meta.get("base") == BASE_UNKNOWN and "origin" not in meta
            request = None if unknown or claim.file_id not in by_file else _request(claim, meta)
            asked.append((claim, meta, request))

    refs = _refs([(claim, meta) for claim, meta, _ in asked], by_file)
    edges: list[EdgeRecord] = []
    calls: list[HttpCallDecision] = []
    for (claim, meta, request), (ref, variant) in zip(asked, refs, strict=True):
        consumer = by_file.get(claim.file_id)
        found = _called(claim, meta, by_file)
        start = [hops.caller_hop(claim, consumer)]
        origin = hops.origin_places(claim, meta, consumer)
        edge: EdgeRecord | None = None
        if request is None:
            # The host is not stated (D2), or the claim is not read: the chain stops at origin.
            unread = not (
                meta.get("base") == BASE_UNKNOWN and "origin" not in meta and _segments(meta)
            )
            decision = UNREAD if unread else UNKNOWN_BASE
            reason = "claim_unread" if unread else "host_not_stated"
            chain = [
                *start,
                Hop(hops.ORIGIN, hops.UNRESOLVED, origin, reason=reason),
                *hops.unreached(*hops.HOPS[2:]),
            ]
            calls.append(HttpCallDecision(ref, claim, variant, decision, found, tuple(chain)))
            continue
        # A literal URL states its origin at the call itself.
        stated = origin or tuple(replace(p, role="stated_at") for p in start[0].provenance)
        start.append(Hop(hops.ORIGIN, hops.PROVEN, stated, detail=request.origin))
        matched = [
            exposure
            for exposure in exposures.get((request.method, request.origin), ())
            if http.endpoint_matches(request.segments, exposure.path)
        ]
        # One handler serving one endpoint is one candidate, however many claims say so.
        distinct = {(e.claim.src_id, e.endpoint): e for e in matched}
        # An identity resolved to this origin that reaches no code: the request may be
        # meant for it, so no handler found elsewhere can be the one it reaches.
        rivals = unsourced.get(request.origin, [])
        # The hosts an Ingress gives under a deployment no local source establishes.
        fitting = [p for p in patterns if p.fits(request.origin)]
        if len(distinct) > 1:
            decision = PROVIDER_AMBIGUOUS
            candidates = list(distinct.values())
            found.update(_identities([e.claim for e in candidates], by_file))
            chain = [*start, *_ambiguous(candidates, [], by_file)]
        elif distinct:
            exposure = next(iter(distinct.values()))
            # A pattern of the chart whose rendering made this exposure is that same
            # service under other values; another chart's pattern is a rival.
            scope = exposure.meta.get("scope", "")
            unbound = [p.claim for p in fitting if p.scope != scope]
            if rivals or unbound:
                decision = PROVIDER_AMBIGUOUS
                found.update(_identities([exposure.claim, *rivals, *unbound], by_file))
                chain = [*start, *_ambiguous([exposure], [*rivals, *unbound], by_file)]
            else:
                # Inside its own repository too: an identity the source proves says which
                # handler the request reaches, as route shape alone cannot.
                decision = LINKED
                edge = _edge(request, exposure, by_file, producer, ref)
                edges.append(edge)
                record = json.loads(edge.resolution or "{}").get(HTTP_CROSSING, {})
                found.update({str(k): str(v) for k, v in record.items()})
                route = _route(exposure, by_file)
                chain = [
                    *start,
                    *hops.proven_identity(_evidence(exposure.claim, by_file)),
                    Hop(hops.ROUTE, hops.PROVEN, route, detail=exposure.endpoint),
                    Hop(hops.HANDLER, hops.PROVEN, route, entity_id=exposure.claim.src_id),
                ]
        elif rivals:
            decision = SOURCE_UNRESOLVED
            found.update(_identities(rivals, by_file))
            evidence = hops.merged([_evidence(claim, by_file) for claim in rivals])
            chain = [
                *start,
                Hop(hops.SERVICE_IDENTITY, hops.PROVEN, evidence.identity, detail=evidence.detail),
                Hop(hops.DEPLOYMENT_BINDING, hops.PROVEN, evidence.binding or evidence.identity),
                Hop(
                    hops.SOURCE_BINDING,
                    hops.UNRESOLVED,
                    evidence.source,
                    reason="source_unresolved",
                    detail=_joined(rivals, "source_outcome"),
                ),
                *hops.unreached(hops.ROUTE, hops.HANDLER),
            ]
        elif fitting:
            decision = BINDING_UNRESOLVED
            claims_fitting = [p.claim for p in fitting]
            found.update(_identities(claims_fitting, by_file))
            evidence = hops.merged([_evidence(claim, by_file) for claim in claims_fitting])
            tied = all(_meta(claim).get("source_outcome") == "tied" for claim in claims_fitting)
            unresolved = _joined(claims_fitting, "unresolved")
            outcome = _joined(claims_fitting, "binding_outcome")
            chain = [
                *start,
                Hop(hops.SERVICE_IDENTITY, hops.PROVEN, evidence.identity, detail=evidence.detail),
                Hop(
                    hops.DEPLOYMENT_BINDING,
                    hops.UNRESOLVED,
                    evidence.binding,
                    reason="binding_unresolved",
                    detail="; ".join(part for part in (unresolved, outcome) if part),
                ),
                Hop(hops.SOURCE_BINDING, hops.PROVEN, evidence.source)
                if tied and evidence.source
                else Hop(
                    hops.SOURCE_BINDING,
                    hops.UNRESOLVED,
                    reason="source_unresolved",
                    detail=_joined(claims_fitting, "source_outcome"),
                ),
                *hops.unreached(hops.ROUTE, hops.HANDLER),
            ]
        elif covering := [e for e in served if _covers(e, request)]:
            decision = ROUTE_UNMATCHED
            found.update(_identities([e.claim for e in covering], by_file))
            evidence = hops.merged([_evidence(e.claim, by_file) for e in covering])
            chain = [
                *start,
                *hops.proven_identity(evidence),
                Hop(hops.ROUTE, hops.UNRESOLVED, reason="route_unmatched"),
                *hops.unreached(hops.HANDLER),
            ]
        else:
            decision = SERVICE_UNMATCHED
            chain = [
                *start,
                Hop(hops.SERVICE_IDENTITY, hops.UNRESOLVED, reason="no_identity"),
                *hops.unreached(*hops.HOPS[3:]),
            ]
        calls.append(HttpCallDecision(ref, claim, variant, decision, found, tuple(chain), edge))
    edges.sort(key=lambda e: (e.file_id or 0, e.line_start or 0, e.src_id, e.dst_id))
    calls.sort(key=lambda call: call.ref)
    return HttpCrossingPlan(edges=tuple(edges), calls=tuple(calls))


def decision_rows(plan: HttpCrossingPlan) -> list[HttpCallDecisionRow]:
    """The plan's decisions as the global commit stores them (``http_call_decisions``)."""
    rows: list[HttpCallDecisionRow] = []
    for call in plan.calls:
        claim = call.claim
        method, _, url = claim.target_key.partition(" ")
        rows.append(
            HttpCallDecisionRow(
                decision_ref=call.ref,
                repo_id=claim.repo_id,
                file_id=claim.file_id,
                src_id=claim.src_id,
                line_start=claim.line_start,
                line_end=claim.line_end,
                method=method,
                url=url,
                variant=call.variant,
                decision=call.decision,
                evidence_json=json.dumps(call.evidence, sort_keys=True),
                chain_json=hops.chain_json(call.chain),
            )
        )
    return rows


def _variant(meta: dict[str, str]) -> str:
    """What tells a request apart from another its claim makes: an injected root, or the
    literal alternative of its base."""
    if "injected_at" in meta:
        return f"root:{meta['injected_at']}|{meta.get('base_in', '')}"
    if "alternatives" in meta:
        return f"alternative:{meta.get('base_at', '')}"
    return ""


def _refs(
    asked: list[tuple[UnresolvedRefRecord, dict[str, str]]], by_file: dict[int, FileRow]
) -> list[tuple[str, str]]:
    """Each request's ``decision_ref`` and variant, from what the source states of it:
    its repository, file, lines, the request as written, and the variant. Requests alike
    in all of that are told apart by their order — by the rest of what their claims
    record, then as given — so no row ID of any index ever reaches a ref."""
    keys: list[str] = []
    for claim, meta in asked:
        row = by_file.get(claim.file_id)
        place = [row.repo_slug, row.rel_path] if row else ["", ""]
        keys.append(
            json.dumps([*place, claim.line_start, claim.line_end, claim.target_key, _variant(meta)])
        )
    order = sorted(
        range(len(asked)), key=lambda i: (keys[i], json.dumps(asked[i][1], sort_keys=True))
    )
    seen: dict[str, int] = defaultdict(int)
    refs: list[tuple[str, str]] = [("", "")] * len(asked)
    for index in order:
        ordinal = seen[keys[index]]
        seen[keys[index]] += 1
        text = keys[index] if ordinal == 0 else f"{keys[index]}#{ordinal}"
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]
        refs[index] = (f"hcd1-{digest}", _variant(asked[index][1]))
    return refs


def _evidence(claim: UnresolvedRefRecord, by_file: dict[int, FileRow]) -> hops.IdentityEvidence:
    return hops.identity_evidence(claim, _meta(claim), by_file[claim.file_id])


def _route(exposure: _Exposure, by_file: dict[int, FileRow]) -> tuple[hops.Provenance, ...]:
    provider = by_file[exposure.claim.file_id]
    return (
        hops.Provenance(
            exposure.claim.repo_id,
            provider.rel_path,
            exposure.claim.line_start,
            exposure.claim.line_end,
            "served_at",
        ),
    )


def _ambiguous(
    candidates: list[_Exposure], rivals: list[UnresolvedRefRecord], by_file: dict[int, FileRow]
) -> list[Hop]:
    """The hops of a request more than one thing could answer: one identity serving two
    matching routes is ambiguous at the route; anything else at the identity."""
    evidence = [_evidence(e.claim, by_file) for e in candidates]
    identities = {
        (e.claim.repo_id, item.identity) for e, item in zip(candidates, evidence, strict=True)
    }
    if not rivals and len(identities) == 1:
        routes = tuple(dict.fromkeys(p for e in candidates for p in _route(e, by_file)))
        return [
            *hops.proven_identity(evidence[0]),
            Hop(hops.ROUTE, hops.AMBIGUOUS, routes, reason="several_routes"),
            *hops.unreached(hops.HANDLER),
        ]
    together = hops.merged([*evidence, *(_evidence(claim, by_file) for claim in rivals)])
    return [
        Hop(
            hops.SERVICE_IDENTITY,
            hops.AMBIGUOUS,
            together.identity,
            reason="several_identities",
            detail=together.detail,
        ),
        *hops.unreached(*hops.HOPS[3:]),
    ]


def _joined(claims: list[UnresolvedRefRecord], key: str) -> str:
    return "; ".join(dict.fromkeys(v for c in claims if (v := _meta(c).get(key, ""))))


def _edge(
    request: _Request, exposure: _Exposure, by_file: dict[int, FileRow], producer: str, ref: str
) -> EdgeRecord:
    consumer = by_file[request.claim.file_id]
    provider = by_file[exposure.claim.file_id]
    record: dict[str, object] = {
        "method": request.method,
        "url": request.display,
        "endpoint": exposure.endpoint,
        "deployment": exposure.deployment,
        "provider": provider.repo_slug,
        "consumer": consumer.repo_slug,
        "called_at": f"{consumer.rel_path}:{request.claim.line_start}",
        "served_at": f"{provider.rel_path}:{exposure.claim.line_start}",
        "identity_source": exposure.meta.get("identity_source", ""),
        # The stored decision this link is: one each way, never a refusal.
        "decision_ref": ref,
    }
    # A request on an injected transport: where the options were written, and its base.
    for key in ("injected_at", "base_in"):
        if key in request.meta:
            record[key] = request.meta[key]
    # An identity a deployment declares, in its own file: where, and what binds it to code.
    for key in ("identity_in", "bound_in", "scheme_in", "backend_in"):
        if key in exposure.meta:
            record[key] = exposure.meta[key]
    for key, line in (
        ("base_at", _line(request.meta.get("base_at"))),
        ("identity_at", _line(exposure.meta.get("identity_at"))),
        ("bound_at", _line(exposure.meta.get("bound_at"))),
    ):
        if line is not None:
            path = consumer.rel_path if key == "base_at" else provider.rel_path
            record[key] = f"{path}:{line}"
    return EdgeRecord(
        src_id=request.claim.src_id,
        dst_id=exposure.claim.src_id,
        type=EdgeType.CALLS_HTTP_HANDLER,
        repo_id=request.claim.repo_id,
        file_id=request.claim.file_id,
        line_start=request.claim.line_start,
        line_end=request.claim.line_end,
        parser=producer,
        resolution=json.dumps({HTTP_CROSSING: record}, sort_keys=True),
    )


def _called(
    claim: UnresolvedRefRecord, meta: dict[str, str], by_file: dict[int, FileRow]
) -> dict[str, str]:
    """The request's own hops: where it is made, and where its base is stated."""
    consumer = by_file.get(claim.file_id)
    if consumer is None:
        return {}
    found = {"called_at": f"{consumer.rel_path}:{claim.line_start}"}
    base_at = _line(meta.get("base_at"))
    if base_at is not None:
        found["base_at"] = f"{consumer.rel_path}:{base_at}"
    for key in ("injected_at", "base_in", "alternatives"):
        if key in meta:
            found[key] = meta[key]
    return found


def _identities(claims: list[UnresolvedRefRecord], by_file: dict[int, FileRow]) -> dict[str, str]:
    """The identities and patterns a decision rests on, each by its deployment or pattern
    and the lines that declare and bind it, with what is unresolved; several are joined
    with ``; `` in a stable order."""
    rows: dict[str, list[str]] = defaultdict(list)
    for claim in sorted(claims, key=lambda c: (c.repo_id, c.file_id, c.line_start, c.target_key)):
        meta = _meta(claim)
        provider = by_file.get(claim.file_id)
        values = {
            key: meta.get(key, "")
            for key in (
                "deployment",
                "pattern",
                "identity_source",
                "identity_in",
                "scheme_in",
                "backend_in",
                "unresolved",
                "bound_in",
                "binding_outcome",
                "source_outcome",
            )
        }
        values["provider"] = provider.repo_slug if provider is not None else ""
        for key, value in values.items():
            if value and value not in rows[key]:
                rows[key].append(value)
    return {key: "; ".join(values) for key, values in rows.items()}
