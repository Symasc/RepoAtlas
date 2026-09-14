"""The answer trust boundary (V1_SPEC §15, ARCHITECTURE.md §11).

Citation verification alone was never the whole guarantee. It checks the typed
`Evidence` objects an answer attaches to a claim — and every other field of the
answer is free text that reached the user unexamined. One valid citation was
enough to carry an entire fabricated statement, and the `ask` renderer printed
that statement under a heading called EVIDENCE.

The rule these tests enforce:

    A citation validates the evidence reference. It does not validate arbitrary
    prose attached to that citation.

The rule is enforced in one place and asserted here field by field: an
unsupported repository reference cannot survive validation through *any* field
of the answer --- summary, statement, unknowns or next check --- and prose never
becomes a citation.

Two of the three layers this suite used to assert are gone with the embedded
reasoner, and neither took a guarantee with it. Serialization was about whether
repository text could forge prompt structure; there is no prompt. Rendering was
about whether model prose could appear under an EVIDENCE heading; there is no
answer renderer, and an agent renders its own conclusions. What is left is the
layer that was always doing the work, asked directly: what survives
:func:`~repoatlas.grounding.ground_answer`, and what it points at afterwards.
"""

from __future__ import annotations

import pytest
from repoatlas.grounding import INSUFFICIENT_EVIDENCE, GroundingReport, ground_answer
from repoatlas.models.evidence import (
    Evidence,
    EvidenceCandidate,
    EvidenceSet,
    Hypothesis,
    RepoAtlasAnswer,
)

pytestmark = pytest.mark.adversarial


# --- fixtures -----------------------------------------------------------------


def candidate(
    repository: str = "svc",
    path: str = "app.py",
    start: int = 1,
    end: int = 2,
    text: str = 'import os\nHOST = os.environ["SERVICE_HOST"]',
    description: str = "reads SERVICE_HOST",
) -> EvidenceCandidate:
    return EvidenceCandidate(
        repository=repository,
        path=path,
        line_start=start,
        line_end=end,
        text=text,
        description=description,
    )


def supplied(
    *candidates: EvidenceCandidate, question: str = "why does the service fail"
) -> EvidenceSet:
    return EvidenceSet(
        question=question,
        primary_repository="svc",
        candidates=candidates or (candidate(),),
    )


def real_citation() -> Evidence:
    return Evidence(
        repository="svc", path="app.py", line_start=1, line_end=2, description="the real one"
    )


def answer(
    summary: str = "The service reads its host from the environment.",
    statement: str | None = None,
    unknowns: list[str] | None = None,
    next_check: str | None = None,
    confidence: float = 0.9,
    evidence: list[Evidence] | None = None,
) -> RepoAtlasAnswer:
    hypotheses = []
    if statement is not None:
        hypotheses = [
            Hypothesis(
                statement=statement,
                confidence=confidence,
                evidence=evidence if evidence is not None else [real_citation()],
            )
        ]
    return RepoAtlasAnswer(
        summary=summary,
        hypotheses=hypotheses,
        unknowns=unknowns or [],
        next_check=next_check,
    )


#: Every repository object named in the review's reproduction. None was supplied.
FABRICATED = ("infra/secrets.yaml", "prod/db.env", "/etc/shadow")


def cited(report: GroundingReport) -> list[str]:
    """Every location the grounded answer still points at, as ``repo/path``.

    The V1 renderer built a VERIFIED EVIDENCE section from exactly these objects,
    which is why this suite used to read that section. Reading them directly says
    the same thing about one more consumer than the renderer was.
    """
    return [
        f"{item.repository_slug or item.repository}/{item.path}"
        for hypothesis in report.answer.hypotheses
        for item in hypothesis.evidence
    ]


# --- 1. unsupported repository references, field by field ---------------------


@pytest.mark.parametrize(
    "field,build",
    [
        ("summary", lambda t: answer(summary=t, statement="A supported reading.")),
        ("statement", lambda t: answer(statement=t)),
        ("unknowns", lambda t: answer(statement="A supported reading.", unknowns=[t])),
        ("next_check", lambda t: answer(statement="A supported reading.", next_check=t)),
    ],
)
@pytest.mark.parametrize("text", FABRICATED)
def test_an_unsupported_repository_reference_never_survives_any_field(
    field: str, build, text: str
) -> None:
    """Every user-visible free-text field is validated, not only citations."""
    report = ground_answer(build(f"The problem is in {text} and must be fixed."), supplied())

    assert text not in _all_text(report.answer), f"{text} survived in {field}"


def test_a_valid_citation_does_not_launder_a_fabricated_statement() -> None:
    """The review's exact laundering case (V1_SPEC §15)."""
    report = ground_answer(
        answer(
            statement=("infra/secrets.yaml:42 contains an AWS key, and prod/db.env overrides it."),
            evidence=[real_citation()],
        ),
        supplied(),
    )

    assert report.answer.hypotheses == []
    assert "infra/secrets.yaml" not in _all_text(report.answer)
    assert report.unsupported


