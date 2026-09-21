"""TypeScript / JavaScript parser using tree-sitter (V1_SPEC §7.2).

The grammar is error-tolerant, so a file with a syntax error still yields the
structure around the error plus a diagnostic — never an aborted sync.

Module specifiers are emitted **verbatim**. Deciding that ``"@company/common"``
is another managed repository while ``"./config"`` is a sibling file is the
linker's job (ARCHITECTURE.md §5); the parser only records what is written.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from repoatlas.models.parsing import (
    GRAMMAR_UNAVAILABLE,
    EdgeType,
    EntityType,
    ParsedFile,
    ParseDiagnostic,
    ParsedReference,
    ParsedSymbol,
    Severity,
)
from repoatlas.parsers.http_nestjs import NestDeclarations
from repoatlas.parsers.http_typescript import TypeScriptHttp
from repoatlas.parsers.marks import whole_file_span
from repoatlas.parsers.treesitter import (
    GrammarUnavailable,
    has_error,
    line_range,
    node_text,
    parse_tree,
    string_literal,
    walk,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tree_sitter import Node

_TS_SUFFIXES = frozenset({".ts", ".mts", ".cts"})
_TSX_SUFFIXES = frozenset({".tsx"})
_JS_SUFFIXES = frozenset({".js", ".mjs", ".cjs", ".jsx"})

_FUNCTION_VALUES = frozenset({"arrow_function", "function_expression", "function"})

#: Declaration nodes that name a definition, mapped to the entity they become.
_DECLARATIONS: dict[str, tuple[EntityType, str]] = {
    "function_declaration": (EntityType.FUNCTION, "function"),
    "generator_function_declaration": (EntityType.FUNCTION, "generator"),
    "function_signature": (EntityType.FUNCTION, "declaration"),
    "class_declaration": (EntityType.CLASS, "class"),
    "abstract_class_declaration": (EntityType.CLASS, "abstract class"),
    "interface_declaration": (EntityType.SYMBOL, "interface"),
    "type_alias_declaration": (EntityType.SYMBOL, "type"),
    "enum_declaration": (EntityType.SYMBOL, "enum"),
    "module": (EntityType.MODULE, "namespace"),
}


def _relative(specifier: str) -> bool:
    return specifier.startswith((".", "/"))


class _Walker:
    """Recursive descent that keeps a scope stack for qualified names."""

    def __init__(
        self,
        source: bytes,
        language: str,
        http: TypeScriptHttp | None = None,
        nest: NestDeclarations | None = None,
    ) -> None:
        self._source = source
        self._language = language
        self._http = http
        self._nest = nest
        self.symbols: list[ParsedSymbol] = []
        self.references: list[ParsedReference] = []
        self._scope: list[str] = []
        self._exported: set[str] = set()

    # -- scope ---------------------------------------------------------------

    @property
    def _current(self) -> str | None:
        return ".".join(self._scope) if self._scope else None

    def _qualify(self, name: str) -> str:
        return f"{self._current}.{name}" if self._scope else name

    # -- entry point ---------------------------------------------------------

    def run(self, root: Node) -> None:
        self._visit_children(root)

    def _visit_children(self, node: Node, *, exported: bool = False) -> None:
        for child in node.children:
            self._visit(child, exported=exported)

    def _visit(self, node: Node, *, exported: bool = False) -> None:
        kind = node.type

        if kind == "import_statement":
            self._import_statement(node)
            return
        if kind == "export_statement":
            self._export_statement(node)
            return
        if kind in _DECLARATIONS:
            self._declaration(node, exported=exported)
            return
        if kind == "method_definition":
            self._method(node)
            return
        if kind in {"lexical_declaration", "variable_declaration"}:
            self._variable_declaration(node, exported=exported)
            return
        if kind == "call_expression":
            self._call(node)
        elif kind in {"member_expression", "subscript_expression"}:
            self._maybe_env_read(node)

        self._visit_children(node, exported=exported)

    # -- imports and exports -------------------------------------------------

    def _import_statement(self, node: Node) -> None:
        source_node = node.child_by_field_name("source")
        if source_node is None:
            return
        specifier = string_literal(self._source, source_node)
        if specifier is None:
            return
        start, end = line_range(node)
        names = self._imported_names(node)
        self.references.append(
            ParsedReference(
                edge_type=EdgeType.IMPORTS,
                target_kind=EntityType.MODULE,
                target_key=specifier,
                line_start=start,
                line_end=end,
                source_symbol=self._current,
                meta={
                    "specifier": "relative" if _relative(specifier) else "package",
                    **({"names": names} if names else {}),
                },
            )
        )

    def _imported_names(self, node: Node) -> str:
        clause = node.child_by_field_name("import_clause") or next(
            (c for c in node.children if c.type == "import_clause"), None
        )
        if clause is None:
            return ""
        found: list[str] = []
        for descendant in clause.children:
            if descendant.type == "identifier":
                found.append(node_text(self._source, descendant))
            elif descendant.type in {"named_imports", "namespace_import"}:
                found.extend(
                    node_text(self._source, spec)
                    for spec in descendant.children
                    if spec.type in {"import_specifier", "identifier"}
                )
        return ",".join(found)

    def _export_statement(self, node: Node) -> None:
        start, end = line_range(node)

        source_node = node.child_by_field_name("source")
        if source_node is not None:
            specifier = string_literal(self._source, source_node)
            if specifier:
                # A re-export both imports from and exports the named module.
                self.references.append(
                    ParsedReference(
                        edge_type=EdgeType.IMPORTS,
                        target_kind=EntityType.MODULE,
                        target_key=specifier,
                        line_start=start,
                        line_end=end,
                        source_symbol=self._current,
                        meta={
                            "specifier": "relative" if _relative(specifier) else "package",
                            "role": "re-export",
                        },
                    )
                )

        for name in self._exported_names(node):
            self._exported.add(name)
            self.references.append(
                ParsedReference(
                    edge_type=EdgeType.EXPORTS,
                    target_kind=EntityType.SYMBOL,
                    target_key=name,
                    line_start=start,
                    line_end=end,
                    source_symbol=self._current,
                )
            )

        self._visit_children(node, exported=True)

    def _exported_names(self, node: Node) -> list[str]:
        found: list[str] = []
        for child in node.children:
            if child.type == "export_clause":
                found.extend(
                    node_text(self._source, spec.children[0])
                    for spec in child.children
                    if spec.type == "export_specifier" and spec.children
                )
            elif child.type in _DECLARATIONS or child.type in {
                "lexical_declaration",
                "variable_declaration",
            }:
                found.extend(self._declared_names(child))
            elif child.type == "identifier":  # export default foo
                found.append(node_text(self._source, child))
        return found

    def _declared_names(self, node: Node) -> list[str]:
        if node.type in _DECLARATIONS:
            name_node = node.child_by_field_name("name")
            return [node_text(self._source, name_node)] if name_node else []
        return [
            node_text(self._source, name)
            for declarator in node.children
            if declarator.type == "variable_declarator"
            and (name := declarator.child_by_field_name("name")) is not None
            and name.type == "identifier"
        ]

    # -- definitions ---------------------------------------------------------

    def _declaration(self, node: Node, *, exported: bool) -> None:
        entity_type, label = _DECLARATIONS[node.type]
        name_node = node.child_by_field_name("name")
        if name_node is None:
            self._visit_children(node)
            return
        name = node_text(self._source, name_node)
        start, end = line_range(node)
        meta = {"kind": label}
        if exported or name in self._exported:
            meta["exported"] = "true"
        self.symbols.append(
            ParsedSymbol(
                kind=entity_type,
                name=name,
                qualified_name=self._qualify(name),
                line_start=start,
                line_end=end,
                signature=self._signature(node, label, name),
                parent=self._current,
                meta=meta,
            )
        )
        self._record_heritage(node, self._qualify(name))
        self._scope.append(name)
        self._visit_children(node)
        self._scope.pop()

    def _record_heritage(self, node: Node, qualified: str) -> None:
        """``extends`` / ``implements`` clauses become REFERENCES."""
        for child in node.children:
            if child.type not in {"class_heritage", "extends_type_clause", "implements_clause"}:
                continue
            for name_node in walk(child):
                if name_node.type not in {"identifier", "type_identifier"}:
                    continue
                start, end = line_range(name_node)
                self.references.append(
                    ParsedReference(
                        edge_type=EdgeType.REFERENCES,
                        target_kind=EntityType.CLASS,
                        target_key=node_text(self._source, name_node),
                        line_start=start,
                        line_end=end,
                        source_symbol=qualified,
                        meta={"role": "base"},
                    )
                )

    def _method(self, node: Node) -> None:
        name_node = node.child_by_field_name("name")
        if name_node is None:
            self._visit_children(node)
            return
        name = node_text(self._source, name_node)
        start, end = line_range(node)
        self.symbols.append(
            ParsedSymbol(
                kind=EntityType.METHOD,
                name=name,
                qualified_name=self._qualify(name),
                line_start=start,
                line_end=end,
                signature=self._signature(node, "method", name),
                parent=self._current,
            )
        )
        self._scope.append(name)
        self._visit_children(node)
        self._scope.pop()

    def _variable_declaration(self, node: Node, *, exported: bool) -> None:
        """Top-level ``const``/``let``/``var``; function values become functions."""
        top_level = not self._scope
        for declarator in node.children:
            if declarator.type != "variable_declarator":
                continue
            name_node = declarator.child_by_field_name("name")
            value = declarator.child_by_field_name("value")
            is_function = value is not None and value.type in _FUNCTION_VALUES
            if name_node is None or name_node.type != "identifier":
                self._visit_children(declarator)
                continue
            name = node_text(self._source, name_node)
            if is_function or top_level or exported:
                start, end = line_range(declarator)
                meta = {"kind": "function" if is_function else "binding"}
                if exported or name in self._exported:
                    meta["exported"] = "true"
                self.symbols.append(
                    ParsedSymbol(
                        kind=EntityType.FUNCTION if is_function else EntityType.SYMBOL,
                        name=name,
                        qualified_name=self._qualify(name),
                        line_start=start,
                        line_end=end,
                        signature=f"{'function' if is_function else 'const'} {name}",
                        parent=self._current,
                        meta=meta,
                    )
                )
            if is_function:
                self._scope.append(name)
                self._visit_children(declarator)
                self._scope.pop()
            else:
                self._visit_children(declarator)

    # -- calls, requires and environment variables ---------------------------

    def _call(self, node: Node) -> None:
        callee = node.child_by_field_name("function")
        if callee is None:
            return
        name = node_text(self._source, callee)
        start, end = line_range(node)

        if name in {"require", "import"} or callee.type == "import":
            self._module_call(node, start, end)
            return

        if self._http is not None:
            self.references.extend(self._http.call_references(node, self._current))
        if self._nest is not None:
            self.references.extend(self._nest.call_references(node, self._current))
        self.references.append(
            ParsedReference(
                edge_type=EdgeType.CALLS,
                target_kind=EntityType.SYMBOL,
                target_key=name,
                line_start=start,
                line_end=end,
                source_symbol=self._current,
            )
        )

    def _module_call(self, node: Node, start: int, end: int) -> None:
        """``require("x")`` and dynamic ``import("x")`` are imports."""
        arguments = node.child_by_field_name("arguments")
        if arguments is None:
            return
        for argument in arguments.children:
            if argument.type != "string":
                continue
            specifier = string_literal(self._source, argument)
            if specifier is None:
                return
            self.references.append(
                ParsedReference(
                    edge_type=EdgeType.IMPORTS,
                    target_kind=EntityType.MODULE,
                    target_key=specifier,
                    line_start=start,
                    line_end=end,
                    source_symbol=self._current,
                    meta={
                        "specifier": "relative" if _relative(specifier) else "package",
                        "role": "dynamic",
                    },
                )
            )
            return

    def _maybe_env_read(self, node: Node) -> None:
        """``process.env.X`` and ``process.env["X"]`` are environment reads."""
        target = node.child_by_field_name("object")
        if target is None or node_text(self._source, target) != "process.env":
            return

        if node.type == "member_expression":
            property_node = node.child_by_field_name("property")
            name = node_text(self._source, property_node) if property_node else None
        else:
            index = node.child_by_field_name("index")
            name = string_literal(self._source, index) if index else None

        if not name:
            # A computed key cannot be resolved statically, so nothing is recorded.
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
                meta={"via": "process.env"},
            )
        )

    def _signature(self, node: Node, label: str, name: str) -> str:
        parameters = node.child_by_field_name("parameters")
        arguments = node_text(self._source, parameters) if parameters else ""
        return f"{label} {name}{arguments}".strip()


class TypeScriptParser:
    """Deterministic TypeScript/JavaScript structure extraction."""

    name = "tree-sitter-typescript"
    # 2: whole-file spans stop at EOF and are absent for an empty file; a
    #    classic-Mac (CR-only) file is no longer collapsed into one line.
    # 3: routes a proven Express application serves, and requests `fetch` and
    #    `axios` provably make, become HTTP facts.
    # 4: NestJS controller routes, modules, application roots and routing markers
    #    are recorded as pending claims; the global pass decides which route a
    #    proven application serves.
    version = "4"
    language = "TypeScript"

    def supports(self, path: Path) -> bool:
        return path.suffix in _TS_SUFFIXES | _TSX_SUFFIXES | _JS_SUFFIXES

    def _grammar(self, path: Path) -> tuple[str, str]:
        suffix = path.suffix
        if suffix in _TSX_SUFFIXES:
            return "tsx", "TypeScript"
        if suffix in _JS_SUFFIXES:
            return "javascript", "JavaScript"
        return "typescript", "TypeScript"

    def parse(self, path: Path, content: str) -> ParsedFile:
        grammar, language = self._grammar(path)
        try:
            tree, source = parse_tree(grammar, content)
        except GrammarUnavailable:
            return ParsedFile(
                language=language,
                parser=self.name,
                parser_version=self.version,
                diagnostics=(
                    ParseDiagnostic(severity=Severity.ERROR, message=GRAMMAR_UNAVAILABLE),
                ),
            )

        http = TypeScriptHttp(source, tree.root_node)
        nest = NestDeclarations(source, tree.root_node, http.counts)
        walker = _Walker(source, grammar, http, nest)
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
            language=language,
            parser=self.name,
            parser_version=self.version,
            symbols=(module, *walker.symbols),
            references=(*walker.references, *nest.references()),
            diagnostics=diagnostics,
        )
