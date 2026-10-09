"""Queries bound to the database a source-backed identity says they run against.

A pure planner: it reads ``persistence-client`` and ``persistence-query`` claims
(:mod:`repoatlas.parsers.ts_persistence`, :mod:`repoatlas.parsers.py_persistence`,
:mod:`repoatlas.parsers.dashboards`) and Prisma projects (:mod:`repoatlas.graph.prisma_schema`),
and writes nothing.

A query is bound only through the whole chain the source states, in this order; the first
link that fails is the decision (``link_decisions``, family ``persistence``, subject
``<repository>:<path>::<declaration>``, or ``<repository>:<path>`` for a dashboard):

1. **its client**: the receiver is a client construction -- this module's, or the one a
   relative import names. A Knex-shaped chain whose receiver is a parameter, a local, or
   anything not a client is ``receiver-unproven``; SQL handed to something that is not a
   client is not a query, and is not decided;
2. **the client's database**: a Prisma client is its project's. Any other client is a
   project's when it reads environment variable ``X`` and exactly one datasource *in the same
   repository* reads ``env("X")``: one process, one environment. Anything else -- another
   repository, no variable, several projects, a dashboard -- is
   ``database-identity-unproven``. A name shared across repositories proves nothing;
3. **the dialect**: the project's provider must be PostgreSQL, else ``dialect-unsupported``;
4. **the text**: ``sql-dynamic`` when only run time decides it, ``sql-unsupported`` when
   sqlglot cannot read it;
5. **the columns**: each comparison's table and column, through ``@@map`` / ``@map``, is a
   model field, and each literal a value of the field's enum by its database name. The
   declaration making the query ``REFERENCES`` the field and the value (evidence
   ``persistence``). ``column-unknown`` when no comparison names a field; ``linked``
   otherwise.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from repoatlas.graph.prisma_schema import POSTGRES, PrismaProject
from repoatlas.graph.resolution import ResolutionIndex
from repoatlas.models.parsing import EdgeType
from repoatlas.parsers.ts_persistence import PERSISTENCE_CLIENT
from repoatlas.storage.protocols import (
    EdgeRecord,
    FileRow,
    LinkDecisionRow,
    SymbolRow,
    UnresolvedRefRecord,
)

FAMILY = "persistence"
EVIDENCE = "persistence"

LINKED = "linked"
RECEIVER_UNPROVEN = "receiver-unproven"
IDENTITY_UNPROVEN = "database-identity-unproven"
DIALECT_UNSUPPORTED = "dialect-unsupported"
SQL_DYNAMIC = "sql-dynamic"
SQL_UNSUPPORTED = "sql-unsupported"
COLUMN_UNKNOWN = "column-unknown"

#: Worst first: a declaration's queries are decided together, by the worst of them.
_ORDER = (
    RECEIVER_UNPROVEN,
    IDENTITY_UNPROVEN,
    DIALECT_UNSUPPORTED,
    SQL_DYNAMIC,
    SQL_UNSUPPORTED,
    COLUMN_UNKNOWN,
    LINKED,
)
_TS_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")


@dataclass(frozen=True, slots=True)
class PersistencePlan:
    edges: tuple[EdgeRecord, ...] = ()
    decisions: tuple[LinkDecisionRow, ...] = ()
    crossings: int = 0
    detail: str = ""


def plan_persistence(
    clients: Sequence[UnresolvedRefRecord],
    queries: Sequence[UnresolvedRefRecord],
    projects: Sequence[PrismaProject],
    index: ResolutionIndex,
    files: Mapping[int, FileRow],
    *,
    producer: str,
) -> PersistencePlan:
    """Bind every query to its database's fields and values. Writes nothing."""
    planner = _Planner(clients, projects, index, files, producer)
    outcomes: dict[str, list[tuple[str, dict[str, object], UnresolvedRefRecord]]] = defaultdict(
        list
    )
    for claim in sorted(queries, key=lambda c: (c.file_id, c.line_start or 0)):
        origin = files.get(claim.file_id)
        if origin is None:
            continue
        decided = planner.decide(claim, origin)
        if decided is None:
            continue
        meta = json.loads(claim.meta_json or "{}")
        owner = str(meta.get("owner") or "")
        subject = f"{origin.repo_slug}:{origin.rel_path}" + (f"::{owner}" if owner else "")
        outcomes[subject].append((*decided, claim))
    decisions: list[LinkDecisionRow] = []
    for subject, found in sorted(outcomes.items()):
        worst = min(found, key=lambda item: _ORDER.index(item[0]))
        claim = worst[2]
        evidence = {
            "queries": [{"line": c.line_start, "decision": d, **e} for d, e, c in found],
        }
        ref = _ref(subject)
        decisions.append(
            LinkDecisionRow(
                decision_ref=ref,
                family=FAMILY,
                repo_id=claim.repo_id,
                file_id=claim.file_id,
                src_id=claim.src_id,
                line_start=claim.line_start,
                line_end=claim.line_end,
                subject=subject,
                decision=worst[0],
                evidence_json=json.dumps(evidence, sort_keys=True, separators=(",", ":")),
            )
        )
        for decision, _, query in found:
            if decision == LINKED:
                planner.link(query, ref)
    counts: dict[str, int] = defaultdict(int)
    for row in decisions:
        counts[row.decision] += 1
    detail = ", ".join(f"{n} {d}" for d, n in sorted(counts.items()))
    return PersistencePlan(
        edges=tuple(
            sorted(planner.edges.values(), key=lambda e: (e.src_id, e.dst_id, e.line_start or 0))
        ),
        decisions=tuple(decisions),
        crossings=planner.crossings,
        detail=f"persistence: {detail or 'no queries'}",
    )


