"""Python uses declared through a root ``requirements.txt``'s local ``-r`` includes (P3d).

A repository stating nothing about packaging declares its dependencies in its root
``requirements.txt`` and in every local file that reaches through ``-r``, whatever the file
is called (:mod:`repoatlas.graph.py_requirements`); the existing package link reads them as
it reads the root's own (:mod:`repoatlas.graph.py_packages`). Each case says what the
consumer's ``service.py`` resolves to and where its declaration is written, or why it
refuses. The fixtures are in :mod:`tests.python_recursive_requirements_corpus`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.graph.py_requirements import (
    INCLUDE_CYCLE,
    INCLUDE_MISSING,
    INCLUDE_REPEATED,
    INCLUDE_UNREAD,
    REQUIREMENTS_FACTS,
    Reached,
    Refused,
    reach,
)
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
from tests.python_recursive_requirements_corpus import AUTH, INCLUDES, OTHER

PROVIDER = "explicit-auth"
MODELS = "src/fixture_auth/models.py"
LEGACY_MODELS = "fixture_auth/models.py"
NAMES = ("User", "make")
GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)


def _build(workspace: Workspace, tmp_path: Path, *names: str) -> Workspace:
    return build(workspace, tmp_path, pick(INCLUDES, *names))


def _edge(workspace: Workspace, consumer: str, provider: str, models: str) -> dict[str, Any]:
    """The consumer's use of ``make``; one edge only."""
    [edge] = [
        e
        for e in edges(workspace)
        if e["source"] == f"{consumer}:{SERVICE}#load"
        and e["target"] == f"{provider}:{models}#make"
    ]
    return edge


def _refused(workspace: Workspace, consumer: str, why: str) -> bool:
    return refusals(workspace, consumer, SERVICE) == dict.fromkeys(NAMES, why)


def _facts_on(workspace: Workspace, consumer: str) -> list[str]:
    """The files of a repository its requirements facts are recorded on."""
    rows = workspace.database.execute(
        "SELECT f.rel_path FROM unresolved_refs u JOIN files f ON f.id = u.file_id "
        "JOIN repositories r ON r.id = u.repo_id WHERE r.slug = ? AND u.target_key = ? "
        "ORDER BY f.rel_path",
        (consumer, REQUIREMENTS_FACTS),
    ).fetchall()
    return [row[0] for row in rows]


@pytest.mark.parametrize(
    ("consumer", "where", "constraint"),
    [
        ("one-include", "requirements/base.txt:1", None),  # R1
        ("nested", "requirements/common.txt:2", ">=1"),  # R2
        ("relative", "requirements/base.txt:1", None),  # R3: from the including file
        ("parent", "common.txt:1", None),  # R4: ../ from requirements/prod.txt
        ("leaf-line", "deps/extra.txt:8", "==2.0"),  # R5: the leaf's own line
        ("cycle", "b.txt:2", None),  # R6
        ("missing-and-root", "requirements.txt:2", None),  # R7: what was read still declares
        ("twice", "requirements/base.txt:1", None),  # R10
        ("include-first", "requirements/base.txt:1", ">=2"),  # R11: pip reads the include first
        ("direct-first", "requirements.txt:1", ">=1"),  # R11: ... or the root's own line
    ],
)
def test_a_requirement_an_include_reaches_declares_the_provider(
    workspace: Workspace, tmp_path: Path, consumer: str, where: str, constraint: str | None
) -> None:
    built = _build(workspace, tmp_path, PROVIDER, consumer)
    assert uses(built, consumer) == service_uses(consumer, PROVIDER, MODELS)
    edge = _edge(built, consumer, PROVIDER, MODELS)
    assert edge["basis"] == "python-dependency"
    assert edge["declared_at"] == edge["crossing"]["declared_at"] == where
    assert edge.get("constraint") == edge["crossing"].get("constraint") == constraint


@pytest.mark.parametrize(
    "consumer",
    [
        "missing-only",  # R7: an include to no file; a file of that name elsewhere is not it
        "escape",  # R8: above the repository, and into another one
        "remote",  # R9: a URL, a variable
        "direct-reference",  # R11: an @ in the include is read with the root's own row
    ],
)
def test_a_declaration_not_read_links_nothing(
    workspace: Workspace, tmp_path: Path, consumer: str
) -> None:
    built = _build(workspace, tmp_path, PROVIDER, consumer, "sibling")
    assert not [
        e
        for e in edges(built)
        if e["source"].startswith(f"{consumer}:") and not e["target"].startswith(f"{consumer}:")
    ]
    assert _refused(built, consumer, "dependency-unread")


