"""The static HTTP inventory of one repository: rows, scope, paging and what it withholds.

The capability exists because an agent could not get a complete picture of a
codebase's HTTP surface without rebuilding both halves of it by hand. These tests
pin the picture it does give, and — just as deliberately — the three things it
refuses to turn that picture into:

* a row is **route compatibility** and nothing else. It never says which host a
  client is configured to reach, which deployment answers, or who owns the data;
* a negative status is **absence in this repository's index**. ``no_indexed_handler``
  is not "the backend has no implementation", ``no_indexed_caller`` is not "nobody
  calls this", and every response carries the ``basis`` block that says so, plus
  the ``coverage`` limitations that say which constructs were not read at all;
* a filter narrows the **rows returned**, never what was known when they were
  decided. Planning happens over the whole repository first, so a path filter
  cannot make a call look unhandled by hiding the route that matches it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from repoatlas.capabilities.errors import (
    ContextResolutionError,
    InvalidCursorError,
    StaleCursorError,
)
from repoatlas.capabilities.http_inventory import _route_under, selected
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.evidence import EvidenceSource
from repoatlas.models.http_inventory import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    HttpOperation,
    HttpOperationsRequest,
    HttpOperationsResponse,
    OperationRole,
    OperationStatus,
)
from repoatlas.models.inspection import InspectRequest, InspectTarget
from repoatlas.models.investigation import EvidenceStatus
from repoatlas.parsers.typescript import TypeScriptParser
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace
from tests.conftest import git, init_git_repository
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
  @Get(":courseId/archive")
  archive() {}
  @Get(":courseId/export")
  download() {}
}
"""

#: Declared, but registered in no module: its mount is exactly what is unproven.
UNREGISTERED = """\
import { Controller, Get } from "@nestjs/common";

@Controller("v1/admin")
export class AdminController {
  @Get("audit")
  audit() {}
}
"""

CLIENT = """\
export function createCourseClient({ baseUrl, fetch: suppliedFetch }: Options) {
  const requestFetch = suppliedFetch ?? globalThis.fetch;

  async function request(path: string, init?: RequestInit) {
    const response = await requestFetch(`${baseUrl}${path}`, { ...init });
    return response.json();
  }

  return {
    roster: (courseId: string) => request(`/v1/courses/${courseId}/roster`),
    add: (courseId: string, input: unknown) =>
      request(`/v1/courses/${courseId}/roster`, {
        method: "POST",
        body: JSON.stringify(input),
      }),
    guardian: (courseId: string) => request(`/v1/courses/${courseId}/guardian`),
    audit: () => request("/v1/admin/audit"),
  };
}

export function createCourseClientAdmin({ baseUrl }: Options) {
  const requestFetch = globalThis.fetch;

  async function request(path: string, init?: RequestInit) {
    const response = await requestFetch(`${baseUrl}${path}`, { ...init });
    return response.json();
  }

  return {
    archive: (courseId: string) => request(`/v1/courses/${courseId}/archive`),
  };
}

export function localRoster(courseId: string) {
  return fetch(`/v1/courses/${courseId}/roster`);
}
"""

REPOSITORY = {
    "package.json": json.dumps({"name": "courses"}),
    "src/main.ts": MAIN,
    "src/app.module.ts": APP_MODULE,
    "src/roster.controller.ts": ROSTER,
    "src/admin.controller.ts": UNREGISTERED,
    "src/client.ts": CLIENT,
}

#: A second repository that serves the very shapes the first one is missing, so a
#: status that leaked across the boundary would turn into a visible false match.
OTHER = {
    "package.json": json.dumps({"name": "guardians"}),
    "src/main.ts": MAIN,
    "src/app.module.ts": APP_MODULE.replace("RosterController", "GuardianController").replace(
        "roster.controller", "guardian.controller"
    ),
    "src/guardian.controller.ts": """\
import { Controller, Get } from "@nestjs/common";

@Controller("v1/courses")
export class GuardianController {
  @Get(":courseId/guardian")
  guardian() {}
}
""",
}

COURSES = "courses"
GUARDIANS = "guardians"


def _only(workspace: Workspace) -> Any:
    found = [repo for repo in workspace.repositories.list_all() if repo.slug == COURSES]
    assert len(found) == 1
    return found[0]


