"""Bounded graph traversal (ARCHITECTURE.md §10 stage 4).

Extracted from the retrieval funnel so that the walk, its edge whitelist and its
cross-repository rule have one definition. Retrieval was its first caller and
remains its only one; the extraction changes no bound, no order and no reason
string, because rank fusion reads traversal order as a proximity signal.

The two edge sets are the policy. Everything else here is the bound.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from repoatlas.models.parsing import EdgeType
from repoatlas.storage.sqlite.stores import SqliteKnowledgeStore

#: Edge types graph expansion may traverse.
#:
#: These are *relationship* edges. ``DEFINES`` and ``CONTAINS`` are deliberately
#: absent even though they are perfectly good facts: they are containment, so
#: traversing them turns any file node into a fan-out over every symbol it
#: declares, and two hops of that buries the relationship the question was about
#: under an inventory of the neighbourhood.
EXPANSION_EDGES = (
    EdgeType.READS,
    EdgeType.WRITES,
    EdgeType.EXPORTS,
    EdgeType.CONFIGURES,
    EdgeType.DEPENDS_ON,
    EdgeType.IMPORTS,
    EdgeType.REFERENCES,
    EdgeType.EXECUTES,
    EdgeType.SOURCES,
    EdgeType.INCLUDES,
    EdgeType.REQUIRES,
    EdgeType.WATCHES,
    EdgeType.MANAGES,
    EdgeType.GENERATES,
)

#: Edge types a traversal may follow **out of one repository into another**.
#:
#: Every one of these is produced by the linker's global pass from an exact match
#: — a resolved package name, a resolved path, a resolved import — so a
#: cross-repository answer always has a structural reason (V1_SPEC §9). A `CALLS`
#: or `DEFINES` edge is deliberately absent: those are meaningful within a
#: repository but would let a traversal wander between repositories on nothing
#: stronger than a shared symbol name.
CROSS_REPOSITORY_EDGES = frozenset(
    {
        EdgeType.DEPENDS_ON,
        EdgeType.REFERENCES,
        EdgeType.EXECUTES,
        EdgeType.SOURCES,
        EdgeType.IMPORTS,
    }
)


@dataclass(frozen=True, slots=True)
class TraversalResult:
    """What one bounded walk reached, in the order it reached it.

    ``reached`` is ordered, and that order is load-bearing: entities found on the
    first hop precede those found on the second, so rank fusion sees the closer
    relationship as the stronger one.
    """

    reached: list[int] = field(default_factory=list)
    #: Why each reached entity was reached — the edge that got there, and the hop.
    reasons: dict[int, str] = field(default_factory=dict)
    #: Every node the walk touched, seeds included. The cost of the traversal.
    visited: int = 0


def expand_from_seeds(
    knowledge: SqliteKnowledgeStore,
    seeds: Sequence[int],
    *,
    hops: int,
    fanout: int,
    seed_limit: int,
) -> TraversalResult:
    """Walk out of ``seeds``, bounded on three axes at once.

    Hop count, fan-out per hop and total nodes, because a popular hub such as
    ``env:SERVICE_HOST`` legitimately touches every repository and an unbounded
    walk from it would return the world.

    Two hops is the useful minimum, not an arbitrary depth: an interned node like
    an environment variable has no location of its own, so hop one reaches the hub
    and hop two reaches the files on the other side of it. That is what makes
    "where is SERVICE_HOST configured?" answerable from inside a service that only
    reads it.

    Only exact-name entity hits may be passed as seeds. Expansion labels
    everything it reaches ``DETERMINISTIC`` — correctly, since an edge is a fact —
    so seeding it from a fuzzy match would launder a guess into the tier that
    outranks every genuine lead.
    """
    if not seeds:
        return TraversalResult()

    frontier = list(seeds[:seed_limit])
    visited: set[int] = set(frontier)
    reached: list[int] = []
    reasons: dict[int, str] = {}

    for hop in range(max(0, hops)):
        if not frontier:
            break
        edges = knowledge.expand_neighbors(
            frontier,
            EXPANSION_EDGES,
            limit=fanout * max(1, len(frontier)),
        )
        next_frontier: list[int] = []
        for edge in edges:
            if edge.crosses_repositories and edge.type not in CROSS_REPOSITORY_EDGES:
                continue
            for neighbor in (edge.src_id, edge.dst_id):
                if neighbor in visited:
                    continue
                visited.add(neighbor)
                next_frontier.append(neighbor)
                reached.append(neighbor)
                reasons[neighbor] = f"{edge.type.value.lower()} (hop {hop + 1})"
            if len(next_frontier) >= fanout:
                break
        frontier = next_frontier

    return TraversalResult(reached=reached, reasons=reasons, visited=len(visited))
