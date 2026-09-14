"""A piece of a cut span may not claim the lines it carries (RC audit, RCA-01).

The character cap cuts a source span into pieces, and every piece carries the
*whole span's* line range (ARCHITECTURE.md §6). That is truthful at chunking's
granularity and false at citation's: a piece other than a whole one begins or
ends mid-line, so its text covers fewer lines than its metadata advertises.

:func:`~repoatlas.capabilities.indexed_source.indexed_line_map` has always
refused those pieces. The retrieval route did not, because the fact that a span
was cut stops at :class:`ChunkHit` and never reached the candidate the shaper
sees. The consequence was not a cosmetic one: search supplied a fragment of a
minified bundle, the evidence claimed the entire line, and a citation asserting
something about the unsupplied remainder verified as ``grounded``.

Two tests are needed and neither subsumes the other, which is exactly why the
defect survived:

* *was anything cut?* --- answerable only from the store's provenance;
* *does the text line up with the range?* --- answerable from the text.

The count test looks like it covers both and does not. A one-line span cut into
pieces gives every piece one claimed line and, whenever the piece holds no
newline, a piece count of one. Both numbers agree and the text still covers a
fraction of the line it names.
"""

from __future__ import annotations

import pytest
from repoatlas.capabilities.evidence_shaping import provable_span, supply
from repoatlas.capabilities.search import provable_line_span, shape_candidates
from repoatlas.models.capability import (
    AppliedBudget,
    CapabilityContext,
    CapabilityScope,
    RetrievalBreadth,
)
from repoatlas.models.evidence import EvidenceCandidate, EvidenceSource, EvidenceTier, MatchClass

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)


def _budget(
    *, max_items: int = 50, max_total_chars: int = 100_000, max_item_chars: int = 100_000
) -> AppliedBudget:
    return AppliedBudget(
        max_items=max_items,
        max_total_chars=max_total_chars,
        max_item_chars=max_item_chars,
        breadth=RetrievalBreadth.STANDARD,
    )


def _candidate(
    *,
    line_start: int | None = 1,
    line_end: int | None = 1,
    text: str = "x" * 800,
    split_total: int | None = None,
) -> EvidenceCandidate:
    return EvidenceCandidate(
        repository="svc",
        repository_slug="svc",
        path="bundle.js",
        line_start=line_start,
        line_end=line_end,
        text=text,
        source=EvidenceSource.EXACT,
        tier=EvidenceTier.DETERMINISTIC,
        match=MatchClass.EXACT_CONTENT,
        split_total=split_total,
    )


# ---------------------------------------------------------------------------
# The rule, stated on the arithmetic
# ---------------------------------------------------------------------------


def test_the_count_test_alone_admits_a_cut_one_line_span() -> None:
    """The defect, isolated: without the cut provenance the arithmetic agrees.

    This is not a hypothetical shape. A minified bundle is one line, it exceeds
    the cap, and every piece of it holds no newline at all.
    """
    fragment = "x" * 8000
    assert provable_span(fragment, 1, 1, split_total=None) == (1, 1)
    assert provable_span(fragment, 1, 1, split_total=24) is None


def test_the_count_test_alone_admits_a_cut_multi_line_span() -> None:
    """And the one-line case is not the only one.

    A two-line span cut just after its first newline leaves a piece holding one
    complete line and a prefix of the next. It splits into two pieces against a
    two-line claim, so the count agrees with itself while line two is a fragment.
    """
    piece = "complete line one\npartial prefix of line"
    assert len(piece.split("\n")) == 2
    assert provable_span(piece, 1, 2, split_total=None) == (1, 2)
    assert provable_span(piece, 1, 2, split_total=2) is None


def test_a_whole_chunk_still_proves_its_span() -> None:
    """The correction narrows nothing that was ever provable."""
    assert provable_span("one\ntwo\nthree", 10, 12, split_total=1) == (10, 12)


def test_text_that_never_came_from_a_chunk_is_judged_exactly_as_before() -> None:
    """``None`` is not ``1``.

    An entity signature was not cut because it was never chunked. Claiming
    ``split_total=1`` for it would assert a completeness no chunker vouched for,
    so the count test remains the whole answer for it — and still refuses a
    one-line signature against a three-line declaration.
    """
    assert provable_span("def f():", 15, 17, split_total=None) is None
    assert provable_span("only line", 4, 4, split_total=None) == (4, 4)


def test_supply_carries_the_cut_provenance_into_the_span_it_returns() -> None:
    piece = supply("x" * 500, 1, 1, limit=100_000, split_total=7)
    assert piece is not None
    assert piece.text == "x" * 500
    assert (piece.line_start, piece.line_end) == (None, None)


# ---------------------------------------------------------------------------
# The candidate the shaper actually sees
# ---------------------------------------------------------------------------


