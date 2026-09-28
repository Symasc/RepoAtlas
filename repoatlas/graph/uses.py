"""Following a use of an imported name to the declaration it names (``USES`` only).

A use through an import (:mod:`repoatlas.parsers.ts_uses`) carries the module it came
from and the name that module exports. The declaration is wherever that name leads,
through however many re-exporting files. This is the resolution the ``USES`` edge
relies on, and nothing else reads it: the ``IMPORTS``, ``REFERENCES`` and ``CALLS``
edges keep :meth:`~repoatlas.graph.modules.ModuleResolver.definition` exactly as it
was, so no existing edge changes.

It reads each file's own export table, and follows what that trace cannot:

* a renamed export (``export { a as b }``): followed by the name it is exported
  under, and resolved to the declaration by the name it is declared under;
* a default export: followed to the declaration or binding it names. A default that
  is an expression has no declaration, so a use of it stays unresolved;
* a name a file imports and exports again (``import { a } from "./x"; export { a }``):
  followed through that file's import;
* a namespace import's member (``ns.a``): followed as the name ``a``.

Every file passed through is kept, in order, with the name it exports the use by, so a
chain can say which name each hop spells. Wholesale re-exports (``export *``) that lead
to two different declarations answer nothing, as they do for the import graph, and a
use leaves its repository never: specifiers resolve within it.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from repoatlas.graph.modules import MAX_REEXPORT_HOPS, MAX_TRACE_STEPS, ModuleResolver
from repoatlas.graph.resolution import ResolutionIndex
from repoatlas.models.parsing import EdgeType, EntityType
from repoatlas.parsers.ts_uses import USES_EXPORTS
from repoatlas.storage.protocols import FileRow, UnresolvedRefRecord


@dataclass(frozen=True, slots=True)
class UseDefinition:
    """Where a used name is declared, and the way there."""

    file: FileRow
    #: The declaring entity; ``None`` when the name is a whole module (a namespace).
    symbol_id: int | None
    #: The name it is declared under; ``None`` for a whole module.
    name: str | None
    #: Re-exporting files passed through, in order...
    via: tuple[str, ...] = ()
    #: ...and the name each of them exports it under.
    via_names: tuple[str, ...] = ()


def export_table(claim: UnresolvedRefRecord) -> dict[str, Any] | None:
    """A file's export table, if ``claim`` is one."""
    if claim.edge_type is not EdgeType.USES or not claim.meta_json:
        return None
    if USES_EXPORTS not in claim.meta_json:
        return None
    raw = json.loads(claim.meta_json).get(USES_EXPORTS)
    return json.loads(raw) if isinstance(raw, str) else None


def use_resolution(meta: dict[str, str], **fields: Any) -> str:
    """The resolution record of a ``USES`` edge: how the name is used, then how it resolved."""
    record: dict[str, Any] = {
        "use": meta.get("use"),
        "form": meta.get("form"),
        "binding": meta.get("binding"),
        "occurrences": int(meta.get("occurrences") or 1),
    }
    if meta.get("type_only"):
        record["type_only"] = True
    record.update({key: value for key, value in fields.items() if value not in (None, [], ())})
    return json.dumps(record, sort_keys=True, separators=(",", ":"))


