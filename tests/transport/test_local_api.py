"""The local HTTP API: its contract, who may call it, and what each refusal is.

Every test talks to a real server on a real loopback socket, started on its own
thread the way ``repoatlas serve`` starts it, over the linked corpus the topology
capabilities are tested on. One test starts the command itself, as a process, for
what only a process shows: the announcement, SIGTERM, and the connection file's
lifetime.

Investigations are the part a transport is likeliest to break quietly, so both
evidence flows are driven over HTTP end to end: search, relationships, an edge and
inspect sharing one investigation, and an overview reference reaching inspect
with nothing before it.
"""

from __future__ import annotations

import http.client
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from repoatlas.api.schema import contract_schema
from repoatlas.api.server import (
    CONNECTION_FILE_ENV,
    CONTRACT,
    ENDPOINTS,
    MAX_BODY,
    STATUS_BY_CATEGORY,
    TOKEN_ENV,
    ApiConfigurationError,
    Serving,
    connection_file_path,
    loopback_address,
    resolve_token,
    serve,
)
from repoatlas.config.settings import Settings, database_path
from repoatlas.mcp.tools import TOOLS
from repoatlas.models.capability import ErrorCategory
from tests.topology_corpus import LINKED, open_corpus
from tests.transport.conftest import server_environment

TOKEN = "test-token-" + "0123456789abcdef" * 2
GLOBAL: dict[str, Any] = {"scope": "global"}
CAN_EDIT = "sym:app:src/policy.ts:canEdit:2"


def repository(slug: str) -> dict[str, Any]:
    return {"scope": "repository", "repository": slug}


# ---------------------------------------------------------------------------
# A server, and a client that controls every header
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Reply:
    status: int
    headers: Mapping[str, str]
    body: dict[str, Any]

    @property
    def result(self) -> dict[str, Any]:
        assert self.status == 200 and self.body["ok"] is True, (self.status, self.body)
        return self.body["result"]

    @property
    def error(self) -> dict[str, Any]:
        assert self.body["ok"] is False, self.body
        return self.body["error"]


def exchange(
    url: str,
    method: str,
    path: str,
    body: object = None,
    *,
    token: str | None = TOKEN,
    headers: Mapping[str, str | None] | None = None,
) -> Reply:
    """One request with exactly these headers; ``None`` in ``headers`` removes one."""
    parts = urlsplit(url)
    fields: dict[str, str] = {"Host": parts.netloc}
    if token is not None:
        fields["Authorization"] = f"Bearer {token}"
    data: bytes | None = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        fields["Content-Type"] = "application/json"
        fields["Content-Length"] = str(len(data))
    for name, value in (headers or {}).items():
        if value is None:
            fields.pop(name, None)
        else:
            fields[name] = value
    connection = http.client.HTTPConnection(parts.hostname or "", parts.port, timeout=60)
    try:
        connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for name, value in fields.items():
            connection.putheader(name, value)
        connection.endheaders(data)
        response = connection.getresponse()
        return Reply(
            response.status,
            {name.lower(): value for name, value in response.getheaders()},
            json.loads(response.read()),
        )
    finally:
        connection.close()


def post(serving: Serving, name: str, body: object, **options: Any) -> Reply:
    return exchange(serving.url, "POST", f"/v1/{name}", body, **options)


@contextmanager
def running(settings: Settings, connection_file: Path, token: str = TOKEN) -> Iterator[Serving]:
    """``serve`` on its own thread, as the command runs it, stopped on the way out."""
    started: queue.Queue[Serving | BaseException] = queue.Queue()

    def main() -> None:
        try:
            serve(
                port=0,
                settings=settings,
                token=token,
                connection_file=connection_file,
                ready=started.put,
                environ={},
            )
        except BaseException as exc:  # handed to the test thread, which raises it
            started.put(exc)

    thread = threading.Thread(target=main, name="local-api", daemon=True)
    thread.start()
    serving = started.get(timeout=120)
    if isinstance(serving, BaseException):
        raise serving
    try:
        yield serving
    finally:
        serving.stop()
        thread.join(timeout=60)
        assert not thread.is_alive()


@pytest.fixture(scope="module")
def linked_settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    workspace = open_corpus(tmp_path_factory.mktemp("local-api"), LINKED)
    settings = workspace.settings
    workspace.close()
    return settings


@pytest.fixture(scope="module")
def api(linked_settings: Settings, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Serving]:
    with running(linked_settings, tmp_path_factory.mktemp("api") / "api.json") as serving:
        yield serving


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


