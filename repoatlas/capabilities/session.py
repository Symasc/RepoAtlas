"""Investigations: one coherent task's worth of evidence identity.

The distinction this module exists to make:

    An investigation is one task. It is not one connection, and it is not one
    process.

A capability server started by a tool launcher serves many unrelated tasks and
outlives all of them. If an investigation were the connection, ``E1`` would mean
"something you were shown at some point today", and a grounding guarantee scoped
to that is materially weaker than one scoped to the question being answered. So a
task gets an investigation, and the caller carries its id for as long as the task
lasts.

Ids are minted here and never accepted from a caller. An unguessable server-minted
token is what makes one investigation unable to read another's evidence by
choosing a plausible id — and the registry refusing anything it did not issue is
what makes the token more than decoration.

Nothing is persisted. A restarted process has no investigations, and an id from
before the restart is unknown rather than quietly recreated as an empty one: an
empty ledger would accept the id and then reject every citation into it, which
reads to a caller exactly like its evidence having been fabricated.
"""

from __future__ import annotations

import secrets
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from repoatlas.capabilities.ledger import EvidenceLedger
from repoatlas.models.investigation import DEFAULT_LEDGER_CAPACITY

#: Concurrent investigations one process will hold. Reached by a client that
#: starts many tasks without finishing them; the oldest idle one is evicted whole.
DEFAULT_MAX_SESSIONS = 32

#: How long an investigation survives without being used. An hour is longer than
#: any single task and short enough that abandoned ones do not accumulate.
DEFAULT_IDLE_TTL_SECONDS = 3600


class UnknownInvestigationError(Exception):
    """No investigation with that id is live in this process.

    One error for four situations — never issued, dropped, expired, evicted, or
    issued by a process that has since restarted — because from the caller's side
    they are the same condition and the same remedy: start a new investigation.
    Distinguishing them would also leak whether an id was ever real, which is
    exactly what the unguessable id is meant to withhold.
    """

    def __init__(self, investigation_id: str) -> None:
        super().__init__(f"unknown investigation: {investigation_id}")
        self.investigation_id = investigation_id


def _opaque_id() -> str:
    """An unguessable, server-minted investigation id.

    Opaque rather than sequential: a guessable id would let one investigation name
    another's, and the ledger's isolation would then rest on a caller not trying.
    """
    return secrets.token_urlsafe(16)


@dataclass(slots=True)
class InvestigationSession:
    """One task's evidence identity and the clock that ages it."""

    id: str
    ledger: EvidenceLedger
    created_at: datetime
    last_used_at: datetime
    #: An optimisation hint, and **never** evidence validity.
    #:
    #: A service may stamp the index generation it last revalidated against so it
    #: can skip repeated work within one unchanged generation. It may not treat a
    #: matching generation as proof that evidence is current: a concurrent
    #: ``repoatlas sync`` in another process changes the index without touching
    #: this counter. Verification always compares real file state (V1_SPEC §12).
    validated_generation: int | None = field(default=None)

    def touch(self, now: datetime) -> None:
        """Mark the investigation as used, deferring its expiry."""
        self.last_used_at = now

    def idle_for(self, now: datetime) -> timedelta:
        return now - self.last_used_at


class SessionRegistry:
    """Every live investigation in this process, bounded by age and by count.

    There is deliberately no way to look up evidence here. Reaching an entry means
    naming an investigation first, so a global ``get_evidence("E1")`` cannot be
    written by accident — and ``E1`` genuinely has no meaning without one, since
    two investigations hold different ``E1``s at the same time.
    """

    def __init__(
        self,
        *,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        idle_ttl_seconds: float = DEFAULT_IDLE_TTL_SECONDS,
        ledger_capacity: int = DEFAULT_LEDGER_CAPACITY,
        id_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if max_sessions < 1:
            raise ValueError(f"a registry must hold at least one session: {max_sessions}")
        self._max_sessions = max_sessions
        self._idle_ttl = timedelta(seconds=idle_ttl_seconds)
        self._ledger_capacity = ledger_capacity
        self._id_factory = id_factory or _opaque_id
        self._clock = clock or (lambda: datetime.now(UTC))
        #: Least-recently-used first, which is the order eviction reads.
        self._sessions: OrderedDict[str, InvestigationSession] = OrderedDict()

    def __len__(self) -> int:
        return len(self._sessions)

    @property
    def max_sessions(self) -> int:
        return self._max_sessions

    def create(self) -> InvestigationSession:
        """Mint a new investigation. The only way one comes into existence.

        There is no counterpart that takes a caller-supplied id. A client echoing
        back an id it was given is the supported flow; a client inventing one is
        not, and offering an ``acquire(id)`` that created a session on miss would
        turn a typo into a silently empty ledger.
        """
        self.cleanup_expired()
        now = self._clock()
        investigation_id = self._unique_id()
        session = InvestigationSession(
            id=investigation_id,
            ledger=EvidenceLedger(investigation_id, capacity=self._ledger_capacity),
            created_at=now,
            last_used_at=now,
        )
        self._sessions[investigation_id] = session
        self._evict_over_capacity()
        return session

    def get(self, investigation_id: str) -> InvestigationSession:
        """The live investigation with that id, or a refusal.

        Expiry is checked on access rather than by a timer, so an investigation
        that aged out is unknown from the first call after it did — not from
        whenever a sweep next happened to run.
        """
        self.cleanup_expired()
        session = self._sessions.get(investigation_id)
        if session is None:
            raise UnknownInvestigationError(investigation_id)
        session.touch(self._clock())
        self._sessions.move_to_end(investigation_id)
        return session

    def drop(self, investigation_id: str) -> bool:
        """Discard an investigation. Returns whether one was there to discard.

        Idempotent, and dropping an unknown id is not an error: the caller wanted
        it gone and it is gone.
        """
        return self._sessions.pop(investigation_id, None) is not None

    def cleanup_expired(self) -> tuple[str, ...]:
        """Discard investigations idle past the TTL. Returns the ids removed."""
        now = self._clock()
        expired = [
            investigation_id
            for investigation_id, session in self._sessions.items()
            if session.idle_for(now) >= self._idle_ttl
        ]
        for investigation_id in expired:
            del self._sessions[investigation_id]
        return tuple(expired)

    def _evict_over_capacity(self) -> tuple[str, ...]:
        """Evict least-recently-used investigations down to the bound.

        A whole investigation at a time, which is a different act from evicting
        evidence out of a live one. Discarding an entry from a ledger breaks a
        citation the caller was told was good; discarding an investigation ends it,
        and the next use of its id says so truthfully.
        """
        evicted: list[str] = []
        while len(self._sessions) > self._max_sessions:
            investigation_id, _ = self._sessions.popitem(last=False)
            evicted.append(investigation_id)
        return tuple(evicted)

    def _unique_id(self) -> str:
        """A minted id that is not already live.

        The loop exists for injected test factories, which are allowed to be
        simple. A collision from :func:`secrets.token_urlsafe` is not a case worth
        planning for; a collision from ``lambda: "fixed"`` is.
        """
        for _ in range(64):
            candidate = self._id_factory()
            if candidate not in self._sessions:
                return candidate
        raise RuntimeError("investigation id factory did not produce an unused id")
