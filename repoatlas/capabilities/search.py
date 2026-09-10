"""Shaping retrieved candidates into citable evidence (RepoAtlas V2).

Retrieval decides *what* and in *what order*. This module decides how much of it
fits and what may honestly be claimed about the part that does. It performs no
ranking, no scoring and no selection by relevance — every decision here is about
characters and lines.

The two rules it exists to enforce
----------------------------------

**A citable span covers only complete lines whose text was returned.** Grounding
verifies a citation by intersecting it with the spans that were supplied
(V1_SPEC §15), so a span wider than the text would authorize citing lines the
agent never saw — a fabrication RepoAtlas would have licensed itself. Truncation
therefore narrows the line range with the text, and evidence whose text cannot be
mapped onto its source lines claims no line at all.

**Budgeting narrows the ranked set; it never reorders it.** Output is a *prefix*
of the trusted order, with at most the last item shortened. Skipping an oversized
high-ranked candidate to fit two smaller ones behind it would be a second
relevance algorithm wearing a budget's clothing, and it would quietly overturn the
tier precedence that makes deterministic evidence trustworthy (V1_SPEC §14).

How a candidate's text maps to its lines
----------------------------------------
Verified against the index rather than assumed. Every chunk in the store
satisfies::

    text == "\\n".join(source_lines[line_start - 1 : line_end])

so ``text.split("\\n")`` yields exactly ``line_end - line_start + 1`` pieces and
piece *k* is source line ``line_start + k``. Note ``split`` and not
``splitlines``: a span ending in a blank line produces a trailing empty piece that
``splitlines`` discards, which would make the count disagree with the span for a
perfectly ordinary chunk.

Candidates that do **not** satisfy it are real and are handled rather than
special-cased away: an entity description carries a one-line signature against a
multi-line declared span, and a piece of a split span begins mid-span. Neither
proves which source lines its text covers, so neither gets a line claim. The check
is arithmetic on the candidate itself, so it needs no metadata and cannot be
fooled by a producer this module has not seen.
"""

from __future__ import annotations

from dataclasses import dataclass

from repoatlas.models.capability import AppliedBudget
from repoatlas.models.evidence import EvidenceCandidate

#: How a candidate's text was joined back together, and therefore how it splits.
_LINE_SEPARATOR = "\n"


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

    ``None`` is the honest answer in three situations, and each is a real producer
    rather than a hypothetical:

    * the candidate already claims no line — a filename match knows nothing about
      any line, so it says nothing about one (V1_SPEC §11);
    * the text is empty — an entity that was reached but has no stored signature
      supplies no line, and arithmetic that "proves" an empty string covers line 1
      would authorize citing a line nobody was shown;
    * the piece count disagrees with the span — a split piece or a signature
      preview, where which lines the text covers is simply not established.

    A ``None`` here is not a discarded candidate. The evidence is still returned;
    it is returned as a claim about the file rather than about lines.
    """
    if candidate.line_start is None or candidate.line_end is None:
        return None
    if not candidate.text:
        return None
    pieces = candidate.text.split(_LINE_SEPARATOR)
    if len(pieces) != candidate.line_end - candidate.line_start + 1:
        return None
    return (candidate.line_start, candidate.line_end)


def _prefix_lines(text: str, limit: int) -> tuple[str, int]:
    """The longest whole-line prefix of ``text`` within ``limit`` characters.

    Returns the prefix and how many lines it holds. A partial line is never
    included: it would be text the caller can read attached to a line number that
    promises the rest of it.
    """
    if limit <= 0:
        return ("", 0)
    if len(text) <= limit:
        return (text, len(text.split(_LINE_SEPARATOR)))

    pieces = text.split(_LINE_SEPARATOR)
    kept: list[str] = []
    used = 0
    for piece in pieces:
        # Every line after the first costs its separator too.
        cost = len(piece) + (1 if kept else 0)
        if used + cost > limit:
            break
        kept.append(piece)
        used += cost
    if not kept:
        return ("", 0)
    return (_LINE_SEPARATOR.join(kept), len(kept))


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

        span = provable_line_span(candidate)

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
        text, lines = _prefix_lines(candidate.text, allowance)

        if not text:
            # Not one complete line fits. Stopping is the honest end of the
            # response: returning the file identity instead would silently swap a
            # content answer for a weaker one, and skipping ahead to something
            # smaller would reorder the trusted ranking.
            considered -= 1
            break

        truncated = len(text) < len(candidate.text)
        if span is None:
            line_start = line_end = None
        else:
            line_start = span[0]
            line_end = span[0] + lines - 1

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
