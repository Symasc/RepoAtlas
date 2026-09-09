"""Migration atomicity, serialization and crash recovery (ARCHITECTURE.md §3, §9).

The defect this suite exists to prevent had one cause and two faces.

``sqlite3.Connection.executescript`` **commits any open transaction before it
executes anything**. The previous implementation wrapped it in ``BEGIN
IMMEDIATE`` and stamped ``user_version`` afterwards, so it looked like one
transaction and was none: every statement committed separately.

* A process killed mid-script left part of the new schema committed with the old
  version still stamped. The next open re-ran the migration onto a schema that
  already had half of it — ``duplicate column name: slug`` — and every command
  that opens the database failed from then on.
* Two processes could both pass the version check and both start applying,
  because neither held a write transaction. Measured on the vulnerable build: 7
  of 150 concurrent opens failed, across 6 of 25 trials.

Everything here uses real SQLite, real files under the production PRAGMAs, real
historical schemas built by applying the real migrations, and real subprocesses
for concurrency and crash cases.
"""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from repoatlas.errors import IndexNotRecognizedError, IndexSchemaError, MigrationError
from repoatlas.storage.sqlite.database import SCHEMA_VERSION, open_database
from repoatlas.storage.sqlite.migrator import (
    Migration,
    apply_migrations,
    load_migrations,
    resume_point,
    schema_fingerprint,
    split_statements,
    user_version,
)

pytestmark = pytest.mark.adversarial

REPO_ROOT = Path(__file__).resolve().parents[2]


# == helpers =================================================================


def connect(path: Path) -> sqlite3.Connection:
    """A connection configured exactly as production configures one.

    Atomicity experiments run against SQLite's defaults prove nothing about
    RepoAtlas, which runs in WAL with a busy timeout and foreign keys on.
    """
    connection = sqlite3.connect(path, isolation_level="DEFERRED")
    connection.row_factory = sqlite3.Row
    for pragma in (
        "busy_timeout = 10000",
        "journal_mode = WAL",
        "foreign_keys = ON",
        "synchronous = NORMAL",
    ):
        connection.execute(f"PRAGMA {pragma}")
    return connection


def historical(path: Path, version: int) -> sqlite3.Connection:
    """A genuine database at ``version``, built by applying the real migrations.

    Deliberately not ``PRAGMA user_version = 4`` stamped onto the current schema:
    that fixture has the new columns already and would let a vacuous upgrade test
    pass while proving nothing.
    """
    connection = connect(path)
    for migration in load_migrations():
        if migration.version > version:
            break
        connection.execute("BEGIN IMMEDIATE")
        for statement in migration.statements:
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version = {migration.version}")
        connection.commit()
    return connection


def migration(version: int) -> Migration:
    return next(m for m in load_migrations() if m.version == version)


def shape(path: Path) -> dict[str, object]:
    connection = connect(path)
    try:
        columns = [r[1] for r in connection.execute("PRAGMA table_xinfo(repositories)")]
        return {
            "user_version": user_version(connection),
            "slug": "slug" in columns,
            "slug_index": bool(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name = 'idx_repositories_slug'"
                ).fetchall()
            ),
            "integrity": connection.execute("PRAGMA integrity_check").fetchone()[0],
            "foreign_key_rows": len(connection.execute("PRAGMA foreign_key_check").fetchall()),
        }
    finally:
        connection.close()


def run_openers(path: Path, count: int) -> list[str]:
    """Open the same database from ``count`` real, concurrent processes."""
    script = (
        "import sys, pathlib\n"
        "from repoatlas.storage.sqlite.database import open_database\n"
        "try:\n"
        "    d = open_database(pathlib.Path(sys.argv[1]))\n"
        "    print('OK', d.connection.execute('PRAGMA user_version').fetchone()[0])\n"
        "    d.close()\n"
        "except Exception as exc:\n"
        "    print('FAIL', type(exc).__name__, exc)\n"
    )
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        for _ in range(count)
    ]
    return [p.communicate()[0].strip() for p in processes]


