"""The MCP stdio transport. A translator, and deliberately nothing else.

Three translations happen here and no fourth:

    protocol arguments  ->  a V2 request model
    a V2 response model ->  protocol structured content
    a capability failure->  a protocol-safe structured error

Everything an agent can reach through this module is a capability that already
existed, unchanged, before this module did. There is no retrieval here, no
traversal, no freshness rule, no ledger and no evidence: if a tool appeared to
need one of those, the capability contract would be incomplete and the fix would
belong below this file, not in it.

Why the dispatch is one lookup
------------------------------
A tool call is ``getattr(service, spec.capability)(spec.request(**arguments))``
and then a dump. It is written that way so that "one tool invocation is one
capability invocation" is a property of the code's shape rather than a rule
maintained by review: there is no place to put a second call, because there is no
per-tool function to put it in.

Why the capability call is not awaited
-------------------------------------
The capabilities are ordinary blocking calls, and they are made from the thread
running the event loop rather than handed to a worker. That is not an oversight
and not laziness. V1 binds its SQLite connection to the thread that opened it, so
a call arriving on a pool thread is refused by the driver however correct the
locking above it is; and V2 was designed around one workspace driven
sequentially. Blocking the loop for the duration of a call keeps both true at
once. The gate below is held anyway, so the invariant is stated rather than
inferred from the absence of an ``await``.

Why stdout is taken away from the process
-----------------------------------------
On this transport stdout *is* the protocol. One stray write from any layer --- a
banner, a warning, a progress line, a child process that inherited the descriptor
--- is not noise but a framing error, and the client's next parse fails on it. So
the descriptor is claimed at the file level: the real stdout becomes the protocol
writer, and file descriptor 1 is pointed at stderr for the server's whole life.
Anything that writes to stdout afterwards, in Python or in a child process,
reaches the operator instead of corrupting the stream.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from io import TextIOWrapper
from typing import Any

import anyio
import mcp.types as protocol
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from pydantic import ValidationError

from repoatlas.capabilities.errors import capability_error
from repoatlas.capabilities.presentation import present
from repoatlas.capabilities.service import CapabilityService
from repoatlas.mcp.tools import TOOLS, TOOLS_BY_NAME, ToolSpec
from repoatlas.models.capability import CapabilityError, ErrorCategory

#: What the server calls itself during the handshake.
SERVER_NAME = "repoatlas"

#: Shown to a client once, after initialize. It states the two things an agent
#: cannot discover from a tool list: that investigations are per task, and that a
#: verdict about grounding is not a verdict about truth.
INSTRUCTIONS = (
    "RepoAtlas serves deterministic evidence about locally indexed repositories. "
    "Every call states its own scope; this server has no working directory and "
    "infers no repository from one. Begin a task by calling a tool without an "
    "investigation_id, then echo the investigation_id it returns on every later "
    "call for that same task, including verify_grounding. Do not reuse an "
    "investigation_id for an unrelated task. Verification checks that citations "
    "resolve to evidence supplied in that investigation; it does not check whether "
    "a statement is true. A search item marked declaration is RepoAtlas's record of "
    "a definition, not its source: inspect its entity key to read the source."
)


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------


def tool_definitions() -> list[protocol.Tool]:
    """The nine tools, with schemas derived from the request models themselves.

    ``model_json_schema()`` rather than a hand-written declaration, so enum
    members, numeric bounds, defaults and which fields are optional are stated
    exactly once. A schema copied by hand is a schema that drifts, and the drift
    is silent: the protocol goes on advertising a field the model stopped
    accepting.

    No ``outputSchema`` is declared. The response models schematize cleanly, but
    all of them together come to roughly 124 KB against 28 KB for the inputs, and
    every byte of that is repeated into the tool list an agent reads before it
    asks anything. The SDK also skips its output validation for handlers that
    return a result object directly, which this one must in order to carry a
    structured error --- so declaring them would spend a large part of a caller's
    context advertising a contract nothing here enforces. The structured content
    is still exactly ``response.model_dump(mode="json")``, unless the request asked
    for ``presentation: compact`` (:mod:`repoatlas.capabilities.presentation`).
    """
    return [
        protocol.Tool(
            name=spec.name,
            title=spec.title,
            description=spec.description,
            inputSchema=spec.request.model_json_schema(),
            annotations=protocol.ToolAnnotations(
                title=spec.title,
                readOnlyHint=spec.read_only,
                # Sync changes the index and never the source, so it is a write
                # that destroys nothing. Saying so is the difference between an
                # agent asking permission and an agent refusing to sync at all.
                destructiveHint=False,
                idempotentHint=False,
                # Nothing here reaches beyond this machine's index.
                openWorldHint=False,
            ),
        )
        for spec in TOOLS
    ]


def _error_result(error: CapabilityError) -> protocol.CallToolResult:
    """A refusal, as structured content a client can branch on.

    ``ok`` is false and the envelope is V2's own :class:`CapabilityError` dumped
    whole, so ``code``, ``category``, ``retryable``, ``detail`` and the structured
    ``context`` all survive the transport. A successful result is deliberately
    *not* wrapped in a matching ``{"ok": true}``: it is the V2 response and
    nothing more, and ``isError`` is what separates the two.
    """
    payload: dict[str, Any] = {"ok": False, "error": error.model_dump(mode="json")}
    return protocol.CallToolResult(
        content=[protocol.TextContent(type="text", text=_json_text(payload))],
        structuredContent=payload,
        isError=True,
    )


def _success_result(payload: dict[str, Any]) -> protocol.CallToolResult:
    """A capability response, unwrapped.

    The same object twice: once as structured content for a client that reads it,
    and once as its JSON text for a client that reads only content blocks. Not a
    summary and not prose --- a caller that parses the text block gets back
    exactly what the structured field holds.
    """
    return protocol.CallToolResult(
        content=[protocol.TextContent(type="text", text=_json_text(payload))],
        structuredContent=payload,
        isError=False,
    )


def _json_text(payload: dict[str, Any]) -> str:
    """The text block: ``payload`` as compact JSON.

    Compact because the block is read by a model, not a person, and it is a second
    copy of everything in ``structuredContent``. Pretty-printing it spent about a
    sixth of a search's text block on indentation (3.7 KB of 22 KB for a
    twelve-item answer on LVL), on every call, for a layout no client parses.
    ``ensure_ascii`` is off for the same reason: an em dash in quoted source is
    three bytes as itself and seven once escaped and then escaped again inside the
    JSON-RPC message. The value is unchanged either way, so a caller that parses
    the text gets exactly what the structured field holds.
    """
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def _unknown_tool(name: str) -> CapabilityError:
    return CapabilityError(
        code="UNKNOWN_TOOL",
        category=ErrorCategory.INVALID_REQUEST,
        message=f"no such tool: {name}",
        detail="Call tools/list for the tools this server exposes.",
        retryable=False,
        context={"available": [spec.name for spec in TOOLS]},
    )


def _invalid_request(spec: ToolSpec, exc: ValidationError) -> CapabilityError:
    """A malformed argument set, reported by field rather than as a traceback.

    Pydantic stays the validation authority: the arguments are handed to the real
    request model and whatever it refuses is refused here, with the same bounds,
    the same enums and the same cross-field rules. What crosses the wire is the
    location and the reason, which is what a caller needs to fix the call.

    The offending ``input`` is deliberately dropped. It is the one part of a
    validation error that can be arbitrarily large and can hold repository text a
    caller pasted in, and a client never needs its own argument echoed back.
    """
    fields = [
        {
            "field": ".".join(str(part) for part in error["loc"]) or "<root>",
            "error": error["msg"],
        }
        for error in exc.errors()
    ]
    return CapabilityError(
        code="INVALID_REQUEST",
        category=ErrorCategory.INVALID_REQUEST,
        message=f"the arguments do not form a valid {spec.name} request",
        detail="; ".join(f"{item['field']}: {item['error']}" for item in fields),
        retryable=False,
        exit_code=2,
        context={"tool": spec.name, "fields": fields},
    )


def invoke(service: CapabilityService, spec: ToolSpec, arguments: dict[str, Any]) -> Any:
    """Convert, call once, dump. The entire body of every tool.

    There is no per-tool handler because there is nothing per-tool to do. Anything
    that had to differ between two capabilities here would be product logic, and
    product logic in a transport is the defect this slice exists to avoid.
    """
    request = spec.request.model_validate(arguments)
    response = getattr(service, spec.capability)(request)
    # In full, or in the compact projection the request asked for. Not per-tool
    # logic: one function decides for every capability, below this layer.
    return present(request, response)


def dispatch(
    service: CapabilityService, name: str, arguments: dict[str, Any]
) -> protocol.CallToolResult:
    """One tool call, resolved to one capability call or to one refusal.

    Every failure becomes a structured result rather than an exception. Letting
    one escape would hand the SDK's generic handler a bare ``str(exc)``, which
    flattens a code, a category and a retryable flag into a sentence --- and a
    sentence is the one thing a machine client cannot branch on.

    What is *not* an error here is as important as what is. A search that matched
    nothing, a walk with no edges, an inventory with no packages and a verdict of
    ungrounded are all successful calls: the capability was asked a question and
    answered it. Only a refusal to operate is a tool error.
    """
    spec = TOOLS_BY_NAME.get(name)
    if spec is None:
        return _error_result(_unknown_tool(name))
    try:
        payload = invoke(service, spec, arguments)
    except ValidationError as exc:
        return _error_result(_invalid_request(spec, exc))
    except Exception as exc:  # every failure crosses the wire as data, never as a raise
        return _error_result(capability_error(exc))
    return _success_result(payload)


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------


def build_server(service: CapabilityService) -> Server[object, object]:
    """Bind the nine tools to one already-open service.

    Exactly two handlers are registered, which is also how the handshake comes to
    advertise tools and nothing else: the SDK derives server capabilities from the
    handlers that exist, so declaring no resource and no prompt handler is what
    makes this server incapable of serving either. That is the intended guarantee
    and not an omission --- a generic file resource would be a second way to reach
    repository text, one that never passes through admission or the evidence
    ledger, and an agent could then quote source that no investigation recorded.
    """
    server: Server[object, object] = Server(
        SERVER_NAME,
        version=_package_version(),
        instructions=INSTRUCTIONS,
    )
    #: Serializes tool calls against each other. The capability call below blocks
    #: the loop and so cannot overlap another by itself, but the gate says so
    #: explicitly: acquiring it is a checkpoint, so a second request that has
    #: already arrived waits here rather than in the transport, and the property
    #: survives anyone later making the call awaitable.
    gate = anyio.Lock()

    @server.list_tools()
    async def _list_tools() -> list[protocol.Tool]:
        return tool_definitions()

    # Input validation is left off deliberately: the SDK's own JSON Schema check
    # returns a prose string, while the real request model returns a field list.
    # Pydantic is the validation authority either way, so the weaker check would
    # only change malformed calls from structured refusals into sentences.
    @server.call_tool(validate_input=False)
    async def _call_tool(name: str, arguments: dict[str, Any]) -> protocol.CallToolResult:
        async with gate:
            return dispatch(service, name, arguments)

    return server


def _package_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("repoatlas")
    except PackageNotFoundError:  # pragma: no cover - only outside an install
        return "0"


@contextmanager
def _protocol_stdout() -> Iterator[Any]:
    """Hand the real stdout to the protocol and point everything else at stderr.

    Done with file descriptors rather than by reassigning :data:`sys.stdout`, so
    the guarantee covers writes this process never makes through Python: a C
    extension, and any child process that inherits descriptor 1. Restored on the
    way out, because a caller that embedded this server still owns its own stdout.
    """
    sys.stdout.flush()
    protocol_fd = os.dup(1)
    os.dup2(2, 1)
    try:
        handle = TextIOWrapper(
            open(protocol_fd, "wb", buffering=0, closefd=False),  # noqa: SIM115
            encoding="utf-8",
        )
        try:
            yield anyio.wrap_file(handle)
        finally:
            handle.flush()
    finally:
        os.dup2(protocol_fd, 1)
        os.close(protocol_fd)


async def run_stdio(service: CapabilityService) -> None:
    """Serve one already-open service over stdio until the transport ends."""
    server = build_server(service)
    with _protocol_stdout() as stdout:
        async with stdio_server(stdout=stdout) as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())


def serve(service: CapabilityService | None = None) -> None:
    """Open one service, serve it over stdio, and close it exactly once.

    One service for the process's whole life. Not one per connection and not one
    per call: the workspace, its database handle and the investigations live in
    it, so a service per call would open a database eight times for one
    investigation and lose every evidence id between the calls that produced them
    and the call that verifies them.

    ``require_root=False`` on purpose. A server that refused to start against an
    uninitialized workspace would answer every question with a dead transport,
    while the capabilities already have a precise answer for that state and can
    say it through the tool that was called. Initialization itself stays a V1
    command; nothing here creates an index.

    A service passed in is closed here too. A caller that wants to keep one is
    expected to drive :func:`run_stdio` rather than this.
    """
    owned = service if service is not None else CapabilityService.open(require_root=False)
    try:
        anyio.run(run_stdio, owned)
    finally:
        owned.close()
