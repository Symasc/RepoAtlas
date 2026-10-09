"""GraphQL in TypeScript source: embedded documents, schema construction, resolver maps.

Read from the tree :class:`~repoatlas.parsers.typescript.TypeScriptParser` already parsed,
as a pure function of one file. Nothing is evaluated and no other file is read; what one
file cannot settle is recorded as a claim the global pass decides
(:mod:`repoatlas.graph.graphql_documents`, :mod:`repoatlas.graph.graphql_resolvers`).

**Embedded documents.** A template or string passed to a tag or call named ``gql`` or
``graphql`` is read as a GraphQL document with
:func:`repoatlas.parsers.graphql.read_embedded`: its operations and fragments become
symbols of the declaration they are written in, at their lines in this file, and each is a
``graphql-document`` claim. A substitution ``${X}`` *between* definitions is a static hole
-- another document's definitions spliced in -- recorded by what ``X`` names: a constant of
this module, or a name imported from a relative module. A substitution anywhere else makes
the text something only run time decides, and the whole document is recorded as
``dynamic``, with nothing it selects: not even its name is trusted.

**Schema construction** (``graphql-schema-construction``): a call to
``buildSubgraphSchema``, ``makeExecutableSchema``, ``createSchema`` or ``new ApolloServer``,
imported from the library that defines it, with what its ``typeDefs`` load and what its
``resolvers`` are. A path is kept only when it is anchored to this file (``__dirname``,
``import.meta.url``); a path relative to the process's working directory, inline SDL and
anything computed are recorded as unsupported, never guessed at.

**Object shapes** (``ts-object-shape``): an exported module-level object literal whose
members are functions, names, nested objects or spreads -- the shape a resolver map has --
so a construction in another file that imports it can be followed. Its keys, and for each
member what it is: an inline function, a name (this module's, or a relative import's), or a
nested shape.
"""

from __future__ import annotations

import json
import posixpath
from collections import Counter
from typing import TYPE_CHECKING

from repoatlas.models.parsing import (
    EdgeType,
    EntityType,
    ParsedReference,
    ParsedSymbol,
)
from repoatlas.parsers.graphql import GRAPHQL_DOCUMENT, read_embedded
from repoatlas.parsers.http_facts import pending_reference
from repoatlas.parsers.treesitter import node_text, string_literal

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tree_sitter import Node

#: The pending-fact roles this module records.
GRAPHQL_SCHEMA_CONSTRUCTION = "graphql-schema-construction"
TS_OBJECT_SHAPE = "ts-object-shape"

_TAGS = frozenset({"gql", "graphql"})
#: Schema construction, by the library that defines it.
_CONSTRUCTORS: dict[str, frozenset[str]] = {
    "buildSubgraphSchema": frozenset({"@apollo/subgraph", "@apollo/federation"}),
    "makeExecutableSchema": frozenset({"@graphql-tools/schema", "graphql-tools"}),
    "createSchema": frozenset({"graphql-yoga"}),
}
_APOLLO_SERVER = "ApolloServer"
_APOLLO_SERVER_PACKAGES = ("@apollo/server", "apollo-server")
_FS = frozenset({"fs", "node:fs", "fs/promises", "node:fs/promises"})
_PATH = frozenset({"path", "node:path"})
_TRANSPARENT = frozenset(
    {"parenthesized_expression", "as_expression", "satisfies_expression", "non_null_expression"}
)
#: A namespace or default import: the whole module.
_MODULE = "*"
_MAX_DEPTH = 6
_MAX_ENTRIES = 256


