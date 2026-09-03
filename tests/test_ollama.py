"""Ollama backend HTTP contract (V1_SPEC §3).

Exercised through a mock transport: no server is required, and the request shape
is asserted rather than assumed.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from repoatlas.config.settings import LLMSettings
from repoatlas.llm.ollama import OllamaBackend
from repoatlas.llm.protocol import Message, ModelUnavailable
from repoatlas.models.evidence import RepoAtlasAnswer


def _backend(handler, **settings: object) -> OllamaBackend:
    return OllamaBackend(
        LLMSettings.model_validate({"model": "gpt-oss:20b", **settings}),
        transport=httpx.MockTransport(handler),
    )


def _tags(*names: str) -> httpx.Response:
    return httpx.Response(200, json={"models": [{"name": n} for n in names]})


def test_available_when_server_and_model_are_present() -> None:
    backend = _backend(lambda request: _tags("gpt-oss:20b", "qwen3:8b"))
    assert asyncio.run(backend.available()) is True


def test_available_tolerates_an_implicit_latest_tag() -> None:
    backend = _backend(lambda request: _tags("gpt-oss:20b-latest"))
    assert asyncio.run(backend.available()) is True


def test_unavailable_when_the_server_is_unreachable() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    backend = _backend(refuse)
    assert asyncio.run(backend.available()) is False
    detail = backend.availability_detail()
    assert "No Ollama server responded" in detail
    assert "localhost:11434" in detail


def test_unavailable_when_the_model_is_not_pulled() -> None:
    """A running server with the model missing would fail at request time."""
    backend = _backend(lambda request: _tags("llama3:8b"))
    assert asyncio.run(backend.available()) is False
    detail = backend.availability_detail()
    assert "ollama pull gpt-oss:20b" in detail
    assert "llama3:8b" in detail


def test_complete_posts_the_expected_request() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"message": {"role": "assistant", "content": '{"summary":"s"}'}}
        )

    backend = _backend(handler)
    response = asyncio.run(
        backend.complete([Message(role="user", content="hello")], response_schema=RepoAtlasAnswer)
    )

    assert seen["url"] == "http://localhost:11434/api/chat"
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["model"] == "gpt-oss:20b"
    assert body["stream"] is False
    assert body["options"]["temperature"] == 0.0
    assert body["messages"] == [{"role": "user", "content": "hello"}]
    # Constrained decoding: the answer schema is sent, not just requested in prose.
    assert body["format"]["properties"].keys() >= {"summary", "hypotheses", "unknowns"}
    assert response.content == '{"summary":"s"}'


def test_complete_honors_a_configured_base_url() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"message": {"content": "{}"}})

    backend = _backend(handler, base_url="http://gpu-box:11434/")
    asyncio.run(backend.complete([Message(role="user", content="x")]))
    assert seen["url"] == "http://gpu-box:11434/api/chat"


def test_connection_failure_raises_model_unavailable() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    backend = _backend(refuse)
    with pytest.raises(ModelUnavailable) as excinfo:
        asyncio.run(backend.complete([Message(role="user", content="x")]))
    assert "Could not reach Ollama" in excinfo.value.message


def test_http_error_raises_model_unavailable() -> None:
    backend = _backend(lambda request: httpx.Response(500, text="boom"))
    with pytest.raises(ModelUnavailable) as excinfo:
        asyncio.run(backend.complete([Message(role="user", content="x")]))
    assert "HTTP 500" in excinfo.value.message
    assert "boom" in (excinfo.value.detail or "")
