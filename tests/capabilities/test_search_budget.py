"""Character budgeting and supplied-span safety (V2 Slice D).

Two rules, and both are about not licensing a citation RepoAtlas cannot stand
behind:

* **a citable span covers only complete lines whose text was returned** —
  grounding intersects a citation with the supplied spans, so a span wider than
  the text authorizes citing lines the agent never saw;
* **budgeting narrows the ranked set and never reorders it** — the output is a
  prefix of the trusted order, at most its last item shortened.

Most of this is exercised against synthesized candidates, because the point is
exact character and line arithmetic and the fixture corpus cannot be relied on to
produce a 200-character line on demand. The end-to-end section then checks the
same invariants hold on real retrieved evidence.
"""

from __future__ import annotations

import pytest
from repoatlas.capabilities.search import shape_candidates
from repoatlas.capabilities.service import CapabilityService
from repoatlas.indexing.chunking import chunk_hash
from repoatlas.models.capability import (
    AppliedBudget,
    CapabilityContext,
    CapabilityScope,
    EvidenceBudget,
    RetrievalBreadth,
)
from repoatlas.models.evidence import EvidenceCandidate, EvidenceSource, EvidenceTier, MatchClass
from repoatlas.models.search import SearchRequest
from repoatlas.services.workspace import Workspace
from tests.capabilities.spans import provable_line_span

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
    path: str = "src/a.py",
    line_start: int | None = 10,
    line_end: int | None = 12,
    text: str = "one\ntwo\nthree",
    tier: EvidenceTier = EvidenceTier.DETERMINISTIC,
    source: EvidenceSource = EvidenceSource.EXACT,
) -> EvidenceCandidate:
    return EvidenceCandidate(
        repository="svc",
        repository_slug="svc",
        path=path,
        line_start=line_start,
        line_end=line_end,
        text=text,
        source=source,
        tier=tier,
        match=MatchClass.EXACT_CONTENT,
    )


# ---------------------------------------------------------------------------
# The mapping between text and lines
# ---------------------------------------------------------------------------


def test_a_contiguous_chunk_proves_its_span() -> None:
    """The invariant the store actually satisfies: ``"\\n".join(lines[start-1:end])``."""
    assert provable_line_span(_candidate(text="one\ntwo\nthree")) == (10, 12)


def test_a_span_ending_in_a_blank_line_still_proves_itself() -> None:
    """``split`` and not ``splitlines``.

    A span whose last line is blank ends with a newline and no text after it.
    ``splitlines`` discards that final empty piece, so the count would disagree
    with the span for a perfectly ordinary chunk and its lines would be given up
    for nothing. ``split("\\n")`` keeps it, which is what the store's own join
    produced.
    """
    candidate = _candidate(line_start=1, line_end=3, text="a\nb\n")
    assert candidate.text.splitlines() == ["a", "b"]
    assert provable_line_span(candidate) == (1, 3)


def test_empty_text_proves_no_line_even_when_the_arithmetic_agrees() -> None:
    """``"".split("\\n")`` is one piece, and a one-line span would "match".

    Special-cased deliberately: nothing was supplied, so authorizing a citation to
    line 1 would be licensing a claim about text the caller never saw.
    """
    assert provable_line_span(_candidate(line_start=1, line_end=1, text="")) is None


def test_a_signature_preview_does_not_prove_its_declared_span() -> None:
    """An entity description carries one line against a multi-line declaration.

    The declared span is a real parser fact, but the *text* does not cover it, and
    a citation is checked against the text that was supplied.
    """
    assert provable_line_span(_candidate(line_start=15, line_end=17, text="def f():")) is None


def test_a_split_piece_does_not_prove_the_spans_lines() -> None:
    """A piece of a cut span carries the whole span's line range.

    A middle piece begins mid-span, so which lines its text covers is simply not
    established — and it is the case where guessing would be most confidently
    wrong. Detected by arithmetic on the candidate rather than by metadata, so it
    holds for any producer.
    """
    # A 40-line span whose returned piece holds 12 lines.
    piece = "\n".join(f"line {n}" for n in range(12))
    assert provable_line_span(_candidate(line_start=100, line_end=139, text=piece)) is None


def test_file_identity_evidence_claims_no_line() -> None:
    assert provable_line_span(_candidate(line_start=None, line_end=None, text="")) is None


def test_crlf_text_is_treated_as_the_store_stored_it() -> None:
    """Carriage returns are part of a line's characters, not a second separator.

    If the indexer normalized them away the text simply has none; if it did not,
    they belong to the line and must not be counted as extra lines.
    """
    candidate = _candidate(line_start=1, line_end=2, text="a\r\nb")
    assert provable_line_span(candidate) == (1, 2)


