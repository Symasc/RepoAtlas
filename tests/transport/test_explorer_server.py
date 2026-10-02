"""The local API sending the exported explorer, and the door its pages call through.

An installed RepoAtlas has no Node, so ``repoatlas ui`` starts the local API with
the exported explorer, and the API sends the pages and answers their calls at
``/api/ra/<name>`` without the token. That door must admit exactly what the
explorer's own server admits (``frontend/lib/server/proxy.ts``), and the files
must come from the export and nowhere else. ``/v1`` must not change at all.

The export here is a stand-in with the same layout as ``pnpm build:static``
writes, beside a file outside it that no request may read.
"""

from __future__ import annotations

import http.client
import json
import socket
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from repoatlas.api.server import MAX_BODY, TOKEN_ENV, ApiConfigurationError, Serving, serve
from repoatlas.config.settings import Settings
from tests.topology_corpus import LINKED, open_corpus
from tests.transport.test_local_api import TOKEN, exchange, running

OUTSIDE = "outside the export"
MISSING = b"<!doctype html><title>missing</title>"
EVERYWHERE = {"context": {"scope": "global"}}


@dataclass(frozen=True)
class Raw:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def envelope(self) -> dict[str, Any]:
        assert self.headers["content-type"].startswith("application/json"), self.headers
        return json.loads(self.body)

    @property
    def code(self) -> str:
        envelope = self.envelope()
        assert envelope["ok"] is False, envelope
        return envelope["error"]["code"]


def fetch(
    url: str,
    method: str,
    path: str,
    body: object = None,
    *,
    headers: Mapping[str, str | None] | None = None,
    timeout: float = 60,
) -> Raw:
    """One request for exactly ``path``, unnormalised; ``None`` in ``headers`` removes one."""
    parts = urlsplit(url)
    fields: dict[str, str] = {"Host": parts.netloc}
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
    connection = http.client.HTTPConnection(parts.hostname or "", parts.port, timeout=timeout)
    try:
        connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for name, value in fields.items():
            connection.putheader(name, value)
        connection.endheaders(data)
        response = connection.getresponse()
        return Raw(
            response.status,
            {name.lower(): value for name, value in response.getheaders()},
            response.read(),
        )
    finally:
        connection.close()


def from_the_page(serving: Serving) -> dict[str, str | None]:
    """What a browser sends with a ``fetch`` from a page this server sent."""
    return {"Origin": serving.url, "Sec-Fetch-Site": "same-origin"}


def door(serving: Serving, name: str, body: object = None) -> Raw:
    """A call as the explorer's pages make one."""
    sent = from_the_page(serving)
    sent_body = EVERYWHERE if body is None else body
    return fetch(serving.url, "POST", f"/api/ra/{name}", sent_body, headers=sent)


def export(root: Path) -> Path:
    """The layout ``pnpm build:static`` writes, and a secret beside it."""
    (root / "secret.txt").write_text(OUTSIDE)
    out = root / "out"
    files = {
        "index.html": "<!doctype html><title>explorer</title>",
        "404.html": MISSING.decode(),
        "_not-found.html": "<!doctype html><title>not found page</title>",
        "index.txt": "0:rsc",
        "icon.svg": "<svg/>",
        "docs/index.html": "<!doctype html><title>docs</title>",
        "_next/static/chunks/app.js": "console.log(1)",
        "_next/static/media/plex.woff2": "wOF2",
    }
    for name, text in files.items():
        (out / name).parent.mkdir(parents=True, exist_ok=True)
        (out / name).write_text(text)
    (out / "leak.txt").symlink_to(root / "secret.txt")
    (out / "_next" / "outside").symlink_to(root)
    return out


@pytest.fixture(scope="module")
def linked_settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    workspace = open_corpus(tmp_path_factory.mktemp("explorer-corpus"), LINKED)
    settings = workspace.settings
    workspace.close()
    return settings


@pytest.fixture(scope="module")
def site(linked_settings: Settings, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Serving]:
    root = tmp_path_factory.mktemp("explorer")
    with running(linked_settings, root / "api.json", explorer=export(root)) as serving:
        yield serving


