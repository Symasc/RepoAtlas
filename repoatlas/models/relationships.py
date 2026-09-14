"""Wire models for the ``relationships`` capability (RepoAtlas V2).

Deterministic graph facts, and nothing else. Every edge here was written by a
parser or resolved by the linker from an exact match; none of it is retrieved,
ranked or guessed. That is why there is no score, no relevance and no similarity
in this module: relationships does not answer "what is most related", it answers
"what does RepoAtlas already know is connected to this, and how does it know".

Two distinctions are built into the types rather than left to the code.

**Stored direction is not traversal direction.** An edge is a directed fact —
``service READS_ENV SERVICE_HOST`` says something about which end does the
reading. Asking "what reads SERVICE_HOST?" walks that edge backwards, and the fact
does not turn around when you approach it from the other side. So
:attr:`GraphEdge.source` and :attr:`GraphEdge.target` always hold the stored
orientation, and how the walk got there lives separately in
:attr:`GraphEdge.traversal_direction`. Reversing the first to express the second
would rewrite the repository's meaning to describe the caller's question.

**Deterministic provenance is not citable text.** An edge knows which file
asserted it and at which lines; that is a fact about the graph and it is reported
as :class:`EdgeProvenance`. Whether the exact source text of those lines could be
reconstructed and recorded is a *separate* question with its own answer, and only
when it is yes does the edge carry evidence ids. Collapsing the two would let an
edge advertise a citation that verification cannot resolve — which is
indistinguishable from a fabricated one.
"""

from __future__ import annotations

import enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from repoatlas.models.capability import CapabilityContext
from repoatlas.models.capability_io import ContextView, EvidenceItem
from repoatlas.models.freshness import FreshnessDepth, IndexFreshness
from repoatlas.models.investigation import EvidenceId, LedgerSummary
from repoatlas.models.parsing import EdgeType, EntityType

#: The deepest a relationships walk will ever go.
#:
#: Two is the useful bound rather than an arbitrary one: an interned node such as
#: an environment variable has no location of its own, so hop one reaches the hub
#: and hop two reaches the files on the far side of it. Three would multiply the
#: neighbourhood again for an answer nobody asked, and a graph question that needs
#: more than two hops is a different capability, not a larger number here.
MAX_HOPS = 2

#: Direct relationships, unless the caller says otherwise.
DEFAULT_HOPS = 1

#: The most nodes-worth of edges the server will return, whatever is asked.
MAX_NEIGHBORS_CEILING = 200

#: Enough to see a real neighbourhood without becoming an inventory.
DEFAULT_MAX_NEIGHBORS = 50


class RelationshipDirection(enum.StrEnum):
    """Which end of an edge the queried entity is asked to sit on.

    A question about the neighbourhood, not a claim about the edges: narrowing to
    ``INCOMING`` selects edges that point *at* the target and leaves every one of
    them oriented exactly as it is stored.
    """

    #: Edges the target is the source of: what it reaches.
    OUTGOING = "outgoing"
    #: Edges the target is the destination of: what reaches it.
    INCOMING = "incoming"
    #: Both, which is the neighbourhood question and the default.
    BOTH = "both"


class EntityRef(BaseModel):
    """A graph entity, by natural identity.

    ``key`` is the identity and the only thing a caller needs to ask again. It is
    derived from what the repository says, so it survives a reindex that renumbers
    every row — which is exactly what a storage id does not. No storage id appears
    in this model or anywhere below it on the wire.
    """

    model_config = ConfigDict(frozen=True)

    #: e.g. ``env:SERVICE_HOST``, ``file:svc:src/config.py``,
    #: ``sym:svc:src/config.py:load_config:18``.
    key: str
    type: EntityType
    #: For people. Not unique and never an identity.
    name: str = ""
    #: ``None`` for an interned entity: an environment variable belongs to no
    #: repository, and giving it one would manufacture provenance for a fact that
    #: has none.
    repository_slug: str | None = None
    path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    #: Whether the key embeds a source position and therefore stops resolving when
    #: the definition moves. Published so a caller knows which handles survive a
    #: sync, instead of finding out from a lookup that returns nothing.
    positional: bool = False


class EdgeProvenance(BaseModel):
    """Where RepoAtlas says an edge came from.

    Deterministic metadata about the graph, and deliberately **not** a citation.
    It answers "which file asserts this, and where" — a question the index can
    always answer for a parsed edge. Whether the text at those coordinates can be
    reconstructed and recorded is a different question, and :attr:`citable` is its
    answer rather than an assumption drawn from this one being present.
    """

    model_config = ConfigDict(frozen=True)

    repository_slug: str | None = None
    path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    #: Which parser or linker pass asserted the edge.
    parser: str | None = None
    #: True only when the exact source text at these coordinates was supplied and
    #: recorded in this investigation's ledger. False is a truthful and common
    #: answer: a linker-resolved edge may have no asserting line at all, and a
    #: span whose chunks cannot prove their coverage has no citable text either.
    citable: bool = False


