"""Wire models for the ``http_operations`` capability (static API inventory).

The inventory is a projection of what RepoAtlas statically extracted about HTTP in
one repository, and deliberately says no more than that. Three questions stay
apart, because answering one is not answering another:

* **route match** — whether a request's method and path have the shape of a route
  this repository's indexed source serves. That is all a ``matched`` row states;
* **service configuration** — which host a client is configured to reach. It is
  not in this inventory at all: configuration is read with search and inspect;
* **ownership** — which service owns the data behind a route. Nothing static
  establishes it, and nothing here claims it.

A caller row's **crossings** are a fourth thing, apart from its status: the workspace
join's stored decisions for the request (``http_call_decisions``), each with the chain
of hops it rests on. A ``linked`` crossing says the source proves a deployment identity
from the request's origin to one handler; it does not say the request is sent, or that
the deployment runs. Under repository scope, what another repository proves is
withheld unless ``cross_repository`` asks for it, and its lines are evidence only under
global scope.

Every status describes **indexed static facts in this repository**. A caller with
``no_indexed_handler`` is not a caller whose backend has no implementation: the
handler may be in another repository, in syntax RepoAtlas does not extract, behind
a mount that is not proven, or in a construct listed under ``coverage``. The same
holds for ``no_indexed_caller``. The ``basis`` block says so in every response,
so a reader cannot miss it by reading one row.
"""

from __future__ import annotations

import enum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.capability_io import ContextView, EvidenceItem
from repoatlas.models.dependency_inventory import AppliedPageLimits
from repoatlas.models.freshness import FreshnessDepth, IndexFreshness
from repoatlas.models.http import HTTP_METHODS
from repoatlas.models.inspection import validate_path_shape
from repoatlas.models.investigation import EvidenceId, LedgerSummary

#: The most operations one page may carry, whatever is asked.
MAX_PAGE_SIZE = 200

#: Enough to see a real inventory without becoming a dump.
DEFAULT_PAGE_SIZE = 50

#: How many counterparts a row names before it only counts them.
MAX_COUNTERPARTS = 5


class OperationRole(enum.StrEnum):
    """Which side of HTTP to list."""

    CALLER = "caller"
    PROVIDER = "provider"
    BOTH = "both"


class OperationStatus(enum.StrEnum):
    """What one row is, in indexed static facts of one repository.

    The first four are callers, the last three providers. None of them is a
    statement about the whole system: "no indexed handler" is absence in the
    index, and so is "no indexed caller".
    """

    #: Path and method established; a route indexed as served here matches.
    MATCHED = "matched"
    #: Path established, method unread, and path-compatible routes exist.
    AMBIGUOUS = "ambiguous"
    #: Path established; no route indexed as served in this repository matches.
    NO_INDEXED_HANDLER = "no_indexed_handler"
    #: The path cannot be established; ``detail`` says why.
    UNRESOLVED = "unresolved"
    #: A route served here that an indexed caller in this repository reaches.
    CALLED = "called"
    #: A route served here that no indexed caller in this repository reaches.
    NO_INDEXED_CALLER = "no_indexed_caller"
    #: A route declared here whose mount is not proven; ``detail`` says why.
    DECLARED = "declared"


class CrossingStatus(enum.StrEnum):
    """The workspace join's decision for one request, as the global link pass stored it."""

    #: Every hop proven, to exactly one handler.
    LINKED = "linked"
    #: Two handlers, two identities, or a rival identity or pattern fit the origin.
    PROVIDER_AMBIGUOUS = "http-provider-ambiguous"
    #: A deployment resolves the origin's identity, and the identity reaches no code.
    DEPLOYMENT_SOURCE_UNRESOLVED = "http-deployment-source-unresolved"
    #: The origin fits an Ingress host no locally established deployment resolves.
    DEPLOYMENT_BINDING_UNRESOLVED = "http-deployment-binding-unresolved"
    #: The service is published; the operation is not.
    ROUTE_UNMATCHED = "http-route-unmatched"
    #: No identity, pattern or published service has the origin.
    SERVICE_UNMATCHED = "http-service-unmatched"
    #: The path is read, the host is not: counted and listed, never joined.
    BASE_UNKNOWN = "http-base-unknown"
    #: The request is not read.
    CLAIM_UNREAD = "http-claim-unread"


