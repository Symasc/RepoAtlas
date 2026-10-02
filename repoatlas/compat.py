"""Standard-library names newer than the oldest interpreter RepoAtlas supports.

RepoAtlas runs on Python 3.10, and two names it uses arrived in 3.11: ``enum.StrEnum``
and ``tomllib``. On 3.11 and later both are the standard library's own. On 3.10,
``StrEnum`` is the same behaviour written out --- members are strings, and ``str()``
and ``format()`` give the value, not ``Class.MEMBER`` --- and ``tomllib`` is
``tomli``, the project ``tomllib`` was taken from, pinned in ``pyproject.toml``.

The interpreter is already part of every standard-library parser's version
(:mod:`repoatlas.parsers.interpreter`), so facts read with ``tomli`` on 3.10 are never
reused by 3.11's ``tomllib``, or the other way round.
"""

from __future__ import annotations

import enum
import sys

__all__ = ["StrEnum", "tomllib"]

if sys.version_info >= (3, 11):
    from enum import StrEnum

    import tomllib
else:
    import tomli as tomllib

    class StrEnum(str, enum.Enum):
        """``enum.StrEnum`` as 3.11 defines it."""

        def __str__(self) -> str:
            return str.__str__(self)

        def __format__(self, format_spec: str) -> str:
            return str.__format__(self, format_spec)
