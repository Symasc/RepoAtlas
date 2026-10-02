"""Repository-context models (ARCHITECTURE.md §7, V1_SPEC §6)."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from repoatlas.compat import StrEnum
from repoatlas.models.repository import Repository


class ContextScope(StrEnum):
    """Resolved scope of a command.

    There are exactly two: one repository, or every managed repository. Anything
    else is an error, never a partial scope (V1_SPEC §6).
    """

    REPOSITORY = "repository"
    GLOBAL = "global"


class RepositoryContext(BaseModel):
    """The resolved answer to "which repositories does this command apply to?".

    Scope is data, not behavior: commands branch on it, none of them re-derive it.
    """

    model_config = ConfigDict(frozen=True)

    scope: ContextScope
    repositories: tuple[Repository, ...]
    primary: Repository | None = None
    reason: str = ""
    cwd: Path | None = None

    @property
    def repository_ids(self) -> tuple[int, ...]:
        return tuple(r.id for r in self.repositories if r.id is not None)
