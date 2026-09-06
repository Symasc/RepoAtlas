"""SQLite connection management and migrations (ARCHITECTURE.md §3, §9)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from repoatlas.errors import IndexSchemaError
from repoatlas.storage.sqlite.errors import opening_errors, storage_errors
from repoatlas.storage.sqlite.identity import Ownership, establish
from repoatlas.storage.sqlite.migrator import (
    APPLICATION_ID,
    application_id,
    apply_migrations,
    load_migrations,
    stamp_application_id,
    user_version,
)

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

    def __init__(
        self, connection: sqlite3.Connection, path: Path, *, unmarked: bool = False
    ) -> None:
        self.connection = connection
        self.path = path
        self._depth = 0
        #: A pre-marker RepoAtlas index, recognized by schema shape and not yet
        #: carrying the identity marker — see :meth:`_claim`.
        self._unmarked = unmarked

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
            self._claim()
            with storage_errors(self.path), self.connection:
                yield self.connection
        finally:
            self._depth = 0

    def _claim(self) -> None:
        """Mark a recognized pre-marker index, at the first write and not before.

        A legacy index already at the current schema version has no migration to
        ride along with, so the marker has to be written by something else. That
        something must not be a *read*: ``status`` exists to report whether the
        workspace changed, and a command that rewrites the database in order to
        answer that is no longer answering it (V1_SPEC §12). The same rule keeps a
        read-only historical index readable instead of failing on a metadata write
        nobody asked for.

        So the write happens exactly where a write was already happening — the
        first application transaction, which only ``sync``, ``init`` and the
        linker open. It is serialized with ``BEGIN IMMEDIATE`` like a migration,
        rather than joining the caller's transaction, because it is not part of
        the caller's work: it is a standalone, idempotent assertion about a
        database whose identity was already proven at open, and it stays true
        whether or not the work that follows commits.

        On a read-only database this raises, and truthfully — the command that got
        here was going to write regardless (ARCHITECTURE.md §3).
        """
        if not self._unmarked:
            return
        self._unmarked = False
        with storage_errors(self.path):
            self.connection.execute("BEGIN IMMEDIATE")
            stamp_application_id(self.connection)
            self.connection.commit()

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

    **Ownership is established before the version check, and both before anything
    writes.** ``PRAGMA journal_mode`` changes the file header — measured — so a
    database RepoAtlas does not own, and one written by a newer RepoAtlas, both
    have to be refused while the connection has still only read. Refusing
    afterwards would mean the refusal itself had modified the file.
    """
    with opening_errors(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, isolation_level="DEFERRED")
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        ownership = _establish_ownership(connection, path)
        _refuse_future_schema(connection, path)
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = NORMAL")
        _apply_migrations(connection)
        # Whether the marker still has to be written is asked of the database
        # rather than inferred from `ownership`: a legacy index below the current
        # version was marked by the migration that just ran, one already at the
        # current version had no migration to ride along with, and reading the
        # header is exact where reasoning about which case happened would not be.
        unmarked = ownership is Ownership.LEGACY and application_id(connection) != APPLICATION_ID

    database = Database(connection, path, unmarked=unmarked)
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


def _establish_ownership(connection: sqlite3.Connection, path: Path) -> Ownership:
    """Refuse a database RepoAtlas cannot prove it owns, changing nothing.

    See :mod:`repoatlas.storage.sqlite.identity`. The connection is closed before
    the refusal escapes, so a caller that catches it leaves no handle open on a
    file RepoAtlas has just declined to touch.
    """
    try:
        return establish(connection, path, schema_version=SCHEMA_VERSION)
    except BaseException:
        connection.close()
        raise
