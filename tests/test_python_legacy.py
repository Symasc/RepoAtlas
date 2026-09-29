"""Python uses declared by a repository's root ``requirements.txt`` (P3a).

A repository stating nothing about packaging declares its dependencies in its root
``requirements.txt`` and nowhere else. Every repository is indexed whole; each case names
the declaration or the provider that must decide it, and what it resolves to -- or why it
refuses. The fixtures are in :mod:`tests.python_legacy_corpus`.
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
from tests.python_legacy_corpus import HYBRID, LEGACY, SERVICE, pick, service_uses
from tests.python_packages_corpus import edges, uses

EXPLICIT_MODELS = "src/fixture_auth/models.py"
NAMES = ("User", "make")


@pytest.fixture
def hybrid(workspace: Workspace, tmp_path: Path) -> Workspace:
    return build(workspace, tmp_path, HYBRID)


def test_r9a_a_root_requirement_names_the_explicit_provider(hybrid: Workspace) -> None:
    """Not the metadata-less copy of the same package, which no metadata names."""
    assert uses(hybrid, "consumer") == service_uses("consumer", "explicit-auth", EXPLICIT_MODELS)
    [edge] = [
        e
        for e in edges(hybrid)
        if e["source"] == f"consumer:{SERVICE}#load"
        and e["target"] == f"explicit-auth:{EXPLICIT_MODELS}#make"
    ]
    assert edge["basis"] == "python-dependency"
    assert edge["crossing"] == {
        "package": "pypi:fixture-auth",
        "provider": "explicit-auth",
        "provided_at": "pyproject.toml:2",
        "entry": EXPLICIT_MODELS,
        "entry_fields": "src-layout",
        "subpath": "models",
        "declared_at": "requirements.txt:4",
        "constraint": ">=1.2",
        "imported_at": 1,
    }
    assert not [
        e
        for e in edges(hybrid)
        if e["target"].startswith("copy-auth:") and not e["source"].startswith("copy-auth:")
    ]


@pytest.mark.parametrize(
    ("repository", "why"),
    [
        ("devonly", "dependency-absent"),  # R22: requirements-dev.txt declares nothing here
        ("nestedonly", "dependency-absent"),  # R22: nor does a requirements.txt below the root
        ("vcs", "dependency-absent"),  # R23: a git+https line is read as a row named `git`
        ("direct", "dependency-unread"),  # R23b: `name @ url` names it in a form not read
        ("setuppy", "dependency-absent"),  # R24: a root setup.py is metadata this does not read
    ],
)
def test_only_the_root_requirements_file_of_a_metadata_less_repository_declares(
    workspace: Workspace, tmp_path: Path, repository: str, why: str
) -> None:
    linked = build(workspace, tmp_path, pick(HYBRID, "explicit-auth", repository))
    assert not [
        e
        for e in edges(linked)
        if e["source"].startswith(f"{repository}:") and e["target"].startswith("explicit-auth:")
    ]
    assert refusals(linked, repository, SERVICE) == dict.fromkeys(NAMES, why)


# -- the legacy convention: a metadata-less repository holding the module (C2) ---------

LEGACY_MODELS = "fixture_auth/models.py"
GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)


def _legacy(workspace: Workspace, tmp_path: Path, *names: str) -> Workspace:
    return build(workspace, tmp_path, pick(LEGACY, *names))


@pytest.mark.parametrize(
    ("repository", "requirement", "line", "constraint"),
    [
        ("basic", "fixture-auth", 1, None),  # R1
        ("capital", "Fixture-Auth", 3, None),  # R2: case, and the line it is on
        ("versioned", "fixture-auth", 1, ">=1.2"),  # R3
        ("extras", "fixture-auth", 1, ">=1"),  # R4: extras are no part of the name
    ],
)
def test_r1_r4_a_root_requirement_reaches_the_one_repository_holding_the_module(
    workspace: Workspace,
    tmp_path: Path,
    repository: str,
    requirement: str,
    line: int,
    constraint: str | None,
) -> None:
    linked = _legacy(workspace, tmp_path, "auth-provider", repository)
    assert uses(linked, repository) == service_uses(repository, "auth-provider", LEGACY_MODELS)
    [edge] = [
        e
        for e in edges(linked)
        if e["source"] == f"{repository}:{SERVICE}#load"
        and e["target"] == f"auth-provider:{LEGACY_MODELS}#make"
    ]
    assert edge["basis"] == "python-requirements-legacy"
    assert (edge["root"], edge["root_basis"], edge["project"]) == (
        ".",
        "repository-root",
        "repository-root",
    )
    expected = {
        "package": f"pypi:{requirement}",
        "provider": "auth-provider",
        "provided_at": "fixture_auth/__init__.py",
        "entry": LEGACY_MODELS,
        "entry_fields": "python-requirements-legacy",
        "declared_at": f"requirements.txt:{line}",
        "resolved_by": "requirements-legacy",
        "requirement": requirement,
        "legacy_requirement_key": "fixture-auth",
        "import": "fixture_auth",
        "legacy_import_key": "fixture-auth",
        "subpath": "models",
        "imported_at": 1,
    }
    if constraint is not None:
        expected["constraint"] = constraint
    assert edge["crossing"] == expected


@pytest.mark.parametrize(
    ("repositories", "consumer", "why"),
    [
        (("auth-provider", "undeclared"), "undeclared", "dependency-absent"),  # R5
        (("auth-provider", "mismatch"), "mismatch", "dependency-absent"),  # R6
        (("auth-provider", "fuzzy"), "fuzzy", "dependency-absent"),  # R7b
        (("auth-provider", "pep503"), "pep503", "dependency-absent"),  # R7c
        (("auth-provider", "casing"), "casing", "module-not-found"),  # LM4
        (("auth-provider", "devonly"), "devonly", "dependency-absent"),  # R22
        (("auth-provider", "nestedonly"), "nestedonly", "dependency-absent"),  # R22
        (("auth-provider", "vcs"), "vcs", "dependency-absent"),  # R23
        (("auth-provider", "direct"), "direct", "dependency-unread"),  # R23b
        (("auth-provider", "setuppy"), "setuppy", "module-not-found"),  # R24
        (("lookalikes", "widgets"), "widgets", "module-not-found"),  # R7
        (("dup-a", "dup-b", "dupuser"), "dupuser", "provider-ambiguous"),  # R8
        (("half-a", "half-b", "halfuser"), "halfuser", "provider-ambiguous"),  # R8b
        (("grid-explicit", "grid-copy", "griduser"), "griduser", "dependency-absent"),  # R9b
        (("mail-explicit", "mail-copy", "mailuser"), "mailuser", "dependency-elsewhere"),  # R10
        (("sms-setup", "sms-copy", "smsuser"), "smsuser", "dependency-elsewhere"),  # R10b
        (("src-provider", "srcuser"), "srcuser", "module-not-found"),  # R12
        (("ns-provider", "nsuser"), "nsuser", "namespace-package"),  # R13
        (("flip-provider", "flipuser"), "flipuser", "module-not-found"),  # R25
        (("gitlike", "gituser"), "gituser", "dependency-absent"),  # R23c
    ],
)
def test_a_requirement_joins_nothing_it_does_not_name_or_cannot_single_out(
    workspace: Workspace, tmp_path: Path, repositories: tuple[str, ...], consumer: str, why: str
) -> None:
    linked = _legacy(workspace, tmp_path, *repositories)
    assert not [
        e
        for e in edges(linked)
        if e["source"].startswith(f"{consumer}:") and not e["target"].startswith(f"{consumer}:")
    ]
    names = NAMES if "Thing" not in LEGACY[consumer][SERVICE] else ("Thing",)
    assert refusals(linked, consumer, SERVICE) == dict.fromkeys(names, why)


def test_r11_a_module_at_the_root_is_held_as_well_as_a_package(
    workspace: Workspace, tmp_path: Path
) -> None:
    linked = _legacy(workspace, tmp_path, "solo-provider", "solouser")
    found = {
        (e["target"], e["use"], e["crossing"]["provided_at"], e["crossing"]["entry"])
        for e in edges(linked)
        if e["source"] == f"solouser:{SERVICE}#load"
    }
    target = "solo-provider:solo_mod.py#Thing"
    assert found == {
        (target, "type", "solo_mod.py", "solo_mod.py"),
        (target, "call", "solo_mod.py", "solo_mod.py"),
    }


def test_r14_a_re_export_is_followed_into_the_provider(
    workspace: Workspace, tmp_path: Path
) -> None:
    linked = _legacy(workspace, tmp_path, "auth-provider", "reexport")
    assert uses(linked, "reexport") == service_uses("reexport", "auth-provider", LEGACY_MODELS)
    vias = {tuple(e["via"]) for e in edges(linked) if e["source"] == f"reexport:{SERVICE}#load"}
    assert vias == {("fixture_auth/__init__.py",)}


def test_r15_a_change_reaches_the_legacy_consumer_and_its_test(
    workspace: Workspace, tmp_path: Path
) -> None:
    linked = _legacy(workspace, tmp_path, "auth-provider", "basic")
    seed = f"sym:auth-provider:{LEGACY_MODELS}:make:5"
    response = CapabilityService(linked).impact(ImpactRequest(context=GLOBAL, seed=seed))
    by_entity = {c.entity: c for c in (*response.candidates, *response.tests)}
    load = by_entity[f"sym:basic:{SERVICE}:load:4"]
    assert load.distance == 1
    crossing = load.step.crossing
    assert crossing is not None
    assert (crossing.package, crossing.provided_by, crossing.declared_by) == (
        "pypi:fixture-auth",
        "auth-provider/fixture_auth/__init__.py",
        "basic/requirements.txt:1",
    )
    assert crossing.entry_fields == "python-requirements-legacy"
    [test] = [c for c in response.tests if c.entity.startswith("sym:basic:tests/")]
    assert (test.distance, test.from_) == (2, load.id)
    [package] = [p for p in response.packages if p.package == "pypi:fixture-auth"]
    assert (package.dependent, package.declared_at, package.exact_consumers) == (
        "basic:.",
        "basic/requirements.txt:1",
        1,
    )
