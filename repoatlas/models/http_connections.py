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
    Verification,
)
from repoatlas.models.http_inventory import DECISION_REF_PATTERN, CrossingStatus

#: A worklist group's key: opaque, deterministic, the same origin has the same key after any sync.
GROUP_KEY_PATTERN = r"^hcg1-[0-9a-f]{20}$"
#: Requests one page of a group lists at most.
MAX_GROUP_PAGE = 100

GroupKind = Literal["stated-origin", "base-not-stated"]
DeclarationState = Literal["pending", "applied", "ineffective", "invalidated"]
#: What a declaration would do to one request, as the sync will decide it.
PreviewOutcome = Literal[
    "would-link",
    "ambiguous",
    "no-matching-route",
    "caller-excluded",
    "source-established",
    "not-declarable",
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
    declarations: dict[str, int] = Field(default_factory=dict)
    by_status: dict[str, int] = Field(default_factory=dict)


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
    #: When the last global pass committed: a declaration stated after it is pending.
    pass_at: datetime | None = None
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


class PreviewRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision_ref: str = Field(pattern=DECISION_REF_PATTERN)
    method: str | None = None
    route: str
    site: ConnectionSite
    outcome: PreviewOutcome
    #: The handler it would reach, ``<path>:<line>`` in the target, when exactly one.
    handler: str | None = None


class HttpConnectionPreviewRequest(BaseModel):
    """What declaring that an origin is served by one repository would do, before it is stored."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    context: CapabilityContext
    group: str = Field(pattern=GROUP_KEY_PATTERN)
    repository: str = Field(min_length=1, max_length=MAX_LINK_TEXT)
    base_path: str = Field(default="", max_length=MAX_LINK_TEXT)
    callers: tuple[str, ...] = Field(default=(), max_length=MAX_LINK_CALLERS)
    environment: str | None = Field(default=None, min_length=1, max_length=MAX_LINK_TEXT)
    limit: int = Field(default=50, ge=1, le=MAX_GROUP_PAGE)

    @field_validator("base_path")
    @classmethod
    def _base_path(cls, value: str) -> str:
        if value and (
            not value.startswith("/")
            or any(c in value for c in "?#{}")
            or any(c.isspace() for c in value)
        ):
            raise ValueError("a base path is empty or starts with `/`, with no query or holes")
        return value.rstrip("/")


class HttpConnectionPreviewResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    context: CapabilityContext
    group: str
    origin: str
    #: The URL the declaration would state.
    url: str
    repository: str
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
    "PreviewRow",
]
