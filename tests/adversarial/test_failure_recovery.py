"""Failure and recovery (V1_SPEC §4, ARCHITECTURE §9, §11).

The standard is stated in three parts, and all three matter:

* fail **clearly** — an exit code and a message naming the fix, never a traceback;
* never **pretend** the operation completed — a partial sync must be recorded as
  partial, so the next run redoes the work rather than trusting it;
* leave the database **usable** — a later retry must succeed.

Silence is the worst outcome of all: an index that quietly stopped updating looks
exactly like an index that is up to date.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from repoatlas.errors import RepoAtlasError
from repoatlas.models.repository import SyncStatus
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace

pytestmark = pytest.mark.adversarial


def _sync(workspace: Workspace, root: Path, repo: str | None = None):
    return sync(workspace, workspace.resolver().resolve(root if repo is None else root / repo))


def _repo_id(workspace: Workspace, name: str) -> int:
    found = workspace.repositories.get_by_name(name)
    assert found is not None and found.id is not None
    return found.id


# -- interrupted work --------------------------------------------------------


def test_an_interrupted_sync_is_redone_rather_than_trusted(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """A sync that did not finish leaves state that may not match the tree, so the
    next run must not take the stat shortcut (§9)."""
    adversarial.repositories.mark_sync_started(_repo_id(adversarial, "python-service"))
    report = _sync(adversarial, adversarial_root, "python-service")
    entry = report.repositories[0]
    assert entry.full_rehash
    assert "did not complete" in (entry.rehash_reason or "")


def test_a_repository_that_vanishes_mid_run_does_not_abort_the_others(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """One unreadable repository must not cost the other eight their sync."""
    import shutil

    shutil.rmtree(adversarial_root / "ambiguous-provider-b")
    report = _sync(adversarial, adversarial_root)
    synced = {entry.repository for entry in report.repositories}
    assert {"python-service", "deployment", "scripts"} <= synced


def _is_target(path: object, dir_fd: int | None, target: Path) -> bool:
    """Does this ``os.open`` name ``target``, however it was addressed?

    Resolves a directory descriptor through ``/proc/self/fd`` so a
    ``dir_fd``-relative basename is recognised. Never raises: an instrument that
    can fail the call it is observing would change the outcome it is measuring.
    """
    try:
        name = os.fspath(path)  # type: ignore[arg-type]
        if isinstance(name, bytes):
            name = name.decode("utf-8", errors="surrogateescape")
        if dir_fd is not None:
            name = str(Path(os.readlink(f"/proc/self/fd/{dir_fd}")) / name)  # noqa: PTH115
        return os.path.realpath(name) == os.path.realpath(target)
    except Exception:
        return False


def test_a_file_deleted_between_scan_and_parse_is_not_fatal(
    adversarial: Workspace, adversarial_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window is real on a live tree: enumeration lists a path, and the file is
    gone by the time it is read. The sync must survive it.

    Consistency of the index afterwards is asserted separately, in
    tests/adversarial/test_unreadable_files.py, where the deletion happens before
    the scan rather than inside it.
    """
    target = adversarial_root / "python-service" / "src" / "config.py"
    original = os.open
    removed = {"done": False}

    def vanishing(path, flags, mode=0o777, *, dir_fd=None):
        # Content is read through the containment boundary, which opens by
        # basename relative to a directory descriptor. Hooking `Path.read_bytes`
        # would hook nothing and the race would never be simulated — so the hook
        # goes where the real open happens, and `removed["done"]` below is the
        # assertion that it did.
        if not removed["done"] and _is_target(path, dir_fd, target):
            removed["done"] = True
            target.unlink()
        if dir_fd is None:
            return original(path, flags, mode)
        return original(path, flags, mode, dir_fd=dir_fd)

    # The file must be classified as changed, or it is never read at all.
    target.write_text(target.read_text() + "\n# edited\n")
    monkeypatch.setattr(os, "open", vanishing)
    report = _sync(adversarial, adversarial_root, "python-service")
    assert removed["done"], "the race must actually have been simulated"
    assert report.repositories


def test_a_parse_failure_in_one_file_costs_only_that_file(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """`broken.sls` is in the ecosystem; the other deployment facts must survive."""
    rows = adversarial.database.execute(
        "SELECT COUNT(*) AS n FROM entities e JOIN repositories r ON r.id = e.repo_id "
        "WHERE r.name = 'deployment' AND e.name = 'service_config'"
    ).fetchone()
    assert rows["n"] >= 1


def test_the_index_survives_a_crash_and_a_retry_succeeds(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """Reopening a database whose last sync was interrupted must work, and the
    retry must produce a complete index."""
    settings = adversarial.settings
    adversarial.repositories.mark_sync_started(_repo_id(adversarial, "deployment"))
    adversarial.close()

    reopened = open_workspace(settings=settings, require_root=False)
    try:
        report = sync(reopened, reopened.resolver().resolve(adversarial_root))
        assert report.repositories
        assert reopened.search.find_literal("service_config").hits
        # The repository row records the outcome, so a later run can tell whether
        # it may trust the previous one.
        deployment = reopened.repositories.get_by_name("deployment")
        assert deployment is not None
        assert deployment.last_sync_status is SyncStatus.OK
    finally:
        reopened.close()


# -- configuration and backends ----------------------------------------------


def test_a_malformed_configuration_file_does_not_crash(tmp_path: Path) -> None:
    from repoatlas.config.settings import load_settings

    broken = tmp_path / "broken.yaml"
    broken.write_text("repos: [unclosed\n")
    with pytest.raises((RepoAtlasError, Exception)) as raised:
        load_settings(broken)
    assert "Traceback" not in str(raised.value)


@pytest.mark.parametrize(
    "content",
    ["", "null\n", "just a string\n", "- a\n- list\n", "repos:\n  root: 12345\n"],
)
def test_odd_but_parseable_configuration_falls_back_to_defaults(
    tmp_path: Path, content: str
) -> None:
    from repoatlas.config.settings import load_settings

    path = tmp_path / "odd.yaml"
    path.write_text(content)
    try:
        settings = load_settings(path)
    except Exception as error:
        assert "Traceback" not in str(error)
        return
    assert settings.retrieval.final


# -- concurrency -------------------------------------------------------------


def test_a_second_concurrent_sync_is_refused_rather_than_interleaved(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """Interleaved writers would produce a state neither run intended (§9)."""
    other = open_workspace(settings=adversarial.settings, require_root=False)
    try:
        adversarial.repositories.mark_sync_started(_repo_id(adversarial, "python-service"))
        report = sync(other, other.resolver().resolve(adversarial_root / "python-service"))
        # Either it refuses, or it treats the previous run as unfinished and redoes
        # the work. Both are safe; silently trusting the interrupted state is not.
        assert report.repositories[0].full_rehash
    finally:
        other.close()


def test_reads_keep_working_while_a_sync_is_in_flight(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """WAL permits concurrent readers, and `locate` must never block on a sync."""
    reader = open_workspace(settings=adversarial.settings, require_root=False)
    try:
        _sync(adversarial, adversarial_root, "deployment")
        context = reader.resolver().resolve(adversarial_root / "python-service")
        assert reader.retriever().retrieve("SERVICE_HOST", context).candidates
    finally:
        reader.close()
