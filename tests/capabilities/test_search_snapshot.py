"""Search evidence names the indexed snapshot that supplied it (V2, Slice D fix).

Two facts that look alike and are not:

* an :class:`~repoatlas.models.investigation.EvidenceRef` describes **the indexed
  snapshot the text came from** — that file's content hash, and the facts version
  that file was parsed under;
* freshness describes **how that snapshot compares with the running build and the
  live repository**.

Search used to fill the second field of the first fact from the running build's
current facts version. On a current index the two coincide and nothing shows. On
an index one build behind they diverge, and the reference then claims the evidence
came from a snapshot that never produced it — and revalidating it against the very
row it was drawn from reports it stale.

The correction is not cosmetic, because Slice B makes a reference immutable at
allocation and keys evidence identity on span plus digest. Inspection already
recorded the file's own version, so before this fix the same span and the same
bytes ended up with two different immutable references depending on whether search
or inspection happened to see it first.

The second half of this file is about generations. Retrieval reads chunk text and
provenance reads a file row; outside one read view another process's sync can land
between them, pairing text from before it with a hash from after. That evidence
would revalidate cleanly against its own hash while describing a state of the file
that never existed, which is worse than an error.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.freshness import FreshnessState
from repoatlas.models.inspection import InspectRequest, InspectTarget
from repoatlas.models.investigation import EvidenceStatus, IndexedEvidenceState
from repoatlas.models.repository import FileState
from repoatlas.models.search import SearchRequest
from repoatlas.services.workspace import Workspace

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)
SERVICE_HOST = "Where is SERVICE_HOST configured?"


def _service(workspace: Workspace) -> CapabilityService:
    return CapabilityService(workspace)


def _stored_versions(workspace: Workspace) -> set[str]:
    rows = workspace.database.execute("SELECT DISTINCT parser_version FROM files").fetchall()
    return {row["parser_version"] for row in rows}


# ---------------------------------------------------------------------------
# The reference names the file's own snapshot
# ---------------------------------------------------------------------------


def test_evidence_names_the_facts_version_its_file_was_indexed_under(
    initialized: Workspace,
) -> None:
    """The value comes from the file row, not from the process that is running."""
    response = _service(initialized).search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    assert response.evidence

    stored = _stored_versions(initialized)
    for item in response.evidence:
        assert item.ref.indexed_facts_version in stored


def test_an_outdated_build_does_not_change_what_the_evidence_came_from(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case the correction exists for.

    The index was produced by one build and a newer one is running. Freshness
    says so. The evidence still came from the older snapshot, and its reference
    has to keep saying that — otherwise the reference reports the build rather
    than the source, and the same fact ends up in two places with one of them
    false.
    """
    from repoatlas.services import freshness_service

    indexed_version = next(iter(_stored_versions(initialized)))
    running_version = f"{indexed_version}+a-newer-build"
    assert running_version != indexed_version

    monkeypatch.setattr(freshness_service, "facts_version", lambda *_: running_version)
    response = _service(initialized).search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))

    # The build has moved on, and freshness is where that is reported.
    assert response.freshness.state is FreshnessState.STALE_BUILD
    assert all(repo.outdated_files > 0 for repo in response.freshness.repositories)

    # The evidence still describes the snapshot that supplied it.
    assert response.evidence
    for item in response.evidence:
        assert item.ref.indexed_facts_version == indexed_version
        assert item.ref.indexed_facts_version != running_version


