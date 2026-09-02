"""The Git trust boundary (V1_SPEC §17, ARCHITECTURE.md §8).

    Indexed repositories are untrusted data. RepoAtlas static inspection must
    never execute repository-controlled code.

A repository directory is not only its tracked files: it is also `.git/config`
and `.gitattributes`, and several Git features let those two together name a
program that Git runs on RepoAtlas's behalf. That is not a hypothetical — the
combination below executed arbitrary shell during `repoatlas init` before the
`GitClient` allowlist was narrowed to commands that cannot reach the filter
machinery.

Every test here uses a **real** temporary repository, a **real** Git process and
a harmless canary (an empty file in `tmp_path`). Nothing is mocked: the point is
to observe what the installed Git actually does, because that — not our reading
of its documentation — is what decides whether the boundary holds.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from repoatlas.gitstate.client import GitClient
from repoatlas.sync.scanner import scan_repository
from tests.conftest import git

CLI = Path(sys.executable).parent / "repoatlas"

pytestmark = pytest.mark.adversarial


class Canary:
    """A file a hostile Git configuration would create if Git ran its command."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @property
    def command(self) -> str:
        # `cat` keeps the filter protocol honest for `clean`, so Git gets a
        # plausible answer rather than failing for an uninteresting reason.
        return f"sh -c 'touch {self.path}; cat'"

    @property
    def fired(self) -> bool:
        return self.path.exists()

    def reset(self) -> None:
        self.path.unlink(missing_ok=True)


@pytest.fixture
def canary(tmp_path: Path) -> Canary:
    return Canary(tmp_path / "CANARY_REPOSITORY_CODE_EXECUTED")


@pytest.fixture
def hostile_root(tmp_path: Path) -> Path:
    """A managed root whose single repository ships a weaponized `.git/`."""
    root = tmp_path / "root"
    (root / "hostile").mkdir(parents=True)
    return root


def _build(repo: Path, *, attributes: str | None = None) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "config.py").write_text('import os\nHOST = os.environ["SERVICE_HOST"]\n')
    if attributes is not None:
        (repo / ".gitattributes").write_text(attributes)
    git(repo, "init", "--quiet", "--initial-branch=main")
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", "initial")


def _dirty(repo: Path) -> None:
    """Make the working tree differ, which is what makes Git consult a filter.

    A clean tree is compared by stat alone, so a hostile filter would never be
    reached and the test would pass for the wrong reason.
    """
    (repo / "config.py").write_text('import os\nHOST = os.environ["SERVICE_HOST"]  # edited\n')


def _configure(repo: Path, key: str, value: str) -> None:
    """Write repository-local config, exactly as a handed-over `.git/` would."""
    git(repo, "config", key, value)


# --- the reproduction, through the installed CLI ------------------------------


@pytest.mark.skipif(not CLI.exists(), reason="the repoatlas console script is not installed")
@pytest.mark.parametrize("driver", ["clean", "process"])
def test_a_repository_filter_never_runs_during_init(
    hostile_root: Path, canary: Canary, tmp_path: Path, driver: str
) -> None:
    """The P0 reproduction: `.gitattributes` + `.git/config` must not execute.

    Driven through the real console script because that is the path a user takes;
    an in-process call would not prove that the installed entry point is safe.
    """
    repo = hostile_root / "hostile"
    _build(repo, attributes="* filter=pwn\n")
    _configure(repo, f"filter.pwn.{driver}", canary.command)
    _dirty(repo)
    canary.reset()

    state = tmp_path / "state"
    state.mkdir()
    done = subprocess.run(
        [str(CLI), "init", str(hostile_root)],
        cwd=hostile_root,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(state),
            "REPOATLAS_CONFIG": str(state / "config.yaml"),
            "REPOATLAS_DB": str(state / "atlas.db"),
        },
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert not canary.fired, (
        f"repository-controlled filter.{driver} executed during `repoatlas init`\n{done.stdout}"
    )


# --- every Git operation RepoAtlas performs, individually ---------------------

#: Each entry runs one RepoAtlas Git operation against `repo`.
OPERATIONS: dict[str, Callable[[GitClient, Path], object]] = {
    "state": lambda client, repo: client.state(repo),
    "list_files": lambda client, repo: client.list_files(repo),
    "toplevel": lambda client, repo: client.toplevel(repo),
    "scan_repository": lambda client, repo: scan_repository(repo, git=client),
}

#: Repository-local configurations that name a program for Git to run. Each is a
#: real Git feature, not an invented one.
HOSTILE_CONFIGURATIONS: dict[str, tuple[str | None, str]] = {
    # Content filters: reached by anything that compares the working tree to the
    # index. This pair is what made the P0 exploitable.
    "filter-clean": ("* filter=pwn\n", "filter.pwn.clean"),
    "filter-process": ("* filter=pwn\n", "filter.pwn.process"),
    "filter-smudge": ("* filter=pwn\n", "filter.pwn.smudge"),
    # Diff drivers: only a diff would reach these, and RepoAtlas runs none.
    "diff-external": (None, "diff.external"),
    "diff-textconv": ("* diff=pwn\n", "diff.pwn.textconv"),
    # Process-spawning config unrelated to content.
    "fsmonitor": (None, "core.fsmonitor"),
    "pager": (None, "core.pager"),
    "credential-helper": (None, "credential.helper"),
}


@pytest.mark.parametrize("operation", sorted(OPERATIONS))
@pytest.mark.parametrize("configuration", sorted(HOSTILE_CONFIGURATIONS))
def test_no_git_operation_executes_repository_configuration(
    tmp_path: Path, canary: Canary, operation: str, configuration: str
) -> None:
    """No RepoAtlas Git call may run a program named by the repository."""
    attributes, key = HOSTILE_CONFIGURATIONS[configuration]
    repo = tmp_path / "repo"
    _build(repo, attributes=attributes)
    _configure(repo, key, canary.command)
    _dirty(repo)
    canary.reset()

    OPERATIONS[operation](GitClient(), repo)

    assert not canary.fired, f"{key} executed during GitClient.{operation}"


