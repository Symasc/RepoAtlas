"""Impact ranking and sections: hubs, units taking turns, tests, unresolved importers (B, E).

A hub is a node with more dependents than an answer can show. It stays a candidate with
its exact fan-out, shows a few representatives spread over units, and the rest are
counted -- never silently dropped. Units take turns so one part of a monorepo cannot
fill an answer. Tests, and importers RepoAtlas cannot narrow to a declaration, are
sectioned apart and capped. Every cut is counted, by reason and by unit.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.capabilities.impact import MAX_TESTS
from repoatlas.capabilities.presentation import present
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.graph.impact import HUB, REPRESENTATIVES
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.impact import ImpactRequest, ImpactResponse
from repoatlas.models.parsing import EdgeType
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace
from tests.conftest import git
from tests.impact_corpus import (
    HUB_CONSUMERS,
    HUB_TESTS,
    HUB_UNITS,
    SYMBOLS,
    build,
    hub_units_repo,
    make_repo,
    verify_steps,
)

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)
BASE = "sym:units:packages/core/src/config.ts:BASE:1"
REQUEST = "sym:units:packages/core/src/request.ts:request:2"
#: Consumers and tests. The barrel only passes ``request`` on: no declaration of it uses
#: the name, and the import graph gives it a file-level import of the defining file only.
FANOUT = len(HUB_UNITS) * HUB_CONSUMERS + HUB_TESTS


def _open(root: Path) -> Workspace:
    settings = Settings.model_validate(
        {
            "storage": {"database": str(root / "state" / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 128},
        }
    )
    return open_workspace(settings=settings, require_root=False)


@pytest.fixture(scope="module")
def indexed(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Workspace]:
    root = tmp_path_factory.mktemp("ranking")
    workspace = _open(root)
    build(workspace, root, {"units": hub_units_repo()})
    yield workspace
    workspace.close()


def impact(workspace: Workspace, seed: str, **options: object) -> ImpactResponse:
    request = ImpactRequest(context=GLOBAL, seed=seed, **options)  # type: ignore[arg-type]
    response = CapabilityService(workspace).impact(request)
    verify_steps(workspace, response)
    return response


def test_a_hub_keeps_its_exact_fanout_and_shows_representatives_across_units(
    indexed: Workspace,
) -> None:
    assert FANOUT > HUB
    response = impact(indexed, BASE)
    [hub] = response.hubs
    request = response.candidates[0]
    assert (hub.candidate, hub.entity, request.entity) == (request.id, REQUEST, REQUEST)
    assert (hub.fanout, hub.shown, hub.collapsed) == (FANOUT, REPRESENTATIVES, FANOUT - 6)
    shown = [c for c in response.candidates if c.from_ == request.id]
    assert len(shown) == REPRESENTATIVES
    # the largest unit first, then the others by name, then round again; no test shown
    assert [c.unit for c in shown] == [
        "units:apps/a",
        "units:apps/a",
        "units:apps/b",
        "units:apps/b",
        "units:apps/c",
        "units:apps/d",
    ]
    assert Counter(c.unit for c in shown) == {
        "units:apps/a": 2,
        "units:apps/b": 2,
        "units:apps/c": 1,
        "units:apps/d": 1,
    }
    assert not response.tests
    assert response.omitted.by_reason == {"hub": FANOUT - REPRESENTATIVES}
    assert hub.units_partial  # more far files than the spread is grouped over
    assert hub.note.startswith("collapsed dependents are not less affected")


def test_a_hub_s_spread_is_exact_when_it_fits(tmp_path: Path) -> None:
    files = hub_units_repo()
    # 4 units x 7 consumers: over the hub bound, under the grouping bound
    files = {k: v for k, v in files.items() if "/__tests__/" not in k}
    files = {k: v for k, v in files.items() if not any(f"/c{i:02d}.ts" in k for i in range(7, 15))}
    workspace = _open(tmp_path)
    try:
        build(workspace, tmp_path, {"units": files})
        [hub] = impact(workspace, BASE).hubs
        assert (hub.fanout, hub.units_partial) == (4 * 7, False)
        assert hub.units == {
            "units:apps/a": 7,
            "units:apps/b": 7,
            "units:apps/c": 7,
            "units:apps/d": 7,
        }
    finally:
        workspace.close()


def test_the_seed_is_never_collapsed_and_units_take_turns(indexed: Workspace) -> None:
    response = impact(indexed, REQUEST, max_candidates=8)
    assert not response.hubs
    assert [c.unit for c in response.candidates] == [
        "units:apps/a",
        "units:apps/a",
        "units:apps/b",
        "units:apps/b",
        "units:apps/c",
        "units:apps/c",
        "units:apps/d",
        "units:apps/d",
    ]
    cut = FANOUT - 8 - MAX_TESTS  # candidates and listed tests
    assert response.omitted.by_reason == {"budget": cut}
    assert sum(response.omitted.by_unit.values()) == cut


def test_tests_are_sectioned_and_capped(indexed: Workspace) -> None:
    response = impact(indexed, REQUEST)
    assert len(response.tests) == MAX_TESTS < HUB_TESTS
    assert all("/__tests__/" in (t.path or "") for t in response.tests)
    assert not any("/__tests__/" in (c.path or "") for c in response.candidates)


def test_a_barrel_that_only_passes_a_name_on_is_reached_from_its_file_not_its_name(
    indexed: Workspace,
) -> None:
    """A name's dependents are the declarations that use it; the barrel uses none.

    The import graph records the barrel's re-export as a file-level import of the file
    that defines the name, never as a reference to the declaration, so a change to the
    declaration lists neither the barrel nor an importer it cannot narrow. A change to
    the whole file reaches it through that import.
    """
    barrel = "file:units:packages/core/src/index.ts"
    by_name = impact(indexed, REQUEST)
    assert by_name.unresolved == ()
    assert barrel not in {c.entity for c in (*by_name.candidates, *by_name.tests)}
    by_file = impact(indexed, "packages/core/src/request.ts", max_candidates=100)
    [reached] = [c for c in by_file.candidates if c.entity == barrel]
    assert (reached.step.edge, reached.basis) == (EdgeType.IMPORTS, "file")
    assert (reached.step.at, reached.step.lines) == ("units/packages/core/src/index.ts", (1, 1))


def test_the_same_facts_indexed_in_another_order_give_the_same_answer(tmp_path: Path) -> None:
    """Row ids are insertion order; no answer may depend on them."""
    one = _open(tmp_path / "one")
    two = _open(tmp_path / "two")
    try:
        build(one, tmp_path / "one", {"symbols": SYMBOLS, "units": hub_units_repo()})
        # the same repositories, the second built in two commits: later files get later ids
        root = tmp_path / "two" / "repos"
        files = hub_units_repo()
        early = {k: v for k, v in files.items() if "/c0" not in k}
        make_repo(root, "units", early)
        make_repo(root, "symbols", SYMBOLS)
        from repoatlas.services.init_service import initialize

        initialize(two, root, config_path=tmp_path / "two" / "config.yaml")
        for relative, content in files.items():
            if relative not in early:
                target = root / "units" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
        git(root / "units", "add", "-A")
        git(root / "units", "commit", "--quiet", "-m", "the rest")
        sync(two, two.resolver().resolve(root / "units"))

        for seed in (BASE, REQUEST, "sym:symbols:src/limits.ts:LIMIT:1"):
            answers = []
            for workspace in (one, two):
                request = ImpactRequest(context=GLOBAL, seed=seed, max_candidates=12)
                payload = present(request, CapabilityService(workspace).impact(request))
                payload.pop("investigation_id")
                payload.pop("freshness")
                answers.append(json.dumps(payload, sort_keys=True))
            assert answers[0] == answers[1], seed
    finally:
        one.close()
        two.close()
