"""Concise terminal rendering (V1_SPEC §16).

Default output is short and emphasizes answer, evidence, confidence and next
action. Expanded reasoning belongs behind ``--verbose``, not in the default view.
"""

from __future__ import annotations

from repoatlas.models.evidence import Evidence, EvidenceSet, Hypothesis, RepoAtlasAnswer
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
    lines.extend(["", "Sync complete."])
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


def _citation_location(item: Evidence) -> str:
    """``repo/path``, with the span when the citation carries one."""
    location = f"{item.repository}/{item.path}"
    if item.line_start is None:
        return location
    span = (
        str(item.line_start)
        if item.line_end in (None, item.line_start)
        else f"{item.line_start}-{item.line_end}"
    )
    return f"{location}:{span}"


def _citation_lines(hypothesis: Hypothesis) -> list[str]:
    lines: list[str] = []
    for item in hypothesis.evidence:
        lines.append(_citation_location(item))
        if item.description:
            lines.append(f"  {item.description}")
    return lines


def _verified_evidence_lines(evidence: EvidenceSet) -> list[str]:
    """The VERIFIED EVIDENCE section, built from retrieval alone.

    Every line here comes from an :class:`EvidenceCandidate` — a real span of a
    real file, with the provenance that proves it. No model string is consulted,
    which is what makes "unverified prose cannot appear as evidence" a property
    of the code rather than a promise about wording (ARCHITECTURE.md §11).
    """
    lines: list[str] = []
    for candidate in evidence.candidates:
        lines.append(candidate.location)
        detail = candidate.description or _first_meaningful_line(candidate.text)
        if detail:
            lines.append(f"  {detail}")
    return lines


def _hypothesis_lines(answer: RepoAtlasAnswer) -> list[str]:
    """The HYPOTHESIS section: model readings, each with the support it kept."""
    lines: list[str] = []
    for hypothesis in sorted(answer.hypotheses, key=lambda h: h.confidence, reverse=True):
        lines.append(hypothesis.statement)
        lines.append(
            f"  confidence: {confidence_label(hypothesis.confidence)} ({hypothesis.confidence:.2f})"
        )
        for item in hypothesis.evidence:
            lines.append(f"  supported by: {_citation_location(item)}")
            if item.description:
                lines.append(f"    {item.description}")
        lines.append("")
    if lines:
        lines.pop()
    return lines


def render_answer(answer: RepoAtlasAnswer, evidence: EvidenceSet) -> str:
    """``ask`` output, with one trust category per section (V1_SPEC §16).

    The sections are deliberately not interchangeable:

    * ``ANSWER`` and ``HYPOTHESIS`` are the model's reading of the evidence;
    * ``VERIFIED EVIDENCE`` is what RepoAtlas retrieved, and nothing else;
    * ``UNKNOWN`` is what the evidence does not settle;
    * ``NEXT CHECK`` proposes an investigation rather than asserting a fact.

    The previous layout printed model statements under a heading called
    ``EVIDENCE``, so a fabricated ``infra/secrets.yaml:42`` read as provenance
    RepoAtlas had verified. Splitting the two is the presentation half of the
    trust rule; grounding is the other half.
    """
    lines = ["ANSWER", "", answer.summary]

    if evidence.candidates:
        lines.extend(["", "VERIFIED EVIDENCE", ""])
        lines.extend(_verified_evidence_lines(evidence))

    if answer.hypotheses:
        lines.extend(["", "HYPOTHESIS", ""])
        lines.extend(_hypothesis_lines(answer))

        # Confidence in the *validated* answer, so it can only fall when grounding
        # removes support — never rise (V1_SPEC §15, §16).
        best = max(hypothesis.confidence for hypothesis in answer.hypotheses)
        lines.extend(["", "CONFIDENCE", "", confidence_label(best)])

    if answer.unknowns:
        lines.extend(["", "UNKNOWN", ""])
        lines.extend(f"- {unknown}" for unknown in answer.unknowns)

    if answer.next_check:
        lines.extend(["", "NEXT CHECK", "", answer.next_check])

    return "\n".join(lines)


def render_diagnosis(answer: RepoAtlasAnswer, evidence: EvidenceSet) -> str:
    """``debug`` output, separating facts from hypotheses (V1_SPEC §15, §16).

    The split is not a confidence threshold on model output. VERIFIED EVIDENCE is
    what RepoAtlas retrieved, which carries provenance; HYPOTHESIS is the model's
    reading of it. That mapping is the trust rule made visible: the model never
    contributes a fact, and the two sections share the vocabulary `ask` uses so
    that the same words mean the same thing in both commands.
    """
    lines = ["SUMMARY", "", answer.summary]

    if evidence.candidates:
        lines.extend(["", "VERIFIED EVIDENCE", ""])
        lines.extend(_verified_evidence_lines(evidence))

    if answer.hypotheses:
        lines.extend(["", "HYPOTHESIS", ""])
        lines.extend(_hypothesis_lines(answer))

    if answer.unknowns:
        lines.extend(["", "UNKNOWN", ""])
        lines.extend(f"- {unknown}" for unknown in answer.unknowns)

    lines.extend(["", "NEXT CHECK", "", answer.next_check or "No further check suggested."])
    return "\n".join(lines)
