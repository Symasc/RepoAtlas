"""Parsing in worker processes, and committing a large pass in groups (repoatlas.sync.derive).

A pass of enough files parses them in a pool of worker processes and commits them a group
at a time. Neither may change what is indexed, so the claims here are:

* the same tree indexes to the **same database**, row for row and id for id, whichever way
  it was parsed: files are written in plan order however the workers finish;
* a pool whose parsers would produce different facts than this process's is never used;
* a small pass never starts a process;
* a sync killed while its workers run leaves no worker behind holding anything, and the
  next sync proceeds.

What a failure inside a commit group leaves is asserted by the crash-consistency suite,
over a pass large enough to be grouped (tests/adversarial/test_interrupted_sync_recovery).
"""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.services.init_service import initialize
from repoatlas.services.workspace import open_workspace
from repoatlas.sync.derive import POOL_THRESHOLD, WORKERS_ENV, Deriver
from tests.conftest import init_git_repository

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Files in the generated repository: past the pool threshold, and past one commit group.
FILES = 160


def _tree(root: Path) -> Path:
    """One repository of every kind of file a pass handles differently."""
    repo = root / "svc"
    (repo / "src").mkdir(parents=True)
    (repo / "web").mkdir()
    for index in range(FILES):
        neighbour = (index + 1) % FILES
        (repo / "src" / f"m{index}.py").write_text(
            f"import os\nimport m{neighbour}\n"
            f'V{index} = os.environ["E{index}"]\n\n\n'
            f"def f{index}(x):\n    return m{neighbour}.f{neighbour}(x)\n"
        )
        if index % 4 == 0:
            (repo / "web" / f"c{index}.ts").write_text(
                f'import {{ g{neighbour} }} from "./c{neighbour}";\n'
                f"export function g{index}(): number {{ return 1; }}\n"
                f'export const url{index} = fetch("/api/items/{index}");\n'
            )
    (repo / "package.json").write_text(
        '{"name":"svc","version":"1.0.0","dependencies":{"express":"^4.0.0"}}\n'
    )
    (repo / "pnpm-lock.yaml").write_text(
        "lockfileVersion: '9.0'\npackages:\n"
        + "".join(f"  /pkg{i}@1.0.{i}:\n    resolution: {{}}\n" for i in range(40))
    )
    (repo / "config.yaml").write_text("service:\n  host: example\n  port: 8080\n")
    (repo / "image.bin").write_bytes(b"\x89PNG\0\0\0binary")
    (repo / "latin1.txt").write_bytes(b"caf\xe9\n")
    (repo / "big.txt").write_text("x" * 10_000)
    (repo / "run.sh").write_text("#!/bin/bash\nexport SERVICE_HOST=prod\npython src/m1.py\n")
    init_git_repository(repo)
    return root


@pytest.fixture(scope="module")
def tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _tree(tmp_path_factory.mktemp("parallel") / "repos")


def _index(tmp_path: Path, root: Path, tag: str, workers: int) -> Path:
    database = tmp_path / tag / "atlas.db"
    settings = Settings.model_validate(
        {"storage": {"database": str(database)}, "indexing": {"max_file_bytes": 4096}}
    )
    os.environ[WORKERS_ENV] = str(workers)
    try:
        workspace = open_workspace(settings=settings, require_root=False)
        try:
            initialize(workspace, root, config_path=tmp_path / f"{tag}.yaml")
        finally:
            workspace.close()
    finally:
        del os.environ[WORKERS_ENV]
    return database


def _tables(database: Path) -> dict[str, list[tuple[object, ...]]]:
    """Every row of every table, ids included, timestamps excluded."""
    connection = sqlite3.connect(database)
    try:
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        dumped: dict[str, list[tuple[object, ...]]] = {}
        for name in sorted(names):
            columns = [
                row[1]
                for row in connection.execute(f"PRAGMA table_info({name})")
                if not str(row[1]).endswith("_at")
            ]
            if not columns:
                continue
            listed = ", ".join(f'"{column}"' for column in columns)
            dumped[name] = connection.execute(
                f'SELECT {listed} FROM "{name}" ORDER BY {listed}'  # noqa: S608 -- names from sqlite_master
            ).fetchall()
        return dumped
    finally:
        connection.close()


@pytest.fixture
def pools_started(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[bool]]:
    """Whether each pool a run tried to start was started."""
    started: list[bool] = []
    original = Deriver._start

    def start(self: Deriver):
        pool = original(self)
        started.append(pool is not None)
        return pool

    monkeypatch.setattr(Deriver, "_start", start)
    yield started


