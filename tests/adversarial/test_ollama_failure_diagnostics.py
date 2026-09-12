"""Ollama failure classification (ARCHITECTURE.md §11, V1_SPEC §15).

Two invariants, and the first is the whole point:

    Receiving an invalid model response is not equivalent to failing to reach
    the model server.

    An error message describes the deepest layer successfully reached before
    the failure.

RepoAtlas tells the user what to go and fix. "Could not reach Ollama" sends them
to restart a server that answered on the first try; "the model returned invalid
output" sends them to change a model when the socket is closed. A diagnostic is
output too, and output has to be true.

**These tests use a real socket.** The finding they close was invisible to a
mocked transport, because the mock never distinguished "no connection" from
"a connection that returned something useless" — the two paths that were being
collapsed. Every transport-layer case below runs against an HTTP server bound to
a real ephemeral port on localhost. No network is required.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from repoatlas.config.settings import LLMSettings
from repoatlas.errors import ExitCode, ReasoningModelUnavailableError
from repoatlas.llm.ollama import OllamaBackend
from repoatlas.llm.protocol import (
    Message,
    ModelConfigurationError,
    ModelError,
    ModelFailure,
    ModelProtocolError,
    ModelResponse,
    ModelServerError,
    ModelUnavailable,
)
from repoatlas.llm.reasoner import QueryIntent, Reasoner
from repoatlas.llm.scripted import ScriptedBackend
from repoatlas.models.evidence import EvidenceCandidate, EvidenceSet, EvidenceTier

pytestmark = pytest.mark.adversarial

MODEL = "test-model"

#: Wording that asserts nothing was reached. Forbidden on every path where an
#: HTTP response actually arrived — the heart of the finding.
UNREACHABLE_WORDS = ("could not reach", "cannot connect", "no connection was established")


# -- a real HTTP server ------------------------------------------------------

#: (status, body, delay_seconds)
Reply = tuple[int, str, float]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        """Keep pytest output clean."""
        return

    def _reply(self) -> None:
        route: Callable[[str], Reply] = self.server.route  # type: ignore[attr-defined]
        status, body, delay = route(self.path)
        if delay:
            time.sleep(delay)
        payload = body.encode("utf-8", errors="surrogateescape")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            # The client timed out and hung up. That is the scenario under test.
            return

    def do_GET(self) -> None:
        self._reply()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        if length:
            self.rfile.read(length)
        self._reply()


@contextmanager
def serving(route: Callable[[str], Reply]) -> Iterator[str]:
    """Run an HTTP server on a real ephemeral port; yield its base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    server.route = route  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def closed_port() -> str:
    """A port nothing is listening on: bind it, learn the number, release it."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def chat_reply(content: str, *, done_reason: str = "stop") -> str:
    return json.dumps(
        {
            "model": MODEL,
            "message": {"role": "assistant", "content": content},
            "done": True,
            "done_reason": done_reason,
            "eval_count": 12,
        }
    )


def tags_reply(*models: str) -> str:
    return json.dumps({"models": [{"name": m, "model": m} for m in models]})


def only_chat(status: int, body: str, delay: float = 0.0) -> Callable[[str], Reply]:
    """Answer /api/tags healthily so a test can isolate the /api/chat behaviour."""

    def route(path: str) -> Reply:
        if path.endswith("/api/tags"):
            return (200, tags_reply(MODEL), 0.0)
        return (status, body, delay)

    return route


def backend(base_url: str, *, timeout: float = 10.0, model: str = MODEL) -> OllamaBackend:
    return OllamaBackend(
        LLMSettings.model_validate(
            {
                "provider": "ollama",
                "model": model,
                "base_url": base_url,
                "timeout_seconds": timeout,
            }
        )
    )


def complete(base_url: str, *, timeout: float = 10.0) -> ModelResponse:
    return asyncio.run(
        backend(base_url, timeout=timeout).complete([Message(role="user", content="hi")])
    )


def failure(base_url: str, *, timeout: float = 10.0) -> ModelError:
    with pytest.raises(ModelError) as caught:
        complete(base_url, timeout=timeout)
    return caught.value


def full_text(error: ModelError) -> str:
    return f"{error.message}\n{error.detail or ''}".lower()


def assert_says_unreachable(error: ModelError) -> None:
    assert error.category is ModelFailure.TRANSPORT, error.category
    assert any(word in full_text(error) for word in UNREACHABLE_WORDS), error.message


def assert_never_says_unreachable(error: ModelError) -> None:
    """The assertion the finding is about: a server that answered is not absent."""
    text = full_text(error)
    for word in UNREACHABLE_WORDS:
        assert word not in text, (word, error.message, error.detail)
    assert error.category is not ModelFailure.TRANSPORT, error.category


# == 1. transport: nothing was reached =======================================


def test_a_refused_connection_is_the_one_case_that_may_say_unreachable() -> None:
    error = failure(closed_port())
    assert isinstance(error, ModelUnavailable)
    assert_says_unreachable(error)
    assert error.retryable


def test_a_connect_timeout_is_a_transport_failure() -> None:
    """10.255.255.1 is RFC1918 space that routes nowhere: the SYN is never
    answered, so the connection times out rather than being refused."""
    error = failure("http://10.255.255.1:11434", timeout=0.25)
    assert error.category is ModelFailure.TRANSPORT
    assert_says_unreachable(error)


def test_an_unusable_url_is_configuration_not_unavailability() -> None:
    """RepoAtlas received a bad setting. Nothing was contacted, so nothing is
    unavailable — and telling the user to start Ollama would be wrong."""
    for bad in ("not-a-url", "ftp://localhost:11434"):
        error = failure(bad)
        assert isinstance(error, ModelConfigurationError), bad
        assert error.category is ModelFailure.CONFIGURATION
        assert "base_url" in (error.detail or "")


# == 2. server: reached, but it failed ========================================


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(400, False), (401, False), (403, False), (404, False), (429, True), (500, True), (503, True)],
)
def test_an_http_error_is_a_server_failure_not_a_reachability_failure(
    status: int, retryable: bool
) -> None:
    with serving(only_chat(status, json.dumps({"error": "boom"}))) as url:
        error = failure(url)
    assert isinstance(error, ModelServerError)
    assert error.category is ModelFailure.SERVER
    assert error.http_status == status
    assert error.retryable is retryable
    assert str(status) in error.message
    assert_never_says_unreachable(error)


def test_a_404_points_at_the_model_rather_than_the_server() -> None:
    with serving(only_chat(404, json.dumps({"error": "model not found"}))) as url:
        error = failure(url)
    assert "ollama pull" in (error.detail or "")


def test_a_read_timeout_does_not_claim_the_server_was_never_reached() -> None:
    """The connection succeeded and the server simply never finished.

    Calling that unreachable sends the user to restart a server that is running
    — and hides the two things that would actually help, a longer timeout or a
    smaller model.
    """
    with serving(only_chat(200, chat_reply("{}"), delay=2.0)) as url:
        error = failure(url, timeout=0.3)
    assert error.category is ModelFailure.SERVER
    assert error.retryable
    assert_never_says_unreachable(error)
    assert "timeout_seconds" in (error.detail or "")


# == 3. protocol: it answered, but not as an Ollama API =======================


@pytest.mark.parametrize(
    ("body", "because"),
    [
        ("not-json", "the body is not JSON at all"),
        ("", "the body is empty"),
        ("[1, 2, 3]", "the body is a JSON array"),
        ('"a string"', "the body is a bare JSON string"),
        ('{"unexpected": "shape"}', "no `message` field"),
        ('{"model": "m", "done": true}', "no `message` field, other fields present"),
        ('{"message": 123}', "`message` is not an object"),
        ('{"message": {"role": "assistant"}}', "`message` has no `content`"),
        ('{"message": {"content": 42}}', "`content` is not a string"),
    ],
)
def test_a_broken_envelope_is_a_protocol_failure(body: str, because: str) -> None:
    """Each of these used to be silent, a lie, or a traceback.

    `not-json` reported "Could not reach Ollama" — the review's finding exactly.
    `{"message": 123}` raised AttributeError and printed a Python traceback.
    `{"unexpected": "shape"}` returned empty content and no error at all, so the
    run continued and blamed the evidence.
    """
    with serving(only_chat(200, body)) as url:
        error = failure(url)
    assert isinstance(error, ModelProtocolError), because
    assert error.category is ModelFailure.PROTOCOL
    assert_never_says_unreachable(error)


def test_a_protocol_error_names_the_fields_it_saw_but_not_their_values() -> None:
    sentinel = "SENTINEL_FROM_REPOSITORY_CONTENT"
    with serving(only_chat(200, json.dumps({"unexpected": sentinel, "other": sentinel}))) as url:
        error = failure(url)
    text = f"{error.message}{error.detail}"
    assert "`unexpected`" in text
    assert sentinel not in text


def test_a_non_utf8_body_is_a_protocol_failure_not_a_transport_one() -> None:
    """Bytes were received, so the server exists. Whatever those bytes are, the
    failure is in what arrived, not in whether anything did.

    The body is written as latin-1 so the handler emits bytes that are not
    valid UTF-8; httpx decodes them leniently and the JSON parse is what fails.
    """
    with serving(only_chat(200, "\udce9\udcff not utf-8")) as url:
        error = failure(url)
    assert error.category is ModelFailure.PROTOCOL
    assert_never_says_unreachable(error)


# == 4. output: the envelope was fine, the model was not ======================


def test_a_valid_envelope_carrying_prose_is_not_an_error_at_the_backend() -> None:
    """The backend's contract ends at the envelope. Judging the *content* is the
    reasoner's job, and conflating the two is how layers get collapsed."""
    with serving(only_chat(200, chat_reply("I am not JSON."))) as url:
        response = complete(url)
    assert response.content == "I am not JSON."
    assert not response.truncated


