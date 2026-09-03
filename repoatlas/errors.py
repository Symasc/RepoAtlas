"""RepoAtlas exceptions and the exit codes they map to (V1_SPEC §4)."""

from __future__ import annotations


class ExitCode:
    OK = 0
    RUNTIME = 1
    CONTEXT = 2
    NOT_INITIALIZED = 3
    LLM_UNAVAILABLE = 4
    #: Another process holds the writer lock. Distinct from RUNTIME so a wrapper
    #: script can retry contention without retrying a genuine failure.
    SYNC_LOCKED = 5


class RepoAtlasError(Exception):
    """Base error. ``exit_code`` is what the CLI returns."""

    exit_code: int = ExitCode.RUNTIME

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def render(self) -> str:
        if self.detail:
            return f"{self.message}\n\n{self.detail}"
        return self.message


class ContextResolutionError(RepoAtlasError):
    """The command's repository context could not be determined (V1_SPEC §6)."""

    exit_code = ExitCode.CONTEXT


class NotInitializedError(RepoAtlasError):
    """No RepoAtlas root is configured, or the index is unusable."""

    exit_code = ExitCode.NOT_INITIALIZED

    def __init__(
        self,
        message: str = "RepoAtlas is not initialized.",
        *,
        detail: str | None = "Run:\n\n  repoatlas init <directory>",
    ) -> None:
        super().__init__(message, detail=detail)


class ReasoningModelUnavailableError(RepoAtlasError):
    """No reasoning model is reachable; ``ask``/``debug`` cannot proceed."""

    exit_code = ExitCode.LLM_UNAVAILABLE


class SyncInProgressError(RepoAtlasError):
    """Another process holds the knowledge-base writer lock (ARCHITECTURE.md §9).

    An operational condition, not a bug: two syncs overlapped. It is raised
    rather than waited out forever so a wedged writer is reported instead of
    hanging the second one silently.
    """

    exit_code = ExitCode.SYNC_LOCKED
