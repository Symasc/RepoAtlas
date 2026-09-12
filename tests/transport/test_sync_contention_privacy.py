"""A sanitized contention refusal crossing the wire (FINAL audit, F-01).

The capability suite proves the refusal is safe. This one proves the transport did
not undo that and, more to the point, that it was never asked to.

The adapter is thin by design (Slice J), and RCA-03 settled why: a transport that
scanned strings for host paths would be a second privacy rule drifting against the
first, and the direct caller --- who never goes through it --- would silently keep
the defect. So there is no redaction under ``repoatlas/mcp/``, and what is asserted
here is parity: what :class:`~repoatlas.capabilities.service.CapabilityService`
decided to say is exactly what the client receives, in the structured content and
in the text block alike.

The text block is checked on its own account. It is a serialization of the same
envelope, so anything leaking through it reaches a reading model whether or not the
client ever parses ``structuredContent`` --- and F-01 leaked through ``detail``,
which both carry.

CASE 14 belongs here rather than in the capability suite because it is a claim about
a *process*: the sibling host-path-bearing condition in the SQLite layer is reached
before a server can hand anything to a client, so it is checked by starting one.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp.types import CallToolResult
from repoatlas.capabilities.service import CapabilityService
from repoatlas.errors import SyncInProgressError
from repoatlas.services.workspace import Workspace
from repoatlas.sync.lock import lock_path, writer_lock
from tests.transport.conftest import (
    over_protocol,
    over_stdio,
    raw_exchange,
    refusal,
    requires_console_script,
    tool_call,
)

GLOBAL: dict[str, Any] = {"scope": "global"}


def _no_wait(workspace: Workspace) -> None:
    workspace.settings = workspace.settings.model_copy(
        update={
            "storage": workspace.settings.storage.model_copy(update={"lock_timeout_seconds": 0.0})
        }
    )


def _strings(value: object, path: str = "$") -> list[tuple[str, str]]:
    if isinstance(value, dict):
        return [pair for key, item in value.items() for pair in _strings(item, f"{path}.{key}")]
    if isinstance(value, list | tuple):
        return [
            pair for index, item in enumerate(value) for pair in _strings(item, f"{path}[{index}]")
        ]
    if isinstance(value, str):
        return [(path, value)]
    return []


def _text_of(result: CallToolResult) -> str:
    return "".join(getattr(block, "text", "") or "" for block in result.content)


def _contended(service: CapabilityService, database: Path) -> CallToolResult:
    """One ``repoatlas_sync`` call over the protocol while the lock is really held.

    The lock is an ``flock`` on an open file description, so a second handle in this
    same process contends exactly as another process would --- which is what lets the
    whole exchange, contention included, happen inside one test.
    """

    async def body(session: Any) -> CallToolResult:
        return await session.call_tool("repoatlas_sync", {"context": GLOBAL})

    with writer_lock(database, timeout=0.0):
        return over_protocol(service, body)


# ---------------------------------------------------------------------------
# CASES 7-9. The wire
# ---------------------------------------------------------------------------


def test_case_7_the_wire_carries_the_same_safe_envelope_as_the_direct_call(
    indexed: tuple[Workspace, Path, Path],
) -> None:
    """CASE 7. Parity, which is the only thing a thin adapter may promise."""
    workspace, _, _ = indexed
    _no_wait(workspace)
    service = CapabilityService(workspace)
    database = Path(workspace.database.path)

    result = _contended(service, database)
    error = refusal(result)

    assert error["code"] == "SYNC_IN_PROGRESS"
    assert error["category"] == "contention"
    assert error["retryable"] is True
    assert error["exit_code"] == 5
    assert error["detail"] == "Wait for it to finish and try again."
    assert error["message"] == "Another RepoAtlas synchronization is already in progress."


def test_case_8_structured_content_names_no_host_coordinate(
    indexed: tuple[Workspace, Path, Path],
) -> None:
    """CASE 8. Every string field of the structured envelope, walked.

    The lock is held by this process, so the coordinates that must be absent are
    this process's own pid and this machine's real hostname.
    """
    workspace, _, _ = indexed
    _no_wait(workspace)
    service = CapabilityService(workspace)
    database = Path(workspace.database.path)

    result = _contended(service, database)
    assert result.structuredContent is not None

    for field, text in _strings(dict(result.structuredContent)):
        assert str(os.getpid()) not in text, f"{field}: pid"
        assert socket.gethostname() not in text, f"{field}: hostname"
        assert str(lock_path(database)) not in text, f"{field}: lock path"
        assert str(database) not in text, f"{field}: database path"
        assert not re.search(r"\bpid=\d+", text), f"{field}: pid= record"
        assert "Lock holder" not in text, f"{field}: owner prose"
        assert "Lock file" not in text, f"{field}: lock prose"


def test_case_9_the_text_block_is_covered_and_not_merely_the_structured_one(
    indexed: tuple[Workspace, Path, Path],
) -> None:
    """CASE 9. The block a model reads even when nothing parses the structured half."""
    workspace, _, _ = indexed
    _no_wait(workspace)
    service = CapabilityService(workspace)
    database = Path(workspace.database.path)

    result = _contended(service, database)
    text = _text_of(result)

    assert text, "a refusal carries a text block"
    assert "SYNC_IN_PROGRESS" in text
    assert str(os.getpid()) not in text
    assert socket.gethostname() not in text
    assert str(lock_path(database)) not in text
    assert not re.search(r"\bpid=\d+", text)
    assert "Lock holder" not in text
    # The two halves say the same thing, which is what parity means here.
    assert json.loads(text) == dict(result.structuredContent or {})


def test_case_9_the_sync_adapter_holds_no_contention_policy_of_its_own() -> None:
    """CASES 7-9's guard. The safety is upstream, and must stay there.

    A redaction pass under ``repoatlas/mcp/`` would make the wire safe while the
    direct caller kept the leak --- the precise shape RCA-03 rejected.
    """
    import repoatlas.mcp.server as server
    import repoatlas.mcp.tools as tools

    for module in (server, tools):
        source = Path(module.__file__ or "").read_text(encoding="utf-8")
        assert "Lock holder" not in source
        assert "lock_path" not in source
        assert "SYNC_IN_PROGRESS" not in source
        assert "gethostname" not in source


# ---------------------------------------------------------------------------
# CASE 1. The auditor's own reproduction: two processes, one lock, real stdio
# ---------------------------------------------------------------------------


@requires_console_script
def test_case_1_two_real_processes_contending_over_real_stdio(tmp_path: Path) -> None:
    """CASE 1. The FINAL audit's reproduction, exactly as it was performed.

    Everything above contends inside one process, which is sound --- an ``flock``
    is held by an open file description, so a second handle conflicts wherever it
    lives. This one spends the process spawn anyway, because the finding was made
    this way and a correction is worth checking against the thing that found it:
    a separate OS process holds the lock, a separate OS process serves MCP, and
    the bytes in between are a pipe.

    The holder writes its *own* pid and this machine's hostname into the lock file,
    so the coordinates asserted absent are real ones that a leak would carry.
    """
    from tests.adversarial.conftest import ADVERSARIAL_ROOT
    from tests.evaluation.harness import build

    state = tmp_path / "atlas"
    workspace, _ = build(ADVERSARIAL_ROOT, state)
    database = Path(workspace.database.path)
    workspace.close()

    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time\n"
            "from pathlib import Path\n"
            "from repoatlas.sync.lock import writer_lock\n"
            "with writer_lock(Path(sys.argv[1]), timeout=10.0):\n"
            "    print('HELD', flush=True)\n"
            "    time.sleep(float(sys.argv[2]))\n",
            str(database),
            "20",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "HELD", "the holder never took the lock"

        async def body(session: Any) -> CallToolResult:
            return await session.call_tool("repoatlas_sync", {"context": GLOBAL})

        result = over_stdio(state, body, REPOATLAS_SYNC_LOCK_TIMEOUT="0")
    finally:
        holder.kill()
        holder.wait()

    error = refusal(result)
    assert error["code"] == "SYNC_IN_PROGRESS"
    assert error["retryable"] is True
    assert error["detail"] == "Wait for it to finish and try again."

    everything = json.dumps(dict(result.structuredContent or {})) + _text_of(result)
    assert str(holder.pid) not in everything, "the holder's pid crossed"
    assert socket.gethostname() not in everything, "the hostname crossed"
    assert str(lock_path(database)) not in everything, "the lock path crossed"
    assert not re.search(r"\bpid=\d+", everything)
    assert "Lock holder" not in everything


@requires_console_script
def test_case_1_host_paths_remain_opt_in_on_that_same_connection(tmp_path: Path) -> None:
    """CASE 1's contradiction, closed. One server, both questions, opposite answers.

    This pairing is what made F-01 a defect rather than a helpful sentence: the
    same process, over the same pipe, refused to name host paths without an opt-in
    and then named one unasked.
    """
    from tests.adversarial.conftest import ADVERSARIAL_ROOT
    from tests.evaluation.harness import build

    state = tmp_path / "atlas"
    workspace, _ = build(ADVERSARIAL_ROOT, state)
    database = Path(workspace.database.path)
    workspace.close()

    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys, time\n"
            "from pathlib import Path\n"
            "from repoatlas.sync.lock import writer_lock\n"
            "with writer_lock(Path(sys.argv[1]), timeout=10.0):\n"
            "    print('HELD', flush=True)\n"
            "    time.sleep(float(sys.argv[2]))\n",
            str(database),
            "20",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])},
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "HELD"

        async def body(session: Any) -> tuple[Any, Any, Any]:
            withheld = await session.call_tool("repoatlas_repositories", {"context": GLOBAL})
            asked = await session.call_tool(
                "repoatlas_repositories", {"context": GLOBAL, "include_host_paths": True}
            )
            contended = await session.call_tool("repoatlas_sync", {"context": GLOBAL})
            return withheld, asked, contended

        withheld, asked, contended = over_stdio(state, body, REPOATLAS_SYNC_LOCK_TIMEOUT="0")
    finally:
        holder.kill()
        holder.wait()

    withheld_paths = [r["host_path"] for r in (withheld.structuredContent or {})["repositories"]]
    asked_paths = [r["host_path"] for r in (asked.structuredContent or {})["repositories"]]
    assert withheld_paths and all(path is None for path in withheld_paths)
    assert asked_paths and all(path is not None for path in asked_paths)

    assert refusal(contended)["code"] == "SYNC_IN_PROGRESS"
    contended_text = json.dumps(dict(contended.structuredContent or {})) + _text_of(contended)
    for path in asked_paths:
        assert str(path) not in contended_text, "contention disclosed what the opt-in gates"


# ---------------------------------------------------------------------------
# CASE 14. The sibling route, which never reaches a client at all
# ---------------------------------------------------------------------------


def test_case_14_a_startup_index_failure_never_crosses_the_protocol(tmp_path: Path) -> None:
    """CASE 14. The SQLite layer names the index path; no client is there to hear it.

    ``storage/sqlite/errors.py`` interpolates the absolute database path into its
    messages. The FINAL audit classified that as a verified non-public route rather
    than a second F-01, and this pins the reason: the workspace opens before the
    server serves, so an unusable index ends the process on stderr with the protocol
    stream still empty. If that ordering ever changed, this fails.
    """
    state = tmp_path / "atlas"
    (state / "state").mkdir(parents=True)
    database = state / "state" / "atlas.db"
    database.write_bytes(os.urandom(4096))

    code, stdout, stderr = raw_exchange(
        state,
        [tool_call(1, "repoatlas_repositories", {"context": GLOBAL})],
        timeout=30.0,
    )

    assert code != 0, "an unusable index is not a usable server"
    assert stdout.strip() == "", f"the protocol stream carried {stdout!r}"
    # The operator is told exactly what is wrong, on the stream meant for them.
    assert "index" in stderr.lower()
    assert str(database) in stderr


def test_case_14_the_contention_route_is_the_only_one_that_changed(
    indexed: tuple[Workspace, Path, Path],
) -> None:
    """CASE 14's companion. Other refusals keep their wording exactly.

    A correction that quietly sanitized every envelope would be a different change
    with a different risk; only ``SYNC_IN_PROGRESS`` was meant to move.
    """
    workspace, _, _ = indexed
    service = CapabilityService(workspace)

    async def body(session: Any) -> dict[str, Any]:
        result = await session.call_tool(
            "repoatlas_status", {"context": {"scope": "repository", "repository": "no-such-repo"}}
        )
        return refusal(result)

    error = over_protocol(service, body)
    assert error["code"] == "REPOSITORY_NOT_FOUND"
    assert error["detail"], "the remedy still names the repositories that do exist"


# ---------------------------------------------------------------------------
# The condition itself still reaches the transport
# ---------------------------------------------------------------------------


def test_a_contended_sync_is_a_refusal_rather_than_a_silent_success(
    indexed: tuple[Workspace, Path, Path],
) -> None:
    """Contention must not be hidden: nothing was synced, and the caller is told."""
    workspace, _, _ = indexed
    _no_wait(workspace)
    service = CapabilityService(workspace)

    result = _contended(service, Path(workspace.database.path))
    assert result.isError is True
    assert refusal(result)["retryable"] is True


def test_the_lock_is_released_and_a_later_sync_succeeds(
    indexed: tuple[Workspace, Path, Path],
) -> None:
    """Retryable means what it says: the identical call works once the lock is free."""
    workspace, _, _ = indexed
    _no_wait(workspace)
    service = CapabilityService(workspace)

    contended = _contended(service, Path(workspace.database.path))
    assert contended.isError is True

    async def body(session: Any) -> CallToolResult:
        return await session.call_tool("repoatlas_sync", {"context": GLOBAL})

    after = over_protocol(service, body)
    assert after.isError is False
    assert (after.structuredContent or {}).get("status") == "completed"


def test_the_error_is_the_v1_condition_and_not_a_new_one(
    indexed: tuple[Workspace, Path, Path],
) -> None:
    """The capability adds no lock of its own; this is V1's condition, typed."""
    workspace, _, _ = indexed
    _no_wait(workspace)
    service = CapabilityService(workspace)

    from repoatlas.models.capability import CapabilityContext, CapabilityScope
    from repoatlas.models.sync import SyncRequest

    with (
        writer_lock(Path(workspace.database.path), timeout=0.0),
        pytest.raises(SyncInProgressError),
    ):
        service.sync(SyncRequest(context=CapabilityContext(scope=CapabilityScope.GLOBAL)))
