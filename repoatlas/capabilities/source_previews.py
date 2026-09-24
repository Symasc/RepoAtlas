"""Small contiguous previews of proven indexed lines; never read the worktree."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from repoatlas.models.evidence import EvidenceCandidate, SourcePreview


def preview(
    candidate: EvidenceCandidate, lines: Mapping[int, str], terms: Sequence[str], limit: int
) -> EvidenceCandidate:
    start, end = candidate.line_start, candidate.line_end
    if start is None or end is None or end < start:
        return candidate
    available = [n for n in sorted(lines) if start <= n <= end]
    if not available:
        return candidate
    needles = tuple(dict.fromkeys(t.casefold() for t in terms if t))
    # Prefer the line covering the most query terms, then its earliest occurrence.
    # A declaration with no literal spelling (Class.method) starts at its definition.
    scores = {n: sum(t in lines[n].casefold() for t in needles) for n in available}
    matching = [n for n in available if scores[n]]
    eligible = [n for n in (matching or available) if len(lines[n]) <= limit]
    if not eligible:
        return candidate
    anchor = max(eligible, key=lambda n: (scores[n], -n))
    lo = hi = anchor
    used = len(lines[anchor])
    # At most two preceding lines, and only if the match still fits in full.
    for n in range(anchor - 1, max(start - 1, anchor - 3), -1):
        if n not in lines or used + len(lines[n]) + 1 > limit:
            break
        lo = n
        used += len(lines[n]) + 1
    while hi < end and hi + 1 in lines and used + len(lines[hi + 1]) + 1 <= limit:
        hi += 1
        used += len(lines[hi]) + 1
    # Small definitions should arrive complete even if the match was in their body.
    while lo > start and lo - 1 in lines and used + len(lines[lo - 1]) + 1 <= limit:
        lo -= 1
        used += len(lines[lo]) + 1
    omitted = tuple(span for span in ((start, lo - 1), (hi + 1, end)) if span[0] <= span[1])
    return candidate.model_copy(
        update={
            "text": "\n".join(lines[n] for n in range(lo, hi + 1)),
            "line_start": lo,
            "line_end": hi,
            "declaration": False,
            "split_total": 1,
            "preview": SourcePreview(source_span=(start, end), omitted_spans=omitted),
        }
    )