class FailAt:
    """A real connection that dies at one chosen point in a migration.

    Wraps rather than subclasses because ``sqlite3.Connection`` is an immutable
    C type. ``apply_migrations`` uses only these four methods.
    """

    def __init__(self, inner: sqlite3.Connection, point: object) -> None:
        self._inner = inner
        self._point = point
        self._seen = 0

    def execute(self, sql: str, *args: Any) -> Any:
        body = sql.strip().lower()
        if body.startswith("pragma user_version ="):
            if self._point == "stamp":
                raise RuntimeError("died before the version stamp")
        elif not body.startswith(("pragma", "begin")):
            if self._seen == self._point:
                raise RuntimeError(f"died before statement {self._point}")
            self._seen += 1
        return self._inner.execute(sql, *args)

    def commit(self) -> None:
        if self._point == "commit":
            raise RuntimeError("died before COMMIT")
        self._inner.commit()

    def rollback(self) -> None:
        self._inner.rollback()


# == 1. the stdlib behaviour that caused this ================================


def test_executescript_commits_the_open_transaction(tmp_path: Path) -> None:
    """The regression that documents the root cause.

    If this ever stops being true, the elaborate machinery in `migrator` could be
    simplified — but until then, no amount of wrapping makes `executescript`
    atomic, and this test is the reason the production code does not use it.
    """
    connection = connect(tmp_path / "a.db")
    connection.execute("BEGIN IMMEDIATE")
    assert connection.in_transaction

    connection.executescript("CREATE TABLE t (x INTEGER);")
    assert not connection.in_transaction, "executescript no longer implicitly commits"

    connection.rollback()
    other = connect(tmp_path / "a.db")
    survived = other.execute("SELECT 1 FROM sqlite_master WHERE name = 't'").fetchall()
    assert survived, "the rollback could not undo what executescript committed"
    connection.close()
    other.close()


def test_the_migrator_never_calls_executescript() -> None:
    """The fix is to stop using it, not to wrap it better.

    Checked against the parsed syntax tree rather than the text, because both
    modules *document* the old call and a substring search would match the prose
    that explains why it is gone.
    """
    import ast

    for name in ("migrator.py", "database.py"):
        tree = ast.parse((REPO_ROOT / "repoatlas" / "storage" / "sqlite" / name).read_text())
        calls = [
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        ]
        assert "executescript" not in calls, name


def test_ddl_and_the_version_stamp_roll_back_together(tmp_path: Path) -> None:
    """SQLite's DDL is transactional — the fact the whole design rests on.

    Asserted rather than assumed, for the exact statement kinds the migrations
    use, under the production connection configuration.
    """
    connection = connect(tmp_path / "b.db")
    connection.execute("CREATE TABLE base (id INTEGER PRIMARY KEY, name TEXT)")
    connection.execute("PRAGMA user_version = 4")
    connection.commit()

    connection.execute("BEGIN IMMEDIATE")
    connection.execute("CREATE TABLE made (x INTEGER)")
    connection.execute("CREATE INDEX idx_base_name ON base (name)")
    connection.execute("ALTER TABLE base ADD COLUMN slug TEXT NOT NULL DEFAULT ''")
    connection.execute("CREATE TRIGGER t AFTER INSERT ON base BEGIN DELETE FROM base; END")
    connection.execute("INSERT INTO base (name) VALUES ('x')")
    connection.execute("PRAGMA user_version = 5")
    assert connection.in_transaction
    connection.rollback()

    fresh = connect(tmp_path / "b.db")
    names = {r[0] for r in fresh.execute("SELECT name FROM sqlite_master")}
    assert "made" not in names
    assert "idx_base_name" not in names
    assert "t" not in names
    assert "slug" not in [r[1] for r in fresh.execute("PRAGMA table_xinfo(base)")]
    assert fresh.execute("SELECT COUNT(*) FROM base").fetchone()[0] == 0
    assert user_version(fresh) == 4, "PRAGMA user_version must be transactional"
    connection.close()
    fresh.close()


