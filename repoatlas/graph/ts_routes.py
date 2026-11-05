"""Fastify routes served through registrations in other modules (HTTP linkage).

A Fastify plugin's routes are served under the prefixes of the ``register`` calls
that reach it from an application, and those calls are usually in other modules
than the routes. The parser records each side as a claim
(:mod:`repoatlas.parsers.http_fastify`):

* ``fastify-scope`` -- an application, a plugin function, or a factory (a function that
  returns an application it builds), and the names a module exports it under;
* ``fastify-route`` -- a route on a plugin's server, or one whose path names an
  imported constant;
* ``fastify-register`` -- a plugin registered on an application or a plugin, with
  where it comes from and its prefix;
* ``ts-constants`` -- a module's exported path-like string constants.

A server that is handed on to a function, or built by a factory, instead of being
registered adds these claims:

* ``fastify-instance`` -- ``const x = F()`` (``F`` a factory), or an application built at
  the top of an entry function, and whether the parser proved it, left it to this pass
  (an imported factory) or refused it (with a code);
* ``fastify-helper-call`` -- ``helper(x)``: a receiver handed to a function;
* ``fastify-traced-scope`` -- a plugin function of a file that does not import Fastify;
* ``fastify-traced-route`` -- a route on an instance, an entry, a factory's own
  application, or a plugin of a file that does not import Fastify.

This stage serves a route only when every step is proven:

* a registered plugin is declared in this file, or exported by the one module a
  relative import resolves to, under exactly one scope of that name. A factory is never
  a plugin export: its scope is indexed apart, so a registration never sees it;
* an instance's factory is a factory of its own file, or the one factory the module a
  relative import resolves to exports under that name, in the same repository. A module
  outside the repository, or none, says :data:`FACTORY_UNRESOLVED`; a module that exports
  no one factory of that name, or a function that is none, says ``fastify-factory-unproven``;
  an ``async`` factory must be awaited;
* a helper call's callee is a plugin function of its own file or of the module a relative
  import resolves to, in the same repository, under exactly one scope of that name, and the
  receiver is its first argument. Otherwise the call says :data:`HELPER_UNRESOLVED`,
  :data:`HELPER_NOT_A_PLUGIN` (a factory, or a scope that is no plugin) or
  :data:`HELPER_ARGUMENT_POSITION`; a call the parser refused keeps its own code;
* every imported name in its path is a string constant that module exports;
* a chain of readable registrations and helper calls reaches an application, a proven
  instance or a proven entry. A chain of registrations may pass through at most
  :data:`MAX_DEPTH` plugins between the route's own plugin and the application, a chain of
  helper calls is at most :data:`MAX_HELPER_DEPTH` calls long, and a factory is crossed at
  most :data:`MAX_FACTORY_DEPTH` time (from the factory's own application to the instances
  of it). The route is served at the chain's prefixes joined to its path, and a ``/`` route
  under a prefix at the prefix itself. A helper call is a registration with no prefix.

A registration or a helper call counts only when its owner is a proven receiver: an
application, a plugin, a factory, a proven instance or a proven entry. One made on a
pending or refused instance is ignored altogether: it neither serves nor blocks. A
registration that cannot be read -- an options object with a spread or a dynamic
prefix, a ``fastify-plugin`` plugin given a prefix it ignores -- made by a proven owner
leaves its plugin unserved everywhere. A route whose prefixes are all empty and whose
path is literal keeps its lines; anything else is composed.

A scope already on the chain being followed contributes nothing: a plugin that reaches
itself again is not served under the prefix of its own cycle. The walk from a route's scope
visits at most :data:`MAX_VISITS` scopes; beyond that the route is not served and
:data:`CHAIN_TOO_COMPLEX` says why. A route reached through many applications stays
served: only the visits bound the work. A route cut by a hop limit and served nowhere says
:data:`CHAIN_TOO_DEEP`. Both are diagnostics in ``chain_refusals``, never an edge.

A traced route is walked the same way. When it is not promoted it is only a diagnostic, in
``traced_outcomes``: no outcome, no declared route, no count and no provider row.

A proven instance is an application rooted at the file that holds it, not at the file of
the factory or of the function that registers the routes.

Everything is repository-local: a relative import resolves inside its own repository,
so a repository's share of the plan is the plan of its claims alone.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from repoatlas.graph.py_routes import PendingRoute, Place, claim_place, ordered_places
from repoatlas.graph.resolution import ResolutionIndex
from repoatlas.models import http
from repoatlas.parsers.http_facts import (
    EXACT,
    FASTIFY_HELPER_CALL,
    FASTIFY_INSTANCE,
    FASTIFY_REGISTER,
    FASTIFY_ROUTE,
    FASTIFY_SCOPE,
    FASTIFY_TRACED_ROUTE,
    FASTIFY_TRACED_SCOPE,
    TS_CONSTANTS,
)
from repoatlas.parsers.http_fastify import (
    _MAX_CONSTANTS,
    AFTER_LISTEN,
    APP,
    CALLEE_BEFORE_BINDING,
    ENTRY,
    FACTORY,
    FACTORY_NOT_AWAITED,
    FACTORY_UNPROVEN,
    HELPER_ARGUMENT_UNSUPPORTED,
    HELPER_CONTROL_FLOW,
    INSTANCE,
    PLUGIN,
    PROVEN,
    RECEIVER_BEFORE_BINDING,
    REFUSED,
)
from repoatlas.storage.protocols import FileRow, UnresolvedRefRecord

#: Plugins registered in plugins, counted from the application.
MAX_DEPTH = 4
#: Helper calls between a route's function and the receiver it was handed.
MAX_HELPER_DEPTH = 4
#: Factories crossed from a factory's own application to an instance of it.
MAX_FACTORY_DEPTH = 1
#: Scopes one route's walk may visit.
MAX_VISITS = 1024

PROMOTED = "promoted"
NOT_DECLARED = "no Fastify application or plugin of that name is declared there"
CONSTANT_UNRESOLVED = "a name in the path is no string constant the module it names exports"
UNREADABLE_REGISTRATION = "a registration of its plugin cannot be read"
NOT_REGISTERED = "not registered by a proven application"

#: What a helper call that was read is, in ``helper_outcomes``.
HELPER_ACCEPTED = "accepted"
#: Stable codes of this stage, beside the parser's (``fastify-factory-unproven`` ...).
FACTORY_UNRESOLVED = "fastify-factory-unresolved"
HELPER_UNRESOLVED = "fastify-helper-unresolved"
HELPER_NOT_A_PLUGIN = "fastify-helper-not-a-plugin"
HELPER_ARGUMENT_POSITION = "fastify-helper-argument-position"
CHAIN_TOO_DEEP = "fastify-chain-too-deep"
CHAIN_TOO_COMPLEX = "fastify-chain-too-complex"

#: Diagnostic codes this stage establishes itself (R1), beside the parser's.
REGISTRATION_UNREADABLE = "fastify-registration-unreadable"
PLUGIN_PREFIX_IGNORED = "fastify-plugin-prefix-ignored"
FACTORY_NOT_INSTANTIATED = "fastify-factory-not-instantiated"
#: The bound a module's ``ts-constants`` table reached (``http_fastify._MAX_CONSTANTS``, the only
#: producer of the role): a name missing from a table that full may have been cut.
BOUND_CONSTANTS_PER_MODULE = "ts-constants-per-module"
#: The refusals of a helper call that name the callee's own scope as the cause when the planner
#: resolved the callee to one. Never ``fastify-helper-dynamic``, ``-callee-unsupported`` or
#: ``-shadowed``: the callee there is not that function. Never a call whose target is unknown.
_ATTRIBUTED = frozenset(
    {
        HELPER_CONTROL_FLOW,
        HELPER_ARGUMENT_UNSUPPORTED,
        HELPER_ARGUMENT_POSITION,
        RECEIVER_BEFORE_BINDING,
        CALLEE_BEFORE_BINDING,
        AFTER_LISTEN,
    }
)

Key = tuple[int, str]
Exports = Mapping[tuple[int, str], list[tuple[str, bool]]]
FactoryExports = Mapping[tuple[int, str], list[str]]
#: ``(prefix, exact, root)``: the route is served at ``prefix + path`` by the module ``root``.
Served = tuple[str, bool, int]


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
    #: Traced route claim id → why it is served nowhere. Diagnostics only: a traced route
    #: that is not promoted is no outcome, is declared nowhere and is not counted.
    traced_outcomes: dict[int, str] = field(default_factory=dict)
    #: Helper call claim id → ``accepted`` or the code that refused it.
    helper_outcomes: dict[int, str] = field(default_factory=dict)
    #: Instance claim id → ``proven`` or the code that refused it.
    instance_outcomes: dict[int, str] = field(default_factory=dict)
    #: Route claim id → ``fastify-chain-too-deep`` or ``fastify-chain-too-complex``, for a
    #: route served nowhere because a bound stopped its chain.
    chain_refusals: dict[int, str] = field(default_factory=dict)
    #: Diagnostic facts (R1), recorded beside the results above and read by none of them.
    #: Route claim id → the code of the refusal this stage itself attributes to the route's own
    #: scope when it is served nowhere: a refused helper call or instance that names the scope
    #: (``fastify-helper-control-flow`` ... ``fastify-factory-unproven``), the reason a
    #: registration of it is blocked (``fastify-registration-unreadable``,
    #: ``fastify-plugin-prefix-ignored``), or ``fastify-factory-not-instantiated``. A route
    #: with no such cause has no entry: the generic reason is the classification's to supply.
    route_codes: dict[int, str] = field(default_factory=dict)
    #: Route claim id → the plugin-relative route body its path evaluates to, for a route that
    #: is not served and not in ``declared`` (a path built from imported constants).
    resolved_routes: dict[int, str] = field(default_factory=dict)
    #: Route claim id → the bounds reached on its evidence path (a ``ts-constants`` table at
    #: its cap, when the route's unresolved name could have been cut by it).
    route_bounds: dict[int, tuple[str, ...]] = field(default_factory=dict)
    #: Route claim id → the claims that caused its code (helper call, registration, instance).
    related: dict[int, tuple[Place, ...]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _Scope:
    kind: str
    wrapped: bool


@dataclass(frozen=True, slots=True)
class _Registration:
    """A plugin's way in: ``register(plugin, { prefix })``, or a helper call (no prefix)."""

    owner: Key
    prefix: str | None
    helper: bool = False


