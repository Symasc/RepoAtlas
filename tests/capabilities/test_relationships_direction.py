"""Direction semantics for relationships (V2 Slice F).

The one invariant this file exists for:

    Stored direction is a fact about the code.
    Traversal direction is a fact about the question.

``file:scripts:deploy.sh EXPORTS env:SERVICE_HOST`` says the shell script does the
exporting. Asking "what exports SERVICE_HOST?" walks that edge backwards, and the
export does not start happening in the other direction because of how it was
approached. So :attr:`GraphEdge.source` and :attr:`GraphEdge.target` always hold
the stored orientation, and how the walk arrived lives in
:attr:`GraphEdge.traversal_direction`.

Swapping the first to express the second is the tidiest-looking bug available
here: every response would still be well formed, every key would still resolve,
and the graph would quietly assert that an environment variable exports a shell
script. Nothing downstream could detect it, which is why it is pinned on real
fixture edges from both ends rather than on a shape.
"""

from __future__ import annotations

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.parsing import EdgeType
from repoatlas.models.relationships import (
    GraphEdge,
    RelationshipDirection,
    RelationshipsRequest,
    RelationshipsResponse,
)
from repoatlas.services.workspace import Workspace

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)

#: ``export SERVICE_HOST="${SERVICE_HOST:-localhost}"`` — one line of one shell
#: script, asserting that the script exports the variable. Both ends are named
#: here because the whole file is about which of them stays on which side.
DEPLOY = "file:scripts:deploy.sh"
SERVICE_HOST = "env:SERVICE_HOST"


def _service(workspace: Workspace) -> CapabilityService:
    return CapabilityService(workspace)


def _ask(
    workspace: Workspace, target: str, direction: RelationshipDirection, **kwargs: object
) -> RelationshipsResponse:
    return _service(workspace).relationships(
        RelationshipsRequest(
            context=GLOBAL,
            target=target,
            direction=direction,
            max_neighbors=200,
            **kwargs,  # type: ignore[arg-type]
        )
    )


def _pairs(response: RelationshipsResponse) -> set[tuple[str, str, str]]:
    return {(edge.source.key, str(edge.type), edge.target.key) for edge in response.edges}


def _one(response: RelationshipsResponse, edge_type: EdgeType, source: str) -> GraphEdge:
    found = [e for e in response.edges if e.type is edge_type and e.source.key == source]
    assert len(found) == 1, f"expected exactly one {edge_type} from {source}"
    return found[0]


# ---------------------------------------------------------------------------
# CASE 1 / CASE 2 — the same stored edge, asked from either end
# ---------------------------------------------------------------------------


def test_an_outgoing_question_returns_edges_the_target_is_the_source_of(
    initialized: Workspace,
) -> None:
    """CASE 1. ``A -> B`` stored; asked outgoing from A."""
    response = _ask(initialized, DEPLOY, RelationshipDirection.OUTGOING)

    assert response.edges
    assert all(edge.source.key == DEPLOY for edge in response.edges)
    assert (DEPLOY, str(EdgeType.EXPORTS), SERVICE_HOST) in _pairs(response)
    assert all(
        edge.traversal_direction is RelationshipDirection.OUTGOING for edge in response.edges
    )


def test_an_incoming_question_does_not_turn_the_relationship_around(
    initialized: Workspace,
) -> None:
    """CASE 2, the concrete one.

    Asked from the variable's end, the answer must still read *the script exports
    the variable*. If the endpoints were swapped to match the question, this edge
    would assert that an environment variable exports a shell script — which is
    not a fact about anything.
    """
    response = _ask(initialized, SERVICE_HOST, RelationshipDirection.INCOMING)

    edge = _one(response, EdgeType.EXPORTS, DEPLOY)
    assert edge.source.key == DEPLOY
    assert edge.target.key == SERVICE_HOST
    assert edge.traversal_direction is RelationshipDirection.INCOMING

    assert not any(e.source.key == SERVICE_HOST for e in response.edges)
    assert all(edge.target.key == SERVICE_HOST for edge in response.edges)


