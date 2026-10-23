"""Shared test fixtures.

Fixture repositories are stored as plain directories and copied to a temp path,
then given real git history, so HEAD, branches and dirty state are genuine.
Real work repositories are never used (V1_SPEC §19).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.services.workspace import Workspace, open_workspace


def _isolate_worker_state() -> None:
    """Give each pytest-xdist worker its own copy of the state the run was pointed at.

    A run is isolated from the operator's real RepoAtlas state by XDG_CONFIG_HOME,
    XDG_DATA_HOME, REPOATLAS_CONFIG and REPOATLAS_DB. Workers inherit one environment,
    so under ``-n`` they would all share it; each worker instead appends its own id to
    whichever of those the run set. Outside xdist this does nothing.
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if not worker:
        return
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        if value := os.environ.get(name):
            os.environ[name] = str(Path(value) / worker)
    for name in ("REPOATLAS_CONFIG", "REPOATLAS_DB"):
        if value := os.environ.get(name):
            path = Path(value)
            os.environ[name] = str(path.parent / worker / path.name)


# Before any fixture or test module reads the environment.
_isolate_worker_state()

#: Tests whose sources are 3.12 syntax (PEP 695), which an older ``ast`` cannot read:
#: there, such a file gives no Python facts at all (docs/installation.md, Python versions).
PEP_695 = pytest.mark.skipif(sys.version_info < (3, 12), reason="PEP 695 syntax needs 3.12")
#: Type-parameter defaults (PEP 696) are 3.13 syntax.
PEP_696 = pytest.mark.skipif(sys.version_info < (3, 13), reason="PEP 696 syntax needs 3.13")

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


