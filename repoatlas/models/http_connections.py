"""HTTP Connections: the unresolved requests of a workspace, and what operators declared about them.

A request the join leaves unlinked is listed here with the reason it was refused; an operator may
declare that the origin it names is served by one indexed service, and the declaration joins the
request at the next sync with its identity, binding and source hops ``declared``, never ``proven``.
Everything in these answers is read from the join's stored decisions and the operators' own
declarations. Nothing is contacted and nothing is guessed: a candidate is a service whose served
routes the requests' shapes match, offered as a hypothesis the operator confirms or ignores.

A declaration's **state** is derived, never stored, from the last global pass:

* ``pending``      stated after the last pass: it applies at the next sync;
* ``applied``      at least one request is joined through it;
* ``ineffective``  the pass considered it and joined nothing; ``reason`` says why;
* ``invalidated``  what it states no longer holds in the index; ``reason`` says what changed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from repoatlas.models.capability import CapabilityContext
from repoatlas.models.deployment_targets import (
    MAX_LINK_CALLERS,
    MAX_LINK_TEXT,
    DeclarationProvenance,
    DeclarationScope,
    Verification,
)
from repoatlas.models.http_inventory import DECISION_REF_PATTERN, CrossingStatus, ProviderLifecycle
from repoatlas.models.service_declarations import ServiceDeclarationView

#: A worklist group's key: opaque, deterministic, the same origin has the same key after any sync.
GROUP_KEY_PATTERN = r"^hcg1-[0-9a-f]{20}$"
#: Requests one page of a group lists at most.
MAX_GROUP_PAGE = 100

GroupKind = Literal["stated-origin", "base-not-stated"]
#: What the index knows of who answers a request: ``identified`` (the join linked it),
#: ``known-external`` (an applied classification says its origin is a service outside the index;
#: the decision stays unmatched) or ``unresolved`` (neither).
RequestIdentity = Literal["unresolved", "known-external", "identified"]
DeclarationState = Literal["pending", "applied", "ineffective", "invalidated"]
#: What a declaration would do to one request, as the sync will decide it.
PreviewOutcome = Literal[
    "would-link",
    "ambiguous",
    "no-matching-route",
    "caller-excluded",
    "source-established",
    "not-declarable",
    # A scoped declaration (Phase A2): the sync's own verdicts on a request in the group.
    "scope-too-broad",
    "scope-unidentified",
    "injected-root-unselected",
    "out-of-scope",
]
#: Why a declaration is ``ineffective`` or ``invalidated``.
DeclarationReason = Literal[
    "",
    "applies-at-next-sync",
    "ambiguous",
    "route-unmatched",
    "callers-excluded",
    "source-established",
    "no-request-matched",
    "repository-not-indexed",
    "repository-serves-no-routes",
    "origin-no-longer-requested",
    "target-url-unresolved",
    # A scoped declaration (Phase A2).
    "caller-not-indexed",
    "scope-too-broad",
    "scope-unidentified",
    "injected-root-unselected",
    "scope-not-found",
    "base-no-longer-requested",
]

BASIS = (
    "Requests are the join's stored decisions, from indexed source. A declaration is an operator's "
    "statement, never evidence: it joins a request only where the target serves a matching route, "
    "the hops it supplies are marked declared, and it never overrides what the source establishes. "
    "Nothing is contacted. A candidate is a service whose served routes the requests' shapes "
    "match; it is a hypothesis, not a finding."
)


class ConnectionSite(BaseModel):
    """Where a request is written."""

    model_config = ConfigDict(frozen=True)

    repository: str
    path: str
    line_start: int | None = None
    line_end: int | None = None


class ConnectionRequest(BaseModel):
    """One request and the join's decision for it."""

    model_config = ConfigDict(frozen=True)

    decision_ref: str = Field(pattern=DECISION_REF_PATTERN)
    method: str | None = None
    #: The request's path as a template, identifier-like segments masked: display only, never
    #: used to match anything.
    route: str
    site: ConnectionSite
    status: CrossingStatus
    #: The join's reason for the refusal (a hop reason such as ``no_identity``).
    reason: str = ""
    language: str
    #: Whether a declaration may join it: it states an origin and the source has not identified
    #: the service that origin names.
    declarable: bool = False
    #: The declarations that concern it, and what the last pass did with them.
    declaration_ids: tuple[int, ...] = ()
    declaration_outcome: str | None = None
    #: The declaration that joined it, when one did.
    joined_by: int | None = None
    #: The qualified name of the entity that makes the request; ``None`` at module level.
    symbol: str | None = None
    #: For a request whose base the source does not state: the identity of its base text
    #: (``hbk1-...``, a hash, never the text) and whether a scoped declaration can name it.
    base_key: str | None = None
    scopable: bool = False
    #: Why it cannot be scoped: ``base-text-truncated``, ``base-text-unreadable``,
    #: ``host-expression``, ``injected-transport`` or ``not-base-unknown``; ``None`` when it can.
    scope_refusal: str | None = None
    #: Who answers it, as the index and the operators' classifications say. A classification never
    #: changes ``status``, ``reason`` or the chain: it only says the origin is known to be outside.
    identity: RequestIdentity = "unresolved"
    #: The external-service declaration behind ``known-external``; ``None`` otherwise.
    external_service_id: int | None = None


