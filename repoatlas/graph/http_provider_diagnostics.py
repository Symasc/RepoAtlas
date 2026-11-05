"""Why a recognised route declaration is not a served route (R1, gate G2).

The planners (:mod:`~repoatlas.graph.py_routes`, :mod:`~repoatlas.graph.ts_routes`,
:mod:`~repoatlas.graph.ts_oas3`, the NestJS stage of :mod:`~repoatlas.graph.http_claims`) decide
which route claims are served and say why the others are not, in words of their own. This module
only *classifies* what they said: for every route claim that is not served it gives the stages
the declaration reached (path, identity, registration), the category and stable code of the one
that failed, the bounds on its evidence path, and the real places that caused it. It decides
nothing: it reads a finished :class:`~repoatlas.graph.http_claims.HttpClaimPlan` and the claims
it was made from, and returns facts for a reader.

Pure by construction: no I/O, no storage, no capability. The global link pass never imports it
(``tests/test_r1_classification.py`` scans for that), so nothing here can reach a ``SERVES``
edge, a join decision or a declaration rule.

**Precedence** of the code a route gets: (1) ``plan.chain_refusals`` (a bound stopped the
chain: ``bound-reached``); (2) ``plan.route_codes`` (a cause the planner itself named); (3) the
planner's outcome text, through the table below; (4) the generic code of the role. Anything not
in the table is ``unclassified`` with every stage ``not-evaluated``; nothing is silently mapped
to a precise category.

**Stages.** ``L`` in the table is ``resolved`` when every part of the claim's path is a literal,
else ``not-evaluated``.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from repoatlas.graph.http_claims import _MAX_MODULES, HttpClaimPlan
from repoatlas.graph.py_routes import (
    CLASS_NO_ROUTE,
    CLASS_NOT_AN_APPLICATION,
    CLASS_NOT_OFFERED,
    CLASS_TOO_DEEP,
    CODE_CHAIN_TOO_DEEP,
    CODE_CONSTANT_HOPS,
    CODE_ROUTER_PATH_UNROOTED,
    CONSTANT_UNRESOLVED,
    MAX_CONSTANT_HOPS,
    MAX_DEPTH,
    MODULE_UNRESOLVED,
    NOT_DECLARED,
    NOT_MOUNTED,
    PROMOTED,
    SERVED_HERE,
    UNREADABLE_MOUNT,
    WRONG_KIND,
    Place,
)
from repoatlas.graph.ts_oas3 import (
    CONTROLLERS_UNREAD,
    CONTROLLERS_UNRESOLVED,
    NO_HANDLER,
    SERVERS_UNREAD,
    SPEC_NOT_FOUND,
    SPEC_UNREAD,
)
from repoatlas.graph.ts_routes import (
    BOUND_CONSTANTS_PER_MODULE,
    CHAIN_TOO_COMPLEX,
    CHAIN_TOO_DEEP,
    FACTORY_NOT_INSTANTIATED,
    FACTORY_UNRESOLVED,
    HELPER_ARGUMENT_POSITION,
    HELPER_NOT_A_PLUGIN,
    HELPER_UNRESOLVED,
    PLUGIN_PREFIX_IGNORED,
    REGISTRATION_UNREADABLE,
)
from repoatlas.graph.ts_routes import CONSTANT_UNRESOLVED as FASTIFY_CONSTANT_UNRESOLVED
from repoatlas.graph.ts_routes import NOT_DECLARED as FASTIFY_NOT_DECLARED
from repoatlas.graph.ts_routes import NOT_REGISTERED as FASTIFY_NOT_REGISTERED
from repoatlas.graph.ts_routes import UNREADABLE_REGISTRATION as FASTIFY_UNREADABLE_REGISTRATION
from repoatlas.models import http
from repoatlas.parsers import ts_spec_paths
from repoatlas.parsers.http_facts import (
    FASTIFY_HELPER_CALL,
    FASTIFY_INSTANCE,
    FASTIFY_ROUTE,
    FASTIFY_TRACED_ROUTE,
    NESTJS_ROOT,
    NESTJS_ROUTE,
    OAS3_APP,
    PENDING_FACT,
    PYTHON_CLASS_ROUTE,
    PYTHON_ROUTE,
    TS_HANDLER_OBJECT,
)
from repoatlas.parsers.http_fastify import (
    _MAX_CONSTANTS,
    _MAX_HELPER_CALLS,
    _MAX_INSTANCES,
    _MAX_TRACED_ROUTES,
    AFTER_LISTEN,
    CALLEE_BEFORE_BINDING,
    ENTRY_NOT_CALLED,
    FACTORY_NOT_AWAITED,
    FACTORY_UNPROVEN,
    HELPER_ARGUMENT_UNSUPPORTED,
    HELPER_CALLEE_UNSUPPORTED,
    HELPER_CONTROL_FLOW,
    HELPER_DYNAMIC,
    HELPER_SHADOWED,
    RECEIVER_BEFORE_BINDING,
)
from repoatlas.parsers.http_python import _MAX_CLASS_DEPTH
from repoatlas.storage.protocols import FileRow, UnresolvedRefRecord

#: Bound notes one answer carries at most; the rest are counted in ``bound_notes_omitted``.
MAX_BOUND_NOTES = 64
#: Bounds one row lists at most, and the places that caused it.
MAX_ROW_BOUNDS = 4
MAX_RELATED = 3

# -- the vocabulary ---------------------------------------------------------------------------

REGISTRATION_UNPROVEN = "registration-unproven"
PATH_UNRESOLVED = "path-unresolved"
IDENTITY_UNRESOLVED = "identity-unresolved"
FRAMEWORK_CONSTRAINT = "framework-constraint"
BOUND_REACHED = "bound-reached"
UNCLASSIFIED = "unclassified"
CATEGORIES = (
    REGISTRATION_UNPROVEN,
    PATH_UNRESOLVED,
    IDENTITY_UNRESOLVED,
    FRAMEWORK_CONSTRAINT,
    BOUND_REACHED,
    UNCLASSIFIED,
)

RESOLVED = "resolved"
UNRESOLVED = "unresolved"
NOT_EVALUATED = "not-evaluated"
PROVEN = "proven"
UNPROVEN = "unproven"
#: In the table: ``resolved`` when every part of the claim's path is a literal, else
#: ``not-evaluated``.
_L = "L"

#: The bounds a file-level note reports, by the role whose claims the parser cut at a cap.
BOUND_HELPER_CALLS = "fastify-helper-calls-per-file"
BOUND_TRACED_ROUTES = "fastify-traced-routes-per-file"
BOUND_INSTANCES = "fastify-instances-per-file"
_FILE_CAPS = (
    (FASTIFY_HELPER_CALL, _MAX_HELPER_CALLS, BOUND_HELPER_CALLS),
    (FASTIFY_TRACED_ROUTE, _MAX_TRACED_ROUTES, BOUND_TRACED_ROUTES),
    (FASTIFY_INSTANCE, _MAX_INSTANCES, BOUND_INSTANCES),
)
#: The caps whose being reached anywhere in a repository may explain a Fastify route nothing
#: registers: a helper call or an instance the parser dropped is a registration it never saw.
_REPOSITORY_CAPS = (
    (FASTIFY_HELPER_CALL, _MAX_HELPER_CALLS, BOUND_HELPER_CALLS),
    (FASTIFY_INSTANCE, _MAX_INSTANCES, BOUND_INSTANCES),
)

BOUND_PYTHON_MOUNT_DEPTH = "python-mount-depth"
BOUND_PYTHON_CLASS_DEPTH = "python-class-depth"
BOUND_NESTJS_MODULES = "nestjs-modules"
BOUND_NESTJS_DYNAMIC_ARGUMENTS = "nestjs-dynamic-arguments"
#: The bounds a route reaches on its own evidence path (not a file's cap), with their limits.
_ROUTE_BOUND_LIMITS: Mapping[str, int | None] = {
    BOUND_PYTHON_MOUNT_DEPTH: MAX_DEPTH,
    CODE_CONSTANT_HOPS: MAX_CONSTANT_HOPS,
    BOUND_PYTHON_CLASS_DEPTH: _MAX_CLASS_DEPTH,
    BOUND_CONSTANTS_PER_MODULE: _MAX_CONSTANTS,
    BOUND_NESTJS_MODULES: _MAX_MODULES,
    BOUND_NESTJS_DYNAMIC_ARGUMENTS: None,
    ts_spec_paths.SPEC_DEPTH_EXCEEDED: ts_spec_paths._MAX_DEPTH,
    ts_spec_paths.SPEC_PROPAGATION_DEPTH: None,
}

#: The roles of the claims this module diagnoses, and the framework each belongs to.
_FRAMEWORKS = {
    NESTJS_ROUTE: "nestjs",
    FASTIFY_ROUTE: "fastify",
    FASTIFY_TRACED_ROUTE: "fastify",
    OAS3_APP: "oas3-tools",
}
_FAMILIES = {"fastapi": "fastapi", "flask": "flask", "either": "fastapi-or-flask"}
_COVERED = frozenset(
    {NESTJS_ROUTE, PYTHON_ROUTE, PYTHON_CLASS_ROUTE, FASTIFY_ROUTE, FASTIFY_TRACED_ROUTE, OAS3_APP}
)

# -- the answer -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Classification:
    """The stages a declaration reached, and the category and code of the one that failed."""

    category: str
    code: str
    path: str
    identity: str
    registration: str
    bounds: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DiagnosticPlace:
    """A real claim location that caused a refusal, as a reader can open it."""

    path: str
    line_start: int | None
    line_end: int | None
    role: str


@dataclass(frozen=True, slots=True)
class BoundNote:
    """An analysis bound that was reached: where, and the limit."""

    bound: str
    limit: int | None
    #: ``file`` (a parser cap hit in one file) or ``route`` (a bound on one route's evidence).
    scope: str
    path: str | None


@dataclass(frozen=True, slots=True)
class RouteDiagnostic:
    """One route claim that is not served, or one oas3-tools application that serves nothing."""

    claim_id: int
    #: ``route``, or ``application`` for an oas3-tools application.
    subject: str
    #: The claim's pending-fact role (``python-route``, ``fastify-traced-route`` ...).
    fact: str
    #: ``fastapi``, ``flask``, ``fastapi-or-flask``, ``nestjs``, ``fastify`` or ``oas3-tools``.
    framework: str | None
    #: The repository-relative path of the file the claim is in, and its lines.
    file: str
    line_start: int | None
    line_end: int | None
    #: The handler's entity id: the caller of a graph read turns it into a key.
    src_id: int
    method: str | None
    #: The relative route body (``GET /audit``) when the declaration states or evaluates one,
    #: else ``""``.
    route: str
    #: The planner's own outcome, verbatim.
    outcome: str
    path: str
    identity: str
    registration: str
    category: str
    code: str
    bounds: tuple[str, ...]
    related: tuple[DiagnosticPlace, ...]
    related_count: int


@dataclass(frozen=True, slots=True)
class ProviderDiagnostics:
    """Everything :func:`diagnose` knows about one repository's unserved route declarations."""

    #: In a fixed order: by file, line, claim id.
    entries: tuple[RouteDiagnostic, ...]
    #: At most :data:`MAX_BOUND_NOTES`, sorted by (bound, path).
    bound_notes: tuple[BoundNote, ...]
    bound_notes_omitted: int

    def by_claim(self) -> dict[int, RouteDiagnostic]:
        return {entry.claim_id: entry for entry in self.entries}


