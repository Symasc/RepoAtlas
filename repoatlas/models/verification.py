"""Wire models for the ``verify_grounding`` capability (RepoAtlas V2).

This module names one guarantee and refuses every stronger one that sounds like
it. The distinction it exists to hold is short enough to state twice:

    RepoAtlas verifies **grounding and provenance**. It does not verify semantic
    entailment, factual truth, or reasoning quality.

A ``GROUNDED`` verdict says that every citation an answer made names evidence
this investigation actually supplied, at coordinates inside the span that was
supplied under that identity, and that the evidence still corresponds to the
indexed snapshot. It says nothing whatever about whether the cited lines support
the sentence attached to them. A deliberately false statement carrying a real
citation is ``GROUNDED``, and :mod:`tests.capabilities.test_verify_grounding`
asserts exactly that, because the day someone "fixes" it is the day the verdict
stops meaning anything checkable.

That is not modesty. It is the only guarantee RepoAtlas is in a position to make:
verification consults no model, and the ledger retains digests rather than text,
so entailment is not merely unproven here — it is uncomputable on this path.

Three consequences are built into the types.

**Evidence identity is the unit of provenance.** A citation carries an
:class:`AgentCitation.evidence_id`, and the id is what proves the coordinates
were supplied. Repository, path and line fields may accompany it, and when they
do they must agree with what that id permits; they never stand in for it. A
caller cannot name coordinates that exist in the index but were never handed over.

**Grounding failure is a finding, not an error.** Citing evidence that was never
issued, or that has gone stale, is the question the caller asked this capability
to answer. It comes back as a :class:`GroundingFinding` inside a successful
response, so one answer can hold a real citation and a fabricated one and be
correctly reported as :attr:`GroundingVerdict.PARTIALLY_GROUNDED`.

**Nothing here is a score.** There is no confidence RepoAtlas produces, no tier
read as strength, no match class read as entailment. The one number that appears
is :attr:`AgentClaim.claimed_confidence`, which belongs to the claimant, travels
through unchanged or lowered, and is never raised.
"""

from __future__ import annotations

import enum
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from repoatlas.models.capability import PRESENTATION_DESCRIPTION, Presentation
from repoatlas.models.capability_io import LineRange
from repoatlas.models.evidence import EvidenceSource
from repoatlas.models.freshness import IndexFreshness
from repoatlas.models.investigation import (
    EvidenceId,
    EvidenceStatus,
    LedgerSummary,
)

#: Ceiling on how much answer one call may submit. Verification is cheap and
#: deterministic, but it is still a public entry point taking caller-supplied
#: text, and an unbounded one is a resource the caller controls.
MAX_CLAIMS = 200
MAX_CITATIONS_PER_CLAIM = 50
MAX_UNKNOWNS = 200


class GroundingVerdict(enum.StrEnum):
    """What verification concluded about an answer's **citations**.

    Deliberately not ``SUPPORTED`` / ``UNSUPPORTED``. Those words name a relation
    between evidence and meaning, and this capability establishes a relation
    between a citation and a ledger. V1's internal grounding report does use the
    word "unsupported" for its own removals; that vocabulary stays where it is and
    is translated here rather than propagated.
    """

    #: Every claim survived with every citation resolving exactly to evidence
    #: supplied in this investigation, inside the span supplied under that id.
    GROUNDED = "grounded"
    #: Some claims grounded and some did not — including a claim whose citation
    #: had to be narrowed to stay inside what was supplied.
    PARTIALLY_GROUNDED = "partially_grounded"
    #: No claim grounded, or the answer made no citation at all.
    UNGROUNDED = "ungrounded"


