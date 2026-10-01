"""TypeScript requests for the cross-repository join (deployment HTTP roadmap, M2).

superagent requests are read like fetch and axios: on a receiver the file proves --
superagent itself, or a module-level agent nothing reconfigures -- with a URL the
source states. Every TypeScript request whose origin the source states also records
the generic ``http-client-call`` claim, in exactly the Python parser's shape, so the
P4 join reads a TypeScript consumer without a change. superagent's Node client
requests ``http`` for a URL without a scheme, which is read as such and marked.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from repoatlas.graph.linker import ReferenceLinker
from repoatlas.models.parsing import EdgeType, ParsedReference
from repoatlas.parsers.http_facts import HTTP_CLIENT_CALL, PENDING_FACT
from repoatlas.parsers.typescript import TypeScriptParser
from repoatlas.services.workspace import Workspace
from tests.http_crossing_corpus import ACCOUNTS, links
from tests.impact_corpus import build


def d(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def _references(source: str) -> list[ParsedReference]:
    return list(TypeScriptParser().parse(Path("client.ts"), d(source)).references)


def _claims(source: str) -> list[dict[str, object]]:
    """Each client claim's key and the meta the join reads."""
    return [
        {
            "key": r.target_key,
            "origin": r.meta["origin"],
            "segments": json.loads(r.meta["segments"]),
            "source": r.meta["source"],
            "lines": (r.line_start, r.line_end),
            **{k: r.meta[k] for k in ("base_at", "scheme_from") if k in r.meta},
        }
        for r in _references(source)
        if r.meta.get(PENDING_FACT) == HTTP_CLIENT_CALL
    ]


def _facts(source: str) -> set[tuple[str, str]]:
    return {
        (r.edge_type.value, r.target_key)
        for r in _references(source)
        if r.edge_type in {EdgeType.CALLS_HTTP_ENDPOINT, EdgeType.CALLS_HTTP_ROUTE}
        and PENDING_FACT not in r.meta
    }


# -- superagent ----------------------------------------------------------------------------


def test_a_literal_superagent_request_is_a_fact_and_a_claim() -> None:
    source = """\
        import superagent from 'superagent';

        export async function load(id: string) {
          return superagent.del('https://api.example/users/1').set('a', 'b');
        }
    """
    assert _facts(source) == {
        ("CALLS_HTTP_ENDPOINT", "DELETE https://api.example/users/1"),
        ("CALLS_HTTP_ROUTE", "DELETE /users/1"),
    }
    assert _claims(source) == [
        {
            "key": "DELETE https://api.example/users/1",
            "origin": "https://api.example",
            "segments": ["users", "1"],
            "source": "superagent",
            "lines": (4, 4),
        }
    ]


def test_a_template_on_an_env_default_base_without_a_scheme_is_requested_over_http() -> None:
    source = """\
        import superagent from 'superagent';

        const devicesBaseUrl = process.env.DEVICES_BASE_URL ?? 'svc-c7';

        export async function listSiteDevices(siteId: string) {
          const response = await superagent.get(`${devicesBaseUrl}/sites/${siteId}/devices`);
          return response.body;
        }
    """
    assert _facts(source) == set()
    assert _claims(source) == [
        {
            "key": "GET http://svc-c7/sites/{siteId}/devices",
            "origin": "http://svc-c7",
            "segments": ["sites", None, "devices"],
            "source": "superagent",
            "lines": (6, 6),
            "base_at": "3",
            "scheme_from": "superagent",
        }
    ]


def test_an_agent_is_read_unless_something_can_rewrite_its_urls() -> None:
    agent = """\
        import request from 'superagent';

        const agent = request.agent().set('x', 'y');
        const plain = request.agent();

        export const a = () => plain.post('https://api.example/login');
    """
    assert [c["key"] for c in _claims(agent)] == ["POST https://api.example/login"]
    configured = agent + "plain.use(prefix);\n"
    assert _claims(configured) == []


