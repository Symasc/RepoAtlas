"""An impact seed names exactly one thing, or nothing is followed (fixture F).

Every later step of an impact answer is a statement about the seed, so a seed that
could mean two things is refused with both listed, and one that means nothing says
why -- including when it means something only outside the requested scope.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.capabilities.impact import NOT_INDEXED, OUT_OF_SCOPE, POLICY
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.impact import (
    MAX_SEED_CANDIDATES,
    Granularity,
    ImpactRequest,
    ImpactSeed,
    SeedBasis,
    SeedStatus,
)
from repoatlas.models.parsing import EntityType
from repoatlas.services.workspace import Workspace, open_workspace
from tests.impact_corpus import ELSEWHERE, SEEDS, build

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)
SEEDS_ONLY = CapabilityContext(scope=CapabilityScope.REPOSITORY, repository="seeds")


@pytest.fixture(scope="module")
def indexed(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Workspace]:
    root = tmp_path_factory.mktemp("seeds")
    settings = Settings.model_validate(
        {
            "storage": {"database": str(root / "state" / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 128},
        }
    )
    workspace = open_workspace(settings=settings, require_root=False)
    build(workspace, root, {"seeds": SEEDS, "elsewhere": ELSEWHERE})
    yield workspace
    workspace.close()


def seed(workspace: Workspace, term: str, context: CapabilityContext = GLOBAL) -> ImpactSeed:
    response = CapabilityService(workspace).impact(ImpactRequest(context=context, seed=term))
    assert response.summary.policy == POLICY
    if response.seed.status is not SeedStatus.RESOLVED:
        # nothing is followed from a seed that is not one thing
        assert response.candidates == () and response.tests == () and response.packages == ()
        assert response.summary.complete
    return response.seed


def test_a_name_two_declarations_share_is_refused_with_both(indexed: Workspace) -> None:
    found = seed(indexed, "formatDate")
    assert found.status is SeedStatus.AMBIGUOUS and found.total == 2
    assert [(c.path, c.lines) for c in found.candidates] == [
        ("src/format.ts", (1, 3)),
        ("src/legacy/format.ts", (1, 3)),
    ]


def test_one_key_carried_by_two_rows_is_refused_never_picked(indexed: Workspace) -> None:
    found = seed(indexed, "sym:seeds:src/limits.ts:limits:1")
    assert found.status is SeedStatus.AMBIGUOUS and found.total == 2
    assert {c.type for c in found.candidates} == {EntityType.MODULE, EntityType.FUNCTION}


def test_a_name_behind_two_wholesale_re_exports_is_refused(indexed: Workspace) -> None:
    found = seed(indexed, "shared")
    assert found.status is SeedStatus.AMBIGUOUS
    assert {c.path for c in found.candidates} == {"src/stars-a.ts", "src/stars-b.ts"}


def test_an_environment_variable_and_a_constant_spelled_alike_are_two_things(
    indexed: Workspace,
) -> None:
    found = seed(indexed, "DATABASE_URL")
    assert found.status is SeedStatus.AMBIGUOUS
    assert {c.entity for c in found.candidates} == {
        "env:DATABASE_URL",
        "sym:seeds:src/db.ts:DATABASE_URL:1",
    }
    exact = seed(indexed, "env:DATABASE_URL")
    assert (exact.status, exact.basis, exact.granularity, exact.type) == (
        SeedStatus.RESOLVED,
        SeedBasis.KEY,
        Granularity.KEY,
        EntityType.ENV_VAR,
    )
    assert exact.unit == "env:DATABASE_URL" and exact.repository is None


def test_an_unknown_seed_says_nothing_is_indexed_under_it(indexed: Workspace) -> None:
    found = seed(indexed, "noSuchThingAnywhere")
    assert (found.status, found.reason, found.candidates) == (
        SeedStatus.UNRESOLVED,
        NOT_INDEXED,
        (),
    )


def test_a_seed_outside_the_scope_says_so_rather_than_answering_from_there(
    indexed: Workspace,
) -> None:
    assert seed(indexed, "onlyThere").status is SeedStatus.RESOLVED
    confined = seed(indexed, "onlyThere", SEEDS_ONLY)
    assert (confined.status, confined.reason) == (SeedStatus.UNRESOLVED, OUT_OF_SCOPE)


def test_a_declaration_resolves_with_its_location_unit_and_members(indexed: Workspace) -> None:
    found = seed(indexed, "Store", SEEDS_ONLY)
    assert (found.status, found.basis, found.granularity) == (
        SeedStatus.RESOLVED,
        SeedBasis.NAME,
        Granularity.SYMBOL,
    )
    assert (found.entity, found.path, found.lines, found.unit, found.members) == (
        "sym:seeds:src/store.ts:Store:1",
        "src/store.ts",
        (1, 8),
        "seeds:.",
        2,
    )
    assert seed(indexed, "Store.get").entity == "sym:seeds:src/store.ts:Store.get:2"


def test_a_file_path_resolves_to_the_file_with_its_declarations(indexed: Workspace) -> None:
    found = seed(indexed, "src/store.ts")
    assert (found.status, found.basis, found.granularity, found.members) == (
        SeedStatus.RESOLVED,
        SeedBasis.PATH,
        Granularity.FILE,
        3,
    )
    # a basename names no one file
    assert seed(indexed, "format.ts").status is not SeedStatus.RESOLVED


def test_a_directory_resolves_to_everything_beneath_it(indexed: Workspace) -> None:
    found = seed(indexed, "src/legacy/")
    assert (found.status, found.basis, found.granularity, found.members, found.entity) == (
        SeedStatus.RESOLVED,
        SeedBasis.DIRECTORY,
        Granularity.SUBTREE,
        2,
        "seeds:src/legacy/",
    )
    # the same path in two repositories is two things
    both = seed(indexed, "src")
    assert both.status is SeedStatus.AMBIGUOUS
    assert [c.repository for c in both.candidates] == ["elsewhere", "seeds"]
    # each is listed as it can be asked for again, and only within scope
    chosen = seed(indexed, both.candidates[1].entity)
    assert (chosen.status, chosen.basis, chosen.entity, chosen.members) == (
        SeedStatus.RESOLVED,
        SeedBasis.DIRECTORY,
        "seeds:src/",
        seed(indexed, "src", SEEDS_ONLY).members,
    )
    assert seed(indexed, "elsewhere:src/", SEEDS_ONLY).status is SeedStatus.UNRESOLVED


def test_a_configuration_key_resolves_by_its_qualified_name(indexed: Workspace) -> None:
    found = seed(indexed, "jobs.build")
    assert (found.status, found.granularity, found.type, found.members) == (
        SeedStatus.RESOLVED,
        Granularity.KEY,
        EntityType.CONFIG,
        3,
    )


def test_an_ambiguous_seed_lists_at_most_eight_and_counts_them_all(
    tmp_path: Path, workspace: Workspace
) -> None:
    files = {"package.json": '{"name": "many", "private": true}'}
    for i in range(12):
        files[f"src/m{i:02d}.ts"] = "export const twin = 1;\n"
    build(workspace, tmp_path, {"many": files})
    found = seed(workspace, "twin")
    assert found.total == 12 and len(found.candidates) == MAX_SEED_CANDIDATES
    assert [c.path for c in found.candidates] == [f"src/m{i:02d}.ts" for i in range(8)]
