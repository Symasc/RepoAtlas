"""Vendor-neutral models for the agent-facing capability layer.

Transport-neutral by construction. Nothing here names a protocol, a client or a
model vendor: a tool-protocol adapter, an HTTP handler and a test all see the
same types, and none of them is the reason a field exists.

Three things are established in this module and nothing else is:

* **context** — which repositories a call applies to, stated explicitly. There is
  no ambient working directory in a capability call, so there is no field for one.
* **budget** — how much evidence a caller may ask for, in characters, together
  with what the server actually granted.
* **errors** — a machine-readable envelope for a refusal, with no exception type
  and no exit-code vocabulary leaking out of V1.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from repoatlas.compat import StrEnum
from repoatlas.models.repository import Repository

# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


class CapabilityScope(StrEnum):
    """Which repositories a capability call applies to.

    The same two scopes V1 resolves to, named separately because the way they are
    *reached* is different: V1 may infer a scope from the working directory, and a
    capability call never may.
    """

    REPOSITORY = "repository"
    GLOBAL = "global"


class CapabilityContext(BaseModel):
    """An explicit statement of scope. Never inferred, never ambient.

    A capability server has no meaningful working directory: the process may have
    been started anywhere, and it outlives any one task. Resolving scope from
    ``Path.cwd()`` would therefore mean answering about whichever repository the
    server happens to sit in, which is a different repository from the one the
    caller is asking about and is indistinguishable from the right answer.

    So the caller says. ``repository`` accepts a canonical slug or a display name;
    a display name that identifies more than one repository is refused rather than
    resolved, exactly as V1 refuses it (V1_SPEC §6).

    **What it is saying is a boundary.** Because the scope is stated rather than
    guessed, every capability answers out of it and nothing else: an answer from
    outside would make the statement advisory, which is the one property that makes
    a scoped answer worth having. Entities that belong to no repository — an
    environment variable, a command, an HTTP operation — are in every scope, so a
    confined answer still names the hub that leads out of it. Reaching another
    repository is a thing the caller asks for: ``GLOBAL`` scope, or a
    ``relationships`` walk that says ``cross_repository``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: CapabilityScope = CapabilityScope.REPOSITORY
    #: Slug or display name. Required in ``REPOSITORY`` scope, forbidden in
    #: ``GLOBAL``: a global call that also names a repository is two different
    #: requests, and guessing which one was meant is not RepoAtlas's decision.
    repository: str | None = None

    @model_validator(mode="after")
    def _selector_matches_scope(self) -> CapabilityContext:
        if self.scope is CapabilityScope.GLOBAL and self.repository is not None:
            raise ValueError("global scope takes no repository selector")
        if self.scope is CapabilityScope.REPOSITORY and not self.repository:
            raise ValueError("repository scope requires a repository selector")
        return self


class ContextResolution(BaseModel):
    """What an explicit :class:`CapabilityContext` resolved to.

    ``reason`` is prose for a human reading a trace. It is not identity, and no
    caller should branch on it.
    """

    model_config = ConfigDict(frozen=True)

    context: CapabilityContext
    scope: CapabilityScope
    repositories: tuple[Repository, ...] = ()
    primary: Repository | None = None
    reason: str = ""

    @property
    def repository_ids(self) -> tuple[int, ...]:
        return tuple(r.id for r in self.repositories if r.id is not None)


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


class RetrievalBreadth(StrEnum):
    """How wide to cast, as a named profile rather than raw retrieval knobs.

    The funnel's real parameters — hop count, fan-out, seed count, the
    primary-repository bias, the similarity floor — are tuning for a ranking whose
    guarantees depend on them holding together (V1_SPEC §14). Exposing them would
    let a caller quietly disable the tier precedence that makes evidence
    trustworthy, so the public contract is three profiles instead.
    """

    FOCUSED = "focused"
    STANDARD = "standard"
    WIDE = "wide"


