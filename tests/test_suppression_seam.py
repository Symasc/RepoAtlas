"""The funnel returns what it decided, not only what it found (V1_SPEC §14).

Suppression — the decision that every associative source would be answering a
different question — was computed once and then survived only as an English
sentence in a trace field. Anything downstream that needed the decision had two
options, and both are wrong: parse the prose, or run the funnel a second time and
watch it decide again.

``retrieve_detailed`` is the seam that removes the choice. These tests assert the
decision is a value, that the trace is rendered *from* that value, that one call
is still one pass, and that ``EvidenceSet`` — the V1 contract and the ``--json``
shape — gained nothing at all.
"""

from __future__ import annotations

from pathlib import Path

from repoatlas.models.evidence import EvidenceSet
from repoatlas.retrieval import retriever as retriever_module
from repoatlas.services.workspace import Workspace

#: Spelled nowhere in the fixture repositories, and shaped like something a user
#: would name outright rather than a word lifted out of prose.
ABSENT = "TOTALLY_ABSENT_IDENTIFIER"

#: Every stage that honours the suppression decision.
SUPPRESSED_STAGES = frozenset({"lexical", "symbol", "semantic"})


def _outcome(workspace: Workspace, question: str, cwd: Path):
    context = workspace.resolver().resolve(cwd)
    return workspace.retriever().retrieve_detailed(question, context)


# -- the decision is a value ----------------------------------------------------


def test_a_question_naming_nothing_that_exists_reports_why(
    initialized: Workspace, workspace_root: Path
) -> None:
    outcome = _outcome(initialized, f"why does {ABSENT} fail?", workspace_root)
    assert outcome.suppression is not None
    assert outcome.suppression.reason == "named_terms_absent"
    assert ABSENT in outcome.suppression.terms
    assert outcome.evidence.candidates == ()


def test_an_answerable_question_suppresses_nothing(
    initialized: Workspace, workspace_root: Path
) -> None:
    outcome = _outcome(initialized, "Where is SERVICE_HOST configured?", workspace_root)
    assert outcome.suppression is None
    assert outcome.evidence.candidates


def test_the_trace_is_rendered_from_the_decision(
    initialized: Workspace, workspace_root: Path
) -> None:
    """The sentence comes from the value. Nothing recovers the value from it."""
    outcome = _outcome(initialized, f"why does {ABSENT} fail?", workspace_root)
    assert outcome.suppression is not None
    trace = outcome.evidence.trace
    assert trace is not None
    details = {
        stage.name: stage.detail for stage in trace.stages if stage.name in SUPPRESSED_STAGES
    }
    assert details, "no suppressible stage ran"
    assert set(details.values()) == {outcome.suppression.message}


# -- one call is one pass ---------------------------------------------------------


def test_retrieve_returns_exactly_the_outcomes_evidence(
    initialized: Workspace, workspace_root: Path
) -> None:
    """The V1 entry point is a projection of the detailed one, not a second run."""
    context = initialized.resolver().resolve(workspace_root)
    question = "Where is SERVICE_HOST configured?"
    evidence = initialized.retriever().retrieve(question, context)
    detailed = initialized.retriever().retrieve_detailed(question, context)
    assert evidence.candidates == detailed.evidence.candidates
    assert evidence.truncated == detailed.evidence.truncated
    assert evidence.scope == detailed.evidence.scope
    assert evidence.primary_repository == detailed.evidence.primary_repository


def test_one_retrieve_analyzes_the_question_once(
    initialized: Workspace, workspace_root: Path, monkeypatch
) -> None:
    """The funnel is one pass. Two would be two chances to decide differently."""
    calls: list[str] = []
    original = retriever_module.analyze

    def counting(question: str):  # type: ignore[no-untyped-def]
        calls.append(question)
        return original(question)

    monkeypatch.setattr(retriever_module, "analyze", counting)
    context = initialized.resolver().resolve(workspace_root)
    initialized.retriever().retrieve("Where is SERVICE_HOST configured?", context)
    assert len(calls) == 1


def test_the_seed_entities_are_the_ones_that_seeded_expansion(
    initialized: Workspace, workspace_root: Path
) -> None:
    outcome = _outcome(initialized, "Where is SERVICE_HOST configured?", workspace_root)
    assert outcome.seed_entity_ids
    resolved = initialized.knowledge.keys_for_entities(outcome.seed_entity_ids)
    assert "env:SERVICE_HOST" in {identity.key for identity in resolved.values()}


# -- the V1 contract did not move ---------------------------------------------------


def test_evidence_set_gained_no_field() -> None:
    """``EvidenceSet`` is what ``--json`` emits, so its shape is frozen.

    Asserted as the field set rather than a stored schema dump: this is the
    invariant, and it cannot be satisfied by regenerating a golden file.
    """
    assert set(EvidenceSet.model_fields) == {
        "question",
        "candidates",
        "scope",
        "primary_repository",
        "truncated",
        "trace",
    }


def test_the_evidence_set_schema_names_nothing_from_the_outcome() -> None:
    schema = EvidenceSet.model_json_schema()
    assert "RetrievalOutcome" not in schema["$defs"]
    assert "SuppressionOutcome" not in schema["$defs"]
