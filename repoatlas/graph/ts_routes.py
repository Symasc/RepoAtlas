"""Fastify routes served through registrations in other modules (HTTP linkage).

A Fastify plugin's routes are served under the prefixes of the ``register`` calls
that reach it from an application, and those calls are usually in other modules
than the routes. The parser records each side as a claim
(:mod:`repoatlas.parsers.http_fastify`):

* ``fastify-scope`` -- an application, or a plugin function, and the names a module
  exports it under;
* ``fastify-route`` -- a route on a plugin's server, or one whose path names an
  imported constant;
* ``fastify-register`` -- a plugin registered on an application or a plugin, with
  where it comes from and its prefix;
* ``ts-constants`` -- a module's exported path-like string constants.

This stage serves a route only when every step is proven:

* a registered plugin is declared in this file, or exported by the one module a
  relative import resolves to, under exactly one scope of that name;
* every imported name in its path is a string constant that module exports;
* a chain of readable registrations at most :data:`MAX_DEPTH` deep reaches an
  application. The route is served at the chain's prefixes joined to its path, and
  a ``/`` route under a prefix at the prefix itself.

A registration that cannot be read -- an options object with a spread or a dynamic
prefix, a ``fastify-plugin`` plugin given a prefix it ignores -- leaves its plugin
unserved everywhere. A route whose prefixes are all empty and whose path is literal
keeps its lines; anything else is composed.

Everything is repository-local: a relative import resolves inside its own repository,
so a repository's share of the plan is the plan of its claims alone.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from repoatlas.graph.py_routes import PendingRoute
from repoatlas.graph.resolution import ResolutionIndex
from repoatlas.models import http
from repoatlas.parsers.http_facts import (
    EXACT,
    FASTIFY_REGISTER,
    FASTIFY_ROUTE,
    FASTIFY_SCOPE,
    TS_CONSTANTS,
)
from repoatlas.parsers.http_fastify import APP
from repoatlas.storage.protocols import FileRow, UnresolvedRefRecord

#: Plugins registered in plugins, counted from the application.
MAX_DEPTH = 4

PROMOTED = "promoted"
NOT_DECLARED = "no Fastify application or plugin of that name is declared there"
CONSTANT_UNRESOLVED = "a name in the path is no string constant the module it names exports"
UNREADABLE_REGISTRATION = "a registration of its plugin cannot be read"
NOT_REGISTERED = "not registered by a proven application"

Key = tuple[int, str]


@dataclass(slots=True)
class FastifyRoutePlan:
    routes: list[PendingRoute] = field(default_factory=list)
    #: Route claim id → ``promoted`` or why not.
    outcomes: dict[int, str] = field(default_factory=dict)
    #: Route claim id → the route bodies it is served at.
    promoted: dict[int, tuple[str, ...]] = field(default_factory=dict)
    #: Route claim id → its plugin-relative route body, when it is served nowhere.
    declared: dict[int, str] = field(default_factory=dict)
    resolved: int = 0
    unresolved: int = 0


@dataclass(frozen=True, slots=True)
class _Scope:
    kind: str
    wrapped: bool


@dataclass(frozen=True, slots=True)
class _Registration:
    owner: Key
    prefix: str | None


def _meta(claim: UnresolvedRefRecord) -> dict[str, str]:
    loaded = json.loads(claim.meta_json) if claim.meta_json else {}
    return {str(k): str(v) for k, v in loaded.items()}


def plan_fastify_routes(
    grouped: Mapping[str, Sequence[UnresolvedRefRecord]],
    index: ResolutionIndex,
    by_id: Mapping[int, FileRow],
) -> FastifyRoutePlan:
    """Serve every Fastify route claim a proven registration chain reaches. Writes nothing."""
    plan = FastifyRoutePlan()
    claims = grouped.get(FASTIFY_ROUTE, ())
    if not claims:
        return plan

    scopes: dict[Key, _Scope] = {}
    exports: dict[tuple[int, str], list[tuple[str, bool]]] = defaultdict(list)
    for claim in grouped.get(FASTIFY_SCOPE, ()):
        meta = _meta(claim)
        scopes[(claim.file_id, claim.target_key)] = _Scope(
            kind=meta.get("kind", ""), wrapped=meta.get("wrapped") == "true"
        )
        for name, wrapped in json.loads(meta.get("exports", "[]")):
            exports[(claim.file_id, str(name))].append((claim.target_key, bool(wrapped)))
    constants: dict[int, dict[str, str]] = {}
    for claim in grouped.get(TS_CONSTANTS, ()):
        table = json.loads(_meta(claim).get("constants", "{}"))
        constants[claim.file_id] = {name: value for name, (value, _) in table.items()}

    registrations: dict[Key, list[_Registration]] = defaultdict(list)
    blocked: set[Key] = set()
    for claim in grouped.get(FASTIFY_REGISTER, ()):
        meta = _meta(claim)
        origin = by_id.get(claim.file_id)
        if origin is None:
            continue
        child = _child(index, origin, meta, exports, scopes)
        if child is None:
            continue
        key, wrapped = child
        prefix = json.loads(meta.get("prefix", "null"))
        if meta.get("readable") != "true" or (wrapped and prefix):
            blocked.add(key)
            continue
        registrations[key].append(
            _Registration(owner=(claim.file_id, meta.get("owner", "")), prefix=prefix)
        )

    chains = _Chains(scopes, registrations, blocked)
    for claim in claims:
        outcome, bodies, exact = _route(claim, index, scopes, constants, chains, by_id)
        if claim.id is not None:
            plan.outcomes[claim.id] = outcome
        if outcome == PROMOTED:
            plan.resolved += 1
            origin = by_id[claim.file_id]
            for body in bodies:
                plan.routes.append(
                    PendingRoute(
                        src_id=claim.src_id,
                        body=body,
                        repo_id=origin.repo_id,
                        file_id=origin.file_id,
                        line_start=claim.line_start if exact[body] else None,
                        line_end=claim.line_end if exact[body] else None,
                    )
                )
            if claim.id is not None:
                plan.promoted[claim.id] = tuple(bodies)
            continue
        plan.unresolved += 1
        relative = _relative(claim)
        if claim.id is not None and relative is not None:
            plan.declared[claim.id] = relative
    return plan


def _child(
    index: ResolutionIndex,
    origin: FileRow,
    meta: Mapping[str, str],
    exports: Mapping[tuple[int, str], list[tuple[str, bool]]],
    scopes: Mapping[Key, _Scope],
) -> tuple[Key, bool] | None:
    """The plugin a registration names, and whether it is wrapped in fastify-plugin."""
    wrapped = meta.get("wrapped") == "true"
    local = meta.get("local")
    if local is not None:
        key = (origin.file_id, local)
        scope = scopes.get(key)
        return (key, wrapped or scope.wrapped) if scope is not None else None
    module = meta.get("module", "")
    if not module.startswith("."):
        return None
    target = index.resolve_module(origin, module, "node")
    if target is None:
        return None
    found = exports.get((target.file_id, meta.get("name", "")), [])
    if len(found) != 1:
        return None
    scope_id, exported_wrapped = found[0]
    key = (target.file_id, scope_id)
    scope = scopes.get(key)
    if scope is None:
        return None
    return key, wrapped or exported_wrapped or scope.wrapped


def _relative(claim: UnresolvedRefRecord) -> str | None:
    meta = _meta(claim)
    parts = json.loads(meta.get("path", "[]"))
    if not parts or any(part[0] != "t" for part in parts):
        return None
    path = http.route_path("".join(part[1] for part in parts))
    body = http.route_body(meta.get("method", ""), path) if path is not None else ""
    return body if body and http.canonical_route(body) is not None else None


class _Chains:
    """Where a plugin's routes are served: the registration chains that reach an application."""

    def __init__(
        self,
        scopes: Mapping[Key, _Scope],
        registrations: Mapping[Key, Sequence[_Registration]],
        blocked: set[Key],
    ) -> None:
        self._scopes = scopes
        self._registrations = registrations
        self._blocked = blocked

    def blocked(self, key: Key) -> bool:
        return key in self._blocked

    def prefixes(self, key: Key, depth: int = 0) -> list[tuple[str, bool]]:
        """``(prefix, exact)`` such that a route path ``p`` in scope ``key`` is served at
        ``prefix + p``: an application serves at its root."""
        scope = self._scopes.get(key)
        if scope is None:
            return []
        if scope.kind == APP:
            return [("", True)]
        if depth > MAX_DEPTH or key in self._blocked:
            return []
        found: list[tuple[str, bool]] = []
        for registration in self._registrations.get(key, ()):
            prefix = registration.prefix or ""
            for base, exact in self.prefixes(registration.owner, depth + 1):
                found.append((f"{base}{prefix}", exact and not prefix))
        return found


