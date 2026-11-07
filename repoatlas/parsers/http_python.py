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

**Values are proven or they are nothing.** The per-file fact of a route is read from
:func:`static_string`, which accepts a string literal, a concatenation of literals, and an
f-string with no placeholders. A route path, a router's or blueprint's prefix and a mount's
prefix are also read where a module constant composes them -- ``USERS = f"{BASE}/users"``,
``P = BASE + "/v1"`` -- from literals, earlier module constants and, in a path, imported
constants. The name must be this module's only binding of it, bound before the line that reads
it; a call, an environment variable, ``.format``, ``%`` and a conversion or format spec are not
read, and neither is a prefix naming an imported constant. Every refusal has an internal
reason, never stored. ``f"{base}/hello/me"`` with an unproven ``base`` is not accepted -- not
even its suffix: whatever ``base`` holds decides the final path (``https://x`` gives
``/hello/me``, ``https://x/api`` gives ``/api/hello/me``), so a known tail proves no route at
all. A composed path is a claim for the global pass; only a literal one is served here.

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
states, and that view stays. An application another file mounts at a literal FastAPI
prefix (``parent.mount("/sub", child)``, the child imported, or one name away from an
import: ``alias = child``) is also served under it: each route of the child with a
literal path is recorded again as a claim (``served_here``) and the mount claim carries
``mount_prefix``, so that the global pass adds the parent's view -- the prefix and the
path, the parent as the application that serves it -- beside the child's own. An empty
prefix, one without its leading ``/`` or with a trailing one, or one built from an
imported constant or a call, proves no mount path.

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
Only the per-file facts above stay literal-only. The URL (and an ``httpx`` client's
``base_url``) may also be built from module constants by an f-string or ``+``, within this
file, on the evaluator the route paths use: a literal constant anywhere in the host, the port
or the head, and a constant composed from earlier ones (``BASE = f"{HOST}/api"``), which must be
bound before the line that reads it. The text is joined exactly as written -- nothing is
inserted or trimmed, so ``BASE`` ending in ``/`` plus ``"/users"`` is ``//users`` -- and read
as scheme, host, port and path: every boundary where a value meets other text must be a place
a URL separates (``://``, ``.``, ``:``, ``/``), or the read is refused. A name filling the
path is spliced only when its own text starts with ``/``; any other stays a placeholder, as a
token must never become a path. A host that is a value (``f"https://{host}.example.com/x"``)
keeps the path after it, with no origin: counted, never joined. A read that is refused for any
reason, or that does not improve on the older one, leaves the older output exactly as it was;
the reason is recorded in ``client_reads`` and never emitted. The composed text is never
written to ``_constants`` or ``_values``, and an imported or cross-file name stays a placeholder.

**A subclass of the framework's application, router or blueprint** constructs a receiver of its
base's kind, where nothing in the class can change what it registers or serves. The class stands
at module level and has one base: ``fastapi.FastAPI``, ``fastapi.APIRouter``, ``flask.Flask`` or
``flask.Blueprint`` as this module's own import names it (``from fastapi import APIRouter [as R]``
or ``fastapi.APIRouter`` on ``import fastapi [as m]``), or a class of this kind defined earlier in
the same module, at most three deep. An alias (``Base = APIRouter``), another module's class,
``fastapi.routing.APIRouter`` and a relative import are not followed, and a familiar name is never
enough. No metaclass, keyword, type parameter or class decorator; the class name bound once; a body
of ``pass``, a docstring and methods, none of which is a special method, a private member the
framework serves through or one of its registration members, decorated by names or attributes only
if at all; and an ``__init__`` that is exactly ``super().__init__(*args, **kwargs)``, or one that
calls the base's once and touches nothing the framework serves through
(:meth:`PythonHttp._custom_init`, held to :func:`init_fits` for a router or blueprint), in a module
with no star import. The class
name may only be called, be a base or stand in an annotation: any other read (``C.get = f``,
``alias = C``) could change it. Every refusal has an internal reason in ``class_refusals``, never
stored. Only the same module's classes are followed here. Every subclass is offered to the global
pass as a :data:`PYTHON_CLASS` claim, with its ``__init__`` facts -- one extending another module's
class too, naming that class; a module that builds a receiver from a class it imports --
``app = CustomApp()`` on ``from base import CustomApp``, or ``app = App()`` for a class ``App``
that extends it, the import above the class or the construction, the imported name read only to
extend or construct -- records the routes on it as :data:`PYTHON_CLASS_ROUTE` claims, with what the
construction hands the class, which the global pass serves when the defining module offers the
class, total depth included: at the root of an application, or as a router's or blueprint's routes.
Such a class is kept in ``imported_subclasses`` and ``imported_class_refusals``, never in
``subclasses`` and ``class_refusals``.

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
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field

from repoatlas.models import http
from repoatlas.models.parsing import EdgeType, EntityType, ParsedReference
from repoatlas.parsers.http_facts import (
    BASE_UNKNOWN,
    COMPOSED,
    EXACT,
    HELPER_CALL_ROLE,
    HELPER_ROLE,
    HTTP_CLIENT_CALL,
    HTTP_EXPOSED_ENDPOINT,
    PENDING_FACT,
    PYTHON_CLASS,
    PYTHON_CLASS_ROUTE,
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
#: How many classes of this module a subclass of a constructor may stand behind.
_MAX_CLASS_DEPTH = 3
#: What a subclass may not define: a member the framework itself reads to register or serve.
_ROUTING_MEMBERS = frozenset(
    {
        "get",
        "put",
        "post",
        "delete",
        "options",
        "head",
        "patch",
        "trace",
        "api_route",
        "add_api_route",
        "api_websocket_route",
        "add_api_websocket_route",
        "websocket",
        "websocket_route",
        "add_websocket_route",
        "route",
        "add_route",
        "include_router",
        "mount",
        "host",
        "url_path_for",
        "add_url_rule",
        "register_blueprint",
        "register",
        "record",
        "record_once",
        "make_setup_state",
        "endpoint",
        "app",
        "wsgi_app",
        "dispatch_request",
        "full_dispatch_request",
        "create_url_adapter",
        "build_middleware_stack",
        "setup",
        "url_rule_class",
        "url_map_class",
        "url_map",
        "route_class",
        "routes",
        "prefix",
        "url_prefix",
        "router",
    }
)
#: Private members the frameworks register or serve through: a subclass may not define them.
_PRIVATE_MEMBERS = frozenset({"_method_route", "_check_setup_finished", "_merge_blueprint_funcs"})
#: What a subclass's ``__init__`` may not read or set on its instance, and no property may be:
#: what the framework registers or serves through.
_SERVING_ATTRIBUTES = _ROUTING_MEMBERS | frozenset(
    {
        "redirect_slashes",
        "default",
        "middleware_stack",
        "user_middleware",
        "root_path",
        "subdomain",
        "url_defaults",
        "static_folder",
        "static_url_path",
    }
)
#: The keywords of a router's or blueprint's constructor that change where its routes are served.
_PATH_KEYWORDS = {
    _FASTAPI_ROUTER: frozenset({"prefix", "routes", "route_class", "redirect_slashes", "default"}),
    _FLASK_BLUEPRINT: frozenset({"url_prefix", "subdomain", "url_defaults"}),
}
#: The positional arguments a router's or blueprint's constructor takes before one changes a path.
_PATH_POSITIONAL = {_FASTAPI_ROUTER: 0, _FLASK_BLUEPRINT: 5}
#: The kind of application a mounting call is made on, when its class is not known here.
_CALL_OWNERS = {
    "include_router": _FASTAPI_APP,
    "mount": _FASTAPI_APP,
    "register_blueprint": _FLASK_APP,
}
#: The keyword a construction states a router's or blueprint's own prefix by.
_PREFIX_KEYWORDS = {_FASTAPI_ROUTER: "prefix", _FLASK_BLUEPRINT: "url_prefix"}
#: Why a module-level class derived from a constructor builds no receiver: internal, for tests
#: and measurement.
_MULTIPLE_BASES = "multiple-bases"
_DYNAMIC_BASE = "dynamic-base"
_METACLASS = "metaclass"
_CLASS_KEYWORDS = "class-keywords"
_TYPE_PARAMS = "type-params"
_CLASS_DECORATED = "class-decorated"
_CLASS_REBOUND = "class-rebound"
_BASE_REFUSED = "base-refused"
_DEPTH_EXCEEDED = "depth-exceeded"
_CLASS_BODY_STATEMENT = "class-body-statement"
_DECORATED_MEMBER = "decorated-member"
_INIT_WITH_EFFECT = "overridden-init-with-effect"
_SPECIAL_METHOD = "overridden-special-method"
_ROUTE_METHOD = "overridden-route-method"
_CLASS_ESCAPES = "class-escapes"
#: A class of another module is no receiver's base unless it is imported before the statement.
_IMPORTED_LATE = "imported-after-class"
#: The kind in an ``_Origin`` whose base is a class another module defines: its kind is not
#: known here.
_IMPORTED = "imported"
#: The kind a :data:`PYTHON_CLASS` claim states for a class whose base another module defines.
IMPORTED_CLASS = "imported"
#: What a base resolves to: its kind, its depth and (for a class of this module) its name.
_Origin = tuple[str, int, str | None]

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

#: Why a registration or a call of a same-module helper is not read (R2c-1): stable codes of the
#: diagnostics (:mod:`repoatlas.graph.http_provider_diagnostics`). A refusal is recorded in the
#: claim of a helper that has something to record, never in a claim of its own.
PY_HELPER_CONTROL_FLOW = "python-helper-control-flow"
PY_HELPER_ARGUMENT = "python-helper-argument"
PY_HELPER_REBOUND_OR_ALIAS = "python-helper-rebound-or-alias"
PY_HELPER_SHADOWED = "python-helper-shadowed"
PY_HELPER_CHAIN_TOO_DEEP = "python-helper-chain-too-deep"
PY_HELPER_BEFORE_CONSTRUCTION = "python-helper-before-construction"
PY_HELPER_BEFORE_BINDING = "python-helper-before-binding"
PY_HELPER_UNRESOLVED = "python-helper-unresolved"
PY_HELPER_CHILD_UNSUPPORTED = "python-helper-child-unsupported"
#: The pending-fact keys a helper's mount keeps from the mount claim it is made as; its owner is
#: the application a call of the helper hands over, not these.
_HELPER_OWNER_KEYS = frozenset({PENDING_FACT, "owner", "owner_kind"})
#: A comprehension statement is looked at only when it calls one of these: a mount.
_COMPREHENSION_MOUNTS = frozenset({"include_router", "register_blueprint", "mount"})
#: What became of a comprehension statement that mounts: internal, for tests and measurement.
_COMPREHENSION_UNROLLED = "unrolled"
_COMPREHENSION_GENERATOR = "generator-not-eager"
_COMPREHENSION_DICT = "dict-comprehension"
_COMPREHENSION_MULTIPLE = "multi-generator"
_COMPREHENSION_ASYNC = "async-comprehension"
_COMPREHENSION_FILTERED = "conditional-comprehension"
_COMPREHENSION_TARGET = "unsupported-target"
_COMPREHENSION_REBOUND = "loop-variable-rebound"
_COMPREHENSION_IMPORTED = "collection-imported"
_COMPREHENSION_DYNAMIC = "dynamic-collection"
_COMPREHENSION_NOT_A_CALL = "not-a-registration"
_LOOP_VARIABLE_NOT_FIRST = "loop-variable-not-first"
_LOOP_VARIABLE_REUSED = "loop-variable-reused"
#: A constant other modules may read only when it could be a piece of a path or a URL:
#: short, and without whitespace -- prose, messages and format strings are left out.
_MAX_CONSTANT = 256
#: Constants one module offers, in line order; past this, the rest are not offered.
_MAX_CONSTANTS = 64
#: How many module constants one composed constant may stand on: a literal is 0, a composed
#: constant is 1 + the deepest local name it reads. Imported names count 0.
_MAX_VALUE_DEPTH = 8
#: Pieces (``["t", text]``, ``["r", module, name]``) one evaluated value may have.
_MAX_VALUE_PARTS = 64
#: Serialized characters of one composed constant offered to the global pass.
_MAX_OFFERED_ENTRY = 1024

#: Why an expression is not a proven string: internal, for tests and measurement; never stored.
_NOT_A_STRING = "not-a-string"
_UNSUPPORTED = "unsupported-expression"
_CONVERSION = "conversion-or-format-spec"
_UNKNOWN_NAME = "unknown-name"
_REBOUND = "rebound"
_STAR_MAY_REBIND = "star-may-rebind"
_UNORDERED = "used-before-binding"
_TOO_DEEP = "depth-exceeded"
_TOO_MANY_PARTS = "too-many-parts"
_TOO_LONG = "too-long"
_TOO_NESTED = "nesting-too-deep"
_SHADOWED = "shadowed-in-function"
_PREFIX_IMPORTED = "prefix-imported"
_INVALID_PREFIX = "invalid-prefix"

#: What an expression is read for. A composed constant's definition and a prefix are read in
#: execution order; a route path is read as it always was (a name needs no earlier binding)
#: except for a composed constant; a mount inside a factory is read with no order at all. A
#: client's request URL and ``base_url`` are read like a route path, for the same reason.
_DEFINITION = "definition"
_PREFIX = "prefix"
_ROUTE = "route"
_FACTORY = "factory"
_CLIENT = "client"

#: What a composed request URL or base is made of: text written at the call, a name proven to
#: hold text, and everything else (kept as the expression it was written as).
_LITERAL = "L"
_PROVEN = "N"
_OPEN = "S"
#: Why a composed read of a request URL or a base gave nothing: internal, for tests and
#: measurement; never stored. ``composed`` is a read that stands; ``unchanged`` one that
#: improved on nothing.
_COMPOSED = "composed"
_UNCHANGED = "unchanged"
_CLIENT_IMPORTED = "client-imported"
_GLUED_AUTHORITY = "glued-authority"
_GLUED_PATH = "glued-path"
_CREDENTIALS = "credentials"
_NOT_AN_ORIGIN = "not-an-origin"
_RELATIVE_URL = "relative-url"
_NO_PATH = "no-path"
_DOTTED_SEGMENT = "dotted-segment"
#: Characters of text one composed request URL is read from.
_MAX_CLIENT_TEXT = 2048
#: ``http://`` or ``https://`` at the start of a URL, in any case.
_HTTP_SCHEME = re.compile(r"https?://", re.IGNORECASE)
#: What the text of a host that is partly a value may be made of: a name, a port, a bracket.
_AUTHORITY_TEXT = re.compile(r"[A-Za-z0-9._:\[\]-]*")


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
    #: The module constants (name, line) its own prefix was composed from.
    prefix_from: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True, slots=True)
class _Subclass:
    """A module-level class proven to construct a receiver of its base's kind."""

    #: The kind of the framework class it stands on, however many classes of this module away.
    kind: str
    #: Classes between it and the framework's: ``1`` for a direct subclass.
    depth: int
    #: Where the class statement begins; a construction counts only after it.
    position: _Position
    line: int
    #: The class of this module it extends; ``None`` for a framework class.
    base: str | None
    #: The own ``__init__`` of each class on the way that is no plain pass-through, nearest
    #: first, as :meth:`PythonHttp._custom_init` states it.
    inits: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class _ClassRef:
    """A class another module defines, as this module's import names it."""

    #: The module as written, relative dots included: ``base``, ``.base``.
    module: str
    #: The class's name in that module.
    name: str
    #: The imported name, or the imported module's alias, it is read through.
    local: str
    #: ``Name`` in ``alias.Name``; ``None`` when ``local`` is the class itself.
    attribute: str | None

    @property
    def spelled(self) -> str:
        return self.local if self.attribute is None else f"{self.local}.{self.attribute}"


@dataclass(frozen=True, slots=True)
class _ImportedSubclass:
    """A module-level class proven to extend a class another module defines."""

    ref: _ClassRef
    #: Classes of this module between it and the imported class: ``1`` for a direct subclass.
    depth: int
    #: Where the class statement begins; a construction counts only after it.
    position: _Position
    line: int
    #: The class of this module it extends; ``None`` for the imported class itself.
    base: str | None
    #: As :attr:`_Subclass.inits`, for the classes of this module only.
    inits: tuple[dict[str, object], ...] = ()


@dataclass(slots=True)
class _ClassReceiver:
    """A name proven to hold an application built from a class another module defines."""

    ref: _ClassRef
    #: Classes of this module between the class constructed and the imported one: ``0`` when the
    #: imported class itself is constructed.
    depth: int
    #: The line that constructs it.
    line: int
    #: The subclass of this module it is constructed from; ``None`` for the imported class.
    via: str | None
    #: Cleared when it is mounted as a sub-application, which serves it somewhere else.
    usable: bool = True
    #: What the construction hands the class: positional arguments, keywords and, for each
    #: prefix keyword, the prefix it states (``None``: unreadable).
    ctor: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _Imported:
    """Where a name this module imports comes from, as its import statement says."""

    #: The module, relative dots included: ``.views``, ``service_prefix``.
    module: str
    #: The name taken from it; ``None`` for ``import module``, which binds the module.
    attribute: str | None


@dataclass(frozen=True, slots=True)
class _Built:
    """An application a function builds: bound once by its own top-level statement, or once in
    each branch of one top-level ``if``/``match`` whose other branches raise."""

    #: Its kind, when every class it may be built from is this module's or the framework's.
    kind: str | None
    #: The classes other modules define that it may be built from: ``module``, ``class``,
    #: ``depth`` and ``init`` (this module's ``__init__`` facts), for the global pass.
    classes: tuple[dict[str, str], ...]
    #: The function's top-level statement that binds it.
    index: int


@dataclass(slots=True)
class _HelperScan:
    """What a same-module function does with one of its positional parameters (R2c-1)."""

    #: The parameter's position among the positional parameters, and whether a call may also
    #: name it (a positional-only parameter may not be).
    index: int
    keyword: bool
    #: Top-level registrations on it: the mount as a claim would state it (the owner is the
    #: application a call hands over), and where its child is bound, when the module says.
    mounts: list[tuple[ParsedReference, _Position | None]] = field(default_factory=list)
    #: Top-level calls of a same-module function handing it over: ``(callee, line, how)``, the
    #: parameter being the argument at position ``how`` (an ``int``) or the keyword ``how``.
    calls: list[tuple[str, int, int | str]] = field(default_factory=list)
    #: Refusals, each ``{"code", "line"}`` and, for a registration whose child is readable, its
    #: ``module``/``name``.
    refusals: list[dict[str, object]] = field(default_factory=list)


def _submodule(module: str, name: str) -> str:
    """``module.name`` as an import specifier: ``.`` + ``views`` is ``.views``."""
    return f"{module}{name}" if module.endswith(".") else f"{module}.{name}"


def _path_like(value: str) -> bool:
    return len(value) <= _MAX_CONSTANT and not any(char.isspace() for char in value)


@dataclass(frozen=True, slots=True)
class _Value:
    """A module constant composed from literals and other names, as its pieces."""

    #: ``("t", text)`` or ``("r", module, name)``, in order, adjacent texts not merged.
    parts: tuple[tuple[str, ...], ...]
    line: int
    #: Where its statement begins: what a read must come after.
    position: _Position
    #: 1 + the deepest module constant it reads; an imported name counts 0.
    depth: int
    #: The module constants (name, line) it stands on, directly or through other values.
    sources: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class _Proven:
    """An expression proven to be a string: its pieces and what it was read from."""

    parts: list[list[str]]
    #: The module constants (name, line) substituted, directly or through a value.
    sources: tuple[tuple[str, int], ...]
    #: The deepest module constant it reads: a literal is 0.
    depth: int
    #: Whether a composed constant (a :class:`_Value`) contributed.
    composed: bool


@dataclass(frozen=True, slots=True)
class _Unknown:
    """An expression that is not a proven string, and why."""

    reason: str


def _by_line(sources: Iterable[tuple[str, int]]) -> tuple[tuple[str, int], ...]:
    """Each (name, line) once, ordered by line and then name."""
    return tuple(sorted(set(sources), key=lambda found: (found[1], found[0])))


def _joined(first: _Proven, second: _Proven) -> _Proven | _Unknown:
    """``first`` followed by ``second``, unless that is more pieces than a value may have."""
    parts = first.parts + second.parts
    if len(parts) > _MAX_VALUE_PARTS:
        return _Unknown(_TOO_MANY_PARTS)
    return _Proven(
        parts,
        tuple(dict.fromkeys((*first.sources, *second.sources))),
        max(first.depth, second.depth),
        first.composed or second.composed,
    )


def _literal_length(parts: Sequence[Sequence[str]]) -> int:
    """The characters of text in a value's pieces; an imported name has none known here."""
    return sum(len(part[1]) for part in parts if part[0] == "t")


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
class _ClientPiece:
    """One piece of a request URL or a client's base, as it is composed (:data:`_LITERAL`,
    :data:`_PROVEN` or :data:`_OPEN`)."""

    kind: str
    #: Literal and proven: the text. Open: the expression, shortened for display.
    text: str
    #: Proven only: the text as the pieces a composed constant is stored in (one for a literal
    #: constant), each boundary between them held to the same rules as a boundary of the URL.
    parts: tuple[str, ...] = ()
    #: Proven only: the line of the module constant, and how many constants it stands on.
    line: int = 0
    depth: int = 0
    #: Proven only: the name as written, which it stays when the path may not take its text.
    expr: str = ""
    #: Open only: why the name is no proven text, when it was a name at all.
    reason: str = ""


@dataclass(slots=True)
class _ClientRead:
    """One attempted composed read of a request URL or a client's base. Never emitted."""

    line: int
    outcome: str
    scheme: str | None = None
    host: str | None = None
    port: str | None = None
    #: The path the first proven piece brings (a base), and the rest of the path.
    base_segments: tuple[str | None, ...] = ()
    endpoint_segments: tuple[str | None, ...] = ()
    #: Whether a name was left open because another module binds it.
    imported: bool = False


@dataclass(frozen=True, slots=True)
class _Composition:
    """A composed read that stands: an absolute URL, or the parts of a rooted one."""

    read: _ClientRead
    url: _ClientUrl | None = None
    rooted: list[list[str]] | None = None


@dataclass(slots=True)
class _Walked:
    """A composed URL read as scheme, authority and path."""

    #: ``absolute`` (a scheme), ``open`` (a host that is a value, then a path) or ``rooted``.
    form: str
    #: The scheme as written, with its ``://``.
    scheme: str = ""
    #: The authority's pieces, as (kind, text): text, or an expression left open.
    authority: list[tuple[str, str]] = field(default_factory=list)
    #: Whether the authority ended at a ``/`` rather than at the end, a ``?`` or a ``#``.
    slashed: bool = False
    #: The path, as the parts :func:`~repoatlas.models.http.route_template` reads.
    parts: list[list[str]] = field(default_factory=list)
    #: The line of the first proven piece at the start of the URL, when there is one.
    base_at: int | None = None
    #: The path the first piece brings with it.
    head_path: str = ""
    #: The expression of a host that is a value, for ``open``.
    host: str = ""
    #: Whether any piece was left open, a proven one that may not fill the path included.
    has_open: bool = False


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


def _first_of(text: str, characters: str) -> int | None:
    """The index of the first of ``characters`` in ``text``, or ``None``."""
    found = [index for index in (text.find(char) for char in characters) if index >= 0]
    return min(found) if found else None


def _append_literal(parts: list[list[str]], text: str) -> None:
    """Add literal text to a template's parts, joined to the literal before it."""
    if not text:
        return
    if parts and parts[-1][0] == http.TEMPLATE_LITERAL:
        parts[-1][1] += text
    else:
        parts.append([http.TEMPLATE_LITERAL, text])


def _glued(state: str, left: tuple[str, str], right: tuple[str, str]) -> str | None:
    """Why two pieces may not meet where they do, or ``None``: the rule of a junction.

    Each side is (kind, text); an open piece has no known text. Inside the authority a value
    may meet text only at ``://``, ``.`` or ``:``; in the path only at a ``/``, a ``?`` or a
    ``#``. The boundary that ends the authority, and any boundary in the scheme or the query,
    is not a junction of this kind.
    """
    left_kind, left_text = left
    right_kind, right_text = right
    if state == "authority":
        if right_kind != _OPEN and right_text.startswith(("/", "?", "#")):
            return None
        if left_kind != _OPEN and left_text.endswith(("://", ".", ":")):
            return None
        if right_kind != _OPEN and right_text.startswith((".", ":")):
            return None
        return _GLUED_AUTHORITY
    if state == "path":
        if left_kind != _OPEN and left_text.endswith("/"):
            return None
        if right_kind != _OPEN and right_text.startswith(("/", "?", "#")):
            return None
        return _GLUED_PATH
    return None


def _origin_parts(origin: str) -> tuple[str, str, str | None]:
    """The scheme, the host and the port (if written) of a canonical origin."""
    scheme, _, authority = origin.partition("://")
    if authority.startswith("["):
        close = authority.find("]")
        return scheme, authority[: close + 1], authority[close + 2 :] or None
    host, _, port = authority.partition(":")
    return scheme, host, port or None


def _walk_client(pieces: list[_ClientPiece]) -> _Walked | str:
    """A composed URL's pieces read as scheme, authority and path; or why they are not one.

    Iterative, left to right, over the characters of the text: the scheme (``http://`` or
    ``https://``, entirely in text before any open piece), the authority up to the first
    ``/``, ``?`` or ``#``, then the path up to a ``?`` or ``#``, after which nothing is read.
    Every boundary where a proven or open piece meets another piece -- the pieces of a composed
    constant included -- is held to :func:`_glued`. A proven piece that begins in the path
    is spliced only if its own text starts with ``/``; otherwise it is left open.
    """
    pieces = [piece for piece in pieces if piece.kind == _OPEN or piece.text]
    if not pieces:
        return _NOT_AN_ORIGIN
    first = pieces[0]
    state = "path"
    left = 0
    previous: tuple[str, str] | None = None
    start = 0
    if first.kind == _OPEN:
        walked = _Walked("open", host=first.text, has_open=True)
        state, previous, start = "after-host", (_OPEN, first.text), 1
        if len(pieces) == 1:
            return _NO_PATH
    else:
        lead = ""
        for piece in pieces:
            if piece.kind == _OPEN:
                break
            lead += piece.text
        found = _HTTP_SCHEME.match(lead)
        if found is not None:
            walked, state, left = _Walked("absolute", scheme=found.group(0)), "scheme", found.end()
        elif lead.startswith("/") and not lead.startswith("//"):
            walked = _Walked("rooted")
        else:
            return _NOT_AN_ORIGIN
    for index in range(start, len(pieces)):
        piece = pieces[index]
        if state == "query":
            break
        if state == "after-host":
            if piece.kind == _OPEN or not piece.text.startswith("/"):
                return _GLUED_PATH
            state = "path"
        if piece.kind == _PROVEN and state == "path" and not piece.text.startswith("/"):
            piece = _ClientPiece(_OPEN, piece.expr)
        if piece.kind == _PROVEN and walked.base_at is None and state in {"scheme", "authority"}:
            walked.base_at = piece.line
        head = index == 0 and piece.kind == _PROVEN
        for text in piece.parts if piece.kind == _PROVEN else (piece.text,):
            if state == "query":
                break
            if piece.kind != _OPEN and not text:
                continue
            atom = (piece.kind, text)
            if previous is not None and (previous[0] != _LITERAL or piece.kind != _LITERAL):
                glued = _glued(state, previous, atom)
                if glued is not None:
                    return glued
            previous = atom
            if piece.kind == _OPEN:
                walked.has_open = True
                if state == "authority":
                    walked.authority.append((_OPEN, text))
                else:
                    walked.parts.append([http.TEMPLATE_SUBSTITUTION, text])
                continue
            offset = 0
            if state == "scheme":
                offset = min(len(text), left)
                left -= offset
                if left == 0:
                    state = "authority"
            tail = ""
            if state == "authority" and offset < len(text):
                rest = text[offset:]
                cut = _first_of(rest, "/?#")
                if cut is None:
                    walked.authority.append((piece.kind, rest))
                    continue
                if cut:
                    walked.authority.append((piece.kind, rest[:cut]))
                walked.slashed = rest[cut] == "/"
                tail = rest[cut:] if walked.slashed else "/" + rest[cut:]
                state = "path"
            elif state == "path":
                tail = text[offset:]
            if tail:
                query = _first_of(tail, "?#")
                _append_literal(walked.parts, tail)
                if head:
                    walked.head_path += tail if query is None else tail[:query]
                if query is not None:
                    state = "query"
    return walked


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


def _escaping(
    tree: ast.Module,
    names: set[str],
    attributes: frozenset[tuple[str, str]] = frozenset(),
    modules: frozenset[str] = frozenset(),
    held: frozenset[str] = frozenset(),
) -> set[str]:
    """The classes among ``names`` that something reads other than to build, extend or annotate.

    A class may be called, extended by a class, or stand in an annotation that calls nothing;
    any other read -- ``C.get = f``, ``alias = C``, ``register(C)``, ``isinstance(x, C)`` --
    could hand the class to code that changes what it registers or serves. One walk,
    parents before children, so the read a name is allowed by is known when it is reached.

    A class read through an imported module (``attributes``, as ``(alias, attribute)``, on the
    module aliases in ``modules``) is held to the same rule, stricter in one way: ``alias.C`` may
    only be called or extended, in any context, so ``alias.C.get = f``, ``alias.C = x`` and an
    annotation escape too; the result names it ``"alias.C"``. Reading the alias itself other than
    as the value of an attribute (``register(alias)``) could hand the whole module over, and
    escapes as the alias name. So does reading one of its special attributes (``alias.__dict__``,
    ``alias.__setattr__``; ``alias.__name__`` is only a name), and with it every ``"alias.C"``.

    A module may also be reached by a path or a string this walk cannot follow to an alias
    (``a.b.C.get = f``, ``getattr(b, "C")``, ``vars(b)["C"]``), so the classes in play are also
    named: ``held`` are their names, and an attribute read of one that does not build or extend,
    or a string that is one, escapes that name wherever it appears.
    """
    allowed: set[int] = set()
    #: The attribute reads that build or extend: a call's callee and a class's base.
    sanctioned: set[int] = set()
    escaping: set[str] = set()
    for node in ast.walk(tree):
        annotation: ast.expr | None = None
        if isinstance(node, ast.Name):
            if (
                isinstance(node.ctx, ast.Load)
                and id(node) not in allowed
                and (node.id in names or node.id in modules)
            ):
                escaping.add(node.id)
            continue
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str) and node.value in held:
                escaping.add(node.value)
            continue
        if isinstance(node, ast.Attribute) and node.attr in held and id(node) not in sanctioned:
            escaping.add(node.attr)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id in modules:
                allowed.add(id(node.value))
                if (node.value.id, node.attr) in attributes and id(node) not in sanctioned:
                    escaping.add(f"{node.value.id}.{node.attr}")
                if node.attr.startswith("__") and node.attr != "__name__":
                    # ``alias.__dict__[...]``, ``alias.__setattr__(...)``: the module's own
                    # table, through which any class it holds can be changed.
                    alias = node.value.id
                    escaping.add(alias)
                    escaping.update(
                        f"{alias}.{held}" for owner, held in attributes if owner == alias
                    )
            continue
        if isinstance(node, ast.Call):
            allowed.add(id(node.func))
            sanctioned.add(id(node.func))
        elif isinstance(node, ast.ClassDef):
            allowed.update(id(base) for base in node.bases)
            sanctioned.update(id(base) for base in node.bases)
        elif isinstance(node, (ast.AnnAssign, ast.arg)):
            annotation = node.annotation
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            annotation = node.returns
        if annotation is not None:
            inside = list(ast.walk(annotation))
            if not any(isinstance(inner, ast.Call) for inner in inside):
                allowed.update(id(inner) for inner in inside)
    return escaping


