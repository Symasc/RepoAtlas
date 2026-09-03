"""Reasoning layer: structured output, repair and grounding (V1_SPEC §15, §19).

Every test uses the scripted backend. No network, no Ollama.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from repoatlas.config.settings import LLMSettings
from repoatlas.llm.factory import build_model
from repoatlas.llm.grounding import ground_answer
from repoatlas.llm.null import NullBackend
from repoatlas.llm.ollama import OllamaBackend
from repoatlas.llm.protocol import ModelUnavailable, ReasoningModel
from repoatlas.llm.reasoner import QueryIntent, Reasoner, build_messages, format_evidence
from repoatlas.llm.scripted import ScriptedBackend
from repoatlas.models.evidence import (
    Evidence,
    EvidenceCandidate,
    EvidenceSet,
    Hypothesis,
    RepoAtlasAnswer,
)


def _evidence(*, question: str = "Where is SERVICE_HOST configured?") -> EvidenceSet:
    return EvidenceSet(
        question=question,
        primary_repository="python-service",
        scope="repository",
        candidates=(
            EvidenceCandidate(
                repository="python-service",
                path="src/config.py",
                line_start=16,
                line_end=21,
                text='host = os.getenv("SERVICE_HOST", "localhost")',
                description="reads SERVICE_HOST",
            ),
            EvidenceCandidate(
                repository="deployment",
                path="service/init.sls",
                line_start=40,
                line_end=48,
                text="SERVICE_HOST: {{ pillar['service']['host'] }}",
                description="configures SERVICE_HOST",
            ),
        ),
    )


def _answer_json(**overrides: object) -> str:
    payload: dict[str, object] = {
        "summary": "SERVICE_HOST is set by the deployment layer.",
        "hypotheses": [
            {
                "statement": "The Salt state supplies SERVICE_HOST.",
                "confidence": 0.9,
                "evidence": [
                    {
                        "repository": "deployment",
                        "path": "service/init.sls",
                        "line_start": 42,
                        "line_end": 42,
                        "description": "configures SERVICE_HOST",
                    }
                ],
            }
        ],
        "unknowns": [],
        "next_check": "Inspect the pillar value.",
    }
    payload.update(overrides)
    return json.dumps(payload)


def _reason(responses: list[str], intent: QueryIntent = QueryIntent.ASK):
    backend = ScriptedBackend(responses)
    result = asyncio.run(Reasoner(backend).reason("Where is SERVICE_HOST?", _evidence(), intent))
    return backend, result


# -- protocol conformance ---------------------------------------------------


def test_backends_satisfy_the_protocol() -> None:
    assert isinstance(NullBackend(), ReasoningModel)
    assert isinstance(ScriptedBackend([]), ReasoningModel)
    assert isinstance(OllamaBackend(LLMSettings()), ReasoningModel)


def test_factory_is_configuration_driven() -> None:
    assert isinstance(build_model(LLMSettings(provider="ollama")), OllamaBackend)
    assert isinstance(build_model(LLMSettings(provider="none")), NullBackend)


def test_factory_rejects_unknown_provider() -> None:
    with pytest.raises(ModelUnavailable) as excinfo:
        build_model(LLMSettings(provider="not-a-provider"))
    assert "ollama" in (excinfo.value.detail or "")


def test_ollama_id_includes_the_model() -> None:
    assert OllamaBackend(LLMSettings(model="qwen3:8b")).id == "ollama/qwen3:8b"


def test_null_backend_reports_insufficient_evidence() -> None:
    response = asyncio.run(NullBackend().complete([]))
    answer = RepoAtlasAnswer.model_validate_json(response.content)
    assert answer.summary == "INSUFFICIENT EVIDENCE"
    assert answer.hypotheses == []


# -- prompt construction ----------------------------------------------------


def test_prompt_contains_the_evidence_and_its_provenance() -> None:
    messages = build_messages("Where is SERVICE_HOST?", _evidence(), QueryIntent.ASK)
    user = messages[-1].content
    assert "python-service" in user
    assert "src/config.py" in user
    assert '"start_line": 16' in user
    assert '"end_line": 21' in user
    assert "SERVICE_HOST" in user


def test_prompt_states_the_trust_rule() -> None:
    system = build_messages("q", _evidence(), QueryIntent.ASK)[0].content
    assert "Cite only what appears in the evidence array" in system
    assert "unknown" in system.lower()


def test_prompt_notes_the_primary_repository() -> None:
    user = build_messages("q", _evidence(), QueryIntent.ASK)[-1].content
    assert "'python-service'" in user


def test_debug_prompt_differs_from_ask() -> None:
    ask = build_messages("q", _evidence(), QueryIntent.ASK)[-1].content
    debug = build_messages("q", _evidence(), QueryIntent.DEBUG)[-1].content
    assert ask != debug
    assert "Symptom under investigation" in debug
    assert "root cause" in debug


def test_empty_evidence_is_stated_not_hidden() -> None:
    empty = EvidenceSet(question="q")
    assert '"evidence": []' in format_evidence(empty)


def test_schema_is_passed_to_the_backend() -> None:
    """Constrained decoding, so the model emits the schema rather than prose."""
    backend, _ = _reason([_answer_json()])
    assert backend.schemas[0] is RepoAtlasAnswer


# -- structured output ------------------------------------------------------


def test_valid_structured_output_is_accepted() -> None:
    _, result = _reason([_answer_json()])
    assert result.answer.summary == "SERVICE_HOST is set by the deployment layer."
    assert len(result.answer.hypotheses) == 1
    assert not result.repaired


def test_output_wrapped_in_a_markdown_fence_is_recovered() -> None:
    fenced = f"Here you go:\n```json\n{_answer_json()}\n```\n"
    _, result = _reason([fenced])
    assert result.answer.hypotheses
    assert not result.repaired


def test_invalid_output_triggers_one_repair_attempt() -> None:
    backend, result = _reason(["not json at all", _answer_json()])
    assert result.repaired
    assert result.answer.hypotheses
    assert len(backend.calls) == 2
    assert "could not be parsed" in backend.calls[1][-1].content


def test_two_failures_yield_insufficient_evidence_not_garbage() -> None:
    _, result = _reason(["not json", "still not json"])
    assert result.answer.summary == "INSUFFICIENT EVIDENCE"
    assert result.answer.hypotheses == []
    assert result.is_insufficient


def test_schema_violation_is_repaired() -> None:
    """Confidence above 1.0 violates the model, so it must not be rendered."""
    bad = _answer_json(hypotheses=[{"statement": "x", "confidence": 5.0, "evidence": []}])
    _, result = _reason([bad, _answer_json()])
    assert result.repaired
    assert result.answer.hypotheses[0].confidence == 0.9


def test_backend_failure_during_repair_is_survived() -> None:
    _, result = _reason(["not json"])
    assert result.answer.summary == "INSUFFICIENT EVIDENCE"


# -- citation verification (the enforcement point) --------------------------


def test_valid_citation_survives_verification() -> None:
    answer = RepoAtlasAnswer(
        summary="s",
        hypotheses=[
            Hypothesis(
                statement="reads it",
                confidence=0.9,
                evidence=[
                    Evidence(
                        repository="python-service",
                        path="src/config.py",
                        line_start=18,
                        line_end=18,
                        description="d",
                    )
                ],
            )
        ],
    )
    report = ground_answer(answer, _evidence())
    assert report.dropped_citations == 0
    assert report.demoted_findings == 0
    assert report.answer.hypotheses[0].evidence[0].line_start == 18


def test_fabricated_path_is_dropped() -> None:
    """A file the model was never shown cannot be cited."""
    answer = RepoAtlasAnswer(
        summary="s",
        hypotheses=[
            Hypothesis(
                statement="invented",
                confidence=0.9,
                evidence=[
                    Evidence(
                        repository="python-service",
                        path="src/totally_invented.py",
                        line_start=1,
                        line_end=1,
                        description="d",
                    )
                ],
            )
        ],
    )
    report = ground_answer(answer, _evidence())
    assert report.dropped_citations == 1
    assert report.demoted_findings == 1
    assert report.answer.hypotheses == []
    assert report.answer.summary == "INSUFFICIENT EVIDENCE"


def test_fabricated_repository_is_dropped() -> None:
    answer = RepoAtlasAnswer(
        summary="s",
        hypotheses=[
            Hypothesis(
                statement="invented repo",
                confidence=0.9,
                evidence=[
                    Evidence(
                        repository="no-such-repo",
                        path="src/config.py",
                        line_start=16,
                        line_end=16,
                        description="d",
                    )
                ],
            )
        ],
    )
    report = ground_answer(answer, _evidence())
    assert report.dropped_citations == 1


def test_line_range_outside_the_supplied_span_is_clamped() -> None:
    answer = RepoAtlasAnswer(
        summary="s",
        hypotheses=[
            Hypothesis(
                statement="overshoots",
                confidence=0.8,
                evidence=[
                    Evidence(
                        repository="python-service",
                        path="src/config.py",
                        line_start=10,
                        line_end=900,
                        description="d",
                    )
                ],
            )
        ],
    )
    report = ground_answer(answer, _evidence())
    assert report.clamped_citations == 1
    cited = report.answer.hypotheses[0].evidence[0]
    assert (cited.line_start, cited.line_end) == (16, 21)


def test_disjoint_line_range_is_dropped_not_clamped() -> None:
    """Pointing at a part of the file that was never shown is fabrication."""
    answer = RepoAtlasAnswer(
        summary="s",
        hypotheses=[
            Hypothesis(
                statement="elsewhere in the file",
                confidence=0.8,
                evidence=[
                    Evidence(
                        repository="python-service",
                        path="src/config.py",
                        line_start=400,
                        line_end=410,
                        description="d",
                    )
                ],
            )
        ],
    )
    report = ground_answer(answer, _evidence())
    assert report.dropped_citations == 1
    assert report.clamped_citations == 0


def test_file_level_citation_without_lines_is_kept() -> None:
    answer = RepoAtlasAnswer(
        summary="s",
        hypotheses=[
            Hypothesis(
                statement="in this file",
                confidence=0.7,
                evidence=[
                    Evidence(repository="deployment", path="service/init.sls", description="d")
                ],
            )
        ],
    )
    report = ground_answer(answer, _evidence())
    assert report.dropped_citations == 0
    assert report.answer.hypotheses[0].evidence[0].line_start is None


def test_a_partly_fabricated_hypothesis_is_dropped_whole() -> None:
    """One fabricated citation sinks the statement it was attached to.

    The old behavior kept the statement and quietly discarded the bad citation,
    which is the laundering V1_SPEC §15 forbids: a claim that references a file
    it was never shown is not made trustworthy by also referencing one it was.
    """
    answer = RepoAtlasAnswer(
        summary="s",
        hypotheses=[
            Hypothesis(
                statement="mixed",
                confidence=0.8,
                evidence=[
                    Evidence(
                        repository="python-service",
                        path="src/config.py",
                        line_start=17,
                        line_end=17,
                        description="real",
                    ),
                    Evidence(
                        repository="python-service",
                        path="src/ghost.py",
                        line_start=1,
                        line_end=1,
                        description="fake",
                    ),
                ],
            )
        ],
    )
    report = ground_answer(answer, _evidence())
    assert report.dropped_citations == 1
    assert report.demoted_findings == 1
    assert report.answer.hypotheses == []
    assert report.unsupported == ("mixed",)


def test_demoted_finding_is_recorded_as_an_unknown() -> None:
    answer = RepoAtlasAnswer(
        summary="s",
        hypotheses=[
            Hypothesis(
                statement="The database driver is misconfigured.",
                confidence=0.9,
                evidence=[Evidence(repository="ghost", path="a.py", description="d")],
            )
        ],
    )
    report = ground_answer(answer, _evidence())
    # The removal is reported as a count, never by quoting the claim back: the
    # unsupported text is diagnostic output, not something to reprint at the user.
    assert any("removed" in u for u in report.answer.unknowns)
    assert "misconfigured" not in " ".join(report.answer.unknowns)
    assert report.unsupported == ("The database driver is misconfigured.",)


def test_existing_unknowns_are_preserved() -> None:
    answer = RepoAtlasAnswer(summary="s", hypotheses=[], unknowns=["runtime value"])
    report = ground_answer(answer, _evidence())
    assert "runtime value" in report.answer.unknowns


def test_grounding_report_flags_modification() -> None:
    clean = RepoAtlasAnswer(summary="s", hypotheses=[])
    assert not ground_answer(clean, _evidence()).modified


def test_end_to_end_fabrication_never_reaches_the_answer() -> None:
    """The whole point: a model that invents a citation cannot mislead."""
    fabricated = _answer_json(
        hypotheses=[
            {
                "statement": "It comes from config/secrets.yaml.",
                "confidence": 0.95,
                "evidence": [
                    {
                        "repository": "python-service",
                        "path": "config/secrets.yaml",
                        "line_start": 3,
                        "line_end": 3,
                        "description": "invented",
                    }
                ],
            }
        ]
    )
    _, result = _reason([fabricated])
    assert result.answer.hypotheses == []
    assert result.answer.summary == "INSUFFICIENT EVIDENCE"
    assert result.grounding.dropped_citations == 1
    rendered = json.dumps(result.answer.model_dump())
    assert "secrets.yaml" not in rendered or "Unsupported" in rendered
