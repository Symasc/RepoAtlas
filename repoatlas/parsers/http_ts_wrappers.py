"""HTTP requests made through a request wrapper, in TypeScript source (HTTP linkage).

Typed clients rarely call ``fetch`` themselves. They call a helper —
``request("/v1/courses", { auth })`` — that calls a fetch-like function with a
base URL in front of the path. Such a call is an HTTP request only when the chain
is proven from source, never when a function merely happens to be called
``request``:

    client method ─→ request helper ─→ fetch-like call ─→ method and path

**What is fetch-like.** The global ``fetch`` or ``globalThis.fetch`` (neither bound
anywhere in the file), a ``const`` alias of one, or ``a ?? b`` / ``a || b`` in which
every operand is fetch-like or a parameter of an enclosing function and at least one
is proven fetch-like. The last is the injected transport: ``suppliedFetch ??
globalThis.fetch`` is whatever the caller supplied *in place of* ``fetch``. An
injected value with no such fallback proves nothing.

**What is a wrapper.** A function whose own body — not a nested function — makes
exactly one call to something fetch-like or to another wrapper, and forwards one of
its own parameters as the path. A closure the function defines and calls is read as
part of its body, once however often it is called: a helper that retries through
``const attempt = async () => fetch(url, …)`` makes that one request. Its names
still resolve where they are written, so the URL and init it hands to ``fetch`` must
be the function's own:

* the URL is ``P``, ``${B}${P}`` or ``B + P`` for a path parameter ``P``. ``B`` is
  the base. A base the file states as a literal makes the request absolute — a
  literal, a ``const`` of one, or a property of a ``const`` object the file only
  reads. So does a deployment setting with a literal default,
  ``process.env.API_URL || "http://localhost:8000/api"``: the source requests its
  default unless the deployment says otherwise. Any other base is injected or
  computed, so the route is recorded relative to it — the same meaning a route has
  under an OpenAPI server — and no deployment or endpoint is inferred;
* the init is absent (the method is GET), one of its parameters, or ``{ ...X, … }``
  where ``X`` is that parameter or ``rest`` from ``const { a, ...rest } = init ??
  {}`` and nothing after the spread names ``method``. ``{ ...build(…), … }`` reads
  as the one object ``build`` returns, written in place.

A **requester factory** returns an object whose members are wrappers; a call through
``const { request } = createRequester(options)`` in the same file is a call to that
member. An **init helper** returns ``{ method, … }`` with ``method`` a literal or a
parameter with a literal default, so ``{ ...jsonBody(input, "PUT") }`` says PUT.

**At the call site** a literal path starting with ``/`` is a route here and now.
The method is read from the init object in order — later entries override earlier
ones — and any entry that could carry a method this cannot read makes it unknown.
Nothing is inferred from a function's name.

A path one file cannot decide is recorded as a pending claim
(:data:`~repoatlas.parsers.http_facts.HTTP_LOCAL_CALL`, ``transport: wrapper``) for
the global pass, which matches it only against routes its own repository serves:
a rooted template (``/v1/courses/${id}``, never ``${base}/x``), a choice between
rooted paths, a named value bound outside every enclosing function (a module
constant or an import — never a parameter or a local, which is how wrappers
forward paths), or a literal path whose method cannot be read. A template whose
substitutions are all in its query is decided here, as a literal. Anything else
yields nothing. A direct call through a ``fetch`` alias records only a rooted
template (``transport: fetch``), which is never matched.

**Resolution is lexical and bounded.** A name resolves to the parameter, ``const``,
function declaration or destructured ``const`` that binds it directly in an
enclosing function (or the file), and only when that is its only binding there.
Wrappers are followed at most :data:`_MAX_DEPTH` deep and never around a cycle.

**Across files, nothing is decided here.** A file declares what its exported
functions are — a requester factory, a wrapper, an init helper — and a call
through a helper imported from a relative module is recorded as a pending claim
with its arguments read as far as this file can read them. The global pass
(:mod:`repoatlas.graph.http_claims`) joins the two across exactly one import.

Parsed, never executed (V1_SPEC §17).
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from repoatlas.models import http
from repoatlas.models.parsing import EdgeType, EntityType, ParsedReference
from repoatlas.parsers.http_facts import (
    HTTP_INIT_HELPER,
    HTTP_REQUESTER,
    HTTP_REQUESTER_CALL,
    HTTP_WRAPPER,
    TRANSPORT_FETCH,
    TRANSPORT_WRAPPER,
    http_reference,
    local_call_reference,
    pending_reference,
)
from repoatlas.parsers.http_typescript import (
    alternatives_spec,
    binding_counts,
    decided_path,
    fetch_method,
    static_string,
    template_spec,
)
from repoatlas.parsers.treesitter import line_range, node_text, string_literal, walk

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tree_sitter import Node

SOURCE = "fetch-wrapper"

#: How many wrappers deep a request is followed.
_MAX_DEPTH = 3
#: How many aliases deep a fetch-like value is followed.
_MAX_ALIAS = 8
#: How much of a named path value's source text a claim keeps, for display only.
_MAX_NAMED = 80

_FUNCTIONS = frozenset(
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
_FUNCTION_VALUES = frozenset({"arrow_function", "function_expression", "function"})
_NESTED = _FUNCTIONS | {"class_declaration", "abstract_class_declaration", "class"}
_TRANSPARENT = frozenset(
    {
        "parenthesized_expression",
        "as_expression",
        "satisfies_expression",
        "non_null_expression",
        "await_expression",
    }
)

GET = "GET"


def operand(node: Node) -> Node:
    """``node`` with parentheses, ``await``, ``as``, ``satisfies`` and ``!`` looked through."""
    while node.type in _TRANSPARENT:
        inner = [child for child in node.named_children if child.type != "comment"]
        if not inner:
            break
        node = inner[0]
    return node


def arguments_of(call: Node) -> list[Node] | None:
    """A call's arguments, or ``None`` when a spread makes their positions unknown."""
    arguments = call.child_by_field_name("arguments")
    if arguments is None or arguments.type != "arguments":
        return None
    found = [child for child in arguments.named_children if child.type != "comment"]
    return None if any(child.type == "spread_element" for child in found) else found


