"""Command output models (V1_SPEC §16).

Renderers consume these; ``--json`` emits them directly.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from repoatlas.models.parsing import ParseDiagnostic
from repoatlas.models.telemetry import LinkTrace


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
    dependencies: int = 0
    full_rehash: bool = False
    rehash_reason: str | None = None
    #: Git could not establish this repository's file set, so nothing was indexed
    #: from it and it is not current. Distinct from "synced and found nothing":
    #: a zero-count report is a fact about the tree, this is the absence of one
    #: (ARCHITECTURE.md §6).
    enumeration_failed: bool = False
    diagnostics: tuple[ParseDiagnostic, ...] = ()

    @property
    def touched(self) -> bool:
        return bool(self.changed or self.new or self.deleted)


class SyncReport(BaseModel):
    """A sync run, including the one global link pass that closes it.

    The link counts are per run rather than per repository: the pass is global by
    necessity, because a change in one repository can create or invalidate an edge
    in another (ARCHITECTURE.md §6).
    """

    model_config = ConfigDict(frozen=True)

    repositories: tuple[RepositorySyncReport, ...] = ()
    scope: str = ""
    linked_references: int = 0
    unresolved_references: int = 0
    cross_repository_edges: int = 0
    embedded_chunks: int = 0
    pending_embeddings: int = 0
    embedding_detail: str = ""
    link_trace: LinkTrace | None = None
    #: Canonical identities of repositories this run discovered and registered for
    #: the first time. Empty on an ordinary run; non-empty is the machine-readable
    #: form of "the managed root gained repositories since the last sync"
    #: (V1_SPEC §12).
    repositories_added: tuple[str, ...] = ()
    #: Directories discovery could not read, so could not search for repositories.
    #: Empty on every ordinary run. Present because "there were no repositories
    #: there" and "we were refused permission to look" are different claims, and
    #: reporting the second as the first is how a discovery gap goes unnoticed
    #: (V1_SPEC §5).
    unreadable_paths: tuple[Path, ...] = ()

    @property
    def unavailable(self) -> tuple[str, ...]:
        """Repositories whose file set git could not establish, so nothing was indexed.

        A run containing one of these is not a success, however well the others
        went: RepoAtlas was asked to synchronize them and could not. The CLI turns
        a non-empty tuple into a non-zero exit so a scripted caller sees it, and
        the healthy repositories in the same run keep their facts either way
        (V1_SPEC §12).
        """
        return tuple(r.repository for r in self.repositories if r.enumeration_failed)

    @property
    def files_indexed(self) -> int:
        return sum(r.files_indexed for r in self.repositories)

    @property
    def symbols(self) -> int:
        return sum(r.symbols for r in self.repositories)

    @property
    def relationships(self) -> int:
        return sum(r.relationships for r in self.repositories) + self.linked_references

    @property
    def dependencies(self) -> int:
        return sum(r.dependencies for r in self.repositories)


class RepositoryStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    #: Canonical identity, shown when the display name does not identify one
    #: repository (ARCHITECTURE.md §3).
    slug: str = ""
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
    #: Canonical identities of repositories that exist beneath the managed root
    #: and are **not registered**. The next global ``sync`` will register and index
    #: them; until then the workspace is not current, whatever every registered
    #: repository individually reports (V1_SPEC §12).
    new_repositories: tuple[str, ...] = ()
    #: Registered repositories whose directory is gone. The next global ``sync``
    #: retires these.
    missing_repositories: tuple[str, ...] = ()
    #: Identities claimed by both a registered repository that still exists and a
    #: different repository beneath the managed root. Named rather than silently
    #: skipped: `sync` cannot register one without merging two repositories, so it
    #: leaves it alone, and a repository that is neither indexed nor mentioned is
    #: the invisibility this reporting exists to prevent (ARCHITECTURE.md §3).
    conflicting_repositories: tuple[str, ...] = ()
    #: Directories discovery was refused. Their contents could not be searched for
    #: repositories, so completeness is unestablished rather than confirmed — which
    #: is a different statement from "current" and is printed as one (V1_SPEC §5).
    unreadable_paths: tuple[Path, ...] = ()
    index_state: str = "unknown"
