"""The cross-repository HTTP join, end to end (Python P4).

A request in one repository reaches a handler in another only when the service identity,
the method and the route shape all agree on exactly one handler. The join becomes one
``CALLS_HTTP_HANDLER`` edge, *consumer caller → provider handler*, whose resolution
records the exact lines on both sides. The change-impact walk follows it inward, from a
handler to the requests that reach it, and never the other way.

The fixture repositories are in :mod:`tests.http_crossing_corpus`. Everything is asserted
as closed sets and exact lines: a join that is almost right is a false relationship
between two repositories.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.graph.http_crossings import (
    LINKED,
    PROVIDER_AMBIGUOUS,
    ROUTE_UNMATCHED,
    SAME_REPOSITORY,
    SERVICE_UNMATCHED,
    plan_http_crossings,
)
from repoatlas.graph.linker import ReferenceLinker
from repoatlas.models.capability import CapabilityContext, CapabilityScope, Presentation
from repoatlas.models.impact import HttpCrossing, ImpactRequest, ImpactResponse
from repoatlas.models.parsing import EdgeType, EntityType
from repoatlas.parsers.http_facts import HTTP_CLIENT_CALL, HTTP_EXPOSED_ENDPOINT
from repoatlas.services.workspace import Workspace
from repoatlas.storage.protocols import FileRow, UnresolvedRefRecord
from tests.http_crossing_corpus import (
    ACCOUNTS,
    ACCOUNTS_BOUND_LINE,
    ACCOUNTS_HOST_LINE,
    BILLING,
    DECISIONS,
    GET_USER_ROUTE_LINE,
    LINKS,
    LOAD_USER_CALL_LINE,
    MIRROR,
    PORTAL,
    PORTAL_BASE_LINE,
    WORKSPACE,
    crossings,
    links,
)
from tests.impact_corpus import build, verify_steps
from tests.incremental_corpus import assert_equals_fresh, edit, sync_all

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)


def decisions(workspace: Workspace, path: str = "client.py") -> dict[str, str]:
    """What the join decides for each request one file makes, by caller."""
    plan = ReferenceLinker(workspace.knowledge).prepare_global().http_crossings
    rows = workspace.database.execute(
        """
        SELECT u.id, s.qualified_name, s.type FROM unresolved_refs u
        JOIN entities s ON s.id = u.src_id JOIN files f ON f.id = u.file_id
        WHERE f.rel_path = ? AND u.meta_json LIKE ?
        """,
        (path, f'%"{HTTP_CLIENT_CALL}"%'),
    ).fetchall()
    return {row["qualified_name"]: plan.decisions[row["id"]] for row in rows}


def impact(workspace: Workspace, seed: str) -> ImpactResponse:
    request = ImpactRequest(context=GLOBAL, seed=seed, presentation=Presentation.FULL)
    response = CapabilityService(workspace).impact(request)
    verify_steps(workspace, response)
    return response


def key(workspace: Workspace, repository: str, name: str) -> str:
    [row] = workspace.database.execute(
        "SELECT e.key FROM entities e JOIN repositories r ON r.id = e.repo_id "
        "WHERE r.slug = ? AND e.qualified_name = ? AND e.type = 'function'",
        (repository, name),
    ).fetchall()
    return row["key"]


@pytest.fixture
def joined(workspace: Workspace, tmp_path: Path) -> Workspace:
    return build(workspace, tmp_path, WORKSPACE)


# -- what is joined (H1-H4, H6-H8) ---------------------------------------------------------


def test_each_request_is_decided_as_pre_registered(joined: Workspace) -> None:
    assert decisions(joined) == DECISIONS


def test_the_joins_are_exactly_the_pre_registered_ones(joined: Workspace) -> None:
    assert links(joined) == LINKS


def test_a_request_the_source_does_not_state_makes_no_claim(joined: Workspace) -> None:
    """H7: a parameter, an environment variable, an imported and a shadowed base."""
    stated = decisions(joined)
    assert {"dynamic", "from_env", "shared"}.isdisjoint(stated)
    assert decisions(joined, "shadow.py") == {}


def test_a_join_is_provenanced_by_the_request_and_made_by_the_global_pass(
    joined: Workspace,
) -> None:
    [row] = [r for r in crossings(joined) if r["caller"] == "load_user"]
    assert (row["consumer"], row["provider"], row["handler"]) == ("portal", "accounts", "get_user")
    assert row["asserted_by"] == "client.py"
    assert (row["line_start"], row["line_end"]) == (LOAD_USER_CALL_LINE, LOAD_USER_CALL_LINE)
    assert row["parser"] == "linker:global"


def test_the_evidence_on_both_sides_is_exact(joined: Workspace) -> None:
    """H9: every line the join cites, as the source states it."""
    [row] = [r for r in crossings(joined) if r["caller"] == "load_user"]
    assert row["resolution"] == {
        "http": {
            "method": "GET",
            "url": "https://accounts.example/api/users/{user_id}",
            "endpoint": "GET https://accounts.example/api/users/<user_id>",
            "deployment": "https://accounts.example/api",
            "provider": "accounts",
            "consumer": "portal",
            "called_at": f"client.py:{LOAD_USER_CALL_LINE}",
            "base_at": f"client.py:{PORTAL_BASE_LINE}",
            "served_at": f"app.py:{GET_USER_ROUTE_LINE}",
            "identity_source": "flasgger-swagger2",
            "identity_at": f"app.py:{ACCOUNTS_HOST_LINE}",
            "bound_at": f"app.py:{ACCOUNTS_BOUND_LINE}",
        }
    }


def test_the_same_route_on_two_services_is_told_apart_by_identity(joined: Workspace) -> None:
    """H4: ``accounts`` and ``billing`` both serve ``/users/<user_id>``."""
    by_caller = {(r["caller"], r["provider"]) for r in crossings(joined)}
    assert ("billing_user", "billing") in by_caller
    assert ("billing_user", "accounts") not in by_caller
    assert ("load_user", "billing") not in by_caller


# -- ambiguity and the repository boundary (H5) ------------------------------------------


def test_two_repositories_publishing_one_identity_make_the_join_ambiguous(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, {**WORKSPACE, "accounts-mirror": MIRROR})
    decided = decisions(workspace)
    assert decided["load_user"] == PROVIDER_AMBIGUOUS
    assert decided["load_literal"] == PROVIDER_AMBIGUOUS
    # Routes the mirror does not serve are still one handler's.
    assert decided["load_settings"] == LINKED
    assert {c for c, *_ in links(workspace)} == {"load_settings", "billing_user", "item", "audit"}


def test_a_request_matching_a_literal_and_a_parameter_route_is_ambiguous(
    workspace: Workspace, tmp_path: Path
) -> None:
    service = dict(ACCOUNTS)
    service["app.py"] = ACCOUNTS["app.py"] + (
        '\n\n@app.route("/users/me")\ndef me():\n    return {}\n'
    )
    client = "import requests\n\n\ndef me():\n    return requests.get('https://accounts.example/api/users/me')\n"
    build(workspace, tmp_path, {"accounts": service, "portal": {"client.py": client}})
    assert decisions(workspace) == {"me": PROVIDER_AMBIGUOUS}
    assert links(workspace) == set()


def test_a_request_to_its_own_repository_is_not_a_crossing(
    workspace: Workspace, tmp_path: Path
) -> None:
    service = dict(ACCOUNTS)
    service["client.py"] = (
        "import requests\n\n\ndef self_call():\n"
        "    return requests.get('https://accounts.example/api/users/1')\n"
    )
    build(workspace, tmp_path, {"accounts": service})
    assert decisions(workspace) == {"self_call": SAME_REPOSITORY}
    assert links(workspace) == set()


# -- the representation is not the extractor's (A1) --------------------------------------


def _row(file_id: int, repo_id: int, slug: str, path: str) -> FileRow:
    return FileRow(
        file_id=file_id,
        file_entity_id=100 + file_id,
        repo_id=repo_id,
        repo_name=slug,
        repo_slug=slug,
        repo_path=Path("/r") / slug,
        rel_path=path,
    )


def _claim(claim_id: int, file_id: int, repo_id: int, src_id: int, key: str, **meta: str):
    return UnresolvedRefRecord(
        id=claim_id,
        repo_id=repo_id,
        file_id=file_id,
        src_id=src_id,
        edge_type=EdgeType.CALLS_HTTP_ENDPOINT,
        target_kind=EntityType.HTTP_ENDPOINT,
        target_key=key,
        line_start=5,
        line_end=5,
        meta_json=json.dumps(meta),
    )


@pytest.mark.parametrize("identity_source", ["flasgger-swagger2", "deployment-config"])
def test_any_source_stating_the_identity_joins_the_same_way(identity_source: str) -> None:
    """A1: the join reads the claim, never what produced it."""
    files = [_row(1, 1, "svc", "deploy/service.yml"), _row(2, 2, "web", "client.py")]
    exposure = _claim(
        1,
        1,
        1,
        11,
        "GET https://svc.example/users/{id}",
        pending_fact=HTTP_EXPOSED_ENDPOINT,
        method="GET",
        route="/users/{id}",
        deployment="https://svc.example",
        identity_source=identity_source,
        identity_at="3",
    )
    request = _claim(
        2,
        2,
        2,
        22,
        "GET https://svc.example/users/7",
        pending_fact=HTTP_CLIENT_CALL,
        method="GET",
        origin="https://svc.example",
        segments='["users", "7"]',
    )
    plan = plan_http_crossings([exposure, request], files, producer="linker:global")
    assert plan.decisions == {2: LINKED}
    [edge] = plan.edges
    assert (edge.src_id, edge.dst_id, edge.type) == (22, 11, EdgeType.CALLS_HTTP_HANDLER)
    record = json.loads(edge.resolution or "{}")["http"]
    assert record["identity_source"] == identity_source
    assert record["identity_at"] == "deploy/service.yml:3"
    assert "bound_at" not in record


def test_a_claim_the_join_cannot_read_is_never_joined() -> None:
    files = [_row(1, 1, "svc", "app.py"), _row(2, 2, "web", "client.py")]
    exposure = _claim(
        1,
        1,
        1,
        11,
        "GET https://svc.example/users/{id}",
        pending_fact=HTTP_EXPOSED_ENDPOINT,
        method="GET",
        route="/users/{id}",
        deployment="https://svc.example",
        identity_source="x",
    )
    broken = _claim(
        2, 2, 2, 22, "GET x", pending_fact=HTTP_CLIENT_CALL, method="GET", origin="svc.example"
    )
    plan = plan_http_crossings([exposure, broken], files, producer="linker:global")
    assert plan.edges == ()
    assert plan.decisions == {2: "http-claim-unread"}


# -- impact: inward only, with the evidence (H10, A2, A3) --------------------------------


def test_a_handler_change_reaches_the_remote_request_with_its_evidence(joined: Workspace) -> None:
    """A2: the edge runs consumer → provider; the step reads it provider → consumer."""
    response = impact(joined, key(joined, "accounts", "get_user"))
    [caller] = [c for c in response.candidates if c.name == "load_user"]
    assert caller.repository == "portal"
    assert caller.step.edge is EdgeType.CALLS_HTTP_HANDLER
    assert caller.step.reading == "requests over HTTP"
    assert caller.step.at == "portal/client.py"
    assert caller.step.lines == (LOAD_USER_CALL_LINE, LOAD_USER_CALL_LINE)
    assert caller.step.crossing is None
    assert caller.step.http == HttpCrossing(
        method="GET",
        url="https://accounts.example/api/users/{user_id}",
        endpoint="GET https://accounts.example/api/users/<user_id>",
        deployment="https://accounts.example/api",
        provider_repository="accounts",
        consumer_repository="portal",
        called_at=f"portal/client.py:{LOAD_USER_CALL_LINE}",
        base_at=f"portal/client.py:{PORTAL_BASE_LINE}",
        served_at=f"accounts/app.py:{GET_USER_ROUTE_LINE}",
        identity_source="flasgger-swagger2",
        identity_at=f"accounts/app.py:{ACCOUNTS_HOST_LINE}",
        bound_at=f"accounts/app.py:{ACCOUNTS_BOUND_LINE}",
    )
    assert caller.chain == (
        "get_user -> load_user (requests over HTTP, accounts -> portal via "
        "GET https://accounts.example/api/users/{user_id})"
    )
    assert caller.boundaries == ("accounts:. -> portal:.",)


def test_a_change_behind_a_handler_reaches_the_remote_callers_and_their_tests(
    joined: Workspace,
) -> None:
    """H10: the handler's own dependency → the handler → the request → its callers and tests."""
    response = impact(joined, key(joined, "accounts", "lookup"))
    via = (
        "lookup -> get_user (calls) -> load_user (requests over HTTP, accounts -> portal via "
        "GET https://accounts.example/api/users/{user_id})"
    )
    chains = {c.name: c.chain for c in response.candidates}
    assert chains["load_user"] == via
    assert chains["render_profile"] == f"{via} -> render_profile (calls)"
    assert [(t.repository, t.name, t.chain) for t in response.tests] == [
        ("portal", "test_load_user", f"{via} -> test_load_user (calls)")
    ]


