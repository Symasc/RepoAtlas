"""The change-impact walk: from what changes to what depends on it, best first, bounded.

RepoAtlas owns the policy here, and only the policy: which indexed relationships carry a
change, in which direction, how far and in what order. Whether a candidate is actually
affected is the reader's judgement, made by reading the source each step locates.

**The propagation table** (:data:`FAMILIES`) is the whole of what may carry a change.
Each row is one edge type, followed in the direction a change travels, with the role the
far end plays and a fixed reading. A row is admitted only with a fixture proving its
stored direction and its role (``tests/capabilities/test_impact_direction.py``); an edge
type without one is excluded rather than inferred from its name. Two further steps are
derived, not indexed: a seed's *members* (declarations whose key extends it), and, at
most once per path, a reached member's *container*.

**Tiers.** A step is ``EXACT`` when it joins two declarations, keys or files by an
indexed fact about exactly them; ``CONTAINMENT`` when it is derived membership; ``FILE``
when only a file-level import carries it. A path is as coarse as its coarsest step.

**Order.** Best first on ``(tier, distance, penalty, role, key, path)``: exact before
contained before file-level, near before far, a type-only use after a value use, a
dependent before a setter, then the far end's key and the whole path's keys.
No row id takes part, so the same repositories give the same answer after any rebuild.
Along a path the key never decreases, so any prefix of the ranking is closed: every
candidate's predecessors rank before it.

**Confinement.** A walk has a home repository: the seed's, else the context's. It leaves
it only by a direct edge of a crossing type (:data:`CROSS_REPOSITORY_EDGES`), or by an
edge of a family that crosses on evidence (:attr:`Family.crosses`) and records that
evidence itself -- a ``USES`` edge a package link proved (:mod:`repoatlas.graph.packages`),
a ``CALLS_HTTP_HANDLER`` edge an HTTP join proved (:mod:`repoatlas.graph.http_crossings`),
a ``CONFIGURES`` edge from a deployment's effective setter to a reader in the image DF2 proved
it runs (:mod:`repoatlas.graph.deployment_env`) -- and never through a shared node: the same
environment variable read in two repositories does not join them. A crossing moves the
home to the far end's repository, so the walk goes on there exactly as it would have
started there; a shared node keeps the home it was reached with. A walk rooted at a shared
node with no home adopts the repository of each dependent it reaches, unless the caller
names the one it starts from (``seed_repository``). Nothing crosses into a repository
outside the caller's scope.

**Deployment context** (DF3d). A configuration crossing (``configures``) applies to the deployments
its evidence names, and a path carries them: a walk state is ``(entity, context)``, never the
entity alone, so two paths through the same code under different deployments are two states. The
context passes unchanged through every step but one: an HTTP crossing (``http``) is followed only
through a link of a deployment in the context, and clears it -- the caller's code runs in its own
service. Without a context (a seed of code, an environment variable, anything a configuration
crossing has not constrained) every link is followed, as before. A deployment is what both edges
already record: a CI-established release (its deploying repository, release, variant and the
workload template its container is selected at) or a compose service (its repository and the line
naming it); anything else is unestablished and matches nothing. A link not followed is counted by
reason (:data:`DEPLOYMENT_INAPPLICABLE`, :data:`DEPLOYMENT_UNESTABLISHED`), never a candidate. An
answer lists one state per entity, chosen so that every predecessor is the listed one; when no such
choice is found, the walk reports the setters whose crossings started each context instead
(:attr:`WalkResult.conflict`).

**Bounds.** At most :data:`MAX_DISTANCE` steps. Every read goes through
:class:`~repoatlas.graph.neighbourhood.Neighbourhood` and is charged to :class:`Work`;
a spent budget stops the walk and is reported, never hidden. Setters and test files are
leaves: they are affected, and nothing is affected through them.
"""

from __future__ import annotations

import copy
import heapq
import json
import re
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import IntEnum

from repoatlas.graph.deploy_identity import COMPOSE
from repoatlas.graph.deployment_env import DEPLOYMENT_ENV_EVIDENCE
from repoatlas.graph.http_crossings import HTTP_CROSSING
from repoatlas.graph.neighbourhood import Neighbourhood, Work, WorkLimits
from repoatlas.graph.traversal import CROSS_REPOSITORY_EDGES
from repoatlas.models.impact import ImpactRole
from repoatlas.models.parsing import FIELD, EdgeDirection, EdgeType, EntityType
from repoatlas.storage.protocols import (
    Confinement,
    EntityIdentity,
    KnowledgeStore,
    NeighborRow,
    NeighborRule,
)

MAX_DISTANCE = 8
#: Rows per page when a node's admitted neighbours are read.
PAGE = 200
#: A reached node with more admitted dependents than this is a hub: it stays a candidate
#: with its exact fan-out, shows :data:`REPRESENTATIVES` of them spread over units, and the
#: rest are counted, not followed. Fixed on the neutral fixtures, before any benchmark
#: probe; for reference, a symbol's fan-out in LVL is 27 at the 99th percentile.
HUB = 24
REPRESENTATIVES = 6
#: The most far files a hub's spread is grouped over before it is reported partial.
MAX_GROUPS = 64
#: The most alternative predecessors a candidate keeps.
MAX_ALTERNATIVES = 5
#: The most members a seed is read with.
MAX_MEMBERS = 2_000
#: The most times the representative pass revisits its choices (DF3d, R2).
MAX_SWEEPS = 16

#: Why a link was not followed under a deployment context (DF3d): it is a link of another
#: deployment, or the deployment it, or the context's deployment it might be, is not established.
DEPLOYMENT_INAPPLICABLE = "deployment_inapplicable"
DEPLOYMENT_UNESTABLISHED = "deployment_unestablished"
#: A deployment, as the evidence states it: ``("release", repository, release, variant,
#: workload)``, ``("compose", repository, service line)``; or, unestablished,
#: ``("release?", repository, release, variant)`` (the workload not established) and
#: ``("unknown",)``. Only the first two ever match.
Deployment = tuple[str, ...]
#: The deployments a path's configuration applies to; ``None`` when unconstrained.
Context = frozenset[Deployment] | None
_ESTABLISHED = frozenset({"release", "compose"})
#: The families a deployment context may keep a step of from being taken.
_GATED = frozenset({"configures", "http", "requests"})
_UNKNOWN: Deployment = ("unknown",)


