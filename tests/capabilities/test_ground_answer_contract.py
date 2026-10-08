"""Characterization of V1 ``ground_answer`` — the contract Slice H builds on.

These tests describe what the existing algorithm **does**. They do not propose
what it should do, and none of them is allowed to become a reason to change it:
``ground_answer`` is the one algorithm in RepoAtlas that is adversarially tested
against a model that is actively trying to get a fabrication past it, and the V2
capability layer earns nothing by rewriting it.

Four of these pin assumptions the frozen V2 architecture made *before* anyone had
measured them, and each would have been a reason to stop:

* the ledger keeps digests rather than source text, which is only safe if
  grounding never reads ``EvidenceCandidate.text``;
* V2 evidence ids must map onto a function that has none, which is only safe if
  citations resolve by coordinate rather than by position;
* a citation may narrow to a sub-range, which is what lets an agent cite three
  lines of a supplied twenty;
* one bad citation condemns its whole claim, which is why the adapter may not
  quietly drop the bad one and keep the claim.

Each is named by the test below that measures it.

The surprising one is :func:`test_a_citation_reaching_past_the_supplied_span_is_narrowed`.
Partial overlap is **clamped, not refused**: the claim survives carrying the part
that was supplied, and its confidence is capped because the model chose that
number before the support shrank. Slice H reports such a citation as a finding and
refuses to call it grounded, because the range the caller wrote is not the range
that checks out — but it does not refuse the claim, because V1 does not.
"""

from __future__ import annotations

import pytest
from repoatlas.grounding import INSUFFICIENT_EVIDENCE, ground_answer
from repoatlas.models.evidence import (
    Evidence,
    EvidenceCandidate,
    EvidenceSet,
    Hypothesis,
    RepoAtlasAnswer,
)

SLUG = "teams/service"
PATH = "src/config.py"


def _supplied(
    *,
    slug: str = SLUG,
    path: str = PATH,
    start: int | None = 20,
    end: int | None = 30,
    text: str = "HOST = os.environ['SERVICE_HOST']",
) -> EvidenceCandidate:
    return EvidenceCandidate(
        repository="service",
        repository_slug=slug,
        path=path,
        line_start=start,
        line_end=end,
        text=text,
        description="",
    )


def _cite(
    *,
    slug: str | None = SLUG,
    path: str = PATH,
    start: int | None = None,
    end: int | None = None,
) -> Evidence:
    return Evidence(
        repository="service",
        repository_slug=slug,
        path=path,
        line_start=start,
        line_end=end,
        description="",
    )


def _answer(
    *evidence: Evidence,
    statement: str = "the host is read from the environment",
    confidence: float = 0.9,
    summary: str = "the host comes from the environment",
) -> RepoAtlasAnswer:
    return RepoAtlasAnswer(
        summary=summary,
        hypotheses=[
            Hypothesis(statement=statement, confidence=confidence, evidence=list(evidence))
        ],
    )


def _set(*candidates: EvidenceCandidate, question: str = "where is the host set?") -> EvidenceSet:
    return EvidenceSet(question=question, candidates=candidates)


# ---------------------------------------------------------------------------
# A. Supplied text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["HOST = os.environ['SERVICE_HOST']", "", "bytes that were never in any file"],
    ids=["real", "empty", "wrong"],
)
def test_supplied_text_is_never_read(text: str) -> None:
    """Grounding resolves provenance, so the bytes behind it do not change a verdict.

    This is the measurement the V2 ledger's design rests on. The ledger retains a
    digest and a bounded excerpt rather than the source text, which would be an
    unsound basis for verification if grounding consulted the text — the verifier
    would be checking citations against evidence it had to invent. It does not, so
    the ledger does not have to keep bytes it would only ever be tempted to serve.
    """
    report = ground_answer(_answer(_cite(start=22, end=25)), _set(_supplied(text=text)))

    assert len(report.answer.hypotheses) == 1
    assert report.dropped_citations == 0
    assert report.clamped_citations == 0


def test_identical_provenance_with_different_text_grounds_identically() -> None:
    """The whole report, not only the verdict, is independent of the text."""
    with_text = ground_answer(_answer(_cite(start=22, end=25)), _set(_supplied(text="a" * 400)))
    without = ground_answer(_answer(_cite(start=22, end=25)), _set(_supplied(text="")))

    assert with_text == without


