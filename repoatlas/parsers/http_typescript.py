"""Static HTTP facts in TypeScript and JavaScript source (HTTP linkage).

The same rule as :mod:`repoatlas.parsers.http_python`, applied to a language where
the rule matters even more: ``x.get("/hello")`` is a ``Map`` lookup, a cache
read, an Express *settings getter* (``app.get("title")``) and a route
declaration, and only the last is a fact about HTTP. So nothing is matched by
method name. A receiver counts only when this file's own imports and top-level
``const``, ``let`` or ``var`` declarations prove what it is, and each such name
is bound exactly once anywhere in the file:

* **Express** — ``express`` imported from ``"express"`` (default import or
  ``require``); ``const app = express()``; ``const router = express.Router()``
  or ``Router()`` from ``"express"``. A route is ``app.<method>(path, handler…)``
  with at least two arguments; one argument is the settings getter. A router's
  routes are recorded only under a mount this file proves
  (``app.use("/api", router)``), composed from two places. A receiver used
  before its declaration is refused outright, and a ``let`` or ``var`` receiver
  registers routes only in top-level statements after its declaration.
  ``app.route(path).<verb>(handler)`` chains are read link by link. A path or
  mount prefix may also be spelled through earlier top-level string constants;
  such a route is always composed, never exact;
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

A template with substitutions names no route this file can spell. When it is
rooted — its first fragment starts with one ``/`` — :func:`template_spec` records
its parts, the path only and never a query. A template whose substitutions are all
in its query is an ordinary route and is decided here; anything dynamic in a direct
``fetch`` is recorded as a pending claim (``http-local-call``, ``transport:
fetch``) so the request stays visible, and is never matched against a route.

Parsed, never executed: tree-sitter reads the syntax tree only (V1_SPEC §17).
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from repoatlas.models import http
from repoatlas.models.parsing import EdgeType, EntityType, ParsedReference
from repoatlas.parsers.http_facts import (
    BASE_UNKNOWN,
    HTTP_CLIENT_CALL,
    PENDING_FACT,
    TRANSPORT_FETCH,
    http_reference,
    local_call_reference,
    pending_reference,
)
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
#: How many constants a path may be spelled through, and how many links a
#: ``.route(path).get(...).post(...)`` chain may have.
_MAX_PATH_DEPTH = 4
_MAX_CHAIN_LINKS = 32
#: Bindings whose value can change or be read before they are set: a route
#: registered on one counts only in a top-level statement after its declaration.
_ORDERED_BINDINGS = frozenset({"let", "var"})

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


#: How much literal text of a template path a claim keeps. A longer template is
#: recorded as truncated, which forbids matching it: a cut template could match a
#: route it does not name.
MAX_TEMPLATE_CHARS = 200
#: How many parts a template may have before it is recorded as truncated.
MAX_TEMPLATE_PARTS = 32
#: How much of one substitution's source text is kept, for display only.
MAX_EXPRESSION_CHARS = 60
#: What a template string's content is made of.
_TEMPLATE_CONTENT = frozenset({"string_fragment", "escape_sequence", "template_substitution"})


def _unwrap(node: Node) -> Node:
    while node.type == "parenthesized_expression":
        inner = [child for child in node.named_children if child.type != "comment"]
        if len(inner) != 1:
            break
        node = inner[0]
    return node


def _same(left: Node | None, right: Node) -> bool:
    """Whether two nodes are the same syntax node, compared by range and type."""
    if left is None:
        return False
    return _node_key(left) == _node_key(right)


def _node_key(node: Node) -> tuple[int, int, str]:
    return node.start_byte, node.end_byte, node.type


def _rooted(value: str) -> bool:
    return value.startswith("/") and not value.startswith("//")


def template_spec(source: bytes, node: Node | None) -> dict[str, object] | None:
    """A rooted template path as parser-neutral parts, or ``None``.

    Only a template whose first fragment starts with one ``/`` is read: one that
    starts with a substitution is composing a base (``${base}${path}``), which is
    transport plumbing rather than a request. Nothing after a literal ``?`` or
    ``#`` is kept — a query can carry a token — so the parts end at the marker. A
    substitution the source proves is empty or a query string
    (``${params ? `?${params}` : ""}``) becomes a query tail, kept without its text.
    An escape sequence is kept raw, so the template model refuses it.
    """
    if node is None:
        return None
    node = _unwrap(node)
    if node.type != "template_string":
        return None
    content = [child for child in node.children if child.type in _TEMPLATE_CONTENT]
    if not content or content[0].type != "string_fragment":
        return None
    if not _rooted(node_text(source, content[0])):
        return None
    parts: list[list[str]] = []
    chars = 0
    truncated = False
    for child in content:
        if child.type == "template_substitution":
            inner = next((c for c in child.named_children if c.type != "comment"), None)
            if inner is not None and _empty_or_query_tail(source, inner):
                parts.append([http.TEMPLATE_QUERY, ""])
            else:
                text = node_text(source, inner) if inner is not None else ""
                parts.append([http.TEMPLATE_SUBSTITUTION, text[:MAX_EXPRESSION_CHARS]])
        else:
            text = node_text(source, child)
            cut = min((i for i in (text.find("?"), text.find("#")) if i >= 0), default=None)
            if cut is not None:
                text = text[: cut + 1]
            if chars + len(text) > MAX_TEMPLATE_CHARS:
                text = text[: MAX_TEMPLATE_CHARS - chars]
                truncated = True
            if parts and parts[-1][0] == http.TEMPLATE_LITERAL:
                parts[-1][1] += text
            else:
                parts.append([http.TEMPLATE_LITERAL, text])
            chars += len(text)
            if cut is not None or truncated:
                break
        if len(parts) > MAX_TEMPLATE_PARTS:
            truncated = True
            break
    return {"parts": parts, "truncated": truncated}


def _empty_or_query_tail(source: bytes, node: Node) -> bool:
    """``cond ? `?${q}` : ""``: a value that is empty or begins a query string."""
    node = _unwrap(node)
    if node.type != "ternary_expression":
        return False
    branches = (node.child_by_field_name("consequence"), node.child_by_field_name("alternative"))
    return all(branch is not None and _empty_or_query(source, branch) for branch in branches)


def _empty_or_query(source: bytes, node: Node) -> bool:
    node = _unwrap(node)
    if node.type not in {"string", "template_string"}:
        return False
    content = [child for child in node.children if child.type in _TEMPLATE_CONTENT]
    if not content:
        return True
    return content[0].type == "string_fragment" and node_text(source, content[0]).startswith("?")


def alternatives_spec(source: bytes, node: Node | None) -> dict[str, object] | None:
    """``cond ? "/a" : `/b/${id}```: each branch as a rooted path of its own, or ``None``.

    Only one level, and every branch must be a rooted literal or template; a
    branch that is anything else makes the whole choice unreadable. A literal
    branch keeps its path only, never its query.
    """
    if node is None:
        return None
    node = _unwrap(node)
    if node.type != "ternary_expression":
        return None
    specs: list[dict[str, object]] = []
    for name in ("consequence", "alternative"):
        branch = node.child_by_field_name(name)
        if branch is None:
            return None
        value = static_string(source, branch)
        if value is not None:
            if not _rooted(value):
                return None
            cut = min((i for i in (value.find("?"), value.find("#")) if i >= 0), default=None)
            specs.append({"string": value if cut is None else value[:cut]})
            continue
        spec = template_spec(source, branch)
        if spec is None:
            return None
        specs.append(spec)
    return {"alternatives": specs}


def decided_path(spec: dict[str, object]) -> str | None:
    """The one literal path a spec names, when nothing about it is dynamic.

    A template whose substitutions are all in its query, or a choice whose
    branches all name the same literal path, is an ordinary route: no server has
    to be consulted to spell it.
    """
    options = spec.get("alternatives", [spec])
    if not isinstance(options, list) or not options:
        return None
    paths: set[str] = set()
    for option in options:
        if not isinstance(option, dict):
            return None
        if "string" in option:
            value = option["string"]
            target = http.client_target(value) if isinstance(value, str) else None
            if target is None or target.url is not None:
                return None
            paths.add(target.path)
        elif "parts" in option:
            parts = template_parts(option.get("parts"))
            if parts is None:
                return None
            template = http.route_template(parts, truncated=option.get("truncated") is True)
            if isinstance(template, str) or template.path is None:
                return None
            paths.add(template.path)
        else:
            return None
    return paths.pop() if len(paths) == 1 else None


def template_parts(value: object) -> list[list[str]] | None:
    """Template parts read back from a claim, or ``None`` if they are malformed."""
    if not isinstance(value, list):
        return None
    parts: list[list[str]] = []
    for part in value:
        if (
            not isinstance(part, list)
            or len(part) != 2
            or not all(isinstance(item, str) for item in part)
        ):
            return None
        parts.append([part[0], part[1]])
    return parts


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
        _count_bindings(source, node, counts)
    return counts


def _count_bindings(source: bytes, node: Node, counts: Counter[str]) -> None:
    """Count each name ``node``'s own children bind: a declared name, a parameter, an
    import. Inline rather than a generator: it runs for every node of every file."""
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


#: Nodes that open a function scope of their own.
FUNCTION_NODES = frozenset(
    {
        "function_declaration",
        "generator_function_declaration",
        "function_expression",
        "function",
        "generator_function",
        "arrow_function",
        "method_definition",
    }
)
_DECLARED_FUNCTIONS = frozenset({"function_declaration", "generator_function_declaration"})
#: Subtrees an early-use scan does not enter: a function body runs later, and an
#: export list names a binding without reading it.
_EARLY_USE_SKIP = FUNCTION_NODES | {"export_clause"}


def scope_bindings(source: bytes, function: Node) -> Counter[str]:
    """Names bound in ``function``'s own scope: its parameters and the declarations
    in its body, never anything bound inside a nested function -- whose name alone, if
    it is declared, binds here."""
    key = (function.start_byte, function.end_byte)
    counts: Counter[str] = Counter()
    stack = [function]
    while stack:
        node = stack.pop()
        if (node.start_byte, node.end_byte) != key and node.type in FUNCTION_NODES:
            name = node.child_by_field_name("name")
            if node.type in _DECLARED_FUNCTIONS and name is not None:
                counts[node_text(source, name)] += 1
            continue
        _count_bindings(source, node, counts)
        stack.extend(node.children)
    return counts


def plain_object(source: bytes, node: Node) -> dict[str, Node] | None:
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
    #: ``const``, ``let`` or ``var``, as the declaration spells it.
    binding: str = "const"
    #: The index of the declaring top-level statement.
    declared_at: int = -1


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
        #: Top-level names bound once with a value: ``(statement index, value)``.
        self._strings: dict[str, tuple[int, Node]] = {}
        #: The index of each top-level statement, by its byte range.
        self._statement_at: dict[tuple[int, int], int] = {}
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
        for index, statement in enumerate(statements):
            self._statement_at[(statement.start_byte, statement.end_byte)] = index
            if statement.type == "import_statement":
                self._import(statement, counts)
            elif statement.type in {"lexical_declaration", "variable_declaration"}:
                self._declaration(statement, counts, index)
        self._refuse_early_uses(statements)
        for index, statement in enumerate(statements):
            if statement.type == "expression_statement":
                self._mount(statement, index)

    def _refuse_early_uses(self, statements: list[Node]) -> None:
        """Drop every receiver named by a top-level statement before its declaration.

        A function body runs later and an export list reads nothing, so neither is
        entered. This holds for ``const`` too: ``app.get(...)`` above ``const app =
        express()`` is a temporal-dead-zone error, not a route.
        """
        if not self._receivers:
            return
        early: set[str] = set()
        for index, statement in enumerate(statements):
            stack = [statement]
            while stack:
                node = stack.pop()
                if node.type in _EARLY_USE_SKIP:
                    continue
                if node.type in {"identifier", "shorthand_property_identifier"}:
                    name = node_text(self._source, node)
                    receiver = self._receivers.get(name)
                    if receiver is not None and receiver.declared_at > index:
                        early.add(name)
                stack.extend(node.children)
        for name in early:
            del self._receivers[name]

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

    def _declaration(self, statement: Node, counts: Counter[str], index: int) -> None:
        if statement.type == "variable_declaration":
            binding = "var"  # no kind field: the keyword is its own token
        else:
            kind = statement.child_by_field_name("kind")
            binding = node_text(self._source, kind) if kind is not None else ""
        if binding not in {"const", "let", "var"}:
            return
        for declarator in statement.children:
            if declarator.type != "variable_declarator":
                continue
            name_node = declarator.child_by_field_name("name")
            value = declarator.child_by_field_name("value")
            if name_node is None or name_node.type != "identifier" or value is None:
                continue
            name = node_text(self._source, name_node)
            if counts[name] != 1:
                continue
            self._strings[name] = (index, value)
            if value.type != "call_expression":
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
                    elif module == "axios" and binding == "const":
                        self._axios.add(name)
                elif callee_name in self._express:
                    self._receivers[name] = _Receiver(
                        _EXPRESS_APP, binding=binding, declared_at=index
                    )
                elif callee_name in self._router_factories:
                    self._receivers[name] = _Receiver(
                        _EXPRESS_ROUTER, binding=binding, declared_at=index
                    )
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
                    self._receivers[name] = _Receiver(
                        _EXPRESS_ROUTER, binding=binding, declared_at=index
                    )

    def _mount(self, statement: Node, index: int) -> None:
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
        if owner is None or node_text(self._source, prop) != "use" or owner.declared_at >= index:
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
        elif (prefix := self._path_value(first, index, 0)) is not None:
            # A prefix spelled through earlier top-level string constants.
            mounted = arguments[1:]
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
        if owner is None or prop is None:
            return []
        attribute = node_text(self._source, prop)
        if owner.type == "call_expression":
            # `app.route("/x").get(h)`: a chain link, proven back to its root.
            return self._chain_route(node, source_symbol) if attribute in _EXPRESS_VERBS else []
        if owner.type != "identifier":
            return []
        name = node_text(self._source, owner)
        if name in self._axios and self._axios_trusted and attribute in _AXIOS_VERBS:
            return self._axios_call(node, attribute, source_symbol)
        receiver = self._receivers.get(name)
        if receiver is not None and receiver.usable and attribute in _EXPRESS_VERBS:
            at = self._registration_index(node)
            if receiver.binding in _ORDERED_BINDINGS and (at is None or at <= receiver.declared_at):
                return []
            return self._route(node, receiver, _EXPRESS_VERBS[attribute], at, source_symbol)
        return []

    def _registration_index(self, call: Node) -> int | None:
        """The top-level statement index when ``call`` is that statement's expression.

        The call counts when it is the statement's whole expression, or a link of a
        member/call chain that is (``app.route("/x").get(h)``); inside a function, a
        condition, a loop or any other expression it does not, and the answer is ``None``.
        """
        node = call
        while True:
            parent = node.parent
            if parent is None:
                return None
            if parent.type == "member_expression":
                child = parent.child_by_field_name("object")
            elif parent.type == "call_expression":
                child = parent.child_by_field_name("function")
            else:
                break
            if not _same(child, node):
                break
            node = parent
        if parent.type != "expression_statement":
            return None
        grandparent = parent.parent
        if grandparent is None or grandparent.type != "program":
            return None
        return self._statement_at.get((parent.start_byte, parent.end_byte))

    def _path_value(self, node: Node | None, at: int, depth: int) -> str | None:
        """A path spelled through earlier top-level string constants, or ``None``.

        ``at`` is the top-level statement the use sits in: a constant counts only when
        it is bound once, at the top level, in a statement before it. Only string
        literals, ``+`` and template substitutions are followed; a number, ``||``, a
        call, a member read or an escape sequence is not a path this file can spell.
        """
        if node is None or depth > _MAX_PATH_DEPTH:
            return None
        literal = static_string(self._source, node)
        if literal is not None:
            return literal
        if node.type == "identifier":
            entry = self._strings.get(node_text(self._source, node))
            if entry is None or entry[0] >= at:
                return None
            return self._path_value(entry[1], entry[0], depth + 1)
        if node.type == "binary_expression":
            operator = node.child_by_field_name("operator")
            if operator is None or node_text(self._source, operator) != "+":
                return None
            left = self._path_value(node.child_by_field_name("left"), at, depth + 1)
            if left is None:
                return None
            right = self._path_value(node.child_by_field_name("right"), at, depth + 1)
            return None if right is None else left + right
        if node.type == "template_string":
            parts: list[str] = []
            for child in node.children:
                if child.type == "escape_sequence":
                    return None
                if child.type == "string_fragment":
                    text = node_text(self._source, child)
                    if "\\" in text:
                        return None
                    parts.append(text)
                elif child.type == "template_substitution":
                    inner = [c for c in child.named_children if c.type != "comment"]
                    if len(inner) != 1:
                        return None
                    value = self._path_value(inner[0], at, depth + 1)
                    if value is None:
                        return None
                    parts.append(value)
            return "".join(parts)
        return None

    def _route_path(self, argument: Node, at: int | None) -> tuple[str | None, bool]:
        """A route's path and whether it was composed from constants rather than written."""
        path = static_string(self._source, argument)
        if path is not None:
            return path, False
        if at is None:
            return None, False
        path = self._path_value(argument, at, 0)
        if path is None or not path.startswith("/"):
            return None, False
        return path, True

    def _route(
        self,
        node: Node,
        receiver: _Receiver,
        method: str,
        at: int | None,
        source_symbol: str | None,
    ) -> list[ParsedReference]:
        arguments = _arguments(node)
        # One argument is the settings getter, not a route.
        if arguments is None or len(arguments) < 2:
            return []
        path, composed_path = self._route_path(arguments[0], at)
        if path is None or any(char in _EXPRESS_PATTERN for char in path):
            return []
        start, _ = line_range(node)
        _, end = line_range(arguments[0])
        return self._served(receiver, method, path, composed_path, start, end, source_symbol)

    def _chain_route(self, node: Node, source_symbol: str | None) -> list[ParsedReference]:
        """``<receiver>.route(path).<verb>(…)…``: one route for the verb link ``node``.

        Every link back to the root must be a verb call with an argument; a link that
        is anything else (``all``, ``use``) or has none stops the chain, and nothing
        in it is a route. The root is ``route`` with exactly one argument on a proven
        receiver.
        """
        link = node
        root: Node | None = None
        root_owner: Node | None = None
        for _ in range(_MAX_CHAIN_LINKS):
            if not _arguments(link):
                return []
            callee = link.child_by_field_name("function")
            inner = callee.child_by_field_name("object") if callee is not None else None
            if inner is None or inner.type != "call_expression":
                return []
            inner_callee = inner.child_by_field_name("function")
            if inner_callee is None or inner_callee.type != "member_expression":
                return []
            inner_owner = inner_callee.child_by_field_name("object")
            inner_prop = inner_callee.child_by_field_name("property")
            if inner_owner is None or inner_prop is None:
                return []
            attribute = node_text(self._source, inner_prop)
            if attribute in _EXPRESS_VERBS:
                link = inner
                continue
            if attribute == "route" and inner_owner.type == "identifier":
                root, root_owner = inner, inner_owner
                break
            return []
        if root is None or root_owner is None:
            return []
        receiver = self._receivers.get(node_text(self._source, root_owner))
        if receiver is None or not receiver.usable:
            return []
        at = self._registration_index(node)
        if receiver.binding in _ORDERED_BINDINGS and (at is None or at <= receiver.declared_at):
            return []
        root_arguments = _arguments(root)
        if root_arguments is None or len(root_arguments) != 1:
            return []
        path, composed_path = self._route_path(root_arguments[0], at)
        if path is None or any(char in _EXPRESS_PATTERN for char in path):
            return []
        callee = node.child_by_field_name("function")
        verb = callee.child_by_field_name("property") if callee is not None else None
        if verb is None:
            return []
        method = _EXPRESS_VERBS[node_text(self._source, verb)]
        start, _ = line_range(node)
        end = max(line_range(root_arguments[0])[1], line_range(verb)[1])
        return self._served(receiver, method, path, composed_path, start, end, source_symbol)

    @staticmethod
    def _served(
        receiver: _Receiver,
        method: str,
        path: str,
        composed_path: bool,
        start: int,
        end: int,
        source_symbol: str | None,
    ) -> list[ParsedReference]:
        if receiver.kind == _EXPRESS_APP:
            served = [(http.route_path(path), composed_path)]
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
        spec = template_spec(self._source, arguments[0]) if url is None else None
        if spec is not None:
            url = decided_path(spec)
            spec = None if url is not None else spec
        _, end = line_range(arguments[0])
        method: str | None = "GET"
        if len(arguments) == 2:
            options = plain_object(self._source, arguments[1])
            method_node = options.get("method") if options is not None else None
            if options is None:
                method = None
            elif method_node is not None:
                raw = static_string(self._source, method_node)
                method = fetch_method(raw) if raw is not None else None
                end = max(end, line_range(method_node)[1])
        if spec is not None:
            # Recorded so the request is visible; never matched (see the module doc).
            start, stop = line_range(node)
            return [
                local_call_reference(
                    "fetch",
                    start,
                    stop,
                    transport=TRANSPORT_FETCH,
                    path=spec,
                    method=method,
                    in_call=True,
                    base=None,
                    source="fetch",
                    source_symbol=source_symbol,
                )
            ]
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
            config = plain_object(self._source, arguments[config_at])
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


