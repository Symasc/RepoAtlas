"""Requests made on a client object (Phase A2b): ``requests.Session``, ``httpx.Client``.

A client object is followed only where its identity is lexical: the one binding of its name
in the module, constructed with nothing that could change where it sends (no positional or
starred argument, no ``transport``, ``mounts``, ``app`` or ``event_hooks``), and read from
the scope the binding lives in. Then:

* a literal absolute URL is read as a module-level call is, whatever the base;
* a rooted URL is appended to a ``base_url`` the source states, for the join only;
* a rooted URL on a ``base_url`` the source does not state is a claim with no origin and the
  synthetic host ``{client.base_url}`` -- counted, never joined (D2);
* a rooted URL on a client with *no* base names no destination, so it yields nothing.

Half of this file is what is refused: each refusal is a place where a plausible reader would
invent a destination for a request.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.graph.http_crossings import (
    LINKED,
    SERVICE_UNMATCHED,
    UNKNOWN_BASE,
    plan_http_crossings,
)
from repoatlas.graph.linker import ReferenceLinker
from repoatlas.models.parsing import EdgeType, EntityType, ParsedReference
from repoatlas.parsers.http_facts import (
    HTTP_CLIENT_CALL,
    HTTP_EXPOSED_ENDPOINT,
    PENDING_FACT,
)
from repoatlas.parsers.python import PythonParser
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace
from repoatlas.storage.protocols import FileRow, UnresolvedRefRecord
from tests.http_crossing_corpus import ACCOUNTS, decision_of
from tests.impact_corpus import build
from tests.incremental_corpus import assert_equals_fresh, edit, sync_all

_HTTP_KINDS = {EntityType.HTTP_ROUTE, EntityType.HTTP_DEPLOYMENT, EntityType.HTTP_ENDPOINT}


def _references(source: str) -> list[ParsedReference]:
    return list(PythonParser().parse(Path("client.py"), textwrap.dedent(source)).references)


def _http(source: str) -> list[ParsedReference]:
    return [r for r in _references(source) if r.target_kind in _HTTP_KINDS]


def _claims(source: str) -> list[ParsedReference]:
    return [r for r in _references(source) if r.meta.get(PENDING_FACT) == HTTP_CLIENT_CALL]


def _facts(source: str) -> list[ParsedReference]:
    return [
        r
        for r in _references(source)
        if r.edge_type in {EdgeType.CALLS_HTTP_ENDPOINT, EdgeType.CALLS_HTTP_ROUTE}
        and PENDING_FACT not in r.meta
    ]


# -- what is read ---------------------------------------------------------------------------


def test_a_session_on_a_constant_f_string_is_a_claim_with_its_client() -> None:
    source = """\
    import requests

    BASE = "https://a.example/api"
    session = requests.Session()


    def load(uid):
        return session.get(f"{BASE}/users/{uid}")
    """
    assert _facts(source) == []
    [claim] = _claims(source)
    assert claim.edge_type is EdgeType.CALLS_HTTP_ENDPOINT
    assert claim.target_key == "GET https://a.example/api/users/{uid}"
    assert claim.source_symbol == "load"
    assert (claim.line_start, claim.line_end) == (8, 8)
    assert claim.meta == {
        PENDING_FACT: HTTP_CLIENT_CALL,
        "method": "GET",
        "segments": '["api", "users", null]',
        "source": "requests",
        "origin": "https://a.example",
        "base_at": "3",
        "client": "session",
        "client_at": "4",
    }


def test_a_session_on_a_literal_absolute_url_keeps_the_facts_of_a_module_level_call() -> None:
    source = """\
    import requests

    session = requests.Session()


    def load():
        return session.get("https://a.example/users/123?x=1")
    """
    assert {(r.edge_type, r.target_key) for r in _facts(source)} == {
        (EdgeType.CALLS_HTTP_ENDPOINT, "GET https://a.example/users/123"),
        (EdgeType.CALLS_HTTP_ROUTE, "GET /users/123"),
    }
    [claim] = _claims(source)
    assert claim.meta == {
        PENDING_FACT: HTTP_CLIENT_CALL,
        "method": "GET",
        "segments": '["users", "123"]',
        "source": "requests",
        "origin": "https://a.example",
        "client": "session",
        "client_at": "3",
    }


def test_the_lower_case_session_function_is_a_session_too() -> None:
    source = """\
    import requests

    session = requests.session()


    def ping():
        return session.get("https://a.example/ping")
    """
    [claim] = _claims(source)
    assert claim.target_key == "GET https://a.example/ping"
    assert (claim.meta["client"], claim.meta["client_at"]) == ("session", "3")


def test_an_httpx_base_url_literal_is_appended_to_for_the_join_only() -> None:
    source = """\
    import httpx

    client = httpx.Client(base_url="https://api.example.com")


    def users():
        return client.get("/users")
    """
    assert _facts(source) == []
    [claim] = _claims(source)
    assert claim.target_key == "GET https://api.example.com/users"
    assert claim.source_symbol == "users"
    assert (claim.line_start, claim.line_end) == (7, 7)
    assert claim.meta == {
        PENDING_FACT: HTTP_CLIENT_CALL,
        "method": "GET",
        "segments": '["users"]',
        "source": "httpx",
        "origin": "https://api.example.com",
        "base_at": "3",
        "client": "client",
        "client_at": "3",
    }


def test_a_base_with_a_path_and_a_trailing_slash_is_appended_to_exactly() -> None:
    source = """\
    import httpx

    client = httpx.Client(base_url="https://api.example.com/v1/")


    def user(uid):
        return client.get(f"/users/{uid}")
    """
    [claim] = _claims(source)
    assert claim.target_key == "GET https://api.example.com/v1/users/{uid}"
    assert claim.meta["origin"] == "https://api.example.com"
    assert json.loads(claim.meta["segments"]) == ["v1", "users", None]


def test_a_rooted_url_built_with_plus_is_read_like_an_f_string() -> None:
    source = """\
    import httpx

    client = httpx.Client(base_url="https://api.example.com")


    def user(uid):
        return client.get("/users/" + uid)
    """
    [claim] = _claims(source)
    assert claim.target_key == "GET https://api.example.com/users/{uid}"
    assert json.loads(claim.meta["segments"]) == ["users", None]


def test_a_base_from_a_module_constant_has_the_constants_line() -> None:
    source = """\
    import httpx

    API = "https://api.example.com/v2"

    client = httpx.Client(base_url=API)


    def create():
        return client.post("/users")
    """
    [claim] = _claims(source)
    assert claim.target_key == "POST https://api.example.com/v2/users"
    assert (claim.meta["base_at"], claim.meta["client_at"]) == ("3", "5")
    assert claim.meta["origin"] == "https://api.example.com"
    assert _facts(source) == []


def test_an_environment_default_base_is_stated_like_a_constant() -> None:
    source = """\
    import os

    import httpx

    API = os.environ.get("API_URL", "https://api.example.com")
    client = httpx.Client(base_url=API)


    def users():
        return client.get("/users")
    """
    [claim] = _claims(source)
    assert claim.target_key == "GET https://api.example.com/users"
    assert (claim.meta["base_at"], claim.meta["client_at"]) == ("5", "6")
    assert _facts(source) == []


def test_a_with_block_on_an_environment_base_is_a_claim_with_no_origin() -> None:
    source = """\
    import os

    import httpx


    def user(uid):
        with httpx.Client(base_url=os.environ["API_URL"]) as client:
            return client.get(f"/users/{uid}")
    """
    assert _facts(source) == []
    [claim] = _claims(source)
    assert claim.target_key == "GET {client.base_url}/users/{uid}"
    assert claim.source_symbol == "user"
    assert (claim.line_start, claim.line_end) == (8, 8)
    assert claim.meta == {
        PENDING_FACT: HTTP_CLIENT_CALL,
        "method": "GET",
        "segments": '["users", null]',
        "source": "httpx",
        "base": "unknown",
        "base_at": "7",
        "client": "client",
        "client_at": "7",
        "base_expr": "os.environ['API_URL']",
    }


def test_an_async_client_on_a_settings_base_is_a_post_with_no_origin() -> None:
    source = """\
    import httpx

    from config import settings

    client = httpx.AsyncClient(base_url=settings.API_URL)


    async def create(name):
        return await client.post("/users", json={"name": name})
    """
    assert _facts(source) == []
    [claim] = _claims(source)
    assert claim.target_key == "POST {client.base_url}/users"
    assert claim.meta == {
        PENDING_FACT: HTTP_CLIENT_CALL,
        "method": "POST",
        "segments": '["users"]',
        "source": "httpx",
        "base": "unknown",
        "base_at": "5",
        "client": "client",
        "client_at": "5",
        "base_expr": "settings.API_URL",
    }


def test_an_environment_base_with_an_empty_default_may_still_be_set() -> None:
    source = """\
    import os

    import httpx

    API = os.environ.get("API_URL", "")
    client = httpx.Client(base_url=API)


    def users():
        return client.get("/users")
    """
    [claim] = _claims(source)
    assert claim.target_key == "GET {client.base_url}/users"
    assert (claim.meta["base"], claim.meta["base_at"], claim.meta["base_expr"]) == (
        "unknown",
        "5",
        "API",
    )
    assert "origin" not in claim.meta


def test_an_absolute_url_ignores_the_base() -> None:
    source = """\
    import httpx

    client = httpx.Client(base_url="https://api.example.com/v1")


    def other():
        return client.get("https://other.example/ping")
    """
    assert {(r.edge_type, r.target_key) for r in _facts(source)} == {
        (EdgeType.CALLS_HTTP_ENDPOINT, "GET https://other.example/ping"),
        (EdgeType.CALLS_HTTP_ROUTE, "GET /ping"),
    }
    [claim] = _claims(source)
    assert claim.meta == {
        PENDING_FACT: HTTP_CLIENT_CALL,
        "method": "GET",
        "segments": '["ping"]',
        "source": "httpx",
        "origin": "https://other.example",
        "client": "client",
        "client_at": "3",
    }


def test_a_client_request_with_a_literal_method_is_read() -> None:
    source = """\
    import httpx

    client = httpx.Client(base_url="https://api.example.com")


    def remove():
        return client.request("DELETE", "/users/1")
    """
    [claim] = _claims(source)
    assert claim.target_key == "DELETE https://api.example.com/users/1"
    assert claim.meta["method"] == "DELETE"
    assert json.loads(claim.meta["segments"]) == ["users", "1"]


def test_an_aliased_async_client_in_an_async_with_is_read() -> None:
    source = """\
    from httpx import AsyncClient as AC


    async def users():
        async with AC(base_url="https://api.example.com") as api:
            return await api.get("/users")
    """
    [claim] = _claims(source)
    assert claim.target_key == "GET https://api.example.com/users"
    assert (claim.meta["client"], claim.meta["client_at"], claim.meta["base_at"]) == (
        "api",
        "5",
        "5",
    )


def test_an_annotated_assignment_binds_a_client_too() -> None:
    source = """\
    import httpx

    client: httpx.Client = httpx.Client(base_url="https://api.example.com")


    def users():
        return client.get("/users")
    """
    [claim] = _claims(source)
    assert claim.target_key == "GET https://api.example.com/users"
    assert claim.meta["client_at"] == "3"


def test_a_client_bound_in_a_function_reaches_its_nested_functions() -> None:
    source = """\
    import httpx


    def make():
        client = httpx.Client(base_url="https://api.example.com")

        def users():
            return client.get("/users")

        return users
    """
    [claim] = _claims(source)
    assert claim.target_key == "GET https://api.example.com/users"
    assert claim.meta["client_at"] == "5"


def test_a_with_block_client_is_read_inside_the_block_and_not_after_it() -> None:
    source = """\
    import httpx


    def users():
        with httpx.Client(base_url="https://api.example.com") as client:
            inside = client.get("/inside")
        outside = client.get("/outside")
        return inside, outside
    """
    assert [c.target_key for c in _claims(source)] == ["GET https://api.example.com/inside"]


@pytest.mark.parametrize("base", ['""', "EMPTY"])
def test_an_empty_base_is_no_base(base: str) -> None:
    source = (
        "import httpx\n"
        'EMPTY = ""\n'
        f"client = httpx.Client(base_url={base})\n"
        "def users():\n"
        '    return client.get("/users")\n'
    )
    assert _http(source) == []


def test_an_unread_host_is_a_claim_only_on_a_client_with_no_base() -> None:
    """A client with a base could mean ``f"{host}/users"`` as a path; one without could not."""
    for keyword, expected in (
        ("", ["GET {host}/users"]),
        ("base_url='https://api.example.com'", []),
    ):
        source = (
            f"import httpx\nclient = httpx.Client({keyword})\n"
            "def use(host):\n"
            "    return client.get(f'{host}/users')\n"
        )
        assert [c.target_key for c in _claims(source)] == expected


def test_at_most_32_clients_are_read_in_one_module() -> None:
    def many(count: int) -> str:
        names = [f"c{index}" for index in range(count)]
        lines = ["import httpx", ""]
        lines += [f'{name} = httpx.Client(base_url="https://a.example")' for name in names]
        lines += ["", "", "def use():"]
        lines += [f'    {name}.get("/users")' for name in names]
        return "\n".join(lines) + "\n"

    assert len(_claims(many(32))) == 32
    assert _claims(many(33)) == []


# -- what is refused ------------------------------------------------------------------------

_IMPORT = "import httpx\n"
_CLIENT = 'client = httpx.Client(base_url="https://api.example.com")\n'
_USE = "\n\ndef use():\n    return client.get('/users')\n"


def _built(arguments: str, constructor: str = "Client") -> str:
    """A module whose one client is built with ``arguments`` and used for a rooted request."""
    return f"import httpx\nclient = httpx.{constructor}({arguments})\n{_USE}"


_REQUESTS_SESSION = "import requests\ns = requests.Session()\n"
_SESSION_USE = "def use():\n    return s.get('https://a.example/x')\n"


@pytest.mark.parametrize(
    "source",
    [
        # The name is bound more than once, anywhere in the module.
        _IMPORT + _CLIENT + _CLIENT + _USE,
        _IMPORT + _CLIENT + _USE + "\n\ndef other(client):\n    return client\n",
        _IMPORT + _CLIENT + _USE + "\n\nfor client in []:\n    pass\n",
        _IMPORT + _CLIENT + _USE + "\n\ndef reset():\n    global client\n    client = None\n",
        # Something stores on it, or reaches its hooks, so what it sends may have moved.
        _IMPORT + _CLIENT + "client.base_url = 'https://b.example'\n" + _USE,
        _IMPORT + _CLIENT + "client.base_url += '/v2'\n" + _USE,
        _IMPORT + _CLIENT + "client.event_hooks['request'].append(log)\n" + _USE,
        _IMPORT + _CLIENT + "print(client.event_hooks)\n" + _USE,
        _REQUESTS_SESSION + "s.hooks['response'].append(log)\n" + _SESSION_USE,
        _REQUESTS_SESSION + "s.proxies = {}\n" + _SESSION_USE,
        # It is built to send elsewhere, or to run code around the request.
        _built("base_url='https://a.example', event_hooks={}"),
        _built("base_url='https://a.example', transport=t"),
        _built("base_url='https://a.example', mounts={}"),
        _built("base_url='https://a.example', app=app"),
        _built("base_url='https://a.example', app=app", "AsyncClient"),
        # Or with arguments this cannot read.
        _built("**cfg"),
        _built("*args"),
        _built("'https://a.example'"),
        "import requests\nclient = requests.Session(adapter)\n" + _USE,
        "import requests\nclient = requests.Session(timeout=3)\n" + _USE,
        # A base a path cannot be appended to, as written.
        _built("base_url='/api'"),
        _built("base_url='api.example.com'"),
        _built("base_url='https://u:p@api.example.com'"),
        _built("base_url='https://api.example.com?k=1'"),
        _built("base_url='https://api.example.com/a#frag'"),
        _built("base_url='https://api.example.com/a//b'"),
        _built("base_url='https://api.example.com/{v}'"),
        _built("base_url='https://api.example.com/a/../b'"),
        _IMPORT + "BASE = '/api'\nclient = httpx.Client(base_url=BASE)\n" + _USE,
        # Not the library's client, and not a name.
        "import httpx\nclass MyClient:\n    def __init__(self, base_url): ...\n"
        "client = MyClient(base_url='https://a.example')\n" + _USE,
        "import httpx\ncache = {}\n\n\ndef use():\n    return cache.get('/users')\n",
        _IMPORT + "client = httpx.Client(base_url='https://a.example'), 1\n" + _USE,
        _IMPORT + "client, other = httpx.Client(base_url='https://a.example'), 1\n" + _USE,
        _IMPORT + "if (client := httpx.Client(base_url='https://a.example')):\n    pass\n" + _USE,
        _IMPORT + "client = other = httpx.Client(base_url='https://a.example')\n" + _USE,
        # Bound on a class or an instance: not a name a method can read.
        "import httpx\nclass Api:\n    client = httpx.Client(base_url='https://a.example')\n"
        "    def use(self):\n        return client.get('/users')\n",
        "import httpx\nclass Api:\n    def __init__(self):\n"
        "        self.client = httpx.Client(base_url='https://a.example')\n"
        "    def use(self):\n        return self.client.get('/users')\n",
        # Built inline: no name at all.
        "import httpx\ndef use():\n"
        "    return httpx.Client(base_url='https://a.example').get('/users')\n",
        "import requests\ndef use():\n    return requests.Session().get('/users')\n",
    ],
)
def test_a_client_that_is_not_provably_one_object_yields_nothing(source: str) -> None:
    assert _http(source) == []


@pytest.mark.parametrize(
    "escape",
    [
        "configure(session)",
        "configure(thing=session)",
        "registry = [session]",
        "alias = session",
        "print(session)",
    ],
)
@pytest.mark.parametrize(
    ("construction", "call", "read_when_clean"),
    [
        # A relative request on a session with no base is no request, escape or not.
        ("import requests\nsession = requests.Session()\n", "session.get('/users')", False),
        # Requests that are read when nothing can reach the object.
        (
            "import httpx\nsession = httpx.Client(base_url='https://a.example')\n",
            "session.get('/users')",
            True,
        ),
        (
            "import requests\nsession = requests.Session()\n",
            "session.get('https://a.example/users')",
            True,
        ),
    ],
)
def test_a_client_that_can_escape_is_refused(
    escape: str, construction: str, call: str, read_when_clean: bool
) -> None:
    """Passed on, stored or aliased, it may get a base through another name: nothing is read."""
    source = f"{construction}{escape}\n\n\ndef use():\n    return {call}\n"
    assert _claims(source) == []
    assert _facts(source) == []
    assert _http(source) == []
    # The control: the same module without the escape.
    clean = f"{construction}\n\ndef use():\n    return {call}\n"
    assert bool(_http(clean)) is read_when_clean


def test_a_client_used_only_through_get_request_and_with_is_still_read() -> None:
    source = """\
    import httpx

    BASE = "https://api.example.com"


    def users():
        with httpx.Client(base_url=BASE) as client:
            first = client.get("/users")
            second = client.request("DELETE", "/users/1")
        return first, second
    """
    assert {c.target_key for c in _claims(source)} == {
        "GET https://api.example.com/users",
        "DELETE https://api.example.com/users/1",
    }


def test_a_client_bound_in_one_function_is_not_the_name_another_function_reads() -> None:
    source = """\
    import httpx


    def make():
        client = httpx.Client(base_url="https://api.example.com")
        return client


    def use():
        return client.get("/users")
    """
    assert _http(source) == []


@pytest.mark.parametrize(
    "call",
    [
        "client.get('users')",
        "client.get(f'{p}')",
        "client.get(url)",
        "client.get(urls[0])",
        "client.get('//other.example/users')",
        "client.get('/../users')",
        "client.get('ftp://other.example/users')",
        "client.get(*args)",
        "client.request(method, '/users')",
        "client.request('FETCH', '/users')",
        "client.stream('GET', '/users')",
        "client.send(request)",
    ],
)
@pytest.mark.parametrize(
    "keyword", ["", "base_url='https://api.example.com'", "base_url=os.environ['API']"]
)
def test_a_url_that_is_not_rooted_or_absolute_yields_nothing(keyword: str, call: str) -> None:
    source = (
        "import os\nimport httpx\n"
        f"client = httpx.Client({keyword})\n"
        "def use(p, url, urls, args, method, request):\n"
        f"    return {call}\n"
    )
    assert _http(source) == []


@pytest.mark.parametrize(
    "setup",
    [
        "import requests\nsession = requests.Session()\n",
        "import requests\nsession = requests.session()\n",
        "import httpx\nsession = httpx.Client()\n",
        "import httpx\nsession = httpx.AsyncClient(headers={'x': 'y'})\n",
        "import httpx\nsession = httpx.Client(base_url='')\n",
    ],
)
@pytest.mark.parametrize(
    "call",
    ["session.get('/users')", "session.post(f'/users/{uid}')", "session.request('GET', '/users')"],
)
def test_a_relative_request_on_an_unconfigured_client_invents_no_destination(
    setup: str, call: str
) -> None:
    source = f"{setup}\n\ndef use(uid):\n    return {call}\n"
    assert _http(source) == []
    assert _claims(source) == []


def test_a_star_import_before_every_binding_rebinds_nothing_the_module_proves() -> None:
    source = "from helpers import *\n" + _IMPORT + _CLIENT + _USE
    assert _facts(source) == []
    [claim] = _claims(source)
    assert claim.meta["origin"] == "https://api.example.com"
    assert claim.meta["segments"] == '["users"]'
    assert (claim.meta["client"], claim.meta["client_at"]) == ("client", "3")


def test_a_star_import_after_the_client_makes_it_unknowable() -> None:
    assert _http(_IMPORT + _CLIENT + "from helpers import *\n" + _USE) == []


def test_a_rebound_library_name_is_no_library() -> None:
    assert _http(_IMPORT + "httpx = None\n" + _CLIENT + _USE) == []


# -- the join -------------------------------------------------------------------------------


def _row(file_id: int, repo_id: int, slug: str, path: str) -> FileRow:
    return FileRow(
        file_id=file_id,
        file_entity_id=100 + file_id,
        repo_id=repo_id,
        repo_name=slug,
        repo_slug=slug,
        repo_path=Path("/r") / slug,
        rel_path=path,
    )


def _record(claim_id: int, reference: ParsedReference) -> UnresolvedRefRecord:
    """The stored claim a parsed reference becomes, in the consumer's file (``web``)."""
    return UnresolvedRefRecord(
        id=claim_id,
        repo_id=2,
        file_id=2,
        src_id=22,
        edge_type=reference.edge_type,
        target_kind=reference.target_kind,
        target_key=reference.target_key,
        line_start=reference.line_start,
        line_end=reference.line_end,
        meta_json=json.dumps(reference.meta),
    )


