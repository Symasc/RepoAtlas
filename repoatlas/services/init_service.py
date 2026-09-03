"""``repoatlas init`` (V1_SPEC §5)."""

from __future__ import annotations

from pathlib import Path

from repoatlas.config.settings import save_settings
from repoatlas.discovery.finder import discover_repositories
from repoatlas.errors import RepoAtlasError
from repoatlas.models.reports import SyncReport
from repoatlas.models.repository import Repository
from repoatlas.services.workspace import Workspace


def initialize(
    workspace: Workspace, root: Path, *, config_path: Path | None = None
) -> tuple[SyncReport, int]:
    """Discover every repository beneath ``root``, record it, and index it.

    The user never registers repositories by hand: discovery is the only path by
    which a repository becomes managed (V1_SPEC §5).

    Holds the same writer lock ``sync`` does, for the same database. ``init`` is a
    mutating operation on the shared knowledge base — it rewrites the repository
    set and rebuilds the global graph — so protecting only ``sync`` would leave the
    more destructive of the two unserialized (ARCHITECTURE.md §9).
    """
    resolved = root.expanduser().resolve()
    if not resolved.is_dir():
        raise RepoAtlasError(f"Not a directory: {root}")

    with workspace.writer_lock():
        candidates = discover_repositories(resolved, workspace.settings.discovery)

        settings = workspace.settings.model_copy(
            update={"repos": workspace.settings.repos.model_copy(update={"root": resolved})}
        )
        workspace.settings = settings
        save_settings(settings, config_path)

        engine = workspace.sync_engine()

        # Discovery is the only way a repository becomes managed, so it is also
        # the only authority on which ones still exist. A repository that has been
        # moved or deleted is forgotten here rather than left behind as a stale
        # identity that evidence could still be attributed to (V1_SPEC §5).
        for stored in workspace.repositories.list_all():
            engine.forget_if_missing(stored)

        managed: list[Repository] = []
        for candidate in candidates:
            workspace.repositories.upsert(
                Repository(name=candidate.name, slug=candidate.slug, path=candidate.path)
            )
            stored = workspace.repositories.get_by_path(candidate.path)
            assert stored is not None
            managed.append(stored)

        report = engine.run(managed, scope="global", embed_repo_ids=None)

    return report, len(candidates)
