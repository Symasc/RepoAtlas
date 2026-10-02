"""Repository discovery (V1_SPEC §5, §19)."""

from __future__ import annotations

from pathlib import Path

from repoatlas.config.settings import DiscoverySettings
from repoatlas.discovery.finder import discover_repositories
from repoatlas.discovery.ignore import IgnoreRules, is_secret_path
from repoatlas.discovery.languages import detect_language, is_lockfile, is_manifest


def test_discovers_all_managed_repositories(workspace_root: Path) -> None:
    found = discover_repositories(workspace_root)
    names = {c.name for c in found}
    assert {"python-service", "deployment", "scripts", "ts-service"} <= names


def test_discovers_nested_repository_as_separate(workspace_root: Path) -> None:
    """A repository inside another is a separate managed repository."""
    found = discover_repositories(workspace_root)
    paths = {c.path for c in found}
    assert workspace_root / "nested-outer" in paths
    assert workspace_root / "nested-outer" / "tools" / "nested-inner" in paths


def test_single_repository_root(tmp_path: Path) -> None:
    from tests.conftest import init_git_repository

    repo = tmp_path / "solo"
    repo.mkdir()
    (repo / "main.py").write_text("x = 1\n")
    init_git_repository(repo)

    found = discover_repositories(tmp_path)
    assert [c.name for c in found] == ["solo"]


def test_no_duplicate_repositories(workspace_root: Path) -> None:
    found = discover_repositories(workspace_root)
    assert len(found) == len({c.path for c in found})


def test_excluded_directories_are_not_descended(tmp_path: Path) -> None:
    from tests.conftest import init_git_repository

    hidden = tmp_path / "node_modules" / "pkg"
    hidden.mkdir(parents=True)
    (hidden / "index.js").write_text("module.exports = {};\n")
    init_git_repository(hidden)

    assert discover_repositories(tmp_path) == []


def test_hidden_directories_are_not_searched_for_repositories(tmp_path: Path) -> None:
    """In a home directory, tools installed as Git clones (`~/.oh-my-zsh`) are not projects."""
    from tests.conftest import init_git_repository

    tool = tmp_path / ".oh-my-zsh"
    tool.mkdir()
    (tool / "oh-my-zsh.sh").write_text("echo hi\n")
    init_git_repository(tool)
    project = tmp_path / "projects" / "app"
    project.mkdir(parents=True)
    (project / "main.py").write_text("x = 1\n")
    init_git_repository(project)

    assert [c.path for c in discover_repositories(tmp_path)] == [project]


def test_virtual_environments_are_not_walked_whatever_their_name(tmp_path: Path) -> None:
    """`pip install -e` clones into an environment's `src/`: a dependency, not a project."""
    from tests.conftest import init_git_repository

    environment = tmp_path / "app" / "py38env"
    dependency = environment / "src" / "dependency"
    dependency.mkdir(parents=True)
    (environment / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (dependency / "setup.py").write_text("x = 1\n")
    init_git_repository(dependency)

    assert discover_repositories(tmp_path) == []


def test_a_repository_holding_pyvenv_cfg_is_still_a_repository(tmp_path: Path) -> None:
    from tests.conftest import init_git_repository

    repo = tmp_path / "made-in-place"
    repo.mkdir()
    (repo / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (repo / "main.py").write_text("x = 1\n")
    init_git_repository(repo)

    assert [c.path for c in discover_repositories(tmp_path)] == [repo]


def test_depth_does_not_bound_the_walk(tmp_path: Path) -> None:
    """The walk had a ``max_depth`` of 6 and this test asserted it.

    That constant made an ``init`` that exited 0 report a repository set it knew
    was incomplete, so the setting is gone rather than raised and the test now
    asserts the opposite: nesting depth is not a reason to omit a repository
    (V1_SPEC §5). The deep case lives in
    ``tests/adversarial/test_recursive_repository_discovery.py``.
    """
    from tests.conftest import init_git_repository

    deep = tmp_path / "a" / "b" / "c" / "d" / "e" / "f" / "g" / "h" / "repo"
    deep.mkdir(parents=True)
    (deep / "f.py").write_text("y = 2\n")
    init_git_repository(deep)

    assert [c.path for c in discover_repositories(tmp_path)] == [deep]
    assert not hasattr(DiscoverySettings(), "max_depth")


def test_repoatlasignore_directory_pattern() -> None:
    rules = IgnoreRules.from_text("generated/\n*.log\n")
    assert rules.matches("generated", is_dir=True)
    assert rules.matches("generated/out.py")
    assert rules.matches("logs/app.log")
    assert not rules.matches("src/config.py")


def test_repoatlasignore_negation() -> None:
    rules = IgnoreRules.from_text("*.py\n!keep.py\n")
    assert rules.matches("src/skip.py")
    assert not rules.matches("src/keep.py")


def test_repoatlasignore_anchored_pattern() -> None:
    rules = IgnoreRules.from_text("/build\n")
    assert rules.matches("build")
    assert rules.matches("build/out.js")
    assert not rules.matches("src/build")


def test_secret_patterns_recognized() -> None:
    for path in (".env", ".env.production", "certs/server.pem", "id_rsa", "deploy.key"):
        assert is_secret_path(path), path
    assert not is_secret_path("src/config.py")


def test_language_detection() -> None:
    assert detect_language("src/config.py") == "Python"
    assert detect_language("src/index.ts") == "TypeScript"
    assert detect_language("app.js") == "JavaScript"
    assert detect_language("deploy.sh") == "Bash"
    assert detect_language("lib/thing.rb") == "Ruby"
    assert detect_language("service/init.sls") == "Salt"
    assert detect_language("tpl/conf.j2") == "Jinja"
    assert detect_language("compose.yml") == "YAML"
    assert detect_language("package.json") == "JSON"
    assert detect_language("Dockerfile") == "Dockerfile"
    assert detect_language("Dockerfile.prod") == "Dockerfile"
    assert detect_language("binary.bin") is None


def test_manifest_and_lockfile_detection() -> None:
    assert is_manifest("pyproject.toml")
    assert is_manifest("requirements-dev.txt")
    assert is_manifest("package.json")
    assert is_lockfile("uv.lock")
    assert is_lockfile("pnpm-lock.yaml")
    assert not is_lockfile("package.json")
