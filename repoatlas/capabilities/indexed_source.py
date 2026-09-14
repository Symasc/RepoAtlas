"""Reconstructing indexed source from the chunks that hold it (RepoAtlas V2).

Pure, and shared by every capability that needs to show source RepoAtlas already
stored: inspection, when a caller asks to read a range of a file, and
relationships, when an edge names the lines that assert it. Both face the same
question — *which source lines do these chunks demonstrably contain?* — and one
wrong answer to it is a fabricated citation either way.

Everything here is built on one representation: a **line map**, from source line
number to that line's text. It is deliberately not a list of chunks or a list of
intervals, and the reason is the defect it removes.

Why a line map rather than merged intervals
-------------------------------------------
Two chunks with adjacent line metadata do not prove that every line between them
was stored. A span whose text exceeded the chunk cap is cut into pieces that all
carry the *span's* line range (ARCHITECTURE.md §6), so a piece other than a whole
one begins or ends mid-line and covers fewer lines than it advertises. Merging by
metadata would therefore produce one continuous citable interval across text that
was never supplied — precisely the fabrication the supplied-span rule exists to
prevent, arrived at by arithmetic instead of by invention.

A line map cannot express that mistake. A line is present only if some chunk's
text demonstrably contained it, so continuity is a consequence of what was stored
rather than an inference about it, gaps are simply absent keys, and two chunks
that overlap contribute one copy of the shared lines instead of two.

Which chunks are allowed to contribute
--------------------------------------
Only whole ones: ``split_total == 1`` **and** a piece count that matches the
declared span. Either test alone is weaker than the pair. The count test alone
would admit a cut piece that happened to land on the right number of lines; the
``split_total`` test alone would admit an entity description whose one-line
signature is stored against a multi-line declaration. What a rejected chunk costs
is line-level citation for a span where it was never provable, and the caller says
so rather than filling it in from inference.
"""

from __future__ import annotations

from collections.abc import Iterable

from repoatlas.capabilities.evidence_shaping import LINE_SEPARATOR
from repoatlas.models.capability_io import LineRange
from repoatlas.storage.protocols import ChunkHit

#: Upper bound for "to the end of the file" when the end is not yet known. Larger
#: than any line number a source file reaches, and finite so the query stays a
#: bounded range rather than becoming an unfiltered table scan.
WHOLE_FILE_END = 2_000_000_000


def indexed_line_map(chunks: Iterable[ChunkHit]) -> dict[int, str]:
    """Every source line the indexed chunks demonstrably contain.

    Earlier chunks win a tie, which arises only where two chunks overlap — and
    there they agree, because both were cut from one decoded file.
    """
    lines: dict[int, str] = {}
    for chunk in chunks:
        if not chunk.text:
            continue
        if chunk.split_total != 1:
            # A piece of a cut span. Its text does not cover the range it carries.
            continue
        pieces = chunk.text.split(LINE_SEPARATOR)
        if len(pieces) != chunk.line_end - chunk.line_start + 1:
            # A preview, or a span whose text and range simply disagree.
            continue
        for offset, piece in enumerate(pieces):
            lines.setdefault(chunk.line_start + offset, piece)
    return lines


def to_ranges(numbers: Iterable[int]) -> tuple[LineRange, ...]:
    """Consecutive line numbers as closed intervals, ascending."""
    ordered = sorted(set(numbers))
    if not ordered:
        return ()
    ranges: list[LineRange] = []
    start = previous = ordered[0]
    for number in ordered[1:]:
        if number == previous + 1:
            previous = number
            continue
        ranges.append(LineRange(start=start, end=previous))
        start = previous = number
    ranges.append(LineRange(start=start, end=previous))
    return tuple(ranges)
