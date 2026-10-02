"""Jinja parser and the Jinja neutralization used by Salt (V1_SPEC §7.5, §7.6).

**Templates are never rendered.** Rendering executes template logic, and template
logic in a Salt tree routinely reaches into pillars, grains and execution modules.
Everything here is a static scan of the template text.

This module also owns :func:`neutralize`, which replaces Jinja constructs with
same-length filler so the remaining text can be parsed as YAML. Preserving the
exact length of every replacement keeps line **and** column positions identical to
the original file, which is what lets Salt facts cite real line numbers.
"""

from __future__ import annotations

import re
from pathlib import Path

from repoatlas.models.parsing import (
    EdgeType,
    EntityType,
    ParsedFile,
    ParsedReference,
    ParsedSymbol,
)
from repoatlas.parsers.marks import whole_file_span

_SUFFIXES = frozenset({".jinja", ".j2", ".jinja2", ".tmpl"})

#: ``{{ expression }}``, ``{% statement %}`` and ``{# comment #}``.
_EXPRESSION = re.compile(r"\{\{.*?\}\}", re.DOTALL)
_STATEMENT = re.compile(r"\{%-?.*?-?%\}", re.DOTALL)
_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)

_TEMPLATE_STATEMENT = re.compile(
    r"\{%-?\s*(?P<keyword>include|extends|import|from)\s+(?P<quote>['\"])(?P<target>[^'\"]+)(?P=quote)",
)
#: ``pillar['a']['b']`` / ``grains['x']`` — bracket chains resolve to salt paths.
_BRACKET_CHAIN = re.compile(
    r"\b(?P<root>pillar|grains|opts)\s*(?P<chain>(?:\[\s*['\"][^'\"]+['\"]\s*\])+)"
)
_BRACKET_PART = re.compile(r"\[\s*['\"]([^'\"]+)['\"]\s*\]")
#: ``pillar.get('a:b')`` / ``grains.get('x')``.
_DOT_GET = re.compile(r"\b(?P<root>pillar|grains|opts)\.get\(\s*['\"](?P<path>[^'\"]+)['\"]")
#: ``salt['pillar.get']('a:b')`` and any other ``salt['module.function']`` call.
_SALT_CALL = re.compile(
    r"\bsalt\s*\[\s*['\"](?P<function>[a-zA-Z_][\w.]*)['\"]\s*\]"
    r"(?:\s*\(\s*['\"](?P<argument>[^'\"]*)['\"])?"
)

_ROOT_KIND = {
    "pillar": EntityType.CONFIG,
    "grains": EntityType.CONFIG,
    "opts": EntityType.CONFIG,
}


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _filler(matched: str, character: str) -> str:
    """Same-length replacement that preserves every newline."""
    return "".join("\n" if c == "\n" else character for c in matched)


def neutralize(content: str) -> str:
    """Replace Jinja with same-length filler so the rest can be parsed as YAML.

    Expressions become underscores because they usually stand where a YAML *value*
    belongs and must survive as a scalar; statements and comments become spaces
    because they stand where nothing belongs.
    """
    without_comments = _COMMENT.sub(lambda m: _filler(m.group(0), " "), content)
    without_statements = _STATEMENT.sub(lambda m: _filler(m.group(0), " "), without_comments)
    return _EXPRESSION.sub(lambda m: _filler(m.group(0), "_"), without_statements)


def extract_references(content: str, *, source_symbol: str | None = None) -> list[ParsedReference]:
    """Every relationship the template text states outright.

    Only literal targets are recorded. ``{% include some_var %}`` names nothing
    knowable without rendering, so it produces nothing.
    """
    references: list[ParsedReference] = []
    seen: set[tuple[str, str, int]] = set()

    def add(
        edge_type: EdgeType,
        target_kind: EntityType,
        target_key: str,
        index: int,
        meta: dict[str, str],
        confidence: float = 1.0,
    ) -> None:
        line = _line_of(content, index)
        signature = (str(edge_type), target_key, line)
        if signature in seen:
            return
        seen.add(signature)
        references.append(
            ParsedReference(
                edge_type=edge_type,
                target_kind=target_kind,
                target_key=target_key,
                line_start=line,
                line_end=line,
                confidence=confidence,
                source_symbol=source_symbol,
                meta=meta,
            )
        )

    for match in _TEMPLATE_STATEMENT.finditer(content):
        keyword = match.group("keyword")
        edge_type = EdgeType.INCLUDES if keyword in {"include", "extends"} else EdgeType.IMPORTS
        add(
            edge_type,
            EntityType.TEMPLATE,
            match.group("target"),
            match.start(),
            {"via": keyword},
        )

    for match in _BRACKET_CHAIN.finditer(content):
        root = match.group("root")
        parts = _BRACKET_PART.findall(match.group("chain"))
        add(
            EdgeType.READS,
            _ROOT_KIND[root],
            f"{root}:{':'.join(parts)}",
            match.start(),
            {"source": root, "syntax": "index"},
        )

    for match in _DOT_GET.finditer(content):
        root = match.group("root")
        add(
            EdgeType.READS,
            _ROOT_KIND[root],
            f"{root}:{match.group('path')}",
            match.start(),
            {"source": root, "syntax": "get"},
        )

    for match in _SALT_CALL.finditer(content):
        function = match.group("function")
        argument = match.group("argument")
        add(
            EdgeType.CALLS,
            EntityType.COMMAND,
            f"salt:{function}",
            match.start(),
            {"module": function.split(".", 1)[0]},
        )
        root = function.split(".", 1)[0]
        if root in _ROOT_KIND and function.endswith(".get") and argument:
            add(
                EdgeType.READS,
                _ROOT_KIND[root],
                f"{root}:{argument}",
                match.start(),
                {"source": root, "syntax": "salt"},
            )

    return references


class JinjaParser:
    """Deterministic Jinja template relationships."""

    name = "jinja-scan"
    version = "2"  # 2: whole-file spans stop at EOF, and are absent for an empty file
    language = "Jinja"

    def supports(self, path: Path) -> bool:
        return path.suffix in _SUFFIXES

    def parse(self, path: Path, content: str) -> ParsedFile:
        whole_file_start, whole_file_last = whole_file_span(content)
        template = ParsedSymbol(
            kind=EntityType.TEMPLATE,
            name=path.name,
            qualified_name=path.as_posix(),
            line_start=whole_file_start,
            line_end=whole_file_last,
        )
        return ParsedFile(
            language=self.language,
            parser=self.name,
            parser_version=self.version,
            symbols=(template,),
            references=tuple(extract_references(content, source_symbol=path.as_posix())),
        )
