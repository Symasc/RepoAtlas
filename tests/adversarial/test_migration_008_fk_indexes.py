"""Migration 008 against the databases it will meet: fresh, hand-repaired, lookalike and broken.

Migration 008 is index-only, and an operator may already have created exactly its five
indexes by hand on a large real index (the text they were given is frozen below as
``MANUAL_REPAIR``, not derived from the migration file). The migrator's recovery rule is exact
and ordered, so what has to hold is narrow in both directions: a database carrying precisely that
repair is recognized and migrated by applying nothing, and a database carrying anything else under
those names or shapes is refused with the file untouched. A failing 008 must leave the version-7
database exactly as it was.

Everything is a real SQLite file built by applying the real migrations.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
from repoatlas.errors import IndexSchemaError, MigrationFailedError
from repoatlas.storage.sqlite import database as database_module
from repoatlas.storage.sqlite.database import open_database
from repoatlas.storage.sqlite.migrator import (
    Migration,
    _normalize,
    _scratch_at,
    load_migrations,
    schema_objects,
    stamp_application_id,
    user_version,
)

pytestmark = pytest.mark.adversarial

#: The statements the operator was given, without the trailing semicolon. Frozen: the migration
#: has to equal this text, so a database carrying it is recognized as migrated.
MANUAL_REPAIR: tuple[str, ...] = (
    "CREATE INDEX idx_unresolved_src ON unresolved_refs (src_id)",
    "CREATE INDEX idx_link_decisions_src ON link_decisions (src_id)",
    "CREATE INDEX idx_link_decisions_file_id ON link_decisions (file_id)",
    "CREATE INDEX idx_http_call_decisions_src ON http_call_decisions (src_id)",
    "CREATE INDEX idx_http_call_decisions_file_id ON http_call_decisions (file_id)",
)
REPAIR_NAMES = (
    "idx_unresolved_src",
    "idx_link_decisions_src",
    "idx_link_decisions_file_id",
    "idx_http_call_decisions_src",
    "idx_http_call_decisions_file_id",
)
INDEXED_TABLES = ("unresolved_refs", "link_decisions", "http_call_decisions")

#: Every module that reads the migration list at call time (``migration_probe._TARGETS``).
_LOADERS = (
    "repoatlas.storage.sqlite.database.load_migrations",
    "repoatlas.storage.sqlite.identity.load_migrations",
    "repoatlas.storage.sqlite.migrator.load_migrations",
)


# == harness =================================================================


def connect(path: Path) -> sqlite3.Connection:
    """A connection configured exactly as production configures one."""
    connection = sqlite3.connect(path, isolation_level="DEFERRED")
    connection.row_factory = sqlite3.Row
    for pragma in ("busy_timeout = 10000", "foreign_keys = ON", "synchronous = NORMAL"):
        connection.execute(f"PRAGMA {pragma}")
    return connection


def v7_marked(path: Path) -> sqlite3.Connection:
    """A genuine, marked RepoAtlas index at schema version 7, in WAL as a real one rests.

    Built by applying the real migrations 1 to 7 statement by statement, each in its own
    transaction with its version stamp, then claiming the file the way a current build would.
    """
    connection = connect(path)
    connection.execute("PRAGMA journal_mode = WAL")
    for migration in load_migrations():
        if migration.version > 7:
            break
        connection.execute("BEGIN IMMEDIATE")
        for statement in migration.statements:
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version = {migration.version}")
        connection.commit()
    stamp_application_id(connection)
    connection.commit()
    assert user_version(connection) == 7
    return connection


def apply(connection: sqlite3.Connection, statements: tuple[str, ...]) -> None:
    connection.execute("BEGIN IMMEDIATE")
    for statement in statements:
        connection.execute(statement)
    connection.commit()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def siblings(path: Path) -> list[str]:
    return sorted(f.name for f in path.parent.iterdir() if f.name.startswith(path.name))


def read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def stored_version(path: Path) -> int:
    connection = read_only(path)
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()


def index_names(path: Path) -> set[str]:
    connection = read_only(path)
    try:
        return {str(r[0]) for r in connection.execute("SELECT name FROM sqlite_master")}
    finally:
        connection.close()


def repair_rows(path: Path) -> list[tuple[str, str, int, str]]:
    """The five repair indexes as the database stores them, page numbers included."""
    connection = read_only(path)
    try:
        found = connection.execute(
            "SELECT name, tbl_name, rootpage, sql FROM sqlite_master "
            "WHERE type = 'index' AND name IN (?, ?, ?, ?, ?) ORDER BY name",
            REPAIR_NAMES,
        )
        return [(str(r[0]), str(r[1]), int(r[2]), str(r[3])) for r in found]
    finally:
        connection.close()


def index_count(connection: sqlite3.Connection) -> int:
    """Indexes on the three tables migration 008 touches, autoindexes included."""
    return int(
        connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'index' AND tbl_name IN (?, ?, ?)",
            INDEXED_TABLES,
        ).fetchone()[0]
    )


def migration_008() -> Migration:
    return next(m for m in load_migrations() if m.version == 8)


def assert_opens_at_8_unchanged(path: Path, repair: tuple[str, ...]) -> None:
    connection = v7_marked(path)
    apply(connection, repair)
    connection.close()
    before = repair_rows(path)
    assert len(before) == 5

    database = open_database(path)
    try:
        assert user_version(database.connection) == 8
        found = database.connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND name IN (?, ?, ?, ?, ?)",
            REPAIR_NAMES,
        ).fetchall()
        assert sorted(r["name"] for r in found) == sorted(REPAIR_NAMES), "each exists exactly once"
        wanted = dict(zip(REPAIR_NAMES, MANUAL_REPAIR, strict=True))
        for row in found:
            assert _normalize(row["sql"]) == _normalize(wanted[row["name"]])
        scratch = _scratch_at(8, load_migrations())
        try:
            assert index_count(database.connection) == index_count(scratch)
        finally:
            scratch.close()
    finally:
        database.close()

    assert repair_rows(path) == before, "migration 008 rebuilt an index it should have recognized"


# == the migration is the repair =============================================


def test_008_is_the_frozen_manual_repair() -> None:
    """The text the operator typed and the migration are the same statements, in that order."""
    assert [_normalize(s) for s in migration_008().statements] == [
        _normalize(s) for s in MANUAL_REPAIR
    ]
    for migration in load_migrations():
        for statement in migration.statements:
            code = _normalize(statement)
            assert "if not exists" not in code, (migration.name, statement)


# == a hand-repaired version 7 database is recognized, nothing else is =======


def test_a_manually_repaired_v7_database_opens_at_8_unchanged(tmp_path: Path) -> None:
    assert_opens_at_8_unchanged(tmp_path / "repaired.db", MANUAL_REPAIR)


def refused_unchanged(path: Path) -> None:
    before, before_siblings = digest(path), siblings(path)

    with pytest.raises(IndexSchemaError):
        open_database(path)

    assert digest(path) == before, "a refused database was modified"
    assert siblings(path) == before_siblings
    assert stored_version(path) == 7


def test_the_repair_created_in_another_order_is_refused_unchanged(tmp_path: Path) -> None:
    """Recognition compares each table's index list in creation order (`schema_fingerprint`), so
    the manual repair is recognized only when created in the migration's order; any other order
    is refused unchanged. The remedy is to DROP the hand-made indexes and reopen."""
    path = tmp_path / "reversed.db"
    connection = v7_marked(path)
    apply(connection, tuple(reversed(MANUAL_REPAIR)))
    connection.close()

    refused_unchanged(path)


def test_a_same_named_index_with_another_definition_is_refused_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "other-definition.db"
    connection = v7_marked(path)
    apply(connection, ("CREATE INDEX idx_unresolved_src ON unresolved_refs (src_id, id)",))
    connection.close()

    refused_unchanged(path)


def test_the_same_definition_under_another_name_is_refused_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "other-name.db"
    connection = v7_marked(path)
    apply(
        connection,
        ("CREATE INDEX idx_unresolved_refs_src_id ON unresolved_refs (src_id)", *MANUAL_REPAIR[1:]),
    )
    connection.close()

    refused_unchanged(path)


def test_a_non_leading_subset_is_refused_unchanged(tmp_path: Path) -> None:
    """Only a prefix of the migration is a state it recognizes (the migrator's ordered rule), so
    the last index alone is refused rather than completed. The fail-closed contract, on record."""
    path = tmp_path / "non-leading.db"
    connection = v7_marked(path)
    apply(connection, (MANUAL_REPAIR[4],))
    connection.close()

    refused_unchanged(path)


# == a migration that fails changes nothing ==================================


def test_a_failing_008_rolls_back_entirely(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Classifying the failure asks for the schema's object names, built from the migration list
    # and cached for the process. Built here from the real chain, before it is patched, so the
    # broken statement below is judged as a defect and not as a missing part of the schema.
    schema_objects()

    path = tmp_path / "failing.db"
    v7_marked(path).close()
    real = load_migrations()
    broken = Migration(
        8, (*migration_008().statements, "CREATE INDEX idx_broken ON no_such_table (x)")
    )
    chain = [*(m for m in real if m.version <= 7), broken]
    for target in _LOADERS:
        monkeypatch.setattr(target, lambda: list(chain))

    with pytest.raises(MigrationFailedError):
        open_database(path)

    assert stored_version(path) == 7
    assert not set(REPAIR_NAMES) & index_names(path), "part of a failed migration survived"
    assert "idx_broken" not in index_names(path)


def test_the_old_build_shape_with_manual_indexes_is_still_usable_at_v7(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The interim state: a build that stops at schema 7 opening an index carrying the repair."""
    chain = [m for m in load_migrations() if m.version <= 7]
    for target in _LOADERS:
        monkeypatch.setattr(target, lambda: list(chain))
    monkeypatch.setattr(database_module, "SCHEMA_VERSION", 7)

    path = tmp_path / "old-build.db"
    connection = v7_marked(path)
    apply(connection, MANUAL_REPAIR)
    connection.close()

    database = open_database(path)
    try:
        assert user_version(database.connection) == 7
    finally:
        database.close()
    assert stored_version(path) == 7
    assert len(repair_rows(path)) == 5
