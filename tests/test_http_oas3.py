"""oas3-tools applications serve a specification by controllers (deployment HTTP roadmap, M2).

``expressAppConfig(path.join(__dirname, spec), { routing: { controllers } })`` serves
each operation of an OpenAPI 3 document, under each root server's base path, by the
controllers member its ``operationId`` names. The application, the objects of functions
its controllers are made of, and the document each record a claim; the global pass
serves an operation by the handler object's ``const`` when every step is proven.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from repoatlas.graph.linker import ReferenceLinker
from repoatlas.graph.ts_oas3 import (
    CONTROLLERS_UNREAD,
    CONTROLLERS_UNRESOLVED,
    NO_HANDLER,
    PROMOTED,
    SERVERS_UNREAD,
    SPEC_NOT_FOUND,
    SPEC_UNREAD,
    handler_name,
)
from repoatlas.models.parsing import ParsedReference
from repoatlas.parsers.http_facts import (
    OAS3_APP,
    OPENAPI_OPERATIONS,
    PENDING_FACT,
    TS_HANDLER_OBJECT,
)
from repoatlas.parsers.structured import StructuredParser
from repoatlas.parsers.typescript import TypeScriptParser
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build
from tests.incremental_corpus import assert_equals_fresh, edit, sync_all


def d(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def _claims(source: str, role: str, name: str = "app.ts") -> list[ParsedReference]:
    parsed = TypeScriptParser().parse(Path(name), d(source))
    return [r for r in parsed.references if r.meta.get(PENDING_FACT) == role]


def _apps(source: str) -> list[dict[str, object]]:
    return [
        {
            "spec": r.meta.get("spec"),
            "controllers": json.loads(r.meta["controllers"]) if "controllers" in r.meta else None,
            "lines": (r.line_start, r.line_end),
        }
        for r in _claims(source, OAS3_APP)
    ]


def _objects(source: str) -> dict[str, tuple[dict[str, int], list[str], str | None]]:
    return {
        r.target_key: (json.loads(r.meta["keys"]), json.loads(r.meta["exports"]), r.source_symbol)
        for r in _claims(source, TS_HANDLER_OBJECT)
    }


def _table(document: str) -> list[dict[str, object]]:
    parsed = StructuredParser().parse(Path("openapi.yaml"), d(document))
    return [
        {
            "operations": json.loads(r.meta["operations"]),
            "bases": json.loads(r.meta["bases"]),
        }
        for r in parsed.references
        if r.meta.get(PENDING_FACT) == OPENAPI_OPERATIONS
    ]


# -- the application -----------------------------------------------------------------------

APP = d("""
    import path from 'node:path';
    import * as oas3Tools from 'oas3-tools';
    import { siteHandlers } from './handlers/sites';
    import { statusHandlers as status } from './handlers/status';

    const admin = {
      Admin_removeDevices: async (req: Request, res: Response) => res.end(),
    };

    const controllers = { ...status, ...siteHandlers, ...admin };

    export const app = oas3Tools
      .expressAppConfig(path.join(__dirname, '..', 'api/openapi.yaml'), {
        routing: { controllers },
      })
      .getApp();
