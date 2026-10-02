"""Where a SQLite failure becomes a RepoAtlas index state (V1_SPEC §4).

One question is answered here and nowhere else: **is this failure a property of
the database the user has, or of the code that just ran?** Everything above this
module receives a :class:`~repoatlas.errors.RepoAtlasError` with an exit code
already decided, and nothing above it inspects ``sqlite3`` types or message text.

The rule is *fail open*. A recognized condition is translated; anything else is
re-raised exactly as SQLite raised it and reaches the terminal as the unexpected
failure it is. That direction matters: exit 3 tells an operator their index is
the problem, and the natural remedy is to delete it. A RepoAtlas bug laundered
into exit 3 therefore costs the user their index *and* buries the defect, which
is a far worse outcome than one more traceback. So the classifier enumerates, and
an unenumerated code is not "probably corruption".

**Codes, not prose.** ``sqlite3.Error`` has carried ``sqlite_errorcode`` and
``sqlite_errorname`` since Python 3.11, so corruption and access faults are
matched on SQLite's own result codes. Extended codes are reduced to their primary
code (``errorcode & 0xFF``): ``SQLITE_READONLY_DIRECTORY`` (1544) is a
``SQLITE_READONLY`` (8), and enumerating the extended variants would be a list
that grows with SQLite rather than with RepoAtlas.

**Python 3.10 has no codes.** Its exceptions carry only SQLite's message, so there,
and only there, an error with no code is given one from the message SQLite itself
words each code with (``sqlite3_errstr``), matched whole. A message SQLite words
any other way is unrecognized, and falls open like an unenumerated code.

**One exception, and it is bounded.** A database missing a table reports
``SQLITE_ERROR`` (1) — the same code as a typo in a query, which is the case that
must *not* become exit 3. The code cannot separate them, so the name SQLite
reports as missing is checked against the set of objects the current schema
defines. Missing an object RepoAtlas creates means the database lost it; naming
an object RepoAtlas never creates means the SQL is wrong, and that stays a bug.
"""

from __future__ import annotations

import errno
import re
import sqlite3
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

from repoatlas.errors import (
    IndexCorruptError,
    IndexInaccessibleError,
    IndexSchemaError,
    IndexUnavailableError,
)

__all__ = [
    "classify",
    "is_contention",
    "is_index_condition",
    "opening_errors",
    "storage_errors",
]

#: SQLite's primary result codes (https://sqlite.org/rescode.html). Python names
#: them ``sqlite3.SQLITE_*`` from 3.11; they are fixed by SQLite's C API, so they
#: are written out here for 3.10.
SQLITE_ERROR = 1
SQLITE_PERM = 3
SQLITE_BUSY = 5
SQLITE_LOCKED = 6
SQLITE_READONLY = 8
SQLITE_CORRUPT = 11
SQLITE_CANTOPEN = 14
SQLITE_NOTADB = 26

#: Result codes whose meaning is "the bytes on disk are not a usable database".
_CORRUPT = {
    SQLITE_CORRUPT: "SQLITE_CORRUPT",
    SQLITE_NOTADB: "SQLITE_NOTADB",
}

#: Result codes whose meaning is "the database could not be reached", which says
#: nothing about whether its contents are sound.
_INACCESSIBLE = {
    SQLITE_CANTOPEN: "SQLITE_CANTOPEN",
    SQLITE_READONLY: "SQLITE_READONLY",
    SQLITE_PERM: "SQLITE_PERM",
}

#: Result codes whose meaning is "another connection holds a lock, try later".
#: Deliberately *not* index conditions: nothing is wrong with the database, and
#: :func:`classify` must keep returning ``None`` for them so contention can never
#: be reported as an unusable index. ``SQLITE_LOCKED`` is included because a
#: shared-cache or table-level conflict is the same transient thing from the
#: caller's side; neither is a property of the bytes on disk.
_CONTENTION = frozenset({SQLITE_BUSY, SQLITE_LOCKED})