class DeclarationView(BaseModel):
    """One operator's link and what became of it."""

    model_config = ConfigDict(frozen=True)

    id: int
    repository: str
    label: str
    url: str | None = None
    environment: str | None = None
    callers: tuple[str, ...] = ()
    verification: Verification
    created_at: datetime
    updated_at: datetime
    state: DeclarationState
    reason: DeclarationReason = ""
    #: Requests joined through it, and requests at its origin the pass declined to join, by why.
    joined: int = 0
    declined: dict[str, int] = Field(default_factory=dict)
    #: The words of a scoped declaration (a file, a symbol, a base by hash); ``None`` for a URL.
    scope: DeclarationScope | None = None


class CandidateService(BaseModel):
    """An indexed service whose served routes match some of a group's requests: a hypothesis."""

    model_config = ConfigDict(frozen=True)

    repository: str
    routes_served: int
    requests_matched: int
    #: Of the matched, those exactly one handler of this service serves.
    requests_unique: int
    requests_ambiguous: int
    #: The base path under which the most requests match (empty for the origin's root).
    suggested_base_path: str = ""


class ScopeChoice(BaseModel):
    """A scope an operator may name for an unstated base: a file of a caller, or one symbol of it.
    A choice is shown, never preselected; the counts are what the sync's own rule
    (:func:`repoatlas.graph.http_scopes.covers`) says of the group's requests in that file."""

    model_config = ConfigDict(frozen=True)

    repository: str
    path: str
    #: A dotted qualified name of exactly one entity of the file; ``None``: the whole file.
    symbol: str | None = None
    #: Requests of the group this scope covers.
    covered: int = 0
    #: Requests of the group in this file it would reach and refuse as too broad (a receiver base
    #: at file scope, a name its code is not proven to read, a member below a direct member).
    too_broad: int = 0


class ConnectionGroup(BaseModel):
    """Requests that share a target: one stated origin, or one unstated base expression."""

    model_config = ConfigDict(frozen=True)

    key: str = Field(pattern=GROUP_KEY_PATTERN)
    kind: GroupKind
    #: ``scheme://host[:port]`` for a stated origin; never a path, query or credential.
    origin: str | None = None
    #: What the source wrote for a base it did not state as a URL (``{baseUrl}``), display only.
    base_expression: str | None = None
    #: A loopback origin: it names a different service in each repository, so a mapping must name
    #: the callers it applies to.
    loopback: bool = False
    requests: int
    declarable: int
    statuses: dict[str, int]
    languages: dict[str, int]
    callers: tuple[str, ...]
    reasons: dict[str, int]
    samples: tuple[ConnectionRequest, ...] = ()
    declarations: tuple[DeclarationView, ...] = ()
    #: For an expanded group whose base the source does not state: the scopes a declaration may
    #: name, each with how many of the group's requests it covers. Empty otherwise.
    scope_choices: tuple[ScopeChoice, ...] = ()
    #: The classification of this origin as an external service, in any state; ``None`` when none.
    external_service: ServiceDeclarationView | None = None


class ConnectionsSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    #: Requests the join stored a decision for, by outcome.
    requests: int
    linked_by_source: int
    linked_by_declaration: int
    unresolved: int
    declarable: int
    #: Unresolved requests whose base the source does not state (A2: not mappable here).
    base_not_stated: int
    #: Of those, the requests whose base text a scoped declaration can name.
    scopable: int = 0
    declarations: dict[str, int] = Field(default_factory=dict)
    by_status: dict[str, int] = Field(default_factory=dict)
    #: Of the unresolved requests, those an applied classification shows as a known external
    #: dependency, and those whose service identity is still unresolved.
    known_external: int = 0
    unresolved_identity: int = 0


class HttpConnectionsRequest(BaseModel):
    """List the workspace's unresolved HTTP requests, grouped by what they target."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    context: CapabilityContext
    status: tuple[CrossingStatus, ...] = Field(
        default=(), description="Only requests with these decisions. Empty: every unresolved one."
    )
    language: str | None = Field(default=None, description="`python` or `typescript`.")
    caller: str | None = Field(default=None, description="Only requests made in this repository.")
    declaration_state: DeclarationState | None = Field(
        default=None, description="Only groups with a declaration in this state."
    )
    text: str | None = Field(
        default=None,
        max_length=200,
        description="Only groups whose origin or base expression contains this text.",
    )
    include_resolved: bool = Field(
        default=False, description="Also list groups every request of which is already joined."
    )
    group: str | None = Field(
        default=None,
        pattern=GROUP_KEY_PATTERN,
        description="Expand one group: its requests (paged) and its candidate services.",
    )
    limit: int = Field(default=50, ge=1, le=MAX_GROUP_PAGE)
    cursor: str | None = None
    identity: RequestIdentity | None = Field(
        default=None,
        description="Only requests with this identity: unresolved, known-external or identified.",
    )

    @field_validator("language")
    @classmethod
    def _language(cls, value: str | None) -> str | None:
        if value is not None and value not in {"python", "typescript"}:
            raise ValueError("language is `python` or `typescript`")
        return value


class HttpConnectionsResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    context: CapabilityContext
    #: Whether the last sync stored the join's decisions; before it there is nothing to list.
    built: bool
    #: Whether a global pass has run: a declaration it has not read is pending.
    synced: bool = False
    summary: ConnectionsSummary
    groups: tuple[ConnectionGroup, ...] = ()
    #: Every declaration, newest first, whatever the filters: the operator's own list.
    declarations: tuple[DeclarationView, ...] = ()
    #: With ``group``: that group's requests (one page) and candidate services.
    requests: tuple[ConnectionRequest, ...] = ()
    candidates: tuple[CandidateService, ...] = ()
    has_more: bool = False
    next_cursor: str | None = None
    basis: str = BASIS
    #: The external-service classifications, newest first, whatever the filters.
    service_declarations: tuple[ServiceDeclarationView, ...] = ()


class PreviewRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision_ref: str = Field(pattern=DECISION_REF_PATTERN)
    method: str | None = None
    route: str
    site: ConnectionSite
    outcome: PreviewOutcome
    #: The handler it would reach, ``<path>:<line>`` in the target, when exactly one.
    handler: str | None = None


def _route_prefix(value: str) -> str:
    """A base path: empty, or ``/`` and literal segments (no query, no holes); a trailing ``/``
    is dropped. Kept in step with ``DeclarationScope._base_path``, which cannot import this."""
    if value and (
        not value.startswith("/")
        or any(c in value for c in "?#{}")
        or any(c.isspace() for c in value)
    ):
        raise ValueError("a base path is empty or starts with `/`, with no query or holes")
    return value.rstrip("/")


class PreviewScope(BaseModel):
    """The scope a preview asks about: one file (and symbol) of one caller, and the base its code
    does not state. The base is named by its key (``hbk1-...``, a hash), never by its text; the
    shown form, when given, must be the one the group has (:attr:`base_shown` is optional because
    the preview derives it)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    caller: str = Field(min_length=1, max_length=MAX_LINK_TEXT)
    path: str = Field(min_length=1, max_length=MAX_LINK_TEXT)
    symbol: str | None = Field(default=None, min_length=1, max_length=MAX_LINK_TEXT)
    base_key: str = Field(pattern=r"^hbk1-[0-9a-f]{20}$")
    base_shown: str | None = Field(default=None, max_length=MAX_LINK_TEXT)
    #: The route prefix the declared service mounts its routes under: never added to or removed
    #: from a request.
    base_path: str = Field(default="", max_length=MAX_LINK_TEXT)

    @field_validator("base_path")
    @classmethod
    def _base_path(cls, value: str) -> str:
        return _route_prefix(value)


