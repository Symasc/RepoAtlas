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

* the specification path -- the one the application's claim states, or the one each call of
  the function that holds it hands over (``oas3-factory-call``) -- names exactly one indexed
  OpenAPI 3 document in the application's own repository, whose root servers' base paths are
  read. A segment, or the whole path, another module exports is read in that module's own
  claims (``ts-constants``, ``ts-path-constants``), never anywhere else; a refusal says why,
  by a ``spec-...`` code (:mod:`repoatlas.parsers.ts_spec_paths`);
* every controllers object resolves to exactly one handler object: declared in the
  application's file, or exported under that name by the one module a relative import
  resolves to. A later object's key replaces an earlier one's, as a spread does;
* the operation's handler name -- :func:`handler_name`, the library's own -- is a key.

Each such operation is served by the handler object's ``const``, at each base path
joined to the operation's path. The route is assembled from the document and the
controllers, so it is composed: no span. An operation whose handler name is no key
is served by nothing here (the library answers 405); the document itself still
declares it, as it always did.

Every call of a function that holds an application is an application of its own, with the
document it hands over; the routes it serves are one set, and every route keeps the file of
the ``expressAppConfig`` call as its root, never a caller's. An application is promoted when
any of its instances is served. One that is not says why by its first instance's refusal --
the first call by file path, then line -- or, when no call reaches it, by
``spec-parameter-uncalled``.

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

from repoatlas.graph.impact import is_test_path
from repoatlas.graph.py_routes import PendingRoute
from repoatlas.graph.resolution import ResolutionIndex
from repoatlas.models import http
from repoatlas.parsers.http_facts import (
    OAS3_APP,
    OAS3_FACTORY_CALL,
    OPENAPI_OPERATIONS,
    TS_CONSTANTS,
    TS_HANDLER_OBJECT,
    TS_PATH_CONSTANTS,
)
from repoatlas.parsers.ts_spec_paths import (
    SPEC_ABSOLUTE,
    SPEC_CWD_RELATIVE,
    SPEC_DYNAMIC,
    SPEC_IMPORT_UNRESOLVED,
    SPEC_LAYOUT_AMBIGUOUS,
    SPEC_NOT_A_FILE,
    SPEC_NOT_INDEXED,
    SPEC_OUTSIDE_REPOSITORY,
    SPEC_PARAMETER_TEST_ONLY,
    SPEC_PARAMETER_UNCALLED,
    SPEC_PARAMETER_UNSUPPORTED,
    _absolute,
)
from repoatlas.storage.protocols import FileRow, UnresolvedRefRecord

PROMOTED = "promoted"
SPEC_UNREAD = "its specification path is not one the source anchors to its file"
SPEC_NOT_FOUND = "no one OpenAPI 3 document naming operations is indexed at its path"
SERVERS_UNREAD = "a root server of its specification has variables or no readable path"
CONTROLLERS_UNREAD = "its controllers are not handler objects the source proves"
CONTROLLERS_UNRESOLVED = "a controllers object resolves to no single handler object"
NO_HANDLER = "no operation's handler name is a key of its controllers"

#: The refusals that mean the path is read but names no document in the repository.
_NOT_FOUND = frozenset({SPEC_OUTSIDE_REPOSITORY, SPEC_NOT_INDEXED, SPEC_LAYOUT_AMBIGUOUS})

Key = tuple[int, str]
Resolve = Callable[[FileRow, Mapping[str, str]], UnresolvedRefRecord | None]
Calls = Callable[[UnresolvedRefRecord, Mapping[str, str]], list[UnresolvedRefRecord]]
#: The directories, besides its own, a file's ``__dirname`` is once built (tsconfig layouts).
Layouts = Callable[[FileRow], Sequence[str]]
#: One path read under each layout of its anchor, the source's first; or one refusal.
Reading = tuple["str | _Refusal", ...]


