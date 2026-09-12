"""The sync adapter — V1's run, projected, then followed by ledger housekeeping.

There is exactly one orchestration owner for an index mutation, and it is
:func:`repoatlas.services.sync_service.sync`. Repository-set reconciliation,
discovery, containment, ignore and secret policy, binary detection, hashing,
parser dispatch, per-file replacement, dependency indexing, chunking, embedding,
linking, sync markers and the writer lock all live below it and are reused whole.
Nothing in this module or its caller restates any of them, because a second
interpretation of "which files belong to this repository" is precisely how an
index ends up admitting a file V1 refuses.

What is left for the capability layer is small, and pure:

* recovering each report's **identity** — V1 names a repository by display name,
  and two may share one;
* projecting host paths away;
* aggregating what the post-sync re-check did to evidence already held by live
  investigations, as counts and nothing more.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from repoatlas.models.investigation import EvidenceStatus, EvidenceTransition, LedgerSummary
from repoatlas.models.reports import RepositorySyncReport, SyncReport
from repoatlas.models.repository import Repository
from repoatlas.models.sync import (
    EvidenceRevalidationSummary,
    EvidenceTransitionCount,
    RepositorySyncView,
    RevalidationStatus,
    SyncDiagnostic,
    SyncStatus,
)

#: The declared order of statuses, used to order transition counts deterministically.
_STATUS_ORDER: tuple[EvidenceStatus, ...] = tuple(EvidenceStatus)


def sync_status(report: SyncReport) -> SyncStatus:
    """``INCOMPLETE`` exactly when V1 would exit non-zero for an unavailable repository."""
    return SyncStatus.INCOMPLETE if report.unavailable else SyncStatus.COMPLETED


def attribute_reports(
    reports: Sequence[RepositorySyncReport], candidates: Sequence[Repository]
) -> list[str | None]:
    """The identity behind each report, recovered without changing V1's report.

    V1's engine produces one report per repository it did not retire, in the order
    it was handed them. ``candidates`` must be that same input in that same order —
    the registry's ``(name, path)`` order when the run covered the managed root, or
    the one resolved repository when it did not. Walking both in order then pairs
    each report with the next candidate of the same name, which stays exact when
    display names collide.

    A report that finds no partner is ``None``: unattributed rather than guessed.
    That happens only if another process changed the registry between this run
    releasing its lock and the read that follows, and guessing between two
    repositories that share a name is how facts get attributed to the wrong tree.
    """
    slugs: list[str | None] = []
    cursor = 0
    for report in reports:
        match: int | None = None
        for index in range(cursor, len(candidates)):
            if candidates[index].name == report.repository:
                match = index
                break
        if match is None:
            slugs.append(None)
            continue
        slugs.append(candidates[match].slug or None)
        cursor = match + 1
    return slugs


def repository_view(report: RepositorySyncReport, slug: str | None) -> RepositorySyncView:
    """One V1 repository report, by identity. Every count is V1's own."""
    return RepositorySyncView(
        repository_slug=slug,
        repository_name=report.repository,
        branch=report.branch,
        is_dirty=report.is_dirty,
        unchanged=report.unchanged,
        changed=report.changed,
        new=report.new,
        deleted=report.deleted,
        files_indexed=report.files_indexed,
        symbols=report.symbols,
        relationships=report.relationships,
        dependencies=report.dependencies,
        full_rehash=report.full_rehash,
        rehash_reason=report.rehash_reason,
        enumeration_failed=report.enumeration_failed,
        diagnostics=tuple(
            SyncDiagnostic(severity=d.severity, message=d.message, line=d.line, path=d.path)
            for d in report.diagnostics
        ),
    )


def relative_unreadable(paths: Sequence[Path], root: Path | None) -> tuple[str, ...]:
    """Unreadable directories relative to the managed root, and never absolute.

    Discovery only walks beneath the root, so every path is relative to it. The
    fallback exists so that an unexpected one degrades to its last component
    rather than leaking where the operator keeps their code.
    """
    projected: list[str] = []
    resolved_root = root.expanduser().resolve() if root is not None else None
    for path in paths:
        candidate = path.expanduser().resolve()
        if resolved_root is not None and candidate.is_relative_to(resolved_root):
            projected.append(candidate.relative_to(resolved_root).as_posix())
        else:
            projected.append(candidate.name)
    return tuple(projected)


def revalidation_summary(
    results: Sequence[tuple[tuple[EvidenceTransition, ...], LedgerSummary]],
) -> EvidenceRevalidationSummary:
    """Aggregate what re-checking each live investigation did, as counts only.

    ``results`` holds one entry per investigation re-checked: the transitions its
    ledger reported and its summary afterwards. Superseded records are sticky and
    are not re-decided, so they are counted but not checked.
    """
    counter: Counter[tuple[EvidenceStatus, EvidenceStatus]] = Counter()
    for transitions, _summary in results:
        for transition in transitions:
            counter[(transition.previous, transition.current)] += 1
    ordered = sorted(
        counter.items(),
        key=lambda item: (_STATUS_ORDER.index(item[0][0]), _STATUS_ORDER.index(item[0][1])),
    )
    summaries = [summary for _transitions, summary in results]
    return EvidenceRevalidationSummary(
        status=RevalidationStatus.COMPLETED,
        investigations_checked=len(results),
        evidence_checked=sum(s.total - s.superseded for s in summaries),
        valid=sum(s.valid for s in summaries),
        stale=sum(s.stale for s in summaries),
        superseded=sum(s.superseded for s in summaries),
        invalid=sum(s.invalid for s in summaries),
        unverified=sum(s.unverified for s in summaries),
        transitions=tuple(
            EvidenceTransitionCount(previous=previous, current=current, count=count)
            for (previous, current), count in ordered
        ),
    )


def revalidation_failed(detail: str) -> EvidenceRevalidationSummary:
    """The re-check could not finish. The index change it follows stands."""
    return EvidenceRevalidationSummary(status=RevalidationStatus.FAILED, detail=detail)
