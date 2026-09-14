"""Fitting retrieved candidates into the granted budget (RepoAtlas V2).

Retrieval decides *what* and in *what order*. This module decides how much of it
fits. It performs no ranking, no scoring and no selection by relevance — every
decision here is about how many characters remain.

What may honestly be claimed about the part that fits is not decided here either:
that question is identical for every capability that supplies citable bytes, and
it is answered once in :mod:`repoatlas.capabilities.evidence_shaping`, below this
module and below inspection. This module supplies the ranked order and the
budget; that one supplies the line arithmetic.

The rule this module exists to enforce
--------------------------------------

**Budgeting narrows the ranked set; it never reorders it.** Output is a *prefix*
of the trusted order, with at most the last item shortened. Skipping an oversized
high-ranked candidate to fit two smaller ones behind it would be a second
relevance algorithm wearing a budget's clothing, and it would quietly overturn the
tier precedence that makes deterministic evidence trustworthy (V1_SPEC §14).
"""

from __future__ import annotations

from dataclasses import dataclass

from repoatlas.capabilities.evidence_shaping import provable_span, supply
from repoatlas.models.capability import AppliedBudget
from repoatlas.models.evidence import EvidenceCandidate


@dataclass(frozen=True, slots=True)
class ShapedEvidence:
    """One candidate after line and character shaping.

    ``text`` is exactly what the caller will be shown and exactly what the digest
    will cover. ``line_start``/``line_end`` describe only lines inside ``text``;
    both ``None`` means the file is claimed and no line is.
    """

    candidate: EvidenceCandidate
    text: str
    line_start: int | None
    line_end: int | None
    #: Something the candidate held was not returned.
    truncated: bool


@dataclass(frozen=True, slots=True)
class ShapingResult:
    items: list[ShapedEvidence]
    #: Anything omitted or shortened for budget, at any stage.
    truncated: bool
    #: Ranked candidates that were never returned at all.
    dropped: int
    #: Candidates that collapsed onto a reference already supplied by a
    #: higher-ranked one. Not an omission — the evidence is in the response.
    duplicates: int = 0


def _claim(
    candidate: EvidenceCandidate, text: str, line_start: int | None, line_end: int | None
) -> tuple[str, str, int | None, int | None, str]:
    """What the ledger will treat as this evidence's identity.

    The same tuple the ledger keys on — repository identity, path, supplied span
    and supplied bytes — so "already supplied" means here exactly what it will
    mean there.
    """
    return (candidate.repository_slug, candidate.path, line_start, line_end, text)


def provable_line_span(candidate: EvidenceCandidate) -> tuple[int, int] | None:
    """The source lines this candidate's text demonstrably covers, or ``None``.

    The candidate-shaped spelling of
    :func:`~repoatlas.capabilities.evidence_shaping.provable_span`, which holds the
    reasoning and the four producers that legitimately prove nothing.
    """
    return provable_span(
        candidate.text,
        candidate.line_start,
        candidate.line_end,
        split_total=candidate.split_total,
    )


def shape_candidates(
    candidates: tuple[EvidenceCandidate, ...] | list[EvidenceCandidate],
    budget: AppliedBudget,
) -> ShapingResult:
    """Turn trusted ranked candidates into evidence that fits the granted budget.

    Walks the ranked order and stops; it never looks past a candidate that did not
    fit. The result is a prefix of that order, possibly with its final item
    shortened, which is what keeps budgeting a narrowing of the evidence set
    rather than a re-ranking of it.

    Three outcomes per candidate:

    * it fits whole, and is included;
    * it can be shortened to whole lines that fit, and is included truncated;
    * not one complete line fits, and shaping stops there.

    File-identity evidence carries no text and therefore costs nothing, so it is
    admitted while item slots remain — it is a deterministic fact about the
    repository, and dropping it to save characters it does not consume would lose
    evidence for nothing.
    """
    items: list[ShapedEvidence] = []
    remaining_total = budget.max_total_chars
    considered = 0
    #: What has already been supplied, by the identity the ledger will give it.
    supplied: set[tuple[str, str, int | None, int | None, str]] = set()

    for candidate in candidates:
        if len(items) >= budget.max_items:
            break
        considered += 1

        if not candidate.text:
            # Costs no characters. Its line claim is whatever it could prove,
            # which for empty text is nothing.
            if _claim(candidate, "", None, None) in supplied:
                continue
            supplied.add(_claim(candidate, "", None, None))
            items.append(
                ShapedEvidence(
                    candidate=candidate, text="", line_start=None, line_end=None, truncated=False
                )
            )
            continue

        allowance = min(budget.max_item_chars, remaining_total)
        piece = supply(
            candidate.text,
            candidate.line_start,
            candidate.line_end,
            limit=allowance,
            split_total=candidate.split_total,
        )

        if piece is None:
            # Not one complete line fits. Stopping is the honest end of the
            # response: returning the file identity instead would silently swap a
            # content answer for a weaker one, and skipping ahead to something
            # smaller would reorder the trusted ranking.
            considered -= 1
            break

        text = piece.text
        line_start = piece.line_start
        line_end = piece.line_end
        truncated = piece.truncated

        claim = _claim(candidate, text, line_start, line_end)
        if claim in supplied:
            # Shaping can collapse two ranked candidates onto one supplied
            # reference — a file-identity hit and a graph hit for the same file
            # both end up claiming the file and nothing else. The ledger would
            # give them one id, correctly, and returning that id twice would spend
            # two rank slots on one piece of evidence and invite a caller to
            # wonder what the difference was. The first, strongest sighting stands.
            continue
        supplied.add(claim)

        items.append(
            ShapedEvidence(
                candidate=candidate,
                text=text,
                line_start=line_start,
                line_end=line_end,
                truncated=truncated,
            )
        )
        remaining_total -= len(text)

    # Duplicates are not "more evidence that did not fit": they are the same
    # evidence, so collapsing them is not an omission and must not report one.
    duplicates = considered - len(items)
    dropped = len(candidates) - considered
    truncated_anything = dropped > 0 or any(item.truncated for item in items)
    return ShapingResult(
        items=items, truncated=truncated_anything, dropped=dropped, duplicates=duplicates
    )