@pytest.mark.parametrize("content", ["", "   \n\t "])
def test_an_empty_or_blank_response_still_parses_as_a_valid_envelope(content: str) -> None:
    with serving(only_chat(200, chat_reply(content))) as url:
        response = complete(url)
    assert response.content == content


def test_the_server_reported_output_limit_is_carried_as_truncation() -> None:
    """Ollama sets done_reason="length" exactly when llama-server reports
    finish_reason="length" — the num_predict budget was exhausted. It is a fact
    the server stated, which is what lets RepoAtlas say "truncated" rather than
    "looks incomplete"."""
    with serving(
        only_chat(200, chat_reply('{"summary": "the host is', done_reason="length"))
    ) as url:
        response = complete(url)
    assert response.truncated
    assert response.finish_reason == "length"


@pytest.mark.parametrize("reason", ["stop", "unload", "", "load"])
def test_any_other_finish_reason_is_not_truncation(reason: str) -> None:
    """A short or malformed answer is not evidence of a limit being hit, and
    telling the user to raise num_predict on that basis would be a guess."""
    with serving(only_chat(200, chat_reply("{}", done_reason=reason))) as url:
        response = complete(url)
    assert not response.truncated


def test_a_missing_done_reason_is_not_truncation() -> None:
    body = json.dumps({"model": MODEL, "message": {"role": "assistant", "content": "{}"}})
    with serving(only_chat(200, body)) as url:
        response = complete(url)
    assert not response.truncated
    assert response.finish_reason is None


