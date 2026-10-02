"""File enumeration is complete at any depth (ARCHITECTURE.md §6, V1_SPEC §5).

Enumeration used to have two authorities. ``git ls-files`` was the primary one,
and when git could not answer, RepoAtlas walked the working tree itself. That
walk began::

    if depth > 64:  # guard against pathological or symlink-induced depth
        return

so a file 65 directories below the repository root was not enumerated, not
indexed, not searchable and not mentioned. The sync reported success. The two
authorities silently disagreed about what the repository contained, and only one
of them was ever right.

The comment was wrong about what it was protecting. A cycle comes from symlinks,
which that walk always refused; the cost of a tree is its *size*, which a depth
limit does not bound at all — a flat directory of 100,000 files costs the same at
depth 1 as at depth 100. The cap bounded the one dimension that was never the
risk, and the thing it actually did was truncate repositories.

Raising 64 to some larger number would have kept the defect and moved it. The
ceiling went, and then the walk itself went: it disagreed with git about far more
than depth, admitting every file git was told to ignore, so a repository git
cannot enumerate is now *refused* rather than enumerated some other way (see
:class:`~repoatlas.sync.scanner.EnumerationUnavailable`). Every test here that
used the walk merely to reach a deep tree now drives ``scan_repository`` against
real git, which reaches it just as well and is the code production runs.

The tests below are adversarial in two directions. Deep files must appear — and
*nothing else* may have arrived with them. Every exclusion, ignore rule,
secret-name refusal, containment check, symlink refusal and resource bound is
re-asserted at depth, because an enumerator that reaches further is one with more
opportunities to reach something it should not.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from repoatlas.config.settings import DiscoverySettings, ReposSettings, Settings
from repoatlas.containment import RepositoryBoundary
from repoatlas.gitstate.client import GitClient
from repoatlas.models.parsing import Severity
from repoatlas.services.init_service import initialize
from repoatlas.services.status_service import status
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace
from repoatlas.sync.scanner import scan_repository
from tests.conftest import fact_snapshot, git, init_git_repository, reindex_from_scratch

pytestmark = pytest.mark.adversarial

#: A git binary that does not exist, which is what makes `list_files` return
#: `None`. This is the architecture's own seam -- `GitClient` resolves its binary
#: once and every command through it fails -- not a private method reached around
#: the front.
#:
#: It used to put enumeration on a filesystem walk. It now makes enumeration
#: *refuse*: a walk answers "what files are here", which is a superset of what git
#: admits, so standing in for `ls-files` with one silently indexed ignored content
#: (see `EnumerationUnavailable`).
NO_GIT = "/nonexistent/git-binary-for-tests"


# --- harness ------------------------------------------------------------------


def deep_dir(root: Path, depth: int, *, name: str = "d") -> Path:
    """A directory ``depth`` components below ``root``, made without recursion.

    ``mkdir(parents=True)`` recurses, so building the fixture that way would fail
    for the same reason the old scanner did, and at a similar depth. One
    component at a time keeps the fixture honest about what it is testing.
    """
    current = root
    for _ in range(depth):
        current = current / name
        current.mkdir(exist_ok=True)
    return current


def deep_file(root: Path, depth: int, filename: str, content: str) -> Path:
    target = deep_dir(root, depth) / filename
    target.write_text(content, encoding="utf-8")
    return target


def make_repo(root: Path, files: dict[str, str] | None = None) -> Path:
    """A real Git repository with one commit.

    Always seeded: `git commit` fails on a repository with nothing staged, so a
    fixture that adds all its files afterwards would never get its initial
    commit.
    """
    root.mkdir(parents=True, exist_ok=True)
    seeded = {"README.md": "fixture\n", **(files or {})}
    for name, content in seeded.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    init_git_repository(root)
    return root


def settings_for(tmp_path: Path, tag: str = "state", **overrides: object) -> Settings:
    payload: dict[str, object] = {
        "storage": {"database": str(tmp_path / tag / "atlas.db")},
    }
    payload.update(overrides)
    return Settings.model_validate(payload)


def indexed_via(settings: Settings, root: Path, config: Path) -> tuple[set[str], list[str]]:
    """Index ``root``; return the indexed paths and every diagnostic raised."""
    workspace = open_workspace(settings=settings, require_root=False)
    try:
        report, _ = initialize(workspace, root, config_path=config)
        rows = workspace.database.execute("SELECT rel_path FROM files").fetchall()
        notes = [d.message for repository in report.repositories for d in repository.diagnostics]
        return {str(r["rel_path"]) for r in rows}, notes
    finally:
        workspace.close()


def open_at(settings: Settings, root: Path) -> Workspace:
    return open_workspace(
        settings=settings.model_copy(update={"repos": ReposSettings(root=root)}),
        require_root=False,
    )


def scanned(
    root: Path,
    config: DiscoverySettings | None = None,
    *,
    incomplete: list[str] | None = None,
) -> list[str]:
    """Every path ``scan_repository`` admits for ``root``, through real git.

    These tests used to call the filesystem walk directly. There is no walk any
    more, so each of them now drives the one enumerator production uses — which
    is what they were standing in for all along.
    """
    return sorted(
        entry.rel_path
        for entry in scan_repository(
            root, settings=config or DiscoverySettings(), incomplete=incomplete
        )
    )


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    return tmp_path / "root"


# --- 1-7. the depth matrix ----------------------------------------------------


DEPTHS = (0, 1, 10, 63, 64, 65, 66, 80, 100, 200)


def test_every_depth_is_enumerated(workspace_root: Path, tmp_path: Path) -> None:
    """The old boundary was exactly here: 64 enumerated, 65 silently absent."""
    repo = workspace_root / "deeprepo"
    repo.mkdir(parents=True)
    for depth in DEPTHS:
        deep_file(repo, depth, f"m{depth}.py", f"DEPTH_MARKER_{depth} = {depth}\n")
    init_git_repository(repo)

    files, _ = indexed_via(settings_for(tmp_path), workspace_root, tmp_path / "c.yaml")

    missing = [d for d in DEPTHS if not any(p.endswith(f"m{d}.py") for p in files)]
    assert missing == [], f"depths absent from the index: {missing}"


@pytest.mark.parametrize("depth", [63, 64, 65, 66, 80, 100])
def test_enumeration_reaches_each_side_of_the_old_cutoff(tmp_path: Path, depth: int) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    deep_file(root, depth, "marker.py", "X = 1\n")
    init_git_repository(root)

    found = scanned(root)

    assert len(found) == 1
    assert found[0].count("/") == depth


def test_depth_four_hundred_is_still_enumerated(tmp_path: Path) -> None:
    """Depth is not a limit anywhere in the path, including in git itself."""
    root = tmp_path / "repo"
    root.mkdir()
    deep_file(root, 400, "deep.py", "DEEP = 1\n")
    init_git_repository(root)

    found = scanned(root)

    assert len(found) == 1
    assert found[0].count("/") == 400


# --- 8-9. the public path, and the enumerator agreeing with git ----------------


def test_the_public_sync_path_reaches_a_deep_file(workspace_root: Path, tmp_path: Path) -> None:
    """Proof the fix is on the branch production takes, not only in a helper."""
    repo = make_repo(workspace_root / "r", {"shallow.py": "S = 1\n"})
    deep_file(repo, 80, "deep.py", "DEEP_VIA_PUBLIC_SYNC = 1\n")

    files, _ = indexed_via(settings_for(tmp_path), workspace_root, tmp_path / "c.yaml")
    assert any(p.endswith("deep.py") for p in files)


def test_the_indexed_set_is_exactly_what_git_lists_minus_policy(
    workspace_root: Path, tmp_path: Path
) -> None:
    """One tree, two independent authorities: RepoAtlas's index and git itself.

    This test used to index the same tree twice, once per enumerator, and assert
    the two agreed. They did not agree in general — the walk admitted every file
    git ignores — and the fixture passed only because it happened to contain no
    ignored file. With one enumerator left, comparing RepoAtlas to itself would be
    a tautology, so the comparison is now against ``git ls-files`` run directly:
    the index must be git's own answer, less exactly the exclusions RepoAtlas
    documents (``node_modules`` here, V1_SPEC §5).
    """
    repo = make_repo(
        workspace_root / "r",
        {
            "shallow.py": "S = 1\n",
            "pkg/mod.py": "M = 1\n",
            "with spaces.py": "SPACED = 1\n",
            "dots.and-dashes[1].py": "PUNCT = 1\n",
            "résumé/配置.py": "UNICODE = 1\n",
        },
    )
    deep_file(repo, 65, "past_cutoff.py", "PAST = 1\n")
    deep_file(repo, 80, "deeper.py", "DEEPER = 1\n")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "vendored.py").write_text("V = 1\n", encoding="utf-8")
    (repo / "bin.dat").write_bytes(b"\x00\x01\x02\x03" * 64)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)

    indexed, _ = indexed_via(settings_for(tmp_path), workspace_root, tmp_path / "p.yaml")

    # `-z`, exactly as production runs it: without it git C-quotes any path that
    # is not plain ASCII, and the comparison would be against a different alphabet
    # rather than a different file set.
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split("\0")
    expected = {entry for entry in listed if entry and not entry.startswith("node_modules/")}

    assert indexed == expected, (
        f"only RepoAtlas: {indexed - expected}; only git: {expected - indexed}"
    )
    assert "node_modules/vendored.py" not in indexed
    assert any(p.endswith("past_cutoff.py") for p in indexed)


def test_a_repository_git_cannot_enumerate_yields_no_facts_at_all(
    workspace_root: Path, tmp_path: Path
) -> None:
    """This used to assert that both enumerators produced the same facts.

    They did not. The walk admitted every file git ignores, and the fixtures here
    happened to contain none, which is the only reason the comparison passed. The
    property that replaces it is the one that is actually true: when git cannot
    enumerate a repository, RepoAtlas produces *no* facts for it rather than facts
    from a different file set.
    """
    repo = make_repo(
        workspace_root / "r",
        {"app.py": "import os\n\n\ndef handler():\n    return os.environ['DEEP_HOST']\n"},
    )
    deep_file(
        repo,
        70,
        "deep.py",
        "import json\n\n\ndef deep_handler():\n    return json.dumps({'k': 1})\n",
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)

    workspace = open_workspace(settings=settings_for(tmp_path, "f"), require_root=False)
    workspace.git = GitClient(git_binary=NO_GIT)
    try:
        report, _ = initialize(workspace, workspace_root, config_path=tmp_path / "f.yaml")
        assert report.unavailable == ("r",), report.unavailable
        assert fact_snapshot(workspace)["files"] == []
    finally:
        workspace.close()


# --- 10-12. policy still applies, at depth ------------------------------------


def test_a_deep_file_under_an_excluded_directory_stays_out(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    excluded = deep_dir(root, 80) / "node_modules"
    excluded.mkdir()
    (excluded / "vendored.py").write_text("V = 1\n", encoding="utf-8")
    (deep_dir(root, 80) / "normal.py").write_text("N = 1\n", encoding="utf-8")
    init_git_repository(root)

    found = scanned(root)
    assert any(p.endswith("normal.py") for p in found)
    assert not any("node_modules" in p for p in found)


def test_a_deep_ignored_file_stays_out(workspace_root: Path, tmp_path: Path) -> None:
    repo = make_repo(workspace_root / "r", {".repoatlasignore": "secretstuff/\n"})
    ignored = deep_dir(repo, 80) / "secretstuff"
    ignored.mkdir()
    (ignored / "hidden.py").write_text("IGNORED_DEEP_MARKER = 1\n", encoding="utf-8")
    deep_file(repo, 80, "visible.py", "VISIBLE_DEEP_MARKER = 1\n")

    files, _ = indexed_via(settings_for(tmp_path), workspace_root, tmp_path / "c.yaml")
    assert any(p.endswith("visible.py") for p in files)
    assert not any("secretstuff" in p for p in files)


def test_deep_secret_files_never_enter_the_index(workspace_root: Path, tmp_path: Path) -> None:
    """Reaching further must not create a route around the secret-name policy."""
    repo = make_repo(workspace_root / "r")
    directory = deep_dir(repo, 80)
    (directory / ".env").write_text("SECRET_DEEP_MARKER=1\n", encoding="utf-8")
    (directory / "id_rsa").write_text("SECRET_DEEP_MARKER key\n", encoding="utf-8")
    (directory / "private.pem").write_text("SECRET_DEEP_MARKER pem\n", encoding="utf-8")
    (directory / "app.py").write_text("APP_DEEP_MARKER = 1\n", encoding="utf-8")

    settings = settings_for(tmp_path)
    files, _ = indexed_via(settings, workspace_root, tmp_path / "c.yaml")

    assert any(p.endswith("app.py") for p in files)
    for secret in (".env", "id_rsa", "private.pem"):
        assert not any(p.endswith(secret) for p in files), secret

    workspace = open_at(settings, workspace_root)
    try:
        chunks = workspace.database.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE text LIKE '%SECRET_DEEP_MARKER%'"
        ).fetchone()
        assert int(chunks["n"]) == 0, "a secret reached chunk storage"
    finally:
        workspace.close()


# --- 13-15. resources and special files ---------------------------------------


def _sparse(path: Path, size: int) -> None:
    with path.open("wb") as handle:
        handle.truncate(size)


def test_a_deep_sparse_file_is_classified_without_being_read(
    workspace_root: Path, tmp_path: Path
) -> None:
    """Traversal found it; content admission still owns the resource limit."""
    repo = make_repo(workspace_root / "r")
    directory = deep_dir(repo, 70)
    (directory / "small.py").write_text("SMALL_DEEP = 1\n", encoding="utf-8")
    sparse = directory / "sparse.bin"
    _sparse(sparse, 5 << 30)
    assert sparse.stat().st_blocks * 512 < (1 << 20), "the fixture must stay sparse"

    settings = settings_for(tmp_path, indexing={"max_file_bytes": 1024})
    files, _ = indexed_via(settings, workspace_root, tmp_path / "c.yaml")

    assert any(p.endswith("small.py") for p in files)
    assert sparse.stat().st_blocks * 512 < (1 << 20), "the file was materialized by indexing"


def test_a_deep_binary_file_keeps_its_classification(workspace_root: Path, tmp_path: Path) -> None:
    repo = make_repo(workspace_root / "r")
    directory = deep_dir(repo, 70)
    (directory / "blob.bin").write_bytes(b"\x00\xff\xfe\x01" * 512)
    (directory / "text.py").write_text("TEXT_DEEP = 1\n", encoding="utf-8")

    settings = settings_for(tmp_path)
    indexed_via(settings, workspace_root, tmp_path / "c.yaml")

    workspace = open_at(settings, workspace_root)
    try:
        row = workspace.database.execute(
            "SELECT is_binary FROM files WHERE rel_path LIKE '%blob.bin'"
        ).fetchone()
        assert row is not None and bool(row["is_binary"]), "deep binary lost its classification"
    finally:
        workspace.close()


def test_enumeration_does_not_block_on_a_deep_fifo(tmp_path: Path) -> None:
    """Two refusals, either of which suffices: git does not list a FIFO as an
    untracked file, and containment would refuse it anyway because it is not a
    regular file. Nothing opens it, so nothing blocks on it."""
    root = tmp_path / "repo"
    root.mkdir()
    directory = deep_dir(root, 70)
    (directory / "ok.py").write_text("OK = 1\n", encoding="utf-8")
    init_git_repository(root)
    os.mkfifo(directory / "pipe")

    found = scanned(root)

    assert any(p.endswith("ok.py") for p in found)
    assert not any(p.endswith("pipe") for p in found), "a FIFO is not a regular file"


def test_containment_still_refuses_a_deep_special_file(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    directory = deep_dir(root, 70)
    os.mkfifo(directory / "pipe")
    rel = (directory / "pipe").relative_to(root).as_posix()

    assert RepositoryBoundary(root).stat(rel) is None


# --- 16-18. symlinks, cycles and git internals --------------------------------


def test_a_deep_symlink_out_of_the_tree_is_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "escaped.py").write_text("ESCAPED = 1\n", encoding="utf-8")
    root = tmp_path / "repo"
    root.mkdir()
    directory = deep_dir(root, 70)
    (directory / "inside.py").write_text("INSIDE = 1\n", encoding="utf-8")
    init_git_repository(root)
    (directory / "link").symlink_to(outside, target_is_directory=True)

    found = scanned(root)

    assert any(p.endswith("inside.py") for p in found)
    assert not any("escaped" in p for p in found), "enumeration left the repository"


def test_a_deep_symlink_cycle_terminates(tmp_path: Path) -> None:
    """Unbounded depth plus a cycle is the combination the old cap pretended to stop.

    Termination is now git's property rather than the walk's: ``ls-files`` records
    a symlink as an entry and never descends through one, so the cycle has no way
    to exist. Containment then refuses the link itself, which is not a regular
    file.
    """
    root = tmp_path / "repo"
    root.mkdir()
    anchor = deep_dir(root, 70)
    (anchor / "file.py").write_text("F = 1\n", encoding="utf-8")
    init_git_repository(root)
    (anchor / "back").symlink_to(root, target_is_directory=True)

    found = scanned(root)  # must return at all

    assert any(p.endswith("file.py") for p in found)
    assert len(found) == 1, f"the cycle was entered: {found}"


def test_git_internals_are_refused_even_when_exclusions_omit_them(tmp_path: Path) -> None:
    """`.git` is structural, not configuration. `ls-files` never reports it either."""
    root = tmp_path / "repo"
    make_repo(root, {"a.py": "A = 1\n"})

    found = scanned(root, DiscoverySettings(exclusions=("node_modules",)))

    assert found == ["README.md", "a.py"], f"unexpected: {found}"


def test_deep_git_internals_are_refused(tmp_path: Path) -> None:
    """A `.git` directory anywhere in the tree, not only at the root.

    The two enumerators differed here, and git is the one that is right. The walk
    treated *any* directory containing a `.git` entry as a nested repository and
    dropped everything beside it. Git asks whether that `.git` is actually a
    repository — a lone `config` file is not — so `inner.py` is an ordinary file
    of *this* repository and is indexed, while the `.git` directory itself is
    still never enumerated. A real nested repository is a separate test.
    """
    root = tmp_path / "repo"
    root.mkdir()
    above = deep_dir(root, 69)
    (above / "src.py").write_text("S = 1\n", encoding="utf-8")
    directory = deep_dir(root, 70)
    (directory / "inner.py").write_text("I = 1\n", encoding="utf-8")
    init_git_repository(root)
    internals = directory / ".git"
    internals.mkdir()
    (internals / "config").write_text("[core]\n", encoding="utf-8")

    found = scanned(root, DiscoverySettings(exclusions=()))

    assert any(p.endswith("src.py") for p in found)
    assert not any(".git" in p for p in found), "git internals enumerated"
    assert any(p.endswith("inner.py") for p in found)


# --- 19. incompleteness is reported, never implied ----------------------------


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read a mode-000 directory")
def test_an_unreadable_deep_subtree_is_reported_not_passed_over(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    directory = deep_dir(root, 70)
    (directory / "visible.py").write_text("V = 1\n", encoding="utf-8")
    # The repository is created first, on purpose: a file committed inside the
    # locked directory would be *tracked*, and git lists a tracked file whether or
    # not it can open the directory it lives in. The property under test is about
    # the untracked side, where being unable to look is the only thing that
    # decides.
    init_git_repository(root)
    locked = directory / "locked"
    locked.mkdir()
    (locked / "hidden.py").write_text("H = 1\n", encoding="utf-8")
    locked.chmod(0o000)
    try:
        refused: list[str] = []
        found = scanned(root, incomplete=refused)
    finally:
        locked.chmod(0o755)

    assert any(p.endswith("visible.py") for p in found)
    assert not any(p.endswith("hidden.py") for p in found)
    # git reports an unopenable directory as a warning and still exits 0, so this
    # is the only thing separating "nothing is under there" from "I could not
    # look". It must reach the caller, not be discarded with the exit status.
    assert refused, "an unreadable directory must be reported, not silently skipped"
    assert any("locked" in reason for reason in refused), refused


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read a mode-000 directory")
def test_a_successful_sync_says_so_when_enumeration_was_incomplete(
    workspace_root: Path, tmp_path: Path
) -> None:
    """A successful sync must not be readable as a complete one."""
    repo = make_repo(workspace_root / "r", {"a.py": "A = 1\n"})
    locked = deep_dir(repo, 70) / "locked"
    locked.mkdir()
    (locked / "hidden.py").write_text("H = 1\n", encoding="utf-8")
    locked.chmod(0o000)
    try:
        _, notes = indexed_via(settings_for(tmp_path), workspace_root, tmp_path / "c.yaml")
    finally:
        locked.chmod(0o755)

    assert any("enumeration incomplete" in note for note in notes), notes
    # Which directory was refused used to be read out of this note, because the
    # note was built from git's stderr. It is reported by the sync's own
    # `unreadable_paths` instead now (RC audit, RCA-10): git names whatever it was
    # pointed at, and a repository helps decide what that is.
    assert not any("Permission denied" in note for note in notes), notes


def test_a_complete_scan_reports_no_incompleteness(workspace_root: Path, tmp_path: Path) -> None:
    repo = make_repo(workspace_root / "r", {"a.py": "A = 1\n"})
    deep_file(repo, 80, "deep.py", "D = 1\n")

    _, notes = indexed_via(settings_for(tmp_path), workspace_root, tmp_path / "c.yaml")
    assert not any("enumeration incomplete" in note for note in notes), notes


def test_the_diagnostic_is_a_warning_and_quotes_nothing_git_said(tmp_path: Path) -> None:
    """One warning, whatever git wrote, and none of git's words in it.

    This test used to assert the opposite --- that the message *named* the refused
    directory --- on the reasoning that git reports it relative to the repository.
    It also reports an absolute ``core.excludesFile`` the repository itself
    configured, which is how a host path reached a public sync response (RC audit,
    RCA-10). The projection now takes a count and writes its own sentence.
    """
    from repoatlas.models.parsing import ENUMERATION_INCOMPLETE
    from repoatlas.sync.engine import _enumeration_diagnostics

    assert _enumeration_diagnostics(0) == []

    diagnostics = _enumeration_diagnostics(2)

    assert len(diagnostics) == 1
    assert diagnostics[0].severity is Severity.WARNING
    assert diagnostics[0].message == ENUMERATION_INCOMPLETE
    assert "enumeration incomplete" in diagnostics[0].message
    assert str(tmp_path) not in diagnostics[0].message, "no host-absolute path in a diagnostic"
    for quoted in ("a/b", "Permission denied", "warning:", "2"):
        assert quoted not in diagnostics[0].message, quoted


# --- 20-22. determinism, unicode, punctuation ---------------------------------


def test_enumeration_is_deterministic_across_runs(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    for index in range(12):
        deep_file(root, 65 + index, f"m{index}.py", f"M{index} = 1\n")
    for name in ("b.py", "a.py", "c.py"):
        (root / name).write_text("X = 1\n", encoding="utf-8")
    init_git_repository(root)

    runs = [
        [entry.rel_path for entry in scan_repository(root, settings=DiscoverySettings())]
        for _ in range(5)
    ]
    assert all(run == runs[0] for run in runs), "enumeration order is not stable"


def test_deep_unicode_paths_are_preserved(workspace_root: Path, tmp_path: Path) -> None:
    repo = make_repo(workspace_root / "r")
    directory = deep_dir(repo, 70) / "résumé" / "配置"
    directory.mkdir(parents=True)
    (directory / "設定.py").write_text("UNICODE_DEEP_MARKER = 1\n", encoding="utf-8")

    settings = settings_for(tmp_path)
    files, _ = indexed_via(settings, workspace_root, tmp_path / "c.yaml")

    matches = [p for p in files if p.endswith("設定.py")]
    assert matches, files
    assert "résumé/配置/設定.py" in matches[0]


def test_deep_paths_with_spaces_and_punctuation(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    directory = deep_dir(root, 70) / "with spaces" / "dots.and-dashes[1]"
    directory.mkdir(parents=True)
    (directory / "a file.py").write_text("X = 1\n", encoding="utf-8")
    init_git_repository(root)

    found = scanned(root)
    assert any(p.endswith("with spaces/dots.and-dashes[1]/a file.py") for p in found)


# --- 23-26. incremental transitions -------------------------------------------


def sync_again(settings: Settings, root: Path) -> None:
    workspace = open_at(settings, root)
    try:
        context = workspace.resolver().resolve(root, command="sync")
        sync(workspace, context)
    finally:
        workspace.close()


def indexed_paths(settings: Settings, root: Path) -> set[str]:
    workspace = open_at(settings, root)
    try:
        rows = workspace.database.execute("SELECT rel_path FROM files").fetchall()
        return {str(r["rel_path"]) for r in rows}
    finally:
        workspace.close()


def test_a_file_moved_from_depth_63_to_depth_65_is_not_left_stale(
    workspace_root: Path, tmp_path: Path
) -> None:
    """The dangerous transition: the old code kept the stale fact and lost the file."""
    repo = make_repo(workspace_root / "r")
    shallow = deep_file(repo, 63, "moving.py", "MOVING_MARKER = 1\n")
    settings = settings_for(tmp_path)
    indexed_via(settings, workspace_root, tmp_path / "c.yaml")
    before = indexed_paths(settings, workspace_root)
    assert any(p.endswith("moving.py") for p in before)
    old_rel = next(p for p in before if p.endswith("moving.py"))

    shallow.unlink()
    deep_file(repo, 65, "moving.py", "MOVING_MARKER = 1\n")
    sync_again(settings, workspace_root)

    after = indexed_paths(settings, workspace_root)
    assert old_rel not in after, "the depth-63 path survived as a stale fact"
    new_rel = next(p for p in after if p.endswith("moving.py"))
    assert new_rel.count("/") == 65


def test_a_shallow_file_moved_deep_is_reindexed(workspace_root: Path, tmp_path: Path) -> None:
    repo = make_repo(workspace_root / "r", {"src/app.py": "APP = 1\n"})
    settings = settings_for(tmp_path)
    indexed_via(settings, workspace_root, tmp_path / "c.yaml")
    assert "src/app.py" in indexed_paths(settings, workspace_root)

    (repo / "src" / "app.py").unlink()
    deep_file(repo, 80, "app.py", "APP = 1\n")
    sync_again(settings, workspace_root)

    after = indexed_paths(settings, workspace_root)
    assert "src/app.py" not in after
    assert any(p.endswith("app.py") and p.count("/") == 80 for p in after)


def test_a_deep_file_deletion_removes_every_fact(workspace_root: Path, tmp_path: Path) -> None:
    repo = make_repo(workspace_root / "r", {"keep.py": "KEEP = 1\n"})
    target = deep_file(
        repo, 80, "gone.py", "DEEP_DELETE_MARKER = 1\n\n\ndef deep_fn():\n    return 1\n"
    )
    settings = settings_for(tmp_path)
    indexed_via(settings, workspace_root, tmp_path / "c.yaml")

    workspace = open_at(settings, workspace_root)
    try:
        assert workspace.database.execute(
            "SELECT COUNT(*) AS n FROM entities WHERE name = 'deep_fn'"
        ).fetchone()["n"]
    finally:
        workspace.close()

    target.unlink()
    sync_again(settings, workspace_root)

    workspace = open_at(settings, workspace_root)
    try:
        assert not any(p.endswith("gone.py") for p in indexed_paths(settings, workspace_root))
        for table, column in (("entities", "name"), ("chunks", "text")):
            leftover = workspace.database.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE {column} LIKE '%deep_fn%'"  # noqa: S608
            ).fetchone()
            assert int(leftover["n"]) == 0, f"stale rows left in {table}"
    finally:
        workspace.close()


def test_incremental_deep_edits_equal_a_clean_index(workspace_root: Path, tmp_path: Path) -> None:
    """Invariant 2 of ARCHITECTURE.md §9, driven entirely through incremental syncs."""
    repo = make_repo(workspace_root / "r", {"root.py": "ROOT = 1\n"})
    deep_file(repo, 80, "deep.py", "DEEP = 1\n\n\ndef first():\n    return 1\n")
    settings = settings_for(tmp_path)
    indexed_via(settings, workspace_root, tmp_path / "c.yaml")

    deep_file(repo, 80, "deep.py", "DEEP = 2\n\n\ndef second():\n    return 2\n")
    sync_again(settings, workspace_root)

    (deep_dir(repo, 80) / "deep.py").unlink()
    deep_file(repo, 90, "moved.py", "MOVED = 3\n\n\ndef third():\n    return 3\n")
    sync_again(settings, workspace_root)

    workspace = open_at(settings, workspace_root)
    try:
        incremental = fact_snapshot(workspace)
    finally:
        workspace.close()

    clean = reindex_from_scratch(settings_for(tmp_path, "clean"), workspace_root, tmp_path, "clean")
    assert incremental == clean


def test_a_second_sync_converges_on_the_same_index(workspace_root: Path, tmp_path: Path) -> None:
    """Enumeration is redone every run and never cached as persistent state."""
    repo = make_repo(workspace_root / "r", {"a.py": "A = 1\n"})
    deep_file(repo, 80, "deep.py", "DEEP = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    settings = settings_for(tmp_path)

    indexed_via(settings, workspace_root, tmp_path / "c.yaml")
    after_first = indexed_paths(settings, workspace_root)

    workspace = open_at(settings, workspace_root)
    try:
        context = workspace.resolver().resolve(workspace_root, command="sync")
        sync(workspace, context)
    finally:
        workspace.close()

    assert indexed_paths(settings, workspace_root) == after_first


# --- 28-30. repository membership ---------------------------------------------


def test_a_nested_repository_at_depth_is_not_absorbed_by_its_parent(tmp_path: Path) -> None:
    root = tmp_path / "outer"
    make_repo(root, {"outer.py": "O = 1\n"})
    inner = deep_dir(root, 80) / "inner"
    make_repo(inner, {"inner.py": "I = 1\n"})

    found = scanned(root)

    assert any(p.endswith("outer.py") for p in found)
    assert not any(p.endswith("inner.py") for p in found), "nested repository content absorbed"


def test_the_managed_root_as_repository_enumerates_deep_files(tmp_path: Path) -> None:
    workspace_root = tmp_path / "root"
    make_repo(workspace_root, {"top.py": "T = 1\n"})
    deep_file(workspace_root, 80, "deep.py", "ROOT_REPO_DEEP_MARKER = 1\n")

    settings = settings_for(tmp_path)
    files, _ = indexed_via(settings, workspace_root, tmp_path / "c.yaml")

    assert "top.py" in files
    assert any(p.endswith("deep.py") and p.count("/") == 80 for p in files)


# --- 31-34. product surface ----------------------------------------------------


def test_a_deep_marker_is_retrievable_after_a_sync(workspace_root: Path, tmp_path: Path) -> None:
    """Proof the fix reaches the product, not just the scanner's return value."""
    repo = make_repo(workspace_root / "r", {"shallow.py": "S = 1\n"})
    deep_file(
        repo,
        80,
        "deep.py",
        "DEEP_UNIQUE_SYMBOL_918273 = 'x'\n\n\ndef deep_unique_function_918273():\n"
        "    return DEEP_UNIQUE_SYMBOL_918273\n",
    )
    settings = settings_for(tmp_path)
    indexed_via(settings, workspace_root, tmp_path / "c.yaml")

    workspace = open_at(settings, workspace_root)
    try:
        from repoatlas.services.retrieval_service import locate

        context = workspace.resolver().resolve(workspace_root, command="locate")
        evidence = locate(workspace, "DEEP_UNIQUE_SYMBOL_918273", context)
        assert evidence.candidates, "the deep marker is not retrievable"
        assert any("deep.py" in repr(candidate) for candidate in evidence.candidates)
    finally:
        workspace.close()


