"""The HTTP Connections worklist: unresolved requests, candidates, previews and history.

Read from the join's stored decisions (``http_call_decisions``), the operators' declarations
(``deployment_links``) and the indexed routes of the services they name. Nothing is replanned for
the worklist itself, nothing is contacted, and nothing is written: a preview computes what a
declaration *would* do with the same matching rule the sync applies
(:func:`repoatlas.graph.http_crossings.declared_candidates`), so the two cannot disagree.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pydantic import ValidationError

from repoatlas.capabilities import http_inventory
from repoatlas.capabilities.errors import InvalidCursorError, InvalidDeploymentLinkError
from repoatlas.config.settings import DECLARATIONS_SLUG
from repoatlas.graph import link_exposures
from repoatlas.graph.deploy_identity import ServedRoute
from repoatlas.graph.http_crossings import (
    DECLARABLE,
    LINKED,
    SERVICE_UNMATCHED,
    UNKNOWN_BASE,
    _qualified_of,
    _scoped_exposure,
    declared_candidates,
    parse_exposure,
)
from repoatlas.graph.http_declarations import (
    RequestFacts,
    base_requested,
    declaration_state,
    display_base,
    facts_of,
    group_key,
    is_loopback_origin,
    origin_of_link,
    safe_route,
    scope_found,
    scoped_declaration_state,
)
from repoatlas.graph.http_scopes import (
    COVERED,
    OUT_OF_SCOPE,
    SCOPE_TOO_BROAD,
    FileScope,
    ScopeDeclaration,
    ScopeRequest,
    ScopeSymbol,
    base_key,
    base_kind,
    covers,
    level_allowed,
    symbols_overlap,
)
from repoatlas.graph.link_exposures import plan_link_exposures, plan_scoped_exposures
from repoatlas.graph.linker import (
    GLOBAL_PARSER,
    RepositoryHttp,
    repository_http_with_routes,
    repository_routes,
)
from repoatlas.graph.service_declarations import (
    KNOWN_EXTERNAL,
    classifications,
    classified_text,
    external_state,
    identity_of,
    link_origins,
)
from repoatlas.models import http
from repoatlas.models.deployment_targets import (
    DeclarationScope,
    DeploymentLink,
    DeploymentLinkFields,
    declared_base,
)
from repoatlas.models.http_connections import (
    CandidateService,
    ConnectionGroup,
    ConnectionRequest,
    ConnectionSite,
    ConnectionsSummary,
    DeclarationView,
    HistoryEntry,
    HttpConnectionPreviewRequest,
    HttpConnectionPreviewResponse,
    HttpConnectionsRequest,
    HttpConnectionsResponse,
    HttpDeclarationHistoryRequest,
    HttpDeclarationHistoryResponse,
    PreviewProviderDiagnostics,
    PreviewRow,
    ScopeChoice,
    UnpublishedMatch,
)
from repoatlas.models.http_inventory import CrossingStatus, OperationStatus
from repoatlas.models.repository import Repository
from repoatlas.models.service_declarations import (
    ExternalServiceFields,
    ServiceDeclaration,
    ServiceDeclarationView,
)
from repoatlas.services.workspace import Workspace
from repoatlas.storage.protocols import UnresolvedRefRecord

#: Samples a group lists inline; the rest are one expansion away.
SAMPLES = 3
#: Base paths tried per candidate service: the root, and up to this many leading literal segments.
MAX_BASE_DEPTH = 3
_CURSOR = "hcc1:"
#: Hints a preview names at most; the rest are counted.
MAX_UNPUBLISHED_MATCHES = 5


@dataclass(slots=True)
class _Tally:
    """What the exposures a preview builds offered (R1 diagnostics): filled beside the matching,
    never read by it."""

    #: Distinct ``(method, route)`` offered.
    routes: set[tuple[str, str]] = field(default_factory=set)
    #: Whether a link reached ``MAX_LINK_EXPOSURES``, so routes may have gone unoffered.
    truncated: bool = False

    def add(self, claims: Sequence[UnresolvedRefRecord]) -> None:
        for claim in claims:
            meta = json.loads(claim.meta_json or "{}")
            self.routes.add((str(meta.get("method", "")), str(meta.get("route", ""))))
        if len(claims) >= link_exposures.MAX_LINK_EXPOSURES:
            self.truncated = True


@dataclass(frozen=True, slots=True)
class _Hinting:
    """The target's plan and routes, planned once, and the tally of what was offered."""

    planned: RepositoryHttp
    routes: list[ServedRoute]
    tally: _Tally


def _slugs(workspace: Workspace) -> tuple[dict[int, str], set[str]]:
    repositories = workspace.repositories.list_all()
    return (
        {repo.id: repo.slug for repo in repositories if repo.id is not None},
        {repo.slug for repo in repositories},
    )


def _snapshot(workspace: Workspace) -> tuple[list[RequestFacts], Mapping[str, object]]:
    """Every stored decision, read, and what the declarations need to be judged."""
    knowledge = workspace.knowledge
    by_id, indexed = _slugs(workspace)
    rows = knowledge.list_http_call_decisions()
    facts = [facts_of(row, by_id.get(row.repo_id, "")) for row in rows]
    served = knowledge.list_served_routes(exclude_parser=GLOBAL_PARSER)
    served_by_slug = {
        by_id[repo_id]: bodies for repo_id, bodies in served.items() if repo_id in by_id
    }
    links = workspace.deployment_links.list_all()
    return facts, {
        "built": knowledge.http_call_decisions_built(),
        "pass_reads": knowledge.link_pass_reads(),
        "links": links,
        "indexed": indexed,
        "served": served_by_slug,
        "scopes": _declared_files(workspace, links),
        "external": workspace.service_declarations.list_all("external_service"),
    }


def _file_scope(workspace: Workspace, file_id: int, *, uses: bool) -> FileScope:
    """What one file offers a scope: its named entities and, with ``uses``, the ``USES`` pairs
    its per-file pass asserted (the global pass's own are replaced at a sync, so never read),
    read as the linker reads them for the join."""
    knowledge = workspace.knowledge
    members = knowledge.list_symbols(file_id=file_id)
    pairs: list[tuple[int, int]] = []
    if uses:
        pairs = [
            (user, used)
            for user, used, asserted_in, producer in knowledge.list_uses_targets(
                [symbol.entity_id for symbol in members]
            )
            if asserted_in == file_id and producer != GLOBAL_PARSER
        ]
    return FileScope.build(
        (
            ScopeSymbol(s.entity_id, s.file_id, s.type.value, s.qualified_name)
            for s in members
            if s.qualified_name
        ),
        pairs,
    )


