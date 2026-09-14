"""Explicit sync — the one capability that is meant to change the index.

Everything here pins one of two things.

**Sync is V1's sync.** The capability calls
:func:`repoatlas.services.sync_service.sync` exactly once and projects what it
returns. It does not plan the repository set, walk a tree, hash a file, parse,
link or mark anything synchronized itself, because each of those restated here
would be a second interpretation of which files belong to which repository — the
precise divergence that lets an index admit a file V1 refuses. The spy tests below
pin the single call; the parity test pins that every count is V1's own.

**Sync is local.** It observes the working trees as they stand and brings the
index in line with them. A dirty tree is indexed as it is, never cleaned; nothing
is pulled, fetched or checked out; and the repository source is byte-identical
afterwards. Selection is the explicit context and nothing else — no working
directory, no enclosing repository, no implicit ``init`` when there is no root.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from repoatlas.capabilities.errors import capability_error
from repoatlas.capabilities.service import CapabilityService
from repoatlas.errors import ContextResolutionError, NotInitializedError
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.freshness import FreshnessDepth
from repoatlas.models.reports import SyncReport
from repoatlas.models.repository import FileState
from repoatlas.models.sync import (
    RevalidationStatus,
    SyncRequest,
    SyncResponse,
    SyncStatus,
)
from repoatlas.services import sync_service
from repoatlas.services.workspace import Workspace
from tests.conftest import init_git_repository

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)
SERVICE = "python-service"
DEPLOYMENT = "deployment"
CONFIG = "src/config.py"


def _repo(slug: str) -> CapabilityContext:
    return CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=slug)


def _sync(
    service: CapabilityService, context: CapabilityContext = GLOBAL, **kwargs: object
) -> SyncResponse:
    return service.sync(SyncRequest(context=context, **kwargs))  # type: ignore[arg-type]


def _tree(root: Path) -> dict[str, bytes]:
    """Every working-tree file beneath ``root``, excluding git's own metadata.

    ``.git`` is excluded because reading repository state legitimately refreshes
    git's stat cache there. What sync must never touch is the source.
    """
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def _state(workspace: Workspace, slug: str, path: str) -> FileState | None:
    repo = next(r for r in workspace.repositories.list_all() if r.slug == slug)
    assert repo.id is not None
    return workspace.files.states_for(repo.id, [path]).get(path)


def _slugs(workspace: Workspace) -> list[str]:
    return sorted(repo.slug for repo in workspace.repositories.list_all())


def _view(response: SyncResponse, slug: str):
    return next(view for view in response.repositories if view.repository_slug == slug)


def _forbid(name: str):
    def refuse(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"{name} was reached before the request was refused")

    return refuse


# ---------------------------------------------------------------------------
# CASE 1 — an ordinary sync
# ---------------------------------------------------------------------------


def test_a_changed_file_enters_the_index(initialized: Workspace, workspace_root: Path) -> None:
    """CASE 1. The edit is indexed, and the response says so in V1's own counts."""
    service = CapabilityService(initialized)
    before = _state(initialized, SERVICE, CONFIG)
    assert before is not None
    edited = workspace_root / SERVICE / CONFIG
    edited.write_text(edited.read_text(encoding="utf-8") + "\nADDED_BY_SYNC_TEST = 1\n")

    response = _sync(service)

    assert response.status is SyncStatus.COMPLETED
    view = _view(response, SERVICE)
    assert (view.changed, view.new, view.deleted) == (1, 0, 0)
    after = _state(initialized, SERVICE, CONFIG)
    assert after is not None
    assert after.content_hash != before.content_hash