def test_deep_files_are_chunked_like_shallow_ones(workspace_root: Path, tmp_path: Path) -> None:
    repo = make_repo(workspace_root / "r", {"shallow.py": "def shallow_fn():\n    return 1\n"})
    deep_file(repo, 80, "deep.py", "def deep_fn():\n    return 2\n")

    settings = settings_for(tmp_path)
    indexed_via(settings, workspace_root, tmp_path / "c.yaml")

    workspace = open_at(settings, workspace_root)
    try:
        row = workspace.database.execute(
            "SELECT COUNT(*) AS n FROM chunks c JOIN files f ON f.id = c.file_id "
            "WHERE f.rel_path LIKE '%deep.py'"
        ).fetchone()
        assert int(row["n"]) > 0, "the deep file produced no chunks"
    finally:
        workspace.close()


def test_provenance_of_a_deep_file_is_intact(workspace_root: Path, tmp_path: Path) -> None:
    repo = make_repo(workspace_root / "r")
    deep_file(repo, 80, "prov.py", "CONST = 1\n\n\ndef named_function():\n    return CONST\n")

    settings = settings_for(tmp_path)
    indexed_via(settings, workspace_root, tmp_path / "c.yaml")

    workspace = open_at(settings, workspace_root)
    try:
        row = workspace.database.execute(
            "SELECT f.rel_path, f.basename, f.parser, e.line_start, e.line_end "
            "FROM entities e JOIN files f ON f.id = e.file_id WHERE e.name = 'named_function'"
        ).fetchone()
        assert row is not None
        assert str(row["basename"]) == "prov.py"
        assert str(row["rel_path"]).count("/") == 80
        assert str(row["parser"]) == "python-ast"
        assert int(row["line_start"]) >= 1
        assert int(row["line_end"]) >= int(row["line_start"])
    finally:
        workspace.close()


