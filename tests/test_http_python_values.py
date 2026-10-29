"""A route path or prefix may name a constant the module composes from what it proves.

``USERS = f"{BASE}/users"`` and ``P = BASE + "/v1"`` stand for the text they build, because they are
bound once, from literals and earlier constants, before the line that reads them. A call, an
environment variable, ``.format``, ``%``, a conditional expression, a conversion, a name bound twice
or conditionally, a class attribute or a function local is not read: the module's reader never runs
the program, and an unknown value is refused with a reason (internal, never stored), not guessed.
Half of this file is what is refused. The other half is what the global pass does with the
composed constants an imported module offers: followed through other modules, relative to the module
that states them, a bounded number of times, a cycle refused.
"""

from __future__ import annotations

import ast
import json
import sys
import textwrap
from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.graph import py_routes
from repoatlas.graph.py_routes import CONSTANT_UNRESOLVED, PROMOTED, UNREADABLE_MOUNT
from repoatlas.models.parsing import ParsedReference
from repoatlas.parsers import http_python
from repoatlas.parsers.http_facts import (
    BASE_UNKNOWN,
    PENDING_FACT,
    PYTHON_CONSTANTS,
    PYTHON_MOUNT,
    PYTHON_RECEIVER,
    PYTHON_ROUTE,
)
from repoatlas.parsers.http_python import PythonHttp
from repoatlas.parsers.interpreter import interpreter_tag
from repoatlas.parsers.python import PythonParser
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build
from tests.incremental_corpus import assert_equals_fresh, edit, sync_all
from tests.test_http_python import _parse, _served
from tests.test_http_python_clients import _claims as _client_claims
from tests.test_http_python_clients import _facts as _client_facts
from tests.test_http_python_routers import line, outcomes, served

STAR = "from helpers import *"


