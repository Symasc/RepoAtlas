"""Freshness describes every repository the response drew on, not only the scope.

Freshness is how an agent decides what a citation is worth. The LVL AuthZ audit
found a repository-scoped call supplying evidence from another repository under a
freshness block that named only the scope — so the agent held bytes from `pitch`
and was told how fresh `lvl` was. The two sides of the answer disagreed about which
repositories the answer was even about.

Scope reporting is unchanged: ``context.repositories`` still names what the caller
asked for. This is the other half — what the answer actually rests on.
"""

from __future__ import annotations

from repoatlas.capabilities.service import CapabilityService, _edge_repositories
from repoatlas.models.capability import CapabilityContext, CapabilityScope, EvidenceBudget
from repoatlas.models.inspection import InspectRequest, InspectTarget
from repoatlas.models.parsing import EdgeType, EntityType
from repoatlas.models.relationships import (
    EdgeProvenance,
    EntityRef,
    GraphEdge,
    RelationshipsRequest,
    RelationshipsResponse,
)
from repoatlas.models.search import SearchRequest
from repoatlas.services.workspace import Workspace

SERVICE_HOST = "env:SERVICE_HOST"
CONFIG = "file:python-service:src/config.py"


def _scoped(slug: str) -> CapabilityContext:
    return CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=slug)


def _reported(response: object) -> set[str]:
    return {repo.repository_slug for repo in response.freshness.repositories}  # type: ignore[attr-defined]


def _drawn_on(response: RelationshipsResponse) -> set[str]:
    slugs = {item.ref.repository_slug for item in response.evidence}
    for edge in response.edges:
        slugs |= {
            ref.repository_slug
            for ref in (edge.source, edge.target)
            if ref.repository_slug is not None
        }
        if edge.provenance is not None and edge.provenance.repository_slug is not None:
            slugs.add(edge.provenance.repository_slug)
    return slugs


def test_a_walk_from_a_hub_reports_the_repositories_it_reached(initialized: Workspace) -> None:
    """A walk from a hub that was asked to cross reaches past its scope.

    This case used to need no ``cross_repository``: a hub belongs to no
    repository, so a walk rooted at one was confined to nothing and left the scope
    behind on its own. That was the last hub escape and it is now closed, which
    the guard below caught rather than letting this pass while proving nothing.
    Asking to cross is now how a hub-rooted answer draws on another repository —
    and the freshness report still has to say so.
    """
    response = CapabilityService(initialized).relationships(
        RelationshipsRequest(
            context=_scoped("ts-service"), target=SERVICE_HOST, hops=2, cross_repository=True
        )
    )
    drawn = _drawn_on(response)
    assert drawn - {"ts-service"}, "the fixture no longer reaches past the scope"
    assert drawn <= _reported(response)


def test_crossing_repositories_reports_freshness_for_the_far_side(
    initialized: Workspace,
) -> None:
    response = CapabilityService(initialized).relationships(
        RelationshipsRequest(
            context=_scoped("python-service"), target=CONFIG, hops=2, cross_repository=True
        )
    )
    assert _drawn_on(response) <= _reported(response)


def test_freshness_still_matches_the_scope_when_nothing_else_contributed(
    initialized: Workspace,
) -> None:
    """The scope is the floor, never narrowed away by this."""
    response = CapabilityService(initialized).relationships(
        RelationshipsRequest(context=_scoped("python-service"), target=CONFIG, max_neighbors=200)
    )
    assert _reported(response) == {"python-service"}


def test_a_confined_hub_walk_claims_no_repository_it_no_longer_answers_about(
    initialized: Workspace,
) -> None:
    """Freshness widens for what an answer drew on, so it must narrow again with it.

    The set is "the scope, plus whatever else contributed". Once a hub-rooted walk
    is confined, nothing else contributes, and reporting the freshness of
    repositories the answer no longer touches would describe an answer that was
    not given.
    """
    response = CapabilityService(initialized).relationships(
        RelationshipsRequest(context=_scoped("ts-service"), target=SERVICE_HOST, hops=2)
    )
    assert _drawn_on(response) <= {"ts-service"}
    assert _reported(response) == {"ts-service"}


def test_the_scope_is_reported_even_when_it_supplied_nothing(initialized: Workspace) -> None:
    """The scope is the floor. `nested-outer` touches no hub and answers nothing here."""
    response = CapabilityService(initialized).relationships(
        RelationshipsRequest(context=_scoped("nested-outer"), target=SERVICE_HOST, hops=2)
    )
    assert "nested-outer" not in _drawn_on(response)
    assert "nested-outer" in _reported(response)


def test_search_reports_freshness_for_every_repository_it_supplied(
    initialized: Workspace,
) -> None:
    response = CapabilityService(initialized).search(
        SearchRequest(
            question="load_config servicePort getConfig",
            context=_scoped("ts-service"),
            budget=EvidenceBudget(max_items=18),
        )
    )
    supplied = {item.ref.repository_slug for item in response.evidence}
    assert supplied <= _reported(response)
    assert "ts-service" in _reported(response)


def test_inspect_reports_freshness_for_the_repository_it_read(initialized: Workspace) -> None:
    response = CapabilityService(initialized).inspect(
        InspectRequest(
            context=_scoped("python-service"),
            target=InspectTarget(path="src/config.py"),
            line_start=1,
            line_end=5,
        )
    )
    supplied = {segment.ref.repository_slug for segment in response.segments}
    assert supplied <= _reported(response)


def test_an_edge_names_its_repositories_even_when_nothing_was_supplied() -> None:
    """An edge is a fact about both its ends, whether or not either could be quoted."""
    edge = GraphEdge(
        source=EntityRef(
            key="file:repo-a:x.py", type=EntityType.FILE, name="x.py", repository_slug="repo-a"
        ),
        target=EntityRef(
            key="env:SERVICE_HOST",
            type=EntityType.ENV_VAR,
            name="SERVICE_HOST",
            repository_slug=None,
        ),
        type=EdgeType.EXPORTS,
        hop=1,
        provenance=EdgeProvenance(repository_slug="repo-b", path="y.py"),
    )
    assert _edge_repositories([edge], []) == {"repo-a", "repo-b"}
