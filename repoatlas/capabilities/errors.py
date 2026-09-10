"""Mapping RepoAtlas failures to a transport-neutral envelope (V1_SPEC §4).

V1's exceptions are untouched. Their messages, details and exit codes are a
shipped contract that scripts depend on, and nothing here changes any of them —
this is a translation at the boundary, not a replacement.

Two rules decide the whole table.

**An unrecognized failure is a bug, not an index problem.** Classifying stray
exceptions as :attr:`ErrorCategory.INDEX` would tell an operator to rebuild a
database that is fine, and would hide the defect that actually fired. Anything
not positively identified is :attr:`ErrorCategory.INTERNAL`. This mirrors the
reasoning already written into :class:`~repoatlas.errors.IndexUnavailableError`,
which refuses to be a catch-all for the same reason.

**No message is ever parsed.** Where V1 distinguishes cases by wording — the four
shapes of :class:`~repoatlas.errors.ContextResolutionError` — the discriminator is
:class:`~repoatlas.errors.ContextFailure`, set by the code that raises and already
knows which case it is in.

The capability vocabulary is also deliberately narrower than V1's. ``ask`` and
``debug`` are V1 commands with a model behind them; every capability is
deterministic, so an unreachable reasoning model is not a capability failure. No
model, provider or wire-protocol name appears anywhere in this module or in the
envelope it produces, and a test asserts the absence.
"""

from __future__ import annotations

from repoatlas.capabilities.ledger import LedgerCapacityError
from repoatlas.capabilities.session import UnknownInvestigationError
from repoatlas.errors import (
    ContextFailure,
    ContextResolutionError,
    ExitCode,
    IndexUnavailableError,
    MigrationContentionError,
    MigrationError,
    NotInitializedError,
    RepoAtlasError,
    RepositoryRootConflictError,
    SyncInProgressError,
)
from repoatlas.models.capability import CapabilityError, ErrorCategory


class ServiceClosedError(RepoAtlasError):
    """A capability was called on a service that has been closed.

    Defined here rather than in :mod:`repoatlas.errors` because it is a V2-only
    condition: V1 opens a workspace per command and exits, so it has no state in
    which a handle outlives what it points at.

    It exists so that the failure is *one deterministic thing*. Without it a
    closed service surfaces as whatever the underlying layer happens to raise —
    an :class:`AttributeError`, a driver-level complaint about operating on a
    closed database, or nothing at all if a call silently reads a stale handle.
    None of those is classifiable at the boundary, and the last is the dangerous
    one: it would answer.

    Categorized :attr:`ErrorCategory.INTERNAL` rather than
    :attr:`ErrorCategory.INVALID_REQUEST`, because nothing about the request is
    wrong. The lifecycle around it is, and that belongs to whoever is hosting the
    service rather than to whoever asked the question.
    """

    exit_code = ExitCode.RUNTIME

    #: Defaulted rather than fixed, so the class keeps the signature every
    #: :class:`RepoAtlasError` shares. ``ServiceClosedError()`` is the call site
    #: that exists; accepting the base arguments is what lets the exhaustiveness
    #: guard over the hierarchy construct this subclass like any other.
    def __init__(
        self,
        message: str = "This RepoAtlas capability service has been closed.",
        *,
        detail: str | None = "Open a new service; a closed one is never reopened.",
    ) -> None:
        super().__init__(message, detail=detail)


class IndexInconsistencyError(RepoAtlasError):
    """Retrieval returned a candidate the file table does not know about.

    A defect, not a state a caller can act on, which is why it is reported rather
    than smoothed over. Every candidate comes from a row the index holds, so a
    missing file row means the search index and the file table disagree — and the
    tempting repair is the dangerous one: recording the reference with an empty
    content hash would produce evidence that revalidates as stale forever and
    reads exactly like ordinary drift, hiding the inconsistency behind a plausible
    answer.
    """

    exit_code = ExitCode.RUNTIME

    def __init__(
        self,
        message: str = "the search index and the file table disagree",
        *,
        detail: str | None = "Re-run `repoatlas sync --full` to rebuild the index.",
    ) -> None:
        super().__init__(message, detail=detail)


class FileNotIndexedError(RepoAtlasError):
    """No indexed file stands at that repository identity and path.

    **One condition, one answer, on purpose.** A secret, a ``.repoatlasignore``
    exclusion, a file belonging to a nested repository, a source file added since
    the last sync, a path that tried to climb out of the repository, and a name
    that was never anything at all all arrive here and are indistinguishable in
    the response. That is the point rather than a simplification: V1's admission
    boundary decided these files are not RepoAtlas's to talk about, and a
    capability that answered "that one exists but is excluded" would turn a
    containment control into a filesystem-disclosure feature.

    It is also literally true in every case. Inspection reads the ``files`` table
    and nothing else, so a path with no row is a path RepoAtlas has nothing
    indexed for, whatever the reason.
    """

    exit_code = ExitCode.RUNTIME

    def __init__(
        self,
        message: str = "no indexed file stands at that path",
        *,
        detail: str | None = "Run `repoatlas sync` if the file should be indexed.",
    ) -> None:
        super().__init__(message, detail=detail)


