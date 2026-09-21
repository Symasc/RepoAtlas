"""A typed client, a route and a NestJS handler, across two repositories (HTTP linkage).

The chain an agent wants is

    client method ─CALLS_HTTP_ROUTE→ route ←SERVES_HTTP_ROUTE─ backend handler

and each edge is published only when its own evidence holds. They meet on the
shared route node and nowhere else: a route both sides name is not proof that one
repository calls the other, so no repository-to-repository edge appears, and an
injected base URL names no deployment. Whatever is not proven — a route no one is
known to serve, a controller whose mount is not proven, a frontend call through
an application's service container — stays unresolved.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.services.workspace import Workspace, open_workspace
from tests.conftest import init_git_repository
from tests.http_corpus import (
    GLOBAL,
    corpus_settings,
    edge_set,
    http_nodes,
    relationships,
    stored_http_edges,
)

HTTP = """\
export function createRequester({ baseUrl, fetch: suppliedFetch }: ClientOptions) {
  const requestFetch = suppliedFetch ?? globalThis.fetch;
  const normalizedBaseUrl = baseUrl.replace(/\\/$/, "");

  async function send(path: string, init?: RequestInit) {
    const response = await requestFetch(`${normalizedBaseUrl}${path}`, { ...init });
    return response.json();
  }

  return {
    request: async (schema: Schema, path: string, init?: RequestInit) =>
      schema.parse(await send(path, init)),
  };
}

export function jsonBody(value: unknown, method: "POST" | "PUT" = "POST") {
  return { method, body: JSON.stringify(value) };
}
"""

CLIENT = """\
import { createRequester, jsonBody } from "./http";

export function createLearningClient(options: ClientOptions) {
  const { request } = createRequester(options);

  return {
    createCourse: (input: CreateCourseRequest) =>
      request(courseSchema, "/v1/courses", { ...jsonBody(input) }),
    getPreferences: () => request(preferencesSchema, "/v1/me/preferences"),
    health: () => request(healthSchema, "/health"),
  };
}
"""

FRONTEND = """\
import { useServices } from "./services";

export function useCreateCourse() {
  const services = useServices();
  return (input: CreateCourseRequest) => services.clients.learning.createCourse(input);
}
"""

MAIN = """\
import { NestFactory } from "@nestjs/core";
import { AppModule } from "./app.module";

async function bootstrap() {
  const app = await NestFactory.create(AppModule);
  await app.listen(3000);
}
"""

APP_MODULE = """\
import { Module } from "@nestjs/common";
import { CoursesController } from "./courses.controller";
import { HealthController } from "./health.controller";

@Module({ controllers: [CoursesController, HealthController] })
export class AppModule {}
"""

COURSES = """\
import { Controller, Post } from "@nestjs/common";

@Controller("v1")
export class CoursesController {
  @Post("courses")
  create() {}
}
"""

HEALTH = """\
import { Controller, Get } from "@nestjs/common";

@Controller()
export class HealthController {
  @Get("health")
  check() {}
}
"""

PREFERENCES = """\
import { Controller, Get } from "@nestjs/common";

