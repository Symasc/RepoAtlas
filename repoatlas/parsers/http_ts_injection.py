"""Requests through a transport and a base injected in an options object (HTTP linkage).

A service wires its HTTP client in one place and hands it on:

    const svcC7Opts = { svcC7BaseUrl, fetcher: superagent };      // main.ts
    const models = createModels({ svcC7Opts });
    export function createModels({ svcC7Opts }) {                 // models.ts
      return { changeModel: createChangeModel(svcC7Opts) };
    }
    export function createChangeModel({ svcC7BaseUrl, fetcher }) { // changeModel.ts
      return { get: (id) => fetcher.get(`${svcC7BaseUrl}/sites/${id}/change`) };
    }

No file alone proves the request: ``fetcher`` and ``svcC7BaseUrl`` are whatever the
callers pass. So each file records what it states, as claims for the global pass
(:mod:`repoatlas.graph.ts_injection`):

* :data:`~repoatlas.parsers.http_facts.TS_INJECTED_REQUEST` -- a superagent-method call
  ``T.<verb>(url)`` on a name the enclosing module-level function binds by destructuring
  its first parameter, with a URL that is a template whose first substitution is
  another such name or a base the file states, followed by literal text and
  substitutions;
* :data:`~repoatlas.parsers.http_facts.TS_INJECTION` -- a call to a function this file
  declares, or imports from a relative module, whose first argument's value the file
  proves as a tree: superagent itself (the default import, bound once), a stated string
  (:meth:`~repoatlas.parsers.http_ts_wrappers.FetchWrappers.literal_node`), a key of the
  enclosing function's own destructured first parameter, or an object literal of those.
  It is recorded only when the tree holds superagent or such a key: it either supplies a
  transport, or passes one on;
* :data:`~repoatlas.parsers.http_facts.TS_FUNCTION` -- each module-level function owning
  such a claim, with the names it is exported under.

A spread, a computed key or a method in an object literal makes it unknown; anything
not proven is an unknown leaf, and an unknown transport or base reaches no request. A
request whose own chain calls ``.use(plugin)`` is not read.

Parsed, never executed (V1_SPEC §17).
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from repoatlas.models import http
from repoatlas.models.parsing import EdgeType, EntityType, ParsedReference
from repoatlas.parsers.http_facts import (
    TS_FUNCTION,
    TS_INJECTED_REQUEST,
    TS_INJECTION,
    pending_reference,
)
from repoatlas.parsers.http_ts_wrappers import (
    SUPERAGENT_VERBS,
    Const,
    FetchWrappers,
    arguments_of,
    operand,
    reconfigured,
)
from repoatlas.parsers.http_typescript import FUNCTION_NODES, scope_bindings, static_string
from repoatlas.parsers.treesitter import line_range, node_text, string_literal

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tree_sitter import Node

#: The value tree's depth and width: deeper than any options object anyone writes.
_MAX_DEPTH = 4
_MAX_KEYS = 32
#: How much of a substitution's source text a claim shows, for display only.
_MAX_SHOWN = 60

_DECLARATIONS = frozenset({"function_declaration", "generator_function_declaration"})
_FUNCTION_VALUES = frozenset({"arrow_function", "function_expression", "function"})
_TEMPLATE_CONTENT = frozenset({"string_fragment", "escape_sequence", "template_substitution"})
DEFAULT_FUNCTION = "*default"

_Key = tuple[int, int]
Spec = dict[str, Any] | None


def _key(node: Node) -> _Key:
    return node.start_byte, node.end_byte


@dataclass(slots=True)
class _Function:
    name: str
    line: int
    #: Local name → options key, for a first parameter destructured from an object.
    pattern: dict[str, str] | None
    exports: list[str] = field(default_factory=list)


class Injections:
    """What one file states about transports and bases handed on in options objects."""

    def __init__(
        self, source: bytes, root: Node, counts: Counter[str], wrappers: FetchWrappers
    ) -> None:
        self._source = source
        self._counts = counts
        self._wrappers = wrappers
        #: Local name → ``(relative specifier, imported name)``, bound once.
        self._imports: dict[str, tuple[str, str]] = {}
        self._functions: dict[_Key, _Function] = {}
        self._named: dict[str, _Function] = {}
        self._own: dict[_Key, Counter[str]] = {}
        self._owners: set[str] = set()
        self._top_level(root)
        #: Every name a registered function destructures: no other name is an option.
        self._destructured = {
            name for function in self._functions.values() for name in function.pattern or {}
        }

    # -- the module ------------------------------------------------------------------

    def _top_level(self, root: Node) -> None:
        aliases: list[tuple[str, str]] = []
        for statement in root.named_children:
            if statement.type == "import_statement":
                self._import(statement)
            elif statement.type in _DECLARATIONS:
                self._function(statement, None)
            elif statement.type == "lexical_declaration":
                self._constants(statement, exported=False)
            elif statement.type == "export_statement":
                aliases.extend(self._export(statement))
        for local, exported in aliases:
            function = self._named.get(local)
            if function is not None:
                function.exports.append(exported)

    def _import(self, statement: Node) -> None:
        if any(child.type == "type" for child in statement.children):
            return
        source_node = statement.child_by_field_name("source")
        module = string_literal(self._source, source_node) if source_node is not None else None
        clause = next((c for c in statement.children if c.type == "import_clause"), None)
        if module is None or not module.startswith(".") or clause is None:
            return
        for child in clause.named_children:
            if child.type == "identifier":
                local = node_text(self._source, child)
                if self._counts[local] == 1:
                    self._imports[local] = (module, "default")
            elif child.type == "named_imports":
                for specifier in child.named_children:
                    if specifier.type != "import_specifier" or any(
                        c.type == "type" for c in specifier.children
                    ):
                        continue
                    imported = specifier.child_by_field_name("name")
                    alias = specifier.child_by_field_name("alias") or imported
                    if imported is None or alias is None:
                        continue
                    local = node_text(self._source, alias)
                    if self._counts[local] == 1:
                        self._imports[local] = (module, node_text(self._source, imported))

    def _function(self, node: Node, exported: str | None) -> list[tuple[str, str]]:
        name_node = node.child_by_field_name("name")
        line = line_range(node)[0]
        if name_node is None:
            if exported == "default":
                function = _Function(DEFAULT_FUNCTION, line, self._pattern(node), ["default"])
                self._functions[_key(node)] = function
            return []
        name = node_text(self._source, name_node)
        if self._counts[name] != 1:
            return []
        self._register(node, name, line)
        return [(name, exported)] if exported is not None else []

    def _register(self, node: Node, name: str, line: int) -> None:
        function = _Function(name, line, self._pattern(node))
        self._functions[_key(node)] = function
        self._named[name] = function

    def _constants(self, statement: Node, *, exported: bool) -> list[tuple[str, str]]:
        kind = statement.child_by_field_name("kind")
        if kind is None or node_text(self._source, kind) != "const":
            return []
        found: list[tuple[str, str]] = []
        for declarator in statement.named_children:
            name_node = declarator.child_by_field_name("name")
            value = declarator.child_by_field_name("value")
            if declarator.type != "variable_declarator" or name_node is None or value is None:
                continue
            name = node_text(self._source, name_node)
            value = operand(value)
            if name_node.type != "identifier" or value.type not in _FUNCTION_VALUES:
                continue
            if self._counts[name] != 1:
                continue
            self._register(value, name, line_range(declarator)[0])
            if exported:
                found.append((name, name))
        return found

    def _export(self, statement: Node) -> list[tuple[str, str]]:
        default = any(child.type == "default" for child in statement.children)
        declaration = statement.child_by_field_name("declaration")
        if declaration is not None:
            if declaration.type in _DECLARATIONS:
                named = declaration.child_by_field_name("name")
                name = node_text(self._source, named) if named is not None else "default"
                return self._function(declaration, "default" if default else name)
            if declaration.type == "lexical_declaration" and not default:
                return self._constants(declaration, exported=True)
            return []
        value = statement.child_by_field_name("value")
        if value is not None and default:
            value = operand(value)
            if value.type == "identifier":
                return [(node_text(self._source, value), "default")]
            if value.type in _FUNCTION_VALUES:
                function = _Function(
                    DEFAULT_FUNCTION, line_range(value)[0], self._pattern(value), ["default"]
                )
                self._functions[_key(value)] = function
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

    def _pattern(self, function: Node) -> dict[str, str] | None:
        """``{ a, b: c, d = 1 }`` as its first parameter: local name → options key."""
        parameters = function.child_by_field_name("parameters")
        if parameters is None:
            return None
        first = next((p for p in parameters.named_children if p.type != "comment"), None)
        if first is None:
            return None
        pattern = first.child_by_field_name("pattern") if first.type != "object_pattern" else first
        if pattern is None or pattern.type != "object_pattern":
            return None
        found: dict[str, str] = {}
        for entry in pattern.named_children:
            if entry.type == "shorthand_property_identifier_pattern":
                name = node_text(self._source, entry)
                found[name] = name
            elif entry.type == "object_assignment_pattern":
                left = entry.child_by_field_name("left")
                if left is not None and left.type == "shorthand_property_identifier_pattern":
                    name = node_text(self._source, left)
                    found[name] = name
            elif entry.type == "pair_pattern":
                key = entry.child_by_field_name("key")
                value = entry.child_by_field_name("value")
                if value is not None and value.type == "assignment_pattern":
                    value = value.child_by_field_name("left")
                if (
                    key is not None
                    and key.type == "property_identifier"
                    and value is not None
                    and value.type == "identifier"
                ):
                    found[node_text(self._source, value)] = node_text(self._source, key)
        return found

    # -- names ---------------------------------------------------------------------------

    def _option(self, node: Node) -> tuple[_Function, str] | None:
        """The enclosing function and options key a name is destructured from, if it is."""
        name = node_text(self._source, node)
        if name not in self._destructured:
            return None
        scope = node.parent
        while scope is not None and scope.type != "program":
            if scope.type in FUNCTION_NODES:
                key = _key(scope)
                counts = self._own.get(key)
                if counts is None:
                    counts = scope_bindings(self._source, scope)
                    self._own[key] = counts
                if counts[name]:
                    function = self._functions.get(key)
                    if counts[name] != 1 or function is None or function.pattern is None:
                        return None
                    option = function.pattern.get(name)
                    return (function, option) if option is not None else None
            scope = scope.parent
        return None

    def _spec(self, node: Node, depth: int = 0) -> Spec:
        """The value tree the source proves for ``node``, or ``None`` when it proves none."""
        if depth > _MAX_DEPTH:
            return None
        node = operand(node)
        literal = self._wrappers.literal_node(node)
        if literal is not None:
            return {"string": static_string(self._source, literal), "line": line_range(literal)[0]}
        if node.type in {"identifier", "shorthand_property_identifier"}:
            return self._name(node, depth)
        if node.type != "object":
            return None
        entries: dict[str, Spec] = {}
        for entry in node.named_children:
            if entry.type == "comment":
                continue
            if entry.type == "shorthand_property_identifier":
                entries[node_text(self._source, entry)] = self._name(entry, depth + 1)
            elif entry.type == "pair":
                key = self._wrappers.key(entry)
                value = entry.child_by_field_name("value")
                if key is None or value is None or key in entries:
                    return None
                entries[key] = self._spec(value, depth + 1)
            else:
                return None  # a spread or a method: any key may be anything
            if len(entries) > _MAX_KEYS:
                return None
        return {"object": entries}

    def _name(self, node: Node, depth: int) -> Spec:
        name = node_text(self._source, node)
        if self._wrappers.is_superagent(name, node):
            return {"superagent": True}
        option = self._option(node)
        if option is not None:
            return {"option": option[1], "of": option[0].name}
        binding = self._wrappers.lookup(name, node)
        if isinstance(binding, Const):
            return self._spec(binding.value, depth + 1)
        return None

    # -- calls ---------------------------------------------------------------------------

    def call_references(self, node: Node, source_symbol: str | None) -> list[ParsedReference]:
        # Every claim holds superagent itself or a key of a destructured first parameter.
        if not self._destructured and not self._wrappers.imports_superagent:
            return []
        callee = node.child_by_field_name("function")
        if callee is None:
            return []
        callee = operand(callee)
        if callee.type == "identifier":
            return self._injection(node, callee, source_symbol)
        if callee.type == "member_expression":
            return self._request(node, callee, source_symbol)
        return []

    def _injection(
        self, node: Node, callee: Node, source_symbol: str | None
    ) -> list[ParsedReference]:
        name = node_text(self._source, callee)
        if name in self._imports and self._wrappers.module_binding(name, node):
            module, imported = self._imports[name]
            target = {"module": module, "name": imported}
        elif name in self._named and self._wrappers.module_binding(name, node):
            target = {"local": name}
        else:
            return []
        arguments = arguments_of(node) or []
        spec = self._spec(arguments[0]) if arguments else None
        if spec is None:
            return []
        owners = _owners(spec)
        if len(owners) > 1 or not (owners or _supplies(spec)):
            return []
        within = owners.pop() if owners else None
        if within is not None:
            self._owners.add(within)
        start, end = line_range(node)
        meta = {**target, "argument": json.dumps(_strip(spec), sort_keys=True)}
        if within is not None:
            meta["within"] = within
        return [
            pending_reference(
                EdgeType.REFERENCES,
                EntityType.SYMBOL,
                name,
                start,
                end,
                role=TS_INJECTION,
                source_symbol=source_symbol,
                **meta,
            )
        ]

    def _request(
        self, node: Node, callee: Node, source_symbol: str | None
    ) -> list[ParsedReference]:
        owner = callee.child_by_field_name("object")
        prop = callee.child_by_field_name("property")
        if owner is None or prop is None or operand(owner).type != "identifier":
            return []
        verb = node_text(self._source, prop)
        if verb not in SUPERAGENT_VERBS:
            return []
        transport = self._option(operand(owner))
        arguments = arguments_of(node) or []
        if transport is None or not 1 <= len(arguments) <= 3:
            return []
        if reconfigured(self._source, node):
            return []
        function, transport_key = transport
        url = operand(arguments[0])
        if url.type != "template_string":
            return []
        content = [child for child in url.children if child.type in _TEMPLATE_CONTENT]
        if (
            not content
            or content[0].type != "template_substitution"
            or any(child.type == "escape_sequence" for child in content)
        ):
            return []
        inner = next((c for c in content[0].named_children if c.type != "comment"), None)
        if inner is None:
            return []
        meta: dict[str, str] = {"function": function.name, "transport": transport_key}
        base = self._option(operand(inner)) if operand(inner).type == "identifier" else None
        if base is not None:
            if base[0] is not function:
                return []
            meta["base_option"] = base[1]
        else:
            stated = self._wrappers.literal_node(inner)
            value = static_string(self._source, stated) if stated is not None else None
            if stated is None or value is None:
                # A base no file states (D2): the request is counted, never joined.
                meta["base_unknown"] = "true"
            else:
                meta["base"] = value
                meta["base_at"] = str(line_range(stated)[0])
        rest: list[list[str]] = []
        for child in content[1:]:
            if child.type == "string_fragment":
                rest.append([http.TEMPLATE_LITERAL, node_text(self._source, child)])
            else:
                value_node = next((c for c in child.named_children if c.type != "comment"), None)
                text = node_text(self._source, value_node) if value_node is not None else ""
                rest.append([http.TEMPLATE_SUBSTITUTION, text])
        method = SUPERAGENT_VERBS[verb]
        self._owners.add(function.name)
        shown = "".join(
            part[1] if part[0] == http.TEMPLATE_LITERAL else "{" + part[1][:_MAX_SHOWN] + "}"
            for part in rest
        )
        base_shown = node_text(self._source, inner)[:_MAX_SHOWN]
        start, _ = line_range(node)
        _, end = line_range(arguments[0])
        return [
            pending_reference(
                EdgeType.CALLS_HTTP_ENDPOINT,
                EntityType.HTTP_ENDPOINT,
                f"{method} {{{base_shown}}}{shown}",
                start,
                end,
                role=TS_INJECTED_REQUEST,
                source_symbol=source_symbol,
                method=method,
                rest=json.dumps(rest),
                **meta,
            )
        ]

    # -- claims --------------------------------------------------------------------------

    def references(self) -> list[ParsedReference]:
        """Each function owning a claim here, with the names other modules import it by."""
        return [
            pending_reference(
                EdgeType.REFERENCES,
                EntityType.SYMBOL,
                function.name,
                function.line,
                function.line,
                role=TS_FUNCTION,
                exports=json.dumps(function.exports),
            )
            for function in self._functions.values()
            if function.name in self._owners
        ]


def _owners(spec: Spec) -> set[str]:
    if not spec:
        return set()
    if "option" in spec:
        return {spec["of"]}
    if "object" in spec:
        return set().union(*(_owners(value) for value in spec["object"].values()))
    return set()


def _supplies(spec: Spec) -> bool:
    """Whether the tree holds superagent itself: it supplies a transport."""
    if not spec:
        return False
    if spec.get("superagent"):
        return True
    if "object" in spec:
        return any(_supplies(value) for value in spec["object"].values())
    return False


def _strip(spec: Spec) -> Spec:
    """The tree as the claim records it: a key of the caller's options names no function."""
    if not spec:
        return None
    if "option" in spec:
        return {"option": spec["option"]}
    if "object" in spec:
        return {"object": {key: _strip(value) for key, value in spec["object"].items()}}
    return spec
