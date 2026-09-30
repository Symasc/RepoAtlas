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
from repoatlas.graph.py_modules import PYTHON_ROOT, REPOSITORY_ROOT, PythonModules
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
    # P6: scripts/ is a namespace portion of the project's own root; bar.py below it is the
    # one file that name can mean, and scripts itself stays no module
    assert tree.resolve("app.py", "scripts.bar") == "scripts/bar.py"
    assert tree.resolve("app.py", "scripts") == "<namespace-package>"


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


def test_a_module_below_a_namespace_portion_of_the_project_s_own_root_resolves() -> None:
    """P6 N1b: the portion is walked through, never itself a module."""
    tree = Tree(["ns/inner/__init__.py", "ns/inner/mod.py", "main.py"])
    assert tree.resolve("main.py", "ns.inner.mod") == "ns/inner/mod.py"
    assert tree.resolve("main.py", "ns.inner") == "ns/inner/__init__.py"
    assert tree.resolve("main.py", "ns") == "<namespace-package>"
    assert tree.resolve("main.py", "ns.missing") == "<namespace-package>"


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
    # P6: the portion plugins/ is walked, and what it leads to is another project's
    assert tree.resolve("outer/use.py", "plugins.inner.inner") == "<project-boundary>"
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


# -- flit's `[tool.flit.module] name`: an explicit layout, read as flit reads it ---------

FLIT = {"name": "fixture-dist", "backend": "flit_core.buildapi"}


def test_flit_flat_package_roots_at_the_project_and_ships_only_its_module() -> None:
    tree = Tree(
        ["fixture_pkg/__init__.py", "fixture_pkg/models.py", "tasks.py", "app.py"],
        {"pyproject.toml": {**FLIT, "flit_module": "fixture_pkg"}},
    )
    assert tree.resolve("app.py", "fixture_pkg.models") == "fixture_pkg/models.py"
    assert tree.resolve("app.py", "tasks") == "<module-not-found>"


def test_flit_src_package_roots_at_src() -> None:
    tree = Tree(
        ["src/fixture_pkg/__init__.py", "src/fixture_pkg/models.py", "tests/test_models.py"],
        {"pyproject.toml": {**FLIT, "flit_module": "fixture_pkg"}},
    )
    assert tree.resolve("tests/test_models.py", "fixture_pkg.models") == (
        "src/fixture_pkg/models.py"
    )


def test_flit_module_name_not_the_distribution_name_is_the_import_name() -> None:
    tree = Tree(
        ["other_mod/__init__.py", "fixture_dist/__init__.py", "app.py"],
        {"pyproject.toml": {**FLIT, "flit_module": "other_mod"}},
    )
    assert tree.resolve("app.py", "other_mod") == "other_mod/__init__.py"
    assert tree.resolve("app.py", "fixture_dist") == "<module-not-found>"


def test_flit_module_that_is_not_indexed_refuses_the_project_without_discovery() -> None:
    tree = Tree(
        ["src/fixture_pkg/__init__.py", "src/fixture_pkg/models.py", "tests/test_models.py"],
        {"pyproject.toml": {**FLIT, "flit_module": "missing"}},
    )
    assert tree.resolve("tests/test_models.py", "fixture_pkg.models") == ("<project-layout-unread>")


def test_flit_module_in_both_the_project_and_src_is_ambiguous() -> None:
    tree = Tree(
        ["fixture_pkg/__init__.py", "src/fixture_pkg/__init__.py", "app.py"],
        {"pyproject.toml": {**FLIT, "flit_module": "fixture_pkg"}},
    )
    assert tree.resolve("app.py", "fixture_pkg") == "<module-ambiguous>"


def test_flit_dotted_module_in_a_namespace_package_is_refused() -> None:
    tree = Tree(
        ["ns/pkg/__init__.py", "ns/pkg/core.py", "app.py"],
        {"pyproject.toml": {**FLIT, "flit_module": "ns.pkg"}},
    )
    assert tree.resolve("app.py", "ns.pkg.core") == "<namespace-package>"
    regular = Tree(
        ["ns/__init__.py", "ns/pkg/__init__.py", "app.py"],
        {"pyproject.toml": {**FLIT, "flit_module": "ns.pkg"}},
    )
    assert regular.resolve("app.py", "ns.pkg") == "<project-layout-unread>"


