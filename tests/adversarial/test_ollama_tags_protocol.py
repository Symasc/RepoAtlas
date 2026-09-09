"""`/api/tags` protocol validation (V1_SPEC §3, ARCHITECTURE.md §11).

The invariant this file exists to pin:

    "Model absent" may only be reported after a structurally valid Ollama
    model-list response has been parsed and inspected.

The fresh review found that `{"models": "x"}` — HTTP 200, valid JSON, and not
remotely an Ollama model listing — produced:

    Ollama is running at http://localhost:11434, but the configured model
    'gpt-oss:20b' is not installed.
    Pull it:  ollama pull gpt-oss:20b
    Installed models:  (none)

Every clause of that is a claim about the server's *state*, drawn from a body
RepoAtlas never understood. The advice is unfollowable: pulling the model
changes nothing, because the response was never a model listing.

The cause was three individually reasonable fallbacks whose sum is an empty
set — a missing key defaulting to `None`, a non-list defaulting to `[]`, and
malformed entries being skipped. An empty set of installed models is exactly
what a healthy server with nothing pulled returns, so "could not read the
answer" and "the answer says no" became the same value.

`{"models": []}` is the control that makes this a real distinction rather than a
blanket refusal: it is a valid listing, it genuinely means the model is absent,
and it must keep saying so. A real Ollama 0.32.14 on this machine returns
exactly that, and it is exercised below when one is reachable.

Every case here runs against a real HTTP server on an ephemeral localhost port,
recording each request, so "no generation was attempted" is asserted from the
server's own log rather than assumed.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

import httpx
import pytest
from repoatlas.config.settings import LLMSettings
from repoatlas.errors import ExitCode, ReasoningModelUnavailableError
from repoatlas.llm.ollama import OllamaBackend, _installed_names
from repoatlas.llm.protocol import ModelFailure
from repoatlas.llm.reasoner import QueryIntent
from repoatlas.models.context import RepositoryContext
from repoatlas.services.workspace import Workspace
from tests.conftest import init_git_repository

pytestmark = pytest.mark.adversarial

MODEL = "gpt-oss:20b"

#: Wording that claims the model is missing. Forbidden wherever the body was
#: never a readable listing — that claim is the finding.
ABSENCE_WORDS = ("is not installed", "ollama pull", "installed models")


# -- a real HTTP server that records what was asked --------------------------


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def _reply(self) -> None:
        server = self.server
        server.seen.append((self.command, self.path))  # type: ignore[attr-defined]
        status, body, delay = server.route(self.path)  # type: ignore[attr-defined]
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
            return

    def do_GET(self) -> None:
        self._reply()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        if length:
            self.rfile.read(length)
        self._reply()


@contextmanager
def serving(route: Callable[[str], tuple[int, str, float]]) -> Iterator[tuple[str, list]]:
    """An HTTP server on a real port; yields its URL and its request log."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    server.route = route  # type: ignore[attr-defined]
    server.seen = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", server.seen  # type: ignore[attr-defined]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def tags_body(body: str) -> Callable[[str], tuple[int, str, float]]:
    """Serve ``body`` for /api/tags; anything else is a test failure to detect."""

    def route(path: str) -> tuple[int, str, float]:
        if path.endswith("/api/tags"):
            return (200, body, 0.0)
        return (200, json.dumps({"unexpected": path}), 0.0)

    return route


def backend(base_url: str, *, model: str = MODEL, timeout: float = 10.0) -> OllamaBackend:
    return OllamaBackend(
        LLMSettings.model_validate(
            {"provider": "ollama", "model": model, "base_url": base_url, "timeout_seconds": timeout}
        )
    )


def probe(payload: object, *, model: str = MODEL) -> tuple[bool, ModelFailure, str]:
    """Run the real availability path over a mock transport."""
    back = OllamaBackend(
        LLMSettings.model_validate({"model": model}),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)),
    )
    ok = asyncio.run(back.available())
    return ok, back.availability_category(), back.availability_detail()


def probe_raw(text: str, *, status: int = 200) -> tuple[bool, ModelFailure, str]:
    back = OllamaBackend(
        LLMSettings.model_validate({"model": MODEL}),
        transport=httpx.MockTransport(lambda r: httpx.Response(status, content=text.encode())),
    )
    ok = asyncio.run(back.available())
    return ok, back.availability_category(), back.availability_detail()


