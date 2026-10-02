"""Repository-set membership is part of what a sync synchronizes (V1_SPEC §5, §12).

The defect this file exists for: ``init`` was the only command that ever looked at
the filesystem for repositories. Afterwards, ``sync`` iterated the stored rows and
found them all current, ``status`` compared those same rows against their own
working trees and answered **current**, and ``locate`` said "no indexed evidence
matches" about a repository sitting in the managed tree. Nothing was wrong with
any individual answer; the question "what is in this root?" had simply stopped
being asked. Recovery required re-running ``init`` — which requires already
knowing the thing the tool was supposed to report.

Every test here builds its own managed root under ``tmp_path`` and supplies a
temporary config and database. Nothing touches real RepoAtlas state.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.discovery.reconcile import plan_repository_set, register
from repoatlas.errors import ExitCode
from repoatlas.models.context import ContextScope
from repoatlas.services.init_service import initialize
from repoatlas.services.retrieval_service import locate
from repoatlas.services.status_service import list_repositories, status
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace
from tests.conftest import fact_snapshot, init_git_repository, reindex_from_scratch

pytestmark = pytest.mark.adversarial

CLI = Path(sys.executable).parent / "repoatlas"


# --- fixtures -----------------------------------------------------------------


def make_repo(path: Path, files: dict[str, str] | None = None) -> Path:
    """A real Git repository with one commit."""
    path.mkdir(parents=True, exist_ok=True)
    for relative, text in (files or {"main.py": "VALUE = 1\n"}).items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    init_git_repository(path)
    return path


def at_depth(root: Path, depth: int, name: str) -> Path:
    """``root/lvl1/.../lvl<depth-1>/<name>`` — a repository at exactly ``depth``."""
    here = root
    for level in range(depth - 1):
        here = here / f"lvl{level + 1}"
    return here / name


def settings_for(base: Path, **overrides: object) -> Settings:
    payload: dict[str, object] = {
        "storage": {"database": str(base / "state" / "atlas.db")},
    }
    payload.update(overrides)
    return Settings.model_validate(payload)


class Managed:
    """A managed root plus its workspace, opened on temporary state throughout."""

    def __init__(self, base: Path, root: Path, settings: Settings) -> None:
        self.base = base
        self.root = root
        self.settings = settings
        self.workspace: Workspace = open_workspace(settings=settings, require_root=False)

    def initialize(self) -> None:
        initialize(self.workspace, self.root, config_path=self.base / "config.yaml")

    def context(self, where: Path | None = None):
        """A freshly resolved context.

        Built per call rather than cached, because the resolver takes a snapshot of
        the registered set and every test here changes that set on purpose.
        """
        return self.workspace.resolver().resolve(where or self.root)

    def sync(self, where: Path | None = None):
        context = self.context(where)
        return sync(self.workspace, context)

    def status(self, where: Path | None = None):
        context = self.context(where)
        return status(self.workspace, context)

    def repos(self) -> list[str]:
        return sorted(repo.slug for repo in list_repositories(self.workspace))

    def locate(self, question: str, where: Path | None = None):
        context = self.context(where)
        return locate(self.workspace, question, context)

    def close(self) -> None:
        self.workspace.close()


@pytest.fixture
def managed(tmp_path: Path) -> Iterator[Managed]:
    """One repository, initialized. Every test grows the tree from here."""
    root = tmp_path / "root"
    root.mkdir()
    make_repo(root / "service-a", {"a.py": "ALPHA_MARKER_918273 = 1\n"})

    workspace = Managed(tmp_path, root, settings_for(tmp_path))
    workspace.initialize()
    yield workspace
    workspace.close()


BETA = {"b.py": "def beta_marker_918273():\n    return 1\n"}


# --- 1-5. the reported defect --------------------------------------------------


def test_a_repository_created_after_init_is_discovered_by_a_plain_sync(
    managed: Managed,
) -> None:
    """The reported defect, stated as the invariant it violates."""
    make_repo(managed.root / "service-b", BETA)
    assert managed.repos() == ["service-a"]

    report = managed.sync()

    assert report.repositories_added == ("service-b",)
    assert managed.repos() == ["service-a", "service-b"]


def test_status_reports_the_workspace_as_stale_before_that_sync(managed: Managed) -> None:
    """``current`` was a false statement, and it was the only one status made."""
    assert managed.status().index_state == "current"

    make_repo(managed.root / "service-b", BETA)

    report = managed.status()
    assert report.index_state == "stale"
    assert report.new_repositories == ("service-b",)


def test_the_same_sync_indexes_what_it_discovered(managed: Managed) -> None:
    """Registered *and* indexed. A row with no facts is not a managed repository."""
    make_repo(managed.root / "service-b", BETA)
    managed.sync()

    after = managed.status()
    assert after.index_state == "current"
    assert after.new_repositories == ()

    indexed = {repo.slug: repo for repo in after.repositories}
    assert indexed["service-b"].indexed_files >= 1
    assert indexed["service-b"].index_state == "current"


def test_the_new_repository_is_queryable_immediately(managed: Managed) -> None:
    """No second sync, no re-init (V1_SPEC §14)."""
    make_repo(managed.root / "service-b", BETA)
    managed.sync()

    evidence = managed.locate("beta_marker_918273")
    returned = [f"{c.repository}/{c.path}" for c in evidence.candidates]

    assert returned, "a symbol in a just-discovered repository must be retrievable"
    assert returned[0] == "service-b/b.py"


def test_the_workspace_is_current_again_afterwards(managed: Managed) -> None:
    make_repo(managed.root / "service-b", BETA)
    managed.sync()
    assert managed.status().index_state == "current"
    assert managed.sync().repositories_added == ()


# --- 6-7. depth (Pass 8 semantics preserved) -----------------------------------


@pytest.mark.parametrize("depth", [1, 7, 12, 25])
def test_a_late_repository_is_found_at_any_depth(managed: Managed, depth: int) -> None:
    """Sync uses the Pass 8 walk, so it inherits the absence of a depth ceiling."""
    late = at_depth(managed.root, depth, "late")
    make_repo(late, {"deep.py": f"DEPTH_{depth} = 1\n"})

    report = managed.sync()

    slug = late.relative_to(managed.root).as_posix()
    assert slug in report.repositories_added
    assert slug in managed.repos()


# --- 8-9. nesting and identity -------------------------------------------------


def test_a_nested_repository_appearing_later_stays_separate(managed: Managed) -> None:
    """Parent and child are two repositories, and context still resolves closest-first."""
    parent = make_repo(managed.root / "parent", {"src/p.py": "PARENT = 1\n"})
    managed.sync()

    child = make_repo(parent / "tools" / "child", {"c.py": "CHILD = 1\n"})
    managed.sync()

    assert {"parent", "parent/tools/child"} <= set(managed.repos())

    from_parent = managed.context(parent / "src")
    assert from_parent.primary is not None
    assert from_parent.primary.slug == "parent"

    from_child = managed.context(child)
    assert from_child.primary is not None
    assert from_child.primary.slug == "parent/tools/child"


def test_late_repositories_sharing_a_basename_keep_distinct_identities(
    managed: Managed,
) -> None:
    make_repo(managed.root / "division" / "service", {"one.py": "ONE = 1\n"})
    make_repo(managed.root / "archive" / "deep" / "service", {"two.py": "TWO = 1\n"})

    report = managed.sync()

    assert sorted(report.repositories_added) == ["archive/deep/service", "division/service"]
    assert {"archive/deep/service", "division/service"} <= set(managed.repos())


# --- 10-12, 16. what must NOT be discovered ------------------------------------


def test_an_ignored_late_repository_is_neither_stale_nor_registered(tmp_path: Path) -> None:
    """`.repoatlasignore` decides membership for sync exactly as it does for init."""
    root = tmp_path / "root"
    root.mkdir()
    (root / ".repoatlasignore").write_text("generated/\n", encoding="utf-8")
    make_repo(root / "service-a", {"a.py": "A = 1\n"})

    managed = Managed(tmp_path, root, settings_for(tmp_path))
    managed.initialize()
    try:
        make_repo(root / "generated" / "deep" / "repo", {"g.py": "G = 1\n"})
        make_repo(root / "active" / "deep" / "repo", {"h.py": "H = 1\n"})

        drift = managed.status()
        assert drift.new_repositories == ("active/deep/repo",)
        assert drift.index_state == "stale"

        managed.sync()
        assert managed.repos() == ["active/deep/repo", "service-a"]
    finally:
        managed.close()


@pytest.mark.parametrize("excluded", ["node_modules", ".venv", "dist", "vendor"])
def test_an_excluded_late_repository_is_never_discovered(managed: Managed, excluded: str) -> None:
    make_repo(managed.root / excluded / "pkg" / "repo", {"x.py": "X = 1\n"})

    assert managed.status().new_repositories == ()
    assert managed.sync().repositories_added == ()
    assert managed.repos() == ["service-a"]


def test_a_late_symlink_out_of_the_root_is_not_a_route_in(managed: Managed, tmp_path: Path) -> None:
    """Containment does not weaken because discovery now runs more often."""
    outside = make_repo(tmp_path / "outside" / "foreign", {"f.py": "FOREIGN = 1\n"})
    (managed.root / "link").symlink_to(outside, target_is_directory=True)

    assert managed.status().new_repositories == ()
    assert managed.sync().repositories_added == ()
    assert managed.repos() == ["service-a"]


# --- 13. unreadable ------------------------------------------------------------


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read a mode-000 directory")
def test_an_unreadable_subtree_is_reported_rather_than_assumed_empty(
    managed: Managed,
) -> None:
    """ "We found nothing" and "we were refused" are different claims (V1_SPEC §5)."""
    locked = managed.root / "locked"
    make_repo(locked / "hidden", {"h.py": "HIDDEN = 1\n"})
    locked.chmod(0o000)
    try:
        report = managed.status()
        assert [p.name for p in report.unreadable_paths] == ["locked"]

        synced = managed.sync()
        assert [p.name for p in synced.unreadable_paths] == ["locked"]
        # The readable half of the tree is still synchronized.
        assert "service-a" in managed.repos()
    finally:
        locked.chmod(0o755)


# --- 14. git shapes ------------------------------------------------------------


def test_a_late_worktree_is_recognized_exactly_as_init_would(managed: Managed) -> None:
    """Whatever init discovery recognizes, sync discovery recognizes."""
    source = make_repo(managed.root / "origin", {"o.py": "ORIGIN = 1\n"})
    managed.sync()

    tree = managed.root / "extern" / "wt"
    tree.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "-q", "--detach", str(tree)],
        cwd=source,
        check=True,
        capture_output=True,
    )
    assert (tree / ".git").is_file(), "a worktree presents .git as a file"

    report = managed.sync()
    assert "extern/wt" in report.repositories_added
    assert "extern/wt" in managed.repos()


# --- 15. the root itself becomes a repository ----------------------------------


def test_a_root_that_becomes_a_repository_still_reconciles(managed: Managed) -> None:
    """Global scope becomes unreachable here; reconciliation must not go with it.

    Being at a root that is itself managed resolves to *that repository* rather
    than to global scope — the more specific answer, and deliberate
    (ARCHITECTURE.md §7). If only global scope reconciled, running ``git init`` on
    a workspace directory would silently restore the defect for good.
    """
    init_git_repository(managed.root)
    managed.sync()
    assert "." in managed.repos()

    context = managed.context(managed.root)
    assert context.scope is ContextScope.REPOSITORY, "the root resolves to itself"

    make_repo(managed.root / "service-c", {"c.py": "GAMMA_MARKER_918273 = 1\n"})
    report = managed.sync()

    assert report.repositories_added == ("service-c",)
    assert "service-c" in managed.repos()


def test_a_repository_scoped_sync_does_not_register_other_repositories(
    managed: Managed,
) -> None:
    """A scoped run never touches another row (ARCHITECTURE.md §9).

    Registering there would create a repository outside the requested scope and
    then not index it — registered, with no facts, and counted as managed.
    """
    make_repo(managed.root / "service-b", BETA)

    report = managed.sync(where=managed.root / "service-a")

    assert report.repositories_added == ()
    assert managed.repos() == ["service-a"]
    # Detection is not narrowed, only reconciliation: the user is still told.
    assert managed.status(where=managed.root / "service-a").new_repositories == ("service-b",)


# --- 16-17. concurrency --------------------------------------------------------


def _cli_env(state: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(state),
        "REPOATLAS_CONFIG": str(state / "config.yaml"),
        "REPOATLAS_DB": str(state / "atlas.db"),
    }


def _run(env: dict[str, str], cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(CLI), *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=300
    )


@pytest.fixture
def cli_managed(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A managed root initialized through the installed console script."""
    root = tmp_path / "root"
    root.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    make_repo(root / "service-a", {"a.py": "ALPHA_MARKER_918273 = 1\n"})

    env = _cli_env(state)
    done = _run(env, root, "init", str(root))
    assert done.returncode == ExitCode.OK, done.stderr
    return root, env


