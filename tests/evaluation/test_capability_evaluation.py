"""The evaluation corpus, driven through ``CapabilityService.search`` (Slice D).

The V1 evaluation asks whether the funnel finds the right evidence. This asks a
narrower and different question: **does the capability layer preserve what the
funnel found?**

Search is an adapter, so it is allowed to return *less* than retrieval did — the
public character budget is exact where V1's internal bound is approximate, and
narrowing is the whole point of a budget. It is not allowed to return anything
else: no evidence retrieval did not produce, no forbidden path retrieval did not
return, and no reordering of the trusted ranking.

So the comparison is deliberately asymmetric, and the asymmetry is stated rather
than assumed:

* every V2 item must correspond to a candidate from the same retrieval outcome;
* V2 must introduce no forbidden hit that V1 did not already have;
* deterministic precedence must survive shaping;
* required evidence present under V1's default must still be present under V2's.

Loosening any of these to make the adapter pass would defeat the point of running
it.
"""

from __future__ import annotations

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.evidence import EvidenceTier
from repoatlas.models.search import SearchMode, SearchRequest, SearchResponse
from repoatlas.services.workspace import Workspace
from tests.evaluation.harness import evaluation_workspace
from tests.evaluation.runner import Case, load_cases, run_case


@pytest.fixture(scope="module")
def evaluated() -> object:
    with evaluation_workspace() as (workspace, root):
        yield workspace, root


def _context(case: Case) -> CapabilityContext:
    if case.is_global:
        return CapabilityContext(scope=CapabilityScope.GLOBAL)
    return CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=case.context_repo)


def _search(service: CapabilityService, case: Case) -> SearchResponse:
    """Run one case the way its command would run it."""
    return service.search(
        SearchRequest(
            question=case.query,
            context=_context(case),
            mode=SearchMode.DIAGNOSTIC if case.command == "debug" else SearchMode.LOOKUP,
        )
    )


def _display_names(workspace: Workspace) -> dict[str, str]:
    """Slug to display name, because the corpus names evidence the way V1 prints it."""
    return {repo.slug: repo.name for repo in workspace.repositories.list_all()}


def _returned(response: SearchResponse, names: dict[str, str]) -> list[str]:
    return [
        f"{names.get(item.ref.repository_slug, item.ref.repository_slug)}/{item.ref.path}"
        for item in response.evidence
    ]


def test_search_returns_nothing_retrieval_did_not(evaluated: object) -> None:
    """Every item corresponds to a candidate of the very same retrieval run.

    The central Slice D invariant, checked across the whole corpus rather than on
    one hand-picked question.
    """
    workspace, root = evaluated  # type: ignore[misc]
    service = CapabilityService(workspace)
    offenders: list[str] = []

    for case in load_cases():
        baseline = run_case(workspace, root, case)
        allowed = {(c.repository_slug, c.path) for c in baseline.evidence.candidates}
        for item in _search(service, case).evidence:
            if (item.ref.repository_slug, item.ref.path) not in allowed:
                offenders.append(f"{case.id}: {item.location}")

    assert not offenders, offenders


def test_search_introduces_no_forbidden_evidence(evaluated: object) -> None:
    """The adapter cannot make a case worse on the metric that must stay at zero."""
    workspace, root = evaluated  # type: ignore[misc]
    names = _display_names(workspace)
    service = CapabilityService(workspace)
    offenders: list[str] = []

    for case in load_cases():
        if not case.forbidden_evidence:
            continue
        baseline = set(run_case(workspace, root, case).forbidden_hits)
        returned = _returned(_search(service, case), names)
        introduced = {p for p in returned if p in case.forbidden_evidence} - baseline
        if introduced:
            offenders.append(f"{case.id}: {sorted(introduced)}")

    assert not offenders, offenders


def test_deterministic_precedence_survives_shaping(evaluated: object) -> None:
    """Budgeting narrows; it never lets an associative result climb."""
    workspace, _root = evaluated  # type: ignore[misc]
    service = CapabilityService(workspace)
    offenders: list[str] = []

    for case in load_cases():
        tiers = [item.tier for item in _search(service, case).evidence]
        if tiers != sorted(tiers):
            offenders.append(f"{case.id}: {[t.name for t in tiers]}")

    assert not offenders, offenders


