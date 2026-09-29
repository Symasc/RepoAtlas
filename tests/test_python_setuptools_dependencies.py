"""A setuptools project's own ``install_requires`` as its Python dependency declaration (P3c).

The project's dependencies are one field, taken whole from the one source setuptools takes
them from (:mod:`repoatlas.graph.py_modules`), and the existing Python package link reads
it as it reads a ``[project]`` table's (:mod:`repoatlas.graph.py_packages`). Each case says
what the consumer's ``service.py`` resolves to, where its declaration is written, or why it
refuses. The fixtures are in :mod:`tests.python_setuptools_deps_corpus`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.impact import ImpactRequest
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build
from tests.incremental_corpus import (
    add_repo,
    append,
    assert_equals_fresh,
    edit,
    remove_repo,
    sync_all,
)
from tests.python_corpus import refusals
from tests.python_legacy_corpus import SERVICE, pick, service_uses
from tests.python_packages_corpus import edges, uses
from tests.python_setuptools_deps_corpus import CFG_D1, DEPS, MODELS, PROVIDER, SETUP

NAMES = ("User", "make")
GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)


def _build(workspace: Workspace, tmp_path: Path, *names: str) -> Workspace:
    return build(workspace, tmp_path, pick(DEPS, *names))


def declared_at(workspace: Workspace, consumer: str, path: str = SERVICE) -> str:
    """Where the consumer's use of ``make`` says it is declared; one edge only."""
    [edge] = [
        e
        for e in edges(workspace)
        if e["source"] == f"{consumer}:{path}#load" and e["target"] == f"{PROVIDER}:{MODELS}#make"
    ]
    assert edge["basis"] == "python-dependency"
    assert edge["crossing"]["declared_at"] == edge["declared_at"]
    return edge["declared_at"]


def linked(workspace: Workspace, consumer: str) -> bool:
    return uses(workspace, consumer) == service_uses(consumer, PROVIDER, MODELS)


@pytest.mark.parametrize(
    ("consumer", "where"),
    [
        ("cfg-consumer", "setup.cfg:7"),  # D1
        ("cfg-mentions", "setup.cfg:10"),  # D2: never the description, keywords or a comment
        ("cfg-overlap", "setup.cfg:8"),  # D3: never inside fixture-auth-extra
        ("py-consumer", "setup.py:6"),  # D4: the requirement's own Constant
        ("pep621-and-cfg", "pyproject.toml:6"),  # D6: [project] states them
        ("pep621-dynamic-cfg", "setup.cfg:7"),  # D6b: ... unless dynamic names them
        ("bs-cfg", "setup.cfg:7"),  # a build-system pyproject completed by its setup.cfg
        ("py-and-cfg", "setup.py:3"),  # D7: setup.py's field, never listed twice
        ("shim-cfg", "setup.cfg:7"),  # D7: a shim states nothing (CM10)
        ("extras-too", "setup.cfg:7"),  # D10: install_requires, not the extra
        ("cfg-marker", "setup.cfg:7"),  # D11: read as a pyproject's marker is
    ],
)
def test_install_requires_declares_the_provider(
    workspace: Workspace, tmp_path: Path, consumer: str, where: str
) -> None:
    built = _build(workspace, tmp_path, PROVIDER, consumer)
    assert linked(built, consumer)
    assert declared_at(built, consumer) == where


def test_d1_the_edge_is_the_one_a_pyproject_declaration_gives(
    workspace: Workspace, tmp_path: Path
) -> None:
    built = _build(workspace, tmp_path, PROVIDER, "cfg-consumer")
    [edge] = [e for e in edges(built) if e["target"] == f"{PROVIDER}:{MODELS}#make"]
    assert edge["crossing"] == {
        "package": "pypi:fixture-auth",
        "provider": PROVIDER,
        "provided_at": "pyproject.toml:2",
        "entry": MODELS,
        "entry_fields": "src-layout",
        "subpath": "models",
        "declared_at": "setup.cfg:7",
        "constraint": ">=1",
        "imported_at": 1,
    }
    assert (edge["project"], edge["package"]) == ("pyproject.toml", "pypi:fixture-auth")