# == 2. statement splitting ==================================================


def test_splitting_survives_trigger_bodies() -> None:
    """`001` defines triggers whose bodies contain `BEGIN ... END;`.

    A `sql.split(";")` shreds each into an invalid `CREATE TRIGGER ... BEGIN
    INSERT ...` fragment plus a stray `END`, which is why the splitter asks
    SQLite where statements end instead.
    """
    first = migration(1)
    assert any("CREATE TRIGGER" in s.upper() for s in first.statements)
    for statement in first.statements:
        if "CREATE TRIGGER" in statement.upper():
            assert statement.rstrip().upper().endswith("END;")
    raw = (
        REPO_ROOT / "repoatlas" / "storage" / "sqlite" / "migrations" / "001_initial.sql"
    ).read_text()
    naive = [s for s in raw.split(";") if s.strip()]
    assert len(naive) > len(first.statements), "naive splitting must be visibly wrong"


def test_every_split_statement_is_complete_sql() -> None:
    for entry in load_migrations():
        for statement in entry.statements:
            assert sqlite3.complete_statement(statement), (entry.version, statement[:60])


def test_splitting_ignores_semicolons_in_literals_and_comments() -> None:
    sql = """
    -- a comment; with a semicolon
    CREATE TABLE t (x TEXT DEFAULT 'a;b');
    CREATE TABLE u (y INTEGER);
    """
    assert len(split_statements(sql)) == 2


# == 3. migration success and equivalence ====================================


def test_a_fresh_database_reaches_the_current_schema(tmp_path: Path) -> None:
    database = open_database(tmp_path / "fresh.db")
    try:
        assert user_version(database.connection) == SCHEMA_VERSION
        assert database.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert database.connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        database.close()


@pytest.mark.parametrize("start", [0, 1, 2, 3, 4])
def test_a_database_any_number_of_versions_behind_upgrades(tmp_path: Path, start: int) -> None:
    path = tmp_path / f"v{start}.db"
    historical(path, start).close()
    database = open_database(path)
    try:
        assert user_version(database.connection) == SCHEMA_VERSION
    finally:
        database.close()


def test_fresh_and_upgraded_and_recovered_schemas_are_identical(tmp_path: Path) -> None:
    """§21: every route to the current schema must arrive at the same schema."""
    fresh = open_database(tmp_path / "fresh.db")
    fingerprint = schema_fingerprint(fresh.connection)
    fresh.close()

    for label, version in (("v1", 1), ("v2", 2), ("v3", 3), ("v4", 4)):
        path = tmp_path / f"{label}.db"
        historical(path, version).close()
        upgraded = open_database(path)
        assert schema_fingerprint(upgraded.connection) == fingerprint, label
        upgraded.close()

    # and the recovered-from-interruption route
    path = tmp_path / "recovered.db"
    connection = historical(path, 4)
    connection.execute("ALTER TABLE repositories ADD COLUMN slug TEXT NOT NULL DEFAULT ''")
    connection.commit()
    connection.close()
    recovered = open_database(path)
    assert schema_fingerprint(recovered.connection) == fingerprint
    recovered.close()


# == 4. failure at every point ===============================================


@pytest.mark.parametrize("point", [0, 1, 2, "stamp", "commit"])
def test_a_failure_anywhere_in_a_migration_rolls_the_whole_thing_back(
    tmp_path: Path, point: object
) -> None:
    """§8: prior schema intact, no partial effects, version unchanged, retry works."""
    path = tmp_path / f"fail{point}.db"
    connection = historical(path, 4)
    connection.execute("INSERT INTO repositories (name, path) VALUES ('common', '/a/common')")
    connection.commit()
    connection.close()

    working = connect(path)
    with pytest.raises(RuntimeError):
        apply_migrations(FailAt(working, point), [migration(5)])  # type: ignore[arg-type]
    working.close()

    assert shape(path) == {
        "user_version": 4,
        "slug": False,
        "slug_index": False,
        "integrity": "ok",
        "foreign_key_rows": 0,
    }

    # the data is still there, and the retry succeeds
    database = open_database(path)
    try:
        assert user_version(database.connection) == SCHEMA_VERSION
        assert database.connection.execute("SELECT COUNT(*) FROM repositories").fetchone()[0] == 1
    finally:
        database.close()