def d(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def _roles(source: str, role: str) -> list[ParsedReference]:
    return [r for r in _parse(source).references if r.meta.get(PENDING_FACT) == role]


def _read(source: str) -> tuple[PythonHttp, list[ParsedReference]]:
    """The reader, after it has read the routes of every module-level function."""
    tree = ast.parse(textwrap.dedent(source))
    http = PythonHttp(tree)
    found: list[ParsedReference] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.extend(http.route_references(node, node.name))
    return http, found


def _reasons(refusals: list[tuple[int, str]]) -> list[str]:
    return [reason for _, reason in refusals]


def _claim(source: str, role: str) -> ParsedReference:
    [found] = _roles(source, role)
    return found


#: One router with no mount: its routes are claims, and a path it cannot read is refused.
ROUTER = "from fastapi import APIRouter\n"
TAIL = "router = APIRouter()\n@router.get(P)\ndef h(): ...\n"


# -- what is read ---------------------------------------------------------------------------


def test_a_literal_route_path_and_its_claim_are_unchanged() -> None:
    source = d("""
        from fastapi import APIRouter
        router = APIRouter(prefix="/users")
        @router.get("/me")
        def me(): ...
        @router.get("/a" + "/b")
        def joined(): ...
    """)
    me, joined = _roles(source, PYTHON_ROUTE)
    assert me.target_key == "GET /me"
    assert me.meta == {
        PENDING_FACT: PYTHON_ROUTE,
        "router": "router",
        "family": "fastapi",
        "method": "GET",
        "path": '[["t", "/me"]]',
        "provenance": "exact",
    }
    assert (me.line_start, me.line_end) == (3, 3)
    assert joined.meta["path"] == '[["t", "/a/b"]]'
    assert "values" not in joined.meta
    # A literal prefix carries no source, and a module of literals offers nothing.
    assert _claim(source, PYTHON_RECEIVER).meta == {
        PENDING_FACT: PYTHON_RECEIVER,
        "kind": "fastapi-router",
        "prefix": '"/users"',
        "usable": "true",
        "mounts": "[]",
    }
    assert _roles(source, PYTHON_CONSTANTS) == []


def test_a_local_literal_constant_path_is_read_as_before() -> None:
    source = d("""
        from fastapi import APIRouter
        BASE = "/api"
        router = APIRouter()
        @router.get(BASE)
        def api(): ...
        @router.get(f"{BASE}/users")
        def users(): ...
    """)
    api, users = _roles(source, PYTHON_ROUTE)
    assert api.target_key == "GET /api"
    assert api.meta["path"] == '[["t", "/api"]]'
    assert api.meta["provenance"] == "exact"
    assert api.meta["values"] == '[["BASE", 2]]'
    # Adjacent texts are not merged: the stored pieces are the ones written.
    assert users.target_key == "GET /api/users"
    assert users.meta["path"] == '[["t", "/api"], ["t", "/users"]]'
    assert users.meta["provenance"] == "exact"
    assert users.meta["values"] == '[["BASE", 2]]'


def test_a_local_fstring_constant_names_the_route() -> None:
    source = d("""
        from fastapi import APIRouter
        BASE = "/api"
        USERS = f"{BASE}/users"
        router = APIRouter()
        @router.get(USERS)
        def users(): ...
    """)
    claim = _claim(source, PYTHON_ROUTE)
    assert claim.target_key == "GET /api/users"
    assert claim.meta["path"] == '[["t", "/api"], ["t", "/users"]]'
    assert claim.meta["provenance"] == "composed"
    assert claim.meta["values"] == '[["BASE", 2], ["USERS", 3]]'
    assert (claim.line_start, claim.line_end) == (5, 5)
    # The composed constant is offered whole, beside the literal one; the literal key is as ever.
    offered = _claim(source, PYTHON_CONSTANTS)
    assert json.loads(offered.meta["constants"]) == {"BASE": ["/api", 2]}
    assert json.loads(offered.meta["composed"]) == {
        "USERS": [[["t", "/api"], ["t", "/users"]], 3, [["BASE", 2]]]
    }
    assert (offered.line_start, offered.line_end) == (2, 3)


def test_a_concatenated_constant_names_the_route() -> None:
    source = d("""
        from fastapi import APIRouter
        BASE = "/api"
        P = BASE + "/v1"
        router = APIRouter()
        @router.get(P)
        def one(): ...
        @router.get(P + "/items")
        def two(): ...
    """)
    one, two = _roles(source, PYTHON_ROUTE)
    assert one.target_key == "GET /api/v1"
    assert one.meta["path"] == '[["t", "/api"], ["t", "/v1"]]'
    assert one.meta["provenance"] == "composed"
    assert one.meta["values"] == '[["BASE", 2], ["P", 3]]'
    assert two.target_key == "GET /api/v1/items"
    assert two.meta["path"] == '[["t", "/api"], ["t", "/v1"], ["t", "/items"]]'
    assert two.meta["provenance"] == "composed"


def _chain(last: int) -> str:
    rows = ['C0 = "/c0"', *(f'C{i} = f"{{C{i - 1}}}/{i}"' for i in range(1, last + 1))]
    route = f"router = APIRouter()\n@router.get(C{last})\ndef h(): ...\n"
    return ROUTER + "\n".join(rows) + "\n" + route


def test_a_constant_chain_at_the_depth_bound_is_read() -> None:
    source = _chain(8)
    claim = _claim(source, PYTHON_ROUTE)
    assert claim.target_key == "GET /c0/1/2/3/4/5/6/7/8"
    assert claim.meta["provenance"] == "composed"
    assert len(json.loads(claim.meta["path"])) == 9
    assert [name for name, _ in json.loads(claim.meta["values"])] == [f"C{i}" for i in range(9)]
    http, found = _read(source)
    assert http.value_refusals.get("C8") is None
    assert len(found) == 1
    offered = json.loads(_claim(source, PYTHON_CONSTANTS).meta["composed"])
    assert sorted(offered) == [f"C{i}" for i in range(1, 9)]


def test_a_constant_chain_past_the_depth_bound_is_refused() -> None:
    source = _chain(9)
    http, found = _read(source)
    assert http.value_refusals["C9"] == "depth-exceeded"
    assert "C8" not in http.value_refusals
    assert found == []
    assert _reasons(http.path_refusals) == ["unknown-name"]
    assert _roles(source, PYTHON_ROUTE) == []
    offered = json.loads(_claim(source, PYTHON_CONSTANTS).meta["composed"])
    assert "C9" not in offered and "C8" in offered


# -- what is refused ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ['os.getenv("X", "/api")', 'os.environ.get("X", "/api")', 'os.environ["X"]'],
)
def test_an_environment_value_never_names_a_route_or_prefix(value: str) -> None:
    source = d(f"""
        import os
        from fastapi import APIRouter, FastAPI
        VALUE = {value}
        app = FastAPI()
        routed = APIRouter()
        mounted = APIRouter()
        other = APIRouter(prefix=VALUE)
        @routed.get(VALUE)
        def a(): ...
        app.include_router(mounted, prefix=VALUE)
    """)
    http, found = _read(source)
    assert http.value_refusals["VALUE"] == "unsupported-expression"
    assert found == []
    assert _reasons(http.path_refusals) == ["unknown-name"]
    assert http.prefix_refusals == [
        (line(source, "other = APIRouter"), "unknown-name"),
        (line(source, "include_router"), "unknown-name"),
    ]
    assert _served(source) == set()
    receivers = {r.target_key: r for r in _roles(source, PYTHON_RECEIVER)}
    assert set(receivers) == {"app", "routed", "mounted"}
    assert receivers["mounted"].meta["usable"] == "false"


@pytest.mark.parametrize(
    ("expression", "reason"),
    [
        ("get_path()", "unsupported-expression"),
        ('"/x/{}".format("a")', "unsupported-expression"),
        ('"/x/%s" % "a"', "unsupported-expression"),
        ('"/x".upper()', "unsupported-expression"),
        ('"/".join(["a", "b"])', "unsupported-expression"),
        ("settings.PATH", "unknown-name"),
        ('"/a" if FLAG else "/b"', "unsupported-expression"),
        ("8080", "not-a-string"),
    ],
)
def test_runtime_dependent_values_are_refused(expression: str, reason: str) -> None:
    route = f"@router.get({expression})\ndef h(): ...\n"
    source = f"{ROUTER}settings = load()\nrouter = APIRouter()\n{route}"
    http, found = _read(source)
    assert found == []
    assert _reasons(http.path_refusals) == [reason]
    assert _roles(source, PYTHON_ROUTE) == []


_REBOUND = [
    'P = "/a"\nP = "/b"\n',
    'P = "/a"\nP += "x"\n',
    'P = "/a"\ndef g(P): ...\n',
    'P = "/a"\ndef g():\n    global P\n    P = "/z"\n',
    'BASE = "/a"\nP = f"{BASE}/b"\nP = "/c"\n',
]


@pytest.mark.parametrize("binding", _REBOUND)
def test_a_reassigned_name_is_refused(binding: str) -> None:
    source = ROUTER + binding + TAIL
    http, found = _read(source)
    assert found == []
    assert http.value_refusals["P"] == "rebound"
    assert _reasons(http.path_refusals) == ["rebound"]


