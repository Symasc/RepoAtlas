"""Database ownership: RepoAtlas must prove it owns a file before writing to it.

The release review pointed RepoAtlas at a SQLite database it had never created —
a ``payroll`` table, one row, ``user_version = 0`` — and ``repoatlas init``
adopted it: twenty-one tables created, every migration run, the schema version
stamped. Nothing collided, so nothing was destroyed. That is the finding, not the
mitigation. RepoAtlas rewrote a file whose only claim to being an index was that
its table names happened not to clash, and **absence of a collision is not
evidence of ownership**.

``user_version`` could not supply the missing evidence: it is generic SQLite
metadata that every database has, and ``0`` is what it says about a file that has
never heard of RepoAtlas. So identity became its own thing — ``PRAGMA
application_id``, SQLite's own header field for naming the format a file belongs
to — and it is asked about *before* the schema version, because the two answer
different questions.

What makes this file adversarial rather than a pair of happy-path assertions is
the second direction. A refusal is only correct if it is narrow: every existing
RepoAtlas index in the world predates the marker, and a fix that made them all
read as foreign would be far worse than the defect. So each refusal here is
paired with a genuine historical database that must be *recognized* — by schema
shape, exactly, against the states the committed migrations actually produce —
and with a lookalike that must not be.

Everything is real: real SQLite files, real historical schemas built by applying
the real migrations, the installed console script in a separate process for exit
codes, and real concurrent and killed processes.
"""

from __future__ import annotations

import hashlib
import os
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from repoatlas.errors import (
    ExitCode,
    IndexCorruptError,
    IndexForeignApplicationError,
    IndexNotRecognizedError,
    IndexSchemaError,
    IndexUnavailableError,
)
from repoatlas.storage.sqlite.database import SCHEMA_VERSION, open_database
from repoatlas.storage.sqlite.identity import Ownership, establish, user_objects
from repoatlas.storage.sqlite.migrator import (
    APPLICATION_ID,
    application_id,
    load_migrations,
    recognizes,
    user_version,
)

CLI = Path(sys.executable).parent / "repoatlas"
REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.adversarial

#: Every command that opens the index. `init` is listed last and matters most:
#: initialization is where the adoption actually happened.
COMMANDS: tuple[tuple[str, ...], ...] = (
    ("status",),
    ("repos",),
    ("sync",),
    ("locate", "SERVICE_HOST"),
    ("init", "."),
)

TRACEBACK_MARKERS = (
    "Traceback (most recent call last)",
    'File "',
    "sqlite3.",
    "DatabaseError",
    "OperationalError",
)


# == harness =================================================================


def connect(path: Path) -> sqlite3.Connection:
    """A connection configured exactly as production configures one."""
    connection = sqlite3.connect(path, isolation_level="DEFERRED")
    connection.row_factory = sqlite3.Row
    for pragma in ("busy_timeout = 10000", "foreign_keys = ON", "synchronous = NORMAL"):
        connection.execute(f"PRAGMA {pragma}")
    return connection


def historical(path: Path, version: int) -> sqlite3.Connection:
    """A genuine pre-marker RepoAtlas database at ``version``.

    Built by applying the real migrations statement by statement — not by
    stamping ``user_version`` onto the current schema, which would have the new
    columns already and let a vacuous test pass. The ``application_id`` is left
    at 0 because these fixtures represent indexes written before the marker
    existed, which is the entire compatibility problem.
    """
    connection = connect(path)
    connection.execute("PRAGMA journal_mode = WAL")
    for migration in load_migrations():
        if migration.version > version:
            break
        connection.execute("BEGIN IMMEDIATE")
        for statement in migration.statements:
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version = {migration.version}")
        connection.commit()
    assert application_id(connection) == 0
    return connection


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def siblings(path: Path) -> list[str]:
    return sorted(f.name for f in path.parent.iterdir() if f.name.startswith(path.name))


def marker(path: Path) -> int:
    """The marker, read without opening the file for writing."""
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return int(connection.execute("PRAGMA application_id").fetchone()[0])
    finally:
        connection.close()


def stored_version(path: Path) -> int:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()


def objects(path: Path) -> list[str]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return sorted(str(r[0]) for r in connection.execute("SELECT name FROM sqlite_master"))
    finally:
        connection.close()


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
        self.repo = self.root / "svc"
        self.database = base / "atlas.db"

    def environment(self, **extra: str) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.base),
            "REPOATLAS_CONFIG": str(self.base / "config.yaml"),
            "REPOATLAS_DB": str(self.database),
            # A closed port: any attempt to reach a model fails loudly rather
            # than quietly succeeding on a machine that happens to run Ollama.
            "REPOATLAS_SYNC_LOCK_TIMEOUT": "0",
            **extra,
        }

    def run(self, *args: str, cwd: Path | None = None, **extra: str) -> Result:
        return Result(
            subprocess.run(
                [str(CLI), *args],
                cwd=cwd or self.root,
                env=self.environment(**extra),
                capture_output=True,
                text=True,
                timeout=300,
            )
        )

    def initialize(self) -> Result:
        return self.run("init", str(self.root))

    def settle(self) -> None:
        """Checkpoint the WAL without leaving WAL mode — how a real index rests.

        Needed before hashing: an index is left in WAL mode, and a comparison
        taken against a file in rollback-journal mode would measure RepoAtlas
        switching the journal mode rather than anything about identity.
        """
        (self.base / "atlas.db.lock").unlink(missing_ok=True)
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()

    def unmark(self) -> None:
        """Turn a real index into a genuine pre-marker one.

        The most faithful legacy fixture available: everything a current index
        has — schema, meta rows, facts — minus the marker, which is exactly what
        an index written before this pass is.
        """
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("PRAGMA application_id = 0")
            connection.commit()
        finally:
            connection.close()
        self.settle()


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
    """The same workspace, initialized: a real, marked, current index."""
    assert bare.initialize().exit_code == ExitCode.OK
    bare.settle()
    return bare


