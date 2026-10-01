"""Express applications that route an OpenAPI document by operationId (HTTP linkage).

``oas3-tools`` (2.2.3, read from its source) builds an Express application from a
specification and a controllers object:

    const controllers = { ...generatedController, ...statusController };
    const app = oas3Tools.expressAppConfig(
      path.join(__dirname, '../api/openapi.yaml'), { routing: { controllers } },
    ).getApp();

``express-openapi-validator`` serves every path of the document under each root
server's base path, and the router calls the controllers object's member named by the
operation's ``operationId`` -- its dashes camel-cased, and prefixed with
``<x-swagger-router-controller>_`` when the operation names one. No file proves those
routes alone, so each records its part as a claim (:mod:`repoatlas.graph.ts_oas3`):

* :data:`~repoatlas.parsers.http_facts.OAS3_APP` -- ``expressAppConfig(spec, options)``
  on ``oas3-tools`` (a namespace, default or ``require`` binding of it, or the named
  import), bound once at module level. ``spec`` is read only as
  ``path.join(__dirname, '<literal>', …)`` (or ``resolve``) on the ``path`` module,
  relative to the file that states it: the library opens a bare relative path from the
  process's working directory, which the source does not state.
  ``options.routing.controllers`` is read only as a handler object, or an object literal
  of spreads of handler objects, each declared in the file or imported by name from a
  relative module (later spreads win, as in JavaScript). A directory of controllers, or
  anything else, is recorded as unread;
* :data:`~repoatlas.parsers.http_facts.TS_HANDLER_OBJECT` -- each module-level ``const``
  bound once to an object literal whose every member is a function (a method, or a key
  whose value is a function), with its keys, their lines, and the names it is exported
  under. The claim belongs to the ``const``: it is the handler a route is served by.

``__dirname`` is read as the directory of the file that states it: the source is run
where it stands, or built to the same depth.

Parsed, never executed (V1_SPEC §17).
"""

from __future__ import annotations

import json
from collections import Counter
from typing import TYPE_CHECKING

from repoatlas.models.parsing import EdgeType, EntityType, ParsedReference
from repoatlas.parsers.http_facts import OAS3_APP, TS_HANDLER_OBJECT, pending_reference
from repoatlas.parsers.http_ts_wrappers import Const, FetchWrappers, arguments_of, operand
from repoatlas.parsers.http_typescript import static_string
from repoatlas.parsers.treesitter import line_range, node_text, string_literal

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tree_sitter import Node

OAS3_TOOLS = "oas3-tools"
FACTORY = "expressAppConfig"
_PATH_MODULES = frozenset({"path", "node:path"})
_PATH_JOINS = frozenset({"join", "resolve"})
_FUNCTION_VALUES = frozenset({"arrow_function", "function_expression", "function"})
_NAMES = frozenset({"identifier", "shorthand_property_identifier"})
_ACCESSORS = frozenset({"get", "set"})
#: A binding of a whole module: a namespace or default import, or ``require``.
_MODULE = "*"
#: How many ``const`` names a value is followed through.
_MAX_DEPTH = 4
#: Keys one handler object may have and still be recorded.
_MAX_KEYS = 256


