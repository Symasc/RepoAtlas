"""R1 gate G1: the route planners record what they met, beside results they leave unchanged.

``PythonRoutePlan``, ``FastifyRoutePlan`` and ``HttpClaimPlan`` gain diagnostic tables keyed by
route claim id: ``route_codes`` (the cause the planner itself can name for a route served
nowhere), ``resolved_routes`` (the body an unserved, undeclared path evaluates to),
``route_bounds`` and ``related`` (the claims that caused a code), and the sub-plan tables that
were never copied before (``traced_outcomes``, ``class_unproven``, ``spec_refusals``,
``chain_refusals``). Nothing here is read by a decision, an edge or the global pass.

A route with no cause the planner can establish has no ``route_codes`` entry: the generic reason
is supplied by the classification (gate G2), as is the precedence between a chain refusal, a
blocked registration and an attribution. The repository-wide helper-call cap is G2's too.

The fixtures are those of ``tests/r1_provider_corpus.py``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from repoatlas.graph.linker import RepositoryHttp, claim_role, plan_repository_http
from repoatlas.graph.py_modules import PythonModules
from repoatlas.graph.py_routes import (
    CODE_CHAIN_TOO_DEEP,
    CODE_CONSTANT_HOPS,
    plan_python_routes,
)
from repoatlas.graph.ts_oas3 import plan_spec_routed
from repoatlas.graph.ts_routes import (
    BOUND_CONSTANTS_PER_MODULE,
    CHAIN_TOO_DEEP,
    CONSTANT_UNRESOLVED,
    FACTORY_NOT_INSTANTIATED,
    HELPER_ARGUMENT_POSITION,
    PLUGIN_PREFIX_IGNORED,
    REGISTRATION_UNREADABLE,
    plan_fastify_routes,
)
from repoatlas.parsers.http_facts import (
    FASTIFY_REGISTER,
    FASTIFY_ROUTE,
    FASTIFY_TRACED_ROUTE,
    PYTHON_ROUTE,
)
from repoatlas.parsers.http_fastify import (
    AFTER_LISTEN,
    ENTRY_NOT_CALLED,
    FACTORY_NOT_AWAITED,
    FACTORY_UNPROVEN,
    HELPER_ARGUMENT_UNSUPPORTED,
    HELPER_CONTROL_FLOW,
    HELPER_SHADOWED,
    RECEIVER_BEFORE_BINDING,
)
from repoatlas.services.workspace import Workspace
from repoatlas.storage.protocols import UnresolvedRefRecord
from tests.impact_corpus import build
from tests.r1_provider_corpus import PY_MAIN, Corpora

FASTIFY_ROUTES = (FASTIFY_ROUTE, FASTIFY_TRACED_ROUTE)


@pytest.fixture(scope="module")
def corpora(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Corpora]:
    built = Corpora(tmp_path_factory.mktemp("r1-planner-facts"))
    yield built
    built.close()


def planned_of(workspace: Workspace, name: str) -> RepositoryHttp:
    repository = next(r for r in workspace.repositories.list_all() if r.name == name)
    assert repository.id is not None
    return plan_repository_http(workspace.knowledge, repository.id)


def routes_at(planned: RepositoryHttp, path: str, *roles: str) -> list[UnresolvedRefRecord]:
    files = {row.file_id: row.rel_path for row in planned.files}
    return [
        claim
        for claim in planned.claims
        if files[claim.file_id] == path and claim_role(claim) in roles
    ]


def single(planned: RepositoryHttp, path: str, *roles: str) -> int:
    """The id of the one route claim of the given roles in a file."""
    found = routes_at(planned, path, *roles)
    assert len(found) == 1, (path, [claim.target_key for claim in found])
    claim_id = found[0].id
    assert claim_id is not None
    return claim_id


def line_of(text: str, needle: str) -> int:
    return next(number for number, row in enumerate(text.splitlines(), 1) if needle in row)


# -- Python: pyprov ---------------------------------------------------------------------------


def test_a_path_built_from_constants_that_is_served_nowhere_records_the_route_it_evaluates_to(
    corpora: Corpora,
) -> None:
    planned = planned_of(corpora.workspace("py"), "pyprov")
    plan = planned.plan
    audit = single(planned, "svc/audit.py", PYTHON_ROUTE)
    reports = single(planned, "svc/reports.py", PYTHON_ROUTE)
    assert plan.resolved_routes[audit] == "GET /audit"
    assert reports not in plan.resolved_routes  # f"{PREFIX}/reports": PREFIX is a call result
    # a literal path is already a fact of the plan (`declared`), and a served route needs none
    orders = single(planned, "svc/orders.py", PYTHON_ROUTE)
    assert orders in plan.declared and orders not in plan.resolved_routes
    assert not set(plan.resolved_routes) & set(plan.promoted)
    # neither of those is a bound or a flag of the planner: no code
    assert not {audit, reports, orders} & set(plan.route_codes)


def test_an_unreadable_mount_records_the_mount_that_made_it_so(corpora: Corpora) -> None:
    planned = planned_of(corpora.workspace("py"), "pyprov")
    carts = single(planned, "svc/carts.py", PYTHON_ROUTE)
    main = next(row.file_id for row in planned.files if row.rel_path == "svc/main.py")
    [(file_id, first, last, role)] = planned.plan.related[carts]
    assert (file_id, first, role) == (main, line_of(PY_MAIN, "app.include_router(carts"), "mount")
    assert first is not None and last is not None and last >= first
    # a route whose router no mount blocks has no such place
    assert single(planned, "svc/orders.py", PYTHON_ROUTE) not in planned.plan.related


def test_a_class_route_nothing_proves_is_passed_through_as_class_unproven(
    corpora: Corpora,
) -> None:
    planned = planned_of(corpora.workspace("py"), "pyprov")
    assert planned.plan.class_unproven
    assert not set(planned.plan.class_unproven) & set(planned.plan.routes)


# -- Python: the bounds -----------------------------------------------------------------------


def test_python_flags_are_recorded_for_a_chain_cut_a_constant_cut_and_a_cycle(
    corpora: Corpora,
) -> None:
    workspace = corpora.workspace("pybounds")
    chain = planned_of(workspace, "chain")
    for deep in ("chain/src/r5.py", "chain/src/r6.py"):
        assert chain.plan.route_codes[single(chain, deep, PYTHON_ROUTE)] == CODE_CHAIN_TOO_DEEP
    last_served = single(chain, "chain/src/r4.py", PYTHON_ROUTE)
    assert last_served in chain.plan.promoted and last_served not in chain.plan.route_codes

    five = planned_of(workspace, "hops5")
    unresolved = single(five, "src/routes.py", PYTHON_ROUTE)
    assert five.plan.route_codes[unresolved] == CODE_CONSTANT_HOPS
    assert unresolved not in five.plan.resolved_routes

    four = planned_of(workspace, "hops4")
    served = single(four, "src/routes.py", PYTHON_ROUTE)
    assert served in four.plan.promoted and served not in four.plan.route_codes

    # a constant that names itself is unresolved, not a bound
    cycle = planned_of(workspace, "cycle")
    looping = single(cycle, "src/routes.py", PYTHON_ROUTE)
    assert looping in cycle.plan.routes and looping not in cycle.plan.promoted
    assert looping not in cycle.plan.route_codes
    assert looping not in cycle.plan.resolved_routes


# -- Fastify: the neg repository --------------------------------------------------------------

#: The refusal each route's own scope owes its helper call or instance.
NEG_CODES = {
    "src/n03.ts": HELPER_CONTROL_FLOW,
    "src/n16.ts": HELPER_CONTROL_FLOW,
    "src/n05.ts": HELPER_ARGUMENT_POSITION,
    "src/n17.ts": HELPER_ARGUMENT_UNSUPPORTED,
    "src/n08.ts": RECEIVER_BEFORE_BINDING,
    "src/n15.ts": AFTER_LISTEN,
    "src/n12.ts": FACTORY_NOT_AWAITED,
    "src/n01.ts": FACTORY_UNPROVEN,
    "src/n02.ts": FACTORY_UNPROVEN,
    "src/n19.ts": ENTRY_NOT_CALLED,
    "src/n18.ts": FACTORY_NOT_INSTANTIATED,
    "src/n21.ts": FACTORY_UNPROVEN,
}
#: Routes whose scope no refusal names: a dynamic, shadowed, unresolved or unbound callee must
#: not be blamed on the route's function (the misattribution guard).
NEG_NONE = (
    "src/n04.ts",
    "src/n06.ts",
    "src/n11.ts",
    "src/real10.ts",
    "src/decoy10.ts",
)


@pytest.mark.parametrize(("path", "code"), sorted(NEG_CODES.items()))
def test_a_route_carries_the_refusal_its_own_scope_owes(
    corpora: Corpora, path: str, code: str
) -> None:
    planned = planned_of(corpora.workspace("neg"), "neg")
    claim = single(planned, path, *FASTIFY_ROUTES)
    assert planned.plan.route_codes[claim] == code
    # never a route that is served
    assert claim not in planned.plan.promoted


@pytest.mark.parametrize("path", NEG_NONE)
def test_a_route_no_refusal_names_has_no_code(corpora: Corpora, path: str) -> None:
    planned = planned_of(corpora.workspace("neg"), "neg")
    claim = single(planned, path, *FASTIFY_ROUTES)
    assert claim not in planned.plan.route_codes
    assert claim not in planned.plan.related


def test_a_shadowed_callee_records_no_route_and_no_shadow_code(corpora: Corpora) -> None:
    """N07: the callee is hidden by a local function of the same name, and the file records no
    route claim at all. A shadowed callee is never blamed on a function: it is not that one."""
    planned = planned_of(corpora.workspace("neg"), "neg")
    assert routes_at(planned, "src/n07.ts", *FASTIFY_ROUTES) == []
    assert HELPER_SHADOWED not in planned.plan.route_codes.values()


def test_the_claims_that_caused_a_code_are_recorded_with_their_role(corpora: Corpora) -> None:
    planned = planned_of(corpora.workspace("neg"), "neg")
    by_path = {row.file_id: row.rel_path for row in planned.files}

    def roles(path: str) -> set[tuple[str, str]]:
        related = planned.plan.related[single(planned, path, *FASTIFY_ROUTES)]
        return {(by_path[place[0]], place[3]) for place in related}

    assert roles("src/n03.ts") == {("src/n03.ts", "helper-call")}
    assert roles("src/n01.ts") == {("src/n01.ts", "instance")}
    assert roles("src/n21.ts") == {("src/n21.ts", "instance")}


def test_a_chain_refusal_is_passed_through_and_the_cap_bound_is_not_recorded_here(
    corpora: Corpora,
) -> None:
    neg = planned_of(corpora.workspace("neg"), "neg")
    refused = {
        claim.target_key: neg.plan.chain_refusals[claim.id]
        for claim in routes_at(neg, "src/chain13.ts", *FASTIFY_ROUTES)
        if claim.id in neg.plan.chain_refusals
    }
    assert refused == {"GET /d5": CHAIN_TOO_DEEP, "GET /d6": CHAIN_TOO_DEEP}
    # the repository-wide helper-call cap is the classification's to report: no code, no bound
    fastify = corpora.workspace("fastify")
    for name in ("fhelp", "fhelp255"):
        planned = planned_of(fastify, name)
        orphan = next(
            claim.id
            for claim in planned.claims
            if claim_role(claim) in FASTIFY_ROUTES and claim.target_key == "GET /o"
        )
        assert orphan not in planned.plan.route_codes
        assert orphan not in planned.plan.route_bounds


# -- Fastify: blocked registrations ------------------------------------------------------------


def test_a_blocked_registration_records_why_it_is_blocked(corpora: Corpora) -> None:
    planned = planned_of(corpora.workspace("fastify"), "ftblk")
    claims = {
        claim.target_key: claim.id
        for claim in routes_at(planned, "src/app.ts", *FASTIFY_ROUTES)
        if claim.id is not None
    }
    # Amendment A1 (2026-11-04): a wrapper at the call with a prefix is stored as unreadable
    # (readable="false", prefix null) exactly like unreadable options, so both read the same.
    assert planned.plan.route_codes[claims["GET /pa"]] == REGISTRATION_UNREADABLE
    assert planned.plan.route_codes[claims["GET /pb"]] == REGISTRATION_UNREADABLE
    for claim in claims.values():
        [(_, _, _, role)] = planned.plan.related[claim]
        assert role == "registration"


def test_a_plugin_wrapped_where_it_is_declared_and_given_a_prefix_has_its_prefix_ignored(
    workspace: Workspace, tmp_path: Path
) -> None:
    """Amendment A1 (2026-11-04): the one case the planner can name as a prefix Fastify ignores:
    the wrapper is at the plugin's declaration, so the registration itself reads fine."""
    build(
        workspace,
        tmp_path,
        {
            "wrapped": {
                "package.json": '{"name": "wrapped"}\n',
                "src/plugins/c.ts": (
                    "import fp from 'fastify-plugin';\n\n"
                    "export const plugC = fp(async (app) => {\n"
                    "  app.get('/pc', h);\n"
                    "});\n"
                ),
                "src/app.ts": (
                    "import Fastify from 'fastify';\n"
                    "import { plugC } from './plugins/c';\n\n"
                    "const app = Fastify();\n"
                    "app.register(plugC, { prefix: '/c' });\n"
                ),
            }
        },
    )
    planned = planned_of(workspace, "wrapped")
    claim = single(planned, "src/plugins/c.ts", *FASTIFY_ROUTES)
    registrations = [
        (c.target_key, c.meta_json) for c in planned.claims if claim_role(c) == FASTIFY_REGISTER
    ]
    assert planned.plan.route_codes.get(claim) == PLUGIN_PREFIX_IGNORED, registrations
    [(_, _, _, role)] = planned.plan.related[claim]
    assert role == "registration"