def test_required_evidence_present_under_v1_survives_the_default_budget(
    evaluated: object,
) -> None:
    """What V1 found at its default, V2 still returns at its default.

    This is the assertion the adapter could most easily buy its way out of, by
    shrinking the default budget until nothing awkward fits. It is written against
    V1's own result per case, so it cannot be satisfied that way.
    """
    workspace, root = evaluated  # type: ignore[misc]
    names = _display_names(workspace)
    service = CapabilityService(workspace)
    lost: list[str] = []

    for case in load_cases():
        if not case.required_evidence:
            continue
        baseline = run_case(workspace, root, case)
        found_by_v1 = {req for req, rank in baseline.ranks.items() if rank is not None}
        if not found_by_v1:
            continue
        returned = set(_returned(_search(service, case), names))
        missing = found_by_v1 - returned
        if missing:
            lost.append(f"{case.id}: {sorted(missing)}")

    assert not lost, lost


def test_unanswerable_cases_stay_unanswered(evaluated: object) -> None:
    """Suppression survives the adapter, and empty stays empty."""
    workspace, _root = evaluated  # type: ignore[misc]
    service = CapabilityService(workspace)
    offenders: list[str] = []

    for case in load_cases():
        if not case.expect_unknown:
            continue
        response = _search(service, case)
        if response.evidence:
            offenders.append(f"{case.id}: {[i.location for i in response.evidence]}")

    assert not offenders, offenders


def test_every_case_records_exactly_what_it_returned(evaluated: object) -> None:
    """Across the whole corpus: no returned id is missing from its ledger."""
    workspace, _root = evaluated  # type: ignore[misc]
    service = CapabilityService(workspace)

    for case in load_cases():
        response = _search(service, case)
        ledger = service.sessions.get(response.investigation_id).ledger
        assert {item.id for item in response.evidence} == {e.id for e in ledger.entries()}, case.id


def test_every_returned_span_is_covered_by_its_text(evaluated: object) -> None:
    """Supplied-span safety, across every case in the corpus."""
    workspace, _root = evaluated  # type: ignore[misc]
    service = CapabilityService(workspace)
    offenders: list[str] = []

    for case in load_cases():
        for item in _search(service, case).evidence:
            if item.ref.line_start is None:
                if item.ref.line_end is not None:
                    offenders.append(f"{case.id}: {item.location} half a span")
                continue
            assert item.ref.line_end is not None
            supplied = len(item.text.split("\n"))
            claimed = item.ref.line_end - item.ref.line_start + 1
            if supplied != claimed:
                offenders.append(f"{case.id}: {item.location} {supplied} lines for {claimed}")

    assert not offenders, offenders


def test_cross_repository_cases_still_reach_both_repositories(evaluated: object) -> None:
    """Shaping must not quietly cut the second repository out of a joined answer."""
    workspace, _root = evaluated  # type: ignore[misc]
    service = CapabilityService(workspace)
    offenders: list[str] = []

    for case in load_cases():
        if not case.expect_cross_repo:
            continue
        slugs = {item.ref.repository_slug for item in _search(service, case).evidence}
        if len(slugs) < 2:
            offenders.append(f"{case.id}: {sorted(slugs)}")

    assert not offenders, offenders


def test_the_first_result_is_deterministic_wherever_v1_said_so(evaluated: object) -> None:
    """Rank one keeps its tier through the adapter."""
    workspace, root = evaluated  # type: ignore[misc]
    service = CapabilityService(workspace)
    offenders: list[str] = []

    for case in load_cases():
        baseline = run_case(workspace, root, case)
        if not baseline.evidence.candidates:
            continue
        if baseline.evidence.candidates[0].tier is not EvidenceTier.DETERMINISTIC:
            continue
        response = _search(service, case)
        if response.evidence and response.evidence[0].tier is not EvidenceTier.DETERMINISTIC:
            offenders.append(case.id)

    assert not offenders, offenders
