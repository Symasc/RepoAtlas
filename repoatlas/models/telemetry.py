"""Instrumentation models (V1_SPEC §20).

RepoAtlas must be able to answer "which stage cost the time, and how much did it
narrow the candidate set" without anyone changing the architecture to find out.
These models are what the retrieval funnel and the global link pass fill in.

They are **observations, never evidence**. A trace is attached to a result for
reporting under ``--verbose`` and ``--json``; nothing reads a trace to make a
decision, so instrumentation can never change an answer.
"""

from __future__ import annotations

import time
from types import TracebackType

from pydantic import BaseModel, ConfigDict


class StageTiming(BaseModel):
    """One stage of a pipeline: what it cost and how it changed the set."""

    model_config = ConfigDict(frozen=True)

    name: str
    duration_ms: float
    candidates_in: int = 0
    candidates_out: int = 0
    detail: str = ""


class RetrievalTrace(BaseModel):
    """Per-stage instrumentation for one retrieval (ARCHITECTURE.md §10)."""

    model_config = ConfigDict(frozen=True)

    stages: tuple[StageTiming, ...] = ()
    literal_terms: tuple[str, ...] = ()
    #: How many repositories the scoped pass covered. For a confined run — every
    #: capability search — that is the whole search. The V1 CLI also makes a second,
    #: unscoped pass that this number says nothing about, so there it is "how narrow
    #: the first pass was" rather than "how much was searched".
    scoped_repositories: int = 0
    graph_hops: int = 0
    graph_nodes_visited: int = 0
    deterministic_candidates: int = 0
    associative_candidates: int = 0
    #: ``repository/path`` for each file the query identified by its whole
    #: relative path. These are the candidates that take precedence over every
    #: less specific match, so a review of *why* something ranked where it did
    #: starts here (ARCHITECTURE.md §10).
    named_paths: tuple[str, ...] = ()
    #: ``repository/path`` for each file the query identified by a *component
    #: suffix* of its relative path. Separate from :attr:`named_paths` because the
    #: two are different claims: a whole path names one file, a suffix names every
    #: file ending on that boundary and asserts uniqueness for none of them. Both
    #: take precedence over less specific matches, and a review of *why* something
    #: ranked where it did needs to be able to tell which of the two happened
    #: (ARCHITECTURE.md §10).
    suffix_paths: tuple[str, ...] = ()
    token_estimate: int = 0

    @property
    def total_ms(self) -> float:
        return round(sum(stage.duration_ms for stage in self.stages), 3)


class UsesWork(BaseModel):
    """What one ``USES`` stage resolved again, and why (repoatlas.graph.uses_link).

    Counts, not times: which files a change reached and how much work that cost are
    deterministic, so they are what a test can hold a sync to.
    """

    model_config = ConfigDict(frozen=True)

    #: ``incremental``, or ``global`` when every use was resolved again.
    mode: str = "global"
    #: Why a pass was global; ``None`` for an incremental one.
    reason: str | None = None
    #: Files whose uses were resolved again, and how many were reached each way: the
    #: file itself changed, a use of it landed in a changed file, it read a changed
    #: export table, its repository's resolution changed, or a package it may link did.
    files_invalidated: int = 0
    own: int = 0
    landed: int = 0
    readers: int = 0
    repository: int = 0
    package: int = 0
    repositories_invalidated: int = 0
    packages_invalidated: int = 0
    claims_examined: int = 0
    claims_resolved: int = 0
    edges_deleted: int = 0
    edges_inserted: int = 0
    #: Export tables recorded as read, for the next pass to invalidate by.
    reads_written: int = 0
    repositories_touched: int = 0


class LinkTrace(BaseModel):
    """Instrumentation for the global link pass (ARCHITECTURE.md §5, §6)."""

    model_config = ConfigDict(frozen=True)

    duration_ms: float = 0.0
    claims_considered: int = 0
    files_indexed: int = 0
    symbols_indexed: int = 0
    stages: tuple[StageTiming, ...] = ()
    uses: UsesWork | None = None


class Stopwatch:
    """Context manager that records one :class:`StageTiming`.

    Lives beside the model it produces because the two are one idea; keeping the
    recorder in a separate package would mean two imports for every measured
    stage and a layering exception for the renderer.
    """

    __slots__ = ("_start", "candidates_in", "candidates_out", "detail", "name", "recorded")

    def __init__(self, name: str, *, candidates_in: int = 0) -> None:
        self.name = name
        self.candidates_in = candidates_in
        self.candidates_out = 0
        self.detail = ""
        self.recorded: StageTiming | None = None
        self._start = 0.0

    def __enter__(self) -> Stopwatch:
        self._start = time.perf_counter()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        elapsed = (time.perf_counter() - self._start) * 1000.0
        self.recorded = StageTiming(
            name=self.name,
            duration_ms=round(elapsed, 3),
            candidates_in=self.candidates_in,
            candidates_out=self.candidates_out,
            detail=self.detail,
        )