_CONDITIONAL = {
    "try-except": ('try:\n    P = "/a"\nexcept Exception:\n    P = "/b"\n', "rebound"),
    # Both branches bind a literal: the alternatives reader sees it, this one must not.
    "if-else": ('if FLAG:\n    P = "/a"\nelse:\n    P = "/b"\n', "rebound"),
    "if-only": ('if FLAG:\n    P = "/a"\n', "unknown-name"),
    "for": ('for P in ("/a",):\n    pass\n', "unknown-name"),
    "with": ("with ctx() as P:\n    pass\n", "unknown-name"),
}


@pytest.mark.parametrize("case", list(_CONDITIONAL))
def test_a_conditionally_bound_name_is_refused(case: str) -> None:
    binding, reason = _CONDITIONAL[case]
    source = ROUTER + binding + TAIL
    http, found = _read(source)
    assert found == []
    assert _reasons(http.path_refusals) == [reason]


def test_a_name_a_star_may_rebind_is_refused() -> None:
    after = f'P = "/a"\n{STAR}\nfrom fastapi import APIRouter\n{TAIL}'
    http, found = _read(after)
    assert http.star_unproven == {"P": "star-after-binding"}
    assert found == []
    assert http.value_refusals["P"] == "star-may-rebind"
    assert http.path_refusals == [(line(after, "@router.get"), "star-may-rebind")]

    # The same module with the star first keeps its names proven.
    first = f'{STAR}\nfrom fastapi import APIRouter\nP = "/a"\n{TAIL}'
    http, found = _read(first)
    assert http.star_unproven == {}
    assert http.path_refusals == []
    assert _claim(first, PYTHON_ROUTE).meta["path"] == '[["t", "/a"]]'

    # A composed constant reading a name the star may rebind is refused for that reason...
    below = f'BASE = "/a"\n{STAR}\nfrom fastapi import APIRouter\nP = f"{{BASE}}/u"\n{TAIL}'
    http, found = _read(below)
    assert http.value_refusals["P"] == "star-may-rebind"
    assert found == []
    # ...and read from a star-first module it is composed as ever.
    above = f'{STAR}\nfrom fastapi import APIRouter\nBASE = "/a"\nP = f"{{BASE}}/u"\n{TAIL}'
    assert _claim(above, PYTHON_ROUTE).meta["path"] == '[["t", "/a"], ["t", "/u"]]'

    # A name only a star could bind is never read.
    unbound = f"{STAR}\nfrom fastapi import APIRouter\nrouter = APIRouter()\n"
    unbound += "@router.get(Q)\ndef h(): ...\n"
    http, found = _read(unbound)
    assert found == []
    assert _reasons(http.path_refusals) == ["unknown-name"]


# -- prefixes -------------------------------------------------------------------------------


def test_a_router_prefix_from_an_fstring_constant_serves_its_routes() -> None:
    source = d("""
        from fastapi import APIRouter, FastAPI
        BASE = "/api"
        PREFIX = f"{BASE}/users"
        app = FastAPI()
        router = APIRouter(prefix=PREFIX)
        other = APIRouter(prefix=f"{BASE}/items")
        @router.get("/me")
        def me(): ...
        @other.get("")
        def items(): ...
        app.include_router(router)
        app.include_router(other)
    """)
    assert _served(source) == {"GET /api/users/me", "GET /api/items"}
    receivers = {r.target_key: r for r in _roles(source, PYTHON_RECEIVER)}
    assert receivers["router"].meta["prefix"] == '"/api/users"'
    assert receivers["router"].meta["prefix_from"] == '[["BASE", 2], ["PREFIX", 3]]'
    assert receivers["other"].meta["prefix_from"] == '[["BASE", 2]]'
    http, _ = _read(source)
    assert (5, "receiver", (("BASE", 2), ("PREFIX", 3))) in http.prefix_sources


def test_an_include_router_prefix_from_a_constant_is_read() -> None:
    local = d("""
        from fastapi import APIRouter, FastAPI
        API = "/api"
        V1 = f"{API}/v1"
        app = FastAPI()
        router = APIRouter(prefix="/users")
        @router.get("/me")
        def me(): ...
        app.include_router(router, prefix=V1)
    """)
    assert _served(local) == {"GET /api/v1/users/me"}

    imported = d("""
        from fastapi import FastAPI
        from svc.routes import users
        API = "/api"
        V1 = f"{API}/v1"
        app = FastAPI()
        app.include_router(users, prefix=V1)
    """)
    mount = _claim(imported, PYTHON_MOUNT)
    assert (mount.meta["readable"], mount.meta["prefix"]) == ("true", '"/api/v1"')
    assert mount.meta["prefix_from"] == '[["API", 3], ["V1", 4]]'

    factory = d("""
        from fastapi import FastAPI
        from svc.routes import users
        API = "/api"
        def create_app():
            app = FastAPI()
            app.include_router(users, prefix=f"{API}/v1")
            return app
    """)
    mount = _claim(factory, PYTHON_MOUNT)
    assert mount.meta["owner"] == "create_app.app"
    assert (mount.meta["readable"], mount.meta["prefix"]) == ("true", '"/api/v1"')
    assert mount.meta["prefix_from"] == '[["API", 3]]'
    # A factory runs when called, after the module: a constant bound below it is read too.
    below = d("""
        from fastapi import FastAPI
        from svc.routes import users
        def create_app():
            app = FastAPI()
            app.include_router(users, prefix=API)
            return app
        API = "/api"
    """)
    mount = _claim(below, PYTHON_MOUNT)
    assert (mount.meta["readable"], mount.meta["prefix"]) == ("true", '"/api"')


