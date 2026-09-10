"""Ledger atomicity, investigations and snapshot coherence for relationships (Slice F).

Two rules meet here, and the second is the one this capability makes newly
interesting.

**Nothing citable leaves without being recorded.** Every asserting span is
prepared, recorded in one atomic ledger batch, and only then bound to the edges
that rest on it. A capacity refusal allocates nothing, strengthens nothing and
returns nothing — because evidence a caller can cite and verification cannot see
is indistinguishable from a fabrication.

**The graph and its provenance are one indexed generation or they are nothing.**
Relationships reads in three passes: the walk finds edges, a second read names
their endpoints, a third reconstructs the asserting lines. Outside one read view,
an external sync landing between any two of them would pair an edge from before it
with a location from after — and the result would revalidate cleanly against its
own hash while describing a state of the repository that never existed. That is
worse than an error, because nothing about it looks wrong.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from repoatlas.capabilities.errors import EntityNotFoundError
from repoatlas.capabilities.ledger import LedgerCapacityError
from repoatlas.capabilities.service import CapabilityService
from repoatlas.capabilities.session import UnknownInvestigationError
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.parsing import EdgeType
from repoatlas.models.relationships import RelationshipsRequest, RelationshipsResponse
from repoatlas.services.workspace import Workspace

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)
CONFIG = "file:python-service:src/config.py"
DEPLOY = "file:scripts:deploy.sh"
SERVICE_HOST = "env:SERVICE_HOST"

#: ``scripts/deploy.sh`` asserts five distinct spans among its edges.
DEPLOY_SPANS = 5


def _service(workspace: Workspace, **kwargs: object) -> CapabilityService:
    return CapabilityService(workspace, **kwargs)  # type: ignore[arg-type]


def _request(target: str, **kwargs: object) -> RelationshipsRequest:
    fields: dict[str, object] = {"context": GLOBAL, "target": target, "max_neighbors": 200}
    fields.update(kwargs)
    return RelationshipsRequest(**fields)  # type: ignore[arg-type]


def _ask(service: CapabilityService, target: str, **kwargs: object) -> RelationshipsResponse:
    return service.relationships(_request(target, **kwargs))


# ---------------------------------------------------------------------------
# Every citation resolves
# ---------------------------------------------------------------------------


def test_every_returned_evidence_id_resolves_in_the_ledger(initialized: Workspace) -> None:
    service = _service(initialized)
    response = _ask(service, SERVICE_HOST, hops=2)
    ledger = service.sessions.get(response.investigation_id).ledger

    assert response.evidence
    assert {item.id for item in response.evidence} == {entry.id for entry in ledger.entries()}


def test_the_ledger_holds_the_reference_the_caller_was_given(initialized: Workspace) -> None:
    service = _service(initialized)
    response = _ask(service, DEPLOY)
    ledger = service.sessions.get(response.investigation_id).ledger

    for item in response.evidence:
        entry = ledger.get(item.id)
        assert entry is not None
        assert entry.ref == item.ref


def test_a_span_supplied_twice_in_one_investigation_keeps_its_id(
    initialized: Workspace,
) -> None:
    """``scripts/deploy.sh:6`` asserts edges reachable from both of its endpoints.

    Two questions, one line, one identity. A second id for the same bytes would
    let one piece of evidence be cited two ways.
    """
    service = _service(initialized)
    first = _ask(service, DEPLOY)
    from_script = next(i for i in first.evidence if i.location == "scripts/deploy.sh:6")

    second = _ask(service, SERVICE_HOST, investigation_id=first.investigation_id)
    from_variable = next(i for i in second.evidence if i.location == "scripts/deploy.sh:6")

    assert from_variable.id == from_script.id
    assert from_variable.ref == from_script.ref


# ---------------------------------------------------------------------------
# CASE 14 / CASE 15 — capacity
# ---------------------------------------------------------------------------


def test_a_capacity_refusal_records_nothing_and_returns_nothing(
    initialized: Workspace,
) -> None:
    """CASE 15. All or nothing, and the refusal precedes any response."""
    service = _service(initialized, ledger_capacity=DEPLOY_SPANS)
    first = _ask(service, DEPLOY)
    ledger = service.sessions.get(first.investigation_id).ledger
    assert len(first.evidence) == DEPLOY_SPANS
    assert ledger.full

    before = {entry.id: entry for entry in ledger.entries()}
    with pytest.raises(LedgerCapacityError) as raised:
        _ask(service, CONFIG, investigation_id=first.investigation_id)

    assert raised.value.required > raised.value.available
    assert {entry.id: entry for entry in ledger.entries()} == before


def test_a_capacity_refusal_strengthens_nothing_either(initialized: Workspace) -> None:
    """The quieter half of the same failure.

    A partial merge shows no new id, so a caller sees only a refusal — while an
    existing entry has silently recorded a route from a call that never completed.
    """
    service = _service(initialized, ledger_capacity=DEPLOY_SPANS)
    first = _ask(service, DEPLOY)
    ledger = service.sessions.get(first.investigation_id).ledger
    origins = {entry.id: entry.origins for entry in ledger.entries()}
    matches = {entry.id: entry.match for entry in ledger.entries()}

    with pytest.raises(LedgerCapacityError):
        _ask(service, CONFIG, investigation_id=first.investigation_id)

    assert {e.id: e.origins for e in ledger.entries()} == origins
    assert {e.id: e.match for e in ledger.entries()} == matches


def test_a_refusal_with_room_left_inserts_none_of_the_batch(initialized: Workspace) -> None:
    """The case a partial insert would survive.

    With the ledger exactly full, "insert what fits" and "insert nothing" are the
    same behaviour and neither is really tested. Here a slot remains and the batch
    needs more than one, so a ledger that took what it could would hand back an id
    for part of an answer the caller never received.
    """
    service = _service(initialized, ledger_capacity=DEPLOY_SPANS + 1)
    first = _ask(service, DEPLOY)
    ledger = service.sessions.get(first.investigation_id).ledger
    assert ledger.available == 1

    with pytest.raises(LedgerCapacityError) as raised:
        _ask(service, CONFIG, investigation_id=first.investigation_id)

    assert raised.value.required > 1
    assert raised.value.available == 1
    assert len(ledger) == DEPLOY_SPANS
    assert ledger.available == 1


def test_re_reading_known_provenance_succeeds_at_full_capacity(
    initialized: Workspace,
) -> None:
    """CASE 14. A full ledger must not stop a caller re-reading what it holds.

    The spans are already recorded, so the call needs no new entry and creates no
    new citable surface.
    """
    service = _service(initialized, ledger_capacity=DEPLOY_SPANS)
    first = _ask(service, DEPLOY)
    assert service.sessions.get(first.investigation_id).ledger.full

    again = _ask(service, DEPLOY, investigation_id=first.investigation_id)
    assert [i.id for i in again.evidence] == [i.id for i in first.evidence]
    assert len(service.sessions.get(first.investigation_id).ledger) == DEPLOY_SPANS


def test_a_call_needing_no_evidence_succeeds_at_full_capacity(
    initialized: Workspace,
) -> None:
    """A topology question costs no ledger slot, so capacity cannot refuse it."""
    service = _service(initialized, ledger_capacity=DEPLOY_SPANS)
    first = _ask(service, DEPLOY)
    assert service.sessions.get(first.investigation_id).ledger.full

    structural = _ask(
        service,
        CONFIG,
        edge_types=(EdgeType.DEFINES,),
        include_containment=True,
        investigation_id=first.investigation_id,
    )
    assert structural.edges
    assert structural.evidence == ()


# ---------------------------------------------------------------------------
# Investigations
# ---------------------------------------------------------------------------


def test_a_call_with_no_id_begins_its_own_investigation(initialized: Workspace) -> None:
    service = _service(initialized)
    first = _ask(service, DEPLOY)
    second = _ask(service, DEPLOY)

    assert first.investigation_id != second.investigation_id
    assert service.sessions.get(first.investigation_id).ledger.entries()
    assert service.sessions.get(second.investigation_id).ledger.entries()


def test_an_unknown_investigation_is_refused_not_invented(initialized: Workspace) -> None:
    """A silently fresh ledger would make every citation to the old one vanish."""
    service = _service(initialized)
    with pytest.raises(UnknownInvestigationError):
        _ask(service, DEPLOY, investigation_id="investigation-1")


def test_a_failed_target_leaves_no_investigation_behind(initialized: Workspace) -> None:
    """A ghost session is an id an agent could be handed and could never use."""
    service = _service(initialized)
    before = len(service.sessions)

    with pytest.raises(EntityNotFoundError):
        _ask(service, "env:NOTHING_LIKE_THIS")

    assert len(service.sessions) == before


def test_an_existing_investigation_survives_a_failed_call(initialized: Workspace) -> None:
    """Cleanup applies to sessions this call invented, never to one it was handed."""
    service = _service(initialized)
    first = _ask(service, DEPLOY)
    ledger = service.sessions.get(first.investigation_id).ledger
    held = {entry.id: entry.ref for entry in ledger.entries()}

    with pytest.raises(EntityNotFoundError):
        _ask(service, "env:NOTHING_LIKE_THIS", investigation_id=first.investigation_id)

    assert {entry.id: entry.ref for entry in ledger.entries()} == held


def test_a_successful_call_with_no_evidence_keeps_its_investigation(
    initialized: Workspace,
) -> None:
    """An empty answer is an answer, and the caller may go on using its id."""
    service = _service(initialized)
    from repoatlas.models.relationships import RelationshipDirection

    response = _ask(service, "module:os", direction=RelationshipDirection.OUTGOING)
    assert response.evidence == ()

    session = service.sessions.get(response.investigation_id)
    assert session.id == response.investigation_id
    assert response.ledger.total == 0
    assert response.ledger.available > 0


def test_the_response_reports_the_ledger_it_wrote_to(initialized: Workspace) -> None:
    service = _service(initialized)
    response = _ask(service, DEPLOY)
    ledger = service.sessions.get(response.investigation_id).ledger

    assert response.ledger.total == len(ledger)
    assert response.ledger.available == ledger.available
    assert response.ledger.full is ledger.full


# ---------------------------------------------------------------------------
# CASE 16 — one snapshot
# ---------------------------------------------------------------------------


def _rewrite_file_rows_externally(database_path: Path, marker: str) -> None:
    """Commit a file-row change from a genuinely separate connection.

    A second connection is the point: a connection always sees its own writes, so
    changing the row through the workspace's own would prove nothing about the
    isolation being tested.
    """
    other = sqlite3.connect(database_path)
    try:
        other.execute("PRAGMA busy_timeout = 5000")
        other.execute(
            "UPDATE files SET content_hash = ?, parser_version = ?",
            (f"{marker}-hash", f"{marker}-version"),
        )
        other.commit()
    finally:
        other.close()


def test_a_sync_landing_between_the_walk_and_the_provenance_read_is_not_seen(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CASE 16. The forbidden pairing, forced as precisely as it can be forced.

    Another process commits a new generation of every file row in the exact window
    between the walk finding the edges and the provenance read reconstructing
    their asserting lines. Without one read view the reference would name the new
    generation while the text came from the old one.
    """
    from repoatlas.storage.sqlite.stores import SqliteKnowledgeStore

    database_path = Path(initialized.database.path)
    real = SqliteKnowledgeStore.keys_for_entities

    def resolve_then_reindex(self: SqliteKnowledgeStore, entity_ids: object) -> object:
        resolved = real(self, entity_ids)  # type: ignore[arg-type]
        _rewrite_file_rows_externally(database_path, "generation-b")
        return resolved

    monkeypatch.setattr(SqliteKnowledgeStore, "keys_for_entities", resolve_then_reindex)
    response = _ask(_service(initialized), DEPLOY)

    assert response.evidence
    for item in response.evidence:
        assert item.ref.file_content_hash != "generation-b-hash", item.location
        assert item.ref.indexed_facts_version != "generation-b-version", item.location


