"""Fastify routes in TypeScript and JavaScript source (HTTP linkage).

The rule of :mod:`repoatlas.parsers.http_typescript` applied to Fastify: nothing is
matched by method name. A receiver counts only when the source proves what it is:

* **an application** -- ``Fastify()`` or ``fastify()``, the factory imported from
  ``"fastify"`` (a default or named ``fastify`` import, or ``require``), assigned
  to a module-level ``const`` bound once in the file;
* **a plugin's server** -- the first parameter of a plugin function, bound once
  in that function's own scope. A plugin function is a module-level function (a
  declaration, or a ``const`` holding one, possibly wrapped in
  ``fastify-plugin``), a default-exported function, or a function handed straight
  to ``register``. Only a file that imports ``fastify`` or ``fastify-plugin`` is
  read for plugins at all.

A route is ``server.<verb>(path, [options,] handler)`` or ``server.route({ method,
url, handler })`` with a literal method. A path is a literal, or a template or a
concatenation whose every substitution is a module-level string constant of the
file, or a name imported from a relative module (one hop, decided by the global
pass from that module's :data:`~repoatlas.parsers.http_facts.TS_CONSTANTS`).

An application's route with a literal path is served here, at that path. Every
other route -- on a plugin, or through an imported constant -- is a
:data:`~repoatlas.parsers.http_facts.FASTIFY_ROUTE` claim: a plugin's routes are
served under the prefixes of the registrations that reach it, which only the
global pass (:mod:`repoatlas.graph.ts_routes`) can follow across files. Each
``server.register(plugin, options)`` whose plugin this file identifies is a
:data:`~repoatlas.parsers.http_facts.FASTIFY_REGISTER` claim. Its options are
readable when absent, or when they are an object literal with no spread whose
``prefix``, if any, is a literal Fastify accepts. A ``fastify-plugin`` plugin ignores
``prefix``, so it is unreadable with one.

Parsed, never executed (V1_SPEC §17).
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from repoatlas.models import http
from repoatlas.models.parsing import EdgeType, EntityType, ParsedReference
from repoatlas.parsers.http_facts import (
    COMPOSED,
    EXACT,
    FASTIFY_REGISTER,
    FASTIFY_ROUTE,
    FASTIFY_SCOPE,
    TS_CONSTANTS,
    http_reference,
    pending_reference,
)
from repoatlas.parsers.http_typescript import (
    FUNCTION_NODES,
    plain_object,
    scope_bindings,
    static_string,
)
from repoatlas.parsers.treesitter import line_range, node_text, string_literal, walk

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tree_sitter import Node

SOURCE = "fastify"

_VERBS = {
    "get": "GET",
    "head": "HEAD",
    "post": "POST",
    "put": "PUT",
    "delete": "DELETE",
    "options": "OPTIONS",
    "patch": "PATCH",
}
_METHODS = frozenset(_VERBS.values())

#: The kinds of scope a route or a registration belongs to.
APP = "app"
PLUGIN = "plugin"

_FUNCTIONS = FUNCTION_NODES
_DECLARATIONS = frozenset({"function_declaration", "generator_function_declaration"})
_FUNCTION_VALUES = frozenset({"arrow_function", "function_expression", "function"})
_TRANSPARENT = frozenset({"parenthesized_expression", "await_expression", "as_expression"})

#: Where a name is bound, when no single function binds it.
_MODULE = "module"
_AMBIGUOUS = "ambiguous"

#: The scope id of an anonymous default-exported plugin: no identifier can spell it.
DEFAULT_SCOPE = "*default"

#: Constants longer than this, or holding whitespace, are no path or URL part.
_MAX_CONSTANT = 256
#: How many constants one module offers other modules.
_MAX_CONSTANTS = 64

_Key = tuple[int, int, str]


def _key(node: Node) -> _Key:
    return node.start_byte, node.end_byte, node.type


def _operand(node: Node) -> Node:
    while node.type in _TRANSPARENT:
        inner = [child for child in node.named_children if child.type != "comment"]
        if not inner:
            break
        node = inner[0]
    return node


def _arguments(node: Node) -> list[Node] | None:
    arguments = node.child_by_field_name("arguments")
    if arguments is None or arguments.type != "arguments":
        return None
    return [child for child in arguments.children if child.is_named and child.type != "comment"]


def _path_like(value: str) -> bool:
    return len(value) <= _MAX_CONSTANT and not any(char.isspace() for char in value)


def fastify_prefix(prefix: str) -> bool:
    """Whether Fastify serves a plugin's routes under ``prefix`` exactly as written.

    Rooted, with no trailing slash (one changes what a ``/`` route serves), and a
    valid path itself.
    """
    return prefix.startswith("/") and not prefix.endswith("/") and http.route_path(prefix) == prefix


@dataclass(slots=True)
class _Scope:
    id: str
    kind: str
    line: int
    wrapped: bool = False
    #: ``(exported name, wrapped in fastify-plugin there)``.
    exports: list[tuple[str, bool]] = field(default_factory=list)


class FastifyDeclarations:
    """What one file proves about Fastify, and the facts and claims that follow."""

    def __init__(self, source: bytes, root: Node, counts: Counter[str]) -> None:
        self._source = source
        self._counts = counts
        self._factories: set[str] = set()
        self._wrappers: set[str] = set()
        #: Local name → ``(relative specifier, imported name)``, each bound once.
        self._imported: dict[str, tuple[str, str]] = {}
        #: Module-level ``const`` strings bound once: value and line.
        self._constants: dict[str, tuple[str, int]] = {}
        #: Exported name → local constant name.
        self._exported_constants: dict[str, str] = {}
        self._scopes: dict[_Key, _Scope] = {}
        #: Module-level plugin scopes by local name, and applications by name.
        self._named: dict[str, _Scope] = {}
        self._apps: dict[str, _Scope] = {}
        self._own: dict[_Key, Counter[str]] = {}
        self._registrations: list[ParsedReference] = []
        self._reads = False
        self._top_level(root)
        if self._reads:
            self._register_calls(root)

    # -- the module ------------------------------------------------------------------

    def _top_level(self, root: Node) -> None:
        aliases: list[tuple[str, str, bool]] = []
        for statement in root.named_children:
            if statement.type == "import_statement":
                self._import(statement)
            elif statement.type == "lexical_declaration":
                self._declaration(statement, exported=False)
            elif statement.type in _DECLARATIONS:
                self._function(statement, exported=None)
            elif statement.type == "export_statement":
                aliases.extend(self._export(statement))
        for local, exported, wrapped in aliases:
            scope = self._named.get(local)
            if scope is not None:
                scope.exports.append((exported, wrapped))
            elif local in self._constants and not wrapped:
                self._exported_constants[exported] = local

    def _import(self, statement: Node) -> None:
        source_node = statement.child_by_field_name("source")
        module = string_literal(self._source, source_node) if source_node is not None else None
        if module is None:
            return
        if module in {"fastify", "fastify-plugin"}:
            self._reads = True
        clause = next((c for c in statement.children if c.type == "import_clause"), None)
        if clause is None:
            return
        for child in clause.named_children:
            if child.type == "identifier":
                local = node_text(self._source, child)
                if self._counts[local] != 1:
                    continue
                if module == "fastify":
                    self._factories.add(local)
                elif module == "fastify-plugin":
                    self._wrappers.add(local)
                elif module.startswith("."):
                    self._imported[local] = (module, "default")
            elif child.type == "named_imports":
                for specifier in child.named_children:
                    if specifier.type != "import_specifier":
                        continue
                    imported = specifier.child_by_field_name("name")
                    alias = specifier.child_by_field_name("alias") or imported
                    if imported is None or alias is None:
                        continue
                    local = node_text(self._source, alias)
                    name = node_text(self._source, imported)
                    if self._counts[local] != 1:
                        continue
                    if module == "fastify" and name == "fastify":
                        self._factories.add(local)
                    elif module.startswith("."):
                        self._imported[local] = (module, name)

    def _declaration(self, statement: Node, *, exported: bool) -> list[tuple[str, str, bool]]:
        kind = statement.child_by_field_name("kind")
        if kind is None or node_text(self._source, kind) != "const":
            return []
        found: list[tuple[str, str, bool]] = []
        for declarator in statement.named_children:
            if declarator.type != "variable_declarator":
                continue
            name_node = declarator.child_by_field_name("name")
            value = declarator.child_by_field_name("value")
            if name_node is None or name_node.type != "identifier" or value is None:
                continue
            name = node_text(self._source, name_node)
            if self._counts[name] != 1:
                continue
            line = line_range(declarator)[0]
            text = static_string(self._source, value)
            if text is not None:
                self._constants[name] = (text, line)
                if exported:
                    found.append((name, name, False))
                continue
            value = _operand(value)
            if value.type in _FUNCTION_VALUES:
                self._plugin(value, name, line, wrapped=False)
                if exported:
                    found.append((name, name, False))
            elif value.type == "call_expression":
                self._call_value(value, name, line)
                if exported and name in self._named:
                    found.append((name, name, False))
        return found

    def _call_value(self, value: Node, name: str, line: int) -> None:
        callee = value.child_by_field_name("function")
        arguments = _arguments(value) or []
        if callee is None:
            return
        if callee.type == "identifier":
            callee_name = node_text(self._source, callee)
            if callee_name in self._factories:
                self._apps[name] = self._scope(value, name, APP, line)
            elif callee_name == "require" and self._required(arguments) == "fastify":
                self._factories.add(name)
                self._reads = True
            elif callee_name in self._wrappers and len(arguments) >= 1:
                inner = _operand(arguments[0])
                if inner.type in _FUNCTION_VALUES:
                    self._plugin(inner, name, line, wrapped=True)
        elif callee.type == "call_expression":
            # `require("fastify")(options)`: an application straight from the factory.
            inner = callee.child_by_field_name("function")
            if (
                inner is not None
                and inner.type == "identifier"
                and node_text(self._source, inner) == "require"
                and self._required(_arguments(callee) or []) == "fastify"
            ):
                self._reads = True
                self._apps[name] = self._scope(value, name, APP, line)

    def _required(self, arguments: list[Node]) -> str | None:
        if len(arguments) != 1 or arguments[0].type != "string":
            return None
        return string_literal(self._source, arguments[0])

    def _function(self, statement: Node, *, exported: str | None) -> list[tuple[str, str, bool]]:
        name_node = statement.child_by_field_name("name")
        line = line_range(statement)[0]
        if name_node is None:
            if exported == "default":
                scope = self._plugin(statement, DEFAULT_SCOPE, line, wrapped=False)
                scope.exports.append(("default", False))
            return []
        name = node_text(self._source, name_node)
        if self._counts[name] != 1:
            return []
        self._plugin(statement, name, line, wrapped=False)
        return [(name, exported, False)] if exported is not None else []

    def _export(self, statement: Node) -> list[tuple[str, str, bool]]:
        default = any(child.type == "default" for child in statement.children)
        declaration = statement.child_by_field_name("declaration")
        if declaration is not None:
            if declaration.type in _DECLARATIONS:
                named = declaration.child_by_field_name("name")
                name = node_text(self._source, named) if named is not None else "default"
                return self._function(declaration, exported="default" if default else name)
            if declaration.type == "lexical_declaration" and not default:
                return self._declaration(declaration, exported=True)
            return []
        value = statement.child_by_field_name("value")
        if value is not None and default:
            value = _operand(value)
            line = line_range(statement)[0]
            if value.type == "identifier":
                return [(node_text(self._source, value), "default", False)]
            if value.type in _FUNCTION_VALUES:
                scope = self._plugin(value, DEFAULT_SCOPE, line, wrapped=False)
                scope.exports.append(("default", False))
                return []
            if value.type == "call_expression":
                callee = value.child_by_field_name("function")
                arguments = _arguments(value) or []
                if (
                    callee is not None
                    and callee.type == "identifier"
                    and node_text(self._source, callee) in self._wrappers
                    and arguments
                ):
                    inner = _operand(arguments[0])
                    if inner.type == "identifier":
                        return [(node_text(self._source, inner), "default", True)]
                    if inner.type in _FUNCTION_VALUES:
                        scope = self._plugin(inner, DEFAULT_SCOPE, line, wrapped=True)
                        scope.exports.append(("default", True))
            return []
        found: list[tuple[str, str, bool]] = []
        if statement.child_by_field_name("source") is not None:
            return found  # a re-export names another module's bindings
        for clause in statement.named_children:
            if clause.type != "export_clause":
                continue
            for specifier in clause.named_children:
                if specifier.type != "export_specifier":
                    continue
                name = specifier.child_by_field_name("name")
                alias = specifier.child_by_field_name("alias") or name
                if name is not None and alias is not None:
                    found.append(
                        (node_text(self._source, name), node_text(self._source, alias), False)
                    )
        return found

    def _scope(self, node: Node, scope_id: str, kind: str, line: int) -> _Scope:
        scope = _Scope(scope_id, kind, line)
        self._scopes[_key(node)] = scope
        return scope

    def _plugin(self, node: Node, name: str, line: int, *, wrapped: bool) -> _Scope:
        scope = self._scope(node, name, PLUGIN, line)
        scope.wrapped = wrapped
        if name != DEFAULT_SCOPE:
            self._named[name] = scope
        return scope

    # -- names -----------------------------------------------------------------------

    def _own_counts(self, function: Node) -> Counter[str]:
        """Names bound in ``function``'s own scope, never inside a nested function."""
        key = _key(function)
        found = self._own.get(key)
        if found is None:
            found = scope_bindings(self._source, function)
            self._own[key] = found
        return found

    def _enclosing_binding(self, at: Node, name: str) -> Node | str:
        """The function that binds ``name`` where ``at`` uses it.

        :data:`_MODULE` when no function does, and :data:`_AMBIGUOUS` when the nearest
        one binds it more than once.
        """
        node = at.parent
        while node is not None and node.type != "program":
            if node.type in _FUNCTIONS:
                counts = self._own_counts(node)
                if counts[name]:
                    return node if counts[name] == 1 else _AMBIGUOUS
            node = node.parent
        return _MODULE

    def _module_name(self, at: Node, name: str) -> bool:
        """Whether ``name`` at ``at`` is the module-level binding, bound once in the file."""
        return self._enclosing_binding(at, name) == _MODULE and self._counts[name] == 1

    def _receiver(self, at: Node, name: str) -> _Scope | None:
        """The application or plugin server ``name`` is where ``at`` uses it."""
        bound = self._enclosing_binding(at, name)
        if isinstance(bound, str):
            if bound == _MODULE and self._counts[name] == 1:
                return self._apps.get(name)
            return None
        if _first_parameter(self._source, bound) != name:
            return None
        return self._scopes.get(_key(bound))

    # -- registrations -----------------------------------------------------------------

    def _register_calls(self, root: Node) -> None:
        for node in walk(root):
            if node.type != "call_expression":
                continue
            callee = node.child_by_field_name("function")
            if callee is None or callee.type != "member_expression":
                continue
            owner_node = callee.child_by_field_name("object")
            prop = callee.child_by_field_name("property")
            if (
                owner_node is None
                or prop is None
                or owner_node.type != "identifier"
                or node_text(self._source, prop) != "register"
            ):
                continue
            owner = self._receiver(node, node_text(self._source, owner_node))
            arguments = _arguments(node) or []
            if owner is None or not 1 <= len(arguments) <= 2:
                continue
            child = self._registered(node, _operand(arguments[0]))
            if child is None:
                continue
            target, wrapped = child
            prefix: str | None = None
            readable = True
            if len(arguments) == 2:
                options = plain_object(self._source, _operand(arguments[1]))
                if options is None:
                    readable = False
                elif "prefix" in options:
                    prefix = static_string(self._source, options["prefix"])
                    readable = prefix is not None and fastify_prefix(prefix)
            if wrapped and prefix:
                readable = False  # fastify-plugin ignores the prefix it is given
            start, end = line_range(node)
            self._registrations.append(
                pending_reference(
                    EdgeType.REFERENCES,
                    EntityType.SYMBOL,
                    target.get("name") or target.get("local", ""),
                    start,
                    end,
                    role=FASTIFY_REGISTER,
                    owner=owner.id,
                    owner_kind=owner.kind,
                    wrapped="true" if wrapped else "false",
                    readable="true" if readable else "false",
                    prefix=json.dumps(prefix if readable else None),
                    **target,
                )
            )

    def _registered(self, at: Node, value: Node) -> tuple[dict[str, str], bool] | None:
        """What ``register`` is handed: a plugin this file declares or imports."""
        wrapped = False
        if value.type == "call_expression":
            callee = value.child_by_field_name("function")
            arguments = _arguments(value) or []
            if (
                callee is None
                or callee.type != "identifier"
                or node_text(self._source, callee) not in self._wrappers
                or not arguments
            ):
                return None
            wrapped = True
            value = _operand(arguments[0])
        if value.type in _FUNCTION_VALUES:
            line, column = value.start_point
            scope = self._scopes.get(_key(value)) or self._plugin(
                value, f"@{line + 1}:{column + 1}", line + 1, wrapped=wrapped
            )
            return {"local": scope.id}, wrapped
        if value.type != "identifier":
            return None
        name = node_text(self._source, value)
        if not self._module_name(at, name):
            return None
        if name in self._named:
            return {"local": name}, wrapped or self._named[name].wrapped
        if name in self._imported:
            module, imported = self._imported[name]
            return {"module": module, "name": imported}, wrapped
        return None

    # -- routes ------------------------------------------------------------------------

    def call_references(self, node: Node, source_symbol: str | None) -> list[ParsedReference]:
        if not self._reads:
            return []
        callee = node.child_by_field_name("function")
        if callee is None or callee.type != "member_expression":
            return []
        owner_node = callee.child_by_field_name("object")
        prop = callee.child_by_field_name("property")
        if owner_node is None or prop is None or owner_node.type != "identifier":
            return []
        attribute = node_text(self._source, prop)
        if attribute not in _VERBS and attribute != "route":
            return []
        scope = self._receiver(node, node_text(self._source, owner_node))
        arguments = _arguments(node)
        if scope is None or arguments is None:
            return []
        declared = self._declared(attribute, arguments)
        if declared is None:
            return []
        methods, path_node, end = declared
        parts = self._parts(path_node)
        if parts is None:
            return []
        start = line_range(node)[0]
        literal = all(part[0] == "t" for part in parts)
        if scope.kind == APP and literal:
            route = http.route_path("".join(part[1] for part in parts))
            if route is None:
                return []
            return [
                http_reference(
                    EdgeType.SERVES_HTTP_ROUTE,
                    EntityType.HTTP_ROUTE,
                    http.route_body(method, route),
                    start,
                    end,
                    source=SOURCE,
                    composed=False,
                    source_symbol=source_symbol,
                )
                for method in methods
            ]
        shown = "".join(part[1] if part[0] == "t" else "{" + part[2] + "}" for part in parts)
        return [
            pending_reference(
                EdgeType.SERVES_HTTP_ROUTE,
                EntityType.HTTP_ROUTE,
                f"{method} {shown}",
                start,
                end,
                role=FASTIFY_ROUTE,
                source_symbol=source_symbol,
                method=method,
                path=json.dumps(parts),
                scope=scope.id,
                provenance=EXACT if literal else COMPOSED,
            )
            for method in methods
        ]

    def _declared(
        self, attribute: str, arguments: list[Node]
    ) -> tuple[tuple[str, ...], Node, int] | None:
        """``(methods, path node, last line)`` of a route declaration, or ``None``."""
        if attribute in _VERBS:
            # The path, then options or the handler: one argument is no route.
            if len(arguments) < 2:
                return None
            return (_VERBS[attribute],), arguments[0], line_range(arguments[0])[1]
        if len(arguments) != 1:
            return None
        options = plain_object(self._source, _operand(arguments[0]))
        if options is None or "url" not in options or "method" not in options:
            return None
        method_node = _operand(options["method"])
        written: list[str | None]
        if method_node.type == "array":
            written = [
                static_string(self._source, element)
                for element in method_node.named_children
                if element.type != "comment"
            ]
        else:
            written = [static_string(self._source, method_node)]
        if not written or any(method not in _METHODS for method in written):
            return None
        methods = tuple(dict.fromkeys(method for method in written if method is not None))
        end = max(line_range(options["url"])[1], line_range(options["method"])[1])
        return methods, options["url"], end

    def _parts(self, node: Node) -> list[list[str]] | None:
        """A path as literal text and named constants, or ``None`` if it is not one."""
        text = static_string(self._source, node)
        if text is not None:
            return [["t", text]]
        node = _operand(node)
        parts: list[list[str]] = []
        if node.type == "template_string":
            for child in node.children:
                if child.type == "string_fragment":
                    parts.append(["t", node_text(self._source, child)])
                elif child.type == "template_substitution":
                    inner = [c for c in child.named_children if c.type != "comment"]
                    if len(inner) != 1:
                        return None
                    named = self._named_part(inner[0])
                    if named is None:
                        return None
                    parts.append(named)
                elif child.type == "escape_sequence":
                    return None
        elif node.type == "binary_expression":
            operator = node.child_by_field_name("operator")
            left = node.child_by_field_name("left")
            right = node.child_by_field_name("right")
            if operator is None or left is None or right is None:
                return None
            if node_text(self._source, operator) != "+":
                return None
            head, tail = self._parts(left), self._parts(right)
            if head is None or tail is None:
                return None
            parts = head + tail
        elif node.type == "identifier":
            named = self._named_part(node)
            if named is None:
                return None
            parts = [named]
        else:
            return None
        merged: list[list[str]] = []
        for part in parts:
            if part[0] == "t" and merged and merged[-1][0] == "t":
                merged[-1] = ["t", merged[-1][1] + part[1]]
            else:
                merged.append(part)
        return merged

    def _named_part(self, node: Node) -> list[str] | None:
        node = _operand(node)
        if node.type != "identifier":
            return None
        name = node_text(self._source, node)
        if not self._module_name(node, name):
            return None
        if name in self._constants:
            return ["t", self._constants[name][0]]
        if name in self._imported:
            module, imported = self._imported[name]
            return ["r", module, imported]
        return None

    # -- claims ------------------------------------------------------------------------

    def references(self) -> list[ParsedReference]:
        """What only the global pass can decide: scopes, registrations, offered constants."""
        found: list[ParsedReference] = []
        if self._reads:
            for scope in self._scopes.values():
                found.append(
                    pending_reference(
                        EdgeType.REFERENCES,
                        EntityType.SYMBOL,
                        scope.id,
                        scope.line,
                        scope.line,
                        role=FASTIFY_SCOPE,
                        kind=scope.kind,
                        wrapped="true" if scope.wrapped else "false",
                        exports=json.dumps(scope.exports),
                    )
                )
            found.extend(self._registrations)
        offered = sorted(
            (self._constants[local][1], exported, self._constants[local][0])
            for exported, local in self._exported_constants.items()
            if _path_like(self._constants[local][0])
        )[:_MAX_CONSTANTS]
        if offered:
            found.append(
                pending_reference(
                    EdgeType.REFERENCES,
                    EntityType.CONFIG,
                    TS_CONSTANTS,
                    offered[0][0],
                    offered[-1][0],
                    role=TS_CONSTANTS,
                    constants=json.dumps(
                        {name: [value, line] for line, name, value in offered}, sort_keys=True
                    ),
                )
            )
        return found


def _first_parameter(source: bytes, function: Node) -> str | None:
    single = function.child_by_field_name("parameter")
    if single is not None:
        return node_text(source, single) if single.type == "identifier" else None
    parameters = function.child_by_field_name("parameters")
    if parameters is None:
        return None
    for parameter in parameters.named_children:
        if parameter.type == "comment":
            continue
        if parameter.type == "identifier":
            return node_text(source, parameter)
        pattern = parameter.child_by_field_name("pattern")
        if pattern is not None and pattern.type == "identifier":
            return node_text(source, pattern)
        return None
    return None
