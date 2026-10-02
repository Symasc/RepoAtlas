"""Concurrent first open is a wait, not a crash (V1_SPEC §4, ARCHITECTURE.md §3).

The release review found that several processes opening one brand-new index at
the same moment sometimes produced::

    sqlite3.OperationalError: database is locked

from ``PRAGMA journal_mode = WAL``, as a Rich traceback panel at exit 1. Two
terminals, or a shell and an editor plugin, running a first command together is
not an exotic state, and "database is locked" is not something an operator can
act on.

**The mechanism is not the obvious one, and the obvious fix does not work.** The
Python connection is in autocommit when that pragma runs — measured, and pinned
by :func:`test_the_wal_transition_does_not_run_inside_a_transaction`. The
read-to-write promotion is *inside* SQLite: changing the journal mode rewrites
the file header, so the pragma takes its own read transaction and promotes it,
and SQLite never runs a busy handler for a promotion because a process that
blocks while holding a read lock is how two processes deadlock. The configured
ten-second timeout is therefore never consulted, and the pragma fails after
about a millisecond.

So the file is organized around the three things that had to be true at once:

* the transition is *asked about* before it is *attempted*, so an ordinary open
  of a healthy index never joins the race (§ "already WAL");
* the attempt itself is retried under a deadline, because a retry is the only
  thing that helps when the busy handler will not run (§ "ordinary race");
* nothing but genuine contention is retried or renamed — a read-only file, a
  missing directory and a corrupt page keep their own instant, accurate
  diagnostics (§ "taxonomy").

Every concurrency assertion here uses real processes. Threads share a
``sqlite3`` connection cache and a GIL and would not reproduce the defect.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from repoatlas.errors import (
    ExitCode,
    IndexForeignApplicationError,
    IndexInaccessibleError,
    IndexNotRecognizedError,
    IndexSchemaError,
    MigrationContentionError,
    MigrationFailedError,
    RepoAtlasError,
)
from repoatlas.storage.sqlite.database import (
    BUSY_TIMEOUT_MS,
    SCHEMA_VERSION,
    WAL,
    _ensure_wal,
    open_database,
)
from repoatlas.storage.sqlite.errors import classify, is_contention
from repoatlas.storage.sqlite.migrator import (
    APPLICATION_ID,
    Migration,
    apply_migrations,
    load_migrations,
)
from tests.sqlite_codes import with_code

pytestmark = pytest.mark.adversarial

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI = Path(sys.executable).parent / "repoatlas"

#: What a Rich traceback panel and a bare Python traceback both contain.
TRACEBACK_MARKERS = ("Traceback (most recent call last)", "sqlite3.", "OperationalError")


# == fixtures: every shape a database can be in when it is first opened ======


def build(path: Path, version: int, *, wal: bool = True, mark: bool = True) -> None:
    """A genuine database at ``version``, built by running the real migrations.

    Not ``PRAGMA user_version = N`` stamped onto the current schema: that fixture
    already has the later columns and would let an upgrade test pass vacuously.
    ``mark=False`` leaves ``application_id`` at 0, which is what every index
    written before RepoAtlas claimed the field looks like.
    """
    connection = sqlite3.connect(path, isolation_level="DEFERRED")
    try:
        if wal:
            connection.execute(f"PRAGMA journal_mode = {WAL}")
        for migration in load_migrations():
            if migration.version > version:
                break
            connection.execute("BEGIN IMMEDIATE")
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {migration.version}")
            connection.commit()
        if mark:
            connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
            connection.commit()
    finally:
        connection.close()


def absent(path: Path) -> None:
    """The common first run: nothing at the index path at all."""


def zero_byte(path: Path) -> None:
    path.write_bytes(b"")


def empty_sqlite(path: Path) -> None:
    sqlite3.connect(path).close()


def foreign(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE payroll(id INTEGER PRIMARY KEY, name TEXT)")
    connection.execute("INSERT INTO payroll(name) VALUES ('ada')")
    connection.commit()
    connection.close()


def foreign_application_id(path: Path) -> None:
    foreign(path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA application_id = 305419896")
    connection.commit()
    connection.close()


def future_schema(path: Path) -> None:
    """Written by a newer RepoAtlas: our marker, a version this build cannot read.

    Built in rollback-journal mode so the fixture brings no ``-wal``/``-shm`` of
    its own and their *absence* afterwards is evidence rather than an accident.
    :func:`future_schema_wal` is the same database in the mode a newer RepoAtlas
    would really have left it in.
    """
    build(path, SCHEMA_VERSION, wal=False)
    connection = sqlite3.connect(path)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    connection.commit()
    connection.close()


def future_schema_wal(path: Path) -> None:
    build(path, SCHEMA_VERSION)
    connection = sqlite3.connect(path)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    connection.commit()
    connection.close()


def healthy_wal(path: Path) -> None:
    build(path, SCHEMA_VERSION)


def healthy_delete(path: Path) -> None:
    """A marked index still in rollback-journal mode — an older RepoAtlas's."""
    build(path, SCHEMA_VERSION, wal=False)


