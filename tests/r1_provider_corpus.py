"""Synthetic repositories for R1 (HTTP provider route visibility and diagnostics).

Every repository is invented, written out file by file, and indexed with the same helpers the
neighbouring HTTP tests use (``tests.impact_corpus.build``). They are the fixtures of
``R1_PREDICTIONS.md`` section 4: ``pyprov`` and its callers, ``flaskprov``, the NestJS
repositories, ``oasprov``, ``tsprov``, the Fastify bound repositories, and the Python bound
repositories. The Fastify ``neg`` repository and the chain/fan fixtures are the ones frozen in
``tests/test_http_fastify_tracing_global.py``, and the Python bounds are those of
``tests/test_http_python_routers.py`` and ``tests/test_http_python_values.py``; they are
imported, never copied, so the two cannot drift.

A *corpus* is a set of repositories indexed together in one workspace. The golden snapshot
(:mod:`tests.r1_golden`) is taken over every corpus in :data:`CORPUS_NAMES`.
"""

from __future__ import annotations

import json
import textwrap
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.deployment_targets import (
    DeclarationProvenance,
    DeploymentLinkCreateRequest,
    DeploymentLinkFields,
)
from repoatlas.models.http_connections import (
    HttpConnectionPreviewRequest,
    HttpConnectionPreviewResponse,
    HttpConnectionsRequest,
)
from repoatlas.models.http_inventory import (
    MAX_PAGE_SIZE,
    HttpOperation,
    HttpOperationsRequest,
    HttpOperationsResponse,
)
from repoatlas.services.workspace import Workspace, open_workspace
from tests.http_connections_corpus import PYPROJECT, repos
from tests.impact_corpus import build
from tests.incremental_corpus import sync_all
from tests.test_http_fastify_tracing_global import NEG, PACKAGE, _fan_visits
from tests.test_http_python_values import _ROUTES, _files, _hops

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)

#: The origins the caller repositories request; a group of the worklist is named by its origin.
ORDERS_ORIGIN = "https://orders.example.test"
NEST_ORIGIN = "https://nest.example.test"


