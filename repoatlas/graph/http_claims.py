"""Pending HTTP facts, decided from everything indexed (HTTP linkage).

A parser records some HTTP facts as *pending* (:mod:`repoatlas.parsers.http_facts`)
because one file cannot prove them. This stage of the global link pass reads all
of them together and publishes only what the indexed source establishes.

**NestJS routes.** A controller-local route becomes ``SERVES_HTTP_ROUTE`` on the
shared route node only when an application is proven to serve it:

* a ``NestFactory.create`` root names its root module directly and proves its
  global prefix and versioning (:mod:`repoatlas.parsers.http_nestjs` decides what
  that takes);
* the controller's registration path is established — the root module reaches the
  declaring module through ``imports`` entries that each resolve to exactly one
  indexed module class, and that module lists the controller;
* nothing that can change routes is left unread where it could reach them.

Under URI versioning a route is served once per version it has — its handler's
``@Version``, else its controller's ``version``, else the application's
``defaultVersion`` — at ``/<prefix>/<v><version>/<controller>/<route>``.
``VERSION_NEUTRAL`` adds no segment; a route with no version at all is withheld,
because NestJS serves it at no URI this can name.

An unknown is never read as "no effect", and it blocks only what it can reach:

* *application-wide* — an unproven prefix, an unreadable module on the path, an
  in-repository import that does not resolve, a ``RouterModule`` entry, an
  unresolvable dynamic module: unseen code could re-route any module, so no
  controller of that application is proven;
* *package-wide* — a file importing ``RouterModule`` blocks every application in
  its package (the nearest ``package.json``); one importing ``ApplicationConfig``,
  or ``@nestjs/core`` without a named list, can set any application's prefix, so
  it blocks every application in its repository;
* *per module* — a module of this repository handed to a dynamic module
  (``X.forRootAsync({ imports: [M] })``) could be re-prefixed by it, so ``M``'s own
  controllers are not proven. Controllers a dynamic module registers are never
  assumed registered.

A package module (a bare npm specifier) handed none of this repository's modules
cannot name them, so it blocks nothing. A package that changes routing at run time
is outside what static reading can see; that boundary is documented, not assumed
away.

**Requests through imported helpers.** A client file records a call through a
helper imported from a relative module — ``const { request } =
createRequester(options)`` with ``createRequester`` from ``./http`` — as a pending
claim, and the helper's file declares what its exported functions do
(:mod:`repoatlas.parsers.http_ts_wrappers`). The two are joined across **exactly
one import**: the declaration must be in the module the import resolves to,
and must be the only one of its name there. A re-export, a package, a module
that does not resolve, or an argument the client file could not read yields
nothing. A call to an imported function that turns out to be no request helper
is not an HTTP claim at all, so it is not counted as unresolved.

**Requests whose path is a template.** A call whose path is a rooted template —
through an imported helper, or through a same-file wrapper
(:data:`~repoatlas.parsers.http_facts.HTTP_LOCAL_CALL`) — names a route *shape*, not
a route key. It is matched only against the routes **its own repository** serves:
per-file ``SERVES_HTTP_ROUTE`` edges, and the NestJS routes this same plan proves.
Never the previous run's output, which would make the pass depend on itself, and
never another repository's routes, which would be a cross-repository relationship
nobody wrote. The shape rule is :func:`repoatlas.models.http.template_matches`:

* a known method links to **every** route key of that method with the shape.
  Those can differ only in parameter spelling — an OpenAPI ``{id}`` beside a
  handler's ``:id`` — so multiple providers stay multiple rather than one being
  chosen; the edge has no lines, because the spelling is the server's;
* a method this file cannot read never links: the compatible routes are recorded
  as candidates, and the call is *ambiguous* or has *no indexed handler*;
* a direct ``fetch`` template is recorded and never matched, and a template that
  cannot be read as a shape keeps its refusal code.

Every decision is kept per claim (:attr:`HttpClaimPlan.calls`), in terms that
describe indexed static facts in one repository — never source-wide absence.

A route that is not proven stays exactly what it was: a claim that records the
declaration and the limitation. The resolved registration entries also become
ordinary ``REFERENCES`` edges — module to controller, module to module, bootstrap
to root module — which are facts about the source whatever the routes turn out to
be.

Everything here writes nothing: it returns edges for the swap to apply, which is
what keeps the global pass a replacement (ARCHITECTURE.md §9).
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from repoatlas.graph.deploy_identity import ServedRoute, plan_deployment_exposures
from repoatlas.graph.keys import global_key
from repoatlas.graph.py_modules import PythonModules
from repoatlas.graph.py_routes import plan_python_routes
from repoatlas.graph.resolution import ResolutionIndex
from repoatlas.graph.ts_injection import plan_injected_requests
from repoatlas.graph.ts_oas3 import plan_spec_routed
from repoatlas.graph.ts_routes import plan_fastify_routes
from repoatlas.models import http
from repoatlas.models.parsing import EdgeType, EntityType
from repoatlas.parsers.http_facts import (
    EXACT,
    HTTP_INIT_HELPER,
    HTTP_LOCAL_CALL,
    HTTP_REQUESTER,
    HTTP_REQUESTER_CALL,
    HTTP_WRAPPER,
    NESTJS_CONTROLLER,
    NESTJS_IMPORT,
    NESTJS_MARKER,
    NESTJS_MODULE,
    NESTJS_ROOT,
    NESTJS_ROUTE,
    PENDING_FACT,
    TRANSPORT_FETCH,
    TRANSPORT_WRAPPER,
)
from repoatlas.parsers.http_nestjs import (
    DYNAMIC,
    GLOBAL_MARKER,
    MODULE,
    PROVEN,
    ROUTER,
    ROUTER_MARKER,
    nest_path,
)
from repoatlas.parsers.http_ts_wrappers import (
    GET,
    Method,
    Silent,
    Wrapper,
    apply_helper,
    evaluate,
    helper_from,
    is_relative,
    wrapper_from,
)
from repoatlas.parsers.http_typescript import template_parts
from repoatlas.storage.protocols import (
    EdgeRecord,
    EntityRecord,
    FileRow,
    RouteServerRow,
    UnresolvedRefRecord,
)

#: More modules than any application registers; a bound on adversarial input.
_MAX_MODULES = 4096
#: Parsers whose route edges a specification document states: a document is no application.
_NOT_APPLICATIONS = frozenset({"structured-config"})

PROMOTED = "promoted"
#: A call to an imported function that is no request helper: not an HTTP claim.
NOT_HTTP = "no request helper of that name is declared in the imported module"
NOT_LITERAL = "the path is not a literal"
METHOD_UNREAD = "the method cannot be read"
NO_ROUTE = "no route served in this repository matches the path"
UNREADABLE_TEMPLATE = "the path template cannot be read"
NAMED_VALUE = "the path is a named value this file cannot read"
DIRECT_FETCH = "a direct fetch template is recorded, never matched"
OLDER_PARSER = "recorded by an older parser; the next sync of its repository re-reads it"

# -- call decisions -------------------------------------------------------------------
#
# What a client call is, measured against the routes indexed as served in its own
# repository. Each status describes indexed static facts in that one repository:
# "no indexed handler" is not "the backend has no implementation", only that
# nothing this repository's indexed source declares matches.

#: Path and method established; at least one route served in the repository matches.
MATCHED = "matched"
#: The path is established, the method cannot be read, and compatible routes exist.
AMBIGUOUS = "ambiguous"
#: The path is established and no route indexed as served in the repository matches.
NO_INDEXED_HANDLER = "no_indexed_handler"
#: The path cannot be established.
UNRESOLVED = "unresolved"

#: Reason codes, stable: the inventory surfaces them verbatim.
REASON_EXACT = "exact"
REASON_STRUCTURAL = "structural"
REASON_NO_ROUTE = "no_served_route"
REASON_METHOD_UNREAD = "method_unread"
REASON_NOT_LITERAL = "not_literal"
REASON_NAMED_VALUE = "named_value"
REASON_DIRECT_FETCH = "direct_fetch_template"
REASON_OLDER_PARSER = "recorded_by_older_parser"


@dataclass(frozen=True, slots=True)
class CallDecision:
    """What the global pass decided about one path a client call requests."""

    status: str
    reason: str
    #: The method, or ``None`` when it cannot be read.
    method: str | None
    #: What the source wrote: a literal path, a template with ``${…}`` segments, or
    #: a named value. Path only — never a query.
    route: str
    #: Route bodies a route indexed as served in this repository matches.
    matched: tuple[str, ...] = ()
    #: Compatible routes it does not link to: under another method, or under any
    #: method when its own cannot be read.
    candidates: tuple[str, ...] = ()
    #: Whether something outside the call's own lines decided the method or the
    #: route: a helper default, a wrapper base, the server's parameter spelling.
    composed: bool = False
    #: The route template read from the source, when the path had a shape.
    template: http.RouteTemplate | None = None
    #: Route bodies this call has an edge to. A literal path always has one — its
    #: route node exists whether or not anything here serves it — so this can
    #: hold more than ``matched``; a shape links only where it matched.
    linked: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PendingHttpEdge:
    """An edge to a shared HTTP node that is named but not yet interned."""

    src_id: int
    target: EntityRecord
    type: EdgeType
    repo_id: int
    file_id: int
    line_start: int | None
    line_end: int | None


@dataclass(frozen=True, slots=True)
class HttpClaimPlan:
    """What the pending HTTP facts establish, computed but not applied."""

    #: Edges between entities that already exist.
    edges: tuple[EdgeRecord, ...] = ()
    #: Edges to shared HTTP nodes, interned when the plan is applied.
    http: tuple[PendingHttpEdge, ...] = ()
    resolved: int = 0
    unresolved: int = 0
    #: Claim id → ``promoted`` or why not: for diagnostics, never for identity.
    routes: dict[int, str] = field(default_factory=dict)
    #: Root claim id → ``proven`` or why not.
    roots: dict[int, str] = field(default_factory=dict)
    #: Imported-helper call claim id → ``promoted`` or why not.
    requests: dict[int, str] = field(default_factory=dict)
    #: Client call claim id (imported helper or local) → one decision per path it
    #: requests. Diagnostics and inventory, never identity.
    calls: dict[int, tuple[CallDecision, ...]] = field(default_factory=dict)
    #: Route declaration claim id → the route bodies it was proven to serve.
    promoted: dict[int, tuple[str, ...]] = field(default_factory=dict)
    #: Route declaration claim id → its controller-relative route body, for every
    #: declaration whose mount is not proven. A hint for readers; never an edge.
    declared: dict[int, str] = field(default_factory=dict)
    #: The join claims requests on an injected transport make, one per root that
    #: reaches them (repoatlas.graph.ts_injection): for the cross-repository join.
    injected: tuple[UnresolvedRefRecord, ...] = ()
    #: The exposure claims deployment identities derive for the routes their code serves
    #: (repoatlas.graph.deploy_identity): for the cross-repository join.
    exposed: tuple[UnresolvedRefRecord, ...] = ()
    #: Identities a deployment resolves that reach no code, and the Ingress host patterns
    #: no local deployment resolves, for the join's refusals: derived, never stored.
    identities: tuple[UnresolvedRefRecord, ...] = ()
    #: Deployment claim id → ``tied`` or why its identity reaches no code.
    deployments: dict[int, str] = field(default_factory=dict)
    #: The routes code serves, with the applications serving them: what the global pass's
    #: artifact join ties CI-established deployments to (repoatlas.graph.deploy_artifacts).
    code_routes: tuple[ServedRoute, ...] = ()

    @property
    def detail(self) -> str:
        promoted = sum(1 for outcome in self.routes.values() if outcome == PROMOTED)
        requests = sum(1 for outcome in self.requests.values() if outcome == PROMOTED)
        shaped = sum(
            1
            for decisions in self.calls.values()
            if any(d.reason == REASON_STRUCTURAL and d.status == MATCHED for d in decisions)
        )
        return (
            f"{promoted} of {len(self.routes)} declared route(s) proven, "
            f"{requests} request(s) through imported helpers, "
            f"{shaped} call(s) matched by route shape"
        )


@dataclass(slots=True)
class _Module:
    readable: bool
    imports: list[UnresolvedRefRecord] = field(default_factory=list)
    controllers: list[UnresolvedRefRecord] = field(default_factory=list)


def _meta(claim: UnresolvedRefRecord) -> dict[str, str]:
    loaded = json.loads(claim.meta_json) if claim.meta_json else {}
    return {str(k): str(v) for k, v in loaded.items()}


def is_package(specifier: str) -> bool:
    """Whether ``specifier`` names an npm package rather than code in this tree.

    ``./x``, ``/x``, ``#x`` and ``~/x`` are paths or aliases into the importing
    repository, and so is ``@/x``, which is not a valid package scope.
    """
    if not specifier or specifier.startswith((".", "/", "#", "~")):
        return False
    if specifier.startswith("@"):
        scope, _, rest = specifier.partition("/")
        return len(scope) > 1 and bool(rest)
    return True


class _Resolver:
    """Exact resolution of the class a claim names, and nothing looser."""

    def __init__(self, index: ResolutionIndex, files: dict[int, FileRow]) -> None:
        self._index = index
        self._files = files

    def origin(self, claim: UnresolvedRefRecord) -> FileRow | None:
        return self._files.get(claim.file_id)

    def local_class(self, file_id: int, name: str) -> int | None:
        matches = [
            symbol
            for symbol in self._index.symbols_by_file.get(file_id, {}).get(name, [])
            if symbol.type is EntityType.CLASS and symbol.name == name
        ]
        return matches[0].entity_id if len(matches) == 1 else None

    def binding(
        self, origin: FileRow, name: str, meta: dict[str, str], prefix: str = ""
    ) -> tuple[str, int | None]:
        """``(status, class entity id)`` for a name bound as ``meta`` says.

        ``status`` is ``class`` when exactly one indexed class is named, ``package``
        for an npm package, and ``unresolved`` for anything in this tree that does
        not resolve exactly — which callers must treat as unknown, never as absent.
        """
        if meta.get(f"{prefix}local") == "true":
            found = self.local_class(origin.file_id, name)
            return ("class", found) if found is not None else ("unresolved", None)
        specifier = meta.get(f"{prefix}specifier")
        if not specifier:
            return "unresolved", None
        if is_package(specifier):
            return "package", None
        imported = meta.get(f"{prefix}imported", "")
        if imported in {"", "*", "default"}:
            return "unresolved", None
        target = self._index.resolve_module(origin, specifier, "node")
        found = self.local_class(target.file_id, imported) if target is not None else None
        return ("class", found) if found is not None else ("unresolved", None)

    def module(self, origin: FileRow, specifier: str) -> FileRow | None:
        """The module a relative import names, when it resolves to an indexed file."""
        if not is_relative(specifier):
            return None
        return self._index.resolve_module(origin, specifier, "node")

    def module_file(self, origin: FileRow, specifier: str) -> bool:
        return self._index.resolve_module(origin, specifier, "node") is not None

    def package_of(self, origin: FileRow) -> tuple[int, str]:
        """The nearest directory holding an indexed ``package.json``."""
        files = self._index.files_by_repo.get(origin.repo_id, {})
        for parent in PurePosixPath(origin.rel_path).parents:
            base = "" if str(parent) == "." else str(parent)
            if (f"{base}/package.json" if base else "package.json") in files:
                return origin.repo_id, base
        return origin.repo_id, ""


def package_of(index: ResolutionIndex, origin: FileRow) -> str:
    """The directory of the nearest indexed ``package.json`` above a file, ``""`` for the root."""
    return _Resolver(index, {}).package_of(origin)[1]


def plan_http_claims(
    claims: Sequence[UnresolvedRefRecord],
    index: ResolutionIndex,
    files: Iterable[FileRow],
    *,
    producer: str,
    served_routes: Mapping[int, Iterable[str]] | None = None,
    module_facts: Sequence[UnresolvedRefRecord] = (),
    route_servers: Sequence[RouteServerRow] = (),
) -> HttpClaimPlan:
    """Decide every pending HTTP fact at once. Writes nothing.

    ``producer`` is recorded on every edge, so the pass that owns them can delete
    exactly its own output and rebuild it. ``served_routes`` is, per repository,
    the route bodies its files serve on their own — per-file facts, never this
    pass's previous output — which template calls are matched against together
    with the routes this plan proves. ``module_facts`` are the module-resolution
    facts Python routes mounted from another module are resolved with
    (:mod:`repoatlas.graph.py_routes`).
    """
    by_id = {row.file_id: row for row in files}
    resolver = _Resolver(index, by_id)
    grouped: dict[str, list[UnresolvedRefRecord]] = defaultdict(list)
    for claim in claims:
        grouped[_meta(claim).get(PENDING_FACT, "")].append(claim)

    edges: list[EdgeRecord] = []
    resolved = unresolved = 0

    # -- modules and what they register -------------------------------------------------
    modules: dict[int, _Module] = {}
    for claim in grouped[NESTJS_MODULE]:
        modules.setdefault(claim.src_id, _Module(_meta(claim).get("readable") == "true"))
    for role, attribute in ((NESTJS_IMPORT, "imports"), (NESTJS_CONTROLLER, "controllers")):
        for claim in grouped[role]:
            module = modules.get(claim.src_id)
            if module is not None:
                getattr(module, attribute).append(claim)

    # Registration entries naming exactly one indexed class are facts in their own
    # right, whatever the routes turn out to be.
    for role in (NESTJS_IMPORT, NESTJS_CONTROLLER, NESTJS_ROOT):
        for claim in grouped[role]:
            meta = _meta(claim)
            origin = resolver.origin(claim)
            target: int | None = None
            if origin is not None and meta.get("kind", MODULE) == MODULE:
                _, target = resolver.binding(origin, claim.target_key, meta)
            if origin is None or target is None:
                unresolved += 1
                continue
            resolved += 1
            edges.append(
                EdgeRecord(
                    src_id=claim.src_id,
                    dst_id=target,
                    type=EdgeType.REFERENCES,
                    repo_id=origin.repo_id,
                    file_id=origin.file_id,
                    line_start=claim.line_start,
                    line_end=claim.line_end,
                    parser=producer,
                )
            )

    # -- markers ------------------------------------------------------------------------
    routed_packages: set[tuple[int, str]] = set()
    configured_repositories: set[int] = set()
    for claim in grouped[NESTJS_MARKER]:
        origin = resolver.origin(claim)
        if origin is None:
            continue
        marker = _meta(claim).get("marker")
        if marker == ROUTER_MARKER:
            routed_packages.add(resolver.package_of(origin))
        elif marker == GLOBAL_MARKER:
            configured_repositories.add(origin.repo_id)

    # -- applications -------------------------------------------------------------------
    served: dict[int, set[tuple[str, str]]] = defaultdict(set)
    withheld: dict[int, str] = {}
    root_outcomes: dict[int, str] = {}
    for claim in grouped[NESTJS_ROOT]:
        outcome = _application(
            claim, resolver, modules, routed_packages, configured_repositories, served, withheld
        )
        if claim.id is not None:
            root_outcomes[claim.id] = outcome

    # -- routes -------------------------------------------------------------------------
    pending: dict[tuple[int, str], PendingHttpEdge] = {}
    route_outcomes: dict[int, str] = {}
    promoted_routes: dict[int, tuple[str, ...]] = {}
    declared_routes: dict[int, str] = {}
    for claim in grouped[NESTJS_ROUTE]:
        meta = _meta(claim)
        origin = resolver.origin(claim)
        controller = (
            resolver.local_class(origin.file_id, meta.get("controller", ""))
            if origin is not None
            else None
        )
        mounts = served.get(controller, set()) if controller is not None else set()
        promoted = False
        # registered, but under URI versioning with no version to serve it at
        versionless = bool(mounts)
        bodies: list[str] = []
        for prefix, versioning in sorted(mounts):
            segments = _segments(versioning, meta)
            versionless = versionless and not segments
            for segment in segments:
                path = nest_path(
                    prefix, segment, meta.get("controller_path", ""), meta.get("method_path", "")
                )
                body = http.route_body(meta.get("method", ""), path) if path is not None else ""
                if origin is None or not body or http.canonical_route(body) is None:
                    continue
                # One decorator states the whole route only when nothing else added to it.
                exact = (
                    not prefix
                    and not segment
                    and not meta.get("controller_path")
                    and meta.get("provenance") == EXACT
                )
                pending.setdefault(
                    (claim.src_id, body),
                    PendingHttpEdge(
                        src_id=claim.src_id,
                        target=EntityRecord(
                            type=EntityType.HTTP_ROUTE,
                            key=global_key(EntityType.HTTP_ROUTE, body),
                            name=body,
                            qualified_name=body,
                        ),
                        type=EdgeType.SERVES_HTTP_ROUTE,
                        repo_id=origin.repo_id,
                        file_id=origin.file_id,
                        line_start=claim.line_start if exact else None,
                        line_end=claim.line_end if exact else None,
                    ),
                )
                promoted = True
                bodies.append(body)
        if promoted:
            resolved += 1
        else:
            unresolved += 1
        if claim.id is not None:
            if promoted:
                promoted_routes[claim.id] = tuple(bodies)
            else:
                relative = nest_path(meta.get("controller_path", ""), meta.get("method_path", ""))
                body = http.route_body(meta.get("method", ""), relative) if relative else ""
                if body and http.canonical_route(body) is not None:
                    declared_routes[claim.id] = body
            if promoted:
                route_outcomes[claim.id] = PROMOTED
            elif controller is None:
                route_outcomes[claim.id] = "controller not indexed"
            elif versionless:
                route_outcomes[claim.id] = "URI versioning names no version for it"
            else:
                route_outcomes[claim.id] = withheld.get(
                    controller, "not registered by a proven application"
                )

    # -- Python routes mounted from another module ----------------------------------
    python = plan_python_routes(grouped, lambda: PythonModules(index, module_facts), index, by_id)
    for route in python.routes:
        _keep(
            pending,
            (route.src_id, route.body),
            PendingHttpEdge(
                src_id=route.src_id,
                target=route.target,
                type=EdgeType.SERVES_HTTP_ROUTE,
                repo_id=route.repo_id,
                file_id=route.file_id,
                line_start=route.line_start,
                line_end=route.line_end,
            ),
        )
    route_outcomes.update(python.outcomes)
    promoted_routes.update(python.promoted)
    declared_routes.update(python.declared)
    resolved += python.resolved
    unresolved += python.unresolved

    # -- Fastify routes registered from another module -------------------------------
    fastify = plan_fastify_routes(grouped, index, by_id)
    for route in fastify.routes:
        _keep(
            pending,
            (route.src_id, route.body),
            PendingHttpEdge(
                src_id=route.src_id,
                target=route.target,
                type=EdgeType.SERVES_HTTP_ROUTE,
                repo_id=route.repo_id,
                file_id=route.file_id,
                line_start=route.line_start,
                line_end=route.line_end,
            ),
        )
    route_outcomes.update(fastify.outcomes)
    promoted_routes.update(fastify.promoted)
    declared_routes.update(fastify.declared)
    resolved += fastify.resolved
    unresolved += fastify.unresolved

    # -- operations an oas3-tools application routes to its controllers -------------
    oas3 = plan_spec_routed(grouped, index, by_id)
    for route in oas3.routes:
        _keep(
            pending,
            (route.src_id, route.body),
            PendingHttpEdge(
                src_id=route.src_id,
                target=route.target,
                type=EdgeType.SERVES_HTTP_ROUTE,
                repo_id=route.repo_id,
                file_id=route.file_id,
                line_start=route.line_start,
                line_end=route.line_end,
            ),
        )
    route_outcomes.update(oas3.outcomes)
    promoted_routes.update(oas3.promoted)
    resolved += oas3.resolved
    unresolved += oas3.unresolved

    # -- what each repository serves ----------------------------------------------------
    # Per-file routes and the routes proven above, and nothing else: template calls
    # are matched within their own repository only.
    serving: dict[int, dict[str, set[str]]] = {}
    for repo_id, served_bodies in (served_routes or {}).items():
        for served_body in served_bodies:
            _serve(serving, repo_id, served_body)
    for edge in pending.values():
        if edge.type is EdgeType.SERVES_HTTP_ROUTE and edge.target.name:
            _serve(serving, edge.repo_id, edge.target.name)

    # -- requests through imported helpers ---------------------------------------------
    declared: dict[tuple[int, str, str], list[UnresolvedRefRecord]] = defaultdict(list)
    for role in (HTTP_REQUESTER, HTTP_WRAPPER, HTTP_INIT_HELPER):
        for claim in grouped[role]:
            declared[(claim.file_id, role, claim.target_key)].append(claim)
    request_outcomes: dict[int, str] = {}
    call_decisions: dict[int, tuple[CallDecision, ...]] = {}
    for claim in grouped[HTTP_REQUESTER_CALL]:
        outcome, found, decisions = _requester_call(claim, resolver, declared, serving)
        for edge in found:
            _keep(pending, (edge.src_id, f"{edge.type}:{edge.target.key}"), edge)
        if outcome == PROMOTED:
            resolved += 1
        elif outcome != NOT_HTTP:
            unresolved += 1
        if claim.id is not None:
            request_outcomes[claim.id] = outcome
            if decisions:
                call_decisions[claim.id] = decisions

    # -- requests this file makes but cannot decide --------------------------------------
    for claim in grouped[HTTP_LOCAL_CALL]:
        outcome, found, decisions = _local_call(claim, resolver, serving)
        for edge in found:
            _keep(pending, (edge.src_id, f"{edge.type}:{edge.target.key}"), edge)
        if outcome == PROMOTED:
            resolved += 1
        else:
            unresolved += 1
        if claim.id is not None and decisions:
            call_decisions[claim.id] = decisions

    # -- service identities a deployment declares, and the code they reach -----------
    code_routes = [
        ServedRoute(
            repo_id=row.repo_id,
            file_id=row.file_id,
            src_id=row.src_id,
            body=row.body,
            line_start=row.line_start,
            line_end=row.line_end,
            roots=frozenset({row.file_id}),
            declared_at=row.line_start,
        )
        for row in route_servers
        if row.file_id is not None and row.parser not in _NOT_APPLICATIONS
    ]
    # Routes this pass promoted, with the applications that serve them where their stage
    # traces them; an untraced one (a NestJS route, say) has none, and so leaves its
    # repository's applications unknown.
    traced = {
        (route.src_id, route.body): route
        for route in (*python.routes, *fastify.routes, *oas3.routes)
    }
    for edge in pending.values():
        if edge.type is not EdgeType.SERVES_HTTP_ROUTE or edge.file_id is None:
            continue
        route = traced.get((edge.src_id, edge.target.name))
        code_routes.append(
            ServedRoute(
                repo_id=edge.repo_id,
                file_id=edge.file_id,
                src_id=edge.src_id,
                body=edge.target.name,
                line_start=edge.line_start,
                line_end=edge.line_end,
                roots=route.roots if route is not None else frozenset(),
                declared_at=route.declared_at if route is not None else edge.line_start,
            )
        )
    deployed = plan_deployment_exposures(grouped, by_id, code_routes)

    return HttpClaimPlan(
        edges=tuple(edges),
        http=tuple(pending.values()),
        resolved=resolved,
        unresolved=unresolved,
        routes=route_outcomes,
        roots=root_outcomes,
        requests=request_outcomes,
        calls=call_decisions,
        promoted=promoted_routes,
        declared=declared_routes,
        injected=plan_injected_requests(grouped, index, by_id),
        exposed=tuple(deployed.exposures),
        identities=(*deployed.identities, *deployed.patterns),
        deployments=deployed.outcomes,
        code_routes=tuple(code_routes),
    )


def _segments(versioning: str, meta: Mapping[str, str]) -> list[str]:
    """The version segment of each URI a NestJS route is served at; ``""`` adds none.

    An application without versioning ignores the versions its controllers state.
    Under URI versioning a route has its handler's version, else its controller's
    (the parser records whichever applies), else the application's default; each
    is its prefix and the version, and ``VERSION_NEUTRAL`` adds no segment. A route
    with no version at all is served at no URI this can name.
    """
    if not versioning:
        return [""]
    try:
        config = json.loads(versioning)
        prefix = config["prefix"]
        versions = json.loads(meta["version"]) if "version" in meta else config["default"]
    except (ValueError, KeyError, TypeError):
        return []
    if not isinstance(prefix, str) or not isinstance(versions, list):
        return []
    return [
        f"{prefix}{version}" if version is not None else ""
        for version in versions
        if version is None or isinstance(version, str)
    ]


def _serve(serving: dict[int, dict[str, set[str]]], repo_id: int, body: str) -> None:
    method, _, path = body.partition(" ")
    if http.canonical_route(body) is not None:
        serving.setdefault(repo_id, {}).setdefault(method, set()).add(path)


def _keep(
    pending: dict[tuple[int, str], PendingHttpEdge], key: tuple[int, str], edge: PendingHttpEdge
) -> None:
    """Plan ``edge`` once per source and target; an edge with lines beats one without.

    The same caller can reach one route both exactly and by shape. The lines are
    the citable half of that, so they must not depend on which call was read first.
    """
    current = pending.get(key)
    if current is None or (current.line_start is None and edge.line_start is not None):
        pending[key] = edge


def _requester_call(
    claim: UnresolvedRefRecord,
    resolver: _Resolver,
    declared: dict[tuple[int, str, str], list[UnresolvedRefRecord]],
    serving: dict[int, dict[str, set[str]]],
) -> tuple[str, list[PendingHttpEdge], tuple[CallDecision, ...]]:
    """Decide one call through an imported helper: ``(outcome, edges, decisions)``."""
    meta = _meta(claim)
    origin = resolver.origin(claim)
    target = resolver.module(origin, meta.get("specifier", "")) if origin else None
    if origin is None or target is None:
        return "import does not resolve", [], ()
    if "factory" in meta:
        role, name = HTTP_REQUESTER, meta["factory"]
    else:
        role, name = HTTP_WRAPPER, meta.get("wrapper", "")
    matches = declared.get((target.file_id, role, name), [])
    if not matches:
        return NOT_HTTP, [], ()
    if len(matches) != 1:
        return "more than one declaration of that name", [], ()
    declaration = _meta(matches[0])
    wrapper: Wrapper | None
    if role == HTTP_REQUESTER:
        members = json.loads(declaration.get("members", "{}"))
        if meta.get("member") not in members:
            return NOT_HTTP, [], ()
        wrapper = wrapper_from(members[meta["member"]])
    else:
        wrapper = wrapper_from(json.loads(declaration.get("wrapper", "null")))
    if wrapper is None:
        return "the declaration cannot be read", [], ()

    arguments = json.loads(meta.get("arguments", "[]"))
    if not isinstance(arguments, list) or len(arguments) <= wrapper.path:
        return NOT_LITERAL, [], ()
    spec = arguments[wrapper.path]

    def helper(entry: dict[str, object]) -> Method | Silent | None:
        specifier, imported = entry.get("specifier"), entry.get("imported")
        if not isinstance(specifier, str) or not isinstance(imported, str):
            return None
        module = resolver.module(origin, specifier)
        found = declared.get((module.file_id, HTTP_INIT_HELPER, imported), []) if module else []
        if len(found) != 1:
            return None
        summary = helper_from(json.loads(_meta(found[0]).get("helper", "null")))
        values = entry.get("arguments")
        if summary is None or not (values is None or isinstance(values, list)):
            return None
        return apply_helper(summary, values)

    method: Method | None
    if wrapper.init is None:
        method = Method(wrapper.method or GET, in_call=False)
    elif len(arguments) <= wrapper.init:
        method = Method(GET, in_call=True)
    else:
        init = arguments[wrapper.init]
        entries = init.get("init") if isinstance(init, dict) else None
        method = evaluate(entries, helper) if isinstance(entries, list) else None
    return _decide(claim, origin, method, spec, wrapper.base, serving.get(origin.repo_id, {}))


def _local_call(
    claim: UnresolvedRefRecord,
    resolver: _Resolver,
    serving: dict[int, dict[str, set[str]]],
) -> tuple[str, list[PendingHttpEdge], tuple[CallDecision, ...]]:
    """Decide one request a file made but could not decide on its own."""
    meta = _meta(claim)
    origin = resolver.origin(claim)
    if origin is None:
        return "the file is not indexed", [], ()
    try:
        spec: object = json.loads(meta.get("path", "null"))
    except ValueError:
        spec = None
    value = meta.get("method")
    method = Method(value, in_call=meta.get("in_call") == "true") if value else None
    if meta.get("transport") == TRANSPORT_FETCH:
        options = _options(spec)
        shape = next((option for option in options if not isinstance(option, str)), None)
        decision = CallDecision(
            status=UNRESOLVED,
            reason=REASON_DIRECT_FETCH,
            method=method.value if method else None,
            route=shape.display if shape is not None else "",
            template=shape,
        )
        return DIRECT_FETCH, [], (decision,)
    if meta.get("transport") != TRANSPORT_WRAPPER:
        return NOT_LITERAL, [], ()
    served = serving.get(origin.repo_id, {})
    return _decide(claim, origin, method, spec, meta.get("base"), served)


def _options(spec: object) -> list[http.RouteTemplate | str]:
    """Each path a spec can name, as a route template, or the code for why not.

    Alternatives that read as the same path collapse into one: a choice between two
    spellings of one route is one request.
    """
    raw = spec.get("alternatives") if isinstance(spec, dict) else None
    choices: list[object] = raw if isinstance(raw, list) and raw else [spec]
    found: dict[str, http.RouteTemplate | str] = {}
    for choice in choices:
        option = _option(choice)
        # Keyed on the path alone. Whether a query followed is not part of which
        # route is named, so `/x?id=1` and `/x` are one request written two ways,
        # exactly as a file that can decide such a call for itself reads them.
        key = option if isinstance(option, str) else option.display
        found.setdefault(key, option)
    return list(found.values())


def _option(spec: object) -> http.RouteTemplate | str:
    if not isinstance(spec, dict):
        return REASON_NOT_LITERAL
    if "string" in spec:
        value = spec["string"]
        if not isinstance(value, str) or not value.startswith("/"):
            return REASON_NOT_LITERAL
        return http.route_template([[http.TEMPLATE_LITERAL, value]])
    if "parts" in spec:
        parts = template_parts(spec["parts"])
        if parts is None:
            return http.NOT_A_PATH
        return http.route_template(parts, truncated=spec.get("truncated") is True)
    if "named" in spec:
        return REASON_NAMED_VALUE
    if "template" in spec:
        return REASON_OLDER_PARSER
    return REASON_NOT_LITERAL


def _under(template: http.RouteTemplate, base: str | None) -> http.RouteTemplate | str:
    """``template`` below a literal wrapper base's path, or why it cannot be."""
    if not base:
        return template
    target = http.client_target(base)
    if target is None:
        return http.NOT_A_PATH
    prefix = tuple(segment for segment in target.path.rstrip("/").split("/")[1:])
    return http.RouteTemplate(
        segments=prefix + template.segments,
        expressions=template.expressions,
        query=template.query,
    )