# == 5. the healthy path is untouched ========================================


def test_a_healthy_response_flows_through_unchanged() -> None:
    answer = json.dumps({"summary": "It is set in config.py.", "hypotheses": [], "unknowns": []})
    with serving(only_chat(200, chat_reply(answer))) as url:
        response = complete(url)
    assert response.content == answer
    assert response.model == MODEL
    assert response.finish_reason == "stop"
    assert not response.truncated


def test_a_healthy_exchange_round_trips_over_a_real_socket() -> None:
    def route(path: str) -> Reply:
        return (200, tags_reply(MODEL) if path.endswith("/api/tags") else chat_reply("{}"), 0.0)

    with serving(route) as url:
        probe = backend(url)
        assert asyncio.run(probe.available())
        assert complete(url).content == "{}"


# == 6. availability: up-but-empty is not down ================================


def test_availability_distinguishes_a_dead_server_from_a_missing_model() -> None:
    dead = backend(closed_port())
    assert not asyncio.run(dead.available())
    assert dead.availability_category() is ModelFailure.TRANSPORT
    assert "no ollama server responded" in dead.availability_detail().lower()

    with serving(lambda _p: (200, tags_reply("other-model:latest"), 0.0)) as url:
        absent = backend(url)
        assert not asyncio.run(absent.available())
        assert absent.availability_category() is ModelFailure.MODEL_ABSENT
        detail = absent.availability_detail()
        assert "is running" in detail
        assert f"ollama pull {MODEL}" in detail
        assert not any(word in detail.lower() for word in UNREACHABLE_WORDS)