@dataclass(slots=True)
class _Run:
    """What one route's walk has spent and met."""

    visits: int = 0
    deep: bool = False
    complex: bool = False


@dataclass(slots=True)
class _Trace:
    """What one route claim's evaluation met, beside its unchanged result (R1 diagnostics)."""

    #: The scope the route names.
    key: Key | None = None
    #: The path text, once every part of it was evaluated.
    path: str | None = None
    #: The module whose constants table lacked a name of the path.
    constants_file: int | None = None


#: ``(file path, first line, code)`` and where: one refusal that names a scope as its cause.
Attribution = tuple[str, int, str, Place]


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
    claims: list[tuple[UnresolvedRefRecord, bool]] = [
        *((claim, False) for claim in grouped.get(FASTIFY_ROUTE, ())),
        *((claim, True) for claim in grouped.get(FASTIFY_TRACED_ROUTE, ())),
    ]
    if not claims:
        return plan

    scopes: dict[Key, _Scope] = {}
    exports: dict[tuple[int, str], list[tuple[str, bool]]] = defaultdict(list)
    # Factories are indexed apart from plugins, so a registration never sees one.
    factories: dict[Key, bool] = {}
    factory_exports: dict[tuple[int, str], list[str]] = defaultdict(list)
    for claim in (*grouped.get(FASTIFY_SCOPE, ()), *grouped.get(FASTIFY_TRACED_SCOPE, ())):
        meta = _meta(claim)
        kind = meta.get("kind", "")
        if kind == FACTORY:
            factories[(claim.file_id, claim.target_key)] = meta.get("is_async") == "true"
            for name, _wrapped in json.loads(meta.get("exports", "[]")):
                factory_exports[(claim.file_id, str(name))].append(claim.target_key)
            continue
        scopes[(claim.file_id, claim.target_key)] = _Scope(
            kind=kind, wrapped=meta.get("wrapped") == "true"
        )
        for name, wrapped in json.loads(meta.get("exports", "[]")):
            exports[(claim.file_id, str(name))].append((claim.target_key, bool(wrapped)))
    constants: dict[int, dict[str, str]] = {}
    for claim in grouped.get(TS_CONSTANTS, ()):
        table = json.loads(_meta(claim).get("constants", "{}"))
        constants[claim.file_id] = {name: value for name, (value, _) in table.items()}

    # Proven instances and entries: key → the factory they build with (None for an entry).
    instances: dict[Key, Key | None] = {}
    #: Diagnostics only: the instances and entries this stage refused, with the code and the claim.
    refused: dict[Key, tuple[str, Place]] = {}
    for claim in grouped.get(FASTIFY_INSTANCE, ()):
        origin = by_id.get(claim.file_id)
        if origin is None:
            continue
        outcome, built = _instance(index, origin, _meta(claim), factories, factory_exports)
        if claim.id is not None:
            plan.instance_outcomes[claim.id] = outcome
        if outcome == PROVEN:
            instances[(claim.file_id, claim.target_key)] = built
        else:
            refused[(claim.file_id, claim.target_key)] = (outcome, claim_place(claim, "instance"))
    by_factory: dict[Key, list[Key]] = defaultdict(list)
    for instance, built in instances.items():
        if built is not None:
            by_factory[built].append(instance)

    def proven(owner: Key) -> bool:
        return owner in scopes or owner in factories or owner in instances

    registrations: dict[Key, list[_Registration]] = defaultdict(list)
    blocked: set[Key] = set()
    #: Diagnostics only, all three: what names a scope as the cause of its routes being unserved.
    attributed: dict[Key, list[Attribution]] = defaultdict(list)
    blocked_reason: dict[Key, str] = {}
    blocked_at: dict[Key, list[Place]] = defaultdict(list)
    for claim in grouped.get(FASTIFY_REGISTER, ()):
        meta = _meta(claim)
        origin = by_id.get(claim.file_id)
        if origin is None:
            continue
        child = _child(index, origin, meta, exports, scopes)
        owner = (claim.file_id, meta.get("owner", ""))
        if child is not None and not proven(owner) and owner in refused:
            code, where = refused[owner]
            attributed[child[0]].append((origin.rel_path, claim.line_start or 0, code, where))
        if child is None or not proven(owner):
            continue  # an unproven receiver's registration neither serves nor blocks
        key, wrapped = child
        prefix = json.loads(meta.get("prefix", "null"))
        if meta.get("readable") != "true" or (wrapped and prefix):
            blocked.add(key)
            blocked_reason.setdefault(
                key,
                (
                    REGISTRATION_UNREADABLE
                    if meta.get("readable") != "true"
                    else PLUGIN_PREFIX_IGNORED
                ),
            )
            blocked_at[key].append(claim_place(claim, "registration"))
            continue
        registrations[key].append(_Registration(owner=owner, prefix=prefix))

    for claim in grouped.get(FASTIFY_HELPER_CALL, ()):
        meta = _meta(claim)
        origin = by_id.get(claim.file_id)
        if origin is None:
            continue
        target, code = _helper_target(
            index, origin, meta, scopes, exports, factories, factory_exports
        )
        if meta.get("refusal"):
            code = meta["refusal"]
        elif target is not None and meta.get("arg", "0") != "0":
            code = HELPER_ARGUMENT_POSITION
        if code:
            if target is not None and code in _ATTRIBUTED:
                attributed[target].append(
                    (
                        origin.rel_path,
                        claim.line_start or 0,
                        code,
                        claim_place(claim, "helper-call"),
                    )
                )
            if claim.id is not None:
                plan.helper_outcomes[claim.id] = code
            continue
        owner = (claim.file_id, meta.get("owner", ""))
        if target is not None and not proven(owner) and owner in refused:
            cause, where = refused[owner]
            attributed[target].append((origin.rel_path, claim.line_start or 0, cause, where))
        if target is None or not proven(owner):
            continue  # a call made on a receiver nothing proves is ignored altogether
        if claim.id is not None:
            plan.helper_outcomes[claim.id] = HELPER_ACCEPTED
        registrations[target].append(_Registration(owner=owner, prefix=None, helper=True))

    chains = _Chains(scopes, factories, instances, by_factory, registrations, blocked)
    for claim, traced in claims:
        trace = _Trace()
        outcome, bodies, exact, refusal = _route(claim, index, constants, chains, by_id, trace)
        if outcome != PROMOTED and refusal is not None and claim.id is not None:
            plan.chain_refusals[claim.id] = refusal
        if outcome != PROMOTED and claim.id is not None:
            _record_facts(
                plan,
                claim,
                outcome,
                trace,
                constants,
                by_id,
                attributed,
                blocked_reason,
                blocked_at,
                factories,
                by_factory,
                will_declare=not traced and _relative(claim) is not None,
            )
        if traced and outcome != PROMOTED:
            if claim.id is not None:
                plan.traced_outcomes[claim.id] = outcome
            continue
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
                        line_start=claim.line_start if exact[body][0] else None,
                        line_end=claim.line_end if exact[body][0] else None,
                        roots=exact[body][1],
                        declared_at=claim.line_start,
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
    exports: Exports,
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


