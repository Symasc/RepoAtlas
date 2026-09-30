"""The local HTTP transport. A translator, like the MCP adapter, over loopback HTTP.

Three translations happen here and no fourth:

    a POST body          ->  a V2 request model
    a V2 response model  ->  ``{"ok": true, "result": ...}``
    a refusal            ->  ``{"ok": false, "error": CapabilityError}`` and a status

Who it is for
-------------
A local app's server process. The RepoAtlas frontend's server reads the connection
file, holds the token, and calls this API on its browser's behalf; the browser
never holds the token and never talks to this port. ``docs/local-api.md`` is the
contract.

Why one thread
--------------
The index's database connection is bound to the thread that opened it, and V2 was
designed around one service driven sequentially. So the service is opened on the
thread that serves, and requests are answered one at a time, in arrival order, by
a single-threaded stdlib server. Every connection carries a socket timeout, so one
stalled client cannot hold the port.

Why a token and a Host check on loopback
----------------------------------------
Loopback is reachable by every process on this machine, and by any web page the
user opens, through DNS rebinding. The token answers the first. The Host
allowlist answers the second: a rebound page's request still names the page's own
host, never this server's loopback address.

Why sync and verify_grounding are not served
--------------------------------------------
Sync is the one write, and a page has no business starting one. Verification
checks an agent's answer against its investigation, and no answer is written
here. Both stay agent tools; this transport serves reads.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import os
import secrets
import signal
import socket
import socketserver
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, ValidationError

from repoatlas.capabilities.errors import capability_error
from repoatlas.capabilities.presentation import present
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings, database_path, load_settings
from repoatlas.errors import ExitCode, RepoAtlasError
from repoatlas.models.capability import CapabilityError, ErrorCategory
from repoatlas.models.capability_io import (
    RepositoriesRequest,
    RepositoriesResponse,
    StatusRequest,
    StatusResponse,
)
from repoatlas.models.dependency_inventory import DependenciesRequest, DependenciesResponse
from repoatlas.models.http_inventory import HttpOperationsRequest, HttpOperationsResponse
from repoatlas.models.impact import ImpactRequest, ImpactResponse
from repoatlas.models.inspection import InspectRequest, InspectResponse
from repoatlas.models.relationships import RelationshipsRequest, RelationshipsResponse
from repoatlas.models.search import SearchRequest, SearchResponse
from repoatlas.models.topology import (
    TopologyChildrenRequest,
    TopologyChildrenResponse,
    TopologyEdgeRequest,
    TopologyEdgeResponse,
    TopologyOverviewRequest,
    TopologyOverviewResponse,
)

#: The contract a client checks before it trusts anything else in a response.
CONTRACT = "repoatlas-local-api/1"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7317
#: Every request model fits in far less. A larger body is refused unread.
MAX_BODY = 1 << 20
#: Seconds a connection may sit silent before it is dropped.
SOCKET_TIMEOUT = 30.0
TOKEN_ENV = "REPOATLAS_API_TOKEN"  # noqa: S105 - a variable's name, not a secret
#: How a local app's server finds this one: the path of the connection file.
CONNECTION_FILE_ENV = "REPOATLAS_API_CONNECTION_FILE"
MIN_TOKEN_LENGTH = 32
#: Owner read and write, nothing for anyone else: the file holds the token.
CONNECTION_FILE_MODE = 0o600
#: The most of a connection file read back before removing it; a record is ~150 bytes.
CONNECTION_FILE_CEILING = 4096
PREFIX = "/v1"

#: The names a server may be started on, and the address each one binds.
_LOOPBACK = {"127.0.0.1": "127.0.0.1", "localhost": "127.0.0.1", "::1": "::1"}


# ---------------------------------------------------------------------------
# What is served
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Endpoint:
    """One path bound to one capability, and nothing in between.

    ``response`` is the full response model. It is what the schema dump
    describes, and it is what a request that asks for the full presentation
    receives.
    """

    name: str
    request: type[BaseModel]
    response: type[BaseModel]
    #: Attribute name on :class:`CapabilityService`, looked up per call.
    capability: str

    @property
    def path(self) -> str:
        return f"{PREFIX}/{self.name}"


#: The single registry: the server routes with it and the schema dump describes it.
ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint("repositories", RepositoriesRequest, RepositoriesResponse, "repositories"),
    Endpoint("status", StatusRequest, StatusResponse, "status"),
    Endpoint("search", SearchRequest, SearchResponse, "search"),
    Endpoint("inspect", InspectRequest, InspectResponse, "inspect"),
    Endpoint("relationships", RelationshipsRequest, RelationshipsResponse, "relationships"),
    Endpoint("impact", ImpactRequest, ImpactResponse, "impact"),
    Endpoint("dependencies", DependenciesRequest, DependenciesResponse, "dependencies"),
    Endpoint("http_operations", HttpOperationsRequest, HttpOperationsResponse, "http_operations"),
    Endpoint(
        "topology/overview",
        TopologyOverviewRequest,
        TopologyOverviewResponse,
        "topology_overview",
    ),
    Endpoint(
        "topology/children",
        TopologyChildrenRequest,
        TopologyChildrenResponse,
        "topology_children",
    ),
    Endpoint("topology/edge", TopologyEdgeRequest, TopologyEdgeResponse, "topology_edge"),
)
ENDPOINTS_BY_PATH: Mapping[str, Endpoint] = {endpoint.path: endpoint for endpoint in ENDPOINTS}

#: The status a capability refusal is answered with, by its category.
STATUS_BY_CATEGORY: Mapping[ErrorCategory, int] = {
    ErrorCategory.CONTEXT: 400,
    ErrorCategory.INVALID_REQUEST: 400,
    ErrorCategory.NOT_FOUND: 404,
    ErrorCategory.SESSION: 409,
    ErrorCategory.CONTENTION: 503,
    ErrorCategory.INDEX: 503,
    ErrorCategory.INTERNAL: 500,
}


class ApiIndex(BaseModel):
    """What ``GET /v1`` answers: the contract, and the names it serves."""

    model_config = ConfigDict(frozen=True)

    contract: str
    endpoints: tuple[str, ...]


class ErrorEnvelope(BaseModel):
    """Every refusal, from the transport or from a capability, in one shape."""

    model_config = ConfigDict(frozen=True)

    ok: Literal[False] = False
    error: CapabilityError


class ApiConfigurationError(RepoAtlasError):
    """The server cannot start as asked: a host off loopback, a weak token, a taken port.

    Exit code 2, the one the CLI already uses for a request it cannot act on.
    """

    exit_code = ExitCode.CONTEXT


# ---------------------------------------------------------------------------
# Answering one request
# ---------------------------------------------------------------------------


class Headers(Protocol):
    def get(self, name: str, /) -> str | None: ...


@dataclass(frozen=True, slots=True)
class Answer:
    status: int
    payload: dict[str, Any]
    headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ServerState:
    service: CapabilityService
    token: bytes
    hosts: frozenset[str]


class _Refusal(Exception):
    """A request the transport answers itself, before any capability sees it."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        context: dict[str, object] | None = None,
        headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        super().__init__(message)
        self.status = status
        self.error = CapabilityError(
            code=code,
            category=ErrorCategory.INVALID_REQUEST,
            message=message,
            retryable=False,
            exit_code=ExitCode.CONTEXT,
            context=context or {},
        )
        self.headers = headers


