"""Requests on a transport and a base injected in options objects (deployment HTTP roadmap, M2).

A service wires superagent and its bases in one file and hands them on, through
destructured options, to the functions that make the requests. Each file states its own
part as a claim; the global pass follows the options from the root call that writes them
to every request they reach, through at most three functions and never around a cycle,
and gives each request the ``http-client-call`` claim the unchanged P4 join reads.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from repoatlas.graph.linker import ReferenceLinker
from repoatlas.graph.ts_injection import MAX_HOPS
from repoatlas.models.parsing import ParsedReference
from repoatlas.parsers.http_facts import (
    PENDING_FACT,
    TS_FUNCTION,
    TS_INJECTED_REQUEST,
    TS_INJECTION,
)
from repoatlas.parsers.typescript import TypeScriptParser
from repoatlas.services.workspace import Workspace
from tests.http_crossing_corpus import ACCOUNTS, crossings, links
from tests.impact_corpus import build
from tests.incremental_corpus import assert_equals_fresh, edit, sync_all


def d(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def _claims(source: str, role: str) -> list[ParsedReference]:
    parsed = TypeScriptParser().parse(Path("models.ts"), d(source))
    return [r for r in parsed.references if r.meta.get(PENDING_FACT) == role]


def _requests(source: str) -> list[dict[str, object]]:
    return [
        {
            "key": r.target_key,
            "lines": (r.line_start, r.line_end),
            **{k: v for k, v in r.meta.items() if k not in {PENDING_FACT, "rest"}},
            "rest": json.loads(r.meta["rest"]),
        }
        for r in _claims(source, TS_INJECTED_REQUEST)
    ]


def _injections(source: str) -> list[tuple[str, dict[str, object], str | None]]:
    return [
        (r.target_key, json.loads(r.meta["argument"]), r.meta.get("within"))
        for r in _claims(source, TS_INJECTION)
    ]


# -- the request ---------------------------------------------------------------------------

USER_MODEL = d("""
    export function createUserModel({ accountsBaseUrl, fetcher, timeoutMs }: Opts) {
      return {
        async getUser(id: string) {
          return fetcher.get(`${accountsBaseUrl}/users/${id}`).timeout(timeoutMs);
        },
        async getSettings(id: string) {
          return fetcher
            .get(`${accountsBaseUrl}/users/${id}/settings`)
            .query({ full: true });
        },
        async rewritten(id: string) {
          return fetcher.get(`${accountsBaseUrl}/users/${id}`).use(prefix);
        },
      };
    }
""")


def test_a_request_on_destructured_options_is_a_claim_for_its_callers() -> None:
    assert _requests(USER_MODEL) == [
        {
            "key": "GET {accountsBaseUrl}/users/{id}",
            "lines": (4, 4),
            "function": "createUserModel",
            "transport": "fetcher",
            "base_option": "accountsBaseUrl",
            "method": "GET",
            "rest": [["l", "/users/"], ["s", "id"]],
        },
        {
            "key": "GET {accountsBaseUrl}/users/{id}/settings",
            "lines": (7, 8),
            "function": "createUserModel",
            "transport": "fetcher",
            "base_option": "accountsBaseUrl",
            "method": "GET",
            "rest": [["l", "/users/"], ["s", "id"], ["l", "/settings"]],
        },
    ]
    functions = _claims(USER_MODEL, TS_FUNCTION)
    assert [(f.target_key, json.loads(f.meta["exports"])) for f in functions] == [
        ("createUserModel", ["createUserModel"])
    ]


def test_a_request_on_a_base_its_own_file_states() -> None:
    source = """\
        const BASE = process.env.BASE_URL ?? 'https://accounts.example/api';

        export const remove = ({ http }: Opts) => (id: string) => http.del(`${BASE}/users/${id}`);
    """
    [request] = _requests(source)
    shown = ("key", "function", "transport", "base", "base_at", "method")
    assert {k: request[k] for k in shown} == {
        "key": "DELETE {BASE}/users/{id}",
        "function": "remove",
        "transport": "http",
        "base": "https://accounts.example/api",
        "base_at": "1",
        "method": "DELETE",
    }


@pytest.mark.parametrize(
    "body",
    [
        # a plugin on the request can rewrite its URL
        "return fetcher.get(`${base}/x`).use(prefix);",
        # the transport is not destructured from the options
        "return opts.fetcher.get(`${base}/x`);",
        # the function rebinds its transport
        "fetcher = other; return fetcher.get(`${base}/x`);",
        # a nested function's own parameter is not the option
        "return (base: string) => fetcher.get(`${base}/x`);",
        # the URL does not begin with its base
        "return fetcher.get(`https://accounts.example/${base}`);",
        "return fetcher.get(url);",
        "return fetcher.get(`${base}\\u002fx`);",
        # a base the file does not state
        "return fetcher.get(`${elsewhere}/x`);",
        # not a request method
        "return fetcher.agent(`${base}/x`);",
    ],
)
def test_an_unproven_injected_request_makes_no_claim(body: str) -> None:
    source = f"""\
        export function f({{ fetcher, base }}: Opts, opts: Opts, url: string) {{
          {body}
        }}
    """
    assert _requests(source) == []


# -- the calls that hand options on --------------------------------------------------------

MAIN = d("""
    import superagent from 'superagent';
    import { createModels } from './models';

    const accountsBaseUrl = process.env.ACCOUNTS_URL ?? 'https://accounts.example/api';
    const emptyBaseUrl = process.env.EMPTY_URL ?? '';

    const accountsOpts = { accountsBaseUrl, timeoutMs: 3000, fetcher: superagent };

    export const models = createModels({
      accountsOpts,
      otherOpts: { otherBaseUrl: emptyBaseUrl, fetcher: superagent },
    });
