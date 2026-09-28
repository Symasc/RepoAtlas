"""A package export used across many repositories: bounded, and every repository shown (XR9).

``formatRole`` is used by declarations in three repositories, one of them spread over
seven units. Reached from the seed, it is a hub: its exact fan-out and each repository's
share are counted, never fetched, and its representatives take turns by repository
before unit, so no repository fills them all. The same layout is built twice, with ten
times the users per file: what the answer reads must not grow with them.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.impact import ImpactRequest, ImpactResponse
from repoatlas.services.workspace import Workspace, open_workspace
from tests.impact_corpus import build
from tests.xrepo_corpus import HUB_FILES_PER_UNIT, HUB_UNITS_A, hub_repos

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)
SEED = "sym:labels:src/labels.ts:ROLE_LABELS:1"
HUB = "sym:labels:src/labels.ts:formatRole:2"
SMALL, LARGE = 2, 20


def _workspace(root: Path, users: int) -> Workspace:
    settings = Settings.model_validate(
        {
            "storage": {"database": str(root / "state" / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 128},
        }
    )
    workspace = open_workspace(settings=settings, require_root=False)
    return build(workspace, root, hub_repos(users))


@pytest.fixture(scope="module")
def sizes(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[int, Workspace]]:
    built = {
        users: _workspace(tmp_path_factory.mktemp(f"hub{users}"), users) for users in (SMALL, LARGE)
    }
    yield built
    for workspace in built.values():
        workspace.close()


def impact(workspace: Workspace, seed: str, **fields: object) -> ImpactResponse:
    request = ImpactRequest(context=GLOBAL, seed=seed, **fields)  # type: ignore[arg-type]
    return CapabilityService(workspace).impact(request)


def users(per_file: int) -> dict[str, int]:
    return {
        "hub-a": HUB_UNITS_A * HUB_FILES_PER_UNIT * per_file,
        "hub-b": HUB_FILES_PER_UNIT * per_file,
        "hub-c": HUB_FILES_PER_UNIT * per_file,
    }


@pytest.mark.parametrize("per_file", [SMALL, LARGE])
def test_xr9_a_hub_counts_every_repository_and_shows_each_one(
    sizes: dict[int, Workspace], per_file: int
) -> None:
    response = impact(sizes[per_file], SEED)
    [hub] = response.hubs
    # The provider's own barrel only re-exports the hub: no declaration there uses it.
    expected = users(per_file)
    assert (hub.entity, hub.fanout) == (HUB, sum(expected.values()))
    assert hub.repositories == expected
    assert hub.collapsed == hub.fanout - hub.shown > 0
    shown = {c.repository for c in response.candidates if c.from_ == hub.candidate}
    # one repository holds seven of the ten units, and still does not take every place
    assert shown == set(users(per_file))


def test_xr9_what_a_hub_reads_does_not_grow_with_its_users(sizes: dict[int, Workspace]) -> None:
    small, large = (impact(sizes[users], SEED) for users in (SMALL, LARGE))
    assert large.hubs[0].fanout == 10 * small.hubs[0].fanout
    assert large.summary.work == small.summary.work
    assert [c.entity for c in large.candidates] == [c.entity for c in small.candidates]


def test_xr9b_a_short_answer_takes_its_consumers_from_every_repository(
    sizes: dict[int, Workspace],
) -> None:
    response = impact(sizes[SMALL], HUB, max_candidates=6)
    assert len(response.candidates) == 6
    assert {c.repository for c in response.candidates} == set(users(SMALL))
