"""The HTTP inventory of one repository: its rows, filters, order and paging.

Pure over its inputs, like :mod:`repoatlas.capabilities.dependencies`: the service
reads one repository's claims, entities and HTTP edges inside one snapshot and
replans them with :func:`~repoatlas.graph.linker.plan_repository_http`; this
module turns that into rows and never touches the index itself.

**Rows come from what the index holds, each once.** A request a file decided on its
own is a per-file edge; a request it could not decide is a claim with the global
pass's decision; a route is a per-file edge or a NestJS declaration proven — or
not — by the same plan. Nothing is read from a previous run's decisions and nothing
is inferred: a row states what one repository's indexed source says, in the terms
of :class:`~repoatlas.models.http_inventory.OperationStatus`, and no status is a
statement about any other repository or about source RepoAtlas does not extract.

**Crossings are read, not replanned.** The workspace join needs every repository, so
a caller row's crossings are the decisions the last global link pass stored
(``http_call_decisions``), placed on the row written at the same lines by the same
entity. A request only the join reads — an absolute or deployment-addressed URL, a
host the source does not state — has no route-matching row; it gets one of its own,
of status ``join_only``, so no stored decision goes unlisted. That row is not a route
match and not an unresolved path, and is counted as neither.
:func:`project` then withholds what another repository proves, as the scope asks.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from repoatlas.capabilities.dependencies import digest
from repoatlas.capabilities.errors import InvalidCursorError, StaleCursorError
from repoatlas.graph import http_chain, http_claims
from repoatlas.graph.http_claims import CallDecision, HttpClaimPlan
from repoatlas.graph.resolution import ResolutionIndex
from repoatlas.models import http
from repoatlas.models.dependency_inventory import AppliedPageLimits
from repoatlas.models.http_inventory import (
    DEFAULT_PAGE_SIZE,
    MAX_COUNTERPARTS,
    MAX_PAGE_SIZE,
    CoverageLimitation,
    CrossingArtifact,
    CrossingHop,
    CrossingStatus,
    DeclaredMatch,
    HopPlace,
    HttpCrossing,
    HttpOperation,
    HttpOperationsRequest,
    OperationCounterpart,
    OperationCoverage,
    OperationRole,
    OperationStatus,
    OperationSummary,
)
from repoatlas.models.parsing import EdgeType, EntityType
from repoatlas.parsers.http_facts import (
    EXACT,
    FASTIFY_ROUTE,
    FASTIFY_TRACED_ROUTE,
    PENDING_FACT,
    PYTHON_CLASS_ROUTE,
    PYTHON_ROUTE,
    TS_HANDLER_OBJECT,
)
from repoatlas.storage.protocols import (
    EntityIdentity,
    FileRow,
    HttpCallDecisionRow,
    HttpEdgeRow,
    UnresolvedRefRecord,
)

#: Claims whose routes the global pass serves, or lists as declared.
_ROUTE_ROLES = frozenset(
    {
        http_claims.NESTJS_ROUTE,
        PYTHON_ROUTE,
        PYTHON_CLASS_ROUTE,
        FASTIFY_ROUTE,
        FASTIFY_TRACED_ROUTE,
        TS_HANDLER_OBJECT,
    }
)

#: Version of the cursor payload. A token from another shape is refused.
CURSOR_VERSION = 1

GLOBAL_PARSER = "linker:global"

_CURSOR_REFUSED = "that HTTP operations cursor does not belong to this request"
_CURSOR_STALE = "the HTTP operation inventory changed since that page was issued"

#: Hops whose places are always the caller's own repository.
_OWN_HOPS = frozenset({http_chain.CALLER, http_chain.ORIGIN})

#: Why an imported-helper call that never reached a decision is unresolved.
_JOIN_FAILURES = {
    "import does not resolve": "import_unresolved",
    "more than one declaration of that name": "declaration_ambiguous",
    "the declaration cannot be read": "declaration_unreadable",
    http_claims.NOT_LITERAL: http_claims.REASON_NOT_LITERAL,
}

#: What a TypeScript or JavaScript tree may hold that is not extracted at all, so
#: it can be said to be possible here but never counted.
_NOT_EXTRACTED = (
    (
        "member_call_client",
        "Requests through a client object's method (`client.request(...)`) are not extracted.",
    ),
    (
        "injected_fetch",
        "Requests through a fetch implementation passed in as a parameter are not extracted.",
    ),
    ("concatenated_path", "Paths built by concatenation (`'/users/' + id`) are not extracted."),
    (
        "imported_requester_named_path",
        "A named path passed to a helper imported from another file is not recorded.",
    ),
    (
        "constant_route_path",
        "A route whose decorator path is a constant rather than a literal is not recorded.",
    ),
)
#: The parser whose trees those constructs belong to, by the name the index
#: records for it. Pinned against the parser itself by the capability's tests.
_TYPESCRIPT_PARSERS = frozenset({"tree-sitter-typescript"})


@dataclass(frozen=True, slots=True)
class Draft:
    """A row before filtering, with what filtering and ordering need beside it."""

    operation: HttpOperation
    qualified_name: str | None
    #: Route bodies a caller reaches that are served here (callers only).
    reaches: tuple[str, ...] = ()

    @property
    def sort_key(self) -> tuple[str, ...]:
        row = self.operation
        return ((row.repository, row.package or "") if row.repository else ()) + (
            row.path or "",
            f"{row.line_start or 0:09d}",
            row.role,
            row.method or "",
            row.route,
            row.entity or "",
            row.detail,
        )


@dataclass(frozen=True, slots=True)
class Inventory:
    """Every row one repository's plan yields, and what coverage needs."""

    drafts: list[Draft]
    #: Parser per indexed file path, for coverage.
    parsers: Mapping[str, str | None] = field(default_factory=dict)
    #: Whether the replanned edges disagree with the stored global graph.
    stale_graph: bool = False
    #: Whether a global link pass has stored the join's decisions in this index.
    crossings_built: bool = False