# -- 1, 41. the original reproduction ----------------------------------------


def test_the_original_finding_is_a_protocol_error_not_an_absence() -> None:
    """`{"models": "x"}` said "pull the model". It cannot be pulled into a string."""
    ok, category, detail = probe({"models": "x"})
    assert ok is False
    assert category is ModelFailure.PROTOCOL
    lowered = detail.lower()
    for word in ABSENCE_WORDS:
        assert word not in lowered, f"still claims absence: {detail}"
    assert "not a model listing" in lowered
    assert "`models` is a str, not an array" in detail


def test_the_valid_empty_listing_still_reports_absence() -> None:
    """The control. Without this, the fix could be a blanket refusal.

    `{"models": []}` is what a healthy Ollama with nothing pulled returns, and
    `ollama pull` is exactly the right advice for it.
    """
    ok, category, detail = probe({"models": []})
    assert ok is False
    assert category is ModelFailure.MODEL_ABSENT
    assert "is not installed" in detail
    assert f"ollama pull {MODEL}" in detail


# -- 5. top-level type matrix -------------------------------------------------


TOP_LEVEL = [
    None,
    [],
    "string",
    123,
    True,
    {},
    {"models": None},
    {"models": {}},
    {"models": "x"},
    {"models": 123},
    {"models": True},
]


@pytest.mark.parametrize("payload", TOP_LEVEL, ids=[repr(p)[:24] for p in TOP_LEVEL])
def test_no_top_level_shape_becomes_model_absent(payload: object) -> None:
    ok, category, detail = probe(payload)
    assert ok is False
    assert category is ModelFailure.PROTOCOL, detail
    assert "is not installed" not in detail


# -- 6. model item matrix -----------------------------------------------------


ITEMS: list[object] = [
    [None],
    ["llama"],
    [123],
    [{}],
    [{"name": None}],
    [{"name": 123}],
    [{"name": ""}],
    [{"name": "   "}],
    [True],
    [[]],
]


@pytest.mark.parametrize("models", ITEMS, ids=[repr(m)[:24] for m in ITEMS])
def test_no_malformed_entry_becomes_model_absent(models: object) -> None:
    ok, category, detail = probe({"models": models})
    assert ok is False
    assert category is ModelFailure.PROTOCOL, detail
    assert "is not installed" not in detail


def test_the_reason_names_the_offending_position() -> None:
    """A bounded, actionable reason — the index, not the payload."""
    _, _, detail = probe({"models": [{"name": "ok"}, 123]})
    assert "`models[1]` is an int, not an object" in detail
    _, _, detail = probe({"models": [{"size": 1}]})
    assert "`models[0]` has no `name`" in detail
    _, _, detail = probe({"models": [{"name": 7}]})
    assert "`models[0]`.name is an int, not a string" in detail
    _, _, detail = probe({"models": [{"name": ""}]})
    assert "`models[0]`.name is empty" in detail


def test_an_empty_name_is_malformed_not_a_model() -> None:
    """`name` is the one field consumed; an empty one identifies nothing.

    Recording it would put `""` into the installed set and make the listing
    report a model that does not exist. Not a version difference — a value the
    consumed field cannot legitimately take.
    """
    names, problem = _installed_names({"models": [{"name": ""}]})
    assert names == set()
    assert problem == "`models[0]`.name is empty"


# -- 7. mixed valid and invalid entries ---------------------------------------


def test_a_malformed_entry_fails_the_whole_listing() -> None:
    """Skipping is how a partial answer becomes a confident one.

    Drop the unreadable entry and the rest is reported as *the* installed set,
    so a listing RepoAtlas half-understood yields a definite claim about what
    the server does not have. The safe reading is that the listing is unusable.
    """
    payload = {"models": [{"name": "foo"}, {"bad": "shape"}, {"name": MODEL}]}
    ok, category, detail = probe(payload)
    assert ok is False
    assert category is ModelFailure.PROTOCOL
    assert "`models[1]` has no `name`" in detail


