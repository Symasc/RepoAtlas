"""Concise output formats (V1_SPEC §16, §19)."""

from __future__ import annotations

from repoatlas.models.evidence import (
    Evidence,
    EvidenceCandidate,
    EvidenceSet,
    Hypothesis,
    RepoAtlasAnswer,
)
from repoatlas.rendering import (
    confidence_label,
    render_answer,
    render_diagnosis,
    render_evidence,
)


def _answer(**overrides: object) -> RepoAtlasAnswer:
    base: dict[str, object] = {
        "summary": "SERVICE_HOST is populated by the Salt deployment layer.",
        "hypotheses": [
            Hypothesis(
                statement="The Salt state configures SERVICE_HOST.",
                confidence=0.9,
                evidence=[
                    Evidence(
                        repository="deployment",
                        path="service/init.sls",
                        line_start=42,
                        line_end=42,
                        description="configures SERVICE_HOST",
                    )
                ],
            )
        ],
        "unknowns": [],
        "next_check": "Inspect the pillar value used by the Salt state.",
    }
    base.update(overrides)
    return RepoAtlasAnswer.model_validate(base)


def _evidence() -> EvidenceSet:
    return EvidenceSet(
        question="Where is SERVICE_HOST configured?",
        candidates=(
            EvidenceCandidate(
                repository="python-service",
                path="src/config.py",
                line_start=18,
                line_end=18,
                text='host = os.getenv("SERVICE_HOST")',
                description="reads SERVICE_HOST",
            ),
        ),
    )


def test_confidence_labels() -> None:
    assert confidence_label(0.95) == "HIGH"
    assert confidence_label(0.6) == "MEDIUM"
    assert confidence_label(0.1) == "LOW"


def test_answer_has_the_documented_sections() -> None:
    out = render_answer(_answer(), _evidence())
    for section in ("ANSWER", "EVIDENCE", "CONFIDENCE", "NEXT"):
        assert section in out
    assert "HIGH" in out


def test_answer_renders_citations_as_repo_path_line() -> None:
    """Answers must cite locations the user can open (V1_SPEC §11)."""
    assert "deployment/service/init.sls:42" in render_answer(_answer(), _evidence())


def test_answer_renders_a_line_range() -> None:
    answer = _answer(
        hypotheses=[
            Hypothesis(
                statement="s",
                confidence=0.8,
                evidence=[
                    Evidence(
                        repository="deployment",
                        path="service/init.sls",
                        line_start=42,
                        line_end=48,
                        description="d",
                    )
                ],
            )
        ]
    )
    assert "deployment/service/init.sls:42-48" in render_answer(answer, _evidence())


def test_answer_omits_lines_when_the_citation_has_none() -> None:
    answer = _answer(
        hypotheses=[
            Hypothesis(
                statement="s",
                confidence=0.8,
                evidence=[
                    Evidence(repository="deployment", path="service/init.sls", description="d")
                ],
            )
        ]
    )
    out = render_answer(answer, _evidence())
    assert "deployment/service/init.sls" in out
    assert "init.sls:" not in out


def test_answer_shows_unknowns() -> None:
    out = render_answer(_answer(hypotheses=[], unknowns=["the runtime pillar value"]), _evidence())
    assert "UNKNOWN" in out
    assert "the runtime pillar value" in out


def test_insufficient_answer_has_no_confidence_claim() -> None:
    """With nothing supported, there is no confidence to report."""
    out = render_answer(_answer(summary="INSUFFICIENT EVIDENCE", hypotheses=[]), _evidence())
    assert "INSUFFICIENT EVIDENCE" in out
    assert "CONFIDENCE" not in out


def test_answer_is_concise() -> None:
    assert len(render_answer(_answer(), _evidence()).splitlines()) < 25


def test_diagnosis_separates_facts_from_hypotheses() -> None:
    """Facts are retrieved evidence; hypotheses are the model's (V1_SPEC §15)."""
    out = render_diagnosis(_answer(), _evidence())
    assert "VERIFIED EVIDENCE" in out
    assert "HYPOTHESIS" in out
    assert out.index("VERIFIED EVIDENCE") < out.index("HYPOTHESIS")

    verified = out[out.index("VERIFIED EVIDENCE") : out.index("HYPOTHESIS")]
    assert "python-service/src/config.py:18" in verified
    assert "The Salt state configures SERVICE_HOST." not in verified

    hypotheses = out[out.index("HYPOTHESIS") :]
    assert "The Salt state configures SERVICE_HOST." in hypotheses


def test_diagnosis_labels_hypothesis_confidence() -> None:
    out = render_diagnosis(_answer(), _evidence())
    assert "confidence: HIGH (0.90)" in out


def test_diagnosis_orders_hypotheses_by_confidence() -> None:
    answer = _answer(
        hypotheses=[
            Hypothesis(
                statement="weak cause",
                confidence=0.2,
                evidence=[
                    Evidence(repository="deployment", path="service/init.sls", description="d")
                ],
            ),
            Hypothesis(
                statement="strong cause",
                confidence=0.9,
                evidence=[
                    Evidence(repository="deployment", path="service/init.sls", description="d")
                ],
            ),
        ]
    )
    out = render_diagnosis(answer, _evidence())
    assert out.index("strong cause") < out.index("weak cause")


def test_diagnosis_always_has_unknowns_and_next_sections() -> None:
    out = render_diagnosis(_answer(unknowns=["runtime value"]), _evidence())
    assert "UNKNOWN" in out
    assert "NEXT" in out


def test_diagnosis_states_when_no_next_check_is_offered() -> None:
    out = render_diagnosis(_answer(next_check=None), _evidence())
    assert "No further check suggested." in out


def test_evidence_only_rendering_is_unchanged() -> None:
    out = render_evidence(_evidence())
    assert "EVIDENCE" in out
    assert "python-service/src/config.py:18" in out
