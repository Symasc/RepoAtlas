"""Fastify routes in TypeScript and JavaScript source (HTTP linkage).

The rule of :mod:`repoatlas.parsers.http_typescript` applied to Fastify: nothing is
matched by method name. A receiver counts only when the source proves what it is:

* **an application** -- ``Fastify()`` or ``fastify()``, the factory imported from
  ``"fastify"`` (a default or named ``fastify`` import, or ``require``), assigned
  to a module-level ``const``. The import and the ``const`` are each bound once in the
  module's own scope: a parameter or a nested function binding the same name is no
  second binding of either, a true second binding in the module's own scope refuses it;
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

**A factory** is a module-level function bound once that hands back an application it
builds: its body's top level holds exactly one ``const app = Fastify(...)`` bound once in
its own scope (the body holds no other ``Fastify(...)`` call), every ``return`` in its own
body returns ``app`` (there is at least one) and its last statement is a ``return``; or its
body is ``return Fastify(...)``, or its expression is ``Fastify(...)``. Returning
``{ app }``, returning something else on a branch, ending without a ``return``, or building
two applications is no factory. Each is a :data:`~repoatlas.parsers.http_facts.FASTIFY_SCOPE`
claim of kind :data:`FACTORY`, with the scope id ``F()``.

**An instance** is ``const x = F()`` (or ``await F()``), bound once in the scope that holds
it, at the module's level or at the top level of an entry function, where ``F`` is a factory
of this file, a name imported from a relative module, or ``ns.F`` of a relative namespace
import (each import bound once in the module's own scope). **An entry
application** is ``const x = Fastify()`` at the top level of a module-level function ``G``
that is no factory, proven where the file calls ``G`` unconditionally at its top level
(``G();``, ``void G();``, ``await G();``, ``G().catch(...)``), or calls ``G().then()`` as the
only statement of a braced ``if (require.main === module) { ... }`` with both globals
unshadowed. That form
is exact: ``===`` with ``require.main`` on the left and ``module`` on the right, a bare
module-level ``G`` called with no arguments, and ``.then()`` with no arguments and no ``await``,
parentheses, optional chaining or ``.catch(...)`` around it. A guard with an ``else`` still
proves ``G``, which runs whenever the file is the main module. A ``.mjs`` or ``.mts`` file is an
ES module and refuses the guard; a ``.js`` file made one by ``"type": "module"`` in its
``package.json`` is not told apart (a known gap). Other conditionals do not prove an entry.
Each is a :data:`~repoatlas.parsers.http_facts.FASTIFY_INSTANCE` claim, rooted at its own file;
the claim names the factory (the global pass decides an imported one) or the refusal code that
says why this parser would not trust it. ``const y = x`` in the same scope, after ``x``,
makes ``y`` a receiver as well, for at most :data:`_MAX_ALIAS_HOPS` hops. A claim is recorded
only for a receiver (or an alias of it) that the file uses: as the receiver of a route verb,
``route``, ``register``, ``listen`` or ``ready``, or as an argument of a call of a function of
this file or of a relative module (``f(x)``, ``ns.f(x)`` on a relative namespace import).

**Tracing.** An instance, an entry application and a factory's own application are
receivers on a straight line only: a route, a registration or a helper call on one counts
when its statement sits directly in that receiver's own body (the module's, the entry
function's or the factory's) as ``call;``, ``await call;``, ``void call;``, ``return call;`` or
``const r = [await] call;``. The same use in a nested function, a callback, a condition, a
loop or a ``try`` is no route and no registration, and a helper call there is recorded
with the refusal :data:`HELPER_CONTROL_FLOW`. A plugin's first parameter keeps its rule for
routes and registrations (anywhere in its body); its helper calls need the straight line. The
module's own application (``const app = Fastify()``) keeps its routes and registrations as
they were; a helper call that hands it on is recorded on the same straight line of the
program, from the end of its declarator.

A route on such a receiver is a :data:`~repoatlas.parsers.http_facts.FASTIFY_TRACED_ROUTE`
claim -- never served here: the same ``method``, ``path`` and ``provenance`` as a
:data:`~repoatlas.parsers.http_facts.FASTIFY_ROUTE`, the receiver's scope id in ``scope``
and its kind in ``owner_kind``, and the declaration that holds the call as its source. A
call that hands a receiver to a function is a
:data:`~repoatlas.parsers.http_facts.FASTIFY_HELPER_CALL` claim per receiver argument (at
most :data:`MAX_ARGUMENTS` are looked at): the target key is the callee as written, ``owner``
and ``owner_kind`` the receiver's scope, ``receiver`` its name there, ``arg`` the index of the
argument, ``local`` (a function of this file) or ``module`` and ``name`` (a relative import,
``default`` for a default import, the member of a relative namespace import) the target when
there is one, and ``refusal`` the first code that applies: :data:`HELPER_CONTROL_FLOW`,
:data:`HELPER_DYNAMIC`, :data:`HELPER_CALLEE_UNSUPPORTED`, :data:`HELPER_SHADOWED`,
:data:`HELPER_ARGUMENT_UNSUPPORTED`, :data:`RECEIVER_BEFORE_BINDING`,
:data:`CALLEE_BEFORE_BINDING`, :data:`AFTER_LISTEN`. Which parameter of the target is the
receiver, and how deep a chain goes, are for the global pass.

A file that imports neither ``fastify`` nor ``fastify-plugin`` is read only when it has an
instance claim, and then records only the claims that trace a receiver -- instances, helper
calls, traced routes and, for each plugin function they name, a
:data:`~repoatlas.parsers.http_facts.FASTIFY_TRACED_SCOPE` claim (``kind``, ``exports``, and
``receiver``, the name of the function's first parameter) -- besides the registrations an
instance makes. A route there is always a traced one, and a registration made on a plugin's
server is not recorded. In a file that imports fastify the claims of an application and of
a plugin are the same as without any tracing.

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
    FASTIFY_HELPER_CALL,
    FASTIFY_INSTANCE,
    FASTIFY_REGISTER,
    FASTIFY_ROUTE,
    FASTIFY_SCOPE,
    FASTIFY_TRACED_ROUTE,
    FASTIFY_TRACED_SCOPE,
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
    from collections.abc import Iterator

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
#: A function that returns an application it builds (scope id ``F()``).
FACTORY = "factory"

#: What a ``fastify-instance`` claim is: ``const x = F()``, or ``const x = Fastify()`` at the
#: top of an entry function.
INSTANCE = "instance"
ENTRY = "entry"
#: Whether the parser proved the factory or the entry (``proven``), leaves an imported
#: factory to the global pass (``pending``), or refused (``refused``, with a code).
PROVEN = "proven"
PENDING = "pending"
REFUSED = "refused"

#: Why an instance is not trusted. Stable codes: the global pass and the tests spell them.
FACTORY_UNPROVEN = "fastify-factory-unproven"
FACTORY_NOT_AWAITED = "fastify-factory-not-awaited"
RECEIVER_BEFORE_BINDING = "fastify-receiver-before-binding"
CALLEE_BEFORE_BINDING = "fastify-callee-before-binding"
ENTRY_NOT_CALLED = "fastify-entry-not-called"

#: Why a helper call is not trusted. The first that applies is recorded, in this order:
#: control flow, dynamic, callee unsupported, shadowed, argument unsupported, then
#: :data:`RECEIVER_BEFORE_BINDING`, :data:`CALLEE_BEFORE_BINDING` and after listen.
HELPER_CONTROL_FLOW = "fastify-helper-control-flow"
HELPER_DYNAMIC = "fastify-helper-dynamic"
HELPER_CALLEE_UNSUPPORTED = "fastify-helper-callee-unsupported"
HELPER_SHADOWED = "fastify-helper-shadowed"
HELPER_ARGUMENT_UNSUPPORTED = "fastify-helper-argument-unsupported"
AFTER_LISTEN = "fastify-after-listen"

_FUNCTIONS = FUNCTION_NODES
_DECLARATIONS = frozenset({"function_declaration", "generator_function_declaration"})
_FUNCTION_VALUES = frozenset({"arrow_function", "function_expression", "function"})
_TRANSPARENT = frozenset({"parenthesized_expression", "await_expression", "as_expression"})

#: File suffixes that are always ES modules, where ``require`` and ``module`` do not exist.
ES_MODULE_SUFFIXES = frozenset({".mjs", ".mts"})

#: Where a name is bound, when no single function binds it.
_MODULE = "module"
_AMBIGUOUS = "ambiguous"

#: The scope id of an anonymous default-exported plugin: no identifier can spell it.
DEFAULT_SCOPE = "*default"

#: Constants longer than this, or holding whitespace, are no path or URL part.
_MAX_CONSTANT = 256
#: How many constants one module offers other modules.
_MAX_CONSTANTS = 64
#: How many instances one file states, in the order the source declares them.
_MAX_INSTANCES = 64
#: How many arguments of one call are read; the rest are not looked at.
MAX_ARGUMENTS = 8
#: How many ``const y = x`` hops lead from an instance to a receiver.
_MAX_ALIAS_HOPS = 4
_IDENTIFIERS = frozenset({"identifier", "shorthand_property_identifier"})
#: How many helper calls, and how many traced routes, one file states.
_MAX_HELPER_CALLS = 256
_MAX_TRACED_ROUTES = 256

#: The receivers whose routes are traced ones: never an application or a plugin of this file.
_TRACED_KINDS = frozenset({INSTANCE, ENTRY, FACTORY})
#: What makes a use of an instance: ``x.<one of these>(...)`` is a route, a registration, or
#: the application starting up.
_USES = frozenset({*_VERBS, "route", "register", "listen", "ready"})
#: ``f.call(...)``, ``f.apply(...)`` and ``f.bind(...)`` hand a receiver on through another call.
_REBINDING = frozenset({"call", "apply", "bind"})
#: What can hold a receiver inside an argument.
_HOLDERS = frozenset({"object", "array", "spread_element"})
_MAX_HOLDING_DEPTH = 4
#: What a use of a receiver may sit in on its way up to the argument it is part of.
_CARRIERS = _TRANSPARENT | _HOLDERS | {"pair"}

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


def _optional(node: Node) -> bool:
    """Whether a member or call expression chains optionally (``?.``)."""
    return any(child.type in {"optional_chain", "?."} for child in node.children)


def _scanned(node: Node) -> list[Node]:
    """The arguments of a call that are looked at: the first :data:`MAX_ARGUMENTS`."""
    return (_arguments(node) or [])[:MAX_ARGUMENTS]


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
    #: A factory that is ``async``: its caller must ``await`` it.
    is_async: bool = False
    #: The function a plugin scope is, when this file declares it.
    node: Node | None = None


@dataclass(slots=True)
class _Held:
    """A receiver of a kind M8 adds -- an instance, an entry application, or the application a
    factory builds -- with the body that runs it and the names that are it."""

    scope: _Scope
    #: The program, or the body of the function that holds the receiver.
    body: Node
    #: The entry function that runs ``body``; empty at the module's level and in a factory.
    entry: str
    #: Each name that is the receiver, and where its binding ends.
    ready: dict[str, int]


@dataclass(slots=True)
class _Function:
    """A module-level function bound once: what a factory or an entry is made of."""

    node: Node
    line: int
    #: Where its binding ends. A declaration is hoisted, so it is ready from the start (0).
    ready: int
    is_async: bool
    factory: bool = False


def _module_statements(root: Node) -> Iterator[Node]:
    """The module's statements, an ``export`` replaced by what it declares."""
    for statement in root.named_children:
        if statement.type == "export_statement":
            declaration = statement.child_by_field_name("declaration")
            if declaration is None:
                continue
            statement = declaration
        yield statement


