"""The index-freshness state machine (V1_SPEC §12, §16).

A top-level module on purpose. Both ``services/status_service.py`` and the
capability layer above it have to answer the same question, and the dependency
arrow only runs one way: capabilities may use services, services may never use
capabilities. Shared logic that sits in either of them is therefore in the wrong
place, so it sits below both.

Everything here is a **pure function**. No store, no git, no filesystem: the
caller gathers the facts and decides what they cost, which is what keeps a
``STORED`` answer free of any git invocation (see
:mod:`repoatlas.services.freshness_service`).

Two rules are load-bearing.

**Depth bounds the claim.** With only the database in hand RepoAtlas can say that
facts exist and were produced by this build. It cannot say ``current``, because
current is a statement about HEAD and the working tree and neither was consulted.
That state is :attr:`~repoatlas.models.freshness.FreshnessState.INDEXED`.

**Equal dirty flags are not equal trees.** V1 compares two booleans and reports
``current (dirty working tree)`` when they agree. A tree that was dirty at index
time and has been edited again since satisfies that comparison and holds different
bytes. The V1 sentence is preserved byte for byte by :func:`legacy_index_state`;
the typed model refuses to inherit the claim.
"""

from __future__ import annotations

from collections.abc import Sequence

from repoatlas.gitstate.client import GitRepositoryState
from repoatlas.models.freshness import (
    FreshnessDepth,
    FreshnessState,
    IndexFreshness,
    RepositoryFreshness,
    WorktreeContentStatus,
    severity,
)
from repoatlas.models.repository import Repository, SyncStatus

#: Exactly the strings V1's ``status`` has always printed, keyed by the typed
#: state that replaced the branch which produced each one. Pinned by
#: ``tests/capabilities/test_v1_status_compatibility.py``; changing a value here
#: changes ``repoatlas status`` output and ``--json`` payloads.
_LEGACY_INDEX_STATE: dict[FreshnessState, str] = {
    FreshnessState.NOT_INDEXED: "not indexed",
    FreshnessState.UNAVAILABLE: "unavailable (git could not enumerate the repository)",
    FreshnessState.INTERRUPTED: "interrupted",
    FreshnessState.STALE_BUILD: "stale (indexed by an older build)",
    FreshnessState.UNKNOWN: "unknown",
    FreshnessState.STALE_HEAD: "stale (HEAD changed)",
    FreshnessState.STALE_WORKTREE: "stale (working tree changed)",
    FreshnessState.DIRTY: "current (dirty working tree)",
    FreshnessState.CURRENT_CLEAN: "current",
    # V1 always resolves at LIVE depth, so it never reaches this state. Mapped
    # rather than raised, and mapped to the honest word: at STORED depth HEAD was
    # not consulted, so "unknown" is what V1's vocabulary has for it.
    FreshnessState.INDEXED: "unknown",
}