def test_the_generation_that_lands_mid_call_is_seen_by_the_next_one(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The snapshot isolates one call and does not outlive it.

    Otherwise the fix would be a cache: a reader pinned to a generation from
    minutes ago answers coherently, and answers about the past.
    """
    from repoatlas.storage.sqlite.stores import SqliteKnowledgeStore

    database_path = Path(initialized.database.path)
    real = SqliteKnowledgeStore.keys_for_entities
    service = _service(initialized)

    def resolve_then_reindex(self: SqliteKnowledgeStore, entity_ids: object) -> object:
        resolved = real(self, entity_ids)  # type: ignore[arg-type]
        _rewrite_file_rows_externally(database_path, "generation-c")
        monkeypatch.setattr(SqliteKnowledgeStore, "keys_for_entities", real)
        return resolved

    monkeypatch.setattr(SqliteKnowledgeStore, "keys_for_entities", resolve_then_reindex)
    _ask(service, DEPLOY)

    later = _ask(service, DEPLOY)
    assert later.evidence
    assert all(item.ref.file_content_hash == "generation-c-hash" for item in later.evidence)


def test_the_read_view_does_not_survive_a_failure(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transaction left open by an exception would pin every later read.

    And nothing would report it: the process would simply keep answering about a
    generation that is already gone.
    """
    from repoatlas.storage.sqlite.stores import SqliteKnowledgeStore

    def explode(self: SqliteKnowledgeStore, entity_ids: object) -> object:
        raise RuntimeError("provenance read failed")

    service = _service(initialized)
    monkeypatch.setattr(SqliteKnowledgeStore, "keys_for_entities", explode)
    with pytest.raises(RuntimeError):
        _ask(service, DEPLOY)

    monkeypatch.undo()
    assert initialized.database.connection.in_transaction is False
    assert _ask(service, DEPLOY).evidence


def test_a_provenance_failure_allocates_no_evidence_at_all(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recording point is single and comes last, so a failure before it is total."""
    from repoatlas.storage.sqlite.stores import SqliteKnowledgeStore

    service = _service(initialized)
    first = _ask(service, DEPLOY)
    ledger = service.sessions.get(first.investigation_id).ledger
    before = {entry.id: entry.ref for entry in ledger.entries()}

    def explode(self: SqliteKnowledgeStore, file_id: int, line_start: int, line_end: int) -> object:
        raise RuntimeError("chunk read failed")

    monkeypatch.setattr(SqliteKnowledgeStore, "chunks_for_file", explode)
    with pytest.raises(RuntimeError):
        _ask(service, CONFIG, investigation_id=first.investigation_id)

    assert {entry.id: entry.ref for entry in ledger.entries()} == before


def test_the_whole_call_reads_inside_one_transaction(initialized: Workspace) -> None:
    """The walk, the endpoint lookup and the provenance read share one view."""
    source = Path("repoatlas/capabilities/service.py").read_text(encoding="utf-8")
    body = source[source.index("def relationships(") : source.index("# -- internals ---")]
    snapshot = body.index("read_snapshot()")
    for read in ("_resolve_graph_entity", "walk(", "_walked_edges", "_supply_provenance"):
        assert body.index(read) > snapshot, read
    # The ledger write is deliberately outside it: a read view must not be held
    # open across a session mutation that has nothing to do with the index.
    assert body.index("session.ledger.record") > body.index("_supply_provenance")


def test_the_response_describes_this_call_and_not_the_ledgers_history(
    initialized: Workspace,
) -> None:
    """An entry accumulates routes. A response reports the one it took.

    ``scripts/deploy.sh:6`` is inspected first and then reached again as the
    provenance of two graph edges. The ledger entry correctly ends up holding
    both routes and the stronger match class — metadata may only ever strengthen.
    The relationships response must still say how *relationships* reached it: a
    graph answer that reported ``EXACT_CONTENT`` and an inspection's reason would
    be describing a call that is over, in fields a caller reads as current.
    """
    from repoatlas.models.evidence import EvidenceSource, MatchClass
    from repoatlas.models.inspection import InspectRequest, InspectTarget

    service = _service(initialized)
    inspected = service.inspect(
        InspectRequest(
            context=CapabilityContext(scope=CapabilityScope.REPOSITORY, repository="scripts"),
            target=InspectTarget(path="deploy.sh"),
            line_start=6,
            line_end=6,
            context_lines=0,
            allow_worktree=False,
        )
    )
    planted = next(s for s in inspected.segments if s.location == "scripts/deploy.sh:6")

    response = _ask(service, DEPLOY, investigation_id=inspected.investigation_id)
    supplied = next(i for i in response.evidence if i.id == planted.id)

    assert supplied.match is MatchClass.RELATED
    assert supplied.why.startswith("source provenance for")

    entry = service.sessions.get(response.investigation_id).ledger.get(supplied.id)
    assert entry is not None
    assert {EvidenceSource.INSPECT, EvidenceSource.RELATIONSHIP} <= set(entry.origins)
    assert entry.match <= supplied.match
