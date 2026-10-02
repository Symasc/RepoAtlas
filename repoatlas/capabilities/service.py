"""The capability service: one long-lived, transport-neutral application boundary.

Everything above this module is a transport. Everything below it is V1. The
service is what makes those two facts compatible, and it exists because V1's
shape and an agent server's shape disagree in three specific ways.

**V1 is a process per question; this is a process per session.** A CLI command
opens a workspace, answers, and exits, so nothing it holds has to survive
anything. A capability server answers many questions over a long life, so the
workspace is opened once and reused, and the thing that opens it also has to know
how to close it exactly once.

**V1 knows where the user is standing; this knows nothing.** A CLI may read the
working directory and be right, because the user chose it. This process was
started by a tool launcher in whatever directory that launcher happened to use.
So scope arrives in the request, every time, and there is no constructor argument
and no attribute here that could later be mistaken for an ambient default.

**V1 is single-threaded by construction; this is not, and must behave as if it
were.** One command, one connection, one thread. That assumption is load-bearing
all the way down to the database handle, so every public operation is serialized
against one lock rather than the assumption being quietly dropped.

What the lock does and does not buy
-----------------------------------
It guarantees **mutual exclusion**: at most one operation is inside this service
at a time, and it is taken before anything reaches the workspace rather than
beside it. It does not make the service **thread-portable**. V1 opens its SQLite
connection bound to the thread that created it, so a call arriving on a different
thread is refused by the driver whether or not it won the lock. Drive one service
from one thread; the lock is what makes that assumption safe to rely on rather
than merely hoped for. Making the connection movable is a V1 change and is
deliberately not made here.

What this module deliberately is not
------------------------------------
It is not a god object. It holds no parsing, no ranking and no freshness logic;
each capability is a short composition over code that already exists and is
already tested below it. If a method here grows an algorithm, the algorithm is in
the wrong place.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

from repoatlas.capabilities import http_inventory
from repoatlas.capabilities.budget import (
    BudgetCeiling,
    apply_budget_policy,
    ceiling_for,
    retrieval_limits_for,
)
from repoatlas.capabilities.context import resolve_context, to_repository_context
from repoatlas.capabilities.dependencies import (
    ProviderCandidate,
    admitted_roles,
    declares_package,
    encode_cursor,
    inventory_digest,
    package_key,
    page_limits_for,
    provider_index,
    provider_resolution,
    request_digest,
    resume,
    selects,
    store_slug,
    validate_dependency_request,
)
from repoatlas.capabilities.errors import (
    EdgeNotFoundError,
    EntityAmbiguousError,
    EntityNotFoundError,
    EntityNotInspectableError,
    EvidenceNotFoundError,
    FileNotIndexedError,
    IndexChangedError,
    IndexInconsistencyError,
    InvalidTopologyRequestError,
    InvestigationRequiredError,
    PathNotFoundError,
    ServiceClosedError,
    SyncFailedError,
    TargetOutOfScopeError,
    capability_error,
)
from repoatlas.capabilities.evidence_shaping import LINE_SEPARATOR
from repoatlas.capabilities.impact import POLICY as IMPACT_POLICY
from repoatlas.capabilities.impact import (
    Assembled,
    SeedResolver,
    assemble,
    http_crossing,
    package_crossing,
    package_dependents,
    seed_nodes,
)
from repoatlas.capabilities.indexed_source import WHOLE_FILE_END, indexed_line_map
from repoatlas.capabilities.inspection import (
    ShapedInspection,
    clamp,
    pad,
    shape_inspection,
    worktree_line_map,
)
from repoatlas.capabilities.relationships import (
    EDGE_DIRECTIONS,
    ProvenanceKey,
    ProvenanceSupply,
    admitted_edge_types,
    distinct_provenance,
    edge_order,
    edge_provenance,
    entity_ref,
    graph_limits_for,
    path_nodes,
    provenance_key,
)
from repoatlas.capabilities.search import ShapedEvidence, ShapingResult, shape_candidates
from repoatlas.capabilities.session import (
    DEFAULT_IDLE_TTL_SECONDS,
    DEFAULT_MAX_SESSIONS,
    InvestigationSession,
    SessionRegistry,
)
from repoatlas.capabilities.source_previews import preview
from repoatlas.capabilities.sync import (
    attribute_reports,
    relative_unreadable,
    repository_view,
    revalidation_failed,
    revalidation_summary,
    sync_status,
)
from repoatlas.capabilities.topology import (
    ROW_CEILING,
    STRUCTURAL,
    ChildScope,
    ambiguous_dependencies,
    bound_child_links,
    child_groups,
    child_links,
    context_view,
    crossing_groups,
    decode_children_cursor,
    decode_edge_ref,
    decoded_resolution,
    directory_entries,
    encode_children_cursor,
    encode_edge_ref,
    file_entries,
    file_matches,
    line_references,
    matching_edges,
    name_matches,
    page_limits,
    page_of,
    peer_repositories,
    query_terms,
    referenced_repositories,
    repository_links,
    with_declaration_counts,
)
from repoatlas.capabilities.verification import (
    CitationOutcome,
    aligned_hypotheses,
    projected_answer,
    resolve_citation,
    sanitized_answer,
    supplied_routes,
    supplied_set,
    validate_grounding_request,
    verdict_for,
)
from repoatlas.config.settings import Settings
from repoatlas.containment import RepositoryBoundary
from repoatlas.discovery.reconcile import RepositorySetPlan
from repoatlas.errors import (
    ContextFailure,
    ContextResolutionError,
    NotInitializedError,
    SyncInProgressError,
)
from repoatlas.freshness import index_freshness
from repoatlas.graph.declared import root_path
from repoatlas.graph.impact import ImpactWalk
from repoatlas.graph.keys import is_positional
from repoatlas.graph.linker import plan_repository_http
from repoatlas.graph.traversal import CROSS_REPOSITORY_EDGES, WalkResult, WalkStep, walk
from repoatlas.graph.units import package_directories, unit_of
from repoatlas.grounding import GroundingReport, ground_answer
from repoatlas.indexing.chunking import chunk_hash
from repoatlas.models.capability import (
    AppliedBudget,
    CapabilityScope,
    ContextResolution,
    Presentation,
)
from repoatlas.models.capability_io import (
    ContextView,
    EntityHandle,
    EvidenceItem,
    IndexCounts,
    RepositoriesRequest,
    RepositoriesResponse,
    RepositoryView,
    StatusRequest,
    StatusResponse,
)
from repoatlas.models.dependencies import DependencyObservation
from repoatlas.models.dependency_inventory import (
    AppliedPageLimits,
    DependenciesRequest,
    DependenciesResponse,
    DependencyObservationView,
    DependencyPackageView,
    DependencyProvenance,
    ProviderView,
)
from repoatlas.models.evidence import EvidenceCandidate, EvidenceSource, EvidenceTier, MatchClass
from repoatlas.models.freshness import FreshnessDepth, IndexFreshness
from repoatlas.models.http_inventory import (
    CrossingBasis,
    DiscoveryBasis,
    HttpOperationsRequest,
    HttpOperationsResponse,
    OperationBasis,
)
from repoatlas.models.impact import (
    ImpactOmitted,
    ImpactRequest,
    ImpactResponse,
    ImpactSummary,
    PackageImpact,
    SeedStatus,
)
from repoatlas.models.inspection import (
    Coverage,
    InspectedFile,
    InspectionSource,
    InspectRequest,
    InspectResponse,
    InspectTargetKind,
    LineRange,
)
from repoatlas.models.investigation import (
    DEFAULT_LEDGER_CAPACITY,
    EvidenceRef,
    EvidenceStatus,
    EvidenceTransition,
    IndexedEvidenceState,
    LedgerEntry,
    ProposedEvidence,
)
from repoatlas.models.parsing import NeighborOrder
from repoatlas.models.relationships import (
    MAX_NEIGHBORS_CEILING,
    AppliedGraphLimits,
    EdgeProvenance,
    EntityRef,
    GraphEdge,
    GraphPath,
    RelationshipDirection,
    RelationshipsRequest,
    RelationshipsResponse,
)
from repoatlas.models.reports import SyncReport
from repoatlas.models.repository import FileState, Repository
from repoatlas.models.retrieval import RetrievalOutcome
from repoatlas.models.search import SearchMode, SearchRequest, SearchResponse
from repoatlas.models.sync import EvidenceRevalidationSummary, SyncRequest, SyncResponse
from repoatlas.models.topology import (
    NAME_TYPES,
    EntryKind,
    TopologyChildrenRequest,
    TopologyChildrenResponse,
    TopologyEdgeRequest,
    TopologyEdgeResponse,
    TopologyFilesRequest,
    TopologyFilesResponse,
    TopologyNamesRequest,
    TopologyNamesResponse,
    TopologyOverviewRequest,
    TopologyOverviewResponse,
    TopologyReferencesRequest,
    TopologyReferencesResponse,
)
from repoatlas.models.verification import (
    AgentAnswer,
    CitationVerdict,
    EvidenceStatusView,
    GroundingFinding,
    GroundingFindingCode,
    VerifyGroundingRequest,
    VerifyGroundingResponse,
)
from repoatlas.retrieval.query import analyze
from repoatlas.retrieval.retriever import DIAGNOSTIC_LIMITS
from repoatlas.services import sync_service
from repoatlas.services.freshness_service import (
    current_facts_version,
    repository_freshness_for,
    repository_set_drift,
)
from repoatlas.services.workspace import Workspace, open_workspace
from repoatlas.storage.protocols import EntityIdentity, StoredEdge
from repoatlas.sync.engine import BINARY_SNIFF_BYTES
from repoatlas.sync.scanner import hash_bytes

#: How many times an inspection re-reads a file whose indexed state moved
#: underneath it. Small, because a second disagreement means a sync is actively
#: running rather than that one landed at an unlucky moment, and retrying past
#: that is waiting rather than reading.
_SNAPSHOT_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class _DependencyPage:
    """One cut of the ordered dependency inventory, with what the cursor needs.

    The two digests travel with the page because they describe the dataset it was
    cut from: one says which question produced it, the other which inventory. A
    continuation token assembled from anything else would be a promise the next
    call could not check.
    """

    packages: list[DependencyPackageView]
    has_more: bool
    identity: str
    inventory: str


@dataclass(frozen=True, slots=True)
class _WalkedEdge:
    """One public edge, with the walk step that produced it.

    The step carries the storage row ids the edge may not: which file asserted it,
    and which end the walk stood on. Keeping them side by side is what lets the
    answer be ordered and cut on natural identity alone, and the storage lookups
    happen afterwards for only the edges that survived.
    """

    step: WalkStep
    edge: GraphEdge


@dataclass(frozen=True, slots=True)
class _InspectTarget:
    """One resolved inspection target: which file, which lines, why."""

    repository: Repository
    path: str
    #: The range the target itself implies, before context lines. ``None`` means
    #: the target names a file rather than a place in one.
    base: LineRange | None
    #: For an evidence target, that evidence's status after revalidation.
    evidence_status: EvidenceStatus | None
    match: MatchClass
    reason: str


@dataclass(frozen=True, slots=True)
class _InspectedSource:
    """The one source an inspection chose, and what it can prove."""

    state: FileState
    #: Source line number to that line's text. A line is present only if some
    #: text demonstrably contained it.
    available: dict[int, str]
    line_count: int | None
    source: InspectionSource
    worktree_matches_index: bool | None
    requested: LineRange | None


def _resolvable_in(identity: EntityIdentity, in_scope: frozenset[str]) -> bool:
    """Whether ``relationships`` answers about this entity under this scope.

    The single statement of the ``search`` → ``relationships`` handoff. An interned
    entity — an environment variable, a command, an HTTP operation — belongs to no
    repository and is in every scope; a repository-owned one has to belong to a
    repository the call named. Search publishes a handle only when this holds, and
    relationships resolves a target only when it holds, so a handle search hands
    back is one relationships will take under the same context.
    """
    return identity.repository_slug is None or identity.repository_slug in in_scope


def _walk_home(
    identity: EntityIdentity, resolution: ContextResolution, *, crossing: bool
) -> int | None:
    """The repository a walk may not leave, or ``None`` when nothing confines it.

    ``cross_repository`` is the caller asking to leave, so it confines nothing.
    Otherwise the walk is about one repository, and there are two ways to know
    which. A target that belongs to one brings its own — the rule the HTTP linkage
    work established, and the one that holds in ``GLOBAL`` scope, where the caller
    named no repository but the target did.

    A target that belongs to *none* — an environment variable, a command, an HTTP
    operation — used to bring nothing, so a walk rooted at a hub was confined to
    nothing even under a context that named a repository. It went to the hub and
    straight out into every repository touching it. Under an explicit context the
    caller has already said which repository the question is about, so that is the
    home: "what does my repository do with this endpoint", not "and here is what
    two others do with it". In ``GLOBAL`` scope there is still nothing to be
    confined to and a hub-rooted walk still sees every side.

    The lookup is over ``resolution.repositories`` rather than the register,
    because a repository-owned target is only resolvable when it is in scope
    (:func:`_resolvable_in`) — so the home is always a repository the call named.
    """
    if crossing:
        return None
    slug = identity.repository_slug or (resolution.primary.slug if resolution.primary else None)
    return next((repo.id for repo in resolution.repositories if repo.slug == slug), None)


def _edge_repositories(edges: Iterable[GraphEdge], items: Iterable[EvidenceItem]) -> set[str]:
    """Every repository a relationships answer draws on: endpoints, provenance, text."""
    slugs = {item.ref.repository_slug for item in items}
    for edge in edges:
        for ref in (edge.source, edge.target):
            if ref.repository_slug is not None:
                slugs.add(ref.repository_slug)
        if edge.provenance is not None and edge.provenance.repository_slug is not None:
            slugs.add(edge.provenance.repository_slug)
    return slugs


def _snapshot_key(state: FileState) -> tuple[int, str, str]:
    """What must not change between reading a file's text and its metadata."""
    return (state.file_id, state.content_hash, state.parser_version)


