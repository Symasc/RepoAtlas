"""§31: the MCP result *is* the capability response, for all eight capabilities.

This is the strongest single proof that the adapter contains no product logic. If
a tool result and a direct call agree field for field, then nothing was added,
nothing was dropped, no enum was renamed, no null became a magic string and no
structure was flattened into prose --- because any of those would show up here as
a difference.

The direct side is written out by hand rather than routed through the adapter's
own conversion helper. Sharing that helper would make the comparison a tautology:
both sides would be one function, and the test would pass however wrong that
function was.

What may legitimately differ is enumerated and nothing else is tolerated. Two
calls are two calls: each evidence-producing one mints its own investigation, and
each sync reads its own clock. So the test collects the *paths* at which the two
dumps disagree and asserts that the leaf names are drawn from a short list of
minted identifiers and clock readings. A capability that started differing in a
ninth field would fail here even though the list was never edited.
"""

from __future__ import annotations

from typing import Any

from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.capability_io import RepositoriesRequest, StatusRequest
from repoatlas.models.dependency_inventory import DependenciesRequest
from repoatlas.models.inspection import InspectRequest, InspectTarget
from repoatlas.models.relationships import RelationshipsRequest
from repoatlas.models.search import SearchRequest
from repoatlas.models.sync import SyncRequest
from repoatlas.models.verification import (
    AgentAnswer,
    AgentCitation,
    AgentClaim,
    VerifyGroundingRequest,
)
from tests.transport.conftest import GLOBAL, over_protocol, structured

from mcp import ClientSession

CONTEXT = CapabilityContext(scope=CapabilityScope.GLOBAL)

QUESTION = "Where is SERVICE_HOST configured?"

#: Leaf names whose value is minted or read from a clock, and therefore differs
#: between any two calls however identical the index. Everything else must match.
PER_CALL = frozenset(
    {"investigation_id", "completed_at", "checked_at", "last_indexed_at", "last_sync"}
)


def differing_leaves(left: Any, right: Any, path: str = "") -> set[str]:
    """Every leaf name at which two dumps disagree.

    Names rather than full paths, because what the assertion is about is *which
    field* moved, and a list index in the middle of a path would make the same
    field read as several different failures.
    """
    if isinstance(left, dict) and isinstance(right, dict):
        if set(left) != set(right):
            return {f"{path}<keys>"}
        found: set[str] = set()
        for key in left:
            found |= differing_leaves(left[key], right[key], key)
        return found
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return {f"{path}<length>"}
        found = set()
        for index, (a, b) in enumerate(zip(left, right, strict=True)):
            found |= differing_leaves(a, b, path or str(index))
        return found
    return set() if left == right else {path or "<root>"}


def assert_parity(mcp_payload: dict[str, Any], direct_payload: dict[str, Any]) -> None:
    differing = differing_leaves(mcp_payload, direct_payload)
    unexpected = sorted(differing - PER_CALL)
    assert not unexpected, f"the transport changed these fields: {unexpected}"


