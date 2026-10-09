"""Persistence in Python source: database clients, queries, typed mappings and comparisons.

Read from the module's ``ast``, as a pure function of one file: nothing is imported,
evaluated or connected to. What one file cannot settle is a claim the global pass decides
(:mod:`repoatlas.graph.persistence`, :mod:`repoatlas.graph.value_mappings`).

**Clients** (``persistence-client``): a module-level name bound to ``psycopg.connect`` /
``psycopg2.connect`` or SQLAlchemy ``create_engine``, with the one environment variable its
arguments read (``os.environ["X"]``, ``os.environ.get("X")``, ``os.getenv("X")``).

**Queries** (``persistence-query``), each in the function that makes it: SQL passed to
``execute`` on a connection this function opens (``with psycopg.connect(...) as conn``,
or ``conn = psycopg.connect(...)``) or on a module-level client, and SQL passed to
SQLAlchemy ``text``. A literal is parsed here (:func:`repoatlas.parsers.sql.sql_predicates`);
an f-string with substitutions, ``%`` / ``+`` / ``.format`` built text is ``dynamic``.

**Typed value mappings** (``value-mapping``): a module-level ``m: dict[A, B] = {...}`` (also
``Dict`` and ``Mapping``), with each entry's key (``A.X``, or a literal) and value.

**Typed comparisons** (``typed-comparison``): ``v.f == "lit"`` (also ``!=``, ``in``,
``not in`` with literals) where ``v`` is a parameter annotated ``T``, or the target of a
``for`` loop or comprehension over a parameter annotated ``list[T]``, ``Sequence[T]``,
``Iterable[T]``, ``set[T]``, ``frozenset[T]`` or ``tuple[T, ...]``. The claim names ``T``,
``f`` and the literals; which type ``T`` is, and what its field ``f`` is declared as, only
the global pass can say.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterator

from repoatlas.models.parsing import EdgeType, EntityType, ParsedReference
from repoatlas.parsers.http_facts import pending_reference
from repoatlas.parsers.sql import sql_predicates
from repoatlas.parsers.ts_persistence import PERSISTENCE_CLIENT, PERSISTENCE_QUERY, VALUE_MAPPING

TYPED_COMPARISON = "typed-comparison"

_CONNECT = {("psycopg", "connect"), ("psycopg2", "connect")}
_ENGINE = {("sqlalchemy", "create_engine")}
_TEXT = {("sqlalchemy", "text"), ("sqlalchemy.sql", "text")}
_MAPPINGS = frozenset({"dict", "Dict", "Mapping", "MutableMapping"})
_COLLECTIONS = frozenset(
    {
        "list",
        "List",
        "Sequence",
        "Iterable",
        "set",
        "Set",
        "frozenset",
        "FrozenSet",
        "tuple",
        "Tuple",
    }
)
_MAX_ENTRIES = 512


class PyPersistence:
    """What one Python module states about databases, mappings and typed comparisons."""

    def __init__(self, tree: ast.Module) -> None:
        #: Local name -> (module, imported name, or ``""`` for the module itself).
        self._imports: dict[str, tuple[str, str]] = {}
        self._clients: set[str] = set()
        self._claims: list[ParsedReference] = []
        for statement in tree.body:
            if isinstance(statement, ast.Import):
                for alias in statement.names:
                    self._imports[alias.asname or alias.name.split(".")[0]] = (
                        alias.name if alias.asname else alias.name.split(".")[0],
                        "",
                    )
            elif isinstance(statement, ast.ImportFrom) and statement.module and not statement.level:
                for alias in statement.names:
                    self._imports[alias.asname or alias.name] = (statement.module, alias.name)
        for statement in tree.body:
            self._module_statement(statement)
        for owner, function in _functions(tree.body, ""):
            self._function(owner, function)

    def references(self) -> list[ParsedReference]:
        return list(self._claims)

    # -- names -------------------------------------------------------------------------

    def _callee(self, call: ast.expr) -> tuple[str, str] | None:
        """``(module, name)`` of what a call calls, through this module's imports."""
        if not isinstance(call, ast.Call):
            return None
        func = call.func
        if isinstance(func, ast.Name) and func.id in self._imports:
            module, imported = self._imports[func.id]
            return (module, imported) if imported else None
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            bound = self._imports.get(func.value.id)
            if bound is not None and not bound[1]:
                return bound[0], func.attr
        return None

    def _env(self, node: ast.AST) -> str | None:
        found: set[str] = set()
        for inner in ast.walk(node):
            key: ast.expr | None = None
            if isinstance(inner, ast.Subscript) and _dotted(inner.value) == "os.environ":
                key = inner.slice
            elif isinstance(inner, ast.Call) and _dotted(inner.func) in {
                "os.environ.get",
                "os.getenv",
            }:
                key = inner.args[0] if inner.args else None
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                found.add(key.value)
        return next(iter(found)) if len(found) == 1 else None

    # -- the module --------------------------------------------------------------------

    def _module_statement(self, statement: ast.stmt) -> None:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target, value = statement.targets[0], statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            target, value = statement.target, statement.value
            if isinstance(target, ast.Name):
                self._mapping(target.id, statement.annotation, value, statement)
        else:
            return
        if not isinstance(target, ast.Name):
            return
        callee = self._callee(value)
        library = "psycopg" if callee in _CONNECT else "sqlalchemy" if callee in _ENGINE else None
        if library is None:
            return
        self._clients.add(target.id)
        meta = {"library": library}
        env = self._env(value)
        if env:
            meta["env"] = env
        self._claims.append(
            pending_reference(
                EdgeType.REFERENCES,
                EntityType.SYMBOL,
                target.id,
                statement.lineno,
                statement.end_lineno or statement.lineno,
                role=PERSISTENCE_CLIENT,
                source_symbol=target.id,
                **meta,
            )
        )

    def _mapping(self, name: str, annotation: ast.expr, value: ast.expr, at: ast.stmt) -> None:
        if not (
            isinstance(annotation, ast.Subscript)
            and _last(annotation.value) in _MAPPINGS
            and isinstance(annotation.slice, ast.Tuple)
            and len(annotation.slice.elts) == 2
            and isinstance(value, ast.Dict)
        ):
            return
        key_type, value_type = (_last(e) for e in annotation.slice.elts)
        if not key_type or not value_type:
            return
        entries: list[list[object]] = []
        for key, item in zip(value.keys, value.values, strict=False):
            if key is None or len(entries) >= _MAX_ENTRIES:
                continue
            side = _side(key)
            if side is None:
                continue
            entries.append([*side, *(_side(item) or ("other", "")), key.lineno])
        self._claims.append(
            pending_reference(
                EdgeType.MAPS_TO,
                EntityType.SYMBOL,
                name,
                at.lineno,
                at.end_lineno or at.lineno,
                role=VALUE_MAPPING,
                source_symbol=name,
                key_type=key_type,
                value_type=value_type,
                entries=json.dumps(entries, separators=(",", ":")),
            )
        )

    # -- functions ---------------------------------------------------------------------

    def _function(self, owner: str, function: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        connections: dict[str, dict[str, str]] = {}
        typed: dict[str, str] = {}
        collections: dict[str, str] = {}
        arguments = [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]
        for argument in arguments:
            annotation = argument.annotation
            if isinstance(annotation, ast.Name):
                typed[argument.arg] = annotation.id
            element = _element(annotation)
            if element:
                collections[argument.arg] = element
        for node in _own_nodes(function):
            if isinstance(node, ast.With | ast.AsyncWith):
                for item in node.items:
                    if (
                        isinstance(item.optional_vars, ast.Name)
                        and self._callee(item.context_expr) in _CONNECT
                    ):
                        connections[item.optional_vars.id] = self._connection(item.context_expr)
            elif (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and self._callee(node.value) in _CONNECT
            ):
                connections[node.targets[0].id] = self._connection(node.value)
            elif (
                isinstance(node, ast.For | ast.comprehension)
                and isinstance(node.target, ast.Name)
                and isinstance(node.iter, ast.Name)
                and node.iter.id in collections
            ):
                typed[node.target.id] = collections[node.iter.id]
        for node in _own_nodes(function):
            if isinstance(node, ast.Call):
                self._query(owner, node, connections)
            elif isinstance(node, ast.Compare):
                self._comparison(owner, node, typed)

    def _connection(self, call: ast.expr) -> dict[str, str]:
        env = self._env(call)
        return {"library": "psycopg", **({"env": env} if env else {})}

    def _query(self, owner: str, call: ast.Call, connections: dict[str, dict[str, str]]) -> None:
        callee = self._callee(call)
        meta: dict[str, str]
        if callee in _TEXT:
            meta = {"library": "sqlalchemy", "binding": "unbound", "receiver": "text"}
        elif (
            isinstance(call.func, ast.Attribute)
            and call.func.attr == "execute"
            and isinstance(call.func.value, ast.Name)
        ):
            receiver = call.func.value.id
            if receiver in connections:
                meta = {"binding": "inline", "receiver": receiver, **connections[receiver]}
            elif receiver in self._clients:
                meta = {"binding": "module", "receiver": receiver}
            else:
                return
        else:
            return
        if not call.args:
            return
        meta.update(kind="sql", owner=owner)
        text, dynamic = _sql_text(call.args[0])
        if dynamic:
            meta["dynamic"] = "true"
        elif text is not None:
            predicates = sql_predicates(text)
            if predicates is None:
                meta["unsupported"] = "true"
            else:
                meta["predicates"] = json.dumps(
                    [[p.column, p.op, list(p.values), p.table] for p in predicates],
                    separators=(",", ":"),
                )
        else:
            return
        self._claims.append(
            pending_reference(
                EdgeType.REFERENCES,
                EntityType.SYMBOL,
                meta["receiver"],
                call.lineno,
                call.end_lineno or call.lineno,
                role=PERSISTENCE_QUERY,
                source_symbol=owner,
                **meta,
            )
        )

    def _comparison(self, owner: str, node: ast.Compare, typed: dict[str, str]) -> None:
        if len(node.ops) != 1 or not isinstance(
            node.ops[0], ast.Eq | ast.NotEq | ast.In | ast.NotIn
        ):
            return
        left, right = node.left, node.comparators[0]
        if not _field_of(left, typed) and _field_of(right, typed):
            left, right = right, left
        field_access = _field_of(left, typed)
        values = _literals(right)
        if field_access is None or not values:
            return
        variable, field_name = field_access
        self._claims.append(
            pending_reference(
                EdgeType.REFERENCES,
                EntityType.SYMBOL,
                typed[variable],
                node.lineno,
                node.end_lineno or node.lineno,
                role=TYPED_COMPARISON,
                source_symbol=owner,
                type=typed[variable],
                field=field_name,
                variable=variable,
                values=json.dumps(values),
            )
        )


def _functions(
    body: list[ast.stmt], prefix: str
) -> Iterator[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]]:
    """Every function and method, under the qualified name the parser gives it."""
    for statement in body:
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            qualified = f"{prefix}{statement.name}"
            yield qualified, statement
            yield from _functions(statement.body, f"{qualified}.")
        elif isinstance(statement, ast.ClassDef):
            yield from _functions(statement.body, f"{prefix}{statement.name}.")


