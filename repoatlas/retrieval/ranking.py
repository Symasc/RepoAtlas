"""Rank fusion (ARCHITECTURE.md §10 stages 5-8).

Reciprocal-rank fusion, because BM25 scores, literal-match scores and graph
distances are not commensurable and RRF needs no per-source calibration.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from repoatlas.models.evidence import EvidenceCandidate

#: RRF damping constant. 60 is the value from the original formulation.
_RRF_K = 60.0


class Reranker(Protocol):
    """The V2 seam. ``NoopReranker`` is what V1 ships."""

    def rerank(
        self, question: str, candidates: list[EvidenceCandidate]
    ) -> list[EvidenceCandidate]: ...


class NoopReranker:
    """Preserves fusion order. Replaced by a cross-encoder in V2."""

    def rerank(self, question: str, candidates: list[EvidenceCandidate]) -> list[EvidenceCandidate]:
        return candidates


def fuse(
    ranked_lists: Sequence[Sequence[EvidenceCandidate]],
    *,
    primary_repository: str | None = None,
    primary_boost: float = 0.5,
) -> list[EvidenceCandidate]:
    """Combine ranked lists, boosting the primary repository (V1_SPEC §6).

    The boost biases toward the repository the question was asked in without
    walling off other repositories, which is what "primary scope, not a wall" means.
    """
    scores: dict[tuple[str, str, int, int], float] = {}
    best: dict[tuple[str, str, int, int], EvidenceCandidate] = {}

    for ranked in ranked_lists:
        for rank, candidate in enumerate(ranked, start=1):
            key = (candidate.repository, candidate.path, candidate.line_start, candidate.line_end)
            scores[key] = scores.get(key, 0.0) + 1.0 / (_RRF_K + rank)
            existing = best.get(key)
            if existing is None or candidate.score > existing.score:
                best[key] = candidate

    for key in scores:
        if primary_repository and key[0] == primary_repository:
            scores[key] += primary_boost

    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    return [best[key].model_copy(update={"score": score}) for key, score in ordered]


def deduplicate(candidates: list[EvidenceCandidate]) -> list[EvidenceCandidate]:
    """Drop candidates whose line range is contained by a higher-ranked one."""
    kept: list[EvidenceCandidate] = []
    for candidate in candidates:
        overlapped = any(
            k.repository == candidate.repository
            and k.path == candidate.path
            and k.line_start <= candidate.line_start
            and k.line_end >= candidate.line_end
            for k in kept
        )
        if not overlapped:
            kept.append(candidate)
    return kept
