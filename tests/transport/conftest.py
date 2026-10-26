"""Fixtures for the MCP transport tests (V2 Slice J).

The directory is ``tests/transport`` rather than ``tests/mcp`` for one concrete
reason: pytest imports a ``conftest.py`` under the first ancestor that is not a
package, so ``tests/mcp/conftest.py`` would be imported as ``mcp.conftest`` and a
top-level package named ``mcp`` would shadow the SDK for the whole run. The
production package is ``repoatlas.mcp``, which is namespaced and cannot collide.

Two ways to reach the server, and both are the real protocol.

:func:`over_protocol` connects an SDK client to an in-process server over memory
streams. Every message is encoded, framed, dispatched and decoded exactly as it
would be on a pipe; what it skips is the process boundary. It is the default here
because most of what Slice J has to prove is about *meaning* — that a schema
matches its model, that an error keeps its code, that two investigations stay
apart — and proving those eighty times over eighty process spawns would buy
nothing.

:func:`over_stdio` starts the installed ``repoatlas mcp`` binary and talks to it
down a pipe. That is the only way to establish the things a process boundary is
made of: that the entry point exists and starts, that the handshake completes
against a cold process, that stdout carries protocol and nothing else, and that
investigations do not survive a restart. Calling handler functions in Python
would assert none of it.

The ecosystem under test is the adversarial one — the same fixtures the retrieval,
graph, dependency and grounding corpora run against — because an adapter proved
only against a repository with nothing difficult in it has been proved against
nothing. Building it costs under a second, so every test gets its own copy and
none of them can leave state for the next.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult
from repoatlas.capabilities.service import CapabilityService
from repoatlas.mcp.server import build_server
from repoatlas.services.workspace import Workspace

from mcp import ClientSession, StdioServerParameters

#: The console script the ``mcp`` command lives behind.
CLI = Path(sys.executable).parent / "repoatlas"

requires_console_script = pytest.mark.skipif(
    not CLI.exists(), reason="the repoatlas console script is not installed in this environment"
)

#: The context every scope-wide call in these tests uses.
GLOBAL: dict[str, Any] = {"scope": "global"}


def repository(slug: str) -> dict[str, Any]:
    return {"scope": "repository", "repository": slug}


# ---------------------------------------------------------------------------
# An indexed ecosystem
# ---------------------------------------------------------------------------


@pytest.fixture
def indexed(tmp_path: Path) -> Iterator[tuple[Workspace, Path, Path]]:
    """The adversarial ecosystem, discovered and fully indexed on disk.

    Returns the open workspace, the managed root, and the state directory holding
    the config and database — the last so that a test can start a real server
    process against the very same index this one is reading.
    """
    from tests.adversarial.conftest import ADVERSARIAL_ROOT
    from tests.evaluation.harness import build

    state = tmp_path / "atlas"
    workspace, root = build(ADVERSARIAL_ROOT, state)
    try:
        yield workspace, root, state
    finally:
        workspace.close()
        # Every test copies the ecosystem and indexes it; pytest keeps each test's temporary
        # directory for the whole run, which exhausts a small CI disk quota. Nothing reads
        # the copy after the test, so it goes now.
        shutil.rmtree(state, ignore_errors=True)


@pytest.fixture
def service(indexed: tuple[Workspace, Path, Path]) -> Iterator[CapabilityService]:
    """One capability service over that index, closed when the test ends."""
    workspace, _, _ = indexed
    capability_service = CapabilityService(workspace)
    try:
        yield capability_service
    finally:
        capability_service.close()


# ---------------------------------------------------------------------------
# Driving the protocol
# ---------------------------------------------------------------------------


def over_protocol(
    capability_service: CapabilityService, body: Callable[[ClientSession], Awaitable[Any]]
) -> Any:
    """Run ``body`` against a real client session bound to one service.

    The session is a genuine :class:`ClientSession`: it performs the initialize
    handshake, and every call it makes is a JSON-RPC request the server decodes.
    Tests stay synchronous, which keeps their assertions where they are readable.
    """

    async def main() -> Any:
        server = build_server(capability_service)
        async with create_connected_server_and_client_session(server) as session:
            return await body(session)

    return anyio.run(main)


def server_environment(state: Path, **overrides: str) -> dict[str, str]:
    """A minimal environment pointing a real server at one prepared index.

    Explicit rather than inherited, so nothing about the developer's own RepoAtlas
    installation can reach the process under test — and so the absence of an
    ambient working directory is a property of the fixture rather than a hope.
    """
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(state / "home"),
        "REPOATLAS_CONFIG": str(state / "config.yaml"),
        "REPOATLAS_DB": str(state / "state" / "atlas.db"),
        # An isolated worktree may reuse a virtualenv whose editable install
        # points at another checkout. Exercise this checkout across the process
        # boundary too, independent of the server's working directory.
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
    }
    environment.update(overrides)
    (state / "home").mkdir(exist_ok=True)
    return environment


@asynccontextmanager
async def stdio_session(
    state: Path, *, cwd: Path | None = None, errlog: Any = None, **overrides: str
) -> AsyncIterator[ClientSession]:
    """A client session talking to a freshly started ``repoatlas mcp`` process."""
    parameters = StdioServerParameters(
        command=str(CLI),
        args=["mcp"],
        env=server_environment(state, **overrides),
        cwd=str(cwd) if cwd is not None else None,
    )
    async with (
        stdio_client(parameters, errlog=errlog or sys.stderr) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session


def over_stdio(
    state: Path,
    body: Callable[[ClientSession], Awaitable[Any]],
    *,
    cwd: Path | None = None,
    **overrides: str,
) -> Any:
    """Start a real server process, run ``body`` against it, shut it down."""

    async def main() -> Any:
        async with stdio_session(state, cwd=cwd, **overrides) as session:
            return await body(session)

    return anyio.run(main)


def raw_exchange(
    state: Path,
    requests: list[dict[str, Any]],
    *,
    command: list[str] | None = None,
    timeout: float = 60.0,
    **overrides: str,
) -> tuple[int, str, str]:
    """Drive the server with handwritten JSON-RPC and keep the streams apart.

    Used where the question is about the *bytes* on the wire rather than about
    what a capability answered. An SDK client parses stdout and discards whatever
    it cannot understand, which is precisely the evidence a stdout-cleanliness
    test needs to see; here every byte of both streams comes back untouched.

    Requests are written and then stdout is read until every one of them has been
    answered, and only then is stdin closed. Closing it earlier would end the
    session while a call was still running --- which is correct behaviour for the
    server and a race for the test, since a slow capability's reply would
    sometimes arrive and sometimes be cut off.
    """
    process = subprocess.Popen(
        command or [str(CLI), "mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=server_environment(state, **overrides),
        text=True,
        encoding="utf-8",
    )
    assert process.stdin is not None and process.stdout is not None

    # Drained continuously: a full stderr pipe would block the server mid-reply.
    collected: list[str] = []

    def drain() -> None:
        assert process.stderr is not None
        collected.append(process.stderr.read())

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()

    for request in requests:
        process.stdin.write(json.dumps(request) + "\n")
    process.stdin.flush()

    awaiting = {request["id"] for request in requests if "id" in request}
    lines: list[str] = []
    while awaiting:
        line = process.stdout.readline()
        if not line:
            break
        lines.append(line)
        try:
            decoded = json.loads(line)
        except json.JSONDecodeError:
            continue  # kept verbatim; proving it is there is the whole point
        if isinstance(decoded, dict):
            awaiting.discard(decoded.get("id"))

    # End of input: ``communicate`` closes stdin itself. Closing it first breaks
    # Python 3.10, whose ``communicate`` flushes stdin before checking it is open.
    try:
        remaining, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:  # pragma: no cover - only on a hang
        process.kill()
        remaining, _ = process.communicate()
        raise
    reader.join(timeout=timeout)
    return process.returncode, "".join(lines) + (remaining or ""), "".join(collected)


def protocol_messages(stdout: str) -> list[dict[str, Any]]:
    """Every stdout line, parsed as JSON-RPC, insisting that all of them are.

    The assertion that matters is not what the messages say but that *nothing
    else* is there. One banner line makes the client's next parse fail, and the
    failure surfaces as a dead server rather than as the print it actually was.
    """
    messages: list[dict[str, Any]] = []
    for number, line in enumerate(stdout.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            decoded = json.loads(line)
        except json.JSONDecodeError as exc:  # pragma: no cover - the failure path
            raise AssertionError(f"stdout line {number} is not protocol: {line!r}") from exc
        assert isinstance(decoded, dict), f"stdout line {number} is not a message: {line!r}"
        assert decoded.get("jsonrpc") == "2.0", f"stdout line {number} is not JSON-RPC: {line!r}"
        messages.append(decoded)
    return messages


#: The initialize handshake, by hand, for :func:`raw_exchange`.
INITIALIZE: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "slice-j-tests", "version": "0"},
    },
}

INITIALIZED: dict[str, Any] = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def tool_call(request_id: int, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": dict(arguments)},
    }


# ---------------------------------------------------------------------------
# Reading results
# ---------------------------------------------------------------------------


def structured(result: CallToolResult) -> dict[str, Any]:
    """The structured content of a successful call, insisted upon.

    A tool that answered in prose would fail here rather than somewhere later and
    vaguer: structured content is the contract, and ``isError`` is what separates
    a capability's answer from a refusal to give one.
    """
    assert result.isError is False, result.content
    assert result.structuredContent is not None, "a successful call carries structured content"
    return dict(result.structuredContent)


def refusal(result: CallToolResult) -> dict[str, Any]:
    """The error envelope of a refused call."""
    assert result.isError is True, result.structuredContent
    assert result.structuredContent is not None, "a refusal carries structured content too"
    payload = dict(result.structuredContent)
    assert payload["ok"] is False
    return dict(payload["error"])