@pytest.mark.parametrize(
    ("consumer", "why"),
    [
        ("py-dynamic", "dependency-unread"),  # D5: computed, and no fallback to setup.cfg
        ("pep621-none-cfg", "dependency-absent"),  # D6: [project] states none
        ("py-other-cfg", "dependency-absent"),  # D7: setup.cfg's list is overridden (CM6)
        ("cfg-direct", "dependency-unread"),  # D8: a direct reference, never a version
        ("extras-only", "dependency-absent"),  # D9: an extra is not a dependency (CM5)
        ("cfg-marker-one-line", "dependency-unread"),  # D11: setuptools would split it
    ],
)
def test_a_declaration_not_read_links_nothing(
    workspace: Workspace, tmp_path: Path, consumer: str, why: str
) -> None:
    built = _build(workspace, tmp_path, PROVIDER, consumer)
    assert not [
        e
        for e in edges(built)
        if e["source"].startswith(f"{consumer}:") and e["target"].startswith(f"{PROVIDER}:")
    ]
    assert refusals(built, consumer, SERVICE) == dict.fromkeys(NAMES, why)


def test_d12_a_nested_setup_cfg_declares_nothing_for_the_enclosing_project(
    workspace: Workspace, tmp_path: Path
) -> None:
    """R1: app/ is no project, so its setup.cfg is not the outer project's (CM2)."""
    built = _build(workspace, tmp_path, PROVIDER, "nested-cfg")
    path = f"app/{SERVICE}"
    assert refusals(built, "nested-cfg", path) == dict.fromkeys(NAMES, "dependency-absent")


def test_each_project_of_a_repository_declares_for_itself(
    workspace: Workspace, tmp_path: Path
) -> None:
    """CM7: the root project's install_requires is not its nested project's."""
    built = _build(workspace, tmp_path, PROVIDER, "two-projects")
    assert declared_at(built, "two-projects") == "setup.cfg:7"
    nested = f"lib/src/lib/{SERVICE}"
    assert refusals(built, "two-projects", nested) == dict.fromkeys(NAMES, "dependency-absent")


def test_d13_two_providers_stay_ambiguous(workspace: Workspace, tmp_path: Path) -> None:
    built = _build(workspace, tmp_path, PROVIDER, "twin-auth", "cfg-consumer")
    assert refusals(built, "cfg-consumer", SERVICE) == dict.fromkeys(NAMES, "provider-ambiguous")


def test_d14_the_declared_provider_wins_over_a_metadata_less_copy(
    workspace: Workspace, tmp_path: Path
) -> None:
    built = _build(workspace, tmp_path, PROVIDER, "copy-auth", "cfg-consumer")
    assert linked(built, "cfg-consumer")
    assert not [
        e
        for e in edges(built)
        if e["source"].startswith("cfg-consumer:") and e["target"].startswith("copy-auth:")
    ]


def test_the_distribution_is_never_spelled_into_a_module(
    workspace: Workspace, tmp_path: Path
) -> None:
    """CM8: fixture-auth ships special_import; the declaration selects the project, and its
    layout proves the module."""
    built = _build(workspace, tmp_path, "dist-other", "cfg-special")
    models = "src/special_import/models.py"
    assert uses(built, "cfg-special") == service_uses("cfg-special", "dist-other", models)


def test_d15_a_change_reaches_the_setuptools_consumer_and_its_test(
    workspace: Workspace, tmp_path: Path
) -> None:
    built = _build(workspace, tmp_path, PROVIDER, "cfg-consumer")
    seed = f"sym:{PROVIDER}:{MODELS}:make:5"
    response = CapabilityService(built).impact(ImpactRequest(context=GLOBAL, seed=seed))
    by_entity = {c.entity: c for c in (*response.candidates, *response.tests)}
    load = by_entity[f"sym:cfg-consumer:{SERVICE}:load:4"]
    assert load.distance == 1
    step = load.step.crossing
    assert step is not None
    assert (step.package, step.provided_by, step.declared_by) == (
        "pypi:fixture-auth",
        f"{PROVIDER}/pyproject.toml:2",
        "cfg-consumer/setup.cfg:7",
    )
    [test] = [c for c in response.tests if c.entity.startswith("sym:cfg-consumer:tests/")]
    assert (test.distance, test.from_) == (2, load.id)


# -- incremental = fresh (D16-D19, plan §20) ------------------------------------------------


def _equal(workspace: Workspace, settings: Settings, tmp_path: Path, tag: str) -> None:
    assert_equals_fresh(workspace, settings, tmp_path / "repos", tmp_path, tag)


def _refused(workspace: Workspace, consumer: str, why: str) -> bool:
    return refusals(workspace, consumer, SERVICE) == dict.fromkeys(NAMES, why)