class TsGraphQL:
    """What one TypeScript file states about GraphQL."""

    def __init__(self, source: bytes, root: Node) -> None:
        self._source = source
        #: Local name -> (module specifier, imported name or ``*``).
        self._imports: dict[str, tuple[str, str]] = {}
        #: Module-level ``const`` name -> its initializer.
        self._consts: dict[str, Node] = {}
        self._functions: set[str] = set()
        self._exported: dict[str, list[str]] = {}
        self._bound: Counter[str] = Counter()
        self.symbols: list[ParsedSymbol] = []
        self._claims: list[ParsedReference] = []
        self._top_level(root)

    # -- the module ------------------------------------------------------------------

    def _top_level(self, root: Node) -> None:
        aliases: list[tuple[str, str]] = []
        for statement in root.named_children:
            exported = statement.type == "export_statement"
            inner = statement.child_by_field_name("declaration") if exported else statement
            if statement.type == "import_statement":
                self._import(statement)
            elif exported and inner is None:
                aliases.extend(self._export_clause(statement))
            if inner is None:
                continue
            if inner.type in {"lexical_declaration", "variable_declaration"}:
                for declarator in inner.named_children:
                    if declarator.type != "variable_declarator":
                        continue
                    name = declarator.child_by_field_name("name")
                    value = declarator.child_by_field_name("value")
                    if name is None or name.type != "identifier" or value is None:
                        continue
                    local = node_text(self._source, name)
                    self._bound[local] += 1
                    self._consts[local] = value
                    if exported:
                        self._exported.setdefault(local, []).append(local)
            elif inner.type in {"function_declaration", "generator_function_declaration"}:
                name = inner.child_by_field_name("name")
                if name is not None:
                    local = node_text(self._source, name)
                    self._bound[local] += 1
                    self._functions.add(local)
                    if exported:
                        self._exported.setdefault(local, []).append(local)
        for local, exported_as in aliases:
            self._exported.setdefault(local, []).append(exported_as)

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
                self._bind(node_text(self._source, child), module, "default")
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

    def _bind(self, local: str, module: str, imported: str) -> None:
        self._bound[local] += 1
        self._imports[local] = (module, imported)

    def _export_clause(self, statement: Node) -> list[tuple[str, str]]:
        if statement.child_by_field_name("source") is not None:
            return []  # a re-export names another module's bindings, not this one's
        found: list[tuple[str, str]] = []
        for clause in statement.named_children:
            if clause.type != "export_clause":
                continue
            for specifier in clause.named_children:
                name = specifier.child_by_field_name("name")
                alias = specifier.child_by_field_name("alias") or name
                if name is not None and alias is not None:
                    found.append((node_text(self._source, name), node_text(self._source, alias)))
        return found

    def _unique(self, name: str) -> bool:
        """Bound once at module level: a name this file does not rebind elsewhere."""
        return self._bound[name] == 1

    def _package(self, name: str) -> str | None:
        found = self._imports.get(name)
        return found[0] if found is not None and self._unique(name) else None

    # -- calls -----------------------------------------------------------------------

    def visit_call(self, node: Node, owner: str) -> None:
        """A call or ``new`` expression the walker reached, in the declaration ``owner``."""
        callee = node.child_by_field_name(
            "function" if node.type == "call_expression" else "constructor"
        )
        if callee is None or callee.type != "identifier":
            return
        name = node_text(self._source, callee)
        arguments = node.child_by_field_name("arguments")
        if arguments is None:
            return
        if node.type == "call_expression" and name in _TAGS:
            template = arguments if arguments.type == "template_string" else None
            if template is None and arguments.type == "arguments":
                first = next((c for c in arguments.named_children if c.type != "comment"), None)
                if first is not None and first.type in {"template_string", "string"}:
                    template = first
            if template is not None:
                self._document(template, owner)
            return
        package = self._package(name)
        if package is None:
            return
        if (node.type == "call_expression" and package in _CONSTRUCTORS.get(name, ())) or (
            node.type == "new_expression"
            and name == _APOLLO_SERVER
            and package.startswith(_APOLLO_SERVER_PACKAGES)
        ):
            self._construction(node, arguments, owner, library=package)

    # -- documents -------------------------------------------------------------------

    def _document(self, template: Node, owner: str) -> None:
        base = template.start_byte + 1
        raw = bytearray(self._source[base : template.end_byte - 1])
        holes: list[tuple[int, Node | None]] = []
        for child in template.named_children:
            if child.type != "template_substitution":
                continue
            start, end = child.start_byte - base, child.end_byte - base
            for index in range(start, end):
                if raw[index] not in (0x0A, 0x0D):
                    raw[index] = 0x20
            expression = next((c for c in child.named_children if c.type != "comment"), None)
            holes.append((len(bytes(raw[:start]).decode("utf-8", "replace")), expression))
        text = bytes(raw).decode("utf-8", "replace")
        first_line = template.start_point[0] + 1
        embedded = read_embedded(text, first_line=first_line, owner=owner)
        if embedded is not None and embedded.type_system and not embedded.claims:
            return  # inline SDL: a schema, which its construction reports, not a document
        inside = embedded is not None and any(
            span_start <= offset < span_end
            for offset, _ in holes
            for span_start, span_end in embedded.spans
        )
        if embedded is None or inside:
            last_line = template.end_point[0] + 1
            self._claims.append(
                pending_reference(
                    EdgeType.REFERENCES,
                    EntityType.CLASS,
                    owner or "document",
                    first_line,
                    last_line,
                    role=GRAPHQL_DOCUMENT,
                    source_symbol=owner or None,
                    document="dynamic" if holes else "unparsed",
                    owner=owner,
                    embedded="true",
                )
            )
            return
        refs = [self._ref(expression) for _, expression in holes]
        self.symbols.extend(embedded.symbols)
        for claim in embedded.claims:
            if claim.meta.get("pending_fact") != GRAPHQL_DOCUMENT:
                continue
            meta = {**claim.meta, "owner": owner, "embedded": "true"}
            if refs:
                meta["holes"] = _json(refs)
            self._claims.append(claim.model_copy(update={"meta": meta}))

    def _ref(self, node: Node | None) -> dict[str, str]:
        """What a name refers to, as far as this file can say."""
        node = _operand(node)
        if node is None or node.type not in {"identifier", "shorthand_property_identifier"}:
            return {"unresolved": "computed"}
        name = node_text(self._source, node)
        if not self._unique(name):
            return {"unresolved": "rebound"}
        if name in self._consts or name in self._functions:
            return {"local": name}
        found = self._imports.get(name)
        if found is None:
            return {"unresolved": "unbound"}
        module, imported = found
        if not module.startswith(".") or imported in {_MODULE, "default"}:
            return {"unresolved": "package" if not module.startswith(".") else "namespace"}
        return {"module": module, "name": imported}

    # -- schema construction ---------------------------------------------------------

    def _construction(self, node: Node, arguments: Node, owner: str, *, library: str) -> None:
        first = next((c for c in arguments.named_children if c.type != "comment"), None)
        first = _operand(first)
        modules: list[Node] = []
        if first is not None and first.type == "array":
            modules = [_operand(c) for c in first.named_children if c.type != "comment"]  # type: ignore[misc]
        elif first is not None:
            modules = [first]
        type_defs: list[dict[str, object]] = []
        resolvers: list[dict[str, object]] = []
        for module in modules:
            fields = self._object_fields(module)
            if fields is None:
                type_defs.append({"unsupported": "computed"})
                continue
            if "typeDefs" in fields:
                type_defs.extend(self._type_defs(fields["typeDefs"], 0))
            if "resolvers" in fields:
                resolvers.append(self._shape(fields["resolvers"], owner, 0))
        if not type_defs and not resolvers:
            return
        start, end = node.start_point[0] + 1, node.end_point[0] + 1
        self._claims.append(
            pending_reference(
                EdgeType.REFERENCES,
                EntityType.CLASS,
                owner or "schema",
                start,
                end,
                role=GRAPHQL_SCHEMA_CONSTRUCTION,
                source_symbol=owner or None,
                library=library,
                owner=owner,
                type_defs=_json(type_defs),
                resolvers=_json(resolvers),
            )
        )

    def _object_fields(self, node: Node | None) -> dict[str, Node] | None:
        """``{ key: value, shorthand }`` as key -> value node; ``None`` for anything else."""
        node = _operand(self._through_const(node, 0))
        if node is None or node.type != "object":
            return None
        fields: dict[str, Node] = {}
        for member in node.named_children:
            if member.type == "pair":
                key = _key(self._source, member.child_by_field_name("key"))
                value = member.child_by_field_name("value")
                if key is not None and value is not None:
                    fields[key] = value
            elif member.type == "shorthand_property_identifier":
                fields[node_text(self._source, member)] = member
        return fields

    def _through_const(self, node: Node | None, depth: int) -> Node | None:
        node = _operand(node)
        while (
            node is not None
            and node.type in {"identifier", "shorthand_property_identifier"}
            and depth < _MAX_DEPTH
        ):
            name = node_text(self._source, node)
            if name not in self._consts or not self._unique(name):
                return node
            node = _operand(self._consts[name])
            depth += 1
        return node

    def _type_defs(self, node: Node | None, depth: int) -> list[dict[str, object]]:
        """Where ``typeDefs`` come from: schema files anchored to this one, or why not."""
        if depth > _MAX_DEPTH:
            return [{"unsupported": "computed"}]
        node = _operand(self._through_const(node, 0))
        if node is None:
            return [{"unsupported": "computed"}]
        if node.type == "array":
            found: list[dict[str, object]] = []
            for item in node.named_children:
                if item.type != "comment":
                    found.extend(self._type_defs(item, depth + 1))
            return found
        if node.type in {"identifier", "shorthand_property_identifier"}:
            return [
                {
                    "unsupported": "imported"
                    if node_text(self._source, node) in self._imports
                    else "computed"
                }
            ]
        if node.type == "call_expression":
            callee = _callee(self._source, node)
            arguments = [
                c
                for c in (node.child_by_field_name("arguments") or node).named_children
                if c.type != "comment"
            ]
            if callee in _TAGS:
                return [{"unsupported": "inline-sdl"}]
            origin = self._callee_module(node)
            if callee == "parse" and origin == "graphql" and arguments:
                return self._type_defs(arguments[0], depth + 1)
            if callee == "mergeTypeDefs" and origin == "@graphql-tools/merge" and arguments:
                return self._type_defs(arguments[0], depth + 1)
            if callee == "readFileSync" and origin in _FS and arguments:
                path: dict[str, object] = dict(self._path(arguments[0]))
                return [path if "unsupported" in path else {"file": path["path"]}]
            if callee == "loadFilesSync" and origin == "@graphql-tools/load-files" and arguments:
                path = dict(self._path(arguments[0]))
                return [path if "unsupported" in path else {"glob": path["path"]}]
        if node.type in {"template_string", "string"}:
            return [{"unsupported": "inline-sdl"}]
        return [{"unsupported": "computed"}]

    def _callee_module(self, call: Node) -> str | None:
        """The module the called function was imported from, through a member of a
        namespace (``fs.readFileSync``) or by name."""
        function = _operand(call.child_by_field_name("function"))
        if function is None:
            return None
        if function.type == "identifier":
            return self._package(node_text(self._source, function))
        if function.type == "member_expression":
            target = function.child_by_field_name("object")
            if target is not None and target.type == "identifier":
                found = self._imports.get(node_text(self._source, target))
                if found is not None and found[1] in {_MODULE, "default"}:
                    return found[0]
        return None

    def _path(self, node: Node | None) -> dict[str, str]:
        """A path anchored to this file's directory, or why it is not one."""
        node = _operand(self._through_const(node, 0))
        if node is None:
            return {"unsupported": "computed"}
        if node.type == "string":
            return {"unsupported": "cwd-relative"}
        if node.type == "template_string":
            parts = [c for c in node.named_children if c.type != "comment"]
            if (
                len(parts) >= 1
                and parts[0].type == "template_substitution"
                and _is_dirname(self._source, parts[0])
                and all(p.type == "string_fragment" for p in parts[1:])
            ):
                rest = "".join(node_text(self._source, p) for p in parts[1:])
                return {"path": posixpath.normpath(rest.lstrip("/") or ".")}
            return {"unsupported": "computed"}
        if node.type == "call_expression":
            callee = _callee(self._source, node)
            arguments = [
                c
                for c in (node.child_by_field_name("arguments") or node).named_children
                if c.type != "comment"
            ]
            if callee in {"join", "resolve"} and self._callee_module(node) in _PATH and arguments:
                if not (
                    arguments[0].type == "identifier"
                    and node_text(self._source, arguments[0]) == "__dirname"
                ):
                    return {"unsupported": "cwd-relative"}
                literals = [
                    string_literal(self._source, a) if a.type == "string" else None
                    for a in arguments[1:]
                ]
                if any(literal is None for literal in literals):
                    return {"unsupported": "computed"}
                joined = (
                    posixpath.join(*[str(literal) for literal in literals]) if literals else "."
                )
                return {"path": posixpath.normpath(joined)}
        if node.type == "new_expression":
            constructor = node.child_by_field_name("constructor")
            arguments = [
                c
                for c in (node.child_by_field_name("arguments") or node).named_children
                if c.type != "comment"
            ]
            if (
                constructor is not None
                and node_text(self._source, constructor) == "URL"
                and len(arguments) == 2
                and arguments[0].type == "string"
                and node_text(self._source, arguments[1]) == "import.meta.url"
            ):
                literal = string_literal(self._source, arguments[0])
                if literal is not None:
                    return {"path": posixpath.normpath(literal)}
        return {"unsupported": "computed"}

    # -- resolver maps -------------------------------------------------------------

    def _shape(self, node: Node | None, owner: str, depth: int) -> dict[str, object]:
        """What a resolver map is, member by member, followed through this module."""
        node = _operand(node)
        if node is None or depth > _MAX_DEPTH:
            return {"unsupported": "computed"}
        if node.type in {"identifier", "shorthand_property_identifier"}:
            name = node_text(self._source, node)
            if name in self._consts and self._unique(name):
                return self._shape(self._consts[name], name, depth + 1)
            return {"ref": self._ref(node)}
        if node.type in {"arrow_function", "function_expression", "function"}:
            return {"function": node.start_point[0] + 1, "owner": owner}
        if node.type == "array":
            items = [c for c in node.named_children if c.type != "comment"][:_MAX_ENTRIES]
            return {"array": [self._shape(item, owner, depth + 1) for item in items]}
        if node.type == "call_expression":
            if _callee(self._source, node) == "mergeResolvers":
                arguments = [
                    c
                    for c in (node.child_by_field_name("arguments") or node).named_children
                    if c.type != "comment"
                ]
                if arguments:
                    return self._shape(arguments[0], owner, depth + 1)
            return {"unsupported": "computed"}
        if node.type != "object":
            return {"unsupported": "computed" if node.type not in _LITERALS else "literal"}
        entries: list[dict[str, object]] = []
        for member in node.named_children[:_MAX_ENTRIES]:
            line = member.start_point[0] + 1
            if member.type == "pair":
                key = _key(self._source, member.child_by_field_name("key"))
                if key is None:
                    entries.append(
                        {"key": None, "line": line, "value": {"unsupported": "computed-key"}}
                    )
                    continue
                value = self._shape(member.child_by_field_name("value"), owner, depth + 1)
                entries.append({"key": key, "line": line, "value": value})
            elif member.type == "shorthand_property_identifier":
                key = node_text(self._source, member)
                entries.append(
                    {"key": key, "line": line, "value": self._shape(member, owner, depth + 1)}
                )
            elif member.type == "method_definition":
                key = _key(self._source, member.child_by_field_name("name"))
                entries.append(
                    {"key": key, "line": line, "value": {"function": line, "owner": owner}}
                )
            elif member.type == "spread_element":
                inner = next((c for c in member.named_children if c.type != "comment"), None)
                entries.append({"spread": self._shape(inner, owner, depth + 1), "line": line})
        return {"object": entries, "owner": owner, "line": node.start_point[0] + 1}

    # -- claims ----------------------------------------------------------------------

    def references(self) -> list[ParsedReference]:
        """Every document and construction claim, then the exported object shapes."""
        shapes: list[ParsedReference] = []
        for name, names in sorted(self._exported.items()):
            value = _operand(self._consts.get(name))
            if value is None or value.type != "object" or not self._unique(name):
                continue
            shape = self._shape(value, name, 0)
            if not _resolver_like(shape):
                continue
            line = value.start_point[0] + 1
            shapes.append(
                pending_reference(
                    EdgeType.REFERENCES,
                    EntityType.SYMBOL,
                    name,
                    line,
                    value.end_point[0] + 1,
                    role=TS_OBJECT_SHAPE,
                    source_symbol=name,
                    shape=_json(shape),
                    exports=_json(sorted(set(names))),
                )
            )
        return [*self._claims, *shapes]


