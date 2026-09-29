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

The join is made **across repositories only**: a request matched inside its own
repository is already joined to its routes there, by route shape, and a
cross-repository relationship is what this adds. It becomes one
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

import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from repoatlas.models import http
from repoatlas.models.parsing import EdgeType
from repoatlas.parsers.http_facts import HTTP_CLIENT_CALL, HTTP_EXPOSED_ENDPOINT, PENDING_FACT
from repoatlas.storage.protocols import EdgeRecord, FileRow, UnresolvedRefRecord

#: The key of the evidence record in a ``CALLS_HTTP_HANDLER`` edge's resolution.
HTTP_CROSSING = "http"

#: Decisions, one per request claim. Stable: tests and diagnostics read them.
LINKED = "linked"
SAME_REPOSITORY = "same-repository"
PROVIDER_AMBIGUOUS = "http-provider-ambiguous"
ROUTE_UNMATCHED = "http-route-unmatched"
SERVICE_UNMATCHED = "http-service-unmatched"
UNREAD = "http-claim-unread"

#: The roles this join reads; every other pending fact is someone else's.
ROLES = frozenset({HTTP_EXPOSED_ENDPOINT, HTTP_CLIENT_CALL})


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
class HttpCrossingPlan:
    """What the join established, computed but not applied."""

    edges: tuple[EdgeRecord, ...] = ()
    #: Request claim id → its decision. Diagnostics, never identity.
    decisions: dict[int, str] = field(default_factory=dict)

    @property
    def detail(self) -> str:
        linked = sum(1 for decision in self.decisions.values() if decision == LINKED)
        ambiguous = sum(1 for d in self.decisions.values() if d == PROVIDER_AMBIGUOUS)
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


def _request(claim: UnresolvedRefRecord, meta: dict[str, str]) -> _Request | None:
    method = http.http_method(meta.get("method", ""))
    origin = http.client_target(meta.get("origin", "") + "/")
    try:
        segments = json.loads(meta.get("segments", "null"))
    except json.JSONDecodeError:
        return None
    if (
        method is None
        or origin is None
        or origin.origin is None
        or origin.origin != meta.get("origin")
        or not isinstance(segments, list)
        or not segments
        or not all(s is None or isinstance(s, str) for s in segments)
    ):
        return None
    display = claim.target_key.partition(" ")[2]
    return _Request(claim, method, origin.origin, tuple(segments), display, meta)


def _covers(exposure: _Exposure, request: _Request) -> bool:
    """Whether the request is addressed to the service the exposure publishes."""
    prefix = request.segments[: len(exposure.base)]
    return exposure.origin == request.origin and tuple(prefix) == exposure.base


def _line(value: str | None) -> int | None:
    return int(value) if value is not None and value.isdigit() else None


def plan_http_crossings(
    claims: Iterable[UnresolvedRefRecord], files: Iterable[FileRow], *, producer: str
) -> HttpCrossingPlan:
    """Join every request claim to at most one handler in another repository."""
    by_file = {row.file_id: row for row in files}
    exposures: dict[tuple[str, str], list[_Exposure]] = defaultdict(list)
    served: list[_Exposure] = []
    requests: list[_Request] = []
    decisions: dict[int, str] = {}
    for claim in claims:
        meta = _meta(claim)
        role = meta.get(PENDING_FACT)
        if role == HTTP_EXPOSED_ENDPOINT:
            exposure = _exposure(claim, meta)
            if exposure is not None and claim.file_id in by_file:
                exposures[(exposure.method, exposure.origin)].append(exposure)
                served.append(exposure)
        elif role == HTTP_CLIENT_CALL:
            request = _request(claim, meta)
            if request is None or claim.file_id not in by_file:
                if claim.id is not None:
                    decisions[claim.id] = UNREAD
                continue
            requests.append(request)

    edges: list[EdgeRecord] = []
    for request in requests:
        matched = [
            exposure
            for exposure in exposures.get((request.method, request.origin), ())
            if http.endpoint_matches(request.segments, exposure.path)
        ]
        # One handler serving one endpoint is one candidate, however many claims say so.
        distinct = {(e.claim.src_id, e.endpoint): e for e in matched}
        elsewhere = [e for e in distinct.values() if e.claim.repo_id != request.claim.repo_id]
        if len(distinct) > 1:
            decision = PROVIDER_AMBIGUOUS
        elif elsewhere:
            decision = LINKED
            edges.append(_edge(request, elsewhere[0], by_file, producer))
        elif distinct:
            decision = SAME_REPOSITORY
        elif any(_covers(e, request) and e.claim.repo_id != request.claim.repo_id for e in served):
            decision = ROUTE_UNMATCHED
        else:
            decision = SERVICE_UNMATCHED
        if request.claim.id is not None:
            decisions[request.claim.id] = decision
    edges.sort(key=lambda e: (e.file_id or 0, e.line_start or 0, e.src_id, e.dst_id))
    return HttpCrossingPlan(edges=tuple(edges), decisions=decisions)


def _edge(
    request: _Request, exposure: _Exposure, by_file: dict[int, FileRow], producer: str
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
    }
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
