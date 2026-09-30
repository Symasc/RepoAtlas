"""The module-resolution fact a ``pyproject.toml`` records (repoatlas.parsers.python_project)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
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
    assert facts('[project]\nname = "Internal-Auth"\nversion = "1"\n') == {
        "name": "Internal-Auth",
        "dependencies": "static",
    }


def test_hatch_wheel_packages_are_the_layout_read() -> None:
    content = (
        '[project]\nname = "demo"\n'
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
        '[tool.hatch.build.targets.wheel]\npackages = ["src/demo"]\n'
    )
    assert facts(content) == {
        "name": "demo",
        "dependencies": "static",
        "backend": "hatchling.build",
        "packages": ["src/demo"],
    }


SETUPTOOLS_BACKEND = '[build-system]\nbuild-backend = "setuptools.build_meta"\n'
HATCH_BACKEND = '[build-system]\nbuild-backend = "hatchling.build"\n'


def test_setuptools_find_is_read_normalised_as_a_setup_cfg_find() -> None:
    """P6 C1: no backend named is setuptools; package-dir must root where `where` does."""
    content = (
        '[project]\nname = "demo"\n'
        '[tool.setuptools]\npackage-dir = {"" = "lib"}\n'
        '[tool.setuptools.packages.find]\nwhere = ["lib"]\n'
    )
    assert facts(content) == {
        "name": "demo",
        "dependencies": "static",
        "setuptools_layout": {
            "root": "lib",
            "packages": "find_namespace",
            "include": [],
            "exclude": [],
        },
    }


def test_setuptools_find_patterns_and_namespaces_as_written() -> None:
    content = (
        SETUPTOOLS_BACKEND + '[tool.setuptools.packages.find]\nwhere = ["."]\n'
        'include = ["demo*"]\nexclude = ["demo.tests*"]\nnamespaces = false\n'
    )
    assert facts(content) == {
        "backend": "setuptools.build_meta",
        "setuptools_layout": {
            "root": "",
            "packages": "find",
            "include": ["demo*"],
            "exclude": ["demo.tests*"],
        },
    }


@pytest.mark.parametrize(
    ("layout", "unread"),
    [
        # two where, a packages list, py-modules beside find, a package-dir rooting elsewhere
        ('[tool.setuptools.packages.find]\nwhere = ["src", "lib"]\n', ["tool.setuptools.packages"]),
        ('[tool.setuptools]\npackages = ["demo"]\n', ["tool.setuptools.packages"]),
        (
            '[tool.setuptools]\npy-modules = ["tool"]\n[tool.setuptools.packages.find]\n',
            ["tool.setuptools.packages", "tool.setuptools.py-modules"],
        ),
        (
            '[tool.setuptools]\npackage-dir = {"" = "lib"}\n[tool.setuptools.packages.find]\n'
            'where = ["src"]\n',
            ["tool.setuptools.package-dir", "tool.setuptools.packages"],
        ),
        (
            '[tool.setuptools.packages.find]\nwhere = ["../elsewhere"]\n',
            ["tool.setuptools.packages"],
        ),
        ('[tool.setuptools.packages.find]\nnamespaces = "yes"\n', ["tool.setuptools.packages"]),
    ],
)
def test_a_setuptools_layout_in_another_shape_is_named_not_guessed(
    layout: str, unread: list[str]
) -> None:
    assert facts(SETUPTOOLS_BACKEND + layout) == {
        "backend": "setuptools.build_meta",
        "unread": unread,
    }


def test_setuptools_keys_under_another_backend_are_not_its_layout() -> None:
    content = HATCH_BACKEND + '[tool.setuptools.packages.find]\nwhere = ["src"]\n'
    assert facts(content) == {"backend": "hatchling.build", "unread": ["tool.setuptools.packages"]}


def test_hatch_only_include_names_the_top_level_packages_shipped() -> None:
    """requests-cache's form: the build's only-include, a README force-included."""
    content = (
        HATCH_BACKEND + '[tool.hatch.build]\nonly-include = ["demo", "./other/"]\n'
        'force-include = {"README.md" = "demo/README.md"}\n'
        '[tool.hatch.build.targets.sdist]\nonly-include = ["demo", "tests"]\n'
    )
    assert facts(content) == {"backend": "hatchling.build", "only_include": ["demo", "other"]}


def test_the_wheel_target_s_only_include_overrides_the_build_s() -> None:
    content = (
        HATCH_BACKEND + '[tool.hatch.build]\nonly-include = ["build_level"]\n'
        '[tool.hatch.build.targets.wheel]\nonly-include = ["wheel_level"]\n'
    )
    assert facts(content) == {"backend": "hatchling.build", "only_include": ["wheel_level"]}


@pytest.mark.parametrize(
    ("build", "unread"),
    [
        ('only-include = ["src/demo"]\n', ["tool.hatch.build.only-include"]),  # a nested path
        ('only-include = ["demo.py"]\n', ["tool.hatch.build.only-include"]),  # a file
        ('only-include = ["demo"]\nexclude = ["demo/x.py"]\n', ["tool.hatch.build.only-include"]),
        (
            'only-include = ["demo"]\nforce-include = {"x.py" = "demo/x.py"}\n',
            ["tool.hatch.build.only-include"],
        ),
        (
            'only-include = ["demo"]\nforce-include = {"shared" = "demo/shared"}\n',
            ["tool.hatch.build.only-include"],
        ),
        (
            'only-include = ["demo"]\nsources = ["src"]\n',
            ["tool.hatch.build.sources", "tool.hatch.build.only-include"],
        ),
    ],
)
def test_an_only_include_in_another_shape_is_named_not_guessed(
    build: str, unread: list[str]
) -> None:
    content = HATCH_BACKEND + "[tool.hatch.build]\n" + build
    assert facts(content) == {"backend": "hatchling.build", "unread": unread}


def test_only_include_under_another_backend_or_beside_packages_is_not_read() -> None:
    setuptools = SETUPTOOLS_BACKEND + '[tool.hatch.build]\nonly-include = ["demo"]\n'
    assert facts(setuptools) == {
        "backend": "setuptools.build_meta",
        "unread": ["tool.hatch.build.only-include"],
    }
    packages = (
        HATCH_BACKEND + '[tool.hatch.build.targets.wheel]\npackages = ["src/demo"]\n'
        'only-include = ["demo"]\n'
    )
    assert facts(packages) == {
        "backend": "hatchling.build",
        "packages": ["src/demo"],
        "unread": ["tool.hatch.build.targets.wheel.only-include"],
    }


def test_a_malformed_hatch_packages_list_is_unread() -> None:
    content = '[project]\nname = "d"\n[tool.hatch.build.targets.wheel]\npackages = "src/d"\n'
    assert facts(content) == {
        "name": "d",
        "dependencies": "static",
        "unread": ["tool.hatch.build.targets.wheel.packages"],
    }


def test_poetry_names_the_project_and_every_name_it_declares() -> None:
    content = (
        '[tool.poetry]\nname = "svc"\npackages = [{include = "svc", from = "src"}]\n'
        "[tool.poetry.dependencies]\n"
        'python = "^3.12"\n'
        'Internal_Auth = {path = "../auth", develop = true}\n'
        'requests = "^2"\n'
        '[tool.poetry.group.dev.dependencies]\nhelpers = {path = "../helpers"}\n'
    )
    assert facts(content) == {
        "name": "svc",
        "unread": ["tool.poetry.packages"],
        "poetry_dependencies": ["Internal_Auth", "helpers", "requests"],
    }


def test_flit_module_is_the_layout_read_and_a_malformed_one_is_unread() -> None:
    content = (
        '[project]\nname = "Fixture-Dist"\n'
        '[build-system]\nrequires = ["flit_core"]\nbuild-backend = "flit_core.buildapi"\n'
        '[tool.flit.module]\nname = "fixture_pkg"\n'
    )
    assert facts(content) == {
        "name": "Fixture-Dist",
        "dependencies": "static",
        "backend": "flit_core.buildapi",
        "flit_module": "fixture_pkg",
    }
    assert facts('[project]\nname = "d"\n[tool.flit.module]\nname = 3\n') == {
        "name": "d",
        "dependencies": "static",
        "unread": ["tool.flit.module.name"],
    }


def test_a_workspace_root_with_no_project_is_recorded_as_virtual() -> None:
    content = '[tool.uv.workspace]\nmembers = ["packages/*"]\n'
    assert facts(content) == {
        "virtual": True,
        "uv_workspace": {"members": ["packages/*"], "exclude": []},
    }


def test_uv_sources_and_workspace_are_recorded_by_distribution() -> None:
    content = (
        '[project]\nname = "app"\n'
        "[tool.uv.sources]\n"
        'Internal_Auth = { path = "../auth", editable = true }\n'
        "shared = { workspace = true }\n"
        'remote = { git = "https://example.invalid/r" }\n'
        'split = [{ path = "../a", marker = "sys_platform == \'linux\'" }]\n'
        'guarded = { path = "../g", marker = "sys_platform == \'linux\'" }\n'
        'other_ws = { workspace = "../elsewhere" }\n'
        '[tool.uv.workspace]\nmembers = ["packages/*"]\nexclude = ["packages/old"]\n'
    )
    assert facts(content) == {
        "name": "app",
        "dependencies": "static",
        "uv_sources": {
            "Internal_Auth": {"path": "../auth", "editable": True},
            "shared": {"workspace": True},
            "remote": {"other": "git"},
            "split": {"other": "multiple"},
            "guarded": {"other": "marker"},
            "other_ws": {"other": "workspace-path"},
        },
        "uv_workspace": {"members": ["packages/*"], "exclude": ["packages/old"]},
    }


def test_a_build_system_alone_makes_a_project() -> None:
    assert facts('[build-system]\nrequires = ["setuptools"]\n') == {}


def test_whether_project_states_its_dependencies() -> None:
    """P3c: a [project] table owns its dependencies unless `dynamic` names them; then a
    setup.py or setup.cfg may state them, or [tool.setuptools.dynamic] reads them from a
    file, which is not read. Without [project] nothing is recorded."""
    project = '[project]\nname = "d"\n'
    assert facts(project)["dependencies"] == "static"  # type: ignore[index]
    assert facts(project + 'dependencies = ["x"]\n')["dependencies"] == "static"  # type: ignore[index]
    dynamic = project + 'dynamic = ["version", "dependencies"]\n'
    assert facts(dynamic)["dependencies"] == "dynamic"  # type: ignore[index]
    files = dynamic + '[tool.setuptools.dynamic]\ndependencies = {file = ["r.txt"]}\n'
    assert facts(files)["dependencies"] == "unread"  # type: ignore[index]
    assert "dependencies" not in facts('[build-system]\nrequires = ["setuptools"]\n')  # type: ignore[operator]


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
