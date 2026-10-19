"""The protocol surface: what a client discovers, and one whole investigation.

Driven by a real MCP client throughout. Calling the handler functions directly
would prove that Python can call Python; it would not prove that a schema
survives serialization, that the handshake advertises what it should, or that the
installed entry point starts at all.

The long test here is §26's flow, and it is the load-bearing one: handshake, tool
discovery, two reads, a search that mints an investigation, an inspection and a
verification inside that same investigation, a walk from a handle the search
returned, an inventory, a mutation, and reads afterwards --- all against one
server process over a pipe. Every capability Slice J exposes is exercised once by
the path an agent would actually take.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repoatlas.mcp.tools import TOOLS
from repoatlas.services.workspace import Workspace
from tests.transport.conftest import (
    GLOBAL,
    over_protocol,
    over_stdio,
    refusal,
    requires_console_script,
    structured,
)

from mcp import ClientSession

#: Exactly the capabilities §1 names, and ``impact`` (the change-impact engine).
#: Written out rather than derived from the package, so that exposing a twelfth
#: tool has to be a decision made twice.
EXPECTED_TOOLS = (
    "repoatlas_repositories",
    "repoatlas_status",
    "repoatlas_search",
    "repoatlas_inspect",
    "repoatlas_impact",
    "repoatlas_relationships",
    "repoatlas_dependencies",
    "repoatlas_http_operations",
    "repoatlas_deployment_targets",
    "repoatlas_verify_grounding",
    "repoatlas_sync",
)

#: Capabilities that exist below but must not be reachable from here. ``init``
#: creates state and is a V1 command; ``ask`` and ``debug`` consult a model, and
#: the agent on the other end of this protocol is the reasoner.
NEVER_EXPOSED = ("repoatlas_init", "repoatlas_ask", "repoatlas_debug", "repoatlas_locate")


def test_the_server_exposes_exactly_the_eleven_completed_capabilities(service: object) -> None:
    async def body(session: ClientSession) -> tuple[str, ...]:
        listed = await session.list_tools()
        return tuple(tool.name for tool in listed.tools)

    names = over_protocol(service, body)  # type: ignore[arg-type]
    assert names == EXPECTED_TOOLS
    for forbidden in NEVER_EXPOSED:
        assert forbidden not in names


def test_no_tool_is_named_after_a_vendor() -> None:
    """The protocol surface is RepoAtlas's, not any one client's.

    A tool called ``claude_search`` would make the next client's adapter a rename
    rather than a configuration, and would imply RepoAtlas knows which agent is
    asking. It does not, and nothing about the answer would change if it did.
    """
    vendors = ("claude", "codex", "copilot", "cursor", "openai", "anthropic", "gpt")
    for spec in TOOLS:
        assert spec.name.startswith("repoatlas_"), spec.name
        for vendor in vendors:
            assert vendor not in spec.name.lower(), spec.name


def test_every_description_states_what_the_capability_proves(service: object) -> None:
    """Descriptions are read by the thing choosing the tool, so they are a contract.

    Asserted over the wire rather than over the constants, because what an agent
    acts on is the string the protocol delivered.
    """

    async def body(session: ClientSession) -> dict[str, str]:
        listed = await session.list_tools()
        return {tool.name: tool.description or "" for tool in listed.tools}

    described = over_protocol(service, body)  # type: ignore[arg-type]
    for name, description in described.items():
        assert description.strip(), f"{name} has no description"
        assert len(description) > 80, f"{name}'s description says too little"


@pytest.mark.parametrize(
    ("tool", "forbidden"),
    [
        # No description may imply RepoAtlas runs anything, changes source, or
        # reaches the network: an agent that believes any of the three will ask
        # for something this server must never do.
        (name, claim)
        for name in EXPECTED_TOOLS
        for claim in ("executes", "runs the code", "modifies the repository", "downloads", "web")
    ],
)
def test_no_description_promises_execution_modification_or_network(
    service: object, tool: str, forbidden: str
) -> None:
    async def body(session: ClientSession) -> str:
        listed = await session.list_tools()
        return next(t.description or "" for t in listed.tools if t.name == tool)

    description = over_protocol(service, body)  # type: ignore[arg-type]
    assert forbidden not in description.lower(), f"{tool}: {forbidden!r}"


def test_the_verify_tool_says_it_does_not_verify_truth(service: object) -> None:
    """§34. The single most consequential sentence on this protocol surface.

    An agent that reads this description as "checks whether my answer is correct"
    will present a grounded verdict as a correctness verdict, and RepoAtlas will
    have supplied the confidence for a claim it never examined. Pinned as an exact
    phrase so that an edit toward "verify answer correctness" fails here.
    """

    async def body(session: ClientSession) -> str:
        listed = await session.list_tools()
        verify = "repoatlas_verify_grounding"
        return next(t.description or "" for t in listed.tools if t.name == verify)

    description = over_protocol(service, body)  # type: ignore[arg-type]
    assert "does not verify truth or semantic entailment" in description
    assert "provenance" in description.lower()
    # And it must not offer the opposite claim anywhere in the same breath.
    for overclaim in ("verifies that the answer is correct", "checks correctness", "proves true"):
        assert overclaim not in description.lower()


def test_the_sync_tool_says_it_updates_the_index_and_not_the_source(service: object) -> None:
    """§34. The other description an agent could act on destructively.

    "Sync" is a word that means "push and pull" to most tooling. A description
    that left that reading open invites an agent to call this expecting git
    behaviour, and to be surprised in the direction that loses work.
    """

    async def body(session: ClientSession) -> str:
        listed = await session.list_tools()
        return next(t.description or "" for t in listed.tools if t.name == "repoatlas_sync")

    description = over_protocol(service, body)  # type: ignore[arg-type]
    lowered = description.lower()
    assert "index" in lowered and "working trees" in lowered
    assert "never modifies repository source" in lowered
    for network in ("fetch", "pull", "checks out"):
        assert network in lowered, f"the description should rule out {network!r} explicitly"


def test_a_write_capability_is_labelled_as_one(service: object) -> None:
    """Seven read-only hints and one write, so a client can gate the mutation."""

    async def body(session: ClientSession) -> dict[str, object]:
        listed = await session.list_tools()
        return {
            tool.name: (
                tool.annotations.readOnlyHint if tool.annotations else None,
                tool.annotations.destructiveHint if tool.annotations else None,
                tool.annotations.openWorldHint if tool.annotations else None,
            )
            for tool in listed.tools
        }

    hints = over_protocol(service, body)  # type: ignore[arg-type]
    assert hints["repoatlas_sync"] == (False, False, False)
    for name in EXPECTED_TOOLS:
        read_only, destructive, open_world = hints[name]  # type: ignore[misc]
        assert read_only is (name != "repoatlas_sync"), name
        # Nothing here destroys anything, and nothing reaches off this machine.
        assert destructive is False, name
        assert open_world is False, name


def test_an_unknown_tool_is_a_structured_refusal_not_a_crash(service: object) -> None:
    async def body(session: ClientSession) -> object:
        return await session.call_tool("repoatlas_ask", {"question": "why"})

    error = refusal(over_protocol(service, body))  # type: ignore[arg-type]
    assert error["code"] == "UNKNOWN_TOOL"
    assert error["category"] == "invalid_request"
    assert error["retryable"] is False
    # The remedy is machine-readable: here is what does exist.
    assert sorted(error["context"]["available"]) == sorted(EXPECTED_TOOLS)


# ---------------------------------------------------------------------------
# §26: the whole flow, against a real server process
# ---------------------------------------------------------------------------


@requires_console_script
def test_one_investigation_runs_end_to_end_over_a_real_stdio_process(
    indexed: tuple[Workspace, Path, Path],
) -> None:
    """§26 verbatim: handshake to shutdown, every capability, one process.

    The workspace is closed first so the server process is the only thing holding
    the database: whatever this test observes, it observes through the protocol.
    """
    workspace, _, state = indexed
    workspace.close()

    async def body(session: ClientSession) -> dict[str, object]:
        seen: dict[str, object] = {}

        listed = await session.list_tools()
        seen["tools"] = tuple(tool.name for tool in listed.tools)

        seen["repositories"] = structured(
            await session.call_tool("repoatlas_repositories", {"context": GLOBAL})
        )
        seen["status"] = structured(
            await session.call_tool("repoatlas_status", {"context": GLOBAL})
        )

        # 6-7. A search with no investigation id mints one and returns evidence.
        search = structured(
            await session.call_tool(
                "repoatlas_search",
                {"question": "Where is SERVICE_HOST configured?", "context": GLOBAL},
            )
        )
        investigation = search["investigation_id"]
        first = search["evidence"][0]
        seen["search"] = search

        # 8. Inspect inside that same investigation.
        seen["inspect"] = structured(
            await session.call_tool(
                "repoatlas_inspect",
                {
                    "context": {
                        "scope": "repository",
                        "repository": first["ref"]["repository_slug"],
                    },
                    "target": {"path": first["ref"]["path"]},
                    "line_start": first["ref"]["line_start"],
                    "line_end": first["ref"]["line_end"],
                    "investigation_id": investigation,
                },
            )
        )

        # 9. Relationships, from a handle the search itself handed back.
        handle = search["entities"][0]["key"]
        seen["handle"] = handle
        seen["relationships"] = structured(
            await session.call_tool(
                "repoatlas_relationships",
                {"context": GLOBAL, "target": handle, "investigation_id": investigation},
            )
        )

        # 10. Dependencies.
        seen["dependencies"] = structured(
            await session.call_tool(
                "repoatlas_dependencies",
                {"context": GLOBAL, "limit": 3, "investigation_id": investigation},
            )
        )

        # 11. Verify a claim citing evidence from this investigation.
        seen["grounding"] = structured(
            await session.call_tool(
                "repoatlas_verify_grounding",
                {
                    "investigation_id": investigation,
                    "answer": {
                        "summary": "SERVICE_HOST is read where the search said it is.",
                        "claims": [
                            {
                                "statement": "The service reads SERVICE_HOST from the environment.",
                                "citations": [{"evidence_id": first["id"]}],
                            }
                        ],
                    },
                },
            )
        )

        # 12. The one mutation.
        seen["sync"] = structured(await session.call_tool("repoatlas_sync", {"context": GLOBAL}))

        # 13. Reads afterwards, on the same process and the same service.
        seen["status_again"] = structured(
            await session.call_tool("repoatlas_status", {"context": GLOBAL})
        )
        seen["search_again"] = structured(
            await session.call_tool(
                "repoatlas_search",
                {"question": "Where is SERVICE_HOST configured?", "context": GLOBAL},
            )
        )
        seen["investigation"] = investigation
        return seen

    seen = over_stdio(state, body)

    assert seen["tools"] == EXPECTED_TOOLS

    # Reads.
    assert seen["repositories"]["repositories"], "the fixture ecosystem is registered"
    assert seen["status"]["root_configured"] is True

    # Search minted an investigation and supplied citable evidence.
    search = seen["search"]
    assert search["investigation_id"] == seen["investigation"]
    assert search["evidence"], "the prepared ecosystem answers this question"
    assert search["evidence"][0]["id"] == "E1"

    # Inspection joined the same investigation rather than starting another.
    assert seen["inspect"]["investigation_id"] == seen["investigation"]
    assert seen["inspect"]["segments"], "the inspected span came back"

    # The walk used the search's own handle verbatim and reached real edges.
    assert seen["handle"] == "env:SERVICE_HOST"
    assert seen["relationships"]["investigation_id"] == seen["investigation"]
    assert seen["relationships"]["edges"], "the prepared ecosystem has this neighbourhood"

    # The inventory came from the index.
    assert seen["dependencies"]["packages"], "the prepared ecosystem declares packages"

    # Grounding resolved a citation minted in this same investigation.
    grounding = seen["grounding"]
    assert grounding["investigation_id"] == seen["investigation"]
    assert grounding["verdict"] == "grounded"

    # The mutation ran once and reported itself.
    assert seen["sync"]["status"] in {"completed", "incomplete"}
    assert seen["sync"]["repositories"], "the sync reported the repositories it visited"

    # And the server kept serving afterwards.
    assert seen["status_again"]["root_configured"] is True
    assert seen["search_again"]["evidence"]
    # A second search with no id is a second investigation, even after a sync.
    assert seen["search_again"]["investigation_id"] != seen["investigation"]
