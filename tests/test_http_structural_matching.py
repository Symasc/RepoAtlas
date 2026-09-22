"""Client path templates, matched against the routes their own repository serves.

A call written ``request(`/v1/courses/${courseId}/roster`)`` names a route *shape*,
not a route key. These tests run the real sync pipeline and read the stored graph
to pin what that shape may and may not become:

* an edge, with no lines, to **every** route of the call's method its own
  repository serves with that shape — per-file routes and the routes the same
  pass proves — and to nothing in any other repository;
* nothing at all when the method cannot be read, when the call is a direct
  ``fetch``, or when the template cannot be read as a shape.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from repoatlas.graph.http_claims import (
    AMBIGUOUS,
    MATCHED,
    NO_INDEXED_HANDLER,
    OLDER_PARSER,
    REASON_DIRECT_FETCH,
    REASON_METHOD_UNREAD,
    REASON_STRUCTURAL,
    UNRESOLVED,
    CallDecision,
)
from repoatlas.graph.linker import ReferenceLinker
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace
from tests.conftest import fact_snapshot, git, init_git_repository, reindex_from_scratch
from tests.http_corpus import corpus_settings

CLIENT = """\
export function createCourseClient({ baseUrl, fetch: suppliedFetch }: Options) {
  const requestFetch = suppliedFetch ?? globalThis.fetch;

  async function request(path: string, init?: RequestInit) {
    const response = await requestFetch(`${baseUrl}${path}`, { ...init });
    return response.json();
  }

  return {
    roster: (courseId: string, params?: string) =>
      request(`/v1/courses/${courseId}/roster${params ? `?${params}` : ""}`),
    enrol: (courseId: string) =>
      request(`/v1/courses/${courseId}/roster`, { method: "PUT" }),
    unread: (courseId: string, options: RequestInit) =>
      request(`/v1/courses/${courseId}/roster`, options),
    guardian: (courseId: string) => request(`/v1/courses/${courseId}/guardian`),
    me: () => request("/v1/me"),
  };
}

export function direct(courseId: string) {
  return fetch(`/v1/courses/${courseId}/roster`);
}
"""

SERVER = """\
import express from "express";
const app = express();
app.get("/v1/courses/:courseId/roster", (req, res) => res.json([]));
app.post("/v1/courses/:courseId/roster", (req, res) => res.json([]));
app.get("/v1/me", (req, res) => res.json({}));
"""

SPEC = """\
openapi: 3.0.3
info:
  title: Courses
  version: "1"
paths:
  /v1/courses/{courseId}/roster:
    get:
      responses:
        "200":
          description: ok
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
import { RosterController } from "./roster.controller";

@Module({ controllers: [RosterController] })
export class AppModule {}
"""

ROSTER = """\
import { Controller, Get } from "@nestjs/common";

