"""Evidence identity, metadata strengthening and atomic capacity.

The invariant every test here defends:

    An evidence id is bound to the bytes that were supplied for a span, is never
    rebound, and is never silently forgotten while its investigation lives.

Each of the three ways to break it fails quietly rather than loudly, which is why
they are asserted rather than reviewed. Rebinding an id makes every claim already
citing it point at content the caller never saw. Evicting an entry turns a good
citation into one that no longer resolves, which verification cannot tell apart
from a fabrication. Half-recording a batch produces exactly the same ambiguity for
the ids that did not make it.
"""

from __future__ import annotations

import pytest
from repoatlas.capabilities.ledger import EvidenceLedger, LedgerCapacityError
from repoatlas.models.evidence import EvidenceSource, EvidenceTier, MatchClass
from repoatlas.models.investigation import (
    DEFAULT_LEDGER_CAPACITY,
    EvidenceRef,
    EvidenceStatus,
    LedgerEntry,
    ProposedEvidence,
)

SLUG = "team/service"
PATH = "src/config.py"


def _ref(
    *,
    path: str = PATH,
    start: int | None = 10,
    end: int | None = 14,
    digest: str = "digest-a",
    file_hash: str = "filehash-1",
    facts: str = "facts-1",
    slug: str = SLUG,
) -> EvidenceRef:
    return EvidenceRef(
        repository_slug=slug,
        path=path,
        line_start=start,
        line_end=end,
        span_digest=digest,
        file_content_hash=file_hash,
        indexed_facts_version=facts,
    )


def _proposed(
    ref: EvidenceRef | None = None,
    *,
    tier: EvidenceTier = EvidenceTier.DETERMINISTIC,
    match: MatchClass = MatchClass.EXACT_CONTENT,
    origin: EvidenceSource = EvidenceSource.EXACT,
    reason: str = "",
    length: int = 42,
    **ref_kwargs: object,
) -> ProposedEvidence:
    return ProposedEvidence(
        ref=ref if ref is not None else _ref(**ref_kwargs),  # type: ignore[arg-type]
        repository_name="service",
        tier=tier,
        match=match,
        origin=origin,
        reason=reason,
        text_length=length,
    )


def _ledger(capacity: int = DEFAULT_LEDGER_CAPACITY) -> EvidenceLedger:
    return EvidenceLedger("investigation-under-test", capacity=capacity)


def _snapshot(ledger: EvidenceLedger) -> tuple[LedgerEntry, ...]:
    """Everything the ledger holds, for proving an operation changed nothing."""
    return ledger.entries()


# -- identity ------------------------------------------------------------------


def test_the_first_evidence_is_e1_and_numbering_starts_there() -> None:
    ledger = _ledger()
    (entry,) = ledger.record([_proposed()])
    assert entry.id == "E1"
    assert entry.status is EvidenceStatus.VALID
    assert entry.supersedes is None
    assert entry.superseded_by is None


def test_distinct_evidence_gets_distinct_increasing_ids() -> None:
    ledger = _ledger()
    first, second = ledger.record([_proposed(), _proposed(start=30, end=34, digest="digest-b")])
    assert (first.id, second.id) == ("E1", "E2")


def test_the_same_span_with_the_same_bytes_is_the_same_evidence() -> None:
    """Re-reading what you were already shown allocates nothing."""
    ledger = _ledger()
    (first,) = ledger.record([_proposed()])
    (again,) = ledger.record([_proposed()])
    assert again.id == first.id
    assert len(ledger) == 1


def test_the_same_bytes_at_a_different_span_is_different_evidence() -> None:
    """Identity is coordinates *and* bytes. Identical text elsewhere is elsewhere.

    Content-deduplicating across locations would let a citation to one place be
    satisfied by evidence from another, which is precisely the provenance claim a
    citation exists to make.
    """
    ledger = _ledger()
    (first,) = ledger.record([_proposed(start=10, end=14, digest="same-text")])
    (second,) = ledger.record([_proposed(start=40, end=44, digest="same-text")])
    assert first.id != second.id
    assert first.status is EvidenceStatus.VALID
    assert second.status is EvidenceStatus.VALID


