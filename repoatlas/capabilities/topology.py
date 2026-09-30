"""Topology: pure logic behind the three topology capabilities.

Nothing here reads storage. The service fetches rows with a fixed number of
statements; this module turns them into links, entries and references. Keeping
the two apart is what keeps the statement count independent of the data: an
aggregation that wanted one more fact per row would have to be written here, where
there is nothing to query with.

Three rules shape every answer.

**Kinds are never summed.** An observed code edge, an HTTP edge and a package
declaration are three different claims. A link aggregates exactly one kind.

**A link between two children is reported once**, from the child holding the
edges' sources. The edge is read from both ends; reporting both readings would
count one fact twice.

**References are exact.** An ``edge_ref`` carries every stored field that tells
one edge from another, including a full SHA-256 of the recorded resolution: two
stored edges can agree on everything else and differ only in how the linker
resolved them. Rows that agree on every field are one fact recorded more than
once, and resolve together.
"""

from __future__ import annotations

import base64
import binascii
import bisect
import hashlib
import json
import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from repoatlas.capabilities.dependencies import (
    ProviderCandidate,
    declares_package,
    provider_index,
    store_slug,
)
from repoatlas.capabilities.errors import (
    EdgeRefInvalidError,
    InvalidTopologyCursorError,
)
from repoatlas.graph.keys import file_key
from repoatlas.models.capability import CapabilityContext, ContextResolution
from repoatlas.models.capability_io import ContextView
from repoatlas.models.dependencies import DependencyObservation, DependencyPackage
from repoatlas.models.dependency_inventory import AppliedPageLimits
from repoatlas.models.parsing import EdgeType, EntityType
from repoatlas.models.relationships import RelationshipDirection
from repoatlas.models.topology import (
    DEFAULT_CHILDREN_PAGE,
    MAX_AMBIGUOUS,
    MAX_CHILD_LINKS,
    MAX_CHILDREN_PAGE,
    MAX_EXAMPLES,
    MAX_PACKAGES,
    AmbiguousDependency,
    ChildEntry,
    ChildLink,
    EdgeTypeCount,
    EntryKind,
    LinkKind,
    PackageLink,
    PeerKind,
    RepositoryLink,
    TopologyPeer,
)
from repoatlas.storage.protocols import (
    CrossingEdgeRow,
    LinkEdgeRow,
    StoredEdge,
    TopologyDeclarationRow,
    TopologyFileRow,
)

#: Containment: what a directory, a file or a class holds. A map expresses it by
#: nesting, never as a link, and no topology answer counts it.
STRUCTURAL: tuple[EdgeType, ...] = (EdgeType.CONTAINS, EdgeType.DEFINES)

#: The HTTP edge family. ``CALLS_HTTP_HANDLER`` joins a caller to a handler by
#: route compatibility; the others reach the shared route and endpoint nodes.
HTTP_EDGES: frozenset[EdgeType] = frozenset(
    {
        EdgeType.SERVES_HTTP_ROUTE,
        EdgeType.CALLS_HTTP_ROUTE,
        EdgeType.DECLARES_HTTP_DEPLOYMENT,
        EdgeType.EXPOSES_HTTP_ENDPOINT,
        EdgeType.CALLS_HTTP_ENDPOINT,
        EdgeType.CALLS_HTTP_HANDLER,
    }
)

#: Manifest dependencies. The linker draws one from a repository to each package
#: its manifests declare, and one to the repository publishing it where exactly
#: one other repository does, both asserted at the declaring line.
DEPENDENCY_EDGES: frozenset[EdgeType] = frozenset({EdgeType.DEPENDS_ON})

#: The most edge rows one scan reads. Past it an answer says it is incomplete and
#: leaves edge links out, rather than aggregating whichever rows came first.
ROW_CEILING = 250_000

_EDGE_REF_PREFIX = "er1."
_CURSOR_PREFIX = "tc1."
_EDGE_REF_FIELDS = 10
_SHA256_HEX = 64