def test_a_parallel_pass_indexes_exactly_what_a_serial_pass_does(
    tree: Path, tmp_path: Path, pools_started: list[bool]
) -> None:
    serial = _index(tmp_path, tree, "serial", workers=1)
    assert pools_started == [], "one worker must parse in this process"

    parallel = _index(tmp_path, tree, "parallel", workers=4)
    assert pools_started == [True], "the pass never reached the workers, so nothing was proven"

    expected, observed = _tables(serial), _tables(parallel)
    assert expected["files"], "nothing was indexed"
    differing = [name for name in expected if expected[name] != observed.get(name)]
    assert not differing, f"parsing in workers changed: {differing}"


def test_a_pool_whose_parsers_differ_is_never_used(
    tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pools_started: list[bool]
) -> None:
    """A worker's facts recorded under this process's facts version would be mislabelled.

    The workers are this installation's, so the difference is planted on this side: the
    engine expects a version no worker reports.
    """
    original = Deriver.__init__

    def expecting_another(self: Deriver, *args: object, **kwargs: object) -> None:
        original(self, *args, **kwargs)  # type: ignore[arg-type]
        self._facts_version = "schema:0||a parser set no worker has"

    monkeypatch.setattr(Deriver, "__init__", expecting_another)
    serial = _index(tmp_path, tree, "serial", workers=1)
    refused = _index(tmp_path, tree, "refused", workers=4)

    assert pools_started == [False]
    assert _tables(refused) == _tables(serial)


def test_a_small_pass_never_starts_a_process(
    tree: Path, tmp_path: Path, pools_started: list[bool]
) -> None:
    database = _index(tmp_path, tree, "small", workers=4)
    assert pools_started == [True]
    pools_started.clear()

    repo = tree / "svc"
    edited = repo / "src" / "m3.py"
    original = edited.read_text()
    edited.write_text(original + "\n\ndef added():\n    return 3\n")
    try:
        settings = Settings.model_validate(
            {"storage": {"database": str(database)}, "repos": {"root": str(tree)}}
        )
        os.environ[WORKERS_ENV] = "4"
        workspace = open_workspace(settings=settings, require_root=False)
        try:
            from repoatlas.services.sync_service import sync

            report = sync(workspace, workspace.resolver().resolve(tree, command="sync"))
        finally:
            workspace.close()
            del os.environ[WORKERS_ENV]
    finally:
        edited.write_text(original)

    assert report.repositories[0].files_indexed == 1
    assert POOL_THRESHOLD > 1
    assert pools_started == []


# -- a sync killed while its workers run ---------------------------------------


def _descendants(pid: int) -> set[int]:
    """Every live process below ``pid``, from /proc."""
    children: dict[int, list[int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
        except OSError:
            continue
        if fields[0] != "Z":
            children.setdefault(int(fields[1]), []).append(int(entry.name))
    found: set[int] = set()
    frontier = [pid]
    while frontier:
        for child in children.get(frontier.pop(), []):
            if child not in found:
                found.add(child)
                frontier.append(child)
    return found


def _alive(pid: int) -> bool:
    try:
        state = (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state != "Z"


@pytest.mark.skipif(sys.platform != "linux", reason="reads the process tree from /proc")
def test_a_killed_sync_leaves_no_worker_and_the_next_sync_proceeds(
    tree: Path, tmp_path: Path
) -> None:
    """``SIGKILL`` runs no cleanup, so only the kernel can stop the workers it leaves.

    A worker that outlived its sync would sit idle forever. The writer lock is the
    reason it matters: a process still holding a descriptor to the lock file would hold
    the lock too, and every later sync would wait for it.
    """
    env = {
        **os.environ,
        WORKERS_ENV: "2",
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
    }
    command = [sys.executable, "-m", "repoatlas.cli.main", "init", str(tree)]
    sync = subprocess.Popen(
        command, env=env, cwd=REPO_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        workers: set[int] = set()
        deadline = time.monotonic() + 120
        # The resource tracker, the fork server and its two workers.
        while len(workers) < 4 and sync.poll() is None and time.monotonic() < deadline:
            workers = _descendants(sync.pid)
            time.sleep(0.01)
        assert len(workers) >= 4, "the sync finished before its workers could be observed"
        sync.send_signal(signal.SIGKILL)
        sync.wait()
    finally:
        if sync.poll() is None:
            sync.kill()
            sync.wait()

    deadline = time.monotonic() + 60
    while any(_alive(pid) for pid in workers) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not [pid for pid in workers if _alive(pid)], "a worker outlived the sync it served"

    finished = subprocess.run(
        [sys.executable, "-m", "repoatlas.cli.main", "sync"],
        env={**env, "REPOATLAS_SYNC_LOCK_TIMEOUT": "5"},
        cwd=tree,
        capture_output=True,
        text=True,
        check=False,
    )
    assert finished.returncode == 0, finished.stderr
