"""Managed-root relocation: moving a workspace is not a change of identity.

The release review moved an indexed managed root and re-ran ``repoatlas init``
against it. Discovery found the same repositories at new absolute paths, the
registered rows still carried the old ones, and registration tried to insert a
second row for each — carrying the slug the old row still held::

    sqlite3.IntegrityError: UNIQUE constraint failed: repositories.slug

An ordinary ``mv`` is not an error condition, and a uniqueness constraint firing
as a traceback is not a decision. The constraint was right; nothing had decided
what a moved root *means* before the write reached it.

It means this: a repository's identity is its path **relative to the managed
root**, so moving the root preserves every identity in it and changes only where
they live. Absolute location is mutable metadata. The reconciliation therefore
matches registered rows to discoveries by slug and rebinds them in place,
preserving row ids — which files, entities, edges and chunks all reference — and
the slugs every entity key is built from.

What makes this suite adversarial is the other half. A matching slug is
*necessary and not sufficient*, and treating it as sufficient would be a worse
defect than the crash: it would silently merge two unrelated repositories that
happen to sit at the same offset from two different roots. So every relocation
test here is paired with a case that must **not** relocate — the old root still
present, an unrelated repository under a matching slug, an internal move that
changes the slug — and with the proof that a refusal writes nothing at all.

Everything is real: real git repositories, real filesystem moves, real
subprocesses for exit codes, concurrency and kills, and a real second index used
as the from-scratch comparison target.
"""

from __future__ import annotations

import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from repoatlas.config.settings import ReposSettings, Settings
from repoatlas.discovery.reconcile import plan_repository_set
from repoatlas.errors import (
    ExitCode,
    IndexNotRecognizedError,
    RepoAtlasError,
    RepositoryRootConflictError,
)
from repoatlas.models.context import ContextScope
from repoatlas.rendering.text import render_status
from repoatlas.services.init_service import initialize
from repoatlas.services.status_service import list_repositories, status
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace
from repoatlas.storage.sqlite.migrator import APPLICATION_ID, application_id
from repoatlas.storage.sqlite.stores import SqliteRepositoryStore
from tests.conftest import fact_snapshot, init_git_repository, reindex_from_scratch

pytestmark = pytest.mark.adversarial

CLI = Path(sys.executable).parent / "repoatlas"

TRACEBACK_MARKERS = ("Traceback (most recent call last)", "IntegrityError", "UNIQUE constraint")


# --- harness ------------------------------------------------------------------


def make_repo(root: Path, relative: str, files: dict[str, str]) -> Path:
    """A real Git repository at ``root/relative`` with one commit."""
    repo = root / relative if relative != "." else root
    for name, content in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    init_git_repository(repo)
    return repo


def build_workspace(
    root: Path, *, names: tuple[str, ...] = ("service-a", "tools/service-b")
) -> Path:
    """A managed root holding one repository per name."""
    for index, name in enumerate(names):
        make_repo(
            root,
            name,
            {
                "app.py": (
                    f"SERVICE_HOST = 'host-{index}'\n\n\n"
                    f"def handler_{index}():\n    return SERVICE_HOST\n"
                ),
            },
        )
    return root


def settings_for(tmp_path: Path, tag: str = "state") -> Settings:
    return Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / tag / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 128},
        }
    )


def rooted(settings: Settings, root: Path) -> Settings:
    return settings.model_copy(update={"repos": ReposSettings(root=root)})


def run_init(settings: Settings, root: Path, config: Path) -> None:
    """Initialize in a workspace opened and closed like a real invocation."""
    workspace = open_workspace(settings=settings, require_root=False)
    try:
        initialize(workspace, root, config_path=config)
    finally:
        workspace.close()


def open_at(settings: Settings, root: Path) -> Workspace:
    """A workspace that knows the root, as a later process would after reading config."""
    return open_workspace(settings=rooted(settings, root), require_root=False)


def repository_rows(settings: Settings) -> list[tuple[int, str, str]]:
    """``(id, slug, path)`` for every registered repository, ordered by identity."""
    workspace = open_workspace(settings=settings, require_root=False)
    try:
        rows = workspace.database.execute(
            "SELECT id, slug, path FROM repositories ORDER BY slug"
        ).fetchall()
        return [(int(r["id"]), str(r["slug"]), str(r["path"])) for r in rows]
    finally:
        workspace.close()