# ---------------------------------------------------------------------------
# Truncation narrows the span
# ---------------------------------------------------------------------------


def test_truncation_narrows_the_line_range_to_what_was_returned() -> None:
    """The §10 case, concretely.

    Candidate lines 10-80. The item budget admits the text through line 32. The
    reference must say 10-32, because a citation to line 70 would otherwise be
    accepted against text nobody was shown.
    """
    lines = [f"line{n:02d}" for n in range(10, 81)]  # 71 lines, six characters each
    text = "\n".join(lines)
    candidate = _candidate(line_start=10, line_end=80, text=text)
    # Exactly enough for 23 lines and the 22 separators between them.
    allowance = len("\n".join(lines[:23]))
    result = shape_candidates([candidate], _budget(max_item_chars=allowance))

    (item,) = result.items
    assert item.line_start == 10
    assert item.line_end == 32
    assert item.truncated is True
    assert item.text.split("\n")[-1] == "line32"
    assert "line33" not in item.text


def test_a_partial_line_is_never_included() -> None:
    """Whole lines only.

    A partial line would be text the caller can read attached to a line number
    that promises the rest of it.
    """
    candidate = _candidate(line_start=1, line_end=3, text="aaaa\nbbbb\ncccc")
    result = shape_candidates([candidate], _budget(max_item_chars=7))
    (item,) = result.items
    assert item.text == "aaaa"
    assert (item.line_start, item.line_end) == (1, 1)


def test_a_first_line_that_cannot_fit_stops_the_response() -> None:
    """Honest stop rather than a partial line or a silently weaker answer."""
    candidate = _candidate(line_start=1, line_end=1, text="x" * 500)
    result = shape_candidates([candidate], _budget(max_item_chars=100))
    assert result.items == []
    assert result.truncated is True
    assert result.dropped == 1


def test_an_unprovable_span_stays_unprovable_after_truncation() -> None:
    """Narrowing a span that was never established does not establish it."""
    piece = "\n".join(f"line {n}" for n in range(12))
    candidate = _candidate(line_start=100, line_end=139, text=piece)
    result = shape_candidates([candidate], _budget(max_item_chars=20))
    (item,) = result.items
    assert item.line_start is None and item.line_end is None
    assert item.truncated is True


# ---------------------------------------------------------------------------
# Exact character enforcement
# ---------------------------------------------------------------------------


def test_the_total_character_budget_is_exact() -> None:
    candidates = [_candidate(path=f"f{n}.py", text="abcd\nefgh") for n in range(5)]
    result = shape_candidates(candidates, _budget(max_total_chars=20))
    assert sum(len(item.text) for item in result.items) <= 20


def test_the_per_item_character_budget_is_exact() -> None:
    candidates = [_candidate(path=f"f{n}.py", text="abcd\nefgh\nijkl") for n in range(3)]
    result = shape_candidates(candidates, _budget(max_item_chars=9))
    assert all(len(item.text) <= 9 for item in result.items)


def test_the_item_count_is_exact() -> None:
    candidates = [_candidate(path=f"f{n}.py") for n in range(10)]
    result = shape_candidates(candidates, _budget(max_items=3))
    assert len(result.items) == 3
    assert result.dropped == 7


def test_the_first_item_is_truncated_rather_than_admitted_whole() -> None:
    """V1's internal bound exempts the first item; the public contract does not.

    A single oversized chunk would otherwise make the caller's budget advisory.
    """
    big = _candidate(line_start=1, line_end=3, text="aaaa\nbbbb\ncccc")
    result = shape_candidates([big], _budget(max_total_chars=9))
    (item,) = result.items
    assert len(item.text) <= 9
    assert item.truncated is True


# ---------------------------------------------------------------------------
# Prefix, not packing
# ---------------------------------------------------------------------------


def test_a_candidate_that_cannot_fit_ends_the_response() -> None:
    """Budgeting narrows the ranked set; it does not re-rank it.

    Skipping the oversized rank-1 candidate to fit the two small ones behind it
    would promote them — a second relevance algorithm wearing a budget's clothes,
    and one that can overturn tier precedence.
    """
    first = _candidate(path="big.py", line_start=1, line_end=1, text="x" * 400)
    second = _candidate(path="small1.py", line_start=1, line_end=1, text="ok")
    third = _candidate(path="small2.py", line_start=1, line_end=1, text="ok")

    result = shape_candidates([first, second, third], _budget(max_item_chars=50))

    assert result.items == []
    assert result.dropped == 3


def test_shaping_never_reorders_what_it_returns() -> None:
    candidates = [_candidate(path=f"f{n}.py", text="ab\ncd") for n in range(6)]
    result = shape_candidates(candidates, _budget(max_total_chars=14))
    assert [item.candidate.path for item in result.items] == [
        f"f{n}.py" for n in range(len(result.items))
    ]


