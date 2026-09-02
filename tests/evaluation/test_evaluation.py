"""The evaluation set, run as tests (V1_SPEC §14, §19).

Two runs of the same hand-written cases:

* the default run uses the deterministic `HashingEmbedding`, so every category
  except `semantic` is fully meaningful and CI needs no network;
* the `slow` run uses the real `BAAI/bge-small-en-v1.5`, which is the only
  configuration in which the semantic numbers mean anything at all — the stub
  shares tokens, not meaning.

Reporting them separately is deliberate. A combined figure would let a semantic
regression hide behind exact-search recall, or the reverse.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from tests.evaluation.harness import evaluation_workspace
from tests.evaluation.runner import Outcome, load_cases, report, run_all

pytestmark = pytest.mark.adversarial

#: Categories whose result does not depend on the embedding backend.
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
def stub_outcomes() -> Iterator[list[Outcome]]:
    with evaluation_workspace("hashing") as (workspace, root):
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
def test_no_forbidden_evidence_is_ever_returned(
    stub_outcomes: list[Outcome], category: str
) -> None:
    """Forbidden evidence is the strongest signal in the set: a path listed there
    is one the fixture proves cannot support the question."""
    offenders = {
        o.case.id: o.forbidden_hits for o in _of(stub_outcomes, category) if o.forbidden_hits
    }
    assert offenders == {}


def test_deterministic_precedence_is_never_violated(stub_outcomes: list[Outcome]) -> None:
    violations = [o.case.id for o in stub_outcomes if not o.deterministic_before_associative]
    assert violations == []


def test_every_unanswerable_case_returns_nothing(stub_outcomes: list[Outcome]) -> None:
    wrong = {o.case.id: o.returned[:3] for o in _of(stub_outcomes, "unanswerable") if o.returned}
    assert wrong == {}


@pytest.mark.parametrize("category", sorted(DETERMINISTIC_CATEGORIES - {"unanswerable"}))
def test_required_evidence_is_recalled_within_its_bar(
    stub_outcomes: list[Outcome], category: str
) -> None:
    missed = {
        o.case.id: {p: r for p, r in o.ranks.items() if r is None or r > o.case.recall_k}
        for o in _of(stub_outcomes, category)
    }
    assert {case: miss for case, miss in missed.items() if miss} == {}


def test_cross_repository_expectations_hold(stub_outcomes: list[Outcome]) -> None:
    wrong = {
        o.case.id: sorted(o.repositories)
        for o in stub_outcomes
        if o.case.expect_cross_repo and len(o.repositories) <= 1
    }
    assert wrong == {}


def test_the_report_separates_categories(stub_outcomes: list[Outcome]) -> None:
    """One combined accuracy number would hide which half regressed."""
    text = report(stub_outcomes)
    for category in DETERMINISTIC_CATEGORIES:
        assert category in text
    assert "r@1" in text and "r@10" in text


# -- the real model ----------------------------------------------------------


@pytest.mark.slow
def test_semantic_recall_against_the_real_model(capsys: pytest.CaptureFixture[str]) -> None:
    """The semantic cases, measured with the model V1 actually ships.

    Prose questions that name no identifier can only be answered by the embedding
    stage, so this is the one measurement that says whether semantic retrieval
    works. The bar is each case's own `recall_k`, defined by hand.
    """
    with evaluation_workspace("fastembed") as (workspace, root):
        embeddings = workspace.embeddings()
        if embeddings is None or not embeddings.available():
            pytest.skip("the real embedding model is not available in this environment")
        outcomes = run_all(workspace, root, load_cases())

    with capsys.disabled():
        print("\n" + report(outcomes))

    semantic = _of(outcomes, "semantic")
    missed = {
        o.case.id: {p: r for p, r in o.ranks.items() if r is None or r > o.case.recall_k}
        for o in semantic
    }
    assert {case: miss for case, miss in missed.items() if miss} == {}
    assert all(not o.forbidden_hits for o in semantic), [o.forbidden_hits for o in semantic]


@pytest.mark.slow
def test_the_real_model_does_not_break_the_deterministic_categories() -> None:
    """A working semantic stage must not displace anything it ranks alongside."""
    with evaluation_workspace("fastembed") as (workspace, root):
        embeddings = workspace.embeddings()
        if embeddings is None or not embeddings.available():
            pytest.skip("the real embedding model is not available in this environment")
        outcomes = run_all(workspace, root, load_cases())

    assert [o.case.id for o in outcomes if not o.deterministic_before_associative] == []
    assert {o.case.id: o.returned[:3] for o in _of(outcomes, "unanswerable") if o.returned} == {}
    assert {
        o.case.id: o.forbidden_hits
        for o in outcomes
        if o.case.category in DETERMINISTIC_CATEGORIES and o.forbidden_hits
    } == {}


def test_the_workspace_used_by_the_runner_is_the_fixture_ecosystem() -> None:
    """Guards against the evaluation silently running against an empty index."""
    from tests.adversarial.conftest import REPOSITORIES

    with evaluation_workspace("hashing") as (workspace, root):
        names = {repository.name for repository in workspace.repositories.list_all()}
        assert names == set(REPOSITORIES)
        assert (root / "python-service" / "src" / "config.py").exists()
