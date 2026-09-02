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
    # Embedding runs last and only over what changed. It is the one stage allowed
    # to be unavailable without failing the sync (V1_SPEC §14).
    embedding = engine.embed_pending([r.id for r in context.repositories if r.id is not None])

    return SyncReport(
        repositories=tuple(reports),
        scope=str(context.scope),
        linked_references=link.resolved,
        unresolved_references=link.unresolved,
        cross_repository_edges=link.cross_repository,
        embedded_chunks=embedding.embedded,
        pending_embeddings=embedding.pending,
        embedding_detail=embedding.detail,
        link_trace=link.trace,
    )