def _instance(
    index: ResolutionIndex,
    origin: FileRow,
    meta: Mapping[str, str],
    factories: Mapping[Key, bool],
    factory_exports: FactoryExports,
) -> tuple[str, Key | None]:
    """``(proven, the factory)`` of an instance or an entry the parser proved or left to this
    pass, else ``(the code that refuses it, None)``. A refusal the parser gave is kept."""
    if meta.get("status") == REFUSED:
        return meta.get("refusal", FACTORY_UNPROVEN), None
    if meta.get("kind") == ENTRY:
        return (PROVEN, None) if meta.get("status") == PROVEN else (FACTORY_UNPROVEN, None)
    if meta.get("kind") != INSTANCE:
        return FACTORY_UNPROVEN, None
    if "local" in meta:
        built = (origin.file_id, meta["local"])
    else:
        module = meta.get("module", "")
        target = index.resolve_module(origin, module, "node") if module.startswith(".") else None
        if target is None or target.repo_id != origin.repo_id:
            return FACTORY_UNRESOLVED, None
        found = factory_exports.get((target.file_id, meta.get("name", "")), [])
        if len(found) != 1:
            return FACTORY_UNPROVEN, None  # the module exports no one factory of that name
        built = (target.file_id, found[0])
    if built not in factories:
        return FACTORY_UNPROVEN, None
    if factories[built] and meta.get("awaited") != "true":
        return FACTORY_NOT_AWAITED, None
    return PROVEN, built