# -- the classification table -----------------------------------------------------------------


def _row(
    category: str, path: str, identity: str, registration: str, *bounds: str
) -> tuple[str, str, str, str, tuple[str, ...]]:
    return category, path, identity, registration, bounds


_NE = NOT_EVALUATED
#: code → (category, path, identity, registration, bounds). R1_DESIGN.md section 2.4, with
#: Amendment A1 of R1_PREDICTIONS.md (a wrapper at the call with a prefix reads as unreadable).
_CODES: Mapping[str, tuple[str, str, str, str, tuple[str, ...]]] = {
    # -- Python routes and class routes -----------------------------------------------------
    "python-module-unresolved": _row(IDENTITY_UNRESOLVED, _L, UNRESOLVED, _NE),
    "python-receiver-not-declared": _row(IDENTITY_UNRESOLVED, _L, UNRESOLVED, _NE),
    "python-receiver-kind-mismatch": _row(FRAMEWORK_CONSTRAINT, _L, RESOLVED, _NE),
    "python-path-constant-unresolved": _row(PATH_UNRESOLVED, UNRESOLVED, RESOLVED, _NE),
    CODE_CONSTANT_HOPS: _row(BOUND_REACHED, UNRESOLVED, RESOLVED, _NE, CODE_CONSTANT_HOPS),
    "python-mount-unreadable": _row(REGISTRATION_UNPROVEN, RESOLVED, RESOLVED, UNPROVEN),
    CODE_ROUTER_PATH_UNROOTED: _row(FRAMEWORK_CONSTRAINT, RESOLVED, RESOLVED, _NE),
    CODE_CHAIN_TOO_DEEP: _row(
        BOUND_REACHED, RESOLVED, RESOLVED, UNPROVEN, BOUND_PYTHON_MOUNT_DEPTH
    ),
    "python-not-mounted": _row(REGISTRATION_UNPROVEN, RESOLVED, RESOLVED, UNPROVEN),
    "python-class-module-unresolved": _row(IDENTITY_UNRESOLVED, _L, UNRESOLVED, _NE),
    "python-class-not-offered": _row(IDENTITY_UNRESOLVED, _L, UNRESOLVED, _NE),
    "python-class-not-application": _row(IDENTITY_UNRESOLVED, _L, UNRESOLVED, _NE),
    "python-class-too-deep": _row(BOUND_REACHED, _L, UNRESOLVED, _NE, BOUND_PYTHON_CLASS_DEPTH),
    "python-class-route-unreadable": _row(PATH_UNRESOLVED, UNRESOLVED, RESOLVED, _NE),
    # -- Fastify routes ---------------------------------------------------------------------
    "fastify-scope-not-declared": _row(IDENTITY_UNRESOLVED, _L, UNRESOLVED, _NE),
    FACTORY_UNRESOLVED: _row(IDENTITY_UNRESOLVED, _L, UNRESOLVED, _NE),
    FACTORY_UNPROVEN: _row(IDENTITY_UNRESOLVED, _L, UNRESOLVED, _NE),
    RECEIVER_BEFORE_BINDING: _row(IDENTITY_UNRESOLVED, _L, UNRESOLVED, _NE),
    CALLEE_BEFORE_BINDING: _row(IDENTITY_UNRESOLVED, _L, UNRESOLVED, _NE),
    FACTORY_NOT_AWAITED: _row(FRAMEWORK_CONSTRAINT, _L, UNRESOLVED, _NE),
    AFTER_LISTEN: _row(FRAMEWORK_CONSTRAINT, _L, RESOLVED, UNPROVEN),
    PLUGIN_PREFIX_IGNORED: _row(FRAMEWORK_CONSTRAINT, RESOLVED, RESOLVED, UNPROVEN),
    REGISTRATION_UNREADABLE: _row(REGISTRATION_UNPROVEN, RESOLVED, RESOLVED, UNPROVEN),
    HELPER_CONTROL_FLOW: _row(REGISTRATION_UNPROVEN, RESOLVED, RESOLVED, UNPROVEN),
    HELPER_ARGUMENT_UNSUPPORTED: _row(REGISTRATION_UNPROVEN, RESOLVED, RESOLVED, UNPROVEN),
    HELPER_ARGUMENT_POSITION: _row(REGISTRATION_UNPROVEN, RESOLVED, RESOLVED, UNPROVEN),
    ENTRY_NOT_CALLED: _row(REGISTRATION_UNPROVEN, RESOLVED, RESOLVED, UNPROVEN),
    FACTORY_NOT_INSTANTIATED: _row(REGISTRATION_UNPROVEN, RESOLVED, RESOLVED, UNPROVEN),
    # a helper call's own refusals that are never a route's cause (a callee that is not that
    # function, or names none): classified so that none is silently unclassified
    HELPER_DYNAMIC: _row(REGISTRATION_UNPROVEN, RESOLVED, RESOLVED, UNPROVEN),
    HELPER_CALLEE_UNSUPPORTED: _row(REGISTRATION_UNPROVEN, RESOLVED, RESOLVED, UNPROVEN),
    HELPER_SHADOWED: _row(REGISTRATION_UNPROVEN, RESOLVED, RESOLVED, UNPROVEN),
    HELPER_UNRESOLVED: _row(REGISTRATION_UNPROVEN, RESOLVED, RESOLVED, UNPROVEN),
    HELPER_NOT_A_PLUGIN: _row(REGISTRATION_UNPROVEN, RESOLVED, RESOLVED, UNPROVEN),
    "fastify-path-constant-unresolved": _row(PATH_UNRESOLVED, UNRESOLVED, RESOLVED, _NE),
    CHAIN_TOO_DEEP: _row(BOUND_REACHED, RESOLVED, RESOLVED, UNPROVEN),
    CHAIN_TOO_COMPLEX: _row(BOUND_REACHED, RESOLVED, RESOLVED, UNPROVEN),
    "fastify-not-registered": _row(REGISTRATION_UNPROVEN, RESOLVED, RESOLVED, UNPROVEN),
    # -- NestJS (reason text → code, below) -------------------------------------------------
    "nestjs-controller-not-indexed": _row(IDENTITY_UNRESOLVED, RESOLVED, UNRESOLVED, _NE),
    "nestjs-versionless-route": _row(FRAMEWORK_CONSTRAINT, UNRESOLVED, RESOLVED, PROVEN),
    "nestjs-no-application": _row(REGISTRATION_UNPROVEN, RESOLVED, RESOLVED, UNPROVEN),
    "nestjs-not-registered": _row(REGISTRATION_UNPROVEN, RESOLVED, RESOLVED, UNPROVEN),
    "nestjs-prefix-unproven": _row(PATH_UNRESOLVED, UNRESOLVED, RESOLVED, UNPROVEN),
    "nestjs-prefix-configurable": _row(PATH_UNRESOLVED, UNRESOLVED, RESOLVED, UNPROVEN),
    "nestjs-router-module": _row(FRAMEWORK_CONSTRAINT, RESOLVED, RESOLVED, UNPROVEN),
    "nestjs-module-unreadable": _row(REGISTRATION_UNPROVEN, RESOLVED, RESOLVED, UNPROVEN),
    "nestjs-module-not-indexed": _row(REGISTRATION_UNPROVEN, RESOLVED, RESOLVED, UNPROVEN),
    "nestjs-import-unresolved": _row(REGISTRATION_UNPROVEN, RESOLVED, RESOLVED, UNPROVEN),
    "nestjs-import-unreadable": _row(REGISTRATION_UNPROVEN, RESOLVED, RESOLVED, UNPROVEN),
    "nestjs-modules-bound": _row(BOUND_REACHED, RESOLVED, RESOLVED, UNPROVEN, BOUND_NESTJS_MODULES),
    "nestjs-dynamic-module": _row(FRAMEWORK_CONSTRAINT, RESOLVED, RESOLVED, UNPROVEN),
    "nestjs-dynamic-module-unresolved": _row(REGISTRATION_UNPROVEN, RESOLVED, RESOLVED, UNPROVEN),
    "nestjs-dynamic-arguments-bound": _row(
        BOUND_REACHED, RESOLVED, RESOLVED, UNPROVEN, BOUND_NESTJS_DYNAMIC_ARGUMENTS
    ),
    # -- oas3-tools applications ------------------------------------------------------------
    "oas3-servers-unread": _row(PATH_UNRESOLVED, UNRESOLVED, _NE, _NE),
    "oas3-controllers-unread": _row(IDENTITY_UNRESOLVED, RESOLVED, UNRESOLVED, _NE),
    "oas3-controllers-unresolved": _row(IDENTITY_UNRESOLVED, RESOLVED, UNRESOLVED, _NE),
    "oas3-no-handler": _row(IDENTITY_UNRESOLVED, RESOLVED, UNRESOLVED, _NE),
}
#: The ``spec-...`` codes a specification path is refused with: path-unresolved, except the two
#: bounds of the path reader.
_SPEC_BOUNDS = frozenset({ts_spec_paths.SPEC_DEPTH_EXCEEDED, ts_spec_paths.SPEC_PROPAGATION_DEPTH})
SPEC_CODES = tuple(
    sorted(
        value
        for name, value in vars(ts_spec_paths).items()
        if name.startswith("SPEC_") and isinstance(value, str)
    )
)

