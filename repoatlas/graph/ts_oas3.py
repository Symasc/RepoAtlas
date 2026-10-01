"""Operations an oas3-tools application serves through its controllers (HTTP linkage).

An ``oas3-tools`` application serves an OpenAPI 3 document's operations by calling the
member of its controllers object that each ``operationId`` names. The parsers record
each side as a claim:

* ``oas3-app`` -- the application: the specification path it reads, relative to its
  file, and the handler objects its controllers are made of
  (:mod:`repoatlas.parsers.http_oas3`);
* ``ts-handler-object`` -- a ``const`` object whose every member is a function: its
  keys, and the names it is exported under;
* ``openapi-operations`` -- the document's operations by ``operationId``, and the base
  paths its root ``servers`` give them (:mod:`repoatlas.parsers.http_openapi`).

This stage serves an operation only when every step is proven:

* the specification path names exactly one indexed OpenAPI 3 document in the
  application's own repository, whose root servers' base paths are read;
* every controllers object resolves to exactly one handler object: declared in the
  application's file, or exported under that name by the one module a relative import
  resolves to. A later object's key replaces an earlier one's, as a spread does;
* the operation's handler name -- :func:`handler_name`, the library's own -- is a key.

Each such operation is served by the handler object's ``const``, at each base path
joined to the operation's path. The route is assembled from the document and the
controllers, so it is composed: no span. An operation whose handler name is no key
is served by nothing here (the library answers 405); the document itself still
declares it, as it always did.

Everything is repository-local: a relative import and a specification path resolve
inside their own repository, so a repository's share of the plan is the plan of its
claims alone.
"""

from __future__ import annotations

import json
import posixpath
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from repoatlas.graph.py_routes import PendingRoute
from repoatlas.graph.resolution import ResolutionIndex
from repoatlas.models import http
from repoatlas.parsers.http_facts import OAS3_APP, OPENAPI_OPERATIONS, TS_HANDLER_OBJECT
from repoatlas.storage.protocols import FileRow, UnresolvedRefRecord

PROMOTED = "promoted"
SPEC_UNREAD = "its specification path is not path.join(__dirname, <literals>)"
SPEC_NOT_FOUND = "no one OpenAPI 3 document naming operations is indexed at its path"
SERVERS_UNREAD = "a root server of its specification has variables or no readable path"
CONTROLLERS_UNREAD = "its controllers are not handler objects the source proves"
CONTROLLERS_UNRESOLVED = "a controllers object resolves to no single handler object"
NO_HANDLER = "no operation's handler name is a key of its controllers"

Key = tuple[int, str]
Resolve = Callable[[FileRow, Mapping[str, str]], UnresolvedRefRecord | None]


@dataclass(slots=True)
class SpecRoutedPlan:
    routes: list[PendingRoute] = field(default_factory=list)
    #: Application claim id → ``promoted`` or why not.
    outcomes: dict[int, str] = field(default_factory=dict)
    #: Handler object claim id → the route bodies it is served at.
    promoted: dict[int, tuple[str, ...]] = field(default_factory=dict)
    resolved: int = 0
    unresolved: int = 0


def _meta(claim: UnresolvedRefRecord) -> dict[str, str]:
    loaded = json.loads(claim.meta_json) if claim.meta_json else {}
    return {str(k): str(v) for k, v in loaded.items()}


def handler_name(operation_id: str, controller: str | None) -> str | None:
    """The controllers key oas3-tools calls for an operation (``getHandlerName``).

    ``removeDashElementToCamelCase``: the first dash-separated piece as written, each
    later one through lodash ``capitalize`` (first letter upper, the rest lower); then
    ``<controller>_`` before it when the operation names one. ``None`` outside ASCII,
    where case mapping is not this simple.
    """
    if not operation_id.isascii() or (controller is not None and not controller.isascii()):
        return None
    first, *rest = operation_id.split("-")
    name = first + "".join(piece[:1].upper() + piece[1:].lower() for piece in rest)
    return f"{controller}_{name}" if controller else name


