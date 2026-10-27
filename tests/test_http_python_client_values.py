"""A request's URL and a client's base may be built from module constants (Python client base, M5).

``BASE = f"{HOST}/api"`` and ``requests.get(f"{BASE}/users/{uid}")`` say the same as the URL written
out, so the request is a claim on that origin -- within one file, on the evaluator the route paths
use (M4). The text is joined exactly as written: nothing is trimmed or inserted, a name filling
the path must bring its own ``/``, and wherever a value meets other text the place must be one a URL
separates. Anything short of proof gives the output the reader gave before, byte for byte. Half of
this file is what stays as it was.

The reasons a read gives nothing are internal (``PythonHttp.client_reads``), never stored.
"""

from __future__ import annotations

import ast
import json
import sys
import textwrap
from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.graph.http_crossings import (
    LINKED,
    SERVICE_UNMATCHED,
    UNKNOWN_BASE,
    decision_rows,
    plan_http_crossings,
)
from repoatlas.graph.http_declarations import NOT_BASE_UNKNOWN, facts_of
from repoatlas.graph.link_exposures import LINK_SOURCE
from repoatlas.graph.service_declarations import external_state
from repoatlas.models.parsing import EdgeType, EntityType, ParsedReference
from repoatlas.models.service_declarations import ExternalServiceFields
from repoatlas.parsers.http_facts import (
    BASE_UNKNOWN,
    HTTP_CLIENT_CALL,
    HTTP_EXPOSED_ENDPOINT,
    PENDING_FACT,
    PYTHON_CONSTANTS,
)
from repoatlas.parsers.http_python import PythonHttp
from repoatlas.parsers.python import PythonParser
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace
from repoatlas.storage.protocols import UnresolvedRefRecord
from tests.http_crossing_corpus import ACCOUNTS
from tests.http_scoped_corpus import repos as scoped_repos
from tests.impact_corpus import build
from tests.incremental_corpus import assert_equals_fresh, edit, sync_all
from tests.test_http_python_clients import (
    FILES,
    _claims,
    _decisions,
    _facts,
    _http,
    _record,
    _service,
    _shown,
)
from tests.test_http_scoped_declarations import decided as scoped_decided
from tests.test_http_scoped_declarations import declare, resync, state_of

d = textwrap.dedent


def _reads(source: str) -> tuple[PythonHttp, list[ParsedReference]]:
    """The reader, after it has read every call of the module in source order."""
    tree = ast.parse(d(source))
    http = PythonHttp(tree)
    found: list[ParsedReference] = []
    calls = sorted(
        (node for node in ast.walk(tree) if isinstance(node, ast.Call)),
        key=lambda node: (node.lineno, node.col_offset),
    )
    for call in calls:
        found.extend(http.call_references(call, None))
    return http, found


def _outcomes(source: str) -> list[str]:
    return [read.outcome for read in _reads(source)[0].client_reads]


def _one(source: str) -> ParsedReference:
    [claim] = _claims(source)
    return claim


def _segments(claim: ParsedReference) -> list[str | None]:
    return json.loads(claim.meta["segments"])


def _without(claim: ParsedReference, *dropped: str) -> tuple[object, ...]:
    meta = {key: value for key, value in claim.meta.items() if key not in dropped}
    return (claim.target_key, claim.source_symbol, claim.line_start, claim.line_end, meta)


# -- what is read ---------------------------------------------------------------------------------


def test_literal_and_literal_constant_heads_are_unchanged() -> None:
    literal = """\
    import requests

    def load():
        return requests.get("https://a.example/users/123?expand=roles")
    """
    claim = _one(literal)
    assert claim.target_key == "GET https://a.example/users/123"
    assert (claim.source_symbol, claim.line_start, claim.line_end) == ("load", 4, 4)
    assert claim.meta == {
        PENDING_FACT: HTTP_CLIENT_CALL,
        "method": "GET",
        "origin": "https://a.example",
        "segments": '["users", "123"]',
        "source": "requests",
    }
    # A literal URL is not composed at all.
    assert _reads(literal)[0].client_reads == []

    constant = """\
    import httpx

    BASE = "https://A.example/api"


    def create(uid):
        return httpx.post(f"{BASE}/users/{uid}")
    """
    claim = _one(constant)
    assert claim.target_key == "POST https://a.example/api/users/{uid}"
    assert claim.meta == {
        PENDING_FACT: HTTP_CLIENT_CALL,
        "method": "POST",
        "origin": "https://a.example",
        "segments": '["api", "users", null]',
        "source": "httpx",
        "base_at": "3",
    }
    assert _outcomes(constant) == ["unchanged"]

    environment = """\
    import os
    import requests

    BASE = os.environ.get("ACCOUNTS_URL", "https://accounts.example/api")


    def load(uid):
        return requests.get(f"{BASE}/users/{uid}")
    """
    claim = _one(environment)
    assert claim.meta == {
        PENDING_FACT: HTTP_CLIENT_CALL,
        "method": "GET",
        "origin": "https://accounts.example",
        "segments": '["api", "users", null]',
        "source": "requests",
        "base_at": "4",
    }
    assert _outcomes(environment) == ["unchanged"]