@pytest.mark.parametrize(
    "call",
    [
        # a plugin on the request itself can rewrite its URL
        "superagent.get('https://api.example/x').use(prefix)",
        # relative: superagent's Node client has nothing to resolve it against
        "superagent.get('/x')",
        # a base glued to the next text changes the host itself
        "superagent.get(`${BASE}x/${id}`)",
        "superagent.get(`${BASE}x/users/${id}`)",
        # a base the file does not state
        "superagent.get(`${base}/x`)",
        "superagent.get(`${EMPTY}/x`)",
        # a placeholder that does not fill a whole segment
        "superagent.get(`${BASE}/x-${id}`)",
        # not a request method
        "superagent.agent()",
    ],
)
def test_an_unstated_superagent_request_makes_no_claim(call: str) -> None:
    source = f"""\
        import superagent from 'superagent';

        const BASE = 'https://api.example';
        const EMPTY = process.env.X ?? '';

        export function f(base: string, id: string) {{
          return {call};
        }}
    """
    assert _claims(source) == []
    assert _facts(source) == set()


def test_superagent_shadowed_where_it_is_called_is_not_superagent() -> None:
    source = """\
        import superagent from 'superagent';

        export function f(superagent: Client) {
          return superagent.get('https://api.example/x');
        }
    """
    assert _claims(source) == []


# -- fetch and axios -----------------------------------------------------------------------


def test_fetch_on_a_constant_base_is_a_claim_with_the_init_s_method() -> None:
    source = """\
        const APP_URL = 'https://app.example';

        export async function login(email: string) {
          await fetch(`${APP_URL}/api/auth/login`, { method: 'POST', body: email });
          await fetch(`${APP_URL}/api/users/${email}`);
          await fetch(`${APP_URL}/api/unknown`, options);
          await fetch(`${APP_URL}/api/lower`, { method: 'patch' });
        }
    """
    assert [(c["key"], c["base_at"]) for c in _claims(source)] == [
        ("POST https://app.example/api/auth/login", "1"),
        ("GET https://app.example/api/users/{email}", "1"),
    ]


def test_fetch_never_supplies_a_scheme() -> None:
    source = "const BASE = 'svc-c7';\nexport const f = () => fetch(`${BASE}/sites`);\n"
    assert _claims(source) == []


def test_every_literal_absolute_request_fact_has_its_claim() -> None:
    source = """\
        import axios from 'axios';

        export async function both() {
          await fetch('https://app.example/api/auth/logout', { method: 'POST' });
          await axios.get('https://api.example/items?page=2');
          await fetch('/relative');
        }
    """
    assert [(c["key"], c["source"], c["segments"]) for c in _claims(source)] == [
        ("POST https://app.example/api/auth/logout", "fetch", ["api", "auth", "logout"]),
        ("GET https://api.example/items", "axios", ["items"]),
    ]


# -- the P4 join, unchanged ------------------------------------------------------------------

WEB = {
    "package.json": '{"name": "web"}\n',
    "src/users.ts": d("""
        import superagent from 'superagent';

        const ACCOUNTS = process.env.ACCOUNTS_URL ?? 'https://accounts.example/api';

        export async function loadUser(id: string) {
          return superagent.get(`${ACCOUNTS}/users/${id}`);
        }

        export async function loadSettings(id: string) {
          return fetch(`${ACCOUNTS}/users/${id}/settings`);
        }

        export async function loadLiteral() {
          return fetch('https://accounts.example/api/users/1');
        }

        export async function unknownService(id: string) {
          return superagent.get(`https://elsewhere.example/users/${id}`);
        }
    """),
}


@pytest.fixture
def joined(workspace: Workspace, tmp_path: Path) -> Workspace:
    return build(workspace, tmp_path, {"accounts": ACCOUNTS, "web": WEB})


def test_a_typescript_request_joins_a_python_handler_through_the_same_join(
    joined: Workspace,
) -> None:
    assert links(joined) == {
        ("loadUser", "accounts", "get_user", "GET https://accounts.example/api/users/<user_id>"),
        (
            "loadSettings",
            "accounts",
            "get_settings",
            "GET https://accounts.example/api/users/<user_id>/settings",
        ),
        ("loadLiteral", "accounts", "get_user", "GET https://accounts.example/api/users/<user_id>"),
    }
    plan = ReferenceLinker(joined.knowledge).prepare_global().http_crossings
    rows = joined.database.execute(
        """
        SELECT u.id, s.qualified_name FROM unresolved_refs u JOIN entities s ON s.id = u.src_id
        WHERE u.meta_json LIKE ?
        """,
        (f'%"{HTTP_CLIENT_CALL}"%',),
    ).fetchall()
    decided = {row[1]: plan.decisions[row[0]] for row in rows}
    assert decided["unknownService"] == "http-service-unmatched"
