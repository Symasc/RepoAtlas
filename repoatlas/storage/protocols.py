"""Storage interfaces (ARCHITECTURE.md §4).

Only ``storage/sqlite/`` knows SQL. Everything above depends on these Protocols,
so the backend can be replaced without touching a caller.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import NamedTuple, Protocol

from pydantic import BaseModel, ConfigDict

from repoatlas.models.dependencies import DependencyPackage, DependencyRecord
from repoatlas.models.parsing import EdgeDirection, EdgeType, EntityType, NeighborOrder
from repoatlas.models.repository import Repository


class EntityRecord(BaseModel):
    """An entity to persist. ``file_id`` set = file-scoped, ``None`` = interned."""

    model_config = ConfigDict(frozen=True)

    type: EntityType
    key: str
    name: str
    qualified_name: str | None = None
    repo_id: int | None = None
    file_id: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    signature: str | None = None
    meta_json: str | None = None


class EdgeRecord(BaseModel):
    """A relationship, provenanced by the file that asserted it (ARCHITECTURE §3)."""

    model_config = ConfigDict(frozen=True)

    src_id: int
    dst_id: int
    type: EdgeType
    confidence: float = 1.0
    repo_id: int | None = None
    file_id: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    parser: str | None = None
    #: How the global pass resolved it, as JSON, when not from literal text alone:
    #: a workspace package, a tsconfig alias, a chain of re-exports, a package another
    #: repository provides. Internal only.
    resolution: str | None = None


class SyncCompletion(BaseModel):
    """One repository's finished sync, held back until the run as a whole succeeds.

    A sync writes a repository's file facts long before the run is over, and the
    cross-repository graph is rebuilt once at the end over all of them. Recording
    "synchronized" at the first of those two moments is what let a failed link
    pass leave every repository claiming to be current (ARCHITECTURE.md §9), so
    the git state observed during the run is carried here and applied afterwards.
    """

    model_config = ConfigDict(frozen=True)

    repo_id: int
    head_sha: str | None = None
    is_dirty: bool = False
    branch: str | None = None


class ChunkRecord(BaseModel):
    """A retrievable span of a file, carrying the lines that prove it."""

    model_config = ConfigDict(frozen=True)

    file_id: int
    repo_id: int
    line_start: int
    line_end: int
    text: str
    token_estimate: int = 0
    #: Position of the line span this chunk came from, within its file.
    span_index: int = 0
    #: Position of this piece within that span, and how many pieces it became.
    #: Anything other than ``(0, 1)`` means the span exceeded the character cap
    #: and was cut, which is what seam recovery needs to know (ARCHITECTURE.md §6).
    split_index: int = 0
    split_total: int = 1


class FileFacts(BaseModel):
    """Everything one file asserts. Written atomically by ``replace_file_facts``."""

    model_config = ConfigDict(frozen=True)

    entities: tuple[EntityRecord, ...] = ()
    chunks: tuple[ChunkRecord, ...] = ()


class ChunkHit(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk_id: int
    file_id: int
    repo_id: int
    repository: str
    repository_slug: str = ""
    path: str
    line_start: int
    line_end: int
    text: str
    score: float
    #: Position of this piece within the line span it came from, and how many
    #: pieces that span became. Every piece of a cut span carries the *span's*
    #: line range (ARCHITECTURE.md §6), which is truthful at chunking's
    #: granularity but means a piece other than a whole one begins or ends
    #: mid-line: its text does not cover the range it advertises. A caller that
    #: must prove line coverage needs to see that, so it is surfaced here rather
    #: than left in the table.
    split_index: int = 0
    split_total: int = 1


class LiteralMatches(BaseModel):
    """What one exact text lookup returned, and whether it read everything it could.

    ``hits`` are the chunks that literally contain the term.
    ``exhaustive`` is whether every indexed chunk that could contain the term was
    examined. A store that stops at its limit before it has verified what it read
    must say so here, and then an empty ``hits`` is not a proof that the term is
    absent. The SQLite store bounds only rows that already contain the term
    exactly, so it is never both empty and incomplete.
    """

    model_config = ConfigDict(frozen=True)

    hits: list[ChunkHit]
    exhaustive: bool


class FileHit(BaseModel):
    """An indexed file matched by its own name or path — not by its content.

    File identity is a deterministic fact about the repository, so a hit carries
    no line range and never acquires one. ``term`` records which spelling matched
    and ``specificity`` how exactly, which is what lets a full relative path
    outrank a bare basename without either becoming a fuzzy match.
    """

    model_config = ConfigDict(frozen=True)

    file_id: int
    repo_id: int
    repository: str
    repository_slug: str = ""
    path: str
    entity_id: int | None
    term: str
    #: 0 = the term is the whole relative path, 1 = a trailing sub-path,
    #: 2 = the basename. Lower is more specific; all three are exact.
    specificity: int
    score: float


class EntityHit(BaseModel):
    model_config = ConfigDict(frozen=True)

    entity_id: int
    type: EntityType
    name: str
    qualified_name: str | None
    repository: str
    repository_slug: str = ""
    path: str | None
    line_start: int | None
    line_end: int | None
    score: float
    #: The declaration the indexer stored for this entity, empty when it stored
    #: none. It is what lets a name match be quoted rather than merely pointed at.
    signature: str = ""
    #: Which of the requested names matched, for an exact-name lookup. Empty for a
    #: full-text hit, which matched no single spelling.
    #:
    #: The same thing :attr:`FileHit.term` records, and for the same reason: a
    #: caller deciding whether the *term it asked about* exists cannot recover that
    #: from the row. Re-deriving it downstream would mean reimplementing the
    #: lookup's own predicate — name, qualified name, or interned key — somewhere
    #: it could drift out of agreement with the query that produced the row.
    matched_term: str = ""


class EntityIdentity(BaseModel):
    """An entity addressed by its natural key (ARCHITECTURE.md §3).

    ``key`` is the identity: it is derived from what the repository says, so it
    survives a reindex that renumbers every row. ``entity_id`` is a storage row id
    and is the opposite — useful inside one process, meaningless outside it.

    ``repository_slug`` and ``path`` are ``None`` for an interned entity. An
    environment variable has no location of its own, and giving it one would
    manufacture provenance for a fact that has none (V1_SPEC §11).
    """

    model_config = ConfigDict(frozen=True)

    entity_id: int
    key: str
    type: EntityType
    name: str
    qualified_name: str | None = None
    repository_slug: str | None = None
    path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    signature: str | None = None


class UnresolvedRefRecord(BaseModel):
    """A reference whose target is not resolvable from the asserting file alone.

    These rows are the durable record of a claim, retried on every link pass, so a
    repository added later retroactively gains the edges that were always implied
    (ARCHITECTURE.md §5).
    """

    model_config = ConfigDict(frozen=True)

    id: int | None = None
    repo_id: int
    file_id: int
    src_id: int
    edge_type: EdgeType
    target_kind: EntityType
    target_key: str
    line_start: int | None = None
    line_end: int | None = None
    confidence: float = 1.0
    meta_json: str | None = None


class FileRow(BaseModel):
    """One indexed file joined to its repository and its file entity."""

    model_config = ConfigDict(frozen=True)

    file_id: int
    file_entity_id: int
    repo_id: int
    repo_name: str
    #: Canonical repository identity. ``repo_name`` is for people and may repeat.
    repo_slug: str
    repo_path: Path
    rel_path: str


class SymbolRow(BaseModel):
    """A file-scoped definition, for cross-file resolution."""

    model_config = ConfigDict(frozen=True)

    entity_id: int
    file_id: int
    repo_id: int
    type: EntityType
    name: str
    qualified_name: str | None
    line_start: int | None
    #: The parser's facts about the definition (``ParsedSymbol.meta``), as stored.
    meta_json: str | None = None

    @property
    def meta(self) -> dict[str, str]:
        """``meta_json`` decoded; empty when the parser stated nothing."""
        return json.loads(self.meta_json) if self.meta_json else {}


class RouteServerRow(BaseModel):
    """A route one file serves: the handler, where it is stated, and which parser read it.

    What a deployment identity exposes is the routes a repository's code serves, so the
    global pass reads these rows -- per-file ``SERVES_HTTP_ROUTE`` edges, never its own
    output -- to tie an identity to the application that serves them.
    """

    model_config = ConfigDict(frozen=True)

    repo_id: int
    file_id: int | None = None
    src_id: int
    body: str
    parser: str | None = None
    line_start: int | None = None
    line_end: int | None = None


class HttpEdgeRow(BaseModel):
    """One edge to a shared HTTP node, with what a reader needs to locate it.

    The asserting file and lines are the edge's own, so a composed edge — stored
    with no lines — stays a row without a span rather than borrowing one.
    """

    model_config = ConfigDict(frozen=True)

    type: EdgeType
    parser: str | None = None
    src_id: int
    src_key: str
    src_name: str
    src_qualified_name: str | None = None
    target_key: str
    target_name: str
    path: str | None = None
    line_start: int | None = None
    line_end: int | None = None


class HttpCallDecisionRow(BaseModel):
    """One stored decision of the HTTP join (``http_call_decisions``), with its caller's
    file and entity for a reader to place it."""

    model_config = ConfigDict(frozen=True)

    decision_ref: str
    repo_id: int
    file_id: int
    src_id: int
    line_start: int | None = None
    line_end: int | None = None
    method: str
    url: str
    variant: str
    decision: str
    evidence_json: str
    chain_json: str
    #: Read back with the row; ignored when one is written.
    path: str | None = None
    src_key: str | None = None


class LinkDecisionRow(BaseModel):
    """One stored decision of a link family (``link_decisions``): what the global pass
    decided about one subject, anchored where the claim it decided was made."""

    model_config = ConfigDict(frozen=True)

    decision_ref: str
    #: The pass that decided it, such as ``graphql_codegen``.
    family: str
    repo_id: int
    file_id: int
    src_id: int
    line_start: int | None = None
    line_end: int | None = None
    #: The family's own spelling of what was decided.
    subject: str
    decision: str
    evidence_json: str
    #: Read back with the row; ignored when one is written.
    path: str | None = None


class NeighborEdge(BaseModel):
    """An edge reached by graph expansion, with both endpoints' repositories."""

    model_config = ConfigDict(frozen=True)

    src_id: int
    dst_id: int
    type: EdgeType
    confidence: float = 1.0
    src_repo_id: int | None = None
    dst_repo_id: int | None = None
    #: The file that asserted this edge, and where in it. An edge is provenanced
    #: by the file that claims it (ARCHITECTURE.md §3); carrying that here is what
    #: lets a caller supply the asserting lines as evidence instead of offering
    #: coordinates the reader has no way to check.
    file_id: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    parser: str | None = None
    #: The edge records the package evidence that lets it join two repositories
    #: (:data:`repoatlas.graph.packages.CROSSING`).
    evidenced: bool = False

    def other(self, entity_id: int) -> int:
        return self.dst_id if entity_id == self.src_id else self.src_id

    @property
    def crosses_repositories(self) -> bool:
        return (
            self.src_repo_id is not None
            and self.dst_repo_id is not None
            and self.src_repo_id != self.dst_repo_id
        )


