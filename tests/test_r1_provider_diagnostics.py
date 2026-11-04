"""R1 provider diagnostics through ``http_operations(include_diagnostics=true)`` (gate G3).

Every test here describes NEW behaviour, written before it exists (gate G0) from the fixtures of
``R1_PREDICTIONS.md`` section 4, and is marked ``xfail(strict=True)``: it fails today, and when
the feature lands it must pass, so an unexpected pass fails the run and the marker has to be
removed deliberately. An expectation that fails once the feature exists is escalated, never
edited to fit: the predictions are frozen.

What each recognised-but-unserved declaration is shown as: its lifecycle (path, identity,
registration, publication, join eligibility), the category and stable code of the stage that
failed, the planner's own reason verbatim in ``detail``, and the real places that caused it.

Rows are typed ``Any`` below: ``HttpOperation.diagnostic`` does not exist until G3.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from repoatlas.capabilities.errors import InvalidCursorError
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import DECLARATIONS_SLUG, Settings
from repoatlas.graph.ts_routes import UNREADABLE_REGISTRATION
from repoatlas.models.http_inventory import (
    CrossingStatus,
    HttpOperationsRequest,
    OperationStatus,
)
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build
from tests.incremental_corpus import assert_equals_fresh, edit, sync_all
from tests.r1_provider_corpus import (
    GLOBAL,
    PY_MAIN,
    PYPROV,
    Corpora,
    inventory,
    repository_context,
)

G3 = pytest.mark.xfail(strict=True, reason="R1 gate G3/G4")

PUBLISHED = {OperationStatus.CALLED, OperationStatus.NO_INDEXED_CALLER}
#: The corpora whose rows are all classified: every existing declared row has a real category.
CLASSIFIED = ("py", "py2", "flask", "pybounds", "neg", "fan", "fastify", "ts", "nest", "oas")

Row = Any


@pytest.fixture(scope="module")
def corpora(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Corpora]:
    built = Corpora(tmp_path_factory.mktemp("r1-diagnostics"))
    yield built
    built.close()


def diagnosed(corpora: Corpora, corpus: str, name: str, **fields: Any) -> tuple[list[Row], Any]:
    """The inventory of one repository with ``include_diagnostics`` on."""
    return inventory(corpora.workspace(corpus), name, include_diagnostics=True, **fields)


def listed(rows: list[Row]) -> list[Row]:
    return [row for row in rows if row.role == "provider"]


def find(rows: list[Row], *, path: str, route: str | None = None, method: str | None = None) -> Row:
    """The one provider row at ``path`` (and ``route`` and ``method``, when given)."""
    found = [
        row
        for row in listed(rows)
        if row.path == path
        and (route is None or row.route == route)
        and (method is None or row.method == method)
    ]
    shown = [(r.method, r.route, r.path, r.status.value) for r in listed(rows)]
    assert len(found) == 1, f"expected one provider row at {path} {route}, got {shown}"
    return found[0]


def said(value: Any) -> Any:
    """An enum's text, or the value itself."""
    return getattr(value, "value", value)


def expect(
    row: Row,
    *,
    status: str,
    category: str,
    code: str,
    path: str | None = None,
    identity: str | None = None,
    registration: str | None = None,
) -> None:
    """A refused declaration: not published, not join-eligible, and where and why it stopped."""
    diagnostic = row.diagnostic
    assert diagnostic is not None
    assert row.status.value == status
    assert said(diagnostic.category) == category
    assert diagnostic.code == code
    assert diagnostic.published is False and diagnostic.join_eligible is False
    assert row.detail, "the planner's own reason stays in detail, verbatim"
    for stage, wanted in (("path", path), ("identity", identity), ("registration", registration)):
        if wanted is not None:
            assert said(getattr(diagnostic, stage)) == wanted, stage


def line_of(text: str, needle: str) -> int:
    return next(number for number, row in enumerate(text.splitlines(), 1) if needle in row)


# -- pyprov: Python routes, mounts and classes (cases 1-5, 8a, 17) ---------------------------------


