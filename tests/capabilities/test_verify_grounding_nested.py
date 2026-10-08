"""Grounding in a nested repository, and findings that name what actually happened.

A nested repository's slug holds a ``/`` (``outer/tools/inner``). Verification once
rebuilt an admitted citation's slug and path by splitting its display location at the
first ``/``, so every citation into a nested repository reached the whole-answer check
pointing at a file that does not exist there. The check dropped the claim as cited to
nothing, and the capability, seeing every citation admitted, reported the drop as prose
naming something unsupplied — even when the prose named no path at all.

Two guarantees are pinned here: a nested repository grounds exactly like any other, and
a removal is reported for the reason that removal was made, never inferred by ruling
the alternatives out.
"""

from __future__ import annotations

import pytest
from repoatlas.capabilities import service as service_module
from repoatlas.capabilities.service import CapabilityService
from repoatlas.grounding import (
    FABRICATED_CITATION,
    NAMED_REFERENCE,
    NO_EVIDENCE,
    ground_answer,
)
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.evidence import (
    Evidence,
    EvidenceCandidate,
    EvidenceSet,
    Hypothesis,
    RepoAtlasAnswer,
)
from repoatlas.models.inspection import InspectRequest, InspectTarget
from repoatlas.models.verification import (
    AgentAnswer,
    AgentCitation,
    AgentClaim,
    GroundingFindingCode,
    GroundingVerdict,
    VerifyGroundingRequest,
    VerifyGroundingResponse,
)
from repoatlas.services.workspace import Workspace

INNER = "inner.py"


def _nested_slug(workspace: Workspace) -> str:
    slug = next(
        r.slug for r in workspace.repositories.list_all() if r.slug.endswith("nested-inner")
    )
    # The whole point: a slug that itself holds a separator.
    assert "/" in slug, slug
    return slug


def _inspect(service: CapabilityService, slug: str, path: str) -> tuple[str, str]:
    response = service.inspect(
        InspectRequest(
            context=CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=slug),
            target=InspectTarget(path=path),
            allow_worktree=False,
        )
    )
    assert response.segments, f"{slug}/{path} supplied nothing"
    return response.investigation_id, response.segments[0].id


def _verify(
    service: CapabilityService, investigation: str, statement: str, evidence_id: str
) -> VerifyGroundingResponse:
    return service.verify_grounding(
        VerifyGroundingRequest(
            investigation_id=investigation,
            answer=AgentAnswer(
                summary="",
                claims=(
                    AgentClaim(
                        statement=statement, citations=(AgentCitation(evidence_id=evidence_id),)
                    ),
                ),
            ),
        )
    )


def _codes(response: VerifyGroundingResponse) -> set[GroundingFindingCode]:
    return {finding.code for finding in response.findings}


@pytest.mark.parametrize(
    "prose",
    [
        "The helper returns a constant.",
        f"{INNER} returns a constant.",
        "{slug}/" + INNER + " returns a constant.",
    ],
    ids=["names-nothing", "names-the-file", "names-slug-and-file"],
)
def test_a_nested_repository_grounds_after_inspection(initialized: Workspace, prose: str) -> None:
    service = CapabilityService(initialized)
    slug = _nested_slug(initialized)
    investigation, evidence_id = _inspect(service, slug, INNER)

    response = _verify(service, investigation, prose.format(slug=slug), evidence_id)

    assert response.verdict is GroundingVerdict.GROUNDED, response.findings
    assert response.findings == ()
    assert response.dropped_claims == 0
    assert len(response.sanitized_answer.claims) == 1


def test_an_unsupplied_file_in_a_nested_claim_is_still_a_prose_finding(
    initialized: Workspace,
) -> None:
    service = CapabilityService(initialized)
    slug = _nested_slug(initialized)
    investigation, evidence_id = _inspect(service, slug, INNER)

    response = _verify(service, investigation, "tools/never_shown.py sets the value.", evidence_id)

    assert _codes(response) == {GroundingFindingCode.PROSE_REFERENCE_NOT_SUPPLIED}
    assert response.dropped_claims == 1


def test_a_removal_the_prose_did_not_cause_is_never_blamed_on_the_prose(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forge the old defect: hand the whole-answer check a source the citation never
    named. The claim goes, and the finding says RepoAtlas disagreed with itself."""
    original = service_module.projected_answer

    def misprojected(*args: object, **kwargs: object) -> RepoAtlasAnswer:
        answer = original(*args, **kwargs)  # type: ignore[arg-type]
        hypotheses = [
            h.model_copy(
                update={
                    "evidence": [
                        e.model_copy(update={"path": f"elsewhere/{e.path}"}) for e in h.evidence
                    ]
                }
            )
            for h in answer.hypotheses
        ]
        return answer.model_copy(update={"hypotheses": hypotheses})

    monkeypatch.setattr(service_module, "projected_answer", misprojected)
    service = CapabilityService(initialized)
    investigation, evidence_id = _inspect(service, "python-service", "src/config.py")

    response = _verify(service, investigation, "The host is read here.", evidence_id)

    assert response.citations[0].grounded is True
    assert _codes(response) == {GroundingFindingCode.ADMITTED_CITATION_REJECTED}
    assert response.dropped_claims == 1


# ---------------------------------------------------------------------------
# ground_answer records why it removed each hypothesis
# ---------------------------------------------------------------------------

_SUPPLIED = EvidenceCandidate(
    repository="outer/inner",
    repository_slug="outer/inner",
    path="src/app.py",
    line_start=1,
    line_end=5,
    text="",
    description="",
)


def _hypothesis(statement: str, *evidence: Evidence) -> Hypothesis:
    return Hypothesis(statement=statement, confidence=0.9, evidence=list(evidence))


def _cite(path: str = "src/app.py") -> Evidence:
    return Evidence(
        repository="outer/inner",
        repository_slug="outer/inner",
        path=path,
        line_start=1,
        line_end=2,
        description="",
    )


def test_ground_answer_records_each_removal_reason() -> None:
    report = ground_answer(
        RepoAtlasAnswer(
            summary="",
            hypotheses=[
                _hypothesis("kept", _cite()),
                _hypothesis("names src/other.py", _cite()),
                _hypothesis("cites nothing real", _cite("src/missing.py")),
                _hypothesis("cites nothing at all"),
                _hypothesis("both: src/other.py", _cite("src/missing.py")),
            ],
        ),
        EvidenceSet(question="", candidates=(_SUPPLIED,)),
    )

    assert [h.statement for h in report.answer.hypotheses] == ["kept"]
    assert dict(report.removals) == {
        1: frozenset({NAMED_REFERENCE}),
        2: frozenset({FABRICATED_CITATION}),
        3: frozenset({NO_EVIDENCE}),
        4: frozenset({NAMED_REFERENCE, FABRICATED_CITATION}),
    }


def test_a_nested_slug_citation_resolves_in_ground_answer() -> None:
    report = ground_answer(
        RepoAtlasAnswer(summary="", hypotheses=[_hypothesis("kept", _cite())]),
        EvidenceSet(question="", candidates=(_SUPPLIED,)),
    )

    assert report.removals == ()
    assert [e.repository_slug for e in report.answer.hypotheses[0].evidence] == ["outer/inner"]