def test_a_file_the_root_does_not_reach_declares_nothing(
    workspace: Workspace, tmp_path: Path
) -> None:
    """+U: requirements/dev.txt, LICENSE.txt, requirements-dev.txt: no include names them."""
    built = _build(workspace, tmp_path, PROVIDER, "unreached")
    assert _refused(built, "unreached", "dependency-absent")
    assert _facts_on(built, "unreached") == ["requirements.txt"]


def test_only_the_files_the_root_reaches_hold_requirements_facts(
    workspace: Workspace, tmp_path: Path
) -> None:
    built = _build(workspace, tmp_path, "relative", "cycle", "escape", "missing-only")
    assert _facts_on(built, "relative") == [
        "requirements.txt",
        "requirements/base.txt",
        "requirements/prod.txt",
    ]
    assert _facts_on(built, "cycle") == ["a.txt", "b.txt", "requirements.txt"]
    assert _facts_on(built, "escape") == ["requirements.txt"]
    assert _facts_on(built, "missing-only") == ["requirements.txt"]


def test_r12_a_legacy_requirement_an_include_reaches_names_the_holder(
    workspace: Workspace, tmp_path: Path
) -> None:
    built = _build(workspace, tmp_path, "auth-provider", "legacy-included")
    assert uses(built, "legacy-included") == service_uses(
        "legacy-included", "auth-provider", LEGACY_MODELS
    )
    edge = _edge(built, "legacy-included", "auth-provider", LEGACY_MODELS)
    assert edge["basis"] == "python-requirements-legacy"
    assert edge["declared_at"] == edge["crossing"]["declared_at"] == "requirements/base.txt:2"
    assert edge["crossing"]["requirement"] == "fixture-auth"


def test_r13_an_included_requirement_selects_its_provider_as_any_declaration_does(
    workspace: Workspace, tmp_path: Path
) -> None:
    built = _build(workspace, tmp_path, PROVIDER, "twin-auth", "one-include")
    assert _refused(built, "one-include", "provider-ambiguous")


def test_r14_a_change_reaches_the_consumer_through_the_included_declaration(
    workspace: Workspace, tmp_path: Path
) -> None:
    built = _build(workspace, tmp_path, PROVIDER, "one-include")
    seed = f"sym:{PROVIDER}:{MODELS}:make:5"
    response = CapabilityService(built).impact(ImpactRequest(context=GLOBAL, seed=seed))
    by_entity = {c.entity: c for c in (*response.candidates, *response.tests)}
    load = by_entity[f"sym:one-include:{SERVICE}:load:4"]
    assert load.distance == 1
    step = load.step.crossing
    assert step is not None
    assert (step.package, step.provided_by, step.declared_by) == (
        "pypi:fixture-auth",
        f"{PROVIDER}/pyproject.toml:2",
        "one-include/requirements/base.txt:1",
    )
    [test] = [c for c in response.tests if c.entity.startswith("sym:one-include:tests/")]
    assert (test.distance, test.from_) == (2, load.id)
    [package] = [p for p in response.packages if p.dependent.startswith("one-include:")]
    assert (package.dependent, package.declared_at) == (
        "one-include:.",
        "one-include/requirements/base.txt:1",
    )


# -- the walk (repoatlas.graph.py_requirements) ------------------------------------------


def _reach(files: dict[str, list[list[Any]] | None], indexed: set[str] | None = None):
    """``files``: each file's includes as its facts hold them, or ``None`` when unread."""
    facts = {path: None if found is None else {"includes": found} for path, found in files.items()}
    return reach(facts.get, (indexed or set(files)).__contains__)


def test_the_walk_reads_each_include_in_place_from_the_root() -> None:
    found = _reach(
        {
            "requirements.txt": [[2, "a.txt", None], [5, "c.txt", None]],
            "a.txt": [[3, "b.txt", None]],
            "b.txt": [],
            "c.txt": [],
        }
    )
    assert found.files == (
        Reached("requirements.txt", ()),
        Reached("a.txt", (2,)),
        Reached("b.txt", (2, 3)),
        Reached("c.txt", (5,)),
    )
    assert not found.refused and not found.incomplete