#: Planner outcome text → code, by the role of the claim. A text that is not here is
#: ``unclassified``.
_PYTHON_OUTCOMES = {
    MODULE_UNRESOLVED: "python-module-unresolved",
    NOT_DECLARED: "python-receiver-not-declared",
    WRONG_KIND: "python-receiver-kind-mismatch",
    CONSTANT_UNRESOLVED: "python-path-constant-unresolved",
    UNREADABLE_MOUNT: "python-mount-unreadable",
    NOT_MOUNTED: "python-not-mounted",
}
_PYTHON_CLASS_OUTCOMES = {
    MODULE_UNRESOLVED: "python-class-module-unresolved",
    CLASS_NOT_OFFERED: "python-class-not-offered",
    CLASS_NOT_AN_APPLICATION: "python-class-not-application",
    CLASS_TOO_DEEP: "python-class-too-deep",
    CLASS_NO_ROUTE: "python-class-route-unreadable",
    WRONG_KIND: "python-receiver-kind-mismatch",
    CONSTANT_UNRESOLVED: "python-path-constant-unresolved",
}
_FASTIFY_OUTCOMES = {
    FASTIFY_NOT_DECLARED: "fastify-scope-not-declared",
    FASTIFY_CONSTANT_UNRESOLVED: "fastify-path-constant-unresolved",
    FASTIFY_UNREADABLE_REGISTRATION: REGISTRATION_UNREADABLE,
    FASTIFY_NOT_REGISTERED: "fastify-not-registered",
}
_OAS3_OUTCOMES = {
    SERVERS_UNREAD: "oas3-servers-unread",
    CONTROLLERS_UNREAD: "oas3-controllers-unread",
    CONTROLLERS_UNRESOLVED: "oas3-controllers-unresolved",
    NO_HANDLER: "oas3-no-handler",
}
#: The outcomes of an oas3-tools application whose code is the ``spec-...`` refusal itself.
_OAS3_SPEC_OUTCOMES = frozenset({SPEC_UNREAD, SPEC_NOT_FOUND})

