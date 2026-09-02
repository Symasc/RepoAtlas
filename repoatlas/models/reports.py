"""Command output models (V1_SPEC §16).

Renderers consume these; ``--json`` emits them directly.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from repoatlas.models.parsing import ParseDiagnostic


class RepositorySyncReport(BaseModel):
    """What one repository's sync did."""

    model_config = ConfigDict(frozen=True)

    repository: str
    branch: str | None = None
    is_dirty: bool = False
    unchanged: int = 0
    changed: int = 0
    new: int = 0
    deleted: int = 0
    files_indexed: int = 0
    symbols: int = 0
    relationships: int = 0
    full_rehash: bool = False
    rehash_reason: str | None = None
    diagnostics: tuple[ParseDiagnostic, ...] = ()

    @property
    def touched(self) -> bool:
        return bool(self.changed or self.new or self.deleted)


class SyncReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    repositories: tuple[RepositorySyncReport, ...] = ()
    scope: str = ""

    @property
    def files_indexed(self) -> int:
        return sum(r.files_indexed for r in self.repositories)

    @property
    def symbols(self) -> int:
        return sum(r.symbols for r in self.repositories)

    @property
    def relationships(self) -> int:
        return sum(r.relationships for r in self.repositories)


class RepositoryStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    path: Path
    branch: str | None = None
    head_sha: str | None = None
    is_dirty: bool = False
    languages: tuple[str, ...] = ()
    indexed_files: int = 0
    symbols: int = 0
    relationships: int = 0
    last_sync: datetime | None = None
    index_state: str = "unknown"


class StatusReport(BaseModel):
    """Repository-scoped or global status (V1_SPEC §16)."""

    model_config = ConfigDict(frozen=True)

    root: Path
    scope: str
    repositories: tuple[RepositoryStatus, ...] = Field(default=())
    repository_count: int = 0
    indexed_files: int = 0
    symbols: int = 0
    relationships: int = 0
    last_sync: datetime | None = None
    index_state: str = "unknown"
