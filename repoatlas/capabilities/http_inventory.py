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
"""

from __future__ import annotations

import base64
import binascii
import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from repoatlas.capabilities.dependencies import digest
from repoatlas.capabilities.errors import InvalidCursorError, StaleCursorError
from repoatlas.graph import http_claims
from repoatlas.graph.http_claims import CallDecision, HttpClaimPlan
from repoatlas.graph.resolution import ResolutionIndex
from repoatlas.models import http
from repoatlas.models.dependency_inventory import AppliedPageLimits
from repoatlas.models.http_inventory import (
    DEFAULT_PAGE_SIZE,
    MAX_COUNTERPARTS,
    MAX_PAGE_SIZE,
    CoverageLimitation,
    DeclaredMatch,
    HttpOperation,
    HttpOperationsRequest,
    OperationCounterpart,
    OperationCoverage,
    OperationRole,
    OperationStatus,
    OperationSummary,
)
from repoatlas.models.parsing import EdgeType, EntityType
from repoatlas.parsers.http_facts import EXACT, FASTIFY_ROUTE, PENDING_FACT, PYTHON_ROUTE
from repoatlas.storage.protocols import EntityIdentity, FileRow, HttpEdgeRow, UnresolvedRefRecord

#: Version of the cursor payload. A token from another shape is refused.
CURSOR_VERSION = 1

GLOBAL_PARSER = "linker:global"

_CURSOR_REFUSED = "that HTTP operations cursor does not belong to this request"
_CURSOR_STALE = "the HTTP operation inventory changed since that page was issued"

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
) -> Inventory:
    """Every caller and provider row of one repository, unfiltered."""
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
        if claim.id is None or role not in {http_claims.NESTJS_ROUTE, PYTHON_ROUTE, FASTIFY_ROUTE}:
            continue
        if role == PYTHON_ROUTE and claim.id not in plan.promoted and claim.id not in plan.declared:
            continue  # served by its own module, whose edges are listed with the per-file ones
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
    return Inventory(drafts=drafts, parsers=parsers, stale_graph=stored != planned)


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
    """
    wanted = set(request.status)
    rows: list[Draft] = []
    for draft in drafts:
        row = draft.operation
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
    ambiguous = sum(1 for row in callers if row.status is OperationStatus.AMBIGUOUS)
    declared = sum(1 for d in scoped if d.operation.status is OperationStatus.DECLARED)
    limitations = [
        CoverageLimitation(
            code="same_repository_only",
            note="Only this repository's routes and callers are considered; other "
            "repositories are never examined here, and their routes on the shared nodes "
            "are reached with relationships.",
        )
    ]
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