def _declared_files(
    workspace: Workspace, links: Sequence[DeploymentLink]
) -> dict[tuple[str, str], FileScope]:
    """The entities of each file a scoped declaration names, by the caller's slug and the path:
    what its state needs to say whether the file and the symbol are still there. A file that is
    not indexed is absent."""
    named = {
        (link.callers[0], link.scope.path)
        for link in links
        if link.scope is not None and len(link.callers) == 1
    }
    if not named:
        return {}
    ids = {repo.slug: repo.id for repo in workspace.repositories.list_all()}
    found: dict[tuple[str, str], FileScope] = {}
    for slug in sorted({caller for caller, _ in named}):
        repo_id = ids.get(slug)
        if repo_id is None:
            continue
        for row in workspace.knowledge.list_file_rows(repo_id):
            if (slug, row.rel_path) in named:
                found[(slug, row.rel_path)] = _file_scope(workspace, row.file_id, uses=False)
    return found


def _declarations(
    facts: Sequence[RequestFacts], context: Mapping[str, object]
) -> list[DeclarationView]:
    """Each declaration with its derived state, newest first."""
    links: list[DeploymentLink] = context["links"]  # type: ignore[assignment]
    indexed: set[str] = context["indexed"]  # type: ignore[assignment]
    served: Mapping[str, list[str]] = context["served"]  # type: ignore[assignment]
    pass_reads: Mapping[int, str] | None = context["pass_reads"]  # type: ignore[assignment]
    scopes: Mapping[tuple[str, str], FileScope] = context["scopes"]  # type: ignore[assignment]
    joined = Counter(f.joined_by for f in facts if f.joined_by is not None)
    declined: dict[int, Counter[str]] = defaultdict(Counter)
    at_origin: Counter[str] = Counter()
    for fact in facts:
        if fact.target.origin is not None:
            at_origin[fact.target.origin] += 1
        if not fact.linked and fact.declaration_outcome is not None:
            for link_id in fact.declaration_ids:
                declined[link_id][fact.declaration_outcome] += 1
    views: list[DeclarationView] = []
    for link in sorted(links, key=lambda item: -item.id):
        scope = link.scope
        if scope is None:
            origin = origin_of_link(link)
            state, reason = declaration_state(
                link,
                pass_reads=pass_reads,
                indexed=link.repository in indexed,
                serves_routes=bool(served.get(link.repository)),
                requests_at_base=at_origin[origin] if origin is not None else 0,
                joined=joined[link.id],
                declined=declined[link.id],
            )
        else:
            # A scoped declaration states no URL: it never reaches the origin rules above.
            caller = link.callers[0] if len(link.callers) == 1 else None
            place = scopes.get((caller, scope.path)) if caller is not None else None
            state, reason = scoped_declaration_state(
                link,
                pass_reads=pass_reads,
                indexed=link.repository in indexed,
                caller_indexed=caller in indexed,
                serves_routes=bool(served.get(link.repository)),
                found=scope_found(place, scope),
                requested=caller is not None and base_requested(facts, caller, scope),
                joined=joined[link.id],
                declined=declined[link.id],
            )
        views.append(
            DeclarationView(
                id=link.id,
                repository=link.repository,
                label=link.label,
                url=link.url,
                environment=link.environment,
                callers=tuple(link.callers),
                verification=link.verification,
                created_at=link.created_at,
                updated_at=link.updated_at,
                state=state,
                reason=reason,  # type: ignore[arg-type]
                joined=joined[link.id],
                declined=dict(sorted(declined[link.id].items())),
                scope=scope,
            )
        )
    return views


@dataclass(frozen=True, slots=True)
class _Externals:
    """The external-service classifications, each judged against the stored decisions."""

    #: Newest first, as the declarations are.
    views: tuple[ServiceDeclarationView, ...]
    #: The classification of each origin, in any state.
    by_origin: Mapping[str, ServiceDeclarationView]
    #: The id of the **applied** classification of each origin: what gives a request its identity.
    known: Mapping[str, int]


def _externals(facts: Sequence[RequestFacts], context: Mapping[str, object]) -> _Externals:
    """Judge each classification: its state is derived from the stored decisions on every read,
    never stored, and takes effect the moment the classification is."""
    declarations: list[ServiceDeclaration] = context["external"]  # type: ignore[assignment]
    links: list[DeploymentLink] = context["links"]  # type: ignore[assignment]
    indexed: set[str] = context["indexed"]  # type: ignore[assignment]
    built = bool(context["built"])
    link_at = link_origins(links)
    at_origin: dict[str, list[RequestFacts]] = defaultdict(list)
    for fact in facts:
        if fact.target.origin is not None:
            at_origin[fact.target.origin].append(fact)
    views: list[ServiceDeclarationView] = []
    by_origin: dict[str, ServiceDeclarationView] = {}
    known: dict[str, int] = {}
    # Newest first; the store holds one classification per origin, so nothing is overwritten.
    for declaration in sorted(declarations, key=lambda item: -item.id):
        fields = declaration.fields
        if not isinstance(fields, ExternalServiceFields):
            continue
        members = at_origin.get(fields.origin, [])
        state, reason = external_state(
            fields, members, link_at.get(fields.origin), indexed, built=built
        )
        unmatched = sum(1 for member in members if member.status == SERVICE_UNMATCHED)
        view = ServiceDeclarationView(
            id=declaration.id,
            fields=fields,
            created_at=declaration.created_at,
            updated_at=declaration.updated_at,
            state=state,
            reason=reason,
            requests=unmatched if state == "applied" else 0,
        )
        views.append(view)
        by_origin[fields.origin] = view
        if state == "applied":
            known[fields.origin] = declaration.id
    return _Externals(tuple(views), by_origin, known)


def _request_view(fact: RequestFacts, known: Mapping[str, int] | None = None) -> ConnectionRequest:
    row = fact.row
    identity, external_id = identity_of(fact, known or {})
    return ConnectionRequest(
        decision_ref=row.decision_ref,
        method=row.method or None,
        route=safe_route(row.url),
        site=ConnectionSite(
            repository=fact.repository,
            path=row.path or "",
            line_start=row.line_start,
            line_end=row.line_end,
        ),
        status=CrossingStatus(fact.status),
        reason=fact.reason,
        language=fact.language,
        declarable=fact.declarable,
        declaration_ids=fact.declaration_ids,
        declaration_outcome=fact.declaration_outcome,
        joined_by=fact.joined_by,
        symbol=fact.symbol,
        base_key=fact.base_key,
        scopable=fact.scopable,
        scope_refusal=fact.scope_refusal,
        identity=identity,
        external_service_id=external_id,
    )