def one_behind(path: Path) -> None:
    build(path, SCHEMA_VERSION - 1)


def legacy(path: Path) -> None:
    """Pre-marker, current schema: recognized by shape, marked on the next write."""
    build(path, SCHEMA_VERSION, wal=False, mark=False)


def legacy_one_behind(path: Path) -> None:
    build(path, SCHEMA_VERSION - 1, wal=False, mark=False)


Shape = Callable[[Path], None]

#: Shapes RepoAtlas may adopt or operate. Every opener must succeed.
USABLE: tuple[tuple[str, Shape], ...] = (
    ("absent", absent),
    ("zero-byte", zero_byte),
    ("empty-sqlite", empty_sqlite),
    ("healthy-wal", healthy_wal),
    ("healthy-delete", healthy_delete),
    ("one-behind", one_behind),
    ("legacy", legacy),
    ("legacy-one-behind", legacy_one_behind),
)

#: Shapes RepoAtlas must refuse. Every opener must refuse, identically.
REFUSED: tuple[tuple[str, Shape, type[RepoAtlasError]], ...] = (
    ("foreign", foreign, IndexNotRecognizedError),
    ("foreign-application-id", foreign_application_id, IndexForeignApplicationError),
    ("future-schema", future_schema, IndexSchemaError),
)


# == running real, simultaneous openers ======================================

#: Opens the database and reports everything an assertion here might need.
#: Written as a script rather than imported so each opener is a real process
#: with its own SQLite library state.
_OPENER = """
import json, os, pathlib, sys, time, traceback
from repoatlas.storage.sqlite.database import open_database
import repoatlas.errors as errors

start_at = float(sys.argv[2])
while time.time() < start_at:
    pass

out = {"pid": os.getpid()}
began = time.perf_counter()
try:
    database = open_database(pathlib.Path(sys.argv[1]))
    execute = database.connection.execute
    out.update(
        ok=True,
        journal_mode=execute("PRAGMA journal_mode").fetchone()[0],
        user_version=execute("PRAGMA user_version").fetchone()[0],
        application_id=execute("PRAGMA application_id").fetchone()[0],
        integrity=execute("PRAGMA integrity_check").fetchone()[0],
    )
    database.close()
except BaseException as exc:
    out.update(
        ok=False,
        error=type(exc).__name__,
        product=isinstance(exc, errors.RepoAtlasError),
        exit_code=getattr(exc, "exit_code", None),
        errorname=getattr(exc, "sqlite_errorname", None),
        traceback=traceback.format_exc(),
    )
out["elapsed"] = time.perf_counter() - began
print(json.dumps(out))
"""


def open_together(path: Path, count: int, *, lead: float = 0.35) -> list[dict[str, Any]]:
    """Open ``path`` from ``count`` processes released by one wall-clock deadline.

    The deadline is what makes this a test rather than a lottery. Started without
    it, the processes reach SQLite in interpreter-start order and spread over
    tens of milliseconds; the window this file is about is roughly one. With it,
    the unfixed code failed 86 of 240 opens.
    """
    environment = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    release = time.time() + lead
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", _OPENER, str(path), str(release)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        for _ in range(count)
    ]
    results: list[dict[str, Any]] = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=180)
        try:
            results.append(json.loads(stdout.strip().splitlines()[-1]))
        except (ValueError, IndexError):  # pragma: no cover - a worker that died
            results.append({"ok": False, "error": "no output", "traceback": stdout + stderr})
    return results


def state(path: Path) -> dict[str, str]:
    """Every byte of the database *and of any sidecar*, keyed by file name.

    Hashing ``index.db`` alone is not enough, and the gap is exactly the kind a
    refusal test must not have: in WAL mode a write lands in ``index.db-wal`` and
    leaves the main file byte-identical, so a database RepoAtlas wrote to would
    hash the same as one it never touched. The ``-wal`` and ``-shm`` files are
    part of the database, so they are part of the comparison — and their
    appearance is itself evidence of a write, since a refused database should
    acquire neither.

    The index fixture gives each test a directory of its own, so this is the
    database and nothing else.
    """
    return {
        entry.name: hashlib.sha256(entry.read_bytes()).hexdigest()
        for entry in sorted(path.parent.iterdir())
    }


