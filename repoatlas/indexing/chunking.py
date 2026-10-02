"""Chunking (ARCHITECTURE.md §6).

Chunks are the unit of lexical retrieval, so every chunk carries the
line range that proves it. Symbol-aligned where the parser supplied ranges,
fixed windows otherwise.

The line array comes from :func:`~repoatlas.parsers.marks.split_lines`, the same
splitter the parsers count with, and that sharing is load-bearing. Chunk spans and
entity spans are compared, fused and cited side by side, so they have to be claims
about the same lines. Splitting here with ``str.splitlines()`` instead — which
breaks on form feed, vertical tab and U+2028 where no source grammar does — put a
chunk containing ``b = 2`` at line 3 of a two-line file, and tore a string literal
holding a vertical tab into two chunks on two lines that were really one.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from repoatlas.config.settings import IndexingSettings
from repoatlas.models.parsing import ParsedSymbol
from repoatlas.parsers.marks import split_lines

#: Version of the chunking rules, part of the facts version (V1_SPEC §12).
#:
#: A chunk carries a line range and is cited like any other deterministic fact, so
#: a change to how spans are computed makes stored chunks stale exactly as a parser
#: change does. Without this, a chunking-only fix would sit in the code and never
#: reach an index whose files are all unchanged.
#:
#: 2: the line array is the shared source-grammar splitter, not ``str.splitlines()``.
CHUNKING_VERSION = "2"

#: Rough characters-per-token ratio, used only for budgeting.
_CHARS_PER_TOKEN = 4


@dataclass(frozen=True, slots=True)
class Chunk:
    line_start: int
    line_end: int
    text: str
    #: Which line span of the file this chunk came from.
    span_index: int = 0
    #: Which piece of that span this is, and how many pieces the span became.
    #:
    #: A span whose text exceeds the character cap is cut, and the cut is what
    #: used to decide whether a literal was findable at all. Recording where each
    #: piece sits makes the cut reversible: exact search stitches adjacent pieces
    #: back together across the seam, so the cap bounds *evidence size* only and
    #: no longer bounds *discoverability* (V1_SPEC §14).
    split_index: int = 0
    split_total: int = 1

    @property
    def token_estimate(self) -> int:
        return max(1, len(self.text) // _CHARS_PER_TOKEN)


def chunk_hash(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()


def chunk_content(
    content: str,
    symbols: tuple[ParsedSymbol, ...] = (),
    settings: IndexingSettings | None = None,
) -> list[Chunk]:
    """Split ``content`` into retrievable, line-provenanced chunks."""
    config = settings or IndexingSettings()
    lines = split_lines(content)
    if not lines:
        return []

    spans = _symbol_spans(symbols, len(lines), config)
    if not spans:
        spans = _window_spans(len(lines), config)

    chunks: list[Chunk] = []
    for span_index, (start, end) in enumerate(spans):
        text = "\n".join(lines[start - 1 : end])
        if not text.strip():
            continue
        pieces = _bounded(text, config.max_chunk_chars)
        chunks.extend(
            Chunk(
                line_start=start,
                line_end=end,
                text=piece,
                span_index=span_index,
                split_index=split_index,
                split_total=len(pieces),
            )
            for split_index, piece in enumerate(pieces)
        )
    return chunks


def _bounded(text: str, limit: int) -> list[str]:
    """Split text that exceeds the character cap, keeping every character.

    Reached only by content a line-based span cannot bound — a minified bundle, a
    base64 blob, a generated one-liner. Truncating instead would silently lose
    whatever sits past the cap, and an exact search for it would then return
    nothing while the file plainly contains it (V1_SPEC §14).

    Each piece keeps the line range of the span it came from, which stays true:
    every piece really is on those lines. The citation is coarser than the text,
    never wrong about it.
    """
    if limit <= 0 or len(text) <= limit:
        return [text]
    return [text[offset : offset + limit] for offset in range(0, len(text), limit)]


def _symbol_spans(
    symbols: tuple[ParsedSymbol, ...], total_lines: int, config: IndexingSettings
) -> list[tuple[int, int]]:
    """Top-level symbol ranges, split when oversized, gaps filled with windows."""
    # A symbol with no span is the whole-file symbol of an empty file, which this
    # function is never reached for; it is filtered here so the arithmetic below
    # never has to consider an absent line.
    # Only spans are needed below, so they are extracted once. A symbol with no
    # span is the whole-file symbol of an empty file, which this function is never
    # reached for; dropping it here keeps the arithmetic free of absent lines.
    # A module value is a declaration for USES alone (USES_ONLY_KINDS): chunks stay
    # where they were before it was one.
    top_level = [
        (s.line_start, s.line_end)
        for s in symbols
        if s.parent is None
        and s.line_start is not None
        and s.line_end is not None
        and not s.uses_only
    ]
    # A module symbol spans the whole file; using it would defeat chunking.
    top_level = [
        (start, end)
        for start, end in top_level
        if start <= end <= total_lines and not (start == 1 and end >= total_lines)
    ]
    if not top_level:
        return []

    spans: list[tuple[int, int]] = []
    cursor = 1
    for start, end in sorted(top_level):
        if start > cursor:
            spans.extend(_window_range(cursor, start - 1, config))
        spans.extend(_split_oversized(start, end, config))
        cursor = max(cursor, end + 1)
    if cursor <= total_lines:
        spans.extend(_window_range(cursor, total_lines, config))
    return spans


def _split_oversized(start: int, end: int, config: IndexingSettings) -> list[tuple[int, int]]:
    if end - start + 1 <= config.max_chunk_lines:
        return [(start, end)]
    return _window_range(start, end, config)


def _window_spans(total_lines: int, config: IndexingSettings) -> list[tuple[int, int]]:
    return _window_range(1, total_lines, config)


def _window_range(start: int, end: int, config: IndexingSettings) -> list[tuple[int, int]]:
    """Fixed windows with overlap, so a match near a boundary keeps its context."""
    if end < start:
        return []
    size = max(1, config.chunk_lines)
    overlap = min(config.chunk_overlap_lines, size - 1) if size > 1 else 0
    step = max(1, size - overlap)

    spans: list[tuple[int, int]] = []
    cursor = start
    while cursor <= end:
        spans.append((cursor, min(cursor + size - 1, end)))
        if cursor + size - 1 >= end:
            break
        cursor += step
    return spans
