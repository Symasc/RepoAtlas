"""Resolving an explicit capability context (V1_SPEC §6).

The rule this module exists to enforce, stated exactly:

    A capability call's scope comes from the call. Never from the process.

V1 may infer scope from the working directory, and that is right for a CLI: the
user is standing somewhere, and where they are standing is evidence about what
they mean. A capability server is not standing anywhere. It was started by a tool
launcher in whatever directory that launcher happened to use, it serves many
tasks, and it outlives all of them. Inferring scope there would answer about the
server's own directory — confidently, and about the wrong repository.

So there is no ``cwd`` parameter here, and no call to :func:`pathlib.Path.cwd`
anywhere under ``capabilities/``. That absence is asserted by a test rather than
maintained by discipline.

What is **not** re-implemented is which repository a selector names. That is
:func:`repoatlas.context.resolver.match_repositories`, shared with the CLI so the
two cannot drift into disagreeing about whether ``common`` is ambiguous.
"""

from __future__ import annotations

from collections.abc import Sequence

from repoatlas.context.resolver import ambiguity_candidates, match_repositories
from repoatlas.errors import ContextFailure, ContextResolutionError
from repoatlas.models.capability import CapabilityContext, CapabilityScope, ContextResolution
from repoatlas.models.context import ContextScope, RepositoryContext
from repoatlas.models.repository import Repository


def resolve_context(
    context: CapabilityContext, repositories: Sequence[Repository]
) -> ContextResolution:
    """Resolve an explicit context against the managed set.

    ``GLOBAL`` resolves to every managed repository and has no primary: there is
    no repository the question was asked "in", so nothing may be boosted for
    being close to the caller.

    ``REPOSITORY`` resolves its selector by canonical slug or display name:

    * exactly one match resolves;
    * more than one is :class:`ContextFailure.AMBIGUOUS`, carrying the candidate
      identities. Choosing between two repositories that share a display name is
      a guess, and a guess here silently attributes evidence to the wrong tree;
    * none is :class:`ContextFailure.UNKNOWN_REPOSITORY`.

    A path is not a selector. It only means something relative to a working
    directory, and the caller and this process do not share one.

    Raises :class:`~repoatlas.errors.ContextResolutionError`, which
    :func:`repoatlas.capabilities.errors.capability_error` turns into a wire
    envelope with the candidates attached.
    """
    if context.scope is CapabilityScope.GLOBAL:
        return ContextResolution(
            context=context,
            scope=CapabilityScope.GLOBAL,
            repositories=tuple(repositories),
            primary=None,
            reason="global scope: every managed repository",
        )

    selector = context.repository
    # The model refuses to construct a REPOSITORY context without one, so this is
    # unreachable through validation and is asserted rather than handled.
    assert selector is not None

    matched = match_repositories(selector, repositories)

    if len(matched) > 1:
        raise ContextResolutionError(
            f"Repository name is ambiguous: {selector}",
            reason=ContextFailure.AMBIGUOUS,
            candidates=ambiguity_candidates(matched),
            detail=_ambiguity_detail(matched),
        )

    if not matched:
        raise ContextResolutionError(
            f"Unknown repository: {selector}",
            reason=ContextFailure.UNKNOWN_REPOSITORY,
            detail=_known_repositories_detail(repositories),
        )

    repo = matched[0]
    selected_by = "identity" if repo.slug == selector else "name"
    return ContextResolution(
        context=context,
        scope=CapabilityScope.REPOSITORY,
        repositories=(repo,),
        primary=repo,
        reason=f"explicit repository {selected_by} ({repo.slug or repo.name})",
    )


def to_repository_context(resolution: ContextResolution) -> RepositoryContext:
    """Adapt a resolved capability context to what the V1 funnel consumes.

    ``cwd`` is ``None`` and always will be. Retrieval reads ``scope``,
    ``repositories`` and ``primary``; the working directory was only ever an
    input to resolution, and resolution has already happened.
    """
    return RepositoryContext(
        scope=(
            ContextScope.GLOBAL
            if resolution.scope is CapabilityScope.GLOBAL
            else ContextScope.REPOSITORY
        ),
        repositories=resolution.repositories,
        primary=resolution.primary,
        reason=resolution.reason,
        cwd=None,
    )


def _ambiguity_detail(matches: Sequence[Repository]) -> str:
    """List the candidates by identity. Never resolved by choosing one."""
    ordered = sorted(matches, key=lambda r: (r.slug, str(r.path)))
    listing = "\n".join(f"  {repo.slug or repo.path}" for repo in ordered)
    return f"Candidates:\n\n{listing}\n\nSelect one by its identity."


def _known_repositories_detail(repositories: Sequence[Repository]) -> str:
    if not repositories:
        return "No repositories are managed."
    listing = "\n".join(
        f"  {repo.slug or repo.name}" for repo in sorted(repositories, key=lambda r: r.slug)
    )
    return f"Known repositories:\n\n{listing}"
