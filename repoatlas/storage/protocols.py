"""Storage interfaces (ARCHITECTURE.md §4).

Only ``storage/sqlite/`` knows SQL. Everything above depends on these Protocols,
so the backend can be replaced without touching a caller.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from repoatlas.models.dependencies import DependencyPackage, DependencyRecord
from repoatlas.models.parsing import EdgeDirection, EdgeType, EntityType, NeighborOrder
from repoatlas.models.repository import FileState, IndexedFile, Repository


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
    """A retrievable span of a file, carrying the lines that prove it.

    ``content_hash`` is the key vectors are stored under, so text that survives a
    reparse unchanged is never embedded twice (V1_SPEC §20).
    """

    model_config = ConfigDict(frozen=True)

    file_id: int
    repo_id: int
    line_start: int
    line_end: int
    text: str
    token_estimate: int = 0
    content_hash: str = ""
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

    ``hits`` is exactly what :meth:`SearchIndex.search_literal` returns.
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


class VectorHit(BaseModel):
    """A chunk reached by vector similarity. ``score`` is cosine, in [-1, 1]."""

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
    #: The same cut provenance :class:`ChunkHit` carries, and for the same
    #: reason. A vector hit is a chunk like any other, so a piece of a cut span
    #: can be reached semantically exactly as it can be reached lexically; a
    #: caller that must prove line coverage needs to see that on both routes or
    #: the weaker one becomes the way around the rule.
    split_index: int = 0
    split_total: int = 1


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

    def other(self, entity_id: int) -> int:
        return self.dst_id if entity_id == self.src_id else self.src_id

    @property
    def crosses_repositories(self) -> bool:
        return (
            self.src_repo_id is not None
            and self.dst_repo_id is not None
            and self.src_repo_id != self.dst_repo_id
        )


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


class FileStore(Protocol):
    def list_states(self, repo_id: int) -> dict[str, FileState]: ...
    def states_for(self, repo_id: int, rel_paths: Sequence[str]) -> dict[str, FileState]: ...
    def upsert(self, repo_id: int, file: IndexedFile) -> int: ...
    def delete_many(self, file_ids: Sequence[int]) -> None: ...
    def count_outdated(self, repo_id: int, parser_version: str) -> int: ...


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
    def list_http_edges(self, repo_id: int) -> list[HttpEdgeRow]: ...
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
    def get_entities_by_key(self, keys: Sequence[str]) -> list[EntityIdentity]: ...
    def keys_for_entities(self, entity_ids: Sequence[int]) -> dict[int, EntityIdentity]: ...
    def chunks_for_file(self, file_id: int, line_start: int, line_end: int) -> list[ChunkHit]: ...
    def gc_orphan_entities(self) -> int: ...


class DependencyStore(Protocol):
    def replace_for_file(self, file_id: int, records: Sequence[DependencyRecord]) -> None: ...
    def providers(self, ecosystem: str, normalized_name: str) -> list[tuple[int, str]]: ...
    def list_for_repos(self, repo_ids: Sequence[int] | None) -> list[DependencyRecord]: ...
    def list_packages(self, repo_ids: Sequence[int] | None) -> list[DependencyPackage]: ...


class VectorIndex(Protocol):
    """Semantic neighbours of a query vector.

    The V1 implementation is brute-force NumPy cosine; the interface exists so
    ``sqlite-vec`` or FAISS can replace it without touching a caller
    (ARCHITECTURE.md §3).
    """

    def upsert_many(
        self, model: str, dim: int, vectors: Sequence[tuple[str, Sequence[float]]]
    ) -> int: ...
    def missing_hashes(self, model: str, repo_ids: Sequence[int] | None) -> list[str]: ...
    def texts_for_hashes(self, hashes: Sequence[str]) -> list[tuple[str, str]]: ...
    def gc_orphan_vectors(self) -> int: ...
    def count(self, model: str | None) -> int: ...
    def query(
        self,
        vector: Sequence[float],
        model: str,
        repo_ids: Sequence[int] | None,
        limit: int,
        floor: float,
        *,
        path_prefix: str | None = None,
    ) -> list[VectorHit]: ...


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
    def search_literal(
        self,
        term: str,
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
    ) -> list[EntityHit]: ...
    def search_entities(
        self,
        query: str,
        repo_ids: Sequence[int] | None,
        types: Sequence[EntityType] | None,
        limit: int,
        *,
        path_prefix: str | None = None,
    ) -> list[EntityHit]: ...