# -- what a name is bound to ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Param:
    """A simple parameter, by position, of ``function``."""

    function: Node
    index: int


@dataclass(frozen=True, slots=True)
class Injected:
    """Bound by a parameter pattern: whatever a caller supplied."""


@dataclass(frozen=True, slots=True)
class Const:
    value: Node


@dataclass(frozen=True, slots=True)
class Func:
    function: Node


@dataclass(frozen=True, slots=True)
class Member:
    """``const { member: name } = value``."""

    member: str
    value: Node


@dataclass(frozen=True, slots=True)
class Rest:
    """``const { a, ...name } = value``, with the keys taken out before the rest."""

    taken: frozenset[str]
    value: Node


@dataclass(frozen=True, slots=True)
class Imported:
    specifier: str
    imported: str


@dataclass(frozen=True, slots=True)
class Global:
    """Bound nowhere in the file."""


Binding = Param | Injected | Const | Func | Member | Rest | Imported | Global


# -- summaries ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Wrapper:
    """What a proven wrapper does with its arguments."""

    #: The parameter that becomes the path.
    path: int
    #: The parameter the method is read from, or ``None`` when the method is fixed.
    init: int | None
    #: The method, when the wrapper decides it rather than the caller.
    method: str | None
    #: A literal base URL, or ``None`` when the base is injected or computed.
    base: str | None
    #: Whether the wrapper puts nothing before the path (a relative request).
    relative: bool


@dataclass(frozen=True, slots=True)
class InitHelper:
    """What a helper spread into an init object does to its method."""

    #: A fixed method, or ``None``.
    method: str | None
    #: The parameter the method comes from, with its literal default.
    parameter: int | None
    default: str | None
    #: The helper sets no method at all.
    silent: bool


@dataclass(frozen=True, slots=True)
class Silent:
    """What spreading a helper that sets no method does to the method: nothing."""


@dataclass(frozen=True, slots=True)
class Method:
    """The method a call makes, and whether a literal in the call decided it."""

    value: str
    in_call: bool


