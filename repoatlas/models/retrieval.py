"""What one retrieval run computed, beyond the evidence it returned.

``EvidenceSet`` is the V1 contract and its shape is frozen: it is what ``locate``
prints and what ``--json`` emits. But the funnel decides more than it can carry.
It decides whether the associative half of retrieval was suppressed, and it
collects the exact entity hits that seeded graph expansion — and both used to
survive only as an English sentence in a trace field.

These models are that data, structured, produced by the same single pass. A
caller reads the outcome; nobody re-derives it by parsing a trace string, and
nobody runs the funnel twice to learn two things about one question.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from repoatlas.models.evidence import EvidenceSet


class SuppressionOutcome(BaseModel):
    """Why every associative source was held back for this question.

    When the question names literal terms and none of them exist anywhere in the
    index, the honest answer is that they do not exist. Suppression is that
    decision, made once and honoured by every stage that follows.

    ``message`` is the sentence the retrieval trace shows. It is produced here and
    read from here, so the trace is derived from the structured decision rather
    than the structured decision being recovered from the trace.
    """

    model_config = ConfigDict(frozen=True)

    reason: Literal["named_terms_absent"] = "named_terms_absent"
    #: The definite terms that were searched and not found. A bare dashed token
    #: lifted out of prose is searched exactly but never gets to declare a
    #: question unanswerable, so it does not appear here.
    terms: tuple[str, ...] = ()
    message: str


class RetrievalOutcome(BaseModel):
    """One retrieval run: the evidence, and what the evidence cannot carry."""

    model_config = ConfigDict(frozen=True)

    evidence: EvidenceSet
    suppression: SuppressionOutcome | None = None
    #: Entity ids of the **exact**-name hits that seeded graph expansion. Internal
    #: by construction: a raw row id is not an identity, so a caller that crosses a
    #: process boundary must resolve these to natural keys first.
    seed_entity_ids: tuple[int, ...] = ()