@G3
def test_case_1_a_route_the_application_serves_is_published_and_join_eligible(
    corpora: Corpora,
) -> None:
    rows, _ = diagnosed(corpora, "py", "pyprov")
    health = find(rows, path="svc/main.py", route="/health")
    diagnostic = health.diagnostic
    assert health.status in PUBLISHED
    assert said(diagnostic.decided_by) == "file"
    assert diagnostic.published is True and diagnostic.join_eligible is True
    assert tuple(diagnostic.join_limits) == ()
    assert diagnostic.category is None and diagnostic.code == ""
    assert said(diagnostic.registration) == "proven"


@G3
def test_case_17_a_route_served_through_a_prefix_is_join_eligible_but_states_no_line(
    corpora: Corpora,
) -> None:
    rows, _ = diagnosed(corpora, "py", "pyprov")
    ping = find(rows, path="svc/main.py", route="/v1/ping")
    diagnostic = ping.diagnostic
    assert ping.status in PUBLISHED and ping.line_start is None
    assert diagnostic.published is True and diagnostic.join_eligible is True
    assert tuple(diagnostic.join_limits) == ("no-declaration-line",)


@G3
def test_case_2_an_unmounted_router_is_declared_and_says_which_stage_stopped(
    corpora: Corpora,
) -> None:
    rows, _ = diagnosed(corpora, "py", "pyprov")
    expect(
        find(rows, path="svc/orders.py", route="/orders/{oid}"),
        status="declared",
        category="registration-unproven",
        code="python-not-mounted",
        path="resolved",
        identity="resolved",
        registration="unproven",
    )


@G3
def test_case_3_an_unreadable_mount_names_the_mount_and_not_the_route_s_own_file(
    corpora: Corpora,
) -> None:
    rows, _ = diagnosed(corpora, "py", "pyprov")
    carts = find(rows, path="svc/carts.py", route="/carts")
    expect(
        carts,
        status="declared",
        category="registration-unproven",
        code="python-mount-unreadable",
        registration="unproven",
    )
    related = carts.diagnostic.related
    # M8: the place is the mount in main.py, never the route's own file
    assert [(place.path, said(place.role)) for place in related] == [("svc/main.py", "mount")]
    assert related[0].line_start == line_of(PY_MAIN, "app.include_router(carts")


@G3
def test_case_5_a_receiver_imported_from_a_module_that_does_not_exist_is_an_identity_failure(
    corpora: Corpora,
) -> None:
    rows, _ = diagnosed(corpora, "py", "pyprov")
    expect(
        find(rows, path="svc/main.py", route="/lost"),
        status="declared",
        category="identity-unresolved",
        code="python-module-unresolved",
        path="resolved",
        identity="unresolved",
    )


@G3
def test_case_4_a_route_whose_path_is_a_call_result_is_a_new_unpublished_row(
    corpora: Corpora,
) -> None:
    rows, _ = diagnosed(corpora, "py", "pyprov")
    reports = find(rows, path="svc/reports.py")
    expect(
        reports,
        status="unpublished",
        category="path-unresolved",
        code="python-path-constant-unresolved",
        path="unresolved",
        identity="resolved",
    )
    assert reports.route == ""


@G3
def test_case_4b_a_route_with_a_readable_path_and_no_mount_was_hidden_and_is_now_listed(
    corpora: Corpora,
) -> None:
    rows, _ = diagnosed(corpora, "py", "pyprov")
    audit = find(rows, path="svc/audit.py")
    expect(
        audit,
        status="unpublished",
        category="registration-unproven",
        code="python-not-mounted",
        path="resolved",
        identity="resolved",
        registration="unproven",
    )
    assert audit.route == "/audit" and audit.method == "GET"


@G3
def test_case_8a_a_class_no_module_offers_is_an_identity_failure(corpora: Corpora) -> None:
    rows, _ = diagnosed(corpora, "py", "pyprov")
    expect(
        find(rows, path="svc/app2.py", route="/c2"),
        status="unpublished",
        category="identity-unresolved",
        code="python-class-not-offered",
    )