@dataclass(slots=True)
class SpecRoutedPlan:
    routes: list[PendingRoute] = field(default_factory=list)
    #: Application claim id → ``promoted`` or why not.
    outcomes: dict[int, str] = field(default_factory=dict)
    #: Application claim id → the ``spec-...`` code its specification path was refused
    #: with, for an application that is not promoted.
    spec_refusals: dict[int, str] = field(default_factory=dict)
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
    layouts: Layouts | None = None,
) -> SpecRoutedPlan:
    """Serve every operation a proven oas3-tools application routes. Writes nothing.

    ``layouts`` gives the build directories a file also runs from; without it, a file runs
    where it stands."""
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

    paths = _Paths(index, grouped, specs, layouts)

    #: ``(file id, local name)`` of a function → the calls of it in its own file; and file id
    #: of the module an imported call resolves to → those calls, with the name they import.
    local_calls: dict[Key, list[UnresolvedRefRecord]] = defaultdict(list)
    imported_calls: dict[int, list[tuple[UnresolvedRefRecord, str]]] = defaultdict(list)
    for call in grouped.get(OAS3_FACTORY_CALL, ()):
        caller = by_id.get(call.file_id)
        if caller is None:
            continue
        ref = _meta(call)
        if "local" in ref:
            local_calls[(call.file_id, ref["local"])].append(call)
            continue
        module = ref.get("module", "")
        target = index.resolve_module(caller, module, "node") if module.startswith(".") else None
        if target is not None and target.repo_id == caller.repo_id:
            imported_calls[target.file_id].append((call, ref.get("name", "")))

    def reaching(app: UnresolvedRefRecord, meta: Mapping[str, str]) -> list[UnresolvedRefRecord]:
        """The calls that hand ``app`` its specification, first by file path, then by line."""
        exported = {str(name) for name in json.loads(meta.get("factory_exports", "[]"))}
        found = [*local_calls.get((app.file_id, meta.get("factory", "")), [])]
        found.extend(call for call, name in imported_calls[app.file_id] if name in exported)
        return sorted(found, key=lambda call: (by_id[call.file_id].rel_path, call.line_start or 0))

    served: dict[int, dict[str, None]] = defaultdict(dict)
    for app in apps:
        origin = by_id.get(app.file_id)
        if origin is None:
            continue
        outcome, code = _serve(app, origin, paths, reaching, resolve, by_id, plan, served)
        if app.id is not None:
            plan.outcomes[app.id] = outcome
            if code is not None:
                plan.spec_refusals[app.id] = code
        if outcome == PROMOTED:
            plan.resolved += 1
        else:
            plan.unresolved += 1
    plan.promoted = {claim_id: tuple(bodies) for claim_id, bodies in served.items()}
    return plan


@dataclass(frozen=True, slots=True)
class _Refusal:
    """A path the source does not state, or states to no document: ``code`` says why."""

    code: str

    @property
    def headline(self) -> str:
        return SPEC_NOT_FOUND if self.code in _NOT_FOUND else SPEC_UNREAD


def _locate(directory: str, segments: Sequence[str]) -> str | _Refusal:
    """The repository path of ``segments`` joined to ``directory``, or why it names no file.

    Checked in this order: a segment that starts somewhere else (rooted, a backslash, a drive);
    then, once normalized, a path that ends in a directory, that leaves the repository.
    """
    if any(_absolute(segment) for segment in segments):
        return _Refusal(SPEC_ABSOLUTE)
    joined = posixpath.join(directory, *segments)
    path = posixpath.normpath(joined)
    if not segments or joined.rsplit("/", 1)[-1] in {"", ".", ".."} or path == ".":
        return _Refusal(SPEC_NOT_A_FILE)
    if path == ".." or path.startswith("../"):
        return _Refusal(SPEC_OUTSIDE_REPOSITORY)
    return path


