"""The ``USES`` stage of the global pass: which uses to resolve again, and resolving them.

Every other edge the global pass writes is rebuilt from scratch on every sync
(:mod:`repoatlas.graph.linker`). ``USES`` edges are not: they are kept between syncs and
replaced one consumer file at a time, because resolving every use again is most of what
a one-file sync used to cost. A file's ``USES`` edges are a function of indexed facts
alone -- its own claims, the export tables its traces read, its repository's files and
module facts, and the package declarations and providers a package link consults
(:mod:`repoatlas.graph.uses`, :mod:`repoatlas.graph.packages`) -- so a file none of whose
inputs changed keeps edges identical to the ones a fresh index would write.

This module plans the stage while writing nothing (:func:`plan_uses`) and applies it
inside the global pass's one transaction (:func:`apply_uses`), which also records that
the stored edges are complete. When they are not known to be -- a first index, a
database from before this stage, a run that died before its commit, or a change to how
uses resolve (:data:`USES_RESOLUTION_VERSION`) -- every use is resolved again.

Otherwise :class:`UsesChangeLog` gathers, as each file is written, what changed that a
use can depend on, and :meth:`UsesChangeLog.scope` turns it into the files to resolve:

* a file whose facts were replaced: its own uses, and every file with a use landing in
  it, because its entities are written again with new ids and those edges went with them;
* a file whose **export surface** changed -- the names it exports, what each is bound to
  (lines aside), the modules it re-exports wholesale, and whether each local export names
  exactly one declaration -- every file whose uses read its export table (``use_reads``);
* a file added or deleted, or a change to a repository's module-resolution facts
  (tsconfig, package manifests, workspaces): every file of that repository and of every
  repository declaring a package it provides, since specifiers resolve within their own
  repository and only a package link, which needs that declaration, reaches into it;
* a change to a repository's declared packages: its own files; to the packages it
  provides: its own files and those of every repository declaring such a package.

Nothing else a use reads can change without one of these.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from repoatlas.dependencies.normalize import NPM
from repoatlas.graph.modules import ModuleResolver, binding_pairs, declared_constraints
from repoatlas.graph.packages import CROSSING, PackageLinks
from repoatlas.graph.resolution import ResolutionIndex
from repoatlas.graph.uses import UseResolver, declared_entity, table_from_meta, use_resolution
from repoatlas.models.dependencies import DependencyRecord
from repoatlas.models.parsing import USES_EXPORT_TABLE, EdgeType
from repoatlas.models.telemetry import UsesWork
from repoatlas.parsers.module_facts import MODULE_RESOLUTION
from repoatlas.storage.protocols import EdgeRecord, SymbolRow, UnresolvedRefRecord
from repoatlas.storage.sqlite.stores import SqliteDependencyStore, SqliteKnowledgeStore

#: Changed whenever what a use resolves to changes for the same facts, so edges kept
#: from before the change are never trusted: the next sync resolves every use again.
USES_RESOLUTION_VERSION = "3"
COMPLETE = f"complete:{USES_RESOLUTION_VERSION}"
PENDING = "pending"


@dataclass(frozen=True, slots=True)
class UsesScope:
    """The consumer files whose uses a pass resolves again; ``None`` means every file.

    The counts say how many files each rule reached; one file may be reached by several.
    """

    files: frozenset[int] | None
    reason: str | None = None
    own: int = 0
    landed: int = 0
    readers: int = 0
    repository: int = 0
    package: int = 0
    repositories: int = 0
    packages: int = 0

    @classmethod
    def everything(cls, reason: str) -> UsesScope:
        return cls(files=None, reason=reason)


@dataclass(frozen=True, slots=True)
class FileState:
    """What one file contributes to other files' uses, compared before and after a write."""

    #: Its export surface (:func:`export_surface`).
    surface: str = "none"
    #: Its module-resolution facts, as recorded.
    facts: tuple[str, ...] = ()
    #: Its npm declared and provided rows (role, name, display name, constraint, line).
    packages: tuple[tuple[str, str, str, str, int], ...] = ()


