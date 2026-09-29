"""Which files a repository's root ``requirements.txt`` reaches through local ``-r`` includes.

A repository that states nothing about packaging declares its dependencies in its root
``requirements.txt`` (:mod:`repoatlas.graph.py_packages`), and that file may delegate them:
``-r requirements/heroku.txt``, which says ``-r prod.txt``, which says ``-r common.txt``. A
file is a requirements file here **only because the root reaches it** -- whatever it is
called -- and nothing unreached contributes anything: no ``.txt`` file declares a
dependency for existing.

Each include was read with its file (:func:`repoatlas.parsers.requirements.requirements_facts`),
resolved from that file's own directory, and refused there when it names a URL, a variable,
an absolute path or a place above the repository. Here it is followed, depth first and in
line order, the way pip reads the files: into a file of the same repository only, and into
each file once. A file a line includes is otherwise not followed, and why is recorded:

* ``include-missing``: the repository indexes no file there -- a path into another
  repository, an untracked or ignored file, a directory;
* ``include-unread``: it indexes one but holds no requirements read from it -- binary, not
  UTF-8, too large, or changed since it was indexed;
* ``include-cycle``: the file is already being read on this path of includes;
* ``include-repeated``: the file was read through an earlier line.

A cycle or a repetition loses nothing, since the file was read; any other refusal, or one
decided where the include is written (``include-unsupported``, ``include-outside``), means
something the root delegates to was not read: the set is **incomplete**.

Every file reached carries the include lines leading to it from the root, and a requirement
the line it is on after them: ordering by that key is exactly pip's reading order, however
the walk went, so the first requirement of a name is the one pip reads first.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from repoatlas.parsers.module_facts import MODULE_RESOLUTION, PYTHON_REQUIREMENTS
from repoatlas.parsers.requirements import INCLUDE_OUTSIDE, INCLUDE_UNSUPPORTED

#: The one file a repository's requirements start from.
ROOT = "requirements.txt"
#: The claim a reached file's requirements are recorded as.
REQUIREMENTS_FACTS = f"module-resolution:{PYTHON_REQUIREMENTS}"
INCLUDE_MISSING = "include-missing"
INCLUDE_UNREAD = "include-unread"
INCLUDE_CYCLE = "include-cycle"
INCLUDE_REPEATED = "include-repeated"
#: The refusals that leave something the root delegates to unread.
_INCOMPLETE = frozenset({INCLUDE_UNSUPPORTED, INCLUDE_OUTSIDE, INCLUDE_MISSING, INCLUDE_UNREAD})


@dataclass(frozen=True, slots=True)
class Reached:
    """A file the root reaches, and the include lines, from the root's, that reach it."""

    path: str
    chain: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class Refused:
    """An include not followed: where it is written, what it names, and why."""

    path: str
    line: int
    target: str
    reason: str


@dataclass(frozen=True, slots=True)
class Reach:
    """Every file the root reaches, in the order first reached, and every include refused."""

    files: tuple[Reached, ...]
    refused: tuple[Refused, ...]

    @property
    def incomplete(self) -> bool:
        """Whether an include the root delegates to was not read (see the module docstring)."""
        return any(refused.reason in _INCOMPLETE for refused in self.refused)


def requirements_meta(facts: Mapping[str, Any]) -> str:
    """The recorded form of a reached file's requirements facts, as every module fact is."""
    meta = {
        MODULE_RESOLUTION: PYTHON_REQUIREMENTS,
        "facts": json.dumps(facts, sort_keys=True),
        "parser": PYTHON_REQUIREMENTS,
    }
    return json.dumps(meta, sort_keys=True)


def _includes(facts: Mapping[str, Any] | None) -> Sequence[Sequence[Any]]:
    found = facts.get("includes") if facts is not None else None
    return found if isinstance(found, list) else []


def reach(load: Callable[[str], Mapping[str, Any] | None], indexed: Callable[[str], bool]) -> Reach:
    """The files the root ``requirements.txt`` reaches (see the module docstring).

    ``load`` gives a file's requirements facts, or ``None`` when none were read from it;
    ``indexed`` whether the repository indexes a file at a path. The root is always the
    first file, its facts or not: without them it includes nothing.
    """
    files = [Reached(ROOT, ())]
    refused: list[Refused] = []
    read = {ROOT}
    # Depth first, each file's includes in line order; a frame is (file, chain, the files
    # on the path to it, its includes, the next one).
    stack: list[tuple[str, tuple[int, ...], frozenset[str], Sequence[Sequence[Any]], int]] = [
        (ROOT, (), frozenset({ROOT}), _includes(load(ROOT)), 0)
    ]
    while stack:
        path, chain, above, includes, at = stack[-1]
        if at >= len(includes):
            stack.pop()
            continue
        stack[-1] = (path, chain, above, includes, at + 1)
        line, target, reason = includes[at]
        facts: Mapping[str, Any] | None = None
        if reason is None and not indexed(target):
            reason = INCLUDE_MISSING
        elif reason is None and target in above:
            reason = INCLUDE_CYCLE
        elif reason is None and target in read:
            reason = INCLUDE_REPEATED
        elif reason is None:
            facts = load(target)
            if facts is None:
                reason = INCLUDE_UNREAD
        if reason is not None:
            refused.append(Refused(path, line, target, reason))
            continue
        read.add(target)
        files.append(Reached(target, (*chain, line)))
        stack.append((target, (*chain, line), above | {target}, _includes(facts), 0))
    return Reach(tuple(files), tuple(refused))
