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

from repoatlas.config.settings import DECLARATIONS_SLUG, DiscoverySettings, declarations_directory
from repoatlas.discovery.finder import discover_repositories
from repoatlas.gitstate.client import is_git_repository
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
class RepositoryRelocation:
    """One registered repository, found again at a new path under a new root.

    The pair is the evidence, not a conclusion drawn from either half alone:
    :attr:`repository` is the row whose directory has *gone*, and
    :attr:`candidate` is the discovery whose root-relative identity is the one
    that row already holds.
    """

    repository: Repository
    candidate: RepositoryCandidate


@dataclass(frozen=True, slots=True)
class RepositoryRootConflict:
    """Two live directories, one identity — and no evidence for choosing between them.

    Produced when a discovered repository's slug is already held by a registered
    repository **that still exists at its old path**. Nothing about that says
    "moved": it says two working trees are claiming one identity, which is what a
    second workspace looks like. Reported rather than resolved.
    """

    registered: Repository
    candidate: RepositoryCandidate


@dataclass(frozen=True, slots=True)
class RepositorySetPlan:
    """What discovery found, against what is registered. Purely descriptive."""

    root: Path
    #: Every repository the current walk found, in discovery's own deterministic
    #: order (sorted by path).
    discovered: tuple[RepositoryCandidate, ...] = ()
    #: Discovered, not registered, and not a relocation of anything. ``sync``
    #: registers and indexes these; ``status`` reports them as the reason the
    #: workspace is not current.
    added: tuple[RepositoryCandidate, ...] = ()
    #: Registered and still discovered at the path on record.
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
    #:
    #: A relocated repository is **not** missing. Its old directory is gone, which
    #: is half of what proves the relocation, and retiring it would delete the very
    #: row the relocation exists to carry forward.
    missing: tuple[Repository, ...] = ()
    #: Registered rows whose identity was found again at a new path (§3). Applied
    #: only by ``init``, which owns the managed root; see :func:`relocate`.
    relocated: tuple[RepositoryRelocation, ...] = ()
    #: Identity collisions that are *not* relocations. ``init`` refuses on these
    #: rather than writing; ``status`` can describe them.
    conflicts: tuple[RepositoryRootConflict, ...] = ()
    #: Directories the operating system refused. "We found nothing there" and "we
    #: were not allowed to look" are different claims (V1_SPEC §5).
    unreadable: tuple[Path, ...] = ()

    @property
    def drifted(self) -> bool:
        """Whether the registered set no longer describes the managed root."""
        return bool(self.added or self.missing or self.relocated or self.conflicts)


def _identify(
    rows: Sequence[Repository], candidates: Sequence[RepositoryCandidate]
) -> tuple[tuple[RepositoryRelocation, ...], tuple[RepositoryRootConflict, ...]]:
    """Match registered rows to discoveries by identity, for the ones paths could not.

    Only ever consulted about the leftovers — a row whose path was discovered is
    already accounted for, and needs no identity argument. What remains is the
    question a moved managed root asks: the same repository is *here* now, and the
    table still describes it as being *there*.

    The slug answers it, because the slug is the repository's path **relative to
    the managed root**, and moving the root does not change it. ``/old/service-a``
    and ``/new/service-a`` are both ``service-a``. That is the entire signal, and
    the reason no Git inspection is needed to read it: identity is already a
    root-relative fact, and relocating the root is precisely the operation that
    preserves it (ARCHITECTURE.md §3).

    **A matching slug is necessary and not sufficient.** The second condition is
    that the registered directory is *gone*. A slug that matches while both
    directories exist is not a repository that moved — it is two repositories
    asking for one identity, which is what a second workspace on the same machine
    looks like, and there is nothing in a slug that distinguishes the copy the
    operator meant. Those become :class:`RepositoryRootConflict` and stop ``init``.

    The match must also be **collision-free on both sides**. Slugs are unique by
    construction — on the filesystem because two directories cannot share a path,
    in the table because of the partial unique index — so an ambiguous group means
    an assumption has already failed, and the safe response is to claim nothing and
    let the ordinary added/missing paths handle it. Rows with no slug are skipped
    entirely: a pre-v5 row has no identity to match on, and inventing one from its
    basename is the duplicate-basename defect this identity replaced.
    """
    rows_by_slug: dict[str, list[Repository]] = {}
    for row in rows:
        if row.slug:
            rows_by_slug.setdefault(row.slug, []).append(row)
    found_by_slug: dict[str, list[RepositoryCandidate]] = {}
    for candidate in candidates:
        if candidate.slug:
            found_by_slug.setdefault(candidate.slug, []).append(candidate)

    relocated: list[RepositoryRelocation] = []
    conflicts: list[RepositoryRootConflict] = []
    for slug in sorted(found_by_slug):
        claimants = rows_by_slug.get(slug, ())
        discoveries = found_by_slug[slug]
        if len(claimants) != 1 or len(discoveries) != 1:
            continue
        row, candidate = claimants[0], discoveries[0]
        if row.path.is_dir():
            conflicts.append(RepositoryRootConflict(registered=row, candidate=candidate))
        else:
            relocated.append(RepositoryRelocation(repository=row, candidate=candidate))
    return tuple(relocated), tuple(conflicts)