def _repository(slug: str) -> CapabilityContext:
    return CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=slug)


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


@pytest.fixture
def indexed(index: Index) -> tuple[Workspace, Path]:
    return index({COURSES: REPOSITORY, GUARDIANS: OTHER})


def _ask(workspace: Workspace, **kwargs: object) -> HttpOperationsResponse:
    fields: dict[str, object] = {"context": _repository(COURSES)}
    fields.update(kwargs)
    return CapabilityService(workspace).http_operations(HttpOperationsRequest(**fields))  # type: ignore[arg-type]


def _keys(rows: Sequence[HttpOperation]) -> list[tuple[str, str, str, str, str]]:
    """Row identity including status, since one route may be requested twice."""
    return [(r.role, r.status.value, r.method or "", r.route, r.path or "") for r in rows]


def _rows(response: HttpOperationsResponse) -> list[tuple[str, str, str, str]]:
    return [
        (row.role, row.status.value, row.method or "", row.route) for row in response.operations
    ]


def _one(
    response: HttpOperationsResponse, method: str, route: str, *, role: str = "caller"
) -> HttpOperation:
    found = [
        row
        for row in response.operations
        if row.method == method and row.route == route and row.role == role
    ]
    assert len(found) == 1, f"expected exactly one {role} {method} {route}, got {_rows(response)}"
    return found[0]


def _walk(workspace: Workspace, size: int, **kwargs: object) -> list[tuple[str, str, str, str]]:
    seen: list[tuple[str, str, str, str]] = []
    cursor: str | None = None
    while True:
        response = _ask(workspace, limit=size, cursor=cursor, **kwargs)
        assert len(response.operations) <= size
        seen += _rows(response)
        cursor = response.next_cursor
        if cursor is None:
            assert response.has_more is False
            break
        assert response.has_more is True
    return seen


# -- what a status is, and what it is not -------------------------------------------------


def test_the_basis_block_states_the_reading_every_status_rests_on(
    indexed: tuple[Workspace, Path],
) -> None:
    """Said once per response, so no row can be read as more than it is."""
    workspace, _ = indexed
    basis = _ask(workspace).basis
    assert (basis.extraction, basis.scope, basis.repository) == ("static", "repository", COURSES)
    assert basis.negative_statuses == "indexed_absence_only"
    assert basis.establishes == ("route_compatibility",)
    # A deployment identity is a crossing's to state, never a status's.
    assert basis.does_not_establish == ("service_configuration", "data_ownership")


def test_a_call_whose_shape_is_served_here_is_matched_and_names_what_it_reaches(
    indexed: tuple[Workspace, Path],
) -> None:
    workspace, _ = indexed
    matched = _ask(workspace, role=OperationRole.CALLER, status=(OperationStatus.MATCHED,))
    row = _one(matched, "GET", "/v1/courses/${courseId}/roster")
    assert row.detail == "structural"
    assert row.routes == ("http-route:GET:/v1/courses/:courseId/roster",)
    assert row.path == "src/client.ts"
    assert [counterpart.entity for counterpart in row.counterparts] == [
        "sym:courses:src/roster.controller.ts:RosterController.page:6"
    ]


def test_a_shape_nothing_here_serves_is_absence_in_this_index_only(
    indexed: tuple[Workspace, Path],
) -> None:
    """The guardian handler exists — in the other repository. This one may not say so."""
    workspace, _ = indexed
    row = _one(_ask(workspace), "GET", "/v1/courses/${courseId}/guardian")
    assert row.status is OperationStatus.NO_INDEXED_HANDLER
    assert row.detail == "no_served_route"
    assert row.routes == ()
    assert row.counterparts == ()

    served = _ask(workspace, context=_repository(GUARDIANS), role=OperationRole.PROVIDER)
    assert ("provider", "no_indexed_caller", "GET", "/v1/courses/:courseId/guardian") in _rows(
        served
    )
    assert {code.code for code in served.coverage.limitations} >= {"same_repository_only"}


