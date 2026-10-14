"""TypeScript / JavaScript parser using tree-sitter (V1_SPEC §7.2).

The grammar is error-tolerant, so a file with a syntax error still yields the
structure around the error plus a diagnostic — never an aborted sync.

Module specifiers are emitted **verbatim**. Deciding that ``"@company/common"``
is another managed repository while ``"./config"`` is a sibling file is the
linker's job (ARCHITECTURE.md §5); the parser only records what is written.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

from repoatlas.models.parsing import (
    ENUM_MEMBER,
    FIELD,
    GRAMMAR_UNAVAILABLE,
    EdgeType,
    EntityType,
    ParsedFile,
    ParseDiagnostic,
    ParsedReference,
    ParsedSymbol,
    Severity,
)
from repoatlas.parsers.http_fastify import FastifyDeclarations
from repoatlas.parsers.http_nestjs import NestDeclarations
from repoatlas.parsers.http_oas3 import SpecRoutedApps
from repoatlas.parsers.http_ts_injection import Injections
from repoatlas.parsers.http_ts_wrappers import FetchWrappers
from repoatlas.parsers.http_typescript import TypeScriptHttp, endpoint_claims
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
from repoatlas.parsers.ts_graphql import TsGraphQL
from repoatlas.parsers.ts_persistence import TsPersistence
from repoatlas.parsers.ts_uses import (
    FIELDS_OMITTED,
    FIELDS_TRUNCATED,
    USE_NODES,
    UseAnalysis,
    UseSite,
    use_references,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tree_sitter import Node

_TS_SUFFIXES = frozenset({".ts", ".mts", ".cts"})
_TSX_SUFFIXES = frozenset({".tsx"})
_JS_SUFFIXES = frozenset({".js", ".mjs", ".cjs", ".jsx"})

_FUNCTION_VALUES = frozenset({"arrow_function", "function_expression", "function"})
#: A class field holding one of these is a function, not a model field (DF3b).
_FIELD_FUNCTION_VALUES = frozenset({*_FUNCTION_VALUES, "generator_function"})

#: Where a function's own body ends: what a nested function or class returns is
#: its own, never the enclosing function's.
_NESTED_SCOPES = frozenset(
    {
        *_FUNCTION_VALUES,
        "generator_function",
        "function_declaration",
        "generator_function_declaration",
        "method_definition",
        "class_declaration",
        "abstract_class_declaration",
        "class",
    }
)

#: Expressions that are their operand as far as its value is concerned.
_TRANSPARENT = frozenset({"parenthesized_expression", "as_expression", "satisfies_expression"})

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


_MEMBER_NAME = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")


def _relative(specifier: str) -> bool:
    return specifier.startswith((".", "/"))


class _Walker:
    """Recursive descent that keeps a scope stack for qualified names."""

    def __init__(
        self,
        source: bytes,
        http: TypeScriptHttp | None = None,
        nest: NestDeclarations | None = None,
        wrappers: FetchWrappers | None = None,
        uses: UseAnalysis | None = None,
        fastify: FastifyDeclarations | None = None,
        injections: Injections | None = None,
        oas3: SpecRoutedApps | None = None,
        graphql: TsGraphQL | None = None,
        persistence: TsPersistence | None = None,
    ) -> None:
        self._source = source
        self._graphql = graphql
        self._persistence = persistence
        self._http = http
        self._nest = nest
        self._wrappers = wrappers
        self._fastify = fastify
        self._injections = injections
        self._oas3 = oas3
        self.symbols: list[ParsedSymbol] = []
        #: Models' own fields (DF3b), kept apart: they are written after every other symbol.
        self.fields: list[ParsedSymbol] = []
        self.references: list[ParsedReference] = []
        self._scope: list[str] = []
        self._exported: set[str] = set()
        #: Object literals a named function returns, by span: their function-valued
        #: properties are that function's methods (see :meth:`_returned_object`).
        self._returned: set[tuple[int, int]] = set()
        self._uses = uses
        #: Each use of a module-scope name, with the declaration it belongs to.
        self.uses: list[tuple[str | None, UseSite]] = []
        #: The module-scope binding whose initializer is being visited, if any: it owns
        #: the uses there, though the calls there stay the file's (see :meth:`_use`).
        self._binding: str | None = None

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
        pending = [(node, exported)]
        while pending:
            current, current_exported = pending.pop()
            kind = current.type

            if kind in USE_NODES and self._uses is not None:
                site = self._uses.sites.get(
                    (current.start_byte, current.end_byte, kind)
                )
                if site is not None:
                    self._use(site)
            if kind == "import_statement":
                self._import_statement(current)
                continue
            if kind == "export_statement":
                self._export_statement(current)
                continue
            if kind in _DECLARATIONS:
                self._declaration(current, exported=current_exported)
                continue
            if kind == "method_definition":
                self._method(current)
                continue
            if kind in {"lexical_declaration", "variable_declaration"}:
                self._variable_declaration(current, exported=current_exported)
                continue
            if kind == "object" and (current.start_byte, current.end_byte) in self._returned:
                self._returned_object(current)
                continue
            if kind in {"call_expression", "new_expression"} and self._graphql is not None:
                self._graphql.visit_call(current, self._binding or self._current or "")
            if kind == "call_expression" and self._persistence is not None:
                self._persistence.visit_call(current, self._binding or self._current or "")
            if kind == "call_expression":
                self._call(current)
            elif kind in {"member_expression", "subscript_expression"}:
                self._maybe_env_read(current)

            for child in reversed(current.children):
                pending.append((child, current_exported))

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
                    **self._import_bindings(node),
                },
            )
        )

    def _import_bindings(self, node: Node) -> dict[str, str]:
        """How each name is bound: the default, a namespace, and ``imported:local`` pairs.

        ``names`` keeps the local names a file uses; resolution needs the names the
        imported module *exports*, which an alias (``B as C``) and a default import
        do not spell, so they are recorded apart.
        """
        clause = next((c for c in node.children if c.type == "import_clause"), None)
        if clause is None:
            return {}
        found: dict[str, str] = {}
        whole_type = any(c.type == "type" for c in node.children)
        for child in clause.children:
            if child.type == "identifier":
                found["default"] = node_text(self._source, child)
            elif child.type == "namespace_import":
                local = next((c for c in child.children if c.type == "identifier"), None)
                if local is not None:
                    found["namespace"] = node_text(self._source, local)
            elif child.type == "named_imports":
                pairs, typed = self._specifiers(child, "import_specifier")
                if pairs:
                    found["bindings"] = ",".join(pairs)
                if whole_type or typed:
                    found["type_only"] = "*" if whole_type else ",".join(typed)
        return found

    def _specifiers(self, clause: Node, kind: str) -> tuple[list[str], list[str]]:
        """``name`` or ``name:alias`` for each specifier, and the type-only names."""
        pairs: list[str] = []
        typed: list[str] = []
        for spec in clause.children:
            if spec.type != kind:
                continue
            name = spec.child_by_field_name("name")
            if name is None:
                continue
            imported = node_text(self._source, name)
            alias = spec.child_by_field_name("alias")
            local = node_text(self._source, alias) if alias is not None else imported
            pairs.append(imported if local == imported else f"{imported}:{local}")
            if any(c.type == "type" for c in spec.children):
                typed.append(imported)
        return pairs, typed

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
                            **self._reexport_bindings(node),
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

    def _reexport_bindings(self, node: Node) -> dict[str, str]:
        """What a re-export passes on: everything (``*``), a namespace, or named bindings.

        Named bindings are ``imported:exported`` pairs, so a renamed re-export
        (``export { Y as Z } from``) is followed under the name importers use.
        """
        clause = next((c for c in node.children if c.type == "export_clause"), None)
        if clause is not None:
            pairs, typed = self._specifiers(clause, "export_specifier")
            whole_type = any(c.type == "type" for c in node.children)
            found = {"reexport": "named", "bindings": ",".join(pairs)}
            if whole_type or typed:
                found["type_only"] = "*" if whole_type else ",".join(typed)
            return found
        namespace = next((c for c in node.children if c.type == "namespace_export"), None)
        if namespace is not None:
            local = next((c for c in namespace.children if c.type == "identifier"), None)
            if local is None:
                return {}
            return {"reexport": "namespace", "namespace": node_text(self._source, local)}
        if any(c.type == "*" for c in node.children):
            return {"reexport": "*"}
        return {}

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
        self.fields.extend(self._fields(node, self._qualify(name)))
        if node.type == "enum_declaration":
            self._enum_members(node, self._qualify(name))
        if entity_type is EntityType.FUNCTION:
            self._mark_returns(node)
        self._scope.append(name)
        self._visit_children(node)
        self._scope.pop()

    def _fields(self, node: Node, model: str) -> list[ParsedSymbol]:
        """A model's own fields, as ``Model.field`` (DF3b): each property signature with an
        identifier name directly in an interface body, or directly in the object type a type
        alias is; each class field with a type annotation and an identifier name that is not
        ``static`` and holds no function. A name written twice in one declaration is none.
        """
        members: list[Node] = []
        if node.type == "interface_declaration":
            body = node.child_by_field_name("body")
            members = [
                c for c in (body.named_children if body else []) if c.type == "property_signature"
            ]
        elif node.type == "type_alias_declaration":
            value = node.child_by_field_name("value")
            if value is not None and value.type == "object_type":
                members = [c for c in value.named_children if c.type == "property_signature"]
        elif node.type in {"class_declaration", "abstract_class_declaration"}:
            body = node.child_by_field_name("body")
            for child in body.named_children if body else []:
                if child.type != "public_field_definition":
                    continue
                if child.child_by_field_name("type") is None:
                    continue
                if any(k.type == "static" for k in child.children):
                    continue
                value = child.child_by_field_name("value")
                if value is not None and value.type in _FIELD_FUNCTION_VALUES:
                    continue
                members.append(child)
        named = [
            (node_text(self._source, key), member)
            for member in members
            if (key := member.child_by_field_name("name")) is not None
            and key.type == "property_identifier"
        ]
        counts = Counter(name for name, _ in named)
        fields: list[ParsedSymbol] = []
        for name, member in named:
            if counts[name] != 1:
                continue
            start, end = line_range(member)
            text = node_text(self._source, member).splitlines()[0].strip()
            fields.append(
                ParsedSymbol(
                    kind=EntityType.SYMBOL,
                    name=name,
                    qualified_name=f"{model}.{name}",
                    line_start=start,
                    line_end=end,
                    signature=text[:200],
                    parent=model,
                    meta={"kind": FIELD},
                )
            )
        return fields

    def _enum_members(self, node: Node, enum: str) -> None:
        """An enum's members whose value is a plain string literal, as ``Enum.Member``.

        The literal is recorded as the member's ``value``: what the member *is* at run
        time, which a generated enum shares with the schema it came from. A member with
        no initializer, a number, an escape or a template states no string, and is left
        out rather than recorded with a guessed one. A member is reached through its enum,
        never by its bare name (``ReferenceLinker._symbol_ids``).
        """
        body = node.child_by_field_name("body")
        for member in body.named_children if body is not None else ():
            key = member.child_by_field_name("name")
            value = member.child_by_field_name("value")
            if member.type != "enum_assignment" or key is None or value is None:
                continue
            literal = string_literal(self._source, value) if value.type == "string" else None
            if key.type == "property_identifier":
                name: str | None = node_text(self._source, key)
            else:
                name = string_literal(self._source, key) if key.type == "string" else None
            if (
                literal is None
                or "\\" in literal
                or name is None
                or not _MEMBER_NAME.fullmatch(name)
            ):
                continue
            start, end = line_range(member)
            self.symbols.append(
                ParsedSymbol(
                    kind=EntityType.SYMBOL,
                    name=name,
                    qualified_name=f"{enum}.{name}",
                    line_start=start,
                    line_end=end,
                    signature=node_text(self._source, member),
                    parent=enum,
                    meta={"kind": ENUM_MEMBER, "value": literal},
                )
            )

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
        self._mark_returns(node)
        self._scope.append(name)
        self._visit_children(node)
        self._scope.pop()

    def _returned_object(self, node: Node) -> None:
        """The object a named function returns: its function properties are methods.

        ``return { createCourse: async (auth, input) => request(…) }`` makes
        ``createCourse`` a method of the enclosing factory, exactly as
        ``return { async createCourse(auth, input) { … } }`` already does. Only a
        property with a static key counts; a computed key names nothing this can
        read, and a key written twice in one object is not one definition. A
        property whose value is itself an object keeps its own name as a scope,
        so ``factory.courses.list`` stays distinguishable from ``factory.list``.
        Everything else is visited exactly as it was, so no other fact changes.
        """
        keys = Counter(
            key for child in node.named_children if (key := self._property_key(child)) is not None
        )
        for child in node.children:
            name = self._property_key(child)
            value = child.child_by_field_name("value") if child.type == "pair" else None
            target = _operand(value) if value is not None else None
            if name is None or keys[name] != 1 or target is None:
                self._visit(child)
            elif target.type in _FUNCTION_VALUES:
                self._property_method(child, name, target)
            elif target.type == "object":
                self._scope.append(name)
                self._returned_object(target)
                self._scope.pop()
            else:
                self._visit(child)

    def _property_key(self, node: Node) -> str | None:
        """The static key of a ``pair``, or ``None``."""
        if node.type != "pair":
            return None
        key = node.child_by_field_name("key")
        if key is None:
            return None
        if key.type == "property_identifier":
            return node_text(self._source, key)
        if key.type == "string":
            literal = string_literal(self._source, key)
            return literal if literal and "\\" not in literal else None
        return None

    def _property_method(self, pair: Node, name: str, function: Node) -> None:
        start, end = line_range(pair)
        self.symbols.append(
            ParsedSymbol(
                kind=EntityType.METHOD,
                name=name,
                qualified_name=self._qualify(name),
                line_start=start,
                line_end=end,
                signature=self._signature(function, "method", name),
                parent=self._current,
                meta={"kind": "property"},
            )
        )
        self._mark_returns(function)
        self._scope.append(name)
        self._visit_children(function)
        self._scope.pop()

    def _mark_returns(self, function: Node) -> None:
        """Record the object literals ``function`` itself returns."""
        body = function.child_by_field_name("body")
        if body is None:
            return
        if body.type != "statement_block":
            # `() => ({ … })`: the expression body is the returned value.
            self._mark_object(body)
            return
        pending = [body]
        while pending:
            node = pending.pop()
            for child in node.children:
                if child.type in _NESTED_SCOPES:
                    continue
                if child.type == "return_statement":
                    value = next((c for c in child.named_children if c.type != "comment"), None)
                    if value is not None:
                        self._mark_object(value)
                pending.append(child)

    def _mark_object(self, node: Node) -> None:
        target = _operand(node)
        if target.type == "object":
            self._returned.add((target.start_byte, target.end_byte))

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
                assert value is not None
                self._mark_returns(value)
                self._scope.append(name)
                self._visit_children(declarator)
                self._scope.pop()
            elif top_level and _module_scope(node):
                previous, self._binding = self._binding, name
                self._visit_children(declarator)
                self._binding = previous
            else:
                self._visit_children(declarator)

    def _use(self, site: UseSite) -> None:
        """Attribute one use of a module-scope name to the declaration it sits in.

        The innermost enclosing named declaration, as for a call; failing that, the
        module-scope binding whose initializer holds it; failing that, the file.

        A callee this file declares stays a call only when the call graph gives it to
        the same declaration, which it resolves exactly within the file. A callee bound
        by an import is always recorded as a use as well: the call graph follows an
        imported name only as far as its own trace goes, which stops at a default or
        a renamed re-export, while this use is followed under every name it passes by
        (repoatlas.graph.uses).
        """
        source = self._current if self._scope else self._binding
        if site.use == "call" and site.binding.kind == "local" and source == self._current:
            return
        self.uses.append((source, site))

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
        if self._wrappers is not None:
            self.references.extend(self._wrappers.call_references(node, self._current))
        if self._fastify is not None:
            self.references.extend(self._fastify.call_references(node, self._current))
        if self._injections is not None:
            self.references.extend(self._injections.call_references(node, self._current))
        if self._oas3 is not None:
            self.references.extend(self._oas3.call_references(node, self._current))
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
        # As a use is attributed (:meth:`_use`): the enclosing named declaration, else the
        # module-scope binding whose initializer holds it (DF3a), else the file.
        self.references.append(
            ParsedReference(
                edge_type=EdgeType.READS,
                target_kind=EntityType.ENV_VAR,
                target_key=name,
                line_start=start,
                line_end=end,
                source_symbol=self._current if self._scope else self._binding,
                meta={"via": "process.env"},
            )
        )

    def _signature(self, node: Node, label: str, name: str) -> str:
        parameters = node.child_by_field_name("parameters")
        if parameters is not None:
            arguments = node_text(self._source, parameters)
        else:
            # `x => …`: one bare parameter, which the grammar does not parenthesize.
            single = node.child_by_field_name("parameter")
            arguments = f"({node_text(self._source, single)})" if single is not None else ""
        return f"{label} {name}{arguments}".strip()


def _module_scope(declaration: Node) -> bool:
    """Whether a variable declaration sits at the top of the module, exported or not."""
    parent = declaration.parent
    while parent is not None and parent.type in {"export_statement", "ambient_declaration"}:
        parent = parent.parent
    return parent is not None and parent.type == "program"


def _operand(node: Node) -> Node:
    """``node`` with parentheses, ``as`` and ``satisfies`` looked through."""
    while node.type in _TRANSPARENT:
        inner = [child for child in node.named_children if child.type != "comment"]
        if not inner:
            break
        node = inner[0]
    return node


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
    # 5: a function-valued property of an object a named function returns is a
    #    method of that function, and owns the calls made inside it.
    # 6: requests made through a request wrapper this file proves — a helper
    #    forwarding a path to a fetch-like call — become HTTP facts.
    # 7: exported request helpers are declared, and calls through helpers imported
    #    from relative modules are recorded for the global pass to decide.
    # 8: a request whose path one file cannot decide — a rooted template, a choice
    #    of rooted paths, a named value, or a route with an unread method — is
    #    recorded as a pending claim, and template arguments are recorded as parts
    #    rather than text, so the global pass can match them against served routes.
    # 9: imports record how each name is bound (default, namespace, and
    #    ``imported:local`` pairs), and re-exports what they pass on (``*``, a
    #    namespace, or ``imported:exported`` pairs), so the global pass can follow a
    #    named import through re-exporting barrels to the file that defines it.
    # 10: declarations' uses of module-scope names (USES), and the file's export
    #    table for following them through barrels (repoatlas.parsers.ts_uses).
    # 11: Fastify applications, plugins, routes and registrations, and the file's
    #    exported string constants (repoatlas.parsers.http_fastify).
    # 12: superagent requests, and an http-client-call claim for every request whose
    #    origin the source states, for the cross-repository join.
    # 13: transports and bases handed on in options objects: injected requests, the
    #    calls that supply or forward them, and the functions that own them
    #    (repoatlas.parsers.http_ts_injection).
    # 14: oas3-tools applications, and the module-level objects of functions their
    #    controllers may be made of (repoatlas.parsers.http_oas3).
    # 15: a superagent or fetch request, or an injected one, whose path is read but whose
    #    base is not (unstated, empty or computed) records a claim with no origin (D2).
    # 16: an enum's members with a plain string value are symbols (``Enum.Member``) that
    #    record the value, which is what a generated enum shares with its schema.
    # 17: GraphQL documents in ``gql``/``graphql`` templates, schema construction and the
    #    shapes of exported resolver-like objects (repoatlas.parsers.ts_graphql).
    # 18: database clients, Knex chains and SQL handed to a client, and typed value
    #    mappings (repoatlas.parsers.ts_persistence).
    # 19: an environment read in a module-scope binding's initializer is that binding's, as a
    #    use there is; one in a binding nested in a callback, or anywhere else at module
    #    scope, stays the file's (DF3a).
    # 20: a model's own fields are symbols (``Model.field``, kind ``field``), and a use of a type
    #    carries the fields read through a parameter it annotates or set in an object literal
    #    it types (DF3b, repoatlas.parsers.ts_uses).
    version = "20"
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
        wrappers = FetchWrappers(source, tree.root_node, http.counts)
        fastify = FastifyDeclarations(source, tree.root_node, http.counts)
        injections = Injections(source, tree.root_node, http.counts, wrappers)
        oas3 = SpecRoutedApps(source, tree.root_node, http.counts, wrappers)
        # Uses are proven by scoping, and a tree the grammar had to recover may have
        # lost a declaration: such a file states no uses at all.
        uses = None if has_error(tree) else UseAnalysis(source, tree.root_node)
        graphql = TsGraphQL(source, tree.root_node)
        persistence = TsPersistence(source, tree.root_node)
        walker = _Walker(
            source, http, nest, wrappers, uses, fastify, injections, oas3, graphql, persistence
        )
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
        used = use_references(walker.uses, walker.symbols, uses.exports) if uses is not None else []
        if any(FIELDS_OMITTED in use.meta for use in used):
            # A use named more model-field members than its claim records (DF3b).
            diagnostics = (
                *diagnostics,
                ParseDiagnostic(severity=Severity.WARNING, message=FIELDS_TRUNCATED),
            )

        return ParsedFile(
            language=language,
            parser=self.name,
            parser_version=self.version,
            symbols=(module, *walker.symbols, *graphql.symbols, *walker.fields),
            references=(
                *walker.references,
                *endpoint_claims(walker.references),
                *nest.references(),
                *fastify.references(),
                *injections.references(),
                *oas3.references(),
                *graphql.references(),
                *persistence.references(),
                *wrappers.declarations(),
                *used,
            ),
            diagnostics=diagnostics,
        )
