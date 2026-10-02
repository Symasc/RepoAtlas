"""Typed index freshness (V1_SPEC §12, §16).

Two properties carry this suite, and both are about what freshness may *not* say.

**Depth bounds the claim.** A ``STORED`` answer is a database read. It cannot
report ``CURRENT_CLEAN``, because current is a statement about HEAD and the
working tree and neither was consulted. Asserted by making git fatal to call.

**Equal dirty flags are not equal trees.** Index a dirty tree, edit it again, and
``last_indexed_dirty`` still equals ``is_dirty``. V1 calls that
``current (dirty working tree)``; the typed model refuses to inherit the claim.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.freshness import index_freshness, repository_freshness
from repoatlas.gitstate.client import GitClient, GitRepositoryState
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.capability_io import StatusRequest
from repoatlas.models.freshness import (
    FreshnessDepth,
    FreshnessState,
    IndexFreshness,
    WorktreeContentStatus,
)
from repoatlas.models.repository import Repository, SyncStatus
from repoatlas.services.freshness_service import repository_freshness_for
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace

INDEXED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)
HEAD = "a" * 40


def _repo(
    *,
    status: SyncStatus = SyncStatus.OK,
    indexed: datetime | None = INDEXED_AT,
    head: str | None = HEAD,
    indexed_dirty: bool | None = False,
) -> Repository:
    return Repository(
        id=1,
        name="service",
        slug="team/service",
        path=Path("/managed/team/service"),
        last_indexed_at=indexed,
        last_indexed_head=head,
        last_indexed_dirty=indexed_dirty,
        last_sync_status=status,
    )


def _live(
    *, head: str | None = HEAD, dirty: bool = False, known: bool = True
) -> GitRepositoryState:
    return GitRepositoryState(branch="main", head_sha=head, is_dirty=dirty, known=known)


# -- every reachable state ------------------------------------------------------


def test_a_repository_that_was_never_indexed() -> None:
    freshness = repository_freshness(_repo(indexed=None), outdated_files=0, live=_live())
    assert freshness.state is FreshnessState.NOT_INDEXED


def test_a_repository_git_could_not_enumerate() -> None:
    """Distinct from interrupted: nothing was indexed, rather than half of it."""
    freshness = repository_freshness(
        _repo(status=SyncStatus.FAILED), outdated_files=0, live=_live()
    )
    assert freshness.state is FreshnessState.UNAVAILABLE


def test_a_run_that_started_and_did_not_finish() -> None:
    freshness = repository_freshness(
        _repo(status=SyncStatus.INTERRUPTED), outdated_files=0, live=_live()
    )
    assert freshness.state is FreshnessState.INTERRUPTED


def test_facts_from_an_older_build_are_stale_even_when_head_and_tree_agree() -> None:
    """The only signal that a parser fix has not reached this index (V1_SPEC §12)."""
    freshness = repository_freshness(_repo(), outdated_files=3, live=_live())
    assert freshness.state is FreshnessState.STALE_BUILD
    assert freshness.build_current is False
    assert freshness.outdated_files == 3
    assert freshness.content_verified_current is False


def test_a_moved_head_is_stale() -> None:
    freshness = repository_freshness(_repo(), outdated_files=0, live=_live(head="b" * 40))
    assert freshness.state is FreshnessState.STALE_HEAD
    assert freshness.head_matches is False
    assert freshness.working_tree_content is WorktreeContentStatus.DIFFERS


def test_a_changed_dirty_flag_is_stale() -> None:
    freshness = repository_freshness(
        _repo(indexed_dirty=False), outdated_files=0, live=_live(dirty=True)
    )
    assert freshness.state is FreshnessState.STALE_WORKTREE
    assert freshness.working_tree_content is WorktreeContentStatus.DIFFERS


def test_git_failing_is_unknown_and_never_fabricated_currency() -> None:
    freshness = repository_freshness(_repo(), outdated_files=0, live=_live(known=False))
    assert freshness.state is FreshnessState.UNKNOWN
    assert freshness.content_verified_current is False


def test_head_matching_a_clean_tree_indexed_clean_is_content_current() -> None:
    """The one case where content equality follows without hashing anything."""
    freshness = repository_freshness(
        _repo(indexed_dirty=False), outdated_files=0, live=_live(dirty=False)
    )
    assert freshness.state is FreshnessState.CURRENT_CLEAN
    assert freshness.working_tree_content is WorktreeContentStatus.MATCHES_INDEX
    assert freshness.content_verified_current is True


def test_a_run_that_never_recorded_the_tree_it_indexed_cannot_prove_content() -> None:
    """Nothing is stale, and nothing is proved either. The dimensions say both."""
    freshness = repository_freshness(
        _repo(indexed_dirty=None), outdated_files=0, live=_live(dirty=False)
    )
    assert freshness.state is FreshnessState.CURRENT_CLEAN
    assert freshness.working_tree_content is WorktreeContentStatus.UNKNOWN
    assert freshness.content_verified_current is False


# -- the dirty counterexample ---------------------------------------------------


def test_matching_dirty_flags_do_not_prove_matching_content() -> None:
    """The whole reason ``CURRENT_DIRTY`` is not a state in this model.

    Indexed dirty, still dirty. The booleans agree and the bytes need not.
    """
    freshness = repository_freshness(
        _repo(indexed_dirty=True), outdated_files=0, live=_live(dirty=True)
    )
    assert freshness.state is FreshnessState.DIRTY
    assert freshness.working_tree_dirty is True
    assert freshness.indexed_worktree_dirty is True
    assert freshness.head_matches is True
    assert freshness.working_tree_content is WorktreeContentStatus.UNKNOWN
    assert freshness.content_verified_current is False


def test_a_dirty_tree_edited_again_is_never_content_current(
    initialized: Workspace, workspace_root: Path
) -> None:
    """The counterexample end to end, against a real git repository.

    T0 dirty, sync. T1 edit the same file again; the tree is still dirty. Every
    boolean RepoAtlas stores is unchanged, and the file on disk is not.
    """
    repo = workspace_root / "python-service"
    target = repo / "src" / "config.py"

    target.write_text("SERVICE_HOST = 'first-edit'\n", encoding="utf-8")
    sync(initialized, initialized.resolver().resolve(workspace_root))

    row = initialized.repositories.get_by_name("python-service")
    assert row is not None and row.last_indexed_dirty is True

    target.write_text("SERVICE_HOST = 'second-edit-never-indexed'\n", encoding="utf-8")

    freshness = repository_freshness_for(initialized, row, depth=FreshnessDepth.LIVE)
    assert freshness.state is FreshnessState.DIRTY
    assert freshness.working_tree_dirty == freshness.indexed_worktree_dirty
    assert freshness.working_tree_content is WorktreeContentStatus.UNKNOWN
    assert freshness.content_verified_current is False


# -- depth bounds the claim ------------------------------------------------------


def test_stored_depth_stops_at_indexed_and_never_says_current() -> None:
    freshness = repository_freshness(_repo(), outdated_files=0, live=None)
    assert freshness.state is FreshnessState.INDEXED
    assert freshness.depth is FreshnessDepth.STORED
    assert freshness.head_matches is None
    assert freshness.working_tree_dirty is None
    assert freshness.content_verified_current is False


def test_no_stored_answer_is_ever_a_live_only_state() -> None:
    live_only = {
        FreshnessState.CURRENT_CLEAN,
        FreshnessState.DIRTY,
        FreshnessState.STALE_HEAD,
        FreshnessState.STALE_WORKTREE,
        FreshnessState.UNKNOWN,
    }
    for status in SyncStatus:
        for outdated in (0, 5):
            for indexed in (None, INDEXED_AT):
                freshness = repository_freshness(
                    _repo(status=status, indexed=indexed), outdated_files=outdated, live=None
                )
                assert freshness.state not in live_only
                assert freshness.depth is FreshnessDepth.STORED


def _status(
    workspace: Workspace, *, depth: FreshnessDepth, include_drift: bool = False
) -> IndexFreshness:
    """The index freshness ``status`` reports, at one depth, with drift opt-in."""
    request = StatusRequest(
        context=CapabilityContext(scope=CapabilityScope.GLOBAL),
        depth=depth,
        include_drift=include_drift,
    )
    return CapabilityService(workspace).status(request).freshness


def test_stored_depth_never_invokes_git(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cheap read that quietly runs a command per repository is not a cheap read."""

    def explode(self: GitClient, repo_path: Path) -> GitRepositoryState:  # pragma: no cover
        raise AssertionError("STORED freshness invoked git")

    monkeypatch.setattr(GitClient, "state", explode)
    freshness = _status(initialized, depth=FreshnessDepth.STORED)
    assert freshness.depth is FreshnessDepth.STORED
    assert freshness.repositories


