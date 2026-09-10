"""Request and response models for the capability operations (RepoAtlas V2).

The wire vocabulary of the two read capabilities that exist so far. Everything
here is transport-neutral: a tool-protocol adapter, an HTTP handler and a test
see the same types, and none of them is why a field exists.

Two rules shape every model in this module.

**Natural identity only.** A repository is named by its ``slug`` — its canonical
root-relative path — and never by ``repositories.id``. A row id is allocation
order, so a clean reindex of the same tree produces different ones; a caller that
stored one would hold an identifier that silently stopped meaning anything
(ARCHITECTURE.md §3). Internal integer ids are used freely *below* these models to
query stores, and none of them reaches a response.

**Counts are index counts.** Every number here describes what RepoAtlas has
indexed, not what is on disk right now. The difference is the whole subject of
freshness, and a count that quietly mixed the two would make the freshness fields
unnecessary and wrong at the same time.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.freshness import FreshnessDepth, IndexFreshness, RepositoryFreshness

# ---------------------------------------------------------------------------
# repositories
# ---------------------------------------------------------------------------


class RepositoriesRequest(BaseModel):
    """Ask what RepoAtlas manages. Read-only.

    Lists the **registered** set, which is what has an identity, a slug and facts
    that evidence can be attributed to. A directory beneath the managed root that
    was never indexed has none of those, so it is not listed here — offering a
    name that no other capability can answer about would be worse than silence.
    Where it *is* reported is :class:`StatusRequest`, as drift, because that is
    where it is actionable.
    """

    model_config = ConfigDict(frozen=True)

    context: CapabilityContext
    #: Absolute on-disk location, off by default. A host path is the caller's
    #: machine layout rather than a fact about the code, it is useless to any
    #: process that is not on this host, and it is the field most likely to end up
    #: pasted into a shared transcript. Asked for, never volunteered.
    include_host_paths: bool = False
    #: ``STORED`` costs one indexed read per repository. ``LIVE`` adds a git
    #: invocation per repository, which is why listing does not do it by default:
    #: a caller that just wants to know what exists should not pay for a git
    #: process per repository to find out (V1_SPEC §12).
    depth: FreshnessDepth = FreshnessDepth.STORED


class RepositoryView(BaseModel):
    """One managed repository, as a caller sees it.

    Everything is either stored on the repository row or counted from indexed
    facts. Nothing here parses a file or walks a tree: a listing that scanned
    source to answer would make "what do you manage?" cost the same as a sync.
    """

    model_config = ConfigDict(frozen=True)

    #: Canonical identity: the repository's path relative to the managed root.
    #: This is what every other capability accepts as a selector.
    slug: str
    #: Display name, usually the directory basename. **Not unique** — two managed
    #: repositories may both be called ``common`` — and never identity.
    name: str
    #: Whether another registered repository shares this display name. Computed
    #: structurally from the registered set rather than guessed, so a caller knows
    #: to pass the slug before it gets refused for ambiguity rather than after.
    name_ambiguous: bool = False

    branch: str | None = None
    head_sha: str | None = None
    languages: tuple[str, ...] = ()
    #: Dependency manifests found during indexing, as repository-relative paths.
    manifests: tuple[str, ...] = ()
    lockfiles: tuple[str, ...] = ()

    #: Files RepoAtlas has indexed for this repository. Not the number of files in
    #: the working tree: excluded, unreadable and unchanged-but-unsynced files
    #: make those two numbers differ, and ``freshness`` is what describes the gap.
    indexed_files: int = 0
    #: Indexed symbols and relationships, on the same terms.
    symbols: int = 0
    relationships: int = 0

    last_indexed_at: datetime | None = None
    freshness: RepositoryFreshness
    #: Present only when the request asked for it.
    host_path: Path | None = None


class RepositoriesResponse(BaseModel):
    """The managed repositories in the requested scope."""

    model_config = ConfigDict(frozen=True)

    context: CapabilityContext
    scope: CapabilityScope
    #: Ordered by slug, so two calls against an unchanged index agree.
    repositories: tuple[RepositoryView, ...] = ()
    #: The depth every :attr:`RepositoryView.freshness` was resolved at. Echoed so
    #: a caller reading ``INDEXED`` knows HEAD was never consulted, rather than
    #: reading it as a weaker claim about a repository that was checked.
    depth: FreshnessDepth = FreshnessDepth.STORED


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


class StatusRequest(BaseModel):
    """Ask whether the index is current. Read-only.

    Detection, never reconciliation. Status says the workspace is not current and
    names why; ``sync`` is the only thing that changes anything. A status call that
    registered a newly discovered repository would make "check whether anything
    changed" a mutating operation (V1_SPEC §4).
    """

    model_config = ConfigDict(frozen=True)

    context: CapabilityContext
    #: ``LIVE`` by default, because the question status answers is whether the
    #: index matches the working tree *now*, and a stored-only answer cannot say.
    depth: FreshnessDepth = FreshnessDepth.LIVE
    #: Walk the managed root to detect repositories the index does not know about.
    #: Costs a discovery walk, which is the only part of the answer whose price
    #: grows with the tree rather than with the scope — hence a separate switch
    #: from ``depth``.
    #:
    #: **Drift is global at every scope.** The managed root is not a property of
    #: any repository in it, so a repository-scoped request that asks for drift
    #: gets the managed root's drift, not that repository's share of it. See
    #: :attr:`StatusResponse.drift_is_global`.
    include_drift: bool = True


class IndexCounts(BaseModel):
    """What the index contains for the requested scope.

    Indexed facts, counted from the index. Not a working-tree census: a file that
    exists but was never synced is in neither :attr:`indexed_files` nor any other
    number here, and that gap is exactly what :attr:`StatusResponse.freshness`
    reports.
    """

    model_config = ConfigDict(frozen=True)

    #: Repositories in scope. In ``GLOBAL`` scope this is the whole registered set.
    repositories: int = 0
    indexed_files: int = 0
    symbols: int = 0
    relationships: int = 0


class StatusResponse(BaseModel):
    """Whether the index can be trusted, and what it holds."""

    model_config = ConfigDict(frozen=True)

    context: CapabilityContext
    scope: CapabilityScope
    #: Whether a managed root is configured at all. False means RepoAtlas has
    #: nothing to be current *about*, which is a different condition from an index
    #: that is out of date.
    root_configured: bool = False
    counts: IndexCounts = IndexCounts()
    #: The typed verdict. Callers branch on :class:`~repoatlas.models.freshness.FreshnessState`
    #: and on the individual dimensions — never on
    #: :attr:`~repoatlas.models.freshness.RepositoryFreshness.detail`, which carries
    #: V1's human sentence for continuity and is deliberately less precise than the
    #: model it is rendered from.
    #:
    #: :attr:`~repoatlas.models.freshness.IndexFreshness.state` is the aggregate of
    #: the **repositories in scope** and nothing else — it is the worst of them. It
    #: does not fold in repository-set drift, which is a fact about the managed root
    #: rather than about any repository beneath it. For the combined answer, read
    #: :attr:`index_current`.
    freshness: IndexFreshness
    #: Whether RepoAtlas can stand behind this index for the requested scope.
    #:
    #: True requires **both** halves: every repository in scope is content-verified
    #: current, and — when drift was checked — the managed root holds no repository
    #: the index does not know about. Published as one field because the two halves
    #: are separately true in the case that matters: every registered repository can
    #: be perfectly current while the workspace is not, and a caller reading only the
    #: per-repository aggregate would call that index current. V1's ``status`` makes
    #: the same combination, which is why its overall verdict goes to ``stale`` on
    #: drift alone (V1_SPEC §12).
    #:
    #: Drift that was **not** requested cannot make this False — nothing looked, so
    #: nothing is claimed either way beyond the repositories that were checked.
    index_current: bool = False
    #: Most recent successful sync across the scope.
    last_sync: datetime | None = None
    #: True when :attr:`~repoatlas.models.freshness.IndexFreshness.repository_set_drifted`
    #: describes the whole managed root rather than the requested scope — which it
    #: always does when drift was requested. Stated as a field rather than left to
    #: documentation, because a repository-scoped answer carrying a global fact is
    #: precisely the thing a caller would otherwise misattribute.
    drift_is_global: bool = False