@pytest.fixture
def foreign(indexed: Harness) -> Harness:
    """A fully configured workspace whose index file is somebody else's database.

    Built from an initialized workspace on purpose. Pointing a *bare* workspace
    at a foreign database proves nothing: with no root configured, every read
    command exits 3 with "RepoAtlas is not initialized" before the database is
    opened at all, and a test written that way would pass whatever open does.
    """
    indexed.database.unlink()
    for residue in ("atlas.db-wal", "atlas.db-shm", "atlas.db.lock"):
        (indexed.base / residue).unlink(missing_ok=True)
    foreign_payroll(indexed.database)
    return indexed


@pytest.fixture
def legacy(indexed: Harness) -> Harness:
    """A genuine pre-marker index at the current schema version."""
    indexed.unmark()
    assert marker(indexed.database) == 0
    return indexed


def assert_clean_failure(result: Result, code: int) -> None:
    assert result.exit_code == code, result
    for token in TRACEBACK_MARKERS:
        assert token not in result.output, (token, result)
    assert result.stderr.strip(), result
    assert len(result.stderr) < 2000, result


# == foreign fixtures ========================================================


def foreign_payroll(path: Path) -> None:
    """The review's reproduction, exactly."""
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE payroll(id INTEGER PRIMARY KEY, employee TEXT, salary INT)")
    connection.execute("INSERT INTO payroll(employee, salary) VALUES ('ada', 100000)")
    connection.execute("PRAGMA user_version = 0")
    connection.commit()
    connection.close()


def foreign_many(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE invoice(id INTEGER PRIMARY KEY, total INTEGER);"
        "CREATE TABLE customer(id INTEGER PRIMARY KEY, name TEXT);"
        "CREATE INDEX idx_invoice_total ON invoice(total);"
        "CREATE VIEW big AS SELECT * FROM invoice WHERE total > 100;"
        "CREATE TRIGGER audit AFTER INSERT ON invoice BEGIN "
        "  INSERT INTO customer(name) VALUES ('auto'); END;"
        "INSERT INTO invoice(total) VALUES (500);"
    )
    connection.commit()
    connection.close()


def foreign_view_only(path: Path) -> None:
    """No tables at all — `type = 'table'` counts zero, and it is still foreign."""
    connection = sqlite3.connect(path)
    connection.execute("CREATE VIEW ledger AS SELECT 1 AS x")
    connection.commit()
    connection.close()


FOREIGN: dict[str, object] = {
    "payroll": foreign_payroll,
    "many-objects": foreign_many,
    "view-only": foreign_view_only,
}


# == 1. the original reproduction ============================================


def test_init_no_longer_adopts_the_foreign_payroll_database(bare: Harness) -> None:
    """The review's case, through the real CLI, asserted on the file itself.

    Before this pass: exit 0, one object became fifty-one, ``user_version`` 0
    became 5. The payroll row survived, which was luck — the table was called
    something RepoAtlas does not use.
    """
    bare.database.parent.mkdir(parents=True, exist_ok=True)
    foreign_payroll(bare.database)
    before, before_objects = digest(bare.database), objects(bare.database)

    result = bare.initialize()

    assert_clean_failure(result, ExitCode.NOT_INITIALIZED)
    assert "is not recognized as a RepoAtlas index" in result.stderr
    assert digest(bare.database) == before
    assert objects(bare.database) == before_objects == ["payroll"]
    assert stored_version(bare.database) == 0
    assert marker(bare.database) == 0


def test_the_foreign_rows_are_still_there_and_still_only_theirs(bare: Harness) -> None:
    bare.database.parent.mkdir(parents=True, exist_ok=True)
    foreign_payroll(bare.database)
    bare.initialize()

    connection = sqlite3.connect(bare.database)
    try:
        assert connection.execute("SELECT employee, salary FROM payroll").fetchall() == [
            ("ada", 100000)
        ]
        assert [r[0] for r in connection.execute("SELECT name FROM sqlite_master")] == ["payroll"]
    finally:
        connection.close()


# == 2-3. empty containers are unowned, not foreign ==========================


def test_an_absent_file_is_created_and_marked(bare: Harness) -> None:
    assert not bare.database.exists()
    assert bare.initialize().exit_code == ExitCode.OK
    assert marker(bare.database) == APPLICATION_ID
    assert stored_version(bare.database) == SCHEMA_VERSION


def test_a_zero_byte_file_is_still_an_empty_index(bare: Harness) -> None:
    """Pass 7 measured that SQLite reads zero bytes as a new database, and this
    pass deliberately keeps that: a zero-byte file holds no foreign SQLite state,
    so there is no owner to displace."""
    bare.database.parent.mkdir(parents=True, exist_ok=True)
    bare.database.write_bytes(b"")
    assert bare.initialize().exit_code == ExitCode.OK
    assert bare.run("status").exit_code == ExitCode.OK
    assert marker(bare.database) == APPLICATION_ID


def test_an_empty_valid_sqlite_database_may_be_adopted(bare: Harness) -> None:
    """A real SQLite header with no user objects is an empty container.

    Refusing it would be defensible and is not what V1 does; the choice is
    recorded here so it cannot change by accident.
    """
    bare.database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(bare.database)
    connection.execute("VACUUM")
    connection.close()
    assert bare.database.stat().st_size > 0

    assert bare.initialize().exit_code == ExitCode.OK
    assert marker(bare.database) == APPLICATION_ID


def test_a_database_holding_only_sqlite_sequence_is_empty(bare: Harness) -> None:
    """§28, measured rather than guessed: dropping an ``AUTOINCREMENT`` table
    leaves ``sqlite_sequence`` behind with no rows in it. It is SQLite's own
    bookkeeping, not an application's state — and RepoAtlas's schema creates it
    anyway — so it does not make a database foreign."""
    bare.database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(bare.database)
    connection.execute("CREATE TABLE t(id INTEGER PRIMARY KEY AUTOINCREMENT)")
    connection.execute("INSERT INTO t DEFAULT VALUES")
    connection.execute("DROP TABLE t")
    connection.commit()
    assert [r[0] for r in connection.execute("SELECT name FROM sqlite_master")] == [
        "sqlite_sequence"
    ]
    assert connection.execute("SELECT count(*) FROM sqlite_sequence").fetchone()[0] == 0
    connection.close()

    assert bare.initialize().exit_code == ExitCode.OK
    assert marker(bare.database) == APPLICATION_ID


