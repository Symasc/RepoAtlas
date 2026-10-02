"""Synchronization atomicity and writer serialization (ARCHITECTURE.md §9).

Two defects share one lifecycle, and this suite is written against the property
both of them broke:

> A successful sync moves RepoAtlas from one complete logical knowledge state to
> another complete logical knowledge state.

The first defect made the global link pass a *committed* delete followed by a
later insert, so any failure in between published a knowledge base with no
cross-repository graph at all — while ``status`` still said ``current``. The
second let two ``sync`` processes run that sequence at the same time, which
produced an ``IntegrityError`` on a bad day and, on a worse one, exit code 0 over
a silently doubled graph.

Both are tested here from the outside: what a *separate connection* can observe
after a failure, and what two *real processes* do to each other. A mock cannot
answer either question.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from repoatlas.config.settings import Settings
from repoatlas.errors import ExitCode
from repoatlas.models.repository import SyncStatus
from repoatlas.services.init_service import initialize
from repoatlas.services.status_service import status
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace
from repoatlas.storage.protocols import EdgeRecord
from repoatlas.storage.sqlite.stores import SqliteKnowledgeStore
from tests.conftest import fact_snapshot, git, reindex_from_scratch

pytestmark = pytest.mark.adversarial

GLOBAL_PARSER = "linker:global"


# -- fixture ecosystem -------------------------------------------------------


def _package_json(name: str, dependencies: dict[str, str] | None = None) -> str:
    payload: dict[str, object] = {"name": name, "version": "1.0.0"}
    if dependencies:
        payload["dependencies"] = dependencies
    return json.dumps(payload, indent=2) + "\n"


def _write_repo(root: Path, name: str, files: dict[str, str]) -> Path:
    path = root / name
    for relative, content in files.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    git(path, "init", "--quiet", "--initial-branch=main")
    git(path, "add", "-A")
    git(path, "commit", "--quiet", "-m", "initial")
    return path


@pytest.fixture
def link_root(tmp_path: Path) -> Path:
    """``svc`` depends on a package ``lib-a`` provides; ``lib-b`` provides another.

    Small on purpose. Every test here is about *when* a write becomes visible, and
    a large tree only makes the failure window harder to aim at.
    """
    root = tmp_path / "link-repos"
    _write_repo(
        root,
        "svc",
        {
            "package.json": _package_json("@acme/svc", {"@acme/lib-a": "^1.0.0"}),
            "src/index.js": 'import { helper } from "@acme/lib-a";\n\nhelper();\n',
        },
    )
    _write_repo(
        root,
        "lib-a",
        {
            "package.json": _package_json("@acme/lib-a"),
            "src/index.js": "export function helper() {\n  return 1;\n}\n",
        },
    )
    _write_repo(
        root,
        "lib-b",
        {
            "package.json": _package_json("@acme/lib-b"),
            "src/index.js": "export function other() {\n  return 2;\n}\n",
        },
    )
    return root


@pytest.fixture
def link_settings(tmp_path: Path) -> Settings:
    return Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / "link-state" / "atlas.db")},
        }
    )


@pytest.fixture
def linked(link_settings: Settings, link_root: Path, tmp_path: Path) -> Iterator[Workspace]:
    workspace = open_workspace(settings=link_settings, require_root=False)
    initialize(workspace, link_root, config_path=tmp_path / "link-config.yaml")
    yield workspace
    workspace.close()


# -- observation helpers -----------------------------------------------------


def _observe(source: Workspace | Settings, probe: Callable[[Workspace], object]) -> object:
    """Run ``probe`` on a *separate connection*, so only committed data is seen."""
    settings = source.settings if isinstance(source, Workspace) else source
    other = open_workspace(settings=settings, require_root=False)
    try:
        return probe(other)
    finally:
        other.close()


def global_edges(workspace: Workspace) -> set[tuple[str, str, str]]:
    rows = workspace.database.execute(
        "SELECT e.type AS t, src.key AS s, dst.key AS d FROM edges e "
        "JOIN entities src ON src.id = e.src_id "
        "JOIN entities dst ON dst.id = e.dst_id "
        "WHERE e.parser = ? ORDER BY s, d, t",
        (GLOBAL_PARSER,),
    ).fetchall()
    return {(row["t"], row["s"], row["d"]) for row in rows}


def repo_edges(workspace: Workspace) -> set[tuple[str, str]]:
    return {
        (src.removeprefix("repo:"), dst.removeprefix("repo:"))
        for kind, src, dst in global_edges(workspace)
        if kind == "DEPENDS_ON" and src.startswith("repo:") and dst.startswith("repo:")
    }


def global_edge_count(workspace: Workspace) -> int:
    return int(
        workspace.database.execute(
            "SELECT COUNT(*) AS n FROM edges WHERE parser = ?", (GLOBAL_PARSER,)
        ).fetchone()["n"]
    )


def _sync(workspace: Workspace, where: Path, *, full: bool = False):
    return sync(workspace, workspace.resolver().resolve(where), full=full)


def _touch(path: Path) -> None:
    """Force the file to be classified changed, so the run has work to do."""
    path.write_text(path.read_text(encoding="utf-8") + "\n// touched\n", encoding="utf-8")


def integrity(database: Path) -> tuple[str, int]:
    connection = sqlite3.connect(database)
    try:
        check = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        violations = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        return check, violations
    finally:
        connection.close()


# -- fault injection ---------------------------------------------------------
#
# Injected by monkeypatching the real storage calls the swap makes, so nothing
# resembling a debug hook ships in production code (§20 of the remediation).


class _Injected(RuntimeError):
    """A failure planted at a named point in the global-link sequence."""


def _fail_when(
    monkeypatch: pytest.MonkeyPatch,
    *,
    point: str,
    after: int = 0,
) -> None:
    """Plant a failure at one point of the prepare/swap sequence.

    ``after`` lets ``mid_insert`` write part of the replacement set before
    failing, which is the case a naive rollback would get wrong.
    """
    if point == "before_swap":
        original = SqliteKnowledgeStore.list_unresolved_refs

        def failing_prepare(self: SqliteKnowledgeStore, *args: Any, **kwargs: Any):
            original(self, *args, **kwargs)
            raise _Injected("failure while computing the replacement graph")

        monkeypatch.setattr(SqliteKnowledgeStore, "list_unresolved_refs", failing_prepare)
        return

    if point == "after_delete":
        original_add = SqliteKnowledgeStore.add_edges

        def failing_add(self: SqliteKnowledgeStore, edges: Sequence[EdgeRecord]) -> None:
            if edges and any(edge.parser == GLOBAL_PARSER for edge in edges):
                raise _Injected("failure after the old global edges were deleted")
            original_add(self, edges)

        monkeypatch.setattr(SqliteKnowledgeStore, "add_edges", failing_add)
        return

    if point == "mid_insert":
        original_add = SqliteKnowledgeStore.add_edges

        def partial_add(self: SqliteKnowledgeStore, edges: Sequence[EdgeRecord]) -> None:
            if edges and any(edge.parser == GLOBAL_PARSER for edge in edges):
                original_add(self, list(edges)[:after])
                raise _Injected("failure part way through inserting the replacement")
            original_add(self, edges)

        monkeypatch.setattr(SqliteKnowledgeStore, "add_edges", partial_add)
        return

    if point == "before_commit":
        original_gc = SqliteKnowledgeStore.gc_orphan_entities

        def failing_gc(self: SqliteKnowledgeStore) -> int:
            original_gc(self)
            raise _Injected("failure after the replacement was written, before commit")

        monkeypatch.setattr(SqliteKnowledgeStore, "gc_orphan_entities", failing_gc)
        return

    raise AssertionError(f"unknown injection point: {point}")


# ---------------------------------------------------------------------------
# 1. The global-link swap is atomic
# ---------------------------------------------------------------------------


def test_a_healthy_sync_builds_the_expected_global_graph(linked: Workspace) -> None:
    """The baseline every atomicity assertion below compares against."""
    assert repo_edges(linked) == {("svc", "lib-a")}
    assert global_edge_count(linked) == 2


@pytest.mark.parametrize(
    ("point", "after"),
    [
        ("before_swap", 0),
        ("after_delete", 0),
        ("mid_insert", 1),
        ("before_commit", 0),
    ],
)
def test_a_failure_anywhere_in_the_rebuild_leaves_the_previous_graph_intact(
    linked: Workspace,
    link_settings: Settings,
    link_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    point: str,
    after: int,
) -> None:
    """The defect, stated as a property.

    Whatever fails and wherever, another connection must see the *previous
    complete* graph. Never an empty one, never a half-written one.
    """
    before = _observe(linked, global_edges)
    assert before, "the fixture must have a graph to lose"

    _touch(link_root / "svc" / "src" / "index.js")
    _fail_when(monkeypatch, point=point, after=after)

    with pytest.raises(_Injected):
        _sync(linked, link_root)

    assert _observe(linked, global_edges) == before
    assert _observe(linked, repo_edges) == {("svc", "lib-a")}


def test_no_committed_state_ever_has_the_graph_deleted_and_not_rebuilt(
    linked: Workspace, link_settings: Settings, link_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reader watching every commit must never catch a zero-edge state.

    Sampled from a second connection at the one moment it could happen: between
    the delete and the write of the replacement.
    """
    seen: list[int] = []
    original_delete = SqliteKnowledgeStore.delete_edges_by_parser

    def sampling_delete(self: SqliteKnowledgeStore, parser: str, **kwargs: Any) -> int:
        removed = original_delete(self, parser, **kwargs)
        # A separate connection sees only committed rows. If the delete has been
        # committed on its own, this reads zero.
        seen.append(int(_observe(linked, global_edge_count)))  # type: ignore[arg-type]
        return removed

    monkeypatch.setattr(SqliteKnowledgeStore, "delete_edges_by_parser", sampling_delete)

    _touch(link_root / "svc" / "src" / "index.js")
    _sync(linked, link_root)

    assert seen, "the sample point was never reached"
    assert 0 not in seen, f"a reader observed a committed empty global graph: {seen}"


