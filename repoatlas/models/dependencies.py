"""Dependency models (ARCHITECTURE.md §5, V1_SPEC §13).

Declared and resolved versions are stored **separately and both**. A manifest
states a constraint (``foo ^4.1``); a lockfile states what that constraint
actually resolved to (``foo 4.7.2``). Collapsing them would lose the only fact
that lets V2 retrieve documentation for the version a repository really runs
(ARCHITECTURE.md §14).
"""

from __future__ import annotations

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


class DependencyView(BaseModel):
    """A package joined across the manifest and the lockfile of one repository."""

    model_config = ConfigDict(frozen=True)

    repository: str
    ecosystem: str
    name: str
    normalized_name: str
    constraint: str | None = None
    version: str | None = None
    group: str | None = None
    manifest_path: str | None = None
    lockfile_path: str | None = None
    manifest_line: int | None = None
    lockfile_line: int | None = None