class HttpConnectionPreviewRequest(BaseModel):
    """What declaring that an origin is served by one repository would do, before it is stored.

    With ``scope`` it is about a base the source does not state: the group is that base's, the
    scope names the caller's file (and symbol), and the base path is the scope's own."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    context: CapabilityContext
    group: str = Field(pattern=GROUP_KEY_PATTERN)
    repository: str = Field(min_length=1, max_length=MAX_LINK_TEXT)
    base_path: str = Field(default="", max_length=MAX_LINK_TEXT)
    callers: tuple[str, ...] = Field(default=(), max_length=MAX_LINK_CALLERS)
    environment: str | None = Field(default=None, min_length=1, max_length=MAX_LINK_TEXT)
    limit: int = Field(default=50, ge=1, le=MAX_GROUP_PAGE)
    scope: PreviewScope | None = None
    include_diagnostics: bool = Field(
        default=False,
        description="Also describe, for the repository the declaration would map, how many of "
        "its served routes a declaration could expose, which declared or unpublished routes the "
        "requests this declaration would not join resemble, and its provider lifecycle. Hints "
        "only: never edges or decisions, and nothing else in the answer changes.",
    )

    @field_validator("base_path")
    @classmethod
    def _base_path(cls, value: str) -> str:
        return _route_prefix(value)

    @model_validator(mode="after")
    def _scope_names_its_caller(self) -> HttpConnectionPreviewRequest:
        if self.scope is None:
            return self
        if self.callers and tuple(self.callers) != (self.scope.caller,):
            raise ValueError("a scoped preview names its one caller in the scope")
        if self.base_path:
            raise ValueError("a scoped preview states its base path in the scope")
        return self


class UnpublishedMatch(BaseModel):
    """A route the target declares, or recognizes and does not serve, that a request this
    declaration would not join resembles: a hint, never an edge or a decision."""

    model_config = ConfigDict(frozen=True)

    decision_ref: str = Field(pattern=DECISION_REF_PATTERN)
    #: ``METHOD /route`` as the target's code declares it, relative to wherever it is mounted.
    route: str
    status: Literal["declared", "unpublished"]
    path: str | None = None
    line: int | None = None
    category: str | None = None
    code: str = ""
    #: The planner's own reason the route is not served.
    detail: str = ""


class PreviewProviderDiagnostics(BaseModel):
    """What a preview knows of the target's provider routes beyond the ones it serves (R1)."""

    model_config = ConfigDict(frozen=True)

    #: Distinct routes the declaration would expose at its URL.
    routes_exposable: int = 0
    #: Served routes no declaration can expose: they state no line (a route built from a prefix).
    routes_without_declaration_line: int = 0
    #: A link's exposure cap was reached, so some routes were not offered.
    exposures_truncated: bool = False
    lifecycle: ProviderLifecycle
    #: At most five, and how many there are.
    unpublished_matches: tuple[UnpublishedMatch, ...] = ()
    unpublished_match_count: int = 0
    basis: str = "hints, never edges or decisions"


class HttpConnectionPreviewResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    context: CapabilityContext
    group: str
    #: ``None`` for a scoped preview: a base the source does not state has no origin.
    origin: str | None = None
    #: The URL the declaration would state; ``None`` for a scoped preview, which states none.
    url: str | None = None
    repository: str
    #: For a scoped preview: the declaration it would store (the base shown masked, never its
    #: text), as the sync will read it.
    scope: DeclarationScope | None = None
    routes_served: int
    counts: dict[str, int]
    rows: tuple[PreviewRow, ...] = ()
    has_more: bool = False
    #: What must change before it can be stored; empty when it can.
    requirements: tuple[str, ...] = ()
    #: Cautions that do not block it.
    warnings: tuple[str, ...] = ()
    #: An existing declaration this one repeats (it would be returned, not stored twice).
    duplicate_of: int | None = None
    #: One it conflicts with (another repository at the same place for the same callers).
    conflicts_with: int | None = None
    can_create: bool
    basis: str = BASIS
    provider_diagnostics: PreviewProviderDiagnostics | None = Field(
        default=None,
        # Without include_diagnostics the answer is written exactly as before: no key.
        exclude_if=lambda value: value is None,
        description="Hints about the target's provider routes. Set only with "
        "`include_diagnostics`; absent from the JSON otherwise.",
    )

    @model_validator(mode="after")
    def _requirements_block(self) -> HttpConnectionPreviewResponse:
        if self.requirements and self.can_create:
            raise ValueError("a preview with requirements cannot be created")
        return self


class HttpDeclarationHistoryRequest(BaseModel):
    """What operators did to their declarations, newest first."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    context: CapabilityContext
    id: int | None = Field(default=None, ge=1, description="One declaration's history.")
    repository: str | None = None
    limit: int = Field(default=100, ge=1, le=500)


class HistoryEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    at: datetime
    action: Literal["create", "update", "revoke", "import"]
    subject_id: int
    repository: str
    via: Literal["ui", "api", "cli", "import"]
    request_group: str | None = None
    note: str | None = None
    before: dict[str, object] | None = None
    after: dict[str, object] | None = None


class HttpDeclarationHistoryResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    context: CapabilityContext
    entries: tuple[HistoryEntry, ...] = ()
    #: Actions only. What a sync did with a declaration is each declaration's state, not history.
    basis: str = (
        "Operators' actions on declarations, append-only and local to this index. It records what "
        "was stated, changed, revoked or imported, never what a sync made of it."
    )


__all__ = [
    "BASIS",
    "GROUP_KEY_PATTERN",
    "CandidateService",
    "ConnectionGroup",
    "ConnectionRequest",
    "ConnectionSite",
    "ConnectionsSummary",
    "DeclarationProvenance",
    "DeclarationState",
    "DeclarationView",
    "HistoryEntry",
    "HttpConnectionPreviewRequest",
    "HttpConnectionPreviewResponse",
    "HttpConnectionsRequest",
    "HttpConnectionsResponse",
    "HttpDeclarationHistoryRequest",
    "HttpDeclarationHistoryResponse",
    "PreviewProviderDiagnostics",
    "PreviewRow",
    "PreviewScope",
    "RequestIdentity",
    "ScopeChoice",
    "UnpublishedMatch",
]
