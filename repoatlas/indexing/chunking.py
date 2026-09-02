"""Chunking (ARCHITECTURE.md §6).

Chunks are the unit of lexical and semantic retrieval, so every chunk carries the
line range that proves it. Symbol-aligned where the parser supplied ranges,
fixed windows otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass

from repoatlas.config.settings import IndexingSettings
from repoatlas.models.parsing import ParsedSymbol

#: Rough characters-per-token ratio, used only for budgeting.
_CHARS_PER_TOKEN = 4


@dataclass(frozen=True, slots=True)
class Chunk:
    line_start: int
    line_end: int
    text: str

    @property
    def token_estimate(self) -> int:
        return max(1, len(self.text) // _CHARS_PER_TOKEN)


def chunk_content(
    content: str,
    symbols: tuple[ParsedSymbol, ...] = (),
    settings: IndexingSettings | None = None,
) -> list[Chunk]:
    """Split ``content`` into retrievable, line-provenanced chunks."""
    config = settings or IndexingSettings()
    lines = content.splitlines()
    if not lines:
        return []

    spans = _symbol_spans(symbols, len(lines), config)
    if not spans:
        spans = _window_spans(len(lines), config)

    chunks: list[Chunk] = []
    for start, end in spans:
        text = "\n".join(lines[start - 1 : end])
        if text.strip():
            chunks.append(Chunk(line_start=start, line_end=end, text=text))
    return chunks


def _symbol_spans(
    symbols: tuple[ParsedSymbol, ...], total_lines: int, config: IndexingSettings
) -> list[tuple[int, int]]:
    """Top-level symbol ranges, split when oversized, gaps filled with windows."""
    top_level = [
        s
        for s in symbols
        if s.parent is None and s.line_end >= s.line_start and s.line_end <= total_lines
    ]
    # A module symbol spans the whole file; using it would defeat chunking.
    top_level = [s for s in top_level if not (s.line_start == 1 and s.line_end >= total_lines)]
    if not top_level:
        return []

    spans: list[tuple[int, int]] = []
    cursor = 1
    for symbol in sorted(top_level, key=lambda s: (s.line_start, s.line_end)):
        if symbol.line_start > cursor:
            spans.extend(_window_range(cursor, symbol.line_start - 1, config))
        spans.extend(_split_oversized(symbol.line_start, symbol.line_end, config))
        cursor = max(cursor, symbol.line_end + 1)
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
