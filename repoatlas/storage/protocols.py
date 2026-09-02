"""Storage interfaces (ARCHITECTURE.md §4).

Only ``storage/sqlite/`` knows SQL. Everything above depends on these Protocols,
so the backend can be replaced without touching a caller.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from repoatlas.models.dependencies import DependencyRecord, DependencyView
from repoatlas.models.parsing import EdgeType, EntityType
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
    path: str
    line_start: int
    line_end: int
    text: str
    score: float


class VectorHit(BaseModel):
    """A chunk reached by vector similarity. ``score`` is cosine, in [-1, 1]."""

    model_config = ConfigDict(frozen=True)

    chunk_id: int
    file_id: int
    repo_id: int
    repository: str
    path: str
    line_start: int
    line_end: int
    text: str
    score: float


class EntityHit(BaseModel):
    model_config = ConfigDict(frozen=True)

    entity_id: int
    type: EntityType
    name: str
    qualified_name: str | None
    repository: str
    path: str | None
    line_start: int | None
    line_end: int | None
    score: float


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


class NeighborEdge(BaseModel):
    """An edge reached by graph expansion, with both endpoints' repositories."""

    model_config = ConfigDict(frozen=True)

    src_id: int
    dst_id: int
    type: EdgeType
    confidence: float = 1.0
    src_repo_id: int | None = None
    dst_repo_id: int | None = None

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
    path: str
    line_start: int
    line_end: int
    text: str
    description: str


class RepositoryStore(Protocol):
    def upsert(self, repo: Repository) -> int: ...
    def get_by_name(self, name: str) -> Repository | None: ...
    def get_by_path(self, path: Path) -> Repository | None: ...
    def list_all(self) -> list[Repository]: ...
    def delete(self, repo_id: int) -> None: ...


class FileStore(Protocol):
    def list_states(self, repo_id: int) -> dict[str, FileState]: ...
    def upsert(self, repo_id: int, file: IndexedFile) -> int: ...
    def delete_many(self, file_ids: Sequence[int]) -> None: ...


class KnowledgeStore(Protocol):
    def replace_file_facts(self, file_id: int, facts: FileFacts) -> None: ...
    def intern_entity(self, entity: EntityRecord) -> int: ...
    def add_edges(self, edges: Sequence[EdgeRecord]) -> None: ...
    def add_unresolved_refs(self, refs: Sequence[UnresolvedRefRecord]) -> None: ...
    def list_unresolved_refs(self) -> list[UnresolvedRefRecord]: ...
    def delete_edges_by_parser(self, parser: str) -> int: ...
    def list_file_rows(self) -> list[FileRow]: ...
    def list_symbols(self, types: Sequence[EntityType] | None) -> list[SymbolRow]: ...
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
    ) -> list[NeighborEdge]: ...
    def gc_orphan_entities(self) -> int: ...


class DependencyStore(Protocol):
    def replace_for_file(self, file_id: int, records: Sequence[DependencyRecord]) -> None: ...
    def providers(self, ecosystem: str, normalized_name: str) -> list[tuple[int, str]]: ...
    def list_for_repos(self, repo_ids: Sequence[int] | None) -> list[DependencyRecord]: ...
    def join_view(self, repo_ids: Sequence[int] | None) -> list[DependencyView]: ...


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
    ) -> list[VectorHit]: ...


class SearchIndex(Protocol):
    def search_text(
        self, query: str, repo_ids: Sequence[int] | None, limit: int
    ) -> list[ChunkHit]: ...
    def search_literal(
        self, term: str, repo_ids: Sequence[int] | None, limit: int
    ) -> list[ChunkHit]: ...
    def find_entities_by_name(
        self, names: Sequence[str], repo_ids: Sequence[int] | None, limit: int
    ) -> list[EntityHit]: ...
    def search_entities(
        self,
        query: str,
        repo_ids: Sequence[int] | None,
        types: Sequence[EntityType] | None,
        limit: int,
    ) -> list[EntityHit]: ...
