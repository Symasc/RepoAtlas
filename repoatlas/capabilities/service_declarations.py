"""External-service classification: preview, create, update, revoke and history.

A person says an origin no indexed repository answers is a service outside the index. The
classification is stored (``service_declarations``, kept apart from the deployment links so an
external origin never becomes a deployment hostname) and applied **when read**
(:mod:`repoatlas.capabilities.http_connections`): a request the join left ``http-service-unmatched``
at that origin is shown as a known external dependency, and its decision, reason, chain and edges
are the join's, untouched. Nothing is joined, so a sync neither reads nor writes any of it.

The preview is the read-only question and the write is its answer: a create or update re-runs the
preview inside one read snapshot and stores nothing while it has a requirement
(:func:`repoatlas.graph.service_declarations.classification_check` is the one rule both use).
"""

from __future__ import annotations

from collections.abc import Mapping

from repoatlas.capabilities.errors import DeploymentLinkNotFoundError, InvalidDeploymentLinkError
from repoatlas.capabilities.http_connections import _externals, _order, _snapshot
from repoatlas.graph.http_crossings import SERVICE_UNMATCHED
from repoatlas.graph.http_declarations import RequestFacts, group_key, safe_route
from repoatlas.graph.service_declarations import (
    IDENTIFIED,
    KNOWN_EXTERNAL,
    classification_check,
    classifications,
    identity_of,
)
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.http_connections import ConnectionSite
from repoatlas.models.service_declaration_io import (
    ServiceDeclarationCreateRequest,
    ServiceDeclarationDeleteRequest,
    ServiceDeclarationDeleteResponse,
    ServiceDeclarationHistoryRequest,
    ServiceDeclarationHistoryResponse,
    ServiceDeclarationPreviewRequest,
    ServiceDeclarationPreviewResponse,
    ServiceDeclarationPreviewRow,
    ServiceDeclarationResponse,
    ServiceDeclarationUpdateRequest,
    ServiceEffectCounts,
    ServiceHistoryEntry,
)
from repoatlas.models.service_declarations import (
    ExternalServiceFields,
    ServiceDeclarationConflictError,
    ServiceDeclarationFields,
    ServiceDeclarationIdentityError,
    ServiceDeclarationLimitError,
    ServiceDeclarationView,
)
from repoatlas.services.workspace import Workspace


def external_fields(fields: ServiceDeclarationFields) -> ExternalServiceFields:
    """The fields of an external service: the one kind this capability states."""
    if not isinstance(fields, ExternalServiceFields):
        raise InvalidDeploymentLinkError(
            "only an external service can be declared here",
            detail="Redirects and permitted callbacks are not served yet.",
        )
    return fields


def _not_found() -> DeploymentLinkNotFoundError:
    return DeploymentLinkNotFoundError(
        "no service declaration has that id",
        detail="List the service declarations for current ids.",
    )


def _row(
    fact: RequestFacts, known: Mapping[str, int], *, applies: bool
) -> ServiceDeclarationPreviewRow:
    """One request at the declared origin: its identity now, and once the declaration stands.
    Nothing changes while the declaration cannot be stored (``applies`` false)."""
    before = identity_of(fact, known)[0]
    after = KNOWN_EXTERNAL if applies and fact.status == SERVICE_UNMATCHED else before
    return ServiceDeclarationPreviewRow(
        decision_ref=fact.row.decision_ref,
        method=fact.row.method or None,
        route=safe_route(fact.row.url),
        site=ConnectionSite(
            repository=fact.repository,
            path=fact.row.path or "",
            line_start=fact.row.line_start,
            line_end=fact.row.line_end,
        ),
        identity_before=before,
        identity_after=after,
    )


def preview(
    workspace: Workspace, request: ServiceDeclarationPreviewRequest
) -> ServiceDeclarationPreviewResponse:
    """What classifying an origin would do to each request there, before anything is stored.

    Read-only. The rule is the one the write applies: no stored decision at the origin may be other
    than ``http-service-unmatched``, no deployment link may state it, it may not name this machine
    and it may not be classified in other words; each repository it authenticates for must be an
    indexed one. With a ``group`` the origin must be that group's and one of its requests must be
    unmatched. Every refusal is listed and none is resolved."""
    fields = external_fields(request.fields)
    facts, context = _snapshot(workspace)
    externals = _externals(facts, context)
    requirements: list[str] = []
    if request.group is not None:
        members = [fact for fact in facts if group_key(fact.target) == request.group]
        if not members:
            raise InvalidDeploymentLinkError(
                "no request has that target",
                detail="The worklist is rebuilt by every sync; reload it and choose again.",
            )
        if members[0].target.origin != fields.origin:
            requirements.append("the origin is not the origin of the group it was made from")
        elif not any(fact.status == SERVICE_UNMATCHED for fact in members):
            requirements.append(
                f"no request of this group is {SERVICE_UNMATCHED}: there is nothing to classify"
            )
    check = classification_check(
        fields,
        facts,
        context["links"],  # type: ignore[arg-type]
        classifications(context["external"]),  # type: ignore[arg-type]
        own_id=request.id,
        indexed=context["indexed"],  # type: ignore[arg-type]
    )
    requirements.extend(check.requirements)
    requirements = list(dict.fromkeys(requirements))

    applies = not requirements
    at_origin = sorted(
        (fact for fact in facts if fact.target.origin == fields.origin),
        key=lambda fact: (fact.status != SERVICE_UNMATCHED, _order(fact)),
    )
    rows = [_row(fact, externals.known, applies=applies) for fact in at_origin]
    counts = ServiceEffectCounts(
        would_be_known_external=sum(1 for row in rows if row.identity_after == KNOWN_EXTERNAL),
        stay_identified=sum(1 for row in rows if row.identity_before == IDENTIFIED),
        redirect_steps_added=0,
        requests_reached=len(rows),
    )
    return ServiceDeclarationPreviewResponse(
        context=request.context,
        group=request.group,
        kind="external_service",
        subject=fields.origin,
        counts=counts,
        rows=tuple(rows[: request.limit]),
        has_more=len(rows) > request.limit,
        requirements=tuple(requirements),
        duplicate_of=check.duplicate_of,
        conflicts_with=check.conflicts_with,
        can_create=applies,
    )