#: Where, in an edge's resolution, the evidence that lets an edge of an evidenced type join
#: two repositories is recorded: a package link's crossing record
#: (repoatlas.graph.packages), and an HTTP join's (repoatlas.graph.http_crossings). A type
#: with no entry here never crosses on evidence.
EVIDENCE_PATHS: dict[EdgeType, str] = {
    EdgeType.USES: "$.crossing.package",
    EdgeType.CALLS_HTTP_HANDLER: "$.http.provider",
}


class Confinement(BaseModel):
    """Which edges the change-impact walk may follow out of the node it stands on.

    The rule :func:`repoatlas.graph.traversal.walk` applies to the edges it reads,
    stated as a value, and widened for the change-impact walk. An edge whose two ends
    belong to different repositories is followed only when its type is in
    ``crossing``, or is in ``evidenced`` and the edge itself records the evidence for
    the crossing (:data:`EVIDENCE_PATHS`); and under ``home_repo_id``, only when its far end
    belongs to that repository or to none (a shared node: an environment variable, an
    HTTP operation, a package). Under ``scope_repo_ids`` the far end must belong to
    one of those repositories or to none, whatever else admits it. :meth:`admits` is
    the rule in Python; the bounded neighbour queries render the same rule as SQL, so
    a count or a page is exact *after* confinement rather than cut before it.
    """

    model_config = ConfigDict(frozen=True)

    crossing: frozenset[EdgeType] = frozenset()
    #: Types that may join two repositories only edge by edge: when the edge records
    #: the evidence for the crossing, never on the strength of its type.
    evidenced: frozenset[EdgeType] = frozenset()
    home_repo_id: int | None = None
    #: Whether a direct crossing edge of an admitted type may leave ``home_repo_id``,
    #: making the far end's repository the new home (the change-impact walk). Off,
    #: the home holds against every edge, as it does in ``walk``.
    crossing_moves_home: bool = False
    #: The repositories the caller's scope names; ``None`` when it names them all.
    scope_repo_ids: frozenset[int] | None = None

    def admits(self, edge: NeighborEdge, *, outgoing: bool) -> bool:
        far = edge.dst_repo_id if outgoing else edge.src_repo_id
        if self.scope_repo_ids is not None and far is not None and far not in self.scope_repo_ids:
            return False
        if edge.crosses_repositories:
            crosses = edge.type in self.crossing or (edge.type in self.evidenced and edge.evidenced)
            if not crosses:
                return False
            if self.crossing_moves_home:
                return True
        return self.home_repo_id is None or far is None or far == self.home_repo_id


