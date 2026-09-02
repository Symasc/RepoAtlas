"""Recursive Git repository discovery (V1_SPEC §5)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from repoatlas.config.settings import DiscoverySettings
from repoatlas.discovery.ignore import IgnoreRules
from repoatlas.gitstate.client import is_git_repository
from repoatlas.models.repository import RepositoryCandidate


def discover_repositories(
    root: Path,
    settings: DiscoverySettings | None = None,
    root_ignore: IgnoreRules | None = None,
) -> list[RepositoryCandidate]:
    """Find every Git repository beneath ``root``.

    A repository nested inside another is a separate managed repository, so the
    walk continues *into* a discovered repository looking for more (V1_SPEC §5).
    Descent is bounded by ``max_depth`` and skips the default exclusion set.
    """
    config = settings or DiscoverySettings()
    ignore = root_ignore if root_ignore is not None else IgnoreRules.load(root)
    resolved_root = root.resolve()

    found: dict[Path, RepositoryCandidate] = {}
    for path in _walk(resolved_root, resolved_root, config, ignore, depth=0):
        if path in found:  # dedup: never index the same repository twice
            continue
        found[path] = RepositoryCandidate(name=path.name, path=path)

    return sorted(found.values(), key=lambda c: str(c.path))


def _walk(
    current: Path,
    root: Path,
    config: DiscoverySettings,
    ignore: IgnoreRules,
    depth: int,
) -> Iterator[Path]:
    if depth > config.max_depth:
        return

    if is_git_repository(current):
        yield current

    try:
        entries = sorted(current.iterdir())
    except (OSError, PermissionError):
        return

    for entry in entries:
        try:
            if not entry.is_dir():
                continue
            if entry.is_symlink() and not config.follow_symlinks:
                # Not followed by default: a symlink out of the tree would place
                # foreign files under a repository's identity.
                continue
        except OSError:
            continue

        if entry.name in config.exclusions:
            continue

        rel = entry.relative_to(root).as_posix()
        if ignore.matches(rel, is_dir=True):
            continue

        yield from _walk(entry, root, config, ignore, depth + 1)