class HttpOperationsRequest(BaseModel):
    """List the HTTP operations one repository's indexed source makes or serves."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    context: CapabilityContext
    discover: bool = Field(
        default=False,
        description="With global scope and a non-root route_prefix, discover "
        "compatible operations across repositories. Rows retain repository-local "
        "statuses and source identity; compatibility does not prove a deployment "
        "identity or a runtime dependency, which only a row's crossings state. "
        "Results are paginated.",
    )
    role: OperationRole = Field(
        default=OperationRole.BOTH,
        description="`caller` for requests the source makes, `provider` for routes it "
        "serves or declares, `both` for all.",
    )
    within_path: str | None = Field(
        default=None,
        description="Return only operations written at or beneath this "
        "repository-relative path. It narrows what is returned, never what is "
        "known: matching still considers every route the repository serves.",
    )
    owner: str | None = Field(
        default=None,
        description="Return only operations inside this qualified name and its "
        "members: `createLearningClient` lists the requests its methods make. "
        "`X.*` lists the members only.",
    )
    method: str | None = Field(default=None, description="An exact uppercase HTTP method.")
    route_prefix: str | None = Field(
        default=None,
        description="Return only operations whose route starts with this path, "
        "matched on whole segments.",
    )
    status: tuple[OperationStatus, ...] = Field(
        default=(), description="Return only these statuses. Empty means every status."
    )
    crossing: tuple[CrossingStatus, ...] = Field(
        default=(),
        description="Return only callers with a crossing of these decisions, and of each "
        "only those crossings. `http-base-unknown` lists the requests whose host the source "
        "does not state. Empty means every row and every crossing.",
    )
    cross_repository: bool = Field(
        default=False,
        description="Under repository scope, show what another repository proves in each "
        "crossing: its repository, path and lines, the handler and the provider. Off, they "
        "are withheld and each hop keeps its status. Lines in another repository are "
        "supplied as evidence only under global scope.",
    )
    limit: int | None = Field(default=None, gt=0)
    #: Opaque continuation from a previous page. Never constructed by a caller.
    cursor: str | None = None
    include_provenance: bool = Field(
        default=False,
        description="Supply each returned row's own lines as evidence, so it can be "
        "cited and so its route can be named in a grounded claim. Off keeps pages "
        "small and allocates no evidence.",
    )
    investigation_id: str | None = None
    depth: FreshnessDepth = FreshnessDepth.STORED

    @model_validator(mode="after")
    def _discovery_is_bounded(self) -> HttpOperationsRequest:
        if self.discover:
            if self.context.scope is not CapabilityScope.GLOBAL:
                raise ValueError("discover requires global scope")
            if not self.route_prefix or not self.route_prefix.strip("/"):
                raise ValueError("global discovery requires a non-root route_prefix")
            if self.within_path is not None:
                raise ValueError("within_path requires repository scope")
        return self

    @field_validator("within_path")
    @classmethod
    def _within_path_is_repository_relative(cls, value: str | None) -> str | None:
        if value is None:
            return None
        validate_path_shape(value)
        if not [part for part in value.replace("\\", "/").split("/") if part not in ("", ".")]:
            raise ValueError(
                "within_path must name a file or directory inside the repository; "
                "omit it to list the whole repository"
            )
        return value

    @field_validator("owner")
    @classmethod
    def _owner_is_a_qualified_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        name = value[:-2] if value.endswith(".*") else value
        if not name or any(char.isspace() for char in name) or "*" in name:
            raise ValueError("owner must be a qualified name such as `X` or `X.*`")
        return value

    @field_validator("method")
    @classmethod
    def _method_is_exact(cls, value: str | None) -> str | None:
        if value is not None and value not in HTTP_METHODS:
            raise ValueError(f"method must be one of {', '.join(HTTP_METHODS)}")
        return value

    @field_validator("route_prefix")
    @classmethod
    def _route_prefix_is_a_rooted_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith("/") or any(char.isspace() or char in "?#" for char in value):
            raise ValueError("route_prefix must be a path starting with `/`, with no query")
        return value

    @field_validator("status")
    @classmethod
    def _each_status_once(cls, value: tuple[OperationStatus, ...]) -> tuple[OperationStatus, ...]:
        if len(set(value)) != len(value):
            raise ValueError("status must name each status at most once")
        return value

    @field_validator("crossing")
    @classmethod
    def _each_crossing_once(cls, value: tuple[CrossingStatus, ...]) -> tuple[CrossingStatus, ...]:
        if len(set(value)) != len(value):
            raise ValueError("crossing must name each decision at most once")
        return value

    @field_validator("cursor")
    @classmethod
    def _cursor_is_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("cursor must be a continuation token, or be omitted entirely")
        return value


def _lean(data: dict[str, Any], always: frozenset[str]) -> dict[str, Any]:
    """Absent means empty: a row writes only the fields it has."""
    return {
        key: value
        for key, value in data.items()
        if key in always or value not in (None, "", [], (), {}, False, 0)
    }


class OperationCounterpart(BaseModel):
    """The other side of a row, in this repository only."""

    model_config = ConfigDict(frozen=True)

    entity: str
    path: str | None = None
    line: int | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        return _lean(handler(self), frozenset({"entity"}))


class DeclaredMatch(BaseModel):
    """A route declared in this repository whose mount is not proven, with the call's shape.

    A hint, never an edge: the declared path is controller-relative, and the
    application that would serve it is exactly what is not proven.
    """

    model_config = ConfigDict(frozen=True)

    route: str
    entity: str
    path: str | None = None
    line: int | None = None
    reason: str = ""

    @model_serializer(mode="wrap")
    def _serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        return _lean(handler(self), frozenset({"route", "entity"}))


class HopPlace(BaseModel):
    """One place the source proves a hop: lines of a file, or a whole file."""

    model_config = ConfigDict(frozen=True)

    repository: str
    path: str
    #: ``None`` for a hop a file proves as a whole; such a place is never evidence.
    line_start: int | None = None
    line_end: int | None = None
    #: What the place is, as the join names it (``called_at``, ``identity_in``, …).
    role: str
    evidence_id: EvidenceId | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        return _lean(handler(self), frozenset({"repository", "path", "role"}))


class CrossingHop(BaseModel):
    """One hop of a crossing's chain: caller, origin, service identity, deployment
    binding, source binding, route, handler — in that order."""

    model_config = ConfigDict(frozen=True)

    kind: str
    #: ``proven``, ``unresolved``, ``ambiguous`` or ``not_reached``. A hop not reached
    #: cites nothing.
    status: str
    provenance: tuple[HopPlace, ...] = ()
    #: A stable reason code, for an unresolved or ambiguous hop, and its text.
    reason: str = ""
    note: str = ""
    #: What the source says at this hop: a deployment, a pattern, the unresolved values.
    detail: str = ""
    #: The handler's entity key, on a proven handler hop.
    handler: str | None = None
    #: Something another repository proves here is not shown under this scope.
    withheld: bool = False

    @model_serializer(mode="wrap")
    def _serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        return _lean(handler(self), frozenset({"kind", "status"}))


class HttpCrossing(BaseModel):
    """The workspace join's stored decision for one request this row makes.

    One per request: a request whose base is a literal alternative, or that an injected
    transport makes from several roots, has one crossing each, none overwriting another.
    """

    model_config = ConfigDict(frozen=True)

    #: Opaque and deterministic; the same request has the same ref after any sync.
    decision_ref: str
    status: CrossingStatus
    method: str
    #: The request as the join read it, origin included when stated.
    url: str
    #: ``alternative:<line>`` or ``root:<options>|<base>``; empty for a plain request.
    variant: str = ""
    chain: tuple[CrossingHop, ...] = ()
    #: A link's handler entity and provider repository, when this scope shows them.
    handler: str | None = None
    provider: str | None = None
    #: A link's shared endpoint node: in every scope, the way to continue from it.
    endpoint: str | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        return _lean(handler(self), frozenset({"decision_ref", "status", "chain"}))


class HttpOperation(BaseModel):
    """One request the source makes or one route it serves or declares."""

    model_config = ConfigDict(frozen=True)

    #: Present in workspace discovery; canonical source repository, never a provider guess.
    repository: str | None = None
    role: Literal["caller", "provider"]
    status: OperationStatus
    #: ``None`` when the method cannot be read.
    method: str | None = None
    #: What the source wrote, path only: a literal, a template with ``${…}``
    #: segments, a named value, or a declared route. Never a query.
    route: str = ""
    path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    #: The calling or serving entity's key: a handle for inspect and relationships.
    entity: str | None = None
    #: The directory of the nearest ``package.json``, when it is not the root.
    package: str | None = None
    #: A reason code (``exact``, ``structural``, ``method_unread``, …), or why a
    #: declared route is not proven.
    detail: str = ""
    #: Route-node keys this row reaches or is: continue from them with
    #: relationships, across repositories when that is the question.
    routes: tuple[str, ...] = ()
    #: Compatible routes not linked: under another method, or under any method
    #: when this row's own cannot be read.
    candidates: tuple[str, ...] = ()
    counterparts: tuple[OperationCounterpart, ...] = ()
    #: How many counterparts exist; more than are named when it exceeds them.
    counterpart_count: int = 0
    declared_matches: tuple[DeclaredMatch, ...] = ()
    #: Whether something outside this row's own lines decided its method or route:
    #: a helper default, a wrapper base, a controller prefix, the server's
    #: parameter spelling. Its evidence shows the lines as written, not those parts.
    composed: bool = False
    evidence_id: EvidenceId | None = None
    #: The workspace join's decisions for the requests this caller row makes, by
    #: ``decision_ref``. Its ``status`` is unchanged by them.
    crossings: tuple[HttpCrossing, ...] = ()

    @model_serializer(mode="wrap")
    def _serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        return _lean(handler(self), frozenset({"role", "status", "route"}))


class CrossingBasis(BaseModel):
    """What every crossing rests on, and how much of another repository this answer shows."""

    model_config = ConfigDict(frozen=True)

    #: Read from the decisions the last global link pass stored, never recomputed here.
    source: Literal["stored_link_pass"] = "stored_link_pass"
    #: Whether any global link pass has stored decisions in this index.
    built: bool
    establishes: tuple[str, ...] = ("source_backed_deployment_identity_chain",)
    does_not_establish: tuple[str, ...] = ("runtime_reachability", "deployment_runs")
    #: ``own_repository``: another repository's places, handler and provider withheld;
    #: ``cross_repository``: shown, its lines never evidence; ``global``: everything.
    projection: Literal["own_repository", "cross_repository", "global"]


class OperationBasis(BaseModel):
    """What every status in this response rests on, stated once per response."""

    model_config = ConfigDict(frozen=True)

    extraction: Literal["static"] = "static"
    scope: Literal["repository"] = "repository"
    repository: str
    #: ``no_indexed_handler`` and ``no_indexed_caller`` are absence in this
    #: repository's index, never source-wide absence.
    negative_statuses: Literal["indexed_absence_only"] = "indexed_absence_only"
    establishes: tuple[str, ...] = ("route_compatibility",)
    does_not_establish: tuple[str, ...] = ("service_configuration", "data_ownership")
    crossings: CrossingBasis


class DiscoveryBasis(BaseModel):
    """Workspace candidates; every row's status still describes its source repository."""

    model_config = ConfigDict(frozen=True)
    extraction: Literal["static"] = "static"
    scope: Literal["global"] = "global"
    repository: None = None
    status_scope: Literal["source_repository"] = "source_repository"
    repositories: tuple[str, ...]
    repositories_examined: int
    operations_examined: int
    negative_statuses: Literal["indexed_absence_only"] = "indexed_absence_only"
    establishes: tuple[str, ...] = ("route_compatibility_candidates",)
    does_not_establish: tuple[str, ...] = (
        "runtime_dependency",
        "service_configuration",
        "data_ownership",
    )
    crossings: CrossingBasis


