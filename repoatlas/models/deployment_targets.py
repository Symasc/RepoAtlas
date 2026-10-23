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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from repoatlas.models import http
from repoatlas.models.capability import CapabilityContext
from repoatlas.models.deployment_conditions import (
    MAX_CONDITIONS,
    ConditionContext,
    ConditionState,
    DeploymentCondition,
)
from repoatlas.models.secrets import credential_named, secret_bearing

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


#: Hosts that name this machine. An origin alone never says which service answers there: the same
#: loopback address is a different program in every repository's dev setup.
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]"})  # noqa: S104 - names, never bound
#: Repositories one link may be scoped to.
MAX_LINK_CALLERS = 20


def is_loopback_url(url: str) -> bool:
    """Whether ``url`` names this machine (``localhost``, ``127.x``, ``::1``, ``0.0.0.0``)."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return host in _LOOPBACK_HOSTS or host.startswith("127.") or host.endswith(".localhost")


def declared_base(url: str) -> str | None:
    """The base a link's URL serves routes under, in one canonical spelling: the lower-cased
    origin (default port dropped) plus its path with no trailing ``/``. ``None`` when it is
    not an absolute http(s) URL. Two links with the same base state the same place."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    target = http.client_target(f"{parts.scheme}://{parts.netloc}/")
    if target is None or target.origin is None:
        return None
    return target.origin + parts.path.rstrip("/")


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
    #: Where the value is stated, ``<path>:<line>`` in the repository that states it: the
    #: chart's ``values.yaml``, a release's ``-f`` values file, its CI step.
    at: str | None = None


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
    #: What keeps this context from resolving, or qualifies what it resolves: a values
    #: file not read, a value set from a CI expression, a value left empty.
    notes: list[str] = Field(default_factory=list)


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
    #: For a target another repository states, the repository asked about, whose built
    #: code that repository's release is configured to run (``container_artifact``).
    runs: str | None = None


