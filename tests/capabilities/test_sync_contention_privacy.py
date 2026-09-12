"""What a writer-contention refusal is allowed to say (FINAL audit, F-01).

The fifth surface in the privacy family, and the one the first four could not
have found. RCA-03 (error envelope), RCA-08 (retrieval trace), RCA-09 (parser
diagnostics) and RCA-10 (git stderr) each closed a route by which *somebody
else's* text --- an exception's, a library's, a subprocess's --- reached a public
field. Every one of them was audited by firing a sentinel through the
implementation and scanning the response for it.

This leak was RepoAtlas's own prose, so no sentinel could reveal it. The lock
subsystem authored a helpful sentence and put it in ``detail``:

    Wait for it to finish and try again.

    Lock holder: pid=1927353 host=Symasc since=2026-09-12T17:37:53+00:00
    Lock file:   /home/…/.local/share/repoatlas/atlas.db.lock

``detail`` is what :func:`~repoatlas.capabilities.errors.capability_error` carries
onto the public envelope, so a pid, a hostname and an absolute path crossed to
whatever agent asked to sync. The contradiction that makes it a defect rather
than a choice is one call away: on the same connection ``repositories`` returns
``host_path: null`` unless the caller passes ``include_host_paths=True``, because
host coordinates "describe the operator's machine rather than the code". One
capability gated them behind an explicit opt-in while another handed them over
unasked.

The correction is the split the family already uses: the raw fact stays in the
subsystem that observed it, and the public projection takes only what the caller
can act on. Here that means the coordinates moved off ``detail`` and onto
:attr:`~repoatlas.errors.SyncInProgressError.owner` and
:attr:`~repoatlas.errors.SyncInProgressError.lock_path`, reached through
``render()`` --- which only the CLI calls. Nothing was deleted: an operator at a
terminal is *on* the machine the lock file sits on, and the pid is what lets them
find the other writer.

Two things are asserted, pulling in opposite directions as in every earlier
correction:

* **nothing host-local crosses** --- no pid, no hostname, no absolute path, in
  any field of the public envelope;
* **nothing useful was lost** --- the CLI still prints all of it, the condition is
  still ``SYNC_IN_PROGRESS``, still ``contention``, still retryable, still exit 5.

The route is established with a **real held writer lock** (CASE 1) rather than by
constructing the exception by hand, because the hand-built form is exactly what
the pre-existing test did and exactly why the leak survived it.
"""

from __future__ import annotations

import json
import os
import re
import socket
from pathlib import Path

import pytest
from repoatlas.capabilities.errors import SyncFailedError, capability_error
from repoatlas.capabilities.service import CapabilityService
from repoatlas.errors import ExitCode, SyncInProgressError
from repoatlas.models.capability import (
    CapabilityContext,
    CapabilityError,
    CapabilityScope,
    ErrorCategory,
)
from repoatlas.models.capability_io import RepositoriesRequest
from repoatlas.models.sync import SyncRequest
from repoatlas.services.workspace import Workspace
from repoatlas.sync.lock import _in_progress, lock_path, writer_lock

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)

#: A hostname and a lock directory no real machine would produce, so a match is
#: proof of disclosure rather than a coincidence of the temporary directory.
PRIVATE_HOST = "RCA_F01_PRIVATE_HOST"
PRIVATE_LOCK = Path("/tmp/RCA_F01_PRIVATE_LOCK/atlas.db.lock")  # noqa: S108 - a literal, never created
PRIVATE_PID = 424242


def _no_wait(workspace: Workspace) -> None:
    """Refuse contention immediately rather than waiting V1's default thirty seconds."""
    workspace.settings = workspace.settings.model_copy(
        update={
            "storage": workspace.settings.storage.model_copy(update={"lock_timeout_seconds": 0.0})
        }
    )