def test_a_malformed_entry_fails_even_when_the_wanted_model_precedes_it() -> None:
    """Order must not decide the verdict: the listing is unusable either way."""
    payload = {"models": [{"name": MODEL}, 42]}
    ok, category, _ = probe(payload)
    assert ok is False
    assert category is ModelFailure.PROTOCOL


# -- 8, 35, 39. real shapes and schema evolution ------------------------------


REAL_ENTRY = {
    "name": MODEL,
    "model": MODEL,
    "modified_at": "2026-01-01T00:00:00.000000000Z",
    "size": 13780173839,
    "digest": "aabbccddeeff",
    "details": {
        "parent_model": "",
        "format": "gguf",
        "family": "gpt-oss",
        "families": ["gpt-oss"],
        "parameter_size": "20.9B",
        "quantization_level": "MXFP4",
    },
}


def test_a_full_real_entry_validates() -> None:
    names, problem = _installed_names({"models": [REAL_ENTRY]})
    assert problem is None
    assert names == {MODEL}


def test_unknown_fields_are_tolerated() -> None:
    """§35. Strict about what is consumed, tolerant about everything else."""
    payload = {
        "models": [{**REAL_ENTRY, "capabilities": ["tools"], "future": {"a": 1}}],
        "future_top_level": "ignored",
    }
    names, problem = _installed_names(payload)
    assert problem is None
    assert names == {MODEL}


def test_a_future_server_that_stops_returning_an_array_is_a_protocol_error() -> None:
    """Tolerance has a limit: an unreadable listing is not an empty one."""
    ok, category, _ = probe({"models": {"gpt-oss:20b": {"size": 1}}})
    assert ok is False
    assert category is ModelFailure.PROTOCOL


# -- 9, 11, 12. matching semantics preserved ----------------------------------


def test_the_present_model_is_still_found() -> None:
    ok, _, _ = probe({"models": [{"name": MODEL}]})
    assert ok is True


@pytest.mark.parametrize(
    ("configured", "listed"),
    [
        ("gpt-oss:20b", "gpt-oss:20b"),
        ("gpt-oss:20b", "gpt-oss:20b-latest"),
        ("llama3.2", "llama3.2"),
        ("llama3.2", "llama3.2:latest"),
        ("model", "model:latest"),
    ],
)
def test_existing_name_matching_is_unchanged(configured: str, listed: str) -> None:
    """§9. This pass is classification, not matching policy."""
    ok, _, _ = probe({"models": [{"name": listed}]}, model=configured)
    assert ok is True, (configured, listed)


def test_a_genuinely_missing_model_is_still_absent() -> None:
    """§11. A valid listing without the model is absence, not protocol failure."""
    ok, category, detail = probe({"models": [{"name": "foo:latest"}, {"name": "bar:7b"}]})
    assert ok is False
    assert category is ModelFailure.MODEL_ABSENT
    assert "foo:latest" in detail and "bar:7b" in detail


# -- 13. invalid JSON ---------------------------------------------------------


@pytest.mark.parametrize("body", ["{", "not-json", "<html>error</html>", "", "  "])
def test_invalid_json_is_a_protocol_failure_not_absence(body: str) -> None:
    ok, category, detail = probe_raw(body)
    assert ok is False
    assert category is ModelFailure.PROTOCOL
    assert "not valid JSON" in detail
    assert "is not installed" not in detail


def test_json_decoding_failure_is_distinguishable_from_shape_failure() -> None:
    """§13. Both are PROTOCOL; the message says which, so the fix differs."""
    _, _, bad_json = probe_raw("not-json")
    _, _, bad_shape = probe({"models": "x"})
    assert "not valid JSON" in bad_json
    assert "not a model listing" in bad_shape
    assert bad_json != bad_shape


# -- 15. HTTP status comes first ----------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 502, 503])
def test_http_errors_stay_server_failures(status: int) -> None:
    """§15. Status is read before the body; an unparseable body cannot demote it."""
    ok, category, detail = probe_raw("<html>nginx</html>", status=status)
    assert ok is False
    assert category is ModelFailure.SERVER, detail
    assert str(status) in detail
    assert "is not installed" not in detail


