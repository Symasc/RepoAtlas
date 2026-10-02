"""Recursive Git repository discovery (V1_SPEC §5).

**Recursive means recursive.** ``init`` promises to find every Git repository
beneath the managed root, and the walk had a hard-coded ceiling of six levels
that no document mentioned and no diagnostic reported: a repository at depth 6
was registered, an identical one at depth 7 was silently absent, and ``init``
still exited 0 saying how many it had "discovered". A monorepo whose services
sit one level deeper than the constant assumed was simply not indexed, and
nothing said so. There is also no manual-registration escape hatch — discovery
is the *only* way a repository becomes managed — so the omission was total.

The constant was a convenience bound from the first implementation. It was not
protecting performance (the cost of a walk is the number of directories in the
tree, which a depth cap does not bound — a flat root with 100,000 children costs
the same at depth 1 as at depth 100), and it was not protecting against cycles
either, except by accident: symlinked directories are not followed by default,
so no cycle can arise on the default path at all. It is gone rather than raised,
because there is no value it could have that is correct — any finite ceiling is
the same defect with a different number, and a *configurable* one would let an
operator re-create it silently.

Termination is therefore structural, not numeric:

* **directory symlinks are not followed** (the default), so the walk can only
  descend through real directories, and a real directory tree is finite;
* a followed symlink — opt-in via ``follow_symlinks`` — must still resolve
  *inside* the managed root, so it cannot escape containment;
* every directory entered is recorded by ``(st_dev, st_ino)`` and never entered
  twice, so a cycle reached through any route — symlink, bind mount, hard-linked
  directory — stops the first time it closes.

The walk is **iterative**, with an explicit stack. Correctness must not depend on
the size of the call stack: the tree is untrusted input, and a recursive walker
with the ceiling removed would trade a silent truncation at depth 6 for a
``RecursionError`` at depth ~1000.

What did *not* change: the default exclusion set, ``.repoatlasignore`` matching,
nested repositories staying separate repositories, the root-relative slug, and
the deterministic ordering of the result.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from repoatlas.config.settings import DiscoverySettings
from repoatlas.discovery.ignore import IgnoreRules
from repoatlas.gitstate.client import is_git_repository
from repoatlas.models.repository import RepositoryCandidate, repository_slug

#: Never descended into, whatever ``exclusions`` is configured to. A repository's
#: object database is thousands of directories of content that is not source, and
#: recognizing ``.git`` is precisely what makes the directory holding it a
#: repository — walking inside it after that is pure cost. This is separate from
#: the configurable exclusion list because removing it there must not turn a
#: preference into a pathological walk (V1_SPEC §5).
GIT_DIRECTORY = ".git"

#: The file every Python virtual environment holds at its top (``venv``,
#: ``virtualenv``, ``uv``), whatever the directory is called. A virtual environment
#: is installed packages, never a project: thousands of directories to walk and no
#: repository to find, so the walk never enters one. A directory that is also a
#: Git repository is still a repository.
VIRTUAL_ENVIRONMENT_MARKER = "pyvenv.cfg"


def discover_repositories(
    root: Path,
    settings: DiscoverySettings | None = None,
    root_ignore: IgnoreRules | None = None,
    *,
    unreadable: list[Path] | None = None,
) -> list[RepositoryCandidate]:
    """Find every Git repository beneath ``root``, at any depth.

    A repository nested inside another is a separate managed repository, so the
    walk continues *into* a discovered repository looking for more (V1_SPEC §5).

    Directories the operating system refuses are skipped rather than fatal — one
    unreadable subtree must not stop a whole workspace from being indexed — and
    are appended to ``unreadable`` when a caller supplies a list, so "we found
    nothing there" can be told apart from "we could not look".
    """
    config = settings or DiscoverySettings()
    ignore = root_ignore if root_ignore is not None else IgnoreRules.load(root)
    resolved_root = root.resolve()

    found: dict[Path, RepositoryCandidate] = {}
    for path in _walk(resolved_root, config, ignore, unreadable):
        if path in found:  # dedup: never index the same repository twice
            continue
        found[path] = RepositoryCandidate(
            name=path.name, slug=repository_slug(path, resolved_root), path=path
        )

    return sorted(found.values(), key=lambda c: str(c.path))


def _identity(path: Path) -> tuple[int, int] | None:
    """``(device, inode)`` — what makes two paths the same directory."""
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_dev, info.st_ino)


def _walk(
    root: Path,
    config: DiscoverySettings,
    ignore: IgnoreRules,
    unreadable: list[Path] | None,
) -> Iterator[Path]:
    """Depth-first over real directories, bounded by the tree rather than a number.

    Depth-first rather than breadth-first because the stack then holds the
    unexplored siblings along one path instead of a whole level, which is the
    difference between bounded memory and holding a 100,000-entry frontier.
    Children are pushed in reverse so they pop in lexicographic order: the
    returned list is sorted regardless, but a walk that visits in a stable order
    is one whose diagnostics can be compared between runs.
    """
    stack: list[Path] = [root]
    visited: set[tuple[int, int]] = set()
    identity = _identity(root)
    if identity is not None:
        visited.add(identity)

    while stack:
        current = stack.pop()

        try:
            repository = is_git_repository(current)
            entries = sorted(current.iterdir())
        except OSError:
            # Includes PermissionError, and the `.git` probe is inside the guard
            # for a reason: on a directory the process may see but not enter,
            # `(current / ".git").is_dir()` raises rather than answering, and it
            # used to do so straight out of the walk — one mode-000 directory
            # anywhere under the root turned `init` into a traceback at exit 1.
            # Recorded rather than swallowed: an empty result from a directory we
            # could not read is not evidence that it held no repositories.
            if unreadable is not None:
                unreadable.append(current)
            continue

        if (
            current != root
            and not repository
            and any(entry.name == VIRTUAL_ENVIRONMENT_MARKER for entry in entries)
        ):
            # Read from the listing already taken, so recognizing a virtual
            # environment costs no extra filesystem call.
            continue

        if repository:
            yield current

        for entry in reversed(entries):
            child = _descendable(entry, root, config, ignore)
            if child is None:
                continue
            identity = _identity(child)
            if identity is None:
                continue
            if identity in visited:
                # A cycle, a bind mount, or two routes to one directory. Entering
                # it again would either loop forever or report the same
                # repository twice under a second identity.
                continue
            visited.add(identity)
            stack.append(child)


def _descendable(
    entry: Path,
    root: Path,
    config: DiscoverySettings,
    ignore: IgnoreRules,
) -> Path | None:
    """``entry`` if the walk may descend into it, else ``None``."""
    if entry.name == GIT_DIRECTORY:
        return None
    if entry.name.startswith("."):
        # Hidden directories hold tools, caches and settings (`~/.nvm`, `~/.cache`,
        # `~/.vscode-server`), not projects: walking them is the bulk of a home
        # directory's cost, and tools installed as Git clones (`~/.oh-my-zsh`)
        # would be adopted as repositories. This decides only where repositories
        # are looked for; a repository's own hidden directories (`.github`,
        # `.circleci`) are enumerated by Git and indexed as before.
        return None
    if entry.name in config.exclusions:
        return None

    try:
        symlinked = entry.is_symlink()
        if symlinked and not config.follow_symlinks:
            # Not followed by default: a symlink out of the tree would place
            # foreign files under a repository's identity.
            return None
        if not entry.is_dir():
            return None
    except OSError:
        return None

    try:
        rel = entry.relative_to(root).as_posix()
    except ValueError:  # pragma: no cover - entries are built by descending root
        return None
    if ignore.matches(rel, is_dir=True):
        return None

    if symlinked:
        # Only a symlink can leave the tree, so only a symlink is resolved — and
        # containment is decided on path *components*, never on a string prefix,
        # so `root/foobar` is not inside `root/foo`.
        try:
            target = entry.resolve(strict=True)
        except OSError:
            return None
        if target != root and not target.is_relative_to(root):
            return None

    return entry