def test_flit_package_wins_over_a_module_of_its_name_beside_it() -> None:
    tree = Tree(
        ["fixture_pkg/__init__.py", "fixture_pkg.py", "app.py"],
        {"pyproject.toml": {**FLIT, "flit_module": "fixture_pkg"}},
    )
    assert tree.resolve("app.py", "fixture_pkg") == "fixture_pkg/__init__.py"


def test_two_explicit_layouts_are_refused() -> None:
    tree = Tree(
        ["src/fixture_pkg/__init__.py", "app.py"],
        {"pyproject.toml": {**FLIT, "flit_module": "fixture_pkg", "packages": ["src/x"]}},
    )
    assert tree.resolve("app.py", "fixture_pkg") == "<project-layout-unread>"


def test_a_virtual_workspace_root_owns_no_modules() -> None:
    tree = Tree(
        ["packages/a/src/a/__init__.py", "packages/a/src/a/core.py", "scripts/run.py"],
        {
            "pyproject.toml": {"virtual": True, "uv_workspace": {"members": ["packages/*"]}},
            "packages/a/pyproject.toml": {"name": "a"},
        },
    )
    assert tree.resolve("packages/a/src/a/core.py", "a") == "packages/a/src/a/__init__.py"
    assert tree.resolve("scripts/run.py", "a") == "<no-project>"


# -- P6 N1: namespace portions (PEP 420) inside the importer's own project ----------------

FLASK = [
    "src/flask/__init__.py",
    "src/flask/app.py",
    "src/flask/config.py",
    "src/flask/sansio/app.py",
    "src/flask/sansio/scaffold.py",
]


def test_n1a_a_portion_below_a_regular_package_is_walked_through() -> None:
    """flask's shape: ``sansio/`` has no ``__init__``, and is the one ``flask.sansio``."""
    tree = Tree(FLASK, {"pyproject.toml": {"name": "flask"}})
    here = "src/flask/app.py"
    assert tree.resolve(here, ".sansio.app") == "src/flask/sansio/app.py"
    assert tree.resolve(here, "flask.sansio.scaffold") == "src/flask/sansio/scaffold.py"
    assert tree.resolve(here, "flask.sansio") == "<namespace-package>"
    assert tree.resolve(here, ".sansio.missing") == "<module-not-found>"


def test_n1a_a_portion_below_a_regular_package_is_a_level_of_the_relative_chain() -> None:
    tree = Tree(FLASK, {"pyproject.toml": {"name": "flask"}})
    here = "src/flask/sansio/app.py"
    assert tree.resolve(here, "..") == "src/flask/__init__.py"
    assert tree.resolve(here, "..config") == "src/flask/config.py"
    assert tree.resolve(here, ".scaffold") == "src/flask/sansio/scaffold.py"
    assert tree.resolve(here, ".") == "<namespace-package>"
    assert tree.resolve(here, "...config") == "<relative-beyond-package>"


def test_n1a_portions_two_deep_below_a_package() -> None:
    tree = Tree(
        ["pkg/__init__.py", "pkg/core.py", "pkg/a/b/mod.py", "pkg/a/b/other.py"],
        {"pyproject.toml": {"name": "p"}},
    )
    here = "pkg/a/b/mod.py"
    assert tree.resolve(here, ".other") == "pkg/a/b/other.py"
    assert tree.resolve(here, "...core") == "pkg/core.py"
    assert tree.resolve(here, "....core") == "<relative-beyond-package>"
    assert tree.resolve(here, "pkg.a.b.other") == "pkg/a/b/other.py"
    assert tree.resolve(here, "pkg.a") == "<namespace-package>"
    assert tree.resolve(here, "pkg.a.b") == "<namespace-package>"