def _own_nodes(function: ast.AST) -> Iterator[ast.AST]:
    """The nodes of a function's body, not those of functions or classes nested in it."""
    stack = list(ast.iter_child_nodes(function))
    while stack:
        node = stack.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _dotted(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = _dotted(node.value)
        return f"{inner}.{node.attr}" if inner else ""
    return ""


def _last(node: ast.expr) -> str:
    return _dotted(node).rsplit(".", 1)[-1]


def _element(annotation: ast.expr | None) -> str | None:
    """``T`` of ``list[T]`` and the other collections a loop draws one ``T`` from."""
    if not isinstance(annotation, ast.Subscript) or _last(annotation.value) not in _COLLECTIONS:
        return None
    inner = annotation.slice
    if isinstance(inner, ast.Tuple):
        if (
            len(inner.elts) == 2
            and isinstance(inner.elts[1], ast.Constant)
            and (inner.elts[1].value is Ellipsis)
        ):
            inner = inner.elts[0]
        else:
            return None
    if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
        return inner.value if inner.value.isidentifier() else None
    return inner.id if isinstance(inner, ast.Name) else None


def _side(node: ast.expr) -> tuple[str, str] | None:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return "member", f"{node.value.id}.{node.attr}"
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str | int)
        and not isinstance(node.value, bool)
    ):
        return "literal", str(node.value)
    return None


def _field_of(node: ast.expr, typed: dict[str, str]) -> tuple[str, str] | None:
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in typed
    ):
        return node.value.id, node.attr
    return None


def _literals(node: ast.expr) -> list[str]:
    items = node.elts if isinstance(node, ast.Tuple | ast.List | ast.Set) else [node]
    found = [i.value for i in items if isinstance(i, ast.Constant) and isinstance(i.value, str)]
    return found if len(found) == len(items) else []


def _sql_text(node: ast.expr) -> tuple[str | None, bool]:
    """A query's text, or whether only run time decides it."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, False
    if isinstance(node, ast.JoinedStr):
        if any(isinstance(v, ast.FormattedValue) for v in node.values):
            return None, True
        return "".join(str(v.value) for v in node.values if isinstance(v, ast.Constant)), False
    if isinstance(node, ast.BinOp) or (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
    ):
        return None, True
    return None, False