#: The reading order of link kinds, fixed so output never depends on row order.
_KIND_ORDER = {LinkKind.OBSERVED: 0, LinkKind.HTTP: 1, LinkKind.DEPENDENCY: 2}


def link_kind(edge_type: EdgeType) -> LinkKind:
    """Which kind of link a stored edge of this type belongs to."""
    if edge_type in HTTP_EDGES:
        return LinkKind.HTTP
    if edge_type in DEPENDENCY_EDGES:
        return LinkKind.DEPENDENCY
    return LinkKind.OBSERVED


_KIND_BY_TYPE: dict[str, LinkKind] = {str(t): link_kind(t) for t in EdgeType}


# ---------------------------------------------------------------------------
# Edge references
# ---------------------------------------------------------------------------


def resolution_digest(resolution: str | None) -> str | None:
    """The full SHA-256 of an edge's stored resolution text, or ``None``."""
    if resolution is None:
        return None
    return hashlib.sha256(resolution.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class EdgeIdentity:
    """Every stored field that tells one edge from another."""

    src_key: str
    dst_key: str
    type: EdgeType
    repository_slug: str | None
    path: str | None
    line_start: int | None
    line_end: int | None
    parser: str | None
    confidence: float
    resolution: str | None

    def matches(self, edge: StoredEdge) -> bool:
        return (
            edge.src_key == self.src_key
            and edge.dst_key == self.dst_key
            and edge.type is self.type
            and edge.repository_slug == self.repository_slug
            and edge.path == self.path
            and edge.line_start == self.line_start
            and edge.line_end == self.line_end
            and edge.parser == self.parser
            and edge.confidence == self.confidence
            and resolution_digest(edge.resolution) == self.resolution
        )


def edge_identity(edge: StoredEdge) -> EdgeIdentity:
    return EdgeIdentity(
        src_key=edge.src_key,
        dst_key=edge.dst_key,
        type=edge.type,
        repository_slug=edge.repository_slug,
        path=edge.path,
        line_start=edge.line_start,
        line_end=edge.line_end,
        parser=edge.parser,
        confidence=edge.confidence,
        resolution=resolution_digest(edge.resolution),
    )


def encode_edge_ref(edge: StoredEdge) -> str:
    """The opaque, exact reference to one stored edge."""
    identity = edge_identity(edge)
    fields = [
        identity.src_key,
        identity.dst_key,
        str(identity.type),
        identity.repository_slug,
        identity.path,
        identity.line_start,
        identity.line_end,
        identity.parser,
        identity.confidence,
        identity.resolution,
    ]
    raw = json.dumps(fields, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return _EDGE_REF_PREFIX + base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _optional_str(value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise EdgeRefInvalidError()


def _optional_int(value: object) -> int | None:
    if value is None or (isinstance(value, int) and not isinstance(value, bool)):
        return value
    raise EdgeRefInvalidError()


def decode_edge_ref(ref: str) -> EdgeIdentity:
    """Read a reference this server issued, or refuse it whole."""
    if not ref.startswith(_EDGE_REF_PREFIX):
        raise EdgeRefInvalidError()
    body = ref[len(_EDGE_REF_PREFIX) :]
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        fields = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise EdgeRefInvalidError() from exc
    if not isinstance(fields, list) or len(fields) != _EDGE_REF_FIELDS:
        raise EdgeRefInvalidError()
    src_key, dst_key, type_name = fields[0], fields[1], fields[2]
    confidence, digest = fields[8], fields[9]
    if not (isinstance(src_key, str) and isinstance(dst_key, str) and isinstance(type_name, str)):
        raise EdgeRefInvalidError()
    if isinstance(confidence, bool) or not isinstance(confidence, int | float):
        raise EdgeRefInvalidError()
    if not math.isfinite(confidence):
        raise EdgeRefInvalidError()
    digest = _optional_str(digest)
    if digest is not None and (
        len(digest) != _SHA256_HEX or digest.strip("0123456789abcdef") != ""
    ):
        raise EdgeRefInvalidError()
    try:
        edge_type = EdgeType(type_name)
    except ValueError as exc:
        raise EdgeRefInvalidError() from exc
    return EdgeIdentity(
        src_key=src_key,
        dst_key=dst_key,
        type=edge_type,
        repository_slug=_optional_str(fields[3]),
        path=_optional_str(fields[4]),
        line_start=_optional_int(fields[5]),
        line_end=_optional_int(fields[6]),
        parser=_optional_str(fields[7]),
        confidence=float(confidence),
        resolution=digest,
    )


def matching_edges(identity: EdgeIdentity, candidates: Iterable[StoredEdge]) -> list[StoredEdge]:
    """The stored rows that are exactly the fact the reference names."""
    return [edge for edge in candidates if identity.matches(edge)]


def decoded_resolution(edge: StoredEdge) -> dict[str, Any] | None:
    """The stored resolution as the object the linker wrote, or ``None``."""
    if not edge.resolution:
        return None
    try:
        loaded = json.loads(edge.resolution)
    except ValueError:
        return None
    return loaded if isinstance(loaded, dict) else None


# ---------------------------------------------------------------------------
# Aggregation shared by both link shapes
# ---------------------------------------------------------------------------

_SortKey = tuple[str, int, str, str, int]


@dataclass(slots=True)
class _Group:
    """One link while it is being counted: totals, per-type counts, first examples."""

    count: int = 0
    types: Counter[str] = field(default_factory=Counter)
    #: The first :data:`MAX_EXAMPLES` edges by natural order, as (key, edge id).
    examples: list[tuple[_SortKey, int]] = field(default_factory=list)

    def add(self, edge_type: str, order: _SortKey, edge_id: int) -> None:
        self.count += 1
        self.types[edge_type] += 1
        if len(self.examples) < MAX_EXAMPLES or order < self.examples[-1][0]:
            bisect.insort(self.examples, (order, edge_id))
            del self.examples[MAX_EXAMPLES:]

    def type_counts(self) -> tuple[EdgeTypeCount, ...]:
        return tuple(
            EdgeTypeCount(type=EdgeType(name), count=n)
            for name, n in sorted(self.types.items(), key=lambda item: (-item[1], item[0]))
        )

    def example_ids(self) -> list[int]:
        return [edge_id for _, edge_id in self.examples]


def example_refs(ids: Sequence[int], refs: Mapping[int, str]) -> tuple[str, ...]:
    return tuple(refs[edge_id] for edge_id in ids if edge_id in refs)


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

_OverviewKey = tuple[LinkKind, str, str]
_Site = tuple[str | None, int | None]


@dataclass(slots=True)
class OverviewGroups:
    groups: dict[_OverviewKey, _Group] = field(default_factory=dict)
    #: For each dependency link, where its edges are asserted: ``(path, line)``.
    sites: dict[tuple[str, str], list[_Site]] = field(default_factory=dict)

    def example_ids(self) -> list[int]:
        return [edge_id for group in self.groups.values() for edge_id in group.example_ids()]


def crossing_groups(rows: Iterable[CrossingEdgeRow], slugs: Mapping[int, str]) -> OverviewGroups:
    """Cross-repository edges grouped by kind and repository pair.

    ``slugs`` names only the repositories in scope, so an edge with either end
    outside the scope is not counted: both ends of an overview link are in scope.
    """
    found = OverviewGroups()
    for row in rows:
        source, target = slugs.get(row.src_repo_id), slugs.get(row.dst_repo_id)
        if source is None or target is None:
            continue
        kind = _KIND_BY_TYPE[row.type]
        key = (kind, source, target)
        group = found.groups.get(key)
        if group is None:
            group = found.groups[key] = _Group()
        order = (row.src_key, row.line_start or 0, row.dst_key, row.type, row.edge_id)
        group.add(row.type, order, row.edge_id)
        if kind is LinkKind.DEPENDENCY:
            found.sites.setdefault((source, target), []).append((row.path, row.line_start))
    return found


def _location(repository: str, path: str, line: int) -> str:
    return f"{repository}/{path}:{line}"


_Declared = tuple[DependencyPackage, DependencyObservation]
_DeclarationSite = tuple[str, str, int]


def declaration_index(
    packages: Sequence[DependencyPackage],
) -> dict[tuple[str, str, int], _Declared]:
    """Every declaration by where it is written: ``(repository, path, line)``."""
    index: dict[_DeclarationSite, _Declared] = {}
    for package in packages:
        for observation in package.declarations:
            site = (store_slug(package), observation.source_path, observation.line)
            index.setdefault(site, (package, observation))
    return index


def package_links(
    declaring: str,
    provider: str,
    sites: Sequence[_Site],
    declared: Mapping[_DeclarationSite, _Declared],
    publishers: Mapping[tuple[str, str], Sequence[ProviderCandidate]],
) -> tuple[PackageLink, ...]:
    """The packages a dependency link's edges were drawn from.

    Read off the declaration each edge is asserted at, so the packages listed are
    exactly the ones the stored edges stand for, never a recomputation of which
    ones should have been linked.
    """
    by_package: dict[tuple[str, str], list[_Declared]] = {}
    for path, line in sites:
        if path is None or line is None:
            continue
        found = declared.get((declaring, path, line))
        if found is not None:
            package, _ = found
            by_package.setdefault((package.ecosystem, package.normalized_name), []).append(found)
    links: list[PackageLink] = []
    for key, entries in sorted(by_package.items()):
        package, first = min(entries, key=lambda entry: entry[1].order)
        there = [c for c in publishers.get(key, ()) if c.repository_slug == provider]
        links.append(
            PackageLink(
                ecosystem=package.ecosystem,
                name=package.name,
                declared_at=_location(declaring, first.source_path, first.line),
                declarations=len(entries),
                constraint=first.value,
                provided_at=(
                    _location(provider, there[0].path, there[0].line_start)
                    if len(there) == 1
                    else None
                ),
            )
        )
    return tuple(links[:MAX_PACKAGES])


def repository_links(
    found: OverviewGroups, refs: Mapping[int, str], packages: Sequence[DependencyPackage]
) -> list[RepositoryLink]:
    declared = declaration_index(packages)
    publishers = provider_index(packages)
    return [
        RepositoryLink(
            kind=kind,
            source=source,
            target=target,
            count=group.count,
            types=group.type_counts(),
            examples=example_refs(group.example_ids(), refs),
            packages=(
                package_links(
                    source, target, found.sites.get((source, target), []), declared, publishers
                )
                if kind is LinkKind.DEPENDENCY
                else ()
            ),
        )
        for (kind, source, target), group in sorted(
            found.groups.items(),
            key=lambda item: (item[0][1], item[0][2], _KIND_ORDER[item[0][0]]),
        )
    ]


def ambiguous_dependencies(
    packages: Sequence[DependencyPackage], in_scope: frozenset[str]
) -> tuple[tuple[AmbiguousDependency, ...], int]:
    """Declarations more than one other repository publishes, which the linker leaves.

    Reported only when the declaring repository and every publisher are in
    scope, so a scoped answer never names a repository outside it.
    """
    publishers = provider_index(packages)
    ambiguous: list[AmbiguousDependency] = []
    for package in sorted(packages, key=lambda p: (store_slug(p), p.ecosystem, p.normalized_name)):
        owner = store_slug(package)
        if owner not in in_scope or not declares_package(package):
            continue
        candidates = publishers.get((package.ecosystem, package.normalized_name), [])
        others = tuple(sorted({c.repository_slug for c in candidates} - {owner}))
        if len(others) > 1 and all(slug in in_scope for slug in others):
            ambiguous.append(
                AmbiguousDependency(
                    repository=owner,
                    ecosystem=package.ecosystem,
                    name=package.name,
                    providers=others,
                )
            )
    return tuple(ambiguous[:MAX_AMBIGUOUS]), len(ambiguous)


# ---------------------------------------------------------------------------
# Children
# ---------------------------------------------------------------------------


def within(prefix: str, path: str) -> bool:
    """Whether ``path`` is at or beneath the directory ``prefix`` (root: ``""``)."""
    return prefix == "" or path.startswith(prefix + "/")


def child_path(prefix: str, path: str) -> str:
    """The immediate child of ``prefix`` that holds ``path``."""
    rest = path[len(prefix) + 1 :] if prefix else path
    head = rest.split("/", 1)[0]
    return f"{prefix}/{head}" if prefix else head


def truncated(path: str, depth: int) -> str:
    """``path`` cut to its first ``depth`` segments."""
    return "/".join(path.split("/")[:depth])


@dataclass(frozen=True, slots=True)
class ChildScope:
    """What a children question is about: one repository, one directory or file.

    In a directory, a child is an immediate subdirectory or file and a local peer
    is named at the child's depth. In a file, a child is a declaration (or the file
    itself, for edges the file asserts as a whole) and a peer in another file is
    named by that file's path.
    """

    repo_id: int
    repository: str
    path: str
    is_file: bool
    #: Declaration entity id -> its key, when ``is_file``.
    declarations: Mapping[int, str]
    cross_repository: bool
    #: Every registered repository's id -> slug, for naming other repositories.
    slugs: Mapping[int, str]


#: A peer while links are counted: ``(kind, repository, path, key, entity_type)``,
#: ``""`` for an absent part. Plain strings, because one is built per edge row and
#: a model per row would cost more than the query that produced the row.
_PeerKey = tuple[str, str, str, str, str]
#: ``(kind, child, direction, peer)``.
_ChildKey = tuple[str, str, str, _PeerKey]

_OUT, _IN = str(RelationshipDirection.OUTGOING), str(RelationshipDirection.INCOMING)
_LOCAL, _REPOSITORY, _SHARED = str(PeerKind.LOCAL), str(PeerKind.REPOSITORY), str(PeerKind.SHARED)


def near_child(scope: ChildScope, row: LinkEdgeRow) -> str:
    """The child a row's near end belongs to."""
    if scope.is_file:
        return scope.declarations.get(row.near_id, scope.path)
    return child_path(scope.path, row.near_path)


def _peer(scope: ChildScope, row: LinkEdgeRow, child: str, outgoing: bool) -> _PeerKey | None:
    """The peer a row links ``child`` to, or ``None`` when the row is no link here.

    ``None`` for an edge between two parts of one child, for an edge into another
    repository unless crossing was asked for, and for the incoming reading of an
    edge between two children — its outgoing reading already counts it.
    """
    if row.far_repo_id is None:
        return (_SHARED, "", "", "", row.far_type)
    if row.far_repo_id != scope.repo_id:
        if not scope.cross_repository:
            return None
        slug = scope.slugs.get(row.far_repo_id)
        return None if slug is None else (_REPOSITORY, slug, "", "", "")
    if row.far_path is None:
        return (_LOCAL, scope.repository, "", row.far_key, "")
    if scope.is_file:
        if row.far_path != scope.path:
            return (_LOCAL, scope.repository, row.far_path, "", "")
        far_child = scope.path if row.far_type == EntityType.FILE else row.far_key
        if far_child == child or not outgoing:
            return None
        return (_LOCAL, scope.repository, scope.path, row.far_key, "")
    if within(scope.path, row.far_path):
        far_child = child_path(scope.path, row.far_path)
        if far_child == child or not outgoing:
            return None
        return (_LOCAL, scope.repository, far_child, "", "")
    depth = child.count("/") + 1
    return (_LOCAL, scope.repository, truncated(row.far_path, depth), "", "")


def child_groups(
    scope: ChildScope,
    outgoing: Iterable[LinkEdgeRow],
    incoming: Iterable[LinkEdgeRow],
    asserted: Iterable[LinkEdgeRow] = (),
) -> dict[_ChildKey, _Group]:
    """Every edge at the path's children, grouped by kind, child, direction and peer.

    ``asserted`` holds the edges of the repository's own entity, which has no file:
    each belongs to the child holding the file that asserts it, as an outgoing
    edge of that child.
    """
    groups: dict[_ChildKey, _Group] = {}
    for rows, is_outgoing in ((outgoing, True), (incoming, False), (asserted, True)):
        direction = _OUT if is_outgoing else _IN
        for row in rows:
            child = near_child(scope, row)
            peer = _peer(scope, row, child, is_outgoing)
            if peer is None:
                continue
            key = (str(_KIND_BY_TYPE[row.type]), child, direction, peer)
            group = groups.get(key)
            if group is None:
                group = groups[key] = _Group()
            order = (row.near_path, row.line_start or 0, row.far_key, row.type, row.edge_id)
            group.add(row.type, order, row.edge_id)
    return groups


def _topology_peer(peer: _PeerKey) -> TopologyPeer:
    kind, repository, path, key, entity_type = peer
    return TopologyPeer(
        kind=PeerKind(kind),
        repository=repository or None,
        path=path or None,
        key=key or None,
        entity_type=EntityType(entity_type) if entity_type else None,
    )


@dataclass(frozen=True, slots=True)
class RankedLink:
    """One child link, placed in the final order."""

    count: int
    kind: LinkKind
    child: str
    direction: str
    peer: _PeerKey
    group: _Group

    @property
    def order(self) -> tuple[int, int, str, str, _PeerKey]:
        return (-self.count, _KIND_ORDER[self.kind], self.child, self.direction, self.peer)


@dataclass(frozen=True, slots=True)
class BoundedChildLinks:
    """The links kept, in their final order, and how many there were."""

    kept: list[RankedLink]
    total: int
    truncated: bool

    def example_ids(self) -> list[int]:
        return [edge_id for link in self.kept for edge_id in link.group.example_ids()]


def bound_child_links(groups: Mapping[_ChildKey, _Group]) -> BoundedChildLinks:
    """Keep at most :data:`MAX_CHILD_LINKS`, largest first, in a stable order."""
    ranked = sorted(
        (
            RankedLink(
                count=group.count,
                kind=LinkKind(kind),
                child=child,
                direction=direction,
                peer=peer,
                group=group,
            )
            for (kind, child, direction, peer), group in groups.items()
        ),
        key=lambda link: link.order,
    )
    return BoundedChildLinks(
        kept=ranked[:MAX_CHILD_LINKS],
        total=len(ranked),
        truncated=len(ranked) > MAX_CHILD_LINKS,
    )


def child_links(bounded: BoundedChildLinks, refs: Mapping[int, str]) -> list[ChildLink]:
    """The kept links as the public model, in the order they were kept."""
    return [
        ChildLink(
            kind=link.kind,
            child=link.child,
            direction=RelationshipDirection(link.direction),
            peer=_topology_peer(link.peer),
            count=link.count,
            types=link.group.type_counts(),
            examples=example_refs(link.group.example_ids(), refs),
        )
        for link in bounded.kept
    ]


# ---------------------------------------------------------------------------
# Children entries and their cursor
# ---------------------------------------------------------------------------

EntryOrder = tuple[int | str, ...]


def page_limits(limit: int | None) -> AppliedPageLimits:
    wanted = limit if limit is not None else DEFAULT_CHILDREN_PAGE
    applied = min(wanted, MAX_CHILDREN_PAGE)
    return AppliedPageLimits(
        requested=limit,
        applied=applied,
        clamped=("limit",) if wanted > MAX_CHILDREN_PAGE else (),
        ceiling=MAX_CHILDREN_PAGE,
    )


def encode_children_cursor(repository: str, path: str, after: EntryOrder) -> str:
    raw = json.dumps({"r": repository, "p": path, "k": list(after)}, separators=(",", ":"))
    return _CURSOR_PREFIX + base64.urlsafe_b64encode(raw.encode("utf-8")).rstrip(b"=").decode()


def decode_children_cursor(token: str, repository: str, path: str) -> EntryOrder:
    """The position a cursor resumes after, refused unless it was made for this path."""
    if not token.startswith(_CURSOR_PREFIX):
        raise InvalidTopologyCursorError()
    body = token[len(_CURSOR_PREFIX) :]
    try:
        loaded = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise InvalidTopologyCursorError() from exc
    if not isinstance(loaded, dict) or loaded.get("r") != repository or loaded.get("p") != path:
        raise InvalidTopologyCursorError()
    after = loaded.get("k")
    if not isinstance(after, list) or not all(
        isinstance(v, int | str) and not isinstance(v, bool) for v in after
    ):
        raise InvalidTopologyCursorError()
    return tuple(after)


@dataclass(frozen=True, slots=True)
class Listed:
    """One entry, with the position it holds in the listing's fixed order."""

    order: EntryOrder
    entry: ChildEntry
    #: The file's row id, for a file entry whose declarations are still to count.
    file_id: int | None = None


def directory_entries(
    repository: str, prefix: str, files: Sequence[TopologyFileRow]
) -> list[Listed]:
    """The immediate children of a directory: subdirectories, then files, by name."""
    directories: Counter[str] = Counter()
    listed: list[Listed] = []
    for row in files:
        child = child_path(prefix, row.path)
        if child != row.path:
            directories[child] += 1
            continue
        name = row.path.rsplit("/", 1)[-1]
        listed.append(
            Listed(
                order=(1, name),
                entry=ChildEntry(
                    id=row.path,
                    kind=EntryKind.FILE,
                    name=name,
                    path=row.path,
                    key=file_key(repository, row.path),
                    type=EntityType.FILE,
                    language=row.language,
                ),
                file_id=row.file_id,
            )
        )
    for child, count in directories.items():
        name = child.rsplit("/", 1)[-1]
        listed.append(
            Listed(
                order=(0, name),
                entry=ChildEntry(
                    id=child, kind=EntryKind.DIRECTORY, name=name, path=child, files=count
                ),
            )
        )
    listed.sort(key=lambda item: item.order)
    return listed


def file_entries(path: str, declarations: Sequence[TopologyDeclarationRow]) -> list[Listed]:
    """The declarations of one file, in source order."""
    listed = [
        Listed(
            order=(row.line_start or 0, row.key, row.line_end or 0, row.type),
            entry=ChildEntry(
                id=row.key,
                kind=EntryKind.DECLARATION,
                name=row.name,
                path=path,
                key=row.key,
                type=EntityType(row.type),
                qualified_name=row.qualified_name,
                line_start=row.line_start,
                line_end=row.line_end,
            ),
        )
        for row in declarations
    ]
    listed.sort(key=lambda item: item.order)
    return listed


def page_of(
    listed: Sequence[Listed], after: EntryOrder | None, size: int
) -> tuple[list[Listed], EntryOrder | None]:
    """One page after ``after``, and the position to resume from if more remain."""
    remaining = [item for item in listed if after is None or item.order > after]
    page = remaining[:size]
    return page, (page[-1].order if len(remaining) > size else None)


def with_declaration_counts(page: Sequence[Listed], counts: Mapping[int, int]) -> list[ChildEntry]:
    return [
        item.entry.model_copy(update={"declarations": counts.get(item.file_id, 0)})
        if item.file_id is not None
        else item.entry
        for item in page
    ]


def context_view(context: CapabilityContext, resolution: ContextResolution) -> ContextView:
    return ContextView(
        context=context,
        scope=resolution.scope,
        repositories=tuple(sorted(repo.slug for repo in resolution.repositories)),
        primary=resolution.primary.slug if resolution.primary else None,
        reason=resolution.reason,
    )


def peer_repositories(links: Iterable[ChildLink]) -> set[str]:
    """Every repository a set of links names, for the freshness that covers them."""
    return {link.peer.repository for link in links if link.peer.repository is not None}
