"""Ariadne in Python source: bindables, their resolvers, and schema construction.

Read from the module's ``ast``, as a pure function of one file; nothing is imported or run.

**Bindables** (``graphql-bindable``): a module-level name bound to ``QueryType()``,
``MutationType()``, ``SubscriptionType()`` or ``ObjectType("T")`` from ``ariadne``, with the
resolver each field is given -- ``x.set_field("f", resolver)`` at module level, or a
module-level function decorated ``@x.field("f")``. A resolver is recorded by what its name
refers to: a function of this module, or a name imported from another module.

**Schema construction** (``graphql-schema-construction``): ``make_executable_schema(type_defs,
*bindables)``, with what ``type_defs`` loads -- ``load_schema_from_path`` of a path anchored
to this file (``Path(__file__).parent / "..."``, ``os.path.join(os.path.dirname(__file__),
...)``) -- and which bindables it is given. A path relative to the working directory,
inline SDL and anything computed are recorded as unsupported, never guessed at.
"""

from __future__ import annotations

import ast
import json
import posixpath

from repoatlas.models.parsing import EdgeType, EntityType, ParsedReference
from repoatlas.parsers.http_facts import pending_reference
from repoatlas.parsers.ts_graphql import GRAPHQL_SCHEMA_CONSTRUCTION

GRAPHQL_BINDABLE = "graphql-bindable"

_ARIADNE = "ariadne"
_ROOTS = {"QueryType": "Query", "MutationType": "Mutation", "SubscriptionType": "Subscription"}
_OBJECT = "ObjectType"
_MAX_ENTRIES = 512


