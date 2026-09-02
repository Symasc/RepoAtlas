"""Parser dispatch (ARCHITECTURE.md §2).

The registry is the only thing the sync spine knows about parsing. Adding a
language means registering a parser here — no spine change.
"""

from __future__ import annotations

from pathlib import Path

from repoatlas.parsers.base import LanguageParser
from repoatlas.parsers.python import PythonParser


class ParserRegistry:
    """Maps a file to the parser that handles it."""

    def __init__(self, parsers: list[LanguageParser] | None = None) -> None:
        self._parsers: list[LanguageParser] = (
            list(parsers) if parsers is not None else default_parsers()
        )

    def register(self, parser: LanguageParser) -> None:
        self._parsers.append(parser)

    def for_path(self, path: Path) -> LanguageParser | None:
        """First parser claiming ``path``, or ``None`` when nothing handles it."""
        for parser in self._parsers:
            if parser.supports(path):
                return parser
        return None

    @property
    def version(self) -> str:
        """Aggregate parser version.

        A change here means stored facts are stale by definition, which forces a
        full rehash on the next sync (V1_SPEC §12). Registering a new parser also
        changes it, which is correct: files previously skipped now have a handler.
        """
        parts = sorted(f"{p.name}:{p.version}" for p in self._parsers)
        return "|".join(parts)

    @property
    def languages(self) -> list[str]:
        return sorted({p.language for p in self._parsers})


def default_parsers() -> list[LanguageParser]:
    """Parsers available in this build.

    Slice A ships Python only. Slice C adds TypeScript/JavaScript, Bash, Ruby, Salt,
    Jinja, structured config and Dockerfile by appending to this list.
    """
    return [PythonParser()]
