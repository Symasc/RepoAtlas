"""The deterministic graph-capability evaluation corpus (V2 Slice F, §33).

A separate corpus from the retrieval one, because the two measure incompatible
things. Retrieval is scored on recall over a ranked, partly associative result:
the right answer is somewhere near the top, and "close" is a grade it can earn.
A graph answer is exact. The correct result is a specific set of edges, oriented
a specific way, and an extra edge is not a near miss — it is a relationship
RepoAtlas asserted about code that does not contain it.

So every case here is closed on both sides. The returned edge set must equal the
expected set, node for node and type for type; anything absent is a lost fact and
anything extra is a fabricated one. The corpus lives in ``graph_cases.yaml`` and
its ground truth was written from the fixture sources, never from a run.

Five properties are checked across the whole corpus, per §33:

* no unexpected nodes;
* no unexpected edges;
* exact semantic direction;
* no raw storage ids;
* citable evidence only where source text was actually supplied.

No model is consulted and nothing is embedded — the graph is deterministic, so
this suite is too.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.relationships import (
    RelationshipDirection,
    RelationshipsRequest,
    RelationshipsResponse,
)
from repoatlas.services.workspace import Workspace
from tests.evaluation.harness import evaluation_workspace

CASES_PATH = Path(__file__).parent / "graph_cases.yaml"

Triple = tuple[str, str, str]


@dataclasses.dataclass(frozen=True)
class GraphCase:
    """One question with an exactly known answer."""

    id: str
    category: str
    target: str
    direction: str = "both"
    hops: int = 1
    cross_repository: bool = False
    include_containment: bool = False
    max_neighbors: int = 200
    expected_edges: tuple[Triple, ...] | None = None
    expected_paths: tuple[tuple[str, ...], ...] = ()
    forbidden_nodes: tuple[str, ...] = ()
    expected_evidence: tuple[tuple[str, str], ...] = ()
    expected_edge_count_at_most: int | None = None
    expected_edge_count_at_least: int | None = None
    expected_edge_types: tuple[str, ...] = ()
    expected_hops: tuple[int, ...] = ()
    expected_sources: tuple[str, ...] = ()
    uncitable_edge_types: tuple[str, ...] = ()
    citable_edge_types: tuple[str, ...] = ()
    expect_truncated: bool | None = None
    notes: str = ""


def load_graph_cases() -> tuple[GraphCase, ...]:
    raw: dict[str, Any] = yaml.safe_load(CASES_PATH.read_text(encoding="utf-8"))
    cases: list[GraphCase] = []
    for entry in raw["cases"]:
        fields = dict(entry)
        edges = fields.pop("expected_edges", None)
        evidence = fields.pop("expected_evidence", ())
        cases.append(
            GraphCase(
                expected_edges=(
                    tuple((str(a), str(b), str(c)) for a, b, c in edges)
                    if edges is not None
                    else None
                ),
                expected_paths=tuple(
                    tuple(str(node) for node in path) for path in fields.pop("expected_paths", ())
                ),
                forbidden_nodes=tuple(fields.pop("forbidden_nodes", ())),
                expected_evidence=tuple(
                    (str(item["location"]), str(item["text"])) for item in evidence
                ),
                expected_edge_types=tuple(fields.pop("expected_edge_types", ())),
                expected_hops=tuple(fields.pop("expected_hops", ())),
                expected_sources=tuple(fields.pop("expected_sources", ())),
                uncitable_edge_types=tuple(fields.pop("uncitable_edge_types", ())),
                citable_edge_types=tuple(fields.pop("citable_edge_types", ())),
                **fields,
            )
        )
    return tuple(cases)


CASES = load_graph_cases()


@pytest.fixture(scope="module")
def graph_workspace() -> Iterator[Workspace]:
    with evaluation_workspace() as (workspace, _root):
        yield workspace


def _run(workspace: Workspace, case: GraphCase) -> tuple[CapabilityService, RelationshipsResponse]:
    service = CapabilityService(workspace)
    response = service.relationships(
        RelationshipsRequest(
            context=CapabilityContext(scope=CapabilityScope.GLOBAL),
            target=case.target,
            direction=RelationshipDirection(case.direction),
            hops=case.hops,
            cross_repository=case.cross_repository,
            include_containment=case.include_containment,
            max_neighbors=case.max_neighbors,
        )
    )
    return service, response


def _triples(response: RelationshipsResponse) -> set[Triple]:
    return {(e.source.key, str(e.type), e.target.key) for e in response.edges}


def _nodes(response: RelationshipsResponse) -> set[str]:
    return {e.source.key for e in response.edges} | {e.target.key for e in response.edges}


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_the_returned_edge_set_is_exactly_the_expected_one(
    graph_workspace: Workspace, case: GraphCase
) -> None:
    """No unexpected edges, and no missing ones.

    Stated as set equality rather than as containment in either direction,
    because both failures matter and they are different bugs: a missing edge
    loses a fact the repository states, and an extra one asserts a relationship
    that is not in the code.
    """
    if case.expected_edges is None:
        pytest.skip("this case pins shape rather than an exact edge set")
    _service, response = _run(graph_workspace, case)

    returned = _triples(response)
    expected = set(case.expected_edges)
    assert returned - expected == set(), f"{case.id}: fabricated edges"
    assert expected - returned == set(), f"{case.id}: lost edges"


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_no_unexpected_node_appears(graph_workspace: Workspace, case: GraphCase) -> None:
    """Every node is either the target or an endpoint the corpus named."""
    _service, response = _run(graph_workspace, case)

    for node in case.forbidden_nodes:
        assert node not in _nodes(response), f"{case.id}: {node} was reached"

    if case.expected_edges is None:
        return
    allowed = {key for triple in case.expected_edges for key in (triple[0], triple[2])}
    assert _nodes(response) <= allowed, f"{case.id}: unexpected nodes"


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_semantic_direction_survives_the_question(
    graph_workspace: Workspace, case: GraphCase
) -> None:
    """The corpus records stored orientation, so equality is the whole assertion.

    A capability that swapped endpoints to match an incoming question would
    return a well-formed answer with every key resolving, and only a corpus that
    wrote the orientation down by hand can tell.
    """
    _service, response = _run(graph_workspace, case)

    if case.expected_edges is not None:
        for source, edge_type, target in case.expected_edges:
            match = [
                e
                for e in response.edges
                if e.source.key == source and str(e.type) == edge_type and e.target.key == target
            ]
            assert len(match) >= 1, f"{case.id}: {source} -{edge_type}-> {target} missing"

    if case.direction == "incoming":
        assert all(edge.target.key == case.target for edge in response.edges)
    elif case.direction == "outgoing" and case.hops == 1:
        assert all(edge.source.key == case.target for edge in response.edges)


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_only_the_expected_second_hop_chains_are_exposed(
    graph_workspace: Workspace, case: GraphCase
) -> None:
    """A path is the route the walk took, and never a synthesized shortcut."""
    _service, response = _run(graph_workspace, case)

    routes = {tuple(node.key for node in path.nodes) for path in response.paths}
    assert routes == set(case.expected_paths), case.id

    for path in response.paths:
        assert len(path.nodes) == len(path.edges) + 1
        for index, edge in enumerate(path.edges):
            ends = {edge.source.key, edge.target.key}
            assert {path.nodes[index].key, path.nodes[index + 1].key} == ends


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_citable_evidence_exists_only_where_source_text_was_supplied(
    graph_workspace: Workspace, case: GraphCase
) -> None:
    """``citable`` means recorded, not "has coordinates".

    Provenance metadata is available for any parsed edge. An edge that advertised
    those coordinates as citable without a ledger entry behind them would offer a
    reference verification cannot resolve, which is indistinguishable from a
    fabricated one.
    """
    service, response = _run(graph_workspace, case)
    ledger = service.sessions.get(response.investigation_id).ledger
    held = {entry.id for entry in ledger.entries()}

    for edge in response.edges:
        citable = edge.provenance is not None and edge.provenance.citable
        assert citable is bool(edge.evidence_ids), case.id
        for evidence_id in edge.evidence_ids:
            assert evidence_id in held, case.id

    supplied = {(item.location, item.text) for item in response.evidence}
    for location, text in case.expected_evidence:
        assert (location, text) in supplied, f"{case.id}: {location} not supplied verbatim"

    for edge_type in case.uncitable_edge_types:
        matching = [e for e in response.edges if str(e.type) == edge_type]
        assert matching, f"{case.id}: no {edge_type} edge to check"
        for edge in matching:
            assert edge.provenance is not None
            assert edge.provenance.citable is False
            assert edge.evidence_ids == ()

    for edge_type in case.citable_edge_types:
        matching = [e for e in response.edges if str(e.type) == edge_type]
        assert matching, f"{case.id}: no {edge_type} edge to check"
        for edge in matching:
            assert edge.provenance is not None
            assert edge.provenance.citable is True
            assert edge.evidence_ids


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_no_storage_identifier_reaches_the_answer(
    graph_workspace: Workspace, case: GraphCase
) -> None:
    """§29, across the whole corpus. Row ids are renumbered by a reindex."""
    _service, response = _run(graph_workspace, case)
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
    for banned in ("entity_id", "edge_id", "file_id", "chunk_id", "repo_id", "rowid"):
        assert banned not in names, f"{case.id}: {banned}"


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_the_shape_the_case_pins_is_the_shape_returned(
    graph_workspace: Workspace, case: GraphCase
) -> None:
    """The bounded cases, where the exact set is large but its shape is the point."""
    _service, response = _run(graph_workspace, case)

    if case.expected_edge_count_at_most is not None:
        assert len(response.edges) <= case.expected_edge_count_at_most, case.id
    if case.expected_edge_count_at_least is not None:
        assert len(response.edges) >= case.expected_edge_count_at_least, case.id
    if case.expected_edge_types:
        assert {str(e.type) for e in response.edges} == set(case.expected_edge_types), case.id
    if case.expected_hops:
        assert {e.hop for e in response.edges} == set(case.expected_hops), case.id
    if case.expected_sources:
        assert {e.source.key for e in response.edges} == set(case.expected_sources), case.id
    if case.expect_truncated is not None:
        assert response.truncated is case.expect_truncated, case.id
        assert response.has_more is response.truncated


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_every_answer_is_reproducible(graph_workspace: Workspace, case: GraphCase) -> None:
    """Same index, same question, same bytes. Twice."""
    _first_service, first = _run(graph_workspace, case)
    _second_service, second = _run(graph_workspace, case)

    fields = {"investigation_id", "ledger", "freshness"}
    assert first.model_dump_json(exclude=fields) == second.model_dump_json(exclude=fields), case.id


def test_the_corpus_covers_every_required_area() -> None:
    """§33 names the areas; this fails if one is quietly dropped from the corpus."""
    covered = {case.category for case in CASES}
    assert covered == {
        "outgoing",
        "incoming",
        "two-hop",
        "cross-repo",
        "containment",
        "ambiguity",
        "zero-result",
        "provenance",
    }


def test_every_expected_edge_is_a_stored_fact(graph_workspace: Workspace) -> None:
    """The corpus itself is checked against the index, in the safe direction.

    This does not derive the expectations from RepoAtlas — it asserts that
    hand-written ground truth names edges the indexer actually produced, so a
    corpus typo shows up as a corpus failure rather than as a capability one.
    """
    stored = {
        (row["s"], row["t"], row["d"])
        for row in graph_workspace.database.execute(
            "SELECT se.key AS s, e.type AS t, de.key AS d FROM edges e "
            "JOIN entities se ON se.id = e.src_id JOIN entities de ON de.id = e.dst_id"
        ).fetchall()
    }
    for case in CASES:
        for triple in case.expected_edges or ():
            assert triple in stored, f"{case.id}: {triple} is not in the index"


def test_no_case_asks_the_index_to_be_searched(graph_workspace: Workspace) -> None:
    """The whole corpus, with retrieval made fatal."""
    from repoatlas.retrieval.retriever import Retriever

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("a graph case reached retrieval")

    original = (Retriever.retrieve, Retriever.retrieve_detailed)
    Retriever.retrieve = forbidden  # type: ignore[method-assign, assignment]
    Retriever.retrieve_detailed = forbidden  # type: ignore[method-assign, assignment]
    try:
        for case in CASES:
            _run(graph_workspace, case)
    finally:
        Retriever.retrieve, Retriever.retrieve_detailed = original  # type: ignore[method-assign]