#: ``errno`` values from the ``mkdir``/``stat`` work that precedes the connection.
#: ``ENOSPC`` and ``EIO`` are absent on purpose: neither has been reproduced here,
#: and a guess about what they mean for an index is exactly what the fail-open
#: rule forbids.
_ACCESS_ERRNOS = frozenset(
    {errno.EACCES, errno.EPERM, errno.EROFS, errno.ENOTDIR, errno.EISDIR, errno.ELOOP}
)

_MISSING_OBJECT = re.compile(r"^no such (table|column|index): ([A-Za-z0-9_.]+)$")

#: Python 3.10 only: SQLite's own message for each code above (``sqlite3_errstr``).
#: ``SQLITE_ERROR`` has none here: its message is a statement's, and only the
#: missing-object messages (:data:`_MISSING_OBJECT`) are ever classified.
_CODE_BY_MESSAGE = {
    "access permission denied": SQLITE_PERM,
    "database is locked": SQLITE_BUSY,
    "database table is locked": SQLITE_LOCKED,
    "attempt to write a readonly database": SQLITE_READONLY,
    "database disk image is malformed": SQLITE_CORRUPT,
    "unable to open database file": SQLITE_CANTOPEN,
    "file is not a database": SQLITE_NOTADB,
}


def _primary(error: sqlite3.Error) -> int | None:
    """SQLite's primary result code, with any extended bits removed.

    On Python 3.10, which gives no code, the code SQLite's own wording of the
    message names, if it is one of :data:`_CODE_BY_MESSAGE` or a missing object.
    """
    code = getattr(error, "sqlite_errorcode", None)
    if code is not None:
        return int(code) & 0xFF
    message = str(error)
    if _MISSING_OBJECT.match(message):
        return SQLITE_ERROR
    return _CODE_BY_MESSAGE.get(message)


def _corrupt(path: Path, name: str) -> IndexCorruptError:
    what = (
        "the file is not a SQLite database"
        if name == "SQLITE_NOTADB"
        else "the database image is malformed"
    )
    return IndexCorruptError(
        f"The RepoAtlas index at {path} cannot be used: {what}.",
        detail=(
            f"SQLite reported {name}.\n\n"
            "RepoAtlas has not changed the file. Nothing is deleted, renamed or "
            "rebuilt automatically.\n\n"
            "To start again from an empty index, move the file aside and run:\n\n"
            "  repoatlas init <directory>"
        ),
    )


def _inaccessible(path: Path, name: str) -> IndexInaccessibleError:
    if name == "SQLITE_READONLY":
        return IndexInaccessibleError(
            f"RepoAtlas cannot write to the index at {path}.",
            detail=(
                f"SQLite reported {name}.\n\n"
                "The index itself may be intact. Check that the file and the "
                "directory containing it are writable by this user."
            ),
        )
    return IndexInaccessibleError(
        f"RepoAtlas cannot read the index at {path}.",
        detail=(
            f"SQLite reported {name}.\n\n"
            "The index itself may be intact. Check that the path exists, is a "
            "file rather than a directory, and is readable by this user."
        ),
    )


def _missing_object(path: Path, kind: str, name: str) -> IndexSchemaError:
    return IndexSchemaError(
        f"The RepoAtlas index at {path} is missing part of its schema.",
        detail=(
            f"The database has no {kind} named {name}, which the current schema "
            "requires.\n\n"
            "RepoAtlas will not rebuild a schema it did not find intact. To start "
            "again from an empty index, move the file aside and run:\n\n"
            "  repoatlas init <directory>"
        ),
    )


