"""Shared test fixtures.

Fixture repositories are stored as plain directories and copied to a temp path,
then given real git history, so HEAD, branches and dirty state are genuine.
Real work repositories are never used (V1_SPEC §19).
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.services.workspace import Workspace, open_workspace

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "repos"

#: Repositories placed inside the managed root. ``unmanaged`` is deliberately
#: excluded so context tests have a Git repository RepoAtlas does not manage.
MANAGED_FIXTURES = (
    "python-service",
    "deployment",
    "scripts",
    "ts-service",
    "common-lib",
    "nested-outer",
)


#: Every repository ``init`` finds beneath the root: the managed fixtures plus
#: ``nested-inner``, which is a separate repository inside ``nested-outer``.
MANAGED_REPOSITORY_COUNT = len(MANAGED_FIXTURES) + 1


def git(repo: Path, *args: str) -> None:
    """Run a git command in a fixture repository."""
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(repo),
            "GIT_AUTHOR_NAME": "RepoAtlas Tests",
            "GIT_AUTHOR_EMAIL": "tests@example.invalid",
            "GIT_COMMITTER_NAME": "RepoAtlas Tests",
            "GIT_COMMITTER_EMAIL": "tests@example.invalid",
        },
    )


def init_git_repository(path: Path) -> None:
    """Create a real git repository with one commit."""
    git(path, "init", "--quiet", "--initial-branch=main")
    git(path, "add", "-A")
    git(path, "commit", "--quiet", "-m", "initial")


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    """A managed root containing copies of the fixture repositories."""
    root = tmp_path / "repos"
    root.mkdir()

    for name in MANAGED_FIXTURES:
        shutil.copytree(FIXTURE_ROOT / name, root / name)

    # nested-inner is a separate repository inside nested-outer, so it must be
    # initialized first: `git add -A` in the outer repo would otherwise record it
    # as a gitlink and hide its files.
    init_git_repository(root / "nested-outer" / "tools" / "nested-inner")
    for name in MANAGED_FIXTURES:
        init_git_repository(root / name)

    return root


@pytest.fixture
def unmanaged_repo(tmp_path: Path) -> Path:
    """A git repository outside the managed root."""
    path = tmp_path / "elsewhere" / "unmanaged"
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(FIXTURE_ROOT / "unmanaged", path)
    init_git_repository(path)
    return path


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings pointing storage at the temp directory."""
    return Settings.model_validate({"storage": {"database": str(tmp_path / "state" / "atlas.db")}})


@pytest.fixture
def workspace(settings: Settings) -> Iterator[Workspace]:
    """An open workspace with no root configured yet."""
    ws = open_workspace(settings=settings, require_root=False)
    yield ws
    ws.close()


@pytest.fixture
def initialized(workspace: Workspace, workspace_root: Path, tmp_path: Path) -> Iterator[Workspace]:
    """A workspace with the fixture root discovered and fully indexed."""
    from repoatlas.services.init_service import initialize

    initialize(workspace, workspace_root, config_path=tmp_path / "config.yaml")
    yield workspace
