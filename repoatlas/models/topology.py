"""Topology: how repositories, directories and files are connected, in aggregate.

Three read-only questions a map of the workspace needs and no other capability
answers without one call per entity:

* **overview** — which repositories are linked to which, and by what kind of fact;
* **children** — what one directory (or one file) holds, and how each child is
  linked to its siblings, the rest of its repository and, when asked, to other
  repositories;
* **edge** — one exact stored relationship, with the evidence that asserts it.

Links are kept apart by **kind**, because the three kinds are different claims and
summing them would manufacture a number no fact supports:

* ``observed`` — stored graph edges between code, other than HTTP;
* ``http`` — stored HTTP edges. A caller-to-handler edge rests on route
  compatibility, never on deployment identity;
* ``dependency`` — stored ``DEPENDS_ON`` edges: a manifest declaring a package,
  resolved by the linker to the repository that publishes it where exactly one
  other repository does.

An ``edge_ref`` names one stored fact exactly. It is opaque: a caller passes it
back verbatim and never parses it, because its encoding is the server's business
and may change with the ``er`` version prefix.
"""

from __future__ import annotations

import enum
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from repoatlas.models.capability import CapabilityContext
from repoatlas.models.capability_io import ContextView, EvidenceItem
from repoatlas.models.dependency_inventory import AppliedPageLimits
from repoatlas.models.freshness import FreshnessDepth, IndexFreshness
from repoatlas.models.impact import HttpCrossing, PackageCrossing
from repoatlas.models.investigation import LedgerSummary
from repoatlas.models.parsing import EdgeType, EntityType
from repoatlas.models.relationships import GraphEdge, RelationshipDirection

#: The topology read policy. Named in every response so a caller comparing two
#: answers can tell a change in the index from a change in the rules.
POLICY = "topology/1"

#: The most example ``edge_ref`` values one link carries.
MAX_EXAMPLES = 3
#: The most package rows one dependency link lists; ``count`` says how many exist.
MAX_PACKAGES = 20
#: The most ambiguous package declarations an overview lists.
MAX_AMBIGUOUS = 50
#: The most links one children answer returns, largest first.
MAX_CHILD_LINKS = 300
#: Children entries per page, by default and at most.
DEFAULT_CHILDREN_PAGE = 100
MAX_CHILDREN_PAGE = 500


class LinkKind(enum.StrEnum):
    """Which kind of fact a link aggregates. Never mixed within one link."""

    OBSERVED = "observed"
    HTTP = "http"
    DEPENDENCY = "dependency"


class EdgeTypeCount(BaseModel):
    """How many stored edges of one type a link holds."""

    model_config = ConfigDict(frozen=True)

    type: EdgeType
    count: int = Field(ge=1)


class PackageLink(BaseModel):
    """One package a dependency link rests on, with where each side says so."""

    model_config = ConfigDict(frozen=True)

    ecosystem: str
    name: str
    #: ``repository/path:line`` of the first declaration, in declaration order.
    declared_at: str
    #: How many declarations of this package the declaring side holds.
    declarations: int = Field(ge=1)
    constraint: str | None = None
    #: ``repository/path:line`` of the manifest publishing the package on the
    #: other side, when exactly one there does.
    provided_at: str | None = None


class AmbiguousDependency(BaseModel):
    """A declaration that more than one other repository could satisfy.

    Listed rather than drawn: the linker draws no edge for it, because picking one
    of several publishers would be a guess.
    """

    model_config = ConfigDict(frozen=True)

    repository: str
    ecosystem: str
    name: str
    providers: tuple[str, ...]


class RepositoryLink(BaseModel):
    """Every fact of one kind that joins one repository to another."""

    model_config = ConfigDict(frozen=True)

    kind: LinkKind
    #: The repository holding each edge's source: the caller for HTTP, the
    #: declaring repository for a dependency.
    source: str
    #: The repository holding each edge's target.
    target: str
    #: How many stored edges the link holds.
    count: int = Field(ge=1)
    types: tuple[EdgeTypeCount, ...] = ()
    #: Up to :data:`MAX_EXAMPLES` opaque references to edges this link counts.
    examples: tuple[str, ...] = ()
    #: For a dependency link, up to :data:`MAX_PACKAGES` of the packages its
    #: edges declare, read from the declarations they were drawn from.
    packages: tuple[PackageLink, ...] = ()