def _unwrapped(node: Node) -> tuple[Node, bool]:
    """``node`` without its parentheses, casts and ``await``, and whether it held an ``await``."""
    awaited = False
    while node.type in _TRANSPARENT:
        inner = [child for child in node.named_children if child.type != "comment"]
        if not inner:
            break
        awaited = awaited or node.type == "await_expression"
        node = inner[0]
    return node, awaited


def _returned(statement: Node) -> Node | None:
    inner = [child for child in statement.named_children if child.type != "comment"]
    return _operand(inner[0]) if inner else None


def _own_returns(body: Node) -> list[Node]:
    """The ``return`` statements of a function's own body, never of a nested function."""
    found: list[Node] = []
    stack = [body]
    while stack:
        node = stack.pop()
        if node.type in _FUNCTIONS:
            continue
        if node.type == "return_statement":
            found.append(node)
        stack.extend(node.children)
    return found


class FastifyDeclarations:
    """What one file proves about Fastify, and the facts and claims that follow."""

    def __init__(
        self, source: bytes, root: Node, counts: Counter[str], *, es_module: bool = False
    ) -> None:
        self._source = source
        #: The file is an ES module by its suffix: it has no ``require`` or ``module``.
        self._es_module = es_module
        self._program = root
        self._counts = counts
        self._factories: set[str] = set()
        self._wrappers: set[str] = set()
        #: Local name → ``(relative specifier, imported name)``, each bound once.
        self._imported: dict[str, tuple[str, str]] = {}
        #: Local name → relative specifier of a namespace import, each bound once.
        self._namespaces: dict[str, str] = {}
        #: Module-level functions bound once, and the factory scopes among them, by name.
        self._functions: dict[str, _Function] = {}
        self._factory_scopes: dict[str, _Scope] = {}
        #: Where the module's top level calls each function unconditionally: start bytes.
        self._called: dict[str, list[int]] = {}
        #: Identifier uses by name and start byte, within one scope, never a nested function.
        self._uses: dict[_Key, dict[str, list[int]]] = {}
        self._instances: list[ParsedReference] = []
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
        #: Receivers M8 adds, by the function that binds the name (``None``: the module).
        self._held: dict[tuple[_Key | None, str], _Held] = {}
        #: Every identifier of the file by name, built once when a candidate needs it.
        self._names: dict[str, list[Node]] | None = None
        #: The ``const y = x`` declarations of a scope's top level, by scope.
        self._alias_sites: dict[_Key | None, list[tuple[Node, Node, str]]] = {}
        self._alias_names: set[str] | None = None
        self._module_bound: Counter[str] | None = None
        #: Where a body awaits ``x.listen(...)`` or ``x.ready(...)``: end byte and receiver name.
        self._listens: dict[_Key, list[tuple[int, str]]] = {}
        #: The plugin scopes the claims of a traced file name, by the order they are named.
        self._touched: dict[_Key, _Scope] = {}
        self._helper_total = 0
        self._traced_total = 0
        self._reads = False
        self._top_level(root)
        self._called = self._top_level_calls(root)
        self._instances = self._instance_claims(root)
        self._names = None
        #: A file without fastify is read, for the M8 roles only, when it uses an instance.
        self._traced = not self._reads and bool(self._instances)
        if self._reads or self._traced:
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
        self._find_factories(root)
        for local, exported, wrapped in aliases:
            scope = self._named.get(local)
            if scope is not None:
                scope.exports.append((exported, wrapped))
            elif local in self._constants and not wrapped:
                self._exported_constants[exported] = local
            factory = self._factory_scopes.get(local)
            if factory is not None and not wrapped:
                factory.exports.append((exported, False))

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
                if module == "fastify-plugin":
                    if self._counts[local] == 1:
                        self._wrappers.add(local)
                elif self._bound_once_in_module(local):
                    if module == "fastify":
                        self._factories.add(local)
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
                    if not self._bound_once_in_module(local):
                        continue
                    if module == "fastify" and name == "fastify":
                        self._factories.add(local)
                    elif module.startswith("."):
                        self._imported[local] = (module, name)
            elif child.type == "namespace_import" and module.startswith("."):
                bound = next((c for c in child.named_children if c.type == "identifier"), None)
                if bound is not None and self._bound_once_in_module(node_text(self._source, bound)):
                    self._namespaces[node_text(self._source, bound)] = module

    def _bound_once_in_module(self, name: str) -> bool:
        """An import's local name is judged by the module's own scope: a parameter or a nested
        function binding the same name is no second binding of the import."""
        return self._module_bindings()[name] == 1

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
            line = line_range(declarator)[0]
            if self._counts[name] != 1:
                # Only an application, or the required factory, is judged by the module's own
                # scope: a parameter or a nested function binding the same name is no second
                # binding of it here.
                if self._module_bindings()[name] == 1:
                    value = _operand(value)
                    if not self._application_value(value, name, line):
                        self._required_factory(value, name)
                continue
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
        if self._application_value(value, name, line):
            return
        if self._required_factory(value, name):
            return
        callee = value.child_by_field_name("function")
        arguments = _arguments(value) or []
        if callee is None or callee.type != "identifier":
            return
        if node_text(self._source, callee) in self._wrappers and len(arguments) >= 1:
            inner = _operand(arguments[0])
            if inner.type in _FUNCTION_VALUES:
                self._plugin(inner, name, line, wrapped=True)

    def _required_factory(self, value: Node, name: str) -> bool:
        """Record ``name`` as the factory when ``value`` is ``require("fastify")``. Whether the
        name is bound once is the caller's."""
        if value.type != "call_expression":
            return False
        callee = value.child_by_field_name("function")
        if (
            callee is None
            or callee.type != "identifier"
            or node_text(self._source, callee) != "require"
            or self._required(_arguments(value) or []) != "fastify"
        ):
            return False
        self._factories.add(name)
        self._reads = True
        return True

    def _application_value(self, value: Node, name: str, line: int) -> bool:
        """Record ``name`` as an application when ``value`` builds one: ``Fastify(...)`` or
        ``require("fastify")(options)``. Whether the name is bound once is the caller's."""
        if value.type != "call_expression":
            return False
        callee = value.child_by_field_name("function")
        if callee is None:
            return False
        if callee.type == "identifier":
            if node_text(self._source, callee) not in self._factories:
                return False
        elif callee.type == "call_expression":
            # `require("fastify")(options)`: an application straight from the factory.
            inner = callee.child_by_field_name("function")
            if not (
                inner is not None
                and inner.type == "identifier"
                and node_text(self._source, inner) == "require"
                and self._required(_arguments(callee) or []) == "fastify"
            ):
                return False
            self._reads = True
        else:
            return False
        self._apps[name] = self._scope(value, name, APP, line)
        return True

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
        scope = _Scope(scope_id, kind, line, node=node)
        self._scopes[_key(node)] = scope
        return scope

    def _touch(self, scope: _Scope) -> None:
        """Remember that a traced file's claims name this plugin scope: it is then stated."""
        if self._traced and scope.node is not None:
            self._touched.setdefault(_key(scope.node), scope)

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

    def _resolve(self, at: Node, name: str) -> tuple[_Scope, _Held | None] | None:
        """The receiver ``name`` is where ``at`` uses it, and what holds it when it is of a
        kind M8 adds (an instance, an entry application or a factory's own application)."""
        bound = self._enclosing_binding(at, name)
        if isinstance(bound, str):
            if bound != _MODULE:
                return None
            app = self._apps.get(name)
            if app is not None:
                return (app, None) if self._module_bindings()[name] == 1 else None
            if self._module_bindings()[name] != 1:
                return None  # a parameter of another function is not a second binding here
            held = self._held.get((None, name))
            return (held.scope, held) if held is not None else None
        if _first_parameter(self._source, bound) == name:
            scope = self._scopes.get(_key(bound))
            return (scope, None) if scope is not None else None
        held = self._held.get((_key(bound), name))
        return (held.scope, held) if held is not None else None

    def _receiver(self, at: Node, name: str) -> _Scope | None:
        """The application or plugin server ``name`` is where ``at`` uses it, or the instance,
        entry application or factory application it is, when ``at`` is a straight-line call
        in that receiver's own body."""
        found = self._resolve(at, name)
        if found is None:
            return None
        scope, held = found
        if held is not None and not self._directly_in(at, held.body):
            return None
        return scope

    def _is_void(self, node: Node) -> bool:
        if node.type != "unary_expression":
            return False
        operator = node.child_by_field_name("operator")
        return operator is not None and node_text(self._source, operator) == "void"

    def _directly_in(self, call: Node, body: Node) -> bool:
        """Whether the statement holding ``call`` sits in ``body`` itself, in a form that runs
        it whatever else happens: ``call;``, ``await call;``, ``void call;``, ``return call;``
        or ``const r = [await] call;``. An arrow function's expression body is the call."""
        node = call
        while True:
            parent = node.parent
            if parent is None:
                return False
            if parent.type in {"await_expression", "parenthesized_expression"} or self._is_void(
                parent
            ):
                node = parent
            else:
                break
        if _key(node) == _key(body):
            return True
        if parent.type == "variable_declarator":
            name = parent.child_by_field_name("name")
            value = parent.child_by_field_name("value")
            declaration = parent.parent
            kind = declaration.child_by_field_name("kind") if declaration is not None else None
            if (
                name is None
                or name.type != "identifier"
                or value is None
                or _key(value) != _key(node)
                or declaration is None
                or declaration.type != "lexical_declaration"
                or kind is None
                or node_text(self._source, kind) != "const"
            ):
                return False
            parent = declaration
        elif parent.type not in {"expression_statement", "return_statement"}:
            return False
        holder = parent.parent
        if holder is not None and holder.type == "export_statement":
            holder = holder.parent
        return holder is not None and _key(holder) == _key(body)

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
            if self._traced and owner.kind == PLUGIN:
                continue  # a file without fastify states the registrations of instances only
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
            self._touch(scope)
            return {"local": scope.id}, wrapped
        if value.type != "identifier":
            return None
        name = node_text(self._source, value)
        if not self._module_name(at, name):
            return None
        if name in self._named:
            self._touch(self._named[name])
            return {"local": name}, wrapped or self._named[name].wrapped
        if name in self._imported:
            module, imported = self._imported[name]
            return {"module": module, "name": imported}, wrapped
        return None

    # -- factories and instances -------------------------------------------------------

    def _fastify_call(self, node: Node) -> bool:
        """Whether ``node`` is ``Fastify(...)``: the factory this file imported from fastify."""
        if node.type != "call_expression":
            return False
        callee = node.child_by_field_name("function")
        if callee is None:
            return False
        if callee.type == "identifier":
            # the module's binding, not a parameter or a local of the same name where it is called
            name = node_text(self._source, callee)
            return name in self._factories and self._binding_function(callee, name) is None
        if callee.type == "call_expression":
            inner = callee.child_by_field_name("function")
            return (
                inner is not None
                and inner.type == "identifier"
                and node_text(self._source, inner) == "require"
                and self._binding_function(inner, "require") is None
                and self._required(_scanned(callee)) == "fastify"
            )
        return False

    def _const_declarators(self, statement: Node) -> list[tuple[Node, Node, Node]]:
        """``(declarator, name, value)`` of each ``const name = value`` of a statement."""
        if statement.type != "lexical_declaration":
            return []
        kind = statement.child_by_field_name("kind")
        if kind is None or node_text(self._source, kind) != "const":
            return []
        found: list[tuple[Node, Node, Node]] = []
        for declarator in statement.named_children:
            if declarator.type != "variable_declarator":
                continue
            name_node = declarator.child_by_field_name("name")
            value = declarator.child_by_field_name("value")
            if name_node is not None and name_node.type == "identifier" and value is not None:
                found.append((declarator, name_node, value))
        return found

    def _find_factories(self, root: Node) -> None:
        """The module-level functions bound once, and which of them are factories."""
        for statement in _module_statements(root):
            if statement.type == "function_declaration":
                name_node = statement.child_by_field_name("name")
                if name_node is not None:
                    name = node_text(self._source, name_node)
                    self._remember(name, statement, 0, line_range(statement)[0])
                continue
            for declarator, name_node, value in self._const_declarators(statement):
                function = _operand(value)
                if function.type in _FUNCTION_VALUES:
                    name = node_text(self._source, name_node)
                    self._remember(name, function, declarator.end_byte, line_range(declarator)[0])

    def _remember(self, name: str, node: Node, ready: int, line: int) -> None:
        if self._counts[name] != 1:
            return
        function = _Function(
            node, line, ready, is_async=any(child.type == "async" for child in node.children)
        )
        function.factory = self._reads and self._is_factory(node)
        self._functions[name] = function
        if function.factory:
            scope = _Scope(f"{name}()", FACTORY, line, is_async=function.is_async)
            self._factory_scopes[name] = scope
            built = self._application(node)
            body = node.child_by_field_name("body")
            if built is not None and body is not None:
                self._held[(_key(node), built[0])] = _Held(scope, body, "", {built[0]: built[1]})

    def _application(self, function: Node) -> tuple[str, int] | None:
        """The name a factory binds its application to, and where that binding ends, if the
        factory binds it to a name at all."""
        body = function.child_by_field_name("body")
        if body is None or body.type != "statement_block":
            return None
        for statement in body.named_children:
            for declarator, name_node, value in self._const_declarators(statement):
                if self._fastify_call(_operand(value)):
                    name = node_text(self._source, name_node)
                    if self._own_counts(function)[name] != 1:
                        return None
                    return name, declarator.end_byte
        return None

    def _is_factory(self, function: Node) -> bool:
        """Whether every call of ``function`` hands back the one application it builds."""
        body = function.child_by_field_name("body")
        if body is None:
            return False
        calls = [node for node in walk(body) if self._fastify_call(node)]
        if len(calls) != 1:
            return False
        built = _key(calls[0])
        if body.type != "statement_block":
            return _key(_operand(body)) == built
        statements = [child for child in body.named_children if child.type != "comment"]
        if len(statements) == 1 and statements[0].type == "return_statement":
            value = _returned(statements[0])
            return value is not None and _key(value) == built
        app = self._built_application(function, statements, built)
        if app is None:
            return False
        # every return hands back the application, and the body ends in one: no path falls
        # off the end and returns nothing
        last = statements[-1]
        if last.type != "return_statement":
            return False
        returns = _own_returns(body)
        for statement in returns:
            value = _returned(statement)
            if value is None or value.type != "identifier":
                return False
            if node_text(self._source, value) != app:
                return False
        return bool(returns)

    def _built_application(self, function: Node, statements: list[Node], built: _Key) -> str | None:
        """The name a top-level ``const`` of the body binds the one ``Fastify(...)`` to."""
        for statement in statements:
            for _declarator, name_node, value in self._const_declarators(statement):
                if _key(_operand(value)) == built:
                    name = node_text(self._source, name_node)
                    return name if self._own_counts(function)[name] == 1 else None
        return None

    def _top_level_calls(self, root: Node) -> dict[str, list[int]]:
        """Where the module's top level calls a function whatever happens: ``G();``,
        ``void G();``, ``await G();``, ``G().catch(...)``, or ``main().then()`` guarded by
        ``require.main === module``. Other calls under a condition, loop, callback or block
        are not evidence that the entry runs."""
        found: dict[str, list[int]] = {}
        for statement in root.named_children:
            if statement.type == "if_statement":
                guarded = self._commonjs_entry_call(statement)
                if guarded is not None:
                    name, at = guarded
                    found.setdefault(name, []).append(at)
                continue
            if statement.type != "expression_statement":
                continue
            inner = [child for child in statement.named_children if child.type != "comment"]
            if len(inner) != 1:
                continue
            node = _operand(inner[0])
            if node.type == "unary_expression":
                operator = node.child_by_field_name("operator")
                argument = node.child_by_field_name("argument")
                if (
                    operator is None
                    or argument is None
                    or node_text(self._source, operator) != "void"
                ):
                    continue
                node = _operand(argument)
            name = self._called_name(node)
            if name is not None:
                found.setdefault(name, []).append(statement.start_byte)
        return found

    def _commonjs_entry_call(self, statement: Node) -> tuple[str, int] | None:
        """The one guarded entry form this reader proves: ``if (require.main === module) {
        G().then(); }``. Both names must be unshadowed module globals in a file that is no ES
        module, and the body must hold only that call: no ``await``, parentheses, optional
        chaining or arguments, and ``G`` a bare identifier. An ``else`` branch is allowed: ``G``
        runs whenever the file is the main module. Every node is checked by its own type; none
        goes through :func:`_operand`, which would read through ``await`` and parentheses."""
        if self._es_module:
            return None
        condition = statement.child_by_field_name("condition")
        consequence = statement.child_by_field_name("consequence")
        if condition is None or consequence is None or consequence.type != "statement_block":
            return None
        if not self._is_main_guard(condition):
            return None
        if self._module_bindings()["require"] or self._module_bindings()["module"]:
            return None
        statements = [child for child in consequence.named_children if child.type != "comment"]
        if len(statements) != 1 or statements[0].type != "expression_statement":
            return None
        expressions = [child for child in statements[0].named_children if child.type != "comment"]
        if len(expressions) != 1:
            return None
        call = expressions[0]
        if not self._plain_call(call):
            return None
        callee = call.child_by_field_name("function")
        if callee is None or callee.type != "member_expression" or _optional(callee):
            return None
        prop = callee.child_by_field_name("property")
        main_call = callee.child_by_field_name("object")
        if (
            prop is None
            or node_text(self._source, prop) != "then"
            or main_call is None
            or not self._plain_call(main_call)
        ):
            return None
        started = main_call.child_by_field_name("function")
        if started is None or started.type != "identifier":
            return None
        return node_text(self._source, started), main_call.start_byte

    def _is_main_guard(self, condition: Node) -> bool:
        """Whether an ``if`` condition is ``require.main === module``, parentheses of the
        condition itself aside."""
        while condition.type == "parenthesized_expression":
            inner = [child for child in condition.named_children if child.type != "comment"]
            if len(inner) != 1:
                return False
            condition = inner[0]
        if condition.type != "binary_expression":
            return False
        operator = condition.child_by_field_name("operator")
        left = condition.child_by_field_name("left")
        right = condition.child_by_field_name("right")
        if operator is None or left is None or right is None:
            return False
        if node_text(self._source, operator) != "===":
            return False
        if right.type != "identifier" or node_text(self._source, right) != "module":
            return False
        if left.type != "member_expression" or _optional(left):
            return False
        owner = left.child_by_field_name("object")
        prop = left.child_by_field_name("property")
        return (
            owner is not None
            and prop is not None
            and owner.type == "identifier"
            and node_text(self._source, owner) == "require"
            and node_text(self._source, prop) == "main"
        )

    @staticmethod
    def _plain_call(node: Node) -> bool:
        """A ``call_expression`` with no arguments and no optional chaining."""
        if node.type != "call_expression" or _optional(node):
            return False
        return _arguments(node) == []

    def _called_name(self, node: Node) -> str | None:
        """``G`` of ``G(...)``, or of ``G(...).catch(...)``."""
        if node.type != "call_expression":
            return None
        callee = node.child_by_field_name("function")
        if callee is not None and callee.type == "member_expression":
            owner = callee.child_by_field_name("object")
            prop = callee.child_by_field_name("property")
            if owner is None or prop is None or node_text(self._source, prop) != "catch":
                return None
            node = _operand(owner)
            if node.type != "call_expression":
                return None
            callee = node.child_by_field_name("function")
        if callee is None or callee.type != "identifier":
            return None
        return node_text(self._source, callee)

    def _instance_claims(self, root: Node) -> list[ParsedReference]:
        """A ``fastify-instance`` claim per ``const x = F()`` and per entry application, in
        the order the source declares them, at most :data:`_MAX_INSTANCES`."""
        found: list[ParsedReference] = []
        for statement in _module_statements(root):
            if statement.type == "function_declaration":
                name_node = statement.child_by_field_name("name")
                if name_node is not None:
                    self._entry(node_text(self._source, name_node), statement, found)
                continue
            for declarator, name_node, value in self._const_declarators(statement):
                function = _operand(value)
                name = node_text(self._source, name_node)
                if function.type in _FUNCTION_VALUES:
                    self._entry(name, function, found)
                else:
                    self._claim(declarator, name_node, value, None, found)
        return found

    def _entry(self, name: str, node: Node, found: list[ParsedReference]) -> None:
        """The instances at the top level of the module-level function ``name``."""
        function = self._functions.get(name)
        if function is None or function.factory or _key(function.node) != _key(node):
            return
        body = node.child_by_field_name("body")
        if body is None or body.type != "statement_block":
            return
        for statement in body.named_children:
            for declarator, name_node, value in self._const_declarators(statement):
                self._claim(declarator, name_node, value, (name, function), found)

    def _claim(
        self,
        declarator: Node,
        name_node: Node,
        value: Node,
        owner: tuple[str, _Function] | None,
        found: list[ParsedReference],
    ) -> None:
        if len(found) >= _MAX_INSTANCES:
            return
        receiver = node_text(self._source, name_node)
        call, awaited = _unwrapped(value)
        if call.type != "call_expression" or not self._bound_once(owner, receiver):
            return
        if self._source.count(receiver.encode()) < 2:
            return  # written once, at its declaration: nothing uses it
        function: _Function | None = None
        refusal: str | None = None
        reference: dict[str, str] = {}
        if self._fastify_call(call):
            if owner is None:
                return  # the module's own application: a receiver already
            kind = ENTRY
        else:
            named = self._factory_callee(call)
            if named is None:
                return
            kind = INSTANCE
            reference, function = named
            reference["awaited"] = "true" if awaited else "false"
            if function is not None:
                if not function.factory:
                    refusal = FACTORY_UNPROVEN
                elif owner is None and function.ready > call.start_byte:
                    refusal = CALLEE_BEFORE_BINDING
                elif function.is_async and not awaited:
                    refusal = FACTORY_NOT_AWAITED
        if refusal is None and owner is not None:
            refusal = self._entry_refusal(owner[0], owner[1])
        scope = self._program if owner is None else owner[1].node
        if refusal is None and self._used_before(scope, name_node, declarator):
            refusal = RECEIVER_BEFORE_BINDING
        reached = self._aliases(owner, scope, declarator, receiver)
        if not self._used(owner, [receiver, *(alias for alias, _hops, _ready in reached)]):
            return  # nothing serves from this receiver or hands it on
        aliases = [] if refusal else reached
        status = PROVEN
        if refusal is not None:
            status = REFUSED
        elif kind == INSTANCE and function is None:
            status = PENDING  # an imported factory: the global pass reads that module
        meta = {
            "kind": kind,
            "receiver": receiver,
            "entry": owner[0] if owner is not None else "",
            **reference,
            "status": status,
            "aliases": json.dumps([[alias, hops] for alias, hops, _ready in aliases]),
        }
        if refusal is not None:
            meta["refusal"] = refusal
        start, end = line_range(declarator)
        target = receiver if owner is None else f"{owner[0]}>{receiver}"
        found.append(
            pending_reference(
                EdgeType.REFERENCES,
                EntityType.SYMBOL,
                target,
                start,
                end,
                role=FASTIFY_INSTANCE,
                **meta,
            )
        )
        body = scope if owner is None else owner[1].node.child_by_field_name("body")
        if body is not None:
            ready = {receiver: declarator.end_byte}
            ready.update({alias: at for alias, _hops, at in aliases})
            held = _Held(_Scope(target, kind, start), body, owner[0] if owner else "", ready)
            where = None if owner is None else _key(owner[1].node)
            for name in ready:
                self._held[(where, name)] = held

    def _factory_callee(self, call: Node) -> tuple[dict[str, str], _Function | None] | None:
        """What ``F(...)`` names: a function of this file, or a relative module's export."""
        callee = call.child_by_field_name("function")
        if callee is None:
            return None
        if callee.type == "identifier":
            name = node_text(self._source, callee)
            if name in self._functions:
                return {"local": f"{name}()"}, self._functions[name]
            if name in self._imported and self._binding_function(callee, name) is None:
                module, imported = self._imported[name]
                return {"module": module, "name": imported}, None
        elif callee.type == "member_expression":
            owner = callee.child_by_field_name("object")
            prop = callee.child_by_field_name("property")
            if (
                owner is not None
                and prop is not None
                and owner.type == "identifier"
                and prop.type == "property_identifier"
                and node_text(self._source, owner) in self._namespaces
                and self._binding_function(owner, node_text(self._source, owner)) is None
            ):
                module = self._namespaces[node_text(self._source, owner)]
                return {"module": module, "name": node_text(self._source, prop)}, None
        return None

    def _entry_refusal(self, name: str, function: _Function) -> str | None:
        """Why the file does not provably run the entry function ``name``, if it does not."""
        calls = self._called.get(name, [])
        if any(at >= function.ready for at in calls):
            return None
        return CALLEE_BEFORE_BINDING if calls else ENTRY_NOT_CALLED

    def _bound_once(self, owner: tuple[str, _Function] | None, name: str) -> bool:
        """Whether ``name`` is bound once where the declaration is: the module's own scope
        (a parameter of a function is no binding there), or the entry's."""
        counts = self._module_bindings() if owner is None else self._own_counts(owner[1].node)
        return counts[name] == 1

    def _identifiers(self, scope: Node) -> dict[str, list[int]]:
        """Where each name is written in ``scope``'s own code, never in a nested function."""
        key = _key(scope)
        found = self._uses.get(key)
        if found is None:
            found = {}
            stack = [scope]
            while stack:
                node = stack.pop()
                if node.type in _FUNCTIONS and _key(node) != key:
                    continue
                if node.type in _IDENTIFIERS:
                    found.setdefault(node_text(self._source, node), []).append(node.start_byte)
                stack.extend(node.children)
            self._uses[key] = found
        return found

    def _used_before(self, scope: Node, name_node: Node, declarator: Node) -> bool:
        """Whether ``scope`` reads the name before its ``const`` has run. A use inside a
        nested function is a later one: it runs when that function is called."""
        name = node_text(self._source, name_node)
        return any(
            at < declarator.end_byte and at != name_node.start_byte
            for at in self._identifiers(scope).get(name, ())
        )

    def _aliases(
        self,
        owner: tuple[str, _Function] | None,
        scope: Node,
        declarator: Node,
        receiver: str,
    ) -> list[tuple[str, int, int]]:
        """``(name, hops, where its binding ends)`` of each ``const y = x`` that makes ``y`` the
        receiver ``x`` is: in the same scope, bound once, declared after what it names, at most
        :data:`_MAX_ALIAS_HOPS` hops from the instance."""
        #: Name → hops from the instance, and where its binding ends.
        known = {receiver: (0, declarator.end_byte)}
        found: list[tuple[str, int, int]] = []
        for later, name_node, origin in self._alias_candidates(owner):
            hops, ready = known.get(origin, (_MAX_ALIAS_HOPS, 0))
            if hops >= _MAX_ALIAS_HOPS or later.start_byte < ready:
                continue
            alias = node_text(self._source, name_node)
            if not self._bound_once(owner, alias):
                continue
            if self._used_before(scope, name_node, later):
                continue
            known[alias] = (hops + 1, later.end_byte)
            found.append((alias, hops + 1, later.end_byte))
        return found

    def _alias_candidates(
        self, owner: tuple[str, _Function] | None
    ) -> list[tuple[Node, Node, str]]:
        """``(declarator, name, x)`` of each ``const y = x`` at the top level of a scope, the
        module's or an entry function's, in source order."""
        where = None if owner is None else _key(owner[1].node)
        found = self._alias_sites.get(where)
        if found is None:
            found = []
            if owner is None:
                statements = list(_module_statements(self._program))
            else:
                body = owner[1].node.child_by_field_name("body")
                statements = list(body.named_children) if body is not None else []
            for statement in statements:
                for later, name_node, value in self._const_declarators(statement):
                    origin, awaited = _unwrapped(value)
                    if not awaited and origin.type == "identifier":
                        found.append((later, name_node, node_text(self._source, origin)))
            self._alias_sites[where] = found
        return found

    # -- use ---------------------------------------------------------------------------

    def _name_index(self) -> dict[str, list[Node]]:
        """Every identifier of the file by name, in source order."""
        if self._names is None:
            found: dict[str, list[Node]] = {}
            for node in walk(self._program):
                if node.type in _IDENTIFIERS:
                    found.setdefault(node_text(self._source, node), []).append(node)
            self._names = found
        return self._names

    def _used(self, owner: tuple[str, _Function] | None, names: list[str]) -> bool:
        """Whether the file serves from a receiver or hands it on: some name of ``names`` is
        the receiver of a route verb, ``route``, ``register``, ``listen`` or ``ready``, or an
        argument (or held by an object, array or spread that is one) of a call of a function of
        this file or of a relative module."""
        # a name written once is only its own declaration: no need to look at the file's nodes
        written = [name for name in names if self._source.count(name.encode()) > 1]
        if not written:
            return False
        index = self._name_index()
        for name in written:
            for node in index.get(name, ()):
                bound = self._enclosing_binding(node, name)
                if owner is None:
                    if not isinstance(bound, str) or bound != _MODULE:
                        continue  # another binding of the same name, in a function
                elif isinstance(bound, str) or _key(bound) != _key(owner[1].node):
                    continue  # another binding of the same name
                if self._is_use(node):
                    return True
        return False

    def _is_use(self, node: Node) -> bool:
        parent = node.parent
        if parent is None:
            return False
        if parent.type == "member_expression":
            held = parent.child_by_field_name("object")
            prop = parent.child_by_field_name("property")
            call = parent.parent
            if held is None or _key(held) != _key(node) or prop is None or call is None:
                return False
            if node_text(self._source, prop) not in _USES or call.type != "call_expression":
                return False
            function = call.child_by_field_name("function")
            return function is not None and _key(function) == _key(parent)
        while parent.type in _CARRIERS and parent.parent is not None:
            node, parent = parent, parent.parent
        if parent.type != "arguments" or parent.parent is None:
            return False
        call = parent.parent
        if call.type != "call_expression" or not any(
            _key(argument) == _key(node) for argument in _scanned(call)
        ):
            return False
        callee = call.child_by_field_name("function")
        return callee is not None and self._known_callee(callee)

    def _known_callee(self, callee: Node) -> bool:
        """Whether a call names a function of this file or of a relative module."""
        callee = _operand(callee)
        if callee.type == "identifier":
            name = node_text(self._source, callee)
            return name in self._functions or name in self._imported
        if callee.type != "member_expression":
            return False
        owner = callee.child_by_field_name("object")
        prop = callee.child_by_field_name("property")
        return (
            owner is not None
            and prop is not None
            and owner.type == "identifier"
            and prop.type == "property_identifier"
            and node_text(self._source, owner) in self._namespaces
        )

    # -- routes ------------------------------------------------------------------------

    def call_references(self, node: Node, source_symbol: str | None) -> list[ParsedReference]:
        if not (self._reads or self._traced):
            return []
        return [
            *self._route_references(node, source_symbol),
            *self._helper_references(node, source_symbol),
        ]

    def _route_references(self, node: Node, source_symbol: str | None) -> list[ParsedReference]:
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
        if scope.kind in _TRACED_KINDS or self._traced:
            traced: list[ParsedReference] = []
            for method in methods:
                if self._traced_total >= _MAX_TRACED_ROUTES:
                    break
                self._traced_total += 1
                traced.append(
                    pending_reference(
                        EdgeType.SERVES_HTTP_ROUTE,
                        EntityType.HTTP_ROUTE,
                        f"{method} {shown}",
                        start,
                        end,
                        role=FASTIFY_TRACED_ROUTE,
                        source_symbol=source_symbol,
                        method=method,
                        path=json.dumps(parts),
                        scope=scope.id,
                        owner_kind=scope.kind,
                        provenance=EXACT if literal else COMPOSED,
                    )
                )
            if traced:
                self._touch(scope)
            return traced
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

    # -- helper calls ------------------------------------------------------------------

    def _helper_references(self, node: Node, source_symbol: str | None) -> list[ParsedReference]:
        """A claim for each receiver ``node`` hands over, until the file's bound is reached."""
        if self._helper_total >= _MAX_HELPER_CALLS:
            return []
        callee = node.child_by_field_name("function")
        handed: list[tuple[int, str, bool, _Scope, _Held | None]] = []
        for index, argument in enumerate(_scanned(node)):
            for identifier, inside in self._handed(argument):
                name = node_text(self._source, identifier)
                found = self._resolve(identifier, name)
                if found is not None:
                    scope, held = found
                    if scope.kind == APP:
                        held = self._application_held(scope, name)
                    handed.append((index, name, inside, scope, held))
        if callee is None or not handed:
            return []
        reached = self._callee(callee)
        if reached is None:
            return []
        target, named_refusal = reached
        start, end = line_range(node)
        claims: list[ParsedReference] = []
        for index, name, inside, scope, held in handed:
            if self._helper_total >= _MAX_HELPER_CALLS:
                break
            self._helper_total += 1
            meta = {
                "owner": scope.id,
                "owner_kind": scope.kind,
                "receiver": name,
                "arg": str(index),
                **target,
            }
            refusal = self._helper_refusal(node, name, inside, scope, held, target, named_refusal)
            if refusal is not None:
                meta["refusal"] = refusal
            self._touch(scope)
            claims.append(
                pending_reference(
                    EdgeType.REFERENCES,
                    EntityType.SYMBOL,
                    node_text(self._source, callee)[:_MAX_CONSTANT],
                    start,
                    end,
                    role=FASTIFY_HELPER_CALL,
                    source_symbol=source_symbol,
                    **meta,
                )
            )
        return claims

    def _application_held(self, scope: _Scope, name: str) -> _Held:
        """The module's own application as a receiver: the program runs it, from the end of
        the declarator that binds it."""
        node = scope.node
        while node is not None and node.type != "variable_declarator":
            node = node.parent
        ready = node.end_byte if node is not None else 0
        return _Held(scope, self._program, "", {name: ready})

    def _handed(self, argument: Node) -> list[tuple[Node, bool]]:
        """The names an argument hands over, and whether each sits inside an object, an array
        or a spread rather than being the argument itself."""
        node = _operand(argument)
        if node.type == "identifier":
            return [(node, False)]
        found: list[Node] = []
        self._holding(node, found, 0)
        return [(identifier, True) for identifier in found]

    def _holding(self, node: Node, found: list[Node], depth: int) -> None:
        """The names an object, an array or a spread holds directly: a value, an element or
        the operand of a spread, down to :data:`_MAX_HOLDING_DEPTH` levels."""
        node = _operand(node)
        if node.type in _IDENTIFIERS:
            found.append(node)
        elif node.type in _HOLDERS and depth < _MAX_HOLDING_DEPTH:
            for child in node.named_children:
                if child.type == "comment":
                    continue
                if child.type == "pair":
                    value = child.child_by_field_name("value")
                    if value is not None:
                        self._holding(value, found, depth + 1)
                else:
                    self._holding(child, found, depth + 1)

    def _callee(self, callee: Node) -> tuple[dict[str, str], str | None] | None:
        """The function a call names and the first code that makes it untrustworthy, or
        ``None`` when the callee names nothing this file can tell (a global, a package)."""
        callee = _operand(callee)
        if callee.type == "identifier":
            return self._named_callee(callee)
        if callee.type == "subscript_expression":
            return {}, HELPER_DYNAMIC
        if callee.type != "member_expression":
            return {}, HELPER_CALLEE_UNSUPPORTED
        owner = callee.child_by_field_name("object")
        prop = callee.child_by_field_name("property")
        if owner is None or prop is None or node_text(self._source, prop) in _REBINDING:
            return {}, HELPER_CALLEE_UNSUPPORTED
        name = node_text(self._source, owner)
        if (
            owner.type == "identifier"
            and prop.type == "property_identifier"
            and name in self._namespaces
        ):
            if self._binding_function(owner, name) is not None:
                return {}, HELPER_SHADOWED
            member = node_text(self._source, prop)
            return {"module": self._namespaces[name], "name": member}, None
        return {}, HELPER_CALLEE_UNSUPPORTED

    def _named_callee(self, callee: Node) -> tuple[dict[str, str], str | None] | None:
        name = node_text(self._source, callee)
        bound = self._binding_function(callee, name)
        if bound is None:
            if name in self._functions:
                return {"local": name}, None
            if name in self._imported:
                module, imported = self._imported[name]
                return {"module": module, "name": imported}, None
            if name in self._aliased():
                return {}, HELPER_CALLEE_UNSUPPORTED
            return None
        kind = self._binder_kind(bound, name)
        if kind in {"parameter", "loop"}:
            return {}, HELPER_DYNAMIC
        if kind == "alias":
            return {}, HELPER_CALLEE_UNSUPPORTED
        if self._module_bindings()[name]:
            return {}, HELPER_SHADOWED  # a name of this file, hidden where it is called
        return {}, HELPER_CALLEE_UNSUPPORTED  # a function declared in a function

    def _aliased(self) -> set[str]:
        """The module's ``const g = f`` names, bound once."""
        if self._alias_names is None:
            names = (node_text(self._source, name) for _d, name, _o in self._alias_candidates(None))
            self._alias_names = {name for name in names if self._counts[name] == 1}
        return self._alias_names

    def _module_bindings(self) -> Counter[str]:
        if self._module_bound is None:
            self._module_bound = scope_bindings(self._source, self._program)
        return self._module_bound

    def _binding_function(self, at: Node, name: str) -> Node | None:
        """The nearest function that binds ``name`` where ``at`` uses it, however often. A
        declaration's own name is bound outside it, not in it; a function expression's own
        name is bound inside it."""
        node = at.parent
        while node is not None and node.type != "program":
            if node.type in _FUNCTIONS:
                label = node.child_by_field_name("name")
                own = (
                    node.type in _DECLARATIONS
                    and label is not None
                    and node_text(self._source, label) == name
                )
                if self._own_counts(node)[name] > (1 if own else 0):
                    return node
            node = node.parent
        return None

    def _binder_kind(self, function: Node, name: str) -> str:
        """How ``function``'s own scope binds ``name``: as a ``parameter``, a ``loop``
        variable, an ``alias`` (a ``const`` naming another value), a ``function``, or
        ``other``."""
        key = _key(function)
        stack = [function]
        while stack:
            node = stack.pop()
            if _key(node) != key and node.type in _FUNCTIONS:
                label = node.child_by_field_name("name")
                if (
                    node.type in _DECLARATIONS
                    and label is not None
                    and node_text(self._source, label) == name
                ):
                    return "function"
                continue
            for index, child in enumerate(node.children):
                if (
                    child.type in {"identifier", "shorthand_property_identifier_pattern"}
                    and node_text(self._source, child) == name
                ):
                    kind = self._binder(node, node.field_name_for_child(index))
                    if kind is not None:
                        return kind
            stack.extend(reversed(node.children))
        return "other"

    def _binder(self, parent: Node, field: str | None) -> str | None:
        """What binding ``parent`` makes of its child in ``field``, or ``None`` if it binds
        nothing."""
        kind = parent.type
        if kind == "variable_declarator":
            if field != "name":
                return None
            declaration = parent.parent
            if (
                declaration is not None
                and declaration.parent is not None
                and declaration.parent.type == "for_statement"
            ):
                return "loop"
            value = parent.child_by_field_name("value")
            if value is None:
                return "other"
            value = _operand(value)
            if value.type in _FUNCTION_VALUES:
                return "function"
            return "alias" if value.type in {"identifier", "member_expression"} else "other"
        if kind == "for_in_statement":
            return "loop" if field == "left" else None
        if kind in {"required_parameter", "optional_parameter"}:
            return "parameter" if field == "pattern" else None
        if kind == "arrow_function":
            return "parameter" if field == "parameter" else None
        if kind in {"function_declaration", "function_expression", "function"}:
            return "function" if field == "name" else None
        if kind == "formal_parameters":
            return "parameter"
        if kind == "catch_clause":
            return "other" if field == "parameter" else None
        if kind in {"object_pattern", "array_pattern", "rest_pattern", "pair_pattern"}:
            return self._pattern_kind(parent)
        return None

    def _pattern_kind(self, pattern: Node) -> str:
        """What a destructuring pattern binds for: the parameters of a function, a loop, or
        a declaration."""
        node: Node | None = pattern
        while node is not None and node.type not in _FUNCTIONS:
            if node.type in {"formal_parameters", "required_parameter", "optional_parameter"}:
                return "parameter"
            if node.type == "for_in_statement":
                return "loop"
            if node.type in {"variable_declarator", "catch_clause"}:
                return "other"
            node = node.parent
        return "other"

    def _function_body(self, scope: _Scope) -> Node | None:
        return scope.node.child_by_field_name("body") if scope.node is not None else None

    def _helper_refusal(
        self,
        call: Node,
        name: str,
        inside: bool,
        scope: _Scope,
        held: _Held | None,
        target: dict[str, str],
        callee_refusal: str | None,
    ) -> str | None:
        """The first code, in the order of the claim's documentation, that stops a helper
        call from being trusted; ``None`` when none applies."""
        body = held.body if held is not None else self._function_body(scope)
        if body is None or not self._directly_in(call, body):
            return HELPER_CONTROL_FLOW
        if callee_refusal is not None:
            return callee_refusal
        if inside:
            return HELPER_ARGUMENT_UNSUPPORTED
        if held is not None:
            if call.start_byte < held.ready.get(name, 0):
                return RECEIVER_BEFORE_BINDING
            if self._callee_early(call, target, held):
                return CALLEE_BEFORE_BINDING
        names = set(held.ready) if held is not None else {name}
        if self._listened(body, names, call):
            return AFTER_LISTEN
        return None

    def _callee_early(self, call: Node, target: dict[str, str], held: _Held) -> bool:
        """Whether the callee is a function of this file bound by a ``const`` that has not run
        yet where the receiver's body runs the call."""
        function = self._functions.get(target.get("local", ""))
        if function is None:
            return False
        if held.body.type == "program":
            point = call.start_byte
        elif held.entry:
            point = min(self._called.get(held.entry) or [call.start_byte])
        else:
            return False
        return function.ready > point

    def _listened(self, body: Node, names: set[str], call: Node) -> bool:
        """Whether ``body`` awaits ``x.listen(...)`` or ``x.ready(...)`` on one of ``names``
        before ``call``."""
        key = _key(body)
        points = self._listens.get(key)
        if points is None:
            points = []
            if body.type == "program":
                statements = list(_module_statements(body))
            elif body.type == "statement_block":
                statements = list(body.named_children)
            else:
                statements = []
            for statement in statements:
                for expression in self._expressions(statement):
                    awaited_call, awaited = _unwrapped(expression)
                    receiver = self._started(awaited_call)
                    if awaited and receiver is not None:
                        points.append((statement.end_byte, receiver))
            self._listens[key] = points
        return any(end <= call.start_byte and who in names for end, who in points)

    def _expressions(self, statement: Node) -> list[Node]:
        """The expression an expression statement is, or the values of a ``const``."""
        if statement.type == "expression_statement":
            inner = [child for child in statement.named_children if child.type != "comment"]
            return inner[:1] if len(inner) == 1 else []
        return [value for _declarator, _name, value in self._const_declarators(statement)]

    def _started(self, node: Node) -> str | None:
        """``x`` of ``x.listen(...)`` and ``x.ready(...)``."""
        if node.type != "call_expression":
            return None
        callee = node.child_by_field_name("function")
        if callee is None or callee.type != "member_expression":
            return None
        owner = callee.child_by_field_name("object")
        prop = callee.child_by_field_name("property")
        if owner is None or prop is None or owner.type != "identifier":
            return None
        if node_text(self._source, prop) not in {"listen", "ready"}:
            return None
        return node_text(self._source, owner)

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
        elif self._traced:
            for scope in self._touched.values():
                receiver = ""
                if scope.node is not None:
                    receiver = _first_parameter(self._source, scope.node) or ""
                found.append(
                    pending_reference(
                        EdgeType.REFERENCES,
                        EntityType.SYMBOL,
                        scope.id,
                        scope.line,
                        scope.line,
                        role=FASTIFY_TRACED_SCOPE,
                        kind=scope.kind,
                        exports=json.dumps(scope.exports),
                        receiver=receiver,
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
        if self._reads:
            for scope in self._factory_scopes.values():
                found.append(
                    pending_reference(
                        EdgeType.REFERENCES,
                        EntityType.SYMBOL,
                        scope.id,
                        scope.line,
                        scope.line,
                        role=FASTIFY_SCOPE,
                        kind=scope.kind,
                        wrapped="false",
                        exports=json.dumps(scope.exports),
                        is_async="true" if scope.is_async else "false",
                    )
                )
        found.extend(self._instances)
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