@dataclass(frozen=True, slots=True)
class Page:
    rows: list[HttpOperation]
    has_more: bool
    identity: str
    inventory: str
    after: tuple[str, ...] | None


# -- building rows ------------------------------------------------------------------------


def build_inventory(
    *,
    plan: HttpClaimPlan,
    claims: Sequence[UnresolvedRefRecord],
    entities: Mapping[int, EntityIdentity],
    files: Sequence[FileRow],
    index: ResolutionIndex,
    edges: Sequence[HttpEdgeRow],
    parsers: Mapping[str, str | None],
    joined: Sequence[HttpCallDecisionRow] = (),
    slugs: Mapping[int, str] | None = None,
    crossings_built: bool = False,
) -> Inventory:
    """Every caller and provider row of one repository, unfiltered.

    ``joined`` are the stored join decisions of this repository's requests; their
    handlers and callers must be in ``entities``, and every repository their chains
    name in ``slugs``.
    """
    by_file = {row.file_id: row for row in files}
    by_path = {row.rel_path: row for row in files}
    by_claim = {claim.id: claim for claim in claims if claim.id is not None}

    def package(path: str | None) -> str | None:
        row = by_path.get(path) if path else None
        found = http_claims.package_of(index, row) if row is not None else ""
        return found or None

    # -- what this repository serves: route body → providers -------------------------
    serving: dict[str, list[OperationCounterpart]] = {}
    for claim_id, bodies in plan.promoted.items():
        claim = by_claim.get(claim_id)
        origin = by_file.get(claim.file_id) if claim else None
        entity = entities.get(claim.src_id) if claim else None
        if claim is None or origin is None or entity is None:
            continue
        for body in bodies:
            serving.setdefault(body, []).append(
                OperationCounterpart(entity=entity.key, path=origin.rel_path, line=claim.line_start)
            )
    local = [edge for edge in edges if edge.parser != GLOBAL_PARSER]
    for edge in local:
        if edge.type is EdgeType.SERVES_HTTP_ROUTE:
            serving.setdefault(edge.target_name, []).append(
                OperationCounterpart(entity=edge.src_key, path=edge.path, line=edge.line_start)
            )

    declared: list[tuple[str, DeclaredMatch]] = []
    for claim_id, body in sorted(plan.declared.items()):
        claim = by_claim.get(claim_id)
        entity = entities.get(claim.src_id) if claim else None
        origin = by_file.get(claim.file_id) if claim else None
        if claim is None or entity is None:
            continue
        declared.append(
            (
                body,
                DeclaredMatch(
                    route=body,
                    entity=entity.key,
                    path=origin.rel_path if origin else None,
                    line=claim.line_start,
                    reason=plan.routes.get(claim_id, ""),
                ),
            )
        )

    drafts: list[Draft] = []

    # -- callers the global pass decided -------------------------------------------------
    for claim in claims:
        if claim.id is None or _role(claim) not in {
            http_claims.HTTP_REQUESTER_CALL,
            http_claims.HTTP_LOCAL_CALL,
        }:
            continue
        origin = by_file.get(claim.file_id)
        entity = entities.get(claim.src_id)
        decisions = plan.calls.get(claim.id, ())
        outcome = plan.requests.get(claim.id)
        if not decisions:
            if outcome is None or outcome in {http_claims.NOT_HTTP, http_claims.PROMOTED}:
                continue
            decisions = (
                CallDecision(
                    status=http_claims.UNRESOLVED,
                    reason=_JOIN_FAILURES.get(outcome, "helper_unresolved"),
                    method=None,
                    route="",
                ),
            )
        for decision in decisions:
            drafts.append(
                _caller(
                    decision=decision,
                    path=origin.rel_path if origin else None,
                    lines=(claim.line_start, claim.line_end),
                    entity=entity,
                    package=package(origin.rel_path if origin else None),
                    serving=serving,
                    declared=declared,
                )
            )

    # -- callers decided in their own file ------------------------------------------------
    for edge in local:
        if edge.type is not EdgeType.CALLS_HTTP_ROUTE:
            continue
        method, _, path = edge.target_name.partition(" ")
        under = sorted(body for body in serving if body.partition(" ")[2] == path)
        served = edge.target_name in serving
        decision = CallDecision(
            # The same status and the same reason a claim would have been given:
            # which of the two read the call is not a fact about the call.
            status=http_claims.MATCHED if served else http_claims.NO_INDEXED_HANDLER,
            reason=http_claims.REASON_EXACT if served else http_claims.REASON_NO_ROUTE,
            method=method,
            route=path,
            matched=(edge.target_name,) if served else (),
            candidates=tuple(body for body in under if body != edge.target_name),
            composed=edge.line_start is None,
            template=http.RouteTemplate(segments=tuple(path[1:].split("/"))),
            linked=(edge.target_name,),
        )
        drafts.append(
            _caller(
                decision=decision,
                path=edge.path,
                lines=(edge.line_start, edge.line_end),
                entity=EntityIdentity(
                    entity_id=edge.src_id,
                    key=edge.src_key,
                    type=_entity_type(entities, edge.src_id),
                    name=edge.src_name,
                    qualified_name=edge.src_qualified_name,
                ),
                package=package(edge.path),
                serving=serving,
                declared=declared,
            )
        )

    drafts = _crossed(drafts, joined, entities, slugs or {}, package)

    # -- providers ------------------------------------------------------------------------
    reaching: dict[str, list[OperationCounterpart]] = {}
    for draft in drafts:
        row = draft.operation
        for body in draft.reaches:
            if row.entity is not None:
                reaching.setdefault(body, []).append(
                    OperationCounterpart(entity=row.entity, path=row.path, line=row.line_start)
                )

    promoted_lines = {
        (edge.src_id, edge.target.name): edge.line_start
        for edge in plan.http
        if edge.type is EdgeType.SERVES_HTTP_ROUTE
    }
    for claim in claims:
        role = _role(claim)
        if claim.id is None or role not in _ROUTE_ROLES:
            continue
        if role == TS_HANDLER_OBJECT and claim.id not in plan.promoted:
            continue  # an object of functions no oas3-tools application routes to
        if role == FASTIFY_TRACED_ROUTE and claim.id not in plan.promoted:
            continue  # a route on a receiver no proven chain reaches: a diagnostic, no row
        if (
            role in {PYTHON_ROUTE, PYTHON_CLASS_ROUTE}
            and claim.id not in plan.promoted
            and claim.id not in plan.declared
        ):
            # served by its own module, whose edges are listed with the per-file ones -- or,
            # for a class route, on a class nothing proves an application (never declared)
            continue
        origin = by_file.get(claim.file_id)
        entity = entities.get(claim.src_id)
        meta = _meta(claim)
        path = origin.rel_path if origin else None
        bodies = plan.promoted.get(claim.id)
        if bodies:
            for body in bodies:
                composed = promoted_lines.get((claim.src_id, body)) is None
                drafts.append(
                    _provider(
                        body=body,
                        path=path,
                        lines=(claim.line_start, claim.line_end),
                        entity=entity,
                        package=package(path),
                        reaching=reaching,
                        composed=composed,
                    )
                )
            continue
        body = plan.declared.get(claim.id, "")
        method, _, route = body.partition(" ")
        drafts.append(
            Draft(
                operation=HttpOperation(
                    role="provider",
                    status=OperationStatus.DECLARED,
                    method=method or meta.get("method") or None,
                    route=route,
                    path=path,
                    line_start=claim.line_start,
                    line_end=claim.line_end,
                    entity=entity.key if entity else None,
                    package=package(path),
                    detail=plan.routes.get(claim.id, ""),
                    composed=bool(meta.get("controller_path")) or meta.get("provenance") != EXACT,
                ),
                qualified_name=entity.qualified_name if entity else None,
            )
        )
    for edge in local:
        if edge.type is not EdgeType.SERVES_HTTP_ROUTE:
            continue
        drafts.append(
            _provider(
                body=edge.target_name,
                path=edge.path,
                lines=(edge.line_start, edge.line_end),
                entity=EntityIdentity(
                    entity_id=edge.src_id,
                    key=edge.src_key,
                    type=_entity_type(entities, edge.src_id),
                    name=edge.src_name,
                    qualified_name=edge.src_qualified_name,
                ),
                package=package(edge.path),
                reaching=reaching,
                composed=edge.line_start is None,
            )
        )

    stored = {
        (edge.src_id, str(edge.type), edge.target_key, edge.line_start, edge.line_end)
        for edge in edges
        if edge.parser == GLOBAL_PARSER
    }
    planned = {
        (edge.src_id, str(edge.type), edge.target.key, edge.line_start, edge.line_end)
        for edge in plan.http
    }
    return Inventory(
        drafts=drafts,
        parsers=parsers,
        stale_graph=stored != planned,
        crossings_built=crossings_built,
    )


