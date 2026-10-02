"""Incremental sync and the no-stale-facts invariants (V1_SPEC §12, §19)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from repoatlas.models.repository import Repository, SyncStatus
from repoatlas.parsers.typescript import TypeScriptParser
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace
from tests.conftest import fact_snapshot, git


def _repo(workspace: Workspace, name: str) -> Repository:
    found = workspace.repositories.get_by_name(name)
    assert found is not None
    assert found.id is not None
    return found


def _repo_id(workspace: Workspace, name: str) -> int:
    repo_id = _repo(workspace, name).id
    assert repo_id is not None
    return repo_id


def _sync_one(workspace: Workspace, root: Path, name: str, *, full: bool = False):
    context = workspace.resolver().resolve(root / name)
    return sync(workspace, context, full=full)


def _entity_names(workspace: Workspace, repo_name: str) -> set[str]:
    rows = workspace.database.execute(
        "SELECT name FROM entities WHERE repo_id = ?", (_repo_id(workspace, repo_name),)
    ).fetchall()
    return {row["name"] for row in rows}


# -- classification ---------------------------------------------------------


def test_unchanged_files_are_skipped(initialized: Workspace, workspace_root: Path) -> None:
    report = _sync_one(initialized, workspace_root, "python-service")
    entry = report.repositories[0]
    assert entry.changed == 0
    assert entry.new == 0
    assert entry.deleted == 0
    assert entry.unchanged > 0


def test_changed_file_is_reindexed(initialized: Workspace, workspace_root: Path) -> None:
    target = workspace_root / "python-service" / "src" / "config.py"
    target.write_text(target.read_text() + "\n\ndef added_function() -> int:\n    return 7\n")

    report = _sync_one(initialized, workspace_root, "python-service")
    assert report.repositories[0].changed == 1
    assert "added_function" in _entity_names(initialized, "python-service")


def test_new_file_is_indexed(initialized: Workspace, workspace_root: Path) -> None:
    (workspace_root / "python-service" / "src" / "extra.py").write_text(
        "def brand_new_symbol() -> str:\n    return 'x'\n"
    )
    report = _sync_one(initialized, workspace_root, "python-service")
    assert report.repositories[0].new == 1
    assert "brand_new_symbol" in _entity_names(initialized, "python-service")


def test_parser_failure_logs_repository_and_file(
    initialized: Workspace,
    workspace_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    rel_path = "src/parser-crash.ts"
    (workspace_root / "python-service" / rel_path).write_text("export const value = 1;\n")

    def fail_parse(self: TypeScriptParser, path: Path, content: str):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(TypeScriptParser, "parse", fail_parse)
    with caplog.at_level(logging.ERROR, logger="repoatlas.sync.engine"):
        with pytest.raises(RecursionError):
            _sync_one(initialized, workspace_root, "python-service")

    assert "python-service" in caplog.text
    assert rel_path in caplog.text
    assert "tree-sitter-typescript" in caplog.text
    assert "RecursionError" in caplog.text


def test_deleted_file_removes_its_facts(initialized: Workspace, workspace_root: Path) -> None:
    """A deleted symbol must not continue appearing (V1_SPEC §12)."""
    assert "load_config" in _entity_names(initialized, "python-service")

    (workspace_root / "python-service" / "src" / "config.py").unlink()
    report = _sync_one(initialized, workspace_root, "python-service")

    assert report.repositories[0].deleted == 1
    assert "load_config" not in _entity_names(initialized, "python-service")


def test_deleted_file_removes_its_chunks(initialized: Workspace, workspace_root: Path) -> None:
    (workspace_root / "python-service" / "src" / "config.py").unlink()
    _sync_one(initialized, workspace_root, "python-service")

    hits = initialized.search.find_literal("SERVICE_HOST").hits
    assert all("config.py" not in hit.path for hit in hits)


def test_deleted_file_removes_its_edges(initialized: Workspace, workspace_root: Path) -> None:
    repo_id = _repo_id(initialized, "python-service")
    before = initialized.knowledge.count_edges([repo_id])
    assert before > 0

    (workspace_root / "python-service" / "src" / "config.py").unlink()
    _sync_one(initialized, workspace_root, "python-service")

    assert initialized.knowledge.count_edges([repo_id]) < before


# -- forced rehash ----------------------------------------------------------


def test_branch_switch_forces_full_rehash(initialized: Workspace, workspace_root: Path) -> None:
    """HEAD changes can rewrite files with arbitrary mtimes (V1_SPEC §12)."""
    repo_path = workspace_root / "python-service"
    git(repo_path, "checkout", "--quiet", "-b", "feature")
    (repo_path / "src" / "feature.py").write_text("def feature_symbol():\n    return 1\n")
    git(repo_path, "add", "-A")
    git(repo_path, "commit", "--quiet", "-m", "feature")

    report = _sync_one(initialized, workspace_root, "python-service")
    entry = report.repositories[0]
    assert entry.full_rehash
    assert entry.rehash_reason == "HEAD changed since last index"
    assert "feature_symbol" in _entity_names(initialized, "python-service")


def test_branch_switch_back_removes_branch_only_facts(
    initialized: Workspace, workspace_root: Path
) -> None:
    """Facts from the previous branch must not remain (V1_SPEC §12)."""
    repo_path = workspace_root / "python-service"
    git(repo_path, "checkout", "--quiet", "-b", "feature")
    (repo_path / "src" / "feature.py").write_text("def only_on_feature():\n    return 1\n")
    git(repo_path, "add", "-A")
    git(repo_path, "commit", "--quiet", "-m", "feature")
    _sync_one(initialized, workspace_root, "python-service")
    assert "only_on_feature" in _entity_names(initialized, "python-service")

    git(repo_path, "checkout", "--quiet", "main")
    _sync_one(initialized, workspace_root, "python-service")
    assert "only_on_feature" not in _entity_names(initialized, "python-service")


def test_dirty_state_change_forces_rehash(initialized: Workspace, workspace_root: Path) -> None:
    (workspace_root / "python-service" / "src" / "config.py").write_text("x = 1\n")
    report = _sync_one(initialized, workspace_root, "python-service")
    assert report.repositories[0].full_rehash


def test_full_flag_forces_rehash(initialized: Workspace, workspace_root: Path) -> None:
    report = _sync_one(initialized, workspace_root, "python-service", full=True)
    entry = report.repositories[0]
    assert entry.full_rehash
    assert entry.rehash_reason == "--full requested"


def test_interrupted_sync_forces_rehash_next_time(
    initialized: Workspace, workspace_root: Path
) -> None:
    initialized.repositories.mark_sync_started(_repo_id(initialized, "python-service"))

    report = _sync_one(initialized, workspace_root, "python-service")
    entry = report.repositories[0]
    assert entry.full_rehash
    assert "did not complete" in (entry.rehash_reason or "")


def test_stat_shortcut_is_used_when_nothing_changed(
    initialized: Workspace, workspace_root: Path
) -> None:
    report = _sync_one(initialized, workspace_root, "python-service")
    assert not report.repositories[0].full_rehash


def test_content_change_hidden_from_stat_is_still_detected(
    initialized: Workspace, workspace_root: Path
) -> None:
    """The hash is the authority; stat must not conceal a real change.

    ``helper.py`` is rewritten with identical length and its original mtime
    restored, so stat cannot reveal the edit — git's own index is fooled by it
    too. A separate commit moves HEAD, which disables the stat shortcut, and the
    rehash then finds the hidden edit. This is precisely the protection that
    V1_SPEC §12 requires.
    """
    import os

    repo_path = workspace_root / "python-service"
    target = repo_path / "src" / "deep" / "nested" / "path" / "helper.py"
    original = target.read_text()
    stat_before = target.stat()

    replacement = original.replace("value * 2", "value * 9")
    assert len(replacement) == len(original), "edit must not change the file size"
    target.write_text(replacement)
    os.utime(target, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))

    after = target.stat()
    assert (after.st_size, after.st_mtime_ns) == (
        stat_before.st_size,
        stat_before.st_mtime_ns,
    ), "stat must be indistinguishable for this test to mean anything"

    # An unrelated commit moves HEAD, which is what disables the shortcut.
    (repo_path / "unrelated.py").write_text("UNRELATED = 1\n")
    git(repo_path, "add", "unrelated.py")
    git(repo_path, "commit", "--quiet", "-m", "unrelated change")
    os.utime(target, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))

    report = _sync_one(initialized, workspace_root, "python-service")
    entry = report.repositories[0]
    assert entry.full_rehash
    assert entry.rehash_reason == "HEAD changed since last index"

    rows = initialized.database.execute(
        "SELECT c.text FROM chunks c JOIN files f ON f.id = c.file_id WHERE f.rel_path = ?",
        ("src/deep/nested/path/helper.py",),
    ).fetchall()
    assert any("value * 9" in row["text"] for row in rows)
    assert not any("value * 2" in row["text"] for row in rows)


# -- scope ------------------------------------------------------------------


def test_sync_inside_repository_syncs_only_that_repository(
    initialized: Workspace, workspace_root: Path
) -> None:
    deep = workspace_root / "python-service" / "src" / "deep" / "nested" / "path"
    context = initialized.resolver().resolve(deep)
    report = sync(initialized, context)
    assert [r.repository for r in report.repositories] == ["python-service"]


def test_sync_at_root_syncs_everything(initialized: Workspace, workspace_root: Path) -> None:
    context = initialized.resolver().resolve(workspace_root)
    report = sync(initialized, context)
    assert len(report.repositories) >= 5


# -- security ---------------------------------------------------------------


def test_secret_files_are_never_indexed(initialized: Workspace, workspace_root: Path) -> None:
    """Secrets are excluded at enumeration, before content is read (V1_SPEC §17)."""
    rows = initialized.database.execute(
        "SELECT rel_path FROM files WHERE repo_id = ?",
        (_repo_id(initialized, "python-service"),),
    ).fetchall()
    assert not [r for r in rows if r["rel_path"] == ".env"]

    assert initialized.search.find_literal("super-secret-value-that-must-not-be-indexed").hits == []


def test_repoatlasignore_is_honored(initialized: Workspace, workspace_root: Path) -> None:
    assert initialized.search.find_literal("IGNORED_MARKER_SHOULD_NOT_BE_INDEXED").hits == []


def test_nested_repository_files_belong_to_the_inner_repository(
    initialized: Workspace,
) -> None:
    rows = initialized.database.execute(
        "SELECT rel_path FROM files WHERE repo_id = ?",
        (_repo_id(initialized, "nested-outer"),),
    ).fetchall()
    paths = {row["rel_path"] for row in rows}
    assert "outer.py" in paths
    assert not any("nested-inner" in p for p in paths)


# -- invariants -------------------------------------------------------------


def test_invariant_no_facts_reference_missing_files(
    initialized: Workspace, workspace_root: Path
) -> None:
    """Invariant 1: no fact survives its source file (ARCHITECTURE §9)."""
    (workspace_root / "python-service" / "src" / "app.py").unlink()
    (workspace_root / "deployment" / "service" / "pillar.sls").unlink()
    context = initialized.resolver().resolve(workspace_root)
    sync(initialized, context)

    rows = initialized.database.execute(
        "SELECT r.path AS repo_path, f.rel_path FROM files f "
        "JOIN repositories r ON r.id = f.repo_id"
    ).fetchall()
    for row in rows:
        assert (Path(row["repo_path"]) / row["rel_path"]).exists(), row["rel_path"]

    orphans = initialized.database.execute(
        "SELECT COUNT(*) AS n FROM entities "
        "WHERE file_id IS NOT NULL AND file_id NOT IN (SELECT id FROM files)"
    ).fetchone()
    assert orphans["n"] == 0


def test_invariant_incremental_equals_full_reindex(
    workspace: Workspace, workspace_root: Path, tmp_path: Path
) -> None:
    """Invariant 2: incremental sync across changes equals a full reindex.

    This is the invariant that catches the whole class of stale-data bugs.
    """
    from repoatlas.services.init_service import initialize
    from repoatlas.services.workspace import open_workspace

    initialize(workspace, workspace_root, config_path=tmp_path / "config.yaml")

    repo_path = workspace_root / "python-service"
    (repo_path / "src" / "config.py").write_text("def only_symbol():\n    return 1\n")
    (repo_path / "src" / "app.py").unlink()
    (repo_path / "src" / "created.py").write_text("def created_symbol():\n    return 2\n")
    git(repo_path, "add", "-A")
    git(repo_path, "commit", "--quiet", "-m", "mutations")
    git(repo_path, "checkout", "--quiet", "-b", "second")
    (repo_path / "src" / "branch_only.py").write_text("def branch_symbol():\n    return 3\n")
    git(repo_path, "add", "-A")
    git(repo_path, "commit", "--quiet", "-m", "branch work")

    context = workspace.resolver().resolve(workspace_root)
    sync(workspace, context)
    incremental = fact_snapshot(workspace)

    # A pristine database indexing the identical tree.
    fresh_settings = workspace.settings.model_copy(
        update={
            "storage": workspace.settings.storage.model_copy(
                update={"database": tmp_path / "fresh" / "atlas.db"}
            )
        }
    )
    fresh = open_workspace(settings=fresh_settings, require_root=False)
    initialize(fresh, workspace_root, config_path=tmp_path / "fresh-config.yaml")
    full = fact_snapshot(fresh)
    fresh.close()

    assert incremental == full


def test_sync_status_recorded_as_ok(initialized: Workspace, workspace_root: Path) -> None:
    _sync_one(initialized, workspace_root, "python-service")
    assert _repo(initialized, "python-service").last_sync_status is SyncStatus.OK


def test_detected_metadata_is_persisted(initialized: Workspace) -> None:
    """Languages, manifests and lockfiles are repository metadata (V1_SPEC §5)."""
    python_service = _repo(initialized, "python-service")
    assert "Python" in python_service.languages
    assert "pyproject.toml" in python_service.manifests

    ts_service = _repo(initialized, "ts-service")
    assert "TypeScript" in ts_service.languages
    assert "package.json" in ts_service.manifests


def test_metadata_reflects_the_whole_tree_not_just_reindexed_files(
    initialized: Workspace, workspace_root: Path
) -> None:
    """A no-op sync must not shrink the recorded metadata."""
    before = _repo(initialized, "python-service")
    _sync_one(initialized, workspace_root, "python-service")
    after = _repo(initialized, "python-service")
    assert after.languages == before.languages
    assert after.manifests == before.manifests


def test_a_virtual_environment_git_lists_is_not_indexed(tmp_path: Path) -> None:
    """An environment that does not ignore itself is listed by Git in full; none is indexed."""
    from repoatlas.sync.scanner import scan_repository
    from tests.conftest import init_git_repository

    repo = tmp_path / "app"
    environment = repo / "tools" / "py38env"
    (environment / "lib").mkdir(parents=True)
    (repo / "main.py").write_text("x = 1\n")
    (environment / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (environment / "lib" / "site.py").write_text("y = 2\n")
    init_git_repository(repo)

    assert [f.rel_path for f in scan_repository(repo)] == ["main.py"]


def test_a_pyvenv_cfg_at_the_top_leaves_the_repository_s_own_files(tmp_path: Path) -> None:
    from repoatlas.sync.scanner import scan_repository
    from tests.conftest import init_git_repository

    repo = tmp_path / "made-in-place"
    repo.mkdir()
    (repo / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (repo / "main.py").write_text("x = 1\n")
    init_git_repository(repo)

    assert [f.rel_path for f in scan_repository(repo)] == ["main.py", "pyvenv.cfg"]