def test_a_request_change_never_reaches_the_handler_it_requests(joined: Workspace) -> None:
    """A3: a handler does not depend on its callers."""
    response = impact(joined, key(joined, "portal", "load_user"))
    assert all(c.repository != "accounts" for c in response.candidates)
    assert all(c.step.edge is not EdgeType.CALLS_HTTP_HANDLER for c in response.candidates)


def test_a_crossing_edge_without_its_evidence_is_not_followed(joined: Workspace) -> None:
    joined.database.execute("UPDATE edges SET resolution = '{}' WHERE type = 'CALLS_HTTP_HANDLER'")
    response = impact(joined, key(joined, "accounts", "get_user"))
    assert all(c.repository != "portal" for c in response.candidates)


# -- incremental == fresh (H11-H14) ------------------------------------------------------


@pytest.mark.parametrize(
    ("repo", "path", "old", "new", "decided"),
    [
        # H11: the provider's route, and its method.
        (
            "accounts",
            "app.py",
            '@app.route("/users/<user_id>")\n',
            '@app.route("/people/<user_id>")\n',
            ROUTE_UNMATCHED,
        ),
        (
            "accounts",
            "app.py",
            '@app.route("/users/<user_id>")\n',
            '@app.route("/users/<user_id>", methods=["PUT"])\n',
            ROUTE_UNMATCHED,
        ),
        # H11: the blueprint's mount does not touch load_user; the host does (H12).
        (
            "accounts",
            "app.py",
            '"host": "accounts.example"',
            '"host": "users.example"',
            SERVICE_UNMATCHED,
        ),
        ("accounts", "app.py", '"schemes": ["https"]', '"schemes": ["http"]', SERVICE_UNMATCHED),
        # H13: the consumer's base constant, now another service's.
        (
            "portal",
            "client.py",
            'ACCOUNTS = "https://accounts.example/api"',
            'ACCOUNTS = "https://billing.example"',
            LINKED,
        ),
    ],
)
def test_an_edit_to_either_side_is_what_a_fresh_index_writes(
    workspace: Workspace,
    settings: Settings,
    tmp_path: Path,
    repo: str,
    path: str,
    old: str,
    new: str,
    decided: str,
) -> None:
    build(workspace, tmp_path, WORKSPACE)
    root = tmp_path / "repos"
    text = (root / repo / path).read_text(encoding="utf-8")
    assert old in text
    edit(root, repo, path, text.replace(old, new, 1))
    sync_all(workspace, root)
    assert decisions(workspace)["load_user"] == decided
    assert_equals_fresh(workspace, settings, root, tmp_path, "fresh")