def test_m5_guard_literal_only_module_byte_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    """A module that states every URL and base it uses reads exactly as it did before composing:
    every reference it yields, claims and facts, is the one the reader gave with composing off."""
    source = d("""\
    import os

    import httpx
    import requests

    if os.environ.get("ACCOUNT") == "prod":
        ALT = "https://api-prd.example"
    else:
        ALT = "http://api.sandbox.example"

    LIT = "https://a.example/api/items/7"
    CONST = "https://c.example/api"
    ENV = os.environ.get("ENV_URL", "https://e.example/api")
    stated = httpx.Client(base_url=CONST)
    unstated = httpx.Client(base_url=os.environ["UNSTATED"])


    def calls(uid, host):
        requests.get("https://a.example/users/1?x=1")
        requests.get(LIT)
        requests.get(f"{CONST}/users/{uid}")
        requests.post(f"{ENV}/users/{uid}")
        requests.get(f"{ALT}/vpn/{uid}/keys")
        requests.get(f"http://{host}/users/{uid}")
        stated.get(f"/users/{uid}")
        unstated.get(f"/users/{uid}")
    """)
    composing = PythonParser().parse(Path("m.py"), source).references
    with monkeypatch.context() as patch:
        patch.setattr(PythonHttp, "_compose_client", lambda self, *args, **kwargs: None)
        patch.setattr(PythonHttp, "_composed_base", lambda self, *args, **kwargs: None)
        before = PythonParser().parse(Path("m.py"), source).references
    assert [r.model_dump_json() for r in composing] == [r.model_dump_json() for r in before]
    claims = [r for r in composing if r.meta.get(PENDING_FACT) == HTTP_CLIENT_CALL]
    # The literal URL, the constant, the default, two alternatives, the host that is a value, and
    # the two clients: every kind of request this must leave alone is in the module.
    assert len(claims) == 9


def test_composed_head_resolves_like_its_literal() -> None:
    composed = """\
    import requests

    HOST = "https://h.example"
    BASE = f"{HOST}/api"


    def load(uid):
        return requests.get(f"{BASE}/users/{uid}")
    """
    literal = """\
    import requests

    # HOST and BASE
    # written out below


    def load(uid):
        return requests.get(f"https://h.example/api/users/{uid}")
    """
    claim, written = _one(composed), _one(literal)
    assert claim.target_key == "GET https://h.example/api/users/{uid}"
    assert claim.meta["origin"] == "https://h.example"
    assert _segments(claim) == ["api", "users", None]
    assert claim.meta["base_at"] == "4"
    assert "base_at" not in written.meta
    assert _without(claim, "base_at") == _without(written, "base_at")
    assert _facts(composed) == []
    http, _ = _reads(composed)
    [read] = http.client_reads
    assert read.outcome == "composed"
    assert (read.scheme, read.host, read.port) == ("https", "h.example", None)
    assert (read.base_segments, read.endpoint_segments) == (("api",), ("users", None))


def test_fstring_host_part_from_constant() -> None:
    source = """\
    import requests

    HOST = "h.example"


    def load():
        return requests.get(f"https://{HOST}/users")
    """
    claim = _one(source)
    assert claim.target_key == "GET https://h.example/users"
    assert claim.meta["origin"] == "https://h.example"
    assert _segments(claim) == ["users"]
    assert claim.meta["base_at"] == "3"
    assert _facts(source) == []


def test_concatenated_bases() -> None:
    constant = """\
    import requests

    BASE = "https://a.example/api"


    def users():
        return requests.get(BASE + "/users")
    """
    claim = _one(constant)
    assert claim.target_key == "GET https://a.example/api/users"
    assert (claim.meta["origin"], claim.meta["base_at"]) == ("https://a.example", "3")
    assert _segments(claim) == ["api", "users"]

    composed = """\
    import requests

    HOST = "https://h.example"
    API = f"{HOST}/api"


    def user(uid):
        return requests.get(API + "/users/" + uid)
    """
    claim = _one(composed)
    assert claim.target_key == "GET https://h.example/api/users/{uid}"
    assert (claim.meta["origin"], claim.meta["base_at"]) == ("https://h.example", "4")
    assert _segments(claim) == ["api", "users", None]
    assert _facts(composed) == []


@pytest.mark.parametrize(
    ("import_line", "name"),
    [
        ("from config import BASE", "BASE"),
        ("from .config import BASE", "BASE"),
        ("import config", "config.BASE"),
    ],
    ids=["absolute", "relative", "module_attr"],
)
def test_imported_base_is_unchanged(import_line: str, name: str) -> None:
    """An imported name is another module's to say: this file reads it as a value it does not know,
    and is left exactly as it is when that name is a parameter."""
    source = f"""\
    import requests
    {import_line}

    def load(uid):
        return requests.get(f"{{{name}}}/users/{{uid}}")
    """
    parameter = name.partition(".")[0]
    control = f"""\
    import requests
    # {import_line}

    def load(uid, {parameter}):
        return requests.get(f"{{{name}}}/users/{{uid}}")
    """
    claim = _one(source)
    assert claim.target_key == "GET {" + name + "}/users/{uid}"
    assert claim.meta["base"] == BASE_UNKNOWN and "origin" not in claim.meta
    assert [c.model_dump_json() for c in _claims(control)] == [claim.model_dump_json()]
    assert _outcomes(source) == ["client-imported"]
    assert _outcomes(control) == ["unchanged"]