class FetchWrappers:
    """Requests one file proves it makes through its own request wrappers."""

    def __init__(self, source: bytes, root: Node, counts: Counter[str]) -> None:
        self._source = source
        self._root = root
        self._counts = counts
        self._scopes: dict[tuple[int, int], Counter[str]] = {}
        self._wrappers: dict[tuple[int, int], Wrapper | None] = {}
        self._reading: set[tuple[int, int]] = set()
        self._factories: dict[tuple[int, int], dict[str, Wrapper]] = {}
        self._helpers: dict[tuple[int, int], InitHelper | None] = {}
        #: Init builders being read, so one that spreads itself is never followed round.
        self._building: set[tuple[int, int]] = set()
        #: Whether each ``const`` object a base is read from is only ever read.
        self._read_only: dict[str, bool] = {}
        self._imports: dict[str, Imported] = {}
        for statement in root.children:
            if statement.type == "import_statement":
                self._read_import(statement)

    # -- call sites ---------------------------------------------------------------------

    def call_references(self, node: Node, source_symbol: str | None) -> list[ParsedReference]:
        """The request a call to a same-file wrapper provably makes."""
        callee = node.child_by_field_name("function")
        if callee is None or operand(callee).type != "identifier":
            return []
        # A call spelled `fetch` is left to the rule that has always read it.
        if node_text(self._source, operand(callee)) != "fetch" and self.fetch_like(callee):
            return self.facts(node, _ALIAS, source_symbol, source="fetch")
        wrapper = self.resolve_callee(node)
        if wrapper is None:
            return self.imported_call(node, source_symbol)
        return self.facts(node, wrapper, source_symbol)

    def resolve_callee(self, call: Node) -> Wrapper | None:
        callee = call.child_by_field_name("function")
        if callee is None:
            return None
        callee = operand(callee)
        if callee.type != "identifier":
            return None
        binding = self.lookup(node_text(self._source, callee), call)
        function = self.function_of(binding)
        if function is not None:
            return self.wrapper(function)
        if isinstance(binding, Member):
            factory_call = operand(binding.value)
            if factory_call.type != "call_expression":
                return None
            factory = factory_call.child_by_field_name("function")
            if factory is None or operand(factory).type != "identifier":
                return None
            origin = self.function_of(
                self.lookup(node_text(self._source, operand(factory)), factory)
            )
            return self.factory(origin).get(binding.member) if origin is not None else None
        return None

    def facts(
        self,
        call: Node,
        wrapper: Wrapper,
        source_symbol: str | None,
        *,
        source: str = SOURCE,
    ) -> list[ParsedReference]:
        arguments = arguments_of(call)
        if arguments is None or len(arguments) <= wrapper.path:
            return []
        if wrapper is _ALIAS and len(arguments) > 2:
            return []
        path_node = arguments[wrapper.path]
        path = static_string(self._source, path_node)
        spec: dict[str, Any] | None = None
        if path is None:
            spec = self._undecided_path(call, path_node, wrapper)
            if spec is None:
                return []
            path = decided_path(spec)
            spec = None if path is not None else spec
        # A wrapper appends the path to a base, so only a path is meaningful there; a
        # `fetch` alias takes a whole URL, as `fetch` itself does.
        if path is not None and wrapper is not _ALIAS and not path.startswith("/"):
            return []
        end = line_range(path_node)[1]
        if wrapper.init is None:
            method: Method | None = Method(wrapper.method or GET, in_call=wrapper.method is None)
        elif len(arguments) <= wrapper.init:
            method = Method(GET, in_call=True)
        else:
            method = self.init_method(arguments[wrapper.init])
            end = max(end, line_range(arguments[wrapper.init])[1])
        if path is not None:
            if method is not None:
                return self.request(call, method, path, wrapper, end, source_symbol, source)
            if wrapper is _ALIAS:
                return []
            # A route whose method this file cannot read: visible, never guessed.
            spec = {"string": path}
        if spec is None:
            return []
        callee = call.child_by_field_name("function")
        start, stop = line_range(call)
        return [
            local_call_reference(
                node_text(self._source, operand(callee)) if callee is not None else "",
                start,
                stop,
                transport=TRANSPORT_FETCH if wrapper is _ALIAS else TRANSPORT_WRAPPER,
                path=spec,
                method=method.value if method is not None else None,
                in_call=method.in_call if method is not None else False,
                base=wrapper.base,
                source=source,
                source_symbol=source_symbol,
            )
        ]

    def _undecided_path(self, call: Node, node: Node, wrapper: Wrapper) -> dict[str, Any] | None:
        """A path argument one file cannot decide, as a claim records it, or ``None``.

        Through a ``fetch`` alias only a rooted template is recorded; through a
        proven wrapper also a choice of rooted paths and a named value.
        """
        spec = template_spec(self._source, node)
        if spec is not None or wrapper is _ALIAS:
            return spec
        spec = alternatives_spec(self._source, node)
        if spec is not None:
            return spec
        named = self._named(operand(node), call)
        return {"named": named} if named is not None else None

    def _named(self, node: Node, at: Node) -> str | None:
        """``PATHS.request``: a value bound outside every enclosing function, or ``None``.

        A parameter or a local is how a wrapper forwards a path it was given, so it
        says nothing about which path; a module constant or an import names one
        value this file cannot read, which is worth keeping visible as unresolved.
        """
        if node.type not in {"identifier", "member_expression"}:
            return None
        root = node
        while root.type == "member_expression":
            owner = root.child_by_field_name("object")
            if owner is None:
                return None
            root = operand(owner)
        if root.type != "identifier":
            return None
        binding = self.lookup(node_text(self._source, root), at)
        if isinstance(binding, (Imported, Global)):
            outside = True
        elif isinstance(binding, (Const, Member, Rest)):
            outside = _enclosing_function(binding.value) is None
        elif isinstance(binding, Func):
            outside = _enclosing_function(binding.function) is None
        else:
            outside = False
        return node_text(self._source, node)[:_MAX_NAMED] if outside else None

    def request(
        self,
        call: Node,
        method: Method,
        path: str,
        wrapper: Wrapper,
        end: int,
        source_symbol: str | None,
        source: str = SOURCE,
    ) -> list[ParsedReference]:
        """The route (and endpoint, when the base is literal) this request names."""
        if wrapper.base is not None:
            target = http.client_target(f"{wrapper.base}{path}")
        else:
            target = http.client_target(path)
        if target is None:
            return []
        start = line_range(call)[0]
        composed = not method.in_call or wrapper.base is not None
        found = [
            http_reference(
                EdgeType.CALLS_HTTP_ROUTE,
                EntityType.HTTP_ROUTE,
                http.route_body(method.value, target.path),
                start,
                end,
                source=source,
                composed=composed,
                source_symbol=source_symbol,
            )
        ]
        if target.url is not None:
            found.insert(
                0,
                http_reference(
                    EdgeType.CALLS_HTTP_ENDPOINT,
                    EntityType.HTTP_ENDPOINT,
                    http.endpoint_body(method.value, target.url),
                    start,
                    end,
                    source=source,
                    composed=composed,
                    source_symbol=source_symbol,
                ),
            )
        return found

    def init_method(self, init: Node) -> Method | None:
        """The method an init object at a call site provably carries, or ``None``."""
        entries = self.init_entries(init)
        return evaluate(entries, lambda _: None) if entries is not None else None

    def init_entries(self, init: Node) -> list[dict[str, Any]] | None:
        """An init object as data: what each entry does to the method, in order.

        ``{"method": m, "in_call": b}`` sets it, ``{"unknown": why}`` makes it
        unknowable, ``{"helper": …}`` is a spread of an imported helper for the
        global pass to read. Entries that cannot touch the method are left out.
        """
        init = operand(init)
        if init.type != "object":
            return None
        entries: list[dict[str, Any]] = []
        for entry in init.named_children:
            if entry.type == "comment":
                continue
            if entry.type == "pair":
                key = self.key(entry)
                if key is None:
                    entries.append({"unknown": "a computed key"})
                elif key == "method":
                    value = entry.child_by_field_name("value")
                    raw = static_string(self._source, value) if value is not None else None
                    normalized = fetch_method(raw) if raw is not None else None
                    entries.append(
                        {"method": normalized, "in_call": True}
                        if normalized is not None
                        else {"unknown": "a method that is not a literal"}
                    )
            elif entry.type == "shorthand_property_identifier":
                if node_text(self._source, entry) == "method":
                    entries.append({"unknown": "a method that is not a literal"})
            elif entry.type == "spread_element":
                entries.append(self._spread(entry))
            elif entry.type == "method_definition":
                name = entry.child_by_field_name("name")
                if name is None or node_text(self._source, name) == "method":
                    entries.append({"unknown": "a method that is not a literal"})
            else:
                entries.append({"unknown": entry.type})
        return entries

    def _spread(self, spread: Node) -> dict[str, Any]:
        """What ``...helper(args)`` does to the method, as far as this file can say."""
        inner = next((c for c in spread.named_children if c.type != "comment"), None)
        call = operand(inner) if inner is not None else None
        if call is None or call.type != "call_expression":
            return {"unknown": "a spread of something that is not a helper call"}
        callee = call.child_by_field_name("function")
        if callee is None or operand(callee).type != "identifier":
            return {"unknown": "a spread of something that is not a helper call"}
        binding = self.lookup(node_text(self._source, operand(callee)), call)
        arguments = arguments_of(call)
        values = [static_string(self._source, a) for a in arguments] if arguments else []
        if isinstance(binding, Imported) and is_relative(binding.specifier):
            return {
                "helper": {
                    "specifier": binding.specifier,
                    "imported": binding.imported,
                    "arguments": values if arguments is not None else None,
                }
            }
        function = self.function_of(binding)
        helper = self.init_helper(function) if function is not None else None
        effect = apply_helper(helper, values if arguments is not None else None) if helper else None
        if isinstance(effect, Method):
            return {"method": effect.value, "in_call": effect.in_call}
        if isinstance(effect, Silent):
            return {"silent": True}
        return {"unknown": "a spread helper this file cannot read"}

    # -- across files --------------------------------------------------------------------

    def declarations(self) -> list[ParsedReference]:
        """What this file's exported functions are, for callers in other files."""
        found: list[ParsedReference] = []
        for name, function in self._exported_functions():
            start, end = line_range(function)
            members = self.factory(function)
            if members:
                found.append(
                    pending_reference(
                        EdgeType.REFERENCES,
                        EntityType.SYMBOL,
                        name,
                        start,
                        end,
                        role=HTTP_REQUESTER,
                        source_symbol=name,
                        members=json.dumps(
                            {member: wrapper_meta(w) for member, w in sorted(members.items())}
                        ),
                    )
                )
            wrapper = self.wrapper(function)
            if wrapper is not None:
                found.append(
                    pending_reference(
                        EdgeType.REFERENCES,
                        EntityType.SYMBOL,
                        name,
                        start,
                        end,
                        role=HTTP_WRAPPER,
                        source_symbol=name,
                        wrapper=json.dumps(wrapper_meta(wrapper)),
                    )
                )
            helper = self.init_helper(function)
            if helper is not None and not helper.silent:
                found.append(
                    pending_reference(
                        EdgeType.REFERENCES,
                        EntityType.SYMBOL,
                        name,
                        start,
                        end,
                        role=HTTP_INIT_HELPER,
                        source_symbol=name,
                        helper=json.dumps(helper_meta(helper)),
                    )
                )
        return found

    def _exported_functions(self) -> list[tuple[str, Node]]:
        """``export function f`` and ``export const f = …``: nothing re-exported."""
        found: list[tuple[str, Node]] = []
        for statement in self._root.children:
            declaration = (
                statement.child_by_field_name("declaration")
                if statement.type == "export_statement"
                else None
            )
            if declaration is None:
                continue
            if declaration.type == "function_declaration":
                name = declaration.child_by_field_name("name")
                if name is not None:
                    found.append((node_text(self._source, name), declaration))
            elif declaration.type == "lexical_declaration":
                for declarator in declaration.named_children:
                    name = declarator.child_by_field_name("name")
                    value = declarator.child_by_field_name("value")
                    if (
                        name is not None
                        and value is not None
                        and name.type == "identifier"
                        and operand(value).type in _FUNCTION_VALUES
                    ):
                        found.append((node_text(self._source, name), operand(value)))
        return found

    def imported_call(self, call: Node, source_symbol: str | None) -> list[ParsedReference]:
        """A call through a helper imported from a relative module, as a pending claim.

        Recorded only when some argument is written as a path — a literal, a
        template or a choice of them, starting with ``/`` — so an ordinary call to an
        imported function is not turned into a claim about HTTP it never makes. A
        template is recorded as its parts, never as a guessed route: the global pass
        matches it only against routes the repository serves.
        """
        callee = call.child_by_field_name("function")
        if callee is None or operand(callee).type != "identifier":
            return []
        binding = self.lookup(node_text(self._source, operand(callee)), call)
        target: dict[str, str] = {}
        if isinstance(binding, Imported) and is_relative(binding.specifier):
            target = {"specifier": binding.specifier, "wrapper": binding.imported}
        elif isinstance(binding, Member):
            factory_call = operand(binding.value)
            factory = (
                factory_call.child_by_field_name("function")
                if factory_call.type == "call_expression"
                else None
            )
            if factory is None or operand(factory).type != "identifier":
                return []
            origin = self.lookup(node_text(self._source, operand(factory)), factory)
            if not isinstance(origin, Imported) or not is_relative(origin.specifier):
                return []
            target = {
                "specifier": origin.specifier,
                "factory": origin.imported,
                "member": binding.member,
            }
        else:
            return []
        arguments = arguments_of(call)
        if arguments is None:
            return []
        summaries = [self._argument(argument) for argument in arguments]
        if not any(
            str(s.get("string", "")).startswith("/") or "parts" in s or "alternatives" in s
            for s in summaries
        ):
            return []
        start, end = line_range(call)
        return [
            pending_reference(
                EdgeType.CALLS_HTTP_ROUTE,
                EntityType.HTTP_ROUTE,
                node_text(self._source, operand(callee)),
                start,
                end,
                role=HTTP_REQUESTER_CALL,
                source_symbol=source_symbol,
                arguments=json.dumps(summaries),
                **target,
            )
        ]

    def _argument(self, node: Node) -> dict[str, Any]:
        value = static_string(self._source, node)
        if value is not None:
            return {"string": value}
        spec = template_spec(self._source, operand(node)) or alternatives_spec(
            self._source, operand(node)
        )
        if spec is not None:
            return spec
        entries = self.init_entries(node)
        return {"init": entries} if entries is not None else {}

    # -- names ---------------------------------------------------------------------------

    def lookup(self, name: str, at: Node) -> Binding | None:
        """The one binding ``name`` has where ``at`` uses it, or ``None``."""
        scope = _enclosing_function(at)
        while scope is not None:
            counts = self._scope_counts(scope)
            if counts[name]:
                return self._direct(scope, name) if counts[name] == 1 else None
            scope = _enclosing_function(scope)
        if self._counts[name] == 0:
            return Global()
        if self._counts[name] != 1:
            return None
        if name in self._imports:
            return self._imports[name]
        return self._direct(self._root, name)

    def function_of(self, binding: Binding | None) -> Node | None:
        if isinstance(binding, Func):
            return binding.function
        if isinstance(binding, Const):
            value = operand(binding.value)
            return value if value.type in _FUNCTION_VALUES else None
        return None

    def _scope_counts(self, scope: Node) -> Counter[str]:
        key = (scope.start_byte, scope.end_byte)
        found = self._scopes.get(key)
        if found is None:
            found = binding_counts(self._source, scope)
            self._scopes[key] = found
        return found

    def _direct(self, scope: Node, name: str) -> Binding | None:
        """How ``scope`` itself binds ``name``: a parameter or a direct statement."""
        for index, parameter in enumerate(_parameters(scope)):
            pattern = parameter.child_by_field_name("pattern") if parameter.is_named else None
            if parameter.type == "identifier":
                pattern = parameter
            if pattern is None:
                continue
            if pattern.type == "identifier":
                if node_text(self._source, pattern) == name:
                    return Param(scope, index)
            elif name in self._pattern_names(pattern):
                return Injected()
        body = scope if scope.type == "program" else scope.child_by_field_name("body")
        if body is None or (scope.type != "program" and body.type != "statement_block"):
            return None
        for statement in body.named_children:
            if statement.type == "export_statement":
                statement = statement.child_by_field_name("declaration") or statement
            found = self._statement(statement, name)
            if found is not None:
                return found
        return None

    def _statement(self, statement: Node, name: str) -> Binding | None:
        if statement.type == "function_declaration":
            declared = statement.child_by_field_name("name")
            if declared is not None and node_text(self._source, declared) == name:
                return Func(statement)
            return None
        if statement.type != "lexical_declaration":
            return None
        kind = statement.child_by_field_name("kind")
        if kind is None or node_text(self._source, kind) != "const":
            return None
        for declarator in statement.named_children:
            if declarator.type != "variable_declarator":
                continue
            pattern = declarator.child_by_field_name("name")
            value = declarator.child_by_field_name("value")
            if pattern is None or value is None:
                continue
            if pattern.type == "identifier":
                if node_text(self._source, pattern) == name:
                    return Const(value)
            elif pattern.type == "object_pattern":
                found = self._destructured(pattern, value, name)
                if found is not None:
                    return found
        return None

    def _destructured(self, pattern: Node, value: Node, name: str) -> Binding | None:
        taken: set[str] = set()
        rest: str | None = None
        member: str | None = None
        for child in pattern.named_children:
            if child.type == "shorthand_property_identifier_pattern":
                key = node_text(self._source, child)
                taken.add(key)
                if key == name:
                    member = key
            elif child.type == "pair_pattern":
                key_node = child.child_by_field_name("key")
                bound = child.child_by_field_name("value")
                if key_node is None or key_node.type != "property_identifier":
                    return None  # a computed key could take anything out
                key = node_text(self._source, key_node)
                taken.add(key)
                if (
                    bound is not None
                    and bound.type == "identifier"
                    and node_text(self._source, bound) == name
                ):
                    member = key
            elif child.type == "rest_pattern":
                inner = next((c for c in child.named_children if c.type == "identifier"), None)
                rest = node_text(self._source, inner) if inner is not None else None
            elif child.type == "object_assignment_pattern":
                left = child.child_by_field_name("left")
                if left is not None:
                    key = node_text(self._source, left)
                    taken.add(key)
                    if key == name:
                        return None  # a default makes the value unknowable
        if member is not None:
            return Member(member, value)
        if rest == name:
            return Rest(frozenset(taken), value)
        return None

    def _pattern_names(self, pattern: Node) -> set[str]:
        return {
            node_text(self._source, node)
            for node in walk(pattern)
            if node.type in {"identifier", "shorthand_property_identifier_pattern"}
        }

    def _read_import(self, statement: Node) -> None:
        if any(child.type == "type" for child in statement.children):
            return
        source_node = statement.child_by_field_name("source")
        specifier = string_literal(self._source, source_node) if source_node else None
        clause = next((c for c in statement.children if c.type == "import_clause"), None)
        if not specifier or clause is None:
            return
        for child in clause.children:
            if child.type != "named_imports":
                continue
            for spec in child.children:
                if spec.type != "import_specifier" or any(c.type == "type" for c in spec.children):
                    continue
                imported = spec.child_by_field_name("name")
                alias = spec.child_by_field_name("alias") or imported
                if imported is not None and alias is not None:
                    self._imports[node_text(self._source, alias)] = Imported(
                        specifier, node_text(self._source, imported)
                    )

    def key(self, entry: Node) -> str | None:
        key = entry.child_by_field_name("key")
        if key is None:
            return None
        if key.type == "property_identifier":
            return node_text(self._source, key)
        if key.type == "string":
            return string_literal(self._source, key)
        return None

    # -- what is fetch-like --------------------------------------------------------------

    def fetch_like(self, expression: Node, depth: int = 0) -> bool:
        """Whether ``expression`` is proven to be ``fetch`` or used in its place."""
        return self._transport(expression, depth) == _FETCH

    def _transport(self, expression: Node, depth: int) -> str | None:
        if depth > _MAX_ALIAS:
            return None
        node = operand(expression)
        if node.type == "identifier":
            name = node_text(self._source, node)
            binding = self.lookup(name, node)
            if isinstance(binding, Global):
                return _FETCH if name == "fetch" else None
            if isinstance(binding, Const):
                return self._transport(binding.value, depth + 1)
            if isinstance(binding, (Param, Injected)):
                return _INJECTED
            return None
        if node.type == "member_expression":
            owner = node.child_by_field_name("object")
            prop = node.child_by_field_name("property")
            if (
                owner is not None
                and prop is not None
                and operand(owner).type == "identifier"
                and node_text(self._source, operand(owner)) == "globalThis"
                and isinstance(self.lookup("globalThis", owner), Global)
                and node_text(self._source, prop) == "fetch"
            ):
                return _FETCH
            return None
        if node.type == "binary_expression":
            operator = node.child_by_field_name("operator")
            if operator is None or node_text(self._source, operator) not in {"??", "||"}:
                return None
            sides = [node.child_by_field_name("left"), node.child_by_field_name("right")]
            kinds = [
                self._transport(side, depth + 1) if side is not None else None for side in sides
            ]
            if None in kinds or _FETCH not in kinds:
                return None
            return _FETCH
        return None

    # -- wrappers ------------------------------------------------------------------------

    def wrapper(self, function: Node, depth: int = 0) -> Wrapper | None:
        key = (function.start_byte, function.end_byte)
        if key in self._wrappers:
            return self._wrappers[key]
        if key in self._reading or depth > _MAX_DEPTH:
            return None
        self._reading.add(key)
        try:
            found = self._read_wrapper(function, depth)
        except _Cycle:
            found = None
        finally:
            self._reading.discard(key)
        self._wrappers[key] = found
        return found

    def _read_wrapper(self, function: Node, depth: int) -> Wrapper | None:
        transports: list[tuple[Node, Wrapper | None]] = []
        # closures read as part of this body, each once however often it is called:
        # a retry makes the same request again, not a second one
        inlined: set[tuple[int, int]] = set()
        calls = _own_calls(function)
        while calls:
            call = calls.pop()
            callee = call.child_by_field_name("function")
            if callee is None or operand(callee).type != "identifier":
                continue
            if self.fetch_like(callee):
                transports.append((call, None))
                continue
            inner = self.function_of(self.lookup(node_text(self._source, operand(callee)), call))
            if inner is None:
                continue
            key = (inner.start_byte, inner.end_byte)
            if key in self._reading:
                raise _Cycle  # a wrapper that reaches itself proves nothing
            nested = self.wrapper(inner, depth + 1)
            if nested is not None:
                transports.append((call, nested))
            elif key not in inlined and _enclosing_function(inner) == function:
                # a closure the wrapper defines and calls is its own body, read in place
                inlined.add(key)
                calls.extend(_own_calls(inner))
        if len(transports) != 1:
            return None
        call, nested = transports[0]
        arguments = arguments_of(call)
        if arguments is None:
            return None
        if nested is None:
            return self._over_fetch(function, arguments)
        return self._over_wrapper(function, arguments, nested)

    def _over_fetch(self, function: Node, arguments: list[Node]) -> Wrapper | None:
        if not 1 <= len(arguments) <= 2:
            return None
        url = self._url(function, arguments[0])
        if url is None:
            return None
        path, base, relative = url
        if len(arguments) == 1:
            return Wrapper(path, None, GET, base, relative)
        init = self._init(function, arguments[1])
        if init is None:
            return None
        index, method = init
        return Wrapper(path, index, method, base, relative)

    def _over_wrapper(
        self, function: Node, arguments: list[Node], nested: Wrapper
    ) -> Wrapper | None:
        if len(arguments) <= nested.path:
            return None
        path = self._own_parameter(function, arguments[nested.path])
        if path is None:
            return None
        if nested.init is None:
            return Wrapper(path, None, nested.method, nested.base, nested.relative)
        if len(arguments) <= nested.init:
            return Wrapper(path, None, GET, nested.base, nested.relative)
        init = self._own_parameter(function, arguments[nested.init])
        if init is None:
            return None
        return Wrapper(path, init, None, nested.base, nested.relative)

    def _own_parameter(self, function: Node, node: Node) -> int | None:
        node = operand(node)
        if node.type != "identifier":
            return None
        binding = self.lookup(node_text(self._source, node), node)
        if isinstance(binding, Param) and binding.function == function:
            return binding.index
        return None

    def _url(self, function: Node, node: Node) -> tuple[int, str | None, bool] | None:
        """``(path parameter, literal base, relative)`` for a wrapper's URL argument."""
        node = operand(node)
        own = self._own_parameter(function, node)
        if own is not None:
            return own, None, True
        if node.type == "identifier":
            binding = self.lookup(node_text(self._source, node), node)
            if isinstance(binding, Const) and _enclosing_function(binding.value) == function:
                return self._url(function, binding.value)
            return None
        if node.type == "template_string":
            parts = [c for c in node.named_children if c.type != "comment"]
            if len(parts) != 2 or any(p.type != "template_substitution" for p in parts):
                return None
            base, path = (next(iter(p.named_children), None) for p in parts)
        elif node.type == "binary_expression":
            operator = node.child_by_field_name("operator")
            if operator is None or node_text(self._source, operator) != "+":
                return None
            base, path = node.child_by_field_name("left"), node.child_by_field_name("right")
        else:
            return None
        if base is None or path is None:
            return None
        index = self._own_parameter(function, path)
        if index is None or self._mentions_parameter(base, function):
            return None
        return index, self._literal(base), False

    def _literal(self, node: Node, depth: int = 0) -> str | None:
        """A base the file states as a literal, or ``None`` for any other base.

        A literal, a ``const`` bound to one, or a property of a ``const`` object that
        the file only ever reads. A deployment setting with a literal default —
        ``process.env.API_URL || "http://localhost:8000/api"`` — is its default: the
        URL this source requests unless its deployment says otherwise.
        """
        node = operand(node)
        value = static_string(self._source, node)
        if value is not None or depth > _MAX_ALIAS:
            return value
        if node.type == "identifier":
            binding = self.lookup(node_text(self._source, node), node)
            return self._literal(binding.value, depth + 1) if isinstance(binding, Const) else None
        if node.type == "member_expression":
            value_node = self._const_property(node)
            return self._literal(value_node, depth + 1) if value_node is not None else None
        if node.type == "binary_expression":
            operator = node.child_by_field_name("operator")
            setting = node.child_by_field_name("left")
            default = node.child_by_field_name("right")
            if (
                operator is None
                or node_text(self._source, operator) not in {"||", "??"}
                or setting is None
                or default is None
                or not self._setting(setting)
            ):
                return None
            return self._literal(default, depth + 1)
        return None

    def _setting(self, node: Node) -> bool:
        """``process.env.NAME``, ``process.env["NAME"]`` or ``import.meta.env.NAME``."""
        node = operand(node)
        if node.type not in {"member_expression", "subscript_expression"}:
            return False
        if node.type == "subscript_expression":
            index = node.child_by_field_name("index")
            if index is None or static_string(self._source, operand(index)) is None:
                return False
        env = node.child_by_field_name("object")
        if env is None or operand(env).type != "member_expression":
            return False
        holder = operand(env).child_by_field_name("object")
        prop = operand(env).child_by_field_name("property")
        if holder is None or prop is None or node_text(self._source, prop) != "env":
            return False
        holder = operand(holder)
        if holder.type == "meta_property":
            return node_text(self._source, holder) == "import.meta"
        return (
            holder.type == "identifier"
            and node_text(self._source, holder) == "process"
            and isinstance(self.lookup("process", holder), Global)
        )

    def _const_property(self, node: Node) -> Node | None:
        """``A.b``'s value, for ``const A = { b: … }`` read and never written in this file."""
        owner = node.child_by_field_name("object")
        prop = node.child_by_field_name("property")
        if (
            owner is None
            or prop is None
            or operand(owner).type != "identifier"
            or prop.type != "property_identifier"
        ):
            return None
        name = node_text(self._source, operand(owner))
        binding = self.lookup(name, node)
        if not isinstance(binding, Const) or operand(binding.value).type != "object":
            return None
        key = node_text(self._source, prop)
        found: Node | None = None
        for entry in operand(binding.value).named_children:
            if entry.type == "comment":
                continue
            if entry.type == "spread_element":
                return None  # a spread can set any key
            if entry.type == "pair":
                entry_key = self.key(entry)
                if entry_key is None:
                    return None  # a computed key can be any key
                if entry_key == key:
                    if found is not None:
                        return None
                    found = entry.child_by_field_name("value")
            elif node_text(self._source, entry.child_by_field_name("name") or entry) == key:
                return None  # a shorthand or a method under that name
        if found is None or not self._only_read(name):
            return None
        return found

    def _only_read(self, name: str) -> bool:
        """Whether every use of ``name`` in the file reads a property of it.

        Anything else — an assignment to a property, an argument, an export by name —
        hands the object to code that could change it.
        """
        cached = self._read_only.get(name)
        if cached is not None:
            return cached
        read = True
        for node in walk(self._root):
            if node.type not in {"identifier", "shorthand_property_identifier"}:
                continue
            if node_text(self._source, node) != name:
                continue
            parent = node.parent
            if (
                parent is not None
                and parent.type == "variable_declarator"
                and parent.child_by_field_name("name") == node
            ):
                continue  # the declaration itself
            member = parent
            while member is not None and member.type in _TRANSPARENT:
                member = member.parent
            target = member.parent if member is not None else None
            if (
                node.type != "identifier"
                or member is None
                or member.type not in {"member_expression", "subscript_expression"}
                or operand(member.child_by_field_name("object") or member) != node
                or target is None
                or target.type == "update_expression"
                or (target.type == "unary_expression" and target.children[0].type == "delete")
                or (
                    target.type in {"assignment_expression", "augmented_assignment_expression"}
                    and target.child_by_field_name("left") == member
                )
            ):
                read = False
                break
        self._read_only[name] = read
        return read

    def _mentions_parameter(self, node: Node, function: Node) -> bool:
        return any(
            child.type == "identifier" and self._own_parameter(function, child) is not None
            for child in walk(node)
        )

    def _init(self, function: Node, node: Node) -> tuple[int | None, str | None] | None:
        """``(init parameter, fixed method)`` for a wrapper's init argument."""
        own = self._own_parameter(function, node)
        if own is not None:
            return own, None
        node = operand(node)
        if node.type != "object":
            return None
        forwarded: int | None = None
        method: str | None = GET
        for entry in node.named_children:
            if entry.type == "comment":
                continue
            if entry.type == "spread_element":
                if forwarded is not None or method != GET:
                    return None
                inner = next((c for c in entry.named_children if c.type != "comment"), None)
                if inner is None:
                    return None
                built = self._built(function, inner)
                if built is not None:
                    forwarded, method = built
                    continue
                forwarded = self._forwarded(function, inner)
                if forwarded is None:
                    return None
                method = None
            elif entry.type == "pair":
                key = self.key(entry)
                if key is None:
                    return None
                if key == "method":
                    if forwarded is not None:
                        return None
                    value = entry.child_by_field_name("value")
                    raw = static_string(self._source, value) if value is not None else None
                    method = fetch_method(raw) if raw is not None else None
                    if method is None:
                        return None
            elif entry.type == "shorthand_property_identifier":
                if node_text(self._source, entry) == "method":
                    return None
            else:
                return None
        return forwarded, method

    def _built(self, function: Node, node: Node) -> tuple[int | None, str | None] | None:
        """``builder(…)``'s init, when ``builder`` returns one object: read in place, as
        if its entries were written here. Its names resolve where it is written, so
        only a closure of ``function`` can forward what the wrapper was given."""
        node = operand(node)
        if node.type != "call_expression":
            return None
        callee = node.child_by_field_name("function")
        if callee is None or operand(callee).type != "identifier":
            return None
        builder = self.function_of(self.lookup(node_text(self._source, operand(callee)), node))
        if builder is None:
            return None
        key = (builder.start_byte, builder.end_byte)
        objects = _returned_objects(builder)
        if key in self._building or len(objects) != 1:
            return None
        self._building.add(key)
        try:
            return self._init(function, objects[0])
        finally:
            self._building.discard(key)

    def _forwarded(self, function: Node, node: Node) -> int | None:
        """The init parameter ``node`` passes on unchanged in its method, if any."""
        own = self._own_parameter(function, node)
        if own is not None:
            return own
        node = operand(node)
        if node.type != "identifier":
            return None
        binding = self.lookup(node_text(self._source, node), node)
        if not isinstance(binding, Rest) or "method" in binding.taken:
            return None
        value = operand(binding.value)
        if value.type == "binary_expression":
            operator = value.child_by_field_name("operator")
            right = value.child_by_field_name("right")
            if (
                operator is None
                or node_text(self._source, operator) not in {"??", "||"}
                or right is None
                or operand(right).type != "object"
                or operand(right).named_children
            ):
                return None
            left = value.child_by_field_name("left")
            value = operand(left) if left is not None else value
        return self._own_parameter(function, value)

    # -- factories and helpers -----------------------------------------------------------

    def factory(self, function: Node) -> dict[str, Wrapper]:
        """The wrapper members of the one object ``function`` returns."""
        key = (function.start_byte, function.end_byte)
        if key in self._factories:
            return self._factories[key]
        self._factories[key] = {}
        returned = _returned_objects(function)
        members: dict[str, Wrapper] = {}
        if len(returned) == 1:
            for entry in returned[0].named_children:
                if entry.type == "method_definition":
                    name = entry.child_by_field_name("name")
                    member = node_text(self._source, name) if name is not None else None
                    target: Node | None = entry
                elif entry.type == "pair":
                    member = self.key(entry)
                    value = entry.child_by_field_name("value")
                    target = operand(value) if value is not None else None
                    if target is not None and target.type == "identifier":
                        target = self.function_of(
                            self.lookup(node_text(self._source, target), target)
                        )
                    elif target is not None and target.type not in _FUNCTION_VALUES:
                        target = None
                else:
                    continue
                if member is None or target is None or member in members:
                    continue
                found = self.wrapper(target)
                if found is not None:
                    members[member] = found
        self._factories[key] = members
        return members

    def init_helper(self, function: Node) -> InitHelper | None:
        key = (function.start_byte, function.end_byte)
        if key not in self._helpers:
            self._helpers[key] = self._read_helper(function)
        return self._helpers[key]

    def _read_helper(self, function: Node) -> InitHelper | None:
        returned = _returned_objects(function)
        if len(returned) != 1:
            return None
        parameters = _parameters(function)
        entry_method: Node | None = None
        for entry in returned[0].named_children:
            if entry.type == "comment":
                continue
            if entry.type == "spread_element":
                return None
            if entry.type == "shorthand_property_identifier":
                if node_text(self._source, entry) == "method":
                    entry_method = entry
            elif entry.type == "pair":
                key = self.key(entry)
                if key is None:
                    return None
                if key == "method":
                    entry_method = entry.child_by_field_name("value")
            elif entry.type != "method_definition":
                return None
        if entry_method is None:
            return InitHelper(None, None, None, silent=True)
        raw = static_string(self._source, entry_method)
        if raw is not None:
            fixed = fetch_method(raw)
            return InitHelper(fixed, None, None, silent=False) if fixed else None
        binding = self.lookup(node_text(self._source, entry_method), entry_method)
        if not isinstance(binding, Param) or binding.function != function:
            return None
        parameter = parameters[binding.index]
        default = parameter.child_by_field_name("value") if parameter.is_named else None
        literal = static_string(self._source, default) if default is not None else None
        return InitHelper(None, binding.index, literal, silent=False)


