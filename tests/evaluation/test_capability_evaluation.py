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
* V2 must introduce no forbidden hit that run did not already have;
* deterministic precedence must survive shaping;
* required evidence present in that run must still be present under V2's budget.

Loosening any of these to make the adapter pass would defeat the point of running
it.

**"The same retrieval outcome" is now the run the capability actually makes.**
These comparisons were written against the V1 funnel's own defaults, which was the
same run until repository scope became a boundary at this layer and a bias at the
V1 CLI's (ARCHITECTURE.md §10). Against the V1 default the comparison stopped
measuring the adapter and started measuring the scope decision: it reported four
cases "introducing" evidence that is simply the caller's own repository filling
slots the second pass used to take, and five losing required evidence that lives
in a repository the call did not name. Both would have been *true statements about
a different run*. So :func:`_funnel` runs the funnel with exactly the arguments
``CapabilityService.search`` passes it, and the invariants are unchanged and just
as strict against it. What no longer has a V2 assertion is stated in
:data:`ANSWERABLE_ONLY_GLOBALLY` rather than left to be noticed.
"""

from __future__ import annotations

import dataclasses

import pytest
from repoatlas.capabilities.budget import apply_budget_policy, ceiling_for, retrieval_limits_for
from repoatlas.capabilities.context import resolve_context, to_repository_context
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import CapabilityContext, CapabilityScope, EvidenceBudget
from repoatlas.models.evidence import EvidenceTier
from repoatlas.models.retrieval import RetrievalOutcome
from repoatlas.models.search import SearchMode, SearchRequest, SearchResponse
from repoatlas.retrieval.retriever import DIAGNOSTIC_LIMITS
from repoatlas.services.workspace import Workspace
from tests.evaluation.harness import evaluation_workspace
from tests.evaluation.runner import Case, load_cases

#: What the corpus calls "asked from the workspace root", i.e. global scope.
GLOBAL_CONTEXT_REPO = "__root__"

#: Cases whose required evidence lives outside the repository they are asked in.
#: The V1 corpus still measures them — the CLI still crosses, and
#: ``tests/evaluation/test_evaluation.py`` holds them to the same ground truth it
#: always did. What changed is that a *repository-scoped* capability call can no
#: longer answer them, by decision rather than by accident, so they are asserted
#: here under ``GLOBAL``: the scope an agent is told to use when it means "wherever
#: this lives". They are named, not dropped; a case that stops being answerable at
#: all should fail something.
ANSWERABLE_ONLY_GLOBALLY = frozenset(
    {
        "exact-path-versus-content-mention",
        "exact-path-repository-qualified",
        "exact-subpath-cross-repository",
        "misleading-legacy-note",
        "cross-repo-service-host-chain",
    }
)


@pytest.fixture(scope="module")
def evaluated() -> object:
    with evaluation_workspace() as (workspace, root):
        yield workspace, root


def _context(case: Case) -> CapabilityContext:
    if case.is_global:
        return CapabilityContext(scope=CapabilityScope.GLOBAL)
    return CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=case.context_repo)


def _funnel(workspace: Workspace, case: Case, context: CapabilityContext) -> RetrievalOutcome:
    """The retrieval run ``search`` makes for this case, made directly.

    Every argument here is one :meth:`CapabilityService.search` passes: the
    diagnostic bounds and literal phrase for a ``debug`` case, quoted signatures,
    and ``confined`` — the capability layer's own reading of an explicit context.
    The point of the comparison is that the adapter adds and moves nothing, so the
    baseline has to be the run it adapts rather than a differently configured one.
    """
    diagnostic = case.command == "debug"
    resolution = resolve_context(context, workspace.repositories.list_all())
    applied = apply_budget_policy(
        EvidenceBudget(), workspace.settings, ceiling=ceiling_for(workspace.settings)
    )
    limits = retrieval_limits_for(
        applied, workspace.settings, base=DIAGNOSTIC_LIMITS if diagnostic else None
    )
    return workspace.retriever(limits).retrieve_detailed(
        case.query,
        to_repository_context(resolution),
        literal_phrase=diagnostic,
        quote_signatures=True,
        confined=True,
    )


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
    one hand-picked question. "The same run" means :func:`_funnel` — the one the
    capability makes — so this still fails if the adapter produces a span no
    retrieval produced, which is the thing it is here to prevent.
    """
    workspace, _root = evaluated  # type: ignore[misc]
    service = CapabilityService(workspace)
    offenders: list[str] = []

    for case in load_cases():
        baseline = _funnel(workspace, case, _context(case))
        allowed = {(c.repository_slug, c.path) for c in baseline.evidence.candidates}
        for item in _search(service, case).evidence:
            if (item.ref.repository_slug, item.ref.path) not in allowed:
                offenders.append(f"{case.id}: {item.location}")

    assert not offenders, offenders


def test_search_introduces_no_forbidden_evidence(evaluated: object) -> None:
    """The adapter cannot make a case worse on the metric that must stay at zero."""
    workspace, _root = evaluated  # type: ignore[misc]
    names = _display_names(workspace)
    service = CapabilityService(workspace)
    offenders: list[str] = []

    for case in load_cases():
        if not case.forbidden_evidence:
            continue
        candidates = _funnel(workspace, case, _context(case)).evidence.candidates
        baseline = {
            f"{c.repository}/{c.path}"
            for c in candidates
            if f"{c.repository}/{c.path}" in case.forbidden_evidence
        }
        returned = _returned(_search(service, case), names)
        introduced = {p for p in returned if p in case.forbidden_evidence} - baseline
        if introduced:
            offenders.append(f"{case.id}: {sorted(introduced)}")

    assert not offenders, offenders


