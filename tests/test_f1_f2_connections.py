"""F2: one definition of "served routes" per operation, and what it changes for a NestJS service.

A declaration's state and the candidate services of a group used to read different sets of
routes, neither of them the set the join matches: the state read per-file edges only
(``list_served_routes``), so a repository serving only routes the global pass promoted (every
NestJS route) read as serving none, and the candidates were shortlisted from the same set. Now
both read ``list_code_served_routes``: every code producer's ``SERVES_HTTP_ROUTE`` edges, the
global pass's included, an OpenAPI document's left out; the candidates keep the routes that state a
line or that the global pass wrote (``offerable``). ``list_served_routes`` and the matching are
unchanged. The line of a NestJS route (F1) is pinned in ``test_f1_nestjs_lines.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.capabilities.errors import InvalidDeploymentLinkError
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.graph.linker import GLOBAL_PARSER, repository_routes
from repoatlas.models.deployment_targets import (
    DeclarationProvenance,
    DeploymentLinkFields,
    DeploymentLinkUpdateRequest,
)
from repoatlas.models.http_connections import (
    CandidateService,
    DeclarationView,
    HttpConnectionsRequest,
)
from repoatlas.parsers.structured import StructuredParser
from repoatlas.services.workspace import Workspace
from repoatlas.storage.sqlite import stores
from tests.f1_f2_corpus import (
    API_ORIGIN,
    DOCCLIENT,
    DOCPROV,
    DOCS_ORIGIN,
    DUPAPI,
    GET_LINE,
    NESTAPI,
    PING_ORIGIN,
    USERS_CONTROLLER,
    WEBCLIENT,
    d,
    open_built,
    ping_repos,
    python_repo,
)
from tests.impact_corpus import build
from tests.incremental_corpus import assert_equals_fresh, edit, sync_all
from tests.r1_provider_corpus import (
    GLOBAL,
    PYPROV2,
    caller_decisions,
    declare_plain,
    group_key,
    preview,
    repository_context,
    slug_of,
)

API_REPOS = {"nestapi": NESTAPI, "webclient": WEBCLIENT}
ORDERS_ORIGIN = "https://orders.example.test"
MISS_ORIGIN = "https://miss.example.test"

#: ``pyprov2`` mounts its router from another module, so it has no per-file route: only the global
#: pass serves ``GET /orders/{oid}``. ``ordersclient`` requests it; ``missclient`` requests a path
#: it does not serve, at an origin nothing else states.
ORDERSCLIENT = python_repo(
    "ordersclient",
    "requests",
    {
        "svc/client.py": d(f"""
            import requests


            def fetch_order() -> dict:
                return requests.get("{ORDERS_ORIGIN}/orders/42", timeout=5).json()
        """)
    },
)
MISSCLIENT = python_repo(
    "missclient",
    "requests",
    {
        "svc/client.py": d(f"""
            import requests


            def fetch_nothing() -> dict:
                return requests.get("{MISS_ORIGIN}/nothing/here", timeout=5).json()
        """)
    },
)
GET_USERS = "GET /api/v1/users"
POST_USERS = "POST /api/v1/users"


def repo_id(workspace: Workspace, name: str) -> int:
    found = next(r.id for r in workspace.repositories.list_all() if r.name == name)
    assert found is not None
    return found


def declarations(workspace: Workspace) -> tuple[DeclarationView, ...]:
    listed = CapabilityService(workspace).http_connections(HttpConnectionsRequest(context=GLOBAL))
    return tuple(listed.declarations)


def candidates(workspace: Workspace, origin: str) -> tuple[CandidateService, ...]:
    request = HttpConnectionsRequest(context=GLOBAL, group=group_key(workspace, origin))
    return CapabilityService(workspace).http_connections(request).candidates


@pytest.fixture(scope="module")
def api(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Workspace]:
    workspace = open_built(tmp_path_factory.mktemp("f2-api"), "api", API_REPOS)
    yield workspace
    workspace.close()


@pytest.fixture(scope="module")
def duplicated(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Workspace]:
    workspace = open_built(
        tmp_path_factory.mktemp("f2-dup"), "dup", {"dupapi": DUPAPI, "webclient": WEBCLIENT}
    )
    yield workspace
    workspace.close()


@pytest.fixture(scope="module")
def orders(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Workspace]:
    workspace = open_built(
        tmp_path_factory.mktemp("f2-orders"),
        "orders",
        {"pyprov2": PYPROV2, "ordersclient": ORDERSCLIENT},
    )
    yield workspace
    workspace.close()


@pytest.fixture(scope="module")
def ping(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Workspace]:
    workspace = open_built(tmp_path_factory.mktemp("f2-ping"), "ping", ping_repos())
    yield workspace
    workspace.close()


# -- the store: one definition of "served", two readings of it ---------------------------------


def test_the_store_parser_spellings_are_the_producers_own() -> None:
    """The store cannot import the linker or the parser; it spells their names. This pins both."""
    assert stores._GLOBAL_PARSER == GLOBAL_PARSER
    assert StructuredParser.name == stores._STRUCTURED_PARSER


def test_code_served_routes_include_the_global_passs_routes_that_list_served_routes_omits(
    api: Workspace,
) -> None:
    """M6: ``list_served_routes`` still leaves the global pass's own edges out, so what the pass
    decides never depends on its last output; the code-served reading includes them (M22)."""
    nestapi = repo_id(api, "nestapi")
    assert api.knowledge.list_served_routes(exclude_parser=GLOBAL_PARSER) == {}
    expected = {nestapi: sorted([GET_USERS, POST_USERS])}
    assert api.knowledge.list_code_served_routes(offerable=False) == expected
    assert api.knowledge.list_code_served_routes(offerable=True) == expected
    assert api.knowledge.list_code_served_routes(offerable=False, repo_id=nestapi) == expected
    assert api.knowledge.list_code_served_routes(offerable=False, repo_id=nestapi + 100) == {}


def test_offerable_drops_a_per_file_route_with_no_line_and_keeps_one_with_a_line(
    ping: Workspace,
) -> None:
    """M3: ``/v1/ping`` through a prefixed router in its own file (D1) is served, stored without
    lines, and is not offered; the same route stated directly is both."""
    served = ping.knowledge.list_code_served_routes(offerable=False)
    offered = ping.knowledge.list_code_served_routes(offerable=True)
    service = repo_id(ping, "zservice")
    decoy = repo_id(ping, "adecoy00")
    assert served[decoy] == ["GET /v1/ping"] and served[service] == ["GET /v1/ping"]
    assert decoy not in offered
    assert offered[service] == ["GET /v1/ping"]


def test_a_specification_document_is_no_application_for_either_reading(tmp_path: Path) -> None:
    """M7: a document's edges are in the table (parser ``structured-config``), and neither reading
    counts them."""
    workspace = open_built(tmp_path, "docs", {"docprov": DOCPROV, "docclient": DOCCLIENT})
    try:
        document = repo_id(workspace, "docprov")
        [(count,)] = workspace.database.execute(
            "SELECT COUNT(*) FROM edges WHERE type = 'SERVES_HTTP_ROUTE' AND parser = ? "
            "AND repo_id = ?",
            (StructuredParser.name, document),
        ).fetchall()
        assert count >= 1
        assert workspace.knowledge.list_code_served_routes(offerable=False) == {}
        assert workspace.knowledge.list_code_served_routes(offerable=True) == {}
        assert candidates(workspace, DOCS_ORIGIN) == ()
    finally:
        workspace.close()


# -- the declaration state and the candidates read the same routes -------------------------------


def test_a_service_serving_only_promoted_routes_is_a_candidate_with_a_unique_request(
    api: Workspace,
) -> None:
    """F2 for the candidates: a group's shortlist reads the global pass's routes too."""
    [candidate] = candidates(api, API_ORIGIN)
    assert candidate.repository == slug_of(api, "nestapi")
    assert candidate.routes_served == 2
    assert (candidate.requests_unique, candidate.requests_ambiguous) == (1, 0)
    assert candidate.suggested_base_path == ""


def test_a_request_a_composed_route_answers_would_link_to_the_handler_line(
    api: Workspace,
) -> None:
    """F1 through the preview: the composed route states its decorator line."""
    result = preview(api, API_ORIGIN, "nestapi")
    assert result.routes_served == 2
    assert result.counts == {"would-link": 1}
    [row] = result.rows
    assert row.handler == f"src/users.controller.ts:{GET_LINE}"
    assert result.can_create is True and result.requirements == ()


def test_a_declaration_of_a_service_serving_only_promoted_routes_is_applied_and_joins(
    workspace: Workspace, tmp_path: Path
) -> None:
    """M22: the state reads the global pass's routes, so the declaration is applied, not
    ``invalidated``/``repository-serves-no-routes``, and the request is linked through it."""
    build(workspace, tmp_path, API_REPOS)
    declare_plain(workspace, "nestapi", API_ORIGIN)
    sync_all(workspace, tmp_path / "repos")
    [declaration] = declarations(workspace)
    assert declaration.repository == slug_of(workspace, "nestapi")
    assert (declaration.state, declaration.reason, declaration.joined) == ("applied", "", 1)
    assert caller_decisions(workspace)["fetch_users"][0] == "linked"


def test_an_unjoined_declaration_of_a_service_serving_only_promoted_routes_is_ineffective_not_invalidated(  # noqa: E501
    workspace: Workspace, tmp_path: Path
) -> None:
    """M22: ``pyprov2`` serves ``GET /orders/{oid}`` only through the global pass. A declaration of
    it at an origin whose request it does not answer is ``ineffective``/``route-unmatched``: the
    service does serve routes. Without the global pass's routes it read as
    ``invalidated``/``repository-serves-no-routes``."""
    build(workspace, tmp_path, {"pyprov2": PYPROV2, "missclient": MISSCLIENT})
    declare_plain(workspace, "pyprov2", MISS_ORIGIN)
    sync_all(workspace, tmp_path / "repos")
    [declaration] = declarations(workspace)
    assert (declaration.state, declaration.reason) == ("ineffective", "route-unmatched")
    assert declaration.joined == 0
    assert declaration.declined == {"route-unmatched": 1}
    # the gap the declaration state used to fall into
    provider = repo_id(workspace, "pyprov2")
    assert provider not in workspace.knowledge.list_served_routes(exclude_parser=GLOBAL_PARSER)
    served = workspace.knowledge.list_code_served_routes(offerable=False)
    assert served[provider] == ["GET /orders/{oid}"]


def test_a_service_whose_only_routes_are_promoted_is_a_candidate_for_a_matching_request(
    orders: Workspace,
) -> None:
    """M22 for the candidates: the group's request matches ``GET /orders/{oid}``."""
    [candidate] = candidates(orders, ORDERS_ORIGIN)
    assert candidate.repository == slug_of(orders, "pyprov2")
    assert candidate.requests_unique == 1


