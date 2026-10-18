"""The ``impact`` capability: seed resolution (and, built on it, the change-impact walk).

A seed names what changes. It has to name exactly one thing, because everything
after it is a statement about that one thing: a name two declarations share, a path
two repositories hold, is refused with the candidates listed, and nothing is followed.
The spellings accepted are the ones the other tools already hand back or a person
would write, tried in a fixed order so the same seed always means the same thing:

1. an exact entity key;
2. a repository-relative file path;
3. a declaration's name or qualified name, a configuration key's qualified name, or
   the ``env:`` / ``module:`` key a bare name spells. These rank alike: a bare
   ``DATABASE_URL`` that is both an environment variable and a constant's name is two
   different things, and the seed is refused with both listed rather than resolved
   to whichever spelling is tried first;
4. a repository-relative directory: every file beneath it changes together.

Scope is the context's, exactly as ``relationships`` applies it
(:func:`repoatlas.capabilities.service._resolvable_in`): an interned entity -- an
environment variable, a package, an HTTP operation -- belongs to no repository and is
in every scope; anything else must belong to a repository the call named.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from repoatlas.config.settings import DECLARATIONS_SLUG
from repoatlas.dependencies.normalize import PYPI
from repoatlas.graph.declared import REFUSED_AS, TargetKind, classify_dependency_target
from repoatlas.graph.deployment_env import DEPLOYMENT_ENV_EVIDENCE
from repoatlas.graph.fields import FIELD_USE
from repoatlas.graph.graphql_codegen import FAMILY as GENERATION
from repoatlas.graph.graphql_ownership import EVIDENCE as COMPOSITION
from repoatlas.graph.http_crossings import HTTP_CROSSING
from repoatlas.graph.impact import ImpactWalk, Node, Tier, WalkResult, is_test_path
from repoatlas.graph.keys import file_key, package_key
from repoatlas.graph.packages import CROSSING, ProviderTargets, stored_targets
from repoatlas.graph.persistence import EVIDENCE as QUERY_EVIDENCE
from repoatlas.graph.prisma_schema import CLIENT_EVIDENCE, MIGRATE_EVIDENCE
from repoatlas.graph.py_packages import PythonTargets, stored_python_targets
from repoatlas.graph.units import package_directories, unit_of
from repoatlas.graph.value_mappings import COMPARISON_EVIDENCE, MAPPING_EVIDENCE
from repoatlas.models.dependencies import DependencyRecord
from repoatlas.models.impact import (
    MAX_DECISIONS,
    MAX_HTTP_DECISIONS,
    MAX_SEED_CANDIDATES,
    ClientBinding,
    CodeGeneration,
    Composition,
    ConfigurationCrossing,
    Granularity,
    HttpCrossing,
    HubImpact,
    ImpactCandidate,
    ImpactDecision,
    ImpactOmitted,
    ImpactRole,
    ImpactSeed,
    ImpactStep,
    ImpactSummary,
    ImpactWork,
    Migration,
    PackageCrossing,
    PackageImpact,
    QueryBinding,
    SeedBasis,
    SeedCandidate,
    SeedStatus,
    TypedComparison,
    UnresolvedImpact,
    ValueMapping,
)
from repoatlas.models.parsing import FIELD, EdgeDirection, EdgeType, EntityType
from repoatlas.storage.protocols import (
    Confinement,
    DependencyStore,
    EntityIdentity,
    HttpRefusalNear,
    KnowledgeStore,
    NeighborRow,
    NeighborRule,
    SearchIndex,
)

#: The traversal policy's name and version, reported with every answer. 2: a use crosses
#: into another repository on a package link's evidence. 3: a schema definition reaches
#: what a configured generator generated from it (``GENERATES`` on codegen evidence).
#: 5: a deployment's effective setter crosses to the readers in the image its artifact join
#: proved it runs (``CONFIGURES`` on deployment_env evidence, DF3a). 6: a model field reaches
#: its exact users, then its model once (DF3b). 7: an answer lists the refused HTTP requests
#: where the walk went, each ``at`` or (hypothetically) ``toward``; none is ever followed (DF3c).
#: 8: a configuration crossing's deployments go with the path, and an HTTP link is followed under
#: them only through one of them; links not followed are counted, never listed (DF3d).
POLICY = "impact/8"

#: Entity types a change is a *key* change for: they name a value, not code.
_KEYS = frozenset(
    {
        EntityType.CONFIG,
        EntityType.ENV_VAR,
        EntityType.PACKAGE,
        EntityType.COMMAND,
        EntityType.HTTP_ROUTE,
        EntityType.HTTP_DEPLOYMENT,
        EntityType.HTTP_ENDPOINT,
    }
)

#: How many exact name matches are read to count an ambiguous seed's candidates.
_LOOKUP_LIMIT = 10_000

NOT_INDEXED = "nothing indexed is keyed, located or named by it"
OUT_OF_SCOPE = "it names something only outside the repositories this call is scoped to"
#: A resolved seed whose walk reached one entity under deployment contexts no single path per
#: entity represents together (DF3d): the answer names the setters that started them instead.
SEVERAL_DEPLOYMENTS = "several_deployments"


@dataclass(frozen=True, slots=True)
class Seed:
    """A seed resolved to one thing: an entity, or a directory of one repository."""

    model: ImpactSeed
    identity: EntityIdentity | None = None
    #: For a directory: its repository and path.
    directory: tuple[str, str] | None = None


def in_scope(identity: EntityIdentity, slugs: frozenset[str]) -> bool:
    return identity.repository_slug is None or identity.repository_slug in slugs


def _lines(identity: EntityIdentity) -> tuple[int, int] | None:
    if identity.line_start is None:
        return None
    return (identity.line_start, identity.line_end or identity.line_start)


def _candidate_order(identity: EntityIdentity) -> tuple[str, str, int, str]:
    return (
        identity.repository_slug or "",
        identity.path or "",
        identity.line_start or 0,
        identity.key,
    )


class SeedResolver:
    """Resolves one seed against the stores, within one context's repositories."""

    def __init__(
        self,
        knowledge: KnowledgeStore,
        search: SearchIndex,
        repositories: Mapping[str, int],
    ) -> None:
        self._knowledge = knowledge
        self._search = search
        #: Repository slug -> repository id, for the repositories in scope.
        self._repositories = dict(repositories)
        self._slugs = frozenset(repositories)
        self._directories: dict[str, tuple[str, ...]] | None = None

    def resolve(self, term: str) -> Seed:
        keyed = self._by_key([term])
        if keyed:
            return self._one(term, keyed, lambda _: SeedBasis.KEY)
        exact = self._by_name(term)
        paths = [i for i in exact if i.type is EntityType.FILE and i.qualified_name == term]
        if paths:
            return self._one(term, paths, lambda _: SeedBasis.PATH)
        spelled = (
            {}
            if ":" in term
            else {i.entity_id: i for i in self._by_key([f"env:{term}", f"module:{term}"])}
        )
        named = [
            i
            for i in exact
            if i.type is not EntityType.FILE
            and (i.entity_id in spelled or term in (i.name, i.qualified_name))
        ]
        both = _unique([*spelled.values(), *named])
        if both:
            return self._one(
                term,
                both,
                lambda i: SeedBasis.KEY if i.entity_id in spelled else SeedBasis.NAME,
            )
        directory = self._by_directory(term)
        if directory is not None:
            return directory
        return Seed(
            ImpactSeed(
                status=SeedStatus.UNRESOLVED,
                term=term,
                reason=OUT_OF_SCOPE if self._anywhere(term) else NOT_INDEXED,
            )
        )

    # -- lookups ---------------------------------------------------------------

    def _by_key(self, keys: Sequence[str]) -> list[EntityIdentity]:
        found = self._knowledge.get_entities_by_key(keys)
        return _unique([i for i in found if in_scope(i, self._slugs)])

    def _by_name(self, term: str) -> list[EntityIdentity]:
        hits = self._search.find_entities_by_name(
            [term], sorted(self._repositories.values()), _LOOKUP_LIMIT
        )
        identities = self._knowledge.keys_for_entities(sorted({hit.entity_id for hit in hits}))
        return _unique([i for i in identities.values() if in_scope(i, self._slugs)])

    def _by_directory(self, term: str) -> Seed | None:
        slugs = sorted(self._slugs)
        path = term.strip("/")
        named, colon, rest = term.partition(":")
        if colon and rest.endswith("/") and named in self._slugs:
            # a directory candidate as it is listed, `<repository>:<path>/`: asked for again
            slugs, path = [named], rest.strip("/")
        if not path or path == ".":
            return None
        found = [
            (slug, count)
            for slug in slugs
            if (count := self._knowledge.count_keys_with_prefix(file_key(slug, path) + "/")) > 0
        ]
        if len(found) > 1:
            return Seed(
                ImpactSeed(
                    status=SeedStatus.AMBIGUOUS,
                    term=term,
                    candidates=tuple(
                        SeedCandidate(entity=f"{slug}:{path}/", repository=slug, path=path)
                        for slug, _ in found[:MAX_SEED_CANDIDATES]
                    ),
                    total=len(found),
                )
            )
        if not found:
            return None
        slug, count = found[0]
        return Seed(
            ImpactSeed(
                status=SeedStatus.RESOLVED,
                term=term,
                entity=f"{slug}:{path}/",
                repository=slug,
                path=path,
                unit=self._unit(f"{slug}:{path}/", slug, path + "/"),
                basis=SeedBasis.DIRECTORY,
                granularity=Granularity.SUBTREE,
                members=count,
            ),
            directory=(slug, path),
        )

    def _anywhere(self, term: str) -> bool:
        """Whether the seed names something in a repository outside the scope."""
        spellings = [term] if ":" in term else [term, f"env:{term}", f"module:{term}"]
        if self._knowledge.get_entities_by_key(spellings):
            return True
        return bool(self._search.find_entities_by_name([term], None, 1))

    # -- one answer -------------------------------------------------------------

    def _one(
        self,
        term: str,
        found: Sequence[EntityIdentity],
        basis: Callable[[EntityIdentity], SeedBasis],
    ) -> Seed:
        ordered = sorted(found, key=_candidate_order)
        if len(ordered) > 1:
            return Seed(
                ImpactSeed(
                    status=SeedStatus.AMBIGUOUS,
                    term=term,
                    candidates=tuple(
                        SeedCandidate(
                            entity=i.key,
                            type=i.type,
                            repository=i.repository_slug,
                            path=i.path,
                            lines=_lines(i),
                        )
                        for i in ordered[:MAX_SEED_CANDIDATES]
                    ),
                    total=len(ordered),
                )
            )
        identity = ordered[0]
        granularity = _granularity(identity)
        return Seed(
            ImpactSeed(
                status=SeedStatus.RESOLVED,
                term=term,
                entity=identity.key,
                type=identity.type,
                repository=identity.repository_slug,
                path=identity.path,
                lines=_lines(identity),
                unit=self._unit(identity.key, identity.repository_slug, identity.path),
                basis=basis(identity),
                granularity=granularity,
                members=self._members(identity, granularity),
            ),
            identity=identity,
        )

    def _members(self, identity: EntityIdentity, granularity: Granularity) -> int | None:
        """A file's declarations; a declaration's or key's own members; a repository's files."""
        slug, path = identity.repository_slug, identity.path
        if identity.type is EntityType.REPOSITORY and identity.qualified_name:
            return self._knowledge.count_keys_with_prefix(file_key(identity.qualified_name, ""))
        if slug is None or path is None:
            return None
        # A model field is no member of its file or model (DF3b, ImpactWalk.members).
        if granularity is Granularity.FILE:
            prefix = f"sym:{slug}:{path}:"
            return self._knowledge.count_keys_with_prefix(
                prefix, exclude_types=[EntityType.MODULE], exclude_kinds=[FIELD]
            )
        if not identity.qualified_name:
            return None
        count = self._knowledge.count_keys_with_prefix(
            f"sym:{slug}:{path}:{identity.qualified_name}.", exclude_kinds=[FIELD]
        )
        return count or None

    def unit_of(self, key: str, slug: str | None, path: str | None) -> str:
        return self._unit(key, slug, path)

    def _unit(self, key: str, slug: str | None, path: str | None) -> str:
        if self._directories is None:
            manifests = self._knowledge.provided_manifests(sorted(self._slugs))
            self._directories = {
                repo: package_directories(paths) for repo, paths in manifests.items()
            }
        return unit_of(key, slug, path, self._directories)


