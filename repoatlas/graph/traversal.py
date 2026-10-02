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

from repoatlas.models.parsing import EdgeDirection, EdgeType, NeighborOrder
from repoatlas.storage.protocols import NeighborEdge
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
    # HTTP linkage. Each joins a repository-owned fact to a shared HTTP node and
    # never two repositories directly, so none belongs in the crossing set below:
    # a walk reaches the far side of an HTTP operation the way it reaches the far
    # side of an environment variable, through the node.
    EdgeType.SERVES_HTTP_ROUTE,
    EdgeType.CALLS_HTTP_ROUTE,
    EdgeType.DECLARES_HTTP_DEPLOYMENT,
    EdgeType.EXPOSES_HTTP_ENDPOINT,
    EdgeType.CALLS_HTTP_ENDPOINT,
)

#: Edge types a relationships request walks when it names none: the expansion set
#: and ``CALLS_HTTP_HANDLER``.
#:
#: A handler link joins a caller to the one handler a source-backed chain proves it
#: reaches (:mod:`repoatlas.graph.http_crossings`), and asking what a request or a
#: handler is connected to is asking for exactly that. It joins two repositories
#: directly, so it leaves one only under ``cross_repository``
#: (:data:`RELATIONSHIP_CROSSING_EDGES`). It is not in :data:`EXPANSION_EDGES`:
#: search expands exactly as it did before links existed.
RELATIONSHIP_DEFAULT_EDGES = (*EXPANSION_EDGES, EdgeType.CALLS_HTTP_HANDLER)

#: Edge types an **explicit** relationship request may name.
#:
#: Wider than :data:`RELATIONSHIP_DEFAULT_EDGES` by exactly ``CALLS`` and ``USES``,
#: and the difference is the point. Those two sets answer different questions, and
#: using one tuple for both is what made ~26,000 indexed ``CALLS`` relationships
#: unreachable: the relationships capability refused to traverse a type the graph
#: plainly held, because the set it consulted was the set that governs *automatic*
#: expansion.
#:
#: ``CALLS`` belongs here and not there. An agent asking "what calls this?" is
#: asking for exactly those edges and has said so. Ordinary search has not: it
#: expands from every exact entity hit, and a call graph that size would generate
#: large, irrelevant candidate sets for questions that never mentioned calls —
#: which is the failure :data:`EXPANSION_EDGES` exists to prevent, and why it is
#: unchanged.
#:
#: ``USES`` is here for the same reason. A declaration's uses of module-scope names
#: are the facts a change-impact question needs, and far more numerous than calls;
#: search must answer exactly as it did before they were indexed, so it never
#: expands through them.
#:
#: Being nameable is not being reachable across a boundary. ``CALLS`` and ``USES``
#: are deliberately absent from :data:`CROSS_REPOSITORY_EDGES` below, so an explicit
#: request still cannot follow one out of its repository.
RELATIONSHIP_EDGES = (*RELATIONSHIP_DEFAULT_EDGES, EdgeType.CALLS, EdgeType.USES)

#: Edge types a traversal may follow **out of one repository into another**.
#:
#: Every one of these is produced by the linker's global pass from an exact match
#: — a resolved package name, path, import, or explicitly mapped Salt pillar — so
#: a cross-repository answer always has a structural reason (V1_SPEC §9). A `CALLS`
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
        EdgeType.GENERATES,
    }
)

#: Edge types a relationships request with ``cross_repository`` may follow out of
#: one repository into another: :data:`CROSS_REPOSITORY_EDGES` and
#: ``CALLS_HTTP_HANDLER``, which the HTTP join emits only for a chain of hops the
#: source proves, each exact. The change-impact walk keeps its own rule for it
#: (:mod:`repoatlas.graph.impact`): inward, on that evidence, and never from a file
#: that does not change whole.
RELATIONSHIP_CROSSING_EDGES = CROSS_REPOSITORY_EDGES | {EdgeType.CALLS_HTTP_HANDLER}


#: Structural edges: what declares or contains what.
#:
#: Absent from :data:`EXPANSION_EDGES` and deliberately so — traversing them turns
#: any file node into a fan-out over every symbol it declares. They are named here
#: because a caller may legitimately ask to *see* them as structural context, and
#: the set that may be shown has to be the same set that may not be expanded
#: through, decided in one place.
CONTAINMENT_EDGES = frozenset({EdgeType.CONTAINS, EdgeType.DEFINES})


