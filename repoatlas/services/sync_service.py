"""``repoatlas sync`` (V1_SPEC §6, §12)."""

from __future__ import annotations

from repoatlas.models.context import RepositoryContext
from repoatlas.models.reports import SyncReport
from repoatlas.services.workspace import Workspace


def sync(workspace: Workspace, context: RepositoryContext, *, full: bool = False) -> SyncReport:
    """Sync exactly the repositories the resolved context names.

    Scope is decided by the shared resolver, never re-derived here.
    """
    engine = workspace.sync_engine()
    reports = []
    for repo in context.repositories:
        engine.ensure_repository_entity(repo)
        reports.append(engine.sync_repository(repo, full=full))
    return SyncReport(repositories=tuple(reports), scope=str(context.scope))