class _Cycle(Exception):
    """A wrapper was reached again while it was being read."""


#: The two outcomes of classifying a transport operand.
_FETCH = "fetch"
_INJECTED = "injected"
#: A ``fetch`` alias called directly: the URL and the init are its own arguments.
_ALIAS = Wrapper(path=0, init=1, method=None, base=None, relative=True)


def is_relative(specifier: str) -> bool:
    """A module the linker can resolve inside the importing repository."""
    return specifier.startswith(("./", "../"))


def evaluate(
    entries: list[dict[str, Any]], helper: Callable[[dict[str, Any]], Method | Silent | None]
) -> Method | None:
    """The method an init object's entries leave, reading them in order.

    ``helper`` reads an imported helper's entry; returning ``None`` means unknown.
    """
    method = Method(GET, in_call=True)
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        if "unknown" in entry:
            return None
        if "method" in entry:
            value = entry["method"]
            if not isinstance(value, str) or fetch_method(value) != value:
                return None
            method = Method(value, in_call=bool(entry.get("in_call")))
        elif "helper" in entry:
            effect = helper(entry["helper"])
            if effect is None:
                return None
            if isinstance(effect, Method):
                method = effect
        elif not entry.get("silent"):
            return None
    return method


def apply_helper(helper: InitHelper, arguments: list[str | None] | None) -> Method | Silent | None:
    """What a call to an init helper does to the method, given its literal arguments."""
    if helper.silent:
        return Silent()
    if helper.method is not None:
        return Method(helper.method, in_call=False)
    if helper.parameter is None or arguments is None:
        return None
    if len(arguments) > helper.parameter:
        raw = arguments[helper.parameter]
        method = fetch_method(raw) if raw is not None else None
        return Method(method, in_call=True) if method is not None else None
    if helper.default is None:
        return None
    method = fetch_method(helper.default)
    return Method(method, in_call=False) if method is not None else None