def test_a_flask_url_prefix_from_a_constant_is_read() -> None:
    source = d("""
        from flask import Blueprint, Flask
        ROOT = "/shop"
        PAGES = f"{ROOT}/pages"
        app = Flask(__name__)
        pages = Blueprint("pages", __name__, url_prefix=PAGES)
        admin = Blueprint("admin", __name__)
        @pages.route("/about")
        def about(): ...
        @admin.route("/panel")
        def panel(): ...
        app.register_blueprint(pages)
        app.register_blueprint(admin, url_prefix=f"{ROOT}/admin")
    """)
    assert _served(source) == {"GET /shop/pages/about", "GET /shop/admin/panel"}


_FLASK_ROOT = d("""
    from flask import Blueprint, Flask
    DECL
    app = Flask(__name__)
    pages = Blueprint("pages", __name__, url_prefix=EXPR)
    @pages.route("/about")
    def about(): ...
    app.register_blueprint(pages)
""")
_FLASK_ROOT_MOUNT = d("""
    from flask import Blueprint, Flask
    DECL
    app = Flask(__name__)
    pages = Blueprint("pages", __name__)
    @pages.route("/about")
    def about(): ...
    app.register_blueprint(pages, url_prefix=EXPR)
""")
_FASTAPI_ROOT = d("""
    from fastapi import APIRouter, FastAPI
    DECL
    app = FastAPI()
    router = APIRouter()
    @router.get("/x")
    def x(): ...
    app.include_router(router, prefix=EXPR)
""")
_FASTAPI_ROOT_OWN = d("""
    from fastapi import APIRouter, FastAPI
    DECL
    app = FastAPI()
    router = APIRouter(prefix=EXPR)
    @router.get("/x")
    def x(): ...
    app.include_router(router)
""")


def _twins(template: str, value: str) -> tuple[str, str]:
    """The template with the prefix a constant holds, and with the same prefix written out."""
    constant = template.replace("DECL", f'P = "{value}"').replace("EXPR", "P")
    literal = template.replace("DECL", "").replace("EXPR", f'"{value}"')
    return constant, literal


@pytest.mark.parametrize("template", [_FLASK_ROOT, _FLASK_ROOT_MOUNT])
def test_a_root_constant_prefix_behaves_as_its_literal(template: str) -> None:
    constant, literal = _twins(template, "/")
    assert _served(constant) == _served(literal) == {"GET /about"}
    # FastAPI refuses "/" as a prefix whichever way it is written.
    for fastapi in (_FASTAPI_ROOT, _FASTAPI_ROOT_OWN):
        constant, literal = _twins(fastapi, "/")
        assert _served(constant) == _served(literal) == set()
        http, _ = _read(constant)
        assert "invalid-prefix" in _reasons(http.prefix_refusals)


@pytest.mark.parametrize(
    "template", [_FLASK_ROOT, _FLASK_ROOT_MOUNT, _FASTAPI_ROOT, _FASTAPI_ROOT_OWN]
)
def test_a_trailing_slash_constant_prefix_is_refused_as_its_literal(template: str) -> None:
    constant, literal = _twins(template, "/api/")
    assert _served(constant) == _served(literal) == set()
    # The control: the same prefix without the slash is served.
    control, _ = _twins(template, "/api")
    assert len(_served(control)) == 1


def test_the_length_bound() -> None:
    def module(text: str, prefix: str = "BASE") -> str:
        return d(f'''
            from fastapi import APIRouter, FastAPI
            BASE = "{text}"
            V = f"{{BASE}}"
            app = FastAPI()
            router = APIRouter(prefix={prefix})
            @router.get("/x")
            def x(): ...
            app.include_router(router)
        ''')

    accepted = "/" + "a" * 255
    source = module(accepted)
    http, _ = _read(source)
    assert len(accepted) == 256
    assert "V" not in http.value_refusals
    assert http.prefix_refusals == []
    assert _served(source) == {f"GET {accepted}/x"}

    refused = "/" + "a" * 256
    source = module(refused)
    http, _ = _read(source)
    assert http.value_refusals["V"] == "too-long"
    assert _reasons(http.prefix_refusals) == ["too-long"]
    assert _served(source) == set()

    # A prefix with no name in it is read as it always was, however long.
    written = "/" + "a" * 299
    source = module(refused, prefix=f'"{written}"')
    assert _served(source) == {f"GET {written}/x"}


def test_a_comprehension_mount_with_a_constant_prefix_is_unrolled_and_served() -> None:
    head = d("""
        from fastapi import APIRouter, FastAPI
        API = "/api"
        app = FastAPI()
        users = APIRouter(prefix="/users")
        items = APIRouter(prefix="/items")
        @users.get("/me")
        def me(): ...
        @items.post("")
        def create_item(): ...
        routers = [users, items]
    """)
    routes = {"GET /api/v1/users/me", "POST /api/v1/items"}
    comprehension = head + 'V1 = f"{API}/v1"\n[app.include_router(r, prefix=V1) for r in routers]\n'
    assert _served(comprehension) == routes
    loop = head + 'V1 = f"{API}/v1"\nfor r in routers: app.include_router(r, prefix=V1)\n'
    assert _served(loop) == routes
    # Read at the call's own place: a constant bound below the comprehension is not yet bound.
    late = head + '[app.include_router(r, prefix=V1) for r in routers]\nV1 = f"{API}/v1"\n'
    assert _served(late) == set()
    http, _ = _read(late)
    assert _reasons(http.prefix_refusals) == ["used-before-binding"] * 2


