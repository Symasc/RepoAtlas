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

import functools
import json
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

from repoatlas.config.settings import DECLARATIONS_SLUG, SaltSettings
from repoatlas.dependencies.normalize import REPOSITORY_ECOSYSTEMS
from repoatlas.graph.ci_facts import plan_ci_facts
from repoatlas.graph.deploy_artifacts import plan_deployment_artifacts
from repoatlas.graph.deploy_identity import DOCKERFILE_UNREAD, NO_BUILD, NOT_IN_IMAGE, ServedRoute
from repoatlas.graph.deployment_env import plan_deployment_env
from repoatlas.graph.deployment_targets import build_targets
from repoatlas.graph.fields import (
    FIELD_USE,
    Field,
    Model,
    field_meta,
    field_record,
    members_of,
    resolve_members,
)
from repoatlas.graph.graphql_artifacts import plan_artifacts
from repoatlas.graph.graphql_codegen import plan_codegen
from repoatlas.graph.graphql_documents import plan_documents
from repoatlas.graph.graphql_ownership import Identity, origin_of, plan_ownership
from repoatlas.graph.graphql_resolvers import plan_resolvers
from repoatlas.graph.graphql_schema import symbols_by_file
from repoatlas.graph.http_claims import HttpClaimPlan, plan_http_claims, served_by_servers
from repoatlas.graph.http_crossings import (
    HttpCrossingPlan,
    ScopedDeclarations,
    decision_rows,
    plan_http_crossings,
)
from repoatlas.graph.http_scopes import FileScope, ScopeSymbol
from repoatlas.graph.keys import file_key, global_key, package_key, repository_key, symbol_key
from repoatlas.graph.link_exposures import plan_link_exposures, plan_scoped_exposures
from repoatlas.graph.modules import ModuleResolver, declared_constraints
from repoatlas.graph.ordinary_outcomes import (
    Classifier,
    Decision,
    OutcomeLedger,
    OutcomePlan,
    finish_accounting,
    summarize,
)
from repoatlas.graph.persistence import plan_persistence
from repoatlas.graph.prisma_schema import plan_prisma, prisma_projects
from repoatlas.graph.resolution import ResolutionIndex, flavor_for
from repoatlas.graph.salt_pillar import resolve_pillar_reads
from repoatlas.graph.uses import table_from_meta, use_resolution
from repoatlas.graph.uses_link import (
    UsesChangeLog,
    UsesPlan,
    UsesScope,
    apply_uses,
    plan_uses,
    stored_scope,
)
from repoatlas.graph.value_mappings import plan_value_mappings
from repoatlas.models import http
from repoatlas.models.dependencies import DependencyRecord
from repoatlas.models.deployment_conditions import ConditionContext
from repoatlas.models.deployment_targets import DeploymentLink
from repoatlas.models.ordinary_outcomes import (
    DEPLOYMENT_FACT,
    EVIDENCE_UNREADABLE,
    GRAPHQL_PLANNER,
    HTTP_PLANNER,
    MODULE_FACT,
    ORIGIN_FILE_MISSING,
    PERSISTENCE_PLANNER,
    PILLAR_READ,
    SALT_MAPPING,
    OrdinarySummary,
)
from repoatlas.models.parsing import (
    EMBEDDED_DOCUMENT,
    ENUM_MEMBER,
    FIELD,
    EdgeType,
    EntityType,
    ParsedFile,
    ParsedReference,
    ParsedSymbol,
)
from repoatlas.models.telemetry import LinkTrace, StageTiming, Stopwatch
from repoatlas.parsers.ci_config import CI_ROLES
from repoatlas.parsers.graphql import GRAPHQL_DOCUMENT, GRAPHQL_FACTS
from repoatlas.parsers.graphql_artifacts import GRAPHQL_ARTIFACT
from repoatlas.parsers.graphql_codegen import GRAPHQL_CODEGEN_OUTPUT
from repoatlas.parsers.http_facts import (
    HELM_VALUES_FILE,
    K8S_CONFIGMAP,
    K8S_WORKLOAD,
    PENDING_FACT,
)
from repoatlas.parsers.module_facts import MODULE_RESOLUTION
from repoatlas.parsers.py_graphql import GRAPHQL_BINDABLE
from repoatlas.parsers.py_persistence import TYPED_COMPARISON
from repoatlas.parsers.ts_graphql import GRAPHQL_SCHEMA_CONSTRUCTION, TS_OBJECT_SHAPE
from repoatlas.parsers.ts_persistence import (
    PERSISTENCE_CLIENT,
    PERSISTENCE_QUERY,
    VALUE_MAPPING,
)
from repoatlas.storage.protocols import (
    EdgeRecord,
    EntityRecord,
    FileRow,
    LinkDecisionRow,
    SymbolRow,
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
    #: The ordinary-claim account of this pass, independent of ``unresolved``.
    ordinary: OrdinarySummary | None = None


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


def _deployment_links(
    links: Sequence[DeploymentLink],
    claims: Sequence[UnresolvedRefRecord],
    by_id: Mapping[int, FileRow],
) -> tuple[list[DeploymentLink], dict[int, str]]:
    """The links, and the URL each one naming a stated target resolves that target to
    under the link's own values -- as the deployment targets tool resolves it, with no
    branch or environment assumed. A link that resolves nothing adds no exposure."""
    urls: dict[int, str] = {}
    wanted = {link.repository for link in links if link.url is None and link.target_id}
    if not wanted:
        return list(links), urls
    by_repo: dict[str, list[UnresolvedRefRecord]] = defaultdict(list)
    declarations: list[UnresolvedRefRecord] = []
    for claim in claims:
        row = by_id.get(claim.file_id)
        if row is None:
            continue
        if row.repo_slug == DECLARATIONS_SLUG:
            declarations.append(claim)
        if row.repo_slug in wanted:
            by_repo[row.repo_slug].append(claim)
    for slug in sorted(wanted):
        files = {f: row for f, row in by_id.items() if row.repo_slug in (slug, DECLARATIONS_SLUG)}
        own = [link for link in links if link.repository == slug]
        targets, _ = build_targets(
            slug,
            by_repo.get(slug, []),
            files,
            (),
            own,
            ConditionContext(),
            declarations=declarations,
        )
        for target in targets:
            for resolution in target.resolutions:
                if resolution.link_id is not None and resolution.url:
                    urls[resolution.link_id] = resolution.url
    return list(links), urls


def _scoped_declarations(
    knowledge: SqliteKnowledgeStore,
    links: Sequence[DeploymentLink],
    routes: Sequence[ServedRoute],
    repo_ids: Mapping[str, int],
    symbols: Sequence[SymbolRow],
    by_id: Mapping[int, FileRow],
) -> ScopedDeclarations | None:
    """What the join needs for the scoped declarations: their exposures, and for each file one
    names what the file offers its scope -- its entities, and the ``USES`` pairs the per-file
    pass asserted there (the global pass's own are being replaced, so never read). ``None`` when
    no declaration has a service and a caller to join."""
    by_repo: dict[int, list[ServedRoute]] = defaultdict(list)
    for route in routes:
        by_repo[route.repo_id].append(route)
    exposures = plan_scoped_exposures(links, by_repo, repo_ids)
    if not exposures:
        return None
    named = {
        (link.callers[0], link.scope.path)
        for link in links
        if link.scope is not None and len(link.callers) == 1
    }
    file_ids = {
        (row.repo_slug, row.rel_path): row.file_id
        for row in by_id.values()
        if (row.repo_slug, row.rel_path) in named
    }
    wanted = set(file_ids.values())
    in_file: dict[int, list[SymbolRow]] = defaultdict(list)
    for symbol in symbols:
        if symbol.file_id in wanted:
            in_file[symbol.file_id].append(symbol)
    files: dict[tuple[str, str], FileScope] = {}
    for place, file_id in sorted(file_ids.items()):
        members = in_file.get(file_id, [])
        pairs = [
            (user, used)
            for user, used, asserted_in, producer in knowledge.list_uses_targets(
                [symbol.entity_id for symbol in members]
            )
            if asserted_in == file_id and producer != GLOBAL_PARSER
        ]
        files[place] = FileScope.build(
            (
                ScopeSymbol(s.entity_id, s.file_id, s.type.value, s.qualified_name)
                for s in members
                if s.qualified_name
            ),
            pairs,
        )
    return ScopedDeclarations(exposures, files)


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
    #: Every link family's decisions, replaced with the edges they account for.
    decisions: tuple[LinkDecisionRow, ...] = ()
    #: The ordinary-claim account, and the figures it is reconciled with (ordinary_outcomes).
    outcomes: OutcomePlan | None = None
    #: Each deployment link the join read, with the revision it read (a declaration changed
    #: since is pending until the next pass).
    link_reads: tuple[tuple[int, str], ...] = ()
    standing_claims: int = 0
    uses_rows: int = 0
    legacy_ordinary: int = 0
    legacy_graphql_unknown_role: int = 0
    legacy_planners: int = 0
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
        salt: SaltSettings | None = None,
    ) -> None:
        self._knowledge = knowledge
        self._dependencies = dependencies
        self._salt = salt or SaltSettings()

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
        documents: dict[str, int] = {}
        fields: list[Field] = []
        declared: dict[int, ParsedSymbol] = {}
        symbol_ids = self._symbol_ids(
            file_id,
            repo_slug,
            rel_path,
            parsed.symbols,
            qualified,
            values,
            documents,
            fields=fields,
            declared=declared,
        )
        python = parsed.language == "Python"
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
            for entity_id in (
                *symbol_ids.values(),
                *values.values(),
                *documents.values(),
                *(field.entity_id for field in fields),
            )
        ]

        claims: list[UnresolvedRefRecord] = []
        field_uses: set[tuple[int, int]] = set()
        for reference in parsed.references:
            source_id = sources.get(reference.source_symbol or "", file_entity_id)
            if reference.edge_type is EdgeType.READS and reference.meta.get("owner") == "value":
                # A module-scope read its value's statement holds (DF3a): that value, by its
                # exact name, never a same-named declaration; failing that, the file.
                source_id = values.get(reference.source_symbol or "", file_entity_id)
            if reference.meta.get("embedded") == "true":
                # A document embedded in another language owns its own claims, and only
                # them: its name may also be a declaration's of that language.
                source_id = documents.get(reference.source_symbol or "", source_id)

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
                        model = declared.get(target)
                        members = members_of(reference.meta)
                        if model is not None and members:
                            # The model's own fields the declaration reads or sets (DF3b).
                            for use in resolve_members(
                                _model(target, model), members, fields, python=python
                            ):
                                key = (source_id, use.field.entity_id)
                                if key in field_uses:
                                    continue
                                field_uses.add(key)
                                edges.append(
                                    EdgeRecord(
                                        src_id=source_id,
                                        dst_id=use.field.entity_id,
                                        type=EdgeType.USES,
                                        confidence=reference.confidence,
                                        repo_id=repo_id,
                                        file_id=file_id,
                                        line_start=use.line,
                                        line_end=use.line,
                                        parser=parsed.parser,
                                        resolution=use_resolution(
                                            field_meta(reference.meta, use),
                                            **{FIELD_USE: field_record(use, reference.meta)},
                                        ),
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
        documents: dict[str, int] | None = None,
        *,
        fields: list[Field] | None = None,
        declared: dict[int, ParsedSymbol] | None = None,
    ) -> dict[str, int]:
        """Map qualified symbol name to its persisted entity id.

        ``qualified``, when given, receives the qualified names alone, without the
        bare-name fallback below. A module value (``ParsedSymbol.uses_only``) is never
        in the mapping: it goes to ``qualified`` and to ``values``, when given. A GraphQL
        document embedded in another language is in none of them, only in ``documents``.
        A model field is in none of them either, only in ``fields``: no name resolves to
        it, and it owns nothing (DF3b). ``declared`` receives every other symbol by id.
        """
        mapping: dict[str, int] = {}
        known = self._knowledge.file_entity_ids(file_id) if symbols else {}
        for symbol in symbols:
            key = symbol_key(repo_slug, rel_path, symbol.qualified_name, symbol.line_start)
            entity_id = known.get(key)
            if entity_id is not None:
                if symbol.meta.get("kind") == FIELD:
                    if fields is not None:
                        fields.append(
                            Field(
                                entity_id=entity_id,
                                name=symbol.name,
                                qualified_name=symbol.qualified_name,
                                line_start=symbol.line_start,
                                init=symbol.meta.get("init"),
                            )
                        )
                    continue
                if declared is not None:
                    declared[entity_id] = symbol
                if symbol.meta.get("kind") == EMBEDDED_DOCUMENT:
                    # No name of the host language resolves to it, and it owns nothing
                    # but its own claims (link_file).
                    if documents is not None:
                        documents[symbol.qualified_name] = entity_id
                    continue
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
                # An enum member is reached through its enum in the same way.
                if symbol.meta.get("kind") not in {"property", ENUM_MEMBER}:
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
        classify: bool = False,
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

        ``classify`` asks for the ordinary-claim account (repoatlas.graph.ordinary_outcomes):
        what became of each ordinary claim and why. It is a diagnostic, kept out of an
        ordinary pass because it costs time every sync would pay; without it none of that
        work is done and the plan carries no account. It never changes an edge.
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
            claims: list[UnresolvedRefRecord] = []
            module_facts: list[UnresolvedRefRecord] = []
            for claim in recorded:
                (module_facts if _is_module_fact(claim) else claims).append(claim)
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
            generation: list[UnresolvedRefRecord] = []
            documents_claims: list[UnresolvedRefRecord] = []
            resolver_claims: list[UnresolvedRefRecord] = []
            artifact_claims: list[UnresolvedRefRecord] = []
            persistence_claims: list[UnresolvedRefRecord] = []
            salt_resolutions = {
                id(result.claim): result
                for result in resolve_pillar_reads(self._salt, index, claims, by_id)
            }
            salt_crossings: set[tuple[int, int]] = set()
            # The ordinary-claim account (repoatlas.graph.ordinary_outcomes): counted beside the
            # resolution below and never consulted by it. A claim is classified only after
            # `_resolve` has returned nothing for it. Opt-in (`classify`): an ordinary pass keeps
            # none of it, so it does none of the work.
            # Bare specifiers an ordinary import names through configuration: read from the same
            # indexed facts, with the importer's declarations, exactly as the uses stage reads them.
            modules = ModuleResolver(
                index, module_facts, declared=declared_constraints(index, records)
            )
            ledger = OutcomeLedger() if classify else None
            classifier = Classifier(index, records, module_facts, modules) if classify else None
            languages: dict[int, str] = {}
            graphql_unknown_role = 0
            ordinary_unresolved = 0
            if ledger is not None:
                for fact in module_facts:
                    ledger.excluded(fact, MODULE_FACT)

            for claim in claims:
                if _is_salt_mapping_claim(claim):
                    if ledger is not None:
                        ledger.excluded(claim, SALT_MAPPING)
                    continue
                if _is_graphql_claim(claim):
                    # A configured output is decided by the codegen pass below. A type or
                    # document claim stays a claim: one file is no schema, and nothing yet
                    # decides which schema it belongs to. Neither is resolved by name, and
                    # neither is ever handed to the HTTP pass.
                    if ledger is not None:
                        ledger.excluded(claim, GRAPHQL_PLANNER)
                    role = claim_role(claim)
                    if role == GRAPHQL_CODEGEN_OUTPUT:
                        generation.append(claim)
                    elif role == GRAPHQL_DOCUMENT:
                        documents_claims.append(claim)
                    elif role in _RESOLVER_FACTS:
                        resolver_claims.append(claim)
                    elif role == GRAPHQL_ARTIFACT:
                        artifact_claims.append(claim)
                    else:
                        unresolved += 1
                        graphql_unknown_role += 1
                    continue
                if claim_role(claim) in _DEPLOYMENT_FACT_ROLES:
                    # CI, workload, ConfigMap and values-file facts: decided by the CI pass
                    # below (repoatlas.graph.ci_facts), and joined from DF2 -- never handed
                    # to the HTTP pass, whose accounting they must not change.
                    if ledger is not None:
                        ledger.excluded(claim, DEPLOYMENT_FACT)
                    continue
                if claim_role(claim) in _PERSISTENCE_ROLES:
                    # Decided after uses resolve: a mapping's types are what it uses.
                    if ledger is not None:
                        ledger.excluded(claim, PERSISTENCE_PLANNER)
                    persistence_claims.append(claim)
                    continue
                if claim_role(claim) is not None:
                    # Never proven by symbol resolution: decided below, together.
                    if ledger is not None:
                        ledger.excluded(claim, HTTP_PLANNER)
                    pending.append(claim)
                    continue
                origin = by_id.get(claim.file_id)
                if origin is None:
                    unresolved += 1
                    ordinary_unresolved += 1
                    if ledger is not None:
                        ledger.refused(
                            claim,
                            None,
                            "other",
                            Decision(EVIDENCE_UNREADABLE, ORIGIN_FILE_MISSING, "claim", "file-row"),
                        )
                    continue
                language = ""
                if ledger is not None:
                    language = languages.get(origin.file_id) or languages.setdefault(
                        origin.file_id, flavor_for(origin.rel_path) or "other"
                    )
                salt_resolution = salt_resolutions.get(id(claim))
                if salt_resolution is not None:
                    definition = salt_resolution.definition
                    if definition.repo_id == origin.repo_id:
                        edges.append(self._edge(claim, origin, definition.entity_id))
                    else:
                        edges.append(
                            EdgeRecord(
                                src_id=definition.entity_id,
                                dst_id=origin.file_entity_id,
                                type=EdgeType.GENERATES,
                                confidence=claim.confidence,
                                repo_id=origin.repo_id,
                                file_id=origin.file_id,
                                line_start=claim.line_start,
                                line_end=claim.line_end,
                                parser=GLOBAL_PARSER,
                                resolution=salt_resolution.resolution,
                            )
                        )
                        salt_crossings.add((definition.repo_id, origin.repo_id))
                    resolved += 1
                    if ledger is not None:
                        ledger.linked(claim, language, PILLAR_READ)
                    continue
                siblings = by_file.get(claim.file_id, [])
                edge = self._resolve(claim, origin, index, siblings, modules)
                if edge is None:
                    unresolved += 1
                    ordinary_unresolved += 1
                    if ledger is not None and classifier is not None:
                        meta = _claim_meta(claim.meta_json)
                        ledger.refused(
                            claim,
                            origin,
                            language,
                            classifier.classify(
                                claim, origin, meta, _flavor(origin, meta), siblings
                            ),
                        )
                    continue
                resolved += 1
                if ledger is not None:
                    ledger.linked(claim, language)
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
                module_facts=module_facts,
                route_servers=self._knowledge.list_route_servers(exclude_parser=GLOBAL_PARSER),
                export_tables=_export_tables(self._knowledge),
            )
            edges.extend(decided.edges)
            # What CI configurations configure, and every step not interpreted
            # (repoatlas.graph.ci_facts); then the releases they establish, rendered and
            # joined to the builds configured to publish their images -- the one place that
            # join is made (repoatlas.graph.deploy_artifacts, DF2_PHASE_A.md §15 E7).
            ci = plan_ci_facts(claims, by_id)
            deployed = plan_deployment_artifacts(claims, by_id, decided.code_routes, ci)
            # Where those deployments, and compose services building their own images, set the
            # variables the code in each image reads: a crossing from the effective setter to
            # each reader, never by a variable's name (repoatlas.graph.deployment_env, DF3a).
            environment = plan_deployment_env(
                claims,
                by_id,
                symbols,
                deployed.containers,
                self._knowledge.list_env_reads(exclude_parser=GLOBAL_PARSER),
                producer=GLOBAL_PARSER,
            )
            edges.extend(environment.edges)
            # Where users say repositories are deployed: exposures the join falls back on
            # for a request the source leaves unlinked, never weighed against the source
            # (repoatlas.graph.link_exposures).
            read_links = self._knowledge.list_deployment_links()
            repo_ids = {row.repo_slug: row.repo_id for row in by_id.values()}
            url_links = [link for link in read_links if link.scope is None]
            stated_links = plan_link_exposures(
                *_deployment_links(url_links, claims, by_id), decided.code_routes, repo_ids
            )
            # A scoped declaration states no URL: it joins only a request its own words cover
            # (repoatlas.graph.http_scopes), through the routes of the repository it names.
            scopes = _scoped_declarations(
                self._knowledge,
                [link for link in read_links if link.scope is not None],
                decided.code_routes,
                repo_ids,
                symbols,
                by_id,
            )
            # The one HTTP decision that joins two repositories: exposure and request
            # claims, both per file or derived from them, never this pass's previous output.
            crossings = plan_http_crossings(
                [
                    *pending,
                    *decided.injected,
                    *decided.exposed,
                    *decided.identities,
                    *deployed.exposures,
                    *deployed.identities,
                    *stated_links,
                ],
                file_rows,
                producer=GLOBAL_PARSER,
                handler_key=_handler_keys(symbols, by_id),
                scopes=scopes,
            )
            edges.extend(crossings.edges)
            # Generated declarations, from the schema files their configuration loads
            # (repoatlas.graph.graphql_codegen); never from a definition sharing a name.
            codegen = plan_codegen(generation, index, by_id, symbols, producer=GLOBAL_PARSER)
            # Documents bound to the schema their configuration validates them against, and
            # resolvers to the schema their construction loads (repoatlas.graph.graphql_*).
            ordered_symbols = symbols_by_file(symbols)
            documents = plan_documents(
                documents_claims,
                codegen.projects,
                index,
                by_id,
                ordered_symbols,
                producer=GLOBAL_PARSER,
            )
            resolvers = plan_resolvers(
                resolver_claims, index, by_id, ordered_symbols, producer=GLOBAL_PARSER
            )
            # Federated schema copies tied to the subgraph each coordinate comes from, through
            # the deployment that serves the subgraph's URL (repoatlas.graph.graphql_ownership).
            artifacts = plan_artifacts(artifact_claims, index, by_id)
            ownership = plan_ownership(
                by_id,
                ordered_symbols,
                _deployment_origins((*decided.exposed, *decided.identities)),
                resolvers.served,
                artifacts.fetched,
                producer=GLOBAL_PARSER,
            )
            edges.extend((*codegen.edges, *documents.edges, *resolvers.edges, *ownership.edges))
            resolved += (
                decided.resolved + codegen.resolved + documents.resolved + resolvers.resolved
            )
            planners_unresolved = (
                decided.unresolved
                + codegen.unresolved
                + documents.unresolved
                + resolvers.unresolved
            )
            unresolved += planners_unresolved
            watch.candidates_out = resolved
            watch.detail = (
                f"{unresolved} still unresolved; {decided.detail}; {crossings.detail}; "
                f"{codegen.detail}; {documents.detail}; {resolvers.detail}; {ownership.detail}; "
                f"{artifacts.detail}; {ci.detail}; {deployed.detail}; {environment.detail}"
            )
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

        entered("persistence")
        with Stopwatch("persistence") as watch:
            # Prisma projects, their migrations and client; queries bound to the database a
            # source-backed identity names; typed mappings and comparisons, whose types are
            # what each declaration uses (repoatlas.graph.prisma_schema, .persistence,
            # .value_mappings). Computed whole every pass, from claims and effective uses.
            projects = prisma_projects(by_id, ordered_symbols)
            specifiers = sorted({s for project in projects for s in project.specifiers})
            prisma = plan_prisma(
                projects,
                self._knowledge.list_use_claims_from(specifiers) if specifiers else [],
                by_id,
                ordered_symbols,
                producer=GLOBAL_PARSER,
            )
            roles = {id(claim): claim_role(claim) for claim in persistence_claims}
            persistence = plan_persistence(
                [c for c in persistence_claims if roles[id(c)] == PERSISTENCE_CLIENT],
                [c for c in persistence_claims if roles[id(c)] == PERSISTENCE_QUERY],
                projects,
                index,
                by_id,
                producer=GLOBAL_PARSER,
            )
            mappings = plan_value_mappings(
                [
                    c
                    for c in persistence_claims
                    if roles[id(c)] in {VALUE_MAPPING, TYPED_COMPARISON}
                ],
                _effective_uses(self._knowledge, uses_plan),
                {symbol.entity_id: symbol for symbol in symbols},
                ordered_symbols,
                prisma.bound,
                by_id,
                producer=GLOBAL_PARSER,
            )
            edges.extend((*prisma.edges, *persistence.edges, *mappings.edges))
            watch.candidates_in = len(persistence_claims)
            watch.candidates_out = len(prisma.edges) + len(persistence.edges) + len(mappings.edges)
            watch.detail = f"{prisma.detail}; {persistence.detail}; {mappings.detail}"
        stages.append(_timing(watch))

        entered("dependencies")
        with Stopwatch("dependencies") as watch:
            dependencies, cross = self._plan_dependencies(index, records)
            watch.candidates_in = len(dependencies)
            watch.candidates_out = cross
            watch.detail = "cross-repository edges"
        stages.append(_timing(watch))

        outcome_plan = ledger.plan() if ledger is not None else None
        standing_claims, uses_rows = (
            self._knowledge.count_standing_claims() if ledger is not None else (0, 0)
        )

        return GlobalPlan(
            edges=tuple(edges),
            dependencies=tuple(dependencies),
            http=decided,
            http_crossings=crossings,
            uses=uses_plan,
            decisions=(
                *(outcome_plan.decisions if outcome_plan is not None else ()),
                *codegen.decisions,
                *documents.decisions,
                *resolvers.decisions,
                *ownership.decisions,
                *artifacts.decisions,
                *prisma.decisions,
                *persistence.decisions,
                *mappings.decisions,
                *ci.decisions,
                *deployed.decisions,
                *environment.decisions,
            ),
            outcomes=outcome_plan,
            link_reads=tuple((link.id, link.updated_at.isoformat()) for link in read_links),
            standing_claims=standing_claims,
            uses_rows=uses_rows,
            legacy_ordinary=ordinary_unresolved,
            legacy_graphql_unknown_role=graphql_unknown_role,
            legacy_planners=planners_unresolved,
            resolved=resolved,
            unresolved=unresolved,
            cross_repository=cross
            + len(salt_crossings)
            + codegen.crossings
            + documents.crossings
            + resolvers.crossings
            + ownership.crossings
            + prisma.crossings
            + persistence.crossings
            + mappings.crossings,
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
                # Every request's decision, links and refusals alike, replaced with the
                # edges they account for.
                self._knowledge.replace_http_call_decisions(decision_rows(plan.http_crossings))
                self._knowledge.replace_link_decisions(plan.decisions)
                self._knowledge.record_link_pass(plan.link_reads)
                applied = apply_uses(self._knowledge, uses, producer=GLOBAL_PARSER)
                ordinary = self._ordinary_account(plan, applied.claims - applied.edges)
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
            ordinary=ordinary,
            trace=LinkTrace(
                duration_ms=round(sum(stage.duration_ms for stage in stages), 3),
                claims_considered=plan.claims_considered,
                files_indexed=plan.files_indexed,
                symbols_indexed=plan.symbols_indexed,
                stages=tuple(stages),
                uses=applied.work,
            ),
        )

    def _ordinary_account(self, plan: GlobalPlan, uses_remainder: int) -> OrdinarySummary | None:
        """Store the ordinary-claim account with the graph it accounts for.

        Runs inside the swap transaction, after the USES stage has said how many uses it left
        open. The account is its own model: the legacy ``unresolved`` scalar is neither an
        input nor something this makes equal, and the parts of it are recorded so their sum can
        be compared with it rather than assumed.
        """
        if plan.outcomes is None:
            # An ordinary pass computed no account, and the one a previous pass stored
            # describes a graph this commit is about to replace. Clearing it here, in the
            # same transaction, is what keeps a stored account from outliving its graph:
            # the tables hold this pass's account or nothing. (Its per-claim rows are
            # `link_decisions`, which every commit replaces whole.)
            self._knowledge.replace_ordinary_outcomes((), {})
            return None
        accounting = finish_accounting(
            plan.outcomes,
            standing=plan.standing_claims,
            uses_rows=plan.uses_rows,
            plan_unresolved=plan.unresolved,
            legacy_ordinary=plan.legacy_ordinary,
            graphql_unknown_role=plan.legacy_graphql_unknown_role,
            planners=plan.legacy_planners,
            uses_remainder=uses_remainder,
        )
        self._knowledge.replace_ordinary_outcomes(plan.outcomes.rows, accounting)
        return summarize(plan.outcomes, accounting)

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
        modules: ModuleResolver | None = None,
    ) -> EdgeRecord | None:
        meta = json.loads(claim.meta_json) if claim.meta_json else {}
        kind = claim.target_kind

        if kind is EntityType.MODULE:
            return self._resolve_module(claim, origin, index, meta, modules)
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
        modules: ModuleResolver | None = None,
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
            if target is None and modules is not None and flavor == "node":
                return self._configured_import(claim, origin, modules)
        return self._edge(claim, origin, target.file_entity_id) if target else None

    def _configured_import(
        self, claim: UnresolvedRefRecord, origin: FileRow, modules: ModuleResolver
    ) -> EdgeRecord | None:
        """A bare specifier a tsconfig ``paths`` pattern, a workspace member or a package entry
        names: the same resolution the declaration uses read (:class:`ModuleResolver`), from
        indexed facts only, and only when it leads to **one** file of the importer's own
        repository. A relative path was decided above; a duplicate provider, conflicting entry
        conditions, an unindexed target, a path out of the repository or a declaration naming
        another source is no target, and the claim stays a claim. The edge says which of these
        mapped it, as the declaration-use edge does."""
        specifier = claim.target_key
        if specifier.startswith((".", "/", "node:")):
            return None
        found = modules.resolve(origin, specifier)
        if found is None or found.file.repo_id != origin.repo_id:
            return None
        return self._edge(
            claim,
            origin,
            found.file.file_entity_id,
            resolution=json.dumps(
                {"basis": found.basis, "specifier": specifier, **dict(found.detail)},
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

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
        resolution: str | None = None,
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
            resolution=resolution,
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


def _export_tables(knowledge: SqliteKnowledgeStore) -> Callable[[int], Mapping[str, object] | None]:
    """A file's stored export table (``None``: none), read once per file, for the planner's
    two-level mounted children (R2c-2). Read on demand: a plan without such a child reads none."""
    cache: dict[int, Mapping[str, object] | None] = {}

    def table(file_id: int) -> Mapping[str, object] | None:
        if file_id not in cache:
            raw = knowledge.list_export_tables([file_id]).get(file_id)
            cache[file_id] = table_from_meta(raw) if raw else None
        return cache[file_id]

    return table


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
        and not _is_salt_mapping_claim(claim)
        and not _is_graphql_claim(claim)
        and claim_role(claim) not in _PERSISTENCE_ROLES
    ]
    served = knowledge.list_served_routes(exclude_parser=GLOBAL_PARSER, repo_id=repo_id)
    index = ResolutionIndex.build(files, symbols)
    own = {row.file_id for row in files}
    facts = [fact for fact in knowledge.list_module_facts() if fact.file_id in own]
    plan = plan_http_claims(
        claims,
        index,
        files,
        producer=GLOBAL_PARSER,
        served_routes=served,
        module_facts=facts,
        export_tables=_export_tables(knowledge),
    )
    return RepositoryHttp(plan=plan, files=files, claims=claims, index=index)


def repository_routes(knowledge: SqliteKnowledgeStore, repo_id: int) -> list[ServedRoute]:
    """Every route a repository's code serves, as the global pass sees them: the routes its files'
    own edges state, and those the plan promoted from claims. What an operator's declaration is
    matched against."""
    servers = knowledge.list_route_servers(exclude_parser=GLOBAL_PARSER, repo_id=repo_id)
    seen: set[tuple[int, str]] = set()
    found: list[ServedRoute] = []
    for route in (
        *served_by_servers(servers),
        *plan_repository_http(knowledge, repo_id).plan.code_routes,
    ):
        if (route.src_id, route.body) not in seen:
            seen.add((route.src_id, route.body))
            found.append(route)
    return found


def repository_http_with_routes(
    knowledge: SqliteKnowledgeStore, repo_id: int
) -> tuple[RepositoryHttp, list[ServedRoute]]:
    """One repository's plan, and the routes :func:`repository_routes` gives, from that one plan.

    The same logic as :func:`repository_routes` (which stays as it is): for a reader that also
    needs the plan the routes were made from, so the repository is planned once."""
    servers = knowledge.list_route_servers(exclude_parser=GLOBAL_PARSER, repo_id=repo_id)
    planned = plan_repository_http(knowledge, repo_id)
    seen: set[tuple[int, str]] = set()
    found: list[ServedRoute] = []
    for route in (*served_by_servers(servers), *planned.plan.code_routes):
        if (route.src_id, route.body) not in seen:
            seen.add((route.src_id, route.body))
            found.append(route)
    return planned, found


def claim_role(claim: UnresolvedRefRecord) -> str | None:
    """The pending-fact role a claim carries, or ``None`` for an ordinary claim."""
    return _role_of(claim.meta_json) if claim.meta_json else None


@functools.lru_cache(maxsize=4096)
def _role_of(meta_json: str) -> str | None:
    """``claim_role`` of a claim's metadata. The global pass asks it of every claim
    several times in a row, and decoding the JSON each time was a third of the pass;
    a role is a string, so nothing a caller does can change what the cache holds."""
    role = json.loads(meta_json).get(PENDING_FACT)
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


def _is_salt_mapping_claim(claim: UnresolvedRefRecord) -> bool:
    if not claim.meta_json:
        return False
    return json.loads(claim.meta_json).get("salt_fact") in {"master_root", "top_entry"}


def _deployment_origins(claims: Sequence[UnresolvedRefRecord]) -> dict[str, dict[int, Identity]]:
    """Origin -> the repositories deployment identity ties it to, each with where the tie is
    stated: from the exposure and unsourced-identity claims the HTTP plan derived."""
    found: dict[str, dict[int, Identity]] = defaultdict(dict)
    for claim in claims:
        meta = json.loads(claim.meta_json or "{}")
        origin = origin_of(str(meta.get("deployment") or ""))
        if origin is None:
            continue
        stated = " via ".join(
            str(meta[key]) for key in ("identity_in", "bound_in") if meta.get(key)
        )
        runs_code = meta.get("source_outcome") not in _BUILT_ELSEWHERE
        known = found[origin].get(claim.repo_id)
        if known is None or (runs_code and not known.runs_code):
            found[origin][claim.repo_id] = Identity(stated, runs_code)
    return {origin: dict(sorted(repos.items())) for origin, repos in sorted(found.items())}


#: Identity outcomes that say the service's image is not built from its repository's code.
_BUILT_ELSEWHERE = frozenset({NO_BUILD, DOCKERFILE_UNREAD, NOT_IN_IMAGE})


def _is_graphql_claim(claim: UnresolvedRefRecord) -> bool:
    """A GraphQL type, document or codegen-output claim (repoatlas.parsers.graphql,
    repoatlas.parsers.graphql_codegen): never an HTTP fact, never resolved by name."""
    return claim_role(claim) in _GRAPHQL_ROLES


#: Facts a resolver map is followed through, and the constructions that use them.
_RESOLVER_FACTS = frozenset({GRAPHQL_SCHEMA_CONSTRUCTION, TS_OBJECT_SHAPE, GRAPHQL_BINDABLE})
#: DF1's deployment facts (repoatlas.parsers.ci_config, repoatlas.parsers.deploy_helm).
_DEPLOYMENT_FACT_ROLES = CI_ROLES | {K8S_WORKLOAD, K8S_CONFIGMAP, HELM_VALUES_FILE}
_GRAPHQL_ROLES = GRAPHQL_FACTS | {GRAPHQL_CODEGEN_OUTPUT, GRAPHQL_ARTIFACT} | _RESOLVER_FACTS
#: Database clients, queries, typed mappings and comparisons: decided after uses resolve.
_PERSISTENCE_ROLES = frozenset(
    {PERSISTENCE_CLIENT, PERSISTENCE_QUERY, VALUE_MAPPING, TYPED_COMPARISON}
)


def _effective_uses(knowledge: SqliteKnowledgeStore, plan: UsesPlan) -> Callable[[int], list[int]]:
    """A declaration's ``USES`` targets once ``plan`` is applied: the per-file pass's own
    edges, the global pass's stored edges of the files the plan leaves alone, and the plan's.
    Read lazily, one declaration at a time."""
    planned: dict[int, set[int]] = defaultdict(set)
    for edge in plan.edges:
        planned[edge.src_id].add(edge.dst_id)
    scope = plan.scope.files
    cache: dict[int, list[int]] = {}

    def uses_of(src_id: int) -> list[int]:
        if src_id not in cache:
            kept = {
                dst
                for _, dst, file_id, producer in knowledge.list_uses_targets([src_id])
                if producer != GLOBAL_PARSER or (scope is not None and file_id not in scope)
            }
            cache[src_id] = sorted(kept | planned[src_id])
        return cache[src_id]

    return uses_of


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


@functools.lru_cache(maxsize=65536)
def _decoded_meta(meta_json: str) -> Mapping[str, str]:
    """A claim's metadata, decoded once per distinct text. Callers only read it: the
    outcome account never changes a claim, and the many claims of one import share a text."""
    return MappingProxyType(json.loads(meta_json))


def _claim_meta(meta_json: str | None) -> Mapping[str, str]:
    return _decoded_meta(meta_json) if meta_json else MappingProxyType({})


def _flavor(origin: FileRow, meta: Mapping[str, str]) -> str | None:
    """Import flavor for a claim: from the parser's own hint, else the extension."""
    parser = meta.get("parser", "")
    if parser.startswith("python"):
        return "python"
    if "typescript" in parser:
        return "node"
    return flavor_for(origin.rel_path)


def _model(entity_id: int, symbol: ParsedSymbol) -> Model:
    """A symbol this file declares, as the model a local use names (DF3b)."""
    return Model(
        entity_id=entity_id,
        type=symbol.kind,
        kind=symbol.meta.get("kind"),
        qualified_name=symbol.qualified_name,
        line_start=symbol.line_start,
        line_end=symbol.line_end,
    )


def _handler_keys(
    symbols: Sequence[SymbolRow], by_id: Mapping[int, FileRow]
) -> Callable[[int], str | None]:
    """A declaration's natural key by its entity id: how a refusal names the handlers its link
    would reach (DF3c). A file serving a route is no declaration, and names none: a route
    registered at module scope is served by its file, whose key is no symbol's."""
    rows = {symbol.entity_id: symbol for symbol in symbols if symbol.type is not EntityType.FILE}

    def key(entity_id: int) -> str | None:
        symbol = rows.get(entity_id)
        row = by_id.get(symbol.file_id) if symbol is not None else None
        if symbol is None or row is None or not symbol.qualified_name:
            return None
        return symbol_key(row.repo_slug, row.rel_path, symbol.qualified_name, symbol.line_start)

    return key