def fact_counts(settings: Settings) -> tuple[int, int, int]:
    workspace = open_workspace(settings=settings, require_root=False)
    try:
        row = workspace.database.execute(
            "SELECT (SELECT COUNT(*) FROM files) AS f, "
            "(SELECT COUNT(*) FROM entities) AS e, "
            "(SELECT COUNT(*) FROM edges) AS g"
        ).fetchone()
        return (int(row["f"]), int(row["e"]), int(row["g"]))
    finally:
        workspace.close()


def cli(root: Path, *args: str, config: Path, database: Path, cwd: Path | None = None):
    """Run the installed console script the way an operator would."""
    environment = dict(os.environ)
    environment.update(
        {
            "REPOATLAS_CONFIG": str(config),
            "REPOATLAS_DB": str(database),
            "HOME": str(root.parent),
        }
    )
    return subprocess.run(
        [str(CLI), *args],
        capture_output=True,
        text=True,
        cwd=str(cwd or root),
        env=environment,
        timeout=300,
    )


@pytest.fixture
def relocation(tmp_path: Path):
    """An indexed two-repository root, ready to be moved."""
    old = build_workspace(tmp_path / "original-root")
    settings = settings_for(tmp_path)
    config = tmp_path / "config.yaml"
    run_init(settings, old, config)
    return old, tmp_path / "moved-root", settings, config


# --- 1. the original reproduction ---------------------------------------------


def test_the_original_unique_slug_crash_no_longer_occurs(relocation) -> None:
    """The exact reported sequence: init, move the root, init again."""
    old, new, settings, config = relocation
    before = repository_rows(settings)
    assert [slug for _, slug, _ in before] == ["service-a", "tools/service-b"]

    old.rename(new)
    run_init(settings, new, config)

    after = repository_rows(settings)
    assert [slug for _, slug, _ in after] == ["service-a", "tools/service-b"]
    assert all(Path(path).is_relative_to(new) for _, _, path in after)


def test_the_original_crash_is_gone_through_the_real_cli(relocation) -> None:
    """Exit 0 and no traceback, from the installed console script."""
    old, new, settings, config = relocation
    old.rename(new)

    result = cli(new, "init", str(new), config=config, database=Path(settings.storage.database))

    assert result.returncode == ExitCode.OK, result.stderr
    combined = result.stdout + result.stderr
    for marker in TRACEBACK_MARKERS:
        assert marker not in combined, f"{marker} in output"


# --- 2-5. relocation proper ---------------------------------------------------


def test_a_pure_root_rename_preserves_repository_ids(relocation) -> None:
    """Row ids are what every fact references; a move must not renumber them."""
    old, new, settings, config = relocation
    before = {slug: rid for rid, slug, _ in repository_rows(settings)}

    old.rename(new)
    run_init(settings, new, config)

    after = {slug: rid for rid, slug, _ in repository_rows(settings)}
    assert after == before


def test_a_pure_root_rename_preserves_indexed_facts(relocation) -> None:
    """Nothing in the tree changed, so nothing indexed from it may be lost."""
    old, new, settings, config = relocation
    before = fact_counts(settings)
    assert before[0] > 0 and before[1] > 0

    old.rename(new)
    run_init(settings, new, config)

    assert fact_counts(settings) == before


def test_copy_then_delete_is_supported_as_a_relocation(tmp_path: Path) -> None:
    """A copied-then-removed root is a move to everyone except the inode table."""
    old = build_workspace(tmp_path / "original-root")
    settings = settings_for(tmp_path)
    config = tmp_path / "config.yaml"
    run_init(settings, old, config)
    before = {slug: rid for rid, slug, _ in repository_rows(settings)}

    new = tmp_path / "copied-root"
    shutil.copytree(old, new, symlinks=True)
    shutil.rmtree(old)
    run_init(settings, new, config)

    after = {slug: rid for rid, slug, _ in repository_rows(settings)}
    assert after == before, "identity must not depend on inode preservation"


def test_a_dirty_repository_relocates(tmp_path: Path) -> None:
    """RepoAtlas indexes working trees, so an uncommitted one must move too."""
    old = build_workspace(tmp_path / "original-root")
    (old / "service-a" / "uncommitted.py").write_text("DIRTY_MARKER = 1\n", encoding="utf-8")
    settings = settings_for(tmp_path)
    config = tmp_path / "config.yaml"
    run_init(settings, old, config)
    before = {slug: rid for rid, slug, _ in repository_rows(settings)}

    new = tmp_path / "moved-root"
    old.rename(new)
    run_init(settings, new, config)

    assert {slug: rid for rid, slug, _ in repository_rows(settings)} == before
    workspace = open_at(settings, new)
    try:
        rows = workspace.database.execute(
            "SELECT rel_path FROM files WHERE rel_path = 'uncommitted.py'"
        ).fetchall()
        assert rows, "the uncommitted file must still be indexed after the move"
    finally:
        workspace.close()


