"""What the HTTP surface does today, and must still do after R1 (R1_PREDICTIONS sections 4-5).

These pinned the behaviour at 2da633f. R1 adds an opt-in view of the routes the inventory
hides; it changes none of what is asserted here, except where F1 and F2 changed it:

* F1: a NestJS route built from a controller prefix now states its decorator line, so a preview
  joins it; a Python router mounted with a prefix in its own file still states no line;
* F2: a declaration's state reads every route the code serves, the global pass's included, so a
  repository serving only promoted routes is applied and joins its caller;
* what the default inventory lists, and hides, for the R1 corpus;
* the unmatched-caller cases 12-15: decisions, previews and the refusal to declare.

The new behaviour is in ``test_r1_provider_diagnostics.py`` and ``test_r1_preview_diagnostics.py``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.capabilities.errors import InvalidDeploymentLinkError
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import DECLARATIONS_SLUG
from repoatlas.graph.http_claims import served_by_servers
from repoatlas.graph.linker import GLOBAL_PARSER, plan_repository_http, repository_routes
from repoatlas.graph.py_routes import MODULE_UNRESOLVED, NOT_MOUNTED, UNREADABLE_MOUNT
from repoatlas.graph.ts_routes import NOT_REGISTERED, UNREADABLE_REGISTRATION
from repoatlas.models.http_connections import HttpConnectionPreviewRequest, HttpConnectionsRequest
from repoatlas.models.http_inventory import HttpOperation, OperationStatus
from repoatlas.models.parsing import EdgeType
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build
from tests.r1_provider_corpus import (
    BUYER2,
    CORPUS_NAMES,
    GLOBAL,
    NEST_ORIGIN,
    ORDERS_ORIGIN,
    PYPROV,
    Corpora,
    caller_decisions,
    declare_plain,
    group_key,
    inventory,
    preview,
    providers,
    slug_of,
)

PUBLISHED = {OperationStatus.CALLED, OperationStatus.NO_INDEXED_CALLER}
NOTHING_JOINED = (
    "no request of this group matches a route this service serves: nothing would be joined"
)
Rows = dict[tuple[str | None, str], list[HttpOperation]]


@pytest.fixture(scope="module")
def corpora(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Corpora]:
    built = Corpora(tmp_path_factory.mktemp("r1-characterization"))
    yield built
    built.close()


def rows_of(corpora: Corpora, corpus: str, name: str) -> Rows:
    """The provider rows of the default inventory, by (method, route)."""
    rows, _ = inventory(corpora.workspace(corpus), name)
    found: Rows = {}
    for row in providers(rows):
        found.setdefault((row.method, row.route), []).append(row)
    return found


# -- F1: a route built from a prefix states no line ------------------------------------------


def test_f1_a_request_a_composed_nest_route_answers_now_joins_beside_a_plain_one(
    corpora: Corpora,
) -> None:
    """``nestok`` serves ``/health`` (its own lines) and ``/shop/items/:id`` (a controller prefix:
    composed). The composed route states its decorator line (5), so both requests of ``buyer3``
    would join, each at its own handler."""
    result = preview(corpora.workspace("nest"), NEST_ORIGIN, "nestok")
    assert result.routes_served == 2
    assert result.counts == {"would-link": 2}
    handlers = {row.route: row.handler for row in result.rows}
    assert handlers == {
        "/health": "src/health.controller.ts:5",
        "/shop/items/42": "src/shop.controller.ts:5",
    }
    assert result.can_create and result.requirements == ()


def test_f1_both_nest_routes_are_published_in_the_inventory(corpora: Corpora) -> None:
    found = rows_of(corpora, "nest", "nestok")
    assert set(found) == {("GET", "/health"), ("GET", "/shop/items/:id")}
    for rows in found.values():
        assert len(rows) == 1 and rows[0].status in PUBLISHED


def test_f1_a_mounted_router_with_a_prefix_is_served_without_lines(corpora: Corpora) -> None:
    """Python's side of the same mechanism: ``/v1/ping`` is served and is stated nowhere."""
    found = rows_of(corpora, "py", "pyprov")
    [ping] = found[("GET", "/v1/ping")]
    assert ping.status in PUBLISHED
    assert ping.line_start is None and ping.line_end is None


