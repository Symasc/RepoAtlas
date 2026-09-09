"""Ollama backend (ARCHITECTURE.md §11, V1_SPEC §3).

The V1 default, but only a default: selection is configuration-driven and this
module is reachable only through :class:`~repoatlas.llm.protocol.ReasoningModel`.

**Every failure is classified by how far the request got.** The layers, and the
only thing each one is allowed to claim:

.. code-block:: text

    configuration   the URL/provider is unusable        nothing was attempted
        v
    transport       no connection                       "could not reach"
        v
    server          connected; HTTP error, or no end    never "could not reach"
        v
    protocol        HTTP body is not the Ollama API     never a server failure
        v
    output          envelope fine, model text unusable  never a protocol failure

Getting this wrong is not cosmetic. Telling a user "Could not reach Ollama" when
Ollama answered on the first try sends them to restart a healthy server; telling
them the model is broken when the socket is closed sends them to the wrong logs.

Response bodies are never included in an error. Ollama's documented error
envelope is ``{"error": "..."}``, so that string is extracted and bounded;
everything else is described by *shape* — a byte count, a character offset —
because a model's output is derived from repository content (V1_SPEC §17).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx
from pydantic import BaseModel

from repoatlas.config.settings import LLMSettings
from repoatlas.llm.protocol import (
    Message,
    ModelConfigurationError,
    ModelFailure,
    ModelProtocolError,
    ModelResponse,
    ModelServerError,
    ModelUnavailable,
    ToolSpec,
)

#: Availability probes must fail fast; a query should not hang on a dead server.
_PROBE_TIMEOUT_SECONDS = 3.0

#: Ollama sets ``done_reason`` to this exactly when llama-server reports
#: ``finish_reason == "length"`` — that is, when the ``num_predict`` budget was
#: exhausted. It is a *server-reported fact*, which is what lets RepoAtlas say
#: the output was truncated rather than that it merely looks incomplete.
_LENGTH_LIMIT_REASON = "length"

#: Statuses where the identical request could plausibly succeed later. A 4xx is
#: a request the server rejected on its merits; repeating it changes nothing.
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

#: Ollama's documented error field. Bounded before it is shown: it is server
#: text, not repository content, but it is still not ours to print in full.
_MAX_SERVER_ERROR_CHARS = 200


def _article(value: object) -> str:
    """``an int`` / ``a list`` — the type name only, never the value."""
    name = type(value).__name__
    return f"{'an' if name[0] in 'aeiou' else 'a'} {name}"


def _server_error_text(response: httpx.Response) -> str | None:
    """Ollama's ``{"error": "..."}`` message, if the body actually is one."""
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    reported = body.get("error")
    if not isinstance(reported, str) or not reported.strip():
        return None
    return reported.strip()[:_MAX_SERVER_ERROR_CHARS]


def _installed_names(body: dict[str, Any]) -> tuple[set[str], str | None]:
    """Model names from an ``/api/tags`` body, or why the body is not one.

    The second element is ``None`` only when the response is a *structurally
    valid* model listing. That distinction is the whole point of this function:
    "the configured model is not installed" is a conclusion about the server's
    state, and it may only be drawn from a listing RepoAtlas actually
    understood. Every shape it did not understand is a protocol failure, because
    the honest thing to say about ``{"models": "x"}`` is that the answer was
    unreadable — not that the model is missing from it.

    The previous version reached the opposite conclusion by being helpful in
    three places at once::

        listed = body.get("models")                     # missing -> None
        {str(m.get("name", ""))                         # absent name -> ""
         for m in (listed if isinstance(listed, list) else [])   # wrong type -> []
         if isinstance(m, dict)}                        # bad entry -> skipped

    Each fallback is individually defensible and their sum is an empty set,
    which is indistinguishable from a server with no models pulled. So a proxy
    login page, a truncated body that still parsed, and a future Ollama that
    renamed the field all arrived at the same advice: ``ollama pull <model>``.

    **Strict about what is consumed, tolerant about everything else.** The only
    field RepoAtlas reads is each entry's ``name``, so that is all that is
    required to be well-formed; ``model``, ``size``, ``digest``, ``details``,
    ``modified_at`` and any field a later server adds are ignored rather than
    rejected (V1_SPEC §3). Validating fields nobody consumes would break on the
    next Ollama release for no gain.

    **A malformed entry fails the whole listing** rather than being skipped.
    Skipping is how a partial answer becomes a confident one: drop the entry
    that could not be read and the remaining set is reported as *the* installed
    models, so a listing RepoAtlas half-understood produces a definite claim
    about what the server does not have.

    The reason string names types and positions only, never values — a
    misbehaving server's body is not RepoAtlas's to print (V1_SPEC §17).
    """
    if "models" not in body:
        return set(), "the object has no `models` key"
    listed = body["models"]
    if not isinstance(listed, list):
        # Specifically a list. `"x"` is iterable, and iterating it would walk
        # characters; a `dict` would walk keys. Both would "work" and both are
        # wrong, so the type is checked rather than the ability to loop.
        return set(), f"`models` is {_article(listed)}, not an array"

    names: set[str] = set()
    for index, entry in enumerate(listed):
        where = f"`models[{index}]`"
        if not isinstance(entry, dict):
            return set(), f"{where} is {_article(entry)}, not an object"
        if "name" not in entry:
            return set(), f"{where} has no `name`"
        name = entry["name"]
        if not isinstance(name, str):
            return set(), f"{where}.name is {_article(name)}, not a string"
        if not name.strip():
            # Not a version difference: `name` is the one field consumed, and an
            # empty one identifies no model. Treating it as a real name would put
            # `""` in the installed set and make the listing say something false.
            return set(), f"{where}.name is empty"
        names.add(name)
    return names, None