def test_content_changed_during_the_move_is_reindexed(tmp_path: Path) -> None:
    """Relocation rebinds a row; it never asserts the contents are unchanged."""
    old = build_workspace(tmp_path / "original-root")
    settings = settings_for(tmp_path)
    config = tmp_path / "config.yaml"
    run_init(settings, old, config)

    new = tmp_path / "moved-root"
    old.rename(new)
    (new / "service-a" / "app.py").write_text(
        "SERVICE_HOST = 'changed'\n\n\ndef added_after_move():\n    return SERVICE_HOST\n",
        encoding="utf-8",
    )
    (new / "service-a" / "brand_new.py").write_text("NEW_AFTER_MOVE = 2\n", encoding="utf-8")
    run_init(settings, new, config)

    workspace = open_at(settings, new)
    try:
        names = {
            str(row["name"])
            for row in workspace.database.execute("SELECT name FROM entities").fetchall()
        }
        assert "added_after_move" in names
        files = {
            str(row["rel_path"])
            for row in workspace.database.execute("SELECT rel_path FROM files").fetchall()
        }
        assert "brand_new.py" in files
    finally:
        workspace.close()


def test_many_repositories_relocate_together(tmp_path: Path) -> None:
    names = tuple(f"team-{i // 5}/repo-{i}" for i in range(20))
    old = build_workspace(tmp_path / "original-root", names=names)
    settings = settings_for(tmp_path)
    config = tmp_path / "config.yaml"
    run_init(settings, old, config)
    before = {slug: rid for rid, slug, _ in repository_rows(settings)}
    assert len(before) == len(names)

    new = tmp_path / "moved-root"
    old.rename(new)
    run_init(settings, new, config)

    after = repository_rows(settings)
    assert {slug: rid for rid, slug, _ in after} == before
    assert all(Path(path).is_relative_to(new) for _, _, path in after)


def test_nested_repositories_relocate_and_stay_distinct(tmp_path: Path) -> None:
    """A repository inside a repository keeps its own identity across the move."""
    root = tmp_path / "original-root"
    make_repo(root, "parent", {"app.py": "PARENT = 1\n"})
    make_repo(root, "parent/tools/child", {"tool.py": "CHILD = 2\n"})
    settings = settings_for(tmp_path)
    config = tmp_path / "config.yaml"
    run_init(settings, root, config)
    before = {slug: rid for rid, slug, _ in repository_rows(settings)}
    assert set(before) == {"parent", "parent/tools/child"}

    new = tmp_path / "moved-root"
    root.rename(new)
    run_init(settings, new, config)

    assert {slug: rid for rid, slug, _ in repository_rows(settings)} == before


def test_duplicate_basenames_relocate_without_merging(tmp_path: Path) -> None:
    """Two repositories called ``service`` stay two repositories."""
    root = tmp_path / "original-root"
    make_repo(root, "division/a/service", {"a.py": "WHICH = 'a'\n"})
    make_repo(root, "archive/deep/service", {"b.py": "WHICH = 'b'\n"})
    settings = settings_for(tmp_path)
    config = tmp_path / "config.yaml"
    run_init(settings, root, config)
    before = {slug: rid for rid, slug, _ in repository_rows(settings)}
    assert set(before) == {"division/a/service", "archive/deep/service"}

    new = tmp_path / "moved-root"
    root.rename(new)
    run_init(settings, new, config)

    after = repository_rows(settings)
    assert {slug: rid for rid, slug, _ in after} == before
    assert len({path for _, _, path in after}) == 2


def test_the_root_itself_as_a_repository_relocates(tmp_path: Path) -> None:
    """Slug ``.`` is the subtlest match: every root has exactly one."""
    root = tmp_path / "original-root"
    root.mkdir(parents=True)
    make_repo(root, ".", {"app.py": "ROOT_REPO = 1\n"})
    settings = settings_for(tmp_path)
    config = tmp_path / "config.yaml"
    run_init(settings, root, config)
    before = repository_rows(settings)
    assert [slug for _, slug, _ in before] == ["."]

    new = tmp_path / "moved-root"
    root.rename(new)
    run_init(settings, new, config)

    after = repository_rows(settings)
    assert [slug for _, slug, _ in after] == ["."]
    assert after[0][0] == before[0][0], "the root repository keeps its id"
    assert Path(after[0][2]) == new


# --- 10-12. what must NOT be treated as a relocation ---------------------------


