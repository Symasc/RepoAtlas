"""The reasoning-model interface (ARCHITECTURE.md §4, §11).

Everything above this layer depends on the Protocol, never on a backend, so
Ollama can be replaced by vLLM, llama.cpp or any OpenAI-compatible local server
without touching a caller (V1_SPEC §3).
"""

from __future__ import annotations

import enum
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
    #: The backend's own word for why generation stopped, verbatim.
    finish_reason: str | None = None
    #: Whether the **server reported** that generation stopped at its output
    #: limit. Set by the backend, which knows its own protocol's spelling, so
    #: nothing above this layer has to recognise a provider-specific string.
    #: Never inferred from the text: an incomplete-looking answer is not proof
    #: that a limit was reached.
    truncated: bool = False


class ModelFailure(enum.StrEnum):
    """Where a request died. Backend-agnostic on purpose (ARCHITECTURE.md §11).

    The categories are *layers*, ordered by how far a request got:

    ``CONFIGURATION``
        RepoAtlas's own settings are wrong. Nothing was attempted.
    ``TRANSPORT``
        No usable connection to the server. Nothing was received.
    ``SERVER``
        A connection was established; the server failed or never finished.
    ``MODEL_ABSENT``
        The server answered; the configured model is not installed on it.
    ``PROTOCOL``
        The server answered; the response is not the API contract.
    ``OUTPUT``
        The API contract held; the model's own text is unusable.
    ``TRUNCATED``
        The server reported that generation stopped at its output limit.
    ``REPAIR``
        The output was invalid and the one repair attempt was invalid too.
    """

    CONFIGURATION = "configuration"
    TRANSPORT = "transport"
    SERVER = "server"
    MODEL_ABSENT = "model_absent"
    PROTOCOL = "protocol"
    OUTPUT = "output"
    TRUNCATED = "truncated"
    REPAIR = "repair"


class ModelError(Exception):
    """A reasoning backend could not produce an answer. Surfaces as exit code 4.

    The invariant this hierarchy exists to hold:

    > Receiving an invalid model response is not equivalent to failing to reach
    > the model server.

    and its operational form:

    > An error message describes the **deepest layer successfully reached**
    > before the failure.

    So a connection that was never established is a transport failure; an HTTP
    response that arrived is never described as a connection failure; and a
    well-formed API envelope carrying unusable model text is never described as
    an API-protocol failure. Callers that only need "the backend failed" catch
    this class; callers that must tell a dead server from a bad answer catch the
    subclass or read :attr:`category`.

    The structured fields exist so a caller can act without parsing prose, and
    deliberately carry no response body: model output is derived from repository
    content and does not belong in an error message (V1_SPEC §17).
    """

    category: ModelFailure = ModelFailure.TRANSPORT
    #: Whether repeating the identical request could plausibly succeed.
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        detail: str | None = None,
        backend: str = "",
        endpoint: str = "",
        model: str = "",
        http_status: int | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.backend = backend
        self.endpoint = endpoint
        self.model = model
        self.http_status = http_status
        if retryable is not None:
            self.retryable = retryable


class ModelConfigurationError(ModelError):
    """RepoAtlas is misconfigured; no request was attempted."""

    category = ModelFailure.CONFIGURATION


class ModelUnavailable(ModelError):
    """No connection to the server. The only failure that may say "could not reach"."""

    category = ModelFailure.TRANSPORT
    retryable = True


class ModelServerError(ModelError):
    """The server was reached and failed, or accepted the request and never finished."""

    category = ModelFailure.SERVER


class ModelAbsent(ModelError):
    """The server is running; the configured model is not installed on it."""

    category = ModelFailure.MODEL_ABSENT


class ModelProtocolError(ModelError):
    """The server responded, but not with the API contract this backend expects."""

    category = ModelFailure.PROTOCOL


class ModelOutputError(ModelError):
    """The API contract held; the model's generated text is unusable."""

    category = ModelFailure.OUTPUT


class ModelOutputTruncated(ModelOutputError):
    """Generation stopped at its output limit, so the text is incomplete.

    Distinct from :class:`ModelOutputError` because the remedy differs: a raised
    output limit fixes this and cannot fix a model that emitted well-formed
    nonsense. Only raised when the *server itself* reported the limit — never
    inferred from the text alone (V1_SPEC §15).
    """

    category = ModelFailure.TRUNCATED
    retryable = True


class ModelRepairFailed(ModelOutputError):
    """The output was invalid, and the one repair attempt was invalid too."""

    category = ModelFailure.REPAIR


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

    def availability_category(self) -> ModelFailure:
        """Which layer the availability probe failed at.

        Optional for a backend to override; the default keeps every existing
        implementation valid. It exists so "the server is down" and "the server
        is up but the model is not installed" are distinguishable in code and
        not only in prose.
        """
        return ModelFailure.TRANSPORT