def _fastapi_prefix(prefix: str) -> bool:
    """FastAPI's own rule: empty, or leading ``/`` and no trailing ``/``."""
    return prefix == "" or (
        prefix.startswith("/") and not prefix.endswith("/") and http.route_path(prefix) is not None
    )


def _names(value: object) -> set[str]:
    return {str(item) for item in value} if isinstance(value, list) else set()


def init_fits(kind: str, inits: Sequence[Mapping[str, object]]) -> bool:
    """Whether the own ``__init__`` methods of a class chain leave where a receiver of ``kind``
    serves to its construction, as the framework's class does.

    An application's always do: what it serves is never read from its construction. A router's or
    blueprint's do when there is one at most, which hands the base no path keyword, no starred
    argument and no more positional ones than precede a path, and reads its ``**kwargs`` only to
    hand them on, none of its own parameters taking a path keyword out of them.
    """
    if kind in _APPLICATIONS or not inits:
        return True
    if kind not in _PATH_KEYWORDS or len(inits) != 1:
        return False
    init = inits[0]
    path = _PATH_KEYWORDS[kind]
    if init.get("starred") or init.get("kwargs_read") or _names(init.get("keywords")) & path:
        return False
    if init.get("forwards") and _names(init.get("named")) & path:
        return False
    positional = init.get("positional")
    return isinstance(positional, int) and positional <= _PATH_POSITIONAL[kind]