def test_an_old_root_that_still_exists_is_a_conflict_not_a_relocation(relocation) -> None:
    """Two live directories, one identity: RepoAtlas refuses rather than guessing."""
    old, new, settings, config = relocation
    shutil.copytree(old, new, symlinks=True)

    workspace = open_workspace(settings=settings, require_root=False)
    try:
        with pytest.raises(RepositoryRootConflictError) as caught:
            initialize(workspace, new, config_path=config)
    finally:
        workspace.close()

    message = caught.value.render()
    assert str(old / "service-a") in message
    assert str(new / "service-a") in message


def test_a_refused_conflict_writes_nothing_at_all(relocation) -> None:
    """Neither the repository rows nor the configured root may move."""
    old, new, settings, config = relocation
    shutil.copytree(old, new, symlinks=True)
    before = repository_rows(settings)
    config_before = config.read_bytes()

    workspace = open_workspace(settings=settings, require_root=False)
    try:
        with pytest.raises(RepositoryRootConflictError):
            initialize(workspace, new, config_path=config)
    finally:
        workspace.close()

    assert repository_rows(settings) == before
    assert config.read_bytes() == config_before, "the configured root must not move"


def test_the_conflict_is_a_clean_exit_not_a_traceback(relocation) -> None:
    old, new, settings, config = relocation
    shutil.copytree(old, new, symlinks=True)

    result = cli(new, "init", str(new), config=config, database=Path(settings.storage.database))

    assert result.returncode != ExitCode.OK
    combined = result.stdout + result.stderr
    for marker in TRACEBACK_MARKERS:
        assert marker not in combined, f"{marker} in output"
    assert "service-a" in combined


def test_an_unrelated_repository_under_a_matching_slug_is_not_given_old_facts(
    tmp_path: Path,
) -> None:
    """Same slug, different repository: the row may be reused, the facts may not."""
    old = build_workspace(tmp_path / "original-root", names=("service-a",))
    settings = settings_for(tmp_path)
    config = tmp_path / "config.yaml"
    run_init(settings, old, config)

    workspace = open_at(settings, old)
    try:
        original_files = {
            str(row["rel_path"])
            for row in workspace.database.execute("SELECT rel_path FROM files").fetchall()
        }
    finally:
        workspace.close()
    assert "app.py" in original_files

    shutil.rmtree(old)
    new = tmp_path / "moved-root"
    make_repo(new, "service-a", {"totally_different.py": "UNRELATED = 1\n"})
    run_init(settings, new, config)

    workspace = open_at(settings, new)
    try:
        files = {
            str(row["rel_path"])
            for row in workspace.database.execute("SELECT rel_path FROM files").fetchall()
        }
    finally:
        workspace.close()
    assert files == {"totally_different.py"}, "stale facts must not survive as current"


def test_an_internal_repository_move_changes_the_slug_and_is_not_a_relocation(
    tmp_path: Path,
) -> None:
    """``services/api`` -> ``archive/api`` is a different identity, by construction."""
    root = tmp_path / "root"
    make_repo(root, "services/api", {"api.py": "API = 1\n"})
    settings = settings_for(tmp_path)
    config = tmp_path / "config.yaml"
    run_init(settings, root, config)
    assert [slug for _, slug, _ in repository_rows(settings)] == ["services/api"]

    (root / "services" / "api").rename(root / "archive-api-tmp")
    (root / "archive").mkdir(parents=True, exist_ok=True)
    (root / "archive-api-tmp").rename(root / "archive" / "api")
    run_init(settings, root, config)

    rows = repository_rows(settings)
    assert [slug for _, slug, _ in rows] == ["archive/api"], "the old identity is retired"
    assert len(rows) == 1, "and is not left behind as a second row"


# --- 13-18. user-visible behaviour after a move -------------------------------


def test_init_is_idempotent_after_a_relocation(relocation) -> None:
    old, new, settings, config = relocation
    old.rename(new)

    run_init(settings, new, config)
    once = repository_rows(settings)
    run_init(settings, new, config)
    twice = repository_rows(settings)
    run_init(settings, new, config)
    thrice = repository_rows(settings)

    assert once == twice == thrice


def test_status_after_a_move_names_no_old_path(relocation) -> None:
    old, new, settings, config = relocation
    old.rename(new)
    run_init(settings, new, config)

    workspace = open_at(settings, new)
    try:
        context = workspace.resolver().resolve(new, command="status")
        report = status(workspace, context)
        rendered = repr(report)
    finally:
        workspace.close()
    assert str(old) not in rendered
    assert report.new_repositories == (), "nothing may read as newly discovered"
    assert report.missing_repositories == (), "and nothing as missing"
    assert report.repository_count == 2


