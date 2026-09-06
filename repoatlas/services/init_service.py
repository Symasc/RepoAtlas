"""``repoatlas init`` (V1_SPEC §5)."""

from __future__ import annotations

from pathlib import Path

from repoatlas.config.settings import save_settings
from repoatlas.discovery.reconcile import plan_repository_set, register
from repoatlas.errors import RepoAtlasError
from repoatlas.models.reports import SyncReport
from repoatlas.services.workspace import Workspace


def initialize(
    workspace: Workspace, root: Path, *, config_path: Path | None = None
) -> tuple[SyncReport, int]:
    """Establish the managed root, then discover and index everything beneath it.

    The user never registers repositories by hand: discovery is the only path by
    which a repository becomes managed (V1_SPEC §5). What ``init`` owns is the
    *root* — membership of that root is re-derived by every ordinary ``sync``
    from the same plan this uses (:mod:`repoatlas.discovery.reconcile`), so a
    repository created later does not wait for someone to re-run this command.

    Discovery is recursive to the bottom of the tree; nothing is omitted for being
    deeply nested. Directories the operating system refuses are skipped and named
    in the report, because "no repositories there" and "could not look" are
    different claims and only one of them is true (V1_SPEC §5).

    Holds the same writer lock ``sync`` does, for the same database. ``init`` is a
    mutating operation on the shared knowledge base — it rewrites the repository
    set and rebuilds the global graph — so protecting only ``sync`` would leave the
    more destructive of the two unserialized (ARCHITECTURE.md §9).
    """
    resolved = root.expanduser().resolve()
    if not resolved.is_dir():
        raise RepoAtlasError(f"Not a directory: {root}")

    with workspace.writer_lock():
        plan = plan_repository_set(
            resolved, workspace.settings.discovery, workspace.repositories.list_all()
        )

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

        # Every discovered repository, not only the new ones: `init` reindexes the
        # whole root, and registering a repository resets its indexed state to
        # match (see `register`).
        managed = register(plan.discovered, workspace.repositories)

        report = engine.run(managed, scope="global", embed_repo_ids=None)

    return (
        report.model_copy(
            update={
                "repositories_added": tuple(candidate.slug for candidate in plan.added),
                "unreadable_paths": plan.unreadable,
            }
        ),
        len(plan.discovered),
    )
