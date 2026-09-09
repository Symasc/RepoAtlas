"""Dependency-parser dispatch (ARCHITECTURE.md §2, §6).

A file may be claimed by more than one parser here in principle, but in practice
each filename belongs to exactly one ecosystem, so dispatch returns every parser
that claims the path and the sync engine merges their reports.
"""

from __future__ import annotations

from pathlib import Path

from repoatlas.dependencies.base import DependencyParser
from repoatlas.dependencies.node import (
    PackageJsonParser,
    PackageLockParser,
    PnpmLockParser,
    YarnLockParser,
)
from repoatlas.dependencies.python import (
    PyProjectParser,
    RequirementsParser,
    SetupCfgParser,
    SetupPyParser,
    UvLockParser,
)
from repoatlas.dependencies.ruby import GemfileLockParser, GemfileParser, GemspecParser


class DependencyRegistry:
    """Maps a manifest or lockfile to the parsers that handle it."""

    def __init__(self, parsers: list[DependencyParser] | None = None) -> None:
        self._parsers: list[DependencyParser] = (
            list(parsers) if parsers is not None else default_dependency_parsers()
        )

    def for_path(self, path: Path) -> list[DependencyParser]:
        return [parser for parser in self._parsers if parser.supports(path)]

    def handles(self, path: Path) -> bool:
        return any(parser.supports(path) for parser in self._parsers)

    @property
    def version(self) -> str:
        """Aggregate version; a change forces reparse of dependency files."""
        return "|".join(sorted(f"{p.name}:{p.version}" for p in self._parsers))

    @property
    def ecosystems(self) -> list[str]:
        return sorted({p.ecosystem for p in self._parsers})


def default_dependency_parsers() -> list[DependencyParser]:
    return [
        PyProjectParser(),
        RequirementsParser(),
        SetupPyParser(),
        SetupCfgParser(),
        UvLockParser(),
        PackageJsonParser(),
        PackageLockParser(),
        PnpmLockParser(),
        YarnLockParser(),
        GemfileParser(),
        GemspecParser(),
        GemfileLockParser(),
    ]