class Holder:
    """Holds SQLite's write lock for as long as the ``with`` block lasts."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection: sqlite3.Connection | None = None

    def __enter__(self) -> Holder:
        self.connection = sqlite3.connect(self.path, isolation_level="DEFERRED")
        self.connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        self.connection.execute("BEGIN IMMEDIATE")
        # A real write, so the RESERVED lock is genuinely held rather than
        # promised: `BEGIN IMMEDIATE` alone is enough, and a statement makes the
        # intent legible in a failure.
        self.connection.execute("CREATE TABLE IF NOT EXISTS held_by_the_test(x)")
        return self

    def release(self) -> None:
        if self.connection is not None:
            self.connection.rollback()
            self.connection.close()
            self.connection = None

    def __exit__(self, *exc: object) -> None:
        self.release()


#: Takes SQLite's write lock, says so, and releases it by exiting.
_FOREIGN_HOLDER = """
import sqlite3, sys, time
connection = sqlite3.connect(sys.argv[1], isolation_level="DEFERRED")
connection.execute("PRAGMA busy_timeout = 10000")
connection.execute("BEGIN IMMEDIATE")
connection.execute("CREATE TABLE IF NOT EXISTS held_by_the_test(x)")
print("held", flush=True)
time.sleep(float(sys.argv[2]))
connection.rollback()
"""


@contextmanager
def foreign_holder(path: Path, *, seconds: float | None = None) -> Iterator[None]:
    """Hold SQLite's write lock from another process — for ``seconds``, or for the block.

    Another *process*, and not only because a ``sqlite3`` connection belongs to
    the thread that created it. On Linux, closing any descriptor on a file drops
    every POSIX lock the whole process holds on it, so SQLite keeps a per-inode
    list of descriptors it must not close yet. A competing connection inside the
    test process therefore makes RepoAtlas's own — correctly closed — descriptor
    linger until the competitor goes away, and a descriptor-counting test reads
    that as a leak. Measured: one extra descriptor with a same-process holder,
    **zero** with this one, and the extra one disappears the moment that holder
    closes. :class:`Holder` stays for the tests that assert outcomes rather than
    resources.
    """
    process = subprocess.Popen(
        [sys.executable, "-c", _FOREIGN_HOLDER, str(path), str(seconds or 600)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "held"
    try:
        yield
    finally:
        if seconds is None:
            process.kill()
        process.wait(timeout=(seconds or 0) + 60)


@pytest.fixture
def index(tmp_path: Path) -> Path:
    """A directory of its own, so `-wal`/`-shm` siblings are unambiguous."""
    directory = tmp_path / "state"
    directory.mkdir()
    return directory / "index.db"


@pytest.fixture
def short_busy_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give up on a held lock after half a second instead of ten, in this process.

    For tests whose writer never lets go: they assert what the open ends in, and
    waiting out the real deadline to get there proves nothing more.
    """
    monkeypatch.setattr("repoatlas.storage.sqlite.database.BUSY_TIMEOUT_MS", 500)


# == 1. the review's reproduction ============================================


@pytest.mark.parametrize("count", [6, 12])
def test_simultaneous_first_opens_of_a_new_database_all_succeed(index: Path, count: int) -> None:
    """The finding, in the form it was reported.

    Before the fix this failed on most runs at six openers: the winner of the
    race held SQLite's write lock through its migrations while every loser
    executed the WAL pragma and was refused immediately.
    """
    results = open_together(index, count)

    failures = [r for r in results if not r["ok"]]
    assert failures == [], failures
    assert {r["journal_mode"] for r in results} == {WAL}
    assert {r["user_version"] for r in results} == {SCHEMA_VERSION}
    assert {r["application_id"] for r in results} == {APPLICATION_ID}
    assert {r["integrity"] for r in results} == {"ok"}


def test_a_lock_released_before_the_deadline_is_waited_out(index: Path) -> None:
    """§10: a millisecond-scale race is a wait, not an error.

    The deterministic half of the reproduction, and the one that isolates the
    retry from everything else. Reading the mode before writing it removes *most*
    of the race on its own — the winner now holds the write lock for the duration
    of a header write rather than of a migration — which makes a purely
    statistical test a poor guard for the retry: it would pass most runs with the
    retry deleted.

    So the lock is held deliberately and released well inside the deadline. With
    the retry, the opener waits and succeeds. Without it, the first ``SQLITE_BUSY``
    is fatal and this fails every time.
    """
    healthy_delete(index)
    with foreign_holder(index, seconds=1.0):
        database = open_database(index)
    try:
        assert database.connection.execute("PRAGMA journal_mode").fetchone()[0] == WAL
    finally:
        database.close()


def test_no_opener_ever_reports_a_raw_sqlite_error(index: Path) -> None:
    """The presentation half, stated separately from reachability.

    A fix that merely made the race rarer would still fail here eventually; what
    this pins is that *whatever* an opener does, it does not hand the user a
    ``sqlite3`` exception. Every failure is a product state with an exit code.
    """
    results = open_together(index, 12)
    for result in results:
        assert result["ok"] or result["product"], result
        assert "sqlite3.OperationalError" not in result.get("traceback", "")


# == 2. transaction state: the regression guard ==============================


