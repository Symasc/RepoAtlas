"""An unreadable file must not abort a sync (V1_SPEC §7, §12).

`_index_file` already treats an `OSError` as a diagnostic, but a file is read
twice: once by the planner to hash it, and once by the indexer to parse it. Only
the second read was guarded, so a file that becomes unreadable between syncs —
permissions changed, a tracked file deleted from the working tree but still in the
git index, a network mount going away — raised out of `plan_sync` and took the
whole repository with it.

The right behaviour is the one the indexer already had: treat it as a change we
cannot verify, and do the work rather than trusting stale state.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace
from tests.conftest import git

pytestmark = pytest.mark.adversarial


def _sync(workspace: Workspace, root: Path, repo: str):
    return sync(workspace, workspace.resolver().resolve(root / repo))


def _indexed(workspace: Workspace, repo: str, rel_path: str) -> int:
    return workspace.database.execute(
        "SELECT COUNT(*) AS n FROM files f JOIN repositories r ON r.id = f.repo_id "
        "WHERE r.name = ? AND f.rel_path = ?",
        (repo, rel_path),
    ).fetchone()["n"]


def test_hashing_an_unreadable_file_does_not_raise(tmp_path: Path) -> None:
    """A hash that cannot be taken is ``None``, not an exception.

    Hashing is expressed against a containment boundary rather than a bare path,
    so this also covers the two ways a hash can be unavailable — the file is not
    readable, and the file is not ours — through the one code path that decides
    both.
    """
    from repoatlas.containment import RepositoryBoundary
    from repoatlas.sync.scanner import hash_within

    boundary = RepositoryBoundary(tmp_path)
    assert hash_within(boundary, "nonexistent/path/that/cannot/be/read") is None
    assert hash_within(boundary, "../escape") is None


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_a_file_that_becomes_unreadable_does_not_abort_the_repository(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    pillar = adversarial_root / "deployment" / "service" / "pillar.sls"
    pillar.write_text(pillar.read_text() + "\n# touched so the hash must be recomputed\n")
    pillar.chmod(0o000)
    try:
        report = _sync(adversarial, adversarial_root, "deployment")
        assert report.repositories
        # Everything else in the repository is still indexed.
        assert _indexed(adversarial, "deployment", "service/init.sls") == 1
    finally:
        pillar.chmod(stat.S_IRUSR | stat.S_IWUSR)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_an_unreadable_file_is_reported_rather_than_silently_skipped(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """Silence would look exactly like success."""
    pillar = adversarial_root / "deployment" / "service" / "pillar.sls"
    pillar.write_text(pillar.read_text() + "\n# changed\n")
    pillar.chmod(0o000)
    try:
        report = _sync(adversarial, adversarial_root, "deployment")
        messages = " ".join(d.message for entry in report.repositories for d in entry.diagnostics)
        assert "unreadable" in messages.lower(), messages
    finally:
        pillar.chmod(stat.S_IRUSR | stat.S_IWUSR)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_the_repository_recovers_once_the_file_is_readable_again(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    pillar = adversarial_root / "deployment" / "service" / "pillar.sls"
    pillar.write_text("service:\n  host: recovered.example\n  port: 9999\n")
    pillar.chmod(0o000)
    try:
        _sync(adversarial, adversarial_root, "deployment")
    finally:
        pillar.chmod(stat.S_IRUSR | stat.S_IWUSR)

    _sync(adversarial, adversarial_root, "deployment")
    assert adversarial.search.search_literal("recovered.example")


def test_a_tracked_file_removed_from_the_working_tree_is_handled(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """`git ls-files --cached` still lists a file deleted but not staged, so the
    scan enumerates a path that cannot be read. This is an ordinary state of a
    working tree, not an exotic failure."""
    repo = adversarial_root / "python-service"
    (repo / "src" / "config.py").unlink()
    report = _sync(adversarial, adversarial_root, "python-service")
    assert report.repositories
    assert _indexed(adversarial, "python-service", "src/config.py") == 0
    assert not adversarial.search.find_entities_by_name(["build_config"], None, 5)


def test_a_file_replaced_by_a_directory_does_not_abort_the_sync(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """Reading it raises `IsADirectoryError`, a different `OSError` subclass."""
    repo = adversarial_root / "scripts"
    target = repo / "notify.sh"
    target.unlink()
    target.mkdir()
    (target / "inside.txt").write_text("x\n")
    git(repo, "add", "-A")
    report = _sync(adversarial, adversarial_root, "scripts")
    assert report.repositories
    assert _indexed(adversarial, "scripts", "notify.sh") == 0


def test_a_dangling_symlink_does_not_abort_the_sync(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    repo = adversarial_root / "scripts"
    (repo / "dangling.sh").symlink_to(repo / "does-not-exist.sh")
    git(repo, "add", "-A")
    report = _sync(adversarial, adversarial_root, "scripts")
    assert report.repositories
    assert adversarial.search.search_literal("deploy_helper")