def require_declarable(
    workspace: Workspace,
    fields: ExternalServiceFields,
    *,
    group: str | None,
    own_id: int | None = None,
) -> None:
    """Re-run the preview inside one read snapshot and refuse on its first requirement.

    The check is against the latest committed index. Within one service process nothing
    interleaves (the call lock). Across processes a sync can commit between the check and the
    write; that is acceptable because the classification is read, never joined, so the worklist
    then shows it ineffective or invalidated rather than wrong."""
    with workspace.database.read_snapshot():
        checked = preview(
            workspace,
            ServiceDeclarationPreviewRequest(
                context=CapabilityContext(scope=CapabilityScope.GLOBAL),
                fields=fields,
                group=group,
                id=own_id,
            ),
        )
    if checked.requirements:
        raise InvalidDeploymentLinkError(checked.requirements[0])


def declaration_view(workspace: Workspace, declaration_id: int) -> ServiceDeclarationView:
    """A stored classification and where it stands against the stored decisions."""
    with workspace.database.read_snapshot():
        facts, context = _snapshot(workspace)
        externals = _externals(facts, context)
    found = next((view for view in externals.views if view.id == declaration_id), None)
    if found is None:
        raise _not_found()
    return found


def create(
    workspace: Workspace, request: ServiceDeclarationCreateRequest
) -> ServiceDeclarationResponse:
    """Classify an origin. Stating what is already stated returns that classification."""
    fields = external_fields(request.fields)
    group = request.provenance.request_group if request.provenance is not None else None
    require_declarable(workspace, fields, group=group)
    try:
        declaration = workspace.service_declarations.create(fields, provenance=request.provenance)
    except (ServiceDeclarationConflictError, ServiceDeclarationLimitError) as exc:
        raise InvalidDeploymentLinkError(str(exc)) from exc
    return ServiceDeclarationResponse(
        context=request.context, declaration=declaration_view(workspace, declaration.id)
    )


def update(
    workspace: Workspace, request: ServiceDeclarationUpdateRequest
) -> ServiceDeclarationResponse:
    """Replace what a stored classification states. What it is about (its kind and origin) cannot
    change, and the write checks are run again for the origin as it stands now."""
    store = workspace.service_declarations
    current = store.get(request.id)
    if current is None:
        raise _not_found()
    if current.fields.kind != request.fields.kind or (
        current.identity_key != request.fields.identity_key
    ):
        raise InvalidDeploymentLinkError(str(ServiceDeclarationIdentityError()))
    fields = external_fields(request.fields)
    group = request.provenance.request_group if request.provenance is not None else None
    require_declarable(workspace, fields, group=group, own_id=request.id)
    try:
        declaration = store.update(request.id, fields, provenance=request.provenance)
    except ServiceDeclarationIdentityError as exc:
        raise InvalidDeploymentLinkError(str(exc)) from exc
    if declaration is None:
        raise _not_found()
    return ServiceDeclarationResponse(
        context=request.context, declaration=declaration_view(workspace, declaration.id)
    )


def delete(
    workspace: Workspace, request: ServiceDeclarationDeleteRequest
) -> ServiceDeclarationDeleteResponse:
    """Revoke a declaration of any kind. What it stated stays in the history."""
    store = workspace.service_declarations
    if store.get(request.id) is None:
        raise _not_found()
    return ServiceDeclarationDeleteResponse(
        context=request.context,
        id=request.id,
        deleted=store.delete(request.id, provenance=request.provenance),
    )


def history(
    workspace: Workspace, request: ServiceDeclarationHistoryRequest
) -> ServiceDeclarationHistoryResponse:
    entries = workspace.service_declarations.history(
        declaration_id=request.id, kind=request.kind, limit=request.limit
    )
    return ServiceDeclarationHistoryResponse(
        context=request.context,
        entries=tuple(
            ServiceHistoryEntry(
                id=entry.id,
                at=entry.at,
                action=entry.action,  # type: ignore[arg-type]
                kind=entry.kind,
                subject_id=entry.subject_id,
                subject=entry.subject,
                via=entry.via,  # type: ignore[arg-type]
                request_group=entry.request_group,
                note=entry.note,
                before=entry.before,
                after=entry.after,
            )
            for entry in entries
        ),
    )


__all__ = [
    "create",
    "declaration_view",
    "delete",
    "external_fields",
    "history",
    "preview",
    "require_declarable",
    "update",
]