@G3
def test_the_lifecycle_of_pyprov_adds_up(corpora: Corpora) -> None:
    _, response = diagnosed(corpora, "py", "pyprov")
    lifecycle = response.coverage.provider_lifecycle
    assert (
        lifecycle.recognized,
        lifecycle.published,
        lifecycle.declared,
        lifecycle.unpublished,
        lifecycle.join_eligible,
    ) == (8, 2, 3, 3, 2)
    assert lifecycle.truncated is False


@G3
def test_case_8b_a_blueprint_registered_under_a_condition_is_declared_as_not_mounted(
    corpora: Corpora,
) -> None:
    """The parser's precise reason (a conditional mount) is internal to it: an R1b item."""
    rows, _ = diagnosed(corpora, "flask", "flaskprov")
    expect(
        find(rows, path="fl/views.py", route="/v"),
        status="declared",
        category="registration-unproven",
        code="python-not-mounted",
    )


@G3
def test_a_router_mounted_by_a_main_is_published_where_the_same_one_unmounted_is_declared(
    corpora: Corpora,
) -> None:
    """The misattribution guard: ``pyprov``'s ``orders.py`` and ``pyprov2``'s are one file."""
    unmounted, _ = diagnosed(corpora, "py", "pyprov")
    mounted, _ = diagnosed(corpora, "py2", "pyprov2")
    assert find(unmounted, path="svc/orders.py").status is OperationStatus.DECLARED
    row = find(mounted, path="svc/orders.py")
    assert row.status in PUBLISHED
    assert row.diagnostic.published is True


# -- the Python bounds ----------------------------------------------------------------------


@G3
def test_a_router_nested_deeper_than_the_bound_is_bound_reached_and_the_last_in_bound_is_served(
    corpora: Corpora,
) -> None:
    rows, _ = diagnosed(corpora, "pybounds", "chain")
    assert find(rows, path="chain/src/r4.py").status in PUBLISHED
    for deep in ("chain/src/r5.py", "chain/src/r6.py"):
        row = find(rows, path=deep)
        expect(
            row,
            status="declared",
            category="bound-reached",
            code="python-chain-too-deep",
            registration="unproven",
        )
        assert "python-mount-depth" in row.diagnostic.bounds


@G3
def test_a_constant_chain_of_five_hops_is_bound_reached_and_four_is_served(
    corpora: Corpora,
) -> None:
    five, _ = diagnosed(corpora, "pybounds", "hops5")
    row = find(five, path="src/routes.py")
    expect(
        row,
        status="unpublished",
        category="bound-reached",
        code="python-constant-hops",
        path="unresolved",
        identity="resolved",
    )
    assert tuple(row.diagnostic.bounds) == ("python-constant-hops",)
    four, _ = diagnosed(corpora, "pybounds", "hops4")
    served = find(four, path="src/routes.py")
    assert served.status in PUBLISHED and served.diagnostic.category is None


@G3
def test_a_cycle_of_constants_is_unresolved_and_not_a_bound(corpora: Corpora) -> None:
    rows, _ = diagnosed(corpora, "pybounds", "cycle")
    row = find(rows, path="src/routes.py")
    expect(
        row,
        status="unpublished",
        category="path-unresolved",
        code="python-path-constant-unresolved",
    )
    assert tuple(row.diagnostic.bounds) == ()


# -- case 6: a Fastify helper refusal (the NEG repository) -------------------------------------

#: (path, route) -> (category, code). The existing declared rows of ``neg`` gain codes.
NEG_DECLARED = {
    ("src/n03.ts", "/n3"): ("registration-unproven", "fastify-helper-control-flow"),
    ("src/n16.ts", "/n16"): ("registration-unproven", "fastify-helper-control-flow"),
    ("src/n05.ts", "/n5"): ("registration-unproven", "fastify-helper-argument-position"),
    ("src/n17.ts", "/n17"): ("registration-unproven", "fastify-helper-argument-unsupported"),
    ("src/n08.ts", "/n8"): ("identity-unresolved", "fastify-receiver-before-binding"),
    ("src/n15.ts", "/n15"): ("framework-constraint", "fastify-after-listen"),
    ("src/n12.ts", "/n12"): ("framework-constraint", "fastify-factory-not-awaited"),
    ("src/n01.ts", "/n1"): ("identity-unresolved", "fastify-factory-unproven"),
    ("src/n02.ts", "/n2"): ("identity-unresolved", "fastify-factory-unproven"),
    ("src/n19.ts", "/n19"): ("registration-unproven", "fastify-entry-not-called"),
}
#: Refusals no helper call of the route's own scope explains: the generic code, never a
#: misattributed one (the misattribution guard).
NEG_GENERIC = (
    ("src/n04.ts", "/n4"),
    ("src/n06.ts", "/n6"),
    ("src/n07.ts", "/n7"),
    ("src/n11.ts", "/n11"),
    ("src/real10.ts", "/n10"),
    ("src/decoy10.ts", "/decoy10"),
)


