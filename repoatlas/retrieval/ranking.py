"""Rank fusion, tiering, deduplication and budgeting (ARCHITECTURE.md §10, stages 5-8).

Reciprocal-rank fusion, because BM25 scores, cosine similarities, literal-match
scores and graph distances are not commensurable and RRF needs no per-source
calibration to combine them stably.

**Fusion never crosses a tier.** RRF answers "how strongly do the retrievers agree
about this span"; the tier answers "is this a fact about the code or a guess about
the question", and the second question is settled before the first is asked. That
ordering is the core retrieval principle of V1_SPEC §14 made structural: no
similarity score, however high, moves an embedding neighbour above an exact
identifier, an exact error string, an exact symbol, an environment variable or a
graph-supported relationship.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from repoatlas.models.evidence import EvidenceCandidate, EvidenceTier

#: RRF damping constant. 60 is the value from the original formulation.
_RRF_K = 60.0

#: Fraction of the shorter span that must overlap before the lower-ranked
#: candidate is treated as redundant rather than as additional context.
_OVERLAP_THRESHOLD = 0.5

_Key = tuple[str, str, int, int]


class Reranker(Protocol):
    """The V2 seam. ``NoopReranker`` is what V1 ships."""

    def rerank(
        self, question: str, candidates: list[EvidenceCandidate]
    ) -> list[EvidenceCandidate]: ...


class NoopReranker:
    """Preserves fusion order. Replaced by a cross-encoder in V2.

    A V2 reranker must respect the tier boundary the same way fusion does: it may
    reorder within a tier, never across one, or it would reintroduce exactly the
    displacement V1_SPEC §14 forbids.
    """

    def rerank(self, question: str, candidates: list[EvidenceCandidate]) -> list[EvidenceCandidate]:
        return candidates


def _key(candidate: EvidenceCandidate) -> _Key:
    return (candidate.repository, candidate.path, candidate.line_start, candidate.line_end)


def fuse(
    ranked_lists: Sequence[Sequence[EvidenceCandidate]],
    *,
    primary_repository: str | None = None,
    primary_boost: float = 0.5,
) -> list[EvidenceCandidate]:
    """Combine ranked lists into one ordering: tier first, fused score second.

    The primary-repository boost biases toward the repository the question was
    asked in without walling off other repositories — "primary scope, not a wall"
    (V1_SPEC §6). It is added to the score, so it competes inside a tier and never
    promotes a candidate across one.
    """
    scores: dict[_Key, float] = {}
    tiers: dict[_Key, EvidenceTier] = {}
    best: dict[_Key, EvidenceCandidate] = {}

    for ranked in ranked_lists:
        for rank, candidate in enumerate(ranked, start=1):
            key = _key(candidate)
            scores[key] = scores.get(key, 0.0) + 1.0 / (_RRF_K + rank)

            # A span found by several retrievers takes its strongest claim: if any
            # of them reached it deterministically, it is deterministic.
            current = tiers.get(key)
            tiers[key] = candidate.tier if current is None else min(current, candidate.tier)

            # The representative candidate is the strongest claim about this span:
            # the best tier first, and within a tier the highest raw score.
            existing = best.get(key)
            if existing is None or (candidate.tier, -candidate.score) < (
                existing.tier,
                -existing.score,
            ):
                best[key] = candidate

    if primary_repository:
        for key in scores:
            if key[0] == primary_repository:
                scores[key] += primary_boost

    ordered = sorted(scores, key=lambda key: (tiers[key], -scores[key], key))
    return [
        best[key].model_copy(update={"score": scores[key], "tier": tiers[key]}) for key in ordered
    ]


def _overlap(a: EvidenceCandidate, b: EvidenceCandidate) -> float:
    """Fraction of the shorter span that the two candidates share."""
    if a.repository != b.repository or a.path != b.path:
        return 0.0
    shared = min(a.line_end, b.line_end) - max(a.line_start, b.line_start) + 1
    if shared <= 0:
        return 0.0
    shortest = min(a.line_end - a.line_start + 1, b.line_end - b.line_start + 1)
    return shared / shortest if shortest else 0.0


def deduplicate(
    candidates: list[EvidenceCandidate], *, threshold: float = _OVERLAP_THRESHOLD
) -> list[EvidenceCandidate]:
    """Drop candidates that mostly repeat a higher-ranked span of the same file.

    Chunk windows overlap by design, so the same lines arrive several times. A
    candidate is dropped when it is contained in a kept one, or when it shares more
    than ``threshold`` of the shorter span — below that the extra lines are real
    additional context and are kept.

    Spans are never merged into a synthetic range: the citation must point at text
    that was actually retrieved (V1_SPEC §11).
    """
    kept: list[EvidenceCandidate] = []
    for candidate in candidates:
        redundant = any(
            (
                k.repository == candidate.repository
                and k.path == candidate.path
                and k.line_start <= candidate.line_start
                and k.line_end >= candidate.line_end
            )
            or _overlap(k, candidate) > threshold
            for k in kept
        )
        if not redundant:
            kept.append(candidate)
    return kept


def apply_budget(
    candidates: Sequence[EvidenceCandidate], *, max_items: int, token_budget: int
) -> tuple[list[EvidenceCandidate], bool, int]:
    """Bound the evidence set by item count **and** by tokens (V1_SPEC §14).

    Returns the kept candidates, whether anything was dropped, and the token
    estimate of what was kept. Both bounds are hard: whole repositories are never
    sent to a model.
    """
    kept: list[EvidenceCandidate] = []
    tokens = 0
    for candidate in candidates:
        if len(kept) >= max_items:
            return kept, True, tokens
        estimate = max(1, len(candidate.text) // 4)
        if tokens + estimate > token_budget and kept:
            return kept, True, tokens
        tokens += estimate
        kept.append(candidate)
    return kept, False, tokens
