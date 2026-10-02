"""Wire models for the ``inspect`` capability (RepoAtlas V2).

Progressive disclosure over trusted indexed evidence. **Not a filesystem reader.**

That distinction is built into these types rather than left to the code that
fills them in, because the two are indistinguishable from the outside once bytes
have been returned. What makes this inspection rather than reading is that every
target names something the index already admitted: an evidence id supplied
earlier in this investigation, a repository-relative path that has a ``files``
row, or an exact natural entity key. A path with no row is not a file RepoAtlas
declines to read — it is a file RepoAtlas has nothing to say about, and it gets
the same answer whether it is a secret, an exclusion, an unsynced addition or
nothing at all.

Three facts a caller must be able to tell apart
-----------------------------------------------
:class:`Coverage` refuses to collapse them, because an agent's next move differs
for each:

* **returned** — lines whose complete text is in this response;
* **unavailable** — requested lines RepoAtlas cannot prove from the source it
  used. Asking again changes nothing; a sync might;
* **budget_omitted** — lines RepoAtlas could have supplied and did not, because
  of the budget. Asking again with a larger budget, or a narrower range, returns
  them.

Folding the last two into one ``missing`` list would tell an agent that widening
its budget might recover a chunk gap, and that re-syncing might recover lines that
were merely too many.

One source per inspection
-------------------------
:class:`InspectionSource` says where the returned text came from, and it is
singular on purpose. Mixing verified working-tree lines with indexed lines from
before the last edit would hand back a file state that never existed on disk or in
the index, and every line of it would look equally trustworthy.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from repoatlas.compat import StrEnum
from repoatlas.models.capability import (
    PRESENTATION_DESCRIPTION,
    AppliedBudget,
    CapabilityContext,
    EvidenceBudget,
    Presentation,
)
from repoatlas.models.capability_io import ContextView, EvidenceItem, LineRange
from repoatlas.models.freshness import FreshnessDepth, IndexFreshness
from repoatlas.models.investigation import EvidenceId, EvidenceStatus, LedgerSummary

#: Lines of surrounding context an inspection adds by default on each side of a
#: derived range. Enough to read a function's signature and its first statements
#: without a second call; small enough that the default is not a file dump.
DEFAULT_CONTEXT_LINES = 20

#: The most context the server will add on each side, whatever is asked. A bound
#: rather than a policy: context is multiplied by two sides and by every gap in a
#: range, so an unbounded value turns a targeted inspection into a whole-file read
#: by arithmetic rather than by request.
MAX_CONTEXT_LINES = 200


class InspectTargetKind(StrEnum):
    """Which of the three things an inspection was asked about."""

    #: Evidence supplied earlier in this same investigation.
    EVIDENCE = "evidence"
    #: A repository-relative path with a ``files`` row.
    PATH = "path"
    #: An exact natural entity key.
    ENTITY = "entity"


class InspectTarget(BaseModel):
    """What to inspect. Exactly one form.

    Three fields rather than a tagged union because each one is a different kind
    of identifier with different resolution rules, and a single ``target: str``
    would have to guess which was meant — a guess that resolves ``E7`` as a path
    the moment a repository contains a file called ``E7``.

    Supplying two is refused rather than ordered by precedence. A caller that sent
    both a path and an entity key does not know what it is asking, and picking one
    would answer a question nobody posed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: An id from **this** investigation. Meaningless in any other, and never
    #: searched for across investigations.
    evidence_id: EvidenceId | None = None
    #: Repository-relative, forward-slashed, exactly as the index stores it.
    path: str | None = None
    #: Exact natural key, e.g. ``sym:svc:src/config.py:load_config:18``. Never a
    #: name, and never resolved fuzzily.
    entity_key: str | None = None

    @model_validator(mode="after")
    def _exactly_one_target(self) -> InspectTarget:
        supplied = [
            name
            for name, value in (
                ("evidence_id", self.evidence_id),
                ("path", self.path),
                ("entity_key", self.entity_key),
            )
            if value is not None
        ]
        if len(supplied) != 1:
            raise ValueError(
                "an inspection names exactly one of evidence_id, path or entity_key; "
                f"got {supplied or ['none']}"
            )
        if self.path is not None:
            validate_path_shape(self.path)
        if self.entity_key is not None and not self.entity_key.strip():
            raise ValueError("entity_key must not be blank")
        return self

    @property
    def kind(self) -> InspectTargetKind:
        if self.evidence_id is not None:
            return InspectTargetKind.EVIDENCE
        if self.path is not None:
            return InspectTargetKind.PATH
        return InspectTargetKind.ENTITY


def validate_path_shape(path: str) -> None:
    """Refuse anything that is not a repository-relative path.

    A first gate, not the gate. The one that matters is the ``files`` row lookup,
    which no host path can satisfy because ``files.rel_path`` never holds one — so
    a traversal attempt that reached the service anyway would resolve to nothing
    and be refused as not indexed. This check exists so a malformed request is
    reported as malformed instead of being quietly translated into a lookup miss,
    and so the refusal happens before anything touches a repository.
    """
    if not path.strip():
        raise ValueError("path must not be blank")
    if path.startswith("/") or path.startswith("\\"):
        raise ValueError("path must be repository-relative, not absolute")
    if len(path) > 1 and path[1] == ":":
        raise ValueError("path must be repository-relative, not absolute")
    parts = path.replace("\\", "/").split("/")
    if any(part == ".." for part in parts):
        raise ValueError("path must not escape the repository with '..'")
    if "\x00" in path:
        raise ValueError("path must not contain a null byte")