#: NestJS reasons: the generic text, and the others by exact text, then by pattern.
NESTJS_GENERIC = "not registered by a proven application"
_NESTJS_EXACT = {
    "controller not indexed": "nestjs-controller-not-indexed",
    "URI versioning names no version for it": "nestjs-versionless-route",
    "a file in this repository can change any application's prefix": "nestjs-prefix-configurable",
    "RouterModule is imported in this package": "nestjs-router-module",
    "RouterModule is registered": "nestjs-router-module",
    "a registered module's metadata cannot be read": "nestjs-module-unreadable",
    "a registered module is not indexed": "nestjs-module-not-indexed",
    "too many modules": "nestjs-modules-bound",
    "its module is handed to a dynamic module that may re-prefix it": "nestjs-dynamic-module",
}
_NESTJS_PATTERNS = (
    (re.compile(r"^import (['\"]).*\1 does not resolve$", re.DOTALL), "nestjs-import-unresolved"),
    (re.compile(r"^import (['\"]).*\1 cannot be read$", re.DOTALL), "nestjs-import-unreadable"),
    (
        re.compile(r"^dynamic module .* (has no known source|does not resolve)$", re.DOTALL),
        "nestjs-dynamic-module-unresolved",
    ),
    (
        re.compile(r"^dynamic module .* is handed too much to read$", re.DOTALL),
        "nestjs-dynamic-arguments-bound",
    ),
)
_NESTJS_PREFIX = "prefix not proven: "
#: What a NestJS *application* (a root) says of itself. A root outcome is never a route's reason
#: (the routes of a root that is not walked carry the generic text), so these read as the
#: generic registration failure.
_NESTJS_ROOT_OUTCOMES = frozenset(
    {
        "bootstrap file not indexed",
        "root module registered dynamically",
        "root module not resolved to an indexed module",
    }
)


