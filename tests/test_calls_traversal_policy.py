"""Established CALLS edges are explicitly traversable, and only explicitly.

The defect this file exists for: the index held roughly 26,000 ``CALLS``
relationships and the relationships capability refused to return any of them. One
tuple — :data:`~repoatlas.graph.traversal.EXPANSION_EDGES` — answered two different
questions at once:

* which edges *automatic* search expansion follows from every exact entity hit;
* which edges a caller may *name* in an explicit relationships request.

``CALLS`` is rightly absent from the first. Expansion runs on every ordinary
search, it labels everything it reaches `DETERMINISTIC`, and a call graph that size
would bury the relationship a question was about under an inventory of the
neighbourhood. But an agent asking "what calls this?" has said which edges it
wants, and answering "that kind is not traversable" about edges the graph plainly
holds is a false statement about the index.

So the sets are separated, and both halves are asserted here: an explicit request
reaches call edges, and nothing else about the funnel or the walk moved.

What enabling traversal does **not** do
---------------------------------------
It exposes edges that already exist. It resolves nothing. An unresolved call
produces no edge at all — it is a row in ``unresolved_refs`` with no destination —
so ``this.courses.byId(courseId)`` does not become a verified edge to
``CoursesRepository.byId``, and nothing here invents one. Every CALLS edge this
returns was written by a parser and resolved through an import the calling file
actually made.
"""

from __future__ import annotations

import pytest
from repoatlas.capabilities.errors import InvalidRelationshipRequestError
from repoatlas.capabilities.relationships import admitted_edge_types
from repoatlas.capabilities.service import CapabilityService
from repoatlas.graph.traversal import (
    CROSS_REPOSITORY_EDGES,
    EXPANSION_EDGES,
    RELATIONSHIP_DEFAULT_EDGES,
    RELATIONSHIP_EDGES,
    expand_from_seeds,
)
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.parsing import EdgeType, EntityType
from repoatlas.models.relationships import RelationshipDirection, RelationshipsRequest
from repoatlas.services.workspace import Workspace

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)


def _ask(workspace: Workspace, target: str, **kwargs: object):
    fields: dict[str, object] = {"context": GLOBAL, "target": target, "max_neighbors": 200}
    fields.update(kwargs)
    return CapabilityService(workspace).relationships(RelationshipsRequest(**fields))  # type: ignore[arg-type]


def _entity_id(workspace: Workspace, key: str) -> int:
    row = workspace.database.execute("SELECT id FROM entities WHERE key = ?", (key,)).fetchone()
    assert row is not None, key
    return int(row["id"])


def _a_calls_edge(workspace: Workspace) -> tuple[str, str]:
    """An established CALLS edge, discovered rather than hard-coded.

    A ``sym:`` key embeds the line its definition sits on, so spelling one out here
    would make this file fail the next time a fixture gains a line.
    """
    row = workspace.database.execute(
        "SELECT s.key AS src, d.key AS dst FROM edges e "
        "JOIN entities s ON s.id = e.src_id "
        "JOIN entities d ON d.id = e.dst_id "
        "WHERE e.type = ? ORDER BY s.key, d.key LIMIT 1",
        (str(EdgeType.CALLS),),
    ).fetchone()
    assert row is not None, "the fixture ecosystem holds no CALLS edge to test with"
    return str(row["src"]), str(row["dst"])


# -- the two policies are separate, and each says what it is for ---------------


def test_calls_is_nameable_but_never_automatically_expanded() -> None:
    assert EdgeType.CALLS in RELATIONSHIP_EDGES
    assert EdgeType.CALLS not in EXPANSION_EDGES
    assert set(RELATIONSHIP_EDGES) >= set(EXPANSION_EDGES)


def test_a_calls_only_request_admits_exactly_calls() -> None:
    """The guard against the quiet failure mode.

    ``expand_neighbors`` applies a type filter only ``if edge_types:`` — an empty
    tuple is no filter at all. So a policy widened without widening the ordering
    that builds this tuple would turn the narrowest possible request into an
    unrestricted walk over every edge type, silently.
    """
    admitted = admitted_edge_types(
        RelationshipsRequest(context=GLOBAL, target="file:x:y.py", edge_types=(EdgeType.CALLS,))
    )
    assert admitted == (EdgeType.CALLS,)
    assert admitted, "an empty tuple would disable the type filter entirely"


