"""Concise output formats (V1_SPEC §16, §19).

Most of this module rendered a generated answer: a summary, hypotheses ordered by
confidence, a confidence word, an UNKNOWN section and a suggested next check.
Those renderers went with the embedded reasoner, because a model's prose is the
only thing they ever formatted, and RepoAtlas produces none.

What is left is what a deterministic command prints. The evidence renderer is
here; repositories, status, sync and the dependency inventory are exercised
through the CLI by ``tests/test_cli.py`` and the adversarial scenario suites,
which is where their output actually matters.
"""

from __future__ import annotations

from repoatlas.models.evidence import EvidenceCandidate, EvidenceSet
from repoatlas.rendering import render_evidence


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


def test_evidence_rendering_names_the_location_it_found() -> None:
    out = render_evidence(_evidence())
    assert "EVIDENCE" in out
    assert "python-service/src/config.py:18" in out


def test_evidence_rendering_survives_an_empty_set() -> None:
    """``locate`` answering "nothing matches" is an answer, not an error."""
    out = render_evidence(EvidenceSet(question="ZZZ_NO_SUCH_TOKEN_QQQ"))
    assert isinstance(out, str)