def d(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def _package(name: str, package: str, dependency: str, files: dict[str, str]) -> dict[str, str]:
    """A Python repository: a manifest, the package's ``__init__`` and the files named."""
    return {
        "pyproject.toml": PYPROJECT.format(name=name, dep=dependency),
        f"{package}/__init__.py": "",
        **files,
    }


# -- repository `pyprov` --------------------------------------------------------------------

PY_MAIN = d("""
    from fastapi import APIRouter, FastAPI

    from .carts import router as carts
    from .missing import lost
    from .reports import router as reports

    app = FastAPI()
    local = APIRouter(prefix="/v1")
    OPTIONS = {"prefix": "/c"}


    @app.get("/health")
    def health():
        return {"status": "ok"}


    @local.get("/ping")
    def ping():
        return {}


    @lost.get("/lost")
    def lost_route():
        return {}


    app.include_router(local)
    app.include_router(carts, **OPTIONS)
    app.include_router(reports)
""")

PY_ORDERS = d("""
    from fastapi import APIRouter

    router = APIRouter()


    @router.get("/orders/{oid}")
    def get_order(oid: str):
        return {"id": oid}
""")

PY_CARTS = d("""
    from fastapi import APIRouter

    router = APIRouter()


    @router.get("/carts")
    def list_carts():
        return []
""")

PY_CONSTS = d("""
    def compute_prefix():
        return "/r"


    PREFIX = compute_prefix()
    AUDIT = "/audit"
""")

PY_REPORTS = d("""
    from fastapi import APIRouter

    from .consts import PREFIX

    router = APIRouter()


    @router.get(f"{PREFIX}/reports")
    def list_reports():
        return []
""")

PY_AUDIT = d("""
    from fastapi import APIRouter

    from .consts import AUDIT

    router = APIRouter()


    @router.get(AUDIT)
    def audit_log():
        return []
""")

PY_CUSTOM = d("""
    from fastapi import FastAPI


    class Meta(type):
        pass


    class Custom(FastAPI, metaclass=Meta):
        pass
""")

PY_APP2 = d("""
    from .custom import Custom

    app2 = Custom()


    @app2.get("/c2")
    def second():
        return {}
""")

PYPROV = _package(
    "pyprov",
    "svc",
    "fastapi",
    {
        "svc/main.py": PY_MAIN,
        "svc/orders.py": PY_ORDERS,
        "svc/carts.py": PY_CARTS,
        "svc/reports.py": PY_REPORTS,
        "svc/audit.py": PY_AUDIT,
        "svc/consts.py": PY_CONSTS,
        "svc/custom.py": PY_CUSTOM,
        "svc/app2.py": PY_APP2,
    },
)

#: The same ``svc/orders.py`` as ``pyprov``, and a main that mounts it: the misattribution guard.
PYPROV2 = _package(
    "pyprov2",
    "svc",
    "fastapi",
    {
        "svc/main.py": d("""
            from fastapi import FastAPI

            from .orders import router as orders

            app = FastAPI()
            app.include_router(orders)
        """),
        "svc/orders.py": PY_ORDERS,
    },
)

BUYER2 = _package(
    "buyer2",
    "buyer2",
    "requests",
    {
        "buyer2/client.py": d("""
            import requests


            def fetch_order() -> dict:
                return requests.get("https://orders.example.test/orders/42", timeout=5).json()


            def fetch_nothing() -> dict:
                return requests.get("https://orders.example.test/nothing/here", timeout=5).json()
        """)
    },
)

_TWIN_ROUTE = d("""
    from fastapi import APIRouter

    router = APIRouter()


    @router.get("/orders/{{{parameter}}}")
    def get_order({parameter}: str):
        return {{}}
""")

#: Two unmounted providers of one shape, and an application that serves only ``/status``.
TWINPROV = _package(
    "twinprov",
    "tw",
    "fastapi",
    {
        "tw/a.py": _TWIN_ROUTE.format(parameter="oid"),
        "tw/b.py": _TWIN_ROUTE.format(parameter="order_id"),
        "tw/main.py": d("""
            from fastapi import FastAPI

            app = FastAPI()


            @app.get("/status")
            def status():
                return {"ok": True}
        """),
    },
)

#: One module, no route: an indexed repository that is not a service.
EMPTYPROV = _package(
    "emptyprov", "emp", "fastapi", {"emp/util.py": "def helper():\n    return 1\n"}
)

# -- repository `flaskprov` -----------------------------------------------------------------

FLASKPROV = _package(
    "flaskprov",
    "fl",
    "flask",
    {
        "fl/views.py": d("""
            from flask import Blueprint

            bp = Blueprint("v", __name__)


            @bp.route("/v")
            def view():
                return {}
        """),
        "fl/factory.py": d("""
            from flask import Flask

            DEBUG = False


            def create_app():
                app = Flask(__name__)
                from .views import bp

                if DEBUG:
                    app.register_blueprint(bp)
                return app
        """),
    },
)

# -- the Python bounds (fixtures of test_http_python_routers / test_http_python_values) ------


def deep_chain_files() -> dict[str, str]:
    """``test_a_router_nested_deeper_than_the_bound_is_not_served``: routers r0 to r6, each
    mounting the next under ``/d<n>``; only the first five are within the mount-depth bound."""
    files = {
        "chain/src/chain-service.py": d("""
            from fastapi import FastAPI

            from r0 import r0

            app = FastAPI()
            app.include_router(r0)
        """)
    }
    for depth in range(7):
        below = depth + 1
        child = f"from r{below} import r{below}\n" if depth < 6 else ""
        include = f'r{depth}.include_router(r{below}, prefix="/d{below}")\n' if depth < 6 else ""
        files[f"chain/src/r{depth}.py"] = (
            "from fastapi import APIRouter\n"
            + child
            + f"\nr{depth} = APIRouter()\n"
            + include
            + f'\n\n@r{depth}.get("/leaf")\ndef leaf{depth}():\n    return {{}}\n'
        )
    return files


def cycle_files() -> dict[str, str]:
    """``test_a_cross_module_cycle_is_unresolved``: a path built from two constants that name
    each other."""
    files = _files(
        routes=_ROUTES.replace("src.consts import USERS", "src.a import A").replace("USERS", "A")
    )["svc"]
    files["src/a.py"] = 'from src.b import B\nA = f"{B}/a"\n'
    files["src/b.py"] = 'from src.a import A\nB = f"{A}/b"\n'
    return files


PYBOUNDS = {
    "chain": deep_chain_files(),
    "hops4": _hops(4)["svc"],
    "hops5": _hops(5)["svc"],
    "cycle": cycle_files(),
}

# -- the Fastify bounds ---------------------------------------------------------------------

#: The factory module of ``neg``, used as the application source of ``fcap``.
_FASTIFY_SERVER = NEG["src/server.ts"]


def traced_routes(prefix: str, count: int) -> str:
    """``count`` straight-line routes on ``const s = createServer()``: each is a traced route."""
    rows = ["import { createServer } from './server';", "", "const s = createServer();"]
    rows += [f"s.get('/{prefix}{index}', h);" for index in range(count)]
    return "\n".join(rows) + "\n"


def helper_calls(count: int) -> str:
    """A plugin ``init`` handed the module's own application ``count`` times, and a plugin
    ``orphan`` nothing hands anything."""
    rows = [
        "import Fastify from 'fastify';",
        "",
        "function init(app) {",
        "  app.get('/i', h);",
        "}",
        "",
        "function orphan(app) {",
        "  app.get('/o', h);",
        "}",
        "",
        "const s = Fastify();",
    ]
    rows += ["init(s);"] * count
    return "\n".join(rows) + "\n"


#: 257 and 255 traced routes in two files of one repository: the per-file cap is 256.
FCAP = {
    "package.json": PACKAGE,
    "src/server.ts": _FASTIFY_SERVER,
    "src/big.ts": traced_routes("b", 257),
    "src/small.ts": traced_routes("s", 255),
}
#: 256 helper calls in one file, and a plugin nothing calls; ``FHELP255`` is the control.
FHELP = {"package.json": PACKAGE, "src/app.ts": helper_calls(256)}
FHELP255 = {"package.json": PACKAGE, "src/app.ts": helper_calls(255)}
#: A ``fastify-plugin`` plugin given a prefix, and a plugin registered with options spread in.
FTBLK = {
    "package.json": PACKAGE,
    "src/app.ts": d("""
        import Fastify from 'fastify';
        import fp from 'fastify-plugin';

        function plugA(s) {
          s.get('/pa', h);
        }

        function plugB(s) {
          s.get('/pb', h);
        }

        const opts = {};
        const app = Fastify();
        app.register(fp(plugA), { prefix: '/a' });
        app.register(plugB, { ...opts });
    """),
}

# -- repository `tsprov` --------------------------------------------------------------------

TSPROV = {
    "package.json": json.dumps({"name": "tsprov", "private": True}),
    "src/server.ts": d("""
        import express from 'express';

        const app = express();

        export function up(req: unknown, res: { json(v: unknown): void }) {
          res.json({});
        }

        export function gone(req: unknown, res: { json(v: unknown): void }) {
          res.json({});
        }

        app.get('/up', up);

        const orphan = express.Router();
        orphan.get('/orphan', gone);
    """),
}

# -- the NestJS repositories ----------------------------------------------------------------

NEST_MAIN = d("""
    import { NestFactory } from "@nestjs/core";
    import { AppModule } from "./app.module";

    async function bootstrap() {
      const app = await NestFactory.create(AppModule);
      await app.listen(3000);
    }

    bootstrap();
""")


def _controller(cls: str, prefix: str, route: str, handler: str) -> str:
    argument = f'"{prefix}"' if prefix else ""
    return (
        'import { Controller, Get } from "@nestjs/common";\n\n'
        f"@Controller({argument})\n"
        f"export class {cls} {{\n"
        f'  @Get("{route}")\n'
        f"  {handler}() {{}}\n"
        "}\n"
    )


def _module(controllers: Sequence[tuple[str, str]], imports: str = "", head: str = "") -> str:
    """``(class, file stem)`` controllers registered by ``AppModule``."""
    rows = ['import { Module } from "@nestjs/common";', *head.splitlines()]
    rows += [f'import {{ {cls} }} from "./{stem}";' for cls, stem in controllers]
    names = ", ".join(cls for cls, _ in controllers)
    entries = f"imports: [{imports}], " if imports else ""
    rows += ["", f"@Module({{ {entries}controllers: [{names}] }})", "export class AppModule {}"]
    return "\n".join(rows) + "\n"


HEALTH = _controller("HealthController", "", "health", "check")
SHOP = _controller("ShopController", "shop", "items/:id", "item")

#: Registered, and served: ``/health`` states its own lines, ``/shop/items/:id`` is composed.
NESTOK = {
    "package.json": json.dumps({"name": "nestok"}),
    "src/main.ts": NEST_MAIN,
    "src/app.module.ts": _module(
        [("HealthController", "health.controller"), ("ShopController", "shop.controller")]
    ),
    "src/health.controller.ts": HEALTH,
    "src/shop.controller.ts": SHOP,
}
#: Serves only the composed route (F2).
NESTCOMPOSED = {
    "package.json": json.dumps({"name": "nestcomposed"}),
    "src/main.ts": NEST_MAIN,
    "src/app.module.ts": _module([("ShopController", "shop.controller")]),
    "src/shop.controller.ts": SHOP,
}
#: An application whose module imports ``RouterModule``: its routes are withheld.
NESTPROV = {
    "package.json": json.dumps({"name": "nestprov"}),
    "src/main.ts": NEST_MAIN,
    "src/app.module.ts": _module(
        [("ItemsController", "items.controller")],
        imports="RouterModule.register([])",
        head='import { RouterModule } from "@nestjs/core";',
    ),
    "src/items.controller.ts": _controller("ItemsController", "items", ":id", "one"),
}
#: A module and a controller, and no ``NestFactory.create`` anywhere.
NESTBARE = {
    "package.json": json.dumps({"name": "nestbare"}),
    "src/app.module.ts": _module([("BareController", "bare.controller")]),
    "src/bare.controller.ts": _controller("BareController", "bare", "list", "list"),
}
#: The caller of ``nestok``: one request a route with lines answers, one a composed route does.
BUYER3 = _package(
    "buyer3",
    "buyer3",
    "requests",
    {
        "buyer3/client.py": d("""
            import requests


            def fetch_health() -> dict:
                return requests.get("https://nest.example.test/health", timeout=5).json()


            def fetch_item() -> dict:
                return requests.get("https://nest.example.test/shop/items/42", timeout=5).json()
        """)
    },
)

# -- repository `oasprov` -------------------------------------------------------------------

OAS_SPEC = d("""
    openapi: 3.0.3
    info: {title: items, version: 1.0.0}
    paths:
      /items/{id}:
        get:
          operationId: getItem
          responses: {'200': {description: ok}}
""")

OASPROV = {
    "package.json": json.dumps({"name": "oasprov"}),
    "api/openapi.yaml": OAS_SPEC,
    # the specification is named relative to the working directory, not to __dirname
    "src/a.ts": d("""
        import * as oas3 from 'oas3-tools';

        const handlers = { getItem: (req, res) => res.end() };
        oas3.expressAppConfig('api/openapi.yaml', { routing: { controllers: handlers } });
    """),
    # the controllers come from a module that does not exist
    "src/b.ts": d("""
        import path from 'path';
        import * as oas3 from 'oas3-tools';
        import { missing } from './nowhere';

        oas3.expressAppConfig(path.join(__dirname, '../api/openapi.yaml'), {
          routing: { controllers: missing },
        });
    """),
}

# -- the corpora ----------------------------------------------------------------------------


def _nest_repos() -> dict[str, dict[str, str]]:
    return {
        "nestprov": NESTPROV,
        "nestbare": NESTBARE,
        "nestok": NESTOK,
        "nestcomposed": NESTCOMPOSED,
        "buyer3": BUYER3,
    }


CORPUS_REPOS: dict[str, Callable[[], dict[str, dict[str, str]]]] = {
    "connections": repos,
    "neg": lambda: {"neg": NEG},
    "fan": lambda: {"fan": _fan_visits(64, 64)},
    "py": lambda: {
        "pyprov": PYPROV,
        "buyer2": BUYER2,
        "twinprov": TWINPROV,
        "emptyprov": EMPTYPROV,
    },
    "py2": lambda: {"pyprov2": PYPROV2},
    "flask": lambda: {"flaskprov": FLASKPROV},
    "pybounds": lambda: PYBOUNDS,
    "fastify": lambda: {"fcap": FCAP, "fhelp": FHELP, "fhelp255": FHELP255, "ftblk": FTBLK},
    "ts": lambda: {"tsprov": TSPROV},
    "nest": _nest_repos,
    # `nest`, and a plain declaration of `nestcomposed` at the origin its caller requests (F2)
    "nest-declared": _nest_repos,
    "oas": lambda: {"oasprov": OASPROV},
}

CORPUS_NAMES = tuple(CORPUS_REPOS)


# -- helpers --------------------------------------------------------------------------------


def slug_of(workspace: Workspace, name: str) -> str:
    return next(r.slug for r in workspace.repositories.list_all() if r.name == name)


def repository_context(workspace: Workspace, name: str) -> CapabilityContext:
    return CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=slug_of(workspace, name))