def _owned() -> SyncInProgressError:
    """The contention error as the lock subsystem builds it, with known coordinates.

    The owner line is spelled exactly as :func:`repoatlas.sync.lock._record_owner`
    writes it, so this is the real shape rather than an invented one.
    """
    return SyncInProgressError(
        "Another RepoAtlas synchronization is already in progress.",
        detail="Wait for it to finish and try again.",
        owner=f"pid={PRIVATE_PID} host={PRIVATE_HOST} since=2026-09-12T00:00:00+00:00",
        lock_path=PRIVATE_LOCK,
    )


def _strings(value: object, path: str = "$") -> list[tuple[str, str]]:
    """Every string in a payload, with the field it sits in.

    Walked rather than matched against ``json.dumps`` output alone: a JSON dump
    escapes, and an assertion that only reads the dump can be satisfied by an
    encoding accident instead of by an absence.
    """
    if isinstance(value, dict):
        return [pair for key, item in value.items() for pair in _strings(item, f"{path}.{key}")]
    if isinstance(value, list | tuple):
        return [
            pair for index, item in enumerate(value) for pair in _strings(item, f"{path}[{index}]")
        ]
    if isinstance(value, str):
        return [(path, value)]
    return []


def assert_no_host_coordinates(error: CapabilityError) -> None:
    """No field of a public envelope names the machine RepoAtlas runs on."""
    dumped = error.model_dump(mode="json")
    for field, text in _strings(dumped):
        assert PRIVATE_HOST not in text, f"{field}: hostname disclosed"
        assert str(PRIVATE_LOCK) not in text, f"{field}: lock path disclosed"
        assert "RCA_F01_PRIVATE_LOCK" not in text, f"{field}: lock directory disclosed"
        assert str(PRIVATE_PID) not in text, f"{field}: pid disclosed"
        assert not re.search(r"\bpid=\d+", text), f"{field}: a pid= record disclosed"
        assert not re.search(r"\bhost=\S", text), f"{field}: a host= record disclosed"
        assert "Lock holder" not in text, f"{field}: raw owner prose disclosed"
        assert "Lock file" not in text, f"{field}: raw lock prose disclosed"
        assert ".lock" not in text, f"{field}: a lock file named"
    # The dump as a whole, so a coordinate split across two fields still fails.
    assert "/tmp/RCA_F01" not in json.dumps(dumped)  # noqa: S108 - an assertion, not a path in use


# ---------------------------------------------------------------------------
# CASE 1. The route, established against a lock that is really held
# ---------------------------------------------------------------------------


def test_case_1_a_really_held_lock_still_reports_retryable_contention(
    initialized: Workspace,
) -> None:
    """CASE 1. The condition survives the correction: this is contention, not a bug."""
    _no_wait(initialized)
    service = CapabilityService(initialized)

    with (
        writer_lock(Path(initialized.database.path), timeout=0.0),
        pytest.raises(SyncInProgressError) as raised,
    ):
        service.sync(SyncRequest(context=GLOBAL))

    error = capability_error(raised.value)
    assert error.code == "SYNC_IN_PROGRESS"
    assert error.retryable is True


def test_case_1_a_really_held_lock_discloses_no_host_coordinates(
    initialized: Workspace,
) -> None:
    """CASE 1. The audit's reproduction, at the capability boundary.

    The lock is genuinely held, so the owner line is this process's own --- a real
    pid and this machine's hostname. Those are what must not appear.
    """
    _no_wait(initialized)
    service = CapabilityService(initialized)
    database = Path(initialized.database.path)

    with (
        writer_lock(database, timeout=0.0),
        pytest.raises(SyncInProgressError) as raised,
    ):
        service.sync(SyncRequest(context=GLOBAL))

    error = capability_error(raised.value)
    dumped = json.dumps(error.model_dump(mode="json"))
    assert str(os.getpid()) not in dumped
    assert socket.gethostname() not in dumped
    assert str(lock_path(database)) not in dumped
    assert str(database) not in dumped
    assert not re.search(r"\bpid=\d+", dumped)