class Tier(IntEnum):
    EXACT = 0
    CONTAINMENT = 1
    FILE = 2


IN, OUT = EdgeDirection.IN, EdgeDirection.OUT


@dataclass(frozen=True, slots=True)
class Family:
    """One row of the propagation table."""

    name: str
    edge_type: EdgeType
    #: Followed from the node being expanded: ``IN`` reaches edge sources, ``OUT`` targets.
    follow: EdgeDirection
    role: ImpactRole
    tier: Tier
    #: How the far end relates to the node it was reached from: "<far> <reading> <near>".
    reading: str
    #: Applies only when the expanded node is one of these types.
    near: frozenset[EntityType] | None = None
    #: Admits only far ends of these types.
    far: frozenset[EntityType] | None = None
    #: The far end is affected, and nothing is affected through it.
    leaf: bool = False
    #: Applies only from the seed itself.
    seed_only: bool = False
    #: Applies only from a file that changes as a whole: a file or directory seed. A
    #: file reached because it imports or re-exports one name does not change whole,
    #: and its importers are reached precisely through their uses.
    whole_only: bool = False
    #: The evidence on which an edge of this family may join two repositories, edge by
    #: edge: ``package``, a package link's ``crossing`` record; ``http``, an HTTP join's
    #: ``http`` record; ``deployment_env``, a configuration crossing's record
    #: (repoatlas.storage.protocols.EVIDENCE_PATHS). ``None``: it never does.
    crosses: str | None = None
    #: Admits only edges whose resolution records one of these evidence keys:
    #: ``graphql_codegen``, the configuration a declaration was generated by, or
    #: ``graphql_composition``, the subgraph a supergraph coordinate was composed from.
    #: Another pass's edges of the same type -- Salt's ``GENERATES`` -- are never followed.
    evidence: tuple[str, ...] = ()


FAMILIES: tuple[Family, ...] = (
    Family("uses", EdgeType.USES, IN, ImpactRole.DEPENDENT, Tier.EXACT, "uses", crosses="package"),
    Family("references", EdgeType.REFERENCES, IN, ImpactRole.DEPENDENT, Tier.EXACT, "references"),
    Family("calls", EdgeType.CALLS, IN, ImpactRole.DEPENDENT, Tier.EXACT, "calls"),
    Family("reads", EdgeType.READS, IN, ImpactRole.DEPENDENT, Tier.EXACT, "reads"),
    # A deployment's setter configures a reader: outward from the setter, and inward to it as
    # a leaf. Across repositories only on a deployment_env record (DF3a); Salt's and a
    # project's CONFIGURES edges carry none, so they never cross.
    Family(
        "configures",
        EdgeType.CONFIGURES,
        OUT,
        ImpactRole.DEPENDENT,
        Tier.EXACT,
        "is set by",
        crosses="deployment_env",
    ),
    Family(
        "configures-setter",
        EdgeType.CONFIGURES,
        IN,
        ImpactRole.SETS,
        Tier.EXACT,
        "sets",
        leaf=True,
        crosses="deployment_env",
    ),
    Family(
        "exports-env",
        EdgeType.EXPORTS,
        OUT,
        ImpactRole.DEPENDENT,
        Tier.EXACT,
        "is declared by",
        far=frozenset({EntityType.ENV_VAR}),
    ),
    Family(
        "exports-env-setter",
        EdgeType.EXPORTS,
        IN,
        ImpactRole.SETS,
        Tier.EXACT,
        "declares",
        near=frozenset({EntityType.ENV_VAR}),
        leaf=True,
    ),
    Family(
        "serves",
        EdgeType.SERVES_HTTP_ROUTE,
        OUT,
        ImpactRole.DEPENDENT,
        Tier.EXACT,
        "is served by",
    ),
    Family(
        "served-by",
        EdgeType.SERVES_HTTP_ROUTE,
        IN,
        ImpactRole.DEPENDENT,
        Tier.EXACT,
        "serves",
        near=frozenset({EntityType.HTTP_ROUTE}),
        seed_only=True,
    ),
    Family(
        "requests",
        EdgeType.CALLS_HTTP_ROUTE,
        IN,
        ImpactRole.DEPENDENT,
        Tier.EXACT,
        "requests",
        near=frozenset({EntityType.HTTP_ROUTE}),
    ),
    # A request reaches a handler another repository serves: the handler changes, so the
    # request may be affected. Only inward, from the handler: its callers do not change it.
    Family(
        "http",
        EdgeType.CALLS_HTTP_HANDLER,
        IN,
        ImpactRole.DEPENDENT,
        Tier.EXACT,
        "requests over HTTP",
        crosses="http",
    ),
    # A generated declaration changes with the schema definition its configuration loads it
    # from: outward, from the definition, and only on that evidence.
    Family(
        "generates",
        EdgeType.GENERATES,
        OUT,
        ImpactRole.DEPENDENT,
        Tier.EXACT,
        "is generated from",
        # the configuration that generated it, the composition that copied it, or the
        # Prisma Migrate migration written from it
        evidence=("graphql_codegen", "graphql_composition", "prisma_migrate"),
    ),
    # An explicit typed mapping maps one enum member to another: what the key maps to
    # changes with it. Outward only, from the key.
    Family(
        "maps-to",
        EdgeType.MAPS_TO,
        OUT,
        ImpactRole.DEPENDENT,
        Tier.EXACT,
        "is mapped from",
    ),
    Family(
        "imports",
        EdgeType.IMPORTS,
        IN,
        ImpactRole.DEPENDENT,
        Tier.FILE,
        "imports",
        near=frozenset({EntityType.FILE}),
        far=frozenset({EntityType.FILE}),
        whole_only=True,
    ),
)

