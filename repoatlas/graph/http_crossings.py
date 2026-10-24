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

A refusal may say what its link **would reach** (``would_reach``, DF3c), and only when its own
evidence already establishes where the source is: a binding refusal whose chart is tied to its
repository's code (the chain proves the source binding), from the routes that code serves with
the request's method and path shape, as an exposure of a bound release would carry them; or a
request one proven service answers with several routes. Each is a handler's natural key, the
route and the repository, at most :data:`MAX_WOULD_REACH`, the rest counted. It is evidence on
the refusal, never an edge: which requests link, and every chain, is decided exactly as before.

A request whose base the source does not state (D2) stays :data:`UNKNOWN_BASE` unless an
operator's **scoped declaration** covers it (:mod:`repoatlas.graph.http_scopes`): its own words
-- a caller, a file, optionally a symbol, the hash of the base text -- must fit the request, and
exactly one handler of the declared service must serve it. The join is then an edge whose
identity, binding and source hops are ``declared`` at the declaration; every other outcome keeps
the source's chain and says, in the evidence, which declarations concerned the request and why
none joined it.

Everything here is pure: claims in, edges and decisions out. The global pass applies
the edges with its other output, replaces them on every run, and never reads them back
(ARCHITECTURE.md §9).
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from repoatlas.graph import http_chain as hops
from repoatlas.graph.http_chain import Hop
from repoatlas.graph.http_scopes import (
    COVERED,
    INJECTED_ROOT_UNSELECTED,
    OUT_OF_SCOPE,
    SCOPE_TOO_BROAD,
    SCOPE_UNIDENTIFIED,
    FileScope,
    ScopeDeclaration,
    ScopeRequest,
    base_text,
    covers,
)
from repoatlas.graph.link_exposures import LINK_SOURCE
from repoatlas.models import http
from repoatlas.models.parsing import EdgeType
from repoatlas.parsers.http_facts import (
    BASE_UNKNOWN,
    HTTP_CLIENT_CALL,
    HTTP_EXPOSED_ENDPOINT,
    HTTP_UNRESOLVED_PATTERN,
    HTTP_UNSOURCED_IDENTITY,
    PENDING_FACT,
    SERVED_ROUTES,
)
from repoatlas.storage.protocols import (
    EdgeRecord,
    FileRow,
    HttpCallDecisionRow,
    UnresolvedRefRecord,
)

#: The key of the evidence record in a ``CALLS_HTTP_HANDLER`` edge's resolution.
HTTP_CROSSING = "http"
#: A refusal's hypothetical handlers (DF3c): ``[{handler, route, repository}]``, and how many
#: more there were past :data:`MAX_WOULD_REACH`.
WOULD_REACH = "would_reach"
WOULD_REACH_OMITTED = "would_reach_omitted"
MAX_WOULD_REACH = 5

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

#: The decisions an operator's declaration may change. In each the source leaves the request
#: unlinked without having identified the service the origin names: no identity at all, an
#: identity that reaches no code, a host pattern no deployment binds. In every other refusal the
#: source has already said something about the origin (a published service, rival handlers), and a
#: declaration never overrides that.
DECLARABLE = frozenset({SERVICE_UNMATCHED, SOURCE_UNRESOLVED, BINDING_UNRESOLVED})
#: What an operator's declaration did to a request it concerns, in the refusal's evidence
#: (``declaration_outcome``) so an ineffective one says why. ``applied`` is a link, never a refusal.
DECLARATION_OUTCOMES = frozenset(
    {
        "ambiguous",
        "route-unmatched",
        "callers-excluded",
        "source-established",
        # A scoped declaration (Phase A2) that concerns a request it does not cover.
        SCOPE_TOO_BROAD,
        SCOPE_UNIDENTIFIED,
        INJECTED_ROOT_UNSELECTED,
    }
)
#: Why a scoped declaration that concerns a request does not cover it, most telling first: the
#: one a refusal's ``declaration_outcome`` names when several declarations concern the request.
_SCOPE_REFUSALS = (INJECTED_ROOT_UNSELECTED, SCOPE_TOO_BROAD, SCOPE_UNIDENTIFIED)

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
    #: or for a refusal the hops proven before (and beside) the one that failed; on a refusal
    #: whose source is established, the handlers its link would reach (DF3c).
    evidence: dict[str, object]
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