def test_r6_a_cycle_ends_where_the_file_is_already_being_read() -> None:
    found = _reach(
        {
            "requirements.txt": [[1, "a.txt", None]],
            "a.txt": [[1, "b.txt", None]],
            "b.txt": [[1, "a.txt", None], [2, "requirements.txt", None]],
        }
    )
    assert [r.path for r in found.files] == ["requirements.txt", "a.txt", "b.txt"]
    assert found.refused == (
        Refused("b.txt", 1, "a.txt", INCLUDE_CYCLE),
        Refused("b.txt", 2, "requirements.txt", INCLUDE_CYCLE),
    )
    assert not found.incomplete  # every file was read


def test_r10_a_file_is_read_once_however_often_it_is_included() -> None:
    found = _reach(
        {
            "requirements.txt": [[1, "a.txt", None], [2, "b.txt", None]],
            "a.txt": [[1, "c.txt", None]],
            "b.txt": [[1, "c.txt", None]],
            "c.txt": [],
        }
    )
    assert [r.path for r in found.files] == ["requirements.txt", "a.txt", "c.txt", "b.txt"]
    assert found.refused == (Refused("b.txt", 1, "c.txt", INCLUDE_REPEATED),)
    assert not found.incomplete


def test_a_file_not_indexed_or_not_read_leaves_the_set_incomplete() -> None:
    found = _reach(
        {"requirements.txt": [[1, "gone.txt", None], [2, "blob.bin", None]], "blob.bin": None},
        indexed={"requirements.txt", "blob.bin"},
    )
    assert found.refused == (
        Refused("requirements.txt", 1, "gone.txt", INCLUDE_MISSING),
        Refused("requirements.txt", 2, "blob.bin", INCLUDE_UNREAD),
    )
    assert found.incomplete


def test_an_include_refused_where_it_is_written_leaves_the_set_incomplete() -> None:
    found = _reach(
        {"requirements.txt": [[1, "https://example.invalid/r.txt", "include-unsupported"]]}
    )
    assert found.refused == (
        Refused("requirements.txt", 1, "https://example.invalid/r.txt", "include-unsupported"),
    )
    assert found.incomplete


def test_a_root_holding_no_facts_includes_nothing() -> None:
    found = reach({}.get, {"requirements.txt"}.__contains__)
    assert found.files == (Reached("requirements.txt", ()),) and not found.refused


# -- incremental = fresh (R15-R18, +T, +B) --------------------------------------------------


def _equal(workspace: Workspace, settings: Settings, tmp_path: Path, tag: str) -> None:
    assert_equals_fresh(workspace, settings, tmp_path / "repos", tmp_path, tag)


def _declared_at(workspace: Workspace, consumer: str) -> str:
    edge = _edge(workspace, consumer, PROVIDER, MODELS)
    assert edge["crossing"]["declared_at"] == edge["declared_at"]
    return edge["declared_at"]


