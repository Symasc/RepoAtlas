"""The outcome of every ordinary reference claim (ORDINARY_REFERENCE_RESOLUTION.md, phase A).

An ordinary claim is one ``ReferenceLinker._resolve`` decides: a call, reference, import,
export, configuration read, script path, Salt requisite or template include that names its
target by text. The global pass either links it or it does not, and until now the only
trace of the second case was one number, ``SyncReport.unresolved_references``.

This module is the vocabulary of a second, separate account. It never changes what the
linker does; it says, for each claim that did not become an edge, **what is established
about why**. The codes are closed sets on purpose: a subreason is never a source
identifier, a path or a host, so a table keyed by them is bounded by the enumeration and
carries no source text (see ``ordinary_accounting`` below).

The account is its own model. It is **not** a decomposition of the legacy scalar, because
the two count different things: the scalar also holds planner claims (HTTP, GraphQL) and
the ``USES`` stage's remainder, and it counts a claim that only has an interned module node
as unresolved exactly as it counts one that has no target at all. The bridge between them is
stored (``legacy_*`` keys) and any residual is shown, never absorbed.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict, Field

# -- outcomes ---------------------------------------------------------------------------

#: The claim became an edge to the target it names, by the ordinary rules.
LINKED: Final = "linked"
#: Evidence establishes a target outside the managed index (stdlib, a declared package).
EXTERNAL: Final = "external"
#: A known target is outside admitted indexed coverage.
NOT_INDEXED: Final = "not-indexed"
#: A complete supported lookup found no target in its searched scope.
TARGET_NOT_FOUND: Final = "target-not-found"
#: The target depends on a runtime value static facts cannot establish.
DYNAMIC: Final = "dynamic"
#: The applicable evidence leaves more than one valid target.
AMBIGUOUS: Final = "ambiguous"
#: The syntax or resolution semantics are not modelled by the ordinary rules.
UNSUPPORTED: Final = "unsupported"
#: Required evidence could not be read.
EVIDENCE_UNREADABLE: Final = "evidence-unreadable"
#: A cycle or budget stopped the proof.
TRACE_BOUND: Final = "trace-bound"
#: No rule classified it. Never expected for a supported family; shown, never hidden.
UNCLASSIFIED: Final = "unclassified"
#: A claim another planner owns (or the USES stage): counted apart, never ordinary.
EXCLUDED: Final = "excluded"

#: Outcomes of a claim the ordinary rules considered and did not link.
REFUSALS: Final = (
    EXTERNAL,
    NOT_INDEXED,
    TARGET_NOT_FOUND,
    DYNAMIC,
    AMBIGUOUS,
    UNSUPPORTED,
    EVIDENCE_UNREADABLE,
    TRACE_BOUND,
    UNCLASSIFIED,
)

#: Every outcome a row may carry.
OUTCOMES: Final = frozenset({LINKED, EXCLUDED, *REFUSALS})

#: The outcomes that get a per-claim row in ``link_decisions``: what is still open. A linked
#: claim is an edge and an external one is a fact about the world; both are counted only.
OPEN_OUTCOMES: Final = frozenset(
    {
        NOT_INDEXED,
        TARGET_NOT_FOUND,
        DYNAMIC,
        AMBIGUOUS,
        UNSUPPORTED,
        EVIDENCE_UNREADABLE,
        TRACE_BOUND,
        UNCLASSIFIED,
    }
)

#: ``link_decisions.family`` of the per-claim rows. Impact never lists this family.
ORDINARY_FAMILY: Final = "ordinary"

#: Per-claim rows kept for each (repository, outcome, subreason); the rest are counted in
#: ``truncated`` and are in the aggregate. Pre-registered, not tuned.
OPEN_ROWS_PER_KEY: Final = 200

# -- subreasons (closed) ----------------------------------------------------------------

# linked
RESOLVED: Final = "resolved"
PILLAR_READ: Final = "pillar-read"
# external
STDLIB: Final = "stdlib"
NODE_BUILTIN: Final = "node-builtin"
DECLARED_PACKAGE: Final = "declared-package"
# not-indexed
OUTSIDE_REPOSITORY: Final = "outside-repository"
OUTSIDE_MANAGED_ROOTS: Final = "outside-managed-roots"
# target-not-found
RELATIVE_NO_FILE: Final = "relative-no-file"
SUBMODULE_CANDIDATE_ABSENT: Final = "submodule-candidate-absent"
NO_INDEXED_FILE: Final = "no-indexed-file"
NO_STATE_FILE: Final = "no-state-file"
NO_STATE_IN_REPOSITORY: Final = "no-state-in-repository"
NO_SALT_SOURCE_FILE: Final = "no-salt-source-file"
KEY_NOT_IN_SCOPE: Final = "key-not-in-scope"
# dynamic
COMPUTED_SPECIFIER: Final = "computed-specifier"
RUNTIME_SUBSTITUTION: Final = "runtime-substitution"
# ambiguous
MULTIPLE_DECLARATIONS: Final = "multiple-declarations-in-module"
DUPLICATE_STATE_ID: Final = "duplicate-state-id"
AMBIGUOUS_SALT_SOURCE: Final = "ambiguous-salt-source"
DUPLICATE_KEY_IN_SCOPE: Final = "duplicate-key-in-scope"
# unsupported
LANGUAGE_NOT_MODELED: Final = "language-not-modeled"
NO_IMPORT_ORIGIN: Final = "no-import-origin"
CONFIGURED_ALIAS: Final = "configured-alias"
WORKSPACE_PACKAGE: Final = "workspace-package"
IN_PROJECT_PACKAGE: Final = "in-project-package"
CROSS_REPOSITORY_PROVIDER: Final = "cross-repository-provider"
UNDECLARED_BARE_SPECIFIER: Final = "undeclared-bare-specifier"
UNDECLARED_ABSOLUTE_MODULE: Final = "undeclared-absolute-module"
RECEIVER_MEMBER: Final = "receiver-member"
BINDING_FORM_UNTRACED: Final = "binding-form-untraced"
NON_PILLAR_CONFIG: Final = "non-pillar-config"
NO_ORDINARY_RESOLVER: Final = "no-ordinary-resolver"
# evidence-unreadable
ORIGIN_FILE_MISSING: Final = "origin-file-missing"
# unclassified
NO_RULE: Final = "no-rule"

#: Every subreason that may be stored. A test asserts nothing outside this set is emitted, so
#: the aggregate table's size is bounded by the enumeration and never by claim text.
SUBREASONS: Final = frozenset(
    {
        RESOLVED,
        PILLAR_READ,
        STDLIB,
        NODE_BUILTIN,
        DECLARED_PACKAGE,
        OUTSIDE_REPOSITORY,
        OUTSIDE_MANAGED_ROOTS,
        RELATIVE_NO_FILE,
        SUBMODULE_CANDIDATE_ABSENT,
        NO_INDEXED_FILE,
        NO_STATE_FILE,
        NO_STATE_IN_REPOSITORY,
        NO_SALT_SOURCE_FILE,
        KEY_NOT_IN_SCOPE,
        COMPUTED_SPECIFIER,
        RUNTIME_SUBSTITUTION,
        MULTIPLE_DECLARATIONS,
        DUPLICATE_STATE_ID,
        AMBIGUOUS_SALT_SOURCE,
        DUPLICATE_KEY_IN_SCOPE,
        LANGUAGE_NOT_MODELED,
        NO_IMPORT_ORIGIN,
        CONFIGURED_ALIAS,
        WORKSPACE_PACKAGE,
        IN_PROJECT_PACKAGE,
        CROSS_REPOSITORY_PROVIDER,
        UNDECLARED_BARE_SPECIFIER,
        UNDECLARED_ABSOLUTE_MODULE,
        RECEIVER_MEMBER,
        BINDING_FORM_UNTRACED,
        NON_PILLAR_CONFIG,
        NO_ORDINARY_RESOLVER,
        ORIGIN_FILE_MISSING,
        NO_RULE,
    }
)

# -- exclusions (closed): why a standing claim is not ordinary --------------------------

MODULE_FACT: Final = "module-fact"
SALT_MAPPING: Final = "salt-mapping"
GRAPHQL_PLANNER: Final = "graphql-planner"
DEPLOYMENT_FACT: Final = "deployment-fact"
PERSISTENCE_PLANNER: Final = "persistence-planner"
HTTP_PLANNER: Final = "http-planner"
USES_STAGE: Final = "uses-stage"

EXCLUSIONS: Final = frozenset(
    {
        MODULE_FACT,
        SALT_MAPPING,
        GRAPHQL_PLANNER,
        DEPLOYMENT_FACT,
        PERSISTENCE_PLANNER,
        HTTP_PLANNER,
        USES_STAGE,
    }
)

#: ``language`` of an excluded row: the dimension means nothing there.
ANY_LANGUAGE: Final = "any"
#: The closed set of ``language`` values of an ordinary row.
LANGUAGES: Final = frozenset({"python", "node", "other", ANY_LANGUAGE})

# -- accounting keys (closed) -----------------------------------------------------------

#: Every standing claim in the snapshot (``unresolved_refs`` rows).
STANDING_CLAIMS: Final = "standing_claims"
#: Rows the ordinary dispatch considered.
ORDINARY_CONSIDERED: Final = "ordinary_considered"
ORDINARY_LINKED: Final = "ordinary_linked"
ORDINARY_NOT_LINKED: Final = "ordinary_not_linked"
#: Not-linked ordinary claims of an interned kind (module, config): each already has an
#: interned node edge, which is a fact about the *name*, not a resolved source relationship.
INTERNED_NODE_ONLY: Final = "interned_node_only"
#: Rows owned by the USES stage (counted, not classified).
USES_ROWS: Final = "uses_rows"
#: ``standing_claims - (ordinary_considered + every excluded count)``; 0 unless something is lost.
PARTITION_RESIDUAL: Final = "partition_residual"
#: Open per-claim rows not written because a (repository, outcome, subreason) key hit its cap.
OPEN_ROWS_TRUNCATED: Final = "open_rows_truncated"
#: Prefix of the per-reason excluded counts: ``excluded:<reason>``.
EXCLUDED_PREFIX: Final = "excluded:"
#: The legacy scalar and the parts it is made of.
LEGACY_UNRESOLVED: Final = "legacy_unresolved"
LEGACY_ORDINARY: Final = "legacy_ordinary"
LEGACY_GRAPHQL_UNKNOWN_ROLE: Final = "legacy_graphql_unknown_role"
LEGACY_PLANNERS: Final = "legacy_planners"
LEGACY_USES_REMAINDER: Final = "legacy_uses_remainder"
#: ``legacy_unresolved`` minus the four parts; 0 unless the scalar counts something else.
LEGACY_RESIDUAL: Final = "legacy_residual"


class OutcomeRow(BaseModel):
    """How many claims share one (repository, language, relationship, target kind, outcome,
    subreason). A count and closed codes only: no path, name or text of any source."""

    model_config = ConfigDict(frozen=True)

    repo_id: int
    language: str
    edge_type: str
    target_kind: str
    outcome: str
    subreason: str
    claims: int


class OrdinarySummary(BaseModel):
    """One sync's ordinary-claim account, summed over repositories: counts only."""

    model_config = ConfigDict(frozen=True)

    #: Claims by outcome (``linked`` and every refusal), ordinary claims only.
    outcomes: dict[str, int] = Field(default_factory=dict)
    #: Standing claims by the reason another planner (or the USES stage) owns them.
    excluded: dict[str, int] = Field(default_factory=dict)
    #: The accounting keys above, as stored.
    accounting: dict[str, int] = Field(default_factory=dict)

    @property
    def considered(self) -> int:
        return self.accounting.get(ORDINARY_CONSIDERED, 0)

    @property
    def linked(self) -> int:
        return self.outcomes.get(LINKED, 0)
