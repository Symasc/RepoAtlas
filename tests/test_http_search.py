"""HTTP nodes through ``search``, and the §81 acceptance workflow in process.

Search gains no tier, match class or tool for HTTP. A question that spells an
operation — ``GET /users``, ``GET https://…/users``, a bare path or URL — yields
the exact keys it names, and those go through the existing exact entity lookup.
A hit is a graph seed, and its handle is published beside the evidence: the
exact string an agent passes to ``relationships`` next.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.evidence import EvidenceTier, MatchClass
from repoatlas.models.inspection import InspectRequest, InspectTarget
from repoatlas.models.parsing import EntityType
from repoatlas.models.search import SearchRequest
from repoatlas.models.verification import (
    AgentAnswer,
    AgentCitation,
    AgentClaim,
    VerifyGroundingRequest,
)
from repoatlas.services.workspace import Workspace
from tests.http_corpus import GLOBAL, index_corpus, relationships


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Workspace]:
    workspace = index_corpus(tmp_path_factory.mktemp("http-search"))
    yield workspace
    workspace.close()


def test_search_hands_back_the_exact_endpoint_handle(corpus: Workspace) -> None:
    service = CapabilityService(corpus)
    response = service.search(
        SearchRequest(question="GET https://api.example.com/hello", context=GLOBAL)
    )
    handles = {(h.key, h.type) for h in response.entities}
    assert ("http-endpoint:GET:https://api.example.com/hello", EntityType.HTTP_ENDPOINT) in handles
    assert all(not h.positional for h in response.entities if h.key.startswith("http-"))


@pytest.mark.parametrize(
    ("question", "handle"),
    [
        ("who calls GET /hello?", "http-route:GET:/hello"),
        ("/api/users/me", "http-route:GET:/api/users/me"),
        ("https://api.example.com/v1/users", "http-endpoint:GET:https://api.example.com/v1/users"),
    ],
)
def test_search_finds_http_nodes_by_exact_key(
    corpus: Workspace, question: str, handle: str
) -> None:
    response = CapabilityService(corpus).search(SearchRequest(question=question, context=GLOBAL))
    assert handle in {h.key for h in response.entities}


def test_search_reaches_both_sides_through_the_route_not_the_text(corpus: Workspace) -> None:
    """No hostname in the question, so no text match can find the callers.

    Before this feature, "who calls GET /hello?" reached them only lexically, as
    associative leads. The exact route key now seeds the graph, and both sides
    arrive as deterministic relationships to the node.
    """
    response = CapabilityService(corpus).search(
        SearchRequest(question="who calls GET /hello?", context=GLOBAL)
    )
    related = {
        (item.ref.repository_slug, item.ref.path)
        for item in response.evidence
        if item.tier is EvidenceTier.DETERMINISTIC and item.match is MatchClass.RELATED
    }
    assert {
        ("web", "src/api.ts"),
        ("pyclient", "client/calls.py"),
        ("api-a", "openapi.yaml"),
    } <= related


def test_the_acceptance_workflow_grounds_a_claim_in_the_cited_call(corpus: Workspace) -> None:
    """search → exact handle → relationships → inspect → verify_grounding (§81)."""
    service = CapabilityService(corpus)
    searched = service.search(
        SearchRequest(question="GET https://api.example.com/hello", context=GLOBAL)
    )
    handle = next(h.key for h in searched.entities if h.type is EntityType.HTTP_ENDPOINT)

    related = relationships(
        corpus, handle, investigation_id=searched.investigation_id, service=service
    )
    call = next(e for e in related.edges if e.source.repository_slug == "web")
    evidence_id = call.evidence_ids[0]

    inspected = service.inspect(
        InspectRequest(
            context=GLOBAL,
            target=InspectTarget(evidence_id=evidence_id),
            investigation_id=searched.investigation_id,
        )
    )
    assert any("https://api.example.com/hello" in item.text for item in inspected.segments)

    verdict = service.verify_grounding(
        VerifyGroundingRequest(
            investigation_id=searched.investigation_id,
            answer=AgentAnswer(
                claims=(
                    AgentClaim(
                        statement=(
                            "web calls GET https://api.example.com/hello, which api-a's "
                            "OpenAPI declaration also exposes."
                        ),
                        citations=(AgentCitation(evidence_id=evidence_id),),
                    ),
                )
            ),
        )
    )
    assert all(citation.grounded for citation in verdict.citations)