class NeighborRule(BaseModel):
    """Edge types followed one way: ``OUT`` from their source, ``IN`` from their target."""

    model_config = ConfigDict(frozen=True)

    edge_types: tuple[EdgeType, ...]
    direction: EdgeDirection
    #: Edge types admitted only when the far end is one of the paired entity types.
    far_types: tuple[tuple[EdgeType, tuple[EntityType, ...]], ...] = ()
    #: Leave out a file's own file-level ``REFERENCES`` (a named import) or ``CALLS`` (a
    #: call in a module-scope initializer) to an entity when a declaration in that same
    #: file (or the file itself) has a ``USES`` edge to it: the declaration-level fact
    #: says the same thing precisely.
    uses_replace_file_facts: bool = False

    @property
    def outgoing(self) -> bool:
        return self.direction is EdgeDirection.OUT


class NeighborCount(BaseModel):
    """How much one node offers under one rule, counted exactly and fetched not at all."""

    model_config = ConfigDict(frozen=True)

    near_id: int
    #: Distinct entities at the far end.
    far: int
    #: Distinct relationships (the same ends, type, file and lines count once).
    relationships: int


class NeighborCursor(BaseModel):
    """Where a page stopped, in natural order: continue strictly after it."""

    model_config = ConfigDict(frozen=True)

    type: EdgeType
    far_key: str
    file_path: str
    line_start: int
    line_end: int
    edge_id: int