def _unclassified() -> Classification:
    return Classification(UNCLASSIFIED, UNCLASSIFIED, _NE, _NE, _NE)


def classify_code(code: str, *, literal: bool = True) -> Classification:
    """The classification of a stable code, or ``unclassified`` when the table does not know it.

    ``literal``: every part of the claim's path is a literal (the ``L`` of the table)."""
    row = _CODES.get(code)
    if row is None:
        return classify_spec_code(code) if code in SPEC_CODES else _unclassified()
    category, path, identity, registration, bounds = row
    return Classification(
        category,
        code,
        (RESOLVED if literal else NOT_EVALUATED) if path == _L else path,
        identity,
        registration,
        bounds,
    )


def classify_spec_code(code: str) -> Classification:
    """A ``spec-...`` code an oas3-tools application's specification path was refused with."""
    if code not in SPEC_CODES:
        return _unclassified()
    if code in _SPEC_BOUNDS:
        return Classification(BOUND_REACHED, code, UNRESOLVED, _NE, _NE, (code,))
    return Classification(PATH_UNRESOLVED, code, UNRESOLVED, _NE, _NE)


def _nestjs_code(reason: str, has_root: bool) -> str | None:
    generic = "nestjs-not-registered" if has_root else "nestjs-no-application"
    if reason == NESTJS_GENERIC or reason in _NESTJS_ROOT_OUTCOMES:
        return generic
    if reason in _NESTJS_EXACT:
        return _NESTJS_EXACT[reason]
    if reason.startswith(_NESTJS_PREFIX):
        return "nestjs-prefix-unproven"
    for pattern, code in _NESTJS_PATTERNS:
        if pattern.match(reason):
            return code
    return None


