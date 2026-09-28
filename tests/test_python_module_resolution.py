"""Python module identity inside one project (repoatlas.graph.py_modules).

Built from file rows and project facts alone, as the global pass builds it: each case
states which directory is a source root and which is not, and what a dotted name then
names.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from repoatlas.graph.modules import ModuleTarget
from repoatlas.graph.py_modules import PythonModules
from repoatlas.graph.resolution import ResolutionIndex
from repoatlas.models.parsing import EdgeType, EntityType
from repoatlas.parsers.module_facts import MODULE_RESOLUTION, PYTHON_PROJECT
from repoatlas.storage.protocols import FileRow, UnresolvedRefRecord


class Tree:
    """One repository's indexed files and the project facts its pyprojects record."""

    def __init__(self, files: list[str], projects: dict[str, dict[str, Any]] | None = None):
        paths: set[str] = {*files, *(projects or {}).keys()}
        self.rows = {
            path: FileRow(
                file_id=index + 1,
                file_entity_id=1000 + index,
                repo_id=1,
                repo_name="demo",
                repo_slug="demo",
                repo_path=Path("/nowhere/demo"),
                rel_path=path,
            )
            for index, path in enumerate(sorted(paths))
        }
        facts = [
            UnresolvedRefRecord(
                repo_id=1,
                file_id=self.rows[path].file_id,
                src_id=self.rows[path].file_entity_id,
                edge_type=EdgeType.CONFIGURES,
                target_kind=EntityType.CONFIG,
                target_key=f"module-resolution:{PYTHON_PROJECT}",
                meta_json=json.dumps(
                    {MODULE_RESOLUTION: PYTHON_PROJECT, "facts": json.dumps(stated)}
                ),
            )
            for path, stated in (projects or {}).items()
        ]
        self.modules = PythonModules(ResolutionIndex.build(list(self.rows.values()), []), facts)

    def resolve(self, origin: str, spec: str) -> str:
        found = self.modules.resolve(self.rows[origin], spec)
        return found.file.rel_path if isinstance(found, ModuleTarget) else f"<{found}>"


def test_p6_src_layout_roots_at_src_and_src_is_no_package() -> None:
    tree = Tree(
        ["src/fixture_pkg/__init__.py", "src/fixture_pkg/models.py", "tests/test_models.py"],
        {"pyproject.toml": {"name": "fixture"}},
    )
    assert tree.resolve("tests/test_models.py", "fixture_pkg.models") == (
        "src/fixture_pkg/models.py"
    )
    assert tree.resolve("tests/test_models.py", "src.fixture_pkg.models") == "<module-not-found>"


def test_p7_flat_layout_roots_at_the_project_and_scripts_are_not_top_level() -> None:
    tree = Tree(
        ["fixture_pkg/__init__.py", "fixture_pkg/models.py", "scripts/bar.py", "app.py"],
        {"pyproject.toml": {"name": "fixture"}},
    )
    assert tree.resolve("app.py", "fixture_pkg.models") == "fixture_pkg/models.py"
    assert tree.resolve("app.py", "bar") == "<module-not-found>"
    assert tree.resolve("app.py", "scripts.bar") == "<namespace-package>"


def test_hatch_packages_name_the_root_and_the_only_top_level_names() -> None:
    tree = Tree(
        ["src/demo/__init__.py", "src/demo/core.py", "src/other/__init__.py", "src/demo/x.py"],
        {"pyproject.toml": {"name": "demo", "packages": ["src/demo"]}},
    )
    assert tree.resolve("src/demo/x.py", "demo.core") == "src/demo/core.py"
    assert tree.resolve("src/demo/x.py", "other") == "<module-not-found>"


def test_a_layout_this_does_not_read_is_refused_not_guessed() -> None:
    tree = Tree(
        ["lib/demo/__init__.py", "src/demo/__init__.py", "demo_user.py"],
        {"pyproject.toml": {"name": "demo", "unread": ["tool.setuptools.package-dir"]}},
    )
    assert tree.resolve("demo_user.py", "demo") == "<project-layout-unread>"


def test_a_regular_package_wins_over_a_module_of_the_same_name() -> None:
    tree = Tree(["pkg/__init__.py", "pkg.py", "pkg/models.py", "main.py"])
    assert tree.resolve("main.py", "pkg") == "pkg/__init__.py"
    assert tree.resolve("main.py", "pkg.models") == "pkg/models.py"


