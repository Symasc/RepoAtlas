"""The grounding adapter — session evidence ids onto the unmodified V1 algorithm.

:func:`repoatlas.grounding.ground_answer` has no concept of an evidence id. It was
written for V1, where the model is shown ``slug/path:start-end`` locations and
cites them back as coordinates, and it resolves a citation by looking up
``(repository_slug, path)`` and testing the cited lines against **every** span
supplied for that file. That is exactly right for V1 and exactly wrong to hand a
whole ledger to, because it is the one place a V2 guarantee could quietly leak:

    Ledger: ``E1`` = ``svc/app.py:10-12``, ``E2`` = ``svc/app.py:20-30``.
    The agent cites ``E1`` at lines 25-28.

Give ``ground_answer`` one evidence set built from both entries and it resolves
``svc/app.py``, finds the span list ``[(10,12), (20,30)]``, sees 25-28 inside the
second, and accepts. The citation would be grounded — against evidence the caller
did not name. ``E1``'s identity would have been rewritten by its own coordinates,
which is the precise failure evidence ids exist to prevent.

So resolution never sees more than one entry at a time. Each citation is checked
against an evidence set containing **only the entry it names**, which makes the
binding between an id and a span inviolable by construction rather than by care.
Three properties fall out of that and are not separately enforced anywhere:

* a citation cannot borrow another id's span, however adjacent (V2 §21);
* a caller-supplied repository or path cannot override the reference (V2 §22);
* nothing is renumbered, because there are no numbers to renumber — selection
  cannot shift ``E3`` onto ``E2``'s span when each is resolved alone (V2 §19).

What the whole-answer pass is still needed for is everything that is *not* a
coordinate: V1's rule that one unresolvable citation condemns its whole claim,
its refusal to let free text name an unsupplied file, and its replacement of a
summary that does. Those run once, over evidence sets whose spans have already
been decided, so the second pass can remove but never widen.

Nothing here reads source text. The ledger holds digests rather than bytes, and
:func:`ground_answer` was measured — not assumed — never to consult
``EvidenceCandidate.text``; :mod:`tests.capabilities.test_ground_answer_contract`
pins that, and every candidate this module builds carries ``text=""``.
"""

from __future__ import annotations

from dataclasses import dataclass

from repoatlas.capabilities.errors import InvalidGroundingRequestError
from repoatlas.grounding import GroundingReport, ground_answer
from repoatlas.models.capability_io import LineRange
from repoatlas.models.evidence import (
    Evidence,
    EvidenceCandidate,
    EvidenceSet,
    Hypothesis,
    RepoAtlasAnswer,
)
from repoatlas.models.investigation import EvidenceStatus, LedgerEntry
from repoatlas.models.verification import (
    AgentAnswer,
    AgentCitation,
    AgentClaim,
    GroundingFindingCode,
    GroundingVerdict,
    VerifyGroundingRequest,
)

#: Statuses whose evidence a citation may rest on. Exactly one, and the tuple
#: exists so the reason is visible rather than implied by an ``is`` comparison:
#: every other status means something moved under the record since it was
#: supplied, and a citation to it is a citation to a snapshot that is gone.
GROUNDABLE: frozenset[EvidenceStatus] = frozenset({EvidenceStatus.VALID})

#: Why each non-groundable status refuses a citation. Kept as a table so a new
#: status cannot be added upstream and silently fall through as groundable.
_STATUS_CODE: dict[EvidenceStatus, GroundingFindingCode] = {
    EvidenceStatus.STALE: GroundingFindingCode.STALE_EVIDENCE,
    EvidenceStatus.SUPERSEDED: GroundingFindingCode.SUPERSEDED_EVIDENCE,
    EvidenceStatus.INVALID: GroundingFindingCode.INVALID_EVIDENCE,
    EvidenceStatus.UNVERIFIED: GroundingFindingCode.UNVERIFIED_EVIDENCE,
}

_STATUS_DETAIL: dict[EvidenceStatus, str] = {
    EvidenceStatus.STALE: (
        "the indexed file has changed since this evidence was supplied, so it no "
        "longer describes what is there now"
    ),
    EvidenceStatus.SUPERSEDED: (
        "different bytes were later supplied for this span under a newer evidence "
        "id; cite that id explicitly if it is what you mean"
    ),
    EvidenceStatus.INVALID: "no indexed file stands at that repository identity and path any more",
    EvidenceStatus.UNVERIFIED: (
        "revalidation established nothing about this evidence's file, so its "
        "standing is unknown rather than good"
    ),
}


def status_code(status: EvidenceStatus) -> GroundingFindingCode:
    """The finding a non-groundable status earns."""
    return _STATUS_CODE[status]


