"""Answer grounding — the enforcement point (ARCHITECTURE.md §11, V1_SPEC §15).

The trust rule is a guarantee, and a guarantee cannot rest on model compliance.
So after a model answer validates against the schema, it is checked against the
evidence set that was *actually supplied*.

The rule this module exists to enforce, stated exactly:

    A citation validates the evidence reference. It does **not** validate
    arbitrary prose attached to that citation.

Verifying only the typed ``Evidence`` objects was never enough. Every other field
of an answer is free text that reaches the user, and one valid citation used to
carry a whole fabricated statement with it. Three rules now apply:

1. **Every citation of a hypothesis must resolve.** Not one of them — all. A
   claim that references a file it was never shown is not made trustworthy by
   also referencing one it was.
2. **No free-text field may name a repository object that was not supplied.**
   Applied to the summary, to every statement, to every unknown and to the next
   check, because a fabricated path in prose is a fabrication wherever it sits.
3. **Confidence may not outlive its support.** A hypothesis whose citations were
   narrowed cannot keep the number the model chose before that happened.

What this deliberately does *not* attempt is to decide whether a sentence with no
repository object in it is true — "the credential is hardcoded in the deployment
configuration" names nothing checkable. That claim is not rejected here; it is
carried as a hypothesis and rendered as one. Labelling is the guarantee for prose
that cannot be verified, and it is why :class:`Hypothesis` is named as it is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from repoatlas.models.evidence import Evidence, EvidenceSet, Hypothesis, RepoAtlasAnswer

INSUFFICIENT_EVIDENCE = "INSUFFICIENT EVIDENCE"

#: Replaces a summary that made an unsupported claim. Saying nothing is correct;
#: rewriting the model's sentence into a supported one would be inventing.
_UNSUPPORTED_SUMMARY = "The retrieved evidence does not support a conclusion."

#: Ceiling applied to a hypothesis whose citations grounding had to narrow. The
#: model chose its number before the support changed, so the number is stale.
_CLAMPED_CONFIDENCE_CEILING = 0.5

#: File extensions that make a token a repository-object reference rather than an
#: ordinary word. Kept explicit so that "e.g.", "i.e." and version numbers such
#: as "4.7.2" are not mistaken for paths.
_PATH_EXTENSIONS = frozenset(
    {
        "bash",
        "c",
        "cfg",
        "conf",
        "cpp",
        "cs",
        "css",
        "env",
        "go",
        "h",
        "hcl",
        "html",
        "ini",
        "j2",
        "java",
        "jinja",
        "js",
        "json",
        "jsx",
        "key",
        "kt",
        "lock",
        "md",
        "mjs",
        "pem",
        "php",
        "pl",
        "properties",
        "proto",
        "ps1",
        "py",
        "rb",
        "rs",
        "rst",
        "scala",
        "sh",
        "sls",
        "sql",
        "swift",
        "tf",
        "tfvars",
        "toml",
        "ts",
        "tsx",
        "txt",
        "xml",
        "yaml",
        "yml",
        "zsh",
    }
)

#: A token that looks like a path: either rooted/nested (contains "/") or ending
#: in a known source extension, optionally followed by ``:line`` or ``:start-end``.
_REFERENCE = re.compile(
    r"(?<![\w/.])(/?[\w.\-]+(?:/[\w.\-]+)*\.[A-Za-z][\w]{0,9}|/[\w.\-]+(?:/[\w.\-]+)+)"
    r"(?::\d+(?:-\d+)?)?"
)


@dataclass(frozen=True, slots=True)
class GroundingReport:
    """What verification changed. Surfaced under ``--verbose``.

    Rising counts across a corpus are a regression signal for prompts and models,
    which is why each is counted rather than discarded.
    """

    answer: RepoAtlasAnswer
    dropped_citations: int = 0
    clamped_citations: int = 0
    demoted_findings: int = 0
    unsupported: tuple[str, ...] = field(default=())
    #: Why each removed hypothesis went, keyed by its position in the submitted
    #: answer: any of :data:`NAMED_REFERENCE`, :data:`FABRICATED_CITATION` and
    #: :data:`NO_EVIDENCE`. Recorded rather than inferred, so that a caller never
    #: blames the prose for a removal the citations caused.
    removals: tuple[tuple[int, frozenset[str]], ...] = field(default=())


#: The statement named a repository object nobody supplied.
NAMED_REFERENCE = "named-reference"
#: A citation resolved to no supplied source.
FABRICATED_CITATION = "fabricated-citation"
#: The hypothesis carried no citation that survived.
NO_EVIDENCE = "no-evidence"


@dataclass(frozen=True, slots=True)
class SuppliedEvidence:
    """What was actually shown to the model, indexed for citation resolution.

    ``spans`` is keyed by repository **identity** and path — not by display name.
    Two managed repositories may both be called ``common``, and a span map keyed on
    the name made evidence supplied from ``teamA/common/index.ts`` validate a
    citation to ``teamB/common/index.ts``: same name, same path, same lines, quite
    possibly the same bytes, and an entirely different repository.

    ``by_display`` records which identities a display name reaches, which is what
    lets a concise citation resolve when it is unambiguous and be refused when it
    is not (ARCHITECTURE.md §11).
    """

    spans: dict[tuple[str, str], list[tuple[int, int]]]
    by_display: dict[tuple[str, str], tuple[tuple[str, str], ...]]


def _supplied_evidence(evidence: EvidenceSet) -> SuppliedEvidence:
    """Index the supplied candidates by identity and by display name.

    A file supplied only as *identity* — a filename match, which carries no line
    range — appears with an empty span list. The distinction is the point: the
    model may say the file exists, because it was shown that; it may not attribute
    anything to a line of it, because it was shown no lines.
    """
    spans: dict[tuple[str, str], list[tuple[int, int]]] = {}
    display: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for candidate in evidence.candidates:
        key = candidate.identity
        supplied = spans.setdefault(key, [])
        reachable = display.setdefault((candidate.repository, candidate.path), [])
        if key not in reachable:
            reachable.append(key)
        if candidate.line_start is None or candidate.line_end is None:
            continue
        supplied.append((candidate.line_start, candidate.line_end))
    return SuppliedEvidence(
        spans=spans,
        by_display={name: tuple(keys) for name, keys in display.items()},
    )


def _supplied_references(evidence: EvidenceSet) -> set[str]:
    """Every spelling of a supplied file that prose may legitimately use.

    A model referring to ``app.py``, ``svc/app.py`` or ``src/app.py`` is naming
    the same supplied file three ways, and all three are honest. Anything outside
    this set names a repository object nobody showed it.
    """
    names: set[str] = set()
    for candidate in evidence.candidates:
        path = candidate.path
        slug = candidate.repository_slug
        names.add(path)
        names.add(f"{candidate.repository}/{path}")
        names.add(PurePosixPath(path).name)
        names.add(candidate.repository)
        if slug:
            # The identity is a spelling of the same supplied file, and it is the
            # spelling the evidence block asked the model to cite by.
            names.add(slug)
            names.add(f"{slug}/{path}")
            for index in range(1, len(PurePosixPath(slug).parts)):
                names.add("/".join(PurePosixPath(slug).parts[index:]))
        # Every trailing sub-path, so "b/c.py" matches a supplied "a/b/c.py".
        parts = PurePosixPath(path).parts
        for index in range(1, len(parts)):
            names.add("/".join(parts[index:]))
    return names


def _unsupported_references(text: str, allowed: set[str]) -> list[str]:
    """Repository objects named in ``text`` that were never supplied.

    Deliberately a *secondary* defence. It catches the fabrication that names a
    file, which is the shape that reads as provenance to a human — the primary
    defence for everything else is that model prose is labelled as inference and
    can never occupy the verified-evidence section (ARCHITECTURE.md §11).
    """
    found: list[str] = []
    for match in _REFERENCE.finditer(text):
        token = match.group(1)
        suffix = PurePosixPath(token).suffix.lstrip(".").lower()
        if "/" not in token and suffix not in _PATH_EXTENSIONS:
            continue
        if token in allowed or token.lstrip("/") in allowed:
            continue
        found.append(match.group(0))
    return found


def _resolve_source(item: Evidence, supplied: SuppliedEvidence) -> tuple[str, str] | None:
    """Which supplied source a citation names, or ``None`` if that is not decidable.

    A citation carrying ``repository_slug`` names one source exactly. A citation
    carrying only a display name is resolved **only when that name identifies a
    single supplied source**; when two supplied repositories share it, the citation
    names neither and is refused. Choosing between them would be the system
    inventing the provenance of a claim, which is the one thing grounding exists to
    prevent (V1_SPEC §15).

    Concise citations therefore keep working for ordinary repositories, and stop
    working exactly where they stop meaning something.
    """
    if item.repository_slug:
        key = (item.repository_slug, item.path)
        return key if key in supplied.spans else None

    candidates = supplied.by_display.get((item.repository, item.path), ())
    return candidates[0] if len(candidates) == 1 else None


def _verify_evidence(item: Evidence, supplied: SuppliedEvidence) -> tuple[Evidence | None, bool]:
    """Return the grounded citation and whether it needed clamping.

    ``None`` means the citation was not supported by the supplied set at all —
    including the case where it is *ambiguous*, which is a failure to identify a
    source rather than a failure to find one.
    """
    key = _resolve_source(item, supplied)
    if key is None:
        return None, False
    spans = supplied.spans[key]
    # Record the identity that was resolved. Not an invention: it is the identity
    # of the source the model was actually shown, and carrying it means the
    # rendered citation says which `common` a hypothesis rests on.
    item = (
        item
        if item.repository_slug == key[0]
        else item.model_copy(update={"repository_slug": key[0]})
    )

    # A file-level citation with no line numbers is grounded as far as it goes:
    # the file really was supplied. Inventing a span for it would be worse.
    if item.line_start is None and item.line_end is None:
        return item, False

    start = item.line_start if item.line_start is not None else item.line_end
    end = item.line_end if item.line_end is not None else item.line_start
    assert start is not None and end is not None
    if start > end:
        start, end = end, start

    for span_start, span_end in spans:
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


def ground_answer(answer: RepoAtlasAnswer, evidence: EvidenceSet) -> GroundingReport:
    """Strip every claim the supplied evidence does not support."""
    supplied = _supplied_evidence(evidence)
    allowed = _supplied_references(evidence)
    # The user introduced the words in their own question; echoing one back is
    # not the model inventing a repository object.
    allowed.update(_REFERENCE.findall(evidence.question))

    dropped = 0
    clamped = 0
    unsupported: list[str] = []
    surviving: list[Hypothesis] = []
    removals: list[tuple[int, frozenset[str]]] = []

    for position, hypothesis in enumerate(answer.hypotheses):
        kept: list[Evidence] = []
        fabricated = 0
        narrowed = False
        for item in hypothesis.evidence:
            grounded, was_clamped = _verify_evidence(item, supplied)
            if grounded is None:
                fabricated += 1
                continue
            narrowed = narrowed or was_clamped
            clamped += int(was_clamped)
            kept.append(grounded)
        dropped += fabricated

        named = _unsupported_references(hypothesis.statement, allowed)
        # Rule 1 (every citation resolves) and rule 2 (no invented object) are
        # both fatal: a statement that fails either one is not a reading of the
        # evidence, whatever else it also cites.
        if not kept or fabricated or named:
            unsupported.append(hypothesis.statement)
            reasons = {NAMED_REFERENCE} if named else set()
            if fabricated:
                reasons.add(FABRICATED_CITATION)
            if not kept and not fabricated:
                reasons.add(NO_EVIDENCE)
            removals.append((position, frozenset(reasons)))
            continue

        surviving.append(
            hypothesis.model_copy(
                update={
                    "evidence": kept,
                    "confidence": _grounded_confidence(hypothesis.confidence, narrowed),
                }
            )
        )

    summary, summary_dropped = _grounded_summary(answer.summary, allowed, surviving)
    unsupported.extend(summary_dropped)

    unknowns, unknown_dropped = _grounded_unknowns(answer.unknowns, allowed)
    unsupported.extend(unknown_dropped)

    next_check, next_dropped = _grounded_next_check(answer.next_check, allowed)
    unsupported.extend(next_dropped)

    if unsupported:
        # Say that something was removed, never *what*. Quoting the removed claim
        # back to the user would put the fabricated path on screen again — under a
        # heading that reads as RepoAtlas's own words rather than the model's,
        # which is worse than where it started. The count is the honest signal;
        # the text stays in the report for `--verbose` diagnostics.
        unknowns.append(
            f"{len(unsupported)} model claim(s) were removed: they referenced repositories, "
            "files or lines that the retrieved evidence does not contain."
        )

    verified = answer.model_copy(
        update={
            "summary": summary,
            "hypotheses": surviving,
            "unknowns": unknowns,
            "next_check": next_check,
        }
    )

    return GroundingReport(
        answer=verified,
        dropped_citations=dropped,
        clamped_citations=clamped,
        demoted_findings=len(answer.hypotheses) - len(surviving),
        unsupported=tuple(unsupported),
        removals=tuple(removals),
    )


def _grounded_confidence(claimed: float, narrowed: bool) -> float:
    """The model's number, capped by what verification left standing.

    Only lowered, never raised: grounding can remove support, and removing
    support cannot make a claim stronger (V1_SPEC §15).
    """
    return min(claimed, _CLAMPED_CONFIDENCE_CEILING) if narrowed else claimed


def _grounded_summary(
    summary: str, allowed: set[str], surviving: list[Hypothesis]
) -> tuple[str, list[str]]:
    """The summary, replaced outright when it names something unsupplied.

    Replaced rather than edited. Removing a clause would leave a sentence the
    model never wrote and whose meaning nobody checked; saying that no conclusion
    is supported is the honest alternative.

    When nothing survived at all, the answer says ``INSUFFICIENT EVIDENCE`` —
    the sentinel the rest of the system already recognizes — even if the summary
    was independently unsupported. The stronger statement subsumes the weaker one,
    and the removal is still counted.
    """
    removed = [summary] if _unsupported_references(summary, allowed) else []
    if not surviving:
        return INSUFFICIENT_EVIDENCE, removed
    if removed:
        return _UNSUPPORTED_SUMMARY, removed
    return summary, []


def _grounded_unknowns(unknowns: list[str], allowed: set[str]) -> tuple[list[str], list[str]]:
    """Unknowns describe missing knowledge; they may not invent an object.

    "The runtime value of SERVICE_HOST is not established" is an unknown.
    "prod/db.env may override it" asserts a file exists, in the one field whose
    whole purpose is to admit that something is *not* established (V1_SPEC §15).
    """
    kept: list[str] = []
    removed: list[str] = []
    for unknown in unknowns:
        if _unsupported_references(unknown, allowed):
            removed.append(unknown)
        else:
            kept.append(unknown)
    return kept, removed


def _grounded_next_check(next_check: str | None, allowed: set[str]) -> tuple[str | None, list[str]]:
    """A proposal may not presuppose a repository object nobody has seen.

    "Inspect the runtime value of SERVICE_HOST" proposes an investigation.
    "Open infra/secrets.yaml and rotate the credential" asserts that the file
    exists and that it holds a credential — a repository claim wearing the
    grammar of a suggestion.
    """
    if next_check is None:
        return None, []
    if _unsupported_references(next_check, allowed):
        return None, [next_check]
    return next_check, []