def test_a_reader_during_a_sync_sees_one_complete_graph_or_the_other(
    linked: Workspace, link_settings: Settings, link_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`locate` runs against whatever is committed. Both answers are complete."""
    complete = {("svc", "lib-a")}
    observed: list[set[tuple[str, str]]] = []
    original_delete = SqliteKnowledgeStore.delete_edges_by_parser

    def sampling_delete(self: SqliteKnowledgeStore, parser: str, **kwargs: Any) -> int:
        removed = original_delete(self, parser, **kwargs)
        observed.append(_observe(linked, repo_edges))  # type: ignore[arg-type]
        return removed

    monkeypatch.setattr(SqliteKnowledgeStore, "delete_edges_by_parser", sampling_delete)
    _touch(link_root / "svc" / "src" / "index.js")
    _sync(linked, link_root)

    assert observed
    for snapshot in observed:
        assert snapshot == complete


# ---------------------------------------------------------------------------
# 2. Status cannot claim a state that was never committed
# ---------------------------------------------------------------------------


def test_a_failed_global_link_does_not_leave_the_repositories_current(
    linked: Workspace, link_settings: Settings, link_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parsing finishing is not the sync succeeding (§9).

    The cross-repository graph is part of the V1 knowledge base, so a run whose
    link pass failed did not produce a current index and must not say it did.
    """
    _touch(link_root / "svc" / "src" / "index.js")
    _fail_when(monkeypatch, point="after_delete")

    with pytest.raises(_Injected):
        _sync(linked, link_root)

    def _states(workspace: Workspace) -> tuple[str, set[str]]:
        report = status(workspace, workspace.resolver().resolve(link_root))
        return report.index_state, {r.index_state for r in report.repositories}

    overall, per_repo = _observe(linked, _states)  # type: ignore[misc]
    assert overall != "current", f"status claimed {overall!r} after a failed sync"
    assert any(state == "interrupted" for state in per_repo), per_repo


def test_a_failed_sync_is_recorded_as_unfinished_not_as_ok(
    linked: Workspace, link_settings: Settings, link_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _touch(link_root / "svc" / "src" / "index.js")
    _fail_when(monkeypatch, point="after_delete")
    with pytest.raises(_Injected):
        _sync(linked, link_root)

    def _status_rows(workspace: Workspace) -> set[str]:
        return {str(r.last_sync_status) for r in workspace.repositories.list_all()}

    assert str(SyncStatus.INTERRUPTED) in _observe(linked, _status_rows)  # type: ignore[operator]


def test_the_retry_after_a_failed_sync_succeeds_and_restores_the_graph(
    linked: Workspace, link_settings: Settings, link_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failing safe is only half of it; the next run must also be able to finish."""
    _touch(link_root / "svc" / "src" / "index.js")
    _fail_when(monkeypatch, point="after_delete")
    with pytest.raises(_Injected):
        _sync(linked, link_root)

    monkeypatch.undo()
    report = _sync(linked, link_root)
    assert report.repositories
    assert repo_edges(linked) == {("svc", "lib-a")}
    assert all(repo.last_sync_status is SyncStatus.OK for repo in linked.repositories.list_all())


def test_a_failed_run_forces_a_rehash_on_the_next_one(
    linked: Workspace, link_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """State written by a run that did not finish may not match the tree (§9)."""
    _touch(link_root / "svc" / "src" / "index.js")
    _fail_when(monkeypatch, point="after_delete")
    with pytest.raises(_Injected):
        _sync(linked, link_root)

    monkeypatch.undo()
    report = _sync(linked, link_root)
    entries = {entry.repository: entry for entry in report.repositories}
    assert entries["svc"].full_rehash
    assert "did not complete" in (entries["svc"].rehash_reason or "")


# ---------------------------------------------------------------------------
# 3. One writer at a time — real processes
# ---------------------------------------------------------------------------


def _cli_env(state: Path, home: Path) -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "REPOATLAS_DB": str(state / "atlas.db"),
        "REPOATLAS_CONFIG": str(state / "config.yaml"),
        "XDG_DATA_HOME": str(state / "data"),
        "XDG_CONFIG_HOME": str(state / "config"),
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
    }


def _cli(args: Sequence[str], env: dict[str, str], cwd: Path | None = None):
    return subprocess.run(
        [sys.executable, "-m", "repoatlas.cli.main", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        check=False,
    )


def _spawn(args: Sequence[str], env: dict[str, str], cwd: Path | None = None):
    return subprocess.Popen(
        [sys.executable, "-m", "repoatlas.cli.main", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=cwd,
    )


@pytest.fixture
def cli_workspace(tmp_path: Path, link_root: Path) -> tuple[Path, Path, dict[str, str]]:
    """A CLI-usable state directory over the link fixture, already initialized."""
    state = tmp_path / "cli-state"
    state.mkdir()
    env = _cli_env(state, tmp_path / "cli-home")
    (tmp_path / "cli-home").mkdir()
    done = _cli(["init", str(link_root)], env)
    assert done.returncode == 0, done.stderr
    return link_root, state / "atlas.db", env


@pytest.mark.parametrize(
    "commands",
    [
        pytest.param(("sync --full", "sync --full"), id="sync+sync"),
        pytest.param(("init", "sync --full"), id="init+sync"),
        pytest.param(("init", "init"), id="init+init"),
    ],
)
def test_two_mutating_commands_never_interleave(
    cli_workspace: tuple[Path, Path, dict[str, str]], commands: tuple[str, str]
) -> None:
    """Two real processes, started together, against one state database.

    Whichever order they end up in, the committed result must equal what a single
    writer produces — and neither may die with a SQLite error.
    """
    root, database, env = cli_workspace
    expected = {("svc", "lib-a")}

    def _argv(command: str) -> list[str]:
        # `init` takes the root as an argument; `sync` takes it from the cwd.
        parts = command.split()
        return [*parts, str(root)] if parts[0] == "init" else parts

    processes = [_spawn(_argv(command), env, cwd=root) for command in commands]
    results = [(process, *process.communicate()) for process in processes]

    for process, _out, err in results:
        assert "Traceback" not in err, err
        assert "IntegrityError" not in err, err
        assert process.returncode in {0, ExitCode.SYNC_LOCKED}, err

    assert any(process.returncode == 0 for process, _o, _e in results)

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT src.key AS s, dst.key AS d FROM edges e "
            "JOIN entities src ON src.id = e.src_id "
            "JOIN entities dst ON dst.id = e.dst_id "
            "WHERE e.parser = ? AND e.type = 'DEPENDS_ON' "
            "AND src.key LIKE 'repo:%' AND dst.key LIKE 'repo:%'",
            (GLOBAL_PARSER,),
        ).fetchall()
    finally:
        connection.close()

    observed = [(r["s"].removeprefix("repo:"), r["d"].removeprefix("repo:")) for r in rows]
    assert sorted(observed) == sorted(expected), f"duplicated or lost edges: {observed}"

    check, violations = integrity(database)
    assert check == "ok"
    assert violations == 0


def test_the_refused_writer_reports_a_repoatlas_error_not_a_traceback(
    cli_workspace: tuple[Path, Path, dict[str, str]], tmp_path: Path
) -> None:
    """A held lock is an operational condition, so it gets a message, not a dump."""
    root, _database, env = cli_workspace
    blocked_env = {**env, "REPOATLAS_SYNC_LOCK_TIMEOUT": "0"}

    from repoatlas.sync.lock import writer_lock

    with writer_lock(Path(env["REPOATLAS_DB"]), timeout=0.0):
        result = _cli(["sync"], blocked_env, cwd=root)

    assert result.returncode == ExitCode.SYNC_LOCKED, (result.returncode, result.stderr)
    assert "Traceback" not in result.stderr
    assert "sqlite3" not in result.stderr
    message = result.stderr.lower()
    assert "synchronization is already in progress" in message, result.stderr
    assert "pid=" in message, "the message should name the holder"


def test_the_lock_is_released_when_a_writer_exits_normally(
    cli_workspace: tuple[Path, Path, dict[str, str]],
) -> None:
    root, _database, env = cli_workspace
    first = _cli(["sync"], env, cwd=root)
    assert first.returncode == 0, first.stderr
    second = _cli(["sync"], {**env, "REPOATLAS_SYNC_LOCK_TIMEOUT": "0"}, cwd=root)
    assert second.returncode == 0, second.stderr


def test_the_lock_is_released_when_a_writer_is_killed(
    cli_workspace: tuple[Path, Path, dict[str, str]], tmp_path: Path
) -> None:
    """A `kill -9` must not brick RepoAtlas. This is why the lock is not a row."""
    root, database, env = cli_workspace
    holder = _spawn(["sync", "--full"], env, cwd=root)
    time.sleep(0.35)
    holder.send_signal(signal.SIGKILL)
    holder.communicate()

    retry = _cli(["sync", "--full"], {**env, "REPOATLAS_SYNC_LOCK_TIMEOUT": "0"}, cwd=root)
    assert retry.returncode == 0, retry.stderr

    check, violations = integrity(database)
    assert check == "ok"
    assert violations == 0


def test_a_terminated_writer_does_not_strand_the_lock(
    cli_workspace: tuple[Path, Path, dict[str, str]],
) -> None:
    root, database, env = cli_workspace
    holder = _spawn(["sync", "--full"], env, cwd=root)
    time.sleep(0.35)
    holder.send_signal(signal.SIGTERM)
    holder.communicate()

    retry = _cli(["sync", "--full"], {**env, "REPOATLAS_SYNC_LOCK_TIMEOUT": "0"}, cwd=root)
    assert retry.returncode == 0, retry.stderr
    assert integrity(database) == ("ok", 0)


def test_a_reader_does_not_need_the_writer_lock(
    cli_workspace: tuple[Path, Path, dict[str, str]],
) -> None:
    """WAL gives readers a committed snapshot; they must never wait on a sync."""
    root, _database, env = cli_workspace
    from repoatlas.sync.lock import writer_lock

    reads = (["status"], ["repos"], ["locate", "helper"], ["locate", "helper", "--json"])
    with writer_lock(Path(env["REPOATLAS_DB"]), timeout=0.0):
        for command in reads:
            result = _cli(command, {**env, "REPOATLAS_SYNC_LOCK_TIMEOUT": "0"}, cwd=root)
            assert result.returncode == 0, (command, result.stderr)
            assert "database is locked" not in result.stderr


def test_the_second_writer_waits_when_a_timeout_allows_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bounded wait is the default; a contending run is not simply rejected.

    The holder lets go only when the second writer first polls, so a successful
    acquisition proves that writer found the lock taken and waited it out.
    """
    from types import SimpleNamespace

    from repoatlas.errors import SyncInProgressError
    from repoatlas.sync import lock as lock_module
    from repoatlas.sync.lock import writer_lock

    database = tmp_path / "wait-state" / "atlas.db"
    database.parent.mkdir(parents=True)
    database.touch()

    project = str(Path(__file__).resolve().parents[2])
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from repoatlas.sync.lock import writer_lock\n"
        f"with writer_lock(Path({str(database)!r}), timeout=0.0):\n"
        "    print('held', flush=True)\n"
        "    sys.stdin.readline()\n"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": project},
    )
    assert holder.stdin is not None and holder.stdout is not None
    release = holder.stdin
    assert holder.stdout.readline().strip() == "held"

    with pytest.raises(SyncInProgressError), writer_lock(database, timeout=0.0):
        pass

    polls: list[float] = []

    def release_on_first_poll(seconds: float) -> None:
        if not polls:
            release.write("release\n")
            release.flush()
        polls.append(seconds)
        time.sleep(seconds)

    monkeypatch.setattr(
        lock_module,
        "time",
        SimpleNamespace(monotonic=time.monotonic, sleep=release_on_first_poll),
    )
    with writer_lock(database, timeout=15.0):
        pass
    holder.communicate()
    assert polls, "the second writer did not actually wait"


# ---------------------------------------------------------------------------
# 4. The graph the swap produces
# ---------------------------------------------------------------------------


def test_repeated_global_linking_is_idempotent(linked: Workspace, link_root: Path) -> None:
    """Three runs, one graph. Atomicity must not come at the cost of drift."""
    snapshots = [fact_snapshot(linked)["edges"]]
    for _ in range(2):
        _sync(linked, link_root)
        snapshots.append(fact_snapshot(linked)["edges"])
    assert snapshots[0] == snapshots[1] == snapshots[2]
    assert global_edge_count(linked) == 2


def test_a_removed_dependency_removes_its_edge(linked: Workspace, link_root: Path) -> None:
    """Atomicity must not preserve stale edges forever."""
    assert repo_edges(linked) == {("svc", "lib-a")}
    (link_root / "svc" / "package.json").write_text(_package_json("@acme/svc"), encoding="utf-8")
    _sync(linked, link_root)
    assert repo_edges(linked) == set()


def test_a_changed_provider_replaces_the_edge(linked: Workspace, link_root: Path) -> None:
    """svc → lib-a becomes svc → lib-b in one commit: never both, never neither."""
    (link_root / "svc" / "package.json").write_text(
        _package_json("@acme/svc", {"@acme/lib-b": "^1.0.0"}), encoding="utf-8"
    )
    _sync(linked, link_root)
    assert repo_edges(linked) == {("svc", "lib-b")}


def test_an_ambiguous_provider_drops_the_edge_and_a_resolved_one_restores_it(
    linked: Workspace, link_root: Path
) -> None:
    """one provider → two providers → one provider, each transition atomic."""
    assert repo_edges(linked) == {("svc", "lib-a")}

    # lib-b starts claiming the same package identity: nothing may be chosen.
    (link_root / "lib-b" / "package.json").write_text(
        _package_json("@acme/lib-a"), encoding="utf-8"
    )
    _sync(linked, link_root)
    assert repo_edges(linked) == set()

    # lib-b stops claiming it: the single remaining provider wins again.
    (link_root / "lib-b" / "package.json").write_text(
        _package_json("@acme/lib-b"), encoding="utf-8"
    )
    _sync(linked, link_root)
    assert repo_edges(linked) == {("svc", "lib-a")}


def test_an_unresolved_claim_becomes_an_edge_when_its_provider_appears(
    linked: Workspace, link_root: Path
) -> None:
    """A standing claim is relinked by the global pass, inside the same swap."""
    (link_root / "svc" / "package.json").write_text(
        _package_json("@acme/svc", {"@acme/lib-c": "^1.0.0"}), encoding="utf-8"
    )
    _sync(linked, link_root)
    assert repo_edges(linked) == set()

    _write_repo(
        link_root,
        "lib-c",
        {
            "package.json": _package_json("@acme/lib-c"),
            "src/index.js": "export function third() {\n  return 3;\n}\n",
        },
    )
    initialize(linked, link_root, config_path=link_root.parent / "relink-config.yaml")
    assert repo_edges(linked) == {("svc", "lib-c")}

    import shutil

    shutil.rmtree(link_root / "lib-c")
    initialize(linked, link_root, config_path=link_root.parent / "relink-config.yaml")
    assert repo_edges(linked) == set()


# ---------------------------------------------------------------------------
# 5. The database survives
# ---------------------------------------------------------------------------


def test_sqlite_integrity_survives_an_injected_failure(
    linked: Workspace, link_settings: Settings, link_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = link_settings.storage.database
    assert database is not None
    _touch(link_root / "svc" / "src" / "index.js")
    _fail_when(monkeypatch, point="mid_insert", after=1)
    with pytest.raises(_Injected):
        _sync(linked, link_root)
    assert integrity(database) == ("ok", 0)


def test_a_sync_after_a_failed_one_equals_a_clean_full_reindex(
    linked: Workspace,
    link_settings: Settings,
    link_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invariant 2 of §9, extended across a failure: recovery is not a special
    state, it is the same state a fresh index would have produced."""
    (link_root / "svc" / "package.json").write_text(
        _package_json("@acme/svc", {"@acme/lib-b": "^1.0.0"}), encoding="utf-8"
    )
    _fail_when(monkeypatch, point="after_delete")
    with pytest.raises(_Injected):
        _sync(linked, link_root)
    monkeypatch.undo()
    _sync(linked, link_root)

    fresh = reindex_from_scratch(link_settings, link_root, tmp_path, tag="after-failure")
    assert fact_snapshot(linked) == fresh


def _edit_a_file(root: Path) -> None:
    _touch(root / "svc" / "src" / "index.js")


def _remove_the_dependency(root: Path) -> None:
    (root / "svc" / "package.json").write_text(_package_json("@acme/svc"), encoding="utf-8")


def _change_the_provider(root: Path) -> None:
    (root / "svc" / "package.json").write_text(
        _package_json("@acme/svc", {"@acme/lib-b": "^1.0.0"}), encoding="utf-8"
    )


def _make_it_ambiguous(root: Path) -> None:
    (root / "lib-b" / "package.json").write_text(_package_json("@acme/lib-a"), encoding="utf-8")


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(_edit_a_file, id="normal-change"),
        pytest.param(_remove_the_dependency, id="dependency-removal"),
        pytest.param(_change_the_provider, id="provider-change"),
        pytest.param(_make_it_ambiguous, id="ambiguity-change"),
    ],
)
def test_an_incremental_sync_equals_a_clean_full_reindex(
    linked: Workspace,
    link_settings: Settings,
    link_root: Path,
    tmp_path: Path,
    mutate: Callable[[Path], None],
) -> None:
    """Invariant 2 of §9, across every transition the atomic swap now handles.

    Atomicity that quietly preserved a stale edge would pass every rollback test
    above and still be wrong; this is the check that the swap replaces rather than
    merges.
    """
    mutate(link_root)
    _sync(linked, link_root)
    fresh = reindex_from_scratch(link_settings, link_root, tmp_path, tag="equivalence")
    assert fact_snapshot(linked) == fresh


def test_a_nested_transaction_joins_the_outer_one_instead_of_committing(
    linked: Workspace, link_settings: Settings
) -> None:
    """The mechanism the atomic swap is built on, asserted directly.

    If an inner ``with`` committed on its own, the swap's delete would become
    visible before its insert — which is the entire defect (§9).
    """
    database = linked.database
    row = database.execute("SELECT id FROM entities LIMIT 1").fetchone()
    assert row is not None

    def edge_total(workspace: Workspace) -> int:
        return int(workspace.database.execute("SELECT COUNT(*) AS n FROM edges").fetchone()["n"])

    before = _observe(linked, edge_total)
    with pytest.raises(_Injected), database.transaction():
        assert database.in_transaction
        linked.knowledge.delete_edges_by_parser(GLOBAL_PARSER)
        # Committed by an inner block, this delete would already be visible.
        assert _observe(linked, edge_total) == before
        raise _Injected("rolled back")

    assert not database.in_transaction
    assert _observe(linked, edge_total) == before