def test_httpx_composed_base_trailing_slash_and_rooted_path() -> None:
    inline = """\
    import httpx

    H = "https://h.example"
    client = httpx.Client(base_url=f"{H}/v1/")


    def users():
        return client.get("/users")
    """
    claim = _one(inline)
    assert claim.target_key == "GET https://h.example/v1/users"
    assert _segments(claim) == ["v1", "users"]
    # An inline base is stated where it is written.
    assert (claim.meta["base_at"], claim.meta["client_at"]) == ("4", "4")
    assert claim.meta["client"] == "client"
    assert _facts(inline) == []

    named = """\
    import httpx

    H = "https://h.example"
    BASE = f"{H}/v1/"
    client = httpx.Client(base_url=BASE)


    def users():
        return client.get("/users")
    """
    claim = _one(named)
    assert claim.target_key == "GET https://h.example/v1/users"
    assert _segments(claim) == ["v1", "users"]
    assert (claim.meta["base_at"], claim.meta["client_at"]) == ("4", "5")


def test_string_composition_keeps_slashes_as_written() -> None:
    template = """\
    import requests

    HOST = "https://h.example"
    BASE = f"{HOST}/api/"


    def load():
        return requests.get({url})
    """
    doubled = _one(template.replace("{url}", 'f"{BASE}/users"'))
    assert doubled.target_key == "GET https://h.example/api//users"
    assert _segments(doubled) == ["api", "", "users"]
    # What the literal says is the same shape.
    literal = _one(
        "import requests\n\ndef load():\n    return requests.get('https://h.example/api//users')\n"
    )
    assert _segments(literal) == _segments(doubled)
    assert literal.target_key == doubled.target_key

    single = _one(template.replace("{url}", 'f"{BASE}users"'))
    assert single.target_key == "GET https://h.example/api/users"
    assert _segments(single) == ["api", "users"]


def test_dynamic_host_keeps_path_never_joins() -> None:
    source = """\
    import requests


    def load(host, uid):
        return requests.get(f"https://{host}.example.com/api/users")
    """
    claim = _one(source)
    assert claim.target_key == "GET https://{host}.example.com/api/users"
    assert claim.meta == {
        PENDING_FACT: HTTP_CLIENT_CALL,
        "method": "GET",
        "segments": '["api", "users"]',
        "source": "requests",
        "base": BASE_UNKNOWN,
    }
    assert _facts(source) == []
    plan = plan_http_crossings([_service(), _record(2, claim)], FILES, producer="linker:global")
    assert plan.edges == ()
    [call] = plan.calls
    assert call.decision == UNKNOWN_BASE
    [row] = decision_rows(plan)
    # The host is an expression, so no declaration can name it.
    assert facts_of(row, "web").scope_refusal == "host-expression"


def _published(deployment: str) -> UnresolvedRefRecord:
    """``svc`` publishes ``GET /users/{id}`` under ``deployment``."""
    return UnresolvedRefRecord(
        id=1,
        repo_id=1,
        file_id=1,
        src_id=11,
        edge_type=EdgeType.CALLS_HTTP_ENDPOINT,
        target_kind=EntityType.HTTP_ENDPOINT,
        target_key=f"GET {deployment}/users/{{id}}",
        line_start=5,
        line_end=5,
        meta_json=json.dumps(
            {
                PENDING_FACT: HTTP_EXPOSED_ENDPOINT,
                "method": "GET",
                "route": "/users/{id}",
                "deployment": deployment,
                "identity_source": "flasgger-swagger2",
                "identity_at": "3",
            }
        ),
    )


def test_port_from_constant_joins_only_that_port() -> None:
    source = """\
    import requests

    HOST = "api.example.com"
    PORT = "8443"


    def load(uid):
        return requests.get(f"https://{HOST}:{PORT}/users/{uid}")
    """
    claim = _one(source)
    assert claim.target_key == "GET https://api.example.com:8443/users/{uid}"
    assert claim.meta["origin"] == "https://api.example.com:8443"
    assert claim.meta["base_at"] == "3"
    assert _segments(claim) == ["users", None]
    [read] = _reads(source)[0].client_reads
    assert (read.scheme, read.host, read.port) == ("https", "api.example.com", "8443")

    def decision_at(deployment: str) -> tuple[str, int]:
        plan = plan_http_crossings(
            [_published(deployment), _record(2, claim)], FILES, producer="linker:global"
        )
        [call] = plan.calls
        return call.decision, len(plan.edges)

    assert decision_at("https://api.example.com:8443") == (LINKED, 1)
    assert decision_at("https://api.example.com") == (SERVICE_UNMATCHED, 0)
    assert decision_at("https://api.example.com:9443") == (SERVICE_UNMATCHED, 0)


@pytest.mark.parametrize(
    "prelude",
    ["", "PORT = 8443\n"],
    ids=["parameter", "integer"],
)
def test_non_literal_port_keeps_path_no_origin(prelude: str) -> None:
    name = "PORT" if prelude else "port"
    source = (
        f"import requests\n{prelude}\n\n"
        "def load(port):\n"
        f'    return requests.get(f"https://api.example.com:{{{name}}}/users")\n'
    )
    claim = _one(source)
    assert claim.target_key == "GET https://api.example.com:{" + name + "}/users"
    assert claim.meta["base"] == BASE_UNKNOWN
    assert "origin" not in claim.meta and "base_at" not in claim.meta
    assert _segments(claim) == ["users"]
    assert _facts(source) == []