def plan_spec_routed(
    grouped: Mapping[str, Sequence[UnresolvedRefRecord]],
    index: ResolutionIndex,
    by_id: Mapping[int, FileRow],
) -> SpecRoutedPlan:
    """Serve every operation a proven oas3-tools application routes. Writes nothing."""
    plan = SpecRoutedPlan()
    apps = grouped.get(OAS3_APP, ())
    if not apps:
        return plan
    objects: dict[Key, UnresolvedRefRecord] = {}
    exports: dict[Key, list[str]] = defaultdict(list)
    for claim in grouped.get(TS_HANDLER_OBJECT, ()):
        objects[(claim.file_id, claim.target_key)] = claim
        for name in json.loads(_meta(claim).get("exports", "[]")):
            exports[(claim.file_id, str(name))].append(claim.target_key)
    specs: dict[tuple[int, str], list[UnresolvedRefRecord]] = defaultdict(list)
    for claim in grouped.get(OPENAPI_OPERATIONS, ()):
        row = by_id.get(claim.file_id)
        if row is not None:
            specs[(row.repo_id, row.rel_path)].append(claim)

    def resolve(origin: FileRow, ref: Mapping[str, str]) -> UnresolvedRefRecord | None:
        if "local" in ref:
            return objects.get((origin.file_id, ref["local"]))
        module = ref.get("module", "")
        target = index.resolve_module(origin, module, "node") if module.startswith(".") else None
        found = exports.get((target.file_id, ref.get("name", "")), []) if target else []
        return objects.get((target.file_id, found[0])) if target and len(found) == 1 else None

    served: dict[int, dict[str, None]] = defaultdict(dict)
    for app in apps:
        origin = by_id.get(app.file_id)
        if origin is None:
            continue
        outcome = _serve(app, origin, specs, resolve, by_id, plan, served)
        if app.id is not None:
            plan.outcomes[app.id] = outcome
        if outcome == PROMOTED:
            plan.resolved += 1
        else:
            plan.unresolved += 1
    plan.promoted = {claim_id: tuple(bodies) for claim_id, bodies in served.items()}
    return plan


def _serve(
    app: UnresolvedRefRecord,
    origin: FileRow,
    specs: Mapping[tuple[int, str], list[UnresolvedRefRecord]],
    resolve: Resolve,
    by_id: Mapping[int, FileRow],
    plan: SpecRoutedPlan,
    served: dict[int, dict[str, None]],
) -> str:
    meta = _meta(app)
    spec = meta.get("spec")
    if spec is None:
        return SPEC_UNREAD
    path = posixpath.normpath(posixpath.join(posixpath.dirname(origin.rel_path), spec))
    found = specs.get((origin.repo_id, path), [])
    if path.startswith("../") or path == ".." or len(found) != 1:
        return SPEC_NOT_FOUND
    document = _meta(found[0])
    bases = json.loads(document.get("bases", "null"))
    if bases is None:
        return SERVERS_UNREAD
    refs = json.loads(meta.get("controllers", "null"))
    if refs is None:
        return CONTROLLERS_UNREAD
    handlers: dict[str, UnresolvedRefRecord] = {}
    for ref in refs:
        owner = resolve(origin, ref)
        if owner is None:
            return CONTROLLERS_UNRESOLVED
        for key in json.loads(_meta(owner).get("keys", "{}")):
            handlers[str(key)] = owner
    any_served = False
    for method, route, operation_id, controller, _line in json.loads(document["operations"]):
        name = handler_name(str(operation_id), controller)
        owner = handlers.get(name) if name is not None else None
        row = by_id.get(owner.file_id) if owner is not None else None
        if owner is None or row is None:
            continue
        for base in bases:
            path_served = http.route_path(f"{base}{route}")
            if path_served is None:
                continue
            body = http.route_body(str(method), path_served)
            plan.routes.append(
                PendingRoute(
                    src_id=owner.src_id,
                    body=body,
                    repo_id=row.repo_id,
                    file_id=row.file_id,
                    line_start=None,
                    line_end=None,
                )
            )
            if owner.id is not None:
                served[owner.id][body] = None
            any_served = True
    return PROMOTED if any_served else NO_HANDLER
