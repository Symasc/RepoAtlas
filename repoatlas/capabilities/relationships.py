"""Ordering, bounding and provenance grouping for graph relationships (V2).

Pure. It receives the edges one bounded walk admitted, already resolved to natural
identities, and decides which of them are returned and in what order — plus which
distinct source spans have to be supplied to back them. It walks no graph, opens
nothing, and holds no handle on anything.

Why the order is written down here
----------------------------------
The store returns edges by confidence and then by row id, which is insertion
order. That is a fine tiebreak for a ranking stage that is about to fuse
everything anyway, and it is the wrong basis for a truncated answer: two indexes
built from the same repositories in a different file order would return different
neighbourhoods for the same question, and neither would be wrong. So the edges are
re-ordered on facts about the repository — hop, relationship type, then the
natural keys of both ends — before anything is cut. The prefix a caller sees is
then a property of the code, not of how the database happened to fill up.

Why provenance is grouped
-------------------------
One import line legitimately asserts several edges. Supplying its text once and
letting every edge that rests on it carry the same evidence id is not an
optimisation: re-supplying identical bytes for the same span would be the same
evidence with two identities, and a reader comparing two citations would have no
way to tell they were the same line. The grouping key is the ledger's own notion
of a location, so "the same span" means here exactly what it will mean there.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from repoatlas.capabilities.errors import InvalidRelationshipRequestError
from repoatlas.graph.keys import is_positional
from repoatlas.graph.traversal import CONTAINMENT_EDGES, EXPANSION_EDGES
from repoatlas.models.investigation import ProposedEvidence
from repoatlas.models.parsing import EdgeDirection, EdgeType
from repoatlas.models.relationships import (
    DEFAULT_MAX_NEIGHBORS,
    MAX_HOPS,
    MAX_NEIGHBORS_CEILING,
    AppliedGraphLimits,
    EdgeProvenance,
    EntityRef,
    GraphEdge,
    RelationshipDirection,
    RelationshipsRequest,
)
from repoatlas.storage.protocols import EntityIdentity, NeighborEdge

#: How a public direction narrows the store's neighbourhood query.
EDGE_DIRECTIONS: dict[RelationshipDirection, EdgeDirection] = {
    RelationshipDirection.OUTGOING: EdgeDirection.OUT,
    RelationshipDirection.INCOMING: EdgeDirection.IN,
    RelationshipDirection.BOTH: EdgeDirection.ANY,
}

#: A source location, as the ledger will key it: which repository, which file,
#: which lines. Two edges asserted by the same lines of the same file share one
#: piece of evidence; the same coordinates in a different repository do not,
#: because they are a different file.
ProvenanceKey = tuple[str, str, int, int]


@dataclass(frozen=True, slots=True)
class BoundedEdges:
    """The edges that fit, and whether anything did not."""

    edges: list[GraphEdge]
    #: More permitted relationships existed than were returned.
    truncated: bool


def edge_order(edge: GraphEdge) -> tuple[object, ...]:
    """The deterministic sort key for one relationship.

    Every component is a fact about the repositories rather than about the
    database: how far the walk went, what kind of relationship it is, and the
    natural identities of both ends — which for a file-scoped entity already
    contain its repository and path. The provenance lines break the remaining
    ties, so a symbol imported twice from the same file keeps a stable order
    rather than an incidental one.

    Deliberately **not** confidence-first. Confidence is a parser's own measure and
    ordering by it would make the returned prefix depend on a number that means
    different things to different parsers; it is reported on every edge instead.

    Deliberately independent of anything that has to be looked up. The order is
    settled before the answer is cut, and the provenance text behind the survivors
    is fetched afterwards — so what a caller receives never depends on how much
    was worth fetching.
    """
    provenance = edge.provenance
    return (
        edge.hop,
        str(edge.type),
        edge.source.key,
        edge.target.key,
        provenance.line_start if provenance and provenance.line_start is not None else 0,
        provenance.line_end if provenance and provenance.line_end is not None else 0,
    )


def bound(edges: Sequence[GraphEdge], max_neighbors: int) -> BoundedEdges:
    """Order deterministically, then cut. Never the other way round.

    Cutting first would make the answer depend on which rows the store happened to
    return, and ordering afterwards would hide that behind a tidy-looking result.
    """
    ordered = sorted(edges, key=edge_order)
    if len(ordered) <= max_neighbors:
        return BoundedEdges(edges=ordered, truncated=False)
    return BoundedEdges(edges=ordered[:max_neighbors], truncated=True)


def provenance_key(provenance: EdgeProvenance | None) -> ProvenanceKey | None:
    """The location an edge's asserting lines occupy, or ``None``.

    ``None`` for every edge whose provenance names no complete span: a linker
    edge resolved from an exact package match has a parser and no line, and a
    partially recorded span cannot be supplied as one. Neither is a defect and
    neither becomes a citation.
    """
    if provenance is None:
        return None
    if provenance.repository_slug is None or provenance.path is None:
        return None
    if provenance.line_start is None or provenance.line_end is None:
        return None
    if provenance.line_end < provenance.line_start:
        return None
    return (
        provenance.repository_slug,
        provenance.path,
        provenance.line_start,
        provenance.line_end,
    )


def distinct_provenance(edges: Sequence[GraphEdge]) -> list[ProvenanceKey]:
    """Every source span these edges rest on, once each, in a stable order.

    Stable because it follows the edges, which are already ordered on facts about
    the repository. A caller reading the evidence in the order it was supplied
    sees it in the same order as the relationships that rest on it.
    """
    seen: dict[ProvenanceKey, None] = {}
    for edge in edges:
        key = provenance_key(edge.provenance)
        if key is not None:
            seen.setdefault(key, None)
    return list(seen)


def path_nodes(chain: Sequence[GraphEdge], origin: EntityRef) -> tuple[EntityRef, ...]:
    """The nodes a chain of edges visits, starting from where the walk began.

    Follows each edge from whichever end the walk was standing on, which is why a
    chain reading ``A -> B -> C`` produces exactly ``A, B, C`` even when the second
    edge is stored as ``C -> B``. The stored orientation is untouched; this is the
    route, not the facts.
    """
    nodes: list[EntityRef] = [origin]
    current = origin.key
    for edge in chain:
        following = edge.target if edge.source.key == current else edge.source
        nodes.append(following)
        current = following.key
    return tuple(nodes)


@dataclass(frozen=True, slots=True)
class ProvenanceSupply:
    """One source span, ready for the ledger, with the exact text it hashed.

    The text travels beside the proposal rather than on it, because the ledger
    keeps no bytes: a proposal carries a digest and a length. Pairing them here is
    what makes it impossible to hash one string and hand back another.
    """

    proposed: ProposedEvidence
    text: str


def graph_limits_for(request: RelationshipsRequest) -> AppliedGraphLimits:
    """What the server grants for one request, and where it lowered it.

    A supplied value is honoured up to the ceiling and lowered to it otherwise,
    never raised: asking for a smaller neighbourhood is a legitimate request and
    quietly widening it would return relationships the caller did not ask to read.
    Hops are already bounded by the request model, which is where a value outside
    the range belongs — it is malformed rather than merely ambitious.
    """
    clamped: list[str] = []
    if request.max_neighbors is None:
        neighbors = min(DEFAULT_MAX_NEIGHBORS, MAX_NEIGHBORS_CEILING)
    elif request.max_neighbors > MAX_NEIGHBORS_CEILING:
        clamped.append("max_neighbors")
        neighbors = MAX_NEIGHBORS_CEILING
    else:
        neighbors = request.max_neighbors
    return AppliedGraphLimits(
        hops=min(request.hops, MAX_HOPS),
        max_neighbors=neighbors,
        clamped=tuple(clamped),
    )


def admitted_edge_types(request: RelationshipsRequest) -> tuple[EdgeType, ...]:
    """The relationship types this request may traverse.

    The policy is the shared expansion set, plus containment when the caller asked
    for it. A request naming a type outside that is refused rather than answered
    with nothing: "no relationships of that kind" and "that kind is not
    traversable" are different answers, and only the second one has a remedy worth
    telling the caller about.

    Containment is the case where the remedy is exact, so the refusal names it.
    """
    policy = set(EXPANSION_EDGES)
    if request.include_containment:
        policy |= CONTAINMENT_EDGES
    if request.edge_types is None:
        return tuple(EXPANSION_EDGES)

    refused = [edge_type for edge_type in request.edge_types if edge_type not in policy]
    if refused:
        structural = [edge_type for edge_type in refused if edge_type in CONTAINMENT_EDGES]
        raise InvalidRelationshipRequestError(
            "that relationship request names an edge type the graph policy does not traverse",
            detail=(
                "Structural containment is shown only when include_containment is set."
                if structural
                else "Narrow to relationship types the deterministic traversal admits."
            ),
            edge_types=tuple(sorted(str(edge_type) for edge_type in refused)),
        )
    # Order follows the policy rather than the request, so two callers asking for
    # the same set in a different order get the same query.
    requested = set(request.edge_types)
    admitted = [edge_type for edge_type in EXPANSION_EDGES if edge_type in requested]
    admitted += [
        edge_type for edge_type in sorted(CONTAINMENT_EDGES, key=str) if edge_type in requested
    ]
    return tuple(admitted)


def entity_ref(identity: EntityIdentity) -> EntityRef:
    """A resolved entity as its natural identity, with no storage id attached."""
    return EntityRef(
        key=identity.key,
        type=identity.type,
        name=identity.name,
        repository_slug=identity.repository_slug,
        path=identity.path,
        line_start=identity.line_start,
        line_end=identity.line_end,
        positional=is_positional(identity.key),
    )


def edge_provenance(edge: NeighborEdge) -> EdgeProvenance | None:
    """Where RepoAtlas says this edge came from, as far as it knows so far.

    The asserting file is named by row id, which the public contract may not
    carry, so ``repository_slug`` and ``path`` are both filled in later from the
    read that resolves that row — together, because they are two halves of one
    fact. Deriving the repository from the edge's *source entity* instead would be
    quicker and would usually agree, and that is exactly the problem: it would be
    a claim about the asserting file assembled from something else, and on the day
    the two disagreed nothing would say so.

    ``citable`` is likewise left false here and set only once text has actually
    been supplied and recorded. That ordering is the point: provenance metadata is
    available for any parsed edge, and its presence is not a claim that anything
    is quotable.

    ``None`` when the edge carries no assertion at all — an edge with no parser,
    no file and no line asserts nothing, which is truthful and is not a gap to be
    filled.
    """
    if edge.parser is None and edge.file_id is None and edge.line_start is None:
        return None
    return EdgeProvenance(
        repository_slug=None,
        path=None,
        line_start=edge.line_start,
        line_end=edge.line_end,
        parser=edge.parser,
        citable=False,
    )
