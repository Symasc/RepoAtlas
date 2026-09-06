"""``repoatlas sync`` (V1_SPEC §6, §12)."""

from __future__ import annotations

from pathlib import Path

from repoatlas.discovery.reconcile import plan_repository_set, register
from repoatlas.models.context import ContextScope, RepositoryContext
from repoatlas.models.reports import SyncReport
from repoatlas.models.repository import Repository
from repoatlas.services.workspace import Workspace


def sync(workspace: Workspace, context: RepositoryContext, *, full: bool = False) -> SyncReport:
    """Reconcile the repository *set*, then the contents of every repository in it.

    Scope is decided by the shared resolver, never re-derived here. The link pass
    that closes the run is global even for a single-repository sync, because a
    change here can create or invalidate an edge over there (ARCHITECTURE.md §6) —
    which is also why a repository-scoped sync takes the *whole* writer lock: two
    runs over different repositories still rebuild one shared graph, so they are
    not independent writers (ARCHITECTURE.md §9).

    **Membership is part of what a sync synchronizes.** A repository created
    beneath the managed root after ``init`` used to stay invisible until someone
    re-ran initialization: this iterated the stored rows, found them all current,
    and said so. Rediscovery closes that, and it runs *inside* the writer lock —
    registration mutates repository rows, and two concurrent syncs must not race
    to create the same one.

    Reconciliation happens when the run **covers the managed root**, not on every
    run. ``repoatlas sync <repo>`` is a statement about one repository, and a
    repository-scoped run never touches another row — the same rule that governs
    which repositories a run may retire (ARCHITECTURE.md §9). Registering a
    repository there would create a row outside the requested scope and then not
    index it, which is the one outcome worse than not finding it: registered, with
    no facts, and counted as managed. ``status`` reports drift at every scope, so
    nothing goes unsaid; only the reconciling is narrowed.

    "Covers the managed root" is two cases, and the second is not a special case so
    much as the same one wearing different clothes. If the configured root is
    itself a managed Git repository, being at the root resolves to *that
    repository* rather than to global scope — the more specific answer, and
    deliberate (ARCHITECTURE.md §7). Global scope is then unreachable, so treating
    only global scope as covering the root would mean that in that configuration
    no ordinary sync ever reconciles again, and the defect this fixes would come
    straight back for anyone who ran ``git init`` on their workspace directory.
    Syncing the repository that *is* the managed root is syncing the managed root.
    """
    root = workspace.root
    with workspace.writer_lock():
        repositories: tuple[Repository, ...] = context.repositories
        added: tuple[str, ...] = ()
        unreadable = ()

        if root is not None and _covers_root(context, root):
            plan = plan_repository_set(
                root, workspace.settings.discovery, workspace.repositories.list_all()
            )
            # Only the new ones: `upsert` writes the indexed-state columns, so
            # re-registering an already-indexed repository would erase the record
            # of it having been indexed (see `register`).
            register(plan.added, workspace.repositories)
            # Re-read rather than splice, so the run's ordering is the one every
            # other command sees and a newly registered repository is indistinguish-
            # able from one that was there all along.
            repositories = tuple(workspace.repositories.list_all())
            added = tuple(candidate.slug for candidate in plan.added)
            unreadable = plan.unreadable

        report = workspace.sync_engine().run(
            repositories,
            full=full,
            scope=str(context.scope),
            embed_repo_ids=[r.id for r in repositories if r.id is not None],
        )

    return report.model_copy(update={"repositories_added": added, "unreadable_paths": unreadable})


def _covers_root(context: RepositoryContext, root: Path) -> bool:
    """Whether this run's scope is the managed root itself.

    Global scope is the ordinary case. The second is the root that is also a
    managed repository, where global scope cannot be reached at all — see
    :func:`sync`.
    """
    if context.scope is ContextScope.GLOBAL:
        return True
    primary = context.primary
    if primary is None:
        return False
    return primary.path.expanduser().resolve() == root.expanduser().resolve()
