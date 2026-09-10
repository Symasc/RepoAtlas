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
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType

from repoatlas.capabilities.budget import BudgetCeiling, ceiling_for
from repoatlas.capabilities.context import resolve_context
from repoatlas.capabilities.errors import ServiceClosedError
from repoatlas.capabilities.session import (
    DEFAULT_IDLE_TTL_SECONDS,
    DEFAULT_MAX_SESSIONS,
    SessionRegistry,
)
from repoatlas.config.settings import Settings
from repoatlas.discovery.reconcile import RepositorySetPlan
from repoatlas.freshness import index_freshness
from repoatlas.models.capability import CapabilityScope, ContextResolution
from repoatlas.models.capability_io import (
    IndexCounts,
    RepositoriesRequest,
    RepositoriesResponse,
    RepositoryView,
    StatusRequest,
    StatusResponse,
)
from repoatlas.models.freshness import FreshnessDepth
from repoatlas.models.investigation import DEFAULT_LEDGER_CAPACITY
from repoatlas.models.repository import Repository
from repoatlas.services.freshness_service import (
    current_facts_version,
    repository_freshness_for,
    repository_set_drift,
)
from repoatlas.services.workspace import Workspace, open_workspace


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
