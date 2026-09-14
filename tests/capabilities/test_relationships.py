"""The deterministic relationships capability (V2 Slice F).

Everything this capability returns was written by a parser or resolved by the
linker from an exact match. Nothing here is retrieved, scored or ranked, so the
questions this file asks are not "did it find the right thing" but "did it report
exactly what the index already holds, and nothing else".

Four bounds carry most of the weight, and each is a place where a plausible
convenience would quietly become a fabrication:

* **identity** — a target resolves by exact natural key or not at all, because a
  name lookup answers about whatever happens to share a name;
* **containment** — structural edges are context around the queried entity and
  never a route onward, because expanding them turns a file into an inventory;
* **repository boundaries** — only the shared crossing set may leave a
  repository, so ``cross_repository=True`` never means "ignore boundaries";
* **truncation** — the cut falls on facts about the repositories, so a narrower
  answer is a shorter one rather than a different neighbourhood.

The read-only half at the end is the load-bearing one for everything above: a
capability that could write could make any of these assertions true after the
fact.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from repoatlas.capabilities.errors import (
    EntityAmbiguousError,
    EntityNotFoundError,
    InvalidRelationshipRequestError,
)
from repoatlas.capabilities.service import CapabilityService
from repoatlas.graph.traversal import CONTAINMENT_EDGES, CROSS_REPOSITORY_EDGES, EXPANSION_EDGES
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.freshness import FreshnessDepth
from repoatlas.models.parsing import EdgeType
from repoatlas.models.relationships import (
    MAX_HOPS,
    MAX_NEIGHBORS_CEILING,
    RelationshipDirection,
    RelationshipsRequest,
    RelationshipsResponse,
)
from repoatlas.services.workspace import Workspace
from tests.conftest import fact_snapshot

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)
CONFIG = "file:python-service:src/config.py"
APP = "file:python-service:src/app.py"
DEPLOY = "file:scripts:deploy.sh"
SERVICE_HOST = "env:SERVICE_HOST"


def _service(workspace: Workspace) -> CapabilityService:
    return CapabilityService(workspace)


def _ask(workspace: Workspace, target: str, **kwargs: object) -> RelationshipsResponse:
    fields: dict[str, object] = {"context": GLOBAL, "target": target, "max_neighbors": 200}
    fields.update(kwargs)
    return _service(workspace).relationships(RelationshipsRequest(**fields))  # type: ignore[arg-type]


def _triples(response: RelationshipsResponse) -> set[tuple[str, str, str]]:
    return {(e.source.key, str(e.type), e.target.key) for e in response.edges}


def _entity_id(workspace: Workspace, key: str) -> int:
    row = workspace.database.execute("SELECT id FROM entities WHERE key = ?", (key,)).fetchone()
    assert row is not None, key
    return int(row["id"])


# ---------------------------------------------------------------------------
# Exact identity
# ---------------------------------------------------------------------------


def test_an_exact_key_resolves_to_its_own_neighbourhood(initialized: Workspace) -> None:
    response = _ask(initialized, CONFIG)

    assert response.target.key == CONFIG
    assert response.target.repository_slug == "python-service"
    assert response.target.path == "src/config.py"
    assert response.target.positional is False
    assert response.edges


def test_an_interned_target_belongs_to_no_repository(initialized: Workspace) -> None:
    """An environment variable is not in a repository, and is not given one."""
    response = _ask(initialized, SERVICE_HOST)

    assert response.target.key == SERVICE_HOST
    assert response.target.repository_slug is None
    assert response.target.path is None
    assert response.target.positional is False


def test_a_symbol_handle_is_marked_positional(initialized: Workspace) -> None:
    response = _ask(initialized, "sym:python-service:src/config.py:load_config:16")
    assert response.target.positional is True


def test_a_display_name_is_not_a_key(initialized: Workspace) -> None:
    """The fuzzy fallback that must not exist.

    ``SERVICE_HOST`` is the *name* of an entity whose key is
    ``env:SERVICE_HOST``, and a name lookup would happily resolve it. Resolving a
    handle by name is how a caller ends up being answered about a different
    definition than the one they hold — so a name is simply not a key here.
    """
    for name in ("SERVICE_HOST", "config.py", "load_config", "src/config.py"):
        with pytest.raises(EntityNotFoundError):
            _ask(initialized, name)


def test_an_unknown_key_is_refused_rather_than_answered_empty(initialized: Workspace) -> None:
    """A missing entity and an entity with no relationships are different answers."""
    with pytest.raises(EntityNotFoundError):
        _ask(initialized, "env:NO_SUCH_VARIABLE_ANYWHERE")


def test_an_ambiguous_key_is_reported_and_never_decided(initialized: Workspace) -> None:
    """``entities.key`` is unique only for interned rows, so a collision is possible.

    Picking one of two would answer confidently about a file the caller did not
    name, and the caller would have no way to tell.
    """
    original = _entity_id(initialized, CONFIG)
    row = initialized.database.execute(
        "SELECT type, key, repo_id, file_id, name, qualified_name, line_start, line_end "
        "FROM entities WHERE id = ?",
        (original,),
    ).fetchone()
    other_file = initialized.database.execute(
        "SELECT id FROM files WHERE rel_path = 'src/app.py'"
    ).fetchone()
    assert other_file is not None
    initialized.database.execute(
        "INSERT INTO entities (type, key, repo_id, file_id, name, qualified_name, "
        "line_start, line_end) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row["type"],
            row["key"],
            row["repo_id"],
            other_file["id"],
            row["name"],
            row["qualified_name"],
            row["line_start"],
            row["line_end"],
        ),
    )
    initialized.database.connection.commit()

    with pytest.raises(EntityAmbiguousError) as raised:
        _ask(initialized, CONFIG)
    assert len(raised.value.candidates) == 2


def test_a_target_outside_the_asked_scope_is_absent_from_it(initialized: Workspace) -> None:
    """A scoped answer is only worth having if the scope is not advisory."""
    scoped = CapabilityContext(scope=CapabilityScope.REPOSITORY, repository="ts-service")
    with pytest.raises(EntityNotFoundError):
        _service(initialized).relationships(
            RelationshipsRequest(context=scoped, target=CONFIG, max_neighbors=200)
        )


def test_an_interned_target_is_reachable_from_every_scope(initialized: Workspace) -> None:
    """It belongs to no repository, so no repository scope excludes it."""
    scoped = CapabilityContext(scope=CapabilityScope.REPOSITORY, repository="ts-service")
    response = _service(initialized).relationships(
        RelationshipsRequest(context=scoped, target=SERVICE_HOST, max_neighbors=200)
    )
    assert response.target.key == SERVICE_HOST


def test_the_search_handle_reaches_relationships_verbatim(initialized: Workspace) -> None:
    """CASE 10. The Slice D handoff, with no string surgery in between."""
    from repoatlas.models.search import SearchRequest

    service = _service(initialized)
    found = service.search(
        SearchRequest(question="Where is SERVICE_HOST configured?", context=GLOBAL)
    )
    handles = {handle.key for handle in found.entities}
    assert SERVICE_HOST in handles

    for key in handles:
        response = service.relationships(
            RelationshipsRequest(context=GLOBAL, target=key, max_neighbors=200)
        )
        assert response.target.key == key


# ---------------------------------------------------------------------------
# Hops and paths
# ---------------------------------------------------------------------------


def test_two_hops_is_the_ceiling_and_a_deeper_request_is_malformed(
    initialized: Workspace,
) -> None:
    with pytest.raises(ValueError):
        RelationshipsRequest(context=GLOBAL, target=CONFIG, hops=MAX_HOPS + 1)
    with pytest.raises(ValueError):
        RelationshipsRequest(context=GLOBAL, target=CONFIG, hops=0)


def test_one_hop_is_the_default_and_reaches_nothing_beyond_it(initialized: Workspace) -> None:
    response = _ask(initialized, APP)
    assert response.limits.hops == 1
    assert {edge.hop for edge in response.edges} == {1}
    assert response.paths == ()


def test_a_second_hop_carries_the_chain_that_reached_it(initialized: Workspace) -> None:
    """CASE 4. ``app.py -> config.py -> module:os`` — the real route, in full."""
    response = _ask(initialized, APP, hops=2)

    hops = {edge.hop for edge in response.edges}
    assert hops == {1, 2}
    assert response.paths

    routes = {tuple(node.key for node in path.nodes) for path in response.paths}
    assert (APP, CONFIG, "module:os") in routes
    for path in response.paths:
        assert len(path.nodes) == 3
        assert len(path.edges) == 2


def test_no_synthetic_edge_joins_the_ends_of_a_path(initialized: Workspace) -> None:
    """CASE 4. A two-hop route is not evidence of a one-hop relationship.

    Collapsing ``A -> B -> C`` into ``A -> C`` would be the single most useful
    lie this capability could tell, and every key in it would still resolve.
    """
    response = _ask(initialized, APP, hops=2)

    stored = {
        (row["s"], row["d"])
        for row in initialized.database.execute(
            "SELECT se.key AS s, de.key AS d FROM edges e "
            "JOIN entities se ON se.id = e.src_id JOIN entities de ON de.id = e.dst_id"
        ).fetchall()
    }
    for edge in response.edges:
        assert (edge.source.key, edge.target.key) in stored

    ends = {(path.nodes[0].key, path.nodes[-1].key) for path in response.paths}
    assert ends
    for first, last in ends:
        assert (first, last) not in {
            (e.source.key, e.target.key) for e in response.edges if e.hop == 1
        }


def test_a_first_hop_edge_is_its_own_explanation(initialized: Workspace) -> None:
    """Paths exist to explain results the caller did not ask about directly."""
    response = _ask(initialized, APP, hops=2)
    explained = [edge for path in response.paths for edge in path.edges]
    assert all(edge.hop in {1, 2} for edge in explained)
    assert {path.edges[-1].hop for path in response.paths} == {2}


# ---------------------------------------------------------------------------
# CASE 5 / CASE 6 — containment
# ---------------------------------------------------------------------------


def test_containment_is_absent_by_default(initialized: Workspace) -> None:
    """CASE 5. ``src/config.py`` declares four symbols and shows none of them."""
    response = _ask(initialized, CONFIG, hops=2)
    assert response.edges
    assert not {edge.type for edge in response.edges} & CONTAINMENT_EDGES


def test_containment_appears_as_structural_context_when_asked_for(
    initialized: Workspace,
) -> None:
    """CASE 6. The symbols the queried file declares, and only those."""
    response = _ask(initialized, CONFIG, include_containment=True)

    defines = [edge for edge in response.edges if edge.type is EdgeType.DEFINES]
    assert defines
    assert all(edge.source.key == CONFIG for edge in defines)
    assert all(edge.target.key.startswith("sym:python-service:src/config.py:") for edge in defines)
    assert all(edge.hop == 1 for edge in defines)


def test_containment_never_becomes_a_second_hop_route(initialized: Workspace) -> None:
    """CASE 6, the explosion pin.

    ``src/config.py`` declares ``load_config``, and ``load_config`` is the symbol
    that reads ``SERVICE_HOST``. If containment were expanded through, that READS
    edge — and every other relationship of every symbol in the file — would arrive
    at hop two. The relationship set must be identical with the flag on and off;
    only the structural context around the queried entity may differ.
    """
    without = _ask(initialized, CONFIG, hops=2)
    with_containment = _ask(initialized, CONFIG, hops=2, include_containment=True)

    relationships = [e for e in with_containment.edges if e.type not in CONTAINMENT_EDGES]
    assert {(e.source.key, str(e.type), e.target.key) for e in relationships} == _triples(without)

    reached = {edge.target.key for edge in with_containment.edges} | {
        edge.source.key for edge in with_containment.edges
    }
    assert SERVICE_HOST not in reached, "containment became a route into the wider graph"
    assert EdgeType.READS not in {edge.type for edge in with_containment.edges}


def test_containment_is_shown_around_the_target_and_not_around_its_neighbours(
    initialized: Workspace,
) -> None:
    """A one-hop relaxation, taken literally.

    ``src/app.py`` declares five symbols. It is one hop from ``src/config.py``, so
    a containment query that reached past the seed would drag all five in as
    context nobody asked for — an inventory of the neighbourhood rather than of
    the entity.
    """
    response = _ask(initialized, CONFIG, hops=2, include_containment=True)

    containment = [edge for edge in response.edges if edge.type in CONTAINMENT_EDGES]
    assert containment
    assert {edge.source.key for edge in containment} == {CONFIG}
    assert all(edge.hop == 1 for edge in containment)


def test_the_containment_set_is_the_shared_one(initialized: Workspace) -> None:
    """§11: use the Slice 0 set; do not restate it in capability code."""
    source = Path("repoatlas/capabilities/relationships.py").read_text(encoding="utf-8")
    assert "from repoatlas.graph.traversal import" in source
    assert "CONTAINS" not in source
    assert frozenset({EdgeType.CONTAINS, EdgeType.DEFINES}) == CONTAINMENT_EDGES


# ---------------------------------------------------------------------------
# Edge-type filtering
# ---------------------------------------------------------------------------


def test_naming_edge_types_narrows_without_inventing(initialized: Workspace) -> None:
    response = _ask(initialized, CONFIG, edge_types=(EdgeType.IMPORTS,))
    assert response.edges
    assert {edge.type for edge in response.edges} == {EdgeType.IMPORTS}
    assert _triples(response) <= _triples(_ask(initialized, CONFIG))


def test_an_untraversable_edge_type_is_refused_not_answered_empty(
    initialized: Workspace,
) -> None:
    """ "No relationships of that kind" and "that kind is not traversable" differ.

    Only the second has a remedy, and only an error can name it.
    """
    with pytest.raises(InvalidRelationshipRequestError) as raised:
        _ask(initialized, CONFIG, edge_types=(EdgeType.CALLS,))
    assert raised.value.edge_types == ("CALLS",)


def test_a_containment_type_is_refused_unless_it_was_asked_for(initialized: Workspace) -> None:
    """And the refusal names the exact remedy, because here there is one."""
    with pytest.raises(InvalidRelationshipRequestError) as raised:
        _ask(initialized, CONFIG, edge_types=(EdgeType.DEFINES,))
    assert raised.value.detail is not None
    assert "include_containment" in raised.value.detail

    allowed = _ask(initialized, CONFIG, edge_types=(EdgeType.DEFINES,), include_containment=True)
    assert {edge.type for edge in allowed.edges} == {EdgeType.DEFINES}


def test_an_empty_edge_type_tuple_is_malformed(initialized: Workspace) -> None:
    with pytest.raises(ValueError):
        RelationshipsRequest(context=GLOBAL, target=CONFIG, edge_types=())


def test_the_default_policy_is_the_shared_expansion_set(initialized: Workspace) -> None:
    response = _ask(initialized, SERVICE_HOST, hops=2)
    assert {edge.type for edge in response.edges} <= set(EXPANSION_EDGES)


# ---------------------------------------------------------------------------
# CASE 7 / CASE 8 — repository boundaries
# ---------------------------------------------------------------------------


def test_cross_repository_traversal_is_off_by_default(initialized: Workspace) -> None:
    """CASE 7. ``repo:python-service DEPENDS_ON repo:common-lib`` stays out."""
    response = _ask(initialized, "repo:python-service")

    assert response.edges
    assert not any(edge.cross_repository for edge in response.edges)
    assert "repo:common-lib" not in {edge.target.key for edge in response.edges}


def test_an_enabled_crossing_edge_is_marked_and_keeps_both_slugs(
    initialized: Workspace,
) -> None:
    """CASE 8. Marked explicitly, with each end still naming its own repository."""
    response = _ask(initialized, "repo:python-service", cross_repository=True)

    crossing = [edge for edge in response.edges if edge.cross_repository]
    assert len(crossing) == 1
    edge = crossing[0]
    assert edge.type is EdgeType.DEPENDS_ON
    assert edge.source.key == "repo:python-service"
    assert edge.target.key == "repo:common-lib"
    assert edge.source.repository_slug == "python-service"
    assert edge.target.repository_slug == "common-lib"


def test_only_the_shared_crossing_set_may_leave_a_repository(initialized: Workspace) -> None:
    """CASE 8. ``cross_repository=True`` is not "ignore repository boundaries".

    A ``DEFINES`` edge between two repositories is planted here because the
    indexer produces none — it is exactly the edge a shared symbol name could
    otherwise be used to wander across, and it must stay out even with crossing
    enabled *and* containment enabled.
    """
    initialized.database.execute(
        "INSERT INTO edges (src_id, dst_id, type, confidence, parser) VALUES (?, ?, ?, 1.0, ?)",
        (
            _entity_id(initialized, CONFIG),
            _entity_id(initialized, "file:ts-service:src/index.ts"),
            str(EdgeType.DEFINES),
            "planted",
        ),
    )
    initialized.database.connection.commit()

    assert EdgeType.DEFINES not in CROSS_REPOSITORY_EDGES
    response = _ask(initialized, CONFIG, cross_repository=True, include_containment=True)

    assert "file:ts-service:src/index.ts" not in {edge.target.key for edge in response.edges}
    assert not any(
        edge.cross_repository and edge.type in CONTAINMENT_EDGES for edge in response.edges
    )


def test_a_repository_whose_display_name_differs_from_its_slug_is_named_by_slug(
    initialized: Workspace,
) -> None:
    """CASE 9. ``nested-inner`` is displayed one way and identified another."""
    response = _ask(
        initialized,
        "file:nested-outer/tools/nested-inner:inner.py",
        include_containment=True,
    )

    assert response.target.repository_slug == "nested-outer/tools/nested-inner"
    assert response.target.name == "inner.py"
    for edge in response.edges:
        for end in (edge.source, edge.target):
            if end.repository_slug is not None:
                assert end.key.split(":")[1].startswith(end.repository_slug.split("/")[0])


def test_entities_sharing_a_display_name_stay_distinct(initialized: Workspace) -> None:
    """CASE 9. Two symbols called ``start`` in two repositories are two entities."""
    keys = [
        "sym:python-service:src/app.py:Application.start:12",
        "sym:ts-service:src/index.ts:start:4",
    ]
    targets = [_ask(initialized, key).target for key in keys]

    assert {t.name for t in targets} == {"start"}
    assert {t.key for t in targets} == set(keys)
    assert {t.repository_slug for t in targets} == {"python-service", "ts-service"}

    # And the neighbourhoods do not bleed into one another.
    ts_edges = _ask(initialized, keys[1]).edges
    assert ts_edges
    assert all("python-service" not in edge.target.key for edge in ts_edges)


# ---------------------------------------------------------------------------
# CASE 13 — deterministic bounding
# ---------------------------------------------------------------------------


def test_a_narrower_request_returns_a_prefix_of_the_wider_one(initialized: Workspace) -> None:
    """The property that makes truncation a narrowing rather than a re-answer."""
    full = _ask(initialized, SERVICE_HOST)
    assert len(full.edges) > 3

    for size in range(1, len(full.edges) + 1):
        cut = _ask(initialized, SERVICE_HOST, max_neighbors=size)
        assert len(cut.edges) == min(size, len(full.edges))
        assert _order(cut) == _order(full)[:size]
        assert cut.truncated is (size < len(full.edges))
        assert cut.has_more is cut.truncated


def test_the_surviving_neighbours_do_not_depend_on_insertion_order(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The order the store happens to return rows in must not decide the answer.

    An index rebuilt from the same repositories in a different file order fills
    its tables differently. If that changed which three neighbours a caller sees,
    two correct indexes would answer the same question differently and neither
    would be wrong — which is the failure that makes a truncated graph answer
    worthless.
    """
    from repoatlas.storage.sqlite.stores import SqliteKnowledgeStore

    baseline = _order(_ask(initialized, SERVICE_HOST, max_neighbors=3))
    real = SqliteKnowledgeStore.expand_neighbors

    def reversed_rows(self: SqliteKnowledgeStore, *args: object, **kwargs: object) -> object:
        rows = real(self, *args, **kwargs)  # type: ignore[arg-type]
        return list(reversed(rows))

    monkeypatch.setattr(SqliteKnowledgeStore, "expand_neighbors", reversed_rows)
    assert _order(_ask(initialized, SERVICE_HOST, max_neighbors=3)) == baseline