def _granularity(identity: EntityIdentity) -> Granularity:
    if identity.type is EntityType.FILE:
        return Granularity.FILE
    if identity.type is EntityType.REPOSITORY:
        return Granularity.SUBTREE
    if identity.type in _KEYS:
        return Granularity.KEY
    return Granularity.SYMBOL


def _unique(found: Sequence[EntityIdentity]) -> list[EntityIdentity]:
    """One identity per entity row, in a fixed order."""
    by_id = {identity.entity_id: identity for identity in found}
    return sorted(by_id.values(), key=_candidate_order)


# -- the walk, and the answer built from it ------------------------------------------


def seed_nodes(
    seed: Seed, walk: ImpactWalk, home: int | None
) -> tuple[list[Node], list[tuple[Node, EntityIdentity]]]:
    """The walk's starting nodes and the members that change with them."""
    identity = seed.identity
    if identity is not None:
        node = walk.node_of(identity, home)
        node.climbs = walk.is_field(identity)
        slug, path = identity.repository_slug, identity.path
        prefixes: list[str] = []
        if identity.type is EntityType.REPOSITORY and identity.qualified_name:
            prefixes = [file_key(identity.qualified_name, ""), f"sym:{identity.qualified_name}:"]
        elif slug is not None and path is not None:
            if identity.type is EntityType.FILE:
                prefixes = [f"sym:{slug}:{path}:"]
            elif identity.qualified_name:
                prefixes = [f"sym:{slug}:{path}:{identity.qualified_name}."]
        members = [
            (node, member)
            for prefix in prefixes
            for member in walk.members(prefix, exclude_types=(EntityType.MODULE,))
            if member.entity_id != identity.entity_id
        ]
        return [node], members
    assert seed.directory is not None
    slug, path = seed.directory
    node = Node(
        entity_id=None,
        key=f"{slug}:{path}/",
        type=None,
        name=path,
        qualified_name=None,
        repo_id=home,
        path=path,
        lines=None,
        home=home,
    )
    members = [
        (node, member)
        for prefix in (file_key(slug, path) + "/", f"sym:{slug}:{path}/")
        for member in walk.members(prefix, exclude_types=(EntityType.MODULE,))
    ]
    return [node], members


_BASIS = {Tier.EXACT: "exact", Tier.CONTAINMENT: "containment", Tier.FILE: "file"}
#: How a field use's step says the field is reached (DF3b): what the declaration itself does.
_FIELD_ACCESS = {"parameter": "through a typed parameter", "construction": "by construction"}