def classify(error: BaseException, path: Path) -> IndexUnavailableError | None:
    """The index condition ``error`` proves, or ``None`` if it proves none.

    ``None`` is the answer for every failure that is not a recognized property of
    the database — a query defect, an unmapped result code, a bug. The caller
    re-raises those untouched.
    """
    if isinstance(error, sqlite3.Error):
        code = _primary(error)
        if code is None:
            return None
        if code in _CORRUPT:
            return _corrupt(path, _CORRUPT[code])
        if code in _INACCESSIBLE:
            return _inaccessible(path, _INACCESSIBLE[code])
        if code == SQLITE_ERROR:
            return _classify_missing_object(error, path)
        return None
    if isinstance(error, OSError) and error.errno in _ACCESS_ERRNOS:
        return IndexInaccessibleError(
            f"RepoAtlas cannot reach the index at {path}.",
            detail=(
                f"The operating system reported {errno.errorcode.get(error.errno or 0, '?')}.\n\n"
                "Check that the path and the directories above it exist and are "
                "accessible to this user."
            ),
        )
    return None


def _classify_missing_object(error: sqlite3.Error, path: Path) -> IndexSchemaError | None:
    """``SQLITE_ERROR`` only when the missing object is one RepoAtlas creates."""
    match = _MISSING_OBJECT.match(str(error))
    if match is None:
        return None
    kind, name = match.group(1), match.group(2)
    # `no such column: t.c` names the table it was qualified with, not an object.
    bare = name.split(".", 1)[0] if kind == "column" else name
    # Deferred: the migrator imports this module for `is_index_condition`, and a
    # module-level import here would close the loop.
    from repoatlas.storage.sqlite.migrator import schema_objects

    if bare not in schema_objects():
        return None
    return _missing_object(path, kind, name)


def is_contention(error: BaseException) -> bool:
    """Whether ``error`` is another connection holding a lock, and nothing worse.

    The predicate that decides what may be retried, and it is narrow on purpose.
    ``SQLITE_READONLY``, ``SQLITE_CANTOPEN``, ``SQLITE_IOERR`` and ``SQLITE_FULL``
    all reach the same ``except sqlite3.OperationalError``, and none of them
    improves by waiting: retrying a read-only file for ten seconds turns an
    instant, accurate diagnostic into a hang that ends in the wrong message.

    Matched on SQLite's own result codes rather than on ``"database is locked"``.
    The text is not a stable interface — it is shared by ``SQLITE_BUSY`` and
    ``SQLITE_BUSY_SNAPSHOT`` and has been reworded across SQLite releases —
    whereas the primary code is documented and has carried through
    ``sqlite_errorcode`` since Python 3.11. Python 3.10 carries no code, so there
    the message is all there is (:func:`_primary`).
    """
    return isinstance(error, sqlite3.Error) and _primary(error) in _CONTENTION


def is_index_condition(error: BaseException) -> bool:
    """Whether :func:`classify` would recognize ``error``, without the path.

    For callers that already have a better description of *what they were doing*
    and only need to know whether the failure is theirs to describe — the
    migrator, which must not report "the migration failed" when what actually
    happened is that the file could not be written at all.
    """
    return classify(error, Path()) is not None


@contextmanager
def _translating(path: Path, caught: tuple[type[BaseException], ...]) -> Iterator[None]:
    try:
        yield
    except caught as error:
        translated = classify(error, path)
        if translated is None:
            raise
        raise translated from error


def storage_errors(path: Path) -> AbstractContextManager[None]:
    """Translate recognized index failures around ordinary statement execution.

    Needed beyond the connect call because a corrupt page is only read when
    something reads it: a damaged database can open cleanly and fail on the first
    query that touches the damage, which is precisely the case a connect-time-only
    fix would leave producing a traceback.

    ``OSError`` is deliberately **not** caught here. A transaction block wraps
    application code, and the sync pipeline can raise ``PermissionError`` about a
    *repository* file; translating that would report a healthy index as
    unreachable — the same laundering this module exists to prevent, pointed at
    the filesystem instead of at bugs.
    """
    return _translating(path, (sqlite3.Error,))


def opening_errors(path: Path) -> AbstractContextManager[None]:
    """The same translation for opening, where the filesystem is in scope.

    ``open_database`` creates the parent directory and hands the path to SQLite,
    so an ``OSError`` here is unambiguously about the index itself.
    """
    return _translating(path, (sqlite3.Error, OSError))