def traced_open(path: Path) -> list[tuple[str, bool]]:
    """Open the database, recording each statement and the transaction state.

    Instruments the real path by handing ``sqlite3.connect`` a connection
    subclass — ``open_database`` is not modified and does not know.
    """
    log: list[tuple[str, bool]] = []
    real_connect = sqlite3.connect

    class Traced(sqlite3.Connection):
        def execute(self, sql: str, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
            log.append((" ".join(sql.split())[:48], self.in_transaction))
            return super().execute(sql, *args, **kwargs)

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        kwargs["factory"] = Traced
        return real_connect(*args, **kwargs)

    sqlite3.connect = connect  # type: ignore[assignment]
    try:
        open_database(path).close()
    finally:
        sqlite3.connect = real_connect  # type: ignore[assignment]
    return log


def test_the_wal_transition_does_not_run_inside_a_transaction(index: Path) -> None:
    """§ the mechanism: the promotion is SQLite's, not Python's.

    The tempting reading of the defect is that the ownership snapshot's read
    transaction was still open, and that committing it earlier would fix
    everything. It was not, and it would not. This pins the state so that a
    future change which *does* leave a transaction open around the pragma — a
    strictly worse version of the same bug, since then not even a retry could
    succeed — fails here rather than in production.
    """
    log = traced_open(index)
    transitions = [(sql, in_tx) for sql, in_tx in log if sql == f"PRAGMA journal_mode = {WAL}"]
    assert transitions, log
    assert all(not in_tx for _, in_tx in transitions), transitions


def test_ownership_is_proven_before_the_journal_mode_is_touched(index: Path) -> None:
    """Pass 11's ordering, restated against the statement log.

    ``PRAGMA journal_mode`` writes the file header, so it may not run until
    RepoAtlas has proven it owns the file. A contention fix that hoisted the
    transition earlier — the shortest way to win the race — would reintroduce
    the foreign-database adoption defect, and it fails here first.
    """
    statements = [sql for sql, _ in traced_open(index)]
    ownership = statements.index("PRAGMA application_id")
    transition = statements.index(f"PRAGMA journal_mode = {WAL}")
    assert ownership < transition, statements


def test_an_ordinary_open_of_a_healthy_index_never_writes_the_header(index: Path) -> None:
    """§ already WAL: the transition is asked about, not attempted.

    The old code executed the write form on every open of every database,
    putting each ordinary ``locate`` into a header-write race it had no reason
    to enter. Now the mode is read, and a database already in WAL stops there.
    """
    healthy_wal(index)
    open_database(index).close()  # settle the `schema_version` stamp

    statements = [sql for sql, _ in traced_open(index)]
    assert "PRAGMA journal_mode" in statements
    assert f"PRAGMA journal_mode = {WAL}" not in statements, statements


def test_a_healthy_index_opens_while_another_process_writes(
    index: Path, short_busy_timeout: None
) -> None:
    """The consequence of the previous test, observed rather than inspected.

    A reader must not queue behind a writer merely to open the database
    (ARCHITECTURE.md §9). The old header write did exactly that. The writer here
    never lets go, so an open that needed its lock would end in
    :class:`MigrationContentionError` instead of succeeding.
    """
    healthy_wal(index)
    open_database(index).close()

    with Holder(index):
        open_database(index).close()


# == 3. the shape matrix, opened concurrently ================================


@pytest.mark.parametrize("name,prepare", USABLE, ids=[n for n, _ in USABLE])
def test_every_usable_shape_survives_simultaneous_openers(
    index: Path, name: str, prepare: Shape
) -> None:
    """§ zero-byte, absent, empty, legacy, behind: all converge on one index.

    Each of these reaches initialization by a different route — adoption, a
    migration, the marker write — and each one used to be able to lose the WAL
    race. The assertion is convergence, not merely absence of failure: one
    database, in WAL, at the current version, marked, intact.
    """
    prepare(index)
    results = open_together(index, 6)

    assert [r for r in results if not r["ok"]] == [], results
    assert {r["journal_mode"] for r in results} == {WAL}
    assert {r["user_version"] for r in results} == {SCHEMA_VERSION}
    assert {r["application_id"] for r in results} == {APPLICATION_ID}
    assert {r["integrity"] for r in results} == {"ok"}


@pytest.mark.parametrize("name,prepare,expected", REFUSED, ids=[n for n, _, _ in REFUSED])
def test_every_refused_shape_is_refused_by_every_opener(
    index: Path, name: str, prepare: Shape, expected: type[RepoAtlasError]
) -> None:
    """Pass 11's ownership rule, under load.

    Concurrency is exactly where "prove ownership first" could quietly rot: a
    process that retried its way past a lock could adopt a database another one
    had just refused. So the file is hashed before and after, and the absence of
    a ``-wal``/``-shm`` sibling is asserted too — creating one would be a write
    to a database RepoAtlas has just said it will not touch.
    """
    prepare(index)
    before = state(index)
    assert list(before) == [index.name], "the fixture itself brought a sidecar"

    results = open_together(index, 12)

    assert [r for r in results if r["ok"]] == [], results
    assert {r["error"] for r in results} == {expected.__name__}
    assert {r["exit_code"] for r in results} == {ExitCode.NOT_INITIALIZED}
    assert state(index) == before


def test_a_refused_wal_database_gains_nothing_in_its_sidecars(index: Path) -> None:
    """The same refusal against a database already in WAL, where a write hides.

    A newer RepoAtlas leaves its index in WAL, so this is the realistic shape of
    the future-schema case — and the one where checking only ``index.db`` would
    miss a write, because in WAL mode a write goes into ``index.db-wal``.
    """
    future_schema_wal(index)
    before = state(index)[index.name]

    results = open_together(index, 12)

    assert [r for r in results if r["ok"]] == [], results
    assert {r["error"] for r in results} == {IndexSchemaError.__name__}
    assert state(index)[index.name] == before
    # Merely *reading* a WAL database creates an empty `-wal` and `-shm`: the
    # shared-memory index is how WAL readers find the latest committed frames, so
    # their existence is not evidence of a write. Their contents are. A committed
    # change appears either as frames in the log or, after a checkpoint, in the
    # main file — and the assertion above already covers the second.
    log = index.with_name(index.name + "-wal")
    assert not log.exists() or log.stat().st_size == 0, "the refusal wrote WAL frames"


# == 4. sustained contention =================================================


@pytest.mark.parametrize("name,prepare", USABLE, ids=[n for n, _ in USABLE])
def test_a_lock_held_past_the_timeout_is_a_diagnostic_not_a_traceback(
    index: Path, name: str, prepare: Shape, short_busy_timeout: None
) -> None:
    """§ persistent contention: bounded wait, then a product error.

    Every shape, because the phases that can block differ between them — the
    journal transition, a migration, the identity marker, the schema stamp — and
    each was its own way to reach the terminal as ``sqlite3.OperationalError``.
    """
    prepare(index)
    with Holder(index), pytest.raises(MigrationContentionError) as raised:
        open_database(index)

    assert raised.value.exit_code == ExitCode.RUNTIME
    assert isinstance(raised.value.__cause__, sqlite3.Error)


def test_the_database_is_usable_again_once_the_lock_is_released(
    index: Path, short_busy_timeout: None
) -> None:
    """Contention leaves nothing behind. It is a moment, not a state."""
    holder = Holder(index)
    holder.__enter__()
    with pytest.raises(MigrationContentionError):
        open_database(index)
    holder.release()

    database = open_database(index)
    try:
        assert database.connection.execute("PRAGMA journal_mode").fetchone()[0] == WAL
        assert database.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        database.close()


def test_contention_is_never_reported_as_an_unusable_index(index: Path) -> None:
    """The direction that costs the user their data if it is got wrong.

    Exit 3 means "your index is unusable", and the documented remedy is to move
    it aside. A transient lock reported that way would send an operator to
    delete a database that is perfectly healthy and merely busy.
    """
    busy = with_code(sqlite3.OperationalError("database is locked"), "SQLITE_BUSY")
    assert classify(busy, Path("/nonexistent")) is None
    assert MigrationContentionError("x").exit_code != ExitCode.NOT_INITIALIZED


# == 5. taxonomy: what must not be mistaken for contention ===================


@pytest.mark.parametrize(
    "name,contention",
    [
        ("SQLITE_BUSY", True),
        ("SQLITE_LOCKED", True),
        ("SQLITE_READONLY", False),
        ("SQLITE_CANTOPEN", False),
        ("SQLITE_IOERR", False),
        ("SQLITE_FULL", False),
        ("SQLITE_CORRUPT", False),
        ("SQLITE_NOTADB", False),
        ("SQLITE_PERM", False),
        ("SQLITE_ERROR", False),
    ],
)
def test_only_lock_codes_are_retriable(name: str, contention: bool) -> None:
    """Codes, not prose. ``except OperationalError: retry`` would take all ten."""
    error = with_code(sqlite3.OperationalError("synthetic"), name)
    assert is_contention(error) is contention


@pytest.mark.parametrize(
    "name,code,contention",
    [
        ("SQLITE_BUSY_SNAPSHOT", 517, True),
        ("SQLITE_READONLY_DIRECTORY", 1544, False),
        ("SQLITE_IOERR_WRITE", 778, False),
    ],
)
def test_extended_codes_reduce_to_their_primary(name: str, code: int, contention: bool) -> None:
    """Enumerating extended codes would be a list that grows with SQLite."""
    error = with_code(sqlite3.OperationalError("synthetic"), name, code)
    assert is_contention(error) is contention


def test_a_migration_failure_that_mentions_a_lock_is_not_contention(index: Path) -> None:
    """The migrator classifies on codes too, and this is why it has to.

    A statement that fails because an object is missing reports ``SQLITE_ERROR``
    and a message naming that object. Deciding contention by looking for
    ``"locked"`` in the message therefore turns a broken migration into "another
    process is busy, try again" — advice that never comes true, for a defect that
    is never reported. The name below is the whole test.
    """
    build(index, SCHEMA_VERSION)
    connection = sqlite3.connect(index, isolation_level="DEFERRED")
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    broken = Migration(
        version=SCHEMA_VERSION + 1,
        statements=("SELECT 1 FROM locked_and_busy_table;",),
    )
    try:
        with pytest.raises(MigrationFailedError):
            apply_migrations(connection, [broken])
    finally:
        connection.close()


def test_a_read_only_index_fails_at_once_rather_than_waiting(index: Path) -> None:
    """§ read-only: nothing improves by waiting ten seconds to say the same thing.

    This is the test a lazy fix fails. Retrying every ``OperationalError`` turns
    an instant, correct "cannot write to the index" into a ten-second hang that
    ends in "another process is initializing the database" — which is false, and
    which points the reader at a process that does not exist.
    """
    healthy_delete(index)
    index.chmod(0o444)
    index.parent.chmod(0o555)
    try:
        with pytest.raises(IndexInaccessibleError):
            open_database(index)
    finally:
        index.parent.chmod(0o755)
        index.chmod(0o644)


def test_an_unreachable_index_path_fails_at_once(index: Path) -> None:
    """The same principle for a path fault rather than a permission one."""
    unreachable = index.parent / "missing" / "index.db"
    index.parent.joinpath("missing").mkdir()
    index.parent.joinpath("missing").chmod(0o000)
    try:
        with pytest.raises(IndexInaccessibleError):
            open_database(unreachable)
    finally:
        index.parent.joinpath("missing").chmod(0o755)


# == 6. the journal mode actually reached ====================================


class _StubConnection:
    """Answers the two journal-mode questions and nothing else."""

    def __init__(self, current: str, reached: str) -> None:
        self.current = current
        self.reached = reached

    def execute(self, sql: str) -> Any:
        if sql == "PRAGMA journal_mode":
            return _StubCursor((self.current,))
        assert sql == f"PRAGMA journal_mode = {WAL}"
        self.current = self.reached
        return _StubCursor((self.reached,))


class _StubCursor:
    def __init__(self, row: tuple[str]) -> None:
        self.row = row

    def fetchone(self) -> tuple[str]:
        return self.row


def test_a_transition_that_silently_did_not_happen_is_refused(index: Path) -> None:
    """§ WAL unsupported: executing the pragma is not the same as it working.

    ``PRAGMA journal_mode = WAL`` reports the mode it *ended in*. WAL needs
    shared memory, and a filesystem that cannot provide it leaves the database in
    its previous mode and raises nothing at all. Everything above storage assumes
    WAL — concurrent readers during a sync, above all — so continuing in
    rollback-journal mode would be operating a configuration the architecture
    does not describe. It is refused, and not as contention: no amount of waiting
    makes a filesystem grow shared memory.
    """
    stub = _StubConnection(current="delete", reached="delete")
    with pytest.raises(IndexInaccessibleError) as raised:
        _ensure_wal(stub, index)  # type: ignore[arg-type]
    assert "WAL" in raised.value.message
    assert "'delete'" in (raised.value.detail or "")
    assert raised.value.exit_code == ExitCode.NOT_INITIALIZED


def test_a_transition_that_did_happen_is_accepted(index: Path) -> None:
    """The control: the same code path must not refuse a successful transition."""
    _ensure_wal(_StubConnection(current="delete", reached=WAL), index)  # type: ignore[arg-type]


def test_the_journal_mode_is_wal_after_every_concurrent_campaign(index: Path) -> None:
    """§45: the durable state, asserted from a fresh connection afterwards."""
    healthy_delete(index)
    open_together(index, 8)

    connection = sqlite3.connect(index)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == WAL
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()


# == 8. migration concurrency ================================================


def test_concurrent_openers_of_an_old_database_migrate_it_exactly_once(index: Path) -> None:
    """§22: one logical migration, and the losers observe the finished result.

    The version is re-read inside the write transaction, so a process that waits
    out the lock finds the work already done rather than repeating it. What this
    adds to the migrator's own tests is the journal transition sitting in front
    of the migration, which is where the losers used to die.
    """
    one_behind(index)
    results = open_together(index, 8)

    assert [r for r in results if not r["ok"]] == [], results
    assert {r["user_version"] for r in results} == {SCHEMA_VERSION}

    connection = sqlite3.connect(index)
    try:
        # Applied once: the index migration 005 adds exists exactly one time.
        count = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'idx_repositories_slug'"
        ).fetchone()[0]
        assert count == 1
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_concurrent_openers_of_a_legacy_database_claim_it_exactly_once(index: Path) -> None:
    """§24: pre-marker index, adopted under contention without duplicate claims."""
    legacy(index)
    results = open_together(index, 8)

    assert [r for r in results if not r["ok"]] == [], results
    assert {r["application_id"] for r in results} == {APPLICATION_ID}
    assert {r["user_version"] for r in results} == {SCHEMA_VERSION}


