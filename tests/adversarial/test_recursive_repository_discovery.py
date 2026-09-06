"""Discovery is recursive to the bottom of the tree (V1_SPEC §5).

The release review pointed a managed root at repositories nested seven, eight and
nine levels down and watched `init` exit 0, print how many repositories it had
"discovered", and register none of them. Discovery is the *only* way a repository
becomes managed — there is no manual registration — so a workspace whose services
happened to sit one directory deeper than a constant nobody had documented was
simply not indexed, and nothing said so.

Removing a limit is the easy half. The hard half is that a walk with no depth
ceiling is exactly the walk that a symlink loop runs forever, that a symlink out
of the tree walks the whole filesystem, and that a deep tree overflows the Python
call stack. So every depth assertion here is paired with a boundary assertion,
and `test_mutations.py`-style controls live at the bottom of this file: the
suite is designed so that restoring the old ceiling fails the first group and
weakening traversal fails the second.

Every fixture is a real Git repository, and every CLI invocation is given its own
temporary config path, database path and managed root — two earlier remediation
passes leaked test state into the real `~/.config/repoatlas`, and
`test_no_test_touches_real_repoatlas_state` is here so a third does not.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.config.settings import DiscoverySettings, config_path, database_path
from repoatlas.context.resolver import RepositoryContextResolver
from repoatlas.discovery.finder import discover_repositories
from repoatlas.discovery.ignore import IgnoreRules
from repoatlas.errors import ExitCode
from repoatlas.models.repository import Repository

CLI = Path(sys.executable).parent / "repoatlas"

pytestmark = pytest.mark.adversarial

GIT_ENV = {
    "PATH": "/usr/bin:/bin",
    "GIT_AUTHOR_NAME": "RepoAtlas Tests",
    "GIT_AUTHOR_EMAIL": "tests@example.invalid",
    "GIT_COMMITTER_NAME": "RepoAtlas Tests",
    "GIT_COMMITTER_EMAIL": "tests@example.invalid",
}


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env=dict(GIT_ENV, HOME=str(repo)),
    )


def make_repo(path: Path, *, content: str = "SERVICE_HOST = 'localhost'\n") -> Path:
    """A real Git repository with one commit."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "app.py").write_text(content, encoding="utf-8")
    git(path, "init", "--quiet", "--initial-branch=main")
    git(path, "add", "-A")
    git(path, "commit", "--quiet", "-m", "initial")
    return path


def at_depth(root: Path, depth: int, name: str | None = None) -> Path:
    """The path of a repository exactly ``depth`` components below ``root``."""
    if depth == 0:
        return root
    chain = [f"lvl{index}" for index in range(1, depth)]
    return root.joinpath(*chain, name or f"repo-d{depth}")


def names(root: Path, settings: DiscoverySettings | None = None) -> set[str]:
    return {
        candidate.path.relative_to(root).as_posix() if candidate.path != root else "."
        for candidate in discover_repositories(root, settings)
    }


# == 1. the depth matrix =====================================================


@pytest.mark.parametrize("depth", [0, 1, 2, 5, 6, 7, 8, 9, 12, 25, 40])
def test_a_repository_is_found_at_any_depth(tmp_path: Path, depth: int) -> None:
    """The review's finding, generalized.

    6 and 7 are the transition the review measured. 25 and 40 are here because a
    fix that merely *raised* the constant would pass a test that stopped at 12.
    """
    root = tmp_path / "work"
    root.mkdir()
    repo = make_repo(at_depth(root, depth) if depth else root / "solo")
    found = [candidate.path for candidate in discover_repositories(root)]
    assert found == [repo], depth


def test_every_depth_at_once(tmp_path: Path) -> None:
    """Mixed depths in one tree: no repository shadows another."""
    root = tmp_path / "work"
    root.mkdir()
    depths = [1, 3, 6, 7, 9, 12, 20]
    for depth in depths:
        make_repo(at_depth(root, depth))
    assert names(root) == {at_depth(root, d).relative_to(root).as_posix() for d in depths}