def test_a_split_candidate_supplies_its_text_and_claims_no_line() -> None:
    """Supplying the fragment is allowed. Claiming the line is not."""
    assert provable_line_span(_candidate(split_total=24)) is None

    result = shape_candidates([_candidate(split_total=24)], _budget())
    (item,) = result.items
    assert item.text == "x" * 800
    assert (item.line_start, item.line_end) == (None, None)


def test_an_unsplit_candidate_is_unaffected() -> None:
    candidate = _candidate(line_start=10, line_end=12, text="one\ntwo\nthree", split_total=1)
    assert provable_line_span(candidate) == (10, 12)

    result = shape_candidates([candidate], _budget())
    (item,) = result.items
    assert (item.line_start, item.line_end) == (10, 12)


@pytest.mark.parametrize("split_total", [2, 3, 24, 1000])
def test_no_number_of_pieces_restores_the_claim(split_total: int) -> None:
    assert provable_line_span(_candidate(split_total=split_total)) is None


# ---------------------------------------------------------------------------
# Budget interaction: truncation must not widen anything
# ---------------------------------------------------------------------------


def test_truncating_a_split_fragment_keeps_it_unclaimed() -> None:
    """Narrowing a span that was never established does not establish it.

    The fragment needs interior newlines for the budget to have anywhere to cut:
    whole lines only, so an unbroken fragment either fits or stops the response.
    That is the case below.
    """
    fragment = "\n".join("y" * 40 for _ in range(20))
    candidate = _candidate(line_start=5, line_end=44, text=fragment, split_total=3)
    result = shape_candidates([candidate], _budget(max_item_chars=200))
    (item,) = result.items
    assert len(item.text) < len(fragment)
    assert (item.line_start, item.line_end) == (None, None)
    assert item.truncated is True


def test_an_unbroken_split_fragment_that_cannot_fit_stops_the_response() -> None:
    """Whole lines only, so there is nothing to cut and nothing to return.

    The honest end of the response, and the same answer the rule gave before the
    correction: a partial line is never supplied.
    """
    result = shape_candidates([_candidate(split_total=24)], _budget(max_item_chars=200))
    assert result.items == []
    assert result.truncated is True
    assert result.dropped == 1


def test_a_split_fragment_that_fits_whole_is_not_marked_truncated() -> None:
    """``truncated`` means *the budget shortened this*, and is left alone.

    The field's contract is about this layer's character allowance, not about the
    source span the piece came from. A fragment that fits within the allowance
    was not shortened by it, so reporting ``True`` here would overload the field
    with a second meaning and make "the budget cut something" unreadable. What
    the caller needs to know about the source span is carried by the absent line
    claim, which is precisely the correction.
    """
    result = shape_candidates([_candidate(split_total=24)], _budget())
    (item,) = result.items
    assert item.truncated is False
    assert (item.line_start, item.line_end) == (None, None)


def test_total_budget_exhaustion_never_widens_a_split_claim() -> None:
    first = _candidate(split_total=24)
    second = _candidate(line_start=5, line_end=5, text="y" * 800, split_total=3)
    result = shape_candidates([first, second], _budget(max_total_chars=900))
    for item in result.items:
        assert (item.line_start, item.line_end) == (None, None)
    assert sum(len(item.text) for item in result.items) <= 900


# ---------------------------------------------------------------------------
# Every construction category represents itself truthfully (§8)
# ---------------------------------------------------------------------------


def test_a_candidate_that_came_from_no_chunk_says_so() -> None:
    """``None`` rather than a blanket ``1``.

    Symbol, file-identity, graph and ledger candidates are built from rows that
    were never chunked. The default is the honest answer for all of them, and it
    leaves their judgement to the count test exactly as before the correction.
    """
    assert EvidenceCandidate(repository="r", path="a.py").split_total is None


def test_the_cut_provenance_never_reaches_the_wire() -> None:
    """V1's ``locate --json`` predates this field and must not learn about it.

    ``EvidenceCandidate`` is serialized by ``locate --json``; the field is
    internal provenance for the shaper and is excluded from serialization, so the
    payload is unchanged.
    """
    candidate = _candidate(split_total=24)
    assert candidate.split_total == 24
    assert "split_total" not in candidate.model_dump()
    assert "split_total" not in candidate.model_dump_json()


def test_the_provenance_survives_the_copy_fusion_makes() -> None:
    """Rank fusion rebuilds the representative candidate with ``model_copy``.

    A field lost there would be lost for every candidate that any stage found
    more than once — which is every interesting one.
    """
    copied = _candidate(split_total=24).model_copy(
        update={"score": 0.5, "tier": EvidenceTier.ASSOCIATIVE, "match": MatchClass.SIMILAR}
    )
    assert copied.split_total == 24
    assert provable_line_span(copied) is None