class UseResolver:
    """Resolves the ``USES`` claims of one global pass."""

    def __init__(
        self,
        modules: ModuleResolver,
        index: ResolutionIndex,
        claims: Sequence[UnresolvedRefRecord],
    ) -> None:
        self._modules = modules
        self._index = index
        self._tables: dict[int, dict[str, Any]] = {}
        for claim in claims:
            table = export_table(claim)
            if table is not None:
                self._tables[claim.file_id] = table
        self._traced: dict[tuple[int, str], tuple[UseDefinition | None, str | None]] = {}
        self._steps = 0
        #: Why each unresolved use stayed unresolved; measurement only.
        self.unresolved: Counter[str] = Counter()

    def resolve(self, origin: FileRow, meta: dict[str, str]) -> tuple[int, dict[str, Any]] | None:
        """The used entity's id and how it was reached, or ``None`` with the reason counted."""
        specifier = meta.get("module") or ""
        target = self._modules.resolve(origin, specifier)
        if target is None:
            self.unresolved["specifier"] += 1
            return None
        name = meta.get("imported")
        if name is None:
            if meta.get("binding") != "namespace":
                self.unresolved["no-name"] += 1
                return None
            definition: UseDefinition | None = UseDefinition(target.file, None, None)
        else:
            self._steps = 0
            definition, reason = self._trace(target.file, name, frozenset(), 0)[:2]
            if definition is None:
                self.unresolved[reason or "not-exported"] += 1
                return None
        assert definition is not None
        entity = definition.symbol_id
        fields: dict[str, Any] = {
            "basis": target.basis,
            "specifier": specifier,
            **dict(target.detail),
            "local": meta.get("local"),
            "imported": name,
            "defined_at": definition.file.rel_path,
            "name": definition.name,
            "via": list(definition.via),
            "via_names": list(definition.via_names),
        }
        if entity is None:
            fields["namespace"] = True
            entity = definition.file.file_entity_id
        return entity, fields

    def _trace(
        self, module: FileRow, name: str, visiting: frozenset[tuple[int, str]], depth: int
    ) -> tuple[UseDefinition | None, str | None, bool]:
        """The definition, why there is none, and whether a cycle or a bound cut the search."""
        key = (module.file_id, name)
        if key in self._traced:
            found, reason = self._traced[key]
            return found, reason, False
        if key in visiting or depth > MAX_REEXPORT_HOPS or self._steps >= MAX_TRACE_STEPS:
            return None, "bound", True
        self._steps += 1
        found, reason, cut = self._trace_once(module, name, visiting | {key}, depth)
        if not cut:
            self._traced[key] = (found, reason)
        return found, reason, cut

    def _trace_once(
        self, module: FileRow, name: str, visiting: frozenset[tuple[int, str]], depth: int
    ) -> tuple[UseDefinition | None, str | None, bool]:
        table = self._tables.get(module.file_id)
        if table is None:
            return None, "no-export-table", False
        spec = table.get("names", {}).get(name)
        if spec is not None:
            return self._follow(module, name, spec, visiting, depth)
        if name == "default":
            return None, "not-exported", False  # `export *` never passes a default on
        found: dict[tuple[int, int | None], UseDefinition] = {}
        cut_any = False
        for specifier in table.get("star", []):
            target = self._modules.resolve(module, specifier)
            if target is None:
                continue
            definition, _, cut = self._trace(target.file, name, visiting, depth + 1)
            cut_any = cut_any or cut
            if definition is not None:
                found.setdefault((definition.file.file_id, definition.symbol_id), definition)
        if len(found) > 1:
            return None, "ambiguous-star", cut_any
        if not found:
            return None, "not-exported", cut_any
        return _through(module, name, next(iter(found.values()))), None, cut_any

    def _follow(
        self,
        module: FileRow,
        exported: str,
        spec: list[str],
        visiting: frozenset[tuple[int, str]],
        depth: int,
    ) -> tuple[UseDefinition | None, str | None, bool]:
        kind = spec[0] if spec else ""
        if kind == "local" and len(spec) == 3 and spec[2].isdigit():
            symbol = self._symbol(module, spec[1], int(spec[2]))
            if symbol is None:
                return None, "no-symbol", False
            return UseDefinition(module, symbol, spec[1]), None, False
        if kind in {"import", "namespace"} and len(spec) >= 2:
            target = self._modules.resolve(module, spec[1])
            if target is None:
                return None, "specifier", False
            if kind == "namespace":
                return (
                    UseDefinition(target.file, None, None, (module.rel_path,), (exported,)),
                    None,
                    False,
                )
            if len(spec) != 3:
                return None, "unsupported", False
            found, reason, cut = self._trace(target.file, spec[2], visiting, depth + 1)
            return (None if found is None else _through(module, exported, found)), reason, cut
        if kind == "expr":
            return None, "default-expression", False
        return None, kind or "unsupported", False

    def _symbol(self, file: FileRow, name: str, line: int) -> int | None:
        """The one top-level declaration of ``name`` at ``line`` in ``file``."""
        rows = [
            row
            for row in self._index.symbols_by_file.get(file.file_id, {}).get(name, [])
            if row.qualified_name == name and row.line_start == line
        ]
        if len(rows) > 1:
            rows = [row for row in rows if row.type is not EntityType.MODULE]
        ids = {row.entity_id for row in rows}
        return ids.pop() if len(ids) == 1 else None


def _through(module: FileRow, exported: str, definition: UseDefinition) -> UseDefinition:
    return UseDefinition(
        definition.file,
        definition.symbol_id,
        definition.name,
        (module.rel_path, *definition.via),
        (exported, *definition.via_names),
    )