def test_an_http_error_carrying_a_valid_listing_is_still_a_server_failure() -> None:
    """Precedence, stated as a test: the body is not consulted for the verdict."""
    body = json.dumps({"models": [{"name": MODEL}]})
    ok, category, _ = probe_raw(body, status=503)
    assert ok is False
    assert category is ModelFailure.SERVER


# -- 17. connection refused ---------------------------------------------------


def closed_port() -> str:
    with socket.socket() as probe_socket:
        probe_socket.bind(("127.0.0.1", 0))
        port = probe_socket.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def test_connection_refused_is_transport_not_protocol_or_absence() -> None:
    back = backend(closed_port())
    assert asyncio.run(back.available()) is False
    assert back.availability_category() is ModelFailure.TRANSPORT
    detail = back.availability_detail()
    assert "No Ollama server responded" in detail
    assert "is not installed" not in detail


# -- 16. timeout --------------------------------------------------------------


def test_a_slow_server_is_not_reported_as_model_absence() -> None:
    """A deterministic local delay past the probe timeout. No network."""

    def route(path: str) -> tuple[int, str, float]:
        return (200, json.dumps({"models": []}), 6.0)

    with serving(route) as (url, _seen):
        back = backend(url)
        assert asyncio.run(back.available()) is False
        assert back.availability_category() is not ModelFailure.MODEL_ABSENT
        assert "is not installed" not in back.availability_detail()


# -- 33, 34. proxies and error envelopes --------------------------------------


def test_a_proxy_login_page_is_a_protocol_failure() -> None:
    """§33. HTTP 200 with an HTML login page is not an empty model list."""
    ok, category, detail = probe_raw("<html><body>Please sign in</body></html>")
    assert ok is False
    assert category is ModelFailure.PROTOCOL
    assert "is not installed" not in detail


def test_a_gateway_json_error_with_status_200_is_a_protocol_failure() -> None:
    """§34. `{"error": "unauthorized"}` is not a listing with nothing in it."""
    ok, category, detail = probe({"error": "unauthorized"})
    assert ok is False
    assert category is ModelFailure.PROTOCOL
    assert "the object has no `models` key" in detail
    assert "is not installed" not in detail


# -- 23, 37. bounded diagnostics ----------------------------------------------


def test_a_huge_malformed_body_is_not_echoed() -> None:
    """A misbehaving server's payload is not RepoAtlas's to print."""
    secret = "SUPER_SECRET_TOKEN_918273"  # noqa: S105 - a marker, not a credential
    payload = {"models": secret * 20_000}
    ok, category, detail = probe(payload)
    assert ok is False
    assert category is ModelFailure.PROTOCOL
    assert secret not in detail
    assert len(detail) < 500, len(detail)


def test_a_deeply_nested_body_is_not_echoed() -> None:
    nested: object = "leaf-value-918273"
    for _ in range(200):
        nested = {"models": nested}
    ok, _, detail = probe(nested)
    assert ok is False
    assert "leaf-value-918273" not in detail
    assert len(detail) < 500


def test_no_diagnostic_carries_a_model_name_from_a_malformed_body() -> None:
    """Even a plausible-looking name must not be reported as installed."""
    _, _, detail = probe({"models": [{"name": "plausible:7b"}, "junk"]})
    assert "plausible:7b" not in detail


# -- 19. no raw Python exception types ----------------------------------------


ALL_MALFORMED: list[object] = [
    *TOP_LEVEL,
    *[{"models": m} for m in ITEMS],
    {"models": [{"name": "foo"}, {"bad": "shape"}]},
    {"error": "unauthorized"},
]


@pytest.mark.parametrize("payload", ALL_MALFORMED, ids=range(len(ALL_MALFORMED)))
def test_no_raw_python_error_name_reaches_the_diagnostic(payload: object) -> None:
    _, _, detail = probe(payload)
    for leak in ("KeyError", "TypeError", "AttributeError", "JSONDecodeError", "Traceback"):
        assert leak not in detail, detail


@pytest.mark.parametrize("payload", ALL_MALFORMED, ids=range(len(ALL_MALFORMED)))
def test_availability_never_raises(payload: object) -> None:
    """A bounded domain outcome, never an exception out of the probe."""
    ok, _, _ = probe(payload)
    assert ok is False


# -- 22. no generic iterable handling -----------------------------------------