def status_detail(status: EvidenceStatus) -> str:
    """Prose for a human reading why a citation was refused. Carries no contract."""
    return _STATUS_DETAIL[status]


def validate_grounding_request(request: VerifyGroundingRequest) -> None:
    """Refuse a request that cannot be checked, before any session is touched.

    Deliberately narrow. A citation naming an id this investigation never issued
    is **not** a malformed request — it is the question the caller asked, and it
    comes back as a finding (V2 §29). What is refused here is a request with no
    answer in it to check at all, because a verdict on nothing would be a verdict
    nobody could act on.
    """
    answer = request.answer
    if not answer.claims and not answer.summary and not answer.unknowns:
        raise InvalidGroundingRequestError(
            "an answer with no summary, claims or unknowns has nothing to ground",
            field="answer",
        )
    for claim_index, claim in enumerate(answer.claims):
        if not claim.statement.strip():
            raise InvalidGroundingRequestError(
                f"claim {claim_index} has no statement", field=f"answer.claims[{claim_index}]"
            )


# ---------------------------------------------------------------------------
# Per-citation resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CitationOutcome:
    """What one citation earned, decided against one entry and nothing else."""

    claim_index: int
    citation_index: int
    evidence_id: str
    #: ``True`` only when the submitted citation resolved exactly. A citation that
    #: had to be narrowed is admitted into the answer and is *not* grounded: the
    #: range the caller wrote is not the range that checks out.
    grounded: bool
    #: ``True`` when the citation may appear in the sanitized answer at all.
    admitted: bool
    status: EvidenceStatus | None
    submitted: LineRange | None
    verified: LineRange | None
    location: str
    code: GroundingFindingCode | None
    detail: str
    #: The claimed confidence after V1's narrowing rule, as ``ground_answer``
    #: itself computed it. ``None`` when nothing was admitted.
    confidence: float | None


def _candidate_for(entry: LedgerEntry) -> EvidenceCandidate:
    """The single supplied candidate a citation to ``entry`` may rest on.

    ``text`` is empty because the ledger has no text to give and grounding does
    not read any. Passing the slug as the display name too keeps resolution on the
    identity path: ``_resolve_source`` consults a display name only when no slug
    is present, and here one always is.
    """
    ref = entry.ref
    return EvidenceCandidate(
        repository=ref.repository_slug,
        repository_slug=ref.repository_slug,
        path=ref.path,
        line_start=ref.line_start,
        line_end=ref.line_end,
        text="",
        description="",
    )


def _submitted_range(citation: AgentCitation, entry: LedgerEntry) -> tuple[int | None, int | None]:
    """The lines this citation names.

    A citation with no range cites the whole span that was supplied under that id,
    which is the natural reading of "I am citing E4" and gives the caller back a
    verified range rather than a shrug. For file-identity evidence there are no
    lines to inherit and there is no line to invent, so it stays ``None``.
    """
    if not citation.narrowed:
        return (entry.ref.line_start, entry.ref.line_end)
    start = citation.line_start if citation.line_start is not None else citation.line_end
    end = citation.line_end if citation.line_end is not None else citation.line_start
    return (start, end)


def _as_range(start: int | None, end: int | None) -> LineRange | None:
    if start is None or end is None:
        return None
    return LineRange(start=min(start, end), end=max(start, end))


