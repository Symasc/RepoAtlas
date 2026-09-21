"""Static HTTP facts in TypeScript and JavaScript source (HTTP linkage).

The same rule as :mod:`repoatlas.parsers.http_python`, applied to a language where
the rule matters even more: ``x.get("/hello")`` is a ``Map`` lookup, a cache
read, an Express *settings getter* (``app.get("title")``) and a route
declaration, and only the last is a fact about HTTP. So nothing is matched by
method name. A receiver counts only when this file's own imports and top-level
``const`` declarations prove what it is, and each such name is bound exactly
once anywhere in the file:

* **Express** — ``express`` imported from ``"express"`` (default import or
  ``require``); ``const app = express()``; ``const router = express.Router()``
  or ``Router()`` from ``"express"``. A route is ``app.<method>(path, handler…)``
  with at least two arguments; one argument is the settings getter. A router's
  routes are recorded only under a mount this file proves
  (``app.use("/api", router)``), composed from two places;
* **fetch** — the global ``fetch``, which is only global when this file binds no
  ``fetch`` of its own. Without an options object the method is GET. With one,
  the method is read only from a literal ``method`` in an object literal with no
  spread, shorthand or computed keys — an options object that *might* carry a
  method proves nothing, and claiming GET for it would be a guess;
* **axios** — ``axios.<method>(url…)`` on the default export, **absolute URLs
  only**. A relative axios URL is combined with a ``baseURL`` that any file can
  set on the default instance, so its path is not knowable here.

A URL or path is read by :func:`static_string`: a string literal, a template
literal with no substitution, or a ``+`` concatenation of those. An escape
sequence is refused rather than decoded, because the raw text is not the value.

Parsed, never executed: tree-sitter reads the syntax tree only (V1_SPEC §17).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from repoatlas.models import http
from repoatlas.models.parsing import EdgeType, EntityType, ParsedReference
from repoatlas.parsers.http_facts import http_reference
from repoatlas.parsers.treesitter import line_range, node_text, string_literal, walk

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tree_sitter import Node

_EXPRESS_VERBS = {
    "get": "GET",
    "post": "POST",
    "put": "PUT",
    "patch": "PATCH",
    "delete": "DELETE",
    "head": "HEAD",
    "options": "OPTIONS",
    "trace": "TRACE",
}
#: ``axios.<alias>`` and the position of its config argument.
_AXIOS_VERBS = {
    "get": ("GET", 1),
    "delete": ("DELETE", 1),
    "head": ("HEAD", 1),
    "options": ("OPTIONS", 1),
    "post": ("POST", 2),
    "put": ("PUT", 2),
    "patch": ("PATCH", 2),
}
#: Config keys that change which URL an axios request goes to, or its method.
_AXIOS_REDIRECTING = frozenset({"baseURL", "url", "method", "allowAbsoluteUrls"})

#: Methods the Fetch standard upper-cases when given in any case. Any other
#: method is sent exactly as written, so ``"patch"`` is not ``PATCH``.
_FETCH_NORMALIZED = frozenset({"DELETE", "GET", "HEAD", "OPTIONS", "POST", "PUT"})

#: Characters with pattern meaning in an Express path. A path containing one is
#: a pattern, not a route identity.
_EXPRESS_PATTERN = frozenset("*?()+{}[]")

_EXPRESS_APP = "express-app"
_EXPRESS_ROUTER = "express-router"

_MAX_STRING_DEPTH = 32

#: ``(parent type, field)`` positions where an identifier *binds* a name.
_BINDING_FIELDS = frozenset(
    {
        ("variable_declarator", "name"),
        ("required_parameter", "pattern"),
        ("optional_parameter", "pattern"),
        ("arrow_function", "parameter"),
        ("assignment_expression", "left"),
        ("augmented_assignment_expression", "left"),
        ("update_expression", "argument"),
        ("function_declaration", "name"),
        ("generator_function_declaration", "name"),
        ("function_expression", "name"),
        ("class_declaration", "name"),
        ("abstract_class_declaration", "name"),
        ("class", "name"),
        ("catch_clause", "parameter"),
        ("for_in_statement", "left"),
        ("pair_pattern", "value"),
        ("assignment_pattern", "left"),
        ("object_assignment_pattern", "left"),
    }
)
#: Parents whose unnamed identifier children bind a name.
_BINDING_PARENTS = frozenset(
    {
        "import_clause",
        "namespace_import",
        "object_pattern",
        "array_pattern",
        "rest_pattern",
        "formal_parameters",
    }
)


def static_string(source: bytes, node: Node | None, depth: int = 0) -> str | None:
    """The value of a literal string expression, or ``None``."""
    if node is None or depth > _MAX_STRING_DEPTH:
        return None
    kind = node.type
    if kind in {"string", "template_string"}:
        if any(child.type == "template_substitution" for child in node.children):
            return None
        value = string_literal(source, node)
        return value if value is not None and "\\" not in value else None
    if kind == "parenthesized_expression":
        inner = [child for child in node.children if child.is_named]
        return static_string(source, inner[0], depth + 1) if len(inner) == 1 else None
    if kind == "binary_expression":
        operator = node.child_by_field_name("operator")
        if operator is None or node_text(source, operator) != "+":
            return None
        left = static_string(source, node.child_by_field_name("left"), depth + 1)
        if left is None:
            return None
        right = static_string(source, node.child_by_field_name("right"), depth + 1)
        return None if right is None else left + right
    return None


def fetch_method(value: str) -> str | None:
    """The method ``fetch`` actually sends for ``value``, if it is one of ours."""
    upper = value.upper()
    if upper in _FETCH_NORMALIZED:
        return upper
    return value if value == "PATCH" else None


def _arguments(node: Node) -> list[Node] | None:
    arguments = node.child_by_field_name("arguments")
    if arguments is None or arguments.type != "arguments":
        return None
    return [child for child in arguments.children if child.is_named and child.type != "comment"]


def binding_counts(source: bytes, root: Node) -> Counter[str]:
    """How many times each name is bound anywhere beneath ``root`` (a file, or a scope)."""
    counts: Counter[str] = Counter()
    for node in walk(root):
        for index, child in enumerate(node.children):
            if child.type == "shorthand_property_identifier_pattern":
                counts[node_text(source, child)] += 1
                continue
            if child.type != "identifier":
                continue
            name = node.field_name_for_child(index)
            if node.type == "import_specifier":
                alias = node.child_by_field_name("alias")
                binds = name == "alias" or (name == "name" and alias is None)
            else:
                binds = (node.type, name) in _BINDING_FIELDS or (
                    name is None and node.type in _BINDING_PARENTS
                )
            if binds:
                counts[node_text(source, child)] += 1
    return counts


def _plain_object(source: bytes, node: Node) -> dict[str, Node] | None:
    """An object literal's keys, or ``None`` when any key is not provably literal."""
    if node.type != "object":
        return None
    keys: dict[str, Node] = {}
    for child in node.children:
        if not child.is_named or child.type == "comment":
            continue
        if child.type != "pair":
            # A spread, a shorthand `{ method }` or a method definition: this
            # object may carry any key at all.
            return None
        key = child.child_by_field_name("key")
        value = child.child_by_field_name("value")
        if key is None or value is None:
            return None
        if key.type == "property_identifier":
            name = node_text(source, key)
        elif key.type == "string":
            literal = string_literal(source, key)
            if literal is None:
                return None
            name = literal
        else:
            return None
        if name in keys:
            return None
        keys[name] = value
    return keys


