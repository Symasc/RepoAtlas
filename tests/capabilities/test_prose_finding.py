"""Which claim the prose rule removed, reported as a finding with its index.

A claim can be removed for naming a repository object nobody supplied. Until now
that removal was reported only as a count with no index, so an agent could see
that *something* went and had no way to learn which sentence did it — and the
most likely repair, adding another citation, cannot help, because the citations
were never the problem.

The finding is emitted on exactly one population: claims that carried at least
one citation, whose citations **all** resolved, and which V1 removed anyway. That
population is well defined because of what the adapter hands V1 — every surviving
citation written at coordinates already verified against the ledger — so the
prose rule is the only thing left that can have removed such a claim. The first
test here pins that invariant, because the finding is only honest while it holds.

Nothing of the removed sentence is repeated. Printing the fabricated path back
under RepoAtlas's own heading is how the original bug got its credibility.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.inspection import InspectRequest, InspectTarget
from repoatlas.models.verification import (
    AgentAnswer,
    AgentCitation,
    AgentClaim,
    GroundingFinding,
    GroundingFindingCode,
    VerifyGroundingRequest,
    VerifyGroundingResponse,
)
from repoatlas.services.workspace import Workspace
from tests.capabilities.test_http_operations import COURSES, REPOSITORY, _index

CLIENT = "src/client.ts"
UNSUPPLIED = "The credential comes from infra/secrets.yaml."
SUPPORTED = "The client is written in this file."


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Workspace]:
    opened, _ = _index(tmp_path, {COURSES: REPOSITORY})
    try:
        yield opened
    finally:
        opened.close()


def _context() -> CapabilityContext:
    return CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=COURSES)


def _supplied(service: CapabilityService) -> tuple[str, str]:
    response = service.inspect(
        InspectRequest(context=_context(), target=InspectTarget(path=CLIENT))
    )
    assert response.segments
    return response.investigation_id, response.segments[0].id


def _verify(
    service: CapabilityService, investigation: str, *claims: AgentClaim, **answer: object
) -> VerifyGroundingResponse:
    return service.verify_grounding(
        VerifyGroundingRequest(
            investigation_id=investigation,
            answer=AgentAnswer(claims=tuple(claims), **answer),  # type: ignore[arg-type]
        )
    )


def _cited(evidence_id: str, statement: str) -> AgentClaim:
    return AgentClaim(statement=statement, citations=(AgentCitation(evidence_id=evidence_id),))


def _findings(
    response: VerifyGroundingResponse, code: GroundingFindingCode
) -> list[GroundingFinding]:
    return [finding for finding in response.findings if finding.code is code]


def test_a_claim_the_prose_rule_removed_is_named_by_its_index(workspace: Workspace) -> None:
    service = CapabilityService(workspace)
    investigation, evidence_id = _supplied(service)
    response = _verify(
        service,
        investigation,
        _cited(evidence_id, SUPPORTED),
        _cited(evidence_id, UNSUPPLIED),
    )
    found = _findings(response, GroundingFindingCode.PROSE_REFERENCE_NOT_SUPPLIED)
    assert len(found) == 1
    assert found[0].claim_index == 1
    assert [claim.statement for claim in response.sanitized_answer.claims] == [SUPPORTED]


def test_the_finding_rests_on_every_citation_of_that_claim_having_resolved(
    workspace: Workspace,
) -> None:
    """The invariant that makes the finding mean what it says.

    If a citation of the removed claim had *not* resolved, the removal would have
    two possible causes and naming one of them would be a guess.
    """
    service = CapabilityService(workspace)
    investigation, evidence_id = _supplied(service)
    response = _verify(service, investigation, _cited(evidence_id, UNSUPPLIED))
    found = _findings(response, GroundingFindingCode.PROSE_REFERENCE_NOT_SUPPLIED)
    assert len(found) == 1
    indexes = {finding.claim_index for finding in found}
    for verdict in response.citations:
        if verdict.claim_index in indexes:
            assert verdict.status is None or verdict.code is None


def test_no_word_of_the_removed_statement_is_repeated(workspace: Workspace) -> None:
    service = CapabilityService(workspace)
    investigation, evidence_id = _supplied(service)
    response = _verify(service, investigation, _cited(evidence_id, UNSUPPLIED))
    found = _findings(response, GroundingFindingCode.PROSE_REFERENCE_NOT_SUPPLIED)
    assert "infra/secrets.yaml" not in found[0].detail
    assert all("infra/secrets.yaml" not in f.detail for f in response.findings)


def test_a_claim_with_no_citation_keeps_its_own_finding_and_gains_no_second_one(
    workspace: Workspace,
) -> None:
    """A claim with nothing to check is a different failure, already reported."""
    service = CapabilityService(workspace)
    investigation, _ = _supplied(service)
    response = _verify(service, investigation, AgentClaim(statement=UNSUPPLIED))
    assert len(_findings(response, GroundingFindingCode.MISSING_CITATION)) == 1
    assert not [
        finding
        for finding in _findings(response, GroundingFindingCode.PROSE_REFERENCE_NOT_SUPPLIED)
        if finding.claim_index is not None
    ]


def test_a_claim_whose_citation_did_not_resolve_gains_no_second_finding(
    workspace: Workspace,
) -> None:
    service = CapabilityService(workspace)
    investigation, _ = _supplied(service)
    response = _verify(service, investigation, _cited("E999", UNSUPPLIED))
    assert len(_findings(response, GroundingFindingCode.UNKNOWN_EVIDENCE)) == 1
    assert not [
        finding
        for finding in _findings(response, GroundingFindingCode.PROSE_REFERENCE_NOT_SUPPLIED)
        if finding.claim_index is not None
    ]


def test_a_claim_that_survived_is_not_reported(workspace: Workspace) -> None:
    service = CapabilityService(workspace)
    investigation, evidence_id = _supplied(service)
    response = _verify(service, investigation, _cited(evidence_id, SUPPORTED))
    assert response.findings == ()
    assert len(response.sanitized_answer.claims) == 1


def test_free_text_removals_are_still_counted_without_an_index(workspace: Workspace) -> None:
    """The non-claim population is unchanged: the summary has no claim index."""
    service = CapabilityService(workspace)
    investigation, evidence_id = _supplied(service)
    response = _verify(
        service,
        investigation,
        _cited(evidence_id, SUPPORTED),
        summary=UNSUPPLIED,
    )
    found = _findings(response, GroundingFindingCode.PROSE_REFERENCE_NOT_SUPPLIED)
    assert [finding.claim_index for finding in found] == [None]
    assert UNSUPPLIED not in response.sanitized_answer.summary


def test_a_claim_and_the_summary_are_reported_separately(workspace: Workspace) -> None:
    service = CapabilityService(workspace)
    investigation, evidence_id = _supplied(service)
    response = _verify(
        service,
        investigation,
        _cited(evidence_id, UNSUPPLIED),
        summary=UNSUPPLIED,
    )
    found = _findings(response, GroundingFindingCode.PROSE_REFERENCE_NOT_SUPPLIED)
    assert sorted(finding.claim_index is None for finding in found) == [False, True]
