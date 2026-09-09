"""Ruby parser using tree-sitter (V1_SPEC §7.4).

Deliberately basic, per the specification: requires, modules, classes, methods,
gem references and calls that are reliable to read statically. Ruby's dynamism
makes anything beyond that a guess, and a guess is not evidence (V1_SPEC §2).

Ruby's own tooling (``ripper``) would mean running a Ruby interpreter beside
untrusted repository content, so tree-sitter is used instead.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from repoatlas.models.parsing import (
    EdgeType,
    EntityType,
    ParsedFile,
    ParseDiagnostic,
    ParsedReference,
    ParsedSymbol,
    Severity,
)
from repoatlas.parsers.marks import whole_file_span
from repoatlas.parsers.treesitter import (
    GrammarUnavailable,
    has_error,
    line_range,
    node_text,
    parse_tree,
    string_literal,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tree_sitter import Node

_SUFFIXES = frozenset({".rb", ".rake", ".gemspec", ".ru"})
_FILENAMES = frozenset({"Gemfile", "Rakefile", "Guardfile", "Capfile"})

_REQUIRE_METHODS = {"require": "package", "require_relative": "relative", "load": "relative"}
_ENV_FETCH_METHODS = frozenset({"fetch", "[]"})


class _Walker:
    def __init__(self, source: bytes) -> None:
        self._source = source
        self.symbols: list[ParsedSymbol] = []
        self.references: list[ParsedReference] = []
        self._scope: list[str] = []

    @property
    def _current(self) -> str | None:
        return "::".join(self._scope) if self._scope else None

    def _qualify(self, name: str) -> str:
        return f"{self._current}::{name}" if self._scope else name

    def run(self, root: Node) -> None:
        self._visit(root)

    def _visit(self, node: Node) -> None:
        kind = node.type
        if kind in {"module", "class"}:
            self._namespace(node)
            return
        if kind in {"method", "singleton_method"}:
            self._method(node)
            return
        if kind == "call":
            self._call(node)
        elif kind == "element_reference":
            self._element_reference(node)

        for child in node.children:
            self._visit(child)

    # -- definitions ---------------------------------------------------------

    def _namespace(self, node: Node) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            for child in node.children:
                self._visit(child)
            return
        name = node_text(self._source, name_node)
        qualified = self._qualify(name)
        start, end = line_range(node)
        self.symbols.append(
            ParsedSymbol(
                kind=EntityType.CLASS if node.type == "class" else EntityType.MODULE,
                name=name,
                qualified_name=qualified,
                line_start=start,
                line_end=end,
                signature=f"{node.type} {name}",
                parent=self._current,
            )
        )
        superclass = node.child_by_field_name("superclass")
        if superclass is not None:
            for child in superclass.children:
                if child.type not in {"constant", "scope_resolution"}:
                    continue
                base_start, base_end = line_range(child)
                self.references.append(
                    ParsedReference(
                        edge_type=EdgeType.REFERENCES,
                        target_kind=EntityType.CLASS,
                        target_key=node_text(self._source, child),
                        line_start=base_start,
                        line_end=base_end,
                        source_symbol=qualified,
                        meta={"role": "base"},
                    )
                )
        self._scope.append(name)
        for child in node.children:
            self._visit(child)
        self._scope.pop()

    def _method(self, node: Node) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            for child in node.children:
                self._visit(child)
            return
        name = node_text(self._source, name_node)
        singleton = node.type == "singleton_method"
        parameters = node.child_by_field_name("parameters")
        start, end = line_range(node)
        self.symbols.append(
            ParsedSymbol(
                kind=EntityType.METHOD,
                name=name,
                qualified_name=self._qualify(f"self.{name}" if singleton else name),
                line_start=start,
                line_end=end,
                signature=f"def {'self.' if singleton else ''}{name}"
                + (node_text(self._source, parameters) if parameters else ""),
                parent=self._current,
                meta={"singleton": "true"} if singleton else {},
            )
        )
        self._scope.append(name)
        for child in node.children:
            self._visit(child)
        self._scope.pop()

    # -- calls ---------------------------------------------------------------

    def _call(self, node: Node) -> None:
        method_node = node.child_by_field_name("method")
        if method_node is None:
            return
        method = node_text(self._source, method_node)
        receiver = node.child_by_field_name("receiver")
        arguments = node.child_by_field_name("arguments")
        start, end = line_range(node)

        if receiver is None and method in _REQUIRE_METHODS:
            self._require(method, arguments, start, end)
            return
        if receiver is None and method == "gem":
            self._gem(arguments, start, end)
            return
        if (
            receiver is not None
            and node_text(self._source, receiver) == "ENV"
            and method in _ENV_FETCH_METHODS
        ):
            self._env(arguments, start, end, via=f"ENV.{method}")
            return

        self.references.append(
            ParsedReference(
                edge_type=EdgeType.CALLS,
                target_kind=EntityType.SYMBOL,
                target_key=f"{node_text(self._source, receiver)}.{method}" if receiver else method,
                line_start=start,
                line_end=end,
                source_symbol=self._current,
            )
        )

    def _require(self, method: str, arguments: Node | None, start: int, end: int) -> None:
        target = self._first_string(arguments)
        if target is None:
            return
        self.references.append(
            ParsedReference(
                edge_type=EdgeType.IMPORTS,
                target_kind=EntityType.MODULE,
                target_key=target,
                line_start=start,
                line_end=end,
                source_symbol=self._current,
                meta={"specifier": _REQUIRE_METHODS[method], "via": method},
            )
        )

    def _gem(self, arguments: Node | None, start: int, end: int) -> None:
        """``gem "rails", "~> 7.0"`` names a dependency."""
        strings = self._strings(arguments)
        if not strings:
            return
        meta = {"constraint": strings[1]} if len(strings) > 1 else {}
        self.references.append(
            ParsedReference(
                edge_type=EdgeType.DEPENDS_ON,
                target_kind=EntityType.PACKAGE,
                target_key=strings[0],
                line_start=start,
                line_end=end,
                source_symbol=self._current,
                meta={"ecosystem": "gem", **meta},
            )
        )

    def _env(self, arguments: Node | None, start: int, end: int, *, via: str) -> None:
        name = self._first_string(arguments)
        if name is None:
            return
        self.references.append(
            ParsedReference(
                edge_type=EdgeType.READS,
                target_kind=EntityType.ENV_VAR,
                target_key=name,
                line_start=start,
                line_end=end,
                source_symbol=self._current,
                meta={"via": via},
            )
        )

    def _element_reference(self, node: Node) -> None:
        """``ENV["X"]`` reads an environment variable."""
        target = node.child_by_field_name("object") or (node.children[0] if node.children else None)
        if target is None or node_text(self._source, target) != "ENV":
            return
        name = next(
            (
                literal
                for child in node.children
                if child.type == "string" and (literal := string_literal(self._source, child))
            ),
            None,
        )
        if name is None:
            return
        start, end = line_range(node)
        self.references.append(
            ParsedReference(
                edge_type=EdgeType.READS,
                target_kind=EntityType.ENV_VAR,
                target_key=name,
                line_start=start,
                line_end=end,
                source_symbol=self._current,
                meta={"via": "ENV[]"},
            )
        )

    # -- helpers -------------------------------------------------------------

    def _strings(self, arguments: Node | None) -> list[str]:
        if arguments is None:
            return []
        found = []
        for child in arguments.children:
            if child.type != "string":
                continue
            literal = string_literal(self._source, child)
            if literal is not None:
                found.append(literal)
        return found

    def _first_string(self, arguments: Node | None) -> str | None:
        strings = self._strings(arguments)
        return strings[0] if strings else None


class RubyParser:
    """Deterministic Ruby structure extraction."""

    name = "tree-sitter-ruby"
    # 2: whole-file spans stop at EOF and are absent for an empty file; a
    #    classic-Mac (CR-only) file is no longer collapsed into one line.
    version = "2"
    language = "Ruby"

    def supports(self, path: Path) -> bool:
        return path.suffix in _SUFFIXES or path.name in _FILENAMES

    def parse(self, path: Path, content: str) -> ParsedFile:
        try:
            tree, source = parse_tree("ruby", content)
        except GrammarUnavailable as exc:
            return ParsedFile(
                language=self.language,
                parser=self.name,
                parser_version=self.version,
                diagnostics=(ParseDiagnostic(severity=Severity.ERROR, message=str(exc)),),
            )

        walker = _Walker(source)
        walker.run(tree.root_node)

        whole_file_start, whole_file_last = whole_file_span(content)
        module = ParsedSymbol(
            kind=EntityType.MODULE,
            name=path.stem,
            qualified_name=path.stem,
            line_start=whole_file_start,
            line_end=whole_file_last,
        )

        diagnostics = (
            (
                ParseDiagnostic(
                    severity=Severity.WARNING,
                    message="syntax errors recovered; results may be partial",
                ),
            )
            if has_error(tree)
            else ()
        )

        return ParsedFile(
            language=self.language,
            parser=self.name,
            parser_version=self.version,
            symbols=(module, *walker.symbols),
            references=tuple(walker.references),
            diagnostics=diagnostics,
        )
