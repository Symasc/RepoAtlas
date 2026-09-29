"""Python providers their setuptools metadata names (P3b).

A project's ``pyproject.toml``, ``setup.cfg`` and ``setup.py`` are sources of one project
(:mod:`repoatlas.graph.py_modules`); a requirement reaches it only when its static metadata
names the distribution and its stated layout ships the module. Each case names the provider
that must decide it, and what it resolves to -- or why it refuses. The fixtures are in
:mod:`tests.python_setuptools_corpus`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.impact import ImpactRequest
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build
from tests.python_corpus import refusals
from tests.python_legacy_corpus import SERVICE, pick, service_uses
from tests.python_packages_corpus import edges, uses
from tests.python_setuptools_corpus import SETUPTOOLS

FLAT_MODELS = "fixture_auth/models.py"
SRC_MODELS = "src/fixture_auth/models.py"
NAMES = ("User", "make")
GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)


def _build(workspace: Workspace, tmp_path: Path, *names: str) -> Workspace:
    return build(workspace, tmp_path, pick(SETUPTOOLS, *names))


def crossing(workspace: Workspace, consumer: str, target: str) -> dict[str, object]:
    [edge] = [
        e
        for e in edges(workspace)
        if e["source"] == f"{consumer}:{SERVICE}#load" and e["target"] == target
    ]
    assert edge["basis"] == "python-dependency"
    return edge["crossing"]


def reached(workspace: Workspace, consumer: str, provider: str) -> list[dict[str, object]]:
    return [
        e
        for e in edges(workspace)
        if e["source"].startswith(f"{consumer}:") and e["target"].startswith(f"{provider}:")
    ]


@pytest.mark.parametrize(
    ("provider", "models", "provided_at", "entry_fields", "project"),
    [
        # S1: the name on line 3, below a comment spelling it (SM11)
        ("cfg-flat", FLAT_MODELS, "setup.cfg:3", "options.packages", "setup.cfg"),
        ("cfg-src", SRC_MODELS, "setup.cfg:2", "options.packages", "setup.cfg"),  # S2
        # S4: the keyword's own line inside a call over several lines (SM11)
        ("py-flat", FLAT_MODELS, "setup.py:5", "setup.packages", "setup.py"),
        ("py-src", SRC_MODELS, "setup.py:3", "setup.packages", "setup.py"),  # S5
        ("shim-cfg", SRC_MODELS, "setup.cfg:2", "options.packages", "setup.cfg"),  # S7
        ("bs-cfg", SRC_MODELS, "setup.cfg:2", "options.packages", "pyproject.toml"),  # S8
        ("ns-regular", SRC_MODELS, "setup.cfg:2", "options.packages", "setup.cfg"),  # S21
    ],
)
def test_a_requirement_reaches_the_project_its_setuptools_metadata_names(
    workspace: Workspace,
    tmp_path: Path,
    provider: str,
    models: str,
    provided_at: str,
    entry_fields: str,
    project: str,
) -> None:
    linked = _build(workspace, tmp_path, provider, "consumer")
    assert uses(linked, "consumer") == service_uses("consumer", provider, models)
    assert crossing(linked, "consumer", f"{provider}:{models}#make") == {
        "package": "pypi:fixture-auth",
        "provider": provider,
        "provided_at": provided_at,
        "entry": models,
        "entry_fields": entry_fields,
        "subpath": "models",
        "declared_at": "requirements.txt:1",
        "imported_at": 1,
    }
    [edge] = [
        e
        for e in edges(linked)
        if e["target"] == f"{provider}:{models}#make" and e["use"] == "call"
    ]
    assert (edge["project"], edge["root_basis"]) == (project, entry_fields)


def test_s3_s15_the_module_is_found_on_disk_never_spelled_from_the_distribution(
    workspace: Workspace, tmp_path: Path
) -> None:
    linked = _build(
        workspace,
        tmp_path,
        "cfg-distname",
        "special-consumer",
        "zebra",
        "zoo",
        "spelled",
        "consumer",
    )
    special = "special_import/models.py"
    assert uses(linked, "special-consumer") == service_uses(
        "special-consumer", "cfg-distname", special
    )
    assert uses(linked, "zoo") == service_uses("zoo", "zebra", "quokka/models.py")
    assert crossing(linked, "zoo", "zebra:quokka/models.py#make")["package"] == "pypi:Zebra-Tools"
    # SM4/S22: `spelled` is named fixture-auth, holds fixture_auth/ and excludes it
    assert not reached(linked, "consumer", "spelled")


@pytest.mark.parametrize(
    ("provider", "consumer", "why"),
    [
        ("cfg-name-only", "consumer", "project-layout-unread"),  # S1b: nothing says it ships
        ("bs-name-only", "consumer", "project-layout-unread"),  # S1c
        ("conflict-pep621", "consumer", "project-metadata-conflict"),  # S9
        ("conflict-pep621", "other-consumer", "project-metadata-conflict"),
        ("conflict-cfgpy", "consumer", "project-metadata-conflict"),
        ("conflict-cfgpy", "other-consumer", "project-metadata-conflict"),
        ("conflict-layout", "consumer", "module-not-found"),  # S10: no root is read
        ("dyn-name", "consumer", "provider-unnamed"),  # S11: setup() may override the cfg
        ("const-name", "consumer", "provider-unnamed"),  # S11: no constant propagation
        ("const-name-cfg", "consumer", "provider-unnamed"),  # ... even where it would agree
        ("dyn-layout", "consumer", "module-not-found"),  # S12: never discovered instead
        ("where-elsewhere", "consumer", "module-not-found"),  # S26
        ("nested-app", "consumer", "module-not-found"),  # R1: no nested project (SM5)
    ],
)
def test_metadata_that_does_not_state_the_provider_links_nothing(
    workspace: Workspace, tmp_path: Path, provider: str, consumer: str, why: str
) -> None:
    linked = _build(workspace, tmp_path, provider, consumer)
    assert not reached(linked, consumer, provider)
    assert refusals(linked, consumer, SERVICE) == dict.fromkeys(NAMES, why)


def test_s1c_a_layout_it_does_not_state_is_final(workspace: Workspace, tmp_path: Path) -> None:
    """Named by setuptools, shipping discovered only: refused, and a metadata-less copy of
    the module is no rescue."""
    linked = _build(workspace, tmp_path, "bs-name-only", "copy-auth", "consumer")
    assert not [
        e
        for e in edges(linked)
        if e["source"].startswith("consumer:") and ":fixture_auth/" in e["target"]
    ]
    assert refusals(linked, "consumer", SERVICE) == dict.fromkeys(NAMES, "project-layout-unread")


def test_s13_the_explicit_provider_wins_over_a_metadata_less_copy(
    workspace: Workspace, tmp_path: Path
) -> None:
    linked = _build(workspace, tmp_path, "cfg-flat", "copy-auth", "consumer")
    assert uses(linked, "consumer") == service_uses("consumer", "cfg-flat", FLAT_MODELS)
    assert not reached(linked, "consumer", "copy-auth")


def test_s14_an_explicit_name_that_ships_another_package_is_final(
    workspace: Workspace, tmp_path: Path
) -> None:
    linked = _build(workspace, tmp_path, "cfg-other", "copy-auth", "consumer")
    assert not reached(linked, "consumer", "copy-auth")
    assert refusals(linked, "consumer", SERVICE) == dict.fromkeys(NAMES, "dependency-elsewhere")


@pytest.mark.parametrize(
    ("provider", "module"),
    [("py-module", "fixture_mail.py"), ("cfg-module-src", "src/fixture_mail.py")],
)
def test_s6_s23_a_module_py_modules_ships(
    workspace: Workspace, tmp_path: Path, provider: str, module: str
) -> None:
    linked = _build(workspace, tmp_path, provider, "mail-consumer")
    assert (f"mail-consumer:{SERVICE}#load", f"{provider}:{module}#Mailer", "type") in uses(
        linked, "mail-consumer"
    )


def test_s6_a_py_modules_name_a_package_shadows_is_ambiguous(
    workspace: Workspace, tmp_path: Path
) -> None:
    linked = _build(workspace, tmp_path, "py-module-dup", "mail-consumer")
    assert not reached(linked, "mail-consumer", "py-module-dup")


def test_s16_an_outer_layout_never_ships_what_a_nested_project_owns(
    workspace: Workspace, tmp_path: Path
) -> None:
    linked = _build(workspace, tmp_path, "outer", "outer-consumer")
    assert not reached(linked, "outer-consumer", "outer")


def test_s17_a_namespace_top_level_is_not_shipped(workspace: Workspace, tmp_path: Path) -> None:
    linked = _build(workspace, tmp_path, "ns-cfg", "ns-consumer")
    assert not reached(linked, "ns-consumer", "ns-cfg")


@pytest.mark.parametrize("provider", ["pep621-shim", "tool-cfg"])
def test_s24_s25_setuptools_sources_stating_nothing_leave_the_pyproject_project(
    workspace: Workspace, tmp_path: Path, provider: str
) -> None:
    linked = _build(workspace, tmp_path, provider, "consumer")
    assert crossing(linked, "consumer", f"{provider}:{SRC_MODELS}#make") == {
        "package": "pypi:fixture-auth",
        "provider": provider,
        "provided_at": "pyproject.toml:2",
        "entry": SRC_MODELS,
        "entry_fields": "src-layout",
        "subpath": "models",
        "declared_at": "requirements.txt:1",
        "imported_at": 1,
    }


def test_another_backend_reads_no_setup_cfg(workspace: Workspace, tmp_path: Path) -> None:
    linked = _build(workspace, tmp_path, "flit-cfg", "consumer", "other-consumer")
    assert uses(linked, "consumer") == service_uses("consumer", "flit-cfg", FLAT_MODELS)
    assert not reached(linked, "other-consumer", "flit-cfg")


def test_s18_a_change_reaches_the_consumer_and_its_test(
    workspace: Workspace, tmp_path: Path
) -> None:
    linked = _build(workspace, tmp_path, "cfg-src", "consumer")
    seed = f"sym:cfg-src:{SRC_MODELS}:make:5"
    response = CapabilityService(linked).impact(ImpactRequest(context=GLOBAL, seed=seed))
    by_entity = {c.entity: c for c in (*response.candidates, *response.tests)}
    load = by_entity[f"sym:consumer:{SERVICE}:load:4"]
    assert load.distance == 1
    step = load.step.crossing
    assert step is not None
    assert (step.package, step.provided_by, step.declared_by, step.entry_fields) == (
        "pypi:fixture-auth",
        "cfg-src/setup.cfg:2",
        "consumer/requirements.txt:1",
        "options.packages",
    )
    [test] = [c for c in response.tests if c.entity.startswith("sym:consumer:tests/")]
    assert (test.distance, test.from_) == (2, load.id)
