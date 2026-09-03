"""SQLite connection management and migrations (ARCHITECTURE.md §3)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = 5


class Database:
    """Owns the connection. The only object that opens SQLite."""

    def __init__(self, connection: sqlite3.Connection, path: Path) -> None:
        self.connection = connection
        self.path = path

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Atomic unit of work. Rolls back on any exception."""
        with self.connection:
            yield self.connection

    def execute(self, sql: str, params: object = ()) -> sqlite3.Cursor:
        return self.connection.execute(sql, params)  # type: ignore[arg-type]

    def close(self) -> None:
        self.connection.close()

    def get_meta(self, key: str) -> str | None:
        row = self.connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def _migration_files() -> list[tuple[int, str]]:
    package = resources.files("repoatlas.storage.sqlite.migrations")
    found: list[tuple[int, str]] = []
    for entry in package.iterdir():
        name = entry.name
        if not name.endswith(".sql"):
            continue
        found.append((int(name.split("_", 1)[0]), entry.read_text(encoding="utf-8")))
    return sorted(found)


def _apply_migrations(connection: sqlite3.Connection) -> None:
    row = connection.execute("PRAGMA user_version").fetchone()
    current = int(row[0]) if row else 0
    for version, sql in _migration_files():
        if version <= current:
            continue
        with connection:
            connection.executescript(sql)
            connection.execute(f"PRAGMA user_version = {version}")


def open_database(path: Path) -> Database:
    """Open (creating if needed) the RepoAtlas database with migrations applied."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, isolation_level="DEFERRED")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA synchronous = NORMAL")
    _apply_migrations(connection)

    database = Database(connection, path)
    with connection:
        database.set_meta("schema_version", str(SCHEMA_VERSION))
    return database