def test_models_must_be_a_list_not_merely_iterable() -> None:
    """The exact mechanism: a str and a dict are both iterable and both wrong.

    Iterating `"x"` walks characters; iterating `{"a": 1}` walks keys. Either
    would "work" and produce nonsense, so the type is checked rather than the
    ability to loop.
    """
    for wrong in ("x", {"a": 1}, 123, True, None):
        names, problem = _installed_names({"models": wrong})
        assert names == set()
        assert problem is not None and "not an array" in problem, wrong
    names, problem = _installed_names({"models": [{"name": MODEL}]})
    assert problem is None and names == {MODEL}


# -- 20, 25, 26. CLI behaviour ------------------------------------------------


def test_a_malformed_listing_stops_before_any_generation_request() -> None:
    """§26. Asserted from the server's own request log."""
    from repoatlas.services.query_service import reason

    with serving(tags_body('{"models": "x"}')) as (url, seen):
        back = backend(url)
        with pytest.raises(ReasoningModelUnavailableError) as caught:
            reason(_workspace_stub(), "q", _context_stub(), QueryIntent.ASK, model=back)
        assert caught.value.exit_code == ExitCode.LLM_UNAVAILABLE
        paths = [path for _method, path in seen]
        assert paths == ["/api/tags"], paths
        assert not any("chat" in p or "generate" in p for p in paths)
    lowered = f"{caught.value}{caught.value.detail or ''}".lower()
    for word in ABSENCE_WORDS:
        assert word not in lowered


def test_a_valid_absence_also_stops_before_generation() -> None:
    """The control: same stop, different and truthful reason."""
    from repoatlas.services.query_service import reason

    with serving(tags_body('{"models": []}')) as (url, seen):
        back = backend(url)
        with pytest.raises(ReasoningModelUnavailableError) as caught:
            reason(_workspace_stub(), "q", _context_stub(), QueryIntent.ASK, model=back)
        assert [p for _m, p in seen] == ["/api/tags"]
    assert "is not installed" in (caught.value.detail or "")


def _workspace_stub() -> Workspace:
    """A placeholder the code under test provably never dereferences.

    `reason` checks availability before it touches the workspace, the context or
    the evidence pipeline — which is the ordering these tests exist to pin. A
    real workspace would index a repository to prove something already covered
    elsewhere, and would hide the property under test: if the stub is ever
    dereferenced, the ordering is wrong and the test fails loudly.
    """
    return cast("Workspace", object())


def _context_stub() -> RepositoryContext:
    return cast("RepositoryContext", object())


def test_the_exit_code_is_the_documented_backend_failure_code() -> None:
    """§25. One stable code for backend failure; the message carries the cause."""
    for payload in ('{"models": "x"}', '{"models": []}', "not-json"):
        error = ReasoningModelUnavailableError("x", detail="y")
        assert error.exit_code == ExitCode.LLM_UNAVAILABLE, payload


# -- 24, 26, 27. the installed CLI, end to end --------------------------------


CLI = Path(sys.executable).parent / "repoatlas"


class _Workspace:
    """A throwaway managed root, config, index and HOME for one CLI run."""

    def __init__(self, base: Path, llm_url: str) -> None:
        self.base = base
        self.root = base / "work"
        self.repo = self.root / "demo"
        self.repo.mkdir(parents=True)
        (self.repo / "app.py").write_text(
            "UNIQUE_CLI_MARKER_918273 = 1\n\n\ndef handler():\n    return 1\n", encoding="utf-8"
        )
        init_git_repository(self.repo)
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(base),
            "REPOATLAS_CONFIG": str(base / "config.yaml"),
            "REPOATLAS_DB": str(base / "atlas.db"),
            "REPOATLAS_LLM_BASE_URL": llm_url,
        }

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(CLI), *args],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=300,
        )


@pytest.fixture
def cli_workspace(tmp_path: Path) -> Iterator[tuple[_Workspace, list]]:
    """An indexed workspace pointed at a fake Ollama that logs every request."""
    with serving(tags_body('{"models": "x"}')) as (url, seen):
        space = _Workspace(tmp_path, url)
        assert space.run("init", str(space.root)).returncode == 0
        seen.clear()
        yield space, seen