class _Paths:
    """The document a path states, read from the claims of the modules it names."""

    def __init__(
        self,
        index: ResolutionIndex,
        grouped: Mapping[str, Sequence[UnresolvedRefRecord]],
        specs: Mapping[tuple[int, str], list[UnresolvedRefRecord]],
        layouts: Layouts | None = None,
    ) -> None:
        self._index = index
        self._specs = specs
        self._layouts = layouts
        #: File id → exported name → the string constant, and the path of a document.
        self._constants = self._table(grouped.get(TS_CONSTANTS, ()))
        self._offered = self._table(grouped.get(TS_PATH_CONSTANTS, ()))

    @staticmethod
    def _table(claims: Sequence[UnresolvedRefRecord]) -> dict[int, dict[str, str]]:
        found: dict[int, dict[str, str]] = defaultdict(dict)
        for claim in claims:
            table = json.loads(_meta(claim).get("constants", "{}"))
            values = {str(name): str(value[0]) for name, value in table.items()}
            found[claim.file_id].update(values)
        return found

    def anchored(self, row: FileRow, segments: Sequence[str]) -> Reading:
        """``segments`` joined to each directory ``row``'s ``__dirname`` may be, its own first."""
        directories = [posixpath.dirname(row.rel_path)]
        if self._layouts is not None:
            directories.extend(self._layouts(row))
        return tuple(_locate(directory, segments) for directory in dict.fromkeys(directories))

    def of_claim(self, origin: FileRow, meta: Mapping[str, str]) -> Reading:
        """The path an application's claim states, relative to its own file."""
        if "spec" in meta:
            return self.anchored(origin, [meta["spec"]])
        if "spec_parts" in meta:
            return self._parts(origin, json.loads(meta["spec_parts"]))
        if "spec_imported_path" in meta:
            names = json.loads(meta["spec_imported_path"])
            if isinstance(names, list) and len(names) == 1 and len(names[0]) == 3:
                return self._imported(origin, str(names[0][1]), str(names[0][2]))
            return (_Refusal(SPEC_DYNAMIC),)
        return (_Refusal(meta.get("spec_refused", SPEC_DYNAMIC)),)

    def of_argument(self, caller: FileRow, entry: Mapping[str, object]) -> Reading:
        """The path one argument of a call states, relative to the caller's file."""
        if "spec" in entry:
            return self.anchored(caller, [str(entry["spec"])])
        if "spec_parts" in entry:
            return self._parts(caller, entry["spec_parts"])
        if "refused" in entry:
            return (_Refusal(str(entry["refused"])),)
        return (_Refusal(SPEC_DYNAMIC),)

    def of_segment(
        self, factory: FileRow, parts: Sequence[str], entry: Mapping[str, object]
    ) -> Reading:
        """The string one argument hands a ``join`` in ``factory``, after its own ``parts``."""
        if "segment" in entry:
            return self.anchored(factory, [*parts, str(entry["segment"])])
        if "spec" in entry or "spec_parts" in entry:
            return (_Refusal(SPEC_ABSOLUTE),)  # an anchored path is absolute where it is joined
        if "refused" in entry:
            return (_Refusal(str(entry["refused"])),)
        return (_Refusal(SPEC_DYNAMIC),)

    def document(self, repo_id: int, reading: Reading) -> UnresolvedRefRecord | _Refusal:
        """The one OpenAPI document indexed at the paths ``reading`` names. Two layouts naming
        two documents name none; a reading naming none is ``spec-not-indexed`` when a layout
        reads a path in the repository, else its source layout's refusal."""
        found: dict[str, UnresolvedRefRecord] = {}
        for path in reading:
            if isinstance(path, str):
                documents = self._specs.get((repo_id, path), [])
                if len(documents) == 1:
                    found[path] = documents[0]
        if len(found) > 1:
            return _Refusal(SPEC_LAYOUT_AMBIGUOUS)
        if found:
            return next(iter(found.values()))
        if any(isinstance(path, str) for path in reading):
            return _Refusal(SPEC_NOT_INDEXED)
        first = reading[0]
        return first if isinstance(first, _Refusal) else _Refusal(SPEC_NOT_INDEXED)

    def _module(self, origin: FileRow, specifier: str) -> FileRow | None:
        if not specifier.startswith("."):
            return None
        return self._index.resolve_module(origin, specifier, "node")

    def _parts(self, origin: FileRow, parts: object) -> Reading:
        """The segments after the file's own directory, each ``["t", text]`` or the string
        constant ``["r", specifier, name]`` names in the module it resolves to."""
        if not isinstance(parts, list) or not parts or parts[0] != ["dir"]:
            return (_Refusal(SPEC_DYNAMIC),)
        segments: list[str] = []
        for part in parts[1:]:
            if isinstance(part, list) and len(part) == 2 and part[0] == "t":
                segments.append(str(part[1]))
            elif isinstance(part, list) and len(part) == 3 and part[0] == "r":
                target = self._module(origin, str(part[1]))
                known = self._constants.get(target.file_id, {}) if target else {}
                value = known.get(str(part[2]))
                if value is None:
                    return (_Refusal(SPEC_IMPORT_UNRESOLVED),)
                segments.append(value)
            else:
                return (_Refusal(SPEC_DYNAMIC),)
        return self.anchored(origin, segments)

    def _imported(self, origin: FileRow, specifier: str, name: str) -> Reading:
        """The whole path is ``name``. A module offers it as the path of a document, anchored at
        its own file; a plain string it offers is a path the process opens from its working
        directory, which no source states."""
        target = self._module(origin, specifier)
        if target is None:
            return (_Refusal(SPEC_IMPORT_UNRESOLVED),)
        offered = self._offered.get(target.file_id, {}).get(name)
        if offered is not None:
            return self.anchored(target, [offered])
        plain = self._constants.get(target.file_id, {}).get(name)
        if plain is None:
            return (_Refusal(SPEC_IMPORT_UNRESOLVED),)
        return (_Refusal(SPEC_ABSOLUTE if _absolute(plain) else SPEC_CWD_RELATIVE),)


