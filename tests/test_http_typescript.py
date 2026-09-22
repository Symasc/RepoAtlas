"""Static HTTP facts in TypeScript and JavaScript source (HTTP linkage).

JavaScript is where "match the method name" fails worst: ``map.get``,
``cache.get``, ``app.get("title")`` (an Express settings *getter*) and
``app.get("/hello", handler)`` share one spelling, and only the last is a route.
So each receiver here must be proven by the file's own imports and ``const``
declarations, and each value must be literal. What is refused is pinned as
carefully as what is found.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from repoatlas.models.parsing import EdgeType, EntityType, ParsedFile, ParsedReference
from repoatlas.parsers.typescript import TypeScriptParser

_HTTP_KINDS = {EntityType.HTTP_ROUTE, EntityType.HTTP_DEPLOYMENT, EntityType.HTTP_ENDPOINT}

EXPRESS = 'import express from "express";\nconst app = express();\n'
AXIOS = 'import axios from "axios";\n'


def _parse(source: str, name: str = "api.ts") -> ParsedFile:
    return TypeScriptParser().parse(Path(name), textwrap.dedent(source))


def _http(source: str, name: str = "api.ts") -> list[ParsedReference]:
    """HTTP facts, not the pending claims only the global pass can decide."""
    return [
        r
        for r in _parse(source, name).references
        if r.target_kind in _HTTP_KINDS and "pending_fact" not in r.meta
    ]


def _claims(source: str, name: str = "api.ts") -> list[ParsedReference]:
    return [r for r in _parse(source, name).references if "pending_fact" in r.meta]


def _pairs(source: str, name: str = "api.ts") -> set[tuple[str, str]]:
    return {(str(r.edge_type), r.target_key) for r in _http(source, name)}


# -- Express ---------------------------------------------------------------------------


def test_an_express_route_is_served_at_the_call() -> None:
    [reference] = _http(EXPRESS + 'app.get("/hello", (req, res) => {\n  res.send("hi");\n});\n')
    assert reference.edge_type is EdgeType.SERVES_HTTP_ROUTE
    assert reference.target_key == "GET /hello"
    assert (reference.line_start, reference.line_end) == (3, 3)
    assert reference.source_symbol is None
    assert reference.meta == {"source": "express", "provenance": "exact"}


@pytest.mark.parametrize(
    "header",
    [
        'const express = require("express");\nconst app = express();\n',
        'import express from "express";\nexport const app = express();\n',
        "import express from 'express';\nconst app = express({ strict: true });\n",
    ],
)
def test_the_express_application_may_be_spelled_several_ways(header: str) -> None:
    assert _pairs(header + "app.post('/items', handler);\n", "server.js") == {
        ("SERVES_HTTP_ROUTE", "POST /items")
    }


@pytest.mark.parametrize(
    ("verb", "method"),
    [
        ("get", "GET"),
        ("post", "POST"),
        ("put", "PUT"),
        ("patch", "PATCH"),
        ("delete", "DELETE"),
        ("head", "HEAD"),
        ("options", "OPTIONS"),
        ("trace", "TRACE"),
    ],
)
def test_every_express_verb_maps_to_its_method(verb: str, method: str) -> None:
    assert _pairs(EXPRESS + f"app.{verb}('/x', h);\n") == {("SERVES_HTTP_ROUTE", f"{method} /x")}


def test_the_settings_getter_is_not_a_route() -> None:
    """``app.get(name)`` with one argument reads a setting (Express API)."""
    assert _http(EXPRESS + "const engine = app.get('view engine');\napp.get('/x');\n") == []


def test_a_router_route_is_composed_from_its_mounts() -> None:
    source = (
        EXPRESS
        + "const users = express.Router();\n"
        + "users.get('/', list);\nusers.get('/:id', show);\n"
        + "app.use('/users', auth, users);\napp.use(users);\n"
    )
    references = _http(source)
    assert {r.target_key for r in references} == {
        "GET /users",
        "GET /users/:id",
        "GET /",
        "GET /:id",
    }
    assert {r.meta["provenance"] for r in references} == {"composed"}


def test_a_named_router_factory_is_an_express_router() -> None:
    source = (
        'import { Router } from "express";\nimport express from "express";\n'
        "const app = express();\nconst api = Router();\napi.get('/items', h);\n"
        "app.use('/api', api);\n"
    )
    assert _pairs(source) == {("SERVES_HTTP_ROUTE", "GET /api/items")}


@pytest.mark.parametrize(
    "tail",
    [
        "",
        "app.use(prefix, router);\n",
        "app.use('/api/', router);\n",
        "app.use('/api/*', router);\n",
        "const outer = express.Router();\nouter.use('/x', router);\napp.use('/o', outer);\n",
        "function mount() { app.use('/api', router); }\n",
    ],
)
def test_a_router_without_a_proven_mount_serves_nothing_here(tail: str) -> None:
    source = EXPRESS + "const router = express.Router();\nrouter.get('/items', h);\n" + tail
    assert not {pair for pair in _pairs(source) if pair[1].endswith("/items")}


@pytest.mark.parametrize("path", ["'/a/*'", "'/a?'", "'/a(b)?'", "'/a+'", "'/{x}'", "/ab+c/"])
def test_an_express_pattern_is_not_a_route_identity(path: str) -> None:
    assert _http(EXPRESS + f"app.get({path}, h);\n") == []


def test_an_express_parameter_path_is_kept_as_a_literal_template() -> None:
    assert _pairs(EXPRESS + "app.get('/users/:id', h);\n") == {
        ("SERVES_HTTP_ROUTE", "GET /users/:id")
    }


@pytest.mark.parametrize(
    "source",
    [
        "arbitraryApp.get('/hello', h);\n",
        "const cache = new Map();\ncache.get('/hello');\nmap.get('/hello', 1);\n",
        'import express from "express-lite";\nconst app = express();\napp.get("/x", h);\n',
        'import * as express from "express";\nconst app = express();\napp.get("/x", h);\n',
        'import express from "express";\nlet app = express();\napp.get("/x", h);\n',
        EXPRESS + "app.get('/x', h);\nfunction wire(app) { return app; }\n",
        EXPRESS + "app.get(PATH, h);\n",
        EXPRESS + "app.get(`/u/${id}`, h);\n",
        EXPRESS + "app.get('/u/' + id, h);\n",
        EXPRESS + "app.get(/^\\/x$/, h);\n",
        EXPRESS + "app.get(['/a', '/b'], h);\n",
    ],
)
def test_no_route_without_proven_identity_and_a_literal_path(source: str) -> None:
    assert _http(source) == []


# -- fetch -----------------------------------------------------------------------------


def test_a_relative_fetch_names_a_route_only() -> None:
    references = _http(
        """\
        export async function whoAmIRelative() {
            const r = await fetch("/hello/me");
            return r.json();
        }
        """
    )
    assert {(r.edge_type, r.target_key) for r in references} == {
        (EdgeType.CALLS_HTTP_ROUTE, "GET /hello/me")
    }
    [reference] = references
    assert reference.source_symbol == "whoAmIRelative"
    assert (reference.line_start, reference.line_end) == (2, 2)


def test_an_absolute_fetch_names_an_endpoint_and_its_route() -> None:
    assert _pairs('fetch("https://api.example.com/hello/me#top");\n') == {
        ("CALLS_HTTP_ENDPOINT", "GET https://api.example.com/hello/me"),
        ("CALLS_HTTP_ROUTE", "GET /hello/me"),
    }


@pytest.mark.parametrize(
    ("options", "method"),
    [
        ('{ method: "POST" }', "POST"),
        ("{ 'method': 'put', headers: h }", "PUT"),
        ('{ method: "delete" }', "DELETE"),
        ('{ method: "PATCH" }', "PATCH"),
        ("{ headers: { a: 1 }, body }", None),
        ("{ headers: h }", "GET"),
        ("{}", "GET"),
    ],
)
def test_a_fetch_method_is_read_from_a_literal_options_object(
    options: str, method: str | None
) -> None:
    pairs = _pairs(f"fetch('/users', {options});\n")
    assert pairs == (set() if method is None else {("CALLS_HTTP_ROUTE", f"{method} /users")})


@pytest.mark.parametrize(
    "call",
    [
        "fetch(url)",
        "fetch('/users', { method })",
        "fetch('/users', { method: verb })",
        "fetch('/users', { ...opts })",
        "fetch('/users', { ['method']: 'POST' })",
        "fetch('/users', opts)",
        "fetch('/users', { method: 'patch' })",
        "fetch('/users', { method: 'TRACE' })",
        "fetch('/users', { method: 'POST', method: 'GET' })",
        "fetch('/users', {}, extra)",
        "fetch(`/users/${id}`)",
        "fetch('/users/' + id)",
        "fetch(process.env.API + '/hello')",
        "fetch(config.baseUrl + '/hello')",
        "fetch('hello')",
        "fetch('//api.example.com/hello')",
        "fetch('https://user:pw@api.example.com/hello')",
        "fetch('\\/escaped')",
        "this.fetch('/hello')",
        "window.fetch('/hello')",
        "client.fetch('/hello')",
    ],
)
def test_a_fetch_that_proves_no_request_yields_nothing(call: str) -> None:
    assert _http(f"{call};\n") == []
    # Only a rooted template is kept, and only as a claim that is never matched.
    kept = {"fetch(`/users/${id}`)"}
    assert (_claims(f"{call};\n") != []) == (call in kept)


def test_a_fetch_template_is_recorded_as_a_claim_that_is_never_a_route() -> None:
    """The request stays visible as unresolved; nothing spells a route for it."""
    [claim] = _claims(
        "export function load(id: string) {\n  return fetch(`/users/${id}?x=${id}`);\n}\n"
    )
    assert claim.meta["pending_fact"] == "http-local-call"
    assert claim.meta["transport"] == "fetch"
    assert claim.meta["method"] == "GET"
    assert claim.target_key == "fetch"
    assert claim.source_symbol == "load"
    # The path only, cut at the query marker: a query value never reaches a claim.
    assert json.loads(claim.meta["path"]) == {
        "parts": [["l", "/users/"], ["s", "id"], ["l", "?"]],
        "truncated": False,
    }


@pytest.mark.parametrize(
    "call",
    [
        "fetch(`${base}/users/${id}`)",
        "fetch(`//api.example.com/${id}`)",
        "fetch(`users/${id}`)",
        "fetch(`https://api.example.com/users/${id}`)",
    ],
)
def test_a_fetch_template_that_is_not_a_rooted_path_is_not_recorded(call: str) -> None:
    """A leading substitution composes a base: transport plumbing, not a request."""
    assert _claims(f"{call};\n") == []
    assert _http(f"{call};\n") == []


def test_a_fetch_template_with_only_a_dynamic_query_is_an_ordinary_route() -> None:
    assert _pairs("fetch(`/users?page=${page}`);\n") == {("CALLS_HTTP_ROUTE", "GET /users")}
    assert _claims("fetch(`/users?page=${page}`);\n") == []


@pytest.mark.parametrize(
    "shadow",
    [
        'import fetch from "node-fetch";\n',
        "function fetch(x) { return x; }\n",
        "const fetch = makeFetch();\n",
        "function f(fetch) { return fetch; }\n",
    ],
)
def test_a_fetch_bound_in_the_file_is_not_the_global(shadow: str) -> None:
    assert _http(shadow + "fetch('/hello');\n") == []


def test_template_and_concatenated_literals_are_static() -> None:
    assert _pairs("fetch(`/a/b`);\nfetch('/c' + '/d');\nfetch(('/e'));\n") == {
        ("CALLS_HTTP_ROUTE", "GET /a/b"),
        ("CALLS_HTTP_ROUTE", "GET /c/d"),
        ("CALLS_HTTP_ROUTE", "GET /e"),
    }


# -- axios -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "call", "method"),
    [
        (AXIOS, "axios.get('https://api.example.com/users')", "GET"),
        (AXIOS, "axios.post('https://api.example.com/users', body)", "POST"),
        (AXIOS, "axios.put('https://api.example.com/users', body, { timeout: 5 })", "PUT"),
        (
            'const axios = require("axios");\n',
            "axios.delete('https://api.example.com/users')",
            "DELETE",
        ),
    ],
)
def test_an_absolute_axios_call_names_an_endpoint(header: str, call: str, method: str) -> None:
    assert _pairs(f"{header}{call};\n", "client.js") == {
        ("CALLS_HTTP_ENDPOINT", f"{method} https://api.example.com/users"),
        ("CALLS_HTTP_ROUTE", f"{method} /users"),
    }


@pytest.mark.parametrize(
    "tail",
    [
        # Relative: combined with a baseURL any file may set on the instance.
        "axios.get('/users');\n",
        "axios.get('https://api.example.com/users', { baseURL: 'https://other.example' });\n",
        "axios.get('https://api.example.com/users', config);\n",
        "axios.get('https://api.example.com/users', { ...config });\n",
        "axios.defaults.allowAbsoluteUrls = false;\naxios.get('https://api.example.com/users');\n",
        "const api = axios.create({ baseURL: 'https://api.example.com' });\napi.get('/users');\n",
        "axios({ method: 'get', url: 'https://api.example.com/users' });\n",
        "axios.request({ url: 'https://api.example.com/users' });\n",
    ],
)
def test_an_axios_call_that_proves_no_request_yields_nothing(tail: str) -> None:
    assert _http(AXIOS + tail) == []


def test_http_facts_are_additive_to_the_existing_call_graph() -> None:
    parsed = _parse("fetch('/hello');\n")
    assert any(r.edge_type is EdgeType.CALLS and r.target_key == "fetch" for r in parsed.references)


def test_tsx_and_javascript_grammars_both_yield_facts() -> None:
    source = "export function A() { fetch('/tsx'); return null; }\n"
    assert _pairs(source, "view.tsx") == {("CALLS_HTTP_ROUTE", "GET /tsx")}
    assert _pairs("fetch('/js');\n", "legacy.js") == {("CALLS_HTTP_ROUTE", "GET /js")}