def reaching_keywords(keywords: Iterable[str], inits: Sequence[Mapping[str, object]]) -> list[str]:
    """The keywords of a construction that reach the framework's constructor through ``inits``
    (which :func:`init_fits` accepted): all of them through no own ``__init__``; through one, those
    its ``**kwargs`` hands on, none if it hands none."""
    if not inits:
        return list(keywords)
    if not inits[0].get("forwards"):
        return []
    named = _names(inits[0].get("named"))
    return [keyword for keyword in keywords if keyword not in named]


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


def _child_ref(node: ast.expr | None, imported: dict[str, _Imported]) -> dict[str, str] | None:
    """The child a registration names: ``_module_ref``'s, or (R2c-2) ``head.A.name`` where
    ``head`` is a name imported from a module -- ``users.views.bp`` after ``from pkg import
    users`` -- stating the module, the head's import and the attribute between; the global
    pass serves it only when the packages' export tables prove both steps submodules."""
    ref = _module_ref(node, imported)
    if ref is not None:
        return {"module": ref[0], "name": ref[1]}
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and isinstance(node.value.value, ast.Name)
    ):
        found = imported.get(node.value.value.id)
        if found is not None and found.attribute is not None:
            return {
                "module": found.module,
                "name": node.attr,
                "through": node.value.attr,
                "head_import": found.attribute,
            }
    return None


def _expression_call(statement: ast.stmt) -> ast.Call | None:
    """``name.method(...)`` as a statement of its own."""
    if not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)):
        return None
    func = statement.value.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return statement.value
    return None


def _name_call(statement: ast.stmt) -> ast.Call | None:
    """``name(...)`` as a statement of its own."""
    if not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)):
        return None
    return statement.value if isinstance(statement.value.func, ast.Name) else None


def _argument_names(call: ast.Call) -> list[str]:
    """The names a call is handed directly, in order: positionally, then by keyword."""
    values = [*call.args, *(keyword.value for keyword in call.keywords)]
    return [value.id for value in values if isinstance(value, ast.Name)]


def _passes(how: int | str, parameter: str, scan: _HelperScan) -> bool:
    """Whether handing something over as ``how`` -- a position, or a keyword -- reaches the
    callee's own parameter ``parameter``."""
    if isinstance(how, int):
        return how == scan.index
    return scan.keyword and how == parameter


def _refusal_order(refusal: dict[str, object]) -> tuple[int, str]:
    return int(str(refusal["line"])), str(refusal["code"])


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


def _literal_lists(
    statements: list[ast.stmt], counts: Counter[str], scope: ast.AST
) -> dict[str, list[ast.expr]]:
    """Names the statements bind, once, to a list or tuple display: ``routers = [a, b]``.

    A list the ``scope`` (the module, or the function holding the statements) reads other
    than as what a loop or a comprehension iterates is not kept: ``routers.pop()``,
    ``routers[0] = x``, ``alias = routers`` and ``register(routers)`` may all change what
    the loop would see, so what it would mount is not the display's elements.
    """
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
    if not found:
        return found
    iterated: set[int] = set()
    loads: list[ast.Name] = []
    for node in ast.walk(scope):
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load) and node.id in found:
                loads.append(node)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            iterated.add(id(node.iter))
    for name in loads:
        if id(name) not in iterated:
            found.pop(name.id, None)
    return found


def _loop_elements(iterated: ast.expr, lists: dict[str, list[ast.expr]]) -> list[ast.expr] | None:
    """What a loop over ``iterated`` visits, when that is a literal list; else ``None``.

    A list or tuple display, or a name bound once to one, with no ``*`` element.
    """
    if isinstance(iterated, (ast.List, ast.Tuple)):
        elements: list[ast.expr] | None = list(iterated.elts)
    elif isinstance(iterated, ast.Name):
        elements = lists.get(iterated.id)
    else:
        elements = None
    if elements is None or any(isinstance(element, ast.Starred) for element in elements):
        return None
    return elements


def _loop_call_refusal(call: ast.Call, variable: str) -> str | None:
    """Why ``call`` is not one the loop variable stands in for, or ``None``.

    The variable must be the first positional argument and appear nowhere else in the call.
    """
    first = call.args[0] if call.args else None
    if not isinstance(first, ast.Name) or first.id != variable:
        return _LOOP_VARIABLE_NOT_FIRST
    if sum(isinstance(node, ast.Name) and node.id == variable for node in ast.walk(call)) != 1:
        return _LOOP_VARIABLE_REUSED
    return None


def _unrolled(call: ast.Call, elements: list[ast.expr]) -> Iterator[ast.Call]:
    """``call`` once per element, each in its first argument's place."""
    for element in elements:
        unrolled = ast.Call(func=call.func, args=[element, *call.args[1:]], keywords=call.keywords)
        yield ast.copy_location(unrolled, call)


def _comprehension_loop(
    statement: ast.stmt,
    counts: Counter[str],
    lists: dict[str, list[ast.expr]],
    imported: dict[str, _Imported],
) -> tuple[str, ast.Call | None, list[ast.expr] | None] | None:
    """A comprehension statement that mounts, and what became of it; ``None`` for any other.

    ``[app.include_router(r, prefix="/api") for r in routers]`` is the one-line loop
    ``for r in routers: app.include_router(r, prefix="/api")``, and is read as it. The
    outcome is ``"unrolled"`` -- the call and the elements it stands for -- or the first
    reason it is refused. A refusal reads nothing and says nothing else about the module.
    """
    comprehension = statement.value if isinstance(statement, ast.Expr) else None
    if not isinstance(comprehension, _COMPREHENSIONS):
        return None
    if not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _COMPREHENSION_MOUNTS
        for node in ast.walk(comprehension)
    ):
        return None
    if isinstance(comprehension, ast.GeneratorExp):
        return _COMPREHENSION_GENERATOR, None, None
    if isinstance(comprehension, ast.DictComp):
        return _COMPREHENSION_DICT, None, None
    if len(comprehension.generators) != 1:
        return _COMPREHENSION_MULTIPLE, None, None
    generator = comprehension.generators[0]
    if generator.is_async:
        return _COMPREHENSION_ASYNC, None, None
    if generator.ifs:
        return _COMPREHENSION_FILTERED, None, None
    target = generator.target
    if not isinstance(target, ast.Name):
        return _COMPREHENSION_TARGET, None, None
    if counts[target.id] != 1:
        return _COMPREHENSION_REBOUND, None, None
    iterated = generator.iter
    if isinstance(iterated, ast.Name) and iterated.id in imported and iterated.id not in lists:
        return _COMPREHENSION_IMPORTED, None, None
    elements = _loop_elements(iterated, lists)
    if elements is None:
        return _COMPREHENSION_DYNAMIC, None, None
    call = comprehension.elt
    if not (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
    ):
        return _COMPREHENSION_NOT_A_CALL, None, None
    refusal = _loop_call_refusal(call, target.id)
    if refusal is not None:
        return refusal, None, None
    return _COMPREHENSION_UNROLLED, call, elements


def _statement_calls(
    statements: list[ast.stmt],
    counts: Counter[str],
    lists: dict[str, list[ast.expr]],
    imported: dict[str, _Imported],
    outcomes: list[tuple[int, str]] | None = None,
) -> Iterator[ast.Call]:
    """``name.method(...)`` statements, with a loop over a literal list unrolled.

    ``for router in routers: app.include_router(router)`` is read as one call per
    element, each element in the loop variable's place, when the list is a display or
    a name bound once to one, the loop variable is bound by nothing else, and it is the
    call's first argument and appears nowhere else in it. A list or set comprehension
    statement of one such call is read as that loop, by :func:`_comprehension_loop`;
    what became of each is appended to ``outcomes`` as (line, outcome).
    """
    for statement in statements:
        call = _expression_call(statement)
        if call is not None:
            yield call
            continue
        found = _comprehension_loop(statement, counts, lists, imported)
        if found is not None:
            outcome, mounted, elements = found
            if outcomes is not None:
                outcomes.append((statement.lineno, outcome))
            if mounted is not None and elements is not None:
                yield from _unrolled(mounted, elements)
            continue
        if not (
            isinstance(statement, ast.For)
            and not statement.orelse
            and isinstance(statement.target, ast.Name)
            and counts[statement.target.id] == 1
        ):
            continue
        variable = statement.target.id
        elements = _loop_elements(statement.iter, lists)
        if elements is None:
            continue
        for inner in statement.body:
            call = _expression_call(inner)
            if call is None or _loop_call_refusal(call, variable) is not None:
                continue
            yield from _unrolled(call, elements)


def _branches(statement: ast.stmt) -> list[list[ast.stmt]] | None:
    """The branches of an ``if`` with an ``else`` (an ``elif`` chain read as one) or of a
    ``match`` whose last case is a bare ``case _:``: exactly one of them runs."""
    if isinstance(statement, ast.If):
        branches: list[list[ast.stmt]] = []
        current = statement
        while True:
            branches.append(current.body)
            if not current.orelse:
                return None
            if len(current.orelse) == 1 and isinstance(current.orelse[0], ast.If):
                current = current.orelse[0]
                continue
            branches.append(current.orelse)
            return branches
    match = getattr(ast, "Match", None)
    if match is None or not isinstance(statement, match):
        return None
    cases = statement.cases
    last = cases[-1].pattern if cases else None
    if (
        not isinstance(last, ast.MatchAs)
        or last.pattern is not None
        or last.name is not None
        or cases[-1].guard is not None
    ):
        return None
    return [case.body for case in cases]


def _raises(branch: list[ast.stmt]) -> bool:
    return bool(branch) and isinstance(branch[-1], ast.Raise)