class GroundingFindingCode(enum.StrEnum):
    """Why one citation or claim did not ground.

    Every member names something the implementation can *prove* from the ledger
    and the indexed snapshot. There is deliberately no ``CLAIM_FALSE``,
    ``DOES_NOT_ENTAIL`` or ``WEAK_SUPPORT``: those are judgments about meaning,
    and nothing in this capability is entitled to make one.
    """

    #: No such evidence id was ever issued **in this investigation**. Another
    #: investigation may hold that id; it was not consulted and must not be.
    UNKNOWN_EVIDENCE = "unknown_evidence"
    #: The indexed file's content hash or facts version moved since it was supplied.
    STALE_EVIDENCE = "stale_evidence"
    #: Different bytes were later supplied for the same span under a newer id.
    #: Not followed to that id: citing it is the caller's decision to make.
    SUPERSEDED_EVIDENCE = "superseded_evidence"
    #: No indexed file stands at that identity and path any more.
    INVALID_EVIDENCE = "invalid_evidence"
    #: Revalidation established nothing about this entry's file.
    UNVERIFIED_EVIDENCE = "unverified_evidence"
    #: The cited lines lie entirely outside the span supplied under that id.
    CITATION_OUTSIDE_SUPPLIED_SPAN = "citation_outside_supplied_span"
    #: The cited lines overlapped the supplied span but reached past one end, so
    #: grounding narrowed them to the part that was actually supplied. The claim
    #: survives; the citation the caller submitted was not the one verified.
    CITATION_CLAMPED_TO_SUPPLIED_SPAN = "citation_clamped_to_supplied_span"
    #: The repository or path submitted alongside the id is not the one that id
    #: refers to. The reference is authoritative; the caller's copy is not.
    CITATION_SOURCE_MISMATCH = "citation_source_mismatch"
    #: A line range was cited against evidence that supplied no lines — a
    #: file-identity match knows the file exists and nothing about any line of it.
    CITATION_WITHOUT_SUPPLIED_LINES = "citation_without_supplied_lines"
    #: The claim carried no citation, so there is no provenance to check.
    MISSING_CITATION = "missing_citation"
    #: Free text named a repository object that was never supplied in this
    #: investigation. V1's secondary defence against a fabricated path in prose.
    PROSE_REFERENCE_NOT_SUPPLIED = "prose_reference_not_supplied"


class GroundingGuarantee(BaseModel):
    """Frozen constants travelling on every response.

    Emitted so that a client cannot present a grounding verdict as a truth verdict
    by accident, and so that the boundary is machine-readable rather than a
    sentence in documentation somebody may not have read.
    """

    model_config = ConfigDict(frozen=True)

    checked: tuple[str, ...] = (
        "citation_names_evidence_issued_in_this_investigation",
        "citation_lies_within_the_span_supplied_under_that_id",
        "citation_source_matches_the_evidence_reference",
        "evidence_still_corresponds_to_the_indexed_snapshot",
        "free_text_names_no_unsupplied_repository_object",
        "claimed_confidence_was_never_raised",
    )
    not_checked: tuple[str, ...] = (
        "semantic_entailment",
        "factual_accuracy_of_the_statement",
        "quality_of_the_reasoning",
        "completeness_of_the_investigation",
        "freshness_of_the_index_against_the_working_tree",
    )


# ---------------------------------------------------------------------------
# What the agent submits
# ---------------------------------------------------------------------------


class AgentCitation(BaseModel):
    """One citation: an evidence identity, optionally narrowed.

    ``evidence_id`` is required and is the whole point. It is what proves these
    coordinates were supplied to *this* investigation, which is a fact no
    repository path can establish on its own — the index is full of lines nobody
    was ever shown.

    ``repository_slug``, ``path``, ``line_start`` and ``line_end`` are optional
    and are checked against the reference rather than trusted. Omitting the line
    range cites the whole span that was supplied under that id.
    """

    model_config = ConfigDict(frozen=True)

    #: Ledger identity, valid inside the named investigation only.
    evidence_id: EvidenceId
    #: Canonical identity of the repository, when the caller states it. Checked
    #: against the evidence reference; never used to resolve the citation.
    repository_slug: str | None = None
    #: Repository-relative path, when the caller states it. Checked, not trusted.
    path: str | None = None
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> AgentCitation:
        start, end = self.line_start, self.line_end
        if start is not None and end is not None and end < start:
            raise ValueError(f"citation ends before it starts: {start}-{end}")
        return self

    @property
    def narrowed(self) -> bool:
        """Whether the caller asked for a sub-range rather than the whole span."""
        return self.line_start is not None or self.line_end is not None