def display(node: Node) -> str:
    if node.type is EntityType.FILE or (node.entity_id is None and node.path):
        return node.path or node.key
    if node.repo_id is None and node.path is None:
        return node.key
    return node.qualified_name or node.name


#: Section quotas: the most of each kind one answer lists; the rest are counted.
MAX_TESTS = 10
MAX_HUBS = 5
MAX_UNRESOLVED = 5
UNRESOLVED_READING = "imports it by name; no declaration-level use of it is indexed"


@dataclass(slots=True)
class Assembled:
    candidates: tuple[ImpactCandidate, ...]
    tests: tuple[ImpactCandidate, ...]
    hubs: tuple[HubImpact, ...]
    unresolved: tuple[UnresolvedImpact, ...]
    omitted: ImpactOmitted
    summary: ImpactSummary


def _round_robin(
    nodes: Sequence[Node], unit: Callable[[Node], str], repository: Callable[[Node], str | None]
) -> list[Node]:
    """Rank order, except that within each tier the units take turns.

    One large part of a monorepo -- whichever sorts first -- would otherwise fill an
    answer before any other part the change reaches appears. Where a tier spans more
    than one repository, the repositories take turns first and each one's units within
    its turns, so one repository with many units cannot fill it either.
    """
    order: list[Node] = []
    for tier in sorted({n.tier for n in nodes}):
        tiered = [n for n in nodes if n.tier == tier]
        if len({r for n in tiered if (r := repository(n)) is not None}) <= 1:
            queues: dict[str, list[Node]] = {}
            for node in tiered:
                queues.setdefault(unit(node), []).append(node)
            while queues:
                for name in list(queues):
                    order.append(queues[name].pop(0))
                    if not queues[name]:
                        del queues[name]
            continue
        # a shared node belongs to no repository: it takes its own turns, as its own unit
        turns: dict[str, dict[str, list[Node]]] = {}
        for node in tiered:
            own = turns.setdefault(repository(node) or unit(node), {})
            own.setdefault(unit(node), []).append(node)
        while turns:
            for name in list(turns):
                units = turns[name]
                first = next(iter(units))
                queue = units.pop(first)
                order.append(queue.pop(0))
                if queue:
                    units[first] = queue  # its next turn goes to the repository's next unit
                if not units:
                    del turns[name]
    return order


def _is_unresolved(node: Node) -> bool:
    """A file that imports a name and has no indexed declaration-level use of it."""
    return (
        node.type is EntityType.FILE
        and node.step is not None
        and node.step.family == "references"
        and not node.whole
    )