def _decide(
    claim: UnresolvedRefRecord,
    origin: FileRow,
    method: Method | None,
    spec: object,
    base: str | None,
    served: dict[str, set[str]],
) -> tuple[str, list[PendingHttpEdge], tuple[CallDecision, ...]]:
    """Edges, decisions and the outcome for one call's path, within its repository.

    A literal path keeps the rule it always had: an edge to its route, with an
    endpoint when a literal base makes it absolute, and lines only when the call
    itself states the method and nothing is prepended. A path with a dynamic
    segment links, with no lines and never to an endpoint, to every route of its
    method this repository serves with its shape. An unread method never links.
    """
    edges: list[PendingHttpEdge] = []
    decisions: list[CallDecision] = []
    verb = method.value if method is not None else None
    for option in _options(spec):
        if isinstance(option, str):
            named = spec.get("named") if isinstance(spec, dict) else None
            route = named if option == REASON_NAMED_VALUE and isinstance(named, str) else ""
            decisions.append(CallDecision(UNRESOLVED, option, verb, route))
            continue
        literal = option.path
        if literal is not None:
            url = http.client_target(f"{base}{literal}" if base else literal)
            if url is None:
                decisions.append(CallDecision(UNRESOLVED, http.NOT_A_PATH, verb, literal))
                continue
            under = [f"{m} {url.path}" for m in sorted(served) if url.path in served[m]]
            if method is None:
                status = AMBIGUOUS if under else NO_INDEXED_HANDLER
                decisions.append(
                    CallDecision(
                        status,
                        REASON_METHOD_UNREAD,
                        None,
                        literal,
                        candidates=tuple(under),
                        composed=base is not None,
                        template=option,
                    )
                )
                continue
            exact = method.in_call and base is None
            lines = (claim.line_start, claim.line_end) if exact else (None, None)
            body = http.route_body(method.value, url.path)
            edges.append(
                _pending(
                    claim, origin, EdgeType.CALLS_HTTP_ROUTE, EntityType.HTTP_ROUTE, body, lines
                )
            )
            if url.url is not None:
                endpoint = http.endpoint_body(method.value, url.url)
                edges.append(
                    _pending(
                        claim,
                        origin,
                        EdgeType.CALLS_HTTP_ENDPOINT,
                        EntityType.HTTP_ENDPOINT,
                        endpoint,
                        lines,
                    )
                )
            decisions.append(
                CallDecision(
                    MATCHED if body in under else NO_INDEXED_HANDLER,
                    REASON_EXACT,
                    method.value,
                    literal,
                    matched=(body,) if body in under else (),
                    candidates=tuple(route for route in under if route != body),
                    composed=not exact,
                    template=option,
                    linked=(body,),
                )
            )
            continue
        shape = _under(option, base)
        if isinstance(shape, str):
            decisions.append(CallDecision(UNRESOLVED, shape, verb, option.display))
            continue
        compatible = [
            http.route_body(m, path)
            for m in sorted(served)
            for path in sorted(served[m])
            if http.template_matches(shape, path)
        ]
        if method is None:
            decisions.append(
                CallDecision(
                    AMBIGUOUS if compatible else NO_INDEXED_HANDLER,
                    REASON_METHOD_UNREAD,
                    None,
                    option.display,
                    candidates=tuple(compatible),
                    composed=True,
                    template=shape,
                )
            )
            continue
        mine = [route for route in compatible if route.partition(" ")[0] == method.value]
        for route in mine:
            edges.append(
                _pending(
                    claim,
                    origin,
                    EdgeType.CALLS_HTTP_ROUTE,
                    EntityType.HTTP_ROUTE,
                    route,
                    (None, None),
                )
            )
        decisions.append(
            CallDecision(
                MATCHED if mine else NO_INDEXED_HANDLER,
                REASON_STRUCTURAL if mine else REASON_NO_ROUTE,
                method.value,
                option.display,
                matched=tuple(mine),
                candidates=tuple(route for route in compatible if route not in mine),
                composed=True,
                template=shape,
                linked=tuple(mine),
            )
        )
    return (PROMOTED if edges else _outcome(decisions)), edges, tuple(decisions)