class AgentClaim(BaseModel):
    """One statement the agent wants to make, and what it rests on.

    The statement is free text and is never read for meaning. It is scanned for
    repository objects it names — a path in prose reads as provenance to a human,
    so a fabricated one is a fabrication wherever it sits — and that is the only
    attention its content receives.
    """

    model_config = ConfigDict(frozen=True)

    statement: str
    #: The claimant's own number. Carried so grounding can **lower** it when a
    #: citation had to be narrowed, which is V1's third grounding rule. Never a
    #: RepoAtlas judgment, never raised, and not a probability that the statement
    #: is true.
    claimed_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    citations: tuple[AgentCitation, ...] = ()

    @model_validator(mode="after")
    def _bounded(self) -> AgentClaim:
        if len(self.citations) > MAX_CITATIONS_PER_CLAIM:
            raise ValueError(
                f"a claim may carry at most {MAX_CITATIONS_PER_CLAIM} citations, "
                f"got {len(self.citations)}"
            )
        return self


class AgentAnswer(BaseModel):
    """The answer submitted for grounding verification.

    Untrusted text throughout. Nothing in any field can reach the index, create
    evidence, change an evidence status except through honest revalidation, or
    redirect which investigation is read. Citations come from
    :attr:`AgentClaim.citations` and from nowhere else: a sentence that happens to
    contain the characters ``E12`` is prose, not a citation.
    """

    model_config = ConfigDict(frozen=True)

    summary: str = ""
    claims: tuple[AgentClaim, ...] = ()
    #: What the agent says it could not establish. Checked for fabricated objects
    #: — the one field whose purpose is admitting ignorance may not smuggle in a
    #: file that does not exist.
    unknowns: tuple[str, ...] = ()
    next_check: str | None = None

    @model_validator(mode="after")
    def _bounded(self) -> AgentAnswer:
        if len(self.claims) > MAX_CLAIMS:
            raise ValueError(f"an answer may carry at most {MAX_CLAIMS} claims")
        if len(self.unknowns) > MAX_UNKNOWNS:
            raise ValueError(f"an answer may carry at most {MAX_UNKNOWNS} unknowns")
        return self


class VerifyGroundingRequest(BaseModel):
    """Verify one answer's citations against one investigation's ledger.

    ``investigation_id`` is **required**. Evidence ids are session-scoped, so a
    verification without one would be checking ids against a ledger nobody named:
    there is no global ``E1``, and inventing an empty investigation to look in
    would report every citation as fabricated while implying the caller's ids were
    wrong.
    """

    model_config = ConfigDict(frozen=True)

    investigation_id: str = Field(min_length=1)
    answer: AgentAnswer
    #: How the verdict is written out. The verification is identical either way.
    presentation: Presentation = Field(
        default=Presentation.FULL,
        description=(
            f"{PRESENTATION_DESCRIPTION} For verification, `compact` also leaves out the "
            "submitted answer, which the caller already holds, and includes "
            "`sanitized_answer` only when grounding removed or narrowed something, as "
            "`answer_changed` says."
        ),
    )


# ---------------------------------------------------------------------------
# What verification reports
# ---------------------------------------------------------------------------


class GroundingFinding(BaseModel):
    """One machine-readable reason something did not ground.

    Callers branch on :attr:`code`. :attr:`detail` is prose for a human reading a
    transcript and carries no contract.
    """

    model_config = ConfigDict(frozen=True)

    code: GroundingFindingCode
    #: The evidence the finding is about, when it is about one.
    evidence_id: EvidenceId | None = None
    #: 0-based position of the claim in the submitted answer, when applicable.
    claim_index: int | None = Field(default=None, ge=0)
    #: 0-based position of the citation within that claim, when applicable.
    citation_index: int | None = Field(default=None, ge=0)
    #: The evidence's status at the moment of this verification, when known.
    status: EvidenceStatus | None = None
    detail: str = ""