@G3
@pytest.mark.parametrize(("path", "route"), sorted(NEG_DECLARED))
def test_case_6_a_refused_helper_call_gives_its_route_the_refusal_code(
    corpora: Corpora, path: str, route: str
) -> None:
    rows, _ = diagnosed(corpora, "neg", "neg")
    category, code = NEG_DECLARED[(path, route)]
    expect(find(rows, path=path, route=route), status="declared", category=category, code=code)


@G3
@pytest.mark.parametrize(("path", "route"), NEG_GENERIC)
def test_case_6_a_route_no_scoped_refusal_explains_keeps_the_generic_code(
    corpora: Corpora, path: str, route: str
) -> None:
    rows, _ = diagnosed(corpora, "neg", "neg")
    expect(
        find(rows, path=path, route=route),
        status="declared",
        category="registration-unproven",
        code="fastify-not-registered",
    )


@G3
def test_case_6_a_factory_nothing_calls_and_a_file_without_fastify_are_new_unpublished_rows(
    corpora: Corpora,
) -> None:
    rows, response = diagnosed(corpora, "neg", "neg")
    expect(
        find(rows, path="src/n18.ts", route="/n18"),
        status="unpublished",
        category="registration-unproven",
        code="fastify-factory-not-instantiated",
    )
    expect(
        find(rows, path="src/n21.ts", route="/n21"),
        status="unpublished",
        category="identity-unresolved",
        code="fastify-factory-unproven",
    )
    # P3: neg has exactly these two
    assert response.coverage.provider_lifecycle.unpublished == 2
    # a helper that does not resolve, and one from a package, record no route at all
    for silent in ("src/n09.ts", "src/n20.ts"):
        assert not [row for row in listed(rows) if row.path == silent]


@G3
def test_case_7_a_chain_too_deep_and_a_fan_out_too_complex_are_bound_reached(
    corpora: Corpora,
) -> None:
    rows, _ = diagnosed(corpora, "neg", "neg")
    for route in ("/d5", "/d6"):
        expect(
            find(rows, path="src/chain13.ts", route=route),
            status="declared",
            category="bound-reached",
            code="fastify-chain-too-deep",
        )
    served = find(rows, path="src/chain13.ts", route="/d1")
    assert served.status in PUBLISHED
    assert said(served.diagnostic.decided_by) == "global-pass"
    fan, _ = diagnosed(corpora, "fan", "fan")
    expect(
        find(fan, path="src/fan.ts", route="/leaf"),
        status="declared",
        category="bound-reached",
        code="fastify-chain-too-complex",
    )


# -- case 7: the per-file caps ----------------------------------------------------------------


@G3
def test_case_7_a_file_at_the_traced_route_cap_is_reported_and_one_below_it_is_not(
    corpora: Corpora,
) -> None:
    _, response = diagnosed(corpora, "fastify", "fcap")
    lifecycle = response.coverage.provider_lifecycle
    notes = [
        (note.bound, note.limit, said(note.scope), note.path) for note in lifecycle.bounds_reached
    ]
    assert notes == [("fastify-traced-routes-per-file", 256, "file", "src/big.ts")]
    assert lifecycle.truncated is True
    assert lifecycle.unpublished == 0
    assert lifecycle.published == 256 + 255