# ---------------------------------------------------------------------------
# The pages
# ---------------------------------------------------------------------------


def test_the_pages_are_sent_with_their_types_caching_and_the_frontends_headers(
    site: Serving,
) -> None:
    page = fetch(site.url, "GET", "/")
    assert page.status == 200 and page.body == b"<!doctype html><title>explorer</title>"
    assert page.headers["content-type"] == "text/html; charset=utf-8"
    assert page.headers["cache-control"] == "no-cache"
    assert page.headers["x-frame-options"] == "DENY"
    assert page.headers["referrer-policy"] == "no-referrer"
    assert page.headers["x-content-type-options"] == "nosniff"

    script = fetch(site.url, "GET", "/_next/static/chunks/app.js")
    assert script.status == 200 and script.body == b"console.log(1)"
    assert script.headers["content-type"] == "text/javascript; charset=utf-8"
    assert script.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert fetch(site.url, "GET", "/_next/static/media/plex.woff2").headers["content-type"] == (
        "font/woff2"
    )
    assert fetch(site.url, "GET", "/icon.svg").headers["content-type"] == "image/svg+xml"
    assert fetch(site.url, "GET", "/index.txt").headers["content-type"] == (
        "text/plain; charset=utf-8"
    )

    head = fetch(site.url, "HEAD", "/")
    assert head.status == 200 and head.body == b""
    assert head.headers["content-length"] == str(len(page.body))


def test_a_path_names_a_page_the_way_an_export_lays_them_out(site: Serving) -> None:
    assert fetch(site.url, "GET", "/?from=link").body == fetch(site.url, "GET", "/").body
    assert fetch(site.url, "GET", "/docs").body == b"<!doctype html><title>docs</title>"
    assert fetch(site.url, "GET", "/docs/").body == b"<!doctype html><title>docs</title>"
    assert b"not found page" in fetch(site.url, "GET", "/_not-found").body
    missing = fetch(site.url, "GET", "/no/such/page")
    assert missing.status == 404 and missing.body == MISSING
    assert missing.headers["content-type"] == "text/html; charset=utf-8"


@pytest.mark.parametrize(
    "path",
    [
        "/../secret.txt",
        "/%2e%2e/secret.txt",
        "/_next/..%2f..%2fsecret.txt",
        "/_next/static/../../../secret.txt",
        "//secret.txt",
        "/.%5c..%5csecret.txt",
        "/_next/outside/secret.txt",
        "/leak.txt",
        "/index.html%00.txt",
        "secret.txt",
    ],
)
def test_no_path_reads_a_file_outside_the_export(site: Serving, path: str) -> None:
    answer = fetch(site.url, "GET", path)
    assert answer.status == 404, (path, answer.status)
    assert OUTSIDE.encode() not in answer.body


def test_a_page_path_answers_only_get_and_head(site: Serving) -> None:
    answer = fetch(site.url, "POST", "/", {})
    assert answer.status == 405 and answer.code == "METHOD_NOT_ALLOWED"
    assert answer.headers["allow"] == "GET, HEAD"


def test_a_page_for_another_host_is_refused(site: Serving) -> None:
    port = urlsplit(site.url).port
    answer = fetch(site.url, "GET", "/", headers={"Host": f"rebound.example:{port}"})
    assert answer.status == 403 and answer.code == "HOST_NOT_ALLOWED"


# ---------------------------------------------------------------------------
# The door
# ---------------------------------------------------------------------------


def test_the_door_answers_the_pages_without_the_token(site: Serving) -> None:
    answer = door(site, "repositories")
    assert answer.status == 200, answer.body
    envelope = answer.envelope()
    assert envelope["ok"] is True and envelope["result"]["repositories"]
    assert answer.headers["cache-control"] == "no-store"
    assert answer.headers["x-frame-options"] == "DENY"
    # A request with no browser headers at all is a local process, as at the proxy.
    bare = fetch(site.url, "POST", "/api/ra/status", EVERYWHERE)
    assert bare.status == 200, bare.body
    nested = door(site, "topology/overview")
    assert nested.status == 200, nested.body