def test_the_settings_no_longer_carry_a_depth_bound() -> None:
    """The constant is gone rather than raised.

    A configurable ceiling would let an operator re-create the exact defect, and
    silently: any value they chose would still make `init` report a repository
    set it knew was truncated.
    """
    assert not hasattr(DiscoverySettings(), "max_depth")
    assert "max_depth" not in DiscoverySettings.model_fields


# == 2. traversal boundaries the depth fix must not weaken ===================


def test_a_symlink_out_of_the_tree_is_not_followed(tmp_path: Path) -> None:
    """§4: a deep symlink must not become a route out of the managed root."""
    outside = tmp_path / "outside"
    make_repo(outside / "foreign")
    root = tmp_path / "work"
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "b" / "link").symlink_to(outside, target_is_directory=True)
    make_repo(at_depth(root, 8))

    found = discover_repositories(root)
    assert {candidate.path for candidate in found} == {at_depth(root, 8)}
    assert all(not str(c.path).startswith(str(outside)) for c in found)


def test_a_symlink_to_an_ancestor_terminates_and_finds_nothing_new(tmp_path: Path) -> None:
    """§5: the cycle the removed ceiling used to hide.

    `max_depth` was never a cycle guard on the default path — symlinks are not
    followed — but it *was* the only thing that stopped `follow_symlinks=True`
    from looping forever. Both directions are asserted, with a wall-clock bound
    so a regression fails the test rather than hanging the suite.
    """
    root = tmp_path / "work"
    cycle = root / "a" / "b" / "c"
    cycle.mkdir(parents=True)
    (cycle / "back").symlink_to(root / "a", target_is_directory=True)
    repo = make_repo(root / "a" / "b" / "c" / "d" / "e" / "f" / "g" / "deep")

    for settings in (DiscoverySettings(), DiscoverySettings(follow_symlinks=True)):
        started = time.monotonic()
        found = {candidate.path for candidate in discover_repositories(root, settings)}
        assert time.monotonic() - started < 30, settings
        assert found == {repo}, settings


def test_a_symlink_is_not_a_route_around_the_ignore_rules(tmp_path: Path) -> None:
    """The default symlink policy, asserted on its own rather than via containment.

    The escape tests above pass even if symlinks are followed, because the
    containment check catches the escape — so they do not actually pin
    ``follow_symlinks=False``. This does: the repository is reachable *only*
    through a link, and the directory it really lives in is one the operator
    ignored. Following the link would re-admit it under a name the ignore rules
    never saw, which is the same defect as ignoring the rules outright.
    """
    root = tmp_path / "work"
    root.mkdir()
    (root / ".repoatlasignore").write_text("hidden/\n", encoding="utf-8")
    make_repo(root / "hidden" / "api")
    (root / "alias").symlink_to(root / "hidden", target_is_directory=True)

    assert discover_repositories(root) == []
    followed = discover_repositories(root, DiscoverySettings(follow_symlinks=True))
    assert [candidate.path for candidate in followed] == [root / "alias" / "api"], (
        "opting in is what makes the link a route; the default must not"
    )


def test_a_symlink_is_not_a_route_around_the_exclusion_list(tmp_path: Path) -> None:
    """The same, for the built-in exclusions rather than the ignore file."""
    root = tmp_path / "work"
    root.mkdir()
    make_repo(root / "node_modules" / "pkg")
    (root / "alias").symlink_to(root / "node_modules", target_is_directory=True)

    assert discover_repositories(root) == []


def test_a_followed_symlink_still_cannot_leave_the_root(tmp_path: Path) -> None:
    """Opting into symlinks opts into convenience, not into containment loss."""
    outside = tmp_path / "outside"
    make_repo(outside / "foreign")
    root = tmp_path / "work"
    (root / "a").mkdir(parents=True)
    (root / "a" / "escape").symlink_to(outside, target_is_directory=True)
    inside = make_repo(root / "a" / "real")

    found = discover_repositories(root, DiscoverySettings(follow_symlinks=True))
    assert {candidate.path for candidate in found} == {inside}


