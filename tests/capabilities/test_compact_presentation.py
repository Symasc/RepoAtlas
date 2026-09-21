"""``presentation: compact``: the same result, each repeated fact written once.

On LVL a twelve-item search carried about 5 KB of evidence text and 10 KB of
metadata about it: every item repeated a 296-character facts version, a file hash
and a span digest, and a grounding verdict echoed its answer twice beside the
freshness of repositories nothing cited. Compact is a projection of the finished
response, so the tests here hold it to two things at once:

* nothing an agent acts on is lost — ids, locations, text, match strength, the
  declaration flag, entity keys, statuses, and freshness of what was drawn on;
* nothing the capability *does* changes — the same ids ground the same way, stale
  evidence is still reported stale, and ``full`` is byte-for-byte the response.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from repoatlas.capabilities.presentation import present
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import CapabilityContext, CapabilityScope, Presentation
from repoatlas.models.inspection import InspectRequest, InspectTarget
from repoatlas.models.investigation import EvidenceStatus
from repoatlas.models.relationships import RelationshipsRequest
from repoatlas.models.search import SearchRequest
from repoatlas.models.sync import SyncRequest
from repoatlas.models.verification import (
    AgentAnswer,
    AgentCitation,
    AgentClaim,
    GroundingVerdict,
    VerifyGroundingRequest,
)
from repoatlas.services.workspace import Workspace

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)
SERVICE = CapabilityContext(scope=CapabilityScope.REPOSITORY, repository="python-service")
QUESTION = "Where is SERVICE_HOST configured?"
COMPACT = Presentation.COMPACT


def _bytes(payload: object) -> int:
    return len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode())


def _search(service: CapabilityService, **kwargs: object) -> tuple[SearchRequest, dict]:
    request = SearchRequest(question=QUESTION, context=GLOBAL, **kwargs)  # type: ignore[arg-type]
    return request, present(request, service.search(request))


def test_full_is_the_response_exactly(initialized: Workspace) -> None:
    service = CapabilityService(initialized)
    request = SearchRequest(question=QUESTION, context=GLOBAL)
    response = service.search(request)
    assert present(request, response) == response.model_dump(mode="json")
    assert request.presentation is Presentation.FULL


def test_compact_search_keeps_everything_an_agent_acts_on(initialized: Workspace) -> None:
    service = CapabilityService(initialized)
    full_request = SearchRequest(question=QUESTION, context=GLOBAL)
    full = service.search(full_request)
    _, compact = _search(service, presentation=COMPACT, investigation_id=full.investigation_id)

    assert compact["presentation"] == "compact"
    assert len(compact["evidence"]) == len(full.evidence)
    for mine, theirs in zip(compact["evidence"], full.evidence, strict=True):
        assert mine["location"] == theirs.location
        assert mine["text"] == theirs.text
        assert mine["tier"] == int(theirs.tier) and mine["match"] == int(theirs.match)
        assert mine["declaration"] == theirs.declaration
        assert mine["why"] == theirs.why
        assert mine.get("entity_key") == (theirs.entity.key if theirs.entity else None)
        assert mine["status"] == theirs.status.value
        # Provenance the ledger holds is not repeated per item.
        assert "ref" not in mine
    assert compact["entities"] == [handle.key for handle in full.entities]
    assert compact["truncated"] == full.truncated


def test_each_repeated_fact_is_written_once(initialized: Workspace) -> None:
    service = CapabilityService(initialized)
    full_request = SearchRequest(question=QUESTION, context=GLOBAL)
    full = service.search(full_request)
    _, compact = _search(service, presentation=COMPACT)

    facts = {item.ref.indexed_facts_version for item in full.evidence}
    assert compact["facts_version"] in facts
    text = json.dumps(compact)
    assert text.count(compact["facts_version"]) == 1

    files = {
        (item.ref.repository_slug, item.ref.path): item.ref.file_content_hash
        for item in full.evidence
    }
    assert {
        (source["repository_slug"], source["path"]): source["content_hash"]
        for source in compact["sources"]
    } == files
    for item in full.evidence:
        assert item.ref.span_digest not in text

    assert _bytes(compact) < _bytes(full.model_dump(mode="json"))


def test_compact_ids_ground_exactly_as_full_ids_do(initialized: Workspace) -> None:
    service = CapabilityService(initialized)
    _, compact = _search(service, presentation=COMPACT)
    lined = next(item for item in compact["evidence"] if ":" in item["location"].rsplit("/", 1)[-1])
    answer = AgentAnswer(
        summary="SERVICE_HOST",
        claims=(
            AgentClaim(
                statement="SERVICE_HOST is configured here.",
                citations=(AgentCitation(evidence_id=lined["id"]),),
            ),
        ),
    )
    full_verdict = service.verify_grounding(
        VerifyGroundingRequest(investigation_id=compact["investigation_id"], answer=answer)
    )
    compact_request = VerifyGroundingRequest(
        investigation_id=compact["investigation_id"], answer=answer, presentation=COMPACT
    )
    compact_verdict = present(compact_request, service.verify_grounding(compact_request))

    assert full_verdict.verdict is GroundingVerdict.GROUNDED
    assert compact_verdict["verdict"] == full_verdict.verdict.value
    assert compact_verdict["citations"] == [
        citation.model_dump(mode="json", exclude_none=True) for citation in full_verdict.citations
    ]
    # The answer the caller sent is not echoed; nothing was removed from it.
    assert "original_answer" not in compact_verdict
    assert compact_verdict["answer_changed"] is False
    assert "sanitized_answer" not in compact_verdict


def test_compact_grounding_reports_a_changed_answer_and_only_cited_repositories(
    initialized: Workspace,
) -> None:
    service = CapabilityService(initialized)
    _, compact = _search(service, presentation=COMPACT)
    cited = compact["evidence"][0]
    answer = AgentAnswer(
        summary="s",
        claims=(
            AgentClaim(statement="Real.", citations=(AgentCitation(evidence_id=cited["id"]),)),
            AgentClaim(statement="Invented.", citations=(AgentCitation(evidence_id="E999"),)),
        ),
    )
    request = VerifyGroundingRequest(
        investigation_id=compact["investigation_id"], answer=answer, presentation=COMPACT
    )
    full = service.verify_grounding(request)
    verdict = present(request, full)

    assert full.sanitized_answer != full.original_answer
    assert verdict["answer_changed"] is True
    assert verdict["sanitized_answer"] == full.sanitized_answer.model_dump(
        mode="json", exclude_none=True
    )
    # The full response describes every registered repository; the compact one
    # describes the repository the cited evidence came from.
    assert len(full.freshness.repositories) > 1
    slug = cited["location"].split("/", 1)[0]
    assert [repo["repository_slug"] for repo in verdict["freshness"]["repositories"]] == [slug]


def test_stale_evidence_is_still_reported_stale(
    initialized: Workspace, workspace_root: Path
) -> None:
    service = CapabilityService(initialized)
    inspect_request = InspectRequest(
        context=SERVICE, target=InspectTarget(path="src/config.py"), presentation=COMPACT
    )
    inspected = present(inspect_request, service.inspect(inspect_request))
    old = inspected["segments"][0]["id"]
    config = workspace_root / "python-service" / "src" / "config.py"
    config.write_text(config.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    service.sync(SyncRequest(context=GLOBAL))

    request = VerifyGroundingRequest(
        investigation_id=inspected["investigation_id"],
        answer=AgentAnswer(
            summary="s",
            claims=(AgentClaim(statement="x", citations=(AgentCitation(evidence_id=old),)),),
        ),
        presentation=COMPACT,
    )
    verdict = present(request, service.verify_grounding(request))
    assert verdict["evidence_statuses"][0]["status"] == EvidenceStatus.STALE.value
    assert verdict["verdict"] != GroundingVerdict.GROUNDED.value


def test_compact_inspection_writes_the_file_once(initialized: Workspace) -> None:
    service = CapabilityService(initialized)
    request = InspectRequest(
        context=SERVICE, target=InspectTarget(path="src/config.py"), presentation=COMPACT
    )
    full = service.inspect(request)
    compact = present(request, full)
    assert compact["file"] == full.file.model_dump(mode="json", exclude_none=True)
    assert compact["coverage"] == full.coverage.model_dump(mode="json", exclude_none=True)
    assert [segment["text"] for segment in compact["segments"]] == [
        segment.text for segment in full.segments
    ]
    assert all("ref" not in segment for segment in compact["segments"])


def test_compact_relationships_keep_their_edges(initialized: Workspace) -> None:
    service = CapabilityService(initialized)
    request = RelationshipsRequest(context=GLOBAL, target="env:SERVICE_HOST", presentation=COMPACT)
    full = service.relationships(request)
    compact = present(request, full)
    assert compact["edges"] == [
        edge.model_dump(mode="json", exclude_none=True) for edge in full.edges
    ]
    assert [item["id"] for item in compact.get("evidence", [])] == [
        item.id for item in full.evidence
    ]


def test_an_unknown_presentation_is_refused() -> None:
    with pytest.raises(ValidationError):
        SearchRequest(question=QUESTION, context=GLOBAL, presentation="terse")  # type: ignore[arg-type]
