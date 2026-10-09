"""GraphQL parser (schema impact).

Reads GraphQL type-system and executable documents -- ``.graphql``, ``.gql`` and
``.graphqls`` files -- with graphql-core, pinned exactly (THIRD_PARTY_NOTICES.md): another
version is another parser. Like every parser here it is a pure function of the path and the
content. Nothing is executed, fetched, imported or resolved, and no other file is read.

**A definition is named by its schema coordinate**, the spelling the GraphQL specification
gives every element of a schema: ``Parcel``, ``Parcel.stage``,
``Query.parcels(stage:)``, ``Stage.PACKED``, ``@key``, ``@key(fields:)``. A named type is
a ``class``; everything inside one is a ``symbol`` under it. An extension is named
``extend <Type>``, so the definition it extends keeps its name to itself, and the members it
adds keep the coordinates they extend. ``meta["graphql"]`` says which element a symbol is;
a field, argument or input field also records how it is typed (``type_ref``, such as
``[Stage!]!``) and the named type inside (``named_type``).

**A type reference is linked here only when this file defines that type exactly once**, and
nothing else in the file holds its name. One file is not a schema: the type a field names
may be defined in another file, in several, or nowhere, and which definition counts depends
on which files some configuration puts together. So every other reference becomes a
``graphql-type`` claim that says why (``external`` or ``duplicate``), and no pass links it
by name. Built-in scalars name no definition and are not references.

**Operations and fragments** are symbols too, named by their own names (an anonymous
operation has none). Each is also a ``graphql-document`` claim carrying what it selects:
fields with their aliases and arguments, spreads, inline fragments, variables and the
literals passed. Its target is the operation's kind, or the fragment's type condition.
A document does not say which schema it is written against, so nothing here binds it to
one.

**Bounds.** graphql-core reads at most :data:`MAX_TOKENS` tokens. Past that the file is
refused with RepoAtlas's own wording, as is a syntax error, whose library message quotes the
source. A document's selections are recorded up to :data:`MAX_SELECTION_NODES` nodes and
:data:`MAX_DEPTH` levels. Past either bound the claim carries ``bound=exceeded`` and no
selections, never only some of them.
"""

from __future__ import annotations

import json
from bisect import bisect_right
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from graphql import GraphQLSyntaxError
from graphql import parse as parse_document
from graphql.language import ast
from graphql.language.lexer import Lexer
from graphql.language.printer import print_ast
from graphql.language.source import Source
from graphql.language.token_kind import TokenKind

from repoatlas.models.parsing import (
    EMBEDDED_DOCUMENT,
    PARSE_BOUND_EXCEEDED,
    PARSE_FAILED,
    SYNTAX_ERROR,
    EdgeType,
    EntityType,
    ParsedFile,
    ParseDiagnostic,
    ParsedReference,
    ParsedSymbol,
    Severity,
)
from repoatlas.parsers.http_facts import pending_reference
from repoatlas.parsers.marks import line_count, line_starts

_SUFFIXES = frozenset({".graphql", ".gql", ".graphqls"})

#: The most tokens graphql-core reads from one file. A sync reads files up to one mebibyte,
#: and one mebibyte of schema measured 145,000 tokens (a long enum) to 348,000 (fields with
#: one-letter names and types) -- at most about two seconds to parse. A file of valid
#: one-character tokens (``union U = A|A|A|...``) reaches a million, and is refused here
#: rather than read.
MAX_TOKENS = 400_000

#: The most selections, arguments, variables and values one document's claim records.
MAX_SELECTION_NODES = 1_024

#: The deepest selection or value one document's claim records.
MAX_DEPTH = 64

#: The pending-fact roles this parser records (``repoatlas.parsers.http_facts``).
GRAPHQL_TYPE = "graphql-type"
GRAPHQL_DOCUMENT = "graphql-document"
GRAPHQL_FACTS = frozenset({GRAPHQL_TYPE, GRAPHQL_DOCUMENT})

#: ``graphql-type`` reasons: no definition of the type in this file, or more than one
#: holder of its name.
EXTERNAL = "external"
DUPLICATE = "duplicate"

