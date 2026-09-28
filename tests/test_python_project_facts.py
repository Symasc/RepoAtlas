"""The module-resolution fact a ``pyproject.toml`` records (repoatlas.parsers.python_project)."""

from __future__ import annotations

import json
from pathlib import Path

from repoatlas.config.settings import Settings
from repoatlas.models.parsing import EntityType
from repoatlas.parsers.module_facts import MODULE_RESOLUTION, PYTHON_PROJECT
from repoatlas.parsers.python_project import PythonProjectParser
from repoatlas.parsers.registry import ParserRegistry
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build


def facts(content: str) -> dict[str, object] | None:
    parsed = PythonProjectParser().parse(Path("pyproject.toml"), content)
    assert not parsed.symbols and not parsed.diagnostics
    if not parsed.references:
        return None
    [reference] = parsed.references
    assert reference.target_kind is EntityType.CONFIG
    assert reference.target_key == f"module-resolution:{PYTHON_PROJECT}"
    assert reference.meta[MODULE_RESOLUTION] == PYTHON_PROJECT
    return json.loads(reference.meta["facts"])


def test_a_pep621_project_records_its_distribution_name_as_written() -> None:
    assert facts('[project]\nname = "Internal-Auth"\nversion = "1"\n') == {"name": "Internal-Auth"}


def test_hatch_wheel_packages_are_the_layout_read() -> None:
    content = (
        '[project]\nname = "demo"\n'
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
        '[tool.hatch.build.targets.wheel]\npackages = ["src/demo"]\n'
    )
    assert facts(content) == {
        "name": "demo",
        "backend": "hatchling.build",
        "packages": ["src/demo"],
    }


def test_a_layout_this_does_not_read_is_named_not_guessed() -> None:
    content = (
        '[project]\nname = "demo"\n'
        '[tool.setuptools]\npackage-dir = {"" = "lib"}\n'
        '[tool.setuptools.packages.find]\nwhere = ["lib"]\n'
    )
    assert facts(content) == {
        "name": "demo",
        "unread": ["tool.setuptools.package-dir", "tool.setuptools.packages"],
    }


def test_a_malformed_hatch_packages_list_is_unread() -> None:
    content = '[project]\nname = "d"\n[tool.hatch.build.targets.wheel]\npackages = "src/d"\n'
    assert facts(content) == {"name": "d", "unread": ["tool.hatch.build.targets.wheel.packages"]}


def test_poetry_names_the_project_and_its_path_dependencies() -> None:
    content = (
        '[tool.poetry]\nname = "svc"\npackages = [{include = "svc", from = "src"}]\n'
        "[tool.poetry.dependencies]\n"
        'python = "^3.12"\n'
        'Internal_Auth = {path = "../auth", develop = true}\n'
        '[tool.poetry.group.dev.dependencies]\nhelpers = {path = "../helpers"}\n'
    )
    assert facts(content) == {
        "name": "svc",
        "unread": ["tool.poetry.packages"],
        "poetry_paths": {"Internal_Auth": "../auth", "helpers": "../helpers"},
    }


def test_uv_sources_and_workspace_are_recorded_by_distribution() -> None:
    content = (
        '[project]\nname = "app"\n'
        "[tool.uv.sources]\n"
        'Internal_Auth = { path = "../auth", editable = true }\n'
        "shared = { workspace = true }\n"
        'remote = { git = "https://example.invalid/r" }\n'
        'split = [{ path = "../a", marker = "sys_platform == \'linux\'" }]\n'
        '[tool.uv.workspace]\nmembers = ["packages/*"]\nexclude = ["packages/old"]\n'
    )
    assert facts(content) == {
        "name": "app",
        "uv_sources": {
            "Internal_Auth": {"path": "../auth", "editable": True},
            "shared": {"workspace": True},
            "remote": {"other": "git"},
            "split": {"other": "multiple"},
        },
        "uv_workspace": {"members": ["packages/*"], "exclude": ["packages/old"]},
    }


def test_a_build_system_alone_makes_a_project() -> None:
    assert facts('[build-system]\nrequires = ["setuptools"]\n') == {}


def test_tool_settings_alone_are_no_project() -> None:
    assert facts("[tool.ruff]\nline-length = 100\n") is None


def test_invalid_toml_records_nothing_and_leaves_the_diagnostic_to_the_dependency_layer() -> None:
    assert facts("[project\nname = \n") is None


def test_the_registry_routes_only_pyproject_to_it() -> None:
    registry = ParserRegistry()
    assert isinstance(registry.for_path(Path("svc/pyproject.toml")), PythonProjectParser)
    assert registry.for_path(Path("Cargo.toml")) is None


def test_indexing_records_the_fact_and_nothing_a_reader_sees_otherwise(
    settings: Settings, workspace: Workspace, tmp_path: Path
) -> None:
    """One claim, no symbols, and no new language for the repository."""
    build(
        workspace,
        tmp_path,
        {"demo": {"pyproject.toml": '[project]\nname = "demo"\n', "demo/__init__.py": ""}},
    )
    claims = workspace.knowledge.list_module_facts()
    assert [json.loads(c.meta_json or "{}")[MODULE_RESOLUTION] for c in claims] == [PYTHON_PROJECT]
    entities = workspace.database.execute(
        "SELECT e.type FROM entities e JOIN files f ON f.id = e.file_id "
        "WHERE f.rel_path = 'pyproject.toml'"
    ).fetchall()
    assert [row["type"] for row in entities] == ["file"]
    languages = workspace.database.execute("SELECT languages_json FROM repositories").fetchone()[0]
    assert json.loads(languages) == ["Python"]
