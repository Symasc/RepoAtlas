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


class EdgeDirection(enum.StrEnum):
    """Which end of an edge the queried entity is required to sit on.

    Edges are directed facts — ``a IMPORTS b`` is not ``b IMPORTS a`` — but a
    neighbourhood query is often about either end. ``ANY`` is that query and stays
    the default, so asking for a direction is a deliberate narrowing rather than
    something a caller has to opt out of.
    """

    ANY = "any"
    #: The queried entity is the edge's source: what it reaches.
    OUT = "out"
    #: The queried entity is the edge's target: what reaches it.
    IN = "in"


class NeighborOrder(enum.StrEnum):
    """Which order the store returns one hop's edges in.

    It matters only because a bounded hop is a *cut*, and whatever comes back
    first is what survives it.

    ``CONFIDENCE`` is the retrieval order and stays the default: the funnel reads
    traversal order as a proximity signal, so the strongest edges must arrive
    first and ties fall to row id. That tiebreak is insertion order, which is fine
    for a stage about to fuse everything anyway and wrong for an answer a caller
    reads directly — two indexes built from the same repositories in a different
    file order would cut different edges, and neither would be wrong.

    ``NATURAL`` orders on facts about the repositories instead, so the same
    question against the same repositories survives a rebuild unchanged.
    """

    CONFIDENCE = "confidence"
    NATURAL = "natural"


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
    """A definition the parser found, with the line range that proves it.

    The span is optional, and exactly one thing makes it absent: the definition is
    the **whole of an empty file**. An empty ``__init__.py`` is a real, importable
    module and must stay in the graph, but it has no lines, so it makes no claim
    about any. Every other symbol comes from source that occupies lines and always
    carries them.

    ``None`` rather than a sentinel line number because that is already what a
    fact about a file rather than a range inside it looks like everywhere else —
    the ``file`` entity, and :class:`~repoatlas.models.evidence.Evidence`'s
    path-only citation. Anchoring an empty file at ``1-1`` instead would name a
    line the file does not contain, which is the defect a whole-file span is
    computed carefully to avoid (ARCHITECTURE.md §11).
    """

    model_config = ConfigDict(frozen=True)

    kind: EntityType
    name: str
    qualified_name: str
    line_start: int | None
    line_end: int | None
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
