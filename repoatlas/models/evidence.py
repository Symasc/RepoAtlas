"""Evidence and answer models (ARCHITECTURE.md §5, V1_SPEC §15).

``RepoAtlasAnswer`` and its parts are the structured-output contract for Slice B.
They live here in Slice A because ``locate`` already renders evidence, and because
citation verification will validate model output against ``EvidenceSet``.
"""

from __future__ import annotations

import enum

from pydantic import BaseModel, ConfigDict, Field

from repoatlas.models.telemetry import RetrievalTrace


class EvidenceSource(enum.StrEnum):
    """How a piece of evidence was reached. Used by rank fusion and by --verbose.

    The first six are retrieval stages, and they are the only ones fusion ever
    sees. :attr:`INSPECT` and :attr:`RELATIONSHIP` are not stages: they name
    evidence that was *addressed* rather than searched for — the source of a file,
    entity or span a caller named exactly, and the lines that assert a graph edge
    reached from one.

    They share this enum rather than getting ones of their own because the ledger
    records a route for every piece of evidence it holds, and two vocabularies for
    "how did this arrive" would drift apart the first time one of them gained a
    member. Nothing about fusion changes: neither capability produces candidates,
    so neither member ever reaches a ranking.
    """

    EXACT = "exact"
    FILE = "file"
    LEXICAL = "lexical"
    SYMBOL = "symbol"
    SEMANTIC = "semantic"
    GRAPH = "graph"
    #: Addressed directly by the caller, not retrieved. Never ranked.
    INSPECT = "inspect"
    #: The source lines a deterministic graph edge is asserted by, supplied so the
    #: edge's provenance can be checked rather than only named. Never ranked.
    RELATIONSHIP = "relationship"


class EvidenceTier(enum.IntEnum):
    """Precedence class. **Lower wins, unconditionally.**

    This is the core retrieval principle of V1_SPEC §14 expressed as a type rather
    than as a weight: *deterministic evidence outranks semantic similarity*. An
    exact identifier, an exact error string, an exact symbol name, an environment
    variable and a graph-supported relationship are all facts about the code. An
    embedding neighbour is a guess about what the question meant.

    Scores are only ever compared **within** a tier, so no amount of cosine
    similarity can displace a deterministic match. A candidate found by several
    retrievers takes its best (lowest) tier.
    """

    DETERMINISTIC = 0
    ASSOCIATIVE = 1


class MatchClass(enum.IntEnum):
    """*How precisely the query named this candidate.* **Lower is more specific.**

    Distinct from :class:`EvidenceTier`, and the distinction is the whole point.
    The tier answers "is this a fact about the code or a guess about the
    question". This answers "how exactly did the user identify it". Two candidates
    can be equally `DETERMINISTIC` and still be answers to different questions:
    ``src/config.py`` because the user spelled its whole path, and
    ``tests/config.py`` because it happens to share a basename.

    Before this existed, the ``file`` stage computed that difference and then threw
    it away — every hit became an undifferentiated `DETERMINISTIC` file match — so
    the only ordering signal left to fusion was how many stages had voted. A
    ``tests/config.py`` full of ``config``-shaped symbols and a ``docs/notes.txt``
    that merely *mentions* ``src/config.py`` both collect more votes than a file
    whose sole claim is that its path is exactly what was typed, and the named file
    finished eighth.

    Two boundaries are load-bearing for ranking (see
    :func:`repoatlas.retrieval.ranking.fuse`), and they are the two the user has to
    have *typed a path* to reach:

    * :attr:`EXACT_PATH` — a complete repository-relative path is an unambiguous
      identification of one file, and nothing weaker may outrank it;
    * :attr:`EXACT_SUBPATH` — a multi-component path matched on segment
      boundaries. Explicit intent, but not proof of uniqueness: it may name several
      files, and all of them are anchored rather than one being chosen.

    The classes below them are retained provenance rather than additional
    precedence — they say what happened, they are shown in ``--json``, and they do
    not silently re-rank the funnel. :attr:`EXACT_BASENAME` is deliberately on that
    side of the line: a basename says what a file is called, not where it lives, so
    ``config.py`` is not an assertion about any one of the ten files that have it.
    """

    #: The query spelled this file's whole repository-relative path, optionally
    #: qualified by the repository's own identity (``svc/src/config.py``).
    EXACT_PATH = 0
    #: A trailing sub-path on a segment boundary: ``api/config.py`` for
    #: ``services/api/config.py``. Exact, but it can name several files, so it
    #: anchors every file it matches and asserts uniqueness for none of them.
    #: Segment boundaries are what make it exact — ``pi/config.py`` and
    #: ``myapi/config.py`` match nothing, because this is not a substring test.
    EXACT_SUBPATH = 1
    #: The file's basename. The least specific exact match there is: it says what
    #: the file is called, not where it lives.
    EXACT_BASENAME = 2
    #: An entity whose name, qualified name or key the query spelled exactly.
    EXACT_NAME = 3
    #: The literal text of the query occurs in this span, verified by substring.
    EXACT_CONTENT = 4
    #: Reached by a graph edge from something the query named.
    RELATED = 5
    #: Word-overlap recall from the full-text index.
    LEXICAL = 6
    #: An embedding neighbour.
    SIMILAR = 7


