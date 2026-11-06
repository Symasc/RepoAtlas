"""F1: a NestJS route the global pass promotes states its own declaration line.

The route a controller prefix or a global prefix composes is stored as the global pass's
``SERVES_HTTP_ROUTE`` edge with no lines (what the stored edge says is unchanged). Its plan row now
carries ``declared_at``: the line of the method decorator that declares it, so an operator's
declaration can expose it and a preview can cite its handler. A route that is withheld, or that
no application registers, is not promoted and states nothing. NestJS roots stay empty: a route is
tied to no application file.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from repoatlas.config.settings import DECLARATIONS_SLUG
from repoatlas.graph.http_claims import HttpClaimPlan
from repoatlas.graph.linker import plan_repository_http
from repoatlas.models.parsing import EdgeType
from repoatlas.services.workspace import Workspace
from tests.f1_f2_corpus import GET_LINE, NESTAPI, POST_LINE, WEBCLIENT, open_built
from tests.r1_provider_corpus import NESTBARE, NESTOK, NESTPROV, Corpora

REPOS = {
    "nestapi": NESTAPI,
    "webclient": WEBCLIENT,
    "nestok": NESTOK,
    "nestprov": NESTPROV,
    "nestbare": NESTBARE,
}


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Workspace]:
    workspace = open_built(tmp_path_factory.mktemp("f1"), "lines", REPOS)
    yield workspace
    workspace.close()


@pytest.fixture(scope="module")
def corpora(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Corpora]:
    built_corpora = Corpora(tmp_path_factory.mktemp("f1-corpora"))
    yield built_corpora
    built_corpora.close()


def plan_of(workspace: Workspace, name: str) -> HttpClaimPlan:
    repo = next(r for r in workspace.repositories.list_all() if r.name == name)
    assert repo.id is not None
    return plan_repository_http(workspace.knowledge, repo.id).plan


def lines_of(workspace: Workspace, name: str) -> dict[str, tuple[int | None, int | None]]:
    """Each route of a plan's ``code_routes``: ``(line_start, declared_at)``."""
    return {r.body: (r.line_start, r.declared_at) for r in plan_of(workspace, name).code_routes}


def test_a_composed_route_declares_its_own_method_decorator_line(built: Workspace) -> None:
    """M1 (no fallback) and the line itself: each route has the line of its own decorator, not the
    controller's (3) and not the first route's."""
    assert lines_of(built, "nestapi") == {
        "GET /api/v1/users": (None, GET_LINE),
        "POST /api/v1/users": (None, POST_LINE),
    }


def test_a_route_stated_exactly_keeps_its_own_line_and_a_composed_one_declares_it(
    built: Workspace,
) -> None:
    assert lines_of(built, "nestok") == {"GET /health": (5, 5), "GET /shop/items/:id": (None, 5)}


def test_the_stored_edges_of_a_composed_route_still_state_no_line(built: Workspace) -> None:
    rows = built.database.execute(
        """
        SELECT t.name, e.line_start, e.line_end FROM edges e
        JOIN entities t ON t.id = e.dst_id
        WHERE e.type = ? AND e.parser = 'linker:global' AND t.name LIKE '% /api/v1/users'
        ORDER BY t.name
        """,
        (str(EdgeType.SERVES_HTTP_ROUTE),),
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("GET /api/v1/users", None, None),
        ("POST /api/v1/users", None, None),
    ]


@pytest.mark.parametrize("name", ["nestprov", "nestbare"])
def test_a_withheld_or_unregistered_route_states_no_line_and_is_offered_by_nothing(
    built: Workspace, name: str
) -> None:
    """M2: ``nestprov`` imports ``RouterModule`` (its routes are withheld) and ``nestbare`` has no
    application. Neither promotes a route, so neither has a line to state or an offer to make."""
    assert plan_of(built, name).code_routes == ()
    repo = next(r.id for r in built.repositories.list_all() if r.name == name)
    assert repo not in built.knowledge.list_code_served_routes(offerable=False)
    assert repo not in built.knowledge.list_code_served_routes(offerable=True)


def test_nestjs_routes_are_tied_to_no_application_file(built: Workspace) -> None:
    """M17: roots stay empty, so a deployment identity's tie to one application still fails for a
    repository whose routes are NestJS."""
    for name in ("nestapi", "nestok"):
        routes = plan_of(built, name).code_routes
        assert routes
        assert all(route.roots == frozenset() for route in routes), name


@pytest.mark.parametrize(
    ("corpus", "promoted_at_least"), [("nest", 3), ("py", 0), ("py2", 1), ("ts", 0)]
)
def test_every_route_the_global_pass_promotes_states_a_line_or_a_declaration(
    corpora: Corpora, corpus: str, promoted_at_least: int
) -> None:
    """The invariant behind F1: no promoted route is left with neither ``line_start`` nor
    ``declared_at``. (A route stored by a file's own parser without lines -- a prefixed router in
    its own file -- is not the global pass's, and states none: D1, D3, X.)"""
    workspace = corpora.workspace(corpus)
    checked = 0
    for repository in workspace.repositories.list_all():
        if repository.slug == DECLARATIONS_SLUG or repository.id is None:
            continue
        plan = plan_repository_http(workspace.knowledge, repository.id).plan
        promoted = {
            (edge.src_id, edge.target.name)
            for edge in plan.http
            if edge.type is EdgeType.SERVES_HTTP_ROUTE and edge.file_id is not None
        }
        for route in plan.code_routes:
            if (route.src_id, route.body) in promoted:
                checked += 1
                assert route.line_start is not None or route.declared_at is not None, (
                    repository.slug,
                    route.body,
                )
    assert checked >= promoted_at_least


def test_a_python_route_mounted_with_a_prefix_in_its_own_file_still_states_no_line(
    corpora: Corpora,
) -> None:
    """D1: ``/v1/ping`` is stored by the file's parser with no lines; F1 does not touch it."""
    workspace = corpora.workspace("py")
    repo = next(r for r in workspace.repositories.list_all() if r.name == "pyprov")
    assert repo.id is not None
    ping = [
        r
        for r in plan_repository_http(workspace.knowledge, repo.id).plan.code_routes
        if r.body == "GET /v1/ping"
    ]
    assert ping == []  # not the global pass's: it is read from the file's own edge
    [row] = workspace.database.execute(
        "SELECT e.line_start, e.line_end, e.parser FROM edges e JOIN entities t ON t.id = e.dst_id "
        "WHERE e.type = ? AND t.name = 'GET /v1/ping'",
        (str(EdgeType.SERVES_HTTP_ROUTE),),
    ).fetchall()
    assert (row[0], row[1], row[2]) == (None, None, "python-ast")