def assemble(
    result: WalkResult,
    walk: ImpactWalk,
    *,
    max_candidates: int,
    full: bool,
    directories: Mapping[str, Sequence[str]],
) -> Assembled:
    """The ranked, sectioned answer, closed under its paths, and what was left out, counted."""

    def unit(node: Node) -> str:
        return unit_of(node.key, walk.slug_of(node.repo_id), node.path, directories)

    ranked = sorted(result.reached.values(), key=lambda n: n.rank)
    selected: dict[int, Node] = {}
    cut: list[Node] = []

    def take(node: Node, room: int) -> bool:
        """Select ``node`` with every predecessor not yet selected, if they fit in ``room``."""
        need = [a for a in reversed(node.ancestors()) if a.entity_id not in selected]
        if len(need) + 1 > room:
            return False
        for item in [*need, node]:
            assert item.entity_id is not None
            selected[item.entity_id] = item
        return True

    def repository(node: Node) -> str | None:
        return walk.slug_of(node.repo_id) if node.repo_id is not None else None

    def reading(node: Node) -> str:
        """The step's reading; a crossing also says which repositories, and through what."""
        assert node.step is not None
        if node.step.row is None:
            return node.step.reading
        consumer = walk.slug_of(node.step.row.file_repo_id)
        crossing = node.step.resolution.get(CROSSING)
        if isinstance(crossing, dict):
            return (
                f"{node.step.reading}, {crossing.get('provider')} -> {consumer}"
                f" via {crossing.get('package')}"
            )
        configured = node.step.resolution.get(DEPLOYMENT_ENV_EVIDENCE)
        if isinstance(configured, dict):
            return f"{node.step.reading}, {configuration_reading(configured)}"
        request = node.step.resolution.get(HTTP_CROSSING)
        if isinstance(request, dict):
            return (
                f"{node.step.reading}, {request.get('provider')} -> {consumer}"
                f" via {request.get('method')} {request.get('url')}"
            ) + "".join(f"; {part}" for part in deployment_qualifiers(request))
        generated = node.step.resolution.get(GENERATION)
        if isinstance(generated, dict):
            return f"{node.step.reading}, by {generated.get('config')}"
        composed = node.step.resolution.get(COMPOSITION)
        if isinstance(composed, dict):
            return f"{COMPOSED}, as subgraph {composed.get('graph')} at {composed.get('url')}"
        migrated = node.step.resolution.get(MIGRATE_EVIDENCE)
        if isinstance(migrated, dict):
            return f"{node.step.reading}, by Prisma Migrate from {migrated.get('schema')}"
        mapped = node.step.resolution.get(MAPPING_EVIDENCE)
        if isinstance(mapped, dict):
            return f"{node.step.reading}, by {mapped.get('mapping')}"
        queried = node.step.resolution.get(QUERY_EVIDENCE)
        if isinstance(queried, dict):
            value = f" '{queried.get('value')}'" if queried.get("value") is not None else ""
            return (
                f"{node.step.reading}, by a query on {queried.get('table')}.{queried.get('column')}"
                f" {queried.get('op')}{value}"
            )
        bound = node.step.resolution.get(CLIENT_EVIDENCE)
        if isinstance(bound, dict):
            return f"{node.step.reading}, through {bound.get('specifier')}"
        compared = node.step.resolution.get(COMPARISON_EVIDENCE)
        if isinstance(compared, dict):
            field_name, value = compared.get("field"), compared.get("value")
            return f"{node.step.reading}, comparing {field_name} with '{value}'"
        used = node.step.resolution.get(FIELD_USE)
        if isinstance(used, dict):
            how = _FIELD_ACCESS.get(str(used.get("access")), str(used.get("access")))
            return f"{node.step.reading}, field {used.get('model')}.{used.get('field')} {how}"
        return node.step.reading

    tests = [n for n in ranked if is_test_path(n.path) and n.role is not ImpactRole.MEMBER]
    loose = [n for n in ranked if _is_unresolved(n) and not is_test_path(n.path)]
    others = [
        n
        for n in ranked
        if n.role is not ImpactRole.MEMBER and not is_test_path(n.path) and not _is_unresolved(n)
    ]
    for node in _round_robin(others, unit, repository):
        if not take(node, max_candidates - len(selected)):
            cut.append(node)
    chosen_tests: list[Node] = []
    for node in _round_robin(tests, unit, repository):
        # A test is listed in its own section; the predecessors it needs are candidates
        # and must fit in what the candidate cap has left.
        room = max_candidates - (len(selected) - len(chosen_tests)) + 1
        if len(chosen_tests) < MAX_TESTS and take(node, room):
            chosen_tests.append(node)
        else:
            cut.append(node)
    listed_loose = [n for n in loose if n.entity_id not in selected][:MAX_UNRESOLVED]
    cut.extend(n for n in loose if n.entity_id not in selected and n not in listed_loose)

    ordered = sorted(selected.values(), key=lambda n: n.rank)
    ids = {node.entity_id: index for index, node in enumerate(ordered, start=1)}

    def path_of(node: Node) -> list[Node]:
        seed = node
        while seed.parent is not None:
            seed = seed.parent
        return [seed, *reversed(node.ancestors()), node]

    def candidate(node: Node) -> ImpactCandidate:
        nodes = path_of(node)
        boundaries: list[str] = []
        last: str | None = None
        for item in nodes:
            if item.entity_id is None:
                here: str | None = f"{walk.slug_of(item.repo_id)}:{item.path}/"
            elif item.repo_id is None:
                here = None  # a shared node belongs to no unit
            else:
                here = unit(item)
            if here is None:
                continue
            if last is not None and here != last:
                boundaries.append(f"{last} -> {here}")
            last = here
        step = node.step
        assert step is not None
        row = step.row
        resolution = step.resolution
        at, lines = None, None
        if row is not None and row.file_path is not None:
            at = f"{walk.slug_of(row.file_repo_id)}/{row.file_path}"
            if row.line_start is not None:
                lines = (row.line_start, row.line_end or row.line_start)
        crossing = _crossing(resolution, walk.slug_of(row.file_repo_id) if row else None, row)
        http = http_crossing(resolution, walk.slug_of(row.file_repo_id) if row else None)
        configuration = configuration_crossing(resolution)
        generation = code_generation(resolution)
        composition = graph_composition(resolution)
        evidence = link_evidence(resolution)
        via = tuple(str(v) for v in _list(resolution.get("via")))
        if crossing is not None:
            via = tuple(f"{crossing.from_repository}/{v}" for v in via)
        parent = node.parent
        return ImpactCandidate(
            id=ids[node.entity_id],
            entity=node.key,
            type=node.type or EntityType.FILE,
            name=node.name,
            repository=walk.slug_of(node.repo_id),
            path=node.path,
            lines=node.lines,
            unit=unit(node),
            role=node.role,
            distance=node.distance,
            from_=ids.get(parent.entity_id, 0) if parent is not None and not parent.seed else 0,
            step=ImpactStep(
                edge=step.edge_type,
                derived=step.derived,
                # One family follows both kinds of GENERATES evidence; a composition reads
                # as what it is.
                reading=COMPOSED if composition is not None else step.reading,
                use=_text(resolution.get("use")),
                at=at,
                lines=lines,
                parser=row.parser if row is not None else None,
                syntax=_text(resolution.get("syntax")),
                via=via,
                via_names=tuple(str(v) for v in _list(resolution.get("via_names"))),
                crossing=crossing,
                http=http,
                configuration=configuration,
                generation=generation,
                composition=composition,
                migration=evidence.migration,
                client=evidence.client,
                query=evidence.query,
                mapping=evidence.mapping,
                comparison=evidence.comparison,
                resolution=(resolution or None) if full else None,
            ),
            boundaries=tuple(boundaries),
            basis=_BASIS[node.tier],
            chain=" -> ".join(
                [display(nodes[0])]
                + [f"{display(n)} ({reading(n)})" for n in nodes[1:] if n.step is not None]
            ),
            alt_paths=tuple(
                sorted(ids[a.entity_id] for a in node.alternatives if a.entity_id in ids)
            )
            if full
            else (),
        )

    chosen_test_ids = {n.entity_id for n in chosen_tests}
    candidates = tuple(candidate(n) for n in ordered if n.entity_id not in chosen_test_ids)
    listed_tests = tuple(candidate(n) for n in ordered if n.entity_id in chosen_test_ids)

    hubs = []
    for node in ordered:
        hub = result.hubs.get(node.entity_id or -1)
        if hub is None:
            continue
        if len(hubs) == MAX_HUBS:
            break
        hubs.append(
            HubImpact(
                candidate=ids[node.entity_id],
                entity=node.key,
                fanout=hub.fanout,
                units=dict(sorted(hub.units.items())),
                units_partial=hub.units_partial,
                repositories=dict(sorted(hub.repositories.items())),
                shown=hub.shown,
                collapsed=hub.collapsed,
            )
        )
    unresolved = tuple(
        UnresolvedImpact(
            entity=node.key,
            reason=f"{UNRESOLVED_READING}: {display(node.parent) if node.parent else ''}",
            at=f"{walk.slug_of(node.step.row.file_repo_id)}/{node.step.row.file_path}"
            if node.step is not None and node.step.row is not None and node.step.row.file_path
            else None,
            lines=(node.step.row.line_start, node.step.row.line_end or node.step.row.line_start)
            if node.step is not None and node.step.row is not None and node.step.row.line_start
            else None,
        )
        for node in listed_loose
    )

    by_reason: dict[str, int] = {}
    by_unit: dict[str, int] = {}
    if cut:
        by_reason["budget"] = len(cut)
        for node in cut:
            by_unit[unit(node)] = by_unit.get(unit(node), 0) + 1
    collapsed = sum(h.collapsed for h in result.hubs.values())
    if collapsed:
        by_reason["hub"] = collapsed
    if result.depth_cut:
        by_reason["depth"] = result.depth_cut
    if result.unexpanded or result.members_cut:
        by_reason["work"] = result.unexpanded + (1 if result.members_cut else 0)
    # Links a deployment context kept from being followed (DF3d): counted under their own
    # reasons, never as a cut, and never in ``by_unit`` or ``complete``.
    by_reason.update(result.skipped)
    returned = [*candidates, *listed_tests]
    summary = ImpactSummary(
        reached=len(result.reached),
        returned=len(returned),
        units=len({c.unit for c in returned if c.unit}),
        cross_unit=sum(1 for c in returned if c.boundaries),
        complete=result.complete,
        work=ImpactWork(
            expanded=result.work.expanded,
            rows=result.work.rows,
            queries=result.work.queries,
            exhausted=result.work.exhausted,
        ),
        policy=POLICY,
    )
    return Assembled(
        candidates=candidates,
        tests=listed_tests,
        hubs=tuple(hubs),
        unresolved=unresolved,
        omitted=ImpactOmitted(by_reason=by_reason, by_unit=dict(sorted(by_unit.items()))),
        summary=summary,
    )


MAX_PACKAGES = 8
#: The most exact consumers one package entry points at.
MAX_CONSUMERS = 3
#: Declarations that name another package, or a location, rather than the provider's.
OUTSIDE_SCOPE = "outside scope"


@dataclass(slots=True)
class _Crossed:
    """Every reached declaration a package crossing leads to, for one dependent."""

    provided_by: str
    provider: str
    consumer: str
    declared_manifest: str
    reached: set[int] = field(default_factory=set)
    subpaths: set[str] = field(default_factory=set)


def _crossed(result: WalkResult, walk: ImpactWalk) -> dict[tuple[str, str], _Crossed]:
    """What the walk crossed, by (package, declaring manifest line): read from its own steps."""
    found: dict[tuple[str, str], _Crossed] = {}
    for node in result.reached.values():
        step = node.step
        if step is None or step.row is None or node.entity_id is None:
            continue
        record = step.resolution.get(CROSSING)
        if not isinstance(record, dict):
            continue
        consumer = walk.slug_of(step.row.file_repo_id) or ""
        declared = f"{consumer}/{record.get('declared_at')}"
        provider = str(record.get("provider"))
        entry = found.setdefault(
            (str(record.get("package")), declared),
            _Crossed(
                provided_by=f"{provider}/{record.get('provided_at')}",
                provider=provider,
                consumer=consumer,
                declared_manifest=str(record.get("declared_at")).rpartition(":")[0],
            ),
        )
        entry.reached.add(node.entity_id)
        entry.subpaths.add(str(record.get("subpath") or ""))
    return found


