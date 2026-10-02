"""Span helpers the capability tests share."""

from __future__ import annotations

from repoatlas.capabilities.evidence_shaping import provable_span
from repoatlas.capabilities.search import offered_lines
from repoatlas.models.evidence import EvidenceCandidate


def provable_line_span(candidate: EvidenceCandidate) -> tuple[int, int] | None:
    """The source lines this candidate's text demonstrably covers, or ``None``.

    :func:`~repoatlas.capabilities.evidence_shaping.provable_span` applied to the
    range :func:`~repoatlas.capabilities.search.offered_lines` allows.
    """
    return provable_span(
        candidate.text, *offered_lines(candidate), split_total=candidate.split_total
    )
