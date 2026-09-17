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
is recorded only for mounts this file proves — the router constructed and
included into an application here, with literal prefixes — and is composed from
more than one line, so it is recorded as composed. A router included from
another file, or nested in another router, yields nothing: its served path is
decided somewhere this parser cannot see. An application passed to
``.mount()`` here is itself a sub-application and yields nothing either.
Application-level routes are taken as served at the root, which is what the file
states; an application mounted under a prefix from *another* file is a known
limitation.

Parsed, never imported: this reads the syntax tree only (V1_SPEC §17).
"""

from __future__ import annotations

import ast
from collections import Counter
from dataclasses import dataclass, field

from repoatlas.models import http
from repoatlas.models.parsing import EdgeType, EntityType, ParsedReference
from repoatlas.parsers.http_facts import http_reference

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

_LIBRARIES = frozenset({"fastapi", "flask", "requests", "httpx"})
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

#: Deep enough for any literal someone writes by hand; shallow enough that an
#: adversarial chain of ``+`` cannot recurse the parser into the ground.
_MAX_STRING_DEPTH = 32


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


def _binding_counts(tree: ast.Module) -> Counter[str] | None:
    """How many times each name is bound anywhere in the module.

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


@dataclass(slots=True)
class _Receiver:
    """A name proven to hold a framework application, router or blueprint."""

    kind: str
    #: The router's or blueprint's own prefix. ``None`` for a blueprint that
    #: declares none; always ``""`` or a prefix for a FastAPI router.
    prefix: str | None = None
    #: Prefixes this file mounts it under (a ``None`` entry: the blueprint's own).
    mounts: list[str | None] = field(default_factory=list)
    #: Cleared when something makes its served paths unknowable from here.
    usable: bool = True


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


def _flask_join(prefix: str | None, rule: str) -> str | None:
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


class PythonHttp:
    """What one module proves about HTTP, and the facts that follow from it."""

    def __init__(self, tree: ast.Module) -> None:
        #: Local name → module, for ``import requests [as r]``.
        self._modules: dict[str, str] = {}
        #: Local name → ``module.attr``, for ``from fastapi import FastAPI``.
        self._names: dict[str, str] = {}
        self._receivers: dict[str, _Receiver] = {}
        counts = _binding_counts(tree)
        if counts is None:
            return
        self._imports(tree, counts)
        self._constructors(tree, counts)
        self._mounts(tree)

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

    def _mounts(self, tree: ast.Module) -> None:
        for statement in tree.body:
            if not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)):
                continue
            call = statement.value
            func = call.func
            if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
                continue
            owner = self._receivers.get(func.value.id)
            if owner is None:
                continue
            if func.attr == "include_router" and owner.kind in {_FASTAPI_APP, _FASTAPI_ROUTER}:
                self._mount(owner, call, "router", _FASTAPI_ROUTER, "prefix")
            elif func.attr == "register_blueprint" and owner.kind in {
                _FLASK_APP,
                _FLASK_BLUEPRINT,
            }:
                self._mount(owner, call, "blueprint", _FLASK_BLUEPRINT, "url_prefix")
            elif func.attr == "mount" and owner.kind == _FASTAPI_APP:
                for argument in (*call.args, *(keyword.value for keyword in call.keywords)):
                    if isinstance(argument, ast.Name) and argument.id in self._receivers:
                        self._receivers[argument.id].usable = False

    def _mount(
        self, owner: _Receiver, call: ast.Call, parameter: str, child_kind: str, prefix_name: str
    ) -> None:
        argument = _argument(call, 0, parameter)
        child = self._receivers.get(argument.id) if isinstance(argument, ast.Name) else None
        if child is None or child.kind != child_kind:
            return
        if owner.kind == child_kind or _dynamic(call):
            # Nested in another router, or mounted with arguments this cannot
            # read: where it is served is decided somewhere else.
            child.usable = False
            return
        keywords = _keywords(call)
        if prefix_name not in keywords:
            child.mounts.append("" if child_kind == _FASTAPI_ROUTER else None)
            return
        prefix = static_string(keywords[prefix_name])
        if prefix is None or (child_kind == _FASTAPI_ROUTER and not _fastapi_prefix(prefix)):
            child.usable = False
            return
        child.mounts.append(prefix)

    # -- routes ---------------------------------------------------------------------

    def route_references(
        self, function: ast.FunctionDef | ast.AsyncFunctionDef, qualified: str
    ) -> list[ParsedReference]:
        """Routes a module-level function's decorators prove it serves."""
        found: list[ParsedReference] = []
        for decorator in function.decorator_list:
            if not isinstance(decorator, ast.Call) or _dynamic(decorator):
                continue
            func = decorator.func
            if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)):
                continue
            receiver = self._receivers.get(func.value.id)
            if receiver is None or not receiver.usable:
                continue
            declared = self._declared(receiver, func.attr, decorator)
            if declared is None:
                continue
            methods, path = declared
            source = "fastapi" if receiver.kind in {_FASTAPI_APP, _FASTAPI_ROUTER} else "flask"
            start, end = decorator.lineno, decorator.end_lineno or decorator.lineno
            for served, composed in self._served(receiver, path):
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
        return found

    @staticmethod
    def _declared(
        receiver: _Receiver, attribute: str, decorator: ast.Call
    ) -> tuple[tuple[str, ...], str] | None:
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
        path = static_string(path_node)
        if methods is None or path is None:
            return None
        return methods, path

    @staticmethod
    def _served(receiver: _Receiver, path: str) -> list[tuple[str, bool]]:
        """Every path this route is served at, and whether it was composed."""
        if receiver.kind in {_FASTAPI_APP, _FLASK_APP}:
            served = http.route_path(path)
            return [(served, False)] if served is not None else []
        found: list[tuple[str, bool]] = []
        for mount in dict.fromkeys(receiver.mounts):
            if receiver.kind == _FASTAPI_ROUTER:
                if path and not path.startswith("/"):
                    continue
                full: str | None = f"{mount or ''}{receiver.prefix or ''}{path}"
            else:
                full = _flask_join(mount if mount is not None else receiver.prefix, path)
            served = http.route_path(full) if full else None
            if served is not None and (served, True) not in found:
                found.append((served, True))
        return found

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
        url = static_string(url_node)
        target = http.client_target(url) if url is not None else None
        # Neither library resolves a relative URL: it names no request at all.
        if method is None or target is None or target.url is None or url_node is None:
            return []
        end = max(
            node.end_lineno or node.lineno for node in (url_node, method_node) if node is not None
        )
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
        ]