def test_evidence_from_an_outdated_build_revalidates_against_its_own_row(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A truthful reference stays valid against the row it was drawn from.

    This is the consequence that makes the correction load-bearing rather than
    tidy. Stamping the running build's version on the reference made evidence
    revalidate as ``STALE`` immediately, against a file nobody had touched —
    indistinguishable from ordinary drift, and wrong.

    Whether stale evidence may ground an answer is a separate policy and belongs
    to verification. This is only about the comparison being honest.
    """
    from repoatlas.services import freshness_service

    monkeypatch.setattr(freshness_service, "facts_version", lambda *_: "a-newer-build")
    service = _service(initialized)
    response = service.search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    assert response.evidence

    ledger = service.sessions.get(response.investigation_id).ledger
    snapshot = {}
    for entry in ledger.entries():
        repo = next(
            r for r in initialized.repositories.list_all() if r.slug == entry.ref.repository_slug
        )
        assert repo.id is not None
        state = initialized.files.states_for(repo.id, [entry.ref.path])[entry.ref.path]
        snapshot[(entry.ref.repository_slug, entry.ref.path)] = IndexedEvidenceState(
            repository_slug=entry.ref.repository_slug,
            path=entry.ref.path,
            exists=True,
            content_hash=state.content_hash,
            parser_version=state.parser_version,
        )

    ledger.revalidate(snapshot)
    assert all(entry.status is EvidenceStatus.VALID for entry in ledger.entries())


# ---------------------------------------------------------------------------
# Order independence
# ---------------------------------------------------------------------------


def _reference_identity(ref: object) -> tuple[object, ...]:
    return (
        ref.repository_slug,  # type: ignore[attr-defined]
        ref.path,  # type: ignore[attr-defined]
        ref.line_start,  # type: ignore[attr-defined]
        ref.line_end,  # type: ignore[attr-defined]
        ref.span_digest,  # type: ignore[attr-defined]
        ref.file_content_hash,  # type: ignore[attr-defined]
        ref.indexed_facts_version,  # type: ignore[attr-defined]
    )


def test_search_first_and_inspect_first_leave_the_same_immutable_reference(
    initialized: Workspace,
) -> None:
    """Which capability saw a span first is an accident of the caller's order.

    An immutable reference that depended on it would make evidence identity a
    property of the investigation's history rather than of the repository, and two
    agents doing the same work in a different order would hold different evidence
    for the same bytes.
    """
    service = _service(initialized)

    found = service.search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    cited = next(item for item in found.evidence if item.ref.line_start is not None)

    # Sequence A: search, then inspect the same lines in the same investigation.
    widened = service.inspect(
        InspectRequest(
            context=GLOBAL,
            target=InspectTarget(evidence_id=cited.id),
            investigation_id=found.investigation_id,
            context_lines=0,
        )
    )
    from_a = next((s for s in widened.segments if s.ref.span_digest == cited.ref.span_digest), None)
    assert from_a is not None, "inspection did not reproduce the searched span"
    # Identical bytes at identical coordinates are one piece of evidence.
    assert from_a.id == cited.id
    assert _reference_identity(from_a.ref) == _reference_identity(cited.ref)

    # Sequence B: a separate investigation, inspection first.
    inspected = service.inspect(
        InspectRequest(
            context=CapabilityContext(
                scope=CapabilityScope.REPOSITORY, repository=cited.ref.repository_slug
            ),
            target=InspectTarget(path=cited.ref.path),
            line_start=cited.ref.line_start,
            line_end=cited.ref.line_end,
            context_lines=0,
        )
    )
    from_b = next(
        (s for s in inspected.segments if s.ref.span_digest == cited.ref.span_digest), None
    )
    assert from_b is not None
    assert _reference_identity(from_b.ref) == _reference_identity(cited.ref)


def test_the_two_capabilities_agree_on_snapshot_metadata_for_every_file(
    initialized: Workspace,
) -> None:
    """Broader than one span: the same file yields the same snapshot either way."""
    service = _service(initialized)
    found = service.search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    assert found.evidence

    seen: dict[str, tuple[str, str]] = {}
    for item in found.evidence:
        seen[f"{item.ref.repository_slug}/{item.ref.path}"] = (
            item.ref.file_content_hash,
            item.ref.indexed_facts_version,
        )

    for location, (content_hash, version) in seen.items():
        slug, _, path = location.partition("/")
        inspected = service.inspect(
            InspectRequest(
                context=CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=slug),
                target=InspectTarget(path=path),
            )
        )
        assert inspected.file.indexed_content_hash == content_hash, location
        assert inspected.file.indexed_facts_version == version, location


# ---------------------------------------------------------------------------
# One generation per search
# ---------------------------------------------------------------------------


def _rewrite_file_row_externally(database_path: Path, marker: str) -> None:
    """Commit a file-row change from a genuinely separate connection.

    A second connection is the point. Changing the row through the workspace's own
    connection would be invisible to this test, because a connection always sees
    its own writes and the snapshot being tested is the one that isolates a reader
    from *other* processes.
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


def test_a_sync_landing_mid_search_cannot_split_text_from_its_provenance(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The forbidden pairing, forced as precisely as it can be forced.

    Another process commits a new generation of every file row in the exact window
    between the funnel reading chunk text and the reference reading that file's
    hash. Without one read view the reference would name the new generation while
    the text came from the old one — and the result would revalidate cleanly
    against its own hash, which is what makes it worse than an error.
    """
    from repoatlas.retrieval.retriever import Retriever

    database_path = Path(initialized.database.path)
    real = Retriever.retrieve_detailed

    def retrieve_then_reindex(self: Retriever, *args: object, **kwargs: object) -> object:
        outcome = real(self, *args, **kwargs)  # type: ignore[arg-type]
        _rewrite_file_row_externally(database_path, "generation-b")
        return outcome

    monkeypatch.setattr(Retriever, "retrieve_detailed", retrieve_then_reindex)
    response = _service(initialized).search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))

    assert response.evidence
    for item in response.evidence:
        assert item.ref.file_content_hash != "generation-b-hash", item.location
        assert item.ref.indexed_facts_version != "generation-b-version", item.location


def test_the_generation_that_lands_mid_search_is_seen_by_the_next_one(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The snapshot isolates one call, and does not outlive it.

    Otherwise the fix would be a cache: a reader pinned to a generation from
    minutes ago answers coherently and answers about the past.
    """
    from repoatlas.retrieval.retriever import Retriever

    database_path = Path(initialized.database.path)
    real = Retriever.retrieve_detailed
    service = _service(initialized)

    def retrieve_then_reindex(self: Retriever, *args: object, **kwargs: object) -> object:
        outcome = real(self, *args, **kwargs)  # type: ignore[arg-type]
        _rewrite_file_row_externally(database_path, "generation-c")
        monkeypatch.setattr(Retriever, "retrieve_detailed", real)
        return outcome

    monkeypatch.setattr(Retriever, "retrieve_detailed", retrieve_then_reindex)
    service.search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))

    later = service.search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    assert later.evidence
    assert all(item.ref.file_content_hash == "generation-c-hash" for item in later.evidence)


