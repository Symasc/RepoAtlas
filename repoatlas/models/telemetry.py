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

from pydantic import BaseModel, ConfigDict, Field


class StageTiming(BaseModel):
    """One stage of a pipeline: what it cost and how it changed the set."""

    model_config = ConfigDict(frozen=True)

    name: str
    duration_ms: float
    candidates_in: int = 0
    candidates_out: int = 0
    detail: str = ""

    @property
    def delta(self) -> int:
        return self.candidates_out - self.candidates_in


class RetrievalTrace(BaseModel):
    """Per-stage instrumentation for one retrieval (ARCHITECTURE.md §10)."""

    model_config = ConfigDict(frozen=True)

    stages: tuple[StageTiming, ...] = ()
    literal_terms: tuple[str, ...] = ()
    scoped_repositories: int = 0
    semantic_used: bool = False
    semantic_detail: str = ""
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


class LinkTrace(BaseModel):
    """Instrumentation for the global link pass (ARCHITECTURE.md §5, §6)."""

    model_config = ConfigDict(frozen=True)

    duration_ms: float = 0.0
    claims_considered: int = 0
    files_indexed: int = 0
    symbols_indexed: int = 0
    stages: tuple[StageTiming, ...] = ()


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


class TraceRecorder(BaseModel):
    """Mutable accumulator, frozen into a :class:`RetrievalTrace` at the end."""

    model_config = ConfigDict(frozen=False)

    stages: list[StageTiming] = Field(default_factory=list)

    def stage(self, name: str, *, candidates_in: int = 0) -> Stopwatch:
        return Stopwatch(name, candidates_in=candidates_in)

    def record(self, watch: Stopwatch) -> None:
        if watch.recorded is not None:
            self.stages.append(watch.recorded)
