"""Wire models for the ``impact`` capability: what a change to one thing may reach.

``impact`` starts from the change -- a declaration, a configuration key, a file or a
directory -- and returns, in rank order, the things that depend on it, each with the
path that connects it and the indexed fact behind every step of that path. RepoAtlas
owns the policy (which relationships propagate a change, in which direction, how far,
and in what order); the reader owns the judgement, by reading the source each step
locates. So the response carries no score and no confidence: a candidate is ranked,
not rated, and a ranking is not a claim that it is affected.

Three kinds of answer are kept apart in the types rather than in prose:

* **exact candidates** (:attr:`ImpactResponse.candidates`, :attr:`ImpactResponse.tests`):
  every step is an indexed edge or a fact derived from spans or manifests, and every
  candidate's predecessors are returned too, so each path can be read end to end;
* **package-level dependents** (:attr:`ImpactResponse.packages`): a unit or repository
  that declares the package the change lives in, or one an exact path crossed. That is
  coarse by construction -- it says the dependent *may* use the change -- and it names
  which of its declarations do only by pointing at the exact candidates whose path
  crosses into it through that package;
* **what was not followed** (:attr:`ImpactResponse.hubs`, :attr:`ImpactResponse.unresolved`,
  :attr:`ImpactResponse.omitted`): counted exactly, so a short answer never reads as a
  complete one.
"""

from __future__ import annotations

import enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from repoatlas.models.capability import CapabilityContext, Presentation
from repoatlas.models.capability_io import ContextView
from repoatlas.models.freshness import FreshnessDepth, IndexFreshness
from repoatlas.models.parsing import EdgeType, EntityType

DEFAULT_CANDIDATES = 30
MAX_CANDIDATES = 100
#: The most candidates an ambiguous seed lists; ``total`` says how many there were.
MAX_SEED_CANDIDATES = 8

SEED_DESCRIPTION = (
    "What changes: an entity key a tool returned, a declaration's name or qualified "
    "name, an `env:` or `module:` key, a configuration key's qualified name, or a "
    "repository-relative file or directory path. It must name exactly one thing in "
    "scope; otherwise the response lists the candidates and follows nothing."
)
IMPACT_PRESENTATION_DESCRIPTION = (
    "`compact` (the default) writes each step's edge, reading, lines and syntax. "
    "`full` adds each step's recorded resolution and up to five alternative "
    "predecessors per candidate."
)


class ImpactRequest(BaseModel):
    """Ask what a change to one thing may reach, and by which indexed paths."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    context: CapabilityContext
    seed: str = Field(description=SEED_DESCRIPTION)
    max_candidates: int = Field(default=DEFAULT_CANDIDATES, ge=1, le=MAX_CANDIDATES)
    investigation_id: str | None = None
    depth: FreshnessDepth = FreshnessDepth.STORED
    presentation: Presentation = Field(
        default=Presentation.COMPACT, description=IMPACT_PRESENTATION_DESCRIPTION
    )

    @field_validator("seed")
    @classmethod
    def _seed_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("seed must name what changes")
        return value


class SeedStatus(enum.StrEnum):
    RESOLVED = "resolved"
    #: More than one thing in scope answers to the seed: nothing is followed.
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"


class SeedBasis(enum.StrEnum):
    """How the seed was identified."""

    KEY = "key"
    PATH = "path"
    NAME = "name"
    DIRECTORY = "directory"


class Granularity(enum.StrEnum):
    """What kind of thing changes, which decides what its members are."""

    SYMBOL = "symbol"
    KEY = "key"
    FILE = "file"
    SUBTREE = "subtree"


class SeedCandidate(BaseModel):
    """One of the things an ambiguous seed could mean."""

    model_config = ConfigDict(frozen=True)

    entity: str
    type: EntityType | None = None
    repository: str | None = None
    path: str | None = None
    lines: tuple[int, int] | None = None


class ImpactSeed(BaseModel):
    """What the seed resolved to, or why it did not resolve to exactly one thing."""

    model_config = ConfigDict(frozen=True)

    status: SeedStatus
    term: str
    entity: str | None = None
    type: EntityType | None = None
    repository: str | None = None
    path: str | None = None
    lines: tuple[int, int] | None = None
    unit: str | None = None
    basis: SeedBasis | None = None
    granularity: Granularity | None = None
    #: A class's members, a file's declarations, or a directory's files.
    members: int | None = None
    candidates: tuple[SeedCandidate, ...] = ()
    #: How many candidates an ambiguous seed had, of which ``candidates`` lists the first.
    total: int | None = None
    reason: str | None = None


class ImpactRole(enum.StrEnum):
    """What a candidate does to the changed thing's reach."""

    #: It depends on what precedes it: uses, imports, calls, reads, is configured by.
    DEPENDENT = "dependent"
    #: It sets the configuration key that precedes it.
    SETS = "sets"
    #: It is part of what changes: a member of the seed.
    MEMBER = "member"


class PackageCrossing(BaseModel):
    """How one step crossed from a providing repository into a consuming one.

    The evidence a package link recorded (:mod:`repoatlas.graph.packages`), each part
    locatable: the manifest that provides the package, the file its entry names, the
    consuming manifest that declares it, and the import statement that binds the name.
    The names read in the change's direction: declared as ``declared_as``, exported under
    each of the step's ``via_names`` by the files in its ``via``, imported as
    ``imported_as``, and bound in the consumer as ``local``.
    """

    model_config = ConfigDict(frozen=True)

    package: str
    subpath: str | None = None
    from_repository: str
    to_repository: str
    provided_by: str
    entry: str
    entry_fields: str | None = None
    #: How an entry in build output was mapped to source: ``<from> -> <to> (<by>)``.
    entry_mapped: str | None = None
    declared_by: str
    constraint: str | None = None
    imported_at: str | None = None
    declared_as: str | None = None
    imported_as: str | None = None
    local: str | None = None


