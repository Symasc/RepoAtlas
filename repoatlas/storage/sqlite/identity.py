"""Database ownership: may RepoAtlas write to this file at all? (ARCHITECTURE.md §3)

One question is answered here, before any other, and before anything writes:

    Is the SQLite database at the configured index path a RepoAtlas index?

The release review found that RepoAtlas answered it by *not asking*. A file
containing a ``payroll`` table and ``user_version = 0`` was read as "a fresh
RepoAtlas database awaiting migration 001", so ``repoatlas init`` created
twenty-one tables in it, ran every migration and stamped the schema version. No
foreign row was destroyed, and nothing collided — but RepoAtlas had rewritten a
file it had no evidence it owned, and the only reason the damage stopped there is
that the foreign table happened to be called something else.

**Absence of a collision is not evidence of ownership.** That is the whole
finding. ``user_version`` cannot supply the missing evidence either: it is
generic SQLite metadata that every database in the world has, and ``0`` is what
it says about a file that has never heard of RepoAtlas.

**The marker.** RepoAtlas claims ``PRAGMA application_id``, SQLite's own header
field for exactly this purpose — "SQLite as an application file format" is a
supported use, and the field exists so a file can name the format it belongs to.
It was chosen over a ``meta`` identity row on measured properties
(:data:`~repoatlas.storage.sqlite.migrator.APPLICATION_ID` records the value):

* **Readable before any write.** Reading it leaves the file byte-identical and
  creates no ``-wal``/``-shm`` sibling — measured. A ``meta`` row cannot be
  reached that way, because on a foreign database ``meta`` does not exist, and
  the question "does RepoAtlas own this?" would have to be answered by creating
  the table that answers it.
* **Transactional.** It rolls back with its enclosing transaction and stays
  invisible to other connections until commit — measured. So identity and schema
  commit as one state change, and no crash leaves a file marked as a RepoAtlas
  index while holding a schema no migration produced.
* **Durable through everything that copies a database.** It survives ``VACUUM``,
  the backup API and a plain file copy — measured — because it lives in the file
  header rather than in a table. Identity is a property of the database's origin,
  not of its path.
* **Independent of the schema version**, which is the distinction the defect
  turned on and §3 of the architecture now states as an invariant.

A ``meta`` row would have had to be maintained by migrations, included in the
schema fingerprint, and created before it could be read. This is one pragma.

**What the marker does and does not prove.** It settles *ownership* and nothing
else. A marked database whose schema is not a state any migration produces is
still refused (see :func:`establish`): identity is never a licence to operate on
a schema RepoAtlas cannot account for.

**Historical databases.** Every RepoAtlas index written before this existed
carries no marker, and none of them may suddenly read as foreign. They are
recognized by *shape* instead — :func:`~repoatlas.storage.sqlite.migrator.recognizes`
compares the whole schema against the states the committed migrations actually
produce — and are marked by the next command that writes. Recognition comes
first, the write second, which is what keeps a read-only historical index
readable and keeps ``status`` from becoming a mutating command.
"""

from __future__ import annotations

import enum
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from repoatlas.errors import (
    IndexForeignApplicationError,
    IndexNotRecognizedError,
    IndexSchemaError,
)
from repoatlas.storage.sqlite.migrator import (
    APPLICATION_ID,
    application_id,
    load_migrations,
    recognizes,
    user_version,
)


class Ownership(enum.Enum):
    """How RepoAtlas established that it may write to this database.

    Not a ranking. Each value is a different *proof*, and the difference matters
    to what happens next: a marked database needs nothing further, an empty one
    is claimed by the first migration, and a legacy one has to be marked without
    that write being forced on a reader.
    """

    #: Carries RepoAtlas's ``application_id``. Nothing else to establish.
    MARKED = "marked"
    #: A valid SQLite database with no user objects — including a zero-byte file,
    #: which SQLite reads as an empty database. An empty container has no owner,
    #: so RepoAtlas may become one.
    EMPTY = "empty"
    #: Unmarked, but its schema is conclusively one RepoAtlas's own migrations
    #: produce. A pre-marker index; recognized now, marked on the next write.
    LEGACY = "legacy"


def user_objects(connection: sqlite3.Connection) -> tuple[str, ...]:
    """Names of the persistent objects in ``main`` that some application created.

    "Empty" has to be defined precisely, because the definition decides whether a
    database may be adopted. Three things it deliberately is **not**:

    * *No tables.* A database whose only object is a ``VIEW`` has zero rows in
      ``sqlite_master`` with ``type = 'table'`` — measured — and is plainly
      foreign state. Views, indexes and triggers all count.
    * *The connection's objects.* ``main`` is named explicitly. Temporary objects
      live in ``temp`` and belong to the connection, not to the file, and
      RepoAtlas never ``ATTACH``es anything, so ``main`` is the only schema whose
      ownership is in question.
    * *SQLite's own bookkeeping.* Names beginning with ``sqlite_`` are reserved
      to SQLite and cannot be created by an application. ``sqlite_sequence``
      survives dropping the ``AUTOINCREMENT`` table that caused it, with no rows
      left in it — measured — so a database reduced to that alone holds no user
      data and stays adoptable. RepoAtlas's own schema creates it anyway.
    """
    return tuple(
        str(row[0])
        for row in connection.execute("SELECT name FROM main.sqlite_master ORDER BY name")
        if not str(row[0]).lower().startswith("sqlite_")
    )