# -- Fastify: a constants table at its cap -----------------------------------------------------


def _constants_repo(count: int) -> dict[str, dict[str, str]]:
    table = "".join(f"export const C{index} = '/c{index}';\n" for index in range(count))
    return {
        "capped": {
            "package.json": '{"name": "capped"}\n',
            "src/consts.ts": table,
            "src/odd.ts": (
                "import { FastifyInstance } from 'fastify';\n"
                "import { NOPE } from './consts';\n\n"
                "export default async function odd(server: FastifyInstance) {\n"
                "  server.get(`/${NOPE}`, async () => 'odd');\n}\n"
            ),
        }
    }


@pytest.mark.parametrize(("count", "bounded"), [(64, True), (63, False)])
def test_a_missing_name_beside_a_full_constants_table_records_the_bound(
    workspace: Workspace, tmp_path: Path, count: int, bounded: bool
) -> None:
    """``http_fastify._MAX_CONSTANTS`` (64) is the only cap on the ``ts-constants`` role."""
    build(workspace, tmp_path, _constants_repo(count))
    planned = planned_of(workspace, "capped")
    claim = single(planned, "src/odd.ts", *FASTIFY_ROUTES)
    assert planned.plan.routes[claim] == CONSTANT_UNRESOLVED
    if bounded:
        assert planned.plan.route_bounds[claim] == (BOUND_CONSTANTS_PER_MODULE,)
    else:
        assert claim not in planned.plan.route_bounds