def test_the_snapshot_leaves_the_connection_usable_after_a_failure(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read view must not outlive the call that opened it, failure included.

    A transaction left open by an exception would pin every later read in this
    process to a generation that is already gone, and nothing would report it.
    """
    from repoatlas.retrieval.retriever import Retriever

    def explode(*_: object, **__: object) -> object:
        raise RuntimeError("retrieval failed inside the snapshot")

    monkeypatch.setattr(Retriever, "retrieve_detailed", explode)
    service = _service(initialized)
    with pytest.raises(RuntimeError):
        service.search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))

    monkeypatch.undo()
    assert initialized.database.connection.in_transaction is False

    _rewrite_file_row_externally(Path(initialized.database.path), "generation-d")
    recovered = service.search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    assert recovered.evidence
    assert all(item.ref.file_content_hash == "generation-d-hash" for item in recovered.evidence)


def test_a_failed_search_records_nothing_and_leaves_no_investigation(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No evidence id is allocated before provenance is known coherent."""
    from repoatlas.retrieval.retriever import Retriever

    service = _service(initialized)
    before = len(service.sessions)

    def explode(*_: object, **__: object) -> object:
        raise RuntimeError("retrieval failed inside the snapshot")

    monkeypatch.setattr(Retriever, "retrieve_detailed", explode)
    with pytest.raises(RuntimeError):
        service.search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))

    assert len(service.sessions) == before