@G3
def test_case_7_a_plugin_nothing_registers_beside_a_file_at_the_helper_cap_is_bound_reached(
    corpora: Corpora,
) -> None:
    rows, _ = diagnosed(corpora, "fastify", "fhelp")
    row = find(rows, path="src/app.ts", route="/o")
    expect(row, status="declared", category="bound-reached", code="fastify-not-registered")
    assert tuple(row.diagnostic.bounds) == ("fastify-helper-calls-per-file",)
    control, _ = diagnosed(corpora, "fastify", "fhelp255")
    unbounded = find(control, path="src/app.ts", route="/o")
    expect(
        unbounded,
        status="declared",
        category="registration-unproven",
        code="fastify-not-registered",
    )
    assert tuple(unbounded.diagnostic.bounds) == ()


@G3
def test_f3_two_registrations_one_detail_two_categories(corpora: Corpora) -> None:
    rows, _ = diagnosed(corpora, "fastify", "ftblk")
    prefixed = find(rows, path="src/app.ts", route="/pa")
    spread = find(rows, path="src/app.ts", route="/pb")
    expect(
        prefixed,
        status="declared",
        category="framework-constraint",
        code="fastify-plugin-prefix-ignored",
        path="resolved",
        identity="resolved",
        registration="unproven",
    )
    expect(
        spread,
        status="declared",
        category="registration-unproven",
        code="fastify-registration-unreadable",
    )
    assert prefixed.detail == spread.detail == UNREADABLE_REGISTRATION


# -- case 9: what stays unrecoverable ------------------------------------------------------------


@G3
def test_case_9_an_express_router_with_no_mount_has_no_row_and_the_gap_is_declared(
    corpora: Corpora,
) -> None:
    rows, response = diagnosed(corpora, "ts", "tsprov")
    assert [row.route for row in listed(rows)] == ["/up"]
    assert find(rows, path="src/server.ts", route="/up").status in PUBLISHED
    missing = {item.code: item.count for item in response.coverage.provider_lifecycle.not_extracted}
    assert "express-router-without-mount" in missing
    assert missing["express-router-without-mount"] is None


# -- cases 10, 11: NestJS and oas3-tools --------------------------------------------------------


@G3
def test_case_10_a_router_module_withholds_the_application_s_routes(corpora: Corpora) -> None:
    rows, _ = diagnosed(corpora, "nest", "nestprov")
    row = find(rows, path="src/items.controller.ts", route="/items/:id")
    expect(
        row,
        status="declared",
        category="framework-constraint",
        code="nestjs-router-module",
        identity="resolved",
        registration="unproven",
    )
    assert row.detail in {"RouterModule is imported in this package", "RouterModule is registered"}


@G3
def test_case_10_a_repository_with_no_application_says_there_is_none(corpora: Corpora) -> None:
    rows, _ = diagnosed(corpora, "nest", "nestbare")
    row = find(rows, path="src/bare.controller.ts", route="/bare/list")
    expect(row, status="declared", category="registration-unproven", code="nestjs-no-application")
    assert row.detail == "not registered by a proven application"


@G3
def test_case_11_the_document_is_published_and_not_join_eligible_and_two_applications_say_why(
    corpora: Corpora,
) -> None:
    rows, response = diagnosed(corpora, "oas", "oasprov")
    document = find(rows, path="api/openapi.yaml")
    diagnostic = document.diagnostic
    assert document.status in PUBLISHED
    assert diagnostic.published is True and diagnostic.join_eligible is False
    assert tuple(diagnostic.join_limits) == ("openapi-document",)
    relative = find(rows, path="src/a.ts")
    expect(relative, status="unpublished", category="path-unresolved", code="spec-cwd-relative")
    assert said(relative.diagnostic.subject) == "application"
    assert relative.method is None
    unresolved = find(rows, path="src/b.ts")
    assert unresolved.status.value == "unpublished"
    assert said(unresolved.diagnostic.category) == "identity-unresolved"
    assert unresolved.diagnostic.code in {"oas3-controllers-unresolved", "oas3-controllers-unread"}
    assert response.coverage.provider_lifecycle.unpublished == 2


# -- the rules every row obeys -----------------------------------------------------------------


def repositories(workspace: Workspace) -> list[str]:
    return [
        repo.name for repo in workspace.repositories.list_all() if repo.slug != DECLARATIONS_SLUG
    ]