def fact_snapshot(workspace: Workspace) -> dict[str, object]:
    """Normalized, id-independent snapshot of every fact table.

    Comparing two of these is how invariant 2 of ARCHITECTURE.md §9 is asserted:
    *a sync across any change yields the same state as a full reindex of the same
    tree*. Row ids are excluded deliberately — they are allocation order, not
    fact — and everything else is ordered so the comparison is stable.
    """
    repositories = workspace.database.execute(
        # `slug` is the repository's identity, and every entity key is built from
        # it. An incremental sync that reproduced the facts but not the identities
        # would not be equivalent to a full reindex (ARCHITECTURE.md §3).
        "SELECT name, slug FROM repositories ORDER BY slug"
    ).fetchall()
    files = workspace.database.execute(
        # `basename` is generated from `rel_path`, and it is what exact filename
        # lookup seeks on. Including it keeps the equivalence invariant covering
        # the column retrieval actually reads (ARCHITECTURE.md §3, §10).
        "SELECT r.name, f.rel_path, f.basename, f.content_hash FROM files f "
        "JOIN repositories r ON r.id = f.repo_id ORDER BY r.name, f.rel_path"
    ).fetchall()
    entities = workspace.database.execute(
        # `meta_json` holds the parser's facts about a definition, which the global
        # link pass reads; a sync that kept stale facts there is not a reindex.
        "SELECT type, key, name, qualified_name, line_start, line_end, meta_json FROM entities "
        "ORDER BY key, name, line_start"
    ).fetchall()
    edges = workspace.database.execute(
        # Who asserted an edge, which pass wrote it and how it was resolved are part of
        # the fact: USES edges are kept between syncs and replaced per file
        # (repoatlas.graph.uses_link), so a kept edge whose resolution record went
        # stale would be an edge a full reindex does not write.
        "SELECT e.type, src.key AS src_key, dst.key AS dst_key, e.line_start, "
        "COALESCE(f.rel_path, '') AS asserted_by, COALESCE(e.parser, '') AS parser, "
        "COALESCE(e.resolution, '') AS resolution "
        "FROM edges e JOIN entities src ON src.id = e.src_id "
        "JOIN entities dst ON dst.id = e.dst_id "
        "LEFT JOIN files f ON f.id = e.file_id "
        "ORDER BY src.key, dst.key, e.type, e.line_start, asserted_by, parser, resolution"
    ).fetchall()
    dependencies = workspace.database.execute(
        # The source path is part of a dependency fact, not decoration: one
        # repository may declare the same package in several manifests, and a
        # snapshot that omitted the path could not tell a moved manifest from an
        # unchanged one. Ordering by every column also makes the comparison total —
        # rows sharing (name, ecosystem, package, role) previously came back in
        # insertion order, so two equivalent states could differ by row order alone.
        "SELECT r.name, d.ecosystem, d.normalized_name, d.role, d.constraint_text, "
        "d.version, d.group_name, f.rel_path, d.line_start "
        "FROM dependencies d JOIN repositories r ON r.id = d.repo_id "
        "JOIN files f ON f.id = d.file_id "
        "ORDER BY r.name, d.ecosystem, d.normalized_name, d.role, f.rel_path, "
        "d.line_start, d.constraint_text, d.version"
    ).fetchall()
    claims = workspace.database.execute(
        "SELECT r.name, f.rel_path, edge_type, target_kind, target_key, u.line_start "
        "FROM unresolved_refs u JOIN files f ON f.id = u.file_id "
        "JOIN repositories r ON r.id = u.repo_id "
        "ORDER BY r.name, f.rel_path, u.line_start, target_key"
    ).fetchall()
    chunks = workspace.database.execute(
        # The split columns are part of the state: they decide whether a literal
        # straddling a chunk boundary is recoverable, so an incremental sync that
        # reproduced the text but not the seam metadata would not be equivalent.
        # They also make the ordering total — several pieces of one span share a
        # line range, so line_start alone left the comparison order undefined.
        "SELECT r.name, f.rel_path, c.line_start, c.line_end, c.text, "
        "c.span_index, c.split_index, c.split_total "
        "FROM chunks c JOIN files f ON f.id = c.file_id "
        "JOIN repositories r ON r.id = c.repo_id "
        "ORDER BY r.name, f.rel_path, c.span_index, c.split_index, c.line_start"
    ).fetchall()
    decisions = workspace.database.execute(
        # Why each link was or was not made is written by the global pass with the edges
        # it accounts for; a sync that kept a stale decision is not a reindex.
        "SELECT d.family, d.subject, d.decision, d.evidence_json, r.slug, f.rel_path, "
        "d.line_start, d.line_end, s.key FROM link_decisions d "
        "JOIN repositories r ON r.id = d.repo_id JOIN files f ON f.id = d.file_id "
        "JOIN entities s ON s.id = d.src_id "
        # Total order: the ordinary family has one row per open site, and many sites share a
        # subject and a decision (`IMPORTS:module:react`, unsupported) in different files.
        "ORDER BY d.family, d.subject, d.decision, r.slug, f.rel_path, d.line_start, "
        "d.decision_ref"
    ).fetchall()
    outcomes = workspace.database.execute(
        # The ordinary-claim account (repoatlas.graph.ordinary_outcomes) is replaced with the
        # graph by every global pass, so a sync and a full reindex must agree on it.
        "SELECT r.slug, o.language, o.edge_type, o.target_kind, o.outcome, o.subreason, o.claims "
        "FROM ordinary_outcomes o JOIN repositories r ON r.id = o.repo_id "
        "ORDER BY r.slug, o.language, o.edge_type, o.target_kind, o.outcome, o.subreason"
    ).fetchall()
    accounting = workspace.database.execute(
        "SELECT key, value FROM ordinary_accounting ORDER BY key"
    ).fetchall()
    return {
        "repositories": [tuple(r) for r in repositories],
        "files": [tuple(r) for r in files],
        "entities": [tuple(r) for r in entities],
        "edges": [tuple(r) for r in edges],
        "dependencies": [tuple(r) for r in dependencies],
        "unresolved_refs": [tuple(r) for r in claims],
        "chunks": [tuple(r) for r in chunks],
        "link_decisions": [tuple(r) for r in decisions],
        "ordinary_outcomes": [tuple(r) for r in outcomes],
        "ordinary_accounting": [tuple(r) for r in accounting],
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