def test_several_valid_citations_do_not_launder_a_fabricated_statement() -> None:
    """More real citations do not buy more latitude."""
    report = ground_answer(
        answer(
            statement="The credential lives in infra/secrets.yaml.",
            evidence=[
                real_citation(),
                Evidence(
                    repository="svc",
                    path="settings.py",
                    line_start=3,
                    line_end=4,
                    description="also real",
                ),
            ],
        ),
        supplied(candidate(), candidate(path="settings.py", start=3, end=4, text="X = 1")),
    )

    assert report.answer.hypotheses == []


def test_one_fabricated_citation_beside_real_ones_sinks_the_hypothesis() -> None:
    """A claim that references something it was never shown is not trustworthy.

    Previously the fabricated citation was dropped and the statement kept, which
    is precisely the laundering this pass removes.
    """
    report = ground_answer(
        answer(
            statement="Startup order explains the failure.",
            evidence=[
                real_citation(),
                Evidence(
                    repository="infra",
                    path="secrets.yaml",
                    line_start=42,
                    line_end=42,
                    description="invented",
                ),
            ],
        ),
        supplied(),
    )

    assert report.answer.hypotheses == []
    assert report.dropped_citations == 1


# --- 2. fabrications that carry no path syntax --------------------------------
#
# These cannot be detected by inspecting the prose, so the guarantee for them is
# structural: they are labelled inference and can never appear as evidence.


@pytest.mark.parametrize(
    "claim",
    [
        "The rotate_credentials helper clears the cached token.",
        "urllib3 is pinned to 2.5.0, which is the incompatible release.",
        "PROD_DB_PASSWORD is read during startup.",
        "The production credential is hardcoded in the deployment configuration.",
        "The infra repository configures this value.",
    ],
)
def test_a_claim_without_path_syntax_is_still_never_presented_as_evidence(claim: str) -> None:
    """Unsupported inference stays a claim, since it cannot be detected as false.

    RepoAtlas cannot tell whether ``rotate_credentials`` clears a cached token.
    What it can guarantee is that saying so never turns into a citation: the
    statement survives as a hypothesis attached to the one real reference, and
    the set of things the answer points at is unchanged by what the prose says.
    """
    evidence = supplied()
    report = ground_answer(answer(statement=claim), evidence)

    assert cited(report) == ["svc/app.py"]
    if report.answer.hypotheses:
        assert report.answer.hypotheses[0].statement == claim
        assert all(claim not in item.description for item in report.answer.hypotheses[0].evidence)


# --- 3. what the answer is allowed to point at --------------------------------


def test_the_surviving_citations_are_only_retrieved_locations() -> None:
    """Structural: the references come from the EvidenceSet, not from the answer."""
    evidence = supplied()
    report = ground_answer(answer(statement="Something plausible."), evidence)

    assert cited(report) == ["svc/app.py"]
    (item,) = report.answer.hypotheses[0].evidence
    assert (item.line_start, item.line_end) == (1, 2)


def test_no_free_text_field_can_become_a_citation() -> None:
    """Every free-text field at once, with a marker that must not appear.

    Summary, statement, unknowns and next check are all prose. None of them is a
    reference, and none may end up in one --- the typed :class:`Evidence` objects
    are the only thing a citation is built from.
    """
    marker = "MODEL_PROSE_MARKER_5150"
    evidence = supplied()
    report = ground_answer(
        answer(
            summary=marker,
            statement=marker,
            unknowns=[marker],
            next_check=marker,
        ),
        evidence,
    )

    for hypothesis in report.answer.hypotheses:
        for item in hypothesis.evidence:
            assert marker not in item.path
            assert marker not in item.repository
            assert marker not in (item.repository_slug or "")
            assert marker not in item.description


# --- 4. confidence cannot outlive its support ---------------------------------


def test_confidence_does_not_survive_the_claim_it_belonged_to() -> None:
    """A named-but-unsupplied path sinks the claim, and takes its 1.0 with it."""
    evidence = supplied()
    report = ground_answer(
        answer(
            statement="infra/secrets.yaml holds the key.",
            confidence=1.0,
        ),
        evidence,
    )

    assert report.answer.hypotheses == []
    assert cited(report) == []


def test_a_clamped_citation_cannot_keep_its_original_confidence() -> None:
    """Grounding changed the support, so the model's own number is stale."""
    evidence = supplied(candidate(start=1, end=2))
    report = ground_answer(
        answer(
            statement="Startup reads the host.",
            confidence=1.0,
            evidence=[
                Evidence(
                    repository="svc", path="app.py", line_start=1, line_end=99, description="wide"
                )
            ],
        ),
        evidence,
    )

    assert report.clamped_citations == 1
    assert report.answer.hypotheses[0].confidence < 1.0


# --- 5. the review's exact reproduction ---------------------------------------


