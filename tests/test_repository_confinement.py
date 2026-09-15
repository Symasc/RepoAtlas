"""``cross_repository=false`` keeps a walk inside the target's repository.

The request field has always promised exactly that: *whether the walk may leave
the target's repository*. The walk kept the promise only for direct edges.

A shared node — ``env:SERVICE_HOST``, and now ``http-endpoint:…`` — belongs to no
repository. So neither edge touching it "crossed", and a two-hop walk from a
service went to the hub and straight out to every other repository touching it,
with crossing switched off. Nothing pinned that path: the evaluation case that
reaches seven repositories through a hub starts *at* the hub, which has no home
repository to leave.

The rule now matches the field:

* from a repository-owned target with ``cross_repository=false``, no edge whose
  far end belongs to another repository is followed — through a hub or
  otherwise. Hubs themselves remain reachable;
* a walk rooted at a hub has no home repository and sees every side, as before;
* ``cross_repository=true`` reaches the far side of a hub;
* retrieval's expansion is not a relationships request and is unchanged.

The same rule for environment variables and HTTP operations, because a special
case for one kind of hub would make the flag mean two things.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.graph import keys
from repoatlas.graph.traversal import expand_from_seeds, walk
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.relationships import (
    RelationshipDirection,
    RelationshipsRequest,
    RelationshipsResponse,
)
from repoatlas.services.workspace import Workspace
from tests.http_corpus import end, entity_key, index_corpus, relationships

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)


def _ask(
    workspace: Workspace, target: str, *, cross_repository: bool, hops: int = 2
) -> RelationshipsResponse:
    return CapabilityService(workspace).relationships(
        RelationshipsRequest(
            context=GLOBAL,
            target=target,
            direction=RelationshipDirection.BOTH,
            hops=hops,
            cross_repository=cross_repository,
        )
    )


def _repositories(response: RelationshipsResponse) -> set[str | None]:
    return {ref.repository_slug for edge in response.edges for ref in (edge.source, edge.target)}


def _service_host_sides(workspace: Workspace) -> dict[str, set[str]]:
    """Every repository-owned entity with an edge into ``env:SERVICE_HOST``, by repository.

    Read from storage, so the expectation is the index's own edge set rather than
    a count remembered from one fixture corpus.
    """
    rows = workspace.database.execute(
        "SELECT src.key AS key, r.slug AS slug FROM edges e "
        "JOIN entities src ON src.id = e.src_id JOIN entities dst ON dst.id = e.dst_id "
        "JOIN repositories r ON r.id = src.repo_id WHERE dst.key = 'env:SERVICE_HOST' "
        "ORDER BY r.slug, src.key"
    ).fetchall()
    sides: dict[str, set[str]] = {}
    for row in rows:
        sides.setdefault(str(row["slug"]), set()).add(str(row["key"]))
    return sides


def _reader(workspace: Workspace) -> tuple[str, str]:
    """One repository-owned entity that touches ``env:SERVICE_HOST``, and its repository."""
    sides = _service_host_sides(workspace)
    assert len(sides) >= 2, "the corpus must share SERVICE_HOST across repositories"
    slug = sorted(sides)[0]
    return sorted(sides[slug])[0], slug


# -- environment-variable hubs -----------------------------------------------------------


def test_a_service_reading_a_variable_does_not_reach_other_readers_when_confined(
    initialized: Workspace,
) -> None:
    key, slug = _reader(initialized)
    response = _ask(initialized, key, cross_repository=False)
    assert "env:SERVICE_HOST" in {edge.target.key for edge in response.edges}
    assert _repositories(response) <= {slug, None}


def test_crossing_enabled_reaches_the_other_side_of_the_variable(initialized: Workspace) -> None:
    key, slug = _reader(initialized)
    response = _ask(initialized, key, cross_repository=True)
    assert _repositories(response) - {slug, None}


def test_a_walk_rooted_at_the_variable_still_sees_every_repository(initialized: Workspace) -> None:
    """Unchanged: a hub has no home repository to be confined to."""
    response = CapabilityService(initialized).relationships(
        RelationshipsRequest(
            context=GLOBAL,
            target="env:SERVICE_HOST",
            direction=RelationshipDirection.INCOMING,
            cross_repository=False,
        )
    )
    assert _repositories(response) - {None} == set(_service_host_sides(initialized))


def test_retrieval_expansion_is_not_confined(initialized: Workspace) -> None:
    """Search's expansion has no target repository and no crossing flag."""
    seed = initialized.knowledge.get_entities_by_key([keys.env_key("SERVICE_HOST")])[0].entity_id
    reached = expand_from_seeds(initialized.knowledge, [seed], hops=2, fanout=50, seed_limit=5)
    described = initialized.knowledge.describe_entities(reached.reached)
    assert len({row.repository for row in described}) >= 2


def test_the_walk_confines_only_when_given_a_home_repository(initialized: Workspace) -> None:
    identity = initialized.knowledge.get_entities_by_key([_reader(initialized)[0]])[0]
    home = next(
        repo.id
        for repo in initialized.repositories.list_all()
        if repo.slug == identity.repository_slug
    )
    free = walk(
        initialized.knowledge,
        [identity.entity_id],
        hops=2,
        fanout=200,
        seed_limit=1,
        crossing_edges=frozenset(),
    )
    confined = walk(
        initialized.knowledge,
        [identity.entity_id],
        hops=2,
        fanout=200,
        seed_limit=1,
        crossing_edges=frozenset(),
        home_repo_id=home,
    )
    assert {step.edge.dst_repo_id for step in confined.steps} | {
        step.edge.src_repo_id for step in confined.steps
    } <= {home, None}
    assert len(free.steps) > len(confined.steps)


# -- HTTP hubs --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Workspace]:
    workspace = index_corpus(tmp_path_factory.mktemp("http-confinement"))
    yield workspace
    workspace.close()


def test_a_caller_confined_to_its_repository_sees_the_endpoint_and_nothing_beyond(
    corpus: Workspace,
) -> None:
    key = entity_key(corpus, "web", "helloAbsolute")
    response = relationships(corpus, key, hops=2, cross_repository=False)
    assert {end(edge.target) for edge in response.edges} >= {
        "http-endpoint:GET:https://api.example.com/hello"
    }
    assert _repositories(response) <= {"web", None}


def test_crossing_enabled_reaches_the_declarer_and_the_other_caller(corpus: Workspace) -> None:
    key = entity_key(corpus, "web", "helloAbsolute")
    response = relationships(corpus, key, hops=2, cross_repository=True)
    second = {(end(edge.source), edge.type.value) for edge in response.edges if edge.hop == 2}
    assert ("api-a/openapi.yaml", "EXPOSES_HTTP_ENDPOINT") in second
    assert ("pyclient/client/calls.py::hello_absolute", "CALLS_HTTP_ENDPOINT") in second
    # Still no edge between the repositories themselves: they meet on the node.
    assert not any(edge.cross_repository for edge in response.edges)


def test_a_provider_confined_to_its_repository_keeps_its_own_second_hop(corpus: Workspace) -> None:
    """Confinement removes other repositories, not the target's own facts."""
    key = entity_key(corpus, "api-a", "hello")
    response = relationships(corpus, key, hops=2, cross_repository=False)
    sources = {end(edge.source) for edge in response.edges}
    assert "api-a/openapi.yaml" in sources
    assert _repositories(response) <= {"api-a", None}