#: Edge types that may join two repositories, edge by edge, on the evidence they record.
EVIDENCED_CROSSING = frozenset(f.edge_type for f in FAMILIES if f.crosses is not None)

#: Derived steps: facts about keys and spans, not edges.
MEMBER = "member"
MEMBER_OF = "member-of"
DERIVED_READINGS = {MEMBER: "is a member of", MEMBER_OF: "contains"}

_ROLE_RANK = {
    ImpactRole.DEPENDENT: 0,
    ImpactRole.MEMBER: 0,
    ImpactRole.SETS: 1,
}

#: Test files, by the common conventions of the ecosystems indexed. A convention, used to
#: section and to stop the walk -- never to decide whether something is affected.
_TEST_PATH = re.compile(
    r"(^|/)(__tests__|__mocks__|tests?|spec|e2e)/"
    r"|(\.|_)(test|spec)\.[A-Za-z0-9]+$"
    r"|(^|/)test_[^/]+\.py$"
    r"|(^|/)conftest\.py$"
)


def is_test_path(path: str | None) -> bool:
    return bool(path) and bool(_TEST_PATH.search(path or ""))


@dataclass(frozen=True, slots=True)
class Step:
    """The one fact joining a reached node to the node it was reached from."""

    #: A family's name, or :data:`MEMBER` / :data:`MEMBER_OF`.
    family: str
    reading: str
    edge_type: EdgeType | None = None
    #: The indexed edge, for an edge step.
    row: NeighborRow | None = None

    @property
    def derived(self) -> str | None:
        return self.family if self.row is None else None

    @property
    def resolution(self) -> dict[str, object]:
        if self.row is None or not self.row.resolution:
            return {}
        loaded = json.loads(self.row.resolution)
        return loaded if isinstance(loaded, dict) else {}


@dataclass(slots=True)
class Node:
    """A seed, or something the walk reached, with the best path to it found so far."""

    entity_id: int | None
    key: str
    type: EntityType | None
    name: str
    qualified_name: str | None
    repo_id: int | None
    path: str | None
    lines: tuple[int, int] | None
    home: int | None
    distance: int = 0
    tier: Tier = Tier.EXACT
    penalty: int = 0
    role: ImpactRole = ImpactRole.DEPENDENT
    parent: Node | None = None
    step: Step | None = None
    #: The keys of every node on the path, seed first.
    signature: tuple[str, ...] = ()
    went_up: bool = False
    leaf: bool = False
    seed: bool = False
    #: A model field seed: it climbs once to its model (DF3b, :meth:`ImpactWalk.run`).
    climbs: bool = False
    #: It changes as a whole (see :attr:`Family.whole_only`).
    whole: bool = False
    expanded: bool = False
    alternatives: list[Node] = field(default_factory=list)
    #: The deployments this path's configuration crossing applies to (DF3d); ``None`` when
    #: no crossing constrains it. Part of the state, never of the rank.
    context: Context = None

    @property
    def rank(self) -> tuple[int, int, int, int, str, tuple[str, ...]]:
        return (
            int(self.tier),
            self.distance,
            self.penalty,
            _ROLE_RANK[self.role],
            self.key,
            self.signature,
        )

    def ancestors(self) -> list[Node]:
        """Every node between the seed and this one, nearest first; seeds excluded."""
        found, node = [], self.parent
        while node is not None and not node.seed:
            found.append(node)
            node = node.parent
        return found


@dataclass(frozen=True, slots=True)
class Hub:
    """A reached node with more dependents than an answer can show."""

    fanout: int
    units: dict[str, int]
    units_partial: bool
    #: Far entities the representatives reached.
    shown: int
    #: Exact dependents per repository, when they are in more than one.
    repositories: dict[str, int] = field(default_factory=dict)

    @property
    def collapsed(self) -> int:
        return max(0, self.fanout - self.shown)


@dataclass(slots=True)
class WalkResult:
    seeds: list[Node]
    #: Everything reached, by entity id.
    reached: dict[int, Node]
    work: Work
    #: Nodes the distance bound kept from being expanded.
    depth_cut: int = 0
    #: Nodes reached but never expanded because the budget ended first.
    unexpanded: int = 0
    #: Whether a seed's members were more than were read.
    members_cut: bool = False
    #: Hubs by entity id.
    hubs: dict[int, Hub] = field(default_factory=dict)
    #: Links not followed under a deployment context, by reason (DF3d): never candidates.
    skipped: dict[str, int] = field(default_factory=dict)
    #: The setters whose configuration crossings started contexts no single path per entity
    #: could represent together (DF3d): the answer is ambiguous, ``reached`` is empty.
    conflict: list[Node] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return (
            self.work.exhausted is None
            and self.depth_cut == 0
            and self.unexpanded == 0
            and not self.members_cut
        )


def rules_for(node: Node) -> list[tuple[NeighborRule, dict[EdgeType, Family]]]:
    """The one ``IN`` and one ``OUT`` query that expand ``node``, with each type's family."""
    found = []
    for direction in (IN, OUT):
        families = {
            f.edge_type: f
            for f in FAMILIES
            if f.follow is direction
            and (f.near is None or node.type in f.near)
            and (not f.seed_only or node.seed)
            and (not f.whole_only or node.whole)
        }
        if not families:
            continue
        rule = NeighborRule(
            edge_types=tuple(sorted(families, key=str)),
            direction=direction,
            far_types=tuple(
                (t, tuple(sorted(f.far, key=str)))
                for t, f in sorted(families.items(), key=lambda item: str(item[0]))
                if f.far is not None
            ),
            uses_replace_file_facts=EdgeType.USES in families,
            evidence=tuple(
                (t, key)
                for t, f in sorted(families.items(), key=lambda item: str(item[0]))
                for key in f.evidence
            ),
        )
        found.append((rule, families))
    return found


