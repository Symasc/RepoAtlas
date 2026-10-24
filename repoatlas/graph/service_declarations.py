"""What an operator's service declarations rest on, read from the join's stored decisions.

Pure: no store, no clock. The capabilities (:mod:`repoatlas.capabilities.http_connections`,
:mod:`repoatlas.capabilities.service_declarations`) call it; nothing here decides a link, which only
:mod:`repoatlas.graph.http_crossings` does at a sync, and nothing here changes a decision, a chain
or an edge.

* An origin may be **classified** as an external service only where the index has said nothing
  about it: every stored decision at it is
  :data:`~repoatlas.graph.http_crossings.SERVICE_UNMATCHED`,
  no deployment link states it as a deployment, it names no machine, and it is not already
  classified in other words (:func:`classification_check`). A classification is **ownership
  separation**, not a mapping: the source's answer and an operator's mapping are never overridden.
* A classification is applied at read time. A request whose decision is ``http-service-unmatched``
  at an origin with an applied classification has the identity ``known-external``
  (:func:`identity_of`); its status, reason and chain are the join's, untouched.
* A classification's state is derived, never stored (:func:`external_state`).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass

from repoatlas.graph.http_crossings import SERVICE_UNMATCHED
from repoatlas.graph.http_declarations import RequestFacts, is_loopback_origin, origin_of_link
from repoatlas.models import http
from repoatlas.models.deployment_targets import DeploymentLink, declared_base
from repoatlas.models.http_connections import RequestIdentity
from repoatlas.models.service_declarations import (
    ExternalServiceFields,
    ServiceDeclaration,
    ServiceDeclarationReason,
    ServiceDeclarationState,
)

UNRESOLVED: RequestIdentity = "unresolved"
KNOWN_EXTERNAL: RequestIdentity = "known-external"
IDENTIFIED: RequestIdentity = "identified"


@dataclass(frozen=True, slots=True)
class Classification:
    """One stored classification of an origin as an external service."""

    id: int
    fields: ExternalServiceFields


@dataclass(frozen=True, slots=True)
class ClassificationCheck:
    """Whether stating ``fields`` is allowed against what the index holds.

    ``requirements`` is what must change first (empty: it may be stored); ``duplicate_of`` is the
    classification it repeats word for word (it would be returned, not stored twice);
    ``conflicts_with`` is one of the same origin in other words."""

    requirements: tuple[str, ...] = ()
    duplicate_of: int | None = None
    conflicts_with: int | None = None


def classified_text(declaration_id: int) -> str:
    """How a refusal says an origin is already classified, for the Map and link checks alike."""
    return f"classified as external service #{declaration_id}; revoke it first"


def origin_of_url(url: str) -> str | None:
    """The origin a URL names, in the spelling the decisions use; ``None`` when it names none."""
    base = declared_base(url)
    target = http.client_target(base + "/") if base else None
    return target.origin if target is not None else None


def classifications(declarations: Iterable[ServiceDeclaration]) -> dict[str, Classification]:
    """The external-service declarations by origin; the oldest wins if ever two share one."""
    found: dict[str, Classification] = {}
    for declaration in sorted(declarations, key=lambda item: item.id):
        fields = declaration.fields
        if isinstance(fields, ExternalServiceFields):
            found.setdefault(fields.origin, Classification(declaration.id, fields))
    return found


def link_origins(links: Iterable[DeploymentLink]) -> dict[str, DeploymentLink]:
    """The unscoped deployment links by the origin their URL names; the oldest wins. A scoped
    declaration states no URL and names no origin."""
    found: dict[str, DeploymentLink] = {}
    for link in sorted(links, key=lambda item: item.id):
        if link.scope is None:
            origin = origin_of_link(link)
            if origin is not None:
                found.setdefault(origin, link)
    return found


def classifiable_origins(
    facts: Iterable[RequestFacts], classified: Mapping[str, Classification]
) -> set[str]:
    """The origins a person may be offered to classify: a stated origin some request of which the
    join left ``http-service-unmatched``, that is not loopback and not classified yet. Whether a
    classification is then allowed is :func:`classification_check`'s."""
    return {
        fact.target.origin
        for fact in facts
        if fact.target.origin is not None
        and fact.status == SERVICE_UNMATCHED
        and not is_loopback_origin(fact.target.origin)
        and fact.target.origin not in classified
    }


