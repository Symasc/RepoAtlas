"""The evaluation set, run as tests (V1_SPEC §14, §19).

Every category is reported on its own. A combined figure would let a regression in
one category hide behind recall in another.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from tests.evaluation.harness import evaluation_workspace
from tests.evaluation.runner import Outcome, load_cases, report, run_all

pytestmark = pytest.mark.adversarial

#: Every category in the set.
DETERMINISTIC_CATEGORIES = frozenset(
    {
        "exact",
        "unanswerable",
        "misleading",
        "cross-repo",
        "ambiguity",
        "dependencies",
        "salt",
        "bash",
    }
)


@pytest.fixture(scope="module")
def evaluated() -> Iterator[list[Outcome]]:
    with evaluation_workspace() as (workspace, root):
        yield run_all(workspace, root, load_cases())


def _of(outcomes: list[Outcome], category: str) -> list[Outcome]:
    return [o for o in outcomes if o.case.category == category]


def test_the_dataset_is_well_formed() -> None:
    cases = load_cases()
    assert len({case.id for case in cases}) == len(cases), "case ids must be unique"
    for case in cases:
        assert case.notes, f"{case.id} must record why its ground truth is what it is"
        assert case.command in {"ask", "locate", "debug"}
        assert not (case.expect_unknown and case.required_evidence), case.id
        assert not set(case.required_evidence) & set(case.forbidden_evidence), case.id


@pytest.mark.parametrize("category", sorted(DETERMINISTIC_CATEGORIES))
def test_no_forbidden_evidence_is_ever_returned(evaluated: list[Outcome], category: str) -> None:
    """Forbidden evidence is the strongest signal in the set: a path listed there
    is one the fixture proves cannot support the question."""
    offenders = {o.case.id: o.forbidden_hits for o in _of(evaluated, category) if o.forbidden_hits}
    assert offenders == {}


def test_deterministic_precedence_is_never_violated(evaluated: list[Outcome]) -> None:
    violations = [o.case.id for o in evaluated if not o.deterministic_before_associative]
    assert violations == []


def test_every_unanswerable_case_returns_nothing(evaluated: list[Outcome]) -> None:
    wrong = {o.case.id: o.returned[:3] for o in _of(evaluated, "unanswerable") if o.returned}
    assert wrong == {}


@pytest.mark.parametrize("category", sorted(DETERMINISTIC_CATEGORIES - {"unanswerable"}))
def test_required_evidence_is_recalled_within_its_bar(
    evaluated: list[Outcome], category: str
) -> None:
    missed = {
        o.case.id: {p: r for p, r in o.ranks.items() if r is None or r > o.case.recall_k}
        for o in _of(evaluated, category)
    }
    assert {case: miss for case, miss in missed.items() if miss} == {}


def test_cross_repository_expectations_hold(evaluated: list[Outcome]) -> None:
    wrong = {
        o.case.id: sorted(o.repositories)
        for o in evaluated
        if o.case.expect_cross_repo and len(o.repositories) <= 1
    }
    assert wrong == {}


def test_the_report_separates_categories(evaluated: list[Outcome]) -> None:
    """One combined accuracy number would hide which half regressed."""
    text = report(evaluated)
    for category in DETERMINISTIC_CATEGORIES:
        assert category in text
    assert "r@1" in text and "r@10" in text


def test_the_workspace_used_by_the_runner_is_the_fixture_ecosystem() -> None:
    """Guards against the evaluation silently running against an empty index."""
    from tests.adversarial.conftest import REPOSITORIES

    with evaluation_workspace() as (workspace, root):
        names = {repository.name for repository in workspace.repositories.list_all()}
        assert names == set(REPOSITORIES)
        assert (root / "python-service" / "src" / "config.py").exists()