@dataclass(slots=True)
class _Receiver:
    kind: str
    mounts: list[str] = field(default_factory=list)
    usable: bool = True


class TypeScriptHttp:
    """What one file proves about HTTP, and the facts that follow from it."""

    def __init__(self, source: bytes, root: Node) -> None:
        self._source = source
        counts = binding_counts(source, root)
        #: How often each name is bound anywhere in the file, shared with the other
        #: readers of the same file so every one proves a binding by the same rule.
        self.counts = counts
        self._express: set[str] = set()
        self._router_factories: set[str] = set()
        self._axios: set[str] = set()
        self._receivers: dict[str, _Receiver] = {}
        self._fetch = counts["fetch"] == 0
        # A client that turns absolute URLs into relative ones can be configured
        # from anywhere in the file; if it is, no axios URL here is trusted.
        self._axios_trusted = b"allowAbsoluteUrls" not in source
        self._top_level(root, counts)

    # -- what the file proves ---------------------------------------------------------

    def _top_level(self, root: Node, counts: Counter[str]) -> None:
        statements: list[Node] = []
        for statement in root.children:
            if statement.type == "export_statement":
                statements.extend(child for child in statement.children if child.is_named)
            else:
                statements.append(statement)
        for statement in statements:
            if statement.type == "import_statement":
                self._import(statement, counts)
            elif statement.type == "lexical_declaration":
                self._declaration(statement, counts)
        for statement in statements:
            if statement.type == "expression_statement":
                self._mount(statement)

    def _import(self, statement: Node, counts: Counter[str]) -> None:
        source_node = statement.child_by_field_name("source")
        module = string_literal(self._source, source_node) if source_node is not None else None
        if module not in {"express", "axios"}:
            return
        clause = next((c for c in statement.children if c.type == "import_clause"), None)
        if clause is None:
            return
        for child in clause.children:
            if child.type == "identifier":
                name = node_text(self._source, child)
                if counts[name] == 1:
                    (self._express if module == "express" else self._axios).add(name)
            elif child.type == "named_imports" and module == "express":
                for specifier in child.children:
                    if specifier.type != "import_specifier":
                        continue
                    imported = specifier.child_by_field_name("name")
                    alias = specifier.child_by_field_name("alias") or imported
                    if imported is None or alias is None:
                        continue
                    local = node_text(self._source, alias)
                    if node_text(self._source, imported) == "Router" and counts[local] == 1:
                        self._router_factories.add(local)

    def _declaration(self, statement: Node, counts: Counter[str]) -> None:
        kind = statement.child_by_field_name("kind")
        if kind is None or node_text(self._source, kind) != "const":
            return
        for declarator in statement.children:
            if declarator.type != "variable_declarator":
                continue
            name_node = declarator.child_by_field_name("name")
            value = declarator.child_by_field_name("value")
            if name_node is None or name_node.type != "identifier" or value is None:
                continue
            name = node_text(self._source, name_node)
            if counts[name] != 1 or value.type != "call_expression":
                continue
            callee = value.child_by_field_name("function")
            if callee is None:
                continue
            if callee.type == "identifier":
                callee_name = node_text(self._source, callee)
                if callee_name == "require":
                    arguments = _arguments(value) or []
                    module = (
                        string_literal(self._source, arguments[0])
                        if len(arguments) == 1 and arguments[0].type == "string"
                        else None
                    )
                    if module == "express":
                        self._express.add(name)
                    elif module == "axios":
                        self._axios.add(name)
                elif callee_name in self._express:
                    self._receivers[name] = _Receiver(_EXPRESS_APP)
                elif callee_name in self._router_factories:
                    self._receivers[name] = _Receiver(_EXPRESS_ROUTER)
            elif callee.type == "member_expression":
                owner = callee.child_by_field_name("object")
                prop = callee.child_by_field_name("property")
                if (
                    owner is not None
                    and prop is not None
                    and owner.type == "identifier"
                    and node_text(self._source, owner) in self._express
                    and node_text(self._source, prop) == "Router"
                ):
                    self._receivers[name] = _Receiver(_EXPRESS_ROUTER)

    def _mount(self, statement: Node) -> None:
        call = next((c for c in statement.children if c.type == "call_expression"), None)
        if call is None:
            return
        callee = call.child_by_field_name("function")
        if callee is None or callee.type != "member_expression":
            return
        owner_node = callee.child_by_field_name("object")
        prop = callee.child_by_field_name("property")
        if owner_node is None or prop is None or owner_node.type != "identifier":
            return
        owner = self._receivers.get(node_text(self._source, owner_node))
        if owner is None or node_text(self._source, prop) != "use":
            return
        arguments = _arguments(call) or []
        if not arguments:
            return
        first = arguments[0]
        prefix = static_string(self._source, first)
        if prefix is not None:
            mounted = arguments[1:]
        elif first.type == "identifier" and node_text(self._source, first) in self._receivers:
            # `app.use(router)`: no path argument, so it is mounted at the root.
            mounted = arguments
        else:
            # A path this cannot read — a variable, a call, a middleware that may
            # or may not be one. Whatever is mounted here is served somewhere
            # unknown, which is not the root.
            for argument in arguments[1:]:
                if argument.type == "identifier":
                    child = self._receivers.get(node_text(self._source, argument))
                    if child is not None:
                        child.usable = False
            return
        for argument in mounted:
            if argument.type != "identifier":
                continue
            child = self._receivers.get(node_text(self._source, argument))
            if child is None:
                continue
            if owner.kind != _EXPRESS_APP or child.kind != _EXPRESS_ROUTER:
                # A nested router, or an application mounted as a sub-app: its
                # served paths are decided by a chain this file does not prove.
                child.usable = False
                continue
            mount = _express_mount(prefix)
            if mount is None:
                child.usable = False
            else:
                child.mounts.append(mount)

    # -- facts ------------------------------------------------------------------------

    def call_references(self, node: Node, source_symbol: str | None) -> list[ParsedReference]:
        callee = node.child_by_field_name("function")
        if callee is None:
            return []
        if callee.type == "identifier":
            if self._fetch and node_text(self._source, callee) == "fetch":
                return self._fetch_call(node, source_symbol)
            return []
        if callee.type != "member_expression":
            return []
        owner = callee.child_by_field_name("object")
        prop = callee.child_by_field_name("property")
        if owner is None or prop is None or owner.type != "identifier":
            return []
        name, attribute = node_text(self._source, owner), node_text(self._source, prop)
        if name in self._axios and self._axios_trusted and attribute in _AXIOS_VERBS:
            return self._axios_call(node, attribute, source_symbol)
        receiver = self._receivers.get(name)
        if receiver is not None and receiver.usable and attribute in _EXPRESS_VERBS:
            return self._route(node, receiver, _EXPRESS_VERBS[attribute], source_symbol)
        return []

    def _route(
        self, node: Node, receiver: _Receiver, method: str, source_symbol: str | None
    ) -> list[ParsedReference]:
        arguments = _arguments(node)
        # One argument is the settings getter, not a route.
        if arguments is None or len(arguments) < 2:
            return []
        path = static_string(self._source, arguments[0])
        if path is None or any(char in _EXPRESS_PATTERN for char in path):
            return []
        start, _ = line_range(node)
        _, end = line_range(arguments[0])
        if receiver.kind == _EXPRESS_APP:
            served = [(http.route_path(path), False)]
        else:
            served = [
                (http.route_path(mount if path == "/" and mount else f"{mount}{path}"), True)
                for mount in dict.fromkeys(receiver.mounts)
            ]
        return [
            http_reference(
                EdgeType.SERVES_HTTP_ROUTE,
                EntityType.HTTP_ROUTE,
                http.route_body(method, route),
                start,
                end,
                source="express",
                composed=composed,
                source_symbol=source_symbol,
            )
            for route, composed in served
            if route is not None
        ]

    def _fetch_call(self, node: Node, source_symbol: str | None) -> list[ParsedReference]:
        arguments = _arguments(node)
        if arguments is None or not 1 <= len(arguments) <= 2:
            return []
        url = static_string(self._source, arguments[0])
        _, end = line_range(arguments[0])
        method: str | None = "GET"
        if len(arguments) == 2:
            options = _plain_object(self._source, arguments[1])
            if options is None:
                return []
            method_node = options.get("method")
            if method_node is not None:
                raw = static_string(self._source, method_node)
                method = fetch_method(raw) if raw is not None else None
                end = max(end, line_range(method_node)[1])
        if url is None or method is None:
            return []
        return self._client(node, "fetch", method, url, end, source_symbol, relative=True)

    def _axios_call(
        self, node: Node, attribute: str, source_symbol: str | None
    ) -> list[ParsedReference]:
        arguments = _arguments(node)
        method, config_at = _AXIOS_VERBS[attribute]
        if arguments is None or not 1 <= len(arguments) <= config_at + 1:
            return []
        if len(arguments) > config_at:
            config = _plain_object(self._source, arguments[config_at])
            if config is None or _AXIOS_REDIRECTING & config.keys():
                return []
        url = static_string(self._source, arguments[0])
        if url is None:
            return []
        _, end = line_range(arguments[0])
        return self._client(node, "axios", method, url, end, source_symbol, relative=False)

    @staticmethod
    def _client(
        node: Node,
        library: str,
        method: str,
        url: str,
        end: int,
        source_symbol: str | None,
        *,
        relative: bool,
    ) -> list[ParsedReference]:
        target = http.client_target(url)
        if target is None or (target.url is None and not relative):
            return []
        start, _ = line_range(node)
        found = [
            http_reference(
                EdgeType.CALLS_HTTP_ROUTE,
                EntityType.HTTP_ROUTE,
                http.route_body(method, target.path),
                start,
                end,
                source=library,
                composed=False,
                source_symbol=source_symbol,
            )
        ]
        if target.url is not None:
            found.insert(
                0,
                http_reference(
                    EdgeType.CALLS_HTTP_ENDPOINT,
                    EntityType.HTTP_ENDPOINT,
                    http.endpoint_body(method, target.url),
                    start,
                    end,
                    source=library,
                    composed=False,
                    source_symbol=source_symbol,
                ),
            )
        return found


def _express_mount(prefix: str | None) -> str | None:
    """The prefix ``app.use`` mounts a router under, or ``None`` if unreadable."""
    if prefix is None or prefix == "/":
        return ""
    if (
        not prefix.startswith("/")
        or prefix.endswith("/")
        or any(char in _EXPRESS_PATTERN for char in prefix)
        or http.route_path(prefix) is None
    ):
        return None
    return prefix
