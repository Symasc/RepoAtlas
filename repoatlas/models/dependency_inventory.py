"""Wire models for the ``dependencies`` capability (RepoAtlas V2).

A truthful projection of the dependency substrate, and deliberately not a tidier
one. V1 records what manifests and lockfiles *say*; it resolves nothing, installs
nothing and executes nothing. Every temptation this module resists is the same
temptation in a different costume: to turn a statement about a file into a
statement about a running program.

Three distinctions are built into the types rather than left to the code.

**A declaration is not a resolution.** ``requests==2.31.0`` in a manifest is a
request; ``requests 2.31.0`` in a lockfile is what a resolver once wrote down.
They are separate rows in the index precisely because a package can be declared
without being locked and locked without being declared, and :class:`DependencyRole`
is what says which one an observation is. Neither is evidence that the package is
installed, importable, or present on any disk RepoAtlas can see.

**Observations are never reduced to a winner.** One package may be declared in
two repositories with two different constraints and locked to two different
versions, and all four statements are true of the code. Grouping them under one
package identity is presentation; discarding any of them to produce a single
``version`` field would be the destructive step, and there is no such field here.

**A provider candidate is not a resolved provider.** Knowing that two registered
repositories publish a package named ``shared-utils`` is knowledge about names,
not about which one a build would pick. Both are returned, ambiguity is stated,
and :class:`ProviderResolution` reports what the deterministic linker concluded —
which for two candidates is *nothing*.
"""

from __future__ import annotations

import enum

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from repoatlas.models.capability import CapabilityContext
from repoatlas.models.capability_io import ContextView, EvidenceItem
from repoatlas.models.dependencies import DependencyRole
from repoatlas.models.freshness import FreshnessDepth, IndexFreshness
from repoatlas.models.investigation import EvidenceId, LedgerSummary

#: The most package groups one page may carry, whatever is asked.
MAX_PAGE_SIZE = 200

#: Enough to see a real inventory without becoming a dump.
DEFAULT_PAGE_SIZE = 50


class Ecosystem(enum.StrEnum):
    """The package ecosystems the dependency layer parses.

    Closed on purpose: an ecosystem outside this set is a malformed request rather
    than a query that happens to match nothing, and the two deserve different
    answers. The set is not a guess — it is exactly what
    ``DependencyRegistry.ecosystems`` reports, and a test pins the two together so
    that adding a parser fails loudly here instead of silently making a real
    ecosystem unaskable.
    """

    PYPI = "pypi"
    NPM = "npm"
    GEM = "gem"


class ProviderResolution(enum.StrEnum):
    """What the deterministic linker concluded about who provides a package.

    Not a ranking and not a recommendation. ``AMBIGUOUS`` is a complete answer: it
    says the index knows several repositories publish this name and knows nothing
    that would choose between them, which is why the linker draws no
    cross-repository edge in that case (V1_SPEC §9).
    """

    #: No registered repository publishes this package name.
    NONE = "none"
    #: Exactly one does, so the linker resolves the dependency to it.
    UNIQUE = "unique"
    #: Several do. Every candidate is returned and none is chosen.
    AMBIGUOUS = "ambiguous"
    #: The question does not arise: either the ecosystem has no notion of a
    #: repository publishing a package, or this repository never declares this one
    #: and so has nothing to resolve. Distinct from ``NONE``, which is a real
    #: answer — that one says nothing provides it.
    NOT_APPLICABLE = "not_applicable"


class DependencyProvenance(BaseModel):
    """Where RepoAtlas read a dependency fact.

    Always available for a stored observation — every dependency row carries the
    file and lines that stated it. Deliberately **not** a citation: whether the
    text at these coordinates could be reconstructed from the indexed snapshot and
    recorded is a separate question, and :attr:`citable` is its answer rather than
    an assumption drawn from this being present.
    """

    model_config = ConfigDict(frozen=True)

    repository_slug: str
    path: str
    line_start: int
    line_end: int
    #: Which dependency parser stated it — ``pyproject``, ``package-lock``, ….
    parser: str = ""
    #: True only when the exact source text at these coordinates was supplied and
    #: recorded in this investigation's ledger.
    citable: bool = False


class DependencyObservationView(BaseModel):
    """One dependency fact stated by one file.

    The unit of truth. ``value`` is what the file said and :attr:`role` is what it
    said it about, which is the whole distinction between asking for a version and
    recording one. The three named views below read that pair without a caller
    having to know the convention — and there is one stored value behind all of
    them, so they cannot drift apart.
    """

    model_config = ConfigDict(frozen=True)

    repository_slug: str
    role: DependencyRole
    #: The constraint for a declaration, the pinned version for a resolution or a
    #: provision. Exactly as parsed: ``^1.3.0``, ``~> 3.0``, ``>=2,<3`` and
    #: ``workspace:*`` are returned as written, because interpreting a range is an
    #: analysis RepoAtlas does not perform.
    value: str | None = None
    #: The manifest's own grouping — ``main``, ``dev``, ``development,test``.
    #: Distinct from :attr:`role`, which is about declaration versus resolution.
    #: ``None`` where the file states no group, which lockfiles generally do not.
    group: str | None = None
    #: The directory owning the stating file, and the unit of manifest-to-lockfile
    #: pairing: ``backend/package.json`` pairs with ``backend/package-lock.json``
    #: and never with a root lockfile.
    scope: str = "."
    provenance: DependencyProvenance
    #: Ledger ids for the supplied source text stating this fact, if any were
    #: recorded. Empty is truthful and frequent.
    evidence_ids: tuple[EvidenceId, ...] = ()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def declared_constraint(self) -> str | None:
        """The constraint a manifest asked for, or ``None`` if this is not one.

        ``None`` for a lockfile row is the point: a resolved version is not the
        constraint that produced it, and there is no field here that would let one
        be read as the other.
        """
        return self.value if self.role is DependencyRole.DECLARED else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def locked_version(self) -> str | None:
        """The version a lockfile pinned, or ``None``.

        A lockfile is a record of what a resolver once chose. It is not evidence
        that the package is installed, that it is what a build would pick today, or
        that anything imports it — RepoAtlas reads files and does not run
        resolvers.
        """
        return self.value if self.role is DependencyRole.RESOLVED else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def provided_version(self) -> str | None:
        """The version this repository publishes itself as, or ``None``."""
        return self.value if self.role is DependencyRole.PROVIDED else None