def classify_outcome(
    role: str,
    outcome: str,
    *,
    literal: bool = True,
    has_nestjs_root: bool = True,
    spec_code: str | None = None,
) -> Classification:
    """The classification of a planner's outcome text for a claim of ``role``.

    ``unclassified`` for a role or a text the table does not know: the only fallback. The
    outcome ``promoted`` (and a Python route served by its own module) is no refusal and is not
    in the table. ``spec_code`` is the ``spec-...`` refusal of an oas3-tools application whose
    outcome says only that its specification was not read."""
    code: str | None
    if role == PYTHON_ROUTE:
        code = _PYTHON_OUTCOMES.get(outcome)
    elif role == PYTHON_CLASS_ROUTE:
        code = _PYTHON_CLASS_OUTCOMES.get(outcome)
    elif role in (FASTIFY_ROUTE, FASTIFY_TRACED_ROUTE):
        code = _FASTIFY_OUTCOMES.get(outcome)
    elif role == NESTJS_ROUTE:
        code = _nestjs_code(outcome, has_nestjs_root)
    elif role == OAS3_APP:
        if outcome in _OAS3_SPEC_OUTCOMES:
            return classify_spec_code(spec_code) if spec_code else _unclassified()
        code = _OAS3_OUTCOMES.get(outcome)
    else:
        code = None
    return classify_code(code, literal=literal) if code is not None else _unclassified()


