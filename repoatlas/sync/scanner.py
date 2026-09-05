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
        # Git unavailable or failed: fall back to walking. Discovery still works.
        candidates = _walk_relative(repo_path, repo_path, config, depth=0)

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


def _walk_relative(
    current: Path, root: Path, config: DiscoverySettings, depth: int
) -> Iterator[str]:
    if depth > 64:  # guard against pathological or symlink-induced depth
        return
    try:
        entries = sorted(current.iterdir())
    except (OSError, PermissionError):
        return
    for entry in entries:
        if entry.name in config.exclusions:
            continue
        try:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if is_git_repository(entry) and entry != root:
                    continue  # a nested repository is enumerated separately
                yield from _walk_relative(entry, root, config, depth + 1)
            elif entry.is_file():
                yield entry.relative_to(root).as_posix()
        except OSError:
            continue
