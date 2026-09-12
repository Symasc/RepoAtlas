"""YAML and JSON parser (V1_SPEC §7.7).

Structured configuration is indexed as *keys with line provenance*, so
``locate "where is retries configured"`` can cite ``repo/config.yaml:14`` rather
than returning the whole document. Values are not indexed as entities: a value is
already searchable through the chunk text, and promoting every scalar to an
entity would bury real definitions.

Lockfiles are skipped deliberately. V1_SPEC §7.7 assigns them to the dependency
layer (§13), which represents them far more usefully than a few thousand
generated keys ever could.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

import yaml

from repoatlas.models.parsing import (
    EntityType,
    ParsedFile,
    ParseDiagnostic,
    ParsedSymbol,
    Severity,
)
from repoatlas.parsers.marks import diagnostic_line, node_span

_YAML_SUFFIXES = frozenset({".yaml", ".yml"})
_JSON_SUFFIXES = frozenset({".json"})

#: Skipped here; the dependency layer represents these (V1_SPEC §13).
_LOCKFILES = frozenset(
    {"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "poetry.lock", "uv.lock", "Gemfile.lock"}
)

#: Bounds on what one document may contribute, so a generated file cannot swamp
#: the entity table (V1_SPEC §20).
_MAX_DEPTH = 6
_MAX_KEYS = 400

_LEADING_TABS = re.compile(r"^\t+", re.MULTILINE)


def collect_config_keys(
    node: yaml.Node,
    prefix: str = "",
    *,
    lines: Sequence[str],
    max_depth: int = _MAX_DEPTH,
    max_keys: int = _MAX_KEYS,
) -> tuple[list[ParsedSymbol], bool]:
    """Mapping keys of a composed YAML node as CONFIG symbols with line ranges.

    ``lines`` is the source file split into lines; ranges are derived from node
    content rather than from end marks (see :mod:`repoatlas.parsers.marks`).

    Shared with the Salt parser, which uses it for the pillar-data half of an
    ``.sls`` tree (V1_SPEC §7.5, §7.7).
    """
    collector = _KeyCollector(lines=lines, max_depth=max_depth, max_keys=max_keys)
    collector.walk(node, prefix)
    return collector.symbols, collector.truncated


class _KeyCollector:
    def __init__(
        self,
        *,
        lines: Sequence[str],
        max_depth: int = _MAX_DEPTH,
        max_keys: int = _MAX_KEYS,
    ) -> None:
        self.symbols: list[ParsedSymbol] = []
        self.truncated = False
        self._lines = lines
        self._max_depth = max_depth
        self._max_keys = max_keys

    def walk(self, node: yaml.Node, prefix: str = "", depth: int = 0) -> None:
        if depth > self._max_depth:
            return
        if len(self.symbols) >= self._max_keys:
            self.truncated = True
            return

        if isinstance(node, yaml.MappingNode):
            for key_node, value_node in node.value:
                if not isinstance(key_node, yaml.ScalarNode):
                    continue
                name = key_node.value
                path = f"{prefix}.{name}" if prefix else name
                start, _ = node_span(key_node, self._lines)
                _, end = node_span(value_node, self._lines)
                self.symbols.append(
                    ParsedSymbol(
                        kind=EntityType.CONFIG,
                        name=name,
                        qualified_name=path,
                        line_start=start,
                        line_end=max(start, end),
                        signature=path,
                        parent=prefix or None,
                    )
                )
                if len(self.symbols) >= self._max_keys:
                    self.truncated = True
                    return
                self.walk(value_node, path, depth + 1)
        elif isinstance(node, yaml.SequenceNode):
            for index, item in enumerate(node.value):
                self.walk(item, f"{prefix}[{index}]", depth + 1)


class StructuredParser:
    """Deterministic YAML/JSON configuration keys with line provenance."""

    name = "structured-config"
    version = "1"
    language = "YAML"

    def supports(self, path: Path) -> bool:
        return path.suffix in _YAML_SUFFIXES | _JSON_SUFFIXES

    def parse(self, path: Path, content: str) -> ParsedFile:
        language = "JSON" if path.suffix in _JSON_SUFFIXES else "YAML"

        if path.name in _LOCKFILES:
            return ParsedFile(language=language, parser=self.name, parser_version=self.version)

        text = content
        if language == "JSON":
            # A leading tab is illegal YAML indentation but legal JSON whitespace.
            # Replacing only *leading* tabs is safe: a JSON string cannot span
            # lines, so no replaced tab can be inside one.
            text = _LEADING_TABS.sub(lambda m: "    " * len(m.group(0)), content)

        try:
            documents = list(yaml.compose_all(text, Loader=yaml.SafeLoader))
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            failed_at = diagnostic_line(mark, content)
            return ParsedFile(
                language=language,
                parser=self.name,
                parser_version=self.version,
                diagnostics=(
                    ParseDiagnostic(
                        severity=Severity.WARNING,
                        # PyYAML renders the offending source line into `str(exc)`
                        # and `exc.problem` is its own wording; the mark is read
                        # for a line number and nothing else.
                        message=f"{language} parse failed",
                        line=failed_at,
                    ),
                ),
            )

        # ``splitlines()`` deliberately: this array is indexed by PyYAML marks and
        # must use PyYAML's line-break set rather than the source-grammar splitter
        # in :mod:`repoatlas.parsers.marks`.
        collector = _KeyCollector(lines=content.splitlines())
        for document in documents:
            if document is not None:
                collector.walk(document)

        diagnostics = (
            (
                ParseDiagnostic(
                    severity=Severity.WARNING,
                    message=f"document truncated at {_MAX_KEYS} keys",
                ),
            )
            if collector.truncated
            else ()
        )

        return ParsedFile(
            language=language,
            parser=self.name,
            parser_version=self.version,
            symbols=tuple(collector.symbols),
            diagnostics=diagnostics,
        )