# ---------------------------------------------------------------------------
# B. How a citation names its source
# ---------------------------------------------------------------------------


def test_citations_resolve_by_coordinate_not_position() -> None:
    """There is no ``E<n>`` here, and therefore nothing that can be renumbered.

    V1 shows the model ``slug/path:start-end`` and takes coordinates back. A
    citation carries no index into the supplied list, so removing a candidate
    cannot shift another one's identity onto it — the hazard V2 §19 names does not
    exist inside this function, and Slice H's job is to make sure it does not
    reappear in the adapter that puts ids on top.
    """
    fields = set(Evidence.model_fields)

    assert "evidence_id" not in fields
    assert "id" not in fields
    assert "index" not in fields
    assert {"repository", "repository_slug", "path", "line_start", "line_end"} <= fields

    # Reordering the supplied candidates changes nothing, because nothing is
    # addressed by position.
    first = _supplied(start=20, end=30)
    second = _supplied(path="src/other.py", start=1, end=5)
    forwards = ground_answer(_answer(_cite(start=22, end=25)), _set(first, second))
    backwards = ground_answer(_answer(_cite(start=22, end=25)), _set(second, first))

    assert forwards == backwards


def test_a_citation_naming_another_repository_does_not_resolve() -> None:
    """Identity is the slug. The same path in another repository is another file."""
    report = ground_answer(_answer(_cite(slug="teams/other", start=22, end=25)), _set(_supplied()))

    assert report.answer.hypotheses == []
    assert report.dropped_citations == 1


def test_a_citation_naming_an_unsupplied_path_does_not_resolve() -> None:
    report = ground_answer(
        _answer(_cite(path="src/elsewhere.py", start=1, end=2)), _set(_supplied())
    )

    assert report.answer.hypotheses == []
    assert report.dropped_citations == 1


# ---------------------------------------------------------------------------
# C. Line ranges
# ---------------------------------------------------------------------------


def test_a_subrange_of_a_supplied_span_resolves() -> None:
    """Citing three lines of a supplied twenty is honest and is accepted unchanged."""
    report = ground_answer(_answer(_cite(start=22, end=25)), _set(_supplied(start=20, end=30)))

    kept = report.answer.hypotheses[0].evidence[0]
    assert (kept.line_start, kept.line_end) == (22, 25)
    assert report.clamped_citations == 0
    assert report.answer.hypotheses[0].confidence == 0.9


def test_the_exact_supplied_span_resolves_unchanged() -> None:
    report = ground_answer(_answer(_cite(start=20, end=30)), _set(_supplied(start=20, end=30)))

    kept = report.answer.hypotheses[0].evidence[0]
    assert (kept.line_start, kept.line_end) == (20, 30)
    assert report.clamped_citations == 0


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [(19, 25, (20, 25)), (24, 35, (24, 30)), (1, 99, (20, 30))],
    ids=["reaches before", "reaches after", "swallows the file"],
)
def test_a_citation_reaching_past_the_supplied_span_is_narrowed(
    start: int, end: int, expected: tuple[int, int]
) -> None:
    """Partial overlap is clamped to what was supplied, and confidence is capped.

    The characterization that most shapes Slice H. The claim is **not** refused —
    it survives carrying only the lines that were actually shown — so the verifier
    may not refuse it either. What the verifier adds is that the narrowing is
    *reported*: a caller who wrote 19-25 and got 20-25 asked about a line nobody
    supplied, and telling them the citation is grounded would be telling them the
    range they wrote checks out.
    """
    report = ground_answer(_answer(_cite(start=start, end=end)), _set(_supplied(start=20, end=30)))

    kept = report.answer.hypotheses[0].evidence[0]
    assert (kept.line_start, kept.line_end) == expected
    assert report.clamped_citations == 1
    assert report.answer.hypotheses[0].confidence == 0.5