def test_file_identity_evidence_costs_nothing_and_is_kept() -> None:
    """It consumes no characters, so dropping it would lose evidence for nothing."""
    identity = _candidate(path="named.py", line_start=None, line_end=None, text="")
    content = _candidate(path="body.py", line_start=1, line_end=1, text="hello")
    result = shape_candidates([identity, content], _budget(max_total_chars=5))
    assert [item.candidate.path for item in result.items] == ["named.py", "body.py"]


# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------


def test_nothing_omitted_means_nothing_truncated() -> None:
    result = shape_candidates([_candidate()], _budget())
    assert result.truncated is False
    assert result.dropped == 0
    assert all(item.truncated is False for item in result.items)


def test_a_dropped_candidate_marks_the_result_truncated() -> None:
    result = shape_candidates([_candidate(), _candidate(path="b.py")], _budget(max_items=1))
    assert result.truncated is True
    assert result.items[0].truncated is False


# ---------------------------------------------------------------------------
# End to end, on real evidence
# ---------------------------------------------------------------------------


def test_every_returned_span_is_covered_by_its_text(initialized: Workspace) -> None:
    """The invariant, asserted on whatever the corpus actually returns."""
    response = CapabilityService(initialized).search(
        SearchRequest(question="Where is SERVICE_HOST configured?", context=GLOBAL)
    )
    for item in response.evidence:
        if item.ref.line_start is None:
            assert item.ref.line_end is None
            continue
        assert item.ref.line_end is not None
        supplied = len(item.text.split("\n"))
        assert supplied == item.ref.line_end - item.ref.line_start + 1, item.location


def test_the_digest_covers_exactly_the_returned_text(initialized: Workspace) -> None:
    response = CapabilityService(initialized).search(
        SearchRequest(question="Where is SERVICE_HOST configured?", context=GLOBAL)
    )
    for item in response.evidence:
        assert item.ref.span_digest == chunk_hash(item.text)


def test_a_narrow_budget_is_honoured_exactly_end_to_end(initialized: Workspace) -> None:
    response = CapabilityService(initialized).search(
        SearchRequest(
            question="Where is SERVICE_HOST configured?",
            context=GLOBAL,
            budget=EvidenceBudget(max_total_chars=400, max_item_chars=200, max_items=3),
        )
    )
    assert len(response.evidence) <= 3
    assert sum(len(item.text) for item in response.evidence) <= 400
    assert all(len(item.text) <= 200 for item in response.evidence)
    assert response.budget.max_total_chars == 400
    assert response.budget.max_item_chars == 200


def test_a_request_above_the_ceiling_is_clamped_and_says_so(initialized: Workspace) -> None:
    response = CapabilityService(initialized).search(
        SearchRequest(
            question="Where is SERVICE_HOST configured?",
            context=GLOBAL,
            budget=EvidenceBudget(max_total_chars=10_000_000, max_items=10_000),
        )
    )
    assert "max_total_chars" in response.budget.clamped
    assert "max_items" in response.budget.clamped
    assert response.budget.max_total_chars == response.budget.ceiling_max_total_chars


def test_truncation_and_has_more_are_truthful_end_to_end(initialized: Workspace) -> None:
    """Both stages count. A budget enforced early is still a budget."""
    response = CapabilityService(initialized).search(
        SearchRequest(
            question="Where is SERVICE_HOST configured?",
            context=GLOBAL,
            budget=EvidenceBudget(max_items=2),
        )
    )
    assert len(response.evidence) == 2
    # The cut happened inside retrieval, because `max_items` is handed down as the
    # funnel's own bound. It is still the caller's budget that omitted evidence,
    # and the response says so rather than reporting a complete answer.
    assert response.truncated is True
    assert response.has_more is True


def test_a_truncated_item_narrows_its_own_reference_end_to_end(
    initialized: Workspace,
) -> None:
    response = CapabilityService(initialized).search(
        SearchRequest(
            question="Where is SERVICE_HOST configured?",
            context=GLOBAL,
            budget=EvidenceBudget(max_item_chars=60),
        )
    )
    truncated = [item for item in response.evidence if item.truncated]
    if not truncated:
        pytest.skip("no candidate in this corpus exceeded the item budget")
    for item in truncated:
        assert len(item.text) <= 60
        # The digest covers what was supplied, not what the candidate held. Under
        # truncation those differ, which is the only condition in which hashing
        # the wrong one is visible at all.
        assert item.ref.span_digest == chunk_hash(item.text)
        if item.ref.line_start is not None:
            assert item.ref.line_end is not None
            assert len(item.text.split("\n")) == item.ref.line_end - item.ref.line_start + 1
