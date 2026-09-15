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
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

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
    EntityAmbiguousError,
    EntityNotFoundError,
    EntityNotInspectableError,
    EvidenceNotFoundError,
    FileNotIndexedError,
    IndexChangedError,
    IndexInconsistencyError,
    InvestigationRequiredError,
    ServiceClosedError,
    SyncFailedError,
    TargetOutOfScopeError,
    capability_error,
)
from repoatlas.capabilities.evidence_shaping import LINE_SEPARATOR
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
from repoatlas.capabilities.sync import (
    attribute_reports,
    relative_unreadable,
    repository_view,
    revalidation_failed,
    revalidation_summary,
    sync_status,
)
from repoatlas.capabilities.verification import (
    CitationOutcome,
    projected_answer,
    resolve_citation,
    sanitized_answer,
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
from repoatlas.graph.keys import is_positional
from repoatlas.graph.traversal import CROSS_REPOSITORY_EDGES, WalkResult, WalkStep, walk
from repoatlas.grounding import GroundingReport, ground_answer
from repoatlas.indexing.chunking import chunk_hash
from repoatlas.models.capability import AppliedBudget, CapabilityScope, ContextResolution
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
from repoatlas.models.evidence import EvidenceSource, EvidenceTier, MatchClass
from repoatlas.models.freshness import FreshnessDepth, IndexFreshness
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
from repoatlas.models.verification import (
    AgentAnswer,
    CitationVerdict,
    EvidenceStatusView,
    GroundingFinding,
    GroundingFindingCode,
    VerifyGroundingRequest,
    VerifyGroundingResponse,
)
from repoatlas.retrieval.retriever import DIAGNOSTIC_LIMITS
from repoatlas.services import sync_service
from repoatlas.services.freshness_service import (
    current_facts_version,
    repository_freshness_for,
    repository_set_drift,
)
from repoatlas.services.workspace import Workspace, open_workspace
from repoatlas.storage.protocols import EntityIdentity
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
                    outcome = self._workspace.retriever(limits).retrieve_detailed(
                        request.question,
                        to_repository_context(resolution),
                        literal_phrase=diagnostic,
                    )
                    shaped = shape_candidates(outcome.evidence.candidates, applied)
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
                    # through shared nodes too, not only across direct edges. A
                    # shared node itself has no repository, so a walk rooted at one
                    # is not confined to anything.
                    home_repo_id = (
                        None
                        if request.cross_repository or identity.repository_slug is None
                        else next(
                            (
                                repo.id
                                for repo in registered
                                if repo.slug == identity.repository_slug
                            ),
                            None,
                        )
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
            report = ground_answer(
                projected_answer(request.answer, outcomes=outcomes),
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
            verdict=verdict_for(submitted=answer, outcomes=outcomes, report=report),
            original_answer=answer,
            sanitized_answer=sanitized_answer(answer, report, outcomes=outcomes),
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
    ) -> SearchResponse:
        """Assemble the response from **this run's** candidates and the new ids.

        Built from the shaped candidates, with only the identity and status taken
        from the ledger entries. Serializing the entries themselves would report
        the strongest metadata the investigation has ever accumulated for a span
        as though this query had established it — true of the ledger, false of the
        search.
        """
        handles = self._entity_handles(shaped.items)
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
                entity=handles.get(id(shaped_item)),
                status=entry.status,
            )
            for rank, (shaped_item, entry, item_ref) in enumerate(
                ((s, e, p.ref) for s, e, p in zip(shaped.items, entries, proposed, strict=True)),
                start=1,
            )
        ]

        freshness = index_freshness(
            [
                repository_freshness_for(self._workspace, repo, depth=request.depth, facts=facts)
                for repo in resolution.repositories
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
            entities=self._seed_handles(outcome.seed_entity_ids),
            trace=outcome.evidence.trace if request.include_trace else None,
            ledger=session.ledger.summary(),
        )

    def _seed_handles(self, seed_entity_ids: tuple[int, ...]) -> tuple[EntityHandle, ...]:
        """Natural handles for the exact-name hits that seeded graph expansion.

        These are the ``search`` → ``relationships`` handoff, and the reason they
        are published separately from evidence is that the best of them are never
        evidence: an interned identity has no file and no line, so the retriever
        admits it as a seed and produces no candidate for it. Returning it here
        gives the caller the hub it actually wants to traverse from, without
        RepoAtlas inventing a citation for something no file supplies.

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
            seen.add(identity.key)
            handles.append(
                EntityHandle(
                    key=identity.key,
                    type=identity.type,
                    positional=is_positional(identity.key),
                )
            )
        return tuple(handles)

    def _entity_handles(self, shaped: list[ShapedEvidence]) -> dict[int, EntityHandle]:
        """Resolve entity-backed evidence to natural keys, in one query.

        Exact resolution by row id through the Slice 0 identity read — never a
        lookup by name. A name search here would be a second, fuzzier answer to a
        question the retriever already answered exactly, and it could hand back a
        different entity that merely shares a name.

        An id that does not resolve yields no handle. The evidence still stands:
        failing to name the entity behind a span is not a reason to withhold the
        span, and inventing a plausible key would be worse than saying nothing.
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
            if identity is None or not identity.key:
                continue
            handles[id(item)] = EntityHandle(
                key=identity.key,
                type=identity.type,
                positional=is_positional(identity.key),
            )
        return handles

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
        in_scope = {repo.slug for repo in resolution.repositories}
        # An interned entity belongs to no repository and is in every scope; a
        # file-scoped one has to be in the scope that was asked for.
        allowed = [
            identity
            for identity in identities
            if identity.repository_slug is None or identity.repository_slug in in_scope
        ]
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
        freshness = index_freshness(
            [
                repository_freshness_for(self._workspace, repo, depth=request.depth, facts=facts)
                for repo in resolution.repositories
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
