"""Static HTTP facts in Python source (HTTP linkage).

Two kinds of fact: a route a web framework is proven to serve, and a request an
HTTP client library is proven to make. *Proven* carries the weight.
``something.get("/hello")`` is a dictionary lookup far more often than it is a
request, so nothing here is matched by a method name. A fact is recorded only
when the receiver's identity is established by this module's **own** imports and
module-level assignments, each of which must be the only binding of its name
anywhere in the module::

    from fastapi import FastAPI
    app = FastAPI()              # `app` is a FastAPI application...
    @app.get("/hello/me")        # ...so this is a route

A parameter, a reassignment or a ``global`` makes a name unproven. A ``from x import *``
may rebind only names this module binds: a name it precedes stays proven when its binding
certainly runs and nothing reads the name before it; one bound before it, bound conditionally
after it, or read before its binding is unproven. A name only a star import could bind is never
read. An unproven name yields nothing; the module's other facts stand.

**Values are literal or they are nothing.** A route path or URL is read by
:func:`static_string`, which accepts a string literal, a concatenation of
literals, and an f-string with no placeholders. ``f"{base}/hello/me"`` is not
accepted — not even its suffix: whatever ``base`` holds decides the final path
(``https://x`` gives ``/hello/me``, ``https://x/api`` gives ``/api/hello/me``),
so a known tail proves no route at all.

**Routers are relative to their mount.** An ``APIRouter`` or ``Blueprint`` route
is served here only for mounts this file proves — the router constructed and
included into an application here, with literal prefixes — and is composed from
more than one line, so it is recorded as composed. A router mounted from another
file, nested in another router, or included inside an application factory is
decided by the global pass (:mod:`repoatlas.graph.py_routes`) from claims this
module records: its receivers, its routes, its mounts of imported routers and its
path constants. An application passed to ``.mount()`` here is itself a
sub-application and yields nothing.
Application-level routes are taken as served at the root, which is what the file
states; an application mounted under a prefix from *another* file is a known
limitation.

**Where an application is published** is read from one source only: a flasgger
Swagger 2 template bound to that exact Flask application in its own module, stating
``host``, ``schemes`` and ``basePath`` — the document flasgger serves as the
deployed Swagger, from which the template's three fields override any other
configuration. Each route of the application is then *exposed* at
``scheme://host`` + ``basePath`` + route, and recorded both as an
``EXPOSES_HTTP_ENDPOINT`` fact on the shared endpoint node and as an
:data:`~repoatlas.parsers.http_facts.HTTP_EXPOSED_ENDPOINT` claim carrying the
lines that prove it. The claim is generic on purpose: it names the identity's
source kind and nothing about flasgger, so another source-backed extractor produces
the same claim.

**A request's URL** for the cross-repository join
(:data:`~repoatlas.parsers.http_facts.HTTP_CLIENT_CALL`) is read a little further
than a literal: a module constant that holds a URL, or an f-string whose origin is
literal or such a constant, with each placeholder filling one whole path segment.
Only the per-file facts above stay literal-only.

**A client object** -- ``requests.Session()``, ``httpx.Client(...)``, ``httpx.AsyncClient(...)`` --
is followed only where its identity is lexical: bound by one ``name = C(...)`` or
``with C(...) as name`` that is the name's only binding in the module, constructed with no
positional or starred argument and no ``transport``, ``mounts``, ``app`` or ``event_hooks``,
and reached from the scope the binding lives in. An attribute store on the name, or a read of
its ``event_hooks`` / ``hooks``, discards it. A literal absolute URL is read as a module-level
call is (the base is ignored). A rooted URL (``"/users"``, ``f"/users/{uid}"``) is composed
onto an ``httpx`` ``base_url`` the source states -- a claim for the join, no per-file fact --
or, on a ``base_url`` it does not state, recorded as a claim with no origin whose host part is
the synthetic ``{name.base_url}`` (D2: counted, never joined). A client with *no* base has no
destination for a rooted URL, so one yields nothing. Every such claim carries ``client`` and
``client_at``.

Parsed, never imported: this reads the syntax tree only (V1_SPEC §17).
"""

from __future__ import annotations

import ast
import json
import re
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from repoatlas.models import http
from repoatlas.models.parsing import EdgeType, EntityType, ParsedReference
from repoatlas.parsers.http_facts import (
    BASE_UNKNOWN,
    COMPOSED,
    EXACT,
    HTTP_CLIENT_CALL,
    HTTP_EXPOSED_ENDPOINT,
    PYTHON_CONSTANTS,
    PYTHON_MOUNT,
    PYTHON_RECEIVER,
    PYTHON_ROUTE,
    PYTHON_SCRIPT,
    http_reference,
    pending_reference,
)

_FASTAPI_VERBS = {
    "get": "GET",
    "put": "PUT",
    "post": "POST",
    "delete": "DELETE",
    "options": "OPTIONS",
    "head": "HEAD",
    "patch": "PATCH",
    "trace": "TRACE",
}
_FLASK_VERBS = {"get": "GET", "post": "POST", "put": "PUT", "delete": "DELETE", "patch": "PATCH"}
_CLIENT_VERBS = {
    "get": "GET",
    "options": "OPTIONS",
    "head": "HEAD",
    "post": "POST",
    "put": "PUT",
    "patch": "PATCH",
    "delete": "DELETE",
}

_LIBRARIES = frozenset({"fastapi", "flask", "requests", "httpx", "flasgger", "os"})
_CLIENT_LIBRARIES = frozenset({"requests", "httpx"})

#: What constructs a client object: ``requests.Session()`` takes no argument at all;
#: an ``httpx`` client takes keywords only.
_SESSION_CONSTRUCTORS = frozenset({"requests.Session", "requests.session"})
_HTTPX_CONSTRUCTORS = frozenset({"httpx.Client", "httpx.AsyncClient"})
#: Keywords that send an ``httpx`` client's requests somewhere else, or run code around them.
_REDIRECTING_KEYWORDS = frozenset({"transport", "mounts", "app", "event_hooks"})
#: Attributes whose read makes a client unprovable: a hook can rewrite any request.
_HOOK_ATTRIBUTES = frozenset({"event_hooks", "hooks"})
#: Client objects one module may have before none is read.
_MAX_INSTANCES = 32
#: A string inside a base expression shown with a claim: a name, a key, a setting.
_EXPRESSION_STRING = re.compile(r"[A-Za-z0-9_.-]{1,64}")
#: An integer inside a base expression shown with a claim is a small index, nothing else.
_MAX_EXPRESSION_INTEGER = 1000
_MAX_BASE_EXPRESSION = 128

#: A client object's kind, and how much of its base the source states.
_SESSION = "session"
_HTTPX = "httpx"
_NO_BASE = "none"
_STATED_BASE = "stated"
_UNSTATED_BASE = "unstated"
#: A client's ``base_url`` as read: its kind, the stated base, its line, its expression.
_ClientBase = tuple[str, str | None, int | None, str | None]

_FASTAPI_APP = "fastapi-app"
_FASTAPI_ROUTER = "fastapi-router"
_FLASK_APP = "flask-app"
_FLASK_BLUEPRINT = "flask-blueprint"

_CONSTRUCTORS = {
    "fastapi.FastAPI": _FASTAPI_APP,
    "fastapi.APIRouter": _FASTAPI_ROUTER,
    "flask.Flask": _FLASK_APP,
    "flask.Blueprint": _FLASK_BLUEPRINT,
}

#: The one callable that binds a published identity to a Flask application.
_SWAGGER = "flasgger.Swagger"
#: ``identity_source`` of the identity a flasgger Swagger 2 template states.
FLASGGER_SWAGGER2 = "flasgger-swagger2"
#: The template fields that decide a Swagger 2 base; all three must be stated.
_TEMPLATE_FIELDS = ("host", "basePath", "schemes")
#: What catching the failed import of an optional dependency looks like.
_IMPORT_ERRORS = frozenset({"ImportError", "ModuleNotFoundError"})
#: Display only: a placeholder's source text is shortened past this.
_MAX_EXPRESSION = 64
#: ``http://`` and nothing after it: the host that follows is a value.
_SCHEME_ONLY = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")
#: Operands one ``+`` chain is read through.
_MAX_ADDED = 16

#: Deep enough for any literal someone writes by hand; shallow enough that an
#: adversarial chain of ``+`` cannot recurse the parser into the ground.
_MAX_STRING_DEPTH = 32

#: Mounting calls: the parameter naming what is mounted, the one naming its prefix, and
#: the kind it must be.
_MOUNT_CALLS = {
    "include_router": ("router", "prefix", _FASTAPI_ROUTER),
    "register_blueprint": ("blueprint", "url_prefix", _FLASK_BLUEPRINT),
}
#: Applications, which serve their routes at the root of their paths.
_APPLICATIONS = frozenset({_FASTAPI_APP, _FLASK_APP})
#: A constant other modules may read only when it could be a piece of a path or a URL:
#: short, and without whitespace -- prose, messages and format strings are left out.
_MAX_CONSTANT = 256
#: Constants one module offers, in line order; past this, the rest are not offered.
_MAX_CONSTANTS = 64


def static_string(node: ast.expr | None, depth: int = 0) -> str | None:
    """The value of a literal string expression, or ``None``.

    Accepted: ``"a"``, ``"a" + "b"`` (recursively), and an f-string whose parts
    are all literal. Everything else — a name, an attribute, a call, ``%``,
    ``.format``, an f-string with any placeholder — is not knowable without
    running the program.
    """
    if node is None or depth > _MAX_STRING_DEPTH:
        return None
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                return None
            parts.append(value.value)
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = static_string(node.left, depth + 1)
        right = static_string(node.right, depth + 1) if left is not None else None
        return None if left is None or right is None else left + right
    return None


def _binding_counts(tree: ast.AST) -> tuple[Counter[str], list[ast.ImportFrom]]:
    """How many times each name is bound anywhere in the module (or function), and its stars.

    A star import binds no name it states: which names it could rebind is
    :func:`_star_unproven`'s to say.
    """
    counts: Counter[str] = Counter()
    stars: list[ast.ImportFrom] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            counts[node.id] += 1
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            counts[node.name] += 1
        elif isinstance(node, ast.arg):
            counts[node.arg] += 1
        elif isinstance(node, ast.ImportFrom):
            if any(alias.name == "*" for alias in node.names):
                stars.append(node)
        elif isinstance(node, ast.alias):
            if node.name != "*":
                counts[(node.asname or node.name).split(".")[0]] += 1
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            counts.update(node.names)
        elif (isinstance(node, ast.ExceptHandler) and node.name) or (
            isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name
        ):
            counts[node.name] += 1
        elif isinstance(node, ast.MatchMapping) and node.rest:
            counts[node.rest] += 1
        elif isinstance(name := getattr(node, "name", None), str) and type(node).__name__ in {
            "TypeVar",
            "ParamSpec",
            "TypeVarTuple",
        }:
            counts[name] += 1
    return counts, stars