// Declared, but no proven application registers it.
@Controller("v1/me")
export class PreferencesController {
  @Get("preferences")
  preferences() {}
}
"""

WEB = "sym:web-client:src/learning-client.ts:createLearningClient"
API = "sym:learning-api:src"


def _write(root: Path, repository: str, files: dict[str, str]) -> None:
    for name, text in files.items():
        (root / repository / name).parent.mkdir(parents=True, exist_ok=True)
        (root / repository / name).write_text(text)
    init_git_repository(root / repository)


def _index(tmp_path: Path) -> Workspace:
    from repoatlas.services.init_service import initialize

    root = tmp_path / "repos"
    _write(
        root,
        "web-client",
        {
            "package.json": json.dumps({"name": "web-client"}),
            "src/http.ts": HTTP,
            "src/learning-client.ts": CLIENT,
            "src/frontend.ts": FRONTEND,
        },
    )
    _write(
        root,
        "learning-api",
        {
            "package.json": json.dumps({"name": "learning-api"}),
            "src/main.ts": MAIN,
            "src/app.module.ts": APP_MODULE,
            "src/courses.controller.ts": COURSES,
            "src/health.controller.ts": HEALTH,
            "src/preferences.controller.ts": PREFERENCES,
        },
    )
    _write(
        root,
        "status-api",
        {
            "package.json": json.dumps({"name": "status-api"}),
            "src/main.ts": MAIN,
            "src/app.module.ts": APP_MODULE.replace(
                'import { CoursesController } from "./courses.controller";\n', ""
            ).replace("CoursesController, ", ""),
            "src/health.controller.ts": HEALTH,
        },
    )
    workspace = open_workspace(settings=corpus_settings(tmp_path / "state"), require_root=False)
    initialize(workspace, root, config_path=tmp_path / "config.yaml")
    return workspace


Index = Callable[..., Workspace]


@pytest.fixture
def index(tmp_path: Path) -> Iterator[Index]:
    """``_index`` for one test; every workspace it opens is closed when the test ends."""
    opened: list[Workspace] = []

    def make(*args: Any, **kwargs: Any) -> Workspace:
        indexed = _index(tmp_path, *args, **kwargs)
        opened.append(indexed)
        return indexed

    try:
        yield make
    finally:
        for workspace in opened:
            workspace.close()


def _repository(slug: str) -> CapabilityContext:
    return CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=slug)


def test_a_client_method_and_a_handler_meet_on_one_route_node(index: Index) -> None:
    workspace = index()
    route = "http-route:POST:/v1/courses"
    assert edge_set(relationships(workspace, route)) == {
        ("web-client/src/learning-client.ts::createCourse", "CALLS_HTTP_ROUTE", route),
        ("learning-api/src/courses.controller.ts::create", "SERVES_HTTP_ROUTE", route),
    }
    # Each edge is owned by the repository whose source states it.
    rows = workspace.database.execute(
        "SELECT r.slug, f.rel_path, e.type FROM edges e JOIN entities dst ON dst.id = e.dst_id "
        "JOIN repositories r ON r.id = e.repo_id JOIN files f ON f.id = e.file_id "
        "WHERE dst.key = ?",
        (route,),
    ).fetchall()
    assert {(row["slug"], row["rel_path"], row["type"]) for row in rows} == {
        ("web-client", "src/learning-client.ts", "CALLS_HTTP_ROUTE"),
        ("learning-api", "src/courses.controller.ts", "SERVES_HTTP_ROUTE"),
    }


def test_meeting_on_a_route_is_not_a_connection_between_repositories(index: Index) -> None:
    workspace = index()
    # Nothing joins the repositories themselves, and the injected base names no
    # deployment or endpoint for either side to meet on.
    rows = workspace.database.execute(
        "SELECT COUNT(*) AS n FROM edges e JOIN entities src ON src.id = e.src_id "
        "JOIN entities dst ON dst.id = e.dst_id "
        "WHERE src.type = 'repository' AND dst.type = 'repository'"
    ).fetchone()
    assert rows["n"] == 0
    assert all(key.startswith("http-route:") for key in http_nodes(workspace))


def test_a_shared_route_node_names_every_provider_and_proves_no_caller_pairing(
    index: Index,
) -> None:
    workspace = index()
    route = "http-route:GET:/health"
    # Two services serve GET /health; the client is not thereby calling either one.
    assert edge_set(relationships(workspace, route)) == {
        ("web-client/src/learning-client.ts::health", "CALLS_HTTP_ROUTE", route),
        ("learning-api/src/health.controller.ts::check", "SERVES_HTTP_ROUTE", route),
        ("status-api/src/health.controller.ts::check", "SERVES_HTTP_ROUTE", route),
    }


def test_a_route_no_proven_application_serves_keeps_no_provider(index: Index) -> None:
    workspace = index()
    route = "http-route:GET:/v1/me/preferences"
    # The client's request is proven; the controller that declares the route is not
    # mounted by any proven application, so it does not meet the client there.
    assert edge_set(relationships(workspace, route)) == {
        ("web-client/src/learning-client.ts::getPreferences", "CALLS_HTTP_ROUTE", route),
    }
    declared = workspace.database.execute(
        "SELECT u.target_key FROM unresolved_refs u JOIN files f ON f.id = u.file_id "
        "WHERE f.rel_path = 'src/preferences.controller.ts' AND u.edge_type = 'SERVES_HTTP_ROUTE'"
    ).fetchall()
    assert [row["target_key"] for row in declared] == ["GET /v1/me/preferences"]


def test_a_frontend_call_through_a_service_container_stays_unresolved(index: Index) -> None:
    workspace = index()
    # `services.clients.learning.createCourse` reaches the client method only through
    # the application's service container, which static reading does not follow.
    calls = workspace.database.execute(
        "SELECT dst.qualified_name AS dst FROM edges e "
        "JOIN entities src ON src.id = e.src_id JOIN entities dst ON dst.id = e.dst_id "
        "WHERE e.type = 'CALLS' AND src.key LIKE 'sym:web-client:src/frontend.ts:%'"
    ).fetchall()
    assert "createLearningClient.createCourse" not in {row["dst"] for row in calls}
    claim = workspace.database.execute(
        "SELECT target_key FROM unresolved_refs u JOIN files f ON f.id = u.file_id "
        "WHERE f.rel_path = 'src/frontend.ts' AND u.edge_type = 'CALLS' "
        "AND target_key = 'services.clients.learning.createCourse'"
    ).fetchall()
    # The call is kept as a claim through the file's own import, and stays one.
    assert len(claim) == 1


def test_repository_scope_confines_a_hub_to_the_named_repository(index: Index) -> None:
    workspace = index()
    route = "http-route:POST:/v1/courses"
    scoped = edge_set(relationships(workspace, route, context=_repository("web-client")))
    assert scoped == {
        ("web-client/src/learning-client.ts::createCourse", "CALLS_HTTP_ROUTE", route),
    }
    assert len(edge_set(relationships(workspace, route, context=GLOBAL))) == 2


def test_every_published_http_edge_is_one_this_branch_can_account_for(index: Index) -> None:
    workspace = index()
    assert stored_http_edges(workspace) == {
        (f"{WEB}.createCourse:7", "CALLS_HTTP_ROUTE", "http-route:POST:/v1/courses"),
        (f"{WEB}.getPreferences:9", "CALLS_HTTP_ROUTE", "http-route:GET:/v1/me/preferences"),
        (f"{WEB}.health:10", "CALLS_HTTP_ROUTE", "http-route:GET:/health"),
        (
            f"{API}/courses.controller.ts:CoursesController.create:6",
            "SERVES_HTTP_ROUTE",
            "http-route:POST:/v1/courses",
        ),
        (
            f"{API}/health.controller.ts:HealthController.check:6",
            "SERVES_HTTP_ROUTE",
            "http-route:GET:/health",
        ),
        (
            "sym:status-api:src/health.controller.ts:HealthController.check:6",
            "SERVES_HTTP_ROUTE",
            "http-route:GET:/health",
        ),
    }