def test_the_same_span_with_new_bytes_allocates_a_new_id_and_supersedes_the_old() -> None:
    ledger = _ledger()
    (old,) = ledger.record([_proposed(digest="digest-a")])
    (new,) = ledger.record([_proposed(digest="digest-b")])

    assert new.id != old.id
    assert new.supersedes == old.id
    assert new.status is EvidenceStatus.VALID

    replaced = ledger.get(old.id)
    assert replaced is not None
    assert replaced.status is EvidenceStatus.SUPERSEDED
    assert replaced.superseded_by == new.id
    # The old record still says exactly what it always said.
    assert replaced.ref.span_digest == "digest-a"


def test_supersession_records_the_immediate_successor_not_the_latest() -> None:
    """A chain of digests is a history, and rewriting each link would erase it."""
    ledger = _ledger()
    (first,) = ledger.record([_proposed(digest="A")])
    (second,) = ledger.record([_proposed(digest="B")])
    (third,) = ledger.record([_proposed(digest="C")])

    stored_first = ledger.get(first.id)
    stored_second = ledger.get(second.id)
    assert stored_first is not None and stored_second is not None

    assert stored_first.superseded_by == second.id
    assert stored_second.superseded_by == third.id
    assert third.supersedes == second.id
    assert stored_first.status is EvidenceStatus.SUPERSEDED
    assert stored_second.status is EvidenceStatus.SUPERSEDED


def test_resupplying_superseded_bytes_does_not_resurrect_the_dead_id() -> None:
    """A superseded id stays superseded, so the old digest returning is new evidence.

    Handing back an id whose status is SUPERSEDED would give the caller something
    that looks citable and is not, and reviving it would put two live ids on one
    span.
    """
    ledger = _ledger()
    (first,) = ledger.record([_proposed(digest="A")])
    (second,) = ledger.record([_proposed(digest="B")])
    (third,) = ledger.record([_proposed(digest="A")])

    assert third.id not in {first.id, second.id}
    assert third.supersedes == second.id
    stored_first = ledger.get(first.id)
    assert stored_first is not None
    assert stored_first.status is EvidenceStatus.SUPERSEDED
    assert stored_first.superseded_by == second.id


def test_ids_are_never_reused_or_renumbered() -> None:
    ledger = _ledger()
    ledger.record([_proposed(digest="A")])
    ledger.record([_proposed(digest="B")])
    ledger.record([_proposed(start=90, end=94, digest="C")])
    assert [entry.id for entry in ledger.entries()] == ["E1", "E2", "E3"]
    # Superseding E1 consumed E2; the next distinct span must not reuse either.
    assert ledger.get("E3") is not None
    (fourth,) = ledger.record([_proposed(start=200, end=204, digest="D")])
    assert fourth.id == "E4"


def test_file_identity_evidence_has_no_line_range_and_is_still_identifiable() -> None:
    """A filename match knows nothing about any line, so it claims none."""
    ledger = _ledger()
    (entry,) = ledger.record([_proposed(start=None, end=None, digest="identity")])
    assert entry.ref.line_start is None and entry.ref.line_end is None
    assert entry.ref.location == f"{SLUG}/{PATH}"
    (again,) = ledger.record([_proposed(start=None, end=None, digest="identity")])
    assert again.id == entry.id


def test_one_batch_touching_a_span_twice_supersedes_within_itself() -> None:
    """The second item must see what the first would have done, or both allocate."""
    ledger = _ledger()
    first, second = ledger.record([_proposed(digest="A"), _proposed(digest="B")])
    assert first.id != second.id
    assert ledger.get(first.id) is not None
    stored = ledger.get(first.id)
    assert stored is not None and stored.status is EvidenceStatus.SUPERSEDED
    assert second.supersedes == first.id


def test_one_batch_repeating_identical_evidence_allocates_once() -> None:
    ledger = _ledger()
    first, second = ledger.record([_proposed(), _proposed()])
    assert first.id == second.id
    assert len(ledger) == 1


# -- metadata strengthening -----------------------------------------------------


def _lexical() -> ProposedEvidence:
    return _proposed(
        tier=EvidenceTier.ASSOCIATIVE,
        match=MatchClass.LEXICAL,
        origin=EvidenceSource.LEXICAL,
        reason="mentions load_config",
    )


