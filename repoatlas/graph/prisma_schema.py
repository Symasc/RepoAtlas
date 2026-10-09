"""Prisma projects: the database a schema describes, its migrations, and its generated client.

A pure planner: it reads indexed symbols (:mod:`repoatlas.parsers.prisma`,
:mod:`repoatlas.parsers.sql`) and ``USES`` claims, and writes nothing.

**A project** is a schema file with a ``datasource``. Its provider is the database's dialect;
the environment variable its ``url`` reads is what code must read to be talking to the same
database (:mod:`repoatlas.graph.persistence`). Its names, as the database spells them, come
from ``@@map`` / ``@map`` or the names themselves: table → model, (table, column) → field,
type → enum, value → enum value.

**Migrations.** Prisma Migrate writes ``migrations/<name>/migration.sql`` beside the schema
from the schema itself. Each ``CREATE TYPE`` / ``ALTER TYPE`` / ``DROP TYPE`` there of the
type an enum is mapped to is ``GENERATES`` from that enum (evidence ``prisma_migrate``).
Decisions (``link_decisions``, family ``prisma_migrations``):

* per migration file, when the project's provider is not PostgreSQL: ``dialect-unsupported``,
  or ``dialect-unknown`` when no provider is stated -- nothing in it is read as PostgreSQL;
* per enum of a project with migrations: ``linked`` when the values the migrations leave,
  folded in directory order, are the enum's database values; ``migration-drift`` listing the
  differences; ``migration-missing`` when no migration creates the type. Migrations are what
  the source says was applied, never proof of what a database holds.

**The generated client.** A ``prisma-client-js`` generator writes a module code imports:
``@prisma/client`` with no ``output``, or the package an ``output`` under ``node_modules``
names (``@prisma/lti-client``). A use of a name imported from that specifier, in the same
repository, ``REFERENCES`` the model or enum of that name (evidence ``prisma_client``): the
client exports exactly the schema's names. Decisions (family ``prisma_client``), per
repository and specifier: ``linked``, or ``client-ambiguous`` when several schemas there
generate it. An ``output`` into a source directory, imported by relative path, is not
followed.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from repoatlas.models.parsing import EdgeType, EntityType
from repoatlas.storage.protocols import (
    EdgeRecord,
    FileRow,
    LinkDecisionRow,
    SymbolRow,
    UnresolvedRefRecord,
)

MIGRATIONS = "prisma_migrations"
CLIENT = "prisma_client"
#: The resolution key of a migration edge, and of a client binding.
MIGRATE_EVIDENCE = "prisma_migrate"
CLIENT_EVIDENCE = "prisma_client"

LINKED = "linked"
DIALECT_UNSUPPORTED = "dialect-unsupported"
DIALECT_UNKNOWN = "dialect-unknown"
MIGRATION_DRIFT = "migration-drift"
MIGRATION_MISSING = "migration-missing"
CLIENT_AMBIGUOUS = "client-ambiguous"

POSTGRES = "postgresql"
_CLIENT_GENERATORS = frozenset({"prisma-client-js", "prisma-client"})
_DEFAULT_CLIENT = "@prisma/client"
_TYPE_STATEMENTS = frozenset({"enum-type", "alter-type", "drop-type"})


@dataclass(frozen=True, slots=True)
class PrismaProject:
    schema: FileRow
    provider: str | None
    url_env: str | None
    specifiers: tuple[str, ...]
    enums: Mapping[str, SymbolRow]
    models: Mapping[str, SymbolRow]
    #: Database type name -> enum; table -> model; (table, column) -> field.
    db_types: Mapping[str, SymbolRow]
    tables: Mapping[str, SymbolRow]
    columns: Mapping[tuple[str, str], SymbolRow]
    #: Enum name -> its values, by database name.
    values: Mapping[str, Mapping[str, SymbolRow]]
    migrations: tuple[FileRow, ...] = ()

    @property
    def at(self) -> str:
        return f"{self.schema.repo_slug}/{self.schema.rel_path}"


@dataclass(frozen=True, slots=True)
class PrismaPlan:
    edges: tuple[EdgeRecord, ...] = ()
    decisions: tuple[LinkDecisionRow, ...] = ()
    #: (consumer declaration entity, imported name) -> the schema symbol it binds to.
    bound: Mapping[tuple[int, str], SymbolRow] = field(default_factory=dict)
    crossings: int = 0
    detail: str = ""


def prisma_projects(
    files: Mapping[int, FileRow], by_file: Mapping[int, Sequence[SymbolRow]]
) -> list[PrismaProject]:
    """Every schema file with a ``datasource``, as a project. Reads only symbols."""
    found: list[PrismaProject] = []
    sql_files: dict[int, list[FileRow]] = defaultdict(list)
    for row in files.values():
        if row.rel_path.endswith("/migration.sql") or row.rel_path == "migration.sql":
            sql_files[row.repo_id].append(row)
    for file_id in sorted(by_file, key=lambda f: _place(files.get(f))):
        row = files.get(file_id)
        symbols = by_file[file_id]
        if row is None or not row.rel_path.endswith(".prisma"):
            continue
        datasource = next((s for s in symbols if s.meta.get("prisma") == "datasource"), None)
        if datasource is None:
            continue
        meta = datasource.meta
        enums = {s.name: s for s in symbols if s.meta.get("prisma") == "enum"}
        models = {s.name: s for s in symbols if s.meta.get("prisma") in {"model", "view"}}
        values: dict[str, dict[str, SymbolRow]] = defaultdict(dict)
        columns: dict[tuple[str, str], SymbolRow] = {}
        tables = {_bare(m.meta.get("db_name", m.name)): m for m in models.values()}
        by_model = {m.name: _bare(m.meta.get("db_name", m.name)) for m in models.values()}
        for symbol in symbols:
            role = symbol.meta.get("prisma")
            owner, _, member = (symbol.qualified_name or "").partition(".")
            if role == "enum-value" and owner in enums:
                values[owner][symbol.meta.get("db_name", member)] = symbol
            elif role == "field" and owner in by_model:
                columns[(by_model[owner], symbol.meta.get("db_name", member))] = symbol
        base = posixpath.dirname(row.rel_path)
        prefix = f"{base}/migrations/" if base else "migrations/"
        migrations = tuple(
            sorted(
                (m for m in sql_files[row.repo_id] if m.rel_path.startswith(prefix)),
                key=lambda m: m.rel_path,
            )
        )
        found.append(
            PrismaProject(
                schema=row,
                provider=meta.get("provider"),
                url_env=meta.get("url_env"),
                specifiers=_specifiers(row, symbols),
                enums=enums,
                models=models,
                db_types={_bare(e.meta.get("db_name", e.name)): e for e in enums.values()},
                tables=tables,
                columns=columns,
                values={k: dict(v) for k, v in values.items()},
                migrations=migrations,
            )
        )
    return found


def _specifiers(row: FileRow, symbols: Sequence[SymbolRow]) -> tuple[str, ...]:
    found: list[str] = []
    for generator in symbols:
        meta = generator.meta
        if meta.get("prisma") != "generator" or meta.get("provider") not in _CLIENT_GENERATORS:
            continue
        output = meta.get("output")
        if output is None:
            found.append(_DEFAULT_CLIENT)
            continue
        joined = posixpath.normpath(posixpath.join(posixpath.dirname(row.rel_path), output))
        if "node_modules/" not in f"{joined}/":
            continue
        package = f"{joined}/".rsplit("node_modules/", 1)[1].rstrip("/")
        found.append(_DEFAULT_CLIENT if package.startswith(".prisma/client") else package)
    return tuple(dict.fromkeys(found))


def _bare(name: str) -> str:
    """A database name without PostgreSQL's default schema."""
    return name.removeprefix("public.")