def _route(
    claim: UnresolvedRefRecord,
    index: ResolutionIndex,
    scopes: Mapping[Key, _Scope],
    constants: Mapping[int, Mapping[str, str]],
    chains: _Chains,
    by_id: Mapping[int, FileRow],
) -> tuple[str, list[str], dict[str, bool]]:
    meta = _meta(claim)
    origin = by_id.get(claim.file_id)
    key = (claim.file_id, meta.get("scope", ""))
    if origin is None or key not in scopes:
        return NOT_DECLARED, [], {}
    path = ""
    for part in json.loads(meta.get("path", "[]")):
        if part[0] == "t":
            path += part[1]
            continue
        target = index.resolve_module(origin, part[1], "node") if part[1].startswith(".") else None
        value = constants.get(target.file_id, {}).get(part[2]) if target is not None else None
        if value is None:
            return CONSTANT_UNRESOLVED, [], {}
        path += value
    if chains.blocked(key):
        return UNREADABLE_REGISTRATION, [], {}
    literal = meta.get("provenance") == EXACT
    bodies: dict[str, bool] = {}
    for base, exact in chains.prefixes(key):
        full = base if path == "/" and base else f"{base}{path}"
        route = http.route_path(full) if full else None
        body = http.route_body(meta.get("method", ""), route) if route is not None else ""
        if not body or http.canonical_route(body) is None:
            continue
        bodies[body] = bodies.get(body, False) or (exact and literal)
    if bodies:
        return PROMOTED, sorted(bodies), bodies
    return NOT_REGISTERED, [], {}