class UsesChangeLog:
    """What one sync run changed that uses can depend on, gathered as each file is written.

    Each ``before`` is asked inside the file's own transaction, before its facts are
    replaced or deleted, and each ``after`` once they are written. When the stored edges
    are not known complete (``everything``) nothing is gathered: every use is resolved.
    """

    def __init__(
        self,
        knowledge: SqliteKnowledgeStore,
        dependencies: SqliteDependencyStore | None,
        *,
        producer: str,
        everything: str | None,
    ) -> None:
        self._knowledge = knowledge
        self._dependencies = dependencies
        self._producer = producer
        self.everything = everything
        self.own: set[int] = set()
        self.landed: set[int] = set()
        #: Files whose export surface changed and still exist, and readers of deleted ones.
        self.surface: set[int] = set()
        self.readers: set[int] = set()
        #: Repositories whose file set or module facts changed.
        self.repositories: set[int] = set()
        #: Repositories whose declared or provided packages changed.
        self.package_repositories: set[int] = set()
        #: Package names whose providers changed.
        self.packages: set[str] = set()

    def before(self, file_id: int) -> FileState | None:
        """The file's state before its facts are replaced; notes the uses landing in it."""
        if self.everything is not None:
            return None
        self.landed |= self._knowledge.use_consumers_landing_in(file_id, self._producer)
        return self._state(file_id)

    def after(self, repo_id: int, file_id: int, before: FileState | None) -> None:
        """Compare the file's written state with ``before`` (``None`` for a new file)."""
        if self.everything is not None:
            return
        self.own.add(file_id)
        self._compare(repo_id, file_id, before or FileState(), self._state(file_id))

    def files_added(self, repo_id: int) -> None:
        if self.everything is None:
            self.repositories.add(repo_id)

    def files_deleting(self, repo_id: int, file_ids: Sequence[int]) -> None:
        """Before files are deleted: what landed in them, read them and they provided."""
        if self.everything is not None or not file_ids:
            return
        self.repositories.add(repo_id)
        for file_id in file_ids:
            self.landed |= self._knowledge.use_consumers_landing_in(file_id, self._producer)
            self.readers |= self._knowledge.use_readers([file_id])
            self._compare(repo_id, file_id, self._state(file_id), FileState())

    def repository_removing(self, repo_id: int) -> None:
        """Before a repository is forgotten: the packages it provided lose a provider."""
        if self.everything is not None or self._dependencies is None:
            return
        self.packages |= self._dependencies.names_provided_by([repo_id], NPM)

    def scope(self) -> UsesScope:
        """The files whose uses may have changed (see the module docstring)."""
        if self.everything is not None:
            return UsesScope.everything(self.everything)
        readers = self.readers | (
            self._knowledge.use_readers(sorted(self.surface)) if self.surface else set()
        )
        dependents = set(self.repositories)
        package_files: set[int] = set()
        if self._dependencies is not None:
            provided = self._dependencies.names_provided_by(sorted(dependents), NPM)
            dependents |= self._dependencies.repositories_declaring(sorted(provided), NPM)
            package_files = self._knowledge.file_ids_of_repositories(
                sorted(self._dependencies.repositories_declaring(sorted(self.packages), NPM))
            )
        repositories = dependents | self.package_repositories
        repository_files = self._knowledge.file_ids_of_repositories(sorted(repositories))
        files = self.own | self.landed | readers | repository_files | package_files
        return UsesScope(
            files=frozenset(files),
            own=len(self.own),
            landed=len(self.landed),
            readers=len(readers),
            repository=len(repository_files),
            package=len(package_files),
            repositories=len(repositories),
            packages=len(self.packages),
        )

    def _state(self, file_id: int) -> FileState:
        tables = self._knowledge.list_export_tables([file_id])
        return FileState(
            surface=export_surface(
                file_id, tables.get(file_id), self._knowledge.list_symbols(file_id=file_id)
            ),
            facts=tuple(
                sorted(
                    claim.meta_json or ""
                    for claim in self._knowledge.list_module_facts(file_id)
                    if claim.meta_json and MODULE_RESOLUTION in claim.meta_json
                )
            ),
            packages=(
                self._dependencies.package_rows_for_file(file_id, NPM)
                if self._dependencies is not None
                else ()
            ),
        )

    def _compare(self, repo_id: int, file_id: int, before: FileState, after: FileState) -> None:
        if before.surface != after.surface:
            self.surface.add(file_id)
        if before.facts != after.facts:
            self.repositories.add(repo_id)
        if before.packages != after.packages:
            # Declarations are read only for this repository's own files; provisions for
            # them too (``provided-locally``), and for every file declaring the package.
            self.package_repositories.add(repo_id)
            provided = {row for row in before.packages if row[0] == "provided"} ^ {
                row for row in after.packages if row[0] == "provided"
            }
            self.packages |= {row[1] for row in provided}