def plan_repository_set(
    root: Path,
    settings: DiscoverySettings,
    registered: Sequence[Repository],
) -> RepositorySetPlan:
    """Diff the managed root against the registered set. **Reads only.**

    Writing nothing is a contract, not an implementation detail: ``status`` calls
    this, and a read command that quietly registered repositories would make
    "check whether anything changed" a mutating operation — indistinguishable, to
    anyone reading a terminal, from having run a sync (V1_SPEC §4). Relocation is
    *detected* here and applied by ``init`` alone, for the same reason.

    Paths are matched first and identities only afterwards, which is what keeps
    relocation from firing on an ordinary run. When the root has not changed, a
    repository that is still there matches by path, and one that moved *within*
    the root has a different root-relative slug — so there is nothing for
    :func:`_identify` to match, and every ordinary ``sync`` and ``status`` reaches
    exactly the plan it reached before (ARCHITECTURE.md §3).
    """
    unreadable: list[Path] = []
    discovered = discover_repositories(root, settings, unreadable=unreadable)
    declarations = declarations_candidate()
    if declarations is not None and all(
        _canonical(candidate.path) != _canonical(declarations.path) for candidate in discovered
    ):
        discovered = [*discovered, declarations]

    found = {_canonical(candidate.path): candidate for candidate in discovered}
    by_path = {_canonical(repo.path): repo for repo in registered}

    existing = tuple(repo for path, repo in by_path.items() if path in found)
    stray = [candidate for path, candidate in found.items() if path not in by_path]
    orphaned = [repo for path, repo in by_path.items() if path not in found]

    relocated, conflicts = _identify(orphaned, stray)
    spoken_for = {_canonical(item.candidate.path) for item in relocated}
    spoken_for |= {_canonical(item.candidate.path) for item in conflicts}
    carried = {_canonical(item.repository.path) for item in relocated}

    added = tuple(c for c in stray if _canonical(c.path) not in spoken_for)
    missing = tuple(
        repo for repo in orphaned if _canonical(repo.path) not in carried and not repo.path.is_dir()
    )

    return RepositorySetPlan(
        root=root,
        discovered=tuple(discovered),
        added=added,
        existing=existing,
        missing=missing,
        relocated=relocated,
        conflicts=conflicts,
        unreadable=tuple(unreadable),
    )


def declarations_candidate() -> RepositoryCandidate | None:
    """The operator declarations source, as the reserved repository ``@declarations``.

    Only when its directory (:func:`~repoatlas.config.settings.declarations_directory`)
    exists and is a git repository the operator made: RepoAtlas never initialises,
    commits to, or publishes it, and a plain directory there is not read (``status`` says
    so). It is enumerated, hashed, synced and checked for freshness like any repository,
    under a slug that is not a path, and kept apart from the workspace's own (DF1, D1-1).
    """
    directory = declarations_directory()
    if not directory.is_dir() or not is_git_repository(directory):
        return None
    return RepositoryCandidate(
        name=directory.name, slug=DECLARATIONS_SLUG, path=directory.resolve()
    )


def declarations_ignored() -> Path | None:
    """The declarations directory, when it exists but is not a git repository."""
    directory = declarations_directory()
    return directory if directory.is_dir() and not is_git_repository(directory) else None


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