@G3
@pytest.mark.parametrize("corpus", CLASSIFIED)
def test_p1_p2_a_row_is_published_or_it_is_declared_or_unpublished_and_never_unclassified(
    corpora: Corpora, corpus: str
) -> None:
    """M1, M2, M4: only a served row is published; the rest say which stage stopped, in the
    planner's own words, and are not join-eligible."""
    workspace = corpora.workspace(corpus)
    for name in repositories(workspace):
        rows, _ = inventory(workspace, name, include_diagnostics=True)
        for row in listed(rows):
            diagnostic = row.diagnostic
            assert diagnostic is not None, (name, row.route)
            if diagnostic.published:
                assert row.status in PUBLISHED
                assert diagnostic.category is None and diagnostic.code == ""
                assert said(diagnostic.registration) == "proven"
            else:
                assert row.status.value in {"declared", "unpublished"}, (name, row.route)
                assert diagnostic.join_eligible is False
                assert row.detail
                assert diagnostic.code and diagnostic.category is not None
                assert said(diagnostic.category) != "unclassified", (name, row.route)


def written(row: Row, drop: frozenset[str] = frozenset()) -> dict[str, Any]:
    return {k: v for k, v in json.loads(row.model_dump_json()).items() if k not in drop}


@G3
@pytest.mark.parametrize("corpus", CLASSIFIED)
def test_p1_p2_p8_what_the_default_inventory_listed_is_listed_unchanged_beside_the_diagnostics(
    corpora: Corpora, corpus: str
) -> None:
    """Every row of the default inventory is the same row with the flag on, field for field but
    the opt-in ones; the only rows added are ``unpublished``."""
    workspace = corpora.workspace(corpus)
    added = frozenset({"diagnostic", "declared_match_count"})
    for name in repositories(workspace):
        plain, _ = inventory(workspace, name)
        both, _ = inventory(workspace, name, include_diagnostics=True)
        kept = [row for row in both if row.status.value != "unpublished"]
        assert [written(row, added) for row in kept] == [written(row) for row in plain], name


# -- case 16: re-indexing ---------------------------------------------------------------------

EDITED_ORDERS = "from fastapi import APIRouter\n\nrouter = APIRouter()\n"


def _mounted(module: str, alias: str) -> str:
    """``svc/main.py`` with one more router imported from ``module`` and mounted."""
    return (
        PY_MAIN.replace(
            "from .reports import router as reports\n",
            f"from .{module} import router as {alias}\nfrom .reports import router as reports\n",
        )
        + f"app.include_router({alias})\n"
    )