@pytest.mark.skipif(not CLI.exists(), reason="the repoatlas console script is not installed")
def test_six_concurrent_syncs_register_one_repository(
    cli_managed: tuple[Path, dict[str, str]],
) -> None:
    """Real processes. Registration is inside the writer lock, so this is a race."""
    root, env = cli_managed
    make_repo(root / "service-b", BETA)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: _run(env, root, "sync"), range(6)))

    for done in results:
        assert done.returncode in (ExitCode.OK, ExitCode.SYNC_LOCKED), done.stderr
        combined = done.stdout + done.stderr
        assert "UNIQUE constraint" not in combined, combined
        assert "Traceback" not in combined, combined

    assert any(done.returncode == ExitCode.OK for done in results), "no sync succeeded"

    listing = _run(env, root, "repos")
    assert listing.stdout.count("service-b") == 1, listing.stdout

    final = _run(env, root, "status", "--json")
    payload = json.loads(final.stdout)
    assert payload["index_state"] == "current"
    assert payload["new_repositories"] == []
    assert payload["repository_count"] == 2


@pytest.mark.skipif(not CLI.exists(), reason="the repoatlas console script is not installed")
def test_repeated_concurrent_rounds_never_duplicate_a_repository(
    cli_managed: tuple[Path, dict[str, str]],
) -> None:
    """Several rounds, because an insertion race does not fail every time."""
    root, env = cli_managed
    for round_number in range(3):
        make_repo(root / f"late-{round_number}", {"x.py": f"R{round_number} = 1\n"})
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: _run(env, root, "sync"), range(4)))

    listing = _run(env, root, "repos")
    for round_number in range(3):
        assert listing.stdout.count(f"late-{round_number}") == 1, listing.stdout

    duplicates = _run(env, root, "status", "--json")
    assert json.loads(duplicates.stdout)["repository_count"] == 4