class ImpactWalk:
    """One change-impact walk over the index, within one budget."""

    def __init__(
        self,
        knowledge: KnowledgeStore,
        repo_slugs: dict[int, str],
        *,
        limits: WorkLimits | None = None,
        max_distance: int = MAX_DISTANCE,
        unit: Callable[[str, str | None, str | None], str] | None = None,
        scope: frozenset[int] | None = None,
    ) -> None:
        self.work = Work(limits or WorkLimits())
        #: The repositories the caller's scope names; ``None`` for all of them.
        self._scope = scope
        self._knowledge = knowledge
        self._neighbourhood = Neighbourhood(knowledge, self.work)
        self._slug_ids = {slug: repo_id for repo_id, slug in repo_slugs.items()}
        self._max_distance = max_distance
        self._heap: list[tuple[tuple[int, int, int, int, str, tuple[str, ...]], int, Node]] = []
        self._pushed = 0
        #: Every state reached, by entity and deployment context (DF3d), and by entity.
        self._states: dict[tuple[int, Context], Node] = {}
        self._by_entity: dict[int, list[Node]] = defaultdict(list)
        self._seed_ids: set[int] = set()
        #: Links a deployment context kept from being followed, and every one followed.
        self._skipped: dict[int, str] = {}
        self._followed: set[int] = set()
        #: Paths offered to a state and not kept (or replaced), by the state's ``id``: what the
        #: representative pass may re-root a state through (DF3d, R2).
        self._offers: dict[int, list[Node]] = defaultdict(list)
        #: Stored deployment_env decisions by deploying repository, read once (DF3d, R4).
        self._configured: dict[str, dict[tuple[str, str], list[dict[str, object]]]] = {}
        self._depth_cut = 0
        self._members_cut = False
        self._hubs: dict[int, Hub] = {}
        #: The unit of a node from its key, repository slug and path.
        self._unit = unit or (lambda key, slug, path: f"{slug}:." if slug else key)

    # -- seeds ------------------------------------------------------------------

    def is_field(self, identity: EntityIdentity) -> bool:
        """Whether the entity is a model field (DF3b)."""
        if identity.type is not EntityType.SYMBOL:
            return False
        return self._knowledge.entity_kinds([identity.entity_id]).get(identity.entity_id) == FIELD

    def node_of(self, identity: EntityIdentity, home: int | None) -> Node:
        return Node(
            entity_id=identity.entity_id,
            key=identity.key,
            type=identity.type,
            name=identity.name,
            qualified_name=identity.qualified_name,
            repo_id=self._slug_ids.get(identity.repository_slug or ""),
            path=identity.path,
            lines=(
                (identity.line_start, identity.line_end or identity.line_start)
                if identity.line_start is not None
                else None
            ),
            home=home,
        )

    def run(
        self, seeds: Sequence[Node], members: Sequence[tuple[Node, EntityIdentity]]
    ) -> WalkResult:
        """Walk from ``seeds`` and their ``members`` until nothing is left or the budget ends."""
        for seed in seeds:
            seed.seed, seed.signature = True, (seed.key,)
            seed.whole = seed.type in {EntityType.FILE, None}
            if seed.entity_id is not None:
                self._seed_ids.add(seed.entity_id)
                self._states[(seed.entity_id, None)] = seed
                self._by_entity[seed.entity_id].append(seed)
            self._push(seed)
        for parent, member in members:
            self._member(parent, member)
        for seed in seeds:
            if seed.climbs:
                # A field changes its model's shape: the model, once, at the containment tier,
                # after every exact use of the field itself (DF3b).
                self._container(seed)
        while self._heap:
            _, _, node = heapq.heappop(self._heap)
            if node.expanded:
                continue
            node.expanded = True
            if node.leaf or node.entity_id is None:
                # a directory seed has no edges of its own: its members carry them
                continue
            if node.distance >= self._max_distance:
                self._depth_cut += 1
                continue
            if not self.work.expand():
                break
            self._expand(node)
            if self.work.exhausted is not None:
                break
        reached, conflict = self._represent()
        skipped: dict[str, int] = {}
        for edge, reason in sorted(self._skipped.items()):
            if edge not in self._followed:
                skipped[reason] = skipped.get(reason, 0) + 1
        return WalkResult(
            seeds=list(seeds),
            reached={} if conflict else reached,
            work=self.work,
            depth_cut=self._depth_cut,
            unexpanded=sum(
                1 for n in self._states.values() if not n.expanded and not n.leaf and not n.seed
            ),
            members_cut=self._members_cut,
            hubs=dict(self._hubs),
            skipped=skipped,
            conflict=conflict,
        )

    def members(
        self, prefix: str, *, exclude_types: Sequence[EntityType] = ()
    ) -> list[EntityIdentity]:
        """A seed's members, read within the budget; more than :data:`MAX_MEMBERS` is a cut.

        A model field is never one: it changes only when its model's source does, and every
        declaration using it uses its model too (DF3b).
        """
        found = self._neighbourhood.members(
            prefix, exclude_types=exclude_types, exclude_kinds=(FIELD,), limit=MAX_MEMBERS + 1
        )
        if len(found) > MAX_MEMBERS:
            self._members_cut = True
        return found[:MAX_MEMBERS]

    # -- expansion --------------------------------------------------------------

    def _expand(self, node: Node) -> None:
        assert node.entity_id is not None
        # Across repositories a file is followed into only when it changes whole. A use
        # that reaches a file names its whole namespace (a computed member, the module
        # passed as a value): it says no declaration in it is used, so from a file that
        # only re-exports or imports the changed name it is no exact consumer of it.
        # The package summary counts it instead, as a consumer left unresolved.
        whole_only = node.type is EntityType.FILE and not node.whole
        confinement = Confinement(
            crossing=CROSS_REPOSITORY_EDGES,
            evidenced=frozenset() if whole_only else EVIDENCED_CROSSING,
            home_repo_id=node.home,
            crossing_moves_home=True,
            scope_repo_ids=self._scope,
        )
        for rule, families in rules_for(node):
            after = None
            limit = PAGE
            if not node.seed and not rule.outgoing:
                # A probe one row past the hub bound: a node this size is read whole.
                limit = HUB + 1
                probe = self._neighbourhood.page(node.entity_id, rule, confinement, limit=limit)
                if len(probe) > HUB and self._hub(node, rule, confinement, families):
                    continue
                for row in probe:
                    self._reach(node, row, families[row.type])
                if len(probe) < limit or self.work.exhausted is not None:
                    continue
                after, limit = probe[-1].cursor, PAGE
            while True:
                rows = self._neighbourhood.page(
                    node.entity_id, rule, confinement, after=after, limit=limit
                )
                for row in rows:
                    self._reach(node, row, families[row.type])
                if len(rows) < limit or self.work.exhausted is not None:
                    break
                after = rows[-1].cursor
        if (
            self.work.exhausted is None
            and node.step is not None
            and node.step.row is not None
            and not node.went_up
            and node.role is ImpactRole.DEPENDENT
        ):
            self._container(node)

    def _hub(
        self,
        node: Node,
        rule: NeighborRule,
        confinement: Confinement,
        families: dict[EdgeType, Family],
    ) -> bool:
        """Collapse ``node`` if it has more dependents than :data:`HUB`; ``False`` if not."""
        assert node.entity_id is not None
        counts = self._neighbourhood.counts([node.entity_id], rule, confinement)
        count = counts.get(node.entity_id) if counts is not None else None
        if count is None or count.far <= HUB:
            return False
        groups, partial = self._neighbourhood.distribution(
            node.entity_id, rule, confinement, max_groups=MAX_GROUPS
        )
        units: dict[str, int] = {}
        queues: dict[str, list[tuple[int | None, str | None]]] = {}
        #: Each unit's repository (a shared node's unit is its own), and each one's total.
        owner: dict[str, str] = {}
        repositories: dict[str, int] = {}
        for group in groups:
            slug = self.slug_of(group.repo_id)
            unit = self._unit(f"{slug}:{group.path}", slug, group.path)
            units[unit] = units.get(unit, 0) + group.far
            queues.setdefault(unit, []).append((group.repo_id, group.path))
            owner.setdefault(unit, slug or unit)
            if slug is not None:
                repositories[slug] = group.repository_far
        # Largest units first, then by name; within a unit, non-test files first.
        for queue in queues.values():
            queue.sort(key=lambda g: is_test_path(g[1]))
        order = sorted(queues, key=lambda u: (-units[u], u))
        chosen = _representatives(order, queues, owner, repositories)
        rows = self._neighbourhood.rows_for(
            node.entity_id, rule, confinement, groups=chosen, per_group=1
        )
        shown = set()
        for row in rows:
            self._reach(node, row, families[row.type])
            shown.add(row.far_id)
        self._hubs[node.entity_id] = Hub(
            fanout=count.far,
            units=units,
            units_partial=partial,
            shown=len(shown),
            repositories=repositories if len(repositories) > 1 else {},
        )
        return True

    def _reach(self, near: Node, row: NeighborRow, family: Family) -> None:
        if row.far_id in self._seed_ids:
            return
        taken, context = self._context(near, row, family)
        if not taken:
            return
        penalty = 0
        if row.type is EdgeType.USES and '"use":"type"' in (row.resolution or ""):
            penalty = 1  # a type-level use changes behaviour less often than a value's
        home = near.home
        if row.far_repo_id is not None and (near.home is None or row.far_repo_id != near.home):
            home = row.far_repo_id
        candidate = Node(
            entity_id=row.far_id,
            key=row.far_key,
            type=row.far_type,
            name=row.far_name,
            qualified_name=row.far_qualified_name,
            repo_id=row.far_repo_id,
            path=row.far_path,
            lines=(
                (row.far_line_start, row.far_line_end or row.far_line_start)
                if row.far_line_start is not None
                else None
            ),
            home=home,
            distance=near.distance + 1,
            tier=max(near.tier, family.tier),
            penalty=near.penalty + penalty,
            role=family.role,
            parent=near,
            step=Step(family.name, family.reading, row.type, row),
            signature=(*near.signature, row.far_key),
            went_up=near.went_up,
            leaf=family.leaf or is_test_path(row.far_path),
            context=context,
        )
        self._offer(candidate)

    # -- deployment context (DF3d) ------------------------------------------------------------

    def _context(self, near: Node, row: NeighborRow, family: Family) -> tuple[bool, Context]:
        """Whether the step ``row`` is taken from ``near`` under its context, and the context
        of the state it reaches. A step not taken is counted, by its edge, unless the same edge
        is taken on another path -- under another context or none."""
        taken, context = self._gate(near, row, family)
        if taken and family.name in _GATED:
            self._followed.add(row.edge_id)
        return taken, context

    def _gate(self, near: Node, row: NeighborRow, family: Family) -> tuple[bool, Context]:
        if family.name == "configures":
            record = _record(row, DEPLOYMENT_ENV_EVIDENCE)
            if not record:
                # no deployment's crossing (a Salt state, a project's own configuration):
                # nothing to constrain the path to
                return True, near.context
            applied = self._applies(record)
            if near.context is None:
                return True, applied
            narrowed = near.context & applied
            if not narrowed:
                return self._skip(row, DEPLOYMENT_INAPPLICABLE)
            return True, narrowed
        if family.name == "http":
            if near.context is None:
                return True, None
            link = _link_deployment(_record(row, HTTP_CROSSING))
            if link is not None and link in near.context:
                return True, None  # the caller's code runs in its own service
            if link is None or _might_be(near.context, link):
                return self._skip(row, DEPLOYMENT_UNESTABLISHED)
            return self._skip(row, DEPLOYMENT_INAPPLICABLE)
        if family.name == "requests" and near.context is not None:
            # A request matched to a route by its path, in the route's repository: it states no
            # deployment, so it never matches a constrained path.
            return self._skip(row, DEPLOYMENT_UNESTABLISHED)
        return True, near.context

    def _skip(self, row: NeighborRow, reason: str) -> tuple[bool, Context]:
        self._skipped.setdefault(row.edge_id, reason)
        return False, None

    def _applies(self, record: Mapping[str, object]) -> frozenset[Deployment]:
        """The deployments a configuration crossing's record applies to, each from its own
        evidence.

        One deployment: the edge's own record. Several (``deployments``): each one's own stored
        ``linked`` decision of the same setter, variable and reader -- its release, variant and
        workload as the release selected them -- never another's (R4). A deployment without
        one is unestablished."""
        listed = record.get("deployments")
        if not isinstance(listed, list) or len(listed) < 2:
            return frozenset({_configured_deployment(record)})
        found = {
            _configured_deployment(decision)
            for decision in self._decisions(record)
            if str(record.get("reader_at", "")) in _places(decision.get("reader_at"))
        }
        established = {d for d in found if d[0] in _ESTABLISHED}
        if len(established) < len(listed):
            # fewer established than listed: the rest are not established
            found |= {_listed_deployment(record, str(entry)) for entry in listed}
            found -= {d for d in found if d[0] == "release?" and _covered(d, established)}
        return frozenset(found)

    def _decisions(self, record: Mapping[str, object]) -> list[dict[str, object]]:
        """The stored ``linked`` deployment_env decisions of this record's setter and variable,
        read once per deploying repository and never charged to the walk's budget."""
        deployed = str(record.get("deployed_by", ""))
        if deployed not in self._configured:
            index: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
            repo_id = self._slug_ids.get(deployed)
            if repo_id is not None:
                for row in self._knowledge.list_link_decisions(DEPLOYMENT_ENV_EVIDENCE, repo_id):
                    if row.decision != "linked":
                        continue
                    loaded = json.loads(row.evidence_json or "{}")
                    if isinstance(loaded, dict) and loaded.get("deployed_by") == deployed:
                        key = (str(loaded.get("setter_at", "")), str(loaded.get("variable", "")))
                        index[key].append(loaded)
            self._configured[deployed] = dict(index)
        key = (str(record.get("setter_at", "")), str(record.get("variable", "")))
        return self._configured[deployed].get(key, [])

    def _represent(self) -> tuple[dict[int, Node], list[Node]]:
        """One state per reached entity, every one's predecessor the state chosen for its own
        entity, so each listed path's ``from`` trail is its ``chain`` (DF3d, R2).

        One state per entity -- every walk no configuration crossing splits -- is that choice
        as it is. Otherwise each entity starts at its best-ranked state, as the walk would list
        it with no contexts. Then, in rank order, a chosen state whose predecessor is not the
        one chosen for its own entity is re-rooted through a path offered to it whose
        predecessor is; else replaced by the best-ranked of its entity's states whose
        predecessor is; else its own path is adopted: each ancestor not chosen for its entity
        becomes the chosen one, each entity at most once. Repeated until nothing changes, at
        most :data:`MAX_SWEEPS` times. A second adoption for one entity, or no settled choice,
        is a conflict: the setters whose crossings started the contexts in play are returned
        instead. Bounded by the walk's own states and offers; a conflict says no choice was
        found, not that none exists."""
        if all(len(states) == 1 for states in self._by_entity.values()):
            return {e: s[0] for e, s in self._by_entity.items() if not s[0].seed}, []
        chosen = {e: min(states, key=lambda s: s.rank) for e, states in self._by_entity.items()}
        adopted: set[int] = set()
        for _ in range(MAX_SWEEPS):
            changed = False
            for entity in sorted(chosen, key=lambda e: chosen[e].rank):
                current = chosen[entity]
                if _from_chosen(current, chosen):
                    continue
                if not self._resolve(entity, current, chosen, adopted):
                    return {}, self._origins()
                changed = True
            if not changed:
                break
        else:
            return {}, self._origins()
        for node in chosen.values():
            node.alternatives = [
                p for p in node.alternatives if p.seed or chosen.get(p.entity_id or -1) is p
            ]
        return {e: n for e, n in chosen.items() if not n.seed}, []

    def _resolve(
        self, entity: int, current: Node, chosen: dict[int, Node], adopted: set[int]
    ) -> bool:
        """Make the entity's chosen state one whose predecessor is chosen: ``current``
        re-rooted, else the best-ranked such state, else ``current``'s own path adopted.
        ``False`` when that would adopt a second path for some entity."""
        if self._reroot(current, chosen):
            return True
        consistent = [s for s in self._by_entity[entity] if _from_chosen(s, chosen)]
        if consistent:
            chosen[entity] = min(consistent, key=lambda s: s.rank)
            return True
        node = current.parent
        while node is not None and not node.seed and node.entity_id is not None:
            if chosen.get(node.entity_id) is node:
                break
            if node.entity_id in adopted:
                return False
            adopted.add(node.entity_id)
            chosen[node.entity_id] = node
            node = node.parent
        return True

    def _reroot(self, state: Node, chosen: Mapping[int, Node]) -> bool:
        """Re-root ``state`` through a path offered to it whose predecessor was chosen, and
        derive its subtree's distances and paths again; never through a path beneath the
        state itself, nor one that takes its subtree past the distance bound."""
        children = self._children()
        for offer in sorted(self._offers.get(id(state), []), key=lambda o: o.rank):
            if offer.parent is None or not _from_chosen(offer, chosen):
                continue
            if _descends(offer.parent, state):
                continue
            longer = offer.parent.distance + 1 - state.distance
            if _deepest(state, children) + longer > self._max_distance:
                continue
            state.parent, state.step, state.role, state.leaf, state.whole = (
                offer.parent,
                offer.step,
                offer.role,
                offer.leaf,
                offer.whole,
            )
            self._derive(state)
            return True
        return False

    def _children(self) -> dict[int, list[Node]]:
        """Each state's successors, by the state's ``id``."""
        children: dict[int, list[Node]] = defaultdict(list)
        for state in self._states.values():
            if state.parent is not None:
                children[id(state.parent)].append(state)
        return children

    def _derive(self, root: Node) -> None:
        """Derive ``root``'s path from its predecessor again, then every state beneath it:
        distance, tier, penalty, path, home -- exactly as the step that reached it did."""
        children = self._children()
        pending = [root]
        while pending:
            node = pending.pop()
            _derive_step(node)
            pending.extend(children.get(id(node), []))

    def _origins(self) -> list[Node]:
        """The setters whose configuration crossings started the contexts of every entity
        reached under more than one: real, indexed setters, by key."""
        found: dict[str, Node] = {}
        for states in self._by_entity.values():
            if len(states) < 2:
                continue
            for state in states:
                origin = None
                node: Node | None = state
                while node is not None and node.parent is not None:
                    if node.step is not None and node.step.family == "configures":
                        origin = node.parent
                    node = node.parent
                if origin is not None:
                    found.setdefault(origin.key, origin)
        return [found[k] for k in sorted(found)]

    def page(
        self, entity_id: int, rule: NeighborRule, confinement: Confinement, *, limit: int
    ) -> list[NeighborRow]:
        """One charged page of a node's neighbours, for steps beside the walk (packages)."""
        return self._neighbourhood.page(entity_id, rule, confinement, limit=limit)

    def repo_id(self, slug: str) -> int | None:
        return self._slug_ids.get(slug)

    def slug_of(self, repo_id: int | None) -> str | None:
        return next((s for s, i in self._slug_ids.items() if i == repo_id), None)

    def repository_at(self, located: str) -> int | None:
        """The repository a ``<slug>/<path>`` names: the longest slug it starts with.

        A slug may hold a ``/`` -- a repository nested inside another is named by its
        path from the root -- and a nested repository's files are never its parent's.
        """
        found = [(len(s), i) for s, i in self._slug_ids.items() if located.startswith(s + "/")]
        return max(found)[1] if found else None

    def _member(self, parent: Node, member: EntityIdentity) -> None:
        if member.entity_id in self._seed_ids or (member.entity_id, parent.context) in self._states:
            return
        node = self.node_of(member, parent.home)
        node.distance = parent.distance + 1
        node.tier = max(parent.tier, Tier.CONTAINMENT)
        node.penalty = parent.penalty
        node.role = ImpactRole.MEMBER
        node.parent = parent
        node.step = Step(MEMBER, DERIVED_READINGS[MEMBER])
        node.signature = (*parent.signature, node.key)
        node.went_up = True  # a member's container is what it is a member of
        node.leaf = is_test_path(node.path)
        node.whole = node.type is EntityType.FILE and parent.whole
        node.context = parent.context
        self._offer(node)

    def _container(self, node: Node) -> None:
        """The declaration a reached member belongs to: its behaviour changes with it."""
        qualified = node.qualified_name or ""
        # An argument's container is what it is an argument of: `Type.field(arg:)` belongs
        # to `Type.field`, and `@directive(arg:)` to `@directive`. No other name has a "(".
        base, paren, _ = qualified.partition("(")
        if node.path is None or node.repo_id is None or not (paren or "." in qualified):
            return
        slug = next((s for s, i in self._slug_ids.items() if i == node.repo_id), None)
        if slug is None:
            return
        parent_name = base if paren else qualified.rsplit(".", 1)[0]
        found = [
            m
            for m in self._neighbourhood.members(
                f"sym:{slug}:{node.path}:{parent_name}:",
                exclude_types=(EntityType.MODULE,),
                limit=2,
            )
            if m.qualified_name == parent_name
        ]
        if len(found) != 1 or (
            found[0].entity_id in self._seed_ids
            or (found[0].entity_id, node.context) in self._states
        ):
            return
        container = self.node_of(found[0], node.home)
        container.distance = node.distance + 1
        container.tier = max(node.tier, Tier.CONTAINMENT)
        container.penalty = node.penalty
        container.role = ImpactRole.DEPENDENT
        container.parent = node
        container.step = Step(MEMBER_OF, DERIVED_READINGS[MEMBER_OF])
        container.signature = (*node.signature, container.key)
        container.went_up = True
        container.leaf = is_test_path(container.path)
        container.context = node.context
        self._offer(container)

    # -- the frontier -------------------------------------------------------------

    def _offer(self, candidate: Node) -> None:
        assert candidate.entity_id is not None
        key = (candidate.entity_id, candidate.context)
        existing = self._states.get(key)
        if existing is None:
            self._states[key] = candidate
            self._by_entity[candidate.entity_id].append(candidate)
            self._push(candidate)
            return
        if existing.seed:
            return
        if not existing.expanded and candidate.rank < existing.rank:
            # A better path to a node not yet expanded: it takes over, the old one stays
            # as an alternative, and the node is queued again under its new rank.
            previous = existing.parent
            self._remember(existing, copy.copy(existing))
            for name in (
                "distance",
                "tier",
                "penalty",
                "role",
                "parent",
                "step",
                "signature",
                "went_up",
                "leaf",
                "home",
                "whole",
            ):
                setattr(existing, name, getattr(candidate, name))
            if previous is not None:
                self._alternative(existing, previous)
            self._push(existing)
            return
        if candidate.parent is not None:
            self._alternative(existing, candidate.parent)
            self._remember(existing, candidate)

    def _remember(self, state: Node, offer: Node) -> None:
        """Keep a path offered to ``state`` -- only where configuration splits a walk into
        several deployment contexts is it ever read (DF3d)."""
        offers = self._offers[id(state)]
        if len(offers) < MAX_ALTERNATIVES:
            offers.append(offer)

    @staticmethod
    def _alternative(node: Node, predecessor: Node) -> None:
        if predecessor is node.parent or predecessor in node.alternatives:
            return
        if len(node.alternatives) < MAX_ALTERNATIVES:
            node.alternatives.append(predecessor)

    def _push(self, node: Node) -> None:
        self._pushed += 1
        heapq.heappush(self._heap, (node.rank, self._pushed, node))