def test_a_confined_search_admits_no_forbidden_evidence_at_all(evaluated: object) -> None:
    """Stronger than "introduces none": under confinement there should be none.

    The weaker form above exists because the adapter cannot be blamed for what the
    funnel handed it. Confinement is the capability's own decision, so it owes the
    stronger answer, and this is what would catch a scope change that traded a
    foreign true answer for a local forbidden one.
    """
    workspace, _root = evaluated  # type: ignore[misc]
    names = _display_names(workspace)
    service = CapabilityService(workspace)
    offenders: list[str] = []

    for case in load_cases():
        if not case.forbidden_evidence:
            continue
        hits = [p for p in _returned(_search(service, case), names) if p in case.forbidden_evidence]
        if hits:
            offenders.append(f"{case.id}: {sorted(set(hits))}")

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


def test_required_evidence_the_funnel_found_survives_the_default_budget(
    evaluated: object,
) -> None:
    """What the funnel found for this call, V2 still returns at its default.

    This is the assertion the adapter could most easily buy its way out of, by
    shrinking the default budget until nothing awkward fits. It is written against
    the funnel's own result per case, so it cannot be satisfied that way.

    Required evidence the *scope* rules out is a different matter from required
    evidence the *budget* dropped, and only the second is this test's business.
    The cases where the two used to be confused are named in
    :data:`ANSWERABLE_ONLY_GLOBALLY` and asserted below under the scope that can
    answer them.
    """
    workspace, _root = evaluated  # type: ignore[misc]
    names = _display_names(workspace)
    service = CapabilityService(workspace)
    lost: list[str] = []

    for case in load_cases():
        if not case.required_evidence:
            continue
        candidates = _funnel(workspace, case, _context(case)).evidence.candidates
        located = {f"{c.repository}/{c.path}" for c in candidates}
        found = {req for req in case.required_evidence if req in located}
        if not found:
            continue
        missing = found - set(_returned(_search(service, case), names))
        if missing:
            lost.append(f"{case.id}: {sorted(missing)}")

    assert not lost, lost


@pytest.mark.parametrize("case_id", sorted(ANSWERABLE_ONLY_GLOBALLY))
def test_a_case_that_outgrew_its_scope_is_still_answered_globally(
    evaluated: object, case_id: str
) -> None:
    """Named, and still measured — one scope out.

    These five ask, inside one repository, about a file in another. The V1 CLI
    still answers them where they stand and the V1 report still scores them; a
    repository-scoped capability call no longer can, because an explicit context is
    a boundary at this layer. That is a decision, not a loss of capability, and the
    way to show it is to assert the same ground truth under ``GLOBAL`` rather than
    to stop asserting it. A case that answers under neither scope fails here.
    """
    workspace, _root = evaluated  # type: ignore[misc]
    names = _display_names(workspace)
    case = next(c for c in load_cases() if c.id == case_id)
    globally = dataclasses.replace(case, context_repo=GLOBAL_CONTEXT_REPO)
    response = _search(CapabilityService(workspace), globally)
    assert set(case.required_evidence) <= set(_returned(response, names))


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
    """Shaping must not quietly cut the second repository out of a joined answer.

    Asked in the scope that can produce a joined answer. Both of these cases stand
    inside one repository in the corpus, and a repository-scoped capability call is
    now confined, so asking them where they stand would test the scope decision —
    which has its own tests — instead of the thing this guards, which is a budget
    cutting a repository off the end of an answer that did span two.
    """
    workspace, _root = evaluated  # type: ignore[misc]
    service = CapabilityService(workspace)
    offenders: list[str] = []

    for case in load_cases():
        if not case.expect_cross_repo:
            continue
        globally = dataclasses.replace(case, context_repo=GLOBAL_CONTEXT_REPO)
        slugs = {item.ref.repository_slug for item in _search(service, globally).evidence}
        if len(slugs) < 2:
            offenders.append(f"{case.id}: {sorted(slugs)}")

    assert not offenders, offenders


def test_the_first_result_is_deterministic_wherever_retrieval_said_so(
    evaluated: object,
) -> None:
    """Rank one keeps its tier through the adapter.

    Against the same run as the assertions above, for the same reason: a premise
    taken from a differently scoped run would make this pass or fail for reasons
    that have nothing to do with the adapter it is watching.
    """
    workspace, _root = evaluated  # type: ignore[misc]
    service = CapabilityService(workspace)
    offenders: list[str] = []

    for case in load_cases():
        baseline = _funnel(workspace, case, _context(case))
        if not baseline.evidence.candidates:
            continue
        if baseline.evidence.candidates[0].tier is not EvidenceTier.DETERMINISTIC:
            continue
        response = _search(service, case)
        if response.evidence and response.evidence[0].tier is not EvidenceTier.DETERMINISTIC:
            offenders.append(case.id)

    assert not offenders, offenders