# -- what is left as it was -----------------------------------------------------------------------


def test_reassigned_base_unchanged() -> None:
    rebound_host = """\
    import requests

    HOST = "https://a.example"
    HOST = "https://b.example"
    BASE = f"{HOST}/api"


    def load(uid):
        return requests.get(f"{BASE}/users/{uid}")
    """
    rebound_base = """\
    import requests

    HOST = "https://a.example"
    BASE = f"{HOST}/api"
    BASE = "https://other.example"


    def load(uid):
        return requests.get(f"{BASE}/users/{uid}")
    """
    for source in (rebound_host, rebound_base):
        claim = _one(source)
        assert claim.target_key == "GET {BASE}/users/{uid}"
        assert claim.meta["base"] == BASE_UNKNOWN and "origin" not in claim.meta


def test_conditional_base_unchanged() -> None:
    conditional = """\
    import requests


    def load(A, B, c, uid):
        return requests.get(f"{A if c else B}/users/{uid}")
    """
    claim = _one(conditional)
    assert claim.target_key == "GET {A if c else B}/users/{uid}"
    assert "origin" not in claim.meta

    client = """\
    import httpx


    def load(A, B, c):
        with httpx.Client(base_url=A if c else B) as client:
            return client.get("/users")
    """
    claim = _one(client)
    assert claim.target_key == "GET {client.base_url}/users"
    assert claim.meta["base"] == BASE_UNKNOWN
    assert "origin" not in claim.meta and "base_expr" not in claim.meta

    branches = """\
    import requests

    HOST = "https://h.example"
    if FLAG:
        BASE = f"{HOST}/a"
    else:
        BASE = f"{HOST}/b"


    def load(uid):
        return requests.get(f"{BASE}/users/{uid}")
    """
    claim = _one(branches)
    assert claim.target_key == "GET {BASE}/users/{uid}"
    assert "origin" not in claim.meta and "alternatives" not in claim.meta


def test_star_rebindable_base_unchanged() -> None:
    rebindable = """\
    HOST = "https://h.example"
    BASE = f"{HOST}/api"
    from helpers import *
    import requests


    def load(uid):
        return requests.get(f"{BASE}/users/{uid}")
    """
    claim = _one(rebindable)
    assert claim.target_key == "GET {BASE}/users/{uid}"
    assert claim.meta["base"] == BASE_UNKNOWN and "origin" not in claim.meta

    # The control: a star before every binding rebinds nothing the module proves.
    leading = """\
    from helpers import *
    HOST = "https://h.example"
    BASE = f"{HOST}/api"
    import requests


    def load(uid):
        return requests.get(f"{BASE}/users/{uid}")
    """
    claim = _one(leading)
    assert claim.target_key == "GET https://h.example/api/users/{uid}"
    assert claim.meta["origin"] == "https://h.example"


def test_environment_bases_keep_existing_behaviour() -> None:
    head = """\
    import os
    import requests

    ENV = os.environ.get("API_URL", "https://e.example/api")


    def load(uid):
        return requests.get(f"{ENV}/users/{uid}")
    """
    claim = _one(head)
    assert claim.target_key == "GET https://e.example/api/users/{uid}"
    assert (claim.meta["origin"], claim.meta["base_at"]) == ("https://e.example", "4")

    # A constant composed from a default is no constant: the default may be overridden.
    composed = """\
    import os
    import requests

    ENV = os.environ.get("API_URL", "https://e.example")
    BASE = f"{ENV}/api"


    def load(uid):
        return requests.get(f"{BASE}/users/{uid}")
    """
    claim = _one(composed)
    assert claim.target_key == "GET {BASE}/users/{uid}"
    assert "origin" not in claim.meta

    subscript = """\
    import os
    import requests


    def load(uid):
        return requests.get(os.environ["API_URL"] + "/users/" + uid)
    """
    claim = _one(subscript)
    assert claim.target_key == "GET {os.environ['API_URL']}/users/{uid}"
    assert claim.meta["base"] == BASE_UNKNOWN and "origin" not in claim.meta


def test_base_used_before_binding_unchanged() -> None:
    client = """\
    import httpx

    client = httpx.Client(base_url=BASE)
    HOST = "https://h.example"
    BASE = f"{HOST}/api"


    def users():
        return client.get("/users")
    """
    claim = _one(client)
    assert claim.target_key == "GET {client.base_url}/users"
    assert claim.meta["base"] == BASE_UNKNOWN and "origin" not in claim.meta
    assert (claim.meta["base_at"], claim.meta["base_expr"]) == ("3", "BASE")

    function = """\
    import requests


    def load(uid):
        return requests.get(f"{BASE}/users/{uid}")


    HOST = "https://h.example"
    BASE = f"{HOST}/api"
    """
    claim = _one(function)
    assert claim.target_key == "GET {BASE}/users/{uid}"
    assert "origin" not in claim.meta

    # The control: the same function, with the binding above it, reads it.
    reordered = (
        'import requests\n\nHOST = "https://h.example"\nBASE = f"{HOST}/api"\n\n\n'
        'def load(uid):\n    return requests.get(f"{BASE}/users/{uid}")\n'
    )
    assert _one(reordered).meta["origin"] == "https://h.example"


