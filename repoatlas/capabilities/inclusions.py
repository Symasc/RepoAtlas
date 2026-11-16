"""Inclusion declarations: which indexed repository an unsettled inclusion is.

The link pass decides every inclusion a repository states (repoatlas.graph.inclusions); one it
cannot settle -- no indexed repository at its path or with its URL, or more than one with the
URL -- is listed in the topology overview. A person settles it here, and the next sync applies
the declaration. A declaration is accepted only for an entry the index states, and never for
one its path or a remote already proves: it is a fallback, not an override.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from repoatlas.capabilities.errors import (
    InclusionDeclarationNotFoundError,
    InvalidInclusionDeclarationError,
)
from repoatlas.graph.inclusions import pin_in_sync
from repoatlas.graph.keys import repository_key
from repoatlas.models.impact import InclusionImpact
from repoatlas.models.inclusions import (
    INCLUSION_FAMILY,
    InclusionDeclaration,
    InclusionDeclarationCreateRequest,
    InclusionDeclarationDeleteRequest,
    InclusionDeclarationDeleteResponse,
    InclusionDeclarationError,
    InclusionDeclarationResponse,
    InclusionDeclarationsRequest,
    InclusionDeclarationsResponse,
)
from repoatlas.models.parsing import EdgeType
from repoatlas.services.workspace import Workspace
from repoatlas.storage.protocols import LinkDecisionRow

_Entry = tuple[str, str, str]


def _entries(workspace: Workspace) -> dict[_Entry, list[LinkDecisionRow]]:
    """Every stated inclusion's decisions, by (including repository, file, path)."""
    slugs = {r.id: r.slug for r in workspace.repositories.list_all() if r.id is not None}
    found: dict[_Entry, list[LinkDecisionRow]] = defaultdict(list)
    for row in workspace.knowledge.list_link_decisions(INCLUSION_FAMILY):
        slug = slugs.get(row.repo_id)
        if slug is not None and row.path is not None:
            found[(slug, row.path, row.subject)].append(row)
    return found


def _evidence(row: LinkDecisionRow) -> dict[str, Any]:
    loaded = json.loads(row.evidence_json or "{}")
    return loaded if isinstance(loaded, dict) else {}


def _with_status(
    declaration: InclusionDeclaration, entries: dict[_Entry, list[LinkDecisionRow]]
) -> InclusionDeclaration:
    """What the last link pass made of a declaration. An entry the pass decided without
    reading the declaration was decided before it was made: ``None``, pending the next sync."""
    rows = entries.get((declaration.repository, declaration.file, declaration.path))
    if not rows:
        return declaration.model_copy(update={"status": "stale"})
    for row in rows:
        read = _evidence(row).get("declaration") or {}
        if read.get("id") == declaration.id and read.get("target") == declaration.target:
            return declaration.model_copy(update={"status": read.get("status")})
    return declaration


def declarations(
    workspace: Workspace, request: InclusionDeclarationsRequest
) -> InclusionDeclarationsResponse:
    entries = _entries(workspace)
    return InclusionDeclarationsResponse(
        context=request.context,
        declarations=tuple(
            _with_status(d, entries) for d in workspace.inclusion_declarations.list_all()
        ),
    )


def create(
    workspace: Workspace, request: InclusionDeclarationCreateRequest
) -> InclusionDeclarationResponse:
    fields = request.declaration
    slugs = {r.slug for r in workspace.repositories.list_all() if r.slug}
    if fields.repository not in slugs:
        raise InvalidInclusionDeclarationError(
            f"{fields.repository!r} is not an indexed repository"
        )
    if fields.target not in slugs:
        raise InvalidInclusionDeclarationError(
            f"{fields.target!r} is not an indexed repository",
            detail="Declare a repository the index holds; index it first if it is missing.",
        )
    if fields.target == fields.repository:
        raise InvalidInclusionDeclarationError("a repository does not include itself")
    rows = _entries(workspace).get((fields.repository, fields.file, fields.path))
    if not rows:
        raise InvalidInclusionDeclarationError(
            f"{fields.repository}/{fields.file} states no inclusion at {fields.path!r}",
            detail="Sync first if the file changed; the entry must be one the index reads.",
        )
    for row in rows:
        evidence = _evidence(row)
        if row.decision == "linked" and evidence.get("method") in ("path", "remote"):
            raise InvalidInclusionDeclarationError(
                f"the inclusion at {fields.path!r} is already linked to "
                f"{evidence.get('target')} by its {evidence.get('method')}",
                detail="A declaration settles what the index cannot, and never overrides it.",
            )
    try:
        stored = workspace.inclusion_declarations.create(fields, provenance=request.provenance)
    except InclusionDeclarationError as exc:
        raise InvalidInclusionDeclarationError(str(exc)) from exc
    return InclusionDeclarationResponse(context=request.context, declaration=stored)


def delete(
    workspace: Workspace, request: InclusionDeclarationDeleteRequest
) -> InclusionDeclarationDeleteResponse:
    if workspace.inclusion_declarations.get(request.id) is None:
        raise InclusionDeclarationNotFoundError()
    return InclusionDeclarationDeleteResponse(
        context=request.context,
        id=request.id,
        deleted=workspace.inclusion_declarations.delete(request.id, provenance=request.provenance),
    )


def includers(
    workspace: Workspace, repository: str, in_scope: frozenset[str]
) -> tuple[InclusionImpact, ...]:
    """The repositories in scope that include ``repository``, read from the stored
    ``INCLUDES_REPOSITORY`` edges arriving at it. Two reads, whatever the count."""
    targets = workspace.knowledge.get_entities_by_key([repository_key(repository)])
    edges = workspace.topology().stored_edges_to(
        [entity.entity_id for entity in targets], EdgeType.INCLUDES_REPOSITORY
    )
    head = next(
        (r.head_sha for r in workspace.repositories.list_all() if r.slug == repository), None
    )
    found: list[InclusionImpact] = []
    for edge in edges:
        including = edge.repository_slug
        if including is None or including not in in_scope or edge.path is None:
            continue
        recorded = json.loads(edge.resolution or "{}").get("inclusion") or {}
        revision = recorded.get("revision")
        found.append(
            InclusionImpact(
                repository=including,
                included=repository,
                declared_at=f"{including}/{edge.path}:{edge.line_start or 1}",
                path=str(recorded.get("path") or ""),
                format=str(recorded.get("format") or ""),
                revision=revision,
                in_sync=pin_in_sync(revision, head),
            )
        )
    return tuple(sorted(found, key=lambda i: (i.repository, i.declared_at)))
