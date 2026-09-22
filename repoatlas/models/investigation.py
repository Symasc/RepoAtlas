"""Session-scoped evidence identity (RepoAtlas V2).

An external agent that reasons over many RepoAtlas calls needs to say "the thing
you showed me earlier" and be understood exactly. These models are that vocabulary.

The invariant everything here exists to hold:

    An evidence id is session-scoped, is never rebound to different supplied
    bytes, and is never silently forgotten while its investigation lives.

What that does **not** mean is worth stating first, because the opposite is the
easy assumption. ``E12`` is bound to the *digest and coordinates* of content that
was supplied. It is not a handle on those bytes. The ledger stores no source text,
so once the index moves on RepoAtlas can still say exactly what ``E12`` referred
to and can still prove the index no longer matches it — and cannot hand the old
text back. That is a deliberate trade: retaining evidence text would make the
ledger a second, unversioned copy of the repository, and a citation is worth more
when it is checkable than when it is quotable.

Nothing here names a transport, a client or a model vendor, and no storage row id
appears: a database id is allocation order, not identity, and would not survive
the reindex that evidence has to be checked against.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

from repoatlas.models.evidence import (
    EvidenceSource,
    EvidenceTier,
    MatchClass,
    MatchField,
    TierField,
)

#: An evidence identifier, valid **only** inside the investigation that issued it.
#:
#: ``E1``, ``E2``, ``E3`` — short enough for a model to carry through a chain of
#: reasoning without losing it, and meaningless anywhere else. Deliberately not a
#: UUID: an opaque global identifier would suggest the evidence is a durable
#: repository object, and it is not. It is a record of one supply event in one
#: task. Two investigations both holding an ``E1`` is normal and they are
#: unrelated.
EvidenceId = Annotated[str, StringConstraints(pattern=r"^E[1-9][0-9]*$")]

#: Sessions hold this many entries, superseded ones included, before refusing more.
#: Large enough that an ordinary investigation never meets it, small enough that a
#: forgotten session cannot grow without bound. Refusing is the point: evicting to
#: make room would hand out ids that later stop resolving.
DEFAULT_LEDGER_CAPACITY = 500


def format_evidence_id(number: int) -> str:
    """``1`` → ``"E1"``. Numbers start at one and only ever increase."""
    if number < 1:
        raise ValueError(f"evidence numbers start at 1, got {number}")
    return f"E{number}"


class EvidenceStatus(enum.StrEnum):
    """Whether an evidence record still corresponds to the current index.

    A statement about the *index*, never about the record. The record is immutable
    at every status: what it referred to, and the bytes it referred to, are the
    same facts after a reindex as before one. What changes is whether the index
    still agrees.
    """

    #: The indexed file is present at the same identity and path, with the same
    #: content hash and the same facts version the evidence was supplied from.
    VALID = "valid"
    #: The file is still indexed, but its content hash or its facts version has
    #: moved. The evidence described something real; it no longer describes what
    #: is there now.
    STALE = "stale"
    #: Different bytes were later supplied for the same span, so a newer id
    #: carries that span. **Sticky** — see :class:`~repoatlas.capabilities.ledger.EvidenceLedger`.
    SUPERSEDED = "superseded"
    #: No indexed file stands at that repository identity and path any more.
    INVALID = "invalid"
    #: Revalidation could not establish an answer, because nothing was supplied
    #: for this entry's file. Not a verdict: the absence of one.
    UNVERIFIED = "unverified"


class EvidenceRef(BaseModel):
    """What was supplied, where it came from, and which index it came from.

    Four things are pinned, and each closes a way a citation could otherwise drift:

    * **identity and path** say which file, by canonical slug rather than display
      name, because two managed repositories may both be called ``common``;
    * **the line range** says which part of it;
    * **``span_digest``** says which exact bytes were handed over, so re-supplying
      different text for the same lines is recognisably a different thing;
    * **``file_content_hash`` and ``indexed_facts_version``** say which snapshot of
      the index produced it, so a reindex that changes either is detectable
      without re-reading the file.

    The last one is not redundant with the content hash. A parser fix changes the
    facts derived from identical bytes, so evidence supplied before it describes
    the same file and a different set of facts (V1_SPEC §12).

    ``line_start`` and ``line_end`` are both ``None`` for file-identity evidence:
    a filename match knows nothing about any line, so it claims none (V1_SPEC §11).
    """

    model_config = ConfigDict(frozen=True)

    #: Canonical repository identity. Never the display name.
    repository_slug: str
    path: str
    line_start: int | None = None
    line_end: int | None = None
    #: Digest of the exact text supplied for this span.
    span_digest: str
    #: ``files.content_hash`` of the indexed file the evidence was taken from.
    file_content_hash: str
    #: The parser/facts version that produced the facts behind it.
    indexed_facts_version: str

    @property
    def span_key(self) -> tuple[str, str, int | None, int | None]:
        """What makes two pieces of evidence the same *location*.

        The same tuple V1 already treats as one source (identity, path, span), so
        the ledger and retrieval deduplication agree on what "the same span" is.
        Identity is decided by this **plus** the digest; see the ledger.
        """
        return (self.repository_slug, self.path, self.line_start, self.line_end)

    @property
    def location(self) -> str:
        """``slug/path``, ``slug/path:line`` or ``slug/path:start-end``."""
        if self.line_start is None:
            return f"{self.repository_slug}/{self.path}"
        if self.line_end is None or self.line_start == self.line_end:
            return f"{self.repository_slug}/{self.path}:{self.line_start}"
        return f"{self.repository_slug}/{self.path}:{self.line_start}-{self.line_end}"


class ProposedEvidence(BaseModel):
    """Evidence a capability is about to supply, before the ledger sees it.

    The retrieval metadata here is a claim about *this* discovery. The same span
    may arrive again by a stronger route, and the ledger strengthens what it holds
    rather than allocating a second identity for it — the bytes did not change, so
    the evidence did not.
    """

    model_config = ConfigDict(frozen=True)

    ref: EvidenceRef
    #: Repository display name as it stood when the evidence was supplied. For
    #: people. Recorded rather than looked up later, because a display name can be
    #: renamed and the record is of what the caller was actually shown.
    repository_name: str = ""
    tier: TierField = EvidenceTier.DETERMINISTIC
    match: MatchField = MatchClass.EXACT_CONTENT
    #: Which retrieval route found it. V1's vocabulary, reused rather than
    #: reinvented: two enums for "which stage produced this" would drift.
    origin: EvidenceSource = EvidenceSource.EXACT
    #: Why this matched, in the retriever's words. Diagnostic.
    reason: str = ""
    #: Characters supplied. A number, not the text: it cannot reconstruct content
    #: and it makes a truncated supply visible.
    text_length: int = 0
    #: The natural key of the entity whose recorded declaration this text is, or
    #: ``None`` for text read from the file. Should the key not resolve, the
    #: declared line span stands in (``lines:16-21``) rather than any invented key.
    #:
    #: A declaration has no span of its own to be identified by: it is supplied as
    #: a claim about its file, and every such claim in one file shares one
    #: ``ref.span_key``. Keyed on that alone, ``class ServiceConfig`` and
    #: ``def load_config()`` would be two versions of the same bytes and the second
    #: would supersede the first. They are two different facts, and the entity
    #: they declare is what tells them apart.
    declares: str | None = None
    #: The HTTP operations these lines were supplied for, each ``METHOD route`` —
    #: or the route alone where the method could not be read. Route *context*, not
    #: text and not identity: the same span supplied again for another route adds
    #: one, and two spans supplied for one route each record it.
    routes: tuple[str, ...] = ()


class LedgerEntry(BaseModel):
    """One evidence identity, and everything the ledger will ever say about it.

    Immutable in the ways that matter: ``id``, ``ref`` and ``allocated_at`` are
    fixed at allocation and no operation rewrites them. ``tier``, ``match``,
    ``origins`` and ``reasons`` may only ever strengthen. ``status`` tracks the
    index and is the one field that reflects the outside world.

    There is deliberately **no evidence text and no excerpt**. Nothing in this
    slice consumes one, and a field that exists "in case" is a field something
    eventually treats as the source. Grounding verifies coordinates against what
    was supplied, never text (V1_SPEC §15), so the absence costs nothing it needs.
    """

    model_config = ConfigDict(frozen=True)

    id: EvidenceId
    ref: EvidenceRef
    repository_name: str = ""
    tier: TierField = EvidenceTier.DETERMINISTIC
    match: MatchField = MatchClass.EXACT_CONTENT
    #: Every route that has found this evidence, sorted and unique. Accumulated
    #: rather than overwritten: a span reached both exactly and semantically was
    #: reached both ways, and first discovery is not more true than the second.
    origins: tuple[EvidenceSource, ...] = ()
    reasons: tuple[str, ...] = ()
    status: EvidenceStatus = EvidenceStatus.VALID
    #: The id that immediately replaced this one for its span. **Immediate**, not
    #: latest: a chain of three digests is a history, and rewriting each link to
    #: point at the newest would erase it.
    superseded_by: EvidenceId | None = None
    #: The id this one immediately replaced.
    supersedes: EvidenceId | None = None
    allocated_at: datetime
    text_length: int = 0
    #: The entity this evidence is the recorded declaration of, or ``None`` for
    #: text read from the file. Part of the identity: see
    #: :attr:`ProposedEvidence.declares`.
    declares: str | None = None
    #: Every HTTP operation this evidence has been supplied for, sorted and
    #: unique. Accumulated like :attr:`origins`, and never part of the identity:
    #: which operation a span was fetched for does not change which bytes it is.
    routes: tuple[str, ...] = ()

    @property
    def number(self) -> int:
        """The allocation number behind the id. ``E12`` → ``12``."""
        return int(self.id[1:])

    @property
    def citable(self) -> bool:
        """Whether the index still stands behind this evidence.

        The single predicate a caller should consult before treating an id as
        current. Every other status means something changed under it.
        """
        return self.status is EvidenceStatus.VALID


class EvidenceTransition(BaseModel):
    """One status change, caused by the operation that reported it.

    Scoped to the operation on purpose: a caller wants to know what just moved,
    not to re-read a history it has already seen.
    """

    model_config = ConfigDict(frozen=True)

    evidence_id: EvidenceId
    previous: EvidenceStatus
    current: EvidenceStatus


class LedgerSummary(BaseModel):
    """The state of one investigation's ledger, without any of its content."""

    model_config = ConfigDict(frozen=True)

    investigation_id: str
    total: int = 0
    valid: int = 0
    stale: int = 0
    superseded: int = 0
    invalid: int = 0
    unverified: int = 0
    #: No room for a new identity. Re-supplying evidence already held still works,
    #: because that allocates nothing.
    full: bool = False
    capacity: int = DEFAULT_LEDGER_CAPACITY
    #: Remaining new identities. Published so a caller can see the headroom before
    #: a wide request rather than discovering it by being refused.
    available: int = 0
    changed: tuple[EvidenceTransition, ...] = ()


