"""Python routes served through a mount in another module (HTTP linkage).

A FastAPI ``APIRouter`` or a Flask ``Blueprint`` serves nothing until an application
includes it, and that usually happens in another module -- the application's own, or
an application factory -- than the one declaring its routes. The parser records each
side as a claim (:mod:`repoatlas.parsers.http_python`):

* ``python-receiver`` -- an application, router or blueprint a module constructs: its
  kind, its own prefix, and the mounts the same module makes of it;
* ``python-route`` -- a route on a router or blueprint, on a receiver the module
  imports, or with a path naming an imported constant; and a literal route on a FastAPI
  application, marked ``served_here`` (its module's edge states it, the claim adds the
  paths of the applications that mount it);
* ``python-mount`` -- an imported router mounted on an application or router, or any
  router mounted from inside an application factory;
* ``python-class`` -- a module-level subclass of FastAPI's or Flask's application, offered
  to the modules that build an application from it;
* ``python-class-route`` -- a route on an application a module builds from a class it
  imports (or from a class of its own extending one): served at the application's root only
  if the module the class comes from offers it, the classes between it and the framework's
  and those above it in the importer at most :data:`_MAX_CLASS_DEPTH` in all. Whatever
  else it comes to is a reason in ``PythonRoutePlan.class_unproven``, never an outcome, never
  counted, never declared;
* ``python-constants`` -- a module's path-like string constants, and those it composes from
  literals and other names by an f-string or ``+``;
* ``python-script`` -- a module that runs as a script.

This stage serves a route only when every step is proven:

* each module a claim names resolves to exactly one indexed file. An absolute name is
  looked up first in the directory of the script the program runs from -- the nearest
  directory above the importer that holds a script and is no package, a script being a
  module with ``if __name__ == "__main__":`` or a file no import can name -- because
  that directory is first on the program's import path; then as
  :class:`~repoatlas.graph.py_modules.PythonModules` resolves it, in the importer's own
  project; and last, a name whose head is the importer's own outermost regular package
  from that package's parent, the only place a module importing it so is loaded from;
* the receiver it names is declared there, by that name, and is of a kind the
  decorator or the mount applies to;
* every constant in the path is a string constant, literal or composed, the named module binds
  once, resolved within :data:`MAX_CONSTANT_HOPS` modules (a composed constant naming another
  module's constant is followed; a cycle is refused);
* a chain of readable mounts, at most :data:`MAX_DEPTH` deep, reaches an application.
  A FastAPI route is served at each chain's prefixes, the router's own prefix and its
  path; a blueprint at the mount's ``url_prefix`` -- else its own -- joined to its rule,
  and only directly on an application, never nested in a blueprint.

An unreadable mount of a router -- dynamic arguments or prefix, an include on a
sub-application (unserved), a blueprint in a blueprint -- leaves that router unserved
everywhere, as an unreadable mount in its own module does.
So does an include or registration made on an application that a ``mount`` in any module
names: the mount's child key equals the owner's, so the owner is a sub-application (R1).
A ``mount`` whose child is not proven -- its module unresolved or ambiguous, a re-export, a
name no receiver declares -- counts as a mount of every application of that name in the
same repository (R5): identity not proven fails closed.
A ``mount`` of an application is *proven* when it carries ``mount_prefix`` (a literal FastAPI
prefix), its child is a FastAPI application and its owner is a FastAPI application. The child
is a sub-application still (R1) and keeps its own view, the routes at the paths its file
states; the parent's view adds ``mount_prefix`` + path, with the parent's module as the
application serving it. A chain continues through an owner that is a usable receiver -- a
sub-application mounted in turn -- ends at a factory's application, and never enters an
application twice. Any other ``mount`` blocks its child, as before: the child serves what its
own file states and nothing under a parent.
A route whose prefixes are all empty and whose path is literal is exact: its decorator
alone states it, and keeps its lines. Anything else is composed.

Everything is repository-local: modules resolve inside the importer's own project or
script directory, so a repository's share of the plan is the plan of its claims alone.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from repoatlas.graph.keys import global_key
from repoatlas.graph.py_modules import PythonModules
from repoatlas.graph.resolution import ResolutionIndex
from repoatlas.models import http
from repoatlas.models.parsing import EntityType
from repoatlas.parsers.http_facts import (
    EXACT,
    PYTHON_CLASS,
    PYTHON_CLASS_ROUTE,
    PYTHON_CONSTANTS,
    PYTHON_MOUNT,
    PYTHON_RECEIVER,
    PYTHON_ROUTE,
    PYTHON_SCRIPT,
)
from repoatlas.parsers.http_python import _MAX_CLASS_DEPTH, _path_like, flask_join
from repoatlas.storage.protocols import EntityRecord, FileRow, UnresolvedRefRecord

#: Routers nested in routers, counted from the application: deeper than anyone writes.
MAX_DEPTH = 4
#: Modules one imported constant is followed through: a composed constant may name another's.
MAX_CONSTANT_HOPS = 4

PROMOTED = "promoted"
MODULE_UNRESOLVED = "the module it names does not resolve"
NOT_DECLARED = "no application, router or blueprint of that name is declared there"
WRONG_KIND = "the decorator does not apply to that kind of receiver"
CONSTANT_UNRESOLVED = "a name in the path is no string constant of the module it names"
UNREADABLE_MOUNT = "a mount of its router cannot be read"
NOT_MOUNTED = "not mounted by a proven application"
#: A route its own module already serves, on a router no other module mounts.
SERVED_HERE = "served by its own module's mount"
#: Why a class route is not served: diagnostics in ``PythonRoutePlan.class_unproven``, never
#: outcomes.
CLASS_NOT_OFFERED = "no application subclass of that name is offered there"
CLASS_NOT_AN_APPLICATION = "the class it names does not build an application"
CLASS_TOO_DEEP = "the class stands too many classes above the framework's"
CLASS_NO_ROUTE = "the application's route is no route"

_FASTAPI_APP = "fastapi-app"
_FASTAPI_ROUTER = "fastapi-router"
_FLASK_APP = "flask-app"
_FLASK_BLUEPRINT = "flask-blueprint"
_APPLICATIONS = frozenset({_FASTAPI_APP, _FLASK_APP})
_FAMILY = {
    _FASTAPI_APP: "fastapi",
    _FASTAPI_ROUTER: "fastapi",
    _FLASK_APP: "flask",
    _FLASK_BLUEPRINT: "flask",
}
_PACKAGE_FILES = ("__init__.py", "__init__.pyi")

Key = tuple[int, str]
#: ``(file id, first line, last line, role)``: a claim that caused a refusal, for a reader to
#: place it (R1 diagnostics). Never read by a decision.
Place = tuple[int, int | None, int | None, str]

#: Diagnostic codes this stage establishes itself (R1): a flag set beside an unchanged return.
CODE_CONSTANT_HOPS = "python-constant-hops"
CODE_ROUTER_PATH_UNROOTED = "python-router-path-unrooted"
CODE_CHAIN_TOO_DEEP = "python-chain-too-deep"


def claim_place(claim: UnresolvedRefRecord, role: str) -> Place:
    return (claim.file_id, claim.line_start, claim.line_end, role)


def ordered_places(places: Sequence[Place]) -> tuple[Place, ...]:
    """The places without repeats, in a fixed order."""
    return tuple(sorted(set(places), key=lambda p: (p[0], p[1] or 0, p[2] or 0, p[3])))


@dataclass(slots=True)
class _Trace:
    """What one route claim's evaluation met, beside its unchanged result (R1 diagnostics)."""

    #: The receiver the route names, once its module resolved.
    key: Key | None = None
    #: The path text, once every part of it was evaluated.
    path: str | None = None
    #: A router or mount chain cut by :data:`MAX_DEPTH`.
    deep: bool = False
    #: A constant followed through :data:`MAX_CONSTANT_HOPS` modules and no further.
    hops: bool = False
    #: A constant that names itself again (not a bound).
    cycle: bool = False
    #: A router route whose path has no leading ``/``.
    unrooted: bool = False