@contextmanager
def _snapshot(connection: sqlite3.Connection) -> Iterator[None]:
    """Read every ownership fact from one consistent view of the database.

    Ownership is decided from three reads — the marker, the schema version and
    the schema itself — and taking them in autocommit would let each land on a
    different side of another process's commit. Two ``repoatlas init`` runs
    against one new database is not a hypothetical: the loser can otherwise
    observe the winner's marker with the *previous* schema, or a version stamp
    with the next version's shape, and refuse a database that is perfectly sound.

    A deferred transaction reads and never writes: measured to leave the file
    byte-identical with no journal sibling, which it must, because at this point
    RepoAtlas may still turn out not to own the file.
    """
    connection.execute("BEGIN DEFERRED")
    try:
        yield
    finally:
        # Rollback rather than commit — there is nothing to commit, and saying so
        # is the stronger statement on a database that may not be ours.
        connection.rollback()


def establish(connection: sqlite3.Connection, path: Path, *, schema_version: int) -> Ownership:
    """Prove RepoAtlas may write to this database, or refuse without touching it.

    Ordered so that the cheapest and most conclusive evidence is consulted first,
    and so that the expensive evidence is only ever gathered when a write is
    about to happen anyway:

    1. **Another application's marker** ends it immediately. RepoAtlas does not
       overwrite an identity it did not write, whatever the schema looks like.
    2. **Our marker on an up-to-date schema** is the ordinary case and the hot
       path: two header reads and no schema inspection at all — measured at
       0.3 ms, against 1.6 ms for the open around it. Nothing at open will write
       to this database, so nothing needs proving beyond ownership. Splitting
       this out to skip the read transaction was measured and did not pay: the
       cost is the pragma round-trips, not the transaction, and a second copy of
       the decision is worth more than 0.03 ms.
    3. **An empty container** — no user objects, no version — is unowned rather
       than foreign, so RepoAtlas may claim it. This is where a fresh index, a
       zero-byte file and a freshly ``VACUUM``ed database all arrive.
    4. **A recognized schema shape** is what proves a pre-marker index genuine,
       and it is also what a *marked* database below the current version must
       satisfy before migrations touch it. Paying for recognition there is free:
       the alternative to inspecting the schema is migrating it blind.
    5. Anything else is refused, unchanged.

    A future ``user_version`` is deliberately not judged here. On a marked
    database that is Pass 7's condition and keeps Pass 7's message; on an
    unmarked one it simply is not a shape any committed migration produces, and
    falls out of step 4 as unrecognized — because a version number alone was
    never evidence that a file is a RepoAtlas index.
    """
    with _snapshot(connection):
        found = application_id(connection)
        if found not in (0, APPLICATION_ID):
            raise _foreign_application(path, found)

        marked = found == APPLICATION_ID
        version = user_version(connection)
        if marked and version >= schema_version:
            return Ownership.MARKED

        if version == 0 and not user_objects(connection):
            return Ownership.EMPTY

        if recognizes(connection, version, load_migrations()):
            return Ownership.MARKED if marked else Ownership.LEGACY

    raise _unrecognized(path, marked=marked)


def _foreign_application(path: Path, found: int) -> IndexForeignApplicationError:
    return IndexForeignApplicationError(
        f"The SQLite database at {path} belongs to another application.",
        detail=(
            f"Its SQLite application_id is {found} (0x{found & 0xFFFFFFFF:08X}); a RepoAtlas "
            f"index carries 0x{APPLICATION_ID:08X}.\n\n"
            "RepoAtlas does not overwrite another application's database and has "
            "changed nothing.\n\n"
            "Point REPOATLAS_DB at a different path."
        ),
    )


def _unrecognized(path: Path, *, marked: bool) -> IndexSchemaError:
    """The refusal, worded for which of the two questions actually failed."""
    if marked:
        return IndexSchemaError(
            f"The RepoAtlas index at {path} has a schema RepoAtlas does not recognize.",
            detail=(
                "The file is marked as a RepoAtlas index, but its schema is not a "
                "state any RepoAtlas migration produces, so RepoAtlas cannot tell "
                "what has been applied to it.\n\n"
                "Nothing has been repaired, rebuilt or deleted. To start again from "
                "an empty index, move the file aside and run:\n\n"
                "  repoatlas init <directory>"
            ),
        )
    return IndexNotRecognizedError(
        f"The SQLite database at {path} is not recognized as a RepoAtlas index.",
        detail=(
            "It is a valid SQLite database, but it carries no RepoAtlas identity "
            "and its schema is not one RepoAtlas created. RepoAtlas will not "
            "modify a database it cannot prove it owns, and has changed nothing.\n\n"
            "The configured index path is probably pointing at the wrong file. "
            "Point REPOATLAS_DB at a different path, or — if this file was meant "
            "to become the RepoAtlas index — move it aside and run:\n\n"
            "  repoatlas init <directory>"
        ),
    )
