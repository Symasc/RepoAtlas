"""SQLite connection management and migrations (ARCHITECTURE.md §3, §9)."""

from __future__ import annotations

import random
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from repoatlas.errors import (
    IndexInaccessibleError,
    IndexSchemaError,
    MigrationContentionError,
)
from repoatlas.storage.sqlite.errors import is_contention, opening_errors, storage_errors
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

#: The journal mode the architecture assumes: readers and one writer concurrently,
#: which is what lets `locate` run against a database a `sync` is writing
#: (ARCHITECTURE.md §9). RepoAtlas does not operate a database in any other mode.
WAL = "wal"

#: Backoff bounds for the one operation `busy_timeout` cannot cover — see
#: :func:`_ensure_wal`. The first retry is immediate enough to catch the common
#: case (a transition that is over in under a millisecond) and the cap keeps a
#: long wait from turning into a spin. Each delay is drawn uniformly from
#: ``[0, delay]`` so that N processes released by the same commit do not all
#: return at the same instant and collide again.
_RETRY_MIN_S = 0.001
_RETRY_MAX_S = 0.050


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

        It is the last piece of *initialization* left after an open, and so it is
        the last place a lock held by another process could reach the terminal as
        a ``sqlite3`` traceback: an index written before the marker existed
        already carries ``meta.schema_version``, so :func:`open_database` writes
        nothing and the claim happens here instead, on the next command that
        writes. :func:`_contention_reported` gives it the same diagnostic the
        rest of the open path has.
        """
        if not self._unmarked:
            return
        self._unmarked = False
        with _contention_reported(self.path), storage_errors(self.path):
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

    **Several processes may do this at once, and that is expected to work.** Two
    terminals running a first command is ordinary, so an opener that finds
    another one mid-initialization waits for it rather than failing: see
    :func:`_ensure_wal` for the one operation SQLite's ``busy_timeout`` does not
    cover, and :func:`_contention_reported` for what happens when the wait runs
    out. Nothing here takes the workspace writer lock — read commands open the
    database too, and making them queue behind a running ``sync`` is the
    behaviour ARCHITECTURE.md §9 exists to prevent.
    """
    with _contention_reported(path), opening_errors(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, isolation_level="DEFERRED")
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        ownership = _establish_ownership(connection, path)
        with _closed_on_failure(connection):
            _refuse_future_schema(connection, path)
            _ensure_wal(connection, path)
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA synchronous = NORMAL")
            _apply_migrations(connection)
            # Whether the marker still has to be written is asked of the database
            # rather than inferred from `ownership`: a legacy index below the
            # current version was marked by the migration that just ran, one
            # already at the current version had no migration to ride along with,
            # and reading the header is exact where reasoning about which case
            # happened would not be.
            unmarked = (
                ownership is Ownership.LEGACY and application_id(connection) != APPLICATION_ID
            )

            database = Database(connection, path, unmarked=unmarked)
            # Written only when it actually changes. Stamping it unconditionally
            # made *opening* the database a write, so `locate` and `status`
            # competed for SQLite's write lock with a running sync and could fail
            # with "database is locked" — readers must not need a writer's
            # privileges (ARCHITECTURE.md §9). The condition is why an ordinary
            # open of a healthy index reaches none of this.
            if database.get_meta("schema_version") != str(SCHEMA_VERSION):
                with database.transaction():
                    database.set_meta("schema_version", str(SCHEMA_VERSION))
    return database


@contextmanager
def _contention_reported(path: Path) -> Iterator[None]:
    """Report a lock held past the timeout as a product state, not a traceback.

    The boundary that makes "another process is busy with this database" a thing
    RepoAtlas *says* rather than a ``sqlite3`` traceback. It covers opening only,
    which is the phase where a second process is expected: two terminals running
    a first command, an editor plugin and a shell, a wrapper script and a human.

    Narrow in two directions, both deliberate:

    * **Only contention.** :func:`is_contention` matches ``SQLITE_BUSY`` and
      ``SQLITE_LOCKED`` on their result codes. Everything else — a read-only
      file, a missing directory, a corrupt page, an I/O fault, a full disk —
      passes straight through to :func:`opening_errors`, which has an accurate
      thing to say about each. Catching ``OperationalError`` wholesale would
      report all of them as "another process is initializing the database".
    * **Only opening.** Contention outside this window is not this condition and
      keeps its own meaning; ``classify`` still returns ``None`` for these codes,
      so nothing anywhere reports a locked database as an *unusable* one.

    Most of what could raise inside an open is already handled closer to the
    failure — :func:`_ensure_wal` retries, and the migrator raises its own
    :class:`MigrationContentionError` naming the migration. What is left is the
    writes: the ``schema_version`` stamp on a database that has never carried
    one, and the identity marker a pre-marker index is claimed with, which
    :meth:`Database._claim` takes this same boundary for because it happens on
    the first command that writes rather than at open. Both wait out the busy
    timeout first, so reaching here means the other process has held the write
    lock for the full interval.
    """
    try:
        yield
    except sqlite3.Error as error:
        if not is_contention(error):
            raise
        raise _initialization_contention(path) from error


