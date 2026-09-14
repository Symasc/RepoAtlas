"""Ledger atomicity, investigations and snapshot coherence for dependencies (Slice G).

Two rules meet here.

**Nothing citable leaves without being recorded.** Every manifest line this page
quotes is prepared, recorded in one atomic ledger batch, and only then bound to
the observations resting on it. A capacity refusal allocates nothing, strengthens
nothing and returns nothing — evidence a caller can cite and verification cannot
see is indistinguishable from a fabrication.

**The inventory and its provenance are one indexed generation or they are
nothing.** Dependencies reads in several passes: the rows, the file states their
provenance names, the chunks that quote them. Outside one read view an external
sync landing between any two would pair a constraint from before it with a
location or a hash from after — evidence that revalidates cleanly against its own
hash while describing a state of the repository that never existed.

Structured facts cost nothing. A dependency row with no quotable line allocates no
id, so an inventory question that quotes nothing spends no ledger capacity at all.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from repoatlas.capabilities.errors import InvalidDependencyRequestError
from repoatlas.capabilities.ledger import LedgerCapacityError
from repoatlas.capabilities.service import CapabilityService
from repoatlas.capabilities.session import UnknownInvestigationError
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.dependencies import DependencyRole
from repoatlas.models.dependency_inventory import (
    MAX_PAGE_SIZE,
    DependenciesRequest,
    DependenciesResponse,
    Ecosystem,
)
from repoatlas.services.workspace import Workspace

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)
SERVICE = "python-service"
COMMON = "common-lib"

#: ``common-lib``'s PyPI package quotes three distinct spans across two repositories.
COMMON_LIB_SPANS = 3


def _service(workspace: Workspace, **kwargs: object) -> CapabilityService:
    return CapabilityService(workspace, **kwargs)  # type: ignore[arg-type]


def _request(**kwargs: object) -> DependenciesRequest:
    fields: dict[str, object] = {"context": GLOBAL, "limit": MAX_PAGE_SIZE}
    fields.update(kwargs)
    return DependenciesRequest(**fields)  # type: ignore[arg-type]


def _ask(service: CapabilityService, **kwargs: object) -> DependenciesResponse:
    return service.dependencies(_request(**kwargs))


def _common_lib(**kwargs: object) -> DependenciesRequest:
    return _request(name="common-lib", ecosystem=Ecosystem.PYPI, **kwargs)


# ---------------------------------------------------------------------------
# Every citation resolves
# ---------------------------------------------------------------------------


def test_every_returned_evidence_id_resolves_in_the_ledger(initialized: Workspace) -> None:
    service = _service(initialized)
    response = _ask(service)
    ledger = service.sessions.get(response.investigation_id).ledger

    assert response.evidence
    assert {item.id for item in response.evidence} == {entry.id for entry in ledger.entries()}


def test_the_ledger_holds_the_reference_the_caller_was_given(
    initialized: Workspace,
) -> None:
    service = _service(initialized)
    response = _ask(service, name="common-lib", ecosystem=Ecosystem.PYPI)
    ledger = service.sessions.get(response.investigation_id).ledger

    for item in response.evidence:
        entry = ledger.get(item.id)
        assert entry is not None
        assert entry.ref == item.ref


def test_a_span_supplied_twice_in_one_investigation_keeps_its_id(
    initialized: Workspace,
) -> None:
    """``common-lib/pyproject.toml:2`` backs a provision and a provider candidate.

    Two questions reaching the same line must reach the same identity, or one
    piece of evidence would be citable two ways.
    """
    service = _service(initialized)
    first = _ask(service, **{"name": "common-lib", "ecosystem": Ecosystem.PYPI})
    planted = next(i for i in first.evidence if i.location == "common-lib/pyproject.toml:2")

    second = _ask(service, investigation_id=first.investigation_id)
    again = next(i for i in second.evidence if i.location == "common-lib/pyproject.toml:2")

    assert again.id == planted.id
    assert again.ref == planted.ref


# ---------------------------------------------------------------------------
# CASE 17 / CASE 18 — capacity
# ---------------------------------------------------------------------------


def test_a_capacity_refusal_records_nothing_and_returns_nothing(
    initialized: Workspace,
) -> None:
    """CASE 18. All or nothing, and the refusal precedes any response."""
    service = _service(initialized, ledger_capacity=COMMON_LIB_SPANS)
    first = service.dependencies(_common_lib())
    ledger = service.sessions.get(first.investigation_id).ledger
    assert len(first.evidence) == COMMON_LIB_SPANS
    assert ledger.full

    before = {entry.id: entry for entry in ledger.entries()}
    with pytest.raises(LedgerCapacityError) as raised:
        _ask(service, investigation_id=first.investigation_id)

    assert raised.value.required > raised.value.available
    assert {entry.id: entry for entry in ledger.entries()} == before


def test_a_capacity_refusal_strengthens_nothing_either(initialized: Workspace) -> None:
    """A partial merge shows no new id, so a caller sees only a refusal — while an
    existing entry has silently recorded a route from a call that never finished."""
    service = _service(initialized, ledger_capacity=COMMON_LIB_SPANS)
    first = service.dependencies(_common_lib())
    ledger = service.sessions.get(first.investigation_id).ledger
    origins = {entry.id: entry.origins for entry in ledger.entries()}
    matches = {entry.id: entry.match for entry in ledger.entries()}

    with pytest.raises(LedgerCapacityError):
        _ask(service, investigation_id=first.investigation_id)

    assert {e.id: e.origins for e in ledger.entries()} == origins
    assert {e.id: e.match for e in ledger.entries()} == matches


def test_a_refusal_with_room_left_inserts_none_of_the_batch(
    initialized: Workspace,
) -> None:
    """The case a partial insert would survive.

    With the ledger exactly full, "insert what fits" and "insert nothing" are the
    same behaviour and neither is really tested. Here a slot remains and the batch
    needs more, so a ledger that took what it could would hand back an id for part
    of an answer the caller never received.
    """
    service = _service(initialized, ledger_capacity=COMMON_LIB_SPANS + 1)
    first = service.dependencies(_common_lib())
    ledger = service.sessions.get(first.investigation_id).ledger
    assert ledger.available == 1

    with pytest.raises(LedgerCapacityError) as raised:
        _ask(service, investigation_id=first.investigation_id)

    assert raised.value.required > 1
    assert raised.value.available == 1
    assert len(ledger) == COMMON_LIB_SPANS


def test_re_reading_known_provenance_succeeds_at_full_capacity(
    initialized: Workspace,
) -> None:
    """CASE 17. A full ledger must not stop a caller re-reading what it holds."""
    service = _service(initialized, ledger_capacity=COMMON_LIB_SPANS)
    first = service.dependencies(_common_lib())
    assert service.sessions.get(first.investigation_id).ledger.full

    again = service.dependencies(_common_lib(investigation_id=first.investigation_id))
    assert [i.id for i in again.evidence] == [i.id for i in first.evidence]
    assert len(service.sessions.get(first.investigation_id).ledger) == COMMON_LIB_SPANS


def test_a_structured_only_call_costs_no_capacity(initialized: Workspace) -> None:
    """An inventory question that quotes nothing spends nothing."""
    service = _service(initialized, ledger_capacity=COMMON_LIB_SPANS)
    first = service.dependencies(_common_lib())
    assert service.sessions.get(first.investigation_id).ledger.full

    structured = _ask(service, include_provenance=False, investigation_id=first.investigation_id)
    assert structured.packages
    assert structured.evidence == ()
    assert len(service.sessions.get(first.investigation_id).ledger) == COMMON_LIB_SPANS


# ---------------------------------------------------------------------------
# Investigations
# ---------------------------------------------------------------------------


def test_a_call_with_no_id_begins_its_own_investigation(initialized: Workspace) -> None:
    service = _service(initialized)
    first = _ask(service)
    second = _ask(service)

    assert first.investigation_id != second.investigation_id
    assert service.sessions.get(first.investigation_id).ledger.entries()


def test_an_unknown_investigation_is_refused_not_invented(initialized: Workspace) -> None:
    service = _service(initialized)
    with pytest.raises(UnknownInvestigationError):
        _ask(service, investigation_id="investigation-1")


def test_a_malformed_request_leaves_no_investigation_behind(
    initialized: Workspace,
) -> None:
    """A ghost session is an id an agent could be handed and could never use."""
    service = _service(initialized)
    before = len(service.sessions)

    with pytest.raises(InvalidDependencyRequestError):
        _ask(service, roles=(DependencyRole.DECLARED, DependencyRole.DECLARED))

    assert len(service.sessions) == before


def test_an_existing_investigation_survives_a_failed_call(initialized: Workspace) -> None:
    """Cleanup applies to sessions this call invented, never to one it was handed."""
    service = _service(initialized)
    first = _ask(service)
    ledger = service.sessions.get(first.investigation_id).ledger
    held = {entry.id: entry.ref for entry in ledger.entries()}

    with pytest.raises(InvalidDependencyRequestError):
        _ask(
            service,
            roles=(DependencyRole.DECLARED, DependencyRole.DECLARED),
            investigation_id=first.investigation_id,
        )

    assert {entry.id: entry.ref for entry in ledger.entries()} == held


def test_a_successful_call_with_no_evidence_keeps_its_investigation(
    initialized: Workspace,
) -> None:
    """An empty inventory answer is an answer, and its id stays usable."""
    service = _service(initialized)
    response = _ask(service, name="no-such-package-anywhere")
    assert response.evidence == ()

    session = service.sessions.get(response.investigation_id)
    assert session.id == response.investigation_id
    assert response.ledger.total == 0
    assert response.ledger.available > 0


def test_the_response_reports_the_ledger_it_wrote_to(initialized: Workspace) -> None:
    service = _service(initialized)
    response = _ask(service)
    ledger = service.sessions.get(response.investigation_id).ledger

    assert response.ledger.total == len(ledger)
    assert response.ledger.available == ledger.available
    assert response.ledger.full is ledger.full


# ---------------------------------------------------------------------------
# CASE 21 — one snapshot
# ---------------------------------------------------------------------------


def _rewrite_file_rows_externally(database_path: Path, marker: str) -> None:
    """Commit a file-row change from a genuinely separate connection.

    A connection always sees its own writes, so changing the row through the
    workspace's own would prove nothing about the isolation being tested.
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


