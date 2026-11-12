"""topology/overview: which repositories are linked to which, and by what kind of fact.

Checked against the index itself: every link count is the number of stored edges
the oracle counts for that kind and repository pair, observed, HTTP and
dependency facts are never merged, and every example resolves to an edge of its
own link.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from repoatlas.capabilities import service as service_module
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import CapabilityContext
from repoatlas.models.topology import (
    MAX_EXAMPLES,
    LinkKind,
    TopologyEdgeRequest,
    TopologyOverviewRequest,
    TopologyOverviewResponse,
)
from repoatlas.services.workspace import Workspace
from tests.topology_corpus import (
    GLOBAL,
    LINKED,
    cross_repository_counts,
    kind_of,
    open_corpus,
    repository,
)


@pytest.fixture(scope="module")
def linked(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Workspace]:
    workspace = open_corpus(tmp_path_factory.mktemp("topology-overview"), LINKED)
    yield workspace
    workspace.close()


def overview(workspace: Workspace, context: CapabilityContext = GLOBAL) -> TopologyOverviewResponse:
    return CapabilityService(workspace).topology_overview(TopologyOverviewRequest(context=context))


def test_every_link_count_is_the_stored_edge_count_for_its_kind(linked: Workspace) -> None:
    response = overview(linked)
    assert response.complete
    reported = {(str(link.kind), link.source, link.target): link.count for link in response.links}
    assert reported == cross_repository_counts(linked)


def test_kinds_are_never_merged(linked: Workspace) -> None:
    response = overview(linked)
    for link in response.links:
        assert {kind_of(str(count.type)) for count in link.types} == {str(link.kind)}
        assert sum(count.count for count in link.types) == link.count
    by_pair = {(link.source, link.target, link.kind): link for link in response.links}
    # one pair, two claims: the code that uses the package, and the manifest declaring it
    observed = by_pair[("app", "contracts", LinkKind.OBSERVED)]
    assert {str(count.type): count.count for count in observed.types} == {
        "IMPORTS": 4,
        "USES": 6,
    }
    assert by_pair[("app", "contracts", LinkKind.DEPENDENCY)].count == 1
    # a caller reaching a handler by route is HTTP, and only HTTP
    http = by_pair[("http-web", "http-api", LinkKind.HTTP)]
    assert [(str(t.type), t.count) for t in http.types] == [("CALLS_HTTP_HANDLER", 1)]
    assert ("http-web", "http-api", LinkKind.OBSERVED) not in by_pair


def test_examples_are_bounded_and_each_resolves_to_its_own_link(linked: Workspace) -> None:
    service = CapabilityService(linked)
    response = overview(linked)
    many = next(
        link
        for link in response.links
        if (link.source, link.target, link.kind) == ("refusals", "contracts", LinkKind.DEPENDENCY)
    )
    assert many.count == 4
    assert len(many.examples) == MAX_EXAMPLES
    for link in response.links:
        assert 1 <= len(link.examples) <= min(MAX_EXAMPLES, link.count)
        assert len(set(link.examples)) == len(link.examples)
        for ref in link.examples:
            edge = service.topology_edge(TopologyEdgeRequest(context=GLOBAL, edge_ref=ref)).edge
            assert kind_of(str(edge.type)) == str(link.kind)
            assert (edge.source.repository_slug, edge.target.repository_slug) == (
                link.source,
                link.target,
            )


def test_dependency_links_name_the_packages_their_edges_were_drawn_from(
    linked: Workspace,
) -> None:
    links = {(link.source, link.target, link.kind): link for link in overview(linked).links}
    kit = links[("kit-user", "mono-kit", LinkKind.DEPENDENCY)]
    assert [(p.name, p.declared_at, p.provided_at) for p in kit.packages] == [
        ("@kit/p", "kit-user/package.json:4", "mono-kit/packages/p/package.json:2"),
        ("@kit/q", "kit-user/package.json:5", "mono-kit/packages/q/package.json:2"),
    ]
    # two manifests in one repository publish it: the edge stands, the manifest is not guessed
    dup = links[("dup-user", "dup-mono", LinkKind.DEPENDENCY)]
    assert [(p.name, p.provided_at) for p in dup.packages] == [("@fixture/dup-mono", None)]
    for link in links.values():
        assert bool(link.packages) == (link.kind is LinkKind.DEPENDENCY)


def test_a_package_two_repositories_publish_is_listed_not_linked(linked: Workspace) -> None:
    response = overview(linked)
    assert [(a.repository, a.name, a.providers) for a in response.ambiguous] == [
        ("dup-user", "@fixture/dup", ("dup-one", "dup-two"))
    ]
    assert response.ambiguous_total == 1
    assert not any(
        link.source == "dup-user" and link.target in {"dup-one", "dup-two"}
        for link in response.links
    )


def test_a_repository_scope_holds_no_link_to_another_repository(linked: Workspace) -> None:
    response = overview(linked, repository("app"))
    assert response.repositories == ("app",)
    assert response.links == ()
    assert response.ambiguous == ()
    # every registered repository is listed globally, linked or not
    assert "left" in overview(linked).repositories


def test_a_scan_past_its_ceiling_is_reported_incomplete_not_partial(
    linked: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service_module, "ROW_CEILING", 2)
    response = overview(linked)
    assert not response.complete
    assert response.links == ()