def test_a_confidence_below_the_ceiling_is_not_raised_by_narrowing() -> None:
    """Grounding only ever lowers. Narrowing cannot make a modest claim bolder."""
    report = ground_answer(
        _answer(_cite(start=19, end=25), confidence=0.2), _set(_supplied(start=20, end=30))
    )

    assert report.answer.hypotheses[0].confidence == 0.2


@pytest.mark.parametrize(("start", "end"), [(1, 10), (40, 50)], ids=["before", "after"])
def test_a_citation_disjoint_from_every_supplied_span_is_dropped(start: int, end: int) -> None:
    report = ground_answer(_answer(_cite(start=start, end=end)), _set(_supplied(start=20, end=30)))

    assert report.answer.hypotheses == []
    assert report.dropped_citations == 1


def test_adjacent_supplied_spans_are_not_bridged() -> None:
    """Two supplied spans do not make the gap between them citable.

    Lines 10-12 and 15-17 were supplied; 13 and 14 were not. A citation to 10-17
    is narrowed to the first span it overlaps, so the surviving citation covers
    only lines that were shown — the bridge is never asserted.
    """
    report = ground_answer(
        _answer(_cite(start=10, end=17)),
        _set(_supplied(start=10, end=12), _supplied(start=15, end=17)),
    )

    kept = report.answer.hypotheses[0].evidence[0]
    assert (kept.line_start, kept.line_end) == (10, 12)
    assert 13 not in range(kept.line_start or 0, (kept.line_end or 0) + 1)
    assert 14 not in range(kept.line_start or 0, (kept.line_end or 0) + 1)


# ---------------------------------------------------------------------------
# File-identity evidence (V2 §23)
# ---------------------------------------------------------------------------


def test_file_identity_evidence_grounds_a_file_level_citation() -> None:
    """A filename match proves the file exists, and a file-level citation says that."""
    report = ground_answer(
        _answer(_cite(start=None, end=None)), _set(_supplied(start=None, end=None, text=""))
    )

    assert len(report.answer.hypotheses) == 1
    assert report.dropped_citations == 0


def test_file_identity_evidence_cannot_ground_a_line_citation() -> None:
    """No line was supplied, so no line may be attributed — and none is invented."""
    report = ground_answer(
        _answer(_cite(start=5, end=6)), _set(_supplied(start=None, end=None, text=""))
    )

    assert report.answer.hypotheses == []
    assert report.dropped_citations == 1


# ---------------------------------------------------------------------------
# D/E. Unknown citations and partial grounding
# ---------------------------------------------------------------------------


def test_one_unresolvable_citation_condemns_its_claim() -> None:
    """Rule 1: **every** citation must resolve, not one of them.

    A claim referencing a file it was never shown is not made trustworthy by also
    referencing one it was. Slice H may therefore not drop the bad citation and
    keep the claim — it routes the claim into this same branch instead.
    """
    report = ground_answer(
        _answer(_cite(start=22, end=25), _cite(path="src/invented.py", start=1, end=2)),
        _set(_supplied()),
    )

    assert report.answer.hypotheses == []
    assert report.dropped_citations == 1
    assert report.demoted_findings == 1


def test_a_claim_with_no_citation_is_dropped() -> None:
    report = ground_answer(
        RepoAtlasAnswer(
            summary="s", hypotheses=[Hypothesis(statement="x", confidence=0.9, evidence=[])]
        ),
        _set(_supplied()),
    )

    assert report.answer.hypotheses == []
    assert report.demoted_findings == 1


def test_grounding_is_decided_per_claim() -> None:
    """A bad claim does not take a good one down with it."""
    report = ground_answer(
        RepoAtlasAnswer(
            summary="s",
            hypotheses=[
                Hypothesis(
                    statement="grounded", confidence=0.9, evidence=[_cite(start=22, end=25)]
                ),
                Hypothesis(
                    statement="fabricated",
                    confidence=0.9,
                    evidence=[_cite(path="src/invented.py", start=1, end=2)],
                ),
            ],
        ),
        _set(_supplied()),
    )

    assert [h.statement for h in report.answer.hypotheses] == ["grounded"]
    assert report.demoted_findings == 1


# ---------------------------------------------------------------------------
# F. Sanitization
# ---------------------------------------------------------------------------


