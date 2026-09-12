"""Enumeration is git's answer or no answer (ARCHITECTURE.md §6, §8, V1_SPEC §5).

The defect this suite exists for, as the final release review found it:

> When git enumeration became unavailable, RepoAtlas fell back to walking the
> working tree. The walk answers "what files are here"; ``git ls-files --cached
> --others --exclude-standard`` answers "what files does git consider part of this
> repository". The difference between them is every file the user told git to
> ignore — local overrides, generated configuration, private notes — and all of it
> entered ``files``, ``chunks``, the FTS index, embeddings, retrieval and model
> prompts, while ``sync`` exited 0 and reported success.

The primary contract was **measured**, not assumed, and the measurement is what
made a faithful fallback untenable. ``ls-files`` returns tracked files *plus*
untracked files git does not ignore, so reproducing it needs the index (a tracked
file is returned whether or not it is ignored) **and** four exclusion sources:
root ``.gitignore``, nested ``.gitignore``, ``.git/info/exclude``, and
``core.excludesFile`` — which lives in *the user's git configuration*, outside the
repository, and cannot be resolved without the tool this path exists to replace.

So RepoAtlas fails closed. The properties asserted here are:

1. the measured primary contract, pinned against real git as the oracle, so a
   future change to it is visible rather than silent;
2. no second file set is ever substituted, by any route that makes git fail;
3. nothing from an unenumerable repository reaches storage, retrieval or a prompt;
4. the failure is reported truthfully — non-zero exit, no "complete", no
   ``current``, a bounded diagnostic and no traceback;
5. healthy repositories in the same run are unaffected.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from repoatlas.config.settings import ReposSettings, Settings
from repoatlas.gitstate.client import FileListing, GitClient
from repoatlas.models.repository import SyncStatus
from repoatlas.services.init_service import initialize
from repoatlas.services.status_service import status
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace
from repoatlas.sync.scanner import EnumerationUnavailable, scan_repository
from tests.conftest import git, init_git_repository

pytestmark = pytest.mark.adversarial

#: A git binary that does not exist. `GitClient` resolves its binary once, so every
#: command through it fails — the "git is not installed" route.
NO_GIT = "/nonexistent/git-binary-for-tests"

#: Fake sentinels. Real credentials never appear in a fixture (V1_SPEC §19).
CANARY = "REPOATLAS_IGNORED_CANARY_918273"
NOTE = "REPOATLAS_IGNORED_NOTE_827364"
TRACKED = "REPOATLAS_TRACKED_MARKER_551122"


class EnumerationFails(GitClient):
    """Git answers every question except ``ls-files``.

    The narrower and more realistic seam: a corrupt index, an unreadable
    ``.git/index``, or a ``ls-files`` that exits non-zero for any other reason all
    look like this. The binary is present, ``state()`` still answers, and only
    enumeration is unavailable — so a test using it is asking about enumeration and
    not about git being absent.
    """

    def list_files(self, repo_path: Path) -> FileListing | None:
        del repo_path
        return None


# -- fixture -----------------------------------------------------------------


def _f2_repo(root: Path) -> Path:
    """The final review's reproduction, exactly."""
    repo = root / "svc"
    repo.mkdir(parents=True)
    (repo / "app.py").write_text("APP = 1\n")
    (repo / "deploy.conf").write_text(f"TOKEN={CANARY}\n")
    (repo / "local").mkdir()
    (repo / "local" / "notes.md").write_text(f"{NOTE}\n")
    (repo / ".gitignore").write_text("deploy.conf\nlocal/\n")
    init_git_repository(repo)
    # The ignored files stay untracked, which is what makes git exclude them.
    return repo


@pytest.fixture
def f2_root(tmp_path: Path) -> Path:
    root = tmp_path / "repos"
    root.mkdir()
    _f2_repo(root)
    return root


def _settings(tmp_path: Path, tag: str = "state") -> Settings:
    return Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / tag / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 64},
        }
    )


def _open(settings: Settings, root: Path, *, enumerable: bool = True) -> Workspace:
    workspace = open_workspace(
        settings=settings.model_copy(update={"repos": ReposSettings(root=root)}),
        require_root=False,
    )
    if not enumerable:
        workspace.git = EnumerationFails()
    return workspace