class OllamaBackend:
    """Talks to a local Ollama server over ``/api/chat``."""

    def __init__(
        self, settings: LLMSettings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._base_url = settings.base_url.rstrip("/")
        self._detail = ""
        self._category = ModelFailure.TRANSPORT
        # Injectable so the HTTP contract can be tested without a live server.
        self._transport = transport

    @property
    def id(self) -> str:
        return f"ollama/{self._settings.model}"

    # -- request -------------------------------------------------------------

    async def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None = None,
        response_schema: type[BaseModel] | None = None,
    ) -> ModelResponse:
        endpoint = f"{self._base_url}/api/chat"
        payload = self._payload(messages, tools, response_schema)

        response = await self._post(endpoint, payload)
        body = self._envelope(endpoint, response)
        content = self._content(endpoint, body)

        done_reason = body.get("done_reason")
        return ModelResponse(
            content=content,
            model=str(body.get("model") or self._settings.model),
            finish_reason=done_reason if isinstance(done_reason, str) else None,
            truncated=done_reason == _LENGTH_LIMIT_REASON,
        )

    def _payload(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSpec] | None,
        response_schema: type[BaseModel] | None,
    ) -> dict[str, Any]:
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
        return payload

    # -- layer 1: transport and the server ------------------------------------

    async def _post(self, endpoint: str, payload: dict[str, Any]) -> httpx.Response:
        """Send the request. Raises the narrowest error the failure supports.

        The exception classes are separated deliberately rather than caught as
        one ``httpx.HTTPError``: that superclass spans "the host does not exist"
        and "the server accepted my request and stopped talking", which are
        different problems with different fixes.
        """
        try:
            async with self._client(self._settings.timeout_seconds) as client:
                response = await client.post(endpoint, json=payload)
        except (httpx.UnsupportedProtocol, httpx.InvalidURL) as exc:
            raise self._configuration_error(exc) from exc
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise self._transport_error(endpoint, exc) from exc
        except httpx.TimeoutException as exc:
            # Not a connect timeout: the connection was established and the
            # server simply never finished. Saying "could not reach" here would
            # be false, and would point the user at the wrong thing entirely.
            raise ModelServerError(
                f"Ollama at {self._base_url} accepted the request but did not "
                f"respond within {self._settings.timeout_seconds:g}s.",
                detail=(
                    "The connection succeeded, so the server is running. Generation "
                    "may still be in progress.\n\n"
                    "Raise llm.timeout_seconds, or use a smaller model."
                ),
                backend=self.id,
                endpoint=endpoint,
                model=self._settings.model,
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            # Everything else httpx can raise below the status layer — a protocol
            # violation, a broken stream, a proxy failure. The connection existed,
            # so this is the server's behaviour, not its absence.
            raise ModelServerError(
                f"Ollama at {self._base_url} did not complete the HTTP response "
                f"({exc.__class__.__name__}).",
                backend=self.id,
                endpoint=endpoint,
                model=self._settings.model,
                retryable=True,
            ) from exc

        if response.is_error:
            raise self._status_error(endpoint, response)
        return response

    def _configuration_error(self, exc: Exception) -> ModelConfigurationError:
        return ModelConfigurationError(
            f"The configured Ollama URL is not usable: {self._base_url!r}",
            detail=(
                f"{exc.__class__.__name__}: the address must be an absolute http:// "
                "or https:// URL.\n\n"
                "Set it in config:\n\n  llm:\n    base_url: http://localhost:11434\n\n"
                "or in the environment:\n\n  REPOATLAS_LLM_BASE_URL=http://localhost:11434"
            ),
            backend=self.id,
            model=self._settings.model,
        )

    def _transport_error(self, endpoint: str, exc: Exception) -> ModelUnavailable:
        """The one place allowed to say the server could not be reached."""
        return ModelUnavailable(
            f"Could not reach Ollama at {self._base_url}.",
            detail=(
                f"No connection was established ({exc.__class__.__name__}).\n\n"
                "Start Ollama, or point RepoAtlas somewhere else:\n\n"
                "  REPOATLAS_LLM_BASE_URL=http://localhost:11434"
            ),
            backend=self.id,
            endpoint=endpoint,
            model=self._settings.model,
        )

    def _status_error(self, endpoint: str, response: httpx.Response) -> ModelServerError:
        status = response.status_code
        reported = _server_error_text(response)
        lines = [f"Ollama at {self._base_url} responded with HTTP {status}."]
        if reported:
            lines.append(f'It reported: "{reported}"')
        if status == 404:
            lines.append(
                f"A 404 from /api/chat usually means the model {self._settings.model!r} "
                f"is not installed.\n\n  ollama pull {self._settings.model}"
            )
        elif status in _RETRYABLE_STATUS:
            lines.append(
                "The server is running but could not serve this request; retrying may work."
            )
        elif 400 <= status < 500:
            lines.append(
                "The server rejected the request, so repeating it unchanged will not help."
            )
        return ModelServerError(
            f"Ollama returned HTTP {status}.",
            detail="\n\n".join(lines),
            backend=self.id,
            endpoint=endpoint,
            model=self._settings.model,
            http_status=status,
            retryable=status in _RETRYABLE_STATUS,
        )

    # -- layer 2: the Ollama API envelope -------------------------------------

    def _envelope(self, endpoint: str, response: httpx.Response) -> dict[str, Any]:
        """The response body, proven to be an Ollama API object.

        Every failure here is a *protocol* failure: an HTTP response arrived, so
        the server is alive and answering. Describing that as unreachable was the
        defect this classification exists to prevent.
        """
        try:
            body = response.json()
        except ValueError as exc:
            raise ModelProtocolError(
                f"Ollama at {self._base_url} responded with HTTP "
                f"{response.status_code}, but the body was not valid JSON.",
                detail=self._shape_detail(response, f"JSON parse failed: {exc.args[0]}"),
                backend=self.id,
                endpoint=endpoint,
                model=self._settings.model,
                http_status=response.status_code,
            ) from exc

        if not isinstance(body, dict):
            raise ModelProtocolError(
                f"Ollama at {self._base_url} responded with JSON that is not an "
                f"API object (got {type(body).__name__}).",
                detail=self._shape_detail(
                    response, "Expected a JSON object with a `message` field."
                ),
                backend=self.id,
                endpoint=endpoint,
                model=self._settings.model,
                http_status=response.status_code,
            )
        return body

    def _content(self, endpoint: str, body: dict[str, Any]) -> str:
        message = body.get("message")
        if message is None:
            raise self._envelope_error(
                endpoint, "the response has no `message` field", sorted(map(str, body))
            )
        if not isinstance(message, dict):
            raise self._envelope_error(
                endpoint, f"`message` is {_article(message)}, not an object", None
            )
        content = message.get("content")
        if content is None:
            raise self._envelope_error(
                endpoint, "`message` has no `content` field", sorted(map(str, message))
            )
        if not isinstance(content, str):
            raise self._envelope_error(
                endpoint, f"`message.content` is {_article(content)}, not a string", None
            )
        return content

    def _envelope_error(
        self, endpoint: str, problem: str, keys: list[str] | None
    ) -> ModelProtocolError:
        """Describe the envelope by its *shape*, never by its contents.

        Field names are structure and are safe to name; values are not, because
        a model's output is written from repository content.
        """
        detail = [
            "The server answered, so it is running — but this is not the "
            "/api/chat response format RepoAtlas expects.",
            f"Problem: {problem}.",
        ]
        if keys is not None:
            listed = ", ".join(f"`{k}`" for k in keys[:12]) or "(no fields)"
            detail.append(f"Fields present: {listed}")
        detail.append(
            "Check that llm.base_url points at an Ollama server and not another "
            "service on that port."
        )
        return ModelProtocolError(
            f"Ollama at {self._base_url} responded, but not in the expected API format.",
            detail="\n\n".join(detail),
            backend=self.id,
            endpoint=endpoint,
            model=self._settings.model,
        )

    @staticmethod
    def _shape_detail(response: httpx.Response, problem: str) -> str:
        return (
            "The server answered, so it is running — but the body is not the "
            "Ollama API format.\n\n"
            f"{problem}\n\n"
            f"Received {len(response.content)} bytes with content-type "
            f"{response.headers.get('content-type', 'unset')!r}."
        )

    # -- availability ---------------------------------------------------------

    async def available(self) -> bool:
        """Check the server is reachable *and* the configured model is present.

        A reachable server with the model unpulled would fail at request time, so
        both are part of availability — but they are different problems, and the
        recorded category says which one occurred.
        """
        endpoint = f"{self._base_url}/api/tags"
        try:
            async with self._client(_PROBE_TIMEOUT_SECONDS) as client:
                response = await client.get(endpoint)
        except (httpx.UnsupportedProtocol, httpx.InvalidURL) as exc:
            return self._probe_failed(
                ModelFailure.CONFIGURATION,
                f"The configured Ollama URL is not usable: {self._base_url!r} "
                f"({exc.__class__.__name__}).\n\n"
                "Set llm.base_url to an absolute http:// or https:// URL.",
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            return self._probe_failed(
                ModelFailure.TRANSPORT,
                f"No Ollama server responded at {self._base_url} "
                f"({exc.__class__.__name__}).\n\n"
                "Start one, or configure a different provider:\n\n"
                "  llm:\n"
                "    provider: ollama\n"
                "    base_url: http://localhost:11434",
            )
        except httpx.HTTPError as exc:
            return self._probe_failed(
                ModelFailure.SERVER,
                f"Ollama at {self._base_url} accepted the connection but the "
                f"model listing did not complete ({exc.__class__.__name__}).",
            )

        if response.is_error:
            reported = _server_error_text(response)
            return self._probe_failed(
                ModelFailure.SERVER,
                f"Ollama at {self._base_url} responded with HTTP "
                f"{response.status_code} to /api/tags."
                + (f'\n\nIt reported: "{reported}"' if reported else ""),
            )

        try:
            body = response.json()
        except ValueError:
            return self._probe_failed(
                ModelFailure.PROTOCOL,
                f"Ollama at {self._base_url} responded to /api/tags, but the body "
                f"was not valid JSON ({len(response.content)} bytes).\n\n"
                "Check that this address is an Ollama server.",
            )
        if not isinstance(body, dict):
            return self._probe_failed(
                ModelFailure.PROTOCOL,
                f"Ollama at {self._base_url} responded to /api/tags with JSON that "
                f"is not an API object (got {type(body).__name__}).",
            )

        installed, problem = _installed_names(body)
        if problem is not None:
            # Ordered deliberately: a body that is not a model listing cannot be
            # searched for a model, so this precedes the lookup rather than
            # falling through it with an empty set (ARCHITECTURE.md §11).
            return self._probe_failed(
                ModelFailure.PROTOCOL,
                f"Ollama at {self._base_url} responded to /api/tags, but the body "
                f"is not a model listing: {problem}.\n\n"
                "Check that this address is an Ollama server.",
            )
        if self._model_installed(installed):
            self._detail = ""
            return True

        listing = "\n".join(f"  {name}" for name in sorted(installed)) or "  (none)"
        return self._probe_failed(
            ModelFailure.MODEL_ABSENT,
            f"Ollama is running at {self._base_url}, but the configured model\n"
            f"{self._settings.model!r} is not installed.\n\n"
            f"Pull it:\n\n  ollama pull {self._settings.model}\n\n"
            f"Installed models:\n\n{listing}",
        )

    def _probe_failed(self, category: ModelFailure, detail: str) -> bool:
        self._category = category
        self._detail = detail
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

    def availability_category(self) -> ModelFailure:
        return self._category
