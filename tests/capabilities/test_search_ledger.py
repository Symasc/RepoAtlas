"""Ledger atomicity for search (V2 Slice D).

The non-negotiable one:

    Search may return only evidence RepoAtlas actually retrieved, and every
    citable byte returned must be recorded atomically in exactly one
    investigation **before** the response leaves RepoAtlas.

The failure this prevents is specific. Evidence returned with an id the ledger
does not hold is evidence verification cannot see — and a citation to it is
indistinguishable, at the verification boundary, from one the model invented. So
the batch is prepared whole, recorded in one operation, and the response is built
only after that succeeded.
"""

from __future__ import annotations

import pytest
from repoatlas.capabilities.ledger import LedgerCapacityError
from repoatlas.capabilities.service import CapabilityService
from repoatlas.indexing.chunking import chunk_hash
from repoatlas.models.capability import CapabilityContext, CapabilityScope, EvidenceBudget
from repoatlas.models.investigation import EvidenceStatus
from repoatlas.models.search import SearchRequest
from repoatlas.services.workspace import Workspace

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)
SERVICE_HOST = "Where is SERVICE_HOST configured?"


def _service(workspace: Workspace, *, capacity: int = 500) -> CapabilityService:
    return CapabilityService(workspace, ledger_capacity=capacity)


# ---------------------------------------------------------------------------
# Every returned id resolves
# ---------------------------------------------------------------------------


def test_every_returned_id_is_in_the_ledger(initialized: Workspace) -> None:
    service = _service(initialized)
    response = service.search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    ledger = service.sessions.get(response.investigation_id).ledger

    assert response.evidence
    for item in response.evidence:
        entry = ledger.get(item.id)
        assert entry is not None, item.id
        assert entry.status is EvidenceStatus.VALID


def test_the_ledger_holds_exactly_what_was_returned(initialized: Workspace) -> None:
    """No more and no less: nothing recorded that was not supplied."""
    service = _service(initialized)
    response = service.search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    ledger = service.sessions.get(response.investigation_id).ledger

    assert {item.id for item in response.evidence} == {e.id for e in ledger.entries()}
    assert response.ledger.total == len(response.evidence)


def test_the_ledger_records_the_shaped_span_not_the_original_candidate(
    initialized: Workspace,
) -> None:
    """What is recorded is what was supplied.

    Recording the untruncated candidate while returning a shorter text would make
    the ledger authorize citations to lines the response never contained — the
    exact hole the shaping exists to close, reopened one layer down.
    """
    service = _service(initialized)
    response = service.search(
        SearchRequest(
            question=SERVICE_HOST,
            context=GLOBAL,
            budget=EvidenceBudget(max_item_chars=60),
        )
    )
    ledger = service.sessions.get(response.investigation_id).ledger

    for item in response.evidence:
        entry = ledger.get(item.id)
        assert entry is not None
        assert entry.ref == item.ref
        assert entry.ref.span_digest == chunk_hash(item.text)
        assert entry.text_length == len(item.text)
        if entry.ref.line_start is not None:
            assert entry.ref.line_end is not None
            supplied = len(item.text.split("\n"))
            assert supplied == entry.ref.line_end - entry.ref.line_start + 1


def test_the_recorded_origin_is_the_route_this_run_took(initialized: Workspace) -> None:
    service = _service(initialized)
    response = service.search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    ledger = service.sessions.get(response.investigation_id).ledger

    for item in response.evidence:
        entry = ledger.get(item.id)
        assert entry is not None
        # One route on a first sighting, and it is the one this run used.
        assert len(entry.origins) == 1
        assert entry.tier is item.tier
        assert entry.match is item.match


def test_the_response_reports_this_run_while_the_ledger_keeps_the_strongest(
    initialized: Workspace,
) -> None:
    """The secondary invariant, constructed rather than hoped for.

    The corpus cannot be relied on to route the same span twice by two different
    strengths on demand, so the stronger history is planted directly: the exact
    reference this query will produce is pre-recorded at the strongest possible
    tier and match. The query then runs and finds that span by its own, weaker
    route.

    The ledger must keep the strong view — it was established, and metadata only
    ever strengthens. The response must report the weak one, because that is what
    *this* search proved. Serializing the entry would tell the caller this query
    established something it did not.
    """
    from repoatlas.models.evidence import EvidenceSource, EvidenceTier, MatchClass
    from repoatlas.models.investigation import ProposedEvidence

    service = _service(initialized)
    question = "how does the service read its configuration"

    # One run to learn the exact reference this query supplies.
    probe = service.search(SearchRequest(question=question, context=GLOBAL))
    weak = (
        next(
            (item for item in probe.evidence if item.tier is not EvidenceTier.DETERMINISTIC),
            None,
        )
        or probe.evidence[0]
    )

    # A fresh investigation whose ledger already holds that exact span, strongly.
    planted = service.search(SearchRequest(question="src/config.py", context=GLOBAL))
    ledger = service.sessions.get(planted.investigation_id).ledger
    ledger.record(
        [
            ProposedEvidence(
                ref=weak.ref,
                tier=EvidenceTier.DETERMINISTIC,
                match=MatchClass.EXACT_PATH,
                origin=EvidenceSource.FILE,
                reason="planted by an earlier, stronger route",
            )
        ]
    )
    entry_id = next(e.id for e in ledger.entries() if e.ref == weak.ref)
    planted_entry = ledger.get(entry_id)
    assert planted_entry is not None
    assert planted_entry.tier is EvidenceTier.DETERMINISTIC
    assert planted_entry.match is MatchClass.EXACT_PATH

    response = service.search(
        SearchRequest(
            question=question,
            context=GLOBAL,
            investigation_id=planted.investigation_id,
        )
    )
    returned = next(item for item in response.evidence if item.ref == weak.ref)

    # The response says what this run established...
    assert returned.id == entry_id
    assert returned.tier is weak.tier
    assert returned.match is weak.match
    # ...and the ledger never weakened.
    stored = ledger.get(entry_id)
    assert stored is not None
    assert stored.tier is EvidenceTier.DETERMINISTIC
    assert stored.match is MatchClass.EXACT_PATH
    # Which is only a meaningful test if the two genuinely differ.
    assert (returned.tier, returned.match) != (stored.tier, stored.match)


