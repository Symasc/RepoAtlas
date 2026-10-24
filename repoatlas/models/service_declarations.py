"""Service declarations: what a person states about the places a request goes that no indexed
repository answers, and about the redirects and callbacks around them.

Three kinds, one discriminated union on ``kind``:

* ``external_service`` -- an origin classified as a service outside the index, with a label, an
  optional provider, category and note, and the repositories (at an environment) it authenticates
  for. Classifying an origin never links a request to a handler: the request keeps its decision.
* ``redirect`` -- a redirect a person says happens from an origin (and a literal path prefix) or
  from a route a repository serves, to a destination URL that may lie outside the index, or to an
  unknown one. It is the person's statement and nothing is inferred from it: there is no method,
  status code or delivery field, because none of them is known from a declaration.
* ``permitted_callback`` -- a destination an external provider's allow-list permits. A permission
  is not proof that any redirect occurs, so it never stands for one.

Nothing here contacts a URL. Every URL passes the same checks as a deployment link's
(:func:`repoatlas.models.deployment_targets.link_url_problem`) and may not name a wildcard host;
every text a person writes is refused if it holds a control character or a credential.
"""

from __future__ import annotations

import unicodedata
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationInfo,
    field_validator,
    model_validator,
)

from repoatlas.models import http
from repoatlas.models.deployment_targets import (
    MAX_LINK_CALLERS,
    MAX_LINK_TEXT,
    MAX_NOTE,
    declared_base,
    is_loopback_url,
    link_url_problem,
)
from repoatlas.models.secrets import secret_bearing

ServiceDeclarationKind = Literal["external_service", "redirect", "permitted_callback"]

#: Longest provider or category a person may write.
MAX_SERVICE_WORDS = 100


def _clean(value: str, what: str, *, multiline: bool = False) -> str:
    """``value`` stripped, refused if it holds a control character or a credential."""
    allowed = "\n\t" if multiline else ""
    control = any(unicodedata.category(c) == "Cc" and c not in allowed for c in value)
    if control or secret_bearing(value):
        raise ValueError(f"{what} holds a control character or a credential")
    return value.strip()


def _required(value: str, what: str) -> str:
    text = _clean(value, what)
    if not text:
        raise ValueError(f"{what} is empty")
    return text


def _optional(value: str | None, what: str, *, multiline: bool = False) -> str | None:
    return None if value is None else (_clean(value, what, multiline=multiline) or None)


def _slug(value: str) -> str:
    if not value or len(value) > MAX_LINK_TEXT or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("a repository is named by its slug, without whitespace")
    return value


def _url_problem(url: str) -> str | None:
    """Why ``url`` cannot be stated, or ``None``: a link's rules, no wildcard host, no credential
    and a form the HTTP identities can read."""
    problem = link_url_problem(url)
    if problem is not None:
        return problem
    if "*" in urlsplit(url).netloc:
        return "it names a wildcard host"
    if secret_bearing(url):
        return "it carries a credential"
    target = http.client_target(url)
    if target is None or target.origin is None:
        return "it is not a plain absolute http or https URL"
    return None


def _checked_origin(value: str, what: str, *, allow_loopback: bool = False) -> str:
    """The canonical ``scheme://host[:port]`` of an origin, or a refusal."""
    problem = _url_problem(value)
    if problem is not None:
        raise ValueError(f"{what} is refused: {problem}")
    if urlsplit(value).path not in ("", "/"):
        raise ValueError(f"{what} is scheme://host[:port], with no path")
    if not allow_loopback and is_loopback_url(value):
        raise ValueError(
            f"{what} names this machine, which is a different service in each repository"
        )
    canonical = declared_base(value)
    if canonical is None:
        raise ValueError(f"{what} is not an absolute http or https origin")
    return canonical


def _checked_destination(value: str | None) -> str | None:
    if value is not None:
        problem = _url_problem(value)
        if problem is not None:
            raise ValueError(f"the destination is refused: {problem}")
    return value


def _literal_path(value: str) -> str:
    """A path of literal segments, without its trailing ``/``; ``""`` for the whole origin."""
    if value in ("", "/"):
        return ""
    path = http.route_path(value)
    if path is None or secret_bearing(path):
        raise ValueError("a source path is a plain path, without a query, fragment or whitespace")
    for segment in path.strip("/").split("/"):
        if (
            not segment
            or segment.startswith(":")
            or any(c in segment for c in "{}*<>")
            or http.is_parameter_segment(segment)
        ):
            raise ValueError("a source path holds literal segments: no hole, wildcard or empty")
    return path.rstrip("/")