def test_repos_lists_each_repository_once_after_a_move(relocation) -> None:
    old, new, settings, config = relocation
    old.rename(new)
    run_init(settings, new, config)

    workspace = open_at(settings, new)
    try:
        listed = list_repositories(workspace)
    finally:
        workspace.close()

    slugs = [repo.slug for repo in listed]
    assert slugs == sorted(set(slugs)), "no duplicates"
    assert len(listed) == 2
    assert all(repo.path.is_relative_to(new) for repo in listed)


def test_context_resolution_works_from_the_new_root(relocation) -> None:
    old, new, settings, config = relocation
    old.rename(new)
    run_init(settings, new, config)

    workspace = open_at(settings, new)
    try:
        resolver = workspace.resolver()
        whole = resolver.resolve(new, command="status")
        assert whole.scope is ContextScope.GLOBAL

        inside = resolver.resolve(new / "service-a", command="sync")
        assert inside.scope is ContextScope.REPOSITORY
        assert inside.repositories[0].slug == "service-a"

        nested = resolver.resolve(new / "tools" / "service-b", command="sync")
        assert nested.repositories[0].slug == "tools/service-b"
    finally:
        workspace.close()


def test_sync_after_a_move_is_clean(relocation) -> None:
    old, new, settings, config = relocation
    old.rename(new)
    run_init(settings, new, config)
    before = repository_rows(settings)

    workspace = open_at(settings, new)
    try:
        context = workspace.resolver().resolve(new, command="sync")
        report = sync(workspace, context)
    finally:
        workspace.close()

    assert report is not None
    assert repository_rows(settings) == before, "sync must not re-register anything"


def test_retrieval_after_a_move_cites_the_new_location(relocation) -> None:
    old, new, settings, config = relocation
    old.rename(new)
    run_init(settings, new, config)

    workspace = open_at(settings, new)
    try:
        from repoatlas.services.retrieval_service import locate

        context = workspace.resolver().resolve(new, command="locate")
        evidence = locate(workspace, "SERVICE_HOST", context)
        rendered = repr(evidence)
    finally:
        workspace.close()

    assert str(old) not in rendered, "no citation may name the old root"


def test_incremental_relocation_equals_a_clean_index_of_the_new_tree(
    relocation, tmp_path: Path
) -> None:
    """Invariant 2 of ARCHITECTURE.md §9, across a relocation."""
    old, new, settings, config = relocation
    old.rename(new)
    run_init(settings, new, config)

    workspace = open_at(settings, new)
    try:
        moved = fact_snapshot(workspace)
    finally:
        workspace.close()

    fresh = reindex_from_scratch(settings_for(tmp_path, "fresh-state"), new, tmp_path, "fresh")
    assert moved == fresh


def test_no_old_root_path_survives_in_active_index_state(relocation) -> None:
    old, new, settings, config = relocation
    old.rename(new)
    run_init(settings, new, config)

    workspace = open_workspace(settings=settings, require_root=False)
    try:
        paths = [
            str(row["path"])
            for row in workspace.database.execute("SELECT path FROM repositories").fetchall()
        ]
    finally:
        workspace.close()
    assert paths and all(not p.startswith(str(old)) for p in paths)


def test_no_duplicate_repository_rows_after_a_move(relocation) -> None:
    old, new, settings, config = relocation
    old.rename(new)
    run_init(settings, new, config)

    rows = repository_rows(settings)
    assert len({slug for _, slug, _ in rows}) == len(rows)
    assert len({path for _, _, path in rows}) == len(rows)


# --- 22. transactionality -----------------------------------------------------


