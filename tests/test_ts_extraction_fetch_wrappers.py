"""Requests made through a request wrapper in the same file (HTTP linkage).

A typed client calls ``request("/v1/me", { auth })``, and ``request`` puts an
injected base URL in front of the path and calls an injected ``fetch``. That is an
HTTP request only when every link is proven from the file: the wrapper forwards a
parameter as the path, the transport is fetch-like, and the method is read from
literals. A function that is merely *called* ``request`` proves nothing, and
neither does a name like ``createCourse``.
"""

from __future__ import annotations

import textwrap
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from repoatlas.models.parsing import EntityType, ParsedReference
from repoatlas.parsers.typescript import TypeScriptParser
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace
from tests.conftest import fact_snapshot, git, init_git_repository, reindex_from_scratch
from tests.http_corpus import corpus_settings, http_nodes, stored_http_edges

_HTTP = {EntityType.HTTP_ROUTE, EntityType.HTTP_DEPLOYMENT, EntityType.HTTP_ENDPOINT}

IDENTITY = """\
export type FetchLike = (url: string, init?: RequestInit) => Promise<Response>;

export function createIdentityClient({ baseUrl, fetch: suppliedFetch }: Options) {
  const runtimeFetch = (globalThis as { fetch?: FetchLike }).fetch;
  const fetcher = suppliedFetch ?? runtimeFetch;
  const requestFetch: FetchLike = fetcher;
  const normalizedBaseUrl = baseUrl.replace(/\\/$/, "");

  async function request(path: string, init?: RequestInit & { auth?: Auth }) {
    const { auth, ...rest } = init ?? {};
    const url = `${normalizedBaseUrl}${path}`;
    const response = await requestFetch(url, {
      ...rest,
      headers: { Accept: "application/json", ...rest.headers },
    });
    return response.json();
  }

  return {
    async me(auth: Auth) {
      return request("/v1/me", { auth });
    },
    async startSession(auth: Auth) {
      return request("/v1/me/session", { method: "POST", auth });
    },
    async listCourses(auth: Auth) {
      return request("/v1/courses", { auth });
    },
    async getCourse(auth: Auth, courseId: string) {
      return request(`/v1/courses/${courseId}`, { auth });
    },
    async createCourse(auth: Auth, input: unknown) {
      return request("/v1/courses", { auth, body: JSON.stringify(input) });
    },
  };
}
"""

REQUESTER = """\
export function createRequester({ baseUrl, fetch: suppliedFetch }: ClientOptions) {
  const runtimeFetch = (globalThis as { fetch?: FetchLike }).fetch;
  const requestFetch: FetchLike = suppliedFetch ?? runtimeFetch;
  const normalizedBaseUrl = baseUrl.replace(/\\/$/, "");

  async function send(path: string, init?: RequestInit): Promise<unknown> {
    const response = await requestFetch(`${normalizedBaseUrl}${path}`, {
      ...init,
      headers: { Accept: "application/json", ...init?.headers },
    });
    if (!response.ok) throw errorFrom(await response.json(), response.status);
    return response.json();
  }

  return {
    request: async (schema, path, init) => schema.parse(await send(path, init)),
    requestVoid: async (path, init) => {
      await send(path, init);
    },
  };
}

export function jsonBody(value: unknown, method: "POST" | "PUT" | "PATCH" = "POST") {
  return { method, body: JSON.stringify(value) };
}
"""

LEARNING = (
    REQUESTER
    + """
export function createLearningClient(options: ClientOptions) {
  const { request, requestVoid } = createRequester(options);

  return {
    getPreferences: (auth: CallAuth) =>
      request(preferencesSchema, "/v1/me/preferences", { headers: authHeaders(auth) }),
    updatePreferences: (auth: CallAuth, input: Preferences) =>
      request(preferencesSchema, "/v1/me/preferences", {
        ...jsonBody(input, "PUT"),
        headers: authHeaders(auth),
      }),
    createCourse: (auth: CallAuth, input: CreateCourseRequest) =>
      request(createdCourseSchema, "/v1/courses", {
        ...jsonBody(input),
        headers: authHeaders(auth),
      }),
    deleteOrganization: (auth: CallAuth, organizationId: string) =>
      request(organizationSchema, `/v1/organizations/${organizationId}`, { method: "DELETE" }),
    leave: (auth: CallAuth) => requestVoid("/v1/me/membership", { method: "DELETE" }),
  };
}
"""
)


def _http(source: str, name: str = "client.ts") -> list[ParsedReference]:
    """HTTP facts, not the pending claims only the global pass can decide."""
    parsed = TypeScriptParser().parse(Path(name), textwrap.dedent(source))
    return [r for r in parsed.references if r.target_kind in _HTTP and "pending_fact" not in r.meta]


def _calls(source: str) -> set[tuple[str, str | None]]:
    return {(r.target_key, r.source_symbol) for r in _http(source)}


# -- proven -------------------------------------------------------------------------------