def test_a_value_used_before_its_binding_is_refused() -> None:
    above = d("""
        from fastapi import APIRouter
        BASE = "/api"
        router = APIRouter()
        @router.get(USERS)
        def users(): ...
        USERS = f"{BASE}/users"
    """)
    http, found = _read(above)
    assert found == []
    assert _reasons(http.path_refusals) == ["used-before-binding"]
    below = d("""
        from fastapi import APIRouter
        BASE = "/api"
        USERS = f"{BASE}/users"
        router = APIRouter()
        @router.get(USERS)
        def users(): ...
    """)
    assert _read(below)[0].path_refusals == []
    assert len(_roles(below, PYTHON_ROUTE)) == 1

    mount = d("""
        from fastapi import APIRouter, FastAPI
        app = FastAPI()
        router = APIRouter()
        @router.get("/x")
        def x(): ...
        app.include_router(router, prefix=P)
        P = "/api"
    """)
    assert _served(mount) == set()
    assert _reasons(_read(mount)[0].prefix_refusals) == ["used-before-binding"]
    ordered = mount.replace('app.include_router(router, prefix=P)\nP = "/api"\n', "")
    ordered = ordered.replace("app = FastAPI()", 'P = "/api"\napp = FastAPI()')
    ordered += "app.include_router(router, prefix=P)\n"
    assert _served(ordered) == {"GET /api/x"}

    constructed = d("""
        from fastapi import APIRouter, FastAPI
        app = FastAPI()
        router = APIRouter(prefix=P)
        P = "/api"
    """)
    assert _reasons(_read(constructed)[0].prefix_refusals) == ["used-before-binding"]


@pytest.mark.parametrize(
    ("rows", "reason"),
    [
        ('U = f"{LATER}/u"\nLATER = "/l"\n', "used-before-binding"),
        ('U = f"{ROOT}/u"\nfrom conf import ROOT\n', "used-before-binding"),
        # A composed constant is only known once its own statement has run.
        ('U = f"{LATER}/u"\nLATER = f"{BASE}/l"\nBASE = "/b"\n', "unknown-name"),
    ],
)
def test_a_value_read_before_its_names_are_bound_is_refused(rows: str, reason: str) -> None:
    http, _ = _read(rows)
    assert http.value_refusals["U"] == reason
    assert "U" not in http._values


@pytest.mark.parametrize("suffix", ["!r", "!s", "!a", ":>10", ":d", "="])
def test_conversion_or_format_spec_is_refused(suffix: str) -> None:
    source = (
        ROUTER
        + f'BASE = "/b"\nU = f"{{BASE{suffix}}}/u"\nrouter = APIRouter()\n'
        + (f'@router.get(f"{{BASE{suffix}}}/v")\ndef h(): ...\n')
    )
    http, found = _read(source)
    assert http.value_refusals["U"] == "conversion-or-format-spec"
    assert found == []
    assert _reasons(http.path_refusals) == ["conversion-or-format-spec"]


def test_an_imported_prefix_stays_unreadable(workspace: Workspace, tmp_path: Path) -> None:
    source = d("""
        from fastapi import APIRouter, FastAPI
        from conf import PREFIX
        app = FastAPI()
        router = APIRouter()
        own = APIRouter(prefix=PREFIX)
        @router.get("/x")
        def x(): ...
        app.include_router(router, prefix=PREFIX)
    """)
    http, _ = _read(source)
    assert _reasons(http.prefix_refusals) == ["prefix-imported", "prefix-imported"]
    assert _served(source) == set()
    receivers = {r.target_key: r for r in _roles(source, PYTHON_RECEIVER)}
    assert set(receivers) == {"app", "router"}
    assert receivers["router"].meta["usable"] == "false"

    qualified = source.replace("from conf import PREFIX", "import conf").replace(
        "prefix=PREFIX", "prefix=conf.PREFIX"
    )
    assert _reasons(_read(qualified)[0].prefix_refusals) == ["prefix-imported"] * 2
    assert _served(qualified) == set()

    # The mount of an imported router is recorded unreadable, as it was.
    claimed = d("""
        from fastapi import FastAPI
        from conf import PREFIX
        from svc.routes import users
        app = FastAPI()
        app.include_router(users, prefix=PREFIX)
    """)
    mount = _claim(claimed, PYTHON_MOUNT)
    assert (mount.meta["readable"], mount.meta["prefix"]) == ("false", "null")
    assert "prefix_from" not in mount.meta

    # So the router it mounts is served nowhere, however readable its own path.
    app = d("""
        from fastapi import FastAPI
        from src.conf import PREFIX
        from src.routes import router
        app = FastAPI()
        app.include_router(router, prefix=PREFIX)
    """)
    files = _files(app=app)
    files["svc"]["src/conf.py"] = 'PREFIX = "/api"\n'
    indexed = build(workspace, tmp_path, files)
    assert served(indexed) == set()
    assert _outcome_of(indexed, "users") == UNREADABLE_MOUNT


