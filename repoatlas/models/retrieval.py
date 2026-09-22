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

import enum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from repoatlas.models.evidence import EvidenceSet


class TermLookup(enum.StrEnum):
    """One exhaustive exact lookup a named term was put through.

    Each is exhaustive over the indexed coverage searched — the repository scope
    and any ``within_path`` — and none is a ranking: a term either matches in it or
    does not, whatever the result budget later keeps.
    """

    #: The term occurs verbatim in indexed text, possibly inside a longer token.
    TEXT = "text"
    #: An indexed file's whole path, trailing sub-path or basename equals it.
    FILE_PATH = "file_path"
    #: An indexed entity's name, qualified name or key equals it.
    ENTITY_NAME = "entity_name"
    #: An indexed entity's natural key equals it (an HTTP route or endpoint key).
    ENTITY_KEY = "entity_key"


class TermState(enum.StrEnum):
    """What the lookups found for one named term, strongest first."""

    #: An indexed entity or file is named exactly this.
    EXACT = "exact"
    #: It occurs verbatim in indexed text, but no indexed entity or file is named it.
    #: ``resetPassword`` inside ``resetPasswordAllowed`` is this, and nothing more.
    TEXT_ONLY = "text_only"
    #: Every lookup in ``lookups`` came back empty in the indexed coverage searched.
    NOT_FOUND = "not_found"


class TermMatch(BaseModel):
    """Whether one term the question named is in the indexed coverage searched.

    A statement about the index, never about the repository at large. ``not_found``
    means the lookups listed in ``lookups`` found nothing in what was searched: the
    repository scope, narrowed by ``within_path`` when one was given, as indexed at
    the response's freshness. A file the index excludes, cannot read, holds as
    binary, or has not synced since it changed is outside that coverage, and a
    lookup not listed was not made — a ``PascalCase`` name, for instance, is looked
    up as an entity name but not searched for as text.
    """

    model_config = ConfigDict(frozen=True)

    term: str
    state: TermState
    lookups: tuple[TermLookup, ...] = ()


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
    #: For each term the question named — every literal term, every HTTP key, and
    #: every other name it spelled as a name — what the exact lookups found. Built
    #: from the proofs the funnel already made; no lookup runs for it alone.
    term_matches: tuple[TermMatch, ...] = ()