def test_a_stub_answers_only_where_no_source_does() -> None:
    tree = Tree(["typed.pyi", "both.py", "both.pyi", "main.py"])
    assert tree.resolve("main.py", "typed") == "typed.pyi"
    assert tree.resolve("main.py", "both") == "both.py"


def test_a_namespace_directory_is_refused_not_treated_as_a_package() -> None:
    tree = Tree(["ns/inner/__init__.py", "ns/inner/mod.py", "main.py"])
    assert tree.resolve("main.py", "ns.inner.mod") == "<namespace-package>"


def test_a_repository_without_a_project_is_one_rooted_at_its_root() -> None:
    tree = Tree(["app/__init__.py", "app/models.py", "manage.py"])
    assert tree.resolve("manage.py", "app.models") == "app/models.py"


def test_relative_imports_climb_the_package_chain_and_no_further() -> None:
    tree = Tree(
        [
            "pkg/__init__.py",
            "pkg/models.py",
            "pkg/sub/__init__.py",
            "pkg/sub/mod.py",
            "pkg/common.py",
        ],
        {"pyproject.toml": {"name": "p"}},
    )
    assert tree.resolve("pkg/sub/mod.py", ".") == "pkg/sub/__init__.py"
    assert tree.resolve("pkg/sub/mod.py", "..common") == "pkg/common.py"
    assert tree.resolve("pkg/sub/__init__.py", "..models") == "pkg/models.py"
    assert tree.resolve("pkg/sub/mod.py", "...other") == "<relative-beyond-package>"
    assert tree.resolve("pkg/models.py", ".sub.mod") == "pkg/sub/mod.py"


def test_a_relative_import_outside_any_package_names_nothing() -> None:
    tree = Tree(["loose/a.py", "loose/b.py"])
    assert tree.resolve("loose/a.py", ".b") == "<relative-beyond-package>"


def test_p12_a_project_is_a_boundary_inside_one_repository() -> None:
    """Git co-location is no evidence: beta cannot import alpha without a declaration."""
    tree = Tree(
        [
            "alpha/src/alpha/__init__.py",
            "alpha/src/alpha/models.py",
            "alpha/src/app/__init__.py",
            "beta/src/beta/__init__.py",
            "beta/src/beta/service.py",
            "beta/src/app/__init__.py",
        ],
        {"alpha/pyproject.toml": {"name": "alpha"}, "beta/pyproject.toml": {"name": "beta"}},
    )
    assert tree.resolve("beta/src/beta/service.py", "app") == "beta/src/app/__init__.py"
    assert tree.resolve("alpha/src/alpha/models.py", "app") == "alpha/src/app/__init__.py"
    assert tree.resolve("beta/src/beta/service.py", "alpha.models") == "<project-boundary>"


def test_a_nested_project_is_unreachable_from_the_flat_project_around_it() -> None:
    tree = Tree(
        ["outer/__init__.py", "outer/use.py", "plugins/inner/inner/__init__.py"],
        {"pyproject.toml": {"name": "outer"}, "plugins/inner/pyproject.toml": {"name": "inner"}},
    )
    assert tree.resolve("outer/use.py", "plugins.inner.inner") == "<namespace-package>"
    assert tree.resolve("outer/use.py", "inner") == "<project-boundary>"


def test_a_file_outside_every_project_has_no_roots() -> None:
    tree = Tree(
        ["lib/demo/__init__.py", "scripts/run.py"],
        {"lib/pyproject.toml": {"name": "demo"}},
    )
    assert tree.resolve("scripts/run.py", "demo") == "<no-project>"


def test_a_script_beside_a_module_of_the_same_name_is_refused() -> None:
    tree = Tree(
        ["helpers/__init__.py", "tools/helpers.py", "tools/run.py"],
        {"pyproject.toml": {"name": "p"}},
    )
    assert tree.resolve("tools/run.py", "helpers") == "<script-shadow>"


def test_a_submodule_is_found_only_under_a_regular_package() -> None:
    tree = Tree(["pkg/__init__.py", "pkg/helper.py", "plain.py"])
    rows = tree.rows
    assert tree.modules.submodule(rows["pkg/__init__.py"], "helper") == rows["pkg/helper.py"]
    assert tree.modules.submodule(rows["plain.py"], "helper") is None
