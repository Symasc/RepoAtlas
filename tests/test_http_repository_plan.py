"""One repository's HTTP decisions, replanned alone, are its share of the global pass.

The inventory answers from :func:`~repoatlas.graph.linker.plan_repository_http`
rather than from a second store of outcomes. That is only sound if every rule the
global pass applies to HTTP claims is repository-local, so the two must agree
exactly — here across two repositories that deliberately reuse class names,
module paths and route shapes, which is where a rule that leaked across the
boundary would show.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.graph.http_claims import HttpClaimPlan
from repoatlas.graph.linker import GLOBAL_PARSER, ReferenceLinker, plan_repository_http
from repoatlas.services.workspace import Workspace, open_workspace
from tests.conftest import init_git_repository
from tests.http_corpus import corpus_settings

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
import { RosterController } from "./roster.controller";

@Module({ controllers: [RosterController] })
export class AppModule {}
"""

ROSTER = """\
import { Controller, Get, Post } from "@nestjs/common";

@Controller("v1/courses")
export class RosterController {
  @Get(":courseId/roster")
  page() {}
  @Post(":courseId/roster")
  add() {}
}
"""

UNREGISTERED = """\
import { Controller, Get } from "@nestjs/common";

@Controller("v1/admin")
export class AdminController {
  @Get("audit")
  audit() {}
}
"""

HTTP = """\
export function createRequester({ baseUrl, fetch: suppliedFetch }: ClientOptions) {
  const requestFetch = suppliedFetch ?? globalThis.fetch;
  async function send(path: string, init?: RequestInit) {
    const response = await requestFetch(`${baseUrl}${path}`, { ...init });
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

export function createCourseClient(options: ClientOptions) {
  const { request } = createRequester(options);
  return {
    roster: (id: string) => request(s, `/v1/courses/${id}/roster`, { headers: {} }),
    add: (id: string, input: unknown) =>
      request(s, `/v1/courses/${id}/roster`, { ...jsonBody(input) }),
    audit: () => request(s, "/v1/admin/audit", { headers: {} }),
    missing: (id: string) => request(s, `/v1/courses/${id}/guardian`, { headers: {} }),
  };
}

export function local(courseId: string) {
  return fetch(`/v1/courses/${courseId}/roster`);
}
"""

SERVER = """\
import express from "express";
const app = express();
app.get("/v1/courses/:courseId/guardian", (req, res) => res.json({}));
"""


def _repository(express: bool) -> dict[str, str]:
    files = {
        "package.json": json.dumps({"name": "svc"}),
        "src/main.ts": MAIN,
        "src/app.module.ts": APP_MODULE,
        "src/roster.controller.ts": ROSTER,
        "src/admin.controller.ts": UNREGISTERED,
        "src/http.ts": HTTP,
        "src/client.ts": CLIENT,
    }
    if express:
        files["src/server.ts"] = SERVER
    return files


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Workspace]:
    from repoatlas.services.init_service import initialize

    root = tmp_path / "repos"
    # Same class names, same module paths, same route shapes; one serves one more route.
    for name, express in (("alpha", True), ("beta", False)):
        for path, text in _repository(express).items():
            (root / name / path).parent.mkdir(parents=True, exist_ok=True)
            (root / name / path).write_text(text)
        init_git_repository(root / name)
    opened = open_workspace(settings=corpus_settings(tmp_path / "state"), require_root=False)
    initialize(opened, root, config_path=tmp_path / "config.yaml")
    try:
        yield opened
    finally:
        opened.close()


Row = tuple[object, ...]


def _http(plan: HttpClaimPlan) -> list[Row]:
    return sorted(
        (e.src_id, str(e.type), e.target.key, e.repo_id, e.file_id, e.line_start, e.line_end)
        for e in plan.http
    )


def _edges(plan: HttpClaimPlan) -> list[Row]:
    return sorted((e.src_id, e.dst_id, str(e.type), e.line_start, e.line_end) for e in plan.edges)


DECISIONS = ("routes", "roots", "requests", "calls", "promoted", "declared")


def test_the_repository_plans_together_are_exactly_the_global_plan(workspace: Workspace) -> None:
    whole = ReferenceLinker(workspace.knowledge).prepare_global().http
    ids = [repo.id for repo in workspace.repositories.list_all() if repo.id is not None]
    assert len(ids) == 2
    plans = [plan_repository_http(workspace.knowledge, repo_id).plan for repo_id in ids]
    assert sorted(row for plan in plans for row in _http(plan)) == _http(whole)
    assert sorted(row for plan in plans for row in _edges(plan)) == _edges(whole)
    for name in DECISIONS:
        merged: dict[int, object] = {}
        for plan in plans:
            merged.update(getattr(plan, name))
        assert merged == getattr(whole, name), name
    assert (sum(p.resolved for p in plans), sum(p.unresolved for p in plans)) == (
        whole.resolved,
        whole.unresolved,
    )


def test_each_repository_plan_decides_only_its_own_claims(workspace: Workspace) -> None:
    by_slug = {
        repo.slug: repo.id for repo in workspace.repositories.list_all() if repo.id is not None
    }
    alpha = plan_repository_http(workspace.knowledge, by_slug["alpha"]).plan
    beta = plan_repository_http(workspace.knowledge, by_slug["beta"]).plan
    assert alpha.calls and beta.calls
    assert not set(alpha.calls) & set(beta.calls)
    assert {e.repo_id for e in alpha.http} == {by_slug["alpha"]}
    assert {e.repo_id for e in beta.http} == {by_slug["beta"]}


def test_the_stored_global_edges_are_the_plan_that_produced_them(workspace: Workspace) -> None:
    """The runtime consistency check compares exactly this, per repository."""
    for repo in workspace.repositories.list_all():
        assert repo.id is not None
        planned = {
            (e.src_id, str(e.type), e.target.key, e.line_start, e.line_end)
            for e in plan_repository_http(workspace.knowledge, repo.id).plan.http
        }
        stored = {
            (row.src_id, str(row.type), row.target_key, row.line_start, row.line_end)
            for row in workspace.knowledge.list_http_edges(repo.id)
            if row.parser == GLOBAL_PARSER
        }
        assert planned == stored
