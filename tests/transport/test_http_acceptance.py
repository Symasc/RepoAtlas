"""The HTTP linkage workflow through the real MCP server (brief §81).

Everything the feature adds is reached through the eight existing tools and
nothing else. An agent starts a real ``repoatlas mcp`` process and runs the flow:
1. search for an operation;
2. take the exact handle search returns;
3. walk that node;
4. see the caller and the declarers;
5. inspect the cited source;
6. verify a claim that cites it.

No tool was added for any of it, no resource or prompt appeared, and the process
never had a reason to open a socket.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from repoatlas.services.workspace import Workspace
from tests.http_corpus import index_corpus
from tests.transport.conftest import (
    GLOBAL,
    over_stdio,
    requires_console_script,
    structured,
)

from mcp import ClientSession

pytestmark = requires_console_script

ENDPOINT = "http-endpoint:GET:https://api.example.com/hello"


@pytest.fixture
def corpus_state(tmp_path: Path) -> Iterator[Path]:
    """The HTTP corpus indexed on disk, laid out the way a server process reads it."""
    workspace: Workspace = index_corpus(tmp_path)
    workspace.close()
    yield tmp_path


def test_an_agent_walks_from_search_to_a_grounded_claim(corpus_state: Path) -> None:
    async def body(session: ClientSession) -> dict[str, Any]:
        tools = await session.list_tools()
        searched = structured(
            await session.call_tool(
                "repoatlas_search",
                {"question": "GET https://api.example.com/hello", "context": GLOBAL},
            )
        )
        investigation = searched["investigation_id"]
        handle = next(h["key"] for h in searched["entities"] if h["type"] == "http_endpoint")
        related = structured(
            await session.call_tool(
                "repoatlas_relationships",
                {"context": GLOBAL, "target": handle, "investigation_id": investigation},
            )
        )
        call = next(edge for edge in related["edges"] if edge["source"]["repository_slug"] == "web")
        evidence_id = call["evidence_ids"][0]
        inspected = structured(
            await session.call_tool(
                "repoatlas_inspect",
                {
                    "context": GLOBAL,
                    "target": {"evidence_id": evidence_id},
                    "investigation_id": investigation,
                },
            )
        )
        verified = structured(
            await session.call_tool(
                "repoatlas_verify_grounding",
                {
                    "investigation_id": investigation,
                    "answer": {
                        "claims": [
                            {
                                "statement": (
                                    "web calls GET https://api.example.com/hello, which "
                                    "api-a's OpenAPI declaration also exposes."
                                ),
                                "citations": [{"evidence_id": evidence_id}],
                            }
                        ]
                    },
                },
            )
        )
        refused: list[str] = []
        for attempt in (session.list_resources, session.list_prompts):
            try:
                await attempt()
            except Exception as exc:  # the refusal is the assertion
                refused.append(type(exc).__name__)
        return {
            "tools": sorted(tool.name for tool in tools.tools),
            "handle": handle,
            "related": related,
            "inspected": inspected,
            "verified": verified,
            "refused": refused,
        }

    outcome = over_stdio(corpus_state, body)

    assert len(outcome["tools"]) == 8
    assert outcome["handle"] == ENDPOINT
    sides = {
        (edge["source"]["repository_slug"], edge["type"]) for edge in outcome["related"]["edges"]
    }
    assert sides == {
        ("api-a", "EXPOSES_HTTP_ENDPOINT"),
        ("web", "CALLS_HTTP_ENDPOINT"),
        ("pyclient", "CALLS_HTTP_ENDPOINT"),
    }
    # The declaration is composed from two lines, so it is a fact without a citation.
    declaration = next(
        edge for edge in outcome["related"]["edges"] if edge["source"]["repository_slug"] == "api-a"
    )
    assert declaration["evidence_ids"] == []
    assert declaration["provenance"]["citable"] is False
    assert any(
        "https://api.example.com/hello" in segment["text"]
        for segment in outcome["inspected"]["segments"]
    )
    assert all(citation["grounded"] for citation in outcome["verified"]["citations"])
    assert len(outcome["refused"]) == 2
