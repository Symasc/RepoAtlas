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
    """No RepoAtlas root is configured, so there is nothing to query.

    The *absence* of an index. :class:`IndexUnavailableError` is the presence of
    one RepoAtlas cannot use; both exit 3, because from the shell's point of view
    the index is equally unavailable, and only the message can usefully tell them
    apart (V1_SPEC §4).
    """

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


class IndexUnavailableError(RepoAtlasError):
    """A RepoAtlas index exists at the configured path and cannot be used.

    Exit 3, the code V1_SPEC §4 gives to "index missing/unusable". The three
    subclasses exist because the *remedy* differs and only the message can carry
    it: a corrupt file has to be replaced, an unreadable one has to be made
    readable, and a schema from another build has to be matched with the build
    that wrote it. Telling an operator to rebuild an index whose permissions are
    merely wrong would destroy a healthy database for no reason.

    What this class is **not** is a catch-all. It is raised only where a storage
    failure has been positively identified (see
    :mod:`repoatlas.storage.sqlite.errors`); an unrecognized one stays an
    unexpected failure at exit 1, because a RepoAtlas bug reported as "your index
    is corrupt" costs the user their index and hides the defect.
    """

    exit_code = ExitCode.NOT_INITIALIZED


class IndexCorruptError(IndexUnavailableError):
    """SQLite says the file is not a database, or its image is malformed."""


class IndexInaccessibleError(IndexUnavailableError):
    """The index cannot be opened, read or written — a path or permission fault.

    Deliberately worded apart from corruption: the database may be perfectly
    intact and simply out of reach (V1_SPEC §4).
    """


class IndexSchemaError(IndexUnavailableError):
    """The schema is not one this build of RepoAtlas can operate on.

    A newer ``user_version`` than this build knows, or a required object the
    database no longer has. Refused rather than adapted to, because guessing at
    an unknown schema is how data gets destroyed.
    """


class IndexNotRecognizedError(IndexSchemaError):
    """The file at the index path is a valid SQLite database RepoAtlas does not own.

    Distinct from :class:`IndexCorruptError`, and the distinction is the whole
    point: the file is perfectly good SQLite, it simply belongs to something
    else. Saying "corrupt" would send an operator to repair or delete a database
    that has nothing wrong with it.

    Raised only after ownership could not be established *positively* — no
    RepoAtlas ``application_id``, and a schema that is not a state any committed
    RepoAtlas migration produces. Absence of a name collision is not evidence of
    ownership, so a foreign database is refused even when nothing in it would
    have clashed (ARCHITECTURE.md §3).
    """


class IndexForeignApplicationError(IndexNotRecognizedError):
    """The database carries another application's ``application_id``.

    The strongest form of the refusal above: the file does not merely lack
    RepoAtlas's mark, it carries someone else's. RepoAtlas never overwrites
    another application's identity.
    """


class RepositoryRootConflictError(RepoAtlasError):
    """A root change would give one identity to two different repositories.

    Raised by ``init`` when a discovered repository's slug is already held by a
    registered repository **that is still present on disk at its old path**. That
    combination is not a relocation: both directories exist, so RepoAtlas is being
    asked to decide which of two live working trees owns one identity, and it has
    no evidence with which to decide (ARCHITECTURE.md §3).

    Refusing is the whole point. Rewriting the registered row because the slugs
    match would silently merge two unrelated repositories and attribute the old
    one's indexed facts to the new one's contents; letting the write proceed is
    what produced ``UNIQUE constraint failed: repositories.slug`` as a traceback.
    A slug collision is not by itself proof of relocation.

    Nothing is written before this is raised — neither the repository rows nor the
    configured root — so the workspace is left exactly as it was found.
    """


class MigrationError(RepoAtlasError):
    """A schema migration could not be applied (ARCHITECTURE.md §3).

    Raised for the failures migration logic can recognize and describe: another
    process holding the write transaction past the busy timeout, a statement that
    failed and was rolled back, and a schema that is not a state any RepoAtlas
    migration produces. Each of those is an operational condition with a useful
    thing to say, so it is said — rather than a ``sqlite3.OperationalError``
    traceback reaching the terminal.

    The base exit code stays ``RUNTIME``, and the subclasses are why: the three
    causes are not one condition. Contention is transient and resolves itself, a
    rolled-back statement points at a defect in a shipped migration, and only an
    unrecognized schema is a property of the *database* the user has to act on.
    Mapping the whole class to exit 3 would tell someone whose second terminal is
    mid-upgrade that their index is unusable.
    """

    exit_code = ExitCode.RUNTIME


class MigrationContentionError(MigrationError):
    """Another process held SQLite's write transaction past the busy timeout.

    Transient by construction: the other process finishes and the next run
    succeeds. Not an unusable index, and not the sync writer lock either — that
    is :class:`SyncInProgressError` at exit 5 (ARCHITECTURE.md §9).
    """


class MigrationFailedError(MigrationError):
    """A migration statement failed and the whole migration rolled back.

    The database is untouched at its previous version, which is the point of the
    atomicity work in ARCHITECTURE.md §3. A shipped migration that cannot run is
    a defect in RepoAtlas rather than a state of the user's index, so it keeps
    exit 1 and does not tell the user to rebuild anything.
    """


class UnrecognizedSchemaError(MigrationError, IndexSchemaError):
    """The schema is not a state any RepoAtlas migration produces.

    Both things at once, and the inheritance says so rather than picking one: it
    is the migrator's fail-closed refusal (ARCHITECTURE.md §3) *and* the index
    condition an operator has to resolve. Exit 3 is stated explicitly because the
    MRO would otherwise take :class:`MigrationError`'s.
    """

    exit_code = ExitCode.NOT_INITIALIZED