def test_a_failure_partway_through_relocation_moves_no_repository(
    relocation, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All rows relocate, or none does. A workspace split across two roots is worse
    than one that has not moved yet."""
    old, new, settings, config = relocation
    before = repository_rows(settings)
    old.rename(new)

    calls = {"n": 0}
    original = SqliteRepositoryStore.relocate

    def failing(self, repo_id: int, path: Path) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise sqlite3.OperationalError("injected failure on the second repository")
        original(self, repo_id, path)

    monkeypatch.setattr(SqliteRepositoryStore, "relocate", failing)

    workspace = open_workspace(settings=settings, require_root=False)
    try:
        with pytest.raises((sqlite3.Error, RepoAtlasError)):
            initialize(workspace, new, config_path=config)
    finally:
        workspace.close()

    assert calls["n"] == 2, "the injection must have been reached"
    monkeypatch.undo()
    assert repository_rows(settings) == before, "mixed old/new repository paths"


def test_a_failed_relocation_leaves_the_configured_root_alone(
    relocation, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new, settings, config = relocation
    config_before = config.read_bytes()
    old.rename(new)

    def failing(self, repo_id: int, path: Path) -> None:
        raise sqlite3.OperationalError("injected")

    monkeypatch.setattr(SqliteRepositoryStore, "relocate", failing)
    workspace = open_workspace(settings=settings, require_root=False)
    try:
        with pytest.raises((sqlite3.Error, RepoAtlasError)):
            initialize(workspace, new, config_path=config)
    finally:
        workspace.close()
    monkeypatch.undo()

    assert config.read_bytes() == config_before


def test_the_next_init_recovers_after_a_failed_relocation(
    relocation, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new, settings, config = relocation
    before = {slug: rid for rid, slug, _ in repository_rows(settings)}
    old.rename(new)

    def failing(self, repo_id: int, path: Path) -> None:
        raise sqlite3.OperationalError("injected")

    monkeypatch.setattr(SqliteRepositoryStore, "relocate", failing)
    workspace = open_workspace(settings=settings, require_root=False)
    try:
        with pytest.raises((sqlite3.Error, RepoAtlasError)):
            initialize(workspace, new, config_path=config)
    finally:
        workspace.close()
    monkeypatch.undo()

    run_init(settings, new, config)
    assert {slug: rid for rid, slug, _ in repository_rows(settings)} == before


# --- 23-24. concurrency -------------------------------------------------------


def test_concurrent_init_against_a_relocated_root_converges(relocation) -> None:
    """Two initializations of the same moved root: one mapping, no UNIQUE failure."""
    old, new, settings, config = relocation
    old.rename(new)
    database = Path(settings.storage.database)

    processes = [
        subprocess.Popen(
            [str(CLI), "init", str(new)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(new),
            env={**os.environ, "REPOATLAS_CONFIG": str(config), "REPOATLAS_DB": str(database)},
        )
        for _ in range(3)
    ]
    outputs = [(p.wait(timeout=300), p.communicate()[0]) for p in processes]

    for code, output in outputs:
        assert "UNIQUE constraint" not in output
        assert "Traceback (most recent call last)" not in output
        assert code in (ExitCode.OK, ExitCode.SYNC_LOCKED), output

    rows = repository_rows(settings)
    assert [slug for _, slug, _ in rows] == ["service-a", "tools/service-b"]
    assert all(Path(path).is_relative_to(new) for _, _, path in rows)


def test_init_and_sync_contend_without_partial_relocation(relocation) -> None:
    old, new, settings, config = relocation
    old.rename(new)
    database = Path(settings.storage.database)
    environment = {**os.environ, "REPOATLAS_CONFIG": str(config), "REPOATLAS_DB": str(database)}

    initializer = subprocess.Popen(
        [str(CLI), "init", str(new)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(new),
        env=environment,
    )
    syncer = subprocess.Popen(
        [str(CLI), "sync"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(new),
        env=environment,
    )
    results = [(p.wait(timeout=300), p.communicate()[0]) for p in (initializer, syncer)]

    for _, output in results:
        assert "UNIQUE constraint" not in output
        assert "Traceback (most recent call last)" not in output

    rows = repository_rows(settings)
    assert all(Path(path).is_relative_to(new) for _, _, path in rows)


# --- 37. crash consistency ----------------------------------------------------


@pytest.mark.parametrize("delay", [0.05, 0.15, 0.4])
def test_a_killed_init_never_leaves_a_split_repository_set(relocation, delay: float) -> None:
    """Kill mid-relocation; the surviving state names exactly one root."""
    old, new, settings, config = relocation
    old.rename(new)
    database = Path(settings.storage.database)

    process = subprocess.Popen(
        [str(CLI), "init", str(new)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(new),
        env={**os.environ, "REPOATLAS_CONFIG": str(config), "REPOATLAS_DB": str(database)},
    )
    time.sleep(delay)
    process.send_signal(signal.SIGKILL)
    process.wait(timeout=60)
    process.communicate()

    rows = repository_rows(settings)
    roots = {str(old) if Path(p).is_relative_to(old) else str(new) for _, _, p in rows}
    assert len(roots) <= 1, f"repository set split across roots: {rows}"

    run_init(settings, new, config)
    recovered = repository_rows(settings)
    assert [slug for _, slug, _ in recovered] == ["service-a", "tools/service-b"]
    assert all(Path(path).is_relative_to(new) for _, _, path in recovered)


# --- 25-28. the database-ownership boundary is untouched ----------------------


def test_relocation_does_not_change_the_database_application_id(relocation) -> None:
    old, new, settings, config = relocation
    database = Path(settings.storage.database)
    connection = sqlite3.connect(database)
    try:
        before = application_id(connection)
    finally:
        connection.close()
    assert before == APPLICATION_ID

    old.rename(new)
    run_init(settings, new, config)

    connection = sqlite3.connect(database)
    try:
        assert application_id(connection) == APPLICATION_ID
    finally:
        connection.close()


def test_a_foreign_database_is_still_refused_from_a_relocated_root(
    relocation, tmp_path: Path
) -> None:
    """Ownership is proven before any root reconciliation runs."""
    old, new, settings, _config = relocation
    old.rename(new)

    foreign = tmp_path / "foreign.db"
    connection = sqlite3.connect(foreign)
    connection.execute("CREATE TABLE payroll (id INTEGER PRIMARY KEY, amount TEXT)")
    connection.execute("INSERT INTO payroll (amount) VALUES ('1000')")
    connection.commit()
    connection.close()
    before = foreign.read_bytes()

    foreign_settings = rooted(
        settings.model_copy(
            update={"storage": settings.storage.model_copy(update={"database": foreign})}
        ),
        new,
    )
    workspace_error: RepoAtlasError | None = None
    try:
        workspace = open_workspace(settings=foreign_settings, require_root=False)
        workspace.close()
    except RepoAtlasError as error:
        workspace_error = error

    assert isinstance(workspace_error, IndexNotRecognizedError)
    assert workspace_error.exit_code == ExitCode.NOT_INITIALIZED
    assert foreign.read_bytes() == before, "a refused database must be byte-identical"
    assert not foreign.with_suffix(".db-wal").exists()


def test_a_foreign_database_is_refused_through_the_cli_with_exit_three(
    relocation, tmp_path: Path
) -> None:
    old, new, _settings, _config = relocation
    old.rename(new)

    foreign = tmp_path / "foreign-cli.db"
    connection = sqlite3.connect(foreign)
    connection.execute("CREATE TABLE payroll (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    before = foreign.read_bytes()

    unpinned = tmp_path / "unpinned-config.yaml"
    unpinned.write_text(f"repos:\n  root: {new}\n", encoding="utf-8")
    result = cli(new, "init", str(new), config=unpinned, database=foreign)

    assert result.returncode == ExitCode.NOT_INITIALIZED, result.stdout + result.stderr
    assert foreign.read_bytes() == before


def test_a_future_schema_is_still_refused_from_a_relocated_root(relocation, tmp_path: Path) -> None:
    """An unusable index outranks any question about where the root is."""
    old, new, settings, _config = relocation
    old.rename(new)

    future = tmp_path / "future.db"
    shutil.copy2(Path(settings.storage.database), future)
    connection = sqlite3.connect(future)
    connection.execute("PRAGMA user_version = 9999")
    connection.commit()
    connection.close()

    unpinned = tmp_path / "unpinned-future-config.yaml"
    unpinned.write_text(f"repos:\n  root: {new}\n", encoding="utf-8")
    result = cli(new, "init", str(new), config=unpinned, database=future)

    assert result.returncode == ExitCode.NOT_INITIALIZED, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "Traceback (most recent call last)" not in combined


# --- 40. the empty root -------------------------------------------------------


def test_an_empty_root_relocates_without_inventing_repositories(tmp_path: Path) -> None:
    old = tmp_path / "empty-root"
    old.mkdir(parents=True)
    settings = settings_for(tmp_path)
    config = tmp_path / "config.yaml"
    run_init(settings, old, config)
    assert repository_rows(settings) == []

    new = tmp_path / "moved-empty"
    old.rename(new)
    run_init(settings, new, config)

    assert repository_rows(settings) == []


# --- the plan itself ----------------------------------------------------------


def test_the_plan_reports_relocation_without_writing(relocation) -> None:
    """``plan_repository_set`` stays read-only, which is what lets status use it."""
    old, new, settings, _config = relocation
    old.rename(new)

    workspace = open_workspace(settings=settings, require_root=False)
    try:
        before = repository_rows(settings)
        plan = plan_repository_set(
            new, workspace.settings.discovery, workspace.repositories.list_all()
        )
        assert {item.repository.slug for item in plan.relocated} == {
            "service-a",
            "tools/service-b",
        }
        assert plan.added == ()
        assert plan.missing == (), "a relocated repository is not a missing one"
        assert plan.conflicts == ()
        assert plan.drifted
    finally:
        workspace.close()
    assert repository_rows(settings) == before


def test_an_ordinary_unchanged_root_plans_no_relocation(relocation) -> None:
    """Relocation must not fire on a run where nothing moved."""
    old, _new, settings, _config = relocation
    workspace = open_workspace(settings=settings, require_root=False)
    try:
        plan = plan_repository_set(
            old, workspace.settings.discovery, workspace.repositories.list_all()
        )
        assert plan.relocated == ()
        assert plan.conflicts == ()
        assert plan.added == ()
        assert not plan.drifted
    finally:
        workspace.close()


# --- recovery from a root the configuration already moved ---------------------


def repoint_config(config: Path, old: Path, new: Path) -> None:
    """Move the configured root without moving the repository rows.

    This is the state the pre-fix crash left behind — `init` wrote the
    configuration before reconciling the database, then died on the uniqueness
    constraint — so it is the state an upgrading workspace is actually in.
    """
    config.write_text(config.read_text(encoding="utf-8").replace(str(old), str(new)), "utf-8")


def test_sync_recovers_a_workspace_whose_config_already_moved(relocation) -> None:
    """`sync` must relocate the rows, not retire them.

    Their directories are gone, so `forget_if_missing` would delete every one —
    and a relocated candidate is deliberately not an added one, so nothing would
    be registered in their place. That is silent, total loss of the repository
    set on an ordinary sync.
    """
    old, new, settings, config = relocation
    before = {slug: rid for rid, slug, _ in repository_rows(settings)}
    old.rename(new)
    repoint_config(config, old, new)

    workspace = open_at(settings, new)
    try:
        context = workspace.resolver().resolve(new, command="sync")
        sync(workspace, context)
    finally:
        workspace.close()

    after = repository_rows(settings)
    assert after, "the repository set must not be emptied"
    assert {slug: rid for rid, slug, _ in after} == before
    assert all(Path(path).is_relative_to(new) for _, _, path in after)


def test_sync_does_not_crash_on_a_conflicting_root(relocation) -> None:
    """The old `UNIQUE constraint failed` reached `sync` too. It must not now."""
    old, new, settings, config = relocation
    shutil.copytree(old, new, symlinks=True)
    repoint_config(config, old, new)
    before = repository_rows(settings)

    workspace = open_at(settings, new)
    try:
        context = workspace.resolver().resolve(new, command="sync")
        sync(workspace, context)
    finally:
        workspace.close()

    assert repository_rows(settings) == before, "no conflicting row may be registered"


def test_status_names_a_conflicting_repository_rather_than_hiding_it(relocation) -> None:
    """A repository that is neither indexed nor mentioned is the worse failure."""
    old, new, settings, config = relocation
    shutil.copytree(old, new, symlinks=True)
    repoint_config(config, old, new)

    workspace = open_at(settings, new)
    try:
        context = workspace.resolver().resolve(new, command="status")
        report = status(workspace, context)
    finally:
        workspace.close()

    assert set(report.conflicting_repositories) == {"service-a", "tools/service-b"}
    assert report.index_state != "current", "a conflict is drift, not a current workspace"
    rendered = render_status(report)
    assert "service-a" in rendered
    assert "already registered elsewhere" in rendered.lower()


def test_a_failed_config_write_leaves_a_recoverable_workspace(
    relocation, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two writes cannot share a transaction, so the order picks the failure.

    Database first: the rows are at the new paths while the configuration still
    names the old root. Every one of those paths exists, so nothing reads as
    missing and no later command retires anything, and the next `init` matches
    them by path and rewrites the configuration.
    """
    old, new, settings, config = relocation
    before = {slug: rid for rid, slug, _ in repository_rows(settings)}
    old.rename(new)

    import repoatlas.services.init_service as init_module

    def failing(*args: object, **kwargs: object) -> None:
        raise OSError("injected configuration write failure")

    monkeypatch.setattr(init_module, "save_settings", failing)
    workspace = open_workspace(settings=settings, require_root=False)
    try:
        with pytest.raises(OSError):
            initialize(workspace, new, config_path=config)
    finally:
        workspace.close()
    monkeypatch.undo()

    # The repository set moved as one unit and kept its identities.
    mid = repository_rows(settings)
    assert {slug: rid for rid, slug, _ in mid} == before
    assert all(Path(path).is_relative_to(new) for _, _, path in mid)

    # Nothing retires those rows in the meantime: their directories all exist.
    workspace = open_workspace(settings=settings, require_root=False)
    try:
        plan = plan_repository_set(
            old, workspace.settings.discovery, workspace.repositories.list_all()
        )
        assert plan.missing == (), "a lagging configuration must not retire anything"
    finally:
        workspace.close()

    # And the next init converges.
    run_init(settings, new, config)
    assert {slug: rid for rid, slug, _ in repository_rows(settings)} == before
