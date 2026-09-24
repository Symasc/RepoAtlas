"""Rank fusion, tiering, deduplication and budgeting (ARCHITECTURE.md §10, stages 5-8).

Reciprocal-rank fusion, because BM25 scores, cosine similarities, literal-match
scores and graph distances are not commensurable and RRF needs no per-source
calibration to combine them stably.

**Fusion never crosses a tier.** RRF answers "how strongly do the retrievers agree
about this span"; the tier answers "is this a fact about the code or a guess about
the question", and the second question is settled before the first is asked. That
ordering is the core retrieval principle of V1_SPEC §14 made structural: no
similarity score, however high, moves an embedding neighbour above an exact
identifier, an exact error string, an exact symbol, an environment variable or a
graph-supported relationship.

**Nor does fusion erase match specificity.** Tier and score together were not
enough, because they conflate two different things a candidate can be strong
*for*. A question naming ``src/config.py`` produced an exact-path candidate for
that file — one deterministic vote — while a ``tests/config.py`` stuffed with
``config``-shaped symbols collected exact-content, entity-name, lexical and
semantic votes, and a ``docs/notes.txt`` mentioning the target path a hundred
times won the literal stage outright. All were `DETERMINISTIC`, so the tier
separated nothing and the fused score decided — by vote count. The file the user
named finished eighth behind a text file that merely talks about it.

So there are three ordering keys, not two, and each answers its own question:

============== ==================================================================
`EvidenceTier` is this a fact about the code, or a guess about the question?
:class:`MatchClass` how precisely did the query identify it?
fused score    how strongly do the retrievers agree about it?
============== ==================================================================

The specificity key is deliberately a **set of boundaries rather than a
gradient**, and there are exactly two of them:

============================ =====================================================
complete repository-relative the one spelling that identifies exactly one file and
path (`EXACT_PATH`)          can mean nothing else
component suffix path        an explicitly typed multi-component path matched on
(`EXACT_SUBPATH`)            segment boundaries; explicit intent, but it can name
                             several files
============================ =====================================================

Within those file boundaries, an exact qualified symbol named by the query
precedes component-name matches and graph neighbors. Other candidates retain
their agreement ordering. A
gradient over all eight classes would have re-ranked the whole funnel on a signal
nobody asked for; these two boundaries answer the questions that were actually
ambiguous.

The second boundary was added because the first one alone left the same
information loss one level down. ``locate api/config.py`` classified
``services/api/config.py`` as `EXACT_SUBPATH` correctly — and then ranked it
tenth, behind six chunks of a ``README.md`` that merely quotes the string
``api/config.py`` two hundred times. The class was computed and then not used for
anything, so a document *about* the file outranked the file. See :func:`fuse`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from repoatlas.models.evidence import EvidenceCandidate, EvidenceTier, MatchClass

#: RRF damping constant. 60 is the value from the original formulation.
_RRF_K = 60.0

#: Fraction of the shorter span that must overlap before the lower-ranked
#: candidate is treated as redundant rather than as additional context.
_OVERLAP_THRESHOLD = 0.5

#: What makes two candidates "the same evidence": repository **identity**, path
#: and line span. The identity is deliberately not the display name — two managed
#: repositories may both be called ``common``, and keying on the name fused
#: ``common/index.ts:1`` from two different repositories into one candidate.
#: A file-level candidate carries no line range, so ``None`` is part of the key
#: rather than a value to fill in.
_Key = tuple[str, str, int | None, int | None]


class Reranker(Protocol):
    """The V2 seam. ``NoopReranker`` is what V1 ships."""

    def rerank(
        self, question: str, candidates: list[EvidenceCandidate]
    ) -> list[EvidenceCandidate]: ...


class NoopReranker:
    """Preserves fusion order. Replaced by a cross-encoder in V2.

    A V2 reranker must respect the tier boundary the same way fusion does: it may
    reorder within a tier, never across one, or it would reintroduce exactly the
    displacement V1_SPEC §14 forbids.
    """

    def rerank(self, question: str, candidates: list[EvidenceCandidate]) -> list[EvidenceCandidate]:
        return candidates


#: Ordering positions for the specificity key. Three values, not eight: see the
#: module docstring on why this is a set of boundaries rather than a gradient.
_EXACT, _SUFFIX, _UNNAMED = 0, 1, 2

#: The match classes that name a *file* rather than merely describe it, and the
#: ordering position each earns. A candidate carries a match class of its own, but
#: specificity is a property of the file: if the user spelled ``src/config.py``,
#: then everything retrieved from ``src/config.py`` is about the file they named,
#: and everything retrieved from ``tests/config.py`` is not. Anchoring the file
#: rather than the single file-identity candidate is what keeps "why does
#: src/config.py read DATABASE_URL?" answering with the assignment inside the
#: named file rather than with the filename alone.
#:
#: Two positions, because the two classes are different claims. A complete
#: repository-relative path identifies exactly one file and can mean nothing else.
#: A component suffix identifies a file *shape* — ``api/config.py`` names every
#: indexed file whose path ends on that boundary, which may be one file or five —
#: so it is explicit intent without being proof of uniqueness, and it sits below
#: exact identity and above everything the user did not spell.
#:
#: Nothing below `EXACT_SUBPATH` earns a position. A basename says what a file is
#: called, not where it lives; an entity name, a content mention and a lexical or
#: semantic neighbour are weaker still. They remain ordered by agreement, exactly
#: as before.
_ANCHORS = {
    MatchClass.EXACT_PATH: _EXACT,
    MatchClass.EXACT_SUBPATH: _SUFFIX,
}


def _key(candidate: EvidenceCandidate) -> _Key:
    repository, path = candidate.identity
    return (repository, path, candidate.line_start, candidate.line_end)


def _ordering(key: _Key) -> tuple[str, str, bool, int, int]:
    """Tie-break within a tier and score: specific evidence before coarse.

    A span names the lines it is about; a file-level match names only the file. So
    when the two agree exactly on how strongly they were retrieved, the one that
    says more goes first — which is why "where is SERVICE_HOST set in
    src/config.py?" leads with the assignment rather than with the filename that
    also matched. It is a tie-break, not a tier: a file match still outranks
    everything associative.

    It also keeps the ordering total in the presence of ``None``, which comparing
    the raw keys would not.
    """
    repository, path, start, end = key
    return (repository, path, start is None, start or 0, end or 0)


def fuse(
    ranked_lists: Sequence[Sequence[EvidenceCandidate]],
    *,
    primary_repository: str | None = None,
    primary_boost: float = 0.5,
) -> list[EvidenceCandidate]:
    """Combine ranked lists into one ordering: tier first, fused score second.

    The primary-repository boost biases toward the repository the question was
    asked in without walling off other repositories — "primary scope, not a wall"
    (V1_SPEC §6). It is added to the score, so it competes inside a tier and never
    promotes a candidate across one. ``primary_repository`` is a repository
    **identity**: matching on a display name meant that standing in ``teamB/common``
    boosted evidence from ``teamA/common``, because both are called ``common``.

    Each list contributes **one** reciprocal rank per span, its best. A span can
    reach a single retriever's list several times — a file whose lines cannot be
    split becomes many chunks sharing one line range — and summing those would
    make the fused score a measure of file size rather than of agreement between
    retrievers, letting generated data outrank the answer purely by volume.

    Ordering is ``(tier, path specificity, qualified-symbol intent, -score, tie-break)``.
    The specificity key
    holds the guarantee the score cannot express: **a candidate from a file the
    query named by path outranks every candidate from a file that was merely named
    less exactly** — a shared basename, a symbol with a matching name, a lexical or
    semantic neighbour, or a document that quotes the path. It is a rank position
    rather than a bonus, so no quantity of agreement can add up to it, and it is
    applied strictly *inside* a tier, so it can never lift a guess above a fact.

    Two positions are anchored, in order: a complete repository-relative path, then
    a component suffix path. The suffix position is explicit intent without a claim
    of uniqueness — ``api/config.py`` may name ``services/api/config.py`` and
    ``legacy/api/config.py`` both, and both are anchored. Ambiguity among them is
    resolved *within* the class by the fused score, which is where the
    primary-repository boost does its work: the candidate in the repository the
    question was asked from sorts first, without either file being promoted over
    the other by anything structural. RepoAtlas does not decide which one was meant.

    Exact qualified symbol definitions precede component-name matches and graph
    neighbors inside each path boundary. Ambiguous qualified matches remain peers;
    this priority never promotes a candidate across a trust tier. With no qualified
    match or path anchor, the original fused-score ordering is unchanged.
    """
    scores: dict[_Key, float] = {}
    tiers: dict[_Key, EvidenceTier] = {}
    matches: dict[_Key, MatchClass] = {}
    best: dict[_Key, EvidenceCandidate] = {}
    qualified: set[_Key] = set()

    for ranked in ranked_lists:
        counted: set[_Key] = set()
        for rank, candidate in enumerate(ranked, start=1):
            key = _key(candidate)
            if candidate.qualified_match:
                qualified.add(key)
            if key not in counted:
                counted.add(key)
                scores[key] = scores.get(key, 0.0) + 1.0 / (_RRF_K + rank)

            # A span found by several retrievers takes its strongest claim: if any
            # of them reached it deterministically, it is deterministic.
            current = tiers.get(key)
            tiers[key] = candidate.tier if current is None else min(current, candidate.tier)

            # And its strongest *match class*, by the same rule and for the same
            # reason. `min` is commutative, so merging cannot depend on the order
            # the stages happened to run in: a span that arrived once as an exact
            # path and four times as a lexical neighbour is an exact path either
            # way round.
            claimed = matches.get(key)
            matches[key] = candidate.match if claimed is None else min(claimed, candidate.match)

            # The representative candidate is the strongest claim about this span:
            # the best tier first, and within a tier the highest raw score. It
            # supplies the text and the description; the merged tier and match
            # class above are stamped onto the copy below, so what is *returned*
            # carries the strongest of each regardless of which stage won here.
            existing = best.get(key)
            if existing is None or (candidate.tier, -candidate.score) < (
                existing.tier,
                -existing.score,
            ):
                best[key] = candidate

    if primary_repository:
        for key in scores:
            if key[0] == primary_repository:
                scores[key] += primary_boost

    anchored = _anchored_files(matches)
    ordered = sorted(
        scores,
        key=lambda key: (
            tiers[key],
            anchored.get(key[:2], _UNNAMED),
            key not in qualified,
            -scores[key],
            _ordering(key),
        ),
    )
    return [
        best[key].model_copy(
            update={
                "score": scores[key],
                "tier": tiers[key],
                "match": matches[key],
                "qualified_match": key in qualified,
            }
        )
        for key in ordered
    ]


def _anchored_files(matches: Mapping[_Key, MatchClass]) -> dict[tuple[str, str], int]:
    """``(repository, path)`` → ordering position, for every file the query named.

    A file takes its **strongest** anchor. One reached both as a complete path and
    as a component suffix was named completely, and the weaker reading of the same
    evidence must not lower it — the same ``min`` rule fusion already applies to
    tiers and match classes, for the same reason.

    Derived from the candidates themselves rather than passed in, which keeps
    ``fuse`` a pure function of its input and makes the anchors independent of the
    order the stages appended their lists.
    """
    anchored: dict[tuple[str, str], int] = {}
    for key, match in matches.items():
        position = _ANCHORS.get(match)
        if position is None:
            continue
        identity = key[:2]
        current = anchored.get(identity)
        if current is None or position < current:
            anchored[identity] = position
    return anchored


def enforce_precedence(candidates: Sequence[EvidenceCandidate]) -> list[EvidenceCandidate]:
    """Partition by tier and then by path specificity, preserving order inside each.

    Fusion already orders by both, so on the V1 path this changes nothing. It
    exists because the guarantees of V1_SPEC §14 must hold at the **boundary**, not
    merely inside one function: between fusion and the caller sits the ``Reranker``
    seam, and a V2 cross-encoder is exactly the kind of component whose ranking is
    opaque. One that reordered across tiers would silently undo the rule the rest
    of the funnel is built on; one that reordered *within* a tier — which it is
    explicitly allowed to do — could just as silently drop a file the user named by
    path below one that merely shares its basename or quotes it. Both anchored
    positions are restored here, not only the exact-path one: a cross-encoder that
    loves a README is exactly the component that would otherwise undo the suffix
    boundary.

    Applying it before budgeting matters as much as applying it before returning:
    a hard token budget spent on embedding neighbours would starve deterministic
    evidence out of the answer just as effectively as ranking it lower.

    ``sorted`` is stable, so within a tier and a specificity the incoming order —
    including whatever a reranker decided — is preserved exactly.
    """
    anchored: dict[tuple[str, str], int] = {}
    for candidate in candidates:
        position = _ANCHORS.get(candidate.match)
        if position is None:
            continue
        current = anchored.get(candidate.identity)
        if current is None or position < current:
            anchored[candidate.identity] = position
    return sorted(
        candidates,
        key=lambda candidate: (
            candidate.tier,
            anchored.get(candidate.identity, _UNNAMED),
        ),
    )


def _same_source(a: EvidenceCandidate, b: EvidenceCandidate) -> bool:
    """Whether two candidates come from the same file of the same repository.

    Compared by identity. Two repositories called ``common`` each holding an
    ``index.ts`` are two sources, however identical they look — and identical is
    exactly what a shared library copied between teams tends to be.
    """
    return a.identity == b.identity


def _overlap(a: EvidenceCandidate, b: EvidenceCandidate) -> float:
    """Fraction of the shorter span that the two candidates share.

    A file-level candidate overlaps nothing: it makes no claim about any line, so
    there is no span for a line-level candidate to repeat. Treating it as covering
    the whole file would delete the very evidence that answers the question, and
    treating a span as covering the file would delete the filename match that
    proves the file exists — the two are different facts and both are kept.
    """
    if not _same_source(a, b):
        return 0.0
    if a.line_start is None or b.line_start is None:
        return 0.0
    a_end = a.line_end if a.line_end is not None else a.line_start
    b_end = b.line_end if b.line_end is not None else b.line_start
    shared = min(a_end, b_end) - max(a.line_start, b.line_start) + 1
    if shared <= 0:
        return 0.0
    shortest = min(a_end - a.line_start + 1, b_end - b.line_start + 1)
    return shared / shortest if shortest else 0.0


def _contains(kept: EvidenceCandidate, candidate: EvidenceCandidate) -> bool:
    """Whether ``kept`` already covers every line ``candidate`` cites."""
    if not _same_source(kept, candidate):
        return False
    if kept.line_start is None or candidate.line_start is None:
        # Two file-level candidates for one file are the same candidate; fusion
        # has already collapsed them by key, so anything reaching here differs.
        return kept.is_file_level and candidate.is_file_level
    kept_end = kept.line_end if kept.line_end is not None else kept.line_start
    end = candidate.line_end if candidate.line_end is not None else candidate.line_start
    return kept.line_start <= candidate.line_start and kept_end >= end


def deduplicate(
    candidates: list[EvidenceCandidate], *, threshold: float = _OVERLAP_THRESHOLD
) -> list[EvidenceCandidate]:
    """Drop candidates that mostly repeat a higher-ranked span of the same file.

    Chunk windows overlap by design, so the same lines arrive several times. A
    candidate is dropped when it is contained in a kept one, or when it shares more
    than ``threshold`` of the shorter span — below that the extra lines are real
    additional context and are kept.

    Spans are never merged into a synthetic range: the citation must point at text
    that was actually retrieved (V1_SPEC §11).
    """
    kept: list[EvidenceCandidate] = []
    for candidate in candidates:
        redundant = any(_contains(k, candidate) or _overlap(k, candidate) > threshold for k in kept)
        if not redundant:
            kept.append(candidate)
    return kept


def apply_budget(
    candidates: Sequence[EvidenceCandidate], *, max_items: int, token_budget: int
) -> tuple[list[EvidenceCandidate], bool, int]:
    """Bound the evidence set by item count **and** by tokens (V1_SPEC §14).

    Returns the kept candidates, whether anything was dropped, and the token
    estimate of what was kept. Both bounds are hard: whole repositories are never
    sent to a model.
    """
    kept: list[EvidenceCandidate] = []
    tokens = 0
    for candidate in candidates:
        if len(kept) >= max_items:
            return kept, True, tokens
        estimate = max(1, len(candidate.text) // 4)
        if tokens + estimate > token_budget and kept:
            return kept, True, tokens
        tokens += estimate
        kept.append(candidate)
    return kept, False, tokens