def test_a_route_declared_without_a_proven_mount_is_declared_and_hints_at_its_caller(
    indexed: tuple[Workspace, Path],
) -> None:
    """Two halves of one limitation: the provider says it, the caller carries it."""
    workspace, _ = indexed
    response = _ask(workspace)
    audit = [row for row in response.operations if row.route == "/v1/admin/audit"]
    declared = [row for row in audit if row.role == "provider"]
    calls = [row for row in audit if row.role == "caller"]
    assert len(declared) == 1 and len(calls) == 1
    assert declared[0].status is OperationStatus.DECLARED
    assert declared[0].detail, "a declared route says why its mount is not proven"
    assert calls[0].status is OperationStatus.NO_INDEXED_HANDLER
    assert [match.entity for match in calls[0].declared_matches] == [
        "sym:courses:src/admin.controller.ts:AdminController.audit:6"
    ]
    assert response.coverage.declared == 1
    assert any(item.code == "unproven_mount" for item in response.coverage.limitations)


def test_a_direct_fetch_template_is_visible_as_unresolved_and_counted(
    indexed: tuple[Workspace, Path],
) -> None:
    """Transport code forwards whole paths, so a rooted template through ``fetch``
    is recorded for coverage and never matched."""
    workspace, _ = indexed
    response = _ask(workspace, role=OperationRole.CALLER)
    unresolved = [row for row in response.operations if row.status is OperationStatus.UNRESOLVED]
    assert [row.detail for row in unresolved] == ["direct_fetch_template"]
    assert response.coverage.unresolved == {"direct_fetch_template": 1}
    assert response.coverage.unknown_paths == 1
    limitation = next(
        item for item in response.coverage.limitations if item.code == "direct_fetch_template"
    )
    assert limitation.count == 1


def test_a_served_route_no_indexed_caller_reaches_is_not_a_route_nobody_calls(
    indexed: tuple[Workspace, Path],
) -> None:
    workspace, _ = indexed
    response = _ask(workspace, role=OperationRole.PROVIDER)
    export = _one(response, "GET", "/v1/courses/:courseId/export", role="provider")
    assert export.status is OperationStatus.NO_INDEXED_CALLER
    assert export.counterpart_count == 0
    roster = _one(response, "GET", "/v1/courses/:courseId/roster", role="provider")
    assert roster.status is OperationStatus.CALLED
    # The direct fetch reaches this very shape and is deliberately not counted as
    # a caller. Asking only for providers does not hide it: coverage describes the
    # scope, so the caveat on `no_indexed_caller` survives a filter that would
    # otherwise have removed the only row that raises it.
    assert roster.counterpart_count == 1
    assert response.coverage.unresolved == {"direct_fetch_template": 1}


def test_constructs_that_are_not_extracted_are_declared_as_possible_here(
    indexed: tuple[Workspace, Path],
) -> None:
    """Counted limitations say how many; uncounted ones say only that they could apply."""
    workspace, _ = indexed
    coverage = _ask(workspace).coverage
    by_code = {item.code: item for item in coverage.limitations}
    assert {"member_call_client", "injected_fetch", "concatenated_path"} <= set(by_code)
    assert by_code["member_call_client"].count is None
    assert by_code["member_call_client"].note
    assert coverage.files[TypeScriptParser.name] >= 5
    assert "unsupported_framework" in by_code
    assert "stored_graph_differs" not in by_code


def test_a_reading_that_disagrees_with_the_stored_graph_is_answered_and_marked(
    indexed: tuple[Workspace, Path],
) -> None:
    """An index an older parser built still answers — it just may not pretend to agree."""
    workspace, _ = indexed
    with workspace.database.transaction() as connection:
        connection.execute("DELETE FROM edges WHERE parser = 'linker:global'")
    response = _ask(workspace)
    assert response.operations
    assert any(item.code == "stored_graph_differs" for item in response.coverage.limitations)


# -- scope: what is returned, and what was known ---------------------------------------


def test_a_global_scope_is_refused_because_no_status_would_speak_for_a_repository(
    indexed: tuple[Workspace, Path],
) -> None:
    workspace, _ = indexed
    with pytest.raises(ContextResolutionError) as raised:
        _ask(workspace, context=CapabilityContext(scope=CapabilityScope.GLOBAL))
    assert sorted(raised.value.candidates) == [COURSES, GUARDIANS]


