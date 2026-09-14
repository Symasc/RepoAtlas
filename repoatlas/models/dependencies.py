"""Dependency models (ARCHITECTURE.md §5, V1_SPEC §13).

Declared and resolved versions are stored **separately and both**. A manifest
states a constraint (``foo ^4.1``); a lockfile states what that constraint
actually resolved to (``foo 4.7.2``). Collapsing them would lose the only fact
that lets V2 retrieve documentation for the version a repository really runs
(ARCHITECTURE.md §14).
"""

from __future__ import annotations

import enum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from repoatlas.models.parsing import ParseDiagnostic

DependencyKind = Literal["manifest", "lockfile"]


class DeclaredDependency(BaseModel):
    """A dependency a manifest asks for, with the constraint it asks for."""

    model_config = ConfigDict(frozen=True)

    ecosystem: str
    name: str
    normalized_name: str
    constraint: str | None = None
    group: str = "main"
    manifest_path: str = ""
    line: int = 1


class ResolvedDependency(BaseModel):
    """A dependency a lockfile pins to an exact version."""

    model_config = ConfigDict(frozen=True)

    ecosystem: str
    name: str
    normalized_name: str
    version: str
    lockfile_path: str = ""
    line: int = 1


class ProvidedPackage(BaseModel):
    """The package a repository itself publishes.

    This is the other half of cross-repository matching (V1_SPEC §9): an edge from
    ``repo-a`` to ``repo-b`` exists only when ``repo-a`` declares a dependency whose
    normalized name equals a package ``repo-b`` provides, in the same ecosystem.
    """

    model_config = ConfigDict(frozen=True)

    ecosystem: str
    name: str
    normalized_name: str
    version: str | None = None
    manifest_path: str = ""
    line: int = 1


class DependencyReport(BaseModel):
    """Everything one manifest or lockfile states."""

    model_config = ConfigDict(frozen=True)

    ecosystem: str
    kind: DependencyKind
    declared: tuple[DeclaredDependency, ...] = ()
    resolved: tuple[ResolvedDependency, ...] = ()
    provides: tuple[ProvidedPackage, ...] = ()
    diagnostics: tuple[ParseDiagnostic, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.declared or self.resolved or self.provides)


class DependencyRecord(BaseModel):
    """A dependency fact as persisted, carrying its own provenance (V1_SPEC §11)."""

    model_config = ConfigDict(frozen=True)

    repo_id: int
    file_id: int
    ecosystem: str
    name: str
    normalized_name: str
    role: Literal["declared", "resolved", "provided"]
    constraint: str | None = None
    version: str | None = None
    group: str | None = None
    line_start: int = 1
    line_end: int = 1
    parser: str = ""


class DependencyRole(enum.StrEnum):
    """What a dependency observation asserts.

    ``DECLARED`` is a request (``left-pad ^1.0.0``), ``RESOLVED`` is what a lockfile
    pins (``left-pad 1.3.0``), ``PROVIDED`` is the package a manifest publishes.
    Kept distinct because they answer different questions and only one of them —
    ``RESOLVED`` — says what actually runs (V1_SPEC §13).
    """

    DECLARED = "declared"
    RESOLVED = "resolved"
    PROVIDED = "provided"


#: Reading order for a package's observations: what was asked for, what it
#: resolved to, what the repository publishes. Fixed so output never depends on
#: SQLite row order.
_ROLE_ORDER: dict[str, int] = {
    DependencyRole.DECLARED: 0,
    DependencyRole.RESOLVED: 1,
    DependencyRole.PROVIDED: 2,
}


class Agreement(enum.StrEnum):
    """How many *distinct* values a set of observations carries.

    Deliberately not called "conflict". V1 has no ecosystem-aware constraint
    intersection, so it cannot know whether ``^1.0`` and ``>=1.0,<2`` are
    incompatible — only that they are two different strings. ``DIVERGENT`` says
    exactly that and nothing more; claiming a version conflict would be inventing
    an analysis RepoAtlas does not perform (V1_SPEC §13, §2).
    """

    NONE = "none"
    SINGLE = "single"
    AGREEING = "agreeing"
    DIVERGENT = "divergent"


def _agreement(values: tuple[str | None, ...]) -> Agreement:
    if not values:
        return Agreement.NONE
    if len(values) == 1:
        return Agreement.SINGLE
    return Agreement.AGREEING if len(set(values)) == 1 else Agreement.DIVERGENT