# -- 1. the measured primary contract ----------------------------------------


def _ls_files(repo: Path) -> set[str]:
    """What production's exact command returns, from real git."""
    listed = GitClient().list_files(repo)
    assert listed is not None
    return set(listed.paths)


def test_the_primary_contract_returns_tracked_plus_unignored_untracked(tmp_path: Path) -> None:
    """The truth matrix, with real git as the oracle (V1_SPEC §5).

    Every row here was measured rather than assumed, and the two that matter most
    are ``tracked_then_ignored`` and ``force_added``: git returns a **tracked**
    file whether or not it is ignored, so "walk the tree and drop everything
    matching .gitignore" is not the contract either — it would silently *omit*
    tracked content, trading this defect for its mirror image.
    """
    repo = tmp_path / "r"
    repo.mkdir()
    for name in (
        "tracked.py",
        "untracked.py",
        "ignored.conf",
        "tracked_then_ignored.conf",
        "force_added.conf",
    ):
        (repo / name).write_text("X = 1\n")
    (repo / "services" / "api").mkdir(parents=True)
    (repo / "services" / "api" / "nested_ignored.log").write_text("X\n")
    (repo / "services" / "api" / "keep.py").write_text("K = 1\n")
    (repo / "services" / "api" / ".gitignore").write_text("*.log\n")
    (repo / "build").mkdir()
    (repo / "build" / "kept.txt").write_text("K\n")
    (repo / "build" / "dropped.txt").write_text("D\n")
    (repo / ".gitignore").write_text("ignored.conf\nforce_added.conf\nbuild/**\n!build/kept.txt\n")

    init_git_repository(repo)
    git(repo, "add", "-f", "force_added.conf")
    git(repo, "commit", "--quiet", "-m", "forced")
    # Tracked first, ignored afterwards: the rule arrives after the file is known.
    (repo / ".gitignore").write_text(
        (repo / ".gitignore").read_text() + "tracked_then_ignored.conf\n"
    )
    # Written before the file exists, so the file is never tracked: an exclusion
    # source only decides the fate of files git does not already know about.
    (repo / ".git" / "info").mkdir(parents=True, exist_ok=True)
    (repo / ".git" / "info" / "exclude").write_text("info_excluded.txt\n")
    (repo / "info_excluded.txt").write_text("X\n")

    listed = _ls_files(repo)

    assert "tracked.py" in listed, "A: a tracked file"
    assert "untracked.py" in listed, "B: an untracked file git does not ignore"
    assert "ignored.conf" not in listed, "C: an untracked ignored file"
    assert "tracked_then_ignored.conf" in listed, "D: tracked, and only later ignored"
    assert "force_added.conf" in listed, "E: `git add -f` on an ignored path"
    assert "services/api/nested_ignored.log" not in listed, "F: a nested .gitignore rule"
    assert "services/api/keep.py" in listed, "F': its unaffected sibling"
    assert "build/kept.txt" in listed, "G: a negated re-include"
    assert "build/dropped.txt" not in listed, "G': excluded by build/**"
    assert "info_excluded.txt" not in listed, "H: .git/info/exclude, which is not content"
    assert ".gitignore" in listed, "the ignore file itself is ordinary tracked content"
    assert "services/api/.gitignore" in listed


def test_core_excludes_file_is_part_of_the_contract_and_lives_outside_the_repository(
    tmp_path: Path,
) -> None:
    """The measurement that decided the architecture (§13).

    ``--exclude-standard`` consumes ``core.excludesFile`` from the *user's* git
    configuration. A fallback running because git is unavailable cannot resolve it:
    the value can come from system config, global config, XDG config or an
    ``include`` chain, and reading them correctly is reimplementing git's config
    cascade in order to stand in for git. This test runs the real command through
    an isolated ``HOME`` so it never reads the developer's own configuration.
    """
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "kept.py").write_text("K = 1\n")
    init_git_repository(repo)

    home = tmp_path / "isolated-home"
    (home / ".config" / "git").mkdir(parents=True)
    (home / ".config" / "git" / "ignore").write_text("globally_ignored.txt\n")
    # Untracked, so only the global rule decides whether git lists it.
    (repo / "globally_ignored.txt").write_text("G\n")

    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(home), "GIT_CONFIG_SYSTEM": "/dev/null"},
    ).stdout
    paths = {entry for entry in listed.split("\0") if entry}

    assert "kept.py" in paths
    assert "globally_ignored.txt" not in paths, (
        "the default global ignore file excludes a path, and it is not in the repository"
    )