_FAMILY = {family.name: family for family in FAMILIES}


def _derive_step(node: Node) -> None:
    """A node's path fields from its predecessor and its own step, as :meth:`ImpactWalk._reach`,
    ``_member`` and ``_container`` set them."""
    parent, step = node.parent, node.step
    if parent is None or step is None:
        return
    node.distance = parent.distance + 1
    node.signature = (*parent.signature, node.key)
    row = step.row
    if row is None:  # a member, or a member's container
        node.tier = max(parent.tier, Tier.CONTAINMENT)
        node.penalty = parent.penalty
        node.went_up = True
        node.home = parent.home
        return
    family = _FAMILY[step.family]
    node.tier = max(parent.tier, family.tier)
    typed = row.type is EdgeType.USES and '"use":"type"' in (row.resolution or "")
    node.penalty = parent.penalty + (1 if typed else 0)
    node.went_up = parent.went_up
    home = parent.home
    if row.far_repo_id is not None and (parent.home is None or row.far_repo_id != parent.home):
        home = row.far_repo_id
    node.home = home


def _record(row: NeighborRow, key: str) -> dict[str, object]:
    """One evidence record of an edge's resolution, or an empty one."""
    if not row.resolution:
        return {}
    loaded = json.loads(row.resolution)
    record = loaded.get(key) if isinstance(loaded, dict) else None
    return record if isinstance(record, dict) else {}