def test_claiming_a_legacy_index_under_a_held_lock_is_a_diagnostic(
    index: Path, short_busy_timeout: None
) -> None:
    """§24, the case the open path does not reach.

    An index written before the marker existed already carries
    ``meta.schema_version``, so opening it writes nothing at all — the marker is
    claimed later, by the first command that writes. That put the last piece of
    initialization outside every boundary the rest of this file establishes, and
    it produced a raw ``sqlite3.OperationalError`` after the full ten seconds.
    """
    legacy(index)
    connection = sqlite3.connect(index)
    connection.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
    )
    connection.commit()
    connection.close()

    database = open_database(index)
    try:
        # Opening wrote nothing: the marker is still unclaimed.
        assert database.connection.execute("PRAGMA application_id").fetchone()[0] == 0
        with Holder(index), pytest.raises(MigrationContentionError), database.transaction():
            database.set_meta("last_global_sync", "now")
    finally:
        database.close()


# == 9. crash during initialization ==========================================


@pytest.mark.parametrize("sig", [signal.SIGKILL, signal.SIGTERM])
def test_a_process_killed_while_initializing_leaves_the_database_openable(
    index: Path, sig: int
) -> None:
    """§44: SQLite's own crash recovery, not a cleanup path of RepoAtlas's.

    Killed at a randomized point inside the open, the next process must either
    complete the initialization or find it complete. What must never happen is a
    lock artifact that outlives the dead process — the reason no advisory lock
    file was introduced for this.
    """
    environment = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    script = (
        "import pathlib, sys\n"
        "from repoatlas.storage.sqlite.database import open_database\n"
        "open_database(pathlib.Path(sys.argv[1]))\n"
        "import time; time.sleep(30)\n"
    )
    for delay in (0.005, 0.010, 0.020, 0.040):
        shutil.rmtree(index.parent, ignore_errors=True)
        index.parent.mkdir(parents=True)
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(index)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
        )
        time.sleep(delay)
        process.send_signal(sig)
        process.wait(timeout=60)

        database = open_database(index)
        try:
            assert database.connection.execute("PRAGMA user_version").fetchone()[0] == (
                SCHEMA_VERSION
            )
            assert database.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            database.close()


