"""Reference resolution (ARCHITECTURE.md §5, §6).

Parsers emit unresolved references; this stage turns them into graph edges. It
runs in two passes, and the split is deliberate:

**Per file** (:meth:`ReferenceLinker.link_file`, during indexing) creates only what
one file can prove on its own — its definitions, its interned global targets
(environment variables, modules, commands, HTTP routes, deployments and
endpoints) and references to symbols it defines itself. Everything else is
written to ``unresolved_refs`` as a durable record of the claim.

**Globally** (:meth:`ReferenceLinker.link_global`, once at the end of a sync)
resolves those standing claims against everything currently indexed: the import
graph, script paths that cross repository boundaries, Salt includes and
requisites, and package names that another managed repository provides.

The global pass is **idempotent**: it deletes every edge it previously created,
then rebuilds from the standing claims. Running it twice therefore gives the same
graph as running it once, which is what lets an incremental sync equal a full
reindex (V1_SPEC §12). It is also **global by necessity** — a change in one
repository can create or invalidate a cross-repository edge in another.

``USES`` edges are the one exception to rebuilding everything. They are kept between
syncs and replaced only for the consumer files a change can reach, because resolving
every declaration use again was most of what a one-file sync cost; which files those
are, and why nothing outside them can have changed, is in
:mod:`repoatlas.graph.uses_link`. A pass that cannot trust the kept edges resolves
every use, so the invariant holds for them too.

Because it is a *replacement*, it is split in two: :meth:`prepare_global` computes
the whole new graph while writing nothing, and :meth:`commit_global` swaps it in
inside a single transaction. Deleting the old graph and inserting the new one used
to be two commits with all the resolution work between them, so any failure in
that gap published a knowledge base with no cross-repository graph at all while
``status`` still reported it current (ARCHITECTURE.md §9).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from repoatlas.dependencies.normalize import REPOSITORY_ECOSYSTEMS
from repoatlas.graph.http_claims import HttpClaimPlan, plan_http_claims
from repoatlas.graph.http_crossings import HttpCrossingPlan, plan_http_crossings
from repoatlas.graph.keys import file_key, global_key, package_key, repository_key, symbol_key
from repoatlas.graph.resolution import ResolutionIndex, flavor_for
from repoatlas.graph.uses import use_resolution
from repoatlas.graph.uses_link import (
    UsesChangeLog,
    UsesPlan,
    UsesScope,
    apply_uses,
    plan_uses,
    stored_scope,
)
from repoatlas.models import http
from repoatlas.models.dependencies import DependencyRecord
from repoatlas.models.parsing import (
    EdgeType,
    EntityType,
    ParsedFile,
    ParsedReference,
    ParsedSymbol,
)
from repoatlas.models.telemetry import LinkTrace, StageTiming, Stopwatch
from repoatlas.parsers.http_facts import PENDING_FACT
from repoatlas.parsers.module_facts import MODULE_RESOLUTION
from repoatlas.storage.protocols import (
    EdgeRecord,
    EntityRecord,
    FileRow,
    UnresolvedRefRecord,
)
from repoatlas.storage.sqlite.stores import SqliteDependencyStore, SqliteKnowledgeStore

#: Reference kinds interned as global entities during the per-file pass.
_INTERNED_KINDS = frozenset(
    {
        EntityType.ENV_VAR,
        EntityType.MODULE,
        EntityType.COMMAND,
        EntityType.CONFIG,
        EntityType.HTTP_ROUTE,
        EntityType.HTTP_DEPLOYMENT,
        EntityType.HTTP_ENDPOINT,
    }
)

#: The shared HTTP kinds, each with the check its target must pass to be interned.
#:
#: A parser already normalized the target, so a failure here is a parser defect
#: rather than a repository's. It is checked anyway because a shared node is where
#: two repositories meet: a target that is not exactly canonical would be a second
#: spelling of an identity, and two spellings of one operation never meet.
_HTTP_CANONICAL = {
    EntityType.HTTP_ROUTE: http.canonical_route,
    EntityType.HTTP_DEPLOYMENT: http.canonical_deployment,
    EntityType.HTTP_ENDPOINT: http.canonical_endpoint,
}

#: Reference kinds resolved to a file by path during the global pass.
_PATH_KINDS = frozenset({EntityType.SCRIPT, EntityType.FILE})

#: Producer recorded on every edge the global pass creates, so it can delete
#: exactly its own output and rebuild (see the module docstring).
GLOBAL_PARSER = "linker:global"


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
    """What one global link pass produced, and what it cost."""

    edges: int
    resolved: int
    unresolved: int
    cross_repository: int
    trace: LinkTrace | None = None


@dataclass(frozen=True, slots=True)
class PendingEdge:
    """A dependency edge whose endpoints are named but not yet interned.

    Endpoints are carried as natural keys rather than row ids so the whole
    replacement graph can be computed without writing anything. They become ids
    inside the swap transaction, where an edge and the entity it points at commit
    together or not at all.
    """

    source: EntityRecord
    target: EntityRecord
    record: DependencyRecord


@dataclass(frozen=True, slots=True)
class GlobalPlan:
    """The complete replacement graph, computed but not yet applied.

    Produced by :meth:`ReferenceLinker.prepare_global` without touching the
    database, and consumed by :meth:`ReferenceLinker.commit_global` in one
    transaction. Nothing here is a partial answer: a plan describes the whole
    global graph, so applying it is a replacement rather than a merge.
    """

    edges: tuple[EdgeRecord, ...] = ()
    dependencies: tuple[PendingEdge, ...] = ()
    #: What the pending HTTP facts established, and why each one did or did not.
    http: HttpClaimPlan = field(default_factory=HttpClaimPlan)
    #: Which requests reach a handler another repository serves, and why the rest do not.
    http_crossings: HttpCrossingPlan = field(default_factory=HttpCrossingPlan)
    #: The ``USES`` edges to replace, and whose (repoatlas.graph.uses_link).
    uses: UsesPlan | None = None
    resolved: int = 0
    unresolved: int = 0
    cross_repository: int = 0
    claims_considered: int = 0
    files_indexed: int = 0
    symbols_indexed: int = 0
    stages: tuple[StageTiming, ...] = ()


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
        repo_slug: str,
        file_id: int,
        rel_path: str,
        parsed: ParsedFile,
    ) -> LinkResult:
        """Create every edge this file can prove alone; record the rest as claims."""
        file_entity_id = self._knowledge.find_file_entity(file_id, file_key(repo_slug, rel_path))
        if file_entity_id is None:
            return LinkResult(edges=0, unresolved=0)

        qualified: dict[str, int] = {}
        values: dict[str, int] = {}
        symbol_ids = self._symbol_ids(
            file_id, repo_slug, rel_path, parsed.symbols, qualified, values
        )
        # A module value owns the uses in its own statement, and is named by its name
        # before anything else is; it is no callee a CALLS or REFERENCES matches.
        sources = {**symbol_ids, **values}
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
            for entity_id in (*symbol_ids.values(), *values.values())
        ]

        claims: list[UnresolvedRefRecord] = []
        for reference in parsed.references:
            source_id = sources.get(reference.source_symbol or "", file_entity_id)

            if reference.edge_type is EdgeType.USES:
                # A declaration's use of a module-scope name (repoatlas.parsers.ts_uses).
                # A name this file declares is proven here, and only by its exact
                # qualified name: the bare-name fallback other references get would be
                # a guess. An imported name, and the file's export table, are claims
                # for the global pass (repoatlas.graph.uses).
                if reference.meta.get("binding") == "local":
                    target = qualified.get(reference.target_key)
                    if target is not None:
                        edges.append(
                            EdgeRecord(
                                src_id=source_id,
                                dst_id=target,
                                type=EdgeType.USES,
                                confidence=reference.confidence,
                                repo_id=repo_id,
                                file_id=file_id,
                                line_start=reference.line_start,
                                line_end=reference.line_end,
                                parser=parsed.parser,
                                resolution=use_resolution(reference.meta),
                            )
                        )
                else:
                    claims.append(self._claim(repo_id, file_id, source_id, reference, parsed))
                continue

            if reference.meta.get(PENDING_FACT):
                # A pending fact (repoatlas.parsers.http_facts): only the global pass
                # can prove it, so it is kept as a claim — never interned here, and
                # never matched against a local definition by name.
                claims.append(self._claim(repo_id, file_id, source_id, reference, parsed))
                continue

            if reference.meta.get(MODULE_RESOLUTION):
                # Configuration the global pass reads to resolve bare specifiers: a
                # fact about this file, not an edge to anything, so never interned.
                claims.append(self._claim(repo_id, file_id, source_id, reference, parsed))
                continue

            if reference.target_kind in _INTERNED_KINDS:
                line_start: int | None = reference.line_start
                line_end: int | None = reference.line_end
                canonical = _HTTP_CANONICAL.get(reference.target_kind)
                if canonical is not None:
                    if canonical(reference.target_key) is None:
                        continue
                    if reference.meta.get("provenance") != "exact":
                        # Composed from literals in more than one place, so no
                        # single span proves it. The edge is kept as a fact about
                        # the file and stored without lines, which is what makes
                        # it uncitable; its constituents keep their own spans.
                        line_start = line_end = None
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
                        line_start=line_start,
                        line_end=line_end,
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
        repo_slug: str,
        rel_path: str,
        symbols: tuple[ParsedSymbol, ...],
        qualified: dict[str, int] | None = None,
        values: dict[str, int] | None = None,
    ) -> dict[str, int]:
        """Map qualified symbol name to its persisted entity id.

        ``qualified``, when given, receives the qualified names alone, without the
        bare-name fallback below. A module value (``ParsedSymbol.uses_only``) is never
        in the mapping: it goes to ``qualified`` and to ``values``, when given.
        """
        mapping: dict[str, int] = {}
        for symbol in symbols:
            key = symbol_key(repo_slug, rel_path, symbol.qualified_name, symbol.line_start)
            entity_id = self._knowledge.find_file_entity(file_id, key)
            if entity_id is not None:
                if qualified is not None:
                    qualified[symbol.qualified_name] = entity_id
                if symbol.uses_only:
                    if values is not None:
                        values[symbol.qualified_name] = entity_id
                    continue
                mapping[symbol.qualified_name] = entity_id
                # A returned object's property is reachable only through the object,
                # never by its bare name: `createCourse()` beside a `createCourse:`
                # property names whatever `createCourse` is bound to, not the property.
                if symbol.meta.get("kind") != "property":
                    mapping.setdefault(symbol.name, entity_id)
        return mapping

    # -- global pass ---------------------------------------------------------

    def link_global(self) -> GlobalLinkResult:
        """Rebuild the global graph: compute the replacement, then swap it in.

        Two phases, and the split is the correctness of the pass rather than an
        optimization. See :meth:`prepare_global` and :meth:`commit_global`.
        """
        return self.commit_global(self.prepare_global())

    def prepare_global(
        self,
        uses: UsesChangeLog | None = None,
        *,
        on_stage: Callable[[str], None] | None = None,
    ) -> GlobalPlan:
        """Compute the complete replacement graph. **Writes nothing.**

        Everything expensive happens here: reading every file row and symbol,
        building the resolution index, resolving every standing claim, and walking
        the dependency table. None of it touches the previous graph, so a failure
        anywhere in this phase leaves the committed state exactly as it was — which
        is the first half of why a failed link pass can no longer publish an empty
        graph (ARCHITECTURE.md §9).

        ``USES`` claims are the exception to "every": ``uses``, what this run changed,
        decides the consumer files whose uses to resolve again, and the rest keep their
        edges (:mod:`repoatlas.graph.uses_link`). Without it, or when the stored ``USES``
        edges are not known complete, every use is resolved.

        Instrumented per stage so the traversal's cost is measurable without
        anyone restructuring it to find out (V1_SPEC §20). ``on_stage`` hears each
        stage's name as it starts, so a progress bar can follow the pass; it is
        told, never asked, and nothing it does reaches the plan.
        """
        stages: list[StageTiming] = []
        entered = on_stage or _unobserved

        entered("index")
        with Stopwatch("index") as watch:
            file_rows = self._knowledge.list_file_rows()
            symbols = self._knowledge.list_symbols()
            index = ResolutionIndex.build(file_rows, symbols)
            watch.candidates_out = len(file_rows)
            watch.detail = f"{len(file_rows)} file(s), {len(symbols)} symbol(s)"
        stages.append(_timing(watch))

        by_id = {row.file_id: row for row in file_rows}

        entered("resolve")
        with Stopwatch("resolve") as watch:
            # Declaration uses resolve in a stage of their own, below; module-resolution
            # configuration is read by that stage, never resolved itself.
            recorded = self._knowledge.list_unresolved_refs(exclude_uses=True)
            claims = [claim for claim in recorded if not _is_module_fact(claim)]
            watch.candidates_in = len(claims)
            by_file: dict[int, list[UnresolvedRefRecord]] = {}
            for claim in claims:
                by_file.setdefault(claim.file_id, []).append(claim)
            # Read once: package links resolve uses against them, and the dependency
            # stage below plans its edges from the same rows.
            records = (
                self._dependencies.list_for_repos(None) if self._dependencies is not None else []
            )

            edges: list[EdgeRecord] = []
            resolved = 0
            unresolved = 0
            pending: list[UnresolvedRefRecord] = []

            for claim in claims:
                if claim_role(claim) is not None:
                    # Never proven by symbol resolution: decided below, together.
                    pending.append(claim)
                    continue
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

            # Part of resolving claims, not a phase of its own: the pass keeps its
            # three computing stages and the one that commits. Template calls are
            # matched against what each repository's files serve on their own —
            # never this pass's previous output.
            served = self._knowledge.list_served_routes(exclude_parser=GLOBAL_PARSER)
            decided = plan_http_claims(
                pending,
                index,
                file_rows,
                producer=GLOBAL_PARSER,
                served_routes=served,
                module_facts=[claim for claim in recorded if _is_module_fact(claim)],
                route_servers=self._knowledge.list_route_servers(exclude_parser=GLOBAL_PARSER),
            )
            edges.extend(decided.edges)
            # The one HTTP decision that joins two repositories: exposure and request
            # claims, both per file or derived from them, never this pass's previous output.
            crossings = plan_http_crossings(
                [*pending, *decided.injected, *decided.exposed], file_rows, producer=GLOBAL_PARSER
            )
            edges.extend(crossings.edges)
            resolved += decided.resolved
            unresolved += decided.unresolved
            watch.candidates_out = resolved
            watch.detail = f"{unresolved} still unresolved; {decided.detail}; {crossings.detail}"
        stages.append(_timing(watch))

        entered("uses")
        with Stopwatch("uses") as watch:
            scope = (
                uses.scope()
                if uses is not None
                else stored_scope(self._knowledge) or UsesScope.everything("every use requested")
            )
            uses_plan = plan_uses(
                self._knowledge, index, records, producer=GLOBAL_PARSER, scope=scope
            )
            watch.candidates_in = uses_plan.work.claims_examined
            watch.candidates_out = uses_plan.work.claims_resolved
            watch.detail = _uses_detail(uses_plan)
        stages.append(_timing(watch))

        entered("dependencies")
        with Stopwatch("dependencies") as watch:
            dependencies, cross = self._plan_dependencies(index, records)
            watch.candidates_in = len(dependencies)
            watch.candidates_out = cross
            watch.detail = "cross-repository edges"
        stages.append(_timing(watch))

        return GlobalPlan(
            edges=tuple(edges),
            dependencies=tuple(dependencies),
            http=decided,
            http_crossings=crossings,
            uses=uses_plan,
            resolved=resolved,
            unresolved=unresolved,
            cross_repository=cross,
            claims_considered=len(claims) + uses_plan.work.claims_examined,
            files_indexed=len(file_rows),
            symbols_indexed=len(symbols),
            stages=tuple(stages),
        )

    def commit_global(self, plan: GlobalPlan) -> GlobalLinkResult:
        """Replace the global graph with ``plan``, atomically.

        Delete, intern, insert and collect, in **one transaction**. Another
        connection sees the previous complete graph or the new complete graph and
        nothing between them, because there is no committed state between them
        (ARCHITECTURE.md §9). Previously the delete committed on its own and the
        insert followed later, so any failure in the gap published a knowledge
        base with no cross-repository graph at all.

        The transaction stays short by construction: everything it does is bounded
        by the size of the graph, and all the resolution work happened in
        :meth:`prepare_global` before it opened. Interning is inside it because an
        edge to an entity written by a transaction that then rolls back would be an
        edge to nothing.
        """
        stages = list(plan.stages)
        edges = list(plan.edges)
        uses = plan.uses or UsesPlan(scope=UsesScope.everything("every use requested"))

        planned = len(edges) + len(plan.dependencies) + len(plan.http.http) + len(uses.edges)
        with Stopwatch("swap", candidates_in=planned) as watch:
            with self._knowledge.transaction():
                # USES edges outside the plan's files are kept; apply_uses replaces the rest.
                removed = self._knowledge.delete_edges_by_parser(GLOBAL_PARSER, keep=EdgeType.USES)
                edges.extend(self._materialize(plan.dependencies))
                edges.extend(self._materialize_http(plan.http))
                self._knowledge.add_edges(edges)
                applied = apply_uses(self._knowledge, uses, producer=GLOBAL_PARSER)
                self._knowledge.gc_orphan_entities()
            watch.candidates_out = len(edges) + len(uses.edges)
            watch.detail = f"{removed + applied.work.edges_deleted} replaced"
        stages.append(_timing(watch))

        # Every use counts, kept or resolved again: one edge per use that resolved.
        return GlobalLinkResult(
            edges=len(edges) + len(uses.edges),
            resolved=plan.resolved + applied.edges,
            unresolved=plan.unresolved + applied.claims - applied.edges,
            cross_repository=plan.cross_repository,
            trace=LinkTrace(
                duration_ms=round(sum(stage.duration_ms for stage in stages), 3),
                claims_considered=plan.claims_considered,
                files_indexed=plan.files_indexed,
                symbols_indexed=plan.symbols_indexed,
                stages=tuple(stages),
                uses=applied.work,
            ),
        )

    def _materialize(self, pending: Sequence[PendingEdge]) -> list[EdgeRecord]:
        """Intern the planned endpoints and turn the plan into real edges.

        Runs inside the swap transaction. Interning is get-or-create by natural
        key, so the same entity named by twenty declarations is one row.
        """
        interned: dict[str, int] = {}

        def entity_id(record: EntityRecord) -> int:
            found = interned.get(record.key)
            if found is None:
                found = self._knowledge.intern_entity(record)
                interned[record.key] = found
            return found

        return [
            EdgeRecord(
                src_id=entity_id(edge.source),
                dst_id=entity_id(edge.target),
                type=EdgeType.DEPENDS_ON,
                repo_id=edge.record.repo_id,
                file_id=edge.record.file_id,
                line_start=edge.record.line_start,
                line_end=edge.record.line_end,
                parser=GLOBAL_PARSER,
            )
            for edge in pending
        ]

    def _materialize_http(self, decided: HttpClaimPlan) -> list[EdgeRecord]:
        """Intern the shared HTTP nodes a pending fact proved, inside the swap.

        The target is re-checked against its canonical form here for the same reason
        :meth:`link_file` checks it: a shared node is where repositories meet, and a
        second spelling of one identity would be a node nothing else reaches.
        """
        found: list[EdgeRecord] = []
        for edge in decided.http:
            canonical = _HTTP_CANONICAL.get(edge.target.type)
            if canonical is None or canonical(edge.target.name) is None:
                continue
            found.append(
                EdgeRecord(
                    src_id=edge.src_id,
                    dst_id=self._knowledge.intern_entity(edge.target),
                    type=edge.type,
                    repo_id=edge.repo_id,
                    file_id=edge.file_id,
                    line_start=edge.line_start,
                    line_end=edge.line_end,
                    parser=GLOBAL_PARSER,
                )
            )
        return found

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
        package = meta.get("package")
        if package is not None:
            # A name the parser could only offer as a candidate: `helper` in
            # `from . import helper`. It becomes an edge when a module of that
            # name exists inside that package, and nothing otherwise.
            target = index.resolve_relative_submodule(
                origin, package, claim.target_key[len(package) :]
            )
        else:
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

    def _plan_dependencies(
        self, index: ResolutionIndex, records: Sequence[DependencyRecord]
    ) -> tuple[list[PendingEdge], int]:
        """Plan package and repository edges from declared dependencies.

        Writes nothing: entity *keys* are decided here and interned during the
        swap, so a failure while planning cannot leave entities behind for a graph
        that was never committed.

        A repository-to-repository edge is planned only on **exact normalized-name
        equality within one ecosystem**, and only when exactly one managed
        repository provides that name (V1_SPEC §9).
        """
        if self._dependencies is None:
            return [], 0

        # Counted as *relationships*, not as assertions. A monorepo whose root and
        # backend manifests both declare one shared package states the same
        # repository-to-repository fact twice, and each statement keeps its own
        # provenanced edge — but reporting "2 cross-repository links" would describe
        # the manifests rather than the graph (V1_SPEC §9).
        cross_pairs: set[tuple[int, int]] = set()
        planned: list[PendingEdge] = []

        def repository_entity(repo_id: int) -> EntityRecord | None:
            name = index.repo_names.get(repo_id)
            slug = index.repo_slugs.get(repo_id)
            if name is None or not slug:
                return None
            return EntityRecord(
                type=EntityType.REPOSITORY,
                key=repository_key(slug),
                name=name,
                qualified_name=slug,
                repo_id=repo_id,
            )

        for record in records:
            if record.role != "declared":
                continue
            source = repository_entity(record.repo_id)
            if source is None:
                continue

            planned.append(
                PendingEdge(
                    source=source,
                    target=EntityRecord(
                        type=EntityType.PACKAGE,
                        key=package_key(record.ecosystem, record.normalized_name),
                        name=record.name,
                        qualified_name=f"{record.ecosystem}:{record.normalized_name}",
                    ),
                    record=record,
                )
            )

            if record.ecosystem not in REPOSITORY_ECOSYSTEMS:
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
            target = repository_entity(providers[0][0])
            if target is None:
                continue
            planned.append(PendingEdge(source=source, target=target, record=record))
            cross_pairs.add((record.repo_id, providers[0][0]))

        return planned, len(cross_pairs)


@dataclass(frozen=True, slots=True)
class RepositoryHttp:
    """One repository's HTTP plan, with the inputs it was computed from."""

    plan: HttpClaimPlan
    files: list[FileRow]
    claims: list[UnresolvedRefRecord]
    index: ResolutionIndex