@Controller("v1/courses")
export class RosterController {
  @Get(":courseId/roster")
  page() {}
}
"""

CLIENT_KEY = "createCourseClient"


def _index(tmp_path: Path, repositories: dict[str, dict[str, str]]) -> tuple[Workspace, Path]:
    from repoatlas.services.init_service import initialize

    root = tmp_path / "repos"
    for repository, files in repositories.items():
        for name, text in files.items():
            (root / repository / name).parent.mkdir(parents=True, exist_ok=True)
            (root / repository / name).write_text(text)
        init_git_repository(root / repository)
    workspace = open_workspace(settings=corpus_settings(tmp_path / "state"), require_root=False)
    initialize(workspace, root, config_path=tmp_path / "config.yaml")
    return workspace, root


Index = Callable[..., tuple[Workspace, Path]]


@pytest.fixture
def index(tmp_path: Path) -> Iterator[Index]:
    opened: list[Workspace] = []

    def make(*args: Any, **kwargs: Any) -> tuple[Workspace, Path]:
        indexed = _index(tmp_path, *args, **kwargs)
        opened.append(indexed[0])
        return indexed

    try:
        yield make
    finally:
        for workspace in opened:
            workspace.close()


def _calls(workspace: Workspace) -> dict[str, set[str]]:
    rows = workspace.database.execute(
        "SELECT src.qualified_name AS caller, dst.key AS route FROM edges e "
        "JOIN entities src ON src.id = e.src_id JOIN entities dst ON dst.id = e.dst_id "
        "WHERE e.type IN ('CALLS_HTTP_ROUTE', 'CALLS_HTTP_ENDPOINT')"
    ).fetchall()
    found: dict[str, set[str]] = {}
    for row in rows:
        found.setdefault(row["caller"], set()).add(row["route"])
    return found


def _decisions(workspace: Workspace) -> dict[str, tuple[CallDecision, ...]]:
    plan = ReferenceLinker(workspace.knowledge).prepare_global().http
    rows = workspace.database.execute(
        "SELECT u.id, src.qualified_name AS caller FROM unresolved_refs u "
        "JOIN entities src ON src.id = u.src_id"
    ).fetchall()
    callers = {row["id"]: row["caller"] for row in rows}
    return {callers[claim]: decisions for claim, decisions in plan.calls.items()}


def test_a_template_links_to_the_served_route_of_its_method_and_shape(index: Index) -> None:
    workspace, _ = index({"courses": {"src/client.ts": CLIENT, "src/server.ts": SERVER}})
    calls = _calls(workspace)
    assert calls[f"{CLIENT_KEY}.roster"] == {"http-route:GET:/v1/courses/:courseId/roster"}
    # PUT is not served under that shape: no edge, and the other methods are listed.
    assert f"{CLIENT_KEY}.enrol" not in calls
    [enrol] = _decisions(workspace)[f"{CLIENT_KEY}.enrol"]
    assert (enrol.status, enrol.method, enrol.route) == (
        NO_INDEXED_HANDLER,
        "PUT",
        "/v1/courses/${courseId}/roster",
    )
    assert enrol.candidates == (
        "GET /v1/courses/:courseId/roster",
        "POST /v1/courses/:courseId/roster",
    )
    # The literal call is decided in its own file, exactly as before.
    assert calls[f"{CLIENT_KEY}.me"] == {"http-route:GET:/v1/me"}


def test_a_structural_edge_has_no_lines_and_belongs_to_the_global_pass(index: Index) -> None:
    workspace, _ = index({"courses": {"src/client.ts": CLIENT, "src/server.ts": SERVER}})
    row = workspace.database.execute(
        "SELECT e.line_start, e.line_end, e.parser FROM edges e "
        "JOIN entities src ON src.id = e.src_id WHERE src.qualified_name = ? "
        "AND e.type = 'CALLS_HTTP_ROUTE'",
        (f"{CLIENT_KEY}.roster",),
    ).fetchone()
    assert (row["line_start"], row["line_end"], row["parser"]) == (None, None, "linker:global")
    [roster] = _decisions(workspace)[f"{CLIENT_KEY}.roster"]
    assert (roster.status, roster.reason, roster.composed) == (MATCHED, REASON_STRUCTURAL, True)


def test_every_spelling_of_the_shape_is_linked_when_a_spec_sits_beside_its_handler(
    index: Index,
) -> None:
    """``{courseId}`` and ``:courseId`` are one shape: neither is chosen over the other."""
    workspace, _ = index(
        {"courses": {"src/client.ts": CLIENT, "src/server.ts": SERVER, "openapi.yaml": SPEC}}
    )
    assert _calls(workspace)[f"{CLIENT_KEY}.roster"] == {
        "http-route:GET:/v1/courses/:courseId/roster",
        "http-route:GET:/v1/courses/{courseId}/roster",
    }


def test_a_template_never_links_to_another_repositorys_routes(index: Index) -> None:
    """Same-repository matching only; an exact route key still meets across them."""
    workspace, _ = index({"web": {"src/client.ts": CLIENT}, "api": {"src/server.ts": SERVER}})
    calls = _calls(workspace)
    assert f"{CLIENT_KEY}.roster" not in calls
    [roster] = _decisions(workspace)[f"{CLIENT_KEY}.roster"]
    assert (roster.status, roster.candidates) == (NO_INDEXED_HANDLER, ())
    # The literal call meets the other repository's route on the shared node, as always.
    assert calls[f"{CLIENT_KEY}.me"] == {"http-route:GET:/v1/me"}
    served = workspace.database.execute(
        "SELECT COUNT(*) FROM edges e JOIN entities dst ON dst.id = e.dst_id "
        "WHERE e.type = 'SERVES_HTTP_ROUTE' AND dst.key = 'http-route:GET:/v1/me'"
    ).fetchone()[0]
    assert served == 1


def test_a_method_that_cannot_be_read_never_links(index: Index) -> None:
    workspace, _ = index({"courses": {"src/client.ts": CLIENT, "src/server.ts": SERVER}})
    assert f"{CLIENT_KEY}.unread" not in _calls(workspace)
    [unread] = _decisions(workspace)[f"{CLIENT_KEY}.unread"]
    assert (unread.status, unread.reason, unread.method) == (AMBIGUOUS, REASON_METHOD_UNREAD, None)
    assert unread.candidates == (
        "GET /v1/courses/:courseId/roster",
        "POST /v1/courses/:courseId/roster",
    )


def test_a_direct_fetch_template_is_visible_and_never_linked(index: Index) -> None:
    workspace, _ = index({"courses": {"src/client.ts": CLIENT, "src/server.ts": SERVER}})
    assert "direct" not in _calls(workspace)
    [direct] = _decisions(workspace)["direct"]
    assert (direct.status, direct.reason, direct.route) == (
        UNRESOLVED,
        REASON_DIRECT_FETCH,
        "/v1/courses/${courseId}/roster",
    )


def test_a_call_with_no_served_shape_has_no_indexed_handler(index: Index) -> None:
    workspace, _ = index({"courses": {"src/client.ts": CLIENT, "src/server.ts": SERVER}})
    [guardian] = _decisions(workspace)[f"{CLIENT_KEY}.guardian"]
    assert (guardian.status, guardian.matched, guardian.candidates) == (
        NO_INDEXED_HANDLER,
        (),
        (),
    )


def test_a_route_the_same_pass_proves_is_served_for_matching(index: Index) -> None:
    workspace, _ = index(
        {
            "courses": {
                "src/client.ts": CLIENT,
                "src/main.ts": MAIN,
                "src/app.module.ts": APP_MODULE,
                "src/roster.controller.ts": ROSTER,
            }
        }
    )
    assert _calls(workspace)[f"{CLIENT_KEY}.roster"] == {
        "http-route:GET:/v1/courses/:courseId/roster"
    }


def test_a_route_that_stops_being_proven_stops_being_matched(index: Index, tmp_path: Path) -> None:
    """The pass matches against what it proves now, never what it proved last time."""
    workspace, root = index(
        {
            "courses": {
                "src/client.ts": CLIENT,
                "src/main.ts": MAIN,
                "src/app.module.ts": APP_MODULE,
                "src/roster.controller.ts": ROSTER,
            }
        }
    )
    assert f"{CLIENT_KEY}.roster" in _calls(workspace)
    (root / "courses" / "src" / "app.module.ts").write_text(
        APP_MODULE.replace("controllers: [RosterController]", "controllers: []")
    )
    git(root / "courses", "commit", "--quiet", "-am", "unregister the controller")
    sync(workspace, workspace.resolver().resolve(root))
    assert f"{CLIENT_KEY}.roster" not in _calls(workspace)
    fresh = reindex_from_scratch(corpus_settings(tmp_path / "unused"), root, tmp_path, "fresh")
    assert fact_snapshot(workspace) == fresh


BASED = """\
const BASE = "https://api.example.com/v2";

