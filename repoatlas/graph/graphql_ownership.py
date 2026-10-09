"""Which provider schema each coordinate of a composed (federated) schema comes from.

A pure planner: it reads indexed symbols and the decisions of other passes, and writes
nothing. A *supergraph* is a schema file that defines ``join__Graph``, each value naming a
subgraph and the URL it is served at (``@join__graph(name:, url:)``). Each type, field and
enum value of the supergraph says which subgraphs it comes from (``@join__type``,
``@join__field``, ``@join__enumValue``). A coordinate is tied to its provider only through
the whole chain, every link stated by the source:

1. the subgraph's URL, as an origin, is an identity deployment files tie to exactly one
   repository (:mod:`repoatlas.graph.deploy_identity`: an exposure, or an identity that
   reaches no HTTP route because the service only answers GraphQL), and that deployment
   runs the repository's own code -- never a host name that happens to match a
   repository's, and never a service whose image is built elsewhere;
2. that repository's served schema is what its schema constructions load
   (:mod:`repoatlas.graph.graphql_resolvers`);
3. the provider definition is the coordinate in those files.

Each tie is a ``GENERATES`` edge from the provider coordinate to the supergraph's, citing
the supergraph's lines, with the chain under ``resolution.graphql_composition``. A type
present on both sides whose members or fields differ is ``snapshot-stale``, the
differences listed: the copy is older or newer than what the provider serves.

A schema file that is neither served nor a supergraph is a copy whose provenance nothing
states: each of its types is ``composition-unproven``, and none is linked to anything.

Decisions (``link_decisions``, family ``graphql_ownership``), per type, subject
``<repository>:<path>::<Type>``: ``linked``, ``snapshot-stale``, ``provider-unproven`` (the
URL ties to no repository, or to several), ``schema-unserved`` (the repository states no
schema construction), ``provider-missing`` (the served schema lacks the type),
``provider-ambiguous`` (served schemas of the repository define it differently),
``composition-unproven``.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import NamedTuple
from urllib.parse import urlsplit

from repoatlas.models.parsing import EdgeType, EntityType
from repoatlas.storage.protocols import EdgeRecord, FileRow, LinkDecisionRow, SymbolRow

FAMILY = "graphql_ownership"
EVIDENCE = "graphql_composition"

LINKED = "linked"
SNAPSHOT_STALE = "snapshot-stale"
PROVIDER_UNPROVEN = "provider-unproven"
SCHEMA_UNSERVED = "schema-unserved"
PROVIDER_MISSING = "provider-missing"
PROVIDER_AMBIGUOUS = "provider-ambiguous"
COMPOSITION_UNPROVEN = "composition-unproven"

_JOIN_GRAPH = "join__Graph"
#: Federation's own machinery in a supergraph: never a provider's type.
_MACHINERY = ("join__", "link__")
_DEFAULT_PORTS = {"http": 80, "https": 443}


class Identity(NamedTuple):
    """One deployment identity of an origin, in one repository."""

    #: Where it is stated, as ``<file>:<line>[ via <file>:<line>]``.
    stated: str
    #: Whether the deployment runs that repository's code. One that runs an image built
    #: elsewhere still makes an origin ambiguous, but never provides it.
    runs_code: bool = True


@dataclass(frozen=True, slots=True)
class OwnershipPlan:
    edges: tuple[EdgeRecord, ...] = ()
    decisions: tuple[LinkDecisionRow, ...] = ()
    crossings: int = 0
    detail: str = ""


def origin_of(url: str) -> str | None:
    """``scheme://host:port`` of a URL, the default port spelled out; ``None`` if unreadable."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port or _DEFAULT_PORTS.get(parts.scheme)
    except ValueError:
        return None
    if not parts.scheme or not parts.hostname or port is None:
        return None
    return f"{parts.scheme}://{parts.hostname}:{port}"


@dataclass(slots=True)
class _Provider:
    """What one subgraph of a supergraph is served by, or why that is not known."""

    graph: str
    url: str
    repo_id: int | None = None
    #: Each construction's schema files in that repository.
    sets: tuple[tuple[FileRow, ...], ...] = ()
    failure: str | None = None
    #: Where the identity is stated.
    identity: str = ""