def package_dependents(
    seed: Seed,
    knowledge: KnowledgeStore,
    dependencies: DependencyStore,
    walk: ImpactWalk,
    result: WalkResult,
    returned: Sequence[ImpactCandidate],
    directories: Mapping[str, Sequence[str]],
    *,
    scoped: bool = False,
    roots: Mapping[int, str] | None = None,
) -> tuple[tuple[PackageImpact, ...], int]:
    """Units and repositories depending on a package the change reaches; coarse by construction.

    Two kinds of entry. The seed's own package -- the one its nearest providing manifest
    names -- with every unit or repository that declares it. A unit of the seed's own
    repository depends on it when a manifest there declares it and no other manifest in
    that repository provides the same name. Another repository depends on it only when
    the linker's repository-to-repository edge -- drawn only when exactly one other
    managed repository provides the name -- is asserted by the same manifest and line,
    and only when that repository neither provides the name itself nor declares it as
    anything but that package, read as a package link reads it: an alias of another, a
    remote source, or a path or workspace leading to another provider. A shared name
    alone is never enough. And every package an exact path crossed, whichever it is.

    Across repositories an entry counts only what crossed into the dependent through that
    very package and declaration -- never whatever else was found there -- and points at
    the candidates that did. A dependent with none says its exact consumer is unresolved,
    and counts the declarations there that use the package's module as a whole. Returns
    the entries listed and how many more there were.
    """
    crossed = _crossed(result, walk)
    found: dict[tuple[str, str], PackageImpact] = {}
    slug, path = seed.model.repository, seed.model.path
    repo_id = walk.repo_id(slug) if slug else None
    if slug is not None and path is not None and repo_id is not None and walk.work.query():
        for entry in _seed_package(
            knowledge, dependencies, walk, slug, path, repo_id, returned, directories, roots or {}
        ):
            found.setdefault((entry.package, entry.declared_at), entry)
    for (package, declared), crossing in crossed.items():
        if (package, declared) in found:
            continue
        dependent = unit_of(
            f"{crossing.consumer}:{crossing.declared_manifest}",
            crossing.consumer,
            crossing.declared_manifest,
            directories,
        )
        found[(package, declared)] = PackageImpact(
            package=package,
            provided_by=crossing.provided_by,
            dependent=dependent,
            declared_at=declared,
            crosses_repository=True,
            exact_consumers=len(crossing.reached),
        )
    listed = [
        _exact(entry, crossed, returned, knowledge, walk, scoped)
        if entry.crosses_repository
        else entry
        for entry in found.values()
    ]
    listed.sort(
        key=lambda p: (not p.consumers, p.crosses_repository, p.dependent, p.declared_at, p.package)
    )
    return tuple(listed[:MAX_PACKAGES]), max(0, len(listed) - MAX_PACKAGES)


def _exact(
    entry: PackageImpact,
    crossed: Mapping[tuple[str, str], _Crossed],
    returned: Sequence[ImpactCandidate],
    knowledge: KnowledgeStore,
    walk: ImpactWalk,
    scoped: bool,
) -> PackageImpact:
    """A cross-repository entry's exact consumers, from the crossings the walk took."""
    crossing = crossed.get((entry.package, entry.declared_at))
    reached = crossing.reached if crossing is not None else set()
    consumers = tuple(
        sorted(
            c.id
            for c in returned
            if c.step.crossing is not None
            and c.step.crossing.package == entry.package
            and c.step.crossing.declared_by == entry.declared_at
        )[:MAX_CONSUMERS]
    )
    provider = walk.repository_at(entry.provided_by)
    dependent = walk.repository_at(entry.declared_at)
    unresolved = None
    if provider is not None and dependent is not None and walk.work.query():
        unresolved = knowledge.count_whole_module_uses(
            entry.package, provider, dependent, exclude_ids=sorted(reached)
        )
    subpaths = crossing.subpaths if crossing is not None else set()
    exact: int | str = len(reached)
    if not reached:
        exact = OUTSIDE_SCOPE if scoped else "unresolved"
    return entry.model_copy(
        update={
            "exact_consumers": exact,
            "consumers": consumers,
            "unresolved_consumers": unresolved or None,
            "subpaths": tuple(sorted(subpaths)) if subpaths - {""} else (),
        }
    )


def _seed_package(
    knowledge: KnowledgeStore,
    dependencies: DependencyStore,
    walk: ImpactWalk,
    slug: str,
    path: str,
    repo_id: int,
    returned: Sequence[ImpactCandidate],
    directories: Mapping[str, Sequence[str]],
    roots: Mapping[int, str],
) -> list[PackageImpact]:
    """The seed's own package, and every unit or repository declaring it (see above)."""
    provided = [r for r in dependencies.list_for_repos([repo_id]) if r.role == "provided"]
    places = knowledge.file_locations(sorted({r.file_id for r in provided}))
    within = [
        (record, places[record.file_id][1])
        for record in provided
        if record.file_id in places
        and _contains(places[record.file_id][1].rpartition("/")[0], path)
    ]
    if not within:
        return []
    nearest = max(manifest.rpartition("/")[0] for _, manifest in within)
    repo_key = knowledge.get_entities_by_key([f"repo:{slug}"])
    evidenced: set[tuple[int | None, str | None, int | None]] = set()
    if len(repo_key) == 1:
        crossing = Confinement(crossing=frozenset({EdgeType.DEPENDS_ON}))
        for row in walk.page(repo_key[0].entity_id, _DEPENDS_IN, crossing, limit=MAX_DECLARATIONS):
            evidenced.add((row.far_repo_id, row.file_path, row.line_start))
    providing = next(m for _, m in within if m.rpartition("/")[0] == nearest)
    providing_unit = unit_of(f"{slug}:{providing}", slug, providing, directories)
    targets = _Targets(knowledge, dependencies, walk, roots)
    found: list[PackageImpact] = []
    for record, manifest in sorted(within, key=lambda item: (item[1], item[0].normalized_name)):
        if manifest.rpartition("/")[0] != nearest:
            continue
        name = (record.ecosystem, record.normalized_name)
        unique_here = sum(1 for r in provided if (r.ecosystem, r.normalized_name) == name) == 1
        package = knowledge.get_entities_by_key([package_key(*name)])
        if len(package) != 1:
            continue
        rows = walk.page(package[0].entity_id, _DEPENDS_IN, Confinement(), limit=MAX_DECLARATIONS)
        others = sorted({r.far_repo_id for r in rows if r.far_repo_id not in (None, repo_id)})
        declared_elsewhere = _declarations(dependencies, walk, others)
        for row in rows:
            other = row.far_repo_id
            crosses = other != repo_id
            if crosses and (other, row.file_path, row.line_start) not in evidenced:
                continue
            if crosses and not _declares_the_provider(
                declared_elsewhere, other, row, name, (repo_id, nearest), targets
            ):
                continue
            if not crosses and (not unique_here or row.file_path == manifest):
                continue
            dependent_slug = walk.slug_of(other)
            dependent = unit_of(
                f"{dependent_slug}:{row.file_path}", dependent_slug, row.file_path, directories
            )
            if not crosses and dependent == providing_unit:
                continue
            # across repositories, what crossed into the dependent is counted afterwards
            exact = (
                0
                if crosses
                else sum(
                    1 for c in returned if c.repository == dependent_slug and c.unit == dependent
                )
            )
            found.append(
                PackageImpact(
                    package=f"{record.ecosystem}:{record.name}",
                    provided_by=f"{slug}/{manifest}:{record.line_start}",
                    dependent=dependent,
                    declared_at=f"{dependent_slug}/{row.file_path}:{row.line_start}",
                    crosses_repository=crosses,
                    exact_consumers=exact if exact else "unresolved",
                )
            )
    return found


