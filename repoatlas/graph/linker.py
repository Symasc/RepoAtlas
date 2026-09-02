"""Reference resolution (ARCHITECTURE.md §5, §6).

Parsers emit unresolved references; this stage turns them into graph edges. It
runs in two passes, and the split is deliberate:

**Per file** (:meth:`ReferenceLinker.link_file`, during indexing) creates only what
one file can prove on its own — its definitions, its interned global targets
(environment variables, modules, commands) and references to symbols it defines
itself. Everything else is written to ``unresolved_refs`` as a durable record of
the claim.

**Globally** (:meth:`ReferenceLinker.link_global`, once at the end of a sync)
resolves those standing claims against everything currently indexed: the import
graph, script paths that cross repository boundaries, Salt includes and
requisites, and package names that another managed repository provides.

The global pass is **idempotent**: it deletes every edge it previously created,
then rebuilds from the standing claims. Running it twice therefore gives the same
graph as running it once, which is what lets an incremental sync equal a full
reindex (V1_SPEC §12). It is also **global by necessity** — a change in one
repository can create or invalidate a cross-repository edge in another.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from repoatlas.dependencies.normalize import GEM, NPM, PYPI
from repoatlas.graph.keys import file_key, global_key, package_key, repository_key, symbol_key
from repoatlas.graph.resolution import ResolutionIndex, flavor_for
from repoatlas.models.dependencies import DependencyRecord
from repoatlas.models.parsing import (
    EdgeType,
    EntityType,
    ParsedFile,
    ParsedReference,
    ParsedSymbol,
)
from repoatlas.storage.protocols import (
    EdgeRecord,
    EntityRecord,
    FileRow,
    UnresolvedRefRecord,
)
from repoatlas.storage.sqlite.stores import SqliteDependencyStore, SqliteKnowledgeStore

#: Reference kinds interned as global entities during the per-file pass.
_INTERNED_KINDS = frozenset(
    {EntityType.ENV_VAR, EntityType.MODULE, EntityType.COMMAND, EntityType.CONFIG}
)

#: Reference kinds resolved to a file by path during the global pass.
_PATH_KINDS = frozenset({EntityType.SCRIPT, EntityType.FILE})

#: Producer recorded on every edge the global pass creates, so it can delete
#: exactly its own output and rebuild (see the module docstring).
GLOBAL_PARSER = "linker:global"

#: Ecosystems whose package references can name a managed repository.
_REPOSITORY_ECOSYSTEMS = frozenset({PYPI, NPM, GEM})


class _NotClaimable:
    """Sentinel: this reference names nothing the global pass could resolve."""

    __slots__ = ()


_NOT_CLAIMABLE = _NotClaimable()


@dataclass(frozen=True, slots=True)
class LinkResult:
    edges: int
    unresolved: int


@dataclass(frozen=True, slots=True)
class GlobalLinkResult:
    """What one global link pass produced."""

    edges: int
    resolved: int
    unresolved: int
    cross_repository: int


class ReferenceLinker:
    """Resolves references into edges, per file and then globally."""

    def __init__(
        self,
        knowledge: SqliteKnowledgeStore,
        dependencies: SqliteDependencyStore | None = None,
    ) -> None:
        self._knowledge = knowledge
        self._dependencies = dependencies

    # -- per-file pass -------------------------------------------------------

    def link_file(
        self,
        *,
        repo_id: int,
        repo_name: str,
        file_id: int,
        rel_path: str,
        parsed: ParsedFile,
    ) -> LinkResult:
        """Create every edge this file can prove alone; record the rest as claims."""
        file_entity_id = self._knowledge.find_file_entity(file_id, file_key(repo_name, rel_path))
        if file_entity_id is None:
            return LinkResult(edges=0, unresolved=0)

        symbol_ids = self._symbol_ids(file_id, repo_name, rel_path, parsed.symbols)
        imported = _imported_names(parsed)

        edges: list[EdgeRecord] = [
            EdgeRecord(
                src_id=file_entity_id,
                dst_id=entity_id,
                type=EdgeType.DEFINES,
                repo_id=repo_id,
                file_id=file_id,
                parser=parsed.parser,
            )
            for entity_id in symbol_ids.values()
        ]

        claims: list[UnresolvedRefRecord] = []
        for reference in parsed.references:
            source_id = symbol_ids.get(reference.source_symbol or "", file_entity_id)

            if reference.target_kind in _INTERNED_KINDS:
                target_id = self._knowledge.intern_entity(
                    EntityRecord(
                        type=reference.target_kind,
                        key=global_key(reference.target_kind, reference.target_key),
                        name=reference.target_key,
                        qualified_name=reference.target_key,
                    )
                )
                edges.append(
                    EdgeRecord(
                        src_id=source_id,
                        dst_id=target_id,
                        type=reference.edge_type,
                        confidence=reference.confidence,
                        repo_id=repo_id,
                        file_id=file_id,
                        line_start=reference.line_start,
                        line_end=reference.line_end,
                        parser=parsed.parser,
                    )
                )
                if reference.target_kind in {EntityType.MODULE, EntityType.CONFIG}:
                    # The interned node is a fact on its own; resolving the
                    # specifier to a file needs the whole tree, so it is also a
                    # claim for the global pass.
                    claims.append(self._claim(repo_id, file_id, source_id, reference, parsed))
                continue

            local = symbol_ids.get(reference.target_key)
            if local is not None:
                edges.append(
                    EdgeRecord(
                        src_id=source_id,
                        dst_id=local,
                        type=reference.edge_type,
                        confidence=reference.confidence,
                        repo_id=repo_id,
                        file_id=file_id,
                        line_start=reference.line_start,
                        line_end=reference.line_end,
                        parser=parsed.parser,
                    )
                )
                continue

            module = self._claimable_via(reference, imported)
            if not isinstance(module, _NotClaimable):
                claims.append(
                    self._claim(repo_id, file_id, source_id, reference, parsed, module=module)
                )

        self._knowledge.add_edges(edges)
        self._knowledge.add_unresolved_refs(claims)
        return LinkResult(edges=len(edges), unresolved=len(claims))

    @staticmethod
    def _claimable_via(
        reference: ParsedReference, imported: dict[str, str]
    ) -> str | _NotClaimable | None:
        """The module a symbol reference came through, or why it is not a claim.

        A symbol reference is recorded **only when this file imported that name**,
        and the import's specifier is carried on the claim so the global pass has
        something exact to resolve against. Without the import there is nothing to
        resolve, and wiring a call to a same-named definition elsewhere would
        assert a fact the parser cannot support (V1_SPEC §2).

        Every other kind — packages, paths, Salt targets, templates — is always
        recorded, because its target key names the target directly.
        """
        if reference.target_kind not in {
            EntityType.SYMBOL,
            EntityType.CLASS,
            EntityType.FUNCTION,
        }:
            return None
        root = reference.target_key.split(".", 1)[0]
        specifier = imported.get(root)
        return specifier if specifier is not None else _NOT_CLAIMABLE

    def _claim(
        self,
        repo_id: int,
        file_id: int,
        source_id: int,
        reference: ParsedReference,
        parsed: ParsedFile,
        *,
        module: str | None = None,
    ) -> UnresolvedRefRecord:
        meta = {**reference.meta, "parser": parsed.parser}
        if module is not None:
            meta["module"] = module
        return UnresolvedRefRecord(
            repo_id=repo_id,
            file_id=file_id,
            src_id=source_id,
            edge_type=reference.edge_type,
            target_kind=reference.target_kind,
            target_key=reference.target_key,
            line_start=reference.line_start,
            line_end=reference.line_end,
            confidence=reference.confidence,
            meta_json=json.dumps(meta, sort_keys=True),
        )

    def _symbol_ids(
        self,
        file_id: int,
        repo_name: str,
        rel_path: str,
        symbols: tuple[ParsedSymbol, ...],
    ) -> dict[str, int]:
        """Map qualified symbol name to its persisted entity id."""
        mapping: dict[str, int] = {}
        for symbol in symbols:
            key = symbol_key(repo_name, rel_path, symbol.qualified_name, symbol.line_start)
            entity_id = self._knowledge.find_file_entity(file_id, key)
            if entity_id is not None:
                mapping[symbol.qualified_name] = entity_id
                mapping.setdefault(symbol.name, entity_id)
        return mapping

    # -- global pass ---------------------------------------------------------

    def link_global(self) -> GlobalLinkResult:
        """Resolve every standing claim against everything currently indexed."""
        self._knowledge.delete_edges_by_parser(GLOBAL_PARSER)

        file_rows = self._knowledge.list_file_rows()
        symbols = self._knowledge.list_symbols()
        index = ResolutionIndex.build(file_rows, symbols)
        by_id = {row.file_id: row for row in file_rows}

        claims = self._knowledge.list_unresolved_refs()
        by_file: dict[int, list[UnresolvedRefRecord]] = {}
        for claim in claims:
            by_file.setdefault(claim.file_id, []).append(claim)

        edges: list[EdgeRecord] = []
        resolved = 0
        unresolved = 0

        for claim in claims:
            origin = by_id.get(claim.file_id)
            if origin is None:
                unresolved += 1
                continue
            edge = self._resolve(claim, origin, index, by_file.get(claim.file_id, []))
            if edge is None:
                unresolved += 1
                continue
            resolved += 1
            edges.append(edge)

        cross = self._link_dependencies(index, edges)
        self._knowledge.add_edges(edges)

        return GlobalLinkResult(
            edges=len(edges),
            resolved=resolved,
            unresolved=unresolved,
            cross_repository=cross,
        )

    def _resolve(
        self,
        claim: UnresolvedRefRecord,
        origin: FileRow,
        index: ResolutionIndex,
        siblings: list[UnresolvedRefRecord],
    ) -> EdgeRecord | None:
        meta = json.loads(claim.meta_json) if claim.meta_json else {}
        kind = claim.target_kind

        if kind is EntityType.MODULE:
            return self._resolve_module(claim, origin, index, meta)
        if kind in _PATH_KINDS:
            target = index.resolve_repo_relative_path(origin, claim.target_key)
            return self._edge(claim, origin, target.file_entity_id) if target else None
        if kind is EntityType.SALT_STATE:
            return self._resolve_salt_state(claim, origin, index)
        if kind is EntityType.TEMPLATE:
            return self._resolve_template(claim, origin, index)
        if kind is EntityType.CONFIG:
            return self._resolve_config(claim, origin, index, siblings)
        if kind in {EntityType.SYMBOL, EntityType.CLASS, EntityType.FUNCTION}:
            return self._resolve_symbol(claim, origin, index, meta)
        return None

    def _resolve_config(
        self,
        claim: UnresolvedRefRecord,
        origin: FileRow,
        index: ResolutionIndex,
        siblings: list[UnresolvedRefRecord],
    ) -> EdgeRecord | None:
        """``pillar['service']['host']`` → the key that defines it.

        Salt resolves pillar data through a pillar top file RepoAtlas cannot read,
        so the search is confined to files this one **provably** pulls in: itself
        and the targets of its own ``include:`` statements. Within that set the
        key path must match exactly and match exactly once; anything else resolves
        to nothing rather than to a plausible guess.
        """
        root, _, path = claim.target_key.partition(":")
        if root != "pillar" or not path:
            return None
        qualified = path.replace(":", ".")

        scope = [origin]
        for sibling in siblings:
            if (
                sibling.target_kind is EntityType.SALT_STATE
                and sibling.edge_type is EdgeType.INCLUDES
            ):
                included = index.resolve_salt_include(origin, sibling.target_key)
                if included is not None:
                    scope.append(included)

        matches = [
            symbol
            for candidate in scope
            for symbol in index.symbols_by_file.get(candidate.file_id, {}).get(qualified, [])
            if symbol.type is EntityType.CONFIG and symbol.qualified_name == qualified
        ]
        if len(matches) != 1:
            return None
        return self._edge(claim, origin, matches[0].entity_id)

    def _resolve_module(
        self,
        claim: UnresolvedRefRecord,
        origin: FileRow,
        index: ResolutionIndex,
        meta: dict[str, str],
    ) -> EdgeRecord | None:
        flavor = _flavor(origin, meta)
        if flavor is None:
            return None
        target = index.resolve_module(origin, claim.target_key, flavor)
        return self._edge(claim, origin, target.file_entity_id) if target else None

    def _resolve_symbol(
        self,
        claim: UnresolvedRefRecord,
        origin: FileRow,
        index: ResolutionIndex,
        meta: dict[str, str],
    ) -> EdgeRecord | None:
        """A call whose name this file imported, resolved through the import graph."""
        specifier = meta.get("module")
        flavor = _flavor(origin, meta)
        if not specifier or flavor is None:
            return None
        target_file = index.resolve_module(origin, specifier, flavor)
        if target_file is None:
            return None
        name = claim.target_key.split(".")[-1]
        symbol = index.resolve_symbol(target_file, name)
        return self._edge(claim, origin, symbol.entity_id) if symbol else None

    def _resolve_salt_state(
        self, claim: UnresolvedRefRecord, origin: FileRow, index: ResolutionIndex
    ) -> EdgeRecord | None:
        if claim.edge_type is EdgeType.INCLUDES:
            target = index.resolve_salt_include(origin, claim.target_key)
            return self._edge(claim, origin, target.file_entity_id) if target else None
        state = index.resolve_salt_state(origin.repo_id, claim.target_key)
        return self._edge(claim, origin, state.entity_id) if state else None

    def _resolve_template(
        self, claim: UnresolvedRefRecord, origin: FileRow, index: ResolutionIndex
    ) -> EdgeRecord | None:
        if claim.target_key.startswith("salt://"):
            match = index.resolve_salt_source(claim.target_key)
            if match is None:
                return None
            target, confidence = match
            return self._edge(claim, origin, target.file_entity_id, confidence=confidence)
        target = index.resolve_repo_relative_path(origin, claim.target_key)
        return self._edge(claim, origin, target.file_entity_id) if target else None

    def _edge(
        self,
        claim: UnresolvedRefRecord,
        origin: FileRow,
        dst_id: int,
        *,
        confidence: float | None = None,
    ) -> EdgeRecord:
        return EdgeRecord(
            src_id=claim.src_id,
            dst_id=dst_id,
            type=claim.edge_type,
            confidence=claim.confidence if confidence is None else confidence,
            repo_id=origin.repo_id,
            file_id=origin.file_id,
            line_start=claim.line_start,
            line_end=claim.line_end,
            parser=GLOBAL_PARSER,
        )

    # -- dependencies and cross-repository edges -----------------------------

    def _link_dependencies(self, index: ResolutionIndex, edges: list[EdgeRecord]) -> int:
        """Turn declared dependencies into package and repository edges.

        A repository-to-repository edge is created only on **exact normalized-name
        equality within one ecosystem**, and only when exactly one managed
        repository provides that name (V1_SPEC §9).
        """
        if self._dependencies is None:
            return 0

        cross = 0
        repo_entities: dict[int, int] = {}

        def repository_entity(repo_id: int) -> int | None:
            if repo_id in repo_entities:
                return repo_entities[repo_id]
            name = index.repo_names.get(repo_id)
            if name is None:
                return None
            entity_id = self._knowledge.intern_entity(
                EntityRecord(
                    type=EntityType.REPOSITORY,
                    key=repository_key(name),
                    name=name,
                    repo_id=repo_id,
                )
            )
            repo_entities[repo_id] = entity_id
            return entity_id

        for record in self._dependencies.list_for_repos(None):
            if record.role != "declared":
                continue
            source_id = repository_entity(record.repo_id)
            if source_id is None:
                continue

            package_id = self._knowledge.intern_entity(
                EntityRecord(
                    type=EntityType.PACKAGE,
                    key=package_key(record.ecosystem, record.normalized_name),
                    name=record.name,
                    qualified_name=f"{record.ecosystem}:{record.normalized_name}",
                )
            )
            edges.append(self._dependency_edge(record, source_id, package_id))

            if record.ecosystem not in _REPOSITORY_ECOSYSTEMS:
                continue
            providers = [
                (repo_id, name)
                for repo_id, name in self._dependencies.providers(
                    record.ecosystem, record.normalized_name
                )
                if repo_id != record.repo_id
            ]
            if len(providers) != 1:
                # Nothing provides it, or more than one does. Both are honest
                # "unknown", and picking a winner would be a guess (V1_SPEC §9).
                continue
            target_id = repository_entity(providers[0][0])
            if target_id is None:
                continue
            edges.append(self._dependency_edge(record, source_id, target_id))
            cross += 1

        return cross

    @staticmethod
    def _dependency_edge(record: DependencyRecord, src_id: int, dst_id: int) -> EdgeRecord:
        return EdgeRecord(
            src_id=src_id,
            dst_id=dst_id,
            type=EdgeType.DEPENDS_ON,
            repo_id=record.repo_id,
            file_id=record.file_id,
            line_start=record.line_start,
            line_end=record.line_end,
            parser=GLOBAL_PARSER,
        )


def _imported_names(parsed: ParsedFile) -> dict[str, str]:
    """Local name → module specifier, from this file's own imports.

    This is the import graph as one file sees it, and the only basis on which a
    cross-file symbol reference may later be resolved.
    """
    imported: dict[str, str] = {}
    for reference in parsed.references:
        if reference.edge_type is not EdgeType.IMPORTS:
            continue
        specifier = reference.target_key
        meta = reference.meta
        for name in (meta.get("names") or "").split(","):
            if name.strip():
                imported[name.strip()] = specifier
        alias = meta.get("alias")
        if alias:
            imported[alias] = specifier
        # ``import x.y`` binds ``x``; ``import fs from "node:fs"`` binds ``fs``.
        head = specifier.replace("/", ".").split(".")[0].lstrip(".")
        if head:
            imported.setdefault(head, specifier)
        tail = specifier.replace("\\", "/").rstrip("/").split("/")[-1].split(".")[-1]
        if tail:
            imported.setdefault(tail, specifier)
    return imported


def _flavor(origin: FileRow, meta: dict[str, str]) -> str | None:
    """Import flavor for a claim: from the parser's own hint, else the extension."""
    parser = meta.get("parser", "")
    if parser.startswith("python"):
        return "python"
    if "typescript" in parser:
        return "node"
    if "ruby" in parser:
        return "ruby"
    return flavor_for(origin.rel_path)
