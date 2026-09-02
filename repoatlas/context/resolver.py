"""The shared repository-context resolver (V1_SPEC §6, ARCHITECTURE.md §7).

Implemented once, used by every command. There are exactly two valid *implicit*
contexts: an enclosing managed repository, or the exact configured root. Anything
else is an error — RepoAtlas never guesses a scope from proximity.
"""

from __future__ import annotations

from pathlib import Path

from repoatlas.errors import ContextResolutionError, NotInitializedError
from repoatlas.gitstate.client import git_toplevel
from repoatlas.models.context import ContextScope, RepositoryContext
from repoatlas.models.repository import Repository

_UNRESOLVED_MESSAGE = "Unable to determine repository."

_UNRESOLVED_DETAIL = """You are not inside a RepoAtlas-managed repository and the current
directory is not the configured repository root.

Specify a repository:

  repoatlas {command} {example}"""


def _canonical(path: Path) -> Path:
    """Resolve symlinks, ``..``, ``~`` and trailing slashes for comparison.

    ``strict=False`` so a non-existent directory still yields a comparable path
    rather than raising.
    """
    return path.expanduser().resolve()


class RepositoryContextResolver:
    """Resolves ``(cwd, explicit_repo)`` to the repositories a command applies to."""

    def __init__(self, root: Path | None, repositories: list[Repository]) -> None:
        self._root = _canonical(root) if root is not None else None
        self._repositories = repositories
        self._by_path: dict[Path, Repository] = {
            _canonical(repo.path): repo for repo in repositories
        }

    def resolve(
        self,
        cwd: Path,
        explicit_repo: str | None = None,
        *,
        command: str = "sync",
    ) -> RepositoryContext:
        if self._root is None:
            raise NotInitializedError()

        if explicit_repo is not None:
            return self._resolve_explicit(explicit_repo, cwd)

        canonical = _canonical(cwd)

        # Step 2 — nearest managed repository wins, so a repository nested inside
        # another resolves to the inner one with no special casing.
        for candidate in (canonical, *canonical.parents):
            repo = self._by_path.get(candidate)
            if repo is not None:
                return RepositoryContext(
                    scope=ContextScope.REPOSITORY,
                    repositories=(repo,),
                    primary=repo,
                    reason=f"inside managed repository {repo.name} ({repo.path})",
                    cwd=canonical,
                )

        # Step 3 — the exact configured root, and only the exact root.
        if canonical == self._root:
            return RepositoryContext(
                scope=ContextScope.GLOBAL,
                repositories=tuple(self._repositories),
                primary=None,
                reason=f"at configured RepoAtlas root ({self._root})",
                cwd=canonical,
            )

        # Step 4 — an enclosing Git repository that RepoAtlas does not manage.
        enclosing = git_toplevel(canonical)
        if enclosing is not None:
            raise ContextResolutionError(
                "This Git repository is not managed by RepoAtlas.",
                detail=(
                    f"Repository:\n  {enclosing}\n\n"
                    f"Configured RepoAtlas root:\n  {self._root}\n\n"
                    "Run `repoatlas init` to configure another root, or explicitly\n"
                    "select a managed repository."
                ),
            )

        # Step 5 — nothing can be inferred. Ask rather than guess.
        raise ContextResolutionError(
            _UNRESOLVED_MESSAGE,
            detail=_UNRESOLVED_DETAIL.format(command=command, example=self._example_name()),
        )

    def _resolve_explicit(self, name: str, cwd: Path) -> RepositoryContext:
        """Explicit selection overrides cwd entirely and works from anywhere."""
        matches = [repo for repo in self._repositories if repo.name == name]

        if len(matches) == 1:
            repo = matches[0]
            return RepositoryContext(
                scope=ContextScope.REPOSITORY,
                repositories=(repo,),
                primary=repo,
                reason=f"explicit repository argument ({repo.name})",
                cwd=_canonical(cwd),
            )

        if len(matches) > 1:
            listing = "\n".join(f"  {repo.path}" for repo in matches)
            raise ContextResolutionError(
                f"Repository name is ambiguous: {name}",
                detail=f"Candidates:\n\n{listing}\n\nSpecify a path instead of a name.",
            )

        # Fall back to an exact canonical path match, so a path can be passed too.
        by_path = self._by_path.get(_canonical(Path(name)))
        if by_path is not None:
            return RepositoryContext(
                scope=ContextScope.REPOSITORY,
                repositories=(by_path,),
                primary=by_path,
                reason=f"explicit repository path ({by_path.path})",
                cwd=_canonical(cwd),
            )

        raise ContextResolutionError(
            f"Unknown repository: {name}",
            detail=self._known_repositories_detail(),
        )

    def _known_repositories_detail(self) -> str:
        if not self._repositories:
            return "No repositories are managed yet. Run:\n\n  repoatlas init <directory>"
        listing = "\n".join(
            f"  {repo.name}" for repo in sorted(self._repositories, key=lambda r: r.name)
        )
        return f"Known repositories:\n\n{listing}"

    def _example_name(self) -> str:
        return self._repositories[0].name if self._repositories else "<repository>"
