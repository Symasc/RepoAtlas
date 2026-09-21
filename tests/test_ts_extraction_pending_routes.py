"""Pending NestJS route declarations, as the index keeps them (HTTP linkage).

A controller route whose application mount is not proven must not reach a shared
HTTP node — and must not vanish either. It is kept as a claim that states what
the source declares (controller, handler, method, controller-local path, where it
is written) and what it still lacks. These tests pin that record through the real
sync pipeline: that it is written, replaced, and removed exactly as the source
changes, and that it is never confused with an ordinary unresolved reference.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from repoatlas.models.reports import SyncReport
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace
from tests.conftest import fact_snapshot, git, init_git_repository, reindex_from_scratch
from tests.http_corpus import corpus_settings, http_nodes, stored_http_edges

CONTROLLER = """\
import {{ Controller, Get, Post }} from "@nestjs/common";
import {{ MessagePattern }} from "@nestjs/microservices";

@Controller("v1/courses")
export class CoursesController {{
  @Get("{detail}")
  detail() {{}}

  @Post()
  create() {{}}

  @MessagePattern("courses.byId")
  byId() {{}}
}}
"""

CALLER = """\
import { helper } from "./missing";

export function useIt() {
  return helper();
}
"""

MISSING = "export function helper() {\n  return 1;\n}\n"

CONTROLLER_PATH = "src/courses.controller.ts"


def _setup(tmp_path: Path) -> tuple[Workspace, Path, SyncReport]:
    from repoatlas.services.init_service import initialize

    root = tmp_path / "repos"
    (root / "api" / "src").mkdir(parents=True)
    (root / "api" / CONTROLLER_PATH).write_text(CONTROLLER.format(detail=":courseId"))
    (root / "api" / "src" / "caller.ts").write_text(CALLER)
    init_git_repository(root / "api")
    workspace = open_workspace(settings=corpus_settings(tmp_path / "state"), require_root=False)
    report, _ = initialize(workspace, root, config_path=tmp_path / "config.yaml")
    return workspace, root, report


Index = Callable[..., tuple[Workspace, Path, SyncReport]]


@pytest.fixture
def index(tmp_path: Path) -> Iterator[Index]:
    """``_setup`` for one test; every workspace it opens is closed when the test ends."""
    opened: list[Workspace] = []

    def make(*args: Any, **kwargs: Any) -> tuple[Workspace, Path, SyncReport]:
        indexed = _setup(tmp_path, *args, **kwargs)
        opened.append(indexed[0])
        return indexed

    try:
        yield make
    finally:
        for workspace in opened:
            workspace.close()


def _commit(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", message)


def _sync(workspace: Workspace, root: Path) -> SyncReport:
    return sync(workspace, workspace.resolver().resolve(root))


def _claims(workspace: Workspace) -> list[dict[str, Any]]:
    rows = workspace.database.execute(
        "SELECT u.edge_type, u.target_kind, u.target_key, u.line_start, u.line_end, "
        "u.meta_json, src.key AS src_key, src.qualified_name AS source, f.rel_path "
        "FROM unresolved_refs u JOIN entities src ON src.id = u.src_id "
        "JOIN files f ON f.id = u.file_id ORDER BY f.rel_path, u.line_start, u.target_key"
    ).fetchall()
    return [{**dict(row), "meta": json.loads(row["meta_json"] or "{}")} for row in rows]


def _routes(workspace: Workspace) -> list[dict[str, Any]]:
    return [c for c in _claims(workspace) if c["meta"].get("pending_fact") == "nestjs-route"]


def _equals_full_reindex(workspace: Workspace, root: Path, tmp_path: Path, tag: str) -> None:
    fresh = reindex_from_scratch(corpus_settings(tmp_path / "unused"), root, tmp_path, tag)
    assert fact_snapshot(workspace) == fresh


def test_a_pending_route_is_recorded_with_everything_it_declares(index: Index) -> None:
    workspace, _, _ = index()
    detail, create = _routes(workspace)

    assert detail["edge_type"] == "SERVES_HTTP_ROUTE"
    assert detail["target_kind"] == "http_route"
    assert detail["target_key"] == "GET /v1/courses/:courseId"
    assert detail["rel_path"] == CONTROLLER_PATH
    assert (detail["line_start"], detail["line_end"]) == (6, 6)
    # The claim hangs off the handler itself, not the file.
    assert detail["source"] == "CoursesController.detail"
    assert str(detail["src_key"]).startswith(f"sym:api:{CONTROLLER_PATH}:CoursesController.detail:")
    assert detail["meta"] == {
        "pending_fact": "nestjs-route",
        "source": "nestjs",
        "provenance": "composed",
        "method": "GET",
        "controller": "CoursesController",
        "controller_path": "v1/courses",
        "method_path": ":courseId",
        "limitation": "application mount not proven",
        "parser": "tree-sitter-typescript",
    }
    assert create["target_key"] == "POST /v1/courses"
    assert create["source"] == "CoursesController.create"

    # Nothing reached a shared node: no route entity, no serving edge.
    assert http_nodes(workspace) == set()
    assert stored_http_edges(workspace) == set()


def test_a_message_handler_leaves_no_http_claim(index: Index) -> None:
    workspace, _, _ = index()
    sources = {c["source"] for c in _claims(workspace)}
    assert "CoursesController.byId" not in sources


def test_the_sync_report_counts_pending_routes_as_unresolved(index: Index) -> None:
    workspace, root, report = index()
    ordinary = [c for c in _claims(workspace) if "pending_fact" not in c["meta"]]
    # Both route declarations and every ordinary claim are unresolved references;
    # nothing else in these files is.
    assert report.unresolved_references == len(_routes(workspace)) + len(ordinary)
    assert _sync(workspace, root).unresolved_references == report.unresolved_references


def test_editing_a_route_replaces_its_claim(index: Index, tmp_path: Path) -> None:
    workspace, root, _ = index()
    (root / "api" / CONTROLLER_PATH).write_text(CONTROLLER.format(detail=":id/summary"))
    _commit(root / "api", "rename the parameter")
    _sync(workspace, root)

    assert [c["target_key"] for c in _routes(workspace)] == [
        "GET /v1/courses/:id/summary",
        "POST /v1/courses",
    ]
    assert http_nodes(workspace) == set()
    _equals_full_reindex(workspace, root, tmp_path, "edited")


def test_deleting_the_controller_removes_its_claims(index: Index, tmp_path: Path) -> None:
    workspace, root, _ = index()
    git(root / "api", "rm", "--quiet", CONTROLLER_PATH)
    _commit(root / "api", "remove the controller")
    _sync(workspace, root)

    assert _routes(workspace) == []
    assert all(c["rel_path"] != CONTROLLER_PATH for c in _claims(workspace))
    _equals_full_reindex(workspace, root, tmp_path, "deleted")


def test_a_pending_route_is_not_an_ordinary_unresolved_reference(index: Index) -> None:
    workspace, root, _ = index()
    [ordinary] = [
        c
        for c in _claims(workspace)
        if c["rel_path"] == "src/caller.ts" and c["edge_type"] == "CALLS"
    ]
    assert "pending_fact" not in ordinary["meta"]
    assert (ordinary["edge_type"], ordinary["target_kind"], ordinary["target_key"]) == (
        "CALLS",
        "symbol",
        "helper",
    )

    # The ordinary claim resolves the moment its target exists; the pending route
    # is untouched by that, and no symbol resolution ever turns it into an edge.
    (root / "api" / "src" / "missing.ts").write_text(MISSING)
    _commit(root / "api", "add the helper")
    _sync(workspace, root)

    calls = workspace.database.execute(
        "SELECT src.qualified_name AS src, dst.qualified_name AS dst FROM edges e "
        "JOIN entities src ON src.id = e.src_id JOIN entities dst ON dst.id = e.dst_id "
        "WHERE e.type = 'CALLS'"
    ).fetchall()
    assert ("useIt", "helper") in {(row["src"], row["dst"]) for row in calls}
    assert len(_routes(workspace)) == 2
    handler_edges = workspace.database.execute(
        "SELECT e.type FROM edges e JOIN entities src ON src.id = e.src_id "
        "WHERE src.qualified_name IN ('CoursesController.detail', 'CoursesController.create') "
        "AND e.type <> 'DEFINES'"
    ).fetchall()
    assert [row["type"] for row in handler_edges] == []
    assert stored_http_edges(workspace) == set()


def test_an_existing_references_own_role_is_never_read_as_a_pending_fact(
    index: Index,
) -> None:
    """Parsers already tag references with a ``role`` (a base class, a re-export).

    Those are ordinary references and must still resolve as they always have.
    """
    workspace, root, _ = index()
    (root / "api" / "src" / "base.ts").write_text("export class Base {}\n")
    (root / "api" / "src" / "child.ts").write_text(
        'import { Base } from "./base";\nexport class Child extends Base {}\n'
    )
    _commit(root / "api", "add a class hierarchy")
    _sync(workspace, root)

    references = workspace.database.execute(
        "SELECT src.qualified_name AS src, dst.qualified_name AS dst FROM edges e "
        "JOIN entities src ON src.id = e.src_id JOIN entities dst ON dst.id = e.dst_id "
        "WHERE e.type = 'REFERENCES'"
    ).fetchall()
    assert ("Child", "Base") in {(row["src"], row["dst"]) for row in references}
