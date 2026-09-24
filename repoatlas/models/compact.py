"""The compact presentation of evidence-bearing responses (``presentation: compact``).

A projection, never a second capability. Each compact response is built from the
full response the capability already produced, after the ledger recorded it, so
nothing about what was retrieved, recorded or verified can differ between the
two. What differs is only how often a repeated fact is written.

On LVL a twelve-item search carried about 5 KB of evidence text and 10 KB of
metadata about it. Every item repeated a 296-character facts version, a 64-hex
file hash and a 32-hex span digest; a grounding verdict echoed the answer it was
given twice and described the freshness of repositories nothing cited. None of
that is information an agent acts on per item, and all of it is still available:

* the **facts version** is written once per response, and a source file whose
  facts were built under a different one says so on its own row;
* a **file's content hash** is written once per file, in ``sources``;
* **span digests** stay in the investigation's ledger. Grounding checks a citation
  against the ledger's record, never against anything the caller sends back, so
  a digest in the response is something only the server ever reads. A file hash
  is not a substitute for one and is not offered as one;
* **freshness** describes the repositories the result draws on.

What every item keeps is what an agent needs to act on it: the id it cites, where
the text came from, the text, how it was matched, whether the text is a recorded
declaration rather than source, the entity key to inspect or walk from, and
whether the ledger still stands behind it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from repoatlas.models.capability import CapabilityScope
from repoatlas.models.evidence import MatchField, RetrievalTrace, SourcePreview, TierField
from repoatlas.models.freshness import FreshnessDepth, FreshnessState
from repoatlas.models.inspection import Coverage, InspectedFile
from repoatlas.models.investigation import EvidenceId, EvidenceStatus, LedgerSummary
from repoatlas.models.relationships import AppliedGraphLimits, EntityRef, GraphEdge, GraphPath
from repoatlas.models.retrieval import SuppressionOutcome, TermMatch
from repoatlas.models.verification import (
    AgentAnswer,
    CitationVerdict,
    EvidenceStatusView,
    GroundingFinding,
    GroundingGuarantee,
    GroundingVerdict,
)


class CompactEvidence(BaseModel):
    """One evidence item, without the provenance the ledger already holds.

    ``location`` is ``slug/path``, ``slug/path:line`` or ``slug/path:start-end``,
    exactly as in the full response, and the line range in it is the citable one.
    """

    model_config = ConfigDict(frozen=True)

    id: EvidenceId
    rank: int
    location: str
    preview: SourcePreview | None = None
    text: str = ""
    tier: TierField
    match: MatchField
    #: True when ``text`` is the declaration RepoAtlas recorded rather than bytes of
    #: the file; such an item carries no line range. Inspect ``entity_key`` for
    #: the source.
    declaration: bool = False
    why: str = ""
    entity_key: str | None = None
    truncated: bool = False
    status: EvidenceStatus = EvidenceStatus.VALID


class CompactSource(BaseModel):
    """A file some of the evidence came from, written once however many items did."""

    model_config = ConfigDict(frozen=True)

    repository_slug: str
    path: str
    content_hash: str
    #: Present only when this file's facts were built under a facts version other
    #: than the response's own ``facts_version``.
    facts_version: str | None = None


class CompactRepositoryFreshness(BaseModel):
    """How one repository's index stands, without V1's prose rendering of it."""

    model_config = ConfigDict(frozen=True)

    repository_slug: str
    state: FreshnessState
    outdated_files: int = 0
    last_indexed_at: datetime | None = None
    #: ``None`` — omitted — when this depth did not check.
    head_matches: bool | None = None
    working_tree_dirty: bool | None = None


class CompactFreshness(BaseModel):
    """Freshness of the repositories this result draws on.

    ``state`` is the worst of the repositories listed here, by the same rule the
    full response applies to its own list.
    """

    model_config = ConfigDict(frozen=True)

    depth: FreshnessDepth
    state: FreshnessState
    repositories: tuple[CompactRepositoryFreshness, ...] = ()
    repository_set_drifted: bool | None = None


class CompactSearchResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    presentation: Literal["compact"] = "compact"
    investigation_id: str
    scope: CapabilityScope
    repositories: tuple[str, ...] = ()
    within_path: str | None = None
    facts_version: str | None = None
    sources: tuple[CompactSource, ...] = ()
    evidence: tuple[CompactEvidence, ...] = ()
    #: Exact entity handles that seeded graph expansion, by key.
    entities: tuple[str, ...] = ()
    terms: tuple[TermMatch, ...] = ()
    suppressed: SuppressionOutcome | None = None
    #: Something was omitted or shortened for budget. The full response's
    #: ``has_more`` is always equal to this.
    truncated: bool = False
    #: Budget fields the server lowered to its ceiling; omitted when none were.
    budget_clamped: tuple[str, ...] | None = None
    freshness: CompactFreshness
    ledger: LedgerSummary
    trace: RetrievalTrace | None = None


class CompactInspectResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    presentation: Literal["compact"] = "compact"
    investigation_id: str
    target_evidence_status: EvidenceStatus | None = None
    #: The file, its content hash and its facts version, once.
    file: InspectedFile
    segments: tuple[CompactEvidence, ...] = ()
    coverage: Coverage
    budget_clamped: tuple[str, ...] | None = None
    freshness: CompactFreshness
    ledger: LedgerSummary


class CompactRelationshipsResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    presentation: Literal["compact"] = "compact"
    investigation_id: str
    target: EntityRef
    edges: tuple[GraphEdge, ...] = ()
    paths: tuple[GraphPath, ...] = ()
    facts_version: str | None = None
    sources: tuple[CompactSource, ...] = ()
    evidence: tuple[CompactEvidence, ...] = ()
    truncated: bool = False
    limits: AppliedGraphLimits
    freshness: CompactFreshness
    ledger: LedgerSummary


class CompactGroundingResponse(BaseModel):
    """A verdict, without the answer the caller already holds.

    The submitted answer is not echoed. ``sanitized_answer`` is included only when
    grounding removed or narrowed something, which ``answer_changed`` states either
    way. The verification performed is the full response's, unchanged.
    """

    model_config = ConfigDict(frozen=True)

    presentation: Literal["compact"] = "compact"
    investigation_id: str
    verdict: GroundingVerdict
    guarantee: GroundingGuarantee = GroundingGuarantee()
    answer_changed: bool = False
    sanitized_answer: AgentAnswer | None = None
    findings: tuple[GroundingFinding, ...] = ()
    citations: tuple[CitationVerdict, ...] = ()
    evidence_statuses: tuple[EvidenceStatusView, ...] = ()
    dropped_claims: int = 0
    dropped_citations: int = 0
    clamped_citations: int = 0
    ledger: LedgerSummary
    freshness: CompactFreshness
    checked_at: datetime