class TopologyOverviewRequest(BaseModel):
    """Ask which repositories in scope are linked to which."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    context: CapabilityContext
    depth: FreshnessDepth = FreshnessDepth.STORED


class TopologyOverviewResponse(BaseModel):
    """Links between the repositories in scope. Both ends of a link are in scope."""

    model_config = ConfigDict(frozen=True)

    context: ContextView
    #: Every repository in scope, linked or not, so an unlinked one is visibly
    #: unlinked rather than absent.
    repositories: tuple[str, ...] = ()
    links: tuple[RepositoryLink, ...] = ()
    ambiguous: tuple[AmbiguousDependency, ...] = ()
    ambiguous_total: int = 0
    #: False when the edge scan reached its row ceiling. Edge links are then
    #: omitted rather than reported from an arbitrary subset.
    complete: bool = True
    freshness: IndexFreshness
    policy: str = POLICY


class EntryKind(enum.StrEnum):
    DIRECTORY = "directory"
    FILE = "file"
    DECLARATION = "declaration"


class ChildEntry(BaseModel):
    """One immediate child of a directory, or one declaration in a file."""

    model_config = ConfigDict(frozen=True)

    #: The identity a link names this child by: the repository-relative path of a
    #: directory or file, or the entity key of a declaration.
    id: str
    kind: EntryKind
    name: str
    #: Repository-relative. For a declaration, the file that declares it.
    path: str
    #: The entity key of a file or declaration. Directories have none.
    key: str | None = None
    type: EntityType | None = None
    qualified_name: str | None = None
    language: str | None = None
    #: Indexed files at or beneath a directory.
    files: int | None = None
    #: Entities declared in a file, the file itself not counted.
    declarations: int | None = None
    line_start: int | None = None
    line_end: int | None = None


class PeerKind(enum.StrEnum):
    #: Elsewhere in the same repository.
    LOCAL = "local"
    #: Another repository.
    REPOSITORY = "repository"
    #: An entity no repository owns: a package, module, variable or HTTP node.
    SHARED = "shared"


class TopologyPeer(BaseModel):
    """What a child is linked to, at the granularity of the question asked.

    A local peer is named by its path truncated to the child's own depth, so the
    peers of a directory's children are themselves directories or files at the
    same level; inside a file, a peer in the same file is named by its key.
    """

    model_config = ConfigDict(frozen=True)

    kind: PeerKind
    repository: str | None = None
    path: str | None = None
    key: str | None = None
    entity_type: EntityType | None = None


class ChildLink(BaseModel):
    """Every fact of one kind between one child and one peer, in one direction."""

    model_config = ConfigDict(frozen=True)

    kind: LinkKind
    #: The :attr:`ChildEntry.id` this link starts or ends at. In a file's answer
    #: it is the file's own path for edges the file asserts as a whole.
    child: str
    #: ``outgoing`` when the child holds the edges' sources. A link between two
    #: children is reported once, from the child holding the sources.
    direction: RelationshipDirection
    peer: TopologyPeer
    count: int = Field(ge=1)
    types: tuple[EdgeTypeCount, ...] = ()
    examples: tuple[str, ...] = ()


def _normalized_path(value: str) -> str:
    """A repository-relative path, or ``""`` for the repository root."""
    raw = value.strip()
    if raw in ("", ".", "./"):
        return ""
    if raw.startswith("/") or "\\" in raw:
        raise ValueError("path must be repository-relative, with forward slashes")
    parts = PurePosixPath(raw).parts
    if any(part in (".", "..") for part in raw.rstrip("/").split("/")):
        raise ValueError("path may not contain '.' or '..' segments")
    return "/".join(parts)


class TopologyChildrenRequest(BaseModel):
    """Ask what one directory or file holds, and how its children are linked."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Must name one repository: children are always children of a path in it.
    context: CapabilityContext
    #: Repository-relative directory or file. ``""`` is the repository root.
    path: str = ""
    #: Whether links to other repositories are reported. Off by default, because
    #: the scope names one repository and reaching another is a thing to ask for.
    cross_repository: bool = False
    limit: int | None = Field(default=None, gt=0)
    cursor: str | None = None
    depth: FreshnessDepth = FreshnessDepth.STORED

    @field_validator("path")
    @classmethod
    def _path_is_relative(cls, value: str) -> str:
        return _normalized_path(value)


class TopologyChildrenResponse(BaseModel):
    """One page of a path's children, and the links of all of them."""

    model_config = ConfigDict(frozen=True)

    context: ContextView
    repository: str
    path: str
    #: Whether ``path`` named a directory or a file.
    target: EntryKind
    entries: tuple[ChildEntry, ...] = ()
    entries_total: int = 0
    next_cursor: str | None = None
    has_more: bool = False
    limits: AppliedPageLimits
    #: Links of every child, not only this page's, largest first.
    links: tuple[ChildLink, ...] = ()
    links_total: int = 0
    #: True when more links existed than :data:`MAX_CHILD_LINKS`.
    truncated: bool = False
    #: False when an edge scan reached its row ceiling; edge links are then
    #: omitted rather than reported from an arbitrary subset.
    complete: bool = True
    freshness: IndexFreshness
    policy: str = POLICY


class TopologyEdgeRequest(BaseModel):
    """Ask for one exact stored relationship, by the reference a topology answer gave."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    context: CapabilityContext
    #: Opaque. Pass it back exactly as it was returned.
    edge_ref: str = Field(min_length=1, max_length=8192)
    #: Omitted, a new investigation is started and returned; supplied, the
    #: evidence is recorded in that one.
    investigation_id: str | None = None
    depth: FreshnessDepth = FreshnessDepth.STORED


class TopologyEdgeResponse(BaseModel):
    """One stored relationship, why RepoAtlas holds it, and the lines that assert it."""

    model_config = ConfigDict(frozen=True)

    investigation_id: str
    context: ContextView
    edge_ref: str
    #: Oriented as stored.
    edge: GraphEdge
    #: How many byte-identical rows record this fact. Duplicates are one fact.
    multiplicity: int = Field(ge=1)
    #: The resolution the linker recorded with the edge, as recorded.
    resolution: dict[str, JsonValue] | None = None
    #: The package evidence of a cross-package use, spelled as impact spells it.
    crossing: PackageCrossing | None = None
    #: The HTTP evidence of a caller-to-handler edge, spelled as impact spells it.
    http: HttpCrossing | None = None
    evidence: tuple[EvidenceItem, ...] = ()
    freshness: IndexFreshness
    ledger: LedgerSummary
