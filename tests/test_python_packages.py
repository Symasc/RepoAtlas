"""Python uses across projects, proven by the importer's own declaration (P12b-P18).

Every repository is indexed whole; each case names the declaration, pin or layout that
must decide it, and what it resolves to -- or why it refuses. The fixtures are in
:mod:`tests.python_packages_corpus`.
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
from tests.python_packages_corpus import (
    MODELS,
    SERVICE,
    TWINS,
    UVWS,
    XREPO,
    consumer_uses,
    edges,
    uses,
)

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)
SEED = "sym:provider:src/internal_fixture/models.py:make:3"


@pytest.fixture
def xrepo(workspace: Workspace, tmp_path: Path) -> Workspace:
    return build(workspace, tmp_path, XREPO)


def test_p14_a_version_declaration_reaches_the_one_provider(xrepo: Workspace) -> None:
    """Through the provider's `__init__` under both names (P17), never the shadow."""
    assert uses(xrepo, "consumer") == consumer_uses("consumer")
    assert not [e for e in edges(xrepo) if e["target"].startswith("shadow:")]
    names = {
        (e["local"], tuple(e["via"]))
        for e in edges(xrepo)
        if e["source"] == f"consumer:{SERVICE}#build" and e["target"] == f"{MODELS}#Widget"
    }
    assert names == {
        ("Widget", ("src/internal_fixture/__init__.py",)),
        ("PublicWidget", ("src/internal_fixture/__init__.py",)),
    }


def test_p14_the_crossing_records_what_proved_it(xrepo: Workspace) -> None:
    [edge] = [
        e
        for e in edges(xrepo)
        if e["source"] == f"consumer:{SERVICE}#via_module" and e["target"] == f"{MODELS}#make"
    ]
    assert edge["basis"] == "python-dependency"
    assert edge["crossing"] == {
        "package": "pypi:internal-fixture",
        "provider": "provider",
        "provided_at": "pyproject.toml:2",
        "entry": "src/internal_fixture/models.py",
        "entry_fields": "src-layout",
        "subpath": "models",
        "declared_at": "pyproject.toml:3",
        "constraint": ">=1.0",
        "imported_at": 3,
    }


def test_p15_a_uv_path_pins_the_provider(xrepo: Workspace) -> None:
    assert uses(xrepo, "pinned") == consumer_uses("pinned")
    crossings = {
        (e["crossing"]["resolved_by"], e["crossing"]["pinned_at"])
        for e in edges(xrepo)
        if e["source"].startswith("pinned:src/") and "crossing" in e
    }
    assert crossings == {("path", "pinned/pyproject.toml")}


def test_p15_a_pin_that_fails_never_falls_back_to_the_name(xrepo: Workspace) -> None:
    """`internal-fixture` has one provider, but these pins do not lead to it."""
    names = {"Widget", "PublicWidget", "make", "models.make"}
    assert refusals(xrepo, "mispinned", SERVICE) == dict.fromkeys(names, "dependency-elsewhere")
    assert refusals(xrepo, "unpinned", SERVICE) == dict.fromkeys(names, "path-outside-index")
    for repository in ("mispinned", "unpinned"):
        test = f"{repository}:tests/test_service.py#test_build"
        assert uses(xrepo, repository) == {(test, f"{repository}:{SERVICE}#build", "call")}


def test_p13_a_distribution_is_declared_by_its_own_name_never_its_module(
    xrepo: Workspace,
) -> None:
    assert uses(xrepo, "rightname") == {
        ("rightname:src/importer/use.py#use", "distpkg:src/fixture_pkg/__init__.py#Thing", "type")
    }
    assert refusals(xrepo, "wrongname", "src/importer/use.py") == {"Thing": "dependency-absent"}


def test_declarations_in_forms_not_read_are_refused_by_name(xrepo: Workspace) -> None:
    assert refusals(xrepo, "unread", "src/unread/use.py") == {
        "Thing": "dependency-unread",  # `fixture-dist @ file:...`
        "Widget": "dependency-elsewhere",  # a git source
    }
    assert refusals(xrepo, "poetic", "poetic/use.py") == {"Widget": "dependency-unread"}


def test_p16_two_providers_of_one_name_join_neither(workspace: Workspace, tmp_path: Path) -> None:
    twins = build(workspace, tmp_path, TWINS)
    assert set(refusals(twins, "consumer", SERVICE).values()) == {"provider-ambiguous"}
    assert not [
        e
        for e in edges(twins)
        if e["source"].startswith("consumer:") and e["target"].startswith(("provider:", "twin:"))
    ]


def test_p12b_a_uv_workspace_member_inherits_its_roots_source(
    workspace: Workspace, tmp_path: Path
) -> None:
    uvws = build(workspace, tmp_path, {"uvws": UVWS})
    helper = "uvws:packages/lib/src/lib/__init__.py#helper"
    found = {
        (e["source"], e["resolved_by"], e["pinned_at"])
        for e in edges(uvws)
        if e["target"] == helper and e["basis"] == "python-dependency"
    }
    assert found == {
        # no source of its own: the root's, which every member inherits
        ("uvws:packages/app/src/app/__init__.py#run", "workspace", "uvws/pyproject.toml"),
        # its own source overrides the root's
        ("uvws:packages/tool/src/tool/__init__.py#go", "path", "uvws/packages/tool/pyproject.toml"),
    }
    assert all("crossing" not in e for e in edges(uvws))
    assert refusals(uvws, "uvws", "packages/legacy/src/legacy/__init__.py") == {
        "helper": "workspace-not-member"
    }


def test_p18_a_change_reaches_the_consumer_in_another_repository_and_its_test(
    xrepo: Workspace,
) -> None:
    response = CapabilityService(xrepo).impact(ImpactRequest(context=GLOBAL, seed=SEED))
    by_entity = {c.entity: c for c in (*response.candidates, *response.tests)}
    consumer = by_entity[f"sym:consumer:{SERVICE}:build:4"]
    assert consumer.distance == 1
    assert consumer.step.crossing is not None
    assert consumer.step.crossing.package == "pypi:internal-fixture"
    assert consumer.step.crossing.declared_by == "consumer/pyproject.toml:3"
    [test] = [c for c in response.tests if c.entity.startswith("sym:consumer:tests/")]
    assert (test.distance, test.from_) == (2, consumer.id)


def test_a_dependent_is_listed_only_when_its_declaration_names_the_provider(
    xrepo: Workspace,
) -> None:
    """A version, or a uv path to the provider, names it; a uv path anywhere else, a git
    source, a direct reference or a Poetry table does not -- as a package link reads it."""
    response = CapabilityService(xrepo).impact(ImpactRequest(context=GLOBAL, seed=SEED))
    listed = {p.dependent for p in response.packages if p.package == "pypi:internal-fixture"}
    assert listed == {"consumer:.", "pinned:."}