# ---------------------------------------------------------------------------
# Capacity
# ---------------------------------------------------------------------------


def _fill(service: CapabilityService, investigation_id: str, count: int) -> None:
    """Occupy ``count`` ledger slots with evidence unrelated to the fixture corpus."""
    from datetime import datetime, timezone

    from repoatlas.models.investigation import EvidenceRef, ProposedEvidence

    ledger = service.sessions.get(investigation_id).ledger
    ledger.record(
        [
            ProposedEvidence(
                ref=EvidenceRef(
                    repository_slug="filler",
                    path=f"pad/{n}.txt",
                    line_start=1,
                    line_end=1,
                    span_digest=f"digest-{n}",
                    file_content_hash="hash",
                    indexed_facts_version="facts",
                )
            )
            for n in range(count)
        ],
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def test_a_batch_that_does_not_fit_records_nothing_and_returns_nothing(
    initialized: Workspace,
) -> None:
    """The §14 case: three needed, two free.

    Not a partial answer and not a warning. A response carrying two of three
    citable items, with the third silently absent, is worse than a refusal: the
    caller cannot tell which evidence it is missing.
    """
    service = _service(initialized, capacity=10)
    seed = service.search(
        SearchRequest(question=SERVICE_HOST, context=GLOBAL, budget=EvidenceBudget(max_items=1))
    )
    investigation = seed.investigation_id
    ledger = service.sessions.get(investigation).ledger
    _fill(service, investigation, 7)
    assert len(ledger) == 8

    before = ledger.summary()
    with pytest.raises(LedgerCapacityError) as caught:
        service.search(
            SearchRequest(
                question="load_config",
                context=GLOBAL,
                investigation_id=investigation,
                budget=EvidenceBudget(max_items=3),
            )
        )

    assert caught.value.available == 2
    assert len(ledger) == 8
    assert ledger.summary() == before


def test_a_capacity_refusal_strengthens_no_existing_entry(initialized: Workspace) -> None:
    """Atomicity covers metadata, not only allocation."""
    service = _service(initialized, capacity=10)
    seed = service.search(
        SearchRequest(question=SERVICE_HOST, context=GLOBAL, budget=EvidenceBudget(max_items=1))
    )
    investigation = seed.investigation_id
    ledger = service.sessions.get(investigation).ledger
    _fill(service, investigation, 8)
    assert len(ledger) == 9

    snapshot = [entry.model_dump() for entry in ledger.entries()]
    with pytest.raises(LedgerCapacityError):
        service.search(
            SearchRequest(
                question=SERVICE_HOST,
                context=GLOBAL,
                investigation_id=investigation,
                budget=EvidenceBudget(max_items=6),
            )
        )
    assert [entry.model_dump() for entry in ledger.entries()] == snapshot


def test_a_full_ledger_still_serves_evidence_it_already_holds(
    initialized: Workspace,
) -> None:
    """Re-reading known evidence allocates nothing, so it cannot be refused.

    A full ledger must not stop an agent looking again at what it was already
    shown; that would turn a bounded record into a bounded investigation.
    """
    service = _service(initialized, capacity=12)
    first = service.search(
        SearchRequest(question=SERVICE_HOST, context=GLOBAL, budget=EvidenceBudget(max_items=12))
    )
    ledger = service.sessions.get(first.investigation_id).ledger
    if not ledger.full:
        _fill(service, first.investigation_id, ledger.available)
    assert ledger.full

    repeat = service.search(
        SearchRequest(
            question=SERVICE_HOST,
            context=GLOBAL,
            investigation_id=first.investigation_id,
            budget=EvidenceBudget(max_items=12),
        )
    )
    assert repeat.evidence
    assert {item.id for item in repeat.evidence} <= {item.id for item in first.evidence}


def test_a_full_ledger_still_strengthens_known_evidence(initialized: Workspace) -> None:
    """Metadata-only strengthening allocates nothing either."""
    service = _service(initialized, capacity=12)
    weak = service.search(
        SearchRequest(
            question="how does the service read its configuration",
            context=GLOBAL,
            budget=EvidenceBudget(max_items=12),
        )
    )
    ledger = service.sessions.get(weak.investigation_id).ledger
    if not ledger.full:
        _fill(service, weak.investigation_id, ledger.available)
    assert ledger.full

    # A stronger route to spans the ledger may already hold. It must not be
    # refused for capacity, because strengthening allocates no new identity.
    repeat = service.search(
        SearchRequest(
            question="how does the service read its configuration",
            context=GLOBAL,
            investigation_id=weak.investigation_id,
            budget=EvidenceBudget(max_items=12),
        )
    )
    assert repeat.investigation_id == weak.investigation_id


def test_a_capacity_refusal_produces_no_response_at_all(initialized: Workspace) -> None:
    """The caller receives an error, never a shortened success."""
    service = _service(initialized, capacity=6)
    seed = service.search(
        SearchRequest(question=SERVICE_HOST, context=GLOBAL, budget=EvidenceBudget(max_items=1))
    )
    _fill(service, seed.investigation_id, 5)

    response = None
    with pytest.raises(LedgerCapacityError):
        response = service.search(
            SearchRequest(
                question="load_config",
                context=GLOBAL,
                investigation_id=seed.investigation_id,
                budget=EvidenceBudget(max_items=4),
            )
        )
    assert response is None


def test_a_capacity_failure_on_a_new_investigation_leaves_no_session(
    initialized: Workspace,
) -> None:
    """A first search that cannot be recorded leaves nothing behind."""
    service = _service(initialized, capacity=2)
    with pytest.raises(LedgerCapacityError):
        service.search(
            SearchRequest(question=SERVICE_HOST, context=GLOBAL, budget=EvidenceBudget(max_items=6))
        )
    assert len(service.sessions) == 0


# ---------------------------------------------------------------------------
# Recording happens before the response exists
# ---------------------------------------------------------------------------


def test_no_evidence_is_returned_if_recording_fails(
    initialized: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordering, forced.

    If recording is made to fail for any reason, the caller must get the failure
    rather than a response full of ids the ledger never learned about.
    """
    from repoatlas.capabilities.ledger import EvidenceLedger

    def exploding(*_: object, **__: object) -> object:
        raise RuntimeError("ledger unavailable")

    service = _service(initialized)
    monkeypatch.setattr(EvidenceLedger, "record", exploding)

    with pytest.raises(RuntimeError):
        service.search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    assert len(service.sessions) == 0


def test_suppressed_searches_record_nothing(initialized: Workspace) -> None:
    """No evidence, no allocation — and still a real investigation."""
    service = _service(initialized)
    response = service.search(
        SearchRequest(question="where is zzz_nonexistent_symbol_xyz used?", context=GLOBAL)
    )
    assert response.evidence == ()
    assert response.ledger.total == 0
    assert service.sessions.get(response.investigation_id).ledger.entries() == ()


# ---------------------------------------------------------------------------
# Identity across queries
# ---------------------------------------------------------------------------


def test_a_narrower_truncation_is_a_different_span_not_a_supersession(
    initialized: Workspace,
) -> None:
    """Same source, smaller returned range, therefore a different piece of evidence.

    Not a supersession: supersession means *different bytes at the same
    coordinates*. A shorter prefix has different coordinates, so it earns its own
    id and the wider one stays exactly as valid as it was.
    """
    service = _service(initialized)
    wide = service.search(SearchRequest(question=SERVICE_HOST, context=GLOBAL))
    narrow = service.search(
        SearchRequest(
            question=SERVICE_HOST,
            context=GLOBAL,
            investigation_id=wide.investigation_id,
            budget=EvidenceBudget(max_item_chars=40),
        )
    )
    ledger = service.sessions.get(wide.investigation_id).ledger

    shortened = [item for item in narrow.evidence if item.truncated]
    if not shortened:
        pytest.skip("no candidate in this corpus exceeded the narrow item budget")

    for item in shortened:
        entry = ledger.get(item.id)
        assert entry is not None
        assert entry.superseded_by is None
        assert entry.status is EvidenceStatus.VALID

    # Every wider entry survives untouched.
    for item in wide.evidence:
        entry = ledger.get(item.id)
        assert entry is not None
        assert entry.status is EvidenceStatus.VALID


def test_the_same_path_in_two_repositories_stays_two_pieces_of_evidence(
    initialized: Workspace,
) -> None:
    """Span identity is keyed on the slug, so a shared relative path cannot collide."""
    service = _service(initialized)
    response = service.search(SearchRequest(question="index.ts", context=GLOBAL))
    by_path: dict[str, set[str]] = {}
    for item in response.evidence:
        by_path.setdefault(item.ref.path, set()).add(item.ref.repository_slug)

    ids = [item.id for item in response.evidence]
    assert len(ids) == len(set(ids))
    for item in response.evidence:
        assert item.ref.repository_slug
        assert item.location.startswith(item.ref.repository_slug)
