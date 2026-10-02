"""The ``status`` capability (V2 Slice C).

Whether the index can be trusted, answered as typed data rather than as a
sentence. The suite is built around four claims:

* the verdict is the **shared** freshness state machine, so V1 and V2 cannot
  disagree about what is stale;
* status **detects and never reconciles** — no write, no sync, no registration;
* drift is a claim about the **managed root** at every scope, and says so;
* a dirty tree is never reported as content-current, however V1's compatibility
  sentence reads.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.gitstate.client import GitClient, GitRepositoryState
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.capability_io import RepositoriesRequest, StatusRequest, StatusResponse
from repoatlas.models.freshness import (
    FreshnessDepth,
    FreshnessState,
    IndexFreshness,
    RepositoryFreshness,
    WorktreeContentStatus,
)
from repoatlas.models.repository import Repository, SyncStatus
from repoatlas.services.workspace import Workspace

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)
HEAD = "a" * 40
INDEXED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _service(workspace: Workspace) -> CapabilityService:
    return CapabilityService(workspace)


def _repository_scope(selector: str) -> CapabilityContext:
    return CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=selector)


def _register(
    workspace: Workspace,
    *,
    slug: str = "team/service",
    name: str = "service",
    indexed: datetime | None = INDEXED_AT,
    indexed_head: str | None = HEAD,
    indexed_dirty: bool | None = False,
    sync_status: SyncStatus = SyncStatus.OK,
) -> Repository:
    """A registered repository row with no indexed facts behind it.

    Enough for every freshness case: the state machine reads the row and the live
    git answer, never the file contents.
    """
    repo = Repository(
        name=name,
        slug=slug,
        path=Path("/managed") / slug,
        last_indexed_at=indexed,
        last_indexed_head=indexed_head,
        last_indexed_dirty=indexed_dirty,
        last_sync_status=sync_status,
    )
    workspace.repositories.upsert(repo)
    return repo


def _fake_git(
    monkeypatch: pytest.MonkeyPatch,
    *,
    head: str | None = HEAD,
    dirty: bool = False,
    known: bool = True,
) -> None:
    def state(self: GitClient, repo_path: Path) -> GitRepositoryState:
        return GitRepositoryState(branch="main", head_sha=head, is_dirty=dirty, known=known)

    monkeypatch.setattr(GitClient, "state", state)


def _only(
    workspace: Workspace,
    monkeypatch: pytest.MonkeyPatch,
    *,
    indexed: datetime | None = INDEXED_AT,
    indexed_dirty: bool | None = False,
    sync_status: SyncStatus = SyncStatus.OK,
    head: str | None = HEAD,
    dirty: bool = False,
    known: bool = True,
) -> RepositoryFreshness:
    """Register one repository, fake its git state, and return its freshness."""
    _register(workspace, indexed=indexed, indexed_dirty=indexed_dirty, sync_status=sync_status)
    _fake_git(monkeypatch, head=head, dirty=dirty, known=known)
    response = _service(workspace).status(StatusRequest(context=GLOBAL, include_drift=False))
    (freshness,) = response.freshness.repositories
    return freshness


# ---------------------------------------------------------------------------
# Depth
# ---------------------------------------------------------------------------


def test_status_resolves_live_by_default(initialized: Workspace) -> None:
    """The question status answers is whether the index matches the tree *now*."""
    response = _service(initialized).status(StatusRequest(context=GLOBAL))
    assert response.freshness.depth is FreshnessDepth.LIVE
    assert all(r.head_matches is not None for r in response.freshness.repositories)


def test_stored_depth_is_available_and_costs_no_git(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(self: GitClient, repo_path: Path) -> GitRepositoryState:
        raise AssertionError("stored-depth status invoked git")

    monkeypatch.setattr(GitClient, "state", forbidden)
    response = _service(initialized).status(
        StatusRequest(context=GLOBAL, depth=FreshnessDepth.STORED, include_drift=False)
    )
    assert response.freshness.depth is FreshnessDepth.STORED
    assert all(r.head_matches is None for r in response.freshness.repositories)


# ---------------------------------------------------------------------------
# The freshness vocabulary, end to end through the capability
# ---------------------------------------------------------------------------


def test_a_never_indexed_repository_is_not_indexed(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _only(workspace, monkeypatch, indexed=None).state is FreshnessState.NOT_INDEXED


def test_an_interrupted_sync_is_reported_as_interrupted(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    freshness = _only(workspace, monkeypatch, sync_status=SyncStatus.INTERRUPTED)
    assert freshness.state is FreshnessState.INTERRUPTED


def test_a_repository_git_could_not_enumerate_is_unavailable(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed sync indexed nothing, so what is stored belongs to a previous run.

    Distinct from ``INTERRUPTED``, which suggests a run that merely needs
    finishing.
    """
    freshness = _only(workspace, monkeypatch, sync_status=SyncStatus.FAILED)
    assert freshness.state is FreshnessState.UNAVAILABLE