# -- the combined plan -------------------------------------------------------------------------


def _subplans(workspace: Workspace, planned: RepositoryHttp) -> tuple[Any, Any, Any]:
    grouped: dict[str, list[UnresolvedRefRecord]] = defaultdict(list)
    for claim in planned.claims:
        grouped[claim_role(claim) or ""].append(claim)
    by_id = {row.file_id: row for row in planned.files}
    facts = [fact for fact in workspace.knowledge.list_module_facts() if fact.file_id in by_id]
    python = plan_python_routes(
        grouped, lambda: PythonModules(planned.index, facts), planned.index, by_id
    )
    return (
        python,
        plan_fastify_routes(grouped, planned.index, by_id),
        plan_spec_routed(grouped, planned.index, by_id),
    )


@pytest.mark.parametrize(
    ("corpus", "name"),
    [
        ("neg", "neg"),
        ("py", "pyprov"),
        ("oas", "oasprov"),
        ("fastify", "ftblk"),
        ("pybounds", "chain"),
    ],
)
def test_the_combined_plan_passes_the_sub_plans_tables_through(
    corpora: Corpora, corpus: str, name: str
) -> None:
    workspace = corpora.workspace(corpus)
    planned = planned_of(workspace, name)
    python, fastify, spec = _subplans(workspace, planned)
    plan = planned.plan
    assert plan.traced_outcomes == fastify.traced_outcomes
    assert plan.chain_refusals == fastify.chain_refusals
    assert plan.class_unproven == python.class_unproven
    assert plan.spec_refusals == spec.spec_refusals
    assert plan.route_codes == {**python.route_codes, **fastify.route_codes}
    assert plan.resolved_routes == {**python.resolved_routes, **fastify.resolved_routes}
    assert plan.route_bounds == fastify.route_bounds
    assert plan.related == {**python.related, **fastify.related}


def test_the_passthrough_has_something_to_pass(corpora: Corpora) -> None:
    """A guard on the guard: each table is non-empty somewhere in the corpus."""
    neg = planned_of(corpora.workspace("neg"), "neg").plan
    assert neg.traced_outcomes and neg.chain_refusals and neg.route_codes and neg.related
    assert planned_of(corpora.workspace("py"), "pyprov").plan.class_unproven
    assert planned_of(corpora.workspace("oas"), "oasprov").plan.spec_refusals


@pytest.mark.parametrize("corpus", ["py", "pybounds", "neg", "fastify", "oas", "nest"])
def test_a_diagnostic_never_names_a_route_that_is_served(corpora: Corpora, corpus: str) -> None:
    workspace = corpora.workspace(corpus)
    for repository in workspace.repositories.list_all():
        plan = planned_of(workspace, repository.name).plan
        served = set(plan.promoted)
        assert not served & set(plan.route_codes), repository.name
        assert not served & set(plan.resolved_routes), repository.name
        assert not served & set(plan.route_bounds), repository.name
        assert not served & set(plan.related), repository.name
        if corpus == "nest":
            # NestJS keeps its own reasons; the planner records no code for it
            assert not plan.route_codes, repository.name
