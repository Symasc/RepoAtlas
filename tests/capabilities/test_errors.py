"""Mapping RepoAtlas failures to the capability envelope (V1_SPEC §4).

Three properties, each of which has a specific way of going wrong.

**Exhaustive.** A new ``RepoAtlasError`` subclass that nobody mapped would fall
through to a generic code and lose whatever made it worth having. The test walks
the hierarchy, so adding a subclass without deciding its category fails the suite.

**Structural.** Where V1 tells four context failures apart by wording, the mapper
reads a discriminator. A test rewrites every message to nonsense and asserts the
mapping is identical, which no message-parsing implementation can survive.

**Conservative.** An unrecognized failure is a bug, not a broken index. Telling an
operator to rebuild a healthy database hides the defect and costs them the index.
"""

from __future__ import annotations

from repoatlas.capabilities.errors import IndexChangedError, capability_error
from repoatlas.errors import (
    ContextFailure,
    ContextResolutionError,
    ExitCode,
    IndexCorruptError,
    IndexForeignApplicationError,
    IndexInaccessibleError,
    IndexNotRecognizedError,
    IndexSchemaError,
    IndexUnavailableError,
    MigrationContentionError,
    MigrationError,
    MigrationFailedError,
    NotInitializedError,
    ReasoningModelUnavailableError,
    RepoAtlasError,
    RepositoryRootConflictError,
    SyncInProgressError,
    UnrecognizedSchemaError,
)
from repoatlas.models.capability import CapabilityError, ErrorCategory


def _subclasses(root: type) -> set[type]:
    found = {root}
    for child in root.__subclasses__():
        found |= _subclasses(child)
    return found


def _instance(cls: type[RepoAtlasError]) -> RepoAtlasError:
    """Construct any subclass with the base signature both accept."""
    return cls("message for the test", detail="detail for the test")


# -- exhaustiveness --------------------------------------------------------------


def test_every_repoatlas_error_subclass_maps_to_a_concrete_category() -> None:
    """Adding a subclass without deciding its category fails here, not in production."""
    for cls in _subclasses(RepoAtlasError):
        mapped = capability_error(_instance(cls))
        assert isinstance(mapped, CapabilityError), cls
        assert mapped.code, cls
        assert mapped.category in set(ErrorCategory), cls


def test_the_hierarchy_covers_the_classes_this_suite_was_written_against() -> None:
    """A guard on the guard: the walk must actually be reaching the subclasses."""
    assert _subclasses(RepoAtlasError) >= {
        RepoAtlasError,
        ContextResolutionError,
        NotInitializedError,
        ReasoningModelUnavailableError,
        SyncInProgressError,
        IndexUnavailableError,
        IndexCorruptError,
        IndexInaccessibleError,
        IndexSchemaError,
        IndexNotRecognizedError,
        IndexForeignApplicationError,
        RepositoryRootConflictError,
        MigrationError,
        MigrationContentionError,
        MigrationFailedError,
        UnrecognizedSchemaError,
    }


def test_every_context_failure_reason_maps() -> None:
    for reason in ContextFailure:
        mapped = capability_error(ContextResolutionError("anything", reason=reason))
        assert mapped.code, reason
        assert mapped.context["reason"] == str(reason)


# -- structural, never textual ------------------------------------------------------


def test_the_mapping_ignores_the_message_entirely() -> None:
    """Rewrite the prose; the envelope must not move."""
    real = ContextResolutionError(
        "Repository name is ambiguous: common",
        reason=ContextFailure.AMBIGUOUS,
        candidates=("teamA/common", "teamB/common"),
    )
    nonsense = ContextResolutionError(
        "zzzz not a sentence about anything at all",
        reason=ContextFailure.AMBIGUOUS,
        candidates=("teamA/common", "teamB/common"),
    )
    assert capability_error(real).code == capability_error(nonsense).code
    assert capability_error(real).category == capability_error(nonsense).category
    assert capability_error(nonsense).context["candidates"] == ["teamA/common", "teamB/common"]


def test_an_ambiguous_selector_carries_its_candidates_structurally() -> None:
    mapped = capability_error(
        ContextResolutionError(
            "ambiguous",
            reason=ContextFailure.AMBIGUOUS,
            candidates=("teamA/common", "teamB/common"),
        )
    )
    assert mapped.code == "REPOSITORY_AMBIGUOUS"
    assert mapped.category is ErrorCategory.CONTEXT
    assert mapped.context["candidates"] == ["teamA/common", "teamB/common"]


def test_an_unknown_repository_is_not_found_rather_than_a_context_failure() -> None:
    """The scope was well formed; the thing it named is absent. Different remedies."""
    mapped = capability_error(
        ContextResolutionError("unknown", reason=ContextFailure.UNKNOWN_REPOSITORY)
    )
    assert mapped.category is ErrorCategory.NOT_FOUND


# -- categories ---------------------------------------------------------------------


def test_a_missing_index_is_an_index_failure() -> None:
    mapped = capability_error(NotInitializedError())
    assert mapped.code == "NOT_INITIALIZED"
    assert mapped.category is ErrorCategory.INDEX
    assert mapped.exit_code == ExitCode.NOT_INITIALIZED