@dataclass(frozen=True, slots=True)
class WalkStep:
    """One edge, and how the walk arrived at it.

    ``edge`` keeps its **stored semantic direction** untouched. ``a IMPORTS b`` is
    a fact about a and b in that order, and it stays that way whether the walk
    reached it from a or from b — asking "what imports this?" does not make the
    importing happen backwards. Which end the walk came from is a separate fact
    and is recorded separately, in :attr:`from_id` and :attr:`outgoing`.
    """

    edge: NeighborEdge
    #: 1 for an edge incident to a seed, 2 for one reached from a hop-1 node.
    hop: int
    #: The frontier node this edge was reached from.
    from_id: int
    #: The endpoint on the other side of it.
    to_id: int
    #: Whether the walk followed the edge with its direction (``from_id`` is the
    #: edge's source) or against it.
    outgoing: bool

    @property
    def followed(self) -> EdgeDirection:
        return EdgeDirection.OUT if self.outgoing else EdgeDirection.IN


@dataclass(frozen=True, slots=True)
class WalkResult:
    """Everything one bounded walk touched: the edges, and the nodes they reached."""

    #: Every admitted edge, in the order the walk met it. Deduplicated on the
    #: edge's own facts, so one relationship appears once however many ways the
    #: walk could have arrived at it.
    steps: list[WalkStep] = field(default_factory=list)
    reached: list[int] = field(default_factory=list)
    reasons: dict[int, str] = field(default_factory=dict)
    visited: int = 0
    #: Whether a bound stopped the walk short of the edges the graph held. Not a
    #: failure — it is the difference between "this is the neighbourhood" and
    #: "this is as much of it as was allowed", which a caller reporting
    #: completeness has to be able to tell apart.
    bounded: bool = False


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