@contextmanager
def _closed_on_failure(connection: sqlite3.Connection) -> Iterator[None]:
    """Close the connection if opening does not complete.

    Every exit from :func:`open_database` other than a returned
    :class:`Database` leaves no SQLite handle behind — the rule
    :func:`_establish_ownership` already applied to the refusals, extended to the
    rest of the sequence now that contention can end it too. A retried open that
    kept its predecessor's handle would hold a read lock on the very file the
    next attempt is waiting to write.
    """
    try:
        yield
    except BaseException:
        connection.close()
        raise


def _ensure_wal(connection: sqlite3.Connection, path: Path) -> None:
    """Put the database into WAL, waiting out another process that is doing it.

    **The one operation ``busy_timeout`` does not protect.** Measured, on one
    database with ``busy_timeout = 10000`` on both connections:

    ===============================================  ==========  ==============
    operation                                        elapsed     outcome
    ===============================================  ==========  ==============
    ``BEGIN IMMEDIATE`` vs a held RESERVED lock       10.016 s    SQLITE_BUSY
    ``PRAGMA journal_mode=WAL`` vs a held SHARED      10.011 s    SQLITE_BUSY
    ``PRAGMA journal_mode=WAL`` vs a held RESERVED     0.000 s    SQLITE_BUSY
    ``PRAGMA journal_mode=WAL`` when already WAL       0.000 s    ok
    ===============================================  ==========  ==============

    The third row is the defect. Changing the journal mode has to rewrite the
    file header, so SQLite takes its *own* read transaction and then promotes it
    to a write — and SQLite never invokes a busy handler for a read-to-write
    promotion, because a process already holding a read lock that blocks waiting
    for a write lock is how two processes deadlock. So the pragma returns
    ``SQLITE_BUSY`` immediately and the configured ten seconds are never
    consulted. The promotion is inside the pragma; the Python connection is in
    autocommit throughout, which is why ending the ownership snapshot earlier
    would not have helped, and why the pragma cannot simply be moved.

    Two things follow, and both are in the loop below.

    **Ask before writing.** Reading ``PRAGMA journal_mode`` takes no lock beyond
    a shared one — measured at 0.11 ms against a held RESERVED lock — and the
    fourth row shows the transition is free once it has happened. So an ordinary
    open of a healthy index now does a read instead of a header write, and never
    joins the race at all. Only the *first* open of a database in any other mode
    contends, which is the population the retry has to cover.

    **Retry the promotion, and only the promotion.** This is the smallest
    operation that can be repeated safely: it is idempotent, it writes nothing
    but the header's version bytes, and a re-read at the top of each attempt
    means the loser of the race usually observes the winner's finished transition
    rather than repeating it. Bounded by the same deadline
    (:data:`BUSY_TIMEOUT_MS`) SQLite would have applied itself, on a monotonic
    clock, so a system clock change cannot extend or truncate the wait.

    Everything that is not contention is re-raised for the storage classifier:
    ``SQLITE_READONLY`` on a read-only index is instant and accurate today
    (measured), and waiting ten seconds to say the same thing would be strictly
    worse.
    """
    deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000
    delay = _RETRY_MIN_S
    while True:
        if _journal_mode(connection) == WAL:
            return
        try:
            row = connection.execute(f"PRAGMA journal_mode = {WAL}").fetchone()
        except sqlite3.Error as error:
            if not is_contention(error):
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _initialization_contention(path) from error
            # S311: spreading racing openers apart, not generating a secret.
            time.sleep(min(random.uniform(0, delay), remaining))  # noqa: S311
            delay = min(delay * 2, _RETRY_MAX_S)
            continue
        # The pragma reports the mode it *ended in*, which is not always the one
        # asked for: WAL needs shared memory, and a filesystem that cannot provide
        # it leaves the database in its previous mode with no error raised. The
        # architecture assumes WAL, so a database that would not take it is
        # refused rather than operated in a mode nothing above here expects.
        reached = "" if row is None else str(row[0]).lower()
        if reached != WAL:
            raise _wal_unsupported(path, reached)
        return


def _wal_unsupported(path: Path, reached: str) -> IndexInaccessibleError:
    return IndexInaccessibleError(
        f"The RepoAtlas index at {path} cannot be placed in WAL mode.",
        detail=(
            f"SQLite left the database in {reached!r} journal mode instead. WAL "
            "needs shared memory, which some network and virtual filesystems do "
            "not provide.\n\n"
            "RepoAtlas requires WAL and has not written to the database. Move the "
            "index onto a local filesystem, or point REPOATLAS_DB at one."
        ),
    )


def _initialization_contention(path: Path) -> MigrationContentionError:
    """The bounded wait ran out. Transient, and says so.

    :class:`MigrationContentionError` rather than a new class beside it: its
    meaning is already "another process held SQLite's write transaction past the
    busy timeout", which is exactly this, and the process being waited for is an
    opener initializing or upgrading the same database. A second contention type
    at the same exit code would be taxonomy without a distinction.
    """
    return MigrationContentionError(
        "Timed out waiting for another process to finish initializing the database.",
        detail=(
            f"Another RepoAtlas process is preparing the index at {path} and held "
            f"SQLite's write lock for longer than {BUSY_TIMEOUT_MS / 1000:.0f} "
            "seconds.\n\n"
            "Nothing has been changed. Wait for it to finish and run the command "
            "again."
        ),
    )


def _journal_mode(connection: sqlite3.Connection) -> str:
    """The mode the database is in. A pure read; takes no write lock."""
    row = connection.execute("PRAGMA journal_mode").fetchone()
    return "" if row is None else str(row[0]).lower()


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