def wrapper_meta(wrapper: Wrapper) -> dict[str, Any]:
    return {
        "path": wrapper.path,
        "init": wrapper.init,
        "method": wrapper.method,
        "base": wrapper.base,
        "relative": wrapper.relative,
    }


def wrapper_from(meta: object) -> Wrapper | None:
    """A wrapper summary read back from a claim, or ``None`` if it is malformed."""
    if not isinstance(meta, dict):
        return None
    path, init, method = meta.get("path"), meta.get("init"), meta.get("method")
    base, relative = meta.get("base"), meta.get("relative")
    if (
        not isinstance(path, int)
        or not (init is None or isinstance(init, int))
        or not (method is None or isinstance(method, str))
        or not (base is None or isinstance(base, str))
        or not isinstance(relative, bool)
        or (init is None) == (method is None)
    ):
        return None
    return Wrapper(path, init, method, base, relative)


def helper_meta(helper: InitHelper) -> dict[str, Any]:
    return {
        "method": helper.method,
        "parameter": helper.parameter,
        "default": helper.default,
        "silent": helper.silent,
    }


def helper_from(meta: object) -> InitHelper | None:
    if not isinstance(meta, dict):
        return None
    method, parameter = meta.get("method"), meta.get("parameter")
    default, silent = meta.get("default"), meta.get("silent")
    if (
        not (method is None or isinstance(method, str))
        or not (parameter is None or isinstance(parameter, int))
        or not (default is None or isinstance(default, str))
        or not isinstance(silent, bool)
    ):
        return None
    return InitHelper(method, parameter, default, silent=silent)