def test_case_1_the_lock_subsystem_still_knows_who_holds_it(initialized: Workspace) -> None:
    """CASE 1's other half. The facts were re-homed, not deleted.

    An operator resolving a wedged writer needs exactly these, and the subsystem
    that observed them is where they belong.
    """
    database = Path(initialized.database.path)
    with writer_lock(database, timeout=0.0):
        raised = _in_progress(lock_path(database))

    owner = raised.owner or ""
    assert f"pid={os.getpid()}" in owner
    assert socket.gethostname() in owner
    assert raised.lock_path == lock_path(database)


# ---------------------------------------------------------------------------
# CASES 2-4. What the public envelope may not carry
# ---------------------------------------------------------------------------


def test_case_2_the_envelope_carries_no_absolute_lock_path() -> None:
    """CASE 2. The field that started the finding."""
    error = capability_error(_owned())
    assert str(PRIVATE_LOCK) not in json.dumps(error.model_dump(mode="json"))
    assert_no_host_coordinates(error)


def test_case_3_the_envelope_carries_no_hostname() -> None:
    """CASE 3. A hostname identifies the machine even without a path beside it."""
    error = capability_error(_owned())
    assert PRIVATE_HOST not in json.dumps(error.model_dump(mode="json"))


def test_case_4_the_envelope_carries_no_pid() -> None:
    """CASE 4. A pid is useless to a remote agent and identifies a local process."""
    error = capability_error(_owned())
    assert str(PRIVATE_PID) not in json.dumps(error.model_dump(mode="json"))


def test_case_4_removing_the_path_alone_would_not_have_been_enough() -> None:
    """CASES 2-4 together, as one assertion.

    A correction that deleted the path and left ``Lock holder: pid=… host=…``
    would pass a path-shaped test and still disclose the machine. The finding was
    three facts, and all three had to move.
    """
    assert_no_host_coordinates(capability_error(_owned()))


def test_case_4_the_detail_is_remediation_and_nothing_else() -> None:
    """CASES 2-4, positively. What the caller is told instead."""
    error = capability_error(_owned())
    assert error.detail == "Wait for it to finish and try again."
    assert error.message == "Another RepoAtlas synchronization is already in progress."


# ---------------------------------------------------------------------------
# CASES 5-6. What the correction was not allowed to change
# ---------------------------------------------------------------------------


def test_case_5_contention_remains_retryable() -> None:
    """CASE 5. The one condition in the table where waiting is the remedy.

    A privacy correction that cost the caller its retry signal would have traded
    one defect for a worse one: an agent told "internal error" stops, and an agent
    told "retryable" succeeds a moment later.
    """
    assert capability_error(_owned()).retryable is True


def test_case_6_code_category_and_exit_code_are_unchanged() -> None:
    """CASE 6. Contention is not flattened into a generic failure."""
    error = capability_error(_owned())
    assert error.code == "SYNC_IN_PROGRESS"
    assert error.category is ErrorCategory.CONTENTION
    assert error.exit_code == ExitCode.SYNC_LOCKED
    assert error.code not in {"INTERNAL_ERROR", "SYNC_FAILED"}


def test_case_6_the_condition_is_still_distinguishable_from_a_failed_sync() -> None:
    """CASE 6. Contention and failure are different answers with different remedies."""
    contention = capability_error(_owned())
    failed = capability_error(SyncFailedError(cause="INDEX_UNAVAILABLE"))
    assert contention.code != failed.code
    assert contention.retryable is True
    assert failed.retryable is False


# ---------------------------------------------------------------------------
# CASE 11. The operator keeps what the agent must not have
# ---------------------------------------------------------------------------