def resolve_citation(
    *,
    claim_index: int,
    citation_index: int,
    citation: AgentCitation,
    entry: LedgerEntry | None,
    claimed_confidence: float,
) -> CitationOutcome:
    """Decide one citation against the one entry it names.

    The entry is the whole world for this decision. Nothing else in the ledger is
    visible, so no other span can satisfy this citation and no ordering among
    entries can change the answer.
    """
    evidence_id = citation.evidence_id
    if entry is None:
        return CitationOutcome(
            claim_index=claim_index,
            citation_index=citation_index,
            evidence_id=evidence_id,
            grounded=False,
            admitted=False,
            status=None,
            submitted=_as_range(citation.line_start, citation.line_end),
            verified=None,
            location="",
            code=GroundingFindingCode.UNKNOWN_EVIDENCE,
            detail=(
                "this investigation never issued that evidence id; ids are valid "
                "only inside the investigation that issued them"
            ),
            confidence=None,
        )

    location = entry.ref.location
    if entry.status not in GROUNDABLE:
        return CitationOutcome(
            claim_index=claim_index,
            citation_index=citation_index,
            evidence_id=evidence_id,
            grounded=False,
            admitted=False,
            status=entry.status,
            submitted=_as_range(*_submitted_range(citation, entry)),
            verified=None,
            location=location,
            code=status_code(entry.status),
            detail=status_detail(entry.status),
            confidence=None,
        )

    slug = citation.repository_slug or entry.ref.repository_slug
    path = citation.path or entry.ref.path
    start, end = _submitted_range(citation, entry)
    submitted = _as_range(start, end)

    # One candidate, one citation, one call. The evidence set cannot offer a span
    # this id did not supply, because it holds nothing else.
    probe = ground_answer(
        RepoAtlasAnswer(
            summary="",
            hypotheses=[
                Hypothesis(
                    statement="",
                    confidence=claimed_confidence,
                    evidence=[
                        Evidence(
                            repository=slug,
                            repository_slug=slug,
                            path=path,
                            line_start=start,
                            line_end=end,
                            description="",
                        )
                    ],
                )
            ],
        ),
        EvidenceSet(question="", candidates=(_candidate_for(entry),)),
    )

    if not probe.answer.hypotheses:
        code, detail = _rejection(citation, entry, slug, path)
        return CitationOutcome(
            claim_index=claim_index,
            citation_index=citation_index,
            evidence_id=evidence_id,
            grounded=False,
            admitted=False,
            status=entry.status,
            submitted=submitted,
            verified=None,
            location=location,
            code=code,
            detail=detail,
            confidence=None,
        )

    kept = probe.answer.hypotheses[0].evidence[0]
    verified = _as_range(kept.line_start, kept.line_end)
    narrowed = bool(probe.clamped_citations)
    return CitationOutcome(
        claim_index=claim_index,
        citation_index=citation_index,
        evidence_id=evidence_id,
        grounded=not narrowed,
        admitted=True,
        status=entry.status,
        submitted=submitted,
        verified=verified,
        location=location,
        code=GroundingFindingCode.CITATION_CLAMPED_TO_SUPPLIED_SPAN if narrowed else None,
        detail=(
            "the cited lines reached past what was supplied under this id and were "
            "narrowed to the part that was"
            if narrowed
            else ""
        ),
        # V1's narrowing rule, applied by V1. Never raised above what was claimed.
        confidence=probe.answer.hypotheses[0].confidence,
    )


def _rejection(
    citation: AgentCitation, entry: LedgerEntry, slug: str, path: str
) -> tuple[GroundingFindingCode, str]:
    """Label a rejection ``ground_answer`` already decided.

    A comparison, not a second opinion. Grounding said no; this says which of the
    three ways it could have said no actually happened, so a caller can tell a
    typo in a path from a line range that was never supplied.
    """
    ref = entry.ref
    if (slug, path) != (ref.repository_slug, ref.path):
        return (
            GroundingFindingCode.CITATION_SOURCE_MISMATCH,
            f"that evidence refers to {ref.repository_slug}/{ref.path}, not {slug}/{path}",
        )
    if ref.line_start is None and citation.narrowed:
        return (
            GroundingFindingCode.CITATION_WITHOUT_SUPPLIED_LINES,
            "that evidence supplied the file's identity and no lines of it, so no "
            "line range can rest on it",
        )
    return (
        GroundingFindingCode.CITATION_OUTSIDE_SUPPLIED_SPAN,
        f"the cited lines lie outside {ref.location}, which is what was supplied under that id",
    )


# ---------------------------------------------------------------------------
# Whole-answer pass
# ---------------------------------------------------------------------------


def supplied_set(
    *, admitted: dict[str, LedgerEntry], others: tuple[LedgerEntry, ...], question: str = ""
) -> EvidenceSet:
    """The evidence set the whole-answer pass runs against.

    Two kinds of candidate, for two different jobs.

    Entries an admitted citation rests on appear with their **span**, so the
    already-decided coordinates resolve. Every other entry this investigation
    holds appears as **file identity only** — no lines — because prose has a
    second question to answer: naming ``config.py`` in a sentence is honest when
    ``config.py`` was supplied, whatever became of the particular lines. Reporting
    it as a fabricated path because the citation that reached it went stale would
    be a false accusation about a file the agent genuinely saw.

    An identity-only candidate contributes an empty span list, so it can never
    ground a line citation — and a citation to a non-groundable id never reaches
    this pass in any case, having been refused with its own status finding.
    """
    candidates: list[EvidenceCandidate] = []
    for entry in admitted.values():
        candidates.append(_candidate_for(entry))
    admitted_ids = set(admitted)
    for entry in others:
        if entry.id in admitted_ids:
            continue
        ref = entry.ref
        candidates.append(
            EvidenceCandidate(
                repository=ref.repository_slug,
                repository_slug=ref.repository_slug,
                path=ref.path,
                line_start=None,
                line_end=None,
                text="",
                description="",
            )
        )
    return EvidenceSet(question=question, candidates=tuple(candidates))