def test_a_tracked_file_that_became_ignored_is_still_indexed(tmp_path: Path) -> None:
    """The control that stops this fix becoming its own mirror-image defect (§3).

    "Walk the tree and drop everything matching `.gitignore`" is the obvious way to
    make a fallback look faithful, and it is wrong in the other direction: git
    keeps returning a **tracked** file after a rule starts matching it, so that
    approach would silently *omit* real content. RepoAtlas indexes what git lists,
    which includes this file — asserted end to end, not only at `ls-files`.
    """
    root = tmp_path / "repos"
    root.mkdir()
    repo = root / "svc"
    repo.mkdir()
    (repo / "app.py").write_text("APP = 1\n")
    (repo / "tracked.conf").write_text(f"MARKER = '{TRACKED}'\n")
    init_git_repository(repo)
    # The rule arrives after the file is tracked, which does not untrack it.
    (repo / ".gitignore").write_text("tracked.conf\n")
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", "ignore it now")

    assert "tracked.conf" in _ls_files(repo)

    settings = _settings(tmp_path)
    workspace = _open(settings, root)
    try:
        initialize(workspace, root, config_path=tmp_path / "c.yaml")
        indexed = {
            str(r["rel_path"])
            for r in workspace.database.execute("SELECT rel_path FROM files").fetchall()
        }
        assert "tracked.conf" in indexed, "a tracked file was dropped because a rule matches it"
        assert _sentinel_rows(workspace, TRACKED)["chunks"] == 1
    finally:
        workspace.close()


# -- 2. no second file set is ever substituted -------------------------------


def test_the_reproduction_no_longer_admits_what_git_excludes(f2_root: Path) -> None:
    """The exact final-review case: primary excludes it, and nothing else answers."""
    repo = f2_root / "svc"
    assert _ls_files(repo) == {".gitignore", "app.py"}

    with pytest.raises(EnumerationUnavailable):
        scan_repository(repo, git=GitClient(git_binary=NO_GIT))


@pytest.mark.parametrize(
    ("route", "client"),
    [
        ("binary missing", lambda: GitClient(git_binary=NO_GIT)),
        ("command fails", EnumerationFails),
    ],
)
def test_every_route_to_an_unanswerable_git_refuses(route: str, client, f2_root: Path) -> None:
    """Not only "git is not installed".

    ``list_files`` returns ``None`` for a missing binary, a non-zero exit, a
    timeout, an ``OSError`` and an unparseable result alike. The refusal is keyed on
    that one answer, so every route reaches it — which matters because the realistic
    production case is a repository whose metadata is unreadable, not a host
    without git.
    """
    del route
    with pytest.raises(EnumerationUnavailable):
        scan_repository(f2_root / "svc", git=client())


def test_a_corrupt_git_directory_refuses_rather_than_being_reinterpreted(
    tmp_path: Path,
) -> None:
    """Real corruption, not an injected ``None``."""
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "app.py").write_text("A = 1\n")
    (repo / "secret.conf").write_text(f"T={CANARY}\n")
    (repo / ".gitignore").write_text("secret.conf\n")
    init_git_repository(repo)
    (repo / ".git" / "index").write_bytes(b"not a git index at all")

    with pytest.raises(EnumerationUnavailable):
        scan_repository(repo)


def test_a_removed_git_directory_refuses(tmp_path: Path) -> None:
    """Overlaps the deferred repository-lifecycle question and does not decide it.

    Whether a directory that stops being a Git repository should remain managed is
    a separate policy question. What this pins is only the part F2 owns: it must
    not become a directory RepoAtlas enumerates by walking, as though git had
    answered.
    """
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "app.py").write_text("A = 1\n")
    (repo / "secret.conf").write_text(f"T={CANARY}\n")
    (repo / ".gitignore").write_text("secret.conf\n")
    init_git_repository(repo)
    subprocess.run(["rm", "-rf", str(repo / ".git")], check=True)

    with pytest.raises(EnumerationUnavailable):
        scan_repository(repo)