@dataclass(frozen=True, slots=True)
class PendingRoute:
    """A route a handler serves, proven here, its route node not yet interned."""

    src_id: int
    body: str
    repo_id: int
    file_id: int
    line_start: int | None
    line_end: int | None
    #: The modules constructing the applications that serve it (file ids): what a
    #: deployment's command must run for its identity to expose the route. Empty when
    #: the stage does not trace its applications.
    roots: frozenset[int] = frozenset()
    #: The line its claim states it at, cited when the route itself is composed.
    declared_at: int | None = None

    @property
    def target(self) -> EntityRecord:
        return EntityRecord(
            type=EntityType.HTTP_ROUTE,
            key=global_key(EntityType.HTTP_ROUTE, self.body),
            name=self.body,
            qualified_name=self.body,
        )


@dataclass(slots=True)
class PythonRoutePlan:
    routes: list[PendingRoute] = field(default_factory=list)
    #: Route claim id → ``promoted`` or why not.
    outcomes: dict[int, str] = field(default_factory=dict)
    #: Route claim id → the route bodies it is served at.
    promoted: dict[int, tuple[str, ...]] = field(default_factory=dict)
    #: Route claim id → its receiver-relative route body, when it is served nowhere.
    declared: dict[int, str] = field(default_factory=dict)
    resolved: int = 0
    unresolved: int = 0
    #: Class-route claim id → why its class is no proven application (diagnostic only). Never
    #: merged into ``outcomes``, never counted in ``unresolved``, never in ``declared``: a route
    #: on a receiver whose class only another module could vouch for is not a declared route.
    class_unproven: dict[int, str] = field(default_factory=dict)
    #: Diagnostic facts (R1), recorded beside the results above and read by none of them.
    #: Route (or class route) claim id → the code of the flag the stage itself set when the
    #: claim was not served: ``python-constant-hops``, ``python-router-path-unrooted`` or
    #: ``python-chain-too-deep``. A claim with no such flag has no entry.
    route_codes: dict[int, str] = field(default_factory=dict)
    #: Claim id → the receiver-relative route body its path evaluates to, for a claim that is not
    #: served and not in ``declared`` (a path built from constants, say).
    resolved_routes: dict[int, str] = field(default_factory=dict)
    #: Claim id → the mounts that left its router unreadable (file, lines, ``mount``).
    related: dict[int, tuple[Place, ...]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _Receiver:
    kind: str
    prefix: str | None
    usable: bool
    #: Prefixes of the mounts its own module makes of it, on applications there.
    mounts: tuple[str | None, ...]


@dataclass(frozen=True, slots=True)
class _Mount:
    owner: Key
    owner_kind: str
    call: str
    prefix: str | None


def _meta(claim: UnresolvedRefRecord) -> dict[str, str]:
    loaded = json.loads(claim.meta_json) if claim.meta_json else {}
    return {str(k): str(v) for k, v in loaded.items()}


def _mount_path(meta: Mapping[str, str]) -> str | None:
    """The literal path an application ``mount`` claim states, when it states one."""
    raw = meta.get("mount_prefix")
    if raw is None:
        return None
    try:
        loaded = json.loads(raw)
    except ValueError:
        return None
    return loaded if isinstance(loaded, str) and loaded else None


def _parent(path: str) -> str:
    """The directory of a repository-relative POSIX path, ``""`` at the root."""
    return path.rpartition("/")[0]


def _join(directory: str, name: str) -> str:
    return f"{directory}/{name}" if directory else name


@dataclass(frozen=True, slots=True)
class _Layout:
    directories: frozenset[str]
    #: Directories a script runs from, none of them a package.
    scripts: frozenset[str]


class _Imports:
    """Module names, resolved the way the program that imports them would."""

    def __init__(
        self,
        modules: PythonModules,
        index: ResolutionIndex,
        scripts: Sequence[UnresolvedRefRecord],
        by_id: Mapping[int, FileRow],
    ) -> None:
        self._modules = modules
        self._index = index
        self._claimed: dict[int, set[str]] = defaultdict(set)
        for claim in scripts:
            row = by_id.get(claim.file_id)
            if row is not None:
                self._claimed[row.repo_id].add(_parent(row.rel_path))
        #: Read on first use: only repositories a claim resolves in pay for a layout.
        self._layouts: dict[int, _Layout] = {}

    def _layout(self, repo_id: int) -> _Layout:
        layout = self._layouts.get(repo_id)
        if layout is None:
            files = self._index.files_by_repo.get(repo_id, {})
            directories: set[str] = set()
            roots = set(self._claimed.get(repo_id, ()))
            for path in files:
                parent, _, name = path.rpartition("/")
                if name.endswith(".py") and not name[:-3].isidentifier():
                    roots.add(parent)
                while parent and parent not in directories:
                    directories.add(parent)
                    parent = _parent(parent)
            layout = _Layout(
                frozenset(directories),
                frozenset(
                    directory
                    for directory in roots
                    if not any(_join(directory, name) in files for name in _PACKAGE_FILES)
                ),
            )
            self._layouts[repo_id] = layout
        return layout

    def resolve(self, origin: FileRow, module: str) -> FileRow | None:
        if module.startswith("."):
            target = self._modules.resolve(origin, module)
            return None if isinstance(target, str) else target.file
        script = self._script_directory(origin)
        found = self._under(origin.repo_id, script, module) if script is not None else None
        if found is not None:
            return found
        target = self._modules.resolve(origin, module)
        if not isinstance(target, str):
            return target.file
        return self._own_package(origin, module)

    def _script_directory(self, origin: FileRow) -> str | None:
        """The nearest directory at or above ``origin`` that holds a script."""
        scripts = self._layout(origin.repo_id).scripts
        directory = _parent(origin.rel_path)
        while directory not in scripts:
            if not directory:
                return None
            directory = _parent(directory)
        return directory

    def _own_package(self, origin: FileRow, module: str) -> FileRow | None:
        """``from src.interfaces import x`` inside the regular package ``src``.

        A module of a regular package that names its outermost package absolutely is
        only ever loaded with that package's parent directory on the import path, so
        the name resolves there.
        """
        files = self._index.files_by_repo.get(origin.repo_id, {})
        outermost: str | None = None
        directory = _parent(origin.rel_path)
        while directory and any(_join(directory, name) in files for name in _PACKAGE_FILES):
            outermost = directory
            directory = _parent(directory)
        if outermost is None or module.split(".")[0] != outermost.rpartition("/")[2]:
            return None
        return self._under(origin.repo_id, _parent(outermost), module)

    def _under(self, repo_id: int, directory: str, module: str) -> FileRow | None:
        """The file ``module`` names when ``directory`` is on the import path."""
        files = self._index.files_by_repo.get(repo_id, {})
        parts = module.split(".")
        if not all(part.isidentifier() for part in parts):
            return None
        directories = self._layout(repo_id).directories
        for part in parts[:-1]:
            directory = _join(directory, part)
            if directory not in directories:
                return None
        base = _join(directory, parts[-1])
        # A regular package before a module of the same name, as CPython looks.
        for candidate in (*(f"{base}/{name}" for name in _PACKAGE_FILES), f"{base}.py"):
            if candidate in files:
                return files[candidate]
        return None


def plan_python_routes(
    grouped: Mapping[str, Sequence[UnresolvedRefRecord]],
    modules: Callable[[], PythonModules],
    index: ResolutionIndex,
    by_id: Mapping[int, FileRow],
) -> PythonRoutePlan:
    """Serve every Python route claim a proven mount chain reaches. Writes nothing."""
    plan = PythonRoutePlan()
    claims = grouped.get(PYTHON_ROUTE, ())
    class_claims = grouped.get(PYTHON_CLASS_ROUTE, ())
    if not claims and not class_claims:
        return plan
    imports = _Imports(modules(), index, grouped.get(PYTHON_SCRIPT, ()), by_id)

    receivers: dict[Key, _Receiver] = {}
    for claim in grouped.get(PYTHON_RECEIVER, ()):
        meta = _meta(claim)
        receivers[(claim.file_id, claim.target_key)] = _Receiver(
            kind=meta.get("kind", ""),
            prefix=json.loads(meta.get("prefix", "null")),
            usable=meta.get("usable") == "true",
            mounts=tuple(json.loads(meta.get("mounts", "[]"))),
        )
    constants: dict[int, dict[str, str]] = {}
    composed: dict[int, dict[str, list[list[str]]]] = {}
    for claim in grouped.get(PYTHON_CONSTANTS, ()):
        meta = _meta(claim)
        table = json.loads(meta.get("constants", "{}"))
        constants[claim.file_id] = {name: value for name, (value, _) in table.items()}
        offered = json.loads(meta.get("composed", "{}"))
        composed[claim.file_id] = {name: entry[0] for name, entry in offered.items()}

    applications: dict[tuple[int, str], list[Key]] = defaultdict(list)
    for key, receiver in receivers.items():
        declared_in = by_id.get(key[0])
        if receiver.kind in _APPLICATIONS and declared_in is not None:
            applications[(declared_in.repo_id, key[1])].append(key)

    mounts: dict[Key, list[_Mount]] = defaultdict(list)
    #: Application → the (owner, prefix) of every proven mount of it on another application.
    app_mounts: dict[Key, list[tuple[Key, str]]] = defaultdict(list)
    mounted_apps: set[Key] = set()
    blocked: set[Key] = set()
    #: Where each blocked receiver was blocked: the mount claims (diagnostics only).
    blocked_at: dict[Key, list[Place]] = defaultdict(list)
    readable: list[tuple[Key, _Mount, Place]] = []
    for claim in grouped.get(PYTHON_MOUNT, ()):
        meta = _meta(claim)
        origin = by_id.get(claim.file_id)
        if origin is None:
            continue
        child = _key(imports, origin, meta.get("module"), claim.target_key)
        if meta.get("call") == "mount" and (child is None or child not in receivers):
            # Identity not proven: any application of that name in this repository may be it.
            blocked.update(applications.get((origin.repo_id, claim.target_key), ()))
            for application in applications.get((origin.repo_id, claim.target_key), ()):
                blocked_at[application].append(claim_place(claim, "mount"))
        if child is None:
            continue
        mounted_at = _mount_path(meta)
        if (
            meta.get("call") == "mount"
            and mounted_at is not None
            and meta.get("owner_kind") == _FASTAPI_APP
            and child in receivers
            and receivers[child].kind == _FASTAPI_APP
        ):
            # Proven: a sub-application still (R1), served under the mount besides its own paths.
            app_mounts[child].append(((claim.file_id, meta.get("owner", "")), mounted_at))
            mounted_apps.add(child)
            continue
        if meta.get("readable") != "true":
            blocked.add(child)
            blocked_at[child].append(claim_place(claim, "mount"))
            continue
        mount = _Mount(
            owner=(claim.file_id, meta.get("owner", "")),
            owner_kind=meta.get("owner_kind", ""),
            call=meta.get("call", ""),
            prefix=json.loads(meta.get("prefix", "null")),
        )
        readable.append((child, mount, claim_place(claim, "mount")))
    # An include or registration on a sub-application is unreadable, whatever module mounts it.
    # Decided against the sub-applications as every mount claim leaves them, so the claims'
    # order does not matter; the children it adds are routers, never applications.
    sub_applications = frozenset(blocked | mounted_apps)
    for child, mount, where in readable:
        if mount.owner_kind in _APPLICATIONS and mount.owner in sub_applications:
            blocked.add(child)
            blocked_at[child].append(where)
            continue
        mounts[child].append(mount)

    chains = _Chains(receivers, mounts, blocked, app_mounts)
    for claim in claims:
        trace = _Trace()
        outcome, bodies, exact = _route(
            claim, imports, receivers, constants, composed, chains, by_id, trace
        )
        _record_facts(plan, claim, outcome, trace, blocked_at, _relative(claim) is not None)
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
        if outcome == SERVED_HERE:
            continue
        plan.unresolved += 1
        relative = _relative(claim)
        if claim.id is not None and relative is not None:
            plan.declared[claim.id] = relative

    classes = _offered_classes(grouped.get(PYTHON_CLASS, ()))
    for claim in class_claims:
        trace = _Trace()
        outcome, bodies, exact = _class_route(
            claim, imports, classes, constants, composed, by_id, trace
        )
        _record_facts(plan, claim, outcome, trace, blocked_at, False)
        if outcome != PROMOTED:
            if claim.id is not None:
                plan.class_unproven[claim.id] = outcome
            continue
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
            plan.outcomes[claim.id] = outcome
            plan.promoted[claim.id] = tuple(bodies)
    return plan


def _record_facts(
    plan: PythonRoutePlan,
    claim: UnresolvedRefRecord,
    outcome: str,
    trace: _Trace,
    blocked_at: Mapping[Key, Sequence[Place]],
    will_declare: bool,
) -> None:
    """Record, beside a claim's unchanged outcome, what its evaluation met (R1 diagnostics).

    Nothing is recorded for a claim that is served. ``will_declare``: the claim lands in
    ``plan.declared``, whose route body is already a fact of the plan."""
    if claim.id is None or outcome in (PROMOTED, SERVED_HERE):
        return
    code: str | None = None
    if outcome == CONSTANT_UNRESOLVED and trace.hops:
        code = CODE_CONSTANT_HOPS
    elif outcome == NOT_MOUNTED and trace.unrooted:
        code = CODE_ROUTER_PATH_UNROOTED
    elif outcome == NOT_MOUNTED and trace.deep:
        code = CODE_CHAIN_TOO_DEEP
    if code is not None:
        plan.route_codes[claim.id] = code
    if outcome == UNREADABLE_MOUNT and trace.key is not None and blocked_at.get(trace.key):
        plan.related[claim.id] = ordered_places(blocked_at[trace.key])
    if trace.path is not None and not will_declare:
        route = http.route_path(trace.path or "/")
        body = http.route_body(_meta(claim).get("method", ""), route) if route is not None else ""
        if body and http.canonical_route(body) is not None:
            plan.resolved_routes[claim.id] = body


def _offered_classes(claims: Sequence[UnresolvedRefRecord]) -> dict[Key, tuple[str, int]]:
    """Each application subclass a module offers, by (its file, its name): kind and depth."""
    offered: dict[Key, tuple[str, int]] = {}
    for claim in claims:
        meta = _meta(claim)
        depth = meta.get("depth", "")
        if depth.isdigit():
            offered[(claim.file_id, claim.target_key)] = (meta.get("kind", ""), int(depth))
    return offered


def _key(imports: _Imports, origin: FileRow, module: str | None, name: str) -> Key | None:
    if module is None:
        return origin.file_id, name
    target = imports.resolve(origin, module)
    return (target.file_id, name) if target is not None else None


def _relative(claim: UnresolvedRefRecord) -> str | None:
    meta = _meta(claim)
    parts = json.loads(meta.get("path", "[]"))
    if not parts or any(part[0] != "t" for part in parts):
        return None
    path = http.route_path("".join(part[1] for part in parts) or "/")
    body = http.route_body(meta.get("method", ""), path) if path is not None else ""
    return body if body and http.canonical_route(body) is not None else None


class _Chains:
    """Where a receiver's routes are served: the mount chains that reach an application."""

    def __init__(
        self,
        receivers: Mapping[Key, _Receiver],
        mounts: Mapping[Key, Sequence[_Mount]],
        blocked: set[Key],
        app_mounts: Mapping[Key, Sequence[tuple[Key, str]]] | None = None,
    ) -> None:
        self._receivers = receivers
        self._mounts = mounts
        self._blocked = blocked
        self._app_mounts = app_mounts or {}

    def usable(self, key: Key) -> bool:
        receiver = self._receivers.get(key)
        return receiver is not None and receiver.usable and key not in self._blocked

    def mounted(
        self,
        key: Key,
        depth: int = 0,
        seen: frozenset[Key] | None = None,
        trace: _Trace | None = None,
    ) -> list[tuple[str, int]]:
        """``(prefix, root)`` such that a route path ``p`` on application ``key`` is also
        served at ``prefix + p`` by the application module ``root``: each proven mount of it on
        another application, then that application's own mounts in turn, for as long as the
        owner is a usable receiver. A parent that is a receiver but blocked contributes nothing;
        a factory's application, which is no receiver, ends the chain with its own file as
        root. An application already on the chain is never entered again."""
        if depth > MAX_DEPTH:
            if trace is not None:
                trace.deep = True
            return []
        visited = seen if seen is not None else frozenset({key})
        found: list[tuple[str, int]] = []
        for owner, prefix in self._app_mounts.get(key, ()):
            if owner in visited:
                continue
            if owner not in self._receivers:
                found.append((prefix, owner[0]))
                continue
            if not self.usable(owner):
                continue
            found.append((prefix, owner[0]))
            found.extend(
                (f"{base}{prefix}", root)
                for base, root in self.mounted(owner, depth + 1, visited | {owner}, trace)
            )
        return found

    def fastapi(
        self, key: Key, *, own_module: bool, depth: int = 0, trace: _Trace | None = None
    ) -> list[tuple[str, bool, int]]:
        """``(prefix, exact, root)`` such that a route path ``p`` on router ``key`` is served
        at ``prefix + p`` by the application module ``root``: the chain's prefixes and the
        router's own. The mounts its own module makes are left out at the first level when
        the route is in that module, which serves those itself."""
        receiver = self._receivers.get(key)
        if receiver is None:
            return []
        if depth > MAX_DEPTH:
            if trace is not None:
                trace.deep = True
            return []
        if not self.usable(key):
            return []
        own = receiver.prefix or ""
        found: list[tuple[str, bool, int]] = []
        if not own_module:
            for mount in receiver.mounts:
                found.append((f"{mount or ''}{own}", not mount and not own, key[0]))
        for mount in self._mounts.get(key, ()):
            if mount.call != "include_router":
                continue
            prefix = mount.prefix or ""
            if mount.owner_kind == _FASTAPI_APP:
                found.append((f"{prefix}{own}", not prefix and not own, mount.owner[0]))
            elif mount.owner_kind == _FASTAPI_ROUTER:
                for base, exact, root in self.fastapi(
                    mount.owner, own_module=False, depth=depth + 1, trace=trace
                ):
                    found.append((f"{base}{prefix}{own}", exact and not prefix and not own, root))
        return found

    def flask(self, key: Key, *, own_module: bool) -> list[tuple[str | None, int]]:
        """The prefix each mount on an application serves a blueprint's rules under -- the
        mount's ``url_prefix``, else the blueprint's own -- and that application's module."""
        receiver = self._receivers.get(key)
        if receiver is None or not self.usable(key):
            return []
        chosen: list[tuple[str | None, int]] = (
            [] if own_module else [(mount, key[0]) for mount in receiver.mounts]
        )
        chosen.extend(
            (mount.prefix, mount.owner[0])
            for mount in self._mounts.get(key, ())
            if mount.call == "register_blueprint" and mount.owner_kind == _FLASK_APP
        )
        return [
            (prefix if prefix is not None else receiver.prefix, root) for prefix, root in chosen
        ]


def _constant_value(
    imports: _Imports,
    constants: Mapping[int, Mapping[str, str]],
    composed: Mapping[int, Mapping[str, list[list[str]]]],
    origin: FileRow,
    module: str,
    name: str,
    hops_left: int = MAX_CONSTANT_HOPS,
    seen: frozenset[Key] = frozenset(),
    trace: _Trace | None = None,
) -> str | None:
    """The text of the string constant ``name`` of ``module``, as ``origin`` imports it.

    A literal constant is its text. A composed one is rebuilt from its pieces: an imported
    name in it is followed in turn, relative to the module that states it, through at most
    :data:`MAX_CONSTANT_HOPS` modules and never twice through the same constant. The text must
    be one a path could be made of, as the literal constants offered are.
    """
    target = imports.resolve(origin, module)
    if target is None:
        return None
    key = (target.file_id, name)
    if key in seen:
        if trace is not None:
            trace.cycle = True
        return None
    if hops_left == 0:
        if trace is not None:
            trace.hops = True
        return None
    literal = constants.get(target.file_id, {}).get(name)
    if literal is not None:
        return literal
    parts = composed.get(target.file_id, {}).get(name)
    if parts is None:
        return None
    text = ""
    for part in parts:
        if part[0] == "t":
            text += part[1]
            continue
        found = _constant_value(
            imports,
            constants,
            composed,
            target,
            part[1],
            part[2],
            hops_left - 1,
            seen | {key},
            trace,
        )
        if found is None:
            return None
        text += found
    return text if _path_like(text) else None


def _class_route(
    claim: UnresolvedRefRecord,
    imports: _Imports,
    offered: Mapping[Key, tuple[str, int]],
    constants: Mapping[int, Mapping[str, str]],
    composed: Mapping[int, Mapping[str, list[list[str]]]],
    by_id: Mapping[int, FileRow],
    trace: _Trace | None = None,
) -> tuple[str, list[str], dict[str, tuple[bool, frozenset[int]]]]:
    """A route on an application built from a class another module defines.

    Served at the application's root, by the module that builds it, when the module the
    class comes from resolves to one indexed file that offers it as an application subclass
    and the classes above the framework's, there and here, are at most :data:`_MAX_CLASS_DEPTH`.
    Anything else leaves the route unserved, with the reason a diagnostic only.
    """
    meta = _meta(claim)
    origin = by_id.get(claim.file_id)
    if origin is None:
        return MODULE_UNRESOLVED, [], {}
    target = imports.resolve(origin, meta.get("module", ""))
    if target is None:
        return MODULE_UNRESOLVED, [], {}
    found = offered.get((target.file_id, meta.get("class", "")))
    if found is None:
        return CLASS_NOT_OFFERED, [], {}
    kind, depth = found
    if kind not in _APPLICATIONS:
        return CLASS_NOT_AN_APPLICATION, [], {}
    here = meta.get("depth", "")
    if not here.isdigit() or depth + int(here) > _MAX_CLASS_DEPTH:
        return CLASS_TOO_DEEP, [], {}
    if meta.get("family", "") not in {"either", _FAMILY.get(kind)}:
        return WRONG_KIND, [], {}
    path = ""
    for part in json.loads(meta.get("path", "[]")):
        if part[0] == "t":
            path += part[1]
            continue
        value = _constant_value(imports, constants, composed, origin, part[1], part[2], trace=trace)
        if value is None:
            return CONSTANT_UNRESOLVED, [], {}
        path += value
    if trace is not None:
        trace.path = path
    route = http.route_path(path) if path else None
    body = http.route_body(meta.get("method", ""), route) if route is not None else ""
    if not body or http.canonical_route(body) is None:
        return CLASS_NO_ROUTE, [], {}
    exact = meta.get("provenance") == EXACT
    return PROMOTED, [body], {body: (exact, frozenset({claim.file_id}))}


def _route(
    claim: UnresolvedRefRecord,
    imports: _Imports,
    receivers: Mapping[Key, _Receiver],
    constants: Mapping[int, Mapping[str, str]],
    composed: Mapping[int, Mapping[str, list[list[str]]]],
    chains: _Chains,
    by_id: Mapping[int, FileRow],
    trace: _Trace | None = None,
) -> tuple[str, list[str], dict[str, tuple[bool, frozenset[int]]]]:
    meta = _meta(claim)
    origin = by_id.get(claim.file_id)
    if origin is None:
        return MODULE_UNRESOLVED, [], {}
    module = meta.get("module")
    key = _key(imports, origin, module, meta.get("router", ""))
    if key is None:
        return MODULE_UNRESOLVED, [], {}
    if trace is not None:
        trace.key = key
    receiver = receivers.get(key)
    if receiver is None:
        return NOT_DECLARED, [], {}
    family = meta.get("family", "")
    if family not in {"either", _FAMILY.get(receiver.kind)}:
        return WRONG_KIND, [], {}
    path = ""
    for part in json.loads(meta.get("path", "[]")):
        if part[0] == "t":
            path += part[1]
            continue
        value = _constant_value(imports, constants, composed, origin, part[1], part[2], trace=trace)
        if value is None:
            return CONSTANT_UNRESOLVED, [], {}
        path += value
    if trace is not None:
        trace.path = path
    # A literal route on a FastAPI application, whose own module's edge states it as written.
    served_here = meta.get("served_here") == "true"
    if not chains.usable(key):
        return (SERVED_HERE if served_here else UNREADABLE_MOUNT), [], {}
    literal = meta.get("provenance") == EXACT
    own_module = module is None
    served: list[tuple[str | None, bool, int]]
    if receiver.kind in _APPLICATIONS:
        served = [] if served_here else [(path, True, key[0])]
        if receiver.kind == _FASTAPI_APP and path.startswith("/"):
            served.extend(
                (f"{prefix}{path}", False, root)
                for prefix, root in chains.mounted(key, trace=trace)
            )
    elif receiver.kind == _FASTAPI_ROUTER:
        if path and not path.startswith("/"):
            if trace is not None:
                trace.unrooted = True
            return NOT_MOUNTED, [], {}
        served = [
            (f"{base}{path}", exact, root)
            for base, exact, root in chains.fastapi(key, own_module=own_module, trace=trace)
        ]
    else:
        served = [
            (flask_join(prefix, path), not prefix, root)
            for prefix, root in chains.flask(key, own_module=own_module)
        ]
    bodies: dict[str, tuple[bool, frozenset[int]]] = {}
    for full, exact, root in served:
        route = http.route_path(full) if full else None
        body = http.route_body(meta.get("method", ""), route) if route is not None else ""
        if not body or http.canonical_route(body) is None:
            continue
        was_exact, roots = bodies.get(body, (False, frozenset()))
        bodies[body] = (was_exact or (exact and literal), roots | {root})
    if bodies:
        return PROMOTED, sorted(bodies), bodies
    if served_here:
        return SERVED_HERE, [], {}
    if own_module and receiver.mounts:
        return SERVED_HERE, [], {}
    return NOT_MOUNTED, [], {}