def test_status_is_current_after_a_deep_sync(workspace_root: Path, tmp_path: Path) -> None:
    """Depth introduces no index state of its own."""
    repo = make_repo(workspace_root / "r", {"a.py": "A = 1\n"})
    deep_file(repo, 80, "deep.py", "DEEP = 1\n")
    # Committed, so the working tree starts clean. `status` compares HEAD and the
    # dirty flag; an edit inside an already-dirty tree moves neither, and content
    # freshness is established by the sync rather than by `status` (V1_SPEC §12).
    # Pinning the existing semantics, not inventing stricter ones for deep files.
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", "deep")
    settings = settings_for(tmp_path)

    workspace = open_at(settings, workspace_root)
    try:
        initialize(workspace, workspace_root, config_path=tmp_path / "c.yaml")
    finally:
        workspace.close()

    def state() -> str:
        workspace = open_at(settings, workspace_root)
        try:
            context = workspace.resolver().resolve(workspace_root, command="status")
            return status(workspace, context).index_state
        finally:
            workspace.close()

    assert state().startswith("current"), "a completed sync is not current"

    deep_file(repo, 80, "deep.py", "DEEP = 2\nEXTRA = 3\n")
    assert not state().startswith("current"), "a deep edit went unnoticed"

    workspace = open_at(settings, workspace_root)
    try:
        context = workspace.resolver().resolve(workspace_root, command="sync")
        sync(workspace, context)
    finally:
        workspace.close()

    assert state().startswith("current"), "the deep edit was not brought current"


