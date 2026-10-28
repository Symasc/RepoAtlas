"""A star import makes unproven the names it could rebind, and no other (HTTP wildcard, M2).

``from x import *`` used to make every name of its module unknowable, so one star in a router
module hid a route, a mount and a client the module proves independently of it. The rule now is
Python's own: a star is one more possible binding of each module-level name it could rebind. A
name its statement certainly binds *after* the star, with nothing reading the name before that,
keeps its one binding; a name bound before the star, bound conditionally, or read before its
binding is unproven, and unproven yields nothing. The star never imports anything it names: no
module is resolved, run or listed (V1_SPEC §17).

Half of this file is what the star still refuses, and the other half is what it no longer does.
"""

from __future__ import annotations

import ast
import json
import sys
import textwrap
from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.graph.http_crossings import LINKED
from repoatlas.graph.py_routes import CONSTANT_UNRESOLVED, NOT_DECLARED, PROMOTED
from repoatlas.models.parsing import EdgeType, EntityType, ParsedReference
from repoatlas.parsers import http_python
from repoatlas.parsers.http_facts import (
    BASE_UNKNOWN,
    HTTP_CLIENT_CALL,
    HTTP_EXPOSED_ENDPOINT,
    PENDING_FACT,
    PYTHON_CONSTANTS,
    PYTHON_MOUNT,
    PYTHON_RECEIVER,
    PYTHON_ROUTE,
    PYTHON_SCRIPT,
)
from repoatlas.parsers.http_python import PythonHttp
from repoatlas.parsers.interpreter import interpreter_tag
from repoatlas.parsers.python import PythonParser
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace
from repoatlas.sync.planner import facts_version
from tests.http_crossing_corpus import decided
from tests.impact_corpus import build, make_repo
from tests.incremental_corpus import assert_equals_fresh, edit, fresh_index, sync_all
from tests.test_deploy_ingress import CALLER, chart
from tests.test_http_python import _http, _parse, _served
from tests.test_http_python_calls import ALTERNATIVES
from tests.test_http_python_clients import _claims, _facts
from tests.test_http_python_identity import _app
from tests.test_http_python_routers import outcomes, served
from tests.test_http_refusals_impact import DEVICES, DEVICES_APP, decisions, impact, key

_HTTP_KINDS = {EntityType.HTTP_ROUTE, EntityType.HTTP_DEPLOYMENT, EntityType.HTTP_ENDPOINT}
_ROLES = {
    PYTHON_RECEIVER,
    PYTHON_MOUNT,
    PYTHON_ROUTE,
    PYTHON_CONSTANTS,
    PYTHON_SCRIPT,
    HTTP_CLIENT_CALL,
    HTTP_EXPOSED_ENDPOINT,
}

STAR = "from helpers import *"
AFTER = "star-after-binding"
CONDITIONAL = "star-before-conditional-binding"
READ_BEFORE = "star-read-before-binding"


