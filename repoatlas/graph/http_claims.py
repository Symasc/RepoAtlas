"""Pending HTTP facts, decided from everything indexed (HTTP linkage).

A parser records some HTTP facts as *pending* (:mod:`repoatlas.parsers.http_facts`)
because one file cannot prove them. This stage of the global link pass reads all
of them together and publishes only what the indexed source establishes.

**NestJS routes.** A controller-local route becomes ``SERVES_HTTP_ROUTE`` on the
shared route node only when an application is proven to serve it:

* a ``NestFactory.create`` root names its root module directly and proves its
  global prefix (:mod:`repoatlas.parsers.http_nestjs` decides what that takes);
* the controller's registration path is established — the root module reaches the
  declaring module through ``imports`` entries that each resolve to exactly one
  indexed module class, and that module lists the controller;
* nothing that can change routes is left unread where it could reach them.

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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from repoatlas.graph.keys import global_key
from repoatlas.graph.resolution import ResolutionIndex
from repoatlas.models import http
from repoatlas.models.parsing import EdgeType, EntityType
from repoatlas.parsers.http_facts import (
    EXACT,
    HTTP_INIT_HELPER,
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
from repoatlas.storage.protocols import EdgeRecord, EntityRecord, FileRow, UnresolvedRefRecord

#: More modules than any application registers; a bound on adversarial input.
_MAX_MODULES = 4096

PROMOTED = "promoted"
#: A call to an imported function that is no request helper: not an HTTP claim.
NOT_HTTP = "no request helper of that name is declared in the imported module"


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

    @property
    def detail(self) -> str:
        promoted = sum(1 for outcome in self.routes.values() if outcome == PROMOTED)
        requests = sum(1 for outcome in self.requests.values() if outcome == PROMOTED)
        return (
            f"{promoted} of {len(self.routes)} declared route(s) proven, "
            f"{requests} request(s) through imported helpers"
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


def plan_http_claims(
    claims: Sequence[UnresolvedRefRecord],
    index: ResolutionIndex,
    files: Iterable[FileRow],
    *,
    producer: str,
) -> HttpClaimPlan:
    """Decide every pending HTTP fact at once. Writes nothing.

    ``producer`` is recorded on every edge, so the pass that owns them can delete
    exactly its own output and rebuild it.
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
    served: dict[int, set[str]] = defaultdict(set)
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
    for claim in grouped[NESTJS_ROUTE]:
        meta = _meta(claim)
        origin = resolver.origin(claim)
        controller = (
            resolver.local_class(origin.file_id, meta.get("controller", ""))
            if origin is not None
            else None
        )
        prefixes = served.get(controller, set()) if controller is not None else set()
        promoted = False
        for prefix in sorted(prefixes):
            path = nest_path(prefix, meta.get("controller_path", ""), meta.get("method_path", ""))
            body = http.route_body(meta.get("method", ""), path) if path is not None else ""
            if origin is None or not body or http.canonical_route(body) is None:
                continue
            # One decorator states the whole route only when nothing else added to it.
            exact = (
                not prefix and not meta.get("controller_path") and meta.get("provenance") == EXACT
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
        if promoted:
            resolved += 1
        else:
            unresolved += 1
        if claim.id is not None:
            if promoted:
                route_outcomes[claim.id] = PROMOTED
            elif controller is None:
                route_outcomes[claim.id] = "controller not indexed"
            else:
                route_outcomes[claim.id] = withheld.get(
                    controller, "not registered by a proven application"
                )

    # -- requests through imported helpers ---------------------------------------------
    declared: dict[tuple[int, str, str], list[UnresolvedRefRecord]] = defaultdict(list)
    for role in (HTTP_REQUESTER, HTTP_WRAPPER, HTTP_INIT_HELPER):
        for claim in grouped[role]:
            declared[(claim.file_id, role, claim.target_key)].append(claim)
    request_outcomes: dict[int, str] = {}
    for claim in grouped[HTTP_REQUESTER_CALL]:
        outcome, found = _requester_call(claim, resolver, declared)
        for edge in found:
            pending.setdefault((edge.src_id, f"{edge.type}:{edge.target.key}"), edge)
        if outcome == PROMOTED:
            resolved += 1
        elif outcome != NOT_HTTP:
            unresolved += 1
        if claim.id is not None:
            request_outcomes[claim.id] = outcome

    return HttpClaimPlan(
        edges=tuple(edges),
        http=tuple(pending.values()),
        resolved=resolved,
        unresolved=unresolved,
        routes=route_outcomes,
        roots=root_outcomes,
        requests=request_outcomes,
    )


def _requester_call(
    claim: UnresolvedRefRecord,
    resolver: _Resolver,
    declared: dict[tuple[int, str, str], list[UnresolvedRefRecord]],
) -> tuple[str, list[PendingHttpEdge]]:
    """Decide one call through an imported helper: ``(outcome, edges)``."""
    meta = _meta(claim)
    origin = resolver.origin(claim)
    target = resolver.module(origin, meta.get("specifier", "")) if origin else None
    if origin is None or target is None:
        return "import does not resolve", []
    if "factory" in meta:
        role, name = HTTP_REQUESTER, meta["factory"]
    else:
        role, name = HTTP_WRAPPER, meta.get("wrapper", "")
    matches = declared.get((target.file_id, role, name), [])
    if not matches:
        return NOT_HTTP, []
    if len(matches) != 1:
        return "more than one declaration of that name", []
    declaration = _meta(matches[0])
    wrapper: Wrapper | None
    if role == HTTP_REQUESTER:
        members = json.loads(declaration.get("members", "{}"))
        if meta.get("member") not in members:
            return NOT_HTTP, []
        wrapper = wrapper_from(members[meta["member"]])
    else:
        wrapper = wrapper_from(json.loads(declaration.get("wrapper", "null")))
    if wrapper is None:
        return "the declaration cannot be read", []

    arguments = json.loads(meta.get("arguments", "[]"))
    if not isinstance(arguments, list) or len(arguments) <= wrapper.path:
        return "the path is not a literal", []
    path = (
        arguments[wrapper.path].get("string") if isinstance(arguments[wrapper.path], dict) else None
    )
    if not isinstance(path, str) or not path.startswith("/"):
        return "the path is not a literal", []

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
    if method is None:
        return "the method cannot be read", []

    url = http.client_target(f"{wrapper.base}{path}" if wrapper.base else path)
    if url is None:
        return "the request names no route", []
    exact = method.in_call and wrapper.base is None
    lines = (claim.line_start, claim.line_end) if exact else (None, None)
    found = [
        _pending(
            claim,
            origin,
            EdgeType.CALLS_HTTP_ROUTE,
            EntityType.HTTP_ROUTE,
            http.route_body(method.value, url.path),
            lines,
        )
    ]
    if url.url is not None:
        found.append(
            _pending(
                claim,
                origin,
                EdgeType.CALLS_HTTP_ENDPOINT,
                EntityType.HTTP_ENDPOINT,
                http.endpoint_body(method.value, url.url),
                lines,
            )
        )
    return PROMOTED, found


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
    served: dict[int, set[str]],
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

    prefix = meta.get("prefix", "")
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
                served[controller].add(prefix)
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