function request(path: string) {
  return fetch(`${BASE}${path}`);
}

export function user(id: string) {
  return request(`/users/${id}`);
}

export function health() {
  return request("/health");
}
"""

BASED_SERVER = """\
import express from "express";
const app = express();
app.get("/v2/users/:id", (req, res) => res.json({}));
"""


def test_a_literal_base_is_prepended_and_a_shape_names_no_endpoint(index: Index) -> None:
    workspace, _ = index({"svc": {"src/client.ts": BASED, "src/server.ts": BASED_SERVER}})
    calls = _calls(workspace)
    assert calls["user"] == {"http-route:GET:/v2/users/:id"}
    # A literal path under the literal base names its endpoint, exactly as before.
    assert calls["health"] == {
        "http-route:GET:/v2/health",
        "http-endpoint:GET:https://api.example.com/v2/health",
    }


def test_removing_the_server_removes_the_link_on_the_next_sync(
    index: Index, tmp_path: Path
) -> None:
    workspace, root = index({"courses": {"src/client.ts": CLIENT, "src/server.ts": SERVER}})
    assert f"{CLIENT_KEY}.roster" in _calls(workspace)
    git(root / "courses", "rm", "--quiet", "src/server.ts")
    git(root / "courses", "commit", "--quiet", "-m", "remove the server")
    sync(workspace, workspace.resolver().resolve(root))
    assert f"{CLIENT_KEY}.roster" not in _calls(workspace)
    fresh = reindex_from_scratch(corpus_settings(tmp_path / "unused"), root, tmp_path, "fresh")
    assert fact_snapshot(workspace) == fresh


HELPER = """\
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
"""

IMPORTED = """\
import { createRequester } from "./http";