def test_a_declaration_made_from_the_worklist_is_refused_an_update_once_the_code_drifts(
    workspace: Workspace, tmp_path: Path
) -> None:
    """M16: a declaration made from a worklist group applies and joins. When the controller's path
    then changes (``v1`` to ``v2``) the sync no longer joins the request, and an update that names
    the group is checked against the routes the sync reads now, so it is refused."""
    build(workspace, tmp_path, API_REPOS)
    root = tmp_path / "repos"
    group = group_key(workspace, API_ORIGIN)
    link_id = declare_plain(workspace, "nestapi", API_ORIGIN, group=group)
    sync_all(workspace, root)
    [declaration] = declarations(workspace)
    assert (declaration.state, declaration.joined) == ("applied", 1)

    edit(
        root,
        "nestapi",
        "src/users.controller.ts",
        USERS_CONTROLLER.replace('"v1/users"', '"v2/users"'),
    )
    sync_all(workspace, root)
    assert caller_decisions(workspace)["fetch_users"][0] == "http-service-unmatched"
    [drifted] = declarations(workspace)
    # The service still serves routes and the request is still made at the declared origin, but
    # none matches any more: expected "ineffective" (not "invalidated"); the first run records it.
    assert drifted.state == "ineffective"

    with pytest.raises(InvalidDeploymentLinkError):
        CapabilityService(workspace).deployment_link_update(
            DeploymentLinkUpdateRequest(
                context=repository_context(workspace, "nestapi"),
                id=link_id,
                link=DeploymentLinkFields(label="again", url=API_ORIGIN, callers=[]),
                provenance=DeclarationProvenance(via="ui", request_group=group),
            )
        )


