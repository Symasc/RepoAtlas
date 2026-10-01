"""Python routes served through a mount in another module (HTTP linkage).

A FastAPI ``APIRouter`` or a Flask ``Blueprint`` serves nothing until an application
includes it, and that usually happens in another module -- the application's own, or
an application factory -- than the one declaring its routes. The parser records each
side as a claim (:mod:`repoatlas.parsers.http_python`):

* ``python-receiver`` -- an application, router or blueprint a module constructs: its
  kind, its own prefix, and the mounts the same module makes of it;
* ``python-route`` -- a route on a router or blueprint, on a receiver the module
  imports, or with a path naming an imported constant;
* ``python-mount`` -- an imported router mounted on an application or router, or any
  router mounted from inside an application factory;
* ``python-constants`` -- a module's path-like string constants;
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
* every constant in the path is a string constant the named module binds once;
* a chain of readable mounts, at most :data:`MAX_DEPTH` deep, reaches an application.
  A FastAPI route is served at each chain's prefixes, the router's own prefix and its
  path; a blueprint at the mount's ``url_prefix`` -- else its own -- joined to its rule,
  and only directly on an application, never nested in a blueprint.

An unreadable mount of a router -- dynamic arguments or prefix, a sub-application
``mount``, a blueprint in a blueprint -- leaves that router unserved everywhere, as an
unreadable mount in its own module does. A route whose prefixes are all empty and whose
path is literal is exact: its decorator alone states it, and keeps its lines. Anything
else is composed.

Everything is repository-local: modules resolve inside the importer's own project or
script directory, so a repository's share of the plan is the plan of its claims alone.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from repoatlas.graph.keys import global_key
from repoatlas.graph.py_modules import PythonModules
from repoatlas.graph.resolution import ResolutionIndex
from repoatlas.models import http
from repoatlas.models.parsing import EntityType
from repoatlas.parsers.http_facts import (
    EXACT,
    PYTHON_CONSTANTS,
    PYTHON_MOUNT,
    PYTHON_RECEIVER,
    PYTHON_ROUTE,
    PYTHON_SCRIPT,
)
from repoatlas.parsers.http_python import flask_join
from repoatlas.storage.protocols import EntityRecord, FileRow, UnresolvedRefRecord

#: Routers nested in routers, counted from the application: deeper than anyone writes.
MAX_DEPTH = 4

PROMOTED = "promoted"
MODULE_UNRESOLVED = "the module it names does not resolve"
NOT_DECLARED = "no application, router or blueprint of that name is declared there"
WRONG_KIND = "the decorator does not apply to that kind of receiver"
CONSTANT_UNRESOLVED = "a name in the path is no string constant of the module it names"
UNREADABLE_MOUNT = "a mount of its router cannot be read"
NOT_MOUNTED = "not mounted by a proven application"
#: A route its own module already serves, on a router no other module mounts.
SERVED_HERE = "served by its own module's mount"

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


@dataclass(frozen=True, slots=True)
class PendingRoute:
    """A route a handler serves, proven here, its route node not yet interned."""

    src_id: int
    body: str
    repo_id: int
    file_id: int
    line_start: int | None
    line_end: int | None

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


def _parent(path: str) -> str:
    parent = str(PurePosixPath(path).parent)
    return "" if parent == "." else parent


def _join(directory: str, name: str) -> str:
    return f"{directory}/{name}" if directory else name


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
        self._directories: dict[int, set[str]] = defaultdict(set)
        roots: dict[int, set[str]] = defaultdict(set)
        for repo_id, files in index.files_by_repo.items():
            for path in files:
                parent = _parent(path)
                while parent and parent not in self._directories[repo_id]:
                    self._directories[repo_id].add(parent)
                    parent = _parent(parent)
                stem = PurePosixPath(path).stem
                if path.endswith(".py") and not stem.isidentifier():
                    roots[repo_id].add(_parent(path))
        for claim in scripts:
            row = by_id.get(claim.file_id)
            if row is not None:
                roots[row.repo_id].add(_parent(row.rel_path))
        self._scripts = {
            repo_id: {
                directory
                for directory in directories
                if not any(
                    _join(directory, name) in index.files_by_repo.get(repo_id, {})
                    for name in _PACKAGE_FILES
                )
            }
            for repo_id, directories in roots.items()
        }

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
        scripts = self._scripts.get(origin.repo_id, set())
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
        if outermost is None or module.split(".")[0] != PurePosixPath(outermost).name:
            return None
        return self._under(origin.repo_id, _parent(outermost), module)

    def _under(self, repo_id: int, directory: str, module: str) -> FileRow | None:
        """The file ``module`` names when ``directory`` is on the import path."""
        files = self._index.files_by_repo.get(repo_id, {})
        parts = module.split(".")
        if not all(part.isidentifier() for part in parts):
            return None
        for part in parts[:-1]:
            directory = _join(directory, part)
            if directory not in self._directories[repo_id]:
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
    if not claims:
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
    for claim in grouped.get(PYTHON_CONSTANTS, ()):
        table = json.loads(_meta(claim).get("constants", "{}"))
        constants[claim.file_id] = {name: value for name, (value, _) in table.items()}

    mounts: dict[Key, list[_Mount]] = defaultdict(list)
    blocked: set[Key] = set()
    for claim in grouped.get(PYTHON_MOUNT, ()):
        meta = _meta(claim)
        origin = by_id.get(claim.file_id)
        if origin is None:
            continue
        child = _key(imports, origin, meta.get("module"), claim.target_key)
        if child is None:
            continue
        if meta.get("readable") != "true":
            blocked.add(child)
            continue
        mounts[child].append(
            _Mount(
                owner=(claim.file_id, meta.get("owner", "")),
                owner_kind=meta.get("owner_kind", ""),
                call=meta.get("call", ""),
                prefix=json.loads(meta.get("prefix", "null")),
            )
        )

    chains = _Chains(receivers, mounts, blocked)
    for claim in claims:
        outcome, bodies, exact = _route(claim, imports, receivers, constants, chains, by_id)
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
        if outcome == SERVED_HERE:
            continue
        plan.unresolved += 1
        relative = _relative(claim)
        if claim.id is not None and relative is not None:
            plan.declared[claim.id] = relative
    return plan


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
    ) -> None:
        self._receivers = receivers
        self._mounts = mounts
        self._blocked = blocked

    def usable(self, key: Key) -> bool:
        receiver = self._receivers.get(key)
        return receiver is not None and receiver.usable and key not in self._blocked

    def fastapi(self, key: Key, *, own_module: bool, depth: int = 0) -> list[tuple[str, bool]]:
        """``(prefix, exact)`` such that a route path ``p`` on router ``key`` is served at
        ``prefix + p``: the chain's prefixes and the router's own. The mounts its own module
        makes are left out at the first level when the route is in that module, which
        serves those itself."""
        receiver = self._receivers.get(key)
        if receiver is None or depth > MAX_DEPTH or not self.usable(key):
            return []
        own = receiver.prefix or ""
        found: list[tuple[str, bool]] = []
        if not own_module:
            for mount in receiver.mounts:
                found.append((f"{mount or ''}{own}", not mount and not own))
        for mount in self._mounts.get(key, ()):
            if mount.call != "include_router":
                continue
            prefix = mount.prefix or ""
            if mount.owner_kind == _FASTAPI_APP:
                found.append((f"{prefix}{own}", not prefix and not own))
            elif mount.owner_kind == _FASTAPI_ROUTER:
                for base, exact in self.fastapi(mount.owner, own_module=False, depth=depth + 1):
                    found.append((f"{base}{prefix}{own}", exact and not prefix and not own))
        return found

    def flask(self, key: Key, *, own_module: bool) -> list[str | None]:
        """The prefix each mount on an application serves a blueprint's rules under: the
        mount's ``url_prefix``, else the blueprint's own."""
        receiver = self._receivers.get(key)
        if receiver is None or not self.usable(key):
            return []
        chosen: list[str | None] = [] if own_module else list(receiver.mounts)
        chosen.extend(
            mount.prefix
            for mount in self._mounts.get(key, ())
            if mount.call == "register_blueprint" and mount.owner_kind == _FLASK_APP
        )
        return [prefix if prefix is not None else receiver.prefix for prefix in chosen]


def _route(
    claim: UnresolvedRefRecord,
    imports: _Imports,
    receivers: Mapping[Key, _Receiver],
    constants: Mapping[int, Mapping[str, str]],
    chains: _Chains,
    by_id: Mapping[int, FileRow],
) -> tuple[str, list[str], dict[str, bool]]:
    meta = _meta(claim)
    origin = by_id.get(claim.file_id)
    if origin is None:
        return MODULE_UNRESOLVED, [], {}
    module = meta.get("module")
    key = _key(imports, origin, module, meta.get("router", ""))
    if key is None:
        return MODULE_UNRESOLVED, [], {}
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
        target = imports.resolve(origin, part[1])
        value = constants.get(target.file_id, {}).get(part[2]) if target is not None else None
        if value is None:
            return CONSTANT_UNRESOLVED, [], {}
        path += value
    if not chains.usable(key):
        return UNREADABLE_MOUNT, [], {}
    literal = meta.get("provenance") == EXACT
    own_module = module is None
    served: list[tuple[str | None, bool]]
    if receiver.kind in _APPLICATIONS:
        served = [(path, True)]
    elif receiver.kind == _FASTAPI_ROUTER:
        if path and not path.startswith("/"):
            return NOT_MOUNTED, [], {}
        served = [
            (f"{base}{path}", exact) for base, exact in chains.fastapi(key, own_module=own_module)
        ]
    else:
        served = [
            (flask_join(prefix, path), not prefix)
            for prefix in chains.flask(key, own_module=own_module)
        ]
    bodies: dict[str, bool] = {}
    for full, exact in served:
        route = http.route_path(full) if full else None
        body = http.route_body(meta.get("method", ""), route) if route is not None else ""
        if not body or http.canonical_route(body) is None:
            continue
        bodies[body] = bodies.get(body, False) or (exact and literal)
    if bodies:
        return PROMOTED, sorted(bodies), bodies
    if own_module and receiver.mounts:
        return SERVED_HERE, [], {}
    return NOT_MOUNTED, [], {}
