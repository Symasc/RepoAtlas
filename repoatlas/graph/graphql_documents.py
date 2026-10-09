"""Which schema coordinates each GraphQL document selects. A pure planner: it writes nothing.

A document names no schema, so the schema it is bound to has to come from configuration:
a GraphQL Code Generator output whose ``documents:`` inputs cover the document's file, and
whose own schema inputs resolved to admitted files (:class:`CodegenProject`). Every such
output's schema is the document's; one covering no output is ``schema-unknown``, whatever
endpoint or name it shares with a schema somewhere.

A bound document is walked from its root type -- the operation's root in that schema, or
the fragment's type condition -- and every selection that names a coordinate becomes a
``REFERENCES`` edge from the document to that coordinate's definitions, at the line it is
selected on: fields, their arguments, the enum literals and input-object fields passed, the
types of variables and their defaults, inline fragments' type conditions, and the fragments
spread. A spread names a fragment brought in by a static hole when there is one
(:mod:`repoatlas.parsers.ts_graphql`), and otherwise the one fragment of that name among
the documents bound to the same configuration -- the GraphQL language makes fragment names
global within a document set. A selection the schema does not have is reported, never
bound to a near match.

Decisions (``link_decisions``, family ``graphql_documents``), one per document, subject
``<repository>:<path>::<name>``: ``linked``, ``document-unresolved`` (some selection named
nothing), ``schema-unknown``, ``document-dynamic`` (named by the declaration it is in),
``document-invalid`` (embedded text that does not parse), ``document-bound`` (recorded past
the parser's bound, so not walked).
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from repoatlas.graph.globs import match_admitted, resolve_file
from repoatlas.graph.graphql_codegen import CodegenProject
from repoatlas.graph.graphql_schema import BUILTIN_SCALARS, Schema, build_schema
from repoatlas.graph.resolution import ResolutionIndex
from repoatlas.models.parsing import EdgeType
from repoatlas.storage.protocols import (
    EdgeRecord,
    FileRow,
    LinkDecisionRow,
    SymbolRow,
    UnresolvedRefRecord,
)

FAMILY = "graphql_documents"

LINKED = "linked"
DOCUMENT_UNRESOLVED = "document-unresolved"
SCHEMA_UNKNOWN = "schema-unknown"
DOCUMENT_DYNAMIC = "document-dynamic"
DOCUMENT_INVALID = "document-invalid"
DOCUMENT_BOUND = "document-bound"

_UNREAD = frozenset({"url", "computed"})


@dataclass(frozen=True, slots=True)
class DocumentsPlan:
    edges: tuple[EdgeRecord, ...] = ()
    decisions: tuple[LinkDecisionRow, ...] = ()
    resolved: int = 0
    unresolved: int = 0
    crossings: int = 0
    detail: str = ""


def plan_documents(
    claims: Sequence[UnresolvedRefRecord],
    projects: Sequence[CodegenProject],
    index: ResolutionIndex,
    files: Mapping[int, FileRow],
    by_file: Mapping[int, Sequence[SymbolRow]],
    *,
    producer: str,
) -> DocumentsPlan:
    """Bind every document claim to the schema its configuration gives it. Writes nothing."""
    planner = _Planner(projects, index, files, by_file, producer)
    ordered = sorted(
        (c for c in claims if c.file_id in files),
        key=lambda c: (files[c.file_id].repo_slug, files[c.file_id].rel_path, c.line_start),
    )
    planner.register(ordered)
    resolved = unresolved = 0
    for claim in ordered:
        if planner.decide(claim):
            resolved += 1
        else:
            unresolved += 1
    decisions = sorted(planner.decisions.values(), key=lambda row: row.subject)
    counts: dict[str, int] = defaultdict(int)
    for row in decisions:
        counts[row.decision] += 1
    detail = ", ".join(f"{count} {decision}" for decision, count in sorted(counts.items()))
    return DocumentsPlan(
        edges=tuple(
            sorted(planner.edges.values(), key=lambda e: (e.src_id, e.dst_id, e.line_start or 0))
        ),
        decisions=tuple(decisions),
        resolved=resolved,
        unresolved=unresolved + len(claims) - len(ordered),
        crossings=planner.crossings,
        detail=f"documents: {detail or 'none'}",
    )


@dataclass(slots=True)
class _Walk:
    claim: UnresolvedRefRecord
    origin: FileRow
    schema: Schema
    project: CodegenProject
    fragments: dict[str, list[UnresolvedRefRecord]]
    ref: str
    missing: list[str] = field(default_factory=list)


class _Planner:
    def __init__(
        self,
        projects: Sequence[CodegenProject],
        index: ResolutionIndex,
        files: Mapping[int, FileRow],
        by_file: Mapping[int, Sequence[SymbolRow]],
        producer: str,
    ) -> None:
        self._index = index
        self._files = files
        self._by_file = by_file
        self._producer = producer
        self._projects = list(projects)
        self._covering: dict[int, list[int]] = defaultdict(list)
        for number, project in enumerate(self._projects):
            for file_id in sorted(self._documents(project)):
                self._covering[file_id].append(number)
        self._schemas: dict[tuple[int, ...], Schema] = {}
        #: Per project, fragment name -> the fragment claims of that name it covers.
        self._fragments: dict[int, dict[str, list[UnresolvedRefRecord]]] = defaultdict(
            lambda: defaultdict(list)
        )
        #: Per file, owner declaration -> the fragment claims written in it.
        self._owned: dict[tuple[int, str], list[UnresolvedRefRecord]] = defaultdict(list)
        self.decisions: dict[str, LinkDecisionRow] = {}
        self.edges: dict[tuple[int, int, int], EdgeRecord] = {}
        self.crossings = 0

    def _documents(self, project: CodegenProject) -> set[int]:
        included: set[int] = set()
        excluded: set[int] = set()
        for item in project.documents:
            kind, value = item.get("kind"), str(item.get("value") or "")
            if kind in _UNREAD or not value:
                continue
            if kind == "glob":
                matched = match_admitted(self._index, project.config, value) or []
                (excluded if item.get("negated") else included).update(r.file_id for r in matched)
            else:
                row = resolve_file(self._index, project.config, value)
                if row is not None:
                    included.add(row.file_id)
        return included - excluded

    def register(self, claims: Sequence[UnresolvedRefRecord]) -> None:
        for claim in claims:
            meta = _meta(claim)
            if meta.get("document") != "fragment":
                continue
            name = str(meta.get("name") or "")
            for number in self._covering.get(claim.file_id, ()):
                self._fragments[number][name].append(claim)
            owner = meta.get("owner")
            if owner:
                self._owned[(claim.file_id, str(owner))].append(claim)

    # -- one document --------------------------------------------------------------

    def decide(self, claim: UnresolvedRefRecord) -> bool:
        meta = _meta(claim)
        origin = self._files[claim.file_id]
        kind = meta.get("document")
        name = str(meta.get("name") or meta.get("owner") or f"document@{claim.line_start}")
        subject = f"{origin.repo_slug}:{origin.rel_path}::{name}"
        if kind == "dynamic":
            self._decide(claim, origin, subject, DOCUMENT_DYNAMIC, {})
            return False
        if kind == "unparsed":
            self._decide(claim, origin, subject, DOCUMENT_INVALID, {})
            return False
        numbers = self._covering.get(claim.file_id, [])
        if not numbers:
            self._decide(claim, origin, subject, SCHEMA_UNKNOWN, {})
            return False
        if meta.get("bound") == "exceeded":
            self._decide(claim, origin, subject, DOCUMENT_BOUND, {})
            return False
        projects = [self._projects[n] for n in numbers]
        schema_files: dict[int, FileRow] = {}
        for project in projects:
            for row in project.schema:
                schema_files.setdefault(row.file_id, row)
        key = tuple(sorted(schema_files))
        schema = self._schemas.get(key)
        if schema is None:
            schema = build_schema(list(schema_files.values()), self._by_file)
            self._schemas[key] = schema
        fragments: dict[str, list[UnresolvedRefRecord]] = defaultdict(list)
        for number in numbers:
            for fragment, found in self._fragments[number].items():
                for item in found:
                    if item not in fragments[fragment]:
                        fragments[fragment].append(item)
        holes = self._holes(claim, origin, meta)
        for fragment, found in holes.items():
            fragments[fragment] = found
        ref = _ref(subject)
        walk = _Walk(claim, origin, schema, projects[0], fragments, ref)
        if kind == "fragment":
            self._type(walk, claim.target_key, claim.line_start or 1)
            self._selections(walk, _decode(meta.get("selections")), claim.target_key)
        else:
            operation = str(meta.get("operation") or "query")
            root = schema.roots.get(operation)
            if root is None or root not in schema.types:
                walk.missing.append(operation)
            else:
                for variable in _decode(meta.get("variables")):
                    named = str(variable.get("named_type") or "")
                    line = _line(variable.get("line"), claim.line_start or 1)
                    self._type(walk, named, line)
                    if "default" in variable:
                        self._value(walk, variable["default"], named, line)
                self._selections(walk, _decode(meta.get("selections")), root)
        evidence = {
            "config": projects[0].at,
            "schema": [self._at(row) for row in schema_files.values()],
        }
        if walk.missing:
            evidence["unresolved"] = sorted(set(walk.missing))
            self._decide(claim, origin, subject, DOCUMENT_UNRESOLVED, evidence)
        else:
            self._decide(claim, origin, subject, LINKED, evidence)
        return not walk.missing

    def _holes(
        self, claim: UnresolvedRefRecord, origin: FileRow, meta: Mapping[str, object]
    ) -> dict[str, list[UnresolvedRefRecord]]:
        """The fragments a static hole splices in, by name."""
        found: dict[str, list[UnresolvedRefRecord]] = defaultdict(list)
        for ref in _decode(meta.get("holes")):
            target: FileRow | None = None
            owner = ""
            if "local" in ref:
                target, owner = origin, str(ref["local"])
            elif "module" in ref:
                module = str(ref["module"])
                target = (
                    self._index.resolve_module(origin, module, "node")
                    if module.startswith(".")
                    else None
                )
                owner = str(ref.get("name") or "")
            if target is None or not owner:
                continue
            for fragment in self._owned.get((target.file_id, owner), ()):
                found[str(_meta(fragment).get("name") or "")].append(fragment)
        return found

    # -- the walk ------------------------------------------------------------------

    def _selections(
        self, walk: _Walk, items: list[dict[str, object]], type_name: str | None
    ) -> None:
        for item in items:
            line = int(item.get("line") or walk.claim.line_start)  # type: ignore[arg-type]
            if "field" in item:
                field_name = str(item["field"])
                if field_name.startswith("__"):
                    continue
                coordinate = f"{type_name}.{field_name}"
                if not self._reference(walk, coordinate, line):
                    continue
                for argument in item.get("arguments", []) or []:  # type: ignore[union-attr]
                    argument_line = int(argument.get("line") or line)
                    argument_coordinate = f"{coordinate}({argument.get('name')}:)"
                    if self._reference(walk, argument_coordinate, argument_line):
                        self._value(
                            walk,
                            argument.get("value"),
                            walk.schema.named.get(argument_coordinate),
                            argument_line,
                        )
                children = item.get("selections")
                if children:
                    self._selections(walk, children, walk.schema.named.get(coordinate))  # type: ignore[arg-type]
            elif "spread" in item:
                self._spread(walk, str(item["spread"]), line)
            elif "on" in item:
                condition = item.get("on")
                if condition:
                    self._type(walk, str(condition), line)
                self._selections(
                    walk,
                    _decode(item.get("selections")),
                    str(condition) if condition else type_name,
                )

    def _value(self, walk: _Walk, value: object, named: str | None, line: int) -> None:
        if not isinstance(value, dict) or named is None:
            return
        if "enum" in value:
            self._reference(walk, f"{named}.{value['enum']}", line)
        elif "object" in value:
            for member in value["object"] or []:
                coordinate = f"{named}.{member.get('name')}"
                if self._reference(walk, coordinate, line):
                    self._value(walk, member.get("value"), walk.schema.named.get(coordinate), line)
        elif "list" in value:
            for item in value["list"] or []:
                self._value(walk, item, named, line)

    def _spread(self, walk: _Walk, name: str, line: int) -> None:
        found = walk.fragments.get(name, [])
        if len(found) != 1:
            walk.missing.append(f"...{name}")
            return
        self._edge(walk, found[0].src_id, found[0].file_id, line)

    def _type(self, walk: _Walk, name: str, line: int) -> None:
        if not name or name in BUILTIN_SCALARS:
            return
        definitions = [
            s for s in walk.schema.types.get(name, []) if s.meta.get("extension") != "true"
        ]
        if not definitions:
            walk.missing.append(name)
            return
        for symbol in definitions:
            self._edge(walk, symbol.entity_id, symbol.file_id, line)

    def _reference(self, walk: _Walk, coordinate: str, line: int) -> bool:
        symbols = walk.schema.coordinates.get(coordinate)
        if not symbols:
            walk.missing.append(coordinate)
            return False
        for symbol in symbols:
            self._edge(walk, symbol.entity_id, symbol.file_id, line)
        return True

    # -- records -------------------------------------------------------------------

    def _edge(self, walk: _Walk, target: int, target_file: int, line: int) -> None:
        claim = walk.claim
        key = (claim.src_id, target, line)
        if key in self.edges or claim.src_id == target:
            return
        target_row = self._files.get(target_file)
        if target_row is not None and target_row.repo_id != walk.origin.repo_id:
            self.crossings += 1
        self.edges[key] = EdgeRecord(
            src_id=claim.src_id,
            dst_id=target,
            type=EdgeType.REFERENCES,
            repo_id=walk.origin.repo_id,
            file_id=walk.origin.file_id,
            line_start=line,
            line_end=line,
            parser=self._producer,
            resolution=json.dumps(
                {FAMILY: {"config": walk.project.at, "decision_ref": walk.ref}}, sort_keys=True
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

    def _at(self, row: FileRow) -> str:
        return f"{row.repo_slug}/{row.rel_path}"


def _ref(subject: str) -> str:
    return hashlib.sha256(f"{FAMILY}\0{subject}".encode()).hexdigest()[:24]


def _meta(claim: UnresolvedRefRecord) -> dict[str, object]:
    return json.loads(claim.meta_json or "{}")


def _line(value: object, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _decode(value: object) -> list[dict[str, object]]:
    decoded = json.loads(value) if isinstance(value, str) else value
    return [item for item in decoded if isinstance(item, dict)] if isinstance(decoded, list) else []
