"""Which generated declarations a GraphQL Code Generator configuration generates from which
schema definitions. A pure planner: it reads claims and index rows, and writes nothing.

The evidence is the configuration, never a name: an output is linked to the schema files
*its* configuration loads, and a definition elsewhere with the same name -- an unloaded
duplicate, another service's schema, a same-named enum in hand-written code -- is never a
candidate. Each output a configuration declares (:mod:`repoatlas.parsers.graphql_codegen`)
is decided in order, and the first that applies is the decision:

1. ``config-unsupported``: the parser could not read the output statically;
2. ``schema-remote``: an input is a URL, so what the schema contains is not in the source;
3. ``schema-unsupported``: an input is code, an introspection result, a computed path, a
   glob that also matches non-schema files, or syntax this pass does not translate;
4. ``schema-missing``: a path or glob matches no admitted schema file;
5. ``output-unsupported``: a preset, a directory, or a file the ``typescript`` plugin
   does not write;
6. ``output-missing``: the output is not an admitted file;
7. ``plugin-unsupported``: no ``typescript`` plugin declares the schema's types;
8. ``naming-unsupported``: options rename what is generated, or ``enumValues`` names a
   module the enums are imported from.

An output that passes is decided type by type. A named type defined in the loaded files
(the definitions GraphQL Code Generator merges, and their extensions) is ``linked`` to the
one top-level declaration of the generated file its naming produces, and every definition
and extension of it ``GENERATES`` that declaration. Otherwise it is ``schema-conflict``
(defined with different kinds), ``naming-unsupported`` (a name the default convention
would change, or an enum ``enumValues`` imports), ``generated-missing`` or
``generated-ambiguous``. A linked enum's values are decided one by one, by **value**: the
generated member whose string is what the generator writes for the value -- its explicit
``enumValues`` mapping when the configuration states one, otherwise the value's name, the
wire value -- is linked, and a value with none is ``member-stale``. A generated declaration
that is not an ``enum`` declares no members (``member-undeclared``).

Every decision is a ``link_decisions`` row (family ``graphql_codegen``) whose subject is
``<repository>:<config>-><output>[::<coordinate>]``, anchored at the output's entry in the
configuration. Every edge records what it rests on under ``resolution.graphql_codegen``.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from repoatlas.graph.globs import match_admitted, resolve_file, tree_files
from repoatlas.graph.resolution import ResolutionIndex
from repoatlas.models.parsing import ENUM_MEMBER, EdgeType, EntityType
from repoatlas.storage.protocols import (
    EdgeRecord,
    FileRow,
    LinkDecisionRow,
    SymbolRow,
    UnresolvedRefRecord,
)

FAMILY = "graphql_codegen"
#: The ``generator`` of a claim read from ``[tool.ariadne-codegen]``, and of one read from a
#: graphql-config project (repoatlas.parsers.graphql_codegen).
ARIADNE_CODEGEN = "ariadne-codegen"
GRAPHQL_CONFIG = "graphql-config"

LINKED = "linked"
CONFIG_UNSUPPORTED = "config-unsupported"
SCHEMA_REMOTE = "schema-remote"
SCHEMA_UNSUPPORTED = "schema-unsupported"
SCHEMA_MISSING = "schema-missing"
OUTPUT_UNSUPPORTED = "output-unsupported"
OUTPUT_MISSING = "output-missing"
PLUGIN_UNSUPPORTED = "plugin-unsupported"
NAMING_UNSUPPORTED = "naming-unsupported"
SCHEMA_CONFLICT = "schema-conflict"
GENERATED_MISSING = "generated-missing"
GENERATED_AMBIGUOUS = "generated-ambiguous"
MEMBER_STALE = "member-stale"
MEMBER_UNDECLARED = "member-undeclared"

#: The plugin that declares a schema's types, and the files it writes.
_PLUGIN = "typescript"
_PLUGIN_PACKAGE = "@graphql-codegen/"
_OUTPUT_SUFFIXES = (".ts", ".tsx", ".mts", ".cts")
_SDL_SUFFIXES = (".graphql", ".gql", ".graphqls")
#: The named types the ``typescript`` plugin declares; a scalar is an entry in ``Scalars``.
_DECLARED_ROLES = frozenset({"enum", "object", "interface", "union", "input"})
#: The default convention is ``change-case-all#pascalCase``, underscores kept. A name of
#: this shape is one it leaves as it is; any other is one it may change.
_PASCAL_FIXED_POINT = re.compile(r"[A-Z][a-z0-9]*(?:[A-Z][a-z0-9]+)*")
_DEFAULT_CONVENTION = "change-case-all#pascalCase"
_UNREAD_KINDS = frozenset({"code", "introspection", "computed", "other"})


@dataclass(frozen=True, slots=True)
class CodegenProject:
    """One configured output whose schema resolved to admitted files: what its
    documents are validated against (repoatlas.graph.graphql_documents)."""

    config: FileRow
    #: Where the output's entry is, as ``<repository>/<path>:<line>``.
    at: str
    #: The documents inputs, root first, as the configuration reader recorded them.
    documents: tuple[dict[str, object], ...]
    schema: tuple[FileRow, ...]
    #: The file the output writes, when it is admitted, and what writes it: the operation
    #: types and document constants generated for each document are found there.
    output: FileRow | None = None
    plugins: tuple[str, ...] = ()
    #: ``pascal``, ``keep``, or ``None`` when the output's naming is not modelled.
    naming: str | None = None
    #: ``<repository>:<config>-><output>``: the subject prefix of its decisions.
    subject: str = ""


@dataclass(frozen=True, slots=True)
class CodegenPlan:
    edges: tuple[EdgeRecord, ...] = ()
    decisions: tuple[LinkDecisionRow, ...] = ()
    resolved: int = 0
    unresolved: int = 0
    crossings: int = 0
    detail: str = ""
    projects: tuple[CodegenProject, ...] = ()


def plan_codegen(
    claims: Sequence[UnresolvedRefRecord],
    index: ResolutionIndex,
    files: Mapping[int, FileRow],
    symbols: Sequence[SymbolRow],
    *,
    producer: str,
) -> CodegenPlan:
    """Decide every configured output at once. Writes nothing."""
    by_file: dict[int, list[SymbolRow]] = defaultdict(list)
    for symbol in symbols:
        by_file[symbol.file_id].append(symbol)
    for listed in by_file.values():
        # Source order, never row order: the same files decide the same way after any
        # rebuild, and every list of evidence reads top to bottom.
        listed.sort(key=lambda s: (s.line_start or 0, s.qualified_name or "", s.name, str(s.type)))
    planner = _Planner(index, files, by_file, producer)
    ordered = sorted(
        (claim for claim in claims if claim.file_id in files),
        key=lambda c: (files[c.file_id].repo_slug, files[c.file_id].rel_path, c.line_start),
    )
    resolved = unresolved = 0
    for claim in ordered:
        if planner.decide(claim, files[claim.file_id]):
            resolved += 1
        else:
            unresolved += 1
    unresolved += len(claims) - len(ordered)
    decisions = sorted(planner.decisions.values(), key=lambda row: row.subject)
    edges = sorted(planner.edges, key=lambda e: (e.src_id, e.dst_id))
    counts = defaultdict(int)
    for row in decisions:
        counts[row.decision] += 1
    detail = ", ".join(f"{count} {decision}" for decision, count in sorted(counts.items()))
    return CodegenPlan(
        edges=tuple(edges),
        decisions=tuple(decisions),
        resolved=resolved,
        unresolved=unresolved,
        crossings=planner.crossings,
        detail=f"codegen: {detail or 'nothing configured'}",
        projects=tuple(planner.projects),
    )


@dataclass(slots=True)
class _Output:
    claim: UnresolvedRefRecord
    origin: FileRow
    meta: dict[str, object]
    subject: str
    evidence: dict[str, object] = field(default_factory=dict)


class _Planner:
    def __init__(
        self,
        index: ResolutionIndex,
        files: Mapping[int, FileRow],
        by_file: Mapping[int, list[SymbolRow]],
        producer: str,
    ) -> None:
        self._index = index
        self._files = files
        self._by_file = by_file
        self._producer = producer
        self._slugs = index.repo_slugs
        self.decisions: dict[str, LinkDecisionRow] = {}
        self.edges: list[EdgeRecord] = []
        self.crossings = 0
        self.projects: list[CodegenProject] = []

    # -- one output ----------------------------------------------------------------

    def decide(self, claim: UnresolvedRefRecord, origin: FileRow) -> bool:
        meta: dict[str, object] = json.loads(claim.meta_json or "{}")
        written = str(meta.get("output") or claim.target_key)
        output = _subject_path(origin, written)
        here = _place(origin.repo_slug, origin.rel_path, claim.line_start)
        current = _Output(
            claim,
            origin,
            meta,
            f"{origin.repo_slug}:{origin.rel_path}->{output}",
            {"config": here, "output": written},
        )
        if meta.get("generator") == GRAPHQL_CONFIG:
            current.subject = f"{origin.repo_slug}:{origin.rel_path}::{meta.get('project')}"
        refused = meta.get("refused")
        if refused:
            return self._refuse(current, CONFIG_UNSUPPORTED, reason=refused)

        inputs = _list(meta.get("schema"))
        remote = [str(i.get("value")) for i in inputs if i.get("kind") == "url"]
        if remote:
            return self._refuse(current, SCHEMA_REMOTE, remote=remote)
        schema, unread, missing = self._schema(origin, inputs)
        if unread:
            return self._refuse(current, SCHEMA_UNSUPPORTED, unsupported=unread)
        if missing or not schema:
            return self._refuse(current, SCHEMA_MISSING, missing=missing)
        typescript_output = written.endswith(_OUTPUT_SUFFIXES) and meta.get("preset") is None
        self.projects.append(
            CodegenProject(
                config=origin,
                at=here,
                documents=tuple(_list(meta.get("documents"))),
                schema=tuple(schema),
                output=resolve_file(self._index, origin, written) if typescript_output else None,
                plugins=tuple(
                    str(name).removeprefix(_PLUGIN_PACKAGE)
                    for name in _list_of_str(meta.get("plugins"))
                ),
                naming=_naming(_mapping(meta.get("options"))),
                subject=current.subject,
            )
        )

        if meta.get("generator") == GRAPHQL_CONFIG:
            # A graphql-config project generates nothing: it says which schema its documents
            # are written against.
            self._decide(current, current.subject, LINKED, schema=[self._file(r) for r in schema])
            return True
        if meta.get("generator") == ARIADNE_CODEGEN:
            # ariadne-codegen writes the schema's enums, under their GraphQL names, into one
            # module; its models and client are other modules this pass does not bind.
            generated = resolve_file(self._index, origin, written)
            if generated is None:
                return self._refuse(current, OUTPUT_MISSING)
            current.evidence["schema_files"] = [
                f"{self._slugs.get(row.repo_id)}/{row.rel_path}" for row in schema
            ]
            return self._types(current, schema, generated, keep=True, enums_only=True)
        if meta.get("preset") is not None:
            return self._refuse(current, OUTPUT_UNSUPPORTED, reason="preset")
        if written.endswith("/"):
            return self._refuse(current, OUTPUT_UNSUPPORTED, reason="directory")
        if not written.endswith(_OUTPUT_SUFFIXES):
            return self._refuse(current, OUTPUT_UNSUPPORTED, reason="not typescript")
        generated = resolve_file(self._index, origin, written)
        if generated is None:
            return self._refuse(current, OUTPUT_MISSING)
        plugins = [
            str(name).removeprefix(_PLUGIN_PACKAGE) for name in _list_of_str(meta.get("plugins"))
        ]
        if _PLUGIN not in plugins:
            return self._refuse(current, PLUGIN_UNSUPPORTED, plugins=plugins)
        options = _mapping(meta.get("options"))
        naming = _naming(options)
        mapped = _enum_values(options.get("enumValues"))
        if naming is None or mapped is None:
            return self._refuse(current, NAMING_UNSUPPORTED, options=options)

        current.evidence["schema_files"] = [
            f"{self._slugs.get(row.repo_id)}/{row.rel_path}" for row in schema
        ]
        return self._types(current, schema, generated, keep=naming == "keep", mapped=mapped)

    def _schema(
        self, origin: FileRow, inputs: list[dict[str, object]]
    ) -> tuple[list[FileRow], list[dict[str, object]], list[str]]:
        """The admitted schema files the inputs load, what could not be read, and what
        named nothing."""
        loaded: dict[int, FileRow] = {}
        unread: list[dict[str, object]] = []
        missing: list[str] = []
        excluded: set[int] = set()
        for item in inputs:
            kind, value = item.get("kind"), str(item.get("value") or "")
            if kind in _UNREAD_KINDS or not value:
                unread.append({"kind": kind, "value": item.get("value"), "line": item.get("line")})
                continue
            if kind == "path":
                row = resolve_file(self._index, origin, value)
                if row is None:
                    missing.append(value)
                else:
                    loaded.setdefault(row.file_id, row)
                continue
            if kind == "tree":
                rows = tree_files(self._index, origin, value, _SDL_SUFFIXES)
                if not rows:
                    missing.append(value)
                for row in rows:
                    loaded.setdefault(row.file_id, row)
                continue
            matched = match_admitted(self._index, origin, value)
            if matched is None:
                unread.append({"kind": "glob-syntax", "value": value, "line": item.get("line")})
                continue
            if item.get("negated"):
                excluded.update(row.file_id for row in matched)
                continue
            if not matched:
                missing.append(value)
                continue
            other = [row.rel_path for row in matched if not row.rel_path.endswith(_SDL_SUFFIXES)]
            if other:
                unread.append({"kind": "glob-code", "value": value, "matches": other[:8]})
                continue
            for row in matched:
                loaded.setdefault(row.file_id, row)
        schema = [row for file_id, row in loaded.items() if file_id not in excluded]
        return schema, unread, missing

    # -- types and members ---------------------------------------------------------

    def _types(
        self,
        current: _Output,
        schema: list[FileRow],
        generated: FileRow,
        *,
        keep: bool,
        enums_only: bool = False,
        mapped: Mapping[str, Mapping[str, str] | None] | None = None,
    ) -> bool:
        mapped = mapped or {}
        definitions: dict[str, list[tuple[SymbolRow, str]]] = defaultdict(list)
        extensions: dict[str, list[SymbolRow]] = defaultdict(list)
        values: dict[str, dict[str, list[SymbolRow]]] = defaultdict(lambda: defaultdict(list))
        for row in schema:
            for symbol in self._by_file.get(row.file_id, ()):
                meta = symbol.meta
                role = meta.get("graphql")
                if role == "enum-value" and symbol.qualified_name:
                    enum, _, value = symbol.qualified_name.rpartition(".")
                    values[enum][value].append(symbol)
                elif symbol.type is EntityType.CLASS and role in _DECLARED_ROLES:
                    if meta.get("extension") == "true":
                        extensions[symbol.name].append(symbol)
                    else:
                        definitions[symbol.name].append((symbol, str(role)))
        declared = [
            s
            for s in self._by_file.get(generated.file_id, ())
            if s.qualified_name == s.name and s.type not in {EntityType.MODULE, EntityType.FUNCTION}
        ]
        by_name: dict[str, list[SymbolRow]] = defaultdict(list)
        for symbol in declared:
            by_name[symbol.name].append(symbol)

        linked = False
        for name in sorted(definitions):
            if enums_only and {role for _, role in definitions[name]} != {"enum"}:
                continue
            sources = [symbol for symbol, _ in definitions[name]] + extensions.get(name, [])
            at = [self._at(symbol) for symbol in sources]
            subject = f"{current.subject}::{name}"
            kinds = sorted({role for _, role in definitions[name]})
            if len(kinds) > 1:
                self._decide(current, subject, SCHEMA_CONFLICT, schema=at, kinds=kinds)
                continue
            if not keep and not _PASCAL_FIXED_POINT.fullmatch(name):
                self._decide(current, subject, NAMING_UNSUPPORTED, schema=at)
                continue
            if name in mapped and mapped[name] is None:
                # Imported from where enumValues says, not declared by this output.
                self._decide(current, subject, NAMING_UNSUPPORTED, schema=at, reason="enumValues")
                continue
            candidates = by_name.get(name, [])
            if len(candidates) != 1:
                decision = GENERATED_MISSING if not candidates else GENERATED_AMBIGUOUS
                self._decide(
                    current,
                    subject,
                    decision,
                    schema=at,
                    generated=[self._at(c) for c in candidates],
                )
                continue
            target = candidates[0]
            ref = self._decide(current, subject, LINKED, schema=at, generated=self._at(target))
            for source in sources:
                self._edge(current, source, target, generated, ref)
            linked = True
            if kinds == ["enum"]:
                self._members(
                    current, name, values.get(name, {}), target, generated, mapped.get(name) or {}
                )
        return linked

    def _members(
        self,
        current: _Output,
        enum: str,
        values: Mapping[str, list[SymbolRow]],
        target: SymbolRow,
        generated: FileRow,
        mapping: Mapping[str, str],
    ) -> None:
        prefix = f"{target.qualified_name}."
        members = [
            s
            for s in self._by_file.get(generated.file_id, ())
            if s.qualified_name
            and s.qualified_name.startswith(prefix)
            and s.meta.get("kind") == ENUM_MEMBER
        ]
        # A TypeScript enum, or a class whose members state their values (a Python Enum).
        enumerated = target.meta.get("kind") == "enum" or bool(members)
        for value in sorted(values):
            sources = values[value]
            subject = f"{current.subject}::{enum}.{value}"
            at = [self._at(symbol) for symbol in sources]
            if not enumerated:
                self._decide(
                    current, subject, MEMBER_UNDECLARED, schema=at, generated=self._at(target)
                )
                continue
            written = mapping.get(value, value)
            stated = {"mapped": written} if value in mapping else {}
            matches = [m for m in members if m.meta.get("value") == written]
            if len(matches) != 1:
                decision = MEMBER_STALE if not matches else GENERATED_AMBIGUOUS
                self._decide(
                    current,
                    subject,
                    decision,
                    schema=at,
                    value=value,
                    generated=[self._at(m) for m in matches],
                    declared=sorted(str(m.meta.get("value")) for m in members),
                    **stated,
                )
                continue
            ref = self._decide(
                current,
                subject,
                LINKED,
                schema=at,
                value=value,
                generated=self._at(matches[0]),
                **stated,
            )
            for source in sources:
                self._edge(current, source, matches[0], generated, ref, value=written)

    # -- records -------------------------------------------------------------------

    def _refuse(self, current: _Output, decision: str, **evidence: object) -> bool:
        self._decide(current, current.subject, decision, **evidence)
        return False

    def _decide(self, current: _Output, subject: str, decision: str, **evidence: object) -> str:
        ref = hashlib.sha256(f"{FAMILY}\0{subject}".encode()).hexdigest()[:24]
        claim = current.claim
        self.decisions[ref] = LinkDecisionRow(
            decision_ref=ref,
            family=FAMILY,
            repo_id=current.origin.repo_id,
            file_id=current.origin.file_id,
            src_id=claim.src_id,
            line_start=claim.line_start,
            line_end=claim.line_end,
            subject=subject,
            decision=decision,
            evidence_json=json.dumps(
                {**current.evidence, **evidence}, sort_keys=True, separators=(",", ":")
            ),
        )
        return ref

    def _edge(
        self,
        current: _Output,
        source: SymbolRow,
        target: SymbolRow,
        generated: FileRow,
        ref: str,
        *,
        value: str | None = None,
    ) -> None:
        record: dict[str, object] = {
            "config": current.evidence["config"],
            "schema": self._at(source),
            "output": f"{self._slugs.get(generated.repo_id)}/{generated.rel_path}",
            "plugin": ARIADNE_CODEGEN
            if current.meta.get("generator") == ARIADNE_CODEGEN
            else _PLUGIN,
            "decision_ref": ref,
        }
        if value is not None:
            record["member_value"] = value
        if source.repo_id != generated.repo_id:
            self.crossings += 1
        self.edges.append(
            EdgeRecord(
                src_id=source.entity_id,
                dst_id=target.entity_id,
                type=EdgeType.GENERATES,
                repo_id=generated.repo_id,
                file_id=generated.file_id,
                line_start=target.line_start,
                line_end=target.line_end if target.line_end is not None else target.line_start,
                parser=self._producer,
                resolution=json.dumps({FAMILY: record}, sort_keys=True),
            )
        )

    def _file(self, row: FileRow) -> str:
        return f"{self._slugs.get(row.repo_id)}/{row.rel_path}"

    def _at(self, symbol: SymbolRow) -> str:
        row = self._files.get(symbol.file_id)
        return _place(
            self._slugs.get(symbol.repo_id, "?"), row.rel_path if row else "?", symbol.line_start
        )


def _naming(options: Mapping[str, object]) -> str | None:
    """``pascal`` (the default convention), ``keep``, or ``None`` for anything else."""
    if any(options.get(key) not in (None, "") for key in ("typesPrefix", "typesSuffix")):
        return None
    if options.get("numericEnums") == "true":
        return None
    if options.get("enumType") == "native-numeric":
        return None
    convention = options.get("namingConvention", _DEFAULT_CONVENTION)
    if isinstance(convention, dict):
        convention = convention.get("typeNames", _DEFAULT_CONVENTION)
    if convention == "keep":
        return "keep"
    return "pascal" if convention == _DEFAULT_CONVENTION else None


def _enum_values(option: object) -> dict[str, dict[str, str] | None] | None:
    """``enumValues`` as stated: enum -> (value -> what is written for it), or ``None`` for
    an enum imported from a module. ``None`` when every enum is imported (a module path)."""
    if option is None:
        return {}
    if not isinstance(option, dict):
        return None
    mapped: dict[str, dict[str, str] | None] = {}
    for enum, values in option.items():
        if isinstance(values, dict) and all(isinstance(v, str) for v in values.values()):
            mapped[str(enum)] = {str(k): str(v) for k, v in values.items()}
        else:
            mapped[str(enum)] = None
    return mapped


def _subject_path(origin: FileRow, written: str) -> str:
    base = posixpath.dirname(origin.rel_path)
    joined = (
        posixpath.normpath(posixpath.join(base, written)) if base else posixpath.normpath(written)
    )
    return joined + "/" if written.endswith("/") and not joined.endswith("/") else joined


def _place(slug: str | None, path: str, line: int | None) -> str:
    return f"{slug}/{path}:{line}" if line is not None else f"{slug}/{path}"


def _list(value: object) -> list[dict[str, object]]:
    decoded = json.loads(value) if isinstance(value, str) else value
    return [item for item in decoded if isinstance(item, dict)] if isinstance(decoded, list) else []


def _mapping(value: object) -> dict[str, object]:
    decoded = json.loads(value) if isinstance(value, str) else value
    return {str(k): v for k, v in decoded.items()} if isinstance(decoded, dict) else {}


def _list_of_str(value: object) -> list[str]:
    decoded = json.loads(value) if isinstance(value, str) else value
    return [str(item) for item in decoded] if isinstance(decoded, list) else []