class NeighborRow(BaseModel):
    """One relationship a bounded query returned, with both ends and its provenance."""

    model_config = ConfigDict(frozen=True)

    edge_id: int
    type: EdgeType
    near_id: int
    far_id: int
    #: Whether the edge was followed from its source (the near end is its source).
    outgoing: bool
    far_key: str
    far_type: EntityType
    far_name: str
    far_qualified_name: str | None = None
    far_repo_id: int | None = None
    #: The far entity's own file and span, when it is file-scoped.
    far_path: str | None = None
    far_line_start: int | None = None
    far_line_end: int | None = None
    #: The file that asserted the edge, its repository, and where.
    file_id: int | None = None
    file_repo_id: int | None = None
    file_path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    parser: str | None = None
    resolution: str | None = None

    @property
    def cursor(self) -> NeighborCursor:
        return NeighborCursor(
            type=self.type,
            far_key=self.far_key,
            file_path=self.file_path or "",
            line_start=self.line_start or 0,
            line_end=self.line_end or 0,
            edge_id=self.edge_id,
        )


class NeighborGroup(BaseModel):
    """How many far entities under one rule live in one file (``path`` ``None``: interned)."""

    model_config = ConfigDict(frozen=True)

    repo_id: int | None
    path: str | None
    far: int
    #: Far entities in all of this group's repository, counted over every group.
    repository_far: int = 0


class EntityDescription(BaseModel):
    """An entity resolved to a citable location (V1_SPEC §11)."""

    model_config = ConfigDict(frozen=True)

    entity_id: int
    repository: str
    repository_slug: str = ""
    path: str
    line_start: int
    line_end: int
    text: str
    description: str
    #: What kind of entity this is. A file reached through an edge is a claim
    #: about the file; a symbol reached the same way names lines it may hold no
    #: text for, and only the kind tells the two apart once both are described.
    entity_type: EntityType | None = None


class RepositoryStore(Protocol):
    def upsert(self, repo: Repository) -> int: ...
    def get_by_name(self, name: str) -> Repository | None: ...
    def get_by_path(self, path: Path) -> Repository | None: ...
    def list_all(self) -> list[Repository]: ...
    def delete(self, repo_id: int) -> None: ...
    def mark_sync_started(self, repo_id: int) -> None: ...
    def mark_sync_failed(self, repo_id: int) -> None: ...
    def mark_many_synced(self, completions: Sequence[SyncCompletion]) -> None: ...