def _exact() -> ProposedEvidence:
    return _proposed(
        tier=EvidenceTier.DETERMINISTIC,
        match=MatchClass.EXACT_NAME,
        origin=EvidenceSource.SYMBOL,
        reason="defines load_config",
    )


def test_a_stronger_route_strengthens_the_entry_without_a_new_id() -> None:
    """The bytes did not change, so the evidence did not. Only the metadata did."""
    ledger = _ledger()
    (weak,) = ledger.record([_lexical()])
    assert weak.tier is EvidenceTier.ASSOCIATIVE
    assert weak.match is MatchClass.LEXICAL

    (strong,) = ledger.record([_exact()])
    assert strong.id == weak.id
    assert strong.tier is EvidenceTier.DETERMINISTIC
    assert strong.match is MatchClass.EXACT_NAME
    assert len(ledger) == 1


def test_discovery_order_does_not_change_the_final_entry() -> None:
    """min over both precedence enums and a sorted set union: order cannot matter."""
    forwards = _ledger()
    forwards.record([_lexical()])
    forwards.record([_exact()])

    backwards = _ledger()
    backwards.record([_exact()])
    backwards.record([_lexical()])

    (a,) = forwards.entries()
    (b,) = backwards.entries()
    assert a.model_dump(exclude={"allocated_at"}) == b.model_dump(exclude={"allocated_at"})


def test_every_route_that_found_the_evidence_is_kept() -> None:
    ledger = _ledger()
    ledger.record([_lexical()])
    (entry,) = ledger.record([_exact()])
    assert set(entry.origins) == {EvidenceSource.LEXICAL, EvidenceSource.SYMBOL}
    assert set(entry.reasons) == {"mentions load_config", "defines load_config"}
    assert list(entry.origins) == sorted(entry.origins, key=str)
    assert list(entry.reasons) == sorted(entry.reasons)


def test_a_later_weak_result_never_weakens_a_strong_entry() -> None:
    """An embedding neighbour adds a route, not a doubt."""
    ledger = _ledger()
    (strong,) = ledger.record([_exact()])
    (after,) = ledger.record([_lexical()])
    assert after.id == strong.id
    assert after.tier is EvidenceTier.DETERMINISTIC
    assert after.match is MatchClass.EXACT_NAME


def test_strengthening_does_not_move_the_immutable_fields() -> None:
    ledger = _ledger()
    (first,) = ledger.record([_lexical()])
    (second,) = ledger.record([_exact()])
    assert second.id == first.id
    assert second.ref == first.ref
    assert second.allocated_at == first.allocated_at
    assert second.status == first.status


# -- capacity ---------------------------------------------------------------------


def test_filling_the_ledger_exactly_succeeds() -> None:
    ledger = _ledger(capacity=3)
    entries = ledger.record([_proposed(start=n, end=n, digest=f"d{n}") for n in range(1, 4)])
    assert [entry.id for entry in entries] == ["E1", "E2", "E3"]
    assert ledger.full is True
    assert ledger.available == 0


def test_one_entry_past_capacity_is_refused() -> None:
    ledger = _ledger(capacity=2)
    ledger.record([_proposed(start=n, end=n, digest=f"d{n}") for n in range(1, 3)])
    with pytest.raises(LedgerCapacityError):
        ledger.record([_proposed(start=9, end=9, digest="d9")])
    assert len(ledger) == 2


def test_three_needed_with_two_free_records_nothing_at_all() -> None:
    """The load-bearing case. A partial response is worse than a refused one."""
    ledger = _ledger(capacity=10)
    ledger.record([_proposed(start=n, end=n, digest=f"d{n}") for n in range(1, 9)])
    assert len(ledger) == 8

    before = _snapshot(ledger)
    with pytest.raises(LedgerCapacityError) as caught:
        ledger.record([_proposed(start=n, end=n, digest=f"new{n}") for n in (20, 21, 22)])

    assert caught.value.required == 3
    assert caught.value.available == 2
    assert caught.value.capacity == 10
    assert len(ledger) == 8
    assert ledger.get("E9") is None
    assert ledger.get("E10") is None
    assert ledger.get("E11") is None
    assert _snapshot(ledger) == before


