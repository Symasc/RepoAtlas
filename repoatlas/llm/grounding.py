"""Citation verification — the enforcement point (ARCHITECTURE.md §11, V1_SPEC §15).

The trust rule is a guarantee, and a guarantee cannot rest on model compliance.
So after a model answer validates, every citation in it is checked against the
evidence set that was *actually supplied*:

* an ``Evidence`` naming a repository or path not in the supplied set is dropped;
* a line range outside the supplied span is clamped to it, or dropped if disjoint;
* a ``Finding`` left with no surviving evidence is demoted to an ``unknown``;
* if every finding is demoted, the answer becomes ``INSUFFICIENT EVIDENCE``.

Nothing here trusts the model's wording. A fabricated citation cannot survive.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from repoatlas.models.evidence import Evidence, EvidenceSet, Finding, RepoAtlasAnswer

INSUFFICIENT_EVIDENCE = "INSUFFICIENT EVIDENCE"


@dataclass(frozen=True, slots=True)
class GroundingReport:
    """What verification changed. Surfaced under ``--verbose``.

    A rising ``dropped_citations`` count across a corpus is a regression signal
    for prompts and models, which is why it is counted rather than discarded.
    """

    answer: RepoAtlasAnswer
    dropped_citations: int = 0
    clamped_citations: int = 0
    demoted_findings: int = 0
    unsupported: tuple[str, ...] = field(default=())

    @property
    def modified(self) -> bool:
        return bool(self.dropped_citations or self.clamped_citations or self.demoted_findings)


def _supplied_spans(evidence: EvidenceSet) -> dict[tuple[str, str], list[tuple[int, int]]]:
    """Line spans actually shown to the model, keyed by (repository, path)."""
    spans: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for candidate in evidence.candidates:
        key = (candidate.repository, candidate.path)
        spans.setdefault(key, []).append((candidate.line_start, candidate.line_end))
    return spans


def _verify_evidence(
    item: Evidence, spans: dict[tuple[str, str], list[tuple[int, int]]]
) -> tuple[Evidence | None, bool]:
    """Return the grounded citation and whether it needed clamping.

    ``None`` means the citation was not supported by the supplied set at all.
    """
    key = (item.repository, item.path)
    supplied = spans.get(key)
    if supplied is None:
        return None, False

    # A file-level citation with no line numbers is grounded as far as it goes:
    # the file really was supplied. Inventing a span for it would be worse.
    if item.line_start is None and item.line_end is None:
        return item, False

    start = item.line_start if item.line_start is not None else item.line_end
    end = item.line_end if item.line_end is not None else item.line_start
    assert start is not None and end is not None
    if start > end:
        start, end = end, start

    for span_start, span_end in supplied:
        if start <= span_end and end >= span_start:
            clamped_start = max(start, span_start)
            clamped_end = min(end, span_end)
            if (clamped_start, clamped_end) == (item.line_start, item.line_end):
                return item, False
            return (
                item.model_copy(update={"line_start": clamped_start, "line_end": clamped_end}),
                True,
            )

    # Disjoint from every supplied span: the model pointed somewhere it was
    # never shown, so the citation goes.
    return None, False


def verify_citations(answer: RepoAtlasAnswer, evidence: EvidenceSet) -> GroundingReport:
    """Strip every claim the supplied evidence does not support."""
    spans = _supplied_spans(evidence)

    dropped = 0
    clamped = 0
    demoted: list[str] = []
    surviving: list[Finding] = []

    for finding in answer.findings:
        kept: list[Evidence] = []
        for item in finding.evidence:
            grounded, was_clamped = _verify_evidence(item, spans)
            if grounded is None:
                dropped += 1
                continue
            clamped += int(was_clamped)
            kept.append(grounded)

        if kept:
            surviving.append(finding.model_copy(update={"evidence": kept}))
        else:
            # A finding with no surviving citation is not a finding. It becomes an
            # unknown, which is the honest thing to report.
            demoted.append(finding.statement)

    unknowns = list(answer.unknowns)
    unknowns.extend(f"Unsupported by retrieved evidence: {statement}" for statement in demoted)

    if surviving:
        verified = answer.model_copy(update={"findings": surviving, "unknowns": unknowns})
    else:
        verified = answer.model_copy(
            update={
                "summary": INSUFFICIENT_EVIDENCE,
                "findings": [],
                "unknowns": unknowns,
            }
        )

    return GroundingReport(
        answer=verified,
        dropped_citations=dropped,
        clamped_citations=clamped,
        demoted_findings=len(demoted),
        unsupported=tuple(demoted),
    )
