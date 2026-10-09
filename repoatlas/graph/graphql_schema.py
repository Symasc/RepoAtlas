"""A schema assembled from indexed SDL files: what documents and resolvers are bound against.

Built from the symbols the GraphQL parser recorded (``meta["graphql"]``, ``named_type``),
never by reading a file. A schema is the files one configuration or one construction loads,
together: GraphQL Code Generator and graphql-tools merge their definitions, so a name defined
in two of them is one type whose coordinates are held by every definition that states them.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from repoatlas.models.parsing import EntityType
from repoatlas.storage.protocols import FileRow, SymbolRow

#: The specification's default root operation types.
DEFAULT_ROOTS = {"query": "Query", "mutation": "Mutation", "subscription": "Subscription"}
BUILTIN_SCALARS = frozenset({"Int", "Float", "String", "Boolean", "ID"})
#: Coordinates a resolver map may provide for a type rather than for one of its fields.
TYPE_LEVEL_KEYS = frozenset({"__resolveReference", "__resolveType", "__isTypeOf"})


@dataclass(slots=True)
class Schema:
    files: tuple[FileRow, ...] = ()
    #: Type name -> every definition and extension of it.
    types: dict[str, list[SymbolRow]] = field(default_factory=dict)
    #: Type name -> its kind (``object``, ``enum``, ...), from its definitions.
    kinds: dict[str, str] = field(default_factory=dict)
    #: Coordinate (``Type.field``, ``Type.field(arg:)``, ``Enum.VALUE``) -> its symbols.
    coordinates: dict[str, list[SymbolRow]] = field(default_factory=dict)
    #: Coordinate -> the named type a field, argument or input field is typed by.
    named: dict[str, str] = field(default_factory=dict)
    roots: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_ROOTS))

    def fields(self, type_name: str) -> list[str]:
        """Every field coordinate of a type, in coordinate order."""
        prefix = f"{type_name}."
        return sorted(
            c
            for c in self.coordinates
            if c.startswith(prefix) and "(" not in c and self._role(c) == "field"
        )

    def _role(self, coordinate: str) -> str | None:
        symbols = self.coordinates.get(coordinate) or []
        return symbols[0].meta.get("graphql") if symbols else None


def symbols_by_file(symbols: Sequence[SymbolRow]) -> dict[int, list[SymbolRow]]:
    """Each file's symbols in source order, never row order: the same files decide the same
    way after any rebuild."""
    found: dict[int, list[SymbolRow]] = defaultdict(list)
    for symbol in symbols:
        found[symbol.file_id].append(symbol)
    for listed in found.values():
        listed.sort(key=lambda s: (s.line_start or 0, s.qualified_name or "", s.name, str(s.type)))
    return dict(found)


def build_schema(files: Sequence[FileRow], by_file: Mapping[int, Sequence[SymbolRow]]) -> Schema:
    schema = Schema(files=tuple(files))
    types: dict[str, list[SymbolRow]] = defaultdict(list)
    kinds: dict[str, set[str]] = defaultdict(set)
    coordinates: dict[str, list[SymbolRow]] = defaultdict(list)
    for row in files:
        for symbol in by_file.get(row.file_id, ()):
            meta = symbol.meta
            role = meta.get("graphql")
            if role is None or symbol.qualified_name is None:
                continue
            if symbol.type is EntityType.CLASS:
                types[symbol.name].append(symbol)
                if meta.get("extension") != "true":
                    kinds[symbol.name].add(str(role))
            elif role in {"field", "argument", "input-field", "enum-value"}:
                coordinates[symbol.qualified_name].append(symbol)
                named = meta.get("named_type")
                if named is not None:
                    schema.named.setdefault(symbol.qualified_name, named)
            elif role == "schema":
                roots = meta.get("roots")
                if roots:
                    for operation, type_name in json.loads(roots).items():
                        schema.roots[str(operation)] = str(type_name)
    schema.types = dict(types)
    schema.kinds = {name: sorted(found)[0] for name, found in kinds.items() if len(found) == 1}
    schema.coordinates = dict(coordinates)
    return schema