def test_case_11_the_cli_rendering_still_names_the_holder_and_the_file() -> None:
    """CASE 11. ``render`` is the CLI's projection, and it is unchanged.

    The coordinates are local by nature: whoever reads this is on the machine the
    lock file is on. Removing them to make the capability safe would have cost a
    real diagnostic for nothing, so they are still here --- just not on the wire.
    """
    rendered = _owned().render()
    assert "Another RepoAtlas synchronization is already in progress." in rendered
    assert "Wait for it to finish and try again." in rendered
    assert f"Lock holder: pid={PRIVATE_PID} host={PRIVATE_HOST}" in rendered
    assert f"Lock file:   {PRIVATE_LOCK}" in rendered


def test_case_11_an_unreadable_owner_line_names_neither(initialized: Workspace) -> None:
    """CASE 11. Both lines or neither, as before the correction.

    The incumbent may be mid-rewrite of its own line. Naming the file without
    saying who holds it tells an operator nothing they could not already see.
    """
    error = SyncInProgressError(
        "Another RepoAtlas synchronization is already in progress.",
        detail="Wait for it to finish and try again.",
        owner="",
        lock_path=PRIVATE_LOCK,
    )
    rendered = error.render()
    assert "Lock holder" not in rendered
    assert "Lock file" not in rendered
    assert rendered.endswith("Wait for it to finish and try again.")


def test_case_11_the_capability_layer_never_calls_render() -> None:
    """CASE 11's guard. The split only holds while the two projections stay apart.

    If the capability layer ever rendered instead of reading ``detail``, every
    coordinate would be back on the wire and every assertion above would still
    pass when run against the exception alone.
    """
    source = Path(capability_error.__code__.co_filename).read_text(encoding="utf-8")
    assert ".render()" not in source


# ---------------------------------------------------------------------------
# CASE 10. Asked-for disclosure is not accidental disclosure
# ---------------------------------------------------------------------------


def test_case_10_host_paths_stay_opt_in_and_still_work(initialized: Workspace) -> None:
    """CASE 10. The product rule the finding contradicted, pinned from both sides.

    This is the comparison that turned a helpful sentence into a defect: one
    capability required an explicit request for host coordinates while another
    supplied them unasked. Both halves are asserted, because a correction that
    broke the opt-in would "fix" the contradiction the wrong way round.
    """
    service = CapabilityService(initialized)

    withheld = service.repositories(RepositoriesRequest(context=GLOBAL))
    assert withheld.repositories
    assert all(entry.host_path is None for entry in withheld.repositories)

    disclosed = service.repositories(RepositoriesRequest(context=GLOBAL, include_host_paths=True))
    assert disclosed.repositories
    assert all(entry.host_path is not None for entry in disclosed.repositories)
    assert all(Path(str(entry.host_path)).is_absolute() for entry in disclosed.repositories)


def test_case_10_contention_is_not_an_opt_in_route_to_the_same_thing(
    initialized: Workspace,
) -> None:
    """CASE 10. There is no request that makes contention disclose coordinates.

    ``sync`` has no ``include_host_paths``, so the only honest answer is the one
    that names nothing.
    """
    assert "include_host_paths" not in SyncRequest.model_fields
    assert_no_host_coordinates(capability_error(_owned()))


# ---------------------------------------------------------------------------
# CASES 12-13. The neighbouring guarantees this change must not disturb
# ---------------------------------------------------------------------------


def test_case_12_sync_failed_still_reports_that_the_index_may_have_moved() -> None:
    """CASE 12. The fact a caller holding evidence has to act on."""
    error = capability_error(SyncFailedError(cause="INDEX_UNAVAILABLE", revalidation={"stale": 1}))
    assert error.code == "SYNC_FAILED"
    assert error.context["index_may_have_changed"] is True
    assert error.context["cause"] == "INDEX_UNAVAILABLE"
    assert error.retryable is False


def test_case_13_unexpected_failures_still_answer_with_fixed_text() -> None:
    """CASE 13. RCA-03's guarantee, re-checked beside the new one."""

    class Boom(Exception):
        pass

    error = capability_error(Boom(f"host={PRIVATE_HOST} at {PRIVATE_LOCK}"))
    assert error.code == "INTERNAL_ERROR"
    assert_no_host_coordinates(error)