def test_user_objects_excludes_sqlite_internals_and_temp_objects(tmp_path: Path) -> None:
    """§27, §29: what "empty" means, pinned at the level it is decided."""
    path = tmp_path / "u.db"
    connection = connect(path)
    try:
        assert user_objects(connection) == ()
        connection.execute("CREATE TEMP TABLE scratch(x)")
        assert user_objects(connection) == (), "a connection's temp objects are not the file's"
        connection.execute("CREATE VIEW v AS SELECT 1")
        assert user_objects(connection) == ("v",), "a view with no table is still a user object"
        connection.execute("CREATE TABLE t(id INTEGER PRIMARY KEY AUTOINCREMENT)")
        connection.execute("INSERT INTO t DEFAULT VALUES")
        assert "sqlite_sequence" not in user_objects(connection)
    finally:
        connection.close()


# == 4-9. foreign databases are refused, unchanged ===========================


@pytest.mark.parametrize("shape", sorted(FOREIGN))
def test_every_foreign_shape_is_refused_without_mutation(bare: Harness, shape: str) -> None:
    bare.database.parent.mkdir(parents=True, exist_ok=True)
    FOREIGN[shape](bare.database)  # type: ignore[operator]
    before, before_objects = digest(bare.database), objects(bare.database)

    assert_clean_failure(bare.initialize(), ExitCode.NOT_INITIALIZED)

    assert digest(bare.database) == before
    assert objects(bare.database) == before_objects
    assert marker(bare.database) == 0


@pytest.mark.parametrize("version", [0, 3, SCHEMA_VERSION, 99])
def test_a_foreign_database_is_refused_at_every_user_version(bare: Harness, version: int) -> None:
    """``user_version`` is a schema version, never proof of ownership.

    A foreign file stamped with RepoAtlas's own current version is the sharpest
    form of the point: the number matches exactly, and it establishes nothing.
    """
    bare.database.parent.mkdir(parents=True, exist_ok=True)
    foreign_payroll(bare.database)
    connection = sqlite3.connect(bare.database)
    connection.execute(f"PRAGMA user_version = {version}")
    connection.commit()
    connection.close()
    before = digest(bare.database)

    assert_clean_failure(bare.initialize(), ExitCode.NOT_INITIALIZED)
    assert digest(bare.database) == before
    assert marker(bare.database) == 0


def test_another_applications_marker_is_never_overwritten(bare: Harness) -> None:
    """§15. RepoAtlas refuses a file that names a format, even an unknown one."""
    bare.database.parent.mkdir(parents=True, exist_ok=True)
    other = 0x4D595342  # 'MYSB' — some other application's registered value
    connection = sqlite3.connect(bare.database)
    connection.execute("CREATE TABLE t(x)")
    connection.execute(f"PRAGMA application_id = {other}")
    connection.commit()
    connection.close()
    before = digest(bare.database)

    result = bare.initialize()

    assert_clean_failure(result, ExitCode.NOT_INITIALIZED)
    assert "belongs to another application" in result.stderr
    assert digest(bare.database) == before
    assert marker(bare.database) == other


def test_the_refusal_does_not_name_what_is_in_the_database(bare: Harness) -> None:
    """§26. The diagnostic identifies the *path*, never the contents.

    A message that helpfully listed the tables would leak the schema of whatever
    the operator actually pointed at.
    """
    bare.database.parent.mkdir(parents=True, exist_ok=True)
    foreign_payroll(bare.database)
    result = bare.initialize()

    assert "payroll" not in result.output
    assert "ada" not in result.output
    assert str(bare.database) in result.stderr
    assert "REPOATLAS_DB" in result.stderr


def test_the_refusal_does_not_call_a_valid_database_corrupt(bare: Harness) -> None:
    """§8. It is perfectly good SQLite; only its owner is wrong."""
    bare.database.parent.mkdir(parents=True, exist_ok=True)
    foreign_payroll(bare.database)
    result = bare.initialize()

    assert "corrupt" not in result.output.lower()
    assert "malformed" not in result.output.lower()
    assert "delete" not in result.output.lower(), "never instruct deletion by default"


def test_the_domain_error_is_typed_not_a_raw_sqlite_exception(tmp_path: Path) -> None:
    """§9. Callers above storage see an index condition with its exit code set."""
    path = tmp_path / "foreign.db"
    foreign_payroll(path)

    with pytest.raises(IndexNotRecognizedError) as caught:
        open_database(path)

    assert isinstance(caught.value, IndexSchemaError)
    assert isinstance(caught.value, IndexUnavailableError)
    assert caught.value.exit_code == ExitCode.NOT_INITIALIZED


def test_another_applications_error_is_a_narrower_type(tmp_path: Path) -> None:
    path = tmp_path / "other.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE t(x)")
    connection.execute("PRAGMA application_id = 1128416343")
    connection.commit()
    connection.close()

    with pytest.raises(IndexForeignApplicationError) as caught:
        open_database(path)
    assert isinstance(caught.value, IndexNotRecognizedError)


# == 11-12. lookalikes must not be blessed ===================================