def test_live_depth_asks_git_once_per_repository(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path] = []
    original = GitClient.state

    def counting(self: GitClient, repo_path: Path) -> GitRepositoryState:
        calls.append(repo_path)
        return original(self, repo_path)

    monkeypatch.setattr(GitClient, "state", counting)
    _status(initialized, depth=FreshnessDepth.LIVE)
    assert len(calls) == len(initialized.repositories.list_all())


def test_repository_set_drift_is_not_computed_unless_asked(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drift costs a discovery walk, so it is the only cost that is opt-in twice."""
    import repoatlas.services.freshness_service as service

    calls: list[Path] = []

    def counting(root: Path, settings: object, registered: object) -> object:  # pragma: no cover
        calls.append(root)
        raise AssertionError("drift was computed without being requested")

    monkeypatch.setattr(service, "plan_repository_set", counting)
    freshness = _status(initialized, depth=FreshnessDepth.STORED)
    assert calls == []
    assert freshness.repository_set_drifted is None
    assert freshness.content_verified_current is False


def test_unchecked_drift_cannot_make_the_index_content_current(
    initialized: Workspace, workspace_root: Path
) -> None:
    """Silence about the repository set is not evidence the set is complete."""
    sync(initialized, initialized.resolver().resolve(workspace_root))
    without = _status(initialized, depth=FreshnessDepth.LIVE)
    assert without.repository_set_drifted is None
    assert without.content_verified_current is False

    checked = _status(initialized, depth=FreshnessDepth.LIVE, include_drift=True)
    assert checked.repository_set_drifted is False
    assert checked.content_verified_current is True


# -- aggregation -----------------------------------------------------------------


def test_the_worst_repository_decides_the_index_verdict() -> None:
    """Nine current repositories may not outvote one stale one."""
    current = repository_freshness(_repo(), outdated_files=0, live=_live())
    stale = repository_freshness(_repo(), outdated_files=1, live=_live())
    aggregate = index_freshness([current, current, stale], depth=FreshnessDepth.LIVE)
    assert aggregate.state is FreshnessState.STALE_BUILD


def test_an_empty_scope_is_not_indexed() -> None:
    aggregate = index_freshness([], depth=FreshnessDepth.STORED)
    assert aggregate.state is FreshnessState.NOT_INDEXED
    assert aggregate.content_verified_current is False


# -- the detail field is not identity ---------------------------------------------


def test_no_capability_or_freshness_module_branches_on_the_detail_string() -> None:
    """``detail`` is compatibility prose. Branching on it would make it a contract."""
    package = Path(__file__).resolve().parents[2] / "repoatlas"
    banned = ("detail.startswith", "detail.endswith", "detail ==", "in freshness.detail")
    offenders: list[str] = []
    candidates = [package / "freshness.py", *sorted((package / "capabilities").rglob("*.py"))]
    for path in candidates:
        source = path.read_text(encoding="utf-8")
        for marker in banned:
            if marker in source:
                offenders.append(f"{path.name} contains {marker!r}")
    assert not offenders, offenders
