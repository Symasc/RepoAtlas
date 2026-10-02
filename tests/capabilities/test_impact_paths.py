"""Impact paths: exact expected sets for a directory and for repositories.

Fixture D is a directory whose module is imported from outside it; E is two repositories
that share an environment variable and a function name.
Every answer here is checked three more ways: each step against the index
(:func:`tests.impact_corpus.verify_steps`), each path closed, and every run the same.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from repoatlas.capabilities.presentation import present
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.graph import impact as walk_module
from repoatlas.graph.neighbourhood import WorkLimits
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.impact import ImpactRequest, ImpactResponse
from repoatlas.services.workspace import Workspace, open_workspace
from tests.impact_corpus import APA, BET, SYMBOLS, TREE, build, verify_steps

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)


@pytest.fixture(scope="module")
def indexed(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Workspace]:
    root = tmp_path_factory.mktemp("paths")
    settings = Settings.model_validate(
        {
            "storage": {"database": str(root / "state" / "atlas.db")},
        }
    )
    workspace = open_workspace(settings=settings, require_root=False)
    repos = {"tree": TREE, "apa": APA, "bet": BET, "symbols": SYMBOLS}
    build(workspace, root, repos)
    yield workspace
    workspace.close()


def impact(workspace: Workspace, seed: str, **options: object) -> ImpactResponse:
    request = ImpactRequest(context=GLOBAL, seed=seed, **options)  # type: ignore[arg-type]
    response = CapabilityService(workspace).impact(request)
    verify_steps(workspace, response)
    returned = [*response.candidates, *response.tests]
    ids = sorted(c.id for c in returned)
    assert ids == list(range(1, len(ids) + 1))
    # closed: every predecessor is returned, as a candidate, and ranks before its successor
    candidate_ids = {c.id for c in response.candidates}
    assert all(c.from_ < c.id and (c.from_ == 0 or c.from_ in candidate_ids) for c in returned)
    return response


def shape(
    response: ImpactResponse, section: str = "candidates"
) -> list[tuple[int, str, str, str, str]]:
    return [
        (c.from_, c.entity, c.role.value, c.basis, str(c.step.edge or c.step.derived))
        for c in getattr(response, section)
    ]


def test_e_one_variable_in_two_repositories_does_not_join_them(indexed: Workspace) -> None:
    response = impact(indexed, "sym:apa:Dockerfile:api:1")
    assert shape(response) == [
        (0, "env:DATABASE_URL", "dependent", "exact", "EXPORTS"),
        (1, "sym:apa:src/db.ts:connect:1", "dependent", "exact", "READS"),
    ]
    # the variable itself, changed from no repository, reaches every reader and setter
    everywhere = {c.entity for c in impact(indexed, "env:DATABASE_URL").candidates}
    assert {
        "sym:apa:src/db.ts:connect:1",
        "sym:bet:src/db.ts:migrate:1",
        "sym:apa:Dockerfile:api:1",
    } <= everywhere


def test_e_one_function_name_in_two_repositories_does_not_join_them(indexed: Workspace) -> None:
    response = impact(indexed, "sym:apa:src/format.ts:formatDate:1")
    assert shape(response) == [(0, "sym:apa:src/report.ts:report:2", "dependent", "exact", "CALLS")]


def test_a_repository_scope_confines_a_walk_from_a_shared_seed(indexed: Workspace) -> None:
    only_bet = CapabilityContext(scope=CapabilityScope.REPOSITORY, repository="bet")
    response = CapabilityService(indexed).impact(
        ImpactRequest(context=only_bet, seed="env:DATABASE_URL")
    )
    assert {c.entity for c in response.candidates} == {"sym:bet:src/db.ts:migrate:1"}


def test_a_symbol_seed_never_falls_back_to_its_file(indexed: Workspace) -> None:
    response = impact(indexed, "sym:symbols:src/limits.ts:unrelated:6")
    assert shape(response) == [
        (0, "sym:symbols:src/other.ts:other:2", "dependent", "exact", "USES"),
    ]


def test_the_same_request_answers_the_same_every_time(indexed: Workspace) -> None:
    seed = "sym:symbols:src/limits.ts:LIMIT:1"
    request = ImpactRequest(context=GLOBAL, seed=seed)
    answers = []
    for _ in range(3):
        payload = present(request, CapabilityService(indexed).impact(request))
        payload.pop("investigation_id")
        for repo in payload["freshness"]["repositories"]:
            repo.pop("last_indexed_at", None)
        answers.append(json.dumps(payload, sort_keys=True))
    assert len(set(answers)) == 1


def test_the_candidate_cap_keeps_paths_closed_and_counts_the_rest(indexed: Workspace) -> None:
    full = impact(indexed, "sym:symbols:src/limits.ts:LIMIT:1")
    capped = impact(indexed, "sym:symbols:src/limits.ts:LIMIT:1", max_candidates=3)
    assert [c.entity for c in capped.candidates] == [c.entity for c in full.candidates[:3]]
    assert capped.omitted.by_reason == {"budget": len(full.candidates) - 3}
    assert sum(capped.omitted.by_unit.values()) == len(full.candidates) - 3
    assert capped.summary.complete  # a cap on what is returned is not an unfinished walk


def test_a_spent_budget_stops_the_walk_and_says_so(
    indexed: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        walk_module.ImpactWalk,
        "__init__",
        _with_limits(walk_module.ImpactWalk.__init__, WorkLimits(expansions=2)),
    )
    response = impact(indexed, "sym:symbols:src/limits.ts:LIMIT:1")
    assert not response.summary.complete
    assert response.summary.work.exhausted == "expansions"
    assert response.summary.work.expanded == 2
    assert response.omitted.by_reason.get("work", 0) > 0


def _with_limits(init, limits: WorkLimits):  # type: ignore[no-untyped-def]
    def patched(self, knowledge, repo_slugs, **options):  # type: ignore[no-untyped-def]
        options["limits"] = limits
        init(self, knowledge, repo_slugs, **options)

    return patched


def test_a_directory_seed_walks_from_everything_beneath_it(indexed: Workspace) -> None:
    response = impact(indexed, "src/gen")
    assert response.seed.granularity is not None and response.seed.granularity.value == "subtree"
    # Everything is reached through a member (a directory has no edges of its own), and a
    # member is returned only as a step on some candidate's path.
    assert shape(response) == [
        (0, "file:tree:src/gen/api.ts", "member", "containment", "member"),
        (0, "sym:tree:src/gen/api.ts:endpoints:1", "member", "containment", "member"),
        (2, "sym:tree:src/client.ts:listPath:2", "dependent", "containment", "USES"),
        (1, "file:tree:src/client.ts", "dependent", "file", "IMPORTS"),
    ]


def test_compact_leaves_out_empty_fields_and_full_adds_resolutions(indexed: Workspace) -> None:
    seed = "sym:symbols:src/limits.ts:LIMIT:1"
    compact_request = ImpactRequest(context=GLOBAL, seed=seed)
    full_request = ImpactRequest(context=GLOBAL, seed=seed, presentation="full")  # type: ignore[arg-type]
    service = CapabilityService(indexed)
    compact = present(compact_request, service.impact(compact_request))
    full = present(full_request, service.impact(full_request))
    sections = {
        "investigation_id",
        "context",
        "seed",
        "candidates",
        "tests",
        "packages",
        "hubs",
        "unresolved",
        "omitted",
        "summary",
        "freshness",
    }
    assert set(compact) == sections == set(full)
    for candidate in compact["candidates"]:
        assert "from" in candidate and "from_" not in candidate
        assert not any(value in ([], {}, None) for value in candidate.values())
        assert "resolution" not in candidate["step"]
    assert all("from" in c for c in full["candidates"])
    stepped = [c for c in full["candidates"] if "edge" in c["step"] and c["step"]["edge"]]
    assert stepped and all(c["step"]["resolution"] for c in stepped)
    assert len(json.dumps(compact)) < len(json.dumps(full))
