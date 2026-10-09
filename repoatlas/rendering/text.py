"""Concise terminal rendering (V1_SPEC §16).

What the deterministic commands print: repositories, index status, a sync report,
a dependency inventory and an evidence set. Output is short by default, with the
per-stage detail behind ``--verbose`` rather than in the default view.

The answer and diagnosis renderers that used to live here went with the embedded
reasoner. They formatted a model's prose, and RepoAtlas no longer produces any:
an agent that reasons over this evidence renders its own conclusions.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from repoatlas.models.dependencies import Agreement, DependencyPackage
from repoatlas.models.evidence import EvidenceSet
from repoatlas.models.reports import DecisionsReport, StatusReport, SyncReport
from repoatlas.models.repository import Repository


def render_repos(repositories: list[Repository]) -> str:
    """Compact table: name, then detected languages.

    A second column appears **only** for names that more than one repository
    answers to. Two rows both reading ``common`` are technically correct and
    useless; printing every repository's path to guard against a collision that is
    not happening would make the ordinary listing worse (V1_SPEC §16). So the
    identity is shown exactly where the name stops identifying anything.
    """
    if not repositories:
        return "No repositories are managed yet.\n\nRun:\n\n  repoatlas init <directory>"

    counts: dict[str, int] = {}
    for repo in repositories:
        counts[repo.name] = counts.get(repo.name, 0) + 1
    ambiguous = {name for name, count in counts.items() if count > 1}

    ordered = sorted(repositories, key=lambda r: (r.name, r.slug, str(r.path)))
    width = max(len(repo.name) for repo in repositories)
    identity_width = max((len(repo.slug) for repo in ordered if repo.name in ambiguous), default=0)

    lines = []
    for repo in ordered:
        languages = ", ".join(repo.languages) if repo.languages else "-"
        if ambiguous:
            identity = repo.slug if repo.name in ambiguous else ""
            lines.append(f"{repo.name.ljust(width)}  {identity.ljust(identity_width)}  {languages}")
        else:
            lines.append(f"{repo.name.ljust(width)}  {languages}")
    return "\n".join(lines)


def render_status(report: StatusReport) -> str:
    if report.scope == "repository" and report.repositories:
        repo = report.repositories[0]
        # The identity is shown whenever it says more than the name does, so a
        # status report for one of two repositories called `common` states which.
        identity = f" ({repo.slug})" if repo.slug and repo.slug != repo.name else ""
        lines = [
            f"Repository: {repo.name}{identity}",
            f"Path: {repo.path}",
            f"Branch: {repo.branch or 'unknown'}",
            f"Working tree: {'dirty' if repo.is_dirty else 'clean'}",
            "",
            f"Indexed files: {repo.indexed_files:,}",
            f"Symbols: {repo.symbols:,}",
            f"Relationships: {repo.relationships:,}",
            f"Last sync: {_format_time(report.last_sync)}",
            f"Index state: {repo.index_state}",
        ]
        # Drift is about the workspace, not about this repository, so it is stated
        # separately rather than folded into this repository's index state — but it
        # is stated, because someone who only ever works inside a repository would
        # otherwise never be told the managed root has grown.
        lines.extend(_drift_notice(report))
        return "\n".join(lines)

    lines = [
        f"Root: {report.root}",
        f"Repositories: {report.repository_count:,}",
        f"Indexed files: {report.indexed_files:,}",
        f"Symbols: {report.symbols:,}",
        f"Relationships: {report.relationships:,}",
        f"Last sync: {_format_time(report.last_sync)}",
        f"Index state: {_state_reason(report)}",
    ]
    lines.extend(_drift_notice(report))
    return "\n".join(lines)


def _drift_notice(report: StatusReport) -> list[str]:
    """Name the repositories the managed root holds and the index does not."""
    lines: list[str] = []
    if report.new_repositories:
        count = len(report.new_repositories)
        lines.extend(
            [
                "",
                f"{count} repositor{'y' if count == 1 else 'ies'} beneath the root "
                f"{'is' if count == 1 else 'are'} not indexed:",
                *_named(report.new_repositories),
                "",
                "Run:",
                "",
                "  repoatlas sync",
            ]
        )
    if report.missing_repositories:
        lines.extend(["", "No longer on disk:", *_named(report.missing_repositories)])
    if report.conflicting_repositories:
        lines.extend(
            [
                "",
                "Already registered elsewhere, at a path that still exists:",
                *_named(report.conflicting_repositories),
                "",
                "These are not indexed. RepoAtlas will not give one identity to two",
                "repositories; move or remove the other location, or use a separate index.",
            ]
        )
    lines.extend(_unreadable_notice(report.unreadable_paths))
    if report.declarations_ignored is not None:
        lines.append(
            f"Declarations directory {report.declarations_ignored} is not a git repository; "
            "it is not read (run `git init` there to use it)."
        )
    return lines


def _state_reason(report: StatusReport) -> str:
    """The index state, with the repository-set reason attached when there is one.

    "stale" on its own asks the reader to guess which of several things changed.
    Repository-set drift is the one case the per-repository lines cannot show at
    all — the repository is not among them — so it is named here or nowhere.
    """
    new, gone = len(report.new_repositories), len(report.missing_repositories)
    clashing = len(report.conflicting_repositories)
    reasons: list[str] = []
    if new:
        reasons.append(f"{new} repositor{'y' if new == 1 else 'ies'} not indexed")
    if gone:
        reasons.append(f"{gone} no longer on disk")
    if clashing:
        reasons.append(f"{clashing} already registered elsewhere")
    if not reasons:
        return report.index_state
    return f"{report.index_state} — {', '.join(reasons)}"


def _unreadable_notice(paths: Sequence[Path]) -> list[str]:
    """Name the directories discovery was refused, if any.

    Printed rather than counted silently: a walk that could not read a subtree has
    not established that the subtree holds no repositories, and reporting the two
    the same way is how a discovery gap goes unnoticed (V1_SPEC §5). At most five
    paths, because the point is that it happened and where to start looking.
    """
    if not paths:
        return []
    shown = [f"  {path}" for path in paths[:5]]
    remainder = len(paths) - len(shown)
    if remainder > 0:
        shown.append(f"  ... and {remainder} more")
    return [
        "",
        f"Could not read {len(paths)} director"
        f"{'y' if len(paths) == 1 else 'ies'}; "
        "any repositories inside were not discovered:",
        *shown,
    ]


def _named(identities: Sequence[str], limit: int = 5) -> list[str]:
    """An indented list of repository identities, bounded."""
    shown = [f"  {identity}" for identity in identities[:limit]]
    remainder = len(identities) - len(shown)
    if remainder > 0:
        shown.append(f"  ... and {remainder} more")
    return shown


def _discovered_notice(report: SyncReport) -> list[str]:
    """Say which repositories this sync found for the first time.

    A sync that silently grew the managed set would be indistinguishable from one
    that did nothing, and the whole point of reconciling membership is that the
    operator stops having to know (V1_SPEC §12).
    """
    if not report.repositories_added:
        return []
    count = len(report.repositories_added)
    return [
        "",
        f"Discovered {count} new repositor{'y' if count == 1 else 'ies'}:",
        *_named(report.repositories_added),
    ]


def render_sync(report: SyncReport) -> str:
    if not report.repositories:
        return "\n".join(
            [
                "No repositories to sync.",
                *_discovered_notice(report),
                *_unreadable_notice(report.unreadable_paths),
            ]
        )

    lines: list[str] = []
    if len(report.repositories) == 1:
        entry = report.repositories[0]
        if entry.enumeration_failed:
            lines.extend([f"Repository: {entry.repository}", "", "not synchronized"])
        else:
            lines.extend(
                [
                    f"Repository: {entry.repository}",
                    f"Branch: {entry.branch or 'unknown'}",
                    f"Working tree: {'dirty' if entry.is_dirty else 'clean'}",
                    "",
                    f"changed: {entry.changed}",
                    f"new: {entry.new}",
                    f"deleted: {entry.deleted}",
                ]
            )
    else:
        lines.append(f"Syncing {len(report.repositories)} repositories...")
        lines.append("")
        for entry in report.repositories:
            if entry.enumeration_failed:
                # "unchanged" would be a lie here: nothing was compared at all.
                detail = "not synchronized"
            elif entry.touched:
                detail = f"changed: {entry.changed}, new: {entry.new}, deleted: {entry.deleted}"
            else:
                detail = "unchanged"
            lines.append(f"{entry.repository}")
            lines.append(f"  {detail}")

    lines.extend(
        [
            "",
            "Updated:",
            f"  {report.files_indexed} files",
            f"  {report.symbols} symbols",
            f"  {report.relationships} relationships",
        ]
    )
    if report.dependencies:
        lines.append(f"  {report.dependencies} dependencies")
    if report.cross_repository_edges:
        lines.append(f"  {report.cross_repository_edges} cross-repository links")
    if report.unresolved_references:
        # Not a failure: a reference to something outside the managed set stays a
        # recorded claim, and is resolved if that repository is added later.
        lines.append(f"  {report.unresolved_references} references still unresolved")
    lines.extend(_discovered_notice(report))
    lines.extend(_unreadable_notice(report.unreadable_paths))
    lines.extend(_unavailable_notice(report))
    # "Sync complete." is a claim, so it is only made when it is true. A run that
    # could not establish one repository's file set indexed nothing from it and
    # says so instead (V1_SPEC §12).
    lines.extend(["", "Sync incomplete." if report.unavailable else "Sync complete."])
    return "\n".join(lines)


def _unavailable_notice(report: SyncReport) -> list[str]:
    """Name the repositories that were not synchronized, and why, once.

    The diagnostic is carried on the per-repository report; it is repeated here
    rather than deep in a verbose stream because it is the reason the command did
    not succeed. Bounded: the repositories are named, nothing about the host's Git
    installation or configuration is echoed (ARCHITECTURE.md §12).
    """
    unavailable = report.unavailable
    if not unavailable:
        return []
    lines = [
        "",
        f"Not synchronized ({len(unavailable)}):",
        *(f"  {name}" for name in unavailable),
    ]
    for entry in report.repositories:
        if entry.enumeration_failed:
            lines.extend(["", *(f"  {d.message}" for d in entry.diagnostics)])
            break
    return lines


def render_dependencies(packages: list[DependencyPackage]) -> str:
    """One block per package, one line per observation (V1_SPEC §13, §16).

    Grouped so multiplicity is obvious, never flattened so it disappears. An
    unambiguous package renders compactly — one declaration, one resolution, no
    commentary — because most of them are. A package with more than one distinct
    value gets an explicit count, phrased as **declarations** and **resolved
    versions** rather than as a conflict: RepoAtlas has no constraint solver, so it
    can say that ``^1.0.0`` and ``^9.9.9`` are two different requests and must not
    say they are incompatible (V1_SPEC §13).

    Every line keeps its own source path, so grouping never costs provenance.
    """
    if not packages:
        return "No dependencies are indexed."

    lines: list[str] = []
    for package in packages:
        header = f"{package.name}  ({package.ecosystem})"
        notes = _multiplicity_notes(package)
        lines.append(f"{header}  [{', '.join(notes)}]" if notes else header)
        width = max((len(o.source_path) for o in package.observations), default=0)
        for observation in package.observations:
            value = observation.value or "-"
            group = f"  ({observation.group})" if observation.group not in (None, "main") else ""
            lines.append(
                f"  {observation.source_path.ljust(width)}  "
                f"{observation.role.value:9s} {value}{group}"
            )
        lines.append("")
    if lines:
        lines.pop()
    return "\n".join(lines)


def _multiplicity_notes(package: DependencyPackage) -> list[str]:
    """What the reader must not miss: several answers exist, and how many."""
    notes: list[str] = []
    if package.declaration_agreement is Agreement.DIVERGENT:
        notes.append(f"{len(package.declared_constraints)} differing declarations")
    elif package.declaration_agreement is Agreement.AGREEING:
        notes.append(f"{len(package.declarations)} agreeing declarations")
    if package.resolution_agreement is Agreement.DIVERGENT:
        notes.append(f"{len(package.resolved_versions)} resolved versions")
    return notes


def render_evidence(evidence: EvidenceSet) -> str:
    """``locate`` output: locations first, prose minimal (V1_SPEC §16)."""
    if not evidence.candidates:
        return "\n".join(
            [
                "INSUFFICIENT EVIDENCE",
                "",
                "No indexed evidence matches this question.",
                "",
                "NEXT",
                "",
                "Run `repoatlas sync` if the index may be out of date,",
                "or rephrase using an exact identifier, path or error string.",
            ]
        )

    lines = ["EVIDENCE", ""]
    for candidate in evidence.candidates:
        lines.append(candidate.located(evidence.prefix_for(candidate)))
        detail = candidate.description or _first_meaningful_line(candidate.text)
        if detail:
            lines.append(f"  {detail}")
    if evidence.truncated:
        lines.extend(["", "(evidence truncated to the configured budget)"])
    return "\n".join(lines)


def _first_meaningful_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:100]
    return ""


def _format_time(value: object) -> str:
    if value is None:
        return "never"
    return str(value).split(".")[0].replace("+00:00", "Z")


def render_decisions(report: DecisionsReport) -> str:
    """The total, the counts by family and outcome, then one page of decisions."""
    if report.total == 0:
        return "No stored decisions match."
    lines = [f"{report.total} decision(s)"]
    for count in report.counts:
        lines.append(f"  {count.count:>6}  {count.family}  {count.decision}")
    lines.append("")
    for entry in report.decisions:
        place = f"{entry.repository}/{entry.path}" + (
            f":{entry.line}" if entry.line is not None else ""
        )
        lines.append(f"{place}  {entry.family}  {entry.decision}")
        lines.append(f"    {entry.subject}")
    shown = len(report.decisions)
    if report.remaining:
        lines.append("")
        lines.append(
            f"{shown} shown from offset {report.offset}; {report.remaining} more "
            f"(--offset {report.offset + shown})"
        )
    return "\n".join(lines)
