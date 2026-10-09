"""Which declarations resolve which schema coordinates. A pure planner: it writes nothing.

The binding is the schema construction: a call that is given type definitions and resolvers
together (:mod:`repoatlas.parsers.ts_graphql`, :mod:`repoatlas.parsers.py_graphql`). Its type
definitions resolve to the admitted schema files they load, anchored to the constructing
file; its resolver map is followed member by member -- through constants of the module,
exported objects of relative modules (``ts-object-shape``) and Ariadne bindables -- to the
declaration that implements each field: the function a member names, or, for a function
written inline, the declaration holding the map. That declaration ``REFERENCES`` the
coordinate, at the line of the map entry that binds it. ``__resolveReference``,
``__resolveType`` and ``__isTypeOf`` reference the type itself.

Nothing is bound by name: a map entry binds only through the construction that uses the
map, to a coordinate of the schema that construction loads.

Decisions (``link_decisions``, family ``graphql_resolvers``):

* per construction, subject ``<repository>:<path>::<owner>``: ``linked``,
  ``schema-unsupported`` (type definitions computed, inline, or relative to the working
  directory), ``schema-missing`` (a path that names no admitted file),
  ``resolvers-unresolved`` (a member that cannot be followed: a package, a barrel, computed);
* per field of each object type the schema defines, subject ``<repository>:<schema
  path>::<Type.field>``: ``resolved``, ``default-resolver`` (no map gives it a resolver, so
  it reads the parent's property of that name), or ``resolver-unknown`` (its type's entry
  could not be followed);
* per map entry the schema does not have, subject ``<construction>::<Type.field>``:
  ``resolver-orphan``.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from repoatlas.graph.globs import match_admitted, resolve_file
from repoatlas.graph.graphql_schema import TYPE_LEVEL_KEYS, Schema, build_schema
from repoatlas.graph.resolution import ResolutionIndex
from repoatlas.models.parsing import EdgeType
from repoatlas.storage.protocols import (
    EdgeRecord,
    FileRow,
    LinkDecisionRow,
    SymbolRow,
    UnresolvedRefRecord,
)

FAMILY = "graphql_resolvers"

LINKED = "linked"
SCHEMA_UNSUPPORTED = "schema-unsupported"
SCHEMA_MISSING = "schema-missing"
RESOLVERS_UNRESOLVED = "resolvers-unresolved"
RESOLVED = "resolved"
DEFAULT_RESOLVER = "default-resolver"
RESOLVER_UNKNOWN = "resolver-unknown"
RESOLVER_ORPHAN = "resolver-orphan"

_SDL_SUFFIXES = (".graphql", ".gql", ".graphqls")
_MAX_DEPTH = 8


@dataclass(frozen=True, slots=True)
class ResolversPlan:
    edges: tuple[EdgeRecord, ...] = ()
    decisions: tuple[LinkDecisionRow, ...] = ()
    resolved: int = 0
    unresolved: int = 0
    crossings: int = 0
    detail: str = ""
    #: Per repository, the schema files each of its constructions loads: what it serves.
    served: Mapping[int, tuple[tuple[FileRow, ...], ...]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _Entry:
    """One resolver a map gives a type or a field."""

    type_name: str
    key: str
    #: The implementing declaration's entity id.
    implementation: int
    #: Where the entry binding it is written.
    file: FileRow
    line: int


@dataclass(slots=True)
class _Construction:
    entries: list[_Entry] = field(default_factory=list)
    unresolved: list[dict[str, object]] = field(default_factory=list)
    #: Types whose entry could not be followed, so their fields' resolvers are unknown.
    unknown: set[str] = field(default_factory=set)
    #: A whole map that could not be followed: no field's resolver is known.
    unknown_all: bool = False


def plan_resolvers(
    claims: Sequence[UnresolvedRefRecord],
    index: ResolutionIndex,
    files: Mapping[int, FileRow],
    by_file: Mapping[int, Sequence[SymbolRow]],
    *,
    producer: str,
) -> ResolversPlan:
    """Bind every schema construction's resolvers to the schema it loads. Writes nothing."""
    planner = _Planner(claims, index, files, by_file, producer)
    return planner.plan()