def test_the_index_names_the_contract_and_exactly_what_the_schema_describes(api: Serving) -> None:
    index = exchange(api.url, "GET", "/v1").result
    assert index["contract"] == CONTRACT
    described = [entry["name"] for entry in contract_schema()["endpoints"]]
    assert index["endpoints"] == described == [endpoint.name for endpoint in ENDPOINTS]
    assert {"topology/overview", "topology/children", "topology/edge"} <= set(described)
    # the write and the answer check stay agent tools; topology is not one
    assert not {"sync", "verify_grounding"} & set(described)
    assert len(TOOLS) == 10
    assert not any("topology" in spec.name for spec in TOOLS)


def test_every_described_path_is_served(api: Serving) -> None:
    schema = contract_schema()
    assert schema["contract"] == CONTRACT
    for entry in schema["endpoints"]:
        reply = exchange(api.url, entry["method"], entry["path"], {})
        assert reply.status in (200, 400), (entry["path"], reply.body)
        if reply.status == 400:
            assert reply.error["code"] != "UNKNOWN_ENDPOINT"


# ---------------------------------------------------------------------------
# Who may call it
# ---------------------------------------------------------------------------


def test_a_request_without_the_token_is_refused(api: Serving) -> None:
    body = {"context": GLOBAL}
    assert post(api, "topology/overview", body).status == 200
    for reply in (
        post(api, "topology/overview", body, token=None),
        post(api, "topology/overview", body, token=TOKEN[:-1] + "x"),
        post(api, "topology/overview", body, token=TOKEN + "x"),
        post(api, "topology/overview", body, headers={"Authorization": f"Basic {TOKEN}"}),
        # without the token, an unserved path is indistinguishable from a served one
        post(api, "sync", body, token=None),
        exchange(api.url, "GET", "/v1", token=None),
    ):
        assert reply.status == 401, reply.body
        assert reply.error["code"] == "UNAUTHORIZED"
        assert reply.headers["www-authenticate"] == "Bearer"
        assert TOKEN not in json.dumps(reply.body)


def test_a_request_naming_any_other_host_is_refused(api: Serving) -> None:
    port = urlsplit(api.url).port
    assert port is not None
    for host in (f"localhost:{port}", f"LOCALHOST:{port}", f"127.0.0.1:{port}"):
        assert exchange(api.url, "GET", "/v1", headers={"Host": host}).status == 200
    for host in ("evil.example", f"evil.example:{port}", f"127.0.0.1:{port + 1}", None):
        reply = post(api, "topology/overview", {"context": GLOBAL}, headers={"Host": host})
        assert reply.status == 403, (host, reply.body)
        assert reply.error["code"] == "HOST_NOT_ALLOWED"


# ---------------------------------------------------------------------------
# What each refusal is
# ---------------------------------------------------------------------------


def test_transport_refusals_have_their_own_status(api: Serving) -> None:
    def refused(reply: Reply, status: int, code: str) -> None:
        assert (reply.status, reply.error["code"]) == (status, code), reply.body
        assert reply.error["category"] == "invalid_request"

    body = {"context": GLOBAL}
    refused(exchange(api.url, "GET", "/v1/topology/overview"), 405, "METHOD_NOT_ALLOWED")
    assert exchange(api.url, "GET", "/v1/search").headers["allow"] == "POST"
    refused(exchange(api.url, "DELETE", "/v1/search"), 405, "METHOD_NOT_ALLOWED")
    refused(exchange(api.url, "POST", "/v1", body), 405, "METHOD_NOT_ALLOWED")
    for path in ("/v1/sync", "/v1/verify_grounding", "/v1/topology", "/v1/search?x=1", "/"):
        refused(exchange(api.url, "POST", path, body), 404, "UNKNOWN_ENDPOINT")
    refused(
        post(api, "topology/overview", body, headers={"Content-Type": "text/plain"}),
        415,
        "UNSUPPORTED_MEDIA_TYPE",
    )
    refused(
        post(api, "topology/overview", body, headers={"Content-Length": str(MAX_BODY + 1)}),
        413,
        "BODY_TOO_LARGE",
    )
    refused(
        post(api, "topology/overview", body, headers={"Content-Length": None}),
        411,
        "LENGTH_REQUIRED",
    )
    refused(post(api, "topology/overview", b"{not json"), 400, "INVALID_JSON")
    refused(post(api, "topology/overview", b"[]"), 400, "INVALID_JSON")