def test_every_count_is_the_one_v1_reported(
    initialized: Workspace, workspace_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A projection, not a second opinion. Nothing here was re-scanned."""
    captured: list[SyncReport] = []
    real = sync_service.sync

    def spy(*args: object, **kwargs: object) -> SyncReport:
        report = real(*args, **kwargs)  # type: ignore[arg-type]
        captured.append(report)
        return report

    monkeypatch.setattr(sync_service, "sync", spy)
    edited = workspace_root / SERVICE / CONFIG
    edited.write_text(edited.read_text(encoding="utf-8") + "\nPARITY = 2\n")

    response = _sync(CapabilityService(initialized))

    (report,) = captured
    assert len(response.repositories) == len(report.repositories)
    for view, original in zip(response.repositories, report.repositories, strict=True):
        assert view.repository_name == original.repository
        assert (view.unchanged, view.changed, view.new, view.deleted) == (
            original.unchanged,
            original.changed,
            original.new,
            original.deleted,
        )
        assert (view.files_indexed, view.symbols, view.relationships, view.dependencies) == (
            original.files_indexed,
            original.symbols,
            original.relationships,
            original.dependencies,
        )
        assert view.is_dirty == original.is_dirty
        assert view.enumeration_failed == original.enumeration_failed
    assert response.files_indexed == report.files_indexed
    assert response.symbols == report.symbols
    assert response.relationships == report.relationships
    assert response.dependencies == report.dependencies
    assert response.linked_references == report.linked_references
    assert response.unresolved_references == report.unresolved_references
    assert response.cross_repository_edges == report.cross_repository_edges


def test_every_report_carries_its_repository_identity(initialized: Workspace) -> None:
    """V1 names repositories by display name. The response names them by slug."""
    response = _sync(CapabilityService(initialized))

    assert response.repositories
    assert all(view.repository_slug for view in response.repositories)
    assert {view.repository_slug for view in response.repositories} == set(_slugs(initialized))


# ---------------------------------------------------------------------------
# CASE 2 — nothing to do
# ---------------------------------------------------------------------------


def test_a_sync_with_nothing_to_do_invents_no_change(initialized: Workspace) -> None:
    """CASE 2. Succeeds, and every changed/new/deleted count is zero."""
    response = _sync(CapabilityService(initialized))

    assert response.status is SyncStatus.COMPLETED
    for view in response.repositories:
        assert (view.changed, view.new, view.deleted) == (0, 0, 0), view.repository_slug
    assert response.repositories_added == ()
    assert response.repositories_unavailable == ()


# ---------------------------------------------------------------------------
# CASE 3 — dirty working trees
# ---------------------------------------------------------------------------


def test_a_dirty_working_tree_is_indexed_as_it_stands(
    initialized: Workspace, workspace_root: Path
) -> None:
    """CASE 3. The uncommitted edit is indexed, and nothing is cleaned or stashed."""
    edited = workspace_root / SERVICE / CONFIG
    edited.write_text(edited.read_text(encoding="utf-8") + "\nUNCOMMITTED = 3\n")
    before_tree = _tree(workspace_root)
    before_state = _state(initialized, SERVICE, CONFIG)

    response = _sync(CapabilityService(initialized))

    assert _view(response, SERVICE).is_dirty is True
    assert _tree(workspace_root) == before_tree
    after_state = _state(initialized, SERVICE, CONFIG)
    assert before_state is not None and after_state is not None
    assert after_state.content_hash != before_state.content_hash
    porcelain = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=workspace_root / SERVICE,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert CONFIG in porcelain, "the uncommitted change must still be uncommitted"


# ---------------------------------------------------------------------------
# CASE 4 — no index to sync
# ---------------------------------------------------------------------------


def test_an_uninitialized_workspace_is_refused_and_not_initialized(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CASE 4. ``NOT_INITIALIZED``, and no implicit ``init`` of anything.

    Without this guard V1's sync would run over an empty repository set and still
    commit a global link pass — a write to an index nobody asked to create, and a
    success reported for a workspace that has no root.
    """
    monkeypatch.setattr(Workspace, "writer_lock", _forbid("the writer lock"))
    monkeypatch.setattr(sync_service, "sync", _forbid("V1 sync"))
    service = CapabilityService(workspace)

    with pytest.raises(NotInitializedError) as raised:
        _sync(service)

    assert capability_error(raised.value).code == "NOT_INITIALIZED"
    assert workspace.root is None
    assert workspace.repositories.list_all() == []
    assert len(service.sessions) == 0


def test_sync_never_consults_the_working_directory(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The caller and this process do not share a working directory."""

    def no_cwd() -> Path:
        raise AssertionError("sync consulted the working directory")

    monkeypatch.setattr(Path, "cwd", staticmethod(no_cwd))

    response = _sync(CapabilityService(initialized), _repo(SERVICE))

    assert [view.repository_slug for view in response.repositories] == [SERVICE]


# ---------------------------------------------------------------------------
# CASE 5 — explicit repository selection
# ---------------------------------------------------------------------------


def test_an_explicit_repository_syncs_only_that_repository(
    initialized: Workspace, workspace_root: Path
) -> None:
    """CASE 5. V1's repository-scoped semantics: one repository, nothing else."""
    for slug in (SERVICE, DEPLOYMENT):
        target = next(
            p for p in (workspace_root / slug).rglob("*") if p.is_file() and ".git" not in p.parts
        )
        target.write_text(target.read_text(encoding="utf-8", errors="replace") + "\n# edited\n")
    untouched = next(
        p.relative_to(workspace_root / DEPLOYMENT).as_posix()
        for p in (workspace_root / DEPLOYMENT).rglob("*")
        if p.is_file() and ".git" not in p.parts
    )
    before_other = _state(initialized, DEPLOYMENT, untouched)

    response = _sync(CapabilityService(initialized), _repo(SERVICE))

    assert [view.repository_slug for view in response.repositories] == [SERVICE]
    assert _state(initialized, DEPLOYMENT, untouched) == before_other


def test_a_repository_scoped_sync_registers_no_other_repository(
    initialized: Workspace, workspace_root: Path
) -> None:
    """V1: a scoped run never creates a row outside its scope."""
    late = workspace_root / "late-arrival"
    late.mkdir()
    (late / "late.py").write_text("LATE = 1\n", encoding="utf-8")
    init_git_repository(late)
    before = _slugs(initialized)

    response = _sync(CapabilityService(initialized), _repo(SERVICE))

    assert response.repositories_added == ()
    assert _slugs(initialized) == before


def test_a_global_sync_registers_a_repository_created_after_init(
    initialized: Workspace, workspace_root: Path
) -> None:
    """V1: a run covering the managed root reconciles membership, then indexes it."""
    late = workspace_root / "late-arrival"
    late.mkdir()
    (late / "late.py").write_text("LATE = 1\n", encoding="utf-8")
    init_git_repository(late)

    response = _sync(CapabilityService(initialized))

    assert response.repositories_added == ("late-arrival",)
    assert "late-arrival" in _slugs(initialized)
    assert _view(response, "late-arrival").new >= 1


def test_a_global_sync_retires_a_repository_whose_directory_is_gone(
    initialized: Workspace, workspace_root: Path
) -> None:
    """V1: a repository that is no longer at its path is no longer that repository."""
    subprocess.run(["rm", "-rf", str(workspace_root / "ts-service")], check=True)

    response = _sync(CapabilityService(initialized))

    assert "ts-service" not in _slugs(initialized)
    assert all(view.repository_slug != "ts-service" for view in response.repositories)


def test_a_repository_scoped_sync_retires_nothing_outside_its_scope(
    initialized: Workspace, workspace_root: Path
) -> None:
    subprocess.run(["rm", "-rf", str(workspace_root / "ts-service")], check=True)

    _sync(CapabilityService(initialized), _repo(SERVICE))

    assert "ts-service" in _slugs(initialized)


# ---------------------------------------------------------------------------
# CASE 6 — refused before anything changes
# ---------------------------------------------------------------------------


def test_an_unknown_repository_is_refused_before_anything_changes(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CASE 6. Resolution fails before the writer lock is so much as requested."""
    monkeypatch.setattr(Workspace, "writer_lock", _forbid("the writer lock"))
    monkeypatch.setattr(sync_service, "sync", _forbid("V1 sync"))
    service = CapabilityService(initialized)
    generation = service.generation

    with pytest.raises(ContextResolutionError) as raised:
        _sync(service, _repo("no-such-repository"))

    # Slice A's mapping: an unknown selector is NOT_FOUND, an ambiguous one CONTEXT.
    assert capability_error(raised.value).category.value == "not_found"
    assert service.generation == generation


def test_an_ambiguous_repository_is_refused_before_anything_changes(
    initialized: Workspace, workspace_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two repositories share a display name. RepoAtlas refuses rather than choosing."""
    for team in ("team-a", "team-b"):
        twin = workspace_root / team / "shared-name"
        twin.mkdir(parents=True)
        (twin / "twin.py").write_text(f"TEAM = {team!r}\n", encoding="utf-8")
        init_git_repository(twin)
    _sync(CapabilityService(initialized))
    assert {"team-a/shared-name", "team-b/shared-name"} <= set(_slugs(initialized))

    monkeypatch.setattr(Workspace, "writer_lock", _forbid("the writer lock"))
    monkeypatch.setattr(sync_service, "sync", _forbid("V1 sync"))

    with pytest.raises(ContextResolutionError) as raised:
        _sync(CapabilityService(initialized), _repo("shared-name"))

    mapped = capability_error(raised.value)
    assert mapped.category.value == "context"


def test_reports_are_attributed_correctly_when_display_names_collide(
    initialized: Workspace, workspace_root: Path
) -> None:
    """The one case where naming a report by display name would pick the wrong tree."""
    for team in ("team-a", "team-b"):
        twin = workspace_root / team / "shared-name"
        twin.mkdir(parents=True)
        (twin / "twin.py").write_text(f"TEAM = {team!r}\n", encoding="utf-8")
        init_git_repository(twin)
    _sync(CapabilityService(initialized))
    edited = workspace_root / "team-b" / "shared-name" / "twin.py"
    edited.write_text("TEAM = 'team-b, edited'\n", encoding="utf-8")

    response = _sync(CapabilityService(initialized), _repo("team-b/shared-name"))

    assert [view.repository_slug for view in response.repositories] == ["team-b/shared-name"]
    assert response.repositories[0].changed == 1


# ---------------------------------------------------------------------------
# One orchestration owner
# ---------------------------------------------------------------------------


def test_v1_sync_is_called_exactly_once(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One owner for the lifecycle. A second call would double every write."""
    calls: list[dict[str, object]] = []
    real = sync_service.sync

    def spy(*args: object, **kwargs: object) -> SyncReport:
        calls.append(dict(kwargs))
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(sync_service, "sync", spy)

    _sync(CapabilityService(initialized), full=True)

    assert len(calls) == 1
    assert calls[0].get("full") is True


def test_full_reaches_v1_and_rehashes_every_file(initialized: Workspace) -> None:
    response = _sync(CapabilityService(initialized), full=True)

    assert response.full is True
    assert all(view.full_rehash for view in response.repositories if not view.enumeration_failed)


def test_the_request_offers_no_option_v1_does_not_safely_support() -> None:
    """No pull, fetch, checkout, reset, prune, force or model download."""
    assert set(SyncRequest.model_fields) == {"context", "full", "depth"}


# ---------------------------------------------------------------------------
# CASE 22 — the source is never modified
# ---------------------------------------------------------------------------


def test_sync_never_modifies_repository_source(
    initialized: Workspace, workspace_root: Path
) -> None:
    """CASE 22. Byte-identical working trees before and after."""
    before = _tree(workspace_root)

    _sync(CapabilityService(initialized), full=True)

    assert _tree(workspace_root) == before


# ---------------------------------------------------------------------------
# Investigations and the generation hint
# ---------------------------------------------------------------------------


def test_sync_creates_no_investigation(initialized: Workspace) -> None:
    """Workspace mutation, not an investigation step."""
    service = CapabilityService(initialized)
    assert len(service.sessions) == 0

    _sync(service)

    assert len(service.sessions) == 0


def test_the_generation_moves_exactly_once_per_successful_sync(initialized: Workspace) -> None:
    """An in-process hint that the index changed through this service. Not proof."""
    service = CapabilityService(initialized)
    start = service.generation

    _sync(service)
    assert service.generation == start + 1
    _sync(service)
    assert service.generation == start + 2


def test_a_response_with_no_live_investigation_rechecks_nothing(initialized: Workspace) -> None:
    response = _sync(CapabilityService(initialized))

    summary = response.evidence_revalidation
    assert summary.status is RevalidationStatus.COMPLETED
    assert summary.investigations_checked == 0
    assert summary.evidence_checked == 0
    assert summary.transitions == ()


# ---------------------------------------------------------------------------
# Freshness and privacy
# ---------------------------------------------------------------------------


def test_freshness_is_reported_at_the_requested_depth(initialized: Workspace) -> None:
    service = CapabilityService(initialized)

    assert _sync(service).freshness.depth is FreshnessDepth.LIVE
    assert _sync(service, depth=FreshnessDepth.STORED).freshness.depth is FreshnessDepth.STORED


def test_the_response_carries_no_host_path(initialized: Workspace, workspace_root: Path) -> None:
    """Slugs and root-relative paths only. No root, database, lock or temp path."""
    payload = json.dumps(json.loads(_sync(CapabilityService(initialized)).model_dump_json()))

    assert str(workspace_root) not in payload
    assert str(initialized.database.path) not in payload
    assert str(initialized.database.path.parent) not in payload
    for prefix in ("/tmp", "/home", "/Users"):  # noqa: S108 — matched, never opened
        assert f'"{prefix}' not in payload, prefix


def test_the_context_is_reported_by_identity(initialized: Workspace) -> None:
    response = _sync(CapabilityService(initialized), _repo(SERVICE))

    assert response.context.scope is CapabilityScope.REPOSITORY
    assert response.context.repositories == (SERVICE,)
    assert response.context.primary == SERVICE


def test_unreadable_directories_are_reported_relative_to_the_root(
    initialized: Workspace, workspace_root: Path
) -> None:
    """V1 names them by absolute path. The response names them from the root down."""
    locked = workspace_root / "locked-away"
    (locked / "inner").mkdir(parents=True)
    locked.chmod(0o000)
    try:
        response = _sync(CapabilityService(initialized))
    finally:
        locked.chmod(0o755)

    assert response.unreadable_paths == ("locked-away",)
    assert str(workspace_root) not in json.dumps(json.loads(response.model_dump_json()))