""")

MODELS = d("""
    import { createUserModel } from './userModel';
    import createOtherModel from './otherModel';

    export function createModels({ accountsOpts, otherOpts }: ModelOpts) {
      return {
        users: createUserModel(accountsOpts),
        other: createOtherModel({ otherBaseUrl: otherOpts.url, fetcher: otherOpts.fetcher }),
        renamed: createUserModel({ ...accountsOpts }),
      };
    }
""")

OTHER_MODEL = d("""
    export default function ({ otherBaseUrl, fetcher }: OtherOpts) {
      return { ping: () => fetcher.get(`${otherBaseUrl}/ping`) };
    }
""")


def test_a_root_call_writes_the_transport_and_the_base() -> None:
    [(callee, argument, within)] = _injections(MAIN)
    assert (callee, within) == ("createModels", None)
    assert argument == {
        "object": {
            "accountsOpts": {
                "object": {
                    "accountsBaseUrl": {"string": "https://accounts.example/api", "line": 4},
                    "timeoutMs": None,
                    "fetcher": {"superagent": True},
                }
            },
            "otherOpts": {
                "object": {
                    "otherBaseUrl": {"string": "", "line": 5},
                    "fetcher": {"superagent": True},
                }
            },
        }
    }


def test_a_forward_passes_its_own_options_on_and_a_spread_passes_nothing() -> None:
    # The member reads of `otherOpts` and the spread prove no value: only the
    # whole option handed on is a forward.
    assert _injections(MODELS) == [
        ("createUserModel", {"option": "accountsOpts"}, "createModels"),
    ]


@pytest.mark.parametrize(
    "call",
    [
        # neither supplies superagent nor passes an option on
        "g({ base: 'https://accounts.example/api' })",
        "g({ fetcher: axios, url: 'https://accounts.example/api' })",
        # a nested function's own parameter is no key of the options
        "[1].map(({ other }: O) => g({ other }))",
        # a callee neither declared here nor imported from a relative module
        "lib({ fetcher })",
        # options that are not destructured prove no key
        "g(opts)",
    ],
)
def test_an_unproven_injection_makes_no_claim(call: str) -> None:
    source = f"""\
        import axios from 'axios';
        import {{ lib }} from 'lib';

        function g(options: O) {{}}

        export function f({{ fetcher, base }}: Opts, opts: Opts) {{
          return {call};
        }}
    """
    assert _injections(source) == []


def test_the_names_a_function_is_exported_under() -> None:
    source = """\
        function a({ fetcher, base }: O) { return fetcher.get(`${base}/a`); }
        export function b({ fetcher, base }: O) { return fetcher.get(`${base}/b`); }
        export const c = ({ fetcher, base }: O) => fetcher.get(`${base}/c`);
        export default function ({ fetcher, base }: O) { return fetcher.get(`${base}/d`); }
        export { a as aliased, a };
    """
    functions = _claims(source, TS_FUNCTION)
    assert sorted((f.target_key, tuple(json.loads(f.meta["exports"]))) for f in functions) == [
        ("*default", ("default",)),
        ("a", ("aliased", "a")),
        ("b", ("b",)),
        ("c", ("c",)),
    ]


# -- the global pass, and the join ---------------------------------------------------------

WEB = {
    "package.json": '{"name": "web"}\n',
    "src/main.ts": MAIN,
    "src/models.ts": MODELS,
    "src/userModel.ts": USER_MODEL,
    "src/otherModel.ts": OTHER_MODEL,
}


@pytest.fixture
def joined(workspace: Workspace, tmp_path: Path) -> Workspace:
    return build(workspace, tmp_path, {"accounts": ACCOUNTS, "web": WEB})


def test_an_injected_transport_s_requests_join_the_handlers_they_reach(
    joined: Workspace,
) -> None:
    assert links(joined) == {
        (
            "createUserModel.getUser",
            "accounts",
            "get_user",
            "GET https://accounts.example/api/users/<user_id>",
        ),
        (
            "createUserModel.getSettings",
            "accounts",
            "get_settings",
            "GET https://accounts.example/api/users/<user_id>/settings",
        ),
    }
    evidence = {
        (row["line_start"], row["line_end"], row["asserted_by"], at["injected_at"], at["base_in"])
        for row in crossings(joined)
        for at in [row["resolution"]["http"]]
    }
    assert evidence == {
        (4, 4, "src/userModel.ts", "src/main.ts:9", "src/main.ts:4"),
        (7, 8, "src/userModel.ts", "src/main.ts:9", "src/main.ts:4"),
    }


def test_an_empty_base_names_no_origin(joined: Workspace) -> None:
    plan = ReferenceLinker(joined.knowledge).prepare_global()
    assert sorted(record.target_key for record in plan.http.injected) == [
        "GET https://accounts.example/api/users/{id}",
        "GET https://accounts.example/api/users/{id}/settings",
    ]


def test_editing_the_injected_base_moves_every_request_it_reaches(
    joined: Workspace, settings, tmp_path: Path
) -> None:
    root = tmp_path / "repos"
    edit(root, "web", "src/main.ts", MAIN.replace("accounts.example/api", "elsewhere.example/api"))
    sync_all(joined, root)
    assert links(joined) == set()
    plan = ReferenceLinker(joined.knowledge).prepare_global()
    assert sorted(record.target_key for record in plan.http.injected) == [
        "GET https://elsewhere.example/api/users/{id}",
        "GET https://elsewhere.example/api/users/{id}/settings",
    ]
    assert_equals_fresh(joined, settings, root, tmp_path, "base")


CHAINS = {
    "package.json": '{"name": "chains"}\n',
    "src/root.ts": d("""
        import superagent from 'superagent';
        import { hop1 } from './hop1';
        import { ping } from './cycle';

        const ACCOUNTS_URL = 'https://accounts.example/api';

        function local({ fetcher, base }: O) {
          return fetcher.get(`${base}/local`);
        }

        function unstated({ fetcher, base }: O) {
          return fetcher.get(`${base}/unstated`);
        }

        local({ fetcher: superagent, base: ACCOUNTS_URL });
        hop1({ http: superagent, url: ACCOUNTS_URL });
        ping({ fetcher: superagent, base: ACCOUNTS_URL });
        unstated({ fetcher: superagent, base: process.env.UNSTATED });
    """),
    **{
        f"src/hop{n}.ts": d(f"""
            import {{ hop{n + 1} }} from './hop{n + 1}';

            export function hop{n}({{ fetcher, base }}: O) {{
              fetcher.get(`${{base}}/hop/{n}`);
              return hop{n + 1}({{ fetcher, base }});
            }}
        """)
        for n in (2, 3)
    },
    "src/hop1.ts": d("""
        import { hop2 } from './hop2';

        export function hop1({ http, url }: O) {
          http.get(`${url}/hop/1`);
          return hop2({ fetcher: http, base: url });
        }
    """),
    "src/hop4.ts": d("""
        export function hop4({ fetcher, base }: O) {
          return fetcher.get(`${base}/hop/4`);
        }
    """),
    "src/cycle.ts": d("""
        export function ping({ fetcher, base }: O) {
          fetcher.get(`${base}/ping`);
          return pong({ fetcher, base });
        }

        export function pong({ fetcher, base }: O) {
          fetcher.get(`${base}/pong`);
          return ping({ fetcher, base });
        }
    """),
}


def test_options_are_followed_through_three_functions_and_never_around_a_cycle(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, {"chains": CHAINS})
    plan = ReferenceLinker(workspace.knowledge).prepare_global()
    reached = sorted(record.target_key for record in plan.http.injected)
    # hop4 is a fourth function from its root; `unstated` is given a base no file states.
    assert MAX_HOPS == 3
    assert reached == [
        "GET https://accounts.example/api/hop/1",
        "GET https://accounts.example/api/hop/2",
        "GET https://accounts.example/api/hop/3",
        "GET https://accounts.example/api/local",
        "GET https://accounts.example/api/ping",
        "GET https://accounts.example/api/pong",
    ]
