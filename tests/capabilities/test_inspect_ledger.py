"""Evidence identity, atomicity and snapshot coherence for inspect (Slice E).

Two rules meet in this file.

**Nothing citable leaves without being recorded.** Every segment is prepared,
recorded in one atomic ledger batch, and only then turned into a response. A
capacity refusal allocates nothing, strengthens nothing and returns nothing —
because evidence a caller can cite and verification cannot see is
indistinguishable from a fabrication.

**An evidence id is never rebound.** ``E7`` is bound to a reference and a digest,
not to bytes the ledger kept, so inspecting it means inspecting what the index
holds at those coordinates *now*. When the file has moved on, the current text is
supplied under new ids and ``E7`` keeps its identity, its digest and its status.
Reporting one status for both would either claim the old id is current or claim
the new text is stale.

Underneath both sits snapshot coherence. The text returned and the hash and parser
version recorded beside it must describe **one** indexed generation. Pairing text
from one with metadata from another would produce evidence that revalidates
cleanly — its own hash would match — while describing a file state that never
existed.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from repoatlas.capabilities.errors import IndexChangedError
from repoatlas.capabilities.ledger import LedgerCapacityError
from repoatlas.capabilities.service import CapabilityService
from repoatlas.indexing.chunking import chunk_hash
from repoatlas.models.capability import CapabilityContext, CapabilityScope, EvidenceBudget
from repoatlas.models.evidence import EvidenceSource, MatchClass
from repoatlas.models.inspection import InspectRequest, InspectTarget
from repoatlas.models.investigation import EvidenceStatus
from repoatlas.models.repository import FileState
from repoatlas.models.search import SearchRequest
from repoatlas.services.workspace import Workspace

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)
SERVICE = "python-service"
CONFIG = "src/config.py"


def _repo(slug: str = SERVICE) -> CapabilityContext:
    return CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=slug)


def _config_request(**kwargs: object) -> InspectRequest:
    fields: dict[str, object] = {
        "context": _repo(),
        "target": InspectTarget(path=CONFIG),
        "allow_worktree": False,
    }
    fields.update(kwargs)
    return InspectRequest(**fields)  # type: ignore[arg-type]


def _resync(workspace: Workspace, root: Path) -> None:
    from repoatlas.services.sync_service import sync

    sync(workspace, workspace.resolver().resolve(root))


# ---------------------------------------------------------------------------
# Every returned segment is recorded
# ---------------------------------------------------------------------------


def test_every_returned_segment_resolves_in_the_ledger(initialized: Workspace) -> None:
    service = CapabilityService(initialized)
    response = service.inspect(_config_request())

    ledger = service.sessions.get(response.investigation_id).ledger
    assert response.segments
    assert {segment.id for segment in response.segments} == {e.id for e in ledger.entries()}
    for segment in response.segments:
        entry = ledger.get(segment.id)
        assert entry is not None
        assert entry.ref == segment.ref


def test_the_digest_covers_the_text_the_caller_received(initialized: Workspace) -> None:
    """Not the whole run, not the whole file, not the requested range."""
    service = CapabilityService(initialized)
    for ceiling in (60, 90, 100_000):
        response = service.inspect(_config_request(budget=EvidenceBudget(max_total_chars=ceiling)))
        for segment in response.segments:
            assert segment.ref.span_digest == chunk_hash(segment.text)


def test_a_truncated_supply_is_its_own_evidence_with_its_own_narrower_span(
    initialized: Workspace,
) -> None:
    """A cut segment is not a cheaper version of the whole one.

    Its span shrinks with its text and it earns its own id, so a citation made
    against the short prefix can never resolve to the wider span the caller was
    never shown.
    """
    service = CapabilityService(initialized)
    narrow = service.inspect(_config_request(budget=EvidenceBudget(max_total_chars=60)))
    wide = service.inspect(_config_request(investigation_id=narrow.investigation_id))

    assert narrow.segments and wide.segments
    cut, whole = narrow.segments[0], wide.segments[0]
    assert cut.truncated and not whole.truncated
    assert cut.ref.line_start == whole.ref.line_start
    assert cut.ref.line_end is not None and whole.ref.line_end is not None
    assert cut.ref.line_end < whole.ref.line_end
    assert cut.ref.span_digest != whole.ref.span_digest
    assert cut.id != whole.id


def test_inspected_evidence_records_the_route_it_arrived_by(initialized: Workspace) -> None:
    """``INSPECT`` is not a retrieval stage, and the ledger says which it was."""
    service = CapabilityService(initialized)
    response = service.inspect(_config_request())
    ledger = service.sessions.get(response.investigation_id).ledger

    for segment in response.segments:
        entry = ledger.get(segment.id)
        assert entry is not None
        assert entry.origins == (EvidenceSource.INSPECT,)
        assert entry.reasons and all("inspect" in reason for reason in entry.reasons)


def test_the_reason_describes_this_inspection_and_not_an_older_route(
    initialized: Workspace,
) -> None:
    """A response says what *this* call did; the ledger accumulates the rest."""
    service = CapabilityService(initialized)
    found = service.search(SearchRequest(question="SERVICE_HOST", context=GLOBAL))
    cited = next(item for item in found.evidence if item.ref.line_start is not None)

    response = service.inspect(
        InspectRequest(
            context=GLOBAL,
            target=InspectTarget(evidence_id=cited.id),
            investigation_id=found.investigation_id,
            context_lines=0,
        )
    )
    assert response.segments
    for segment in response.segments:
        assert cited.id in segment.why
        assert segment.match is MatchClass.EXACT_PATH


def test_a_segment_whose_span_was_already_supplied_keeps_its_id(
    initialized: Workspace,
) -> None:
    """Re-reading evidence allocates nothing. The same bytes are the same evidence."""
    service = CapabilityService(initialized)
    first = service.inspect(_config_request())
    again = service.inspect(_config_request(investigation_id=first.investigation_id))

    assert [s.id for s in again.segments] == [s.id for s in first.segments]
    ledger = service.sessions.get(first.investigation_id).ledger
    assert len(ledger) == len(first.segments)


# ---------------------------------------------------------------------------
# Capacity
# ---------------------------------------------------------------------------


def test_a_capacity_refusal_inserts_nothing_and_returns_nothing(
    initialized: Workspace,
) -> None:
    """CASE 9. All or nothing, and the refusal comes before any response exists."""
    service = CapabilityService(initialized, ledger_capacity=3)
    first = service.inspect(_config_request())
    assert len(first.segments) == 3
    ledger = service.sessions.get(first.investigation_id).ledger
    before = {entry.id: entry for entry in ledger.entries()}
    assert len(before) == 3
    assert ledger.full

    with pytest.raises(LedgerCapacityError) as raised:
        service.inspect(
            InspectRequest(
                context=_repo(),
                target=InspectTarget(path="src/app.py"),
                allow_worktree=False,
                investigation_id=first.investigation_id,
            )
        )

    assert raised.value.required > raised.value.available
    after = {entry.id: entry for entry in ledger.entries()}
    assert after == before, "a refused batch changed the ledger"


def test_a_capacity_refusal_strengthens_nothing_either(initialized: Workspace) -> None:
    """Not a partial insert, and not a partial merge.

    A merge is the quieter failure of the two: no id appears, so a caller sees
    only a refusal, while an entry has silently claimed a route this call was
    never allowed to complete.
    """
    service = CapabilityService(initialized, ledger_capacity=3)
    first = service.inspect(_config_request())
    ledger = service.sessions.get(first.investigation_id).ledger
    origins_before = {entry.id: entry.origins for entry in ledger.entries()}
    matches_before = {entry.id: entry.match for entry in ledger.entries()}

    with pytest.raises(LedgerCapacityError):
        service.inspect(
            InspectRequest(
                context=_repo(),
                target=InspectTarget(path="src/app.py"),
                allow_worktree=False,
                investigation_id=first.investigation_id,
            )
        )

    assert {e.id: e.origins for e in ledger.entries()} == origins_before
    assert {e.id: e.match for e in ledger.entries()} == matches_before


def test_a_refusal_with_room_left_inserts_none_of_the_batch(initialized: Workspace) -> None:
    """The case a partial insert would survive.

    With the ledger exactly full, "insert as many as fit" and "insert none" are the
    same behaviour, so neither is really tested. Here one slot remains and the
    batch needs two: a ledger that took the one it could would look reasonable and
    would hand back an id for half of an inspection the caller never received.
    """
    service = CapabilityService(initialized, ledger_capacity=4)
    first = service.inspect(_config_request())
    ledger = service.sessions.get(first.investigation_id).ledger
    assert len(ledger) == 3
    assert ledger.available == 1

    with pytest.raises(LedgerCapacityError) as raised:
        service.inspect(
            InspectRequest(
                context=_repo(),
                target=InspectTarget(path="src/app.py"),
                allow_worktree=False,
                investigation_id=first.investigation_id,
            )
        )

    assert raised.value.required == 2
    assert raised.value.available == 1
    assert len(ledger) == 3, "part of a refused batch was inserted"
    assert ledger.available == 1


def test_re_inspecting_held_evidence_succeeds_at_full_capacity(initialized: Workspace) -> None:
    """A full ledger must not stop an agent re-reading what it already holds."""
    service = CapabilityService(initialized, ledger_capacity=3)
    first = service.inspect(_config_request())
    assert service.sessions.get(first.investigation_id).ledger.full

    again = service.inspect(_config_request(investigation_id=first.investigation_id))
    assert [s.id for s in again.segments] == [s.id for s in first.segments]


def test_a_capacity_failure_on_a_new_investigation_leaves_no_session(
    initialized: Workspace,
) -> None:
    service = CapabilityService(initialized, ledger_capacity=2)
    before = len(service.sessions)

    with pytest.raises(LedgerCapacityError):
        service.inspect(_config_request())

    assert len(service.sessions) == before


# ---------------------------------------------------------------------------
# Investigation lifecycle
# ---------------------------------------------------------------------------


def test_an_inspection_with_no_id_begins_its_own_investigation(
    initialized: Workspace,
) -> None:
    service = CapabilityService(initialized)
    first = service.inspect(_config_request())
    second = service.inspect(_config_request())
    assert first.investigation_id != second.investigation_id


def test_a_failed_target_leaves_no_investigation_behind(initialized: Workspace) -> None:
    """A ghost id an agent could be handed and then never resolve anything in."""
    from repoatlas.capabilities.errors import EntityNotFoundError, FileNotIndexedError

    service = CapabilityService(initialized)
    before = len(service.sessions)

    with pytest.raises(FileNotIndexedError):
        service.inspect(_config_request(target=InspectTarget(path="nope/missing.py")))
    with pytest.raises(EntityNotFoundError):
        service.inspect(
            InspectRequest(context=GLOBAL, target=InspectTarget(entity_key="sym:no:such:key:1"))
        )

    assert len(service.sessions) == before


def test_a_successful_inspection_with_no_text_keeps_its_investigation(
    initialized: Workspace, workspace_root: Path
) -> None:
    """An investigation that began is an investigation, even with an empty ledger.

    Dropping it would hand back an id that stops resolving on the next call, which
    reads to a caller exactly like evidence having been forgotten.
    """
    blob = workspace_root / SERVICE / "src" / "empty.bin"
    blob.write_bytes(b"\x00" * 64)
    _resync(initialized, workspace_root)

    service = CapabilityService(initialized)
    response = service.inspect(_config_request(target=InspectTarget(path="src/empty.bin")))
    assert response.segments == ()
    assert service.sessions.get(response.investigation_id) is not None


def test_an_existing_investigation_survives_a_failed_inspection(
    initialized: Workspace,
) -> None:
    from repoatlas.capabilities.errors import FileNotIndexedError

    service = CapabilityService(initialized)
    first = service.inspect(_config_request())
    held = {entry.id for entry in service.sessions.get(first.investigation_id).ledger.entries()}

    with pytest.raises(FileNotIndexedError):
        service.inspect(
            _config_request(
                target=InspectTarget(path="nope/missing.py"),
                investigation_id=first.investigation_id,
            )
        )

    ledger = service.sessions.get(first.investigation_id).ledger
    assert {entry.id for entry in ledger.entries()} == held


# ---------------------------------------------------------------------------
# Stale and superseded targets
# ---------------------------------------------------------------------------


def test_inspecting_stale_evidence_reports_its_status_and_supplies_new_ids(
    initialized: Workspace, workspace_root: Path
) -> None:
    """CASE 2. ``E1`` is not rebound; the current text arrives under its own id."""
    service = CapabilityService(initialized)
    found = service.search(SearchRequest(question="SERVICE_HOST", context=GLOBAL))
    cited = next(item for item in found.evidence if item.ref.line_start is not None)
    original_ref = cited.ref

    edited = workspace_root / cited.ref.repository_slug / cited.ref.path
    edited.write_text(
        "# a line inserted at the very top\n" + edited.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    _resync(initialized, workspace_root)

    response = service.inspect(
        InspectRequest(
            context=GLOBAL,
            target=InspectTarget(evidence_id=cited.id),
            investigation_id=found.investigation_id,
            context_lines=2,
        )
    )

    assert response.target_evidence_status is EvidenceStatus.STALE
    ledger = service.sessions.get(found.investigation_id).ledger
    unchanged = ledger.get(cited.id)
    assert unchanged is not None
    assert unchanged.ref == original_ref, "E1 was rebound to different bytes"
    assert unchanged.status is EvidenceStatus.STALE

    assert response.segments
    assert cited.id not in {segment.id for segment in response.segments}
    for segment in response.segments:
        assert segment.ref.file_content_hash != original_ref.file_content_hash


def test_the_target_status_is_not_the_status_of_what_came_back(
    initialized: Workspace, workspace_root: Path
) -> None:
    """Two different claims about two different things."""
    service = CapabilityService(initialized)
    found = service.search(SearchRequest(question="SERVICE_HOST", context=GLOBAL))
    cited = next(item for item in found.evidence if item.ref.line_start is not None)

    edited = workspace_root / cited.ref.repository_slug / cited.ref.path
    edited.write_text(edited.read_text(encoding="utf-8") + "\n# tail\n", encoding="utf-8")
    _resync(initialized, workspace_root)

    response = service.inspect(
        InspectRequest(
            context=GLOBAL,
            target=InspectTarget(evidence_id=cited.id),
            investigation_id=found.investigation_id,
        )
    )
    assert response.target_evidence_status is EvidenceStatus.STALE
    assert all(segment.status is EvidenceStatus.VALID for segment in response.segments)


def test_a_path_target_reports_no_target_status(initialized: Workspace) -> None:
    """There is no target evidence, so there is nothing to say about it."""
    response = CapabilityService(initialized).inspect(_config_request())
    assert response.target_evidence_status is None


def test_evidence_whose_file_is_gone_cannot_be_inspected(
    initialized: Workspace, workspace_root: Path
) -> None:
    """And no search is made for a moved or similarly named replacement."""
    from repoatlas.capabilities.errors import FileNotIndexedError

    service = CapabilityService(initialized)
    found = service.search(SearchRequest(question="load_config", context=GLOBAL))
    cited = next(item for item in found.evidence if item.ref.path.endswith(".py"))

    (workspace_root / cited.ref.repository_slug / cited.ref.path).unlink()
    _resync(initialized, workspace_root)

    with pytest.raises(FileNotIndexedError):
        service.inspect(
            InspectRequest(
                context=GLOBAL,
                target=InspectTarget(evidence_id=cited.id),
                investigation_id=found.investigation_id,
            )
        )
    entry = service.sessions.get(found.investigation_id).ledger.get(cited.id)
    assert entry is not None
    assert entry.status is EvidenceStatus.INVALID


def test_revalidation_survives_a_later_failure_in_the_same_call(
    initialized: Workspace, workspace_root: Path
) -> None:
    """Evidence mutation is atomic; observing validity is not a mutation.

    The inspection revalidates the ledger, learns that the target has gone stale,
    and then fails on capacity before supplying anything. Rolling that observation
    back to make the failure "atomic" would restore a status RepoAtlas has just
    proved wrong — an untruth introduced for tidiness. Nothing was allocated, and
    what was learned is kept.
    """
    service = CapabilityService(initialized, ledger_capacity=2)
    found = service.search(
        SearchRequest(question="SERVICE_HOST", context=GLOBAL, budget=EvidenceBudget(max_items=2))
    )
    cited = next(item for item in found.evidence if item.ref.line_start is not None)
    assert cited.status is EvidenceStatus.VALID

    ledger = service.sessions.get(found.investigation_id).ledger
    assert ledger.full, "the ledger must be full for this call to fail after revalidating"
    held = {entry.id: entry.ref for entry in ledger.entries()}

    edited = workspace_root / cited.ref.repository_slug / cited.ref.path
    edited.write_text(
        "# a line inserted at the very top\n" + edited.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    _resync(initialized, workspace_root)

    with pytest.raises(LedgerCapacityError):
        service.inspect(
            InspectRequest(
                context=GLOBAL,
                target=InspectTarget(evidence_id=cited.id),
                investigation_id=found.investigation_id,
                context_lines=0,
            )
        )

    entry = ledger.get(cited.id)
    assert entry is not None
    assert entry.status is EvidenceStatus.STALE, "a truthful observation was rolled back"
    # And nothing was allocated or rebound while learning it.
    assert {e.id: e.ref for e in ledger.entries()} == held


# ---------------------------------------------------------------------------
# Snapshot coherence
# ---------------------------------------------------------------------------


def test_text_and_snapshot_metadata_describe_one_generation(initialized: Workspace) -> None:
    service = CapabilityService(initialized)
    for allow in (True, False):
        response = service.inspect(_config_request(allow_worktree=allow))
        hashes = {segment.ref.file_content_hash for segment in response.segments}
        versions = {segment.ref.indexed_facts_version for segment in response.segments}
        assert hashes == {response.file.indexed_content_hash}
        assert versions == {response.file.indexed_facts_version}


def test_a_file_reindexed_between_reads_never_produces_mixed_evidence(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CASE 10. Old text with a new hash, or new text with an old hash. Neither.

    The state is read, the chunks are read, and the state is read again; the two
    readings must agree. Here they never do, so the inspection is refused rather
    than answered — and a refusal is safe in a way a plausible answer is not,
    because mixed evidence would revalidate cleanly against its own hash.
    """
    from repoatlas.storage.sqlite.stores import SqliteFileStore

    service = CapabilityService(initialized)
    real = SqliteFileStore.states_for
    calls = {"n": 0}

    def drifting(
        self: SqliteFileStore, repo_id: int, rel_paths: Sequence[str]
    ) -> dict[str, FileState]:
        found = real(self, repo_id, rel_paths)
        calls["n"] += 1
        return {
            path: state.model_copy(update={"content_hash": f"generation-{calls['n']}"})
            for path, state in found.items()
        }

    monkeypatch.setattr(SqliteFileStore, "states_for", drifting)
    with pytest.raises(IndexChangedError):
        service.inspect(_config_request())

    assert calls["n"] >= 6, "the read was not retried before being refused"


def test_a_single_reindex_settles_into_one_coherent_snapshot(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read that lost one race and won the next answers from the new generation."""
    from repoatlas.storage.sqlite.stores import SqliteFileStore

    service = CapabilityService(initialized)
    real = SqliteFileStore.states_for
    calls = {"n": 0}

    def once(self: SqliteFileStore, repo_id: int, rel_paths: Sequence[str]) -> dict[str, FileState]:
        found = real(self, repo_id, rel_paths)
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                path: state.model_copy(update={"content_hash": "a-generation-that-moved"})
                for path, state in found.items()
            }
        return found

    monkeypatch.setattr(SqliteFileStore, "states_for", once)
    response = service.inspect(_config_request())

    hashes = {segment.ref.file_content_hash for segment in response.segments}
    assert hashes == {response.file.indexed_content_hash}
    assert "a-generation-that-moved" not in hashes


def test_the_reader_takes_no_writer_lock(initialized: Workspace) -> None:
    """Inspection must not block a sync, or wait behind one, to read."""
    source = Path("repoatlas/capabilities/service.py").read_text(encoding="utf-8")
    inspect_source = source[source.index("def inspect(") :]
    assert "writer_lock" not in inspect_source
