"""SQLite result codes by name, for tests that give an error a code.

``sqlite3`` names the codes from Python 3.11, and these tests read them from there:
an oracle independent of the table the classifier writes out for 3.10
(``repoatlas.storage.sqlite.errors``). On 3.10 SQLite's documented numbers
(https://sqlite.org/rescode.html) stand in.
"""

from __future__ import annotations

import sqlite3
from typing import TypeVar

_DOCUMENTED = {
    "SQLITE_ERROR": 1,
    "SQLITE_PERM": 3,
    "SQLITE_BUSY": 5,
    "SQLITE_LOCKED": 6,
    "SQLITE_READONLY": 8,
    "SQLITE_IOERR": 10,
    "SQLITE_CORRUPT": 11,
    "SQLITE_FULL": 13,
    "SQLITE_CANTOPEN": 14,
    "SQLITE_SCHEMA": 17,
    "SQLITE_CONSTRAINT": 19,
    "SQLITE_MISMATCH": 20,
    "SQLITE_NOTADB": 26,
    "SQLITE_CANTOPEN_ISDIR": 526,
    "SQLITE_CORRUPT_INDEX": 779,
    "SQLITE_READONLY_DIRECTORY": 1544,
}

CODES: dict[str, int] = {
    name: getattr(sqlite3, name, number) for name, number in _DOCUMENTED.items()
}

E = TypeVar("E", bound=sqlite3.Error)


def with_code(error: E, name: str, code: int | None = None) -> E:
    """``error`` carrying a result code, as CPython 3.11+ sets one on a real error.

    Set through ``__dict__``: 3.10's ``sqlite3.Error`` declares no such attribute,
    and the classifier reads it with ``getattr`` on every version.
    """
    vars(error).update(
        sqlite_errorcode=CODES[name] if code is None else code, sqlite_errorname=name
    )
    return error