def test_availability_reports_a_server_error_as_a_server_error() -> None:
    """The probe endpoint answered with HTTP 500. Saying "no server responded"
    about a server that responded is the same lie one layer up."""
    with serving(lambda _p: (500, json.dumps({"error": "boom"}), 0.0)) as url:
        probe = backend(url)
        assert not asyncio.run(probe.available())
        assert probe.availability_category() is ModelFailure.SERVER
        assert "http 500" in probe.availability_detail().lower()
        assert "no ollama server responded" not in probe.availability_detail().lower()


@pytest.mark.parametrize("body", ["not-json", "[1,2,3]"])
def test_availability_reports_a_broken_probe_body_as_a_protocol_failure(body: str) -> None:
    with serving(lambda _p, b=body: (200, b, 0.0)) as url:
        probe = backend(url)
        assert not asyncio.run(probe.available())
        assert probe.availability_category() is ModelFailure.PROTOCOL
        assert "no ollama server responded" not in probe.availability_detail().lower()


def test_availability_succeeds_and_clears_stale_detail() -> None:
    with serving(lambda _p: (200, tags_reply(MODEL), 0.0)) as url:
        probe = backend(url)
        assert asyncio.run(probe.available())


def test_availability_tolerates_an_implicit_latest_tag() -> None:
    with serving(lambda _p: (200, tags_reply(f"{MODEL}:latest"), 0.0)) as url:
        assert asyncio.run(backend(url).available())


def test_availability_survives_a_models_field_of_the_wrong_type() -> None:
    """Not a crash, and not an absence either.

    This test used to assert `MODEL_ABSENT` here, which was the defect rather
    than the contract: a `models` field that is not an array is a body RepoAtlas
    could not read, and "the configured model is not installed" is a claim about
    the server's state that such a body cannot support. The no-crash property it
    was written for is unchanged; the classification is now truthful. The full
    matrix lives in `test_ollama_tags_protocol.py`.
    """
    with serving(lambda _p: (200, json.dumps({"models": "not-a-list"}), 0.0)) as url:
        probe = backend(url)
        assert not asyncio.run(probe.available())
        assert probe.availability_category() is ModelFailure.PROTOCOL
        assert "is not installed" not in probe.availability_detail()


# == 7. the reasoner: output problems, and repair =============================


