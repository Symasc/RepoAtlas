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
                    reason=f"inside managed repository {repo.slug or repo.name} ({repo.path})",
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
        """Explicit selection overrides cwd entirely and works from anywhere.

        A selector is matched against two namespaces at once: repository **names**,
        which may repeat, and repository **identities** (the path within the managed
        root), which cannot. The union decides:

        * exactly one repository matched either way — resolve to it;
        * more than one — refuse and list them.

        Checking identities first and stopping there would silently answer the
        ambiguous case whenever the name collided with some *other* repository's
        identity: with ``scripts`` and ``nested/scripts`` both managed, ``--repo
        scripts`` matches one identity and two names, and picking the identity is
        exactly the arbitrary choice V1_SPEC §6 forbids.
        """
        matched: list[Repository] = []
        for repo in self._repositories:
            if repo.name == name or (repo.slug and repo.slug == name):
                matched.append(repo)

        if len(matched) == 1:
            repo = matched[0]
            selector = "identity" if repo.slug == name else "argument"
            return RepositoryContext(
                scope=ContextScope.REPOSITORY,
                repositories=(repo,),
                primary=repo,
                reason=f"explicit repository {selector} ({repo.slug or repo.name})",
                cwd=_canonical(cwd),
            )

        if len(matched) > 1:
            raise ContextResolutionError(
                f"Repository name is ambiguous: {name}",
                detail=self._ambiguity_detail(matched),
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

    @staticmethod
    def _ambiguity_detail(matches: list[Repository]) -> str:
        """List the candidates by identity. Never resolved by choosing one.

        Both spellings are offered because the identity is not always enough on its
        own: a repository sitting directly under the root has an identity equal to
        its name, so in the rare case where that name is *also* shared, only the
        path separates them.
        """
        ordered = sorted(matches, key=lambda r: (r.slug, str(r.path)))
        listing = "\n".join(f"  {repo.slug or repo.path}\n    {repo.path}" for repo in ordered)
        example = next(
            (repo.slug for repo in ordered if repo.slug and repo.slug != repo.name), None
        )
        hint = (
            f"Select one by its path within the root, for example:\n\n  --repo {example}"
            if example
            else "Select one by its absolute path."
        )
        return f"Candidates:\n\n{listing}\n\n{hint}"

    def _known_repositories_detail(self) -> str:
        if not self._repositories:
            return "No repositories are managed yet. Run:\n\n  repoatlas init <directory>"
        listing = "\n".join(
            f"  {repo.name}" for repo in sorted(self._repositories, key=lambda r: r.name)
        )
        return f"Known repositories:\n\n{listing}"

    def _example_name(self) -> str:
        return self._repositories[0].name if self._repositories else "<repository>"
