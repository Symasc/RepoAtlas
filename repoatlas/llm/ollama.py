"""Ollama backend (ARCHITECTURE.md §11, V1_SPEC §3).

The V1 default, but only a default: selection is configuration-driven and this
module is reachable only through :class:`~repoatlas.llm.protocol.ReasoningModel`.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import httpx
from pydantic import BaseModel

from repoatlas.config.settings import LLMSettings
from repoatlas.llm.protocol import Message, ModelResponse, ModelUnavailable, ToolSpec

#: Availability probes must fail fast; a query should not hang on a dead server.
_PROBE_TIMEOUT_SECONDS = 3.0


class OllamaBackend:
    """Talks to a local Ollama server over ``/api/chat``."""

    def __init__(
        self, settings: LLMSettings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._base_url = settings.base_url.rstrip("/")
        self._detail = ""
        # Injectable so the HTTP contract can be tested without a live server.
        self._transport = transport

    @property
    def id(self) -> str:
        return f"ollama/{self._settings.model}"

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None = None,
        response_schema: type[BaseModel] | None = None,
    ) -> ModelResponse:
        payload: dict[str, Any] = {
            "model": self._settings.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
            # Deterministic decoding: the same evidence should reason the same way.
            "options": {"temperature": 0.0},
        }
        if response_schema is not None:
            # Constrained decoding, so the model emits the answer schema directly
            # rather than prose we would have to salvage.
            payload["format"] = response_schema.model_json_schema()
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]

        try:
            async with self._client(self._settings.timeout_seconds) as client:
                response = await client.post(f"{self._base_url}/api/chat", json=payload)
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPStatusError as exc:
            raise ModelUnavailable(
                f"Ollama returned HTTP {exc.response.status_code}.",
                detail=exc.response.text[:500],
            ) from exc
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise ModelUnavailable(
                f"Could not reach Ollama at {self._base_url}.", detail=str(exc)
            ) from exc

        message = body.get("message") or {}
        return ModelResponse(
            content=message.get("content", ""),
            model=body.get("model", self._settings.model),
            finish_reason=body.get("done_reason"),
        )

    async def available(self) -> bool:
        """Check the server is reachable *and* the configured model is present.

        A reachable server with the model unpulled would fail at request time, so
        both are part of availability.
        """
        try:
            async with self._client(_PROBE_TIMEOUT_SECONDS) as client:
                response = await client.get(f"{self._base_url}/api/tags")
                response.raise_for_status()
                body = response.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            self._detail = (
                f"No Ollama server responded at {self._base_url} ({exc.__class__.__name__}).\n\n"
                "Start one, or configure a different provider:\n\n"
                "  llm:\n"
                "    provider: ollama\n"
                "    base_url: http://localhost:11434"
            )
            return False

        installed = {
            str(model.get("name", ""))
            for model in body.get("models", [])
            if isinstance(model, dict)
        }
        if self._model_installed(installed):
            return True

        listing = "\n".join(f"  {name}" for name in sorted(installed)) or "  (none)"
        self._detail = (
            f"Ollama is running at {self._base_url}, but the configured model\n"
            f"{self._settings.model!r} is not installed.\n\n"
            f"Pull it:\n\n  ollama pull {self._settings.model}\n\n"
            f"Installed models:\n\n{listing}"
        )
        return False

    def _model_installed(self, installed: set[str]) -> bool:
        """Match the configured model, tolerating an implicit ``:latest`` tag."""
        wanted = self._settings.model
        candidates = {wanted, f"{wanted}:latest"}
        if ":" in wanted:
            candidates.add(wanted.split(":", 1)[0])
        return bool(candidates & installed) or any(
            name.split(":", 1)[0] == wanted.split(":", 1)[0] for name in installed
        )

    def _client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, transport=self._transport)

    def availability_detail(self) -> str:
        return self._detail or f"Ollama at {self._base_url} is unavailable."
