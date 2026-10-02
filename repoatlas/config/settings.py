"""Configuration: Pydantic models, YAML persistence, XDG paths (V1_SPEC §3).

Normal usage requires no configuration file beyond the root recorded by ``init``.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

APP_NAME = "repoatlas"

#: Directories never descended into (V1_SPEC §5).
DEFAULT_EXCLUSIONS: tuple[str, ...] = (
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    "target",
    ".next",
    "coverage",
    ".cache",
    "__pycache__",
    "vendor",
)

#: Files never read (V1_SPEC §17). Applied at enumeration, before any content is
#: read, so excluded content cannot reach chunks, prompts or logs.
SECRET_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "id_rsa",
    "id_ed25519",
)


def _xdg_dir(env_var: str, default: str) -> Path:
    raw = os.environ.get(env_var)
    base = Path(raw) if raw else Path.home() / default
    return base / APP_NAME


def config_path() -> Path:
    """``$XDG_CONFIG_HOME/repoatlas/config.yaml``."""
    override = os.environ.get("REPOATLAS_CONFIG")
    if override:
        return Path(override)
    return _xdg_dir("XDG_CONFIG_HOME", ".config") / "config.yaml"


def database_path(settings: Settings | None = None) -> Path:
    """``$XDG_DATA_HOME/repoatlas/atlas.db``, or the configured override."""
    if settings is not None and settings.storage.database is not None:
        return settings.storage.database
    override = os.environ.get("REPOATLAS_DB")
    if override:
        return Path(override)
    return _xdg_dir("XDG_DATA_HOME", ".local/share") / "atlas.db"


class ReposSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    root: Path | None = None


class DiscoverySettings(BaseModel):
    """How the managed tree is walked (V1_SPEC §5).

    There is deliberately **no depth bound**. A ``max_depth`` of 6 used to live
    here, and every repository below it was silently absent from an ``init`` that
    still reported success. No finite value is correct — any ceiling is the same
    defect with a different number — so the walk is bounded by the tree, the
    exclusions and the ignore rules instead, and terminates on directory identity
    rather than on a counter (see :mod:`repoatlas.discovery.finder`).
    """

    model_config = ConfigDict(frozen=True)

    exclusions: tuple[str, ...] = DEFAULT_EXCLUSIONS
    #: Off by default, and containment-checked when on: a followed symlink must
    #: still resolve inside the managed root.
    follow_symlinks: bool = False


class IndexingSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    #: Inclusive maximum on a file's size for its **content** to be indexed. A
    #: file of exactly this many bytes is read; one byte more is recorded as
    #: oversized and never allocated (ARCHITECTURE.md §12).
    #:
    #: Bounded below at zero because it is a memory bound, and a negative one has
    #: no meaning: it would make every file oversized while reading as though the
    #: operator had asked for something. Zero is legal and does exactly what it
    #: says — only an empty file has content within it.
    max_file_bytes: int = Field(default=1_048_576, ge=0)
    #: Fixed-window chunking parameters used when no symbol range applies.
    chunk_lines: int = 40
    chunk_overlap_lines: int = 8
    max_chunk_lines: int = 200
    #: Hard character cap on a single chunk. `max_chunk_lines` bounds how many
    #: lines a chunk spans, which says nothing about its size: minified
    #: JavaScript, embedded base64 and generated one-liners are all a single line.
    #: Without this, one chunk can carry a whole file into the token budget
    #: (V1_SPEC §14). ~8000 characters is roughly 2000 tokens — a third of the
    #: default evidence budget, so even the worst single candidate leaves room.
    max_chunk_chars: int = 8000


class StorageSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    database: Path | None = None
    #: How long a second ``init``/``sync`` waits for the writer lock before
    #: reporting that a synchronization is already running (ARCHITECTURE.md §9).
    #: Zero refuses immediately; the wait is always bounded.
    lock_timeout_seconds: float = 30.0


class RetrievalSettings(BaseModel):
    """Bounds and biases of the retrieval funnel (ARCHITECTURE.md §10)."""

    model_config = ConfigDict(frozen=True)

    #: Per-source cap, applied before fusion.
    per_source: int = 30
    #: Hard cap on the evidence set.
    final: int = 12
    #: Second bound, on tokens rather than items (V1_SPEC §14).
    token_budget: int = 6000
    #: Graph expansion, bounded on both depth and breadth.
    graph_hops: int = 2
    graph_fanout: int = 25
    graph_seeds: int = 20
    #: Additive bias toward the repository the question was asked in (V1_SPEC §6).
    primary_boost: float = 0.5


class Settings(BaseModel):
    """The whole of RepoAtlas configuration.

    An unknown top-level key is ignored rather than refused, and absent on save:
    :func:`save_settings` writes this model, so a round-trip drops it. That is
    tolerance, not validation --- a misspelled section is ignored as silently as
    a retired one.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    repos: ReposSettings = Field(default_factory=ReposSettings)
    discovery: DiscoverySettings = Field(default_factory=DiscoverySettings)
    indexing: IndexingSettings = Field(default_factory=IndexingSettings)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)

    @property
    def root(self) -> Path | None:
        return self.repos.root


def _env_overrides(settings: Settings) -> Settings:
    """Apply the few settings a container must be able to set without a file.

    Deliberately narrow. Configuration lives in YAML; this exists for a process
    that has no config file to edit. ``REPOATLAS_CONFIG`` and ``REPOATLAS_DB`` are
    handled at path resolution for the same reason.
    """
    updates: dict[str, Any] = {}

    # A container orchestrator decides how long an overlapping sync may wait, and
    # it has no config file to edit. Ignored if unparseable rather than fatal: a
    # bad value must not stop indexing, only lose the override.
    timeout = os.environ.get("REPOATLAS_SYNC_LOCK_TIMEOUT")
    if timeout:
        with contextlib.suppress(ValueError):
            updates["storage"] = settings.storage.model_copy(
                update={"lock_timeout_seconds": max(float(timeout), 0.0)}
            )

    return settings.model_copy(update=updates) if updates else settings


def load_settings(path: Path | None = None) -> Settings:
    """Load settings, falling back to defaults when no config file exists."""
    target = path or config_path()
    if not target.exists():
        return _env_overrides(Settings())
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return _env_overrides(Settings())
    return _env_overrides(Settings.model_validate(raw))


def save_settings(settings: Settings, path: Path | None = None) -> Path:
    """Persist settings as YAML, creating parent directories as needed."""
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = settings.model_dump(mode="json", exclude_none=True)
    target.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")
    return target
