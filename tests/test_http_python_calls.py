"""Requests Python source states well enough to join to a published handler (Python P4).

A request becomes an :data:`HTTP_CLIENT_CALL` claim when its method is known and its
URL's origin and path shape are: a literal, a module constant holding a URL, or an
f-string on a literal or constant origin whose placeholders each fill one whole path
segment. The per-file HTTP facts stay literal-only, so a constant or a template adds
a claim and nothing else. Anything that could send the request somewhere else — a
parameter, an environment variable, an imported or rebound name, a placeholder glued
to text — is refused.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from repoatlas.models.parsing import EdgeType, ParsedReference
from repoatlas.parsers.http_facts import HTTP_CLIENT_CALL, PENDING_FACT
from repoatlas.parsers.python import PythonParser


def _references(source: str) -> list[ParsedReference]:
    return list(PythonParser().parse(Path("client.py"), textwrap.dedent(source)).references)


def _claims(source: str) -> list[ParsedReference]:
    return [r for r in _references(source) if r.meta.get(PENDING_FACT) == HTTP_CLIENT_CALL]


def _facts(source: str) -> list[ParsedReference]:
    return [
        r
        for r in _references(source)
        if r.edge_type in {EdgeType.CALLS_HTTP_ENDPOINT, EdgeType.CALLS_HTTP_ROUTE}
        and PENDING_FACT not in r.meta
    ]


def _shape(claim: ParsedReference) -> tuple[str, str, list[str | None]]:
    return claim.meta["method"], claim.meta["origin"], json.loads(claim.meta["segments"])


def test_a_literal_request_keeps_its_facts_and_adds_one_claim() -> None:
    source = """\
    import requests

    def load():
        return requests.get("https://a.example/users/123?expand=roles")
    """
    assert {(r.edge_type, r.target_key) for r in _facts(source)} == {
        (EdgeType.CALLS_HTTP_ENDPOINT, "GET https://a.example/users/123"),
        (EdgeType.CALLS_HTTP_ROUTE, "GET /users/123"),
    }
    [claim] = _claims(source)
    assert claim.edge_type is EdgeType.CALLS_HTTP_ENDPOINT
    assert claim.target_key == "GET https://a.example/users/123"
    assert claim.source_symbol == "load"
    assert (claim.line_start, claim.line_end) == (4, 4)
    assert claim.meta == {
        PENDING_FACT: HTTP_CLIENT_CALL,
        "method": "GET",
        "origin": "https://a.example",
        "segments": '["users", "123"]',
        "source": "requests",
    }


def test_a_module_constant_url_is_a_claim_with_the_constant_line() -> None:
    source = """\
    import requests

    ITEM = "https://a.example/api/items/7"


    def item():
        return requests.get(ITEM)
    """
    assert _facts(source) == []
    [claim] = _claims(source)
    assert _shape(claim) == ("GET", "https://a.example", ["api", "items", "7"])
    assert claim.meta["base_at"] == "3"
    assert (claim.line_start, claim.line_end) == (7, 7)
    assert claim.target_key == "GET https://a.example/api/items/7"


def test_an_f_string_on_a_literal_origin_fills_whole_segments() -> None:
    source = """\
    import requests

    def load(uid):
        return requests.get(f"https://a.example/users/{uid}/settings?full=1")
    """
    [claim] = _claims(source)
    assert _shape(claim) == ("GET", "https://a.example", ["users", None, "settings"])
    assert "base_at" not in claim.meta
    assert claim.target_key == "GET https://a.example/users/{uid}/settings"


def test_an_f_string_on_a_constant_base_is_appended_to_it() -> None:
    source = """\
    import httpx

    BASE = "https://A.example/api"


    def create(uid):
        return httpx.post(f"{BASE}/users/{uid}")
    """
    [claim] = _claims(source)
    assert _shape(claim) == ("POST", "https://a.example", ["api", "users", None])
    assert claim.meta["base_at"] == "3"
    assert claim.meta["source"] == "httpx"


def test_a_base_ending_in_a_slash_is_appended_to_exactly() -> None:
    source = """\
    import requests
    BASE = "https://a.example/api/"
    def load(uid):
        return requests.get(f"{BASE}users/{uid}")
    """
    [claim] = _claims(source)
    assert _shape(claim) == ("GET", "https://a.example", ["api", "users", None])


def test_a_literal_method_through_request_is_read() -> None:
    source = """\
    import requests
    BASE = "https://a.example"
    def remove(uid):
        return requests.request("delete", f"{BASE}/users/{uid}")
    """
    [claim] = _claims(source)
    assert _shape(claim) == ("DELETE", "https://a.example", ["users", None])


def test_a_module_level_request_has_no_enclosing_declaration() -> None:
    [claim] = _claims("import requests\nRESPONSE = requests.get('https://a.example/get')\n")
    assert claim.source_symbol is None


@pytest.mark.parametrize(
    ("prelude", "url"),
    [
        # The base is not a module constant this file binds once.
        ("", "f'{base}/users/{uid}'"),
        ("from config import BASE\n", "f'{BASE}/users/{uid}'"),
        ("BASE = 'https://a.example'\nBASE = 'https://b.example'\n", "f'{BASE}/users/{uid}'"),
        ("BASE = 'https://a.example'\ndef other():\n    BASE = 'x'\n", "f'{BASE}/users/{uid}'"),
        ("BASE = os.environ['API']\n", "f'{BASE}/users/{uid}'"),
        ("", "f\"{os.environ['API']}/users/{uid}\""),
        ("BASE = '/api'\n", "f'{BASE}/users/{uid}'"),
        # The base is text a placeholder is glued to, or is glued to text.
        ("BASE = 'https://a.example'\n", "f'{BASE}users/{uid}'"),
        ("BASE = 'https://a.example'\n", "f'{BASE}{uid}'"),
        ("", "f'https://{host}/users/{uid}'"),
        ("", "f'https://a.example{path}'"),
        # A placeholder must be a whole path segment, and constrain something.
        ("", "f'https://a.example/users/u-{uid}'"),
        ("", "f'https://a.example/users/{uid}.json'"),
        ("", "f'https://a.example/{endpoint}'"),
        ("", "f'https://a.example/users/{uid!r}'"),
        ("", "f'https://a.example/users/{uid:>8}'"),
        # Not a URL this reads at all.
        ("URL = 'https://a.example/users'\n", "URL + '/' + uid"),
        ("", "'https://a.example/users/{}'.format(uid)"),
        ("", "urls[0]"),
        ("", "build_url(uid)"),
        ("", "url"),
    ],
)
def test_a_url_whose_origin_or_shape_is_not_stated_is_refused(prelude: str, url: str) -> None:
    source = (
        f"import os\nimport requests\n{prelude}"
        "def load(uid, base, host, path, url, urls, endpoint):\n"
        f"    return requests.get({url})\n"
    )
    assert _claims(source) == []


def test_an_unreadable_method_is_refused_whatever_the_url() -> None:
    source = """\
    import requests
    BASE = "https://a.example"
    def send(method, uid):
        return requests.request(method, f"{BASE}/users/{uid}")
    """
    assert _claims(source) == []


def test_a_client_object_is_still_not_followed() -> None:
    source = """\
    import requests
    BASE = "https://a.example"
    session = requests.Session()
    def load(uid):
        return session.get(f"{BASE}/users/{uid}")
    """
    assert _claims(source) == []