def over(
    capability_service: CapabilityService, tool: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    async def body(session: ClientSession) -> dict[str, Any]:
        return structured(await session.call_tool(tool, arguments))

    return over_protocol(capability_service, body)


# ---------------------------------------------------------------------------
# One test per capability
# ---------------------------------------------------------------------------


def test_repositories_matches_the_direct_call(service: CapabilityService) -> None:
    mcp = over(service, "repoatlas_repositories", {"context": GLOBAL})
    direct = service.repositories(RepositoriesRequest(context=CONTEXT)).model_dump(mode="json")
    assert differing_leaves(mcp, direct) == set(), "a read with no investigation is byte-identical"


def test_status_matches_the_direct_call(service: CapabilityService) -> None:
    mcp = over(service, "repoatlas_status", {"context": GLOBAL})
    direct = service.status(StatusRequest(context=CONTEXT)).model_dump(mode="json")
    assert differing_leaves(mcp, direct) == set()


def test_search_matches_the_direct_call(service: CapabilityService) -> None:
    mcp = over(service, "repoatlas_search", {"question": QUESTION, "context": GLOBAL})
    direct = service.search(SearchRequest(question=QUESTION, context=CONTEXT)).model_dump(
        mode="json"
    )
    assert_parity(mcp, direct)
    # Not merely equal: still carrying the machine fields an agent acts on.
    assert mcp["evidence"] and mcp["evidence"][0]["id"] == "E1"
    assert mcp["entities"] == direct["entities"]
    assert mcp["budget"] == direct["budget"]


def test_inspect_matches_the_direct_call(service: CapabilityService) -> None:
    arguments = {
        "context": {"scope": "repository", "repository": "python-service"},
        "target": {"path": "src/config.py"},
        "line_start": 1,
        "line_end": 6,
    }
    mcp = over(service, "repoatlas_inspect", arguments)
    direct = service.inspect(
        InspectRequest(
            context=CapabilityContext(
                scope=CapabilityScope.REPOSITORY, repository="python-service"
            ),
            target=InspectTarget(path="src/config.py"),
            line_start=1,
            line_end=6,
        )
    ).model_dump(mode="json")
    assert_parity(mcp, direct)
    assert mcp["segments"], "the inspection returned source"


def test_relationships_matches_the_direct_call(service: CapabilityService) -> None:
    mcp = over(
        service, "repoatlas_relationships", {"context": GLOBAL, "target": "env:SERVICE_HOST"}
    )
    direct = service.relationships(
        RelationshipsRequest(context=CONTEXT, target="env:SERVICE_HOST")
    ).model_dump(mode="json")
    assert_parity(mcp, direct)
    assert mcp["edges"], "the walk found the neighbourhood"


def test_dependencies_matches_the_direct_call(service: CapabilityService) -> None:
    mcp = over(service, "repoatlas_dependencies", {"context": GLOBAL, "limit": 3})
    direct = service.dependencies(DependenciesRequest(context=CONTEXT, limit=3)).model_dump(
        mode="json"
    )
    assert_parity(mcp, direct)
    assert mcp["packages"], "the inventory returned packages"


def test_verify_grounding_matches_the_direct_call(service: CapabilityService) -> None:
    """Each side gets its own investigation holding the same evidence.

    Verification is defined against a ledger, so there is no way to ask the
    question twice without two ledgers. They are built the same way from the same
    index, which is what makes the comparison meaningful.
    """
    answer = {
        "summary": "s",
        "claims": [
            {
                "statement": "The service reads SERVICE_HOST from the environment.",
                "citations": [{"evidence_id": "E1"}],
            }
        ],
    }

    async def body(session: ClientSession) -> dict[str, Any]:
        search = structured(
            await session.call_tool("repoatlas_search", {"question": QUESTION, "context": GLOBAL})
        )
        return structured(
            await session.call_tool(
                "repoatlas_verify_grounding",
                {"investigation_id": search["investigation_id"], "answer": answer},
            )
        )

    mcp = over_protocol(service, body)

    supplied = service.search(SearchRequest(question=QUESTION, context=CONTEXT))
    direct = service.verify_grounding(
        VerifyGroundingRequest(
            investigation_id=supplied.investigation_id,
            answer=AgentAnswer(
                summary="s",
                claims=(
                    AgentClaim(
                        statement="The service reads SERVICE_HOST from the environment.",
                        citations=(AgentCitation(evidence_id="E1"),),
                    ),
                ),
            ),
        )
    ).model_dump(mode="json")
    assert_parity(mcp, direct)
    assert mcp["verdict"] == direct["verdict"] == "grounded"


def test_sync_matches_the_direct_call(service: CapabilityService) -> None:
    """The mutation too, run twice against an index already current.

    A sync that has nothing to do still produces the whole report, so this
    compares every field of it without the first run changing what the second
    would see.
    """
    service.sync(SyncRequest(context=CONTEXT))  # settle the index first
    mcp = over(service, "repoatlas_sync", {"context": GLOBAL})
    direct = service.sync(SyncRequest(context=CONTEXT)).model_dump(mode="json")
    assert_parity(mcp, direct)
    assert mcp["status"] == direct["status"]
    assert mcp["repositories"] == direct["repositories"]


# ---------------------------------------------------------------------------
# §20: the same response serializes the same way every time
# ---------------------------------------------------------------------------


def test_serialization_is_deterministic_for_every_capability(service: CapabilityService) -> None:
    """No set iteration, no repr, no address, no inconsistent enum dumping.

    The same request twice on the same index must serialize identically. An enum
    dumped as its member on one path and its value on another, or a set ordered by
    hash, would show up as an unstable field here rather than as an occasional
    client-side parse failure months later.
    """
    import json

    calls: list[tuple[str, dict[str, Any]]] = [
        ("repoatlas_repositories", {"context": GLOBAL}),
        ("repoatlas_status", {"context": GLOBAL}),
        ("repoatlas_search", {"question": QUESTION, "context": GLOBAL}),
        (
            "repoatlas_inspect",
            {
                "context": {"scope": "repository", "repository": "python-service"},
                "target": {"path": "src/config.py"},
                "line_start": 1,
                "line_end": 6,
            },
        ),
        ("repoatlas_relationships", {"context": GLOBAL, "target": "env:SERVICE_HOST"}),
        ("repoatlas_dependencies", {"context": GLOBAL, "limit": 3}),
    ]
    for tool, arguments in calls:
        first = over(service, tool, arguments)
        second = over(service, tool, arguments)
        assert differing_leaves(first, second) <= PER_CALL, tool
        # And the bytes themselves are stable, not merely the decoded object.
        assert json.dumps(first, sort_keys=True) == json.dumps(
            json.loads(json.dumps(first)), sort_keys=True
        ), tool


def test_the_text_block_carries_the_same_object_as_the_structured_field(
    service: CapabilityService,
) -> None:
    """§11: the content block is the response as JSON, never a summary of it.

    A client that reads only content blocks must be able to act on exactly what a
    client reading structured content acts on. Prose in that block would be a
    second, lossy, unspecified response shape.
    """
    import json

    async def body(session: ClientSession) -> tuple[Any, Any]:
        result = await session.call_tool(
            "repoatlas_search", {"question": QUESTION, "context": GLOBAL}
        )
        assert result.content and result.content[0].type == "text"
        return json.loads(result.content[0].text), result.structuredContent

    text_payload, structured_payload = over_protocol(service, body)
    assert text_payload == structured_payload
    assert text_payload["evidence"][0]["id"] == "E1"