def test_a_git_state_that_could_not_be_established_is_unknown(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git answered, and the answer was that it does not know."""
    freshness = _only(workspace, monkeypatch, known=False)
    assert freshness.state is FreshnessState.UNKNOWN
    assert freshness.working_tree_dirty is None


def test_a_missing_head_is_a_moved_head_not_an_unknown_one(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty repository has no HEAD, and that is a genuine mismatch.

    Pinned because the tempting reading is the other one: ``head_sha=None`` looks
    like ignorance, and treating it as such would report an unborn branch as
    "unknown" when RepoAtlas can see perfectly well that it no longer matches what
    was indexed.
    """
    freshness = _only(workspace, monkeypatch, head=None)
    assert freshness.state is FreshnessState.STALE_HEAD
    assert freshness.head_matches is False


def test_a_moved_head_is_stale(workspace: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    freshness = _only(workspace, monkeypatch, head="b" * 40)
    assert freshness.state is FreshnessState.STALE_HEAD


def test_a_clean_matching_tree_is_current(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    freshness = _only(workspace, monkeypatch)
    assert freshness.state is FreshnessState.CURRENT_CLEAN
    assert freshness.working_tree_content is WorktreeContentStatus.MATCHES_INDEX
    assert freshness.content_verified_current is True


def test_a_changed_dirty_flag_is_stale(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    freshness = _only(workspace, monkeypatch, indexed_dirty=False, dirty=True)
    assert freshness.state is FreshnessState.STALE_WORKTREE


def test_an_older_build_is_a_stale_build(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outdated files outrank a matching HEAD: the facts, not the commit, are old."""
    from repoatlas.capabilities import service as service_module

    # Patched where it is looked up: status resolves the facts version in its own
    # namespace, so patching the defining module would not reach this call.
    monkeypatch.setattr(service_module, "current_facts_version", lambda _ws: "schema:999")
    response = _service(initialized).status(StatusRequest(context=GLOBAL, include_drift=False))
    assert response.freshness.state is FreshnessState.STALE_BUILD
    assert all(r.outdated_files > 0 for r in response.freshness.repositories if r.outdated_files)


# ---------------------------------------------------------------------------
# The dirty-tree case, which V1's sentence understates
# ---------------------------------------------------------------------------


def test_a_dirty_tree_is_never_reported_as_content_current(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Indexed dirty, edited again, still dirty, HEAD unmoved.

    Both booleans V1 compares are unchanged, so its sentence reads "current (dirty
    working tree)". The bytes are not current, and nothing hashed them, so the
    typed answer says HEAD matches and stops there.
    """
    freshness = _only(workspace, monkeypatch, indexed_dirty=True, dirty=True)
    assert freshness.state is FreshnessState.DIRTY
    assert freshness.head_matches is True
    assert freshness.working_tree_dirty is True
    assert freshness.working_tree_content is WorktreeContentStatus.UNKNOWN
    assert freshness.content_verified_current is False
    # The V1 sentence is carried for continuity and is deliberately less precise
    # than the model beside it. It is not a discriminator.
    assert freshness.detail == "current (dirty working tree)"


def test_the_dirty_case_survives_a_real_edit_after_a_real_sync(
    initialized: Workspace, workspace_root: Path
) -> None:
    """The same case against a real git repository, not a constructed row."""
    from repoatlas.services.sync_service import sync

    target = workspace_root / "python-service" / "src" / "config.py"
    target.write_text(target.read_text(encoding="utf-8") + "\n# first edit\n", encoding="utf-8")
    context = initialized.resolver().resolve(workspace_root)
    sync(initialized, context)

    target.write_text(target.read_text(encoding="utf-8") + "\n# second edit\n", encoding="utf-8")

    response = _service(initialized).status(StatusRequest(context=GLOBAL, include_drift=False))
    by_slug = {r.repository_slug: r for r in response.freshness.repositories}
    dirty = by_slug["python-service"]

    assert dirty.state is FreshnessState.DIRTY
    assert dirty.working_tree_content is WorktreeContentStatus.UNKNOWN
    assert dirty.content_verified_current is False
    assert dirty.detail == "current (dirty working tree)"


def test_no_response_field_asserts_content_currency_for_a_dirty_tree(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Belt and braces: the whole serialized answer is searched for the claim."""
    _register(workspace, indexed_dirty=True)
    _fake_git(monkeypatch, dirty=True)
    response = _service(workspace).status(StatusRequest(context=GLOBAL, include_drift=False))

    (freshness,) = response.freshness.repositories
    assert freshness.content_verified_current is False
    assert WorktreeContentStatus.MATCHES_INDEX.value not in response.model_dump_json()


# ---------------------------------------------------------------------------
# Scope and drift
# ---------------------------------------------------------------------------


def test_global_status_covers_every_registered_repository(initialized: Workspace) -> None:
    from tests.conftest import MANAGED_REPOSITORY_COUNT

    response = _service(initialized).status(StatusRequest(context=GLOBAL, include_drift=False))
    assert len(response.freshness.repositories) == MANAGED_REPOSITORY_COUNT
    assert response.counts.repositories == MANAGED_REPOSITORY_COUNT


def test_repository_status_covers_only_the_selected_repository(initialized: Workspace) -> None:
    response = _service(initialized).status(
        StatusRequest(context=_repository_scope("python-service"), include_drift=False)
    )
    assert [r.repository_slug for r in response.freshness.repositories] == ["python-service"]
    assert response.counts.repositories == 1


def test_drift_is_not_evaluated_unless_it_is_asked_for(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A discovery walk is the only cost that grows with the tree, so it is opt-in."""
    from repoatlas.services import freshness_service

    def forbidden(*_: object, **__: object) -> object:
        raise AssertionError("status walked the managed root without being asked")

    monkeypatch.setattr(freshness_service, "plan_repository_set", forbidden)
    response = _service(initialized).status(StatusRequest(context=GLOBAL, include_drift=False))

    assert response.drift_checked is False
    assert response.drift_detected is False
    assert response.freshness.repository_set_drifted is None


def test_unevaluated_drift_is_not_a_finding_of_no_drift(
    initialized: Workspace, workspace_root: Path
) -> None:
    """The correction, stated as directly as it can be.

    A repository the index has never heard of is sitting beneath the managed root.
    With ``include_drift=False`` every drift collection comes back empty — and that
    emptiness means the walk did not happen, not that the root is clean. The flag
    beside them is the only thing that can tell those apart, and without it a
    caller reads the stronger claim. Same principle as ``STORED`` freshness
    declining to report ``CURRENT``.
    """
    from tests.conftest import init_git_repository

    newcomer = workspace_root / "late-arrival"
    newcomer.mkdir()
    (newcomer / "main.py").write_text("x = 1\n", encoding="utf-8")
    init_git_repository(newcomer)

    unchecked = _service(initialized).status(StatusRequest(context=GLOBAL, include_drift=False))
    assert unchecked.drift_checked is False
    assert unchecked.new_repositories == ()

    checked = _service(initialized).status(StatusRequest(context=GLOBAL, include_drift=True))
    assert checked.drift_checked is True
    assert "late-arrival" in checked.new_repositories

    # Identical empty-looking collections, opposite meanings. Only the flag separates them.
    assert unchecked.new_repositories == () and checked.new_repositories != ()


def test_evaluated_drift_with_nothing_found_is_a_finding(initialized: Workspace) -> None:
    """``drift_checked=True`` with empty collections means evaluated and clean."""
    response = _service(initialized).status(StatusRequest(context=GLOBAL, include_drift=True))

    assert response.drift_checked is True
    assert response.drift_detected is False
    assert response.new_repositories == ()
    assert response.missing_repositories == ()
    assert response.relocated_repositories == ()
    assert response.conflicting_repositories == ()
    assert response.unreadable_paths == ()
    assert response.freshness.repository_set_drifted is False


def test_an_unregistered_repository_lands_in_the_new_collection(
    initialized: Workspace, workspace_root: Path
) -> None:
    from tests.conftest import init_git_repository

    newcomer = workspace_root / "late-arrival"
    newcomer.mkdir()
    (newcomer / "main.py").write_text("x = 1\n", encoding="utf-8")
    init_git_repository(newcomer)

    response = _service(initialized).status(StatusRequest(context=GLOBAL, include_drift=True))
    assert response.drift_checked is True
    assert response.drift_detected is True
    assert response.new_repositories == ("late-arrival",)
    assert response.freshness.repository_set_drifted is True


def test_a_removed_repository_lands_in_the_missing_collection(
    initialized: Workspace, workspace_root: Path
) -> None:
    import shutil

    shutil.rmtree(workspace_root / "scripts")

    response = _service(initialized).status(StatusRequest(context=GLOBAL, include_drift=True))
    assert response.drift_checked is True
    assert response.drift_detected is True
    assert "scripts" in response.missing_repositories


def test_detected_drift_always_names_something(
    initialized: Workspace, workspace_root: Path
) -> None:
    """``drift_detected`` and the collections cannot disagree.

    The invariant that makes the collections trustworthy: if drift was found, at
    least one collection says what it was. ``relocated_repositories`` is reported
    for exactly this reason — a moved managed root counts as drift, and without it
    this could be True with every collection empty.
    """
    from tests.conftest import init_git_repository

    newcomer = workspace_root / "late-arrival"
    newcomer.mkdir()
    (newcomer / "main.py").write_text("x = 1\n", encoding="utf-8")
    init_git_repository(newcomer)

    response = _service(initialized).status(StatusRequest(context=GLOBAL, include_drift=True))
    named = (
        response.new_repositories
        + response.missing_repositories
        + response.relocated_repositories
        + response.conflicting_repositories
    )
    assert response.drift_detected is bool(named)


def test_drift_is_global_at_global_scope(initialized: Workspace, workspace_root: Path) -> None:
    from tests.conftest import init_git_repository

    newcomer = workspace_root / "late-arrival"
    newcomer.mkdir()
    (newcomer / "main.py").write_text("x = 1\n", encoding="utf-8")
    init_git_repository(newcomer)

    response = _service(initialized).status(StatusRequest(context=GLOBAL, include_drift=True))
    assert response.drift_is_global is True


def test_drift_at_repository_scope_is_the_managed_roots_drift_and_says_so(
    initialized: Workspace, workspace_root: Path
) -> None:
    """Option A, pinned.

    A repository-scoped request that asks for drift gets the **managed root's**
    drift, because the root is not a property of any repository beneath it. This
    matches what V1's status already does, and it is the behaviour Slice A's
    freshness service implements. The risk in that choice is a caller reading a
    global fact as a statement about the one repository it asked about, so the
    response says which it is rather than leaving it to be inferred.
    """
    from tests.conftest import init_git_repository

    newcomer = workspace_root / "late-arrival"
    newcomer.mkdir()
    (newcomer / "main.py").write_text("x = 1\n", encoding="utf-8")
    init_git_repository(newcomer)

    response = _service(initialized).status(
        StatusRequest(context=_repository_scope("python-service"), include_drift=True)
    )

    # The scope narrowed the per-repository answers...
    assert [r.repository_slug for r in response.freshness.repositories] == ["python-service"]
    # ...and did not narrow the drift, which is labelled global for that reason.
    assert response.drift_checked is True
    assert response.new_repositories == ("late-arrival",)
    assert response.drift_is_global is True


def test_drift_does_not_change_the_in_scope_freshness_aggregate(
    initialized: Workspace, workspace_root: Path
) -> None:
    """Two differently-scoped facts, reported separately and never merged.

    Every registered repository is genuinely current, so the in-scope aggregate
    says ``CURRENT_CLEAN`` and is right to. The managed root holds one the index
    has never heard of, so drift is reported beside it. There is deliberately no
    single boolean folding the two together: an agent reading one would not know
    which it had.
    """
    from tests.conftest import init_git_repository

    newcomer = workspace_root / "late-arrival"
    newcomer.mkdir()
    (newcomer / "main.py").write_text("x = 1\n", encoding="utf-8")
    init_git_repository(newcomer)

    response = _service(initialized).status(StatusRequest(context=GLOBAL, include_drift=True))

    assert all(r.state is FreshnessState.CURRENT_CLEAN for r in response.freshness.repositories)
    assert response.freshness.state is FreshnessState.CURRENT_CLEAN
    assert response.drift_detected is True


def test_a_workspace_with_no_root_reports_drift_as_unevaluated(workspace: Workspace) -> None:
    """There is nothing to have drifted from, which is not "nothing drifted"."""
    response = _service(workspace).status(StatusRequest(context=GLOBAL, include_drift=True))
    assert response.root_configured is False
    assert response.drift_checked is False
    assert response.drift_detected is False


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_counts_are_index_counts_for_the_scope(initialized: Workspace) -> None:
    response = _service(initialized).status(StatusRequest(context=GLOBAL, include_drift=False))
    every_id = [r.id for r in initialized.repositories.list_all() if r.id is not None]
    assert response.counts.indexed_files == initialized.files.count(every_id)
    assert response.counts.symbols == initialized.knowledge.count_entities(every_id)
    assert response.counts.relationships == initialized.knowledge.count_edges(every_id)
    assert response.counts.indexed_files > 0


def test_repository_scope_counts_only_that_repository(initialized: Workspace) -> None:
    response = _service(initialized).status(
        StatusRequest(context=_repository_scope("python-service"), include_drift=False)
    )
    repo = next(r for r in initialized.repositories.list_all() if r.slug == "python-service")
    assert repo.id is not None
    assert response.counts.indexed_files == initialized.files.count([repo.id])
    assert response.counts.indexed_files > 0


def test_an_unconfigured_workspace_has_no_root(workspace: Workspace) -> None:
    """No managed root means there is nothing to be current *about*.

    A different condition from an index that is out of date, and one a caller has
    to be able to tell apart: the remedy is ``init``, not ``sync``.
    """
    response = _service(workspace).status(StatusRequest(context=GLOBAL, include_drift=False))
    assert response.root_configured is False
    assert response.freshness.state is FreshnessState.NOT_INDEXED


def test_a_configured_workspace_reports_its_root(initialized: Workspace) -> None:
    response = _service(initialized).status(StatusRequest(context=GLOBAL, include_drift=False))
    assert response.root_configured is True


def test_last_sync_is_the_most_recent_across_the_scope(initialized: Workspace) -> None:
    response = _service(initialized).status(StatusRequest(context=GLOBAL, include_drift=False))
    stamps = [r.last_indexed_at for r in initialized.repositories.list_all() if r.last_indexed_at]
    assert response.last_sync == max(stamps)


def test_the_response_carries_no_surrogate_id(initialized: Workspace) -> None:
    import json

    from tests.capabilities.test_repositories import SURROGATE_KEYS, _keys_anywhere

    response = _service(initialized).status(StatusRequest(context=GLOBAL, include_drift=True))
    payload = json.loads(response.model_dump_json())
    assert not (_keys_anywhere(payload) & SURROGATE_KEYS)


def test_callers_branch_on_the_enum_not_on_the_sentence(initialized: Workspace) -> None:
    """``detail`` is V1's prose. Every discriminator beside it is typed."""
    response = _service(initialized).status(StatusRequest(context=GLOBAL, include_drift=False))
    for freshness in response.freshness.repositories:
        assert isinstance(freshness.state, FreshnessState)
        assert isinstance(freshness.working_tree_content, WorktreeContentStatus)
        assert isinstance(freshness.detail, str)


# ---------------------------------------------------------------------------
# Read-only
# ---------------------------------------------------------------------------


def test_neither_capability_writes_the_index(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Startup may migrate and set up WAL. A capability call may not write at all.

    The baseline is captured **after** the service is open, so the database
    lifecycle is excluded and only the calls themselves are under test. Spies sit
    at the semantic boundaries rather than on the file, because WAL checkpointing
    makes filesystem metadata a poor detector of whether anything was written.
    """
    service = _service(initialized)

    def forbid(name: str):
        def spy(*_: object, **__: object) -> object:
            raise AssertionError(f"a read capability called {name}")

        return spy

    monkeypatch.setattr(Workspace, "writer_lock", forbid("writer_lock"))
    monkeypatch.setattr(Workspace, "sync_engine", forbid("sync_engine"))
    monkeypatch.setattr(initialized.repositories, "upsert", forbid("repositories.upsert"))
    monkeypatch.setattr(initialized.repositories, "delete", forbid("repositories.delete"))
    monkeypatch.setattr(initialized.repositories, "mark_sync_started", forbid("mark_sync_started"))
    monkeypatch.setattr(initialized.repositories, "mark_sync_failed", forbid("mark_sync_failed"))
    monkeypatch.setattr(initialized.repositories, "mark_many_synced", forbid("mark_many_synced"))
    monkeypatch.setattr(initialized.files, "upsert", forbid("files.upsert"))
    monkeypatch.setattr(initialized.files, "delete_many", forbid("files.delete_many"))
    monkeypatch.setattr(
        initialized.knowledge, "replace_file_facts", forbid("knowledge.replace_file_facts")
    )
    monkeypatch.setattr(initialized.knowledge, "add_edges", forbid("knowledge.add_edges"))
    monkeypatch.setattr(initialized.knowledge, "intern_entity", forbid("knowledge.intern_entity"))

    service.repositories(RepositoriesRequest(context=GLOBAL))
    service.status(StatusRequest(context=GLOBAL))
    service.repositories(RepositoriesRequest(context=GLOBAL, depth=FreshnessDepth.LIVE))
    service.status(StatusRequest(context=GLOBAL, include_drift=True))


def test_the_indexed_facts_are_byte_identical_afterwards(initialized: Workspace) -> None:
    """The secondary check: a logical snapshot of every fact table, not an mtime."""
    from tests.conftest import fact_snapshot

    service = _service(initialized)
    before = fact_snapshot(initialized)

    service.repositories(RepositoriesRequest(context=GLOBAL))
    service.status(StatusRequest(context=GLOBAL, include_drift=True))
    service.repositories(RepositoriesRequest(context=GLOBAL, depth=FreshnessDepth.LIVE))
    service.status(StatusRequest(context=GLOBAL, depth=FreshnessDepth.STORED))

    assert fact_snapshot(initialized) == before


# ---------------------------------------------------------------------------
# No investigation side effects
# ---------------------------------------------------------------------------


def test_reading_the_workspace_creates_no_investigation(initialized: Workspace) -> None:
    """These are workspace queries. Evidence identity belongs to search."""
    service = _service(initialized)
    assert len(service.sessions) == 0

    service.repositories(RepositoriesRequest(context=GLOBAL))
    service.status(StatusRequest(context=GLOBAL))
    service.repositories(RepositoriesRequest(context=GLOBAL))
    service.status(StatusRequest(context=GLOBAL))

    assert len(service.sessions) == 0


def test_existing_investigations_are_left_structurally_unchanged(
    initialized: Workspace,
) -> None:
    """Not even a touched clock: these calls are not investigation steps."""
    service = _service(initialized)
    first = service.sessions.create()
    second = service.sessions.create()

    before = {
        session.id: (session.last_used_at, len(session.ledger), session.ledger.summary())
        for session in (first, second)
    }

    service.repositories(RepositoriesRequest(context=GLOBAL))
    service.status(StatusRequest(context=GLOBAL))

    after = {
        session.id: (session.last_used_at, len(session.ledger), session.ledger.summary())
        for session in (first, second)
    }
    assert after == before
    assert len(service.sessions) == 2


def test_no_evidence_id_is_allocated_by_a_read(initialized: Workspace) -> None:
    service = _service(initialized)
    session = service.sessions.create()

    service.repositories(RepositoriesRequest(context=GLOBAL))
    service.status(StatusRequest(context=GLOBAL))

    assert session.ledger.entries() == ()
    assert session.ledger.available == session.ledger.capacity


# ---------------------------------------------------------------------------
# Schema guards
# ---------------------------------------------------------------------------


def test_the_status_response_has_no_index_current_field() -> None:
    """Removed deliberately, and kept removed.

    It combined the in-scope freshness aggregate with repository-set drift, which
    are differently scoped, and it treated unchecked drift as absent drift — so a
    call that never walked the root could report the index current. Both halves are
    now separate structured facts.
    """
    assert "index_current" not in StatusResponse.model_fields
    response = StatusResponse(
        context=GLOBAL,
        scope=CapabilityScope.GLOBAL,
        freshness=IndexFreshness(depth=FreshnessDepth.LIVE, state=FreshnessState.NOT_INDEXED),
    )
    assert "index_current" not in response.model_dump()
    assert "index_current" not in response.model_dump_json()


def test_no_status_boolean_turns_an_unchecked_fact_into_a_true_one() -> None:
    """A guard on the shape, so the next aggregate boolean is a decision.

    Every boolean on the response is enumerated here. Adding one fails this test
    until somebody states what it claims and what was actually checked to support
    it — which is the review step the removed field skipped.
    """
    booleans = {
        name for name, field in StatusResponse.model_fields.items() if field.annotation is bool
    }
    assert booleans == {
        # Whether a managed root is configured. Read from settings, always known.
        "root_configured",
        # Whether the root was walked on this call.
        "drift_checked",
        # A finding, meaningful only when drift_checked.
        "drift_detected",
        # A statement about what the drift collections describe, not a finding.
        "drift_is_global",
    }


def test_every_drift_finding_is_false_when_nothing_was_checked(initialized: Workspace) -> None:
    """The default posture is "not evaluated", never "evaluated and clean"."""
    response = _service(initialized).status(StatusRequest(context=GLOBAL, include_drift=False))
    assert response.drift_checked is False
    assert response.drift_detected is False
    assert response.new_repositories == ()
    assert response.missing_repositories == ()
    assert response.relocated_repositories == ()
    assert response.conflicting_repositories == ()
    assert response.unreadable_paths == ()