def test_a_malformed_request_is_refused_by_field_and_its_input_is_not_echoed(
    api: Serving,
) -> None:
    reply = post(
        api,
        "topology/children",
        {"context": repository("app"), "path": "../private-marker", "limit": "many-marker"},
    )
    assert (reply.status, reply.error["code"]) == (400, "INVALID_REQUEST")
    assert {item["field"] for item in reply.error["context"]["fields"]} == {"path", "limit"}
    assert "marker" not in json.dumps(reply.body)


def test_every_error_category_has_a_status() -> None:
    assert set(STATUS_BY_CATEGORY) == set(ErrorCategory)


def test_capability_refusals_are_answered_by_their_category(api: Serving) -> None:
    overview = post(api, "topology/overview", {"context": GLOBAL}).result
    (ref,) = [
        ref for link in overview["links"] if link["kind"] == "http" for ref in link["examples"]
    ]
    cases = [
        (
            "topology/edge",
            {"context": GLOBAL, "edge_ref": "not-a-reference"},
            400,
            "EDGE_REF_INVALID",
        ),
        ("topology/edge", {"context": repository("left"), "edge_ref": ref}, 404, "EDGE_NOT_FOUND"),
        (
            "topology/edge",
            {"context": GLOBAL, "edge_ref": ref, "investigation_id": "no-such-investigation"},
            409,
            "UNKNOWN_INVESTIGATION",
        ),
        ("topology/children", {"context": GLOBAL}, 400, "INVALID_TOPOLOGY_REQUEST"),
        (
            "topology/children",
            {"context": repository("app"), "path": "no/such"},
            404,
            "PATH_NOT_FOUND",
        ),
        ("topology/children", {"context": repository("no-such-repository")}, 404, None),
    ]
    for name, body, status, code in cases:
        reply = post(api, name, body)
        assert reply.status == status, (name, body, reply.body)
        assert reply.status == STATUS_BY_CATEGORY[ErrorCategory(reply.error["category"])]
        if code is not None:
            assert reply.error["code"] == code


# ---------------------------------------------------------------------------
# Investigations, over the wire
# ---------------------------------------------------------------------------


def test_one_investigation_runs_from_search_through_an_edge_to_inspect(api: Serving) -> None:
    found = post(
        api, "search", {"context": GLOBAL, "question": "canEdit policy", "presentation": "full"}
    ).result
    investigation = found["investigation_id"]
    related = post(
        api,
        "relationships",
        {
            "context": GLOBAL,
            "target": CAN_EDIT,
            "investigation_id": investigation,
            "presentation": "full",
        },
    ).result
    assert related["investigation_id"] == investigation
    children = post(
        api,
        "topology/children",
        {"context": repository("app"), "path": "src/policy.ts", "cross_repository": True},
    ).result
    ref = next(
        link["examples"][0]
        for link in children["links"]
        if link["child"] == CAN_EDIT and link["peer"]["kind"] == "repository"
    )
    edge = post(
        api,
        "topology/edge",
        {"context": GLOBAL, "edge_ref": ref, "investigation_id": investigation},
    ).result
    assert edge["investigation_id"] == investigation
    assert edge["ledger"]["total"] >= related["ledger"]["total"] >= found["ledger"]["total"]
    evidence = edge["evidence"][0]["id"]
    inspected = post(
        api,
        "inspect",
        {
            "context": GLOBAL,
            "target": {"evidence_id": evidence},
            "investigation_id": investigation,
            "context_lines": 0,
            "presentation": "full",
        },
    ).result
    assert inspected["investigation_id"] == investigation
    assert inspected["target_evidence_status"] is not None


def test_an_overview_reference_reaches_inspect_with_nothing_before_it(api: Serving) -> None:
    overview = post(api, "topology/overview", {"context": GLOBAL}).result
    ref = next(link["examples"][0] for link in overview["links"] if link["kind"] == "http")
    edge = post(api, "topology/edge", {"context": GLOBAL, "edge_ref": ref}).result
    investigation = edge["investigation_id"]
    assert investigation and edge["ledger"]["investigation_id"] == investigation
    (item,) = edge["evidence"]
    inspected = post(
        api,
        "inspect",
        {
            "context": GLOBAL,
            "target": {"evidence_id": item["id"]},
            "investigation_id": investigation,
            "context_lines": 0,
            "presentation": "full",
        },
    ).result
    assert inspected["investigation_id"] == investigation
    assert item["text"] in "\n".join(segment["text"] for segment in inspected["segments"])


