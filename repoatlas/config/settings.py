"""Configuration: Pydantic models, YAML persistence, XDG paths (V1_SPEC §3).

Normal usage requires no configuration file beyond the root recorded by ``init``.
"""

from __future__ import annotations

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
#: read, so excluded content cannot reach chunks, embeddings, prompts or logs.
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
    model_config = ConfigDict(frozen=True)

    max_depth: int = 6
    exclusions: tuple[str, ...] = DEFAULT_EXCLUSIONS
    follow_symlinks: bool = False


class IndexingSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    #: Files larger than this are recorded but never read for content.
    max_file_bytes: int = 1_048_576
    #: Fixed-window chunking parameters used when no symbol range applies.
    chunk_lines: int = 40
    chunk_overlap_lines: int = 8
    max_chunk_lines: int = 200


class StorageSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    database: Path | None = None


class LLMSettings(BaseModel):
    """Backend selection is configuration-driven; nothing is hardcoded to Ollama."""

    model_config = ConfigDict(frozen=True)

    provider: str = "ollama"
    model: str = "gpt-oss:20b"
    base_url: str = "http://localhost:11434"
    timeout_seconds: float = 120.0


class OutputSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    verbosity: str = "concise"
    max_evidence: int = 12


class GroundingSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    require_evidence: bool = True


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    repos: ReposSettings = Field(default_factory=ReposSettings)
    discovery: DiscoverySettings = Field(default_factory=DiscoverySettings)
    indexing: IndexingSettings = Field(default_factory=IndexingSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    output: OutputSettings = Field(default_factory=OutputSettings)
    grounding: GroundingSettings = Field(default_factory=GroundingSettings)

    @property
    def root(self) -> Path | None:
        return self.repos.root


def load_settings(path: Path | None = None) -> Settings:
    """Load settings, falling back to defaults when no config file exists."""
    target = path or config_path()
    if not target.exists():
        return Settings()
    raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return Settings()
    return Settings.model_validate(raw)


def save_settings(settings: Settings, path: Path | None = None) -> Path:
    """Persist settings as YAML, creating parent directories as needed."""
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = settings.model_dump(mode="json", exclude_none=True)
    target.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")
    return target