class EvidenceCandidate(BaseModel):
    """One retrieved piece of a real repository, with the provenance that proves it.

    Provenance comes at two granularities, and the difference is not cosmetic:

    * a **span** — ``line_start``/``line_end`` set — is a claim about specific
      lines of a file, which is what content retrieval produces;
    * a **file** — both ``None`` — is a claim about the file's identity, which is
      what a filename or path lookup produces.

    A filename match knows nothing about any line, so it says nothing about one.
    The alternative — citing line 1 because the renderer wanted a number — would
    put a fabricated line range under the one heading that exists to be trusted
    (V1_SPEC §11). Making the fields optional is what lets the honest answer be
    representable at all.
    """

    model_config = ConfigDict(frozen=True)

    #: Display name of the repository. For people, and **not unique**: two
    #: managed repositories may both be called ``common``.
    repository: str
    #: Canonical repository identity (ARCHITECTURE.md §3). This is what fusion,
    #: deduplication and grounding compare; ``repository`` is what is printed.
    #: Empty only for a candidate constructed outside the retrieval path.
    repository_slug: str = ""
    path: str
    line_start: int | None = None
    line_end: int | None = None
    text: str = ""
    score: float = 0.0
    source: EvidenceSource = EvidenceSource.EXACT
    tier: EvidenceTier = EvidenceTier.DETERMINISTIC
    #: How precisely the query identified this candidate. Retained through fusion
    #: and deduplication — a span merged from several stages keeps the strongest
    #: class any of them claimed, exactly as it keeps its strongest tier.
    match: MatchClass = MatchClass.EXACT_CONTENT
    description: str = ""
    entity_id: int | None = None

    @property
    def is_file_level(self) -> bool:
        """True when this candidate is about the file, not about any of its lines."""
        return self.line_start is None and self.line_end is None

    @property
    def identity(self) -> tuple[str, str]:
        """What makes two candidates the same source: identity, then path."""
        return (self.repository_slug or self.repository, self.path)

    @property
    def location(self) -> str:
        """``repo/path``, ``repo/path:line`` or ``repo/path:start-end`` (V1_SPEC §11)."""
        return self.located(self.repository)

    def located(self, prefix: str) -> str:
        """``location`` with an explicit repository prefix.

        Rendering picks the prefix from the surrounding set: the display name when
        it identifies one repository, the canonical identity when it does not.
        Printing ``common/index.ts:1`` for two different repositories is a citation
        that does not cite anything (V1_SPEC §11).
        """
        if self.line_start is None:
            return f"{prefix}/{self.path}"
        if self.line_end is None or self.line_start == self.line_end:
            return f"{prefix}/{self.path}:{self.line_start}"
        return f"{prefix}/{self.path}:{self.line_start}-{self.line_end}"


class EvidenceSet(BaseModel):
    """A bounded set of candidates. Never a whole repository (V1_SPEC §14)."""

    model_config = ConfigDict(frozen=True)

    question: str
    candidates: tuple[EvidenceCandidate, ...] = ()
    scope: str = ""
    primary_repository: str | None = None
    truncated: bool = False
    #: Stage instrumentation. An observation about the search, never evidence.
    trace: RetrievalTrace | None = None

    @property
    def deterministic(self) -> tuple[EvidenceCandidate, ...]:
        return tuple(c for c in self.candidates if c.tier is EvidenceTier.DETERMINISTIC)

    @property
    def ambiguous_repositories(self) -> frozenset[str]:
        """Display names that name more than one repository in *this* set.

        Disambiguation is conditional on purpose. Most workspaces have no
        duplicated names, and printing a full path on every line to guard against
        a case that is not occurring would make the ordinary output worse
        (V1_SPEC §16).
        """
        seen: dict[str, set[str]] = {}
        for candidate in self.candidates:
            seen.setdefault(candidate.repository, set()).add(
                candidate.repository_slug or candidate.repository
            )
        return frozenset(name for name, slugs in seen.items() if len(slugs) > 1)

    def prefix_for(self, candidate: EvidenceCandidate) -> str:
        """The repository prefix to print for one candidate of this set."""
        if candidate.repository in self.ambiguous_repositories:
            return candidate.repository_slug or candidate.repository
        return candidate.repository


class Evidence(BaseModel):
    """A citation in a model answer. Verified against the supplied EvidenceSet.

    ``repository_path`` is the canonical identity the evidence block carried. It
    is optional because a concise citation naming an unambiguous repository is
    perfectly good — but when two supplied sources share a display name, a
    citation without it names neither, and grounding refuses it rather than
    picking one (ARCHITECTURE.md §11).
    """

    repository: str
    repository_slug: str | None = None
    path: str
    line_start: int | None = None
    line_end: int | None = None
    description: str


class Hypothesis(BaseModel):
    """A model's reading of the evidence. **Never a repository fact.**

    Named for what it is. The previous name — ``Finding`` — invited exactly the
    presentation bug it produced: a renderer printed these statements under a
    heading called EVIDENCE, so unverified prose read as verified provenance.

    ``evidence`` is the set of citations the statement rests on. Every one of
    them must resolve to something the model was actually shown, or the whole
    hypothesis goes: a claim that references a file it was never given is not
    made trustworthy by also referencing one it was (V1_SPEC §15).
    """

    statement: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[Evidence]


class RepoAtlasAnswer(BaseModel):
    """The validated structured answer (V1_SPEC §15).

    Each field carries exactly one trust category, and no field carries two:

    ============= =================================================================
    ``summary``   model inference — a conclusion drawn from the evidence
    ``hypotheses`` model inference — specific readings, each with its citations
    ``unknowns``  what the evidence does **not** establish
    ``next_check`` a proposed investigation, not a claim that anything exists
    ============= =================================================================

    Verified facts are deliberately absent: they are not the model's to supply.
    They come from the :class:`EvidenceSet` the retriever produced, and the
    renderer takes them from there (ARCHITECTURE.md §11).
    """

    summary: str
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    next_check: str | None = None
