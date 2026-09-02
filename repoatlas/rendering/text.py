"""Concise terminal rendering (V1_SPEC §16).

Default output is short and emphasizes answer, evidence, confidence and next
action. Expanded reasoning belongs behind ``--verbose``, not in the default view.
"""

from __future__ import annotations

from repoatlas.models.evidence import EvidenceSet, Finding, RepoAtlasAnswer
from repoatlas.models.reports import StatusReport, SyncReport
from repoatlas.models.repository import Repository


def render_repos(repositories: list[Repository]) -> str:
    """Compact table: name, then detected languages."""
    if not repositories:
        return "No repositories are managed yet.\n\nRun:\n\n  repoatlas init <directory>"

    width = max(len(repo.name) for repo in repositories)
    lines = []
    for repo in sorted(repositories, key=lambda r: r.name):
        languages = ", ".join(repo.languages) if repo.languages else "-"
        lines.append(f"{repo.name.ljust(width)}  {languages}")
    return "\n".join(lines)


def render_status(report: StatusReport) -> str:
    if report.scope == "repository" and report.repositories:
        repo = report.repositories[0]
        lines = [
            f"Repository: {repo.name}",
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
        return "\n".join(lines)

    return "\n".join(
        [
            f"Root: {report.root}",
            f"Repositories: {report.repository_count:,}",
            f"Indexed files: {report.indexed_files:,}",
            f"Symbols: {report.symbols:,}",
            f"Relationships: {report.relationships:,}",
            f"Last sync: {_format_time(report.last_sync)}",
            f"Index state: {report.index_state}",
        ]
    )


def render_sync(report: SyncReport) -> str:
    if not report.repositories:
        return "No repositories to sync."

    lines: list[str] = []
    if len(report.repositories) == 1:
        entry = report.repositories[0]
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
            if entry.touched:
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
            "",
            "Sync complete.",
        ]
    )
    return "\n".join(lines)


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
        lines.append(f"{candidate.location}")
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


#: Numeric confidence to the word shown in the terminal (V1_SPEC §16).
_HIGH = 0.75
_MEDIUM = 0.45


def confidence_label(value: float) -> str:
    if value >= _HIGH:
        return "HIGH"
    if value >= _MEDIUM:
        return "MEDIUM"
    return "LOW"


def _citation_lines(finding: Finding) -> list[str]:
    lines: list[str] = []
    for item in finding.evidence:
        location = f"{item.repository}/{item.path}"
        if item.line_start is not None:
            span = (
                str(item.line_start)
                if item.line_end in (None, item.line_start)
                else f"{item.line_start}-{item.line_end}"
            )
            location = f"{location}:{span}"
        lines.append(location)
        if item.description:
            lines.append(f"  {item.description}")
    return lines


def render_answer(answer: RepoAtlasAnswer) -> str:
    """``ask`` output: answer, evidence, confidence, next action (V1_SPEC §16)."""
    lines = ["ANSWER", "", answer.summary]

    if answer.findings:
        lines.extend(["", "EVIDENCE", ""])
        for finding in answer.findings:
            lines.append(finding.statement)
            lines.extend(f"  {line}" for line in _citation_lines(finding))
            lines.append("")
        lines.pop()

        best = max(f.confidence for f in answer.findings)
        lines.extend(["", "CONFIDENCE", "", confidence_label(best)])

    if answer.unknowns:
        lines.extend(["", "UNKNOWN", ""])
        lines.extend(f"- {unknown}" for unknown in answer.unknowns)

    if answer.next_check:
        lines.extend(["", "NEXT", "", answer.next_check])

    return "\n".join(lines)


def render_diagnosis(answer: RepoAtlasAnswer, evidence: EvidenceSet) -> str:
    """``debug`` output, separating facts from hypotheses (V1_SPEC §15, §16).

    The split is not a confidence threshold on model output. FACTS are the
    deterministic evidence RepoAtlas retrieved, which carries provenance;
    HYPOTHESES are the model's findings, which are interpretations of it. That
    mapping is the trust rule made visible: the model never contributes a fact.
    """
    lines = ["SUMMARY", "", answer.summary]

    if evidence.candidates:
        lines.extend(["", "FACTS", ""])
        for candidate in evidence.candidates:
            lines.append(candidate.location)
            detail = candidate.description or _first_meaningful_line(candidate.text)
            if detail:
                lines.append(f"  {detail}")

    if answer.findings:
        lines.extend(["", "HYPOTHESES", ""])
        for finding in sorted(answer.findings, key=lambda f: f.confidence, reverse=True):
            lines.append(finding.statement)
            lines.append(
                f"  confidence: {confidence_label(finding.confidence)} ({finding.confidence:.2f})"
            )
            lines.extend(f"  {line}" for line in _citation_lines(finding))
            lines.append("")
        lines.pop()

    if answer.unknowns:
        lines.extend(["", "UNKNOWNS", ""])
        lines.extend(f"- {unknown}" for unknown in answer.unknowns)

    lines.extend(["", "NEXT", "", answer.next_check or "No further check suggested."])
    return "\n".join(lines)
