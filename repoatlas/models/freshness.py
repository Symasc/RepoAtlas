"""Typed index freshness (V1_SPEC §12, §16).

V1 answers "is the index current?" with a sentence. The sentence is good for a
terminal and useless to a caller that has to decide something, because the only
way back to the decision is to parse prose. These models are the decision itself.

The load-bearing rule here is what freshness may **not** claim:

    Two equal dirty flags do not prove two equal working trees.

A repository indexed while dirty, then edited again while still dirty, has
``last_indexed_dirty == is_dirty == True`` and different bytes on disk. V1 reports
that as ``current (dirty working tree)`` and V1's wording is kept exactly, but the
typed model refuses to inherit the claim: :class:`WorktreeContentStatus` says
``UNKNOWN``, and :attr:`RepositoryFreshness.content_verified_current` is ``False``.

Establishing more would mean hashing every tracked file on every status call,
which costs more than the sync it reports on. Truthfulness is the cheaper half,
and it is the half that was missing.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from repoatlas.compat import StrEnum


class FreshnessDepth(StrEnum):
    """How hard RepoAtlas looked, and therefore what it is entitled to say.

    The depth is part of the answer, not a parameter that produced it. A caller
    reading ``INDEXED`` at ``STORED`` depth learns that facts are present and
    current for this build, and specifically *not* that HEAD still matches —
    because nothing checked.
    """

    #: Database only. No git invocation, no filesystem walk.
    STORED = "stored"
    #: The stored answer plus this repository's live git state.
    LIVE = "live"


class FreshnessState(StrEnum):
    """What RepoAtlas established about one repository's index.

    Five states are reachable at ``STORED`` depth and five more need ``LIVE``.
    None of them is a summary: each names the single strongest thing that was
    actually checked, and the dimensions on :class:`RepositoryFreshness` carry the
    rest.
    """

    #: Nothing has ever been indexed for this repository.
    NOT_INDEXED = "not_indexed"
    #: The last run could not establish the repository's file set at all, so
    #: nothing was indexed from it. Whatever is stored belongs to an earlier run.
    UNAVAILABLE = "unavailable"
    #: A run started and did not finish.
    INTERRUPTED = "interrupted"
    #: Facts were produced by a different parser/chunking build. The tree may be
    #: untouched; the stored facts are stale regardless (V1_SPEC §12).
    STALE_BUILD = "stale_build"
    #: ``STORED`` terminal state: facts are present and were produced by this
    #: build. Deliberately **not** called current — HEAD and the working tree
    #: were not consulted.
    INDEXED = "indexed"
    #: ``LIVE``: HEAD matches the indexed commit and the working tree is clean.
    CURRENT_CLEAN = "current_clean"
    #: ``LIVE``: HEAD matches and the tree is dirty. Says nothing about whether
    #: the dirty bytes are the indexed bytes — see :class:`WorktreeContentStatus`.
    DIRTY = "dirty"
    #: ``LIVE``: the live HEAD is not the indexed one.
    STALE_HEAD = "stale_head"
    #: ``LIVE``: the working tree's dirty flag changed since indexing.
    STALE_WORKTREE = "stale_worktree"
    #: ``LIVE``: git could not report this repository's state.
    UNKNOWN = "unknown"


#: Worst-first severity. Used to aggregate many repositories into one verdict
#: without sorting prose, which is how V1's aggregate has to do it.
_SEVERITY: dict[FreshnessState, int] = {
    FreshnessState.NOT_INDEXED: 0,
    FreshnessState.UNAVAILABLE: 1,
    FreshnessState.INTERRUPTED: 2,
    FreshnessState.STALE_BUILD: 3,
    FreshnessState.STALE_HEAD: 4,
    FreshnessState.STALE_WORKTREE: 5,
    FreshnessState.UNKNOWN: 6,
    FreshnessState.DIRTY: 7,
    FreshnessState.INDEXED: 8,
    FreshnessState.CURRENT_CLEAN: 9,
}


def severity(state: FreshnessState) -> int:
    """Rank of ``state``, lowest first. Lower is a weaker claim about the index."""
    return _SEVERITY[state]


class WorktreeContentStatus(StrEnum):
    """Whether the bytes on disk are the bytes that were indexed.

    A separate dimension because it is a different question from
    :class:`FreshnessState`, and collapsing the two is exactly what let
    ``current (dirty working tree)`` imply something nobody checked.
    """

    #: HEAD matches, the tree is clean, and the tree was clean when indexed.
    #: Content equality follows from those three, with nothing hashed.
    MATCHES_INDEX = "matches_index"
    #: RepoAtlas did not establish it. A dirty tree lands here, and so does every
    #: ``STORED``-depth answer.
    UNKNOWN = "unknown"
    #: Positively different: HEAD moved, or the dirty flag changed.
    DIFFERS = "differs"


class RepositoryFreshness(BaseModel):
    """One repository's index freshness, and how much of it was checked."""

    model_config = ConfigDict(frozen=True)

    #: Canonical identity. Display names may repeat and are never identity.
    repository_slug: str
    repository_name: str = ""
    state: FreshnessState
    depth: FreshnessDepth

    #: Explanatory text carried for V1 compatibility. **Never** a machine
    #: identity: callers branch on ``state``, ``depth`` and the dimensions below.
    #: Nothing in RepoAtlas may parse this field.
    detail: str = ""

    #: Stored facts were produced by the current parser/chunking build.
    build_current: bool = False
    #: ``None`` at ``STORED`` depth, where HEAD was not consulted.
    head_matches: bool | None = None
    #: ``None`` at ``STORED`` depth, or when git could not report.
    working_tree_dirty: bool | None = None
    #: What the last sync recorded about the tree it indexed. ``None`` when no
    #: run recorded it, which is itself a reason not to claim content currency.
    indexed_worktree_dirty: bool | None = None
    working_tree_content: WorktreeContentStatus = WorktreeContentStatus.UNKNOWN

    outdated_files: int = 0
    last_indexed_at: datetime | None = None

    @property
    def content_verified_current(self) -> bool:
        """Whether RepoAtlas can prove the indexed bytes are the tree's bytes.

        The one predicate a caller should use before treating stored evidence as
        describing the working tree. False is the honest answer for every dirty
        tree, and for every ``STORED``-depth answer.
        """
        return self.working_tree_content is WorktreeContentStatus.MATCHES_INDEX


class IndexFreshness(BaseModel):
    """Freshness across the repositories in scope.

    ``state`` is the **worst** repository's state rather than a summary word, so
    one stale repository cannot be averaged away by nine current ones.
    """

    model_config = ConfigDict(frozen=True)

    depth: FreshnessDepth
    state: FreshnessState
    repositories: tuple[RepositoryFreshness, ...] = ()
    #: ``None`` means repository-set drift was not checked. It requires a
    #: filesystem walk of the managed root, so it is opt-in rather than a cost
    #: every freshness question pays (V1_SPEC §5).
    repository_set_drifted: bool | None = None

    @property
    def content_verified_current(self) -> bool:
        """True only when every repository in scope proves content currency.

        Drift that was never checked cannot contribute to a positive claim, so an
        unchecked repository set is not allowed to make this ``True`` by silence:
        it is only ``True`` when drift was checked and found absent.
        """
        if self.repository_set_drifted is not False:
            return False
        return bool(self.repositories) and all(
            repository.content_verified_current for repository in self.repositories
        )