def test_the_same_edge_is_oriented_identically_from_both_ends(initialized: Workspace) -> None:
    """The strongest form of the invariant: one fact, two questions, one answer.

    Comparing the two responses rather than either one alone is what catches a
    swap that is applied consistently — a bug that looks self-consistent from
    inside a single response and disagrees with itself the moment the graph is
    approached from the other side.
    """
    outgoing = _one(
        _ask(initialized, DEPLOY, RelationshipDirection.OUTGOING), EdgeType.EXPORTS, DEPLOY
    )
    incoming = _one(
        _ask(initialized, SERVICE_HOST, RelationshipDirection.INCOMING), EdgeType.EXPORTS, DEPLOY
    )

    assert (outgoing.source.key, outgoing.target.key) == (incoming.source.key, incoming.target.key)
    assert outgoing.type is incoming.type
    assert outgoing.provenance is not None and incoming.provenance is not None
    assert outgoing.provenance.path == incoming.provenance.path
    assert outgoing.provenance.line_start == incoming.provenance.line_start

    # Only the fact about the walk differs, which is the entire point.
    assert outgoing.traversal_direction is RelationshipDirection.OUTGOING
    assert incoming.traversal_direction is RelationshipDirection.INCOMING


def test_traversal_direction_describes_the_walk_and_never_the_relationship(
    initialized: Workspace,
) -> None:
    """Every incoming answer points at the target; none of them is stored that way."""
    response = _ask(initialized, SERVICE_HOST, RelationshipDirection.INCOMING)

    assert response.edges
    for edge in response.edges:
        assert edge.traversal_direction is RelationshipDirection.INCOMING
        assert edge.target.key == SERVICE_HOST, "an incoming edge was re-pointed at the caller"


# ---------------------------------------------------------------------------
# CASE 3 — both
# ---------------------------------------------------------------------------


def test_both_returns_each_relationship_exactly_once(initialized: Workspace) -> None:
    """CASE 3. A neighbourhood, not a double count.

    ``src/config.py`` is imported by ``app.py`` and imports two modules of its
    own, so it has edges on both sides and a walk that met each edge twice would
    show here.
    """
    response = _ask(initialized, "file:python-service:src/config.py", RelationshipDirection.BOTH)

    keys = [
        (e.source.key, str(e.type), e.target.key, e.provenance.line_start if e.provenance else None)
        for e in response.edges
    ]
    assert len(keys) == len(set(keys)), "a relationship was returned twice"

    directions = {edge.traversal_direction for edge in response.edges}
    assert directions == {RelationshipDirection.INCOMING, RelationshipDirection.OUTGOING}


def test_both_is_exactly_the_union_of_the_two_narrower_questions(initialized: Workspace) -> None:
    """No edge appears only in the wide question, and none goes missing from it."""
    target = "file:python-service:src/config.py"
    both = _pairs(_ask(initialized, target, RelationshipDirection.BOTH))
    outgoing = _pairs(_ask(initialized, target, RelationshipDirection.OUTGOING))
    incoming = _pairs(_ask(initialized, target, RelationshipDirection.INCOMING))

    assert outgoing and incoming
    assert both == outgoing | incoming
    assert not (outgoing & incoming), "one stored edge answered both narrow questions"


def test_both_is_the_default(initialized: Workspace) -> None:
    """The neighbourhood question is what an unqualified ask means."""
    service = _service(initialized)
    target = "file:python-service:src/config.py"
    default = service.relationships(
        RelationshipsRequest(context=GLOBAL, target=target, max_neighbors=200)
    )
    assert _pairs(default) == _pairs(_ask(initialized, target, RelationshipDirection.BOTH))


def test_ordering_does_not_depend_on_the_direction_asked(initialized: Workspace) -> None:
    """The narrow answers are subsequences of the wide one, in the same order.

    Direction narrows which edges are eligible. It is not a second sort key, and a
    caller comparing a filtered view against the full neighbourhood must not have
    to reconcile two orders.
    """
    target = "file:python-service:src/config.py"
    both = [
        (e.source.key, str(e.type), e.target.key)
        for e in _ask(initialized, target, RelationshipDirection.BOTH).edges
    ]
    for direction in (RelationshipDirection.OUTGOING, RelationshipDirection.INCOMING):
        narrow = [
            (e.source.key, str(e.type), e.target.key)
            for e in _ask(initialized, target, direction).edges
        ]
        assert narrow == [item for item in both if item in set(narrow)]