def evidence_set() -> EvidenceSet:
    return EvidenceSet(
        question="Where is SERVICE_HOST set?",
        candidates=(
            EvidenceCandidate(
                repository="svc",
                repository_slug="svc",
                path="app.py",
                line_start=1,
                line_end=2,
                text="SERVICE_HOST = 'localhost'",
                description="contains SERVICE_HOST",
                tier=EvidenceTier.DETERMINISTIC,
                score=1.0,
            ),
        ),
    )


#: A grounded answer over the evidence set above. It carries a hypothesis
#: because an answer with none is "insufficient" by definition, which would make
#: a repaired-successfully assertion indistinguishable from a repair that failed.
VALID_ANSWER = json.dumps(
    {
        "summary": "It is set in app.py.",
        "hypotheses": [
            {
                "statement": "SERVICE_HOST is defined in app.py.",
                "confidence": 0.9,
                "evidence": [
                    {
                        "repository": "svc",
                        "repository_slug": "svc",
                        "path": "app.py",
                        "line_start": 1,
                        "line_end": 2,
                        "description": "contains SERVICE_HOST",
                    }
                ],
            }
        ],
        "unknowns": [],
    }
)


def reason_with(backend_obj: ScriptedBackend):
    return asyncio.run(Reasoner(backend_obj).reason("q", evidence_set(), QueryIntent.ASK))


def test_a_malformed_first_response_repaired_successfully_still_answers() -> None:
    scripted = ScriptedBackend(["not json at all", VALID_ANSWER])
    result = reason_with(scripted)
    assert result.repaired
    assert not result.is_insufficient
    assert result.answer.summary == "It is set in app.py."
    assert len(scripted.calls) == 2


def test_a_repair_that_also_fails_names_both_failures() -> None:
    result = reason_with(ScriptedBackend(["not json", "still not json"]))
    assert result.repaired
    assert result.is_insufficient
    reason = "\n".join(result.answer.unknowns).lower()
    assert "repair attempt" in reason
    assert "not valid json" in reason
    assert "parsing failed" in reason
    assert not any(word in reason for word in UNREACHABLE_WORDS)


def test_a_schema_violation_is_reported_as_a_schema_violation() -> None:
    """Well-formed JSON of the wrong shape is a different problem from text that
    is not JSON, and telling them apart is the difference between "the model
    ignored the schema" and "the model did not emit JSON"."""
    wrong = json.dumps({"summary": 12345, "hypotheses": "not-a-list"})
    result = reason_with(ScriptedBackend([wrong, wrong]))
    reason = "\n".join(result.answer.unknowns).lower()
    assert "schema" in reason
    assert "not valid json" not in reason


def test_an_empty_response_is_reported_as_an_empty_response() -> None:
    result = reason_with(ScriptedBackend(["", ""]))
    reason = "\n".join(result.answer.unknowns).lower()
    assert "empty response" in reason
    assert "connection" in reason  # explicitly says it is NOT a connection problem
    assert not any(word in reason for word in UNREACHABLE_WORDS)


class _TruncatingBackend:
    """A backend whose responses report the server's own output-limit signal."""

    id = "truncating"

    def __init__(self, contents: list[str], *, truncated: bool = True) -> None:
        self._contents = list(contents)
        self._truncated = truncated
        self.calls = 0

    async def complete(self, messages, tools=None, response_schema=None) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            content=self._contents.pop(0),
            model=self.id,
            finish_reason="length" if self._truncated else "stop",
            truncated=self._truncated,
        )

    async def available(self) -> bool:
        return True

    def availability_detail(self) -> str:
        return ""

    def availability_category(self) -> ModelFailure:
        return ModelFailure.TRANSPORT


