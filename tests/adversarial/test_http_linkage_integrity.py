"""HTTP linkage under adversarial repositories (brief §47, §48, §73, §74).

The parser suites prove each refusal on a string. This suite proves the same
refusals survive the whole pipeline — admission, parsing, linking, storage, search
— on repositories written to provoke them:

* a secret-bearing file that names an API URL is excluded before it is read, so it
  declares nothing, and asking about its URL answers exactly as asking about a URL
  that exists nowhere (no existence oracle);
* credentials in a URL's authority, and a token in its query string, never reach an
  entity key, an entity name or an edge;
* a repository made entirely of HTTP-shaped decoys produces no HTTP fact at all;
* a generated specification is bounded and says where it stopped.

Every fixture value is a harmless marker.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.search import SearchRequest
from repoatlas.services.init_service import initialize
from repoatlas.services.workspace import Workspace, open_workspace
from tests.conftest import init_git_repository
from tests.http_corpus import GLOBAL, corpus_settings, http_nodes, stored_http_edges

pytestmark = pytest.mark.adversarial

CANARY_HOST = "secret-canary-host.example"
QUERY_CANARY = "REPOATLAS_URL_TOKEN_CANARY_7731"
AUTHORITY_CANARY = "REPOATLAS_URL_PASSWORD_CANARY_7731"

DECOY_PY = '''\
"""Every line here looks like HTTP and none of it is."""
from mylib import FastAPI, requests as fake_requests
import requests

app = FastAPI()
cache = {}


def handlers(router, session, client, base, user_id, requests_like):
    cache.get("/hello")
    router.get("/hello")
    session.get("https://api.example.com/hello")
    client.post("https://api.example.com/hello")
    fake_requests.get("https://api.example.com/hello")
    requests_like.get("https://api.example.com/hello")
    requests.get(f"{base}/hello")
    requests.get("/users/" + user_id)
    requests.get("https://api.example.com/{}".format(user_id))
    requests.get(base)


@app.get("/hello")
def not_a_route():
    return None


def factory():
    from flask import Flask

    inner = Flask(__name__)

    @inner.route("/inner")
    def inner_route():
        return None

    return inner
'''

DECOY_TS = """\
// Every line here looks like HTTP and none of it is.
import express from "express-clone";
import axios from "axios";

const app = express();
const map = new Map<string, string>();
declare const base: string;
declare const opts: RequestInit;

export function decoys(router: { get(p: string, h: unknown): void }, id: string) {
    map.get("/hello");
    router.get("/hello", () => null);
    app.get("/hello", () => null);
    fetch(`${base}/hello`);
    fetch("/users/" + id);
    fetch("/hello", opts);
    fetch("/hello", { ...opts, method: "POST" });
    axios.get("/relative/only");
    this.fetch("/hello");
}
"""

DECOY_YAML = """\
# A pipeline file with an OpenAPI-looking shape and no signature.
paths:
  /hello:
    get:
      summary: not an operation
servers:
  - url: https://api.example.com