def near_repoatlas(path: Path, variant: str) -> None:
    """§34: schemas built to look like RepoAtlas's and fail on one detail."""
    if variant == "repositories-only":
        connection = connect(path)
        connection.execute("CREATE TABLE repositories(id INTEGER PRIMARY KEY, nickname TEXT)")
        connection.commit()
        connection.close()
        return
    if variant == "files-and-edges-wrong-shape":
        connection = connect(path)
        connection.executescript(
            "CREATE TABLE files(id INTEGER PRIMARY KEY, name TEXT);"
            "CREATE TABLE edges(id INTEGER PRIMARY KEY, a INTEGER, b INTEGER);"
            "CREATE TABLE payroll(x);"
        )
        connection.commit()
        connection.close()
        return
    if variant == "v5-plus-one-column":
        connection = historical(path, SCHEMA_VERSION)
        connection.execute("ALTER TABLE files ADD COLUMN extra TEXT")
        connection.commit()
        connection.close()
        return
    if variant == "v5-minus-one-index":
        connection = historical(path, SCHEMA_VERSION)
        connection.execute("DROP INDEX idx_files_hash")
        connection.commit()
        connection.close()
        return
    if variant == "v4-shape-stamped-v5":
        connection = historical(path, 4)
        connection.execute("PRAGMA user_version = 5")
        connection.commit()
        connection.close()
        return
    if variant == "v5-shape-stamped-v4":
        connection = historical(path, SCHEMA_VERSION)
        connection.execute("PRAGMA user_version = 4")
        connection.commit()
        connection.close()
        return
    raise AssertionError(variant)  # pragma: no cover


LOOKALIKES = (
    "repositories-only",
    "files-and-edges-wrong-shape",
    "v5-plus-one-column",
    "v5-minus-one-index",
    "v4-shape-stamped-v5",
)


@pytest.mark.parametrize("variant", LOOKALIKES)
def test_a_near_repoatlas_schema_never_gains_identity(tmp_path: Path, variant: str) -> None:
    """The marker must never be used to bless an unproven database.

    Each of these shares names, and several share the exact ``user_version``,
    with a genuine index. Recognition compares the whole shape, so one wrong
    column, one missing index or one version that never produced that schema is
    enough to refuse — and refusing must leave the file untouched.
    """
    path = tmp_path / f"{variant}.db"
    near_repoatlas(path, variant)
    before, before_objects = digest(path), objects(path)

    with pytest.raises(IndexSchemaError):
        open_database(path)

    assert digest(path) == before, "a refused database was modified"
    assert objects(path) == before_objects
    assert marker(path) == 0, "identity was written to an unproven database"


def test_a_colliding_table_name_is_not_ownership_evidence(bare: Harness) -> None:
    """The inverse of the original defect. Adoption used to turn on whether names
    collided; now a database that *does* use the name ``repositories`` is refused
    just the same, because the name was never the evidence."""
    bare.database.parent.mkdir(parents=True, exist_ok=True)
    near_repoatlas(bare.database, "repositories-only")
    before = digest(bare.database)

    assert_clean_failure(bare.initialize(), ExitCode.NOT_INITIALIZED)
    assert digest(bare.database) == before


# == 13-18. genuine historical databases are recognized ======================


@pytest.mark.parametrize("version", [1, 2, 3, 4, SCHEMA_VERSION])
def test_a_genuine_historical_database_is_marked_and_migrated(tmp_path: Path, version: int) -> None:
    """§11: the one safe adoption path. Recognized by shape, then marked.

    Every RepoAtlas index that exists predates the marker, so this is the case a
    careless fix breaks. Data has to survive it, which is why a row goes in
    first.
    """
    path = tmp_path / f"v{version}.db"
    connection = historical(path, version)
    connection.execute(
        "INSERT INTO repositories (name, path, branch, head_sha) "
        "VALUES ('svc', '/repos/svc', 'main', 'abc123')"
    )
    connection.commit()
    connection.close()
    assert marker(path) == 0

    database = open_database(path)
    try:
        assert user_version(database.connection) == SCHEMA_VERSION
        assert application_id(database.connection) == APPLICATION_ID
        row = database.connection.execute("SELECT name, path FROM repositories").fetchone()
        assert (row["name"], row["path"]) == ("svc", "/repos/svc")
    finally:
        database.close()


def test_a_recognized_partial_migration_is_recovered_and_marked(tmp_path: Path) -> None:
    """§18/§46.18: the shape a database damaged by the pre-atomicity implementation
    has — part of migration 005 committed, ``user_version`` still 4. Pass 2
    recovers it; this pass must recognize it as genuinely RepoAtlas rather than
    call it an unknown schema."""
    path = tmp_path / "partial.db"
    connection = historical(path, 4)
    pending = next(m for m in load_migrations() if m.version == 5)
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(pending.statements[0])
    connection.commit()
    assert user_version(connection) == 4
    assert recognizes(connection, 4, load_migrations()) is True
    connection.close()

    database = open_database(path)
    try:
        assert user_version(database.connection) == SCHEMA_VERSION
        assert application_id(database.connection) == APPLICATION_ID
    finally:
        database.close()


def test_recognition_is_shape_not_table_names(tmp_path: Path) -> None:
    """The rule stated directly at the function that implements it."""
    genuine = connect(tmp_path / "genuine.db")
    lookalike = connect(tmp_path / "lookalike.db")
    try:
        for migration in load_migrations():
            for statement in migration.statements:
                genuine.execute(statement)
            genuine.execute(f"PRAGMA user_version = {migration.version}")
        genuine.commit()
        lookalike.execute("CREATE TABLE repositories(id INTEGER PRIMARY KEY, nickname TEXT)")
        lookalike.execute("CREATE TABLE files(id INTEGER PRIMARY KEY)")
        lookalike.commit()

        migrations = load_migrations()
        assert recognizes(genuine, SCHEMA_VERSION, migrations) is True
        assert recognizes(lookalike, 0, migrations) is False
        assert recognizes(lookalike, SCHEMA_VERSION, migrations) is False
        # A v5 shape stamped 4 is not a mismatch: it is exactly what a crash
        # between the last statement of 005 and its version stamp leaves behind,
        # and Pass 2 recovers it. Recognition has to include it or that database
        # becomes unopenable.
        assert recognizes(genuine, 4, migrations) is True
        # The other direction has no such reading. Nothing produces version 5
        # over a v4 schema, because the stamp lands last.
        assert recognizes(genuine, 3, migrations) is False
        assert recognizes(genuine, 99, migrations) is False
    finally:
        genuine.close()
        lookalike.close()


def test_a_marked_current_database_takes_the_fast_path(indexed: Harness) -> None:
    """§14: a trusted marker settles ownership; no fingerprinting on the hot path."""
    connection = connect(indexed.database)
    try:
        assert application_id(connection) == APPLICATION_ID
        assert (
            establish(connection, indexed.database, schema_version=SCHEMA_VERSION)
            is Ownership.MARKED
        )
    finally:
        connection.close()