class InspectionSource(StrEnum):
    """Where the text in this response came from. Exactly one per inspection."""

    #: The working tree, read once through the containment boundary and proven
    #: byte-identical to the indexed file's content hash. Not "the tree looked
    #: clean" — the bytes were hashed.
    VERIFIED_WORKTREE = "verified_worktree"
    #: The indexed chunks. What RepoAtlas searched, which is not necessarily what
    #: is on disk now.
    INDEXED_CHUNKS = "indexed_chunks"


class Coverage(BaseModel):
    """What was asked for, what came back, and why the rest did not."""

    model_config = ConfigDict(frozen=True)

    #: The source lines this inspection set out to supply, after the target's own
    #: range and any context lines. ``None`` when no range could be established at
    #: all — a file with no provable textual extent and no explicit request.
    requested: LineRange | None = None
    #: Lines whose complete text is in ``segments``. Disjoint, ascending.
    returned: tuple[LineRange, ...] = ()
    #: Requested lines RepoAtlas cannot prove from :attr:`source`. A gap between
    #: indexed chunks, or a range beyond what the chunks establish.
    unavailable: tuple[LineRange, ...] = ()
    #: Requested lines RepoAtlas could have supplied but did not, because the
    #: budget ran out. A different remedy from :attr:`unavailable`.
    budget_omitted: tuple[LineRange, ...] = ()
    source: InspectionSource
    #: Whether the working tree's bytes hashed equal to the indexed content hash.
    #: ``None`` means the comparison was not made — the caller declined a
    #: working-tree read, or the file could not be read at all. ``False`` carries
    #: no information about what the working tree holds, deliberately: mismatching
    #: bytes are never returned, previewed, counted or described.
    worktree_matches_index: bool | None = None


class InspectedFile(BaseModel):
    """The indexed file an inspection read, by identity rather than by row.

    Every value here is a fact the index already holds about the file. There is no
    host path: the absolute location of a repository on this machine is not
    something a caller needs in order to cite a line, and it is exactly what a
    containment boundary exists to keep from leaking.
    """

    model_config = ConfigDict(frozen=True)

    repository_slug: str
    path: str
    #: Bytes, as indexed.
    size: int
    #: ``files.content_hash``. The same value an ``EvidenceRef`` pins, so a caller
    #: can tell that two inspections read the same snapshot.
    indexed_content_hash: str
    indexed_facts_version: str
    #: The file's true length in source lines, and ``None`` whenever that is not
    #: provable. A verified working-tree read establishes it exactly; a set of
    #: indexed chunks does not, because chunks with no text are not stored and the
    #: last line of the last chunk is not the last line of the file.
    line_count: int | None = None


class InspectRequest(BaseModel):
    """Read source RepoAtlas already indexed, and record what it hands over."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Explicit, as everywhere in this layer. A capability server has no working
    #: directory worth consulting.
    context: CapabilityContext
    target: InspectTarget
    #: Overrides the range the target would imply, while the target still chooses
    #: the file. Omit ``line_end`` to name a single line.
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    #: Lines added on each side of the derived range, before clamping.
    context_lines: int = Field(
        default=DEFAULT_CONTEXT_LINES,
        ge=0,
        le=MAX_CONTEXT_LINES,
        description="Extra lines on each side of the requested range or entity. "
        "Defaults to 20; set 0 for precisely the requested lines.",
    )
    budget: EvidenceBudget = EvidenceBudget()
    #: Whether RepoAtlas may read the working tree at all. Even when true, live
    #: bytes are used only if they hash equal to the indexed content hash; this
    #: switch turns the attempt off entirely, which is what a caller wants when it
    #: is reasoning about what was *indexed* rather than what is there now.
    allow_worktree: bool = True
    #: An id RepoAtlas issued earlier. **Required** for an evidence target, since
    #: an evidence id has no meaning outside the investigation that minted it.
    investigation_id: str | None = None
    depth: FreshnessDepth = FreshnessDepth.STORED
    #: How the response is written out. The capability runs identically either way.
    presentation: Presentation = Field(
        default=Presentation.FULL, description=PRESENTATION_DESCRIPTION
    )

    @model_validator(mode="after")
    def _range_is_coherent(self) -> InspectRequest:
        if self.line_end is not None and self.line_start is None:
            raise ValueError("line_end without line_start does not name a range")
        if (
            self.line_start is not None
            and self.line_end is not None
            and self.line_end < self.line_start
        ):
            raise ValueError(f"line range ends before it starts: {self.line_start}-{self.line_end}")
        return self

    @property
    def explicit_range(self) -> LineRange | None:
        """The caller's own range, with a lone ``line_start`` meaning one line."""
        if self.line_start is None:
            return None
        return LineRange(start=self.line_start, end=self.line_end or self.line_start)


class InspectResponse(BaseModel):
    """Source text for one target, recorded in one investigation."""

    model_config = ConfigDict(frozen=True)

    investigation_id: str
    context: ContextView
    #: Echoed so a response is self-describing when it arrives without its request.
    target: InspectTarget
    #: For an evidence target, that evidence's status **after** this call
    #: revalidated it. ``None`` for every other target.
    #:
    #: Deliberately separate from the status of the evidence in ``segments``. An
    #: inspection widened from a ``STALE`` id returns current text under new ids;
    #: reporting one status for both would either claim the old id is current or
    #: claim the new text is stale.
    target_evidence_status: EvidenceStatus | None = None
    file: InspectedFile
    #: In ascending source order — the only order inspection has. ``rank`` is that
    #: ordinal and nothing more: there is no relevance here to rank by, because
    #: nothing was searched for.
    segments: tuple[EvidenceItem, ...] = ()
    coverage: Coverage
    freshness: IndexFreshness
    budget: AppliedBudget
    ledger: LedgerSummary