def join_limits(
    *, eligible: bool, line_start: int | None, declared_at: int | None, structured: bool
) -> tuple[str, ...]:
    """What keeps a served row from being joined by an operator's declaration, though it is
    served: it states no line (a composed route: the deployment join cannot cite one), or it is a
    specification document, matched in its own repository only."""
    limits: list[str] = []
    if eligible and line_start is None and declared_at is None:
        limits.append("no-declaration-line")
    if structured:
        limits.append("openapi-document")
    return tuple(limits)


# -- the diagnosis ----------------------------------------------------------------------------


def _meta(claim: UnresolvedRefRecord) -> dict[str, str]:
    loaded = json.loads(claim.meta_json) if claim.meta_json else {}
    return {str(k): str(v) for k, v in loaded.items()}


def _is_literal(meta: Mapping[str, str]) -> bool:
    parts = json.loads(meta.get("path", "[]"))
    return all(part[0] == "t" for part in parts)


def _literal_body(role: str, meta: Mapping[str, str]) -> str:
    """The relative route body a claim states when its whole path is literal, else ``""``."""
    if role not in (PYTHON_ROUTE, PYTHON_CLASS_ROUTE, FASTIFY_ROUTE, FASTIFY_TRACED_ROUTE):
        return ""
    parts = json.loads(meta.get("path", "[]"))
    if not parts or any(part[0] != "t" for part in parts):
        return ""
    text = "".join(part[1] for part in parts)
    route = http.route_path(text or "/") if role.startswith("python") else http.route_path(text)
    body = http.route_body(meta.get("method", ""), route) if route is not None else ""
    return body if body and http.canonical_route(body) is not None else ""


def diagnose(
    plan: HttpClaimPlan, claims: Sequence[UnresolvedRefRecord], files: Sequence[FileRow]
) -> ProviderDiagnostics:
    """Classify every route claim of ``claims`` that ``plan`` does not serve.

    Covered: the non-promoted claims of the roles NestJS route, Python route (except one its own
    module serves), Python class route, Fastify route and traced route, and the non-promoted
    oas3-tools applications. ``claims`` are the repository's claims the plan was made from;
    ``files`` its files. Writes nothing and reads nothing else."""
    by_file = {row.file_id: row for row in files}
    roles: list[tuple[UnresolvedRefRecord, str]] = []
    for claim in claims:
        role = _meta(claim).get(PENDING_FACT, "") if claim.meta_json else ""
        roles.append((claim, role))
    has_root = any(role == NESTJS_ROOT for _, role in roles)

    # The caps the parser cut a file at: a count of claims at the cap means it stopped reading.
    counts: dict[str, Counter[int]] = {role: Counter() for role, _, _ in _FILE_CAPS}
    for claim, role in roles:
        if role in counts:
            counts[role][claim.file_id] += 1
    notes: dict[tuple[str, str], BoundNote] = {}
    for role, cap, bound in _FILE_CAPS:
        for file_id, count in counts[role].items():
            row = by_file.get(file_id)
            if count >= cap and row is not None:
                notes.setdefault((bound, row.rel_path), BoundNote(bound, cap, "file", row.rel_path))
    capped = sorted(
        bound
        for role, cap, bound in _REPOSITORY_CAPS
        if any(count >= cap for count in counts[role].values())
    )

    entries: list[RouteDiagnostic] = []
    for claim, role in roles:
        if role not in _COVERED or claim.id is None:
            continue
        row = by_file.get(claim.file_id)
        outcome = _outcome(plan, claim.id, role)
        if row is None or outcome is None:
            continue
        meta = _meta(claim)
        literal = role == NESTJS_ROUTE or _is_literal(meta)
        classified = _classify(plan, claim.id, role, outcome, literal, has_root)
        bounds = set(classified.bounds)
        category = classified.category
        if role in (FASTIFY_ROUTE, FASTIFY_TRACED_ROUTE):
            bounds.update(plan.route_bounds.get(claim.id, ()))
            if capped and outcome in (FASTIFY_NOT_REGISTERED, FASTIFY_UNREADABLE_REGISTRATION):
                bounds.update(capped)
                if classified.code == "fastify-not-registered":
                    category = BOUND_REACHED
        listed = tuple(sorted(bounds))[:MAX_ROW_BOUNDS]
        for bound in listed:
            if bound in _ROUTE_BOUND_LIMITS:
                notes.setdefault(
                    (bound, row.rel_path),
                    BoundNote(bound, _ROUTE_BOUND_LIMITS[bound], "route", row.rel_path),
                )
        entries.append(
            RouteDiagnostic(
                claim_id=claim.id,
                subject="application" if role == OAS3_APP else "route",
                fact=role,
                framework=_framework(role, meta),
                file=row.rel_path,
                line_start=claim.line_start,
                line_end=claim.line_end,
                src_id=claim.src_id,
                method=None if role == OAS3_APP else (meta.get("method") or None),
                route=_route(plan, claim.id, role, meta),
                outcome=outcome,
                path=classified.path,
                identity=classified.identity,
                registration=classified.registration,
                category=category,
                code=classified.code,
                bounds=listed,
                related=_places(plan.related.get(claim.id, ()), by_file)[:MAX_RELATED],
                related_count=len(plan.related.get(claim.id, ())),
            )
        )
    entries.sort(key=lambda entry: (entry.file, entry.line_start or 0, entry.claim_id))
    ordered = sorted(notes.values(), key=lambda note: (note.bound, note.path or ""))
    return ProviderDiagnostics(
        entries=tuple(entries),
        bound_notes=tuple(ordered[:MAX_BOUND_NOTES]),
        bound_notes_omitted=max(0, len(ordered) - MAX_BOUND_NOTES),
    )


