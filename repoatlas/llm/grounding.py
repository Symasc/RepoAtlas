"""Compatibility shim. Grounding moved to :mod:`repoatlas.grounding`.

The enforcement point is not part of the LLM layer. It reads an ``EvidenceSet``
and a model answer and returns a narrowed answer; it calls no model, and the
capability layer needs it without importing ``llm/`` at all. It therefore sits at
the top level beside ``security`` and ``containment``.

This module keeps the old import path working. Nothing new should use it.
"""

from repoatlas.grounding import (
    INSUFFICIENT_EVIDENCE,
    GroundingReport,
    SuppliedEvidence,
    ground_answer,
)

__all__ = [
    "INSUFFICIENT_EVIDENCE",
    "GroundingReport",
    "SuppliedEvidence",
    "ground_answer",
]