@dataclass(slots=True)
class _Type:
    edges: list[tuple[SymbolRow, SymbolRow, str]] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    failures: list[dict[str, str]] = field(default_factory=list)


def plan_ownership(
    files: Mapping[int, FileRow],
    by_file: Mapping[int, Sequence[SymbolRow]],
    origins: Mapping[str, Mapping[int, Identity]],
    served: Mapping[int, Sequence[Sequence[FileRow]]],
    fetched: Mapping[int, Sequence[str]] | None = None,
    *,
    producer: str,
) -> OwnershipPlan:
    """Tie every supergraph coordinate to its provider; mark every unproven copy. Writes nothing.

    ``origins`` maps an origin (:func:`origin_of`) to the repositories deployment identity
    ties it to, each with where the tie is stated.
    """
    planner = _Planner(files, by_file, origins, served, fetched or {}, producer)
    return planner.plan()


class _Planner:
    def __init__(
        self,
        files: Mapping[int, FileRow],
        by_file: Mapping[int, Sequence[SymbolRow]],
        origins: Mapping[str, Mapping[int, Identity]],
        served: Mapping[int, Sequence[Sequence[FileRow]]],
        fetched: Mapping[int, Sequence[str]],
        producer: str,
    ) -> None:
        self._fetched = fetched
        self._files = files
        self._by_file = by_file
        self._origins = origins
        self._served = {repo: tuple(tuple(s) for s in sets) for repo, sets in served.items()}
        self._served_files = {
            row.file_id for sets in self._served.values() for s in sets for row in s
        }
        self._producer = producer
        self._slugs = {row.repo_id: row.repo_slug for row in files.values()}
        self.decisions: dict[str, LinkDecisionRow] = {}
        self.edges: dict[tuple[int, int], EdgeRecord] = {}
        self.crossings = 0

    def plan(self) -> OwnershipPlan:
        for file_id in sorted(self._by_file, key=lambda f: self._sort_key(f)):
            row = self._files.get(file_id)
            if row is None:
                continue
            symbols = self._by_file[file_id]
            types = [
                s
                for s in symbols
                if s.type is EntityType.CLASS
                and s.meta.get("graphql")
                and s.meta.get("extension") != "true"
            ]
            if not types:
                continue
            graphs = self._graphs(symbols)
            if graphs:
                self._supergraph(row, symbols, types, graphs)
            elif file_id not in self._served_files:
                for symbol in types:
                    self._decide(row, symbol, COMPOSITION_UNPROVEN, {})
        decisions = sorted(self.decisions.values(), key=lambda r: r.subject)
        counts: dict[str, int] = defaultdict(int)
        for decision in decisions:
            counts[decision.decision] += 1
        detail = ", ".join(f"{n} {d}" for d, n in sorted(counts.items()))
        return OwnershipPlan(
            edges=tuple(sorted(self.edges.values(), key=lambda e: (e.src_id, e.dst_id))),
            decisions=tuple(decisions),
            crossings=self.crossings,
            detail=f"ownership: {detail or 'none'}",
        )

    def _sort_key(self, file_id: int) -> tuple[str, str]:
        row = self._files.get(file_id)
        return (row.repo_slug, row.rel_path) if row else ("", "")

    # -- a supergraph --------------------------------------------------------------

    def _graphs(self, symbols: Sequence[SymbolRow]) -> dict[str, str]:
        """``join__Graph`` value -> the URL its ``@join__graph`` states."""
        found: dict[str, str] = {}
        for symbol in symbols:
            name = symbol.qualified_name or ""
            if not name.startswith(f"{_JOIN_GRAPH}.") or symbol.meta.get("graphql") != "enum-value":
                continue
            for directive in _directives(symbol):
                arguments = directive.get("arguments")
                if directive.get("name") == "join__graph" and isinstance(arguments, dict):
                    url = _string(arguments.get("url"))
                    if url:
                        found[name.split(".", 1)[1]] = url
        return found

    def _supergraph(
        self,
        row: FileRow,
        symbols: Sequence[SymbolRow],
        types: Sequence[SymbolRow],
        graphs: Mapping[str, str],
    ) -> None:
        providers = {graph: self._provider(graph, url) for graph, url in sorted(graphs.items())}
        members: dict[str, list[SymbolRow]] = defaultdict(list)
        for symbol in symbols:
            name = symbol.qualified_name or ""
            if symbol.type is not EntityType.CLASS and "." in name:
                members[name.split(".", 1)[0]].append(symbol)
        for type_symbol in types:
            if type_symbol.name.startswith(_MACHINERY):
                continue
            owners = _joined(type_symbol, "join__type")
            if not owners:
                continue  # stated by no subgraph: nothing to tie
            found = _Type()
            for graph in owners:
                provider = providers.get(graph)
                if provider is None:
                    found.failures.append({"graph": graph, "failure": PROVIDER_UNPROVEN})
                    continue
                self._type(found, provider, type_symbol, members.get(type_symbol.name, []), owners)
            self._record(row, type_symbol, found, providers)

    def _provider(self, graph: str, url: str) -> _Provider:
        provider = _Provider(graph=graph, url=url)
        origin = origin_of(url)
        repos = dict(self._origins.get(origin, {})) if origin else {}
        if len(repos) != 1:
            provider.failure = PROVIDER_UNPROVEN
            return provider
        [(repo_id, identity)] = repos.items()
        provider.identity = identity.stated
        if not identity.runs_code:
            provider.failure = PROVIDER_UNPROVEN
            return provider
        provider.repo_id = repo_id
        provider.sets = self._served.get(provider.repo_id, ())
        if not provider.sets:
            provider.failure = SCHEMA_UNSERVED
        return provider

    def _type(
        self,
        found: _Type,
        provider: _Provider,
        type_symbol: SymbolRow,
        members: Sequence[SymbolRow],
        owners: Sequence[str],
    ) -> None:
        if provider.failure is not None:
            found.failures.append({"graph": provider.graph, "failure": provider.failure})
            return
        definitions = [self._definitions(s, type_symbol.name) for s in provider.sets]
        distinct = {tuple(sorted(_shape(d))) for d in definitions if d}
        present = [d for d in definitions if d]
        if not present:
            found.failures.append({"graph": provider.graph, "failure": PROVIDER_MISSING})
            return
        if len(distinct) > 1:
            found.failures.append({"graph": provider.graph, "failure": PROVIDER_AMBIGUOUS})
            return
        provided = present[0]
        for symbol in provided.get(type_symbol.name, []):
            if symbol.type is EntityType.CLASS:
                found.edges.append((symbol, type_symbol, provider.graph))
        # Members this subgraph states: those its join directive names it on, else every
        # member of a type it states; an argument goes with its field.
        by_name = {m.qualified_name: m for m in members}
        expected: set[str] = set()
        for member in members:
            role = member.meta.get("graphql")
            if role == "enum-value":
                graphs = _joined(member, "join__enumValue")
            elif role == "argument":
                graphs = _joined(
                    by_name.get((member.qualified_name or "").split("(", 1)[0]), "join__field"
                )
            else:
                graphs = _joined(member, "join__field")
            if provider.graph not in (graphs or owners):
                continue
            coordinate = member.qualified_name or ""
            expected.add(coordinate)
            sources = [s for s in provided.get(coordinate, []) if s.type is not EntityType.CLASS]
            if not sources:
                found.stale.append(f"missing in provider: {coordinate}")
                continue
            for source in sources:
                found.edges.append((source, member, provider.graph))
        if type_symbol.meta.get("graphql") == "enum":
            for coordinate, sources in sorted(provided.items()):
                if (
                    coordinate.startswith(f"{type_symbol.name}.")
                    and coordinate not in expected
                    and any(s.meta.get("graphql") == "enum-value" for s in sources)
                ):
                    found.stale.append(f"missing in copy: {coordinate}")

    def _definitions(self, schema: Sequence[FileRow], type_name: str) -> dict[str, list[SymbolRow]]:
        """The type and its members, by coordinate, in one served schema."""
        found: dict[str, list[SymbolRow]] = defaultdict(list)
        prefix = f"{type_name}."
        for row in schema:
            for symbol in self._by_file.get(row.file_id, ()):
                name = symbol.qualified_name or ""
                if (
                    symbol.type is EntityType.CLASS
                    and symbol.name == type_name
                    and symbol.meta.get("graphql")
                ):
                    found[type_name].append(symbol)
                elif name.startswith(prefix) and symbol.meta.get("graphql"):
                    found[name].append(symbol)
        return dict(found) if type_name in found else {}

    # -- records -------------------------------------------------------------------

    def _record(
        self, row: FileRow, type_symbol: SymbolRow, found: _Type, providers: Mapping[str, _Provider]
    ) -> None:
        evidence: dict[str, object] = {
            "graphs": [
                {
                    "graph": p.graph,
                    "url": p.url,
                    "repository": self._slug(p.repo_id),
                    "identity": p.identity,
                }
                for p in providers.values()
                if p.graph in _joined(type_symbol, "join__type")
            ],
        }
        if row.file_id in self._fetched:
            evidence["fetched_by"] = list(self._fetched[row.file_id])
        if found.failures:
            evidence["failures"] = found.failures
            decision = found.failures[0]["failure"]
            ref = self._decide(row, type_symbol, decision, evidence)
            return
        if found.stale:
            evidence["stale"] = sorted(set(found.stale))
            ref = self._decide(row, type_symbol, SNAPSHOT_STALE, evidence)
        else:
            ref = self._decide(row, type_symbol, LINKED, evidence)
        for source, target, graph in found.edges:
            self._edge(source, target, row, graph, providers[graph], ref)

    def _edge(
        self,
        source: SymbolRow,
        target: SymbolRow,
        row: FileRow,
        graph: str,
        provider: _Provider,
        ref: str,
    ) -> None:
        key = (source.entity_id, target.entity_id)
        if key in self.edges:
            return
        if source.repo_id != target.repo_id:
            self.crossings += 1
        source_file = self._files.get(source.file_id)
        self.edges[key] = EdgeRecord(
            src_id=source.entity_id,
            dst_id=target.entity_id,
            type=EdgeType.GENERATES,
            repo_id=row.repo_id,
            file_id=row.file_id,
            line_start=target.line_start,
            line_end=target.line_end if target.line_end is not None else target.line_start,
            parser=self._producer,
            resolution=json.dumps(
                {
                    EVIDENCE: {
                        "graph": graph,
                        "url": provider.url,
                        "identity": provider.identity,
                        "provider": f"{self._slug(source.repo_id)}/"
                        f"{source_file.rel_path if source_file else '?'}:{source.line_start}",
                        "decision_ref": ref,
                    }
                },
                sort_keys=True,
            ),
        )

    def _decide(
        self, row: FileRow, symbol: SymbolRow, decision: str, evidence: dict[str, object]
    ) -> str:
        subject = f"{row.repo_slug}:{row.rel_path}::{symbol.name}"
        ref = hashlib.sha256(f"{FAMILY}\0{subject}".encode()).hexdigest()[:24]
        self.decisions[ref] = LinkDecisionRow(
            decision_ref=ref,
            family=FAMILY,
            repo_id=row.repo_id,
            file_id=row.file_id,
            src_id=symbol.entity_id,
            line_start=symbol.line_start,
            line_end=symbol.line_end,
            subject=subject,
            decision=decision,
            evidence_json=json.dumps(evidence, sort_keys=True, separators=(",", ":")),
        )
        return ref

    def _slug(self, repo_id: int | None) -> str | None:
        return self._slugs.get(repo_id) if repo_id is not None else None


def _directives(symbol: SymbolRow | None) -> list[dict[str, object]]:
    if symbol is None:
        return []
    raw = symbol.meta.get("directives")
    decoded = json.loads(raw) if raw else []
    return [d for d in decoded if isinstance(d, dict)]


def _joined(symbol: SymbolRow | None, directive: str) -> list[str]:
    """The subgraphs a ``@join__*`` directive names on a symbol, in order."""
    graphs: list[str] = []
    for applied in _directives(symbol):
        if applied.get("name") == directive:
            arguments = applied.get("arguments")
            graph = arguments.get("graph") if isinstance(arguments, dict) else None
            if isinstance(graph, str) and graph and graph not in graphs:
                graphs.append(graph)
    return graphs


def _string(printed: object) -> str | None:
    """A GraphQL string literal as printed, unquoted."""
    if not isinstance(printed, str):
        return None
    try:
        value = json.loads(printed)
    except ValueError:
        return None
    return value if isinstance(value, str) else None


def _shape(definitions: Mapping[str, Sequence[SymbolRow]]) -> list[str]:
    return sorted(definitions)