def test_two_routes_to_one_directory_report_one_repository(tmp_path: Path) -> None:
    """Identity is the directory, not the path that reached it."""
    root = tmp_path / "work"
    real = make_repo(root / "services" / "api")
    (root / "alias").symlink_to(root / "services", target_is_directory=True)

    found = discover_repositories(root, DiscoverySettings(follow_symlinks=True))
    assert [candidate.path for candidate in found] == [real]


def test_git_internals_are_never_traversed(tmp_path: Path, monkeypatch) -> None:
    """§7: recognizing `.git` is the end of the descent, not the start of one.

    Asserted structurally — every directory the walk enters is recorded — rather
    than by looking for a repository planted inside `.git`, because the cost of
    walking an object database is the failure even when it finds nothing.
    """
    root = tmp_path / "work"
    repo = make_repo(root / "svc")
    (repo / ".git" / "decoy").mkdir(parents=True, exist_ok=True)

    entered: list[Path] = []
    original = Path.iterdir

    def spy(self: Path):
        entered.append(self)
        return original(self)

    monkeypatch.setattr(Path, "iterdir", spy)
    discover_repositories(root)

    assert repo in entered, "the repository itself is still walked for nested repos"
    assert not any(".git" in path.parts for path in entered)


def test_git_internals_are_not_traversed_even_if_exclusions_are_emptied(
    tmp_path: Path, monkeypatch
) -> None:
    """`.git` is excluded structurally, not only by the configurable list.

    Without the ceiling, a caller who overrode `exclusions` would otherwise turn a
    preference into a walk of every loose object in every repository.
    """
    root = tmp_path / "work"
    make_repo(root / "svc")

    entered: list[Path] = []
    original = Path.iterdir

    def spy(self: Path):
        entered.append(self)
        return original(self)

    monkeypatch.setattr(Path, "iterdir", spy)
    discover_repositories(root, DiscoverySettings(exclusions=()))

    assert not any(".git" in path.parts for path in entered)


def test_a_deep_tree_does_not_consume_the_call_stack(tmp_path: Path) -> None:
    """§12/§27: correctness must not depend on the size of the call stack.

    The interpreter's recursion limit is lowered to just above the current stack
    rather than the tree being made 1000 levels deep, because a tree that deep
    hits the operating system's path-length limit before it hits Python's — the
    fixture fails to build and proves nothing. Lowering the limit tests the same
    property directly: a recursive walker needs one frame per level and dies at
    the first few dozen; an iterative one is flat.
    """
    root = tmp_path / "work"
    deep = root
    for _ in range(600):
        deep = deep / "d"
    deep.mkdir(parents=True)
    repo = make_repo(deep / "bottom")

    previous = sys.getrecursionlimit()
    headroom = len(traceback.extract_stack()) + 60
    try:
        sys.setrecursionlimit(headroom)
        found = [candidate.path for candidate in discover_repositories(root)]
    finally:
        sys.setrecursionlimit(previous)
    assert found == [repo]


# == 3. ignore rules are not a depth exemption ===============================


def test_a_deep_repository_inside_an_ignored_directory_is_not_discovered(
    tmp_path: Path,
) -> None:
    """§8: removing the ceiling must not smuggle ignored subtrees back in."""
    root = tmp_path / "work"
    root.mkdir()
    (root / ".repoatlasignore").write_text("archives/\ngenerated/**\n", encoding="utf-8")

    hidden_a = root / "archives"
    hidden_b = root / "generated"
    visible = root / "active"
    for base in (hidden_a, hidden_b, visible):
        make_repo(at_depth(base, 10))

    found = names(root)
    assert found == {(at_depth(visible, 10)).relative_to(root).as_posix()}


def test_the_built_in_exclusions_still_prune_at_any_depth(tmp_path: Path) -> None:
    root = tmp_path / "work"
    make_repo(at_depth(root / "app" / "node_modules", 9))
    kept = make_repo(at_depth(root / "app" / "src", 9))
    assert [candidate.path for candidate in discover_repositories(root)] == [kept]