def _service() -> UnresolvedRefRecord:
    """``svc`` publishes ``GET /users/{id}`` at ``https://svc.example``."""
    return UnresolvedRefRecord(
        id=1,
        repo_id=1,
        file_id=1,
        src_id=11,
        edge_type=EdgeType.CALLS_HTTP_ENDPOINT,
        target_kind=EntityType.HTTP_ENDPOINT,
        target_key="GET https://svc.example/users/{id}",
        line_start=5,
        line_end=5,
        meta_json=json.dumps(
            {
                PENDING_FACT: HTTP_EXPOSED_ENDPOINT,
                "method": "GET",
                "route": "/users/{id}",
                "deployment": "https://svc.example",
                "identity_source": "flasgger-swagger2",
                "identity_at": "3",
            }
        ),
    )


FILES = [_row(1, 1, "svc", "app.py"), _row(2, 2, "web", "client.py")]


def test_a_dynamic_base_client_claim_is_counted_and_never_joined() -> None:
    source = """\
    import os

    import httpx


    def user(uid):
        with httpx.Client(base_url=os.environ["API_URL"]) as client:
            return client.get(f"/users/{uid}")
    """
    [claim] = _claims(source)
    plan = plan_http_crossings([_service(), _record(2, claim)], FILES, producer="linker:global")
    assert plan.edges == ()
    [call] = plan.calls
    assert (call.claim.id, call.decision) == (2, UNKNOWN_BASE)
    # The expression is the parser's note, not the join's evidence.
    assert "API_URL" not in json.dumps(call.evidence)
    assert "API_URL" not in repr(call.chain)


