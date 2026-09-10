"""Budgeting inspectable source into citable segments (RepoAtlas V2).

Pure. It receives source lines that have already been proven — decoded bytes whose
hash matched the index, or the line map
:mod:`repoatlas.capabilities.indexed_source` reconstructed from stored chunks —
and decides which of the requested lines fit, which could not be proven, and which
were left out for budget. It opens nothing, reads nothing and has no handle on
anything.

The reconstruction itself, and the reasoning about which chunks may contribute to
it, lives in :mod:`repoatlas.capabilities.indexed_source` because inspection is
not its only caller: an edge's asserting lines are reconstructed exactly the same
way, and one wrong answer there would be a fabricated citation just as surely.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from repoatlas.capabilities.evidence_shaping import LINE_SEPARATOR, prefix_lines
from repoatlas.capabilities.indexed_source import WHOLE_FILE_END, indexed_line_map, to_ranges
from repoatlas.models.capability import AppliedBudget
from repoatlas.models.capability_io import LineRange
from repoatlas.parsers.marks import split_lines

__all__ = [
    "WHOLE_FILE_END",
    "InspectionSegment",
    "ShapedInspection",
    "clamp",
    "indexed_line_map",
    "pad",
    "shape_inspection",
    "to_ranges",
    "worktree_line_map",
]


@dataclass(frozen=True, slots=True)
class InspectionSegment:
    """One run of consecutive source lines, and the text supplied for it."""

    line_start: int
    line_end: int
    text: str
    #: The run held more lines than were supplied.
    truncated: bool


@dataclass(frozen=True, slots=True)
class ShapedInspection:
    """The outcome of fitting one requested range into one budget."""

    segments: list[InspectionSegment]
    returned: tuple[LineRange, ...]
    #: Lines that were available and were not supplied, for budget alone.
    budget_omitted: tuple[LineRange, ...]
    #: Requested lines the source could not prove.
    unavailable: tuple[LineRange, ...]


def worktree_line_map(content: str) -> dict[int, str]:
    """Every line of a fully read file, numbered from one.

    Splits with the one source-line splitter RepoAtlas counts every line number
    with, so these numbers are the same ones the chunks, the entity spans and the
    parsers mean (ARCHITECTURE.md §11). Doing it here rather than taking a
    pre-split list is the point: a caller cannot hand this function lines produced
    by ``str.splitlines()``, which breaks on characters no source grammar treats
    as a line ending and would number the whole file differently.
    """
    return {number: text for number, text in enumerate(split_lines(content), start=1)}


def pad(base: LineRange, context_lines: int) -> LineRange:
    """Widen a range by ``context_lines`` on each side, never below line one."""
    return LineRange(start=max(1, base.start - context_lines), end=base.end + context_lines)


def clamp(requested: LineRange, line_count: int) -> LineRange | None:
    """Confine a range to a file whose true length is known.

    Only ever called with a **provable** length. Clamping to the last line some
    chunk happened to mention would silently convert "RepoAtlas cannot prove these
    lines" into "you did not ask for them".
    """
    if line_count < 1:
        return None
    start = min(requested.start, line_count)
    end = min(requested.end, line_count)
    return LineRange(start=start, end=end)


def shape_inspection(
    available: Mapping[int, str], requested: LineRange, budget: AppliedBudget
) -> ShapedInspection:
    """Fit the provable part of a requested range into the granted budget.

    Ascending source order is inspection's only order, and unlike retrieval it is
    not a ranking — so walking it and stopping is not a relevance decision, it is
    simply where the characters ran out. The rules that follow from that:

    * **no skipping.** A run that does not fit ends the supply. Packing a smaller
      later run in front of it would reorder the file, and a caller reading source
      in line order would be handed line 90 before line 40;
    * **a gap is not a stop.** Where the source itself cannot prove a range, the
      run after it is still supplied, because skipping it was not a budget choice
      and the gap is reported explicitly;
    * **partial lines are never supplied**, so a run that is cut is cut at a line
      boundary and its claim shrinks with it.

    The three coverage sets partition the requested range exactly: every requested
    line is returned, unavailable, or budget-omitted, and never two of those.
    """
    wanted = range(requested.start, requested.end + 1)
    provable = {number for number in wanted if number in available}

    segments: list[InspectionSegment] = []
    remaining_total = budget.max_total_chars

    for run in to_ranges(provable):
        if len(segments) >= budget.max_items:
            break
        text = LINE_SEPARATOR.join(available[number] for number in range(run.start, run.end + 1))
        kept, count = prefix_lines(text, min(budget.max_item_chars, remaining_total))
        if text and not kept:
            # Not one complete line of this run fits. The honest end of the
            # response: what follows is further down the same file, and moving it
            # up to fill the space would misrepresent the order of the source.
            break
        # ``count`` is the number of complete lines in ``kept``; the line map is
        # what proves they are exactly ``run.start`` onwards. A run of one blank
        # line yields no characters and still covers its line, which is why the
        # span comes from the map rather than from the text.
        line_end = run.start + max(count, 1) - 1
        segments.append(
            InspectionSegment(
                line_start=run.start,
                line_end=line_end,
                text=kept,
                truncated=len(kept) < len(text),
            )
        )
        remaining_total -= len(kept)
        if len(kept) < len(text):
            # The run was cut, so everything after it is a budget omission too.
            break

    returned_lines = {
        number for segment in segments for number in range(segment.line_start, segment.line_end + 1)
    }
    return ShapedInspection(
        segments=segments,
        returned=to_ranges(returned_lines),
        budget_omitted=to_ranges(provable - returned_lines),
        unavailable=to_ranges(set(wanted) - provable),
    )