class AuthenticationTarget(BaseModel):
    """A repository (at an environment) an external service authenticates for."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(min_length=1, max_length=MAX_LINK_TEXT)
    #: ``None``: whatever environment the repository runs in.
    environment: str | None = Field(default=None, min_length=1, max_length=MAX_LINK_TEXT)

    @field_validator("repository")
    @classmethod
    def _repository(cls, value: str) -> str:
        return _slug(value)

    @field_validator("environment")
    @classmethod
    def _environment(cls, value: str | None) -> str | None:
        return _optional(value, "the environment")


class ExternalServiceFields(BaseModel):
    """An origin no indexed repository answers, classified as a service outside the index."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["external_service"] = "external_service"
    #: ``scheme://host[:port]``, canonical, never loopback, never a wildcard.
    origin: str = Field(max_length=MAX_LINK_TEXT)
    label: str = Field(min_length=1, max_length=MAX_LINK_TEXT)
    provider: str | None = Field(default=None, max_length=MAX_SERVICE_WORDS)
    category: str | None = Field(default=None, max_length=MAX_SERVICE_WORDS)
    note: str | None = Field(default=None, max_length=MAX_NOTE)
    #: Who it authenticates for. Never a deployment link and never read by the join.
    authenticates: list[AuthenticationTarget] = Field(
        default_factory=list,
        description=f"At most {MAX_LINK_CALLERS}, each repository and environment once.",
    )

    @field_validator("origin")
    @classmethod
    def _origin(cls, value: str) -> str:
        return _checked_origin(value, "the origin")

    @field_validator("label")
    @classmethod
    def _label(cls, value: str) -> str:
        return _required(value, "the label")

    @field_validator("provider", "category")
    @classmethod
    def _words(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _optional(value, f"the {info.field_name}")

    @field_validator("note")
    @classmethod
    def _note(cls, value: str | None) -> str | None:
        return _optional(value, "the note", multiline=True)

    @field_validator("authenticates")
    @classmethod
    def _authenticates(cls, value: list[AuthenticationTarget]) -> list[AuthenticationTarget]:
        if len(value) > MAX_LINK_CALLERS:
            raise ValueError(f"at most {MAX_LINK_CALLERS} authenticated repositories")
        if len({(t.repository, t.environment) for t in value}) != len(value):
            raise ValueError("a repository and environment is named once")
        return sorted(value, key=lambda t: (t.repository, t.environment or ""))

    @property
    def identity_key(self) -> str:
        return self.origin

    @property
    def subject(self) -> str:
        return self.origin


class RedirectSource(BaseModel):
    """Where a redirect starts: an origin and a literal path prefix, or a route one repository
    serves."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    form: Literal["origin", "route"]
    #: ``form == "origin"``: ``scheme://host[:port]``, never loopback.
    origin: str | None = Field(default=None, max_length=MAX_LINK_TEXT)
    #: ``form == "origin"``: literal segments after the origin, or empty for the whole origin.
    path: str = Field(default="", max_length=MAX_LINK_TEXT)
    #: ``form == "route"``: the repository slug that serves the route.
    repository: str | None = Field(default=None, min_length=1, max_length=MAX_LINK_TEXT)
    #: ``form == "route"``: a canonical route, ``GET /login``.
    route: str | None = Field(default=None, min_length=1, max_length=MAX_LINK_TEXT)

    @field_validator("origin")
    @classmethod
    def _origin(cls, value: str | None) -> str | None:
        return None if value is None else _checked_origin(value, "the source origin")

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        return _literal_path(value)

    @field_validator("repository")
    @classmethod
    def _repository(cls, value: str | None) -> str | None:
        return None if value is None else _slug(value)

    @field_validator("route")
    @classmethod
    def _route(cls, value: str | None) -> str | None:
        if value is not None and (http.canonical_route(value) != value or secret_bearing(value)):
            raise ValueError("a source route is a canonical route such as `GET /login`")
        return value

    @model_validator(mode="after")
    def _one_form(self) -> RedirectSource:
        if self.form == "origin":
            if self.origin is None or self.repository is not None or self.route is not None:
                raise ValueError("an origin source states an origin, and no repository or route")
        elif self.repository is None or self.route is None or self.origin is not None or self.path:
            raise ValueError("a route source states a repository and a route, and no origin")
        return self

    @property
    def identity_key(self) -> str:
        if self.form == "origin":
            return f"origin {self.origin}{self.path}"
        return f"route {self.repository} {self.route}"

    @property
    def shown(self) -> str:
        if self.form == "origin":
            return f"{self.origin}{self.path}"
        return f"{self.route} in {self.repository}"


class RedirectFields(BaseModel):
    """A redirect a person states from a source to a destination, or to an unknown one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["redirect"] = "redirect"
    source: RedirectSource
    #: An absolute URL, possibly outside the index, never contacted; ``None``: not known.
    destination: str | None = Field(default=None, max_length=MAX_LINK_TEXT)
    #: When it happens, in the person's words. Shown, never parsed.
    condition: str | None = Field(default=None, max_length=MAX_NOTE)
    label: str | None = Field(default=None, max_length=MAX_LINK_TEXT)
    note: str | None = Field(default=None, max_length=MAX_NOTE)

    @field_validator("destination")
    @classmethod
    def _destination(cls, value: str | None) -> str | None:
        return _checked_destination(value)

    @field_validator("condition", "label")
    @classmethod
    def _words(cls, value: str | None, info: ValidationInfo) -> str | None:
        return _optional(value, f"the {info.field_name}")

    @field_validator("note")
    @classmethod
    def _note(cls, value: str | None) -> str | None:
        return _optional(value, "the note", multiline=True)

    @property
    def identity_key(self) -> str:
        return self.source.identity_key

    @property
    def subject(self) -> str:
        return self.source.shown


class PermittedCallbackFields(BaseModel):
    """A destination an external provider is configured to allow: permitted, not proof that a
    redirect occurs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["permitted_callback"] = "permitted_callback"
    #: The origin of the provider that allows it; the API requires it to be a classified external
    #: service.
    provider_origin: str = Field(max_length=MAX_LINK_TEXT)
    destination: str = Field(max_length=MAX_LINK_TEXT)
    label: str | None = Field(default=None, max_length=MAX_LINK_TEXT)
    note: str | None = Field(default=None, max_length=MAX_NOTE)

    @field_validator("provider_origin")
    @classmethod
    def _provider_origin(cls, value: str) -> str:
        return _checked_origin(value, "the provider origin")

    @field_validator("destination")
    @classmethod
    def _destination(cls, value: str) -> str:
        checked = _checked_destination(value)
        assert checked is not None
        return checked

    @field_validator("label")
    @classmethod
    def _label(cls, value: str | None) -> str | None:
        return _optional(value, "the label")

    @field_validator("note")
    @classmethod
    def _note(cls, value: str | None) -> str | None:
        return _optional(value, "the note", multiline=True)

    @property
    def identity_key(self) -> str:
        return f"callback {self.provider_origin}"

    @property
    def subject(self) -> str:
        return self.provider_origin


#: What a person states: one of the three kinds, told apart by ``kind``.
ServiceDeclarationFields = Annotated[
    ExternalServiceFields | RedirectFields | PermittedCallbackFields,
    Field(discriminator="kind"),
]

#: Reads fields of any kind (a stored row, an import) into the class their ``kind`` names.
SERVICE_DECLARATION_FIELDS: TypeAdapter[
    ExternalServiceFields | RedirectFields | PermittedCallbackFields
] = TypeAdapter(ServiceDeclarationFields)


class ServiceDeclaration(BaseModel):
    """A stored declaration (``service_declarations``): its fields and the row's bookkeeping."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int
    fields: ServiceDeclarationFields
    created_at: datetime
    updated_at: datetime

    @property
    def kind(self) -> ServiceDeclarationKind:
        return self.fields.kind

    @property
    def identity_key(self) -> str:
        return self.fields.identity_key


class ServiceDeclarationLimitError(ValueError):
    """The store already keeps as many service declarations as it allows."""


class ServiceDeclarationConflictError(ValueError):
    """An origin is already classified as an external service, in other words."""

    def __init__(self, declaration_id: int, subject: str) -> None:
        super().__init__(
            f"{subject} is already classified as external service #{declaration_id}: "
            "update it, or revoke it first"
        )
        self.declaration_id = declaration_id


class ServiceDeclarationIdentityError(ValueError):
    """An update would change what a declaration is about."""

    def __init__(self) -> None:
        super().__init__(
            "what a declaration is about cannot change: revoke the declaration and declare it anew"
        )


__all__ = [
    "MAX_SERVICE_WORDS",
    "SERVICE_DECLARATION_FIELDS",
    "AuthenticationTarget",
    "ExternalServiceFields",
    "PermittedCallbackFields",
    "RedirectFields",
    "RedirectSource",
    "ServiceDeclaration",
    "ServiceDeclarationConflictError",
    "ServiceDeclarationFields",
    "ServiceDeclarationIdentityError",
    "ServiceDeclarationKind",
    "ServiceDeclarationLimitError",
]
