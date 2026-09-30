"""Namespace portions (PEP 420) inside a Python project (P6 C2).

A directory without ``__init__`` is walked through where it is the one directory a name
can mean: below a regular package (N1a), and at the top of the importer's *own* project's
roots when exactly one of them holds the module named (N1b). A portion is never itself a
module, never shipped and never provided: another project's reaches the importer only
inside a provider its declaration already names. The fixtures are in
:mod:`tests.python_namespace_corpus`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build
from tests.python_corpus import refusals
from tests.python_legacy_corpus import SERVICE, pick, service_uses
from tests.python_namespace_corpus import NAMESPACES, PORTION_MODELS
from tests.python_packages_corpus import edges, uses

NAMES = ("User", "make")


def _build(workspace: Workspace, tmp_path: Path, *names: str) -> Workspace:
    return build(workspace, tmp_path, pick(NAMESPACES, *names))


def _from(workspace: Workspace, source: str) -> dict[str, dict[str, object]]:
    return {e["target"]: e for e in edges(workspace) if e["source"] == source}


def test_n1a_a_portion_s_modules_and_their_relative_names_resolve_inside_the_project(
    workspace: Workspace, tmp_path: Path
) -> None:
    linked = _build(workspace, tmp_path, "portioned")
    built = _from(linked, "portioned:src/fixture_auth/app.py#build")
    assert set(built) == {f"portioned:{PORTION_MODELS}#User", f"portioned:{PORTION_MODELS}#make"}
    assert {e["basis"] for e in built.values()} == {"python-relative"}
    rebuilt = _from(linked, "portioned:src/fixture_auth/sansio/base.py#rebuild")
    assert set(rebuilt) == {
        "portioned:src/fixture_auth/app.py#build",
        f"portioned:{PORTION_MODELS}#User",
    }


def test_n1a_a_declared_provider_is_walked_through_its_portion(
    workspace: Workspace, tmp_path: Path
) -> None:
    """Only inside the provider the declaration already names: the portion selects and
    authorizes nothing."""
    linked = _build(workspace, tmp_path, "portioned", "consumer")
    assert uses(linked, "consumer") == service_uses("consumer", "portioned", PORTION_MODELS)
    edge = _from(linked, f"consumer:{SERVICE}#load")[f"portioned:{PORTION_MODELS}#make"]
    assert edge["basis"] == "python-dependency"
    assert edge["crossing"] == {
        "package": "pypi:fixture-auth",
        "provider": "portioned",
        "provided_at": "pyproject.toml:2",
        "entry": PORTION_MODELS,
        "entry_fields": "src-layout",
        "subpath": "sansio.models",
        "declared_at": "requirements.txt:1",
        "imported_at": 1,
    }


def test_n1a_an_undeclared_import_through_a_portion_reaches_nothing(
    workspace: Workspace, tmp_path: Path
) -> None:
    linked = _build(workspace, tmp_path, "portioned", "undeclared")
    assert not [
        e
        for e in edges(linked)
        if e["source"].startswith("undeclared:") and e["target"].startswith("portioned:")
    ]
    assert refusals(linked, "undeclared", SERVICE) == dict.fromkeys(NAMES, "dependency-absent")


def test_n1b_the_importer_s_own_top_level_portion_resolves_with_its_evidence(
    workspace: Workspace, tmp_path: Path
) -> None:
    linked = _build(workspace, tmp_path, "ha")
    [edge] = _from(linked, "ha:tests/test_const.py#test_domain").values()
    assert edge["target"] == "ha:custom_components/fixture_ha/const.py#domain"
    assert (edge["basis"], edge["root"], edge["root_basis"], edge["namespace_package"]) == (
        "python-root",
        ".",
        "repository-root",
        "custom_components",
    )


@pytest.mark.parametrize("importer", ["ha", "ha-declaring"])
def test_n1b_a_namespace_another_repository_also_holds_is_never_linked(
    workspace: Workspace, tmp_path: Path, importer: str
) -> None:
    """The namespace root is shared and nobody's: a module only another repository holds
    below it is ``namespace-package`` -- not ``dependency-absent``, which would say a
    declaration could reach it -- and a declaration of the namespace's name does not."""
    linked = _build(workspace, tmp_path, importer, "ha-other")
    assert not [e for e in edges(linked) if e["target"].startswith("ha-other:")]
    assert refusals(linked, importer, "tests/test_const.py") == {"other": "namespace-package"}


def test_n1c_another_repository_s_top_level_portion_is_not_walked_even_when_required(
    workspace: Workspace, tmp_path: Path
) -> None:
    linked = _build(workspace, tmp_path, "plain", "ha-other")
    assert not [e for e in edges(linked) if e["target"].startswith("ha-other:")]
    assert refusals(linked, "plain", SERVICE) == {"other": "namespace-package"}