def _configured_deployment(record: Mapping[str, object]) -> Deployment:
    """The deployment a configuration record (a crossing's, or a stored decision's) states."""
    deployed, release = record.get("deployed_by"), record.get("release")
    if deployed and release:
        variant = str(record.get("variant") or "")
        workload = record.get("workload_in")
        if workload:
            return ("release", str(deployed), str(release), variant, str(workload))
        return ("release?", str(deployed), str(release), variant)
    if deployed and record.get("compose_in"):
        return ("compose", str(deployed), str(record["compose_in"]))
    return _UNKNOWN


def _listed_deployment(record: Mapping[str, object], entry: str) -> Deployment:
    """A deployment a crossing lists without its own evidence: never established."""
    if entry.startswith("compose:"):
        return _UNKNOWN
    release, _, variant = entry.partition("#")
    return ("release?", str(record.get("deployed_by", "")), release, variant)


def _covered(partial: Deployment, established: set[Deployment]) -> bool:
    return any(d[0] == "release" and d[1:4] == partial[1:4] for d in established)


def _link_deployment(record: Mapping[str, object]) -> Deployment | None:
    """The deployment an HTTP link goes through, as its join recorded it; ``None`` when the
    record states none (a Tilt-bound release, a specification's host)."""
    deployed, release, workload = (
        record.get("deployed_by"),
        record.get("release"),
        record.get("workload_in"),
    )
    if deployed and release:
        if not workload:
            return None
        variant = str(record.get("variant") or "")
        return ("release", str(deployed), str(release), variant, str(workload))
    if record.get("identity_source") == COMPOSE and record.get("identity_in"):
        # a compose service runs what its own repository builds: the provider's
        repository = record.get("provider")
        return ("compose", str(repository), str(record["identity_in"])) if repository else None
    return None