class EvidenceBudget(BaseModel):
    """How much evidence the caller is asking for.

    **Characters, never tokens.** A token count is a property of somebody else's
    tokenizer and RepoAtlas does not have one; a character is a property of the
    text it returns. Every field here is optional, and an omitted field means "use
    the profile's default" rather than "no limit".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Maximum number of evidence items.
    max_items: int | None = Field(default=None, gt=0)
    #: Maximum characters summed across every returned item.
    max_total_chars: int | None = Field(default=None, gt=0)
    #: Maximum characters in any single item.
    max_item_chars: int | None = Field(default=None, gt=0)
    breadth: RetrievalBreadth = RetrievalBreadth.STANDARD


class AppliedBudget(BaseModel):
    """What the server actually granted, and where it overrode the request.

    Every field is concrete: a caller reading this never has to consult the
    request to learn what happened. ``clamped`` names the fields the server
    lowered, so a request that quietly did not fit is visible rather than inferred
    from a short result.
    """

    model_config = ConfigDict(frozen=True)

    max_items: int
    max_total_chars: int
    max_item_chars: int
    breadth: RetrievalBreadth

    #: Field names the server lowered to its own ceiling, in a stable order.
    clamped: tuple[str, ...] = ()

    #: The ceiling that did the clamping, echoed so a caller can adjust its next
    #: request without a second round trip to discover the limit.
    ceiling_max_items: int = 0
    ceiling_max_total_chars: int = 0
    ceiling_max_item_chars: int = 0

    @property
    def was_clamped(self) -> bool:
        return bool(self.clamped)


# ---------------------------------------------------------------------------
# Presentation
# ---------------------------------------------------------------------------


class Presentation(StrEnum):
    """How a response is written out, never what the capability does.

    The capability runs identically either way: the same retrieval, the same
    ledger records, the same verification. ``COMPACT`` is a projection of the
    finished response that writes each repeated fact once — the facts version,
    a file's content hash, the reason every segment of one inspection shares —
    and leaves span digests in the ledger, where grounding reads them.
    """

    FULL = "full"
    COMPACT = "compact"


#: The published description of every ``presentation`` request field. One text,
#: because the four capabilities that take it make one promise about it.
PRESENTATION_DESCRIPTION = (
    "`full` (the default) returns the complete response. `compact` returns the same "
    "result with repeated metadata written once: every evidence item keeps its id, "
    "rank, location, text, tier, match, declaration flag, reason, entity key, "
    "truncation flag and status; the indexed facts version appears once per response "
    "and each source file's content hash once per file; span digests stay in the "
    "investigation's ledger, which is what grounding checks against; freshness covers "
    "only the repositories the result draws on. Null fields are omitted. Evidence ids, "
    "citations, grounding and staleness behave identically in both."
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ErrorCategory(StrEnum):
    """What kind of thing went wrong, for a caller deciding what to do next.

    Coarse on purpose. A category answers "should I fix my request, fix the
    workspace, wait, or report a bug?" and nothing finer; the specific condition
    is ``code``.
    """

    #: The requested scope could not be resolved.
    CONTEXT = "context"
    #: There is no usable index: absent, unreadable, or a schema this build
    #: cannot operate on.
    INDEX = "index"
    #: Another process holds a lock. Transient by construction.
    CONTENTION = "contention"
    #: The scope resolved but the thing asked about is not there.
    NOT_FOUND = "not_found"
    #: The request itself is malformed or self-contradictory.
    INVALID_REQUEST = "invalid_request"
    #: Something about this investigation's own state, such as an unknown
    #: identifier or an exhausted allowance.
    SESSION = "session"
    #: An unexpected failure. A bug, until shown otherwise.
    INTERNAL = "internal"


class CapabilityError(BaseModel):
    """A refusal, as data.

    A model rather than an exception: this crosses a wire, and a caller on the
    other side of it has no Python types. The layer that raises still raises —
    V1's exceptions are untouched — and this is what a refusal looks like once it
    has left the process.

    ``exit_code`` is carried for continuity with the CLI and is **informational**.
    A capability caller has no shell to return it to, and branching on it instead
    of on ``category`` would tie an agent to a terminal convention.
    """

    model_config = ConfigDict(frozen=True)

    #: Stable machine identifier for the specific condition, in SCREAMING_SNAKE.
    code: str
    category: ErrorCategory
    message: str
    detail: str | None = None
    #: Whether repeating the identical request could plausibly succeed without
    #: anything else changing. True only for genuine contention.
    retryable: bool = False
    #: V1's exit code. Informational; never a branch target for an agent.
    exit_code: int = 1
    #: Structured particulars: candidate slugs for an ambiguous selector, the
    #: selector that was not found, and so on. Never prose to be parsed.
    context: dict[str, object] = Field(default_factory=dict)