""")


def test_an_application_records_its_specification_and_its_controllers() -> None:
    assert _apps(APP) == [
        {
            "spec": "../api/openapi.yaml",
            "controllers": [
                {"module": "./handlers/status", "name": "statusHandlers"},
                {"module": "./handlers/sites", "name": "siteHandlers"},
                {"local": "admin"},
            ],
            "lines": (12, 15),
        }
    ]


def test_require_and_a_handler_object_as_the_controllers() -> None:
    source = """\
        const path = require('path');
        const oas3Tools = require('oas3-tools');

        const handlers = { getStatus: (req, res) => res.end() };
        const options = { routing: { controllers: handlers } };

        oas3Tools.expressAppConfig(path.resolve(__dirname, 'api/openapi.yaml'), options);
    """
    assert _apps(source) == [
        {"spec": "api/openapi.yaml", "controllers": [{"local": "handlers"}], "lines": (7, 7)}
    ]


HERE = "path.join(__dirname, 'a.yaml')"
HANDLERS = "{ routing: { controllers: handlers } }"


@pytest.mark.parametrize(
    ("spec_argument", "options", "spec", "controllers"),
    [
        # the library opens a bare path from the process's working directory
        ("'api/openapi.yaml'", HANDLERS, None, True),
        ("path.join(cwd, 'api.yaml')", HANDLERS, None, True),
        ("path.join(__dirname, '/abs.yaml')", HANDLERS, None, True),
        # a directory of controllers, read when the process starts
        (HERE, "{ routing: { controllers: path.join(__dirname, 'c') } }", "a.yaml", False),
        # a member that is not a spread of a handler object
        (HERE, "{ routing: { controllers: { ...handlers, x: () => 1 } } }", "a.yaml", False),
        (HERE, "{ routing: { controllers: { ...other } } }", "a.yaml", False),
        # options whose keys cannot be read
        (HERE, "{ ...defaults, routing: { controllers: handlers } }", "a.yaml", False),
    ],
)
def test_what_an_application_states_unreadably_is_left_unread(
    spec_argument: str, options: str, spec: str | None, controllers: bool
) -> None:
    source = f"""\
        import path from 'path';
        import {{ expressAppConfig }} from 'oas3-tools';

        const handlers = {{ getStatus: (req, res) => res.end() }};
        const other = {{ name: 'x' }};

        export const app = expressAppConfig({spec_argument}, {options});
    """
    [app] = _apps(source)
    assert app["spec"] == spec
    assert app["controllers"] == ([{"local": "handlers"}] if controllers else None)


@pytest.mark.parametrize(
    "source",
    [
        # not oas3-tools
        "import { expressAppConfig } from './local';\nexpressAppConfig(a, b);\n",
        # shadowed where it is called
        "import * as o from 'oas3-tools';\nfunction f(o) { return o.expressAppConfig(a, b); }\n",
        # the library itself would fail without options
        "import * as o from 'oas3-tools';\no.expressAppConfig(a);\n",
    ],
)
def test_a_call_that_is_not_an_application_records_nothing(source: str) -> None:
    assert _apps(source) == []


# -- handler objects -----------------------------------------------------------------------


def test_an_object_of_functions_is_a_handler_object_owned_by_its_const() -> None:
    source = """\
        export const sites = {
          listSites: async (req, res) => res.json([]),
          async getSite(req, res) { res.json({}); },
          'site-change': function (req, res) {},
        };
        const local = { ping: () => 'pong' };
        export { local as aliased };
    """
    assert _objects(source) == {
        "sites": ({"listSites": 2, "getSite": 3, "site-change": 4}, ["sites"], "sites"),
        "local": ({"ping": 6}, ["aliased"], "local"),
    }


@pytest.mark.parametrize(
    "declaration",
    [
        "const o = { name: 'x', f: () => 1 };",  # a member that is not a function
        "const o = { ...base, f: () => 1 };",  # a spread
        "const o = { f };",  # a shorthand: its value is not proven a function
        "const o = { get f() { return 1; } };",  # an accessor
        "const o = {};",
        "let o = { f: () => 1 };",
        "const o = { f: () => 1 };\nfunction g(o) {}",  # bound twice
    ],
)
def test_anything_else_is_no_handler_object(declaration: str) -> None:
    assert _objects(declaration + "\n") == {}


# -- the specification ---------------------------------------------------------------------

SPEC = d("""
    openapi: 3.0.3
    info: {title: devices, version: 1.0.0}
    servers:
      - url: http://devices.internal/v2/
    paths:
      /status:
        get:
          operationId: getStatus
          responses: {'200': {description: up}}
      /sites/{siteId}/devices:
        get:
          operationId: list-site-devices
          responses: {'200': {description: devices}}
        delete:
          operationId: removeDevices
          x-swagger-router-controller: Admin
          responses: {'204': {description: removed}}
      /sites/{siteId}/orphan:
        get:
          operationId: orphan
          responses: {'200': {description: nobody}}
      /sites/{siteId}/unnamed:
        get:
          responses: {'200': {description: no operationId}}