def _outcome(plan: HttpClaimPlan, claim_id: int, role: str) -> str | None:
    """The planner's reason a claim is not served, or ``None`` when it is served (or not ours)."""
    if role == PYTHON_CLASS_ROUTE:
        return plan.class_unproven.get(claim_id)
    if role == FASTIFY_TRACED_ROUTE:
        return plan.traced_outcomes.get(claim_id)
    outcome = plan.routes.get(claim_id)
    if outcome is None or outcome == PROMOTED:
        return None
    if role == PYTHON_ROUTE and outcome == SERVED_HERE:
        return None
    return outcome


def _classify(
    plan: HttpClaimPlan, claim_id: int, role: str, outcome: str, literal: bool, has_root: bool
) -> Classification:
    """Chain refusal, then the planner's own code, then the outcome text, then the generic."""
    if role in (FASTIFY_ROUTE, FASTIFY_TRACED_ROUTE):
        code = plan.chain_refusals.get(claim_id) or plan.route_codes.get(claim_id)
        if code:
            return classify_code(code, literal=literal)
    elif role in (PYTHON_ROUTE, PYTHON_CLASS_ROUTE):
        code = plan.route_codes.get(claim_id)
        if code:
            return classify_code(code, literal=literal)
    return classify_outcome(
        role,
        outcome,
        literal=literal,
        has_nestjs_root=has_root,
        spec_code=plan.spec_refusals.get(claim_id),
    )


def _framework(role: str, meta: Mapping[str, str]) -> str | None:
    if role in (PYTHON_ROUTE, PYTHON_CLASS_ROUTE):
        return _FAMILIES.get(meta.get("family", ""))
    return _FRAMEWORKS.get(role)


def framework_of(role: str, meta: Mapping[str, str]) -> str | None:
    """The framework of a route claim of ``role``, served or not (G3 labels published rows with
    it): as :func:`diagnose` labels its entries, and ``oas3-tools`` for a served handler object."""
    if role == TS_HANDLER_OBJECT:
        return "oas3-tools"
    return _framework(role, meta)


def _route(plan: HttpClaimPlan, claim_id: int, role: str, meta: Mapping[str, str]) -> str:
    """The route body the declaration evaluates to, else the literal one, else ``""``."""
    if role == OAS3_APP:
        return ""
    return (
        plan.resolved_routes.get(claim_id)
        or plan.declared.get(claim_id)
        or _literal_body(role, meta)
    )


def _places(places: Sequence[Place], by_file: Mapping[int, FileRow]) -> tuple[DiagnosticPlace, ...]:
    """Places a reader can open, in a fixed order: by path, then line."""
    found = [
        DiagnosticPlace(by_file[file_id].rel_path, first, last, role)
        for file_id, first, last, role in places
        if file_id in by_file
    ]
    return tuple(sorted(found, key=lambda p: (p.path, p.line_start or 0, p.line_end or 0, p.role)))