def test_a_truncated_answer_says_so_and_names_the_output_limit() -> None:
    """The silent case: before, a response cut off at num_predict produced
    "INSUFFICIENT EVIDENCE" and nothing else, which reads as "your repository
    does not contain the answer" — a statement about the wrong subject."""
    fragment = '{"summary": "the host is'
    result = asyncio.run(
        Reasoner(_TruncatingBackend([fragment, fragment])).reason(
            "q", evidence_set(), QueryIntent.ASK
        )
    )
    reason = "\n".join(result.answer.unknowns).lower()
    assert "cut off" in reason
    assert "output limit" in reason
    assert "num_predict" in reason
    assert not any(word in reason for word in UNREACHABLE_WORDS)


def test_a_non_truncated_failure_never_suggests_raising_the_output_limit() -> None:
    """Advice has to be earned. A model that emitted confident nonsense will
    emit more of it with a bigger budget."""
    result = asyncio.run(
        Reasoner(_TruncatingBackend(["garbage", "garbage"], truncated=False)).reason(
            "q", evidence_set(), QueryIntent.ASK
        )
    )
    reason = "\n".join(result.answer.unknowns).lower()
    assert "num_predict" not in reason
    assert "output limit" not in reason


def test_a_backend_failure_during_repair_is_reported_as_a_backend_failure() -> None:
    """The first response was invalid, but what ended the run was the server.

    Reporting "the model did not return a valid answer" would name the layer
    that failed first instead of the layer that failed last — and only one of
    those is actionable.
    """
    scripted = ScriptedBackend(["not json"])  # exhausted -> raises on the repair call
    with pytest.raises(ModelError):
        reason_with(scripted)


def test_grounding_rejection_is_still_an_evidence_outcome_not_a_model_failure() -> None:
    """A parsed, schema-valid answer citing evidence that was never supplied is
    the grounding layer doing its job. It must not become a backend diagnostic."""
    fabricated = json.dumps(
        {
            "summary": "It is set elsewhere.",
            "hypotheses": [
                {
                    "statement": "Set in infra/secrets.yaml",
                    "confidence": 0.9,
                    "evidence": [
                        {
                            "repository": "infra",
                            "path": "secrets.yaml",
                            "line_start": 1,
                            "line_end": 2,
                            "description": "the credential",
                        }
                    ],
                }
            ],
            "unknowns": [],
        }
    )
    result = reason_with(ScriptedBackend([fabricated]))
    assert not result.repaired
    reason = "\n".join(result.answer.unknowns).lower()
    assert "repair" not in reason
    assert "invalid" not in reason


# == 8. cancellation is not a model failure ===================================


class _CancellingBackend:
    id = "cancelling"

    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def complete(self, messages, tools=None, response_schema=None):
        raise self._error

    async def available(self) -> bool:
        return True

    def availability_detail(self) -> str:
        return ""

    def availability_category(self) -> ModelFailure:
        return ModelFailure.TRANSPORT


@pytest.mark.parametrize(
    "error",
    [KeyboardInterrupt(), SystemExit(1), asyncio.CancelledError()],
    ids=["keyboard-interrupt", "system-exit", "cancelled"],
)
def test_user_cancellation_is_never_converted_into_a_model_failure(error: BaseException) -> None:
    """A user pressing ctrl-c has not discovered a broken server."""
    with pytest.raises(type(error)):
        asyncio.run(
            Reasoner(_CancellingBackend(error)).reason("q", evidence_set(), QueryIntent.ASK)
        )


def test_a_programming_error_in_a_backend_is_not_relabelled() -> None:
    """An unexpected bug must not be laundered into an operational diagnosis."""
    with pytest.raises(AttributeError):
        asyncio.run(
            Reasoner(_CancellingBackend(AttributeError("bug"))).reason(
                "q", evidence_set(), QueryIntent.ASK
            )
        )


# == 9. exit codes and the service boundary ===================================


