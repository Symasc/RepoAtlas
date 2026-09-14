"""Revalidating evidence against the index it came from (V1_SPEC §12).

Status is a statement about the **index**, never about the record. What ``E1``
referred to, and the bytes it referred to, are the same facts after a reindex as
before one; what changes is whether the index still agrees with them.

Two rules here are load-bearing and easy to get subtly wrong.

**A content hash is not enough.** A parser fix changes the facts derived from
identical bytes, so evidence taken before it describes the same file and a
different set of facts. Comparing hashes alone would report such evidence as
current.

**Supersession is not an index condition.** An id is superseded because different
bytes were later supplied for its span, and no amount of reindexing undoes that.
Letting it return to valid would put two live ids on one span.
"""

from __future__ import annotations

from repoatlas.capabilities.ledger import EvidenceLedger
from repoatlas.models.evidence import EvidenceSource, EvidenceTier, MatchClass
from repoatlas.models.investigation import (
    EvidenceRef,
    EvidenceStatus,
    IndexedEvidenceState,
    ProposedEvidence,
    state_index,
)

SLUG = "team/service"
PATH = "src/config.py"
FILE_HASH = "filehash-1"
FACTS = "schema:4||py:3||deps:2||chunks:2"


def _proposed(
    *,
    path: str = PATH,
    slug: str = SLUG,
    start: int | None = 10,
    end: int | None = 14,
    digest: str = "digest-a",
    file_hash: str = FILE_HASH,
    facts: str = FACTS,
) -> ProposedEvidence:
    return ProposedEvidence(
        ref=EvidenceRef(
            repository_slug=slug,
            path=path,
            line_start=start,
            line_end=end,
            span_digest=digest,
            file_content_hash=file_hash,
            indexed_facts_version=facts,
        ),
        repository_name="service",
        tier=EvidenceTier.DETERMINISTIC,
        match=MatchClass.EXACT_CONTENT,
        origin=EvidenceSource.EXACT,
        text_length=64,
    )


def _state(
    *,
    slug: str = SLUG,
    path: str = PATH,
    exists: bool = True,
    file_hash: str = FILE_HASH,
    facts: str = FACTS,
) -> IndexedEvidenceState:
    return IndexedEvidenceState(
        repository_slug=slug,
        path=path,
        exists=exists,
        content_hash=file_hash,
        parser_version=facts,
    )


def _ledger_with_one() -> tuple[EvidenceLedger, str]:
    ledger = EvidenceLedger("investigation-under-test")
    (entry,) = ledger.record([_proposed()])
    return ledger, entry.id


def _status(ledger: EvidenceLedger, evidence_id: str) -> EvidenceStatus:
    entry = ledger.get(evidence_id)
    assert entry is not None
    return entry.status


# -- the four verdicts ------------------------------------------------------------


def test_an_unchanged_file_keeps_the_evidence_valid() -> None:
    ledger, first = _ledger_with_one()
    changed = ledger.revalidate(state_index((_state(),)))
    assert changed == ()
    assert _status(ledger, first) is EvidenceStatus.VALID


def test_a_changed_content_hash_makes_the_evidence_stale() -> None:
    ledger, first = _ledger_with_one()
    changed = ledger.revalidate(state_index((_state(file_hash="filehash-2"),)))
    assert [(t.evidence_id, t.previous, t.current) for t in changed] == [
        (first, EvidenceStatus.VALID, EvidenceStatus.STALE)
    ]


def test_identical_bytes_with_a_new_facts_version_are_still_stale() -> None:
    """A parser fix changes the facts, so the content hash cannot answer this.

    T0: bytes H, facts version V1, evidence allocated.
    T1: the same bytes H, facts version V2. The evidence is stale.
    """
    ledger, first = _ledger_with_one()
    ledger.revalidate(state_index((_state(facts="schema:5||py:4||deps:2||chunks:2"),)))
    assert _status(ledger, first) is EvidenceStatus.STALE


def test_a_file_no_longer_indexed_makes_the_evidence_invalid() -> None:
    ledger, first = _ledger_with_one()
    ledger.revalidate(state_index((_state(exists=False),)))
    assert _status(ledger, first) is EvidenceStatus.INVALID