def declare_plain(workspace: Workspace, name: str, url: str, *, group: str | None = None) -> int:
    """A declaration of ``url`` for the repository ``name``, applied at the next sync. Through the
    plain API (the default) nothing is checked against a preview; naming the worklist ``group``
    it came from makes creation re-check it, as the dialog's does."""
    return (
        CapabilityService(workspace)
        .deployment_link_create(
            DeploymentLinkCreateRequest(
                context=repository_context(workspace, name),
                link=DeploymentLinkFields(label="mapped", url=url, callers=[]),
                provenance=DeclarationProvenance(via="ui" if group else "api", request_group=group),
            )
        )
        .link.id
    )


def build_corpus(name: str, base: Path) -> Workspace:
    """Index the repositories of one corpus into a fresh workspace under ``base / name``."""
    directory = base / name
    settings = Settings.model_validate(
        {"storage": {"database": str(directory / "state" / "atlas.db")}}
    )
    workspace = open_workspace(settings=settings, require_root=False)
    try:
        build(workspace, directory, CORPUS_REPOS[name]())
        if name == "nest-declared":
            declare_plain(workspace, "nestcomposed", NEST_ORIGIN)
            sync_all(workspace, directory / "repos")
    except BaseException:
        workspace.close()
        raise
    return workspace


