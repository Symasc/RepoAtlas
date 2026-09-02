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


def test_max_depth_bounds_the_walk(tmp_path: Path) -> None:
    from tests.conftest import init_git_repository

    deep = tmp_path / "a" / "b" / "c" / "d" / "repo"
    deep.mkdir(parents=True)
    (deep / "f.py").write_text("y = 2\n")
    init_git_repository(deep)

    assert discover_repositories(tmp_path, DiscoverySettings(max_depth=2)) == []
    assert len(discover_repositories(tmp_path, DiscoverySettings(max_depth=6))) == 1


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