def test_the_default_still_excludes_calls() -> None:
    admitted = admitted_edge_types(RelationshipsRequest(context=GLOBAL, target="file:x:y.py"))
    # Search's expansion set and the HTTP handler links (M5a, C7); never CALLS.
    assert admitted == tuple(RELATIONSHIP_DEFAULT_EDGES)
    assert admitted == (*EXPANSION_EDGES, EdgeType.CALLS_HTTP_HANDLER)
    assert EdgeType.CALLS not in admitted


def test_containment_is_still_refused_without_being_asked_for() -> None:
    with pytest.raises(InvalidRelationshipRequestError) as raised:
        admitted_edge_types(
            RelationshipsRequest(
                context=GLOBAL, target="file:x:y.py", edge_types=(EdgeType.DEFINES,)
            )
        )
    assert raised.value.detail is not None
    assert "include_containment" in raised.value.detail


def test_a_type_outside_the_vocabulary_is_rejected_before_policy_runs() -> None:
    """Widening what may be named did not widen what may be spelled.

    The public request carries ``EdgeType`` members, so a relationship kind the
    graph has no word for is malformed at the schema and never reaches the policy
    check. That is where an unsupported type is refused, exactly as before this
    repair; the policy's own refusal stays the default for any member added to
    the vocabulary later without a traversal decision.
    """
    with pytest.raises(ValueError):
        RelationshipsRequest(
            context=GLOBAL,
            target="file:x:y.py",
            edge_types=("WANDERS_TO",),  # type: ignore[arg-type]
        )


# -- an explicit request reaches the edges the index holds ---------------------


def test_an_explicit_calls_request_returns_an_established_edge(initialized: Workspace) -> None:
    source, target = _a_calls_edge(initialized)
    response = _ask(initialized, source, edge_types=(EdgeType.CALLS,))

    assert response.edges, "the graph holds this edge and the capability returned none"
    assert {edge.type for edge in response.edges} == {EdgeType.CALLS}
    assert target in {edge.target.key for edge in response.edges}


def test_the_default_answer_for_the_same_target_holds_no_calls(initialized: Workspace) -> None:
    """Explicit traversal did not leak into the default."""
    source, _ = _a_calls_edge(initialized)
    response = _ask(initialized, source)
    assert EdgeType.CALLS not in {edge.type for edge in response.edges}
    assert {edge.type for edge in response.edges} <= set(EXPANSION_EDGES)


# -- direction is preserved ----------------------------------------------------


def test_direction_is_preserved(initialized: Workspace) -> None:
    """``a CALLS b`` is a fact about a and b in that order.

    Asking "what does a call?" and "what calls b?" reach the same edge from
    opposite ends, and the stored orientation is the same in both answers.
    """
    source, target = _a_calls_edge(initialized)

    outgoing = _ask(
        initialized,
        source,
        edge_types=(EdgeType.CALLS,),
        direction=RelationshipDirection.OUTGOING,
    )
    assert [(e.source.key, e.target.key) for e in outgoing.edges if e.target.key == target] == [
        (source, target)
    ]

    incoming = _ask(
        initialized,
        target,
        edge_types=(EdgeType.CALLS,),
        direction=RelationshipDirection.INCOMING,
    )
    assert (source, target) in {(e.source.key, e.target.key) for e in incoming.edges}

    # The queried end is what changes, never the edge.
    assert all(e.source.key != target for e in incoming.edges if e.target.key == target)


# -- bounds still bound --------------------------------------------------------


def test_hop_and_neighbour_limits_still_apply(initialized: Workspace) -> None:
    source, _ = _a_calls_edge(initialized)
    one = _ask(initialized, source, edge_types=(EdgeType.CALLS,), hops=1, max_neighbors=1)
    assert len(one.edges) <= 1
    assert one.limits.hops == 1
    assert {edge.hop for edge in one.edges} <= {1}

    wider = _ask(initialized, source, edge_types=(EdgeType.CALLS,), hops=1, max_neighbors=200)
    if len(wider.edges) > 1:
        assert one.truncated, "a cut answer must say it was cut"


def test_a_narrower_request_is_a_prefix_not_a_different_neighbourhood(
    initialized: Workspace,
) -> None:
    source, _ = _a_calls_edge(initialized)
    full = _ask(initialized, source, edge_types=(EdgeType.CALLS,), max_neighbors=200)
    cut = _ask(initialized, source, edge_types=(EdgeType.CALLS,), max_neighbors=1)
    keys = [(e.source.key, str(e.type), e.target.key) for e in full.edges]
    assert [(e.source.key, str(e.type), e.target.key) for e in cut.edges] == keys[: len(cut.edges)]


# -- repository confinement --------------------------------------------------


