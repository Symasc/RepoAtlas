"""Repository, file and file-state models (ARCHITECTURE.md §5)."""

from __future__ import annotations

import enum
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class SyncStatus(enum.StrEnum):
    """Outcome of the last sync run for a repository.

    ``INTERRUPTED`` is load-bearing: per V1_SPEC §12 a sync that did not complete
    cleanly disables the stat-based hashing shortcut on the next run.
    """

    NEVER = "never"
    OK = "ok"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


class Repository(BaseModel):
    """A managed Git repository and everything known about its indexed state."""

    model_config = ConfigDict(frozen=True)

    id: int | None = None
    name: str
    path: Path
    branch: str | None = None
    head_sha: str | None = None
    is_dirty: bool = False
    languages: tuple[str, ...] = ()
    manifests: tuple[str, ...] = ()
    lockfiles: tuple[str, ...] = ()
    last_indexed_at: datetime | None = None
    last_indexed_head: str | None = None
    last_indexed_dirty: bool | None = None
    last_sync_status: SyncStatus = SyncStatus.NEVER

    @property
    def is_indexed(self) -> bool:
        return self.last_indexed_at is not None


class FileState(BaseModel):
    """The unit of the sync diff: what we recorded about a file last time.

    ``content_hash`` is the sole authority on whether a file changed; ``size`` and
    ``mtime_ns`` only ever permit *skipping* the hash (V1_SPEC §12).
    """

    model_config = ConfigDict(frozen=True)

    file_id: int
    size: int
    mtime_ns: int
    content_hash: str
    parser_version: str


class IndexedFile(BaseModel):
    """A file as persisted after a successful parse."""

    model_config = ConfigDict(frozen=True)

    id: int | None = None
    rel_path: str
    size: int
    mtime_ns: int
    content_hash: str
    language: str | None = None
    parser: str | None = None
    parser_version: str = ""
    is_binary: bool = False
    indexed_at: datetime | None = None


class RepositoryCandidate(BaseModel):
    """A Git repository found by discovery, before it becomes managed."""

    model_config = ConfigDict(frozen=True)

    name: str
    path: Path
    languages: tuple[str, ...] = Field(default=())
    manifests: tuple[str, ...] = Field(default=())
    lockfiles: tuple[str, ...] = Field(default=())
