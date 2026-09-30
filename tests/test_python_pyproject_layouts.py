"""Python providers whose ``pyproject.toml`` states their layout (P6 C1).

setuptools' ``[tool.setuptools.packages.find]`` and a hatchling wheel's ``only-include``
name what a project ships (:mod:`repoatlas.parsers.python_project`); a requirement then
reaches it exactly as it reaches any explicit layout -- on the declaration, the one
provider it names, and the module found on disk. Each case names the provider that must
decide it, and what it resolves to or why it refuses. The fixtures are in
:mod:`tests.python_pyproject_layouts_corpus`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build
from tests.python_corpus import refusals
from tests.python_legacy_corpus import SERVICE, pick, service_uses
from tests.python_packages_corpus import edges, uses
from tests.python_pyproject_layouts_corpus import PYPROJECT_LAYOUTS

FLAT_MODELS = "fixture_auth/models.py"
SRC_MODELS = "src/fixture_auth/models.py"
NAMES = ("User", "make")
FIND = "tool.setuptools.packages"
ONLY = "tool.hatch.build.only-include"


def _build(workspace: Workspace, tmp_path: Path, *names: str) -> Workspace:
    return build(workspace, tmp_path, pick(PYPROJECT_LAYOUTS, *names))


def reached(workspace: Workspace, consumer: str, provider: str) -> list[dict[str, object]]:
    return [
        e
        for e in edges(workspace)
        if e["source"].startswith(f"{consumer}:") and e["target"].startswith(f"{provider}:")
    ]


@pytest.mark.parametrize(
    ("provider", "models", "provided_at", "entry_fields"),
    [
        ("find-flat", FLAT_MODELS, "pyproject.toml:2", FIND),  # P3, httpbin's form
        ("find-src", SRC_MODELS, "pyproject.toml:2", FIND),
        ("find-where", SRC_MODELS, "pyproject.toml:2", FIND),  # where alone roots it
        ("find-agree", SRC_MODELS, "setup.cfg:2", FIND),  # one layout, stated twice alike
        ("only-include", FLAT_MODELS, "pyproject.toml:2", ONLY),  # requests-cache's form
        ("only-include-wheel", FLAT_MODELS, "pyproject.toml:2", ONLY),  # the target's own
    ],
)
def test_p3_p7_a_requirement_reaches_the_module_the_pyproject_layout_ships(
    workspace: Workspace,
    tmp_path: Path,
    provider: str,
    models: str,
    provided_at: str,
    entry_fields: str,
) -> None:
    linked = _build(workspace, tmp_path, provider, "consumer")
    assert uses(linked, "consumer") == service_uses("consumer", provider, models)
    [edge] = [
        e
        for e in edges(linked)
        if e["source"] == f"consumer:{SERVICE}#load" and e["target"] == f"{provider}:{models}#make"
    ]
    assert edge["basis"] == "python-dependency"
    assert edge["crossing"] == {
        "package": "pypi:fixture-auth",
        "provider": provider,
        "provided_at": provided_at,
        "entry": models,
        "entry_fields": entry_fields,
        "subpath": "models",
        "declared_at": "requirements.txt:1",
        "imported_at": 1,
    }
    assert edge["root_basis"] == entry_fields


@pytest.mark.parametrize(
    ("provider", "consumer", "why"),
    [
        ("find-flat", "undeclared", "dependency-absent"),  # P1: physical provider, no evidence
        ("find-excluded", "consumer", "module-not-found"),  # P2: declared, not shipped
        ("find-two-where", "consumer", "module-not-found"),  # P6: not read, nothing shipped
        ("find-conflict", "consumer", "module-not-found"),  # P5: no root is read
        ("find-namespaces-differ", "consumer", "module-not-found"),  # find_namespace vs find
        ("find-hatchling", "consumer", "module-not-found"),  # another backend's keys
        ("only-include-nested", "consumer", "module-not-found"),
        ("only-include-forced", "consumer", "module-not-found"),
        ("only-include-exclude", "consumer", "module-not-found"),
    ],
)
def test_a_layout_that_does_not_ship_the_module_or_is_not_read_links_nothing(
    workspace: Workspace, tmp_path: Path, provider: str, consumer: str, why: str
) -> None:
    linked = _build(workspace, tmp_path, provider, consumer)
    assert not reached(linked, consumer, provider)
    assert refusals(linked, consumer, SERVICE) == dict.fromkeys(NAMES, why)


@pytest.mark.parametrize(
    ("provider", "path", "why"),
    [
        ("find-two-where", "src/app.py", "project-layout-unread"),  # P6: never src/ instead
        ("find-conflict", "app.py", "project-metadata-conflict"),  # P5: neither source wins
    ],
)
def test_p5_p6_a_refused_layout_is_final_inside_the_project_too(
    workspace: Workspace, tmp_path: Path, provider: str, path: str, why: str
) -> None:
    linked = _build(workspace, tmp_path, provider)
    assert refusals(linked, provider, path) == dict.fromkeys(NAMES, why)


def test_p4_two_providers_of_one_distribution_are_ambiguous(
    workspace: Workspace, tmp_path: Path
) -> None:
    linked = _build(workspace, tmp_path, "find-flat", "only-include-twin", "consumer")
    assert not reached(linked, "consumer", "find-flat")
    assert not reached(linked, "consumer", "only-include-twin")
    assert refusals(linked, "consumer", SERVICE) == dict.fromkeys(NAMES, "provider-ambiguous")


def test_only_include_ships_what_it_names_and_never_the_project_s_own_tests(
    workspace: Workspace, tmp_path: Path
) -> None:
    """requests-cache's shape: its tests import the package it ships, and their own
    helpers, which a wheel limited to ``only-include`` does not ship (plan decision 2)."""
    linked = _build(workspace, tmp_path, "only-include")
    here = "only-include:tests/test_models.py#test_make"
    [edge] = [e for e in edges(linked) if e["source"] == here]
    assert (edge["target"], edge["basis"], edge["root_basis"], edge["root"]) == (
        f"only-include:{FLAT_MODELS}#make",
        "python-root",
        ONLY,
        ".",
    )
    assert refusals(linked, "only-include", "tests/test_models.py") == {
        "helper": "module-not-found"
    }


def test_a_find_layout_s_own_module_resolves_inside_its_project(
    workspace: Workspace, tmp_path: Path
) -> None:
    linked = _build(workspace, tmp_path, "find-flat")
    found = {
        (e["target"], e["root_basis"])
        for e in edges(linked)
        if e["source"] == "find-flat:app.py#build"
    }
    assert found == {
        (f"find-flat:{FLAT_MODELS}#User", FIND),
        (f"find-flat:{FLAT_MODELS}#make", FIND),
    }