def test_a_path_filter_narrows_the_rows_and_not_the_routes_they_were_decided_against(
    indexed: tuple[Workspace, Path],
) -> None:
    """The mutation this pins: pushing ``within_path`` into the loading queries.

    Every route that decides these rows lives in another file. If scope reached
    the plan, the matched call would come back unhandled — the most convincing
    possible wrong answer.
    """
    workspace, _ = indexed
    response = _ask(workspace, within_path="src/client.ts")
    assert {row.path for row in response.operations} == {"src/client.ts"}
    matched = [row for row in response.operations if row.status is OperationStatus.MATCHED]
    assert {(row.method, row.route) for row in matched} == {
        ("GET", "/v1/courses/${courseId}/roster"),
        ("POST", "/v1/courses/${courseId}/roster"),
        ("GET", "/v1/courses/${courseId}/archive"),
    }
    assert response.summary.by_role == {"caller": 6}


def test_a_repository_never_reports_another_repositorys_operations(
    indexed: tuple[Workspace, Path],
) -> None:
    workspace, _ = indexed
    guardians = _ask(workspace, context=_repository(GUARDIANS))
    assert guardians.basis.repository == GUARDIANS
    assert all("guardian" in row.route or row.role == "provider" for row in guardians.operations)
    assert not [row for row in guardians.operations if row.route.startswith("/v1/admin")]
    assert all(row.entity is None or ":guardians:" in row.entity for row in guardians.operations)


def test_owner_walks_from_a_factory_to_the_requests_its_methods_make(
    indexed: tuple[Workspace, Path],
) -> None:
    """The navigation the evaluation could not do: a factory carries no route edge."""
    workspace, _ = indexed
    owned = _ask(workspace, owner="createCourseClient")
    assert {row.route for row in owned.operations} == {
        "/v1/courses/${courseId}/roster",
        "/v1/courses/${courseId}/guardian",
        "/v1/admin/audit",
    }
    assert owned.summary.total == 4
    members = _ask(workspace, owner="createCourseClient.*")
    assert _rows(members) == _rows(owned)
    assert _ask(workspace, owner="createCourseClient.roster").summary.total == 1
    # A qualified name ends at a dot: the factory beside it is a different owner,
    # not a member of this one.
    sibling = _ask(workspace, owner="createCourseClientAdmin")
    assert {row.route for row in sibling.operations} == {"/v1/courses/${courseId}/archive"}


def test_the_method_and_route_prefix_filters_cut_on_whole_values(
    indexed: tuple[Workspace, Path],
) -> None:
    workspace, _ = indexed
    posts = _ask(workspace, method="POST")
    assert {row.method for row in posts.operations} == {"POST"}
    admin = _ask(workspace, route_prefix="/v1/admin")
    assert {row.route for row in admin.operations} == {"/v1/admin/audit"}
    # A prefix matches whole segments, so `/v1/cour` is not a prefix of `/v1/courses`.
    assert _ask(workspace, route_prefix="/v1/cour").summary.total == 0


def test_a_route_prefix_selects_a_written_template_and_a_declared_parameter_alike(
    indexed: tuple[Workspace, Path],
) -> None:
    """One prefix answers for both roles, because they name one route.

    A caller writes ``/v1/courses/${courseId}/roster`` and the handler declares
    ``/v1/courses/:courseId/roster``. Read byte for byte, a prefix spelled either
    way answers for one role and silently returns nothing for the other — so an
    agent that reads a route off a provider row and filters callers by it sees an
    empty page and concludes the request is not indexed.
    """
    workspace, _ = indexed
    both = _ask(workspace, route_prefix="/v1/courses/:courseId")
    assert {row.role for row in both.operations} == {"caller", "provider"}
    assert {(row.role, row.route) for row in both.operations} == {
        ("provider", "/v1/courses/:courseId/roster"),
        ("provider", "/v1/courses/:courseId/archive"),
        ("provider", "/v1/courses/:courseId/export"),
        ("caller", "/v1/courses/${courseId}/roster"),
        ("caller", "/v1/courses/${courseId}/guardian"),
        ("caller", "/v1/courses/${courseId}/archive"),
    }
    # The same rows, whichever way the caller spells the prefix.
    assert _rows(_ask(workspace, route_prefix="/v1/courses/${courseId}")) == _rows(both)