def test_ownership_outcomes_are_what_each_fixture_deserves(tmp_path: Path) -> None:
    """The §37 decision matrix, at the function that decides it."""
    cases: list[tuple[str, object, Ownership | type[BaseException]]] = [
        ("zero-byte", lambda p: p.write_bytes(b""), Ownership.EMPTY),
        ("empty-sqlite", lambda p: connect(p).close(), Ownership.EMPTY),
        ("foreign", foreign_payroll, IndexNotRecognizedError),
        ("view-only", foreign_view_only, IndexNotRecognizedError),
        ("legacy-v4", lambda p: historical(p, 4).close(), Ownership.LEGACY),
        ("legacy-current", lambda p: historical(p, SCHEMA_VERSION).close(), Ownership.LEGACY),
    ]
    for name, build, expected in cases:
        path = tmp_path / f"{name}.db"
        build(path)  # type: ignore[operator]
        connection = connect(path)
        try:
            if isinstance(expected, Ownership):
                got = establish(connection, path, schema_version=SCHEMA_VERSION)
                assert got is expected, (name, got)
            else:
                with pytest.raises(expected):
                    establish(connection, path, schema_version=SCHEMA_VERSION)
        finally:
            connection.close()


# == 20-21. a marker proves ownership, not schema correctness ================


def test_a_marked_future_database_keeps_the_pass_7_refusal(tmp_path: Path) -> None:
    """§17: identity established, then the version refused — in that order."""
    path = tmp_path / "future.db"
    connection = historical(path, SCHEMA_VERSION)
    connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 7}")
    connection.commit()
    connection.close()
    before = digest(path)

    with pytest.raises(IndexSchemaError) as caught:
        open_database(path)

    assert "newer version of RepoAtlas" in caught.value.render()
    assert digest(path) == before


def test_an_unmarked_future_database_is_unrecognized_not_trusted(tmp_path: Path) -> None:
    """§17's harder half: a version number alone never proved RepoAtlas wrote it.

    Refused as unrecognized rather than as "a newer RepoAtlas", because that
    claim cannot be made about a file carrying no identity — and refusing is what
    keeps the marker off an unproven database.
    """
    path = tmp_path / "future-unmarked.db"
    connection = historical(path, SCHEMA_VERSION)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 7}")
    connection.commit()
    connection.close()
    before = digest(path)

    with pytest.raises(IndexNotRecognizedError):
        open_database(path)

    assert digest(path) == before
    assert marker(path) == 0


def test_a_marked_database_with_an_impossible_schema_is_refused(tmp_path: Path) -> None:
    """§16: the marker is not a licence to migrate whatever is there.

    ``user_version = 0`` with user objects present is a state no RepoAtlas
    migration can produce — 001 creates the schema and stamps 1 in one
    transaction — so a marked file in that state is refused rather than migrated
    onto someone else's tables.
    """
    path = tmp_path / "marked-garbage.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE payroll(id INTEGER PRIMARY KEY, salary INTEGER)")
    connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    connection.commit()
    connection.close()
    before, before_objects = digest(path), objects(path)

    with pytest.raises(IndexSchemaError) as caught:
        open_database(path)

    assert "does not recognize" in caught.value.message
    assert digest(path) == before
    assert objects(path) == before_objects, "no RepoAtlas table was created"


def test_a_marked_database_below_current_with_a_wrong_schema_is_refused(tmp_path: Path) -> None:
    """The same rule where it costs something: a marked database that a migration
    is about to be applied to has its shape checked first, because the
    alternative is migrating a schema RepoAtlas cannot account for."""
    path = tmp_path / "marked-wrong-v4.db"
    connection = historical(path, 4)
    connection.execute("DROP TABLE chunks")
    connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    connection.commit()
    connection.close()
    before = digest(path)

    with pytest.raises(IndexSchemaError):
        open_database(path)
    assert digest(path) == before


def test_a_marked_database_missing_a_table_still_reaches_exit_3(indexed: Harness) -> None:
    """§16, §46.21: the marker settles ownership, and Pass 7 still catches the
    schema where it is used. Nothing is repaired."""
    connection = sqlite3.connect(indexed.database)
    connection.execute("DROP TABLE repositories")
    connection.commit()
    connection.close()

    result = indexed.run("status")
    assert_clean_failure(result, ExitCode.NOT_INITIALIZED)
    assert "missing part of its schema" in result.stderr
    assert "repositories" not in objects(indexed.database), "the table was silently recreated"
    assert marker(indexed.database) == APPLICATION_ID, "ownership was revoked by a schema fault"


# == 22-25. the legacy identity upgrade policy ===============================


def test_status_does_not_write_the_marker(legacy: Harness) -> None:
    """§44, and the reason the policy exists.

    ``status`` answers "did anything change?". A ``status`` that rewrote the
    database in order to answer would make its own answer untrue, and Pass 10's
    read-only guarantee is not negotiable for a metadata convenience.
    """
    before = digest(legacy.database)
    before_siblings = siblings(legacy.database)

    for command in (("status",), ("status", "--json"), ("repos",), ("locate", "SERVICE_HOST")):
        result = legacy.run(*command)
        assert result.exit_code == ExitCode.OK, result
        legacy.settle()
        assert digest(legacy.database) == before, command
        assert marker(legacy.database) == 0, command

    assert siblings(legacy.database) == before_siblings


def test_sync_upgrades_the_identity_of_a_legacy_index(legacy: Harness) -> None:
    """§43 option B, the other half: write paths upgrade."""
    assert marker(legacy.database) == 0
    assert legacy.run("sync").exit_code == ExitCode.OK
    assert marker(legacy.database) == APPLICATION_ID
    assert stored_version(legacy.database) == SCHEMA_VERSION
    assert legacy.run("status").exit_code == ExitCode.OK