def _order(fact: RequestFacts) -> tuple[str, str, int, str]:
    return (fact.repository, fact.row.path or "", fact.row.line_start or 0, fact.row.decision_ref)


def connections(workspace: Workspace, request: HttpConnectionsRequest) -> HttpConnectionsResponse:
    facts, context = _snapshot(workspace)
    declarations = _declarations(facts, context)
    by_origin: dict[str, list[DeclarationView]] = defaultdict(list)
    by_base: dict[str, list[DeclarationView]] = defaultdict(list)
    for link, view in zip(
        sorted(context["links"], key=lambda item: -item.id),  # type: ignore[arg-type,attr-defined]
        declarations,
        strict=True,
    ):
        if link.scope is not None:
            by_base[link.scope.base_key].append(view)
            continue
        origin = origin_of_link(link)
        if origin is not None:
            by_origin[origin].append(view)

    externals = _externals(facts, context)
    linked = [f for f in facts if f.linked]
    open_facts = [f for f in facts if not f.linked]
    known_external = sum(
        1 for f in open_facts if identity_of(f, externals.known)[0] == KNOWN_EXTERNAL
    )
    summary = ConnectionsSummary(
        requests=len(facts),
        linked_by_source=sum(1 for f in linked if not f.by_declaration),
        linked_by_declaration=sum(1 for f in linked if f.by_declaration),
        unresolved=len(open_facts),
        declarable=sum(1 for f in open_facts if f.declarable),
        base_not_stated=sum(1 for f in open_facts if f.target.origin is None),
        scopable=sum(1 for f in open_facts if f.scopable),
        declarations=dict(sorted(Counter(d.state for d in declarations).items())),
        by_status=dict(sorted(Counter(f.status for f in open_facts).items())),
        known_external=known_external,
        unresolved_identity=len(open_facts) - known_external,
    )

    wanted = [
        f
        for f in (facts if request.include_resolved else open_facts)
        if (not request.status or f.status in {s.value for s in request.status} or f.linked)
        and (request.language is None or f.language == request.language)
        and (request.caller is None or f.repository == request.caller)
        and (request.identity is None or identity_of(f, externals.known)[0] == request.identity)
    ]
    groups: dict[str, list[RequestFacts]] = defaultdict(list)
    for fact in wanted:
        groups[group_key(fact.target)].append(fact)

    listed: list[ConnectionGroup] = []
    for key, members in groups.items():
        sample = members[0].target
        origin = sample.origin
        expression = display_base(sample.base_expression)
        text = request.text.lower() if request.text else None
        if text and text not in (origin or "").lower() and text not in (expression or "").lower():
            continue
        if origin:
            views = tuple(by_origin.get(origin, ()))
        else:
            # An unstated base is declared by the hash of its text, in a file of one caller.
            keys = sorted({m.base_key for m in members if m.base_key is not None})
            views = tuple(view for base in keys for view in by_base.get(base, ()))
        if request.declaration_state and not any(
            v.state == request.declaration_state for v in views
        ):
            continue
        unresolved = [m for m in members if not m.linked]
        if not unresolved and not request.include_resolved:
            continue
        ordered = sorted(unresolved or members, key=_order)
        # Read from the index per file, so only for the one group an operator has opened.
        choices = _scope_choices(workspace, members) if key == request.group and not origin else ()
        listed.append(
            ConnectionGroup(
                key=key,
                kind="stated-origin" if origin else "base-not-stated",
                origin=origin,
                base_expression=expression,
                loopback=is_loopback_origin(origin),
                requests=len(members),
                declarable=sum(1 for m in members if m.declarable),
                statuses=dict(sorted(Counter(m.status for m in members).items())),
                languages=dict(sorted(Counter(m.language for m in members).items())),
                callers=tuple(sorted({m.repository for m in members})),
                reasons=dict(sorted(Counter(m.reason for m in unresolved if m.reason).items())),
                samples=tuple(_request_view(m, externals.known) for m in ordered[:SAMPLES]),
                declarations=views,
                scope_choices=choices,
                external_service=externals.by_origin.get(origin) if origin else None,
            )
        )
    listed.sort(key=lambda g: (-g.requests, g.origin or "~", g.base_expression or "", g.key))

    expanded: tuple[ConnectionRequest, ...] = ()
    candidates: tuple[CandidateService, ...] = ()
    has_more, next_cursor = False, None
    if request.group is not None:
        members = sorted(groups.get(request.group, ()), key=_order)
        start = _offset(request.cursor)
        page = members[start : start + request.limit]
        expanded = tuple(_request_view(m, externals.known) for m in page)
        has_more = start + request.limit < len(members)
        next_cursor = _cursor(start + request.limit) if has_more else None
        origin_of = members[0].target.origin if members else None
        if origin_of is not None:
            candidates = _candidates(
                workspace,
                origin_of,
                members,
                context["served"],  # type: ignore[arg-type]
            )
        elif members:
            candidates = _scoped_candidates(
                workspace,
                members,
                context["served"],  # type: ignore[arg-type]
            )

    return HttpConnectionsResponse(
        context=request.context,
        built=bool(context["built"]),
        synced=context["pass_reads"] is not None,
        summary=summary,
        groups=tuple(listed),
        declarations=tuple(declarations),
        requests=expanded,
        candidates=candidates,
        has_more=has_more,
        next_cursor=next_cursor,
        service_declarations=externals.views,
    )


def _offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        text = base64.urlsafe_b64decode(cursor.encode()).decode()
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise InvalidCursorError("the continuation token is not one this answer issued") from exc
    if not text.startswith(_CURSOR) or not text[len(_CURSOR) :].isdigit():
        raise InvalidCursorError("the continuation token is not one this answer issued")
    return int(text[len(_CURSOR) :])


def _cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"{_CURSOR}{offset}".encode()).decode()


#: Services whose routes are checked exactly for one group: the best prefiltered by route shape.
MAX_CANDIDATES = 12