def _plainly_decorated(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether a method's decorators are names or attributes, none a call, and its name is no
    member the framework serves through: what they bind under that name serves nothing."""
    name = function.name
    if name.startswith("__") or name in _PRIVATE_MEMBERS or name in _SERVING_ATTRIBUTES:
        return False
    for decorator in function.decorator_list:
        node = decorator
        while isinstance(node, ast.Attribute):
            node = node.value
        if not isinstance(node, ast.Name):
            return False
    return True


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
    values: tuple[tuple[str, int], ...] = (),
    composed: bool = False,
) -> list[ParsedReference]:
    """One :data:`PYTHON_ROUTE` claim per method; ``receiver`` names who declares it.

    ``values`` are the module constants (name, line) the path was read from, shown when there
    are any; ``composed`` is whether one of them was itself composed, which makes the path so.
    """
    literal = all(part[0] == "t" for part in parts) and not composed
    shown = "".join(part[1] if part[0] == "t" else "{" + ".".join(part[1:]) + "}" for part in parts)
    read_from = {"values": json.dumps(values)} if values else {}
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
            **read_from,
            **receiver,
        )
        for method in methods
    ]


def _class_route_claim(
    receiver: str,
    built_from: dict[str, str],
    methods: tuple[str, ...],
    parts: list[list[str]],
    span: tuple[int, int],
    qualified: str,
    values: tuple[tuple[str, int], ...] = (),
    composed: bool = False,
) -> list[ParsedReference]:
    """One :data:`PYTHON_CLASS_ROUTE` claim per method, on the application ``receiver``.

    ``built_from`` says which class the application is built from and how many classes of this
    module stand above it. The claim names a symbol, not a route: it is a route only once the
    global pass proves the class, and it never becomes an edge by itself.
    """
    literal = all(part[0] == "t" for part in parts) and not composed
    read_from = {"values": json.dumps(values)} if values else {}
    return [
        pending_reference(
            EdgeType.REFERENCES,
            EntityType.SYMBOL,
            receiver,
            span[0],
            span[1],
            role=PYTHON_CLASS_ROUTE,
            source_symbol=qualified,
            method=method,
            path=json.dumps(parts),
            provenance=EXACT if literal else COMPOSED,
            **read_from,
            **built_from,
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
        #: Module constant composed from literals and other names, each bound exactly once:
        #: ``USERS = f"{BASE}/users"``. Never in ``_constants``, and nothing that reads one
        #: (a route path, a prefix, a request's URL, a client's base) ever writes it there.
        self._values: dict[str, _Value] = {}
        #: Where the statement binding a literal constant, a top-level import or a value begins.
        self._positions: dict[str, _Position] = {}
        #: How many times each name is bound in the module, a star's possible rebinding included.
        self._counts: Counter[str] = Counter()
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
        #: Alias → (the imported name it is one hop from, where the alias statement begins):
        #: ``s = sub`` or ``s = m.sub`` at module level, after the import, `s` bound only so.
        self._aliases: dict[str, tuple[tuple[str, str], _Position]] = {}
        #: Alias → (the name it is one hop from, where the alias statement begins), for any
        #: module-level ``s = name`` whose ``s`` is bound only so: read for a local receiver.
        self._local_aliases: dict[str, tuple[str, _Position]] = {}
        #: Mounts only the global pass can decide: an imported router, or a factory's.
        self._mount_claims: list[ParsedReference] = []
        #: Same-module helpers that register on an application they are handed, and the calls
        #: that hand them one (R2c-1): claims for the global pass, which expands the calls.
        self._helper_claims: list[ParsedReference] = []
        #: Factory-local applications that another application in the same factory mounts,
        #: as ``"<function>.<name>"``: the owner a claim for their includes carries.
        self._sub_owners: set[str] = set()
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
        #: Each comprehension statement that calls a mount, as (its line, what became of it):
        #: ``"unrolled"`` -- read as the equivalent loop -- or the reason it was refused. For
        #: tests and measurement; never emitted.
        self.comprehension_outcomes: list[tuple[int, str]] = []
        #: Module-level name → why its assignment is no proven string. For tests and
        #: measurement; never emitted.
        self.value_refusals: dict[str, str] = {}
        #: A route path no evaluation proves, as (decorator line, reason). Never emitted.
        self.path_refusals: list[tuple[int, str]] = []
        #: A prefix no evaluation proves, as (line, reason). Never emitted.
        self.prefix_refusals: list[tuple[int, str]] = []
        #: The module constants a proven prefix stands on, as (line, ``"receiver"`` or
        #: ``"mount"``, sources). Never emitted.
        self.prefix_sources: list[tuple[int, str, tuple[tuple[str, int], ...]]] = []
        #: Each attempt to read a request's URL or a client's base by composing module
        #: constants, with what became of it. For tests and measurement; never emitted.
        self.client_reads: list[_ClientRead] = []
        #: Module-level class → the receiver kind it constructs, for each class derived from a
        #: framework application, router or blueprint with nothing in it that could change what
        #: it registers or serves. For tests and measurement; never emitted.
        self.subclasses: dict[str, _Subclass] = {}
        #: Module-level class derived from one → why it builds no receiver. Never emitted.
        self.class_refusals: dict[str, str] = {}
        #: Module-level class derived from a class another module defines (a name imported here,
        #: or ``alias.Name`` on an imported module), or from such a class of this module, with
        #: nothing in it that could change what it registers or serves. Never in ``subclasses``:
        #: what the imported class is, only the global pass can say. For tests and measurement;
        #: never emitted.
        self.imported_subclasses: dict[str, _ImportedSubclass] = {}
        #: Module-level class derived from one → why it builds no receiver. Never emitted.
        self.imported_class_refusals: dict[str, str] = {}
        #: Receiver name → the imported class (or subclass of one) it is constructed from.
        self._class_receivers: dict[str, _ClassReceiver] = {}
        #: Class → the facts of its own ``__init__``, when that is read and no pass-through.
        self._inits: dict[str, dict[str, object]] = {}
        #: Imported class refs a function builds an application from → whether one escapes.
        self._ref_escapes: dict[_ClassRef, bool] = {}
        self._tree = tree
        counts, stars = _binding_counts(tree)
        #: Whether the module has a star import, which could rebind ``super``.
        self._stars = bool(stars)
        unproven = _star_unproven(tree, stars) if stars else {}
        if unproven is None:
            self.star_unproven = None
            return
        for name in unproven:
            counts[name] += 1
        self.star_unproven = unproven
        self._counts = counts
        self._imports(tree, counts)
        self._read_aliases(tree, counts)
        self._optional_imports(tree, counts)
        self._read_constants(tree, counts)
        self._read_values(tree, counts)
        self._read_alternatives(tree, counts)
        self._read_clients(tree, counts)
        self._classes(tree, counts)
        self._imported_classes(tree, counts)
        self._constructors(tree, counts)
        self._imported_constructors(tree, counts)
        self._mounts(tree, counts)
        self._imported_escapes(tree)
        self._factories(tree)
        self._helpers(tree)
        self._unserve_sub_applications()
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
        for statement in tree.body:
            if isinstance(statement, (ast.Import, ast.ImportFrom)):
                for local in _bound_names(statement):
                    if local in self._imported:
                        self._positions[local] = _position(statement)
        self._lists = _literal_lists(tree.body, counts, tree)

    def _read_aliases(self, tree: ast.Module, counts: Counter[str]) -> None:
        """``s = sub``, one hop from an imported application, read for ``mount`` only.

        The alias is a module-level plain assignment of one name that nothing else binds
        anywhere in the module, to an exact imported name or ``module.attr`` -- never to
        another alias -- and it comes after the import that makes the name an import. A
        conditional, nested, annotated or repeated binding is no alias. The same
        assignment of a bare name is also kept for a local receiver: mounting its alias
        makes the receiver a sub-application as mounting the receiver itself does.
        """
        for statement in tree.body:
            if not (
                isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
            ):
                continue
            name, value = statement.targets[0].id, statement.value
            if counts[name] != 1:
                continue
            at = _position(statement)
            if isinstance(value, ast.Name):
                head = value.id
                self._local_aliases[name] = (head, at)
            elif isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
                head = value.value.id
            else:
                continue
            ref = _module_ref(value, self._imported)
            imported_at = self._positions.get(head)
            if ref is not None and imported_at is not None and imported_at < at:
                self._aliases[name] = (ref, at)

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

    def _class_origin(self, base: ast.expr) -> _Origin | str | None:
        """What a base stands on: a constructor, a subclass of this module, or a refused class.

        ``(kind, depth, name)`` -- the name is the module's own class, ``None`` for a
        constructor -- :data:`_BASE_REFUSED` for a class of this module that builds no receiver,
        and ``None`` for anything else.
        """
        kind = _CONSTRUCTORS.get(self._resolve(base) or "")
        if kind is not None:
            return kind, 0, None
        if isinstance(base, ast.Name):
            found = self.subclasses.get(base.id)
            if found is not None:
                return found.kind, found.depth, base.id
            if base.id in self.class_refusals:
                return _BASE_REFUSED
        return None

    def _classes(self, tree: ast.Module, counts: Counter[str]) -> None:
        """Subclasses of a constructor, defined at module level, that build a receiver.

        Only a statement of the module's own body is read, in source order, so a base is a
        class defined above. A class none of whose bases stands on a constructor, a subclass
        or a refused class is not this reader's, and nothing is recorded for it. The first
        rule a derived class fails is its reason, in ``class_refusals``.
        """
        accepted: list[str] = []
        for node in tree.body:
            if not isinstance(node, ast.ClassDef) or not self._derived(node):
                continue
            reason = self._class_refusal(node, counts)
            if reason is not None:
                self.class_refusals[node.name] = reason
                continue
            origin = self._class_origin(node.bases[0])
            if not isinstance(origin, tuple):
                continue
            base = origin[2]
            inits = self._chain_inits(node.name, self.subclasses[base].inits if base else ())
            if not init_fits(origin[0], inits):
                self.class_refusals[node.name] = _INIT_WITH_EFFECT
                continue
            self.subclasses[node.name] = _Subclass(
                origin[0], origin[1] + 1, _position(node), node.lineno, base, inits
            )
            accepted.append(node.name)
        if not accepted:
            return
        escaping = _escaping(tree, set(accepted))
        for name in accepted:
            base = self.subclasses[name].base
            if base is not None and base in self.class_refusals:
                reason = _BASE_REFUSED
            elif name in escaping:
                reason = _CLASS_ESCAPES
            else:
                continue
            del self.subclasses[name]
            self.class_refusals[name] = reason

    def _derived(self, node: ast.ClassDef) -> bool:
        """Whether a base, or anything inside one, stands on a constructor or a module class."""
        return any(
            isinstance(part, (ast.Name, ast.Attribute)) and self._class_origin(part) is not None
            for base in node.bases
            for part in ast.walk(base)
        )

    # -- classes another module defines ------------------------------------------------

    def _imported_class(self, node: ast.expr) -> _ClassRef | None:
        """The class a name imported from another module, or ``alias.Name``, may be.

        Only an import this module's table proves (bound once, at module level) and not one of
        the libraries read here: the framework's own classes are :meth:`_class_origin`'s. What
        the name is in that module -- a class, a function, a constant -- only the global pass
        can say.
        """
        found = _module_ref(node, self._imported)
        if found is None or found[0].split(".")[0] in _LIBRARIES:
            return None
        if isinstance(node, ast.Name):
            return _ClassRef(found[0], found[1], node.id, None)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            return _ClassRef(found[0], found[1], node.value.id, node.attr)
        return None

    def _imported_before(self, ref: _ClassRef, at: _Position) -> bool:
        """Whether the import ``ref`` is read through comes before the statement at ``at``."""
        bound = self._positions.get(ref.local)
        return bound is not None and bound < at

    def _imported_origin(self, base: ast.expr) -> _Origin | str | None:
        """What a base stands on: a class another module defines, or one of this module's.

        ``(imported, depth, name)`` -- the name is this module's own class on it, ``None`` for the
        imported class itself -- :data:`_BASE_REFUSED` for a class of this module that builds
        no receiver, and ``None`` for anything else.
        """
        if isinstance(base, ast.Name):
            found = self.imported_subclasses.get(base.id)
            if found is not None:
                return _IMPORTED, found.depth, base.id
            if base.id in self.imported_class_refusals:
                return _BASE_REFUSED
        if self._imported_class(base) is not None:
            return _IMPORTED, 0, None
        return None

    def _imported_classes(self, tree: ast.Module, counts: Counter[str]) -> None:
        """Subclasses, defined at module level, of a class another module defines.

        Read like :meth:`_classes`, by the same rules, but kept apart from its results: a
        class whose base is imported is a receiver's class only if the module it comes from
        offers it, which only the global pass can prove. The import must come before the class
        statement. Whether the imported name or the class is read other than to build or extend
        is decided afterwards, by :meth:`_imported_escapes`.
        """
        for node in tree.body:
            if not isinstance(node, ast.ClassDef) or not self._derived_from_import(node):
                continue
            reason = self._refusal(node, counts, self._imported_origin)
            if reason is not None:
                self.imported_class_refusals[node.name] = reason
                continue
            base = node.bases[0]
            origin = self._imported_origin(base)
            if not isinstance(origin, tuple):
                continue
            if origin[2] is None:
                ref = self._imported_class(base)
                if ref is None or not self._imported_before(ref, _position(node)):
                    self.imported_class_refusals[node.name] = _IMPORTED_LATE
                    continue
                inits = self._chain_inits(node.name, ())
            else:
                ref = self.imported_subclasses[origin[2]].ref
                inits = self._chain_inits(node.name, self.imported_subclasses[origin[2]].inits)
            self.imported_subclasses[node.name] = _ImportedSubclass(
                ref, origin[1] + 1, _position(node), node.lineno, origin[2], inits
            )

    def _derived_from_import(self, node: ast.ClassDef) -> bool:
        """Whether a base, or anything inside one, stands on an imported class or a class of it."""
        return any(
            isinstance(part, (ast.Name, ast.Attribute)) and self._imported_origin(part) is not None
            for base in node.bases
            for part in ast.walk(base)
        )

    @staticmethod
    def _binding(statement: ast.stmt) -> tuple[str, ast.expr] | None:
        """``name = value`` or ``name: T = value`` as a module-level statement."""
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
        ):
            return statement.targets[0].id, statement.value
        if (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.value is not None
        ):
            return statement.target.id, statement.value
        return None

    def _imported_constructors(self, tree: ast.Module, counts: Counter[str]) -> None:
        """Names bound once, at module level, to a call of an imported class or of a subclass.

        ``app = CustomApp()`` on ``from base import CustomApp`` (or ``base.CustomApp()``), the
        import above the statement, or ``app = App()`` on a class of this module that extends one
        and stands above it. What the receiver is only the global pass can say.
        """
        for statement in tree.body:
            bound = self._binding(statement)
            if bound is None:
                continue
            name, value = bound
            if counts[name] != 1 or not isinstance(value, ast.Call) or _dynamic(value):
                continue
            at = _position(statement)
            func = value.func
            if isinstance(func, ast.Name) and func.id in self.imported_subclasses:
                subclass = self.imported_subclasses[func.id]
                if not subclass.position < at:
                    continue
                receiver = _ClassReceiver(subclass.ref, subclass.depth, statement.lineno, func.id)
            else:
                found = self._imported_class(func)
                if found is None or not self._imported_before(found, at):
                    continue
                receiver = _ClassReceiver(found, 0, statement.lineno, None)
            receiver.ctor = self._construction(value, at)
            self._class_receivers[name] = receiver

    def _construction(self, call: ast.Call, at: _Position) -> dict[str, object]:
        """What a construction of a class another module defines hands it, for the global pass:
        whether a router or blueprint it may be serves where its keywords say."""
        keywords = _keywords(call)
        prefixes: dict[str, str | None] = {}
        for name in _PREFIX_KEYWORDS.values():
            if name in keywords:
                found = self._prefix_value(keywords[name], at)
                prefixes[name] = found[0] if found is not None else None
        return {"positional": len(call.args), "keywords": sorted(keywords), "prefixes": prefixes}

    def _imported_escapes(self, tree: ast.Module) -> None:
        """Drop what an imported class, or a class that extends one, could be changed through.

        An imported class (or ``alias.Name``) may only be called or extended, as a class of this
        module may (:func:`_escaping`): ``Base.get = f`` here changes the class every module
        sees. A class of this module that is read otherwise, or stands on one that is refused or
        escapes, is refused; a receiver built from one is no receiver.
        """
        if not self.imported_subclasses and not self._class_receivers:
            return
        refs = {found.ref for found in self.imported_subclasses.values()}
        refs.update(found.ref for found in self._class_receivers.values())
        escaping, escaped = self._escaped_refs(refs)

        for name in list(self.imported_subclasses):
            found = self.imported_subclasses[name]
            if found.base is not None and found.base in self.imported_class_refusals:
                reason = _BASE_REFUSED
            elif name in escaping or escaped(found.ref):
                reason = _CLASS_ESCAPES
            else:
                continue
            del self.imported_subclasses[name]
            self.imported_class_refusals[name] = reason
        for name in list(self._class_receivers):
            receiver = self._class_receivers[name]
            gone = receiver.via is not None and receiver.via not in self.imported_subclasses
            if gone or escaped(receiver.ref):
                del self._class_receivers[name]

    def _escaped_refs(self, refs: set[_ClassRef]) -> tuple[set[str], Callable[[_ClassRef], bool]]:
        """The names :func:`_escaping` finds read other than to build or extend, with the
        imported classes ``refs`` and this module's subclasses of them in play, and whether a ref
        is one of them."""
        names = set(self.imported_subclasses) | {ref.local for ref in refs if ref.attribute is None}
        attributes = {(ref.local, ref.attribute) for ref in refs if ref.attribute is not None}
        modules = {ref.local for ref in refs if ref.attribute is not None}
        # Any other local an import binds may be the module that holds such a class, spelled
        # another way (``import base`` beside ``from base import C``): `base.C.get = f` changes
        # it too. Held to the same rule for every class in play, by name.
        others = set(self._imported) - names - modules
        modules |= others
        attributes |= {(alias, ref.name) for alias in others for ref in refs}
        # A module reached by a dotted path, ``getattr`` or ``vars`` is not an alias this walk
        # follows; the class is named instead, by attribute or by string, wherever it is read.
        held = set(self.imported_subclasses) | {ref.name for ref in refs}
        escaping = _escaping(
            self._tree, names, frozenset(attributes), frozenset(modules), frozenset(held)
        )

        def escaped(ref: _ClassRef) -> bool:
            return (
                ref.spelled in escaping
                or ref.local in escaping
                or ref.name in escaping
                or any(f"{alias}.{ref.name}" in escaping for alias in modules)
            )

        return escaping, escaped

    def _class_refusal(self, node: ast.ClassDef, counts: Counter[str]) -> str | None:
        """Why a derived class builds no receiver, by the first rule it fails; else ``None``."""
        return self._refusal(node, counts, self._class_origin)

    def _refusal(
        self,
        node: ast.ClassDef,
        counts: Counter[str],
        origin_of: Callable[[ast.expr], _Origin | str | None],
    ) -> str | None:
        """The rules a derived class is held to, in order; ``origin_of`` says what a base is.

        The same rules for a class on a framework class or a class of this module and for one on
        a class another module defines: only what a base stands on differs.
        """
        if len(node.bases) != 1:
            return _MULTIPLE_BASES
        base = node.bases[0]
        origin = origin_of(base)
        named = isinstance(base, ast.Name) or (
            isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name)
        )
        if not named or origin is None:
            return _DYNAMIC_BASE
        if any(keyword.arg == "metaclass" for keyword in node.keywords):
            return _METACLASS
        if node.keywords:
            return _CLASS_KEYWORDS
        if getattr(node, "type_params", None):
            return _TYPE_PARAMS
        if node.decorator_list:
            return _CLASS_DECORATED
        if counts[node.name] != 1:
            return _CLASS_REBOUND
        if not isinstance(origin, tuple):
            return _BASE_REFUSED
        if origin[1] + 1 > _MAX_CLASS_DEPTH:
            return _DEPTH_EXCEEDED
        return self._body_refusal(node)

    def _body_refusal(self, node: ast.ClassDef) -> str | None:
        """Why a class body could change what the class registers or serves, else ``None``.

        Only ``pass``, a docstring, ``...`` and methods are read. A method may not be a special
        method, a private member the framework serves through or a member it registers or serves
        through; it may be decorated only by names and attributes, never a call
        (``@property``, ``@name.setter``, ``@staticmethod``, ``@asynccontextmanager``), and then
        its name is not one the framework serves through either. ``__init__`` passes its
        arguments on (:meth:`_passes_through`) or calls the base's once and touches nothing the
        framework serves through (:meth:`_custom_init`).
        """
        for statement in node.body:
            if isinstance(statement, ast.Pass):
                continue
            if (
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Constant)
                and (isinstance(statement.value.value, str) or statement.value.value is Ellipsis)
            ):
                continue
            if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return _CLASS_BODY_STATEMENT
            if statement.decorator_list:
                if not _plainly_decorated(statement):
                    return _DECORATED_MEMBER
                continue
            if statement.name == "__init__":
                if self._passes_through(statement):
                    continue
                init = self._custom_init(statement)
                if init is None:
                    return _INIT_WITH_EFFECT
                self._inits[node.name] = init
            elif statement.name.startswith("__") or statement.name in _PRIVATE_MEMBERS:
                return _SPECIAL_METHOD
            elif statement.name in _ROUTING_MEMBERS:
                return _ROUTE_METHOD
        return None

    def _chain_inits(
        self, name: str, inherited: tuple[dict[str, object], ...]
    ) -> tuple[dict[str, object], ...]:
        own = self._inits.get(name)
        return inherited if own is None else (own, *inherited)

    def _custom_init(
        self, function: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> dict[str, object] | None:
        """The facts of an ``__init__`` that calls the base's once and touches nothing the
        framework serves through, else ``None``.

        Synchronous, with its instance as the first positional parameter, bound only so; exactly
        one top-level ``super().__init__(...)`` and no other ``super``, which neither a binding nor
        a star import of the module could change; the instance read only as ``self.name`` with no
        name the framework serves through (:data:`_SERVING_ATTRIBUTES`), a special or a private
        member of it, so it is handed to nothing. Anything else it does stays inside its body.
        The facts say what reaches the base: the positional arguments it hands (and whether one is
        starred), the keywords it names, whether it hands its own ``**kwargs`` on and reads them
        otherwise, and its own parameters by name -- for :func:`init_fits`.
        """
        if self._counts["super"] or self._stars or not isinstance(function, ast.FunctionDef):
            return None
        arguments = function.args
        positional = [*arguments.posonlyargs, *arguments.args]
        if not positional:
            return None
        me = positional[0].arg
        counts, _ = _binding_counts(function)
        if counts[me] != 1:
            return None
        calls = [statement for statement in function.body if self._super_init(statement)]
        supers = [node for node in ast.walk(function) if _is_name(node, "super")]
        if len(calls) != 1 or len(supers) != 1:
            return None
        parents = {
            id(child): parent
            for parent in ast.walk(function)
            for child in ast.iter_child_nodes(parent)
        }
        kwarg = arguments.kwarg.arg if arguments.kwarg is not None else None
        call = calls[0].value
        assert isinstance(call, ast.Call)
        handed = {id(keyword.value) for keyword in call.keywords if keyword.arg is None}
        kwargs_read = False
        for node in ast.walk(function):
            if not isinstance(node, ast.Name):
                continue
            if node.id == me:
                parent = parents.get(id(node))
                if not (
                    isinstance(parent, ast.Attribute)
                    and parent.value is node
                    and not parent.attr.startswith("__")
                    and parent.attr not in _PRIVATE_MEMBERS
                    and parent.attr not in _SERVING_ATTRIBUTES
                ):
                    return None
            elif node.id == kwarg and id(node) not in handed:
                kwargs_read = True
        forwards = False
        keywords: set[str] = set()
        for keyword in call.keywords:
            if keyword.arg is not None:
                keywords.add(keyword.arg)
            elif forwards or kwarg is None or not _is_name(keyword.value, kwarg):
                return None
            else:
                forwards = True
        named = {argument.arg for argument in [*positional[1:], *arguments.kwonlyargs]}
        return {
            "forwards": forwards,
            "named": sorted(named),
            "positional": sum(not isinstance(arg, ast.Starred) for arg in call.args),
            "starred": any(isinstance(arg, ast.Starred) for arg in call.args),
            "keywords": sorted(keywords),
            "kwargs_read": kwargs_read,
        }

    @staticmethod
    def _super_init(statement: ast.stmt) -> bool:
        """``super().__init__(...)`` as a statement of its own."""
        if not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)):
            return False
        func = statement.value.func
        return (
            isinstance(func, ast.Attribute)
            and func.attr == "__init__"
            and isinstance(func.value, ast.Call)
            and _is_name(func.value.func, "super")
            and not func.value.args
            and not func.value.keywords
        )

    def _passes_through(self, function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        """Whether ``function`` only hands its arguments to the base's ``__init__``.

        Exactly ``def __init__(self, *args, **kwargs)`` and ``super().__init__(*args, **kwargs)``,
        after an optional docstring. Refused when ``super`` is bound anywhere in the module or a
        star import could bind it.
        """
        if self._counts["super"] or self._stars or not isinstance(function, ast.FunctionDef):
            return False
        arguments = function.args
        if (
            arguments.posonlyargs
            or len(arguments.args) != 1
            or arguments.vararg is None
            or arguments.kwarg is None
            or arguments.kwonlyargs
            or arguments.defaults
        ):
            return False
        body = function.body
        first = body[0] if body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            body = body[1:]
        if len(body) != 1:
            return False
        only = body[0]
        if not (isinstance(only, ast.Expr) and isinstance(only.value, ast.Call)):
            return False
        call = only.value
        func = call.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "__init__"
            and isinstance(func.value, ast.Call)
            and _is_name(func.value.func, "super")
            and not func.value.args
            and not func.value.keywords
        ):
            return False
        if len(call.args) != 1 or len(call.keywords) != 1:
            return False
        star, keyword = call.args[0], call.keywords[0]
        return (
            isinstance(star, ast.Starred)
            and _is_name(star.value, arguments.vararg.arg)
            and keyword.arg is None
            and _is_name(keyword.value, arguments.kwarg.arg)
        )

    def _constructed_kind(
        self, func: ast.expr, at: _Position | None, shadow: Counter[str] | None = None
    ) -> str | None:
        """The kind of receiver calling ``func`` constructs: a constructor, or a subclass.

        A subclass counts when nothing shadows its name (``shadow``: a factory's own bindings)
        and the class statement comes before the construction, where ``at`` says where that is.
        """
        kind = _CONSTRUCTORS.get(self._resolve(func) or "")
        if kind is not None:
            return kind
        if isinstance(func, ast.Name):
            found = self.subclasses.get(func.id)
            if (
                found is not None
                and (shadow is None or shadow[func.id] == 0)
                and (at is None or found.position < at)
            ):
                return found.kind
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
            at = _position(statement)
            kind = self._constructed_kind(value.func, at)
            if kind is None:
                continue
            subclass = (
                self.subclasses.get(value.func.id) if isinstance(value.func, ast.Name) else None
            )
            if subclass is not None and subclass.inits:
                reaching = set(reaching_keywords(_keywords(value), subclass.inits))
                value = ast.copy_location(
                    ast.Call(
                        func=value.func,
                        args=[],
                        keywords=[k for k in value.keywords if k.arg in reaching],
                    ),
                    value,
                )
            receiver = self._receiver(kind, value, at)
            if receiver is not None:
                receiver.line = statement.lineno
                self._receivers[name] = receiver

    def _receiver(self, kind: str, call: ast.Call, at: _Position) -> _Receiver | None:
        keywords = _keywords(call)
        if kind == _FASTAPI_ROUTER:
            # `APIRouter`'s parameters are keyword-only.
            if call.args:
                return None
            if "prefix" not in keywords:
                return _Receiver(kind, "")
            node = keywords["prefix"]
            found = self._prefix_value(node, at)
            if found is None:
                return None
            if not _fastapi_prefix(found[0]):
                self.prefix_refusals.append((node.lineno, _INVALID_PREFIX))
                return None
            if found[1]:
                self.prefix_sources.append((node.lineno, "receiver", found[1]))
            return _Receiver(kind, found[0], prefix_from=found[1])
        if kind == _FLASK_BLUEPRINT:
            # `url_prefix` is the sixth positional parameter; past five, it may
            # have been passed without a name.
            if len(call.args) > 5:
                return None
            if "url_prefix" not in keywords:
                return _Receiver(kind, None)
            node = keywords["url_prefix"]
            found = self._prefix_value(node, at)
            if found is None:
                return None
            if found[1]:
                self.prefix_sources.append((node.lineno, "receiver", found[1]))
            return _Receiver(kind, found[0], prefix_from=found[1])
        return _Receiver(kind)

    def _mounts(self, tree: ast.Module, counts: Counter[str]) -> None:
        for call in _statement_calls(
            tree.body, counts, self._lists, self._imported, self.comprehension_outcomes
        ):
            func = call.func
            if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
                continue
            if func.attr == "mount" and self._class_receivers:
                self._class_mounts(func.value.id, call)
                built = self._class_owner(func.value.id, call)
                if built is not None:
                    self._class_mount_claim(built, call)
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
                    if isinstance(argument, ast.Name):
                        mounted = self._receivers.get(argument.id) or self._aliased_receiver(
                            argument.id, call
                        )
                        if mounted is not None:
                            mounted.usable = False
                target = _mount_target(owner.kind, call)
                ref = _module_ref(target, self._imported) or self._aliased_ref(target, call)
                if ref is not None:
                    child = {"module": ref[0], "name": ref[1]}
                    self._claim_mount(func.value.id, owner, call, child)

    def _aliased_receiver(self, name: str, call: ast.Call) -> _Receiver | None:
        """The local receiver ``name`` is a one-hop alias of, when the alias precedes ``call``."""
        found = self._local_aliases.get(name)
        if found is None or found[1] >= (call.lineno, call.col_offset):
            return None
        return self._receivers.get(found[0])

    def _aliased_ref(self, node: ast.expr | None, call: ast.Call) -> tuple[str, str] | None:
        """``(module, name)`` the imported name ``node`` is a one-hop alias of, before ``call``."""
        found = self._aliases.get(node.id) if isinstance(node, ast.Name) else None
        if found is None or found[1] >= (call.lineno, call.col_offset):
            return None
        return found[0]

    def _class_owner(self, name: str, call: ast.Call) -> str | None:
        """The name an application built from an imported class is bound to, when ``name``
        is that name or a one-hop alias of it bound before ``call``."""
        if name in self._class_receivers:
            return name
        found = self._local_aliases.get(name)
        if found is None or found[0] not in self._class_receivers:
            return None
        return found[0] if found[1] < (call.lineno, call.col_offset) else None

    def _class_mounts(self, owner: str, call: ast.Call) -> None:
        """``owner.mount(path, name)`` where an application built from an imported class is
        involved: a sub-application serves its routes under the mount's path, not at its root.

        A receiver passed to it is no longer served at the root, and a receiver of this module
        mounted on such an application is not either: the owner's kind is unknown here, and
        ``mount`` is the only way a mounted application is served elsewhere. Names are followed
        through the one-hop aliases the module states before the call.
        """
        owned = self._class_owner(owner, call) is not None
        for argument in (*call.args, *(keyword.value for keyword in call.keywords)):
            if not isinstance(argument, ast.Name):
                continue
            built = self._class_owner(argument.id, call)
            if built is not None:
                self._class_receivers[built].usable = False
            if owned:
                mounted = self._receivers.get(argument.id) or self._aliased_receiver(
                    argument.id, call
                )
                if mounted is not None:
                    mounted.usable = False

    def _class_mount_claim(self, owner: str, call: ast.Call) -> None:
        """A ``mount`` of an imported application by an application built from an imported
        class, recorded as a mount claim of a FastAPI application: the global pass serves the
        child's own routes under the mount, and the parent's paths at the parent's root.
        """
        target = _mount_target(_FASTAPI_APP, call)
        ref = _module_ref(target, self._imported) or self._aliased_ref(target, call)
        if ref is not None:
            child = {"module": ref[0], "name": ref[1]}
            self._claim_mount(owner, _Receiver(_FASTAPI_APP), call, child)

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
            found = _child_ref(argument, self._imported)
            if found is not None:
                self._claim_mount(owner_name, owner, call, found)
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
        node = keywords[prefix_name]
        found = self._prefix_value(node, (call.lineno, call.col_offset))
        if found is not None and child_kind == _FASTAPI_ROUTER and not _fastapi_prefix(found[0]):
            self.prefix_refusals.append((node.lineno, _INVALID_PREFIX))
            found = None
        if found is None:
            child.usable = False
            return
        if found[1]:
            self.prefix_sources.append((node.lineno, "mount", found[1]))
        child.mounts.append((found[0], owner_name))

    def _claim_mount(
        self,
        owner_name: str,
        owner: _Receiver,
        call: ast.Call,
        child: dict[str, str],
        shadow: Counter[str] | None = None,
        sink: list[ParsedReference] | None = None,
        owner_classes: tuple[dict[str, str], ...] = (),
    ) -> None:
        """A mount only the global pass can decide: of an imported router, or a factory's.

        Recorded unreadable -- which keeps the router from being served at all, as a
        mount this module cannot read does to one of its own -- when its arguments may
        carry a prefix this cannot read, when it is a sub-application ``mount``, when its
        owner is a sub-application, or when a blueprint is nested in a blueprint, whose
        composition Flask decides. ``shadow``
        is the factory's own bindings, which a prefix's names must not be among.

        A ``mount`` of an application by a FastAPI application also carries ``mount_prefix``
        -- its path, when that is a non-empty prefix FastAPI itself accepts -- for the global
        pass, which serves the child's routes under it only if it proves the child.

        The claim is added to ``sink`` instead of ``_mount_claims`` when one is given: a
        helper's registration (:meth:`_helpers`), whose owner a call of the helper decides.
        ``owner_classes`` are the classes other modules define the owner may be built from: the
        global pass reads the mount only where each proves the owner's kind.
        """
        attribute = call.func.attr if isinstance(call.func, ast.Attribute) else ""
        readable = attribute in _MOUNT_CALLS and not _dynamic(call)
        prefix: str | None = None
        prefix_from: tuple[tuple[str, int], ...] = ()
        if readable:
            _, prefix_name, child_kind = _MOUNT_CALLS[attribute]
            keywords = _keywords(call)
            if prefix_name in keywords:
                node = keywords[prefix_name]
                found = self._prefix_value(node, (call.lineno, call.col_offset), shadow)
                if found is not None:
                    prefix, prefix_from = found
                readable = prefix is not None
                if (
                    prefix is not None
                    and child_kind == _FASTAPI_ROUTER
                    and not _fastapi_prefix(prefix)
                ):
                    self.prefix_refusals.append((node.lineno, _INVALID_PREFIX))
                    readable = False
                if prefix_from:
                    self.prefix_sources.append((node.lineno, "mount", prefix_from))
            elif child_kind == _FASTAPI_ROUTER:
                prefix = ""
        read_from = {"prefix_from": json.dumps(prefix_from)} if prefix_from else {}
        if owner.kind == _FLASK_BLUEPRINT:
            readable = False
        # What the mount of an application says of where the parent serves it, apart from
        # `readable` and `prefix`, which stay as they are: a literal FastAPI prefix.
        mount_path = ""
        if attribute == "mount" and owner.kind == _FASTAPI_APP and not _dynamic(call):
            path_node = _argument(call, 0, "path")
            found = (
                self._prefix_value(path_node, (call.lineno, call.col_offset), shadow)
                if path_node is not None
                else None
            )
            if found is not None and found[0] and _fastapi_prefix(found[0]):
                mount_path = found[0]
        mounted_at = {"mount_prefix": json.dumps(mount_path)} if mount_path else {}
        if owner_classes:
            mounted_at["owner_classes"] = json.dumps(owner_classes, sort_keys=True)
        (self._mount_claims if sink is None else sink).append(
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
                **read_from,
                **mounted_at,
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
            built = self._function_applications(function, counts)
            lists = {name: found for name, found in self._lists.items() if not counts[name]}
            lists.update(_literal_lists(function.body, counts, function))
            for call in _statement_calls(
                function.body, counts, lists, imported, self.comprehension_outcomes
            ):
                if not isinstance(call.func, ast.Attribute):
                    continue
                name = call.func.value.id if isinstance(call.func.value, ast.Name) else ""
                application = built.get(name)
                kind = None
                if application is not None:
                    kind = application.kind or _CALL_OWNERS.get(call.func.attr)
                if application is None or kind is None:
                    continue
                argument = _mount_target(kind, call)
                mounts_application = kind == _FASTAPI_APP and call.func.attr == "mount"
                if mounts_application and isinstance(argument, ast.Name):
                    if argument.id in built:
                        self._sub_owners.add(f"{function.name}.{argument.id}")
                    elif argument.id in self._receivers and not counts[argument.id]:
                        self._receivers[argument.id].usable = False
                if (
                    isinstance(argument, ast.Name)
                    and argument.id in self._receivers
                    and not counts[argument.id]
                ):
                    child = {"name": argument.id}
                elif call.func.attr in _MOUNT_CALLS:
                    found = _child_ref(argument, imported)
                    if found is None:
                        continue
                    child = found
                else:
                    ref = _module_ref(argument, imported)
                    if ref is None:
                        continue
                    child = {"module": ref[0], "name": ref[1]}
                self._claim_mount(
                    f"{function.name}.{name}",
                    _Receiver(kind),
                    call,
                    child,
                    counts,
                    owner_classes=application.classes,
                )

    def _function_applications(
        self, function: ast.FunctionDef | ast.AsyncFunctionDef, counts: Counter[str]
    ) -> dict[str, _Built]:
        """The applications a function builds, by name.

        A name bound once, by a top-level statement of the function, to a call of an application
        class; or bound once in each branch of one top-level ``if`` with an ``else`` (``elif``
        included) or ``match`` whose last case is a bare ``case _:``, each binding a top-level
        statement of its branch, and every other branch ending in ``raise``. An application class
        is the framework's or this module's (its kind known here), or one another module
        defines -- imported, or extended by this module -- that the global pass must prove; the
        name it is read through is bound by nothing in the function, and it escapes nowhere.
        All the classes of this module a name may be built from are of one kind.
        """
        found: dict[str, _Built] = {}
        for index, statement in enumerate(function.body):
            bound = self._binding(statement)
            if bound is not None:
                name, value = bound
                choice = self._application_choice(value, counts)
                if counts[name] == 1 and choice is not None:
                    built = self._built((choice,), index)
                    if built is not None:
                        found[name] = built
                continue
            branches = _branches(statement)
            if branches is None:
                continue
            bindings: dict[str, list[ast.expr]] = defaultdict(list)
            for branch in branches:
                for inner in branch:
                    named = self._binding(inner)
                    if named is not None:
                        bindings[named[0]].append(named[1])
            for name, values in bindings.items():
                binding = [
                    sum(
                        1
                        for inner in branch
                        if (named := self._binding(inner)) is not None and named[0] == name
                    )
                    for branch in branches
                ]
                if counts[name] != len(values) or any(
                    count > 1 or (count == 0 and not _raises(branch))
                    for count, branch in zip(binding, branches, strict=True)
                ):
                    continue
                choices = [self._application_choice(value, counts) for value in values]
                if any(choice is None for choice in choices):
                    continue
                built = self._built(tuple(c for c in choices if c is not None), index)
                if built is not None:
                    found[name] = built
        return found

    def _application_choice(
        self, value: ast.expr, counts: Counter[str]
    ) -> str | dict[str, str] | None:
        """What a call in a function builds an application from: a kind, an imported class (as
        the global pass reads one), or ``None`` when it is no application this can follow."""
        if not isinstance(value, ast.Call) or _dynamic(value):
            return None
        func = value.func
        kind = self._constructed_kind(func, None, counts)
        if kind is not None:
            return kind if kind in _APPLICATIONS else None
        if isinstance(func, ast.Name) and func.id in self.imported_subclasses:
            if counts[func.id]:
                return None
            subclass = self.imported_subclasses[func.id]
            ref, depth, inits = subclass.ref, subclass.depth, subclass.inits
        else:
            found = self._imported_class(func)
            if found is None or counts[found.local]:
                return None
            ref, depth, inits = found, 0, ()
        if self._escapes(ref):
            return None
        chosen = {"module": ref.module, "class": ref.name, "depth": str(depth)}
        if inits:
            chosen["init"] = json.dumps(inits, sort_keys=True)
        return chosen

    @staticmethod
    def _built(choices: tuple[str | dict[str, str], ...], index: int) -> _Built | None:
        kinds = {choice for choice in choices if isinstance(choice, str)}
        classes = tuple(choice for choice in choices if isinstance(choice, dict))
        if len(kinds) > 1:
            return None
        kind = next(iter(kinds)) if kinds else None
        if kind is not None and classes:
            # The classes another module defines must prove this same kind.
            classes = (*classes, {"kind": kind})
            kind = None
        return _Built(kind, classes, index)

    def _escapes(self, ref: _ClassRef) -> bool:
        """Whether an imported class a function builds from is read, anywhere in the module,
        other than to build or extend it (:meth:`_imported_escapes`'s rule)."""
        found = self._ref_escapes.get(ref)
        if found is None:
            found = self._escaped_refs({ref})[1](ref)
            self._ref_escapes[ref] = found
        return found

    # -- helpers: a same-module function that registers on the application it is handed ----

    def _helpers(self, tree: ast.Module) -> None:
        """Same-module helpers that register on an application they are handed (R2c-1).

        A helper is a module-level, undecorated, synchronous function bound once in the module,
        with a positional parameter bound only as that parameter. What it does with the
        parameter, as its own top-level statements: ``p.register_blueprint(child)`` or
        ``p.include_router(child)`` with a child one import away (or this module's own
        receiver), read as a factory's registration is, and ``G(p)`` of another helper. A
        registration or call inside a branch, a loop, a ``with``, a ``try`` or a nested
        function is refused, as a parameter's alias or a child that is not readable is; any
        other use of the parameter is allowed and ignored.

        A claim (:data:`HELPER_ROLE`) is made only of a helper with something to record: a
        registration, or a call of a helper that has. A call of one (:data:`HELPER_CALL_ROLE`) is
        recorded where the application it hands over is proven in the module: a factory's local
        (bound once to a constructor, before the call) or a module-level application, the call
        being a top-level statement of the function or the module. The refusals are recorded
        beside the call, or in the helper's claim, with the code of the diagnostics; the global
        pass does the expansion (depth, cycles, the application's own rules).
        """
        definitions = {
            statement.name: statement
            for statement in tree.body
            if isinstance(statement, ast.FunctionDef)
            and not statement.decorator_list
            and self._counts[statement.name] == 1
        }
        if not definitions:
            return
        scans = {
            name: self._scan_helper(function, definitions) for name, function in definitions.items()
        }
        chosen: dict[str, tuple[str, _HelperScan]] = {}
        changed = True
        while changed:
            changed = False
            for name, parameters in scans.items():
                if name in chosen:
                    continue
                for parameter, scan in parameters.items():
                    if scan.mounts or any(
                        callee in chosen and _passes(how, *chosen[callee])
                        for callee, _, how in scan.calls
                    ):
                        chosen[name] = (parameter, scan)
                        changed = True
                        break
        if not chosen:
            return
        claims: list[ParsedReference] = []
        heads: dict[str, list[_Position | None]] = {}
        passes: dict[str, list[str]] = {}
        for name in sorted(chosen, key=lambda helper: definitions[helper].lineno):
            parameter, scan = chosen[name]
            calls: list[dict[str, object]] = []
            refusals = list(scan.refusals)
            for callee, line, how in scan.calls:
                target = chosen.get(callee)
                if target is None:
                    continue
                if _passes(how, *target):
                    calls.append({"callee": callee, "line": line})
                else:
                    refusals.append({"code": PY_HELPER_ARGUMENT, "line": line})
            heads[name] = [position for _, position in scan.mounts]
            passes[name] = [str(call["callee"]) for call in calls]
            mounts: list[dict[str, object]] = []
            for mount, _ in scan.mounts:
                entry: dict[str, object] = {
                    key: value for key, value in mount.meta.items() if key not in _HELPER_OWNER_KEYS
                }
                entry["line"] = mount.line_start
                entry["end"] = mount.line_end
                mounts.append(entry)
            line = definitions[name].lineno
            claims.append(
                pending_reference(
                    EdgeType.REFERENCES,
                    EntityType.SYMBOL,
                    name,
                    line,
                    line,
                    role=HELPER_ROLE,
                    param=str(scan.index),
                    param_name=parameter,
                    mounts=json.dumps(mounts),
                    calls=json.dumps(calls),
                    refusals=json.dumps(sorted(refusals, key=_refusal_order)),
                )
            )
        for function in tree.body:
            if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                claims.extend(self._factory_helper_calls(function, chosen))
        claims.extend(self._module_helper_calls(tree, definitions, chosen, heads, passes))
        self._helper_claims = claims

    def _scan_helper(
        self, function: ast.FunctionDef, definitions: Mapping[str, ast.FunctionDef]
    ) -> dict[str, _HelperScan]:
        """What ``function`` does, at its top level, with each positional parameter that nothing
        in it rebinds (a parameter bound twice, say to a fresh application, is no handle)."""
        arguments = function.args
        positional = [*arguments.posonlyargs, *arguments.args]
        if not positional:
            return {}
        counts, _ = _binding_counts(function)
        scans = {
            argument.arg: _HelperScan(index=index, keyword=argument in arguments.args)
            for index, argument in enumerate(positional)
            if counts[argument.arg] == 1
        }
        if not scans:
            return {}
        imported = {name: found for name, found in self._imported.items() if not counts[name]}
        lists = {name: found for name, found in self._lists.items() if not counts[name]}
        lists.update(_literal_lists(function.body, counts, function))
        aliases: dict[str, str] = {}
        for statement in function.body:
            if (
                isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
                and isinstance(statement.value, ast.Name)
                and statement.value.id in scans
                and counts[statement.targets[0].id] == 1
            ):
                aliases[statement.targets[0].id] = statement.value.id
        for statement in function.body:
            if isinstance(statement, (ast.Import, ast.ImportFrom)):
                # A local import binds its name from this statement on: a registration above it
                # reads an unbound local at run time, so it sees no entry (and no module-level one).
                imported.update(_import_table([statement], counts))
                continue
            call = _expression_call(statement)
            if (
                call is not None
                and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.attr in _MOUNT_CALLS
            ):
                receiver = call.func.value.id
                if receiver in scans:
                    self._helper_registration(scans[receiver], receiver, call, counts, imported)
                    continue
                if receiver in aliases:
                    scans[aliases[receiver]].refusals.append(
                        {"code": PY_HELPER_REBOUND_OR_ALIAS, "line": call.lineno}
                    )
                    continue
            named = _name_call(statement)
            if (
                named is not None
                and isinstance(named.func, ast.Name)
                and named.func.id in definitions
            ):
                self._helper_hand_over(named, named.func.id, scans, counts)
                continue
            if isinstance(statement, ast.For) and self._helper_loop(
                statement, scans, aliases, counts, imported, definitions, lists
            ):
                continue
            self._helper_nested(statement, scans, aliases, counts, imported, definitions)
        return scans

    def _helper_loop(
        self,
        loop: ast.For,
        scans: dict[str, _HelperScan],
        aliases: dict[str, str],
        counts: Counter[str],
        imported: dict[str, _Imported],
        definitions: Mapping[str, ast.FunctionDef],
        lists: dict[str, list[ast.expr]],
    ) -> bool:
        """``for child in children: p.include_router(child)`` as a top-level statement of a helper,
        read as one registration per element, as :func:`_statement_calls` reads a factory's loop:
        over a literal list, the loop variable bound by nothing else and the registration's first
        argument. Any other statement of its body is read as a nested one. ``False`` for a loop
        this does not unroll."""
        if loop.orelse or not isinstance(loop.target, ast.Name) or counts[loop.target.id] != 1:
            return False
        elements = _loop_elements(loop.iter, lists)
        if elements is None:
            return False
        for inner in loop.body:
            call = _expression_call(inner)
            if (
                call is not None
                and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.attr in _MOUNT_CALLS
                and call.func.value.id in scans
                and _loop_call_refusal(call, loop.target.id) is None
            ):
                receiver = call.func.value.id
                for unrolled in _unrolled(call, elements):
                    self._helper_registration(scans[receiver], receiver, unrolled, counts, imported)
                continue
            self._helper_nested(inner, scans, aliases, counts, imported, definitions)
        return True

    def _helper_registration(
        self,
        scan: _HelperScan,
        receiver: str,
        call: ast.Call,
        counts: Counter[str],
        imported: dict[str, _Imported],
    ) -> None:
        """``receiver.register_blueprint(child)`` as a top-level statement of a helper."""
        attribute = call.func.attr if isinstance(call.func, ast.Attribute) else ""
        kind = _FASTAPI_APP if attribute == "include_router" else _FLASK_APP
        found = self._helper_child(_mount_target(kind, call), counts, imported)
        if isinstance(found, str):
            scan.refusals.append({"code": found, "line": call.lineno})
            return
        child, position = found
        sink: list[ParsedReference] = []
        self._claim_mount(receiver, _Receiver(kind), call, child, counts, sink)
        scan.mounts.append((sink[0], position))

    def _helper_child(
        self, argument: ast.expr | None, counts: Counter[str], imported: dict[str, _Imported]
    ) -> tuple[dict[str, str], _Position | None] | str:
        """The child a helper registers -- this module's own receiver, or one name, one
        attribute or two attributes (R2c-2) away on an import -- and where the module binds it
        (``None``: the helper binds it); else the code of why it is not read. A child that is the
        helper's own parameter or local is an argument (the caller decides it); anything else is
        unsupported."""
        if (
            isinstance(argument, ast.Name)
            and argument.id in self._receivers
            and not counts[argument.id]
        ):
            return {"name": argument.id}, (self._receivers[argument.id].line, 0)
        child = _child_ref(argument, imported)
        if child is not None:
            node = argument
            while isinstance(node, ast.Attribute):
                node = node.value
            head = node.id if isinstance(node, ast.Name) else ""
            at = self._positions.get(head) if head in self._imported and not counts[head] else None
            return child, at
        if isinstance(argument, ast.Name) and counts[argument.id]:
            return PY_HELPER_ARGUMENT
        return PY_HELPER_CHILD_UNSUPPORTED

    @staticmethod
    def _helper_hand_over(
        call: ast.Call, callee: str, scans: dict[str, _HelperScan], counts: Counter[str]
    ) -> None:
        """``callee(p)`` of another same-module function, as a top-level statement of a helper:
        recorded, and judged once the callee's own parameter is known."""
        if _dynamic(call):
            names = {
                node.id
                for node in ast.walk(call)
                if isinstance(node, ast.Name) and node.id in scans
            }
            for name in sorted(names):
                scans[name].refusals.append({"code": PY_HELPER_ARGUMENT, "line": call.lineno})
            return
        passed = [name for name in _argument_names(call) if name in scans]
        if not passed:
            return
        if counts[callee]:
            for name in passed:
                scans[name].refusals.append({"code": PY_HELPER_SHADOWED, "line": call.lineno})
            return
        for index, argument in enumerate(call.args):
            if isinstance(argument, ast.Name) and argument.id in scans:
                scans[argument.id].calls.append((callee, call.lineno, index))
        for keyword in call.keywords:
            if (
                keyword.arg is not None
                and isinstance(keyword.value, ast.Name)
                and keyword.value.id in scans
            ):
                scans[keyword.value.id].calls.append((callee, call.lineno, keyword.arg))

    def _helper_nested(
        self,
        statement: ast.stmt,
        scans: dict[str, _HelperScan],
        aliases: dict[str, str],
        counts: Counter[str],
        imported: dict[str, _Imported],
        definitions: Mapping[str, ast.FunctionDef],
    ) -> None:
        """A registration or a hand-over somewhere other than a top-level statement of the
        helper (a branch, a loop, a nested function, an assignment): refused."""
        for node in ast.walk(statement):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.attr in _MOUNT_CALLS
            ):
                receiver = func.value.id
                if receiver in scans:
                    refusal: dict[str, object] = {
                        "code": PY_HELPER_CONTROL_FLOW,
                        "line": node.lineno,
                    }
                    kind = _FASTAPI_APP if func.attr == "include_router" else _FLASK_APP
                    found = self._helper_child(_mount_target(kind, node), counts, imported)
                    if not isinstance(found, str):
                        refusal.update(found[0])
                    scans[receiver].refusals.append(refusal)
                elif receiver in aliases:
                    scans[aliases[receiver]].refusals.append(
                        {"code": PY_HELPER_REBOUND_OR_ALIAS, "line": node.lineno}
                    )
            elif isinstance(func, ast.Name) and func.id in definitions:
                for name in sorted({name for name in _argument_names(node) if name in scans}):
                    scans[name].refusals.append(
                        {"code": PY_HELPER_CONTROL_FLOW, "line": node.lineno}
                    )

    @staticmethod
    def _passed_application(
        call: ast.Call, parameter: str, scan: _HelperScan, applications: Collection[str]
    ) -> tuple[str | None, str | None]:
        """``(application, refusal)`` of a call of a helper: the application handed over as the
        helper's own parameter, or why not. ``(None, None)``: no application is involved."""
        if not any(
            isinstance(node, ast.Name) and node.id in applications for node in ast.walk(call)
        ):
            return None, None
        if _dynamic(call):
            return None, PY_HELPER_ARGUMENT
        positional = call.args[scan.index] if len(call.args) > scan.index else None
        named = _keywords(call).get(parameter) if scan.keyword else None
        if positional is not None and named is not None:
            return None, PY_HELPER_ARGUMENT
        argument = positional if positional is not None else named
        if isinstance(argument, ast.Name) and argument.id in applications:
            return argument.id, None
        return None, PY_HELPER_ARGUMENT

    @staticmethod
    def _call_claim(
        call: ast.Call,
        helper: str,
        owner: str,
        kind: str,
        scope: str,
        refusal: str | None,
        owner_classes: tuple[dict[str, str], ...] = (),
    ) -> ParsedReference:
        """A call of a recordable helper with an application: the helper, the application's
        owner name (the key the global pass mounts under) and kind -- or the classes other modules
        define it may be built from, which the global pass must prove one kind -- and the
        refusal, if any."""
        refused: dict[str, str] = {} if refusal is None else {"refusal": refusal}
        if owner_classes:
            refused["owner_classes"] = json.dumps(owner_classes, sort_keys=True)
        return pending_reference(
            EdgeType.REFERENCES,
            EntityType.SYMBOL,
            helper,
            call.lineno,
            call.end_lineno or call.lineno,
            role=HELPER_CALL_ROLE,
            owner=owner,
            owner_kind=kind,
            scope=scope,
            readable="true",
            **refused,
        )

    def _factory_helper_calls(
        self,
        function: ast.FunctionDef | ast.AsyncFunctionDef,
        chosen: Mapping[str, tuple[str, _HelperScan]],
    ) -> list[ParsedReference]:
        """Calls of a recordable helper with an application the function builds (R-6)."""
        counts, _ = _binding_counts(function)
        applications = self._function_applications(function, counts)
        if not applications:
            return []
        found: list[ParsedReference] = []
        for index, statement in enumerate(function.body):
            call = _name_call(statement)
            if call is not None and isinstance(call.func, ast.Name) and call.func.id in chosen:
                name = call.func.id
                parameter, scan = chosen[name]
                app, refusal = self._passed_application(call, parameter, scan, applications)
                if app is None and refusal is None:
                    continue
                if counts[name]:
                    refusal = PY_HELPER_SHADOWED
                elif refusal is None and app is not None and applications[app].index > index:
                    refusal = PY_HELPER_BEFORE_CONSTRUCTION
                if app is None and len(applications) == 1:
                    app = next(iter(applications))
                built = applications[app] if app is not None else None
                found.append(
                    self._call_claim(
                        call,
                        name,
                        f"{function.name}.{app}" if app is not None else "",
                        (built.kind or "") if built is not None else "",
                        "factory",
                        refusal,
                        built.classes if built is not None else (),
                    )
                )
                continue
            for node in ast.walk(statement):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in chosen
                ):
                    passed = sorted({n for n in _argument_names(node) if n in applications})
                    if passed:
                        built = applications[passed[0]]
                        found.append(
                            self._call_claim(
                                node,
                                node.func.id,
                                f"{function.name}.{passed[0]}",
                                built.kind or "",
                                "factory",
                                PY_HELPER_CONTROL_FLOW,
                                built.classes,
                            )
                        )
        return found

    def _module_helper_calls(
        self,
        tree: ast.Module,
        definitions: Mapping[str, ast.FunctionDef],
        chosen: Mapping[str, tuple[str, _HelperScan]],
        heads: Mapping[str, list[_Position | None]],
        passes: Mapping[str, list[str]],
    ) -> list[ParsedReference]:
        """Calls of a recordable helper with a module-level application (R-7): a top-level
        statement of the module, after the application, the helper and every child it
        registers are bound."""
        applications = {
            name: receiver
            for name, receiver in self._receivers.items()
            if receiver.kind in _APPLICATIONS
        }
        if not applications:
            return []
        found: list[ParsedReference] = []
        for statement in tree.body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            call = _name_call(statement)
            if call is not None and isinstance(call.func, ast.Name) and call.func.id in chosen:
                name = call.func.id
                parameter, scan = chosen[name]
                app, refusal = self._passed_application(call, parameter, scan, applications)
                if app is None and refusal is None:
                    continue
                if refusal is None and app is not None:
                    at = (call.lineno, call.col_offset)
                    if applications[app].line >= call.lineno:
                        refusal = PY_HELPER_BEFORE_CONSTRUCTION
                    elif definitions[name].lineno >= call.lineno or self._children_unbound(
                        name, at, heads, passes
                    ):
                        refusal = PY_HELPER_BEFORE_BINDING
                if app is None and len(applications) == 1:
                    app = next(iter(applications))
                kind = applications[app].kind if app is not None else ""
                found.append(self._call_claim(call, name, app or "", kind, "module", refusal))
                continue
            for node in ast.walk(statement):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in chosen
                ):
                    passed = sorted({n for n in _argument_names(node) if n in applications})
                    if passed:
                        found.append(
                            self._call_claim(
                                node,
                                node.func.id,
                                passed[0],
                                applications[passed[0]].kind,
                                "module",
                                PY_HELPER_CONTROL_FLOW,
                            )
                        )
        return found

    @staticmethod
    def _children_unbound(
        helper: str,
        at: _Position,
        heads: Mapping[str, list[_Position | None]],
        passes: Mapping[str, list[str]],
    ) -> bool:
        """Whether a child the helper -- or a helper it hands the application to -- registers is
        bound at or after ``at`` in the module (a name the module imports later is no name yet)."""
        seen = {helper}
        queue = [helper]
        for current in queue:
            if any(position is not None and position >= at for position in heads.get(current, ())):
                return True
            for callee in passes.get(current, ()):
                if callee not in seen:
                    seen.add(callee)
                    queue.append(callee)
        return False

    def _unserve_sub_applications(self) -> None:
        """Nothing is served from a router included on a sub-application.

        A sub-application is one that this module, or one of its factories, mounts on
        another application: the parent serves it under the mount's path, which is not the
        include's, and the parent's route is not the include's either. A router included on
        one -- in this module, or by a claim only the global pass can read -- is served
        nowhere, as one whose mount this module cannot read is.
        """
        owners = {
            name
            for name, receiver in self._receivers.items()
            if receiver.kind in _APPLICATIONS and not receiver.usable
        } | self._sub_owners
        if not owners:
            return
        for receiver in self._receivers.values():
            if any(owner in owners for _, owner in receiver.mounts):
                receiver.usable = False
        self._mount_claims = [self._unserved(claim, owners) for claim in self._mount_claims]
        # A helper called with a sub-application registers unreadably, as a mount on one is.
        self._helper_claims = [self._unserved(claim, owners) for claim in self._helper_claims]

    @staticmethod
    def _unserved(claim: ParsedReference, owners: set[str]) -> ParsedReference:
        """A mount made on a sub-application is unreadable, and its application mount path
        is dropped: the parent that serves its owner under another path is not this owner's."""
        if claim.meta.get("owner") not in owners:
            return claim
        meta = {key: value for key, value in claim.meta.items() if key != "mount_prefix"}
        if meta.get("readable") == "true":
            meta["readable"] = "false"
        if meta == claim.meta:
            return claim
        return claim.model_copy(update={"meta": meta})

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
                self._positions[name] = _position(statement)
            default = self._env_default(value) if value is not None else None
            if default is not None and counts[name] == 1:
                self._env_defaults[name] = (default, statement.lineno)

    def _read_values(self, tree: ast.Module, counts: Counter[str]) -> None:
        """Module constants composed from literals and other names, in execution order.

        ``USERS = f"{BASE}/users"`` and ``P = BASE + "/v1"`` stand for the text they would
        build when everything they read is proven: this module's literal constants and earlier
        values (bound once, before the statement), and imported names, kept as names for the
        global pass. Each assignment is read where it stands, so a read of a name bound
        later is refused. What is not accepted is recorded in ``value_refusals``.
        """
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
            if name in self._constants:
                continue
            if counts[name] != 1:
                self.value_refusals[name] = (
                    _STAR_MAY_REBIND if name in (self.star_unproven or ()) else _REBOUND
                )
                continue
            at = _position(statement)
            found = self._evaluate(value, _DEFINITION, at, None)
            if isinstance(found, _Proven):
                # A value always reads a name: a text with none is a literal constant.
                if 1 + found.depth > _MAX_VALUE_DEPTH:
                    found = _Unknown(_TOO_DEEP)
                elif _literal_length(found.parts) > _MAX_CONSTANT:
                    found = _Unknown(_TOO_LONG)
            if isinstance(found, _Unknown):
                self.value_refusals[name] = found.reason
                continue
            self._values[name] = _Value(
                tuple(tuple(part) for part in found.parts),
                statement.lineno,
                at,
                1 + found.depth,
                found.sources,
            )
            self._positions[name] = at

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
        serves wherever it proves a mount and the values. So is a literal route on a FastAPI
        application, marked ``served_here``: the global pass adds the paths of the parents
        that mount the application, and nothing else.
        serves wherever it proves a mount and the values. One on an application built from a
        class another module defines is only a :data:`PYTHON_CLASS_ROUTE` claim.
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
                built = self._class_receivers.get(func.value.id)
                if built is not None:
                    if built.usable:
                        found.extend(
                            self._class_route(func.value.id, built, func.attr, decorator, qualified)
                        )
                    continue
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
                proven = (
                    _Proven([["t", path]], (), 0, False)
                    if path is not None
                    else self._path_parts(path_node, decorator)
                )
                if proven is not None:
                    found.extend(
                        _route_claim(
                            {"router": func.value.id, "family": source},
                            methods,
                            proven.parts,
                            (start, end),
                            qualified,
                            _by_line(proven.sources),
                            proven.composed,
                        )
                    )
            elif receiver.kind == _FASTAPI_APP:
                # Served here, as stated; and, where another file mounts this application
                # under a prefix, at the parent's paths too: a claim for the global pass.
                found.extend(
                    _route_claim(
                        {"router": func.value.id, "family": source, "served_here": "true"},
                        methods,
                        [["t", path]],
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
        proven = self._path_parts(_argument(decorator, 0, named), decorator)
        if proven is None and family[0] == "either":
            # A verb shortcut names its path `path` in FastAPI and `rule` in Flask.
            proven = self._path_parts(_argument(decorator, 0, "path"), decorator)
        if proven is None:
            return []
        span = (decorator.lineno, decorator.end_lineno or decorator.lineno)
        meta = {"module": ref[0], "router": ref[1], "family": family[0]}
        return _route_claim(
            meta,
            family[1],
            proven.parts,
            span,
            qualified,
            _by_line(proven.sources),
            proven.composed,
        )

    def _class_route(
        self,
        name: str,
        built: _ClassReceiver,
        attribute: str,
        decorator: ast.Call,
        qualified: str,
    ) -> list[ParsedReference]:
        """A route on an application built from a class another module defines.

        Read under whatever both frameworks agree on, as a route on an imported receiver is:
        the application's kind is the class's, which the global pass checks.
        """
        family = _family(attribute, decorator)
        if family is None:
            return []
        named = "path" if family[0] == "fastapi" else "rule"
        proven = self._path_parts(_argument(decorator, 0, named), decorator)
        if proven is None and family[0] == "either":
            # A verb shortcut names its path `path` in FastAPI and `rule` in Flask.
            proven = self._path_parts(_argument(decorator, 0, "path"), decorator)
        if proven is None:
            return []
        span = (decorator.lineno, decorator.end_lineno or decorator.lineno)
        meta = {
            "module": built.ref.module,
            "class": built.ref.name,
            "depth": str(built.depth),
            "family": family[0],
            "ctor": json.dumps(built.ctor, sort_keys=True),
        }
        via = self.imported_subclasses.get(built.via) if built.via is not None else None
        if via is not None and via.inits:
            meta["init"] = json.dumps(via.inits, sort_keys=True)
        return _class_route_claim(
            name,
            meta,
            family[1],
            proven.parts,
            span,
            qualified,
            _by_line(proven.sources),
            proven.composed,
        )

    def _path_parts(self, node: ast.expr | None, decorator: ast.Call) -> _Proven | None:
        """A path as literal text and names: ``f"/{PREFIX}/{API_VERSION}" + "/x"``.

        Each part is ``["t", text]`` or ``["r", module, name]`` for a name some module this
        one imports must bind to a string constant. This module's own constants and composed
        constants are read here; anything else -- a call, an attribute of an object, ``%``,
        ``.format``, a conversion or format spec -- is not a path this can read, and why is
        recorded in ``path_refusals``.
        """
        if node is None:
            return None
        found = self._evaluate(node, _ROUTE, (decorator.lineno, decorator.col_offset), None)
        if isinstance(found, _Unknown):
            self.path_refusals.append((decorator.lineno, found.reason))
            return None
        return found

    def _prefix_value(
        self, node: ast.expr, at: _Position, shadow: Counter[str] | None = None
    ) -> tuple[str, tuple[tuple[str, int], ...]] | None:
        """A router's, blueprint's or mount's prefix, and the module constants it stands on.

        A literal is read as :func:`static_string` reads it. Otherwise the expression is
        evaluated in execution order from ``at``, or -- in a factory, whose ``shadow`` is
        its own bindings -- with no order. A prefix naming an imported constant is not read
        here (the module's own constants cannot be told from them): the site stays
        unreadable, as it was.
        """
        text = static_string(node)
        if text is not None:
            return text, ()
        found = self._evaluate(node, _PREFIX if shadow is None else _FACTORY, at, shadow)
        if isinstance(found, _Proven):
            if any(part[0] == "r" for part in found.parts):
                found = _Unknown(_PREFIX_IMPORTED)
            elif _literal_length(found.parts) > _MAX_CONSTANT:
                found = _Unknown(_TOO_LONG)
        if isinstance(found, _Unknown):
            self.prefix_refusals.append((node.lineno, found.reason))
            return None
        return "".join(part[1] for part in found.parts), _by_line(found.sources)

    # -- evaluating a string ---------------------------------------------------------

    def _evaluate(
        self,
        node: ast.expr | None,
        mode: str,
        at: _Position | None,
        shadow: Counter[str] | None,
        depth: int = 0,
    ) -> _Proven | _Unknown:
        """The string ``node`` provably is, from literals and names, or why it is not one.

        Accepted: a string literal; an f-string whose substitutions are names, with no
        conversion and no format spec; ``+`` of two accepted operands, at most
        :data:`_MAX_STRING_DEPTH` deep; and a name or ``module.NAME``, by
        :meth:`_named_part`. Anything else -- a call, ``os.environ[...]``, ``.format``,
        ``%``, a conditional expression -- depends on a program that is never run.
        """
        if node is None:
            return _Unknown(_UNSUPPORTED)
        if depth > _MAX_STRING_DEPTH:
            return _Unknown(_TOO_NESTED)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                return _Proven([["t", node.value]], (), 0, False)
            return _Unknown(_NOT_A_STRING)
        if isinstance(node, ast.JoinedStr):
            return self._interpolated(node, mode, at, shadow)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = self._evaluate(node.left, mode, at, shadow, depth + 1)
            if isinstance(left, _Unknown):
                return left
            right = self._evaluate(node.right, mode, at, shadow, depth + 1)
            return right if isinstance(right, _Unknown) else _joined(left, right)
        if isinstance(node, (ast.Name, ast.Attribute)):
            return self._named_part(node, mode, at, shadow)
        return _Unknown(_UNSUPPORTED)

    def _interpolated(
        self, node: ast.JoinedStr, mode: str, at: _Position | None, shadow: Counter[str] | None
    ) -> _Proven | _Unknown:
        found = _Proven([], (), 0, False)
        for piece in node.values:
            if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                addition: _Proven | _Unknown = _Proven([["t", piece.value]], (), 0, False)
            elif isinstance(piece, ast.FormattedValue):
                # `f"{A=}"` is a literal "A=" and a repr conversion: it lands here too.
                if piece.conversion != -1 or piece.format_spec is not None:
                    return _Unknown(_CONVERSION)
                if not isinstance(piece.value, (ast.Name, ast.Attribute)):
                    return _Unknown(_UNSUPPORTED)
                addition = self._named_part(piece.value, mode, at, shadow)
            else:
                return _Unknown(_UNSUPPORTED)
            if isinstance(addition, _Unknown):
                return addition
            joined = _joined(found, addition)
            if isinstance(joined, _Unknown):
                return joined
            found = joined
        return found

    def _named_part(
        self,
        node: ast.Name | ast.Attribute,
        mode: str,
        at: _Position | None,
        shadow: Counter[str] | None,
    ) -> _Proven | _Unknown:
        """A name this module binds once to a string, or an imported one, as pieces.

        A literal constant is its text; a composed constant (:class:`_Value`) is its pieces,
        spliced as stored; an imported name, or ``module.NAME``, stays a name for the global
        pass. Anything else is refused with the reason it is not a binding to rely on.
        """
        root: ast.expr = node
        while isinstance(root, ast.Attribute):
            root = root.value
        if not isinstance(root, ast.Name):
            return _Unknown(_UNSUPPORTED)
        name = root.id
        if shadow is not None and shadow[name]:
            return _Unknown(_SHADOWED)
        if isinstance(node, ast.Name) and name in self._constants:
            text, line = self._constants[name]
            if not self._read_in_order(name, mode, at, composed=False):
                return _Unknown(_UNORDERED)
            return _Proven([["t", text]], ((name, line),), 0, False)
        value = self._values.get(name) if isinstance(node, ast.Name) else None
        if value is not None:
            if not self._read_in_order(name, mode, at, composed=True):
                return _Unknown(_UNORDERED)
            return _Proven(
                [list(part) for part in value.parts],
                tuple(dict.fromkeys(((name, value.line), *value.sources))),
                value.depth,
                True,
            )
        ref = _module_ref(node, self._imported)
        if ref is not None:
            if not self._read_in_order(name, mode, at, composed=False):
                return _Unknown(_UNORDERED)
            return _Proven([["r", ref[0], ref[1]]], (), 0, False)
        if name in (self.star_unproven or ()):
            return _Unknown(_STAR_MAY_REBIND)
        return _Unknown(_REBOUND if self._counts[name] > 1 else _UNKNOWN_NAME)

    def _read_in_order(self, name: str, mode: str, at: _Position | None, *, composed: bool) -> bool:
        """Whether ``name`` is bound before a read at ``at``, where the mode asks it.

        A composed constant is held to it wherever it is read, except by a factory's mount. A
        route path and a client's request URL or base read a literal constant or an import with
        no such check, as a route path always has.
        """
        if at is None or mode == _FACTORY or (mode in {_ROUTE, _CLIENT} and not composed):
            return True
        bound = self._positions.get(name)
        return bound is not None and bound < at

    def claim_references(self) -> list[ParsedReference]:
        """What only the global pass can decide: receivers, mounts, offered constants, and the
        application classes this module offers (:data:`PYTHON_CLASS`)."""
        found: list[ParsedReference] = []
        for name, receiver in self._receivers.items():
            read_from = (
                {"prefix_from": json.dumps(receiver.prefix_from)} if receiver.prefix_from else {}
            )
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
                    **read_from,
                )
            )
        found.extend(self._mount_claims)
        found.extend(self._helper_claims)
        for name, subclass in self.subclasses.items():
            inits = {"init": json.dumps(subclass.inits, sort_keys=True)} if subclass.inits else {}
            found.append(
                pending_reference(
                    EdgeType.REFERENCES,
                    EntityType.SYMBOL,
                    name,
                    subclass.line,
                    subclass.line,
                    role=PYTHON_CLASS,
                    kind=subclass.kind,
                    depth=str(subclass.depth),
                    **inits,
                )
            )
        for name, imported in self.imported_subclasses.items():
            inits = {"init": json.dumps(imported.inits, sort_keys=True)} if imported.inits else {}
            found.append(
                pending_reference(
                    EdgeType.REFERENCES,
                    EntityType.SYMBOL,
                    name,
                    imported.line,
                    imported.line,
                    role=PYTHON_CLASS,
                    kind=IMPORTED_CLASS,
                    depth=str(imported.depth),
                    base_module=imported.ref.module,
                    base_class=imported.ref.name,
                    **inits,
                )
            )
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
        composed = self._offered_values()
        if offered or composed:
            lines = [line for line, _, _ in offered] + [line for line, _, _ in composed]
            offered_composed: dict[str, str] = {}
            if composed:
                table = {name: entry for _, name, entry in composed}
                offered_composed["composed"] = json.dumps(table, sort_keys=True)
            found.append(
                pending_reference(
                    EdgeType.REFERENCES,
                    EntityType.CONFIG,
                    PYTHON_CONSTANTS,
                    min(lines),
                    max(lines),
                    role=PYTHON_CONSTANTS,
                    constants=json.dumps(
                        {name: [value, line] for line, name, value in offered}, sort_keys=True
                    ),
                    **offered_composed,
                )
            )
        return found

    def _offered_values(self) -> list[tuple[int, str, list[object]]]:
        """The composed constants another module may read, as (line, name, entry), by line.

        Each entry is ``[parts, line, sources]``. Offered only when it could be a piece of a
        path, as a literal constant is (some text, no whitespace, short), and small enough to
        store; the first :data:`_MAX_CONSTANTS` in line order.
        """
        found: list[tuple[int, str, list[object]]] = []
        for name, value in self._values.items():
            texts = [part[1] for part in value.parts if part[0] == "t"]
            if not any(texts) or _literal_length(value.parts) > _MAX_CONSTANT:
                continue
            if any(char.isspace() for text in texts for char in text):
                continue
            entry: list[object] = [value.parts, value.line, _by_line(value.sources)]
            if len(json.dumps(entry)) <= _MAX_OFFERED_ENTRY:
                found.append((value.line, name, entry))
        return sorted(found, key=lambda item: (item[0], item[1]))[:_MAX_CONSTANTS]

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
        today = self._client_url(url_node)
        if today is None and unread_host:
            today = self._unknown_base(url_node)
        # What composing module constants reads, kept only where it improves on the above.
        composition = self._compose_client(url_node, unread_host=unread_host, client=client)
        candidate: _ClientUrl | None = None
        if composition is not None:
            if composition.rooted is not None and client is not None:
                candidate = self._rooted_url(client, composition.rooted)
            else:
                candidate = composition.url
        read = self._prefer(today, candidate)
        self._settle(composition, candidate is not None and read is candidate)
        if read is None:
            return []
        base_expr: str | None = None
        if read is candidate and composition is not None and composition.rooted is not None:
            # Appended to the client's base, as a rooted request is: the base's note goes with it.
            base_expr = client.base_expr if client is not None else None
        claim = self._client_claim(
            read, method, start, end, library, source_symbol, client=client, base_expr=base_expr
        )
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
            base = self._client_base(keywords["base_url"], (value.lineno, value.col_offset))
        if base is None:
            return None
        kind, text, base_at, expression = base
        return _ClientInstance(
            _HTTPX, kind, text, base_at, expression, value.lineno, name, bound_at, reach
        )

    def _client_base(self, node: ast.expr, at: _Position) -> _ClientBase | None:
        """What the source states of a client's ``base_url``: its kind, the base, its line and
        (for an unstated one) its expression; ``None`` for a base that makes the client unreadable.

        A literal, a module constant bound once, or an environment default is stated -- as
        a request's own URL is -- and must be an absolute URL with no query, fragment or
        credentials. An empty one is no base at all, unless the environment may set it. Any
        other expression is a base the source does not state -- unless it is composed of
        module constants alone (:meth:`_composed_base`), read at ``at``.
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
            composed = self._composed_base(node, at)
            if composed is not None:
                return composed
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
            composition = None
            if url is None and not self._alternative_urls(url_node):
                composition = self._compose_client(
                    url_node, unread_host=client.base_kind == _NO_BASE, client=client
                )
            return self._composed(
                client, parts, method, call.lineno, end, source_symbol, composition
            )
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
        composition: _Composition | None = None,
    ) -> list[ParsedReference]:
        """A rooted request on a client that has a base.

        Appended to a base the source states, as ``httpx`` does (its trailing ``/`` is not a
        second one): a claim with the origin and the base's line, never a per-file fact.
        On a base it does not state, the host part is the synthetic ``{name.base_url}``: a
        claim with no origin, which the join counts and never links. ``composition`` is what
        composing module constants made of the same URL, kept only where it improves on this.
        """
        today = self._rooted_url(client, parts)
        candidate: _ClientUrl | None = None
        if composition is not None and composition.rooted is not None:
            candidate = self._rooted_url(client, composition.rooted)
        read = self._prefer(today, candidate)
        self._settle(composition, candidate is not None and read is candidate)
        if read is None:
            return []
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

    @staticmethod
    def _rooted_url(client: _ClientInstance, parts: list[list[str]]) -> _ClientUrl | None:
        """A rooted URL's parts read on a client's base, or ``None`` when no destination follows.

        On a base the source states: appended to it, with the origin and the base's line. On
        one it does not: the host part is the synthetic ``{name.base_url}``, with no origin.
        """
        origin: str | None = None
        host = "{" + f"{client.name}.base_url" + "}"
        if client.base_kind == _STATED_BASE and client.base_text is not None:
            base = http.client_target(client.base_text)
            if base is None or base.origin is None:
                return None
            origin = host = base.origin
            parts = [[http.TEMPLATE_LITERAL, base.path.rstrip("/") + parts[0][1]], *parts[1:]]
        elif client.base_kind != _UNSTATED_BASE:
            return None
        template = http.route_template(parts)
        if isinstance(template, str) or _dotted(template.segments):
            return None
        display = f"{host}/{_display(template)}"
        return _ClientUrl(origin, template.segments, display, client.base_at)

    @staticmethod
    def _prefer(today: _ClientUrl | None, candidate: _ClientUrl | None) -> _ClientUrl | None:
        """The read to keep: the older one unless composing constants reads strictly more.

        Nothing composed is taken where the older read already says the same (same origin and
        segments: the older object stays, display and line included), or where it has an origin
        and the candidate has none. A candidate with an origin replaces an older read that has
        none; either one stands alone when the other is ``None``.
        """
        if candidate is None:
            return today
        if today is None:
            return candidate
        if (today.origin, today.segments) == (candidate.origin, candidate.segments):
            return today
        if candidate.origin is not None or today.origin is None:
            return candidate
        return today

    @staticmethod
    def _settle(composition: _Composition | None, kept: bool) -> None:
        """Record that a composed read changed nothing, when the older read was kept."""
        if composition is not None and not kept:
            read = composition.read
            read.outcome = _CLIENT_IMPORTED if read.imported else _UNCHANGED

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

    # -- composing a request's URL or a client's base from module constants ------------------

    def _client_pieces(self, node: ast.expr, at: _Position) -> list[_ClientPiece] | str | None:
        """The pieces of a URL or base written as an f-string, a ``+`` chain or one name.

        ``None`` for an expression this does not try to compose (a call, a conditional, a
        subscript...); a reason when the attempt gives up (more than :data:`_MAX_VALUE_PARTS`
        pieces, counting the parts of a composed constant; more than :data:`_MAX_CLIENT_TEXT`
        characters; a conversion or format spec; an operand that is no string). A name is read by
        :meth:`_named_part` at ``at`` and is a proven piece only for a literal constant, a
        composed constant bound before ``at`` standing on no imported name, or -- first only --
        an environment default; every other name, call or expression stays open, as written.
        """
        operands: list[ast.expr]
        if isinstance(node, ast.JoinedStr | ast.Name):
            operands = [node]
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            added = _added(node)
            if added is None:
                return _TOO_MANY_PARTS
            operands = added
        else:
            return None
        pieces: list[_ClientPiece] = []
        weight = length = 0
        for operand in operands:
            inside = isinstance(operand, ast.JoinedStr)
            for value in operand.values if isinstance(operand, ast.JoinedStr) else [operand]:
                piece = self._client_piece(value, inside=inside, first=not pieces, at=at)
                if isinstance(piece, str):
                    return piece
                weight += len(piece.parts) if piece.kind == _PROVEN else 1
                length += len(piece.text) if piece.kind != _OPEN else 0
                if weight > _MAX_VALUE_PARTS:
                    return _TOO_MANY_PARTS
                if length > _MAX_CLIENT_TEXT:
                    return _TOO_LONG
                pieces.append(piece)
        return pieces

    def _client_piece(
        self, value: ast.expr, *, inside: bool, first: bool, at: _Position
    ) -> _ClientPiece | str:
        """One operand of a ``+`` chain, or one value of an f-string (``inside``)."""
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return _ClientPiece(_LITERAL, value.value)
        if inside:
            # A conversion or a format spec is not read: whatever it makes is not the name's text.
            if (
                not isinstance(value, ast.FormattedValue)
                or value.conversion != -1
                or value.format_spec is not None
            ):
                return _UNCHANGED
            inner = value.value
            if isinstance(inner, ast.Name | ast.Attribute):
                return self._client_name(inner, first, at)
            return _ClientPiece(_OPEN, ast.unparse(inner)[:_MAX_EXPRESSION])
        if isinstance(value, ast.Name | ast.Attribute):
            return self._client_name(value, first, at)
        if isinstance(value, ast.Subscript | ast.Call):
            return _ClientPiece(_OPEN, ast.unparse(value)[:_MAX_EXPRESSION])
        return _UNCHANGED

    def _client_name(
        self, node: ast.Name | ast.Attribute, first: bool, at: _Position
    ) -> _ClientPiece | str:
        shown = ast.unparse(node)[:_MAX_EXPRESSION]
        found = self._named_part(node, _CLIENT, at, None)
        if isinstance(found, _Proven):
            if any(part[0] == "r" for part in found.parts):
                return _ClientPiece(_OPEN, shown, reason=_CLIENT_IMPORTED)
            if 1 + found.depth > _MAX_VALUE_DEPTH:
                return _TOO_DEEP
            parts = tuple(part[1] for part in found.parts)
            line = found.sources[0][1]
            return _ClientPiece(_PROVEN, "".join(parts), parts, line, found.depth, shown)
        # An environment default is a stated base only as the first piece, as a request's own
        # URL reads it; an empty one is no host at all.
        default = self._env_defaults.get(node.id) if first and isinstance(node, ast.Name) else None
        if default is not None and default[0] and found.reason == _UNKNOWN_NAME:
            return _ClientPiece(_PROVEN, default[0], (default[0],), default[1], 0, shown)
        return _ClientPiece(_OPEN, shown, reason=found.reason)

    def _compose_client(
        self, node: ast.expr, *, unread_host: bool, client: _ClientInstance | None
    ) -> _Composition | None:
        """What a request's URL says when its module constants are composed, or ``None``.

        Four forms, all within this file. An absolute URL whose authority is text and proven
        names has an origin, as a literal would. One whose authority has a value in it (a host
        or a port) is a request with no origin that keeps its path after it, and so is one
        that starts with a value and then ``/`` -- both only where ``unread_host`` (the older
        D2 read applies). A URL starting with ``/`` is rooted: a client's base is appended
        to it, and a module-level call has no destination. Every refusal is recorded in
        :attr:`client_reads` with its reason and leaves the older read alone.
        """
        pieces = self._client_pieces(node, (node.lineno, node.col_offset))
        if pieces is None:
            return None
        read = _ClientRead(node.lineno, _COMPOSED)
        self.client_reads.append(read)
        if isinstance(pieces, str):
            read.outcome = pieces
            return None
        read.imported = any(piece.reason == _CLIENT_IMPORTED for piece in pieces)
        walked = _walk_client(pieces)
        if isinstance(walked, str):
            read.outcome = walked
            return None
        if walked.form == "rooted":
            if client is None:
                read.outcome = _RELATIVE_URL
                return None
            shaped = http.route_template(walked.parts)
            if not isinstance(shaped, str):
                read.endpoint_segments = shaped.segments
            return _Composition(read, rooted=walked.parts)
        opened = walked.form == "open" or any(kind == _OPEN for kind, _ in walked.authority)
        if opened and not unread_host:
            read.outcome = _UNCHANGED
            return None
        if walked.form == "absolute" and not walked.slashed and opened:
            read.outcome = _NO_PATH
            return None
        texts = [text for kind, text in walked.authority if kind != _OPEN]
        if "@" in "".join(texts):
            read.outcome = _CREDENTIALS
            return None
        if opened and not all(_AUTHORITY_TEXT.fullmatch(text) for text in texts):
            read.outcome = _NOT_AN_ORIGIN
            return None
        template = http.route_template(walked.parts or [[http.TEMPLATE_LITERAL, "/"]])
        if isinstance(template, str):
            read.outcome = template
            return None
        if _dotted(template.segments):
            read.outcome = _DOTTED_SEGMENT
            return None
        if walked.form == "open":
            read.endpoint_segments = template.segments
            shown = "{" + walked.host + "}"
            return _Composition(
                read, url=_ClientUrl(None, template.segments, f"{shown}/{_display(template)}")
            )
        if opened:
            read.scheme = walked.scheme[:-3].lower()
            read.endpoint_segments = template.segments
            shown = "".join(
                text if kind != _OPEN else "{" + text + "}" for kind, text in walked.authority
            )
            display = f"{walked.scheme}{shown}/{_display(template)}"
            return _Composition(read, url=_ClientUrl(None, template.segments, display))
        target = http.client_target(f"{walked.scheme}{''.join(texts)}/")
        if target is None or target.origin is None:
            read.outcome = _NOT_AN_ORIGIN
            return None
        read.scheme, read.host, read.port = _origin_parts(target.origin)
        head = tuple(segment for segment in walked.head_path.split("/") if segment)
        read.base_segments = head
        read.endpoint_segments = template.segments[len(head) :]
        display = f"{target.origin}/{_display(template)}"
        return _Composition(
            read, url=_ClientUrl(target.origin, template.segments, display, walked.base_at)
        )

    def _composed_base(self, node: ast.expr, at: _Position) -> _ClientBase | None:
        """A ``base_url`` the source composes entirely from module constants: it is then stated.

        Only text and proven names, an absolute URL that is a base as written
        (:meth:`_base_url`). Anything with an open piece, or a base that is no base, is
        not read, and the client keeps the base it had: unstated, never dropped. The line is the
        constant's when the base is one name, and the expression's when it is written inline.
        """
        pieces = self._client_pieces(node, at)
        if pieces is None:
            return None
        read = _ClientRead(node.lineno, _COMPOSED)
        self.client_reads.append(read)
        if isinstance(pieces, str):
            read.outcome = pieces
            return None
        read.imported = any(piece.reason == _CLIENT_IMPORTED for piece in pieces)
        walked = _walk_client(pieces)
        if isinstance(walked, str):
            read.outcome = walked
            return None
        if walked.has_open:
            read.outcome = _CLIENT_IMPORTED if read.imported else _UNCHANGED
            return None
        if walked.form != "absolute":
            read.outcome = _RELATIVE_URL
            return None
        authority = "".join(text for _, text in walked.authority)
        text = f"{walked.scheme}{authority}{walked.parts[0][1] if walked.parts else ''}"
        if not self._base_url(text):
            read.outcome = _NOT_AN_ORIGIN
            return None
        target = http.client_target(text)
        if target is not None and target.origin is not None:
            read.scheme, read.host, read.port = _origin_parts(target.origin)
            read.base_segments = tuple(segment for segment in target.path.split("/") if segment)
        line = walked.base_at if isinstance(node, ast.Name) and walked.base_at else node.lineno
        return _STATED_BASE, text, line, None