class KnowledgeStore(Protocol):
    def transaction(self) -> AbstractContextManager[None]: ...
    def replace_file_facts(self, file_id: int, facts: FileFacts) -> None: ...
    def intern_entity(self, entity: EntityRecord) -> int: ...
    def add_edges(self, edges: Sequence[EdgeRecord]) -> None: ...
    def add_unresolved_refs(self, refs: Sequence[UnresolvedRefRecord]) -> None: ...
    def list_unresolved_refs(
        self, repo_id: int | None = None, *, pending_only: bool = False
    ) -> list[UnresolvedRefRecord]: ...
    def delete_edges_by_parser(self, parser: str) -> int: ...
    def list_file_rows(self, repo_id: int | None = None) -> list[FileRow]: ...
    def list_symbols(
        self, types: Sequence[EntityType] | None, repo_id: int | None = None
    ) -> list[SymbolRow]: ...
    def list_served_routes(
        self, *, exclude_parser: str, repo_id: int | None = None
    ) -> dict[int, list[str]]: ...
    def list_route_servers(
        self, *, exclude_parser: str, repo_id: int | None = None
    ) -> list[RouteServerRow]: ...
    def list_http_edges(self, repo_id: int) -> list[HttpEdgeRow]: ...
    def replace_http_call_decisions(self, rows: Sequence[HttpCallDecisionRow]) -> None: ...
    def list_http_call_decisions(self, repo_id: int | None = None) -> list[HttpCallDecisionRow]: ...
    def http_call_decision_repository(self, decision_ref: str) -> int | None: ...
    def http_call_decisions_built(self) -> bool: ...
    def list_file_parsers(self, repo_id: int) -> list[tuple[str, str | None]]: ...
    def neighbors(
        self,
        entity_ids: Sequence[int],
        edge_types: Sequence[EdgeType] | None,
        limit: int,
    ) -> list[EdgeRecord]: ...
    def expand_neighbors(
        self,
        entity_ids: Sequence[int],
        edge_types: Sequence[EdgeType] | None,
        limit: int,
        direction: EdgeDirection = EdgeDirection.ANY,
        order: NeighborOrder = NeighborOrder.CONFIDENCE,
    ) -> list[NeighborEdge]: ...
    def count_keys_with_prefix(
        self, prefix: str, *, exclude_types: Sequence[EntityType] = ()
    ) -> int: ...
    def entities_with_key_prefix(
        self, prefix: str, *, exclude_types: Sequence[EntityType] = (), limit: int
    ) -> list[EntityIdentity]: ...
    def impact_counts(
        self, near_ids: Sequence[int], rule: NeighborRule, confinement: Confinement
    ) -> dict[int, NeighborCount]: ...
    def impact_page(
        self,
        near_id: int,
        rule: NeighborRule,
        confinement: Confinement,
        *,
        after: NeighborCursor | None,
        limit: int,
    ) -> list[NeighborRow]: ...
    def impact_distribution(
        self, near_id: int, rule: NeighborRule, confinement: Confinement, *, max_groups: int
    ) -> tuple[list[NeighborGroup], bool]: ...
    def impact_rows_for(
        self,
        near_id: int,
        rule: NeighborRule,
        confinement: Confinement,
        *,
        groups: Sequence[tuple[int | None, str | None]],
        per_group: int,
    ) -> list[NeighborRow]: ...
    def count_whole_module_uses(
        self,
        package: str,
        provider_repo_id: int,
        dependent_repo_id: int,
        *,
        exclude_ids: Sequence[int] = (),
    ) -> int: ...
    def get_entities_by_key(self, keys: Sequence[str]) -> list[EntityIdentity]: ...
    def keys_for_entities(self, entity_ids: Sequence[int]) -> dict[int, EntityIdentity]: ...
    def file_locations(self, file_ids: Sequence[int]) -> dict[int, tuple[str, str]]: ...
    def list_module_facts(self, file_id: int | None = None) -> list[UnresolvedRefRecord]: ...
    def provided_manifests(self, repository_slugs: Sequence[str]) -> dict[str, tuple[str, ...]]: ...
    def chunks_for_file(self, file_id: int, line_start: int, line_end: int) -> list[ChunkHit]: ...
    def gc_orphan_entities(self) -> int: ...


