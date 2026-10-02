"""Graph traversal has exactly one definition (ARCHITECTURE.md §10 stage 4).

The walk, the edge whitelist it may follow and the rule that governs leaving a
repository were private to the retrieval funnel. They are now
``repoatlas.graph.traversal``, and retrieval is *a* caller rather than the owner.

These tests hold that seam open. A second copy of the edge sets appearing back in
the retriever would not fail anything else — both copies would be correct on the
day they were written, and would then drift — so the absence is asserted here.
"""

from __future__ import annotations

from pathlib import Path

from repoatlas.graph import keys
from repoatlas.graph.traversal import (
    CROSS_REPOSITORY_EDGES,
    EXPANSION_EDGES,
    expand_from_seeds,
)
from repoatlas.models.parsing import EdgeType
from repoatlas.retrieval import retriever as retriever_module
from repoatlas.services.retrieval_service import locate
from repoatlas.services.workspace import Workspace


def _seed(workspace: Workspace, key: str) -> int:
    """The entity id behind a natural key, via the exact lookup."""
    resolved = workspace.knowledge.get_entities_by_key([key])
    assert len(resolved) == 1, f"{key} resolved to {len(resolved)} rows"
    return resolved[0].entity_id


# -- the seam ----------------------------------------------------------------


def test_the_retriever_holds_no_second_copy_of_the_edge_sets() -> None:
    """One definition, or the two drift apart while both look right."""
    assert not hasattr(retriever_module, "_EXPANSION_EDGES")
    assert not hasattr(retriever_module, "_CROSS_REPOSITORY_EDGES")


def test_retrieval_reaches_the_walk_through_the_shared_module(
    initialized: Workspace, workspace_root: Path, monkeypatch
) -> None:
    calls: list[int] = []
    original = retriever_module.expand_from_seeds

    def counting(knowledge, seeds, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(len(seeds))
        return original(knowledge, seeds, **kwargs)

    monkeypatch.setattr(retriever_module, "expand_from_seeds", counting)
    context = initialized.resolver().resolve(workspace_root)
    locate(initialized, "Where is SERVICE_HOST configured?", context)
    assert calls, "the funnel did not go through graph.traversal"


# -- the policy the walk owns --------------------------------------------------


def test_containment_edges_are_not_traversable() -> None:
    """Expansion follows relationships. Containment fans out over an inventory."""
    assert EdgeType.DEFINES not in EXPANSION_EDGES
    assert EdgeType.CONTAINS not in EXPANSION_EDGES


def test_every_cross_repository_edge_is_also_an_expansion_edge() -> None:
    """A traversal cannot leave a repository by an edge it may not follow at all."""
    assert set(EXPANSION_EDGES) >= CROSS_REPOSITORY_EDGES


def test_name_shaped_edges_may_not_cross_a_repository_boundary() -> None:
    """``CALLS`` and ``DEFINES`` mean something inside one repository only.

    Following either across a boundary would let a traversal wander between
    repositories on nothing stronger than a shared symbol name (V1_SPEC §9).
    """
    assert EdgeType.CALLS not in CROSS_REPOSITORY_EDGES
    assert EdgeType.DEFINES not in CROSS_REPOSITORY_EDGES
    assert EdgeType.GENERATES in CROSS_REPOSITORY_EDGES


# -- the bounds ------------------------------------------------------------------


def test_an_environment_variable_hub_reaches_the_files_on_both_sides(
    initialized: Workspace,
) -> None:
    """Two hops is the useful minimum: hop one is the hub, hop two the files."""
    seed = _seed(initialized, keys.env_key("SERVICE_HOST"))
    walk = expand_from_seeds(initialized.knowledge, [seed], hops=2, fanout=25, seed_limit=20)
    assert walk.reached
    assert walk.visited > 1
    described = initialized.knowledge.describe_entities(walk.reached)
    assert {row.repository for row in described} >= {"python-service"}


def test_zero_hops_reaches_nothing_but_still_counts_the_seeds(
    initialized: Workspace,
) -> None:
    seed = _seed(initialized, keys.env_key("SERVICE_HOST"))
    walk = expand_from_seeds(initialized.knowledge, [seed], hops=0, fanout=25, seed_limit=20)
    assert walk.reached == []
    assert walk.visited == 1


def test_the_seed_limit_bounds_the_starting_frontier(initialized: Workspace) -> None:
    seed = _seed(initialized, keys.env_key("SERVICE_HOST"))
    walk = expand_from_seeds(initialized.knowledge, [seed], hops=2, fanout=25, seed_limit=0)
    assert walk.reached == []
    assert walk.visited == 0


def test_no_seeds_is_not_a_walk(initialized: Workspace) -> None:
    walk = expand_from_seeds(initialized.knowledge, [], hops=2, fanout=25, seed_limit=20)
    assert walk.reached == []
    assert walk.reasons == {}
    assert walk.visited == 0


def test_every_reached_entity_carries_the_edge_that_reached_it(
    initialized: Workspace,
) -> None:
    """Order is the proximity signal fusion reads, so hop one precedes hop two."""
    seed = _seed(initialized, keys.env_key("SERVICE_HOST"))
    walk = expand_from_seeds(initialized.knowledge, [seed], hops=2, fanout=25, seed_limit=20)
    assert set(walk.reasons) == set(walk.reached)
    hops = [int(walk.reasons[entity].rsplit(" ", 1)[1].rstrip(")")) for entity in walk.reached]
    assert hops == sorted(hops)
    assert seed not in walk.reached