def test_an_existing_investigation_is_untouched_by_a_failed_search(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from repoatlas.retrieval.retriever import Retriever

    service = _service(initialized)
    found = service.search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    ledger = service.sessions.get(found.investigation_id).ledger
    held = {entry.id: entry for entry in ledger.entries()}

    def explode(*_: object, **__: object) -> object:
        raise RuntimeError("retrieval failed inside the snapshot")

    monkeypatch.setattr(Retriever, "retrieve_detailed", explode)
    with pytest.raises(RuntimeError):
        service.search(
            SearchRequest(
                question="another question",
                context=GLOBAL,
                investigation_id=found.investigation_id,
            )
        )

    assert {entry.id: entry for entry in ledger.entries()} == held


def test_a_provenance_failure_allocates_no_evidence_at_all(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is recorded until every reference's provenance is known.

    The failure is arranged part-way through on purpose: earlier candidates
    resolve and a later one does not. That is the shape the tempting shortcut
    survives — banking the references that did resolve looks like salvaging work,
    and it hands out ids for part of a response the caller never received, with
    the rest of the answer unaccounted for.
    """
    from repoatlas.capabilities.errors import IndexInconsistencyError
    from repoatlas.storage.sqlite.stores import SqliteFileStore

    service = _service(initialized)
    found = service.search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    paths = [item.ref.path for item in found.evidence]
    assert paths
    unresolvable = next((path for path in reversed(paths) if path != paths[0]), None)
    assert unresolvable is not None, "this question must reach more than one file"

    ledger = service.sessions.get(found.investigation_id).ledger
    held = {entry.id: entry for entry in ledger.entries()}
    assert held

    real = SqliteFileStore.states_for

    def missing_one(
        self: SqliteFileStore, repo_id: int, rel_paths: Sequence[str]
    ) -> dict[str, FileState]:
        found_states = real(self, repo_id, rel_paths)
        return {path: state for path, state in found_states.items() if path != unresolvable}

    monkeypatch.setattr(SqliteFileStore, "states_for", missing_one)

    # A brand new investigation: anything banked would survive as a real id.
    sessions_before = len(service.sessions)
    with pytest.raises(IndexInconsistencyError):
        service.search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    assert len(service.sessions) == sessions_before, "a partial batch kept its investigation alive"

    # And an investigation the caller already owned is untouched.
    with pytest.raises(IndexInconsistencyError):
        service.search(
            SearchRequest(
                question=SERVICE_HOST,
                context=GLOBAL,
                investigation_id=found.investigation_id,
            )
        )
    assert {entry.id: entry for entry in ledger.entries()} == held


def test_the_snapshot_does_not_make_the_funnel_run_twice(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A consistent view is not a retry, and must not become one."""
    from repoatlas.retrieval.retriever import Retriever

    calls: list[str] = []
    real = Retriever.retrieve_detailed

    def counting(self: Retriever, question: str, *args: object, **kwargs: object) -> object:
        calls.append(question)
        return real(self, question, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Retriever, "retrieve_detailed", counting)
    _service(initialized).search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    assert calls == [SERVICE_HOST]


def test_search_takes_no_writer_lock_to_read_consistently(initialized: Workspace) -> None:
    """A read view is not the writer lock, and must not quietly become one."""
    del initialized
    source = Path("repoatlas/capabilities/service.py").read_text(encoding="utf-8")
    search_source = source[source.index("def search(") : source.index("def inspect(")]
    assert "writer_lock" not in search_source
    assert "read_snapshot" in search_source