def _crossed(
    drafts: list[Draft],
    decisions: Sequence[HttpCallDecisionRow],
    entities: Mapping[int, EntityIdentity],
    slugs: Mapping[int, str],
    package: Callable[[str | None], str | None],
) -> list[Draft]:
    """The caller rows with their crossings, and a row for each request only the join reads.

    A request's decisions go on the caller row written at its lines by its entity — the
    first in order when two rows are. A request whose route a wrapper composed is a row
    of its file with no lines (its base is stated elsewhere): a decision of that file's
    requests with exactly its method and path goes there, one to each such row in the
    order of the requests' lines. What neither places gets a row of status ``join_only``,
    so each stored decision is listed exactly once.
    """
    grouped: dict[tuple[str | None, int | None, int | None, str | None], list[HttpCrossing]] = {}
    for row in decisions:
        key = (row.path, row.line_start, row.line_end, row.src_key)
        grouped.setdefault(key, []).append(_crossing(row, entities, slugs))
    ordered_drafts = sorted(drafts, key=lambda d: d.sort_key)
    attached: dict[int, list[HttpCrossing]] = {}
    for index, draft in enumerate(ordered_drafts):
        row = draft.operation
        found = grouped.pop((row.path, row.line_start, row.line_end, row.entity), None)
        if found is not None:
            attached.setdefault(index, []).extend(found)
    # Each composed row is one request, and rows alike in all they say are told apart
    # by the order of the requests' lines.
    composed: dict[tuple[str | None, str | None, str], list[int]] = {}
    for index, draft in enumerate(ordered_drafts):
        row = draft.operation
        if row.role == "caller" and row.line_start is None:
            composed.setdefault((row.path, row.method, row.route), []).append(index)
    meeting: dict[tuple[str | None, str | None, str], list[HttpCrossing]] = {}
    alone: dict[tuple[str | None, int | None, int | None, str | None], list[HttpCrossing]] = {}
    for key, crossings in grouped.items():
        for crossing in crossings:
            route = (key[0], crossing.method, _path_of(crossing.url))
            if route in composed:
                meeting.setdefault(route, []).append(crossing)
            else:
                alone.setdefault(key, []).append(crossing)
    for route, crossings in meeting.items():
        rows = composed[route]
        for order, crossing in enumerate(sorted(crossings, key=_called)):
            attached.setdefault(rows[min(order, len(rows) - 1)], []).append(crossing)
    placed: list[Draft] = []
    for index, draft in enumerate(ordered_drafts):
        crossings = attached.get(index)
        if crossings is None:
            placed.append(draft)
            continue
        ordered = tuple(sorted(crossings, key=lambda c: c.decision_ref))
        row = draft.operation.model_copy(update={"crossings": ordered})
        placed.append(replace(draft, operation=row))
    by_key = {found.key: found for found in entities.values()}
    for (path, line_start, line_end, entity), crossings in alone.items():
        ordered = tuple(sorted(crossings, key=lambda c: c.decision_ref))
        first = ordered[0]
        owner = by_key.get(entity) if entity is not None else None
        placed.append(
            Draft(
                operation=HttpOperation(
                    role="caller",
                    status=OperationStatus.JOIN_ONLY,
                    method=first.method or None,
                    route=_path_of(first.url),
                    path=path,
                    line_start=line_start,
                    line_end=line_end,
                    entity=entity,
                    package=package(path),
                    crossings=ordered,
                ),
                qualified_name=owner.qualified_name if owner else None,
            )
        )
    return placed


