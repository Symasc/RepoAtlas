"""The evidence ledger: what an investigation was shown, and whether it still holds.

Three rules carry this module, and each exists because the alternative fails
quietly rather than loudly.

**An id is bound to bytes and never rebound.** If ``E4`` named lines 10-14 of a
file, no later operation makes ``E4`` mean different text at those lines. Supplying
different bytes for that span allocates a new id and marks the old one superseded.
Rebinding would be worse than losing the evidence: every claim already citing
``E4`` would silently start pointing at content the caller never saw.

Sameness is decided by the bytes and where they are, and by nothing else: the same
bytes at the same coordinates are one evidence identity whichever route, or
whichever declared entity, supplied them. Whether *different* bytes replace what is
there is decided by identity, which for a recorded declaration includes the entity
it declares. Its text was assembled from facts about the source rather than read
from it, so it claims its file and no lines — and every such claim in one file
shares a span. ``class ServiceConfig`` and ``def load_config()`` are not two
versions of one file's bytes, and keying replacement on the span alone made
whichever arrived second supersede the other, in the same response that issued
both. A new declaration of the *same* entity still supersedes the old one, and text
read from the file is keyed exactly as it always was.

**Nothing is forgotten silently.** At capacity the ledger refuses new identities
rather than evicting old ones. An evicted id does not become an error a caller can
handle, it becomes a citation that no longer resolves — indistinguishable, to
verification, from a fabrication.

**A batch is one operation.** Recording a response either happens completely or
not at all, metadata strengthening included. A half-recorded response hands a
caller some ids the ledger knows about and some it does not, which is exactly the
state verification cannot tell apart from an invented citation.

The ledger holds no source text and no excerpt. It can always say what ``E12``
referred to and always prove whether the index still matches it; it can never hand
back the bytes, because they were deliberately never stored.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from repoatlas.models.evidence import EvidenceSource, EvidenceTier, MatchClass
from repoatlas.models.investigation import (
    DEFAULT_LEDGER_CAPACITY,
    EvidenceRef,
    EvidenceStatus,
    EvidenceTransition,
    IndexedEvidenceState,
    LedgerEntry,
    LedgerSummary,
    ProposedEvidence,
    format_evidence_id,
)

#: Statuses a grounding projection includes unless the caller says otherwise.
#: Stale evidence is excluded by default: it described the index accurately once,
#: and a citation to it now asserts something about content that has moved.
GROUNDABLE_STATUSES: tuple[EvidenceStatus, ...] = (EvidenceStatus.VALID,)

#: The span, then the entity a declaration declares (``None`` for text read from
#: the file). Scopes supersession; see :func:`evidence_identity`.
EvidenceIdentity = tuple[str, str, int | None, int | None, str | None]


def evidence_identity(ref: EvidenceRef, declares: str | None) -> EvidenceIdentity:
    """What different bytes at one span would *replace*, as the ledger decides it.

    Not what makes two supplies the same evidence — identical bytes at identical
    coordinates are that, whatever this says. For text read from the file this is
    ``ref.span_key`` and nothing more, so every rule about source spans is
    unchanged. A declaration adds the entity it declares (see
    :attr:`~repoatlas.models.investigation.ProposedEvidence.declares`), so a
    different declaration in the same file sits beside it rather than replacing it.
    """
    return (*ref.span_key, declares)


class LedgerCapacityError(Exception):
    """The batch needed more new identities than the investigation has left.

    A refusal, raised **before** anything is recorded. The alternatives were both
    considered and both lie: evicting old entries breaks citations that were valid
    when they were made, and truncating the batch returns evidence the caller can
    see and verification cannot.

    Carries the numbers rather than prose so the layer that maps it to a wire
    error has something structured to send, and so the remedy — a new
    investigation — can be stated with the actual figures.
    """

    def __init__(self, *, required: int, available: int, capacity: int) -> None:
        super().__init__(
            f"evidence ledger is full: {required} new entries needed, "
            f"{available} available of {capacity}"
        )
        self.required = required
        self.available = available
        self.capacity = capacity


@dataclass(slots=True)
class _Plan:
    """A complete decision about a batch, computed before anything is mutated.

    The atomicity strategy, and the reason it is a data structure rather than a
    sequence of guarded writes: planning reads the ledger and writes nothing, so a
    refusal is simply a plan that is never applied. There is no half-applied state
    to detect and no repair path to get wrong.
    """

    #: New entries in allocation order.
    allocations: list[LedgerEntry] = field(default_factory=list)
    #: Existing ids whose metadata strengthened, as complete replacement entries.
    merges: dict[str, LedgerEntry] = field(default_factory=dict)
    #: Ids this batch supersedes, mapped to their immediate successor. An id
    #: allocated *by this same batch* can appear here, when two proposals name one
    #: span with different bytes.
    supersessions: dict[str, str] = field(default_factory=dict)
    #: The id each proposed item resolved to, in input order.
    resolved: list[str] = field(default_factory=list)
    #: The complete active-span map as it will stand afterwards. Carried whole
    #: rather than patched during application: a batch that supersedes an entry it
    #: also allocated would otherwise have its own new entry removed by the
    #: supersession that followed it.
    active_after: dict[EvidenceIdentity, str] = field(default_factory=dict)
    #: The next free allocation number afterwards.
    next_number: int = 1

    @property
    def required(self) -> int:
        return len(self.allocations)


class EvidenceLedger:
    """Evidence identity for one investigation. In memory, for one process.

    Never persisted. A restarted process has no ledger, and an id from before the
    restart is unknown rather than silently re-minted — which is the truthful
    answer, since nothing recorded what it meant.
    """

    def __init__(
        self,
        investigation_id: str,
        *,
        capacity: int = DEFAULT_LEDGER_CAPACITY,
    ) -> None:
        if capacity < 0:
            raise ValueError(f"ledger capacity cannot be negative: {capacity}")
        self._investigation_id = investigation_id
        self._capacity = capacity
        #: Allocation-ordered. Python dicts preserve insertion order, which is
        #: what makes every projection deterministic without a sort.
        self._entries: dict[str, LedgerEntry] = {}
        #: The **currently active** id for each location (:func:`evidence_identity`).
        #: A superseded id leaves this map, which is what stops a resurrected digest
        #: reusing a dead identity.
        self._active_by_identity: dict[EvidenceIdentity, str] = {}
        self._next_number = 1

    # -- reading ------------------------------------------------------------

    @property
    def investigation_id(self) -> str:
        return self._investigation_id

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def available(self) -> int:
        """New identities that could still be allocated."""
        return max(0, self._capacity - len(self._entries))

    @property
    def full(self) -> bool:
        return self.available == 0

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, evidence_id: str) -> LedgerEntry | None:
        """One entry by id, or ``None``.

        There is no lookup that does not go through a ledger, and therefore none
        that does not go through an investigation. An id is meaningless without
        the investigation that issued it, so an API that accepted one alone would
        be offering to answer a question nobody can ask correctly.
        """
        return self._entries.get(evidence_id)

    def entries(self) -> tuple[LedgerEntry, ...]:
        """Every entry, superseded ones included, in allocation order."""
        return tuple(self._entries.values())

    def entries_for_grounding(
        self, statuses: Collection[EvidenceStatus] = GROUNDABLE_STATUSES
    ) -> tuple[LedgerEntry, ...]:
        """The entries a grounding check may treat as supplied, in allocation order.

        A pure projection. It selects and orders; it does not verify anything, and
        it deliberately knows nothing about how an evidence set is built — that
        belongs with the capability that performs the check.
        """
        wanted = set(statuses)
        return tuple(entry for entry in self._entries.values() if entry.status in wanted)

    def summary(self, changed: Sequence[EvidenceTransition] = ()) -> LedgerSummary:
        """Counts and headroom. ``changed`` describes the operation just performed."""
        counts = dict.fromkeys(EvidenceStatus, 0)
        for entry in self._entries.values():
            counts[entry.status] += 1
        return LedgerSummary(
            investigation_id=self._investigation_id,
            total=len(self._entries),
            valid=counts[EvidenceStatus.VALID],
            stale=counts[EvidenceStatus.STALE],
            superseded=counts[EvidenceStatus.SUPERSEDED],
            invalid=counts[EvidenceStatus.INVALID],
            unverified=counts[EvidenceStatus.UNVERIFIED],
            full=self.full,
            capacity=self._capacity,
            available=self.available,
            changed=tuple(changed),
        )

    # -- recording ----------------------------------------------------------

    def record(
        self, proposed: Sequence[ProposedEvidence], *, now: datetime | None = None
    ) -> tuple[LedgerEntry, ...]:
        """Record a batch of supplied evidence atomically.

        Returns one entry per proposed item, in the order supplied, so a caller can
        attach ids to the evidence it is about to hand over without re-matching.

        Either every item is recorded or none is. A batch that needs more new
        identities than remain raises :class:`LedgerCapacityError` having changed
        nothing at all — not a partial insert, and not a metadata merge either.

        A batch of evidence the ledger already holds succeeds at full capacity,
        because re-reading evidence allocates nothing and creates no new citable
        surface.
        """
        moment = now or datetime.now(UTC)
        plan = self._plan(proposed, moment)

        if plan.required > self.available:
            raise LedgerCapacityError(
                required=plan.required,
                available=self.available,
                capacity=self._capacity,
            )

        self._apply(plan)
        return tuple(self._entries[evidence_id] for evidence_id in plan.resolved)

    def _plan(self, proposed: Sequence[ProposedEvidence], now: datetime) -> _Plan:
        """Decide the whole batch against a *simulated* ledger. Mutates nothing.

        The simulation matters for a batch that touches one span twice: the second
        item has to see what the first would have done, or two proposals for the
        same span would both allocate.
        """
        plan = _Plan()
        # Shadow state: the real maps plus whatever this batch would do to them.
        active: dict[EvidenceIdentity, str] = dict(self._active_by_identity)
        pending: dict[str, LedgerEntry] = {}
        number = self._next_number
        # Every live id by where it is and the bytes it binds. At most one live id
        # ever holds a given pair: a new one is allocated only when none does, and
        # a successor always binds different bytes. A superseded id is not live, so
        # it is not here, and re-supplying its bytes cannot resurrect it.
        live: dict[tuple[tuple[str, str, int | None, int | None], str], str] = {}
        for evidence_id in active.values():
            entry = self._lookup(evidence_id, pending)
            if entry is not None:
                live[(entry.ref.span_key, entry.ref.span_digest)] = evidence_id

        for item in proposed:
            same_id = live.get((item.ref.span_key, item.ref.span_digest))
            same = self._lookup(same_id, pending) if same_id else None
            if same is not None:
                # The same bytes at the same coordinates. One evidence identity,
                # discovered again — possibly by a stronger route, and whichever
                # entity, if any, the bytes were supplied as the declaration of.
                merged = _strengthen(same, item)
                if merged != same:
                    plan.merges[same.id] = merged
                    pending[same.id] = merged
                plan.resolved.append(same.id)
                continue

            # Different bytes. Whether they replace what is there is a question
            # about identity: the same span, and for a declaration the same entity.
            identity = evidence_identity(item.ref, item.declares)
            current_id = active.get(identity)
            current = self._lookup(current_id, pending) if current_id else None

            evidence_id = format_evidence_id(number)
            number += 1
            entry = LedgerEntry(
                id=evidence_id,
                ref=item.ref,
                repository_name=item.repository_name,
                tier=item.tier,
                match=item.match,
                origins=(item.origin,),
                reasons=(item.reason,) if item.reason else (),
                # Valid by construction: the ref carries the indexed hash and facts
                # version observed at the moment the evidence was taken, so at
                # allocation the index does agree with it. Revalidation is what
                # tests that claim later, against an index that may have moved.
                status=EvidenceStatus.VALID,
                supersedes=current.id if current is not None else None,
                allocated_at=now,
                text_length=item.text_length,
                declares=item.declares,
            )
            if current is not None:
                plan.supersessions[current.id] = evidence_id
                live.pop((current.ref.span_key, current.ref.span_digest), None)
            plan.allocations.append(entry)
            pending[evidence_id] = entry
            active[identity] = evidence_id
            live[(item.ref.span_key, item.ref.span_digest)] = evidence_id
            plan.resolved.append(evidence_id)

        plan.active_after = active
        plan.next_number = number
        return plan

    def _lookup(self, evidence_id: str, pending: Mapping[str, LedgerEntry]) -> LedgerEntry | None:
        """An entry as the batch would see it: planned version first, then stored."""
        return pending.get(evidence_id) or self._entries.get(evidence_id)

    def _apply(self, plan: _Plan) -> None:
        """Commit a plan. Every check has already passed, so nothing here can fail.

        Ordered so each step sees the previous one's result. Allocations land
        first, because a batch may supersede an entry it just created. Merges
        follow, and supersession is applied last so an entry that was both
        strengthened and then replaced keeps the strengthened metadata under its
        superseded status.
        """
        for entry in plan.allocations:
            self._entries[entry.id] = entry

        for evidence_id, merged in plan.merges.items():
            self._entries[evidence_id] = merged

        for old_id, new_id in plan.supersessions.items():
            previous = self._entries[old_id]
            self._entries[old_id] = previous.model_copy(
                update={"status": EvidenceStatus.SUPERSEDED, "superseded_by": new_id}
            )

        self._active_by_identity = plan.active_after
        self._next_number = plan.next_number

    # -- revalidation -------------------------------------------------------

    def revalidate(
        self, snapshot: Mapping[tuple[str, str], IndexedEvidenceState]
    ) -> tuple[EvidenceTransition, ...]:
        """Re-decide every entry's status against the current index.

        ``snapshot`` maps ``(repository_slug, path)`` to the file's state now. The
        ledger compares values and reads nothing itself, which is what keeps every
        rule here testable without a database and stops the ledger acquiring one.

        The rules:

        ``SUPERSEDED`` is **sticky**. A superseded id is not superseded because
        the index changed; it is superseded because different bytes were later
        supplied for its span, and no amount of reindexing undoes that. Letting it
        return to ``VALID`` because the file happens to match again would make two
        live ids claim the same span.

        Every other status is recomputed from scratch, so recovery is possible in
        both directions: a file restored to the exact hash and facts version the
        evidence came from becomes ``VALID`` again. That follows from what the
        status means — whether the record still corresponds to the index — and the
        alternative would report a repository as permanently changed because it was
        briefly edited.

        An entry whose file is absent from ``snapshot`` becomes ``UNVERIFIED``.
        Nothing was established about it, and saying so is not the same as saying
        it is gone.
        """
        transitions: list[EvidenceTransition] = []
        for evidence_id, entry in self._entries.items():
            if entry.status is EvidenceStatus.SUPERSEDED:
                continue
            decided = _status_for(
                entry.ref, snapshot.get((entry.ref.repository_slug, entry.ref.path))
            )
            if decided is entry.status:
                continue
            transitions.append(
                EvidenceTransition(evidence_id=evidence_id, previous=entry.status, current=decided)
            )
            self._entries[evidence_id] = entry.model_copy(update={"status": decided})
        return tuple(transitions)


def _status_for(ref: EvidenceRef, state: IndexedEvidenceState | None) -> EvidenceStatus:
    """The status one entry's reference earns against one file's current state.

    Matched on canonical identity and relative path only. No display name, and no
    search for a same-named replacement: a repository that moved within the managed
    root and took a new slug is a different identity, and evidence attributed to
    the old one is invalid rather than transferable (ARCHITECTURE.md §3). A root
    that relocates while every slug survives changes nothing here, which is the
    whole reason the slug rather than the absolute path is the identity.
    """
    if state is None:
        return EvidenceStatus.UNVERIFIED
    if not state.exists:
        return EvidenceStatus.INVALID
    if state.content_hash != ref.file_content_hash:
        return EvidenceStatus.STALE
    if state.parser_version != ref.indexed_facts_version:
        # Identical bytes, different facts. A parser fix makes the derived facts
        # differ from the ones this evidence was drawn from, so the content hash
        # alone cannot answer this (V1_SPEC §12).
        return EvidenceStatus.STALE
    return EvidenceStatus.VALID


def _strengthen(entry: LedgerEntry, item: ProposedEvidence) -> LedgerEntry:
    """Merge a rediscovery into an entry. Metadata may only ever get stronger.

    The same span reached twice is one piece of evidence, and the order in which
    the routes found it is an accident of the query rather than a fact about the
    code. So the merge is built from operations that do not care about order:
    ``min`` over both precedence enums — lower wins in each, exactly as rank fusion
    treats them — and a sorted set union over the provenance. Running the two
    discoveries in either sequence yields byte-identical state.

    That also means a later weak result cannot weaken anything. A span proved by an
    exact path match and then also returned by an embedding neighbour is still
    proved; the embedding hit adds a route, not a doubt.
    """
    return entry.model_copy(
        update={
            "tier": EvidenceTier(min(entry.tier, item.tier)),
            "match": MatchClass(min(entry.match, item.match)),
            "origins": _sorted_origins(entry.origins, item.origin),
            "reasons": _sorted_reasons(entry.reasons, item.reason),
        }
    )


def _sorted_origins(
    existing: tuple[EvidenceSource, ...], addition: EvidenceSource
) -> tuple[EvidenceSource, ...]:
    return tuple(sorted({*existing, addition}, key=str))


def _sorted_reasons(existing: tuple[str, ...], addition: str) -> tuple[str, ...]:
    if not addition:
        return tuple(sorted(set(existing)))
    return tuple(sorted({*existing, addition}))
