"""Python declaration-level uses, resolved by the global pass (P1-P12, P19, P21).

Each fixture repository is indexed whole and its every ``USES`` edge compared with the
full expected set, so an edge nobody expected fails as surely as a missing one. The
fixtures are in :mod:`tests.python_corpus`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build, use_edges, uses
from tests.python_corpus import FLATFIX, FLITFIX, MONO, PKG, PYFIX, refusals

R = PKG


@pytest.fixture
def pyfix(workspace: Workspace, tmp_path: Path) -> Workspace:
    return build(
        workspace,
        tmp_path,
        {"pyfix": PYFIX, "flatfix": FLATFIX, "flitfix": FLITFIX, "mono": MONO},
    )


def repo_uses(workspace: Workspace, prefix: str) -> set[tuple[str, str, str]]:
    return {use for use in uses(workspace) if use[0].startswith(prefix)}


def test_every_python_use_and_no_other(pyfix: Workspace) -> None:
    """P1-P5, P8-P11, P19, P21 and P18's consumer: the whole edge set of one project."""
    found = {u for u in uses(pyfix) if u[0].startswith((f"{R}/", "tests/"))}
    assert found == {
        # the module's own proven uses: a local string annotation and a local call
        (f"{R}/models.py#make", f"{R}/models.py#User", "type"),
        (f"{R}/models.py#make", f"{R}/models.py#User", "call"),
        # P1: relative, and a from-import of a submodule followed by its member
        (f"{R}/sub/rel.py#p1", f"{R}/models.py#User", "type"),
        (f"{R}/sub/rel.py#p1b", f"{R}/models.py#make", "call"),
        # P2 (P6: the `src.` spelling of the same module resolves to nothing)
        (f"{R}/abs_user.py#p2", f"{R}/models.py#User", "type"),
        # P3
        (f"{R}/alias_user.py#p3", f"{R}/models.py#User", "type"),
        # P4: a module alias, and a chain the package's own __init__ binds
        (f"{R}/mod_alias.py#p4", f"{R}/service.py#run", "call"),
        (f"{R}/mod_alias.py#p4_bound", f"{R}/service.py#run", "call"),
        # P5: through the package __init__, under both names
        (f"{R}/reexport_user.py#p5", f"{R}/models.py#User", "type"),
        # P8: only the method, which does not see the class body
        (f"{R}/shadow.py#Holder.method", f"{R}/models.py#User", "value"),
        # P9
        (f"{R}/checking.py#p9", f"{R}/models.py#User", "type"),
        # P10
        (f"{R}/forms.py#decorated", f"{R}/models.py#register", "value"),
        (f"{R}/forms.py#decorated", f"{R}/models.py#User", "type"),
        (f"{R}/forms.py#Child", f"{R}/models.py#Base", "value"),
        (f"{R}/forms.py#Child", f"{R}/models.py#User", "type"),
        (f"{R}/forms.py#check", f"{R}/models.py#User", "value"),
        (f"{R}/forms.py#build", f"{R}/models.py#make", "call"),
        # P10-TP: only the base, the bound and the non-generic alias. The file is 3.12
        # syntax (PEP 695), so an older interpreter reads nothing from it.
        *(
            {
                (f"{R}/generic.py#C", f"{R}/models.py#Base", "value"),
                (f"{R}/generic.py#g", f"{R}/models.py#Bound", "type"),
                (f"{R}/generic.py#Other", f"{R}/models.py#T", "type"),
            }
            if sys.version_info >= (3, 12)
            else set()
        ),
        # P11: the explicit re-export beside two stars, and the binding after a star
        (f"{R}/star_user.py#p11d", f"{R}/starpkg/things.py#Thing", "type"),
        (f"{R}/star_after.py#p11a", f"{R}/models.py#User", "type"),
        # P21: the imported function, never the same-named method; the local helper
        (f"{R}/calls.py#main", f"{R}/service.py#run", "call"),
        (f"{R}/calls.py#main", f"{R}/calls.py#helper", "call"),
        # P18's first step
        ("tests/test_consumer.py#test_p2", f"{R}/abs_user.py#p2", "call"),
    }


def test_p19_nothing_joins_a_same_named_declaration_by_name(pyfix: Workspace) -> None:
    assert not [u for u in uses(pyfix) if u[1] == f"{R}/unrelated.py#User"]
    assert not [u for u in uses(pyfix) if u[1].startswith(f"{R}/starpkg/a.py")]


def test_p5_the_re_export_path_and_the_names_along_it(pyfix: Workspace) -> None:
    found = {
        (edge["local"], tuple(edge["via"]), tuple(edge["via_names"]), edge["name"])
        for edge in use_edges(pyfix)
        if edge["source"] == f"{R}/reexport_user.py#p5"
    }
    assert found == {
        ("User", (f"{R}/__init__.py",), ("User",), "User"),
        ("PublicUser", (f"{R}/__init__.py",), ("PublicUser",), "User"),
    }