def test_r15_a_requirement_in_a_reached_file_changed_and_back(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    """The leaf, two includes from the root, is the only file edited: no dependency row
    changes, so what resolves ``nested`` again is what the sync records it reaching (RM7)."""
    built = _build(workspace, tmp_path, PROVIDER, "nested")
    root = tmp_path / "repos"
    edit(root, "nested", "requirements/common.txt", "requests\n" + OTHER)
    work = sync_all(built, root)
    assert work.mode == "incremental"
    assert (work.repositories_invalidated, work.package) == (1, 0), work
    assert work.repository > 0, work
    assert _refused(built, "nested", "dependency-absent")
    _equal(built, settings, tmp_path, "other")
    edit(root, "nested", "requirements/common.txt", "requests\n\nfixture-auth>=1\n")
    sync_all(built, root)
    assert _declared_at(built, "nested") == "requirements/common.txt:3"
    _equal(built, settings, tmp_path, "moved")


def test_r16_the_roots_include_removed_and_back(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    """The root states no requirement before or after: only the include changes."""
    built = _build(workspace, tmp_path, PROVIDER, "one-include")
    root = tmp_path / "repos"
    edit(root, "one-include", "requirements.txt", "# nothing included\n")
    work = sync_all(built, root)
    assert work.repository > 0, work
    assert _refused(built, "one-include", "dependency-absent")
    assert _facts_on(built, "one-include") == ["requirements.txt"]  # RM8: base.txt's are gone
    _equal(built, settings, tmp_path, "removed")
    edit(root, "one-include", "requirements.txt", "-r requirements/base.txt\n")
    sync_all(built, root)
    assert _declared_at(built, "one-include") == "requirements/base.txt:1"
    _equal(built, settings, tmp_path, "back")


def test_r17_a_reached_file_deleted_and_back(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    built = _build(workspace, tmp_path, PROVIDER, "one-include")
    root = tmp_path / "repos"
    base = root / "one-include" / "requirements/base.txt"
    base.unlink()
    sync_all(built, root)
    assert _refused(built, "one-include", "dependency-unread")
    _equal(built, settings, tmp_path, "deleted")
    edit(root, "one-include", "requirements/base.txt", AUTH)
    sync_all(built, root)
    assert _declared_at(built, "one-include") == "requirements/base.txt:1"
    _equal(built, settings, tmp_path, "back")


def test_r18_the_include_pointed_elsewhere_into_a_cycle_and_back(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    built = _build(workspace, tmp_path, PROVIDER, "retarget")
    root = tmp_path / "repos"
    assert _refused(built, "retarget", "dependency-absent")
    edit(root, "retarget", "requirements.txt", "-r requirements/b.txt\n")
    work = sync_all(built, root)
    assert work.repository > 0, work
    assert _declared_at(built, "retarget") == "requirements/b.txt:1"
    assert _facts_on(built, "retarget") == ["requirements.txt", "requirements/b.txt"]
    _equal(built, settings, tmp_path, "b")
    edit(root, "retarget", "requirements/b.txt", "-r a.txt\n" + AUTH)
    edit(root, "retarget", "requirements/a.txt", "-r b.txt\n" + OTHER)
    sync_all(built, root)
    assert _declared_at(built, "retarget") == "requirements/b.txt:2"
    assert _facts_on(built, "retarget") == [
        "requirements.txt",
        "requirements/a.txt",
        "requirements/b.txt",
    ]
    _equal(built, settings, tmp_path, "cycle")
    edit(root, "retarget", "requirements.txt", "-r requirements/a.txt\n")
    edit(root, "retarget", "requirements/a.txt", OTHER)
    sync_all(built, root)
    assert _refused(built, "retarget", "dependency-absent")
    _equal(built, settings, tmp_path, "a")


@pytest.mark.parametrize(
    ("provider", "consumer", "models"),
    [
        (PROVIDER, "one-include", MODELS),  # the project providing fixture-auth
        ("auth-provider", "legacy-included", LEGACY_MODELS),  # the holder of fixture_auth
    ],
)
def test_the_provider_of_a_name_only_included_goes_and_comes_back(
    workspace: Workspace,
    settings: Settings,
    tmp_path: Path,
    provider: str,
    consumer: str,
    models: str,
) -> None:
    """+T: the consumer requires the name in an included file only, so only the recorded
    requirements can say it depends on the provider (RM7b)."""
    built = _build(workspace, tmp_path, provider, consumer)
    root = tmp_path / "repos"
    remove_repo(root, provider)
    sync_all(built, root)
    assert _refused(built, consumer, "module-not-found")
    _equal(built, settings, tmp_path, "gone")
    add_repo(root, provider, INCLUDES[provider])
    sync_all(built, root)
    assert uses(built, consumer) == service_uses(consumer, provider, models)
    _equal(built, settings, tmp_path, "back")


@pytest.mark.parametrize(
    ("consumer", "path", "text"),
    [
        ("one-include", SERVICE, "\n\ndef extra() -> None:\n    pass\n"),  # a body edit
        ("one-include", "requirements/base.txt", "# the same requirements\n"),  # facts unchanged
        ("unreached", "requirements/dev.txt", "requests\n"),  # a file the root does not reach
    ],
)
def test_an_edit_changing_no_declaration_resolves_no_repository(
    workspace: Workspace, settings: Settings, tmp_path: Path, consumer: str, path: str, text: str
) -> None:
    """+B: whatever the file, an edit changing no requirement the root reaches is its own
    work and the uses landing in it, never a repository's."""
    built = _build(workspace, tmp_path, PROVIDER, consumer)
    root = tmp_path / "repos"
    append(root, consumer, path, text)
    work = sync_all(built, root)
    assert work.mode == "incremental"
    assert (work.repository, work.package) == (0, 0), work
    _equal(built, settings, tmp_path, "edit")