def test_a_statement_failure_is_a_domain_error_not_a_raw_sqlite_error(tmp_path: Path) -> None:
    path = tmp_path / "conflict.db"
    connection = historical(path, 4)
    # Something else already owns the name the migration's index needs.
    connection.execute("CREATE TABLE idx_repositories_slug (x INTEGER)")
    connection.commit()

    try:
        with pytest.raises(MigrationError) as caught:
            apply_migrations(connection, load_migrations())
        assert "migration 005" in str(caught.value).lower()
    finally:
        connection.close()
    assert shape(path)["user_version"] == 4

    # Through `open_database` this schema no longer reaches the migrator at all:
    # a database carrying no RepoAtlas identity, whose shape is not one the
    # committed migrations produce, is refused as unrecognized *before* anything
    # writes. Both refusals are exit 3, and the earlier one is strictly stronger
    # — the migrator's fires after `PRAGMA journal_mode = WAL` has already
    # modified the header (ARCHITECTURE.md §3).
    with pytest.raises(IndexSchemaError):
        open_database(path).close()
    assert shape(path)["user_version"] == 4


# == 5. recovery of databases the vulnerable build damaged ===================


@pytest.mark.parametrize("version", [1, 2, 3, 4, 5])
def test_every_interruption_point_of_every_migration_recovers(tmp_path: Path, version: int) -> None:
    """§18: the vulnerable mechanism affected *all* migrations, not only 005.

    For each migration, every committed prefix is built and then opened. Each
    must reach the current schema with integrity intact.
    """
    entry = migration(version)
    for cut in range(len(entry.statements) + 1):
        path = tmp_path / f"m{version}_c{cut}.db"
        connection = historical(path, version - 1)
        for statement in entry.statements[:cut]:
            connection.execute(statement)
        connection.commit()
        connection.close()

        database = open_database(path)
        try:
            assert user_version(database.connection) == SCHEMA_VERSION, (version, cut)
            assert database.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok", (
                version,
                cut,
            )
            assert database.connection.execute("PRAGMA foreign_key_check").fetchall() == []
        finally:
            database.close()


def test_a_fully_applied_but_unstamped_migration_is_not_reapplied(tmp_path: Path) -> None:
    """§16: the whole shape is verified, not one column.

    The resume point lands on 005's trailing `DELETE` rather than past it, and
    that is the design rather than an off-by-one: a data-only statement leaves no
    schema trace, so no shape comparison can distinguish "before" it from
    "after" it. Recovery therefore replays it, which is safe exactly because it
    is idempotent — the property every statement with no detectable effect must
    have. The DDL before it *is* recognized as applied and is not repeated, which
    is what would otherwise raise `duplicate column name: slug`.
    """
    path = tmp_path / "unstamped.db"
    connection = historical(path, 4)
    for statement in migration(5).statements:
        connection.execute(statement)
    connection.commit()

    statements = migration(5).statements
    start = resume_point(connection, migration(5), load_migrations())
    assert start == len(statements) - 1
    assert "DELETE" in statements[start].upper(), "only the data-only tail may be replayed"
    connection.close()

    database = open_database(path)
    try:
        assert user_version(database.connection) == SCHEMA_VERSION
        columns = [r[1] for r in database.connection.execute("PRAGMA table_xinfo(repositories)")]
        assert columns.count("slug") == 1
    finally:
        database.close()