# == 10. resource hygiene ====================================================


def open_file_count() -> int:
    return len(list(Path(f"/proc/{os.getpid()}/fd").iterdir()))


@pytest.mark.skipif(not Path("/proc/self/fd").exists(), reason="needs /proc")
def test_a_refused_open_leaves_no_file_descriptor_behind(index: Path) -> None:
    """§31: a failed attempt must close its connection.

    A leaked handle is not merely untidy here — it holds a read lock on the file
    the next attempt is waiting to write, so a process that leaked one would
    make the very contention this pass removes permanent within itself.
    """
    foreign(index)
    open_database_failures = 0
    baseline = open_file_count()
    for _ in range(50):
        with pytest.raises(IndexNotRecognizedError):
            open_database(index)
        open_database_failures += 1
    assert open_database_failures == 50
    assert open_file_count() <= baseline + 2


@pytest.mark.skipif(not Path("/proc/self/fd").exists(), reason="needs /proc")
def test_a_contended_open_leaves_no_file_descriptor_behind(
    index: Path, short_busy_timeout: None
) -> None:
    """The same guarantee on the path that now retries and can time out.

    Three times, because one leak inside a tolerance proves nothing and this
    path is the one an operator could hit repeatedly from a wrapper script.
    """
    healthy_delete(index)
    with foreign_holder(index):
        baseline = open_file_count()
        for _ in range(3):
            with pytest.raises(MigrationContentionError):
                open_database(index)
        assert open_file_count() == baseline


