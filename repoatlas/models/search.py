"""Wire models for the ``search`` capability (RepoAtlas V2).

Search is the first capability that hands an agent something it can cite, so two
distinctions are built into these types rather than left to the code that fills
them in.

The evidence vocabulary these responses are built from —
:class:`~repoatlas.models.capability_io.EvidenceItem`,
:class:`~repoatlas.models.capability_io.EntityHandle` and
:class:`~repoatlas.models.capability_io.ContextView` — is shared with every other
capability that supplies citable bytes and lives beneath all of them. Two
distinctions built into those types are what search depends on most, and both are
recorded here because search is where they first bite.

**This query, not this investigation.** An :class:`EvidenceItem` describes how
*this* retrieval run reached this span. The ledger separately accumulates the
strongest metadata any run has ever established for it, and the two legitimately
disagree: a span first proved by an exact path match and later returned again by
word-overlap recall is still proved, but the lexical run did not prove it. A
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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from repoatlas.compat import StrEnum
from repoatlas.models.capability import (
    PRESENTATION_DESCRIPTION,
    AppliedBudget,
    CapabilityContext,
    CapabilityScope,
    EvidenceBudget,
    Presentation,
)
from repoatlas.models.capability_io import ContextView, EntityHandle, EvidenceItem
from repoatlas.models.evidence import RetrievalTrace
from repoatlas.models.freshness import FreshnessDepth, IndexFreshness
from repoatlas.models.inspection import validate_path_shape
from repoatlas.models.investigation import LedgerSummary
from repoatlas.models.retrieval import SuppressionOutcome, TermMatch


class SearchMode(StrEnum):
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


class SearchRequest(BaseModel):
    """Ask what the index holds about a question."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_preview: bool = Field(
        default=False,
        description="Supply a bounded window of indexed source around matching lines, "
        "or the full definition when it fits. Includes omitted spans; declarations "
        "remain declarations when source lines cannot be proved. Opt-in; does not "
        "change ranking. Inspect the entity to expand a partial preview.",
    )
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
    #: A path scope inside the one repository the context names. Published with a
    #: description because an agent choosing whether to narrow a search reads the
    #: schema, not this comment, and the two properties that matter — it is applied
    #: before any candidate limit, and it never widens the repository scope — are
    #: exactly what it would otherwise have to guess.
    within_path: str | None = Field(
        default=None,
        description=(
            "Search only at or beneath this repository-relative path: a directory "
            "such as `services/identity` (a trailing slash is optional) or one file. "
            "It matches whole path segments, so `services/identity` never matches "
            "`services/identity-client`. Requires `scope: repository`; it narrows that "
            "repository and never reaches another. Every retrieval stage applies it "
            "before its candidate limits, so matches elsewhere cannot crowd out matches "
            "here. A path with no indexed file at or beneath it is refused with "
            "FILE_NOT_INDEXED. Environment variables, packages and HTTP operations have "
            "no location, so they are not returned under a path scope; search without "
            "one to obtain those handles."
        ),
    )
    #: Whether graph expansion also follows package-proven links into other repositories.
    #: Published with a description because an agent chooses it from the schema; the
    #: default keeps search to the cross-repository links it has always followed.
    cross_repository: bool = Field(
        default=False,
        description=(
            "Also follow, from exact matches, imports and references that a package "
            "link proves into another repository. Requires `scope: global`. Off by "
            "default: search follows only the cross-repository links it always has "
            "(manifest dependencies, paths, generated code), never a package-proven "
            "code link. repoatlas_impact follows those links without this."
        ),
    )
    #: How the response is written out. The capability runs identically either way.
    presentation: Presentation = Field(
        default=Presentation.FULL, description=PRESENTATION_DESCRIPTION
    )

    @model_validator(mode="after")
    def _cross_repository_needs_global_scope(self) -> SearchRequest:
        """A repository scope is a boundary; asking to cross it is refused, not ignored."""
        if self.cross_repository and self.context.scope is CapabilityScope.REPOSITORY:
            raise ValueError(
                "cross_repository requires scope: global; a repository scope is a boundary"
            )
        return self

    @field_validator("question")
    @classmethod
    def _question_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question must not be empty")
        return value

    @field_validator("within_path")
    @classmethod
    def _within_path_is_repository_relative(cls, value: str | None) -> str | None:
        """Refuse a path that is not inside a repository, before anything is searched.

        The same first gate inspection applies to a path target: absolute paths,
        drive letters, ``..`` segments and NUL are malformed requests rather than
        lookups that find nothing. A path that reduces to nothing (``.``, ``./``)
        names the whole repository, which is what omitting the field already means.
        """
        if value is None:
            return None
        validate_path_shape(value)
        if not [part for part in value.replace("\\", "/").split("/") if part not in ("", ".")]:
            raise ValueError(
                "within_path must name a file or directory inside the repository; "
                "omit it to search the whole repository"
            )
        return value


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
    #: The path scope this search was confined to, in the spelling the index
    #: stores (``services/identity``), or ``None`` when the whole repository scope
    #: was searched. Echoed so that a response read without its request still says
    #: that "nothing matched" meant "nothing matched *here*".
    within_path: str | None = None
    #: For each identifier, path or key the question named, what the exact
    #: lookups established about it in the coverage searched: per lookup in
    #: ``checks`` (``match``, ``no_match``, ``incomplete`` or ``not_checked``), and
    #: summarised in ``state``.
    #:
    #: The answer an existence question needs and the evidence list cannot give:
    #: evidence is ranked and budgeted, so a term's absence from twelve items says
    #: nothing. A negative here is only ever one a lookup established — ``not_found``
    #: needs every relevant lookup to have run to completion, and anything less is
    #: ``not_established``. They are still claims about the *index* — see
    #: :class:`~repoatlas.models.retrieval.TermMatch`. Words the question merely used
    #: are not listed.
    terms: tuple[TermMatch, ...] = ()
    #: How many package-proven links into other repositories this search did not
    #: follow. ``None`` when the request asked for ``cross_repository`` or there was
    #: none to report, so a response with nothing withheld reads as it always did.
    withheld_crossings: int | None = Field(
        default=None,
        description=(
            "How many imports and references that a package link proves into other "
            "repositories this search did not follow, among the links touching the "
            "nodes it expanded: a count of indexed links, not a judgement of relevance. "
            "Absent or null when none were withheld or cross_repository was set. To "
            "follow them, repeat the search with scope global and cross_repository true."
        ),
    )