def test_cyclic_chain_refused() -> None:
    source = """\
    import requests

    A = f"{B}/x"
    B = f"{A}/y"


    def load(uid):
        return requests.get(f"{A}/users/{uid}")
    """
    claim = _one(source)
    assert claim.target_key == "GET {A}/users/{uid}"
    assert "origin" not in claim.meta
    assert _outcomes(source) == ["unchanged"]


def test_function_local_and_shadowed_base_unchanged() -> None:
    template = """\
    import requests

    HOST = "https://h.example"
    BASE = f"{HOST}/api"


    def load(uid):
        return requests.get(f"{BASE}/users/{uid}")


    {more}
    """
    control = _one(template.replace("{more}", ""))
    assert control.meta["origin"] == "https://h.example"

    local = _one(template.replace("{more}", 'def local():\n        BASE = "https://other.example"'))
    shadowed = _one(template.replace("{more}", "def shadow(uid, BASE):\n        return BASE"))
    for claim in (local, shadowed):
        assert claim.target_key == "GET {BASE}/users/{uid}"
        assert "origin" not in claim.meta
    local_host = _one(template.replace("{more}", 'def local():\n        HOST = "x"'))
    assert local_host.target_key == "GET {BASE}/users/{uid}"


# -- the join and the declarations ----------------------------------------------------------------


def _link(deployment: str) -> UnresolvedRefRecord:
    """What an operator's deployment link states: ``svc`` is reached at ``deployment``."""
    return UnresolvedRefRecord(
        id=3,
        repo_id=1,
        file_id=1,
        src_id=11,
        edge_type=EdgeType.EXPOSES_HTTP_ENDPOINT,
        target_kind=EntityType.HTTP_ENDPOINT,
        target_key=f"GET {deployment}/users/{{id}}",
        line_start=5,
        line_end=5,
        meta_json=json.dumps(
            {
                PENDING_FACT: HTTP_EXPOSED_ENDPOINT,
                "method": "GET",
                "route": "/users/{id}",
                "deployment": deployment,
                "identity_source": LINK_SOURCE,
                "source": LINK_SOURCE,
                "scope": "link:1",
                "link_id": "1",
                "link_in": "@deployment-links/1",
                "link_label": "production",
                "link_callers": "[]",
                "link_verification": "unverified",
            },
            sort_keys=True,
        ),
    )


COMPOSED_USER = """\
import requests
HOST = "https://svc.example"
BASE = f"{HOST}/v2"
def user(uid):
    return requests.get(f"{BASE}/users/{uid}")
"""
LITERAL_USER = """\
import requests
#
#
def user(uid):
    return requests.get(f"https://svc.example/v2/users/{uid}")
"""


def test_a1_link_joins_composed_origin_like_literal() -> None:
    plans = []
    for source in (COMPOSED_USER, LITERAL_USER):
        claim = _one(source)
        plan = plan_http_crossings(
            [_link("https://svc.example/v2"), _record(2, claim)], FILES, producer="linker:global"
        )
        [call] = plan.calls
        assert call.decision == LINKED
        statuses = {hop.kind: hop.status for hop in call.chain}
        assert statuses["origin"] == "proven"
        expected = {
            "service_identity": "declared",
            "deployment_binding": "declared",
            "source_binding": "declared",
            "route": "proven",
            "handler": "proven",
        }
        assert {kind: statuses[kind] for kind in expected} == expected
        [edge] = plan.edges
        assert (edge.src_id, edge.dst_id, edge.type) == (22, 11, EdgeType.CALLS_HTTP_HANDLER)
        plans.append(json.loads(edge.resolution or "{}")["http"])
    composed, literal = plans
    assert composed["base_at"] == "client.py:3" and "base_at" not in literal
    assert composed["url"] == literal["url"] == "https://svc.example/v2/users/{uid}"
    assert composed["link_id"] == literal["link_id"] == "1"

    def stable(record: dict[str, str]) -> dict[str, str]:
        return {k: v for k, v in record.items() if k not in {"base_at", "decision_ref"}}

    assert stable(composed) == stable(literal)


def test_a1_link_at_other_origin_does_not_join() -> None:
    claim = _one(COMPOSED_USER)
    # The same host at another port, and another scheme, is not where the request goes.
    for deployment in ("https://svc.example:8443/v2", "http://svc.example/v2"):
        plan = plan_http_crossings(
            [_link(deployment), _record(2, claim)], FILES, producer="linker:global"
        )
        [call] = plan.calls
        assert call.decision == SERVICE_UNMATCHED
        assert plan.edges == ()
        assert "declaration_ids" not in call.evidence


@pytest.fixture
def scoped(workspace: Workspace, tmp_path: Path) -> Workspace:
    return build(workspace, tmp_path, scoped_repos())