def test_a_parameter_segment_matches_however_it_is_spelled_and_whatever_it_is_named(
    indexed: tuple[Workspace, Path],
) -> None:
    """The four declared spellings and the written template are one segment.

    A parameter's *name* is the server's business and the client's separately, so
    two names for one position are still one position. What is compared is that
    both sides fill the segment with a value rather than a literal.
    """
    workspace, _ = indexed
    expected = _rows(_ask(workspace, route_prefix="/v1/courses/:courseId"))
    assert expected
    for spelling in (
        "/v1/courses/{courseId}",
        "/v1/courses/<courseId>",
        "/v1/courses/${courseId}",
        # A different name for the same position, in each spelling.
        "/v1/courses/:id",
        "/v1/courses/{anything}",
        "/v1/courses/${whateverTheClientCalledIt}",
    ):
        assert _rows(_ask(workspace, route_prefix=spelling)) == expected, spelling


def test_a_literal_segment_never_matches_a_parameter_in_either_direction(
    indexed: tuple[Workspace, Path],
) -> None:
    """``/v1/courses/42`` is a request about one course, not about the shape.

    This is the boundary that keeps the looser reading honest: if a literal could
    stand in for a parameter, every concrete path would select every route of its
    shape and the filter would stop meaning anything.
    """
    workspace, _ = indexed
    assert _ask(workspace, route_prefix="/v1/courses/42").summary.total == 0
    assert _ask(workspace, route_prefix="/v1/courses/42/roster").summary.total == 0
    # And the reverse: a parameter does not select a route with a literal there.
    assert {row.route for row in _ask(workspace, route_prefix="/v1/:section").operations} == set()


def test_a_route_prefix_compares_only_the_segments_it_names(
    indexed: tuple[Workspace, Path],
) -> None:
    """Prefix boundaries, on the shape reading as on the literal one."""
    workspace, _ = indexed
    whole = _ask(workspace)
    # Every rooted route, and nothing filtered out.
    assert _ask(workspace, route_prefix="/").summary.total == whole.summary.total
    # A prefix naming more segments than the route has selects nothing.
    assert _ask(workspace, route_prefix="/v1/courses/:courseId/roster/extra").summary.total == 0
    # A partial segment is not a segment, whichever side the parameter is on.
    assert _ask(workspace, route_prefix="/v1/cour/:courseId").summary.total == 0
    # Narrowing by a further segment really narrows.
    roster = _ask(workspace, route_prefix="/v1/courses/:courseId/roster")
    assert {row.route for row in roster.operations} == {
        "/v1/courses/:courseId/roster",
        "/v1/courses/${courseId}/roster",
    }


def test_narrowing_by_route_prefix_never_narrows_what_the_rows_were_decided_against(
    indexed: tuple[Workspace, Path],
) -> None:
    """The looser prefix selects rows; it does not re-decide them.

    A caller matched against the whole repository's routes before any filter ran,
    so its status and counterparts must read identically whether it arrived under
    a prefix or not.
    """
    workspace, _ = indexed
    prefix = "/v1/courses/:courseId"
    filtered = _ask(workspace, route_prefix=prefix).operations
    kept = [row for row in _ask(workspace).operations if _route_under(row.route, prefix)]
    # Selecting rows is all the filter does: the same rows, decided identically.
    assert _keys(filtered) == _keys(kept)
    for row, same in zip(filtered, kept, strict=True):
        assert (row.status, row.routes, row.counterparts) == (
            same.status,
            same.routes,
            same.counterparts,
        )
    # The guardian request is served only by the *other* repository, so it stays
    # absent here however the prefix that selected it was spelled.
    guardian = _one(_ask(workspace, route_prefix=prefix), "GET", "/v1/courses/${courseId}/guardian")
    assert guardian.status is OperationStatus.NO_INDEXED_HANDLER


