"""SQLite connection management and migrations (ARCHITECTURE.md §3, §9)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from repoatlas.errors import IndexSchemaError
from repoatlas.storage.sqlite.errors import opening_errors, storage_errors
from repoatlas.storage.sqlite.migrator import apply_migrations, load_migrations, user_version

SCHEMA_VERSION = 5

#: How long a statement waits for SQLite's own write lock before raising
#: ``database is locked``. This is *contention behaviour*, not the synchronization
#: protocol — that is the advisory writer lock in ``repoatlas.sync.lock``
#: (ARCHITECTURE.md §9). It exists for the cases the writer lock does not cover:
#: a WAL checkpoint, and a second process opening the database and finding
#: migrations to apply at the same moment.
BUSY_TIMEOUT_MS = 10_000


class Database:
    """Owns the connection. The only object that opens SQLite."""

    def __init__(self, connection: sqlite3.Connection, path: Path) -> None:
        self.connection = connection
        self.path = path
        self._depth = 0

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Atomic unit of work. Rolls back on any exception.

        **Reentrant, and deliberately so.** Several stores each open a transaction
        for their own small write, which is right when each is called on its own
        and wrong when a caller needs a group of them to land together — the
        global-link swap being the case that matters, where a delete and the
        insert that replaces it must commit as one (ARCHITECTURE.md §9).

        Nesting therefore *joins* the outermost block rather than committing
        early: the outermost ``with`` owns the commit, and an exception anywhere
        inside rolls the whole thing back. The consequence to know about is that a
        caller which swallows an exception raised inside an inner block leaves the
        outer transaction alive and will commit whatever came before it; nothing
        on the sync path does that, and a failure there is meant to abort the run.

        The outermost block also translates recognized storage failures, so a
        write that meets a corrupt page or a read-only file reports the index
        state rather than a ``sqlite3`` traceback (V1_SPEC §4). Rollback happens
        first — ``storage_errors`` wraps ``self.connection``, so the connection's
        own context manager has already rolled back by the time the exception is
        reclassified.
        """
        if self._depth:
            self._depth += 1
            try:
                yield self.connection
            finally:
                self._depth -= 1
            return

        self._depth = 1
        try:
            with storage_errors(self.path), self.connection:
                yield self.connection
        finally:
            self._depth = 0

    @property
    def in_transaction(self) -> bool:
        """Whether an outer transaction owns the commit. Used by tests and asserts."""
        return self._depth > 0

    def execute(self, sql: str, params: object = ()) -> sqlite3.Cursor:
        with storage_errors(self.path):
            return self.connection.execute(sql, params)  # type: ignore[arg-type]

    def close(self) -> None:
        self.connection.close()

    def get_meta(self, key: str) -> str | None:
        row = self.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def _apply_migrations(connection: sqlite3.Connection) -> None:
    """Bring the schema up to date. See :mod:`repoatlas.storage.sqlite.migrator`.

    Migrations run at *open*, which readers do too, so the sync writer lock
    cannot cover them: requiring it would make ``locate`` queue behind a running
    ``sync``. Serialization is SQLite's own write transaction instead.
    """
    apply_migrations(connection, load_migrations())


def open_database(path: Path) -> Database:
    """Open (creating if needed) the RepoAtlas database with migrations applied.

    Every failure a database can present at open is a product state here, not an
    escaping ``sqlite3`` exception: :func:`opening_errors` translates the ones
    that are recognized (V1_SPEC §4) and passes on the ones that are not.

    The version check runs **before** anything writes. ``PRAGMA journal_mode``
    changes the file header, so an index written by a newer RepoAtlas has to be
    refused while the connection is still read-only in effect — refusing after
    modifying it would be a downgrade by one pragma.
    """
    with opening_errors(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, isolation_level="DEFERRED")
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        _refuse_future_schema(connection, path)
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = NORMAL")
        _apply_migrations(connection)

    database = Database(connection, path)
    # Written only when it actually changes. Stamping it unconditionally made
    # *opening* the database a write, so `locate` and `status` competed for
    # SQLite's write lock with a running sync and could fail with "database is
    # locked" — readers must not need a writer's privileges (ARCHITECTURE.md §9).
    if database.get_meta("schema_version") != str(SCHEMA_VERSION):
        with database.transaction():
            database.set_meta("schema_version", str(SCHEMA_VERSION))
    return database


def _refuse_future_schema(connection: sqlite3.Connection, path: Path) -> None:
    """Refuse a database stamped past :data:`SCHEMA_VERSION`, changing nothing.

    Migrations only ever run forward, so a future database used to open as though
    valid: every migration was skipped, the stamp was left alone, and RepoAtlas
    then read and wrote a layout it does not know — silently, and with whatever
    the newer build had stored in it. There is no safe reading of a schema this
    build has never seen, so it is refused.
    """
    found = user_version(connection)
    if found <= SCHEMA_VERSION:
        return
    connection.close()
    raise IndexSchemaError(
        f"The RepoAtlas index at {path} was created by a newer version of RepoAtlas.",
        detail=(
            f"Its schema version is {found}; this build understands version "
            f"{SCHEMA_VERSION}.\n\n"
            "RepoAtlas does not downgrade a database and has changed nothing. "
            "Upgrade RepoAtlas, or point REPOATLAS_DB at a different index."
        ),
    )