def _outcome(decisions: Sequence[CallDecision]) -> str:
    """Why a call that links nowhere links nowhere, in words."""
    reasons = {decision.reason for decision in decisions}
    for reason, words in (
        (REASON_NOT_LITERAL, NOT_LITERAL),
        (REASON_OLDER_PARSER, OLDER_PARSER),
        (REASON_NAMED_VALUE, NAMED_VALUE),
        (REASON_METHOD_UNREAD, METHOD_UNREAD),
        (REASON_NO_ROUTE, NO_ROUTE),
    ):
        if reason in reasons:
            return words
    if any(decision.status == UNRESOLVED for decision in decisions):
        return f"{UNREADABLE_TEMPLATE} ({decisions[0].reason})"
    return "the request names no route"


def _pending(
    claim: UnresolvedRefRecord,
    origin: FileRow,
    edge: EdgeType,
    kind: EntityType,
    body: str,
    lines: tuple[int | None, int | None],
) -> PendingHttpEdge:
    return PendingHttpEdge(
        src_id=claim.src_id,
        target=EntityRecord(type=kind, key=global_key(kind, body), name=body, qualified_name=body),
        type=edge,
        repo_id=origin.repo_id,
        file_id=origin.file_id,
        line_start=lines[0],
        line_end=lines[1],
    )


