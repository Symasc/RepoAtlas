"""Writer contention, lock order, serialization and snapshot coherence for sync.

Slice I is the first V2 capability that takes the writer path, so this file pins
how it shares that path.

**One inter-process lock, and it is V1's.** The capability adds no mutex of its
own for index mutation. Contention surfaces as V1's own condition —
``SYNC_IN_PROGRESS``, retryable — with no traceback, no SQLite string and no
longer wait than V1 would have spent. And because the lock was never acquired,
nothing changed: no generation moves and no ledger is re-checked.

**One lock order.** The service's in-process operation lock is taken first, then
V1's writer lock inside it. Every capability path takes them in that order, so the
two can never be held in opposite orders by two callers.

**One read of the index for the housekeeping.** Once the sync has released its
lock, another process may commit at any moment. Every live ledger is re-checked
against a single read snapshot, so their statuses describe one index — the one
this call read — rather than a mixture of two.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest
from repoatlas.capabilities.errors import capability_error
from repoatlas.capabilities.service import CapabilityService
from repoatlas.errors import SyncInProgressError
from repoatlas.models.capability import CapabilityContext, CapabilityScope, ErrorCategory
from repoatlas.models.investigation import EvidenceStatus
from repoatlas.models.reports import SyncReport
from repoatlas.models.search import SearchRequest
from repoatlas.models.sync import SyncRequest, SyncResponse, SyncStatus
from repoatlas.services import sync_service
from repoatlas.services.workspace import Workspace
from repoatlas.sync.lock import writer_lock

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)
SERVICE_HOST = "SERVICE_HOST"


def _sync(service: CapabilityService) -> SyncResponse:
    return service.sync(SyncRequest(context=GLOBAL))


def _no_wait(workspace: Workspace) -> None:
    """Refuse contention immediately instead of waiting V1's default thirty seconds."""
    workspace.settings = workspace.settings.model_copy(
        update={
            "storage": workspace.settings.storage.model_copy(update={"lock_timeout_seconds": 0.0})
        }
    )


def _search(service: CapabilityService, question: str = SERVICE_HOST) -> str:
    response = service.search(SearchRequest(question=question, context=GLOBAL))
    assert response.evidence
    return response.investigation_id


def _statuses(service: CapabilityService) -> dict[tuple[str, str], EvidenceStatus]:
    return {
        (investigation, entry.id): entry.status
        for investigation, session in service.sessions._sessions.items()
        for entry in session.ledger.entries()
    }


def _held_elsewhere(lock: threading.RLock) -> bool:
    """Whether ``lock`` is held by some thread other than the one asking."""
    result: list[bool] = []

    def attempt() -> None:
        acquired = lock.acquire(blocking=False)
        if acquired:
            lock.release()
        result.append(not acquired)

    probe = threading.Thread(target=attempt)
    probe.start()
    probe.join()
    return result[0]


# ---------------------------------------------------------------------------
# CASE 7 — another writer holds the lock
# ---------------------------------------------------------------------------


def test_a_held_writer_lock_is_retryable_contention(initialized: Workspace) -> None:
    """CASE 7. V1's condition, typed, retryable, and free of driver noise."""
    _no_wait(initialized)
    service = CapabilityService(initialized)

    with (
        writer_lock(Path(initialized.database.path), timeout=0.0),
        pytest.raises(SyncInProgressError) as raised,
    ):
        _sync(service)

    error = capability_error(raised.value)
    assert error.code == "SYNC_IN_PROGRESS"
    assert error.category is ErrorCategory.CONTENTION
    assert error.retryable is True
    rendered = f"{error.message}\n{error.detail or ''}".lower()
    assert "traceback" not in rendered
    assert "sqlite" not in rendered


