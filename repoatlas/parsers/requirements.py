"""How a requirement is read: a PEP 508 string, and a requirements file's lines.

Below both the dependency layer, which records each requirement a manifest states (its
``python`` module, which re-exports this through ``normalize``), and the setuptools facts,
which record whether a
``setup.cfg`` or ``setup()`` states its ``install_requires`` as requirements at all
(:mod:`repoatlas.parsers.setuptools_project`): the two must read a requirement alike, and
a language parser never imports the dependency layer.

A requirements file is read here once, line by line (:func:`requirement_lines`), for both of
its readers: the dependency layer's rows of a ``requirements*.txt``, and the facts of a file
a repository's root ``requirements.txt`` reaches through ``-r`` (:func:`requirements_facts`),
whatever that file is called -- so a line is a requirement, an include or nothing alike for
both, and never read twice two ways.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterator
from typing import Any

from repoatlas.parsers.marks import split_lines

#: ``foo>=1.0``, ``foo[extra]~=2``, ``foo (>=1)`` — the name is what precedes these.
_PYTHON_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
#: A comment, as pip strips one before reading a line: ``#`` at its start or after a space.
_COMMENT = re.compile(r"(^|\s+)#.*$")
#: A target pip reads as a URL rather than a local file.
_URL = re.compile(r"^(https?|file):|://", re.IGNORECASE)
#: Version of :func:`requirements_facts`; a fact of another version is read again.
REQUIREMENTS_FACTS_VERSION = 1
#: Why an include is not followed, decided from its own text and place.
INCLUDE_UNSUPPORTED = "include-unsupported"
INCLUDE_OUTSIDE = "include-outside"


def python_requirement(specifier: str) -> tuple[str, str | None] | None:
    """Split a PEP 508 requirement into its name and the rest as a constraint."""
    text = specifier.strip()
    if not text or text.startswith("#"):
        return None
    text = text.split(";", 1)[0].strip()  # drop environment markers
    match = _PYTHON_NAME.match(text)
    if match is None:
        return None
    name = match.group(1)
    remainder = text[match.end() :].strip()
    if remainder.startswith("["):
        closing = remainder.find("]")
        if closing != -1:
            remainder = remainder[closing + 1 :].strip()
    return name, remainder or None


def _include(text: str) -> str | None:
    """The file a line includes with ``-r``, as written, or ``None`` when it includes none:
    ``-r X``, ``--requirement X``, ``-rX`` or ``--requirement=X``, its comment stripped."""
    line = _COMMENT.sub("", text).strip()
    option, _, rest = line.partition(" ") if " " in line else line.partition("\t")
    if option in ("-r", "--requirement"):
        return rest.strip()
    if option.startswith("--requirement="):
        return line.removeprefix("--requirement=").strip()
    if option.startswith("-r") and not option.startswith("--"):
        return line[2:].strip()
    return None


def requirement_lines(content: str) -> Iterator[tuple[int, str, str]]:
    """Each line of a requirements file that states something, as (line, kind, text): a
    ``requirement`` line as written, or an ``include`` with the file it names as written.

    Blank lines, comments and every other option (``-c``, ``-e``, ``--index-url``, ...)
    state nothing here. Lines are numbered by the shared source-line model.
    """
    for number, line in enumerate(split_lines(content), start=1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if text.startswith("-"):  # an option, never a requirement: -r, -c, -e, --index-url, ...
            included = _include(text)
            if included is not None:
                yield number, "include", included
            continue
        yield number, "requirement", text


def include_target(including: str, written: str) -> tuple[str, str | None]:
    """Where an include in the file ``including`` leads, repository-relative, or why it is
    not followed: a URL, an environment variable, a home, absolute, backslash or drive path
    (``include-unsupported``), or a path climbing above the repository (``include-outside``).

    Relative to the including file's own directory, as pip resolves it, and lexically: the
    index knows paths, not a file system.
    """
    if (
        not written
        or _URL.search(written)
        or "$" in written
        or "\\" in written
        or written.startswith(("/", "~"))
        or (len(written) > 1 and written[1] == ":")
    ):
        return written, INCLUDE_UNSUPPORTED
    path = posixpath.normpath(posixpath.join(posixpath.dirname(including), written))
    if path == ".." or path.startswith("../"):
        return written, INCLUDE_OUTSIDE
    return path, None


def requirements_facts(path: str, content: str) -> dict[str, Any]:
    """What a requirements file states, read as the root ``requirements.txt``'s ``-r`` lines
    reach it: each include (line, target, why it is not followed or ``None``) and each
    requirement (line, name, constraint), in line order."""
    includes: list[list[Any]] = []
    requires: list[list[Any]] = []
    for number, kind, text in requirement_lines(content):
        if kind == "include":
            target, refused = include_target(path, text)
            includes.append([number, target, refused])
            continue
        parsed = python_requirement(text)
        if parsed is not None:
            requires.append([number, parsed[0], parsed[1]])
    return {"v": REQUIREMENTS_FACTS_VERSION, "includes": includes, "requires": requires}