#: Why a name a star import could have rebound is unproven: internal, for tests and measurement.
_STAR_AFTER_BINDING = "star-after-binding"
_STAR_BEFORE_CONDITIONAL = "star-before-conditional-binding"
_STAR_READ_BEFORE_BINDING = "star-read-before-binding"

_Position = tuple[int, int]
_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
_WITHS = (ast.With, ast.AsyncWith)
#: ``except*`` needs Python 3.11; 3.10 is supported.
_TRY_STAR = getattr(ast, "TryStar", None)
_TRIES: tuple[type[ast.Try], ...] = (ast.Try,) if _TRY_STAR is None else (ast.Try, _TRY_STAR)


def _position(statement: ast.stmt) -> _Position:
    """Where a statement begins: a decorated definition, at its first decorator."""
    found = (statement.lineno, statement.col_offset)
    for decorator in getattr(statement, "decorator_list", ()):
        found = min(found, (decorator.lineno, decorator.col_offset))
    return found


def _nested_bodies(statement: ast.stmt) -> list[list[ast.stmt]]:
    """The statement lists a compound statement runs in its own scope; none for a simple one."""
    if isinstance(statement, (ast.If, ast.For, ast.AsyncFor, ast.While)):
        return [statement.body, statement.orelse]
    if isinstance(statement, _WITHS):
        return [statement.body]
    if isinstance(statement, _TRIES):
        bodies = [statement.body, *(handler.body for handler in statement.handlers)]
        return [*bodies, statement.orelse, statement.finalbody]
    if isinstance(statement, ast.Match):
        return [case.body for case in statement.cases]
    return []


def _scope_statements(tree: ast.Module) -> list[ast.stmt]:
    """Every statement that runs in module scope; a definition's body does not.

    Loops, ``with``, ``try`` and ``match`` bodies are module scope. An explicit stack: the
    constructor is not covered by the ``RecursionError`` guard.
    """
    found: list[ast.stmt] = []
    pending = list(tree.body)
    while pending:
        statement = pending.pop()
        found.append(statement)
        for body in _nested_bodies(statement):
            pending.extend(body)
    return found


def _defaults(arguments: ast.arguments) -> list[ast.expr]:
    return [*arguments.defaults, *(value for value in arguments.kw_defaults if value is not None)]