class ImpactStep(BaseModel):
    """The one fact connecting a candidate to its predecessor.

    Either an indexed edge (``edge``) in its stored orientation, read in the
    direction the change travels (``reading``), or a fact derived from spans or
    manifests (``derived``). Every step is locatable: ``at`` and ``lines`` name the
    source to read.
    """

    model_config = ConfigDict(frozen=True)

    edge: EdgeType | None = None
    derived: str | None = None
    reading: str
    use: str | None = None
    at: str | None = None
    lines: tuple[int, int] | None = None
    parser: str | None = None
    syntax: str | None = None
    #: Files a use was re-exported through, and the name it bore in each. On a step that
    #: crosses repositories each file is spelled ``<repository>/<path>``.
    via: tuple[str, ...] = ()
    via_names: tuple[str, ...] = ()
    #: The package evidence of a step that crosses from one repository into another.
    crossing: PackageCrossing | None = None
    #: The edge's recorded resolution; ``full`` only.
    resolution: dict[str, object] | None = None


class ImpactCandidate(BaseModel):
    """One thing the change may reach, with the step from its predecessor."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    id: int
    entity: str
    type: EntityType
    name: str
    repository: str | None = None
    path: str | None = None
    lines: tuple[int, int] | None = None
    unit: str | None = None
    role: ImpactRole = ImpactRole.DEPENDENT
    distance: int
    #: The predecessor's ``id``; ``0`` is the seed.
    from_: int = Field(serialization_alias="from")
    step: ImpactStep
    #: Unit and repository boundaries the path crosses, in order.
    boundaries: tuple[str, ...] = ()
    #: ``exact`` for declaration-level steps only, ``file`` when a file-level import
    #: carries the path, ``containment`` when it passes through membership.
    basis: str
    #: The whole path in one line, seed first.
    chain: str
    #: Other candidates this one is also reached from; ``full`` only.
    alt_paths: tuple[int, ...] = ()


class PackageImpact(BaseModel):
    """A unit or repository declaring the package the change lives in. Coarse."""

    model_config = ConfigDict(frozen=True)

    package: str
    provided_by: str
    dependent: str
    declared_at: str
    crosses_repository: bool
    granularity: str = "package"
    #: How many reached declarations use the change through this very package and
    #: declaration, or ``unresolved`` when none do (the exact consumer is unresolved),
    #: or ``outside scope`` when the dependent is a repository the call did not name.
    #: Within one repository: exact candidates found inside the dependent unit.
    exact_consumers: int | str
    #: Candidates (by ``id``) whose path crosses into the dependent through this package.
    consumers: tuple[int, ...] = ()
    #: Declarations in the dependent that use the package's module as a whole (a computed
    #: member, the namespace as a value): consumers no declaration can be named for.
    unresolved_consumers: int | None = None
    #: Package subpaths the exact consumers import from, when not only the root.
    subpaths: tuple[str, ...] = ()


class HubImpact(BaseModel):
    """A candidate with more dependents than one answer can show; collapsed, not unimportant."""

    model_config = ConfigDict(frozen=True)

    candidate: int
    entity: str
    fanout: int
    units: dict[str, int] = Field(default_factory=dict)
    units_partial: bool = False
    #: Exact dependents per repository, when they are in more than one.
    repositories: dict[str, int] = Field(default_factory=dict)
    shown: int
    collapsed: int
    note: str = "collapsed dependents are not less affected; they were not listed"


class UnresolvedImpact(BaseModel):
    """A dependent RepoAtlas can name only as a file, not as the declaration involved."""

    model_config = ConfigDict(frozen=True)

    entity: str
    reason: str
    at: str | None = None
    lines: tuple[int, int] | None = None


class ImpactOmitted(BaseModel):
    """What the walk reached or could have reached and did not return, counted."""

    model_config = ConfigDict(frozen=True)

    by_reason: dict[str, int] = Field(default_factory=dict)
    by_unit: dict[str, int] = Field(default_factory=dict)


class ImpactWork(BaseModel):
    model_config = ConfigDict(frozen=True)

    expanded: int = 0
    rows: int = 0
    queries: int = 0
    exhausted: str | None = None


class ImpactSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    reached: int = 0
    returned: int = 0
    units: int = 0
    cross_unit: int = 0
    #: Every candidate the policy reaches was returned or counted in ``omitted``, and
    #: no budget stopped the walk.
    complete: bool = True
    work: ImpactWork = ImpactWork()
    #: The traversal policy's name and version.
    policy: str


class ImpactResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    investigation_id: str
    context: ContextView
    seed: ImpactSeed
    candidates: tuple[ImpactCandidate, ...] = ()
    tests: tuple[ImpactCandidate, ...] = ()
    packages: tuple[PackageImpact, ...] = ()
    hubs: tuple[HubImpact, ...] = ()
    unresolved: tuple[UnresolvedImpact, ...] = ()
    omitted: ImpactOmitted = ImpactOmitted()
    summary: ImpactSummary
    freshness: IndexFreshness