def test_a_class_attribute_and_a_function_local_are_refused() -> None:
    attribute = ROUTER + 'class Config:\n    P = "/api"\n' + TAIL
    http, found = _read(attribute)
    assert found == []
    assert _reasons(http.path_refusals) == ["unknown-name"]
    local = ROUTER + 'def configure():\n    P = "/api"\n' + TAIL
    http, found = _read(local)
    assert found == []
    assert _reasons(http.path_refusals) == ["unknown-name"]

    # A factory's own binding of a name is never the module's.
    local_name = d("""
        from fastapi import FastAPI
        from svc.routes import users
        def create_app():
            API = "/local"
            app = FastAPI()
            app.include_router(users, prefix=API)
            return app
    """)
    http, _ = _read(local_name)
    assert http.prefix_refusals == [(line(local_name, "include_router"), "shadowed-in-function")]
    mount = _claim(local_name, PYTHON_MOUNT)
    assert (mount.meta["readable"], mount.meta["prefix"]) == ("false", "null")
    with_module = local_name.replace("def create_app", 'API = "/api"\ndef create_app')
    http, _ = _read(with_module)
    assert _reasons(http.prefix_refusals) == ["shadowed-in-function"]


def test_a_composed_constant_base_resolves_a_client_url() -> None:
    source = d("""
        import requests
        HOST = "https://h.example"
        BASE = f"{HOST}/api"
        def users():
            return requests.get(f"{BASE}/users")
    """)
    [claim] = _client_claims(source)
    assert claim.target_key == "GET https://h.example/api/users"
    assert claim.meta["origin"] == "https://h.example"
    assert json.loads(claim.meta["segments"]) == ["api", "users"]
    assert claim.meta["base_at"] == "3"
    assert "base" not in claim.meta
    assert _client_facts(source) == []
    # The control: a module whose BASE is something no reader can know is still counted, with
    # no origin, as before.
    control = source.replace('f"{HOST}/api"', "join(HOST)")
    [counted] = _client_claims(control)
    assert counted.target_key == "GET {BASE}/users"
    assert counted.meta["base"] == BASE_UNKNOWN
    assert "origin" not in counted.meta
    # Reading the composed constant offers and stores nothing new.
    offered = _claim(source, PYTHON_CONSTANTS)
    assert json.loads(offered.meta["constants"]) == {"HOST": ["https://h.example", 2]}
    assert set(json.loads(offered.meta["composed"])) == {"BASE"}


def test_a_literal_only_module_offers_exactly_as_before() -> None:
    source = 'BASE = "/api"\nUSERS = "/users"\nNOTE = "two words"\nX = BASE\n'
    [offered] = _roles(source, PYTHON_CONSTANTS)
    expected = json.dumps({"BASE": ["/api", 1], "USERS": ["/users", 2]}, sort_keys=True)
    # `X = BASE` is composed (a pure alias), the others are literals.
    assert offered.meta["constants"] == expected
    assert (offered.line_start, offered.line_end) == (1, 4)
    literal_only = 'BASE = "/api"\nUSERS = "/users"\n'
    [offered] = _roles(literal_only, PYTHON_CONSTANTS)
    assert offered.meta == {PENDING_FACT: PYTHON_CONSTANTS, "constants": expected}
    assert (offered.line_start, offered.line_end) == (1, 2)


def test_composed_constants_are_offered_only_when_a_path_could_be_made_of_them() -> None:
    names = [f"V{i}" for i in range(70)]
    rows = [f'{name} = f"{{BASE}}/{i}"' for i, name in enumerate(names)]
    module = 'from conf import X\nBASE = "/b"\n' + "\n".join(rows) + "\n"
    [offered] = _roles(module, PYTHON_CONSTANTS)
    # The first 64 in line order, and the claim spans the literal and the composed ones.
    assert sorted(json.loads(offered.meta["composed"])) == sorted(names[:64])
    assert json.loads(offered.meta["constants"]) == {"BASE": ["/b", 2]}
    assert (offered.line_start, offered.line_end) == (2, 66)

    many = "".join("{NAME}" for _ in range(63))
    module = d(f"""
        from a_module_with_a_long_name_for_the_entry import NAME
        BASE = "/b"
        SPACED = f"{{BASE}}/a b"
        ALIAS = NAME
        WIDE = f"/{many}"
        PLAIN = f"{{BASE}}/p"
    """)
    http, _ = _read(module)
    # All of them are values, readable here...
    assert set(http._values) == {"SPACED", "ALIAS", "WIDE", "PLAIN"}
    # ...and only the one a path could be made of is offered: not one with whitespace, not a bare
    # imported name, not an entry too large to store.
    [offered] = _roles(module, PYTHON_CONSTANTS)
    assert set(json.loads(offered.meta["composed"])) == {"PLAIN"}


# -- bounds and adversarial input ------------------------------------------------------------


def _chain_expression(count: int, anchor: ast.expr) -> ast.expr:
    """``"/a" + "/a" + ...`` with ``count`` operands, built node by node: no source to parse."""
    node: ast.expr = ast.copy_location(ast.Constant(value="/a"), anchor)
    for _ in range(count - 1):
        right = ast.copy_location(ast.Constant(value="/a"), anchor)
        node = ast.copy_location(ast.BinOp(left=node, op=ast.Add(), right=right), anchor)
    return node


