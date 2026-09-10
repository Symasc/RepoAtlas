"""Wire models for the ``search`` capability (RepoAtlas V2).

Search is the first capability that hands an agent something it can cite, so two
distinctions are built into these types rather than left to the code that fills
them in.

**This query, not this investigation.** An :class:`EvidenceItem` describes how
*this* retrieval run reached this span. The ledger separately accumulates the
strongest metadata any run has ever established for it, and the two legitimately
disagree: a span first proved by an exact path match and later returned again by a
semantic neighbour is still proved, but the semantic run did not prove it. A
response built by serializing ledger entries would report the historical strength
as though this query had earned it, which is a claim about the search that is not
true. So the item carries the current run's tier, match and reason, and the
:class:`~repoatlas.models.investigation.LedgerEntry` keeps the accumulated view.

**Only lines whose text was supplied.** ``ref`` is the citable span, and it never
exceeds the complete lines actually present in ``text``. Truncation narrows it;
evidence whose text cannot be mapped to its source lines claims no line at all.
Grounding checks a citation against supplied spans, so a span wider than the text
would authorize citing lines the agent was never shown.

Nothing here carries a database row id, a host path or a fused score. Rank, tier
and match are the whole of the public strength vocabulary.
"""

from __future__ import annotations

import enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from repoatlas.models.capability import (
    AppliedBudget,
    CapabilityContext,
    CapabilityScope,
    EvidenceBudget,
)
from repoatlas.models.evidence import EvidenceTier, MatchClass, RetrievalTrace
from repoatlas.models.freshness import FreshnessDepth, IndexFreshness
from repoatlas.models.investigation import EvidenceId, EvidenceRef, EvidenceStatus, LedgerSummary
from repoatlas.models.parsing import EntityType
from repoatlas.models.retrieval import SuppressionOutcome


class SearchMode(enum.StrEnum):
    """How widely to run the same composed funnel.

    Not two algorithms. Both modes run the identical retrieval pipeline with the
    identical trust rules; the mode chooses how much it is allowed to gather, and
    whether the whole question is also searched as a literal phrase.
    """

    #: What ``locate`` retrieves: a direct lookup.
    LOOKUP = "lookup"
    #: What ``debug`` retrieves: wider bounds, plus the whole question searched
    #: verbatim, which is how an unquoted error string embedded in source is found
    #: (V1_SPEC §16).
    DIAGNOSTIC = "diagnostic"


class EntityHandle(BaseModel):
    """A natural, reusable identity for an entity behind a piece of evidence.

    The point of it is the next call: an agent that found ``env:SERVICE_HOST``
    should be able to ask about its relationships without a second fuzzy lookup
    that might resolve to something else. The key is exact and is what the graph
    capability will accept verbatim.

    Never a database id. A row id is allocation order, so a clean reindex of the
    same tree produces different ones, and a handle stored across a reindex would
    silently start naming a different entity — or nothing.
    """

    model_config = ConfigDict(frozen=True)

    #: The natural key, e.g. ``env:SERVICE_HOST``, ``module:src.config``,
    #: ``sym:python-service:src/config.py:load_config:18``.
    key: str
    type: EntityType
    #: Whether the key embeds a source position and therefore stops resolving when
    #: the definition moves. ``sym:`` keys do; interned identities such as ``env:``
    #: and ``module:`` do not. Published so a caller knows which handles are safe
    #: to keep across a sync, rather than discovering it from a failed lookup.
    positional: bool = False


class EvidenceItem(BaseModel):
    """One piece of evidence, as this search run found it.

    ``text`` is exactly what the caller was shown, and ``ref.span_digest`` is a
    digest of exactly these characters. ``ref``'s line range covers only complete
    lines present in ``text``.
    """

    model_config = ConfigDict(frozen=True)

    #: Ledger identity, valid inside this investigation only.
    id: EvidenceId
    #: 1-based position in **this** response, after budget shaping. Not a score,
    #: not stable across queries, and never leaves gaps: if a candidate could not
    #: fit, the ranks that follow are still consecutive because nothing follows.
    rank: int = Field(gt=0)
    ref: EvidenceRef
    #: Display name of the repository. Not unique; ``ref.repository_slug`` is the
    #: identity.
    repository_name: str = ""
    #: ``slug/path``, ``slug/path:line`` or ``slug/path:start-end``. Derived from
    #: ``ref``, so it can never describe a wider span than was supplied.
    location: str = ""
    #: The supplied text. Empty for file-identity evidence, which claims the file
    #: exists and nothing about any line.
    text: str = ""
    #: True when this candidate was shortened to fit the budget. More of *this*
    #: source existed and was not returned.
    truncated: bool = False

    #: Why **this run** matched it, in the retriever's words. Diagnostic prose.
    why: str = ""
    #: Strength as established by **this run**. The ledger may hold something
    #: stronger from an earlier query; that is the ledger's view, not this one's.
    tier: EvidenceTier = EvidenceTier.DETERMINISTIC
    match: MatchClass = MatchClass.EXACT_CONTENT

    #: The entity behind an entity-backed result, when one resolves exactly.
    #: ``None`` is a truthful answer and never a fabricated key.
    entity: EntityHandle | None = None
    #: Whether the ledger still stands behind this id. ``VALID`` at allocation;
    #: an id re-supplied after a revalidation may carry something else.
    status: EvidenceStatus = EvidenceStatus.VALID


