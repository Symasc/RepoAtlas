"""Fitting retrieved candidates into the granted budget (RepoAtlas V2).

Retrieval decides *what* and in *what order*. This module decides how much of it
fits. It performs no ranking, no scoring and no selection by relevance — every
decision here is about how many characters remain, with one exception that is not
a budget decision at all: a candidate that names an entity and supplies no text
about it is never evidence (:attr:`EvidenceCandidate.is_handle_only`).

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

from collections.abc import Callable
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
    #: Handle-only candidates: entities named with no text to cite. Not an
    #: omission either — there was never anything here that could be evidence,
    #: and the exact-name ones reach the caller as seed handles.
    withheld: int = 0


def _claim(
    candidate: EvidenceCandidate, text: str, line_start: int | None, line_end: int | None
) -> tuple[str, str, int | None, int | None, str]:
    """What the ledger will treat as this evidence's identity.

    The same tuple the ledger keys on — repository identity, path, supplied span
    and supplied bytes — so "already supplied" means here exactly what it will
    mean there.
    """
    return (candidate.repository_slug, candidate.path, line_start, line_end, text)


def offered_lines(candidate: EvidenceCandidate) -> tuple[int | None, int | None]:
    """The line range this candidate may try to prove, before any arithmetic.

    Its own range, except for a declaration (:attr:`EvidenceCandidate.declaration`),
    which offers none. A declaration's text was assembled from facts about the
    source, so there are no lines for it to prove — and the arithmetic would not
    notice: ``service.host`` is one line of text, the key's span is one line of
    source, the counts agree, and the evidence would then tell a caller that line
    reads ``service.host`` when it reads ``host: service.internal.example``.

    Withholding the range here, rather than teaching the arithmetic about
    declarations, keeps :func:`~repoatlas.capabilities.evidence_shaping.provable_span`
    exactly what it was: a check on text that *was* read from the file. The
    declaration is still supplied, as a claim about the file it is declared in.
    """
    if candidate.declaration:
        return (None, None)
    return (candidate.line_start, candidate.line_end)


def provable_line_span(candidate: EvidenceCandidate) -> tuple[int, int] | None:
    """The source lines this candidate's text demonstrably covers, or ``None``.

    The candidate-shaped spelling of
    :func:`~repoatlas.capabilities.evidence_shaping.provable_span`, which holds the
    reasoning and the four producers that legitimately prove nothing, applied to
    the range :func:`offered_lines` allows.
    """
    return provable_span(
        candidate.text,
        *offered_lines(candidate),
        split_total=candidate.split_total,
    )


def shape_candidates(
    candidates: tuple[EvidenceCandidate, ...] | list[EvidenceCandidate],
    budget: AppliedBudget,
    prepare: Callable[[EvidenceCandidate, int], EvidenceCandidate] | None = None,
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

    A handle-only candidate is never admitted, wherever it ranks. It names an
    entity and supplies no text about it, so an id issued for it would let any
    claim at all verify against it. Withholding one is not budgeting — it spends
    no slot, is not an omission, and does not mark the response truncated —
    because nothing was left out that could have been cited.
    """
    items: list[ShapedEvidence] = []
    remaining_total = budget.max_total_chars
    duplicates = 0
    withheld = 0
    #: Where the walk stopped for budget, if it did. Everything citable from here
    #: on was ranked and not returned.
    stopped_at: int | None = None
    #: What has already been supplied, by the identity the ledger will give it.
    supplied: set[tuple[str, str, int | None, int | None, str]] = set()

    for index, candidate in enumerate(candidates):
        if prepare is not None and len(items) < budget.max_items:
            candidate = prepare(candidate, min(budget.max_item_chars, remaining_total))
        if candidate.is_handle_only:
            withheld += 1
            continue
        if len(items) >= budget.max_items:
            stopped_at = index
            break

        if not candidate.text:
            # Costs no characters. Its line claim is whatever it could prove,
            # which for empty text is nothing.
            if _claim(candidate, "", None, None) in supplied:
                duplicates += 1
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
            *offered_lines(candidate),
            limit=allowance,
            split_total=candidate.split_total,
        )

        if piece is None:
            # Not one complete line fits. Stopping is the honest end of the
            # response: returning the file identity instead would silently swap a
            # content answer for a weaker one, and skipping ahead to something
            # smaller would reorder the trusted ranking.
            stopped_at = index
            break

        text = piece.text
        line_start = piece.line_start
        line_end = piece.line_end
        truncated = piece.truncated or bool(candidate.preview and candidate.preview.omitted_spans)

        claim = _claim(candidate, text, line_start, line_end)
        if claim in supplied:
            # Shaping can collapse two ranked candidates onto one supplied
            # reference — a file-identity hit and a graph hit for the same file
            # both end up claiming the file and nothing else. The ledger would
            # give them one id, correctly, and returning that id twice would spend
            # two rank slots on one piece of evidence and invite a caller to
            # wonder what the difference was. The first, strongest sighting stands.
            duplicates += 1
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
    # Neither is a handle-only candidate past the stopping point: it could never
    # have been returned, so it is not something the budget left out.
    dropped = (
        0
        if stopped_at is None
        else sum(1 for candidate in candidates[stopped_at:] if not candidate.is_handle_only)
    )
    truncated_anything = dropped > 0 or any(item.truncated for item in items)
    return ShapingResult(
        items=items,
        truncated=truncated_anything,
        dropped=dropped,
        duplicates=duplicates,
        withheld=withheld,
    )
