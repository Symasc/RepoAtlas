"""Crossings in the HTTP inventory (deployment HTTP roadmap, M5a; review decisions C3, C5, C6, C8).

A caller row lists the workspace join's stored decisions for the requests it makes,
beside a status that keeps its route-matching meaning. A request only the join reads
gets a row of its own, so no decision goes unlisted. What another repository proves is
withheld under repository scope unless ``cross_repository`` asks for it, and its lines
are evidence only under global scope.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
from repoatlas.capabilities.errors import InvalidCursorError
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.http_inventory import (
    CrossingStatus,
    HttpCrossing,
    HttpOperation,
    HttpOperationsRequest,
    HttpOperationsResponse,
    OperationRole,
    OperationStatus,
)
from repoatlas.services.workspace import Workspace
from tests.http_crossing_corpus import (
    ACCOUNTS,
    ACCOUNTS_HOST_LINE,
    DECISIONS,
    LOAD_USER_CALL_LINE,
    WORKSPACE,
)
from tests.impact_corpus import build
from tests.test_http_call_decisions import MIRRORED
from tests.test_http_crossings import ALTERNATED
from tests.test_http_ts_injection import WEB


def _ask(
    workspace: Workspace, repository: str = "portal", **kwargs: object
) -> HttpOperationsResponse:
    fields: dict[str, object] = {
        "context": CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=repository),
        "role": OperationRole.CALLER,
        "limit": 200,
        **kwargs,
    }
    return CapabilityService(workspace).http_operations(HttpOperationsRequest(**fields))  # type: ignore[arg-type]


def _discover(workspace: Workspace, **kwargs: object) -> HttpOperationsResponse:
    fields: dict[str, object] = {
        "context": CapabilityContext(scope=CapabilityScope.GLOBAL),
        "discover": True,
        "route_prefix": "/api/users",
        "role": OperationRole.CALLER,
        "limit": 200,
        **kwargs,
    }
    return CapabilityService(workspace).http_operations(HttpOperationsRequest(**fields))  # type: ignore[arg-type]


def _caller(response: HttpOperationsResponse, name: str) -> HttpOperation:
    [row] = [row for row in response.operations if row.entity and f":{name}:" in row.entity]
    return row


def _stored(workspace: Workspace, repository: str) -> list[str]:
    repo = workspace.repositories.get_by_slug(repository)
    assert repo is not None and repo.id is not None
    return sorted(row.decision_ref for row in workspace.knowledge.list_http_call_decisions(repo.id))


def _refs(response: HttpOperationsResponse) -> list[str]:
    return sorted(c.decision_ref for row in response.operations for c in row.crossings)


def _places(crossing: HttpCrossing) -> list[tuple[str, str, int | None]]:
    return [(p.repository, p.path, p.line_start) for hop in crossing.chain for p in hop.provenance]


@pytest.fixture
def joined(workspace: Workspace, tmp_path: Path) -> Workspace:
    return build(workspace, tmp_path, WORKSPACE)


# -- every decision, once ------------------------------------------------------------------


def test_every_stored_decision_is_one_crossing_of_one_caller_row(joined: Workspace) -> None:
    for repository in ("accounts", "billing", "portal"):
        refs = _refs(_ask(joined, repository))
        assert refs == _stored(joined, repository)
        assert len(set(refs)) == len(refs)
    response = _ask(joined)
    # client.py's requests, and shadow.py's, whose base a function shadows.
    expected = Counter(DECISIONS.values()) + Counter({"http-base-unknown": 1})
    assert response.summary.by_crossing == dict(sorted(expected.items()))


def test_a_request_only_the_join_reads_has_a_row_of_its_own(joined: Workspace) -> None:
    """Of a status of its own: neither a route match nor an unresolved path, and counted as
    neither, but listed, filtered and explained like any other."""
    response = _ask(joined)
    row = _caller(response, "load_user")
    assert (row.status, row.detail) == (OperationStatus.JOIN_ONLY, "")
    assert (row.method, row.route, row.path) == ("GET", "/api/users/{user_id}", "client.py")
    assert (row.line_start, row.line_end) == (LOAD_USER_CALL_LINE, LOAD_USER_CALL_LINE)
    assert [c.status for c in row.crossings] == [CrossingStatus.LINKED]
    joined_only = [r for r in response.operations if r.status is OperationStatus.JOIN_ONLY]
    assert len(joined_only) > 1
    assert response.summary.by_status["join_only"] == len(joined_only)
    assert response.coverage.unresolved == {}
    assert response.coverage.unknown_paths == 0
    codes = {item.code for item in response.coverage.limitations}
    assert "unresolved_path" not in codes
    [limitation] = [item for item in response.coverage.limitations if item.code == "join_only"]
    assert limitation.count == len(joined_only)
    assert "join_only" in limitation.note
    # A host the source does not state still has its path to filter by.
    assert _caller(response, "dynamic").route == "/users/{user_id}"

    only = _ask(joined, status=(OperationStatus.JOIN_ONLY,))
    assert only.operations == tuple(joined_only)
    assert _ask(joined, status=(OperationStatus.UNRESOLVED,)).operations == ()
    rest = _ask(
        joined, status=tuple(s for s in OperationStatus if s is not OperationStatus.JOIN_ONLY)
    )
    assert rest.summary.total == response.summary.total - len(joined_only)
    assert not any(r.status is OperationStatus.JOIN_ONLY for r in rest.operations)


def test_a_row_keeps_its_route_match_status_beside_its_crossings(joined: Workspace) -> None:
    """The crossings say what the join decided; the status still says what route matching
    found in this repository, the same with crossings stored or not."""
    with_crossings = _ask(joined)
    literal = _caller(with_crossings, "load_literal")
    assert (literal.status, literal.detail) == (
        OperationStatus.NO_INDEXED_HANDLER,
        "no_served_route",
    )
    assert [c.status for c in literal.crossings] == [CrossingStatus.LINKED]

    with joined.database.transaction() as connection:
        connection.execute("DELETE FROM http_call_decisions")
        connection.execute("DELETE FROM meta WHERE key = 'http_call_decisions'")
    without = _ask(joined)
    assert not without.basis.crossings.built
    assert "crossings_not_built" in {item.code for item in without.coverage.limitations}
    assert not any(row.crossings for row in without.operations)
    assert all(row.status is not OperationStatus.JOIN_ONLY for row in without.operations)

    def statuses(response: HttpOperationsResponse) -> set[tuple[object, ...]]:
        return {
            (row.path, row.line_start, row.status, row.detail)
            for row in response.operations
            if row.status is not OperationStatus.JOIN_ONLY
        }

    assert statuses(with_crossings) == statuses(without)


def test_literal_alternatives_are_two_crossings_of_one_row(
    workspace: Workspace, tmp_path: Path
) -> None:
    built = build(workspace, tmp_path, {"accounts": ACCOUNTS, "portal": ALTERNATED})
    row = _caller(_ask(built), "pick")
    assert sorted((c.variant, c.status) for c in row.crossings) == [
        ("alternative:6", CrossingStatus.LINKED),
        ("alternative:8", CrossingStatus.SERVICE_UNMATCHED),
    ]
    assert _refs(_ask(built)) == _stored(built, "portal")


def test_two_injected_roots_are_two_crossings_of_one_row(
    workspace: Workspace, tmp_path: Path
) -> None:
    built = build(
        workspace, tmp_path, {"accounts": ACCOUNTS, "web": {**WEB, "src/main.ts": MIRRORED}}
    )
    row = _caller(_ask(built, "web"), "createUserModel.getUser")
    assert sorted((c.status.value, c.url) for c in row.crossings) == [
        ("http-service-unmatched", "https://mirror.example/api/users/{id}"),
        ("linked", "https://accounts.example/api/users/{id}"),
    ]
    assert len({c.variant for c in row.crossings}) == 2
    assert _refs(_ask(built, "web")) == _stored(built, "web")


WRAPPED = """\
const API_BASE = 'https://accounts.example/api'