# -- requests for the cross-repository join -----------------------------------------------

#: How much of one substitution's source text a client claim shows, for display only.
_MAX_SHOWN = 60


@dataclass(frozen=True, slots=True)
class ClientUrl:
    """A request's origin and path shape, as far as the source states them."""

    #: ``None`` for a base the source does not state (D2): the path is read, the host not.
    origin: str | None
    #: Each path segment's literal text, or ``None`` where one value fills it.
    segments: tuple[str | None, ...]
    #: The URL as written, a placeholder shown as ``{expression}``: display only.
    display: str
    #: The line of the literal the URL's base came from, when it is not the call's.
    base_at: int | None = None
    #: The library that supplied a scheme the source left out, if one did.
    scheme_from: str | None = None


def client_url(
    head: str,
    rest: Sequence[Sequence[str]],
    *,
    base_at: int | None = None,
    default_scheme: str | None = None,
    library: str | None = None,
) -> ClientUrl | None:
    """A request URL from its literal head and the template parts after it, or ``None``.

    ``head`` is the URL's leading text -- a base's value and any literal text glued
    to it -- appended to exactly as the template appends, never resolved against.
    The origin must lie entirely within it. A head with no scheme is read under
    ``default_scheme`` when the library supplies one (superagent's Node client
    requests ``http`` for a URL without a scheme); otherwise it names no origin.
    Every substitution after the origin must fill one whole path segment
    (:func:`repoatlas.models.http.route_template`).
    """
    supplied: str | None = None
    if "://" not in head:
        if default_scheme is None or not head or head.startswith("/"):
            return None
        head, supplied = f"{default_scheme}://{head}", library
    scheme = head.find("://")
    slash = head.find("/", scheme + 3)
    if slash < 0:
        if rest:
            return None
        head, slash = head + "/", len(head)
    origin = http.client_target(head[:slash] + "/")
    if origin is None or origin.origin is None:
        return None
    template = http.route_template([[http.TEMPLATE_LITERAL, head[slash:]], *rest])
    if isinstance(template, str) or not template.segments:
        return None
    expressions = iter(template.expressions)
    display = "/".join(
        segment if segment is not None else "{" + next(expressions, "")[:_MAX_SHOWN] + "}"
        for segment in template.segments
    )
    return ClientUrl(
        origin.origin, template.segments, f"{origin.origin}/{display}", base_at, supplied
    )


