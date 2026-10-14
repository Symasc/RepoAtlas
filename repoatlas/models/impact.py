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

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from repoatlas.compat import StrEnum
from repoatlas.models.capability import CapabilityContext, Presentation
from repoatlas.models.capability_io import ContextView
from repoatlas.models.freshness import FreshnessDepth, IndexFreshness
from repoatlas.models.parsing import EdgeType, EntityType

DEFAULT_CANDIDATES = 30
MAX_CANDIDATES = 100
#: The most link decisions one answer lists (``ImpactResponse.decisions``).
MAX_DECISIONS = 25
#: The most refused HTTP requests one answer lists beside them, under a cap of their own so
#: they never displace a link decision (DF3c); the rest are counted under ``http``.
MAX_HTTP_DECISIONS = 10
#: The most candidates an ambiguous seed lists; ``total`` says how many there were.
MAX_SEED_CANDIDATES = 8

SEED_REPOSITORY_DESCRIPTION = (
    "For an environment-variable seed only: the repository whose reads of it the change "
    "starts from. The first step follows that repository's readers alone; every later step "
    "is the ordinary walk. Under a repository scope it must be that repository."
)
SEED_DESCRIPTION = (
    "What changes: an entity key a tool returned, a declaration's name or qualified "
    "name, an `env:` or `module:` key, a configuration key's qualified name, or a "
    "repository-relative file or directory path. It must name exactly one thing in "
    "scope; otherwise the response lists the candidates and follows nothing."
)
IMPACT_PRESENTATION_DESCRIPTION = (
    "`compact` (the default) leaves out empty fields, and freshness for repositories "
    "the answer does not draw on. `full` adds each step's recorded resolution and up "
    "to five alternative predecessors per candidate."
)


