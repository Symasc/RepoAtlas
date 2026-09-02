"""Parser contract models (ARCHITECTURE.md §5, V1_SPEC §7).

The load-bearing decision here: a parser emits *unresolved* references carrying a
raw target key, never an entity id. Resolution happens later in the linker, which
keeps parsers pure functions of ``(path, content)``.
"""

from __future__ import annotations

import enum

from pydantic import BaseModel, ConfigDict, Field


class EntityType(enum.StrEnum):
    """Entity types in the knowledge graph (V1_SPEC §8).

    Deliberately small. Add a member only when a concrete retrieval need requires
    distinguishing it.
    """

    REPOSITORY = "repository"
    FILE = "file"
    MODULE = "module"
    FUNCTION = "function"
    CLASS = "class"
    METHOD = "method"
    SYMBOL = "symbol"
    SCRIPT = "script"
    COMMAND = "command"
    PACKAGE = "package"
    SALT_STATE = "salt_state"
    TEMPLATE = "template"
    CONFIG = "config"
    ENV_VAR = "env_var"
    SERVICE = "service"
    TEST = "test"


class EdgeType(enum.StrEnum):
    """Relationship types in the knowledge graph (V1_SPEC §8)."""

    CONTAINS = "CONTAINS"
    DEFINES = "DEFINES"
    IMPORTS = "IMPORTS"
    EXPORTS = "EXPORTS"
    REFERENCES = "REFERENCES"
    CALLS = "CALLS"
    DEPENDS_ON = "DEPENDS_ON"
    EXECUTES = "EXECUTES"
    SOURCES = "SOURCES"
    INCLUDES = "INCLUDES"
    REQUIRES = "REQUIRES"
    WATCHES = "WATCHES"
    READS = "READS"
    WRITES = "WRITES"
    CONFIGURES = "CONFIGURES"
    MANAGES = "MANAGES"
    GENERATES = "GENERATES"


class Severity(enum.StrEnum):
    WARNING = "warning"
    ERROR = "error"


class ParseDiagnostic(BaseModel):
    """A parse problem. Never aborts a sync (V1_SPEC §7)."""

    model_config = ConfigDict(frozen=True)

    severity: Severity
    message: str
    line: int | None = None


class ParsedSymbol(BaseModel):
    """A definition the parser found, with the line range that proves it."""

    model_config = ConfigDict(frozen=True)

    kind: EntityType
    name: str
    qualified_name: str
    line_start: int
    line_end: int
    signature: str | None = None
    parent: str | None = None
    meta: dict[str, str] = Field(default_factory=dict)


class ParsedReference(BaseModel):
    """An unresolved reference from this file to something else.

    ``target_key`` is raw text as written in the source (a module path, a package
    name, an environment variable name). The linker resolves it; the parser never
    guesses what it points at.
    """

    model_config = ConfigDict(frozen=True)

    edge_type: EdgeType
    target_kind: EntityType
    target_key: str
    line_start: int
    line_end: int
    confidence: float = 1.0
    source_symbol: str | None = None
    meta: dict[str, str] = Field(default_factory=dict)


class ParsedFile(BaseModel):
    """Everything one parser deterministically extracted from one file."""

    model_config = ConfigDict(frozen=True)

    language: str
    parser: str
    parser_version: str
    symbols: tuple[ParsedSymbol, ...] = ()
    references: tuple[ParsedReference, ...] = ()
    diagnostics: tuple[ParseDiagnostic, ...] = ()
