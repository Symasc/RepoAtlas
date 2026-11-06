"""Synthetic repositories for F1 (NestJS routes state a declaration line) and F2 (one definition
of "served routes" per operation).

``nestapi`` serves routes composed from a controller prefix and a global prefix, so its stored
edges state no line; ``webclient`` requests one of them. ``dupapi`` has two handlers for one route.
``docprov`` holds an OpenAPI document and nothing else. ``pingNN`` repositories serve ``/v1/ping``
through a router mounted with a prefix in its own file (stored without lines), and ``zservice``
serves the same route with a line: together they make a shortlist longer than its cap.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

from repoatlas.config.settings import Settings
from repoatlas.services.workspace import Workspace, open_workspace
from tests.http_connections_corpus import PYPROJECT
from tests.impact_corpus import build
from tests.r1_provider_corpus import OAS_SPEC

API_ORIGIN = "https://users.example.test"
DOCS_ORIGIN = "https://docs.example.test"
PING_ORIGIN = "https://ping.example.test"

#: How many repositories serve ``/v1/ping`` without a line: one more than the shortlist cap
#: (``MAX_CANDIDATES`` is 12) once ``zservice`` is counted with them.
DECOYS = 13


def d(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


NEST_MAIN = d("""
    import { NestFactory } from "@nestjs/core";
    import { AppModule } from "./app.module";

    async function bootstrap() {
      const app = await NestFactory.create(AppModule);
      app.setGlobalPrefix("api");
      await app.listen(3000);
    }

    bootstrap();
""")

NEST_MODULE = d("""
    import { Module } from "@nestjs/common";
    import { UsersController } from "./users.controller";

    @Module({ controllers: [UsersController] })
    export class AppModule {}
""")

#: ``@Get()`` is on line 5 and ``@Post()`` on line 8.
USERS_CONTROLLER = d("""
    import { Controller, Get, Post } from "@nestjs/common";

    @Controller("v1/users")
    export class UsersController {
      @Get()
      list() {}

      @Post()
      create() {}
    }
""")

#: A second handler for the route ``list`` serves (M13).
USERS_CONTROLLER_TWICE = USERS_CONTROLLER.replace(
    "  @Post()\n  create() {}\n", "  @Get()\n  listAgain() {}\n\n  @Post()\n  create() {}\n"
)

GET_LINE = 5
POST_LINE = 8

NESTAPI = {
    "package.json": json.dumps({"name": "nestapi"}),
    "src/main.ts": NEST_MAIN,
    "src/app.module.ts": NEST_MODULE,
    "src/users.controller.ts": USERS_CONTROLLER,
}

DUPAPI = {**NESTAPI, "src/users.controller.ts": USERS_CONTROLLER_TWICE}


def python_repo(name: str, dependency: str, files: dict[str, str]) -> dict[str, str]:
    return {
        "pyproject.toml": PYPROJECT.format(name=name, dep=dependency),
        "svc/__init__.py": "",
        **files,
    }


WEBCLIENT = python_repo(
    "webclient",
    "requests",
    {
        "svc/client.py": d(f"""
            import requests


            def fetch_users() -> dict:
                return requests.get("{API_ORIGIN}/api/v1/users", timeout=5).json()
        """)
    },
)

DOCPROV = {"package.json": json.dumps({"name": "docprov"}), "api/openapi.yaml": OAS_SPEC}

DOCCLIENT = python_repo(
    "docclient",
    "requests",
    {
        "svc/client.py": d(f"""
            import requests


            def fetch_item() -> dict:
                return requests.get("{DOCS_ORIGIN}/items/1", timeout=5).json()
        """)
    },
)

#: ``/v1/ping`` through a router mounted with a prefix in its own file: stored with no lines.
PING_PREFIXED = d("""
    from fastapi import APIRouter, FastAPI

    app = FastAPI()
    local = APIRouter(prefix="/v1")


    @local.get("/ping")
    def ping():
        return {}


    app.include_router(local)
""")

#: The same route stated directly: stored with its line (6).
PING_DIRECT = d("""
    from fastapi import FastAPI

    app = FastAPI()


    @app.get("/v1/ping")
    def ping():
        return {}
""")

PINGCLIENT = python_repo(
    "pingclient",
    "requests",
    {
        "svc/client.py": d(f"""
            import requests


            def fetch_ping() -> dict:
                return requests.get("{PING_ORIGIN}/v1/ping", timeout=5).json()
        """)
    },
)


def decoys() -> dict[str, dict[str, str]]:
    """``DECOYS`` repositories that sort before ``zservice``, serving ``/v1/ping`` with no line."""
    return {
        f"adecoy{index:02d}": python_repo(
            f"adecoy{index:02d}", "fastapi", {"svc/main.py": PING_PREFIXED}
        )
        for index in range(DECOYS)
    }


def ping_repos() -> dict[str, dict[str, str]]:
    return {
        **decoys(),
        "zservice": python_repo("zservice", "fastapi", {"svc/main.py": PING_DIRECT}),
        "pingclient": PINGCLIENT,
    }


def open_built(base: Path, name: str, repos: dict[str, dict[str, str]]) -> Workspace:
    """Index ``repos`` into a fresh workspace under ``base / name``; the caller closes it."""
    directory = base / name
    settings = Settings.model_validate(
        {"storage": {"database": str(directory / "state" / "atlas.db")}}
    )
    workspace = open_workspace(settings=settings, require_root=False)
    try:
        build(workspace, directory, repos)
    except BaseException:
        workspace.close()
        raise
    return workspace