# ---------------------------------------------------------------------------
# A direction with no answer
# ---------------------------------------------------------------------------


def test_an_outgoing_question_about_a_sink_is_empty_and_successful(
    initialized: Workspace,
) -> None:
    """Nothing points *out* of an environment variable, and that is an answer.

    Every edge touching ``env:SERVICE_HOST`` names it as the destination — files
    read it, export it and configure it, and it does nothing to anything. An
    empty result here is the truth about the graph rather than a failure to find
    one, so it is a response and not an error.
    """
    response = _ask(initialized, SERVICE_HOST, RelationshipDirection.OUTGOING)

    assert response.edges == ()
    assert response.evidence == ()
    assert response.target.key == SERVICE_HOST
    assert response.truncated is False

    # And the other direction on the same node is not empty, so this is a fact
    # about orientation rather than about an unreachable node.
    assert _ask(initialized, SERVICE_HOST, RelationshipDirection.INCOMING).edges


# ---------------------------------------------------------------------------
# Routes versus facts
# ---------------------------------------------------------------------------


def test_a_path_follows_the_route_even_where_the_edge_is_stored_backwards(
    initialized: Workspace,
) -> None:
    """``GraphPath`` records where the walk went; the edges keep what they say.

    The chain out of ``src/config.py`` runs through ``src/app.py``, and the edge
    joining them is stored ``app.py IMPORTS config.py`` — the walk crosses it
    against its direction. The path must still read config.py, app.py, and then
    onward, or it is not a route anyone could follow.
    """
    response = _service(initialized).relationships(
        RelationshipsRequest(
            context=GLOBAL,
            target="file:python-service:src/config.py",
            hops=2,
            max_neighbors=200,
        )
    )

    assert response.paths, "no second-hop chain in this corpus"
    path = response.paths[0]
    assert path.nodes[0].key == "file:python-service:src/config.py"
    assert path.nodes[1].key == "file:python-service:src/app.py"
    assert len(path.nodes) == len(path.edges) + 1

    leading = path.edges[0]
    assert leading.source.key == "file:python-service:src/app.py"
    assert leading.target.key == "file:python-service:src/config.py"
    assert leading.traversal_direction is RelationshipDirection.INCOMING


def test_every_path_edge_joins_the_nodes_on_either_side_of_it(initialized: Workspace) -> None:
    """A route is only a route if each step's endpoints are the nodes around it."""
    response = _service(initialized).relationships(
        RelationshipsRequest(
            context=GLOBAL,
            target="file:python-service:src/app.py",
            hops=2,
            max_neighbors=200,
        )
    )

    assert response.paths
    for path in response.paths:
        for index, edge in enumerate(path.edges):
            ends = {edge.source.key, edge.target.key}
            assert {path.nodes[index].key, path.nodes[index + 1].key} == ends


@pytest.mark.parametrize(
    "direction",
    [RelationshipDirection.OUTGOING, RelationshipDirection.INCOMING, RelationshipDirection.BOTH],
)
def test_no_direction_ever_names_the_target_on_the_wrong_side(
    initialized: Workspace, direction: RelationshipDirection
) -> None:
    """Whatever is asked, every returned edge is one the store actually holds."""
    response = _ask(initialized, SERVICE_HOST, direction, hops=2)

    stored = {
        (row["s"], row["t"], row["d"])
        for row in initialized.database.execute(
            "SELECT se.key AS s, e.type AS t, de.key AS d FROM edges e "
            "JOIN entities se ON se.id = e.src_id JOIN entities de ON de.id = e.dst_id"
        ).fetchall()
    }
    for edge in response.edges:
        assert (edge.source.key, str(edge.type), edge.target.key) in stored