# == 11. the user-facing command =============================================


class Workspace:
    """A real, initialized workspace: its own HOME, config, index and repository."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.root = base / "work"
        self.database = base / "index.db"

    @property
    def environment(self) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.base),
            "REPOATLAS_CONFIG": str(self.base / "config.yaml"),
            "REPOATLAS_DB": str(self.database),
            # A closed port: no command may quietly reach a model that happens
            # to be running on this machine.
            "REPOATLAS_SYNC_LOCK_TIMEOUT": "0",
        }

    def run(self, *command: str, timeout: float = 300) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(CLI), *command],
            cwd=self.root,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def revert_to_rollback_journal(self) -> None:
        """Put the index back in DELETE mode, as an older RepoAtlas's would be.

        The point of the CLI tests is to reach the *initialization* write while a
        lock is held, and a healthy WAL index reaches no write at all — which is
        the fix working, and would make those tests pass vacuously.
        """
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("PRAGMA journal_mode = DELETE")
        finally:
            connection.close()


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    """Initialized for real through the CLI, so every command reaches the index."""
    space = Workspace(tmp_path / "ws")
    repo = space.root / "demo"
    repo.mkdir(parents=True)
    (repo / "app.py").write_text("import os\n\nHOST = os.environ['SERVICE_HOST']\n")
    git = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(space.base),
        "GIT_AUTHOR_NAME": "RepoAtlas Tests",
        "GIT_AUTHOR_EMAIL": "tests@example.invalid",
        "GIT_COMMITTER_NAME": "RepoAtlas Tests",
        "GIT_COMMITTER_EMAIL": "tests@example.invalid",
    }
    for args in (
        ("init", "--quiet", "--initial-branch=main"),
        ("add", "-A"),
        ("commit", "--quiet", "-m", "initial"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=git)
    done = space.run("init", str(space.root))
    assert done.returncode == ExitCode.OK, done.stderr
    return space


@pytest.mark.skipif(not CLI.exists(), reason="the repoatlas console script is not installed")
@pytest.mark.parametrize(
    "command",
    [
        ("init", "."),
        ("sync",),
        ("status",),
        ("repos",),
        ("locate", "SERVICE_HOST"),
    ],
    ids=lambda c: c[0],
)
def test_no_command_shows_a_traceback_when_the_index_is_locked(
    workspace: Workspace, command: tuple[str, ...]
) -> None:
    """§13: the whole surface, against a lock held past the timeout.

    Every one of these opens the index, so every one of them could reach the
    terminal as a ``sqlite3`` traceback panel. Each is checked for the diagnostic
    as well as for the absence of a traceback, so a command that fails earlier
    for an unrelated reason cannot pass this vacuously.
    """
    workspace.revert_to_rollback_journal()
    with Holder(workspace.database):
        done = workspace.run(*command)

    output = done.stdout + done.stderr
    for marker in TRACEBACK_MARKERS:
        assert marker not in output, (marker, output[-1500:])
    assert done.returncode == ExitCode.RUNTIME, output
    assert "another process" in done.stderr.lower(), output
    # Bounded: a diagnostic, not a dump of whatever SQLite was holding.
    assert len(done.stderr) < 2000, output


@pytest.mark.skipif(not CLI.exists(), reason="the repoatlas console script is not installed")
def test_the_locked_index_message_says_what_to_do(workspace: Workspace) -> None:
    """A diagnostic is only useful if it names an action. This one says wait."""
    workspace.revert_to_rollback_journal()
    with Holder(workspace.database):
        done = workspace.run("status")

    assert done.returncode == ExitCode.RUNTIME, done.stderr
    assert "another process" in done.stderr.lower()
    assert "run the command again" in done.stderr.lower()
    assert "nothing has been changed" in done.stderr.lower()


@pytest.mark.skipif(not CLI.exists(), reason="the repoatlas console script is not installed")
def test_the_same_command_succeeds_once_the_lock_is_gone(workspace: Workspace) -> None:
    """§40: released, and the workspace is exactly as usable as before.

    Contention must leave no poisoned state — no half-transitioned journal, no
    partial schema, no marker written on the way out.
    """
    workspace.revert_to_rollback_journal()
    holder = Holder(workspace.database)
    holder.__enter__()
    assert workspace.run("status").returncode == ExitCode.RUNTIME
    holder.release()

    done = workspace.run("status")
    assert done.returncode == ExitCode.OK, done.stderr
    assert "Index state: current" in done.stdout, done.stdout

    connection = sqlite3.connect(workspace.database)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == WAL
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()


# == 12. interaction with the workspace writer lock ==========================


@pytest.mark.skipif(not CLI.exists(), reason="the repoatlas console script is not installed")
def test_initialization_and_the_writer_lock_cannot_deadlock(workspace: Workspace) -> None:
    """§43: the two locks are ordered, and only one order exists.

    ``open_workspace`` opens the database and *then* hands back a workspace whose
    ``writer_lock`` a command may take; nothing anywhere takes the writer lock
    first and opens the database afterwards, because the lock is a method on the
    object the open produces. The acquisition order is therefore structural —
    database, then writer lock — and a cycle needs two orders.

    Asserted rather than argued, with a bound: a deadlock shows up here as the
    ``communicate`` timeout, not as a wrong answer.
    """
    workspace.revert_to_rollback_journal()
    environment = workspace.environment | {"REPOATLAS_SYNC_LOCK_TIMEOUT": "30"}
    processes = [
        subprocess.Popen(
            [str(CLI), "sync"],
            cwd=workspace.root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(6)
    ]
    outcomes = [p.communicate(timeout=180) for p in processes]

    codes = [p.returncode for p in processes]
    assert ExitCode.OK in codes, outcomes
    for code, (out, err) in zip(codes, outcomes, strict=True):
        assert code in (ExitCode.OK, ExitCode.SYNC_LOCKED), (code, err)
        for marker in TRACEBACK_MARKERS:
            assert marker not in out + err, (marker, err[-1000:])


@pytest.mark.skipif(not CLI.exists(), reason="the repoatlas console script is not installed")
def test_a_read_command_during_another_process_initialization_is_never_wrong(
    workspace: Workspace,
) -> None:
    """§42: a reader may wait, and may fail — it may not see a half-built index.

    The states it must never report are the interesting ones: a foreign database
    (which is what a partially created one could look like to a shape check), or
    a success built on a schema that is not yet complete.
    """
    workspace.revert_to_rollback_journal()
    environment = workspace.environment
    writer = subprocess.Popen(
        [str(CLI), "sync"],
        cwd=workspace.root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    readers = [
        subprocess.Popen(
            [str(CLI), "status"],
            cwd=workspace.root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(6)
    ]
    writer.communicate(timeout=180)
    for reader in readers:
        out, err = reader.communicate(timeout=180)
        assert reader.returncode in (ExitCode.OK, ExitCode.RUNTIME), (reader.returncode, err)
        assert "not recognized" not in err.lower(), err
        assert "another application" not in err.lower(), err
        for marker in TRACEBACK_MARKERS:
            assert marker not in out + err, (marker, err[-1000:])
