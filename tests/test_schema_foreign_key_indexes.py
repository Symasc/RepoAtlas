"""Every foreign-key child column has an index, so deleting a parent row is a seek, not a scan.

With foreign keys enforced, deleting a parent row makes SQLite look up each child table by the
child column. Without an index that lookup scans the whole child table once per deleted row; a
sync that reparsed N entities scanned ``unresolved_refs`` N times (measured 0.26 s per entity at
1.57M rows). Migration 008 added the missing indexes, and the first test below makes the next
table with a foreign key say whether it needs one. Nothing here measures time.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace
from repoatlas.storage.sqlite.migrator import load_migrations, user_version
from tests.impact_corpus import build

#: Foreign keys whose child column is deliberately unindexed.
ALLOWED = {
    # Parent `repositories`: deleted only by repository removal, so one scan per repository.
    ("edges", ("repo_id",)),
    ("unresolved_refs", ("repo_id",)),
}

#: Migration 008's indexes, which the no-reparse test removes and expects back.
EIGHT = (
    "idx_unresolved_src",
    "idx_link_decisions_src",
    "idx_link_decisions_file_id",
    "idx_http_call_decisions_src",
    "idx_http_call_decisions_file_id",
)

_WITHOUT_ROWID = re.compile(r"\bWITHOUT\s+ROWID\b", re.IGNORECASE)


def unindexed_foreign_keys(
    conn: sqlite3.Connection,
) -> tuple[set[tuple[str, tuple[str, ...]]], int]:
    """The foreign keys with no index leading on their child columns, and how many were checked.

    A foreign key of n child columns is indexed when some non-partial index, or the table's own
    rowid alias, has those columns as its first n key columns in any order: the lookup a delete
    makes is an equality on all of them.
    """
    missing: set[tuple[str, tuple[str, ...]]] = set()
    checked = 0
    tables = conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'").fetchall()
    for name, sql in tables:
        declared = str(sql or "")
        if str(name).startswith("sqlite_") or declared.lstrip().upper().startswith(
            "CREATE VIRTUAL TABLE"
        ):
            continue
        keys: dict[int, list[tuple[int, str]]] = {}
        for row in conn.execute(f'PRAGMA foreign_key_list("{name}")'):
            keys.setdefault(int(row[0]), []).append((int(row[1]), str(row[3])))
        if not keys:
            continue

        candidates: list[list[str]] = []
        for index in conn.execute(f'PRAGMA index_list("{name}")').fetchall():
            if index[4]:  # partial: it does not hold every row, so it cannot answer the lookup
                continue
            keyed = conn.execute(f'PRAGMA index_info("{index[1]}")').fetchall()
            candidates.append([str(c[2]) for c in sorted(keyed, key=lambda c: c[0])])
        primary = [row for row in conn.execute(f'PRAGMA table_info("{name}")') if row[5]]
        if (
            len(primary) == 1
            and str(primary[0][2]).upper() == "INTEGER"
            and not _WITHOUT_ROWID.search(declared)
        ):
            candidates.append([str(primary[0][1])])

        for parts in keys.values():
            child = tuple(column for _, column in sorted(parts))
            checked += 1
            width = len(child)
            if not any(set(leading[:width]) == set(child) for leading in candidates):
                missing.add((str(name), child))
    return missing, checked


def schema_at_head() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    for migration in load_migrations():
        for statement in migration.statements:
            conn.execute(statement)
    return conn


def test_every_foreign_key_child_column_is_indexed() -> None:
    conn = schema_at_head()
    try:
        missing, checked = unindexed_foreign_keys(conn)
        columns = {row[3] for row in conn.execute("PRAGMA foreign_key_list(unresolved_refs)")}
    finally:
        conn.close()

    # Equality, so an allowance that no longer applies fails here too.
    assert missing == ALLOWED
    # 23 foreign keys at schema 8 (19 in 001, 3 in 002, 1 in 004): a floor that fails if the
    # checker stops seeing them.
    assert checked >= 23
    assert "src_id" in columns, "the column behind the slow delete was not even checked"
    assert ("unresolved_refs", ("src_id",)) not in missing


def check(*statements: str) -> tuple[set[tuple[str, tuple[str, ...]]], int]:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        for statement in statements:
            conn.execute(statement)
        return unindexed_foreign_keys(conn)
    finally:
        conn.close()


PARENT = "CREATE TABLE p (id INTEGER PRIMARY KEY)"
CHILD = "CREATE TABLE c (id INTEGER PRIMARY KEY, p_id INTEGER REFERENCES p (id))"
PAIR_PARENT = "CREATE TABLE q (a INTEGER, b INTEGER, PRIMARY KEY (a, b))"
PAIR_CHILD = (
    "CREATE TABLE d (id INTEGER PRIMARY KEY, a INTEGER, b INTEGER, z INTEGER, "
    "FOREIGN KEY (a, b) REFERENCES q (a, b))"
)


def test_the_checker_finds_what_it_should() -> None:
    # A foreign key with no index is reported, and counted.
    assert check(PARENT, CHILD) == ({("c", ("p_id",))}, 1)
    # A plain index on the child column answers the lookup.
    assert check(PARENT, CHILD, "CREATE INDEX c_p ON c (p_id)") == (set(), 1)
    # A partial index does not hold every row, so it does not.
    partial = "CREATE INDEX c_p ON c (p_id) WHERE p_id IS NOT NULL"
    assert check(PARENT, CHILD, partial) == ({("c", ("p_id",))}, 1)
    # A composite key is answered by an index that leads with its columns in any order.
    reordered = "CREATE INDEX d_ba ON d (b, a, z)"
    assert check(PAIR_PARENT, PAIR_CHILD, reordered) == (set(), 1)
    # An index on only part of the key does not.
    leading_only = "CREATE INDEX d_a ON d (a)"
    assert check(PAIR_PARENT, PAIR_CHILD, leading_only) == ({("d", ("a", "b"))}, 1)
    # An index that reaches the key's columns only after another column does not either.
    behind = "CREATE INDEX d_za ON d (z, a, b)"
    assert check(PAIR_PARENT, PAIR_CHILD, behind) == ({("d", ("a", "b"))}, 1)
    # A foreign key on the INTEGER PRIMARY KEY is the rowid itself: a seek already.
    keyed = "CREATE TABLE k (id INTEGER PRIMARY KEY REFERENCES p (id))"
    assert check(PARENT, keyed) == (set(), 1)
    # Virtual tables and their shadow tables are skipped without error.
    virtual = "CREATE VIRTUAL TABLE v USING fts5(x)"
    assert check(PARENT, CHILD, "CREATE INDEX c_p ON c (p_id)", virtual) == (set(), 1)


def test_migrating_to_008_reparses_nothing(
    workspace: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The migration is index-only: an index at version 7 gains the indexes and keeps every file
    row, its parser version and content hash included, so the next sync reads no file."""
    monkeypatch.setenv("REPOATLAS_SYNC_WORKERS", "1")
    build(
        workspace,
        tmp_path,
        {
            "demo": {
                "pkg/__init__.py": "",
                "pkg/app.py": "def run() -> int:\n    return 1\n",
                "web/util.ts": "export function util(): number {\n  return 1;\n}\n",
                "pyproject.toml": '[project]\nname = "demo"\nversion = "1.0"\n',
                "notes.md": "# notes\n",
            }
        },
    )
    root = tmp_path / "repos"
    settings = workspace.settings

    def files_snapshot(ws: Workspace) -> list[tuple[object, ...]]:
        cursor = ws.database.execute("SELECT * FROM files ORDER BY id")
        skipped = [i for i, column in enumerate(cursor.description) if column[0] == "indexed_at"]
        return [
            tuple(value for i, value in enumerate(row) if i not in skipped)
            for row in cursor.fetchall()
        ]

    def present(ws: Workspace) -> set[str]:
        found = ws.database.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        return {row[0] for row in found}

    assert set(EIGHT) <= present(workspace)
    connection = workspace.database.connection
    for name in EIGHT:
        connection.execute(f"DROP INDEX {name}")
    connection.execute("PRAGMA user_version = 7")
    connection.commit()
    before = files_snapshot(workspace)
    assert len(before) >= 5
    workspace.close()

    reopened = open_workspace(settings=settings, require_root=False)
    try:
        assert user_version(reopened.database.connection) == 8
        assert set(EIGHT) <= present(reopened)
        assert files_snapshot(reopened) == before

        report = sync(reopened, reopened.resolver().resolve(root))
        for repository in report.repositories:
            assert (repository.changed, repository.new, repository.deleted) == (0, 0, 0), report
        assert files_snapshot(reopened) == before
    finally:
        reopened.close()
