"""The deterministic MCP adapter evaluation (V2 Slice J, §38).

A sixth corpus, and the only one that measures a *transport* rather than a kind of
knowledge. The other five ask whether RepoAtlas found the right thing; this one
asks whether what it found survived the trip, and whether the surface a client
discovers is the one that was intended.

It deliberately does not re-run retrieval, graph, dependency or grounding
questions through the protocol. Those corpora exist, the adapter is proved
elsewhere to return their answers field for field, and copying them through here
would multiply runtime while measuring the capabilities a second time instead of
the adapter once. What is measured is one representative call per capability plus
the adapter's own contract: inventory, schemas, investigation lifecycle, error
codes, the absence of resources and prompts, and a protocol stream with nothing
in it but protocol.

Ground truth lives in ``mcp_adapter_cases.yaml`` and was written by reasoning
about the capability contracts. No model is consulted and nothing is embedded.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import anyio
import pytest
import yaml
from repoatlas.capabilities.service import CapabilityService
from repoatlas.mcp.tools import TOOLS
from tests.evaluation.harness import build, evaluation_workspace
from tests.transport.conftest import (
    INITIALIZE,
    INITIALIZED,
    over_protocol,
    over_stdio,
    protocol_messages,
    raw_exchange,
    refusal,
    requires_console_script,
    structured,
    tool_call,
)

from mcp import ClientSession

CASES_PATH = Path(__file__).parent / "mcp_adapter_cases.yaml"

#: Exactly the capabilities §1 names, in listing order.
EXPECTED_INVENTORY = (
    "repoatlas_repositories",
    "repoatlas_status",
    "repoatlas_search",
    "repoatlas_inspect",
    "repoatlas_relationships",
    "repoatlas_dependencies",
    "repoatlas_http_operations",
    "repoatlas_verify_grounding",
    "repoatlas_sync",
)

QUESTION = "Where is SERVICE_HOST configured?"


@dataclasses.dataclass(frozen=True, slots=True)
class AdapterCase:
    id: str
    tool: str
    arguments: dict[str, Any]
    expect: dict[str, Any]
    notes: str


def _load(section: str) -> tuple[AdapterCase, ...]:
    raw: dict[str, Any] = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))
    cases = tuple(
        AdapterCase(
            id=entry["id"],
            tool=entry["tool"],
            arguments=dict(entry.get("arguments") or {}),
            expect=dict(entry["expect"]),
            notes=entry.get("notes", ""),
        )
        for entry in raw[section]
    )
    if len({case.id for case in cases}) != len(cases):
        raise AssertionError(f"{section} case ids must be unique")
    return cases


CAPABILITY_CASES = _load("capabilities")
ERROR_CASES = _load("errors")


@pytest.fixture
def adapter_service() -> Any:
    """One capability service over a freshly indexed ecosystem."""
    with evaluation_workspace() as (workspace, root):
        service = CapabilityService(workspace)
        yield service, root


# ---------------------------------------------------------------------------
# Inventory and discovery
# ---------------------------------------------------------------------------


def test_the_tool_inventory_is_exactly_the_completed_capabilities(adapter_service: Any) -> None:
    service, _ = adapter_service

    async def body(session: ClientSession) -> tuple[str, ...]:
        listed = await session.list_tools()
        return tuple(tool.name for tool in listed.tools)

    assert over_protocol(service, body) == EXPECTED_INVENTORY


def test_the_advertised_schemas_are_the_request_models(adapter_service: Any) -> None:
    service, _ = adapter_service

    async def body(session: ClientSession) -> dict[str, Any]:
        listed = await session.list_tools()
        return {tool.name: tool.inputSchema for tool in listed.tools}

    advertised = over_protocol(service, body)
    for spec in TOOLS:
        assert advertised[spec.name] == spec.request.model_json_schema(), spec.name


# ---------------------------------------------------------------------------
# One representative call per capability
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", CAPABILITY_CASES, ids=[case.id for case in CAPABILITY_CASES])
def test_each_capability_answers_over_the_protocol(case: AdapterCase, adapter_service: Any) -> None:
    """The happy path, against ground truth about the shape of the answer.

    The fields listed in the corpus are the ones a client acts on. Their presence
    is asserted rather than their values, because the values belong to the
    capability corpora --- but a transport that dropped ``evidence`` or
    ``investigation_id`` would be caught here, and that is an adapter defect.
    """
    service, _ = adapter_service
    supplied: dict[str, Any] = {}

    async def body(session: ClientSession) -> dict[str, Any]:
        arguments = dict(case.arguments)
        if case.tool == "repoatlas_verify_grounding":
            # Verification needs a ledger, so one is built the way a client builds
            # one: by searching first and echoing the id back.
            search = structured(
                await session.call_tool(
                    "repoatlas_search",
                    {"question": QUESTION, "context": {"scope": "global"}},
                )
            )
            supplied["investigation_id"] = search["investigation_id"]
            arguments = {
                "investigation_id": search["investigation_id"],
                "answer": {
                    "summary": "s",
                    "claims": [
                        {
                            "statement": "The service reads SERVICE_HOST.",
                            "citations": [{"evidence_id": search["evidence"][0]["id"]}],
                        }
                    ],
                },
            }
        return structured(await session.call_tool(case.tool, arguments))

    payload = over_protocol(service, body)

    for field in case.expect["fields"]:
        assert field in payload, f"{case.id}: {field} is missing — {case.notes}"
    for field in case.expect.get("non_empty", ()):
        assert payload[field], f"{case.id}: {field} is empty — {case.notes}"

    lifecycle = case.expect["investigation"]
    if lifecycle == "none":
        assert "investigation_id" not in payload, case.notes
    elif lifecycle == "minted":
        assert payload["investigation_id"], case.notes
    else:
        assert payload["investigation_id"] == supplied["investigation_id"], case.notes


@pytest.mark.parametrize("case", ERROR_CASES, ids=[case.id for case in ERROR_CASES])
def test_each_refusal_keeps_its_machine_meaning(case: AdapterCase, adapter_service: Any) -> None:
    """A refusal a client can branch on, not a sentence it has to parse."""
    service, _ = adapter_service

    async def body(session: ClientSession) -> Any:
        return await session.call_tool(case.tool, case.arguments)

    error = refusal(over_protocol(service, body))
    assert error["code"] == case.expect["code"], case.notes
    assert error["category"] == case.expect["category"], case.notes
    assert error["retryable"] is case.expect["retryable"], case.notes
    assert error["message"], "a refusal says something to a human too"


def test_every_refusal_in_the_corpus_carries_a_distinct_code(adapter_service: Any) -> None:
    """§37 mutation O, measured: the codes do not collapse into one.

    A corpus that only asserted "this failed" would pass against an adapter that
    reported every condition as an internal error.
    """
    service, _ = adapter_service
    codes: list[str] = []
    for case in ERROR_CASES:

        async def body(session: ClientSession, case: AdapterCase = case) -> Any:
            return await session.call_tool(case.tool, case.arguments)

        codes.append(refusal(over_protocol(service, body))["code"])
    assert len(set(codes)) == len(ERROR_CASES), codes


# ---------------------------------------------------------------------------
# Output parity, measured once per capability
# ---------------------------------------------------------------------------


def test_the_structured_result_is_the_capability_response(adapter_service: Any) -> None:
    """The structured field and the text block hold one object, and it is V2's.

    Asserted against a direct call for the two capabilities that carry the most
    machine structure. The exhaustive field-for-field comparison across all eight
    lives in the transport suite; what this corpus records is that the property
    holds at all, so an adapter evaluation read on its own still shows it.
    """
    from repoatlas.models.capability import CapabilityContext, CapabilityScope
    from repoatlas.models.capability_io import RepositoriesRequest
    from repoatlas.models.relationships import RelationshipsRequest

    service, _ = adapter_service
    context = CapabilityContext(scope=CapabilityScope.GLOBAL)

    async def body(session: ClientSession) -> tuple[Any, Any]:
        listing = await session.call_tool(
            "repoatlas_repositories", {"context": {"scope": "global"}}
        )
        walk = await session.call_tool(
            "repoatlas_relationships",
            {"context": {"scope": "global"}, "target": "env:SERVICE_HOST"},
        )
        return listing, walk

    listing, walk = over_protocol(service, body)

    direct_listing = service.repositories(RepositoriesRequest(context=context)).model_dump(
        mode="json"
    )
    assert listing.structuredContent == direct_listing
    assert json.loads(listing.content[0].text) == direct_listing

    direct_walk = service.relationships(
        RelationshipsRequest(context=context, target="env:SERVICE_HOST")
    ).model_dump(mode="json")
    walk_payload = dict(walk.structuredContent or {})
    assert walk_payload["edges"] == direct_walk["edges"]
    assert walk_payload["target"] == direct_walk["target"]


# ---------------------------------------------------------------------------
# Investigation lifecycle
# ---------------------------------------------------------------------------


def test_two_investigations_in_one_process_stay_apart(adapter_service: Any) -> None:
    service, _ = adapter_service

    async def body(session: ClientSession) -> tuple[str, str]:
        first = structured(
            await session.call_tool(
                "repoatlas_search", {"question": QUESTION, "context": {"scope": "global"}}
            )
        )
        second = structured(
            await session.call_tool(
                "repoatlas_search",
                {"question": "Which ruby gems are declared?", "context": {"scope": "global"}},
            )
        )
        return first["investigation_id"], second["investigation_id"]

    left, right = over_protocol(service, body)
    assert left != right
    assert len(service.sessions) == 2


@requires_console_script
def test_a_restart_loses_investigations_and_keeps_the_index(tmp_path: Path) -> None:
    from tests.adversarial.conftest import ADVERSARIAL_ROOT

    state = tmp_path / "atlas"
    workspace, _ = build(ADVERSARIAL_ROOT, state)
    workspace.close()

    async def mint(session: ClientSession) -> str:
        return structured(
            await session.call_tool(
                "repoatlas_search", {"question": QUESTION, "context": {"scope": "global"}}
            )
        )["investigation_id"]

    investigation = over_stdio(state, mint)

    async def reuse(session: ClientSession) -> Any:
        return await session.call_tool(
            "repoatlas_verify_grounding",
            {
                "investigation_id": investigation,
                "answer": {
                    "summary": "s",
                    "claims": [{"statement": "x", "citations": [{"evidence_id": "E1"}]}],
                },
            },
        )

    assert refusal(over_stdio(state, reuse))["code"] == "UNKNOWN_INVESTIGATION"

    # The index, by contrast, is exactly where it was.
    async def read(session: ClientSession) -> dict[str, Any]:
        return structured(
            await session.call_tool("repoatlas_repositories", {"context": {"scope": "global"}})
        )

    assert over_stdio(state, read)["repositories"]


# ---------------------------------------------------------------------------
# What the protocol must not offer
# ---------------------------------------------------------------------------


@requires_console_script
def test_no_resource_or_prompt_surface_exists(tmp_path: Path) -> None:
    """Repository bytes have one door, and it enforces admission and provenance."""
    from tests.adversarial.conftest import ADVERSARIAL_ROOT

    state = tmp_path / "atlas"
    workspace, _ = build(ADVERSARIAL_ROOT, state)
    workspace.close()

    async def body(session: ClientSession) -> list[str]:
        outcomes: list[str] = []
        for attempt in (session.list_resources, session.list_prompts):
            try:
                await attempt()
            except Exception:
                outcomes.append("refused")
            else:  # pragma: no cover - would be a real defect
                outcomes.append("answered")
        return outcomes

    assert over_stdio(state, body) == ["refused", "refused"]


@requires_console_script
def test_the_protocol_stream_carries_nothing_but_protocol(tmp_path: Path) -> None:
    """Including across the mutation, which is the path that invokes git."""
    from tests.adversarial.conftest import ADVERSARIAL_ROOT

    state = tmp_path / "atlas"
    workspace, _ = build(ADVERSARIAL_ROOT, state)
    workspace.close()

    code, out, _ = raw_exchange(
        state,
        [
            INITIALIZE,
            INITIALIZED,
            tool_call(
                2, "repoatlas_search", {"question": QUESTION, "context": {"scope": "global"}}
            ),
            tool_call(3, "repoatlas_sync", {"context": {"scope": "global"}}),
        ],
    )
    messages = protocol_messages(out)
    assert code == 0
    assert [message.get("id") for message in messages] == [1, 2, 3]


# ---------------------------------------------------------------------------
# Corpus hygiene
# ---------------------------------------------------------------------------


def test_the_corpus_covers_every_exposed_capability() -> None:
    assert {case.tool for case in CAPABILITY_CASES} == set(EXPECTED_INVENTORY)


def test_the_corpus_imports_no_model_or_embedding_dependency() -> None:
    """Checked on imports, not text — the assertion itself has to name what it forbids."""
    import ast

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any(name.startswith("repoatlas.llm") for name in imported), imported
    assert not any(name.split(".")[0] == "fastembed" for name in imported), imported
    assert anyio is not None