def export_surface(file_id: int, meta_json: str | None, symbols: Sequence[SymbolRow]) -> str:
    """What a trace passing through this file reads from it, whatever it lands on.

    The export table, with each well-formed ``local`` binding's line replaced by whether
    it names exactly one declaration (:func:`~repoatlas.graph.uses.declared_entity`, as a
    trace reads it). A use that lands on a declaration here is re-resolved whenever the
    file is written, so the line it lands on need not be part of this.
    """
    table = table_from_meta(meta_json) if meta_json else None
    if table is None:
        return "none"
    declared = ResolutionIndex.build([], list(symbols)).symbols_by_file.get(file_id)
    names: dict[str, Any] = {}
    for exported, spec in table.get("names", {}).items():
        if spec and spec[0] == "local" and len(spec) == 3 and spec[2].isdigit():
            found = declared_entity(declared, spec[1], int(spec[2]))
            names[exported] = ["local", spec[1], "declared" if found is not None else "none"]
        else:
            names[exported] = spec
    return json.dumps({"names": names, "star": table.get("star", [])}, sort_keys=True)


@dataclass(frozen=True, slots=True)
class UsesPlan:
    """The ``USES`` edges of the files in scope, computed but not yet written."""

    scope: UsesScope
    edges: tuple[EdgeRecord, ...] = ()
    #: (consumer file, file whose export table its uses read), for every file in scope.
    reads: tuple[tuple[int, int], ...] = ()
    work: UsesWork = field(default_factory=UsesWork)


@dataclass(frozen=True, slots=True)
class UsesApplied:
    """What applying a plan wrote, and the totals the sync reports."""

    work: UsesWork
    #: Every ``USES`` edge the global pass now holds, and every use it tried to resolve.
    edges: int
    claims: int


def stored_scope(knowledge: SqliteKnowledgeStore) -> UsesScope | None:
    """Every file, with the reason, unless the stored edges are known complete."""
    state = knowledge.uses_links_state()
    if state == COMPLETE:
        return None
    if state is None:
        return UsesScope.everything("no USES edges recorded yet")
    if state == PENDING:
        return UsesScope.everything("the previous sync did not finish")
    return UsesScope.everything("USES resolution changed")


def plan_uses(
    knowledge: SqliteKnowledgeStore,
    index: ResolutionIndex,
    records: Sequence[DependencyRecord],
    *,
    producer: str,
    scope: UsesScope,
) -> UsesPlan:
    """Resolve the uses of the files in ``scope``. **Writes nothing.**"""
    files = None if scope.files is None else sorted(scope.files)
    claims = knowledge.list_use_claims(files)
    imports: dict[int, list[UnresolvedRefRecord]] = {}
    uses: list[UnresolvedRefRecord] = []
    for claim in claims:
        if claim.edge_type is EdgeType.IMPORTS:
            imports.setdefault(claim.file_id, []).append(claim)
        elif claim.target_key != USES_EXPORT_TABLE:
            uses.append(claim)

    resolver = use_resolver(knowledge, index, records)
    by_id = {row.file_id: row for rows in index.files_by_repo.values() for row in rows.values()}

    edges: list[EdgeRecord] = []
    for claim in uses:
        origin = by_id.get(claim.file_id)
        if origin is None:
            continue
        meta = json.loads(claim.meta_json) if claim.meta_json else {}
        found = resolver.resolve(origin, meta)
        if found is None:
            continue
        entity_id, fields = found
        crossing = fields.get(CROSSING)
        if isinstance(crossing, dict):
            line = _import_line(imports.get(claim.file_id, []), meta)
            if line is not None:
                crossing["imported_at"] = line
        edges.append(
            EdgeRecord(
                src_id=claim.src_id,
                dst_id=entity_id,
                type=claim.edge_type,
                confidence=claim.confidence,
                repo_id=origin.repo_id,
                file_id=origin.file_id,
                line_start=claim.line_start,
                line_end=claim.line_end,
                parser=producer,
                resolution=use_resolution(meta, **fields),
            )
        )

    consumers = {claim.file_id for claim in uses}
    reads = tuple(
        (consumer, read)
        for consumer in sorted(resolver.reads)
        for read in sorted(resolver.reads[consumer])
    )
    work = UsesWork(
        mode="global" if scope.files is None else "incremental",
        reason=scope.reason,
        files_invalidated=len(consumers) if scope.files is None else len(scope.files),
        own=scope.own,
        landed=scope.landed,
        readers=scope.readers,
        repository=scope.repository,
        package=scope.package,
        repositories_invalidated=scope.repositories,
        packages_invalidated=scope.packages,
        claims_examined=len(uses),
        claims_resolved=len(edges),
        reads_written=len(reads),
        repositories_touched=len({by_id[f].repo_id for f in consumers if f in by_id}),
    )
    return UsesPlan(scope=scope, edges=tuple(edges), reads=reads, work=work)