def test_a_sync_landing_between_the_rows_and_their_provenance_is_not_seen(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CASE 21. The forbidden pairing, forced as precisely as it can be forced.

    Another process commits a new generation of every file row in the exact window
    between the dependency rows being read and their stating lines being
    reconstructed. Without one read view the reference would name the new
    generation while the text came from the old one.
    """
    from repoatlas.storage.sqlite.stores import SqliteDependencyStore

    database_path = Path(initialized.database.path)
    real = SqliteDependencyStore.list_packages

    def read_then_reindex(self: SqliteDependencyStore, repo_ids: object = None) -> object:
        packages = real(self, repo_ids)  # type: ignore[arg-type]
        _rewrite_file_rows_externally(database_path, "generation-b")
        return packages

    monkeypatch.setattr(SqliteDependencyStore, "list_packages", read_then_reindex)
    response = _ask(_service(initialized))

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
    from repoatlas.storage.sqlite.stores import SqliteDependencyStore

    database_path = Path(initialized.database.path)
    real = SqliteDependencyStore.list_packages
    service = _service(initialized)

    def read_then_reindex(self: SqliteDependencyStore, repo_ids: object = None) -> object:
        packages = real(self, repo_ids)  # type: ignore[arg-type]
        _rewrite_file_rows_externally(database_path, "generation-c")
        monkeypatch.setattr(SqliteDependencyStore, "list_packages", real)
        return packages

    monkeypatch.setattr(SqliteDependencyStore, "list_packages", read_then_reindex)
    _ask(service)

    later = _ask(service)
    assert later.evidence
    assert all(item.ref.file_content_hash == "generation-c-hash" for item in later.evidence)


def test_the_read_view_does_not_survive_a_failure(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transaction left open by an exception would pin every later read."""
    from repoatlas.storage.sqlite.stores import SqliteDependencyStore

    def explode(self: SqliteDependencyStore, repo_ids: object = None) -> object:
        raise RuntimeError("dependency read failed")

    service = _service(initialized)
    monkeypatch.setattr(SqliteDependencyStore, "list_packages", explode)
    with pytest.raises(RuntimeError):
        _ask(service)

    monkeypatch.undo()
    assert initialized.database.connection.in_transaction is False
    assert _ask(service).evidence


def test_a_provenance_failure_allocates_no_evidence_at_all(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recording point is single and comes last, so a failure before it is total."""
    from repoatlas.storage.sqlite.stores import SqliteKnowledgeStore

    service = _service(initialized)
    first = _ask(service)
    ledger = service.sessions.get(first.investigation_id).ledger
    before = {entry.id: entry.ref for entry in ledger.entries()}

    def explode(self: SqliteKnowledgeStore, file_id: int, line_start: int, line_end: int) -> object:
        raise RuntimeError("chunk read failed")

    monkeypatch.setattr(SqliteKnowledgeStore, "chunks_for_file", explode)
    with pytest.raises(RuntimeError):
        _ask(service, investigation_id=first.investigation_id)

    assert {entry.id: entry.ref for entry in ledger.entries()} == before


def test_the_whole_call_reads_inside_one_transaction(initialized: Workspace) -> None:
    """The rows, the provider index, the file states and the chunks share one view."""
    source = Path("repoatlas/capabilities/service.py").read_text(encoding="utf-8")
    body = source[source.index("def dependencies(") : source.index("# -- dependencies internals")]
    snapshot = body.index("read_snapshot()")
    assert body.index("_dependency_page") > snapshot
    # The ledger write is deliberately outside it: a read view must not be held
    # open across a session mutation that has nothing to do with the index.
    assert body.index("session.ledger.record") > body.index("_dependency_page")


# ---------------------------------------------------------------------------
# §22 — this call, not the ledger's history
# ---------------------------------------------------------------------------


def test_the_response_describes_this_call_and_not_the_ledgers_history(
    initialized: Workspace,
) -> None:
    """An entry accumulates routes. A response reports the one it took.

    ``common-lib/pyproject.toml:2`` is inspected first and then reached again as a
    dependency provision. The entry correctly ends up holding both routes; the
    dependency response must still say how *dependencies* reached it.
    """
    from repoatlas.models.evidence import EvidenceSource, MatchClass
    from repoatlas.models.inspection import InspectRequest, InspectTarget

    service = _service(initialized)
    inspected = service.inspect(
        InspectRequest(
            context=CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=COMMON),
            target=InspectTarget(path="pyproject.toml"),
            line_start=2,
            line_end=2,
            context_lines=0,
            allow_worktree=False,
        )
    )
    planted = next(s for s in inspected.segments if s.location == "common-lib/pyproject.toml:2")

    response = service.dependencies(_common_lib(investigation_id=inspected.investigation_id))
    supplied = next(i for i in response.evidence if i.id == planted.id)

    assert supplied.match is MatchClass.EXACT_NAME
    assert supplied.why.startswith("dependency source for")

    entry = service.sessions.get(response.investigation_id).ledger.get(supplied.id)
    assert entry is not None
    assert {EvidenceSource.INSPECT, EvidenceSource.DEPENDENCY} <= set(entry.origins)
    assert entry.match <= supplied.match