def _shortlist(
    wanted: Sequence[RequestFacts], served: Mapping[str, Sequence[str]]
) -> list[tuple[int, str, str]]:
    """(requests matched, repository, base path) of each service one of whose served routes some
    wanted request's shape matches, best first. The base path is the root or a literal prefix of
    the requests' own paths, whichever matches the most (the shorter on a tie)."""
    prefixes: list[tuple[str, ...]] = [()]
    for member in wanted:
        literal: list[str] = []
        for segment in member.target.segments[:MAX_BASE_DEPTH]:
            if not segment:  # a value fills it, or it is the root's empty one: no literal base
                break
            literal.append(segment)
            prefixes.append(tuple(literal))
    shortlist: list[tuple[int, str, str]] = []
    for repository, bodies in served.items():
        if repository == DECLARATIONS_SLUG:
            continue
        routes = [(b.partition(" ")[0], b.partition(" ")[2]) for b in bodies]
        best: tuple[int, str] | None = None
        for prefix in dict.fromkeys(prefixes):
            base = "/" + "/".join(prefix) if prefix else ""
            matched = sum(
                1
                for member in wanted
                if any(
                    method == member.row.method
                    and http.endpoint_matches(member.target.segments, base + path)
                    for method, path in routes
                )
            )
            if matched and (best is None or (matched, -len(base)) > (best[0], -len(best[1]))):
                best = (matched, base)
        if best is not None:
            shortlist.append((best[0], repository, best[1]))
    shortlist.sort(key=lambda item: (-item[0], item[1]))
    return shortlist


def _candidates(
    workspace: Workspace,
    origin: str,
    members: Sequence[RequestFacts],
    served: Mapping[str, Sequence[str]],
) -> tuple[CandidateService, ...]:
    """Services whose served routes some declarable request's shape matches, best first.

    A hypothesis, never applied and never chosen by a name: route shapes prefilter (the root, or a
    literal prefix of the requests' own paths, as the base), then each survivor is evaluated
    exactly as a preview would, so the counts are the sync's own."""
    wanted = [m for m in members if m.declarable]
    if not wanted:
        return ()
    shortlist = _shortlist(wanted, served)
    registered = {repo.slug: repo for repo in workspace.repositories.list_all()}
    found: list[CandidateService] = []
    for _, repository, base in shortlist[:MAX_CANDIDATES]:
        repo = registered.get(repository)
        if repo is None:
            continue
        outcomes, routes_served = _evaluate(workspace, repo, origin + base, (), None, wanted)
        counts = Counter(outcome for _, outcome, _ in outcomes)
        if counts["would-link"] + counts["ambiguous"]:
            found.append(
                CandidateService(
                    repository=repository,
                    routes_served=routes_served,
                    requests_matched=counts["would-link"] + counts["ambiguous"],
                    requests_unique=counts["would-link"],
                    requests_ambiguous=counts["ambiguous"],
                    suggested_base_path=base,
                )
            )
    found.sort(key=lambda c: (-c.requests_unique, -c.requests_matched, c.repository))
    return tuple(found)


def _scoped_candidates(
    workspace: Workspace,
    members: Sequence[RequestFacts],
    served: Mapping[str, Sequence[str]],
) -> tuple[CandidateService, ...]:
    """Services whose served routes some scopable request of an unstated base matches, best first.

    As for a stated origin, a hypothesis never applied and never chosen by a name: the same route
    shapes prefilter, then each survivor is evaluated as a scoped declaration of it would be
    decided (:func:`_evaluate_scoped`). ``suggested_base_path`` is the prefix the service would
    mount its routes under for the requests to match as they are written."""
    wanted = [m for m in members if m.scopable]
    if not wanted:
        return ()
    registered = {repo.slug: repo for repo in workspace.repositories.list_all()}
    found: list[CandidateService] = []
    for _, repository, base in _shortlist(wanted, served)[:MAX_CANDIDATES]:
        repo = registered.get(repository)
        if repo is None:
            continue
        outcomes, routes_served = _evaluate_scoped(workspace, repo, base, wanted)
        counts = Counter(outcome for _, outcome, _ in outcomes)
        if counts["would-link"] + counts["ambiguous"]:
            found.append(
                CandidateService(
                    repository=repository,
                    routes_served=routes_served,
                    requests_matched=counts["would-link"] + counts["ambiguous"],
                    requests_unique=counts["would-link"],
                    requests_ambiguous=counts["ambiguous"],
                    suggested_base_path=base,
                )
            )
    found.sort(key=lambda c: (-c.requests_unique, -c.requests_matched, c.repository))
    return tuple(found)


def _evaluate_scoped(
    workspace: Workspace,
    repository: Repository,
    base_path: str,
    members: Sequence[RequestFacts],
    *,
    known: list[ServedRoute] | None = None,
    tally: _Tally | None = None,
) -> tuple[list[tuple[RequestFacts, str, str | None]], int]:
    """What a scoped declaration of ``repository`` under ``base_path`` would do to each scopable
    request, as :func:`_evaluate` does for a URL: the repository's routes as the global pass sees
    them, the exposures a scoped declaration would add (with the route prefix before each route),
    and the join's own matching of the request's path as it is written. Whether a request is
    inside the scope an operator names is not asked here: every request given is taken as covered.
    Returns each request with its outcome and handler place, and how many routes it serves."""
    assert repository.id is not None
    routes = [
        route
        for route in (
            known if known is not None else repository_routes(workspace.knowledge, repository.id)
        )
        if route.repo_id == repository.id
    ]
    ids = {repo.slug: repo.id for repo in workspace.repositories.list_all() if repo.id is not None}
    files = {row.file_id: row for row in workspace.knowledge.list_file_rows(repository.id)}
    now = datetime.now(timezone.utc)
    mine = [f for f in members if f.scopable and not f.linked]
    offers = {}
    for caller, shown in sorted({(f.repository, _shown(f)) for f in mine}):
        # Never stored, so not validated: the words of the scope are the group's own.
        synthetic = DeploymentLink.model_construct(
            id=0,
            repository=repository.slug,
            label="preview",
            environment=None,
            url=None,
            target_id=None,
            variables={},
            conditions=[],
            verification="unverified",
            callers=[caller],
            scope=DeclarationScope.model_construct(
                path="", symbol=None, base_key="", base_shown=shown, base_path=base_path
            ),
            created_at=now,
            updated_at=now,
            verified_at=None,
        )
        claims = plan_scoped_exposures([synthetic], {repository.id: routes}, ids)
        if tally is not None:
            tally.add(claims)
        offered = (_scoped_exposure(claim, json.loads(claim.meta_json or "{}")) for claim in claims)
        offers[(caller, shown)] = [e for e in offered if e is not None]
    result: list[tuple[RequestFacts, str, str | None]] = []
    for fact in mine:
        at_method = [
            e for e in offers[(fact.repository, _shown(fact))] if e.method == fact.row.method
        ]
        found, why = declared_candidates(fact.target.segments, at_method, fact.repository)
        outcome = {
            "": "would-link",
            "ambiguous": "ambiguous",
            "route-unmatched": "no-matching-route",
            "callers-excluded": "caller-excluded",
        }[why]
        handler = None
        if outcome == "would-link":
            claim = found[0].claim
            where = files.get(claim.file_id)
            handler = f"{where.rel_path}:{claim.line_start}" if where is not None else None
        result.append((fact, outcome, handler))
    return result, len(routes)


