"""Express receivers bound by ``let``/``var``, constant paths and ``.route()`` chains (R2b-0).

``TypeScriptHttp`` used to prove an Express application or router only from a
top-level ``const``, and read a route path only from a literal. R2b-0 widens both
and stays as strict about what it refuses:

* a receiver may be bound by ``const``, ``let`` or ``var``, once, at the top level,
  and no receiver is served when a top-level statement names it before its
  declaration (this includes ``const``);
* a ``let``/``var`` receiver registers a route only in a top-level expression
  statement after its declaration -- not in a function, a condition or a block;
* a route path or mount prefix may be spelled through earlier top-level string
  constants; such a route is always composed, never exact;
* ``app.route(path).<verb>(handler)`` chains are read link by link, and any link
  that is not a verb call with an argument stops the chain.

Every case asserts the exact set of facts -- body, lines and provenance -- with no
duplicates and no other HTTP fact.

Supersessions (2026-11-06, R2b-0, user-approved)
------------------------------------------------

``tests/test_http_typescript.py`` listed this case among those that prove no route
(``test_no_route_without_proven_identity_and_a_literal_path``)::

    'import express from "express";\\nlet app = express();\\napp.get("/x", h);\\n',

A ``let app`` bound once and used after its statement is a proven application, so
that case is superseded by ``P2`` below. ``tests/test_grammar_loader.py`` pins the
``TypeScriptParser`` version to ``"24"`` for the same change.

Prediction amendments (2026-11-06)
----------------------------------

* A4(a): the prediction {(GET /x, 3, 3, exact)} is withdrawn; the parameter of function
  wire(app) is a second binding of app under R1, so app is not a receiver and the expected
  result is None. Fixture unchanged. See R2B0_SUPERSESSIONS.md in topology/express-r2b.
* P4 line 10: the predicted (GET /n/v1, 10, 10, composed) is corrected to
  (GET /nv1, 10, 10, composed); binary + concatenates without a separator. Fixture unchanged.
  See R2B0_SUPERSESSIONS.md.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from repoatlas.models.parsing import EdgeType, EntityType, ParsedFile, ParsedReference
from repoatlas.parsers.typescript import TypeScriptParser

_HTTP_KINDS = {EntityType.HTTP_ROUTE, EntityType.HTTP_DEPLOYMENT, EntityType.HTTP_ENDPOINT}

#: ``(body, line_start, line_end, provenance)``.
Fact = tuple[str, int, int, str]


def _src(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def _parse(source: str, name: str) -> ParsedFile:
    return TypeScriptParser().parse(Path(name), textwrap.dedent(source))


def _http(source: str, name: str) -> list[ParsedReference]:
    """HTTP facts, not the pending claims only the global pass can decide."""
    return [
        r
        for r in _parse(source, name).references
        if r.target_kind in _HTTP_KINDS and "pending_fact" not in r.meta
    ]


def _fact(reference: ParsedReference) -> Fact:
    assert reference.line_start is not None
    assert reference.line_end is not None
    return (
        reference.target_key,
        reference.line_start,
        reference.line_end,
        str(reference.meta["provenance"]),
    )


def _facts(source: str, name: str) -> list[Fact]:
    return [_fact(r) for r in _http(source, name)]


def _exact(source: str, name: str, expected: set[Fact]) -> None:
    facts = _facts(source, name)
    assert len(facts) == len(set(facts)), facts
    assert set(facts) == expected


# -- served --------------------------------------------------------------------------------

_P1 = _src(
    'var express = require("express");',
    "var app = express();",
    'app.get("/health", h);',
)
_P2 = _src(
    'import express from "express";',
    "let app = express();",
    'app.get("/x", h);',
)
_P3 = _src(
    'var express = require("express");',
    "var app = express();",
    "let router = express.Router();",
    'var BASE = "/api";',
    'router.get("/items", h);',
    'router.post("/items", h);',
    "app.use(BASE, router);",
)
_P4 = _src(
    'var express = require("express");',
    "var app = express();",
    'var ver = "v1";',
    "var router = express.Router();",
    'router.get("/read", h);',
    'app.use("/api/" + ver, router);',
    'app.get("/v/" + ver, h);',
    "app.get(`/t/${ver}`, h);",
    'var NESTED = "/n" + ver;',
    "app.get(NESTED, h);",
)
_P5 = _src(
    'const express = require("express");',
    "const app = express();",
    "const router = express.Router();",
    "router",
    '  .route("/users")',
    "  .get(list)",
    "  .post(create);",
    'app.route("/ping").get(h).delete(h);',
    'app.use("/v1", router);',
)


@pytest.mark.parametrize(
    ("source", "name", "expected"),
    [
        pytest.param(_P1, "server.js", {("GET /health", 3, 3, "exact")}, id="P1-var-app"),
        pytest.param(_P2, "api.ts", {("GET /x", 3, 3, "exact")}, id="P2-let-app"),
        pytest.param(
            _P3,
            "server.js",
            {("GET /api/items", 5, 5, "composed"), ("POST /api/items", 6, 6, "composed")},
            id="P3-identifier-prefix-same-file-mount",
        ),
        pytest.param(
            _P4,
            "server.js",
            {
                ("GET /api/v1/read", 5, 5, "composed"),
                ("GET /v/v1", 7, 7, "composed"),
                ("GET /t/v1", 8, 8, "composed"),
                ("GET /nv1", 10, 10, "composed"),
            },
            id="P4-identifier-and-literal",
        ),
        pytest.param(
            _P5,
            "server.js",
            {
                ("GET /v1/users", 4, 6, "composed"),
                ("POST /v1/users", 4, 7, "composed"),
                ("GET /ping", 8, 8, "exact"),
                ("DELETE /ping", 8, 8, "exact"),
            },
            id="P5-route-chain",
        ),
    ],
)
def test_r2b0_served(source: str, name: str, expected: set[Fact]) -> None:
    references = _http(source, name)
    assert all(r.edge_type is EdgeType.SERVES_HTTP_ROUTE for r in references)
    assert all(r.meta["source"] == "express" for r in references)
    _exact(source, name, expected)


# -- refused -------------------------------------------------------------------------------

_NONE: set[Fact] = set()

_A1 = _P2 + "app = express();\n"
_A2 = _src(
    'var express = require("express");',
    'app.get("/early", h);',
    "var app = express();",
    'app.get("/late", h);',
)
_A2B = _src(
    'import express from "express";',
    'app.get("/early", h);',
    "const app = express();",
    'app.get("/late", h);',
)
_A2C = _src(
    'var express = require("express");',
    "var app = express();",
    "var router = express.Router();",
    'router.get("/items", h);',
    "app.use(BASE, router);",
    'var BASE = "/api";',
)
_A3A = _src(
    'var express = require("express");',
    "var app = express();",
    "var router = express.Router();",
    'router.get("/items", h);',
    "app.use(PREFIX, router);",
)
_A3B = _src(
    'var express = require("express");',
    "var app = express();",
    "var router = express.Router();",
    'var PREFIX = process.env.BASE || "/api";',
    'router.get("/items", h);',
    "app.use(PREFIX, router);",
)
_A4A = _P2 + 'function wire(app) { app.get("/y", h); }\n'
_A4B = _src(
    'var express = require("express");',
    "var app = express();",
    "let router = express.Router();",
    'var BASE = "/api";',
    'router.get("/items", h);',
    'router.post("/items", h);',
    "function f(BASE) { return BASE; }",
    "app.use(BASE, router);",
)
_A5 = _src(
    'var express = require("express");',
    "var app = express();",
    "var router = express.Router();",
    'router.get("/orphan", h);',
    'router.route("/chain").get(h);',
)
_A6_VAR = _src(
    'var express = require("express");',
    "var app = express();",
    'app.use("/api", router);',
    "var router = express.Router();",
    'router.get("/items", h);',
)
_A6_LET = _src(
    'var express = require("express");',
    "var app = express();",
    'app.use("/api", router);',
    "let router = express.Router();",
    'router.get("/items", h);',
)
_A7 = _src(
    'import express from "express";',
    '{ let app = express(); app.get("/x", h); }',
    'if (c) { var server = express(); server.get("/y", h); }',
)
_A8 = _src(
    'const express = require("express");',
    "const app = express();",
    'app.route("/a").all(mw).get(h);',
    'app.route("/b").get(h).all(mw).post(h);',
    'app.route("/c").get().post(h);',
    'app.route("/d", extra).get(h);',
    'other.route("/e").get(h);',
    'app.route("/f*").get(h);',
)
_A9 = _src(
    'import express from "express";',
    "const app = express();",
    "app.get(`/u/${id}`, h);",
    "const v = 1;",
    "app.get(`/n/${v}`, h);",
    'app.get("/p/" + v, h);',
    'const s = "/s";',
    "app.get(`\\/e${s}`, h);",
    "app.get(`/c/${s.toUpperCase()}`, h);",
)
_A11 = _src(
    'var express = require("express");',
    "var app = express();",
    'function setup() { app.get("/in", h); }',
    'if (ready) app.get("/cond", h);',
    'app.get("/top", h);',
)


@pytest.mark.parametrize(
    ("source", "name", "expected"),
    [
        pytest.param(_A1, "api.ts", _NONE, id="A1-reassigned-let"),
        pytest.param(_A2, "server.js", _NONE, id="A2-var-used-before-declaration"),
        pytest.param(_A2B, "api.ts", _NONE, id="A2b-const-used-before-declaration"),
        pytest.param(_A2C, "server.js", _NONE, id="A2c-constant-declared-after-use"),
        pytest.param(_A3A, "server.js", _NONE, id="A3a-prefix-never-declared"),
        pytest.param(_A3B, "server.js", _NONE, id="A3b-prefix-or-expression"),
        pytest.param(_A4A, "api.ts", _NONE, id="A4a-shadowed-app"),
        pytest.param(_A4B, "server.js", _NONE, id="A4b-shadowed-constant"),
        pytest.param(_A5, "server.js", _NONE, id="A5-router-never-mounted"),
        pytest.param(_A6_VAR, "server.js", _NONE, id="A6-mount-before-var-router"),
        pytest.param(_A6_LET, "server.js", _NONE, id="A6-mount-before-let-router"),
        pytest.param(_A7, "api.ts", _NONE, id="A7-let-var-in-a-block"),
        pytest.param(_A8, "server.js", {("GET /b", 4, 4, "exact")}, id="A8-bad-chain-links"),
        pytest.param(_A9, "api.ts", _NONE, id="A9-dynamic-and-unsafe-templates"),
        pytest.param(
            _A11, "server.js", {("GET /top", 5, 5, "exact")}, id="A11-var-registration-position"
        ),
    ],
)
def test_r2b0_refused(source: str, name: str, expected: set[Fact]) -> None:
    _exact(source, name, expected)


def test_r2b0_var_axios_is_not_a_client() -> None:
    """``require("axios")`` stays const-only: a ``var`` axios proves no client."""
    source = _src(
        'var axios = require("axios");',
        'axios.get("https://api.example.com/x");',
    )
    references = _parse(source, "client.js").references
    assert not [r for r in references if r.meta.get("source") == "axios"]