def test_a_refused_batch_performs_no_metadata_merge_either() -> None:
    """Atomicity covers strengthening, not only allocation."""
    ledger = _ledger(capacity=2)
    ledger.record([_lexical(), _proposed(start=99, end=99, digest="other")])
    assert ledger.full is True

    before = _snapshot(ledger)
    with pytest.raises(LedgerCapacityError):
        # One item would strengthen E1; the other needs an id there is no room for.
        ledger.record([_exact(), _proposed(start=50, end=50, digest="fresh")])

    assert _snapshot(ledger) == before
    (entry, _other) = ledger.entries()
    assert entry.tier is EvidenceTier.ASSOCIATIVE
    assert entry.match is MatchClass.LEXICAL


def test_re_supplying_known_evidence_succeeds_at_full_capacity() -> None:
    """Re-reading what you already hold creates no new citable surface."""
    ledger = _ledger(capacity=2)
    first, second = ledger.record(
        [_proposed(start=1, end=1, digest="a"), _proposed(start=2, end=2, digest="b")]
    )
    assert ledger.full is True

    again = ledger.record(
        [_proposed(start=1, end=1, digest="a"), _proposed(start=2, end=2, digest="b")]
    )
    assert [entry.id for entry in again] == [first.id, second.id]
    assert len(ledger) == 2


def test_metadata_strengthening_alone_succeeds_at_full_capacity() -> None:
    ledger = _ledger(capacity=1)
    (weak,) = ledger.record([_lexical()])
    assert ledger.full is True

    (strong,) = ledger.record([_exact()])
    assert strong.id == weak.id
    assert strong.tier is EvidenceTier.DETERMINISTIC
    assert len(ledger) == 1


def test_superseded_entries_still_occupy_capacity() -> None:
    """They are still records. Reclaiming them would be forgetting evidence."""
    ledger = _ledger(capacity=2)
    ledger.record([_proposed(digest="A")])
    ledger.record([_proposed(digest="B")])
    assert len(ledger) == 2
    assert ledger.full is True
    with pytest.raises(LedgerCapacityError):
        ledger.record([_proposed(digest="C")])


def test_the_capacity_error_names_the_numbers_rather_than_prose() -> None:
    ledger = _ledger(capacity=1)
    ledger.record([_proposed(start=1, end=1, digest="a")])
    with pytest.raises(LedgerCapacityError) as caught:
        ledger.record(
            [_proposed(start=2, end=2, digest="b"), _proposed(start=3, end=3, digest="c")]
        )
    assert (caught.value.required, caught.value.available, caught.value.capacity) == (2, 0, 1)


def test_the_summary_reports_headroom_before_a_caller_is_refused() -> None:
    ledger = _ledger(capacity=5)
    ledger.record([_proposed(start=n, end=n, digest=f"d{n}") for n in range(1, 4)])
    summary = ledger.summary()
    assert summary.total == 3
    assert summary.valid == 3
    assert summary.capacity == 5
    assert summary.available == 2
    assert summary.full is False
    assert summary.investigation_id == "investigation-under-test"


# -- no historical text ------------------------------------------------------------


def test_a_ledger_entry_has_no_field_that_could_hold_supplied_text() -> None:
    """A field that exists "in case" is a field something eventually reads as source."""
    fields = set(LedgerEntry.model_fields)
    assert "text" not in fields
    assert "excerpt" not in fields
    assert "content" not in fields
    assert "text_length" in fields


def test_the_ledger_exposes_no_way_to_read_historical_bytes() -> None:
    ledger = _ledger()
    ledger.record([_proposed()])
    public = {name for name in dir(ledger) if not name.startswith("_")}
    for banned in ("text", "content", "source", "bytes", "excerpt"):
        assert not any(banned in name for name in public), (banned, public)


def test_the_reference_survives_even_though_the_text_never_existed_here() -> None:
    """What E1 pointed at is answerable forever. What it said is not answerable at all."""
    ledger = _ledger()
    (entry,) = ledger.record([_proposed(length=4096)])
    serialized = entry.model_dump_json()
    assert entry.ref.span_digest == "digest-a"
    assert entry.ref.file_content_hash == "filehash-1"
    assert entry.ref.indexed_facts_version == "facts-1"
    assert entry.text_length == 4096
    # A length is not a reconstruction.
    assert "digest-a" in serialized