def test_every_statement_with_no_detectable_effect_is_idempotent() -> None:
    """The obligation the recovery design creates, checked against every migration.

    A statement whose effect leaves no schema trace may be replayed during
    recovery. Today that is one `DELETE` with a `WHERE` clause. If a future
    migration adds an `INSERT` or an `UPDATE`, this fails and the author has to
    decide what replay means, rather than discovering it in production.
    """
    from repoatlas.storage.sqlite.migrator import OPAQUE, _body, statement_state

    connection = sqlite3.connect(":memory:")
    try:
        for entry in load_migrations():
            for statement in entry.statements:
                if statement_state(connection, statement) != OPAQUE:
                    continue
                verb = _body(statement).split()[0].upper()
                assert verb == "DELETE", (entry.version, verb)
    finally:
        connection.close()


def test_an_unrecognized_schema_is_refused_rather_than_forced(tmp_path: Path) -> None:
    """§17: a schema that merely overlaps a migration is not one that ran."""
    path = tmp_path / "strange.db"
    connection = historical(path, 4)
    # The column exists but with the wrong type, and the index is absent: no
    # interrupted migration produces this.
    connection.execute("ALTER TABLE repositories ADD COLUMN slug INTEGER NOT NULL DEFAULT 0")
    connection.commit()

    try:
        with pytest.raises(MigrationError):
            apply_migrations(connection, load_migrations())
    finally:
        connection.close()
    assert shape(path)["user_version"] == 4, "a refused migration must change nothing"

    with pytest.raises(IndexNotRecognizedError):
        open_database(path).close()
    assert shape(path)["user_version"] == 4


def test_an_index_with_a_different_definition_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "wrongindex.db"
    connection = historical(path, 4)
    connection.execute("ALTER TABLE repositories ADD COLUMN slug TEXT NOT NULL DEFAULT ''")
    connection.execute("CREATE INDEX idx_repositories_slug ON repositories (name)")
    connection.commit()

    try:
        with pytest.raises(MigrationError):
            apply_migrations(connection, load_migrations())
    finally:
        connection.close()
    assert shape(path)["user_version"] == 4

    with pytest.raises(IndexNotRecognizedError):
        open_database(path).close()
    assert shape(path)["user_version"] == 4


# == 6. concurrency ==========================================================


@pytest.mark.parametrize("count", [2, 6])
def test_concurrent_openers_of_a_fresh_database_all_succeed(tmp_path: Path, count: int) -> None:
    path = tmp_path / f"fresh{count}.db"
    results = run_openers(path, count)
    assert all(r.startswith("OK") for r in results), results
    assert all(r.split()[1] == str(SCHEMA_VERSION) for r in results), results
    assert shape(path)["integrity"] == "ok"


@pytest.mark.parametrize("count", [2, 6])
def test_concurrent_openers_of_an_old_database_all_succeed(tmp_path: Path, count: int) -> None:
    """The case the review reproduced: every process sees a valid old schema
    first, so every process believes it must migrate."""
    path = tmp_path / f"v4_{count}.db"
    historical(path, 4).close()
    results = run_openers(path, count)
    assert all(r.startswith("OK") for r in results), results
    assert all(r.split()[1] == str(SCHEMA_VERSION) for r in results), results
    state = shape(path)
    assert state["user_version"] == SCHEMA_VERSION
    assert state["integrity"] == "ok"
    assert state["foreign_key_rows"] == 0


def test_exactly_one_process_applies_the_migration(tmp_path: Path) -> None:
    """§6: the loser re-reads the version inside its own write transaction and
    finds the work already done, rather than repeating it."""
    path = tmp_path / "once.db"
    connection = historical(path, 4)
    for _ in range(3):
        connection.execute(
            "INSERT INTO entities (type, key, name) VALUES ('repository', ?, 'r')", (f"repo:{_}",)
        )
    connection.commit()
    connection.close()

    results = run_openers(path, 6)
    assert all(r.startswith("OK") for r in results), results
    check = connect(path)
    try:
        # 005 deletes global repository entities. Applied twice it would still be
        # zero, so the durable evidence of single application is the schema: one
        # slug column and one index, which a second application could not add.
        columns = [r[1] for r in check.execute("PRAGMA table_xinfo(repositories)")]
        assert columns.count("slug") == 1
        indexes = check.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'idx_repositories_slug'"
        ).fetchone()[0]
        assert indexes == 1
    finally:
        check.close()