def test_the_scanner_writes_nothing_to_the_repository(tmp_path: Path) -> None:
    """Enumeration is inspection. No repository code runs, nothing is created.

    The working tree is compared, not `.git`: `ls-files` is a read of the index,
    but git is entitled to touch its own metadata, and `--no-optional-locks`
    (which `GitClient` always passes) is what keeps it from doing so gratuitously.
    What must not change is the content the repository's owner can see.
    """
    root = tmp_path / "repo"
    root.mkdir()
    deep_file(root, 70, "a.py", "A = 1\n")
    init_git_repository(root)

    def worktree() -> dict[Path, int]:
        return {
            entry: entry.stat().st_mtime_ns
            for entry in root.rglob("*")
            if ".git" not in entry.relative_to(root).parts
        }

    before = worktree()

    scanned(root)

    assert worktree() == before, "enumeration modified the tree"


def test_enumeration_costs_one_subprocess_however_deep_the_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Correctness must not become one process per directory.

    This used to assert *zero* subprocesses, because the walk it measured never
    ran git. Enumeration is git now, so zero is the wrong bound; the property that
    still matters is that the count is constant — one `ls-files` — and does not
    grow with the size or depth of the tree.
    """

    def count_runs(root: Path) -> tuple[int, int]:
        calls: list[object] = []
        original = subprocess.run

        def counting(*args: object, **kwargs: object):
            calls.append(args)
            return original(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(subprocess, "run", counting)
        try:
            found = scanned(root)
        finally:
            monkeypatch.undo()
        return len(found), len(calls)

    small = tmp_path / "small"
    small.mkdir()
    for index in range(5):
        deep_file(small, 65 + index, f"m{index}.py", "X = 1\n")
    init_git_repository(small)

    large = tmp_path / "large"
    large.mkdir()
    for index in range(60):
        deep_file(large, 65 + index, f"m{index}.py", "X = 1\n")
    init_git_repository(large)

    small_files, small_calls = count_runs(small)
    large_files, large_calls = count_runs(large)

    assert (small_files, large_files) == (5, 60)
    assert small_calls == 1, f"enumeration spawned {small_calls} subprocess(es)"
    assert large_calls == small_calls, f"cost scales with the tree: {small_calls} -> {large_calls}"


def test_scan_repository_applies_containment_to_deep_candidates(tmp_path: Path) -> None:
    """Enumeration finding a path is never admission; the boundary still decides."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("ESCAPED = 1\n", encoding="utf-8")
    root = tmp_path / "repo"
    root.mkdir()
    directory = deep_dir(root, 70)
    (directory / "real.py").write_text("REAL = 1\n", encoding="utf-8")

    init_git_repository(root)
    scanned = scan_repository(root)
    names = {f.rel_path for f in scanned}

    assert any(n.endswith("real.py") for n in names)
    # Replace a real directory with a symlink out, exactly as the containment
    # module describes: the path stays, the object it names leaves the tree.
    shutil.rmtree(directory)
    deep_dir(root, 69)
    (deep_dir(root, 69) / "d").symlink_to(outside, target_is_directory=True)
    rescanned = scan_repository(root)
    assert not any("secret.py" in f.rel_path for f in rescanned)


