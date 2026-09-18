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
* a walk rooted at a hub takes its home from the context: under
  ``scope: repository`` it stays in the repository the caller named, and under
  ``scope: global`` there is nothing to stay inside and it sees every side;
* ``cross_repository=true`` reaches the far side of a hub;
* retrieval's expansion is not a relationships request and is unchanged.

The hub-rooted case was the last one open. A shared target has no "target's
repository" for the field to speak of, so the walk was confined to nothing even
when the context named a repository — and a call scoped to one repository came
back describing several. The context is the caller saying which repository the
question is about, so when the target cannot supply a home, it does.

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
    workspace: Workspace,
    target: str,
    *,
    cross_repository: bool,
    hops: int = 2,
    context: CapabilityContext = GLOBAL,
) -> RelationshipsResponse:
    return CapabilityService(workspace).relationships(
        RelationshipsRequest(
            context=context,
            target=target,
            direction=RelationshipDirection.BOTH,
            hops=hops,
            cross_repository=cross_repository,
        )
    )


def _scoped(slug: str) -> CapabilityContext:
    return CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=slug)


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


def test_a_hub_rooted_walk_stays_in_the_repository_the_context_named(
    initialized: Workspace,
) -> None:
    """A hub has no repository of its own, so the caller's is the one it is in.

    This is the last way a repository-scoped answer left its repository. The
    target is shared, so the "target's repository" that ``cross_repository``
    speaks of does not exist — and a walk with nothing to stay inside went to the
    hub and straight out into every repository touching it, under a context that
    named one. Which repository the answer is about is then a question the two
    halves of the response disagree about.
    """
    sides = _service_host_sides(initialized)
    here, elsewhere = sorted(sides)[0], sorted(sides)[1]
    response = _ask(initialized, "env:SERVICE_HOST", cross_repository=False, context=_scoped(here))
    assert "env:SERVICE_HOST" in {edge.target.key for edge in response.edges} | {
        edge.source.key for edge in response.edges
    }, "the hub itself is still the thing being asked about"
    assert _repositories(response) <= {here, None}
    assert elsewhere not in _repositories(response)


def test_the_context_decides_which_side_of_the_hub_is_answered(
    initialized: Workspace,
) -> None:
    """The same hub, the other context, the other side — and nothing of the first.

    Written as the inverse of the case above because a confinement that is really
    a filter on something else — insertion order, the first registered repository,
    the alphabetically first slug — passes that one and fails this one.
    """
    sides = _service_host_sides(initialized)
    first, second = sorted(sides)[0], sorted(sides)[1]
    response = _ask(
        initialized, "env:SERVICE_HOST", cross_repository=False, context=_scoped(second)
    )
    assert _repositories(response) <= {second, None}
    assert first not in _repositories(response)


def test_crossing_reaches_the_far_side_of_a_hub_the_context_confined(
    initialized: Workspace,
) -> None:
    """Saying ``cross_repository`` still crosses. The boundary is asked past, not removed."""
    sides = _service_host_sides(initialized)
    here = sorted(sides)[0]
    response = _ask(initialized, "env:SERVICE_HOST", cross_repository=True, context=_scoped(here))
    assert _repositories(response) - {here, None}


def test_a_repository_owned_target_is_confined_by_its_own_repository(
    initialized: Workspace,
) -> None:
    """Unchanged, and asserted under a repository context rather than only globally.

    A target that belongs to a repository brings its own home, which is what the
    HTTP linkage work established. The context does not override it — under this
    scope they are the same repository anyway, because a scoped call cannot resolve
    a target belonging to another one.
    """
    key, slug = _reader(initialized)
    response = _ask(initialized, key, cross_repository=False, context=_scoped(slug))
    assert "env:SERVICE_HOST" in {edge.target.key for edge in response.edges}
    assert _repositories(response) <= {slug, None}


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


ENDPOINT = "http-endpoint:GET:https://api.example.com/hello"


def test_an_endpoint_rooted_walk_stays_in_the_repository_the_context_named(
    corpus: Workspace,
) -> None:
    """The same rule for the other kind of hub, because the flag means one thing.

    An HTTP operation is shared exactly as an environment variable is: the caller,
    the declarer and the second caller all meet on it and none of them owns it.
    Asked from inside ``web``, this is "what does my repository do with this
    endpoint", and the answer is not allowed to be "and here is what two other
    repositories do with it".
    """
    response = relationships(
        corpus, ENDPOINT, hops=2, cross_repository=False, context=_scoped("web")
    )
    assert ENDPOINT in {end(edge.source) for edge in response.edges} | {
        end(edge.target) for edge in response.edges
    }
    assert _repositories(response) <= {"web", None}


def test_the_context_decides_which_caller_of_an_endpoint_is_answered(
    corpus: Workspace,
) -> None:
    """The inverse, from the other caller's side."""
    response = relationships(
        corpus, ENDPOINT, hops=2, cross_repository=False, context=_scoped("pyclient")
    )
    assert _repositories(response) <= {"pyclient", None}
    assert "web" not in _repositories(response)


def test_crossing_from_an_endpoint_still_reaches_every_side(corpus: Workspace) -> None:
    """Scoped, but asked past: the declarer and the other caller come back."""
    response = relationships(
        corpus, ENDPOINT, hops=2, cross_repository=True, context=_scoped("web")
    )
    assert {"api-a", "pyclient"} <= {slug for slug in _repositories(response) if slug is not None}


def test_a_globally_rooted_endpoint_walk_still_sees_every_repository(corpus: Workspace) -> None:
    """Unchanged: in global scope there is no repository to be confined to."""
    response = relationships(corpus, ENDPOINT, hops=2, cross_repository=False)
    assert {"web", "api-a", "pyclient"} <= {
        slug for slug in _repositories(response) if slug is not None
    }
