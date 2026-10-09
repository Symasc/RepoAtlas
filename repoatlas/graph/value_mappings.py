"""Explicit typed value mappings, and comparisons a declared type makes about a value.

A pure planner: it reads ``value-mapping`` and ``typed-comparison`` claims
(:mod:`repoatlas.parsers.ts_persistence`, :mod:`repoatlas.parsers.py_persistence`), the
effective ``USES`` edges of the declarations that make them, and what the Prisma client
binds (:mod:`repoatlas.graph.prisma_schema`). It writes nothing.

**Types are resolved, never matched.** A type a declaration names is the one declaration of
that name it uses -- an effective ``USES`` edge from it, or the Prisma client binding of a
name it imports -- and an enum is a declaration whose members record their values.

**A mapping** ``m: Record<A, B>`` / ``m: dict[A, B]`` whose key type is an enum: each entry
whose key is a member of ``A`` (``A.X``, or a literal equal to a member's value) and whose
value is a member of ``B`` is ``MAPS_TO`` from the one to the other, asserted at the entry's
line; when ``B`` is an enum too, ``A`` itself is ``MAPS_TO`` ``B``, at the declaration's
line. Decisions (``link_decisions``, family ``value_mappings``, subject
``<repository>:<path>::<name>``): ``linked`` when every member of ``A`` has an entry, else
``mapping-incomplete`` listing those without one. A mapping whose key type is not an enum
is not decided.

**A comparison** ``v.f == "lit"``: ``v``'s type resolves to a class, the class records its
field ``f`` as naming a type, that name resolves -- through the class's own uses -- to an
enum, and the literal is one of its members' values. The comparing declaration then
``REFERENCES`` that member (evidence ``typed_comparison``). Anything short of the whole chain
links nothing; a value that happens to equal another enum's member is never a candidate.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from repoatlas.models.parsing import ENUM_MEMBER, EdgeType, EntityType
from repoatlas.parsers.py_persistence import TYPED_COMPARISON
from repoatlas.parsers.ts_persistence import VALUE_MAPPING
from repoatlas.storage.protocols import (
    EdgeRecord,
    FileRow,
    LinkDecisionRow,
    SymbolRow,
    UnresolvedRefRecord,
)

FAMILY = "value_mappings"
MAPPING_EVIDENCE = "value_mapping"
COMPARISON_EVIDENCE = "typed_comparison"

LINKED = "linked"
MAPPING_INCOMPLETE = "mapping-incomplete"

#: What a type name can name: a class, or a declaration recorded as a symbol (a
#: TypeScript enum, interface or type alias).
_DECLARATIONS = frozenset({EntityType.CLASS, EntityType.SYMBOL})


@dataclass(frozen=True, slots=True)
class MappingsPlan:
    edges: tuple[EdgeRecord, ...] = ()
    decisions: tuple[LinkDecisionRow, ...] = ()
    crossings: int = 0
    detail: str = ""


def plan_value_mappings(
    claims: Sequence[UnresolvedRefRecord],
    uses_of: Callable[[int], Sequence[int]],
    symbols: Mapping[int, SymbolRow],
    by_file: Mapping[int, Sequence[SymbolRow]],
    bound: Mapping[tuple[int, str], SymbolRow],
    files: Mapping[int, FileRow],
    *,
    producer: str,
) -> MappingsPlan:
    """Link every typed mapping's entries and every typed comparison. Writes nothing.

    ``uses_of`` gives a declaration's effective ``USES`` targets, by entity id.
    """
    planner = _Planner(uses_of, symbols, by_file, bound, files, producer)
    for claim in sorted(claims, key=lambda c: (c.file_id, c.line_start or 0, c.src_id)):
        role = json.loads(claim.meta_json or "{}").get("pending_fact")
        if role == VALUE_MAPPING:
            planner.mapping(claim)
        elif role == TYPED_COMPARISON:
            planner.comparison(claim)
    decisions = sorted(planner.decisions.values(), key=lambda r: r.subject)
    counts: dict[str, int] = defaultdict(int)
    for row in decisions:
        counts[row.decision] += 1
    detail = ", ".join(f"{n} {d}" for d, n in sorted(counts.items()))
    return MappingsPlan(
        edges=tuple(
            sorted(planner.edges.values(), key=lambda e: (e.src_id, e.dst_id, e.line_start or 0))
        ),
        decisions=tuple(decisions),
        crossings=planner.crossings,
        detail=f"value mappings: {detail or 'none'}; {planner.compared} comparison(s) linked",
    )


class _Planner:
    def __init__(
        self,
        uses_of: Callable[[int], Sequence[int]],
        symbols: Mapping[int, SymbolRow],
        by_file: Mapping[int, Sequence[SymbolRow]],
        bound: Mapping[tuple[int, str], SymbolRow],
        files: Mapping[int, FileRow],
        producer: str,
    ) -> None:
        self._uses_of = uses_of
        self._symbols = symbols
        self._by_file = by_file
        self._bound = bound
        self._files = files
        self._producer = producer
        self.edges: dict[tuple[int, int, int], EdgeRecord] = {}
        self.decisions: dict[str, LinkDecisionRow] = {}
        self.crossings = 0
        self.compared = 0

    # -- types -----------------------------------------------------------------------

    def _type(self, declaration: int, name: str) -> SymbolRow | None:
        """The one declaration named ``name`` that ``declaration`` uses."""
        found = {
            target.entity_id: target
            for target in (self._symbols.get(t) for t in self._uses_of(declaration))
            if target is not None
            and target.name == name
            and target.type in _DECLARATIONS
            and target.meta.get("kind") != ENUM_MEMBER
        }
        prisma = self._bound.get((declaration, name))
        if prisma is not None:
            found[prisma.entity_id] = prisma
        return next(iter(found.values())) if len(found) == 1 else None

    def _members(self, enum: SymbolRow) -> dict[str, SymbolRow]:
        """An enum's members by their own name, when it is one; else nothing."""
        prefix = f"{enum.qualified_name}."
        return {
            (s.qualified_name or "")[len(prefix) :]: s
            for s in self._by_file.get(enum.file_id, ())
            if (s.qualified_name or "").startswith(prefix)
            and "." not in (s.qualified_name or "")[len(prefix) :]
            and s.meta.get("kind") == ENUM_MEMBER
        }

    def _member(
        self, enum: SymbolRow, members: Mapping[str, SymbolRow], local: str, kind: str, text: str
    ) -> SymbolRow | None:
        if kind == "member":
            base, _, name = text.partition(".")
            return members.get(name) if base == local else None
        if kind == "literal":
            matches = [m for m in members.values() if m.meta.get("value") == text]
            return matches[0] if len(matches) == 1 else None
        return None

    # -- mappings --------------------------------------------------------------------

    def mapping(self, claim: UnresolvedRefRecord) -> None:
        origin = self._files.get(claim.file_id)
        if origin is None:
            return
        meta = json.loads(claim.meta_json or "{}")
        key_name, value_name = str(meta.get("key_type")), str(meta.get("value_type"))
        key_type = self._type(claim.src_id, key_name)
        keys = self._members(key_type) if key_type is not None else {}
        if key_type is None or not keys:
            return
        value_type = self._type(claim.src_id, value_name)
        values = self._members(value_type) if value_type is not None else {}
        subject = f"{origin.repo_slug}:{origin.rel_path}::{claim.target_key}"
        ref = hashlib.sha256(f"{FAMILY}\0{subject}".encode()).hexdigest()[:24]
        covered: set[int] = set()
        pairs: list[tuple[SymbolRow, SymbolRow, int]] = []
        for entry in json.loads(str(meta.get("entries") or "[]")):
            key_kind, key_text, value_kind, value_text, line = entry
            key = self._member(key_type, keys, key_name, key_kind, key_text)
            if key is None:
                continue
            covered.add(key.entity_id)
            if value_type is not None:
                value = self._member(value_type, values, value_name, value_kind, value_text)
                if value is not None:
                    pairs.append((key, value, int(line)))
        missing = sorted(name for name, m in keys.items() if m.entity_id not in covered)
        evidence: dict[str, object] = {
            "keys": f"{_at(self._files, key_type)}",
            "entries": len(covered),
        }
        if value_type is not None:
            evidence["values"] = _at(self._files, value_type)
        if missing:
            evidence["missing"] = missing
        self.decisions[ref] = LinkDecisionRow(
            decision_ref=ref,
            family=FAMILY,
            repo_id=claim.repo_id,
            file_id=claim.file_id,
            src_id=claim.src_id,
            line_start=claim.line_start,
            line_end=claim.line_end,
            subject=subject,
            decision=MAPPING_INCOMPLETE if missing else LINKED,
            evidence_json=json.dumps(evidence, sort_keys=True, separators=(",", ":")),
        )
        if value_type is not None and values:
            # The annotation maps one enum type to the other: what the key type gains or
            # loses, the value type must answer for.
            pairs.insert(0, (key_type, value_type, claim.line_start or 0))
        for key, value, line in pairs:
            self._edge(
                key,
                value,
                origin,
                line,
                EdgeType.MAPS_TO,
                {
                    MAPPING_EVIDENCE: {
                        "mapping": f"{origin.repo_slug}/{origin.rel_path}:{line}",
                        "decision_ref": ref,
                    }
                },
            )

    # -- comparisons -----------------------------------------------------------------

    def comparison(self, claim: UnresolvedRefRecord) -> None:
        origin = self._files.get(claim.file_id)
        if origin is None:
            return
        meta = json.loads(claim.meta_json or "{}")
        model = self._type(claim.src_id, str(meta.get("type")))
        if model is None:
            return
        fields = json.loads(model.meta.get("fields") or "{}")
        named = fields.get(str(meta.get("field")))
        enum = self._type(model.entity_id, str(named)) if named else None
        members = self._members(enum) if enum is not None else {}
        if enum is None or not members:
            return
        for value in json.loads(str(meta.get("values") or "[]")):
            matches = [m for m in members.values() if m.meta.get("value") == value]
            if len(matches) != 1:
                continue
            self.compared += 1
            self._edge(
                self._symbols.get(claim.src_id),
                matches[0],
                origin,
                claim.line_start or 0,
                EdgeType.REFERENCES,
                {
                    COMPARISON_EVIDENCE: {
                        "type": _at(self._files, model),
                        "field": meta.get("field"),
                        "enum": _at(self._files, enum),
                        "value": value,
                    }
                },
                src_id=claim.src_id,
            )

    # -- records ---------------------------------------------------------------------

    def _edge(
        self,
        source: SymbolRow | None,
        target: SymbolRow,
        origin: FileRow,
        line: int,
        edge_type: EdgeType,
        resolution: Mapping[str, object],
        *,
        src_id: int | None = None,
    ) -> None:
        source_id = src_id if src_id is not None else (source.entity_id if source else None)
        if source_id is None:
            return
        key = (source_id, target.entity_id, line)
        if key in self.edges:
            return
        source_repo = source.repo_id if source is not None else origin.repo_id
        if source_repo != target.repo_id:
            self.crossings += 1
        self.edges[key] = EdgeRecord(
            src_id=source_id,
            dst_id=target.entity_id,
            type=edge_type,
            repo_id=origin.repo_id,
            file_id=origin.file_id,
            line_start=line,
            line_end=line,
            parser=self._producer,
            resolution=json.dumps(resolution, sort_keys=True),
        )


def _at(files: Mapping[int, FileRow], symbol: SymbolRow) -> str:
    row = files.get(symbol.file_id)
    where = f"{row.repo_slug}/{row.rel_path}" if row else "?"
    return f"{where}:{symbol.line_start}"