# -- F2: the state of a declaration reads per-file edges only -----------------------------------


def test_f2_a_repository_serving_only_promoted_routes_is_applied_and_joins_its_caller(
    corpora: Corpora,
) -> None:
    """``nestcomposed`` serves ``/shop/items/:id`` and nothing else. Declared through a plain API
    create and synced, its declaration is applied and joined once, ``fetch_item`` is linked
    through it, and ``fetch_health`` (a route nothing serves there) stays unmatched, while
    ``http_operations`` lists the route as published."""
    nest = corpora.workspace("nest-declared")
    listed = CapabilityService(nest).http_connections(HttpConnectionsRequest(context=GLOBAL))
    [declaration] = listed.declarations
    assert declaration.repository == slug_of(nest, "nestcomposed")
    assert (declaration.state, declaration.reason) == ("applied", "")
    assert declaration.joined == 1
    found = rows_of(corpora, "nest-declared", "nestcomposed")
    assert set(found) == {("GET", "/shop/items/:id")}
    assert found[("GET", "/shop/items/:id")][0].status in PUBLISHED
    made = caller_decisions(nest)
    assert made["fetch_item"][0] == "linked"
    assert made["fetch_health"][0] == "http-service-unmatched"


def test_f2_without_a_declaration_the_worklist_lists_none(corpora: Corpora) -> None:
    listed = CapabilityService(corpora.workspace("nest")).http_connections(
        HttpConnectionsRequest(context=GLOBAL)
    )
    assert listed.declarations == ()


# -- the default inventory of the R1 corpus: what it lists, and what it hides ---------------------


def test_the_default_inventory_of_pyprov_lists_five_routes_and_hides_three(
    corpora: Corpora,
) -> None:
    found = rows_of(corpora, "py", "pyprov")
    assert set(found) == {
        ("GET", "/health"),
        ("GET", "/v1/ping"),
        ("GET", "/orders/{oid}"),
        ("GET", "/carts"),
        ("GET", "/lost"),
    }
    for key in (("GET", "/health"), ("GET", "/v1/ping")):
        assert found[key][0].status in PUBLISHED
    declared = {route: found[("GET", route)][0] for route in ("/orders/{oid}", "/carts", "/lost")}
    assert {row.status for row in declared.values()} == {OperationStatus.DECLARED}
    assert declared["/orders/{oid}"].detail == NOT_MOUNTED
    assert declared["/carts"].detail == UNREADABLE_MOUNT
    assert declared["/lost"].detail == MODULE_UNRESOLVED
    # the three recognised declarations the default inventory does not show (cases 4, 4b, 8a)
    rows, response = inventory(corpora.workspace("py"), "pyprov")
    assert {r.path for r in providers(rows)}.isdisjoint(
        {"svc/reports.py", "svc/audit.py", "svc/app2.py"}
    )
    assert response.coverage.declared == 3


def test_a_router_mounted_by_a_main_is_published_where_the_same_router_unmounted_is_declared(
    corpora: Corpora,
) -> None:
    """The misattribution guard's other half: ``pyprov2`` mounts the ``orders.py`` of ``pyprov``."""
    found = rows_of(corpora, "py2", "pyprov2")
    assert set(found) == {("GET", "/orders/{oid}")}
    assert found[("GET", "/orders/{oid}")][0].status in PUBLISHED


def test_an_express_router_with_no_mount_has_no_row(corpora: Corpora) -> None:
    """Case 9, today: ``/orphan`` is not extracted, so no row and no diagnostic."""
    found = rows_of(corpora, "ts", "tsprov")
    assert set(found) == {("GET", "/up")}
    assert found[("GET", "/up")][0].status in PUBLISHED


def test_a_traced_fastify_route_nothing_proves_has_no_row(corpora: Corpora) -> None:
    """N18 (a factory nothing calls) and N21 (a file with no Fastify): diagnostics, not rows."""
    rows, _ = inventory(corpora.workspace("neg"), "neg")
    listed = {row.path for row in providers(rows)}
    assert listed.isdisjoint({"src/n18.ts", "src/n21.ts", "src/n09.ts", "src/n20.ts"})