@G3
def test_case_16a_mounting_a_declared_router_publishes_it(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    build(workspace, tmp_path, {"pyprov": PYPROV})
    root = tmp_path / "repos"
    before, _ = inventory(workspace, "pyprov", include_diagnostics=True)
    assert find(before, path="svc/orders.py").status is OperationStatus.DECLARED
    edit(root, "pyprov", "svc/main.py", _mounted("orders", "orders"))
    sync_all(workspace, root)
    assert_equals_fresh(workspace, settings, root, tmp_path, "mounted-orders")
    after, _ = inventory(workspace, "pyprov", include_diagnostics=True)
    orders = find(after, path="svc/orders.py", route="/orders/{oid}")
    assert orders.status in PUBLISHED
    assert orders.diagnostic.published is True and orders.diagnostic.join_eligible is True
    assert orders.diagnostic.category is None


@G3
def test_case_16b_deleting_the_route_removes_the_row(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    build(workspace, tmp_path, {"pyprov": PYPROV})
    root = tmp_path / "repos"
    before, _ = inventory(workspace, "pyprov", include_diagnostics=True)
    assert find(before, path="svc/orders.py")
    edit(root, "pyprov", "svc/orders.py", EDITED_ORDERS)
    sync_all(workspace, root)
    assert_equals_fresh(workspace, settings, root, tmp_path, "deleted-orders")
    after, _ = inventory(workspace, "pyprov", include_diagnostics=True)
    assert not [row for row in listed(after) if row.path == "svc/orders.py"]


@G3
def test_case_16c_mounting_the_hidden_router_publishes_it(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    build(workspace, tmp_path, {"pyprov": PYPROV})
    root = tmp_path / "repos"
    before, _ = inventory(workspace, "pyprov", include_diagnostics=True)
    assert find(before, path="svc/audit.py").status.value == "unpublished"
    edit(root, "pyprov", "svc/main.py", _mounted("audit", "audit_router"))
    sync_all(workspace, root)
    assert_equals_fresh(workspace, settings, root, tmp_path, "mounted-audit")
    after, _ = inventory(workspace, "pyprov", include_diagnostics=True)
    audit = find(after, path="svc/audit.py", route="/audit")
    assert audit.status in PUBLISHED
    assert audit.diagnostic.published is True


# -- the request, the cursor and the caps -------------------------------------------------------


@G3
def test_the_flag_is_refused_where_it_cannot_apply_and_unpublished_needs_it(
    corpora: Corpora,
) -> None:
    py = corpora.workspace("py")
    context = repository_context(py, "pyprov")
    assert HttpOperationsRequest(context=context, include_diagnostics=True)  # pyright: ignore[reportCallIssue]
    # unpublished rows exist only with the flag
    status: Any = ("unpublished",)
    assert HttpOperationsRequest(context=context, include_diagnostics=True, status=status)  # pyright: ignore[reportCallIssue]
    with pytest.raises(ValidationError):
        HttpOperationsRequest(context=context, status=status)
    # discovery and a stored decision's lookup answer other questions
    discovering: dict[str, Any] = {
        "context": GLOBAL,
        "discover": True,
        "crossing": (CrossingStatus.SERVICE_UNMATCHED,),
    }
    assert HttpOperationsRequest(**discovering)
    with pytest.raises(ValidationError):
        HttpOperationsRequest(**discovering, include_diagnostics=True)  # pyright: ignore[reportCallIssue]
    looking_up: dict[str, Any] = {"context": context, "decision_ref": "hcd1-" + "0" * 20}
    assert HttpOperationsRequest(**looking_up)
    with pytest.raises(ValidationError):
        HttpOperationsRequest(**looking_up, include_diagnostics=True)  # pyright: ignore[reportCallIssue]


@G3
def test_every_page_size_yields_the_same_diagnosed_inventory_and_a_cursor_keeps_its_question(
    corpora: Corpora,
) -> None:
    py = corpora.workspace("py")
    service = CapabilityService(py)
    context = repository_context(py, "pyprov")

    def walk(size: int) -> list[str]:
        seen: list[str] = []
        cursor: str | None = None
        while True:
            page = service.http_operations(
                HttpOperationsRequest(
                    context=context,
                    limit=size,
                    cursor=cursor,
                    include_diagnostics=True,  # pyright: ignore[reportCallIssue]
                )
            )
            seen += [row.model_dump_json() for row in page.operations]
            cursor = page.next_cursor
            if cursor is None:
                return seen

    whole = walk(200)
    assert len(whole) > 7
    for size in (1, 3, 7):
        assert walk(size) == whole
    cursor = service.http_operations(HttpOperationsRequest(context=context, limit=2)).next_cursor
    assert cursor is not None
    with pytest.raises(InvalidCursorError):
        service.http_operations(
            HttpOperationsRequest(context=context, limit=2, cursor=cursor, include_diagnostics=True)  # pyright: ignore[reportCallIssue]
        )


@G3
def test_m13_the_unpublished_cap_says_how_many_it_cut(
    corpora: Corpora, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cap is a module constant of the inventory (``MAX_UNPUBLISHED_ROWS``); lowered to 1,
    two of ``pyprov``'s three unpublished rows are omitted and the answer says so."""
    from repoatlas.capabilities import http_inventory

    monkeypatch.setattr(http_inventory, "MAX_UNPUBLISHED_ROWS", 1)
    rows, response = diagnosed(corpora, "py", "pyprov")
    lifecycle = response.coverage.provider_lifecycle
    assert len([row for row in listed(rows) if row.status.value == "unpublished"]) == 1
    assert lifecycle.unpublished_omitted >= 1
    assert lifecycle.truncated is True