def test_a_waiting_opener_observes_the_finished_migration(tmp_path: Path) -> None:
    """§13/§23: the second process waits, then sees the new schema."""
    path = tmp_path / "wait.db"
    historical(path, 4).close()

    holder = connect(path)
    holder.execute("BEGIN IMMEDIATE")
    for statement in migration(5).statements:
        holder.execute(statement)

    script = (
        "import sys, pathlib, time\n"
        "from repoatlas.storage.sqlite.database import open_database\n"
        "d = open_database(pathlib.Path(sys.argv[1]))\n"
        "print('OK', d.connection.execute('PRAGMA user_version').fetchone()[0])\n"
        "d.close()\n"
    )
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    waiter = subprocess.Popen(
        [sys.executable, "-c", script, str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    time.sleep(0.4)
    assert waiter.poll() is None, "the second opener should still be waiting"

    holder.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    holder.commit()
    holder.close()

    out = waiter.communicate(timeout=30)[0].strip()
    assert out == f"OK {SCHEMA_VERSION}", out


def test_a_reader_of_a_current_database_is_not_blocked_by_a_writer(tmp_path: Path) -> None:
    """Migrations must not be serialized behind the sync writer lock: readers
    open the database too, and `locate` must not queue behind a `sync`."""
    path = tmp_path / "current.db"
    open_database(path).close()

    writer = connect(path)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO meta (key, value) VALUES ('held', '1')")

    started = time.perf_counter()
    results = run_openers(path, 1)
    elapsed = time.perf_counter() - started
    writer.rollback()
    writer.close()

    assert results[0].startswith("OK"), results
    assert elapsed < 5.0, f"an up-to-date open waited {elapsed:.1f}s behind a writer"


# == 7. process death ========================================================


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL])
def test_a_killed_migration_leaves_the_previous_schema_usable(tmp_path: Path, sig: int) -> None:
    """§25: SQLite's own crash recovery does this. No custom cleanup exists, and
    none should — a stale-lock file would be one more thing to get wrong."""
    path = tmp_path / f"kill{sig}.db"
    historical(path, 4).close()

    script = (
        "import sys, pathlib, sqlite3, time\n"
        "from repoatlas.storage.sqlite.migrator import load_migrations\n"
        "c = sqlite3.connect(sys.argv[1], isolation_level='DEFERRED')\n"
        "c.execute('PRAGMA journal_mode = WAL')\n"
        "m = [m for m in load_migrations() if m.version == 5][0]\n"
        "c.execute('BEGIN IMMEDIATE')\n"
        "[c.execute(s) for s in m.statements]\n"
        "print('READY', flush=True)\n"
        "time.sleep(60)\n"
    )
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    victim = subprocess.Popen(
        [sys.executable, "-c", script, str(path)],
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert victim.stdout is not None
    assert victim.stdout.readline().strip() == "READY"
    victim.send_signal(sig)
    victim.wait(timeout=30)

    state = shape(path)
    assert state["user_version"] == 4
    assert state["slug"] is False
    assert state["integrity"] == "ok"
    assert state["foreign_key_rows"] == 0

    database = open_database(path)
    try:
        assert user_version(database.connection) == SCHEMA_VERSION
    finally:
        database.close()


# == 8. data =================================================================


def test_representative_indexed_state_survives_the_upgrade(tmp_path: Path) -> None:
    """§22: migrations are not only schemas."""
    path = tmp_path / "data.db"
    connection = historical(path, 4)
    connection.execute(
        "INSERT INTO repositories (name, path, branch, head_sha) "
        "VALUES ('common', '/teamA/common', 'main', 'abc')"
    )
    connection.execute(
        "INSERT INTO repositories (name, path, branch, head_sha) "
        "VALUES ('common', '/teamB/common', 'main', 'def')"
    )
    connection.execute(
        "INSERT INTO files (repo_id, rel_path, size, mtime_ns, content_hash, language) "
        "VALUES (1, 'src/app.py', 100, 1, 'h1', 'python')"
    )
    connection.execute(
        "INSERT INTO entities (type, key, repo_id, file_id, name) "
        "VALUES ('symbol', 'sym:common:app.py:F:1', 1, 1, 'F')"
    )
    connection.execute(
        "INSERT INTO entities (type, key, name) VALUES ('env', 'env:SERVICE_HOST', 'SERVICE_HOST')"
    )
    connection.execute(
        "INSERT INTO dependencies (repo_id, file_id, ecosystem, name, normalized_name, role) "
        "VALUES (1, 1, 'pypi', 'Requests', 'requests', 'declared')"
    )
    connection.execute(
        "INSERT INTO chunks (file_id, repo_id, line_start, line_end, text) "
        "VALUES (1, 1, 1, 10, 'SERVICE_HOST = 1')"
    )
    connection.execute("INSERT INTO meta (key, value) VALUES ('probe', 'kept')")
    connection.commit()
    connection.close()

    database = open_database(path)
    try:
        execute = database.connection.execute
        assert execute("SELECT COUNT(*) FROM repositories").fetchone()[0] == 2
        assert execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
        assert execute("SELECT COUNT(*) FROM dependencies").fetchone()[0] == 1
        assert execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 1
        assert execute("SELECT value FROM meta WHERE key = 'probe'").fetchone()[0] == "kept"
        # The symbol and env entities survive; only the global repository nodes
        # are dropped, which is what migration 005 exists to do.
        kinds = {r[0] for r in execute("SELECT DISTINCT type FROM entities")}
        assert kinds == {"symbol", "env"}
        assert execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        database.close()


def test_the_transaction_is_held_for_every_statement(tmp_path: Path) -> None:
    """§28: no statement in the body may commit implicitly.

    Checked at every step rather than only at the end, because that is exactly
    how the old implementation passed inspection: `BEGIN IMMEDIATE` opened a
    transaction, `executescript` silently closed it, and the final state looked
    plausible. One observation before `COMMIT` cannot tell those apart.
    """
    path = tmp_path / "held.db"
    historical(path, 4).close()

    connection = connect(path)
    observed: list[bool] = []

    class Watch:
        """Observes only *inside* the transaction body.

        The version check that precedes `BEGIN` is legitimately outside it, so
        watching from the first `execute` would fail on correct code.
        """

        def __init__(self, inner: sqlite3.Connection) -> None:
            self._inner = inner
            self._open = False

        def execute(self, sql: str, *args: Any) -> Any:
            result = self._inner.execute(sql, *args)
            if sql.strip().lower().startswith("begin"):
                self._open = True
            elif self._open:
                observed.append(self._inner.in_transaction)
            return result

        def commit(self) -> None:
            observed.append(self._inner.in_transaction)
            self._open = False
            self._inner.commit()

        def rollback(self) -> None:
            self._open = False
            self._inner.rollback()

    apply_migrations(Watch(connection), [migration(5)])  # type: ignore[arg-type]
    connection.close()

    assert observed, "nothing was observed"
    assert all(observed), (
        "the transaction was closed part-way through the migration: "
        f"{observed.index(False)} of {len(observed)} observations"
    )
    assert shape(path)["user_version"] == SCHEMA_VERSION


def test_twelve_concurrent_openers_of_an_old_database_all_succeed(tmp_path: Path) -> None:
    """§10: more contenders than cores, on the case that actually raced."""
    path = tmp_path / "v4_12.db"
    historical(path, 4).close()
    results = run_openers(path, 12)
    assert all(r.startswith("OK") for r in results), results
    assert {r.split()[1] for r in results} == {str(SCHEMA_VERSION)}
    state = shape(path)
    assert state["integrity"] == "ok"
    assert state["foreign_key_rows"] == 0