def _place(row: FileRow | None) -> tuple[str, str]:
    return (row.repo_slug, row.rel_path) if row else ("", "")


def plan_prisma(
    projects: Sequence[PrismaProject],
    use_claims: Sequence[UnresolvedRefRecord],
    files: Mapping[int, FileRow],
    by_file: Mapping[int, Sequence[SymbolRow]],
    *,
    producer: str,
) -> PrismaPlan:
    """Link every project's migrations and client uses. Writes nothing."""
    planner = _Planner(files, by_file, producer)
    for project in projects:
        planner.migrations(project)
    planner.clients(projects, use_claims)
    decisions = sorted(planner.decisions.values(), key=lambda r: (r.family, r.subject))
    counts: dict[str, int] = defaultdict(int)
    for row in decisions:
        counts[row.decision] += 1
    detail = ", ".join(f"{n} {d}" for d, n in sorted(counts.items()))
    return PrismaPlan(
        edges=tuple(
            sorted(planner.edges.values(), key=lambda e: (e.src_id, e.dst_id, e.line_start or 0))
        ),
        decisions=tuple(decisions),
        bound=planner.bound,
        crossings=planner.crossings,
        detail=f"prisma: {len(projects)} project(s); {detail or 'no decisions'}",
    )


class _Planner:
    def __init__(
        self,
        files: Mapping[int, FileRow],
        by_file: Mapping[int, Sequence[SymbolRow]],
        producer: str,
    ) -> None:
        self._files = files
        self._by_file = by_file
        self._producer = producer
        self.edges: dict[tuple[int, int, int], EdgeRecord] = {}
        self.decisions: dict[str, LinkDecisionRow] = {}
        self.bound: dict[tuple[int, str], SymbolRow] = {}
        self.crossings = 0

    # -- migrations ------------------------------------------------------------------

    def migrations(self, project: PrismaProject) -> None:
        if not project.migrations:
            return
        if project.provider != POSTGRES:
            decision = DIALECT_UNKNOWN if project.provider is None else DIALECT_UNSUPPORTED
            for migration in project.migrations:
                self._decide(
                    MIGRATIONS,
                    f"{migration.repo_slug}:{migration.rel_path}",
                    migration,
                    migration.file_entity_id,
                    None,
                    decision,
                    {"schema": project.at, "provider": project.provider},
                )
            return
        state: dict[str, list[str]] = {}
        created: set[str] = set()
        refs = {
            name: self._ref(MIGRATIONS, self._subject(project, enum))
            for name, enum in project.enums.items()
        }
        for migration in project.migrations:
            statements = sorted(
                (
                    s
                    for s in self._by_file.get(migration.file_id, ())
                    if s.type is EntityType.CLASS and s.meta.get("sql") in _TYPE_STATEMENTS
                ),
                key=lambda s: s.line_start or 0,
            )
            for statement in statements:
                name = _bare(statement.qualified_name or "")
                meta = statement.meta
                kind = meta.get("sql")
                if kind == "enum-type":
                    state[name] = json.loads(meta.get("values") or "[]")
                    created.add(name)
                elif kind == "drop-type":
                    state.pop(name, None)
                elif meta.get("action") == "add-value" and name in state:
                    if meta.get("value") not in state[name]:
                        state[name].append(str(meta.get("value")))
                elif meta.get("action") == "rename-value" and name in state:
                    state[name] = [
                        str(meta.get("to")) if v == meta.get("from") else v for v in state[name]
                    ]
                elif meta.get("action") == "rename" and name in state:
                    state[_bare(str(meta.get("to")))] = state.pop(name)
                enum = project.db_types.get(name)
                if enum is not None:
                    self._edge(
                        enum,
                        statement,
                        migration,
                        {
                            MIGRATE_EVIDENCE: {
                                "schema": project.at,
                                "migration": f"{migration.repo_slug}/{migration.rel_path}",
                                "type": name,
                                "decision_ref": refs[enum.name],
                            }
                        },
                    )
        for name, enum in sorted(project.enums.items()):
            db_type = _bare(enum.meta.get("db_name", name))
            declared = sorted(project.values.get(name, {}))
            evidence: dict[str, object] = {"type": db_type}
            if db_type not in created:
                decision = MIGRATION_MISSING
            else:
                migrated = state.get(db_type, [])
                missing = sorted(set(declared) - set(migrated))
                extra = sorted(set(migrated) - set(declared))
                decision = MIGRATION_DRIFT if missing or extra else LINKED
                if missing:
                    evidence["missing_in_migrations"] = missing
                if extra:
                    evidence["missing_in_schema"] = extra
            self._decide(
                MIGRATIONS,
                self._subject(project, enum),
                project.schema,
                enum.entity_id,
                enum,
                decision,
                evidence,
            )

    # -- the client ------------------------------------------------------------------

    def clients(
        self, projects: Sequence[PrismaProject], use_claims: Sequence[UnresolvedRefRecord]
    ) -> None:
        by_specifier: dict[tuple[int, str], list[PrismaProject]] = defaultdict(list)
        for project in projects:
            for specifier in project.specifiers:
                by_specifier[(project.schema.repo_id, specifier)].append(project)
        used: dict[tuple[int, str], int] = defaultdict(int)
        for claim in sorted(use_claims, key=lambda c: (c.file_id, c.line_start or 0, c.src_id)):
            meta = json.loads(claim.meta_json or "{}")
            origin = self._files.get(claim.file_id)
            module = meta.get("module")
            if origin is None or not module:
                continue
            key = (origin.repo_id, str(module))
            candidates = by_specifier.get(key)
            if not candidates or len(candidates) != 1:
                continue
            project = candidates[0]
            name = str(meta.get("imported") or claim.target_key)
            target = project.enums.get(name) or project.models.get(name)
            if target is None:
                continue
            used[key] += 1
            self.bound[(claim.src_id, name)] = target
            subject = f"{origin.repo_slug}:{module}"
            ref = self._ref(CLIENT, subject)
            line = claim.line_start
            dedupe = (claim.src_id, target.entity_id, line or 0)
            if dedupe in self.edges:
                continue
            if origin.repo_id != target.repo_id:
                self.crossings += 1
            self.edges[dedupe] = EdgeRecord(
                src_id=claim.src_id,
                dst_id=target.entity_id,
                type=EdgeType.REFERENCES,
                repo_id=origin.repo_id,
                file_id=origin.file_id,
                line_start=line,
                line_end=claim.line_end if claim.line_end is not None else line,
                parser=self._producer,
                resolution=json.dumps(
                    {
                        CLIENT_EVIDENCE: {
                            "schema": project.at,
                            "specifier": module,
                            "decision_ref": ref,
                        }
                    },
                    sort_keys=True,
                ),
            )
        for (repo_id, specifier), found in sorted(by_specifier.items()):
            schema = found[0].schema
            subject = f"{schema.repo_slug}:{specifier}"
            if len(found) > 1:
                self._decide(
                    CLIENT,
                    subject,
                    schema,
                    schema.file_entity_id,
                    None,
                    CLIENT_AMBIGUOUS,
                    {"schemas": sorted(p.at for p in found)},
                )
            elif used[(repo_id, specifier)]:
                self._decide(
                    CLIENT,
                    subject,
                    schema,
                    schema.file_entity_id,
                    None,
                    LINKED,
                    {"schema": found[0].at, "uses": used[(repo_id, specifier)]},
                )

    # -- records ---------------------------------------------------------------------

    @staticmethod
    def _subject(project: PrismaProject, enum: SymbolRow) -> str:
        return f"{project.schema.repo_slug}:{project.schema.rel_path}::{enum.name}"

    @staticmethod
    def _ref(family: str, subject: str) -> str:
        return hashlib.sha256(f"{family}\0{subject}".encode()).hexdigest()[:24]

    def _decide(
        self,
        family: str,
        subject: str,
        row: FileRow,
        src_id: int,
        symbol: SymbolRow | None,
        decision: str,
        evidence: Mapping[str, object],
    ) -> str:
        ref = self._ref(family, subject)
        self.decisions[ref] = LinkDecisionRow(
            decision_ref=ref,
            family=family,
            repo_id=row.repo_id,
            file_id=row.file_id,
            src_id=src_id,
            line_start=symbol.line_start if symbol else None,
            line_end=(symbol.line_end or symbol.line_start) if symbol else None,
            subject=subject,
            decision=decision,
            evidence_json=json.dumps(evidence, sort_keys=True, separators=(",", ":")),
        )
        return ref

    def _edge(
        self, source: SymbolRow, target: SymbolRow, row: FileRow, resolution: Mapping[str, object]
    ) -> None:
        key = (source.entity_id, target.entity_id, target.line_start or 0)
        if key in self.edges:
            return
        if source.repo_id != target.repo_id:
            self.crossings += 1
        self.edges[key] = EdgeRecord(
            src_id=source.entity_id,
            dst_id=target.entity_id,
            type=EdgeType.GENERATES,
            repo_id=row.repo_id,
            file_id=row.file_id,
            line_start=target.line_start,
            line_end=target.line_end if target.line_end is not None else target.line_start,
            parser=self._producer,
            resolution=json.dumps(resolution, sort_keys=True),
        )