def test_adversarial_inputs_are_refused_with_reasons() -> None:
    # A chain of 5000 constants: refused at the ninth, and the rest cannot read it.
    rows = ['C0 = "/a"', *(f'C{i} = f"{{C{i - 1}}}/x"' for i in range(1, 5001))]
    source = ROUTER + "\n".join(rows) + "\nrouter = APIRouter()\n@router.get(C5000)\ndef h(): ...\n"
    http, found = _read(source)
    assert http.value_refusals["C9"] == "depth-exceeded"
    assert http.value_refusals["C10"] == "unknown-name"
    assert http.value_refusals["C5000"] == "unknown-name"
    assert "C8" not in http.value_refusals
    assert found == []
    assert _reasons(http.path_refusals) == ["unknown-name"]

    # An f-string of 5000 substitutions: 64 pieces are read, a 65th is too many.
    def fstring(count: int) -> str:
        body = "{BASE}" * count
        route = "router = APIRouter()\n@router.get(W)\ndef h(): ...\n"
        return ROUTER + f'BASE = "/a"\nW = f"{body}"\n' + route

    http, found = _read(fstring(5000))
    assert http.value_refusals["W"] == "too-many-parts"
    assert found == []
    assert _reasons(http.path_refusals) == ["unknown-name"]
    assert "W" not in _read(fstring(64))[0].value_refusals
    assert _read(fstring(65))[0].value_refusals["W"] == "too-many-parts"

    # 5000 operands of `+`, in a value and in a route path: deeper than a path is written.
    tree = ast.parse(ROUTER + 'X = "/a"\nrouter = APIRouter()\n@router.get("/x")\ndef h(): ...\n')
    assign, function = tree.body[1], tree.body[3]
    assert isinstance(assign, ast.Assign) and isinstance(function, ast.FunctionDef)
    decorator = function.decorator_list[0]
    assert isinstance(decorator, ast.Call)
    assign.value = _chain_expression(5000, assign.value)
    decorator.args[0] = _chain_expression(5000, decorator.args[0])
    http = PythonHttp(tree)
    assert http.value_refusals["X"] == "nesting-too-deep"
    assert http.route_references(function, "h") == []
    assert http.path_refusals == [(decorator.lineno, "nesting-too-deep")]


def test_the_reader_executes_and_imports_nothing() -> None:
    # Statically: neither module calls what runs code or names what imports it.
    calls = {"eval", "exec", "compile", "__import__"}
    modules = {"importlib", "runpy", "subprocess"}
    for module in (http_python, py_routes):
        found: list[str] = []
        for node in ast.walk(ast.parse(Path(str(module.__file__)).read_text(encoding="utf-8"))):
            if isinstance(node, ast.Name) and node.id in calls | modules:
                found.append(node.id)
            elif isinstance(node, ast.Import):
                found.extend(a.name for a in node.names if a.name.split(".")[0] in modules)
            elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in modules:
                found.append(str(node.module))
        assert found == [], (module.__name__, found)

    # Dynamically: a module a path and a prefix name is never loaded by reading them.
    probe = "m4_probe_never_imported"
    assert probe not in sys.modules
    source = d(f"""
        import {probe} as probe
        from {probe} import PATH
        from fastapi import APIRouter, FastAPI
        app = FastAPI()
        router = APIRouter()
        mounted = APIRouter()
        @router.get(PATH)
        def a(): ...
        @router.get(probe.OTHER)
        def b(): ...
        app.include_router(mounted, prefix=probe.PREFIX)
    """)
    parsed = PythonParser().parse(Path("app.py"), source)
    assert probe not in sys.modules
    paths = sorted(
        r.meta["path"] for r in parsed.references if r.meta.get(PENDING_FACT) == PYTHON_ROUTE
    )
    assert paths == sorted(
        [json.dumps([["r", probe, "PATH"]]), json.dumps([["r", probe, "OTHER"]])]
    )


def test_the_parser_version_is_28() -> None:
    assert PythonParser.version == f"28+{interpreter_tag()}"


# -- the global pass --------------------------------------------------------------------------


_APP = d("""
    from fastapi import FastAPI
    from src.routes import router
    app = FastAPI()
    app.include_router(router)
""")
_ROUTES = d("""
    from fastapi import APIRouter
    from src.consts import USERS
    router = APIRouter()
    @router.get(USERS)
    def users(): ...
""")
_CONSTS = 'API = "/api"\nUSERS = f"{API}/users"\n'


def _files(
    consts: str = _CONSTS, routes: str = _ROUTES, app: str = _APP
) -> dict[str, dict[str, str]]:
    return {
        "svc": {
            "src/__init__.py": "",
            "src/consts.py": consts,
            "src/routes.py": routes,
            "src/app.py": app,
        }
    }


def _route(
    route: str, handler: str = "users", path: str = "src/routes.py"
) -> tuple[str, str, str, str, int | None]:
    return ("svc", path, handler, route, None)


def _outcome_of(workspace: Workspace, handler: str) -> str:
    [found] = [v for (_, name, _), v in outcomes(workspace).items() if name == handler]
    return found


def test_an_imported_composed_constant_serves_the_route(
    workspace: Workspace, tmp_path: Path
) -> None:
    indexed = build(workspace, tmp_path, _files())
    assert served(indexed) == {_route("GET /api/users")}
    assert _outcome_of(indexed, "users") == PROMOTED