def test_d16_a_setup_cfg_requirement_removed_renamed_and_back(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    built = _build(workspace, tmp_path, PROVIDER, "cfg-consumer")
    root = tmp_path / "repos"
    edit(root, "cfg-consumer", "setup.cfg", CFG_D1.replace("\n    fixture-auth>=1", ""))
    work = sync_all(built, root)
    assert work.mode == "incremental" and work.package == 0  # CM9: the row change reaches it
    assert _refused(built, "cfg-consumer", "dependency-absent")
    _equal(built, settings, tmp_path, "removed")
    edit(root, "cfg-consumer", "setup.cfg", CFG_D1.replace("fixture-auth", "fixture-other"))
    sync_all(built, root)
    assert _refused(built, "cfg-consumer", "dependency-absent")
    _equal(built, settings, tmp_path, "renamed")
    edit(root, "cfg-consumer", "setup.cfg", CFG_D1)
    sync_all(built, root)
    assert declared_at(built, "cfg-consumer") == "setup.cfg:7"
    _equal(built, settings, tmp_path, "back")


def test_d17_a_setup_py_requirement_changed_computed_and_back(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    built = _build(workspace, tmp_path, PROVIDER, "py-consumer")
    root = tmp_path / "repos"
    text = DEPS["py-consumer"]["setup.py"]
    edit(root, "py-consumer", "setup.py", text.replace('"fixture-auth>=1"', '"fixture-other"'))
    sync_all(built, root)
    assert _refused(built, "py-consumer", "dependency-absent")
    _equal(built, settings, tmp_path, "other")
    computed = text.replace('[\n        "fixture-auth>=1",\n    ]', "REQS")
    edit(root, "py-consumer", "setup.py", 'REQS = ["fixture-auth"]\n' + computed)
    sync_all(built, root)
    assert _refused(built, "py-consumer", "dependency-unread")
    _equal(built, settings, tmp_path, "computed")
    edit(root, "py-consumer", "setup.py", text)
    sync_all(built, root)
    assert declared_at(built, "py-consumer") == "setup.py:6"
    _equal(built, settings, tmp_path, "static")


def test_d18_the_source_stating_the_dependency_moves_and_the_target_stays(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    """setup.cfg, then setup.py over it, then [project] over both, then back: the edge is the
    same throughout and its declaration follows the source deciding the field (CM10)."""
    built = _build(workspace, tmp_path, PROVIDER, "cfg-consumer")
    root = tmp_path / "repos"
    edit(root, "cfg-consumer", "setup.py", SETUP + 'setup(install_requires=["fixture-auth"])\n')
    sync_all(built, root)
    assert linked(built, "cfg-consumer")
    assert declared_at(built, "cfg-consumer") == "setup.py:3"
    _equal(built, settings, tmp_path, "setup-py")
    edit(
        root,
        "cfg-consumer",
        "pyproject.toml",
        '[project]\nname = "fixture-client"\ndependencies = ["fixture-auth"]\n',
    )
    sync_all(built, root)
    assert linked(built, "cfg-consumer")
    assert declared_at(built, "cfg-consumer") == "pyproject.toml:3"
    _equal(built, settings, tmp_path, "pyproject")
    (root / "cfg-consumer" / "pyproject.toml").unlink()
    (root / "cfg-consumer" / "setup.py").unlink()
    sync_all(built, root)
    assert linked(built, "cfg-consumer")
    assert declared_at(built, "cfg-consumer") == "setup.cfg:7"
    _equal(built, settings, tmp_path, "setup-cfg")


def test_the_provider_goes_comes_back_and_gains_a_twin(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    built = _build(workspace, tmp_path, PROVIDER, "cfg-consumer")
    root = tmp_path / "repos"
    remove_repo(root, PROVIDER)
    sync_all(built, root)
    assert _refused(built, "cfg-consumer", "module-not-found")
    _equal(built, settings, tmp_path, "gone")
    add_repo(root, PROVIDER, DEPS[PROVIDER])
    sync_all(built, root)
    assert linked(built, "cfg-consumer")
    _equal(built, settings, tmp_path, "back")
    add_repo(root, "twin-auth", DEPS["twin-auth"])
    sync_all(built, root)
    assert _refused(built, "cfg-consumer", "provider-ambiguous")
    _equal(built, settings, tmp_path, "twin")


def test_d19_a_consumer_body_edit_resolves_no_other_repository(
    workspace: Workspace, tmp_path: Path
) -> None:
    """CM12: a body edit is its own work and the uses landing in it, never a repository's."""
    built = _build(workspace, tmp_path, PROVIDER, "cfg-consumer")
    root = tmp_path / "repos"
    append(root, "cfg-consumer", SERVICE, "\n\ndef extra() -> None:\n    pass\n")
    work = sync_all(built, root)
    assert (work.repository, work.package) == (0, 0)
    assert linked(built, "cfg-consumer")