def _called(crossing: HttpCrossing) -> tuple[int, str]:
    """A crossing's order among a file's requests: its caller's line, then its ref."""
    places = crossing.chain[0].provenance if crossing.chain else ()
    line = places[0].line_start if places and places[0].line_start is not None else 0
    return (line, crossing.decision_ref)


def handler_ids(decisions: Sequence[HttpCallDecisionRow]) -> set[int]:
    """The entities the decisions' chains name, for their keys to be read."""
    found: set[int] = set()
    for row in decisions:
        found.update(
            hop.entity_id
            for hop in http_chain.chain_from_json(row.chain_json)
            if hop.entity_id is not None
        )
    return found


def _crossing(
    row: HttpCallDecisionRow, entities: Mapping[int, EntityIdentity], slugs: Mapping[int, str]
) -> HttpCrossing:
    """One stored decision, in full: every place with its repository."""
    chain: list[CrossingHop] = []
    handler: str | None = None
    for hop in http_chain.chain_from_json(row.chain_json):
        key = None
        if hop.entity_id is not None and (found := entities.get(hop.entity_id)) is not None:
            key = found.key
        if hop.kind == http_chain.HANDLER and hop.status == http_chain.PROVEN:
            handler = key
        chain.append(
            CrossingHop(
                kind=hop.kind,
                status=hop.status,
                provenance=tuple(
                    HopPlace(
                        repository=slugs.get(place.repo_id, f"repository:{place.repo_id}"),
                        path=place.path,
                        line_start=place.line_start,
                        line_end=place.line_end,
                        role=place.role,
                        basis=place.basis or None,
                    )
                    for place in hop.provenance
                ),
                reason=hop.reason,
                note=http_chain.REASONS.get(hop.reason, ""),
                detail=hop.detail,
                handler=key,
            )
        )
    evidence = json.loads(row.evidence_json or "{}")
    linked = row.decision == CrossingStatus.LINKED
    endpoint = str(evidence.get("endpoint", "")) if linked else ""
    artifact = (
        CrossingArtifact.model_validate(
            {
                key: str(evidence[key])
                for key in _ARTIFACT_KEYS
                if isinstance(evidence.get(key), str) and evidence[key]
            }
        )
        if "binding_basis" in evidence
        else None
    )
    return HttpCrossing(
        decision_ref=row.decision_ref,
        status=CrossingStatus(row.decision),
        method=row.method,
        url=row.url,
        variant=row.variant,
        chain=tuple(chain),
        handler=handler,
        provider=str(evidence.get("provider", "")) or None if linked else None,
        endpoint=http.endpoint_key(endpoint) if endpoint else None,
        artifact=artifact,
    )


#: The stored evidence a crossing's artifact shows, by the join's own key (DF2).
_ARTIFACT_KEYS = (
    "deployed_by",
    "built_by",
    "release",
    "variant",
    "image",
    "version_relation",
    "binding_basis",
    "build_basis",
    "source_rule",
)


