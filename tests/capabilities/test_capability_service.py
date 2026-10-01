"""The capability service's lifecycle, serialization and boundaries (V2 Slice C).

Three properties are load-bearing here, and each of them fails silently if it is
wrong:

* the workspace is opened **once**, so a long-lived server does not accumulate
  database handles or pay the open cost per question;
* every public operation is **serialized**, because V1's single-connection
  assumption reaches all the way down to the database handle;
* a closed service refuses **deterministically**, rather than answering from a
  handle that no longer points anywhere.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from repoatlas.capabilities.errors import ServiceClosedError, capability_error
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.models.capability import CapabilityContext, CapabilityScope, ErrorCategory
from repoatlas.models.capability_io import RepositoriesRequest, StatusRequest
from repoatlas.services.workspace import Workspace

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)


def _service(workspace: Workspace) -> CapabilityService:
    return CapabilityService(workspace)


# ---------------------------------------------------------------------------
# Workspace ownership
# ---------------------------------------------------------------------------


def test_open_builds_exactly_one_workspace(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server answers many questions. It opens one database to do it."""
    from repoatlas.capabilities import service as service_module

    opened: list[Settings | None] = []
    real = service_module.open_workspace

    def counting(*, settings: Settings | None = None, require_root: bool = True) -> Workspace:
        opened.append(settings)
        return real(settings=settings, require_root=require_root)

    monkeypatch.setattr(service_module, "open_workspace", counting)

    service = CapabilityService.open(settings=settings, require_root=False)
    try:
        assert len(opened) == 1
    finally:
        service.close()