def test_contention_changes_nothing_and_rechecks_nothing(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock was never acquired, so there is no index change to re-check against."""
    _no_wait(initialized)
    service = CapabilityService(initialized)
    _search(service)
    generation = service.generation
    before = _statuses(service)

    def refuse(*args: object, **kwargs: object) -> object:
        raise AssertionError("ledgers were re-checked although nothing changed")

    monkeypatch.setattr(CapabilityService, "_ledger_snapshot", refuse)
    with (
        writer_lock(Path(initialized.database.path), timeout=0.0),
        pytest.raises(SyncInProgressError),
    ):
        _sync(service)

    assert service.generation == generation
    assert _statuses(service) == before
    assert len(service.sessions) == 1


def test_the_index_stays_usable_and_the_retry_succeeds(initialized: Workspace) -> None:
    _no_wait(initialized)
    service = CapabilityService(initialized)
    with (
        writer_lock(Path(initialized.database.path), timeout=0.0),
        pytest.raises(SyncInProgressError),
    ):
        _sync(service)

    assert service.search(SearchRequest(question=SERVICE_HOST, context=GLOBAL)).evidence
    assert _sync(service).status is SyncStatus.COMPLETED


@pytest.mark.timing
def test_the_wait_is_v1s_own_timeout_and_no_longer(initialized: Workspace) -> None:
    """The capability passes V1's configured timeout through and adds none."""
    import time

    initialized.settings = initialized.settings.model_copy(
        update={
            "storage": initialized.settings.storage.model_copy(update={"lock_timeout_seconds": 0.4})
        }
    )
    service = CapabilityService(initialized)

    with writer_lock(Path(initialized.database.path), timeout=0.0):
        started = time.monotonic()
        with pytest.raises(SyncInProgressError):
            _sync(service)
        waited = time.monotonic() - started

    assert 0.3 <= waited < 3.0, waited


# ---------------------------------------------------------------------------
# Lock order and in-process serialization
# ---------------------------------------------------------------------------


def test_the_service_lock_is_held_before_the_writer_lock_is_requested(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operation lock first, writer lock second — the one order every path uses."""
    service = CapabilityService(initialized)
    real = Workspace.writer_lock
    observed: list[bool] = []

    def probing(self: Workspace):
        observed.append(_held_elsewhere(service._call_lock))
        return real(self)

    monkeypatch.setattr(Workspace, "writer_lock", probing)

    _sync(service)

    assert observed == [True]


def test_no_other_operation_enters_the_service_while_sync_runs(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In-process mutual exclusion, proven without touching SQLite from a second thread.

    The workspace's connection belongs to the thread that opened it, so a second
    thread cannot be sent through a real capability body. The lock is what every
    body waits on, so probing it while sync is inside V1 is the precise question.
    """
    service = CapabilityService(initialized)
    real = sync_service.sync
    during: list[bool] = []

    def spy(*args: object, **kwargs: object) -> SyncReport:
        during.append(_held_elsewhere(service._call_lock))
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(sync_service, "sync", spy)

    _sync(service)

    assert during == [True]
    assert _held_elsewhere(service._call_lock) is False


# ---------------------------------------------------------------------------
# CASE 21 — an external writer during the housekeeping
# ---------------------------------------------------------------------------


def _rewrite_file_rows_externally(database_path: Path, marker: str) -> None:
    """Commit a new generation of every file row from a genuinely separate connection."""
    other = sqlite3.connect(database_path)
    try:
        other.execute("PRAGMA busy_timeout = 5000")
        other.execute(
            "UPDATE files SET content_hash = ?, parser_version = ?",
            (f"{marker}-hash", f"{marker}-version"),
        )
        other.commit()
    finally:
        other.close()


def test_every_ledger_is_rechecked_against_one_generation(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CASE 21. Another process commits between the first ledger's read and the next.

    Without one read snapshot, the first investigation would be judged against the
    index before that commit and the second against the index after it, and the
    summary would add up statuses from two indexes that never coexisted.
    """
    service = CapabilityService(initialized)
    _search(service)
    _search(service, "deploy")
    database_path = Path(initialized.database.path)
    real = CapabilityService._ledger_snapshot
    calls = {"n": 0}

    def read_then_reindex(self: CapabilityService, session: object, registered: object) -> object:
        snapshot = real(self, session, registered)  # type: ignore[arg-type]
        calls["n"] += 1
        if calls["n"] == 1:
            _rewrite_file_rows_externally(database_path, "generation-b")
        return snapshot

    monkeypatch.setattr(CapabilityService, "_ledger_snapshot", read_then_reindex)

    response = _sync(service)

    assert calls["n"] == 2
    statuses = set(_statuses(service).values())
    assert statuses in ({EvidenceStatus.VALID}, {EvidenceStatus.STALE}), statuses
    assert response.evidence_revalidation.investigations_checked == 2
