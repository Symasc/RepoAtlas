"""Schema migration: one atomic state transition per version (ARCHITECTURE.md §3, §9).

**The invariant.**

    A migration's statements and its ``user_version`` stamp commit together, or
    neither commits.

Another connection, or a process restarted after a crash, may observe complete
schema *N* or complete schema *N+1*. Never some of *N+1*'s DDL with
``user_version`` still *N*.

**Why this module exists rather than three lines in ``database.py``.** The
previous implementation looked atomic and was not::

    BEGIN IMMEDIATE
    connection.executescript(migration_sql)     # <-- implicit COMMIT, here
    PRAGMA user_version = N
    COMMIT

``sqlite3.Connection.executescript`` **commits any open transaction before it
runs a single statement** — documented stdlib behaviour, and the reason
``connection.in_transaction`` is ``False`` immediately after the call. So the
``BEGIN IMMEDIATE`` above bought nothing: every statement in the script committed
on its own, and the version stamp landed afterwards in autocommit. A process
killed mid-script left half a schema behind with the old version still stamped,
and the next open re-ran the migration onto a schema that already had part of it
— ``duplicate column name: slug``, permanently unopenable. Two processes opening
the same database could also both pass the version check and both start applying,
because neither actually held a write transaction.

The fix is not a better wrapper around ``executescript``. It is to stop using it:
statements are executed one at a time with ``Connection.execute``, which does not
commit, inside a transaction this module owns from ``BEGIN IMMEDIATE`` to
``COMMIT``. SQLite's DDL is transactional — ``CREATE TABLE``, ``CREATE INDEX``,
``CREATE TRIGGER``, ``ALTER TABLE ADD COLUMN``, ``DROP TABLE`` and ordinary DML
all roll back — which is what makes that sufficient rather than merely tidier.

**Splitting.** Statements are separated with :func:`sqlite3.complete_statement`,
the stdlib binding for SQLite's own ``sqlite3_complete()``. It is the only
splitter that is correct here: ``001_initial.sql`` defines six triggers whose
bodies contain ``BEGIN ... END;``, and a ``sql.split(";")`` shreds each one into
an invalid ``CREATE TRIGGER ... BEGIN INSERT ...`` fragment plus a stray ``END``
(measured: 39 fragments where there are 29 statements). Asking SQLite where a
statement ends also handles semicolons inside string literals and comments, and
keeps the migrations as ``.sql`` files rather than transcribing them into Python
lists that would drift from the schema they document.

**Serialization** is SQLite's own writer lock, not an advisory file lock.
Migrations run at *open*, and readers open the database too; taking the sync
writer lock here would make ``locate`` queue behind a running ``sync``.
``BEGIN IMMEDIATE`` acquires the write lock for real once the transaction is
genuinely held, ``busy_timeout`` bounds the wait, and the version is re-read
**inside** the transaction so the loser of the race observes the winner's work
instead of repeating it.

**Recovery.** Databases damaged by the vulnerable implementation still exist, and
making future migrations atomic does not un-strand them. Each statement's effect
is checked against the live schema, so a migration can resume from the first
statement that has not been applied. What makes that safe rather than a blanket
``IF NOT EXISTS`` is that the check is exact and ordered: an object that exists
with a *different* definition, or an effect applied out of order, is not a
recognizable partial migration and is refused. RepoAtlas never treats an
arbitrary schema that happens to overlap as successfully migrated.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from importlib import resources

from repoatlas.errors import MigrationError

#: What a statement did to the schema, as far as the schema can tell.
APPLIED = "applied"
PENDING = "pending"
CONFLICT = "conflict"
#: A statement whose effect leaves no schema trace — data-only work. It may be
#: re-executed during recovery, so every such statement must be idempotent; the
#: audit in ARCHITECTURE.md §3 records that the current ones are.
OPAQUE = "opaque"

_CREATE = re.compile(
    r"^\s*CREATE\s+(?:UNIQUE\s+)?(?:VIRTUAL\s+)?(TABLE|INDEX|TRIGGER|VIEW)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?[\"'`\[]?(\w+)",
    re.IGNORECASE,
)
_ALTER_ADD = re.compile(
    r"^\s*ALTER\s+TABLE\s+[\"'`\[]?(\w+)[\"'`\]]?\s+ADD\s+(?:COLUMN\s+)?[\"'`\[]?(\w+)",
    re.IGNORECASE,
)
_DROP = re.compile(
    r"^\s*DROP\s+(TABLE|INDEX|TRIGGER|VIEW)\s+(?:IF\s+EXISTS\s+)?[\"'`\[]?(\w+)",
    re.IGNORECASE,
)


def split_statements(sql: str) -> list[str]:
    """Split a migration into executable statements.

    Uses :func:`sqlite3.complete_statement` — SQLite's own notion of where a
    statement ends — so semicolons inside string literals, inside comments and
    inside a trigger's ``BEGIN ... END`` body do not split anything.
    """
    statements: list[str] = []
    buffer = ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statements.append(buffer.strip())
            buffer = ""
    tail = buffer.strip()
    if tail and not _is_only_comment(tail):
        statements.append(tail)
    return [s for s in statements if not _is_only_comment(s)]


def _is_only_comment(text: str) -> bool:
    """Whether a fragment carries no statement — trailing commentary."""
    stripped = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("--")
    ).strip()
    return not stripped.strip(";")


def _body(sql: str) -> str:
    """The statement with its leading commentary removed.

    Statements arrive carrying the comment block that documents them — the
    migrations are written to be read, and the splitter keeps that prose attached
    to the statement it explains. Structural matching has to look past it.
    """
    lines = sql.splitlines()
    start = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith("--"):
            start = index
            break
    else:
        return ""
    return "\n".join(lines[start:]).strip()


def _normalize(sql: str) -> str:
    """Collapse whitespace and drop comments, so two spellings of one statement
    compare equal. Used only to decide whether an existing object is *the* object
    this migration creates, never to rewrite anything."""
    body = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    return re.sub(r"\s+", " ", body).strip().rstrip(";").lower()


@dataclass(frozen=True, slots=True)
class Migration:
    """One version transition, with its statements already separated."""

    version: int
    statements: tuple[str, ...]

    @property
    def name(self) -> str:
        return f"migration {self.version:03d}"


def load_migrations() -> list[Migration]:
    package = resources.files("repoatlas.storage.sqlite.migrations")
    found: list[Migration] = []
    for entry in package.iterdir():
        if not entry.name.endswith(".sql"):
            continue
        version = int(entry.name.split("_", 1)[0])
        sql = entry.read_text(encoding="utf-8")
        found.append(Migration(version=version, statements=tuple(split_statements(sql))))
    return sorted(found, key=lambda m: m.version)


def user_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


def _object_sql(connection: sqlite3.Connection, name: str) -> str | None:
    row = connection.execute("SELECT sql FROM sqlite_master WHERE name = ?", (name,)).fetchone()
    if row is None:
        return None
    # An auto-created index (a UNIQUE constraint's own index) has NULL sql. It
    # exists, but it is not something a migration statement authored.
    return "" if row[0] is None else str(row[0])


def _column_type(connection: sqlite3.Connection, table: str, column: str) -> str | None:
    try:
        # `table_xinfo`, not `table_info`: the latter omits GENERATED columns
        # entirely, so migration 004's `files.basename` would read as absent and
        # the migration would be re-applied onto a schema that already has it.
        rows = connection.execute(f"PRAGMA table_xinfo({table})").fetchall()
    except sqlite3.Error:
        return None
    for row in rows:
        if str(row[1]).lower() == column.lower():
            return str(row[2]).lower()
    return None


def statement_state(connection: sqlite3.Connection, statement: str) -> str:
    """Whether ``statement``'s effect is already present in the live schema.

    ``CONFLICT`` is the important answer: the object exists but is *not* the one
    this migration creates. That is an unrecognized schema, not a partial
    migration, and the caller refuses rather than guessing.
    """
    body = _body(statement)
    created = _CREATE.match(body)
    if created:
        existing = _object_sql(connection, created.group(2))
        if existing is None:
            return PENDING
        if _normalize(existing) == _normalize(statement):
            return APPLIED
        return CONFLICT

    added = _ALTER_ADD.match(body)
    if added:
        table, column = added.group(1), added.group(2)
        declared = _column_type(connection, table, column)
        if declared is None:
            return PENDING
        # The column exists. Require the declared type to match the statement, so
        # a hand-made column of the wrong type is a conflict rather than a
        # silently accepted migration.
        expected = re.search(
            rf"ADD\s+(?:COLUMN\s+)?[\"'`\[]?{re.escape(column)}[\"'`\]]?\s+(\w+)",
            body,
            re.IGNORECASE,
        )
        if expected and expected.group(1).lower() != declared:
            return CONFLICT
        return APPLIED

    dropped = _DROP.match(body)
    if dropped:
        return PENDING if _object_sql(connection, dropped.group(2)) is not None else APPLIED

    return OPAQUE


def schema_fingerprint(connection: sqlite3.Connection) -> tuple[object, ...]:
    """Every structural fact about the schema, and nothing about the data.

    Two databases with the same fingerprint have the same tables, columns,
    declared types, nullability, defaults, indexes, triggers and views. Row
    contents are deliberately excluded — a migration's identity is its shape.
    """
    objects = tuple(
        (str(row[0]), str(row[1]), _normalize(str(row[3] or "")))
        for row in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        )
    )
    tables = tuple(
        (
            str(name),
            tuple(
                (str(c[1]), str(c[2]).lower(), int(c[3]), str(c[4]), int(c[5]), int(c[6]))
                for c in connection.execute(f"PRAGMA table_xinfo({name})")
            ),
            tuple((str(i[1]), int(i[2])) for i in connection.execute(f"PRAGMA index_list({name})")),
        )
        for (name,) in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
    )
    return (objects, tables)


def _scratch_at(version: int, migrations: list[Migration]) -> sqlite3.Connection:
    """An in-memory database carrying exactly the schema of ``version``."""
    scratch = sqlite3.connect(":memory:")
    scratch.execute("PRAGMA foreign_keys = ON")
    for migration in migrations:
        if migration.version > version:
            break
        for statement in migration.statements:
            scratch.execute(statement)
        scratch.execute(f"PRAGMA user_version = {migration.version}")
    scratch.commit()
    return scratch


def resume_point(
    connection: sqlite3.Connection, migration: Migration, migrations: list[Migration]
) -> int:
    """The index of the first statement of ``migration`` that still has to run.

    ``0`` is the ordinary answer and costs nothing to reach: on a database whose
    version is genuinely *N-1*, the migration has not started.

    Anything else means this database was damaged by the implementation this
    module replaced — statements committed one at a time, then a crash, with the
    old version still stamped. The resume point is then found by **simulation**
    rather than by inspecting statements individually: a scratch database is
    built at version *N-1* and the migration's statements are applied to it one
    at a time, comparing the schema shape against the live database after each.
    The prefix whose shape matches exactly is the prefix that ran.

    Simulation is used because per-statement inspection cannot answer the
    question. ``003`` drops ``embeddings`` and creates a different table with the
    same name; asking "has ``CREATE TABLE embeddings`` been applied?" has no
    order-independent answer, and asking "has ``DROP TABLE embeddings`` been
    applied?" reads as *no* once the replacement exists. Replaying the statements
    is the only check that is exact for every migration rather than for the ones
    whose statements happen to be independent.

    Raises :class:`MigrationError` when no prefix matches. A schema that merely
    resembles a migration is not one RepoAtlas will complete — fail closed.
    """
    if not migration.statements:
        return 0
    if statement_state(connection, migration.statements[0]) in (PENDING, OPAQUE):
        return 0

    live = schema_fingerprint(connection)
    scratch = _scratch_at(migration.version - 1, migrations)
    try:
        for index in range(len(migration.statements) + 1):
            if schema_fingerprint(scratch) == live:
                return index
            if index < len(migration.statements):
                try:
                    scratch.execute(migration.statements[index])
                except sqlite3.Error:  # pragma: no cover - a broken migration file
                    break
    finally:
        scratch.close()

    raise MigrationError(
        f"The database schema is not a state {migration.name} can produce.",
        detail=(
            "It is neither the schema before the migration nor any point part-way "
            "through it, so RepoAtlas cannot tell what has already been applied.\n\n"
            "RepoAtlas will not modify a schema it does not recognize."
        ),
    )


def apply_migrations(connection: sqlite3.Connection, migrations: list[Migration]) -> None:
    """Bring the schema up to date, one atomic version transition at a time.

    Each version is its own transaction rather than the whole chain being one, so
    a database several versions behind that fails at *N+1* stays usable at *N*
    instead of losing the transitions that already succeeded. Every
    ``user_version`` boundary is a valid, openable schema.
    """
    for migration in migrations:
        if migration.version <= user_version(connection):
            continue
        _apply_one(connection, migration, migrations)


def _apply_one(
    connection: sqlite3.Connection, migration: Migration, migrations: list[Migration]
) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as error:
        raise _contention(migration, error) from error

    try:
        # Re-read *inside* the write transaction. Between the check above and the
        # lock being granted, another process may have applied this exact
        # migration; the version read before waiting is stale by definition.
        if migration.version <= user_version(connection):
            connection.rollback()
            return

        start = resume_point(connection, migration, migrations)
        for statement in migration.statements[start:]:
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version = {migration.version}")
    except sqlite3.OperationalError as error:
        connection.rollback()
        if "locked" in str(error) or "busy" in str(error):
            raise _contention(migration, error) from error
        raise MigrationError(
            f"{migration.name} failed and was rolled back.",
            detail=f"The database is unchanged at schema version {user_version(connection)}.",
        ) from error
    except BaseException:
        connection.rollback()
        raise
    connection.commit()


def _contention(migration: Migration, error: Exception) -> MigrationError:
    return MigrationError(
        "Timed out waiting for another process to finish upgrading the database.",
        detail=(
            f"Another RepoAtlas process is applying {migration.name}.\n\n"
            "Wait for it to finish and run the command again."
        ),
    )
