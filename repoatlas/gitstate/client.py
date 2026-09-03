"""Hardened read-only git access (ARCHITECTURE.md §8).

Repositories are untrusted input, and a repository directory is not only its
tracked files: it is also ``.git/config`` and ``.gitattributes``. Several git
features let those two together name a program for git to run — which makes
"which git subcommand may I use" a security question, not a convenience one.

Every invocation here:

* runs the ``git`` CLI as an argument array — never a shell string, so repository
  content can never be interpolated into a command;
* is restricted to a fixed allowlist of subcommands that **cannot reach the
  content-filter machinery**, and rejects the options that would make an allowed
  subcommand reach it anyway;
* neutralizes the process-spawning configuration that applies to those
  subcommands, and ignores system and per-user git configuration entirely;
* has a timeout, and treats any failure as "state unknown" rather than raising.

**Nothing here compares working-tree content to the index by asking git.** That
comparison is precisely what makes git apply a ``filter.<driver>.clean`` or
``filter.<driver>.process`` command, and both ``git status`` and
``git ls-files -m`` were measured executing repository-supplied shell through it.
The comparison is done in :func:`_worktree_differs_from_index` instead, from the
index blob hashes, which git will hand over without running anything.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from repoatlas.security import is_secret_path

#: Subcommands that read refs, the object database or the index, and never
#: compare a working-tree file against it. Adding to this list is a security
#: decision: the test is not "is it read-only" but "can it spawn a process".
_ALLOWED_SUBCOMMANDS = frozenset({"rev-parse", "ls-files", "ls-tree"})

#: Options that make an allowed subcommand compare working-tree content — and so
#: run a clean filter. ``ls-files -m`` executes repository-supplied shell exactly
#: as ``git status`` did; the subcommand allowlist alone would not have caught it.
_FORBIDDEN_OPTIONS = frozenset({"-m", "--modified", "-k", "--killed"})

#: Applied to every invocation (ARCHITECTURE.md §8).
#:
#: ``core.hooksPath`` and ``core.fsmonitor`` are the two keys that can name a
#: program for the subcommands above. Neither is reachable by them today — this
#: is defence in depth against a future git that widens when they are consulted.
_HARDENING = (
    "--no-pager",
    "--no-optional-locks",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "safe.directory=*",
    "-c",
    "core.fsmonitor=false",
)

#: The whole environment for a git call. `HOME` is absent, so no per-user config
#: is read; the explicit `/dev/null` overrides make that true rather than
#: incidental, and cover the case of a future git that finds a config another way.
_ENVIRONMENT = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_ATTR_NOSYSTEM": "1",
    "PATH": "/usr/bin:/bin",
}

_TIMEOUT_SECONDS = 30.0

#: Index entry modes that have working-tree content to compare.
_MODE_SYMLINK = "120000"
_MODE_GITLINK = "160000"

_READ_CHUNK_BYTES = 1 << 20


@dataclass(frozen=True, slots=True)
class GitResult:
    ok: bool
    stdout: str


@dataclass(frozen=True, slots=True)
class GitRepositoryState:
    """Branch, HEAD and dirty state. ``None``/``False`` means "unknown"."""

    branch: str | None = None
    head_sha: str | None = None
    is_dirty: bool = False
    known: bool = False


class GitClient:
    """The only place in RepoAtlas that invokes git."""

    def __init__(self, git_binary: str | None = None) -> None:
        self._git = git_binary or shutil.which("git")

    @property
    def available(self) -> bool:
        return self._git is not None

    def _run(self, cwd: Path, *args: str) -> GitResult:
        if self._git is None:
            return GitResult(ok=False, stdout="")
        if not args or args[0] not in _ALLOWED_SUBCOMMANDS:
            raise ValueError(f"git subcommand not permitted: {args[0] if args else '<none>'}")
        forbidden = sorted(_FORBIDDEN_OPTIONS.intersection(args[1:]))
        if forbidden:
            raise ValueError(f"git option not permitted: {', '.join(forbidden)}")
        try:
            completed = subprocess.run(  # noqa: S603 - fixed binary, argument array, no shell
                [self._git, *_HARDENING, *args],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_SECONDS,
                check=False,
                env=dict(_ENVIRONMENT),
            )
        except (OSError, subprocess.SubprocessError):
            return GitResult(ok=False, stdout="")
        if completed.returncode != 0:
            return GitResult(ok=False, stdout="")
        return GitResult(ok=True, stdout=completed.stdout)

    def toplevel(self, path: Path) -> Path | None:
        """The working-tree root of the repository containing ``path``."""
        result = self._run(path, "rev-parse", "--show-toplevel")
        if not result.ok:
            return None
        text = result.stdout.strip()
        return Path(text).resolve() if text else None

    def state(self, repo_path: Path) -> GitRepositoryState:
        """Current branch, HEAD SHA and dirty state of the working tree."""
        head = self._run(repo_path, "rev-parse", "HEAD")
        branch = self._run(repo_path, "rev-parse", "--abbrev-ref", "HEAD")
        dirty = self._dirty(repo_path)

        if not branch.ok and not head.ok and dirty is None:
            return GitRepositoryState(known=False)

        branch_name = branch.stdout.strip() or None
        if branch_name == "HEAD":  # detached
            branch_name = "HEAD (detached)"

        return GitRepositoryState(
            branch=branch_name,
            # An unborn branch has no HEAD; that is a valid, known state.
            head_sha=head.stdout.strip() or None,
            is_dirty=bool(dirty),
            known=True,
        )

    def _dirty(self, repo_path: Path) -> bool | None:
        """Whether the working tree differs from HEAD, without asking git to look.

        ``git status`` answers this in one call, and that call is exactly what
        cannot be made: deciding whether a file still matches the index means
        re-hashing it *through the repository's clean filter*, so a hostile
        ``.git/config`` gets to run a program (V1_SPEC §17). The same is true of
        ``git ls-files -m``.

        So the question is decomposed into the differences git itself reports,
        each answerable from refs, the object database and the index — none of
        which involves reading working-tree content through git:

        1. an untracked, non-ignored file exists;
        2. a tracked file is missing from the working tree;
        3. the index differs from HEAD (a staged change);
        4. a tracked file's content differs from its index blob — compared here,
           against hashes git already recorded.

        ``None`` means git could not answer at all, which is not the same as
        "clean" and is reported as unknown rather than guessed.
        """
        others = self._run(repo_path, "ls-files", "--others", "--exclude-standard", "-z")
        if not others.ok:
            return None
        if others.stdout.strip("\0").strip():
            return True

        deleted = self._run(repo_path, "ls-files", "--deleted", "-z")
        if deleted.ok and deleted.stdout.strip("\0").strip():
            return True

        staged = self._run(repo_path, "ls-files", "--stage", "-z")
        if not staged.ok:
            return None
        entries = _parse_index_entries(staged.stdout)

        if _staged_differs_from_head(entries, self._run(repo_path, "ls-tree", "-r", "-z", "HEAD")):
            return True

        return _worktree_differs_from_index(repo_path, entries)

    def list_files(self, repo_path: Path) -> list[str] | None:
        """Tracked plus untracked-not-ignored files, relative to the repo root.

        This is exactly the working tree V1_SPEC §5 declares authoritative, and it
        gives real ``.gitignore`` semantics without reimplementing them. Returns
        ``None`` when git cannot answer, so the caller can fall back to walking.
        """
        result = self._run(
            repo_path, "ls-files", "--cached", "--others", "--exclude-standard", "-z"
        )
        if not result.ok:
            return None
        return [entry for entry in result.stdout.split("\0") if entry]


def _parse_index_entries(raw: str) -> list[tuple[str, str, str]]:
    """``ls-files --stage -z`` output as ``(mode, blob_sha, rel_path)``.

    Each record is ``<mode> <sha> <stage>\\t<path>``. A malformed record is
    skipped rather than raising: this feeds a boolean, and a parse failure must
    not take down a sync.
    """
    entries: list[tuple[str, str, str]] = []
    for record in raw.split("\0"):
        if not record:
            continue
        meta, _, rel_path = record.partition("\t")
        fields = meta.split()
        if not rel_path or len(fields) < 3:
            continue
        entries.append((fields[0], fields[1], rel_path))
    return entries


def _staged_differs_from_head(entries: list[tuple[str, str, str]], head: GitResult) -> bool:
    """Whether the index differs from the HEAD tree — i.e. something is staged.

    ``ls-tree`` reads the object database and never touches the working tree, so
    this comparison cannot reach a filter. An unborn branch has no HEAD tree, in
    which case any index entry at all is a staged addition.
    """
    if not head.ok:
        return bool(entries)

    tree: set[tuple[str, str, str]] = set()
    for record in head.stdout.split("\0"):
        if not record:
            continue
        meta, _, rel_path = record.partition("\t")
        fields = meta.split()
        if not rel_path or len(fields) < 3:
            continue
        tree.add((fields[0], fields[2], rel_path))

    return {(mode, sha, path) for mode, sha, path in entries} != tree


def _blob_hash(path: Path, mode: str) -> str | None:
    """Git's object name for what is on disk at ``path``.

    Git blob hashing is ``sha1("blob <length>\\0" + content)``, which is a content
    address rather than a security primitive — the algorithm is git's choice, not
    ours. A symlink hashes its target, which is what git stores for mode 120000.
    """
    digest = hashlib.sha1(usedforsecurity=False)
    try:
        if mode == _MODE_SYMLINK:
            payload = str(path.readlink()).encode("utf-8")
            digest.update(b"blob %d\0" % len(payload))
            digest.update(payload)
            return digest.hexdigest()

        size = path.stat().st_size
        digest.update(b"blob %d\0" % size)
        with path.open("rb") as handle:
            while block := handle.read(_READ_CHUNK_BYTES):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def _worktree_differs_from_index(repo_path: Path, entries: list[tuple[str, str, str]]) -> bool:
    """Whether any tracked file's content differs from the blob git recorded.

    This is the comparison ``git status`` would make, done without asking git to
    make it. Two consequences are worth stating plainly, because both are
    deliberate:

    * A repository that uses a **real** clean filter, or CRLF normalization,
      stores a blob that is not byte-identical to the file on disk, so this
      reports dirty where git would report clean. That is conservative in the
      safe direction — ARCHITECTURE §9's rule is that work may be skipped only
      when nothing changed can be *proven*, and here it cannot.
    * A mode-only change (``chmod +x``) is not content, so it is not reported.
      No fact RepoAtlas stores depends on it.

    Stops at the first difference: the answer is a boolean, and a dirty tree is
    usually dirty early.

    Files the built-in secret policy claims are **skipped without being read**.
    Answering "is this tree dirty?" is not a reason to open a private key: the
    guarantee is that such a file is never read, and it would be a strange
    guarantee that held everywhere except the one place that hashes every tracked
    file (V1_SPEC §17). The narrowed meaning is the useful one anyway — an edit to
    an excluded file cannot change a single fact RepoAtlas stores, so counting it
    as dirtiness would force a rehash that can only reproduce what is already
    there.
    """
    for mode, sha, rel_path in entries:
        if mode == _MODE_GITLINK:  # a submodule has no blob in this repository
            continue
        if is_secret_path(rel_path):
            continue
        absolute = repo_path / rel_path
        if _blob_hash(absolute, mode) != sha:
            return True
    return False


def is_git_repository(path: Path) -> bool:
    """True when ``path`` is a Git working-tree root.

    Checks for ``.git`` directly rather than shelling out: discovery calls this for
    every directory it walks, and a subprocess per directory would dominate runtime.
    A ``.git`` file (worktree or submodule) counts.
    """
    marker = path / ".git"
    return marker.is_dir() or marker.is_file()


def git_toplevel(path: Path, client: GitClient | None = None) -> Path | None:
    """Locate the enclosing Git repository root by walking upward.

    Used by context resolution to detect an unmanaged repository (V1_SPEC §6). Pure
    filesystem inspection, so it works even when the git binary is missing.
    """
    del client  # accepted for symmetry with GitClient.toplevel; unused by design
    for candidate in (path, *path.parents):
        if is_git_repository(candidate):
            return candidate
    return None
