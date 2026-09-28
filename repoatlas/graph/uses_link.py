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
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from repoatlas.graph.modules import ModuleResolver, binding_pairs
from repoatlas.graph.packages import CROSSING, PackageLinks
from repoatlas.graph.resolution import ResolutionIndex
from repoatlas.graph.uses import UseResolver, table_from_meta, use_resolution
from repoatlas.models.dependencies import DependencyRecord
from repoatlas.models.parsing import USES_EXPORT_TABLE, EdgeType
from repoatlas.models.telemetry import UsesWork
from repoatlas.storage.protocols import EdgeRecord, UnresolvedRefRecord
from repoatlas.storage.sqlite.stores import SqliteKnowledgeStore

#: Changed whenever what a use resolves to changes for the same facts, so edges kept
#: from before the change are never trusted: the next sync resolves every use again.
USES_RESOLUTION_VERSION = "1"
COMPLETE = f"complete:{USES_RESOLUTION_VERSION}"
PENDING = "pending"


@dataclass(frozen=True, slots=True)
class UsesScope:
    """The consumer files whose uses a pass resolves again; ``None`` means every file."""

    files: frozenset[int] | None
    reason: str | None = None

    @classmethod
    def everything(cls, reason: str) -> UsesScope:
        return cls(files=None, reason=reason)


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

    modules = ModuleResolver(index, knowledge.list_module_facts(), {})
    tables = _Tables(knowledge, preload=scope.files is None)
    resolver = UseResolver(modules, index, tables, PackageLinks(modules, index, records))
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
        claims_examined=len(uses),
        claims_resolved=len(edges),
        reads_written=len(reads),
        repositories_touched=len({by_id[f].repo_id for f in consumers if f in by_id}),
    )
    return UsesPlan(scope=scope, edges=tuple(edges), reads=reads, work=work)


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
    """Export tables by file id, read from the store as traces ask for them."""

    def __init__(self, knowledge: SqliteKnowledgeStore, *, preload: bool) -> None:
        self._knowledge = knowledge
        self._raw: dict[int, str] = knowledge.list_export_tables(None) if preload else {}
        self._complete = preload
        self._parsed: dict[int, dict[str, Any] | None] = {}

    def __call__(self, file_id: int) -> dict[str, Any] | None:
        if file_id not in self._parsed:
            if not self._complete and file_id not in self._raw:
                self._raw.update(self._knowledge.list_export_tables([file_id]))
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
