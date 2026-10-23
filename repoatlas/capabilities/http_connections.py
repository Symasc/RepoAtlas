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
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone

from pydantic import ValidationError

from repoatlas.capabilities.errors import InvalidCursorError, InvalidDeploymentLinkError
from repoatlas.config.settings import DECLARATIONS_SLUG
from repoatlas.graph.http_crossings import (
    DECLARABLE,
    LINKED,
    declared_candidates,
    parse_exposure,
)
from repoatlas.graph.http_declarations import (
    RequestFacts,
    declaration_state,
    display_base,
    facts_of,
    group_key,
    is_loopback_origin,
    origin_of_link,
    safe_route,
)
from repoatlas.graph.link_exposures import plan_link_exposures
from repoatlas.graph.linker import GLOBAL_PARSER, repository_routes
from repoatlas.models import http
from repoatlas.models.deployment_targets import (
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
    PreviewRow,
)
from repoatlas.models.http_inventory import CrossingStatus
from repoatlas.models.repository import Repository
from repoatlas.services.workspace import Workspace

#: Samples a group lists inline; the rest are one expansion away.
SAMPLES = 3
#: Base paths tried per candidate service: the root, and up to this many leading literal segments.
MAX_BASE_DEPTH = 3
_CURSOR = "hcc1:"


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
    return facts, {
        "built": knowledge.http_call_decisions_built(),
        "pass_reads": knowledge.link_pass_reads(),
        "links": workspace.deployment_links.list_all(),
        "indexed": indexed,
        "served": served_by_slug,
    }


def _declarations(
    facts: Sequence[RequestFacts], context: Mapping[str, object]
) -> list[DeclarationView]:
    """Each declaration with its derived state, newest first."""
    links: list[DeploymentLink] = context["links"]  # type: ignore[assignment]
    indexed: set[str] = context["indexed"]  # type: ignore[assignment]
    served: Mapping[str, list[str]] = context["served"]  # type: ignore[assignment]
    pass_reads: Mapping[int, str] | None = context["pass_reads"]  # type: ignore[assignment]
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
            )
        )
    return views


def _request_view(fact: RequestFacts) -> ConnectionRequest:
    row = fact.row
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
    )


def _order(fact: RequestFacts) -> tuple[str, str, int, str]:
    return (fact.repository, fact.row.path or "", fact.row.line_start or 0, fact.row.decision_ref)


def connections(workspace: Workspace, request: HttpConnectionsRequest) -> HttpConnectionsResponse:
    facts, context = _snapshot(workspace)
    declarations = _declarations(facts, context)
    by_origin: dict[str, list[DeclarationView]] = defaultdict(list)
    for link, view in zip(
        sorted(context["links"], key=lambda item: -item.id),  # type: ignore[arg-type,attr-defined]
        declarations,
        strict=True,
    ):
        origin = origin_of_link(link)
        if origin is not None:
            by_origin[origin].append(view)

    linked = [f for f in facts if f.linked]
    open_facts = [f for f in facts if not f.linked]
    summary = ConnectionsSummary(
        requests=len(facts),
        linked_by_source=sum(1 for f in linked if not f.by_declaration),
        linked_by_declaration=sum(1 for f in linked if f.by_declaration),
        unresolved=len(open_facts),
        declarable=sum(1 for f in open_facts if f.declarable),
        base_not_stated=sum(1 for f in open_facts if f.target.origin is None),
        declarations=dict(sorted(Counter(d.state for d in declarations).items())),
        by_status=dict(sorted(Counter(f.status for f in open_facts).items())),
    )

    wanted = [
        f
        for f in (facts if request.include_resolved else open_facts)
        if (not request.status or f.status in {s.value for s in request.status} or f.linked)
        and (request.language is None or f.language == request.language)
        and (request.caller is None or f.repository == request.caller)
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
        views = tuple(by_origin.get(origin, ())) if origin else ()
        if request.declaration_state and not any(
            v.state == request.declaration_state for v in views
        ):
            continue
        unresolved = [m for m in members if not m.linked]
        if not unresolved and not request.include_resolved:
            continue
        ordered = sorted(unresolved or members, key=_order)
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
                samples=tuple(_request_view(m) for m in ordered[:SAMPLES]),
                declarations=views,
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
        expanded = tuple(_request_view(m) for m in page)
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
    prefixes: list[tuple[str, ...]] = [()]
    for member in wanted:
        literal: list[str] = []
        for segment in member.target.segments[:MAX_BASE_DEPTH]:
            if segment is None:
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


def _evaluate(
    workspace: Workspace,
    repository: Repository,
    url: str,
    callers: Sequence[str],
    environment: str | None,
    members: Sequence[RequestFacts],
) -> tuple[list[tuple[RequestFacts, str, str | None]], int]:
    """What a declaration of ``url`` for ``repository`` would do to each request: the repository's
    routes as the global pass sees them, the exposures the link would add, the join's own matching.
    Returns each request with its outcome and handler place, and how many routes it serves."""
    assert repository.id is not None
    routes = repository_routes(workspace.knowledge, repository.id)
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
    exposures = [
        e
        for e in (
            parse_exposure(claim)
            for claim in plan_link_exposures(
                [synthetic], {}, routes, {repository.slug: repository.id}
            )
        )
        if e is not None
    ]
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


def preview(
    workspace: Workspace, request: HttpConnectionPreviewRequest
) -> HttpConnectionPreviewResponse:
    """What declaring ``origin`` is served by one repository would do, as the sync will decide.

    Exact for the handlers (the repository's own plan, the join's own matching) and for the
    callers it applies to; nothing is stored."""
    facts, context = _snapshot(workspace)
    members = sorted((f for f in facts if group_key(f.target) == request.group), key=_order)
    if not members:
        raise InvalidDeploymentLinkError(
            "no request has that target",
            detail="The worklist is rebuilt by every sync; reload it and choose again.",
        )
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

    evaluated, served = _evaluate(
        workspace,
        repository,
        url,
        request.callers,
        request.environment,
        [f for f in members if f.target.origin == origin],
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