def _declarations(
    dependencies: DependencyStore, walk: ImpactWalk, repo_ids: Sequence[int]
) -> list[DependencyRecord]:
    """The dependency rows of the repositories that declare a package, in one charged read."""
    if not repo_ids or not walk.work.query():
        return []
    return dependencies.list_for_repos(list(repo_ids))


def _declares_the_provider(
    records: Sequence[DependencyRecord],
    repo_id: int | None,
    row: NeighborRow,
    name: tuple[str, str],
    provider: tuple[int, str],
    targets: _Targets,
) -> bool:
    """Whether a repository's declaration is of the package the seed's repository provides.

    Not when that repository provides the same name itself (two providers: which one it
    uses is not ours to pick), nor when its manifest declares it as anything but that
    package. Every row the manifest declares the name with is read as the target it
    names (:mod:`repoatlas.graph.declared`), as a package link reads it: an alias of
    another package, a remote source or an unreadable form is not it, and a path or a
    workspace must lead to ``provider``, the seed's own providing manifest (repository,
    directory). A PyPI declaration is read as a Python package link reads it
    (:func:`_python_declares`).
    """
    mine = [r for r in records if r.repo_id == repo_id and (r.ecosystem, r.normalized_name) == name]
    if repo_id is None or row.file_path is None or any(r.role == "provided" for r in mine):
        return False
    if name[0] == PYPI:
        return _python_declares(
            mine, repo_id, row.file_id, row.file_path, name[1], provider, targets
        )
    for record in mine:
        if record.role != "declared" or record.file_id != row.file_id:
            continue
        target = classify_dependency_target(record.constraint or "", name[1])
        if target.kind in REFUSED_AS:
            return False
        if target.kind not in (TargetKind.PATH, TargetKind.WORKSPACE):
            continue
        found = targets()
        if found is None:
            return False
        pinned = (
            found.by_path(repo_id, row.file_path, target.path or "", name[1])
            if target.kind is TargetKind.PATH
            else found.by_workspace(repo_id, row.file_path, name[1])
        )
        if isinstance(pinned, str) or (pinned[0].repo_id, pinned[0].directory) != provider:
            return False
    return True


def _python_declares(
    records: Sequence[DependencyRecord],
    repo_id: int,
    file_id: int | None,
    manifest: str,
    wanted: str,
    provider: tuple[int, str],
    targets: _Targets,
) -> bool:
    """:func:`_declares_the_provider` for a PyPI declaration, read as a Python package link
    reads it (:mod:`repoatlas.graph.py_packages`).

    A direct reference (``name @ ...``) or a name Poetry's tables declare is not read; a
    ``pyproject.toml``'s uv source -- its own, or its workspace root's -- must lead to
    ``provider``; a version names the package.
    """
    constraints = [
        r.constraint or "" for r in records if r.role == "declared" and r.file_id == file_id
    ]
    if PurePosixPath(manifest).name != "pyproject.toml":
        return not any(c.strip().startswith("@") for c in constraints)
    found = targets.python()
    if found is None:
        return False
    directory = manifest.rpartition("/")[0]
    if found.unread(repo_id, directory, wanted, constraints):
        return False
    pinned = found.pinned(repo_id, directory, wanted)
    if pinned is None:
        return True
    return not isinstance(pinned, str) and (pinned[0].repo_id, pinned[0].directory) == provider


class _Targets:
    """Where paths, workspaces and uv sources lead -- npm's (:class:`ProviderTargets`) and
    Python's (:class:`PythonTargets`) -- read once when first asked.

    Only a declaration that pins its provider, or a Python project's, asks, so an answer
    with none reads nothing more; the read is charged, and ``None`` when the budget
    refuses it.
    """

    def __init__(
        self,
        knowledge: KnowledgeStore,
        dependencies: DependencyStore,
        walk: ImpactWalk,
        roots: Mapping[int, str],
    ) -> None:
        self._knowledge = knowledge
        self._dependencies = dependencies
        self._walk = walk
        self._roots = roots
        self._built: tuple[ProviderTargets, PythonTargets] | None = None
        self._asked = False

    def __call__(self) -> ProviderTargets | None:
        built = self._read()
        return None if built is None else built[0]

    def python(self) -> PythonTargets | None:
        built = self._read()
        return None if built is None else built[1]

    def _read(self) -> tuple[ProviderTargets, PythonTargets] | None:
        if not self._asked:
            self._asked = True
            if self._walk.work.query():
                records = self._dependencies.list_for_repos(None)
                facts = self._knowledge.list_module_facts()
                files = sorted({r.file_id for r in records} | {c.file_id for c in facts})
                places = self._knowledge.file_locations(files)
                paths = {file_id: path for file_id, (_, path) in places.items()}
                slugs = {i: s for i in self._roots if (s := self._walk.slug_of(i)) is not None}
                self._built = (
                    stored_targets(records, facts, paths, self._roots, slugs),
                    stored_python_targets(records, facts, paths, self._roots, slugs),
                )
        return self._built


#: The most package declarations one step reads.
MAX_DECLARATIONS = 500
_DEPENDS_IN = NeighborRule(edge_types=(EdgeType.DEPENDS_ON,), direction=EdgeDirection.IN)


def _contains(directory: str, path: str) -> bool:
    return directory == "" or path == directory or path.startswith(directory + "/")


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _crossing(
    resolution: Mapping[str, object], consumer: str | None, row: NeighborRow | None
) -> PackageCrossing | None:
    """A step's package evidence, each part spelled where it can be read, or ``None``."""
    if row is None:
        return None
    return package_crossing(resolution, consumer, row.file_path)


def package_crossing(
    resolution: Mapping[str, object], consumer: str | None, asserting_path: str | None
) -> PackageCrossing | None:
    """An edge's package evidence, each part spelled where it can be read, or ``None``.

    ``consumer`` is the repository of the file asserting the edge and
    ``asserting_path`` that file, which is where the import it rests on was read.
    Public because an impact step and a topology edge describe the same stored
    fact, and two spellings of one fact are how they come to disagree.
    """
    record = resolution.get(CROSSING)
    if not isinstance(record, dict) or consumer is None:
        return None
    provider = str(record.get("provider") or "")
    mapped = record.get("entry_mapped")
    imported_at = record.get("imported_at")
    return PackageCrossing(
        package=str(record.get("package")),
        subpath=_text(record.get("subpath")),
        from_repository=provider,
        to_repository=consumer,
        provided_by=f"{provider}/{record.get('provided_at')}",
        entry=f"{provider}/{record.get('entry')}",
        entry_fields=_text(record.get("entry_fields")),
        entry_mapped=f"{mapped.get('from')} -> {mapped.get('to')} ({mapped.get('by')})"
        if isinstance(mapped, dict)
        else None,
        declared_by=f"{consumer}/{record.get('declared_at')}",
        constraint=_text(record.get("constraint")),
        imported_at=f"{consumer}/{asserting_path}:{imported_at}"
        if isinstance(imported_at, int) and asserting_path
        else None,
        declared_as=_text(resolution.get("name")),
        imported_as=_text(resolution.get("imported")),
        local=_text(resolution.get("local")),
    )