def _headers(definition: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> list[ast.expr]:
    """What a definition evaluates where it stands, besides its body."""
    if isinstance(definition, ast.ClassDef):
        return [
            *definition.decorator_list,
            *definition.bases,
            *(keyword.value for keyword in definition.keywords),
        ]
    return [*definition.decorator_list, *_defaults(definition.args)]


def _bound_names(statement: ast.stmt) -> set[str]:
    """Every name ``statement`` binds in the scope holding it, without entering an inner scope.

    Mirrors ``py_uses._stored``. A definition binds its name and evaluates its decorators,
    defaults, bases and keywords here; a comprehension binds only its ``:=`` targets.
    """
    names: set[str] = set()
    pending: list[ast.AST] = [statement]
    while pending:
        current = pending.pop()
        if isinstance(current, ast.Name):
            if isinstance(current.ctx, (ast.Store, ast.Del)):
                names.add(current.id)
        elif isinstance(current, _DEFINITIONS):
            names.add(current.name)
            pending.extend(_headers(current))
        elif isinstance(current, ast.Lambda):
            pending.extend(_defaults(current.args))
        elif isinstance(current, _COMPREHENSIONS):
            names.update(
                inner.target.id
                for inner in ast.walk(current)
                if isinstance(inner, ast.NamedExpr) and isinstance(inner.target, ast.Name)
            )
        else:
            if isinstance(current, ast.alias):
                if current.name != "*":
                    names.add((current.asname or current.name).split(".")[0])
            elif (isinstance(current, ast.ExceptHandler) and current.name) or (
                isinstance(current, (ast.MatchAs, ast.MatchStar)) and current.name
            ):
                names.add(current.name)
            elif isinstance(current, ast.MatchMapping) and current.rest:
                names.add(current.rest)
            pending.extend(ast.iter_child_nodes(current))
    return names


def _is_name(node: ast.AST | None, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


def _binds_directly(statement: ast.stmt, name: str) -> bool:
    """Whether running ``statement`` itself binds ``name`` (its nested statements aside)."""
    if isinstance(statement, (ast.Import, ast.ImportFrom)):
        return name in _bound_names(statement)
    if isinstance(statement, _DEFINITIONS):
        return statement.name == name
    if isinstance(statement, ast.Assign):
        return any(_is_name(target, name) for target in statement.targets)
    if isinstance(statement, ast.AnnAssign):
        return statement.value is not None and _is_name(statement.target, name)
    if isinstance(statement, (ast.For, ast.AsyncFor)):
        return _is_name(statement.target, name)
    if isinstance(statement, _WITHS):
        return any(_is_name(item.optional_vars, name) for item in statement.items)
    return False


def _required_bodies(statement: ast.stmt) -> list[list[ast.stmt]] | None:
    """The bodies that must each bind a name for ``statement`` to, or ``None`` for a leaf.

    An ``if`` chain with no final ``else`` has a branch that runs nothing: an empty body.
    """
    if isinstance(statement, ast.If):
        bodies: list[list[ast.stmt]] = []
        branch = statement
        while True:
            bodies.append(branch.body)
            if not branch.orelse:
                return [[]]
            if len(branch.orelse) == 1 and isinstance(branch.orelse[0], ast.If):
                branch = branch.orelse[0]
                continue
            bodies.append(branch.orelse)
            return bodies
    if isinstance(statement, _TRIES):
        return [statement.body, *(handler.body for handler in statement.handlers)]
    return None


def _certainly_binds(statement: ast.stmt, name: str) -> bool:
    """Whether running ``statement`` certainly binds ``name``.

    Iterative, post-order: a compound statement binds it when every body it may run has a
    statement that does.
    """
    certain: dict[int, bool] = {}
    pending: list[tuple[ast.stmt, bool]] = [(statement, False)]
    while pending:
        current, expanded = pending.pop()
        bodies = _required_bodies(current)
        if bodies is None:
            certain[id(current)] = _binds_directly(current, name)
        elif expanded:
            certain[id(current)] = all(
                any(certain.get(id(child), False) for child in body) for body in bodies
            )
        else:
            pending.append((current, True))
            pending.extend((child, False) for body in bodies for child in body)
    return certain[id(statement)]


def _evaluated_first(statement: ast.stmt) -> list[ast.AST]:
    """The parts of a statement that run before its binding takes effect.

    A compound statement binds somewhere inside, so all of it counts.
    """
    if isinstance(statement, ast.Assign):
        return [statement.value]
    if isinstance(statement, ast.AnnAssign):
        return [statement.annotation, *([statement.value] if statement.value else [])]
    if isinstance(statement, (ast.For, ast.AsyncFor)):
        return [statement.iter]
    if isinstance(statement, _WITHS):
        return [item.context_expr for item in statement.items]
    if isinstance(statement, _DEFINITIONS):
        return list(_headers(statement))
    if isinstance(statement, (ast.If, *_TRIES)):
        return [statement]
    return []


def _star_unproven(tree: ast.Module, stars: list[ast.ImportFrom]) -> dict[str, str] | None:
    """The module-level names a star import could have rebound, each with the reason.

    A star can rebind only a name this module binds in module scope. It cannot rebind one
    bound after it by a statement that certainly runs and that nothing reads first: that
    binding is the last word. ``None`` when a star stands in a function or class body
    (CPython refuses it): nothing in such a module is proven.
    """
    in_module = {id(statement) for statement in _scope_statements(tree)}
    if any(id(star) not in in_module for star in stars):
        return None
    last_star = max(_position(star) for star in stars)
    sites: dict[str, list[int]] = {}
    for index, statement in enumerate(tree.body):
        for name in _bound_names(statement):
            sites.setdefault(name, []).append(index)
    first_read: dict[str, _Position] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            at = (node.lineno, node.col_offset)
            if node.id not in first_read or at < first_read[node.id]:
                first_read[node.id] = at
    unproven: dict[str, str] = {}
    for name, indexes in sites.items():
        statement = tree.body[indexes[0]]
        begins = _position(statement)
        if len(indexes) != 1 or not last_star < begins:
            unproven[name] = _STAR_AFTER_BINDING
        elif not _certainly_binds(statement, name):
            unproven[name] = _STAR_BEFORE_CONDITIONAL
        elif (name in first_read and first_read[name] < begins) or any(
            isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load)
            for part in _evaluated_first(statement)
            for node in ast.walk(part)
        ):
            unproven[name] = _STAR_READ_BEFORE_BINDING
    return unproven


@dataclass(frozen=True, slots=True)
class _Deployment:
    """A base location the module states an application is published at."""

    base: str
    #: The kind of source that stated it (``identity_source``).
    source: str
    #: The line of the literal naming the host: the identity's citable part.
    identity_at: int
    #: The line that binds the identity to the application.
    bound_at: int


@dataclass(slots=True)
class _Receiver:
    """A name proven to hold a framework application, router or blueprint."""

    kind: str
    #: The router's or blueprint's own prefix. ``None`` for a blueprint that
    #: declares none; always ``""`` or a prefix for a FastAPI router.
    prefix: str | None = None
    #: Where this file mounts it: the prefix (a ``None`` entry: the blueprint's own)
    #: and the name of the application it is mounted on.
    mounts: list[tuple[str | None, str]] = field(default_factory=list)
    #: Cleared when something makes its served paths unknowable from here.
    usable: bool = True
    #: Where an application is published, when its own module states it.
    deployments: tuple[_Deployment, ...] = ()
    #: The line that constructs it.
    line: int = 0


@dataclass(frozen=True, slots=True)
class _Imported:
    """Where a name this module imports comes from, as its import statement says."""

    #: The module, relative dots included: ``.views``, ``service_prefix``.
    module: str
    #: The name taken from it; ``None`` for ``import module``, which binds the module.
    attribute: str | None


def _submodule(module: str, name: str) -> str:
    """``module.name`` as an import specifier: ``.`` + ``views`` is ``.views``."""
    return f"{module}{name}" if module.endswith(".") else f"{module}.{name}"


def _path_like(value: str) -> bool:
    return len(value) <= _MAX_CONSTANT and not any(char.isspace() for char in value)


@dataclass(frozen=True, slots=True)
class _ClientUrl:
    """A request's origin and path shape, as far as the source states them."""

    #: ``None`` for a base the source does not state (D2): the path is read, the host not.
    origin: str | None
    #: Each path segment's literal text, or ``None`` where one value fills it.
    segments: tuple[str | None, ...]
    #: The URL as written, a placeholder shown as ``{expression}``: display only.
    display: str
    #: The line of the module constant the URL or its base came from.
    base_at: int | None = None


@dataclass(frozen=True, slots=True)
class _ClientInstance:
    """A client object whose binding is lexical, and what the source states of its base."""

    #: :data:`_SESSION` or :data:`_HTTPX`.
    kind: str
    #: :data:`_NO_BASE`, :data:`_STATED_BASE` or :data:`_UNSTATED_BASE`.
    base_kind: str
    #: The base as the source states it, for :data:`_STATED_BASE` only: an absolute URL with
    #: no query, fragment or credentials.
    base_text: str | None
    #: The line of the base: the keyword's value, or the constant it names.
    base_at: int | None
    #: The base's expression, for :data:`_UNSTATED_BASE` when it is a bounded chain of names.
    base_expr: str | None
    #: The line of the constructor call.
    constructed_at: int
    #: The one name the module binds it to, and the line of that binding.
    name: str
    bound_at: int
    #: ``(line, column, end line, end column)`` of what the name reaches; ``None`` for the
    #: whole module.
    reach: tuple[int, int, int, int] | None

    @property
    def library(self) -> str:
        return "requests" if self.kind == _SESSION else "httpx"

    def reaches(self, node: ast.expr) -> bool:
        """Whether the name written at ``node`` is read where this binding is the one in force."""
        if self.reach is None:
            return True
        if node.end_lineno is None or node.end_col_offset is None:
            return False
        begin, finish = (self.reach[0], self.reach[1]), (self.reach[2], self.reach[3])
        ends = (node.end_lineno, node.end_col_offset)
        return (node.lineno, node.col_offset) >= begin and ends <= finish


def _span(body: list[ast.stmt]) -> tuple[int, int, int, int] | None:
    """The source span of a block of statements, or ``None`` for an empty one."""
    if not body:
        return None
    first, last = body[0], body[-1]
    if last.end_lineno is None or last.end_col_offset is None:
        return None
    return first.lineno, first.col_offset, last.end_lineno, last.end_col_offset


def _constructions(
    tree: ast.Module,
) -> Iterator[tuple[str, ast.expr, int, tuple[int, int, int, int] | None]]:
    """Each ``name = value``, ``name: T = value`` and ``with value as name`` outside a class body.

    Yields the name, the value, the line of the binding and what the name reaches: the whole
    module for a module-level binding, a function's body for one in a function, the ``with``
    body for a ``with``. A class body binds attributes, not names a method can read.
    """
    stack: list[tuple[ast.AST, ast.AST]] = [(tree, tree)]
    while stack:
        node, scope = stack.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                stack.append((child, child))
            else:
                stack.append((child, scope))
            reach: tuple[int, int, int, int] | None
            if isinstance(scope, ast.Module):
                reach = None
            elif isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                reach = _span(scope.body)
                if reach is None:
                    continue
            else:
                continue
            if (
                isinstance(child, ast.Assign)
                and len(child.targets) == 1
                and isinstance(child.targets[0], ast.Name)
            ):
                yield child.targets[0].id, child.value, child.lineno, reach
            elif (
                isinstance(child, ast.AnnAssign)
                and isinstance(child.target, ast.Name)
                and child.value is not None
            ):
                yield child.target.id, child.value, child.lineno, reach
            elif isinstance(child, (ast.With, ast.AsyncWith)):
                body = _span(child.body)
                for item in child.items:
                    if isinstance(item.optional_vars, ast.Name) and body is not None:
                        yield (
                            item.optional_vars.id,
                            item.context_expr,
                            item.optional_vars.lineno,
                            body,
                        )


def _base_expression(node: ast.expr) -> str | None:
    """A base expression worth showing: names, attributes, subscripts and calls of them.

    Every string in it must look like a name or a setting's key, every number like an index:
    anything else -- a URL, a credential, a long string -- keeps the expression out of the
    claim altogether, so no literal the source puts there is ever copied.
    """
    for part in ast.walk(node):
        if isinstance(part, ast.Constant):
            value = part.value
            if isinstance(value, str):
                if _EXPRESSION_STRING.fullmatch(value) is None:
                    return None
            elif not (
                value is None
                or (
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and 0 <= value < _MAX_EXPRESSION_INTEGER
                )
            ):
                return None
        elif not isinstance(
            part, (ast.Name, ast.Attribute, ast.Subscript, ast.Call, ast.keyword, ast.Load)
        ):
            return None
    text = ast.unparse(node)
    return text if len(text) <= _MAX_BASE_EXPRESSION else None


def _pieces(operands: list[ast.expr]) -> list[list[str]] | None:
    """The parts of a URL's later operands: literal text, and one substitution per value.

    The operands of a ``+`` chain, or an f-string's pieces; an operand may itself be an
    f-string. A conversion or a format specification is not read. ``None`` when an operand
    is anything else.
    """
    parts: list[list[str]] = []
    for value in operands:
        for piece in value.values if isinstance(value, ast.JoinedStr) else [value]:
            if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                parts.append([http.TEMPLATE_LITERAL, piece.value])
            elif isinstance(piece, ast.FormattedValue):
                if piece.conversion != -1 or piece.format_spec is not None:
                    return None
                parts.append([http.TEMPLATE_SUBSTITUTION, ast.unparse(piece.value)])
            elif isinstance(piece, ast.Name | ast.Attribute | ast.Subscript | ast.Call):
                parts.append([http.TEMPLATE_SUBSTITUTION, ast.unparse(piece)])
            else:
                return None
    return parts


def _dotted(segments: Iterable[str | None]) -> bool:
    """Whether a path has a ``.`` or ``..`` segment, which a client resolves before it sends."""
    return any(segment in {".", ".."} for segment in segments)


def _display(template: http.RouteTemplate) -> str:
    """A template's path without its leading ``/``, a substitution shown as ``{expression}``."""
    expressions = iter(template.expressions)
    return "/".join(
        segment if segment is not None else "{" + next(expressions, "")[:_MAX_EXPRESSION] + "}"
        for segment in template.segments
    )


def _module_statements(tree: ast.Module) -> Iterator[ast.stmt]:
    """Statements that run at module level: the body, and module-level ``if``/``try`` bodies.

    Function and class bodies are not module level, and neither is a loop's: a loop
    body may run any number of times, which is not "the only binding".
    """
    stack = list(reversed(tree.body))
    while stack:
        statement = stack.pop()
        yield statement
        if isinstance(statement, ast.If):
            stack.extend(reversed([*statement.body, *statement.orelse]))
        elif isinstance(statement, ast.Try):
            handled = [inner for handler in statement.handlers for inner in handler.body]
            stack.extend(
                reversed([*statement.body, *handled, *statement.orelse, *statement.finalbody])
            )


def _catches_import_error(handler: ast.ExceptHandler) -> bool:
    caught = handler.type
    names = caught.elts if isinstance(caught, ast.Tuple) else [caught]
    return any(isinstance(name, ast.Name) and name.id in _IMPORT_ERRORS for name in names)


def _dynamic(call: ast.Call) -> bool:
    """``*args`` or ``**kwargs`` could carry any argument this reader looks for."""
    return any(isinstance(arg, ast.Starred) for arg in call.args) or any(
        keyword.arg is None for keyword in call.keywords
    )


def _keywords(call: ast.Call) -> dict[str, ast.expr]:
    return {keyword.arg: keyword.value for keyword in call.keywords if keyword.arg is not None}


def _argument(call: ast.Call, index: int, name: str) -> ast.expr | None:
    """A parameter passed by position or by name — never both, which is an error."""
    keywords = _keywords(call)
    positional = call.args[index] if len(call.args) > index else None
    if positional is not None and name in keywords:
        return None
    return positional if positional is not None else keywords.get(name)


def _fastapi_prefix(prefix: str) -> bool:
    """FastAPI's own rule: empty, or leading ``/`` and no trailing ``/``."""
    return prefix == "" or (
        prefix.startswith("/") and not prefix.endswith("/") and http.route_path(prefix) is not None
    )


def flask_join(prefix: str | None, rule: str) -> str | None:
    """A blueprint route under a URL prefix, where the result is unambiguous.

    Flask joins the prefix and the rule on one ``/``. That is the same as plain
    appending exactly when the prefix has no trailing slash and the rule starts
    with a single one, so those are the only shapes read here; anything else
    fails closed rather than depending on the join's finer behaviour.
    """
    if prefix is None:
        return rule
    if prefix and (not prefix.startswith("/") or (prefix != "/" and prefix.endswith("/"))):
        return None
    if not rule:
        return prefix or None
    if not rule.startswith("/") or rule.startswith("//"):
        return None
    return prefix.rstrip("/") + rule


def _static_methods(node: ast.expr) -> tuple[str, ...] | None:
    """A literal ``["GET", "post"]``, upper-cased as both frameworks do."""
    if not isinstance(node, (ast.List, ast.Tuple, ast.Set)) or not node.elts:
        return None
    methods: dict[str, None] = {}
    for element in node.elts:
        value = static_string(element)
        method = http.http_method(value.upper()) if value is not None else None
        if method is None:
            return None
        methods.setdefault(method, None)
    return tuple(methods)


def _import_table(statements: list[ast.stmt], counts: Counter[str]) -> dict[str, _Imported]:
    """Where each name the statements import comes from, for names bound exactly once."""
    table: dict[str, _Imported] = {}
    for statement in statements:
        if isinstance(statement, ast.ImportFrom):
            module = "." * statement.level + (statement.module or "")
            for alias in statement.names:
                local = alias.asname or alias.name
                if alias.name != "*" and counts[local] == 1:
                    table[local] = _Imported(module, alias.name)
        elif isinstance(statement, ast.Import):
            for alias in statement.names:
                # `import a.b` binds `a`; `import a.b as c` binds the module `a.b`.
                local = alias.asname or alias.name.split(".")[0]
                if counts[local] == 1:
                    table[local] = _Imported(alias.name if alias.asname else local, None)
    return table


def _module_ref(node: ast.expr | None, imported: dict[str, _Imported]) -> tuple[str, str] | None:
    """``(module, name)`` for an imported name, or ``module.name`` on an imported module."""
    if isinstance(node, ast.Name):
        found = imported.get(node.id)
        if found is None or found.attribute is None:
            return None
        return found.module, found.attribute
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        found = imported.get(node.value.id)
        if found is None:
            return None
        module = found.module
        if found.attribute is not None:
            module = _submodule(found.module, found.attribute)
        return module, node.attr
    return None


def _expression_call(statement: ast.stmt) -> ast.Call | None:
    """``name.method(...)`` as a statement of its own."""
    if not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)):
        return None
    func = statement.value.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return statement.value
    return None


