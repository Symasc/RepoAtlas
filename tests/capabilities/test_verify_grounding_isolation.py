"""Investigation scoping, ledger side effects, and origin neutrality.

Three separate guarantees, kept in one file because each is about what
verification is *not* allowed to reach.

**There is no global E1.** Evidence ids are allocated per investigation, so two
live investigations hold different ``E1``s at the same moment. A verification
under one may not see the other's, and the failure mode if it could is severe:
the caller would be told a citation checks out against evidence their task was
never shown. Nothing here looks up an id without first resolving the
investigation, and the lookup is the ledger's own — there is no registry-wide
evidence index to accidentally consult.

**Verification consumes evidence and produces none.** It allocates no id,
strengthens no metadata, adds no origin and supersedes nothing. The one thing it
may change is an evidence *status*, because that is a current observation about
whether previously supplied evidence still corresponds to the index — and an
observation is not a fabrication.

**Origin does not affect validity.** Evidence from search, inspection, the graph
and the dependency index is subject to one rule. Privileging search evidence would
make grounding a statement about how something was found rather than about
whether it was supplied.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.capabilities.session import UnknownInvestigationError
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.dependency_inventory import DependenciesRequest
from repoatlas.models.evidence import EvidenceSource
from repoatlas.models.inspection import InspectRequest, InspectTarget
from repoatlas.models.investigation import EvidenceStatus
from repoatlas.models.relationships import RelationshipsRequest
from repoatlas.models.search import SearchRequest
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

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)
SERVICE = "python-service"
CONFIG = "src/config.py"
DEPLOY = "file:scripts:deploy.sh"
SERVICE_HOST = "SERVICE_HOST"


def _answer(*claims: AgentClaim) -> AgentAnswer:
    return AgentAnswer(summary="what the evidence shows", claims=claims)


def _claim(statement: str, *citations: AgentCitation) -> AgentClaim:
    return AgentClaim(statement=statement, claimed_confidence=0.9, citations=tuple(citations))


def _verify(
    service: CapabilityService, investigation_id: str, answer: AgentAnswer
) -> VerifyGroundingResponse:
    return service.verify_grounding(
        VerifyGroundingRequest(investigation_id=investigation_id, answer=answer)
    )


def _cite(
    service: CapabilityService, investigation: str, evidence_id: str
) -> VerifyGroundingResponse:
    return _verify(
        service, investigation, _answer(_claim("x", AgentCitation(evidence_id=evidence_id)))
    )


def _search(service: CapabilityService, question: str = SERVICE_HOST) -> tuple[str, list]:
    response = service.search(SearchRequest(question=question, context=GLOBAL))
    assert response.evidence
    return response.investigation_id, list(response.evidence)


# ---------------------------------------------------------------------------
# CASE 13 — investigations do not see each other
# ---------------------------------------------------------------------------


def test_two_investigations_allocate_the_same_id_to_different_evidence(
    initialized: Workspace,
) -> None:
    """The premise. Without this collision the isolation test proves nothing."""
    service = CapabilityService(initialized)
    first, first_evidence = _search(service)
    second, second_evidence = _search(service, question="deploy.sh")

    assert first != second
    assert first_evidence[0].id == second_evidence[0].id == "E1"
    assert first_evidence[0].ref != second_evidence[0].ref


def test_a_citation_can_only_reach_the_investigation_that_issued_it(
    initialized: Workspace,
) -> None:
    """CASE 13. ``E1`` under investigation B is B's ``E1`` and nothing else.

    Both investigations hold an ``E1``, at different files. Verification under B
    resolves B's, and the reported location proves which one — a verdict that
    silently checked A's would be indistinguishable from a correct one to the
    caller.
    """
    service = CapabilityService(initialized)
    first, first_evidence = _search(service)
    second, second_evidence = _search(service, question="deploy.sh")

    under_first = _cite(service, first, "E1")
    under_second = _cite(service, second, "E1")

    assert under_first.citations[0].location == first_evidence[0].ref.location
    assert under_second.citations[0].location == second_evidence[0].ref.location
    assert under_first.citations[0].location != under_second.citations[0].location


def test_an_id_absent_from_this_investigation_is_never_found_in_another(
    initialized: Workspace,
) -> None:
    """No fallback search across sessions, and no borrowed evidence."""
    service = CapabilityService(initialized)
    first, first_evidence = _search(service)
    assert len(first_evidence) > 1, "this test needs A to hold more ids than B"
    second = service.search(
        SearchRequest(question="a question with one narrow answer", context=GLOBAL)
    )
    held_by_second = {
        entry.id for entry in service.sessions.get(second.investigation_id).ledger.entries()
    }
    only_in_first = next(item.id for item in first_evidence if item.id not in held_by_second)

    response = _cite(service, second.investigation_id, only_in_first)

    assert response.verdict is GroundingVerdict.UNGROUNDED
    assert {finding.code for finding in response.findings} == {
        GroundingFindingCode.UNKNOWN_EVIDENCE
    }
    assert response.evidence_statuses[0].status is None
    assert first != second.investigation_id


def test_verification_never_creates_the_investigation_it_was_given(
    initialized: Workspace,
) -> None:
    """An unknown id is refused, not turned into a fresh empty ledger."""
    service = CapabilityService(initialized)
    _search(service)
    before = set(service.sessions._sessions)

    with pytest.raises(UnknownInvestigationError):
        _cite(service, "investigation-nobody-issued", "E1")

    assert set(service.sessions._sessions) == before


def test_one_investigations_verification_does_not_touch_anothers_ledger(
    initialized: Workspace,
) -> None:
    service = CapabilityService(initialized)
    first, _ = _search(service)
    second, _ = _search(service, question="deploy.sh")
    other = service.sessions.get(second).ledger
    before = {entry.id: entry for entry in other.entries()}

    _cite(service, first, "E1")

    assert {entry.id: entry for entry in other.entries()} == before


# ---------------------------------------------------------------------------
# CASE 15 — verification allocates nothing
# ---------------------------------------------------------------------------


def _structure(service: CapabilityService, investigation: str) -> dict[str, object]:
    """Everything about the ledger that verification is forbidden to change."""
    ledger = service.sessions.get(investigation).ledger
    return {
        "next_number": ledger._next_number,
        "ids": tuple(entry.id for entry in ledger.entries()),
        "refs": {entry.id: entry.ref for entry in ledger.entries()},
        "tiers": {entry.id: entry.tier for entry in ledger.entries()},
        "matches": {entry.id: entry.match for entry in ledger.entries()},
        "origins": {entry.id: entry.origins for entry in ledger.entries()},
        "reasons": {entry.id: entry.reasons for entry in ledger.entries()},
        "allocated": {entry.id: entry.allocated_at for entry in ledger.entries()},
        "superseded_by": {entry.id: entry.superseded_by for entry in ledger.entries()},
    }


def test_verification_allocates_no_evidence_id(initialized: Workspace) -> None:
    """CASE 15. The next free number is exactly where it was."""
    service = CapabilityService(initialized)
    investigation, evidence = _search(service)
    before = _structure(service, investigation)

    _cite(service, investigation, evidence[0].id)

    assert _structure(service, investigation) == before


def test_verification_strengthens_no_metadata(initialized: Workspace) -> None:
    """Verification is a reader. It adds no origin, no reason and no tier.

    An entry found associatively and then cited in a verified answer is still an
    entry found associatively. Letting a citation strengthen it would make the
    ledger record how confidently the *agent* used the evidence rather than how
    RepoAtlas found it.
    """
    service = CapabilityService(initialized)
    investigation, evidence = _search(service)
    ledger = service.sessions.get(investigation).ledger
    before = {
        entry.id: (entry.tier, entry.match, entry.origins, entry.reasons)
        for entry in ledger.entries()
    }

    for item in evidence:
        _cite(service, investigation, item.id)

    after = {
        entry.id: (entry.tier, entry.match, entry.origins, entry.reasons)
        for entry in ledger.entries()
    }
    assert after == before


def test_a_failed_verification_allocates_nothing_either(initialized: Workspace) -> None:
    service = CapabilityService(initialized)
    investigation, _ = _search(service)
    before = _structure(service, investigation)

    _verify(
        service,
        investigation,
        _answer(
            _claim("a", AgentCitation(evidence_id="E999")),
            _claim("b", AgentCitation(evidence_id="E1", line_start=9000, line_end=9001)),
        ),
    )

    assert _structure(service, investigation) == before


def test_verification_may_record_a_truthful_status_change(initialized: Workspace) -> None:
    """The one permitted mutation, and it is an observation rather than a change.

    Status says whether the index still stands behind the record. Refusing to
    update it would mean verification reported a standing it knew to be out of
    date — which is the opposite of the guarantee.
    """
    service = CapabilityService(initialized)
    investigation, evidence = _search(service)
    refs_before = {
        entry.id: entry.ref for entry in service.sessions.get(investigation).ledger.entries()
    }

    response = _cite(service, investigation, evidence[0].id)

    # Nothing moved, so nothing is reported as having moved.
    assert response.ledger.changed == ()
    refs_after = {
        entry.id: entry.ref for entry in service.sessions.get(investigation).ledger.entries()
    }
    assert refs_after == refs_before


def test_a_status_change_observed_during_verification_is_not_rolled_back(
    initialized: Workspace, workspace_root: Path
) -> None:
    """A truthful observation survives a later failure in the same call.

    Verification notices that evidence went stale, and the rest of the submitted
    answer is nonsense. The staleness is still true. Reverting it for the sake of
    operation atomicity would make the ledger claim a standing RepoAtlas had
    already established was wrong — atomicity belongs to evidence *insertion*, and
    this call inserts nothing.
    """
    from repoatlas.services.sync_service import sync

    service = CapabilityService(initialized)
    investigation, evidence = _search(service)
    cited = evidence[0]

    edited = workspace_root / cited.ref.repository_slug / cited.ref.path
    edited.write_text("# inserted\n" + edited.read_text(encoding="utf-8"), encoding="utf-8")
    sync(initialized, initialized.resolver().resolve(workspace_root))

    response = _verify(
        service,
        investigation,
        _answer(
            _claim("cites the now-stale evidence", AgentCitation(evidence_id=cited.id)),
            _claim("cites nothing that exists", AgentCitation(evidence_id="E999")),
        ),
    )

    entry = service.sessions.get(investigation).ledger.get(cited.id)
    assert entry is not None
    assert entry.status is EvidenceStatus.STALE
    assert response.verdict is GroundingVerdict.UNGROUNDED


# ---------------------------------------------------------------------------
# CASE 14 — origin is diagnostic, never a privilege
# ---------------------------------------------------------------------------


def _from_search(service: CapabilityService) -> tuple[str, str]:
    investigation, evidence = _search(service)
    return investigation, evidence[0].id


def _from_inspect(service: CapabilityService, investigation: str | None = None) -> tuple[str, str]:
    response = service.inspect(
        InspectRequest(
            context=CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=SERVICE),
            target=InspectTarget(path=CONFIG),
            line_start=1,
            line_end=6,
            context_lines=0,
            allow_worktree=False,
            investigation_id=investigation,
        )
    )
    assert response.segments
    return response.investigation_id, response.segments[0].id


def _from_relationships(
    service: CapabilityService, investigation: str | None = None
) -> tuple[str, str]:
    response = service.relationships(
        RelationshipsRequest(
            context=GLOBAL,
            target=DEPLOY,
            max_neighbors=200,
            investigation_id=investigation,
        )
    )
    assert response.evidence, "the fixture graph must quote at least one edge"
    return response.investigation_id, response.evidence[0].id


def _from_dependencies(
    service: CapabilityService, investigation: str | None = None
) -> tuple[str, str]:
    response = service.dependencies(
        DependenciesRequest(context=GLOBAL, limit=50, investigation_id=investigation)
    )
    assert response.evidence, "the fixture manifests must quote at least one line"
    return response.investigation_id, response.evidence[0].id


#: One supplier per capability that can put evidence in a ledger. Search evidence
#: carries a retrieval stage as its origin rather than a "search" member, which is
#: itself the point: origin names a route, and no route is privileged.
ORIGINS = {
    "search": _from_search,
    "inspect": _from_inspect,
    "relationship": _from_relationships,
    "dependency": _from_dependencies,
}


@pytest.mark.parametrize("supply", list(ORIGINS.values()), ids=list(ORIGINS))
def test_evidence_from_every_origin_grounds_under_the_same_rule(
    initialized: Workspace, supply: object
) -> None:
    """CASE 14. Search evidence is not privileged, and graph evidence is not suspect."""
    service = CapabilityService(initialized)
    investigation, evidence_id = supply(service)  # type: ignore[operator]

    response = _cite(service, investigation, evidence_id)

    assert response.verdict is GroundingVerdict.GROUNDED
    assert response.findings == ()
    assert response.evidence_statuses[0].status is EvidenceStatus.VALID


def test_one_answer_may_rest_on_evidence_from_several_capabilities(
    initialized: Workspace,
) -> None:
    """An investigation is one task, and a task uses whatever capability it needs."""
    service = CapabilityService(initialized)
    investigation, search_id = _from_search(service)
    _, inspect_id = _from_inspect(service, investigation)
    _, relationship_id = _from_relationships(service, investigation)
    _, dependency_id = _from_dependencies(service, investigation)
    ids = [search_id, inspect_id, relationship_id, dependency_id]

    response = _verify(
        service,
        investigation,
        _answer(
            *(
                _claim(f"claim {index}", AgentCitation(evidence_id=evidence_id))
                for index, evidence_id in enumerate(ids)
            )
        ),
    )

    assert response.verdict is GroundingVerdict.GROUNDED
    assert len(response.sanitized_answer.claims) == len(ids)
    assert all(verdict.grounded for verdict in response.citations)


def test_origin_is_reported_for_diagnostics_and_changes_no_rule(
    initialized: Workspace,
) -> None:
    """The caller can see where evidence came from; the algorithm cannot."""
    service = CapabilityService(initialized)
    investigation, evidence_id = _from_dependencies(service)

    response = _cite(service, investigation, evidence_id)

    status = response.evidence_statuses[0]
    assert status.origins
    assert EvidenceSource.DEPENDENCY in status.origins
    assert response.verdict is GroundingVerdict.GROUNDED
