"""Evidence and answer models (ARCHITECTURE.md §5, V1_SPEC §15).

``RepoAtlasAnswer`` and its parts are the structured-output contract for Slice B.
They live here in Slice A because ``locate`` already renders evidence, and because
citation verification will validate model output against ``EvidenceSet``.
"""

from __future__ import annotations

import enum
import inspect
from collections.abc import Mapping
from typing import Annotated, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from repoatlas.compat import StrEnum
from repoatlas.models.parsing import EntityType
from repoatlas.models.telemetry import RetrievalTrace


class EvidenceSource(StrEnum):
    """How a piece of evidence was reached. Used by rank fusion and by --verbose.

    The first six are retrieval stages, and they are the only ones fusion ever
    sees. :attr:`INSPECT`, :attr:`RELATIONSHIP`, :attr:`DEPENDENCY` and
    :attr:`HTTP_OPERATION` are not stages: they name evidence that was *addressed*
    rather than searched for — the source of a file, entity or span a caller named
    exactly, the lines that assert a graph edge reached from one, the manifest line
    stating a package fact, and the lines where an HTTP request is written or a
    route declared.

    They share this enum rather than getting ones of their own because the ledger
    records a route for every piece of evidence it holds, and two vocabularies for
    "how did this arrive" would drift apart the first time one of them gained a
    member. Nothing about fusion changes: none of these capabilities produces
    candidates, so none of these members ever reaches a ranking.
    """

    EXACT = "exact"
    FILE = "file"
    LEXICAL = "lexical"
    SYMBOL = "symbol"
    GRAPH = "graph"
    #: Addressed directly by the caller, not retrieved. Never ranked.
    INSPECT = "inspect"
    #: The source lines a deterministic graph edge is asserted by, supplied so the
    #: edge's provenance can be checked rather than only named. Never ranked.
    RELATIONSHIP = "relationship"
    #: The manifest or lockfile line stating a dependency, supplied so a caller can
    #: read what the file actually says rather than trust a parsed field. Never
    #: ranked.
    DEPENDENCY = "dependency"
    #: The lines where an HTTP request is written or a route declared, as written.
    #: They show what the source states at that place — the call, or the decorator
    #: — and nothing decided elsewhere: not a helper's default method, a wrapper's
    #: base, a controller prefix, or the server's spelling of a parameter. Distinct
    #: from :attr:`RELATIONSHIP` because many of these rows assert no graph edge at
    #: all. Never ranked.
    HTTP_OPERATION = "http_operation"


