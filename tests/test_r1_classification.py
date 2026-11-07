"""R1 gate G2: the classification of unserved route declarations.

``repoatlas.graph.http_provider_diagnostics`` reads a finished plan and the claims it was made
from and says, for every recognised route declaration that is not served, which stage failed and
why. These tests pin the table (every outcome and reason the planners can give maps to a
category; anything else is ``unclassified``), the expectations of ``R1_PREDICTIONS.md`` section 4
as amended by A1, determinism, the note caps, and that the global link pass never imports it.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import repoatlas.graph as graph_package
from repoatlas.graph import http_claims, py_routes, ts_oas3, ts_routes
from repoatlas.graph import http_provider_diagnostics as diagnostics
from repoatlas.graph.http_provider_diagnostics import (
    BOUND_HELPER_CALLS,
    BOUND_REACHED,
    BOUND_TRACED_ROUTES,
    CATEGORIES,
    FRAMEWORK_CONSTRAINT,
    IDENTITY_UNRESOLVED,
    PATH_UNRESOLVED,
    REGISTRATION_UNPROVEN,
    SPEC_CODES,
    UNCLASSIFIED,
    BoundNote,
    DiagnosticPlace,
    ProviderDiagnostics,
    RouteDiagnostic,
    classify_code,
    classify_outcome,
    classify_spec_code,
    diagnose,
    join_limits,
)
from repoatlas.graph.linker import RepositoryHttp, plan_repository_http
from repoatlas.parsers import http_fastify
from repoatlas.parsers.http_facts import (
    FASTIFY_ROUTE,
    FASTIFY_TRACED_ROUTE,
    NESTJS_ROUTE,
    OAS3_APP,
    PYTHON_CLASS_ROUTE,
    PYTHON_ROUTE,
)
from tests.r1_provider_corpus import PY_MAIN, Corpora


@pytest.fixture(scope="module")
def corpora(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Corpora]:
    built = Corpora(tmp_path_factory.mktemp("r1-classification"))
    yield built
    built.close()


def diagnosed(
    corpora: Corpora, corpus: str, name: str
) -> tuple[ProviderDiagnostics, RepositoryHttp]:
    workspace = corpora.workspace(corpus)
    repository = next(r for r in workspace.repositories.list_all() if r.name == name)
    assert repository.id is not None
    planned = plan_repository_http(workspace.knowledge, repository.id)
    return diagnose(planned.plan, planned.claims, planned.files), planned


def at(result: ProviderDiagnostics, file: str, route: str | None = None) -> RouteDiagnostic:
    """The one entry in a file (and with a route body, when given)."""
    found = [e for e in result.entries if e.file == file and (route is None or e.route == route)]
    shown = [(e.file, e.route, e.code) for e in result.entries]
    assert len(found) == 1, f"expected one entry at {file} {route}, got {shown}"
    return found[0]


def files_of(result: ProviderDiagnostics) -> set[str]:
    return {entry.file for entry in result.entries}


def stages(entry: RouteDiagnostic) -> tuple[str, str, str]:
    return entry.path, entry.identity, entry.registration


def line_of(text: str, needle: str) -> int:
    return next(number for number, row in enumerate(text.splitlines(), 1) if needle in row)


# -- the table is complete ---------------------------------------------------------------------

PYTHON_OUTCOMES = (
    py_routes.MODULE_UNRESOLVED,
    py_routes.NOT_DECLARED,
    py_routes.WRONG_KIND,
    py_routes.CONSTANT_UNRESOLVED,
    py_routes.UNREADABLE_MOUNT,
    py_routes.NOT_MOUNTED,
)
CLASS_OUTCOMES = (
    py_routes.MODULE_UNRESOLVED,
    py_routes.CLASS_NOT_OFFERED,
    py_routes.CLASS_NOT_AN_APPLICATION,
    py_routes.CLASS_TOO_DEEP,
    py_routes.CLASS_INIT_UNPROVEN,
    py_routes.CLASS_NO_ROUTE,
    py_routes.WRONG_KIND,
    py_routes.CONSTANT_UNRESOLVED,
    py_routes.UNREADABLE_MOUNT,
    py_routes.NOT_MOUNTED,
)
FASTIFY_OUTCOMES = (
    ts_routes.NOT_DECLARED,
    ts_routes.CONSTANT_UNRESOLVED,
    ts_routes.UNREADABLE_REGISTRATION,
    ts_routes.NOT_REGISTERED,
)
#: Every code a Fastify stage or the parser gives a helper call, an instance or a route.
FASTIFY_CODES = (
    http_fastify.FACTORY_UNPROVEN,
    http_fastify.FACTORY_NOT_AWAITED,
    http_fastify.RECEIVER_BEFORE_BINDING,
    http_fastify.CALLEE_BEFORE_BINDING,
    http_fastify.ENTRY_NOT_CALLED,
    http_fastify.HELPER_CONTROL_FLOW,
    http_fastify.HELPER_DYNAMIC,
    http_fastify.HELPER_CALLEE_UNSUPPORTED,
    http_fastify.HELPER_SHADOWED,
    http_fastify.HELPER_ARGUMENT_UNSUPPORTED,
    http_fastify.AFTER_LISTEN,
    ts_routes.FACTORY_UNRESOLVED,
    ts_routes.HELPER_UNRESOLVED,
    ts_routes.HELPER_NOT_A_PLUGIN,
    ts_routes.HELPER_ARGUMENT_POSITION,
    ts_routes.CHAIN_TOO_DEEP,
    ts_routes.CHAIN_TOO_COMPLEX,
    ts_routes.REGISTRATION_UNREADABLE,
    ts_routes.PLUGIN_PREFIX_IGNORED,
    ts_routes.FACTORY_NOT_INSTANTIATED,
)
OAS3_OUTCOMES = (
    ts_oas3.SERVERS_UNREAD,
    ts_oas3.CONTROLLERS_UNREAD,
    ts_oas3.CONTROLLERS_UNRESOLVED,
    ts_oas3.NO_HANDLER,
)
#: The reasons NestJS gives a route outside the two functions below: ``http_claims.py`` plan.
NESTJS_ROUTE_REASONS = (
    "controller not indexed",
    "URI versioning names no version for it",
    "not registered by a proven application",
)


def _strings(node: ast.AST) -> Iterator[str]:
    """The string literals under a node; an f-string as one text with ``'X'`` for each field."""
    if isinstance(node, ast.JoinedStr):
        yield "".join(
            v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "'X'"
            for v in node.values
        )
        return
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node.value
        return
    for child in ast.iter_child_nodes(node):
        yield from _strings(child)


def _reasons(function: Any) -> set[str]:
    """The sentences a function writes (strings with a space), its docstring left out."""
    definition = ast.parse(inspect.getsource(function)).body[0]
    assert isinstance(definition, ast.FunctionDef)
    body = definition.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    return {text for statement in body for text in _strings(statement) if " " in text}


@pytest.mark.parametrize("outcome", PYTHON_OUTCOMES)
def test_every_python_route_outcome_is_classified(outcome: str) -> None:
    assert classify_outcome(PYTHON_ROUTE, outcome).category != UNCLASSIFIED


@pytest.mark.parametrize("outcome", CLASS_OUTCOMES)
def test_every_python_class_route_outcome_is_classified(outcome: str) -> None:
    assert classify_outcome(PYTHON_CLASS_ROUTE, outcome).category != UNCLASSIFIED


@pytest.mark.parametrize("outcome", FASTIFY_OUTCOMES)
@pytest.mark.parametrize("role", [FASTIFY_ROUTE, FASTIFY_TRACED_ROUTE])
def test_every_fastify_route_outcome_is_classified(role: str, outcome: str) -> None:
    assert classify_outcome(role, outcome).category != UNCLASSIFIED


@pytest.mark.parametrize("code", FASTIFY_CODES)
def test_every_fastify_code_is_classified(code: str) -> None:
    classified = classify_code(code)
    assert classified.category in CATEGORIES and classified.category != UNCLASSIFIED
    assert classified.code == code


@pytest.mark.parametrize("outcome", OAS3_OUTCOMES)
def test_every_oas3_application_outcome_is_classified(outcome: str) -> None:
    assert classify_outcome(OAS3_APP, outcome).category != UNCLASSIFIED


@pytest.mark.parametrize("code", SPEC_CODES)
def test_every_spec_path_refusal_is_classified(code: str) -> None:
    classified = classify_spec_code(code)
    assert classified.code == code and classified.category != UNCLASSIFIED
    # the two bounds of the path reader are bounds; the rest are an unresolved path
    bounded = code in {"spec-depth-exceeded", "spec-propagation-depth"}
    assert (classified.category == BOUND_REACHED) is bounded
    assert (classified.category == PATH_UNRESOLVED) is not bounded
    # an application whose outcome says only that its specification was not read takes the code
    assert classify_outcome(OAS3_APP, ts_oas3.SPEC_UNREAD, spec_code=code) == classified
    assert classify_outcome(OAS3_APP, ts_oas3.SPEC_NOT_FOUND, spec_code=code) == classified


def test_the_spec_codes_are_all_of_the_path_reader_s() -> None:
    assert len(SPEC_CODES) == 20 and "spec-cwd-relative" in SPEC_CODES
    assert {"spec-layout-ambiguous", "spec-parameter-test-only"} <= set(SPEC_CODES)


def test_every_nestjs_reason_the_stage_writes_is_classified() -> None:
    """The texts of ``_application`` and ``_dynamic`` are read from their source, so a reason
    added there without a row here fails this test."""
    found = _reasons(http_claims._application) | _reasons(http_claims._dynamic)
    assert found, "the source of the two functions has sentences to read"
    for reason in sorted(found | set(NESTJS_ROUTE_REASONS)):
        classified = classify_outcome(NESTJS_ROUTE, reason)
        assert classified.category != UNCLASSIFIED, reason


@pytest.mark.parametrize(
    ("reason", "code", "category"),
    [
        ("controller not indexed", "nestjs-controller-not-indexed", IDENTITY_UNRESOLVED),
        (
            "URI versioning names no version for it",
            "nestjs-versionless-route",
            FRAMEWORK_CONSTRAINT,
        ),
        ("prefix not proven: unknown", "nestjs-prefix-unproven", PATH_UNRESOLVED),
        (
            "a file in this repository can change any application's prefix",
            "nestjs-prefix-configurable",
            PATH_UNRESOLVED,
        ),
        ("RouterModule is imported in this package", "nestjs-router-module", FRAMEWORK_CONSTRAINT),
        ("RouterModule is registered", "nestjs-router-module", FRAMEWORK_CONSTRAINT),
        (
            "a registered module's metadata cannot be read",
            "nestjs-module-unreadable",
            REGISTRATION_UNPROVEN,
        ),
        ("a registered module is not indexed", "nestjs-module-not-indexed", REGISTRATION_UNPROVEN),
        (
            "import 'BillingModule' does not resolve",
            "nestjs-import-unresolved",
            REGISTRATION_UNPROVEN,
        ),
        ("import 'X' cannot be read", "nestjs-import-unreadable", REGISTRATION_UNPROVEN),
        ("too many modules", "nestjs-modules-bound", BOUND_REACHED),
        (
            "its module is handed to a dynamic module that may re-prefix it",
            "nestjs-dynamic-module",
            FRAMEWORK_CONSTRAINT,
        ),
        (
            "dynamic module 'Auth' has no known source",
            "nestjs-dynamic-module-unresolved",
            REGISTRATION_UNPROVEN,
        ),
        (
            "dynamic module 'Auth' does not resolve",
            "nestjs-dynamic-module-unresolved",
            REGISTRATION_UNPROVEN,
        ),
        (
            "dynamic module 'Auth' is handed too much to read",
            "nestjs-dynamic-arguments-bound",
            BOUND_REACHED,
        ),
    ],
)
def test_each_nestjs_reason_has_its_code(reason: str, code: str, category: str) -> None:
    classified = classify_outcome(NESTJS_ROUTE, reason)
    assert (classified.code, classified.category) == (code, category)


def test_the_generic_nestjs_reason_says_whether_the_repository_has_an_application() -> None:
    generic = "not registered by a proven application"
    assert classify_outcome(NESTJS_ROUTE, generic, has_nestjs_root=False).code == (
        "nestjs-no-application"
    )
    assert classify_outcome(NESTJS_ROUTE, generic, has_nestjs_root=True).code == (
        "nestjs-not-registered"
    )


def test_the_literal_stage_follows_the_claim_s_path() -> None:
    """``L`` of the table: resolved when every part of the path is a literal."""
    literal = classify_outcome(PYTHON_ROUTE, py_routes.MODULE_UNRESOLVED, literal=True)
    built = classify_outcome(PYTHON_ROUTE, py_routes.MODULE_UNRESOLVED, literal=False)
    assert (literal.path, built.path) == ("resolved", "not-evaluated")
    assert (literal.identity, literal.registration) == ("unresolved", "not-evaluated")


# -- the fallback (mutant M6) --------------------------------------------------------------------


@pytest.mark.parametrize(
    "role",
    [PYTHON_ROUTE, PYTHON_CLASS_ROUTE, FASTIFY_ROUTE, FASTIFY_TRACED_ROUTE, NESTJS_ROUTE, OAS3_APP],
)
def test_an_outcome_the_table_does_not_know_is_unclassified_and_nothing_else(role: str) -> None:
    classified = classify_outcome(role, "zzz")
    assert classified == classify_code("zzz")
    assert (classified.category, classified.code) == (UNCLASSIFIED, UNCLASSIFIED)
    assert (classified.path, classified.identity, classified.registration) == (
        "not-evaluated",
        "not-evaluated",
        "not-evaluated",
    )
    assert classified.bounds == ()


def test_a_role_the_table_does_not_know_is_unclassified() -> None:
    assert classify_outcome("zzz-role", py_routes.NOT_MOUNTED).category == UNCLASSIFIED


def test_an_unknown_outcome_in_a_plan_is_an_unclassified_entry(corpora: Corpora) -> None:
    result, planned = diagnosed(corpora, "py", "pyprov")
    orders = at(result, "svc/orders.py")
    changed = dataclasses.replace(
        planned.plan, routes={**planned.plan.routes, orders.claim_id: "zzz"}
    )
    entry = at(diagnose(changed, planned.claims, planned.files), "svc/orders.py")
    assert (entry.category, entry.code, entry.outcome) == (UNCLASSIFIED, UNCLASSIFIED, "zzz")
    assert stages(entry) == ("not-evaluated",) * 3


# -- the expectations of section 4, as amended by A1 --------------------------------------------


def test_pyprov_every_declaration_that_is_not_served_is_diagnosed(corpora: Corpora) -> None:
    result, _ = diagnosed(corpora, "py", "pyprov")
    # /health and /v1/ping are served: no entry. Six are not.
    assert files_of(result) == {
        "svc/main.py",
        "svc/orders.py",
        "svc/carts.py",
        "svc/reports.py",
        "svc/audit.py",
        "svc/app2.py",
    }
    assert len(result.entries) == 6
    assert result.bound_notes == () and result.bound_notes_omitted == 0

    lost = at(result, "svc/main.py")
    assert (lost.category, lost.code) == (IDENTITY_UNRESOLVED, "python-module-unresolved")
    assert stages(lost) == ("resolved", "unresolved", "not-evaluated")
    # G2 ruling (reasoner): GET /lost's receiver is imported and unresolved, so its family
    # is "either" and the framework is not established.
    assert lost.fact == PYTHON_ROUTE and lost.subject == "route"
    assert lost.framework == "fastapi-or-flask"

    orders = at(result, "svc/orders.py")
    assert (orders.category, orders.code) == (REGISTRATION_UNPROVEN, "python-not-mounted")
    assert stages(orders) == ("resolved", "resolved", "unproven")
    assert orders.route == "GET /orders/{oid}" and orders.method == "GET"

    reports = at(result, "svc/reports.py")
    assert (reports.category, reports.code) == (PATH_UNRESOLVED, "python-path-constant-unresolved")
    assert stages(reports) == ("unresolved", "resolved", "not-evaluated")
    assert reports.route == ""

    audit = at(result, "svc/audit.py")
    assert (audit.category, audit.code) == (REGISTRATION_UNPROVEN, "python-not-mounted")
    assert stages(audit) == ("resolved", "resolved", "unproven")
    assert audit.route == "GET /audit"

    second = at(result, "svc/app2.py")
    assert (second.category, second.code) == (IDENTITY_UNRESOLVED, "python-class-not-offered")
    assert second.fact == PYTHON_CLASS_ROUTE and second.route == "GET /c2"


def test_pyprov_an_unreadable_mount_names_the_mount_and_not_the_route_s_own_file(
    corpora: Corpora,
) -> None:
    result, _ = diagnosed(corpora, "py", "pyprov")
    carts = at(result, "svc/carts.py")
    assert (carts.category, carts.code) == (REGISTRATION_UNPROVEN, "python-mount-unreadable")
    assert stages(carts) == ("resolved", "resolved", "unproven")
    assert carts.related_count == 1
    [place] = carts.related
    assert (place.path, place.role) == ("svc/main.py", "mount")
    assert place.line_start == line_of(PY_MAIN, "app.include_router(carts")


def test_pybounds_each_python_bound_is_a_bound_and_a_cycle_is_not(corpora: Corpora) -> None:
    chain, _ = diagnosed(corpora, "pybounds", "chain")
    assert files_of(chain) == {"chain/src/r5.py", "chain/src/r6.py"}
    for file in ("chain/src/r5.py", "chain/src/r6.py"):
        deep = at(chain, file)
        assert (deep.category, deep.code) == (BOUND_REACHED, "python-chain-too-deep")
        assert deep.bounds == ("python-mount-depth",)
    assert chain.bound_notes == (
        BoundNote("python-mount-depth", py_routes.MAX_DEPTH, "route", "chain/src/r5.py"),
        BoundNote("python-mount-depth", py_routes.MAX_DEPTH, "route", "chain/src/r6.py"),
    )

    five, _ = diagnosed(corpora, "pybounds", "hops5")
    hops = at(five, "src/routes.py")
    assert (hops.category, hops.code) == (BOUND_REACHED, "python-constant-hops")
    assert hops.bounds == ("python-constant-hops",)
    assert stages(hops) == ("unresolved", "resolved", "not-evaluated")

    cycle, _ = diagnosed(corpora, "pybounds", "cycle")
    looping = at(cycle, "src/routes.py")
    assert (looping.category, looping.code) == (PATH_UNRESOLVED, "python-path-constant-unresolved")
    assert looping.bounds == () and cycle.bound_notes == ()

    four, _ = diagnosed(corpora, "pybounds", "hops4")
    assert four.entries == ()


#: path → (category, code) of the declared routes of ``neg`` that a scoped refusal explains.
NEG_SPECIFIC = {
    "src/n03.ts": (REGISTRATION_UNPROVEN, "fastify-helper-control-flow"),
    "src/n16.ts": (REGISTRATION_UNPROVEN, "fastify-helper-control-flow"),
    "src/n05.ts": (REGISTRATION_UNPROVEN, "fastify-helper-argument-position"),
    "src/n17.ts": (REGISTRATION_UNPROVEN, "fastify-helper-argument-unsupported"),
    "src/n08.ts": (IDENTITY_UNRESOLVED, "fastify-receiver-before-binding"),
    "src/n15.ts": (FRAMEWORK_CONSTRAINT, "fastify-after-listen"),
    "src/n12.ts": (FRAMEWORK_CONSTRAINT, "fastify-factory-not-awaited"),
    "src/n01.ts": (IDENTITY_UNRESOLVED, "fastify-factory-unproven"),
    "src/n02.ts": (IDENTITY_UNRESOLVED, "fastify-factory-unproven"),
    "src/n19.ts": (REGISTRATION_UNPROVEN, "fastify-entry-not-called"),
    # new rows: a traced route nothing proves
    "src/n18.ts": (REGISTRATION_UNPROVEN, "fastify-factory-not-instantiated"),
    "src/n21.ts": (IDENTITY_UNRESOLVED, "fastify-factory-unproven"),
}
#: Refusals no helper call of the route's own scope explains: the generic code only.
NEG_GENERIC = (
    "src/n04.ts",
    "src/n06.ts",
    "src/n11.ts",
    "src/real10.ts",
    "src/decoy10.ts",
)


def test_neg_each_refusal_gives_its_route_the_code_the_prediction_names(corpora: Corpora) -> None:
    result, _ = diagnosed(corpora, "neg", "neg")
    for file, (category, code) in NEG_SPECIFIC.items():
        entry = at(result, file)
        assert (entry.category, entry.code) == (category, code), file
        assert entry.bounds == (), file
    for file in NEG_GENERIC:
        entry = at(result, file)
        assert (entry.category, entry.code) == (REGISTRATION_UNPROVEN, "fastify-not-registered")
        assert stages(entry) == ("resolved", "resolved", "unproven"), file


def test_neg_which_files_have_an_entry_and_which_have_none(corpora: Corpora) -> None:
    result, _ = diagnosed(corpora, "neg", "neg")
    # 13 + 2 declared in their files, the two chain13 routes, and the two traced ones
    assert files_of(result) == {*NEG_SPECIFIC, *NEG_GENERIC, "src/chain13.ts"}
    assert len(result.entries) == len(NEG_SPECIFIC) + len(NEG_GENERIC) + 2
    # A1: n07 records no route claim, and n09 and n20 record none either
    assert not files_of(result) & {"src/n07.ts", "src/n09.ts", "src/n20.ts"}
    assert result.bound_notes == ()
    # a traced route nothing proves is a new row; the others were declared already
    assert at(result, "src/n18.ts").fact == "fastify-traced-route"
    assert at(result, "src/n18.ts").route == "GET /n18"
    assert at(result, "src/n21.ts").route == "GET /n21"


def test_neg_a_chain_cut_by_a_bound_says_so_before_anything_else(corpora: Corpora) -> None:
    result, _ = diagnosed(corpora, "neg", "neg")
    deep = [e for e in result.entries if e.file == "src/chain13.ts"]
    assert sorted(e.route for e in deep) == ["GET /d5", "GET /d6"]
    for entry in deep:
        assert (entry.category, entry.code) == (BOUND_REACHED, "fastify-chain-too-deep")
        assert stages(entry) == ("resolved", "resolved", "unproven")


def test_fan_a_walk_too_complex_is_bound_reached(corpora: Corpora) -> None:
    result, _ = diagnosed(corpora, "fan", "fan")
    leaf = at(result, "src/fan.ts")
    assert (leaf.category, leaf.code) == (BOUND_REACHED, "fastify-chain-too-complex")
    assert leaf.route == "GET /leaf"


def test_fcap_a_file_at_the_traced_route_cap_has_exactly_one_note(corpora: Corpora) -> None:
    result, _ = diagnosed(corpora, "fastify", "fcap")
    assert result.entries == ()
    assert result.bound_notes == (
        BoundNote(BOUND_TRACED_ROUTES, http_fastify._MAX_TRACED_ROUTES, "file", "src/big.ts"),
    )
    assert result.bound_notes_omitted == 0


def test_fhelp_a_plugin_nothing_registers_beside_the_helper_cap_is_bound_reached(
    corpora: Corpora,
) -> None:
    result, _ = diagnosed(corpora, "fastify", "fhelp")
    orphan = at(result, "src/app.ts", "GET /o")
    assert (orphan.category, orphan.code) == (BOUND_REACHED, "fastify-not-registered")
    assert orphan.bounds == (BOUND_HELPER_CALLS,)
    assert result.bound_notes == (
        BoundNote(BOUND_HELPER_CALLS, http_fastify._MAX_HELPER_CALLS, "file", "src/app.ts"),
    )
    control, _ = diagnosed(corpora, "fastify", "fhelp255")
    unbounded = at(control, "src/app.ts", "GET /o")
    assert (unbounded.category, unbounded.code) == (REGISTRATION_UNPROVEN, "fastify-not-registered")
    assert unbounded.bounds == () and control.bound_notes == ()


def test_ftblk_both_unreadable_registrations_read_the_same(corpora: Corpora) -> None:
    """A1: a wrapper at the call with a prefix is stored exactly like unreadable options."""
    result, _ = diagnosed(corpora, "fastify", "ftblk")
    assert len(result.entries) == 2
    for entry in result.entries:
        assert (entry.category, entry.code) == (
            REGISTRATION_UNPROVEN,
            "fastify-registration-unreadable",
        )
        assert stages(entry) == ("resolved", "resolved", "unproven")
        [place] = entry.related
        assert (place.path, place.role) == ("src/app.ts", "registration")


def test_nest_a_router_module_withholds_and_a_repository_with_no_application_says_so(
    corpora: Corpora,
) -> None:
    withheld, _ = diagnosed(corpora, "nest", "nestprov")
    router = at(withheld, "src/items.controller.ts")
    assert (router.category, router.code) == (FRAMEWORK_CONSTRAINT, "nestjs-router-module")
    assert router.fact == NESTJS_ROUTE and router.framework == "nestjs"
    assert stages(router) == ("resolved", "resolved", "unproven")

    bare, _ = diagnosed(corpora, "nest", "nestbare")
    none = at(bare, "src/bare.controller.ts")
    assert (none.category, none.code) == (REGISTRATION_UNPROVEN, "nestjs-no-application")

    for served in ("nestok", "nestcomposed"):
        assert diagnosed(corpora, "nest", served)[0].entries == ()


def test_oasprov_two_applications_say_why_they_serve_nothing(corpora: Corpora) -> None:
    result, _ = diagnosed(corpora, "oas", "oasprov")
    assert files_of(result) == {"src/a.ts", "src/b.ts"}
    relative = at(result, "src/a.ts")
    assert (relative.category, relative.code) == (PATH_UNRESOLVED, "spec-cwd-relative")
    assert relative.subject == "application" and relative.fact == OAS3_APP
    assert relative.method is None and relative.route == ""
    assert relative.framework == "oas3-tools"
    unresolved = at(result, "src/b.ts")
    assert unresolved.category == IDENTITY_UNRESOLVED
    assert unresolved.code in {"oas3-controllers-unresolved", "oas3-controllers-unread"}
    assert unresolved.subject == "application"


# -- determinism and caps ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("corpus", "name"), [("neg", "neg"), ("py", "pyprov"), ("nest", "nestprov")]
)
def test_the_same_inputs_give_the_same_answer(corpora: Corpora, corpus: str, name: str) -> None:
    first, planned = diagnosed(corpora, corpus, name)
    again = diagnose(planned.plan, planned.claims, planned.files)
    reversed_inputs = diagnose(planned.plan, list(reversed(planned.claims)), planned.files)
    assert again == first
    assert reversed_inputs == first


def test_bound_notes_are_capped_and_the_cut_is_counted(
    corpora: Corpora, monkeypatch: pytest.MonkeyPatch
) -> None:
    whole, planned = diagnosed(corpora, "pybounds", "chain")
    assert len(whole.bound_notes) == 2 and whole.bound_notes_omitted == 0
    monkeypatch.setattr(diagnostics, "MAX_BOUND_NOTES", 1)
    cut = diagnose(planned.plan, planned.claims, planned.files)
    assert cut.bound_notes == whole.bound_notes[:1]
    assert cut.bound_notes_omitted == 1
    assert cut.entries == whole.entries


# -- join limits ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("eligible", "line_start", "declared_at", "structured", "limits"),
    [
        (True, 3, 3, False, ()),
        (True, None, 3, False, ()),
        (True, 3, None, False, ()),
        (True, None, None, False, ("no-declaration-line",)),
        (False, None, None, False, ()),
        (False, None, None, True, ("openapi-document",)),
        (True, None, None, True, ("no-declaration-line", "openapi-document")),
    ],
)
def test_join_limits_name_why_a_served_row_cannot_be_joined(
    eligible: bool, line_start: int | None, declared_at: int | None, structured: bool, limits: Any
) -> None:
    assert (
        join_limits(
            eligible=eligible,
            line_start=line_start,
            declared_at=declared_at,
            structured=structured,
        )
        == limits
    )


# -- the global pass never imports it ---------------------------------------------------------

GLOBAL_PASS = (
    "http_crossings.py",
    "link_exposures.py",
    "deploy_identity.py",
    "deploy_artifacts.py",
    "linker.py",
    "http_claims.py",
    "py_routes.py",
    "ts_routes.py",
    "ts_oas3.py",
)


@pytest.mark.parametrize("name", GLOBAL_PASS)
def test_the_global_link_pass_never_imports_the_diagnostics(name: str) -> None:
    source = (Path(graph_package.__file__).parent / name).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)
    assert not [name for name in imported if "http_provider_diagnostics" in name]


def test_the_module_imports_no_io_or_capability_layer() -> None:
    source = Path(inspect.getsourcefile(diagnostics) or "").read_text(encoding="utf-8")
    modules = {
        node.module or ""
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom)
    }
    forbidden = ("repoatlas.capabilities", "repoatlas.storage.sqlite", "repoatlas.services")
    assert not [m for m in modules if m.startswith(forbidden)]
    assert "repoatlas.graph.linker" not in modules


def test_the_public_names_are_the_ones_g3_reads() -> None:
    assert set(dataclasses.fields(RouteDiagnostic)[i].name for i in range(3)) == {
        "claim_id",
        "subject",
        "fact",
    }
    assert DiagnosticPlace("p", 1, 2, "mount").role == "mount"