def _helper_target(
    index: ResolutionIndex,
    origin: FileRow,
    meta: Mapping[str, str],
    scopes: Mapping[Key, _Scope],
    exports: Exports,
    factories: Mapping[Key, bool],
    factory_exports: FactoryExports,
) -> tuple[Key | None, str]:
    """The plugin scope a helper call names, or ``(None, why it names none)``."""
    if "local" in meta:
        file_id, name = origin.file_id, meta["local"]
        if (file_id, f"{name}()") in factories:
            return None, HELPER_NOT_A_PLUGIN
        key = (file_id, name)
    else:
        module = meta.get("module", "")
        target = index.resolve_module(origin, module, "node") if module.startswith(".") else None
        if target is None or target.repo_id != origin.repo_id:
            return None, HELPER_UNRESOLVED
        name = meta.get("name", "")
        if factory_exports.get((target.file_id, name)):
            return None, HELPER_NOT_A_PLUGIN
        found = exports.get((target.file_id, name), [])
        if len(found) != 1:
            return None, HELPER_UNRESOLVED
        key = (target.file_id, found[0][0])
    scope = scopes.get(key)
    if scope is None:
        return None, HELPER_UNRESOLVED
    if scope.kind != PLUGIN:
        return None, HELPER_NOT_A_PLUGIN
    return key, ""