def test_the_independent_review_reproduction_is_closed() -> None:
    """Supplied: svc/app.py:1-2 only. Nothing else may be asserted.

    One real citation carrying four fabricated repository objects, one in each
    free-text field. The review's finding was that the citation laundered the
    rest. Asserted against every field of the grounded answer rather than against
    a rendering of it, which is a stronger statement than the original made: the
    fabrications do not reach *any* consumer, not merely the terminal.
    """
    evidence = supplied()
    hostile = RepoAtlasAnswer(
        summary="prod/db.env overrides the credential used at startup.",
        hypotheses=[
            Hypothesis(
                statement=(
                    "infra/secrets.yaml:42 contains an AWS key, and the deployment "
                    "configuration hardcodes the production credential."
                ),
                confidence=0.99,
                evidence=[real_citation()],
            )
        ],
        unknowns=["/etc/shadow may be relevant."],
        next_check="Open infra/secrets.yaml and rotate the hardcoded credential.",
    )

    report = ground_answer(hostile, evidence)

    surviving = _all_text(report.answer)
    for fabricated in FABRICATED:
        assert fabricated not in surviving, f"{fabricated} survived verification"
    assert cited(report) == []


# --- 6. useful reasoning is preserved -----------------------------------------


def test_a_supported_hypothesis_survives_as_a_hypothesis() -> None:
    """Hallucination is not solved by removing the model's ability to reason."""
    evidence = EvidenceSet(
        question="where does SERVICE_HOST come from",
        primary_repository="svc",
        candidates=(
            candidate(
                path="config.py", start=14, end=18, text='SERVICE_HOST = os.getenv("SERVICE_HOST")'
            ),
            candidate(
                repository="deployment",
                path="init.sls",
                start=22,
                end=27,
                text="SERVICE_HOST: backend.internal",
                description="configures SERVICE_HOST",
            ),
        ),
    )
    reasonable = RepoAtlasAnswer(
        summary="The deployment configuration is likely the source of SERVICE_HOST.",
        hypotheses=[
            Hypothesis(
                statement="deployment/init.sls is likely the origin of the value config.py reads.",
                confidence=0.6,
                evidence=[
                    Evidence(
                        repository="svc",
                        path="config.py",
                        line_start=14,
                        line_end=18,
                        description="reads it",
                    ),
                    Evidence(
                        repository="deployment",
                        path="init.sls",
                        line_start=22,
                        line_end=27,
                        description="sets it",
                    ),
                ],
            )
        ],
        unknowns=["The runtime value is not established by indexed evidence."],
        next_check="Inspect the runtime value of SERVICE_HOST before startup.",
    )

    report = ground_answer(reasonable, evidence)

    assert len(report.answer.hypotheses) == 1
    assert report.answer.hypotheses[0].confidence == pytest.approx(0.6)
    assert "likely" in report.answer.hypotheses[0].statement
    assert cited(report) == ["svc/config.py", "deployment/init.sls"]
    assert report.unsupported == ()


def test_inference_language_is_not_upgraded_to_certainty() -> None:
    """Normalization must not remove hedging (V1_SPEC §15)."""
    evidence = supplied()
    statement = "This is possibly caused by startup ordering, which the evidence suggests."
    report = ground_answer(answer(statement=statement), evidence)

    assert report.answer.hypotheses[0].statement == statement


def test_a_path_the_user_named_may_be_echoed_back() -> None:
    """The user introduced the string; repeating it is not model invention."""
    evidence = supplied(question="is infra/secrets.yaml used anywhere?")
    report = ground_answer(
        answer(
            summary="No indexed evidence mentions infra/secrets.yaml.",
            statement="A supported reading.",
        ),
        evidence,
    )

    assert "infra/secrets.yaml" in report.answer.summary


# --- 7. fallbacks ------------------------------------------------------------


def test_no_surviving_hypothesis_yields_insufficient_evidence() -> None:
    report = ground_answer(answer(statement="Everything is in infra/secrets.yaml."), supplied())
    assert report.answer.summary == INSUFFICIENT_EVIDENCE


def test_an_unsupported_summary_is_replaced_rather_than_passed_through() -> None:
    report = ground_answer(
        answer(summary="prod/db.env is the culprit.", statement="A supported reading."),
        supplied(),
    )
    assert "prod/db.env" not in report.answer.summary


def test_one_valid_and_one_invalid_hypothesis_keeps_only_the_valid_one() -> None:
    evidence = supplied()
    mixed = RepoAtlasAnswer(
        summary="Two readings.",
        hypotheses=[
            Hypothesis(
                statement="A supported reading.", confidence=0.7, evidence=[real_citation()]
            ),
            Hypothesis(
                statement="An unsupported one about infra/secrets.yaml.",
                confidence=0.9,
                evidence=[real_citation()],
            ),
        ],
    )

    report = ground_answer(mixed, evidence)

    assert [h.statement for h in report.answer.hypotheses] == ["A supported reading."]
    assert "infra/secrets.yaml" not in "\n".join(report.answer.unknowns)


def test_an_empty_evidence_set_supports_nothing() -> None:
    empty = EvidenceSet(question="q")
    report = ground_answer(answer(statement="Anything at all."), empty)

    assert report.answer.hypotheses == []
    assert report.answer.summary == INSUFFICIENT_EVIDENCE


# --- helpers ------------------------------------------------------------------


def _all_text(result: RepoAtlasAnswer) -> str:
    parts = [result.summary, result.next_check or "", *result.unknowns]
    parts.extend(h.statement for h in result.hypotheses)
    return "\n".join(parts)