class DependencyObservation(BaseModel):
    """One dependency fact stated by one file, with the provenance that proves it.

    The unit of truth in the dependency layer. Observations are never merged: a
    higher level may *summarize* them, but every summary is derived from the whole
    set and none of them is discarded to make one (ARCHITECTURE.md §5).
    """

    model_config = ConfigDict(frozen=True)

    repository: str
    repository_slug: str = ""
    ecosystem: str
    name: str
    normalized_name: str
    role: DependencyRole
    #: The constraint for a declaration, the pinned version for a resolution or a
    #: provision. One field, because which one it is is what ``role`` says.
    value: str | None = None
    group: str | None = None
    #: Directory within the repository that owns this file — see ``DependencyScope``.
    scope: str = "."
    source_path: str = ""
    line: int = 1
    #: The last line of the declaration. Equal to :attr:`line` for the one-line
    #: declarations every current parser produces, and carried separately because
    #: the stored row has always had both: a consumer that wants to quote the
    #: asserting text needs the whole span, and inferring the end from the start
    #: would be a guess the row does not require anyone to make.
    line_end: int = 1
    #: Which parser stated this. Provenance, like the path and the lines.
    parser: str = ""

    @property
    def order(self) -> tuple[str, int, str, int, str]:
        """Deterministic sort key. Never insertion order (V1_SPEC §13)."""
        return (self.scope, _ROLE_ORDER[self.role], self.source_path, self.line, self.value or "")


class DependencyScope(BaseModel):
    """The observations of one package within one dependency domain.

    A *scope* is the directory holding the file that stated the fact: ``.`` for a
    root ``package.json``, ``backend`` for ``backend/package.json``. It is the unit
    of manifest-to-lockfile pairing, and it is deterministic — derived from where
    the file sits, never from a runtime environment RepoAtlas cannot observe.

    One repository may hold several. A root ``package.json`` and a
    ``backend/package.json`` are two dependency domains that each state something
    true, and neither is the repository's answer (ARCHITECTURE.md §5).
    """

    model_config = ConfigDict(frozen=True)

    scope: str
    declarations: tuple[DependencyObservation, ...] = ()
    resolutions: tuple[DependencyObservation, ...] = ()
    provisions: tuple[DependencyObservation, ...] = ()

    @property
    def declaration_agreement(self) -> Agreement:
        return _agreement(tuple(o.value for o in self.declarations))

    @property
    def resolution_agreement(self) -> Agreement:
        return _agreement(tuple(o.value for o in self.resolutions))


class DependencyPackage(BaseModel):
    """Every observation of one package in one repository, grouped, never reduced.

    Identity is ``(repository, ecosystem, normalized_name)``. The ecosystem is part
    of it: npm ``foo-bar`` and PyPI ``foo_bar`` normalize to the same string and are
    not the same dependency (V1_SPEC §13).

    This replaced a view that grouped on the same key and then *assigned*
    ``constraint``, ``version``, ``manifest_path`` and ``lockfile_path`` in a loop
    over the rows — so a repository with a root and a backend manifest reported one
    constraint and one version, chosen by whichever row SQLite returned last, and
    the other declarations vanished without trace.
    """

    model_config = ConfigDict(frozen=True)

    repository: str
    repository_slug: str = ""
    ecosystem: str
    name: str
    normalized_name: str
    observations: tuple[DependencyObservation, ...] = ()
    scopes: tuple[DependencyScope, ...] = ()

    def _of(self, role: DependencyRole) -> tuple[DependencyObservation, ...]:
        return tuple(o for o in self.observations if o.role is role)

    @property
    def declarations(self) -> tuple[DependencyObservation, ...]:
        return self._of(DependencyRole.DECLARED)

    @property
    def resolutions(self) -> tuple[DependencyObservation, ...]:
        return self._of(DependencyRole.RESOLVED)

    @property
    def provisions(self) -> tuple[DependencyObservation, ...]:
        return self._of(DependencyRole.PROVIDED)

    @property
    def declared_constraints(self) -> tuple[str | None, ...]:
        """The distinct constraints asked for, in first-seen (deterministic) order."""
        return tuple(dict.fromkeys(o.value for o in self.declarations))

    @property
    def resolved_versions(self) -> tuple[str | None, ...]:
        """The distinct versions this package resolved to. **May be more than one.**"""
        return tuple(dict.fromkeys(o.value for o in self.resolutions))

    @property
    def declaration_agreement(self) -> Agreement:
        return _agreement(tuple(o.value for o in self.declarations))

    @property
    def resolution_agreement(self) -> Agreement:
        return _agreement(tuple(o.value for o in self.resolutions))

    @property
    def is_ambiguous(self) -> bool:
        """Whether no single answer exists for this package in this repository."""
        return Agreement.DIVERGENT in (self.declaration_agreement, self.resolution_agreement)