@pytest.mark.parametrize("command", [("ask",), ("debug",)])
def test_the_cli_reports_a_protocol_failure_not_an_absence(
    cli_workspace: tuple[_Workspace, list], command: tuple[str, ...]
) -> None:
    """The finding as a user meets it: exit 4, no traceback, no false advice."""
    space, seen = cli_workspace
    result = space.run(*command, "why does this fail")

    assert result.returncode == ExitCode.LLM_UNAVAILABLE, result.stdout + result.stderr
    output = result.stdout + result.stderr
    assert "Traceback (most recent call last)" not in output
    lowered = output.lower()
    for word in ABSENCE_WORDS:
        assert word not in lowered, output
    assert "not a model listing" in lowered
    paths = [path for _method, path in seen]
    assert paths == ["/api/tags"], paths


def test_the_cli_still_reports_genuine_absence(tmp_path: Path) -> None:
    """The control, through the same path: a valid empty listing still advises a pull."""
    with serving(tags_body('{"models": []}')) as (url, seen):
        space = _Workspace(tmp_path, url)
        assert space.run("init", str(space.root)).returncode == 0
        seen.clear()
        result = space.run("ask", "why does this fail")

    assert result.returncode == ExitCode.LLM_UNAVAILABLE
    output = result.stdout + result.stderr
    assert "Traceback (most recent call last)" not in output
    assert "is not installed" in output.lower()
    assert [p for _m, p in seen] == ["/api/tags"]


def test_no_llm_commands_never_contact_the_server(
    cli_workspace: tuple[_Workspace, list],
) -> None:
    """§27. Protocol validation must not make evidence-only commands depend on Ollama."""
    space, seen = cli_workspace
    for command in (("locate", "UNIQUE_CLI_MARKER_918273"), ("ask", "why", "--no-llm")):
        result = space.run(*command)
        assert result.returncode == ExitCode.OK, (command, result.stdout + result.stderr)
    assert seen == [], seen


def test_the_malformed_diagnostic_is_identical_across_runs(
    cli_workspace: tuple[_Workspace, list],
) -> None:
    """§36, at the product boundary."""
    space, _seen = cli_workspace
    outputs = {space.run("ask", "why").stdout + space.run("ask", "why").stderr for _ in range(3)}
    assert len(outputs) == 1


# -- 36. determinism ----------------------------------------------------------


def test_the_same_malformed_body_always_classifies_identically() -> None:
    """No dependence on dict ordering or set iteration."""
    payload = {"models": "x", "zzz": 1, "aaa": 2}
    results = {probe(payload)[1:] for _ in range(20)}
    assert len(results) == 1


def test_key_order_does_not_change_the_verdict() -> None:
    a = json.loads('{"models": [{"name": "foo"}, 1], "other": 2}')
    b = json.loads('{"other": 2, "models": [{"name": "foo"}, 1]}')
    assert probe(a)[1:] == probe(b)[1:]


# -- 31. the real server, when one is reachable -------------------------------


def _real_ollama() -> str | None:
    url = "http://127.0.0.1:11434"
    try:
        response = httpx.get(f"{url}/api/tags", timeout=2.0)
    except httpx.HTTPError:
        return None
    return url if response.status_code == 200 else None


@pytest.mark.skipif(_real_ollama() is None, reason="no local Ollama server")
def test_a_real_ollama_response_validates() -> None:
    """§31. The validator must accept what a real server actually sends.

    Deliberately does not require a model to be installed: an empty listing is
    a valid listing, and asserting protocol validity is the point.
    """
    url = _real_ollama()
    assert url is not None
    body = httpx.get(f"{url}/api/tags", timeout=5.0).json()
    names, problem = _installed_names(body)
    assert problem is None, f"a real Ollama response was rejected: {problem}"
    assert isinstance(names, set)


@pytest.mark.skipif(_real_ollama() is None, reason="no local Ollama server")
def test_a_real_server_never_yields_a_protocol_error() -> None:
    url = _real_ollama()
    assert url is not None
    back = backend(url, model="definitely-not-installed-918273")
    assert asyncio.run(back.available()) is False
    assert back.availability_category() is ModelFailure.MODEL_ABSENT