def test_the_documented_order_is_the_order(initialized: Workspace) -> None:
    """§13: hop, then edge type, then both natural keys, then provenance position."""
    response = _ask(initialized, SERVICE_HOST, hops=2)
    keys = [
        (
            edge.hop,
            str(edge.type),
            edge.source.key,
            edge.target.key,
            edge.provenance.line_start if edge.provenance and edge.provenance.line_start else 0,
        )
        for edge in response.edges
    ]
    assert keys == sorted(keys)


def test_max_neighbors_is_clamped_to_the_server_ceiling(initialized: Workspace) -> None:
    response = _ask(initialized, SERVICE_HOST, max_neighbors=MAX_NEIGHBORS_CEILING * 10)
    assert response.limits.max_neighbors == MAX_NEIGHBORS_CEILING
    assert response.limits.clamped == ("max_neighbors",)
    assert response.limits.ceiling_max_neighbors == MAX_NEIGHBORS_CEILING


def test_a_request_within_the_ceiling_is_honoured_unchanged(initialized: Workspace) -> None:
    response = _ask(initialized, SERVICE_HOST, max_neighbors=2)
    assert response.limits.max_neighbors == 2
    assert response.limits.clamped == ()


def test_a_non_positive_neighbour_count_is_malformed(initialized: Workspace) -> None:
    with pytest.raises(ValueError):
        RelationshipsRequest(context=GLOBAL, target=CONFIG, max_neighbors=0)


