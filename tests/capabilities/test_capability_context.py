"""Explicit capability context (V1_SPEC §6).

One rule, asserted from several directions:

    A capability call's scope comes from the call. Never from the process.

The CLI may infer scope from where the user is standing, because that is evidence
about what they mean. A capability server is not standing anywhere useful — it was
launched in whatever directory its launcher happened to use and it outlives every
task it serves — so the same inference would answer confidently about the wrong
repository. Every test below exists because that failure is silent.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from repoatlas.capabilities.context import resolve_context, to_repository_context
from repoatlas.errors import ContextFailure, ContextResolutionError
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.context import ContextScope
from repoatlas.models.repository import Repository


def _repo(name: str, slug: str, path: str = "", repo_id: int = 1) -> Repository:
    return Repository(id=repo_id, name=name, slug=slug, path=Path(path or f"/managed/{slug}"))


#: Two repositories genuinely called ``common``. Not a contrivance: a managed root
#: holding ``teamA/common`` and ``teamB/common`` is ordinary, and it is the case
#: that makes a display name unusable as identity (ARCHITECTURE.md §3).
AMBIGUOUS_SET = [
    _repo("common", "teamA/common", repo_id=1),
    _repo("common", "teamB/common", repo_id=2),
    _repo("service", "teamA/service", repo_id=3),
]


# -- the shape of a context ---------------------------------------------------


def test_global_scope_takes_no_repository_selector() -> None:
    """A global call that also names a repository is two different requests."""
    with pytest.raises(ValidationError):
        CapabilityContext(scope=CapabilityScope.GLOBAL, repository="teamA/common")


def test_global_scope_is_constructible_with_nothing_else() -> None:
    context = CapabilityContext(scope=CapabilityScope.GLOBAL)
    assert context.repository is None


def test_repository_scope_requires_a_selector() -> None:
    """There is no working directory to fall back on, so there is no default."""
    with pytest.raises(ValidationError):
        CapabilityContext(scope=CapabilityScope.REPOSITORY)


def test_repository_scope_rejects_an_empty_selector() -> None:
    with pytest.raises(ValidationError):
        CapabilityContext(scope=CapabilityScope.REPOSITORY, repository="")


# -- resolution ---------------------------------------------------------------


def test_a_canonical_slug_resolves() -> None:
    resolution = resolve_context(CapabilityContext(repository="teamA/common"), AMBIGUOUS_SET)
    assert resolution.scope is CapabilityScope.REPOSITORY
    assert resolution.primary is not None
    assert resolution.primary.slug == "teamA/common"
    assert resolution.repositories == (AMBIGUOUS_SET[0],)


def test_an_unambiguous_display_name_resolves() -> None:
    """Names stay usable exactly where they still mean one thing."""
    resolution = resolve_context(CapabilityContext(repository="service"), AMBIGUOUS_SET)
    assert resolution.primary is not None
    assert resolution.primary.slug == "teamA/service"


def test_an_ambiguous_display_name_is_refused_with_its_candidates() -> None:
    """Refused, not resolved. Choosing would attribute evidence to a guess."""
    with pytest.raises(ContextResolutionError) as caught:
        resolve_context(CapabilityContext(repository="common"), AMBIGUOUS_SET)

    error = caught.value
    assert error.reason is ContextFailure.AMBIGUOUS
    assert error.candidates == ("teamA/common", "teamB/common")


def test_an_unknown_selector_is_refused() -> None:
    with pytest.raises(ContextResolutionError) as caught:
        resolve_context(CapabilityContext(repository="absent"), AMBIGUOUS_SET)
    assert caught.value.reason is ContextFailure.UNKNOWN_REPOSITORY


def test_global_scope_resolves_to_every_repository_and_no_primary() -> None:
    """Nothing was asked "in" a repository, so nothing may be boosted for being near one."""
    resolution = resolve_context(CapabilityContext(scope=CapabilityScope.GLOBAL), AMBIGUOUS_SET)
    assert resolution.repositories == tuple(AMBIGUOUS_SET)
    assert resolution.primary is None


def test_a_path_is_not_a_selector() -> None:
    """A path only means something against a working directory the caller does not share."""
    with pytest.raises(ContextResolutionError) as caught:
        resolve_context(CapabilityContext(repository="/managed/teamA/common"), AMBIGUOUS_SET)
    assert caught.value.reason is ContextFailure.UNKNOWN_REPOSITORY


def test_resolution_does_not_depend_on_the_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Proved by making the process directory hostile rather than by inspection."""

    def explode() -> Path:  # pragma: no cover - fails the test if ever reached
        raise AssertionError("capability context consulted the working directory")

    monkeypatch.setattr(Path, "cwd", staticmethod(explode))
    monkeypatch.chdir(tmp_path)
    resolution = resolve_context(CapabilityContext(repository="service"), AMBIGUOUS_SET)
    assert resolution.primary is not None


# -- handing off to the V1 funnel ---------------------------------------------


def test_the_adapted_context_carries_no_working_directory() -> None:
    resolution = resolve_context(CapabilityContext(repository="service"), AMBIGUOUS_SET)
    context = to_repository_context(resolution)
    assert context.cwd is None
    assert context.scope is ContextScope.REPOSITORY
    assert context.repository_ids == (3,)


def test_a_global_resolution_adapts_to_global_scope() -> None:
    resolution = resolve_context(CapabilityContext(scope=CapabilityScope.GLOBAL), AMBIGUOUS_SET)
    context = to_repository_context(resolution)
    assert context.scope is ContextScope.GLOBAL
    assert context.primary is None
    assert context.cwd is None