# --- 18. the global linker -----------------------------------------------------


def test_a_new_repository_resolves_an_older_reference_in_the_same_sync(
    tmp_path: Path,
) -> None:
    """Registration is not the point; participating in the global graph is."""
    root = tmp_path / "root"
    root.mkdir()
    make_repo(
        root / "consumer",
        {
            "pyproject.toml": (
                '[project]\nname = "consumer"\nversion = "1.0"\n'
                'dependencies = ["provider-lib>=1.0"]\n'
            ),
            "app.py": "USES_PROVIDER = 1\n",
        },
    )

    managed = Managed(tmp_path, root, settings_for(tmp_path))
    managed.initialize()
    try:
        before = managed.sync()
        assert before.cross_repository_edges == 0

        make_repo(
            root / "provider",
            {
                "pyproject.toml": '[project]\nname = "provider-lib"\nversion = "1.4.0"\n',
                "lib.py": "PROVIDER = 1\n",
            },
        )

        after = managed.sync()

        assert after.repositories_added == ("provider",)
        assert after.cross_repository_edges >= 1, "the linker must run over the new repository"
    finally:
        managed.close()


# --- 20. incremental == clean --------------------------------------------------


def test_an_incremental_timeline_equals_a_clean_index_of_the_same_tree(
    tmp_path: Path,
) -> None:
    """The strongest test here: reconciliation must not be a second kind of state.

    T0 init · T1 add a repository · T2 modify one and add a nested one ·
    T3 delete one — then compare against a pristine index of the final tree.
    """
    root = tmp_path / "root"
    root.mkdir()
    make_repo(root / "service-a", {"a.py": "def alpha():\n    return 1\n"})

    settings = settings_for(tmp_path)
    managed = Managed(tmp_path, root, settings)
    managed.initialize()
    try:
        make_repo(root / "service-b", BETA)
        managed.sync()

        (root / "service-a" / "a.py").write_text(
            "def alpha():\n    return 2\n\n\ndef alpha_two():\n    return 3\n", encoding="utf-8"
        )
        make_repo(root / "service-a" / "tools" / "nested", {"n.py": "NESTED = 1\n"})
        managed.sync()

        doomed = root / "service-b"
        subprocess.run(["rm", "-rf", str(doomed)], check=True)
        managed.sync()

        incremental = fact_snapshot(managed.workspace)
    finally:
        managed.close()

    clean = reindex_from_scratch(settings, root, tmp_path, tag="clean")
    assert incremental == clean


