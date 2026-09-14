"""HTTP linkage through real indexing and the public capabilities (brief §80).

Every scenario is asserted through ``relationships`` on the shared node, as a
**closed** set: the edges listed are exactly the edges that exist, so an
invented provider, caller or deployment fails the test rather than slipping in
beside the expected ones. The corpus is ``tests/fixtures/http``; each of its
files names the scenario it exists for.

What is asserted everywhere, and never relaxed for convenience:

* two repositories meet on a shared node and **nowhere else** — no edge ever
  joins them directly;
* a node with two providers keeps both, and nothing picks one;
* a route match is not an endpoint match, and a method is part of both.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.models.parsing import EdgeType
from repoatlas.models.relationships import RelationshipDirection
from repoatlas.services.workspace import Workspace
from tests.http_corpus import (
    HTTP_EDGE_TYPES,
    edge_set,
    entity_key,
    http_nodes,
    index_corpus,
    relationships,
    stored_http_edges,
)

WEB = "web/src/api.ts"
PY = "pyclient/client/calls.py"


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Workspace]:
    workspace = index_corpus(tmp_path_factory.mktemp("http-corpus"))
    yield workspace
    workspace.close()


# -- scenario A: route only ---------------------------------------------------------------


def test_a_relative_call_and_a_decorator_meet_on_one_route(corpus: Workspace) -> None:
    assert edge_set(relationships(corpus, "http-route:GET:/hello")) == {
        ("api-a/app/main.py::hello", "SERVES_HTTP_ROUTE", "http-route:GET:/hello"),
        ("api-a/openapi.yaml", "SERVES_HTTP_ROUTE", "http-route:GET:/hello"),
        (f"{WEB}::helloRelative", "CALLS_HTTP_ROUTE", "http-route:GET:/hello"),
        (f"{WEB}::helloAbsolute", "CALLS_HTTP_ROUTE", "http-route:GET:/hello"),
        (f"{WEB}::helloEu", "CALLS_HTTP_ROUTE", "http-route:GET:/hello"),
        (f"{PY}::hello_absolute", "CALLS_HTTP_ROUTE", "http-route:GET:/hello"),
    }


def test_a_relative_call_reaches_no_endpoint(corpus: Workspace) -> None:
    key = entity_key(corpus, "web", "helloRelative")
    assert edge_set(relationships(corpus, key, direction=RelationshipDirection.OUTGOING)) == {
        (f"{WEB}::helloRelative", "CALLS_HTTP_ROUTE", "http-route:GET:/hello"),
    }


def test_a_framework_route_declares_no_deployment(corpus: Workspace) -> None:
    key = entity_key(corpus, "api-a", "hello")
    types = {edge.type for edge in relationships(corpus, key).edges}
    assert types == {EdgeType.SERVES_HTTP_ROUTE}


# -- scenario B: deployed endpoint ----------------------------------------------------------


def test_a_specification_and_absolute_calls_meet_on_one_endpoint(corpus: Workspace) -> None:
    node = "http-endpoint:GET:https://api.example.com/hello"
    assert edge_set(relationships(corpus, node)) == {
        ("api-a/openapi.yaml", "EXPOSES_HTTP_ENDPOINT", node),
        (f"{WEB}::helloAbsolute", "CALLS_HTTP_ENDPOINT", node),
        (f"{PY}::hello_absolute", "CALLS_HTTP_ENDPOINT", node),
    }


# -- scenario C: base path --------------------------------------------------------------------


def test_a_swagger_base_path_is_part_of_the_endpoint(corpus: Workspace) -> None:
    node = "http-endpoint:GET:https://api.example.com/v1/users"
    assert edge_set(relationships(corpus, node)) == {
        ("api-b/swagger.yaml", "EXPOSES_HTTP_ENDPOINT", node),
        (f"{WEB}::listUsersV1", "CALLS_HTTP_ENDPOINT", node),
        (f"{PY}::users_v1", "CALLS_HTTP_ENDPOINT", node),
    }
    assert edge_set(relationships(corpus, "http-deployment:https://api.example.com/v1")) == {
        (
            "api-b/swagger.yaml",
            "DECLARES_HTTP_DEPLOYMENT",
            "http-deployment:https://api.example.com/v1",
        ),
    }


# -- scenario D: same route, different deployment ------------------------------------------------


def test_the_same_route_on_different_deployments_shares_only_the_route(corpus: Workspace) -> None:
    assert edge_set(relationships(corpus, "http-route:GET:/ping")) == {
        ("api-d/openapi.yaml", "SERVES_HTTP_ROUTE", "http-route:GET:/ping"),
        (f"{WEB}::pingOtherDeployment", "CALLS_HTTP_ROUTE", "http-route:GET:/ping"),
    }
    declared = "http-endpoint:GET:https://api-a.example.com/ping"
    called = "http-endpoint:GET:https://api-b.example.com/ping"
    assert edge_set(relationships(corpus, declared)) == {
        ("api-d/openapi.yaml", "EXPOSES_HTTP_ENDPOINT", declared),
    }
    assert edge_set(relationships(corpus, called)) == {
        (f"{WEB}::pingOtherDeployment", "CALLS_HTTP_ENDPOINT", called),
    }


# -- scenario E: method mismatch ---------------------------------------------------------------


def test_a_method_mismatch_shares_no_endpoint(corpus: Workspace) -> None:
    get = "http-endpoint:GET:https://api.example.com/users"
    post = "http-endpoint:POST:https://api.example.com/users"
    assert edge_set(relationships(corpus, get)) == {
        (f"{WEB}::listUsers", "CALLS_HTTP_ENDPOINT", get)
    }
    assert edge_set(relationships(corpus, post)) == {
        ("api-a/openapi.yaml", "EXPOSES_HTTP_ENDPOINT", post),
        (f"{WEB}::createUser", "CALLS_HTTP_ENDPOINT", post),
    }


# -- scenario F: dynamic base ----------------------------------------------------------------------


def test_a_dynamic_base_url_produces_no_http_fact(corpus: Workspace) -> None:
    key = entity_key(corpus, "pyclient", "hello_dynamic")
    types = {edge.type.value for edge in relationships(corpus, key).edges}
    assert not types & HTTP_EDGE_TYPES


# -- scenario G: multiple deployments --------------------------------------------------------------


def test_a_call_to_one_deployment_links_to_that_deployment_only(corpus: Workspace) -> None:
    key = entity_key(corpus, "web", "helloEu")
    assert edge_set(relationships(corpus, key, direction=RelationshipDirection.OUTGOING)) == {
        (
            f"{WEB}::helloEu",
            "CALLS_HTTP_ENDPOINT",
            "http-endpoint:GET:https://api-eu.example.com/hello",
        ),
        (f"{WEB}::helloEu", "CALLS_HTTP_ROUTE", "http-route:GET:/hello"),
    }
    eu = "http-endpoint:GET:https://api-eu.example.com/hello"
    assert edge_set(relationships(corpus, eu)) == {
        ("api-a/openapi.yaml", "EXPOSES_HTTP_ENDPOINT", eu),
        (f"{WEB}::helloEu", "CALLS_HTTP_ENDPOINT", eu),
    }


def test_both_declared_deployments_are_kept(corpus: Workspace) -> None:
    for node in (
        "http-deployment:https://api.example.com",
        "http-deployment:https://api-eu.example.com",
    ):
        sources = {source for source, _, _ in edge_set(relationships(corpus, node))}
        assert "api-a/openapi.yaml" in sources


# -- scenario H: multiple providers ----------------------------------------------------------------


def test_two_declarers_of_one_endpoint_stay_two(corpus: Workspace) -> None:
    node = "http-endpoint:GET:https://api.example.com/health"
    assert edge_set(relationships(corpus, node)) == {
        ("api-a/openapi.yaml", "EXPOSES_HTTP_ENDPOINT", node),
        ("api-c/openapi.json", "EXPOSES_HTTP_ENDPOINT", node),
        (f"{WEB}::health", "CALLS_HTTP_ENDPOINT", node),
    }
    assert sum(1 for key in http_nodes(corpus) if key == node) == 1


# -- scenario I: path parameters -------------------------------------------------------


def test_a_concrete_path_does_not_match_a_parameter_template(corpus: Workspace) -> None:
    assert edge_set(relationships(corpus, "http-route:GET:/users/{user_id}")) == {
        ("api-a/app/main.py::show_user", "SERVES_HTTP_ROUTE", "http-route:GET:/users/{user_id}"),
    }
    assert edge_set(relationships(corpus, "http-route:GET:/users/42")) == {
        (f"{WEB}::user42", "CALLS_HTTP_ROUTE", "http-route:GET:/users/42"),
    }


# -- scenario J: adversarial .get() ----------------------------------------------------


def test_http_shaped_lookups_and_unproven_frameworks_produce_nothing(corpus: Workspace) -> None:
    assert not {edge for edge in stored_http_edges(corpus) if ":decoys:" in edge[0]}


# -- framework composition ------------------------------------------------------------------------


def test_router_mounts_compose_the_served_path(corpus: Workspace) -> None:
    assert edge_set(relationships(corpus, "http-route:GET:/api/users/me")) == {
        ("api-a/app/main.py::me", "SERVES_HTTP_ROUTE", "http-route:GET:/api/users/me"),
    }
    assert edge_set(relationships(corpus, "http-route:GET:/items")) == {
        ("api-c/server.js", "SERVES_HTTP_ROUTE", "http-route:GET:/items"),
    }
    assert edge_set(relationships(corpus, "http-route:GET:/status")) == {
        ("api-c/server.js", "SERVES_HTTP_ROUTE", "http-route:GET:/status"),
    }


# -- the shape of the graph ------------------------------------------------------------


def test_no_http_edge_joins_two_repository_owned_entities(corpus: Workspace) -> None:
    """Repositories meet on a shared node, never directly (brief §39)."""
    rows = corpus.database.execute(
        "SELECT e.type, src.repo_id AS src_repo, dst.repo_id AS dst_repo, dst.file_id AS dst_file, "
        "dst.type AS dst_type, e.repo_id AS edge_repo FROM edges e "
        "JOIN entities src ON src.id = e.src_id JOIN entities dst ON dst.id = e.dst_id "
        "WHERE e.type IN (?, ?, ?, ?, ?)",
        tuple(sorted(HTTP_EDGE_TYPES)),
    ).fetchall()
    assert rows
    for row in rows:
        assert row["src_repo"] is not None
        assert row["dst_repo"] is None and row["dst_file"] is None
        assert row["dst_type"] in {"http_route", "http_deployment", "http_endpoint"}
        assert row["edge_repo"] == row["src_repo"]


def test_shared_nodes_are_deduplicated_across_repositories_and_files(corpus: Workspace) -> None:
    rows = corpus.database.execute(
        "SELECT key, count(*) AS n FROM entities WHERE type LIKE 'http_%' GROUP BY key HAVING n > 1"
    ).fetchall()
    assert rows == []


def test_stored_orientation_survives_an_incoming_walk(corpus: Workspace) -> None:
    response = relationships(
        corpus,
        "http-endpoint:GET:https://api.example.com/hello",
        direction=RelationshipDirection.INCOMING,
    )
    assert response.edges
    for edge in response.edges:
        assert edge.target.key == "http-endpoint:GET:https://api.example.com/hello"
        assert edge.target.repository_slug is None
        assert edge.source.repository_slug is not None
        assert edge.traversal_direction is RelationshipDirection.INCOMING
        assert edge.cross_repository is False


def test_a_shared_node_rooted_walk_sees_every_side(corpus: Workspace) -> None:
    """A shared node has no repository to be confined to (same as ``env:`` hubs)."""
    response = relationships(corpus, "http-endpoint:GET:https://api.example.com/health")
    assert {edge.source.repository_slug for edge in response.edges} == {"api-a", "api-c", "web"}


def test_nothing_http_reaches_the_dependency_capability(corpus: Workspace) -> None:
    rows = corpus.database.execute(
        "SELECT count(*) AS n FROM dependencies WHERE normalized_name LIKE 'http%' "
        "OR normalized_name LIKE '%example.com%'"
    ).fetchone()
    assert rows["n"] == 0


# -- provenance -----------------------


def test_a_call_is_citable_and_a_composed_endpoint_declaration_is_not(corpus: Workspace) -> None:
    """§86: a composed identity has no single span, so it is not cited as one."""
    node = "http-endpoint:GET:https://api.example.com/hello"
    response = relationships(corpus, node)
    by_source = {edge.source.repository_slug: edge for edge in response.edges}

    call = by_source["web"]
    assert call.provenance is not None and call.provenance.citable
    assert call.evidence_ids

    declaration = by_source["api-a"]
    assert declaration.provenance is not None
    assert declaration.provenance.path == "openapi.yaml"
    assert declaration.provenance.line_start is None
    assert declaration.provenance.citable is False
    assert declaration.evidence_ids == ()


def test_the_constituents_of_a_composed_endpoint_are_separately_citable(corpus: Workspace) -> None:
    route = relationships(corpus, "http-route:GET:/hello")
    [spec_route] = [e for e in route.edges if e.source.path == "openapi.yaml"]
    assert spec_route.provenance is not None and spec_route.provenance.citable

    deployment = relationships(corpus, "http-deployment:https://api.example.com")
    [spec_server] = [e for e in deployment.edges if e.source.repository_slug == "api-a"]
    assert spec_server.provenance is not None and spec_server.provenance.citable


def test_a_composed_router_route_is_a_fact_without_a_citation(corpus: Workspace) -> None:
    [edge] = relationships(corpus, "http-route:GET:/api/users/me").edges
    assert edge.provenance is not None and edge.provenance.line_start is None
    assert edge.evidence_ids == ()


def test_an_unindexed_env_file_is_not_a_deployment(corpus: Workspace, tmp_path: Path) -> None:
    """The decoy ``.env`` names the API URL and is excluded, so it declares nothing."""
    rows = corpus.database.execute(
        "SELECT count(*) AS n FROM files f JOIN repositories r ON r.id = f.repo_id "
        "WHERE r.slug = 'decoys' AND f.rel_path = '.env'"
    ).fetchone()
    assert rows["n"] == 0
    assert not {edge for edge in stored_http_edges(corpus) if edge[0].startswith("file:decoys:")}