@pytest.mark.parametrize(
    ("headers", "status", "code"),
    [
        ({"Host": "rebound.example:{port}"}, 403, "HOST_NOT_ALLOWED"),
        ({"Sec-Fetch-Site": "cross-site"}, 403, "CROSS_SITE_REQUEST"),
        ({"Sec-Fetch-Site": "same-site"}, 403, "CROSS_SITE_REQUEST"),
        ({"Origin": "http://rebound.example:{port}"}, 403, "CROSS_SITE_REQUEST"),
        ({"Origin": "null"}, 403, "CROSS_SITE_REQUEST"),
        ({"Origin": "https://127.0.0.1:{port}"}, 403, "CROSS_SITE_REQUEST"),
        # the right port on another loopback name is still another origin
        ({"Origin": "http://localhost:{port}"}, 403, "CROSS_SITE_REQUEST"),
        ({"Content-Type": "text/plain"}, 415, "UNSUPPORTED_MEDIA_TYPE"),
        ({"Content-Length": str(MAX_BODY + 1)}, 413, "BODY_TOO_LARGE"),
    ],
)
def test_the_door_refuses_what_a_page_sent_from_here_could_not_send(
    site: Serving, headers: dict[str, str], status: int, code: str
) -> None:
    port = urlsplit(site.url).port
    sent = {name: value.format(port=port) for name, value in headers.items()}
    answer = fetch(
        site.url, "POST", "/api/ra/repositories", {}, headers={**from_the_page(site), **sent}
    )
    assert (answer.status, answer.code) == (status, code)
    assert answer.headers["x-frame-options"] == "DENY"


@pytest.mark.parametrize("name", ["sync", "verify_grounding", "nothing", "", "../v1/search"])
def test_the_door_opens_only_onto_what_the_contract_serves(site: Serving, name: str) -> None:
    answer = door(site, name)
    assert (answer.status, answer.code) == (404, "UNKNOWN_ENDPOINT")


def test_the_door_answers_post_only(site: Serving) -> None:
    answer = fetch(site.url, "GET", "/api/ra/repositories", headers=from_the_page(site))
    assert (answer.status, answer.code) == (405, "METHOD_NOT_ALLOWED")


def test_a_capability_refusal_comes_through_the_door_unchanged(site: Serving) -> None:
    answer = door(site, "search", {"query": 7})
    assert (answer.status, answer.code) == (400, "INVALID_REQUEST")


# ---------------------------------------------------------------------------
# The server around them
# ---------------------------------------------------------------------------


def test_v1_still_needs_the_token_when_the_explorer_is_sent(site: Serving) -> None:
    assert exchange(site.url, "GET", "/v1", token=None).status == 401
    assert exchange(site.url, "GET", "/v1").status == 200
    assert exchange(site.url, "POST", "/v1/repositories", {}, token=None).status == 401


def test_calls_from_many_connections_at_once_all_reach_the_index(site: Serving) -> None:
    """Each connection is read on its own thread; the index answers on its own one."""
    with ThreadPoolExecutor(max_workers=8) as pool:
        answers = list(pool.map(lambda _: door(site, "repositories"), range(24)))
    assert [answer.status for answer in answers] == [200] * 24
    assert len({answer.body for answer in answers}) == 1


def test_a_connection_that_sends_nothing_holds_up_no_one(site: Serving) -> None:
    """A browser opens spare connections it may never use; the server must not wait on one.

    The idle connection is accepted first. Read one at a time, it would hold the
    server for the 30-second socket timeout, past this request's own 10.
    """
    parts = urlsplit(site.url)
    with socket.create_connection((parts.hostname or "", parts.port or 0)):
        assert fetch(site.url, "GET", "/", timeout=10).status == 200
        assert door(site, "repositories").status == 200


def test_serve_refuses_a_directory_that_is_not_an_export(
    linked_settings: Settings, tmp_path: Path
) -> None:
    with pytest.raises(ApiConfigurationError) as refused:
        serve(port=0, settings=linked_settings, explorer=tmp_path, environ={TOKEN_ENV: TOKEN})
    assert "not an exported explorer" in refused.value.message