def test_a_nested_request_helper_over_an_injected_fetch_is_read_through() -> None:
    found = _http(IDENTITY)
    assert {(str(r.edge_type), r.target_key, r.source_symbol) for r in found} == {
        ("CALLS_HTTP_ROUTE", "GET /v1/me", "createIdentityClient.me"),
        ("CALLS_HTTP_ROUTE", "POST /v1/me/session", "createIdentityClient.startSession"),
        ("CALLS_HTTP_ROUTE", "GET /v1/courses", "createIdentityClient.listCourses"),
        # A body is not a method: `fetch` sends GET unless told otherwise.
        ("CALLS_HTTP_ROUTE", "GET /v1/courses", "createIdentityClient.createCourse"),
    }
    session = next(r for r in found if r.source_symbol == "createIdentityClient.startSession")
    # Every literal that decides it — the path and the method — is in the call.
    assert session.meta == {"source": "fetch-wrapper", "provenance": "exact"}
    assert (session.line_start, session.line_end) == (24, 24)


def test_an_injected_base_names_a_route_and_nothing_about_where_it_is_served() -> None:
    kinds = {r.target_kind for r in _http(IDENTITY)}
    assert kinds == {EntityType.HTTP_ROUTE}


def test_a_requester_factory_its_members_and_an_init_helper_in_one_file() -> None:
    found = {(r.target_key, r.source_symbol): r for r in _http(LEARNING)}
    assert set(found) == {
        ("GET /v1/me/preferences", "createLearningClient.getPreferences"),
        ("PUT /v1/me/preferences", "createLearningClient.updatePreferences"),
        ("POST /v1/courses", "createLearningClient.createCourse"),
        ("DELETE /v1/me/membership", "createLearningClient.leave"),
    }
    # "PUT" is written in the call; "POST" is the helper's default, written elsewhere.
    assert (
        found["PUT /v1/me/preferences", "createLearningClient.updatePreferences"].meta["provenance"]
        == "exact"
    )
    assert (
        found["POST /v1/courses", "createLearningClient.createCourse"].meta["provenance"]
        == "composed"
    )


def test_a_literal_base_makes_the_request_absolute() -> None:
    source = """\
    const BASE = "https://api.example.com/v2";
    function request(path: string) {
      return fetch(`${BASE}${path}`);
    }
    export function load() {
      return request("/users");
    }
    """
    assert {(str(r.edge_type), r.target_key) for r in _http(source)} == {
        ("CALLS_HTTP_ENDPOINT", "GET https://api.example.com/v2/users"),
        ("CALLS_HTTP_ROUTE", "GET /v2/users"),
    }
    assert {r.meta["provenance"] for r in _http(source)} == {"composed"}


def test_a_wrapper_of_a_wrapper_and_a_wrapper_without_init() -> None:
    source = """\
    function send(path: string, init?: RequestInit) {
      return fetch(path, init);
    }
    function request(path: string, init?: RequestInit) {
      return send(path, init);
    }
    function get(path: string) {
      return fetch(path);
    }
    export function load() {
      request("/a", { method: "PATCH" });
      get("/b");
    }
    """
    assert _calls(source) == {("PATCH /a", "load"), ("GET /b", "load")}


def test_a_proven_fetch_alias_is_read_like_fetch() -> None:
    source = """\
    const send = globalThis.fetch;
    export function load() {
      return send("/items", { method: "POST" });
    }
    """
    [found] = _http(source)
    assert (found.target_key, found.meta["source"]) == ("POST /items", "fetch")


# -- not proven ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        # A function that happens to be called `request`.
        """\
        function request(path: string) { return db.query(path); }
        export function load() { return request("/v1/me"); }
        """,
        # `fetch` bound in the file is not the global one.
        """\
        const fetch = customFetch;
        function request(path: string) { return fetch(path); }
        export function load() { return request("/v1/me"); }
        """,
        """\
        function request(path: string, fetch: Fetcher) { return fetch(path); }
        export function load() { return request("/v1/me", f); }
        """,
        # `globalThis` bound in the file is not the global object.
        """\
        const globalThis = sandbox;
        function request(path: string) { return globalThis.fetch(path); }
        export function load() { return request("/v1/me"); }
        """,
        # An injected transport with no proven fallback proves nothing.
        """\
        export function make({ fetch: supplied }: Options) {
          function request(path: string) { return supplied(path); }
          return { me: () => request("/v1/me") };
        }
        """,
        # A helper that reaches itself.
        """\
        function a(path: string) { return b(path); }
        function b(path: string) { return a(path); }
        export function load() { return a("/v1/me"); }
        """,
        # The name is bound twice where it is called.
        """\
        export function make() {
          function request(path: string) { return fetch(path); }
          const other = (request: Handler) => request("/v1/me");
          return other;
        }
        """,
        # A wrapper that overrides whatever method its caller asked for.
        """\
        function request(path: string, init?: RequestInit) {
          return fetch(path, { ...init, method: "GET" });
        }
        export function load() { return request("/v1/me", { method: "POST" }); }
        """,
        # A helper imported from elsewhere is not read from this file.
        """\
        import { request } from "./http";
        export function load() { return request("/v1/me"); }
        """,
    ],
)
def test_a_chain_that_is_not_proven_yields_nothing(source: str) -> None:
    assert _http(source) == []