def _record_facts(
    plan: FastifyRoutePlan,
    claim: UnresolvedRefRecord,
    outcome: str,
    trace: _Trace,
    constants: Mapping[int, Mapping[str, str]],
    by_id: Mapping[int, FileRow],
    attributed: Mapping[Key, Sequence[Attribution]],
    blocked_reason: Mapping[Key, str],
    blocked_at: Mapping[Key, Sequence[Place]],
    factories: Mapping[Key, bool],
    by_factory: Mapping[Key, Sequence[Key]],
    *,
    will_declare: bool,
) -> None:
    """Record, beside an unserved route's unchanged outcome, the cause this stage itself can
    name (R1 diagnostics). Nothing is recorded for a route with no such cause."""
    assert claim.id is not None
    key = trace.key
    if outcome == CONSTANT_UNRESOLVED:
        table: Mapping[str, str] = (
            {} if trace.constants_file is None else constants.get(trace.constants_file, {})
        )
        if len(table) >= _MAX_CONSTANTS:
            plan.route_bounds[claim.id] = (BOUND_CONSTANTS_PER_MODULE,)
    elif key is not None and outcome == UNREADABLE_REGISTRATION and key in blocked_reason:
        plan.route_codes[claim.id] = blocked_reason[key]
        plan.related[claim.id] = ordered_places(blocked_at[key])
    elif key is not None and outcome == NOT_REGISTERED:
        causes = sorted(attributed.get(key, ()), key=lambda item: item[:3])
        if causes:
            code = causes[0][2]
            plan.route_codes[claim.id] = code
            plan.related[claim.id] = ordered_places([c[3] for c in causes if c[2] == code])
        elif key in factories and not by_factory.get(key):
            plan.route_codes[claim.id] = FACTORY_NOT_INSTANTIATED
    if trace.path is not None and not will_declare:
        route = http.route_path(trace.path)
        body = http.route_body(_meta(claim).get("method", ""), route) if route is not None else ""
        if body and http.canonical_route(body) is not None:
            plan.resolved_routes[claim.id] = body