def test_a_relative_import_inside_a_composed_constant_resolves_from_its_own_module(
    workspace: Workspace, tmp_path: Path
) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/a/__init__.py": "",
        "pkg/a/base.py": 'ROOT = "/right"\n',
        "pkg/a/consts.py": 'from .base import ROOT\nAPI = f"{ROOT}/api"\n',
        # A different ROOT beside the importer: the decoy a wrong origin would find.
        "pkg/b/__init__.py": "",
        "pkg/b/base.py": 'ROOT = "/decoy"\n',
        "pkg/b/routes.py": d("""
            from fastapi import APIRouter
            from pkg.a.consts import API
            router = APIRouter()
            @router.get(API)
            def api(): ...
        """),
        "pkg/main.py": d("""
            from fastapi import FastAPI
            from pkg.b.routes import router
            app = FastAPI()
            app.include_router(router)
        """),
    }
    indexed = build(workspace, tmp_path, {"svc": files})
    assert served(indexed) == {_route("GET /right/api", "api", "pkg/b/routes.py")}


def test_a_cross_module_cycle_is_unresolved(workspace: Workspace, tmp_path: Path) -> None:
    files = _files(
        routes=_ROUTES.replace("src.consts import USERS", "src.a import A").replace("USERS", "A")
    )
    files["svc"]["src/a.py"] = 'from src.b import B\nA = f"{B}/a"\n'
    files["svc"]["src/b.py"] = 'from src.a import A\nB = f"{A}/b"\n'
    indexed = build(workspace, tmp_path, files)
    assert served(indexed) == set()
    assert _outcome_of(indexed, "users") == CONSTANT_UNRESOLVED


def _hops(modules: int) -> dict[str, dict[str, str]]:
    """``V0`` in ``m0`` composed through ``modules - 1`` others, the last a literal."""
    files = _files(routes=_ROUTES.replace("src.consts import USERS", "src.m0 import V0"))
    files["svc"]["src/routes.py"] = files["svc"]["src/routes.py"].replace("USERS", "V0")
    for index in range(modules):
        name = f"V{index}"
        if index == modules - 1:
            files["svc"][f"src/m{index}.py"] = f'{name} = "/root"\n'
        else:
            below = f"V{index + 1}"
            files["svc"][f"src/m{index}.py"] = (
                f'from src.m{index + 1} import {below}\n{name} = f"{{{below}}}/{index}"\n'
            )
    return files


@pytest.mark.parametrize(("modules", "reaches"), [(4, True), (5, False)], ids=["four", "five"])
def test_a_chain_of_four_hops_is_served_and_five_is_unresolved(
    modules: int, reaches: bool, workspace: Workspace, tmp_path: Path
) -> None:
    assert py_routes.MAX_CONSTANT_HOPS == 4
    indexed = build(workspace, tmp_path, _hops(modules))
    if reaches:
        assert served(indexed) == {_route("GET /root/2/1/0")}
        assert _outcome_of(indexed, "users") == PROMOTED
    else:
        assert served(indexed) == set()
        assert _outcome_of(indexed, "users") == CONSTANT_UNRESOLVED


# -- end to end: incremental equals fresh ---------------------------------------------------


def test_editing_the_constant_module_moves_the_route(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    indexed = build(workspace, tmp_path, _files())
    root = tmp_path / "repos"
    assert served(indexed) == {_route("GET /api/users")}

    edit(root, "svc", "src/consts.py", 'API = "/v2"\nUSERS = f"{API}/users"\n')
    sync_all(indexed, root)
    assert served(indexed) == {_route("GET /v2/users")}
    assert_equals_fresh(indexed, settings, root, tmp_path, "values-literal")

    edit(root, "svc", "src/consts.py", 'API = "/v2"\nUSERS = f"{API}/people"\n')
    sync_all(indexed, root)
    assert served(indexed) == {_route("GET /v2/people")}
    assert_equals_fresh(indexed, settings, root, tmp_path, "values-composition")


_PREFIXED_ROUTES = d("""
    from fastapi import APIRouter
    router = APIRouter(prefix="/users")
    @router.get("/me")
    def me(): ...
""")
_PREFIXED_APP = d("""
    from fastapi import FastAPI
    from src.routes import router
    API = "/api"
    V1 = f"{API}/v1"
    app = FastAPI()
    app.include_router(router, prefix=V1)
""")


def test_editing_the_consumer_prefix_moves_the_routes(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    indexed = build(workspace, tmp_path, _files(routes=_PREFIXED_ROUTES, app=_PREFIXED_APP))
    root = tmp_path / "repos"
    assert served(indexed) == {_route("GET /api/v1/users/me", "me")}

    edit(root, "svc", "src/app.py", _PREFIXED_APP.replace('API = "/api"', 'API = "/edge"'))
    sync_all(indexed, root)
    assert served(indexed) == {_route("GET /edge/v1/users/me", "me")}
    assert_equals_fresh(indexed, settings, root, tmp_path, "values-prefix")

    # A prefix the reader cannot prove unserves the router, as any unreadable mount does.
    edit(root, "svc", "src/app.py", _PREFIXED_APP.replace('V1 = f"{API}/v1"', "V1 = load(API)"))
    sync_all(indexed, root)
    assert served(indexed) == set()
    assert_equals_fresh(indexed, settings, root, tmp_path, "values-prefix-unread")


def test_a_body_edit_in_the_constant_module_changes_nothing(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    consts = _CONSTS + "\n\ndef helper():\n    return 1\n"
    indexed = build(workspace, tmp_path, _files(consts=consts))
    root = tmp_path / "repos"
    before = served(indexed)
    assert before == {_route("GET /api/users")}
    edit(root, "svc", "src/consts.py", consts.replace("return 1", "return 2"))
    work = sync_all(indexed, root)
    assert served(indexed) == before
    assert work.repositories_invalidated == 0
    assert_equals_fresh(indexed, settings, root, tmp_path, "values-body")