def test_a2_declaration_stops_applying_when_source_states_base(
    scoped: Workspace, tmp_path: Path
) -> None:
    """The control: a declaration on ``BASE`` in ``read_item`` joins its request by scope. Once the
    module composes ``BASE`` from constants the request states its origin, so nothing is left for
    the declaration to say, and it is invalidated for that reason."""
    ws = scoped
    link = declare(ws, "alpha", "caller", "client.py", "BASE", symbol="read_item")
    resync(ws, tmp_path)
    assert state_of(ws, link) == ("applied", "")
    assert scoped_decided(ws)[("caller", "client.py", "read_item")].decision == LINKED

    source = tmp_path / "repos" / "caller" / "client.py"
    text = source.read_text()
    base = 'BASE = os.environ["ITEMS_BASE"]'
    shadow = (
        "def shadowed_read(BASE, item_id):\n"
        '    return requests.get(f"{BASE}/v1/items/{item_id}", timeout=5).json()'
    )
    assert text.count(base) == 1 and text.count(shadow) == 1
    text = text.replace(base, 'ROOT = "https://items.example.test"\nBASE = f"{ROOT}/v1"')
    # A parameter named BASE would be a second binding of it.
    text = text.replace(shadow, shadow.replace("BASE", "base"))
    source.write_text(text)
    resync(ws, tmp_path)

    assert scoped_decided(ws)[("caller", "client.py", "read_item")].decision != UNKNOWN_BASE
    assert state_of(ws, link) == ("invalidated", "base-no-longer-requested")
    [row] = [
        r
        for r in ws.knowledge.list_http_call_decisions()
        if r.url == "https://items.example.test/v1/v1/items/{item_id}"
    ]
    assert facts_of(row, "caller").scope_refusal == NOT_BASE_UNKNOWN


def test_external_service_classifies_composed_origin_like_literal() -> None:
    composed = """\
    import requests
    HOST = "https://API.Example.com"
    BASE = f"{HOST}/v1"
    def ping(uid):
        return requests.get(f"{BASE}/ping/{uid}")
    """
    literal = """\
    import requests
    #
    #
    def ping(uid):
        return requests.get(f"https://API.Example.com/v1/ping/{uid}")
    """
    fields = ExternalServiceFields(origin="https://api.example.com", label="vendor")
    states = []
    origins = []
    for source in (composed, literal):
        plan = plan_http_crossings([_record(2, _one(source))], FILES, producer="linker:global")
        [row] = decision_rows(plan)
        fact = facts_of(row, "web")
        assert fact.status == SERVICE_UNMATCHED
        origins.append(fact.target.origin)
        states.append(external_state(fields, [fact], None, set(), built=True))
    assert origins == ["https://api.example.com"] * 2
    assert states == [("applied", "")] * 2


# -- incremental == fresh -------------------------------------------------------------------------

PORTAL = """\
import httpx
import requests

HOST = "https://accounts.example"
BASE = f"{HOST}/api"
client = httpx.Client(base_url=BASE)
LIMIT = 10


def by_url(user_id):
    return requests.get(f"{BASE}/users/{user_id}")


def by_client(user_id):
    return client.get(f"/users/{user_id}")
"""
DECIDED = {"by_url": LINKED, "by_client": LINKED}