def respond(
    state: ServerState,
    method: str,
    target: str,
    headers: Headers,
    read: Callable[[int], bytes],
) -> Answer:
    """One request, answered by one capability call or by one refusal."""
    try:
        endpoint = _admit(state, method, target, headers)
        if endpoint is None:
            index = ApiIndex(contract=CONTRACT, endpoints=tuple(e.name for e in ENDPOINTS))
            return Answer(200, {"ok": True, "result": index.model_dump(mode="json")})
        arguments = _arguments(headers, read)
    except _Refusal as refusal:
        return _failure(refusal.status, refusal.error, refusal.headers)
    try:
        request = endpoint.request.model_validate(arguments)
    except ValidationError as exc:
        return _failure(400, _invalid_request(endpoint, exc))
    try:
        # The arguments reach the capability exactly as they arrived: an
        # investigation_id is the caller's, and nothing here starts, ends or
        # rewrites one.
        payload = present(request, getattr(state.service, endpoint.capability)(request))
    except Exception as exc:  # every refusal crosses the wire as data, never as a raise
        error = capability_error(exc)
        return _failure(STATUS_BY_CATEGORY[error.category], error)
    return Answer(200, {"ok": True, "result": payload})


def _admit(state: ServerState, method: str, target: str, headers: Headers) -> Endpoint | None:
    """The endpoint a request may reach, ``None`` for the index, or a refusal.

    Host first, then the token, then the route: a request from anything but this
    server's own name learns nothing, and one without the token cannot tell a
    served path from an unserved one.
    """
    host = (headers.get("Host") or "").strip().lower()
    if host not in state.hosts:
        raise _Refusal(
            403,
            "HOST_NOT_ALLOWED",
            "the Host header does not name this server's loopback address",
        )
    if not _authorized(state.token, headers.get("Authorization")):
        raise _Refusal(
            401,
            "UNAUTHORIZED",
            "a valid bearer token is required",
            headers=(("WWW-Authenticate", "Bearer"),),
        )
    if target == PREFIX:
        if method != "GET":
            raise _method_not_allowed("GET")
        return None
    endpoint = ENDPOINTS_BY_PATH.get(target)
    if endpoint is None:
        raise _Refusal(
            404,
            "UNKNOWN_ENDPOINT",
            "no such endpoint",
            context={"available": [e.path for e in ENDPOINTS]},
        )
    if method != "POST":
        raise _method_not_allowed("POST")
    return endpoint