#: The specification's scalars. A schema never defines them, so naming one is no reference.
BUILTIN_SCALARS = frozenset({"Int", "Float", "String", "Boolean", "ID"})

#: How graphql-core 3.2.6 words the one syntax error that is RepoAtlas's own bound. Read to
#: choose the category, never repeated.
_BOUND_WORDING = "Document contains more than"

_TypeDefinition = (
    ast.ScalarTypeDefinitionNode
    | ast.ObjectTypeDefinitionNode
    | ast.InterfaceTypeDefinitionNode
    | ast.UnionTypeDefinitionNode
    | ast.EnumTypeDefinitionNode
    | ast.InputObjectTypeDefinitionNode
    | ast.ScalarTypeExtensionNode
    | ast.ObjectTypeExtensionNode
    | ast.InterfaceTypeExtensionNode
    | ast.UnionTypeExtensionNode
    | ast.EnumTypeExtensionNode
    | ast.InputObjectTypeExtensionNode
)

#: Each named-type definition and extension: its keyword and its ``meta["graphql"]`` role.
_TYPE_ROLES: dict[type[ast.Node], tuple[str, str]] = {
    ast.ScalarTypeDefinitionNode: ("scalar", "scalar"),
    ast.ObjectTypeDefinitionNode: ("type", "object"),
    ast.InterfaceTypeDefinitionNode: ("interface", "interface"),
    ast.UnionTypeDefinitionNode: ("union", "union"),
    ast.EnumTypeDefinitionNode: ("enum", "enum"),
    ast.InputObjectTypeDefinitionNode: ("input", "input"),
    ast.ScalarTypeExtensionNode: ("scalar", "scalar"),
    ast.ObjectTypeExtensionNode: ("type", "object"),
    ast.InterfaceTypeExtensionNode: ("interface", "interface"),
    ast.UnionTypeExtensionNode: ("union", "union"),
    ast.EnumTypeExtensionNode: ("enum", "enum"),
    ast.InputObjectTypeExtensionNode: ("input", "input"),
}


class GraphQLParser:
    """GraphQL schema and document files, read with graphql-core."""

    name = "graphql"
    version = "1"
    language = "GraphQL"

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in _SUFFIXES

    def parse(self, path: Path, content: str) -> ParsedFile:
        try:
            if _empty(content):
                return self._file()
            document = parse_document(content, max_tokens=MAX_TOKENS)
        except GraphQLSyntaxError as error:
            line = error.locations[0].line if error.locations else None
            if line is not None:
                line = max(1, min(line, line_count(content)))
            message = PARSE_BOUND_EXCEEDED if _BOUND_WORDING in error.message else SYNTAX_ERROR
            return self._file(
                diagnostics=(ParseDiagnostic(severity=Severity.ERROR, message=message, line=line),)
            )
        except Exception:
            # Nesting deeper than graphql-core's recursive descent reaches, or anything it
            # did not foresee. The file is refused; a parser never raises.
            return self._failed()

        try:
            reader = _Reader(content)
            reader.walk(document)
            references = reader.references()
        except Exception:
            return self._failed()
        return self._file(symbols=tuple(reader.symbols), references=references)

    def _failed(self) -> ParsedFile:
        return self._file(
            diagnostics=(ParseDiagnostic(severity=Severity.ERROR, message=PARSE_FAILED),)
        )

    def _file(
        self,
        *,
        symbols: tuple[ParsedSymbol, ...] = (),
        references: tuple[ParsedReference, ...] = (),
        diagnostics: tuple[ParseDiagnostic, ...] = (),
    ) -> ParsedFile:
        return ParsedFile(
            language=self.language,
            parser=self.name,
            parser_version=self.version,
            symbols=symbols,
            references=references,
            diagnostics=diagnostics,
        )


@dataclass(frozen=True, slots=True)
class EmbeddedDocument:
    """What one GraphQL document embedded in another language's source states."""

    symbols: tuple[ParsedSymbol, ...]
    claims: tuple[ParsedReference, ...]
    #: Character offsets each definition occupies in the text, to place interpolations.
    spans: tuple[tuple[int, int], ...]
    #: Whether the text also held type-system definitions, which are not read when embedded.
    type_system: bool