def code_generation(resolution: Mapping[str, object]) -> CodeGeneration | None:
    """An edge's configured-generation evidence, or ``None``."""
    record = resolution.get(GENERATION)
    if not isinstance(record, dict):
        return None
    value = record.get("member_value")
    return CodeGeneration(
        config=str(record.get("config")),
        definition=str(record.get("schema")),
        output=str(record.get("output")),
        plugin=str(record.get("plugin")),
        member_value=str(value) if value is not None else None,
        decision_ref=str(record.get("decision_ref")),
    )


def decisions_near(
    knowledge: KnowledgeStore, entity_keys: Sequence[str]
) -> tuple[tuple[ImpactDecision, ...], dict[str, int]]:
    """The link decisions other than a link made (``linked``, ``resolved``) made where the
    walk went -- at the seed or a
    returned candidate, or in a file one is in -- at most :data:`MAX_DECISIONS`, the rest
    counted by family. A decision is listed because the walk touched where it was made,
    never because a name suggests it is relevant."""
    rows = knowledge.link_decisions_near([key for key in entity_keys if key])
    listed = tuple(
        ImpactDecision(
            family=family,
            subject=subject,
            decision=decision,
            at=f"{place}:{line}" if line is not None else place,
            evidence=json.loads(evidence),
        )
        for family, subject, decision, place, evidence, line in rows[:MAX_DECISIONS]
    )
    omitted: dict[str, int] = {}
    for family, *_ in rows[MAX_DECISIONS:]:
        omitted[family] = omitted.get(family, 0) + 1
    return listed, omitted


#: How each relation of a refused HTTP request reads (DF3c). ``toward`` is a hypothetical.
_HTTP_READINGS = {
    "at": "a request made where the walk went; it is not linked",
    "toward": "hypothetical: not linked; if it were, the request would reach this handler",
}
#: The hop statuses a chain establishes; the first other one is why a request is refused.
_ESTABLISHED = frozenset({"proven", "declared"})


def http_decisions_near(
    knowledge: KnowledgeStore,
    entity_keys: Sequence[str],
    *,
    slug_of: Callable[[int | None], str | None],
    scope: frozenset[str] | None = None,
) -> tuple[tuple[ImpactDecision, ...], int]:
    """The refused HTTP requests where the walk went (DF3c), at most :data:`MAX_HTTP_DECISIONS`,
    and how many more there were.

    ``at``: the request is made by a declaration the answer reached, or in a file whose own
    entity it reached. ``toward``: the request is not linked, and its stored ``would_reach``
    names a handler the answer reached -- a hypothetical, never a step, a candidate or a
    connection. A refusal both is listed once, as ``at``. ``at`` first, then ``toward``, each
    by place and decision ref. Under a repository scope (``scope``) only requests made inside it
    are listed, and a ``toward``'s source places outside it are left out: no hypothetical
    target crosses the boundary.
    """
    rows = [
        row
        for row in knowledge.http_decisions_near([key for key in entity_keys if key])
        if scope is None or row.repository in scope
    ]
    at = {row.decision_ref for row in rows if row.relation == "at"}
    chosen = [row for row in rows if row.relation == "at" or row.decision_ref not in at]

    def order(row: HttpRefusalNear) -> tuple[bool, str, int, str]:
        return (
            row.relation != "at",
            f"{row.repository}/{row.path}",
            row.line or 0,
            row.decision_ref,
        )

    chosen.sort(key=order)
    listed = tuple(_http_decision(row, slug_of, scope) for row in chosen[:MAX_HTTP_DECISIONS])
    return listed, max(0, len(chosen) - MAX_HTTP_DECISIONS)


def _http_decision(
    row: HttpRefusalNear, slug_of: Callable[[int | None], str | None], scope: frozenset[str] | None
) -> ImpactDecision:
    chain = json.loads(row.chain_json or "[]")
    refused = next(
        (hop for hop in chain if isinstance(hop, dict) and hop.get("status") not in _ESTABLISHED),
        {},
    )
    evidence: dict[str, object] = {
        "decision_ref": row.decision_ref,
        "relation": row.relation,
        "reading": _HTTP_READINGS[row.relation],
        "request": f"{row.method} {row.url}",
        "hop": refused.get("kind"),
        "reason": refused.get("reason") or refused.get("status"),
    }
    if row.relation == "at":
        evidence["reached"] = row.reached
    else:
        evidence["hypothetical"] = True
        evidence["would_reach"] = [dict(handler) for handler in row.handlers]
        source = next(
            (hop for hop in chain if isinstance(hop, dict) and hop.get("kind") == "source_binding"),
            {},
        )
        places = []
        for place in source.get("provenance") or []:
            slug = slug_of(place.get("repo_id"))
            if slug is None or (scope is not None and slug not in scope):
                continue
            line = place.get("line_start")
            places.append(f"{slug}/{place.get('path')}" + (f":{line}" if line is not None else ""))
        evidence["source"] = places
        evidence["source_status"] = source.get("status")
    return ImpactDecision(
        family="http",
        subject=f"{row.repository}:{row.method} {row.url}",
        decision=row.decision,
        at=f"{row.repository}/{row.path}" + (f":{row.line}" if row.line is not None else ""),
        evidence=evidence,
        relation="at" if row.relation == "at" else "toward",
    )


@dataclass(frozen=True, slots=True)
class LinkEvidence:
    """An edge's schema-impact evidence beyond generation and composition, as typed step
    fields: a migration, a client binding, a query, a mapping or a comparison."""

    migration: Migration | None = None
    client: ClientBinding | None = None
    query: QueryBinding | None = None
    mapping: ValueMapping | None = None
    comparison: TypedComparison | None = None


def link_evidence(resolution: Mapping[str, object]) -> LinkEvidence:
    """The :class:`LinkEvidence` an edge's resolution records."""
    found: dict[str, object] = {}
    record = resolution.get(MIGRATE_EVIDENCE)
    if isinstance(record, dict):
        found["migration"] = Migration(
            project=str(record.get("schema")),
            migration=str(record.get("migration")),
            type=str(record.get("type")),
            decision_ref=str(record.get("decision_ref")),
        )
    record = resolution.get(CLIENT_EVIDENCE)
    if isinstance(record, dict):
        found["client"] = ClientBinding(
            project=str(record.get("schema")),
            specifier=str(record.get("specifier")),
            decision_ref=str(record.get("decision_ref")),
        )
    record = resolution.get(QUERY_EVIDENCE)
    if isinstance(record, dict):
        found["query"] = QueryBinding(
            database=str(record.get("database")),
            table=str(record.get("table")),
            column=str(record.get("column")),
            op=str(record.get("op")),
            value=None if record.get("value") is None else str(record.get("value")),
            decision_ref=str(record.get("decision_ref")),
        )
    record = resolution.get(MAPPING_EVIDENCE)
    if isinstance(record, dict):
        found["mapping"] = ValueMapping(
            mapping=str(record.get("mapping")), decision_ref=str(record.get("decision_ref"))
        )
    record = resolution.get(COMPARISON_EVIDENCE)
    if isinstance(record, dict):
        found["comparison"] = TypedComparison(
            model=str(record.get("type")),
            field=str(record.get("field")),
            enum=str(record.get("enum")),
            value=str(record.get("value")),
        )
    return LinkEvidence(**found)  # type: ignore[arg-type]


#: How a step from a provider definition to a supergraph copy of it reads.
COMPOSED = "is composed from"


def graph_composition(resolution: Mapping[str, object]) -> Composition | None:
    """An edge's federation-composition evidence, or ``None``."""
    record = resolution.get(COMPOSITION)
    if not isinstance(record, dict):
        return None
    return Composition(
        graph=str(record.get("graph")),
        url=str(record.get("url")),
        identity=str(record.get("identity")),
        provider=str(record.get("provider")),
        decision_ref=str(record.get("decision_ref")),
    )