def _masked(expression: str | None) -> str:
    """How a scoped declaration shows a base: its name when that is a plain one, otherwise only
    that it is a value (``{…}``). A dotted name (``self.base_url``) is masked too: a scope never
    shows an expression (``DeclarationScope`` refuses one). ``display_base`` is the A1 display and
    is not changed."""
    shown = display_base(expression)
    return "{…}" if shown is None or "." in shown else shown


def _shown(fact: RequestFacts) -> str:
    """How a request's base is shown, as a scoped declaration's exposures show it."""
    return _masked(fact.target.base_expression)


def _scope_choices(
    workspace: Workspace, members: Sequence[RequestFacts]
) -> tuple[ScopeChoice, ...]:
    """The scopes an operator may name for one group of unstated-base requests, never one chosen.

    Per file of a caller that has scopable requests in the group: the file, and each dotted prefix
    of an enclosing symbol that names exactly one entity of that file. Each choice counts the
    group's requests of that file it covers, and those it would reach and refuse as too broad,
    by asking :func:`repoatlas.graph.http_scopes.covers` with the declaration it would be."""
    placed: dict[tuple[str, str, int], list[RequestFacts]] = defaultdict(list)
    for member in members:
        if member.scopable and member.base_key is not None:
            placed[(member.repository, member.row.path or "", member.row.file_id)].append(member)
    choices: list[ScopeChoice] = []
    for (repository, path, file_id), here in sorted(placed.items()):
        scope = _file_scope(workspace, file_id, uses=True)
        symbols: set[str | None] = {None}
        for member in here:
            parts = member.symbol.split(".") if member.symbol else []
            for depth in range(1, len(parts) + 1):
                name = ".".join(parts[:depth])
                if scope.entity_count(name) == 1:
                    symbols.add(name)
        for symbol in sorted(symbols, key=lambda item: (item is not None, item or "")):
            verdicts = Counter(
                covers(
                    ScopeDeclaration(repository, path, member.base_key or "", symbol),
                    ScopeRequest(
                        repository, path, member.row.url, member.symbol or "", member.row.src_id
                    ),
                    scope,
                )
                for member in here
            )
            choices.append(
                ScopeChoice(
                    repository=repository,
                    path=path,
                    symbol=symbol,
                    covered=verdicts[COVERED],
                    too_broad=verdicts[SCOPE_TOO_BROAD],
                )
            )
    return tuple(choices)


def _evaluate(
    workspace: Workspace,
    repository: Repository,
    url: str,
    callers: Sequence[str],
    environment: str | None,
    members: Sequence[RequestFacts],
    *,
    known: list[ServedRoute] | None = None,
    tally: _Tally | None = None,
) -> tuple[list[tuple[RequestFacts, str, str | None]], int]:
    """What a declaration of ``url`` for ``repository`` would do to each request: the repository's
    routes as the global pass sees them, the exposures the link would add, the join's own matching.
    Returns each request with its outcome and handler place, and how many routes it serves.

    ``known`` are the repository's routes when the caller planned it already (a preview with
    diagnostics), else they are read as the global pass sees them; ``tally`` records, beside the
    matching, what the exposures offered."""
    assert repository.id is not None
    routes = known if known is not None else repository_routes(workspace.knowledge, repository.id)
    served = len([r for r in routes if r.repo_id == repository.id])
    now = datetime.now(timezone.utc)
    # Never stored, so not validated: a loopback origin with no callers is a question the preview
    # answers (it states the requirement), not an error to raise while answering it.
    synthetic = DeploymentLink.model_construct(
        id=0,
        repository=repository.slug,
        label="preview",
        environment=environment,
        url=url,
        target_id=None,
        variables={},
        conditions=[],
        verification="unverified",
        callers=list(callers),
        created_at=now,
        updated_at=now,
        verified_at=None,
    )
    claims = plan_link_exposures([synthetic], {}, routes, {repository.slug: repository.id})
    if tally is not None:
        tally.add(claims)
    exposures = [e for e in (parse_exposure(claim) for claim in claims) if e is not None]
    files = {row.file_id: row for row in workspace.knowledge.list_file_rows(repository.id)}
    result: list[tuple[RequestFacts, str, str | None]] = []
    for fact in members:
        if fact.linked or fact.target.origin is None:
            continue
        if not fact.declarable:
            result.append((fact, "source-established", None))
            continue
        at_origin = [
            e for e in exposures if e.method == fact.row.method and e.origin == fact.target.origin
        ]
        found, why = declared_candidates(fact.target.segments, at_origin, fact.repository)
        outcome = {
            "": "would-link",
            "ambiguous": "ambiguous",
            "route-unmatched": "no-matching-route",
            "callers-excluded": "caller-excluded",
        }[why]
        handler = None
        if outcome == "would-link":
            claim = found[0].claim
            where = files.get(claim.file_id)
            handler = f"{where.rel_path}:{claim.line_start}" if where is not None else None
        result.append((fact, outcome, handler))
    return result, served


#: What a preview cautions of the requests the sync would leave unjoined, by the verdict.
_SCOPED_WARNINGS = (
    ("ambiguous", "match more than one handler or service and would stay refused"),
    (
        "scope-too-broad",
        "are on this base but not provably the one this scope names (a receiver base outside "
        "the symbol's own methods, or a name its code is not shown to read) and would stay "
        "refused",
    ),
    (
        "scope-unidentified",
        "are on this base but cannot be identified in this scope and would stay refused",
    ),
    (
        "injected-root-unselected",
        "are made by a function handed its transport at run time, which one scope cannot name, "
        "and would stay refused",
    ),
)


