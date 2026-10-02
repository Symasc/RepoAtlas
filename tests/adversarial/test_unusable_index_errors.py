"""An unusable index is a product state, not an uncaught exception (V1_SPEC §4).

The release review found that pointing RepoAtlas at a damaged database produced
exit 1 and a Rich traceback panel — the same result as a crash in the parser, and
nothing an operator or a wrapper script could act on. V1_SPEC §4 already reserved
exit 3 for "index missing/unusable"; nothing reached it.

What makes this file adversarial rather than a pair of happy-path assertions is
the *other* direction. Exit 3 tells someone their index is the problem, and the
obvious next move is to delete it. So every case here is paired with a control
that must **not** reach exit 3: a programmer error, an unmapped SQLite code, a
context failure, writer-lock contention, and a migration state Pass 2 recovers
from. A fix that turns every exception into "your index is corrupt" passes the
first half of this file and fails the second.

User-facing behaviour is asserted through the installed console script in a
separate process, because exit codes and the absence of a traceback are
properties of the program, not of a Typer callable.
"""

from __future__ import annotations

import errno
import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from repoatlas.errors import (
    ExitCode,
    IndexCorruptError,
    IndexInaccessibleError,
    IndexSchemaError,
    IndexUnavailableError,
    MigrationContentionError,
    MigrationError,
    MigrationFailedError,
    UnrecognizedSchemaError,
)
from repoatlas.storage.sqlite.database import SCHEMA_VERSION, open_database
from repoatlas.storage.sqlite.errors import classify
from repoatlas.storage.sqlite.migrator import load_migrations, schema_objects

CLI = Path(sys.executable).parent / "repoatlas"

pytestmark = [
    pytest.mark.adversarial,
    pytest.mark.skipif(not CLI.exists(), reason="the repoatlas console script is not installed"),
]

#: Every command that opens the index, with arguments that reach the store.
COMMANDS: tuple[tuple[str, ...], ...] = (
    ("status",),
    ("repos",),
    ("sync",),
    ("locate", "SERVICE_HOST"),
)

#: What a Rich traceback panel and a bare Python traceback both contain.
TRACEBACK_MARKERS = (
    "Traceback (most recent call last)",
    'File "',
    "sqlite3.",
    "DatabaseError",
    "OperationalError",
)


class Result:
    def __init__(self, done: subprocess.CompletedProcess[str]) -> None:
        self.exit_code = done.returncode
        self.stdout = done.stdout
        self.stderr = done.stderr
        self.output = done.stdout + done.stderr

    def __repr__(self) -> str:  # pragma: no cover - failure messages only
        return f"exit={self.exit_code}\n--stdout--\n{self.stdout}\n--stderr--\n{self.stderr}"