_SCOPE_SYMBOL = re.compile(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*")
#: What a scope may show of a base: a plain name in braces, or the masked form ``{…}`` that
#: ``http_declarations.display_base`` shows for anything else.
_SCOPE_SHOWN_BASE = re.compile(r"\{(?:[A-Za-z_$][\w$]*|…)\}")


class DeclarationScope(BaseModel):
    """The words of a scoped declaration: which unstated base, in which file (and symbol) of its
    one caller, it serves. The base is kept by hash; only a plain name is ever shown."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Relative POSIX path of the file in the caller repository.
    path: str = Field(min_length=1, max_length=MAX_LINK_TEXT)
    #: A dotted qualified name of one entity of that file; ``None``: the file's module level.
    symbol: str | None = Field(default=None, min_length=1, max_length=MAX_LINK_TEXT)
    #: ``hbk1-`` and the hash of the base text (``http_scopes.base_key``), never the text.
    base_key: str = Field(pattern=r"^hbk1-[0-9a-f]{20}$")
    #: The base as shown: ``{name}`` for a plain name, else ``{…}``.
    base_shown: str = Field(max_length=MAX_LINK_TEXT)
    #: A literal path prefix after the base, like a preview's ``base_path``.
    base_path: str = Field(default="", max_length=MAX_LINK_TEXT)

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        if value.startswith("/") or "\\" in value or ".." in value.split("/"):
            raise ValueError("a scope path is relative, with `/` separators and no `..` segment")
        return value

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, value: str | None) -> str | None:
        if value is not None and not _SCOPE_SYMBOL.fullmatch(value):
            raise ValueError("a scope symbol is a dotted qualified name")
        return value

    @field_validator("base_shown")
    @classmethod
    def _base_shown(cls, value: str) -> str:
        if not _SCOPE_SHOWN_BASE.fullmatch(value) or secret_bearing(value):
            raise ValueError("a shown base is `{name}` for a plain name, or `{…}`")
        return value

    @field_validator("base_path")
    @classmethod
    def _base_path(cls, value: str) -> str:
        # Kept in step with HttpConnectionPreviewRequest._base_path, which this module cannot
        # import (that module imports this one).
        if value and (
            not value.startswith("/")
            or any(c in value for c in "?#{}")
            or any(c.isspace() for c in value)
        ):
            raise ValueError("a base path is empty or starts with `/`, with no query or holes")
        return value.rstrip("/")


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
    #: Repositories whose requests this link may join. Empty: any. A link at a loopback origin
    #: must name them, because that origin means a different service in each repository.
    callers: list[str] = Field(
        default_factory=list, description=f"At most {MAX_LINK_CALLERS} repository slugs."
    )
    #: A declaration scoped to one file (and symbol) of its one caller, for a base the caller's
    #: code does not state. ``None``: an ordinary link.
    scope: DeclarationScope | None = None

    @field_validator("callers")
    @classmethod
    def _callers(cls, callers: list[str]) -> list[str]:
        if len(callers) > MAX_LINK_CALLERS:
            raise ValueError(f"at most {MAX_LINK_CALLERS} caller repositories")
        for slug in callers:
            if (
                not slug
                or len(slug) > MAX_LINK_TEXT
                or any(c.isspace() or ord(c) < 32 for c in slug)
            ):
                raise ValueError("a caller repository is a slug, without whitespace")
        if len(set(callers)) != len(callers):
            raise ValueError("a caller repository is named once")
        return sorted(callers)

    @model_validator(mode="after")
    def _loopback_is_scoped(self) -> DeploymentLinkFields:
        if self.url is not None and is_loopback_url(self.url):
            if not self.callers:
                raise ValueError(
                    "a loopback origin names a different service in each repository: "
                    "name the caller repositories it applies to"
                )
            if not self.environment:
                raise ValueError("a loopback origin belongs to an environment: name it")
        return self

    @model_validator(mode="after")
    def _scope_is_alone(self) -> DeploymentLinkFields:
        if self.scope is None:
            return self
        if self.url is not None or self.target_id is not None:
            raise ValueError("a scoped declaration states no URL and resolves no target")
        if self.variables or self.conditions:
            raise ValueError("a scoped declaration binds no variables and holds no conditions")
        if len(self.callers) != 1:
            raise ValueError("a scoped declaration names exactly one caller repository")
        return self

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
            if credential_named(name) or secret_bearing(value):
                # A link names a place; a credential has no business in one.
                raise ValueError(f"`{name[:40]}` names or holds a credential")
            if not value or len(value) > MAX_LINK_TEXT or any(c.isspace() for c in value):
                raise ValueError(f"the value of `{name}` is empty, too long or has whitespace")
            if any(char in value for char in "/?#@") and name != "scheme":
                raise ValueError(f"the value of `{name}` holds a URL delimiter")
        return variables


class LinkLimitError(ValueError):
    """A repository already carries as many links as a store keeps for one."""


class LinkConflictError(ValueError):
    """Another repository already states this place, for callers this link would also serve."""

    def __init__(self, link_id: int, repository: str) -> None:
        super().__init__(
            f"link {link_id} already states that {repository} is at this URL for the same callers: "
            "two services at one place join nothing; revoke it first"
        )
        self.link_id = link_id
        self.repository = repository


class ScopeConflictError(LinkConflictError):
    """A scoped declaration contradicts another declaration over the same scope."""

    def __init__(self, message: str) -> None:
        ValueError.__init__(self, message)


MAX_NOTE = 500


class DeclarationProvenance(BaseModel):
    """Where a declaration came from, for the history only; never what the link states."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The explorer or a direct API call; the CLI and an import set their own.
    via: Literal["ui", "api"] = "api"
    #: Why, in the person's words. Never read by the join; refused if it carries a credential.
    note: str | None = Field(default=None, max_length=MAX_NOTE)
    #: The worklist group the declaration was made from (``hcg1-<hex>``): with it, the target must
    #: serve a route one of the group's requests matches, or nothing is stored.
    request_group: str | None = Field(default=None, pattern=r"^hcg1-[0-9a-f]{20}$")

    @field_validator("note")
    @classmethod
    def _note(cls, note: str | None) -> str | None:
        if note is None:
            return None
        if any(ord(c) < 32 and c not in "\n\t" for c in note) or secret_bearing(note):
            raise ValueError("a note holds a control character or a credential")
        return note.strip() or None


class DeploymentLink(DeploymentLinkFields):
    """A stored link (``deployment_links``), kept by repository slug so it outlives the
    repository's index."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int
    repository: str
    created_at: datetime
    updated_at: datetime
    verified_at: datetime | None = None


#: The format ``repoatlas links export`` writes and ``repoatlas links import`` reads.
LINKS_EXPORT_FORMAT = "repoatlas/deployment-links/v1"


class ExportedLink(DeploymentLinkFields):
    """One link as exported: what it states, and the repository it is about."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(min_length=1, max_length=MAX_LINK_TEXT)


class LinksExport(BaseModel):
    """Every deployment link of an index, outside it: the only state a user writes there,
    so rebuilding or moving the index need not lose it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    format: Literal["repoatlas/deployment-links/v1"] = LINKS_EXPORT_FORMAT
    links: list[ExportedLink] = Field(default_factory=list)


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
    provenance: DeclarationProvenance | None = None


class DeploymentLinkUpdateRequest(BaseModel):
    """Replace what a stored link states."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    context: CapabilityContext
    id: int = Field(ge=1)
    link: DeploymentLinkFields
    provenance: DeclarationProvenance | None = None


class DeploymentLinkDeleteRequest(BaseModel):
    """Remove a stored link. The history keeps what it stated and who revoked it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    context: CapabilityContext
    id: int = Field(ge=1)
    provenance: DeclarationProvenance | None = None


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
    "DeclarationProvenance",
    "DeclarationScope",
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
    "LinkConflictError",
    "ScopeConflictError",
    "declared_base",
    "is_loopback_url",
    "link_url_problem",
]
