"""The interpreter whose standard library turns some files into facts.

Most parsers are pinned artifacts: a tree-sitter grammar at an exact version reads a
file the same way on every host. Python sources, ``setup.py``, ``pyproject.toml`` and
the TOML lockfiles are read with the standard library instead -- ``ast`` and
``tomllib`` -- and those belong to whichever interpreter runs RepoAtlas. A newer one
accepts syntax an older one rejects (``ast`` in 3.14 parses template strings that 3.13
calls a syntax error), so the same file can hold facts on one interpreter and none on
another.

Facts are only ever reused while the version that produced them is current (V1_SPEC
§12), so the interpreter is part of each such parser's version: an index written by one
interpreter and synced by another reparses those files rather than keeping facts the
running interpreter would not produce. Major and minor only: a patch release does not
change what the standard library accepts.
"""

from __future__ import annotations

import sys


def interpreter_tag() -> str:
    """``py<major>.<minor>`` of the running interpreter, for a parser's version."""
    return f"py{sys.version_info.major}.{sys.version_info.minor}"
