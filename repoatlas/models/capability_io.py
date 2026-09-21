"""Request and response models for the capability operations (RepoAtlas V2).

The wire vocabulary the capability operations share. Everything
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

from pydantic import BaseModel, ConfigDict, Field, model_validator

from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.evidence import EvidenceTier, MatchClass, MatchField, TierField
from repoatlas.models.freshness import FreshnessDepth, IndexFreshness, RepositoryFreshness
from repoatlas.models.investigation import EvidenceId, EvidenceRef, EvidenceStatus
from repoatlas.models.parsing import EntityType

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
    #:
    #: The description is published rather than left as a source comment because
    #: this is the one field on the public surface whose *purpose* is disclosure,
    #: and a caller deciding whether to set it is exactly who needs to know that
    #: (RC audit, RCA-06). Everything RepoAtlas discloses by accident is a defect;
    #: this is the one thing it discloses on request, and the two must not be
    #: confused for each other.
    include_host_paths: bool = Field(
        default=False,
        description=(
            "Disclose each repository's absolute path on this machine. Host paths "
            "are private by default: setting this to true is an explicit request "
            "for local filesystem coordinates, which describe the operator's "
            "machine rather than the code and are meaningless to any process "
            "elsewhere. Use `repository_slug` for repository identity in normal "
            "work, and every other capability accepts it. Ask for host paths only "
            "when the task genuinely needs on-disk locations, such as operator, "
            "debugging or integration work outside RepoAtlas. A host path is not a "
            "way to read a repository: use inspect and search, whose results are "
            "ledgered and citable."
        ),
    )
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
    host_path: Path | None = Field(
        default=None,
        description=(
            "Absolute path on the machine running RepoAtlas. Populated only when "
            "the request set `include_host_paths`; `null` otherwise. Identity for "
            "every other capability is `repository_slug`, not this."
        ),
    )


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
    #: The typed verdict for the repositories **in scope**. Callers branch on
    #: :class:`~repoatlas.models.freshness.FreshnessState` and on the individual
    #: dimensions — never on
    #: :attr:`~repoatlas.models.freshness.RepositoryFreshness.detail`, which carries
    #: V1's human sentence for continuity and is deliberately less precise than the
    #: model it is rendered from.
    #:
    #: :attr:`~repoatlas.models.freshness.IndexFreshness.state` is the worst of the
    #: repositories in scope and nothing else. It does not fold in repository-set
    #: drift, which is a fact about the managed root rather than about any
    #: repository beneath it, and there is deliberately **no** aggregate boolean
    #: combining the two. Collapsing differently-scoped facts into one convenience
    #: flag is how a caller ends up unable to tell which of them it is reading, and
    #: how an unchecked fact becomes an asserted one.
    freshness: IndexFreshness
    #: Most recent successful sync across the scope.
    last_sync: datetime | None = None
    #: Whether repository-set drift was **evaluated on this call**.
    #:
    #: The distinction this field exists to make, and the reason the collections
    #: below cannot carry it alone:
    #:
    #: * ``False`` — nothing looked. Every drift collection is empty because the
    #:   walk did not happen, which is **not** a finding of "no drift".
    #: * ``True`` — the managed root was walked. Empty collections now mean
    #:   evaluated and none found.
    #:
    #: Same principle as ``STORED`` freshness declining to report ``CURRENT``:
    #: "checked and absent" and "not checked" are different claims, and a model
    #: that cannot tell them apart will be read as the stronger one. Structural
    #: rather than a sentence, because a caller has to branch on it.
    drift_checked: bool = False
    #: Whether any drift was found. Meaningful only when :attr:`drift_checked`.
    drift_detected: bool = False
    #: The drift collections describe the **whole managed root**, at every scope.
    #:
    #: The root is not a property of any repository beneath it, so a
    #: repository-scoped request that asks for drift is asking a global question.
    #: Per-repository freshness *is* narrowed by scope; these are not, and a
    #: response carrying both says which is which rather than leaving a global fact
    #: to be misattributed to the one repository that was asked about.
    drift_is_global: bool = True

    #: Discovered beneath the root and not registered. The next global ``sync``
    #: registers and indexes them.
    new_repositories: tuple[str, ...] = ()
    #: Registered, and the directory is gone. The next global ``sync`` retires them.
    missing_repositories: tuple[str, ...] = ()
    #: Registered rows found again at a new path — a moved managed root. Reported
    #: because they count as drift: without them, :attr:`drift_detected` could be
    #: ``True`` with every other collection empty, which is the ambiguity this
    #: whole group exists to remove.
    relocated_repositories: tuple[str, ...] = ()
    #: Identities claimed by both a registered repository that still exists and a
    #: different one beneath the root. ``sync`` cannot register these without
    #: merging two repositories, so it names them and leaves them alone.
    conflicting_repositories: tuple[str, ...] = ()
    #: Directories discovery was refused. "We found nothing there" and "we were not
    #: allowed to look" are different claims (V1_SPEC §5).
    unreadable_paths: tuple[Path, ...] = ()


# ---------------------------------------------------------------------------
# Shared evidence vocabulary
#
# Every capability that hands an agent something it can cite speaks these three
# types, so they live beneath all of them rather than inside whichever one was
# written first. `search` reaches them by rank, `inspect` by name; the evidence
# they produce is the same kind of thing and is recorded in the same ledger, and
# a second EvidenceItem "for inspection" would be a second set of rules about
# what a citation may claim.
# ---------------------------------------------------------------------------


class EntityHandle(BaseModel):
    """A natural, reusable identity for an entity behind a piece of evidence.

    The point of it is the next call: an agent that found ``env:SERVICE_HOST``
    should be able to ask about its relationships without a second fuzzy lookup
    that might resolve to something else. The key is exact and is what the graph
    capability will accept verbatim.

    Never a database id. A row id is allocation order, so a clean reindex of the
    same tree produces different ones, and a handle stored across a reindex would
    silently start naming a different entity — or nothing.
    """

    model_config = ConfigDict(frozen=True)

    #: The natural key, e.g. ``env:SERVICE_HOST``, ``module:src.config``,
    #: ``sym:python-service:src/config.py:load_config:18``.
    key: str
    type: EntityType
    #: Whether the key embeds a source position and therefore stops resolving when
    #: the definition moves. ``sym:`` keys do; interned identities such as ``env:``
    #: and ``module:`` do not. Published so a caller knows which handles are safe
    #: to keep across a sync, rather than discovering it from a failed lookup.
    positional: bool = False


class EvidenceItem(BaseModel):
    """One piece of evidence, as this search run found it.

    ``text`` is exactly what the caller was shown, and ``ref.span_digest`` is a
    digest of exactly these characters. ``ref``'s line range covers only complete
    lines present in ``text``.
    """

    model_config = ConfigDict(frozen=True)

    #: Ledger identity, valid inside this investigation only.
    id: EvidenceId
    #: 1-based position in **this** response, after budget shaping. Not a score,
    #: not stable across queries, and never leaves gaps: if a candidate could not
    #: fit, the ranks that follow are still consecutive because nothing follows.
    rank: int = Field(gt=0)
    ref: EvidenceRef
    #: Display name of the repository. Not unique; ``ref.repository_slug`` is the
    #: identity.
    repository_name: str = ""
    #: ``slug/path``, ``slug/path:line`` or ``slug/path:start-end``. Derived from
    #: ``ref``, so it can never describe a wider span than was supplied.
    location: str = ""
    #: The supplied text. Empty for file-identity evidence, which claims the file
    #: exists and nothing about any line.
    text: str = ""
    #: True when this candidate was shortened to fit the budget. More of *this*
    #: source existed and was not returned.
    truncated: bool = False

    #: Why **this run** matched it, in the retriever's words. Diagnostic prose.
    why: str = ""
    #: Strength as established by **this run**. The ledger may hold something
    #: stronger from an earlier query; that is the ledger's view, not this one's.
    tier: TierField = EvidenceTier.DETERMINISTIC
    match: MatchField = MatchClass.EXACT_CONTENT
    #: Stated rather than left to be inferred from a line-less ``ref``, because a
    #: filename match is line-less too and says something different: that one
    #: supplies no text, and this one supplies text that reads like source and is
    #: not. Additive, and ``False`` for everything read from the file.
    declaration: bool = Field(
        default=False,
        description=(
            "True when `text` is the declaration RepoAtlas recorded for an entity "
            "— such as `def load_config()`, `class ServiceConfig` or a "
            "configuration key's dotted path — rather than bytes read from the "
            "file. It may not appear in the file verbatim, so it is supplied as a "
            "claim about the file and carries no line range: it can show that "
            "the entity is declared there, not what its source says. To read the "
            "source, inspect `entity.key`."
        ),
    )

    #: The entity behind an entity-backed result, when one resolves exactly.
    #: ``None`` is a truthful answer and never a fabricated key.
    entity: EntityHandle | None = None
    #: Whether the ledger still stands behind this id. ``VALID`` at allocation;
    #: an id re-supplied after a revalidation may carry something else.
    status: EvidenceStatus = EvidenceStatus.VALID


class ContextView(BaseModel):
    """What the context resolved to, as identities rather than rows.

    A transport-neutral projection of
    :class:`~repoatlas.models.capability.ContextResolution`, which carries whole
    ``Repository`` objects and therefore their database ids. The caller needs to
    know *what was searched*; it must not receive a surrogate id to store.
    """

    model_config = ConfigDict(frozen=True)

    context: CapabilityContext
    scope: CapabilityScope
    #: Canonical identities of the repositories in scope, ordered by slug.
    repositories: tuple[str, ...] = ()
    #: The repository the question was asked "in", if any. In a capability call it
    #: is a boundary: evidence comes from it or from something that belongs to no
    #: repository at all. Reaching another one is asked for, by ``scope: global`` or
    #: by ``relationships(cross_repository=true)``. The V1 CLI's own primary scope
    #: is a bias rather than a wall (ARCHITECTURE.md §10); this is not that field.
    primary: str | None = None
    #: Human-readable account of how the scope was decided. Never a discriminator.
    reason: str = ""


# ---------------------------------------------------------------------------
# Source coordinates
# ---------------------------------------------------------------------------


class LineRange(BaseModel):
    """A closed, 1-based interval of source lines. Both ends inclusive."""

    model_config = ConfigDict(frozen=True)

    start: int = Field(ge=1)
    end: int = Field(ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> LineRange:
        if self.end < self.start:
            raise ValueError(f"line range ends before it starts: {self.start}-{self.end}")
        return self

    @property
    def count(self) -> int:
        return self.end - self.start + 1