def _path_of(url: str) -> str:
    """The path of a request as the join read it: after its origin, or after the
    expression that stands for an origin the source does not state. No query."""
    rest = url
    if rest.startswith("{"):
        depth = 0
        for index, char in enumerate(rest):
            depth += {"{": 1, "}": -1}.get(char, 0)
            if depth == 0:
                rest = rest[index + 1 :]
                break
    elif "://" in rest:
        rest = rest.partition("://")[2]
        slash = rest.find("/")
        rest = rest[slash:] if slash >= 0 else "/"
    path = rest.partition("?")[0].partition("#")[0]
    return path if path.startswith("/") else ""


@dataclass(slots=True)
class Span:
    """Lines to supply as evidence, and what they state."""

    #: The operations a row written here makes or serves, for a route named in prose.
    routes: set[str] = field(default_factory=set)
    #: A row's own lines, rather than only a place a crossing's hop cites.
    written: bool = False
    #: Every basis other than source a hop place here rests on (DF2).
    bases: set[str] = field(default_factory=set)


def evidence_spans(
    rows: Sequence[HttpOperation], home: str | None, everywhere: bool
) -> dict[str, dict[tuple[str, int, int], Span]]:
    """The spans a page supplies as evidence, by repository.

    Each row's own lines, and the lines its crossings' established hops cite -- proven, or
    declared by an operator declaration (DF2), each place keeping its basis: in the row's
    own repository, or in any under global scope (``everywhere``). A hop not established
    cites nothing to supply, and a place a file proves as a whole is no span.
    """
    found: dict[str, dict[tuple[str, int, int], Span]] = {}
    for row in rows:
        own = row.repository or home
        if own is None:
            continue
        if row.path is not None and row.line_start is not None and row.line_end is not None:
            span = found.setdefault(own, {}).setdefault(
                (row.path, row.line_start, row.line_end), Span()
            )
            span.written = True
            if row.route:
                span.routes.add(f"{row.method} {row.route}" if row.method else row.route)
        for crossing in row.crossings:
            for hop in crossing.chain:
                # A declared hop supplies its places too, each labelled with its basis (DF2).
                if hop.status not in http_chain.ESTABLISHED:
                    continue
                for place in hop.provenance:
                    if place.line_start is None or place.line_end is None:
                        continue
                    if not everywhere and place.repository != own:
                        continue
                    span = found.setdefault(place.repository, {}).setdefault(
                        (place.path, place.line_start, place.line_end), Span()
                    )
                    if place.basis:
                        span.bases.add(place.basis)
    return found


def with_evidence(
    row: HttpOperation, ids: Mapping[tuple[str, str, int, int], str], home: str | None
) -> HttpOperation:
    """The row with the evidence ids recorded for its lines and its hops' places."""
    own = row.repository or home or ""
    update: dict[str, object] = {}
    if row.path is not None and row.line_start is not None and row.line_end is not None:
        found = ids.get((own, row.path, row.line_start, row.line_end))
        if found is not None:
            update["evidence_id"] = found
    if row.crossings:
        crossings: list[HttpCrossing] = []
        for crossing in row.crossings:
            chain = tuple(
                hop.model_copy(
                    update={
                        "provenance": tuple(
                            place.model_copy(update={"evidence_id": found})
                            if place.line_start is not None
                            and place.line_end is not None
                            and (
                                found := ids.get(
                                    (place.repository, place.path, place.line_start, place.line_end)
                                )
                            )
                            else place
                            for place in hop.provenance
                        )
                    }
                )
                for hop in crossing.chain
            )
            crossings.append(crossing.model_copy(update={"chain": chain}))
        update["crossings"] = tuple(crossings)
    return row.model_copy(update=update) if update else row


def repositories_named(rows: Sequence[HttpOperation]) -> set[str]:
    """Every repository the rows' crossings show a place or a provider in."""
    named: set[str] = set()
    for row in rows:
        for crossing in row.crossings:
            if crossing.provider:
                named.add(crossing.provider)
            for hop in crossing.chain:
                named.update(place.repository for place in hop.provenance)
    return named


def project(row: HttpOperation, home: str | None, shown: bool) -> HttpOperation:
    """A row as this scope may show it.

    Under repository scope without ``cross_repository`` (``home`` named, ``shown``
    false), every place another repository proves is withheld, and with it a hop's
    detail unless the hop is the caller's own; a link's handler and provider are
    withheld too. Each hop keeps its kind, status and reason, and says when something
    was withheld; a link keeps its shared endpoint node, the way out.
    """
    if home is None or shown or not row.crossings:
        return row
    crossings: list[HttpCrossing] = []
    for crossing in row.crossings:
        chain: list[CrossingHop] = []
        for hop in crossing.chain:
            own = tuple(place for place in hop.provenance if place.repository == home)
            foreign = len(own) != len(hop.provenance)
            keeps = hop.kind in _OWN_HOPS or (bool(own) and not foreign)
            chain.append(
                hop.model_copy(
                    update={
                        "provenance": own,
                        "detail": hop.detail if keeps else "",
                        "handler": hop.handler if not foreign else None,
                        "withheld": foreign or (bool(hop.detail) and not keeps),
                    }
                )
            )
        away = crossing.provider is not None and crossing.provider != home
        crossings.append(
            crossing.model_copy(
                update={
                    "chain": tuple(chain),
                    "handler": None if away else crossing.handler,
                    "provider": None if away else crossing.provider,
                    "artifact": _projected(crossing.artifact, home),
                }
            )
        )
    return row.model_copy(update={"crossings": tuple(crossings)})