def test_a_blank_target_is_malformed(initialized: Workspace) -> None:
    with pytest.raises(ValueError):
        RelationshipsRequest(context=GLOBAL, target="   ")


# ---------------------------------------------------------------------------
# CASE 17 / CASE 18 — empty answers and stored depth
# ---------------------------------------------------------------------------


def test_zero_relationships_is_a_successful_answer(initialized: Workspace) -> None:
    """CASE 17. A valid entity nothing points out of."""
    response = _ask(initialized, "module:os", direction=RelationshipDirection.OUTGOING)

    assert response.edges == ()
    assert response.paths == ()
    assert response.evidence == ()
    assert response.truncated is False
    assert response.target.key == "module:os"
    assert response.investigation_id


def test_a_stored_depth_request_runs_no_git(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CASE 18. Stored freshness answers from the index alone."""
    import subprocess

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("relationships shelled out at STORED depth")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "check_output", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)

    response = _ask(initialized, CONFIG, depth=FreshnessDepth.STORED)
    assert response.freshness.depth is FreshnessDepth.STORED
    assert response.edges


def test_freshness_covers_the_repositories_in_scope(initialized: Workspace) -> None:
    scoped = CapabilityContext(scope=CapabilityScope.REPOSITORY, repository="python-service")
    response = _service(initialized).relationships(
        RelationshipsRequest(context=scoped, target=CONFIG, max_neighbors=200)
    )
    assert {r.repository_slug for r in response.freshness.repositories} == {"python-service"}
    assert response.context.repositories == ("python-service",)


# ---------------------------------------------------------------------------
# §21 / §29 — what may appear in the answer
# ---------------------------------------------------------------------------


def test_no_storage_identifier_reaches_the_response(initialized: Workspace) -> None:
    """§29. Row ids are allocation order; a reindex renumbers every one of them."""
    response = _ask(initialized, SERVICE_HOST, hops=2, include_containment=True)
    payload = json.loads(response.model_dump_json())

    def field_names(value: object) -> set[str]:
        found: set[str] = set()
        if isinstance(value, dict):
            for key, item in value.items():
                found.add(str(key))
                found |= field_names(item)
        elif isinstance(value, list):
            for item in value:
                found |= field_names(item)
        return found

    names = field_names(payload)
    for banned in ("entity_id", "edge_id", "file_id", "chunk_id", "repo_id", "rowid", "id_"):
        assert banned not in names, banned


def test_no_retrieval_vocabulary_reaches_an_edge(initialized: Workspace) -> None:
    """§21. Relationships is not ranking, and must not borrow its words."""
    from repoatlas.models.relationships import GraphEdge

    fields = set(GraphEdge.model_fields)
    for banned in ("score", "relevance", "similarity", "tier", "match", "rank", "distance"):
        assert banned not in fields, banned

    assert "confidence" in fields


def test_confidence_is_the_parsers_own_number(initialized: Workspace) -> None:
    """§21. Reported unchanged, and not reinterpreted as a probability."""
    response = _ask(initialized, SERVICE_HOST)
    stored = {
        (row["s"], row["t"], row["d"]): row["confidence"]
        for row in initialized.database.execute(
            "SELECT se.key AS s, e.type AS t, de.key AS d, e.confidence FROM edges e "
            "JOIN entities se ON se.id = e.src_id JOIN entities de ON de.id = e.dst_id"
        ).fetchall()
    }
    for edge in response.edges:
        assert edge.confidence == stored[(edge.source.key, str(edge.type), edge.target.key)]


def test_no_host_absolute_path_appears_anywhere(initialized: Workspace) -> None:
    """§29. Paths are repository-relative; the host's layout is not public."""
    response = _ask(initialized, SERVICE_HOST, hops=2)
    payload = response.model_dump_json()
    assert str(Path(initialized.database.path).parent) not in payload
    assert str(Path(initialized.database.path)) not in payload
    for edge in response.edges:
        if edge.provenance and edge.provenance.path:
            assert not edge.provenance.path.startswith("/")


# ---------------------------------------------------------------------------
# §28 — read-only proof
# ---------------------------------------------------------------------------


def test_relationships_changes_no_indexed_fact(initialized: Workspace) -> None:
    """The whole capability, against every fact table, before and after."""
    before = fact_snapshot(initialized)

    service = _service(initialized)
    for target, kwargs in (
        (SERVICE_HOST, {"hops": 2}),
        (CONFIG, {"include_containment": True}),
        ("repo:python-service", {"cross_repository": True}),
        (APP, {"hops": 2, "direction": RelationshipDirection.OUTGOING}),
    ):
        service.relationships(
            RelationshipsRequest(
                context=GLOBAL,
                target=target,
                max_neighbors=200,
                **kwargs,  # type: ignore[arg-type]
            )
        )

    assert fact_snapshot(initialized) == before


def test_relationships_makes_no_write_at_all(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every write boundary is fatal for the duration of one call.

    A snapshot comparison proves the facts came back unchanged; this proves no
    write was attempted, which is the stronger statement and the one that stays
    true when a future change makes a write idempotent.
    """
    from repoatlas.storage.sqlite.database import Database

    real_execute = Database.execute
    written: list[str] = []

    def watched(self: Database, sql: str, *args: object, **kwargs: object) -> object:
        head = sql.strip().split(None, 1)[0].upper() if sql.strip() else ""
        if head in {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER"}:
            written.append(sql.strip()[:60])
        return real_execute(self, sql, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Database, "execute", watched)
    _ask(initialized, SERVICE_HOST, hops=2, include_containment=True)

    assert written == []


def test_relationships_never_syncs_or_takes_the_writer_lock(initialized: Workspace) -> None:
    source = Path("repoatlas/capabilities/service.py").read_text(encoding="utf-8")
    body = source[source.index("def relationships(") : source.index("# -- internals ---")]
    code = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))
    assert "writer_lock" not in code
    assert "sync_engine" not in code
    assert "sync(" not in code


def test_relationships_runs_no_retrieval_stage(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A graph question is answered from the graph, not from the funnel."""
    from repoatlas.retrieval.retriever import Retriever
    from repoatlas.storage.sqlite.search import SqliteSearchIndex

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("relationships reached retrieval")

    monkeypatch.setattr(Retriever, "retrieve", forbidden)
    monkeypatch.setattr(Retriever, "retrieve_detailed", forbidden)
    stages = ("search_literal", "find_files_by_path", "find_entities_by_name", "search_entities")
    for name in stages:
        monkeypatch.setattr(SqliteSearchIndex, name, forbidden)

    assert _ask(initialized, SERVICE_HOST, hops=2).edges


def test_a_second_identical_call_returns_an_identical_answer(initialized: Workspace) -> None:
    """Deterministic in the plainest sense: same index, same question, same bytes."""
    service = _service(initialized)

    def once() -> str:
        response = service.relationships(
            RelationshipsRequest(context=GLOBAL, target=SERVICE_HOST, hops=2, max_neighbors=200)
        )
        return response.model_dump_json(exclude={"investigation_id", "ledger", "freshness"})

    assert once() == once()


def _order(response: RelationshipsResponse) -> list[tuple[str, str, str, int]]:
    return [
        (
            e.source.key,
            str(e.type),
            e.target.key,
            e.provenance.line_start if e.provenance and e.provenance.line_start else 0,
        )
        for e in response.edges
    ]


def _external_edge_count(database_path: Path) -> int:
    other = sqlite3.connect(database_path)
    try:
        return int(other.execute("SELECT COUNT(*) FROM edges").fetchone()[0])
    finally:
        other.close()


def test_no_edge_is_created_by_reading(initialized: Workspace) -> None:
    """Checked from a second connection, so it is the committed state."""
    path = Path(initialized.database.path)
    before = _external_edge_count(path)
    _ask(initialized, SERVICE_HOST, hops=2, include_containment=True)
    assert _external_edge_count(path) == before