class EntityNotFoundError(RepoAtlasError):
    """No entity holds that exact natural key.

    Exact means exact. There is no fallback to a name search, and specifically
    none for a ``sym:`` key whose definition has moved: such a key embeds the line
    it was defined at, so it stops resolving when the definition does, and
    resolving it to the nearest similarly-named symbol would silently answer about
    a different definition than the caller named.
    """

    exit_code = ExitCode.RUNTIME

    def __init__(
        self,
        message: str = "no entity holds that key",
        *,
        detail: str | None = (
            "Entity keys are exact and positional `sym:` keys move with their "
            "definition. Search for the symbol again to obtain a current key."
        ),
    ) -> None:
        super().__init__(message, detail=detail)


class EntityAmbiguousError(RepoAtlasError):
    """One key resolves to more than one entity row.

    The schema enforces key uniqueness only for interned entities, so a
    file-scoped collision is possible rather than hypothetical. Ambiguity is
    returned with its candidates instead of resolved: picking one would answer
    about a definition the caller did not choose, and it would do so with the same
    confidence as the unambiguous case.

    Classified as an invalid request rather than a context failure because the
    remedy is a more specific identifier, not a different scope.
    """

    exit_code = ExitCode.RUNTIME

    def __init__(
        self,
        message: str = "that key resolves to more than one entity",
        *,
        detail: str | None = "Name one of the candidates exactly.",
        candidates: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message, detail=detail)
        self.candidates = candidates


class EntityNotInspectableError(RepoAtlasError):
    """The entity resolved, and it has no source of its own to show.

    An environment variable, a module name, a config key, a package or a command
    is *interned*: it is a real identity that files refer to, and it lives at no
    file and no line. Inspection could pick one of the files that mention it and
    call that "the source", which is the tempting repair and the wrong one — the
    caller would receive a citation implying that file defines the thing, when in
    truth several files reference it and none of them owns it.

    So this is refused rather than approximated. The relationships of such an
    entity are a real question with a real answer; it is simply a different
    operation.
    """

    exit_code = ExitCode.RUNTIME

    def __init__(
        self,
        message: str = "that entity has no source location of its own",
        *,
        detail: str | None = (
            "Interned identities such as `env:` and `module:` are referenced by "
            "files rather than defined in one. Ask what refers to it instead."
        ),
    ) -> None:
        super().__init__(message, detail=detail)


class InvalidRelationshipRequestError(RepoAtlasError):
    """The request names something the deterministic graph policy does not traverse.

    Refused rather than answered with an empty result. An edge type outside the
    traversal policy — or a containment type without the flag that admits it —
    would return nothing, and "no relationships of that kind" and "that kind is
    not traversable" are different answers a caller acts on differently. Saying so
    also names the remedy, which an empty tuple cannot.
    """

    exit_code = ExitCode.RUNTIME

    def __init__(
        self,
        message: str = "that relationship request names an untraversable edge type",
        *,
        detail: str | None = None,
        edge_types: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message, detail=detail)
        self.edge_types = edge_types


class TargetOutOfScopeError(RepoAtlasError):
    """The target resolved, in a repository this call's context excludes.

    Not silently widened. The context is the caller's explicit statement of what
    this call is about, and answering outside it would make the scope advisory —
    which is exactly the property that makes a scoped answer worth having.
    """

    exit_code = ExitCode.RUNTIME

    def __init__(
        self,
        message: str = "that target lives outside the requested scope",
        *,
        detail: str | None = "Widen the context to global, or name its repository.",
    ) -> None:
        super().__init__(message, detail=detail)


class InvestigationRequiredError(RepoAtlasError):
    """An evidence id was named without the investigation that issued it.

    ``E7`` is session-scoped, and two live investigations hold different ``E7``s
    at the same time. Searching every investigation for one would let a caller
    read evidence from a task it never ran; creating a fresh investigation and
    looking there would find nothing while implying the id was wrong. Neither is
    an answer, so the request is refused as incomplete.
    """

    exit_code = ExitCode.RUNTIME

    def __init__(
        self,
        message: str = "an evidence id needs the investigation that issued it",
        *,
        detail: str | None = "Send the investigation_id returned with that evidence.",
    ) -> None:
        super().__init__(message, detail=detail)


