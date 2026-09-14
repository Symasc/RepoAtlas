"""Wire models for the ``sync`` capability (RepoAtlas V2).

The one V2 capability that is *meant* to change the index, and the models are
shaped around saying exactly how much it changed and nothing more.

Sync observes the local repositories as they exist and brings RepoAtlas's
deterministic index in line with them. It does not pull, fetch, check out, reset,
merge, install or execute anything. A dirty working tree is a legitimate input
and is indexed as it stands; nothing tries to clean it. Everything here is a
projection of V1's own :class:`~repoatlas.models.reports.SyncReport`, because a
second account of what a sync did would be a second interpretation of it.

Three things the types say out loud.

**A sync can be partly successful.** V1 commits each file's facts as they are
produced and marks a repository current only when the whole run's graph rebuild
commits. So a run in which git could not enumerate one repository still indexes
the others, keeps their facts and marks them current — and is not a success,
because it did not do what it was asked. :attr:`SyncStatus.INCOMPLETE` is that
outcome, reported as a result rather than raised, since the index genuinely moved.

**The mutation and the housekeeping after it are separate facts.** After the
index changes, active investigations are re-checked against it so their evidence
stops claiming validity against the snapshot that was replaced. If that re-check
fails, the sync still happened. :attr:`EvidenceRevalidationSummary.status` says
which, so a caller is never told the index is untouched when it is not.

**Identity is the slug, and nothing here is a host path.** V1's report names
repositories by display name and unreadable directories by absolute path; both
are projected before they leave. Two repositories may share a name, and an
absolute path says where the operator keeps their code while identifying nothing
anyone else can resolve.
"""

from __future__ import annotations

import enum
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from repoatlas.models.capability import CapabilityContext
from repoatlas.models.capability_io import ContextView
from repoatlas.models.freshness import FreshnessDepth, IndexFreshness
from repoatlas.models.investigation import EvidenceStatus
from repoatlas.models.parsing import Severity


class SyncRequest(BaseModel):
    """Bring the index in line with the local working trees in scope.

    ``context`` is required and is the whole of the selection. There is no
    working-directory inference here: the caller and this process do not share
    one, and "the repository you happen to be standing in" is exactly the kind of
    ambient default that turns a scoped mutation into a global one.

    ``full`` is the one option V1 exposes and safely supports: rehash every file
    rather than trusting size and modification time. It reads more; it never
    reaches further. Nothing that would touch the network, the working tree or a
    package manager is offered, and embedding-model download in particular is not,
    because fetching a model is a network operation a sync must never start.
    """

    model_config = ConfigDict(frozen=True)

    context: CapabilityContext
    full: bool = False
    #: How hard to look at the result afterwards. ``LIVE`` by default, because a
    #: caller who just mutated the index reasonably wants to know how it stands
    #: against the working tree *now* — which may already differ from what this
    #: run observed, and saying so is the point.
    depth: FreshnessDepth = FreshnessDepth.LIVE


class SyncStatus(enum.StrEnum):
    """What the mutation achieved. Never a claim about freshness afterwards."""

    #: Every repository in the run was enumerated, indexed and marked current.
    COMPLETED = "completed"
    #: The run finished and committed, but git could not establish the file set of
    #: at least one repository, so nothing was indexed from it and it is not
    #: current. The others in the same run kept their facts and are current. V1
    #: treats this as a failed command; it is reported here as a result because
    #: the index did change and the caller needs the particulars.
    INCOMPLETE = "incomplete"


class SyncDiagnostic(BaseModel):
    """One diagnostic V1 recorded while syncing a repository.

    ``message`` is written by RepoAtlas and never repeats what a parser, grammar
    loader or configuration library said. That is a property of where the text
    comes from rather than of what it happens to look like, and it has to be:
    ``configparser`` quotes the offending source line, PyYAML renders a snippet of
    it, ``SyntaxError.msg`` quotes the token it tripped on, and a grammar loader
    names the shared object it could not open. A sync response is a public machine
    surface, so a diagnostic that repeated any of those would be a second way to
    read the repository and the host underneath it.

    What remains is what a caller can act on: the category of failure, the line
    when one is deterministically known, and ``path`` --- the repository-relative
    path *this sync admitted*, never a filename recovered from an exception.
    ``path`` is ``None`` for a diagnostic about the repository rather than one of
    its files.
    """

    model_config = ConfigDict(frozen=True)

    severity: Severity
    message: str
    line: int | None = None
    #: Repository-relative, and resolved by RepoAtlas rather than quoted from a
    #: library. ``None`` when the diagnostic is about the repository itself.
    path: str | None = None


