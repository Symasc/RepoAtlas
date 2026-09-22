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

from pydantic import BaseModel, ConfigDict, Field

from repoatlas.models.evidence import EvidenceSet


class TermLookup(enum.StrEnum):
    """One exact lookup a named term can be put through.

    Each asks the indexed coverage searched — the repository scope and any
    ``within_path`` — and none is a ranking: what it establishes about a term is
    independent of what the result budget later keeps. Whether a given lookup ran,
    and ran to completion, is recorded per term in :attr:`TermMatch.checks`.
    """

    #: The term occurs verbatim in indexed text, possibly inside a longer token.
    TEXT = "text"
    #: An indexed file's whole path, trailing sub-path or basename equals it.
    FILE_PATH = "file_path"
    #: An indexed entity's name, qualified name or key equals it.
    ENTITY_NAME = "entity_name"
    #: An indexed entity's natural key equals it (an HTTP route or endpoint key).
    ENTITY_KEY = "entity_key"


class LookupOutcome(enum.StrEnum):
    """What one lookup established about one term."""

    #: An indexed entity, file or text matches the term exactly.
    MATCH = "match"
    #: The lookup ran over the whole indexed coverage searched and nothing matched.
    NO_MATCH = "no_match"
    #: The lookup ran, found no match, and stopped at its limit before it could
    #: have seen every candidate — so it establishes nothing about absence, and
    #: never decides suppression. The text lookup is the one that can end this
    #: way, when its store reports that it did not read everything it could; a
    #: lookup whose limit admitted near misses before checking them exactly, such
    #: as ``api_key`` for ``API_KEY``, is that shape.
    INCOMPLETE = "incomplete"
    #: The lookup never ran for this term.
    NOT_CHECKED = "not_checked"


class TermState(enum.StrEnum):
    """What the checks establish about one named term, as one word.

    Derived from :attr:`TermMatch.checks` and never from the evidence list, a
    ranking or a candidate budget. A negative state is only ever the conjunction
    of negatives that were actually established.
    """

    #: An indexed entity or file is named exactly this.
    EXACT = "exact"
    #: It occurs verbatim in indexed text, and the file and entity lookups ran to
    #: completion without a match. ``resetPassword`` inside ``resetPasswordAllowed``
    #: is this, and nothing more.
    TEXT_ONLY = "text_only"
    #: Every lookup relevant to the term ran to completion and matched nothing in
    #: the indexed coverage searched.
    NOT_FOUND = "not_found"
    #: No lookup matched, and at least one relevant lookup did not run or did not
    #: finish, so neither presence nor absence is established. ``checks`` says what
    #: was: ``architecture`` asked of ``docs`` is no file's name and no entity's,
    #: and whether the document's text contains it was never asked.
    NOT_ESTABLISHED = "not_established"


class TermMatch(BaseModel):
    """What the exact lookups established about one term the question named.

    A statement about the index, never about the repository at large, and only
    about the lookups that ran. Everything is relative to the coverage searched:
    the repository scope, narrowed by ``within_path`` when one was given, as indexed
    at the response's freshness. A file the index excludes, cannot read, holds as
    binary, or has not synced since it changed is outside that coverage.

    Which lookups are relevant depends on what kind of term it is. A term the
    question spelled as a literal — camelCase, snake_case, a dotted, quoted or
    dashed name, a path — is searched as text, as a file path and as an entity
    name. A ``PascalCase`` or otherwise named word is looked up as a file path and
    an entity name only, so its text is ``not_checked`` and it can be ``exact`` or
    ``not_established`` but never ``not_found``. An HTTP key is looked up as an
    entity key and nothing else.
    """

    model_config = ConfigDict(frozen=True)

    term: str
    state: TermState
    #: The lookups that ran for this term: every key of ``checks`` whose outcome is
    #: not ``not_checked``, in the same order.
    lookups: tuple[TermLookup, ...] = ()
    #: Every lookup relevant to this term, and what it established.
    checks: dict[TermLookup, LookupOutcome] = Field(default_factory=dict)


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
    #: every other name it spelled as a name — what the exact lookups established.
    #: Built from the lookups the funnel already made; the only lookup that runs for
    #: it alone is a term's own exact file or entity lookup, asked when a lookup
    #: shared with other terms filled its limit before that term matched.
    term_matches: tuple[TermMatch, ...] = ()
