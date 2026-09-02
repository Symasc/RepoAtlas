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
    """Settings pointing storage at the temp directory.

    Embeddings use the deterministic hashing backend. Tests must not download a
    model or touch the network (V1_SPEC §19), and a stub also makes the semantic
    stage assertable: the same text always yields the same vector.
    """
    return Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / "state" / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 128},
        }
    )


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


def fact_snapshot(workspace: Workspace) -> dict[str, object]:
    """Normalized, id-independent snapshot of every fact table.

    Comparing two of these is how invariant 2 of ARCHITECTURE.md §9 is asserted:
    *a sync across any change yields the same state as a full reindex of the same
    tree*. Row ids are excluded deliberately — they are allocation order, not
    fact — and everything else is ordered so the comparison is stable.
    """
    files = workspace.database.execute(
        "SELECT r.name, f.rel_path, f.content_hash FROM files f "
        "JOIN repositories r ON r.id = f.repo_id ORDER BY r.name, f.rel_path"
    ).fetchall()
    entities = workspace.database.execute(
        "SELECT type, key, name, qualified_name, line_start, line_end FROM entities "
        "ORDER BY key, name, line_start"
    ).fetchall()
    edges = workspace.database.execute(
        "SELECT e.type, src.key AS src_key, dst.key AS dst_key, e.line_start "
        "FROM edges e JOIN entities src ON src.id = e.src_id "
        "JOIN entities dst ON dst.id = e.dst_id "
        "ORDER BY src.key, dst.key, e.type, e.line_start"
    ).fetchall()
    dependencies = workspace.database.execute(
        "SELECT r.name, ecosystem, normalized_name, role, constraint_text, version "
        "FROM dependencies d JOIN repositories r ON r.id = d.repo_id "
        "ORDER BY r.name, ecosystem, normalized_name, role"
    ).fetchall()
    claims = workspace.database.execute(
        "SELECT r.name, f.rel_path, edge_type, target_kind, target_key, u.line_start "
        "FROM unresolved_refs u JOIN files f ON f.id = u.file_id "
        "JOIN repositories r ON r.id = u.repo_id "
        "ORDER BY r.name, f.rel_path, u.line_start, target_key"
    ).fetchall()
    chunks = workspace.database.execute(
        "SELECT r.name, f.rel_path, c.line_start, c.line_end, c.content_hash "
        "FROM chunks c JOIN files f ON f.id = c.file_id "
        "JOIN repositories r ON r.id = c.repo_id "
        "ORDER BY r.name, f.rel_path, c.line_start"
    ).fetchall()
    return {
        "files": [tuple(r) for r in files],
        "entities": [tuple(r) for r in entities],
        "edges": [tuple(r) for r in edges],
        "dependencies": [tuple(r) for r in dependencies],
        "unresolved_refs": [tuple(r) for r in claims],
        "chunks": [tuple(r) for r in chunks],
    }


def reindex_from_scratch(
    settings: Settings, workspace_root: Path, tmp_path: Path, tag: str = "fresh"
) -> dict[str, object]:
    """Index the tree as it is now into a pristine database, and snapshot it.

    The comparison target for invariant 2. A separate database is used rather
    than ``--full`` on the same one, so nothing an incremental run wrote can
    survive into the baseline.
    """
    from repoatlas.services.init_service import initialize
    from repoatlas.services.workspace import open_workspace

    fresh_settings = settings.model_copy(
        update={
            "storage": settings.storage.model_copy(update={"database": tmp_path / tag / "atlas.db"})
        }
    )
    fresh = open_workspace(settings=fresh_settings, require_root=False)
    try:
        initialize(fresh, workspace_root, config_path=tmp_path / f"{tag}-config.yaml")
        return fact_snapshot(fresh)
    finally:
        fresh.close()