def _hinting(workspace: Workspace, repository: Repository) -> _Hinting:
    """Plan the target once for a preview with diagnostics: its plan, and the routes
    :func:`repository_routes` would give from it."""
    assert repository.id is not None
    planned, routes = repository_http_with_routes(workspace.knowledge, repository.id)
    return _Hinting(planned, routes, _Tally())


def _provider_diagnostics(
    workspace: Workspace,
    repository: Repository,
    hinting: _Hinting,
    requests: Sequence[tuple[RequestFacts, str, str | None]],
    served: int,
    base_path: str,
) -> PreviewProviderDiagnostics:
    """What a preview with ``include_diagnostics`` adds (R1): hints, never edges or decisions.

    The target's lifecycle, as ``http_operations`` lists it. How many routes the declaration could
    expose, and how many served routes no declaration can (they state no line: composed). And,
    for each request that would join nothing, the target's declared or unpublished routes of the
    same method it could reach once the declared ``base_path`` is taken off, by the rule the join
    matches served routes with (:func:`repoatlas.models.http.endpoint_matches`: a concrete value
    reaches a declared parameter). A request that does not start with the base path has none.
    Nothing here reaches the preview's requirements, warnings, rows, counts or ``can_create``."""
    assert repository.id is not None
    registered = workspace.repositories.list_all()
    slugs = {repo.id: repo.slug for repo in registered if repo.id is not None}
    inventory = http_inventory.repository_inventory(
        workspace.knowledge,
        repository.id,
        slugs,
        include_diagnostics=True,
        planned=hinting.planned,
    )
    lifecycle = http_inventory.coverage(inventory, inventory.drafts, None).provider_lifecycle
    assert lifecycle is not None
    mine = [route for route in hinting.routes if route.repo_id == repository.id]
    stated = {r.body for r in mine if r.line_start is not None or r.declared_at is not None}
    unstated = {r.body for r in mine if r.line_start is None and r.declared_at is None} - stated

    unserved = sorted(
        (
            draft.operation
            for draft in inventory.drafts
            if draft.operation.role == "provider"
            and draft.operation.status in (OperationStatus.DECLARED, OperationStatus.UNPUBLISHED)
            and draft.operation.route
            and draft.operation.method
        ),
        key=lambda row: (row.path or "", row.line_start or 0, row.method or "", row.route),
    )
    base = tuple(part for part in base_path.split("/") if part)
    matches: list[UnpublishedMatch] = []
    for fact, outcome, _ in requests:
        if outcome != "no-matching-route" and served != 0:
            continue
        segments = tuple(fact.target.segments)
        if segments[: len(base)] != base:
            continue
        for row in unserved:
            if row.method != fact.row.method or not http.endpoint_matches(
                segments[len(base) :], row.route
            ):
                continue
            diagnostic = row.diagnostic
            matches.append(
                UnpublishedMatch(
                    decision_ref=fact.row.decision_ref,
                    route=f"{row.method} {row.route}",
                    status="declared" if row.status is OperationStatus.DECLARED else "unpublished",
                    path=row.path,
                    line=row.line_start,
                    category=diagnostic.category if diagnostic else None,
                    code=diagnostic.code if diagnostic else "",
                    detail=row.detail,
                )
            )
    return PreviewProviderDiagnostics(
        routes_exposable=len(hinting.tally.routes),
        routes_without_declaration_line=len(unstated),
        exposures_truncated=hinting.tally.truncated,
        lifecycle=lifecycle,
        unpublished_matches=tuple(matches[:MAX_UNPUBLISHED_MATCHES]),
        unpublished_match_count=len(matches),
    )


def _problems(error: ValidationError) -> list[str]:
    return [str(err["msg"]).removeprefix("Value error, ") for err in error.errors()]