def test_a_helper_that_makes_two_requests_forwards_its_path_to_neither() -> None:
    source = """\
    function request(path: string) {
      fetch(path);
      return fetch("/audit");
    }
    export function load() {
      return request("/v1/me");
    }
    """
    # Its own literal request is a direct `fetch` fact; the forwarded path is not.
    assert _calls(source) == {("GET /audit", "request")}


@pytest.mark.parametrize(
    "call",
    [
        "request(`/v1/courses/${id}`)",
        "request(path)",
        "request('v1/relative')",
        "request('/v1/x', options)",
        "request('/v1/x', { ...options })",
        "request('/v1/x', { method: verb })",
        "request('/v1/x', { method })",
        "request('/v1/x', { [key]: 'POST' })",
        "request('/v1/x', { ...unknownHelper(), auth })",
        "request(...args)",
    ],
)
def test_a_call_whose_path_or_method_cannot_be_read_yields_nothing(call: str) -> None:
    source = f"""\
    function request(path: string, init?: RequestInit) {{
      return fetch(path, init);
    }}
    export function load(id: string, path: string, options: object, verb: string, key: string) {{
      return {call};
    }}
    """
    assert _http(source) == []


def test_a_method_is_never_inferred_from_a_name() -> None:
    source = """\
    function request(path: string, init?: RequestInit) {
      return fetch(path, init);
    }
    export function make() {
      return {
        createCourse: () => request("/v1/courses"),
        getCourse: () => request("/v1/courses/current", { method: "DELETE" }),
      };
    }
    """
    assert _calls(source) == {
        ("GET /v1/courses", "make.createCourse"),
        ("DELETE /v1/courses/current", "make.getCourse"),
    }


def test_direct_fetch_axios_and_express_are_read_exactly_as_before() -> None:
    source = """\
    import axios from "axios";
    import express from "express";
    const app = express();
    app.get("/served", handler);
    export function load() {
      fetch("/direct", { method: "PUT" });
      return axios.get("https://api.example.com/absolute");
    }
    """
    assert {(r.target_key, r.meta["source"]) for r in _http(source)} == {
        ("GET /served", "express"),
        ("PUT /direct", "fetch"),
        ("GET https://api.example.com/absolute", "axios"),
        ("GET /absolute", "axios"),
    }


# -- through the index --------------------------------------------------------------------


def _index(tmp_path: Path, source: str) -> tuple[Workspace, Path]:
    from repoatlas.services.init_service import initialize

    root = tmp_path / "repos"
    (root / "web" / "src").mkdir(parents=True)
    (root / "web" / "src" / "identity-client.ts").write_text(source)
    init_git_repository(root / "web")
    workspace = open_workspace(settings=corpus_settings(tmp_path / "state"), require_root=False)
    initialize(workspace, root, config_path=tmp_path / "config.yaml")
    return workspace, root


Index = Callable[..., tuple[Workspace, Path]]


@pytest.fixture
def index(tmp_path: Path) -> Iterator[Index]:
    """``_index`` for one test; every workspace it opens is closed when the test ends."""
    opened: list[Workspace] = []

    def make(*args: Any, **kwargs: Any) -> tuple[Workspace, Path]:
        indexed = _index(tmp_path, *args, **kwargs)
        opened.append(indexed[0])
        return indexed

    try:
        yield make
    finally:
        for workspace in opened:
            workspace.close()


def _commit_and_sync(workspace: Workspace, root: Path, message: str) -> None:
    git(root / "web", "add", "-A")
    git(root / "web", "commit", "--quiet", "-m", message)
    sync(workspace, workspace.resolver().resolve(root))


def test_a_changed_or_removed_request_leaves_no_obsolete_route(
    index: Index, tmp_path: Path
) -> None:
    workspace, root = index(IDENTITY)
    prefix = "sym:web:src/identity-client.ts:createIdentityClient"
    assert (f"{prefix}.me:20", "CALLS_HTTP_ROUTE", "http-route:GET:/v1/me") in (
        stored_http_edges(workspace)
    )
    assert http_nodes(workspace) == {
        "http-route:GET:/v1/me",
        "http-route:POST:/v1/me/session",
        "http-route:GET:/v1/courses",
    }

    (root / "web/src/identity-client.ts").write_text(
        IDENTITY.replace('"/v1/me/session", { method: "POST"', '"/v1/sessions", { method: "PUT"')
    )
    _commit_and_sync(workspace, root, "move the session route")
    assert http_nodes(workspace) == {
        "http-route:GET:/v1/me",
        "http-route:PUT:/v1/sessions",
        "http-route:GET:/v1/courses",
    }
    fresh = reindex_from_scratch(corpus_settings(tmp_path / "unused"), root, tmp_path, "fresh")
    assert fact_snapshot(workspace) == fresh

    git(root / "web", "rm", "--quiet", "src/identity-client.ts")
    _commit_and_sync(workspace, root, "remove the client")
    assert http_nodes(workspace) == set()
    assert stored_http_edges(workspace) == set()