def _relative(claim: UnresolvedRefRecord) -> str | None:
    meta = _meta(claim)
    parts = json.loads(meta.get("path", "[]"))
    if not parts or any(part[0] != "t" for part in parts):
        return None
    path = http.route_path("".join(part[1] for part in parts))
    body = http.route_body(meta.get("method", ""), path) if path is not None else ""
    return body if body and http.canonical_route(body) is not None else None


class _Chains:
    """Where a scope's routes are served: the chains that reach an application, a proven
    instance or a proven entry."""

    def __init__(
        self,
        scopes: Mapping[Key, _Scope],
        factories: Mapping[Key, bool],
        instances: Mapping[Key, Key | None],
        by_factory: Mapping[Key, Sequence[Key]],
        registrations: Mapping[Key, Sequence[_Registration]],
        blocked: set[Key],
    ) -> None:
        self._scopes = scopes
        self._factories = factories
        self._instances = instances
        self._by_factory = by_factory
        self._registrations = registrations
        self._blocked = blocked
        self._walked: dict[Key, tuple[list[Served], str | None]] = {}

    def known(self, key: Key) -> bool:
        """Whether a route claim may name this scope."""
        return key in self._scopes or key in self._factories or key in self._instances

    def blocked(self, key: Key) -> bool:
        return key in self._blocked

    def prefixes(self, key: Key) -> tuple[list[Served], str | None]:
        """``(prefix, exact, root)`` such that a route path ``p`` in scope ``key`` is served at
        ``prefix + p`` by the module ``root``, and the code of the bound that stopped the walk
        (:data:`CHAIN_TOO_COMPLEX` when it did, else :data:`CHAIN_TOO_DEEP` when a hop limit cut
        a chain), if one did. A walk that was too complex serves nothing."""
        found = self._walked.get(key)
        if found is None:
            run = _Run()
            served = self._visit(key, (0, 0, 0), frozenset(), run)
            if run.complex:
                found = ([], CHAIN_TOO_COMPLEX)
            else:
                found = (served, CHAIN_TOO_DEEP if run.deep else None)
            self._walked[key] = found
        return found

    def _visit(
        self, key: Key, hops: tuple[int, int, int], chain: frozenset[Key], run: _Run
    ) -> list[Served]:
        """The prefixes ``key`` is served at, after ``hops`` = (registrations, helper calls,
        factories) from the route's scope. ``chain`` holds the scopes being followed: one
        already on it contributes nothing, so a cycle is never walked twice."""
        registers, helpers, crossed = hops
        run.visits += 1
        if run.visits > MAX_VISITS:
            run.complex = True
            return []
        if helpers > MAX_HELPER_DEPTH or crossed > MAX_FACTORY_DEPTH:
            run.deep = True
            return []
        scope = self._scopes.get(key)
        if (scope is not None and scope.kind == APP) or key in self._instances:
            return [("", True, key[0])]
        below = chain | {key}
        found: list[Served] = []
        if key in self._factories:
            through = (registers, helpers, crossed + 1)
            for instance in self._by_factory.get(key, ()):
                if instance not in chain:
                    found.extend(self._visit(instance, through, below, run))
            return list(dict.fromkeys(found))
        if scope is None:
            return []
        if registers > MAX_DEPTH:
            run.deep = True
            return []
        if key in self._blocked:
            return []
        for registration in self._registrations.get(key, ()):
            if registration.owner in chain:
                continue
            prefix = registration.prefix or ""
            if registration.helper:
                more = (registers, helpers + 1, crossed)
            else:
                more = (registers + 1, helpers, crossed)
            for base, exact, root in self._visit(registration.owner, more, below, run):
                found.append((f"{base}{prefix}", exact and not prefix, root))
            if run.complex:
                return []
        return list(dict.fromkeys(found))