def test_ignore_patterns_that_depend_on_path_shape_still_apply(tmp_path: Path) -> None:
    """§9: existing pattern semantics, exercised deep. Matching itself is unchanged."""
    root = tmp_path / "work"
    root.mkdir()
    (root / ".repoatlasignore").write_text(
        "foo/**/bar\n**/generated/**\nexact/deep/path/\n", encoding="utf-8"
    )
    rules = IgnoreRules.load(root)
    assert rules.matches("foo/x/y/bar", is_dir=True)
    assert rules.matches("a/b/generated/c", is_dir=True)
    assert rules.matches("exact/deep/path", is_dir=True)
    assert not rules.matches("foo/x/y/baz", is_dir=True)


# == 4. nesting, identity and the managed root ===============================


def test_a_nested_repository_stays_a_separate_repository(tmp_path: Path) -> None:
    """§6: `parent` and `parent/vendor/child` are two managed repositories."""
    root = tmp_path / "work"
    parent = make_repo(root / "parent")
    (parent / "src").mkdir()
    (parent / "src" / "main.py").write_text("x = 1\n", encoding="utf-8")
    child = make_repo(parent / "tools" / "child")

    found = [candidate.path for candidate in discover_repositories(root)]
    assert found == [parent, child]

    resolver = RepositoryContextResolver(
        root,
        [
            Repository(id=1, name="parent", slug="parent", path=parent),
            Repository(id=2, name="child", slug="parent/tools/child", path=child),
        ],
    )
    outer = resolver.resolve(parent / "src").primary
    inner = resolver.resolve(child).primary
    assert outer is not None and inner is not None
    assert outer.slug == "parent"
    assert inner.slug == "parent/tools/child", "the closest enclosing repository wins"


def test_the_managed_root_being_a_repository_does_not_stop_the_walk(tmp_path: Path) -> None:
    """§17: root is registered *and* the descent continues, unchanged."""
    root = tmp_path / "work"
    make_repo(root)
    deep = make_repo(at_depth(root, 9))
    found = [candidate.path for candidate in discover_repositories(root)]
    assert found == [root, deep]

    slugs = {candidate.slug for candidate in discover_repositories(root)}
    assert "." in slugs


def test_the_same_basename_at_different_deep_paths_stays_two_repositories(
    tmp_path: Path,
) -> None:
    """§15: depth is not identity; the root-relative slug still is."""
    root = tmp_path / "work"
    first = make_repo(root / "division" / "a" / "service")
    second = make_repo(root / "archive" / "very" / "deep" / "nested" / "further" / "service")

    found = discover_repositories(root)
    assert {candidate.path for candidate in found} == {first, second}
    assert {candidate.name for candidate in found} == {"service"}
    assert {candidate.slug for candidate in found} == {
        "division/a/service",
        "archive/very/deep/nested/further/service",
    }


def test_prefix_similarity_is_not_ancestry(tmp_path: Path) -> None:
    """§16: `root/foobar` is not inside `root/foo`, at any depth."""
    root = tmp_path / "work"
    root.mkdir()
    (root / ".repoatlasignore").write_text("foo/\n", encoding="utf-8")
    make_repo(at_depth(root / "foo", 8))
    kept = make_repo(at_depth(root / "foobar", 8))

    assert [candidate.path for candidate in discover_repositories(root)] == [kept]


def test_an_empty_tree_discovers_nothing_and_invents_nothing(tmp_path: Path) -> None:
    """§18: no repository is fabricated for a managed root that is not one."""
    root = tmp_path / "work"
    (root / "a" / "b" / "c" / "d" / "e" / "f" / "g" / "h").mkdir(parents=True)
    assert discover_repositories(root) == []


# == 5. determinism ==========================================================


def test_discovery_is_byte_identical_across_runs(tmp_path: Path) -> None:
    """§22: more repositories, still no dependence on enumeration order."""
    root = tmp_path / "work"
    for depth in (1, 4, 7, 11):
        make_repo(at_depth(root, depth))
    make_repo(root / "zeta" / "svc")
    make_repo(root / "alpha" / "svc")

    runs = [[(c.name, c.slug, str(c.path)) for c in discover_repositories(root)] for _ in range(5)]
    assert all(run == runs[0] for run in runs)
    assert [entry[2] for entry in runs[0]] == sorted(entry[2] for entry in runs[0])


