"""The parser contract (ARCHITECTURE.md §4, V1_SPEC §7).

A ``LanguageParser`` is a **pure function of (path, content)**. It performs no I/O,
touches neither storage nor the LLM, and never executes what it reads. It emits
*unresolved* references carrying raw target keys; the linker resolves them later.

This module defines the contract. Concrete parsers are separate modules registered
in ``registry.py``; adding one must require no change to the sync spine.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from repoatlas.models.parsing import ParsedFile


@runtime_checkable
class LanguageParser(Protocol):
    """Extracts deterministic structure from one file."""

    #: Stable identifier recorded as provenance on every fact this parser produces.
    name: str

    #: Bumped when output shape changes; a mismatch forces reparse (V1_SPEC §12).
    version: str

    #: Language label recorded on the file and reported by ``repos``.
    language: str

    def supports(self, path: Path) -> bool:
        """Whether this parser handles ``path``, by extension or filename."""
        ...

    def parse(self, path: Path, content: str) -> ParsedFile:
        """Extract structure. Must not raise: a failure becomes a diagnostic."""
        ...