def _might_be(context: frozenset[Deployment], link: Deployment) -> bool:
    """Whether a deployment in the context, not itself established, could be the link's."""
    for deployment in context:
        if deployment == _UNKNOWN:
            return True
        if deployment[0] == "release?" and link[0] == "release" and link[1:4] == deployment[1:4]:
            return True
    return False


def _places(value: object) -> set[str]:
    return {p.strip() for p in str(value or "").split(";") if p.strip()}


def _deepest(root: Node, children: Mapping[int, list[Node]]) -> int:
    """The greatest distance in ``root``'s subtree."""
    deepest, pending = root.distance, [root]
    while pending:
        node = pending.pop()
        deepest = max(deepest, node.distance)
        pending.extend(children.get(id(node), []))
    return deepest


def _descends(node: Node, ancestor: Node) -> bool:
    """Whether ``ancestor`` is on ``node``'s path."""
    current: Node | None = node
    while current is not None:
        if current is ancestor:
            return True
        current = current.parent
    return False


def _from_chosen(state: Node, chosen: Mapping[int, Node]) -> bool:
    """Whether the state's predecessor is the one chosen for its own entity (or a seed)."""
    parent = state.parent
    if parent is None or parent.seed or parent.entity_id is None:
        return True
    return chosen.get(parent.entity_id) is parent