@pytest.mark.parametrize("operation", sorted(OPERATIONS))
def test_no_git_operation_runs_a_repository_hook(
    tmp_path: Path, canary: Canary, operation: str
) -> None:
    """Hook neutralization must keep holding (ARCHITECTURE.md §8)."""
    repo = tmp_path / "repo"
    _build(repo)
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    for name in ("post-index-change", "pre-auto-gc", "post-checkout"):
        hook = hooks / name
        hook.write_text(f"#!/bin/sh\ntouch {canary.path}\n")
        hook.chmod(0o755)
    _dirty(repo)
    canary.reset()

    OPERATIONS[operation](GitClient(), repo)

    assert not canary.fired, f"a repository hook executed during GitClient.{operation}"


@pytest.mark.parametrize("operation", sorted(OPERATIONS))
def test_a_repository_alias_cannot_shadow_the_commands_repoatlas_runs(
    tmp_path: Path, canary: Canary, operation: str
) -> None:
    """Aliases never override built-ins, and RepoAtlas runs only built-ins.

    Asserted rather than assumed: the allowlist is only a security control if a
    repository cannot redefine what the allowed words mean.
    """
    repo = tmp_path / "repo"
    _build(repo)
    for command in ("status", "ls-files", "rev-parse"):
        _configure(repo, f"alias.{command}", f"!{canary.command}")
    _dirty(repo)
    canary.reset()

    OPERATIONS[operation](GitClient(), repo)

    assert not canary.fired, f"a repository alias executed during GitClient.{operation}"


def test_the_allowlist_admits_no_command_that_compares_content(tmp_path: Path) -> None:
    """The structural half of the guarantee.

    Filters are reached by *any* command that must decide whether a working-tree
    file still matches the index — `git status` and `git ls-files -m` both do, and
    both executed the canary before this fix. Keeping such a command out of the
    allowlist is what makes the boundary a property of the design rather than of
    remembering to pass the right flags.
    """
    repo = tmp_path / "repo"
    _build(repo)
    client = GitClient()

    with pytest.raises(ValueError, match="not permitted"):
        client._run(repo, "status", "--porcelain")
    with pytest.raises(ValueError, match="not permitted"):
        client._run(repo, "diff", "--quiet")


# --- the functionality the fix must not cost ----------------------------------


def test_dirty_state_is_still_detected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _build(repo)
    client = GitClient()

    assert client.state(repo).is_dirty is False
    _dirty(repo)
    assert client.state(repo).is_dirty is True


def test_an_untracked_file_makes_the_tree_dirty(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _build(repo)
    client = GitClient()
    assert client.state(repo).is_dirty is False

    (repo / "new.py").write_text("x = 1\n")
    assert client.state(repo).is_dirty is True


def test_an_ignored_file_does_not_make_the_tree_dirty(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _build(repo)
    (repo / ".gitignore").write_text("build/\n")
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", "ignore")
    client = GitClient()
    assert client.state(repo).is_dirty is False

    (repo / "build").mkdir()
    (repo / "build" / "out.js").write_text("noise\n")
    assert client.state(repo).is_dirty is False


def test_a_deleted_tracked_file_makes_the_tree_dirty(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _build(repo)
    client = GitClient()
    assert client.state(repo).is_dirty is False

    (repo / "config.py").unlink()
    assert client.state(repo).is_dirty is True


def test_a_staged_change_makes_the_tree_dirty(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _build(repo)
    _dirty(repo)
    git(repo, "add", "-A")
    assert GitClient().state(repo).is_dirty is True


def test_branch_and_head_are_still_reported(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _build(repo)
    state = GitClient().state(repo)

    assert state.known is True
    assert state.branch == "main"
    assert state.head_sha is not None
    assert len(state.head_sha) == 40


def test_a_branch_switch_is_reflected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _build(repo)
    git(repo, "checkout", "--quiet", "-b", "feature")
    assert GitClient().state(repo).branch == "feature"


def test_a_detached_head_is_reported_as_detached(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _build(repo)
    head = GitClient().state(repo).head_sha
    assert head is not None
    git(repo, "checkout", "--quiet", head)

    state = GitClient().state(repo)
    assert state.branch == "HEAD (detached)"
    assert state.head_sha == head


def test_file_enumeration_still_reflects_the_working_tree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _build(repo)
    (repo / "untracked.py").write_text("y = 2\n")
    (repo / ".gitignore").write_text("ignored.py\n")
    (repo / "ignored.py").write_text("z = 3\n")

    listed = GitClient().list_files(repo)

    assert listed is not None
    assert set(listed) == {".gitignore", "config.py", "untracked.py"}


def test_the_repository_root_is_still_found(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _build(repo)
    (repo / "src").mkdir()

    assert GitClient().toplevel(repo / "src") == repo.resolve()


def test_a_hostile_repository_still_indexes_correctly(tmp_path: Path, canary: Canary) -> None:
    """Safety must not be bought by refusing to read the repository at all."""
    repo = tmp_path / "repo"
    _build(repo, attributes="* filter=pwn\n")
    _configure(repo, "filter.pwn.clean", canary.command)
    _dirty(repo)
    canary.reset()

    client = GitClient()
    scanned = {entry.rel_path for entry in scan_repository(repo, git=client)}

    assert not canary.fired
    assert scanned == {".gitattributes", "config.py"}
    assert client.state(repo).is_dirty is True