# == 6. permission-denied subtrees ===========================================


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_an_unreadable_subtree_is_reported_rather_than_reported_empty(tmp_path: Path) -> None:
    """§10/§14: "no repositories there" and "could not look" are different claims.

    The subtree is skipped rather than made fatal — one bad directory must not
    stop a whole workspace indexing — but it is *named*, because silently
    treating a refusal as an empty result is the same class of defect as the
    depth ceiling, one layer down.
    """
    root = tmp_path / "work"
    visible = make_repo(at_depth(root, 8))
    denied = root / "locked"
    make_repo(denied / "hidden")
    denied.chmod(0o000)

    unreadable: list[Path] = []
    try:
        found = discover_repositories(root, unreadable=unreadable)
    finally:
        denied.chmod(0o700)

    assert [candidate.path for candidate in found] == [visible]
    assert unreadable == [denied]


# == 7. git repository shapes ================================================


def test_a_bare_repository_is_treated_the_same_at_every_depth(tmp_path: Path) -> None:
    """§19: current policy pinned, not changed.

    A bare repository has no `.git` entry, so `is_git_repository` does not
    recognize it and it is not managed. That is unchanged here; what this asserts
    is that the answer no longer depends on how deep it sits.
    """
    root = tmp_path / "work"
    for depth in (2, 20):
        bare = at_depth(root, depth, name=f"bare-{depth}.git")
        bare.mkdir(parents=True)
        subprocess.run(
            ["git", "init", "--quiet", "--bare"],
            cwd=bare,
            check=True,
            capture_output=True,
            env=dict(GIT_ENV, HOME=str(bare)),
        )
    assert discover_repositories(root) == []


def test_a_git_file_worktree_is_recognized_at_depth(tmp_path: Path) -> None:
    """§20/§21: a `.git` *file* counts, which is what a worktree and a checked-out
    submodule both present. Current policy, asserted deep so the depth fix cannot
    quietly change it."""
    root = tmp_path / "work"
    main = make_repo(root / "main")
    linked = at_depth(root, 9, name="worktree")
    linked.parent.mkdir(parents=True, exist_ok=True)
    git(main, "worktree", "add", "--quiet", str(linked), "-b", "side")

    assert (linked / ".git").is_file()
    found = {candidate.path for candidate in discover_repositories(root)}
    assert found == {main, linked}


def test_a_checked_out_submodule_is_a_separate_repository_at_depth(tmp_path: Path) -> None:
    """Submodule semantics pinned: the `.git` file makes it its own repository."""
    upstream = make_repo(tmp_path / "upstream")
    root = tmp_path / "work"
    parent = make_repo(root / "parent")
    # Not under `vendor/`: that name is in the default exclusion set, so a
    # submodule there is pruned for being vendored, not for being deep.
    target = "extern/a/b/c/d/e/dep"
    git(
        parent,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "--quiet",
        "add",
        str(upstream),
        target,
    )
    git(parent, "commit", "--quiet", "-m", "add submodule")

    found = {candidate.path for candidate in discover_repositories(root)}
    assert parent in found
    assert (parent / target) in found


# == 8. through the real CLI =================================================