def test_a_summary_naming_an_unsupplied_file_is_replaced_not_edited() -> None:
    """Removing a clause would leave a sentence nobody wrote and nobody checked."""
    report = ground_answer(
        _answer(_cite(start=22, end=25), summary="the value comes from infra/secrets.yaml"),
        _set(_supplied()),
    )

    assert "secrets.yaml" not in report.answer.summary
    assert report.answer.summary != "the value comes from infra/secrets.yaml"
    assert report.unsupported


def test_when_no_claim_survives_the_summary_becomes_the_sentinel() -> None:
    report = ground_answer(
        _answer(_cite(path="src/invented.py", start=1, end=2)), _set(_supplied())
    )

    assert report.answer.summary == INSUFFICIENT_EVIDENCE


def test_an_unknown_naming_an_unsupplied_file_is_removed() -> None:
    """The field whose purpose is admitting ignorance may not smuggle in a file."""
    answer = _answer(_cite(start=22, end=25))
    answer = answer.model_copy(
        update={"unknowns": ["prod/override.env may set it", "the runtime value is unknown"]}
    )
    report = ground_answer(answer, _set(_supplied()))

    assert "the runtime value is unknown" in report.answer.unknowns
    assert not any("override.env" in unknown for unknown in report.answer.unknowns)


def test_a_next_check_presupposing_an_unsupplied_file_is_removed() -> None:
    answer = _answer(_cite(start=22, end=25)).model_copy(
        update={"next_check": "open infra/secrets.yaml and rotate it"}
    )
    report = ground_answer(answer, _set(_supplied()))

    assert report.answer.next_check is None


def test_removals_are_counted_and_never_quoted_back() -> None:
    """The count is the honest signal; reprinting the path would restore it."""
    report = ground_answer(
        _answer(_cite(start=22, end=25), summary="see infra/secrets.yaml"), _set(_supplied())
    )

    assert any("were removed" in unknown for unknown in report.answer.unknowns)
    assert not any("secrets.yaml" in unknown for unknown in report.answer.unknowns)


def test_a_word_in_the_question_is_not_treated_as_a_fabrication() -> None:
    """The user introduced it; echoing it back is not the model inventing a file."""
    report = ground_answer(
        _answer(_cite(start=22, end=25), summary="nothing in deploy/prod.yaml sets it"),
        _set(_supplied(), question="does deploy/prod.yaml set the host?"),
    )

    assert report.answer.summary == "nothing in deploy/prod.yaml sets it"


@pytest.mark.parametrize(
    ("statement", "kept"),
    [
        ("./src/config.py reads the host", True),
        ("././src/config.py reads the host", True),
        ("../src/config.py reads the host", False),
        ("./src/other.py reads the host", False),
        ("./config.py reads the host", True),
    ],
    ids=["dot-slash", "repeated-dot-slash", "parent", "unsupplied", "basename"],
)
def test_a_supplied_file_named_relative_to_the_root_is_not_a_fabrication(
    statement: str, kept: bool
) -> None:
    """``./`` restates the path; ``../`` names somewhere else and stays checked."""
    report = ground_answer(_answer(_cite(start=22, end=25), statement=statement), _set(_supplied()))

    assert bool(report.answer.hypotheses) is kept


@pytest.mark.parametrize(
    ("statement", "kept"),
    [
        ("other.graphql defines the phase", False),
        ("other.graphqls defines the phase", False),
        ("the queries in listings.gql select it", False),
        ("schema.prisma maps the column", False),
        ("api/schema.graphql defines the phase", True),
        ("schema.graphql defines the phase", True),
    ],
    ids=["graphql", "graphqls", "gql", "prisma", "supplied", "supplied-basename"],
)
def test_a_schema_file_the_answer_was_never_shown_is_a_fabrication(
    statement: str, kept: bool
) -> None:
    """A bare schema file name is a path, so prose naming one is checked like any other
    (the supplied file's own basename included, and accepted)."""
    supplied = _supplied(path="api/schema.graphql", text="enum Phase { DRAFT }")
    report = ground_answer(
        _answer(_cite(path="api/schema.graphql", start=22, end=25), statement=statement),
        _set(supplied),
    )

    assert bool(report.answer.hypotheses) is kept
