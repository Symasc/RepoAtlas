"""Repository inclusions: one repository including another at a path (submodules and the
manifests that state them; :mod:`repoatlas.parsers.inclusions`).

The global pass decides each inclusion a repository's files state (:mod:`repoatlas.graph.
inclusions`), and records one of these decisions about it:

* ``linked`` -- one indexed repository is the included one, proven by its **path** (it is
  checked out where the entry says) or its **remote** (exactly one indexed repository has the
  URL the entry names), or **declared** by a user when neither proves one;
* ``ambiguous`` -- more than one indexed repository has the URL, and nothing tells them apart;
* ``unresolved`` -- no indexed repository is checked out at the path or has the URL.

Only ``linked`` draws an ``INCLUDES_REPOSITORY`` edge. A declaration settles an ambiguous or
unresolved entry and never overrides a path or remote the index proves.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from repoatlas.models.capability import CapabilityContext
from repoatlas.models.deployment_targets import DeclarationProvenance

#: The ``link_decisions`` family inclusion decisions are stored under.
INCLUSION_FAMILY = "repository_inclusion"

#: The most declarations kept: a person's list, not a crawl.
MAX_INCLUSION_DECLARATIONS = 2_000
#: The most unsettled inclusions one overview lists; a total says how many exist.
MAX_UNSETTLED_INCLUSIONS = 200
#: The most inclusions one submodule link of an overview lists; ``inclusions_total`` says how
#: many it holds.
MAX_LINK_INCLUSIONS = 20

InclusionMethod = Literal["path", "remote", "declared"]
InclusionDecision = Literal["linked", "ambiguous", "unresolved"]


class InclusionLink(BaseModel):
    """One inclusion a submodule link rests on: where the parent states it, and what it pins."""

    model_config = ConfigDict(frozen=True)

    #: ``gitmodules``, ``git-subrepo``, ``vcstool``, ``west`` or ``repo-manifest``.
    format: str
    #: Where the including repository's entry puts the included one.
    path: str
    #: ``repository/path:line`` of the entry.
    declared_at: str
    #: The URL the entry names, credentials removed; ``None`` when it names none.
    url: str | None = None
    #: The commit, tag or branch the including repository pins it at, when it pins one.
    revision: str | None = None
    #: The included repository's indexed HEAD.
    head: str | None = None
    #: Whether the included repository's HEAD is the pinned commit. ``None`` when either is
    #: unknown, or the pin is a branch, a tag, an abbreviation or of another hash length.
    in_sync: bool | None = None
    #: How the included repository was found.
    method: InclusionMethod


class UnsettledInclusion(BaseModel):
    """An inclusion no indexed repository was linked to, and why."""

    model_config = ConfigDict(frozen=True)

    #: The including repository's slug.
    repository: str
    #: The file stating it, repository-relative, and the entry's first line.
    file: str
    line: int | None = None
    path: str
    format: str
    url: str | None = None
    decision: Literal["ambiguous", "unresolved"]
    #: For an ambiguous inclusion, every indexed repository the URL names.
    candidates: tuple[str, ...] = ()
    #: A declaration that names a repository no longer indexed, when there is one.
    declared: str | None = None


class InclusionDeclarationFields(BaseModel):
    """What a user states: the entry ``path`` of ``repository``'s ``file`` is ``target``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The including repository's slug.
    repository: str = Field(min_length=1, max_length=1024)
    #: The file stating the entry, repository-relative.
    file: str = Field(min_length=1, max_length=1024)
    #: The entry's path, as the inclusion states it.
    path: str = Field(min_length=1, max_length=1024)
    #: The included repository's slug.
    target: str = Field(min_length=1, max_length=1024)


class InclusionDeclaration(InclusionDeclarationFields):
    """A stored declaration (``inclusion_declarations``)."""

    id: int
    created_at: datetime
    updated_at: datetime
    #: What the last link pass made of it: ``applied`` (it linked the entry), ``superseded``
    #: (the path or a remote proves a repository, and that one is linked), ``stale`` (the
    #: entry is no longer stated, or the target is not indexed). ``None`` before a pass read it.
    status: Literal["applied", "superseded", "stale"] | None = None


class InclusionDeclarationsRequest(BaseModel):
    """List every inclusion declaration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    context: CapabilityContext


class InclusionDeclarationsResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    context: CapabilityContext
    declarations: tuple[InclusionDeclaration, ...] = ()


class InclusionDeclarationCreateRequest(BaseModel):
    """State which indexed repository an unsettled inclusion is. The next sync applies it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    context: CapabilityContext
    declaration: InclusionDeclarationFields
    provenance: DeclarationProvenance | None = None


class InclusionDeclarationDeleteRequest(BaseModel):
    """Revoke a declaration. The history keeps what it stated."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    context: CapabilityContext
    id: int = Field(ge=1)
    provenance: DeclarationProvenance | None = None


class InclusionDeclarationResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    context: CapabilityContext
    declaration: InclusionDeclaration


class InclusionDeclarationDeleteResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    context: CapabilityContext
    id: int
    deleted: bool


class InclusionDeclarationError(ValueError):
    """A declaration that cannot be stored, in words the caller can act on."""


def imported_declarations(
    held: Mapping[tuple[str, str, str], str], stated: Sequence[InclusionDeclarationFields]
) -> list[InclusionDeclarationFields]:
    """The declarations of an import the index does not hold yet, in the file's order.

    ``held`` maps each declared entry ``(repository, file, path)`` to its target. An import never
    replaces a declaration: an entry stated again with the same target is skipped (already
    recorded, or stated twice), and one declared with another target -- in ``held`` or earlier in
    ``stated`` -- refuses the whole import, as does a repository declared to include itself or
    more declarations than are kept. Nothing is judged partially: either the list comes back or
    :class:`InclusionDeclarationError` is raised.
    """
    seen: dict[tuple[str, str, str], str] = {}
    new: list[InclusionDeclarationFields] = []
    for declaration in stated:
        entry = (declaration.repository, declaration.file, declaration.path)
        name = f"{declaration.repository}/{declaration.file}:{declaration.path}"
        if declaration.repository == declaration.target:
            raise InclusionDeclarationError(f"{name}: a repository cannot include itself")
        earlier = seen.get(entry)
        if earlier is None:
            earlier = held.get(entry)
        if earlier is None:
            seen[entry] = declaration.target
            new.append(declaration)
        elif earlier != declaration.target:
            raise InclusionDeclarationError(
                f"{name} is declared as {earlier} (in the index or in this file): "
                "revoke one of them first"
            )
    if len(held) + len(new) > MAX_INCLUSION_DECLARATIONS:
        raise InclusionDeclarationError(
            f"at most {MAX_INCLUSION_DECLARATIONS} inclusion declarations are kept"
        )
    return new


__all__ = [
    "INCLUSION_FAMILY",
    "MAX_INCLUSION_DECLARATIONS",
    "MAX_LINK_INCLUSIONS",
    "MAX_UNSETTLED_INCLUSIONS",
    "InclusionDecision",
    "InclusionDeclaration",
    "InclusionDeclarationCreateRequest",
    "InclusionDeclarationDeleteRequest",
    "InclusionDeclarationDeleteResponse",
    "InclusionDeclarationError",
    "InclusionDeclarationFields",
    "InclusionDeclarationResponse",
    "InclusionDeclarationsRequest",
    "InclusionDeclarationsResponse",
    "InclusionLink",
    "InclusionMethod",
    "UnsettledInclusion",
    "imported_declarations",
]