def test_the_status_filter_selects_rows_and_the_summary_counts_the_whole_filtered_set(
    indexed: tuple[Workspace, Path],
) -> None:
    workspace, _ = indexed
    whole = _ask(workspace)
    missing = _ask(workspace, status=(OperationStatus.NO_INDEXED_HANDLER,))
    assert {row.status for row in missing.operations} == {OperationStatus.NO_INDEXED_HANDLER}
    assert missing.summary.total == whole.summary.by_status["no_indexed_handler"]
    assert missing.summary.total == len(missing.operations)


def test_a_path_that_nothing_indexed_stands_under_is_refused_rather_than_answered_empty(
    indexed: tuple[Workspace, Path],
) -> None:
    from repoatlas.capabilities.errors import FileNotIndexedError

    workspace, _ = indexed
    with pytest.raises(FileNotIndexedError):
        _ask(workspace, within_path="src/nowhere")


# -- paging --------------------------------------------------------------------------------


def test_every_page_size_yields_the_same_whole_inventory(
    indexed: tuple[Workspace, Path],
) -> None:
    workspace, _ = indexed
    whole = _walk(workspace, MAX_PAGE_SIZE)
    assert len(whole) > 7, "the corpus must need several pages for this to mean anything"
    for size in (1, 2, 3, 7):
        assert _walk(workspace, size) == whole
    assert _ask(workspace).limits.applied == DEFAULT_PAGE_SIZE
    over = _ask(workspace, limit=MAX_PAGE_SIZE + 1).limits
    assert (over.requested, over.applied, over.clamped) == (
        MAX_PAGE_SIZE + 1,
        MAX_PAGE_SIZE,
        ("limit",),
    )


def test_a_page_is_the_same_bytes_however_often_it_is_asked_for(
    indexed: tuple[Workspace, Path],
) -> None:
    workspace, _ = indexed
    first = _ask(workspace, limit=3)
    again = _ask(workspace, limit=3, cursor=None)
    assert first.operations == again.operations
    second = _ask(workspace, limit=3, cursor=first.next_cursor)
    assert _ask(workspace, limit=3, cursor=first.next_cursor).operations == second.operations


def test_a_cursor_from_a_different_question_is_refused_rather_than_reinterpreted(
    indexed: tuple[Workspace, Path],
) -> None:
    workspace, _ = indexed
    cursor = _ask(workspace, limit=2).next_cursor
    assert cursor is not None
    with pytest.raises(InvalidCursorError):
        _ask(workspace, limit=2, cursor=cursor, role=OperationRole.PROVIDER)
    with pytest.raises(InvalidCursorError):
        _ask(workspace, limit=2, cursor=cursor, within_path="src/client.ts")
    with pytest.raises(InvalidCursorError):
        _ask(workspace, limit=2, cursor=cursor, include_provenance=True)
    with pytest.raises(InvalidCursorError):
        _ask(workspace, limit=2, cursor="not-a-cursor")
    # Only the page size may change under a cursor.
    assert _ask(workspace, limit=5, cursor=cursor).operations


def test_a_cursor_is_stale_once_the_inventory_it_described_has_changed(
    indexed: tuple[Workspace, Path],
) -> None:
    workspace, root = indexed
    cursor = _ask(workspace, limit=2).next_cursor
    assert cursor is not None
    sync(workspace, workspace.resolver().resolve(root))
    assert _ask(workspace, limit=2, cursor=cursor).operations, "a no-op sync keeps a page valid"

    path = root / COURSES / "src" / "roster.controller.ts"
    path.write_text(path.read_text().replace(':courseId/export"', ':courseId/history"'))
    git(root / COURSES, "commit", "--quiet", "-am", "rename the route")
    sync(workspace, workspace.resolver().resolve(root))
    with pytest.raises(StaleCursorError):
        _ask(workspace, limit=2, cursor=cursor)


def test_a_change_outside_the_filtered_set_leaves_its_cursor_alone(
    indexed: tuple[Workspace, Path],
) -> None:
    workspace, root = indexed
    cursor = _ask(workspace, limit=1, within_path="src/client.ts").next_cursor
    assert cursor is not None
    other = root / GUARDIANS / "src" / "guardian.controller.ts"
    other.write_text(other.read_text().replace(":courseId/guardian", ":courseId/guardians"))
    git(root / GUARDIANS, "commit", "--quiet", "-am", "rename the route")
    sync(workspace, workspace.resolver().resolve(root))
    assert _ask(workspace, limit=1, within_path="src/client.ts", cursor=cursor).operations