def _meta_of(fact: RequestFacts) -> dict[str, object]:
    """The evidence the join stored for a request: what its claim said, as the sync read it."""
    try:
        loaded = json.loads(fact.row.evidence_json or "{}")
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _preview_scoped(
    workspace: Workspace,
    request: HttpConnectionPreviewRequest,
    facts: Sequence[RequestFacts],
    members: Sequence[RequestFacts],
    context: Mapping[str, object],
) -> HttpConnectionPreviewResponse:
    """What a scoped declaration of one repository would do to each request of a group whose base
    the source does not state, as the sync will decide it.

    Each verdict is the sync's own function. ``http_scopes.covers`` says whether the declared
    file, symbol, base text and base kind cover a request, or why not; the handler comes from the
    exposures ``plan_scoped_exposures`` gives the service's routes, matched by
    ``declared_candidates`` through :func:`_evaluate_scoped` (the candidates use it too); and a
    stored declaration of another service that covers the same request makes it ``ambiguous``, as
    the join does. The base path is the service's route prefix: it is never added to or removed
    from a request. The base is named by its key and shown masked; its text is not carried."""
    asked = request.scope
    assert asked is not None
    expression = members[0].target.base_expression
    if members[0].target.origin is not None or expression is None:
        raise InvalidDeploymentLinkError(
            "these requests do not leave a base expression unstated",
            detail="A scope names a base the source writes as an expression and does not state; "
            "a stated origin is mapped without one.",
        )
    repositories = {repo.slug: repo for repo in workspace.repositories.list_all()}
    service = repositories.get(request.repository)
    if service is None or service.id is None or service.slug == DECLARATIONS_SLUG:
        raise InvalidDeploymentLinkError(
            "that repository is not indexed",
            detail="A mapping names an indexed service: one this index holds routes of.",
        )
    links: list[DeploymentLink] = context["links"]  # type: ignore[assignment]
    indexed: set[str] = context["indexed"]  # type: ignore[assignment]
    served_by: Mapping[str, list[str]] = context["served"]  # type: ignore[assignment]
    shown = _masked(expression)
    requirements: list[str] = []
    warnings: list[str] = []
    try:
        scope = DeclarationScope(
            path=asked.path,
            symbol=asked.symbol,
            base_key=asked.base_key,
            base_shown=shown,
            base_path=asked.base_path,
        )
    except ValidationError as exc:
        # The words are not a scope at all: nothing further can be asked of them.
        return HttpConnectionPreviewResponse(
            context=request.context,
            group=request.group,
            repository=service.slug,
            routes_served=0,
            counts={},
            requirements=tuple(dict.fromkeys(_problems(exc))),
            can_create=False,
        )
    try:
        DeploymentLinkFields(
            label="mapped from the HTTP Connections worklist",
            environment=request.environment,
            callers=[asked.caller],
            scope=scope,
        )
    except ValidationError as exc:
        requirements.extend(_problems(exc))

    place = scope.path + (f"#{scope.symbol}" if scope.symbol else "")
    caller = repositories.get(asked.caller)
    file_scope = FileScope()
    if caller is None or caller.id is None:
        requirements.append(f"{asked.caller} is not an indexed repository")
    else:
        file_row = next(
            (r for r in workspace.knowledge.list_file_rows(caller.id) if r.rel_path == scope.path),
            None,
        )
        if file_row is None:
            requirements.append(f"{scope.path} is not a file of {asked.caller} in this index")
        else:
            file_scope = _file_scope(workspace, file_row.file_id, uses=True)
            if scope.symbol is not None and file_scope.entity_count(scope.symbol) != 1:
                requirements.append(
                    f"{scope.symbol} names {file_scope.entity_count(scope.symbol)} entities of "
                    f"{scope.path}, not exactly one: it identifies no scope"
                )
    if not base_requested(facts, asked.caller, scope):
        requirements.append(
            f"the scope cannot be identified: no request of {asked.caller} in {place} leaves "
            f"the base {shown} unstated"
        )
    if not level_allowed(base_kind(expression[1:-1]), scope.symbol):
        requirements.append(
            "a receiver-rooted base (`self.`, `cls.`, `this.`) names no single binding in a "
            "whole file: scope it to a symbol"
        )

    declared = ScopeDeclaration(asked.caller, scope.path, scope.base_key, scope.symbol)
    # Declarations of another service (or base path) over this file, as the join weighs them.
    rivals = [
        ScopeDeclaration(asked.caller, link.scope.path, link.scope.base_key, link.scope.symbol)
        for link in links
        if link.scope is not None
        and link.callers == [asked.caller]
        and link.scope.path == scope.path
        and (link.repository, link.scope.base_path) != (service.slug, scope.base_path)
        and link.repository in indexed
        and served_by.get(link.repository)
    ]
    waiting: list[tuple[RequestFacts, str]] = []
    rivalled: set[str] = set()
    for fact in members:
        if fact.linked:
            continue
        if fact.status != UNKNOWN_BASE:
            waiting.append((fact, "not-declarable"))
            continue
        # Only a request in the declared file needs its symbol and evidence (`covers` looks no
        # further for another), as the join reads them.
        inside = fact.repository == asked.caller and (fact.row.path or "") == scope.path
        ask = ScopeRequest(
            fact.repository,
            fact.row.path or "",
            fact.row.url,
            _qualified_of(file_scope, fact.row.src_id) if inside else "",
            fact.row.src_id,
            _meta_of(fact) if inside else {},
        )
        verdict = covers(declared, ask, file_scope)
        if verdict == COVERED and any(covers(r, ask, file_scope) == COVERED for r in rivals):
            rivalled.add(fact.row.decision_ref)
        waiting.append((fact, verdict))

    covered = [fact for fact, verdict in waiting if verdict == COVERED]
    hinting = _hinting(workspace, service) if request.include_diagnostics else None
    evaluated, served = _evaluate_scoped(
        workspace,
        service,
        scope.base_path,
        covered,
        known=hinting.routes if hinting else None,
        tally=hinting.tally if hinting else None,
    )
    reached = {fact.row.decision_ref: (outcome, handler) for fact, outcome, handler in evaluated}
    placed: list[tuple[RequestFacts, str, str | None]] = []
    for fact, verdict in waiting:
        if verdict != COVERED:
            placed.append((fact, verdict, None))
        elif fact.row.decision_ref in rivalled:
            placed.append((fact, "ambiguous", None))
        else:
            outcome, handler = reached.get(fact.row.decision_ref, ("no-matching-route", None))
            placed.append((fact, outcome, handler))
    # What the scope reaches first, then the rest of the group, each in the worklist's order.
    placed.sort(key=lambda item: item[1] == OUT_OF_SCOPE)
    counts = Counter(outcome for _, outcome, _ in placed)
    if served == 0:
        requirements.append(
            f"{service.slug} serves no route this index holds: it is not an indexed service"
        )
    elif not counts.get("would-link"):
        requirements.append(
            "no request this scope covers matches a route this service serves: nothing would be "
            "joined"
        )
    # The group is the base's, so the words must name it: the scope is read from the group, never
    # from what the request says about it.
    if asked.base_key != base_key(expression[1:-1]):
        requirements.append("the base key does not name the base expression of this group")
    if asked.base_shown is not None and asked.base_shown != shown:
        requirements.append(f"the base of this group is shown as {shown}")

    duplicate_of = conflicts_with = None
    for link in links:
        held = link.scope
        if held is None or link.callers != [asked.caller]:
            continue
        same = link.repository == service.slug and held == scope
        if same and link.environment == request.environment:
            duplicate_of = link.id
        elif (
            (held.base_key, held.path) == (scope.base_key, scope.path)
            and symbols_overlap(held.symbol, scope.symbol)
            and (link.repository, held.base_path) != (service.slug, scope.base_path)
            and conflicts_with is None
        ):
            conflicts_with = link.id
    if conflicts_with is not None:
        requirements.append(
            f"declaration {conflicts_with} already states another service behind {shown} in "
            f"{scope.path} for {asked.caller}; revoke it first"
        )
    for outcome, text in _SCOPED_WARNINGS:
        if counts.get(outcome):
            warnings.append(f"{counts[outcome]} request(s) {text}")

    rows = [
        PreviewRow(
            decision_ref=fact.row.decision_ref,
            method=fact.row.method or None,
            route=safe_route(fact.row.url),
            site=ConnectionSite(
                repository=fact.repository,
                path=fact.row.path or "",
                line_start=fact.row.line_start,
                line_end=fact.row.line_end,
            ),
            outcome=outcome,  # type: ignore[arg-type]
            handler=handler,
        )
        for fact, outcome, handler in placed
    ]
    hints = (
        _provider_diagnostics(
            workspace,
            service,
            hinting,
            [item for item in placed if item[0].row.decision_ref in reached],
            served,
            scope.base_path,
        )
        if hinting is not None
        else None
    )
    return HttpConnectionPreviewResponse(
        context=request.context,
        group=request.group,
        repository=service.slug,
        scope=scope,
        routes_served=served,
        counts=dict(sorted(counts.items())),
        rows=tuple(rows[: request.limit]),
        has_more=len(rows) > request.limit,
        requirements=tuple(dict.fromkeys(requirements)),
        warnings=tuple(warnings),
        duplicate_of=duplicate_of,
        conflicts_with=conflicts_with,
        can_create=not requirements,
        provider_diagnostics=hints,
    )