def _projected(artifact: CrossingArtifact | None, home: str) -> CrossingArtifact | None:
    """A crossing's artifact under repository scope: what a repository other than ``home``
    states -- its name, the release, variant and image it configures -- is withheld; the
    bases and the version relation, labels rather than source, stay (DF2, E7)."""
    if artifact is None:
        return None
    foreign = {artifact.deployed_by, artifact.built_by} - {home, None}
    if not foreign:
        return artifact
    deployment_away = artifact.deployed_by not in (home, None)
    return artifact.model_copy(
        update={
            "deployed_by": None if deployment_away else artifact.deployed_by,
            "built_by": None if artifact.built_by not in (home, None) else artifact.built_by,
            "release": None if deployment_away else artifact.release,
            "variant": None if deployment_away else artifact.variant,
            "image": None,
            "withheld": True,
        }
    )


def _caller(
    *,
    decision: CallDecision,
    path: str | None,
    lines: tuple[int | None, int | None],
    entity: EntityIdentity | None,
    package: str | None,
    serving: Mapping[str, list[OperationCounterpart]],
    declared: Sequence[tuple[str, DeclaredMatch]],
) -> Draft:
    handlers = [provider for body in decision.matched for provider in serving.get(body, [])]
    matches: list[DeclaredMatch] = []
    if decision.status == http_claims.NO_INDEXED_HANDLER and decision.template is not None:
        for body, match in declared:
            method, _, route = body.partition(" ")
            if decision.method not in (None, method):
                continue
            if http.template_matches(decision.template, route):
                matches.append(match)
    return Draft(
        operation=HttpOperation(
            role="caller",
            status=OperationStatus(decision.status),
            method=decision.method,
            route=decision.route,
            path=path,
            line_start=lines[0],
            line_end=lines[1],
            entity=entity.key if entity else None,
            package=package,
            detail=decision.reason,
            routes=tuple(http.route_key(body) for body in decision.linked),
            candidates=decision.candidates,
            counterparts=tuple(_unique(handlers)[:MAX_COUNTERPARTS]),
            counterpart_count=len(_unique(handlers)),
            declared_matches=tuple(matches[:MAX_COUNTERPARTS]),
            composed=decision.composed,
        ),
        qualified_name=entity.qualified_name if entity else None,
        reaches=decision.matched,
    )


def _provider(
    *,
    body: str,
    path: str | None,
    lines: tuple[int | None, int | None],
    entity: EntityIdentity | None,
    package: str | None,
    reaching: Mapping[str, list[OperationCounterpart]],
    composed: bool,
) -> Draft:
    method, _, route = body.partition(" ")
    callers = _unique(reaching.get(body, []))
    return Draft(
        operation=HttpOperation(
            role="provider",
            status=OperationStatus.CALLED if callers else OperationStatus.NO_INDEXED_CALLER,
            method=method,
            route=route,
            path=path,
            line_start=lines[0],
            line_end=lines[1],
            entity=entity.key if entity else None,
            package=package,
            routes=(http.route_key(body),),
            counterparts=tuple(callers[:MAX_COUNTERPARTS]),
            counterpart_count=len(callers),
            composed=composed,
        ),
        qualified_name=entity.qualified_name if entity else None,
    )


def _unique(items: Iterable[OperationCounterpart]) -> list[OperationCounterpart]:
    return sorted(set(items), key=lambda c: (c.path or "", c.line or 0, c.entity))


def _meta(claim: UnresolvedRefRecord) -> dict[str, str]:
    loaded = json.loads(claim.meta_json) if claim.meta_json else {}
    return {str(key): str(value) for key, value in loaded.items()}


def _role(claim: UnresolvedRefRecord) -> str | None:
    return _meta(claim).get(PENDING_FACT)


def _entity_type(entities: Mapping[int, EntityIdentity], entity_id: int) -> EntityType:
    found = entities.get(entity_id)
    return found.type if found is not None else EntityType.SYMBOL


# -- filtering, order, paging ---------------------------------------------------------------


def selected(
    drafts: Sequence[Draft], request: HttpOperationsRequest, prefix: str | None
) -> list[Draft]:
    """The rows this request asks for, in their fixed order.

    Filters apply to rows, after the whole repository was planned: narrowing what
    is returned must never narrow what is known.
    A ``crossing`` filter keeps the callers with a crossing of those decisions, each
    with only those crossings. A ``decision_ref`` keeps the one caller holding that
    stored decision, with only it: an exact match on the ref, never a search.
    """
    wanted = set(request.status)
    crossing = set(request.crossing)
    rows: list[Draft] = []
    for draft in drafts:
        row = draft.operation
        if request.decision_ref is not None:
            named = tuple(c for c in row.crossings if c.decision_ref == request.decision_ref)
            if not named:
                continue
            if len(named) != len(row.crossings):
                row = row.model_copy(update={"crossings": named})
                draft = replace(draft, operation=row)
        if crossing:
            kept = tuple(c for c in row.crossings if c.status in crossing)
            if not kept:
                continue
            if len(kept) != len(row.crossings):
                row = row.model_copy(update={"crossings": kept})
                draft = replace(draft, operation=row)
        if request.role is not OperationRole.BOTH and row.role != request.role.value:
            continue
        if prefix is not None and not _under(row.path, prefix):
            continue
        if request.owner is not None and not _owned(draft.qualified_name, request.owner):
            continue
        if request.method is not None and row.method != request.method:
            continue
        if request.route_prefix is not None and not _route_under(row.route, request.route_prefix):
            continue
        if wanted and row.status not in wanted:
            continue
        rows.append(draft)
    return sorted(rows, key=lambda draft: draft.sort_key)