def walk(
    knowledge: SqliteKnowledgeStore,
    seeds: Sequence[int],
    *,
    hops: int,
    fanout: int,
    seed_limit: int,
    edge_types: Sequence[EdgeType] = EXPANSION_EDGES,
    direction: EdgeDirection = EdgeDirection.ANY,
    crossing_edges: frozenset[EdgeType] = CROSS_REPOSITORY_EDGES,
    containment: bool = False,
    order: NeighborOrder = NeighborOrder.CONFIDENCE,
    home_repo_id: int | None = None,
) -> WalkResult:
    """The one bounded walk. Everything that traverses this graph comes through here.

    Bounded on three axes at once — hop count, fan-out per hop and total nodes —
    because a popular hub such as ``env:SERVICE_HOST`` legitimately touches every
    repository and an unbounded walk from it would return the world.

    Two hops is the useful minimum, not an arbitrary depth: an interned node like
    an environment variable has no location of its own, so hop one reaches the hub
    and hop two reaches the files on the other side of it. That is what makes
    "where is SERVICE_HOST configured?" answerable from inside a service that only
    reads it.

    ``crossing_edges`` is the set permitted to leave a repository. Every member of
    the default is produced by the linker's global pass from an exact match, so a
    cross-repository answer always has a structural reason (V1_SPEC §9). Passing
    an empty set confines the walk to one repository; passing a *wider* set is not
    offered, because the restriction is what keeps a traversal from wandering
    between repositories on nothing stronger than a shared symbol name.

    ``containment`` admits :data:`CONTAINMENT_EDGES` as visible structural context
    around the **seeds only**, and only that. It is a deliberately bounded one-hop
    relaxation, held down twice over: a containment edge is not even queried
    beyond the first hop, and a node reached through one never joins the next
    frontier. Either bound alone would leak. Without the first, a file reached by
    a relationship at hop one would arrive carrying an inventory of every symbol
    it declares — the answer would not have expanded *through* containment, but it
    would have exploded all the same, which is the outcome the exclusion exists to
    prevent.

    ``home_repo_id`` confines the walk to one repository **through shared nodes as
    well**. ``crossing_edges`` governs an edge whose two ends belong to different
    repositories, and an edge touching a shared node — ``env:SERVICE_HOST``, an
    HTTP operation — has a repository at one end only, so it never "crosses". Two
    such edges in a row still leave the repository: a service reaches the hub, and
    the hub reaches every other repository that touches it. A walk given a home
    repository follows no edge whose far end belongs to another one. The shared
    node stays reachable, and so does everything on the home side of it. ``None``
    — retrieval's expansion, and a walk rooted at a shared node, which has no
    repository to stay in — confines nothing.

    ``order`` decides which edges survive the per-hop bound. Retrieval's default
    puts the most confident first because rank fusion reads that as proximity; a
    caller whose answer is the cut itself wants
    :attr:`~repoatlas.models.parsing.NeighborOrder.NATURAL` instead, so the cut
    falls on facts about the repositories rather than on insertion order.

    Only exact-name entity hits may be passed as seeds. The walk labels everything
    it reaches deterministic — correctly, since an edge is a fact — so seeding it
    from a fuzzy match would launder a guess into the tier that outranks every
    genuine lead.
    """
    if not seeds:
        return WalkResult()

    structural: Sequence[EdgeType] = (
        (*edge_types, *sorted(CONTAINMENT_EDGES, key=str)) if containment else edge_types
    )
    frontier = list(seeds[:seed_limit])
    visited: set[int] = set(frontier)
    steps: list[WalkStep] = []
    seen_edges: set[tuple[int, int, str, int | None, int | None, int | None]] = set()
    reached: list[int] = []
    reasons: dict[int, str] = {}
    bounded = False

    for hop in range(max(0, hops)):
        if not frontier:
            break
        frontier_set = set(frontier)
        # Containment is asked for around the seeds and nowhere else.
        admitted = structural if hop == 0 else edge_types
        want = fanout * max(1, len(frontier))
        # One row past the bound, so "the graph held more" is observed rather
        # than inferred from a full page — which would report a cut on a
        # neighbourhood that happened to end exactly on it.
        edges = knowledge.expand_neighbors(
            frontier,
            admitted,
            limit=want + 1,
            direction=direction,
            order=order,
        )
        if len(edges) > want:
            bounded = True
            edges = edges[:want]
        next_frontier: list[int] = []
        for edge in edges:
            if edge.crosses_repositories and edge.type not in crossing_edges:
                continue

            if edge.src_id in frontier_set:
                from_id, to_id, outgoing = edge.src_id, edge.dst_id, True
            elif edge.dst_id in frontier_set:
                from_id, to_id, outgoing = edge.dst_id, edge.src_id, False
            else:  # pragma: no cover - the query guarantees one end is in scope
                continue

            if home_repo_id is not None:
                far_repo = edge.dst_repo_id if outgoing else edge.src_repo_id
                if far_repo is not None and far_repo != home_repo_id:
                    continue

            identity = (
                edge.src_id,
                edge.dst_id,
                str(edge.type),
                edge.file_id,
                edge.line_start,
                edge.line_end,
            )
            if identity not in seen_edges:
                seen_edges.add(identity)
                steps.append(
                    WalkStep(
                        edge=edge, hop=hop + 1, from_id=from_id, to_id=to_id, outgoing=outgoing
                    )
                )

            contains = edge.type in CONTAINMENT_EDGES
            for neighbor in (edge.src_id, edge.dst_id):
                if neighbor in visited:
                    continue
                visited.add(neighbor)
                reached.append(neighbor)
                reasons[neighbor] = f"{edge.type.value.lower()} (hop {hop + 1})"
                if not contains:
                    # Containment is context, never a route onward.
                    next_frontier.append(neighbor)
            if len(next_frontier) >= fanout:
                bounded = True
                break
        frontier = next_frontier

    return WalkResult(
        steps=steps, reached=reached, reasons=reasons, visited=len(visited), bounded=bounded
    )


def expand_from_seeds(
    knowledge: SqliteKnowledgeStore,
    seeds: Sequence[int],
    *,
    hops: int,
    fanout: int,
    seed_limit: int,
    home_repo_id: int | None = None,
) -> TraversalResult:
    """The nodes a bounded walk reaches, in the order it reaches them.

    Retrieval's view of :func:`walk`: it needs which entities were reached and
    why, and nothing about the edges themselves. The order is load-bearing —
    entities found on the first hop precede those found on the second, so rank
    fusion sees the closer relationship as the stronger one.

    ``home_repo_id`` confines the walk exactly as it does for a relationships
    request: the shared node stays reachable and nothing on the far side of one is
    followed. ``None`` — V1's own expansion, which has no repository to stay in —
    confines nothing, and is what this has always done.
    """
    result = walk(
        knowledge, seeds, hops=hops, fanout=fanout, seed_limit=seed_limit, home_repo_id=home_repo_id
    )
    return TraversalResult(reached=result.reached, reasons=result.reasons, visited=result.visited)
