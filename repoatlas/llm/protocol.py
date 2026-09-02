"""The reasoning-model interface (ARCHITECTURE.md §4, §11).

Everything above this layer depends on the Protocol, never on a backend, so
Ollama can be replaced by vLLM, llama.cpp or any OpenAI-compatible local server
without touching a caller (V1_SPEC §3).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

Role = Literal["system", "user", "assistant"]


class Message(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: Role
    content: str


class ToolSpec(BaseModel):
    """Declared for interface completeness; V1 issues no tool calls."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    parameters: dict[str, Any]


class ModelResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    content: str
    model: str = ""
    finish_reason: str | None = None


class ModelUnavailable(Exception):
    """The backend cannot serve a request. Surfaces as exit code 4."""

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


@runtime_checkable
class ReasoningModel(Protocol):
    """Reasons over evidence. It is never a source of repository facts."""

    @property
    def id(self) -> str:
        """Identifier reported in diagnostics.

        Read-only, so a backend may compute it (Ollama includes the model name)
        or declare it as a plain class attribute.
        """
        ...

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None = None,
        response_schema: type[BaseModel] | None = None,
    ) -> ModelResponse: ...

    async def available(self) -> bool:
        """Whether a request would be served, checked before use."""
        ...

    def availability_detail(self) -> str:
        """Human-readable reason the backend is unusable, for the exit-4 message."""
        ...