def in_scope(drafts: Sequence[Draft], prefix: str | None) -> list[Draft]:
    """The rows the scope covers — repository and path — whatever else is filtered."""
    return [draft for draft in drafts if prefix is None or _under(draft.operation.path, prefix)]


def _under(path: str | None, prefix: str) -> bool:
    return path is not None and (path == prefix or path.startswith(prefix.rstrip("/") + "/"))


def _owned(qualified: str | None, owner: str) -> bool:
    if qualified is None:
        return False
    if owner.endswith(".*"):
        return qualified.startswith(owner[:-1])
    return qualified == owner or qualified.startswith(owner + ".")


def _route_under(route: str, prefix: str) -> bool:
    """Whether ``route`` begins with ``prefix``, segment by segment.

    A caller's route is the path as the source writes it — ``/v1/courses/${courseId}``
    — and a provider's is the path as the handler declares it —
    ``/v1/courses/:courseId``. They name one route, so one prefix has to select
    both: read byte for byte, a prefix spelled either way silently answers for one
    role and not the other.

    So the literal reading is tried first and a shape reading second, using the
    same :func:`~repoatlas.models.http.route_shape` that decides whether a route
    named in prose was the route supplied. A segment filled by one value matches a
    declared parameter whatever either is called, and a literal never matches a
    parameter in either direction — ``/v1/courses/42`` still selects nothing that
    ``/v1/courses/:courseId`` serves. Only the segments the prefix names are
    compared; the route may carry any number more.
    """
    if prefix == "/":
        return route.startswith("/")
    base = prefix.rstrip("/")
    if route == base or route.startswith(base + "/"):
        return True
    shape, wanted = http.route_shape(route), http.route_shape(base)
    return bool(shape) and bool(wanted) and shape[: len(wanted)] == wanted


def summarize(drafts: Sequence[Draft]) -> OperationSummary:
    return OperationSummary(
        total=len(drafts),
        by_status=dict(sorted(Counter(d.operation.status.value for d in drafts).items())),
        by_role=dict(sorted(Counter(d.operation.role for d in drafts).items())),
        by_crossing=dict(
            sorted(Counter(c.status.value for d in drafts for c in d.operation.crossings).items())
        ),
    )


def coverage(
    inventory: Inventory, scoped: Sequence[Draft], prefix: str | None
) -> OperationCoverage:
    """What was read in scope, and every limitation that applies to it."""
    files = Counter(
        parser or "unparsed"
        for path, parser in inventory.parsers.items()
        if prefix is None or _under(path, prefix)
    )
    callers = [d.operation for d in scoped if d.operation.role == "caller"]
    unresolved = Counter(row.detail for row in callers if row.status is OperationStatus.UNRESOLVED)
    # A request only the join reads has a path; its crossings say what became of it.
    joined = sum(1 for row in callers if row.status is OperationStatus.JOIN_ONLY)
    ambiguous = sum(1 for row in callers if row.status is OperationStatus.AMBIGUOUS)
    declared = sum(1 for d in scoped if d.operation.status is OperationStatus.DECLARED)
    limitations = [
        CoverageLimitation(
            code="same_repository_only",
            note="A row's status considers only this repository's routes and callers; "
            "other repositories' routes on the shared nodes are reached with "
            "relationships. Crossings are the workspace join's stored decisions.",
        )
    ]
    if not inventory.crossings_built:
        limitations.append(
            CoverageLimitation(
                code="crossings_not_built",
                note="No global link pass has stored the workspace join's decisions in "
                "this index, so no row lists crossings; sync to store them.",
            )
        )
    if joined:
        limitations.append(
            CoverageLimitation(
                code="join_only",
                count=joined,
                note="Rows of status join_only: requests only the workspace join reads (an "
                "absolute or deployment-addressed URL, or a host the source does not "
                "state). Neither a route match nor an unresolved path, and not counted as "
                "either; their crossings say what the join decided.",
            )
        )
    fetch = unresolved.get(http_claims.REASON_DIRECT_FETCH, 0)
    if fetch:
        limitations.append(
            CoverageLimitation(
                code="direct_fetch_template",
                count=fetch,
                note="Direct fetch calls with a template URL are recorded, never matched.",
            )
        )
    others = sum(unresolved.values()) - fetch
    if others:
        limitations.append(
            CoverageLimitation(
                code="unresolved_path",
                count=others,
                note="Requests whose path could not be established; coverage.unresolved "
                "gives each reason.",
            )
        )
    if declared:
        limitations.append(
            CoverageLimitation(
                code="unproven_mount",
                count=declared,
                note="Routes declared without a proven mount; a caller they could serve "
                "lists them as declared_matches.",
            )
        )
    if any(parser in _TYPESCRIPT_PARSERS for parser in files):
        limitations.extend(
            CoverageLimitation(code=code, note=note) for code, note in _NOT_EXTRACTED
        )
    limitations.append(
        CoverageLimitation(
            code="unsupported_framework",
            note="Only NestJS, Express, FastAPI, Flask and OpenAPI declarations are "
            "extracted; handlers written any other way are not.",
        )
    )
    if inventory.stale_graph:
        limitations.append(
            CoverageLimitation(
                code="stored_graph_differs",
                note="The stored graph differs from a fresh reading of this repository "
                "(another version built it, or a link pass failed); sync the repository.",
            )
        )
    return OperationCoverage(
        files=dict(sorted(files.items())),
        unresolved=dict(sorted(unresolved.items())),
        ambiguous=ambiguous,
        declared=declared,
        unknown_paths=sum(unresolved.values()),
        limitations=tuple(limitations),
    )