def test_the_per_file_cap_is_silent_today(corpora: Corpora) -> None:
    """256 of ``big.ts``'s 257 traced routes and all 255 of ``small.ts``'s are published, and
    nothing says the 257th was dropped."""
    rows, response = inventory(corpora.workspace("fastify"), "fcap")
    by_file: dict[str | None, int] = {}
    for row in providers(rows):
        assert row.status in PUBLISHED
        by_file[row.path] = by_file.get(row.path, 0) + 1
    assert by_file == {"src/big.ts": 256, "src/small.ts": 255}
    assert response.coverage.declared == 0


def test_a_plugin_nothing_registers_is_declared_with_the_generic_reason_in_both_helper_repos(
    corpora: Corpora,
) -> None:
    for name in ("fhelp", "fhelp255"):
        found = rows_of(corpora, "fastify", name)
        assert set(found) == {("GET", "/i"), ("GET", "/o")}, name
        assert found[("GET", "/i")][0].status in PUBLISHED
        assert found[("GET", "/o")][0].status is OperationStatus.DECLARED
        assert found[("GET", "/o")][0].detail == NOT_REGISTERED


def test_both_unreadable_fastify_registrations_say_the_same_thing(corpora: Corpora) -> None:
    """F3: a ``fastify-plugin`` plugin given a prefix and a registration with spread options."""
    found = rows_of(corpora, "fastify", "ftblk")
    assert set(found) == {("GET", "/pa"), ("GET", "/pb")}
    for rows in found.values():
        assert rows[0].status is OperationStatus.DECLARED
        assert rows[0].detail == UNREADABLE_REGISTRATION


def test_nest_refusals_are_declared_with_the_applications_own_reason(corpora: Corpora) -> None:
    withheld = rows_of(corpora, "nest", "nestprov")
    assert set(withheld) == {("GET", "/items/:id")}
    assert withheld[("GET", "/items/:id")][0].status is OperationStatus.DECLARED
    assert withheld[("GET", "/items/:id")][0].detail in {
        "RouterModule is imported in this package",
        "RouterModule is registered",
    }
    bare = rows_of(corpora, "nest", "nestbare")
    assert set(bare) == {("GET", "/bare/list")}
    assert bare[("GET", "/bare/list")][0].status is OperationStatus.DECLARED
    assert bare[("GET", "/bare/list")][0].detail == "not registered by a proven application"


def test_an_oas3_document_is_listed_and_the_applications_that_cannot_serve_it_are_not(
    corpora: Corpora,
) -> None:
    rows, _ = inventory(corpora.workspace("oas"), "oasprov")
    listed = providers(rows)
    assert [(r.method, r.path) for r in listed] == [("GET", "api/openapi.yaml")]
    assert listed[0].status in PUBLISHED


def test_a_flask_blueprint_registered_under_a_condition_is_declared_as_not_mounted(
    corpora: Corpora,
) -> None:
    found = rows_of(corpora, "flask", "flaskprov")
    assert set(found) == {("GET", "/v")}
    assert found[("GET", "/v")][0].status is OperationStatus.DECLARED
    assert found[("GET", "/v")][0].detail == NOT_MOUNTED


@pytest.mark.parametrize("corpus", ["py", "nest", "neg", "fastify", "oas"])
def test_the_default_output_carries_no_diagnostic_field(corpora: Corpora, corpus: str) -> None:
    """M18: the opt-in fields are absent unless asked for, from every row and the coverage."""
    workspace = corpora.workspace(corpus)
    for repository in workspace.repositories.list_all():
        rows, response = inventory(workspace, repository.name)
        for row in rows:
            written = json.loads(row.model_dump_json())
            assert "diagnostic" not in written and "declared_match_count" not in written
        assert "provider_lifecycle" not in json.loads(response.coverage.model_dump_json())


# -- the routes the global pass sees ----------------------------------------------------------


