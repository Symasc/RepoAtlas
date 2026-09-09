"""A deterministic backend that never invents anything (ARCHITECTURE.md §11).

Used by tests and selected by ``llm.provider: none``. It performs no reasoning:
it reports insufficient evidence and lets the retrieved evidence speak for itself.
That is the honest behavior for a backend that cannot reason, and it keeps the
whole pipeline exercisable with no model installed.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel

from repoatlas.llm.protocol import Message, ModelFailure, ModelResponse, ToolSpec
from repoatlas.models.evidence import RepoAtlasAnswer

INSUFFICIENT_EVIDENCE = "INSUFFICIENT EVIDENCE"


class NullBackend:
    """Always answers ``INSUFFICIENT EVIDENCE`` with no hypotheses."""

    id = "null"

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None = None,
        response_schema: type[BaseModel] | None = None,
    ) -> ModelResponse:
        del messages, tools, response_schema
        answer = RepoAtlasAnswer(
            summary=INSUFFICIENT_EVIDENCE,
            hypotheses=[],
            unknowns=["No reasoning model is configured, so no analysis was performed."],
            next_check=None,
        )
        return ModelResponse(content=answer.model_dump_json(), model=self.id)

    async def available(self) -> bool:
        return True

    def availability_detail(self) -> str:
        return "the null backend is always available"

    def availability_category(self) -> ModelFailure:
        """Configuration, not a failure: no backend was requested."""
        return ModelFailure.CONFIGURATION