def test_a_declaration_of_a_document_only_repository_serves_no_routes(
    workspace: Workspace, tmp_path: Path
) -> None:
    """M7: ``docprov`` holds an OpenAPI document and no application; a declaration of it reads as
    serving no routes (it used to read as ``ineffective``/``no-request-matched``)."""
    build(workspace, tmp_path, {"docprov": DOCPROV, "docclient": DOCCLIENT})
    declare_plain(workspace, "docprov", DOCS_ORIGIN)
    sync_all(workspace, tmp_path / "repos")
    [declaration] = declarations(workspace)
    assert (declaration.state, declaration.reason) == ("invalidated", "repository-serves-no-routes")


def test_two_handlers_for_one_route_make_the_request_ambiguous_not_unique(
    duplicated: Workspace,
) -> None:
    """M13: ``list`` and ``listAgain`` both serve ``GET /api/v1/users``, each with its own line."""
    result = preview(duplicated, API_ORIGIN, "dupapi")
    assert result.counts == {"ambiguous": 1}
    [candidate] = candidates(duplicated, API_ORIGIN)
    assert (candidate.requests_unique, candidate.requests_ambiguous) == (0, 1)


# -- the candidates are shortlisted from what can be offered ------------------------------------


def test_services_that_can_only_be_served_without_a_line_do_not_fill_the_shortlist(
    ping: Workspace,
) -> None:
    """M21: thirteen repositories serve ``/v1/ping`` with no line and sort before the one that
    states it; the shortlist holds twelve. Read from what is served they would push the real
    service out; read from what can be offered it is the only candidate."""
    [candidate] = candidates(ping, PING_ORIGIN)
    assert candidate.repository == slug_of(ping, "zservice")
    assert (candidate.requests_unique, candidate.requests_ambiguous) == (1, 0)