@dataclass(frozen=True, slots=True)
class ScopedDeclarations:
    """What a pass hands the join for the operators' scoped declarations (Phase A2).

    ``exposures`` are the claims of :func:`repoatlas.graph.link_exposures.plan_scoped_exposures`,
    one per route of each declaration's service; they never enter the ordinary fallback.
    ``files`` gives, for each file a declaration names, what the file offers its scope, by the
    caller's slug and the file's path."""

    exposures: Sequence[UnresolvedRefRecord] = ()
    files: Mapping[tuple[str, str], FileScope] = field(default_factory=dict)


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
    claims: Iterable[UnresolvedRefRecord],
    files: Iterable[FileRow],
    *,
    producer: str,
    handler_key: Callable[[int], str | None] | None = None,
    scopes: ScopedDeclarations | None = None,
) -> HttpCrossingPlan:
    """Decide every request claim: joined to at most one handler, or refused at a hop.

    ``handler_key`` names a handler declaration by its natural key, for a refusal's
    ``would_reach``; without it no refusal records one.

    ``scopes`` carries the operators' scoped declarations. They are consulted only for a request
    whose base the source does not state, and join it only when a declaration's own words cover
    it (:func:`repoatlas.graph.http_scopes.covers`) and exactly one handler of its service serves
    it; without any, no request is looked at for them.
    """
    by_file = {row.file_id: row for row in files}
    exposures: dict[tuple[str, str], list[_Exposure]] = defaultdict(list)
    #: What users' deployment links state (repoatlas.graph.link_exposures): consulted only
    #: for a request the source leaves unlinked, never weighed against it.
    linked_by_user: dict[tuple[str, str], list[_Exposure]] = defaultdict(list)
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
                if meta.get("identity_source") == LINK_SOURCE:
                    linked_by_user[(exposure.method, exposure.origin)].append(exposure)
                    continue
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
    #: The scoped declarations by the caller's slug and file: empty (so never looked at) unless
    #: operators declared some whose service and caller are both indexed.
    scoped = _declarations(scopes, by_file) if scopes is not None and scopes.exposures else {}
    edges: list[EdgeRecord] = []
    calls: list[HttpCallDecision] = []
    for (claim, meta, request), (ref, variant) in zip(asked, refs, strict=True):
        consumer = by_file.get(claim.file_id)
        found: dict[str, object] = dict(_called(claim, meta, by_file))
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
            if decision == UNKNOWN_BASE and scoped and scopes is not None:
                held = _scoped_outcome(
                    claim, meta, consumer, scoped, scopes.files, start, ref, by_file, producer
                )
                if held is not None and held.edge is not None:
                    decision, edge, chain = LINKED, held.edge, list(held.chain)
                    record = json.loads(edge.resolution or "{}").get(HTTP_CROSSING, {})
                    found.update({str(k): str(v) for k, v in record.items()})
                    edges.append(edge)
                elif held is not None:
                    # A declaration concerns this request and does not join it: the chain stays
                    # the source's, and the evidence says which declarations and why.
                    found["declaration_ids"] = ",".join(str(i) for i in held.ids)
                    found["declaration_outcome"] = held.outcome
            calls.append(HttpCallDecision(ref, claim, variant, decision, found, tuple(chain), edge))
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
            if handler_key is not None and _one_proven_service(chain):
                # Several routes of one service whose source the chain proves (DF3c).
                _reach(found, _exposed_handlers(candidates, by_file, handler_key))
        elif distinct:
            exposure = next(iter(distinct.values()))
            # A pattern of the chart whose rendering made this exposure is that same
            # service under other values; another chart's pattern is a rival. So is an
            # established release's own unrendered host, whatever its chart, except the
            # very release variant that made this exposure (DF2_PHASE_A.md §15, E8).
            own = {exposure.meta.get("scope", ""), exposure.meta.get("release_scope", "")}
            own.discard("")
            unbound = [p.claim for p in fitting if p.scope not in own]
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
            # A CI-established deployment's artifact refusal names its own reason (E6).
            reasons = {_meta(claim).get("source_reason", "source_unresolved") for claim in rivals}
            reason = reasons.pop() if len(reasons) == 1 else "source_unresolved"
            chain = [
                *start,
                Hop(hops.SERVICE_IDENTITY, hops.PROVEN, evidence.identity, detail=evidence.detail),
                Hop(
                    hops.DEPLOYMENT_BINDING,
                    evidence.binding_status,
                    evidence.binding or evidence.identity,
                ),
                Hop(
                    hops.SOURCE_BINDING,
                    hops.AMBIGUOUS if reason == "artifact_ambiguous" else hops.UNRESOLVED,
                    evidence.source,
                    reason=reason,
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
            if handler_key is not None and tied and evidence.source:
                # The chart is tied to its repository's code: the routes that code serves (DF3c).
                _reach(found, _served_handlers(request, claims_fitting, by_file, handler_key))
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
        declared = (
            linked_by_user.get((request.method, request.origin), ()) if linked_by_user else ()
        )
        if decision != LINKED and declared:
            stated = None
            if decision in DECLARABLE:
                stated = _user_linked(request, linked_by_user, start, by_file, producer, ref)
            if stated is not None:
                decision, edge, chain = LINKED, stated[0], stated[1]
                found = dict(_called(claim, meta, by_file))
                record = json.loads(edge.resolution or "{}").get(HTTP_CROSSING, {})
                found.update({str(k): str(v) for k, v in record.items()})
                edges.append(edge)
            else:
                # The declaration did not join this request, and the refusal says why beside
                # the source's own reason: it never replaces it.
                why = (
                    declared_candidates(request.segments, declared, _caller_of(request, by_file))[1]
                    if decision in DECLARABLE
                    else "source-established"
                )
                found["declaration_ids"] = ",".join(
                    sorted({e.meta.get("link_id", "") for e in declared} - {""}, key=int)
                )
                found["declaration_outcome"] = why or "route-unmatched"
        calls.append(HttpCallDecision(ref, claim, variant, decision, found, tuple(chain), edge))
    edges.sort(key=lambda e: (e.file_id or 0, e.line_start or 0, e.src_id, e.dst_id))
    calls.sort(key=lambda call: call.ref)
    return HttpCrossingPlan(edges=tuple(edges), calls=tuple(calls))


def _caller_of(request: _Request, by_file: Mapping[int, FileRow]) -> str | None:
    consumer = by_file.get(request.claim.file_id)
    return consumer.repo_slug if consumer is not None else None


@dataclass(frozen=True, slots=True)
class _Declaration:
    """One scoped declaration, read back from the exposures its service gives."""

    link_id: int
    words: ScopeDeclaration
    #: The slug of the repository the declaration says serves the base.
    service: str
    base_path: str
    exposures: tuple[_Exposure, ...]

    @property
    def symbol(self) -> str | None:
        return self.words.symbol


@dataclass(frozen=True, slots=True)
class _Scoped:
    """What the scoped declarations did to one base-unknown request."""

    #: The declarations that concern it, by link id.
    ids: tuple[int, ...]
    #: Why none joined it, a ``declaration_outcome``; empty when one did.
    outcome: str = ""
    edge: EdgeRecord | None = None
    chain: tuple[Hop, ...] = ()


def _scoped_exposure(claim: UnresolvedRefRecord, meta: dict[str, str]) -> _Exposure | None:
    """A scoped exposure claim as the join reads it: the route as the service serves it, which is
    the declared base path (the prefix the service mounts its routes under) before the route the
    code states. The request's own segments are matched against that whole path as written
    (:func:`_scoped_outcome`); the prefix is never added to or removed from them. ``endpoint``,
    ``origin`` and ``deployment`` agree with the claim's target: the shown base, then the prefix
    and the route. The ``origin`` is the shown base, a label no request's origin is ever compared
    with."""
    method = http.http_method(meta.get("method", ""))
    route, shown = meta.get("route", ""), meta.get("base_shown", "")
    path = f"{meta.get('base_path', '')}{route}"
    if method is None or not shown or http.canonical_route(f"{method} {path}") is None:
        return None
    return _Exposure(
        claim=claim,
        endpoint=claim.target_key,
        method=method,
        origin=shown,
        path=path,
        deployment=f"{shown}{meta.get('base_path', '')}",
        # Only a published base (:func:`_covers`) reads this; a scoped exposure is not covered.
        base=(),
        meta=meta,
    )


def _declarations(
    scopes: ScopedDeclarations, by_file: Mapping[int, FileRow]
) -> dict[tuple[str, str], list[_Declaration]]:
    """The scoped declarations by the caller's slug and file, each with its service's exposures.
    A declaration whose service gives no route is not here: it could join nothing."""
    grouped: dict[int, list[_Exposure]] = defaultdict(list)
    for claim in scopes.exposures:
        meta = _meta(claim)
        exposure = _scoped_exposure(claim, meta)
        if exposure is not None and claim.file_id in by_file and meta.get("link_id", "").isdigit():
            grouped[int(meta["link_id"])].append(exposure)
    found: dict[tuple[str, str], list[_Declaration]] = defaultdict(list)
    for link_id in sorted(grouped):
        exposures = grouped[link_id]
        meta = exposures[0].meta
        try:
            callers = json.loads(meta.get("link_callers", "[]"))
        except json.JSONDecodeError:
            continue
        path, key = meta.get("scope_path", ""), meta.get("scope_base_key", "")
        if not (isinstance(callers, list) and len(callers) == 1) or not path or not key:
            continue
        words = ScopeDeclaration(str(callers[0]), path, key, meta.get("scope_symbol") or None)
        service = by_file[exposures[0].claim.file_id].repo_slug
        declaration = _Declaration(
            link_id, words, service, meta.get("base_path", ""), tuple(exposures)
        )
        found[(words.caller, words.path)].append(declaration)
    return found


def _qualified_of(scope: FileScope, entity_id: int) -> str:
    """The qualified name of the entity that makes a request, ``""`` for the file itself."""
    for name, ids in scope.entities.items():
        if entity_id in ids:
            return name
    return ""


def _scoped_outcome(
    claim: UnresolvedRefRecord,
    meta: dict[str, str],
    consumer: FileRow | None,
    declarations: Mapping[tuple[str, str], list[_Declaration]],
    files: Mapping[tuple[str, str], FileScope],
    start: Sequence[Hop],
    ref: str,
    by_file: dict[int, FileRow],
    producer: str,
) -> _Scoped | None:
    """What the scoped declarations of the request's file do to a request whose base the source
    does not state, or ``None`` when none concerns it.

    A declaration joins it only when it covers it. Covering declarations naming more than one
    service (or base path) are ambiguous and join nothing; otherwise the most specific one -- a
    symbol before a whole file, the longer symbol, the lower id -- offers its service's routes,
    and exactly one handler must serve the request, as for any declaration.

    A declaration's base path is a service-side route prefix: each route it offers is the prefix
    before the route the code serves (:func:`_scoped_exposure`). The request's segments are
    matched as written, never with the prefix added or removed, so a prefix matches only whole
    segments (``/v1`` is not ``/v10``) and a request that writes it twice does not join a route
    the service serves once under it."""
    segments = _segments(meta)
    here = declarations.get((consumer.repo_slug, consumer.rel_path)) if consumer else None
    method = http.http_method(meta.get("method", ""))
    if consumer is None or not here or segments is None or method is None:
        return None
    url = claim.target_key.partition(" ")[2]
    scope = files.get((consumer.repo_slug, consumer.rel_path))
    scope = scope if scope is not None else FileScope()
    request = ScopeRequest(
        consumer.repo_slug,
        consumer.rel_path,
        url,
        _qualified_of(scope, claim.src_id),
        claim.src_id,
        meta,
    )
    verdicts = [(item, covers(item.words, request, scope)) for item in here]
    covering = [item for item, verdict in verdicts if verdict == COVERED]
    if not covering:
        seen = {verdict for _, verdict in verdicts if verdict != OUT_OF_SCOPE}
        for outcome in _SCOPE_REFUSALS:
            if outcome in seen:
                named = sorted(item.link_id for item, verdict in verdicts if verdict == outcome)
                return _Scoped(tuple(named), outcome)
        return None
    ids = tuple(sorted(item.link_id for item in covering))
    if len({(item.service, item.base_path) for item in covering}) > 1:
        return _Scoped(ids, "ambiguous")
    by = min(
        covering, key=lambda item: (item.symbol is None, -len(item.symbol or ""), item.link_id)
    )
    # Only the routes that answer the request's method, as an ordinary declaration's are keyed.
    offered = [exposure for exposure in by.exposures if exposure.method == method]
    candidates, why = declared_candidates(segments, offered, consumer.repo_slug)
    if why or len(candidates) != 1:
        return _Scoped(ids, why or "route-unmatched")
    exposure = candidates[0]
    # The URL as the edge shows it: the declaration's shown base and the path as written, never
    # the base expression itself.
    path = url[len(base_text(url) or "") + 2 :]
    shown = _Request(claim, method, exposure.origin, segments, f"{exposure.origin}{path}", meta)
    edge = _edge(shown, exposure, by_file, producer, ref)
    where = by.words.path + (f"#{by.symbol}" if by.symbol else "")
    loaded = json.loads(edge.resolution or "{}")
    record = loaded.get(HTTP_CROSSING, {})
    for key in ("link_id", "link_label", "link_environment", "link_verification"):
        if key in exposure.meta:
            record[key] = exposure.meta[key]
    record["link_scope"] = where
    loaded[HTTP_CROSSING] = record
    edge = edge.model_copy(update={"resolution": json.dumps(loaded, sort_keys=True)})
    place = (
        hops.Provenance(
            exposure.claim.repo_id, exposure.meta.get("link_in", ""), None, None, "link_in", "link"
        ),
    )
    label, shown_base = exposure.meta.get("link_label", ""), exposure.meta.get("base_shown", "")
    stated = f"{label}: {shown_base} in {where}"
    route = _route(exposure, by_file)
    chain = (
        *start,
        Hop(hops.ORIGIN, hops.DECLARED, place, detail=stated),
        Hop(hops.SERVICE_IDENTITY, hops.DECLARED, place),
        Hop(hops.DEPLOYMENT_BINDING, hops.DECLARED, place),
        Hop(hops.SOURCE_BINDING, hops.DECLARED, place),
        Hop(hops.ROUTE, hops.PROVEN, route, detail=exposure.endpoint),
        Hop(hops.HANDLER, hops.PROVEN, route, entity_id=exposure.claim.src_id),
    )
    return _Scoped(ids, edge=edge, chain=chain)


def parse_exposure(claim: UnresolvedRefRecord) -> _Exposure | None:
    """An ``http-exposed-endpoint`` claim as the join reads it, or ``None``. Public so a
    preview reads a declaration's exposures exactly as the sync will."""
    return _exposure(claim, _meta(claim))


def _callers_allow(meta: Mapping[str, str], caller: str | None) -> bool:
    """Whether a link's exposure may serve a request made by ``caller``: a link names the
    repositories it applies to, or none and so all."""
    try:
        callers = json.loads(meta.get("link_callers", "[]"))
    except json.JSONDecodeError:
        return False
    return not callers or (caller in callers if isinstance(callers, list) else False)


def declared_candidates(
    segments: Sequence[str | None],
    at_origin: Sequence[_Exposure],
    caller: str | None,
) -> tuple[list[_Exposure], str]:
    """The distinct handlers operators' links give a request at this origin, and why there is
    not exactly one: ``callers-excluded`` (every link at the origin names other callers),
    ``route-unmatched`` (no served route of theirs has the request's method and shape) or
    ``ambiguous`` (two handlers). Empty reason: exactly one."""
    allowed = [e for e in at_origin if _callers_allow(e.meta, caller)]
    if at_origin and not allowed:
        return [], "callers-excluded"
    matched = [e for e in allowed if http.endpoint_matches(segments, e.path)]
    distinct = list({(e.claim.src_id, e.endpoint): e for e in matched}.values())
    if not distinct:
        return [], "route-unmatched"
    return distinct, "ambiguous" if len(distinct) > 1 else ""


def _user_linked(
    request: _Request,
    linked_by_user: Mapping[tuple[str, str], list[_Exposure]],
    start: Sequence[Hop],
    by_file: dict[int, FileRow],
    producer: str,
    ref: str,
) -> tuple[EdgeRecord, list[Hop]] | None:
    """A request the source left unlinked, joined through a user's deployment link: when
    exactly one handler of the linked repository serves it at the link's URL. Its
    identity, binding and source hops are ``declared``, each citing the link, never a
    file; its route and handler are proven by the source as ever."""
    at_origin = linked_by_user.get((request.method, request.origin), ())
    candidates, why = declared_candidates(request.segments, at_origin, _caller_of(request, by_file))
    if why or len(candidates) != 1:
        return None
    exposure = candidates[0]
    edge = _edge(request, exposure, by_file, producer, ref)
    loaded = json.loads(edge.resolution or "{}")
    record = loaded.get(HTTP_CROSSING, {})
    for key in ("link_id", "link_label", "link_environment", "link_verification"):
        if key in exposure.meta:
            record[key] = exposure.meta[key]
    loaded[HTTP_CROSSING] = record
    edge = edge.model_copy(update={"resolution": json.dumps(loaded, sort_keys=True)})
    place = hops.Provenance(
        exposure.claim.repo_id,
        exposure.meta.get("link_in", ""),
        None,
        None,
        "link_in",
        basis="link",
    )
    stated = f"{exposure.meta.get('link_label', '')}: {exposure.deployment}"
    route = _route(exposure, by_file)
    return edge, [
        *start,
        Hop(hops.SERVICE_IDENTITY, hops.DECLARED, (place,), detail=stated),
        Hop(hops.DEPLOYMENT_BINDING, hops.DECLARED, (place,)),
        Hop(hops.SOURCE_BINDING, hops.DECLARED, (place,)),
        Hop(hops.ROUTE, hops.PROVEN, route, detail=exposure.endpoint),
        Hop(hops.HANDLER, hops.PROVEN, route, entity_id=exposure.claim.src_id),
    ]


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


#: What a CI-established deployment's link and refusals carry (DF2_PHASE_A.md §6.3): the
#: repositories, release and variant, the image and its version relation, the bases, and
#: the places of the binding, the workload, the build and the code.
DEPLOYMENT_KEYS = (
    "deployed_by",
    "built_by",
    "release",
    "variant",
    "namespace",
    "image",
    "version_relation",
    "binding_basis",
    "build_basis",
    "source_rule",
    "artifact_ref",
    "binding_in",
    "values_in",
    "workload_in",
    "image_in",
    "published_in",
    "built_in",
    "source_in",
    "declared_in",
)


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
    # A CI-established deployment (DF2): who deploys and builds it, on what basis, and the
    # artifact between them. Only such a link carries these.
    if "binding_basis" in exposure.meta:
        for key in DEPLOYMENT_KEYS:
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


def _reach(found: dict[str, object], handlers: list[dict[str, str]]) -> None:
    """A refusal's would_reach: at most :data:`MAX_WOULD_REACH` by repository and handler, the
    rest counted; nothing when there is none."""
    if not handlers:
        return
    ordered = sorted(handlers, key=lambda h: (h["repository"], h["handler"]))
    found[WOULD_REACH] = ordered[:MAX_WOULD_REACH]
    if len(ordered) > MAX_WOULD_REACH:
        found[WOULD_REACH_OMITTED] = len(ordered) - MAX_WOULD_REACH


def _served_handlers(
    request: _Request,
    patterns: list[UnresolvedRefRecord],
    by_file: dict[int, FileRow],
    handler_key: Callable[[int], str | None],
) -> list[dict[str, str]]:
    """The handlers a tied pattern's code serves the request with: its method, and a path
    shape that matches the route as an exposure of a bound release would carry it (the
    Ingress origin, then the route). A route no declaration serves names no handler."""
    found: dict[str, dict[str, str]] = {}
    for claim in patterns:
        provider = by_file.get(claim.file_id)
        try:
            served = json.loads(_meta(claim).get(SERVED_ROUTES, "[]"))
        except json.JSONDecodeError:
            continue
        if provider is None or not isinstance(served, list):
            continue
        for row in served:
            if not (isinstance(row, list) and len(row) == 3 and isinstance(row[2], int)):
                continue
            method, path, src_id = str(row[0]), str(row[1]), row[2]
            if method != request.method:
                continue
            body = http.endpoint_body(method, f"{request.origin}{path}")
            target = http.client_target(body.partition(" ")[2])
            if (
                http.canonical_endpoint(body) is None
                or target is None
                or not http.endpoint_matches(request.segments, target.path)
            ):
                continue
            key = handler_key(src_id)
            if key is not None:
                route = f"{method} {path}"
                found[key] = {"handler": key, "route": route, "repository": provider.repo_slug}
    return list(found.values())


def _one_proven_service(chain: list[Hop]) -> bool:
    """Whether the request's ambiguity is between routes of one service whose identity and
    source the chain proves (several routes), not between several identities."""
    kinds = {hop.kind: hop for hop in chain}
    source = kinds.get(hops.SOURCE_BINDING)
    route = kinds.get(hops.ROUTE)
    return (
        source is not None
        and source.status in {hops.PROVEN, hops.DECLARED}
        and route is not None
        and route.status == hops.AMBIGUOUS
    )


def _exposed_handlers(
    candidates: list[_Exposure],
    by_file: dict[int, FileRow],
    handler_key: Callable[[int], str | None],
) -> list[dict[str, str]]:
    """The handlers of the exposures one proven service answers a request with."""
    found: dict[str, dict[str, str]] = {}
    for exposure in candidates:
        provider = by_file.get(exposure.claim.file_id)
        key = handler_key(exposure.claim.src_id)
        if provider is not None and key is not None:
            route = f"{exposure.method} {exposure.meta.get('route', exposure.path)}"
            found[key] = {"handler": key, "route": route, "repository": provider.repo_slug}
    return list(found.values())


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
        if "binding_basis" in meta:
            values.update({key: meta[key] for key in DEPLOYMENT_KEYS if key in meta})
            if "source_reason" in meta:
                values["reason"] = meta["source_reason"]
        for key, value in values.items():
            if value and value not in rows[key]:
                rows[key].append(value)
    return {key: "; ".join(values) for key, values in rows.items()}