def test_a_file_that_disappears_between_listing_and_stat_is_dropped_quietly(
    tmp_path: Path,
) -> None:
    """A path git listed that is gone by the time it is stat-ed.

    This test used to assert that such a subtree was *reported*. That was a
    property of the filesystem walk, which classified a directory and then failed
    to enter it; git's enumerator never had it, and the walk is gone. What is left
    is the honest behaviour of the path production takes: the file is skipped,
    every other file is indexed, and nothing raises.

    It is not a silent-truncation defect of the kind this suite exists to catch.
    The window is between one `ls-files` and one `stat`, the missing file is
    genuinely absent from the tree at the moment it is asked about, and the next
    sync re-enumerates from scratch. The reportable case — git could not *look* —
    is a different one, and is asserted directly above.
    """
    root = tmp_path / "repo"
    root.mkdir()
    (root / "top.py").write_text("T = 1\n", encoding="utf-8")
    doomed = root / "sub"
    doomed.mkdir()
    (doomed / "under.py").write_text("U = 1\n", encoding="utf-8")
    init_git_repository(root)

    listing = GitClient().list_files(root)
    assert listing is not None
    assert "sub/under.py" in listing.paths

    shutil.rmtree(doomed)

    found = scanned(root)

    assert "top.py" in found
    assert not any("under.py" in path for path in found)