class IndexedEvidenceState(BaseModel):
    """The current indexed state of one file, as revalidation input.

    The ledger compares values and reads nothing. Gathering these is the caller's
    job — after a sync, or before verification — which is what keeps the ledger
    free of a database handle and makes every comparison rule testable without one.
    """

    model_config = ConfigDict(frozen=True)

    repository_slug: str
    path: str
    #: Whether a file row stands at this identity and path **now**. A relocation
    #: that keeps the slug is not a change; one that alters the slug is a
    #: different file as far as evidence is concerned (ARCHITECTURE.md §3).
    exists: bool
    content_hash: str = ""
    parser_version: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.repository_slug, self.path)


def state_index(
    states: tuple[IndexedEvidenceState, ...] | list[IndexedEvidenceState],
) -> dict[tuple[str, str], IndexedEvidenceState]:
    """Index revalidation input by ``(slug, path)``, the way the ledger reads it."""
    return {state.key: state for state in states}


#: Fields a caller may pass to :class:`LedgerSummary` that are derived rather than
#: counted. Kept beside the model so the ledger and the model cannot disagree.
SUMMARY_STATUS_FIELDS: dict[EvidenceStatus, str] = {
    EvidenceStatus.VALID: "valid",
    EvidenceStatus.STALE: "stale",
    EvidenceStatus.SUPERSEDED: "superseded",
    EvidenceStatus.INVALID: "invalid",
    EvidenceStatus.UNVERIFIED: "unverified",
}