def identity_of(fact: RequestFacts, known: Mapping[str, int]) -> tuple[RequestIdentity, int | None]:
    """A request's identity and the classification behind it. ``known`` maps the origins with an
    **applied** classification to its id. Only an ``http-service-unmatched`` request becomes
    ``known-external``; a linked one is ``identified``; every other is ``unresolved``."""
    if fact.linked:
        return IDENTIFIED, None
    origin = fact.target.origin
    if fact.status == SERVICE_UNMATCHED and origin is not None and origin in known:
        return KNOWN_EXTERNAL, known[origin]
    return UNRESOLVED, None


def classification_check(
    fields: ExternalServiceFields,
    facts: Sequence[RequestFacts],
    links: Iterable[DeploymentLink],
    classified: Mapping[str, Classification],
    *,
    own_id: int | None = None,
    indexed: Collection[str] | None = None,
) -> ClassificationCheck:
    """Whether ``fields`` may be stored (``own_id``: as the replacement of that classification).

    Refused when the origin names this machine; when any stored decision at the origin is not
    ``http-service-unmatched`` (the source has already said something there, linked or refused);
    when an unscoped deployment link states the origin as a deployment; when the origin is
    already classified in other words; or, with ``indexed`` (the slugs of the indexed
    repositories), when an authentication target is not one of them. Every refusal is listed,
    none is resolved. An import does not run this check: :func:`external_state` judges it at read
    time."""
    origin = fields.origin
    requirements: list[str] = []
    if indexed is not None:
        for target in fields.authenticates:
            if target.repository not in indexed:
                requirements.append(
                    f"{target.repository} is not an indexed repository: an authentication target "
                    "is an application service or deployment the index holds"
                )
    if is_loopback_origin(origin):
        requirements.append(
            f"{origin} names this machine, which is a different service in each repository: "
            "it is not an external service"
        )
    decided = Counter(
        fact.status
        for fact in facts
        if fact.target.origin == origin and fact.status != SERVICE_UNMATCHED
    )
    for status, count in sorted(decided.items()):
        requirements.append(
            f"{count} request(s) at {origin} are {status}: the source already says something "
            "about that origin, so it is not classified as an external service"
        )
    named = link_origins(links).get(origin)
    if named is not None:
        requirements.append(
            f"deployment link #{named.id} of {named.repository} states {origin} as a deployment: "
            "revoke it first"
        )
    duplicate_of = conflicts_with = None
    held = classified.get(origin)
    if held is not None and held.id != own_id:
        if held.fields == fields:
            duplicate_of = held.id
        else:
            conflicts_with = held.id
            requirements.append(
                f"{origin} is already classified as external service #{held.id}: update it, or "
                "revoke it first"
            )
    return ClassificationCheck(tuple(dict.fromkeys(requirements)), duplicate_of, conflicts_with)


def external_state(
    fields: ExternalServiceFields,
    at_origin: Sequence[RequestFacts],
    link: DeploymentLink | None,
    indexed: set[str],
    *,
    built: bool,
) -> tuple[ServiceDeclarationState, ServiceDeclarationReason]:
    """Where a classification stands against the stored decisions; the first rule that holds.

    ``at_origin``: every stored decision whose request names the origin; ``link``: the unscoped
    deployment link that states it, if any; ``indexed``: the slugs of the indexed repositories.
    Only an ``applied`` classification gives its requests an identity."""
    if not built:
        return "ineffective", "index-not-built"
    if not at_origin:
        return "invalidated", "origin-no-longer-requested"
    if link is not None:
        return "ineffective", "origin-declared-as-deployment"
    if any(fact.status != SERVICE_UNMATCHED for fact in at_origin):
        return "ineffective", "source-identifies-origin"
    if any(target.repository not in indexed for target in fields.authenticates):
        return "invalidated", "authenticated-service-not-indexed"
    return "applied", ""


__all__ = [
    "IDENTIFIED",
    "KNOWN_EXTERNAL",
    "UNRESOLVED",
    "Classification",
    "ClassificationCheck",
    "classifiable_origins",
    "classification_check",
    "classifications",
    "classified_text",
    "external_state",
    "identity_of",
    "link_origins",
    "origin_of_url",
]