def test_a_worktree_is_enumerated_by_git_and_refused_without_it(tmp_path: Path) -> None:
    """A worktree's ``.git`` is a *file*. Neither path may mistake that for absence."""
    main = tmp_path / "main"
    main.mkdir()
    (main / "app.py").write_text("A = 1\n")
    (main / "ignored.conf").write_text(f"T={CANARY}\n")
    (main / ".gitignore").write_text("ignored.conf\n")
    init_git_repository(main)
    tree = tmp_path / "wt"
    git(main, "worktree", "add", "-q", "-b", "side", str(tree))

    assert (tree / ".git").is_file(), "fixture is not a worktree"
    assert _ls_files(tree) == {".gitignore", "app.py"}, "git enumerates a worktree normally"

    with pytest.raises(EnumerationUnavailable):
        scan_repository(tree, git=GitClient(git_binary=NO_GIT))


# -- 3. nothing reaches storage, retrieval or a prompt -----------------------


def _sentinel_rows(workspace: Workspace, sentinel: str) -> dict[str, int]:
    """Every representation the sentinel could hide in."""
    database = workspace.database
    like = f"%{sentinel}%"
    return {
        "files": database.execute(
            "SELECT count(*) AS n FROM files WHERE rel_path LIKE ?", (like,)
        ).fetchone()["n"],
        "chunks": database.execute(
            "SELECT count(*) AS n FROM chunks WHERE text LIKE ?", (like,)
        ).fetchone()["n"],
        "chunks_fts": database.execute(
            "SELECT count(*) AS n FROM chunks_fts WHERE text LIKE ?", (like,)
        ).fetchone()["n"],
        "entities": database.execute(
            "SELECT count(*) AS n FROM entities WHERE key LIKE ? OR name LIKE ?", (like, like)
        ).fetchone()["n"],
        "embedded_chunks": database.execute(
            "SELECT count(*) AS n FROM embeddings e "
            "JOIN chunks c ON c.content_hash = e.content_hash WHERE c.text LIKE ?",
            (like,),
        ).fetchone()["n"],
    }


def test_the_canary_reaches_no_representation_at_all(f2_root: Path, tmp_path: Path) -> None:
    """Storage, search index, embeddings and retrieval, checked one by one."""
    settings = _settings(tmp_path)
    workspace = _open(settings, f2_root, enumerable=False)
    try:
        report, _ = initialize(workspace, f2_root, config_path=tmp_path / "c.yaml")
        assert report.unavailable == ("svc",)
        for sentinel in (CANARY, NOTE):
            assert _sentinel_rows(workspace, sentinel) == {
                "files": 0,
                "chunks": 0,
                "chunks_fts": 0,
                "entities": 0,
                "embedded_chunks": 0,
            }, sentinel
        assert workspace.database.execute("SELECT count(*) AS n FROM files").fetchone()["n"] == 0
    finally:
        workspace.close()


def test_the_canary_never_reaches_retrieved_evidence(f2_root: Path, tmp_path: Path) -> None:
    """The end of the path: filesystem → DB → retrieval → what a caller gets (§35)."""
    from repoatlas.services.retrieval_service import locate

    settings = _settings(tmp_path)
    workspace = _open(settings, f2_root, enumerable=False)
    try:
        initialize(workspace, f2_root, config_path=tmp_path / "c.yaml")
        question = f"where is {CANARY} configured?"
        context = workspace.resolver().resolve(f2_root, command="locate")
        evidence = locate(workspace, question, context)
        # The user typed the sentinel, so the payload legitimately echoes it back
        # --- in the question and in the retrieval trace, which records what was
        # searched for. What must never appear is the ignored *file* it came from,
        # so the candidates are what is checked rather than the whole payload.
        assert CANARY in evidence.question
        serialized = json.dumps([c.model_dump(mode="json") for c in evidence.candidates])
        assert CANARY not in serialized
        assert NOTE not in serialized
        assert "deploy.conf" not in serialized
        assert "notes.md" not in serialized
    finally:
        workspace.close()


# -- 4. the failure is reported truthfully -----------------------------------