class SearchRequest(BaseModel):
    """Ask what the index holds about a question."""

    model_config = ConfigDict(frozen=True)

    question: str
    context: CapabilityContext
    budget: EvidenceBudget = EvidenceBudget()
    mode: SearchMode = SearchMode.LOOKUP
    #: An id RepoAtlas issued earlier, to continue that investigation. Omit to
    #: begin a new one. A client **echoes** an id; it never invents one, and an id
    #: this server did not issue is refused rather than created.
    investigation_id: str | None = None
    #: Stage instrumentation. Diagnostic only — every fact a caller needs to act
    #: on has a first-class field, and search is fully usable with this off.
    include_trace: bool = False
    #: Freshness depth for the index being searched. ``STORED`` costs one indexed
    #: read per repository; ``LIVE`` adds a git invocation per repository, which a
    #: retrieval call should not pay for unless asked.
    depth: FreshnessDepth = FreshnessDepth.STORED

    @field_validator("question")
    @classmethod
    def _question_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question must not be empty")
        return value


class ContextView(BaseModel):
    """What the context resolved to, as identities rather than rows.

    A transport-neutral projection of
    :class:`~repoatlas.models.capability.ContextResolution`, which carries whole
    ``Repository`` objects and therefore their database ids. The caller needs to
    know *what was searched*; it must not receive a surrogate id to store.
    """

    model_config = ConfigDict(frozen=True)

    context: CapabilityContext
    scope: CapabilityScope
    #: Canonical identities of the repositories in scope, ordered by slug.
    repositories: tuple[str, ...] = ()
    #: The repository the question was asked "in", if any. Retrieval biases
    #: towards it; it is not a wall, and evidence from elsewhere may still rank.
    primary: str | None = None
    #: Human-readable account of how the scope was decided. Never a discriminator.
    reason: str = ""


class SearchResponse(BaseModel):
    """Evidence for one question, recorded in one investigation."""

    model_config = ConfigDict(frozen=True)

    #: The investigation this evidence belongs to. Echo it on the next call to
    #: keep citing these ids; omit it to start a new investigation.
    investigation_id: str
    question: str
    context: ContextView
    #: In trusted rank order. Every item is recorded in the ledger before this
    #: response exists, so every id here resolves.
    evidence: tuple[EvidenceItem, ...] = ()
    #: Freshness of the index that was searched. Metadata, not a gate: searching a
    #: stale index is allowed and is labelled rather than refused.
    freshness: IndexFreshness
    #: What the server actually granted, including any clamp it applied.
    budget: AppliedBudget
    #: True when anything was omitted or shortened for budget — by this layer's
    #: exact character enforcement **or** by the funnel's own internal bound.
    #:
    #: Both stages are folded together deliberately. A caller's ``max_items`` is
    #: handed down and enforced inside retrieval, so a flag that reported only
    #: what this layer cut would say ``False`` for a request that was in fact
    #: narrowed by exactly the number the caller chose — which reads as "you have
    #: everything".
    truncated: bool = False
    #: The same fact, named for the question a caller actually asks: is there more
    #: to get? Equal to :attr:`truncated` by construction, and kept as its own
    #: field because "this response was cut" and "more exists" are read at
    #: different moments and a caller should not have to know they coincide.
    #: Per-item detail lives on :attr:`EvidenceItem.truncated`.
    has_more: bool = False
    #: Set when the question named something definite that exists nowhere in the
    #: index. Then empty evidence is the truthful answer rather than a failure,
    #: and associative near-misses are deliberately withheld.
    suppressed: SuppressionOutcome | None = None
    #: Present only when the request asked for it. Diagnostic instrumentation,
    #: **not** a ranking contract: its internal counters and scores may change
    #: between versions and nothing should branch on them.
    trace: RetrievalTrace | None = None
    #: Exact entity-name hits that seeded graph expansion, as reusable handles.
    #:
    #: Separate from :attr:`EvidenceItem.entity` because the most useful handles
    #: are precisely the ones that have no evidence item. An interned identity such
    #: as ``env:SERVICE_HOST`` has no file and no line, so V1 admits it as a graph
    #: seed and produces no candidate for it — and it is exactly the hub a caller
    #: then wants to ask about. Manufacturing an evidence row to carry it would
    #: invent a citation for something no file supplies.
    #:
    #: Exact-name matches only, which is not a choice made here: it is the set the
    #: retriever admits as seeds, and the rule that a fuzzy match may not seed
    #: graph expansion is what bounds this list too.
    entities: tuple[EntityHandle, ...] = ()
    #: The investigation's ledger after this call.
    ledger: LedgerSummary