def test_a_prefixed_router_in_its_own_file_still_joins_nothing(ping: Workspace) -> None:
    """D1 stays as it is: served, stored without lines, and not joined by a preview."""
    result = preview(ping, PING_ORIGIN, "adecoy00")
    assert result.routes_served == 1
    assert result.counts == {"no-matching-route": 1}
    assert result.can_create is False


# -- an incremental sync is a fresh index -------------------------------------------------------


def routes_of(workspace: Workspace, name: str) -> set[str]:
    return {r.body for r in repository_routes(workspace.knowledge, repo_id(workspace, name))}


def test_changing_a_controller_prefix_moves_the_routes_and_equals_a_fresh_index(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    """M8."""
    build(workspace, tmp_path, API_REPOS)
    root = tmp_path / "repos"
    edit(
        root,
        "nestapi",
        "src/users.controller.ts",
        USERS_CONTROLLER.replace('"v1/users"', '"v2/users"'),
    )
    sync_all(workspace, root)
    assert routes_of(workspace, "nestapi") == {"GET /api/v2/users", "POST /api/v2/users"}
    assert_equals_fresh(workspace, settings, root, tmp_path, "controller-prefix")


def test_changing_the_global_prefix_moves_the_routes_and_equals_a_fresh_index(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    """M9."""
    build(workspace, tmp_path, API_REPOS)
    root = tmp_path / "repos"
    main = NESTAPI["src/main.ts"]
    edit(root, "nestapi", "src/main.ts", main.replace('"api"', '"rest"'))
    sync_all(workspace, root)
    assert routes_of(workspace, "nestapi") == {"GET /rest/v1/users", "POST /rest/v1/users"}
    assert_equals_fresh(workspace, settings, root, tmp_path, "global-prefix")


def test_deleting_a_route_drops_it_and_equals_a_fresh_index(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    """M10: the declaration line of the route that is gone goes with it."""
    build(workspace, tmp_path, API_REPOS)
    root = tmp_path / "repos"
    edit(
        root,
        "nestapi",
        "src/users.controller.ts",
        USERS_CONTROLLER.replace("  @Post()\n  create() {}\n", ""),
    )
    sync_all(workspace, root)
    assert routes_of(workspace, "nestapi") == {GET_USERS}
    assert_equals_fresh(workspace, settings, root, tmp_path, "route-deleted")