class OperationSummary(BaseModel):
    """Counts over the whole filtered set, not the page."""

    model_config = ConfigDict(frozen=True)

    total: int = 0
    by_status: dict[str, int] = Field(default_factory=dict)
    by_role: dict[str, int] = Field(default_factory=dict)
    #: Crossings by decision, over the same rows.
    by_crossing: dict[str, int] = Field(default_factory=dict)


class CoverageLimitation(BaseModel):
    """One reason the inventory may not be the whole story here."""

    model_config = ConfigDict(frozen=True)

    code: str
    #: How many rows in scope show it, or ``None`` for a construct that is not
    #: extracted at all and so cannot be counted — only said to be possible here.
    count: int | None = None
    note: str = ""


class OperationCoverage(BaseModel):
    """What was read in scope, and what could not be."""

    model_config = ConfigDict(frozen=True)

    #: Indexed files in scope, by the parser that read them.
    files: dict[str, int] = Field(default_factory=dict)
    #: Unresolved caller rows by reason code.
    unresolved: dict[str, int] = Field(default_factory=dict)
    ambiguous: int = 0
    declared: int = 0
    #: Unresolved callers whose path is unknown: any of them could reach any route.
    unknown_paths: int = 0
    limitations: tuple[CoverageLimitation, ...] = ()


class HttpOperationsResponse(BaseModel):
    """One page of one repository's static HTTP inventory."""

    model_config = ConfigDict(frozen=True)

    investigation_id: str
    context: ContextView
    basis: OperationBasis | DiscoveryBasis
    summary: OperationSummary
    #: Rows in an order fixed by what they are, so the same index answers the same
    #: question the same way.
    operations: tuple[HttpOperation, ...] = ()
    coverage: OperationCoverage
    #: Each returned row's own lines, recorded in the ledger before this response
    #: existed, when ``include_provenance`` asked for them.
    evidence: tuple[EvidenceItem, ...] = ()
    freshness: IndexFreshness
    #: Opaque token for the next page. ``None`` when this page is the last.
    next_cursor: str | None = None
    #: Whether further rows exist after this page under the same filters.
    has_more: bool = False
    limits: AppliedPageLimits
    ledger: LedgerSummary