def preview(
    workspace: Workspace, request: HttpConnectionPreviewRequest
) -> HttpConnectionPreviewResponse:
    """What declaring ``origin`` is served by one repository would do, as the sync will decide.

    Exact for the handlers (the repository's own plan, the join's own matching) and for the
    callers it applies to; nothing is stored. With a ``scope`` it is a base the source does not
    state, and :func:`_preview_scoped` answers."""
    facts, context = _snapshot(workspace)
    members = sorted((f for f in facts if group_key(f.target) == request.group), key=_order)
    if not members:
        raise InvalidDeploymentLinkError(
            "no request has that target",
            detail="The worklist is rebuilt by every sync; reload it and choose again.",
        )
    if request.scope is not None:
        return _preview_scoped(workspace, request, facts, members, context)
    origin = members[0].target.origin
    if origin is None:
        raise InvalidDeploymentLinkError(
            "these requests do not state their origin",
            detail="Only a stated origin can be mapped; a base the source does not state is not.",
        )
    repository = next(
        (r for r in workspace.repositories.list_all() if r.slug == request.repository), None
    )
    if repository is None or repository.id is None or repository.slug == DECLARATIONS_SLUG:
        raise InvalidDeploymentLinkError(
            "that repository is not indexed",
            detail="A mapping names an indexed service: one this index holds routes of.",
        )
    url = origin + request.base_path
    requirements: list[str] = []
    warnings: list[str] = []
    fields: DeploymentLinkFields | None = None
    try:
        fields = DeploymentLinkFields(
            label="mapped from the HTTP Connections worklist",
            url=url,
            environment=request.environment,
            callers=list(request.callers),
        )
    except ValidationError as exc:
        requirements.extend(str(err["msg"]).removeprefix("Value error, ") for err in exc.errors())
    # An origin classified as an external service is not mapped to an indexed one until the
    # classification is revoked; with none classified this adds nothing.
    classified = classifications(context["external"]).get(origin)  # type: ignore[arg-type]
    if classified is not None:
        requirements.append(f"{origin} is {classified_text(classified.id)}")

    hinting = _hinting(workspace, repository) if request.include_diagnostics else None
    evaluated, served = _evaluate(
        workspace,
        repository,
        url,
        request.callers,
        request.environment,
        [f for f in members if f.target.origin == origin],
        known=hinting.routes if hinting else None,
        tally=hinting.tally if hinting else None,
    )
    if served == 0:
        requirements.append(
            f"{repository.slug} serves no route this index holds: it is not an indexed service"
        )
    rows: list[PreviewRow] = []
    counts: Counter[str] = Counter()
    for fact, outcome, handler in evaluated:
        counts[outcome] += 1
        rows.append(
            PreviewRow(
                decision_ref=fact.row.decision_ref,
                method=fact.row.method or None,
                route=safe_route(fact.row.url),
                site=ConnectionSite(
                    repository=fact.repository,
                    path=fact.row.path or "",
                    line_start=fact.row.line_start,
                    line_end=fact.row.line_end,
                ),
                outcome=outcome,  # type: ignore[arg-type]
                handler=handler,
            )
        )
    if served and not counts.get("would-link"):
        requirements.append(
            "no request of this group matches a route this service serves: nothing would be joined"
        )

    duplicate_of = conflicts_with = None
    if fields is not None:
        for link in context["links"]:  # type: ignore[attr-defined]
            if link.repository == repository.slug and _same(link, fields):
                duplicate_of = link.id
            elif (
                link.repository != repository.slug
                and link.url
                and declared_base(link.url) == declared_base(url)
                and _overlap(link.callers, fields.callers)
            ):
                conflicts_with = link.id
    if conflicts_with is not None:
        requirements.append(
            f"declaration {conflicts_with} already states another service at this place for the "
            "same callers; revoke it first"
        )
    if is_loopback_origin(origin):
        warnings.append(
            "A loopback origin names a different program in each repository's dev setup; this "
            "declaration applies only to the callers named."
        )
    if counts.get("ambiguous"):
        warnings.append(
            f"{counts['ambiguous']} request(s) match more than one handler and would stay refused"
        )
    if counts.get("caller-excluded"):
        warnings.append(
            f"{counts['caller-excluded']} request(s) are made by repositories this declaration "
            "does not name"
        )
    hints = (
        _provider_diagnostics(workspace, repository, hinting, evaluated, served, request.base_path)
        if hinting is not None
        else None
    )
    return HttpConnectionPreviewResponse(
        context=request.context,
        group=request.group,
        origin=origin,
        url=url,
        repository=repository.slug,
        routes_served=served,
        counts=dict(sorted(counts.items())),
        rows=tuple(rows[: request.limit]),
        has_more=len(rows) > request.limit,
        requirements=tuple(dict.fromkeys(requirements)),
        warnings=tuple(warnings),
        duplicate_of=duplicate_of,
        conflicts_with=conflicts_with,
        can_create=not requirements,
        provider_diagnostics=hints,
    )


def _same(link: DeploymentLink, fields: DeploymentLinkFields) -> bool:
    return (
        link.url == fields.url
        and link.environment == fields.environment
        and sorted(link.callers) == sorted(fields.callers)
        and link.target_id is None
        and not link.variables
        and not link.conditions
    )


def _overlap(first: Sequence[str], second: Sequence[str]) -> bool:
    return not first or not second or bool(set(first) & set(second))


def history(
    workspace: Workspace, request: HttpDeclarationHistoryRequest
) -> HttpDeclarationHistoryResponse:
    entries = workspace.deployment_links.history(
        link_id=request.id, repository=request.repository, limit=request.limit
    )
    return HttpDeclarationHistoryResponse(
        context=request.context,
        entries=tuple(
            HistoryEntry(
                id=e.id,
                at=e.at,
                action=e.action,  # type: ignore[arg-type]
                subject_id=e.subject_id,
                repository=e.repository,
                via=e.via,  # type: ignore[arg-type]
                request_group=e.request_group,
                note=e.note,
                before=e.before,
                after=e.after,
            )
            for e in entries
        ),
    )


__all__ = ["DECLARABLE", "LINKED", "connections", "history", "preview"]