def _application(
    claim: UnresolvedRefRecord,
    resolver: _Resolver,
    modules: dict[int, _Module],
    routed_packages: set[tuple[int, str]],
    configured_repositories: set[int],
    served: dict[int, set[tuple[str, str]]],
    withheld: dict[int, str],
) -> str:
    """Walk one application's registration and record what it proves.

    Returns ``proven`` or why not. The walk runs to the end even when the answer
    is already no, so every controller the application reaches carries the reason
    it was withheld rather than a generic one.
    """
    meta = _meta(claim)
    origin = resolver.origin(claim)
    if origin is None:
        return "bootstrap file not indexed"
    if meta.get("kind") != MODULE:
        return "root module registered dynamically"
    _, root = resolver.binding(origin, claim.target_key, meta)
    if root is None or root not in modules:
        return "root module not resolved to an indexed module"

    blocked: str | None = None
    if meta.get("prefix_status") != PROVEN:
        blocked = f"prefix not proven: {meta.get('reason', 'unknown')}"
    elif origin.repo_id in configured_repositories:
        blocked = "a file in this repository can change any application's prefix"
    elif resolver.package_of(origin) in routed_packages:
        blocked = "RouterModule is imported in this package"

    reached = [root]
    seen = {root}
    handed: set[int] = set()
    cursor = 0
    while cursor < len(reached):
        module = modules[reached[cursor]]
        cursor += 1
        if not module.readable:
            blocked = blocked or "a registered module's metadata cannot be read"
        for entry in module.imports:
            entry_meta = _meta(entry)
            entry_origin = resolver.origin(entry)
            if entry_origin is None:
                blocked = blocked or "a registered module is not indexed"
                continue
            kind = entry_meta.get("kind")
            if kind == MODULE:
                status, target = resolver.binding(entry_origin, entry.target_key, entry_meta)
                if status == "unresolved":
                    blocked = blocked or f"import {entry.target_key!r} does not resolve"
                if target is not None and target in modules and target not in seen:
                    if len(reached) >= _MAX_MODULES:
                        blocked = blocked or "too many modules"
                        continue
                    seen.add(target)
                    reached.append(target)
            elif kind == DYNAMIC:
                reason = _dynamic(entry, entry_origin, entry_meta, resolver, modules, handed)
                blocked = blocked or reason
            elif kind == ROUTER:
                blocked = blocked or "RouterModule is registered"
            else:
                blocked = blocked or f"import {entry.target_key!r} cannot be read"

    mount = (meta.get("prefix", ""), meta.get("versioning", ""))
    for module_id in reached:
        for entry in modules[module_id].controllers:
            entry_origin = resolver.origin(entry)
            if entry_origin is None:
                continue
            _, controller = resolver.binding(entry_origin, entry.target_key, _meta(entry))
            if controller is None:
                continue
            if blocked is not None:
                withheld.setdefault(controller, blocked)
            elif module_id in handed:
                withheld.setdefault(
                    controller, "its module is handed to a dynamic module that may re-prefix it"
                )
            else:
                served[controller].add(mount)
    return blocked or PROVEN


def _dynamic(
    entry: UnresolvedRefRecord,
    origin: FileRow,
    meta: dict[str, str],
    resolver: _Resolver,
    modules: dict[int, _Module],
    handed: set[int],
) -> str | None:
    """Read a dynamic module entry. It registers nothing this can prove.

    Returns why the application cannot be proven, or ``None`` and records every
    module of this repository it was handed.
    """
    if meta.get("callee_local") != "true":
        specifier = meta.get("callee_specifier", "")
        if not specifier:
            return f"dynamic module {entry.target_key!r} has no known source"
        if not is_package(specifier) and not resolver.module_file(origin, specifier):
            return f"dynamic module {entry.target_key!r} does not resolve"
    if meta.get("arguments_truncated") == "true":
        return f"dynamic module {entry.target_key!r} is handed too much to read"
    for name, binding in json.loads(meta.get("arguments", "{}")).items():
        _, target = resolver.binding(origin, name, {str(k): str(v) for k, v in binding.items()})
        if target is not None and target in modules:
            handed.add(target)
    return None