export function createLearningClient(options: ClientOptions) {
  const { request } = createRequester(options);
  return {
    both: (id: string) => {
      request(courseSchema, `/v1/courses/${id}`, { headers: {} });
      return request(courseSchema, "/v1/courses/:courseId", { headers: {} });
    },
    template: (id: string) => request(courseSchema, `/v1/courses/${id}`, { headers: {} }),
  };
}
"""

IMPORTED_SERVER = """\
import express from "express";
const app = express();
app.get("/v1/courses/:courseId", (req, res) => res.json({}));
"""


def test_one_caller_reaching_a_route_exactly_and_by_shape_keeps_the_lines(
    index: Index,
) -> None:
    """The citable half must not depend on which call happened to be read first."""
    workspace, _ = index(
        {
            "web": {
                "src/http.ts": HELPER,
                "src/client.ts": IMPORTED,
                "src/server.ts": IMPORTED_SERVER,
            }
        }
    )
    rows = workspace.database.execute(
        "SELECT e.line_start FROM edges e JOIN entities src ON src.id = e.src_id "
        "JOIN entities dst ON dst.id = e.dst_id WHERE src.qualified_name = ? "
        "AND dst.key = 'http-route:GET:/v1/courses/:courseId'",
        ("createLearningClient.both",),
    ).fetchall()
    # The shape is read first; the literal call on line 8 still decides the lines.
    assert [row["line_start"] for row in rows] == [8]
    assert _calls(workspace)["createLearningClient.template"] == {
        "http-route:GET:/v1/courses/:courseId"
    }


CHOICE = """\
import { createRequester } from "./http";

export function createLearningClient(options: ClientOptions) {
  const { request } = createRequester(options);
  return {
    list: (courseId?: string) =>
      request(schema, courseId ? `/v1/announcements?courseId=${courseId}` : "/v1/announcements"),
  };
}
"""


def test_two_spellings_of_one_route_are_one_request_through_an_imported_helper(
    index: Index,
) -> None:
    """A query is not part of which route is named, so this is one call, not two.

    A file that can decide such a call for itself already reads it this way. The
    global pass decides the same call when the helper is imported, and the two
    must not disagree — the inventory counts what the pass decided, so a second
    decision here is a second operation an agent would go looking for.
    """
    workspace, _ = index({"web": {"src/http.ts": HELPER, "src/client.ts": CHOICE}})
    [decisions] = _decisions(workspace).values()
    assert [(d.method, d.route, d.status) for d in decisions] == [
        ("GET", "/v1/announcements", NO_INDEXED_HANDLER)
    ]


EITHER = """\
import { createRequester } from "./http";

export function createCourseClient(options: ClientOptions) {
  const { request } = createRequester(options);
  return {
    either: (courseId?: string) =>
      request(schema, courseId ? `/v1/courses/${courseId}/roster` : "/v1/unserved"),
  };
}
"""


def test_two_different_paths_in_one_choice_are_each_decided_on_their_own(
    index: Index,
) -> None:
    """The other half of collapsing: two routes must not become one.

    Folding the alternatives together would answer the whole call with whichever
    one happened to be read first — here, reporting a served roster and losing
    the request that nothing serves, which is the finding worth having.
    """
    workspace, _ = index(
        {
            "courses": {
                "src/http.ts": HELPER,
                "src/client.ts": EITHER,
                "src/server.ts": SERVER,
            }
        }
    )
    [decisions] = _decisions(workspace).values()
    assert [(d.method, d.route, d.status) for d in decisions] == [
        ("GET", "/v1/courses/${courseId}/roster", MATCHED),
        ("GET", "/v1/unserved", NO_INDEXED_HANDLER),
    ]


def test_a_claim_an_older_parser_recorded_is_unresolved_rather_than_an_error(
    index: Index,
) -> None:
    """A repository not yet re-synced keeps v7 claims; the pass must still run."""
    workspace, _ = index(
        {
            "web": {
                "src/http.ts": HELPER,
                "src/client.ts": IMPORTED,
                "src/server.ts": IMPORTED_SERVER,
            }
        }
    )
    rows = workspace.database.execute(
        "SELECT u.id, u.meta_json FROM unresolved_refs u JOIN entities src ON src.id = u.src_id "
        "WHERE src.qualified_name = 'createLearningClient.template'"
    ).fetchall()
    [row] = rows
    meta = json.loads(row["meta_json"])
    arguments = json.loads(meta["arguments"])
    arguments[1] = {"template": "`/v1/courses/${id}`"}
    meta["arguments"] = json.dumps(arguments)
    with workspace.database.transaction() as conn:
        conn.execute(
            "UPDATE unresolved_refs SET meta_json = ? WHERE id = ?", (json.dumps(meta), row["id"])
        )
    plan = ReferenceLinker(workspace.knowledge).prepare_global().http
    assert plan.requests[row["id"]] == OLDER_PARSER
    [decision] = plan.calls[row["id"]]
    assert decision.status == UNRESOLVED
