"""The dependency-parser contract (ARCHITECTURE.md §4).

Like a ``LanguageParser``, a ``DependencyParser`` is a pure function of
``(path, content)``: no I/O, no execution. ``setup.py`` is the case that makes
this rule load-bearing — it is inspected with ``ast`` and never imported
(V1_SPEC §13, §17).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from repoatlas.models.dependencies import DependencyKind, DependencyReport


@runtime_checkable
class DependencyParser(Protocol):
    """Extracts dependency facts from one manifest or lockfile."""

    #: Package ecosystem these facts belong to, e.g. ``pypi``.
    ecosystem: str

    #: Whether this file states constraints or resolved versions.
    kind: DependencyKind

    #: Stable identifier recorded as provenance on every fact produced.
    name: str

    #: Bumped when output shape changes; forces reparse (V1_SPEC §12).
    version: str

    def supports(self, path: Path) -> bool: ...

    def parse(self, path: Path, content: str) -> DependencyReport:
        """Extract dependencies. Must not raise: a failure becomes a diagnostic."""
        ...
