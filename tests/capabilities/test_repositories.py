"""The ``repositories`` capability (V2 Slice C).

What RepoAtlas manages, answered without changing anything and without paying for
git unless asked. Three properties carry the suite:

* it lists the **registered** set and discovers nothing;
* identity is the **slug**, and no database row id escapes;
* the default depth is **cheap**, and cheapness is proved by making the expensive
  path fatal rather than by timing it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.errors import ContextResolutionError
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.capability_io import RepositoriesRequest
from repoatlas.models.freshness import FreshnessDepth, FreshnessState
from repoatlas.models.repository import Repository
from repoatlas.services.workspace import Workspace

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)

#: Keys that would mean a storage surrogate id reached a caller.
SURROGATE_KEYS = frozenset({"id", "repo_id", "repository_id", "file_id", "entity_id", "rowid"})


def _service(workspace: Workspace) -> CapabilityService:
    return CapabilityService(workspace)


def _repository_scope(selector: str) -> CapabilityContext:
    return CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=selector)


def _keys_anywhere(value: object) -> set[str]:
    """Every mapping key in a nested serialized structure."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(str(key))
            found |= _keys_anywhere(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            found |= _keys_anywhere(item)
    return found


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def test_global_scope_lists_every_registered_repository(initialized: Workspace) -> None:
    from tests.conftest import MANAGED_REPOSITORY_COUNT

    response = _service(initialized).repositories(RepositoriesRequest(context=GLOBAL))
    assert len(response.repositories) == MANAGED_REPOSITORY_COUNT
    assert response.scope is CapabilityScope.GLOBAL


def test_repository_scope_returns_exactly_the_resolved_repository(
    initialized: Workspace,
) -> None:
    response = _service(initialized).repositories(
        RepositoriesRequest(context=_repository_scope("python-service"))
    )
    assert [view.slug for view in response.repositories] == ["python-service"]
    assert response.scope is CapabilityScope.REPOSITORY


def test_an_unknown_repository_is_refused(initialized: Workspace) -> None:
    with pytest.raises(ContextResolutionError):
        _service(initialized).repositories(
            RepositoriesRequest(context=_repository_scope("does-not-exist"))
        )


def test_results_are_ordered_by_identity(initialized: Workspace) -> None:
    """Two calls against an unchanged index agree, and agree with each other."""
    service = _service(initialized)
    first = service.repositories(RepositoriesRequest(context=GLOBAL))
    second = service.repositories(RepositoriesRequest(context=GLOBAL))
    slugs = [view.slug for view in first.repositories]
    assert slugs == sorted(slugs)
    assert slugs == [view.slug for view in second.repositories]


# ---------------------------------------------------------------------------
# Registration boundary
# ---------------------------------------------------------------------------


def test_an_unregistered_repository_beneath_the_root_is_not_listed(
    initialized: Workspace, workspace_root: Path
) -> None:
    """Listing does not discover. Discovering here would make a read mutate.

    The new directory is real and beneath the managed root, so a listing that ran
    discovery would find it — and would then either register it, which is a write,
    or offer a name that ``--repo`` cannot select and no other capability can
    answer about. It belongs to ``status`` drift, which is where it is actionable.
    """
    from tests.conftest import init_git_repository

    newcomer = workspace_root / "late-arrival"
    newcomer.mkdir()
    (newcomer / "main.py").write_text("x = 1\n", encoding="utf-8")
    init_git_repository(newcomer)

    response = _service(initialized).repositories(RepositoriesRequest(context=GLOBAL))
    assert "late-arrival" not in {view.slug for view in response.repositories}


def test_listing_registers_nothing(initialized: Workspace, workspace_root: Path) -> None:
    from tests.conftest import init_git_repository

    newcomer = workspace_root / "late-arrival"
    newcomer.mkdir()
    (newcomer / "main.py").write_text("x = 1\n", encoding="utf-8")
    init_git_repository(newcomer)

    before = len(initialized.repositories.list_all())
    service = _service(initialized)
    service.repositories(RepositoriesRequest(context=GLOBAL))
    service.repositories(RepositoriesRequest(context=GLOBAL))
    assert len(initialized.repositories.list_all()) == before


# ---------------------------------------------------------------------------
# Identity and privacy
# ---------------------------------------------------------------------------


def test_identity_is_the_slug_and_never_a_row_id(initialized: Workspace) -> None:
    response = _service(initialized).repositories(RepositoriesRequest(context=GLOBAL))
    keys = _keys_anywhere(response.model_dump())
    assert not (keys & SURROGATE_KEYS), sorted(keys & SURROGATE_KEYS)
    assert all(view.slug for view in response.repositories)


def test_the_serialized_response_carries_no_surrogate_id(initialized: Workspace) -> None:
    """Checked on the JSON form too: that is what actually crosses a wire."""
    import json

    response = _service(initialized).repositories(
        RepositoriesRequest(context=GLOBAL, include_host_paths=True)
    )
    payload = json.loads(response.model_dump_json())
    assert not (_keys_anywhere(payload) & SURROGATE_KEYS)


def test_a_duplicate_display_name_is_reported_as_ambiguous(workspace: Workspace) -> None:
    """Two repositories may legitimately both be called ``common``.

    Computed from the registered set rather than guessed from the path shape, so
    the caller learns to pass a slug *before* being refused rather than after.
    """
    root = Path("/managed")
    workspace.repositories.upsert(
        Repository(name="common", slug="team-a/common", path=root / "team-a" / "common")
    )
    workspace.repositories.upsert(
        Repository(name="common", slug="team-b/common", path=root / "team-b" / "common")
    )
    workspace.repositories.upsert(
        Repository(name="service", slug="team-a/service", path=root / "team-a" / "service")
    )

    response = _service(workspace).repositories(RepositoriesRequest(context=GLOBAL))
    ambiguity = {view.slug: view.name_ambiguous for view in response.repositories}
    assert ambiguity == {
        "team-a/common": True,
        "team-b/common": True,
        "team-a/service": False,
    }


def test_ambiguity_is_a_property_of_the_registered_set_not_of_the_scope(
    workspace: Workspace,
) -> None:
    """A repository is ambiguously named even when it is the only one returned."""
    root = Path("/managed")
    workspace.repositories.upsert(
        Repository(name="common", slug="team-a/common", path=root / "team-a" / "common")
    )
    workspace.repositories.upsert(
        Repository(name="common", slug="team-b/common", path=root / "team-b" / "common")
    )

    response = _service(workspace).repositories(
        RepositoriesRequest(context=_repository_scope("team-a/common"))
    )
    (view,) = response.repositories
    assert view.name_ambiguous is True


def test_an_ambiguous_selector_is_refused_rather_than_resolved(workspace: Workspace) -> None:
    root = Path("/managed")
    workspace.repositories.upsert(
        Repository(name="common", slug="team-a/common", path=root / "team-a" / "common")
    )
    workspace.repositories.upsert(
        Repository(name="common", slug="team-b/common", path=root / "team-b" / "common")
    )

    with pytest.raises(ContextResolutionError) as caught:
        _service(workspace).repositories(RepositoriesRequest(context=_repository_scope("common")))
    assert "ambiguous" in caught.value.message


def test_host_paths_are_absent_by_default(initialized: Workspace) -> None:
    """A host path is this machine's layout, not a fact about the code."""
    response = _service(initialized).repositories(RepositoriesRequest(context=GLOBAL))
    assert all(view.host_path is None for view in response.repositories)


def test_host_paths_appear_only_when_asked_for(
    initialized: Workspace, workspace_root: Path
) -> None:
    response = _service(initialized).repositories(
        RepositoriesRequest(context=GLOBAL, include_host_paths=True)
    )
    assert all(view.host_path is not None for view in response.repositories)
    assert any(
        view.host_path == workspace_root / "python-service" for view in response.repositories
    )


# ---------------------------------------------------------------------------
# Counts
# ---------------------------------------------------------------------------


def test_counts_come_from_the_index(initialized: Workspace) -> None:
    """Compared against the stores directly, not recomputed by scanning files."""
    response = _service(initialized).repositories(RepositoriesRequest(context=GLOBAL))
    by_slug = {view.slug: view for view in response.repositories}

    for repo in initialized.repositories.list_all():
        assert repo.id is not None
        view = by_slug[repo.slug]
        assert view.indexed_files == initialized.files.count([repo.id])
        assert view.symbols == initialized.knowledge.count_entities([repo.id])
        assert view.relationships == initialized.knowledge.count_edges([repo.id])

    assert sum(view.indexed_files for view in response.repositories) > 0
    assert sum(view.symbols for view in response.repositories) > 0


def test_metadata_comes_from_the_repository_row(initialized: Workspace) -> None:
    response = _service(initialized).repositories(RepositoriesRequest(context=GLOBAL))
    by_slug = {view.slug: view for view in response.repositories}
    for repo in initialized.repositories.list_all():
        view = by_slug[repo.slug]
        assert view.name == repo.name
        assert view.branch == repo.branch
        assert view.head_sha == repo.head_sha
        assert view.languages == repo.languages
        assert view.manifests == repo.manifests
        assert view.lockfiles == repo.lockfiles
        assert view.last_indexed_at == repo.last_indexed_at


# ---------------------------------------------------------------------------
# Freshness cost
# ---------------------------------------------------------------------------


def test_the_default_depth_is_stored(initialized: Workspace) -> None:
    response = _service(initialized).repositories(RepositoriesRequest(context=GLOBAL))
    assert response.depth is FreshnessDepth.STORED
    assert all(view.freshness.depth is FreshnessDepth.STORED for view in response.repositories)


def test_stored_depth_never_invokes_git(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cheapness proved by making the expensive path fatal, not by timing it."""

    def forbidden(*_: object, **__: object) -> object:
        raise AssertionError("stored-depth freshness invoked git")

    monkeypatch.setattr(initialized.git, "state", forbidden)
    response = _service(initialized).repositories(RepositoriesRequest(context=GLOBAL))
    assert response.repositories


def test_stored_depth_never_walks_the_managed_root(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Listing does not detect drift, at any depth. That is ``status``'s question."""
    from repoatlas.services import freshness_service

    def forbidden(*_: object, **__: object) -> object:
        raise AssertionError("listing repositories walked the managed root")

    monkeypatch.setattr(freshness_service, "plan_repository_set", forbidden)
    _service(initialized).repositories(RepositoriesRequest(context=GLOBAL))


def test_stored_depth_never_claims_a_repository_is_current(initialized: Workspace) -> None:
    """``CURRENT_CLEAN`` means HEAD was checked. At stored depth nothing was."""
    response = _service(initialized).repositories(RepositoriesRequest(context=GLOBAL))
    states = {view.freshness.state for view in response.repositories}
    assert FreshnessState.CURRENT_CLEAN not in states
    assert FreshnessState.DIRTY not in states
    assert all(view.freshness.head_matches is None for view in response.repositories)


def test_live_depth_is_available_when_the_caller_asks(initialized: Workspace) -> None:
    response = _service(initialized).repositories(
        RepositoriesRequest(context=GLOBAL, depth=FreshnessDepth.LIVE)
    )
    assert response.depth is FreshnessDepth.LIVE
    assert all(view.freshness.depth is FreshnessDepth.LIVE for view in response.repositories)
    assert all(view.freshness.head_matches is not None for view in response.repositories)


def test_live_depth_still_does_not_detect_drift(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Depth and drift are separate switches; asking for one is not asking for both."""
    from repoatlas.services import freshness_service

    def forbidden(*_: object, **__: object) -> object:
        raise AssertionError("live-depth listing walked the managed root")

    monkeypatch.setattr(freshness_service, "plan_repository_set", forbidden)
    _service(initialized).repositories(
        RepositoriesRequest(context=GLOBAL, depth=FreshnessDepth.LIVE)
    )


def test_a_repository_with_no_row_id_is_reported_without_counting(workspace: Workspace) -> None:
    """A repository that was never indexed has nothing to count, and no id to count by."""
    unsaved = Repository(
        name="pending",
        slug="pending",
        path=Path("/managed/pending"),
        last_indexed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    workspace.repositories.upsert(unsaved)

    response = _service(workspace).repositories(RepositoriesRequest(context=GLOBAL))
    (view,) = response.repositories
    assert view.slug == "pending"
    assert view.indexed_files == 0
    assert view.symbols == 0
    assert view.relationships == 0