class SpecRoutedApps:
    """What one file states about oas3-tools applications and their controllers."""

    def __init__(
        self, source: bytes, root: Node, counts: Counter[str], wrappers: FetchWrappers
    ) -> None:
        self._source = source
        self._counts = counts
        self._wrappers = wrappers
        #: Local name → ``(module, imported name or _MODULE)``, for oas3-tools and path.
        self._packages: dict[str, tuple[str, str]] = {}
        #: Local name → ``(relative specifier, imported name)``.
        self._relative: dict[str, tuple[str, str]] = {}
        #: Handler object name → ``(keys and their lines, first line, last line)``.
        self._objects: dict[str, tuple[dict[str, int], int, int]] = {}
        self._exports: dict[str, list[str]] = {}
        self._top_level(root)

    # -- the module ------------------------------------------------------------------

    def _top_level(self, root: Node) -> None:
        aliases: list[tuple[str, str]] = []
        for statement in root.named_children:
            if statement.type == "import_statement":
                self._import(statement)
            elif statement.type in {"lexical_declaration", "variable_declaration"}:
                self._declaration(statement, exported=False)
            elif statement.type == "export_statement":
                aliases.extend(self._export(statement))
        for local, exported in aliases:
            if local in self._objects:
                self._exports.setdefault(local, []).append(exported)

    def _bind(self, local: str, module: str, imported: str) -> None:
        if self._counts[local] != 1:
            return
        if module == OAS3_TOOLS or module in _PATH_MODULES:
            self._packages[local] = (module, imported)
        elif module.startswith(".") and imported != _MODULE:
            self._relative[local] = (module, imported)

    def _import(self, statement: Node) -> None:
        if any(child.type == "type" for child in statement.children):
            return
        source_node = statement.child_by_field_name("source")
        module = string_literal(self._source, source_node) if source_node is not None else None
        clause = next((c for c in statement.children if c.type == "import_clause"), None)
        if module is None or clause is None:
            return
        for child in clause.named_children:
            if child.type == "identifier":
                self._bind(node_text(self._source, child), module, _MODULE)
            elif child.type == "namespace_import":
                name = next((c for c in child.named_children if c.type == "identifier"), None)
                if name is not None:
                    self._bind(node_text(self._source, name), module, _MODULE)
            elif child.type == "named_imports":
                for specifier in child.named_children:
                    if specifier.type != "import_specifier" or any(
                        c.type == "type" for c in specifier.children
                    ):
                        continue
                    imported = specifier.child_by_field_name("name")
                    alias = specifier.child_by_field_name("alias") or imported
                    if imported is not None and alias is not None:
                        self._bind(
                            node_text(self._source, alias),
                            module,
                            node_text(self._source, imported),
                        )

    def _declaration(self, statement: Node, *, exported: bool) -> list[tuple[str, str]]:
        kind = statement.child_by_field_name("kind")
        constant = kind is not None and node_text(self._source, kind) == "const"
        found: list[tuple[str, str]] = []
        for declarator in statement.named_children:
            name_node = declarator.child_by_field_name("name")
            value = declarator.child_by_field_name("value")
            if declarator.type != "variable_declarator" or name_node is None or value is None:
                continue
            if name_node.type != "identifier":
                continue
            name = node_text(self._source, name_node)
            value = operand(value)
            required = self._required(value)
            if required is not None:
                self._bind(name, required, _MODULE)
                continue
            members = self._members(value) if constant and self._counts[name] == 1 else None
            if members is not None:
                start, end = line_range(declarator)
                self._objects[name] = (members, start, end)
                if exported:
                    found.append((name, name))
        return found

    def _required(self, value: Node) -> str | None:
        """The module ``require('<module>')`` loads, for a call bound to a name."""
        callee = value.child_by_field_name("function") if value.type == "call_expression" else None
        if callee is None or node_text(self._source, callee) != "require":
            return None
        arguments = arguments_of(value) or []
        if len(arguments) != 1 or self._counts["require"]:
            return None
        return string_literal(self._source, arguments[0])

    def _export(self, statement: Node) -> list[tuple[str, str]]:
        declaration = statement.child_by_field_name("declaration")
        default = any(child.type == "default" for child in statement.children)
        if declaration is not None:
            if declaration.type == "lexical_declaration" and not default:
                return self._declaration(declaration, exported=True)
            return []
        value = statement.child_by_field_name("value")
        if value is not None:
            value = operand(value)
            if default and value.type == "identifier":
                return [(node_text(self._source, value), "default")]
            return []
        if statement.child_by_field_name("source") is not None:
            return []
        found: list[tuple[str, str]] = []
        for clause in statement.named_children:
            if clause.type != "export_clause":
                continue
            for specifier in clause.named_children:
                name = specifier.child_by_field_name("name")
                alias = specifier.child_by_field_name("alias") or name
                if specifier.type == "export_specifier" and name is not None and alias is not None:
                    found.append((node_text(self._source, name), node_text(self._source, alias)))
        return found

    def _members(self, node: Node) -> dict[str, int] | None:
        """Each key of an object literal whose every member is a function, and its line."""
        if node.type != "object":
            return None
        keys: dict[str, int] = {}
        for entry in node.named_children:
            if entry.type == "comment":
                continue
            if entry.type == "pair":
                key = self._wrappers.key(entry)
                value = entry.child_by_field_name("value")
                if key is None or value is None or operand(value).type not in _FUNCTION_VALUES:
                    return None
            elif entry.type == "method_definition":
                name = entry.child_by_field_name("name")
                if name is None or any(c.type in _ACCESSORS for c in entry.children):
                    return None
                if name.type == "property_identifier":
                    key = node_text(self._source, name)
                elif name.type == "string":
                    key = string_literal(self._source, name)
                else:
                    key = None
                if key is None:
                    return None
            else:
                return None  # a spread or a shorthand: a member not proven a function
            keys[key] = line_range(entry)[0]
        return keys if 0 < len(keys) <= _MAX_KEYS else None

    # -- calls ---------------------------------------------------------------------------

    def call_references(self, node: Node, source_symbol: str | None) -> list[ParsedReference]:
        callee = node.child_by_field_name("function")
        if callee is None or not self._is_factory(operand(callee)):
            return []
        arguments = arguments_of(node)
        if arguments is None or len(arguments) != 2:
            return []
        meta: dict[str, str] = {}
        spec = self._spec(arguments[0])
        if spec is not None:
            meta["spec"] = spec
        controllers = self._controllers(arguments[1])
        if controllers is not None:
            meta["controllers"] = json.dumps(controllers, sort_keys=True)
        start, end = line_range(node)
        return [
            pending_reference(
                EdgeType.REFERENCES,
                EntityType.CONFIG,
                FACTORY,
                start,
                end,
                role=OAS3_APP,
                source_symbol=source_symbol,
                **meta,
            )
        ]

    def _package(self, node: Node) -> tuple[str, str] | None:
        """What a name refers to among oas3-tools and path, at its module-level binding."""
        if node.type != "identifier":
            return None
        name = node_text(self._source, node)
        bound = self._packages.get(name)
        return bound if bound is not None and self._wrappers.module_binding(name, node) else None

    def _is_factory(self, callee: Node) -> bool:
        if callee.type == "identifier":
            return self._package(callee) == (OAS3_TOOLS, FACTORY)
        if callee.type != "member_expression":
            return False
        owner = callee.child_by_field_name("object")
        prop = callee.child_by_field_name("property")
        if owner is None or prop is None or node_text(self._source, prop) != FACTORY:
            return False
        return self._package(operand(owner)) == (OAS3_TOOLS, _MODULE)

    def _spec(self, node: Node, depth: int = 0) -> str | None:
        """``path.join(__dirname, '<literal>', …)``: the literals, joined by ``/``."""
        node = operand(node)
        if node.type in _NAMES and depth < _MAX_DEPTH:
            binding = self._wrappers.lookup(node_text(self._source, node), node)
            return self._spec(binding.value, depth + 1) if isinstance(binding, Const) else None
        if node.type != "call_expression":
            return None
        callee = node.child_by_field_name("function")
        callee = operand(callee) if callee is not None else None
        if callee is None:
            return None
        if callee.type == "member_expression":
            owner = callee.child_by_field_name("object")
            prop = callee.child_by_field_name("property")
            bound = self._package(operand(owner)) if owner is not None else None
            joins = (
                bound is not None
                and bound[1] == _MODULE
                and bound[0] in _PATH_MODULES
                and prop is not None
                and node_text(self._source, prop) in _PATH_JOINS
            )
        else:
            bound = self._package(callee)
            joins = bound is not None and bound[0] in _PATH_MODULES and bound[1] in _PATH_JOINS
        arguments = arguments_of(node) or []
        if not joins or len(arguments) < 2:
            return None
        first = operand(arguments[0])
        if node_text(self._source, first) != "__dirname" or self._counts["__dirname"]:
            return None
        parts = [static_string(self._source, operand(argument)) for argument in arguments[1:]]
        if any(part is None or part.startswith("/") or "\\" in part for part in parts):
            return None
        joined = "/".join(part for part in parts if part)
        return joined or None

    def _object(self, node: Node, depth: int = 0) -> Node | None:
        """The object literal a value is, through ``const`` names."""
        node = operand(node)
        if node.type in _NAMES and depth < _MAX_DEPTH:
            binding = self._wrappers.lookup(node_text(self._source, node), node)
            return self._object(binding.value, depth + 1) if isinstance(binding, Const) else None
        return node if node.type == "object" else None

    def _entries(self, node: Node) -> dict[str, Node] | None:
        """An object literal's entries by key, or ``None`` when a key is not provable."""
        entries: dict[str, Node] = {}
        for entry in node.named_children:
            if entry.type == "comment":
                continue
            if entry.type == "shorthand_property_identifier":
                entries[node_text(self._source, entry)] = entry
            elif entry.type == "pair":
                key = self._wrappers.key(entry)
                value = entry.child_by_field_name("value")
                if key is None or value is None:
                    return None
                entries[key] = value
            else:
                return None
        return entries

    def _controllers(self, options: Node) -> list[dict[str, str]] | None:
        """``options.routing.controllers`` as handler objects, in spread order."""
        found: Node | None = options
        for key in ("routing", "controllers"):
            literal = self._object(found) if found is not None else None
            entries = self._entries(literal) if literal is not None else None
            found = entries.get(key) if entries is not None else None
        if found is None:
            return None
        own = self._handler(found)
        if own is not None:
            return [own]
        literal = self._object(found)
        if literal is None:
            return None
        refs: list[dict[str, str]] = []
        for entry in literal.named_children:
            if entry.type == "comment":
                continue
            inner = next((c for c in entry.named_children if c.type != "comment"), None)
            ref = self._handler(inner) if entry.type == "spread_element" and inner else None
            if ref is None:
                return None
            refs.append(ref)
        return refs or None

    def _handler(self, node: Node) -> dict[str, str] | None:
        """A handler object declared here, or one imported by name from a relative module."""
        node = operand(node)
        if node.type not in _NAMES:
            return None
        name = node_text(self._source, node)
        if not self._wrappers.module_binding(name, node):
            return None
        if name in self._objects:
            return {"local": name}
        if name in self._relative:
            module, imported = self._relative[name]
            return {"module": module, "name": imported}
        return None

    # -- claims --------------------------------------------------------------------------

    def references(self) -> list[ParsedReference]:
        """Each handler object, owned by its ``const``, with its keys and exports."""
        return [
            pending_reference(
                EdgeType.REFERENCES,
                EntityType.SYMBOL,
                name,
                start,
                end,
                role=TS_HANDLER_OBJECT,
                source_symbol=name,
                keys=json.dumps(keys, sort_keys=True),
                exports=json.dumps(self._exports.get(name, [])),
            )
            for name, (keys, start, end) in self._objects.items()
        ]
