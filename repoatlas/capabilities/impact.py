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

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from repoatlas.graph.impact import ImpactWalk, Node, Tier, WalkResult
from repoatlas.graph.keys import file_key
from repoatlas.graph.units import package_directories, unit_of
from repoatlas.models.impact import (
    MAX_SEED_CANDIDATES,
    Granularity,
    ImpactCandidate,
    ImpactOmitted,
    ImpactRole,
    ImpactSeed,
    ImpactStep,
    ImpactSummary,
    ImpactWork,
    SeedBasis,
    SeedCandidate,
    SeedStatus,
)
from repoatlas.models.parsing import EntityType
from repoatlas.storage.protocols import EntityIdentity, KnowledgeStore, SearchIndex

#: The traversal policy's name and version, reported with every answer.
POLICY = "impact/1"

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
        path = term.strip("/")
        if not path or path == ".":
            return None
        found = [
            (slug, count)
            for slug in sorted(self._slugs)
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
        if granularity is Granularity.FILE:
            prefix = f"sym:{slug}:{path}:"
            return self._knowledge.count_keys_with_prefix(prefix, exclude_types=[EntityType.MODULE])
        if not identity.qualified_name:
            return None
        count = self._knowledge.count_keys_with_prefix(
            f"sym:{slug}:{path}:{identity.qualified_name}."
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


def display(node: Node) -> str:
    if node.type is EntityType.FILE or (node.entity_id is None and node.path):
        return node.path or node.key
    if node.repo_id is None and node.path is None:
        return node.key
    return node.qualified_name or node.name


def assemble(
    result: WalkResult,
    walk: ImpactWalk,
    *,
    max_candidates: int,
    full: bool,
    directories: Mapping[str, Sequence[str]],
) -> tuple[tuple[ImpactCandidate, ...], ImpactOmitted, ImpactSummary]:
    """The ranked candidates, closed under their paths, and what was left out, counted."""
    ranked = sorted(result.reached.values(), key=lambda n: n.rank)
    selected: dict[int, Node] = {}
    cut: list[Node] = []
    for node in ranked:
        if node.role is ImpactRole.MEMBER:
            continue  # a member is returned only as a step on some candidate's path
        need = [a for a in reversed(node.ancestors()) if a.entity_id not in selected]
        need.append(node)
        if len(selected) + len(need) > max_candidates:
            cut.append(node)
            continue
        for item in need:
            assert item.entity_id is not None
            selected[item.entity_id] = item
    ordered = sorted(selected.values(), key=lambda n: n.rank)
    ids = {node.entity_id: index for index, node in enumerate(ordered, start=1)}

    def unit(node: Node) -> str:
        return unit_of(node.key, walk.slug_of(node.repo_id), node.path, directories)

    def path_of(node: Node) -> list[Node]:
        chain = [node, *node.ancestors()]
        seed = node
        while seed.parent is not None:
            seed = seed.parent
        return [seed, *reversed(chain)]

    candidates = []
    for node in ordered:
        nodes = path_of(node)
        boundaries: list[str] = []
        last: str | None = None
        for item in nodes:
            if item.repo_id is None and item.entity_id is not None:
                continue  # a shared node belongs to no unit
            here = (
                unit(item)
                if item.entity_id is not None
                else f"{walk.slug_of(item.repo_id)}:{item.path}/"
            )
            if last is not None and here != last:
                boundaries.append(f"{last} -> {here}")
            last = here
        step = node.step
        assert step is not None
        row = step.row
        resolution = step.resolution
        at = None
        lines = None
        if row is not None and row.file_path is not None:
            at = f"{walk.slug_of(row.file_repo_id)}/{row.file_path}"
            if row.line_start is not None:
                lines = (row.line_start, row.line_end or row.line_start)
        parent = node.parent
        candidates.append(
            ImpactCandidate(
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
                    reading=step.reading,
                    use=_text(resolution.get("use")),
                    at=at,
                    lines=lines,
                    parser=row.parser if row is not None else None,
                    syntax=_text(resolution.get("syntax")),
                    via=tuple(str(v) for v in _list(resolution.get("via"))),
                    via_names=tuple(str(v) for v in _list(resolution.get("via_names"))),
                    resolution=resolution or None if full else None,
                ),
                boundaries=tuple(boundaries),
                basis=_BASIS[node.tier],
                chain=" -> ".join(
                    [
                        display(nodes[0]),
                        *(f"{display(n)} ({n.step.reading})" for n in nodes[1:] if n.step),
                    ]
                ),
                alt_paths=tuple(
                    sorted(ids[a.entity_id] for a in node.alternatives if a.entity_id in ids)
                )
                if full
                else (),
            )
        )

    by_reason: dict[str, int] = {}
    if cut:
        by_reason["budget"] = len(cut)
    if result.depth_cut:
        by_reason["depth"] = result.depth_cut
    if result.unexpanded or result.members_cut:
        by_reason["work"] = result.unexpanded + (1 if result.members_cut else 0)
    by_unit: dict[str, int] = {}
    for node in cut:
        by_unit[unit(node)] = by_unit.get(unit(node), 0) + 1
    units = {c.unit for c in candidates if c.unit}
    summary = ImpactSummary(
        reached=len(result.reached),
        returned=len(candidates),
        units=len(units),
        cross_unit=sum(1 for c in candidates if c.boundaries),
        complete=result.complete,
        work=ImpactWork(
            expanded=result.work.expanded,
            rows=result.work.rows,
            queries=result.work.queries,
            exhausted=result.work.exhausted,
        ),
        policy=POLICY,
    )
    return (
        tuple(candidates),
        ImpactOmitted(by_reason=by_reason, by_unit=dict(sorted(by_unit.items()))),
        summary,
    )


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []
