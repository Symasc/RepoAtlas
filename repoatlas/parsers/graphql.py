"""Pure GraphQL SDL and document facts; schema binding belongs to the linker."""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from pathlib import Path

from graphql import GraphQLError, parse, print_ast
from graphql.language.ast import (
    DefinitionNode,
    FragmentDefinitionNode,
    NamedTypeNode,
    NameNode,
    Node,
    OperationDefinitionNode,
)

from repoatlas.models.parsing import (
    SYNTAX_ERROR,
    EdgeType,
    EntityType,
    ParsedFile,
    ParseDiagnostic,
    ParsedReference,
    ParsedSymbol,
    Severity,
)

SCHEMA_FACT = "graphql-fact"
MAX_TOKENS = 100_000
PARSE_LIMIT_MESSAGE = "GraphQL token limit exceeded"
_SCALARS = frozenset({"String", "Int", "Float", "Boolean", "ID"})


def _name(node: Node) -> str | None:
    name = getattr(node, "name", None)
    return name.value if isinstance(name, NameNode) else None


def _children(node: Node) -> list[Node]:
    children: list[Node] = []
    for key in node.keys:
        value = getattr(node, key, None)
        if isinstance(value, Node):
            children.append(value)
        elif isinstance(value, tuple):
            children.extend(child for child in value if isinstance(child, Node))
    return children


class GraphQLParser:
    name = "graphql"
    version = "1"
    language = "GraphQL"

    def supports(self, path: Path) -> bool:
        return path.suffix in {".graphql", ".gql", ".graphqls"}

    def parse(self, path: Path, content: str) -> ParsedFile:
        lines = [0, *(index + 1 for index, char in enumerate(content) if char == "\n")]
        symbols: list[ParsedSymbol] = []
        references: list[ParsedReference] = []

        def span(node: Node) -> tuple[int, int]:
            assert node.loc is not None
            return (
                bisect_right(lines, node.loc.start),
                bisect_right(lines, max(node.loc.start, node.loc.end - 1)),
            )

        def symbol(node: Node, coordinate: str, parent: str | None = None) -> None:
            start, end = span(node)
            symbols.append(
                ParsedSymbol(
                    kind=EntityType.SYMBOL,
                    name=coordinate.rsplit(".", 1)[-1],
                    qualified_name=coordinate,
                    parent=parent,
                    line_start=start,
                    line_end=end,
                    signature=print_ast(node),
                    meta={"graphql_kind": node.kind},
                )
            )

        def reference(node: Node, source: str, target: str, *, pending: bool) -> None:
            start, end = span(node)
            references.append(
                ParsedReference(
                    edge_type=EdgeType.REFERENCES,
                    target_kind=EntityType.SYMBOL,
                    target_key=target,
                    source_symbol=source,
                    line_start=start,
                    line_end=end,
                    meta={SCHEMA_FACT: "type"} if pending else {},
                )
            )

        try:
            document = parse(content, max_tokens=MAX_TOKENS)
        except (GraphQLError, RecursionError) as error:
            line = None
            message = SYNTAX_ERROR
            if isinstance(error, GraphQLError) and error.locations:
                line = error.locations[0].line
            if isinstance(error, GraphQLError) and error.message.startswith(
                "Syntax Error: Document contains more than"
            ):
                message = PARSE_LIMIT_MESSAGE
            return ParsedFile(
                language=self.language,
                parser=self.name,
                parser_version=self.version,
                diagnostics=(ParseDiagnostic(severity=Severity.ERROR, message=message, line=line),),
            )

        definitions: list[DefinitionNode] = list(document.definitions)
        counts = Counter(
            name
            for definition in definitions
            if (name := _name(definition)) is not None
            and not isinstance(definition, (OperationDefinitionNode, FragmentDefinitionNode))
            and definition.kind != "directive_definition"
        )
        names = {name for name, count in counts.items() if count == 1}

        for definition in definitions:
            if isinstance(definition, (OperationDefinitionNode, FragmentDefinitionNode)):
                start, end = span(definition)
                name = definition.name.value if definition.name else f"anonymous@{start}"
                coordinate = f"{definition.kind}.{name}"
                symbol(definition, coordinate)
                references.append(
                    ParsedReference(
                        edge_type=EdgeType.REFERENCES,
                        target_kind=EntityType.SYMBOL,
                        target_key=coordinate,
                        source_symbol=coordinate,
                        line_start=start,
                        line_end=end,
                        meta={SCHEMA_FACT: "document", "document": print_ast(definition)},
                    )
                )
                continue
            name = getattr(definition, "name", None)
            if name is None and definition.kind not in {"schema_definition", "schema_extension"}:
                continue
            coordinate = name.value if name is not None else "@schema"
            if definition.kind == "directive_definition":
                coordinate = f"@{coordinate}"
            symbol(definition, coordinate)
            pending_nodes: list[tuple[Node, str]] = [(definition, coordinate)]
            while pending_nodes:
                node, owner = pending_nodes.pop()
                for child in _children(node):
                    if child.kind in {
                        "field_definition",
                        "input_value_definition",
                        "enum_value_definition",
                    }:
                        member = f"{owner}.{_name(child)}"
                        symbol(child, member, owner)
                        pending_nodes.append((child, member))
                    elif isinstance(child, NamedTypeNode):
                        target = child.name.value
                        if target not in _SCALARS:
                            reference(child, owner, target, pending=target not in names)
                    else:
                        pending_nodes.append((child, owner))

        return ParsedFile(
            language=self.language,
            parser=self.name,
            parser_version=self.version,
            symbols=tuple(symbols),
            references=tuple(references),
        )
