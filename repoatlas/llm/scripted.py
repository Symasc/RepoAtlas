"""A test-only backend returning canned responses (ARCHITECTURE.md §11).

Lets the validation, repair and citation-verification paths be exercised
deterministically, with no network and no Ollama in CI.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel

from repoatlas.llm.protocol import Message, ModelResponse, ModelUnavailable, ToolSpec


class ScriptedBackend:
    """Replays ``responses`` in order, recording the prompts it received."""

    id = "scripted"

    def __init__(
        self,
        responses: Sequence[str],
        *,
        is_available: bool = True,
        detail: str = "scripted backend marked unavailable",
    ) -> None:
        self._responses = list(responses)
        self._is_available = is_available
        self._detail = detail
        self.calls: list[list[Message]] = []
        self.schemas: list[type[BaseModel] | None] = []

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None = None,
        response_schema: type[BaseModel] | None = None,
    ) -> ModelResponse:
        del tools
        self.calls.append(list(messages))
        self.schemas.append(response_schema)
        if not self._responses:
            raise ModelUnavailable("scripted backend exhausted")
        return ModelResponse(content=self._responses.pop(0), model=self.id)

    async def available(self) -> bool:
        return self._is_available

    def availability_detail(self) -> str:
        return self._detail

    @property
    def remaining(self) -> int:
        return len(self._responses)