def test_every_call_reuses_the_same_workspace(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mutation this kills: opening a workspace per capability call.

    That version passes every behavioural test — it returns the right answer —
    and leaks a database handle per question.
    """
    from repoatlas.capabilities import service as service_module

    def forbidden(**_: object) -> Workspace:
        raise AssertionError("a capability call opened a second workspace")

    service = _service(initialized)
    monkeypatch.setattr(service_module, "open_workspace", forbidden)

    service.repositories(RepositoriesRequest(context=GLOBAL))
    service.status(StatusRequest(context=GLOBAL, include_drift=False))
    service.repositories(RepositoriesRequest(context=GLOBAL))


def test_the_service_holds_one_registry_and_one_ceiling(initialized: Workspace) -> None:
    service = _service(initialized)
    assert service.sessions is service.sessions
    assert service.budget_ceiling is service.budget_ceiling
    assert service.budget_ceiling.max_items > 0


# ---------------------------------------------------------------------------
# Close
# ---------------------------------------------------------------------------


def test_close_is_idempotent(workspace: Workspace) -> None:
    """A host with both a context manager and a shutdown hook is normal."""
    service = _service(workspace)
    service.close()
    service.close()
    service.close()
    assert service.closed is True


def test_the_context_manager_closes_exactly_once(settings: Settings) -> None:
    with CapabilityService.open(settings=settings, require_root=False) as service:
        assert service.closed is False
    assert service.closed is True


def test_repositories_after_close_is_a_deterministic_refusal(workspace: Workspace) -> None:
    """Not an AttributeError, and not a driver complaint about a closed handle."""
    service = _service(workspace)
    service.close()

    with pytest.raises(ServiceClosedError):
        service.repositories(RepositoriesRequest(context=GLOBAL))


def test_status_after_close_is_the_same_refusal(workspace: Workspace) -> None:
    service = _service(workspace)
    service.close()

    with pytest.raises(ServiceClosedError):
        service.status(StatusRequest(context=GLOBAL))


def test_the_closed_refusal_maps_to_a_transport_neutral_envelope() -> None:
    """It has to survive the trip to a caller with no Python types."""
    error = capability_error(ServiceClosedError())
    assert error.code == "SERVICE_CLOSED"
    assert error.category is ErrorCategory.INTERNAL
    assert error.retryable is False


def test_a_closed_service_is_never_reopened(workspace: Workspace) -> None:
    """Reopening would leave handles taken earlier pointing somewhere else."""
    service = _service(workspace)
    service.close()
    with service:
        pass
    assert service.closed is True
    with pytest.raises(ServiceClosedError):
        service.status(StatusRequest(context=GLOBAL))


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def test_two_callers_never_execute_a_capability_body_at_once(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At most one operation runs against this workspace at a time.

    The proof is a high-water mark rather than a sleep: ``max_active`` can only
    exceed one if two bodies were inside simultaneously, whatever the timing. The
    first caller is held inside the critical section on an event until the second
    has demonstrably been started, so the overlapping schedule is the one being
    attempted, not the one being avoided.

    The raced body deliberately touches no database. V1 opens its SQLite
    connection bound to the creating thread, so a body that queried would fail in
    the second thread for a reason that has nothing to do with the lock — and
    would leave the lock's behaviour unproven either way. What is under test here
    is mutual exclusion, and mutual exclusion is what is measured.
    """
    inside = threading.Event()
    release = threading.Event()
    guard = threading.Lock()
    active = 0
    max_active = 0

    def blocking() -> list[object]:
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
        inside.set()
        release.wait(10)
        with guard:
            active -= 1
        return []

    monkeypatch.setattr(initialized.repositories, "list_all", blocking)
    service = _service(initialized)
    failures: list[BaseException] = []

    def call() -> None:
        try:
            service.repositories(RepositoriesRequest(context=GLOBAL))
        except BaseException as exc:  # pragma: no cover - reported, not swallowed
            failures.append(exc)

    first = threading.Thread(target=call)
    second = threading.Thread(target=call)

    first.start()
    assert inside.wait(10), "the first call never entered the capability body"

    second.start()
    # The second thread is running and blocked on the service lock. If the lock
    # were absent it would enter the body here, and the high-water mark would
    # record it.
    second.join(0.2)
    with guard:
        assert max_active == 1, "a second call entered while the first was inside"

    release.set()
    first.join(10)
    second.join(10)

    assert not first.is_alive() and not second.is_alive()
    assert max_active == 1
    assert not failures, failures


def test_a_failed_call_does_not_wedge_the_service(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock is released on the way out however the call leaves."""

    def exploding() -> list[object]:
        raise RuntimeError("boom")

    service = _service(initialized)
    monkeypatch.setattr(initialized.repositories, "list_all", exploding)

    with pytest.raises(RuntimeError):
        service.repositories(RepositoriesRequest(context=GLOBAL))

    assert service._call_lock.acquire(blocking=False) is True
    service._call_lock.release()

    monkeypatch.undo()
    response = service.repositories(RepositoriesRequest(context=GLOBAL))
    assert response.repositories


def test_a_failed_call_releases_the_lock_for_a_different_thread(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stronger form: the lock is genuinely free, not merely re-entrant.

    ``RLock`` is re-entrant, so the owning thread can always re-acquire it — which
    means the same-thread check above cannot distinguish "released" from "still
    held by me". Another thread can.
    """

    def exploding() -> list[object]:
        raise RuntimeError("boom")

    service = _service(initialized)
    monkeypatch.setattr(initialized.repositories, "list_all", exploding)
    with pytest.raises(RuntimeError):
        service.repositories(RepositoriesRequest(context=GLOBAL))

    acquired: list[bool] = []

    def take_lock() -> None:
        got = service._call_lock.acquire(timeout=5)
        acquired.append(got)
        if got:
            service._call_lock.release()

    worker = threading.Thread(target=take_lock)
    worker.start()
    worker.join(10)
    assert acquired == [True]


def test_the_call_lock_is_reentrant(initialized: Workspace) -> None:
    """A capability composed of two others must not deadlock on its own lock."""
    service = _service(initialized)
    with service._call_lock:
        service.repositories(RepositoriesRequest(context=GLOBAL))


# ---------------------------------------------------------------------------
# Boundaries
# ---------------------------------------------------------------------------


def test_the_public_callable_surface_is_only_the_intended_operations() -> None:
    """Every capability that exists is one somebody decided to ship.

    Updated once per slice, deliberately: a capability appearing here without that
    edit means one was added without the review that decides what it promises.
    """
    public = {
        name
        for name in dir(CapabilityService)
        if not name.startswith("_") and callable(getattr(CapabilityService, name))
    }
    assert public == {
        "open",
        "close",
        "repositories",
        "status",
        "search",
        "inspect",
        "relationships",
        # change impact: shipped with the change-impact engine
        "impact",
        "dependencies",
        "http_operations",
        # topology: shipped for the local API, deliberately not an agent tool
        "topology_overview",
        "topology_children",
        "topology_edge",
        # topology for the explorer's search box and code reader
        "topology_files",
        "topology_names",
        "topology_references",
        "verify_grounding",
        "sync",
    }


def test_no_future_capability_exists_as_a_stub() -> None:
    """A stub is worse than an absence: a caller cannot tell it from an empty result.

    ``init`` is named because it must never become a capability at all. It creates
    configuration and chooses a root, and a caller on the far side of a transport
    has no business doing either.
    """
    for name in ("init", "initialize"):
        assert not hasattr(CapabilityService, name), f"{name} must not be a capability"


def test_the_service_captures_no_working_directory(initialized: Workspace) -> None:
    """Opening a service says which index to talk to, never which repository."""
    service = _service(initialized)
    values = [value for value in vars(service).values() if isinstance(value, Path)]
    assert not values, f"the service holds a path that could become an ambient default: {values}"


def test_the_generation_counter_is_a_stable_in_process_hint(initialized: Workspace) -> None:
    """Read capabilities never move it — and it never proves anything about
    freshness even when sync does."""
    service = _service(initialized)
    before = service.generation
    service.repositories(RepositoriesRequest(context=GLOBAL))
    service.status(StatusRequest(context=GLOBAL))
    assert service.generation == before
