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

A parameter, a reassignment, a ``global`` or a ``from x import *`` that could
rebind the name makes it unproven, and an unproven name yields nothing.

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

Parsed, never imported: this reads the syntax tree only (V1_SPEC §17).
"""

from __future__ import annotations

import ast
import json
import re
from collections import Counter
from collections.abc import Iterator
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


def _binding_counts(tree: ast.AST) -> Counter[str] | None:
    """How many times each name is bound anywhere in the module (or function).

    ``None`` when a star import makes the answer unknowable for every name.
    """
    counts: Counter[str] = Counter()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            counts[node.id] += 1
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            counts[node.name] += 1
        elif isinstance(node, ast.arg):
            counts[node.arg] += 1
        elif isinstance(node, ast.alias):
            if node.name == "*":
                return None
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
    return counts


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
        #: Local name → where it is imported from, for every import bound exactly once.
        self._imported: dict[str, _Imported] = {}
        #: Mounts only the global pass can decide: an imported router, or a factory's.
        self._mount_claims: list[ParsedReference] = []
        #: Module-level names bound once to a list or tuple display.
        self._lists: dict[str, list[ast.expr]] = {}
        #: The line of a module-level ``if __name__ == "__main__":``, when there is one.
        self._script = next(
            (statement.lineno for statement in tree.body if _main_guard(statement)), None
        )
        counts = _binding_counts(tree)
        if counts is None:
            return
        self._imports(tree, counts)
        self._optional_imports(tree, counts)
        self._read_constants(tree, counts)
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
            counts = _binding_counts(function)
            if counts is None:
                continue
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
        """The request a ``requests`` or ``httpx`` module-level call provably makes."""
        resolved = self._resolve(call.func)
        if resolved is None:
            return []
        library, _, function = resolved.partition(".")
        if library not in _CLIENT_LIBRARIES or any(
            isinstance(arg, ast.Starred) for arg in call.args
        ):
            return []
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
            return []
        if method is None or url_node is None:
            return []
        end = max(
            node.end_lineno or node.lineno for node in (url_node, method_node) if node is not None
        )
        url = static_string(url_node)
        if url is None:
            # Not literal: at most a claim for the join, never a per-file fact.
            read = self._client_url(url_node) or self._unknown_base(url_node)
            if read is None:
                return []
            return [self._client_claim(read, method, call.lineno, end, library, source_symbol)]
        target = http.client_target(url)
        # Neither library resolves a relative URL: it names no request at all.
        if target is None or target.url is None or target.origin is None:
            return []
        literal = _ClientUrl(target.origin, tuple(target.path[1:].split("/")), target.url)
        return [
            http_reference(
                EdgeType.CALLS_HTTP_ENDPOINT,
                EntityType.HTTP_ENDPOINT,
                http.endpoint_body(method, target.url),
                call.lineno,
                end,
                source=library,
                composed=False,
                source_symbol=source_symbol,
            ),
            http_reference(
                EdgeType.CALLS_HTTP_ROUTE,
                EntityType.HTTP_ROUTE,
                http.route_body(method, target.path),
                call.lineno,
                end,
                source=library,
                composed=False,
                source_symbol=source_symbol,
            ),
            self._client_claim(literal, method, call.lineno, end, library, source_symbol),
        ]

    @staticmethod
    def _client_claim(
        read: _ClientUrl,
        method: str,
        start: int,
        end: int,
        library: str,
        source_symbol: str | None,
    ) -> ParsedReference:
        meta = {
            "method": method,
            "segments": json.dumps(list(read.segments)),
            "source": library,
        }
        if read.origin is None:
            meta["base"] = BASE_UNKNOWN
        else:
            meta["origin"] = read.origin
        if read.base_at is not None:
            meta["base_at"] = str(read.base_at)
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

    def _stated(self, node: ast.expr) -> tuple[str, int] | None:
        """A string the source states for a URL or its base: a module constant, an
        environment default, or a constant bound to one; with its line."""
        if isinstance(node, ast.Name):
            return self._constants.get(node.id) or self._env_defaults.get(node.id)
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
        parts: list[list[str]] = []
        for value in rest:
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
        template = http.route_template(parts)
        if isinstance(template, str):
            return None
        expressions = iter(template.expressions)
        display = "/".join(
            segment if segment is not None else "{" + next(expressions, "")[:_MAX_EXPRESSION] + "}"
            for segment in template.segments
        )
        base = "{" + ast.unparse(head)[:_MAX_EXPRESSION] + "}"
        return _ClientUrl(None, template.segments, f"{prefix}{base}/{display}")

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
        expressions = iter(template.expressions)
        display = "/".join(
            segment if segment is not None else "{" + next(expressions, "")[:_MAX_EXPRESSION] + "}"
            for segment in template.segments
        )
        return _ClientUrl(origin.origin, template.segments, f"{origin.origin}/{display}", base_at)
