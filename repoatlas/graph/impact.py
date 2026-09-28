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
evidence itself -- a ``USES`` edge a package link proved (:mod:`repoatlas.graph.packages`)
-- and never through a shared node: the same environment variable read in two
repositories does not join them. A crossing moves the home to the far end's repository,
so the walk goes on there exactly as it would have started there; a shared node keeps
the home it was reached with. A walk rooted at a shared node with no home adopts the
repository of each dependent it reaches. Nothing crosses into a repository outside the
caller's scope.

**Bounds.** At most :data:`MAX_DISTANCE` steps. Every read goes through
:class:`~repoatlas.graph.neighbourhood.Neighbourhood` and is charged to :class:`Work`;
a spent budget stops the walk and is reported, never hidden. Setters and test files are
leaves: they are affected, and nothing is affected through them.
"""

from __future__ import annotations

import heapq
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import IntEnum

from repoatlas.graph.neighbourhood import Neighbourhood, Work, WorkLimits
from repoatlas.graph.traversal import CROSS_REPOSITORY_EDGES
from repoatlas.models.impact import ImpactRole
from repoatlas.models.parsing import EdgeDirection, EdgeType, EntityType
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
    #: edge: ``package``, a package link's ``crossing`` record. ``None``: it never does.
    crosses: str | None = None


FAMILIES: tuple[Family, ...] = (
    Family("uses", EdgeType.USES, IN, ImpactRole.DEPENDENT, Tier.EXACT, "uses", crosses="package"),
    Family("references", EdgeType.REFERENCES, IN, ImpactRole.DEPENDENT, Tier.EXACT, "references"),
    Family("calls", EdgeType.CALLS, IN, ImpactRole.DEPENDENT, Tier.EXACT, "calls"),
    Family("reads", EdgeType.READS, IN, ImpactRole.DEPENDENT, Tier.EXACT, "reads"),
    Family("configures", EdgeType.CONFIGURES, OUT, ImpactRole.DEPENDENT, Tier.EXACT, "is set by"),
    Family(
        "configures-setter",
        EdgeType.CONFIGURES,
        IN,
        ImpactRole.SETS,
        Tier.EXACT,
        "sets",
        leaf=True,
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
    #: It changes as a whole (see :attr:`Family.whole_only`).
    whole: bool = False
    expanded: bool = False
    alternatives: list[Node] = field(default_factory=list)

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
        self._neighbourhood = Neighbourhood(knowledge, self.work)
        self._slug_ids = {slug: repo_id for repo_id, slug in repo_slugs.items()}
        self._max_distance = max_distance
        self._heap: list[tuple[tuple[int, int, int, int, str, tuple[str, ...]], int, Node]] = []
        self._pushed = 0
        self._reached: dict[int, Node] = {}
        self._depth_cut = 0
        self._members_cut = False
        self._hubs: dict[int, Hub] = {}
        #: The unit of a node from its key, repository slug and path.
        self._unit = unit or (lambda key, slug, path: f"{slug}:." if slug else key)

    # -- seeds ------------------------------------------------------------------

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
                self._reached[seed.entity_id] = seed
            self._push(seed)
        for parent, member in members:
            self._member(parent, member)
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
        return WalkResult(
            seeds=list(seeds),
            reached={i: n for i, n in self._reached.items() if not n.seed},
            work=self.work,
            depth_cut=self._depth_cut,
            unexpanded=sum(
                1 for n in self._reached.values() if not n.expanded and not n.leaf and not n.seed
            ),
            members_cut=self._members_cut,
            hubs=dict(self._hubs),
        )

    def members(
        self, prefix: str, *, exclude_types: Sequence[EntityType] = ()
    ) -> list[EntityIdentity]:
        """A seed's members, read within the budget; more than :data:`MAX_MEMBERS` is a cut."""
        found = self._neighbourhood.members(
            prefix, exclude_types=exclude_types, limit=MAX_MEMBERS + 1
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
        if row.far_id in self._reached and self._reached[row.far_id].seed:
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
        )
        self._offer(candidate)

    def page(
        self, entity_id: int, rule: NeighborRule, confinement: Confinement, *, limit: int
    ) -> list[NeighborRow]:
        """One charged page of a node's neighbours, for steps beside the walk (packages)."""
        return self._neighbourhood.page(entity_id, rule, confinement, limit=limit)

    def repo_id(self, slug: str) -> int | None:
        return self._slug_ids.get(slug)

    def slug_of(self, repo_id: int | None) -> str | None:
        return next((s for s, i in self._slug_ids.items() if i == repo_id), None)

    def _member(self, parent: Node, member: EntityIdentity) -> None:
        if member.entity_id in self._reached:
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
        self._offer(node)

    def _container(self, node: Node) -> None:
        """The declaration a reached member belongs to: its behaviour changes with it."""
        qualified = node.qualified_name or ""
        if "." not in qualified or node.path is None or node.repo_id is None:
            return
        slug = next((s for s, i in self._slug_ids.items() if i == node.repo_id), None)
        if slug is None:
            return
        parent_name = qualified.rsplit(".", 1)[0]
        found = [
            m
            for m in self._neighbourhood.members(
                f"sym:{slug}:{node.path}:{parent_name}:",
                exclude_types=(EntityType.MODULE,),
                limit=2,
            )
            if m.qualified_name == parent_name
        ]
        if len(found) != 1 or found[0].entity_id in self._reached:
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
        self._offer(container)

    # -- the frontier -------------------------------------------------------------

    def _offer(self, candidate: Node) -> None:
        assert candidate.entity_id is not None
        existing = self._reached.get(candidate.entity_id)
        if existing is None:
            self._reached[candidate.entity_id] = candidate
            self._push(candidate)
            return
        if existing.seed:
            return
        if not existing.expanded and candidate.rank < existing.rank:
            # A better path to a node not yet expanded: it takes over, the old one stays
            # as an alternative, and the node is queued again under its new rank.
            previous = existing.parent
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

    @staticmethod
    def _alternative(node: Node, predecessor: Node) -> None:
        if predecessor is node.parent or predecessor in node.alternatives:
            return
        if len(node.alternatives) < MAX_ALTERNATIVES:
            node.alternatives.append(predecessor)

    def _push(self, node: Node) -> None:
        self._pushed += 1
        heapq.heappush(self._heap, (node.rank, self._pushed, node))


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