class AriadneFacts:
    """What one Python module states about Ariadne."""

    def __init__(self, tree: ast.Module) -> None:
        #: Local name -> (module, imported name, relative level).
        self._imports: dict[str, tuple[str, str, int]] = {}
        self._functions: set[str] = set()
        self._assigned: dict[str, ast.expr] = {}
        self._bound: dict[str, int] = {}
        self._bindables: dict[str, tuple[str, int]] = {}
        self._fields: dict[str, list[dict[str, object]]] = {}
        self._claims: list[ParsedReference] = []
        self._read(tree)

    def _read(self, tree: ast.Module) -> None:
        for statement in tree.body:
            if isinstance(statement, ast.ImportFrom) and statement.module is not None:
                for alias in statement.names:
                    self._bind(alias.asname or alias.name)
                    self._imports[alias.asname or alias.name] = (
                        statement.module,
                        alias.name,
                        statement.level,
                    )
            elif isinstance(statement, ast.Import):
                for alias in statement.names:
                    self._bind((alias.asname or alias.name).split(".")[0])
            elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._bind(statement.name)
                self._functions.add(statement.name)
            elif isinstance(statement, ast.Assign) and len(statement.targets) == 1:
                target = statement.targets[0]
                if isinstance(target, ast.Name):
                    self._bind(target.id)
                    self._assigned[target.id] = statement.value
        for name, value in self._assigned.items():
            kind = self._bindable(value)
            if kind is not None and self._bound.get(name) == 1:
                self._bindables[name] = (kind, value.lineno)
                self._fields[name] = []
        for statement in tree.body:
            if isinstance(statement, ast.Expr):
                self._set_field(statement.value)
            elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in statement.decorator_list:
                    self._decorated(decorator, statement)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and self._is(node.func, "make_executable_schema"):
                self._construction(node, tree)

    def _bind(self, name: str) -> None:
        self._bound[name] = self._bound.get(name, 0) + 1

    def _is(self, function: ast.expr, name: str) -> bool:
        """A call of ``name`` as ``ariadne`` exports it, imported by name or through the module."""
        if isinstance(function, ast.Name):
            found = self._imports.get(function.id)
            return found is not None and found[0] == _ARIADNE and found[1] == name
        return (
            isinstance(function, ast.Attribute)
            and function.attr == name
            and isinstance(function.value, ast.Name)
            and function.value.id == _ARIADNE
        )

    def _bindable(self, value: ast.expr) -> str | None:
        if not isinstance(value, ast.Call):
            return None
        for constructor, root in _ROOTS.items():
            if self._is(value.func, constructor):
                return root
        if self._is(value.func, _OBJECT) and value.args:
            first = value.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                return first.value
        return None

    def _set_field(self, call: ast.expr) -> None:
        """``x.set_field("f", resolver)`` on a bindable of this module."""
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "set_field"
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id in self._bindables
            and len(call.args) == 2
        ):
            return
        field, resolver = call.args
        if not (isinstance(field, ast.Constant) and isinstance(field.value, str)):
            return
        self._field(call.func.value.id, field.value, call.lineno, self._ref(resolver))

    def _decorated(
        self, decorator: ast.expr, function: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        """``@x.field("f")`` on a module-level function."""
        if not (
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "field"
            and isinstance(decorator.func.value, ast.Name)
            and decorator.func.value.id in self._bindables
            and len(decorator.args) == 1
            and isinstance(decorator.args[0], ast.Constant)
            and isinstance(decorator.args[0].value, str)
        ):
            return
        resolver: dict[str, object] = (
            {"local": function.name}
            if self._bound.get(function.name) == 1
            else {"unresolved": "rebound"}
        )
        self._field(decorator.func.value.id, decorator.args[0].value, decorator.lineno, resolver)

    def _field(self, bindable: str, field: str, line: int, resolver: dict[str, object]) -> None:
        fields = self._fields[bindable]
        if len(fields) < _MAX_ENTRIES:
            fields.append({"field": field, "line": line, "value": resolver})

    def _ref(self, node: ast.expr) -> dict[str, object]:
        if isinstance(node, ast.Lambda):
            return {"function": node.lineno}
        if not isinstance(node, ast.Name):
            return {"unresolved": "computed"}
        if self._bound.get(node.id) != 1:
            return {"unresolved": "rebound"}
        if node.id in self._functions:
            return {"local": node.id}
        found = self._imports.get(node.id)
        if found is None:
            return {"unresolved": "unbound"}
        module, imported, level = found
        return {"module": "." * level + module, "name": imported}

    # -- construction ----------------------------------------------------------------

    def _construction(self, call: ast.Call, tree: ast.Module) -> None:
        if not call.args:
            return
        type_defs = self._type_defs(call.args[0], 0)
        bindables: list[dict[str, object]] = []
        for argument in call.args[1:]:
            items = argument.elts if isinstance(argument, (ast.List, ast.Tuple)) else [argument]
            for item in items[:_MAX_ENTRIES]:
                if isinstance(item, ast.Name) and item.id in self._bindables:
                    bindables.append({"local": item.id})
                else:
                    bindables.append(self._ref(item))
        owner = _owner(tree, call)
        self._claims.append(
            pending_reference(
                EdgeType.REFERENCES,
                EntityType.CLASS,
                owner or "schema",
                call.lineno,
                getattr(call, "end_lineno", None) or call.lineno,
                role=GRAPHQL_SCHEMA_CONSTRUCTION,
                source_symbol=owner or None,
                library=_ARIADNE,
                owner=owner,
                type_defs=_json(type_defs),
                bindables=_json(bindables),
            )
        )

    def _type_defs(self, node: ast.expr, depth: int) -> list[dict[str, object]]:
        if depth > 4:
            return [{"unsupported": "computed"}]
        if isinstance(node, ast.Name):
            if node.id in self._assigned and self._bound.get(node.id) == 1:
                return self._type_defs(self._assigned[node.id], depth + 1)
            return [{"unsupported": "imported" if node.id in self._imports else "computed"}]
        if isinstance(node, (ast.List, ast.Tuple)):
            return [found for item in node.elts for found in self._type_defs(item, depth + 1)]
        if isinstance(node, ast.Call):
            if self._is(node.func, "load_schema_from_path") and node.args:
                path: dict[str, object] = dict(_path(node.args[0]))
                return [path if "unsupported" in path else {"tree": path["path"]}]
            if self._is(node.func, "gql"):
                return [{"unsupported": "inline-sdl"}]
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [{"unsupported": "inline-sdl"}]
        return [{"unsupported": "computed"}]

    # -- claims ----------------------------------------------------------------------

    def references(self) -> list[ParsedReference]:
        bindables = [
            pending_reference(
                EdgeType.REFERENCES,
                EntityType.CLASS,
                kind,
                line,
                line,
                role=GRAPHQL_BINDABLE,
                source_symbol=name,
                bindable=name,
                fields=_json(self._fields[name]),
            )
            for name, (kind, line) in sorted(self._bindables.items())
        ]
        return [*bindables, *self._claims]


def _path(node: ast.expr) -> dict[str, str]:
    """A path anchored to this module's directory, or why it is not one."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "str"
        and node.args
    ):
        return _path(node.args[0])
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {"unsupported": "cwd-relative"}
    parts = _anchored(node)
    if parts is None:
        return {"unsupported": "computed"}
    return {"path": posixpath.normpath(posixpath.join(*parts)) if parts else "."}


def _anchored(node: ast.expr) -> list[str] | None:
    """The literal parts after this module's directory, or ``None``."""
    if _is_directory(node):
        return []
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _anchored(node.left)
        right = node.right
        if left is not None and isinstance(right, ast.Constant) and isinstance(right.value, str):
            return [*left, right.value]
        return None
    if (
        isinstance(node, ast.Call)
        and _dotted(node.func) in {"os.path.join", "path.join", "join"}
        and node.args
    ):
        head = _anchored(node.args[0])
        rest = node.args[1:]
        if head is not None and all(
            isinstance(a, ast.Constant) and isinstance(a.value, str) for a in rest
        ):
            return [*head, *(str(a.value) for a in rest)]  # type: ignore[attr-defined]
    return None


def _is_directory(node: ast.expr) -> bool:
    """``Path(__file__).parent`` (resolved or not), or ``os.path.dirname(__file__)``."""
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        inner = node.value
        if (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr in {"resolve", "absolute"}
        ):
            inner = inner.func.value
        return (
            isinstance(inner, ast.Call)
            and _dotted(inner.func) in {"Path", "pathlib.Path"}
            and len(inner.args) == 1
            and _is_file(inner.args[0])
        )
    if (
        isinstance(node, ast.Call)
        and _dotted(node.func) in {"os.path.dirname", "path.dirname", "dirname"}
        and len(node.args) == 1
    ):
        argument = node.args[0]
        if (
            isinstance(argument, ast.Call)
            and _dotted(argument.func) in {"os.path.abspath", "os.path.realpath"}
            and argument.args
        ):
            argument = argument.args[0]
        return _is_file(argument)
    return False


def _is_file(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id == "__file__"


def _dotted(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return ""


def _owner(tree: ast.Module, call: ast.Call) -> str:
    """The module-level name or function the call is in, ``""`` for neither."""
    for statement in tree.body:
        if not (
            statement.lineno
            <= call.lineno
            <= (getattr(statement, "end_lineno", None) or statement.lineno)
        ):
            continue
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return statement.name
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
        ):
            return statement.targets[0].id
    return ""


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