class Harness:
    """One throwaway workspace: its own root, config, index and HOME."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.root = base / "work"
        self.repo = self.root / "demo"
        self.database = base / "atlas.db"

    @property
    def environment(self) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.base),
            "REPOATLAS_CONFIG": str(self.base / "config.yaml"),
            "REPOATLAS_DB": str(self.database),
            # A closed port. Any attempt to reach a model fails loudly rather
            # than quietly succeeding on a machine that happens to run Ollama.
            "REPOATLAS_SYNC_LOCK_TIMEOUT": "0",
        }

    def run(self, *args: str, cwd: Path | None = None) -> Result:
        return Result(
            subprocess.run(
                [str(CLI), *args],
                cwd=cwd or self.root,
                env=self.environment,
                capture_output=True,
                text=True,
                timeout=300,
            )
        )

    def initialize(self) -> None:
        assert self.run("init", str(self.root)).exit_code == ExitCode.OK

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database)

    def user_version(self) -> int:
        connection = self.connect()
        try:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])
        finally:
            connection.close()


def _git(repo: Path, *args: str) -> None:
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


@pytest.fixture
def bare(tmp_path: Path) -> Harness:
    """A workspace with a repository on disk and nothing initialized yet."""
    harness = Harness(tmp_path / "ws")
    harness.repo.mkdir(parents=True)
    (harness.repo / "app.py").write_text(
        "import os\n\nSERVICE_HOST = os.environ['SERVICE_HOST']\n", encoding="utf-8"
    )
    _git(harness.repo, "init", "--quiet", "--initial-branch=main")
    _git(harness.repo, "add", "-A")
    _git(harness.repo, "commit", "--quiet", "-m", "initial")
    return harness


@pytest.fixture
def indexed(bare: Harness) -> Harness:
    """The same workspace, initialized: a real current-schema index."""
    bare.initialize()
    return bare


def assert_clean_failure(result: Result, code: int) -> None:
    """The whole user-facing contract for an expected failure, in one place."""
    assert result.exit_code == code, result
    for marker in TRACEBACK_MARKERS:
        assert marker not in result.output, (marker, result)
    assert result.stderr.strip(), result
    # Bounded: a diagnostic, not a dump of whatever SQLite was holding.
    assert len(result.stderr) < 2000, result


def truncate_and_scramble(path: Path) -> None:
    """A file that keeps SQLite's header and is malformed inside it."""
    blob = bytearray(path.read_bytes())
    blob[4096:8192] = b"\x00" * 4096
    path.write_bytes(bytes(blob[: len(blob) // 2]))


# == 1-2. the review's two reproductions, every command ======================


@pytest.mark.parametrize("command", COMMANDS, ids=lambda c: c[0])
def test_random_bytes_at_the_index_path_exit_3(indexed: Harness, command) -> None:
    """The review's first case: `echo "not sqlite" > atlas.db`."""
    indexed.database.write_bytes(b"not sqlite\n" * 40)
    result = indexed.run(*command)
    assert_clean_failure(result, ExitCode.NOT_INITIALIZED)
    assert "not a SQLite database" in result.stderr
    assert "SQLITE_NOTADB" in result.stderr


@pytest.mark.parametrize("command", COMMANDS, ids=lambda c: c[0])
def test_a_malformed_database_image_exit_3(indexed: Harness, command) -> None:
    """The review's second case: a real SQLite header over a damaged image."""
    truncate_and_scramble(indexed.database)
    result = indexed.run(*command)
    assert_clean_failure(result, ExitCode.NOT_INITIALIZED)
    assert "malformed" in result.stderr
    assert "SQLITE_CORRUPT" in result.stderr


# == 3. what an empty file actually means ====================================


def test_a_zero_byte_file_is_an_empty_index_not_a_corrupt_one(bare: Harness) -> None:
    """Observed rather than assumed: SQLite reads zero bytes as a new database.

    Reporting it as corruption would be a lie that costs the user nothing to
    obey and everything to believe — they would move a file aside that RepoAtlas
    was about to fill in.
    """
    bare.database.parent.mkdir(parents=True, exist_ok=True)
    bare.database.write_bytes(b"")
    bare.initialize()
    assert bare.run("status").exit_code == ExitCode.OK
    assert bare.user_version() == SCHEMA_VERSION


# == 4. corruption that only appears when something reads it =================


@pytest.mark.parametrize("command", COMMANDS, ids=lambda c: c[0])
def test_a_required_table_removed_after_open_exit_3(indexed: Harness, command) -> None:
    """§18: the open succeeds; the *query* is what finds the damage.

    Handling only the connect call would leave this exact shape — a database
    SQLite is happy to open and RepoAtlas cannot use — producing the traceback
    this whole file exists to remove.
    """
    connection = indexed.connect()
    connection.execute("DROP TABLE repositories")
    connection.commit()
    connection.close()

    result = indexed.run(*command)
    assert_clean_failure(result, ExitCode.NOT_INITIALIZED)
    assert "missing part of its schema" in result.stderr
    assert "repositories" in result.stderr


def test_a_missing_object_repoatlas_never_creates_is_not_an_index_failure(
    indexed: Harness, tmp_path: Path
) -> None:
    """The control for the case above: `no such table` is not enough on its own.

    SQLite reports a typo in RepoAtlas's own SQL with the same result code as a
    database that has lost a table. Only the name separates them, so only the
    name is trusted.
    """
    database = open_database(tmp_path / "control.db")
    try:
        with pytest.raises(sqlite3.OperationalError) as caught:
            database.execute("SELECT 1 FROM repositores")
        assert not isinstance(caught.value, IndexUnavailableError)
    finally:
        database.close()
    assert "repositories" in schema_objects()
    assert "repositores" not in schema_objects()


def test_corruption_introduced_while_the_process_is_running_is_translated(
    indexed: Harness,
) -> None:
    """§19: hostile external mutation of a live database file.

    RepoAtlas does not promise to survive someone scribbling on its index
    mid-run, and this asserts no recovery. What it asserts is the part that is
    RepoAtlas's to get right: when SQLite eventually reads a damaged page and
    says so, the answer is a diagnostic and exit 3 rather than a traceback.

    `PRAGMA shrink_memory` is what makes the case reproducible — without it the
    first queries are served from the page cache and never touch the disk, which
    is exactly what an earlier version of this test measured and mistook for
    "no error".
    """
    database = open_database(indexed.database)
    try:
        database.execute("SELECT count(*) FROM repositories").fetchone()

        blob = bytearray(indexed.database.read_bytes())
        page = int.from_bytes(blob[16:18], "big")
        for index in range(2, min(len(blob) // page, 40)):
            blob[index * page : index * page + 32] = b"\xff" * 32
        indexed.database.write_bytes(bytes(blob))
        database.execute("PRAGMA shrink_memory")

        with pytest.raises(IndexCorruptError) as caught:
            database.execute("SELECT count(*) FROM chunks").fetchall()
        assert caught.value.exit_code == ExitCode.NOT_INITIALIZED
    finally:
        database.close()


def test_a_stray_wal_or_shm_sidecar_is_sqlites_business_not_a_failure(
    indexed: Harness,
) -> None:
    """§20: SQLite owns transactional recovery; RepoAtlas invents nothing.

    A stale `-shm` beside an intact database is a normal state SQLite rebuilds,
    and reporting it as an unusable index would be a false alarm on every crash
    recovery.
    """
    indexed.database.with_name("atlas.db-shm").write_bytes(b"\x00" * 32768)
    assert indexed.run("status").exit_code == ExitCode.OK


# == 5. schema versions this build cannot operate on =========================


@pytest.mark.parametrize("command", COMMANDS, ids=lambda c: c[0])
def test_a_future_schema_version_is_refused_exit_3(indexed: Harness, command) -> None:
    connection = indexed.connect()
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 7}")
    connection.commit()
    connection.close()
    before = indexed.database.read_bytes()

    result = indexed.run(*command)
    assert_clean_failure(result, ExitCode.NOT_INITIALIZED)
    assert "newer version of RepoAtlas" in result.stderr
    assert indexed.database.read_bytes() == before, "a refused index must not be touched"


def test_a_future_schema_is_not_downgraded_by_the_open_itself(indexed: Harness) -> None:
    """The pragmas that configure a connection write to the file, so the version
    check has to come before them, not after the migrations."""
    connection = indexed.connect()
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 7}")
    connection.commit()
    connection.close()

    with pytest.raises(IndexSchemaError):
        open_database(indexed.database)
    assert indexed.user_version() == SCHEMA_VERSION + 7


# == 6. migration states: Pass 2's truth, preserved ==========================


def test_a_recognized_partial_migration_still_recovers_at_exit_0(indexed: Harness) -> None:
    """§15: the historical damage Pass 2 repairs must not become "unusable".

    Every committed prefix of the last migration is built on a genuine
    previous-version database and opened through the real CLI. This is the test
    that fails if the new classifier is placed where it can see a *recoverable*
    state and call it a corrupt one.
    """
    migrations = load_migrations()
    latest = migrations[-1]
    for cut in range(len(latest.statements)):
        indexed.database.unlink(missing_ok=True)
        connection = sqlite3.connect(indexed.database, isolation_level="DEFERRED")
        for pragma in ("journal_mode = WAL", "foreign_keys = ON"):
            connection.execute(f"PRAGMA {pragma}")
        for migration in migrations[:-1]:
            connection.execute("BEGIN IMMEDIATE")
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {migration.version}")
            connection.commit()
        for statement in latest.statements[:cut]:
            connection.execute(statement)
        connection.commit()
        connection.close()

        result = indexed.run("status")
        assert result.exit_code == ExitCode.OK, (cut, result)
        assert indexed.user_version() == SCHEMA_VERSION, cut


@pytest.mark.parametrize("command", COMMANDS, ids=lambda c: c[0])
def test_an_unrecognized_schema_shape_exit_3(indexed: Harness, command) -> None:
    """§16: presentation changes, migration policy does not.

    Pass 2 already refused this state and refuses it identically now. What
    changed is that "RepoAtlas will not modify a schema it does not recognize"
    now leaves at exit 3 instead of exit 1.
    """
    connection = indexed.connect()
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")
    connection.execute("DROP INDEX IF EXISTS idx_repositories_slug")
    connection.execute("CREATE INDEX idx_repositories_slug ON repositories (name)")
    connection.commit()
    connection.close()

    result = indexed.run(*command)
    assert_clean_failure(result, ExitCode.NOT_INITIALIZED)
    assert "does not recognize" in result.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_a_read_only_database_behind_on_schema_is_not_a_failed_migration(
    tmp_path: Path,
) -> None:
    """The migrator describes migrations; it must not describe file modes.

    A database at the previous version that cannot be written reported
    "migration 005 failed and was rolled back" at exit 1 — sending the reader
    after a defect in a `.sql` file when the fault was a permission bit, and
    nothing was wrong with the migration or the database at all.
    """
    path = tmp_path / "old.db"
    connection = sqlite3.connect(path, isolation_level="DEFERRED")
    connection.execute("PRAGMA journal_mode = WAL")
    for migration in load_migrations()[:-1]:
        connection.execute("BEGIN IMMEDIATE")
        for statement in migration.statements:
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version = {migration.version}")
        connection.commit()
    connection.close()
    path.chmod(0o400)

    try:
        with pytest.raises(IndexInaccessibleError) as caught:
            open_database(path)
    finally:
        path.chmod(0o600)
    assert caught.value.exit_code == ExitCode.NOT_INITIALIZED
    assert not isinstance(caught.value, MigrationFailedError)


def test_the_three_migration_causes_keep_three_meanings() -> None:
    """§14: subtyped, not collapsed.

    Contention resolves itself and a shipped migration that cannot run is a
    RepoAtlas defect; neither is a state of the user's index, and telling someone
    mid-upgrade that their database is unusable would be false.
    """
    assert UnrecognizedSchemaError("x").exit_code == ExitCode.NOT_INITIALIZED
    assert MigrationContentionError("x").exit_code == ExitCode.RUNTIME
    assert MigrationFailedError("x").exit_code == ExitCode.RUNTIME
    assert issubclass(UnrecognizedSchemaError, MigrationError)
    assert issubclass(UnrecognizedSchemaError, IndexUnavailableError)
    assert not issubclass(MigrationContentionError, IndexUnavailableError)
    assert not issubclass(MigrationFailedError, IndexUnavailableError)


# == 7. reachable, but not usable ============================================


@pytest.mark.parametrize("command", COMMANDS, ids=lambda c: c[0])
def test_a_directory_where_the_index_belongs_exit_3(indexed: Harness, command) -> None:
    indexed.database.unlink()
    indexed.database.mkdir()
    result = indexed.run(*command)
    assert_clean_failure(result, ExitCode.NOT_INITIALIZED)
    assert "cannot read the index" in result.stderr
    assert "corrupt" not in result.stderr.lower(), "a path fault is not corruption"


def test_a_symlink_loop_at_the_index_path_exit_3(indexed: Harness) -> None:
    """§24: a symlink to a real database is followed and works; only a link that
    resolves to nothing usable is a failure, and it is a path fault rather than
    corruption."""
    indexed.database.unlink()
    indexed.database.symlink_to(indexed.database)
    result = indexed.run("status")
    assert_clean_failure(result, ExitCode.NOT_INITIALIZED)
    assert "cannot read the index" in result.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_an_unreadable_index_exit_3_without_calling_it_corrupt(indexed: Harness) -> None:
    """§9: never tell someone to replace a database whose permissions are wrong."""
    indexed.database.chmod(0o000)
    try:
        result = indexed.run("status")
    finally:
        indexed.database.chmod(0o600)
    assert_clean_failure(result, ExitCode.NOT_INITIALIZED)
    assert "cannot read the index" in result.stderr
    assert "corrupt" not in result.stderr.lower()
    assert "malformed" not in result.stderr.lower()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
def test_a_read_only_index_still_reads_and_refuses_to_write(indexed: Harness) -> None:
    """Read-only is not unusable for a reader, and the distinction is kept."""
    indexed.database.chmod(0o400)
    try:
        assert indexed.run("status").exit_code == ExitCode.OK
        assert indexed.run("locate", "SERVICE_HOST").exit_code == ExitCode.OK
        result = indexed.run("sync")
    finally:
        indexed.database.chmod(0o600)
    assert_clean_failure(result, ExitCode.NOT_INITIALIZED)
    assert "cannot write to the index" in result.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_an_unwritable_parent_directory_exit_3(indexed: Harness) -> None:
    """The failure happens in `mkdir`, before SQLite is involved at all."""
    holder = indexed.base / "holder"
    holder.mkdir(parents=True)
    indexed.database = holder / "nested" / "atlas.db"
    holder.chmod(0o500)
    try:
        result = indexed.run("status")
    finally:
        holder.chmod(0o700)
    assert_clean_failure(result, ExitCode.NOT_INITIALIZED)
    assert "cannot reach the index" in result.stderr


# == 8. nothing there at all =================================================


@pytest.mark.parametrize("command", COMMANDS, ids=lambda c: c[0])
def test_no_configuration_and_no_index_stays_not_initialized(bare: Harness, command) -> None:
    """The pre-existing exit 3, unchanged: absence, not damage."""
    result = bare.run(*command)
    assert_clean_failure(result, ExitCode.NOT_INITIALIZED)
    assert "not initialized" in result.stderr.lower()
    assert "corrupt" not in result.stderr.lower()


# == 9. init must never take an existing database as scratch space ===========


def test_init_reuses_a_valid_current_index(indexed: Harness) -> None:
    assert indexed.run("init", str(indexed.root)).exit_code == ExitCode.OK


def test_init_refuses_a_corrupt_index_without_replacing_it(indexed: Harness) -> None:
    indexed.database.write_bytes(b"not sqlite\n" * 40)
    before = indexed.database.read_bytes()
    result = indexed.run("init", str(indexed.root))
    assert_clean_failure(result, ExitCode.NOT_INITIALIZED)
    assert indexed.database.read_bytes() == before, "init must not overwrite evidence"


def test_init_refuses_a_future_index_without_downgrading_it(indexed: Harness) -> None:
    connection = indexed.connect()
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 7}")
    connection.commit()
    connection.close()
    before = indexed.database.read_bytes()

    result = indexed.run("init", str(indexed.root))
    assert_clean_failure(result, ExitCode.NOT_INITIALIZED)
    assert indexed.database.read_bytes() == before


def test_an_unrelated_database_whose_names_collide_is_refused_intact(bare: Harness) -> None:
    """A SQLite database that is not RepoAtlas's, carrying data of its own.

    The migrator's fail-closed check is what protects it: the first statement
    whose object already exists with a different definition is a schema RepoAtlas
    does not recognize, so nothing is created, nothing is dropped and the version
    stamp is left alone.
    """
    bare.database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(bare.database)
    connection.execute("CREATE TABLE files(id INTEGER PRIMARY KEY, secret TEXT)")
    connection.execute("INSERT INTO files VALUES (1, 'CANARY')")
    connection.execute("CREATE TABLE repositories(id INTEGER PRIMARY KEY, who TEXT)")
    connection.execute("INSERT INTO repositories VALUES (1, 'CANARY-REPOS')")
    connection.commit()
    connection.close()

    result = bare.run("init", str(bare.root))
    assert_clean_failure(result, ExitCode.NOT_INITIALIZED)

    connection = sqlite3.connect(bare.database)
    try:
        assert connection.execute("SELECT * FROM files").fetchall() == [(1, "CANARY")]
        assert connection.execute("SELECT * FROM repositories").fetchall() == [(1, "CANARY-REPOS")]
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert names == {"files", "repositories"}, "no RepoAtlas object may be added"
    finally:
        connection.close()


# == 10. precedence: which failure is reported when several are true =========


def test_an_unusable_index_is_never_reported_as_a_model_failure(indexed: Harness) -> None:
    """§27: exit 3, and never the retired code 4.

    Exit 4 was V1's "no reasoning model is reachable". Nothing raises it --- there
    is no model --- and the number is reserved rather than reused so a wrapper
    still branching on it cannot be handed a new meaning. What this pins is the
    taxonomy: a storage failure reports storage, from every command.

    The original form of this test stood a real listener in for the model server
    and asserted nothing connected to it. That assertion moved somewhere it still
    means something: ``tests/test_no_embedded_reasoner.py`` denies ``socket``
    outright and runs the whole capability surface under it, which is a stronger
    statement than watching a port the product was never told about.
    """
    indexed.database.write_bytes(b"not sqlite\n" * 40)
    for command in COMMANDS:
        result = indexed.run(*command)
        assert_clean_failure(result, ExitCode.NOT_INITIALIZED)


def test_writer_lock_contention_still_exits_5(indexed: Harness) -> None:
    """§28: a held lock over a healthy index is contention, not unavailability."""
    from repoatlas.sync.lock import writer_lock

    with writer_lock(indexed.database, timeout=0):
        result = indexed.run("sync")
    assert result.exit_code == ExitCode.SYNC_LOCKED, result
    assert "Traceback (most recent call last)" not in result.output


def test_an_unusable_index_precedes_the_writer_lock(indexed: Harness) -> None:
    """The index is opened to build the workspace, before any command takes the
    lock, so a corrupt index reports itself even while a sync holds the lock."""
    from repoatlas.sync.lock import writer_lock

    with writer_lock(indexed.database, timeout=0):
        indexed.database.write_bytes(b"not sqlite\n" * 40)
        result = indexed.run("sync")
    assert_clean_failure(result, ExitCode.NOT_INITIALIZED)


def test_context_resolution_still_exits_2_on_a_healthy_index(
    indexed: Harness, tmp_path: Path
) -> None:
    """§29: exit 2 is not swallowed by a boundary that catches too much.

    `status`, not `sync`: from outside every repository `sync` is the whole workspace.
    """
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    result = indexed.run("status", cwd=outside)
    assert result.exit_code == ExitCode.CONTEXT, result
    assert "Traceback (most recent call last)" not in result.output


def test_a_corrupt_index_precedes_context_resolution(indexed: Harness, tmp_path: Path) -> None:
    """And the order is fixed rather than incidental: the workspace is opened
    before context is resolved, so the storage failure is the one reported."""
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    indexed.database.write_bytes(b"not sqlite\n" * 40)
    result = indexed.run("sync", cwd=outside)
    assert_clean_failure(result, ExitCode.NOT_INITIALIZED)


# == 11. controls: what must never become exit 3 =============================


@pytest.mark.parametrize(
    "raiser",
    [
        pytest.param(lambda: RuntimeError("BUG_SENTINEL"), id="RuntimeError"),
        pytest.param(lambda: TypeError("BUG_SENTINEL"), id="TypeError"),
        pytest.param(lambda: AttributeError("BUG_SENTINEL"), id="AttributeError"),
    ],
)
def test_a_programmer_error_inside_storage_is_not_an_unusable_index(
    tmp_path: Path, raiser: Callable[[], Exception]
) -> None:
    """§30: the mandatory control.

    A defect in RepoAtlas reported as "your index is corrupt" costs the user
    their index and hides the bug. Both halves of that are worse than a
    traceback, so the boundary must let this through untouched.
    """
    database = open_database(tmp_path / "control.db")
    try:
        with pytest.raises(type(raiser())) as caught, database.transaction():
            raise raiser()
        assert "BUG_SENTINEL" in str(caught.value)
        assert not isinstance(caught.value, IndexUnavailableError)
    finally:
        database.close()


def test_a_filesystem_error_about_a_repository_is_not_an_index_failure(
    tmp_path: Path,
) -> None:
    """The sync pipeline reads repository files inside the write transaction, so
    a `PermissionError` about *a repository file* can be raised there. Reporting
    that as an unreachable index would be the same laundering as the control
    above, aimed at the filesystem instead of at bugs."""
    database = open_database(tmp_path / "control.db")
    try:
        with pytest.raises(PermissionError) as caught:  # noqa: SIM117 - two roles
            with database.transaction():
                raise PermissionError(errno.EACCES, "Permission denied", "/repo/app.py")
        assert not isinstance(caught.value, IndexUnavailableError)
    finally:
        database.close()


def test_a_programmer_error_reaches_the_cli_as_an_internal_failure(indexed: Harness) -> None:
    """The same control end to end: exit 1, not exit 3.

    Injected through `sitecustomize` rather than a monkeypatch, because the
    assertion is about the *installed program's* exit code and a monkeypatch
    cannot cross a subprocess boundary.
    """
    (indexed.base / "sitecustomize.py").write_text(
        "from repoatlas.storage.sqlite import stores\n"
        "def boom(self, *a, **k):\n"
        "    raise RuntimeError('BUG_SENTINEL')\n"
        "stores.SqliteRepositoryStore.list_all = boom\n",
        encoding="utf-8",
    )
    environment = dict(indexed.environment, PYTHONPATH=str(indexed.base))
    done = subprocess.run(
        [str(CLI), "repos"],
        cwd=indexed.root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert done.returncode == ExitCode.RUNTIME, done.stdout + done.stderr
    assert done.returncode != ExitCode.NOT_INITIALIZED
    assert "BUG_SENTINEL" in done.stdout + done.stderr


@pytest.mark.parametrize(
    "code",
    [
        pytest.param(sqlite3.SQLITE_BUSY, id="SQLITE_BUSY"),
        pytest.param(sqlite3.SQLITE_LOCKED, id="SQLITE_LOCKED"),
        pytest.param(sqlite3.SQLITE_CONSTRAINT, id="SQLITE_CONSTRAINT"),
        pytest.param(sqlite3.SQLITE_FULL, id="SQLITE_FULL"),
        pytest.param(sqlite3.SQLITE_IOERR, id="SQLITE_IOERR"),
        pytest.param(sqlite3.SQLITE_SCHEMA, id="SQLITE_SCHEMA"),
        pytest.param(sqlite3.SQLITE_MISMATCH, id="SQLITE_MISMATCH"),
        pytest.param(sqlite3.SQLITE_ERROR, id="SQLITE_ERROR"),
    ],
)
def test_an_unclassified_sqlite_code_is_not_rewritten_as_corruption(
    tmp_path: Path, code: int
) -> None:
    """§31: the classifier enumerates, and everything else falls open.

    `SQLITE_FULL`, `SQLITE_IOERR` and `SQLITE_SCHEMA` are here deliberately: they
    are plausible index failures that this pass did **not** reproduce, and a
    guess about what they mean is exactly what the fail-open rule forbids. If a
    later pass reproduces one, it moves out of this list on evidence.
    """
    error = sqlite3.OperationalError("something happened")
    assert classify(_with_code(error, code), tmp_path / "atlas.db") is None


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        pytest.param(sqlite3.SQLITE_NOTADB, IndexCorruptError, id="SQLITE_NOTADB"),
        pytest.param(sqlite3.SQLITE_CORRUPT, IndexCorruptError, id="SQLITE_CORRUPT"),
        pytest.param(sqlite3.SQLITE_CANTOPEN, IndexInaccessibleError, id="SQLITE_CANTOPEN"),
        pytest.param(sqlite3.SQLITE_READONLY, IndexInaccessibleError, id="SQLITE_READONLY"),
        pytest.param(sqlite3.SQLITE_PERM, IndexInaccessibleError, id="SQLITE_PERM"),
        pytest.param(
            sqlite3.SQLITE_READONLY_DIRECTORY, IndexInaccessibleError, id="extended READONLY"
        ),
        pytest.param(sqlite3.SQLITE_CORRUPT_INDEX, IndexCorruptError, id="extended CORRUPT"),
        pytest.param(sqlite3.SQLITE_CANTOPEN_ISDIR, IndexInaccessibleError, id="extended CANTOPEN"),
    ],
)
def test_every_classified_code_maps_to_one_category(
    tmp_path: Path, code: int, expected: type
) -> None:
    """Extended codes included: they reduce to their primary code rather than
    being enumerated one by one, so SQLite may add more without a code change."""
    translated = classify(_with_code(sqlite3.OperationalError("x"), code), tmp_path / "atlas.db")
    assert isinstance(translated, expected)
    assert isinstance(translated, IndexUnavailableError)
    assert translated.exit_code == ExitCode.NOT_INITIALIZED
    assert str(tmp_path / "atlas.db") in translated.render()


@pytest.mark.parametrize(
    ("number", "classified"),
    [
        (errno.EACCES, True),
        (errno.EROFS, True),
        (errno.ENOTDIR, True),
        (errno.ENOSPC, False),
        (errno.EIO, False),
    ],
)
def test_operating_system_faults_are_classified_by_errno(
    tmp_path: Path, number: int, classified: bool
) -> None:
    """The `mkdir` that precedes the connection raises `OSError`, not
    `sqlite3.Error`, and the same enumerate-or-fail-open rule applies to it."""
    error = OSError(number, os.strerror(number))
    translated = classify(error, tmp_path / "atlas.db")
    assert (translated is not None) is classified


def test_a_non_sqlite_non_os_exception_is_never_classified(tmp_path: Path) -> None:
    assert classify(RuntimeError("BUG_SENTINEL"), tmp_path / "atlas.db") is None
    assert classify(ValueError("BUG_SENTINEL"), tmp_path / "atlas.db") is None


# == 12. output shape ========================================================


@pytest.mark.parametrize("command", [("status",), ("repos",), ("locate", "x")], ids=lambda c: c[0])
def test_json_mode_does_not_emit_half_valid_output_before_failing(
    indexed: Harness, command
) -> None:
    """§13: the established contract is human stderr plus an exit code.

    What must never happen is a success document on stdout followed by a failure,
    which is what a caller piping to `jq` would parse as an answer.
    """
    indexed.database.write_bytes(b"not sqlite\n" * 40)
    result = indexed.run(*command, "--json")
    assert_clean_failure(result, ExitCode.NOT_INITIALIZED)
    assert result.stdout.strip() == "", result


def test_the_diagnostic_names_the_path_and_nothing_from_inside_the_file(
    indexed: Harness,
) -> None:
    """§11: the index path is operator-owned state and is named. The bytes in the
    file are not, and no part of them appears."""
    indexed.database.write_bytes(b"CANARY-INDEX-CONTENT " * 40)
    result = indexed.run("status")
    assert_clean_failure(result, ExitCode.NOT_INITIALIZED)
    assert str(indexed.database) in result.stderr
    assert "CANARY" not in result.output


def _with_code(error: sqlite3.Error, code: int) -> sqlite3.Error:
    """A SQLite exception carrying ``code``, for classifier unit tests.

    ``sqlite_errorcode`` is read-only on a real exception and most of these codes
    cannot be provoked on demand, so the classifier is given the one attribute it
    reads. It is a function of the code, which is exactly what this asserts.
    """

    class _Coded(type(error)):  # type: ignore[misc]
        sqlite_errorcode = code

    return _Coded(*error.args)