def _index_path(raw: str) -> str:
    """A path in the spelling the index stores, or a refusal.

    Normalizes separators and drops redundant segments so that ``./src/config.py``
    finds the row ``src/config.py`` has. It resolves nothing and follows nothing:
    a ``..`` segment is refused rather than collapsed, because collapsing it would
    turn a path that climbs out of the repository into a plausible one inside it.
    """
    candidate = raw.replace("\\", "/").strip()
    if candidate.startswith("/"):
        raise FileNotIndexedError()
    parts = [part for part in candidate.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise FileNotIndexedError()
    return "/".join(parts)


def _search_path(raw: str | None, resolution: ContextResolution) -> str | None:
    """A search's path scope in the spelling the index stores, or ``None``.

    A repository-relative path means nothing across a managed root, so it needs a
    repository scope — the same refusal, for the same reason, that inspection gives
    a path target. That keeps it a narrowing of the one repository the caller named
    rather than a second way of choosing repositories.
    """
    if raw is None:
        return None
    if resolution.scope is not CapabilityScope.REPOSITORY:
        raise ContextResolutionError(
            "a repository-relative path needs a repository scope",
            detail="Name the repository this path belongs to.",
            reason=ContextFailure.AMBIGUOUS,
            candidates=tuple(sorted(repo.slug for repo in resolution.repositories)),
        )
    return _index_path(raw)


def _whole_or_clamped(padded: LineRange | None, line_count: int) -> LineRange | None:
    """The range to supply from a file whose true length is known."""
    if padded is not None:
        return clamp(padded, line_count)
    return LineRange(start=1, end=line_count) if line_count >= 1 else None


class CapabilityService:
    """One workspace, one session registry, one lock, for as long as it lives.

    Construct with :meth:`open` in production, which opens the workspace. Pass an
    already-open :class:`~repoatlas.services.workspace.Workspace` to the
    constructor when the caller assembled one itself — a test, or a host that
    already had one. Either way the service owns it from that point and
    :meth:`close` closes it.
    """

    def __init__(
        self,
        workspace: Workspace,
        *,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        idle_ttl_seconds: float = DEFAULT_IDLE_TTL_SECONDS,
        ledger_capacity: int = DEFAULT_LEDGER_CAPACITY,
    ) -> None:
        self._workspace = workspace
        self._sessions = SessionRegistry(
            max_sessions=max_sessions,
            idle_ttl_seconds=idle_ttl_seconds,
            ledger_capacity=ledger_capacity,
        )
        self._budget_ceiling = ceiling_for(workspace.settings)
        #: Re-entrant deliberately. Nothing here calls another public method
        #: today, but a capability composed of two others is the obvious next
        #: shape, and a plain lock would turn that into a self-deadlock discovered
        #: at runtime rather than a design decision made on purpose.
        self._call_lock = threading.RLock()
        self._closed = False
        #: An in-process cache hint and nothing else. It exists so a later slice
        #: can skip repeated revalidation within one unchanged generation. It is
        #: not persisted, it is not evidence, and no capability may treat a
        #: matching value as proof that anything is current: another process can
        #: sync this index without this counter ever moving (V1_SPEC §12).
        #: Slice C has no mutating capability, so it does not move at all.
        self._generation = 0

    # -- lifecycle ----------------------------------------------------------

    @classmethod
    def open(
        cls,
        *,
        settings: Settings | None = None,
        require_root: bool = True,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        idle_ttl_seconds: float = DEFAULT_IDLE_TTL_SECONDS,
        ledger_capacity: int = DEFAULT_LEDGER_CAPACITY,
    ) -> CapabilityService:
        """Open the configured workspace once and wrap it.

        The V1 path is reused whole: schema check, migration and WAL setup all
        happen here because they happen in :func:`open_workspace`, and a second
        implementation of database lifecycle is exactly the kind of divergence
        that produces two answers to "is this index usable?".

        Note what is **not** captured: no working directory, and no repository
        selection. Opening a service says which index to talk to. It never says
        which repository a later question is about.
        """
        return cls(
            open_workspace(settings=settings, require_root=require_root),
            max_sessions=max_sessions,
            idle_ttl_seconds=idle_ttl_seconds,
            ledger_capacity=ledger_capacity,
        )

    def close(self) -> None:
        """Close the workspace. Idempotent, and one way only.

        Idempotent because a host with both a context manager and an explicit
        shutdown path is normal, and a second close raising would make correct
        code look wrong. One way only because reopening would leave any handle
        taken before the close pointing at a database that is no longer the one
        being served.
        """
        with self._call_lock:
            if self._closed:
                return
            self._closed = True
            self._workspace.close()

    def __enter__(self) -> CapabilityService:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    # -- owned state --------------------------------------------------------

    @property
    def sessions(self) -> SessionRegistry:
        """The investigation registry this service owns.

        Readable because the evidence-producing capabilities will mint
        investigations through it, and because a host may want to see how many are
        live. It is not a mutation API: nothing in Slice C creates, touches or
        reads a session, and a test asserts that.
        """
        return self._sessions

    @property
    def budget_ceiling(self) -> BudgetCeiling:
        """The most evidence this server will grant for one question."""
        return self._budget_ceiling

    @property
    def generation(self) -> int:
        """In-process cache hint. Never a freshness verdict — see the constructor."""
        return self._generation

    @property
    def closed(self) -> bool:
        return self._closed

    # -- capabilities -------------------------------------------------------

    def repositories(self, request: RepositoriesRequest) -> RepositoriesResponse:
        """What RepoAtlas manages, in the requested scope. Reads only.

        The **registered** set. Discovery is not run and nothing is registered:
        both would make a listing call change what the workspace contains, and a
        caller cannot tell an answer that reconciled from one that did not.
        Unregistered directories are reported by :meth:`status` as drift, which is
        where a caller can act on them.
        """
        with self._operation():
            registered = self._workspace.repositories.list_all()
            resolution = resolve_context(request.context, registered)
            facts = current_facts_version(self._workspace)

            # Ambiguity is a property of the whole registered set, not of the
            # scope asked about: a repository is ambiguously named even when it is
            # the only one a repository-scoped call returns.
            duplicates = _duplicate_names(registered)

            views = [
                self._repository_view(
                    repo,
                    depth=request.depth,
                    facts=facts,
                    name_ambiguous=repo.name in duplicates,
                    include_host_path=request.include_host_paths,
                )
                for repo in sorted(resolution.repositories, key=lambda r: r.slug)
            ]
            return RepositoriesResponse(
                context=request.context,
                scope=resolution.scope,
                repositories=tuple(views),
                depth=request.depth,
            )

    def status(self, request: StatusRequest) -> StatusResponse:
        """Whether the index is current, and what it holds. Reads only.

        Detects and never reconciles. Every number is counted from indexed facts
        and every verdict comes from the shared freshness state machine, so this
        and ``repoatlas status`` cannot disagree about what is stale — they are
        the same decision rendered for two audiences.
        """
        with self._operation():
            registered = self._workspace.repositories.list_all()
            resolution = resolve_context(request.context, registered)
            facts = current_facts_version(self._workspace)

            per_repository = [
                repository_freshness_for(self._workspace, repo, depth=request.depth, facts=facts)
                for repo in resolution.repositories
            ]
            # Walked at most once per call. The plan carries both the verdict and
            # the particulars, so asking the aggregate for one and the collections
            # for the other would mean two discovery walks disagreeing about the
            # same root.
            plan = repository_set_drift(self._workspace) if request.include_drift else None
            drift = _drift_of(plan)

            return StatusResponse(
                context=request.context,
                scope=resolution.scope,
                root_configured=self._workspace.root is not None,
                counts=self._counts(resolution, registered),
                freshness=index_freshness(
                    per_repository,
                    depth=request.depth,
                    repository_set_drifted=plan.drifted if plan is not None else None,
                ),
                last_sync=_last_sync(resolution.repositories),
                drift_checked=drift.checked,
                drift_detected=drift.detected,
                new_repositories=drift.added,
                missing_repositories=drift.missing,
                relocated_repositories=drift.relocated,
                conflicting_repositories=drift.conflicting,
                unreadable_paths=drift.unreadable,
            )

    def search(self, request: SearchRequest) -> SearchResponse:
        """Find evidence for a question, and record every citable byte of it.

        The whole of the deterministic funnel runs **once**, through
        :meth:`~repoatlas.retrieval.retriever.Retriever.retrieve_detailed`. Nothing
        about ranking, suppression, tier assignment or deduplication is reproduced
        here: this is an adapter around one retrieval run, and a second opinion
        about relevance formed at this layer would be a second retrieval system
        that the trust guarantees were never written for.

        What this layer adds is what a citation needs and retrieval does not
        provide: exact character bounds, line claims narrowed to the text actually
        supplied, and an atomic ledger record made **before** the response exists.
        The order matters. Evidence returned but not recorded is evidence
        verification cannot see, which is indistinguishable from a fabrication.
        """
        with self._operation():
            registered = self._workspace.repositories.list_all()
            # Before any investigation exists, so a rejected context leaves no
            # session behind.
            resolution = resolve_context(request.context, registered)
            path_prefix = _search_path(request.within_path, resolution)

            # Derived from the parser and dependency registries, so it is one
            # value for the whole call and cannot differ between two repositories.
            facts = current_facts_version(self._workspace)
            applied = apply_budget_policy(
                request.budget, self._workspace.settings, ceiling=self._budget_ceiling
            )
            diagnostic = request.mode is SearchMode.DIAGNOSTIC
            limits = retrieval_limits_for(
                applied,
                self._workspace.settings,
                base=DIAGNOSTIC_LIMITS if diagnostic else None,
            )

            existing = request.investigation_id is not None
            session = (
                self._sessions.get(request.investigation_id)
                if request.investigation_id is not None
                else self._sessions.create()
            )

            try:
                # Retrieval and provenance read one generation of the index.
                #
                # The funnel reads chunk text; the reference built from it names
                # the file's content hash and facts version. Those are separate
                # reads, and outside a snapshot another process's sync can land
                # between them — leaving text from before it paired with a hash
                # from after. Such evidence is worse than wrong: it revalidates
                # cleanly against its own hash while describing a state of the
                # file that never existed. The service lock cannot help, because
                # the other writer is in another process.
                #
                # One read view removes the window rather than detecting it, so
                # there is nothing here to retry and no contention to report. The
                # funnel still runs exactly once inside it.
                with self._workspace.database.read_snapshot():
                    if path_prefix is not None and not self._workspace.search.has_files_under(
                        resolution.repository_ids, path_prefix
                    ):
                        # Inside the snapshot, so "nothing indexed here" and the
                        # search it prevents describe the same generation. The
                        # answer inspect gives any unindexed path, for the same
                        # reasons: an excluded directory and a misspelt one are
                        # not RepoAtlas's to tell apart.
                        raise FileNotIndexedError("no indexed file stands at or beneath that path")
                    outcome = self._workspace.retriever(limits).retrieve_detailed(
                        request.question,
                        to_repository_context(resolution),
                        literal_phrase=diagnostic,
                        # Search mints citable ids, so an entity it names is
                        # worth quoting: a bare location cannot be cited.
                        quote_signatures=True,
                        # An explicit context is a boundary here, as it already is
                        # for inspect, relationships and dependencies. Crossing it
                        # takes `scope: global`, or a relationships walk that says
                        # cross_repository.
                        confined=True,
                        path_prefix=path_prefix,
                    )
                    line_cache: dict[tuple[str, str], dict[int, str]] = {}
                    by_slug = {repo.slug: repo for repo in resolution.repositories}
                    analysis = analyze(request.question)

                    def prepare(candidate: EvidenceCandidate, limit: int) -> EvidenceCandidate:
                        if candidate.line_start is None:
                            return candidate
                        key = (candidate.repository_slug, candidate.path)
                        if key not in line_cache:
                            repo = by_slug[key[0]]
                            assert repo.id is not None
                            state = self._workspace.files.states_for(repo.id, [key[1]]).get(key[1])
                            line_cache[key] = (
                                indexed_line_map(
                                    self._workspace.knowledge.chunks_for_file(
                                        state.file_id, 1, WHOLE_FILE_END
                                    )
                                )
                                if state
                                else {}
                            )
                        return preview(
                            candidate,
                            line_cache[key],
                            analysis.literal_terms or analysis.terms,
                            limit,
                        )

                    shaped = shape_candidates(
                        outcome.evidence.candidates,
                        applied,
                        prepare=prepare if request.source_preview else None,
                    )
                    proposed = self._propose(shaped.items, registered)
                # The single atomic point. A capacity refusal raises here having
                # allocated nothing and strengthened nothing.
                entries = session.ledger.record(proposed)
            except BaseException:
                # A session this call invented, which never got to hold anything,
                # is not left behind as a ghost an agent could be handed the id of.
                # One the caller already owned is untouched.
                if not existing and len(session.ledger) == 0:
                    self._sessions.drop(session.id)
                raise

            return self._search_response(
                request=request,
                resolution=resolution,
                session=session,
                shaped=shaped,
                entries=entries,
                proposed=proposed,
                outcome=outcome,
                applied=applied,
                facts=facts,
                path_prefix=path_prefix,
            )

    def inspect(self, request: InspectRequest) -> InspectResponse:
        """Show source RepoAtlas already indexed, and record every line of it.

        Progressive disclosure over trusted evidence, and deliberately **not** a
        file reader. Every target names something the index already admitted, and
        the admission gate is a ``files`` row: a path with no row is refused
        before any repository is touched, whether it is a secret, an exclusion, a
        file added since the last sync, an attempt to climb out of the repository
        or nothing at all. They are one answer on purpose — telling them apart
        would turn a containment boundary into a way to probe the filesystem.

        Nothing is searched. Paths and entity keys resolve exactly, an evidence id
        resolves inside the investigation that issued it, and no retrieval stage
        runs, so there is no ranking here and nothing to rank.
        """
        with self._operation():
            registered = self._workspace.repositories.list_all()
            # Before any investigation exists, so a rejected context or a target
            # that cannot be named leaves no session behind.
            resolution = resolve_context(request.context, registered)
            applied = apply_budget_policy(
                request.budget, self._workspace.settings, ceiling=self._budget_ceiling
            )

            if (
                request.target.kind is InspectTargetKind.EVIDENCE
                and request.investigation_id is None
            ):
                # Raised before a session could be minted, because minting one and
                # then failing to find E7 in it would report a fresh, empty ledger
                # as though the id had been wrong.
                raise InvestigationRequiredError()

            existing = request.investigation_id is not None
            session = (
                self._sessions.get(request.investigation_id)
                if request.investigation_id is not None
                else self._sessions.create()
            )

            try:
                target = self._resolve_inspect_target(request, resolution, registered, session)
                source = self._inspect_source(target, request)
                shaped = (
                    shape_inspection(source.available, source.requested, applied)
                    if source.requested is not None
                    else ShapedInspection(
                        segments=[], returned=(), budget_omitted=(), unavailable=()
                    )
                )
                proposed = self._propose_segments(target, source, shaped)
                # The single atomic point. A capacity refusal raises here having
                # allocated nothing and strengthened nothing.
                entries = session.ledger.record(proposed)
            except BaseException:
                # A session this call invented, which never got to hold anything,
                # is not left behind as a ghost an agent could be handed the id of.
                # One the caller already owned is untouched — including the
                # revalidation an evidence target legitimately performed on it,
                # which observed the index rather than changing the evidence.
                if not existing and len(session.ledger) == 0:
                    self._sessions.drop(session.id)
                raise

            return self._inspect_response(
                request=request,
                resolution=resolution,
                session=session,
                target=target,
                source=source,
                shaped=shaped,
                entries=entries,
                proposed=proposed,
                applied=applied,
            )

    def relationships(self, request: RelationshipsRequest) -> RelationshipsResponse:
        """What RepoAtlas already knows is connected to one exact entity.

        Deterministic throughout. The target resolves by natural key or not at
        all, the walk follows the one bounded traversal policy the retrieval
        funnel uses, and every edge returned was written by a parser or resolved
        by the linker from an exact match. Nothing here is retrieved, scored or
        ranked, which is why an edge carries the confidence its parser assigned
        and no relevance this layer invented.

        Two things are kept apart on purpose. An edge's **provenance** is a graph
        fact — which file asserts it, at which lines — and is reported for every
        edge that has one. Whether the **source text** at those coordinates could
        be reconstructed and recorded is a separate question, and only a yes to it
        puts evidence ids on the edge. An edge advertising a citation the ledger
        cannot resolve is indistinguishable from a fabricated one.
        """
        with self._operation():
            registered = self._workspace.repositories.list_all()
            # Before any investigation exists, so a rejected context or an
            # untraversable request leaves no session behind.
            resolution = resolve_context(request.context, registered)
            limits = graph_limits_for(request)
            admitted = admitted_edge_types(request)

            existing = request.investigation_id is not None
            session = (
                self._sessions.get(request.investigation_id)
                if request.investigation_id is not None
                else self._sessions.create()
            )

            try:
                # The graph, the identities of its endpoints, the provenance
                # coordinates and the chunks behind them are one indexed snapshot
                # or they are nothing. Read separately, an external sync landing
                # mid-call could pair an old edge with a new entity location, and
                # the result would look exactly like a fact.
                with self._workspace.database.read_snapshot():
                    identity = self._resolve_graph_entity(request.target, resolution)
                    target = entity_ref(identity)
                    # "Whether the walk may leave the target's repository" holds
                    # through shared nodes too, not only across direct edges — and
                    # a shared node has no repository, so the context supplies one.
                    home_repo_id = _walk_home(
                        identity, resolution, crossing=request.cross_repository
                    )
                    walked = walk(
                        self._workspace.knowledge,
                        [identity.entity_id],
                        hops=limits.hops,
                        # The server's own ceiling, never the caller's number. A
                        # request for three neighbours must return the first three
                        # of the fifty, not the three the store happened to reach
                        # first — otherwise the answer to a narrow question is a
                        # different neighbourhood rather than a shorter one.
                        fanout=MAX_NEIGHBORS_CEILING,
                        seed_limit=1,
                        edge_types=admitted,
                        direction=EDGE_DIRECTIONS[request.direction],
                        crossing_edges=(
                            CROSS_REPOSITORY_EDGES if request.cross_repository else frozenset()
                        ),
                        containment=request.include_containment,
                        # The cut is the answer here, so it falls on the
                        # repositories' own facts rather than on insertion order.
                        order=NeighborOrder.NATURAL,
                        home_repo_id=home_repo_id,
                    )
                    walked_edges = self._walked_edges(walked)
                    # Ordered and cut before anything is fetched, so what comes
                    # back never depends on how much was worth fetching.
                    ordered = sorted(walked_edges, key=lambda w: edge_order(w.edge))
                    # Either bound counts. The walk's own ceiling and the caller's
                    # number cut for different reasons, and a caller asking
                    # whether they saw everything is owed the same answer either
                    # way.
                    truncated = walked.bounded or len(ordered) > limits.max_neighbors
                    kept = ordered[: limits.max_neighbors]
                    located, supplies = self._supply_provenance(kept)
                # The single atomic point. A capacity refusal raises here having
                # allocated nothing and strengthened nothing.
                entries = session.ledger.record([supply.proposed for supply in supplies])
            except BaseException:
                # A session this call invented, which never got to hold anything,
                # is not left behind as a ghost an agent could be handed the id of.
                # One the caller already owned is untouched.
                if not existing and len(session.ledger) == 0:
                    self._sessions.drop(session.id)
                raise

            return self._relationships_response(
                request=request,
                resolution=resolution,
                session=session,
                target=target,
                located=located,
                supplies=supplies,
                entries=entries,
                truncated=truncated,
                limits=limits,
            )

    def impact(self, request: ImpactRequest) -> ImpactResponse:
        """What a change to one thing may reach, by which indexed paths, in rank order.

        Deterministic throughout, like ``relationships``: the seed resolves to exactly
        one thing or nothing is followed, and every step returned is an indexed edge
        or a fact derived from spans or manifests (:mod:`repoatlas.graph.impact`).
        Nothing is recorded in the investigation's ledger and no source text is
        returned: each step names the file and lines that prove it, and reading them
        is the caller's part.
        """
        with self._operation():
            registered = self._workspace.repositories.list_all()
            resolution = resolve_context(request.context, registered)
            session = (
                self._sessions.get(request.investigation_id)
                if request.investigation_id is not None
                else self._sessions.create()
            )
            scope = {repo.slug: repo.id for repo in resolution.repositories if repo.id is not None}
            slugs = {repo.id: repo.slug for repo in registered if repo.id is not None}
            answer: Assembled | None = None
            packages: tuple[PackageImpact, ...] = ()
            # The seed, the walk and every row it reads are one indexed snapshot.
            with self._workspace.database.read_snapshot():
                seeds = SeedResolver(self._workspace.knowledge, self._workspace.search, scope)
                seed = seeds.resolve(request.seed)
                if seed.model.status is SeedStatus.RESOLVED:
                    home = scope.get(seed.model.repository or "")
                    if home is None and resolution.scope is CapabilityScope.REPOSITORY:
                        # A shared seed (an environment variable, a route) walked from
                        # inside one repository stays in it.
                        home = resolution.primary.id if resolution.primary else None
                    manifests = self._workspace.knowledge.provided_manifests(sorted(slugs.values()))
                    directories = {
                        slug: package_directories(paths) for slug, paths in manifests.items()
                    }
                    walk = ImpactWalk(
                        self._workspace.knowledge,
                        slugs,
                        unit=lambda key, slug, path: unit_of(key, slug, path, directories),
                        # A repository scope is a boundary: nothing crosses out of it.
                        scope=frozenset(scope.values())
                        if resolution.scope is CapabilityScope.REPOSITORY
                        else None,
                    )
                    starts, members = seed_nodes(seed, walk, home)
                    walked = walk.run(starts, members)
                    answer = assemble(
                        walked,
                        walk,
                        max_candidates=request.max_candidates,
                        full=request.presentation is Presentation.FULL,
                        directories=directories,
                    )
                    packages, more = package_dependents(
                        seed,
                        self._workspace.knowledge,
                        self._workspace.dependencies,
                        walk,
                        walked,
                        [*answer.candidates, *answer.tests],
                        directories,
                        scoped=resolution.scope is CapabilityScope.REPOSITORY,
                        roots={r.id: root_path(r.path) for r in registered if r.id is not None},
                    )
                    if more:
                        reasons = dict(answer.omitted.by_reason)
                        reasons["budget"] = reasons.get("budget", 0) + more
                        answer.omitted = answer.omitted.model_copy(update={"by_reason": reasons})
            candidates = answer.candidates if answer is not None else ()
            tests = answer.tests if answer is not None else ()
            facts = current_facts_version(self._workspace)
            drawn = {c.repository for c in (*candidates, *tests) if c.repository}
            if seed.model.repository:
                drawn.add(seed.model.repository)
            covered = self._covering_repositories(resolution, sorted(drawn))
            freshness = index_freshness(
                [
                    repository_freshness_for(self._workspace, r, depth=request.depth, facts=facts)
                    for r in covered
                ],
                depth=request.depth,
            )
            return ImpactResponse(
                investigation_id=session.id,
                context=ContextView(
                    context=request.context,
                    scope=resolution.scope,
                    repositories=tuple(sorted(repo.slug for repo in resolution.repositories)),
                    primary=resolution.primary.slug if resolution.primary else None,
                    reason=resolution.reason,
                ),
                seed=seed.model,
                candidates=candidates,
                tests=tests,
                packages=packages,
                hubs=answer.hubs if answer is not None else (),
                unresolved=answer.unresolved if answer is not None else (),
                omitted=answer.omitted if answer is not None else ImpactOmitted(),
                summary=answer.summary
                if answer is not None
                else ImpactSummary(policy=IMPACT_POLICY),
                freshness=freshness,
            )

    def dependencies(self, request: DependenciesRequest) -> DependenciesResponse:
        """What the index records about declared, resolved and provided packages.

        A projection of what manifests and lockfiles say, and nothing more. Every
        row here was parsed out of a file in the indexed snapshot; none of it was
        resolved, installed or executed, and no registry was consulted. That is
        why a package's observations are returned whole rather than reduced to a
        version: two repositories declaring one package with two constraints are
        making two true statements, and the single answer a caller might prefer
        would be one nobody wrote.

        Three things are deliberately kept apart. A **declaration** asks for a
        version and a **resolution** records one a resolver once chose; neither is
        evidence that anything is installed. A **provider candidate** is a
        repository publishing a name, which is knowledge about names rather than
        about what a build would pick — so when two repositories publish one name,
        both are returned and neither is chosen. And an observation's
        **provenance** is always available, while its **source text** is citable
        only when the indexed snapshot could supply it.
        """
        with self._operation():
            registered = self._workspace.repositories.list_all()
            # Before any investigation exists, so a rejected context or a
            # malformed request leaves no session behind.
            resolution = resolve_context(request.context, registered)
            validate_dependency_request(request)
            limits = page_limits_for(request)

            existing = request.investigation_id is not None
            session = (
                self._sessions.get(request.investigation_id)
                if request.investigation_id is not None
                else self._sessions.create()
            )

            try:
                # The dependency rows, the provider rows behind them, the file
                # states their provenance names and the chunks that quote it are
                # one indexed snapshot or they are nothing. Read separately, a sync
                # landing mid-call could pair a constraint from before it with a
                # lockfile location from after, and the result would look exactly
                # like a fact.
                with self._workspace.database.read_snapshot():
                    views, page, supplies = self._dependency_page(request, resolution, limits)
                # The single atomic point. A capacity refusal raises here having
                # allocated nothing and strengthened nothing.
                entries = session.ledger.record([supply.proposed for supply in supplies])
            except BaseException:
                # A session this call invented, which never got to hold anything,
                # is not left behind as a ghost an agent could be handed the id of.
                # One the caller already owned is untouched.
                if not existing and len(session.ledger) == 0:
                    self._sessions.drop(session.id)
                raise

            return self._dependencies_response(
                request=request,
                resolution=resolution,
                session=session,
                views=views,
                page=page,
                supplies=supplies,
                entries=entries,
                limits=limits,
            )

    def http_operations(self, request: HttpOperationsRequest) -> HttpOperationsResponse:
        """The HTTP operations one repository's indexed source makes or serves.

        A static inventory, and deliberately nothing more. Each row is a request the
        source writes or a route it serves or declares, with its location and a
        status measured against **this repository's indexed facts only**: whether
        a route indexed as served here has the request's method and shape. That is
        route compatibility. It is not which host a client is configured for, not
        which deployment answers, and not who owns the data — none of which static
        reading establishes — and ``basis`` says so in every response.

        The repository is replanned from its standing claims on every call rather
        than read from stored outcomes, with the same function the global link pass
        uses: every rule that pass applies to HTTP claims is repository-local, so
        the replan is exactly that repository's share of it. A replan that disagrees
        with the stored graph is still answered, and marked.

        Filters narrow the rows returned, never what is known: the whole repository
        is planned first, so a path filter cannot make a call look unhandled by
        hiding the route it matches.

        A caller row's crossings are the workspace join's decisions, read as the last
        global link pass stored them, since the join needs every repository. Under
        repository scope, what another repository proves in them is withheld unless
        ``cross_repository`` asks for it, and is never supplied as evidence outside
        global scope.
        """
        with self._operation():
            registered = self._workspace.repositories.list_all()
            slugs = {repo.id: repo.slug for repo in registered if repo.id is not None}
            # Before any investigation exists, so a rejected context or a
            # malformed request leaves no session behind.
            resolution = resolve_context(request.context, registered)
            if not request.discover and (
                resolution.scope is not CapabilityScope.REPOSITORY
                or len(resolution.repositories) != 1
            ):
                raise ContextResolutionError(
                    "the HTTP inventory needs a repository scope",
                    detail=(
                        "Name the one repository to list: every status in it speaks "
                        "for that repository's indexed source only."
                    ),
                    reason=ContextFailure.AMBIGUOUS,
                    candidates=tuple(sorted(repo.slug for repo in resolution.repositories)),
                )
            repository = None if request.discover else resolution.repositories[0]
            prefix = _search_path(request.within_path, resolution)
            limits = http_inventory.page_limits_for(request)

            existing = request.investigation_id is not None
            session = (
                self._sessions.get(request.investigation_id)
                if request.investigation_id is not None
                else self._sessions.create()
            )

            try:
                # The claims, the replan, the edges and the chunks any evidence is
                # quoted from are one indexed snapshot or nothing: a sync landing
                # mid-call could otherwise pair a decision from before it with a
                # location from after.
                with self._workspace.database.read_snapshot():
                    if prefix is not None and not self._workspace.search.has_files_under(
                        resolution.repository_ids, prefix
                    ):
                        raise FileNotIndexedError("no indexed file stands at or beneath that path")
                    if repository is not None:
                        inventory = self._http_inventory(repository, slugs)
                    else:
                        inventories = [
                            (repo, self._http_inventory(repo, slugs))
                            for repo in resolution.repositories
                        ]
                        inventory = http_inventory.Inventory(
                            drafts=[
                                http_inventory.Draft(
                                    operation=draft.operation.model_copy(
                                        update={"repository": repo.slug}
                                    ),
                                    qualified_name=draft.qualified_name,
                                    reaches=draft.reaches,
                                )
                                for repo, local in inventories
                                for draft in local.drafts
                            ],
                            parsers={
                                f"{repo.slug}/{path}": parser
                                for repo, local in inventories
                                for path, parser in local.parsers.items()
                            },
                            stale_graph=any(local.stale_graph for _, local in inventories),
                            crossings_built=self._workspace.knowledge.http_call_decisions_built(),
                        )
                    chosen = http_inventory.selected(inventory.drafts, request, prefix)
                    page = http_inventory.cut_page(
                        chosen,
                        request.cursor,
                        identity=http_inventory.request_digest(
                            request, repository.slug if repository else "*global*", prefix
                        ),
                        inventory=http_inventory.inventory_digest(chosen),
                        limits=limits,
                    )
                    home = repository.slug if repository is not None else None
                    page = replace(
                        page,
                        rows=[
                            http_inventory.project(row, home, request.cross_repository)
                            for row in page.rows
                        ],
                    )
                    supplies: list[ProvenanceSupply] = []
                    if request.include_provenance:
                        spans = http_inventory.evidence_spans(
                            page.rows,
                            home,
                            everywhere=resolution.scope is CapabilityScope.GLOBAL,
                        )
                        by_slug = {repo.slug: repo for repo in registered}
                        order = [repo.slug for repo in resolution.repositories]
                        order += sorted(set(spans) - set(order))
                        for slug in order:
                            if slug in spans and slug in by_slug:
                                supplies.extend(
                                    self._http_operation_supplies(by_slug[slug], spans[slug])
                                )
                # The single atomic point. A capacity refusal raises here having
                # allocated nothing and strengthened nothing.
                entries = session.ledger.record([supply.proposed for supply in supplies])
            except BaseException:
                # A session this call invented, which never got to hold anything,
                # is not left behind as a ghost an agent could be handed the id of.
                if not existing and len(session.ledger) == 0:
                    self._sessions.drop(session.id)
                raise

            return self._http_operations_response(
                request=request,
                resolution=resolution,
                registered=registered,
                session=session,
                repository=repository,
                inventory=inventory,
                chosen=chosen,
                page=page,
                supplies=supplies,
                entries=entries,
                limits=limits,
                prefix=prefix,
            )

    def topology_overview(self, request: TopologyOverviewRequest) -> TopologyOverviewResponse:
        """Which repositories in scope are linked to which, and by what kind of fact.

        Reads only, and a fixed number of statements: one scan of the edges whose
        ends lie in two repositories, one read of the examples it keeps, and the
        dependency inventory. Nothing is read per repository or per edge, so the
        cost in statements is the same for two repositories and for twenty.

        Both ends of every link are in scope. A repository-scoped overview is
        therefore empty of links by construction: reaching another repository is
        a thing to ask for with ``scope: global``.
        """
        with self._operation():
            registered = self._workspace.repositories.list_all()
            resolution = resolve_context(request.context, registered)
            in_scope = {r.id: r.slug for r in resolution.repositories if r.id is not None}
            store = self._workspace.topology()
            with self._workspace.database.read_snapshot():
                rows = store.crossing_edges(exclude=STRUCTURAL, limit=ROW_CEILING + 1)
                complete = len(rows) <= ROW_CEILING
                found = crossing_groups(rows if complete else (), in_scope)
                refs = {
                    edge.edge_id: encode_edge_ref(edge)
                    for edge in store.stored_edges(found.example_ids())
                }
                packages = self._workspace.dependencies.list_packages(None)
            links = repository_links(found, refs, packages)
            listed, ambiguous_total = ambiguous_dependencies(packages, frozenset(in_scope.values()))
            facts = current_facts_version(self._workspace)
            return TopologyOverviewResponse(
                context=context_view(request.context, resolution),
                repositories=tuple(sorted(in_scope.values())),
                links=tuple(links),
                ambiguous=listed,
                ambiguous_total=ambiguous_total,
                complete=complete,
                freshness=index_freshness(
                    [
                        repository_freshness_for(
                            self._workspace, repo, depth=request.depth, facts=facts
                        )
                        for repo in resolution.repositories
                    ],
                    depth=request.depth,
                ),
            )

    def topology_children(self, request: TopologyChildrenRequest) -> TopologyChildrenResponse:
        """One page of what a directory or file holds, and how all its children link.

        Reads only, and a fixed number of statements whatever the path holds: the
        listing, one count over the page's files, the edges leaving the path and
        the edges arriving at it, the examples kept, and — only when crossing was
        asked for — the dependency inventory. No statement runs per child.

        A link to another repository is reported only with ``cross_repository``,
        the same explicit step a relationships walk takes to leave its scope.
        """
        with self._operation():
            registered = self._workspace.repositories.list_all()
            resolution = resolve_context(request.context, registered)
            repo = resolution.primary
            if (
                resolution.scope is not CapabilityScope.REPOSITORY
                or repo is None
                or repo.id is None
            ):
                raise InvalidTopologyRequestError(
                    "children are the children of a path in one repository",
                    detail="Name the repository in a repository-scoped context.",
                )
            limits = page_limits(request.limit)
            after = (
                decode_children_cursor(request.cursor, repo.slug, request.path)
                if request.cursor is not None
                else None
            )
            slugs = {r.id: r.slug for r in registered if r.id is not None}
            store = self._workspace.topology()
            with self._workspace.database.read_snapshot():
                file_row = store.file_at(repo.id, request.path) if request.path else None
                if file_row is not None:
                    declarations = store.declarations_in(file_row.file_id)
                    listed = file_entries(request.path, declarations)
                    declared = {row.entity_id: row.key for row in declarations}
                else:
                    files = store.files_under(repo.id, request.path)
                    if request.path and not files:
                        raise PathNotFoundError()
                    listed = directory_entries(repo.slug, request.path, files)
                    declared = {}
                page, resume_after = page_of(listed, after, limits.applied)
                counts = store.declaration_counts(
                    [item.file_id for item in page if item.file_id is not None]
                )
                scope = ChildScope(
                    repo_id=repo.id,
                    repository=repo.slug,
                    path=request.path,
                    is_file=file_row is not None,
                    declarations=declared,
                    cross_repository=request.cross_repository,
                    slugs=slugs,
                )
                where = {
                    "prefix": None if file_row is not None else request.path,
                    "file_id": file_row.file_id if file_row is not None else None,
                }
                outgoing = store.edges_under(
                    repo.id, outgoing=True, exclude=STRUCTURAL, limit=ROW_CEILING + 1, **where
                )
                incoming = store.edges_under(
                    repo.id, outgoing=False, exclude=STRUCTURAL, limit=ROW_CEILING + 1, **where
                )
                asserted = store.repository_edges_under(
                    repo.id, exclude=STRUCTURAL, limit=ROW_CEILING + 1, **where
                )
                complete = all(len(rows) <= ROW_CEILING for rows in (outgoing, incoming, asserted))
                groups = child_groups(scope, outgoing, incoming, asserted) if complete else {}
                bounded = bound_child_links(groups)
                refs = {
                    edge.edge_id: encode_edge_ref(edge)
                    for edge in store.stored_edges(bounded.example_ids())
                }
            links = child_links(bounded, refs)
            facts = current_facts_version(self._workspace)
            return TopologyChildrenResponse(
                context=context_view(request.context, resolution),
                repository=repo.slug,
                path=request.path,
                target=EntryKind.FILE if file_row is not None else EntryKind.DIRECTORY,
                entries=tuple(with_declaration_counts(page, counts)),
                entries_total=len(listed),
                next_cursor=(
                    encode_children_cursor(repo.slug, request.path, resume_after)
                    if resume_after is not None
                    else None
                ),
                has_more=resume_after is not None,
                limits=limits,
                links=tuple(links),
                links_total=bounded.total,
                truncated=bounded.truncated,
                complete=complete,
                freshness=index_freshness(
                    [
                        repository_freshness_for(
                            self._workspace, covered, depth=request.depth, facts=facts
                        )
                        for covered in self._covering_repositories(
                            resolution, peer_repositories(links)
                        )
                    ],
                    depth=request.depth,
                ),
            )

    def topology_files(self, request: TopologyFilesRequest) -> TopologyFilesResponse:
        """Indexed files in scope whose paths hold every term of the query, best first.

        One statement, ranked and counted in the database, whatever the index holds.
        Evidence-free, like the rest of topology: it names files, it quotes nothing,
        and it opens no investigation, so a search box may ask it on every keystroke.
        """
        with self._operation():
            registered = self._workspace.repositories.list_all()
            resolution = resolve_context(request.context, registered)
            in_scope = {r.id: r.slug for r in resolution.repositories if r.id is not None}
            store = self._workspace.topology()
            with self._workspace.database.read_snapshot():
                rows = store.files_matching(
                    sorted(in_scope), query_terms(request.query), limit=request.limit
                )
            total = rows[0].total if rows else 0
            facts = current_facts_version(self._workspace)
            return TopologyFilesResponse(
                context=context_view(request.context, resolution),
                query=request.query,
                files=tuple(file_matches(rows, in_scope)),
                total=total,
                truncated=total > len(rows),
                freshness=index_freshness(
                    [
                        repository_freshness_for(
                            self._workspace, repo, depth=request.depth, facts=facts
                        )
                        for repo in resolution.repositories
                    ],
                    depth=request.depth,
                ),
            )

    def topology_names(self, request: TopologyNamesRequest) -> TopologyNamesResponse:
        """Declared names in scope whose qualified names hold every term, best first.

        One statement, ranked and counted in the database, like files. Evidence-
        free: it names entities, it quotes nothing, and it opens no investigation.
        A name found here is traced or inspected by its key, which records its own.
        """
        with self._operation():
            registered = self._workspace.repositories.list_all()
            resolution = resolve_context(request.context, registered)
            in_scope = {r.id: r.slug for r in resolution.repositories if r.id is not None}
            store = self._workspace.topology()
            with self._workspace.database.read_snapshot():
                rows = store.names_matching(
                    sorted(in_scope),
                    query_terms(request.query),
                    types=NAME_TYPES,
                    limit=request.limit,
                )
            total = rows[0].total if rows else 0
            facts = current_facts_version(self._workspace)
            return TopologyNamesResponse(
                context=context_view(request.context, resolution),
                query=request.query,
                names=tuple(name_matches(rows, in_scope)),
                total=total,
                truncated=total > len(rows),
                freshness=index_freshness(
                    [
                        repository_freshness_for(
                            self._workspace, repo, depth=request.depth, facts=facts
                        )
                        for repo in resolution.repositories
                    ],
                    depth=request.depth,
                ),
            )

    def topology_references(self, request: TopologyReferencesRequest) -> TopologyReferencesResponse:
        """What each line of one file declares and refers to, in source order.

        Three statements whatever the file holds: the file, its declarations, and
        the located edges it asserts. A reference resolved into another repository
        is listed only with ``cross_repository``; otherwise it is counted.
        """
        with self._operation():
            registered = self._workspace.repositories.list_all()
            resolution = resolve_context(request.context, registered)
            repo = resolution.primary
            if (
                resolution.scope is not CapabilityScope.REPOSITORY
                or repo is None
                or repo.id is None
            ):
                raise InvalidTopologyRequestError(
                    "references are the references of a file in one repository",
                    detail="Name the repository in a repository-scoped context.",
                )
            slugs = {r.id: r.slug for r in registered if r.id is not None}
            store = self._workspace.topology()
            with self._workspace.database.read_snapshot():
                file_row = store.file_at(repo.id, request.path)
                if file_row is None:
                    if store.files_under(repo.id, request.path):
                        raise InvalidTopologyRequestError(
                            "references are read from one file, and that path is a directory",
                            detail="Ask topology/children for what a directory holds.",
                        )
                    raise PathNotFoundError()
                declarations = store.declarations_in(file_row.file_id)
                rows = store.references_in(
                    file_row.file_id, exclude=STRUCTURAL, limit=ROW_CEILING + 1
                )
            complete = len(rows) <= ROW_CEILING
            found = line_references(
                rows if complete else (),
                repo_id=repo.id,
                cross_repository=request.cross_repository,
                slugs=slugs,
            )
            facts = current_facts_version(self._workspace)
            return TopologyReferencesResponse(
                context=context_view(request.context, resolution),
                repository=repo.slug,
                path=request.path,
                declarations=tuple(item.entry for item in file_entries(request.path, declarations)),
                references=found.references,
                references_total=found.total,
                truncated=found.truncated,
                other_repositories=found.other_repositories,
                complete=complete,
                freshness=index_freshness(
                    [
                        repository_freshness_for(
                            self._workspace, covered, depth=request.depth, facts=facts
                        )
                        for covered in self._covering_repositories(
                            resolution, referenced_repositories(found.references)
                        )
                    ],
                    depth=request.depth,
                ),
            )

    def topology_edge(self, request: TopologyEdgeRequest) -> TopologyEdgeResponse:
        """One exact stored relationship, why it is held, and the lines asserting it.

        The reference names the fact exactly, so nothing here searches: the
        source's key is read by index, the edges of that type leaving it by index,
        and the one whose every stored field matches is the answer. Rows that match
        on every field are one fact recorded more than once, and resolve together.

        The investigation follows ``relationships`` exactly. Without an id a new
        one is started and returned; with one, that one is used, and the asserting
        lines are recorded in it — so ``inspect`` can read them by evidence id in
        the same investigation. An edge resolves only in a scope that holds every
        repository it touches, and one that does not is reported as not found,
        exactly as an entity outside the scope is.
        """
        with self._operation():
            registered = self._workspace.repositories.list_all()
            resolution = resolve_context(request.context, registered)
            # Before any investigation exists, so a malformed reference leaves no
            # session behind.
            identity = decode_edge_ref(request.edge_ref)

            existing = request.investigation_id is not None
            session = (
                self._sessions.get(request.investigation_id)
                if request.investigation_id is not None
                else self._sessions.create()
            )
            try:
                with self._workspace.database.read_snapshot():
                    sources = self._workspace.knowledge.get_entities_by_key([identity.src_key])
                    found = matching_edges(
                        identity,
                        self._workspace.topology().stored_edges_from(
                            [source.entity_id for source in sources], identity.type
                        ),
                    )
                    if not found:
                        raise EdgeNotFoundError()
                    edge = found[0]
                    in_scope = {repo.id for repo in resolution.repositories}
                    touched = {i for i in (edge.src_repo_id, edge.dst_repo_id) if i is not None}
                    if not touched <= in_scope:
                        raise EdgeNotFoundError()
                    ends = self._workspace.knowledge.keys_for_entities([edge.src_id, edge.dst_id])
                    supply = self._edge_supply(edge)
                entries = session.ledger.record([supply.proposed] if supply is not None else [])
            except BaseException:
                if not existing and len(session.ledger) == 0:
                    self._sessions.drop(session.id)
                raise

            items = tuple(
                EvidenceItem(
                    id=entry.id,
                    rank=rank,
                    ref=supplied.proposed.ref,
                    repository_name=supplied.proposed.repository_name,
                    location=supplied.proposed.ref.location,
                    text=supplied.text,
                    why=supplied.proposed.reason,
                    tier=supplied.proposed.tier,
                    match=supplied.proposed.match,
                    status=entry.status,
                )
                for rank, (supplied, entry) in enumerate(
                    zip([supply] if supply is not None else [], entries, strict=True), start=1
                )
            )
            resolution_record = decoded_resolution(edge)
            provenance = (
                None
                if edge.parser is None and edge.file_id is None and edge.line_start is None
                else EdgeProvenance(
                    repository_slug=edge.repository_slug,
                    path=edge.path,
                    line_start=edge.line_start,
                    line_end=edge.line_end,
                    parser=edge.parser,
                    citable=bool(items),
                )
            )
            facts = current_facts_version(self._workspace)
            touched_slugs = {
                slug
                for slug in (
                    ends[edge.src_id].repository_slug,
                    ends[edge.dst_id].repository_slug,
                    edge.repository_slug,
                )
                if slug is not None
            }
            return TopologyEdgeResponse(
                investigation_id=session.id,
                context=context_view(request.context, resolution),
                edge_ref=request.edge_ref,
                edge=GraphEdge(
                    source=entity_ref(ends[edge.src_id]),
                    target=entity_ref(ends[edge.dst_id]),
                    type=edge.type,
                    hop=1,
                    traversal_direction=RelationshipDirection.OUTGOING,
                    cross_repository=(
                        edge.src_repo_id is not None
                        and edge.dst_repo_id is not None
                        and edge.src_repo_id != edge.dst_repo_id
                    ),
                    confidence=edge.confidence,
                    provenance=provenance,
                    evidence_ids=tuple(item.id for item in items),
                ),
                multiplicity=len(found),
                resolution=resolution_record,
                crossing=(
                    package_crossing(resolution_record, edge.repository_slug, edge.path)
                    if resolution_record is not None
                    else None
                ),
                http=(
                    http_crossing(resolution_record, edge.repository_slug)
                    if resolution_record is not None
                    else None
                ),
                evidence=items,
                freshness=index_freshness(
                    [
                        repository_freshness_for(
                            self._workspace, covered, depth=request.depth, facts=facts
                        )
                        for covered in self._covering_repositories(resolution, touched_slugs)
                    ],
                    depth=request.depth,
                ),
                ledger=session.ledger.summary(),
            )

    def _edge_supply(self, edge: StoredEdge) -> ProvenanceSupply | None:
        """The asserting lines of one edge, prepared for the ledger, where provable.

        The same rule relationships applies: read from the indexed chunks, never
        the working tree, and only when they cover the whole asserting span.
        """
        if edge.file_id is None or edge.line_start is None or edge.line_end is None:
            return None
        chunks = self._workspace.knowledge.chunks_for_file(edge.file_id, 1, WHOLE_FILE_END)
        if not chunks:
            return None
        available = indexed_line_map(chunks)
        if any(n not in available for n in range(edge.line_start, edge.line_end + 1)):
            return None
        return self._provenance_supply(
            chunks[0].repository_slug, chunks[0].path, edge.line_start, edge.line_end, available
        )

    def verify_grounding(self, request: VerifyGroundingRequest) -> VerifyGroundingResponse:
        """Check an answer's citations against the evidence this investigation supplied.

        What a ``GROUNDED`` verdict means, stated so it cannot be read as more:
        every citation names evidence issued in **this** investigation, sits inside
        the span supplied under that id, agrees with that evidence's repository and
        path, and rests on evidence that still corresponds to the indexed snapshot
        this call read. What it does not mean is that the cited lines support the
        sentence, that the sentence is true, or that the investigation was complete.
        RepoAtlas consults no model here and the ledger retains no source text, so
        entailment is not weakly established — it is not established at all.

        A citation that fails is a **finding**, not an error. Naming a fabricated
        evidence id is the question the caller asked this capability to answer, and
        answering it is more useful than refusing the call: an answer holding one
        real citation and one invented one comes back partially grounded, with both
        named.

        The only state this touches is the standing of evidence that already
        exists. No id is allocated, no metadata strengthened, no repository fact
        written, and nothing is retrieved, inspected, traversed or read from a
        working tree.
        """
        with self._operation():
            validate_grounding_request(request)
            # Before the ledger is consulted, and never created: an evidence id
            # means nothing without the investigation that issued it, and
            # inventing an empty one to look in would report every citation as
            # fabricated while implying the caller's ids were wrong.
            session = self._sessions.get(request.investigation_id)
            registered = self._workspace.repositories.list_all()
            facts = current_facts_version(self._workspace)

            # One generation for the whole verdict. An external sync committing
            # mid-call must land entirely before these reads or entirely after
            # them; a verdict assembled from two generations would describe an
            # index that never existed, and would look exactly like a verdict.
            with self._workspace.database.read_snapshot():
                # Every file the ledger names, not only the cited ones. The
                # ledger revalidates as a whole, and a snapshot covering less
                # would mark every uncited entry UNVERIFIED — damaging session
                # state that this call was never asked about.
                snapshot = self._ledger_snapshot(session, registered)
                transitions = session.ledger.revalidate(snapshot)
                freshness = index_freshness(
                    [
                        repository_freshness_for(
                            self._workspace, repo, depth=FreshnessDepth.STORED, facts=facts
                        )
                        for repo in registered
                    ],
                    depth=FreshnessDepth.STORED,
                )

            checked_at = datetime.now(UTC)
            outcomes = self._resolve_citations(request.answer, session)
            # The routes this investigation was shown, which prose may name for
            # the same reason it may name a file this investigation was shown.
            routes = supplied_routes(session.ledger.entries())
            report = ground_answer(
                projected_answer(request.answer, outcomes=outcomes, routes=routes),
                supplied_set(
                    admitted={
                        outcome.evidence_id: entry
                        for outcome in outcomes.values()
                        if outcome.admitted
                        and (entry := session.ledger.get(outcome.evidence_id)) is not None
                    },
                    others=session.ledger.entries(),
                ),
            )
            return self._grounding_response(
                routes=routes,
                request=request,
                session=session,
                outcomes=outcomes,
                report=report,
                transitions=transitions,
                freshness=freshness,
                checked_at=checked_at,
            )

    # -- verification internals ---------------------------------------------

    @staticmethod
    def _resolve_citations(
        answer: AgentAnswer, session: InvestigationSession
    ) -> dict[tuple[int, int], CitationOutcome]:
        """Decide every citation, each against only the entry it names.

        The lookup goes through this investigation's ledger and no other. There is
        no id-to-evidence path that skips the investigation, which is what makes
        ``E1`` in one investigation and ``E1`` in another different facts rather
        than a collision to resolve.
        """
        outcomes: dict[tuple[int, int], CitationOutcome] = {}
        for claim_index, claim in enumerate(answer.claims):
            for citation_index, citation in enumerate(claim.citations):
                outcomes[(claim_index, citation_index)] = resolve_citation(
                    claim_index=claim_index,
                    citation_index=citation_index,
                    citation=citation,
                    entry=session.ledger.get(citation.evidence_id),
                    claimed_confidence=claim.claimed_confidence,
                )
        return outcomes

    def _grounding_response(
        self,
        *,
        request: VerifyGroundingRequest,
        session: InvestigationSession,
        outcomes: dict[tuple[int, int], CitationOutcome],
        report: GroundingReport,
        transitions: tuple[EvidenceTransition, ...],
        freshness: IndexFreshness,
        checked_at: datetime,
        routes: Sequence[str] = (),
    ) -> VerifyGroundingResponse:
        """Assemble the verdict, what it rests on, and what it leaves unsaid."""
        answer = request.answer
        findings: list[GroundingFinding] = []
        citations: list[CitationVerdict] = []
        for key in sorted(outcomes):
            outcome = outcomes[key]
            citations.append(
                CitationVerdict(
                    claim_index=outcome.claim_index,
                    citation_index=outcome.citation_index,
                    evidence_id=outcome.evidence_id,
                    grounded=outcome.grounded,
                    status=outcome.status,
                    submitted_range=outcome.submitted,
                    verified_range=outcome.verified,
                    location=outcome.location,
                    code=outcome.code,
                )
            )
            if outcome.code is not None:
                findings.append(
                    GroundingFinding(
                        code=outcome.code,
                        evidence_id=outcome.evidence_id,
                        claim_index=outcome.claim_index,
                        citation_index=outcome.citation_index,
                        status=outcome.status,
                        detail=outcome.detail,
                    )
                )

        for claim_index, claim in enumerate(answer.claims):
            if not claim.citations:
                findings.append(
                    GroundingFinding(
                        code=GroundingFindingCode.MISSING_CITATION,
                        claim_index=claim_index,
                        detail="a claim with no citation has no provenance to check",
                    )
                )

        # A claim whose citations every one resolved, and which V1 removed anyway.
        # The adapter handed V1 that claim with resolvable evidence at coordinates
        # it had already verified, so the prose rule is the only thing left that
        # can have removed it: the statement named a repository object — a file, a
        # line or a route — this investigation was never supplied. Which claim it
        # was is the whole value of the finding, so it carries its index; not a
        # word of the statement is repeated, for the same reason the count below
        # quotes nothing.
        aligned = aligned_hypotheses(answer, report, outcomes=outcomes, routes=routes)
        for claim_index, claim in enumerate(answer.claims):
            decided = [outcomes[(claim_index, index)] for index in range(len(claim.citations))]
            if not decided or not all(outcome.admitted for outcome in decided):
                continue
            if aligned[claim_index] is not None:
                continue
            findings.append(
                GroundingFinding(
                    code=GroundingFindingCode.PROSE_REFERENCE_NOT_SUPPLIED,
                    claim_index=claim_index,
                    detail=(
                        "the statement named a repository, file, line or route this "
                        "investigation never supplied"
                    ),
                )
            )

        # Everything V1 removed that was not a claim: the summary, an unknown, or
        # the next check, each because it named a repository object nobody
        # supplied. Counted rather than quoted — repeating the fabricated path
        # here would put it back on screen under RepoAtlas's own heading, which is
        # worse than where it started.
        prose_removals = max(0, len(report.unsupported) - report.demoted_findings)
        findings.extend(
            GroundingFinding(
                code=GroundingFindingCode.PROSE_REFERENCE_NOT_SUPPLIED,
                detail=(
                    "free text named a repository, file or line this investigation never supplied"
                ),
            )
            for _ in range(prose_removals)
        )

        statuses: list[EvidenceStatusView] = []
        seen: set[str] = set()
        for key in sorted(outcomes):
            evidence_id = outcomes[key].evidence_id
            if evidence_id in seen:
                continue
            seen.add(evidence_id)
            entry = session.ledger.get(evidence_id)
            statuses.append(
                EvidenceStatusView(
                    evidence_id=evidence_id,
                    status=entry.status if entry else None,
                    location=entry.ref.location if entry else "",
                    origins=entry.origins if entry else (),
                    superseded_by=entry.superseded_by if entry else None,
                )
            )

        return VerifyGroundingResponse(
            investigation_id=session.id,
            verdict=verdict_for(submitted=answer, outcomes=outcomes, report=report, routes=routes),
            original_answer=answer,
            sanitized_answer=sanitized_answer(answer, report, outcomes=outcomes, routes=routes),
            findings=tuple(findings),
            citations=tuple(citations),
            evidence_statuses=tuple(statuses),
            dropped_claims=report.demoted_findings,
            dropped_citations=sum(1 for outcome in outcomes.values() if not outcome.admitted),
            clamped_citations=sum(
                1 for outcome in outcomes.values() if outcome.admitted and not outcome.grounded
            ),
            ledger=session.ledger.summary(transitions),
            freshness=freshness,
            checked_at=checked_at,
        )

    # -- dependencies internals ---------------------------------------------

    def _dependency_page(
        self,
        request: DependenciesRequest,
        resolution: ContextResolution,
        limits: AppliedPageLimits,
    ) -> tuple[list[DependencyPackageView], _DependencyPage, list[ProvenanceSupply]]:
        """Build the whole ordered dataset, then cut one page out of it.

        The whole dataset first, deliberately. ``has_more`` is then something
        observed rather than inferred from a full page, the cursor's digest
        describes the set it was actually cut from, and the page is a prefix of an
        order settled before anything was fetched.

        The inventory is read once and globally. Scope narrows what is *returned*;
        it must not narrow what is *known*, because who publishes a package name is
        a fact about the managed root and a provider set assembled from a filtered
        view would report uniqueness the index never established.
        """
        stored = self._workspace.dependencies.list_packages(None)
        providers = provider_index(stored) if request.include_providers else {}
        in_scope = {repo.slug for repo in resolution.repositories}
        roles = set(admitted_roles(request))

        views: list[DependencyPackageView] = []
        for package in stored:
            slug = store_slug(package)
            if slug not in in_scope or not selects(package, request):
                continue
            observations = [
                self._dependency_observation(slug, observation)
                for observation in package.observations
                if observation.role in roles
            ]
            if not observations:
                # Every stored fact about this package was filtered out, so the
                # package is not in this answer at all. Returning it empty would
                # claim RepoAtlas knows a package it was not asked about.
                continue
            candidates = providers.get((package.ecosystem, package.normalized_name), [])
            views.append(
                DependencyPackageView(
                    repository_slug=slug,
                    repository_name=package.repository,
                    ecosystem=package.ecosystem,
                    name=package.name,
                    normalized_name=package.normalized_name,
                    observations=tuple(observations),
                    providers=tuple(self._provider_view(candidate) for candidate in candidates),
                    provider_resolution=provider_resolution(
                        package.ecosystem,
                        slug,
                        candidates,
                        declares=declares_package(package),
                    ),
                )
            )

        identity = request_digest(request, sorted(in_scope))
        inventory = inventory_digest(views)
        start = resume(views, request.cursor, request=identity, inventory=inventory)
        stop = start + limits.applied
        page = views[start:stop]
        supplies = self._dependency_supplies(page) if request.include_provenance else []
        return (
            views,
            _DependencyPage(
                packages=page,
                has_more=stop < len(views),
                identity=identity,
                inventory=inventory,
            ),
            supplies,
        )

    @staticmethod
    def _dependency_observation(
        slug: str, observation: DependencyObservation
    ) -> DependencyObservationView:
        """One stored fact, with the provenance the row carries and no citation yet.

        ``citable`` stays false here and is set only once text has actually been
        supplied and recorded. Provenance metadata exists for every dependency row;
        its presence is not a claim that anything is quotable.
        """
        return DependencyObservationView(
            repository_slug=slug,
            role=observation.role,
            value=observation.value,
            group=observation.group,
            scope=observation.scope,
            provenance=DependencyProvenance(
                repository_slug=slug,
                path=observation.source_path,
                line_start=observation.line,
                line_end=observation.line_end,
                parser=observation.parser,
                citable=False,
            ),
        )

    @staticmethod
    def _provider_view(candidate: ProviderCandidate) -> ProviderView:
        """One publishing repository, located by its own manifest line.

        The provenance is the *provision's*: the line in the provider's manifest
        that publishes the name. Never the declaration that went looking for it —
        those are two files making two statements, and citing one as proof of the
        other is the exact confusion this field exists to prevent.
        """
        return ProviderView(
            repository_slug=candidate.repository_slug,
            repository_name=candidate.repository_name,
            version=candidate.version,
            provenance=DependencyProvenance(
                repository_slug=candidate.repository_slug,
                path=candidate.path,
                line_start=candidate.line_start,
                line_end=candidate.line_end,
                parser=candidate.parser,
                citable=False,
            ),
        )

    def _dependency_supplies(self, page: Sequence[DependencyPackageView]) -> list[ProvenanceSupply]:
        """Supply the manifest and lockfile lines this page's facts rest on.

        One read per distinct stating file, over the whole file, because that
        single query answers both questions at once: which of its lines the stored
        chunks demonstrably contain, and under which snapshot they were parsed. It
        runs only for the page, so the work follows the answer rather than the
        inventory.

        Reconstructed from **indexed** chunks and never from the working tree. The
        dependency fact was parsed out of the indexed snapshot, so the line that
        proves it is the line that snapshot holds; reading the manifest as it is
        now could quote a constraint that replaced the one which produced the row.
        """
        wanted: dict[tuple[str, str], list[tuple[int, int]]] = {}
        for package in page:
            sources: list[DependencyProvenance] = [
                observation.provenance for observation in package.observations
            ]
            sources += [provider.provenance for provider in package.providers]
            for provenance in sources:
                if not provenance.path:
                    continue
                spans = wanted.setdefault((provenance.repository_slug, provenance.path), [])
                span = (provenance.line_start, provenance.line_end)
                if span not in spans:
                    spans.append(span)

        by_slug = {repo.slug: repo for repo in self._workspace.repositories.list_all()}
        supplies: list[ProvenanceSupply] = []
        for slug, path in sorted(wanted):
            repo = by_slug.get(slug)
            if repo is None or repo.id is None:
                continue
            state = self._workspace.files.states_for(repo.id, [path]).get(path)
            if state is None:
                continue
            chunks = self._workspace.knowledge.chunks_for_file(state.file_id, 1, WHOLE_FILE_END)
            available = indexed_line_map(chunks) if chunks else {}
            for line_start, line_end in sorted(wanted[(slug, path)]):
                if line_end < line_start:
                    continue
                numbers = range(line_start, line_end + 1)
                if any(number not in available for number in numbers):
                    # The chunks do not cover the whole stating span. Quoting the
                    # part they do cover would cite a narrower claim than the row
                    # makes, and stitching across the gap would cite text nobody
                    # was shown. The dependency fact stands; it is simply not
                    # quotable.
                    continue
                text = LINE_SEPARATOR.join(available[number] for number in numbers)
                supplies.append(
                    ProvenanceSupply(
                        proposed=ProposedEvidence(
                            ref=EvidenceRef(
                                repository_slug=slug,
                                path=path,
                                line_start=line_start,
                                line_end=line_end,
                                span_digest=chunk_hash(text),
                                file_content_hash=state.content_hash,
                                indexed_facts_version=state.parser_version,
                            ),
                            repository_name=repo.name,
                            # A manifest line is a fact about the repository, read
                            # from it. Nothing here was guessed at.
                            tier=EvidenceTier.DETERMINISTIC,
                            # The caller addressed a package identity and this is
                            # the line naming it — which is what this class means.
                            match=MatchClass.EXACT_NAME,
                            origin=EvidenceSource.DEPENDENCY,
                            reason=f"dependency source for {slug}/{path}:{line_start}-{line_end}",
                            text_length=len(text),
                        ),
                        text=text,
                    )
                )
        return supplies

    def _dependencies_response(
        self,
        *,
        request: DependenciesRequest,
        resolution: ContextResolution,
        session: InvestigationSession,
        views: Sequence[DependencyPackageView],
        page: _DependencyPage,
        supplies: Sequence[ProvenanceSupply],
        entries: tuple[LedgerEntry, ...],
        limits: AppliedPageLimits,
    ) -> DependenciesResponse:
        """Bind the recorded evidence to the facts that rest on it, then answer."""
        ids: dict[tuple[str, str, int, int], str] = {}
        items: list[EvidenceItem] = []
        for rank, (supply, entry) in enumerate(zip(supplies, entries, strict=True), start=1):
            ref = supply.proposed.ref
            assert ref.line_start is not None and ref.line_end is not None
            ids[(ref.repository_slug, ref.path, ref.line_start, ref.line_end)] = entry.id
            items.append(
                EvidenceItem(
                    id=entry.id,
                    # Supply order, and nothing more. Dependencies searched for
                    # nothing, so there is no relevance for a rank to express.
                    rank=rank,
                    ref=ref,
                    repository_name=supply.proposed.repository_name,
                    location=ref.location,
                    text=supply.text,
                    why=supply.proposed.reason,
                    tier=supply.proposed.tier,
                    match=supply.proposed.match,
                    status=entry.status,
                )
            )

        def bind(provenance: DependencyProvenance) -> tuple[DependencyProvenance, tuple[str, ...]]:
            key = (
                provenance.repository_slug,
                provenance.path,
                provenance.line_start,
                provenance.line_end,
            )
            evidence_id = ids.get(key)
            if evidence_id is None:
                return (provenance, ())
            return (provenance.model_copy(update={"citable": True}), (evidence_id,))

        bound: list[DependencyPackageView] = []
        for package in page.packages:
            observations = []
            for observation in package.observations:
                provenance, evidence_ids = bind(observation.provenance)
                observations.append(
                    observation.model_copy(
                        update={"provenance": provenance, "evidence_ids": evidence_ids}
                    )
                )
            providers = []
            for provider in package.providers:
                provenance, evidence_ids = bind(provider.provenance)
                providers.append(
                    provider.model_copy(
                        update={"provenance": provenance, "evidence_ids": evidence_ids}
                    )
                )
            bound.append(
                package.model_copy(
                    update={"observations": tuple(observations), "providers": tuple(providers)}
                )
            )

        facts = current_facts_version(self._workspace)
        freshness = index_freshness(
            [
                repository_freshness_for(self._workspace, repo, depth=request.depth, facts=facts)
                for repo in resolution.repositories
            ],
            depth=request.depth,
        )
        return DependenciesResponse(
            investigation_id=session.id,
            context=ContextView(
                context=request.context,
                scope=resolution.scope,
                repositories=tuple(sorted(repo.slug for repo in resolution.repositories)),
                primary=resolution.primary.slug if resolution.primary else None,
                reason=resolution.reason,
            ),
            packages=tuple(bound),
            evidence=tuple(items),
            freshness=freshness,
            next_cursor=(
                encode_cursor(
                    request=page.identity,
                    inventory=page.inventory,
                    after=package_key(page.packages[-1]),
                )
                if page.has_more and page.packages
                else None
            ),
            has_more=page.has_more,
            limits=limits,
            ledger=session.ledger.summary(),
        )

    def _http_inventory(
        self, repository: Repository, slugs: Mapping[int, str] | None = None
    ) -> http_inventory.Inventory:
        """Replan one repository and read what its rows need. Inside the snapshot."""
        assert repository.id is not None
        if slugs is None:
            registered = self._workspace.repositories.list_all()
            slugs = {repo.id: repo.slug for repo in registered if repo.id is not None}
        knowledge = self._workspace.knowledge
        planned = plan_repository_http(knowledge, repository.id)
        edges = knowledge.list_http_edges(repository.id)
        decisions = knowledge.list_http_call_decisions(repository.id)
        wanted = {claim.src_id for claim in planned.claims}
        wanted.update(row.src_id for row in decisions)
        wanted.update(http_inventory.handler_ids(decisions))
        return http_inventory.build_inventory(
            plan=planned.plan,
            claims=planned.claims,
            entities=knowledge.keys_for_entities(sorted(wanted)),
            files=planned.files,
            index=planned.index,
            edges=edges,
            parsers=dict(knowledge.list_file_parsers(repository.id)),
            joined=decisions,
            slugs=slugs,
            crossings_built=knowledge.http_call_decisions_built(),
        )

    def _http_operation_supplies(
        self, repository: Repository, spans: Mapping[tuple[str, int, int], http_inventory.Span]
    ) -> list[ProvenanceSupply]:
        """Supply each span of one repository, whole or not at all.

        A row's own lines, where the request is written or the route declared, and
        the lines a crossing's proven hop cites, reconstructed from indexed chunks and
        never from the working tree, because the row was planned from the indexed
        snapshot. A span the chunks do not cover in full is not quoted: a prefix of a
        call could cut off the very path it names. What the lines do not show — a
        helper default, a prefix, the server's spelling — is not in them, and the
        row's ``composed`` says so.
        """
        assert repository.id is not None
        wanted: dict[str, list[tuple[int, int]]] = {}
        for path, line_start, line_end in spans:
            wanted.setdefault(path, []).append((line_start, line_end))
        supplies: list[ProvenanceSupply] = []
        states = self._workspace.files.states_for(repository.id, sorted(wanted))
        for path in sorted(wanted):
            state = states.get(path)
            if state is None:
                continue
            chunks = self._workspace.knowledge.chunks_for_file(state.file_id, 1, WHOLE_FILE_END)
            available = indexed_line_map(chunks) if chunks else {}
            for line_start, line_end in sorted(wanted[path]):
                numbers = range(line_start, line_end + 1)
                if line_end < line_start or any(n not in available for n in numbers):
                    continue
                text = LINE_SEPARATOR.join(available[number] for number in numbers)
                # Which operations the span was supplied for. One span may state two
                # rows, and grounding later asks what this investigation was shown,
                # not which row asked for it first.
                span = spans[(path, line_start, line_end)]
                written = "HTTP operation as written" if span.written else "HTTP crossing hop"
                supplies.append(
                    ProvenanceSupply(
                        proposed=ProposedEvidence(
                            ref=EvidenceRef(
                                repository_slug=repository.slug,
                                path=path,
                                line_start=line_start,
                                line_end=line_end,
                                span_digest=chunk_hash(text),
                                file_content_hash=state.content_hash,
                                indexed_facts_version=state.parser_version,
                            ),
                            repository_name=repository.name,
                            tier=EvidenceTier.DETERMINISTIC,
                            # Reached by structure from the repository the caller
                            # named, not by matching any text of the question.
                            match=MatchClass.RELATED,
                            origin=EvidenceSource.HTTP_OPERATION,
                            reason=(
                                f"{written} at {repository.slug}/{path}:{line_start}-{line_end}"
                            ),
                            text_length=len(text),
                            routes=tuple(sorted(span.routes)),
                        ),
                        text=text,
                    )
                )
        return supplies

    def _http_operations_response(
        self,
        *,
        request: HttpOperationsRequest,
        resolution: ContextResolution,
        registered: Sequence[Repository],
        session: InvestigationSession,
        repository: Repository | None,
        inventory: http_inventory.Inventory,
        chosen: Sequence[http_inventory.Draft],
        page: http_inventory.Page,
        supplies: Sequence[ProvenanceSupply],
        entries: tuple[LedgerEntry, ...],
        limits: AppliedPageLimits,
        prefix: str | None,
    ) -> HttpOperationsResponse:
        """Bind recorded evidence to the rows whose lines it is, then answer."""
        ids: dict[tuple[str, str, int, int], str] = {}
        items: list[EvidenceItem] = []
        for rank, (supply, entry) in enumerate(zip(supplies, entries, strict=True), start=1):
            ref = supply.proposed.ref
            assert ref.line_start is not None and ref.line_end is not None
            ids[(ref.repository_slug, ref.path, ref.line_start, ref.line_end)] = entry.id
            items.append(
                EvidenceItem(
                    id=entry.id,
                    # Supply order only: nothing was searched, so nothing is ranked.
                    rank=rank,
                    ref=ref,
                    repository_name=supply.proposed.repository_name,
                    location=ref.location,
                    text=supply.text,
                    why=supply.proposed.reason,
                    tier=supply.proposed.tier,
                    match=supply.proposed.match,
                    status=entry.status,
                )
            )
        home = repository.slug if repository else None
        rows = [http_inventory.with_evidence(row, ids, home) for row in page.rows]
        # Every repository the answer shows a fact of, not only the one named: a
        # crossing shown across repositories rests on their index too.
        drawn = list(resolution.repositories)
        if request.cross_repository:
            named = http_inventory.repositories_named(rows) - {repo.slug for repo in drawn}
            drawn += [repo for repo in registered if repo.slug in named]
        facts = current_facts_version(self._workspace)
        freshness = index_freshness(
            [
                repository_freshness_for(self._workspace, repo, depth=request.depth, facts=facts)
                for repo in drawn
            ],
            depth=request.depth,
        )
        crossings = CrossingBasis(
            built=inventory.crossings_built,
            projection=(
                "global"
                if resolution.scope is CapabilityScope.GLOBAL
                else "cross_repository"
                if request.cross_repository
                else "own_repository"
            ),
        )
        return HttpOperationsResponse(
            investigation_id=session.id,
            context=ContextView(
                context=request.context,
                scope=resolution.scope,
                repositories=tuple(sorted(repo.slug for repo in resolution.repositories)),
                primary=resolution.primary.slug if resolution.primary else None,
                reason=resolution.reason,
            ),
            basis=(
                OperationBasis(repository=repository.slug, crossings=crossings)
                if repository
                else DiscoveryBasis(
                    repositories=tuple(sorted(repo.slug for repo in resolution.repositories)),
                    repositories_examined=len(resolution.repositories),
                    operations_examined=len(inventory.drafts),
                    crossings=crossings,
                )
            ),
            summary=http_inventory.summarize(chosen),
            operations=tuple(rows),
            coverage=http_inventory.coverage(
                inventory, http_inventory.in_scope(inventory.drafts, prefix), prefix
            ),
            evidence=tuple(items),
            freshness=freshness,
            next_cursor=(
                http_inventory.encode_cursor(
                    request=page.identity, inventory=page.inventory, after=page.after
                )
                if page.has_more and page.after is not None
                else None
            ),
            has_more=page.has_more,
            limits=limits,
            ledger=session.ledger.summary(),
        )

    # -- internals ----------------------------------------------------------

    @contextmanager
    def _operation(self) -> Iterator[None]:
        """Serialize one public operation and refuse a closed service.

        The lock is taken **before** anything reaches the workspace, so the
        serialization boundary sits above every database access rather than
        beside it. The closed check is inside the lock for the same reason: a
        concurrent :meth:`close` must either be fully before this call or fully
        after it.

        ``with`` releases on the way out however that happens, so a failed call
        cannot leave the service wedged for the next one.
        """
        with self._call_lock:
            if self._closed:
                raise ServiceClosedError()
            yield

    def _propose(
        self, shaped: list[ShapedEvidence], registered: list[Repository]
    ) -> list[ProposedEvidence]:
        """Turn shaped evidence into the exact batch the ledger will record.

        Every reference carries the indexed snapshot it was taken from — that
        file's content hash and the facts version *that file was indexed under* —
        because that is what lets a later revalidation say whether the evidence
        still describes what is there. Both are read from the **indexed** file row
        and never from the working tree: the claim being recorded is about what
        RepoAtlas searched, not about what happens to be on disk now.

        The facts version is the file's own, not the running build's, and the
        difference is a real one whenever the index is a build behind. A file
        parsed under an older version supplied text that carries that version's
        facts; stamping today's version on it would describe a snapshot that never
        produced this evidence, and would then make it revalidate as stale against
        the very row it came from. That the build has moved on is a separate,
        already-reported fact, and freshness is where it belongs — encoding it by
        misdescribing the evidence would put one fact in two places and make one
        of them false.
        """
        by_slug = {repo.slug: repo for repo in registered}
        wanted: dict[int, set[str]] = {}
        for item in shaped:
            repo = by_slug.get(item.candidate.repository_slug)
            if repo is not None and repo.id is not None:
                wanted.setdefault(repo.id, set()).add(item.candidate.path)

        states: dict[tuple[str, str], FileState] = {}
        for repo_id, paths in wanted.items():
            slug = next(slug for slug, repo in by_slug.items() if repo.id == repo_id)
            for path, state in self._workspace.files.states_for(repo_id, sorted(paths)).items():
                states[(slug, path)] = state

        # A declaration is told apart from every other claim on its file by the
        # entity it declares (ProposedEvidence.declares). Resolved here, inside the
        # same read view as the text, and by row id rather than by name.
        declared = self._workspace.knowledge.keys_for_entities(
            sorted(
                {
                    item.candidate.entity_id
                    for item in shaped
                    if item.candidate.declaration and item.candidate.entity_id is not None
                }
            )
        )

        proposed: list[ProposedEvidence] = []
        for item in shaped:
            candidate = item.candidate
            state = states.get((candidate.repository_slug, candidate.path))
            if state is None:
                # Retrieval only ever returns rows the index holds, so a candidate
                # with no file row means the search index and the file table
                # disagree. Reported rather than papered over with empty strings:
                # a reference carrying a blank hash would revalidate as stale
                # forever and look like ordinary drift.
                raise IndexInconsistencyError(f"{candidate.repository_slug}/{candidate.path}")
            declares: str | None = None
            if candidate.declaration:
                identity = (
                    declared.get(candidate.entity_id) if candidate.entity_id is not None else None
                )
                # The entity's natural key when it resolves. When it does not, the
                # evidence still stands — failing to name an entity is never a
                # reason to withhold what was found (see _entity_handles) — and the
                # declared span stands in for the key: still a fact about the
                # source rather than an invented identity, and distinct for every
                # declaration that does not share its exact lines.
                declares = (
                    identity.key
                    if identity is not None and identity.key
                    else f"lines:{candidate.line_start}-{candidate.line_end}"
                )
            proposed.append(
                ProposedEvidence(
                    ref=EvidenceRef(
                        repository_slug=candidate.repository_slug,
                        path=candidate.path,
                        line_start=item.line_start,
                        line_end=item.line_end,
                        # Over exactly the characters the caller receives. Not the
                        # untruncated candidate: the digest has to identify what
                        # was supplied, or re-supplying a shorter prefix would
                        # silently reuse an id bound to text nobody saw.
                        span_digest=chunk_hash(item.text),
                        file_content_hash=state.content_hash,
                        indexed_facts_version=state.parser_version,
                    ),
                    repository_name=candidate.repository,
                    tier=candidate.tier,
                    match=candidate.match,
                    origin=candidate.source,
                    reason=candidate.description,
                    text_length=len(item.text),
                    declares=declares,
                )
            )
        return proposed

    def _search_response(
        self,
        *,
        request: SearchRequest,
        resolution: ContextResolution,
        session: InvestigationSession,
        shaped: ShapingResult,
        entries: tuple[LedgerEntry, ...],
        proposed: list[ProposedEvidence],
        outcome: RetrievalOutcome,
        applied: AppliedBudget,
        facts: str,
        path_prefix: str | None = None,
    ) -> SearchResponse:
        """Assemble the response from **this run's** candidates and the new ids.

        Built from the shaped candidates, with only the identity and status taken
        from the ledger entries. Serializing the entries themselves would report
        the strongest metadata the investigation has ever accumulated for a span
        as though this query had established it — true of the ledger, false of the
        search.
        """
        in_scope = frozenset(repo.slug for repo in resolution.repositories)
        handles = self._entity_handles(shaped.items, in_scope)
        incomplete = shaped.truncated or outcome.evidence.truncated
        items = [
            EvidenceItem(
                id=entry.id,
                rank=rank,
                ref=item_ref,
                repository_name=shaped_item.candidate.repository,
                location=item_ref.location,
                text=shaped_item.text,
                truncated=shaped_item.truncated,
                why=shaped_item.candidate.description,
                tier=shaped_item.candidate.tier,
                match=shaped_item.candidate.match,
                declaration=shaped_item.candidate.declaration,
                preview=shaped_item.candidate.preview,
                entity=handles.get(id(shaped_item)),
                status=entry.status,
            )
            for rank, (shaped_item, entry, item_ref) in enumerate(
                ((s, e, p.ref) for s, e, p in zip(shaped.items, entries, proposed, strict=True)),
                start=1,
            )
        ]

        supplied = (item.ref.repository_slug for item in items)
        freshness = index_freshness(
            [
                repository_freshness_for(self._workspace, repo, depth=request.depth, facts=facts)
                for repo in self._covering_repositories(resolution, supplied)
            ],
            depth=request.depth,
        )
        return SearchResponse(
            investigation_id=session.id,
            question=request.question,
            context=ContextView(
                context=request.context,
                scope=resolution.scope,
                repositories=tuple(sorted(repo.slug for repo in resolution.repositories)),
                primary=resolution.primary.slug if resolution.primary else None,
                reason=resolution.reason,
            ),
            evidence=tuple(items),
            freshness=freshness,
            budget=applied,
            truncated=incomplete,
            has_more=incomplete,
            suppressed=outcome.suppression,
            entities=self._seed_handles(outcome.seed_entity_ids, in_scope),
            trace=outcome.evidence.trace if request.include_trace else None,
            ledger=session.ledger.summary(),
            within_path=path_prefix,
            terms=outcome.term_matches,
        )

    def _seed_handles(
        self, seed_entity_ids: tuple[int, ...], in_scope: frozenset[str]
    ) -> tuple[EntityHandle, ...]:
        """Natural handles for the exact-name hits that seeded graph expansion.

        These are the ``search`` → ``relationships`` handoff, and the reason they
        are published separately from evidence is that the best of them are never
        evidence: an interned identity has no file and no line, so the retriever
        admits it as a seed and produces no candidate for it. Returning it here
        gives the caller the hub it actually wants to traverse from, without
        RepoAtlas inventing a citation for something no file supplies.

        A handle is published only if ``relationships`` will accept it under the
        same context (:func:`_resolvable_in`). Offering one it must refuse would
        break the handoff this list exists for, and the refusal would look like a
        key that moved rather than a key from somewhere else.

        Order follows the retriever's own seed order, which is the order the
        question mentioned them in.
        """
        if not seed_entity_ids:
            return ()
        identities = self._workspace.knowledge.keys_for_entities(sorted(set(seed_entity_ids)))
        handles: list[EntityHandle] = []
        seen: set[str] = set()
        for entity_id in seed_entity_ids:
            identity = identities.get(entity_id)
            if identity is None or not identity.key or identity.key in seen:
                continue
            if not _resolvable_in(identity, in_scope):
                continue
            seen.add(identity.key)
            handles.append(
                EntityHandle(
                    key=identity.key,
                    type=identity.type,
                    positional=is_positional(identity.key),
                )
            )
        return tuple(handles)

    def _entity_handles(
        self, shaped: list[ShapedEvidence], in_scope: frozenset[str]
    ) -> dict[int, EntityHandle]:
        """Resolve entity-backed evidence to natural keys, in one query.

        Exact resolution by row id through the Slice 0 identity read — never a
        lookup by name. A name search here would be a second, fuzzier answer to a
        question the retriever already answered exactly, and it could hand back a
        different entity that merely shares a name.

        An id that does not resolve yields no handle. The evidence still stands:
        failing to name the entity behind a span is not a reason to withhold the
        span, and inventing a plausible key would be worse than saying nothing. The
        same holds for a key ``relationships`` would refuse under this context
        (:func:`_resolvable_in`): the span is still evidence, the handle is not one.
        """
        wanted = {
            item.candidate.entity_id for item in shaped if item.candidate.entity_id is not None
        }
        if not wanted:
            return {}
        identities = self._workspace.knowledge.keys_for_entities(sorted(wanted))
        handles: dict[int, EntityHandle] = {}
        for item in shaped:
            entity_id = item.candidate.entity_id
            if entity_id is None:
                continue
            identity = identities.get(entity_id)
            if identity is None or not identity.key or not _resolvable_in(identity, in_scope):
                continue
            handles[id(item)] = EntityHandle(
                key=identity.key,
                type=identity.type,
                positional=is_positional(identity.key),
            )
        return handles

    def _covering_repositories(
        self, resolution: ContextResolution, supplied: Iterable[str]
    ) -> list[Repository]:
        """The repositories a response's freshness must describe.

        The scope the caller named, and then every repository this response drew
        anything from. An agent reads the freshness of evidence from the response
        that supplied it, so evidence from outside the scope — reached across a
        shared node with crossing enabled, say — must not arrive under a freshness
        report that says nothing about where it came from. Scope first, in
        resolution order; anything else after it, by identity.
        """
        covered = list(resolution.repositories)
        extra = sorted(set(supplied) - {repo.slug for repo in covered})
        if extra:
            by_slug = {repo.slug: repo for repo in self._workspace.repositories.list_all()}
            covered.extend(by_slug[slug] for slug in extra if slug in by_slug)
        return covered

    def _repository_view(
        self,
        repo: Repository,
        *,
        depth: FreshnessDepth,
        facts: str,
        name_ambiguous: bool,
        include_host_path: bool,
    ) -> RepositoryView:
        repo_ids = [repo.id] if repo.id is not None else []
        return RepositoryView(
            slug=repo.slug,
            name=repo.name,
            name_ambiguous=name_ambiguous,
            branch=repo.branch,
            head_sha=repo.head_sha,
            languages=repo.languages,
            manifests=repo.manifests,
            lockfiles=repo.lockfiles,
            indexed_files=self._workspace.files.count(repo_ids),
            symbols=self._workspace.knowledge.count_entities(repo_ids),
            relationships=self._workspace.knowledge.count_edges(repo_ids),
            last_indexed_at=repo.last_indexed_at,
            freshness=repository_freshness_for(self._workspace, repo, depth=depth, facts=facts),
            host_path=repo.path if include_host_path else None,
        )

    def _counts(self, resolution: ContextResolution, registered: list[Repository]) -> IndexCounts:
        """Indexed totals for the resolved scope.

        ``repositories`` counts the whole registered set in ``GLOBAL`` scope and
        the selection in ``REPOSITORY`` scope, matching what V1's status reports.
        """
        repo_ids = list(resolution.repository_ids)
        in_scope = (
            len(resolution.repositories)
            if resolution.scope is CapabilityScope.REPOSITORY
            else len(registered)
        )
        return IndexCounts(
            repositories=in_scope,
            indexed_files=self._workspace.files.count(repo_ids),
            symbols=self._workspace.knowledge.count_entities(repo_ids),
            relationships=self._workspace.knowledge.count_edges(repo_ids),
        )

    # -- inspection internals -----------------------------------------------

    def _resolve_inspect_target(
        self,
        request: InspectRequest,
        resolution: ContextResolution,
        registered: list[Repository],
        session: InvestigationSession,
    ) -> _InspectTarget:
        """Turn one of the three target forms into a repository, a path and a range.

        Each form resolves **exactly**. A path is looked up as itself, an entity
        key as itself, and an evidence id inside the investigation that issued it.
        None of them falls back to a search, because a fallback would answer about
        something the caller did not name while looking exactly like the case
        where it did.
        """
        target = request.target
        in_scope = {repo.slug for repo in resolution.repositories}

        if target.kind is InspectTargetKind.PATH:
            assert target.path is not None
            if resolution.scope is not CapabilityScope.REPOSITORY:
                # A repository-relative path is ambiguous across a managed root by
                # definition, and searching every repository for one would be a
                # path search with a single winner picked by luck. V1's own
                # vocabulary for "you named something that identifies more than
                # one repository" applies unchanged.
                raise ContextResolutionError(
                    "a repository-relative path needs a repository scope",
                    detail="Name the repository this path belongs to.",
                    reason=ContextFailure.AMBIGUOUS,
                    candidates=tuple(sorted(in_scope)),
                )
            repo = resolution.repositories[0]
            return _InspectTarget(
                repository=repo,
                path=_index_path(target.path),
                base=None,
                evidence_status=None,
                match=MatchClass.EXACT_PATH,
                reason=f"inspected {repo.slug}/{_index_path(target.path)}",
            )

        if target.kind is InspectTargetKind.ENTITY:
            assert target.entity_key is not None
            return self._resolve_entity_target(target.entity_key, registered, in_scope)

        assert target.evidence_id is not None
        return self._resolve_evidence_target(target.evidence_id, session, registered, in_scope)

    def _resolve_entity_target(
        self, key: str, registered: list[Repository], in_scope: set[str]
    ) -> _InspectTarget:
        """One exact natural key, or a refusal.

        The lookup is key-only. Resolving through a name search would answer with
        whatever happens to share the name — which for a ``sym:`` key whose
        definition has moved means confidently describing a different definition
        than the one the caller held a handle to.
        """
        identities = self._workspace.knowledge.get_entities_by_key([key])
        if not identities:
            raise EntityNotFoundError()
        if len(identities) > 1:
            # The schema enforces key uniqueness only for interned entities, so
            # this is possible rather than hypothetical. Returned, never decided.
            raise EntityAmbiguousError(
                candidates=tuple(
                    sorted(
                        f"{identity.repository_slug or ''}/{identity.path or ''}"
                        for identity in identities
                    )
                )
            )
        identity = identities[0]
        if identity.repository_slug is None or identity.path is None:
            # Interned: an environment variable, a module name, a package. Real,
            # referenced by files, defined in none of them.
            raise EntityNotInspectableError()

        repo = next((r for r in registered if r.slug == identity.repository_slug), None)
        if repo is None:
            raise FileNotIndexedError()
        if repo.slug not in in_scope:
            raise TargetOutOfScopeError()

        base = (
            LineRange(start=identity.line_start, end=identity.line_end)
            if identity.line_start is not None
            and identity.line_end is not None
            and identity.line_end >= identity.line_start
            else None
        )
        return _InspectTarget(
            repository=repo,
            path=_index_path(identity.path),
            base=base,
            evidence_status=None,
            match=MatchClass.EXACT_NAME,
            reason=f"inspected the source of {key}",
        )

    def _resolve_evidence_target(
        self,
        evidence_id: str,
        session: InvestigationSession,
        registered: list[Repository],
        in_scope: set[str],
    ) -> _InspectTarget:
        """The coordinates one evidence id referred to, as they stand **now**.

        An evidence id is not a handle on historical bytes. The ledger keeps a
        digest and a reference, never text, so ``E7`` names a place and the bytes
        that were once there — and inspecting it means inspecting what the index
        holds at that place today. Revalidation happens first so the caller is
        told which of those two it is getting.

        A ``STALE`` or ``SUPERSEDED`` target is inspected rather than refused:
        widening from coordinates that have moved on is a legitimate and common
        thing to want. What must not happen is ``E7`` quietly coming to mean the
        new bytes, so the current text is supplied under its own new ids and
        ``E7`` keeps its identity, its digest and its status.
        """
        if session.ledger.get(evidence_id) is None:
            raise EvidenceNotFoundError()

        # Against a snapshot of every file the ledger refers to, not just this
        # one: a partial snapshot would mark every other entry UNVERIFIED, which
        # is a claim about evidence this call never looked at.
        session.ledger.revalidate(self._ledger_snapshot(session, registered))
        entry = session.ledger.get(evidence_id)
        assert entry is not None

        if entry.status is EvidenceStatus.INVALID:
            # No file stands at those coordinates any more. There is deliberately
            # no search for a moved or similarly named file: evidence is attributed
            # to an identity and a path, and a different file is a different thing.
            raise FileNotIndexedError()

        repo = next((r for r in registered if r.slug == entry.ref.repository_slug), None)
        if repo is None:
            raise FileNotIndexedError()
        if repo.slug not in in_scope:
            raise TargetOutOfScopeError()

        base = (
            LineRange(start=entry.ref.line_start, end=entry.ref.line_end)
            if entry.ref.line_start is not None and entry.ref.line_end is not None
            else None
        )
        return _InspectTarget(
            repository=repo,
            path=entry.ref.path,
            base=base,
            evidence_status=entry.status,
            match=MatchClass.EXACT_PATH,
            reason=f"inspected indexed context around {evidence_id}",
        )

    def _ledger_snapshot(
        self, session: InvestigationSession, registered: list[Repository]
    ) -> dict[tuple[str, str], IndexedEvidenceState]:
        """Current indexed state for every file this investigation cites."""
        by_slug = {repo.slug: repo for repo in registered}
        wanted: dict[str, set[str]] = {}
        for entry in session.ledger.entries():
            wanted.setdefault(entry.ref.repository_slug, set()).add(entry.ref.path)

        snapshot: dict[tuple[str, str], IndexedEvidenceState] = {}
        for slug, paths in wanted.items():
            repo = by_slug.get(slug)
            states: dict[str, FileState] = {}
            if repo is not None and repo.id is not None:
                states = self._workspace.files.states_for(repo.id, sorted(paths))
            for path in paths:
                state = states.get(path)
                snapshot[(slug, path)] = IndexedEvidenceState(
                    repository_slug=slug,
                    path=path,
                    exists=state is not None,
                    content_hash=state.content_hash if state else "",
                    parser_version=state.parser_version if state else "",
                )
        return snapshot

    def _inspect_source(self, target: _InspectTarget, request: InspectRequest) -> _InspectedSource:
        """Choose one source for this file, and prove the choice.

        The order is fixed and each step is a gate rather than a preference:

        1. the **indexed file row**. No row, no inspection — this is the admission
           boundary, and it is consulted before any repository file is opened;
        2. the **verified working tree**, only when the caller allowed it and only
           when the bytes read hash equal to the indexed content hash. A mismatch
           is not a reason to prefer live data; it is the end of that option, and
           not one of those bytes is decoded, measured, previewed or described;
        3. the **indexed chunks** — what RepoAtlas actually searched.

        One source per inspection, never a blend. Splicing verified lines from the
        working tree together with indexed lines from before the last edit would
        return a file state that existed neither on disk nor in the index, with
        every line of it looking equally trustworthy.
        """
        for _ in range(_SNAPSHOT_ATTEMPTS):
            state = self._indexed_file(target.repository, target.path)
            padded = pad(target.base, request.context_lines) if target.base else None
            if request.explicit_range is not None:
                padded = pad(request.explicit_range, request.context_lines)

            worktree_text: str | None = None
            matches: bool | None = None
            if request.allow_worktree:
                worktree_text, matches = self._verified_worktree_text(target, state)

            if worktree_text is not None:
                # Text and snapshot are coherent by construction rather than by
                # timing: these exact bytes hashed to this exact row's content
                # hash, and the parser version came from that same row read. A
                # sync landing after this point makes the evidence stale, which is
                # what revalidation is for — it cannot make it self-contradictory.
                available = worktree_line_map(worktree_text)
                line_count = len(available)
                return _InspectedSource(
                    state=state,
                    available=available,
                    line_count=line_count,
                    source=InspectionSource.VERIFIED_WORKTREE,
                    worktree_matches_index=matches,
                    requested=_whole_or_clamped(padded, line_count),
                )

            chunks = self._workspace.knowledge.chunks_for_file(
                state.file_id,
                padded.start if padded else 1,
                padded.end if padded else WHOLE_FILE_END,
            )
            confirm = self._indexed_file(target.repository, target.path)
            if _snapshot_key(confirm) != _snapshot_key(state):
                # Another process reindexed this file between the two reads, so
                # the chunks in hand belong to one generation and the hash and
                # parser version to another. Pairing them would produce evidence
                # describing a snapshot that never existed — and it would
                # revalidate cleanly, because its own hash would match. Read again.
                continue

            available = indexed_line_map(chunks)
            return _InspectedSource(
                state=state,
                available=available,
                # Chunks establish which lines exist, never how many the file has:
                # a span with no text is not stored, and the last stored line is
                # not the last line.
                line_count=None,
                source=InspectionSource.INDEXED_CHUNKS,
                worktree_matches_index=matches,
                requested=padded
                if padded
                else (LineRange(start=1, end=max(available)) if available else None),
            )
        raise IndexChangedError()

    def _indexed_file(self, repo: Repository, path: str) -> FileState:
        """The exact ``files`` row, or the one refusal every unindexed path gets."""
        if repo.id is None:
            raise FileNotIndexedError()
        state = self._workspace.files.states_for(repo.id, [path]).get(path)
        if state is None:
            raise FileNotIndexedError()
        return state

    def _verified_worktree_text(
        self, target: _InspectTarget, state: FileState
    ) -> tuple[str | None, bool | None]:
        """Working-tree text, only if it is provably the indexed bytes.

        Returns the text and whether the comparison was made and passed. ``None``
        for the flag means it was not made at all — the file could not be read
        within the boundary, or was too large, or changed mid-read — which is a
        different fact from a mismatch and is reported as one.

        Read once through the containment boundary, hashed with the primitive that
        produced ``files.content_hash``, and sliced from those same bytes. There is
        no stat, no size comparison and no Git dirty flag anywhere in this path:
        each of those is a hint about whether content changed, and a hint is not
        the proof that lets live text be cited against an indexed snapshot. Reading
        twice would be worse still, since the bytes returned would not be the bytes
        that were hashed.
        """
        boundary = RepositoryBoundary(target.repository.path)
        found = boundary.read_content(
            target.path, max_bytes=self._workspace.settings.indexing.max_file_bytes
        )
        if not found.ok or found.data is None:
            return (None, None)

        raw = found.data
        if hash_bytes(raw) != state.content_hash:
            # The one branch that matters most. Nothing derived from these bytes
            # leaves this method — not a line, not a count, not a length.
            return (None, False)
        if b"\x00" in raw[:BINARY_SNIFF_BYTES]:
            # The identical predicate the indexer applied to these identical
            # bytes, so the verdict is the same one: no text was stored for this
            # file, and inventing line numbers for it here would contradict the
            # index rather than extend it.
            return (None, True)
        try:
            return (raw.decode("utf-8"), True)
        except UnicodeDecodeError:
            # Indexing reached the same conclusion about the same bytes and stored
            # no facts. Inspect does not get its own decoder.
            return (None, True)

    def _propose_segments(
        self, target: _InspectTarget, source: _InspectedSource, shaped: ShapedInspection
    ) -> list[ProposedEvidence]:
        """The exact batch the ledger will record, built from the returned text."""
        return [
            ProposedEvidence(
                ref=EvidenceRef(
                    repository_slug=target.repository.slug,
                    path=target.path,
                    line_start=segment.line_start,
                    line_end=segment.line_end,
                    # Over exactly the characters the caller receives, so a
                    # truncated segment is recognisably a different supply from
                    # the whole one.
                    span_digest=chunk_hash(segment.text),
                    file_content_hash=source.state.content_hash,
                    # The version behind *this file's* stored facts, which is the
                    # snapshot the text was taken from.
                    indexed_facts_version=source.state.parser_version,
                ),
                repository_name=target.repository.name,
                # Addressed, not guessed at. Inspection reads what the caller
                # named; there is no similarity anywhere in this path.
                tier=EvidenceTier.DETERMINISTIC,
                match=target.match,
                origin=EvidenceSource.INSPECT,
                reason=target.reason,
                text_length=len(segment.text),
            )
            for segment in shaped.segments
        ]

    def _inspect_response(
        self,
        *,
        request: InspectRequest,
        resolution: ContextResolution,
        session: InvestigationSession,
        target: _InspectTarget,
        source: _InspectedSource,
        shaped: ShapedInspection,
        entries: tuple[LedgerEntry, ...],
        proposed: list[ProposedEvidence],
        applied: AppliedBudget,
    ) -> InspectResponse:
        """Assemble the response from **this** inspection and the new ids."""
        segments = [
            EvidenceItem(
                id=entry.id,
                # Source order, and nothing more. Inspection searched for nothing,
                # so there is no relevance here for a rank to express.
                rank=rank,
                ref=item_ref,
                repository_name=target.repository.name,
                location=item_ref.location,
                text=piece.text,
                truncated=piece.truncated,
                why=target.reason,
                tier=EvidenceTier.DETERMINISTIC,
                match=target.match,
                status=entry.status,
            )
            for rank, (piece, entry, item_ref) in enumerate(
                ((s, e, p.ref) for s, e, p in zip(shaped.segments, entries, proposed, strict=True)),
                start=1,
            )
        ]

        facts = current_facts_version(self._workspace)
        freshness = index_freshness(
            [
                repository_freshness_for(self._workspace, repo, depth=request.depth, facts=facts)
                for repo in resolution.repositories
            ],
            depth=request.depth,
        )
        return InspectResponse(
            investigation_id=session.id,
            context=ContextView(
                context=request.context,
                scope=resolution.scope,
                repositories=tuple(sorted(repo.slug for repo in resolution.repositories)),
                primary=resolution.primary.slug if resolution.primary else None,
                reason=resolution.reason,
            ),
            target=request.target,
            target_evidence_status=target.evidence_status,
            file=InspectedFile(
                repository_slug=target.repository.slug,
                path=target.path,
                size=source.state.size,
                indexed_content_hash=source.state.content_hash,
                indexed_facts_version=source.state.parser_version,
                line_count=source.line_count,
            ),
            segments=tuple(segments),
            coverage=Coverage(
                requested=source.requested,
                returned=shaped.returned,
                unavailable=shaped.unavailable,
                budget_omitted=shaped.budget_omitted,
                source=source.source,
                worktree_matches_index=source.worktree_matches_index,
            ),
            freshness=freshness,
            budget=applied,
            ledger=session.ledger.summary(),
        )

    # -- relationships internals --------------------------------------------

    def _resolve_graph_entity(self, key: str, resolution: ContextResolution) -> EntityIdentity:
        """One exact natural key, or a refusal.

        The same exact read inspection uses, and for the same reason: a name
        lookup would resolve to whatever happens to share a name, which for a
        positional key whose definition has moved means confidently answering
        about a different definition than the caller holds a handle to.
        """
        identities = self._workspace.knowledge.get_entities_by_key([key])
        if not identities:
            raise EntityNotFoundError()
        in_scope = frozenset(repo.slug for repo in resolution.repositories)
        allowed = [identity for identity in identities if _resolvable_in(identity, in_scope)]
        if not allowed:
            # Absent *from this context* rather than answered from outside it.
            # Crossing the boundary silently would make the scope advisory, which
            # is the one property that makes a scoped answer worth having.
            raise EntityNotFoundError()
        if len(allowed) > 1:
            raise EntityAmbiguousError(
                candidates=tuple(
                    sorted(
                        f"{identity.repository_slug or ''}/{identity.path or ''}"
                        for identity in allowed
                    )
                )
            )
        return allowed[0]

    def _walked_edges(self, walked: WalkResult) -> list[_WalkedEdge]:
        """Turn walked steps into public edges, keeping the step beside each one.

        Endpoint identities are resolved by row id in one query and rendered as
        natural keys. A step whose endpoint does not resolve is dropped rather
        than emitted with a placeholder: an edge whose ends cannot be named is not
        a relationship a caller can act on, and inventing a name for one would be
        the fabrication this capability exists to avoid.
        """
        wanted = {
            entity_id for step in walked.steps for entity_id in (step.edge.src_id, step.edge.dst_id)
        }
        identities = self._workspace.knowledge.keys_for_entities(sorted(wanted)) if wanted else {}

        walked_edges: list[_WalkedEdge] = []
        for step in walked.steps:
            source = identities.get(step.edge.src_id)
            destination = identities.get(step.edge.dst_id)
            if source is None or destination is None:
                continue
            walked_edges.append(
                _WalkedEdge(
                    step=step,
                    edge=GraphEdge(
                        # Stored orientation, untouched. Which end the walk came
                        # from is a separate field; swapping these to express it
                        # would rewrite what the repository says.
                        source=entity_ref(source),
                        target=entity_ref(destination),
                        type=step.edge.type,
                        hop=step.hop,
                        traversal_direction=(
                            RelationshipDirection.OUTGOING
                            if step.outgoing
                            else RelationshipDirection.INCOMING
                        ),
                        cross_repository=step.edge.crosses_repositories,
                        confidence=step.edge.confidence,
                        provenance=edge_provenance(step.edge),
                    ),
                )
            )
        return walked_edges

    def _supply_provenance(
        self, walked_edges: Sequence[_WalkedEdge]
    ) -> tuple[list[_WalkedEdge], list[ProvenanceSupply]]:
        """Name each edge's asserting file, and supply its lines where provable.

        One read per distinct asserting file, over the whole file, because that
        single query answers both questions at once: which path the row id names,
        and which of its lines the stored chunks demonstrably contain. It runs
        only for the edges that survived bounding, so the work follows the answer
        rather than the neighbourhood.

        Reconstructed from **indexed** chunks and never from the working tree. The
        edge was produced by the indexed snapshot, so the lines that prove it are
        the lines that snapshot holds; reading the file as it is now could supply
        text that never asserted anything.
        """
        by_file: dict[int, tuple[str, str, dict[int, str]]] = {}
        for walked_edge in walked_edges:
            file_id = walked_edge.step.edge.file_id
            if file_id is None or file_id in by_file:
                continue
            chunks = self._workspace.knowledge.chunks_for_file(file_id, 1, WHOLE_FILE_END)
            if not chunks:
                continue
            by_file[file_id] = (
                chunks[0].repository_slug,
                chunks[0].path,
                indexed_line_map(chunks),
            )

        located: list[_WalkedEdge] = []
        for walked_edge in walked_edges:
            file_id = walked_edge.step.edge.file_id
            found = by_file.get(file_id) if file_id is not None else None
            provenance = walked_edge.edge.provenance
            if found is None or provenance is None:
                located.append(walked_edge)
                continue
            slug, path, _ = found
            located.append(
                _WalkedEdge(
                    step=walked_edge.step,
                    edge=walked_edge.edge.model_copy(
                        update={
                            "provenance": provenance.model_copy(
                                update={"repository_slug": slug, "path": path}
                            )
                        }
                    ),
                )
            )

        supplies: list[ProvenanceSupply] = []
        for slug, path, line_start, line_end in distinct_provenance(
            [walked_edge.edge for walked_edge in located]
        ):
            found = next(
                (value for value in by_file.values() if value[0] == slug and value[1] == path),
                None,
            )
            if found is None:
                continue
            available = found[2]
            wanted = range(line_start, line_end + 1)
            if any(number not in available for number in wanted):
                # The chunks do not cover the whole asserting span. Returning the
                # part they do cover would cite a narrower claim than the edge
                # makes, and stitching across the gap would cite text nobody was
                # shown. The edge stands; its provenance is simply not quotable.
                continue
            supply = self._provenance_supply(slug, path, line_start, line_end, available)
            if supply is not None:
                supplies.append(supply)
        return (located, supplies)

    def _provenance_supply(
        self,
        slug: str,
        path: str,
        line_start: int,
        line_end: int,
        available: dict[int, str],
    ) -> ProvenanceSupply | None:
        """One asserting span, prepared for the ledger with the text it hashed."""
        repo = next((r for r in self._workspace.repositories.list_all() if r.slug == slug), None)
        if repo is None or repo.id is None:
            return None
        state = self._workspace.files.states_for(repo.id, [path]).get(path)
        if state is None:
            return None
        text = LINE_SEPARATOR.join(available[number] for number in range(line_start, line_end + 1))
        return ProvenanceSupply(
            proposed=ProposedEvidence(
                ref=EvidenceRef(
                    repository_slug=slug,
                    path=path,
                    line_start=line_start,
                    line_end=line_end,
                    span_digest=chunk_hash(text),
                    file_content_hash=state.content_hash,
                    indexed_facts_version=state.parser_version,
                ),
                repository_name=repo.name,
                # An edge is a fact about the code and these are the lines that
                # assert it. Nothing here was guessed at.
                tier=EvidenceTier.DETERMINISTIC,
                # Literally what this match class names: reached by a graph edge
                # from something the query named.
                match=MatchClass.RELATED,
                origin=EvidenceSource.RELATIONSHIP,
                reason=f"source provenance for {slug}/{path}:{line_start}-{line_end}",
                text_length=len(text),
            ),
            text=text,
        )

    def _graph_paths(self, target: EntityRef, kept: Sequence[_WalkedEdge]) -> tuple[GraphPath, ...]:
        """The chains by which second-hop relationships were reached.

        A path is the route this walk took, never a shortest path — RepoAtlas
        computes none and claims none. It exists because a hop-2 edge is otherwise
        unexplained: the caller asked about one entity and is handed a
        relationship between two others, and the chain is what connects them. A
        first-hop edge is its own explanation and gets no path.
        """
        first_by_node: dict[int, GraphEdge] = {}
        for walked_edge in kept:
            if walked_edge.step.hop == 1:
                first_by_node.setdefault(walked_edge.step.to_id, walked_edge.edge)

        paths: list[GraphPath] = []
        for walked_edge in kept:
            if walked_edge.step.hop != 2:
                continue
            leading = first_by_node.get(walked_edge.step.from_id)
            if leading is None:
                # The edge that reached this node did not survive bounding, so the
                # chain cannot be shown whole. Half a path is not a route.
                continue
            chain = (leading, walked_edge.edge)
            paths.append(GraphPath(nodes=path_nodes(chain, target), edges=chain))
        paths.sort(key=lambda path: tuple(edge_order(edge) for edge in path.edges))
        return tuple(paths)

    def _relationships_response(
        self,
        *,
        request: RelationshipsRequest,
        resolution: ContextResolution,
        session: InvestigationSession,
        target: EntityRef,
        located: Sequence[_WalkedEdge],
        supplies: Sequence[ProvenanceSupply],
        entries: tuple[LedgerEntry, ...],
        truncated: bool,
        limits: AppliedGraphLimits,
    ) -> RelationshipsResponse:
        """Bind the recorded evidence to the edges that rest on it, then answer."""
        ids: dict[ProvenanceKey, str] = {}
        items: list[EvidenceItem] = []
        for rank, (supply, entry) in enumerate(zip(supplies, entries, strict=True), start=1):
            ref = supply.proposed.ref
            assert ref.line_start is not None and ref.line_end is not None
            ids[(ref.repository_slug, ref.path, ref.line_start, ref.line_end)] = entry.id
            items.append(
                EvidenceItem(
                    id=entry.id,
                    # Supply order, and nothing more. Relationships searched for
                    # nothing, so there is no relevance for a rank to express.
                    rank=rank,
                    ref=ref,
                    repository_name=supply.proposed.repository_name,
                    location=ref.location,
                    text=supply.text,
                    why=supply.proposed.reason,
                    tier=supply.proposed.tier,
                    match=supply.proposed.match,
                    status=entry.status,
                )
            )

        bound_edges: list[GraphEdge] = []
        for walked_edge in located:
            edge = walked_edge.edge
            key = provenance_key(edge.provenance)
            evidence_id = ids.get(key) if key is not None else None
            if evidence_id is None or edge.provenance is None:
                bound_edges.append(edge)
                continue
            bound_edges.append(
                edge.model_copy(
                    update={
                        "evidence_ids": (evidence_id,),
                        "provenance": edge.provenance.model_copy(update={"citable": True}),
                    }
                )
            )

        by_order = {edge_order(edge): edge for edge in bound_edges}
        paths = (
            tuple(
                path.model_copy(
                    update={"edges": tuple(by_order.get(edge_order(e), e) for e in path.edges)}
                )
                for path in self._graph_paths(
                    target,
                    [
                        _WalkedEdge(step=w.step, edge=by_order.get(edge_order(w.edge), w.edge))
                        for w in located
                    ],
                )
            )
            if limits.hops > 1
            else ()
        )

        facts = current_facts_version(self._workspace)
        supplied = _edge_repositories(bound_edges, items)
        freshness = index_freshness(
            [
                repository_freshness_for(self._workspace, repo, depth=request.depth, facts=facts)
                for repo in self._covering_repositories(resolution, supplied)
            ],
            depth=request.depth,
        )
        return RelationshipsResponse(
            investigation_id=session.id,
            context=ContextView(
                context=request.context,
                scope=resolution.scope,
                repositories=tuple(sorted(repo.slug for repo in resolution.repositories)),
                primary=resolution.primary.slug if resolution.primary else None,
                reason=resolution.reason,
            ),
            target=target,
            edges=tuple(bound_edges),
            paths=paths,
            evidence=tuple(items),
            freshness=freshness,
            truncated=truncated,
            has_more=truncated,
            limits=limits,
            ledger=session.ledger.summary(),
        )

    # -- sync: the one mutation ---------------------------------------------

    def sync(self, request: SyncRequest) -> SyncResponse:
        """Bring the index in line with the local working trees. The one mutation.

        V1's sync, called once and projected. Repository-set reconciliation, file
        admission, parsing, linking, the writer lock and the run's transaction
        boundary all belong to :func:`repoatlas.services.sync_service.sync`, and
        none of them is restated here: a capability that re-derived which files
        belong to which repository would be a second interpretation of the one
        question the index most needs to answer consistently.

        Sync observes. It does not pull, fetch, check out, reset or clean, and it
        runs nothing a repository contains. A dirty working tree is indexed as it
        stands.

        Selection is the explicit context. There is no working-directory inference
        and no implicit ``init``: with no configured root this refuses before
        anything can write.

        After the index changes, every live investigation's evidence is re-checked
        against it, so no ledger goes on claiming validity against the snapshot
        that was replaced. That housekeeping never counts as activity inside any
        investigation and never creates one. If it fails, the sync still happened,
        and the response says both.

        A run that fails part-way is not an untouched index. V1 commits each
        file's facts as they are produced, so the re-check runs against the index
        as the failure left it, and only then is the failure raised.
        """
        with self._operation():
            root = self._workspace.root
            # No index, no sync — checked before anything can write. Handed an
            # empty repository set, V1 would still commit its global link pass and
            # report success for a workspace that has no root.
            if root is None:
                raise NotInitializedError()
            registered = self._workspace.repositories.list_all()
            resolution = resolve_context(request.context, registered)
            repository_context = to_repository_context(resolution)
            # V1's own rule for whether this run reconciles the repository set,
            # asked of V1 rather than restated: a second copy of it is how the
            # capability and the CLI would come to disagree about scope.
            covers_root = sync_service._covers_root(repository_context, root)

            try:
                report = sync_service.sync(self._workspace, repository_context, full=request.full)
            except SyncInProgressError:
                # The writer lock was never acquired, so nothing changed and there
                # is nothing to re-check. The other writer finishing is the remedy.
                raise
            except Exception as exc:
                # The lock was held and the run had begun. The index may already
                # differ from the one every live ledger was checked against, so the
                # hint moves and the ledgers are re-checked against the index as the
                # failure left it — then the failure is reported, never rolled back.
                self._generation += 1
                revalidation = self._revalidate_live_investigations()
                raise SyncFailedError(
                    cause=capability_error(exc).code,
                    revalidation=revalidation.model_dump(mode="json"),
                    exit_code=getattr(exc, "exit_code", None),
                ) from exc

            # The mutation committed. The hint moves once, and only after it.
            self._generation += 1
            completed_at = datetime.now(UTC)
            revalidation = self._revalidate_live_investigations()
            return self._sync_response(
                request=request,
                resolution=resolution,
                report=report,
                covers_root=covers_root,
                revalidation=revalidation,
                completed_at=completed_at,
            )

    # -- sync internals -----------------------------------------------------

    def _revalidate_live_investigations(self) -> EvidenceRevalidationSummary:
        """Re-check every live investigation's evidence against the index now.

        Housekeeping, not use. Sessions are reached through
        :meth:`SessionRegistry.live_sessions`, which writes no clock and changes no
        order, so a sync cannot keep an abandoned investigation alive or reorder
        which one is evicted next. Expired investigations are discarded first
        rather than revived by being looked at.

        One read snapshot for every ledger, so all of them describe the same index.
        A failure here is reported rather than raised: the index change it follows
        has already happened, and verification revalidates before it grounds
        anything, so a missed eager refresh can never become an accepted citation.
        """
        sessions = self._sessions.live_sessions()
        try:
            with self._workspace.database.read_snapshot():
                registered = self._workspace.repositories.list_all()
                snapshots = [
                    (session, self._ledger_snapshot(session, registered)) for session in sessions
                ]
        except Exception as exc:
            return revalidation_failed(
                "live investigations could not be re-checked after the index changed "
                f"({capability_error(exc).code}); their evidence is revalidated before "
                "any verification"
            )

        results = []
        for session, snapshot in snapshots:
            transitions = session.ledger.revalidate(snapshot)
            # An optimisation hint and never evidence validity. Verification
            # compares real file state whatever this says.
            session.validated_generation = self._generation
            results.append((transitions, session.ledger.summary()))
        return revalidation_summary(results)

    def _sync_response(
        self,
        *,
        request: SyncRequest,
        resolution: ContextResolution,
        report: SyncReport,
        covers_root: bool,
        revalidation: EvidenceRevalidationSummary,
        completed_at: datetime,
    ) -> SyncResponse:
        """Project V1's report. Every count is V1's; only identity and paths change."""
        registered = self._workspace.repositories.list_all()
        # The engine's own input, in its own order: the registry when the run
        # covered the managed root, the resolved repository when it did not.
        # Matching against the whole registry in the second case would attribute a
        # report to the first repository sharing its display name.
        candidates = registered if covers_root else list(resolution.repositories)
        slugs = attribute_reports(report.repositories, candidates)
        views = tuple(
            repository_view(entry, slug)
            for entry, slug in zip(report.repositories, slugs, strict=True)
        )

        in_scope = {repo.id for repo in resolution.repositories}
        after = registered if covers_root else [r for r in registered if r.id in in_scope]
        facts = current_facts_version(self._workspace)
        freshness = index_freshness(
            [
                repository_freshness_for(self._workspace, repo, depth=request.depth, facts=facts)
                for repo in after
            ],
            depth=request.depth,
        )

        return SyncResponse(
            context=ContextView(
                context=request.context,
                scope=resolution.scope,
                repositories=tuple(sorted(repo.slug for repo in resolution.repositories)),
                primary=resolution.primary.slug if resolution.primary else None,
                reason=resolution.reason,
            ),
            status=sync_status(report),
            full=request.full,
            repositories=views,
            repositories_added=report.repositories_added,
            repositories_unavailable=tuple(
                view.repository_slug
                for view in views
                if view.enumeration_failed and view.repository_slug is not None
            ),
            unreadable_paths=relative_unreadable(report.unreadable_paths, self._workspace.root),
            files_indexed=report.files_indexed,
            symbols=report.symbols,
            relationships=report.relationships,
            dependencies=report.dependencies,
            linked_references=report.linked_references,
            unresolved_references=report.unresolved_references,
            cross_repository_edges=report.cross_repository_edges,
            embedded_chunks=report.embedded_chunks,
            pending_embeddings=report.pending_embeddings,
            evidence_revalidation=revalidation,
            freshness=freshness,
            completed_at=completed_at,
        )


def _duplicate_names(repositories: list[Repository]) -> frozenset[str]:
    """Display names held by more than one registered repository.

    Structural: it counts what is registered. Deriving ambiguity from a path
    shape or a naming convention would be a guess, and the guess would be wrong
    in exactly the case the field exists for.
    """
    seen: set[str] = set()
    duplicated: set[str] = set()
    for repo in repositories:
        if repo.name in seen:
            duplicated.add(repo.name)
        seen.add(repo.name)
    return frozenset(duplicated)


@dataclass(frozen=True, slots=True)
class _Drift:
    """The drift half of a status response, from one walk or from none."""

    checked: bool = False
    detected: bool = False
    added: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    relocated: tuple[str, ...] = ()
    conflicting: tuple[str, ...] = ()
    unreadable: tuple[Path, ...] = ()


def _drift_of(plan: RepositorySetPlan | None) -> _Drift:
    """Read one discovery plan, or record that none was made.

    ``plan is None`` covers both "not requested" and "no managed root to walk".
    Both leave ``checked`` False with every collection empty, which is the honest
    reading: nothing looked, so nothing is being claimed either way. The
    alternative — empty collections with no flag beside them — is indistinguishable
    from a clean result and would be read as one.
    """
    if plan is None:
        return _Drift()
    return _Drift(
        checked=True,
        detected=plan.drifted,
        added=tuple(candidate.slug for candidate in plan.added),
        missing=tuple(repo.slug or repo.name for repo in plan.missing),
        relocated=tuple(move.repository.slug for move in plan.relocated),
        conflicting=tuple(conflict.candidate.slug for conflict in plan.conflicts),
        unreadable=tuple(plan.unreadable),
    )


def _last_sync(repositories: tuple[Repository, ...]) -> datetime | None:
    stamps = [repo.last_indexed_at for repo in repositories if repo.last_indexed_at is not None]
    return max(stamps) if stamps else None