"""


def _write(repo: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _index(tmp_path: Path, repos: dict[str, dict[str, str]]) -> Workspace:
    root = tmp_path / "repos"
    for name, files in repos.items():
        _write(root / name, files)
        init_git_repository(root / name)
    workspace = open_workspace(settings=corpus_settings(tmp_path / "state"), require_root=False)
    initialize(workspace, root, config_path=tmp_path / "config.yaml")
    return workspace


def _all_identity_text(workspace: Workspace) -> str:
    rows = workspace.database.execute(
        "SELECT key, name, coalesce(qualified_name, '') AS qualified FROM entities"
    ).fetchall()
    return "\n".join(f"{row['key']}\n{row['name']}\n{row['qualified']}" for row in rows)


@pytest.fixture
def hostile(tmp_path: Path) -> Iterator[Workspace]:
    workspace = _index(
        tmp_path,
        {
            "svc": {
                "openapi.yaml": (
                    "openapi: 3.0.3\nservers:\n  - url: https://api.example.com\n"
                    "paths:\n  /hello:\n    get: {}\n"
                ),
                # Excluded by name before it is read: it may name an API, and it
                # still declares nothing.
                ".env": f"API_URL=https://{CANARY_HOST}/hook\n",
                ".env.production": f"API_URL=https://{CANARY_HOST}/prod\n",
                "deploy/server.pem": f"https://{CANARY_HOST}/pem\n",
            },
            "client": {
                "src/calls.ts": (
                    "export function a() {\n"
                    f'    return fetch("https://svc-user:{AUTHORITY_CANARY}@api.example.com/hello");\n'
                    "}\n"
                    "export function b() {\n"
                    f'    return fetch("https://api.example.com/hello?token={QUERY_CANARY}#frag");\n'
                    "}\n"
                ),
                "client/calls.py": (
                    "import requests\n\n\ndef c():\n"
                    f'    return requests.get("https://user:{AUTHORITY_CANARY}@api.example.com/hello")\n'
                ),
            },
            "decoys": {
                "src/decoys.py": DECOY_PY,
                "src/decoys.ts": DECOY_TS,
                "pipeline.yaml": DECOY_YAML,
                "README.md": "The API is at https://api.example.com/hello and docs at /docs.\n",
            },
        },
    )
    yield workspace
    workspace.close()


def test_an_excluded_file_declares_no_http_fact(hostile: Workspace) -> None:
    assert CANARY_HOST not in _all_identity_text(hostile)
    assert not [edge for edge in stored_http_edges(hostile) if CANARY_HOST in edge[2]]


def test_asking_about_an_excluded_url_answers_like_asking_about_nothing(hostile: Workspace) -> None:
    """No existence oracle: the excluded URL and an invented one look identical."""
    service = CapabilityService(hostile)

    def shape(question: str) -> tuple[int, tuple[str, ...], bool]:
        response = service.search(SearchRequest(question=question, context=GLOBAL))
        return (
            len(response.evidence),
            tuple(sorted(handle.key for handle in response.entities)),
            response.suppressed is not None,
        )

    excluded = shape(f"GET https://{CANARY_HOST}/hook")
    invented = shape("GET https://never-declared-anywhere.example/hook")
    assert excluded == invented


def test_credentials_in_a_url_never_reach_an_identity(hostile: Workspace) -> None:
    text = _all_identity_text(hostile)
    assert AUTHORITY_CANARY not in text
    assert "svc-user" not in text
    assert "@api.example.com" not in text


def test_a_query_token_never_reaches_an_identity_but_the_request_still_links(
    hostile: Workspace,
) -> None:
    assert QUERY_CANARY not in _all_identity_text(hostile)
    callers = {
        source
        for source, edge_type, target in stored_http_edges(hostile)
        if edge_type == "CALLS_HTTP_ENDPOINT"
        and target == "http-endpoint:GET:https://api.example.com/hello"
    }
    # Only `b` links: `a` and `c` carried credentials and were refused outright.
    assert len(callers) == 1
    assert next(iter(callers)).startswith("sym:client:src/calls.ts:b:")


def test_a_repository_of_decoys_produces_no_http_fact(hostile: Workspace) -> None:
    assert not [edge for edge in stored_http_edges(hostile) if ":decoys:" in edge[0]]


def test_every_http_node_that_exists_is_accounted_for(hostile: Workspace) -> None:
    """A closed set: nothing appears that the two honest repositories do not state."""
    assert http_nodes(hostile) == {
        "http-route:GET:/hello",
        "http-deployment:https://api.example.com",
        "http-endpoint:GET:https://api.example.com/hello",
    }


def test_a_generated_specification_is_bounded(tmp_path: Path) -> None:
    from repoatlas.parsers.http_openapi import MAX_OPERATIONS

    paths = "".join(
        f"  /generated/{index}:\n    get: {{}}\n" for index in range(MAX_OPERATIONS * 2)
    )
    workspace = _index(
        tmp_path,
        {"gen": {"openapi.yaml": f"openapi: 3.0.0\npaths:\n{paths}"}},
    )
    try:
        routes = [key for key in http_nodes(workspace) if key.startswith("http-route:")]
        assert len(routes) == MAX_OPERATIONS
    finally:
        workspace.close()