class Corpora:
    """The corpora a module reads, each built once on first use and closed together."""

    def __init__(self, base: Path) -> None:
        self._base = base
        self._open: dict[str, Workspace] = {}

    def workspace(self, name: str) -> Workspace:
        if name not in self._open:
            self._open[name] = build_corpus(name, self._base)
        return self._open[name]

    def close(self) -> None:
        for workspace in self._open.values():
            workspace.close()
        self._open.clear()


def inventory(
    workspace: Workspace, name: str, **fields: Any
) -> tuple[list[HttpOperation], HttpOperationsResponse]:
    """Every row of one repository's inventory (all pages), and the first page's response, whose
    ``basis``, ``summary`` and ``coverage`` describe the whole. ``fields`` are request fields:
    ``include_diagnostics=True``, ``status=...``, ``within_path=...``."""
    service = CapabilityService(workspace)
    rows: list[HttpOperation] = []
    first: HttpOperationsResponse | None = None
    cursor: str | None = None
    while True:
        request = HttpOperationsRequest(
            context=repository_context(workspace, name),
            limit=MAX_PAGE_SIZE,
            cursor=cursor,
            **fields,
        )
        response = service.http_operations(request)
        if first is None:
            first = response
        rows += response.operations
        cursor = response.next_cursor
        if cursor is None:
            return rows, first


def providers(rows: Sequence[HttpOperation]) -> list[HttpOperation]:
    return [row for row in rows if row.role == "provider"]


def group_key(workspace: Workspace, origin: str) -> str:
    response = CapabilityService(workspace).http_connections(HttpConnectionsRequest(context=GLOBAL))
    return next(g.key for g in response.groups if g.origin == origin)


def preview(
    workspace: Workspace, origin: str, name: str, **fields: Any
) -> HttpConnectionPreviewResponse:
    """The preview of declaring ``origin`` is served by the repository ``name``."""
    return CapabilityService(workspace).http_connection_preview(
        HttpConnectionPreviewRequest(
            context=GLOBAL,
            group=group_key(workspace, origin),
            repository=slug_of(workspace, name),
            **fields,
        )
    )


def caller_decisions(workspace: Workspace) -> dict[str, tuple[str, dict[str, object]]]:
    """(decision, evidence) of every stored request decision, by the calling function's name."""
    rows = workspace.database.execute(
        """SELECT e.qualified_name, d.decision, d.evidence_json FROM http_call_decisions d
           JOIN entities e ON e.id = d.src_id"""
    ).fetchall()
    return {name: (decision, json.loads(evidence)) for name, decision, evidence in rows}