def _route(
    claim: UnresolvedRefRecord,
    index: ResolutionIndex,
    constants: Mapping[int, Mapping[str, str]],
    chains: _Chains,
    by_id: Mapping[int, FileRow],
    trace: _Trace | None = None,
) -> tuple[str, list[str], dict[str, tuple[bool, frozenset[int]]], str | None]:
    """``(outcome, bodies, per body (exact, roots), chain refusal)`` of a route claim."""
    meta = _meta(claim)
    origin = by_id.get(claim.file_id)
    key = (claim.file_id, meta.get("scope", ""))
    if origin is None or not chains.known(key):
        return NOT_DECLARED, [], {}, None
    if trace is not None:
        trace.key = key
    path = ""
    for part in json.loads(meta.get("path", "[]")):
        if part[0] == "t":
            path += part[1]
            continue
        target = index.resolve_module(origin, part[1], "node") if part[1].startswith(".") else None
        value = constants.get(target.file_id, {}).get(part[2]) if target is not None else None
        if value is None:
            if trace is not None and target is not None:
                trace.constants_file = target.file_id
            return CONSTANT_UNRESOLVED, [], {}, None
        path += value
    if trace is not None:
        trace.path = path
    if chains.blocked(key):
        return UNREADABLE_REGISTRATION, [], {}, None
    literal = meta.get("provenance") == EXACT
    bodies: dict[str, tuple[bool, frozenset[int]]] = {}
    served, refusal = chains.prefixes(key)
    for base, exact, root in served:
        full = base if path == "/" and base else f"{base}{path}"
        route = http.route_path(full) if full else None
        body = http.route_body(meta.get("method", ""), route) if route is not None else ""
        if not body or http.canonical_route(body) is None:
            continue
        was_exact, roots = bodies.get(body, (False, frozenset()))
        bodies[body] = (was_exact or (exact and literal), roots | {root})
    if bodies:
        return PROMOTED, sorted(bodies), bodies, None
    return NOT_REGISTERED, [], {}, refusal
