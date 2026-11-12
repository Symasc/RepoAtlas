"""A package-proven crossing is followed by the bounded walk only when its type is named.

A package link records the proof for a crossing in the edge's own resolution
(``$.crossing.package``). ``walk`` follows such an edge only if its type is in
``evidenced_edges``; a crossing without that evidence follows ``crossing_edges`` as it
always did. The exclusion is made in the neighbour query, before the per-hop fan-out bound,
so a withheld edge cannot use up a place the walk would have given a followed one, and the
walk can count what it withheld (``count_withheld``) without that count touching the walk.

The unit tests plant edges between the file entities of two tiny repositories, the way
``test_impact_xrepo`` n6 does, so each rule is exercised on the real query.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from typing import Any

import pytest
from repoatlas.config.settings import Settings
from repoatlas.graph.traversal import (
    CROSS_REPOSITORY_EDGES,
    EXPANSION_EDGES,
    PACKAGE_CROSSING_EDGES,
    WalkResult,
    expand_from_seeds,
    walk,
)
from repoatlas.models.parsing import EdgeDirection, EdgeType, NeighborOrder
from repoatlas.services.workspace import Workspace, open_workspace
from repoatlas.storage.protocols import NeighborEdge
from tests.impact_corpus import build

IMPORTS, REFERENCES, USES = EdgeType.IMPORTS, EdgeType.REFERENCES, EdgeType.USES

#: What a package link writes into an edge's resolution.
PACKAGE = json.dumps({"crossing": {"package": "npm:@fixture/far", "provider": "far"}})

#: Each test uses names of its own, so the edges one plants are invisible to the others.
HOME_NAMES = (
    "crowd_seed",
    "crowd_in1",
    "crowd_in2",
    "count_seed",
    "count_in",
    "hop_seed",
    "hop_a",
    "hop_b",
    "dedup_seed",
    "uses_seed",
    "store_seed",
    "named_seed",
)
FAR_NAMES = (
    "crowd_p1",
    "crowd_p2",
    "crowd_p3",
    "crowd_plain",
    "count_p1",
    "count_p2",
    "count_p3",
    "hop_f1",
    "hop_f2",
    "dedup_x",
    "uses_far",
    "store_package",
    "store_garbled",
    "named_p1",
    "named_p2",
    "named_p3",
)


def _files(names: Sequence[str]) -> dict[str, str]:
    # Comments only: a file with no declarations has no edges of its own to disturb a walk.
    return {f"src/{name}.ts": f"// {name}\n" for name in names}


def home(name: str) -> str:
    return f"file:home:src/{name}.ts"


def far(name: str) -> str:
    return f"file:far:src/{name}.ts"


@pytest.fixture(scope="module")
def workspace(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Workspace]:
    root = tmp_path_factory.mktemp("package-crossing-policy")
    settings = Settings.model_validate({"storage": {"database": str(root / "state" / "atlas.db")}})
    opened = open_workspace(settings=settings, require_root=False)
    build(opened, root, {"home": _files(HOME_NAMES), "far": _files(FAR_NAMES)})
    yield opened
    opened.close()


def entity_id(ws: Workspace, key: str) -> int:
    row = ws.database.execute("SELECT id FROM entities WHERE key = ?", (key,)).fetchone()
    assert row is not None, key
    return int(row[0])


def keys_of(ws: Workspace, ids: Sequence[int]) -> set[str]:
    return {
        row[0]
        for row in ws.database.execute(
            f"SELECT key FROM entities WHERE id IN ({','.join('?' * len(ids))})",  # noqa: S608 - placeholders only
            tuple(ids),
        ).fetchall()
    }


_lines = iter(range(1, 10_000))


def plant(
    ws: Workspace,
    src: str,
    dst: str,
    kind: EdgeType = IMPORTS,
    *,
    confidence: float = 1.0,
    resolution: str | None = None,
) -> int:
    """Store one edge between two file entities, on a line of its own; return its id."""
    source = ws.database.execute(
        "SELECT id, repo_id, file_id FROM entities WHERE key = ?", (src,)
    ).fetchone()
    target = entity_id(ws, dst)
    with ws.database.transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO edges (src_id, dst_id, type, confidence, repo_id, file_id,"
            " line_start, line_end, parser, resolution)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'linker:global', ?)",
            (
                source["id"],
                target,
                kind.value,
                confidence,
                source["repo_id"],
                source["file_id"],
                next(_lines),
                next(_lines),
                resolution,
            ),
        )
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)


def run(
    ws: Workspace, seed: str, *, fanout: int = 5, hops: int = 2, **fields: object
) -> WalkResult:
    return walk(
        ws.knowledge,
        [entity_id(ws, seed)],
        hops=hops,
        fanout=fanout,
        seed_limit=5,
        **fields,  # type: ignore[arg-type]
    )


# -- the constants -------------------------------------------------------------------


def test_the_package_crossing_set_names_exactly_imports_references_and_calls() -> None:
    assert {IMPORTS, REFERENCES, EdgeType.CALLS} == PACKAGE_CROSSING_EDGES
    assert EdgeType.USES not in PACKAGE_CROSSING_EDGES
    assert EdgeType.EXPORTS not in PACKAGE_CROSSING_EDGES
    # Search expands through the first two only; it never expands through a call.
    assert PACKAGE_CROSSING_EDGES & set(EXPANSION_EDGES) == {IMPORTS, REFERENCES}


# -- the rule, on stub edges ---------------------------------------------------------


class _Knowledge:
    """A store that returns the same two crossing edges from node 1, and records the asks."""

    def __init__(self) -> None:
        self.package_crossings: list[object] = []
        self.edges = [
            NeighborEdge(
                src_id=1, dst_id=2, type=REFERENCES, src_repo_id=1, dst_repo_id=2, evidenced=True
            ),
            NeighborEdge(
                src_id=1, dst_id=3, type=REFERENCES, src_repo_id=1, dst_repo_id=3, evidenced=False
            ),
        ]

    def expand_neighbors(
        self,
        entity_ids: Sequence[int],
        edge_types: Sequence[EdgeType] | None = None,
        limit: int = 200,
        direction: EdgeDirection = EdgeDirection.ANY,
        order: NeighborOrder = NeighborOrder.CONFIDENCE,
        package_crossings: object = None,
    ) -> list[NeighborEdge]:
        self.package_crossings.append(package_crossings)
        return list(self.edges) if 1 in entity_ids else []


def test_a_package_evidenced_crossing_is_followed_only_if_its_type_is_named() -> None:
    """``REFERENCES`` is a crossing type, and still the evidenced one is not followed."""
    assert EdgeType.REFERENCES in CROSS_REPOSITORY_EDGES
    default = _Knowledge()
    followed = walk(default, [1], hops=1, fanout=5, seed_limit=1)  # type: ignore[arg-type]
    assert followed.reached == [3]
    assert default.package_crossings == [frozenset()]

    asked = _Knowledge()
    both = walk(
        asked,  # type: ignore[arg-type]
        [1],
        hops=1,
        fanout=5,
        seed_limit=1,
        evidenced_edges=PACKAGE_CROSSING_EDGES,
    )
    assert both.reached == [2, 3]
    assert asked.package_crossings == [PACKAGE_CROSSING_EDGES]


def test_an_evidenced_crossing_ignores_the_unconditional_crossing_set() -> None:
    """Evidence decides for the edge that has it, and the type set for the one that has not."""
    knowledge = _Knowledge()
    result = walk(
        knowledge,  # type: ignore[arg-type]
        [1],
        hops=1,
        fanout=5,
        seed_limit=1,
        crossing_edges=frozenset(),
        evidenced_edges=PACKAGE_CROSSING_EDGES,
    )
    assert result.reached == [2]


# -- the rule and the fan-out bound, on the real query -------------------------------


def test_a_withheld_crossing_does_not_use_up_the_fan_out(workspace: Workspace) -> None:
    """Three package crossings outrank everything by confidence; the cut must not see them."""
    seed = home("crowd_seed")
    for name in ("crowd_p1", "crowd_p2", "crowd_p3"):
        plant(workspace, seed, far(name), IMPORTS, resolution=PACKAGE)
    for name in ("crowd_in1", "crowd_in2"):
        plant(workspace, seed, home(name), IMPORTS, confidence=0.5)
    # A crossing with no evidence at all keeps following the crossing set.
    plant(workspace, seed, far("crowd_plain"), REFERENCES, confidence=0.5)
    seeds = [entity_id(workspace, seed)]

    default = expand_from_seeds(workspace.knowledge, seeds, hops=2, fanout=3, seed_limit=5)
    assert keys_of(workspace, default.reached) == {
        home("crowd_in1"),
        home("crowd_in2"),
        far("crowd_plain"),
    }

    asked = expand_from_seeds(
        workspace.knowledge, seeds, hops=2, fanout=3, seed_limit=5, package_crossings=True
    )
    assert keys_of(workspace, asked.reached) == {
        far("crowd_p1"),
        far("crowd_p2"),
        far("crowd_p3"),
    }


def test_the_neighbour_query_leaves_out_a_crossing_only_when_asked_to(
    workspace: Workspace,
) -> None:
    seed = entity_id(workspace, home("store_seed"))
    package = plant(workspace, home("store_seed"), far("store_package"), resolution=PACKAGE)
    # Not JSON: it records no evidence, and reading it must not fail the query.
    garbled = plant(workspace, home("store_seed"), far("store_garbled"), resolution="not json")
    knowledge = workspace.knowledge

    def reached(
        package_crossings: frozenset[EdgeType] | None = None,
    ) -> dict[int, bool]:
        edges = knowledge.expand_neighbors(
            [seed], [IMPORTS], limit=10, package_crossings=package_crossings
        )
        return {edge.dst_id: edge.evidenced for edge in edges}

    package_id = entity_id(workspace, far("store_package"))
    garbled_id = entity_id(workspace, far("store_garbled"))
    # Direct callers pass nothing and see every edge; the flag is set for any type.
    assert reached() == {package_id: True, garbled_id: False}
    assert reached(package_crossings=frozenset()) == {garbled_id: False}
    assert reached(package_crossings=frozenset({EdgeType.CALLS})) == {garbled_id: False}
    assert reached(package_crossings=frozenset({IMPORTS})) == {package_id: True, garbled_id: False}

    ids = knowledge.package_crossing_ids
    assert ids([seed], [IMPORTS], EdgeDirection.OUT, frozenset()) == [package]
    assert ids([seed], [IMPORTS], EdgeDirection.ANY, frozenset({EdgeType.CALLS})) == [package]
    assert ids([seed], [IMPORTS], EdgeDirection.OUT, frozenset({IMPORTS})) == []
    # Only edges that touch the nodes asked about, in the direction asked, of a named type.
    assert ids([seed], [IMPORTS], EdgeDirection.IN, frozenset()) == []
    assert ids([seed], [REFERENCES], EdgeDirection.OUT, frozenset()) == []
    assert ids([seed], [], EdgeDirection.OUT, frozenset()) == []
    assert ids([], [IMPORTS], EdgeDirection.OUT, frozenset()) == []
    assert garbled not in ids([seed], [IMPORTS], EdgeDirection.ANY, frozenset())


# -- the count of what was withheld --------------------------------------------------


def test_withheld_is_every_package_crossing_however_the_cut_falls(workspace: Workspace) -> None:
    seed = home("count_seed")
    for name in ("count_p1", "count_p2", "count_p3"):
        plant(workspace, seed, far(name), IMPORTS, resolution=PACKAGE)
    plant(workspace, seed, home("count_in"), IMPORTS, confidence=0.5)

    # A fan-out of two: the three crossings would have filled it and more.
    counted = run(workspace, seed, fanout=2, count_withheld=True)
    assert counted.withheld == 3
    assert keys_of(workspace, counted.reached) == {home("count_in")}

    # Counting is asked for; the walk itself is the same without it.
    plain = run(workspace, seed, fanout=2)
    assert plain.withheld == 0
    assert (plain.reached, plain.steps, plain.bounded) == (
        counted.reached,
        counted.steps,
        counted.bounded,
    )

    through_retrieval = expand_from_seeds(
        workspace.knowledge,
        [entity_id(workspace, seed)],
        hops=2,
        fanout=2,
        seed_limit=5,
        count_withheld=True,
    )
    assert through_retrieval.withheld == 3


def test_nothing_is_withheld_when_the_type_is_named(workspace: Workspace) -> None:
    seed = home("named_seed")
    targets = [far(name) for name in ("named_p1", "named_p2", "named_p3")]
    for target in targets:
        plant(workspace, seed, target, IMPORTS, resolution=PACKAGE)

    counted = run(
        workspace, seed, fanout=5, evidenced_edges=PACKAGE_CROSSING_EDGES, count_withheld=True
    )
    assert counted.withheld == 0
    assert keys_of(workspace, counted.reached) == set(targets)
    # And the same seed, not asked: all three are withheld, none reached.
    default = run(workspace, seed, fanout=5, count_withheld=True)
    assert default.reached == [] and default.withheld == 3


def test_a_crossing_counts_where_the_walk_expanded_and_not_where_it_only_arrived(
    workspace: Workspace,
) -> None:
    """``hop_a`` is expanded at hop 2, ``hop_b`` is only reached by it."""
    plant(workspace, home("hop_seed"), home("hop_a"), IMPORTS, confidence=0.9)
    plant(workspace, home("hop_a"), home("hop_b"), IMPORTS, confidence=0.9)
    plant(workspace, home("hop_a"), far("hop_f1"), IMPORTS, resolution=PACKAGE)
    plant(workspace, home("hop_b"), far("hop_f2"), IMPORTS, resolution=PACKAGE)

    two = run(workspace, home("hop_seed"), hops=2, count_withheld=True)
    assert keys_of(workspace, two.reached) == {home("hop_a"), home("hop_b")}
    assert two.withheld == 1  # hop_a's; hop_b was never expanded

    one = run(workspace, home("hop_seed"), hops=1, count_withheld=True)
    assert one.withheld == 0  # hop_a was only reached


def test_one_crossing_is_counted_once_however_many_hops_touch_it(workspace: Workspace) -> None:
    """``dedup_x`` is followed by a plain crossing, and a package crossing ends there too."""
    seed = home("dedup_seed")
    plant(workspace, seed, far("dedup_x"), IMPORTS)
    plant(workspace, seed, far("dedup_x"), IMPORTS, resolution=PACKAGE)

    result = run(workspace, seed, hops=2, count_withheld=True)
    assert keys_of(workspace, result.reached) == {far("dedup_x")}
    # Hop 1 touches the package edge at the seed, hop 2 at its far end: the same edge.
    assert result.withheld == 1


def test_a_use_is_never_counted_because_the_walk_never_asks_for_one(workspace: Workspace) -> None:
    seed = home("uses_seed")
    plant(workspace, seed, far("uses_far"), USES, resolution=PACKAGE)

    result = run(workspace, seed, count_withheld=True)
    assert result.reached == [] and result.withheld == 0
    asked = run(workspace, seed, evidenced_edges=PACKAGE_CROSSING_EDGES, count_withheld=True)
    assert asked.reached == [] and asked.withheld == 0


def test_a_walk_without_seeds_withholds_nothing() -> None:
    # No query is made at all: the walk returns before it reads the store.
    nowhere: Any = object()
    empty = walk(nowhere, [], hops=2, fanout=5, seed_limit=5, count_withheld=True)
    assert empty.withheld == 0 and empty.reached == []
