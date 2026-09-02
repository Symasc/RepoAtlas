"""Working-tree file enumeration (ARCHITECTURE.md §6, §8).

The working tree is authoritative, so enumeration prefers ``git ls-files``: it
returns tracked plus untracked-not-ignored files, giving real ``.gitignore``
semantics without reimplementing them, and it reflects uncommitted changes.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from repoatlas.config.settings import DiscoverySettings
from repoatlas.discovery.ignore import IgnoreRules, is_secret_path
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


def hash_file(path: Path) -> str | None:
    """Content hash — the sole authority on whether a file changed (V1_SPEC §12).

    ``None`` when the file cannot be read. A file can stop being readable between
    one sync and the next for entirely ordinary reasons — permissions change, a
    tracked file is deleted from the working tree while remaining in the git index,
    a network mount goes away — and one of them must not abort the repository.

    The caller treats an unknown hash as "changed", which is the same rule the
    indexer already follows: *we may skip work only when we can prove nothing
    changed* (ARCHITECTURE §9). The indexer then records the failure as a
    diagnostic, so the file is reported rather than silently dropped.
    """
    digest = hashlib.blake2b(digest_size=32)
    try:
        with path.open("rb") as handle:
            while block := handle.read(_HASH_CHUNK_BYTES):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def scan_repository(
    repo_path: Path,
    *,
    git: GitClient | None = None,
    settings: DiscoverySettings | None = None,
    managed_subrepos: frozenset[Path] = frozenset(),
) -> list[ScannedFile]:
    """Enumerate the indexable files of one repository.

    Applies, in order: git's own ignore handling, the built-in exclusion set,
    ``.repoatlasignore``, and finally secret patterns. Secrets are filtered here,
    before any content is read, so they cannot reach chunks, embeddings, prompts
    or logs (V1_SPEC §17).
    """
    config = settings or DiscoverySettings()
    client = git or GitClient()
    ignore = IgnoreRules.load(repo_path)

    listed = client.list_files(repo_path)
    candidates: Iterator[str]
    if listed is not None:
        candidates = iter(listed)
    else:
        # Git unavailable or failed: fall back to walking. Discovery still works.
        candidates = _walk_relative(repo_path, repo_path, config, depth=0)

    results: list[ScannedFile] = []
    for rel_path in candidates:
        if _is_excluded(rel_path, config):
            continue
        if ignore.matches(rel_path):
            continue
        if is_secret_path(rel_path):
            continue

        absolute = repo_path / rel_path
        if _belongs_to_nested_repository(absolute, repo_path, managed_subrepos):
            continue

        try:
            if absolute.is_symlink() or not absolute.is_file():
                continue
            stat = absolute.stat()
        except OSError:
            continue

        results.append(ScannedFile(rel_path=rel_path, size=stat.st_size, mtime_ns=stat.st_mtime_ns))

    results.sort(key=lambda f: f.rel_path)
    return results


def _is_excluded(rel_path: str, config: DiscoverySettings) -> bool:
    return any(part in config.exclusions for part in Path(rel_path).parts)


def _belongs_to_nested_repository(
    absolute: Path, repo_path: Path, managed_subrepos: frozenset[Path]
) -> bool:
    """Files inside a nested managed repository belong to it, not the outer one."""
    return any(sub != repo_path and absolute.is_relative_to(sub) for sub in managed_subrepos)


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