def test_the_marker_upgrade_changes_nothing_else(legacy: Harness) -> None:
    """§13: ownership metadata only. Facts and schema version are untouched."""

    queries = {
        "repositories": "SELECT name, slug, path FROM repositories ORDER BY slug",
        "files": "SELECT rel_path, content_hash FROM files ORDER BY rel_path",
        "entities": "SELECT type, key FROM entities ORDER BY type, key",
        "edges": "SELECT type, confidence FROM edges ORDER BY type, confidence",
        "meta": "SELECT key, value FROM meta ORDER BY key",
    }

    def facts() -> dict[str, list[tuple[object, ...]]]:
        connection = sqlite3.connect(legacy.database)
        try:
            return {
                name: [tuple(row) for row in connection.execute(sql)]
                for name, sql in queries.items()
            }
        finally:
            connection.close()

    before = facts()
    assert legacy.run("sync").exit_code == ExitCode.OK
    after = facts()

    assert marker(legacy.database) == APPLICATION_ID
    assert stored_version(legacy.database) == SCHEMA_VERSION
    for name in queries:
        assert after[name] == before[name], name
    assert before["files"], "the fixture proved nothing: it had no facts to preserve"


def test_a_read_only_legacy_index_stays_readable_and_is_never_called_foreign(
    legacy: Harness,
) -> None:
    """§41, §42. Recognition happens in memory; the write is a separate act.

    Design order matters here and nowhere else so visibly: an architecture that
    stamped identity at open would tell an operator their own historical index —
    which they merely made read-only — belongs to somebody else.
    """
    before = digest(legacy.database)
    legacy.database.chmod(0o400)
    try:
        for command in (("status",), ("repos",), ("locate", "SERVICE_HOST")):
            result = legacy.run(*command)
            assert result.exit_code == ExitCode.OK, result
            assert "not recognized" not in result.output
            assert digest(legacy.database) == before

        writer = legacy.run("sync")
        assert writer.exit_code == ExitCode.NOT_INITIALIZED
        assert "cannot write to the index" in writer.stderr
        assert "not recognized" not in writer.output, "a read-only index is not a foreign one"
        assert digest(legacy.database) == before
    finally:
        legacy.database.chmod(0o600)


def test_a_read_only_historical_index_is_recognized_not_refused(tmp_path: Path) -> None:
    """§41 for an *old* schema: recognition must not depend on being able to write.

    The writer fails because the file is read-only — the truthful reason — and
    not because RepoAtlas decided it was foreign.
    """
    path = tmp_path / "old-ro.db"
    historical(path, 3).close()
    before = digest(path)
    path.chmod(0o400)
    try:
        with pytest.raises(IndexUnavailableError) as caught:
            open_database(path)
        assert not isinstance(caught.value, IndexNotRecognizedError)
        assert isinstance(caught.value, IndexUnavailableError)
        assert digest(path) == before
    finally:
        path.chmod(0o600)


# == 26-27. every command, and init above all ================================


@pytest.mark.parametrize("command", COMMANDS, ids=lambda c: c[0])
def test_every_command_refuses_a_foreign_database(foreign: Harness, command) -> None:
    """§23. Pass 7 centralized unusable-index behaviour at one boundary; ownership
    rides the same boundary, so no command can be the one that forgets."""
    before = digest(foreign.database)

    result = foreign.run(*command)

    assert_clean_failure(result, ExitCode.NOT_INITIALIZED)
    assert "is not recognized as a RepoAtlas index" in result.stderr
    assert digest(foreign.database) == before
    assert marker(foreign.database) == 0


@pytest.mark.parametrize("command", COMMANDS, ids=lambda c: c[0])
def test_no_command_leaves_wal_or_shm_behind_on_a_foreign_database(
    foreign: Harness, command
) -> None:
    """§21, §30, §40. Merely opening SQLite can create sibling files, and the
    refusal must happen before anything that would. ``PRAGMA journal_mode = WAL``
    writes the file header — measured — so ownership is settled before it runs."""
    before = siblings(foreign.database)
    assert before == ["atlas.db"]

    foreign.run(*command)

    assert siblings(foreign.database) == before


def test_a_configured_db_path_pointing_at_the_wrong_file_says_so(indexed: Harness) -> None:
    """§26: the likeliest way to meet this defect is a mistyped ``REPOATLAS_DB``."""
    elsewhere = indexed.base / "accounts.sqlite"
    foreign_payroll(elsewhere)
    before = digest(elsewhere)

    result = indexed.run("status", REPOATLAS_DB=str(elsewhere))

    assert_clean_failure(result, ExitCode.NOT_INITIALIZED)
    assert str(elsewhere) in result.stderr
    assert digest(elsewhere) == before


# == 28. no model contact ====================================================


# == 25 (brief). the writer lock =============================================


def test_ownership_outranks_the_writer_lock(foreign: Harness) -> None:
    """§25. Error precedence, pinned so it stays consistent.

    A foreign database cannot be opened as a workspace at all, and the workspace
    is opened before the lock is taken — so the answer is exit 3 whether or not
    another process is writing. Reporting contention would tell the operator to
    wait for a sync that could never have run.
    """
    from repoatlas.sync.lock import writer_lock

    before = digest(foreign.database)
    assert foreign.run("sync").exit_code == ExitCode.NOT_INITIALIZED

    with writer_lock(foreign.database, timeout=0.0):
        contended = foreign.run("sync")
    assert contended.exit_code == ExitCode.NOT_INITIALIZED, contended
    assert "is not recognized as a RepoAtlas index" in contended.stderr
    assert digest(foreign.database) == before


def test_a_held_writer_lock_still_wins_for_a_genuine_index(indexed: Harness) -> None:
    """The control. Exit 5 must keep meaning "someone else is writing" — a fix
    that turned every contended sync into exit 3 would pass the test above."""
    from repoatlas.sync.lock import writer_lock

    with writer_lock(indexed.database, timeout=0.0):
        result = indexed.run("sync")
    assert result.exit_code == ExitCode.SYNC_LOCKED, result


# == 31-32. concurrency ======================================================