def test_every_unusable_index_subclass_is_an_index_failure() -> None:
    for cls in (
        IndexUnavailableError,
        IndexCorruptError,
        IndexInaccessibleError,
        IndexSchemaError,
        IndexNotRecognizedError,
        IndexForeignApplicationError,
        UnrecognizedSchemaError,
    ):
        mapped = capability_error(_instance(cls))
        assert mapped.category is ErrorCategory.INDEX, cls


def test_a_failed_migration_is_internal_rather_than_a_broken_index() -> None:
    """A shipped migration that cannot run is a RepoAtlas defect, and V1 agrees: exit 1."""
    mapped = capability_error(MigrationFailedError("boom"))
    assert mapped.category is ErrorCategory.INTERNAL
    assert mapped.exit_code == ExitCode.RUNTIME


def test_an_identity_conflict_is_an_invalid_request() -> None:
    mapped = capability_error(RepositoryRootConflictError("two trees, one identity"))
    assert mapped.category is ErrorCategory.INVALID_REQUEST


# -- retryability --------------------------------------------------------------------


def test_only_genuine_contention_is_retryable() -> None:
    """Retryable means the identical call could succeed with nothing else changed.

    Three conditions qualify, and all three are one process waiting on another:
    two writers meeting, and a reader whose file was reindexed underneath it. Each
    ends when the other finishes, with nothing for the caller to change.
    """
    retryable = {
        cls for cls in _subclasses(RepoAtlasError) if capability_error(_instance(cls)).retryable
    }
    assert retryable == {SyncInProgressError, MigrationContentionError, IndexChangedError}


def test_contention_is_categorised_as_contention() -> None:
    for cls in (SyncInProgressError, MigrationContentionError, IndexChangedError):
        mapped = capability_error(_instance(cls))
        assert mapped.category is ErrorCategory.CONTENTION, cls
        assert mapped.retryable is True, cls


# -- the conservative default ----------------------------------------------------------


def test_an_unexpected_exception_is_internal_and_never_an_index_verdict() -> None:
    mapped = capability_error(ValueError("something nobody anticipated"))
    assert mapped.category is ErrorCategory.INTERNAL
    assert mapped.code == "INTERNAL_ERROR"
    assert mapped.retryable is False
    assert mapped.category is not ErrorCategory.INDEX


def test_an_exception_with_no_message_still_produces_one() -> None:
    mapped = capability_error(RuntimeError())
    assert mapped.message == "RuntimeError"


# -- vocabulary -------------------------------------------------------------------------


def test_no_model_or_provider_vocabulary_reaches_the_capability_error_model() -> None:
    """Every capability is deterministic, so no model vocabulary belongs in its errors."""
    from pathlib import Path

    package = Path(__file__).resolve().parents[2] / "repoatlas"
    banned = ("Ollama", "ollama", "ModelFailure", "model_absent", "ModelProtocolError")
    offenders: list[str] = []
    for path in (
        package / "capabilities" / "errors.py",
        package / "models" / "capability.py",
    ):
        source = path.read_text(encoding="utf-8")
        for marker in banned:
            if marker in source:
                offenders.append(f"{path.name} contains {marker!r}")
    assert not offenders, offenders


def test_an_unreachable_reasoning_model_is_never_a_capability_condition() -> None:
    """A capability that reached a model would be a bug, so it maps as one.

    Nothing in the envelope names a provider, a protocol or a model.
    """
    mapped = capability_error(ReasoningModelUnavailableError("no model", detail="none reachable"))
    assert mapped.category is ErrorCategory.INTERNAL
    serialized = mapped.model_dump_json()
    for marker in ("ollama", "Ollama", "ModelFailure"):
        assert marker not in serialized


# -- V1 is untouched -----------------------------------------------------------------------


def test_v1_rendering_and_exit_codes_are_unchanged() -> None:
    """Mapping is a translation at the boundary, not a replacement."""
    error = ContextResolutionError("Unknown repository: x", detail="Known repositories:\n\n  a")
    assert error.render() == "Unknown repository: x\n\nKnown repositories:\n\n  a"
    assert error.exit_code == ExitCode.CONTEXT

    assert NotInitializedError().exit_code == ExitCode.NOT_INITIALIZED
    assert SyncInProgressError("busy").exit_code == ExitCode.SYNC_LOCKED
    assert ReasoningModelUnavailableError("x").exit_code == ExitCode.LLM_UNAVAILABLE
    assert IndexCorruptError("x").exit_code == ExitCode.NOT_INITIALIZED
    assert MigrationFailedError("x").exit_code == ExitCode.RUNTIME
    assert UnrecognizedSchemaError("x").exit_code == ExitCode.NOT_INITIALIZED


def test_the_context_discriminator_defaults_without_changing_any_raise_site() -> None:
    """Additive: every existing construction still works and still renders the same."""
    error = ContextResolutionError("plain", detail="d")
    assert error.reason is ContextFailure.UNRESOLVED
    assert error.candidates == ()
    assert error.render() == "plain\n\nd"


def test_the_envelope_exposes_exit_code_without_making_it_a_branch_target() -> None:
    """Carried for continuity with the CLI; a caller branches on ``category``."""
    mapped = capability_error(SyncInProgressError("busy"))
    assert mapped.exit_code == ExitCode.SYNC_LOCKED
    assert mapped.category is ErrorCategory.CONTENTION
