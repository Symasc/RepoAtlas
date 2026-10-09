"""Persistence in TypeScript source: database clients, queries, and typed value mappings.

Read from the tree :class:`~repoatlas.parsers.typescript.TypeScriptParser` already parsed,
as a pure function of one file. Nothing is evaluated, connected to or resolved; what one
file cannot settle is a claim the global pass decides (:mod:`repoatlas.graph.persistence`,
:mod:`repoatlas.graph.value_mappings`).

**Clients** (``persistence-client``): a module-level ``const`` bound to ``knex(...)`` (the
default or named import of ``knex``, or ``require("knex")``), to ``new Pool(...)`` /
``new Client(...)`` of ``pg``, or to ``new PrismaClient(...)`` of any module. The claim
records the library and, when the construction reads exactly one environment variable as
``process.env.X``, that variable: the only thing that can tie it to a database.

**Queries** (``persistence-query``), each in the declaration that makes it:

* a Knex chain, ``R('t [as a]')`` or ``R.from('t')`` then ``where`` / ``andWhere`` /
  ``orWhere`` / ``whereNot`` ``('c', [op,] 'v')`` or ``({c: 'v'})``, and ``whereIn`` /
  ``whereNotIn`` ``('c', ['v', ...])``; joins name more tables. Only string literals count;
* SQL passed to ``R.query(...)``, ``R.raw(...)``, ``R.$queryRawUnsafe(...)`` /
  ``$executeRawUnsafe``, or written as ``R.$queryRaw`...``` / ``$executeRaw``. A literal is
  parsed here (:func:`repoatlas.parsers.sql.sql_predicates`); a substitution in a tagged
  ``$queryRaw`` is a bound parameter, anywhere else (concatenation, an untagged template)
  it makes the text something only run time decides: ``dynamic``.

Each records how its receiver ``R`` is bound: a module-level ``const`` of this file, an
import, or something else -- a parameter or local that shadows it -- which is never taken
for a client.

**Typed value mappings** (``value-mapping``): a module-level ``const m: Record<A, B> = {...}``
or ``{...} satisfies Record<A, B>``, with each entry's key (``[A.X]``, or a literal) and
value (``B.Y``, or a literal) at its line.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import TYPE_CHECKING

from repoatlas.models.parsing import EdgeType, EntityType, ParsedReference
from repoatlas.parsers.http_facts import pending_reference
from repoatlas.parsers.sql import sql_predicates
from repoatlas.parsers.treesitter import node_text, string_literal

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tree_sitter import Node

#: The pending-fact roles this module records.
PERSISTENCE_CLIENT = "persistence-client"
PERSISTENCE_QUERY = "persistence-query"
VALUE_MAPPING = "value-mapping"

_WHERE = frozenset({"where", "andWhere", "orWhere", "whereNot", "andWhereNot", "orWhereNot"})
_WHERE_IN = frozenset({"whereIn", "orWhereIn", "whereNotIn", "orWhereNotIn"})
_FROM = frozenset({"from", "table", "into"})
_JOINS = frozenset(
    {
        "join",
        "innerJoin",
        "leftJoin",
        "leftOuterJoin",
        "rightJoin",
        "rightOuterJoin",
        "fullOuterJoin",
    }
)
_SQL_SINKS = frozenset({"query", "raw", "$queryRawUnsafe", "$executeRawUnsafe"})
_TAGGED = frozenset({"$queryRaw", "$executeRaw"})
_TRANSPARENT = frozenset(
    {"parenthesized_expression", "as_expression", "satisfies_expression", "non_null_expression"}
)
_FUNCTIONS = frozenset(
    {
        "function_declaration",
        "generator_function_declaration",
        "function_expression",
        "function",
        "arrow_function",
        "method_definition",
    }
)
_ALIAS = re.compile(r"^\s*([\w.\"]+)(?:\s+(?:as\s+)?(\w+))?\s*$", re.IGNORECASE)
_MAX_CHAIN = 64
_MAX_ENTRIES = 512


class TsPersistence:
    """What one TypeScript file states about databases and value mappings."""

    def __init__(self, source: bytes, root: Node) -> None:
        self._source = source
        #: Local name -> (module specifier, imported name, or ``default`` / ``*``).
        self._imports: dict[str, tuple[str, str]] = {}
        self._consts: dict[str, Node] = {}
        self._bound: Counter[str] = Counter()
        self._claims: list[ParsedReference] = []
        self._top_level(root)

    def references(self) -> list[ParsedReference]:
        return list(self._claims)

    # -- the module ------------------------------------------------------------------

    def _top_level(self, root: Node) -> None:
        declarations: list[Node] = []
        for statement in root.named_children:
            inner = statement
            if statement.type == "export_statement":
                inner = statement.child_by_field_name("declaration") or statement
            if statement.type == "import_statement":
                self._import(statement)
            elif inner.type in {"lexical_declaration", "variable_declaration"}:
                for declarator in inner.named_children:
                    if declarator.type != "variable_declarator":
                        continue
                    name = declarator.child_by_field_name("name")
                    if name is None or name.type != "identifier":
                        continue
                    local = node_text(self._source, name)
                    self._bound[local] += 1
                    value = declarator.child_by_field_name("value")
                    if value is not None:
                        self._consts[local] = value
                        self._require(local, value)
                    declarations.append(declarator)
            elif inner.type in {"function_declaration", "class_declaration"}:
                name = inner.child_by_field_name("name")
                if name is not None:
                    self._bound[node_text(self._source, name)] += 1
        for declarator in declarations:
            self._client(declarator)
            self._mapping(declarator)

    def _import(self, statement: Node) -> None:
        source_node = statement.child_by_field_name("source")
        module = string_literal(self._source, source_node) if source_node is not None else None
        clause = next((c for c in statement.children if c.type == "import_clause"), None)
        if module is None or clause is None:
            return
        for child in clause.named_children:
            if child.type == "identifier":
                self._bind(node_text(self._source, child), module, "default")
            elif child.type == "namespace_import":
                name = next((c for c in child.named_children if c.type == "identifier"), None)
                if name is not None:
                    self._bind(node_text(self._source, name), module, "*")
            elif child.type == "named_imports":
                for specifier in child.named_children:
                    imported = specifier.child_by_field_name("name")
                    alias = specifier.child_by_field_name("alias") or imported
                    if specifier.type == "import_specifier" and imported and alias:
                        self._bind(
                            node_text(self._source, alias),
                            module,
                            node_text(self._source, imported),
                        )

    def _require(self, local: str, value: Node) -> None:
        """``const knex = require("knex")`` binds like a default import."""
        call = _operand(value)
        if call is None or call.type != "call_expression":
            return
        callee = call.child_by_field_name("function")
        arguments = call.child_by_field_name("arguments")
        if callee is None or node_text(self._source, callee) != "require" or arguments is None:
            return
        strings = [a for a in arguments.named_children if a.type == "string"]
        if len(strings) == 1:
            module = string_literal(self._source, strings[0])
            if module:
                self._imports[local] = (module, "default")

    def _bind(self, local: str, module: str, imported: str) -> None:
        self._bound[local] += 1
        self._imports[local] = (module, imported)

    # -- clients ---------------------------------------------------------------------

    def _client(self, declarator: Node) -> None:
        name_node = declarator.child_by_field_name("name")
        value = _operand(declarator.child_by_field_name("value"))
        if name_node is None or value is None:
            return
        name = node_text(self._source, name_node)
        if self._bound[name] != 1:
            return
        library: str | None = None
        module = ""
        if value.type == "call_expression":
            callee = value.child_by_field_name("function")
            bound = self._imports.get(node_text(self._source, callee)) if callee else None
            if bound is not None and bound[0] == "knex" and bound[1] in {"default", "knex"}:
                library = "knex"
        elif value.type == "new_expression":
            constructor = value.child_by_field_name("constructor")
            text = node_text(self._source, constructor) if constructor is not None else ""
            head, _, tail = text.partition(".")
            bound = self._imports.get(head)
            if bound is not None and bound[0] == "pg":
                if (not tail and bound[1] in {"Pool", "Client"}) or (
                    tail in {"Pool", "Client"} and bound[1] in {"default", "*"}
                ):
                    library = "pg"
            elif bound is not None and not tail and bound[1] == "PrismaClient":
                library, module = "prisma", bound[0]
        if library is None:
            return
        line = declarator.start_point[0] + 1
        meta = {"library": library}
        env = self._env(value)
        if env:
            meta["env"] = env
        if module:
            meta["module"] = module
        self._claims.append(
            pending_reference(
                EdgeType.REFERENCES,
                EntityType.SYMBOL,
                name,
                line,
                declarator.end_point[0] + 1,
                role=PERSISTENCE_CLIENT,
                source_symbol=name,
                **meta,
            )
        )

    def _env(self, node: Node) -> str | None:
        """The one environment variable a construction reads as ``process.env.X``."""
        found: set[str] = set()
        stack = [node]
        while stack:
            current = stack.pop()
            if current.type == "member_expression":
                text = node_text(self._source, current)
                if text.startswith("process.env.") and text.count(".") == 2:
                    found.add(text.rsplit(".", 1)[1])
                    continue
            if current.type == "subscript_expression":
                obj = current.child_by_field_name("object")
                index = current.child_by_field_name("index")
                if obj is not None and node_text(self._source, obj) == "process.env" and index:
                    key = string_literal(self._source, index) if index.type == "string" else None
                    if key:
                        found.add(key)
                        continue
            stack.extend(current.named_children)
        return next(iter(found)) if len(found) == 1 else None

    # -- mappings --------------------------------------------------------------------

    def _mapping(self, declarator: Node) -> None:
        name_node = declarator.child_by_field_name("name")
        value = declarator.child_by_field_name("value")
        if name_node is None or value is None:
            return
        types = self._record(declarator.child_by_field_name("type"))
        if value.type == "satisfies_expression":
            inner = value.named_children
            if len(inner) == 2:
                types = types or self._record(inner[1])
                value = inner[0]
        value = _operand(value)
        if types is None or value is None or value.type != "object":
            return
        entries: list[list[object]] = []
        for pair in value.named_children:
            if pair.type != "pair" or len(entries) >= _MAX_ENTRIES:
                continue
            key = self._entry_side(pair.child_by_field_name("key"))
            val = self._entry_side(pair.child_by_field_name("value"))
            if key is None:
                continue
            entries.append([key[0], key[1], *(val or ("other", "")), pair.start_point[0] + 1])
        name = node_text(self._source, name_node)
        self._claims.append(
            pending_reference(
                EdgeType.MAPS_TO,
                EntityType.SYMBOL,
                name,
                declarator.start_point[0] + 1,
                declarator.end_point[0] + 1,
                role=VALUE_MAPPING,
                source_symbol=name,
                key_type=types[0],
                value_type=types[1],
                entries=json.dumps(entries, separators=(",", ":")),
            )
        )

    def _record(self, node: Node | None) -> tuple[str, str] | None:
        """``(A, B)`` of a ``Record<A, B>`` annotation over two plain type names."""
        if node is None:
            return None
        if node.type == "type_annotation":
            node = next(iter(node.named_children), None)
        if node is None or node.type != "generic_type":
            return None
        name = node.child_by_field_name("name")
        arguments = node.child_by_field_name("type_arguments")
        if name is None or node_text(self._source, name) != "Record" or arguments is None:
            return None
        named = [
            a for a in arguments.named_children if a.type in {"type_identifier", "predefined_type"}
        ]
        if len(named) != 2 or len(arguments.named_children) != 2:
            return None
        return node_text(self._source, named[0]), node_text(self._source, named[1])

    def _entry_side(self, node: Node | None) -> tuple[str, str] | None:
        if node is None:
            return None
        if node.type == "computed_property_name":
            node = next(iter(node.named_children), None)
            if node is None:
                return None
        node = _operand(node)
        if node is None:
            return None
        if node.type == "member_expression":
            obj = node.child_by_field_name("object")
            prop = node.child_by_field_name("property")
            if obj is not None and prop is not None and obj.type == "identifier":
                return "member", f"{node_text(self._source, obj)}.{node_text(self._source, prop)}"
            return None
        if node.type == "string":
            literal = string_literal(self._source, node)
            return ("literal", literal) if literal is not None else None
        if node.type in {"property_identifier", "number"}:
            return "literal", node_text(self._source, node)
        return None

    # -- queries ---------------------------------------------------------------------

    def visit_call(self, node: Node, owner: str) -> None:
        """A call or ``new`` expression the walker reached, in the declaration ``owner``."""
        if node.type != "call_expression":
            return
        callee = node.child_by_field_name("function")
        if callee is None or callee.type != "member_expression":
            return
        method_node = callee.child_by_field_name("property")
        method = node_text(self._source, method_node) if method_node is not None else ""
        if method in _TAGGED or method in _SQL_SINKS:
            self._sql(node, callee, method, owner)
        elif not _continues(node):
            self._knex(node, owner)

    def _receiver(self, node: Node, name: str) -> dict[str, str]:
        """How ``name`` is bound where ``node`` is: this module's, an import's, or shadowed."""
        if _shadowed(self._source, node, name) or self._bound[name] != 1:
            return {"binding": "local"}
        if name in self._imports:
            module, imported = self._imports[name]
            return {"binding": "import", "module": module, "imported": imported}
        if name in self._consts:
            return {"binding": "module"}
        return {"binding": "unbound"}

    def _sql(self, node: Node, callee: Node, method: str, owner: str) -> None:
        receiver = callee.child_by_field_name("object")
        if receiver is None or receiver.type != "identifier":
            return
        name = node_text(self._source, receiver)
        arguments = node.child_by_field_name("arguments")
        if arguments is None:
            return
        meta: dict[str, str] = {"kind": "sql", "method": method, "receiver": name}
        if method in _TAGGED:
            if arguments.type != "template_string":
                return
            text, holes = self._template(arguments, parameters=True)
            dynamic = text is None
        else:
            first = next(iter(arguments.named_children), None)
            if first is None:
                return
            text, dynamic = self._literal_sql(first)
            holes = 0
        meta.update(self._receiver(node, name))
        self._query(node, owner, meta, text, dynamic=dynamic, holes=holes)

    def _literal_sql(self, node: Node) -> tuple[str | None, bool]:
        """The SQL text of an argument, or whether only run time decides it."""
        node = _operand(node) or node
        if node.type == "string":
            return string_literal(self._source, node), False
        if node.type == "template_string":
            text, _ = self._template(node, parameters=False)
            return text, text is None
        if node.type == "binary_expression":
            return None, True
        return None, True

    def _template(self, node: Node, *, parameters: bool) -> tuple[str | None, int]:
        """A template's text, each substitution a ``$n`` parameter when ``parameters``,
        else ``None`` if it has any."""
        parts: list[str] = []
        holes = 0
        cursor = node.start_byte + 1
        for child in node.named_children:
            if child.type != "template_substitution":
                continue
            if not parameters:
                return None, 0
            holes += 1
            parts.append(self._source[cursor : child.start_byte].decode("utf-8", "replace"))
            parts.append(f"${holes}")
            cursor = child.end_byte
        parts.append(self._source[cursor : node.end_byte - 1].decode("utf-8", "replace"))
        return "".join(parts), holes

    def _knex(self, node: Node, owner: str) -> None:
        """A Knex chain, read from its last call down to its root."""
        calls: list[tuple[str, Node]] = []
        current: Node | None = node
        root: Node | None = None
        for _ in range(_MAX_CHAIN):
            if current is None or current.type != "call_expression":
                break
            function = current.child_by_field_name("function")
            if function is None:
                break
            if function.type == "identifier":
                root = current
                break
            if function.type != "member_expression":
                break
            prop = function.child_by_field_name("property")
            calls.append((node_text(self._source, prop) if prop else "", current))
            current = _operand(function.child_by_field_name("object"))
            if current is not None and current.type == "identifier":
                root = current
                break
        if root is None:
            return
        tables: list[list[str]] = []
        if root.type == "call_expression":
            function = root.child_by_field_name("function")
            if function is None:
                return
            name = node_text(self._source, function)
            first = self._first_string(root)
            if first is None:
                return
            tables.append(_table(first))
        else:
            name = node_text(self._source, root)
        predicates: list[list[object]] = []
        for method, call in reversed(calls):
            args = [a for a in (call.child_by_field_name("arguments") or call).named_children]
            if method in _FROM | _JOINS:
                first = self._first_string(call)
                if first is not None:
                    tables.append(_table(first))
            elif method in _WHERE:
                predicates += self._where(args, negated="Not" in method)
            elif method in _WHERE_IN and len(args) == 2:
                column = string_literal(self._source, args[0]) if args[0].type == "string" else None
                values = self._strings(args[1])
                if column and values:
                    predicates.append([column, "<>" if "Not" in method else "in", values])
        if not tables or not predicates:
            return
        meta = {
            "kind": "knex",
            "receiver": name,
            "tables": json.dumps(tables, separators=(",", ":")),
            "predicates": json.dumps(predicates, separators=(",", ":")),
            **self._receiver(node, name),
        }
        self._query(node, owner, meta, None, dynamic=False, holes=0)

    def _where(self, args: list[Node], *, negated: bool) -> list[list[object]]:
        if len(args) == 1 and args[0].type == "object":
            found = []
            for pair in args[0].named_children:
                if pair.type != "pair":
                    continue
                key = pair.child_by_field_name("key")
                value = pair.child_by_field_name("value")
                column = node_text(self._source, key) if key is not None else ""
                if key is not None and key.type == "string":
                    column = string_literal(self._source, key) or ""
                literal = self._strings(value) if value is not None else None
                if column and literal:
                    found.append([column, "<>" if negated else "=", literal])
            return found
        if len(args) in {2, 3} and args[0].type == "string":
            column = string_literal(self._source, args[0])
            op = "="
            if len(args) == 3:
                op_text = (
                    string_literal(self._source, args[1]) if args[1].type == "string" else None
                )
                if op_text not in {"=", "<>", "!="}:
                    return []
                op = "=" if op_text == "=" else "<>"
            values = self._strings(args[-1])
            if column and values:
                return [[column, ("<>" if negated else op), values]]
        return []

    def _strings(self, node: Node | None) -> list[str] | None:
        node = _operand(node)
        if node is None:
            return None
        if node.type == "string":
            text = string_literal(self._source, node)
            return [text] if text is not None else None
        if node.type == "array":
            found = [string_literal(self._source, c) for c in node.named_children]
            if found and all(isinstance(f, str) for f in found):
                return [f for f in found if f is not None]
        return None

    def _first_string(self, call: Node) -> str | None:
        arguments = call.child_by_field_name("arguments")
        first = next(iter(arguments.named_children), None) if arguments is not None else None
        if first is None or first.type != "string":
            return None
        return string_literal(self._source, first)

    def _query(
        self,
        node: Node,
        owner: str,
        meta: dict[str, str],
        text: str | None,
        *,
        dynamic: bool,
        holes: int,
    ) -> None:
        if meta["kind"] == "sql":
            if dynamic:
                meta["dynamic"] = "true"
            elif text is not None:
                predicates = sql_predicates(text)
                if predicates is None:
                    meta["unsupported"] = "true"
                else:
                    meta["predicates"] = json.dumps(
                        [[p.column, p.op, list(p.values), p.table] for p in predicates],
                        separators=(",", ":"),
                    )
            if holes:
                meta["parameters"] = str(holes)
        line = node.start_point[0] + 1
        self._claims.append(
            pending_reference(
                EdgeType.REFERENCES,
                EntityType.SYMBOL,
                meta["receiver"],
                line,
                node.end_point[0] + 1,
                role=PERSISTENCE_QUERY,
                source_symbol=owner or None,
                owner=owner,
                **meta,
            )
        )


def _table(text: str) -> list[str]:
    """``[table, alias]`` of ``'t'``, ``'t as a'`` or ``'t a'``."""
    match = _ALIAS.match(text)
    if match is None:
        return [text.strip(), text.strip()]
    table = match.group(1).replace('"', "")
    return [table, match.group(2) or table]


def _operand(node: Node | None) -> Node | None:
    while node is not None and node.type in _TRANSPARENT:
        inner = [c for c in node.named_children if c.type != "comment"]
        if not inner:
            break
        node = inner[0]
    return node


def _continues(node: Node) -> bool:
    """Whether this call is the object of a further call in the same chain."""
    parent = node.parent
    return (
        parent is not None
        and parent.type == "member_expression"
        and parent.parent is not None
        and parent.parent.type == "call_expression"
    )


def _shadowed(source: bytes, node: Node, name: str) -> bool:
    """Whether a function around ``node`` binds ``name``: a parameter, or a local
    declaration anywhere in its body."""
    current = node.parent
    while current is not None:
        if current.type in _FUNCTIONS:
            parameters = current.child_by_field_name("parameters") or current.child_by_field_name(
                "parameter"
            )
            if parameters is not None and _declares(source, parameters, name):
                return True
            body = current.child_by_field_name("body")
            if body is not None and _locally_declared(source, body, name):
                return True
        current = current.parent
    return False


def _declares(source: bytes, node: Node, name: str) -> bool:
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in {"identifier", "shorthand_property_identifier_pattern"} and (
            node_text(source, current) == name
        ):
            parent = current.parent
            if parent is None or parent.type != "type_annotation":
                return True
        if current.type in {"type_annotation", "arrow_function", "function_expression"}:
            continue
        stack.extend(current.named_children)
    return False


def _locally_declared(source: bytes, body: Node, name: str) -> bool:
    stack = [body]
    while stack:
        current = stack.pop()
        if current.type == "variable_declarator":
            target = current.child_by_field_name("name")
            if target is not None and _declares(source, target, name):
                return True
        if current.type in _FUNCTIONS:
            continue
        stack.extend(current.named_children)
    return False