class ProviderView(BaseModel):
    """A repository that publishes a package name, with the manifest that says so.

    A candidate, not a conclusion. It answers "which registered repository claims
    this name" and nothing about which one a resolver would reach for — that is
    what :attr:`ProviderResolution` on the package is for, and its answer is
    frequently "several, and I do not know".
    """

    model_config = ConfigDict(frozen=True)

    repository_slug: str
    #: Display name of the providing repository. Not unique; the slug is identity.
    repository_name: str = ""
    #: The version that repository publishes, where its manifest states one.
    version: str | None = None
    #: The provision's **own** source location — the line in the *provider's*
    #: manifest that publishes the name. Never the line of the declaration that
    #: went looking for it: those are two different files making two different
    #: statements, and one is not evidence of the other.
    provenance: DependencyProvenance
    evidence_ids: tuple[EvidenceId, ...] = ()


class DependencyPackageView(BaseModel):
    """Every observation of one package in one repository, grouped, never reduced.

    Identity is ``(repository_slug, ecosystem, normalized_name)`` — the substrate's
    own. The ecosystem is part of it because it has to be: npm ``foo-bar`` and PyPI
    ``foo_bar`` normalize to the same string and are not the same dependency. The
    repository is part of it because two repositories declaring one package are
    making two statements, and merging them across a managed root would produce a
    version nobody wrote.
    """

    model_config = ConfigDict(frozen=True)

    repository_slug: str
    repository_name: str = ""
    ecosystem: str
    #: The name as a human wrote it in the manifest.
    name: str
    #: The name under this ecosystem's own equality rule, which is what matching
    #: uses. PEP 503 for PyPI, lowercase for npm and gem.
    normalized_name: str
    #: Every stored fact about this package here, in a deterministic order. There
    #: is no ``version`` field beside this and there is not going to be one.
    observations: tuple[DependencyObservationView, ...] = ()
    #: Registered repositories publishing this name, when the request asked for
    #: them. Empty when it did not, which is not the same as none existing.
    providers: tuple[ProviderView, ...] = ()
    #: What the linker concludes from those candidates.
    provider_resolution: ProviderResolution = ProviderResolution.NONE


class AppliedPageLimits(BaseModel):
    """The page bounds the server actually applied."""

    model_config = ConfigDict(frozen=True)

    requested: int | None = None
    applied: int = Field(gt=0)
    #: Field names the server lowered to its own ceiling.
    clamped: tuple[str, ...] = ()
    ceiling: int = MAX_PAGE_SIZE


class DependenciesRequest(BaseModel):
    """Ask what the index records about declared and resolved packages."""

    model_config = ConfigDict(frozen=True)

    context: CapabilityContext
    #: Narrow to one ecosystem. ``None`` means every ecosystem.
    ecosystem: Ecosystem | None = None
    #: An exact package name, compared under the ecosystem's own equality rule
    #: rather than as a substring. This is structured inventory, not search: a
    #: caller who wants to explore by resemblance has a capability for that.
    name: str | None = None
    #: Narrow to particular observation kinds. Empty means all three.
    roles: tuple[DependencyRole, ...] = ()
    include_providers: bool = True
    #: Whether to supply the source text stating each observation. Off makes the
    #: call cheap and allocates no evidence; on is the citable mode.
    include_provenance: bool = True
    limit: int | None = Field(default=None, gt=0)
    #: Opaque continuation from a previous page. Never constructed by a caller.
    cursor: str | None = None
    investigation_id: str | None = None
    depth: FreshnessDepth = FreshnessDepth.STORED

    @field_validator("name")
    @classmethod
    def _name_is_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("name must be a package name, or be omitted entirely")
        return value

    @field_validator("cursor")
    @classmethod
    def _cursor_is_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("cursor must be a continuation token, or be omitted entirely")
        return value


class DependenciesResponse(BaseModel):
    """One page of the deterministic dependency inventory."""

    model_config = ConfigDict(frozen=True)

    investigation_id: str
    context: ContextView
    #: Package groups, in an order fixed by the repositories' own identities so
    #: that the same index answers the same question the same way.
    packages: tuple[DependencyPackageView, ...] = ()
    #: Source text stating these observations, recorded in the ledger before this
    #: response existed. Supplied once per span however many facts rest on it.
    evidence: tuple[EvidenceItem, ...] = ()
    freshness: IndexFreshness
    #: Opaque token for the next page. ``None`` when this page is the last.
    next_cursor: str | None = None
    #: Whether further package groups exist after this page under the same
    #: filters. Determined by looking, never inferred from a full page.
    has_more: bool = False
    limits: AppliedPageLimits
    ledger: LedgerSummary