class EvidenceTier(enum.IntEnum):
    """Precedence class. **Lower wins, unconditionally.**

    This is the core retrieval principle of V1_SPEC §14 expressed as a type rather
    than as a weight: *deterministic evidence outranks association*. An exact
    identifier, an exact error string, an exact symbol name, an environment
    variable and a graph-supported relationship are all facts about the code.
    Word-overlap recall is a guess about what the question meant.

    Scores are only ever compared **within** a tier, so no amount of word overlap
    can displace a deterministic match. A candidate found by several
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


#: What each tier means to a client reading the number.
#:
#: Both of these enums serialize as **bare integers** --- they are
#: :class:`enum.IntEnum`, so ``tier`` is ``0`` on the wire and not
#: ``"deterministic"`` --- and an integer with no published key is a value a
#: caller can order but cannot interpret. The prose above says what the ordering
#: is *for*; these say which number is which, which is the half a machine needs.
#:
#: Kept as a mapping keyed by the member rather than written into the docstring
#: by hand, so the numbers a client reads are the numbers the enum actually has.
#: :func:`value_legend` derives the published sentence from ``member.value``, and
#: a test fails if a member is missing, extra, or renumbered.
TIER_MEANINGS: dict[EvidenceTier, str] = {
    EvidenceTier.DETERMINISTIC: (
        "a fact about the code, established by exact lookup --- a path, a symbol "
        "or entity name, literal content, or a parsed relationship"
    ),
    EvidenceTier.ASSOCIATIVE: (
        "recall rather than an exact match --- word overlap from the full-text "
        "index --- which is a guess about what the question meant and never "
        "outranks a deterministic result"
    ),
}

#: What each match class means to a client reading the number.
#:
#: Read off the member documentation above, which is the contract the retrieval
#: stages implement. A match class says *how the query named this candidate*; it
#: is not a score, a confidence, or a claim that the statement is true of the
#: lines. See :class:`MatchClass` for why it is separate from the tier.
MATCH_MEANINGS: dict[MatchClass, str] = {
    MatchClass.EXACT_PATH: "the query spelled this file's whole repository-relative path",
    MatchClass.EXACT_SUBPATH: (
        "a trailing sub-path matched on segment boundaries, which may name several files"
    ),
    MatchClass.EXACT_BASENAME: "the file's basename, which says what it is called and not where",
    MatchClass.EXACT_NAME: "an entity whose name, qualified name or key the query spelled exactly",
    MatchClass.EXACT_CONTENT: "the query's literal text occurs in this span, verified by substring",
    MatchClass.RELATED: "reached by a graph edge from something the query named",
    MatchClass.LEXICAL: (
        "word-overlap recall, from the full-text index or from an entity name the "
        "query did not spell exactly"
    ),
}


Member = TypeVar("Member", bound=enum.IntEnum)


def value_legend(meanings: Mapping[Member, str]) -> str:
    """The published number-to-name key, built from the members themselves.

    The point of generating it is that there is then no second place holding the
    numbers. A renumbered member changes this sentence automatically; a member
    added without a meaning raises here rather than shipping an integer with no
    published interpretation.
    """
    return "; ".join(
        f"{member.value} = {member.name.lower()} ({meanings[member]})"
        for member in sorted(meanings)
    )


# Appended rather than written inline, for the reason above: Pydantic publishes a
# model's enum docstring as the JSON Schema ``description`` for that ``$def``, so
# this one generated sentence is what reaches every client that inspects the
# contract, whichever way it came to be looking at it.
EvidenceTier.__doc__ = (
    f"{inspect.cleandoc(EvidenceTier.__doc__ or '')}\n\n"
    f"Serialized as an integer. Values, strongest first: {value_legend(TIER_MEANINGS)}."
)
MatchClass.__doc__ = (
    f"{inspect.cleandoc(MatchClass.__doc__ or '')}\n\n"
    f"Serialized as an integer. Values, most specific first: {value_legend(MATCH_MEANINGS)}."
)

#: The two fields, annotated once so the description cannot drift between the four
#: models that carry them. The numeric key deliberately lives in the enum
#: description instead of being repeated here.
TierField = Annotated[
    EvidenceTier,
    Field(
        description=(
            "Precedence class: whether this is a fact about the code or "
            "word-overlap recall. Lower wins unconditionally, and scores are only "
            "compared within a tier. Distinct from `match`. See the EvidenceTier "
            "definition for the meaning of each value."
        )
    ),
]
MatchField = Annotated[
    MatchClass,
    Field(
        description=(
            "How precisely the query named this candidate, from a whole path down "
            "to word-overlap recall. Lower is more specific. Distinct from "
            "`tier`: two candidates can share a tier and still have been asked "
            "for in different ways. See the MatchClass definition for the meaning "
            "of each value."
        )
    ),
]


#: Entity kinds whose whole claim is a file rather than a declaration inside one:
#: the file, the module that is that file, a script, a template. The indexer stores
#: no signature for any of them because there is no declaration to store, so
#: supplying one names the file and nothing in it — which is exactly what
#: file-level provenance is for.
_FILE_IDENTITY_KINDS = frozenset(
    {EntityType.FILE, EntityType.MODULE, EntityType.SCRIPT, EntityType.TEMPLATE}
)


class SourcePreview(BaseModel):
    """Original indexed span and the closed line intervals this preview omits."""

    model_config = ConfigDict(frozen=True)
    source_span: tuple[int, int]
    omitted_spans: tuple[tuple[int, int], ...] = ()


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
    #: Exact qualified symbol explicitly named by the query; internal ranking intent.
    qualified_match: bool = Field(default=False, exclude=True)
    preview: SourcePreview | None = Field(default=None, exclude=True)
    source: EvidenceSource = EvidenceSource.EXACT
    tier: TierField = EvidenceTier.DETERMINISTIC
    #: How precisely the query identified this candidate. Retained through fusion
    #: and deduplication — a span merged from several stages keeps the strongest
    #: class any of them claimed, exactly as it keeps its strongest tier.
    match: MatchField = MatchClass.EXACT_CONTENT
    description: str = ""
    entity_id: int | None = None
    #: How many pieces the source span this text came from was cut into, or
    #: ``None`` when this candidate did not come from a stored chunk at all.
    #:
    #: Three states, because there are three honest answers and collapsing any
    #: two of them licenses a false citation:
    #:
    #: * ``1`` --- a whole chunk. Its text covers the span it carries, so a line
    #:   claim is provable;
    #: * ``> 1`` --- one piece of a span the character cap cut
    #:   (ARCHITECTURE.md §6). Every piece carries the *span's* line range, so a
    #:   piece other than a whole one begins or ends mid-line and covers fewer
    #:   lines than it advertises;
    #: * ``None`` --- no chunk was involved. An entity signature, a filename
    #:   match, a ledger reference. Nothing was cut because nothing was chunked,
    #:   and saying ``1`` here would assert a completeness no chunker vouched
    #:   for.
    #:
    #: Excluded from serialization: this is provenance the shaper needs, not a
    #: field any caller reads, and V1's ``locate --json`` predates it.
    #:
    #: ``split_index`` is deliberately *not* carried. Knowing which piece this is
    #: could in principle license a narrower claim for the head piece, but that
    #: proof depends on exactly where the cap fell and is not established here —
    #: and an unprovable line claim is the defect this field exists to prevent.
    split_total: int | None = Field(default=None, exclude=True)
    #: The type of the entity this candidate was reached through, when a stage
    #: knows it. Only the graph stage needs to: it describes whatever an edge
    #: reached, and a file reached that way is a claim about the file, while a
    #: symbol reached that way names lines it may hold no text for.
    #:
    #: Excluded from serialization for the reason ``split_total`` is: provenance
    #: the capability layer reads, not a field V1's ``locate --json`` publishes.
    entity_type: EntityType | None = Field(default=None, exclude=True)
    #: Whether ``text`` is the declaration the indexer recorded for the entity —
    #: ``def load_config()``, ``class ServiceConfig``, a configuration key's dotted
    #: path — rather than bytes read from the file.
    #:
    #: The two look alike and are not interchangeable. A declaration is assembled
    #: from facts about the source; it need not appear anywhere in the file, even
    #: for an entity whose whole span is one line. The line arithmetic that proves
    #: what a chunk covers cannot see the difference — ``service.host`` is one line
    #: of text and ``pillar.sls:2`` is one line of source — so this travels as a
    #: stated fact rather than being inferred from the shape of the text, and no
    #: line claim is ever offered on its behalf.
    #:
    #: Excluded from serialization for the reason ``split_total`` is: provenance
    #: the capability layer reads, not a field V1's ``locate --json`` publishes.
    declaration: bool = Field(default=False, exclude=True)

    @property
    def is_handle_only(self) -> bool:
        """True when this names an entity and supplies nothing to cite about it.

        An entity reached with no stored text — an exact-name symbol hit, a graph
        neighbour with no signature — carries the entity's line range and not one
        byte of it. What it establishes is an identity, which is what a handle is
        for; admitted as evidence it would receive a citable id that any claim at
        all could rest on, because there is nothing in it to check a claim against.

        File identity is the exception, and a designed one (ARCHITECTURE.md §10,
        *File-level provenance is representable*): a filename match, or a file an
        edge reached, supports exactly "this file exists", and grounding already
        refuses to let a line range rest on it. A module, a script and a template
        belong with them rather than with declarations — each one *is* its file,
        which is why no signature is stored for any of them — and a text-less
        candidate that names no entity at all claims only its file too.
        """
        return (
            not self.text
            and self.entity_id is not None
            and self.source is not EvidenceSource.FILE
            and self.entity_type not in _FILE_IDENTITY_KINDS
        )

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