def test_calls_is_not_a_crossing_edge() -> None:
    assert EdgeType.CALLS not in CROSS_REPOSITORY_EDGES


def test_an_unproven_cross_repository_call_never_leaves_its_repository(
    initialized: Workspace,
) -> None:
    """An edge without package evidence must not become a cross-repository route."""
    source, _ = _a_calls_edge(initialized)
    initialized.database.execute(
        "INSERT INTO edges (src_id, dst_id, type, confidence, parser) VALUES (?, ?, ?, 1.0, ?)",
        (
            _entity_id(initialized, source),
            _entity_id(initialized, "file:ts-service:src/index.ts"),
            str(EdgeType.CALLS),
            "planted",
        ),
    )
    initialized.database.connection.commit()

    response = _ask(
        initialized, source, edge_types=(EdgeType.CALLS,), cross_repository=True, hops=2
    )
    assert "file:ts-service:src/index.ts" not in {edge.target.key for edge in response.edges}
    assert not any(edge.cross_repository for edge in response.edges)


def test_repository_scope_confines_an_explicit_calls_walk(initialized: Workspace) -> None:
    source, _ = _a_calls_edge(initialized)
    slug = source.split(":")[1]
    scoped = CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=slug)
    response = CapabilityService(initialized).relationships(
        RelationshipsRequest(
            context=scoped, target=source, edge_types=(EdgeType.CALLS,), hops=2, max_neighbors=200
        )
    )
    reached = {edge.source.repository_slug for edge in response.edges} | {
        edge.target.repository_slug for edge in response.edges
    }
    assert reached <= {slug, None}


def test_a_repository_less_hub_is_not_a_scope_escape(initialized: Workspace) -> None:
    """A salt command node belongs to no repository, so it never "crosses".

    Two such edges in a row would still leave the repository, which is what
    ``home_repo_id`` exists to stop. Enabling CALLS must not reopen it.
    """
    hubs = initialized.database.execute(
        "SELECT DISTINCT d.key AS key FROM edges e JOIN entities d ON d.id = e.dst_id "
        "WHERE e.type = ? AND d.repo_id IS NULL",
        (str(EdgeType.CALLS),),
    ).fetchall()
    if not hubs:
        pytest.skip("this ecosystem holds no repository-less CALLS target")
    key = str(hubs[0]["key"])
    owner = initialized.database.execute(
        "SELECT s.key AS key FROM edges e JOIN entities s ON s.id = e.src_id "
        "JOIN entities d ON d.id = e.dst_id WHERE e.type = ? AND d.key = ? LIMIT 1",
        (str(EdgeType.CALLS), key),
    ).fetchone()
    slug = str(owner["key"]).split(":")[1]
    scoped = CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=slug)
    response = CapabilityService(initialized).relationships(
        RelationshipsRequest(
            context=scoped,
            target=str(owner["key"]),
            edge_types=(EdgeType.CALLS,),
            hops=2,
            max_neighbors=200,
        )
    )
    reached = {edge.target.repository_slug for edge in response.edges} | {
        edge.source.repository_slug for edge in response.edges
    }
    assert reached <= {slug, None}


# -- ordinary search did not become a call graph ------------------------------


def test_search_expansion_reaches_no_call_edge(initialized: Workspace, workspace_root) -> None:
    """Behavioural, not only structural.

    The seeds below are every exact entity the question names, which is the widest
    the funnel ever expands from. Nothing it reaches may have arrived over a CALLS
    edge, whatever the relationships capability now permits.
    """
    context = initialized.resolver().resolve(workspace_root)
    outcome = initialized.retriever().retrieve_detailed(
        "Where is SERVICE_HOST configured?", context
    )
    walk = expand_from_seeds(
        initialized.knowledge, outcome.seed_entity_ids, hops=2, fanout=50, seed_limit=20
    )
    assert not [reason for reason in walk.reasons.values() if "calls" in reason]


def test_the_funnel_asks_for_no_edge_set_of_its_own(initialized: Workspace) -> None:
    """Expansion takes the walk's default, so the two policies cannot drift apart
    by someone widening the wrong one."""
    source, target = _a_calls_edge(initialized)
    walk = expand_from_seeds(
        initialized.knowledge, [_entity_id(initialized, source)], hops=2, fanout=50, seed_limit=20
    )
    described = initialized.knowledge.describe_entities(walk.reached)
    callee = _entity_id(initialized, target)
    assert callee not in {
        row.entity_id for row in described if row.entity_type is EntityType.FUNCTION
    }
