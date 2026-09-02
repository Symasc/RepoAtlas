"""Hardened read-only git access (ARCHITECTURE.md §8).

Repositories are untrusted input. Every invocation here:

* runs the ``git`` CLI as an argument array — never a shell string, so repository
  content can never be interpolated into a command;
* disables hooks, so no repository-supplied script can execute;
* is restricted to a fixed allowlist of read-only plumbing subcommands;
* has a timeout, and treats any failure as "state unknown" rather than raising.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: Read-only plumbing only. Adding to this list is a security decision.
_ALLOWED_SUBCOMMANDS = frozenset({"rev-parse", "status", "ls-files"})

#: Applied to every invocation (ARCHITECTURE.md §8).
_HARDENING = (
    "--no-optional-locks",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "safe.directory=*",
    "-c",
    "core.fsmonitor=false",
)

_TIMEOUT_SECONDS = 30.0


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
        try:
            completed = subprocess.run(  # noqa: S603 - fixed binary, argument array, no shell
                [self._git, *_HARDENING, *args],
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_SECONDS,
                check=False,
                env={
                    "GIT_TERMINAL_PROMPT": "0",
                    "GIT_OPTIONAL_LOCKS": "0",
                    "PATH": "/usr/bin:/bin",
                },
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
        status = self._run(repo_path, "status", "--porcelain")

        if not branch.ok and not head.ok and not status.ok:
            return GitRepositoryState(known=False)

        branch_name = branch.stdout.strip() or None
        if branch_name == "HEAD":  # detached
            branch_name = "HEAD (detached)"

        return GitRepositoryState(
            branch=branch_name,
            # An unborn branch has no HEAD; that is a valid, known state.
            head_sha=head.stdout.strip() or None,
            is_dirty=bool(status.stdout.strip()) if status.ok else False,
            known=True,
        )

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