def use_resolver(
    knowledge: SqliteKnowledgeStore, index: ResolutionIndex, records: Sequence[DependencyRecord]
) -> UseResolver:
    """The resolver a pass resolves uses with, built from indexed facts alone."""
    modules = ModuleResolver(
        index, knowledge.list_module_facts(), {}, declared=declared_constraints(index, records)
    )
    return UseResolver(modules, index, _Tables(knowledge), PackageLinks(modules, index, records))


def apply_uses(knowledge: SqliteKnowledgeStore, plan: UsesPlan, *, producer: str) -> UsesApplied:
    """Replace the planned files' ``USES`` edges. Joins the caller's transaction."""
    files = None if plan.scope.files is None else sorted(plan.scope.files)
    with knowledge.transaction():
        deleted = knowledge.delete_use_edges(producer, files)
        knowledge.add_edges(plan.edges)
        knowledge.replace_use_reads(files, plan.reads)
        knowledge.set_uses_links_state(COMPLETE)
        edges = knowledge.count_use_edges(producer)
        claims = knowledge.count_use_claims()
    work = plan.work.model_copy(
        update={"edges_deleted": deleted, "edges_inserted": len(plan.edges)}
    )
    return UsesApplied(work=work, edges=edges, claims=claims)


class _Tables:
    """Export tables by file id, read in one query and decoded as traces ask for them.

    One query for all of them, even when only a few files' uses are resolved: a trace
    may pass through any barrel, and asking for each file's table in turn scanned the
    export tables once per file.
    """

    def __init__(self, knowledge: SqliteKnowledgeStore) -> None:
        self._raw: dict[int, str] = knowledge.list_export_tables(None)
        self._parsed: dict[int, dict[str, Any] | None] = {}

    def __call__(self, file_id: int) -> dict[str, Any] | None:
        if file_id not in self._parsed:
            raw = self._raw.get(file_id)
            self._parsed[file_id] = table_from_meta(raw) if raw else None
        return self._parsed[file_id]


def _import_line(siblings: Sequence[UnresolvedRefRecord], meta: dict[str, str]) -> int | None:
    """The line of the import statement that binds a use's local name, when one does.

    Found among the same file's own claims: the import of that specifier whose bindings
    include the name. The first by line when the file imports it twice.
    """
    specifier, local = meta.get("module"), meta.get("local")
    if not specifier or not local:
        return None
    lines = sorted(
        claim.line_start
        for claim in siblings
        if claim.edge_type is EdgeType.IMPORTS
        and claim.line_start is not None
        and claim.target_key == specifier
        and claim.meta_json
        and _binds(json.loads(claim.meta_json), local)
    )
    return lines[0] if lines else None


def _binds(meta: dict[str, str], local: str) -> bool:
    """Whether an import (never a re-export, which binds nothing) binds ``local``."""
    if meta.get("role") == "re-export":
        return False
    return any(bound == local for bound, _ in _bound_names(meta))


def _bound_names(meta: dict[str, str]) -> list[tuple[str, str | None]]:
    """(local, exported) for each name one import binds.

    Reads the parser's binding record when there is one, and otherwise the older
    ``names`` list, whose ``type X`` and ``A as B`` spellings are taken apart here.
    """
    if any(key in meta for key in ("bindings", "default", "namespace")):
        found: list[tuple[str, str | None]] = [
            (local, name) for name, local in binding_pairs(meta.get("bindings"))
        ]
        if meta.get("default"):
            found.append((meta["default"], "default"))
        if meta.get("namespace"):
            found.append((meta["namespace"], None))
        return found
    found = []
    for raw in (meta.get("names") or "").split(","):
        name = raw.strip().removeprefix("type ").strip()
        if not name:
            continue
        exported, _, local = name.partition(" as ")
        found.append((local.strip() or exported.strip(), exported.strip()))
    return found