def test_a_stated_base_client_claim_joins_like_any_request_on_that_origin() -> None:
    source = """\
    import httpx

    client = httpx.Client(base_url="https://svc.example")


    def user(uid):
        return client.get(f"/users/{uid}")
    """
    [claim] = _claims(source)
    plan = plan_http_crossings([_service(), _record(2, claim)], FILES, producer="linker:global")
    assert [(call.claim.id, call.decision) for call in plan.calls] == [(2, LINKED)]
    [edge] = plan.edges
    assert (edge.src_id, edge.dst_id, edge.type) == (22, 11, EdgeType.CALLS_HTTP_HANDLER)
    record = json.loads(edge.resolution or "{}")["http"]
    assert record["url"] == "https://svc.example/users/{uid}"
    assert record["base_at"] == "client.py:3"


# -- incremental == fresh -------------------------------------------------------------------

CLIENTS = """\
import os

import httpx
import requests

ACCOUNTS = "https://accounts.example/api"
session = requests.Session()
client = httpx.Client(base_url=ACCOUNTS)
env_client = httpx.Client(base_url=os.environ["ACCOUNTS"])


def by_session(user_id):
    return session.get(f"{ACCOUNTS}/users/{user_id}")


def by_client(user_id):
    return client.get(f"/users/{user_id}")


def by_env(user_id):
    return env_client.get(f"/users/{user_id}")


def unconfigured(user_id):
    return session.get(f"/users/{user_id}")
"""