def test_a_found_file_reads_its_references_into_another_repository(api: Serving) -> None:
    found = post(api, "topology/files", {"context": GLOBAL, "query": "policy.ts"}).result
    assert "investigation_id" not in found
    first = found["files"][0]
    assert (first["repository"], first["path"]) == ("app", "src/policy.ts")
    lines = post(
        api,
        "topology/references",
        {"context": repository("app"), "path": first["path"], "cross_repository": True},
    ).result
    assert "investigation_id" not in lines
    assert CAN_EDIT in {d["key"] for d in lines["declarations"]}
    crossing = [r for r in lines["references"] if r["target"]["repository_slug"] == "contracts"]
    assert crossing and all(r["source"] == CAN_EDIT for r in crossing)


def test_a_found_name_is_a_seed_impact_traces(api: Serving) -> None:
    found = post(api, "topology/names", {"context": GLOBAL, "query": "canEdit"}).result
    assert "investigation_id" not in found
    first = found["names"][0]
    assert first["entity"]["key"] == CAN_EDIT
    assert (first["entity"]["repository_slug"], first["entity"]["path"]) == ("app", "src/policy.ts")
    traced = post(
        api, "impact", {"context": GLOBAL, "seed": first["entity"]["key"], "presentation": "full"}
    ).result
    assert (traced["seed"]["status"], traced["seed"]["entity"]) == ("resolved", CAN_EDIT)


# ---------------------------------------------------------------------------
# Starting, finding and stopping it
# ---------------------------------------------------------------------------


def test_the_connection_file_is_private_names_the_server_and_goes_with_it(
    linked_settings: Settings, tmp_path: Path
) -> None:
    path = tmp_path / "nested" / "api.json"
    with running(linked_settings, path) as serving:
        assert serving.connection_file == path
        assert path.stat().st_mode & 0o777 == 0o600
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record == {
            "contract": CONTRACT,
            "url": serving.url,
            "token": TOKEN,
            "pid": os.getpid(),
        }
        assert exchange(record["url"], "GET", "/v1", token=record["token"]).status == 200
    assert not path.exists()

    # a later server's file is not this one's to remove
    with running(linked_settings, path):
        later = {"contract": CONTRACT, "url": "http://127.0.0.1:1", "token": "t", "pid": 1}
        path.write_text(json.dumps(later), encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8")) == later


def test_the_connection_file_is_found_through_its_environment_variable(
    linked_settings: Settings, tmp_path: Path
) -> None:
    assert connection_file_path(linked_settings, {}) == (
        database_path(linked_settings).parent / "api.json"
    )
    chosen = tmp_path / "elsewhere" / "api.json"
    assert connection_file_path(linked_settings, {CONNECTION_FILE_ENV: str(chosen)}) == chosen


def test_only_loopback_is_served_and_a_weak_token_is_refused(
    linked_settings: Settings, tmp_path: Path
) -> None:
    path = tmp_path / "api.json"
    for host in ("0.0.0.0", "::", "192.168.1.10", "example.com"):  # noqa: S104 - refused
        with pytest.raises(ApiConfigurationError):
            serve(host=host, port=0, settings=linked_settings, token=TOKEN, connection_file=path)
    assert (loopback_address("localhost"), loopback_address("::1")) == ("127.0.0.1", "::1")
    for weak in ("short", "x" * 31, "x" * 31 + " ", "x" * 31 + "é"):
        with pytest.raises(ApiConfigurationError):
            resolve_token({TOKEN_ENV: weak})
        with pytest.raises(ApiConfigurationError):
            serve(port=0, settings=linked_settings, token=weak, connection_file=path)
    assert not path.exists()
    assert resolve_token({TOKEN_ENV: TOKEN}) == TOKEN
    first, second = resolve_token({}), resolve_token({})
    assert len(first) >= 32 and first != second


def test_the_command_announces_where_to_connect_and_never_the_token(tmp_path: Path) -> None:
    path = tmp_path / "run" / "api.json"
    environment = server_environment(tmp_path, **{TOKEN_ENV: TOKEN, CONNECTION_FILE_ENV: str(path)})
    process = subprocess.Popen(
        [sys.executable, "-m", "repoatlas.cli.main", "serve", "--port", "0"],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 120
        while not path.exists():
            assert process.poll() is None, process.communicate()
            assert time.monotonic() < deadline, "the server never wrote its connection file"
            time.sleep(0.05)
        record = json.loads(path.read_text(encoding="utf-8"))
        assert exchange(record["url"], "GET", "/v1", token=record["token"]).result == {
            "contract": CONTRACT,
            "endpoints": [endpoint.name for endpoint in ENDPOINTS],
        }
        process.send_signal(signal.SIGTERM)
        out, err = process.communicate(timeout=60)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    assert process.returncode == 0, err
    assert record["url"] in err and str(path) in err
    assert TOKEN not in out + err
    assert not path.exists()
