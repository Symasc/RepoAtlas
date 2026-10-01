"""NestJS declarations in TypeScript source (HTTP linkage).

A NestJS controller serves nothing on its own. ``@Controller("v1")`` and
``@Get("me")`` state what a controller answers *relative to where it is mounted*,
and the mount is decided elsewhere: a ``@Module`` must list the controller, an
application's root module must reach that module through ``imports``, and the
application ``NestFactory.create`` builds may add a global prefix, versioning or
``RouterModule`` paths. That is an Express router or a FastAPI ``APIRouter``
included from another file, and the rule there already holds: a route is not a
fact until its mount is proven.

So this module records **declarations** and proves nothing across files. The
global pass (:mod:`repoatlas.graph.http_claims`) reads them together:

* **routes** — each handler whose HTTP decorator and controller path are static,
  as a pending ``nestjs-route`` claim carrying the method, both path fragments and
  the controller-local route, and the version its ``@Version`` or else its
  controller states, if either does (a string, ``VERSION_NEUTRAL``, or an array of
  them). Nothing reaches a shared HTTP node from here;
* **modules** — a ``@Module`` class and every entry of its ``controllers`` and
  ``imports``. An entry that cannot be read is recorded with what is known about
  it, never dropped: an unread entry is not an entry without effect;
* **roots** — the root module of each ``NestFactory.create``, with the global
  prefix when this file proves it — which includes proving that nothing else done
  to the application could change a route (documenting it with ``SwaggerModule``
  cannot) — and its URI versioning: the prefix before each version and the default
  version. Any other versioning type, or an option this cannot read, leaves the
  prefix unknown;
* **markers** — an import of ``RouterModule`` or ``ApplicationConfig``, or of
  ``@nestjs/core`` without a named list. Each can change routes that the
  registration it appears in never mentions. A ``require`` or ``import()`` of it
  destructured by name, ``const { NestFactory } = require("@nestjs/core")``, is a
  named list.

**Recognition is by import, never by name.** A decorator counts only when it is a
named import from the NestJS package that defines it, bound once in the file. A
local function called ``Get`` or a ``Controller`` from another module is not
NestJS, and a namespace import is not read.

``@MessagePattern`` and ``@EventPattern`` are recognised only so that a message
handler is never mistaken for an HTTP route: a handler yields a route from an HTTP
decorator of its own and from nothing else. ``@All`` has no method identity in the
HTTP model and yields nothing.

Parsed, never executed (V1_SPEC §17).
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from repoatlas.models import http
from repoatlas.models.parsing import EdgeType, EntityType, ParsedReference
from repoatlas.parsers.http_facts import (
    COMPOSED,
    EXACT,
    NESTJS_CONTROLLER,
    NESTJS_IMPORT,
    NESTJS_MARKER,
    NESTJS_MODULE,
    NESTJS_ROOT,
    NESTJS_ROUTE,
    pending_reference,
)
from repoatlas.parsers.http_typescript import static_string
from repoatlas.parsers.treesitter import line_range, node_text, string_literal, walk

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tree_sitter import Node

COMMON = "@nestjs/common"
CORE = "@nestjs/core"
MICROSERVICES = "@nestjs/microservices"
_PACKAGES = frozenset({COMMON, CORE, MICROSERVICES})
SWAGGER = "@nestjs/swagger"
#: What ``SwaggerModule`` does with an application it is handed: read its routes into
#: a document, and serve that document. Neither changes a route of the application.
_DOCUMENTING = frozenset({"createDocument", "setup"})

#: The method decorators with a method identity in :mod:`repoatlas.models.http`.
VERBS = {
    "Get": "GET",
    "Post": "POST",
    "Put": "PUT",
    "Patch": "PATCH",
    "Delete": "DELETE",
    "Options": "OPTIONS",
    "Head": "HEAD",
}

#: Characters with pattern meaning to the NestJS router. A path holding one is a
#: pattern, not a route identity.
_PATTERN = frozenset("*?()+{}[]")

#: Recorded on every route claim: what the declaration still lacks to be a fact.
LIMITATION = "application mount not proven"

#: The kinds of a module ``imports`` entry.
MODULE = "module"
DYNAMIC = "dynamic"
ROUTER = "router"
UNKNOWN = "unknown"

#: The kinds of a routing marker.
ROUTER_MARKER = "router"
GLOBAL_MARKER = "global"

#: A root's prefix is proven, or it is not.
PROVEN = "proven"

#: The versions a controller or handler states, in order: each a version string, or
#: ``None`` for ``VERSION_NEUTRAL``. Recorded on a claim as a JSON array.
Versions = tuple[str | None, ...]
#: The prefix URI versioning puts before a version when the application names none.
VERSION_PREFIX = "v"

#: How many names a dynamic module's arguments are searched for.
_MAX_ARGUMENT_NAMES = 64
#: A dynamic entry's target is its callee's text, bounded.
_MAX_TARGET = 200


def nest_path(*fragments: str) -> str | None:
    """NestJS's own composition of route fragments, or ``None``.

    ``RoutePathFactory``: each non-empty fragment is appended after removing one
    trailing ``/`` from what came before and adding a leading ``/`` to the
    fragment; the result is ``/`` when empty and otherwise loses one trailing
    ``/``. A fragment with pattern characters is refused rather than composed.
    """
    path = ""
    for fragment in fragments:
        if not fragment:
            continue
        if any(char in _PATTERN for char in fragment):
            return None
        path = _strip_end(path) + _add_leading(fragment)
    path = _add_leading(path) if path else "/"
    if path != "/":
        path = _strip_end(path)
    return http.route_path(path)


def _add_leading(fragment: str) -> str:
    return fragment if fragment.startswith("/") else f"/{fragment}"


def _strip_end(path: str) -> str:
    return path[:-1] if path.endswith("/") else path


def _arguments(call: Node) -> list[Node] | None:
    arguments = call.child_by_field_name("arguments")
    if arguments is None or arguments.type != "arguments":
        return None
    return [child for child in arguments.named_children if child.type != "comment"]


def _unwrap(node: Node) -> Node:
    """Strip parentheses, ``await``, ``as``, ``satisfies`` and ``!``."""
    while node.type in {
        "parenthesized_expression",
        "await_expression",
        "as_expression",
        "satisfies_expression",
        "non_null_expression",
    }:
        inner = [child for child in node.named_children if child.type != "comment"]
        if not inner:
            break
        node = inner[0]
    return node


@dataclass(frozen=True, slots=True)
class _Binding:
    """Where an imported name comes from."""

    specifier: str
    #: The exported name, ``default`` for a default import, ``*`` for a namespace.
    imported: str


@dataclass(frozen=True, slots=True)
class _Root:
    """One ``NestFactory.create`` call, as the file proves it."""

    target: str
    meta: dict[str, str]
    line_start: int
    line_end: int


class NestDeclarations:
    """What one file declares about NestJS routing, as claims for the global pass."""

    def __init__(self, source: bytes, root: Node, counts: Counter[str]) -> None:
        self._source = source
        self._counts = counts
        self._imports: dict[str, _Binding] = {}
        self._classes: Counter[str] = Counter()
        self._consts: dict[str, Node] = {}
        self._marked: dict[str, Node] = {}
        self._references: list[ParsedReference] = []
        self._roots: dict[tuple[int, int], _Root] = {}
        self._top_level(root)
        self._markers(root)
        for name, decorators, node in self._top_level_classes(root):
            self._class(name, decorators, node)
        self._find_roots(root)

    # -- output -----------------------------------------------------------------------

    def references(self) -> list[ParsedReference]:
        """Every declaration this file makes, except application roots."""
        return list(self._references)

    def call_references(self, node: Node, source_symbol: str | None) -> list[ParsedReference]:
        """The application root a ``NestFactory.create`` call declares, if it is one.

        Emitted from the walker, which knows the enclosing definition.
        """
        found = self._roots.get((node.start_byte, node.end_byte))
        if found is None:
            return []
        return [
            pending_reference(
                EdgeType.REFERENCES,
                EntityType.CLASS,
                found.target,
                found.line_start,
                found.line_end,
                role=NESTJS_ROOT,
                source_symbol=source_symbol,
                **found.meta,
            )
        ]

    # -- what the file binds ----------------------------------------------------------

    @staticmethod
    def _statements(root: Node) -> Iterator[Node]:
        """Top-level statements, with an ``export`` looked through to what it exports."""
        for statement in root.children:
            if statement.type == "export_statement":
                yield from (child for child in statement.children if child.is_named)
            else:
                yield statement

    def _top_level(self, root: Node) -> None:
        for statement in self._statements(root):
            if statement.type == "import_statement":
                self._import(statement)
            elif statement.type in {"class_declaration", "abstract_class_declaration"}:
                name = statement.child_by_field_name("name")
                if name is not None:
                    self._classes[node_text(self._source, name)] += 1
            elif statement.type == "lexical_declaration":
                kind = statement.child_by_field_name("kind")
                if kind is None or node_text(self._source, kind) != "const":
                    continue
                for declarator in statement.named_children:
                    if declarator.type != "variable_declarator":
                        continue
                    name = declarator.child_by_field_name("name")
                    value = declarator.child_by_field_name("value")
                    if name is not None and value is not None and name.type == "identifier":
                        self._consts[node_text(self._source, name)] = value

    def _import(self, statement: Node) -> None:
        if any(child.type == "type" for child in statement.children):
            return  # `import type { … }`: a type binds no value
        source_node = statement.child_by_field_name("source")
        specifier = string_literal(self._source, source_node) if source_node else None
        clause = next((c for c in statement.children if c.type == "import_clause"), None)
        if not specifier or clause is None:
            return
        for child in clause.children:
            if child.type == "identifier":
                self._imports[node_text(self._source, child)] = _Binding(specifier, "default")
            elif child.type == "namespace_import":
                name = next((c for c in child.children if c.type == "identifier"), None)
                if name is not None:
                    self._imports[node_text(self._source, name)] = _Binding(specifier, "*")
            elif child.type == "named_imports":
                for spec in child.children:
                    if spec.type != "import_specifier" or any(
                        c.type == "type" for c in spec.children
                    ):
                        continue
                    imported = spec.child_by_field_name("name")
                    alias = spec.child_by_field_name("alias") or imported
                    if imported is not None and alias is not None:
                        self._imports[node_text(self._source, alias)] = _Binding(
                            specifier, node_text(self._source, imported)
                        )
        if specifier == CORE:
            for binding in self._imports.values():
                kind = self._marker_kind(binding.imported) if binding.specifier == CORE else None
                if kind is not None:
                    self._marked.setdefault(kind, statement)

    def _nest(self, local: str) -> tuple[str, str] | None:
        """``(package, export)`` when ``local`` provably names a NestJS export."""
        binding = self._imports.get(local)
        if (
            binding is None
            or binding.specifier not in _PACKAGES
            or binding.imported in {"*", "default"}
            or self._counts[local] != 1
        ):
            return None
        return binding.specifier, binding.imported

    def _decorator(self, decorator: Node) -> tuple[tuple[str, str], Node] | None:
        """The NestJS export a decorator applies, with its call."""
        call = next((c for c in decorator.named_children if c.type == "call_expression"), None)
        if call is None:
            return None
        callee = call.child_by_field_name("function")
        if callee is None or callee.type != "identifier":
            return None
        nest = self._nest(node_text(self._source, callee))
        return (nest, call) if nest is not None else None

    def _binding(self, name: str) -> dict[str, str]:
        """How ``name`` is bound in this file, for the global pass to resolve."""
        binding = self._imports.get(name)
        if binding is not None and self._counts[name] == 1:
            return {"specifier": binding.specifier, "imported": binding.imported}
        # A class name is a type identifier to the TypeScript grammar and a value
        # binding to the JavaScript one, so the class itself may count once.
        if self._classes[name] == 1 and self._counts[name] <= 1 and name not in self._imports:
            return {"local": "true"}
        return {}

    # -- markers ----------------------------------------------------------------------

    def _markers(self, root: Node) -> None:
        found = dict(self._marked)
        for statement in root.children:
            source_node = statement.child_by_field_name("source")
            if (
                statement.type != "export_statement"
                or source_node is None
                or string_literal(self._source, source_node) != CORE
            ):
                continue
            # A re-export from `@nestjs/core`; `export *` hands on everything.
            names = [
                node_text(self._source, spec.children[0])
                for clause in statement.children
                if clause.type == "export_clause"
                for spec in clause.children
                if spec.type == "export_specifier" and spec.children
            ]
            if not names:
                found.setdefault(GLOBAL_MARKER, statement)
            for name in names:
                kind = self._marker_kind(name)
                if kind is not None:
                    found.setdefault(kind, statement)
        for node in walk(root):
            if node.type != "call_expression":
                continue
            callee = node.child_by_field_name("function")
            arguments = _arguments(node) or []
            if (
                callee is not None
                and (callee.type == "import" or node_text(self._source, callee) == "require")
                and arguments
                and string_literal(self._source, arguments[0]) == CORE
            ):
                names = self._destructured(node)
                if names is None:
                    found.setdefault(GLOBAL_MARKER, node)
                for name in names or ():
                    kind = self._marker_kind(name)
                    if kind is not None:
                        found.setdefault(kind, node)
        for kind in sorted(found):
            start, end = line_range(found[kind])
            self._references.append(
                pending_reference(
                    EdgeType.REFERENCES,
                    EntityType.SYMBOL,
                    CORE,
                    start,
                    end,
                    role=NESTJS_MARKER,
                    marker=kind,
                )
            )

    def _destructured(self, call: Node) -> list[str] | None:
        """The names ``const { A, B: b } = require(…)`` takes, or ``None`` for any other use.

        Destructured by name, a ``require`` or ``await import()`` takes exactly what an
        import's named list takes. Anything else — a binding of the whole module, a
        rest element, a computed key — can reach every export.
        """
        node = call
        while node.parent is not None and node.parent.type in {
            "await_expression",
            "parenthesized_expression",
        }:
            node = node.parent
        declarator = node.parent
        if (
            declarator is None
            or declarator.type != "variable_declarator"
            or declarator.child_by_field_name("value") != node
        ):
            return None
        pattern = declarator.child_by_field_name("name")
        if pattern is None or pattern.type != "object_pattern":
            return None
        names: list[str] = []
        for child in pattern.named_children:
            if child.type == "comment":
                continue
            if child.type == "shorthand_property_identifier_pattern":
                names.append(node_text(self._source, child))
            elif child.type == "pair_pattern":
                key = child.child_by_field_name("key")
                if key is None or key.type != "property_identifier":
                    return None
                names.append(node_text(self._source, key))
            else:
                return None
        return names

    @staticmethod
    def _marker_kind(imported: str) -> str | None:
        if imported == "RouterModule":
            return ROUTER_MARKER
        if imported in {"ApplicationConfig", "*", "default"}:
            return GLOBAL_MARKER
        return None

    # -- classes ----------------------------------------------------------------------

    def _top_level_classes(self, root: Node) -> Iterator[tuple[str, list[Node], Node]]:
        for statement in root.children:
            decorators: list[Node] = []
            node: Node | None = statement
            if statement.type == "export_statement":
                decorators = [c for c in statement.children if c.type == "decorator"]
                node = statement.child_by_field_name("declaration")
            if node is None or node.type not in {"class_declaration", "abstract_class_declaration"}:
                continue
            decorators.extend(c for c in node.children if c.type == "decorator")
            name = node.child_by_field_name("name")
            if name is not None and decorators:
                yield node_text(self._source, name), decorators, node

    def _class(self, name: str, decorators: list[Node], node: Node) -> None:
        applied = [
            (found, decorator) for decorator in decorators if (found := self._decorator(decorator))
        ]
        controllers = [(call, d) for (nest, call), d in applied if nest == (COMMON, "Controller")]
        modules = [(call, d) for (nest, call), d in applied if nest == (COMMON, "Module")]
        versioned = any(nest == (COMMON, "Version") for (nest, _), _ in applied)
        if len(controllers) == 1 and not modules and not versioned:
            self._controller(name, controllers[0][0], node)
        elif len(modules) == 1 and not controllers:
            self._module(name, *modules[0])

    # -- controllers ------------------------------------------------------------------

    def _controller(self, name: str, call: Node, node: Node) -> None:
        read = self._controller_path(call)
        body = node.child_by_field_name("body")
        if read is None or body is None:
            return
        prefix, version = read
        pending: list[Node] = []
        for child in body.children:
            if child.type == "decorator":
                pending.append(child)
                continue
            if child.type == "comment":
                continue
            if child.type == "method_definition":
                # The TypeScript grammar puts a method's decorators before it in the
                # class body; the JavaScript grammar puts them inside it.
                own = [c for c in child.children if c.type == "decorator"]
                self._handler(name, prefix, version, child, [*pending, *own])
            pending = []

    def _controller_path(self, call: Node) -> tuple[str, Versions | None] | None:
        """``@Controller()``, ``@Controller("p")`` or ``@Controller({ path, version })``.

        ``(path, version)``; the version is ``None`` when the controller states none.
        Any other option — ``host``, ``scope`` — or any value this cannot read makes
        every route of the controller unknowable, so the class yields none.
        """
        arguments = _arguments(call)
        if arguments is None or len(arguments) > 1:
            return None
        if not arguments:
            return "", None
        [argument] = arguments
        if argument.type != "object":
            path = static_string(self._source, argument)
            return (path, None) if path is not None else None
        entries = self._entries(argument)
        if entries is None or not set(entries) <= {"path", "version"}:
            return None
        path = static_string(self._source, entries["path"]) if "path" in entries else ""
        version = self._versions(entries["version"]) if "version" in entries else None
        if path is None or ("version" in entries and version is None):
            return None
        return path, version

    def _versions(self, node: Node) -> Versions | None:
        """``"1"``, ``VERSION_NEUTRAL`` or an array of them; ``None`` when unreadable."""
        node = _unwrap(node)
        if node.type == "array":
            items = [child for child in node.named_children if child.type != "comment"]
            found: list[str | None] = []
            for item in items:
                one = self._versions(item) if _unwrap(item).type != "array" else None
                if one is None:
                    return None
                found.extend(v for v in one if v not in found)
            return tuple(found) if found else None
        if node.type == "identifier":
            neutral = self._nest(node_text(self._source, node)) == (COMMON, "VERSION_NEUTRAL")
            return (None,) if neutral else None
        value = static_string(self._source, node)
        return (value,) if value else None

    def _handler(
        self,
        controller: str,
        prefix: str,
        version: Versions | None,
        method: Node,
        decorators: list[Node],
    ) -> None:
        name_node = method.child_by_field_name("name")
        if name_node is None or name_node.type != "property_identifier":
            return
        if any(child.type in {"static", "get", "set", "*"} for child in method.children):
            return  # accessors, generators and static methods are never handlers
        applied = [(found, d) for d in decorators if (found := self._decorator(d))]
        versions = [call for (nest, call), _ in applied if nest == (COMMON, "Version")]
        if versions:
            # A handler's own version replaces its controller's.
            arguments = _arguments(versions[0])
            if len(versions) > 1 or arguments is None or len(arguments) != 1:
                return
            version = self._versions(arguments[0])
            if version is None:
                return
        stated = {"version": json.dumps(list(version))} if version is not None else {}
        name = node_text(self._source, name_node)
        seen: set[str] = set()
        for (nest, call), decorator in applied:
            if nest[0] != COMMON or nest[1] not in VERBS:
                continue  # `@All`, `@MessagePattern` and the rest are never a route here
            path = self._path_argument(call)
            local = nest_path(prefix, path) if path is not None else None
            if path is None or local is None:
                continue
            verb = VERBS[nest[1]]
            body = http.route_body(verb, local)
            if body in seen:
                continue
            seen.add(body)
            start, end = line_range(decorator)
            self._references.append(
                pending_reference(
                    EdgeType.SERVES_HTTP_ROUTE,
                    EntityType.HTTP_ROUTE,
                    body,
                    start,
                    end,
                    role=NESTJS_ROUTE,
                    source_symbol=f"{controller}.{name}",
                    source="nestjs",
                    provenance=COMPOSED if prefix else EXACT,
                    method=verb,
                    controller=controller,
                    controller_path=prefix,
                    method_path=path,
                    limitation=LIMITATION,
                    **stated,
                )
            )

    def _path_argument(self, call: Node) -> str | None:
        arguments = _arguments(call)
        if arguments is None or len(arguments) > 1:
            return None
        return "" if not arguments else static_string(self._source, arguments[0])

    def _entries(self, node: Node) -> dict[str, Node] | None:
        """An object literal's entries, or ``None`` when any key is not literal."""
        entries: dict[str, Node] = {}
        for child in node.named_children:
            if child.type == "comment":
                continue
            if child.type != "pair":
                return None
            key = child.child_by_field_name("key")
            value = child.child_by_field_name("value")
            if key is None or value is None:
                return None
            if key.type == "property_identifier":
                name = node_text(self._source, key)
            elif key.type == "string" and (literal := string_literal(self._source, key)):
                name = literal
            else:
                return None
            if name in entries:
                return None
            entries[name] = value
        return entries

    # -- modules ----------------------------------------------------------------------

    def _module(self, name: str, call: Node, decorator: Node) -> None:
        start, end = line_range(decorator)
        arguments = _arguments(call) or []
        entries = (
            self._entries(arguments[0])
            if len(arguments) == 1 and arguments[0].type == "object"
            else None
        )
        self._references.append(
            pending_reference(
                EdgeType.REFERENCES,
                EntityType.CLASS,
                name,
                start,
                end,
                role=NESTJS_MODULE,
                source_symbol=name,
                readable="true" if entries is not None else "false",
            )
        )
        if entries is None:
            return
        controllers = entries.get("controllers")
        if controllers is not None and controllers.type == "array":
            for element in controllers.named_children:
                if element.type != "identifier":
                    continue  # never registered as far as this can prove
                target = node_text(self._source, element)
                element_start, element_end = line_range(element)
                self._references.append(
                    pending_reference(
                        EdgeType.REFERENCES,
                        EntityType.CLASS,
                        target,
                        element_start,
                        element_end,
                        role=NESTJS_CONTROLLER,
                        source_symbol=name,
                        **self._binding(target),
                    )
                )
        imports = entries.get("imports")
        if imports is None:
            return
        if imports.type != "array":
            self._import_entry(name, imports, UNKNOWN)
            return
        for element in imports.named_children:
            if element.type != "comment":
                self._import_entry(name, element)

    def _import_entry(self, module: str, element: Node, kind: str | None = None) -> None:
        start, end = line_range(element)
        target = node_text(self._source, element)[:_MAX_TARGET]
        meta: dict[str, str] = {}
        if kind is None:
            kind, target, meta = self._classify(element, target)
        self._references.append(
            pending_reference(
                EdgeType.REFERENCES,
                EntityType.CLASS,
                target,
                start,
                end,
                role=NESTJS_IMPORT,
                source_symbol=module,
                kind=kind,
                **meta,
            )
        )

    def _classify(self, element: Node, target: str) -> tuple[str, str, dict[str, str]]:
        """``(kind, target, meta)`` for one ``imports`` entry. Nothing is dropped."""
        forwarded = self._forward_ref(element)
        if forwarded is not None:
            element = forwarded
        if element.type == "identifier":
            name = node_text(self._source, element)
            meta = self._binding(name)
            if meta or name not in self._consts or self._counts[name] != 1:
                return MODULE, name, meta
            # `const CORE = CoreModule.forRoot()`, listed as `imports: [CORE]`.
            value = _unwrap(self._consts[name])
            if value.type != "call_expression":
                return UNKNOWN, name, {}
            return DYNAMIC, name, {**self._dynamic(value), "const": name}
        if element.type == "call_expression":
            callee = element.child_by_field_name("function")
            root = self._callee_root(callee) if callee is not None else None
            if root is not None and self._nest(root) == (CORE, "RouterModule"):
                return ROUTER, target, {}
            return DYNAMIC, target, self._dynamic(element)
        return UNKNOWN, target, {}

    def _forward_ref(self, element: Node) -> Node | None:
        """``forwardRef(() => X)`` names ``X`` exactly as ``X`` would."""
        if element.type != "call_expression":
            return None
        callee = element.child_by_field_name("function")
        if callee is None or callee.type != "identifier":
            return None
        if self._nest(node_text(self._source, callee)) != (COMMON, "forwardRef"):
            return None
        arguments = _arguments(element) or []
        if len(arguments) != 1 or arguments[0].type != "arrow_function":
            return None
        body = arguments[0].child_by_field_name("body")
        body = _unwrap(body) if body is not None else None
        return body if body is not None and body.type == "identifier" else None

    def _callee_root(self, callee: Node) -> str | None:
        node = _unwrap(callee)
        while node.type == "member_expression":
            inner = node.child_by_field_name("object")
            if inner is None:
                return None
            node = _unwrap(inner)
        return node_text(self._source, node) if node.type == "identifier" else None

    def _dynamic(self, call: Node) -> dict[str, str]:
        """A dynamic module: where its callee comes from, and which names it is handed."""
        callee = call.child_by_field_name("function")
        root = self._callee_root(callee) if callee is not None else None
        meta: dict[str, str] = {}
        if root is not None:
            meta = {f"callee_{key}": value for key, value in self._binding(root).items()}
            meta["callee"] = root
        handed: dict[str, dict[str, str]] = {}
        arguments = call.child_by_field_name("arguments")
        for node in walk(arguments) if arguments is not None else ():
            if len(handed) >= _MAX_ARGUMENT_NAMES:
                meta["arguments_truncated"] = "true"
                break
            if node.type not in {"identifier", "shorthand_property_identifier"}:
                continue
            name = node_text(self._source, node)
            binding = self._binding(name)
            if binding and name not in handed:
                handed[name] = binding
        meta["arguments"] = json.dumps(handed, sort_keys=True)
        return meta

    # -- application roots ------------------------------------------------------------

    def _find_roots(self, root: Node) -> None:
        for node in walk(root):
            if node.type != "call_expression":
                continue
            callee = node.child_by_field_name("function")
            callee = _unwrap(callee) if callee is not None else None
            if callee is None or callee.type != "member_expression":
                continue
            owner = callee.child_by_field_name("object")
            prop = callee.child_by_field_name("property")
            if (
                owner is None
                or prop is None
                or owner.type != "identifier"
                or node_text(self._source, prop) != "create"
                or self._nest(node_text(self._source, owner)) != (CORE, "NestFactory")
            ):
                continue
            self._roots[(node.start_byte, node.end_byte)] = self._root(node)

    def _root(self, call: Node) -> _Root:
        start, end = line_range(call)
        arguments = _arguments(call) or []
        meta: dict[str, str] = {}
        if arguments and arguments[0].type == "identifier":
            target = node_text(self._source, arguments[0])
            meta = {"kind": MODULE, **self._binding(target)}
        else:
            target = node_text(self._source, arguments[0])[:_MAX_TARGET] if arguments else ""
            meta = {"kind": DYNAMIC}
        reason: str | None = None
        prefix = ""
        versioning: str | None = None
        if not arguments or len(arguments) > 2:
            reason = "arguments"
        elif len(arguments) == 2 and arguments[1].type != "object":
            reason = "adapter"
        else:
            application = self._application(call)
            if application is None:
                reason = "unbound application"
            else:
                reason, prefix, versioning = self._prefix(*application)
        if reason is None:
            meta.update(prefix_status=PROVEN, prefix=prefix)
            if versioning is not None:
                meta["versioning"] = versioning
        else:
            meta.update(prefix_status=UNKNOWN, reason=reason)
        return _Root(target, meta, start, end)

    def _application(self, call: Node) -> tuple[str, Node] | None:
        """The ``const`` the application is bound to, bound nowhere else."""
        node = call
        while node.parent is not None and node.parent.type in {
            "await_expression",
            "parenthesized_expression",
            "as_expression",
            "satisfies_expression",
            "non_null_expression",
        }:
            node = node.parent
        declarator = node.parent
        if declarator is None or declarator.type != "variable_declarator":
            return None
        declaration = declarator.parent
        kind = declaration.child_by_field_name("kind") if declaration is not None else None
        name = declarator.child_by_field_name("name")
        if (
            kind is None
            or node_text(self._source, kind) != "const"
            or name is None
            or name.type != "identifier"
        ):
            return None
        local = node_text(self._source, name)
        if self._counts[local] != 1:
            return None
        return local, name

    def _prefix(self, application: str, binding: Node) -> tuple[str | None, str, str | None]:
        """``(reason, prefix, versioning)``: the prefix and URI versioning, or why not known.

        Every use of the application must be a method call on it, or an argument to
        ``SwaggerModule.createDocument`` or ``SwaggerModule.setup``, which document
        it. Anything else — an argument, a return, a property read, a shorthand —
        hands it to code that could set a prefix or version routes, so the prefix is
        unknown. Versioning is ``None`` when the application enables none, else the
        JSON of :meth:`_versioning`.
        """
        root = binding
        while root.parent is not None:
            root = root.parent
        prefixes: list[str | None] = []
        versionings: list[dict[str, object] | None] = []
        for node in walk(root):
            if node.type not in {"identifier", "shorthand_property_identifier"}:
                continue
            if node_text(self._source, node) != application or node == binding:
                continue
            if node.type == "identifier" and self._documented(node):
                continue
            member = node.parent
            call = member.parent if member is not None else None
            if (
                node.type != "identifier"
                or member is None
                or member.type != "member_expression"
                or member.child_by_field_name("object") != node
                or call is None
                or call.type != "call_expression"
                or call.child_by_field_name("function") != member
            ):
                return "application escapes", "", None
            prop = member.child_by_field_name("property")
            method = node_text(self._source, prop) if prop is not None else ""
            if method == "enableVersioning":
                versionings.append(self._versioning(call))
            if method == "setGlobalPrefix":
                arguments = _arguments(call) or []
                value = static_string(self._source, arguments[0]) if len(arguments) == 1 else None
                prefixes.append(value)
        if len(versionings) > 1 or None in versionings:
            return "versioning", "", None
        versioning = json.dumps(versionings[0], sort_keys=True) if versionings else None
        if not prefixes:
            return None, "", versioning
        if len(prefixes) > 1 or prefixes[0] is None:
            return "global prefix", "", None
        value = prefixes[0]
        if any(char in _PATTERN for char in value):
            return "global prefix", "", None
        return None, value, versioning

    def _documented(self, node: Node) -> bool:
        """Whether ``node`` is an argument to ``SwaggerModule.createDocument`` or ``.setup``.

        ``SwaggerModule`` must be a named import from ``@nestjs/swagger``, bound once.
        """
        arguments = node.parent
        call = arguments.parent if arguments is not None else None
        if arguments is None or arguments.type != "arguments" or call is None:
            return False
        callee = call.child_by_field_name("function") if call.type == "call_expression" else None
        if callee is None or callee.type != "member_expression":
            return False
        owner = callee.child_by_field_name("object")
        prop = callee.child_by_field_name("property")
        if owner is None or prop is None or owner.type != "identifier":
            return False
        local = node_text(self._source, owner)
        binding = self._imports.get(local)
        return (
            binding is not None
            and (binding.specifier, binding.imported) == (SWAGGER, "SwaggerModule")
            and self._counts[local] == 1
            and node_text(self._source, prop) in _DOCUMENTING
        )

    def _versioning(self, call: Node) -> dict[str, object] | None:
        """``enableVersioning``'s URI versioning, or ``None``.

        ``{"prefix", "default"}``: what goes before each version in the URI, and the
        versions of a route that states none (``None`` when there are none). Any
        other type routes by a header, a media type or code, which no path names;
        that, or an option this cannot read, is ``None``.
        """
        arguments = _arguments(call)
        if arguments is None or len(arguments) > 1:
            return None
        if not arguments:
            return {"prefix": VERSION_PREFIX, "default": None}  # URI is the default type
        options = _unwrap(arguments[0])
        entries = self._entries(options) if options.type == "object" else None
        if entries is None or "type" not in entries:
            return None
        if not set(entries) <= {"type", "prefix", "defaultVersion"}:
            return None
        kind = _unwrap(entries["type"])
        owner = kind.child_by_field_name("object") if kind.type == "member_expression" else None
        prop = kind.child_by_field_name("property") if kind.type == "member_expression" else None
        if (
            owner is None
            or prop is None
            or owner.type != "identifier"
            or self._nest(node_text(self._source, owner)) != (COMMON, "VersioningType")
            or node_text(self._source, prop) != "URI"
        ):
            return None
        prefix: str | None = VERSION_PREFIX
        if "prefix" in entries:
            value = _unwrap(entries["prefix"])
            prefix = "" if value.type == "false" else static_string(self._source, value)
        default: Versions | None = None
        if "defaultVersion" in entries:
            default = self._versions(entries["defaultVersion"])
            if default is None:
                return None
        if prefix is None:
            return None
        return {"prefix": prefix, "default": list(default) if default is not None else None}