_LITERALS = frozenset(
    {"string", "number", "true", "false", "null", "undefined", "template_string", "regex"}
)


def _resolver_like(shape: dict[str, object]) -> bool:
    """An object with at least one function or name among its members, and no literal:
    a resolver map's shape, and not a configuration's."""
    found = {"leaf": False, "literal": False}

    def walk(node: object) -> None:
        if not isinstance(node, dict):
            return
        if node.get("unsupported") == "literal":
            found["literal"] = True
        if "function" in node or "ref" in node:
            found["leaf"] = True
        for key in ("object", "array"):
            for item in node.get(key, ()) or ():
                if isinstance(item, dict):
                    walk(item.get("value", item.get("spread", item)))

    walk(shape)
    return found["leaf"] and not found["literal"]


def _operand(node: Node | None) -> Node | None:
    while node is not None and node.type in _TRANSPARENT:
        inner = [c for c in node.named_children if c.type != "comment"]
        if not inner:
            break
        node = inner[0]
    return node


def _key(source: bytes, node: Node | None) -> str | None:
    if node is None:
        return None
    if node.type in {"property_identifier", "identifier"}:
        return node_text(source, node)
    if node.type == "string":
        return string_literal(source, node)
    return None


def _callee(source: bytes, call: Node) -> str | None:
    function = _operand(call.child_by_field_name("function"))
    if function is None:
        return None
    if function.type == "identifier":
        return node_text(source, function)
    if function.type == "member_expression":
        prop = function.child_by_field_name("property")
        return node_text(source, prop) if prop is not None else None
    return None


def _is_dirname(source: bytes, substitution: Node) -> bool:
    inner = next((c for c in substitution.named_children if c.type != "comment"), None)
    return inner is not None and node_text(source, inner) == "__dirname"


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
