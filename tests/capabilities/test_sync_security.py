"""The admission boundary stays below sync, and sync never runs repository code.

A V2 caller cannot use sync to widen what the index admits. Secrets, ignored
files, out-of-repository content, a nested repository's files, oversized and
binary content all go through V1's admission pipeline whole, and the request has
no switch that could reach around it — there is no "include ignored", no "unsafe",
no path list. Each case below plants the material, syncs with ``full=True`` so
nothing is skipped by a stat shortcut, and asserts it never reached the index. A
visible control file planted beside it proves each sync genuinely ran.

Sync observes; it does not execute. Git is the only program it may start, and only
for local state — never ``fetch``, ``pull``, ``clone``, ``checkout``, ``reset``,
``stash`` or ``clean``. A repository that ships a ``Makefile``, an npm
``postinstall`` script or an executable git hook gets none of them run.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.search import SearchRequest
from repoatlas.models.sync import SyncRequest, SyncResponse
from repoatlas.services.workspace import Workspace
from tests.conftest import git

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)
SERVICE = "python-service"

#: Git subcommands that reach the network or change a working tree or history.
FORBIDDEN_GIT = frozenset(
    {
        "fetch",
        "pull",
        "push",
        "clone",
        "checkout",
        "switch",
        "restore",
        "reset",
        "stash",
        "clean",
        "merge",
        "rebase",
        "commit",
        "add",
        "am",
        "apply",
        "remote",
        "submodule",
        "gc",
        "prune",
    }
)


def _sync(service: CapabilityService, **kwargs: object) -> SyncResponse:
    return service.sync(SyncRequest(context=GLOBAL, **kwargs))  # type: ignore[arg-type]


def _repo_id(workspace: Workspace, slug: str) -> int:
    repo = next(r for r in workspace.repositories.list_all() if r.slug == slug)
    assert repo.id is not None
    return repo.id


def _indexed(workspace: Workspace, slug: str, paths: list[str]) -> set[str]:
    return set(workspace.files.states_for(_repo_id(workspace, slug), paths))


def _commit(repo: Path) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", "planted by a sync security test")


def _mentions(service: CapabilityService, token: str) -> bool:
    response = service.search(SearchRequest(question=token, context=GLOBAL))
    return any(token in item.text for item in response.evidence)


# ---------------------------------------------------------------------------
# CASE 8 — secrets and ignored files
# ---------------------------------------------------------------------------


SECRETS = [".env", ".ENV.local", "id_rsa", "Id_Rsa", "server.pem", "private.KEY"]


def test_secret_and_ignored_files_never_reach_the_index(
    initialized: Workspace, workspace_root: Path
) -> None:
    """CASE 8. Committed and tracked, and still refused — the policy is RepoAtlas's own."""
    repo = workspace_root / SERVICE
    for name in SECRETS:
        (repo / name).write_text(f"SECRET_TOKEN_7731 = {name!r}\n", encoding="utf-8")
    (repo / ".repoatlasignore").write_text("ignored_by_repoatlas.py\n", encoding="utf-8")
    (repo / "ignored_by_repoatlas.py").write_text("IGNORED_TOKEN_7731 = 1\n", encoding="utf-8")
    (repo / "visible_control.py").write_text("VISIBLE_CONTROL_7731 = 1\n", encoding="utf-8")
    _commit(repo)
    # Untracked and ignored by git: refused whether or not V1 indexes untracked files.
    (repo / ".gitignore").write_text("gitignored_untracked.py\n", encoding="utf-8")
    (repo / "gitignored_untracked.py").write_text("GITIGNORED_TOKEN_7731 = 1\n", encoding="utf-8")

    _sync(CapabilityService(initialized), full=True)

    planted = [*SECRETS, "ignored_by_repoatlas.py", "gitignored_untracked.py"]
    indexed = _indexed(initialized, SERVICE, [*planted, "visible_control.py"])
    assert "visible_control.py" in indexed, "the control proves the sync ran"
    assert indexed.isdisjoint(planted), indexed & set(planted)