@pytest.mark.parametrize("corpus", CORPUS_NAMES)
def test_the_routes_code_serves_are_the_served_edges_of_the_plan_and_of_the_files(
    corpora: Corpora, corpus: str
) -> None:
    """M15: ``code_routes`` is the promoted ``SERVES_HTTP_ROUTE`` edges of the plan, and a
    repository's routes are those with the per-file ones. Nothing a plan only reports is in it."""
    workspace = corpora.workspace(corpus)
    for repository in workspace.repositories.list_all():
        if repository.slug == DECLARATIONS_SLUG or repository.id is None:
            continue
        plan = plan_repository_http(workspace.knowledge, repository.id).plan
        promoted = {
            (e.src_id, e.target.name)
            for e in plan.http
            if e.type is EdgeType.SERVES_HTTP_ROUTE and e.file_id is not None
        }
        assert {(r.src_id, r.body) for r in plan.code_routes} == promoted, repository.slug
        servers = workspace.knowledge.list_route_servers(
            exclude_parser=GLOBAL_PARSER, repo_id=repository.id
        )
        per_file = {(r.src_id, r.body) for r in served_by_servers(servers)}
        routes = repository_routes(workspace.knowledge, repository.id)
        assert {(r.src_id, r.body) for r in routes} == promoted | per_file, repository.slug


# -- cases 12-15: callers whose provider is declared but not served ----------------------------


def test_case_12_both_callers_of_pyprov_s_origin_stay_unmatched(corpora: Corpora) -> None:
    made = caller_decisions(corpora.workspace("py"))
    assert made["fetch_order"][0] == "http-service-unmatched"
    assert made["fetch_nothing"][0] == "http-service-unmatched"


def test_case_12_a_preview_against_pyprov_joins_nothing_and_says_so(corpora: Corpora) -> None:
    result = preview(corpora.workspace("py"), ORDERS_ORIGIN, "pyprov")
    assert result.routes_served == 2
    assert result.counts == {"no-matching-route": 2}
    assert [row.outcome for row in result.rows] == ["no-matching-route"] * 2
    assert result.requirements == (NOTHING_JOINED,)
    assert result.can_create is False


def test_case_14_a_preview_against_twinprov_joins_nothing_and_none_becomes_ambiguous(
    corpora: Corpora,
) -> None:
    """``twinprov`` serves only ``/status``; its two providers of one shape are unmounted."""
    result = preview(corpora.workspace("py"), ORDERS_ORIGIN, "twinprov")
    assert result.routes_served == 1
    assert result.counts == {"no-matching-route": 2}
    assert result.requirements == (NOTHING_JOINED,)
    assert result.can_create is False


def test_case_15_a_repository_that_is_not_indexed_is_refused(corpora: Corpora) -> None:
    py = corpora.workspace("py")
    with pytest.raises(InvalidDeploymentLinkError, match="not indexed"):
        CapabilityService(py).http_connection_preview(
            HttpConnectionPreviewRequest(
                context=GLOBAL, group=group_key(py, ORDERS_ORIGIN), repository="nowhere"
            )
        )


def test_case_15_a_repository_with_no_route_is_not_an_indexed_service(corpora: Corpora) -> None:
    py = corpora.workspace("py")
    result = preview(py, ORDERS_ORIGIN, "emptyprov")
    assert result.routes_served == 0
    assert result.requirements[0] == (
        f"{slug_of(py, 'emptyprov')} serves no route this index holds: it is not an indexed service"
    )
    assert result.can_create is False


def test_p7_a_declaration_from_the_worklist_that_joins_nothing_is_still_refused(
    workspace: Workspace, tmp_path: Path
) -> None:
    """``deployment_link_create`` with a ``request_group`` for ``pyprov`` against ``buyer2``'s
    group raises today, and R1 does not let its diagnostics change that."""
    build(workspace, tmp_path, {"pyprov": PYPROV, "buyer2": BUYER2})
    group = group_key(workspace, ORDERS_ORIGIN)
    with pytest.raises(InvalidDeploymentLinkError):
        declare_plain(workspace, "pyprov", ORDERS_ORIGIN, group=group)
    assert workspace.deployment_links.list_all() == []