def test_the_consumer_edit_moves_the_join_to_the_other_service(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    build(workspace, tmp_path, WORKSPACE)
    root = tmp_path / "repos"
    text = (root / "portal" / "client.py").read_text(encoding="utf-8")
    edit(
        root,
        "portal",
        "client.py",
        text.replace('https://accounts.example/api"', 'https://billing.example"', 1),
    )
    sync_all(workspace, root)
    assert (
        "load_user",
        "billing",
        "billing_user",
        "GET https://billing.example/users/<user_id>",
    ) in links(workspace)


def test_a_deleted_provider_file_takes_its_joins_with_it(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    build(workspace, tmp_path, WORKSPACE)
    root = tmp_path / "repos"
    (root / "billing" / "app.py").unlink()
    sync_all(workspace, root)
    assert decisions(workspace)["billing_user"] == SERVICE_UNMATCHED
    assert all(r["provider"] != "billing" for r in crossings(workspace))
    assert_equals_fresh(workspace, settings, root, tmp_path, "fresh")


def test_an_ordinary_body_edit_adds_no_repository_wide_work(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    """H14: the join is replanned with the global pass; USES work stays the file's own."""
    build(workspace, tmp_path, WORKSPACE)
    root = tmp_path / "repos"
    before = crossings(workspace)
    edit(root, "accounts", "store.py", 'def lookup(user_id):\n    return {"user": user_id}\n')
    work = sync_all(workspace, root)
    assert work.mode == "incremental"
    assert (work.repository, work.package, work.repositories_invalidated) == (0, 0, 0)
    assert crossings(workspace) == before
    assert_equals_fresh(workspace, settings, root, tmp_path, "fresh")


def test_the_services_themselves_are_unchanged_by_the_join(joined: Workspace) -> None:
    """The per-file facts still meet on shared nodes exactly as before."""
    rows = joined.database.execute(
        "SELECT COUNT(*) FROM edges "
        "WHERE type = 'EXPOSES_HTTP_ENDPOINT' AND parser != 'linker:global'"
    ).fetchall()
    # accounts: 5 routes x 1 deployment; billing: 2 routes x 1 deployment.
    assert rows[0][0] == 7


def test_billing_is_its_own_service(workspace: Workspace, tmp_path: Path) -> None:
    build(workspace, tmp_path, {"billing": BILLING, "portal": PORTAL})
    assert {c for c, *_ in links(workspace)} == {"billing_user"}
