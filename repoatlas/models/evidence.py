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
    """Which retriever produced a candidate. Used by rank fusion and by --verbose."""

    EXACT = "exact"
    FILE = "file"
    LEXICAL = "lexical"
    SYMBOL = "symbol"
    SEMANTIC = "semantic"
    GRAPH = "graph"


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
