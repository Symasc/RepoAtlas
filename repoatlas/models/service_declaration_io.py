"""The requests and answers of the service-declaration endpoints (``service_declarations/*``).

A person classifies an origin the index cannot identify as an external service, previews what that
would change before storing it, edits or revokes it, and reads the history. Every answer says what
it rests on: a classification is a person's statement, applied when read and never joined, so a
request it concerns keeps its own decision and chain.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from repoatlas.models.capability import CapabilityContext
from repoatlas.models.deployment_targets import DeclarationProvenance
from repoatlas.models.http_connections import (
    GROUP_KEY_PATTERN,
    MAX_GROUP_PAGE,
    ConnectionSite,
    RequestIdentity,
)
from repoatlas.models.http_inventory import DECISION_REF_PATTERN
from repoatlas.models.service_declarations import (
    ServiceDeclarationFields,
    ServiceDeclarationKind,
    ServiceDeclarationView,
)

SERVICE_BASIS = (
    "A classification is an operator's statement, never evidence: it says an origin is a service "
    "outside the index and joins nothing. A request it concerns keeps its decision and its chain; "
    "only its identity is shown as a known external dependency. Nothing is contacted."
)

#: Audit rows one history read returns at most.
MAX_SERVICE_HISTORY_PAGE = 500


class ServiceDeclarationPreviewRequest(BaseModel):
    """What stating a declaration would change, before it is stored."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    context: CapabilityContext
    fields: ServiceDeclarationFields
    group: str | None = Field(
        default=None,
        pattern=GROUP_KEY_PATTERN,
        description="The worklist group it is made from: its origin must be the declared one.",
    )
    id: int | None = Field(
        default=None, ge=1, description="The declaration this one would replace, when editing."
    )
    limit: int = Field(default=50, ge=1, le=MAX_GROUP_PAGE)


class ServiceEffectCounts(BaseModel):
    """What a declaration would do, in requests."""

    model_config = ConfigDict(frozen=True)

    #: Requests the join left unmatched that would be shown as a known external dependency.
    would_be_known_external: int = 0
    #: Requests at the origin the source already links: they stay identified.
    stay_identified: int = 0
    #: Redirect steps the declaration would add to requests.
    redirect_steps_added: int = 0
    #: Requests at the declared origin, whatever becomes of them.
    requests_reached: int = 0


class ServiceDeclarationPreviewRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision_ref: str = Field(pattern=DECISION_REF_PATTERN)
    method: str | None = None
    route: str
    site: ConnectionSite
    identity_before: RequestIdentity
    identity_after: RequestIdentity
    steps_added: int = 0


class ServiceDeclarationPreviewResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    context: CapabilityContext
    group: str | None = None
    kind: ServiceDeclarationKind
    #: What the declaration is about: the origin it classifies.
    subject: str
    counts: ServiceEffectCounts = Field(default_factory=ServiceEffectCounts)
    rows: tuple[ServiceDeclarationPreviewRow, ...] = ()
    has_more: bool = False
    #: What must change before it can be stored; empty when it can.
    requirements: tuple[str, ...] = ()
    #: An existing declaration this one repeats (it would be returned, not stored twice).
    duplicate_of: int | None = None
    #: One of the same origin in other words.
    conflicts_with: int | None = None
    can_create: bool
    basis: str = SERVICE_BASIS

    @model_validator(mode="after")
    def _requirements_block(self) -> ServiceDeclarationPreviewResponse:
        if self.requirements and self.can_create:
            raise ValueError("a preview with requirements cannot be created")
        return self


class ServiceDeclarationCreateRequest(BaseModel):
    """State a declaration. The checks of the preview are run again before anything is stored."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    context: CapabilityContext
    fields: ServiceDeclarationFields
    provenance: DeclarationProvenance | None = None


class ServiceDeclarationUpdateRequest(BaseModel):
    """Replace what a stored declaration states; what it is about cannot change."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    context: CapabilityContext
    id: int = Field(ge=1)
    fields: ServiceDeclarationFields
    provenance: DeclarationProvenance | None = None


class ServiceDeclarationDeleteRequest(BaseModel):
    """Revoke a declaration. The history keeps what it stated and who revoked it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    context: CapabilityContext
    id: int = Field(ge=1)
    provenance: DeclarationProvenance | None = None


class ServiceDeclarationResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    context: CapabilityContext
    declaration: ServiceDeclarationView
    basis: str = SERVICE_BASIS


class ServiceDeclarationDeleteResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    context: CapabilityContext
    id: int
    deleted: bool


class ServiceDeclarationHistoryRequest(BaseModel):
    """What operators did to service declarations, newest first."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    context: CapabilityContext
    id: int | None = Field(default=None, ge=1, description="One declaration's history.")
    kind: ServiceDeclarationKind | None = None
    limit: int = Field(default=100, ge=1, le=MAX_SERVICE_HISTORY_PAGE)


class ServiceHistoryEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    at: datetime
    action: Literal["create", "update", "revoke", "import"]
    kind: ServiceDeclarationKind
    subject_id: int
    #: What the declaration was about: the origin, the redirect source or the callback provider.
    subject: str
    via: Literal["ui", "api", "cli", "import"]
    request_group: str | None = None
    note: str | None = None
    before: dict[str, object] | None = None
    after: dict[str, object] | None = None


class ServiceDeclarationHistoryResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    context: CapabilityContext
    entries: tuple[ServiceHistoryEntry, ...] = ()
    #: Actions only. What a classification means for a request is read, not recorded.
    basis: str = (
        "Operators' actions on service declarations, append-only and local to this index. It "
        "records what was stated, changed, revoked or imported, never what a read made of it."
    )


__all__ = [
    "MAX_SERVICE_HISTORY_PAGE",
    "SERVICE_BASIS",
    "ServiceDeclarationCreateRequest",
    "ServiceDeclarationDeleteRequest",
    "ServiceDeclarationDeleteResponse",
    "ServiceDeclarationHistoryRequest",
    "ServiceDeclarationHistoryResponse",
    "ServiceDeclarationPreviewRequest",
    "ServiceDeclarationPreviewResponse",
    "ServiceDeclarationPreviewRow",
    "ServiceDeclarationResponse",
    "ServiceDeclarationUpdateRequest",
    "ServiceEffectCounts",
    "ServiceHistoryEntry",
]