class ImpactRequest(BaseModel):
    """Ask what a change to one thing may reach, and by which indexed paths."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    context: CapabilityContext
    seed: str = Field(description=SEED_DESCRIPTION)
    #: DF3a (D3-1): the source a shared seed's change starts from.
    seed_repository: str | None = Field(default=None, description=SEED_REPOSITORY_DESCRIPTION)
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

    @field_validator("seed_repository")
    @classmethod
    def _seed_repository_is_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("seed_repository must name a repository")
        return value


class SeedStatus(StrEnum):
    RESOLVED = "resolved"
    #: More than one thing in scope answers to the seed: nothing is followed.
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"


class SeedBasis(StrEnum):
    """How the seed was identified."""

    KEY = "key"
    PATH = "path"
    NAME = "name"
    DIRECTORY = "directory"


class Granularity(StrEnum):
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


class ImpactRole(StrEnum):
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


class HttpCrossing(BaseModel):
    """How one step crossed from a repository serving a handler into one requesting it.

    The evidence an HTTP join recorded (:mod:`repoatlas.graph.http_crossings`), each
    part locatable as ``<repository>/<path>:<line>``. On the consumer's side: the
    request (``called_at``) and, when its URL or base came from a module constant, that
    constant (``base_at``). On the provider's side: the route's declaration
    (``served_at``), the source that states where the service is published
    (``identity_source``, ``identity_at``) and where that identity is bound to the
    serving application (``bound_at``). The request's method and URL as written, and
    the endpoint it reached, name the operation both sides state.

    The edge runs *consumer caller → provider handler*; the step reads it inward, from
    the handler to the request, the way a change travels. The repositories are named
    by role, never by that direction. Source evidence, not a runtime observation.
    """

    model_config = ConfigDict(frozen=True)

    method: str
    url: str
    endpoint: str
    deployment: str
    provider_repository: str
    consumer_repository: str
    called_at: str
    base_at: str | None = None
    served_at: str
    identity_source: str
    identity_at: str | None = None
    bound_at: str | None = None
    # A CI-established deployment (DF2): who deploys and who builds what the handler is
    # deployed as, the image between them, and on what basis each is known -- ``source``,
    # ``builtin`` (RepoAtlas's own version-specific action semantics) or ``declared`` (an
    # operator declaration). Each place is ``<repository>/<path>[:<line>]`` in the repository
    # it lies in. The version relation says how far the configured versions are tied:
    # ``exact``, ``symbolic`` or ``unknown``. All of it configuration, never a deployment seen.
    deployment_repository: str | None = None
    builder_repository: str | None = None
    release: str | None = None
    variant: str | None = None
    image: str | None = None
    version_relation: str | None = None
    binding_basis: str | None = None
    build_basis: str | None = None
    source_rule: str | None = None
    identity_in: str | None = None
    binding_in: str | None = None
    workload_in: str | None = None
    image_in: str | None = None
    published_in: str | None = None
    built_in: str | None = None
    declared_in: str | None = None


class ConfigurationCrossing(BaseModel):
    """How one step crossed from where a deployment sets an environment variable to code
    reading it (DF3a, :mod:`repoatlas.graph.deployment_env`).

    The setter is the one place the variable is effectively set, by the narrow precedence
    RepoAtlas reads: a values-file key, the CI step carrying a ``--set``, a ConfigMap key
    (``configMapKeyRef`` or ``envFrom``), or a compose ``environment`` entry. The reader holds
    the read, in the image the deployment's artifact join proved it runs (or the compose
    service builds). Each place is ``<repository>/<path>:<line>`` in the repository it lies in.
    ``value`` is only the value's kind -- ``literal``, ``symbolic`` (a CI variable or a compose
    interpolation, not established) or ``withheld`` -- never the value. The release, image,
    version relation and bases are the artifact join's (:class:`HttpCrossing`). All of it
    configuration, never a deployment seen.
    """

    model_config = ConfigDict(frozen=True)

    variable: str
    setter_kind: str
    setter_at: str
    reader_at: str
    deployment_repository: str
    builder_repository: str
    value: str
    release: str | None = None
    variant: str | None = None
    container: str | None = None
    service: str | None = None
    image: str | None = None
    version_relation: str | None = None
    binding_basis: str | None = None
    build_basis: str | None = None
    workload_in: str | None = None
    declared_in: str | None = None
    #: Every release variant (or compose service) whose configuration this setter is, when
    #: more than one.
    deployments: tuple[str, ...] = ()
    decision_ref: str


class CodeGeneration(BaseModel):
    """How a generated declaration was generated from the schema definition before it.

    The evidence a configured-generation link recorded (repoatlas.graph.graphql_codegen),
    each part locatable as ``<repository>/<path>[:<line>]``: the configuration's entry for
    the output, the schema definition it was generated from (``definition``), the generated
    file, the plugin that writes it, and -- for an enum member -- the value both sides carry.
    Source evidence, not a record that the generator ran: the file may be stale, which the
    decision says.
    """

    model_config = ConfigDict(frozen=True)

    config: str
    #: The schema definition (or enum value) it was generated from.
    definition: str
    output: str
    plugin: str
    member_value: str | None = None
    decision_ref: str


class Composition(BaseModel):
    """How a supergraph coordinate was composed from the provider definition before it.

    The chain an ownership link recorded (repoatlas.graph.graphql_ownership): the subgraph
    the supergraph names (``graph``) and the URL it states for it, the deployment that ties
    that URL to the provider's repository (``identity``), and the provider definition, as
    ``<repository>/<path>:<line>``. Source evidence: the copy may be stale, which the
    decision says.
    """

    model_config = ConfigDict(frozen=True)

    graph: str
    url: str
    identity: str
    provider: str
    decision_ref: str


class Migration(BaseModel):
    """A migration Prisma Migrate wrote from the schema definition before it.

    The project's schema file, the migration and the database type it creates or alters
    (repoatlas.graph.prisma_schema). Source evidence: what the migrations leave is compared
    with the schema in the ``prisma_migrations`` decision.
    """

    model_config = ConfigDict(frozen=True)

    project: str
    migration: str
    type: str
    decision_ref: str


class ClientBinding(BaseModel):
    """A name imported from the client a Prisma generator writes, bound to its schema.

    The schema file and the module specifier code imports it by (repoatlas.graph.prisma_schema).
    """

    model_config = ConfigDict(frozen=True)

    project: str
    specifier: str
    decision_ref: str


class QueryBinding(BaseModel):
    """A query's comparison of a mapped column with one of its enum's database values.

    The database, as the Prisma schema that describes it, and the table, column, operator and
    value as the query states them (repoatlas.graph.persistence).
    """

    model_config = ConfigDict(frozen=True)

    database: str
    table: str
    column: str
    op: str
    value: str | None = None
    decision_ref: str


class ValueMapping(BaseModel):
    """An explicit typed mapping that maps the member before it to this one.

    The mapping's entry, as ``<repository>/<path>:<line>`` (repoatlas.graph.value_mappings).
    """

    model_config = ConfigDict(frozen=True)

    mapping: str
    decision_ref: str


class TypedComparison(BaseModel):
    """A comparison of a typed field with a value of the enum the field is declared as.

    The model and enum declarations, as ``<repository>/<path>:<line>``, the field and the value
    compared (repoatlas.graph.value_mappings).
    """

    model_config = ConfigDict(frozen=True)

    model: str
    field: str
    enum: str
    value: str


class ImpactDecision(BaseModel):
    """A link the global pass refused or qualified where the walk went.

    Decided at the seed, a returned candidate, or in a file one of them is in: why something
    one might expect is not linked (``mapping-incomplete``, ``sql-dynamic``,
    ``snapshot-stale``, ...). A reason a link was not made, never a breakage.
    """

    model_config = ConfigDict(frozen=True)

    family: str
    subject: str
    decision: str
    at: str
    evidence: dict[str, object] = Field(default_factory=dict)
    #: A refused HTTP request (family ``http``, DF3c) only: ``at`` -- the request is made by a
    #: declaration, or in a file, the walk reached; ``toward`` -- **hypothetical**: the request is
    #: not linked, and if it were it would reach a handler the walk reached. Never a connection.
    relation: Literal["at", "toward"] | None = Field(
        default=None,
        # Every other family's decision is written exactly as before: no relation key.
        exclude_if=lambda value: value is None,
        description=(
            "Refused HTTP requests only. 'at': the request is made where the walk went. "
            "'toward': hypothetical -- the request is NOT linked; if it were, it would reach a "
            "handler the walk reached. Neither is a dependency or a step of the answer."
        ),
    )


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
    #: The HTTP evidence of a step from a handler to a request in another repository.
    http: HttpCrossing | None = None
    #: The configuration evidence of a step between a deployment's setter and a reader.
    configuration: ConfigurationCrossing | None = None
    #: The configured generation behind a step from a schema definition to what was
    #: generated from it.
    generation: CodeGeneration | None = None
    #: The federation composition behind a step from a provider definition to a supergraph's.
    composition: Composition | None = None
    #: The migration behind a step from a Prisma enum to the type a migration creates or alters.
    migration: Migration | None = None
    #: The generated Prisma client behind a step from a schema definition to code using it.
    client: ClientBinding | None = None
    #: The query behind a step from a mapped column or value to the code comparing it.
    query: QueryBinding | None = None
    #: The typed mapping behind a step from one enum member (or enum) to another.
    mapping: ValueMapping | None = None
    #: The typed comparison behind a step from an enum member to the code comparing a field.
    comparison: TypedComparison | None = None
    #: The edge's recorded resolution; ``full`` only.
    resolution: dict[str, object] | None = None


class ImpactCandidate(BaseModel):
    """One thing the change may reach, with the step from its predecessor."""

    model_config = ConfigDict(frozen=True, populate_by_name=True, serialize_by_alias=True)

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
    #: Links refused or qualified where the walk went, at most :data:`MAX_DECISIONS`.
    decisions: tuple[ImpactDecision, ...] = ()
    #: Decisions left out past that bound, counted by family.
    decisions_omitted: dict[str, int] = Field(default_factory=dict)
    omitted: ImpactOmitted = ImpactOmitted()
    summary: ImpactSummary
    freshness: IndexFreshness