class DependencyStore(Protocol):
    def replace_for_file(self, file_id: int, records: Sequence[DependencyRecord]) -> None: ...
    def providers(self, ecosystem: str, normalized_name: str) -> list[tuple[int, str]]: ...
    def list_for_repos(self, repo_ids: Sequence[int] | None) -> list[DependencyRecord]: ...
    def list_packages(self, repo_ids: Sequence[int] | None) -> list[DependencyPackage]: ...


class CrossingEdgeRow(NamedTuple):
    """A stored edge whose two endpoints belong to two different repositories."""

    edge_id: int
    type: str
    src_repo_id: int
    dst_repo_id: int
    src_key: str
    dst_key: str
    #: The file asserting the edge, and where.
    path: str | None
    line_start: int | None


class LinkEdgeRow(NamedTuple):
    """A stored edge incident to an entity under one path, seen from that entity.

    ``near`` is the end under the path; ``far`` is the other end, wherever it is.
    """

    edge_id: int
    type: str
    near_id: int
    near_path: str
    line_start: int | None
    far_repo_id: int | None
    far_path: str | None
    far_type: str
    far_key: str


class TopologyFileRow(NamedTuple):
    file_id: int
    path: str
    language: str | None


class TopologyDeclarationRow(NamedTuple):
    """An entity a file declares, other than the file itself."""

    entity_id: int
    key: str
    type: str
    name: str
    qualified_name: str | None
    line_start: int | None
    line_end: int | None


class FileMatchRow(NamedTuple):
    """A file a name search matched, and how many matched in all."""

    repo_id: int
    path: str
    name: str
    language: str | None
    #: Every matching file in scope, the same on each row.
    total: int


class NameMatchRow(NamedTuple):
    """A declared name a search matched, and how many matched in all."""

    key: str
    type: str
    name: str
    qualified_name: str | None
    #: ``None`` for an entity no repository owns, such as an environment variable.
    repo_id: int | None
    path: str | None
    line_start: int | None
    line_end: int | None
    #: Every matching name in scope, the same on each row.
    total: int


class ReferenceRow(NamedTuple):
    """A located edge leaving an entity in one file, with its far end."""

    line_start: int
    line_end: int | None
    type: str
    source_key: str
    target_key: str
    target_type: str
    target_name: str
    target_repo_id: int | None
    target_path: str | None
    target_line_start: int | None
    target_line_end: int | None


class StoredEdge(BaseModel):
    """One stored edge with every field that identifies it exactly.

    ``resolution`` is the stored text, unparsed: an exact reference to the fact has
    to be able to tell apart two rows that differ in nothing else.
    """

    model_config = ConfigDict(frozen=True)

    edge_id: int
    type: EdgeType
    src_id: int
    dst_id: int
    src_key: str
    dst_key: str
    src_repo_id: int | None = None
    dst_repo_id: int | None = None
    #: The repository and file asserting the edge, and where.
    repository_slug: str | None = None
    file_id: int | None = None
    path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    parser: str | None = None
    confidence: float = 1.0
    resolution: str | None = None


class SearchIndex(Protocol):
    """Exact and lexical lookups over the index.

    Every lookup takes an optional ``path_prefix``: a normalized
    repository-relative path naming one file or one directory. It is applied
    inside the lookup, before its limit, and it admits only facts that have a
    location beneath it.
    """

    def search_text(
        self,
        query: str,
        repo_ids: Sequence[int] | None,
        limit: int,
        *,
        path_prefix: str | None = None,
    ) -> list[ChunkHit]: ...
    def find_literal(
        self,
        term: str,
        repo_ids: Sequence[int] | None,
        limit: int,
        *,
        path_prefix: str | None = None,
    ) -> LiteralMatches: ...
    def find_files_by_path(
        self,
        terms: Sequence[str],
        repo_ids: Sequence[int] | None,
        limit: int,
        *,
        path_prefix: str | None = None,
    ) -> list[FileHit]: ...
    def has_files_under(self, repo_ids: Sequence[int], path_prefix: str) -> bool: ...
    def find_entities_by_name(
        self,
        names: Sequence[str],
        repo_ids: Sequence[int] | None,
        limit: int,
        *,
        path_prefix: str | None = None,
        module_values: bool = True,
    ) -> list[EntityHit]: ...
    def search_entities(
        self,
        query: str,
        repo_ids: Sequence[int] | None,
        types: Sequence[EntityType] | None,
        limit: int,
        *,
        path_prefix: str | None = None,
        module_values: bool = True,
    ) -> list[EntityHit]: ...
