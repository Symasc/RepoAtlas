"""Parser dispatch (ARCHITECTURE.md §2).

The registry is the only thing the sync spine knows about parsing. Adding a
language means registering a parser here — no spine change. Slice C proved that:
seven parsers were added and nothing above this module was touched to accept them.
"""

from __future__ import annotations

from pathlib import Path

from repoatlas.parsers.base import LanguageParser
from repoatlas.parsers.bash import BashParser
from repoatlas.parsers.deploy_helm import HelmHelpersParser, TiltfileParser
from repoatlas.parsers.dockerfile import DockerfileParser
from repoatlas.parsers.graphql import GraphQLParser
from repoatlas.parsers.jinja import JinjaParser
from repoatlas.parsers.prisma import PrismaParser
from repoatlas.parsers.python import PythonParser
from repoatlas.parsers.python_project import PythonProjectParser
from repoatlas.parsers.salt import SaltMasterParser, SaltParser
from repoatlas.parsers.setuptools_project import SetuptoolsProjectParser
from repoatlas.parsers.sql import SqlParser
from repoatlas.parsers.structured import StructuredParser
from repoatlas.parsers.typescript import TypeScriptParser


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
        reparse of the affected files on the next sync (V1_SPEC §12). Registering a
        new parser also changes it, which is correct: files previously skipped now
        have a handler.
        """
        parts = sorted(f"{p.name}:{p.version}" for p in self._parsers)
        return "|".join(parts)

    @property
    def languages(self) -> list[str]:
        return sorted({p.language for p in self._parsers})


def default_parsers() -> list[LanguageParser]:
    """Parsers available in this build, in dispatch order.

    Order matters exactly once: ``SaltParser`` must precede ``StructuredParser``
    so an ``.sls`` file is never treated as ordinary YAML (V1_SPEC §7.5).
    """
    return [
        PythonParser(),
        TypeScriptParser(),
        BashParser(),
        SaltMasterParser(),
        SaltParser(),
        JinjaParser(),
        DockerfileParser(),
        HelmHelpersParser(),
        TiltfileParser(),
        PythonProjectParser(),
        SetuptoolsProjectParser(),
        GraphQLParser(),
        PrismaParser(),
        SqlParser(),
        StructuredParser(),
    ]