def plan_repository_http(knowledge: SqliteKnowledgeStore, repo_id: int) -> RepositoryHttp:
    """One repository's HTTP decisions, computed exactly as the global pass computes them.

    Every rule the global pass applies to HTTP claims is repository-local: module
    resolution stays inside the importing repository, markers are keyed by package
    or repository, a controller resolves in its own file, and template calls are
    matched only against their own repository's served routes. So planning one
    repository's claims alone reproduces its share of the global plan — the same
    function over the same inputs, narrowed by repository and nothing else. Only
    classes are loaded as symbols, because a class is the only symbol a decision
    reads.

    Writes nothing, and never reads a ``linker:global`` edge as an input.
    """
    files = knowledge.list_file_rows(repo_id)
    symbols = knowledge.list_symbols([EntityType.CLASS], repo_id)
    claims = [
        claim
        for claim in knowledge.list_unresolved_refs(repo_id, pending_only=True)
        if claim_role(claim) is not None
    ]
    served = knowledge.list_served_routes(exclude_parser=GLOBAL_PARSER, repo_id=repo_id)
    index = ResolutionIndex.build(files, symbols)
    own = {row.file_id for row in files}
    facts = [fact for fact in knowledge.list_module_facts() if fact.file_id in own]
    plan = plan_http_claims(
        claims, index, files, producer=GLOBAL_PARSER, served_routes=served, module_facts=facts
    )
    return RepositoryHttp(plan=plan, files=files, claims=claims, index=index)