def _ref(subject: str) -> str:
    return hashlib.sha256(f"{FAMILY}\0{subject}".encode()).hexdigest()[:24]


@dataclass(frozen=True, slots=True)
class _Binding:
    project: PrismaProject
    #: (field, value or ``None``, the comparison it came from).
    targets: tuple[tuple[SymbolRow, SymbolRow | None, dict[str, object]], ...]


class _Planner:
    def __init__(
        self,
        clients: Sequence[UnresolvedRefRecord],
        projects: Sequence[PrismaProject],
        index: ResolutionIndex,
        files: Mapping[int, FileRow],
        producer: str,
    ) -> None:
        self._index = index
        self._files = files
        self._producer = producer
        self._projects = projects
        self._clients: dict[tuple[int, str], dict[str, str]] = {}
        for claim in clients:
            meta = json.loads(claim.meta_json or "{}")
            if meta.get("pending_fact") == PERSISTENCE_CLIENT:
                self._clients[(claim.file_id, claim.target_key)] = meta
        self._paths = {(row.repo_id, row.rel_path): row for row in files.values()}
        self._bindings: dict[int, _Binding] = {}
        self.edges: dict[tuple[int, int, int], EdgeRecord] = {}
        self.crossings = 0

    def decide(
        self, claim: UnresolvedRefRecord, origin: FileRow
    ) -> tuple[str, dict[str, object]] | None:
        meta = json.loads(claim.meta_json or "{}")
        kind, binding = meta.get("kind"), meta.get("binding")
        if binding == "dashboard":
            return IDENTITY_UNPROVEN, {"reason": "a dashboard names no database"}
        if binding == "local":
            return (
                (RECEIVER_UNPROVEN, {"receiver": meta.get("receiver")}) if kind == "knex" else None
            )
        client = self._client(meta, origin)
        if client is None:
            if kind == "knex":
                return RECEIVER_UNPROVEN, {"receiver": meta.get("receiver")}
            if meta.get("library") == "sqlalchemy":
                return IDENTITY_UNPROVEN, {"reason": "no client states its database"}
            return None
        project = self._identity(client, origin)
        if project is None:
            stated: dict[str, object] = {k: client[k] for k in ("library", "env") if client.get(k)}
            return IDENTITY_UNPROVEN, stated
        if project.provider != POSTGRES:
            return DIALECT_UNSUPPORTED, {"schema": project.at, "provider": project.provider}
        if meta.get("dynamic") == "true":
            return SQL_DYNAMIC, {"schema": project.at}
        if meta.get("unsupported") == "true":
            return SQL_UNSUPPORTED, {"schema": project.at}
        targets = self._bind(meta, project)
        if not targets:
            return COLUMN_UNKNOWN, {"schema": project.at}
        if claim.id is not None:
            self._bindings[claim.id] = _Binding(project, tuple(targets))
        return LINKED, {"schema": project.at, "bound": len(targets)}

    def _client(self, meta: Mapping[str, object], origin: FileRow) -> dict[str, str] | None:
        binding = meta.get("binding")
        if binding == "inline":
            return {k: str(v) for k, v in meta.items() if k in {"library", "env"}}
        if binding == "module":
            return self._clients.get((origin.file_id, str(meta.get("receiver"))))
        if binding == "import":
            module = str(meta.get("module") or "")
            if not module.startswith("."):
                return None
            target = self._module(origin, module)
            if target is None:
                return None
            return self._clients.get((target.file_id, str(meta.get("imported"))))
        return None

    def _module(self, origin: FileRow, specifier: str) -> FileRow | None:
        """The file a relative TypeScript import names: the path, a suffix, or an index."""
        base = posixpath.normpath(posixpath.join(posixpath.dirname(origin.rel_path), specifier))
        for candidate in (
            base,
            *(f"{base}{s}" for s in _TS_SUFFIXES),
            *(f"{base}/index{s}" for s in _TS_SUFFIXES),
        ):
            row = self._paths.get((origin.repo_id, candidate))
            if row is not None:
                return row
        return None

    def _identity(self, client: Mapping[str, str], origin: FileRow) -> PrismaProject | None:
        mine = [p for p in self._projects if p.schema.repo_id == origin.repo_id]
        if client.get("library") == "prisma":
            found = [p for p in mine if client.get("module") in p.specifiers]
        elif client.get("env"):
            found = [p for p in mine if p.url_env == client["env"]]
        else:
            return None
        return found[0] if len(found) == 1 else None

    def _bind(
        self, meta: Mapping[str, object], project: PrismaProject
    ) -> list[tuple[SymbolRow, SymbolRow | None, dict[str, object]]]:
        aliases: dict[str, str] = {}
        for table, alias in json.loads(str(meta.get("tables") or "[]")):
            aliases[alias] = _bare(table)
            aliases.setdefault(_bare(table), _bare(table))
        found: list[tuple[SymbolRow, SymbolRow | None, dict[str, object]]] = []
        for predicate in json.loads(str(meta.get("predicates") or "[]")):
            column, op, values = predicate[0], predicate[1], predicate[2]
            table = _bare(predicate[3]) if len(predicate) > 3 and predicate[3] else None
            qualifier, _, bare_column = str(column).rpartition(".")
            if table is None:
                if qualifier:
                    table = aliases.get(qualifier)
                elif len(set(aliases.values())) == 1:
                    table = next(iter(aliases.values()))
            if table is None:
                continue
            field = project.columns.get((table, bare_column))
            if field is None:
                continue
            enum = field.meta.get("type", "")
            members = project.values.get(enum, {})
            comparison = {"table": table, "column": bare_column, "op": op}
            if not members:
                found.append((field, None, comparison))
                continue
            for value in values:
                member = members.get(str(value))
                if member is not None:
                    found.append((field, member, {**comparison, "value": value}))
        return found

    def link(self, claim: UnresolvedRefRecord, ref: str) -> None:
        binding = self._bindings.get(claim.id) if claim.id is not None else None
        origin = self._files.get(claim.file_id)
        if binding is None or origin is None:
            return
        for field, member, comparison in binding.targets:
            for target in (field, member):
                if target is None:
                    continue
                key = (claim.src_id, target.entity_id, claim.line_start or 0)
                if key in self.edges:
                    continue
                if origin.repo_id != target.repo_id:
                    self.crossings += 1
                self.edges[key] = EdgeRecord(
                    src_id=claim.src_id,
                    dst_id=target.entity_id,
                    type=EdgeType.REFERENCES,
                    repo_id=origin.repo_id,
                    file_id=origin.file_id,
                    line_start=claim.line_start,
                    line_end=claim.line_end if claim.line_end is not None else claim.line_start,
                    parser=self._producer,
                    resolution=json.dumps(
                        {
                            EVIDENCE: {
                                "database": binding.project.at,
                                **comparison,
                                "decision_ref": ref,
                            }
                        },
                        sort_keys=True,
                    ),
                )


def _bare(name: str) -> str:
    return name.replace('"', "").removeprefix("public.")
