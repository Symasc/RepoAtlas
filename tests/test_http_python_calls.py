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
        # A host that is a value.
        ("", "f'https://{host}/users/{uid}'"),
        # Stated empty: no origin.
        ("BASE = os.environ.get('API', '')\n", "f'{BASE}/users/{uid}'"),
        # A + chain on a base that is a value.
        ("", "base + '/users/' + uid"),
    ],
)
def test_a_request_on_a_base_the_source_does_not_state_is_counted_with_no_origin(
    prelude: str, url: str
) -> None:
    source = (
        f"import os\nimport requests\n{prelude}"
        "def load(uid, base, host, path, url, urls, endpoint):\n"
        f"    return requests.get({url})\n"
    )
    [claim] = _claims(source)
    assert claim.meta["base"] == "unknown" and "origin" not in claim.meta
    assert json.loads(claim.meta["segments"]) == ["users", None]


@pytest.mark.parametrize(
    ("prelude", "url"),
    [
        # The base is stated, and names no origin.
        ("BASE = '/api'\n", "f'{BASE}/users/{uid}'"),
        # The base is text a placeholder is glued to, or is glued to text.
        ("BASE = 'https://a.example'\n", "f'{BASE}users/{uid}'"),
        # Glued text would change the host, or the base path, while still reading as a URL.
        ("BASE = 'https://a.example'\n", "f'{BASE}users/{uid}/settings'"),
        ("BASE = 'https://a.example/api'\n", "f'{BASE}users/{uid}/settings'"),
        ("BASE = 'https://a.example'\n", "f'{BASE}{uid}'"),
        ("", "f'https://a.example{path}'"),
        # An unstated base whose path is not read either.
        ("", "f'{base}{path}'"),
        ("", "f'{base}/{uid}'"),
        ("", "f'{base!r}/users/{uid}'"),
        ("", "base + path"),
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


def test_a_session_object_reads_a_request_like_a_module_level_call() -> None:
    source = """\
    import requests
    BASE = "https://a.example"
    session = requests.Session()
    def load(uid):
        return session.get(f"{BASE}/users/{uid}")
    """
    assert _facts(source) == []
    [claim] = _claims(source)
    assert _shape(claim) == ("GET", "https://a.example", ["users", None])
    assert claim.meta["base_at"] == "2"
    assert (claim.meta["client"], claim.meta["client_at"]) == ("session", "3")
    assert claim.target_key == "GET https://a.example/users/{uid}"


@pytest.mark.parametrize(
    "source",
    [
        # A relative request on a client with no base names no destination.
        "import requests\nsession = requests.Session()\n"
        "def load(uid):\n    return session.get(f'/users/{uid}')\n",
        # The client is bound twice, so no one object is meant.
        "import requests\nsession = requests.Session()\nsession = requests.Session()\n"
        "def load(uid):\n    return session.get('https://a.example/users/1')\n",
    ],
)
def test_a_relative_request_on_an_unconfigured_or_ambiguous_session_invents_nothing(
    source: str,
) -> None:
    assert _claims(source) == []
    assert _facts(source) == []


# -- environment defaults (deployment HTTP roadmap, M3) ---------------------------------------


@pytest.mark.parametrize(
    ("prelude", "url", "base_at"),
    [
        ('URL = os.environ.get("TIME_URL", "http://svc-e6:5000/svc-e6/time")\n', "URL", "3"),
        ('URL = os.getenv("TIME_URL", "http://svc-e6:5000/svc-e6/time")\n', "URL", "3"),
        ("", 'os.environ.get("TIME_URL", "http://svc-e6:5000/svc-e6/time")', "4"),
        (
            'from os import getenv\nURL = getenv("TIME_URL", default="http://svc-e6:5000/svc-e6/time")\n',
            "URL",
            "4",
        ),
    ],
)
def test_an_environment_default_url_is_the_request_s_url(
    prelude: str, url: str, base_at: str
) -> None:
    source = f"import os\nimport requests\n{prelude}def load():\n    return requests.get({url})\n"
    [claim] = _claims(source)
    assert claim.target_key == "GET http://svc-e6:5000/svc-e6/time"
    assert (claim.meta["origin"], claim.meta["base_at"]) == ("http://svc-e6:5000", base_at)
    assert _facts(source) == []  # a default the deployment may override proves no fact


def test_an_environment_default_base_composes_like_a_constant() -> None:
    source = (
        "import os\nimport requests\n"
        'BASE = os.environ.get("ACCOUNTS_URL", "https://accounts.example/api")\n'
        "def load(uid):\n"
        '    return requests.get(f"{BASE}/users/{uid}")\n'
    )
    [claim] = _claims(source)
    assert claim.target_key == "GET https://accounts.example/api/users/{uid}"
    assert claim.meta["origin"] == "https://accounts.example"


@pytest.mark.parametrize(
    "prelude",
    [
        'URL = os.environ["TIME_URL"]\n',  # no default
        'URL = os.environ.get(KEY, "http://svc-e6:5000/x")\n',  # the key is not literal
        'URL = os.environ.get("TIME_URL", fallback)\n',  # the default is not literal
        'URL = os.environ.get("TIME_URL", "http://a/x")\nURL = "http://b/x"\n',  # bound twice
        'import other as os\nURL = os.getenv("TIME_URL", "http://a/x")\n',  # not the os module
    ],
)
def test_anything_but_a_literal_default_is_no_url(prelude: str) -> None:
    source = f"import os\nimport requests\n{prelude}def load():\n    return requests.get(URL)\n"
    assert _claims(source) == []


def test_an_environment_default_never_names_a_route() -> None:
    source = (
        "import os\nfrom fastapi import FastAPI\n"
        'PREFIX = os.environ.get("PREFIX", "/api")\n'
        "app = FastAPI()\n"
        '@app.get(f"{PREFIX}/items")\n'
        "def items():\n    return []\n"
    )
    served = [
        r.target_key
        for r in _references(source)
        if r.edge_type is EdgeType.SERVES_HTTP_ROUTE and PENDING_FACT not in r.meta
    ]
    assert served == []


ALTERNATIVES = """\
import os

import requests

if os.environ.get("ACCOUNT") == "prod":
    BASE = "https://api-prd.example"
elif os.environ.get("ACCOUNT") == "stg":
    BASE = "https://api-stg.example"
else:
    BASE = "http://api.sandbox.example"


def keys(vpn):
    return requests.post(f"{BASE}/vpn/{vpn}/keys", timeout=5)
"""


def test_each_literal_alternative_of_a_base_is_its_own_claim() -> None:
    claims = _claims(ALTERNATIVES)
    assert [
        (c.meta["origin"], c.meta["base_at"], c.meta["alternatives"], c.line_start) for c in claims
    ] == [
        ("https://api-prd.example", "6", "3", 14),
        ("https://api-stg.example", "8", "3", 14),
        ("http://api.sandbox.example", "10", "3", 14),
    ]
    assert {json.dumps(json.loads(c.meta["segments"])) for c in claims} == {
        json.dumps(["vpn", None, "keys"])
    }
    # A claim for the join only: no per-file fact names any of the hosts.
    assert _facts(ALTERNATIVES) == []


@pytest.mark.parametrize(
    "change",
    [
        # no else: the name may be left unbound
        lambda s: s.replace('else:\n    BASE = "http://api.sandbox.example"\n', ""),
        # bound once more elsewhere
        lambda s: s + 'BASE = "https://other.example"\n',
        # a branch that is not a literal
        lambda s: s.replace('BASE = "https://api-stg.example"', 'BASE = os.environ["BASE"]'),
        # a branch that binds it twice
        lambda s: s.replace(
            '    BASE = "https://api-stg.example"\n',
            '    BASE = "https://api-stg.example"\n    BASE = "https://api-stg2.example"\n',
        ),
        # a literal that is no URL
        lambda s: s.replace('"http://api.sandbox.example"', '"sandbox"'),
        # bound in a nested block, not at the branch's own level
        lambda s: s.replace(
            '    BASE = "https://api-stg.example"\n',
            '    if True:\n        BASE = "https://api-stg.example"\n',
        ),
    ],
)
def test_anything_short_of_a_literal_in_every_branch_is_read_as_before(change) -> None:
    [claim] = _claims(change(ALTERNATIVES))
    assert claim.meta["base"] == "unknown"
    assert "alternatives" not in claim.meta