def page_limits_for(request: HttpOperationsRequest) -> AppliedPageLimits:
    if request.limit is None:
        return AppliedPageLimits(applied=DEFAULT_PAGE_SIZE, ceiling=MAX_PAGE_SIZE)
    if request.limit > MAX_PAGE_SIZE:
        return AppliedPageLimits(
            requested=request.limit,
            applied=MAX_PAGE_SIZE,
            clamped=("limit",),
            ceiling=MAX_PAGE_SIZE,
        )
    return AppliedPageLimits(requested=request.limit, applied=request.limit, ceiling=MAX_PAGE_SIZE)


def request_digest(request: HttpOperationsRequest, repository: str, prefix: str | None) -> str:
    """Everything a cursor must still agree with. ``limit`` alone may change."""
    return digest(
        [
            ("scope", str(request.context.scope)),
            ("repository", repository),
            ("role", request.role.value),
            ("within_path", prefix or ""),
            ("owner", request.owner or ""),
            ("method", request.method or ""),
            ("route_prefix", request.route_prefix or ""),
            ("status", *sorted(status.value for status in request.status)),
            ("crossing", *sorted(status.value for status in request.crossing)),
            ("cross_repository", str(request.cross_repository)),
            ("decision_ref", request.decision_ref or ""),
            ("provenance", str(request.include_provenance)),
        ]
    )


def inventory_digest(drafts: Sequence[Draft]) -> str:
    """A digest of every fact the filtered, ordered rows could carry."""
    records: list[tuple[str, ...]] = []
    for draft in drafts:
        row = draft.operation
        records.append(
            (
                *draft.sort_key,
                row.status.value,
                str(row.line_end or 0),
                row.package or "",
                str(row.composed),
                *row.routes,
                "|",
                *row.candidates,
                "|",
                *(f"{c.entity}@{c.path}:{c.line}" for c in row.counterparts),
                str(row.counterpart_count),
                "|",
                *(f"{m.route}@{m.entity}" for m in row.declared_matches),
                "|",
                *(f"{c.decision_ref}={c.status.value}" for c in row.crossings),
            )
        )
    return digest(records)


def keyed(drafts: Sequence[Draft]) -> list[tuple[tuple[str, ...], Draft]]:
    """Each row with its continuation key: its order key and an ordinal among equals."""
    seen: Counter[tuple[str, ...]] = Counter()
    found: list[tuple[tuple[str, ...], Draft]] = []
    for draft in drafts:
        key = draft.sort_key
        found.append(((*key, str(seen[key])), draft))
        seen[key] += 1
    return found


def cut_page(
    drafts: Sequence[Draft],
    token: str | None,
    *,
    identity: str,
    inventory: str,
    limits: AppliedPageLimits,
) -> Page:
    rows = keyed(drafts)
    start = 0
    if token is not None:
        cursor = decode_cursor(token)
        if cursor[0] != identity:
            raise InvalidCursorError(_CURSOR_REFUSED)
        if cursor[1] != inventory:
            raise StaleCursorError(_CURSOR_STALE)
        positions = {key: index for index, (key, _) in enumerate(rows)}
        if cursor[2] not in positions:
            raise InvalidCursorError(_CURSOR_REFUSED)
        start = positions[cursor[2]] + 1
    stop = start + limits.applied
    chosen = rows[start:stop]
    return Page(
        rows=[draft.operation for _, draft in chosen],
        has_more=stop < len(rows),
        identity=identity,
        inventory=inventory,
        after=chosen[-1][0] if chosen and stop < len(rows) else None,
    )


def encode_cursor(*, request: str, inventory: str, after: tuple[str, ...]) -> str:
    payload = json.dumps(
        {"v": CURSOR_VERSION, "r": request, "i": inventory, "a": list(after)},
        separators=(",", ":"),
        sort_keys=True,
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(token: str) -> tuple[str, str, tuple[str, ...]]:
    """``(request digest, inventory digest, continuation key)``, or a refusal."""
    padding = "=" * (-len(token) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(token + padding).decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise InvalidCursorError(_CURSOR_REFUSED) from exc
    if not isinstance(payload, dict) or payload.get("v") != CURSOR_VERSION:
        raise InvalidCursorError(_CURSOR_REFUSED)
    request, inventory, after = payload.get("r"), payload.get("i"), payload.get("a")
    if (
        not isinstance(request, str)
        or not isinstance(inventory, str)
        or not isinstance(after, list)
        or len(after) not in (8, 10)
        or not all(isinstance(part, str) for part in after)
    ):
        raise InvalidCursorError(_CURSOR_REFUSED)
    return request, inventory, tuple(after)
