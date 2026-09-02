"""Directory-context resolution (V1_SPEC §6, §19).

This is a core V1 requirement, so the case matrix is exhaustive.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from repoatlas.context.resolver import RepositoryContextResolver
from repoatlas.errors import ContextResolutionError, NotInitializedError
from repoatlas.models.context import ContextScope
from repoatlas.models.repository import Repository
from repoatlas.services.workspace import Workspace


def _resolver(workspace: Workspace) -> RepositoryContextResolver:
    return workspace.resolver()


def test_repository_root_resolves_to_that_repository(
    initialized: Workspace, workspace_root: Path
) -> None:
    context = _resolver(initialized).resolve(workspace_root / "python-service")
    assert context.scope is ContextScope.REPOSITORY
    assert context.primary is not None
    assert context.primary.name == "python-service"


def test_one_directory_deep(initialized: Workspace, workspace_root: Path) -> None:
    context = _resolver(initialized).resolve(workspace_root / "python-service" / "src")
    assert context.primary is not None
    assert context.primary.name == "python-service"


def test_ten_directories_deep(initialized: Workspace, workspace_root: Path) -> None:
    """Depth must not matter (V1_SPEC §6)."""
    deep = workspace_root / "python-service" / "a/b/c/d/e/f/g/h/i/j"
    deep.mkdir(parents=True)
    context = _resolver(initialized).resolve(deep)
    assert context.scope is ContextScope.REPOSITORY
    assert context.primary is not None
    assert context.primary.name == "python-service"


def test_configured_root_resolves_to_global(initialized: Workspace, workspace_root: Path) -> None:
    context = _resolver(initialized).resolve(workspace_root)
    assert context.scope is ContextScope.GLOBAL
    assert context.primary is None
    assert len(context.repositories) >= 4


def test_unrelated_directory_errors(initialized: Workspace, tmp_path: Path) -> None:
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    with pytest.raises(ContextResolutionError) as excinfo:
        _resolver(initialized).resolve(elsewhere)
    assert "Unable to determine repository." in excinfo.value.message
    assert excinfo.value.exit_code == 2
    assert "repoatlas sync" in (excinfo.value.detail or "")


def test_unmanaged_git_repository_errors(initialized: Workspace, unmanaged_repo: Path) -> None:
    with pytest.raises(ContextResolutionError) as excinfo:
        _resolver(initialized).resolve(unmanaged_repo)
    assert excinfo.value.message == "This Git repository is not managed by RepoAtlas."
    assert str(unmanaged_repo) in (excinfo.value.detail or "")
    assert excinfo.value.exit_code == 2


def test_directory_under_root_but_not_in_a_repository_errors(
    initialized: Workspace, workspace_root: Path
) -> None:
    """There is no subtree scope: proximity never implies a partial scope."""
    loose = workspace_root / "not-a-repo"
    loose.mkdir()
    with pytest.raises(ContextResolutionError) as excinfo:
        _resolver(initialized).resolve(loose)
    assert "Unable to determine repository." in excinfo.value.message


def test_explicit_repository_from_anywhere(initialized: Workspace, tmp_path: Path) -> None:
    elsewhere = tmp_path / "anywhere"
    elsewhere.mkdir()
    context = _resolver(initialized).resolve(elsewhere, "deployment")
    assert context.primary is not None
    assert context.primary.name == "deployment"


def test_explicit_repository_overrides_cwd(initialized: Workspace, workspace_root: Path) -> None:
    context = _resolver(initialized).resolve(
        workspace_root / "python-service" / "src", "deployment"
    )
    assert context.primary is not None
    assert context.primary.name == "deployment"


def test_closest_nested_repository_wins(initialized: Workspace, workspace_root: Path) -> None:
    inner = workspace_root / "nested-outer" / "tools" / "nested-inner"
    context = _resolver(initialized).resolve(inner)
    assert context.primary is not None
    assert context.primary.name == "nested-inner"


def test_closest_nested_repository_wins_from_within_inner(
    initialized: Workspace, workspace_root: Path
) -> None:
    inner = workspace_root / "nested-outer" / "tools" / "nested-inner" / "src" / "deep"
    inner.mkdir(parents=True)
    context = _resolver(initialized).resolve(inner)
    assert context.primary is not None
    assert context.primary.name == "nested-inner"


def test_outer_repository_still_resolves(initialized: Workspace, workspace_root: Path) -> None:
    context = _resolver(initialized).resolve(workspace_root / "nested-outer")
    assert context.primary is not None
    assert context.primary.name == "nested-outer"


def test_unknown_repository_lists_candidates(initialized: Workspace) -> None:
    with pytest.raises(ContextResolutionError) as excinfo:
        _resolver(initialized).resolve(Path.cwd(), "does-not-exist")
    assert "Unknown repository" in excinfo.value.message
    assert "python-service" in (excinfo.value.detail or "")


def test_ambiguous_repository_name_lists_paths(workspace_root: Path) -> None:
    duplicate = Repository(id=1, name="dup", path=workspace_root / "a")
    other = Repository(id=2, name="dup", path=workspace_root / "b")
    resolver = RepositoryContextResolver(workspace_root, [duplicate, other])
    with pytest.raises(ContextResolutionError) as excinfo:
        resolver.resolve(workspace_root, "dup")
    assert "ambiguous" in excinfo.value.message
    assert str(workspace_root / "a") in (excinfo.value.detail or "")


def test_explicit_path_is_accepted(initialized: Workspace, workspace_root: Path) -> None:
    context = _resolver(initialized).resolve(workspace_root, str(workspace_root / "scripts"))
    assert context.primary is not None
    assert context.primary.name == "scripts"


def test_relative_and_dotted_paths(initialized: Workspace, workspace_root: Path) -> None:
    """``..`` and ``.`` must canonicalize, not be string-matched."""
    tricky = workspace_root / "python-service" / "src" / ".." / "src" / "."
    context = _resolver(initialized).resolve(tricky)
    assert context.primary is not None
    assert context.primary.name == "python-service"


def test_trailing_slash_at_root(initialized: Workspace, workspace_root: Path) -> None:
    context = _resolver(initialized).resolve(Path(f"{workspace_root}/"))
    assert context.scope is ContextScope.GLOBAL


def test_paths_with_spaces(workspace: Workspace, tmp_path: Path) -> None:
    from repoatlas.services.init_service import initialize
    from tests.conftest import init_git_repository

    root = tmp_path / "my repos"
    repo = root / "spaced service"
    repo.mkdir(parents=True)
    (repo / "main.py").write_text("value = 1\n")
    init_git_repository(repo)

    initialize(workspace, root, config_path=tmp_path / "config.yaml")
    context = workspace.resolver().resolve(repo)
    assert context.primary is not None
    assert context.primary.name == "spaced service"


def test_similar_prefix_directory_is_not_matched(
    initialized: Workspace, workspace_root: Path
) -> None:
    """``/work/repos-old`` must never match ``/work/repos``."""
    sibling = workspace_root.parent / f"{workspace_root.name}-old"
    sibling.mkdir()
    with pytest.raises(ContextResolutionError):
        _resolver(initialized).resolve(sibling)


def test_symlinked_path_resolves_to_real_repository(
    initialized: Workspace, workspace_root: Path, tmp_path: Path
) -> None:
    link = tmp_path / "link-to-service"
    link.symlink_to(workspace_root / "python-service")
    context = _resolver(initialized).resolve(link)
    assert context.primary is not None
    assert context.primary.name == "python-service"


def test_uninitialized_workspace_errors(workspace: Workspace) -> None:
    with pytest.raises(NotInitializedError) as excinfo:
        workspace.resolver().resolve(Path.cwd())
    assert excinfo.value.exit_code == 3


def test_context_reason_is_populated(initialized: Workspace, workspace_root: Path) -> None:
    """Scope is data with a stated reason, printed under --verbose."""
    context = _resolver(initialized).resolve(workspace_root / "scripts")
    assert "scripts" in context.reason


def test_root_that_is_itself_a_repository(workspace: Workspace, tmp_path: Path) -> None:
    """A managed root that is also a repository resolves to the repository."""
    from repoatlas.services.init_service import initialize
    from tests.conftest import FIXTURE_ROOT, init_git_repository

    root = tmp_path / "monorepo"
    shutil.copytree(FIXTURE_ROOT / "python-service", root)
    init_git_repository(root)

    initialize(workspace, root, config_path=tmp_path / "config.yaml")
    context = workspace.resolver().resolve(root)
    assert context.scope is ContextScope.REPOSITORY
    assert context.primary is not None
    assert context.primary.name == "monorepo"