def _main_guard(statement: ast.stmt) -> bool:
    """``if __name__ == "__main__":``, either way round."""
    test = statement.test if isinstance(statement, ast.If) else None
    if not (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and len(test.comparators) == 1
    ):
        return False
    sides = (test.left, test.comparators[0])
    return any(isinstance(side, ast.Name) and side.id == "__name__" for side in sides) and any(
        isinstance(side, ast.Constant) and side.value == "__main__" for side in sides
    )


def _literal_lists(statements: list[ast.stmt], counts: Counter[str]) -> dict[str, list[ast.expr]]:
    """Names the statements bind, once, to a list or tuple display: ``routers = [a, b]``."""
    found: dict[str, list[ast.expr]] = {}
    for statement in statements:
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and isinstance(statement.value, (ast.List, ast.Tuple))
            and counts[statement.targets[0].id] == 1
        ):
            found[statement.targets[0].id] = list(statement.value.elts)
    return found


def _statement_calls(
    statements: list[ast.stmt], counts: Counter[str], lists: dict[str, list[ast.expr]]
) -> Iterator[ast.Call]:
    """``name.method(...)`` statements, with a loop over a literal list unrolled.

    ``for router in routers: app.include_router(router)`` is read as one call per
    element, each element in the loop variable's place, when the list is a display or
    a name bound once to one, the loop variable is bound by nothing else, and it is the
    call's first argument and appears nowhere else in it.
    """
    for statement in statements:
        call = _expression_call(statement)
        if call is not None:
            yield call
            continue
        if not (
            isinstance(statement, ast.For)
            and not statement.orelse
            and isinstance(statement.target, ast.Name)
            and counts[statement.target.id] == 1
        ):
            continue
        variable = statement.target.id
        iterated = statement.iter
        if isinstance(iterated, (ast.List, ast.Tuple)):
            elements: list[ast.expr] | None = list(iterated.elts)
        elif isinstance(iterated, ast.Name):
            elements = lists.get(iterated.id)
        else:
            elements = None
        if elements is None or any(isinstance(element, ast.Starred) for element in elements):
            continue
        for inner in statement.body:
            call = _expression_call(inner)
            first = call.args[0] if call is not None and call.args else None
            if (
                call is None
                or not isinstance(first, ast.Name)
                or first.id != variable
                or sum(
                    isinstance(node, ast.Name) and node.id == variable for node in ast.walk(call)
                )
                != 1
            ):
                continue
            for element in elements:
                unrolled = ast.Call(
                    func=call.func, args=[element, *call.args[1:]], keywords=call.keywords
                )
                yield ast.copy_location(unrolled, call)


def _mount_target(kind: str, call: ast.Call) -> ast.expr | None:
    """What an application's or router's mounting call mounts, or ``None``."""
    attribute = call.func.attr if isinstance(call.func, ast.Attribute) else ""
    if attribute == "include_router" and kind in {_FASTAPI_APP, _FASTAPI_ROUTER}:
        return _argument(call, 0, "router")
    if attribute == "register_blueprint" and kind in {_FLASK_APP, _FLASK_BLUEPRINT}:
        return _argument(call, 0, "blueprint")
    if attribute == "mount" and kind == _FASTAPI_APP:
        return _argument(call, 1, "app")
    return None


def _family(attribute: str, decorator: ast.Call) -> tuple[str, tuple[str, ...]] | None:
    """Which framework a decorator on an imported receiver can belong to, and its methods.

    The receiver's kind is decided where it is constructed, so the decorator is read
    under whatever both frameworks agree on, and the global pass checks the kind.
    """
    keywords = _keywords(decorator)
    if attribute == "route":
        methods = _static_methods(keywords["methods"]) if "methods" in keywords else ("GET",)
        return ("flask", methods) if methods is not None else None
    if attribute == "api_route" and "methods" in keywords:
        methods = _static_methods(keywords["methods"])
        return ("fastapi", methods) if methods is not None else None
    if "methods" in keywords:
        return None
    if attribute in _FLASK_VERBS:
        return "either", (_FLASK_VERBS[attribute],)
    if attribute in _FASTAPI_VERBS:
        return "fastapi", (_FASTAPI_VERBS[attribute],)
    return None


def _route_claim(
    receiver: dict[str, str],
    methods: tuple[str, ...],
    parts: list[list[str]],
    span: tuple[int, int],
    qualified: str,
) -> list[ParsedReference]:
    """One :data:`PYTHON_ROUTE` claim per method; ``receiver`` names who declares it."""
    literal = all(part[0] == "t" for part in parts)
    shown = "".join(part[1] if part[0] == "t" else "{" + ".".join(part[1:]) + "}" for part in parts)
    return [
        pending_reference(
            EdgeType.SERVES_HTTP_ROUTE,
            EntityType.HTTP_ROUTE,
            f"{method} {shown}",
            span[0],
            span[1],
            role=PYTHON_ROUTE,
            source_symbol=qualified,
            method=method,
            path=json.dumps(parts),
            provenance=EXACT if literal else COMPOSED,
            **receiver,
        )
        for method in methods
    ]


def _added(node: ast.BinOp) -> list[ast.expr] | None:
    """The operands of a ``+`` chain, left to right, or ``None`` past :data:`_MAX_ADDED`."""
    operands: list[ast.expr] = []
    current: ast.expr = node
    while isinstance(current, ast.BinOp) and isinstance(current.op, ast.Add):
        operands.append(current.right)
        current = current.left
        if len(operands) > _MAX_ADDED:
            return None
    operands.append(current)
    return list(reversed(operands))


