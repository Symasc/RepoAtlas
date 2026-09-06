"""Working-tree file enumeration (ARCHITECTURE.md §6, §8, §12).

The working tree is authoritative, so enumeration prefers ``git ls-files``: it
returns tracked plus untracked-not-ignored files, giving real ``.gitignore``
semantics without reimplementing them, and it reflects uncommitted changes.

That authority is also what makes enumeration a security boundary. ``ls-files``
reports the paths git has in its *index*, and an index path is not a promise
about the working-tree object it currently names: a committed directory replaced
by a symlink leaves the path in place and moves the object outside the repository
entirely. Every candidate is therefore admitted through a
:class:`~repoatlas.containment.RepositoryBoundary`, which decides from the real
filesystem rather than from the path (V1_SPEC §17).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from repoatlas.config.settings import DiscoverySettings
from repoatlas.containment import RepositoryBoundary
from repoatlas.discovery.finder import GIT_DIRECTORY
from repoatlas.discovery.ignore import (
    IGNORE_FILENAME,
    MAX_IGNORE_BYTES,
    IgnoreRules,
    is_secret_path,
)
from repoatlas.gitstate.client import GitClient, is_git_repository

_HASH_CHUNK_BYTES = 1 << 20


@dataclass(frozen=True, slots=True)
class ScannedFile:
    """A file present in the working tree, with its stat metadata."""

    rel_path: str
    size: int
    mtime_ns: int


def hash_bytes(data: bytes) -> str:
    return hashlib.blake2b(data, digest_size=32).hexdigest()


def hash_within(boundary: RepositoryBoundary, rel_path: str) -> str | None:
    """Content hash — the sole authority on whether a file changed (V1_SPEC §12).

    ``None`` when the file cannot be read *or* is not contained. A file can stop
    being readable between one sync and the next for entirely ordinary reasons —
    permissions change, a tracked file is deleted from the working tree while
    remaining in the git index, a network mount goes away — and one of them must
    not abort the repository.

    The caller treats an unknown hash as "changed", which is the same rule the
    indexer already follows: *we may skip work only when we can prove nothing
    changed* (ARCHITECTURE §9). The indexer then records the failure as a
    diagnostic, so the file is reported rather than silently dropped.

    It takes a boundary rather than a path deliberately. The previous signature
    accepted any ``Path``, which made "hash this file" expressible without
    answering "is this file ours" — and the two questions have to be asked
    together or the second one gets forgotten (ARCHITECTURE.md §12).
    """
    digest = hashlib.blake2b(digest_size=32)
    if not boundary.read_into(rel_path, digest.update):
        return None
    return digest.hexdigest()


def scan_repository(
    repo_path: Path,
    *,
    git: GitClient | None = None,
    settings: DiscoverySettings | None = None,
    managed_subrepos: frozenset[Path] = frozenset(),
    boundary: RepositoryBoundary | None = None,
    unreadable: list[Path] | None = None,
) -> list[ScannedFile]:
    """Enumerate the indexable files of one repository.

    Two mandatory filters run before anything else, in this order:

    1. the built-in obvious-secret policy, ahead of git's ignore handling, the
       built-in exclusion set and ``.repoatlasignore`` — which is what makes it
       mandatory rather than merely usually-applied, so a ``!.ENV`` negation in a
       user's ignore file cannot re-admit a file it claims (V1_SPEC §17);
    2. the containment boundary, which decides whether the path git reported
       actually names an object inside this repository.

    They answer different questions and neither substitutes for the other. The
    secret policy excludes *some in-repository files by name*; containment
    excludes *all out-of-repository content*, whatever it is called. A file such
    as ``sub/credentials`` reached through a symlinked ``sub`` is refused by
    containment alone — ``credentials`` is not in the secret patterns and never
    needed to be.

    Every decision is made before any content is read, so a file failing either
    filter is never opened and cannot reach chunks, embeddings, retrieval,
    prompts or logs (ARCHITECTURE.md §12).
    """
    config = settings or DiscoverySettings()
    client = git or GitClient()
    limit = boundary if boundary is not None else RepositoryBoundary(repo_path)
    ignore = _load_ignore_rules(limit)

    listed = client.list_files(repo_path)
    candidates: Iterator[str]
    if listed is not None:
        candidates = iter(listed)
    else:
        # Git unavailable or failed: fall back to walking the working tree. The
        # fallback enumerates the same admissible file set as `ls-files`, at any
        # depth — it used to stop at 64 and report success, so a repository with
        # deeper files was indexed incompletely with nothing said (ARCHITECTURE.md
        # §6). Directories it could not read are appended to `unreadable` rather
        # than passed over, because "nothing there" and "could not look" are
        # different claims and only one of them is true.
        candidates = _walk_relative(repo_path, config, unreadable=unreadable)

    results: list[ScannedFile] = []
    for rel_path in candidates:
        # Security policy first: not configurable, not negotiable, and decided
        # from the name alone so nothing below it ever sees the file.
        if is_secret_path(rel_path):
            continue
        if _is_excluded(rel_path, config):
            continue
        if ignore.matches(rel_path):
            continue
        if _belongs_to_nested_repository(rel_path, limit, managed_subrepos):
            continue

        # Containment decides from the filesystem, not from the path: a regular
        # file reached through a symlinked ancestor is refused here, before it is
        # opened. `stat` returns None for anything that is not a contained
        # regular file, which is every reason a candidate is unusable.
        info = limit.stat(rel_path)
        if info is None:
            continue

        results.append(ScannedFile(rel_path=rel_path, size=info.st_size, mtime_ns=info.st_mtime_ns))

    results.sort(key=lambda f: f.rel_path)
    return results


def _load_ignore_rules(boundary: RepositoryBoundary) -> IgnoreRules:
    """``.repoatlasignore`` read through the boundary like any other file.

    It is repository-supplied content, so it is subject to both of the same
    rules: a ``.repoatlasignore`` that is a symlink out of the tree is not read,
    and it is read under a byte ceiling. The second matters as much as the first
    — this is the *earliest* content read of a sync, before any file has been
    classified, so an unbounded read here would exhaust memory before the size
    cap protecting everything else was ever consulted.
    """
    found = boundary.read_content(IGNORE_FILENAME, max_bytes=MAX_IGNORE_BYTES)
    if not found.ok or found.data is None:
        # Absent, refused, or absurdly large: no rules rather than partial rules.
        return IgnoreRules()
    return IgnoreRules.from_text(found.data.decode("utf-8", errors="replace"))


def _is_excluded(rel_path: str, config: DiscoverySettings) -> bool:
    return any(part in config.exclusions for part in Path(rel_path).parts)


def _belongs_to_nested_repository(
    rel_path: str, boundary: RepositoryBoundary, managed_subrepos: frozenset[Path]
) -> bool:
    """Files inside a nested managed repository belong to it, not the outer one.

    Compared against **resolved** paths on both sides. Ownership is a question
    about directories rather than about the file being opened, so there is
    nothing to race here — but comparing one resolved path against one
    unresolved one would answer it wrongly whenever the root is a symlink.
    """
    if not managed_subrepos:
        return False
    absolute = boundary.root / rel_path
    for sub in managed_subrepos:
        try:
            resolved_sub = sub.expanduser().resolve()
        except OSError:
            continue
        if resolved_sub != boundary.root and absolute.is_relative_to(resolved_sub):
            return True
    return False


def _identity(path: Path) -> tuple[int, int] | None:
    """``(device, inode)`` — what makes two paths the same directory."""
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_dev, info.st_ino)


def _walk_relative(
    root: Path, config: DiscoverySettings, *, unreadable: list[Path] | None = None
) -> Iterator[str]:
    """Every admissible file beneath ``root``, at any depth. **Iterative.**

    This walk used to stop at ``depth > 64`` and return, which meant a repository
    nested deeper than that was indexed as though its lower files did not exist:
    no error, no diagnostic, a successful sync and an incomplete index. The cap
    was also not the safety property it was commented as being — a cycle is
    created by symlinks, which this walk has always refused, and the cost of a
    tree is its *size*, which a depth limit does not bound at all: a flat
    directory of 100,000 files costs exactly the same at depth 1 as at depth 100
    (ARCHITECTURE.md §6). It bounded the one dimension that was never the risk.

    **Termination is structural.** Symlinks are never followed, so no route out
    of the tree and no cycle through one exists; what remains is a finite acyclic
    directory tree, which a depth-first walk exhausts. Directory identity
    ``(st_dev, st_ino)`` is tracked as well, so a bind mount or any second route
    to a directory already visited is entered once rather than twice — the same
    rule repository discovery uses, and the reason neither needs a counter.

    **Iterative rather than recursive**, with an explicit stack. The previous
    implementation recursed through ``yield from`` once per directory level, so
    lifting the ceiling on its own would have swapped a silent truncation at
    depth 65 for a ``RecursionError`` somewhere near depth 1000 — a scanner must
    not require user-controlled nesting to fit the Python call stack. Depth-first
    rather than breadth-first because the stack then holds the unexplored
    siblings along one path instead of a whole level, which is the difference
    between bounded memory and a 100,000-entry frontier. Children are pushed
    reversed so they pop in lexicographic order: results are sorted by the caller
    regardless, but a walk that visits in a stable order is one whose diagnostics
    can be compared between runs.

    ``.git`` is refused **structurally**, not by configuration. It is in the
    default exclusions, but those are configurable, and a root whose
    ``exclusions`` were set without it had this walk enumerate the object store,
    the hooks and ``.git/config`` as ordinary source — content ``git ls-files``
    would never report, so the fallback was strictly less safe than the path it
    stands in for. Git internals are not repository source at any depth.
    """
    stack: list[Path] = [root]
    visited: set[tuple[int, int]] = set()
    origin = _identity(root)
    if origin is not None:
        visited.add(origin)

    while stack:
        current = stack.pop()
        try:
            # The nested-repository probe sits *inside* the guard deliberately. On
            # a directory the process can see but not enter, `(current / ".git")
            # .is_dir()` raises rather than answering — and when that probe ran
            # before the push, its `OSError` was caught by the per-entry handler
            # and the whole subtree was dropped with nothing recorded. One guard
            # covers both questions, so a directory that cannot be inspected is
            # reported instead of quietly becoming an empty one.
            nested = current != root and is_git_repository(current)
            entries = () if nested else sorted(current.iterdir())
        except OSError:
            # Includes PermissionError. Recorded rather than swallowed: an empty
            # result from a directory that could not be read is not evidence that
            # it held no files, and a sync reporting success on the strength of it
            # would claim a completeness it never established.
            if unreadable is not None:
                unreadable.append(current)
            continue
        if nested:
            continue  # a nested repository is enumerated as itself, separately

        descend: list[Path] = []
        blocked = False
        for entry in entries:
            if entry.name == GIT_DIRECTORY:
                continue
            if entry.name in config.exclusions:
                continue
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    descend.append(entry)
                elif entry.is_file():
                    yield entry.relative_to(root).as_posix()
            except OSError:
                # Listable but not stattable — a directory readable without being
                # searchable does this to every one of its children. The entry is
                # skipped, and the directory is named for the same reason an
                # unreadable one is: something admissible may be under it.
                blocked = True
                continue
        if blocked and unreadable is not None:
            unreadable.append(current)

        for child in reversed(descend):
            identity = _identity(child)
            if identity is None:
                # Classified as a directory a moment ago and now un-stattable: it
                # was removed mid-scan, or its permissions changed under us.
                # Either way this walk did not inspect it, and the last silent
                # way to drop a subtree is to decide that does not need saying.
                if unreadable is not None:
                    unreadable.append(child)
                continue
            if identity in visited:
                # A cycle, a bind mount, or two routes to one directory. Entering
                # it twice would duplicate everything beneath it.
                continue
            visited.add(identity)
            stack.append(child)