def _lines(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def _related(source: str) -> list[ParsedReference]:
    """Every reference the HTTP reader contributes: facts, and the claims the global pass reads."""
    return [
        r
        for r in _parse(source).references
        if r.target_kind in _HTTP_KINDS or r.meta.get(PENDING_FACT) in _ROLES
    ]


def _reasons(source: str) -> dict[str, str] | None:
    return PythonHttp(ast.parse(textwrap.dedent(source))).star_unproven


def _roles(source: str, role: str) -> list[ParsedReference]:
    return [r for r in _parse(source).references if r.meta.get(PENDING_FACT) == role]


def _fact_keys(source: str) -> set[tuple[EdgeType, str]]:
    return {(r.edge_type, r.target_key) for r in _facts(source)}


# -- what a star no longer hides ------------------------------------------------------------


def test_a_star_before_the_framework_import_leaves_the_application_proven() -> None:
    source = _lines(
        STAR,
        "from fastapi import FastAPI",
        "app = FastAPI()",
        "@app.get('/hello')",
        "def hello(): ...",
    )
    assert _served(source) == {"GET /hello"}
    [route] = [r for r in _http(source) if PENDING_FACT not in r.meta]
    assert route.meta == {"source": "fastapi", "provenance": "exact"}
    assert (route.line_start, route.line_end) == (4, 4)
    assert _reasons(source) == {}


def test_a_star_after_the_framework_import_refuses_only_what_depends_on_it() -> None:
    source = _lines(
        "from fastapi import FastAPI",
        STAR,
        "import requests",
        "app = FastAPI()",
        "@app.get('/x')",
        "def h(): ...",
        "def call(): return requests.get('https://api.example.com/users')",
    )
    assert _served(source) == set()
    assert _roles(source, PYTHON_RECEIVER) == []
    assert _fact_keys(source) == {
        (EdgeType.CALLS_HTTP_ENDPOINT, "GET https://api.example.com/users"),
        (EdgeType.CALLS_HTTP_ROUTE, "GET /users"),
    }
    assert len(_claims(source)) == 1
    assert _reasons(source) == {"FastAPI": AFTER}


def test_a_star_after_the_application_refuses_its_routes_and_keeps_a_later_client() -> None:
    source = _lines(
        "from fastapi import FastAPI",
        "app = FastAPI()",
        "@app.get('/x')",
        "def h(): ...",
        STAR,
        "import httpx",
        'client = httpx.Client(base_url="https://svc.example")',
        "def use(): return client.get('/users')",
    )
    references = _parse(source).references
    assert [r for r in references if r.edge_type is EdgeType.SERVES_HTTP_ROUTE] == []
    assert _roles(source, PYTHON_RECEIVER) == []
    [claim] = _claims(source)
    assert claim.meta["origin"] == "https://svc.example"
    assert claim.meta["client"] == "client"
    assert _reasons(source) == {"FastAPI": AFTER, "app": AFTER, "h": AFTER}


def test_a_function_local_client_is_out_of_a_star_import_s_reach() -> None:
    source = _lines(
        STAR,
        "import httpx",
        "def use():",
        '    with httpx.Client(base_url="https://svc.example") as client:',
        "        return client.get('/users')",
    )
    [claim] = _claims(source)
    assert claim.meta["origin"] == "https://svc.example"
    assert _reasons(source) == {}


def test_a_star_in_a_module_level_try_is_a_star_at_its_position() -> None:
    source = _lines(
        "try: from fast_helpers import *",
        "except ImportError: pass",
        "import requests",
        "def load(): return requests.get('https://api.example.com/users')",
    )
    assert _fact_keys(source) == {
        (EdgeType.CALLS_HTTP_ENDPOINT, "GET https://api.example.com/users"),
        (EdgeType.CALLS_HTTP_ROUTE, "GET /users"),
    }


def test_order_on_one_line_is_python_s_order() -> None:
    call = "def load(): return requests.get('https://api.example.com/users')\n"
    before = "import requests; " + STAR + "\n" + call
    assert _http(before) == []
    assert _reasons(before) == {"requests": AFTER}
    after = STAR + "; import requests\n" + call
    assert len(_fact_keys(after)) == 2
    assert _reasons(after) == {}


# -- a star before everything changes nothing -----------------------------------------------

_POSITIVE = {
    "fastapi-application": _lines(
        "from fastapi import FastAPI",
        "app = FastAPI()",
        "",
        "@app.get('/hello')",
        "def hello(): ...",
    ),
    "router-mounts": _lines(
        "from fastapi import APIRouter, FastAPI",
        "",
        "app = FastAPI()",
        'router = APIRouter(prefix="/users")',
        "",
        "",
        '@router.get("/me")',
        "def me(): ...",
        "",
        "",
        'app.include_router(router, prefix="/api")',
        "app.include_router(router)",
    ),
    "flask-blueprint": _lines(
        "from flask import Blueprint, Flask",
        "app = Flask(__name__)",
        'pages = Blueprint("pages", __name__, url_prefix="/pages")',
        '@pages.route("/about")',
        "def about(): ...",
        "app.register_blueprint(pages)",
        'app.register_blueprint(pages, url_prefix="/v2", name="pages_v2")',
    ),
    "flasgger-swagger": _lines(
        "from flask import Flask",
        "from flasgger import Swagger",
        "",
        "app = Flask(__name__)",
        'Swagger(app, template={"host": "a.example", "basePath": "/v1", "schemes": ["https"]})',
        "",
        '@app.get("/items/<int:item_id>")',
        "def item(item_id): ...",
    ),
    "flasgger-optional-import": _app(),
    "if-elif-else-alternatives": ALTERNATIVES,
    "environment-default": _lines(
        "import os",
        "import requests",
        'BASE = os.environ.get("ACCOUNTS_URL", "https://accounts.example/api")',
        "def load(uid):",
        '    return requests.get(f"{BASE}/users/{uid}")',
    ),
    "requests-session": _lines(
        "import requests",
        "",
        "session = requests.Session()",
        "",
        "",
        "def load():",
        '    return session.get("https://a.example/users/123")',
    ),
    "httpx-client-with-base": _lines(
        "import httpx",
        "",
        'client = httpx.Client(base_url="https://api.example.com")',
        "",
        "",
        "def users():",
        '    return client.get("/users")',
    ),
    "top-level-with-client": _lines(
        "import httpx",
        'with httpx.Client(base_url="https://api.example.com") as client:',
        '    client.get("/users")',
    ),
    "literal-list-loop": _lines(
        "from fastapi import APIRouter, FastAPI",
        "app = FastAPI()",
        'first = APIRouter(prefix="/a")',
        'second = APIRouter(prefix="/b")',
        "",
        "@first.get('/x')",
        "def x(): ...",
        "",
        "@second.get('/y')",
        "def y(): ...",
        "",
        "routers = [first, second]",
        "for router in routers:",
        "    app.include_router(router)",
    ),
    "factory-mount": _lines(
        "from fastapi import APIRouter, FastAPI",
        'router = APIRouter(prefix="/r")',
        "",
        "@router.get('/x')",
        "def x(): ...",
        "",
        "def create_app():",
        "    app = FastAPI()",
        '    app.include_router(router, prefix="/api")',
        "    return app",
    ),
    "module-level-call": _lines(
        "import requests",
        "",
        "def load():",
        '    return requests.get("https://a.example/users/123")',
    ),
    "script-guard": _lines(
        "from fastapi import FastAPI",
        "app = FastAPI()",
        "@app.get('/hello')",
        "def hello(): ...",
        'if __name__ == "__main__":',
        "    pass",
    ),
}
_POSITIVE_IDS = list(_POSITIVE)
_POSITIVE_SOURCES = list(_POSITIVE.values())


@pytest.mark.parametrize("source", _POSITIVE_SOURCES, ids=_POSITIVE_IDS)
def test_a_star_import_before_everything_changes_no_fact(source: str) -> None:
    original = _related(source)
    assert original != []
    prefixed = f"{STAR}; {source}"
    assert _related(prefixed) == original
    assert _reasons(prefixed) == {}


@pytest.mark.parametrize("source", _POSITIVE_SOURCES, ids=_POSITIVE_IDS)
def test_a_star_import_after_everything_refuses_every_module_binding(source: str) -> None:
    assert _related(source) != []
    remaining = _related(source + "\n" + STAR + "\n")
    assert all(r.meta.get(PENDING_FACT) == PYTHON_SCRIPT for r in remaining), remaining


@pytest.mark.parametrize(
    "source",
    [
        STAR + "\n" + _POSITIVE["script-guard"],
        _POSITIVE["script-guard"] + STAR + "\n",
        _POSITIVE["script-guard"] + "def f():\n    " + STAR + "\n",
    ],
    ids=["first", "last", "in-a-function"],
)
def test_the_script_claim_does_not_depend_on_a_star(source: str) -> None:
    guard = source.splitlines().index('if __name__ == "__main__":') + 1
    [script] = _roles(source, PYTHON_SCRIPT)
    assert (script.line_start, script.line_end) == (guard, guard)


# -- what a star still refuses ---------------------------------------------------------------


def test_a_star_after_a_constant_offers_it_to_no_one_and_reads_it_as_unknown() -> None:
    source = _lines(
        'BASE = "https://svc.example/api"',
        STAR,
        "import requests",
        "def users(): return requests.get(f'{BASE}/users')",
    )
    assert _roles(source, PYTHON_CONSTANTS) == []
    assert _facts(source) == []
    [claim] = _claims(source)
    assert claim.meta["base"] == BASE_UNKNOWN
    assert "origin" not in claim.meta
    assert claim.target_key == "GET {BASE}/users"
    assert _reasons(source) == {"BASE": AFTER}
    # The same module with the star first states the constant, and offers it.
    control = _lines(
        STAR,
        'BASE = "https://svc.example/api"',
        "import requests",
        "def users(): return requests.get(f'{BASE}/users')",
    )
    [stated] = _claims(control)
    assert stated.meta["origin"] == "https://svc.example"
    [offered] = _roles(control, PYTHON_CONSTANTS)
    assert "BASE" in json.loads(offered.meta["constants"])


def test_a_star_before_a_conditional_binding_leaves_the_name_unproven() -> None:
    source = _lines(
        STAR,
        "import httpx",
        "if FLAG:",
        '    client = httpx.Client(base_url="https://svc.example")',
        "def use(): return client.get('/users')",
    )
    assert _claims(source) == []
    assert _reasons(source) == {"client": CONDITIONAL}
    # Without the star the conditional binding is the name's only one, as before.
    [claim] = _claims("\n".join(source.splitlines()[1:]) + "\n")
    assert claim.meta["origin"] == "https://svc.example"


def test_a_name_read_before_its_binding_under_a_star_is_unproven() -> None:
    source = _lines(
        STAR,
        "from fastapi import FastAPI",
        '@app.get("/early")',
        "def early(): ...",
        "app = FastAPI()",
        '@app.get("/late")',
        "def late(): ...",
    )
    assert _served(source) == set()
    assert _reasons(source) == {"app": READ_BEFORE}
    # Not changed here: with no star, both decorators read the one application the module binds.
    control = "\n".join(source.splitlines()[1:]) + "\n"
    assert _served(control) == {"GET /early", "GET /late"}


def test_alternatives_after_a_star_are_read_and_a_star_inside_a_branch_refuses_them() -> None:
    call = ["def users(): return requests.get(f'{BASE}/users')"]
    leading = _lines(
        STAR,
        "import requests",
        "if PROD:",
        '    BASE = "https://a.example"',
        "else:",
        '    BASE = "https://b.example"',
        *call,
    )
    claims = _claims(leading)
    assert len(claims) == 2
    assert {c.meta["alternatives"] for c in claims} == {"2"}
    assert {c.meta["origin"] for c in claims} == {"https://a.example", "https://b.example"}

    inside = _lines(
        "if PROD:",
        '    BASE = "https://a.example"',
        "else:",
        "    " + STAR,
        "    BASE = 'https://b.example'",
        "import requests",
        *call,
    )
    [claim] = _claims(inside)
    assert claim.meta["base"] == BASE_UNKNOWN
    assert _reasons(inside) == {"BASE": AFTER}


def test_shadowing_still_refuses_under_a_leading_star() -> None:
    source = _lines(
        STAR,
        "from fastapi import FastAPI",
        "app = FastAPI()",
        "@app.get('/hello')",
        "def hello(): ...",
        "def g(app): ...",
    )
    assert _served(source) == set()
    assert _reasons(source) == {}


def test_a_name_only_a_star_import_could_bind_is_never_resolved() -> None:
    source = _lines(
        "from routes import *",
        "from fastapi import FastAPI",
        "app = FastAPI()",
        'app.include_router(router, prefix="/api")',
        '@router.get("/me")',
        "def me(): ...",
    )
    assert _roles(source, PYTHON_MOUNT) == []
    assert _roles(source, PYTHON_ROUTE) == []
    assert _served(source) == set()
    [receiver] = _roles(source, PYTHON_RECEIVER)
    assert receiver.meta["kind"] == "fastapi-app"


def test_a_star_inside_a_function_still_refuses_the_whole_module() -> None:
    call = "def g(): return requests.get('https://api.example.com/users')"
    source = _lines("import requests", "def f():", "    " + STAR, call)
    assert _http(source) == []
    assert PythonHttp(ast.parse(source)).star_unproven is None
    # CPython itself refuses it, which is why the parser need not read past it.
    with pytest.raises(SyntaxError):
        compile(source, "app.py", "exec")
    in_class = _lines("import requests", "class C:", "    " + STAR, call)
    assert _http(in_class) == []


def test_deeply_nested_statements_need_no_recursion() -> None:
    depth = 80
    nested = "".join("    " * level + "if x:\n" for level in range(depth))
    inner = "    " * depth
    source = nested + inner + STAR + "\n" + inner + "import requests\n"
    assert _reasons(source) == {"requests": AFTER}
    chain = "if a0:\n    N = 1\n" + "".join(f"elif a{i}:\n    N = 1\n" for i in range(1, 100))
    assert _reasons(STAR + "\n" + chain + "else:\n    N = 1\n") == {}
    assert _reasons(STAR + "\n" + chain) == {"N": CONDITIONAL}


# -- the rule, statement by statement ---------------------------------------------------------

_SCOPES = {
    "if": "if x:\n    from h import *",
    "else": "if x:\n    pass\nelse:\n    from h import *",
    "for": "for i in y:\n    from h import *",
    "while": "while x:\n    from h import *",
    "with": "with x:\n    from h import *",
    "except": "try:\n    pass\nexcept E:\n    from h import *",
    "try-else": "try:\n    pass\nexcept E:\n    pass\nelse:\n    from h import *",
    "finally": "try:\n    pass\nfinally:\n    from h import *",
    "match": "match v:\n    case 1:\n        from h import *",
    "except-star": pytest.param(
        "try:\n    pass\nexcept* E:\n    from h import *",
        marks=pytest.mark.skipif(sys.version_info < (3, 11), reason="except* needs Python 3.11"),
    ),
}


@pytest.mark.parametrize("star", list(_SCOPES.values()), ids=list(_SCOPES))
def test_a_star_in_any_module_scope_statement_leaves_the_rest_of_the_module_proven(
    star: str,
) -> None:
    source = _lines(
        star, "import requests", "def load(): return requests.get('https://api.example.com/x')"
    )
    assert _reasons(source) is not None
    assert len(_fact_keys(source)) == 2


@pytest.mark.parametrize(
    ("statement", "unproven"),
    [
        ("if X:\n    N = 1", {"N"}),
        ("if X:\n    N = 1\nelif Y:\n    N = 2", {"N"}),
        ("if X:\n    N = 1\nelse:\n    pass", {"N"}),
        ("while X:\n    N = 1", {"N"}),
        ("for i in Y:\n    N = 1", {"N"}),
        ("with X:\n    N = 1", {"N"}),
        ("(N := 1)", {"N"}),
        ("N, M = 1, 2", {"N", "M"}),
        ("N: int", {"N"}),
        ("N += 1", {"N"}),
        ("try:\n    N = 1\nexcept E:\n    pass", {"N"}),
        ("try:\n    pass\nexcept E:\n    N = 1", {"N"}),
        ("match X:\n    case _:\n        N = 1", {"N"}),
    ],
)
def test_a_binding_that_may_not_run_is_unproven_under_a_leading_star(
    statement: str, unproven: set[str]
) -> None:
    assert _reasons(_lines(STAR, statement)) == dict.fromkeys(unproven, CONDITIONAL)


@pytest.mark.parametrize(
    "statement",
    [
        "import N",
        "import N.sub",
        "import a.N as N",
        "from a import N",
        "def N(): ...",
        "@decorate\ndef N(): ...",
        "class N: ...",
        "N = 1",
        "N = M = 1",
        "N: int = 1",
        "for N in Y:\n    pass",
        "with X as N:\n    pass",
        "if X:\n    N = 1\nelse:\n    N = 2",
        "if X:\n    N = 1\nelif Y:\n    N = 2\nelse:\n    N = 3",
        "if X:\n    if Y:\n        N = 1\n    else:\n        N = 2\nelse:\n    N = 3",
        "try:\n    N = 1\nexcept E:\n    N = 2",
        "try:\n    N = 1\nfinally:\n    pass",
        "try:\n    import N\nexcept ImportError:\n    N = None",
    ],
)
def test_a_binding_that_certainly_runs_stays_proven_under_a_leading_star(statement: str) -> None:
    assert _reasons(_lines(STAR, statement)) == {}


@pytest.mark.parametrize(
    "statement",
    [
        "X = X + 1",
        "X: int = X",
        "for X in X:\n    pass",
        "with X as X:\n    pass",
        "@X\ndef X(): ...",
        "def X(a=X): ...",
        "class X(X): ...",
        "class X(metaclass=X): ...",
        "if X:\n    X = 1\nelse:\n    X = 2",
        "try:\n    X = X\nexcept E:\n    X = 1",
    ],
)
def test_a_name_read_by_the_part_of_its_statement_that_runs_first_is_unproven(
    statement: str,
) -> None:
    assert _reasons(_lines(STAR, statement)) == {"X": READ_BEFORE}


def test_a_name_bound_by_two_statements_is_unproven_whatever_the_order() -> None:
    assert _reasons(_lines(STAR, "x = 1", "x = 2")) == {"x": AFTER}


def test_the_last_star_decides_the_order() -> None:
    other = "from other import *"
    assert _reasons(_lines(STAR, "import requests", other)) == {"requests": AFTER}
    assert _reasons(_lines(STAR, other, "import requests")) == {}


# -- the global pass -------------------------------------------------------------------------

_MAIN = _lines(
    "from fastapi import FastAPI",
    "from routes import router",
    "app = FastAPI()",
    'app.include_router(router, prefix="/api")',
    'if __name__ == "__main__":',
    "    pass",
)
_ROUTES = _lines(
    STAR,
    "from fastapi import APIRouter",
    "router = APIRouter()",
    '@router.get("/me")',
    "def me(): ...",
)
_ROUTES_STAR_BELOW = _lines(
    "from fastapi import APIRouter",
    "router = APIRouter()",
    STAR,
    '@router.get("/me")',
    "def me(): ...",
)
_SERVICE = {"main.py": _MAIN, "routes.py": _ROUTES, "helpers.py": "X = 1\n"}
_ME = {("svc", "routes.py", "me", "GET /api/me", None)}


def _outcome(workspace: Workspace, path: str, handler: str) -> str:
    [found] = [v for (p, h, _), v in outcomes(workspace).items() if (p, h) == (path, handler)]
    return found


def test_a_router_bound_after_a_star_is_served_through_another_module(
    workspace: Workspace, tmp_path: Path
) -> None:
    indexed = build(workspace, tmp_path, {"svc": _SERVICE})
    assert served(indexed) == _ME


def test_a_router_bound_before_a_star_is_served_nowhere(
    workspace: Workspace, tmp_path: Path
) -> None:
    indexed = build(workspace, tmp_path, {"svc": {**_SERVICE, "routes.py": _ROUTES_STAR_BELOW}})
    assert served(indexed) == set()


def test_a_receiver_only_a_star_re_exports_is_not_declared(
    workspace: Workspace, tmp_path: Path
) -> None:
    views = _lines("from pkg import app", "@app.get('/x')", "def x(): ...")
    files = {**_SERVICE, "pkg.py": "from main import *\n", "views.py": views}
    indexed = build(workspace, tmp_path, {"svc": files})
    assert _outcome(indexed, "views.py", "x") == NOT_DECLARED
    assert "GET /x" not in {route for _, _, _, route, _ in served(indexed)}


def test_a_constant_a_star_may_rebind_is_not_offered(workspace: Workspace, tmp_path: Path) -> None:
    routes = _lines(
        "from fastapi import APIRouter",
        "from paths import PREFIX",
        "router = APIRouter()",
        '@router.get(f"{PREFIX}/me")',
        "def me(): ...",
    )
    files = {**_SERVICE, "routes.py": routes, "paths.py": 'PREFIX = "/api"\n' + STAR + "\n"}
    indexed = build(workspace, tmp_path, {"svc": files})
    assert _outcome(indexed, "routes.py", "me") == CONSTANT_UNRESOLVED
    assert served(indexed) == set()


def test_a_constant_bound_after_a_star_is_offered(workspace: Workspace, tmp_path: Path) -> None:
    routes = _lines(
        "from fastapi import APIRouter",
        "from paths import PREFIX",
        "router = APIRouter()",
        '@router.get(f"{PREFIX}/me")',
        "def me(): ...",
    )
    files = {**_SERVICE, "routes.py": routes, "paths.py": STAR + '\nPREFIX = "/api"\n'}
    indexed = build(workspace, tmp_path, {"svc": files})
    assert _outcome(indexed, "routes.py", "me") == PROMOTED
    assert {route for _, _, _, route, _ in served(indexed)} == {"GET /api/api/me"}


# -- end to end: a caller is linked as it was without the star -------------------------------


def _candidates(workspace: Workspace) -> set[tuple[str | None, str | None, str]]:
    response = impact(workspace, key(workspace, "devices", "get_item"))
    return {(c.repository, c.path, c.name) for c in response.candidates}


def test_a_star_before_the_application_links_its_caller_as_without_it(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    star_app = f"{STAR}; " + DEVICES_APP
    built = build(
        workspace,
        tmp_path,
        {"devices": {**DEVICES, "app.py": star_app, **chart(enabled=True)}, "caller": CALLER},
    )
    free_root = tmp_path / "free-repos"
    for name, files in {
        "devices": {**DEVICES, **chart(enabled=True)},
        "caller": CALLER,
    }.items():
        make_repo(free_root, name, files)
    [(decision, _)] = decided(built, "caller")["dev_item"]
    assert decision == LINKED
    with fresh_index(settings, free_root, tmp_path, "star-free") as free:
        assert decisions(built) == decisions(free)
        assert _candidates(built) == _candidates(free)
    assert any(repository == "caller" for repository, _, _ in _candidates(built))


def test_a_star_after_the_application_unlinks_its_caller(
    workspace: Workspace, tmp_path: Path
) -> None:
    built = build(
        workspace,
        tmp_path,
        {
            "devices": {**DEVICES, "app.py": DEVICES_APP + STAR + "\n", **chart(enabled=True)},
            "caller": CALLER,
        },
    )
    [(decision, _)] = decided(built, "caller")["dev_item"]
    assert decision != LINKED, decision
    assert not [repository for repository, _, _ in _candidates(built) if repository == "caller"]


# -- incremental equals fresh, and no execution ----------------------------------------------


def test_moving_a_star_import_is_what_a_fresh_index_writes(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    indexed = build(workspace, tmp_path, {"svc": _SERVICE})
    root = tmp_path / "repos"
    assert served(indexed) == _ME

    edit(root, "svc", "routes.py", _ROUTES_STAR_BELOW)
    sync_all(indexed, root)
    assert served(indexed) == set()
    assert_equals_fresh(indexed, settings, root, tmp_path, "star-below")

    edit(root, "svc", "routes.py", _ROUTES)
    sync_all(indexed, root)
    assert served(indexed) == _ME
    assert_equals_fresh(indexed, settings, root, tmp_path, "star-above")


def test_an_index_written_before_the_rule_is_reparsed_on_upgrade(
    workspace: Workspace, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Worker processes do not see a monkeypatch: parse in this one.
    monkeypatch.setenv("REPOATLAS_SYNC_WORKERS", "1")
    current = http_python._star_unproven
    # The old policy: any star refuses the whole module.
    monkeypatch.setattr(http_python, "_star_unproven", lambda tree, stars: None)
    indexed = build(workspace, tmp_path, {"svc": _SERVICE})
    assert served(indexed) == set()

    live = facts_version(indexed.registry, indexed.dependency_registry)
    version = f"{PythonParser.name}:{PythonParser.version}"
    assert version in live, live
    # A literal, not "one less than now": a relative offset would pass with the bump deleted.
    previous = live.replace(version, f"python-ast:19+{interpreter_tag()}")
    assert previous != live
    indexed.database.execute(
        "UPDATE files SET parser_version = ? WHERE rel_path LIKE '%.py'", (previous,)
    )
    indexed.database.connection.commit()

    monkeypatch.setattr(http_python, "_star_unproven", current)
    root = tmp_path / "repos"
    report = sync(indexed, indexed.resolver().resolve(root))
    assert sum(r.changed for r in report.repositories) >= 1, report
    assert served(indexed) == _ME
    assert_equals_fresh(indexed, settings, root, tmp_path, "upgraded")


_BOOM = _lines(
    "import pathlib",
    'pathlib.Path(__file__).with_name("EXECUTED").write_text("ran")',
    "raise SystemExit(1)",
)
_BOOM_APP = _lines(
    "from boom import *",
    "import requests",
    "def load(): return requests.get('https://api.example.com/users')",
)


def test_parsing_a_star_import_never_imports_its_module(
    workspace: Workspace, tmp_path: Path
) -> None:
    parsed = PythonParser().parse(Path("app.py"), _BOOM_APP)
    assert [r for r in parsed.references if r.target_kind in _HTTP_KINDS]
    build(workspace, tmp_path, {"boomrepo": {"boom.py": _BOOM, "app.py": _BOOM_APP}})
    assert list(tmp_path.rglob("EXECUTED")) == []
    assert "boom" not in sys.modules


def test_the_http_reader_runs_and_imports_nothing_it_reads() -> None:
    tree = ast.parse(Path(http_python.__file__).read_text(encoding="utf-8"))
    allowed = {"__future__", "ast", "json", "re", "collections", "collections.abc", "dataclasses"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "a relative import"
            modules = [node.module or ""]
        else:
            continue
        for module in modules:
            assert module in allowed or module.split(".")[0] == "repoatlas", module
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert not called & {"__import__", "exec", "eval", "compile"}
    assert not [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in {"importlib", "builtins"}
    ]
