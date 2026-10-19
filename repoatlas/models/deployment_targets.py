"""Deployment targets: where a repository says it is reached, and where a user says it is.

Four questions stay apart for every target, because each has its own evidence:

* *configured* -- something states it: a file (``declared``) or a user (a link);
* *conditions* -- whether the situation it is asked about is one it holds in
  (:mod:`repoatlas.models.deployment_conditions`), and, for a chart, whether the guards
  around its template let it render;
* *resolved* -- whether every part of its URL has a value, and from where
  (:data:`ResolutionBasis`);
* *verified* -- whether a person confirmed it is live. RepoAtlas never contacts a
  deployment, so nothing is ever verified on its own.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from repoatlas.models.capability import CapabilityContext
from repoatlas.models.deployment_conditions import (
    MAX_CONDITIONS,
    ConditionContext,
    ConditionState,
    DeploymentCondition,
)

#: Where a target's statement comes from.
TargetKind = Literal["ingress", "openapi-server", "link"]
#: Where a variable's value comes from: the chart's own ``values.yaml``
#: (``chart-defaults``, not by itself any deployment), a release that sets it
#: (``release``), a user's link (``configured``), the situation asked about
#: (``context``), the chart's ``Chart.yaml`` (``chart``) or the file itself
#: (``declared``).
ResolutionBasis = Literal["chart-defaults", "release", "configured", "context", "chart", "declared"]
ResolutionState = Literal["resolved", "partial", "unresolved"]
GuardState = Literal["met", "unmet", "unknown"]
Verification = Literal["unverified", "user_confirmed"]

#: Longest label, environment, URL or variable a link may carry.
MAX_LINK_TEXT = 300
#: Variables one link may bind.
MAX_LINK_VARIABLES = 32
_VARIABLE_NAME = re.compile(r"[A-Za-z_][\w.\[\]*-]{0,199}")


def link_url_problem(url: str) -> str | None:
    """Why ``url`` cannot be a link's URL, or ``None``: an absolute ``http(s)`` URL with a
    host, and no credentials, query or fragment -- a link names a place, never a secret."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "it is not a URL"
    del port
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return "it is not an absolute http or https URL"
    if parts.username is not None or parts.password is not None:
        return "it carries credentials"
    if parts.query or parts.fragment or "?" in url or "#" in url:
        return "it carries a query or fragment"
    if any(char.isspace() for char in url) or "{" in url or "}" in url:
        return "it holds whitespace or a template hole"
    return None


class DeploymentVariable(BaseModel):
    """One part of a target's URL that is not literal in the file that states it."""

    model_config = ConfigDict(frozen=True)

    #: How a link binds it: a values path (``stage``, ``ingress.hosts[*].host``), or
    #: ``chart``, ``release``, ``namespace``, ``scheme``; an opaque expression is named
    #: as written and cannot be bound.
    name: str
    source: Literal["value", "chart", "release", "namespace", "scheme", "opaque"]
    value: str | None = None
    basis: ResolutionBasis | None = None
    #: A credential's value is never shown nor used; the variable stays unresolved.
    withheld: bool = False


class DeploymentGuard(BaseModel):
    """A template ``if`` around a target, and whether the values at hand meet it."""

    model_config = ConfigDict(frozen=True)

    expression: str
    line: int
    state: GuardState


class DeploymentResolution(BaseModel):
    """A target under one context: the chart's defaults, a release, or a user's link."""

    model_config = ConfigDict(frozen=True)

    basis: Literal["declared", "chart-defaults", "release", "link"]
    #: Who supplies this context: a release name, a link's label, or the file.
    label: str
    environment: str | None = None
    #: ``scheme://host[:port]``, only when every part is resolved.
    origin: str | None = None
    #: The origin plus a literal path prefix, when that is resolved too.
    url: str | None = None
    #: The target's pattern with what this context resolves filled in.
    rendered: str
    state: ResolutionState
    variables: list[DeploymentVariable] = Field(default_factory=list)
    #: Whether the template's guards let it render under this context.
    enabled: GuardState = "met"
    conditions: ConditionState = "none"
    verification: Verification = "unverified"
    link_id: int | None = None
    #: The target and resolution this one repeats, when two reach the same URL.
    duplicate_of: str | None = None


class DeploymentTarget(BaseModel):
    """One place a repository is reached, as stated, with every context it resolves under."""

    model_config = ConfigDict(frozen=True)

    #: Stable across syncs while the stating file and line stay put.
    target_id: str
    kind: TargetKind
    repository: str
    path: str | None = None
    line: int | None = None
    #: ``{scheme}://{chart}-{stage}.example.test/`` -- every hole named.
    pattern: str
    schemes: list[str] = Field(default_factory=list)
    scheme_basis: str | None = None
    guards: list[DeploymentGuard] = Field(default_factory=list)
    resolutions: list[DeploymentResolution] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)
    #: A link whose target is no longer stated by any indexed file.
    detached: bool = False