# --- 21-22. read-only, and the lock ---------------------------------------------


def test_status_never_registers_anything(managed: Managed) -> None:
    """Detection is not reconciliation. ``status`` writes nothing (V1_SPEC §4)."""
    make_repo(managed.root / "service-b", BETA)

    for _ in range(3):
        assert managed.status().new_repositories == ("service-b",)

    assert managed.repos() == ["service-a"]


def test_status_does_not_need_the_writer_lock(managed: Managed) -> None:
    """Drift detection is a filesystem walk, not a mutation."""
    with managed.workspace.writer_lock():
        make_repo(managed.root / "service-b", BETA)
        report = status(managed.workspace, managed.workspace.resolver().resolve(managed.root))

    assert report.new_repositories == ("service-b",)


@pytest.mark.skipif(not CLI.exists(), reason="the repoatlas console script is not installed")
def test_sync_still_refuses_when_the_writer_lock_is_held(
    cli_managed: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    """Exit 5 semantics are unchanged, and nothing is registered before the lock."""
    root, env = cli_managed
    make_repo(root / "service-b", BETA)

    settings = Settings.model_validate(
        {
            "storage": {"database": env["REPOATLAS_DB"], "lock_timeout_seconds": 0.0},
        }
    )
    holder = open_workspace(settings=settings, require_root=False)
    try:
        with holder.writer_lock():
            blocked = _run({**env, "REPOATLAS_SYNC_LOCK_TIMEOUT": "0"}, root, "sync")
    finally:
        holder.close()

    assert blocked.returncode == ExitCode.SYNC_LOCKED, blocked.stdout + blocked.stderr
    listing = _run(env, root, "repos")
    assert "service-b" not in listing.stdout, "a refused sync must register nothing"


# --- 23. context before and after -----------------------------------------------


def test_an_unregistered_repository_is_not_a_context_until_it_is_synced(
    managed: Managed,
) -> None:
    """A read command never quietly indexes. It reports, and ``sync`` reconciles."""
    from repoatlas.errors import ContextResolutionError

    late = make_repo(managed.root / "service-b", BETA)

    with pytest.raises(ContextResolutionError):
        managed.context(late)

    managed.sync()

    resolved = managed.context(late)
    assert resolved.primary is not None
    assert resolved.primary.slug == "service-b"


# --- 24-25. CLI surfaces --------------------------------------------------------


@pytest.mark.skipif(not CLI.exists(), reason="the repoatlas console script is not installed")
def test_the_cli_reproduction_from_the_review(
    cli_managed: tuple[Path, dict[str, str]],
) -> None:
    """Exactly the sequence the review reported, end to end."""
    root, env = cli_managed

    before = _run(env, root, "status")
    assert "Index state: current" in before.stdout

    make_repo(root / "service-b", BETA)

    stale = _run(env, root, "status")
    assert "stale" in stale.stdout
    assert "service-b" in stale.stdout
    assert "repoatlas sync" in stale.stdout

    synced = _run(env, root, "sync")
    assert synced.returncode == ExitCode.OK, synced.stderr
    assert "Discovered 1 new repository:" in synced.stdout
    assert "service-b" in synced.stdout

    listing = _run(env, root, "repos")
    assert "service-b" in listing.stdout

    after = _run(env, root, "status")
    assert "Index state: current" in after.stdout

    found = _run(env, root, "locate", "beta_marker_918273")
    assert found.returncode == ExitCode.OK, found.stderr
    assert "service-b/b.py" in found.stdout


@pytest.mark.skipif(not CLI.exists(), reason="the repoatlas console script is not installed")
def test_json_output_exposes_the_repository_set_fields(
    cli_managed: tuple[Path, dict[str, str]],
) -> None:
    """Machine-readable, so nothing has to parse English (V1_SPEC §16)."""
    root, env = cli_managed
    make_repo(root / "service-b", BETA)

    drift = json.loads(_run(env, root, "status", "--json").stdout)
    assert drift["new_repositories"] == ["service-b"]
    assert drift["missing_repositories"] == []
    assert drift["unreadable_paths"] == []
    assert drift["index_state"] == "stale"

    synced = json.loads(_run(env, root, "sync", "--json").stdout)
    assert synced["repositories_added"] == ["service-b"]

    settled = json.loads(_run(env, root, "status", "--json").stdout)
    assert settled["new_repositories"] == []
    assert settled["index_state"] == "current"


# --- 26-28. removal, determinism, and the state guard ---------------------------


def test_a_deleted_repository_is_retired_and_named(managed: Managed) -> None:
    """The mirror operation, and it was already sync's behaviour — now it is reported."""
    make_repo(managed.root / "service-b", BETA)
    managed.sync()

    subprocess.run(["rm", "-rf", str(managed.root / "service-b")], check=True)

    drift = managed.status()
    assert drift.missing_repositories == ("service-b",)
    assert drift.index_state == "stale"

    managed.sync()
    assert managed.repos() == ["service-a"]
    assert managed.status().index_state == "current"


def test_a_repository_that_stops_being_git_stays_registered(managed: Managed) -> None:
    """Pinned, not chosen here: init and sync already agree, and they agree on this.

    A repository's path is its identity, and that path still exists — so neither
    command retires it. It is deliberately absent from ``missing_repositories``,
    which names only what the next sync will actually retire, so the report and the
    behaviour cannot drift apart.
    """
    make_repo(managed.root / "service-b", BETA)
    managed.sync()

    subprocess.run(["rm", "-rf", str(managed.root / "service-b" / ".git")], check=True)

    drift = managed.status()
    assert drift.missing_repositories == ()
    assert managed.sync().repositories_added == ()
    assert "service-b" in managed.repos()


def test_reconciliation_is_deterministic(managed: Managed) -> None:
    """Repeated planning over an unchanged tree gives an identical answer."""
    make_repo(managed.root / "service-b", BETA)
    make_repo(managed.root / "deep" / "a" / "b" / "service-c", {"c.py": "C = 1\n"})

    plans = [
        plan_repository_set(
            managed.root,
            managed.workspace.settings.discovery,
            managed.workspace.repositories.list_all(),
        )
        for _ in range(5)
    ]
    signatures = {
        (
            tuple(c.slug for c in plan.discovered),
            tuple(c.slug for c in plan.added),
            tuple(r.slug for r in plan.missing),
        )
        for plan in plans
    }
    assert len(signatures) == 1
    assert next(iter(signatures))[1] == ("deep/a/b/service-c", "service-b")


def test_planning_writes_nothing_and_registering_is_the_only_writer(
    managed: Managed,
) -> None:
    """The split that lets ``status`` share ``sync``'s discovery authority."""
    make_repo(managed.root / "service-b", BETA)
    registered = managed.workspace.repositories.list_all()

    plan = plan_repository_set(managed.root, managed.workspace.settings.discovery, registered)
    assert plan.drifted
    assert managed.workspace.repositories.list_all() == registered

    with managed.workspace.writer_lock():
        created = register(plan.added, managed.workspace.repositories)

    assert [repo.slug for repo in created] == ["service-b"]
    assert len(managed.workspace.repositories.list_all()) == 2


def test_no_test_touches_real_repoatlas_state() -> None:
    """Two earlier remediation passes found tests writing into real state."""
    from repoatlas.config.settings import config_path, database_path

    for path in (config_path(), database_path()):
        assert "pytest" in str(path) or "tmp" in str(path) or not path.exists(), path
