"""topology/edge: one exact stored relationship, why it is held, and its evidence.

An ``edge_ref`` names one stored fact exactly. Rows that differ only in how the
linker resolved them are different facts with different references; rows that
agree on every field are one fact recorded more than once. The evidence is
recorded in the investigation the caller names, or in a new one it is handed
back, and it is the same evidence ``inspect`` then reads by id.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.capabilities.errors import EdgeNotFoundError, EdgeRefInvalidError
from repoatlas.capabilities.service import CapabilityService
from repoatlas.capabilities.session import UnknownInvestigationError
from repoatlas.capabilities.topology import encode_edge_ref
from repoatlas.models.impact import ImpactRequest
from repoatlas.models.inspection import InspectRequest, InspectTarget
from repoatlas.models.relationships import RelationshipsRequest
from repoatlas.models.topology import (
    LinkKind,
    PeerKind,
    TopologyChildrenRequest,
    TopologyEdgeRequest,
    TopologyEdgeResponse,
    TopologyOverviewRequest,
)
from repoatlas.services.workspace import Workspace
from repoatlas.storage.protocols import StoredEdge
from tests.impact_corpus import HTTP_API, HTTP_WEB
from tests.topology_corpus import GLOBAL, LINKED, open_corpus, repository

COURSE_ROLE = "sym:contracts:src/roles.ts:CourseRole:1"
CAN_EDIT = "sym:app:src/policy.ts:canEdit:2"


@pytest.fixture(scope="module")
def linked(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Workspace]:
    workspace = open_corpus(tmp_path_factory.mktemp("topology-edge"), LINKED)
    yield workspace
    workspace.close()


@pytest.fixture
def http_pair(tmp_path: Path) -> Iterator[Workspace]:
    """A workspace this test may change: the rows of one HTTP crossing."""
    workspace = open_corpus(tmp_path, {"http-api": HTTP_API, "http-web": HTTP_WEB})
    yield workspace
    workspace.close()


def link_examples(workspace: Workspace, kind: LinkKind) -> list[str]:
    response = CapabilityService(workspace).topology_overview(
        TopologyOverviewRequest(context=GLOBAL)
    )
    return [ref for link in response.links if link.kind is kind for ref in link.examples]


def edge(workspace: Workspace, ref: str, **fields: object) -> TopologyEdgeResponse:
    request = TopologyEdgeRequest(context=fields.pop("context", GLOBAL), edge_ref=ref, **fields)  # type: ignore[arg-type]
    return CapabilityService(workspace).topology_edge(request)


def uses_of_course_role(workspace: Workspace) -> str:
    response = CapabilityService(workspace).topology_children(
        TopologyChildrenRequest(
            context=repository("app"), path="src/policy.ts", cross_repository=True
        )
    )
    for link in response.links:
        if link.peer.kind is PeerKind.REPOSITORY and link.child == CAN_EDIT:
            for ref in link.examples:
                if edge(workspace, ref).edge.target.key == COURSE_ROLE:
                    return ref
    raise AssertionError("canEdit's use of CourseRole must be an example of its link")


# ---------------------------------------------------------------------------
# What the edge says
# ---------------------------------------------------------------------------


def test_an_http_edge_names_both_sides_and_its_route_evidence(linked: Workspace) -> None:
    (ref,) = link_examples(linked, LinkKind.HTTP)
    response = edge(linked, ref)
    assert response.edge_ref == ref
    assert (response.edge.source.key, str(response.edge.type), response.edge.target.key) == (
        "sym:http-web:client.py:load_item:4",
        "CALLS_HTTP_HANDLER",
        "sym:http-api:app.py:get_item:9",
    )
    assert response.edge.cross_repository and response.multiplicity == 1
    assert response.http is not None and response.crossing is None
    assert (response.http.method, response.http.consumer_repository) == ("GET", "http-web")
    assert (response.http.called_at, response.http.served_at) == (
        "http-web/client.py:5",
        "http-api/app.py:8",
    )
    # the asserting lines, recorded and citable
    assert response.edge.provenance is not None and response.edge.provenance.citable
    assert response.edge.evidence_ids == tuple(item.id for item in response.evidence)
    assert response.evidence[0].location == "http-web/client.py:5"


def test_a_package_crossing_reads_exactly_as_impact_reads_it(linked: Workspace) -> None:
    ref = uses_of_course_role(linked)
    detail = edge(linked, ref)
    impact = CapabilityService(linked).impact(ImpactRequest(context=GLOBAL, seed=COURSE_ROLE))
    step = next(c for c in impact.candidates if c.entity == CAN_EDIT).step
    assert step.crossing is not None
    assert detail.crossing == step.crossing
    assert detail.resolution is not None and "crossing" in detail.resolution


# ---------------------------------------------------------------------------
# Investigations
# ---------------------------------------------------------------------------


def test_without_an_investigation_one_is_started_and_inspect_reads_its_evidence(
    linked: Workspace,
) -> None:
    """The direct flow: an overview reference, then the edge, then inspect — nothing else."""
    service = CapabilityService(linked)
    (ref,) = link_examples(linked, LinkKind.HTTP)
    before = len(service.sessions)
    response = service.topology_edge(TopologyEdgeRequest(context=GLOBAL, edge_ref=ref))
    assert len(service.sessions) == before + 1
    assert response.ledger.investigation_id == response.investigation_id
    (item,) = response.evidence
    inspected = service.inspect(
        InspectRequest(
            context=GLOBAL,
            target=InspectTarget(evidence_id=item.id),
            investigation_id=response.investigation_id,
            context_lines=0,
        )
    )
    assert inspected.investigation_id == response.investigation_id
    assert inspected.target_evidence_status is not None
    assert item.text in "\n".join(segment.text for segment in inspected.segments)


def test_a_supplied_investigation_is_the_one_used(linked: Workspace) -> None:
    service = CapabilityService(linked)
    started = service.relationships(RelationshipsRequest(context=GLOBAL, target=CAN_EDIT))
    ref = uses_of_course_role(linked)
    response = service.topology_edge(
        TopologyEdgeRequest(context=GLOBAL, edge_ref=ref, investigation_id=started.investigation_id)
    )
    assert response.investigation_id == started.investigation_id
    assert response.ledger.total >= started.ledger.total
    assert set(response.edge.evidence_ids) <= {
        entry.id for entry in service.sessions.get(started.investigation_id).ledger.entries()
    }
    with pytest.raises(UnknownInvestigationError):
        service.topology_edge(
            TopologyEdgeRequest(context=GLOBAL, edge_ref=ref, investigation_id="no-such-id")
        )


@pytest.mark.parametrize(
    "ref",
    [
        "not-a-reference",
        "er1.%%%%",
        "er1." + "W10",  # base64 of "[]"
        "er2.WyJhIl0",
    ],
)
def test_a_malformed_reference_is_refused_and_starts_nothing(linked: Workspace, ref: str) -> None:
    service = CapabilityService(linked)
    before = len(service.sessions)
    with pytest.raises(EdgeRefInvalidError):
        service.topology_edge(TopologyEdgeRequest(context=GLOBAL, edge_ref=ref))
    assert len(service.sessions) == before


# ---------------------------------------------------------------------------
# Exactness and scope
# ---------------------------------------------------------------------------


def _handler_edge(workspace: Workspace) -> StoredEdge:
    row = workspace.database.execute(
        "SELECT id FROM edges WHERE type = 'CALLS_HTTP_HANDLER'"
    ).fetchone()
    (stored,) = workspace.topology().stored_edges([int(row["id"])])
    return stored


def _copy_edge(workspace: Workspace, edge_id: int, resolution: str | None) -> None:
    workspace.database.execute(
        """
        INSERT INTO edges (src_id, dst_id, type, confidence, repo_id, file_id,
                           line_start, line_end, parser, resolution)
        SELECT src_id, dst_id, type, confidence, repo_id, file_id,
               line_start, line_end, parser, ?
        FROM edges WHERE id = ?
        """,
        (resolution, edge_id),
    )
    workspace.database.connection.commit()


def test_two_edges_differing_only_in_resolution_are_two_facts(http_pair: Workspace) -> None:
    original = _handler_edge(http_pair)
    assert original.resolution is not None
    changed = json.dumps({**json.loads(original.resolution), "note": "another reading"})
    _copy_edge(http_pair, original.edge_id, changed)
    other = original.model_copy(update={"resolution": changed})
    first, second = encode_edge_ref(original), encode_edge_ref(other)
    assert first != second
    for ref, resolution in ((first, original.resolution), (second, changed)):
        response = edge(http_pair, ref)
        assert response.multiplicity == 1
        assert response.resolution == json.loads(resolution)


def test_rows_identical_in_every_field_are_one_fact(http_pair: Workspace) -> None:
    original = _handler_edge(http_pair)
    _copy_edge(http_pair, original.edge_id, original.resolution)
    response = edge(http_pair, encode_edge_ref(original))
    assert response.multiplicity == 2


def test_a_reference_stops_resolving_when_its_fact_is_gone(http_pair: Workspace) -> None:
    original = _handler_edge(http_pair)
    ref = encode_edge_ref(original)
    service = CapabilityService(http_pair)
    http_pair.database.execute("DELETE FROM edges WHERE id = ?", (original.edge_id,))
    http_pair.database.connection.commit()
    before = len(service.sessions)
    with pytest.raises(EdgeNotFoundError):
        service.topology_edge(TopologyEdgeRequest(context=GLOBAL, edge_ref=ref))
    # the investigation this call started held nothing, and is not left behind
    assert len(service.sessions) == before


def test_an_edge_resolves_only_in_a_scope_holding_every_repository_it_touches(
    linked: Workspace,
) -> None:
    crossing = uses_of_course_role(linked)
    for slug in ("app", "contracts"):
        with pytest.raises(EdgeNotFoundError):
            edge(linked, crossing, context=repository(slug))
    assert edge(linked, crossing).edge.target.key == COURSE_ROLE

    local = CapabilityService(linked).topology_children(
        TopologyChildrenRequest(context=repository("app"), path="src")
    )
    ref = next(
        link.examples[0]
        for link in local.links
        if link.peer.kind is PeerKind.LOCAL and link.peer.path is not None
    )
    assert edge(linked, ref, context=repository("app")).edge.source.repository_slug == "app"
    with pytest.raises(EdgeNotFoundError):
        edge(linked, ref, context=repository("contracts"))
