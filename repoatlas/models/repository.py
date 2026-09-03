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


def repository_slug(path: Path, root: Path | None) -> str:
    """The canonical identity of a repository: its path relative to the root.

    Three different things are wanted from a repository, and one field cannot be
    all of them (ARCHITECTURE.md §3):

    * **identity** — this value, unambiguous and machine-facing;
    * **path** — where the repository currently sits on this host;
    * **display name** — the basename, which is for people and may repeat.

    A root-relative path is chosen over the alternatives for reasons that matter:

    * a **database row id** is allocation order, so a clean reindex of the same
      tree would produce different identities and break the equivalence invariant
      of §9 by construction;
    * a **UUID** has the same problem and is meaningless in a citation — the model
      and the user both have to be told which repository evidence came from, and
      neither can read a UUID;
    * a **remote URL** may be absent, shared between clones, mutable, or
      unreachable, and RepoAtlas must work on a local repository with no remote;
    * the **basename** is exactly what this function exists to stop being used.

    A root-relative path is unique by construction — a filesystem cannot hold two
    directories at one path — deterministic, reproducible by a fresh index of the
    same tree, independent of discovery order, and readable. It also scales past
    two duplicates: ``apps/api`` and ``backend/api`` differ for the same reason
    ``teamA/common`` and ``teamB/common`` do.

    Case is preserved, because ``Common`` and ``common`` are different directories
    on a case-sensitive filesystem. Package-name normalization rules deliberately
    do not apply here: they are about matching declared names, not about identity.

    A repository that *is* the root gets ``"."``; one outside the root falls back
    to its absolute path, which is the only unique and deterministic identity
    available in that case.
    """
    resolved = path.expanduser().resolve()
    if root is not None:
        try:
            relative = resolved.relative_to(root.expanduser().resolve())
        except ValueError:
            relative = None
        if relative is not None:
            return relative.as_posix()
    return resolved.as_posix()


class Repository(BaseModel):
    """A managed Git repository and everything known about its indexed state."""

    model_config = ConfigDict(frozen=True)

    id: int | None = None
    #: Human-readable name. Usually the directory basename, and **not unique**:
    #: two managed repositories may legitimately both be called ``common``.
    #: Never used as identity (ARCHITECTURE.md §3).
    name: str
    #: Canonical identity. See :func:`repository_slug`. Empty only for a row
    #: written before the identity column existed; it is backfilled on first sync.
    slug: str = ""
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
    slug: str = ""
    path: Path
    languages: tuple[str, ...] = Field(default=())
    manifests: tuple[str, ...] = Field(default=())
    lockfiles: tuple[str, ...] = Field(default=())