class DeploymentLinkFields(BaseModel):
    """What a user states about a deployment: where it is, under which conditions, and
    the values of the variables a stated target leaves open."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1, max_length=MAX_LINK_TEXT)
    environment: str | None = Field(default=None, min_length=1, max_length=MAX_LINK_TEXT)
    url: str | None = Field(default=None, max_length=MAX_LINK_TEXT)
    #: A discovered target this link resolves; ``None`` for a link of its own.
    target_id: str | None = Field(default=None, pattern=r"^dt_[0-9a-f]{16}$")
    #: Values for a target's variables; ``{branch}`` or ``{environment}`` takes the
    #: context's value.
    variables: dict[str, str] = Field(default_factory=dict, max_length=MAX_LINK_VARIABLES)
    #: Bounded by a validator rather than ``max_length``, which a schema would spell out
    #: as one tuple type per length.
    conditions: list[DeploymentCondition] = Field(
        default_factory=list, description=f"At most {MAX_CONDITIONS}; all must hold."
    )
    verification: Verification = "unverified"

    @field_validator("conditions")
    @classmethod
    def _conditions(cls, conditions: list[DeploymentCondition]) -> list[DeploymentCondition]:
        if len(conditions) > MAX_CONDITIONS:
            raise ValueError(f"at most {MAX_CONDITIONS} conditions")
        return conditions

    @field_validator("url")
    @classmethod
    def _url(cls, url: str | None) -> str | None:
        if url is not None:
            problem = link_url_problem(url)
            if problem is not None:
                raise ValueError(f"the URL is refused: {problem}")
        return url

    @field_validator("variables")
    @classmethod
    def _variables(cls, variables: dict[str, str]) -> dict[str, str]:
        for name, value in variables.items():
            if not _VARIABLE_NAME.fullmatch(name):
                raise ValueError(f"`{name[:40]}` is not a variable name")
            if not value or len(value) > MAX_LINK_TEXT or any(c.isspace() for c in value):
                raise ValueError(f"the value of `{name}` is empty, too long or has whitespace")
            if any(char in value for char in "/?#@") and name != "scheme":
                raise ValueError(f"the value of `{name}` holds a URL delimiter")
        return variables


class LinkLimitError(ValueError):
    """A repository already carries as many links as a store keeps for one."""


class DeploymentLink(DeploymentLinkFields):
    """A stored link (``deployment_links``), kept by repository slug so it outlives the
    repository's index."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int
    repository: str
    created_at: datetime
    updated_at: datetime
    verified_at: datetime | None = None


#: Every deployment-targets answer says what it rests on, since a reader may take a URL for
#: a claim that something is live.
TARGETS_BASIS = (
    "Deployment targets are read from indexed files (Ingress hosts, OpenAPI servers) and "
    "from links users stated. Nothing is contacted: a resolved URL is what the files and "
    "links render to, not evidence that anything answers there. Only a user can confirm one."
)


class DeploymentTargetsRequest(BaseModel):
    """List where one repository says it is deployed, and what users say about it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    context: CapabilityContext
    branch: str | None = Field(
        default=None,
        max_length=200,
        description="The branch to evaluate conditions for. Defaults to the branch the "
        "repository's checkout is on.",
    )
    environment: str | None = Field(
        default=None, max_length=200, description="The environment to evaluate conditions for."
    )
    pull_request: bool | None = Field(
        default=None, description="Whether conditions are evaluated for a pull request."
    )
    variables: dict[str, str] = Field(
        default_factory=dict,
        max_length=MAX_LINK_VARIABLES,
        description="Values for `variable` conditions.",
    )


class DeploymentTargetsResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    context: CapabilityContext
    repository: str
    #: The branch conditions were evaluated for, and where it came from.
    branch: str | None = None
    branch_basis: Literal["request", "checkout"] | None = None
    environment: str | None = None
    targets: tuple[DeploymentTarget, ...] = ()
    links: tuple[DeploymentLink, ...] = ()
    #: What keeps targets from resolving that belongs to no one target.
    diagnostics: tuple[str, ...] = ()
    basis: str = TARGETS_BASIS


class DeploymentLinkCreateRequest(BaseModel):
    """State where one repository is deployed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    context: CapabilityContext
    link: DeploymentLinkFields


class DeploymentLinkUpdateRequest(BaseModel):
    """Replace what a stored link states."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    context: CapabilityContext
    id: int = Field(ge=1)
    link: DeploymentLinkFields


class DeploymentLinkDeleteRequest(BaseModel):
    """Remove a stored link."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    context: CapabilityContext
    id: int = Field(ge=1)


class DeploymentLinkResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    context: CapabilityContext
    link: DeploymentLink


class DeploymentLinkDeleteResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    context: CapabilityContext
    id: int
    deleted: bool


__all__ = [
    "TARGETS_BASIS",
    "ConditionContext",
    "DeploymentCondition",
    "DeploymentGuard",
    "DeploymentLink",
    "DeploymentLinkCreateRequest",
    "DeploymentLinkDeleteRequest",
    "DeploymentLinkDeleteResponse",
    "DeploymentLinkFields",
    "DeploymentLinkResponse",
    "DeploymentLinkUpdateRequest",
    "DeploymentResolution",
    "DeploymentTarget",
    "DeploymentTargetsRequest",
    "DeploymentTargetsResponse",
    "DeploymentVariable",
    "link_url_problem",
]