class EvidenceNotFoundError(RepoAtlasError):
    """That investigation's ledger holds no such evidence id."""

    exit_code = ExitCode.RUNTIME

    def __init__(
        self,
        message: str = "that investigation holds no such evidence",
        *,
        detail: str
        | None = "Evidence ids are valid only inside the investigation that issued them.",
    ) -> None:
        super().__init__(message, detail=detail)


class IndexChangedError(RepoAtlasError):
    """The indexed file moved underneath the read, repeatedly.

    Inspection reads a file's indexed state, then its stored text, then its state
    again, and requires the two states to agree. When they do not, another process
    reindexed the file between the reads, and the text in hand belongs to one
    generation while the hash and parser version belong to another. Pairing them
    would produce evidence describing a snapshot that never existed — and it would
    revalidate cleanly, because its own hash would match the index.

    Retried a bounded number of times, then reported. Transient by construction:
    the writer finishes and the identical call succeeds.
    """

    exit_code = ExitCode.RUNTIME

    def __init__(
        self,
        message: str = "the index changed while this file was being read",
        *,
        detail: str | None = "Another process is syncing. Repeat the request.",
    ) -> None:
        super().__init__(message, detail=detail)


#: Which capability code and category each context failure becomes. A total map
#: over :class:`ContextFailure`, so a new member fails the exhaustiveness test
#: rather than silently falling through to a generic code.
_CONTEXT_CODES: dict[ContextFailure, tuple[str, ErrorCategory]] = {
    ContextFailure.AMBIGUOUS: ("REPOSITORY_AMBIGUOUS", ErrorCategory.CONTEXT),
    ContextFailure.UNKNOWN_REPOSITORY: ("REPOSITORY_NOT_FOUND", ErrorCategory.NOT_FOUND),
    ContextFailure.UNMANAGED_REPOSITORY: ("REPOSITORY_UNMANAGED", ErrorCategory.CONTEXT),
    ContextFailure.UNRESOLVED: ("CONTEXT_UNRESOLVED", ErrorCategory.CONTEXT),
}