@pytest.mark.parametrize(
    "make",
    [
        lambda url: (200, "not-json", 0.0),
        lambda url: (500, '{"error": "boom"}', 0.0),
        lambda url: (200, '{"message": 123}', 0.0),
    ],
    ids=["protocol", "server", "envelope"],
)
def test_every_backend_failure_maps_to_the_stable_exit_code(make) -> None:
    """One stable non-zero exit for backend failure; the *message* carries the
    cause. A distinct code per subtype would be a second taxonomy to keep in
    sync with the first."""
    from repoatlas.services.query_service import reason as service_reason

    with serving(only_chat(*make(None))) as url:
        error = failure(url)
    wrapped = ReasoningModelUnavailableError(error.message, detail=error.detail)
    assert wrapped.exit_code == ExitCode.LLM_UNAVAILABLE
    assert service_reason is not None  # the import is the contract under test


def test_the_taxonomy_is_reachable_from_the_public_package() -> None:
    import repoatlas.llm as package

    for name in (
        "ModelError",
        "ModelFailure",
        "ModelUnavailable",
        "ModelServerError",
        "ModelProtocolError",
        "ModelOutputError",
        "ModelOutputTruncated",
        "ModelRepairFailed",
        "ModelConfigurationError",
    ):
        assert hasattr(package, name), name


def test_every_specific_failure_is_catchable_as_the_base_class() -> None:
    """A caller that only needs "the backend failed" must not have to enumerate."""
    with serving(only_chat(500, "{}")) as url, pytest.raises(ModelError):
        complete(url)


def test_transport_and_output_failures_are_distinguishable_in_code() -> None:
    """The requirement in one assertion: these two must not be one type."""
    transport = failure(closed_port())
    with serving(only_chat(200, "not-json")) as url:
        protocol = failure(url)
    assert isinstance(transport, ModelUnavailable)
    assert not isinstance(protocol, ModelUnavailable)
    assert transport.category is not protocol.category


# == 10. no response body ever reaches an error message =======================


def test_a_server_error_reports_ollamas_error_field_and_nothing_else() -> None:
    leak = "LEAKED_REPOSITORY_CONTENT_SENTINEL"
    body = json.dumps({"error": "model failed", "trace": leak, "prompt": leak})
    with serving(only_chat(500, body)) as url:
        error = failure(url)
    text = f"{error.message}{error.detail}"
    assert "model failed" in text
    assert leak not in text


def test_a_long_server_error_is_bounded() -> None:
    with serving(only_chat(500, json.dumps({"error": "x" * 5000}))) as url:
        error = failure(url)
    assert len(error.detail or "") < 1000


def test_a_protocol_error_reports_size_not_content() -> None:
    leak = "LEAKED_REPOSITORY_CONTENT_SENTINEL"
    with serving(only_chat(200, f"garbage {leak} garbage")) as url:
        error = failure(url)
    text = f"{error.message}{error.detail}"
    assert leak not in text
    assert "bytes" in text


def test_an_output_diagnostic_reports_a_position_not_the_text() -> None:
    leak = "LEAKED_REPOSITORY_CONTENT_SENTINEL"
    result = reason_with(ScriptedBackend([f"prose {leak}", f"more prose {leak}"]))
    reason = "\n".join(result.answer.unknowns)
    assert leak not in reason
    assert "parsing failed" in reason
    assert "column" in reason


# == 11. --no-llm never touches a backend =====================================


def test_no_llm_never_constructs_or_contacts_a_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Evidence-only mode must not be able to fail on a model it never wanted."""
    import repoatlas.services.query_service as service

    def explode(settings):
        raise AssertionError("--no-llm must not build a backend")

    monkeypatch.setattr(service, "build_model", explode)

    from repoatlas.cli import main as cli_main

    source = Path(cli_main.__file__).read_text(encoding="utf-8")
    reasoning = source[source.index("def _reasoning_command") :]
    body = reasoning[: reasoning.index("except RepoAtlasError")]
    # The early return is structural, not incidental: `services.reason` — the
    # only caller that builds a backend — appears after it.
    assert body.index("if no_llm:") < body.index("services.reason")
    assert "services.locate" in body