def test_an_edge_records_how_its_module_was_found(pyfix: Workspace) -> None:
    [edge] = [
        e
        for e in use_edges(pyfix)
        if e["source"] == f"{R}/abs_user.py#p2" and e["parser"] == "linker:global"
    ]
    assert edge["basis"] == "python-root"
    assert (edge["root"], edge["root_basis"], edge["project"]) == (
        "src",
        "src-layout",
        "pyproject.toml",
    )
    assert (edge["specifier"], edge["imported"], edge["defined_at"]) == (
        "fixture_pkg.models",
        "User",
        f"{R}/models.py",
    )
    [relative] = [e for e in use_edges(pyfix) if e["source"] == f"{R}/sub/rel.py#p1"]
    assert relative["basis"] == "python-relative"


def test_p9_a_type_checking_use_is_type_only(pyfix: Workspace) -> None:
    [edge] = [e for e in use_edges(pyfix) if e["source"] == f"{R}/checking.py#p9"]
    assert (edge["use"], edge["form"], edge.get("type_only")) == ("type", "string-annotation", True)


def test_what_resolves_to_nothing_says_why(pyfix: Workspace) -> None:
    assert refusals(pyfix, "pyfix", f"{R}/abs_user.py")["Base"] == "module-not-found"
    assert refusals(pyfix, "pyfix", f"{R}/mod_alias.py") == {
        "fixture_pkg.sub.rel.p1": "attribute-unproven"
    }
    assert refusals(pyfix, "pyfix", f"{R}/star_user.py") == {"User": "star-deferred"}
    assert refusals(pyfix, "pyfix", f"{R}/star_before.py") == {}
    # P6: `tasks.py` beside `src/` is no module of a src layout
    assert refusals(pyfix, "pyfix", f"{R}/outside.py") == {"tasks.build": "module-not-found"}


def test_p7_a_flat_project_roots_at_itself_and_scripts_are_no_root(pyfix: Workspace) -> None:
    assert repo_uses(pyfix, "app.py") == {("app.py#p7", "fixture_pkg/models.py#User", "type")}
    assert refusals(pyfix, "flatfix", "app.py") == {"bar.bar": "module-not-found"}
    # nor is a package's own directory: `import models` beside `models.py` names nothing
    assert repo_uses(pyfix, "fixture_pkg/implicit.py") == set()
    assert refusals(pyfix, "flatfix", "fixture_pkg/implicit.py") == {
        "models.User": "module-not-found"
    }


def test_p12_a_project_is_a_boundary_inside_one_repository(pyfix: Workspace) -> None:
    """Each project's own `app`; the other project's module only on its declaration."""
    found = repo_uses(pyfix, "alpha/") | repo_uses(pyfix, "beta/") | repo_uses(pyfix, "gamma/")
    assert found == {
        ("alpha/src/alpha/local.py#mine", "alpha/src/app/__init__.py#App", "type"),
        ("beta/src/beta/service.py#use", "beta/src/app/__init__.py#App", "type"),
        # P12b: `beta` declares `alpha`, the one project providing it
        ("beta/src/beta/service.py#use", "alpha/src/alpha/models.py#Thing", "type"),
    }
    assert refusals(pyfix, "mono", "gamma/src/gamma/uses.py") == {"Thing": "dependency-absent"}
    [edge] = [e for e in use_edges(pyfix) if e["target"] == "alpha/src/alpha/models.py#Thing"]
    assert edge["basis"] == "python-dependency"
    assert (edge["package"], edge["declared_at"], edge["project"]) == (
        "pypi:alpha",
        "beta/pyproject.toml:3",
        "alpha/pyproject.toml",
    )
    assert "crossing" not in edge  # one repository: nothing to cross


def test_p12_a_project_nested_in_a_package_is_out_of_the_outer_roots_reach(
    pyfix: Workspace,
) -> None:
    """The outer flat root spells `fixture_pkg.vendored`, but another project owns it."""
    assert repo_uses(pyfix, "fixture_pkg/nested_user.py") == set()
    assert refusals(pyfix, "flatfix", "fixture_pkg/nested_user.py") == {"V": "project-boundary"}


def test_a_flit_module_is_the_package_its_project_ships(pyfix: Workspace) -> None:
    """The module flit names, never one spelled like the distribution."""
    assert repo_uses(pyfix, "checks/test_widget.py") == {
        ("checks/test_widget.py#test_widget", "src/flit_pkg/models.py#Widget", "type")
    }
    assert refusals(pyfix, "flitfix", "checks/test_widget.py") == {"Decoy": "module-not-found"}
