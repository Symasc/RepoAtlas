"""Why an ordinary claim did not become an edge (ORDINARY_REFERENCE_RESOLUTION.md, phase A).

``ReferenceLinker._resolve`` answers an ordinary claim with an edge or with nothing. This
module explains the nothing, and counts the rest, **without being able to change either**:
a claim is classified only after ``_resolve`` has already returned ``None`` for it, the
classifier reads the same indexed facts and writes nothing, and the edges the pass plans
are computed before it is asked anything. A defect here can mislabel a claim; it cannot link
one, unlink one or move an edge.

What each outcome may rest on is deliberately narrow:

* ``external`` needs evidence that the target is a package or the standard library, never the
  mere absence of an indexed file. The standard library is read from the interpreter running
  RepoAtlas (``sys.stdlib_module_names``) and Node's core modules from a fixed list; a third
  party package counts only when the claim's repository declares it **and** no managed
  repository provides it. A name nobody declares is ``unsupported``, not external: the
  ordinary lookup does not read tsconfig aliases, workspaces or Python project layouts, so
  "nothing found" is not "nothing there".
* ``target-not-found`` is said only where the lookup that failed is the complete supported
  lookup for that claim (a relative path against the indexed files).
* Nothing here follows a re-export, an alias or a receiver. Those are capabilities the
  ordinary rules lack, and they are reported as ``unsupported`` with the form that was met.

The per-claim rows go to ``link_decisions`` (family ``ordinary``) for open outcomes only, and
are capped per (repository, outcome, subreason); everything is also counted in the aggregate.
Neither carries source text: an aggregate row is closed codes and a count, and a per-claim
row holds only the claim's own target spelling when that is a plain identifier or specifier.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Final

from repoatlas.dependencies.normalize import NPM, PYPI, normalize
from repoatlas.graph.modules import ModuleResolver, split_package
from repoatlas.graph.resolution import ResolutionIndex, _normalize
from repoatlas.models.dependencies import DependencyRecord
from repoatlas.models.ordinary_outcomes import (
    ALIAS_TARGET_NOT_INDEXED,
    AMBIGUOUS,
    AMBIGUOUS_SALT_SOURCE,
    ANY_LANGUAGE,
    BINDING_FORM_UNTRACED,
    COMPUTED_SPECIFIER,
    CONFIGURED_ALIAS,
    CONFLICTING_PACKAGE_ENTRY,
    CROSS_REPOSITORY_PROVIDER,
    DECLARATION_NAMES_OTHER_SOURCE,
    DECLARED_PACKAGE,
    DUPLICATE_KEY_IN_SCOPE,
    DUPLICATE_STATE_ID,
    DUPLICATE_WORKSPACE_PROVIDER,
    DYNAMIC,
    EXCLUDED_PREFIX,
    EXCLUSIONS,
    EXTERNAL,
    IN_PROJECT_PACKAGE,
    INTERNED_NODE_ONLY,
    KEY_NOT_IN_SCOPE,
    LANGUAGE_NOT_MODELED,
    LEGACY_GRAPHQL_UNKNOWN_ROLE,
    LEGACY_ORDINARY,
    LEGACY_PLANNERS,
    LEGACY_RESIDUAL,
    LEGACY_UNRESOLVED,
    LEGACY_USES_REMAINDER,
    LINKED,
    MULTIPLE_DECLARATIONS,
    NO_IMPORT_ORIGIN,
    NO_INDEXED_FILE,
    NO_ORDINARY_RESOLVER,
    NO_RULE,
    NO_SALT_SOURCE_FILE,
    NO_STATE_FILE,
    NO_STATE_IN_REPOSITORY,
    NODE_BUILTIN,
    NON_PILLAR_CONFIG,
    NOT_INDEXED,
    OPEN_OUTCOMES,
    OPEN_ROWS_PER_KEY,
    OPEN_ROWS_TRUNCATED,
    ORDINARY_CONSIDERED,
    ORDINARY_FAMILY,
    ORDINARY_LINKED,
    ORDINARY_NOT_LINKED,
    OUTSIDE_MANAGED_ROOTS,
    OUTSIDE_REPOSITORY,
    PACKAGE_ENTRY_NOT_FOUND,
    PARTITION_RESIDUAL,
    RECEIVER_MEMBER,
    RELATIVE_NO_FILE,
    RESOLVED,
    RUNTIME_SUBSTITUTION,
    STANDING_CLAIMS,
    STDLIB,
    SUBMODULE_CANDIDATE_ABSENT,
    TARGET_NOT_FOUND,
    UNCLASSIFIED,
    UNDECLARED_ABSOLUTE_MODULE,
    UNDECLARED_BARE_SPECIFIER,
    UNSUPPORTED,
    USES_ROWS,
    USES_STAGE,
    WORKSPACE_PACKAGE,
    OrdinarySummary,
    OutcomeRow,
)
from repoatlas.models.parsing import EdgeType, EntityType
from repoatlas.storage.protocols import (
    FileRow,
    LinkDecisionRow,
    UnresolvedRefRecord,
)

#: Node's core modules. ``test`` and ``sqlite`` exist only behind ``node:`` and are not here.
NODE_BUILTINS: Final = frozenset(
    {
        "assert",
        "async_hooks",
        "buffer",
        "child_process",
        "cluster",
        "console",
        "constants",
        "crypto",
        "dgram",
        "diagnostics_channel",
        "dns",
        "domain",
        "events",
        "fs",
        "http",
        "http2",
        "https",
        "inspector",
        "module",
        "net",
        "os",
        "path",
        "perf_hooks",
        "process",
        "punycode",
        "querystring",
        "readline",
        "repl",
        "stream",
        "string_decoder",
        "sys",
        "timers",
        "tls",
        "trace_events",
        "tty",
        "url",
        "util",
        "v8",
        "vm",
        "wasi",
        "worker_threads",
        "zlib",
    }
)

#: Kinds whose per-file pass interns a node for the name (``linker._INTERNED_KINDS``): a
#: claim of one that stays unlinked still has an edge, to the name and not to a source file.
_INTERNED_CLAIM_KINDS: Final = frozenset({EntityType.MODULE, EntityType.CONFIG})
_INTERNED_KIND_CODES: Final = frozenset(str(kind) for kind in _INTERNED_CLAIM_KINDS)
#: Refusals that say a package entry names no indexed file.
_ENTRY_NOT_FOUND: Final = frozenset(
    {"entry-unresolved", "entry-not-exported", "subpath-not-exported", "subpath-outside-package"}
)
_PATH_KINDS: Final = frozenset({EntityType.SCRIPT, EntityType.FILE})
_SYMBOL_KINDS: Final = frozenset({EntityType.SYMBOL, EntityType.CLASS, EntityType.FUNCTION})
_SAFE_SUBJECT: Final = re.compile(r"^[A-Za-z0-9_.@/:$#~\-]{1,120}$")
_SUBJECT_EXPRESSION: Final = "<expression>"


@dataclass(frozen=True, slots=True)
class Decision:
    """What is established about one claim that did not link."""

    outcome: str
    subreason: str
    #: What was searched, as a closed phrase (``relative-path``, ``repository-manifests``...).
    scope: str = ""
    #: The evidence the outcome rests on, as a closed phrase.
    basis: str = ""


class Classifier:
    """Explains the ordinary claims ``_resolve`` returned ``None`` for. Read-only."""

    def __init__(
        self,
        index: ResolutionIndex,
        records: Sequence[DependencyRecord],
        module_facts: Sequence[UnresolvedRefRecord],
        modules: ModuleResolver | None = None,
        resolved_module: Callable[[FileRow, str, str], FileRow | None] | None = None,
    ) -> None:
        self._index = index
        self._module_facts = module_facts
        #: The resolver the link pass resolved imports with (so its refusals are the ones
        #: explained); built from the same facts when the caller has none.
        self._modules = modules
        self._resolved_module = resolved_module
        #: (repo_id, ecosystem, normalized name) of every package a repository declares, and
        #: the repositories that provide each (ecosystem, normalized name).
        self._declared: set[tuple[int, str, str]] = set()
        self._providers: dict[tuple[str, str], set[int]] = defaultdict(set)
        for record in records:
            key = (record.ecosystem, record.normalized_name)
            if record.role == "provided":
                self._providers[key].add(record.repo_id)
            else:
                self._declared.add((record.repo_id, *key))
        self._roots = tuple(root for root, _ in index.repo_roots)
        #: The decision for each (repository, ecosystem, package) already made.
        self._packages: dict[tuple[int, str, str, str], Decision] = {}
        self._modules_by: dict[tuple[int, str, str, str], Decision] = {}
        self._targets: dict[tuple[int, str, str, str], FileRow | None] = {}

    # -- entry --------------------------------------------------------------------------

    def classify(
        self,
        claim: UnresolvedRefRecord,
        origin: FileRow,
        meta: Mapping[str, str],
        flavor: str | None,
        siblings: Sequence[UnresolvedRefRecord],
    ) -> Decision:
        kind = claim.target_kind
        if kind is EntityType.MODULE:
            return self._module_claim(origin, claim.target_key, meta, flavor)
        if kind in _PATH_KINDS:
            return self._path(origin, claim.target_key)
        if kind is EntityType.SALT_STATE:
            return self._salt_state(claim, origin)
        if kind is EntityType.TEMPLATE:
            return self._template(claim, origin)
        if kind is EntityType.CONFIG:
            return self._config(claim, origin, siblings)
        if kind in _SYMBOL_KINDS:
            return self._symbol(claim, origin, meta, flavor)
        if kind is EntityType.PACKAGE:
            return Decision(UNSUPPORTED, NO_ORDINARY_RESOLVER, "ordinary-dispatch", "no-branch")
        return Decision(UNCLASSIFIED, NO_RULE)

    # -- modules ------------------------------------------------------------------------

    def _module_claim(
        self, origin: FileRow, specifier: str, meta: Mapping[str, str], flavor: str | None
    ) -> Decision:
        if flavor is None:
            return Decision(UNSUPPORTED, LANGUAGE_NOT_MODELED, "importing-file", "extension")
        if meta.get("package") is not None:
            # `from . import name`: a candidate that is a module only if one exists inside a
            # package. The linker resolved nothing, so no submodule was found there.
            return Decision(
                TARGET_NOT_FOUND, SUBMODULE_CANDIDATE_ABSENT, "package-submodule", "indexed-files"
            )
        if flavor == "node":
            return self._import(origin, specifier)
        return self._module(origin, specifier, flavor)

    def _import(self, origin: FileRow, specifier: str) -> Decision:
        """Why an import the link pass could not resolve, configuration included, is unlinked.

        The refusal is the resolver's own (:meth:`ModuleResolver.workspace_refusal`); nothing is
        concluded from a missing file alone.
        """
        if (
            not specifier
            or specifier.startswith((".", "/", "node:"))
            or "$" in specifier
            or "{" in specifier
            or specifier.split("/", 1)[0] in NODE_BUILTINS
        ):
            return self._module(origin, specifier, "node")
        if self._modules is None:
            self._modules = ModuleResolver(self._index, list(self._module_facts))
        if self._modules.resolve(origin, specifier) is not None:
            # Resolved to a file of another repository: refused by the link pass, never linked.
            return Decision(
                UNSUPPORTED, CROSS_REPOSITORY_PROVIDER, "repository-manifests", "other-repository"
            )
        why = self._modules.workspace_refusal(origin, specifier)
        if why == "provider-ambiguous":
            return Decision(AMBIGUOUS, DUPLICATE_WORKSPACE_PROVIDER, "workspace-members", why)
        if why == "entry-ambiguous":
            return Decision(AMBIGUOUS, CONFLICTING_PACKAGE_ENTRY, "package-entry", why)
        if why in _ENTRY_NOT_FOUND:
            return Decision(TARGET_NOT_FOUND, PACKAGE_ENTRY_NOT_FOUND, "package-entry", why)
        if why is not None and why.startswith("dependency-"):
            return Decision(UNSUPPORTED, DECLARATION_NAMES_OTHER_SOURCE, "importer-manifest", why)
        if self._modules.aliased(origin, specifier):
            return Decision(
                TARGET_NOT_FOUND, ALIAS_TARGET_NOT_INDEXED, "tsconfig-paths", "no-indexed-file"
            )
        return self._node_module(origin, specifier)

    def _module(self, origin: FileRow, specifier: str, flavor: str) -> Decision:
        """Why a specifier names no file. It depends on the importing file only through its
        repository and directory (relative paths, the nearest tsconfig), so a decision is made
        once for each and reused: an import is usually claimed by many calls."""
        key = (origin.repo_id, origin.rel_path.rpartition("/")[0], specifier, flavor)
        known = self._modules_by.get(key)
        if known is None:
            known = self._modules_by[key] = self._module_decision(origin, specifier, flavor)
        return known

    def _module_decision(self, origin: FileRow, specifier: str, flavor: str) -> Decision:
        if not specifier or "$" in specifier or "{" in specifier:
            return Decision(DYNAMIC, COMPUTED_SPECIFIER, "specifier-text", "specifier")
        if flavor == "python":
            return self._python_module(origin, specifier)
        if flavor == "node":
            return self._node_module(origin, specifier)
        return Decision(UNSUPPORTED, LANGUAGE_NOT_MODELED, "importing-file", "extension")

    def _python_module(self, origin: FileRow, specifier: str) -> Decision:
        if specifier.startswith("."):
            level = len(specifier) - len(specifier.lstrip("."))
            base = PurePosixPath(origin.rel_path).parent
            for _ in range(level - 1):
                if base == PurePosixPath("."):
                    return Decision(NOT_INDEXED, OUTSIDE_REPOSITORY, "relative-import", "path")
                base = base.parent
            remainder = specifier[level:].replace(".", "/")
            target = _normalize(str(base / remainder) if remainder else str(base))
            if target is None:
                return Decision(NOT_INDEXED, OUTSIDE_REPOSITORY, "relative-import", "path")
            return Decision(TARGET_NOT_FOUND, RELATIVE_NO_FILE, "indexed-files", "path")
        top = specifier.split(".", 1)[0]
        if top in sys.stdlib_module_names:
            return Decision(EXTERNAL, STDLIB, "standard-library", "interpreter-module-names")
        return self._package(origin, PYPI, top, UNDECLARED_ABSOLUTE_MODULE)

    def _node_module(self, origin: FileRow, specifier: str) -> Decision:
        if specifier.startswith((".", "/")):
            directory = PurePosixPath(origin.rel_path).parent
            if _normalize(str(directory / specifier)) is None:
                return Decision(NOT_INDEXED, OUTSIDE_REPOSITORY, "relative-import", "path")
            return Decision(TARGET_NOT_FOUND, RELATIVE_NO_FILE, "indexed-files", "path")
        if specifier.startswith("node:") or specifier.split("/", 1)[0] in NODE_BUILTINS:
            return Decision(EXTERNAL, NODE_BUILTIN, "node-core", "fixed-list")
        if self._aliased(origin, specifier):
            return Decision(UNSUPPORTED, CONFIGURED_ALIAS, "tsconfig-paths", "module-facts")
        split = split_package(specifier)
        if split is None:
            return Decision(UNSUPPORTED, UNDECLARED_BARE_SPECIFIER, "repository-manifests", "none")
        return self._package(origin, NPM, split[0], UNDECLARED_BARE_SPECIFIER)

    def _package(self, origin: FileRow, ecosystem: str, name: str, undeclared: str) -> Decision:
        """A package a bare specifier names: external only on a declaration nobody provides."""
        key = (origin.repo_id, ecosystem, name, undeclared)
        known = self._packages.get(key)
        if known is None:
            known = self._packages[key] = self._package_decision(
                origin.repo_id, ecosystem, name, undeclared
            )
        return known

    def _package_decision(
        self, repo_id: int, ecosystem: str, name: str, undeclared: str
    ) -> Decision:
        normalized = normalize(ecosystem, name)
        providers = self._providers.get((ecosystem, normalized), set())
        declared = (repo_id, ecosystem, normalized) in self._declared
        if providers:
            if repo_id in providers:
                reason = WORKSPACE_PACKAGE if ecosystem == NPM else IN_PROJECT_PACKAGE
                return Decision(UNSUPPORTED, reason, "repository-manifests", "provided-locally")
            return Decision(
                UNSUPPORTED, CROSS_REPOSITORY_PROVIDER, "repository-manifests", "provided-elsewhere"
            )
        if declared:
            return Decision(EXTERNAL, DECLARED_PACKAGE, "repository-manifests", "declared-only")
        return Decision(UNSUPPORTED, undeclared, "repository-manifests", "none")

    def _aliased(self, origin: FileRow, specifier: str) -> bool:
        """Whether the claim's own tsconfig ``paths`` claims the specifier. A read of
        configuration facts already indexed; it resolves nothing."""
        if self._modules is None:
            self._modules = ModuleResolver(self._index, list(self._module_facts))
        return self._modules.aliased(origin, specifier)

    # -- symbols ------------------------------------------------------------------------

    def _symbol(
        self,
        claim: UnresolvedRefRecord,
        origin: FileRow,
        meta: Mapping[str, str],
        flavor: str | None,
    ) -> Decision:
        specifier = meta.get("module")
        if not specifier:
            return Decision(UNSUPPORTED, NO_IMPORT_ORIGIN, "claim", "metadata")
        if flavor is None:
            return Decision(UNSUPPORTED, LANGUAGE_NOT_MODELED, "importing-file", "extension")
        where = (origin.repo_id, origin.rel_path.rpartition("/")[0], specifier, flavor)
        if where not in self._targets:
            resolved = (
                self._resolved_module(origin, specifier, flavor)
                if self._resolved_module is not None
                else None
            )
            self._targets[where] = resolved or self._index.resolve_module(origin, specifier, flavor)
        target = self._targets[where]
        if target is None:
            return self._module(origin, specifier, flavor)
        name = claim.target_key.split(".")[-1]
        found = self._index.symbol_candidates(target, name)
        if len(found) > 1:
            return Decision(AMBIGUOUS, MULTIPLE_DECLARATIONS, "module-file", "symbols")
        # Nothing declared under that name in the module the import names. The global linker
        # already tried proven direct Python from-import re-exports; a member of an imported
        # value, or a re-export that is not proven by that trace, stays unsupported.
        shape = RECEIVER_MEMBER if "." in claim.target_key else BINDING_FORM_UNTRACED
        return Decision(UNSUPPORTED, shape, "module-file", "symbols")

    # -- paths, Salt, templates, configuration ------------------------------------------

    def _path(self, origin: FileRow, raw: str) -> Decision:
        if not raw or "$" in raw or "{" in raw or "*" in raw:
            return Decision(DYNAMIC, RUNTIME_SUBSTITUTION, "path-text", "path")
        if raw.startswith("/"):
            absolute = os.path.normpath(raw)
        else:
            absolute = os.path.normpath(
                str(origin.repo_path / PurePosixPath(origin.rel_path).parent / raw)
            )
        inside = any(
            absolute == str(root) or absolute.startswith(f"{root}{os.sep}") for root in self._roots
        )
        if not inside:
            return Decision(NOT_INDEXED, OUTSIDE_MANAGED_ROOTS, "managed-roots", "path")
        return Decision(TARGET_NOT_FOUND, NO_INDEXED_FILE, "indexed-files", "path")

    def _salt_state(self, claim: UnresolvedRefRecord, origin: FileRow) -> Decision:
        if claim.edge_type is EdgeType.INCLUDES:
            return Decision(TARGET_NOT_FOUND, NO_STATE_FILE, "indexed-files", "include-path")
        matches = self._index.salt_state_candidates(origin.repo_id, claim.target_key)
        if matches > 1:
            return Decision(AMBIGUOUS, DUPLICATE_STATE_ID, "repository-states", "state-id")
        return Decision(TARGET_NOT_FOUND, NO_STATE_IN_REPOSITORY, "repository-states", "state-id")

    def _template(self, claim: UnresolvedRefRecord, origin: FileRow) -> Decision:
        key = claim.target_key
        if key.startswith("salt://"):
            if self._index.salt_source_candidates(key) > 1:
                return Decision(AMBIGUOUS, AMBIGUOUS_SALT_SOURCE, "managed-files", "salt-url")
            return Decision(TARGET_NOT_FOUND, NO_SALT_SOURCE_FILE, "managed-files", "salt-url")
        return self._path(origin, key)

    def _config(
        self,
        claim: UnresolvedRefRecord,
        origin: FileRow,
        siblings: Sequence[UnresolvedRefRecord],
    ) -> Decision:
        root, _, path = claim.target_key.partition(":")
        if root != "pillar" or not path:
            return Decision(UNSUPPORTED, NON_PILLAR_CONFIG, "claim", "key-root")
        qualified = path.replace(":", ".")
        scope = [origin]
        for sibling in siblings:
            if (
                sibling.target_kind is EntityType.SALT_STATE
                and sibling.edge_type is EdgeType.INCLUDES
            ):
                included = self._index.resolve_salt_include(origin, sibling.target_key)
                if included is not None:
                    scope.append(included)
        matches = [
            symbol
            for candidate in scope
            for symbol in self._index.symbols_by_file.get(candidate.file_id, {}).get(qualified, [])
            if symbol.type is EntityType.CONFIG and symbol.qualified_name == qualified
        ]
        if len(matches) > 1:
            return Decision(AMBIGUOUS, DUPLICATE_KEY_IN_SCOPE, "file-and-includes", "key-path")
        return Decision(TARGET_NOT_FOUND, KEY_NOT_IN_SCOPE, "file-and-includes", "key-path")


# -- the ledger -------------------------------------------------------------------------


@dataclass(slots=True)
class _Open:
    """One distinct unresolved site: its place, the decision, and how many claims share it."""

    claim: UnresolvedRefRecord
    origin: FileRow
    decision: Decision
    language: str
    count: int = 1


@dataclass(slots=True)
class OutcomePlan:
    """The account of one global pass, computed beside the graph and applied with it."""

    rows: tuple[OutcomeRow, ...] = ()
    #: Per-claim rows for the open outcomes, capped; replaced with the link decisions.
    decisions: tuple[LinkDecisionRow, ...] = ()
    #: The closed accounting keys known before the commit.
    accounting: dict[str, int] = field(default_factory=dict)
    #: Excluded counts by reason, over every repository.
    excluded: dict[str, int] = field(default_factory=dict)


class OutcomeLedger:
    """Counts every standing claim exactly once: linked, refused, or excluded.

    Nothing here is order dependent. Aggregates are sums; the per-claim rows are chosen by a
    sort key made of repository slug, path, line and the claim's own spelling, never an id, so
    a full reindex and an incremental one keep the same rows.
    """

    def __init__(self) -> None:
        #: Keyed by the enum members themselves; they are turned into codes once, in `plan`.
        self._counts: Counter[tuple[int, str, EdgeType, EntityType, str, str]] = Counter()
        self._open: dict[str, _Open] = {}

    # -- recording ----------------------------------------------------------------------

    def linked(self, claim: UnresolvedRefRecord, language: str, subreason: str = RESOLVED) -> None:
        self._count(claim, language, LINKED, subreason)

    def excluded(self, claim: UnresolvedRefRecord, reason: str) -> None:
        assert reason in EXCLUSIONS, reason
        self._count(claim, ANY_LANGUAGE, "excluded", reason)

    def refused(
        self,
        claim: UnresolvedRefRecord,
        origin: FileRow | None,
        language: str,
        decision: Decision,
    ) -> None:
        outcome = decision.outcome
        self._counts[
            (
                claim.repo_id,
                language,
                claim.edge_type,
                claim.target_kind,
                outcome,
                decision.subreason,
            )
        ] += 1
        if outcome not in OPEN_OUTCOMES or origin is None:
            return
        ref = _decision_ref(claim, origin)
        known = self._open.get(ref)
        if known is None:
            self._open[ref] = _Open(claim, origin, decision, language)
        else:
            known.count += 1

    def _count(self, claim: UnresolvedRefRecord, language: str, outcome: str, sub: str) -> None:
        self._counts[
            (claim.repo_id, language, claim.edge_type, claim.target_kind, outcome, sub)
        ] += 1

    # -- result -------------------------------------------------------------------------

    def plan(self) -> OutcomePlan:
        rows = tuple(
            OutcomeRow(
                repo_id=repo_id,
                language=language,
                edge_type=str(edge_type),
                target_kind=str(target_kind),
                outcome=outcome,
                subreason=sub,
                claims=claims,
            )
            for (repo_id, language, edge_type, target_kind, outcome, sub), claims in sorted(
                self._counts.items(), key=_row_order
            )
        )
        excluded: Counter[str] = Counter()
        considered = linked = interned_only = 0
        for row in rows:
            if row.outcome == "excluded":
                excluded[row.subreason] += row.claims
                continue
            considered += row.claims
            if row.outcome == LINKED:
                linked += row.claims
            elif row.target_kind in _INTERNED_KIND_CODES:
                interned_only += row.claims
        accounting = {
            ORDINARY_CONSIDERED: considered,
            ORDINARY_LINKED: linked,
            ORDINARY_NOT_LINKED: considered - linked,
            INTERNED_NODE_ONLY: interned_only,
        }
        decisions, truncated = self._decisions()
        accounting[OPEN_ROWS_TRUNCATED] = truncated
        return OutcomePlan(
            rows=rows,
            decisions=decisions,
            accounting=accounting,
            excluded=dict(sorted(excluded.items())),
        )

    def _decisions(self) -> tuple[tuple[LinkDecisionRow, ...], int]:
        by_key: dict[tuple[int, str, str], list[tuple[tuple[str, str, int, str, str], str]]] = (
            defaultdict(list)
        )
        for ref, site in self._open.items():
            claim = site.claim
            order = (
                site.origin.repo_slug,
                site.origin.rel_path,
                claim.line_start or 0,
                str(claim.edge_type),
                claim.target_key,
            )
            key = (claim.repo_id, site.decision.outcome, site.decision.subreason)
            by_key[key].append((order, ref))
        rows: list[LinkDecisionRow] = []
        truncated = 0
        for key in sorted(by_key):
            ordered = sorted(by_key[key])
            kept = ordered[:OPEN_ROWS_PER_KEY]
            truncated += sum(self._open[ref].count for _, ref in ordered[OPEN_ROWS_PER_KEY:])
            for _, ref in kept:
                site = self._open[ref]
                claim = site.claim
                evidence = {
                    "basis": site.decision.basis,
                    "claims": site.count,
                    "language": site.language,
                    "scope": site.decision.scope,
                    "subreason": site.decision.subreason,
                }
                rows.append(
                    LinkDecisionRow(
                        decision_ref=ref,
                        family=ORDINARY_FAMILY,
                        repo_id=claim.repo_id,
                        file_id=claim.file_id,
                        src_id=claim.src_id,
                        line_start=claim.line_start,
                        line_end=claim.line_end,
                        subject=_subject(claim),
                        decision=site.decision.outcome,
                        evidence_json=json.dumps(evidence, sort_keys=True),
                    )
                )
        return tuple(rows), truncated


def _row_order(
    item: tuple[tuple[int, str, EdgeType, EntityType, str, str], int],
) -> tuple[object, ...]:
    """Rows in the order of their codes, whatever order the claims arrived in."""
    repo_id, language, edge_type, target_kind, outcome, sub = item[0]
    return (repo_id, language, str(edge_type), str(target_kind), outcome, sub)


def _decision_ref(claim: UnresolvedRefRecord, origin: FileRow) -> str:
    """Stable across reindexing: built from where the claim is and what it says, never an id."""
    material = "\0".join(
        (
            ORDINARY_FAMILY,
            origin.repo_slug,
            origin.rel_path,
            str(claim.line_start or 0),
            str(claim.edge_type),
            str(claim.target_kind),
            claim.target_key,
        )
    )
    return hashlib.sha256(material.encode()).hexdigest()[:24]


def _subject(claim: UnresolvedRefRecord) -> str:
    """``EDGE:kind:spelling``. The spelling is kept only when it is a plain specifier or
    dotted name: a callee that carries arguments or text is not stored."""
    key = claim.target_key if _SAFE_SUBJECT.match(claim.target_key) else _SUBJECT_EXPRESSION
    return f"{claim.edge_type}:{claim.target_kind}:{key}"


def summarize(
    plan: OutcomePlan, accounting: Mapping[str, int], sources: Iterable[OutcomeRow] = ()
) -> OrdinarySummary:
    """The counts a sync reports, summed over repositories."""
    outcomes: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    for row in (*plan.rows, *sources):
        if row.outcome != "excluded":
            outcomes[row.outcome] += row.claims
            if row.outcome != LINKED:
                reasons[f"{row.outcome}:{row.subreason}"] += row.claims
    return OrdinarySummary(
        outcomes=dict(sorted(outcomes.items())),
        reasons=dict(sorted(reasons.items())),
        excluded=dict(plan.excluded),
        accounting=dict(sorted(accounting.items())),
    )


def finish_accounting(
    plan: OutcomePlan,
    *,
    standing: int,
    uses_rows: int,
    plan_unresolved: int,
    legacy_ordinary: int,
    graphql_unknown_role: int,
    planners: int,
    uses_remainder: int,
) -> dict[str, int]:
    """The full accounting once the commit knows the USES remainder.

    ``standing`` is every row of the claims table. The partition residual is what the ledger
    and the USES rows fail to cover; the legacy residual is what the legacy scalar holds that
    the four parts of it do not. Both are reported as measured.
    """
    accounting = dict(plan.accounting)
    excluded_total = sum(plan.excluded.values())
    accounting[STANDING_CLAIMS] = standing
    accounting[USES_ROWS] = uses_rows
    accounting[PARTITION_RESIDUAL] = standing - (
        accounting[ORDINARY_CONSIDERED] + excluded_total + uses_rows
    )
    for reason, count in plan.excluded.items():
        accounting[f"{EXCLUDED_PREFIX}{reason}"] = count
    accounting[f"{EXCLUDED_PREFIX}{USES_STAGE}"] = uses_rows
    legacy = plan_unresolved + uses_remainder
    accounting[LEGACY_UNRESOLVED] = legacy
    accounting[LEGACY_ORDINARY] = legacy_ordinary
    accounting[LEGACY_GRAPHQL_UNKNOWN_ROLE] = graphql_unknown_role
    accounting[LEGACY_PLANNERS] = planners
    accounting[LEGACY_USES_REMAINDER] = uses_remainder
    accounting[LEGACY_RESIDUAL] = legacy - (
        legacy_ordinary + graphql_unknown_role + planners + uses_remainder
    )
    return accounting