def projected_answer(
    answer: AgentAnswer,
    *,
    outcomes: dict[tuple[int, int], CitationOutcome],
) -> RepoAtlasAnswer:
    """The submitted answer in V1's shape, carrying decided coordinates only.

    Every surviving citation is written at the range resolution already verified,
    so the whole-answer pass has no coordinate left to decide and cannot widen
    one. A claim that lost any citation is handed over with **no** evidence at
    all, which routes it into V1's own "not kept" branch — the rule that one
    unresolvable citation condemns the claim it was attached to, enforced by the
    code that owns it rather than restated here.
    """
    hypotheses: list[Hypothesis] = []
    for claim_index, claim in enumerate(answer.claims):
        decided = [outcomes[(claim_index, index)] for index in range(len(claim.citations))]
        kept: list[Evidence] = []
        if decided and all(outcome.admitted for outcome in decided):
            for outcome in decided:
                slug, _, path = outcome.location.partition("/")
                path = path.split(":", 1)[0]
                kept.append(
                    Evidence(
                        repository=slug,
                        repository_slug=slug,
                        path=path,
                        line_start=outcome.verified.start if outcome.verified else None,
                        line_end=outcome.verified.end if outcome.verified else None,
                        description="",
                    )
                )
        confidence = min(
            (outcome.confidence for outcome in decided if outcome.confidence is not None),
            default=claim.claimed_confidence,
        )
        hypotheses.append(
            Hypothesis(statement=claim.statement, confidence=confidence, evidence=kept)
        )
    return RepoAtlasAnswer(
        summary=answer.summary,
        hypotheses=hypotheses,
        unknowns=list(answer.unknowns),
        next_check=answer.next_check,
    )


def sanitized_answer(
    submitted: AgentAnswer,
    report: GroundingReport,
    *,
    outcomes: dict[tuple[int, int], CitationOutcome],
) -> AgentAnswer:
    """V1's verified answer, translated back into the public shape.

    Claims are matched to their originals by statement text in submission order,
    so a claim V1 removed is simply absent. Citations come back carrying the
    evidence id they were submitted with, because the id is the identity — the
    coordinates alone would make the sanitized answer uncitable in a later call.
    """
    surviving = {hypothesis.statement for hypothesis in report.answer.hypotheses}
    claims: list[AgentClaim] = []
    confidences = {
        hypothesis.statement: hypothesis.confidence for hypothesis in report.answer.hypotheses
    }
    for claim_index, claim in enumerate(submitted.claims):
        if claim.statement not in surviving:
            continue
        citations = tuple(
            citation
            for index, citation in enumerate(claim.citations)
            if outcomes[(claim_index, index)].admitted
        )
        claims.append(
            claim.model_copy(
                update={
                    "citations": citations,
                    "claimed_confidence": confidences.get(
                        claim.statement, claim.claimed_confidence
                    ),
                }
            )
        )
        surviving.discard(claim.statement)
    return AgentAnswer(
        summary=report.answer.summary,
        claims=tuple(claims),
        unknowns=tuple(report.answer.unknowns),
        next_check=report.answer.next_check,
    )


def verdict_for(
    *,
    submitted: AgentAnswer,
    outcomes: dict[tuple[int, int], CitationOutcome],
    report: GroundingReport,
) -> GroundingVerdict:
    """Derive the verdict from grounding outcomes and nothing else.

    No similarity, no keyword overlap, no tier read as strength, no match class
    read as entailment. Three populations, and the boundaries between them are the
    whole contract:

    A claim is **fully grounded** when it carried at least one citation, every one
    of them resolved exactly as written, and V1 kept the claim — which is also
    where the prose rule lands, so a statement that named an unsupplied file is not
    grounded however good its citations were.

    A claim is **partly grounded** when it survived but a citation had to be
    narrowed to stay inside what was supplied. Something real was cited and the
    caller's range was not it, so neither verdict at the extremes is honest: the
    lines that checked out did check out, and the ones the caller wrote did not.

    Everything else grounded nothing.
    """
    if not submitted.claims:
        return GroundingVerdict.UNGROUNDED

    surviving = {hypothesis.statement for hypothesis in report.answer.hypotheses}
    full = 0
    partial = 0
    for claim_index, claim in enumerate(submitted.claims):
        if claim.statement not in surviving:
            continue
        decided = [outcomes[(claim_index, index)] for index in range(len(claim.citations))]
        if not decided or not all(outcome.admitted for outcome in decided):
            continue
        if all(outcome.grounded for outcome in decided):
            full += 1
        else:
            partial += 1

    if full == len(submitted.claims) and not report.unsupported:
        return GroundingVerdict.GROUNDED
    if not full and not partial:
        return GroundingVerdict.UNGROUNDED
    return GroundingVerdict.PARTIALLY_GROUNDED
