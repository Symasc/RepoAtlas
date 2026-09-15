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


#: Safe wording for the failures a third-party exception used to describe.
#:
#: A parse problem is reported to whoever asked for the sync, and a sync response
#: is a public machine surface. What produced the failure is a parser, a grammar
#: loader or a configuration library, and the text those raise is theirs: PyYAML
#: renders the offending source line into its message, ``configparser`` quotes the
#: line verbatim, ``SyntaxError.msg`` quotes the token it tripped on, and a
#: grammar loader names the shared object it could not open. None of that is a
#: contract, all of it is attacker- or host-influenced, and repeating it turns a
#: diagnostic into a second way to read the repository and the machine under it.
#:
#: So RepoAtlas writes the message and takes nothing from the exception but a line
#: number. The categories below are the distinctions RepoAtlas already drew, kept
#: because "the grammar is missing" and "this file is not valid TOML" call for
#: different actions; what is gone is the library's commentary underneath them.
#: They are constants rather than literals at each site because the same failure
#: is reported from several parsers, and wording that drifts between them is
#: wording a caller cannot match on.
GRAMMAR_UNAVAILABLE = "parser grammar unavailable"
SYNTAX_ERROR = "syntax error"
PARSE_FAILED = "parse failed"
INVALID_JSON = "invalid JSON"
INVALID_YAML = "invalid YAML"
INVALID_TOML = "invalid TOML"
INVALID_CFG = "invalid cfg"

#: A language RepoAtlas recognizes but deliberately does not parse structurally.
#:
#: Distinct from :data:`GRAMMAR_UNAVAILABLE` on purpose. That one means a grammar
#: RepoAtlas ships could not be loaded --- a broken installation, worth fixing.
#: This one is policy: no grammar is loaded at all, because none can be shipped
#: safely (OFF-01, Ruby). The file is still discovered, hashed and chunked for text
#: search; it just yields no symbols and no references. Like every constant here it
#: is written by RepoAtlas and repeats nothing from the file.
STRUCTURAL_PARSING_DISABLED = "structural parsing disabled"

#: Safe wording for an enumeration that git could not complete.
#:
#: The same rule as above, applied to the other source of arbitrary text on this
#: model: a child process. ``git ls-files`` reports a directory it could not open
#: as a *warning* on stderr and still exits 0, so those lines are the only
#: evidence that a short listing is not a complete one --- and they are free-form
#: prose, not a protocol. Git writes them about whatever it was pointed at, and
#: part of what points it is ``.git/config``, which belongs to the repository
#: rather than to RepoAtlas. A repository can therefore choose what git says: a
#: repo-local ``core.excludesFile`` naming an unreadable absolute path was
#: measured putting that path, verbatim, on a public sync response.
#:
#: Git's *stdout* here is the opposite case and is parsed as what it is --- a
#: NUL-delimited list of repository-relative paths, a defined machine format that
#: enumeration depends on. The distinction is contract, not origin: stdout has
#: one, stderr does not.
#:
#: The fact worth reporting survives in full, because it was never the text: a
#: successful sync must not be readable as a complete one. Which directory could
#: not be read is reported separately, in ``SyncResponse.unreadable_paths``, from
#: RepoAtlas's own walk rather than from anything git said.
ENUMERATION_INCOMPLETE = (
    "enumeration incomplete: git could not read part of this repository, "
    "so files beneath it are not indexed"
)


class ParseDiagnostic(BaseModel):
    """A parse problem. Never aborts a sync (V1_SPEC §7).

    ``message`` is always written by RepoAtlas. It may describe *what* could not
    be understood and, through ``line`` and ``path``, *where* --- and it never
    repeats text a parser, grammar loader or configuration library produced. See
    the constants above for why that is a rule rather than a habit.

    ``path`` is the repository-relative path the sync **admitted**, stamped on by
    the engine that already knows it. It is deliberately not taken from an
    exception: a library that happens to mention a filename is naming whatever it
    was handed, which on some paths is an absolute host path, and a coordinate is
    only trustworthy when it comes from the identity RepoAtlas itself resolved.
    ``None`` means the diagnostic is about the repository rather than about one
    file --- enumeration being incomplete, say.
    """

    model_config = ConfigDict(frozen=True)

    severity: Severity
    message: str
    line: int | None = None
    path: str | None = None


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