#: Joined where the base is stated, counted where it is not; a relative request on a client
#: with no base (``unconfigured``) is not a request at all, so it has no decision.
DECIDED = {"by_session": LINKED, "by_client": LINKED, "by_env": UNKNOWN_BASE}


def _decisions(workspace: Workspace) -> dict[str, str]:
    plan = ReferenceLinker(workspace.knowledge).prepare_global().http_crossings
    rows = workspace.database.execute(
        """
        SELECT u.id, s.qualified_name FROM unresolved_refs u
        JOIN entities s ON s.id = u.src_id JOIN files f ON f.id = u.file_id
        WHERE f.rel_path = 'client.py' AND u.meta_json LIKE ?
        """,
        (f'%"{HTTP_CLIENT_CALL}"%',),
    ).fetchall()
    return {row["qualified_name"]: decision_of(plan, row["id"]) for row in rows}


def test_a_resync_with_no_change_is_what_a_fresh_index_writes(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    build(workspace, tmp_path, {"accounts": ACCOUNTS, "portal": {"client.py": CLIENTS}})
    root = tmp_path / "repos"
    assert _decisions(workspace) == DECIDED
    sync(workspace, workspace.resolver().resolve(root))
    assert _decisions(workspace) == DECIDED
    assert_equals_fresh(workspace, settings, root, tmp_path, "fresh")


def test_an_edit_to_a_clients_base_is_what_a_fresh_index_writes(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    build(workspace, tmp_path, {"accounts": ACCOUNTS, "portal": {"client.py": CLIENTS}})
    root = tmp_path / "repos"
    old = "client = httpx.Client(base_url=ACCOUNTS)"
    assert old in CLIENTS
    new = 'client = httpx.Client(base_url="https://billing.example")'
    edit(root, "portal", "client.py", CLIENTS.replace(old, new))
    sync_all(workspace, root)
    assert _decisions(workspace) == {**DECIDED, "by_client": SERVICE_UNMATCHED}
    assert_equals_fresh(workspace, settings, root, tmp_path, "fresh")


# -- privacy --------------------------------------------------------------------------------


def _shown(source: str, *, without: tuple[str, ...] = ()) -> str:
    """Everything the HTTP facts and claims of a module record, as text."""
    shown = []
    for reference in _http(source):
        meta = {k: v for k, v in reference.meta.items() if k not in without}
        shown.append((reference.target_key, reference.source_symbol, sorted(meta.items())))
    return repr(shown)


def test_a_credential_in_a_stated_base_is_never_recorded() -> None:
    source = """\
    import httpx

    DSN = "https://user:hunter2@internal.example/api"
    client = httpx.Client(base_url=DSN)
    other = httpx.Client(base_url="https://user:hunter2@internal.example/api")


    def users():
        return client.get("/users"), other.get("/users")
    """
    assert _http(source) == []
    assert "hunter2" not in _shown(source)


@pytest.mark.parametrize(
    "expression",
    [
        'lookup("https://user:hunter2@internal.example")',
        'lookup(token="hunter2:secret")',
        'settings["hunter2@internal"]',
        f'lookup("{"k" * 65}")',
        'f"https://user:hunter2@{host}"',
        "lookup(*hunter2)",
    ],
)
def test_a_literal_in_a_base_expression_that_is_not_a_plain_name_is_never_recorded(
    expression: str,
) -> None:
    source = (
        "import httpx\n"
        "\n\n"
        "def users(host):\n"
        f"    with httpx.Client(base_url={expression}) as client:\n"
        "        return client.get('/users')\n"
    )
    [claim] = _claims(source)
    assert claim.target_key == "GET {client.base_url}/users"
    assert "base_expr" not in claim.meta
    for secret in ("hunter2", "k" * 65):
        assert secret not in _shown(source)


def test_a_plain_name_in_a_base_expression_is_recorded_only_as_the_bounded_base_expr() -> None:
    source = (
        "import os\n"
        "import httpx\n"
        "\n\n"
        "def users():\n"
        '    with httpx.Client(base_url=os.environ["INTERNAL_URL_hunter2"]) as client:\n'
        "        return client.get('/users')\n"
    )
    [claim] = _claims(source)
    assert claim.meta["base_expr"] == "os.environ['INTERNAL_URL_hunter2']"
    assert "hunter2" in _shown(source)
    assert "hunter2" not in _shown(source, without=("base_expr",))