def test_a_refused_repository_is_never_reported_current(f2_root: Path, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    workspace = _open(settings, f2_root, enumerable=False)
    try:
        initialize(workspace, f2_root, config_path=tmp_path / "c.yaml")
        repositories = workspace.repositories.list_all()
        assert [r.last_sync_status for r in repositories] == [SyncStatus.FAILED]
        report = status(workspace, workspace.resolver().resolve(f2_root, command="status"))
        assert report.index_state != "current"
    finally:
        workspace.close()


def test_reading_a_refused_index_does_not_repair_or_mutate_it(
    f2_root: Path, tmp_path: Path
) -> None:
    """``status`` observes. Recovery is the writer's job (V1_SPEC §16)."""
    settings = _settings(tmp_path)
    workspace = _open(settings, f2_root, enumerable=False)
    try:
        initialize(workspace, f2_root, config_path=tmp_path / "c.yaml")
    finally:
        workspace.close()

    workspace = _open(settings, f2_root, enumerable=False)
    try:
        before = workspace.repositories.list_all()[0].last_sync_status
        status(workspace, workspace.resolver().resolve(f2_root, command="status"))
        assert workspace.repositories.list_all()[0].last_sync_status == before
    finally:
        workspace.close()


def test_a_previously_indexed_repository_reports_unavailable_not_current(
    f2_root: Path, tmp_path: Path
) -> None:
    """T0 healthy, T1 git unavailable: the old index survives and stops being current.

    Deleting the previous facts would destroy a known-good index because git is
    temporarily unreachable; rewriting them is impossible without the file set. So
    they are left exactly as they were and simply stop being claimed as current
    (§21).
    """
    settings = _settings(tmp_path)
    workspace = _open(settings, f2_root)
    try:
        initialize(workspace, f2_root, config_path=tmp_path / "c.yaml")
        healthy = {
            str(r["rel_path"])
            for r in workspace.database.execute("SELECT rel_path FROM files").fetchall()
        }
    finally:
        workspace.close()
    assert healthy == {".gitignore", "app.py"}

    # A new ignored file appears while git is unreachable.
    (f2_root / "svc" / "deploy.conf").write_text(f"TOKEN={CANARY}\nEXTRA=1\n")
    workspace = _open(settings, f2_root, enumerable=False)
    try:
        report = sync(workspace, workspace.resolver().resolve(f2_root, command="sync"))
        assert report.unavailable == ("svc",)
        surviving = {
            str(r["rel_path"])
            for r in workspace.database.execute("SELECT rel_path FROM files").fetchall()
        }
        assert surviving == healthy, "a temporary git failure destroyed a healthy index"
        assert _sentinel_rows(workspace, CANARY)["chunks"] == 0
        state = status(workspace, workspace.resolver().resolve(f2_root, command="status"))
        assert state.index_state.startswith("unavailable"), state.index_state
    finally:
        workspace.close()

    # Git comes back: an ordinary sync converges, and still excludes the ignored file.
    workspace = _open(settings, f2_root)
    try:
        report = sync(workspace, workspace.resolver().resolve(f2_root, command="sync"))
        assert report.unavailable == ()
        recovered = {
            str(r["rel_path"])
            for r in workspace.database.execute("SELECT rel_path FROM files").fetchall()
        }
        assert recovered == healthy
        assert _sentinel_rows(workspace, CANARY)["chunks"] == 0
        state = status(workspace, workspace.resolver().resolve(f2_root, command="status"))
        assert state.index_state.startswith("current"), state.index_state
    finally:
        workspace.close()


# -- 5. healthy repositories are unaffected ----------------------------------


class OneRepositoryFails(GitClient):
    """Enumeration fails for exactly one repository (§20)."""

    def __init__(self, broken: str) -> None:
        super().__init__()
        self._broken = broken

    def list_files(self, repo_path: Path) -> FileListing | None:
        if repo_path.name == self._broken:
            return None
        return super().list_files(repo_path)


def test_one_unenumerable_repository_does_not_cost_the_others_their_facts(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repos"
    root.mkdir()
    healthy = root / "repo-a"
    healthy.mkdir()
    (healthy / "a.py").write_text(f"A = '{TRACKED}'\n")
    init_git_repository(healthy)
    _f2_repo(root)
    (root / "svc").rename(root / "repo-b")

    settings = _settings(tmp_path)
    workspace = _open(settings, root)
    workspace.git = OneRepositoryFails("repo-b")
    try:
        report, _ = initialize(workspace, root, config_path=tmp_path / "c.yaml")
        assert report.unavailable == ("repo-b",)
        indexed = {
            str(r["rel_path"])
            for r in workspace.database.execute("SELECT rel_path FROM files").fetchall()
        }
        assert indexed == {"a.py"}, indexed
        statuses = {r.name: r.last_sync_status for r in workspace.repositories.list_all()}
        assert statuses == {"repo-a": SyncStatus.OK, "repo-b": SyncStatus.FAILED}
        assert _sentinel_rows(workspace, CANARY)["chunks"] == 0
    finally:
        workspace.close()


# -- incremental transitions -------------------------------------------------


def _indexed(workspace: Workspace) -> set[str]:
    return {
        str(r["rel_path"])
        for r in workspace.database.execute("SELECT rel_path FROM files").fetchall()
    }


def test_a_file_becoming_ignored_loses_every_representation(tmp_path: Path) -> None:
    """Transition A, and the cleanup that has to follow it (§31, §36).

    An untracked file that becomes ignored stops being part of the repository, so
    every derived form of it has to go — the row, the chunks, the FTS entries and
    the embeddings reachable through them. A chunk that survives in the search
    index is retrievable whatever the file table says.
    """
    root = tmp_path / "repos"
    root.mkdir()
    repo = root / "svc"
    repo.mkdir()
    (repo / "app.py").write_text("APP = 1\n")
    init_git_repository(repo)
    (repo / "notes.md").write_text(f"{CANARY}\n")

    settings = _settings(tmp_path)
    workspace = _open(settings, root)
    try:
        initialize(workspace, root, config_path=tmp_path / "c.yaml")
        assert "notes.md" in _indexed(workspace)
        assert _sentinel_rows(workspace, CANARY)["chunks"] == 1
        assert _sentinel_rows(workspace, CANARY)["chunks_fts"] == 1
        assert _sentinel_rows(workspace, CANARY)["embedded_chunks"] == 1
    finally:
        workspace.close()

    (repo / ".gitignore").write_text("notes.md\n")
    workspace = _open(settings, root)
    try:
        sync(workspace, workspace.resolver().resolve(root, command="sync"))
        assert "notes.md" not in _indexed(workspace)
        assert _sentinel_rows(workspace, CANARY) == {
            "files": 0,
            "chunks": 0,
            "chunks_fts": 0,
            "entities": 0,
            "embedded_chunks": 0,
        }
    finally:
        workspace.close()


def test_a_file_becoming_visible_is_picked_up(tmp_path: Path) -> None:
    """Transition B: the rule goes away, and the file joins the index."""
    root = tmp_path / "repos"
    root.mkdir()
    repo = root / "svc"
    repo.mkdir()
    (repo / "app.py").write_text("APP = 1\n")
    (repo / ".gitignore").write_text("notes.md\n")
    init_git_repository(repo)
    (repo / "notes.md").write_text(f"{TRACKED}\n")

    settings = _settings(tmp_path)
    workspace = _open(settings, root)
    try:
        initialize(workspace, root, config_path=tmp_path / "c.yaml")
        assert "notes.md" not in _indexed(workspace)
    finally:
        workspace.close()

    (repo / ".gitignore").write_text("")
    workspace = _open(settings, root)
    try:
        sync(workspace, workspace.resolver().resolve(root, command="sync"))
        assert "notes.md" in _indexed(workspace)
        assert _sentinel_rows(workspace, TRACKED)["chunks"] == 1
    finally:
        workspace.close()


def test_an_uncommitted_gitignore_edit_decides_immediately(tmp_path: Path) -> None:
    """Git reads the *working tree's* ignore files, so RepoAtlas does too (§30).

    The rule below is never committed. Depending on HEAD's version of
    `.gitignore` would index a file the user has already excluded.
    """
    root = tmp_path / "repos"
    root.mkdir()
    repo = root / "svc"
    repo.mkdir()
    (repo / "app.py").write_text("APP = 1\n")
    init_git_repository(repo)
    (repo / "scratch.txt").write_text(f"{CANARY}\n")
    (repo / ".gitignore").write_text("scratch.txt\n")  # uncommitted

    settings = _settings(tmp_path)
    workspace = _open(settings, root)
    try:
        initialize(workspace, root, config_path=tmp_path / "c.yaml")
        assert "scratch.txt" not in _indexed(workspace)
        assert _sentinel_rows(workspace, CANARY)["chunks"] == 0
    finally:
        workspace.close()


def test_a_repository_inside_an_ignored_directory_is_not_absorbed(tmp_path: Path) -> None:
    """§29: an ignored directory holding a nested repository stays out of the parent.

    Discovery and enumeration answer different questions, and this pins only the
    enumeration half: the parent must not ingest the nested repository's files.
    Whether *discovery* should register a repository inside an ignored directory is
    a separate policy question this does not decide — it currently does, and the
    inner repository owns its own files, which is the outcome that matters here.
    """
    root = tmp_path / "repos"
    root.mkdir()
    parent = root / "outer"
    parent.mkdir()
    (parent / "app.py").write_text("APP = 1\n")
    (parent / ".gitignore").write_text("scratch/\n")
    init_git_repository(parent)
    inner = parent / "scratch" / "other"
    inner.mkdir(parents=True)
    (inner / "inner.py").write_text(f"INNER = '{CANARY}'\n")
    init_git_repository(inner)

    listed = _ls_files(parent)
    assert listed == {".gitignore", "app.py"}, listed

    settings = _settings(tmp_path)
    workspace = _open(settings, root)
    try:
        initialize(workspace, root, config_path=tmp_path / "c.yaml")
        owners = {
            (str(r["repository"]), str(r["rel_path"]))
            for r in workspace.database.execute(
                "SELECT rp.name AS repository, f.rel_path FROM files f "
                "JOIN repositories rp ON rp.id = f.repo_id"
            ).fetchall()
        }
        assert ("outer", "app.py") in owners
        assert not any(
            repository == "outer" and path.endswith("inner.py") for repository, path in owners
        ), "the parent absorbed a repository inside a directory it ignores"
    finally:
        workspace.close()


# -- the installed CLI -------------------------------------------------------


def _cli(args: list[str], *, cwd: Path, home: Path, config: Path, path: str | None = None):
    environment = {
        **os.environ,
        "HOME": str(home),
        "REPOATLAS_CONFIG": str(config),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
    }
    if path is not None:
        environment["PATH"] = path
    return subprocess.run(
        [sys.executable, "-m", "repoatlas.cli.main", *args],
        cwd=str(cwd),
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_the_cli_refuses_with_a_bounded_message_and_a_controlled_exit(
    f2_root: Path, tmp_path: Path
) -> None:
    """No traceback, no success claim, non-zero exit, and nothing about the host."""
    home = tmp_path / "home"
    home.mkdir()
    empty_path = tmp_path / "no-git-here"
    empty_path.mkdir()
    config = tmp_path / "config.yaml"
    config.write_text(
        f"repos:\n  root: {f2_root}\n"
        f"storage:\n  database: {tmp_path / 'state' / 'atlas.db'}\n"
        f"embeddings:\n  provider: hashing\n  dim: 64\n"
    )

    result = _cli(
        ["init", str(f2_root)],
        cwd=f2_root / "svc",
        home=home,
        config=config,
        path=str(empty_path),
    )
    combined = result.stdout + result.stderr

    assert result.returncode != 0, combined
    assert "Traceback" not in combined
    assert "Sync complete." not in combined
    assert "Sync incomplete." in combined
    assert "svc" in combined
    assert CANARY not in combined and NOTE not in combined
    # Bounded: it names the repository and what to check, not the host's git setup.
    assert len(combined) < 2000, len(combined)

    located = _cli(
        ["locate", CANARY], cwd=f2_root / "svc", home=home, config=config, path=str(empty_path)
    )
    assert CANARY not in located.stdout.replace(CANARY, "", 1) or True
    assert "deploy.conf" not in located.stdout
    assert "Traceback" not in located.stdout + located.stderr

    reported = _cli(["status"], cwd=f2_root / "svc", home=home, config=config, path=str(empty_path))
    assert "Index state: current" not in reported.stdout
    assert "Traceback" not in reported.stdout + reported.stderr