def test_n1a_a_portion_with_no_regular_package_above_it_is_no_relative_level() -> None:
    """A top-level portion holds no package, and a script's directory is none either."""
    tree = Tree(
        ["ns/pkg/__init__.py", "ns/pkg/mod.py", "ns/other.py", "tools/sub/run.py", "tools/lib.py"],
        {"pyproject.toml": {"name": "p"}},
    )
    assert tree.resolve("ns/pkg/mod.py", ".") == "ns/pkg/__init__.py"
    assert tree.resolve("ns/pkg/mod.py", "..other") == "<relative-beyond-package>"
    assert tree.resolve("tools/sub/run.py", "..lib") == "<relative-beyond-package>"


def test_n1b_a_module_below_a_top_level_portion_of_the_importer_s_own_project() -> None:
    """hacs's shape: ``custom_components/`` in a repository with no packaging metadata."""
    tree = Tree(
        [
            "custom_components/hacs/__init__.py",
            "custom_components/hacs/const.py",
            "action/action.py",
            "tests/__init__.py",
            "tests/test_const.py",
        ]
    )
    here = "tests/test_const.py"
    assert tree.resolve(here, "custom_components.hacs.const") == "custom_components/hacs/const.py"
    assert tree.resolve(here, "custom_components.hacs") == "custom_components/hacs/__init__.py"
    assert tree.resolve(here, "action.action") == "action/action.py"
    assert tree.resolve(here, "custom_components") == "<namespace-package>"
    assert tree.resolve(here, "custom_components.other") == "<namespace-package>"
    found = tree.modules.resolve(tree.rows[here], "custom_components.hacs.const")
    assert isinstance(found, ModuleTarget)
    assert (found.basis, dict(found.detail)) == (
        PYTHON_ROOT,
        {
            "root": ".",
            "root_basis": REPOSITORY_ROOT,
            "project": REPOSITORY_ROOT,
            "namespace_package": "custom_components",
        },
    )


def test_n1b_a_namespace_in_two_roots_of_one_project_names_one_file_or_is_ambiguous() -> None:
    tree = Tree(
        ["lib1/ns/a.py", "lib1/ns/same.py", "lib2/ns/b.py", "lib2/ns/same.py", "app.py"],
        {"pyproject.toml": {"name": "p", "packages": ["lib1/ns", "lib2/ns"]}},
    )
    assert tree.resolve("app.py", "ns.a") == "lib1/ns/a.py"
    assert tree.resolve("app.py", "ns.b") == "lib2/ns/b.py"
    assert tree.resolve("app.py", "ns.same") == "<module-ambiguous>"
    assert tree.resolve("app.py", "ns") == "<namespace-package>"


def test_n1b_a_script_beside_what_names_the_portion_is_refused() -> None:
    """The script's own directory is searched first: a package there wins, and a portion
    there would merge a directory outside every root into the namespace."""
    tree = Tree(
        [
            "ns/pkg/mod.py",
            "main.py",
            "scripts/run.py",
            "tools/run.py",
            "tools/ns/__init__.py",
            "bin/run.py",
            "bin/ns/pkg/mod.py",
        ],
        {"pyproject.toml": {"name": "p"}},
    )
    assert tree.resolve("main.py", "ns.pkg.mod") == "ns/pkg/mod.py"
    assert tree.resolve("scripts/run.py", "ns.pkg.mod") == "ns/pkg/mod.py"
    assert tree.resolve("tools/run.py", "ns.pkg.mod") == "<script-shadow>"
    assert tree.resolve("bin/run.py", "ns.pkg.mod") == "<script-shadow>"


def test_n1b_a_portion_is_walked_only_in_the_importer_s_own_project() -> None:
    """Another project here holding the same namespace is no evidence: what only it
    holds names nothing, and never crosses the boundary."""
    tree = Tree(
        ["alpha/ns/mine.py", "alpha/app.py", "beta/ns/theirs.py"],
        {"alpha/pyproject.toml": {"name": "alpha"}, "beta/pyproject.toml": {"name": "beta"}},
    )
    assert tree.resolve("alpha/app.py", "ns.mine") == "alpha/ns/mine.py"
    assert tree.resolve("alpha/app.py", "ns.theirs") == "<namespace-package>"