def _authorized(token: bytes, header: str | None) -> bool:
    scheme, _, given = (header or "").partition(" ")
    if scheme.lower() != "bearer":
        return False
    # Headers arrive decoded as Latin-1, so every character encodes back.
    return hmac.compare_digest(given.strip().encode("latin-1"), token)


def _method_not_allowed(allowed: str) -> _Refusal:
    return _Refusal(
        405,
        "METHOD_NOT_ALLOWED",
        f"this path answers {allowed} only",
        headers=(("Allow", allowed),),
    )


def _arguments(headers: Headers, read: Callable[[int], bytes]) -> dict[str, Any]:
    """The body as a JSON object, or a refusal naming what is wrong with it."""
    media = (headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if media != "application/json":
        raise _Refusal(415, "UNSUPPORTED_MEDIA_TYPE", "the body must be application/json")
    raw_length = headers.get("Content-Length")
    if raw_length is None or headers.get("Transfer-Encoding") is not None:
        raise _Refusal(411, "LENGTH_REQUIRED", "the body must be sent with a Content-Length")
    try:
        length = int(raw_length)
    except ValueError:
        length = -1
    if length < 0:
        raise _Refusal(400, "INVALID_BODY", "Content-Length is not a length")
    if length > MAX_BODY:
        raise _Refusal(
            413,
            "BODY_TOO_LARGE",
            f"the body is larger than {MAX_BODY} bytes",
            context={"max_bytes": MAX_BODY},
        )
    try:
        arguments = json.loads(read(length))
    except ValueError:
        raise _Refusal(400, "INVALID_JSON", "the body is not JSON") from None
    if not isinstance(arguments, dict):
        raise _Refusal(400, "INVALID_JSON", "the body must be a JSON object")
    return arguments


def _invalid_request(endpoint: Endpoint, exc: ValidationError) -> CapabilityError:
    """A malformed request, reported by field; the offending input is never echoed."""
    fields = [
        {
            "field": ".".join(str(part) for part in error["loc"]) or "<root>",
            "error": error["msg"],
        }
        for error in exc.errors()
    ]
    return CapabilityError(
        code="INVALID_REQUEST",
        category=ErrorCategory.INVALID_REQUEST,
        message=f"the body does not form a valid {endpoint.name} request",
        detail="; ".join(f"{item['field']}: {item['error']}" for item in fields),
        retryable=False,
        exit_code=ExitCode.CONTEXT,
        context={"endpoint": endpoint.name, "fields": fields},
    )


def _failure(
    status: int, error: CapabilityError, headers: tuple[tuple[str, str], ...] = ()
) -> Answer:
    return Answer(status, ErrorEnvelope(error=error).model_dump(mode="json"), headers)


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    """Reads one request, answers it, closes the connection (HTTP/1.0)."""

    timeout = SOCKET_TIMEOUT

    def version_string(self) -> str:
        return "repoatlas"

    def do_GET(self) -> None:
        state = cast("_Server", self.server).state
        self._write(respond(state, self.command, self.path, self.headers, self.rfile.read))

    # Every other common method reaches the same admission, and is refused there.
    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = do_GET

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """Malformed HTTP itself, answered in the same envelope as everything else."""
        self.close_connection = True
        error = _Refusal(code, "MALFORMED_HTTP", message or HTTPStatus(code).phrase).error
        self._write(_failure(code, error))

    def log_message(self, format: str, *args: Any) -> None:
        """Silent: requests carry repository text, and the operator's terminal is not a log."""

    def _write(self, answer: Answer) -> None:
        body = json.dumps(answer.payload, separators=(",", ":"), ensure_ascii=False).encode()
        self.send_response(answer.status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in answer.headers:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


class _Server(HTTPServer):
    state: ServerState

    def __init__(self, address: tuple[str, int], family: socket.AddressFamily) -> None:
        self.address_family = family
        super().__init__(address, _Handler)

    def server_bind(self) -> None:
        # HTTPServer would look its own name up in DNS; loopback needs no lookup.
        socketserver.TCPServer.server_bind(self)
        self.server_name = str(self.server_address[0])
        self.server_port = int(self.server_address[1])

    def handle_error(self, request: Any, client_address: Any) -> None:
        # A client that went away mid-answer is not the server's failure.
        if isinstance(sys.exc_info()[1], OSError):
            return
        super().handle_error(request, client_address)


@dataclass(frozen=True, slots=True)
class Serving:
    """A running server, as :func:`serve` hands it to its ``ready`` callback."""

    url: str
    connection_file: Path
    #: Stops the server from another thread; :func:`serve` then cleans up and returns.
    stop: Callable[[], None]


def loopback_address(host: str) -> str:
    """The address to bind for ``host``, which must name loopback."""
    address = _LOOPBACK.get(host.strip().lower())
    if address is None:
        raise ApiConfigurationError(
            f"refusing to listen on {host!r}: the local API serves loopback only",
            detail="Use 127.0.0.1, ::1 or localhost.",
        )
    return address


def resolve_token(environ: Mapping[str, str]) -> str:
    """``REPOATLAS_API_TOKEN`` when it is set, otherwise a fresh random token."""
    supplied = environ.get(TOKEN_ENV)
    if not supplied:
        return secrets.token_urlsafe(32)
    return checked_token(supplied)


def checked_token(token: str) -> str:
    if (
        len(token) < MIN_TOKEN_LENGTH
        or not (token.isascii() and token.isprintable())
        or any(character.isspace() for character in token)
    ):
        raise ApiConfigurationError(
            f"{TOKEN_ENV} must be at least {MIN_TOKEN_LENGTH} printable ASCII characters "
            "with no spaces",
            detail=f"Unset {TOKEN_ENV} to have a random token generated.",
        )
    return token


def connection_file_path(settings: Settings, environ: Mapping[str, str]) -> Path:
    """``REPOATLAS_API_CONNECTION_FILE``, or ``api.json`` beside the index."""
    override = environ.get(CONNECTION_FILE_ENV)
    if override:
        return Path(override).expanduser()
    return database_path(settings).parent / "api.json"


def write_connection_file(path: Path, record: Mapping[str, object]) -> None:
    """Replace ``path`` with ``record`` atomically, readable by its owner only."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=path.parent, prefix=".api-", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(CONNECTION_FILE_MODE)
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def remove_connection_file(path: Path, record: Mapping[str, object]) -> None:
    """Remove ``path`` if it still holds ``record``; a later server's file is left.

    Read under a ceiling: the path may be the operator's, and anything larger
    than a record is not this server's file.
    """
    try:
        with path.open("rb") as stream:
            raw = stream.read(CONNECTION_FILE_CEILING + 1)
        current = json.loads(raw) if len(raw) <= CONNECTION_FILE_CEILING else None
    except (OSError, ValueError):
        return
    if current == dict(record):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def serve(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    settings: Settings | None = None,
    token: str | None = None,
    connection_file: Path | None = None,
    ready: Callable[[Serving], None] | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Serve the capabilities on loopback until stopped, then clean up once.

    Everything that could refuse to start is checked before anything is opened:
    the host, then the token. The port is bound before the index is opened, so a
    taken port touches nothing. The service is opened on this thread and every
    request is answered on it. On the way out, in reverse: SIGTERM restored, the
    connection file removed if it is still ours, the service closed, the socket
    closed.
    """
    environment = os.environ if environ is None else environ
    address = loopback_address(host)
    secret = checked_token(token) if token is not None else resolve_token(environment)
    resolved = settings if settings is not None else load_settings()
    target = connection_file or connection_file_path(resolved, environment)
    family = socket.AF_INET6 if ":" in address else socket.AF_INET

    with contextlib.ExitStack() as cleanup:
        try:
            server = _Server((address, port), family)
        except OSError as exc:
            raise ApiConfigurationError(
                f"cannot listen on {host} port {port}", detail=str(exc)
            ) from exc
        cleanup.callback(server.server_close)
        service = CapabilityService.open(settings=resolved, require_root=False)
        cleanup.callback(service.close)
        bound = server.server_port
        server.state = ServerState(
            service=service,
            token=secret.encode("ascii"),
            hosts=frozenset({f"127.0.0.1:{bound}", f"localhost:{bound}", f"[::1]:{bound}"}),
        )
        authority = f"[{address}]" if family is socket.AF_INET6 else address
        url = f"http://{authority}:{bound}"
        record = {"contract": CONTRACT, "url": url, "token": secret, "pid": os.getpid()}
        write_connection_file(target, record)
        cleanup.callback(remove_connection_file, target, record)
        cleanup.enter_context(_sigterm_stops())
        if ready is not None:
            ready(Serving(url=url, connection_file=target, stop=server.shutdown))
        with contextlib.suppress(KeyboardInterrupt):
            server.serve_forever(poll_interval=0.2)


@contextlib.contextmanager
def _sigterm_stops() -> Iterator[None]:
    """On the main thread, SIGTERM stops the server the way Ctrl-C does."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def interrupt(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)
