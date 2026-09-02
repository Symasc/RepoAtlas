"""``repoatlas sync`` (V1_SPEC §6, §12)."""

from __future__ import annotations

from repoatlas.models.context import RepositoryContext
from repoatlas.models.reports import SyncReport
from repoatlas.services.workspace import Workspace


def sync(workspace: Workspace, context: RepositoryContext, *, full: bool = False) -> SyncReport:
    """Sync exactly the repositories the resolved context names.

    Scope is decided by the shared resolver, never re-derived here. The link pass
    that follows is global even for a single-repository sync, because a change
    here can create or invalidate an edge over there (ARCHITECTURE.md §6).
    """
    engine = workspace.sync_engine()
    reports = []
    for repo in context.repositories:
        engine.ensure_repository_entity(repo)
        reports.append(engine.sync_repository(repo, full=full))

    link = engine.link_global()
    return SyncReport(
        repositories=tuple(reports),
        scope=str(context.scope),
        linked_references=link.resolved,
        unresolved_references=link.unresolved,
        cross_repository_edges=link.cross_repository,
    )