def test_a_file_the_caller_said_nothing_about_is_unverified() -> None:
    """The absence of a verdict, which is not the same as a verdict of absence."""
    ledger, first = _ledger_with_one()
    ledger.revalidate({})
    assert _status(ledger, first) is EvidenceStatus.UNVERIFIED


# -- identity, not resemblance ------------------------------------------------------


def test_a_repository_that_no_longer_holds_that_slug_invalidates_its_evidence() -> None:
    ledger, first = _ledger_with_one()
    # The same path, under a different identity. Not a match.
    ledger.revalidate(state_index((_state(slug="team/renamed-service"),)))
    assert _status(ledger, first) is EvidenceStatus.UNVERIFIED


def test_a_repository_moving_within_the_root_and_taking_a_new_slug_invalidates() -> None:
    """A new slug is a new identity. Evidence is not transferable to it."""
    ledger, first = _ledger_with_one()
    ledger.revalidate(
        state_index(
            (
                _state(exists=False),
                _state(slug="platform/service"),
            )
        )
    )
    assert _status(ledger, first) is EvidenceStatus.INVALID


def test_a_root_relocation_that_preserves_the_slug_changes_nothing() -> None:
    """The slug rather than the absolute path is the identity, so a move is invisible."""
    ledger, first = _ledger_with_one()
    ledger.revalidate(state_index((_state(),)))
    assert _status(ledger, first) is EvidenceStatus.VALID


def test_no_same_named_replacement_is_ever_searched_for() -> None:
    """A different repository with a same-named file is a different file."""
    ledger, first = _ledger_with_one()
    ledger.revalidate(
        state_index(
            (
                _state(exists=False),
                _state(slug="other-team/service", file_hash=FILE_HASH, facts=FACTS),
            )
        )
    )
    assert _status(ledger, first) is EvidenceStatus.INVALID


# -- stickiness and recovery ----------------------------------------------------------


def test_superseded_evidence_stays_superseded_however_the_index_moves() -> None:
    """The one status revalidation may not overturn."""
    ledger = EvidenceLedger("investigation-under-test")
    (old,) = ledger.record([_proposed(digest="A")])
    (new,) = ledger.record([_proposed(digest="B")])
    assert _status(ledger, old.id) is EvidenceStatus.SUPERSEDED

    ledger.revalidate(state_index((_state(),)))
    assert _status(ledger, old.id) is EvidenceStatus.SUPERSEDED
    assert _status(ledger, new.id) is EvidenceStatus.VALID

    # Even after the file changes and changes back.
    ledger.revalidate(state_index((_state(file_hash="other"),)))
    ledger.revalidate(state_index((_state(),)))
    assert _status(ledger, old.id) is EvidenceStatus.SUPERSEDED


def test_a_superseded_entry_never_appears_in_the_reported_transitions() -> None:
    ledger = EvidenceLedger("investigation-under-test")
    (old,) = ledger.record([_proposed(digest="A")])
    ledger.record([_proposed(digest="B")])
    changed = ledger.revalidate(state_index((_state(file_hash="moved"),)))
    assert old.id not in {transition.evidence_id for transition in changed}


def test_stale_evidence_becomes_valid_again_when_the_exact_snapshot_returns() -> None:
    """Pinned deliberately: status describes correspondence, not history.

    A file edited and then restored byte for byte, on the same facts version, once
    again matches what the evidence was drawn from. Refusing to say so would report
    a repository as permanently changed because it was briefly saved.
    """
    ledger, first = _ledger_with_one()
    ledger.revalidate(state_index((_state(file_hash="edited"),)))
    assert _status(ledger, first) is EvidenceStatus.STALE

    changed = ledger.revalidate(state_index((_state(),)))
    assert _status(ledger, first) is EvidenceStatus.VALID
    assert [(t.previous, t.current) for t in changed] == [
        (EvidenceStatus.STALE, EvidenceStatus.VALID)
    ]


def test_invalid_evidence_becomes_valid_again_when_the_file_returns_unchanged() -> None:
    ledger, first = _ledger_with_one()
    ledger.revalidate(state_index((_state(exists=False),)))
    assert _status(ledger, first) is EvidenceStatus.INVALID
    ledger.revalidate(state_index((_state(),)))
    assert _status(ledger, first) is EvidenceStatus.VALID