def test_the_last_page_carries_no_continuation_of_its_own(
    indexed: tuple[Workspace, Path],
) -> None:
    """Pinned on the cut itself, not only on the response that reads it.

    The response refuses to publish a cursor unless more rows exist, so a cut that
    handed one back anyway would be invisible from outside. Two conditions holding
    a promise is defence in depth only while both of them hold it.
    """
    from repoatlas.capabilities.http_inventory import cut_page, page_limits_for

    workspace, _ = indexed
    service = CapabilityService(workspace)
    request = HttpOperationsRequest(context=_repository(COURSES), limit=200)
    with workspace.database.read_snapshot():
        drafts = selected(service._http_inventory(_only(workspace)).drafts, request, None)
    page = cut_page(drafts, None, identity="r", inventory="i", limits=page_limits_for(request))
    assert page.has_more is False
    assert page.after is None
    assert len(page.rows) == len(drafts)


# -- evidence ------------------------------------------------------------------------------


def test_evidence_is_allocated_only_when_it_is_asked_for(
    indexed: tuple[Workspace, Path],
) -> None:
    workspace, _ = indexed
    plain = _ask(workspace, limit=3)
    assert plain.evidence == ()
    assert all(row.evidence_id is None for row in plain.operations)
    assert plain.ledger.total == 0


def test_provenance_supplies_each_returned_rows_own_lines_and_binds_them_to_it(
    indexed: tuple[Workspace, Path],
) -> None:
    workspace, _ = indexed
    response = _ask(workspace, include_provenance=True, within_path="src/client.ts")
    assert response.evidence
    ids = {item.id for item in response.evidence}
    cited = [row for row in response.operations if row.evidence_id is not None]
    assert cited and {row.evidence_id for row in cited} <= ids
    by_id = {item.id: item for item in response.evidence}
    for row in cited:
        item = by_id[row.evidence_id or ""]
        assert item.ref.path == row.path
        assert (item.ref.line_start, item.ref.line_end) == (row.line_start, row.line_end)
        assert item.text.count("\n") == (row.line_end or 0) - (row.line_start or 0)
    assert response.ledger.total == len(response.evidence)


def test_one_span_that_states_two_rows_is_supplied_once(
    indexed: tuple[Workspace, Path],
) -> None:
    workspace, _ = indexed
    response = _ask(workspace, include_provenance=True, within_path="src/client.ts")
    spans = [(item.ref.path, item.ref.line_start, item.ref.line_end) for item in response.evidence]
    assert len(spans) == len(set(spans))


def test_supplied_evidence_carries_the_http_origin(
    indexed: tuple[Workspace, Path],
) -> None:
    """Its own origin, because a row asserts no edge — only where it is written."""
    workspace, _ = indexed
    service = CapabilityService(workspace)
    response = service.http_operations(
        HttpOperationsRequest(context=_repository(COURSES), include_provenance=True, limit=5)
    )
    session = service._sessions.get(response.investigation_id)
    assert session is not None
    origins = {origin for entry in session.ledger.entries() for origin in entry.origins}
    assert origins == {EvidenceSource.HTTP_OPERATION}


def test_evidence_of_a_line_that_has_moved_on_is_stale_rather_than_silently_reused(
    indexed: tuple[Workspace, Path],
) -> None:
    workspace, root = indexed
    service = CapabilityService(workspace)
    response = service.http_operations(
        HttpOperationsRequest(
            context=_repository(COURSES), include_provenance=True, within_path="src/client.ts"
        )
    )
    evidence_id = response.evidence[0].id
    path = root / COURSES / "src" / "client.ts"
    path.write_text("// a line nobody asked about\n" + path.read_text())
    git(root / COURSES, "commit", "--quiet", "-am", "shift every line")
    sync(workspace, workspace.resolver().resolve(root))

    inspected = service.inspect(
        InspectRequest(
            context=_repository(COURSES),
            target=InspectTarget(evidence_id=evidence_id),
            investigation_id=response.investigation_id,
        )
    )
    assert inspected.target_evidence_status is EvidenceStatus.STALE