#: How a CI-established link's image versions are tied, in the step's words (DF2).
_RELATIONS = {
    "exact": "image tag configured identically on both sides",
    "symbolic": "image version from the same CI run's variable",
    "unknown": "image version not established",
}


def deployment_qualifiers(record: Mapping[str, object]) -> list[str]:
    """What a CI-established link's step adds to its reading: who deploys and builds it,
    any basis but source, and the version relation. Nothing for any other link."""
    deployed = record.get("deployed_by")
    if not isinstance(deployed, str) or not deployed:
        return []
    found = [
        f"deployment configured in {deployed}"
        + (f" ({record['binding_basis']})" if record.get("binding_basis") != "source" else "")
    ]
    built = record.get("built_by")
    if isinstance(built, str) and built:
        found.append(
            f"image built in {built}"
            + (f" ({record['build_basis']})" if record.get("build_basis") != "source" else "")
        )
    relation = _RELATIONS.get(str(record.get("version_relation", "")))
    if relation is not None:
        found.append(relation)
    return found


#: Where a setter is, in a step's words (DF3a).
_SETTERS = {
    "values-file": "values file",
    "set": "--set on the release",
    "configmap-key": "ConfigMap key",
    "env-from": "ConfigMap, through envFrom",
    "compose-environment": "compose environment",
}
#: What a setter's value is known to be, in a step's words: never the value itself (D3-4).
_VALUES = {
    "symbolic": "value from a variable, not established",
    "withheld": "value withheld",
}


def configuration_reading(record: Mapping[str, object]) -> str:
    """How a configuration crossing reads: from the deployment's repository to the builder's
    through the variable, with where it is set, the deployment, any basis but source, the image
    version relation, and what is known of the value. Configuration, never what runs."""
    deployed, built = record.get("deployed_by"), record.get("built_by")
    route = f"{deployed} -> {built}" if deployed != built else f"in {deployed}"
    parts = [_SETTERS.get(str(record.get("setter_kind")), str(record.get("setter_kind")))]
    listed = record.get("deployments")
    if isinstance(listed, list) and len(listed) > 1:
        # One setter of several deployments: every one is named, and nothing the record
        # states for its first deployment alone -- version, bases -- is given to all (DF3d R1).
        parts.append("releases " + ", ".join(_deployment_named(str(d)) for d in listed))
        value = _VALUES.get(str(record.get("value", "")))
        if value is not None:
            parts.append(value)
        return f"{route} via {record.get('variable')} ({'; '.join(parts)})"
    if record.get("release"):
        variant = f" ({record['variant']})" if record.get("variant") else ""
        parts.append(f"release {record['release']}{variant}")
    elif record.get("service"):
        parts.append(f"service {record['service']}")
    if record.get("binding_basis") not in (None, "source"):
        parts.append(f"deployment {record['binding_basis']}")
    if record.get("build_basis") not in (None, "source"):
        parts.append(f"image built ({record['build_basis']})")
    relation = _RELATIONS.get(str(record.get("version_relation", "")))
    if relation is not None:
        parts.append(relation)
    value = _VALUES.get(str(record.get("value", "")))
    if value is not None:
        parts.append(value)
    return f"{route} via {record.get('variable')} ({'; '.join(parts)})"


def _deployment_named(entry: str) -> str:
    """A ``deployments`` entry as a reading names it: ``release (variant)``, or the compose
    service's target."""
    if entry.startswith("compose:"):
        return entry.removeprefix("compose:")
    release, _, variant = entry.partition("#")
    return f"{release} ({variant})" if variant else release


def configuration_crossing(resolution: Mapping[str, object]) -> ConfigurationCrossing | None:
    """An edge's configuration-crossing evidence, each place spelled in its repository, or
    ``None``. Public for the reason :func:`package_crossing` is."""
    record = resolution.get(DEPLOYMENT_ENV_EVIDENCE)
    if not isinstance(record, dict):
        return None

    def text(key: str) -> str | None:
        value = record.get(key)
        return str(value) if isinstance(value, str) and value else None

    deployed, built = text("deployed_by") or "", text("built_by") or ""
    workload = text("workload_in")
    declared = text("declared_in")
    return ConfigurationCrossing(
        variable=str(record.get("variable")),
        setter_kind=str(record.get("setter_kind")),
        setter_at=f"{deployed}/{record.get('setter_at')}",
        reader_at=f"{built}/{record.get('reader_at')}",
        deployment_repository=deployed,
        builder_repository=built,
        value=str(record.get("value")),
        release=text("release"),
        variant=text("variant"),
        container=text("container"),
        service=text("service"),
        image=text("image"),
        version_relation=text("version_relation"),
        binding_basis=text("binding_basis"),
        build_basis=text("build_basis"),
        workload_in=f"{deployed}/{workload}" if workload else None,
        declared_in=f"{DECLARATIONS_SLUG}/{declared}" if declared else None,
        deployments=tuple(str(d) for d in _list(record.get("deployments"))),
        decision_ref=str(record.get("decision_ref")),
    )


def http_crossing(resolution: Mapping[str, object], consumer: str | None) -> HttpCrossing | None:
    """An edge's HTTP evidence, each part spelled where it can be read, or ``None``.

    ``consumer`` is the repository of the file asserting the edge, which for a
    caller-to-handler edge is the caller's. Public for the reason
    :func:`package_crossing` is.
    """
    record = resolution.get(HTTP_CROSSING)
    if not isinstance(record, dict) or consumer is None:
        return None
    provider = str(record.get("provider") or "")

    def at(repository: str, key: str) -> str | None:
        value = record.get(key)
        return f"{repository}/{value}" if isinstance(value, str) else None

    def each(repository: str | None, key: str) -> str | None:
        """Several places of one repository, ``; ``-joined, each spelled in it."""
        value = record.get(key)
        if repository is None or not isinstance(value, str) or not value:
            return None
        return "; ".join(
            f"{repository}/{place.strip()}" for place in value.split(";") if place.strip()
        )

    def text(key: str) -> str | None:
        value = record.get(key)
        return str(value) if isinstance(value, str) and value else None

    deployed = text("deployed_by")
    built = text("built_by") or (provider if deployed is not None else None)
    declared = text("declared_in")
    return HttpCrossing(
        method=str(record.get("method")),
        url=str(record.get("url")),
        endpoint=str(record.get("endpoint")),
        deployment=str(record.get("deployment")),
        provider_repository=provider,
        consumer_repository=consumer,
        called_at=at(consumer, "called_at") or "",
        base_at=at(consumer, "base_at"),
        served_at=at(provider, "served_at") or "",
        identity_source=str(record.get("identity_source")),
        identity_at=at(provider, "identity_at"),
        bound_at=at(provider, "bound_at"),
        deployment_repository=deployed,
        builder_repository=text("built_by"),
        release=text("release"),
        variant=text("variant"),
        image=text("image"),
        version_relation=text("version_relation"),
        binding_basis=text("binding_basis"),
        build_basis=text("build_basis"),
        source_rule=text("source_rule"),
        identity_in=each(deployed, "identity_in") if deployed else None,
        binding_in=each(deployed, "binding_in"),
        workload_in=each(deployed, "workload_in"),
        image_in=each(deployed, "image_in"),
        published_in=each(built, "published_in") if text("built_by") else None,
        built_in=each(built, "built_in") if text("built_by") else None,
        declared_in=f"{DECLARATIONS_SLUG}/{declared}" if declared else None,
    )


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []
