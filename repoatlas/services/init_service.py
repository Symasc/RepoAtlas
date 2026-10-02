"""``repoatlas init`` (V1_SPEC §5)."""

from __future__ import annotations

from pathlib import Path

from repoatlas.config.settings import save_settings
from repoatlas.discovery.reconcile import (
    RepositorySetPlan,
    plan_repository_set,
    register,
)
from repoatlas.errors import RepoAtlasError, RepositoryRootConflictError
from repoatlas.models.reports import SyncReport
from repoatlas.services.workspace import Workspace
from repoatlas.sync.progress import SILENT, SyncProgress


def initialize(
    workspace: Workspace,
    root: Path,
    *,
    config_path: Path | None = None,
    progress: SyncProgress | None = None,
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

    ``progress`` hears how far discovery and the run have got, and nothing else.
    """
    listener = progress if progress is not None else SILENT
    resolved = root.expanduser().resolve()
    if not resolved.is_dir():
        raise RepoAtlasError(f"Not a directory: {root}")

    with workspace.writer_lock():
        listener.discovering()
        plan = plan_repository_set(
            resolved, workspace.settings.discovery, workspace.repositories.list_all()
        )
        # Before anything is written, and before the configured root is touched:
        # a refusal that had already moved the root would leave the workspace
        # pointing somewhere its repositories are not (ARCHITECTURE.md §3).
        if plan.conflicts:
            raise _conflict(plan)

        # In memory only. The engine derives slugs for pre-v5 rows from the root,
        # so it has to see the new one; the *file* is not rewritten until the
        # database agrees with it.
        settings = workspace.settings.model_copy(
            update={"repos": workspace.settings.repos.model_copy(update={"root": resolved})}
        )
        workspace.settings = settings
        engine = workspace.sync_engine()

        # One transaction for the whole repository set. A root move rewrites every
        # row, and a half-applied move is a workspace split between two roots —
        # so either all of them relocate or none does (ARCHITECTURE.md §3).
        with workspace.database.transaction():
            for relocation in plan.relocated:
                assert relocation.repository.id is not None
                workspace.repositories.relocate(relocation.repository.id, relocation.candidate.path)

            # Discovery is the only way a repository becomes managed, so it is also
            # the only authority on which ones still exist. A repository that has
            # been moved or deleted is forgotten here rather than left behind as a
            # stale identity that evidence could still be attributed to (V1_SPEC
            # §5). Read back *after* relocation: a relocated row is at a path that
            # exists again, and retiring it here would delete the row the
            # relocation just carried across.
            for stored in workspace.repositories.list_all():
                engine.forget_if_missing(stored)

            # Every discovered repository, not only the new ones: `init` reindexes
            # the whole root, and registering a repository resets its indexed state
            # to match (see `register`). A relocated row is now keyed at its new
            # path, so this updates it in place instead of inserting a duplicate.
            managed = register(plan.discovered, workspace.repositories)

        # Only now, with the repository set committed at the new root. The two
        # writes cannot share a transaction — one is a YAML file, the other is
        # SQLite — so the question is only which failure is survivable, and this
        # order picks the milder one. If this write fails, the rows are at the new
        # paths while the configuration still names the old root: every one of
        # those paths exists, so nothing reads as missing and no later command
        # retires anything, and the next `init` at the new root matches them all
        # by path and simply rewrites the configuration. The opposite order is the
        # one that fails badly, and it is what produced the reported crash's
        # aftermath — configuration moved, rows not.
        save_settings(settings, config_path)

        report = engine.run(managed, scope="global", progress=listener)

    return (
        report.model_copy(
            update={
                "repositories_added": tuple(candidate.slug for candidate in plan.added),
                "unreadable_paths": plan.unreadable,
            }
        ),
        len(plan.discovered),
    )


def _conflict(plan: RepositorySetPlan) -> RepositoryRootConflictError:
    """Refuse a root change that two live repositories would answer to.

    Named in full — both paths, and the identity they are competing for — because
    the operator is the only one who knows which of the two directories is the
    workspace they meant, and a message that withheld either would not let them
    decide.
    """
    lines = [
        f"  {conflict.candidate.slug}\n"
        f"    registered: {conflict.registered.path}\n"
        f"    discovered: {conflict.candidate.path}"
        for conflict in plan.conflicts
    ]
    listed = "\n".join(lines)
    return RepositoryRootConflictError(
        f"Cannot initialize {plan.root}: it would give one identity to two repositories.",
        detail=(
            "These repositories are already registered at a different path, and "
            "that path still exists:\n\n"
            f"{listed}\n\n"
            "RepoAtlas identifies a repository by its path relative to the managed "
            "root, so both directories claim the same identity. A repository that "
            "had simply moved would have left its old location behind; these have "
            "not, so this is a second workspace rather than a relocation, and "
            "RepoAtlas will not guess which one you meant.\n\n"
            "Nothing has been changed. Either remove the old location, or keep the "
            "two workspaces apart by pointing REPOATLAS_DB at a separate index:\n\n"
            "  REPOATLAS_DB=<other-index> repoatlas init <directory>"
        ),
    )