def _readings(
    app: UnresolvedRefRecord,
    origin: FileRow,
    meta: Mapping[str, str],
    paths: _Paths,
    reaching: Calls,
    by_id: Mapping[int, FileRow],
) -> list[Reading]:
    """The path of each instance of an application: its own, or one for each call that reaches
    the function it is the parameter of. A call in a test file is an instance only of a factory
    that is itself in one: a harness does not say where the application finds its document."""
    if "spec_param" not in meta or "factory" not in meta:
        return [paths.of_claim(origin, meta)]
    position = int(meta["spec_param"]) if meta["spec_param"].isdigit() else -1
    calls = reaching(app, meta)
    if not calls:
        return [(_Refusal(SPEC_PARAMETER_UNCALLED),)]
    if not is_test_path(origin.rel_path):
        calls = [call for call in calls if not is_test_path(by_id[call.file_id].rel_path)]
        if not calls:
            return [(_Refusal(SPEC_PARAMETER_TEST_ONLY),)]
    parts = json.loads(meta["spec_param_dir"]) if "spec_param_dir" in meta else None
    readings: list[Reading] = []
    for call in calls:
        arguments = json.loads(_meta(call).get("arguments", "[]"))
        if not 0 <= position < len(arguments):
            readings.append((_Refusal(SPEC_PARAMETER_UNSUPPORTED),))
        elif parts is None:
            readings.append(paths.of_argument(by_id[call.file_id], arguments[position]))
        else:
            readings.append(paths.of_segment(origin, parts, arguments[position]))
    return readings


def _handlers(
    meta: Mapping[str, str], origin: FileRow, resolve: Resolve
) -> dict[str, UnresolvedRefRecord] | str:
    """The controllers key → the handler object it is a key of, or why they are unread."""
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
    return handlers


def _serve(
    app: UnresolvedRefRecord,
    origin: FileRow,
    paths: _Paths,
    reaching: Calls,
    resolve: Resolve,
    by_id: Mapping[int, FileRow],
    plan: SpecRoutedPlan,
    served: dict[int, dict[str, None]],
) -> tuple[str, str | None]:
    """The outcome of an application, and the ``spec-...`` code when its path was refused."""
    meta = _meta(app)
    handlers: dict[str, UnresolvedRefRecord] | str | None = None
    seen: set[tuple[int, str]] = set()
    first: tuple[str, str | None] | None = None
    any_served = False
    for reading in _readings(app, origin, meta, paths, reaching, by_id):
        found = paths.document(origin.repo_id, reading)
        if isinstance(found, _Refusal):
            failure: tuple[str, str | None] = (found.headline, found.code)
        elif (bases := json.loads(_meta(found).get("bases", "null"))) is None:
            failure = (SERVERS_UNREAD, None)
        else:
            if handlers is None:  # read once, and only for a document that reaches them
                handlers = _handlers(meta, origin, resolve)
            if isinstance(handlers, str):
                failure = (handlers, None)
            elif _emit(found, bases, handlers, app, by_id, plan, served, seen):
                any_served = True
                continue
            else:
                failure = (NO_HANDLER, None)
        first = first or failure
    if any_served:
        return PROMOTED, None
    return first if first is not None else (NO_HANDLER, None)


def _emit(
    found: UnresolvedRefRecord,
    bases: list[str],
    handlers: Mapping[str, UnresolvedRefRecord],
    app: UnresolvedRefRecord,
    by_id: Mapping[int, FileRow],
    plan: SpecRoutedPlan,
    served: dict[int, dict[str, None]],
    seen: set[tuple[int, str]],
) -> bool:
    """Serve the document's operations by the handlers; whether any operation was served."""
    document = _meta(found)
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
            any_served = True
            if (owner.src_id, body) in seen:  # another instance served it: one route
                continue
            seen.add((owner.src_id, body))
            plan.routes.append(
                PendingRoute(
                    src_id=owner.src_id,
                    body=body,
                    repo_id=row.repo_id,
                    file_id=row.file_id,
                    line_start=None,
                    line_end=None,
                    roots=frozenset({app.file_id}),
                    declared_at=owner.line_start,
                )
            )
            if owner.id is not None:
                served[owner.id][body] = None
    return any_served
