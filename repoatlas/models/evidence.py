"""Evidence and answer models (ARCHITECTURE.md §5, V1_SPEC §15).

``RepoAtlasAnswer`` and its parts are the structured-output contract for Slice B.
They live here in Slice A because ``locate`` already renders evidence, and because
citation verification will validate model output against ``EvidenceSet``.
"""

from __future__ import annotations

import enum

from pydantic import BaseModel, ConfigDict, Field


class EvidenceSource(enum.StrEnum):
    """Which retriever produced a candidate. Used by rank fusion and by --verbose."""

    EXACT = "exact"
    SYMBOL = "symbol"
    SEMANTIC = "semantic"
    GRAPH = "graph"


class EvidenceCandidate(BaseModel):
    """One retrieved span of a real file, with the provenance that proves it."""

    model_config = ConfigDict(frozen=True)

    repository: str
    path: str
    line_start: int
    line_end: int
    text: str
    score: float = 0.0
    source: EvidenceSource = EvidenceSource.EXACT
    description: str = ""
    entity_id: int | None = None

    @property
    def location(self) -> str:
        """``repo/path:line`` or ``repo/path:start-end`` (V1_SPEC §11)."""
        if self.line_start == self.line_end:
            return f"{self.repository}/{self.path}:{self.line_start}"
        return f"{self.repository}/{self.path}:{self.line_start}-{self.line_end}"


class EvidenceSet(BaseModel):
    """A bounded set of candidates. Never a whole repository (V1_SPEC §14)."""

    model_config = ConfigDict(frozen=True)

    question: str
    candidates: tuple[EvidenceCandidate, ...] = ()
    scope: str = ""
    primary_repository: str | None = None
    truncated: bool = False


class Evidence(BaseModel):
    """A citation in a model answer. Verified against the supplied EvidenceSet."""

    repository: str
    path: str
    line_start: int | None = None
    line_end: int | None = None
    description: str


class Finding(BaseModel):
    statement: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[Evidence]


class RepoAtlasAnswer(BaseModel):
    """The validated structured answer (V1_SPEC §15)."""

    summary: str
    findings: list[Finding] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    next_check: str | None = None
