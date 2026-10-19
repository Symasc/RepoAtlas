"""§4, §6, §12 and §21: one service, used once per call, sequentially, closed once.

V2 was designed around one workspace, one database handle and one lock, driven in
order. None of that is enforceable from below --- SQLite will refuse a foreign
thread, but nothing stops a transport from opening a second workspace, or from
calling two capabilities where the caller asked for one. So it is enforced here.

The proxy in this file is what makes those properties observable. It stands where
the service stands, records every capability entered and left, and reports the
deepest overlap it ever saw. A transport that dispatched to a thread pool, or that
followed a search with an inspection of its own, changes a number in it.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from repoatlas.capabilities.service import CapabilityService
from repoatlas.mcp import server as transport
from repoatlas.mcp.server import build_server, serve
from repoatlas.mcp.tools import TOOLS
from repoatlas.services.workspace import Workspace
from tests.transport.conftest import GLOBAL, over_protocol, structured

from mcp import ClientSession

CAPABILITIES = {spec.capability for spec in TOOLS}


class Recording:
    """A stand-in for the service that records what the transport asked of it.

    Deliberately a proxy rather than a subclass: the adapter reaches its
    capabilities by name, so anything that answers to the same names is
    indistinguishable from the real service to the code under test, and nothing
    about the recording can be bypassed by an inherited method.
    """

    def __init__(self, inner: CapabilityService, *, delay: float = 0.0) -> None:
        self._inner = inner
        self._delay = delay
        self._lock = threading.Lock()
        self.calls: list[str] = []
        self.threads: set[int] = set()
        self.depth = 0
        self.max_depth = 0

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._inner, name)
        if name not in CAPABILITIES:
            return attribute

        def recorded(request: object) -> object:
            with self._lock:
                self.calls.append(name)
                self.threads.add(threading.get_ident())
                self.depth += 1
                self.max_depth = max(self.max_depth, self.depth)
            try:
                if self._delay:
                    time.sleep(self._delay)
                return attribute(request)
            finally:
                with self._lock:
                    self.depth -= 1

        return recorded


# ---------------------------------------------------------------------------
# §12: one tool invocation is one capability invocation
# ---------------------------------------------------------------------------


def _arguments_for(tool: str) -> dict[str, Any]:
    if tool == "repoatlas_search":
        return {"question": "Where is SERVICE_HOST configured?", "context": GLOBAL}
    if tool == "repoatlas_inspect":
        return {
            "context": {"scope": "repository", "repository": "python-service"},
            "target": {"path": "src/config.py"},
            "line_start": 1,
            "line_end": 4,
        }
    if tool == "repoatlas_relationships":
        return {"context": GLOBAL, "target": "env:SERVICE_HOST"}
    if tool == "repoatlas_impact":
        return {"context": GLOBAL, "seed": "env:SERVICE_HOST"}
    if tool == "repoatlas_dependencies":
        return {"context": GLOBAL, "limit": 2}
    if tool == "repoatlas_deployment_targets":
        return {"context": {"scope": "repository", "repository": "python-service"}}
    if tool == "repoatlas_http_operations":
        # Every status it returns speaks for one repository, so it takes one.
        return {"context": {"scope": "repository", "repository": "python-service"}, "limit": 2}
    return {"context": GLOBAL}


@pytest.mark.parametrize(
    "spec", [s for s in TOOLS if s.name != "repoatlas_verify_grounding"], ids=lambda s: s.name
)
def test_one_tool_call_reaches_exactly_one_capability(
    service: CapabilityService, spec: Any
) -> None:
    """No fallback, no enrichment, no follow-up.

    A search that quietly inspected its top hit, or a walk that fell back to a
    search when it found nothing, would each be defensible as a convenience and
    would each move the orchestration of an investigation into the transport ---
    where the agent cannot see it, cannot turn it off, and does not pay for it in
    a way it can account for.
    """
    probe = Recording(service)

    async def body(session: ClientSession) -> None:
        result = await session.call_tool(spec.name, _arguments_for(spec.name))
        assert result.isError is False, result.content

    async def main() -> None:
        async with create_connected_server_and_client_session(build_server(probe)) as session:  # type: ignore[arg-type]
            await body(session)

    anyio.run(main)
    assert probe.calls == [spec.capability]


def test_verify_grounding_reaches_exactly_one_capability(service: CapabilityService) -> None:
    """Verification in particular must not go looking for replacements.

    A verify that auto-searched for fresh evidence when a citation went stale
    would be supplying the very thing it was asked to check, and the agent would
    never learn its answer had drifted.
    """
    supplied = service.search(
        __import__("repoatlas.models.search", fromlist=["SearchRequest"]).SearchRequest(
            question="Where is SERVICE_HOST configured?",
            context=__import__(
                "repoatlas.models.capability", fromlist=["CapabilityContext"]
            ).CapabilityContext(scope="global"),
        )
    )
    probe = Recording(service)

    async def main() -> None:
        async with create_connected_server_and_client_session(build_server(probe)) as session:  # type: ignore[arg-type]
            result = await session.call_tool(
                "repoatlas_verify_grounding",
                {
                    "investigation_id": supplied.investigation_id,
                    "answer": {
                        "summary": "s",
                        "claims": [{"statement": "A claim.", "citations": [{"evidence_id": "E1"}]}],
                    },
                },
            )
            assert result.isError is False, result.content

    anyio.run(main)
    assert probe.calls == ["verify_grounding"]


def test_a_refused_call_reaches_no_capability_twice(service: CapabilityService) -> None:
    """A failure is not retried inside the adapter either."""
    probe = Recording(service)

    async def main() -> None:
        async with create_connected_server_and_client_session(build_server(probe)) as session:  # type: ignore[arg-type]
            result = await session.call_tool(
                "repoatlas_relationships", {"context": GLOBAL, "target": "env:NOPE"}
            )
            assert result.isError is True

    anyio.run(main)
    assert probe.calls == ["relationships"]


def test_a_malformed_call_reaches_no_capability_at_all(service: CapabilityService) -> None:
    """Validation happens before the service is touched, so nothing is half-done."""
    probe = Recording(service)

    async def main() -> None:
        async with create_connected_server_and_client_session(build_server(probe)) as session:  # type: ignore[arg-type]
            result = await session.call_tool(
                "repoatlas_relationships", {"context": GLOBAL, "target": "x", "hops": 99}
            )
            assert result.isError is True

    anyio.run(main)
    assert probe.calls == []


# ---------------------------------------------------------------------------
# §6: sequential execution
# ---------------------------------------------------------------------------


def test_two_concurrent_tool_calls_never_overlap_inside_the_service(
    service: CapabilityService,
) -> None:
    """§6. Both requests are in flight; at most one capability body runs.

    The two calls are genuinely concurrent at the protocol level --- the SDK
    dispatches each request in its own task --- so nothing but the adapter's own
    serialization keeps them apart. A transport that handed capability calls to a
    worker pool would raise the observed depth to two and would, below that, be
    driving a SQLite connection from a thread that did not open it.
    """
    probe = Recording(service, delay=0.05)

    async def main() -> None:
        async with (
            create_connected_server_and_client_session(build_server(probe)) as session,  # type: ignore[arg-type]
            anyio.create_task_group() as tasks,
        ):
            tasks.start_soon(session.call_tool, "repoatlas_status", {"context": GLOBAL})
            tasks.start_soon(session.call_tool, "repoatlas_repositories", {"context": GLOBAL})

    anyio.run(main)
    assert sorted(probe.calls) == ["repositories", "status"]
    assert probe.max_depth == 1, "two capability bodies were inside the service at once"


def test_every_capability_runs_on_one_thread(service: CapabilityService) -> None:
    """§6. The thread that drives the service is the thread that opened it.

    V1 binds its SQLite connection to its creating thread, so this is not a
    stylistic preference: a capability called from a pool thread is refused by the
    driver, and the refusal would surface as an internal error on whichever call
    happened to land elsewhere.
    """
    probe = Recording(service)

    async def main() -> None:
        async with create_connected_server_and_client_session(build_server(probe)) as session:  # type: ignore[arg-type]
            for tool in ("repoatlas_status", "repoatlas_repositories", "repoatlas_status"):
                await session.call_tool(tool, {"context": GLOBAL})

    anyio.run(main)
    assert probe.threads == {threading.get_ident()}, probe.threads


def test_many_concurrent_calls_all_answer_and_none_overlap(
    service: CapabilityService,
) -> None:
    """Serialized does not mean dropped: every request still gets its answer."""
    probe = Recording(service, delay=0.01)
    answers: list[Any] = []

    async def one(session: ClientSession) -> None:
        answers.append(await session.call_tool("repoatlas_status", {"context": GLOBAL}))

    async def main() -> None:
        async with (
            create_connected_server_and_client_session(build_server(probe)) as session,  # type: ignore[arg-type]
            anyio.create_task_group() as tasks,
        ):
            for _ in range(6):
                tasks.start_soon(one, session)

    anyio.run(main)
    assert len(answers) == 6
    assert all(answer.isError is False for answer in answers)
    assert probe.max_depth == 1


# ---------------------------------------------------------------------------
# §4 and §21: one service for the process, closed exactly once
# ---------------------------------------------------------------------------


def test_no_tool_call_opens_a_service_of_its_own(service: CapabilityService) -> None:
    """§4. A service per call would reopen the database and lose every ledger.

    Proved by making opening one impossible for the duration: the session below
    exercises every read capability, and any handler that tried to build its own
    service would fail loudly instead of quietly working.
    """
    opened: list[int] = []

    def forbidden(*args: object, **kwargs: object) -> None:
        opened.append(1)
        raise AssertionError("a tool call opened its own CapabilityService")

    original = CapabilityService.open
    CapabilityService.open = classmethod(forbidden)  # type: ignore[assignment,method-assign]
    try:

        async def body(session: ClientSession) -> None:
            for spec in TOOLS:
                if spec.name == "repoatlas_verify_grounding":
                    continue
                await session.call_tool(spec.name, _arguments_for(spec.name))

        over_protocol(service, body)
    finally:
        CapabilityService.open = original  # type: ignore[method-assign]
    assert opened == []


def test_serve_opens_one_service_and_closes_it_once(
    indexed: tuple[Workspace, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """§21. Normal shutdown closes, and closes once."""
    workspace, _, _ = indexed
    capability_service = CapabilityService(workspace)
    closes: list[int] = []
    original_close = capability_service.close

    def counted() -> None:
        closes.append(1)
        original_close()

    monkeypatch.setattr(capability_service, "close", counted)

    served: list[object] = []

    async def fake_run(passed: CapabilityService) -> None:
        served.append(passed)

    monkeypatch.setattr(transport, "run_stdio", fake_run)
    serve(capability_service)

    assert served == [capability_service], "the transport served the one service it was given"
    assert closes == [1]
    assert capability_service.closed is True


def test_serve_closes_the_service_even_when_the_transport_fails(
    indexed: tuple[Workspace, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """§21. A protocol exception must not abandon an open database handle."""
    workspace, _, _ = indexed
    capability_service = CapabilityService(workspace)

    async def fake_run(passed: CapabilityService) -> None:
        raise RuntimeError("the transport died")

    monkeypatch.setattr(transport, "run_stdio", fake_run)
    with pytest.raises(RuntimeError, match="the transport died"):
        serve(capability_service)
    assert capability_service.closed is True


def test_a_startup_failure_leaves_no_half_open_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """§21. If the index cannot be opened, nothing is served at all."""
    started: list[int] = []

    def failing_open(**kwargs: object) -> CapabilityService:
        raise RuntimeError("the index could not be opened")

    async def fake_run(passed: CapabilityService) -> None:
        started.append(1)

    monkeypatch.setattr(CapabilityService, "open", classmethod(lambda cls, **kw: failing_open()))
    monkeypatch.setattr(transport, "run_stdio", fake_run)
    with pytest.raises(RuntimeError, match="could not be opened"):
        serve()
    assert started == [], "the transport must not start against a service that failed to open"


def test_a_failing_tool_leaves_the_server_able_to_answer_the_next_call(
    service: CapabilityService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§21. One bad call is one bad answer, not a wedged server."""

    calls = {"n": 0}
    original = CapabilityService.status

    def flaky(self: CapabilityService, request: object) -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("the first call explodes")
        return original(self, request)  # type: ignore[arg-type]

    monkeypatch.setattr(CapabilityService, "status", flaky)

    async def body(session: ClientSession) -> tuple[Any, Any]:
        first = await session.call_tool("repoatlas_status", {"context": GLOBAL})
        second = await session.call_tool("repoatlas_status", {"context": GLOBAL})
        return first, second

    first, second = over_protocol(service, body)
    assert first.isError is True
    assert second.isError is False
    assert structured(second)["root_configured"] is True