def test_a_secret_token_is_not_searchable_after_sync(
    initialized: Workspace, workspace_root: Path
) -> None:
    repo = workspace_root / SERVICE
    (repo / ".env").write_text("DATABASE_PASSWORD=SECRET_TOKEN_8842\n", encoding="utf-8")
    _commit(repo)
    service = CapabilityService(initialized)

    _sync(service, full=True)

    assert not _mentions(service, "SECRET_TOKEN_8842")


# ---------------------------------------------------------------------------
# CASE 9 — nested repository ownership
# ---------------------------------------------------------------------------


def test_a_nested_repositorys_files_belong_to_it_alone(initialized: Workspace) -> None:
    """CASE 9. The outer repository never claims a file the inner one owns."""
    registered = initialized.repositories.list_all()
    inner = next(r for r in registered if r.slug.endswith("nested-inner"))
    outer = next(r for r in registered if r.slug == "nested-outer")
    inner_file = next(
        p
        for p in sorted(inner.path.rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(inner.path).parts
    )
    within_inner = inner_file.relative_to(inner.path).as_posix()
    within_outer = inner_file.relative_to(outer.path).as_posix()

    _sync(CapabilityService(initialized), full=True)

    assert within_inner in _indexed(initialized, inner.slug, [within_inner])
    assert within_outer not in _indexed(initialized, outer.slug, [within_outer])


# ---------------------------------------------------------------------------
# Oversized, binary and symlink-escaping content
# ---------------------------------------------------------------------------


def test_oversized_content_is_not_indexed(initialized: Workspace, workspace_root: Path) -> None:
    repo = workspace_root / SERVICE
    limit = initialized.settings.indexing.max_file_bytes
    (repo / "oversized.py").write_text(
        "OVERSIZED_TOKEN_5521 = 1\n" + "x = 0\n" * (limit // 6 + 64), encoding="utf-8"
    )
    _commit(repo)
    service = CapabilityService(initialized)

    _sync(service, full=True)

    assert not _mentions(service, "OVERSIZED_TOKEN_5521")


def test_binary_content_is_not_indexed(initialized: Workspace, workspace_root: Path) -> None:
    repo = workspace_root / SERVICE
    (repo / "blob.dat").write_bytes(b"\x00\x01BINARY_TOKEN_5521\x00\xff" * 200)
    _commit(repo)
    service = CapabilityService(initialized)

    _sync(service, full=True)

    assert not _mentions(service, "BINARY_TOKEN_5521")


def test_a_symlink_out_of_the_repository_is_not_a_route_in(
    initialized: Workspace, workspace_root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside-the-root"
    outside.mkdir()
    (outside / "leaked.py").write_text("ESCAPE_TOKEN_5521 = 1\n", encoding="utf-8")
    repo = workspace_root / SERVICE
    (repo / "escape").symlink_to(outside, target_is_directory=True)
    (repo / "visible_control.py").write_text("VISIBLE_CONTROL_5521 = 1\n", encoding="utf-8")
    _commit(repo)
    service = CapabilityService(initialized)

    _sync(service, full=True)

    assert "visible_control.py" in _indexed(initialized, SERVICE, ["visible_control.py"])
    assert "escape/leaked.py" not in _indexed(initialized, SERVICE, ["escape/leaked.py"])
    assert not _mentions(service, "ESCAPE_TOKEN_5521")


def test_the_request_has_no_switch_that_widens_admission() -> None:
    """No "include ignored", no "unsafe", no explicit path list."""
    assert set(SyncRequest.model_fields) == {"context", "full", "depth"}


# ---------------------------------------------------------------------------
# §4 / §12 — local observation only, and no repository code runs
# ---------------------------------------------------------------------------


def _argv(args: object) -> list[str]:
    if isinstance(args, (str, bytes, os.PathLike)):
        return [os.fsdecode(args)]
    return [os.fsdecode(part) for part in args]  # type: ignore[union-attr]


def _git_subcommand(argv: list[str]) -> str:
    """The subcommand, skipping options and the values of ``-C`` / ``-c``."""
    skip = False
    for part in argv[1:]:
        if skip:
            skip = False
            continue
        if part in {"-C", "-c"}:
            skip = True
            continue
        if part.startswith("-"):
            continue
        return part
    return ""


@pytest.fixture
def guarded_processes(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Every process started during the test, with anything but local git refused."""
    started: list[list[str]] = []
    real = subprocess.Popen

    class Guarded(real):  # type: ignore[misc, valid-type]
        def __init__(self, args: object, *rest: object, **kwargs: object) -> None:
            argv = _argv(args)
            program = Path(argv[0]).name
            if program != "git":
                raise AssertionError(f"sync started a program other than git: {argv!r}")
            subcommand = _git_subcommand(argv)
            if subcommand in FORBIDDEN_GIT:
                raise AssertionError(f"sync ran a forbidden git subcommand: {argv!r}")
            started.append(argv)
            super().__init__(args, *rest, **kwargs)  # type: ignore[arg-type]

    def no_shell(*args: object, **kwargs: object) -> object:
        raise AssertionError("sync reached a shell")

    monkeypatch.setattr(subprocess, "Popen", Guarded)
    monkeypatch.setattr(os, "system", no_shell)
    monkeypatch.setattr(os, "popen", no_shell)
    return started


def test_git_is_the_only_program_and_never_over_the_network(
    initialized: Workspace, guarded_processes: list[list[str]]
) -> None:
    """§4. Local state reads only. The guard raises on anything else."""
    _sync(CapabilityService(initialized), full=True)

    assert guarded_processes, "sync reads git state, so the guard must have seen git"
    assert all(Path(argv[0]).name == "git" for argv in guarded_processes)


def test_repository_code_is_never_executed(
    initialized: Workspace, workspace_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§12. Build files, install scripts and hooks are data to RepoAtlas, never code."""
    repo = workspace_root / SERVICE
    marker = workspace_root / "EXECUTED_MARKER"
    (repo / "Makefile").write_text(f"all:\n\ttouch {marker}\n", encoding="utf-8")
    (repo / "package.json").write_text(
        '{"name": "x", "scripts": {"postinstall": "touch ' + str(marker) + '"}}\n',
        encoding="utf-8",
    )
    (repo / "setup.py").write_text(
        f"import pathlib\npathlib.Path({str(marker)!r}).touch()\n", encoding="utf-8"
    )
    _commit(repo)
    for hook in ("post-checkout", "post-merge", "pre-commit", "post-index-change"):
        path = repo / ".git" / "hooks" / hook
        path.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
        path.chmod(0o755)

    started: list[list[str]] = []
    real = subprocess.Popen

    class Recording(real):  # type: ignore[misc, valid-type]
        def __init__(self, args: object, *rest: object, **kwargs: object) -> None:
            argv = _argv(args)
            if Path(argv[0]).name != "git":
                raise AssertionError(f"sync started {argv!r}")
            started.append(argv)
            super().__init__(args, *rest, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(subprocess, "Popen", Recording)

    _sync(CapabilityService(initialized), full=True)

    assert not marker.exists(), "repository code ran during sync"
    assert all(_git_subcommand(argv) not in FORBIDDEN_GIT for argv in started)


def test_capability_code_performs_no_mutation_of_its_own() -> None:
    """§27/§28. One orchestration owner, and the capability layer only calls it.

    Every write a sync performs goes through
    :func:`repoatlas.services.sync_service.sync`. The capability layer names no
    store writer, no sync marker, no linker commit, no engine and no lock, so it
    cannot split V1's lifecycle or run a second one beside it.
    """
    package = Path(__file__).resolve().parents[2] / "repoatlas" / "capabilities"
    forbidden = (
        "sync_engine(",
        "mark_sync_started",
        "mark_many_synced",
        "mark_sync_failed",
        "commit_global",
        "link_global",
        "replace_file_facts",
        ".upsert(",
        "writer_lock(",
        "embed_pending",
        ".transaction()",
        ".relocate(",
    )
    offenders = [
        f"{path.name}: {token}"
        for path in sorted(package.rglob("*.py"))
        for token in forbidden
        if token in path.read_text(encoding="utf-8")
    ]
    assert not offenders, offenders