def claim_role(claim: UnresolvedRefRecord) -> str | None:
    """The pending-fact role a claim carries, or ``None`` for an ordinary claim."""
    if not claim.meta_json:
        return None
    role = json.loads(claim.meta_json).get(PENDING_FACT)
    return role if isinstance(role, str) and role else None


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


def _is_module_fact(claim: UnresolvedRefRecord) -> bool:
    if not claim.meta_json or MODULE_RESOLUTION not in claim.meta_json:
        return False
    return bool(json.loads(claim.meta_json).get(MODULE_RESOLUTION))


def _uses_detail(plan: UsesPlan) -> str:
    work = plan.work
    if work.mode == "global":
        return f"global ({work.reason}): {work.files_invalidated} file(s)"
    return (
        f"incremental: {work.files_invalidated} file(s) -- {work.own} changed, "
        f"{work.landed} landed, {work.readers} readers, {work.repository} by repository, "
        f"{work.package} by package"
    )


def _timing(watch: Stopwatch) -> StageTiming:
    assert watch.recorded is not None
    return watch.recorded


def _unobserved(stage: str) -> None:
    """The stage listener of a pass nobody is following."""


def _flavor(origin: FileRow, meta: dict[str, str]) -> str | None:
    """Import flavor for a claim: from the parser's own hint, else the extension."""
    parser = meta.get("parser", "")
    if parser.startswith("python"):
        return "python"
    if "typescript" in parser:
        return "node"
    return flavor_for(origin.rel_path)