def read_embedded(text: str, *, first_line: int, owner: str) -> EmbeddedDocument | None:
    """The operations and fragments of ``text``, a document embedded at ``first_line`` of
    another file and belonging to the declaration ``owner`` there (``""`` for none).

    ``None`` when the text does not parse or passes a bound: an embedded document is
    read whole or not at all, and never raises. Text with type-system definitions only
    (inline SDL) is returned with nothing in it and ``type_system`` set. Its symbols are
    the owner's members, so they cut no chunk of the host file.
    """
    try:
        if _empty(text):
            return None
        document = parse_document(text, max_tokens=MAX_TOKENS)
        reader = _Reader(text, line_offset=first_line - 1, parent=owner, executable_only=True)
        reader.walk(document)
        claims = reader.references()
    except Exception:
        return None
    if not reader.symbols and not claims and not reader.type_system:
        return None
    return EmbeddedDocument(
        symbols=tuple(reader.symbols),
        claims=claims,
        spans=tuple(reader.spans),
        type_system=reader.type_system,
    )


def _empty(content: str) -> bool:
    """Whether ``content`` holds no token at all: only whitespace, commas and comments.

    graphql-core refuses such a file as a syntax error, but an empty schema file is an
    ordinary file with nothing in it, not a broken one.
    """
    return Lexer(Source(content)).advance().kind is TokenKind.EOF


@dataclass(frozen=True, slots=True)
class _TypeUse:
    """A named type a definition is typed by, implements, contains or extends."""

    source: str
    named: str
    line_start: int
    line_end: int


class _Exceeded(Exception):
    """A document's selections went past :data:`MAX_SELECTION_NODES` or :data:`MAX_DEPTH`."""


class _Budget:
    def __init__(self) -> None:
        self.left = MAX_SELECTION_NODES

    def take(self, depth: int) -> None:
        self.left -= 1
        if self.left < 0 or depth > MAX_DEPTH:
            raise _Exceeded


class _Reader:
    """One document's definitions, in source order."""

    def __init__(
        self,
        content: str,
        *,
        line_offset: int = 0,
        parent: str | None = None,
        executable_only: bool = False,
    ) -> None:
        self._starts = line_starts(content)
        self._offset = line_offset
        #: The declaration of another language an embedded document belongs to.
        self._parent = parent
        self._executable_only = executable_only
        #: Whether type-system definitions were present and, being embedded, not read.
        self.type_system = False
        #: Character offsets each definition occupies.
        self.spans: list[tuple[int, int]] = []
        self.symbols: list[ParsedSymbol] = []
        self._claims: list[ParsedReference] = []
        self._uses: list[_TypeUse] = []
        #: Named-type definitions (never extensions) by name.
        self._defined: Counter[str] = Counter()

    # -- positions ---------------------------------------------------------------------

    def _lines(self, node: ast.Node) -> tuple[int, int]:
        """The lines ``node`` occupies, from its offsets. A token's own line is where it
        starts, which for a block string is not where it ends."""
        loc = node.loc
        assert loc is not None, "parsed with locations"
        start = bisect_right(self._starts, loc.start)
        end = max(start, bisect_right(self._starts, loc.end - 1))
        return start + self._offset, end + self._offset

    # -- definitions -------------------------------------------------------------------

    def walk(self, document: ast.DocumentNode) -> None:
        for definition in document.definitions:
            if definition.loc is not None:
                self.spans.append((definition.loc.start, definition.loc.end))
            executable = isinstance(
                definition, (ast.OperationDefinitionNode, ast.FragmentDefinitionNode)
            )
            if self._executable_only and not executable:
                self.type_system = True
                continue
            if isinstance(definition, tuple(_TYPE_ROLES)):
                self._type(definition)  # type: ignore[arg-type]
            elif isinstance(definition, (ast.SchemaDefinitionNode, ast.SchemaExtensionNode)):
                self._schema(definition)
            elif isinstance(definition, ast.DirectiveDefinitionNode):
                self._directive(definition)
            elif isinstance(definition, ast.OperationDefinitionNode):
                self._operation(definition)
            elif isinstance(definition, ast.FragmentDefinitionNode):
                self._fragment(definition)

    def _symbol(
        self,
        node: ast.Node,
        entity: EntityType,
        name: str,
        qualified: str,
        *,
        signature: str,
        parent: str | None = None,
        **meta: str | None,
    ) -> None:
        start, end = self._lines(node)
        self.symbols.append(
            ParsedSymbol(
                kind=entity,
                name=name,
                qualified_name=qualified,
                line_start=start,
                line_end=end,
                signature=signature,
                parent=parent,
                meta={key: value for key, value in meta.items() if value is not None},
            )
        )

    def _use(self, source: str, node: ast.Node, named: str) -> None:
        start, end = self._lines(node)
        self._uses.append(_TypeUse(source, named, start, end))

    def _type(self, node: _TypeDefinition) -> None:
        keyword, role = _TYPE_ROLES[type(node)]
        name = node.name.value
        extension = isinstance(node, ast.TypeExtensionNode)
        qualified = f"extend {name}" if extension else name
        interfaces: Sequence[ast.NamedTypeNode] = getattr(node, "interfaces", None) or ()
        signature = f"{'extend ' if extension else ''}{keyword} {name}"
        if interfaces:
            signature += " implements " + " & ".join(i.name.value for i in interfaces)
        if not extension:
            self._defined[name] += 1
        self._symbol(
            node,
            EntityType.CLASS,
            name,
            qualified,
            signature=signature,
            graphql=role,
            extension="true" if extension else None,
            directives=_directives(node.directives),
        )
        if extension:
            # What an extension adds means nothing without the type it extends.
            self._use(qualified, node.name, name)
        for interface in interfaces:
            self._use(qualified, interface, interface.name.value)
        for member in getattr(node, "types", None) or ():
            self._use(qualified, member, member.name.value)
        for value in getattr(node, "values", None) or ():
            self._symbol(
                value,
                EntityType.SYMBOL,
                value.name.value,
                f"{name}.{value.name.value}",
                signature=value.name.value,
                parent=qualified,
                graphql="enum-value",
                directives=_directives(value.directives),
            )
        for field in getattr(node, "fields", None) or ():
            coordinate = f"{name}.{field.name.value}"
            if isinstance(field, ast.InputValueDefinitionNode):
                self._input_value(field, coordinate, qualified, "input-field")
            else:
                self._field(field, coordinate, qualified)

    def _field(self, node: ast.FieldDefinitionNode, coordinate: str, parent: str) -> None:
        type_ref = print_ast(node.type)
        named = _named(node.type)
        arguments = node.arguments or ()
        self._symbol(
            node,
            EntityType.SYMBOL,
            node.name.value,
            coordinate,
            signature=f"{node.name.value}{_argument_list(arguments)}: {type_ref}",
            parent=parent,
            graphql="field",
            type_ref=type_ref,
            named_type=named.name.value,
            directives=_directives(node.directives),
        )
        self._use(coordinate, named, named.name.value)
        for argument in arguments:
            self._input_value(
                argument, f"{coordinate}({argument.name.value}:)", coordinate, "argument"
            )

    def _input_value(
        self, node: ast.InputValueDefinitionNode, coordinate: str, parent: str, role: str
    ) -> None:
        type_ref = print_ast(node.type)
        named = _named(node.type)
        default = print_ast(node.default_value) if node.default_value is not None else None
        self._symbol(
            node,
            EntityType.SYMBOL,
            node.name.value,
            coordinate,
            signature=_input_signature(node),
            parent=parent,
            graphql=role,
            type_ref=type_ref,
            named_type=named.name.value,
            default=default,
            directives=_directives(node.directives),
        )
        self._use(coordinate, named, named.name.value)

    def _schema(self, node: ast.SchemaDefinitionNode | ast.SchemaExtensionNode) -> None:
        extension = isinstance(node, ast.SchemaExtensionNode)
        qualified = "extend schema" if extension else "schema"
        operation_types = node.operation_types or ()
        roots = {root.operation.value: root.type.name.value for root in operation_types}
        self._symbol(
            node,
            EntityType.SYMBOL,
            "schema",
            qualified,
            signature=qualified,
            graphql="schema",
            extension="true" if extension else None,
            roots=_json(roots) if roots else None,
            directives=_directives(node.directives),
        )
        for root in operation_types:
            self._use(qualified, root.type, root.type.name.value)

    def _directive(self, node: ast.DirectiveDefinitionNode) -> None:
        coordinate = f"@{node.name.value}"
        locations = [location.value for location in node.locations]
        arguments = node.arguments or ()
        signature = f"directive {coordinate}{_argument_list(arguments)}"
        if node.repeatable:
            signature += " repeatable"
        signature += " on " + " | ".join(locations)
        self._symbol(
            node,
            EntityType.SYMBOL,
            coordinate,
            coordinate,
            signature=signature,
            graphql="directive",
            locations=",".join(locations),
            repeatable="true" if node.repeatable else None,
        )
        for argument in arguments:
            self._input_value(
                argument, f"{coordinate}({argument.name.value}:)", coordinate, "argument"
            )

    # -- documents ---------------------------------------------------------------------

    def _operation(self, node: ast.OperationDefinitionNode) -> None:
        kind = node.operation.value
        name = node.name.value if node.name is not None else None
        variables = node.variable_definitions or ()
        if name is not None:
            listed = ", ".join(f"${v.variable.name.value}: {print_ast(v.type)}" for v in variables)
            self._symbol(
                node,
                EntityType.SYMBOL,
                name,
                name,
                signature=f"{kind} {name}" + (f"({listed})" if listed else ""),
                parent=self._parent,
                graphql="operation",
                operation=kind,
                directives=_directives(node.directives),
                kind=EMBEDDED_DOCUMENT if self._parent is not None else None,
            )
        self._document(node, kind, name, variables, document="operation", operation=kind)

    def _fragment(self, node: ast.FragmentDefinitionNode) -> None:
        name = node.name.value
        condition = node.type_condition.name.value
        self._symbol(
            node,
            EntityType.SYMBOL,
            name,
            name,
            signature=f"fragment {name} on {condition}",
            parent=self._parent,
            graphql="fragment",
            on=condition,
            directives=_directives(node.directives),
            kind=EMBEDDED_DOCUMENT if self._parent is not None else None,
        )
        self._document(node, condition, name, (), document="fragment", on=condition)

    def _document(
        self,
        node: ast.OperationDefinitionNode | ast.FragmentDefinitionNode,
        target: str,
        name: str | None,
        variables: Iterable[ast.VariableDefinitionNode],
        **meta: str,
    ) -> None:
        start, end = self._lines(node)
        budget = _Budget()
        try:
            declared = [self._variable(variable, budget) for variable in variables]
            recorded = {"selections": _json(self._selections(node.selection_set, budget, 1))}
            if declared:
                recorded["variables"] = _json(declared)
        except _Exceeded:
            recorded = {"bound": "exceeded"}
        if name is not None:
            recorded["name"] = name
        self._claims.append(
            pending_reference(
                EdgeType.REFERENCES,
                EntityType.CLASS,
                target,
                start,
                end,
                role=GRAPHQL_DOCUMENT,
                source_symbol=name,
                **meta,
                **recorded,
            )
        )

    def _variable(self, node: ast.VariableDefinitionNode, budget: _Budget) -> dict[str, object]:
        budget.take(1)
        named = _named(node.type)
        variable: dict[str, object] = {
            "name": node.variable.name.value,
            "line": self._lines(node)[0],
            "type_ref": print_ast(node.type),
            "named_type": named.name.value,
        }
        if node.default_value is not None:
            variable["default"] = self._value(node.default_value, budget, 2)
        return variable

    def _selections(
        self, selection_set: ast.SelectionSetNode | None, budget: _Budget, depth: int
    ) -> list[dict[str, object]]:
        if selection_set is None:
            return []
        recorded: list[dict[str, object]] = []
        for selection in selection_set.selections:
            budget.take(depth)
            if isinstance(selection, ast.FieldNode):
                item: dict[str, object] = {
                    "field": selection.name.value,
                    "line": self._lines(selection.name)[0],
                }
                if selection.alias is not None:
                    item["alias"] = selection.alias.value
                if selection.arguments:
                    item["arguments"] = [
                        {
                            "name": argument.name.value,
                            "line": self._lines(argument)[0],
                            "value": self._value(argument.value, budget, depth + 1),
                        }
                        for argument in selection.arguments
                    ]
                if selection.selection_set is not None:
                    item["selections"] = self._selections(
                        selection.selection_set, budget, depth + 1
                    )
            elif isinstance(selection, ast.FragmentSpreadNode):
                item = {"spread": selection.name.value, "line": self._lines(selection)[0]}
            elif isinstance(selection, ast.InlineFragmentNode):
                condition = selection.type_condition
                item = {
                    "on": condition.name.value if condition is not None else None,
                    "line": self._lines(selection)[0],
                    "selections": self._selections(selection.selection_set, budget, depth + 1),
                }
            else:  # pragma: no cover - the specification has three selections
                continue
            recorded.append(item)
        return recorded

    def _value(self, node: ast.ValueNode, budget: _Budget, depth: int) -> dict[str, object]:
        budget.take(depth)
        if isinstance(node, ast.EnumValueNode):
            return {"enum": node.value}
        if isinstance(node, ast.VariableNode):
            return {"variable": node.name.value}
        if isinstance(node, ast.ListValueNode):
            return {"list": [self._value(item, budget, depth + 1) for item in node.values]}
        if isinstance(node, ast.ObjectValueNode):
            return {
                "object": [
                    {"name": item.name.value, "value": self._value(item.value, budget, depth + 1)}
                    for item in node.fields
                ]
            }
        printed = print_ast(node)
        # A long literal is never what binds a document to a schema: kept as a literal,
        # without its text.
        return {"literal": printed if len(printed) <= _MAX_LITERAL else None}

    # -- references --------------------------------------------------------------------

    def references(self) -> tuple[ParsedReference, ...]:
        """Every type use, linked here or claimed, then every document claim."""
        holders = Counter(symbol.qualified_name for symbol in self.symbols)
        decided: list[ParsedReference] = []
        for use in self._uses:
            if use.named in BUILTIN_SCALARS:
                continue
            defined = self._defined[use.named]
            if defined == 1 and holders[use.named] == 1:
                decided.append(
                    ParsedReference(
                        edge_type=EdgeType.REFERENCES,
                        target_kind=EntityType.CLASS,
                        target_key=use.named,
                        line_start=use.line_start,
                        line_end=use.line_end,
                        source_symbol=use.source,
                    )
                )
                continue
            decided.append(
                pending_reference(
                    EdgeType.REFERENCES,
                    EntityType.CLASS,
                    use.named,
                    use.line_start,
                    use.line_end,
                    role=GRAPHQL_TYPE,
                    source_symbol=use.source,
                    reason=EXTERNAL if defined == 0 else DUPLICATE,
                )
            )
        return (*decided, *self._claims)


#: The longest literal a document's claim keeps the text of.
_MAX_LITERAL = 256


def _named(node: ast.TypeNode) -> ast.NamedTypeNode:
    """The named type inside any list and non-null wrappers."""
    while not isinstance(node, ast.NamedTypeNode):
        node = node.type  # type: ignore[attr-defined]
    return node


def _argument_list(arguments: Sequence[ast.InputValueDefinitionNode]) -> str:
    if not arguments:
        return ""
    return "(" + ", ".join(_input_signature(argument) for argument in arguments) + ")"


def _input_signature(node: ast.InputValueDefinitionNode) -> str:
    signature = f"{node.name.value}: {print_ast(node.type)}"
    if node.default_value is not None:
        signature += f" = {print_ast(node.default_value)}"
    return signature


def _directives(directives: Sequence[ast.DirectiveNode] | None) -> str | None:
    """The directives applied to a definition, with their arguments as written."""
    if not directives:
        return None
    return _json(
        [
            {
                "name": directive.name.value,
                "arguments": {
                    argument.name.value: print_ast(argument.value)
                    for argument in directive.arguments or ()
                },
            }
            for directive in directives
        ]
    )


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
