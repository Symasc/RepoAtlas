"""Repository-set reconciliation: what is managed, versus what is there (V1_SPEC §5).

``init`` used to be the only command that looked at the filesystem for
repositories. Everything afterwards read the ``repositories`` table and believed
it. So a repository created beneath the managed root after initialization was
invisible: ``sync`` iterated the stored rows and found nothing to do, ``status``
compared those same rows against their own working trees and reported the
workspace *current*, and ``locate`` answered "no indexed evidence matches" about
a repository sitting in the managed tree. Only an operator who already knew to
re-run ``init`` could recover — which requires knowing the thing the tool was
supposed to tell them.

The membership of the managed root is not a snapshot taken once. It is a fact
about the filesystem, and it is re-derived the same way every time, by the one
hardened walk in :mod:`repoatlas.discovery.finder`: recursive to any depth,
honouring the exclusions, ``.repoatlasignore``, nested repositories, ``.git``
files as well as directories, the symlink policy, containment, cycle detection
and unreadable subtrees. Nothing here re-implements a simplified version of that;
a second walker would be a second set of answers to the same question.

Three commands, one plan:

* ``init``   — take the plan and register everything it discovered;
* ``sync``   — take the plan, register what is new, then index the whole set;
* ``status`` — take the plan and *only look at it*.

:func:`plan_repository_set` writes nothing, which is what lets ``status`` use it.
:func:`register` is the only function here that touches the database, and its
callers hold the writer lock.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from repoatlas.config.settings import DiscoverySettings
from repoatlas.discovery.finder import discover_repositories
from repoatlas.models.repository import Repository, RepositoryCandidate
from repoatlas.storage.sqlite.stores import SqliteRepositoryStore


def _canonical(path: Path) -> Path:
    """The form two paths must share to be the same directory.

    Stored rows carry resolved paths, and discovery resolves its root before it
    descends — but a database written by an older build, or a root configured
    through a symlink, may not agree textually about a directory both sides can
    see. Comparing canonical forms is how the diff stays a statement about
    directories rather than about spelling.
    """
    return path.expanduser().resolve()


@dataclass(frozen=True, slots=True)
class RepositorySetPlan:
    """What discovery found, against what is registered. Purely descriptive."""

    root: Path
    #: Every repository the current walk found, in discovery's own deterministic
    #: order (sorted by path).
    discovered: tuple[RepositoryCandidate, ...] = ()
    #: Discovered, not registered. ``sync`` registers and indexes these; ``status``
    #: reports them as the reason the workspace is not current.
    added: tuple[RepositoryCandidate, ...] = ()
    #: Registered and still discovered.
    existing: tuple[Repository, ...] = ()
    #: Registered, and the directory is **gone**.
    #:
    #: Deliberately narrower than "registered but not discovered". A repository
    #: whose ``.git`` was deleted while its files remain is still registered, by
    #: both ``init`` and ``sync``, because a repository's path is its identity and
    #: that path still exists (ARCHITECTURE.md §3). Calling it missing here would
    #: describe a removal that neither command performs. This set is exactly what
    #: :meth:`~repoatlas.sync.engine.SyncEngine.forget_if_missing` will retire on
    #: the next run, so the report and the behaviour cannot drift apart.
    missing: tuple[Repository, ...] = ()
    #: Directories the operating system refused. "We found nothing there" and "we
    #: were not allowed to look" are different claims (V1_SPEC §5).
    unreadable: tuple[Path, ...] = ()

    @property
    def drifted(self) -> bool:
        """Whether the registered set no longer describes the managed root."""
        return bool(self.added or self.missing)


def plan_repository_set(
    root: Path,
    settings: DiscoverySettings,
    registered: Sequence[Repository],
) -> RepositorySetPlan:
    """Diff the managed root against the registered set. **Reads only.**

    Writing nothing is a contract, not an implementation detail: ``status`` calls
    this, and a read command that quietly registered repositories would make
    "check whether anything changed" a mutating operation — indistinguishable, to
    anyone reading a terminal, from having run a sync (V1_SPEC §4).
    """
    unreadable: list[Path] = []
    discovered = discover_repositories(root, settings, unreadable=unreadable)

    found = {_canonical(candidate.path): candidate for candidate in discovered}
    by_path = {_canonical(repo.path): repo for repo in registered}

    added = tuple(candidate for path, candidate in found.items() if path not in by_path)
    existing = tuple(repo for path, repo in by_path.items() if path in found)
    missing = tuple(
        repo for path, repo in by_path.items() if path not in found and not repo.path.is_dir()
    )

    return RepositorySetPlan(
        root=root,
        discovered=tuple(discovered),
        added=added,
        existing=existing,
        missing=missing,
        unreadable=tuple(unreadable),
    )


def register(
    candidates: Sequence[RepositoryCandidate], repositories: SqliteRepositoryStore
) -> list[Repository]:
    """Persist candidates as managed repositories, and read them back.

    Only ever called with candidates the caller means to *index in the same run*.
    A :class:`~repoatlas.models.repository.Repository` built from a candidate
    carries no indexed state — no ``last_indexed_at``, ``last_sync_status``
    ``NEVER`` — and ``upsert`` writes those columns, so registering a repository
    that is already indexed would erase the record of it having been indexed.
    That is correct for ``init``, which reindexes everything it discovers, and
    would be data loss in ``sync``, which is why ``sync`` passes only
    :attr:`RepositorySetPlan.added`.

    The caller must hold the writer lock.
    """
    managed: list[Repository] = []
    for candidate in candidates:
        repositories.upsert(
            Repository(name=candidate.name, slug=candidate.slug, path=candidate.path)
        )
        stored = repositories.get_by_path(candidate.path)
        assert stored is not None
        managed.append(stored)
    return managed