class PythonHttp:
    """What one module proves about HTTP, and the facts that follow from it."""

    def __init__(self, tree: ast.Module) -> None:
        #: Local name → module, for ``import requests [as r]``.
        self._modules: dict[str, str] = {}
        #: Local name → ``module.attr``, for ``from fastapi import FastAPI``.
        self._names: dict[str, str] = {}
        self._receivers: dict[str, _Receiver] = {}
        #: Module constant → (its literal value, its line), each bound exactly once.
        self._constants: dict[str, tuple[str, int]] = {}
        #: Module constant → (its environment default, its line), each bound exactly once:
        #: ``X = os.environ.get("NAME", "<url>")``. Read for request URLs only, never for
        #: routes, whose paths a deployment's environment does not set.
        self._env_defaults: dict[str, tuple[str, int]] = {}
        #: Module name → its literal alternatives, each (value, line): bound once in every
        #: branch of one module-level ``if``/``elif``/``else`` and nowhere else.
        self._alternatives: dict[str, tuple[tuple[str, int], ...]] = {}
        #: The one alternative a request is being read under, by name.
        self._choice: dict[str, tuple[str, int]] = {}
        #: Local name → where it is imported from, for every import bound exactly once.
        self._imported: dict[str, _Imported] = {}
        #: Mounts only the global pass can decide: an imported router, or a factory's.
        self._mount_claims: list[ParsedReference] = []
        #: Module-level names bound once to a list or tuple display.
        self._lists: dict[str, list[ast.expr]] = {}
        #: Client objects whose binding is lexical, by their one name.
        self._clients: dict[str, _ClientInstance] = {}
        #: The line of a module-level ``if __name__ == "__main__":``, when there is one.
        self._script = next(
            (statement.lineno for statement in tree.body if _main_guard(statement)), None
        )
        #: Name → why a star import left it unproven. ``None``: a star stands outside module
        #: scope and nothing is proven. For tests and measurement; never emitted.
        self.star_unproven: dict[str, str] | None = {}
        counts, stars = _binding_counts(tree)
        unproven = _star_unproven(tree, stars) if stars else {}
        if unproven is None:
            self.star_unproven = None
            return
        for name in unproven:
            counts[name] += 1
        self.star_unproven = unproven
        self._imports(tree, counts)
        self._optional_imports(tree, counts)
        self._read_constants(tree, counts)
        self._read_alternatives(tree, counts)
        self._read_clients(tree, counts)
        self._constructors(tree, counts)
        self._mounts(tree, counts)
        self._factories(tree)
        self._identities(tree, counts)

    # -- what the module proves ---------------------------------------------------

    def _imports(self, tree: ast.Module, counts: Counter[str]) -> None:
        for statement in tree.body:
            if isinstance(statement, ast.Import):
                for alias in statement.names:
                    local = alias.asname or alias.name
                    if alias.name in _LIBRARIES and counts[local] == 1:
                        self._modules[local] = alias.name
            elif isinstance(statement, ast.ImportFrom) and not statement.level:
                if statement.module not in _LIBRARIES:
                    continue
                for alias in statement.names:
                    local = alias.asname or alias.name
                    if counts[local] == 1:
                        self._names[local] = f"{statement.module}.{alias.name}"
        self._imported = _import_table(tree.body, counts)
        self._lists = _literal_lists(tree.body, counts)

    def _optional_imports(self, tree: ast.Module, counts: Counter[str]) -> None:
        """``try: from flasgger import Swagger`` / ``except ImportError: Swagger = None``.

        The name has more than one binding, but every other one is ``None`` or
        ``False`` in a handler of that very import's failure. Calling either raises,
        so any call through the name that runs is a call to flasgger's. Read for
        flasgger only: it is the one optional dependency this module reads.
        """
        for statement in tree.body:
            if not isinstance(statement, ast.Try):
                continue
            imported = [
                alias.asname or alias.name
                for node in statement.body
                if isinstance(node, ast.ImportFrom) and node.module == "flasgger" and not node.level
                for alias in node.names
                if alias.name == "Swagger"
            ]
            if len(imported) != 1:
                continue
            local = imported[0]
            fallbacks = sum(
                1
                for handler in statement.handlers
                if _catches_import_error(handler)
                for node in handler.body
                if isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == local
                and isinstance(node.value, ast.Constant)
                and node.value.value in (None, False)
            )
            if fallbacks and counts[local] == 1 + fallbacks:
                self._names[local] = _SWAGGER

    def _resolve(self, node: ast.expr) -> str | None:
        """``module.attr`` a callee provably names, or ``None``."""
        if isinstance(node, ast.Name):
            return self._names.get(node.id)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            module = self._modules.get(node.value.id)
            return f"{module}.{node.attr}" if module is not None else None
        return None

    def _constructors(self, tree: ast.Module, counts: Counter[str]) -> None:
        for statement in tree.body:
            if (
                isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
            ):
                name, value = statement.targets[0].id, statement.value
            elif (
                isinstance(statement, ast.AnnAssign)
                and isinstance(statement.target, ast.Name)
                and statement.value is not None
            ):
                name, value = statement.target.id, statement.value
            else:
                continue
            if counts[name] != 1 or not isinstance(value, ast.Call) or _dynamic(value):
                continue
            kind = _CONSTRUCTORS.get(self._resolve(value.func) or "")
            if kind is None:
                continue
            receiver = self._receiver(kind, value)
            if receiver is not None:
                receiver.line = statement.lineno
                self._receivers[name] = receiver

    @staticmethod
    def _receiver(kind: str, call: ast.Call) -> _Receiver | None:
        keywords = _keywords(call)
        if kind == _FASTAPI_ROUTER:
            # `APIRouter`'s parameters are keyword-only.
            if call.args:
                return None
            prefix = static_string(keywords["prefix"]) if "prefix" in keywords else ""
            if prefix is None or not _fastapi_prefix(prefix):
                return None
            return _Receiver(kind, prefix)
        if kind == _FLASK_BLUEPRINT:
            # `url_prefix` is the sixth positional parameter; past five, it may
            # have been passed without a name.
            if len(call.args) > 5:
                return None
            if "url_prefix" not in keywords:
                return _Receiver(kind, None)
            prefix = static_string(keywords["url_prefix"])
            return _Receiver(kind, prefix) if prefix is not None else None
        return _Receiver(kind)

    def _mounts(self, tree: ast.Module, counts: Counter[str]) -> None:
        for call in _statement_calls(tree.body, counts, _literal_lists(tree.body, counts)):
            func = call.func
            if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
                continue
            owner = self._receivers.get(func.value.id)
            if owner is None:
                continue
            if func.attr == "include_router" and owner.kind in {_FASTAPI_APP, _FASTAPI_ROUTER}:
                self._mount(func.value.id, owner, call, "router", _FASTAPI_ROUTER, "prefix")
            elif func.attr == "register_blueprint" and owner.kind in {
                _FLASK_APP,
                _FLASK_BLUEPRINT,
            }:
                self._mount(func.value.id, owner, call, "blueprint", _FLASK_BLUEPRINT, "url_prefix")
            elif func.attr == "mount" and owner.kind == _FASTAPI_APP:
                for argument in (*call.args, *(keyword.value for keyword in call.keywords)):
                    if isinstance(argument, ast.Name) and argument.id in self._receivers:
                        self._receivers[argument.id].usable = False
                ref = _module_ref(_mount_target(owner.kind, call), self._imported)
                if ref is not None:
                    child = {"module": ref[0], "name": ref[1]}
                    self._claim_mount(func.value.id, owner, call, child)

    def _mount(
        self,
        owner_name: str,
        owner: _Receiver,
        call: ast.Call,
        parameter: str,
        child_kind: str,
        prefix_name: str,
    ) -> None:
        argument = _argument(call, 0, parameter)
        child = self._receivers.get(argument.id) if isinstance(argument, ast.Name) else None
        if child is None:
            ref = _module_ref(argument, self._imported)
            if ref is not None:
                self._claim_mount(owner_name, owner, call, {"module": ref[0], "name": ref[1]})
            return
        if child.kind != child_kind:
            return
        if owner.kind == child_kind or _dynamic(call):
            # Nested in another router, or mounted with arguments this cannot
            # read: where it is served is decided somewhere else.
            child.usable = False
            return
        keywords = _keywords(call)
        if prefix_name not in keywords:
            child.mounts.append(("" if child_kind == _FASTAPI_ROUTER else None, owner_name))
            return
        prefix = static_string(keywords[prefix_name])
        if prefix is None or (child_kind == _FASTAPI_ROUTER and not _fastapi_prefix(prefix)):
            child.usable = False
            return
        child.mounts.append((prefix, owner_name))

    def _claim_mount(
        self, owner_name: str, owner: _Receiver, call: ast.Call, child: dict[str, str]
    ) -> None:
        """A mount only the global pass can decide: of an imported router, or a factory's.

        Recorded unreadable -- which keeps the router from being served at all, as a
        mount this module cannot read does to one of its own -- when its arguments may
        carry a prefix this cannot read, when it is a sub-application ``mount``, or when
        a blueprint is nested in a blueprint, whose composition Flask decides.
        """
        attribute = call.func.attr if isinstance(call.func, ast.Attribute) else ""
        readable = attribute in _MOUNT_CALLS and not _dynamic(call)
        prefix: str | None = None
        if readable:
            _, prefix_name, child_kind = _MOUNT_CALLS[attribute]
            keywords = _keywords(call)
            if prefix_name in keywords:
                prefix = static_string(keywords[prefix_name])
                readable = prefix is not None and (
                    child_kind != _FASTAPI_ROUTER or _fastapi_prefix(prefix)
                )
            elif child_kind == _FASTAPI_ROUTER:
                prefix = ""
        if owner.kind == _FLASK_BLUEPRINT:
            readable = False
        self._mount_claims.append(
            pending_reference(
                EdgeType.REFERENCES,
                EntityType.SYMBOL,
                child["name"],
                call.lineno,
                call.end_lineno or call.lineno,
                role=PYTHON_MOUNT,
                owner=owner_name,
                owner_kind=owner.kind,
                call=attribute,
                readable="true" if readable else "false",
                prefix=json.dumps(prefix),
                **child,
            )
        )

    def _factories(self, tree: ast.Module) -> None:
        """Mounts in an application factory: a module-level function that constructs one.

        The application is the function's local, constructed once at the function's top
        level and bound nowhere else in it; the mounts are its own top-level statements,
        so a conditional or looped registration is not read. A mounted name is read as
        the function binds it: imported there or at module level, or this module's own
        router -- never one a parameter or a local shadows.
        """
        for function in tree.body:
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            counts, _ = _binding_counts(function)
            imported = {name: found for name, found in self._imported.items() if not counts[name]}
            imported.update(_import_table(function.body, counts))
            applications: dict[str, str] = {}
            for statement in function.body:
                if (
                    isinstance(statement, ast.Assign)
                    and len(statement.targets) == 1
                    and isinstance(statement.targets[0], ast.Name)
                    and isinstance(statement.value, ast.Call)
                    and not _dynamic(statement.value)
                    and counts[statement.targets[0].id] == 1
                ):
                    kind = _CONSTRUCTORS.get(self._resolve(statement.value.func) or "")
                    if kind in _APPLICATIONS:
                        applications[statement.targets[0].id] = kind
            lists = {name: found for name, found in self._lists.items() if not counts[name]}
            lists.update(_literal_lists(function.body, counts))
            for call in _statement_calls(function.body, counts, lists):
                if not isinstance(call.func, ast.Attribute):
                    continue
                name = call.func.value.id if isinstance(call.func.value, ast.Name) else ""
                kind = applications.get(name)
                if kind is None:
                    continue
                argument = _mount_target(kind, call)
                if (
                    isinstance(argument, ast.Name)
                    and argument.id in self._receivers
                    and not counts[argument.id]
                ):
                    child = {"name": argument.id}
                else:
                    ref = _module_ref(argument, imported)
                    if ref is None:
                        continue
                    child = {"module": ref[0], "name": ref[1]}
                self._claim_mount(f"{function.name}.{name}", _Receiver(kind), call, child)

    # -- where an application is published -------------------------------------------

    def _identities(self, tree: ast.Module, counts: Counter[str]) -> None:
        """Deployments of each Flask application, from its flasgger Swagger 2 template.

        Every ``Swagger(...)`` call in the module must be readable, or none is used:
        a call this cannot read — inside a function, without a positional
        application, through ``**kwargs`` — may bind any application here.
        """
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and self._resolve(node.func) == _SWAGGER
        ]
        if not calls:
            return
        module_level: set[int] = set()
        dicts: dict[str, ast.Dict] = {}
        for statement in _module_statements(tree):
            value = getattr(statement, "value", None)
            if isinstance(statement, (ast.Expr, ast.Assign, ast.AnnAssign)) and isinstance(
                value, ast.Call
            ):
                module_level.add(id(value))
            if (
                isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
                and isinstance(statement.value, ast.Dict)
                and counts[statement.targets[0].id] == 1
            ):
                dicts[statement.targets[0].id] = statement.value
        bound: dict[str, list[ast.Call]] = {}
        for call in calls:
            first = call.args[0] if call.args else None
            if (
                id(call) not in module_level
                or _dynamic(call)
                or len(call.args) != 1
                or not isinstance(first, ast.Name)
                or "app" in _keywords(call)
            ):
                return
            bound.setdefault(first.id, []).append(call)
        for name, found in bound.items():
            receiver = self._receivers.get(name)
            if receiver is None or receiver.kind != _FLASK_APP or len(found) != 1:
                continue
            receiver.deployments = self._template_deployments(found[0], dicts)

    @staticmethod
    def _template_deployments(
        call: ast.Call, dicts: dict[str, ast.Dict]
    ) -> tuple[_Deployment, ...]:
        """The bases a ``Swagger(app, template=…)`` call states, or none.

        flasgger applies the template last, so its ``host``, ``basePath`` and
        ``schemes`` decide the published document whatever the configuration says —
        which holds only for the fields the template states: all three are required.
        """
        keywords = _keywords(call)
        if "template_file" in keywords or "template" not in keywords:
            return ()
        template = keywords["template"]
        if isinstance(template, ast.Name):
            template = dicts.get(template.id)
        if not isinstance(template, ast.Dict) or any(key is None for key in template.keys):
            return ()
        names = [static_string(key) for key in template.keys]
        if None in names or len(set(names)) != len(names):
            return ()
        fields = {name: value for name, value in zip(names, template.values, strict=True) if name}
        if "openapi" in fields or (
            "swagger" in fields and static_string(fields["swagger"]) != "2.0"
        ):
            return ()
        if any(name not in fields for name in _TEMPLATE_FIELDS):
            return ()
        host = static_string(fields["host"])
        base_path = static_string(fields["basePath"])
        schemes = fields["schemes"]
        if host is None or base_path is None or not isinstance(schemes, (ast.List, ast.Tuple)):
            return ()
        written = [static_string(element) for element in schemes.elts]
        if not written or None in written:
            return ()
        identity_at = next(
            key.lineno
            for key, name in zip(template.keys, names, strict=True)
            if key is not None and name == "host"
        )
        found: list[_Deployment] = []
        for scheme in dict.fromkeys(written):
            base = http.swagger2_base(scheme or "", host, base_path)
            if base is None:
                return ()
            found.append(_Deployment(base, FLASGGER_SWAGGER2, identity_at, call.lineno))
        return tuple(found)

    def deployment_references(self) -> list[ParsedReference]:
        """Each proven deployment, declared by the module that states it.

        Composed, as a Swagger 2 base always is: ``schemes``, ``host`` and
        ``basePath`` are three literals, usually on three lines.
        """
        found: list[ParsedReference] = []
        for receiver in self._receivers.values():
            for deployment in receiver.deployments:
                found.append(
                    http_reference(
                        EdgeType.DECLARES_HTTP_DEPLOYMENT,
                        EntityType.HTTP_DEPLOYMENT,
                        deployment.base,
                        deployment.identity_at,
                        deployment.identity_at,
                        source="flask",
                        composed=True,
                    )
                )
        return found

    def _read_constants(self, tree: ast.Module, counts: Counter[str]) -> None:
        for statement in tree.body:
            if (
                isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
            ):
                name, value = statement.targets[0].id, statement.value
            elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                name, value = statement.target.id, statement.value
            else:
                continue
            text = static_string(value)
            if text is not None and counts[name] == 1:
                self._constants[name] = (text, statement.lineno)
            default = self._env_default(value) if value is not None else None
            if default is not None and counts[name] == 1:
                self._env_defaults[name] = (default, statement.lineno)

    def _read_alternatives(self, tree: ast.Module, counts: Counter[str]) -> None:
        """Names one module-level ``if``/``elif``/``else`` binds to a literal in every branch.

        Each branch, ``else`` included, binds the name exactly once at its own top level to
        a static string, and nothing else in the module binds it; the condition is never
        read. Without an ``else`` the name may be left unbound, so it is not one of these.
        """
        for statement in tree.body:
            if not isinstance(statement, ast.If):
                continue
            branches: list[list[ast.stmt]] = []
            node = statement
            while True:
                branches.append(node.body)
                if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
                    node = node.orelse[0]
                elif node.orelse:
                    branches.append(node.orelse)
                    break
                else:
                    branches = []
                    break
            bound: list[dict[str, tuple[str, int] | None]] = []
            for body in branches:
                found: dict[str, tuple[str, int] | None] = {}
                for item in body:
                    if (
                        isinstance(item, ast.Assign)
                        and len(item.targets) == 1
                        and isinstance(item.targets[0], ast.Name)
                    ):
                        name, value = item.targets[0].id, item.value
                    elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                        name, value = item.target.id, item.value
                    else:
                        continue
                    text = static_string(value)
                    found[name] = None if name in found or text is None else (text, item.lineno)
                bound.append(found)
            if len(bound) < 2:
                continue
            for name in set.intersection(*(set(found) for found in bound)):
                values = [found[name] for found in bound]
                if counts[name] == len(bound) and all(value is not None for value in values):
                    self._alternatives[name] = tuple(value for value in values if value)

    def _env_default(self, node: ast.expr) -> str | None:
        """The literal default of ``os.environ.get("NAME", "<value>")`` or ``os.getenv(…)``.

        The whole default is the value whenever the environment does not set ``NAME``;
        ``os.environ["NAME"]``, or a default that is not literal, gives nothing.
        """
        if not isinstance(node, ast.Call) or any(isinstance(a, ast.Starred) for a in node.args):
            return None
        func = node.func
        getenv = self._resolve(func) == "os.getenv"
        environ_get = (
            isinstance(func, ast.Attribute)
            and func.attr == "get"
            and self._resolve(func.value) == "os.environ"
        )
        if not (getenv or environ_get) or static_string(_argument(node, 0, "key")) is None:
            return None
        return static_string(_argument(node, 1, "default"))

    # -- routes ---------------------------------------------------------------------

    def route_references(
        self, function: ast.FunctionDef | ast.AsyncFunctionDef, qualified: str
    ) -> list[ParsedReference]:
        """Routes a module-level function's decorators prove it serves.

        A route on an application with a literal path is served here. One on a router or
        blueprint, on a receiver this module imports, or with a path naming an imported
        constant, is also recorded as a :data:`PYTHON_ROUTE` claim, which the global pass
        serves wherever it proves a mount and the values.
        """
        found: list[ParsedReference] = []
        for decorator in function.decorator_list:
            if not isinstance(decorator, ast.Call) or _dynamic(decorator):
                continue
            func = decorator.func
            if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
                continue
            start, end = decorator.lineno, decorator.end_lineno or decorator.lineno
            receiver = self._receivers.get(func.value.id)
            if receiver is None:
                found.extend(self._imported_route(func.value, func.attr, decorator, qualified))
                continue
            if not receiver.usable:
                continue
            declared = self._declared(receiver, func.attr, decorator)
            if declared is None:
                continue
            methods, path_node = declared
            source = "fastapi" if receiver.kind in {_FASTAPI_APP, _FASTAPI_ROUTER} else "flask"
            path = static_string(path_node)
            if path is None or receiver.kind not in _APPLICATIONS:
                parts = [["t", path]] if path is not None else self._path_parts(path_node)
                if parts is not None:
                    found.extend(
                        _route_claim(
                            {"router": func.value.id, "family": source},
                            methods,
                            parts,
                            (start, end),
                            qualified,
                        )
                    )
            if path is None:
                continue
            for served, composed, owners in self._served(func.value.id, receiver, path):
                for method in methods:
                    found.append(
                        http_reference(
                            EdgeType.SERVES_HTTP_ROUTE,
                            EntityType.HTTP_ROUTE,
                            http.route_body(method, served),
                            start,
                            end,
                            source=source,
                            composed=composed,
                            source_symbol=qualified,
                        )
                    )
                    found.extend(
                        self._exposures(owners, method, served, start, end, source, qualified)
                    )
        return found

    def _exposures(
        self,
        owners: tuple[str, ...],
        method: str,
        served: str,
        start: int,
        end: int,
        source: str,
        qualified: str,
    ) -> list[ParsedReference]:
        """Where a served route is published: a fact and the claim that proves it.

        The fact is composed — the base and the route are written apart — so it has no
        lines. The claim keeps every line the join cites: the decorator's own span,
        and, in its meta, the identity's and the binding's.
        """
        found: list[ParsedReference] = []
        for owner in owners:
            receiver = self._receivers.get(owner)
            for deployment in receiver.deployments if receiver is not None else ():
                body = http.endpoint_body(method, http.compose(deployment.base, served))
                if http.canonical_endpoint(body) is None:
                    continue
                found.append(
                    http_reference(
                        EdgeType.EXPOSES_HTTP_ENDPOINT,
                        EntityType.HTTP_ENDPOINT,
                        body,
                        start,
                        end,
                        source=source,
                        composed=True,
                        source_symbol=qualified,
                    )
                )
                found.append(
                    pending_reference(
                        EdgeType.EXPOSES_HTTP_ENDPOINT,
                        EntityType.HTTP_ENDPOINT,
                        body,
                        start,
                        end,
                        role=HTTP_EXPOSED_ENDPOINT,
                        source_symbol=qualified,
                        method=method,
                        route=served,
                        deployment=deployment.base,
                        identity_source=deployment.source,
                        identity_at=str(deployment.identity_at),
                        bound_at=str(deployment.bound_at),
                        source=source,
                    )
                )
        return found

    def _imported_route(
        self, receiver: ast.Name, attribute: str, decorator: ast.Call, qualified: str
    ) -> list[ParsedReference]:
        """A route on a receiver this module imports: ``from . import main``, ``@main.route``."""
        ref = _module_ref(receiver, self._imported)
        family = _family(attribute, decorator)
        if ref is None or family is None:
            return []
        named = "path" if family[0] == "fastapi" else "rule"
        parts = self._path_parts(_argument(decorator, 0, named))
        if parts is None and family[0] == "either":
            # A verb shortcut names its path `path` in FastAPI and `rule` in Flask.
            parts = self._path_parts(_argument(decorator, 0, "path"))
        if parts is None:
            return []
        span = (decorator.lineno, decorator.end_lineno or decorator.lineno)
        meta = {"module": ref[0], "router": ref[1], "family": family[0]}
        return _route_claim(meta, family[1], parts, span, qualified)

    def _path_parts(self, node: ast.expr | None, depth: int = 0) -> list[list[str]] | None:
        """A path as literal text and imported names: ``f"/{PREFIX}/{API_VERSION}" + "/x"``.

        Each part is ``["t", text]`` or ``["r", module, name]`` for a name some module this
        one imports must bind to a string constant. This module's own constants are read
        here; anything else -- a call, an attribute of an object, ``%``, ``.format``, a
        conversion or format spec -- is not a path this can read.
        """
        if node is None or depth > _MAX_STRING_DEPTH:
            return None
        if isinstance(node, ast.Constant):
            return [["t", node.value]] if isinstance(node.value, str) else None
        if isinstance(node, ast.JoinedStr):
            parts: list[list[str]] = []
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    parts.append(["t", value.value])
                elif (
                    isinstance(value, ast.FormattedValue)
                    and value.conversion == -1
                    and value.format_spec is None
                ):
                    named = self._named_part(value.value)
                    if named is None:
                        return None
                    parts.append(named)
                else:
                    return None
            return parts
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = self._path_parts(node.left, depth + 1)
            right = self._path_parts(node.right, depth + 1) if left is not None else None
            return None if left is None or right is None else left + right
        named = self._named_part(node)
        return [named] if named is not None else None

    def _named_part(self, node: ast.expr) -> list[str] | None:
        if isinstance(node, ast.Name) and node.id in self._constants:
            return ["t", self._constants[node.id][0]]
        ref = _module_ref(node, self._imported)
        return ["r", ref[0], ref[1]] if ref is not None else None

    def claim_references(self) -> list[ParsedReference]:
        """What only the global pass can decide: receivers, mounts, offered constants."""
        found: list[ParsedReference] = []
        for name, receiver in self._receivers.items():
            found.append(
                pending_reference(
                    EdgeType.REFERENCES,
                    EntityType.SYMBOL,
                    name,
                    receiver.line,
                    receiver.line,
                    role=PYTHON_RECEIVER,
                    kind=receiver.kind,
                    prefix=json.dumps(receiver.prefix),
                    usable="true" if receiver.usable else "false",
                    mounts=json.dumps([mount for mount, _ in receiver.mounts]),
                )
            )
        found.extend(self._mount_claims)
        if self._script is not None:
            found.append(
                pending_reference(
                    EdgeType.REFERENCES,
                    EntityType.CONFIG,
                    PYTHON_SCRIPT,
                    self._script,
                    self._script,
                    role=PYTHON_SCRIPT,
                )
            )
        offered = sorted(
            (line, name, value)
            for name, (value, line) in self._constants.items()
            if _path_like(value)
        )[:_MAX_CONSTANTS]
        if offered:
            found.append(
                pending_reference(
                    EdgeType.REFERENCES,
                    EntityType.CONFIG,
                    PYTHON_CONSTANTS,
                    offered[0][0],
                    offered[-1][0],
                    role=PYTHON_CONSTANTS,
                    constants=json.dumps(
                        {name: [value, line] for line, name, value in offered}, sort_keys=True
                    ),
                )
            )
        return found

    @staticmethod
    def _declared(
        receiver: _Receiver, attribute: str, decorator: ast.Call
    ) -> tuple[tuple[str, ...], ast.expr | None] | None:
        keywords = _keywords(decorator)
        if receiver.kind in {_FASTAPI_APP, _FASTAPI_ROUTER}:
            path_node = _argument(decorator, 0, "path")
            if attribute in _FASTAPI_VERBS and "methods" not in keywords:
                methods: tuple[str, ...] | None = (_FASTAPI_VERBS[attribute],)
            elif attribute == "api_route" and "methods" in keywords:
                # Only with the methods spelled out: the default is the
                # framework's to decide, and this reader does not assume it.
                methods = _static_methods(keywords["methods"])
            else:
                return None
        else:
            path_node = _argument(decorator, 0, "rule")
            if attribute == "route":
                # Flask's documented default is GET. HEAD and OPTIONS are added
                # by the framework implicitly and are not recorded as declared.
                methods = (
                    _static_methods(keywords["methods"]) if "methods" in keywords else ("GET",)
                )
            elif attribute in _FLASK_VERBS and "methods" not in keywords:
                methods = (_FLASK_VERBS[attribute],)
            else:
                return None
        if methods is None or path_node is None:
            return None
        return methods, path_node

    @staticmethod
    def _served(
        name: str, receiver: _Receiver, path: str
    ) -> list[tuple[str, bool, tuple[str, ...]]]:
        """Every path this route is served at, whether it was composed, and by which apps."""
        if receiver.kind in {_FASTAPI_APP, _FLASK_APP}:
            served = http.route_path(path)
            return [(served, False, (name,))] if served is not None else []
        found: dict[str, list[str]] = {}
        for mount, owner in dict.fromkeys(receiver.mounts):
            if receiver.kind == _FASTAPI_ROUTER:
                if path and not path.startswith("/"):
                    continue
                full: str | None = f"{mount or ''}{receiver.prefix or ''}{path}"
            else:
                full = flask_join(mount if mount is not None else receiver.prefix, path)
            served = http.route_path(full) if full else None
            if served is not None:
                owners = found.setdefault(served, [])
                if owner not in owners:
                    owners.append(owner)
        return [(served, True, tuple(owners)) for served, owners in found.items()]

    # -- client calls ---------------------------------------------------------------

    def call_references(self, call: ast.Call, source_symbol: str | None) -> list[ParsedReference]:
        """The request a ``requests`` or ``httpx`` call provably makes: a module-level function,
        or a method of a client object whose binding is lexical."""
        client = self._client_of(call)
        if client is not None:
            return self._client_references(call, client, source_symbol)
        resolved = self._resolve(call.func)
        if resolved is None:
            return []
        library, _, function = resolved.partition(".")
        if library not in _CLIENT_LIBRARIES or any(
            isinstance(arg, ast.Starred) for arg in call.args
        ):
            return []
        request = self._request_of(call, function)
        if request is None:
            return []
        method, url_node, end = request
        url = static_string(url_node)
        if url is None:
            # Not literal: at most a claim for the join, never a per-file fact.
            return self._read_claims(
                url_node, method, call.lineno, end, library, source_symbol, unread_host=True
            )
        target = http.client_target(url)
        # Neither library resolves a relative URL: it names no request at all.
        if target is None or target.url is None or target.origin is None:
            return []
        return self._literal_references(target, method, call.lineno, end, library, source_symbol)

    @staticmethod
    def _request_of(call: ast.Call, function: str) -> tuple[str, ast.expr, int] | None:
        """The method, the URL's expression and the last line of a client's request call."""
        method_node: ast.expr | None = None
        if function == "request":
            method_node = _argument(call, 0, "method")
            url_node = _argument(call, 1, "url")
            raw = static_string(method_node)
            # Both libraries upper-case the method they are given.
            method = http.http_method(raw.upper()) if raw is not None else None
        elif function in _CLIENT_VERBS:
            url_node = _argument(call, 0, "url")
            method = _CLIENT_VERBS[function]
        else:
            return None
        if method is None or url_node is None:
            return None
        end = max(
            node.end_lineno or node.lineno for node in (url_node, method_node) if node is not None
        )
        return method, url_node, end

    def _literal_references(
        self,
        target: http.HttpTarget,
        method: str,
        start: int,
        end: int,
        library: str,
        source_symbol: str | None,
        client: _ClientInstance | None = None,
    ) -> list[ParsedReference]:
        """The facts and the claim of a request to a literal absolute URL."""
        if target.url is None or target.origin is None:
            return []
        literal = _ClientUrl(target.origin, tuple(target.path[1:].split("/")), target.url)
        return [
            http_reference(
                EdgeType.CALLS_HTTP_ENDPOINT,
                EntityType.HTTP_ENDPOINT,
                http.endpoint_body(method, target.url),
                start,
                end,
                source=library,
                composed=False,
                source_symbol=source_symbol,
            ),
            http_reference(
                EdgeType.CALLS_HTTP_ROUTE,
                EntityType.HTTP_ROUTE,
                http.route_body(method, target.path),
                start,
                end,
                source=library,
                composed=False,
                source_symbol=source_symbol,
            ),
            self._client_claim(literal, method, start, end, library, source_symbol, client=client),
        ]

    def _read_claims(
        self,
        url_node: ast.expr,
        method: str,
        start: int,
        end: int,
        library: str,
        source_symbol: str | None,
        *,
        unread_host: bool,
        client: _ClientInstance | None = None,
    ) -> list[ParsedReference]:
        """The claims of a request whose URL is not a literal: through a constant, an
        environment default or an f-string on a known origin, and -- where ``unread_host`` --
        on a host the source does not state (D2)."""
        alternatives = self._alternative_urls(url_node)
        if alternatives:
            # One claim per literal, each decided on its own: none overwrites another.
            return [
                self._client_claim(
                    read, method, start, end, library, source_symbol, len(alternatives), client
                )
                for read in alternatives
            ]
        read = self._client_url(url_node)
        if read is None and unread_host:
            read = self._unknown_base(url_node)
        if read is None:
            return []
        claim = self._client_claim(read, method, start, end, library, source_symbol, client=client)
        return [claim]

    @staticmethod
    def _client_claim(
        read: _ClientUrl,
        method: str,
        start: int,
        end: int,
        library: str,
        source_symbol: str | None,
        alternatives: int | None = None,
        client: _ClientInstance | None = None,
        base_expr: str | None = None,
    ) -> ParsedReference:
        meta = {
            "method": method,
            "segments": json.dumps(list(read.segments)),
            "source": library,
        }
        if alternatives is not None:
            meta["alternatives"] = str(alternatives)
        if read.origin is None:
            meta["base"] = BASE_UNKNOWN
        else:
            meta["origin"] = read.origin
        if read.base_at is not None:
            meta["base_at"] = str(read.base_at)
        if client is not None:
            # The object the request is made on: where it is bound, for a reader to find.
            meta["client"] = client.name
            meta["client_at"] = str(client.bound_at)
        if base_expr is not None:
            meta["base_expr"] = base_expr
        return pending_reference(
            EdgeType.CALLS_HTTP_ENDPOINT,
            EntityType.HTTP_ENDPOINT,
            f"{method} {read.display}",
            start,
            end,
            role=HTTP_CLIENT_CALL,
            source_symbol=source_symbol,
            **meta,
        )

    # -- client objects -------------------------------------------------------------

    def _read_clients(self, tree: ast.Module, counts: Counter[str]) -> None:
        """Client objects the module binds, each to a name nothing else in it binds.

        ``client = httpx.Client(base_url=...)`` or ``with requests.Session() as session``.
        A name bound twice, anywhere -- a parameter, a loop variable, an import -- names no
        one object. Past :data:`_MAX_INSTANCES` none is read. An attribute stored on the
        name (``client.base_url = ...``) or a read of its ``event_hooks`` or ``hooks``
        discards the object wherever it happens: either can change where a request goes. So
        does any other read of the name (``configure(client)``): it can escape.
        """
        libraries = {
            *self._modules.values(),
            *(name.partition(".")[0] for name in self._names.values()),
        }
        if not libraries & _CLIENT_LIBRARIES:
            return
        found: dict[str, _ClientInstance] = {}
        for name, value, bound_at, reach in _constructions(tree):
            client = self._client_instance(name, value, bound_at, reach, counts)
            if client is not None:
                found[name] = client
        if len(found) > _MAX_INSTANCES:
            return
        refuted = {
            node.value.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in found
            and (not isinstance(node.ctx, ast.Load) or node.attr in _HOOK_ATTRIBUTES)
        }
        # A name read any other way (``configure(session)``, ``return client``) can escape, and
        # whatever receives the object may change where it sends.
        attribute_objects = {
            id(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
        }
        refuted |= {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in found
            and id(node) not in attribute_objects
        }
        self._clients = {name: client for name, client in found.items() if name not in refuted}

    def _client_instance(
        self,
        name: str,
        value: ast.expr,
        bound_at: int,
        reach: tuple[int, int, int, int] | None,
        counts: Counter[str],
    ) -> _ClientInstance | None:
        """The client object ``value`` constructs, or ``None`` when it is not provably one."""
        if counts[name] != 1 or len(name) > _MAX_EXPRESSION or not isinstance(value, ast.Call):
            return None
        constructor = self._resolve(value.func)
        if constructor in _SESSION_CONSTRUCTORS:
            if value.args or value.keywords:
                return None
            return _ClientInstance(
                _SESSION, _NO_BASE, None, None, None, value.lineno, name, bound_at, reach
            )
        if constructor not in _HTTPX_CONSTRUCTORS or value.args or _dynamic(value):
            return None
        keywords = _keywords(value)
        if _REDIRECTING_KEYWORDS & keywords.keys():
            return None
        base: _ClientBase | None = (_NO_BASE, None, None, None)
        if "base_url" in keywords:
            base = self._client_base(keywords["base_url"])
        if base is None:
            return None
        kind, text, base_at, expression = base
        return _ClientInstance(
            _HTTPX, kind, text, base_at, expression, value.lineno, name, bound_at, reach
        )

    def _client_base(self, node: ast.expr) -> _ClientBase | None:
        """What the source states of a client's ``base_url``: its kind, the base, its line and
        (for an unstated one) its expression; ``None`` for a base that makes the client unreadable.

        A literal, a module constant bound once, or an environment default is stated -- as
        a request's own URL is -- and must be an absolute URL with no query, fragment or
        credentials. An empty one is no base at all, unless the environment may set it. Any
        other expression is a base the source does not state.
        """
        from_environment = False
        text = static_string(node)
        stated: tuple[str, int] | None = (text, node.lineno) if text is not None else None
        if stated is None and isinstance(node, ast.Name) and node.id in self._constants:
            stated = self._constants[node.id]
        elif stated is None:
            stated = self._stated(node)
            from_environment = True
        if stated is None:
            return _UNSTATED_BASE, None, node.lineno, _base_expression(node)
        text, line = stated
        if text == "":
            if from_environment:
                return _UNSTATED_BASE, None, line, _base_expression(node)
            return _NO_BASE, None, None, None
        return (_STATED_BASE, text, line, None) if self._base_url(text) else None

    @staticmethod
    def _base_url(text: str) -> bool:
        """Whether a base is an absolute URL a request path can be appended to, as written."""
        if http.deployment_base(text) is None:
            return False
        target = http.client_target(text)
        return (
            target is not None
            and target.origin is not None
            and "//" not in target.path
            and not _dotted(target.path.split("/"))
        )

    def _client_of(self, call: ast.Call) -> _ClientInstance | None:
        """The client object ``name.method(...)`` is made on, where the name's binding holds."""
        func = call.func
        if not (
            self._clients and isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
        ):
            return None
        client = self._clients.get(func.value.id)
        return client if client is not None and client.reaches(func.value) else None

    def _client_references(
        self, call: ast.Call, client: _ClientInstance, source_symbol: str | None
    ) -> list[ParsedReference]:
        """The request a call on a client object provably makes.

        A literal absolute URL goes where it says, whatever the base. A rooted one is
        appended to the base the source states, or is a request on a host it does not
        (D2); on a client with no base it names no destination at all, and neither does a
        URL with no leading ``/`` that is not an origin the source states.
        """
        function = call.func.attr if isinstance(call.func, ast.Attribute) else ""
        if any(isinstance(arg, ast.Starred) for arg in call.args):
            return []
        request = self._request_of(call, function)
        if request is None:
            return []
        method, url_node, end = request
        library = client.library
        url = static_string(url_node)
        if url is not None:
            target = http.client_target(url)
            if target is not None and target.url is not None and target.origin is not None:
                return self._literal_references(
                    target, method, call.lineno, end, library, source_symbol, client
                )
        parts = self._rooted_parts(url_node)
        if parts is not None:
            return self._composed(client, parts, method, call.lineno, end, source_symbol)
        if url is not None:
            return []
        # A host the source does not state is read only where a relative URL could not be
        # meant: a client with a base would join it, a client with none would refuse it.
        return self._read_claims(
            url_node,
            method,
            call.lineno,
            end,
            library,
            source_symbol,
            unread_host=client.base_kind == _NO_BASE,
            client=client,
        )

    @staticmethod
    def _rooted_parts(node: ast.expr) -> list[list[str]] | None:
        """A URL's parts when it begins with one literal ``/``: ``"/users"``, ``f"/users/{uid}"``
        or ``"/users/" + uid``. ``//`` names a host, so it is not rooted."""
        if isinstance(node, ast.JoinedStr):
            operands: list[ast.expr] = list(node.values)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            added = _added(node)
            if added is None:
                return None
            operands = added
        else:
            operands = [node]
        head = operands[0] if operands else None
        if not (
            isinstance(head, ast.Constant)
            and isinstance(head.value, str)
            and head.value.startswith("/")
            and not head.value.startswith("//")
        ):
            return None
        tail = _pieces(operands[1:])
        return None if tail is None else [[http.TEMPLATE_LITERAL, head.value], *tail]

    def _composed(
        self,
        client: _ClientInstance,
        parts: list[list[str]],
        method: str,
        start: int,
        end: int,
        source_symbol: str | None,
    ) -> list[ParsedReference]:
        """A rooted request on a client that has a base.

        Appended to a base the source states, as ``httpx`` does (its trailing ``/`` is not a
        second one): a claim with the origin and the base's line, never a per-file fact.
        On a base it does not state, the host part is the synthetic ``{name.base_url}``: a
        claim with no origin, which the join counts and never links.
        """
        origin: str | None = None
        host = "{" + f"{client.name}.base_url" + "}"
        if client.base_kind == _STATED_BASE and client.base_text is not None:
            base = http.client_target(client.base_text)
            if base is None or base.origin is None:
                return []
            origin = host = base.origin
            parts = [[http.TEMPLATE_LITERAL, base.path.rstrip("/") + parts[0][1]], *parts[1:]]
        elif client.base_kind != _UNSTATED_BASE:
            return []
        template = http.route_template(parts)
        if isinstance(template, str) or _dotted(template.segments):
            return []
        display = f"{host}/{_display(template)}"
        read = _ClientUrl(origin, template.segments, display, client.base_at)
        claim = self._client_claim(
            read,
            method,
            start,
            end,
            client.library,
            source_symbol,
            client=client,
            base_expr=client.base_expr,
        )
        return [claim]

    def _client_url(self, node: ast.expr) -> _ClientUrl | None:
        """A URL named by a module constant or an environment default, or an f-string on
        a known origin."""
        if isinstance(node, ast.Name | ast.Call):
            stated = self._stated(node)
            target = http.client_target(stated[0]) if stated is not None else None
            if stated is None or target is None or target.url is None or target.origin is None:
                return None
            segments = tuple(target.path[1:].split("/"))
            return _ClientUrl(target.origin, segments, target.url, stated[1])
        if isinstance(node, ast.JoinedStr):
            return self._template_url(node)
        return None

    def _alternative_urls(self, node: ast.expr) -> list[_ClientUrl]:
        """The URL under each literal alternative of the one alternated name it uses.

        ``[]`` unless the URL uses exactly one such name and reads as a URL with an origin
        under every one of its alternatives; the request is then read as before.
        """
        names = {
            found.id
            for found in ast.walk(node)
            if isinstance(found, ast.Name) and found.id in self._alternatives
        }
        if len(names) != 1:
            return []
        name = names.pop()
        reads: list[_ClientUrl] = []
        for alternative in self._alternatives[name]:
            self._choice = {name: alternative}
            try:
                read = self._client_url(node)
            finally:
                self._choice = {}
            if read is None or read.origin is None:
                return []
            reads.append(read)
        return reads

    def _stated(self, node: ast.expr) -> tuple[str, int] | None:
        """A string the source states for a URL or its base: a module constant, an
        environment default, a constant bound to one, or the literal alternative a request
        is being read under; with its line."""
        if isinstance(node, ast.Name):
            return (
                self._choice.get(node.id)
                or self._constants.get(node.id)
                or self._env_defaults.get(node.id)
            )
        default = self._env_default(node)
        return (default, node.lineno) if default is not None else None

    def _unknown_base(self, node: ast.expr) -> _ClientUrl | None:
        """A request whose path is read but whose host is not (D2): ``None`` otherwise.

        ``f"{base}/users/{uid}"``, ``f"http://{host}/users"`` or ``base + "/users/" + uid``:
        the host part is an unread value -- or an empty stated one -- and the text after
        it starts with ``/``, each later value filling one whole segment. A base the
        source does state, refused for anything else (glued to the next text, say),
        stays unread.
        """
        if isinstance(node, ast.JoinedStr):
            values: list[ast.expr] = list(node.values)
            prefix = ""
            if (
                len(values) > 1
                and isinstance(values[0], ast.Constant)
                and isinstance(values[0].value, str)
                and _SCHEME_ONLY.fullmatch(values[0].value)
            ):
                prefix, values = values[0].value, values[1:]
            first = values[0] if values else None
            if (
                not isinstance(first, ast.FormattedValue)
                or first.conversion != -1
                or first.format_spec is not None
            ):
                return None
            head, rest = first.value, values[1:]
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            operands = _added(node)
            if operands is None:
                return None
            head, rest, prefix = operands[0], operands[1:], ""
        else:
            return None
        if not self._unread_base(head):
            return None
        parts = _pieces(rest)
        if parts is None:
            return None
        template = http.route_template(parts)
        if isinstance(template, str):
            return None
        base = "{" + ast.unparse(head)[:_MAX_EXPRESSION] + "}"
        return _ClientUrl(None, template.segments, f"{prefix}{base}/{_display(template)}")

    def _unread_base(self, node: ast.expr) -> bool:
        """Whether a URL's host part is a value the source does not state (or states empty)."""
        if isinstance(node, ast.Constant | ast.JoinedStr):
            return False
        stated = self._stated(node) if isinstance(node, ast.Name | ast.Call) else None
        return stated is None or stated[0] == ""

    def _template_url(self, node: ast.JoinedStr) -> _ClientUrl | None:
        """``f"https://host/users/{uid}"`` or ``f"{BASE}/users/{uid}"``, or ``None``.

        The text is concatenated exactly as the f-string would concatenate it: a base
        constant is appended to, never resolved against. The origin must be entirely
        literal; every placeholder after it goes through :func:`http.route_template`,
        which accepts one only as a whole path segment.
        """
        values = list(node.values)
        head, base_at = "", None
        first = values[0] if values else None
        if isinstance(first, ast.FormattedValue):
            if not (
                isinstance(first.value, ast.Name)
                and first.conversion == -1
                and first.format_spec is None
            ):
                return None
            constant = self._stated(first.value)
            if constant is None:
                return None
            head, base_at = constant
            values = values[1:]
            following = values[0] if values else None
            if not head.endswith("/") and not (
                isinstance(following, ast.Constant)
                and isinstance(following.value, str)
                and following.value.startswith("/")
            ):
                return None
        while values and isinstance(values[0], ast.Constant):
            text = values[0].value
            if not isinstance(text, str):
                return None
            head += text
            values = values[1:]
        scheme = head.find("://")
        slash = head.find("/", scheme + 3) if scheme >= 0 else -1
        if slash < 0:
            return None
        origin = http.client_target(head[:slash] + "/")
        if origin is None or origin.origin is None:
            return None
        parts: list[list[str]] = [[http.TEMPLATE_LITERAL, head[slash:]]]
        for value in values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append([http.TEMPLATE_LITERAL, value.value])
            elif (
                isinstance(value, ast.FormattedValue)
                and value.conversion == -1
                and value.format_spec is None
            ):
                parts.append([http.TEMPLATE_SUBSTITUTION, ast.unparse(value.value)])
            else:
                return None
        template = http.route_template(parts)
        if isinstance(template, str):
            return None
        display = _display(template)
        return _ClientUrl(origin.origin, template.segments, f"{origin.origin}/{display}", base_at)