def unknown_base_url(base_shown: str, rest: Sequence[Sequence[str]]) -> ClientUrl | None:
    """A request whose path the template states after a base it does not (D2).

    ``rest`` is the template after the base: it must start with literal ``/`` and every
    substitution in it must fill one whole segment, as for any claim. ``None`` otherwise.
    """
    template = http.route_template(rest)
    if isinstance(template, str) or not template.segments:
        return None
    expressions = iter(template.expressions)
    display = "/".join(
        segment if segment is not None else "{" + next(expressions, "")[:_MAX_SHOWN] + "}"
        for segment in template.segments
    )
    return ClientUrl(None, template.segments, f"{{{base_shown[:_MAX_SHOWN]}}}/{display}")


def client_claim(
    read: ClientUrl,
    method: str,
    start: int,
    end: int,
    *,
    source: str,
    source_symbol: str | None,
) -> ParsedReference:
    """An :data:`~repoatlas.parsers.http_facts.HTTP_CLIENT_CALL` claim: the join's request
    side, in exactly the shape the Python parser records it."""
    meta = {
        "method": method,
        "segments": json.dumps(list(read.segments)),
        "source": source,
    }
    if read.origin is None:
        meta["base"] = BASE_UNKNOWN
    else:
        meta["origin"] = read.origin
    if read.base_at is not None:
        meta["base_at"] = str(read.base_at)
    if read.scheme_from is not None:
        meta["scheme_from"] = read.scheme_from
    return pending_reference(
        EdgeType.CALLS_HTTP_ENDPOINT,
        EntityType.HTTP_ENDPOINT,
        f"{method} {read.display}",
        start,
        end,
        role=HTTP_CLIENT_CALL,
        source_symbol=source_symbol,
        **meta,
    )


def endpoint_claims(references: Iterable[ParsedReference]) -> list[ParsedReference]:
    """A client claim for each absolute request a file's own facts already name.

    Every ``CALLS_HTTP_ENDPOINT`` fact is a literal URL a request is proven to make --
    fetch, axios, superagent, or a same-file wrapper with a stated base -- so its claim
    carries the same origin and path, every segment literal.
    """
    found: list[ParsedReference] = []
    for reference in references:
        if reference.edge_type is not EdgeType.CALLS_HTTP_ENDPOINT:
            continue
        if PENDING_FACT in reference.meta:
            continue
        method, _, url = reference.target_key.partition(" ")
        target = http.client_target(url)
        if target is None or target.url is None or target.origin is None:
            continue
        read = ClientUrl(target.origin, tuple(target.path[1:].split("/")), target.url)
        found.append(
            client_claim(
                read,
                method,
                reference.line_start,
                reference.line_end,
                source=reference.meta.get("source", ""),
                source_symbol=reference.source_symbol,
            )
        )
    return found