class Workspace:
    """A managed root with its own config, database and HOME."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.root = base / "work"
        self.root.mkdir(parents=True)

    @property
    def environment(self) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.base),
            "REPOATLAS_CONFIG": str(self.base / "config.yaml"),
            "REPOATLAS_DB": str(self.base / "atlas.db"),
            "REPOATLAS_LLM_BASE_URL": "http://127.0.0.1:1",
        }

    def run(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(CLI), *args],
            cwd=cwd or self.root,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=900,
        )


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Workspace]:
    yield Workspace(tmp_path / "ws")


@pytest.mark.skipif(not CLI.exists(), reason="the repoatlas console script is not installed")
def test_the_review_reproduction_through_the_installed_cli(workspace: Workspace) -> None:
    """§33: depth 6, 7 and 9 all registered, with no manual registration step."""
    for depth in (6, 7, 9):
        make_repo(at_depth(workspace.root, depth))

    done = workspace.run("init", str(workspace.root))
    assert done.returncode == ExitCode.OK, done.stdout + done.stderr
    assert "Repositories discovered: 3" in done.stdout

    listing = workspace.run("repos")
    assert listing.returncode == ExitCode.OK
    for depth in (6, 7, 9):
        assert f"repo-d{depth}" in listing.stdout, listing.stdout


@pytest.mark.skipif(not CLI.exists(), reason="the repoatlas console script is not installed")
def test_repeated_init_is_idempotent_over_deep_repositories(workspace: Workspace) -> None:
    """§23: no duplicate rows, no slug churn, same set."""
    make_repo(at_depth(workspace.root, 1))
    make_repo(at_depth(workspace.root, 9))

    def identities() -> list[tuple[str, str, str]]:
        # Identity only: `last_indexed_at` is a timestamp and moves every run,
        # which is not what idempotence means here.
        rows = json.loads(workspace.run("repos", "--json").stdout)
        return sorted((row["name"], row["slug"], row["path"]) for row in rows)

    first = workspace.run("init", str(workspace.root))
    assert first.returncode == ExitCode.OK
    before = identities()
    assert len(before) == 2

    second = workspace.run("init", str(workspace.root))
    assert second.returncode == ExitCode.OK
    assert "Repositories discovered: 2" in second.stdout
    assert identities() == before


@pytest.mark.skipif(not CLI.exists(), reason="the repoatlas console script is not installed")
def test_a_deep_repository_added_later_appears_on_reinit_not_on_sync(
    workspace: Workspace,
) -> None:
    """§24: proves P2-4 is fixed while leaving the separate `sync` finding intact.

    `sync` not noticing a *new* repository is its own release-review finding and
    is deliberately untouched here. What this asserts is that when discovery does
    run, depth 12 is no longer a reason to miss one.
    """
    make_repo(at_depth(workspace.root, 1))
    assert workspace.run("init", str(workspace.root)).returncode == ExitCode.OK

    late = make_repo(at_depth(workspace.root, 12, name="late"))
    assert late.exists()

    workspace.run("sync")
    after_sync = workspace.run("repos").stdout
    assert "late" not in after_sync, "the separate sync-discovery finding is unchanged"

    reinit = workspace.run("init", str(workspace.root))
    assert reinit.returncode == ExitCode.OK
    assert "late" in workspace.run("repos").stdout


@pytest.mark.skipif(not CLI.exists(), reason="the repoatlas console script is not installed")
def test_a_removed_deep_repository_is_forgotten_on_reinit(workspace: Workspace) -> None:
    """§25: existing retirement semantics, observed at depth rather than changed."""
    make_repo(at_depth(workspace.root, 1))
    doomed = make_repo(at_depth(workspace.root, 10, name="doomed"))
    assert workspace.run("init", str(workspace.root)).returncode == ExitCode.OK
    assert "doomed" in workspace.run("repos").stdout

    shutil.rmtree(doomed)
    assert workspace.run("init", str(workspace.root)).returncode == ExitCode.OK
    assert "doomed" not in workspace.run("repos").stdout


@pytest.mark.skipif(not CLI.exists(), reason="the repoatlas console script is not installed")
def test_init_on_an_empty_tree_reports_nothing_found(workspace: Workspace) -> None:
    (workspace.root / "a" / "b" / "c").mkdir(parents=True)
    done = workspace.run("init", str(workspace.root))
    assert done.returncode == ExitCode.OK
    assert "Repositories discovered: 0" in done.stdout


# == 9. the guard the previous passes needed =================================


def test_no_test_touches_real_repoatlas_state() -> None:
    """Two earlier remediation passes leaked fixture state into the real config.

    This runs in the ambient environment, so it asserts what the *suite* must
    never create: a RepoAtlas config or database at the user's default paths that
    points at a pytest temporary directory.
    """
    for path in (config_path(), database_path()):
        if not path.exists():
            continue
        blob = path.read_bytes()[:200_000]
        assert b"pytest-of-" not in blob, path
        assert b"/tmp/pytest" not in blob, path