def capability_error(exc: Exception) -> CapabilityError:
    """Translate any failure into the envelope a caller receives.

    Dispatch is on **type**, most specific first, because the hierarchy already
    encodes the distinctions: ``MigrationContentionError`` is transient while its
    siblings are not, and it is a subclass precisely so that fact travels.
    """
    if isinstance(exc, ContextResolutionError):
        return _context_error(exc)
    if isinstance(exc, ServiceClosedError):
        return _from_repoatlas(exc, code="SERVICE_CLOSED", category=ErrorCategory.INTERNAL)
    if isinstance(exc, IndexInconsistencyError):
        return _from_repoatlas(exc, code="INDEX_INCONSISTENT", category=ErrorCategory.INTERNAL)
    if isinstance(exc, FileNotIndexedError):
        return _from_repoatlas(exc, code="FILE_NOT_INDEXED", category=ErrorCategory.NOT_FOUND)
    if isinstance(exc, EntityAmbiguousError):
        return _from_repoatlas(
            exc,
            code="ENTITY_AMBIGUOUS",
            category=ErrorCategory.INVALID_REQUEST,
            # Structured, so a caller can choose by identity rather than by
            # parsing prose. The same treatment an ambiguous repository gets.
            context={"candidates": list(exc.candidates)} if exc.candidates else None,
        )
    if isinstance(exc, EntityNotInspectableError):
        return _from_repoatlas(
            exc, code="ENTITY_NOT_INSPECTABLE", category=ErrorCategory.INVALID_REQUEST
        )
    if isinstance(exc, EntityNotFoundError):
        return _from_repoatlas(exc, code="ENTITY_NOT_FOUND", category=ErrorCategory.NOT_FOUND)
    if isinstance(exc, InvalidRelationshipRequestError):
        return _from_repoatlas(
            exc,
            code="INVALID_RELATIONSHIP_REQUEST",
            category=ErrorCategory.INVALID_REQUEST,
            context={"edge_types": list(exc.edge_types)} if exc.edge_types else None,
        )
    if isinstance(exc, TargetOutOfScopeError):
        return _from_repoatlas(exc, code="TARGET_OUT_OF_SCOPE", category=ErrorCategory.CONTEXT)
    if isinstance(exc, InvestigationRequiredError):
        return _from_repoatlas(exc, code="INVESTIGATION_REQUIRED", category=ErrorCategory.SESSION)
    if isinstance(exc, EvidenceNotFoundError):
        return _from_repoatlas(exc, code="EVIDENCE_NOT_FOUND", category=ErrorCategory.SESSION)
    if isinstance(exc, IndexChangedError):
        return _from_repoatlas(
            exc,
            code="INDEX_CHANGED",
            category=ErrorCategory.CONTENTION,
            # The other writer finishes and the identical call then succeeds,
            # which is what makes this contention rather than a failure.
            retryable=True,
        )
    if isinstance(exc, NotInitializedError):
        # The absence of an index rather than a broken one, but from the caller's
        # side both mean the same thing: there is nothing here to answer with.
        return _from_repoatlas(exc, code="NOT_INITIALIZED", category=ErrorCategory.INDEX)
    if isinstance(exc, SyncInProgressError):
        return _from_repoatlas(
            exc,
            code="SYNC_IN_PROGRESS",
            category=ErrorCategory.CONTENTION,
            # The other writer finishes and the identical call then succeeds.
            # This is the only condition in the table where that is true.
            retryable=True,
        )
    if isinstance(exc, MigrationContentionError):
        return _from_repoatlas(
            exc,
            code="MIGRATION_IN_PROGRESS",
            category=ErrorCategory.CONTENTION,
            retryable=True,
        )
    if isinstance(exc, IndexUnavailableError):
        # Covers the corrupt, inaccessible, foreign and unrecognized-schema
        # subclasses, including UnrecognizedSchemaError, which is both this and a
        # MigrationError and is matched here because the index condition is what
        # a caller has to act on.
        return _from_repoatlas(exc, code="INDEX_UNAVAILABLE", category=ErrorCategory.INDEX)
    if isinstance(exc, MigrationError):
        # A shipped migration that cannot run is a defect in RepoAtlas, not a
        # state of the caller's index, and V1 keeps it at exit 1 for that reason.
        return _from_repoatlas(exc, code="MIGRATION_FAILED", category=ErrorCategory.INTERNAL)
    if isinstance(exc, RepositoryRootConflictError):
        return _from_repoatlas(
            exc, code="REPOSITORY_IDENTITY_CONFLICT", category=ErrorCategory.INVALID_REQUEST
        )
    if isinstance(exc, RepoAtlasError):
        # A RepoAtlas failure with no specific mapping. Reported as internal
        # rather than guessed at: a wrong category sends the caller to fix
        # something that is not broken.
        return _from_repoatlas(exc, code="INTERNAL_ERROR", category=ErrorCategory.INTERNAL)

    # The two session conditions. Neither derives from RepoAtlasError, because
    # neither is a V1 condition: V1 has no investigations and no ledger. They are
    # matched here rather than left to fall through, since falling through
    # reported an exhausted allowance and a mistyped id as internal defects — and
    # an agent told a bug occurred cannot do the one thing that resolves either,
    # which is to start a fresh investigation.
    if isinstance(exc, UnknownInvestigationError):
        return CapabilityError(
            code="UNKNOWN_INVESTIGATION",
            category=ErrorCategory.SESSION,
            message=str(exc),
            # Deliberately one answer for never-issued, dropped, expired and
            # evicted. Distinguishing them would leak whether an id was ever real.
            detail="Investigations do not outlive the process. Start a new one.",
            retryable=False,
            exit_code=1,
        )
    if isinstance(exc, LedgerCapacityError):
        return CapabilityError(
            code="LEDGER_FULL",
            category=ErrorCategory.SESSION,
            message=str(exc),
            detail="Start a new investigation; this one cannot hold more evidence.",
            retryable=False,
            exit_code=1,
            context={
                "required": exc.required,
                "available": exc.available,
                "capacity": exc.capacity,
            },
        )

    return CapabilityError(
        code="INTERNAL_ERROR",
        category=ErrorCategory.INTERNAL,
        message=str(exc) or exc.__class__.__name__,
        retryable=False,
        exit_code=1,
    )


def _context_error(exc: ContextResolutionError) -> CapabilityError:
    code, category = _CONTEXT_CODES[exc.reason]
    context: dict[str, object] = {"reason": str(exc.reason)}
    if exc.candidates:
        # Structured, so a caller can offer the choice back to a user or pick by
        # identity. The prose in `detail` says the same thing for a human and is
        # not the copy anything acts on.
        context["candidates"] = list(exc.candidates)
    return _from_repoatlas(exc, code=code, category=category, context=context)


def _from_repoatlas(
    exc: RepoAtlasError,
    *,
    code: str,
    category: ErrorCategory,
    retryable: bool = False,
    context: dict[str, object] | None = None,
) -> CapabilityError:
    return CapabilityError(
        code=code,
        category=category,
        message=exc.message,
        detail=exc.detail,
        retryable=retryable,
        exit_code=exc.exit_code,
        context=context or {},
    )
