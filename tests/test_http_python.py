"""Static HTTP facts in Python source (HTTP linkage).

Half of this file is about what is found and half about what is refused, and the
second half matters more. ``.get("/hello")`` is a dictionary lookup far more
often than a request; a known URL *suffix* is not a known URL; a router's path is
decided by where it is mounted. Each of those is a place where a plausible reader
invents an HTTP fact, and a fact two repositories meet on is not a small error.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from repoatlas.models.parsing import EdgeType, EntityType, ParsedFile, ParsedReference
from repoatlas.parsers.http_facts import HTTP_CLIENT_CALL, PENDING_FACT
from repoatlas.parsers.python import PythonParser

_HTTP_KINDS = {EntityType.HTTP_ROUTE, EntityType.HTTP_DEPLOYMENT, EntityType.HTTP_ENDPOINT}


def _parse(source: str) -> ParsedFile:
    return PythonParser().parse(Path("app.py"), textwrap.dedent(source))


def _http(source: str) -> list[ParsedReference]:
    return [r for r in _parse(source).references if r.target_kind in _HTTP_KINDS]


def _targets(source: str, edge: EdgeType) -> set[str]:
    return {r.target_key for r in _http(source) if r.edge_type is edge}


def _served(source: str) -> set[str]:
    return _targets(source, EdgeType.SERVES_HTTP_ROUTE)


# -- FastAPI ---------------------------------------------------------------------------


def test_a_fastapi_route_is_served_by_its_handler_at_the_decorator() -> None:
    [reference] = _http(
        """\
        from fastapi import FastAPI

        app = FastAPI()


        @app.get("/hello/me")
        def hello():
            return {"hello": "me"}
        """
    )
    assert reference.edge_type is EdgeType.SERVES_HTTP_ROUTE
    assert reference.target_kind is EntityType.HTTP_ROUTE
    assert reference.target_key == "GET /hello/me"
    assert reference.source_symbol == "hello"
    assert (reference.line_start, reference.line_end) == (6, 6)
    assert reference.meta == {"source": "fastapi", "provenance": "exact"}


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
def test_every_fastapi_verb_maps_to_its_method(verb: str, method: str) -> None:
    source = f"from fastapi import FastAPI\napp = FastAPI()\n@app.{verb}('/x')\ndef h(): ...\n"
    assert _served(source) == {f"{method} /x"}


@pytest.mark.parametrize(
    "header",
    [
        "import fastapi\napp = fastapi.FastAPI(title='x')\n",
        "import fastapi as fa\napp = fa.FastAPI()\n",
        "from fastapi import FastAPI as Api\napp = Api()\n",
        "from fastapi import FastAPI\napp: FastAPI = FastAPI()\n",
    ],
)
def test_the_application_identity_may_be_spelled_several_ways(header: str) -> None:
    assert _served(header + "@app.get(path='/x')\nasync def h(): ...\n") == {"GET /x"}


def test_api_route_needs_its_methods_spelled_out() -> None:
    source = """\
    from fastapi import FastAPI
    app = FastAPI()
    @app.api_route("/both", methods=["GET", "post"])
    def both(): ...
    @app.api_route("/default")
    def default(): ...
    @app.api_route("/dynamic", methods=METHODS)
    def dynamic(): ...
    """
    assert _served(source) == {"GET /both", "POST /both"}


def test_a_router_route_is_composed_from_its_mounts() -> None:
    source = """\
    from fastapi import APIRouter, FastAPI

    app = FastAPI()
    router = APIRouter(prefix="/users")


    @router.get("/me")
    def me(): ...


    @router.get("")
    def index(): ...


    app.include_router(router, prefix="/api")
    app.include_router(router)
    """
    references = _http(source)
    assert {r.target_key for r in references} == {
        "GET /api/users/me",
        "GET /users/me",
        "GET /api/users",
        "GET /users",
    }
    assert {r.meta["provenance"] for r in references} == {"composed"}


@pytest.mark.parametrize(
    "tail",
    [
        # Never included here: served wherever another file mounts it.
        "",
        # Included with a prefix nothing here can read.
        "app.include_router(router, prefix=settings.API_PREFIX)\n",
        "app.include_router(router, **options)\n",
        # A prefix FastAPI itself rejects.
        "app.include_router(router, prefix='/api/')\n",
        # Nested in another router: its final path depends on that router's mount.
        "outer = APIRouter()\nouter.include_router(router)\napp.include_router(outer)\n",
    ],
)
def test_a_router_route_without_a_proven_mount_is_not_recorded(tail: str) -> None:
    source = (
        "from fastapi import APIRouter, FastAPI\napp = FastAPI()\n"
        "router = APIRouter(prefix='/users')\n@router.get('/me')\ndef me(): ...\n" + tail
    )
    assert _served(source) == set()


@pytest.mark.parametrize(
    "construction",
    [
        "router = APIRouter(prefix=PREFIX)",
        "router = APIRouter(prefix='/users/')",
        "router = APIRouter(prefix='users')",
        "router = APIRouter(**kw)",
    ],
)
def test_a_router_with_an_unreadable_prefix_yields_nothing(construction: str) -> None:
    source = (
        f"from fastapi import APIRouter, FastAPI\napp = FastAPI()\n{construction}\n"
        "@router.get('/me')\ndef me(): ...\napp.include_router(router)\n"
    )
    assert _served(source) == set()


def test_an_application_mounted_as_a_sub_application_yields_nothing() -> None:
    source = """\
    from fastapi import FastAPI
    app = FastAPI()
    sub = FastAPI()
    @sub.get("/inner")
    def inner(): ...
    @app.get("/outer")
    def outer(): ...
    app.mount("/sub", sub)
    """
    assert _served(source) == {"GET /outer"}


# -- Flask ---------------------------------------------------------------------------


def test_a_flask_route_defaults_to_get_only() -> None:
    """HEAD and OPTIONS are added by Flask implicitly; they are not declared here."""
    source = "from flask import Flask\napp = Flask(__name__)\n@app.route('/hello')\ndef h(): ...\n"
    assert _served(source) == {"GET /hello"}


def test_flask_methods_are_read_only_when_literal() -> None:
    source = """\
    from flask import Flask
    app = Flask(__name__)
    @app.route("/login", methods=["get", "POST"])
    def login(): ...
    @app.route("/dynamic", methods=allowed)
    def dynamic(): ...
    @app.post("/submit")
    def submit(): ...
    @app.get("/both", methods=["POST"])
    def invalid(): ...
    """
    assert _served(source) == {"GET /login", "POST /login", "POST /submit"}


def test_a_blueprint_route_is_composed_and_a_registration_prefix_overrides() -> None:
    source = """\
    from flask import Blueprint, Flask
    app = Flask(__name__)
    pages = Blueprint("pages", __name__, url_prefix="/pages")
    @pages.route("/about")
    def about(): ...
    @pages.route("/")
    def index(): ...
    app.register_blueprint(pages)
    app.register_blueprint(pages, url_prefix="/v2", name="pages_v2")
    """
    assert _served(source) == {"GET /pages/about", "GET /pages/", "GET /v2/about", "GET /v2/"}


@pytest.mark.parametrize(
    "tail",
    [
        "",
        "app.register_blueprint(pages, url_prefix=PREFIX)\n",
        "app.register_blueprint(pages, url_prefix='/v2/')\n",
        "outer = Blueprint('o', __name__)\nouter.register_blueprint(pages)\n",
    ],
)
def test_a_blueprint_without_a_readable_registration_yields_nothing(tail: str) -> None:
    source = (
        "from flask import Blueprint, Flask\napp = Flask(__name__)\n"
        "pages = Blueprint('pages', __name__, url_prefix='/pages')\n"
        "@pages.route('/about')\ndef about(): ...\n" + tail
    )
    assert _served(source) == set()


# -- identity must be proven ------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        # Not a framework object at all.
        "cache = {}\n@cache.get('/hello')\ndef h(): ...\n",
        "@arbitraryApp.get('/hello')\ndef h(): ...\n",
        "from mylib import FastAPI\napp = FastAPI()\n@app.get('/hello')\ndef h(): ...\n",
        # Rebound after construction, or bound twice.
        "from fastapi import FastAPI\napp = FastAPI()\napp = make()\n"
        "@app.get('/x')\ndef h(): ...\n",
        # Shadowed by a parameter somewhere in the module.
        "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/x')\ndef h(): ...\n"
        "def g(app): ...\n",
        # A star import could rebind anything.
        "from fastapi import FastAPI\nfrom helpers import *\napp = FastAPI()\n"
        "@app.get('/x')\ndef h(): ...\n",
        # An application factory: the route is inside a function.
        "from flask import Flask\ndef create():\n    app = Flask(__name__)\n"
        "    @app.route('/x')\n    def h(): ...\n    return app\n",
        # A method of a class.
        "from fastapi import FastAPI\napp = FastAPI()\nclass V:\n    @app.get('/x')\n"
        "    def h(self): ...\n",
        # A dynamic path.
        "from fastapi import FastAPI\napp = FastAPI()\n@app.get(PATH)\ndef h(): ...\n",
        "from fastapi import FastAPI\napp = FastAPI()\n@app.get(f'/u/{x}')\ndef h(): ...\n",
        "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/u/' + x)\ndef h(): ...\n",
        "from fastapi import FastAPI\napp = FastAPI()\n@app.get(*args)\ndef h(): ...\n",
    ],
)
def test_no_route_without_proven_framework_identity_and_a_literal_path(source: str) -> None:
    assert _http(source) == []


def test_a_route_path_built_from_literals_is_accepted() -> None:
    source = (
        "from fastapi import FastAPI\napp = FastAPI()\n"
        "@app.get('/hello' + '/me')\ndef a(): ...\n@app.get(f'/plain')\ndef b(): ...\n"
    )
    assert _served(source) == {"GET /hello/me", "GET /plain"}


# -- requests and httpx -----------------------------------------------------------------


def test_an_absolute_requests_call_names_an_endpoint_and_its_route() -> None:
    references = _http(
        """\
        import requests


        def get_me():
            return requests.get(
                "https://api.example.com/hello/me?source=cli",
                timeout=5,
            ).json()
        """
    )
    # The claim for the cross-repository join rides beside the facts
    # (tests/test_http_python_calls.py); the facts themselves are unchanged.
    facts = [r for r in references if PENDING_FACT not in r.meta]
    assert {(r.edge_type, r.target_key) for r in facts} == {
        (EdgeType.CALLS_HTTP_ENDPOINT, "GET https://api.example.com/hello/me"),
        (EdgeType.CALLS_HTTP_ROUTE, "GET /hello/me"),
    }
    for reference in facts:
        assert reference.source_symbol == "get_me"
        assert (reference.line_start, reference.line_end) == (5, 6)
        assert reference.meta == {"source": "requests", "provenance": "exact"}
    assert [r.meta[PENDING_FACT] for r in references if PENDING_FACT in r.meta] == [
        HTTP_CLIENT_CALL
    ]


def test_a_dynamic_base_url_proves_no_route_and_no_endpoint() -> None:
    """§21: the known suffix does not decide the path; ``base`` does."""
    source = """\
    import requests

    def get_me(base):
        return requests.get(f"{base}/hello/me").json()
    """
    assert _http(source) == []


@pytest.mark.parametrize(
    "call",
    [
        "requests.get(url)",
        "requests.get('/users/' + user_id)",
        "requests.get('https://x.example/{}'.format(p))",
        "requests.get('https://x.example/%s' % p)",
        "requests.get(get_url())",
        "requests.get(os.environ['API'] + '/x')",
        "requests.get(config.base_url + '/x')",
        "requests.get('/relative/path')",
        "requests.get('https://user:pw@x.example/a')",
        "requests.request(method, 'https://x.example/a')",
        "requests.request('FETCH', 'https://x.example/a')",
        "requests.get(*args)",
        "requests.get('https://x.example/a', url='https://y.example/b')",
        "requests.Session().get('https://x.example/a')",
        "session.get('https://x.example/a')",
        "cache.get('/hello')",
        "mapping.get('/hello')",
    ],
)
def test_calls_that_prove_no_request_yield_nothing(call: str) -> None:
    assert _http(f"import os\nimport requests\ndef f():\n    return {call}\n") == []


@pytest.mark.parametrize(
    ("header", "call", "endpoint"),
    [
        ("import requests as r", "r.post('https://x.example/a')", "POST https://x.example/a"),
        ("from requests import put", "put('https://x.example/a')", "PUT https://x.example/a"),
        (
            "import requests",
            "requests.request('delete', 'https://x.example/a')",
            "DELETE https://x.example/a",
        ),
        (
            "import requests",
            "requests.request(method='PATCH', url='https://x.example/a')",
            "PATCH https://x.example/a",
        ),
        ("import httpx", "httpx.get('https://x.example/a')", "GET https://x.example/a"),
        ("import httpx", "httpx.head(url='http://localhost:8000/')", "HEAD http://localhost:8000/"),
        (
            "import httpx",
            "httpx.request('OPTIONS', 'https://x.example' + '/a')",
            "OPTIONS https://x.example/a",
        ),
    ],
)
def test_client_identity_may_be_spelled_several_ways(header: str, call: str, endpoint: str) -> None:
    source = f"{header}\ndef f():\n    return {call}\n"
    assert _targets(source, EdgeType.CALLS_HTTP_ENDPOINT) == {endpoint}


def test_an_httpx_client_object_is_not_followed() -> None:
    """Only module-level functions: ``base_url`` would need object flow to read."""
    source = """\
    import httpx
    client = httpx.Client(base_url="https://api.example.com")
    def f():
        return client.get("/hello")
    """
    assert _http(source) == []


@pytest.mark.parametrize(
    "rebinding",
    ["requests = None\n", "def g(requests): ...\n", "import requests\n"],
)
def test_a_shadowed_client_module_proves_nothing(rebinding: str) -> None:
    source = (
        f"import requests\n{rebinding}def f():\n    return requests.get('https://x.example/a')\n"
    )
    assert _http(source) == []


def test_http_facts_are_additive_to_the_existing_call_graph() -> None:
    parsed = _parse("import requests\ndef f():\n    return requests.get('https://x.example/a')\n")
    assert any(
        r.edge_type is EdgeType.CALLS and r.target_key == "requests.get" for r in parsed.references
    )


def test_a_file_that_does_not_parse_yields_no_http_facts() -> None:
    assert _http("import requests\nrequests.get('https://x.example/a'\n") == []