def _enclosing_function(node: Node) -> Node | None:
    parent = node.parent
    while parent is not None:
        if parent.type in _FUNCTIONS:
            return parent
        parent = parent.parent
    return None


def _parameters(function: Node) -> list[Node]:
    if function.type == "program":
        return []
    parameters = function.child_by_field_name("parameters")
    if parameters is not None:
        return [child for child in parameters.named_children if child.type != "comment"]
    single = function.child_by_field_name("parameter")
    return [single] if single is not None else []


def _own_calls(function: Node) -> list[Node]:
    """Every call in ``function``'s own body, not inside a nested function or class."""
    body = function.child_by_field_name("body")
    if body is None:
        return []
    found: list[Node] = []
    pending = [body]
    while pending:
        node = pending.pop()
        if node.type == "call_expression":
            found.append(node)
        for child in node.children:
            if child.type not in _NESTED:
                pending.append(child)
    return found


def _returned_objects(function: Node) -> list[Node]:
    """The object literals ``function`` itself returns."""
    body = function.child_by_field_name("body")
    if body is None:
        return []
    if body.type != "statement_block":
        node = operand(body)
        return [node] if node.type == "object" else []
    found: list[Node] = []
    pending = [body]
    while pending:
        node = pending.pop()
        for child in node.children:
            if child.type in _NESTED:
                continue
            if child.type == "return_statement":
                value = next((c for c in child.named_children if c.type != "comment"), None)
                if value is not None and operand(value).type == "object":
                    found.append(operand(value))
            pending.append(child)
    return found
