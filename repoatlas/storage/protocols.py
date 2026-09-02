"""Storage interfaces (ARCHITECTURE.md §4).

Only ``storage/sqlite/`` knows SQL. Everything above depends on these Protocols,
so the backend can be replaced without touching a caller.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

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
    """A retrievable span of a file, carrying the lines that prove it."""

    model_config = ConfigDict(frozen=True)

    file_id: int
    repo_id: int
    line_start: int
    line_end: int
    text: str
    token_estimate: int = 0


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
    def neighbors(
        self,
        entity_ids: Sequence[int],
        edge_types: Sequence[EdgeType] | None,
        limit: int,
    ) -> list[EdgeRecord]: ...
    def gc_orphan_entities(self) -> int: ...


class SearchIndex(Protocol):
    def search_text(
        self, query: str, repo_ids: Sequence[int] | None, limit: int
    ) -> list[ChunkHit]: ...
    def search_entities(
        self,
        query: str,
        repo_ids: Sequence[int] | None,
        types: Sequence[EntityType] | None,
        limit: int,
    ) -> list[EntityHit]: ...