""")


def test_a_document_records_its_named_operations_and_its_base_paths() -> None:
    assert _table(SPEC) == [
        {
            "operations": [
                ["GET", "/status", "getStatus", None, 8],
                ["GET", "/sites/{siteId}/devices", "list-site-devices", None, 12],
                ["DELETE", "/sites/{siteId}/devices", "removeDevices", "Admin", 15],
                ["GET", "/sites/{siteId}/orphan", "orphan", None, 20],
            ],
            "bases": ["/v2"],
        }
    ]


@pytest.mark.parametrize(
    ("servers", "bases"),
    [
        ("", [""]),
        ("servers: []\n", [""]),
        (
            "servers:\n  - url: https://a.example\n  - url: /api/\n  - url: http://b/api\n",
            ["", "/api"],
        ),
        ("servers:\n  - url: relative\n", [""]),
        ("servers:\n  - url: https://{env}.example/v1\n    variables: {env: {default: x}}\n", None),
        ("servers:\n  - url: https://a.example/{version}\n", None),
        ("servers:\n  - url: https://a.example/v1?x=1\n", None),
    ],
)
def test_base_paths_are_read_as_the_validator_reads_them(
    servers: str, bases: list[str] | None
) -> None:
    document = (
        "openapi: 3.0.0\ninfo: {title: t, version: '1'}\n"
        + servers
        + "paths:\n  /x:\n    get:\n      operationId: x\n      responses: {}\n"
    )
    [table] = _table(document)
    assert table["bases"] == bases


def test_a_swagger_document_or_one_naming_no_operation_records_no_table() -> None:
    assert _table("swagger: '2.0'\npaths:\n  /x:\n    get:\n      operationId: x\n") == []
    assert _table("openapi: 3.0.0\npaths:\n  /x:\n    get:\n      responses: {}\n") == []


@pytest.mark.parametrize(
    ("operation_id", "controller", "name"),
    [
        ("getAllChangeBysiteId", None, "getAllChangeBysiteId"),
        ("list-site-DEVICES", None, "listSiteDevices"),
        ("removeDevices", "Admin", "Admin_removeDevices"),
        ("get-x", "", "getX"),
        ("café-x", None, None),
    ],
)
def test_the_handler_name_is_the_library_s(
    operation_id: str, controller: str | None, name: str | None
) -> None:
    assert handler_name(operation_id, controller) == name


# -- the global pass -----------------------------------------------------------------------

DEVICES = {
    "package.json": '{"name": "devices"}\n',
    "api/openapi.yaml": SPEC,
    "src/app.ts": APP,
    "src/handlers/sites.ts": d("""
        export const siteHandlers = {
          listSiteDevices: async (req: Request, res: Response) => res.json([]),
          getStatus: async (req: Request, res: Response) => res.json({ from: 'sites' }),
        };
    """),
    "src/handlers/status.ts": d("""
        export const statusHandlers = {
          async getStatus(req: Request, res: Response) {
            res.json({ ok: true });
          },
        };
    """),
}


def served(workspace: Workspace) -> set[tuple[str, str, str, int | None]]:
    """(file, handler, route, first line or None) for every route served from code."""
    rows = workspace.database.execute(
        """
        SELECT f.rel_path, s.qualified_name, d.name, e.line_start
        FROM edges e
        JOIN entities s ON s.id = e.src_id
        JOIN entities d ON d.id = e.dst_id
        JOIN files f ON f.id = s.file_id
        WHERE e.type = 'SERVES_HTTP_ROUTE' AND f.rel_path != 'api/openapi.yaml'
        """
    ).fetchall()
    return {tuple(row) for row in rows}


@pytest.fixture
def devices(workspace: Workspace, tmp_path: Path) -> Workspace:
    return build(workspace, tmp_path, {"devices": DEVICES})


def test_operations_are_served_by_the_handler_objects_their_names_reach(
    devices: Workspace,
) -> None:
    # `getStatus` is in both objects: the later spread wins, as in JavaScript. `orphan`
    # names no key, and the unnamed operation is never routed.
    assert served(devices) == {
        ("src/handlers/sites.ts", "siteHandlers", "GET /v2/status", None),
        ("src/handlers/sites.ts", "siteHandlers", "GET /v2/sites/{siteId}/devices", None),
        ("src/app.ts", "admin", "DELETE /v2/sites/{siteId}/devices", None),
    }
    spec_routes = devices.database.execute(
        """
        SELECT COUNT(*) FROM edges e JOIN files f ON f.id = e.file_id
        WHERE e.type = 'SERVES_HTTP_ROUTE' AND f.rel_path = 'api/openapi.yaml'
        """
    ).fetchone()[0]
    assert spec_routes == 5


def _outcome_app(spec: str, controllers: str, imports: str = "") -> str:
    return (
        "import path from 'path';\nimport * as oas3 from 'oas3-tools';\n"
        + imports
        + "const handlers = { getStatus: (req, res) => res.end() };\n"
        + f"oas3.expressAppConfig({spec}, {{ routing: {{ controllers: {controllers} }} }});\n"
    )


BROKEN = {
    "package.json": '{"name": "broken"}\n',
    "api/openapi.yaml": SPEC,
    "api/variable.yaml": SPEC.replace(
        "url: http://devices.internal/v2/",
        "url: http://devices.internal/{v}\n    variables: {v: {default: v2}}",
    ),
    "src/handlers.ts": "export const present = { getStatus: () => 1 };\n",
    "src/ok.ts": _outcome_app("path.join(__dirname, '../api/openapi.yaml')", "handlers"),
    "src/unread.ts": _outcome_app("'api/openapi.yaml'", "handlers"),
    "src/missing.ts": _outcome_app("path.join(__dirname, '../api/missing.yaml')", "handlers"),
    "src/outside.ts": _outcome_app("path.join(__dirname, '../../api/openapi.yaml')", "handlers"),
    "src/variable.ts": _outcome_app("path.join(__dirname, '../api/variable.yaml')", "handlers"),
    "src/directory.ts": _outcome_app(
        "path.join(__dirname, '../api/openapi.yaml')", "path.join(__dirname, 'c')"
    ),
    "src/unresolved.ts": _outcome_app(
        "path.join(__dirname, '../api/openapi.yaml')",
        "{ ...absent }",
        "import { absent } from './handlers';\n",
    ),
    "src/nohandler.ts": _outcome_app(
        "path.join(__dirname, '../api/openapi.yaml')",
        "{ ...unrelated }",
        "const unrelated = { somethingElse: () => 1 };\n",
    ),
}


def test_each_application_is_served_or_says_why_not(workspace: Workspace, tmp_path: Path) -> None:
    build(workspace, tmp_path, {"broken": BROKEN})
    plan = ReferenceLinker(workspace.knowledge).prepare_global().http
    rows = workspace.database.execute(
        """
        SELECT u.id, f.rel_path FROM unresolved_refs u JOIN files f ON f.id = u.file_id
        WHERE u.meta_json LIKE ?
        """,
        (f'%"{OAS3_APP}"%',),
    ).fetchall()
    assert {path: plan.routes[claim_id] for claim_id, path in rows} == {
        "src/ok.ts": PROMOTED,
        "src/unread.ts": SPEC_UNREAD,
        "src/missing.ts": SPEC_NOT_FOUND,
        "src/outside.ts": SPEC_NOT_FOUND,
        "src/variable.ts": SERVERS_UNREAD,
        "src/directory.ts": CONTROLLERS_UNREAD,
        "src/unresolved.ts": CONTROLLERS_UNRESOLVED,
        "src/nohandler.ts": NO_HANDLER,
    }


def test_editing_an_operation_id_moves_its_handler(
    devices: Workspace, settings, tmp_path: Path
) -> None:
    root = tmp_path / "repos"
    edit(root, "devices", "api/openapi.yaml", SPEC.replace("list-site-devices", "listDevices"))
    sync_all(devices, root)
    routes = {route for _, _, route, _ in served(devices)}
    assert "GET /v2/sites/{siteId}/devices" not in routes and "GET /v2/status" in routes
    assert_equals_fresh(devices, settings, root, tmp_path, "operation")