def test_unverified_evidence_resolves_once_the_caller_supplies_the_state() -> None:
    ledger, first = _ledger_with_one()
    ledger.revalidate({})
    assert _status(ledger, first) is EvidenceStatus.UNVERIFIED
    ledger.revalidate(state_index((_state(),)))
    assert _status(ledger, first) is EvidenceStatus.VALID


# -- what revalidation reports ---------------------------------------------------------


def test_only_the_current_operations_changes_are_reported() -> None:
    """``changed`` is what just moved, not a history the caller has already seen."""
    ledger, first = _ledger_with_one()
    first_pass = ledger.revalidate(state_index((_state(file_hash="edited"),)))
    assert len(first_pass) == 1

    second_pass = ledger.revalidate(state_index((_state(file_hash="edited"),)))
    assert second_pass == ()
    assert _status(ledger, first) is EvidenceStatus.STALE


def test_the_summary_counts_every_status_after_revalidation() -> None:
    ledger = EvidenceLedger("investigation-under-test")
    ledger.record([_proposed(start=1, end=1, digest="a")])
    ledger.record([_proposed(start=2, end=2, digest="b", path="src/other.py")])
    ledger.record([_proposed(start=1, end=1, digest="a2")])

    changed = ledger.revalidate(
        state_index((_state(file_hash="edited"), _state(path="src/other.py")))
    )
    summary = ledger.summary(changed)
    assert summary.total == 3
    assert summary.superseded == 1
    assert summary.stale == 1
    assert summary.valid == 1
    assert summary.changed == tuple(changed)


def test_the_reference_survives_every_status_change() -> None:
    """What E1 pointed at is answerable forever, whatever the index does next."""
    ledger, first = _ledger_with_one()
    ledger.revalidate(state_index((_state(exists=False),)))

    entry = ledger.get(first)
    assert entry is not None
    assert entry.status is EvidenceStatus.INVALID
    assert entry.ref.repository_slug == SLUG
    assert entry.ref.path == PATH
    assert (entry.ref.line_start, entry.ref.line_end) == (10, 14)
    assert entry.ref.span_digest == "digest-a"
    assert entry.ref.file_content_hash == FILE_HASH
    assert entry.ref.indexed_facts_version == FACTS


# -- the grounding projection ------------------------------------------------------------


def test_the_grounding_projection_excludes_everything_but_valid_by_default() -> None:
    ledger = EvidenceLedger("investigation-under-test")
    ledger.record([_proposed(start=1, end=1, digest="a")])
    ledger.record([_proposed(start=2, end=2, digest="b")])
    ledger.record([_proposed(start=1, end=1, digest="a2")])
    ledger.revalidate(state_index((_state(),)))

    groundable = ledger.entries_for_grounding()
    assert {entry.status for entry in groundable} == {EvidenceStatus.VALID}
    assert [entry.id for entry in groundable] == ["E2", "E3"]


def test_the_grounding_projection_is_deterministic_and_widenable() -> None:
    ledger = EvidenceLedger("investigation-under-test")
    ledger.record([_proposed(start=1, end=1, digest="a")])
    ledger.record([_proposed(start=1, end=1, digest="b")])
    everything = ledger.entries_for_grounding((EvidenceStatus.VALID, EvidenceStatus.SUPERSEDED))
    assert [entry.id for entry in everything] == ["E1", "E2"]
    assert ledger.entries_for_grounding(()) == ()


def test_ids_are_never_renumbered_by_revalidation_or_summary() -> None:
    """An id, once issued, names the same evidence for the life of the investigation.

    Renumbering would be the quietest possible failure: every previously returned
    id keeps resolving, and starts resolving to something else.
    """
    ledger = EvidenceLedger("investigation-under-test")
    ledger.record([_proposed(start=1, end=1, digest="a")])
    ledger.record([_proposed(start=2, end=2, digest="b")])
    ledger.record([_proposed(start=1, end=1, digest="a2")])

    before = {entry.id: entry.ref for entry in ledger.entries()}
    assert list(before) == ["E1", "E2", "E3"]

    ledger.revalidate(state_index((_state(file_hash="edited"),)))
    ledger.summary()
    ledger.revalidate(state_index((_state(),)))

    after = {entry.id: entry.ref for entry in ledger.entries()}
    assert after == before