class _Planner:
    def __init__(
        self,
        claims: Sequence[UnresolvedRefRecord],
        index: ResolutionIndex,
        files: Mapping[int, FileRow],
        by_file: Mapping[int, Sequence[SymbolRow]],
        producer: str,
    ) -> None:
        from repoatlas.parsers.py_graphql import GRAPHQL_BINDABLE
        from repoatlas.parsers.ts_graphql import GRAPHQL_SCHEMA_CONSTRUCTION, TS_OBJECT_SHAPE

        self._index = index
        self._files = files
        self._by_file = by_file
        self._producer = producer
        self._constructions: list[UnresolvedRefRecord] = []
        #: (file, exported name) -> object shape.
        self._shapes: dict[tuple[int, str], dict[str, object]] = {}
        #: (file, name) -> Ariadne bindable claim.
        self._bindables: dict[tuple[int, str], UnresolvedRefRecord] = {}
        for claim in claims:
            if claim.file_id not in files:
                continue
            meta = _meta(claim)
            role = meta.get("pending_fact")
            if role == GRAPHQL_SCHEMA_CONSTRUCTION:
                self._constructions.append(claim)
            elif role == TS_OBJECT_SHAPE:
                shape = _decode_one(meta.get("shape"))
                for name in _decode_list(meta.get("exports")):
                    self._shapes[(claim.file_id, str(name))] = shape
            elif role == GRAPHQL_BINDABLE:
                self._bindables[(claim.file_id, str(meta.get("bindable")))] = claim
        self.decisions: dict[str, LinkDecisionRow] = {}
        self.edges: dict[tuple[int, int, int, int], EdgeRecord] = {}
        self.crossings = 0
        self.served: dict[int, list[tuple[FileRow, ...]]] = defaultdict(list)
        #: (schema file, coordinate) -> (implementing ids, unknown) across constructions.
        self._fields: dict[
            tuple[int, str], tuple[set[int], bool, SymbolRow, UnresolvedRefRecord, FileRow]
        ] = {}

    def plan(self) -> ResolversPlan:
        ordered = sorted(
            self._constructions,
            key=lambda c: (
                self._files[c.file_id].repo_slug,
                self._files[c.file_id].rel_path,
                c.line_start,
            ),
        )
        resolved = unresolved = 0
        for claim in ordered:
            if self._construction(claim):
                resolved += 1
            else:
                unresolved += 1
        for (_, coordinate), (implementations, unknown, symbol, claim, origin) in sorted(
            self._fields.items(), key=lambda item: (item[1][2].file_id, item[0][1])
        ):
            schema_file = self._files.get(symbol.file_id)
            if schema_file is None:
                continue
            subject = f"{schema_file.repo_slug}:{schema_file.rel_path}::{coordinate}"
            decision = (
                RESOLVED if implementations else RESOLVER_UNKNOWN if unknown else DEFAULT_RESOLVER
            )
            self._decide(
                claim, origin, subject, decision, {"implementations": len(implementations)}
            )
        decisions = sorted(self.decisions.values(), key=lambda row: row.subject)
        counts: dict[str, int] = defaultdict(int)
        for row in decisions:
            counts[row.decision] += 1
        detail = ", ".join(f"{count} {decision}" for decision, count in sorted(counts.items()))
        return ResolversPlan(
            edges=tuple(
                sorted(self.edges.values(), key=lambda e: (e.src_id, e.dst_id, e.line_start or 0))
            ),
            decisions=tuple(decisions),
            resolved=resolved,
            unresolved=unresolved,
            crossings=self.crossings,
            detail=f"resolvers: {detail or 'none'}",
            served={
                repo: tuple(sorted(set(sets), key=lambda s: [r.rel_path for r in s]))
                for repo, sets in sorted(self.served.items())
            },
        )

    # -- one construction ----------------------------------------------------------

    def _construction(self, claim: UnresolvedRefRecord) -> bool:
        meta = _meta(claim)
        origin = self._files[claim.file_id]
        owner = str(meta.get("owner") or f"schema@{claim.line_start}")
        subject = f"{origin.repo_slug}:{origin.rel_path}::{owner}"
        descriptors = _decode_list(meta.get("type_defs"))
        unsupported = [d for d in descriptors if isinstance(d, dict) and "unsupported" in d]
        if unsupported or not descriptors:
            self._decide(claim, origin, subject, SCHEMA_UNSUPPORTED, {"type_defs": descriptors})
            return False
        schema_files, missing = self._schema_files(origin, descriptors)
        if missing or not schema_files:
            self._decide(claim, origin, subject, SCHEMA_MISSING, {"missing": missing})
            return False
        schema = build_schema(schema_files, self._by_file)
        self.served[origin.repo_id].append(
            tuple(sorted(schema_files, key=lambda row: row.rel_path))
        )
        found = _Construction()
        if meta.get("library") == "ariadne":
            for ref in _decode_list(meta.get("bindables")):
                self._bindable(found, origin, ref if isinstance(ref, dict) else {})
        else:
            for shape in _decode_list(meta.get("resolvers")):
                if isinstance(shape, dict):
                    self._type_map(found, origin, shape, 0)
        ref = _ref(subject)
        orphans: list[str] = []
        for entry in found.entries:
            if entry.type_name not in schema.types:
                orphans.append(f"{entry.type_name}.{entry.key}")
                continue
            if entry.key in TYPE_LEVEL_KEYS:
                targets = [
                    s for s in schema.types[entry.type_name] if s.meta.get("extension") != "true"
                ]
            else:
                targets = schema.coordinates.get(f"{entry.type_name}.{entry.key}", [])
                targets = [s for s in targets if s.meta.get("graphql") in {"field", "enum-value"}]
            if not targets:
                orphans.append(f"{entry.type_name}.{entry.key}")
                continue
            for target in targets:
                self._edge(entry, target, ref, claim, origin)
        for orphan in sorted(set(orphans)):
            self._decide(claim, origin, f"{subject}::{orphan}", RESOLVER_ORPHAN, {})
        self._coverage(schema, found, claim, origin)
        evidence = {
            "schema": [f"{row.repo_slug}/{row.rel_path}" for row in schema_files],
            "entries": len(found.entries),
        }
        if found.unresolved:
            evidence["unresolved"] = found.unresolved
            self._decide(claim, origin, subject, RESOLVERS_UNRESOLVED, evidence)
            return False
        self._decide(claim, origin, subject, LINKED, evidence)
        return True

    def _schema_files(
        self, origin: FileRow, descriptors: list[object]
    ) -> tuple[list[FileRow], list[str]]:
        loaded: dict[int, FileRow] = {}
        missing: list[str] = []
        for descriptor in descriptors:
            if not isinstance(descriptor, dict):
                continue
            if "file" in descriptor:
                row = resolve_file(self._index, origin, str(descriptor["file"]))
                rows = [row] if row is not None else []
            elif "glob" in descriptor:
                rows = match_admitted(self._index, origin, str(descriptor["glob"])) or []
            else:  # Ariadne's load_schema_from_path: a file, or every schema file below a directory
                path = str(descriptor.get("tree"))
                row = resolve_file(self._index, origin, path)
                rows = [row] if row is not None else []
                if not rows:
                    for suffix in _SDL_SUFFIXES:
                        rows.extend(
                            match_admitted(self._index, origin, f"{path}/**/*{suffix}") or []
                        )
            rows = [r for r in rows if r.rel_path.endswith(_SDL_SUFFIXES)]
            if not rows:
                missing.append(str(next(iter(descriptor.values()))))
            for row in rows:
                loaded.setdefault(row.file_id, row)
        return list(loaded.values()), missing

    # -- following maps ------------------------------------------------------------

    def _type_map(
        self, found: _Construction, origin: FileRow, shape: dict[str, object], depth: int
    ) -> None:
        """A map from type names to field maps."""
        if depth > _MAX_DEPTH:
            found.unresolved.append({"depth": depth})
            return
        resolved = self._follow(found, origin, shape, depth)
        if resolved is None:
            found.unknown_all = True
            return
        shape, origin = resolved
        if "array" in shape:
            for item in shape["array"] or []:  # type: ignore[union-attr]
                if isinstance(item, dict):
                    self._type_map(found, origin, item, depth + 1)
            return
        if "object" not in shape:
            found.unknown_all = True
            found.unresolved.append({"map": shape})
            return
        for entry in shape["object"] or []:  # type: ignore[union-attr]
            if not isinstance(entry, dict):
                continue
            if "spread" in entry:
                self._type_map(found, origin, entry["spread"], depth + 1)  # type: ignore[arg-type]
                continue
            type_name = entry.get("key")
            if not isinstance(type_name, str):
                found.unresolved.append({"key": "computed", "line": entry.get("line")})
                continue
            self._field_map(
                found,
                origin,
                type_name,
                entry.get("value"),
                str(shape.get("owner") or ""),
                depth + 1,
            )

    def _field_map(
        self,
        found: _Construction,
        origin: FileRow,
        type_name: str,
        value: object,
        owner: str,
        depth: int,
    ) -> None:
        """One type's map from field names to resolvers."""
        resolved = self._follow(found, origin, value, depth) if isinstance(value, dict) else None
        if resolved is None:
            found.unknown.add(type_name)
            return
        shape, origin = resolved
        if "object" not in shape:
            found.unknown.add(type_name)
            found.unresolved.append({"type": type_name, "value": shape})
            return
        holder = str(shape.get("owner") or owner)
        for entry in shape["object"] or []:  # type: ignore[union-attr]
            if not isinstance(entry, dict):
                continue
            if "spread" in entry:
                self._field_map(found, origin, type_name, entry["spread"], holder, depth + 1)
                continue
            key = entry.get("key")
            line = int(entry.get("line") or 0)
            if not isinstance(key, str):
                # A computed key: some field of this type is bound, and which is unknown.
                found.unknown.add(type_name)
                found.unresolved.append({"type": type_name, "key": "computed", "line": line})
                continue
            implementation = self._implementation(
                found, origin, entry.get("value"), holder, depth + 1
            )
            if implementation is None:
                continue
            found.entries.append(_Entry(type_name, key, implementation, origin, line))

    def _implementation(
        self, found: _Construction, origin: FileRow, value: object, holder: str, depth: int
    ) -> int | None:
        if not isinstance(value, dict):
            return None
        if "function" in value:
            return self._declaration(origin, str(value.get("owner") or holder))
        if "ref" in value:
            ref = value["ref"]
            target = self._resolve(origin, ref if isinstance(ref, dict) else {})
            if target is None:
                found.unresolved.append({"ref": ref})
                return None
            row, name = target
            shape = self._shapes.get((row.file_id, name))
            if shape is not None and "object" in shape:
                # ``{ resolve, subscribe }``: the field's resolver is its ``resolve``.
                for entry in shape["object"] or []:  # type: ignore[union-attr]
                    if isinstance(entry, dict) and entry.get("key") in {"resolve", "subscribe"}:
                        return self._implementation(found, row, entry.get("value"), name, depth + 1)
                return None
            declared = self._declaration(row, name)
            if declared is None:
                found.unresolved.append({"ref": ref})
            return declared
        if "object" in value:
            for entry in value["object"] or []:  # type: ignore[union-attr]
                if isinstance(entry, dict) and entry.get("key") in {"resolve", "subscribe"}:
                    return self._implementation(
                        found, origin, entry.get("value"), holder, depth + 1
                    )
            return None
        if value.get("unsupported") == "literal":
            return None  # an enum's internal value: an explicit value mapping, not a resolver
        found.unresolved.append({"value": value})
        return None

    def _follow(
        self, found: _Construction, origin: FileRow, shape: object, depth: int
    ) -> tuple[dict[str, object], FileRow] | None:
        """A shape, with a name followed to the exported object it names."""
        seen = 0
        while isinstance(shape, dict) and "ref" in shape and seen <= _MAX_DEPTH:
            ref = shape["ref"]
            target = self._resolve(origin, ref if isinstance(ref, dict) else {})
            exported = self._shapes.get((target[0].file_id, target[1])) if target else None
            if target is None or exported is None:
                found.unresolved.append({"ref": ref})
                return None
            shape, origin = exported, target[0]
            seen += 1
        if not isinstance(shape, dict) or "unsupported" in shape:
            found.unresolved.append({"value": shape})
            return None
        return shape, origin

    def _resolve(self, origin: FileRow, ref: Mapping[str, object]) -> tuple[FileRow, str] | None:
        if "local" in ref:
            return origin, str(ref["local"])
        module = ref.get("module")
        name = ref.get("name")
        if not isinstance(module, str) or not isinstance(name, str):
            return None
        flavor = "python" if origin.rel_path.endswith((".py", ".pyi")) else "node"
        if flavor == "node" and not module.startswith("."):
            return None
        target = self._index.resolve_module(origin, module, flavor)
        return (target, name) if target is not None else None

    def _declaration(self, row: FileRow, name: str) -> int | None:
        """The one top-level declaration ``name`` in a file, or the file itself for ``""``."""
        if not name:
            return row.file_entity_id
        matches = [
            s
            for s in self._by_file.get(row.file_id, ())
            if s.qualified_name == name and s.name == name
        ]
        ids = {s.entity_id for s in matches}
        return ids.pop() if len(ids) == 1 else None

    def _bindable(self, found: _Construction, origin: FileRow, ref: Mapping[str, object]) -> None:
        target = self._resolve(origin, ref)
        claim = self._bindables.get((target[0].file_id, target[1])) if target else None
        if target is None or claim is None:
            found.unresolved.append({"bindable": dict(ref)})
            return
        row = target[0]
        type_name = claim.target_key
        holder = str(_meta(claim).get("bindable") or "")
        for item in _decode_list(_meta(claim).get("fields")):
            if not isinstance(item, dict):
                continue
            value = item.get("value")
            implementation: int | None
            if isinstance(value, dict) and "function" in value:
                implementation = self._declaration(row, holder)
            else:
                resolved = self._resolve(row, value if isinstance(value, dict) else {})
                implementation = self._declaration(*resolved) if resolved else None
            if implementation is None:
                found.unresolved.append({"field": item.get("field"), "value": value})
                continue
            found.entries.append(
                _Entry(
                    type_name,
                    str(item.get("field")),
                    implementation,
                    row,
                    int(item.get("line") or 0),
                )
            )

    # -- coverage ------------------------------------------------------------------

    def _coverage(
        self, schema: Schema, found: _Construction, claim: UnresolvedRefRecord, origin: FileRow
    ) -> None:
        """Every field of every object type: given a resolver, defaulted, or unknown."""
        given = {(e.type_name, e.key) for e in found.entries}
        for type_name, kind in sorted(schema.kinds.items()):
            if kind != "object":
                continue
            for coordinate in schema.fields(type_name):
                _, _, field_name = coordinate.partition(".")
                for symbol in schema.coordinates[coordinate]:
                    key = (symbol.file_id, coordinate)
                    implementations, unknown, first, anchor, anchor_origin = self._fields.get(
                        key, (set(), False, symbol, claim, origin)
                    )
                    if (type_name, field_name) in given:
                        implementations = implementations | {
                            e.implementation
                            for e in found.entries
                            if (e.type_name, e.key) == (type_name, field_name)
                        }
                    unknown = unknown or found.unknown_all or type_name in found.unknown
                    self._fields[key] = (implementations, unknown, first, anchor, anchor_origin)

    # -- records -------------------------------------------------------------------

    def _edge(
        self,
        entry: _Entry,
        target: SymbolRow,
        ref: str,
        claim: UnresolvedRefRecord,
        origin: FileRow,
    ) -> None:
        key = (entry.implementation, target.entity_id, entry.file.file_id, entry.line)
        if key in self.edges:
            return
        if target.repo_id != entry.file.repo_id:
            self.crossings += 1
        self.edges[key] = EdgeRecord(
            src_id=entry.implementation,
            dst_id=target.entity_id,
            type=EdgeType.REFERENCES,
            repo_id=entry.file.repo_id,
            file_id=entry.file.file_id,
            line_start=entry.line or None,
            line_end=entry.line or None,
            parser=self._producer,
            resolution=json.dumps(
                {
                    FAMILY: {
                        "construction": f"{origin.repo_slug}/{origin.rel_path}:{claim.line_start}",
                        "decision_ref": ref,
                    }
                },
                sort_keys=True,
            ),
        )

    def _decide(
        self,
        claim: UnresolvedRefRecord,
        origin: FileRow,
        subject: str,
        decision: str,
        evidence: dict[str, object],
    ) -> None:
        ref = _ref(subject)
        self.decisions[ref] = LinkDecisionRow(
            decision_ref=ref,
            family=FAMILY,
            repo_id=origin.repo_id,
            file_id=origin.file_id,
            src_id=claim.src_id,
            line_start=claim.line_start,
            line_end=claim.line_end,
            subject=subject,
            decision=decision,
            evidence_json=json.dumps(evidence, sort_keys=True, separators=(",", ":")),
        )


def _ref(subject: str) -> str:
    return hashlib.sha256(f"{FAMILY}\0{subject}".encode()).hexdigest()[:24]


def _meta(claim: UnresolvedRefRecord) -> dict[str, object]:
    return json.loads(claim.meta_json or "{}")


def _decode_list(value: object) -> list[object]:
    decoded = json.loads(value) if isinstance(value, str) else value
    return list(decoded) if isinstance(decoded, list) else []


def _decode_one(value: object) -> dict[str, object]:
    decoded = json.loads(value) if isinstance(value, str) else value
    return decoded if isinstance(decoded, dict) else {}
