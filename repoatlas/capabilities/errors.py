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