async function apiRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init)
  return response.json() as Promise<T>
}

export const api = {
  users: {
    getOne: () => apiRequest<any>('/users/123'),
    settings: () => apiRequest<any>('/users/7/settings'),
    again: () => apiRequest<any>('/users/123'),
  },
}
"""


def test_a_request_a_wrapper_composes_is_crossed_on_its_composed_row(
    workspace: Workspace, tmp_path: Path
) -> None:
    """Its route row is its file's, with no lines, since the base is stated elsewhere: the
    decision goes there, one to each such row in the order of the requests' lines, and
    the request is not listed twice."""
    web = {"package.json": '{"name": "web"}', "src/client.ts": WRAPPED}
    built = build(workspace, tmp_path, {"accounts": ACCOUNTS, "web": web})
    rows = _ask(built, "web").operations
    assert [row for row in rows if row.status is OperationStatus.JOIN_ONLY] == []
    assert [
        (row.route, [c.chain[0].provenance[0].line_start for c in row.crossings])
        for row in rows
        if row.line_start is None
    ] == [("/api/users/123", [10]), ("/api/users/123", [12]), ("/api/users/7/settings", [11])]
    assert all(c.status is CrossingStatus.LINKED for row in rows for c in row.crossings)
    assert _refs(_ask(built, "web")) == _stored(built, "web")


# -- filtering -----------------------------------------------------------------------------


def test_the_crossing_filter_keeps_callers_with_those_decisions_and_only_those(
    joined: Workspace,
) -> None:
    unknown = _ask(joined, crossing=(CrossingStatus.BASE_UNKNOWN,))
    callers = {row.entity.split(":")[-2] for row in unknown.operations if row.entity}
    assert callers == {"dynamic", "from_env", "shared", "load"}
    assert all(
        c.status is CrossingStatus.BASE_UNKNOWN for row in unknown.operations for c in row.crossings
    )
    assert unknown.summary.by_crossing == {"http-base-unknown": 4}
    assert unknown.summary.total == 4


def test_a_cursor_is_bound_to_how_much_of_a_crossing_it_showed(joined: Workspace) -> None:
    first = _ask(joined, limit=1)
    assert first.next_cursor is not None
    with pytest.raises(InvalidCursorError):
        _ask(joined, limit=1, cursor=first.next_cursor, cross_repository=True)


# -- scope ---------------------------------------------------------------------------------


def test_repository_scope_withholds_what_another_repository_proves(joined: Workspace) -> None:
    response = _ask(joined, include_provenance=True)
    assert response.basis.crossings.projection == "own_repository"
    [link] = _caller(response, "load_user").crossings
    assert (link.handler, link.provider) == (None, None)
    assert link.endpoint == "http-endpoint:GET:https://accounts.example/api/users/<user_id>"
    assert [(hop.kind, hop.status) for hop in link.chain][2:] == [
        (kind, "proven")
        for kind in ("service_identity", "deployment_binding", "source_binding", "route", "handler")
    ]
    for hop in link.chain[2:]:
        assert (hop.provenance, hop.detail, hop.handler, hop.withheld) == ((), "", None, True)
    assert {
        place[0] for row in response.operations for c in row.crossings for place in _places(c)
    } == {"portal"}
    # A refusal keeps the hop that failed and why, whatever the scope.
    [refused] = _caller(response, "spans").crossings
    route = refused.chain[-2]
    assert (route.kind, route.status, route.reason) == ("route", "unresolved", "route_unmatched")
    assert route.note
    assert {item.ref.repository_slug for item in response.evidence} == {"portal"}
    assert [f.repository_slug for f in response.freshness.repositories] == ["portal"]


def test_cross_repository_shows_it_and_supplies_no_evidence_from_it(joined: Workspace) -> None:
    response = _ask(joined, cross_repository=True, include_provenance=True)
    assert response.basis.crossings.projection == "cross_repository"
    [link] = _caller(response, "load_user").crossings
    assert link.provider == "accounts"
    assert link.handler is not None and ":get_user:" in link.handler
    identity = link.chain[2]
    assert [(p.repository, p.path, p.line_start) for p in identity.provenance] == [
        ("accounts", "app.py", ACCOUNTS_HOST_LINE)
    ]
    assert identity.detail and not identity.withheld
    caller = link.chain[0].provenance[0]
    assert caller.repository == "portal" and caller.evidence_id is not None
    assert all(
        place.evidence_id is None
        for hop in link.chain
        for place in hop.provenance
        if place.repository != "portal"
    )
    assert {item.ref.repository_slug for item in response.evidence} == {"portal"}
    # The answer shows accounts' and billing's facts, so it says how fresh their index is.
    assert sorted(f.repository_slug for f in response.freshness.repositories) == [
        "accounts",
        "billing",
        "portal",
    ]


def test_global_scope_supplies_the_proven_hops_of_every_repository(joined: Workspace) -> None:
    response = _discover(joined, include_provenance=True)
    assert response.basis.crossings.projection == "global"
    [link] = _caller(response, "load_user").crossings
    supplied = {
        (item.ref.repository_slug, item.ref.path, item.ref.line_start): item.id
        for item in response.evidence
    }
    for hop in link.chain:
        for place in hop.provenance:
            if place.line_start is not None:
                key = (place.repository, place.path, place.line_start)
                assert place.evidence_id == supplied[key], key
    assert ("accounts", "app.py", ACCOUNTS_HOST_LINE) in supplied
    # A hop not reached cites nothing, so supplies nothing.
    [refused] = _caller(response, "spans").crossings
    assert not refused.chain[-1].provenance


# -- basis ---------------------------------------------------------------------------------


def test_the_basis_says_what_a_crossing_establishes_and_a_status_does_not(
    joined: Workspace,
) -> None:
    basis = _ask(joined).basis
    assert basis.establishes == ("route_compatibility",)
    assert basis.does_not_establish == ("service_configuration", "data_ownership")
    assert basis.crossings.built
    assert basis.crossings.establishes == ("source_backed_deployment_identity_chain",)
    assert basis.crossings.does_not_establish == ("runtime_reachability", "deployment_runs")
    discovery = _discover(joined).basis
    assert "deployment_identity" not in discovery.does_not_establish
    assert discovery.crossings.establishes == ("source_backed_deployment_identity_chain",)