def repository_freshness(
    repo: Repository,
    *,
    outdated_files: int,
    live: GitRepositoryState | None,
) -> RepositoryFreshness:
    """Decide one repository's freshness from facts the caller already gathered.

    ``outdated_files`` is ``files.count_outdated`` against the current facts
    version — the cheap half of freshness, and a pure database read.

    ``live`` is this repository's git state, or ``None`` to answer at ``STORED``
    depth. ``None`` is not a failure: it means the caller chose not to pay for a
    git invocation, and the returned depth says so. Git *failing* is a different
    thing and arrives as a ``GitRepositoryState`` with ``known=False``.

    The branch order mirrors V1's ``_index_state`` exactly, because it is the same
    decision: never claim currency where RepoAtlas can cheaply tell there is none,
    and check the facts version before the git state, since a build change makes
    stored facts stale while HEAD, mtime and content hash all still agree.
    """
    depth = FreshnessDepth.STORED if live is None else FreshnessDepth.LIVE
    build_current = outdated_files == 0
    indexed_dirty = repo.last_indexed_dirty

    def state_only(
        state: FreshnessState,
        *,
        content: WorktreeContentStatus = WorktreeContentStatus.UNKNOWN,
        head_matches: bool | None = None,
        working_tree_dirty: bool | None = None,
    ) -> RepositoryFreshness:
        return RepositoryFreshness(
            repository_slug=repo.slug,
            repository_name=repo.name,
            state=state,
            depth=depth,
            detail=_LEGACY_INDEX_STATE[state],
            build_current=build_current,
            head_matches=head_matches,
            working_tree_dirty=working_tree_dirty,
            indexed_worktree_dirty=indexed_dirty,
            working_tree_content=content,
            outdated_files=outdated_files,
            last_indexed_at=repo.last_indexed_at,
        )

    if repo.last_indexed_at is None:
        return state_only(FreshnessState.NOT_INDEXED)

    if repo.last_sync_status is SyncStatus.FAILED:
        # Git could not say which files belong to this repository, so RepoAtlas
        # indexed none of them. Whatever is stored is a previous run's, and
        # "interrupted" would suggest a run that merely needs finishing.
        return state_only(FreshnessState.UNAVAILABLE)

    if repo.last_sync_status is not SyncStatus.OK:
        return state_only(FreshnessState.INTERRUPTED)

    if not build_current:
        # A facts-version change makes stored facts stale while the working tree
        # is untouched, so HEAD, mtime and content hash all still agree. This is
        # the only signal that a build which fixed a parser has not reached this
        # index (V1_SPEC §12).
        return state_only(FreshnessState.STALE_BUILD)

    if live is None:
        # Everything the database can establish, established. HEAD and the
        # working tree were not consulted, so this is deliberately not "current".
        return state_only(FreshnessState.INDEXED)

    if not live.known:
        return state_only(FreshnessState.UNKNOWN, working_tree_dirty=None)

    head_matches = live.head_sha == repo.last_indexed_head
    if not head_matches:
        return state_only(
            FreshnessState.STALE_HEAD,
            content=WorktreeContentStatus.DIFFERS,
            head_matches=False,
            working_tree_dirty=live.is_dirty,
        )

    if indexed_dirty is not None and live.is_dirty != indexed_dirty:
        return state_only(
            FreshnessState.STALE_WORKTREE,
            content=WorktreeContentStatus.DIFFERS,
            head_matches=True,
            working_tree_dirty=live.is_dirty,
        )

    if live.is_dirty:
        # HEAD matches and the tree is dirty. That is the whole of what was
        # checked. The dirty flag agreeing with the one recorded at index time
        # proves the tree is dirty in both cases and nothing whatever about the
        # bytes, so content stays UNKNOWN — this is the case V1's
        # "current (dirty working tree)" overstates.
        return state_only(
            FreshnessState.DIRTY,
            content=WorktreeContentStatus.UNKNOWN,
            head_matches=True,
            working_tree_dirty=True,
        )

    # HEAD matches and the tree is clean, so the tree is HEAD's content. That
    # equals the indexed content only if the tree was also clean when indexed. A
    # run that never recorded which it was leaves this unprovable, so the state
    # is still CURRENT_CLEAN — nothing about it is stale — while the content
    # dimension declines to claim more than was established.
    content = (
        WorktreeContentStatus.MATCHES_INDEX
        if indexed_dirty is False
        else WorktreeContentStatus.UNKNOWN
    )
    return state_only(
        FreshnessState.CURRENT_CLEAN,
        content=content,
        head_matches=True,
        working_tree_dirty=False,
    )


def index_freshness(
    repositories: Sequence[RepositoryFreshness],
    *,
    depth: FreshnessDepth,
    repository_set_drifted: bool | None = None,
) -> IndexFreshness:
    """Aggregate per-repository freshness by taking the **worst** state.

    Not an average and not a count. One stale repository in ten makes the answer
    to "can I trust this index?" no, and any rule that lets nine current ones
    outvote it is answering a different question.

    An empty scope is ``NOT_INDEXED``: there is nothing to be current about.
    """
    if not repositories:
        return IndexFreshness(
            depth=depth,
            state=FreshnessState.NOT_INDEXED,
            repositories=(),
            repository_set_drifted=repository_set_drifted,
        )
    worst = min(repositories, key=lambda freshness: severity(freshness.state))
    return IndexFreshness(
        depth=depth,
        state=worst.state,
        repositories=tuple(repositories),
        repository_set_drifted=repository_set_drifted,
    )


def legacy_index_state(freshness: RepositoryFreshness) -> str:
    """The exact sentence V1's ``status`` has always printed for this state.

    A compatibility rendering, kept apart from the decision above so that
    correcting the model never edits the string and correcting the string never
    edits the model. V1's ``index_state`` is a public output: it appears in
    ``--json``, in the human table, and in the aggregate that ``status`` derives
    by inspecting these very strings.

    ``current (dirty working tree)`` is the one that outlives its own correction.
    The typed model says HEAD matches and stops; this says what V1 said. Both are
    deliberate, and the asymmetry is the point of having two functions.
    """
    return _LEGACY_INDEX_STATE[freshness.state]
