"""A degraded stage crossing the wire (RC audit, RCA-08).

The capability suite proves the trace is safe. This one proves the transport did
not undo that, and --- more importantly --- that it was never asked to.

The adapter is thin on purpose (Slice J), and RCA-03 settled the principle: a
transport that scanned strings for host paths would be a second privacy rule
drifting against the first, and the first would rot the moment anyone trusted the
second. So there is no redaction under ``repoatlas/mcp/``, and the assertion here
is parity: what ``CapabilityService.search`` decided to say is exactly what the
client receives, in both the structured content and the text block the SDK
derives from it.

The text block matters on its own. It is a serialization of the same response, so
a leak reaches a reading model through it whether or not the client ever parses
``structuredContent`` --- and a suite that checked only the structured half would
pass while the words were still on screen.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from mcp.types import CallToolResult
from repoatlas.capabilities.service import CapabilityService
from repoatlas.embeddings.fastembed_backend import FastEmbedModel
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.search import SearchRequest
from repoatlas.retrieval.retriever import SEMANTIC_UNAVAILABLE
from repoatlas.services.workspace import Workspace
from tests.capabilities.test_trace_privacy import (
    AUDIT_MESSAGE,
    MODEL_ID,
    SENTINELS,
    walk,
)
from tests.transport.conftest import GLOBAL, over_protocol, structured

#: The adversarial ecosystem is embedded with the same deterministic backend the
#: capability suite uses, so a refusing backend answers to the same id here.
DIM = 128

QUESTION = "how does the service read its database settings"


def refusing_backend(message: str) -> FastEmbedModel:
    def loader() -> Any:
        raise OSError(message)

    return FastEmbedModel(MODEL_ID, dim=DIM, loader=loader)


@pytest.fixture
def degraded(
    indexed: tuple[Workspace, Path, Path],
) -> Iterator[tuple[CapabilityService, Workspace]]:
    """One service over the adversarial index, with an embedding backend that refuses."""
    workspace, _, _ = indexed
    workspace._embeddings = refusing_backend(AUDIT_MESSAGE)
    workspace._embeddings_built = True
    capability_service = CapabilityService(workspace)
    try:
        yield capability_service, workspace
    finally:
        capability_service.close()


def call(capability_service: CapabilityService, *, trace: bool = True) -> CallToolResult:
    async def body(session: Any) -> Any:
        return await session.call_tool(
            "repoatlas_search",
            {"context": GLOBAL, "question": QUESTION, "include_trace": trace},
        )

    return over_protocol(capability_service, body)


def text_of(result: CallToolResult) -> str:
    """Every text block the SDK produced, which is what a model actually reads."""
    return " ".join(getattr(block, "text", "") for block in result.content)


def everything(result: CallToolResult) -> str:
    """Structured content and text content together, unescaped.

    Joined from raw values rather than from JSON text so a backslash-bearing path
    is searched for in the form it was injected in; the serialized text is
    included as well, so an escaped occurrence is caught too.
    """
    payload = result.structuredContent or {}
    return " ".join(walk(payload)) + " " + text_of(result)


def semantic_detail(result: CallToolResult) -> str:
    payload = structured(result)
    stages = payload["trace"]["stages"]
    return next(stage["detail"] for stage in stages if stage["name"] == "semantic")


# ---------------------------------------------------------------------------
# CASE 9: the transport says exactly what the capability said
# ---------------------------------------------------------------------------


def test_the_wire_carries_the_same_safe_trace_as_the_direct_call(
    degraded: tuple[CapabilityService, Workspace],
) -> None:
    """CASE 9. Semantic parity, not merely "both are safe".

    Two independently safe answers would still be a defect: an agent that reasons
    about a trace over MCP must be reading the same diagnostic a direct caller
    reads, or the two deployments disagree about what happened.
    """
    capability_service, _ = degraded

    direct = capability_service.search(
        SearchRequest(
            context=CapabilityContext(scope=CapabilityScope.GLOBAL),
            question=QUESTION,
            include_trace=True,
        )
    )
    assert direct.trace is not None

    payload = structured(call(capability_service))
    assert payload["trace"]["semantic_detail"] == direct.trace.semantic_detail
    assert payload["trace"]["semantic_used"] == direct.trace.semantic_used
    assert [stage["name"] for stage in payload["trace"]["stages"]] == [
        stage.name for stage in direct.trace.stages
    ]
    assert [stage["detail"] for stage in payload["trace"]["stages"]] == [
        stage.detail for stage in direct.trace.stages
    ]
    assert semantic_detail(call(capability_service)) == SEMANTIC_UNAVAILABLE


def test_the_adapter_holds_no_privacy_policy_of_its_own() -> None:
    """The transport is thin, and that is what keeps one rule instead of two.

    If redaction ever appears under ``repoatlas/mcp/``, the capability boundary has
    stopped being the place the guarantee lives --- and the direct caller, who
    never goes through this adapter, silently loses it.
    """
    server = Path("repoatlas/mcp/server.py").read_text(encoding="utf-8")

    for forbidden in ("redact", "sanitize", "scrub", "SEMANTIC_UNAVAILABLE"):
        assert forbidden not in server


# ---------------------------------------------------------------------------
# CASES 1-6 over the wire, in both halves of the result
# ---------------------------------------------------------------------------


def test_the_audit_reproduction_does_not_cross_the_wire(
    degraded: tuple[CapabilityService, Workspace],
) -> None:
    """CASE 1, transported. The response still succeeds; it just says less."""
    capability_service, _ = degraded
    result = call(capability_service)

    assert result.isError is False
    body = everything(result)
    assert "/home/private-user/.cache/huggingface/PRIVATE_MODEL_DIR" not in body
    assert "RCA08_SECRET_SENTINEL_918273" not in body
    assert "embedding loader failed" not in body
    assert structured(result)["evidence"]


@pytest.mark.parametrize("sentinel", SENTINELS)
def test_no_shape_of_private_text_crosses_the_wire(
    indexed: tuple[Workspace, Path, Path], sentinel: str
) -> None:
    """CASES 2-6, transported. §18's cross-slice assertion, one shape at a time.

    Backend exception -> retrieval degradation -> ``SearchResponse.trace`` -> MCP,
    asserted recursively over every field rather than against one Linux-shaped
    path. The Windows and UNC cases are the ones a ``json.dumps`` substring check
    would wave through.
    """
    workspace, _, _ = indexed
    workspace._embeddings = refusing_backend(f"loader exploded: {sentinel}")
    workspace._embeddings_built = True
    capability_service = CapabilityService(workspace)
    try:
        body = everything(call(capability_service))
    finally:
        capability_service.close()

    assert sentinel not in body
    for fragment in sentinel.replace("\\", "/").split("/"):
        if len(fragment) > 8:
            assert fragment not in body, fragment


def test_the_text_block_is_covered_and_not_merely_the_structured_one(
    degraded: tuple[CapabilityService, Workspace],
) -> None:
    """A guard on the guard above.

    If the SDK ever stopped emitting a text block, every assertion about text
    content would pass by having nothing to search. This fails in that case, so
    the coverage above cannot become vacuous without saying so.
    """
    result = call(degraded[0])

    text = text_of(result)
    assert text.strip(), "the result carried no text block to check"
    assert SEMANTIC_UNAVAILABLE in text, "the safe detail should be visible in text too"


# ---------------------------------------------------------------------------
# CASE 8 over the wire
# ---------------------------------------------------------------------------


def test_without_a_trace_the_wire_carries_no_diagnostic(
    degraded: tuple[CapabilityService, Workspace],
) -> None:
    """CASE 8, transported. A caller that asked for no diagnostics gets none."""
    result = call(degraded[0], trace=False)

    payload = structured(result)
    assert payload.get("trace") is None
    body = everything(result)
    assert SEMANTIC_UNAVAILABLE not in body
    assert "RCA08_SECRET_SENTINEL_918273" not in body
    assert "PRIVATE_MODEL_DIR" not in body


# ---------------------------------------------------------------------------
# The degradation is still legible to whoever is on the far end
# ---------------------------------------------------------------------------


def test_a_client_can_still_tell_that_semantic_retrieval_was_unavailable(
    degraded: tuple[CapabilityService, Workspace],
) -> None:
    """Sanitizing must not make the failure invisible over the transport.

    The whole reason the stage is non-fatal is so that a caller gets an answer
    anyway. A caller that cannot see the answer was produced without the vector
    stage cannot judge how complete it is.
    """
    payload = structured(call(degraded[0]))

    assert payload["trace"]["semantic_used"] is False
    assert semantic_detail(call(degraded[0])) == SEMANTIC_UNAVAILABLE
    semantic = next(stage for stage in payload["trace"]["stages"] if stage["name"] == "semantic")
    assert semantic["candidates_out"] == 0
    # And the deterministic half of the funnel still answered.
    assert payload["evidence"]
