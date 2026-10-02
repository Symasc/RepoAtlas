"""Investigation lifecycle and isolation.

An investigation is one task. It is not one connection and it is not one process:
a capability server serves many unrelated tasks and outlives all of them, so an
evidence guarantee scoped to the process would mean "something you were shown at
some point today", which is materially weaker than the guarantee a citation needs.

The isolation property is the sharp one. Two live investigations both hold an
``E1``, they mean different things, and neither can reach the other's. That holds
because there is no way to look up evidence without naming an investigation first
— asserted here as an API property, not just a behaviour.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest
from repoatlas.capabilities.ledger import EvidenceLedger
from repoatlas.capabilities.session import (
    DEFAULT_IDLE_TTL_SECONDS,
    DEFAULT_MAX_SESSIONS,
    SessionRegistry,
    UnknownInvestigationError,
)
from repoatlas.models.evidence import EvidenceSource, EvidenceTier, MatchClass
from repoatlas.models.investigation import EvidenceRef, ProposedEvidence

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _Clock:
    """A hand-wound clock, so expiry is tested rather than waited for."""

    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _counted_ids() -> Iterator[str]:
    number = 0
    while True:
        number += 1
        yield f"investigation-{number}"


def _proposed(digest: str, *, path: str = "src/config.py") -> ProposedEvidence:
    return ProposedEvidence(
        ref=EvidenceRef(
            repository_slug="team/service",
            path=path,
            line_start=10,
            line_end=14,
            span_digest=digest,
            file_content_hash="filehash-1",
            indexed_facts_version="facts-1",
        ),
        repository_name="service",
        tier=EvidenceTier.DETERMINISTIC,
        match=MatchClass.EXACT_CONTENT,
        origin=EvidenceSource.EXACT,
    )


# -- minting -----------------------------------------------------------------------


def test_the_server_mints_every_investigation_id() -> None:
    registry = SessionRegistry()
    first = registry.create()
    second = registry.create()
    assert first.id != second.id
    assert len(first.id) >= 16
    assert len(registry) == 2


def test_ids_are_opaque_rather_than_sequential() -> None:
    """A guessable id would make isolation rest on a caller not trying."""
    registry = SessionRegistry()
    ids = [registry.create().id for _ in range(5)]
    assert len(set(ids)) == 5
    for investigation_id in ids:
        assert not investigation_id.isdigit()
        assert "investigation" not in investigation_id


def test_there_is_no_way_to_supply_an_id_and_have_it_created() -> None:
    """A typo must not become a silently empty ledger.

    ``live_sessions`` joined the surface for sync's housekeeping (Slice I). It takes
    nothing, so it cannot be handed an id to create; it only reads what is live.
    """
    registry = SessionRegistry()
    public = {name for name in dir(registry) if not name.startswith("_")}
    assert "acquire" not in public
    assert public == {"cleanup_expired", "create", "drop", "get", "live_sessions", "max_sessions"}
    assert not inspect.signature(registry.live_sessions).parameters


def test_a_new_investigation_starts_with_an_empty_ledger() -> None:
    registry = SessionRegistry()
    session = registry.create()
    assert isinstance(session.ledger, EvidenceLedger)
    assert len(session.ledger) == 0
    assert session.ledger.investigation_id == session.id


# -- refusal ------------------------------------------------------------------------


def test_an_id_the_registry_never_issued_is_refused() -> None:
    registry = SessionRegistry()
    with pytest.raises(UnknownInvestigationError):
        registry.get("investigation-1")


def test_a_dropped_investigation_is_unknown() -> None:
    registry = SessionRegistry()
    session = registry.create()
    assert registry.drop(session.id) is True
    with pytest.raises(UnknownInvestigationError):
        registry.get(session.id)


def test_dropping_an_unknown_id_is_not_an_error() -> None:
    """The caller wanted it gone and it is gone."""
    assert SessionRegistry().drop("never-existed") is False


def test_a_fresh_registry_holds_nothing_from_a_previous_one() -> None:
    """Nothing is persisted, so a restarted process starts empty."""
    first = SessionRegistry()
    session = first.create()
    second = SessionRegistry()
    assert len(second) == 0
    with pytest.raises(UnknownInvestigationError):
        second.get(session.id)


# -- expiry -------------------------------------------------------------------------


def test_an_idle_investigation_expires_and_becomes_unknown() -> None:
    clock = _Clock()
    registry = SessionRegistry(idle_ttl_seconds=60, clock=clock)
    session = registry.create()

    clock.advance(59)
    assert registry.get(session.id).id == session.id

    clock.advance(61)
    with pytest.raises(UnknownInvestigationError):
        registry.get(session.id)


def test_using_an_investigation_defers_its_expiry() -> None:
    clock = _Clock()
    registry = SessionRegistry(idle_ttl_seconds=60, clock=clock)
    session = registry.create()

    for _ in range(5):
        clock.advance(30)
        registry.get(session.id)

    clock.advance(30)
    assert registry.get(session.id).id == session.id


def test_expiry_is_decided_on_access_rather_than_by_a_sweep() -> None:
    """So an aged-out investigation is unknown from the first call after it aged."""
    clock = _Clock()
    registry = SessionRegistry(idle_ttl_seconds=10, clock=clock)
    session = registry.create()
    clock.advance(11)
    with pytest.raises(UnknownInvestigationError):
        registry.get(session.id)
    assert len(registry) == 0


def test_cleanup_reports_what_it_removed() -> None:
    clock = _Clock()
    registry = SessionRegistry(idle_ttl_seconds=10, clock=clock)
    stale = registry.create()
    clock.advance(11)
    assert registry.cleanup_expired() == (stale.id,)
    assert len(registry) == 0


def test_creating_an_investigation_sweeps_the_expired_ones() -> None:
    """So an abandoned task cannot occupy a slot a live one needs."""
    clock = _Clock()
    registry = SessionRegistry(idle_ttl_seconds=10, clock=clock)
    stale = registry.create()
    clock.advance(11)

    fresh = registry.create()
    assert len(registry) == 1
    assert registry.get(fresh.id).id == fresh.id
    with pytest.raises(UnknownInvestigationError):
        registry.get(stale.id)


# -- bounded by count ------------------------------------------------------------------


def test_the_registry_evicts_the_least_recently_used_investigation() -> None:
    """A whole investigation, which is a different act from evicting its evidence."""
    registry = SessionRegistry(max_sessions=2)
    first = registry.create()
    second = registry.create()
    registry.get(first.id)  # first is now the most recently used

    third = registry.create()

    assert len(registry) == 2
    assert registry.get(first.id).id == first.id
    assert registry.get(third.id).id == third.id
    with pytest.raises(UnknownInvestigationError):
        registry.get(second.id)


def test_an_evicted_investigation_is_never_silently_recreated() -> None:
    registry = SessionRegistry(max_sessions=1)
    first = registry.create()
    first.ledger.record([_proposed("a")])
    registry.create()

    with pytest.raises(UnknownInvestigationError):
        registry.get(first.id)
    assert len(registry) == 1


def test_a_registry_must_hold_at_least_one_investigation() -> None:
    with pytest.raises(ValueError, match="at least one"):
        SessionRegistry(max_sessions=0)


def test_the_accepted_defaults_are_what_ship() -> None:
    assert DEFAULT_MAX_SESSIONS == 32
    assert DEFAULT_IDLE_TTL_SECONDS == 3600
    registry = SessionRegistry()
    assert registry.max_sessions == DEFAULT_MAX_SESSIONS


# -- isolation ---------------------------------------------------------------------------


def test_two_investigations_hold_unrelated_e1s() -> None:
    """The property the whole design rests on."""
    registry = SessionRegistry()
    a = registry.create()
    b = registry.create()

    (a_entry,) = a.ledger.record([_proposed("digest-from-a", path="a/only.py")])
    (b_entry,) = b.ledger.record([_proposed("digest-from-b", path="b/only.py")])

    assert a_entry.id == "E1"
    assert b_entry.id == "E1"

    from_a = a.ledger.get("E1")
    from_b = b.ledger.get("E1")
    assert from_a is not None and from_b is not None
    assert from_a.ref.path == "a/only.py"
    assert from_b.ref.path == "b/only.py"
    assert from_a.ref.span_digest != from_b.ref.span_digest


def test_a_grounding_projection_from_one_investigation_never_sees_the_other() -> None:
    registry = SessionRegistry()
    a = registry.create()
    b = registry.create()
    a.ledger.record([_proposed("digest-from-a", path="a/only.py")])
    b.ledger.record([_proposed("digest-from-b", path="b/only.py")])

    assert {entry.ref.path for entry in a.ledger.entries()} == {"a/only.py"}
    assert {entry.ref.path for entry in b.ledger.entries()} == {"b/only.py"}


def test_no_evidence_can_be_looked_up_without_naming_an_investigation() -> None:
    """Asserted as an API property: the wrong call must not be writable."""
    registry = SessionRegistry()
    for name in dir(registry):
        assert "evidence" not in name.lower(), name
    assert not hasattr(registry, "get_evidence")
    assert not hasattr(registry, "find_evidence")


def test_each_investigations_ledger_has_its_own_capacity() -> None:
    registry = SessionRegistry(ledger_capacity=1)
    a = registry.create()
    b = registry.create()
    a.ledger.record([_proposed("a")])
    assert a.ledger.full is True
    assert b.ledger.full is False


def test_creating_and_fetching_an_investigation_updates_its_clock() -> None:
    clock = _Clock()
    registry = SessionRegistry(clock=clock)
    session = registry.create()
    assert session.last_used_at == START

    clock.advance(120)
    fetched = registry.get(session.id)
    assert fetched.last_used_at == START + timedelta(seconds=120)


def test_an_injected_id_factory_is_used_and_collisions_are_survived() -> None:
    """Test factories are allowed to be simple; the registry must not deadlock on one."""
    ids = _counted_ids()
    registry = SessionRegistry(id_factory=lambda: next(ids))
    assert registry.create().id == "investigation-1"
    assert registry.create().id == "investigation-2"


def test_a_factory_that_only_ever_repeats_itself_is_refused() -> None:
    registry = SessionRegistry(id_factory=lambda: "always-the-same")
    registry.create()
    with pytest.raises(RuntimeError, match="unused id"):
        registry.create()