class RepositorySyncView(BaseModel):
    """What one repository's sync did, by identity rather than display name.

    Counts are V1's, unchanged. They describe the working tree as this run saw it
    against what the index held before, and they are the only counts here: nothing
    was re-scanned to produce a second opinion.
    """

    model_config = ConfigDict(frozen=True)

    #: Canonical identity. ``None`` only when the report could not be attributed to
    #: a registered repository after the run — reported rather than guessed, since
    #: guessing between two repositories that share a name is how evidence gets
    #: attributed to the wrong tree.
    repository_slug: str | None = None
    #: Display name. Not unique, and never a discriminator.
    repository_name: str
    branch: str | None = None
    #: Whether the working tree had uncommitted changes. It was indexed as it
    #: stood; nothing was stashed, reset or cleaned.
    is_dirty: bool = False
    unchanged: int = 0
    changed: int = 0
    new: int = 0
    deleted: int = 0
    files_indexed: int = 0
    symbols: int = 0
    relationships: int = 0
    dependencies: int = 0
    full_rehash: bool = False
    rehash_reason: str | None = None
    #: Git could not establish this repository's file set. Nothing was indexed from
    #: it, and it is not current. Distinct from "synced and found nothing".
    enumeration_failed: bool = False
    diagnostics: tuple[SyncDiagnostic, ...] = ()


class RevalidationStatus(enum.StrEnum):
    """Whether active investigations were re-checked after the index changed."""

    #: Every live investigation's evidence was re-decided against one coherent
    #: snapshot of the index as this call left it.
    COMPLETED = "completed"
    #: The re-check could not finish. The index change it follows is **not**
    #: undone and is not in doubt; some evidence statuses were simply not refreshed
    #: eagerly. Verification revalidates before it grounds anything, so no citation
    #: is accepted on the strength of a stale status.
    FAILED = "failed"


class EvidenceTransitionCount(BaseModel):
    """How many evidence records moved from one status to another."""

    model_config = ConfigDict(frozen=True)

    previous: EvidenceStatus
    current: EvidenceStatus
    count: int = Field(ge=1)


class EvidenceRevalidationSummary(BaseModel):
    """What the index change did to evidence already held by live investigations.

    Counts only. No investigation id, no evidence id, no location and no text: sync
    is workspace housekeeping, and the caller asking for it is not entitled to see
    what other investigations were looking at. The numbers are enough to know
    whether previously supplied evidence is still good.
    """

    model_config = ConfigDict(frozen=True)

    status: RevalidationStatus
    #: Live investigations re-checked. Excludes any that had already expired, which
    #: were discarded rather than kept alive by being looked at.
    investigations_checked: int = 0
    #: Records whose status was re-decided. Superseded records are sticky and are
    #: not re-decided, so they are counted below and not here.
    evidence_checked: int = 0
    valid: int = 0
    stale: int = 0
    superseded: int = 0
    invalid: int = 0
    unverified: int = 0
    #: Every status change the re-check made, aggregated across investigations and
    #: ordered by ``(previous, current)``.
    transitions: tuple[EvidenceTransitionCount, ...] = ()
    #: Prose for a human when the re-check failed. Never a discriminator.
    detail: str = ""


class SyncResponse(BaseModel):
    """What the sync changed, and how the index stands afterwards.

    Describes one point in time. Another process may sync this index the moment
    this call returns, and nothing here claims otherwise: the evidence counts
    describe the snapshot this call read after its own run, and a later
    verification revalidates against whatever the index holds then.
    """

    model_config = ConfigDict(frozen=True)

    context: ContextView
    status: SyncStatus
    #: Whether every file was rehashed rather than trusted by size and mtime.
    full: bool = False
    repositories: tuple[RepositorySyncView, ...] = ()
    #: Repositories this run discovered beneath the managed root and registered,
    #: by identity. Empty unless the run covered the managed root: a
    #: repository-scoped sync never registers another repository.
    repositories_added: tuple[str, ...] = ()
    #: Repositories in this run whose file set git could not establish, by identity.
    repositories_unavailable: tuple[str, ...] = ()
    #: Directories beneath the managed root that discovery could not read, relative
    #: to that root. "Could not look" is a different claim from "nothing there".
    unreadable_paths: tuple[str, ...] = ()
    files_indexed: int = 0
    symbols: int = 0
    relationships: int = 0
    dependencies: int = 0
    linked_references: int = 0
    unresolved_references: int = 0
    cross_repository_edges: int = 0
    embedded_chunks: int = 0
    pending_embeddings: int = 0
    evidence_revalidation: EvidenceRevalidationSummary
    #: How the index stands against the working trees afterwards. Not "current"
    #: merely because the sync succeeded: a file edited a moment later is already
    #: newer than what this run indexed.
    freshness: IndexFreshness
    #: When this call observed the run finish.
    completed_at: datetime
