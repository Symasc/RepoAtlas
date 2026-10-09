"""Paths and globs a configuration writes, matched against the files the sync admitted.

A configuration names files relative to its own directory, and may name them in another
managed repository (``../schema/api.graphql``). Matching happens here, against the indexed
file rows and the managed repository roots and nothing else: the filesystem is never
listed, read or asked to resolve a link. A file the sync did not admit -- ignored,
excluded, unreadable, outside every managed repository -- matches nothing, however it
exists on disk.

Globs follow the conventions configuration tools share (fast-glob, globby): ``*`` and
``?`` within one path segment, ``**`` across any number of segments including none,
``[...]`` classes, ``{a,b}`` alternatives, and no match for a dot-segment unless the
pattern spells the dot. Extended globs (``@(...)``, ``+(...)``) and nested alternatives
are not translated; :func:`compile_glob` returns ``None`` for them and the caller says so.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path, PurePosixPath

from repoatlas.graph.resolution import ResolutionIndex
from repoatlas.storage.protocols import FileRow

_GLOB_CHARACTERS = frozenset("*?[{")
_EXTGLOB = re.compile(r"[@+!*?]\(")


def locate(index: ResolutionIndex, origin: FileRow, raw: str) -> tuple[int, str] | None:
    """``(repo_id, rel_path)`` of what ``raw``, written in ``origin``'s directory, names.

    Within ``origin``'s repository the path is normalized textually; a path that climbs
    out of it is placed under the deepest managed root that contains it. ``None`` when it
    lies in no managed repository.
    """
    if not raw:
        return None
    base = PurePosixPath(origin.rel_path).parent.as_posix()
    if raw.startswith("/"):
        return _in_managed(index, raw)
    joined = (
        posixpath.normpath(posixpath.join(base, raw)) if base != "." else posixpath.normpath(raw)
    )
    if joined != ".." and not joined.startswith("../"):
        return origin.repo_id, "" if joined == "." else joined
    absolute = posixpath.normpath(posixpath.join(origin.repo_path.as_posix(), base, raw))
    return _in_managed(index, absolute)


def _in_managed(index: ResolutionIndex, absolute: str) -> tuple[int, str] | None:
    path = Path(absolute)
    for root, repo_id in index.repo_roots:  # deepest first
        if path == root or path.is_relative_to(root):
            relative = path.relative_to(root).as_posix()
            return repo_id, "" if relative == "." else relative
    return None


def resolve_file(index: ResolutionIndex, origin: FileRow, raw: str) -> FileRow | None:
    """The admitted file ``raw`` names, or ``None``."""
    found = locate(index, origin, raw)
    return index.file(*found) if found is not None else None


def tree_files(
    index: ResolutionIndex, origin: FileRow, raw: str, suffixes: tuple[str, ...]
) -> list[FileRow]:
    """What a tool that takes "a file or a directory" reads: the admitted file ``raw``
    names, or else every admitted file below that directory with one of ``suffixes``."""
    found = resolve_file(index, origin, raw)
    if found is not None:
        return [found]
    directory = locate(index, origin, raw)
    if directory is None:
        return []
    repo_id, prefix = directory
    lead = f"{prefix}/" if prefix else ""
    return [
        row
        for rel, row in sorted(index.files_by_repo.get(repo_id, {}).items())
        if rel.startswith(lead) and rel.endswith(suffixes)
    ]


def match_admitted(index: ResolutionIndex, origin: FileRow, pattern: str) -> list[FileRow] | None:
    """Every admitted file ``pattern`` matches, in path order; ``None`` when the pattern
    uses syntax this module does not translate."""
    segments = pattern.split("/")
    static: list[str] = []
    for segment in segments:
        if any(character in _GLOB_CHARACTERS for character in segment) or _EXTGLOB.search(segment):
            break
        static.append(segment)
    rest = "/".join(segments[len(static) :])
    if not rest:
        found = resolve_file(index, origin, pattern)
        return [found] if found is not None else []
    regex = compile_glob(rest)
    if regex is None:
        return None
    directory = locate(index, origin, "/".join(static) or ".")
    if directory is None:
        return []
    repo_id, prefix = directory
    lead = f"{prefix}/" if prefix else ""
    return [
        row
        for rel, row in sorted(index.files_by_repo.get(repo_id, {}).items())
        if rel.startswith(lead) and regex.fullmatch(rel[len(lead) :])
    ]


def compile_glob(pattern: str) -> re.Pattern[str] | None:
    """A regular expression matching what ``pattern`` matches, relative to its base."""
    if _EXTGLOB.search(pattern):
        return None
    translated = _translate(pattern, nested=False)
    return re.compile(translated) if translated is not None else None


def _translate(pattern: str, *, nested: bool) -> str | None:
    out: list[str] = []
    index = 0
    at_segment_start = True
    while index < len(pattern):
        character = pattern[index]
        if pattern.startswith("**", index) and at_segment_start:
            after = index + 2
            if after == len(pattern):
                out.append(r"(?:(?!\.)[^/]*(?:/(?!\.)[^/]*)*)?")
                index = after
                continue
            if pattern[after] == "/":
                out.append(r"(?:(?!\.)[^/]+/)*")
                index = after + 1
                continue
        if character == "*":
            out.append(r"(?!\.)[^/]*" if at_segment_start else r"[^/]*")
        elif character == "?":
            out.append(r"(?!\.)[^/]" if at_segment_start else r"[^/]")
        elif character == "[":
            close = pattern.find("]", index + 2)
            if close == -1:
                out.append(re.escape(character))
            else:
                body = pattern[index + 1 : close]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                index = close + 1
                at_segment_start = False
                continue
        elif character == "{":
            if nested:
                return None
            close = pattern.find("}", index)
            if close == -1:
                out.append(re.escape(character))
            else:
                options = pattern[index + 1 : close].split(",")
                translated = [_translate(option, nested=True) for option in options]
                if any(option is None for option in translated):
                    return None
                out.append("(?:" + "|".join(t for t in translated if t is not None) + ")")
                index = close + 1
                at_segment_start = False
                continue
        else:
            out.append(re.escape(character))
        at_segment_start = character == "/"
        index += 1
    return "".join(out)
