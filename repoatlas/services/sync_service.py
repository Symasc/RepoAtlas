"""``repoatlas sync`` (V1_SPEC §6, §12)."""

from __future__ import annotations

from repoatlas.models.context import RepositoryContext
from repoatlas.models.reports import SyncReport
from repoatlas.services.workspace import Workspace


def sync(workspace: Workspace, context: RepositoryContext, *, full: bool = False) -> SyncReport:
    """Sync exactly the repositories the resolved context names.

    Scope is decided by the shared resolver, never re-derived here. The link pass
    that closes the run is global even for a single-repository sync, because a
    change here can create or invalidate an edge over there (ARCHITECTURE.md §6) —
    which is also why a repository-scoped sync takes the *whole* writer lock: two
    runs over different repositories still rebuild one shared graph, so they are
    not independent writers (ARCHITECTURE.md §9).
    """
    with workspace.writer_lock():
        return workspace.sync_engine().run(
            context.repositories,
            full=full,
            scope=str(context.scope),
            embed_repo_ids=[r.id for r in context.repositories if r.id is not None],
        )
