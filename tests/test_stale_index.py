"""Stale-index stress (V1_SPEC §12, ARCHITECTURE.md §9).

Every test here asserts the same thing in a different way: **invariant 2** — an
incremental sync across a change yields the same facts as a full reindex of the
same tree. That single equality is what makes the whole class of stale-data bugs
detectable, because a fact that should have been removed shows up as a difference
no matter which table it lives in.

The scenarios are the ones that actually break naive indexers: branch switches
that rewrite files without touching mtime, deletions, renames and moves that
preserve content byte-for-byte, dirty working trees, and dependency edits that
change relationships in *another* repository.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from repoatlas.config.settings import Settings
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace
from tests.conftest import fact_snapshot, git, reindex_from_scratch


def _sync_all(workspace: Workspace, root: Path) -> None:
    sync(workspace, workspace.resolver().resolve(root))


def _assert_matches_full_reindex(
    workspace: Workspace, settings: Settings, root: Path, tmp_path: Path, tag: str
) -> None:
    """Sync incrementally, then prove the result equals a from-scratch index."""
    _sync_all(workspace, root)
    assert fact_snapshot(workspace) == reindex_from_scratch(settings, root, tmp_path, tag)


def _commit(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", message)


def _paths(workspace: Workspace, repo_name: str) -> set[str]:
    rows = workspace.database.execute(
        "SELECT f.rel_path FROM files f JOIN repositories r ON r.id = f.repo_id WHERE r.name = ?",
        (repo_name,),
    ).fetchall()
    return {row["rel_path"] for row in rows}


def _entity_names(workspace: Workspace, repo_name: str) -> set[str]:
    rows = workspace.database.execute(
        "SELECT e.name FROM entities e JOIN repositories r ON r.id = e.repo_id WHERE r.name = ?",
        (repo_name,),
    ).fetchall()
    return {row["name"] for row in rows}


# -- branch switching -------------------------------------------------------


def test_switching_between_diverged_branches_leaves_no_residue(
    initialized: Workspace, settings: Settings, workspace_root: Path, tmp_path: Path
) -> None:
    """Three checkouts, each adding and removing facts, then back to the start."""
    repo = workspace_root / "python-service"

    git(repo, "checkout", "--quiet", "-b", "alpha")
    (repo / "src" / "alpha.py").write_text("def alpha_symbol():\n    return 1\n")
    (repo / "src" / "config.py").unlink()
    _commit(repo, "alpha")
    _sync_all(initialized, workspace_root)
    assert "alpha_symbol" in _entity_names(initialized, "python-service")

    git(repo, "checkout", "--quiet", "-b", "beta", "main")
    (repo / "src" / "beta.py").write_text("def beta_symbol():\n    return 2\n")
    _commit(repo, "beta")
    _sync_all(initialized, workspace_root)
    names = _entity_names(initialized, "python-service")
    assert "beta_symbol" in names
    assert "alpha_symbol" not in names

    git(repo, "checkout", "--quiet", "main")
    _assert_matches_full_reindex(initialized, settings, workspace_root, tmp_path, "back-to-main")
    assert _entity_names(initialized, "python-service").isdisjoint({"alpha_symbol", "beta_symbol"})


def test_a_branch_switch_that_only_changes_content_is_still_seen(
    initialized: Workspace, settings: Settings, workspace_root: Path, tmp_path: Path
) -> None:
    """No file is added or removed, so only the hash can reveal the change."""
    repo = workspace_root / "python-service"
    git(repo, "checkout", "--quiet", "-b", "rewritten")
    (repo / "src" / "config.py").write_text("def rewritten_only():\n    return 1\n")
    _commit(repo, "rewrite")

    _assert_matches_full_reindex(initialized, settings, workspace_root, tmp_path, "rewritten")
    assert "rewritten_only" in _entity_names(initialized, "python-service")
    assert "load_config" not in _entity_names(initialized, "python-service")


# -- deletion ---------------------------------------------------------------


def test_deleting_a_whole_directory_removes_every_fact_beneath_it(
    initialized: Workspace, settings: Settings, workspace_root: Path, tmp_path: Path
) -> None:
    repo = workspace_root / "python-service"
    shutil.rmtree(repo / "src" / "deep")
    _commit(repo, "drop deep tree")

    _assert_matches_full_reindex(initialized, settings, workspace_root, tmp_path, "rmtree")
    assert not [p for p in _paths(initialized, "python-service") if p.startswith("src/deep/")]


def test_deleting_the_target_of_a_cross_repository_edge_downgrades_it_to_a_claim(
    initialized: Workspace, settings: Settings, workspace_root: Path, tmp_path: Path
) -> None:
    """``scripts`` invokes ``python-service``; removing the callee must not leave
    an edge asserting a file that no longer exists."""
    before = initialized.database.execute(
        "SELECT COUNT(*) AS n FROM edges e JOIN entities dst ON dst.id = e.dst_id "
        "WHERE dst.key = 'file:python-service:src/app.py'"
    ).fetchone()["n"]
    assert before > 0

    repo = workspace_root / "python-service"
    (repo / "src" / "app.py").unlink()
    _commit(repo, "remove app")

    _assert_matches_full_reindex(initialized, settings, workspace_root, tmp_path, "drop-callee")
    after = initialized.database.execute(
        "SELECT COUNT(*) AS n FROM edges e JOIN entities dst ON dst.id = e.dst_id "
        "WHERE dst.key = 'file:python-service:src/app.py'"
    ).fetchone()["n"]
    assert after == 0

    # The claim survives, so re-adding the file restores the edge without a
    # reparse of the *calling* repository (ARCHITECTURE.md §5).
    standing = initialized.database.execute(
        "SELECT COUNT(*) AS n FROM unresolved_refs WHERE target_key LIKE '%app.py%'"
    ).fetchone()["n"]
    assert standing > 0


def test_a_deleted_file_that_returns_is_indexed_again(
    initialized: Workspace, settings: Settings, workspace_root: Path, tmp_path: Path
) -> None:
    repo = workspace_root / "python-service"
    original = (repo / "src" / "config.py").read_text()
    (repo / "src" / "config.py").unlink()
    _commit(repo, "remove config")
    _sync_all(initialized, workspace_root)
    assert "load_config" not in _entity_names(initialized, "python-service")

    (repo / "src" / "config.py").write_text(original)
    _commit(repo, "restore config")
    _assert_matches_full_reindex(initialized, settings, workspace_root, tmp_path, "restored")
    assert "load_config" in _entity_names(initialized, "python-service")


# -- rename and move --------------------------------------------------------


def test_renaming_a_file_moves_its_facts_and_leaves_none_behind(
    initialized: Workspace, settings: Settings, workspace_root: Path, tmp_path: Path
) -> None:
    """Content is byte-identical, so only the path changed. Provenance must follow
    the path: a citation to the old location would be a false statement."""
    repo = workspace_root / "python-service"
    git(repo, "mv", "src/config.py", "src/configuration.py")
    _commit(repo, "rename config")

    _assert_matches_full_reindex(initialized, settings, workspace_root, tmp_path, "rename")
    paths = _paths(initialized, "python-service")
    assert "src/configuration.py" in paths
    assert "src/config.py" not in paths

    rows = initialized.database.execute(
        "SELECT f.rel_path FROM entities e JOIN files f ON f.id = e.file_id "
        "WHERE e.name = 'load_config'"
    ).fetchall()
    assert [row["rel_path"] for row in rows] == ["src/configuration.py"]


def test_moving_a_file_deeper_keeps_the_index_consistent(
    initialized: Workspace, settings: Settings, workspace_root: Path, tmp_path: Path
) -> None:
    repo = workspace_root / "python-service"
    (repo / "src" / "moved").mkdir()
    git(repo, "mv", "src/deep/nested/path/helper.py", "src/moved/helper.py")
    _commit(repo, "move helper")

    _assert_matches_full_reindex(initialized, settings, workspace_root, tmp_path, "move-deep")
    paths = _paths(initialized, "python-service")
    assert "src/moved/helper.py" in paths
    assert "src/deep/nested/path/helper.py" not in paths


def test_moving_a_file_between_repositories_reassigns_it(
    initialized: Workspace, settings: Settings, workspace_root: Path, tmp_path: Path
) -> None:
    """The same content in a different repository is a different fact: its
    provenance names the repository, and both sides must agree."""
    source = workspace_root / "python-service"
    destination = workspace_root / "scripts"
    content = (source / "src" / "deep" / "nested" / "path" / "helper.py").read_text()
    (source / "src" / "deep" / "nested" / "path" / "helper.py").unlink()
    (destination / "helper.py").write_text(content)
    _commit(source, "move out")
    _commit(destination, "move in")

    _assert_matches_full_reindex(initialized, settings, workspace_root, tmp_path, "cross-repo-move")
    assert "helper.py" in _paths(initialized, "scripts")
    assert "src/deep/nested/path/helper.py" not in _paths(initialized, "python-service")


def test_swapping_two_files_does_not_confuse_content_addressing(
    initialized: Workspace, settings: Settings, workspace_root: Path, tmp_path: Path
) -> None:
    """Both hashes still exist, but at each other's paths. Keying anything on
    content alone rather than on ``(file, content)`` would silently pass."""
    repo = workspace_root / "python-service"
    first, second = repo / "src" / "config.py", repo / "src" / "app.py"
    first_text, second_text = first.read_text(), second.read_text()
    first.write_text(second_text)
    second.write_text(first_text)
    _commit(repo, "swap")

    _assert_matches_full_reindex(initialized, settings, workspace_root, tmp_path, "swap")
    rows = initialized.database.execute(
        "SELECT f.rel_path FROM entities e JOIN files f ON f.id = e.file_id "
        "WHERE e.name = 'load_config' AND e.type = 'function'"
    ).fetchall()
    assert [row["rel_path"] for row in rows] == ["src/app.py"]


# -- dirty working trees ----------------------------------------------------


def test_uncommitted_edits_are_indexed(
    initialized: Workspace, settings: Settings, workspace_root: Path, tmp_path: Path
) -> None:
    """RepoAtlas indexes the working tree, not HEAD: the answer must describe the
    code as it is on disk right now (ARCHITECTURE.md §8)."""
    repo = workspace_root / "python-service"
    (repo / "src" / "config.py").write_text("def uncommitted_symbol():\n    return 1\n")

    _assert_matches_full_reindex(initialized, settings, workspace_root, tmp_path, "dirty-edit")
    assert "uncommitted_symbol" in _entity_names(initialized, "python-service")


def test_untracked_files_are_indexed_and_then_removed_cleanly(
    initialized: Workspace, settings: Settings, workspace_root: Path, tmp_path: Path
) -> None:
    repo = workspace_root / "python-service"
    scratch = repo / "src" / "scratch.py"
    scratch.write_text("def scratch_symbol():\n    return 1\n")
    _sync_all(initialized, workspace_root)
    assert "scratch_symbol" in _entity_names(initialized, "python-service")

    scratch.unlink()
    _assert_matches_full_reindex(initialized, settings, workspace_root, tmp_path, "untracked-gone")
    assert "scratch_symbol" not in _entity_names(initialized, "python-service")


def test_a_staged_deletion_removes_facts_before_it_is_committed(
    initialized: Workspace, settings: Settings, workspace_root: Path, tmp_path: Path
) -> None:
    repo = workspace_root / "python-service"
    git(repo, "rm", "--quiet", "src/config.py")

    _assert_matches_full_reindex(initialized, settings, workspace_root, tmp_path, "staged-delete")
    assert "src/config.py" not in _paths(initialized, "python-service")


def test_going_from_dirty_back_to_clean_restores_the_committed_facts(
    initialized: Workspace, settings: Settings, workspace_root: Path, tmp_path: Path
) -> None:
    repo = workspace_root / "python-service"
    (repo / "src" / "config.py").write_text("def temporary():\n    return 1\n")
    _sync_all(initialized, workspace_root)
    assert "temporary" in _entity_names(initialized, "python-service")

    git(repo, "checkout", "--quiet", "--", "src/config.py")
    _assert_matches_full_reindex(initialized, settings, workspace_root, tmp_path, "reverted")
    names = _entity_names(initialized, "python-service")
    assert "temporary" not in names
    assert "load_config" in names


# -- dependency changes -----------------------------------------------------


def test_removing_a_dependency_removes_the_cross_repository_edge(
    initialized: Workspace, settings: Settings, workspace_root: Path, tmp_path: Path
) -> None:
    """``python-service`` depends on ``common-lib`` through PEP 503 normalization.
    Dropping the declaration must drop the edge, not merely stop refreshing it."""
    linked = initialized.database.execute(
        "SELECT COUNT(*) AS n FROM edges e "
        "JOIN entities src ON src.id = e.src_id JOIN entities dst ON dst.id = e.dst_id "
        "WHERE e.type = 'DEPENDS_ON' AND src.key = 'repo:python-service' "
        "AND dst.key = 'repo:common-lib'"
    ).fetchone()["n"]
    assert linked > 0

    manifest = workspace_root / "python-service" / "pyproject.toml"
    manifest.write_text(
        manifest.read_text().replace('"common-lib>=1.0"', '"unrelated-package>=1.0"')
    )
    _commit(workspace_root / "python-service", "drop dependency")

    _assert_matches_full_reindex(initialized, settings, workspace_root, tmp_path, "dep-removed")
    remaining = initialized.database.execute(
        "SELECT COUNT(*) AS n FROM edges e "
        "JOIN entities src ON src.id = e.src_id JOIN entities dst ON dst.id = e.dst_id "
        "WHERE e.type = 'DEPENDS_ON' AND src.key = 'repo:python-service' "
        "AND dst.key = 'repo:common-lib'"
    ).fetchone()["n"]
    assert remaining == 0


def test_adding_a_dependency_creates_the_edge_without_touching_the_provider(
    initialized: Workspace, settings: Settings, workspace_root: Path, tmp_path: Path
) -> None:
    manifest = workspace_root / "ts-service" / "package.json"
    manifest.write_text(
        manifest.read_text().replace(
            '"dependencies": {', '"dependencies": {\n    "left-pad": "^1.3.0",'
        )
    )
    _commit(workspace_root / "ts-service", "add dependency")

    _assert_matches_full_reindex(initialized, settings, workspace_root, tmp_path, "dep-added")
    row = initialized.database.execute(
        "SELECT constraint_text FROM dependencies d JOIN repositories r ON r.id = d.repo_id "
        "WHERE r.name = 'ts-service' AND d.normalized_name = 'left-pad' AND d.role = 'declared'"
    ).fetchone()
    assert row is not None
    assert row["constraint_text"] == "^1.3.0"


def test_a_provider_appearing_later_gains_the_edge_from_a_standing_claim(
    initialized: Workspace, settings: Settings, workspace_root: Path, tmp_path: Path
) -> None:
    """The point of retaining ``unresolved_refs``: a repository indexed after the
    one that depends on it must gain the edge without reparsing the dependant."""
    manifest = workspace_root / "python-service" / "pyproject.toml"
    manifest.write_text(manifest.read_text().replace('"common-lib>=1.0"', '"late-arrival>=1.0"'))
    _commit(workspace_root / "python-service", "depend on something absent")
    _sync_all(initialized, workspace_root)

    provider = workspace_root / "common-lib" / "pyproject.toml"
    provider.write_text(
        provider.read_text().replace('name = "common_lib"', 'name = "late_arrival"')
    )
    _commit(workspace_root / "common-lib", "rename to the awaited name")

    _assert_matches_full_reindex(initialized, settings, workspace_root, tmp_path, "late-provider")
    found = initialized.database.execute(
        "SELECT COUNT(*) AS n FROM edges e "
        "JOIN entities src ON src.id = e.src_id JOIN entities dst ON dst.id = e.dst_id "
        "WHERE e.type = 'DEPENDS_ON' AND src.key = 'repo:python-service' "
        "AND dst.key = 'repo:common-lib'"
    ).fetchone()["n"]
    assert found > 0