def test_editing_the_constant_moves_the_join(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    build(workspace, tmp_path, {"accounts": ACCOUNTS, "portal": {"client.py": PORTAL}})
    root = tmp_path / "repos"
    assert _decisions(workspace) == DECIDED
    old = 'HOST = "https://accounts.example"'
    assert old in PORTAL
    edit(root, "portal", "client.py", PORTAL.replace(old, 'HOST = "https://elsewhere.example"'))
    sync_all(workspace, root)
    assert _decisions(workspace) == {"by_url": SERVICE_UNMATCHED, "by_client": SERVICE_UNMATCHED}
    assert_equals_fresh(workspace, settings, root, tmp_path, "fresh")


def test_resync_no_change_equals_fresh(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    build(workspace, tmp_path, {"accounts": ACCOUNTS, "portal": {"client.py": PORTAL}})
    root = tmp_path / "repos"
    assert _decisions(workspace) == DECIDED
    sync(workspace, workspace.resolver().resolve(root))
    assert _decisions(workspace) == DECIDED
    assert_equals_fresh(workspace, settings, root, tmp_path, "fresh")


def test_body_edit_changes_nothing(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    """An edit that changes no constant a request reads, on the same line, moves nothing."""
    edited = PORTAL.replace("LIMIT = 10", "LIMIT = 20")
    assert edited != PORTAL
    assert [c.model_dump_json() for c in _claims(edited)] == [
        c.model_dump_json() for c in _claims(PORTAL)
    ]
    build(workspace, tmp_path, {"accounts": ACCOUNTS, "portal": {"client.py": PORTAL}})
    root = tmp_path / "repos"
    assert _decisions(workspace) == DECIDED
    edit(root, "portal", "client.py", edited)
    sync_all(workspace, root)
    assert _decisions(workspace) == DECIDED
    assert_equals_fresh(workspace, settings, root, tmp_path, "fresh")


# -- the reader runs and imports nothing ----------------------------------------------------------


def test_reader_executes_and_imports_nothing_for_clients() -> None:
    probe = "m5_probe_never_imported"
    assert probe not in sys.modules
    source = (
        f"import {probe} as probe\n"
        "import httpx\n"
        "import requests\n"
        "\n"
        'client = httpx.Client(base_url=f"{probe.BASE}/v1")\n'
        'other = httpx.Client(base_url=probe.BASE + "/v2")\n'
        "\n\n"
        "def load(uid):\n"
        '    a = requests.get(f"{probe.HOST}/users/{uid}")\n'
        '    b = requests.get(probe.URL + "/x")\n'
        '    return a, b, client.get("/users"), other.get("/users")\n'
    )
    parsed = PythonParser().parse(Path("client.py"), source)
    assert probe not in sys.modules
    claims = [r for r in parsed.references if r.meta.get(PENDING_FACT) == HTTP_CLIENT_CALL]
    # Every one is a request on a host this file does not state.
    assert claims and all("origin" not in claim.meta for claim in claims)
    assert probe not in sys.modules


# -- refused forms --------------------------------------------------------------------------------


def test_glued_composed_forms_refused() -> None:
    composed = """\
    import requests

    HOST = "https://h.example"
    BASE = f"{HOST}/api"
    ENV = "prod"
    N = "2"


    def load(uid):
        return requests.get({url})
    """
    cases = {
        'f"{BASE}users"': "glued-path",
        'f"{BASE}{uid}"': "glued-path",
        'f"https://api-{ENV}.example.com/x"': "glued-authority",
        'f"https://a.example/v{N}/x"': "glued-path",
    }
    for url, reason in cases.items():
        source = composed.replace("{url}", url)
        assert _claims(source) == [], url
        assert _outcomes(source) == [reason], url


def test_credentials_never_recorded_from_composition() -> None:
    head = """\
    import requests

    HOST = "u:hunter2@h"
    BASE = f"https://{HOST}/api"


    def load(uid):
        return requests.get(f"{BASE}/users/{uid}")
    """
    base = """\
    import httpx

    HOST = "u:hunter2@h"
    BASE = f"https://{HOST}/api"
    client = httpx.Client(base_url=BASE)


    def load():
        return client.get("/users")
    """
    dynamic = """\
    import requests

    HOST = "u:hunter2@h"


    def load(port):
        return requests.get(f"https://{HOST}:{port}/users")
    """
    for source in (head, base, dynamic):
        assert "hunter2" not in _shown(source), source
        for claim in _claims(source):
            assert "origin" not in claim.meta
    # The head and the base are refused for their credentials; the dynamic port is not read at all.
    assert _outcomes(head) == ["credentials"]
    assert _outcomes(dynamic) == ["credentials"]
    assert _claims(dynamic) == []


def test_path_region_name_spliced_only_when_rooted() -> None:
    source = """\
    import requests

    HOST = "https://h.example"
    BASE = f"{HOST}/api"
    TOKEN = "T0/B0/XYZ"
    USERS = "/users"


    def one(uid):
        return requests.get(f"{BASE}/services/{TOKEN}")


    def two(uid):
        return requests.get(f"{BASE}{USERS}/{uid}")
    """
    one, two = _claims(source)
    # A name that does not start with a slash stays a placeholder: its text is never copied.
    assert one.target_key == "GET https://h.example/api/services/{TOKEN}"
    assert _segments(one) == ["api", "services", None]
    assert "XYZ" not in _shown(source) and "T0/B0" not in _shown(source)
    # One that does is part of the path.
    assert two.target_key == "GET https://h.example/api/users/{uid}"
    assert _segments(two) == ["api", "users", None]


def test_rooted_constant_on_httpx_client() -> None:
    source = """\
    import httpx

    USERS = "/users"
    client = httpx.Client(base_url="https://api.example.com/v1")


    def a():
        return client.get(USERS)


    def b(uid):
        return client.get(f"{USERS}/{uid}")
    """
    first, second = _claims(source)
    assert first.target_key == "GET https://api.example.com/v1/users"
    assert _segments(first) == ["v1", "users"]
    assert first.meta == {
        PENDING_FACT: HTTP_CLIENT_CALL,
        "method": "GET",
        "segments": '["v1", "users"]',
        "source": "httpx",
        "origin": "https://api.example.com",
        "base_at": "4",
        "client": "client",
        "client_at": "4",
    }
    assert second.target_key == "GET https://api.example.com/v1/users/{uid}"
    assert _segments(second) == ["v1", "users", None]
    assert _facts(source) == []


def test_composed_base_url_and_invalid_falls_back() -> None:
    named = """\
    import httpx

    HOST = "https://h.example"
    BASE = f"{HOST}/api"
    client = httpx.Client(base_url=BASE)


    def users():
        return client.get("/users")
    """
    claim = _one(named)
    assert claim.target_key == "GET https://h.example/api/users"
    assert (claim.meta["base_at"], claim.meta["client_at"]) == ("4", "5")
    assert _segments(claim) == ["api", "users"]

    inline = """\
    import httpx

    HOST = "https://h.example"
    client = httpx.Client(base_url=f"{HOST}/api")


    def users():
        return client.get("/users")
    """
    claim = _one(inline)
    assert claim.target_key == "GET https://h.example/api/users"
    assert (claim.meta["base_at"], claim.meta["client_at"]) == ("4", "4")

    # A base with a query is no base to append to: the client keeps the base it had, unstated,
    # and is not dropped.
    query = named.replace('f"{HOST}/api"', 'f"{HOST}/api?k=1"')
    claim = _one(query)
    assert claim.target_key == "GET {client.base_url}/users"
    assert claim.meta["base"] == BASE_UNKNOWN and "origin" not in claim.meta
    assert (claim.meta["base_at"], claim.meta["base_expr"]) == ("5", "BASE")


def test_composed_values_never_enter_constants_table() -> None:
    source = d("""\
    import httpx
    import requests

    HOST = "https://h.example"
    BASE = f"{HOST}/api"
    client = httpx.Client(base_url=BASE)


    def load(uid):
        return requests.get(f"{BASE}/users/{uid}"), client.get("/users")
    """)
    tree = ast.parse(source)
    http = PythonHttp(tree)
    constants, values = dict(http._constants), dict(http._values)
    assert set(constants) == {"HOST"} and set(values) == {"BASE"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            http.call_references(node, None)
    assert http._constants == constants and http._values == values
    assert "composed" in [read.outcome for read in http.client_reads]
    [offered] = [
        r
        for r in PythonParser().parse(Path("m.py"), source).references
        if r.meta.get(PENDING_FACT) == PYTHON_CONSTANTS
    ]
    assert json.loads(offered.meta["constants"]) == {"HOST": ["https://h.example", 4]}
    assert set(json.loads(offered.meta["composed"])) == {"BASE"}


# -- bounds ---------------------------------------------------------------------------------------


def test_adversarial_client_bounds() -> None:
    # A URL of 5000 placeholders behind a composed head: refused at the 65th piece, and the
    # older reader's answer (here, none: the shape is too long to be a path) stands.
    head = (
        'import requests\n\nHOST = "https://h.example"\nBASE = f"{HOST}/api"\n\n\n'
        'def load(x):\n    return requests.get(f"{BASE}/a/' + "{x}/" * 5000 + '")\n'
    )
    assert _claims(head) == []
    assert _outcomes(head) == ["too-many-parts"]

    # A chain of 5000 composed constants: C20 is no constant at all, C8 is one the chain's
    # depth bound lets a definition read and a request not, C7 is read.
    rows = ['C0 = "https://h.example"', *(f'C{i} = f"{{C{i - 1}}}/x"' for i in range(1, 5001))]
    chain = (
        "import httpx\nimport requests\n"
        + "\n".join(rows)
        + "\nclient = httpx.Client(base_url=C20)\n\n\n"
        'def head(uid):\n    return requests.get(f"{C20}/users/{uid}")\n\n\n'
        'def deep(uid):\n    return requests.get(f"{C8}/users/{uid}")\n\n\n'
        'def fine(uid):\n    return requests.get(f"{C7}/users/{uid}")\n\n\n'
        'def use():\n    return client.get("/users")\n'
    )
    by_symbol = {claim.source_symbol: claim for claim in _claims(chain)}
    assert by_symbol["head"].target_key == "GET {C20}/users/{uid}"
    assert by_symbol["deep"].target_key == "GET {C8}/users/{uid}"
    assert by_symbol["use"].target_key == "GET {client.base_url}/users"
    for symbol in ("head", "deep", "use"):
        assert "origin" not in by_symbol[symbol].meta
    assert by_symbol["fine"].meta["origin"] == "https://h.example"
    assert _segments(by_symbol["fine"]) == ["x"] * 7 + ["users", None]
    outcomes = _outcomes(chain)
    assert "depth-exceeded" in outcomes and "composed" in outcomes

    # 5000 operands of `+`, built node by node: no source to parse, no claim, no exception.
    tree = ast.parse("import requests\n\n\ndef load():\n    return requests.get('/x')\n")
    call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call))
    anchor = call.args[0]
    node: ast.expr = ast.copy_location(ast.Constant(value="/a"), anchor)
    for _ in range(4999):
        right = ast.copy_location(ast.Constant(value="/a"), anchor)
        node = ast.copy_location(ast.BinOp(left=node, op=ast.Add(), right=right), anchor)
    call.args[0] = node
    http = PythonHttp(tree)
    assert http.call_references(call, "load") == []
    assert [read.outcome for read in http.client_reads] == ["too-many-parts"]


def test_every_claim_of_this_file_is_a_client_claim() -> None:
    """The helpers read what the parser records: no per-file fact is added by composing."""
    source = """\
    import requests

    HOST = "https://h.example"
    BASE = f"{HOST}/api"


    def load(uid):
        return requests.get(f"{BASE}/users/{uid}")
    """
    assert _facts(source) == []
    assert all(r.meta.get(PENDING_FACT) == HTTP_CLIENT_CALL for r in _http(source))


def test_conditional_over_constants_is_never_its_body() -> None:
    """Both branches are proven constants, yet which one applies is a runtime choice: the
    expression is not a proven origin, whichever branch is written first."""
    source = """\
    import requests

    HOST_A = "https://a.example"
    HOST_B = "https://b.example"


    def load(c, uid):
        return requests.get(f"{HOST_A if c else HOST_B}/users/{uid}")
    """
    claim = _one(source)
    assert claim.target_key == "GET {HOST_A if c else HOST_B}/users/{uid}"
    assert "origin" not in claim.meta
    assert claim.meta["base"] == BASE_UNKNOWN