def open_concurrently(path: Path, count: int) -> list[str]:
    """Open the same database from ``count`` real, concurrent processes."""
    script = (
        "import sys, pathlib\n"
        "from repoatlas.storage.sqlite.database import open_database\n"
        "from repoatlas.storage.sqlite.migrator import application_id\n"
        "try:\n"
        "    d = open_database(pathlib.Path(sys.argv[1]))\n"
        "    print('OK', d.connection.execute('PRAGMA user_version').fetchone()[0],\n"
        "          application_id(d.connection))\n"
        "    d.close()\n"
        "except Exception as exc:\n"
        "    print('FAIL', type(exc).__name__, exc)\n"
    )
    environment = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=environment,
        )
        for _ in range(count)
    ]
    return [p.communicate()[0].strip() for p in processes]


def test_concurrent_first_opens_of_a_new_database_all_agree(tmp_path: Path) -> None:
    """§38. The hazard identity introduces: a loser that reads the marker before
    the winner's commit and the schema after it would see a database that is
    non-empty and unmarked, and could refuse a perfectly sound index. Ownership
    is therefore read from one consistent snapshot."""
    path = tmp_path / "race.db"
    results = open_concurrently(path, 6)

    assert all(r.startswith("OK") for r in results), results
    assert {r.split()[1] for r in results} == {str(SCHEMA_VERSION)}
    assert marker(path) == APPLICATION_ID


def test_concurrent_openers_of_a_legacy_database_all_upgrade_cleanly(tmp_path: Path) -> None:
    """§38's second half: identity adoption under contention."""
    path = tmp_path / "legacy-race.db"
    historical(path, 3).close()

    results = open_concurrently(path, 6)

    assert all(r.startswith("OK") for r in results), results
    assert {r.split()[1] for r in results} == {str(SCHEMA_VERSION)}
    assert {r.split()[2] for r in results} == {str(APPLICATION_ID)}
    assert marker(path) == APPLICATION_ID


def test_concurrent_syncs_against_a_legacy_index_mark_it_once(legacy: Harness) -> None:
    """The same race through the real CLI, where the marker write is a separate
    serialized transaction rather than part of a migration."""
    processes = [
        subprocess.Popen(
            [str(CLI), "sync"],
            cwd=legacy.root,
            env=legacy.environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for _ in range(4)
    ]
    codes = [(p.communicate()[0], p.returncode) for p in processes]

    assert all(code in (ExitCode.OK, ExitCode.SYNC_LOCKED) for _, code in codes), codes
    assert marker(legacy.database) == APPLICATION_ID
    assert legacy.run("status").exit_code == ExitCode.OK


# == 33-34. crash consistency ================================================


def kill_during(path: Path, script: str, sig: int) -> None:
    environment = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    victim = subprocess.Popen(
        [sys.executable, "-c", script, str(path)],
        stdout=subprocess.PIPE,
        text=True,
        env=environment,
    )
    assert victim.stdout is not None
    assert victim.stdout.readline().strip() == "READY"
    victim.send_signal(sig)
    victim.wait(timeout=30)


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL])
def test_a_kill_during_a_legacy_migration_never_leaves_a_false_claim(
    tmp_path: Path, sig: int
) -> None:
    """§18, §39. Identity and schema commit together, so a crash cannot leave a
    database that claims to be a RepoAtlas index while holding a schema no
    migration produced. Either both landed, or neither did."""
    path = tmp_path / f"crash{sig}.db"
    historical(path, 4).close()

    kill_during(
        path,
        "import sys, pathlib, sqlite3, time\n"
        "from repoatlas.storage.sqlite.migrator import load_migrations, stamp_application_id\n"
        "c = sqlite3.connect(sys.argv[1], isolation_level='DEFERRED')\n"
        "c.execute('PRAGMA journal_mode = WAL')\n"
        "m = [m for m in load_migrations() if m.version == 5][0]\n"
        "c.execute('BEGIN IMMEDIATE')\n"
        "stamp_application_id(c)\n"
        "[c.execute(s) for s in m.statements]\n"
        "print('READY', flush=True)\n"
        "time.sleep(60)\n",
        sig,
    )

    assert stored_version(path) == 4
    assert marker(path) == 0, "identity outlived the transaction that was meant to carry it"

    database = open_database(path)
    try:
        assert user_version(database.connection) == SCHEMA_VERSION
        assert application_id(database.connection) == APPLICATION_ID
        assert database.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        database.close()


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL])
def test_a_kill_during_the_marker_write_leaves_a_recognizable_index(
    tmp_path: Path, sig: int
) -> None:
    """§39 for the standalone upgrade: a legacy index already at the current
    version, killed while claiming it. Recognition does not depend on the marker,
    so the database is still recognized on the next open and marked then."""
    path = tmp_path / f"marker{sig}.db"
    historical(path, SCHEMA_VERSION).close()

    kill_during(
        path,
        "import sys, pathlib, sqlite3, time\n"
        "from repoatlas.storage.sqlite.migrator import stamp_application_id\n"
        "c = sqlite3.connect(sys.argv[1], isolation_level='DEFERRED')\n"
        "c.execute('PRAGMA journal_mode = WAL')\n"
        "c.execute('BEGIN IMMEDIATE')\n"
        "stamp_application_id(c)\n"
        "print('READY', flush=True)\n"
        "time.sleep(60)\n",
        sig,
    )

    assert marker(path) == 0
    database = open_database(path)
    try:
        assert user_version(database.connection) == SCHEMA_VERSION
        assert database.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        database.close()


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL])
def test_a_kill_during_a_fresh_bootstrap_never_leaves_a_marked_empty_file(
    tmp_path: Path, sig: int
) -> None:
    """§4: the ordering question. Marking a file and then creating the schema
    would leave a crash window in which a database claims to be a complete
    RepoAtlas index and is empty. The marker rides migration 001's transaction
    instead, so the window does not exist."""
    path = tmp_path / f"bootstrap{sig}.db"

    kill_during(
        path,
        "import sys, sqlite3, time\n"
        "from repoatlas.storage.sqlite.migrator import load_migrations, stamp_application_id\n"
        "c = sqlite3.connect(sys.argv[1], isolation_level='DEFERRED')\n"
        "c.execute('PRAGMA journal_mode = WAL')\n"
        "m = load_migrations()[0]\n"
        "c.execute('BEGIN IMMEDIATE')\n"
        "stamp_application_id(c)\n"
        "[c.execute(s) for s in m.statements]\n"
        "print('READY', flush=True)\n"
        "time.sleep(60)\n",
        sig,
    )

    assert marker(path) == 0
    assert stored_version(path) == 0
    database = open_database(path)
    try:
        assert user_version(database.connection) == SCHEMA_VERSION
        assert application_id(database.connection) == APPLICATION_ID
    finally:
        database.close()