def _representatives(
    order: Sequence[str],
    queues: dict[str, list[tuple[int | None, str | None]]],
    owner: dict[str, str],
    totals: dict[str, int],
) -> list[tuple[int | None, str | None]]:
    """A hub's representatives: repositories take turns, then each one's units do.

    Within one repository -- the only case before package links -- this is the units'
    own round, largest first. Across several, the largest repository goes first, and
    each turn a repository gives its next unit's next file, so one repository with many
    units cannot fill every place while another's dependents go unshown.
    """
    chosen: list[tuple[int | None, str | None]] = []
    by_repository: dict[str, list[str]] = {}
    for unit in order:
        by_repository.setdefault(owner[unit], []).append(unit)
    if len(by_repository) <= 1:
        while len(chosen) < REPRESENTATIVES and any(queues[u] for u in order):
            for unit in order:
                if queues[unit] and len(chosen) < REPRESENTATIVES:
                    chosen.append(queues[unit].pop(0))
        return chosen

    def total(repository: str) -> int:
        return totals.get(repository, sum(len(queues[u]) for u in by_repository[repository]))

    turn = sorted(by_repository, key=lambda r: (-total(r), r))
    cursor = dict.fromkeys(turn, 0)
    while len(chosen) < REPRESENTATIVES and any(queues[u] for u in order):
        for repository in turn:
            own = by_repository[repository]
            for step in range(len(own)):
                unit = own[(cursor[repository] + step) % len(own)]
                if queues[unit] and len(chosen) < REPRESENTATIVES:
                    chosen.append(queues[unit].pop(0))
                    cursor[repository] = (cursor[repository] + step + 1) % len(own)
                    break
    return chosen