class GraphEdge(BaseModel):
    """One deterministic relationship, oriented as the repository states it."""

    model_config = ConfigDict(frozen=True)

    #: The end the stored fact names first. Never swapped to suit the question.
    source: EntityRef
    target: EntityRef
    type: EdgeType
    #: 1 for an edge incident to the queried entity, 2 for one beyond it.
    hop: int = Field(ge=1, le=MAX_HOPS)
    #: How the walk arrived at this edge — with its direction or against it. A
    #: fact about the traversal, never about the relationship.
    traversal_direction: RelationshipDirection = RelationshipDirection.OUTGOING
    #: Whether the two endpoints live in different repositories.
    cross_repository: bool = False
    #: The parser or linker's own confidence in this edge, unchanged. It means
    #: what V1 stores; it is not a probability that the fact is true, and nothing
    #: here reinterprets it.
    confidence: float = 1.0
    provenance: EdgeProvenance | None = None
    #: Ledger ids for the supplied source text asserting this edge, if any were
    #: recorded. Empty is truthful and frequent; it never means the edge is less
    #: of a fact.
    evidence_ids: tuple[EvidenceId, ...] = ()


class GraphPath(BaseModel):
    """One bounded traversal chain by which a result was reached.

    **Not a shortest path.** RepoAtlas computes no shortest path and claims none;
    this records the route this walk actually took, which is what lets a caller
    see why a second-hop relationship is in the answer at all. For ``A -> B -> C``
    it holds exactly three nodes and the two real edges between them, and never a
    synthetic ``A -> C`` unless such an edge genuinely exists.
    """

    model_config = ConfigDict(frozen=True)

    nodes: tuple[EntityRef, ...] = ()
    edges: tuple[GraphEdge, ...] = ()


class AppliedGraphLimits(BaseModel):
    """The bounds the server actually applied, and where it lowered a request."""

    model_config = ConfigDict(frozen=True)

    hops: int
    max_neighbors: int
    #: Field names the server lowered to its own ceiling, in a stable order.
    clamped: tuple[str, ...] = ()
    ceiling_hops: int = MAX_HOPS
    ceiling_max_neighbors: int = MAX_NEIGHBORS_CEILING


class RelationshipsRequest(BaseModel):
    """Ask what RepoAtlas already knows is connected to one exact entity."""

    model_config = ConfigDict(frozen=True)

    context: CapabilityContext
    #: The exact natural key. This is the handle ``search`` hands back, usable
    #: verbatim: there is no name lookup here and nothing to reconstruct.
    target: str
    direction: RelationshipDirection = RelationshipDirection.BOTH
    #: Narrow to particular relationships. ``None`` means the whole set the
    #: deterministic traversal policy permits; naming a type outside that set is
    #: refused rather than silently returning nothing.
    edge_types: tuple[EdgeType, ...] | None = None
    hops: int = Field(default=DEFAULT_HOPS, ge=1, le=MAX_HOPS)
    max_neighbors: int | None = Field(default=None, gt=0)
    #: Whether the walk may leave the target's repository. Even when true, only
    #: the edge types the shared policy allows to cross may do so.
    cross_repository: bool = False
    #: Whether structural containment edges are shown. Off by default, because
    #: they turn a file into an inventory of every symbol it declares. When on,
    #: they are context only and are never expanded through.
    include_containment: bool = False
    investigation_id: str | None = None
    depth: FreshnessDepth = FreshnessDepth.STORED

    @field_validator("target")
    @classmethod
    def _target_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("target must be an exact entity key")
        return value

    @model_validator(mode="after")
    def _edge_types_are_named(self) -> RelationshipsRequest:
        if self.edge_types is not None and not self.edge_types:
            raise ValueError("edge_types must name at least one type, or be omitted entirely")
        return self


class RelationshipsResponse(BaseModel):
    """The deterministic neighbourhood of one entity, in one investigation."""

    model_config = ConfigDict(frozen=True)

    investigation_id: str
    context: ContextView
    #: The entity that was asked about, resolved exactly.
    target: EntityRef
    #: In a deterministic order that does not depend on storage layout, so the
    #: same question against the same index returns the same answer, and a
    #: truncated answer keeps the same prefix.
    edges: tuple[GraphEdge, ...] = ()
    #: Present only for results beyond the first hop, where the chain is what
    #: explains the result. A first-hop edge is its own explanation.
    paths: tuple[GraphPath, ...] = ()
    #: Source text asserting these edges, recorded in the ledger before this
    #: response existed. Supplied once per span however many edges cite it.
    evidence: tuple[EvidenceItem, ...] = ()
    freshness: IndexFreshness
    #: True when the graph held more permitted relationships than were returned.
    truncated: bool = False
    #: The same fact, named for the question a caller asks. Equal to
    #: :attr:`truncated` by construction, and kept because "this was cut" and
    #: "more exists" are read at different moments.
    has_more: bool = False
    limits: AppliedGraphLimits
    ledger: LedgerSummary