# == 35-36. identity travels with the file, not the path =====================


def test_a_copied_marked_index_is_still_ours(indexed: Harness, tmp_path: Path) -> None:
    """§31: identity is a property of the database's origin. It survives ``VACUUM``,
    the backup API and a plain copy — measured — because it is in the header."""
    destination = tmp_path / "elsewhere" / "copied.db"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(indexed.database.read_bytes())

    assert marker(destination) == APPLICATION_ID
    result = indexed.run("repos", REPOATLAS_DB=str(destination))
    assert result.exit_code == ExitCode.OK, result


def test_a_copied_legacy_index_is_recognized_at_its_new_path(
    legacy: Harness, tmp_path: Path
) -> None:
    """§32: recognition is by schema shape, so it cannot depend on the old root."""
    destination = tmp_path / "elsewhere" / "legacy.db"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(legacy.database.read_bytes())
    assert marker(destination) == 0

    assert legacy.run("repos", REPOATLAS_DB=str(destination)).exit_code == ExitCode.OK
    assert marker(destination) == 0, "a read stamped a copy"

    assert legacy.run("sync", REPOATLAS_DB=str(destination)).exit_code == ExitCode.OK
    assert marker(destination) == APPLICATION_ID


def test_the_marker_survives_vacuum(indexed: Harness) -> None:
    connection = sqlite3.connect(indexed.database)
    try:
        connection.execute("VACUUM")
    finally:
        connection.close()
    assert marker(indexed.database) == APPLICATION_ID
    assert indexed.run("status").exit_code == ExitCode.OK


# == 37. Pass 7's controls, unchanged ========================================


def test_a_file_that_is_not_sqlite_is_still_corrupt_not_foreign(bare: Harness) -> None:
    """The distinction this pass introduces must not swallow the one Pass 7 made.

    Random bytes are not "a database belonging to someone else" — they are not a
    database, and the remedy differs.
    """
    bare.database.parent.mkdir(parents=True, exist_ok=True)
    bare.database.write_bytes(b"not sqlite\n" * 40)

    result = bare.initialize()

    assert_clean_failure(result, ExitCode.NOT_INITIALIZED)
    assert "not a SQLite database" in result.stderr
    assert "SQLITE_NOTADB" in result.stderr
    assert "not recognized as a RepoAtlas index" not in result.stderr


def test_a_malformed_image_is_still_corrupt(bare: Harness) -> None:
    bare.initialize()
    blob = bytearray(bare.database.read_bytes())
    blob[4096:8192] = b"\x00" * 4096
    bare.database.write_bytes(bytes(blob[: len(blob) // 2]))

    result = bare.run("status")
    assert_clean_failure(result, ExitCode.NOT_INITIALIZED)
    with pytest.raises(IndexCorruptError):
        open_database(bare.database)


def test_a_programmer_error_is_still_not_an_index_condition(indexed: Harness) -> None:
    """Pass 7's asymmetry, restated: exit 3 tells an operator to move their index
    aside, so a RepoAtlas bug must never reach it."""
    database = open_database(indexed.database)
    try:
        with pytest.raises(sqlite3.OperationalError):
            database.execute("SELECT * FROM a_table_repoatlas_never_creates")
    finally:
        database.close()


def test_ownership_never_becomes_a_catch_all_for_a_healthy_index(indexed: Harness) -> None:
    """The whole-file control: nothing about a normal, marked index changed."""
    for command in (("status",), ("repos",), ("sync",), ("locate", "SERVICE_HOST")):
        assert indexed.run(*command).exit_code == ExitCode.OK, command
    assert marker(indexed.database) == APPLICATION_ID
    assert stored_version(indexed.database) == SCHEMA_VERSION


# == the marker value itself =================================================


def test_the_application_id_is_inside_sqlites_signed_32_bit_field(tmp_path: Path) -> None:
    """§19. Measured: SQLite silently stores 0 for anything outside the signed
    32-bit range, so a value chosen above it would leave every RepoAtlas index
    quietly unmarked — and every one of them would then read as unowned."""
    assert 0 < APPLICATION_ID <= 2**31 - 1

    path = tmp_path / "range.db"
    connection = connect(path)
    try:
        connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        assert application_id(connection) == APPLICATION_ID
        connection.execute(f"PRAGMA application_id = {2**31}")
        assert application_id(connection) == 0, "out-of-range values are silently dropped"
    finally:
        connection.close()


def test_the_marker_is_transactional(tmp_path: Path) -> None:
    """§18, §19. The property the crash tests depend on, asserted directly."""
    from repoatlas.storage.sqlite.migrator import stamp_application_id

    path = tmp_path / "txn.db"
    connection = connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        stamp_application_id(connection)
        assert application_id(connection) == APPLICATION_ID
        connection.rollback()
        assert application_id(connection) == 0
    finally:
        connection.close()


def test_reading_ownership_does_not_touch_the_file(tmp_path: Path) -> None:
    """§21, §22. What makes the marker usable as the *first* question asked of a
    database RepoAtlas may turn out not to own."""
    path = tmp_path / "read.db"
    foreign_payroll(path)
    before, before_siblings = digest(path), siblings(path)

    connection = connect(path)
    try:
        with pytest.raises(IndexNotRecognizedError):
            establish(connection, path, schema_version=SCHEMA_VERSION)
    finally:
        connection.close()

    assert digest(path) == before
    assert siblings(path) == before_siblings