class CitationVerdict(BaseModel):
    """What became of one submitted citation.

    ``verified_range`` is what grounding was willing to stand behind. When it
    differs from what was submitted, the citation was narrowed to stay inside the
    supplied span, and :attr:`grounded` is ``False``: the caller's citation is not
    the one that checks out.
    """

    model_config = ConfigDict(frozen=True)

    claim_index: int = Field(ge=0)
    citation_index: int = Field(ge=0)
    evidence_id: EvidenceId
    grounded: bool = False
    #: Status of the cited evidence against the snapshot this call read.
    status: EvidenceStatus | None = None
    #: The range the caller submitted, or ``None`` when it cited the whole span.
    submitted_range: LineRange | None = None
    #: The range grounding accepted, or ``None`` when nothing was accepted.
    verified_range: LineRange | None = None
    #: ``slug/path`` or ``slug/path:start-end`` of the evidence reference. Derived
    #: from the ledger, so it can never describe a span that was not supplied.
    location: str = ""
    code: GroundingFindingCode | None = None


class EvidenceStatusView(BaseModel):
    """One cited entry's standing against the indexed snapshot this call read.

    Reported for every distinct evidence id the answer cited, including ids that
    resolved to nothing, so a caller can see what each citation rested on without
    a second call.
    """

    model_config = ConfigDict(frozen=True)

    evidence_id: EvidenceId
    #: ``None`` when this investigation never issued the id.
    status: EvidenceStatus | None = None
    location: str = ""
    #: Which capabilities supplied this span. Diagnostic only: origin has no
    #: bearing on validity, and search evidence is not privileged over graph,
    #: inspection or dependency evidence.
    origins: tuple[EvidenceSource, ...] = ()
    #: The id that immediately replaced this one, when it was superseded. Reported
    #: so a caller can choose to cite it; never followed automatically.
    superseded_by: EvidenceId | None = None


class VerifyGroundingResponse(BaseModel):
    """The verdict, what it rests on, and what it deliberately does not cover.

    :attr:`sanitized_answer` is the submitted answer with the material grounding
    could not stand behind removed, exactly as V1's algorithm removes it. It is
    **not** a corrected answer: nothing was rewritten to be true, no statement was
    improved, and a surviving statement may still be false. Sanitized means
    citation-grounding failures were removed or narrowed — no more than that.
    """

    model_config = ConfigDict(frozen=True)

    investigation_id: str
    verdict: GroundingVerdict
    #: The contract, on every response, so it travels with the verdict.
    guarantee: GroundingGuarantee = GroundingGuarantee()
    #: Exactly what was submitted, returned unchanged for comparison.
    original_answer: AgentAnswer
    sanitized_answer: AgentAnswer
    findings: tuple[GroundingFinding, ...] = ()
    citations: tuple[CitationVerdict, ...] = ()
    evidence_statuses: tuple[EvidenceStatusView, ...] = ()
    #: Counts, mirroring V1's grounding report. Removals, not disagreements.
    dropped_claims: int = 0
    dropped_citations: int = 0
    clamped_citations: int = 0
    ledger: LedgerSummary
    #: How the index as a whole stands. Reported **beside** the verdict and never
    #: folded into it: evidence can correspond exactly to an indexed snapshot that
    #: is itself behind the working tree, and that evidence is groundable. Read at
    #: stored depth, so verification invokes no git and touches no working tree.
    freshness: IndexFreshness
    #: When the evidence statuses in this response were established. They describe
    #: one coherent indexed snapshot as of this moment, and make no promise about
    #: any moment after it.
    checked_at: datetime
