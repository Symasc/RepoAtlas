"""Declaration-level uses of module-scope names in Python (``USES``).

A change to a class, a function or anything a module binds reaches the declarations that
*use* it: annotate a parameter with it, subclass it, decorate with it, call it, pass it.
The import graph records only that a file imports a module, and the call graph only calls
it resolves by name. This module records the uses it can prove, and only those, from the
syntax tree alone -- the source is parsed, never run (V1_SPEC §17):

* the name resolves, by Python's own scoping, to a binding of the **module** -- one
  ``def`` or ``class`` of this file, one declaring statement, one import -- or to the one
  import of an enclosing function that binds it nothing else. The scopes are CPython's: a
  name bound anywhere in a function is local to all of it, class bodies are invisible to
  the functions inside them, comprehensions bind their own targets, and a generic
  ``def``, ``class`` or ``type`` statement (PEP 695) binds its type parameters in a scope
  of their own, which its annotations, bases, bounds, defaults and body see and its
  decorators and default values do not. A doubt costs a fact: every name a class body
  binds hides the module's from the whole body, whatever the order;
* the module binds the name **exactly once**. A second binding of any kind, a ``global``
  rebinding it from a function, or a ``del`` leaves no declaration to name -- whichever
  branch or order the bindings are in, since telling them apart is flow analysis. The one
  exception is an ``@overload`` group: ``def`` statements whose ``overload`` this module's
  own import of ``typing`` or ``typing_extensions`` binds, then one plain, unconditional
  ``def``, last -- the implementation, which is the group's one binding;
* no star import could have rebound it. ``from x import *`` rebinds whatever names ``x``
  has, which is not knowable from this file, so a binding a star import follows -- or a
  conditional one a star import precedes -- is refused. Every other binding stands; a
  star import never costs the file its other facts. A name no binding of the module
  supplies, read in a module with a star import, is only a **star claim**: it names the
  module's stars (line, specifier, whether conditional) and the line of its first read, and
  the global pass decides whether exactly one of them provides it, in time.

A **declaring statement** is an assignment whose every target is a name (``X = …``,
``A = B = …``), an annotated name (``X: T = …``, and ``X: T`` alone, which is how a
stub declares one) or a ``type`` statement (PEP 695). Bound once -- under a guard or not,
as a lone import is -- the name it declares is a declaration of this module: a *value*,
whatever it holds and however it is spelled. Nothing here says what it evaluates to, or
that it never changes. Unpacking, a loop, ``with`` or ``except`` target, a walrus and
``del`` declare nothing. A value is a **type alias** only where the syntax says so: a
``type`` statement, or ``X: TypeAlias = …`` whose ``TypeAlias`` this module's own import
of ``typing`` or ``typing_extensions`` binds -- ``UserId = int`` is a value like any.

``if TYPE_CHECKING:`` imports are ordinary module bindings: a type checker binds them, and
the use is marked ``type_only``. A string in an annotation is the annotation it spells
(PEP 484) and is read as one -- never inside ``Literal[...]`` or ``Annotated`` metadata,
where a string is a value.

A use records what kind it is -- ``type``, ``value`` or ``call`` -- the syntax it came
from (``form``), how the name is bound (``local``, ``import``, ``namespace``), the module
and name an import names, and the attribute chain it reaches through (``member``). A use
belongs to the innermost ``def`` or ``class`` it sits in, qualified as the parser
qualifies symbols; decorators, defaults, annotations and bases belong to the declaration
they decorate or annotate, and a module value's annotation and value to the module value
(``A = B = …`` declares two, and its uses stay the file's). A change to ``X`` so reaches
what ``ALIAS = X`` reaches. A call this binding model proves is recorded too, local or
imported: the call graph matches callees by bare name, and a method of the same name is
not the function an import binds.

The file's **export table** is one more fact: every name the module binds -- in Python,
every module-level binding is an attribute another module may import -- with what it is
bound to, the modules it star-imports, and whether it defines a module ``__getattr__``.
It also records what a star import of this module would take: the literal ``__all__``
(``all``: its sorted names) or, when ``__all__`` is anything but one unconditional list or
tuple of string literals nothing reads, ``"dynamic"``; and the names with a conditional
binding (``conditional``). Only :mod:`repoatlas.graph.py_uses` reads it.
"""

from __future__ import annotations

import ast
import json
import sys
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

from repoatlas.models.parsing import (
    USES_EXPORT_TABLE,
    EdgeType,
    EntityType,
    ParsedReference,
    ParsedSymbol,
)
from repoatlas.parsers.ts_uses import USES_EXPORTS, member_meta

_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
_TYPING = frozenset({"typing", "typing_extensions"})
#: ``try``, and from 3.11 ``try``/``except*``: one statement shape to this parser.
_TRY = (ast.Try, ast.TryStar) if sys.version_info >= (3, 11) else (ast.Try,)
#: Where a statement, an ``except`` handler or a ``case`` holds the statements inside it.
_STATEMENT_LISTS = ("body", "orelse", "finalbody", "handlers", "cases")
#: The longest forward reference read, and how deeply one may nest another.
_MAX_FORWARD = 1_000
_MAX_FORWARD_DEPTH = 4


@dataclass(frozen=True, slots=True)
class Binding:
    """What one name is bound to where a use finds it."""

    #: ``local`` (declared in this file), ``import``, ``namespace``, ``star`` (only a star
    #: import could bind it) or ``none``.
    kind: str
    #: ``local``: the first line of the declaration.
    line: int | None = None
    #: ``import``: the module named, dots kept; ``namespace``: the module bound.
    module: str | None = None
    #: ``import``: the name the module is asked for; ``star``: the name read.
    imported: str | None = None
    #: ``namespace``: the submodules this file imports beneath it (``import a.b.c``).
    loaded: tuple[str, ...] = ()
    type_only: bool = False
    #: The line of the import statement that binds it.
    bound_at: int | None = None
    #: ``none``: why no use of this name can be resolved.
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class UseSite:
    """One use of a name bound at module scope or by a function's own import."""

    name: str
    binding: Binding
    use: str
    form: str
    line: int
    #: The attribute chain the use reaches through the name (``svc.run`` -> ``run``).
    member: str | None = None

    @property
    def target_key(self) -> str:
        return f"{self.name}.{self.member}" if self.member else self.name


@dataclass(frozen=True, slots=True)
class FieldDeclaration:
    """A model's own field: an annotated name in a class body (DF3b, ``Model.field``)."""

    #: The class, qualified as the parser qualifies symbols.
    model: str
    name: str
    line: int
    end_line: int
    signature: str
    #: The initializer proven to bind it by its name as a keyword (:func:`_init_binding`).
    init: str | None = None


@dataclass(frozen=True, slots=True)
class FieldMember:
    """A model field a declaration reads through a typed parameter or sets by constructing it.

    It rides on the declaration's use of the model's name (``use``: ``type`` for the
    parameter's annotation, ``call`` for the construction): it resolves only through that
    use, to one of the resolved declaration's own fields (DF3B_PHASE_A.md §4).
    """

    owner: str | None
    name: str
    use: str
    field: str
    line: int
    #: ``parameter`` (``p.f``) or ``construction`` (``T(f=...)``).
    access: str


@dataclass(frozen=True, slots=True)
class Declaration:
    """A module value: a name one declaring statement binds, and nothing else does."""

    name: str
    #: The declaring statement's lines.
    line: int
    end_line: int
    #: ``variable`` or ``type-alias``; the signature is the statement's target and
    #: annotation (``type X[T]`` for an alias), never its value.
    kind: str
    signature: str
    #: A variable's explicit annotation, retained separately for typed receiver lookup.
    annotation: str | None = None


@dataclass(slots=True)
class _Site:
    """One place a module binds a name."""

    kind: str  # local, import, namespace, value, or other
    line: int
    conditional: bool
    type_only: bool = False
    module: str | None = None
    imported: str | None = None
    loaded: str | None = None
    #: ``value``: the declaring statement.
    statement: ast.stmt | None = None


@dataclass(frozen=True, slots=True)
class _Scope:
    kind: str  # module, function, class, comprehension, typeparams
    bound: frozenset[str] = frozenset()
    globals: frozenset[str] = frozenset()
    imports: dict[str, Binding] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _Ctx:
    """How an expression is read: as a type or a value, and in which syntax."""

    type: bool = False
    form: str = "read"
    depth: int = 0


_VALUE = _Ctx()


class _Names:
    """Every name one scope binds, gathered without entering the scopes inside it."""

    def __init__(self) -> None:
        self.bound: set[str] = set()
        self.globals: set[str] = set()
        self.nonlocals: set[str] = set()
        self.sites: Counter[str] = Counter()
        self.imports: dict[str, Binding] = {}
        self.stars: list[int] = []

    def bind(self, name: str) -> None:
        self.bound.add(name)
        self.sites[name] += 1

    def gather(self, node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            self._one(child)

    def _one(self, node: ast.AST) -> None:
        if isinstance(node, _FUNCTIONS):
            self.bind(node.name)
            for expr in (*node.decorator_list, *_defaults(node.args)):
                self._one(expr)
            return
        if isinstance(node, ast.ClassDef):
            self.bind(node.name)
            for expr in node.decorator_list:
                self._one(expr)
            if sys.version_info < (3, 12) or not node.type_params:
                for expr in (*node.bases, *(k.value for k in node.keywords)):
                    self._one(expr)
            return
        if isinstance(node, ast.Lambda):
            for expr in _defaults(node.args):
                self._one(expr)
            return
        if isinstance(node, _COMPREHENSIONS):
            # Only a walrus binds here; the loop targets belong to the comprehension.
            self._one(node.generators[0].iter)
            for inner in ast.walk(node):
                if isinstance(inner, ast.NamedExpr) and isinstance(inner.target, ast.Name):
                    self.bind(inner.target.id)
            return
        if sys.version_info >= (3, 12) and isinstance(node, ast.TypeAlias):
            if isinstance(node.name, ast.Name):
                self.bind(node.name.id)
            return
        if isinstance(node, ast.Import):
            for alias in node.names:
                local, binding = _import_binding(alias, node.lineno)
                self.bind(local)
                self.imports[local] = binding
            return
        if isinstance(node, ast.ImportFrom):
            if node.module == "__future__":
                return
            for alias in node.names:
                if alias.name == "*":
                    self.stars.append(node.lineno)
                    continue
                local = alias.asname or alias.name
                self.bind(local)
                self.imports[local] = _from_binding(node, alias.name, node.lineno)
            return
        if isinstance(node, ast.Global):
            self.globals.update(node.names)
            return
        if isinstance(node, ast.Nonlocal):
            self.nonlocals.update(node.names)
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
            self.bind(node.id)
        elif isinstance(node, ast.ExceptHandler | ast.MatchAs | ast.MatchStar) and node.name:
            self.bind(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            self.bind(node.rest)
        self.gather(node)

    def scope(self, kind: str, extra: Sequence[str] = ()) -> _Scope:
        """This scope, bound names first; an import is a binding only when it is the only one."""
        bound = (self.bound | set(extra)) - self.globals
        imports = {
            name: binding
            for name, binding in self.imports.items()
            if self.sites[name] == 1 and name not in extra and name not in self.globals
        }
        return _Scope(
            kind,
            frozenset(bound | self.nonlocals),
            frozenset(self.globals),
            imports,
        )


def _defaults(arguments: ast.arguments) -> list[ast.expr]:
    return [*arguments.defaults, *(d for d in arguments.kw_defaults if d is not None)]


def _parameters(arguments: ast.arguments) -> list[str]:
    names = [a.arg for a in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)]
    names.extend(a.arg for a in (arguments.vararg, arguments.kwarg) if a is not None)
    return names


def _annotations(arguments: ast.arguments) -> list[ast.expr]:
    every = (
        *arguments.posonlyargs,
        *arguments.args,
        *arguments.kwonlyargs,
        arguments.vararg,
        arguments.kwarg,
    )
    return [a.annotation for a in every if a is not None and a.annotation is not None]


def _import_binding(alias: ast.alias, line: int) -> tuple[str, Binding]:
    """``import a.b.c`` binds ``a`` and loads ``a.b.c``; ``import a.b as x`` binds ``a.b``."""
    if alias.asname:
        return alias.asname, Binding("namespace", module=alias.name, bound_at=line)
    head = alias.name.split(".", 1)[0]
    loaded = (alias.name,) if "." in alias.name else ()
    return head, Binding("namespace", module=head, loaded=loaded, bound_at=line)


def _from_binding(node: ast.ImportFrom, name: str, line: int, type_only: bool = False) -> Binding:
    module = "." * node.level + (node.module or "")
    return Binding("import", module=module, imported=name, type_only=type_only, bound_at=line)


if sys.version_info >= (3, 12):  # type parameters are 3.12 syntax; nothing older calls these

    def _type_params(params: Sequence[ast.type_param]) -> list[str]:
        return [
            p.name for p in params if isinstance(p, ast.TypeVar | ast.ParamSpec | ast.TypeVarTuple)
        ]

    def _type_param_exprs(params: Sequence[ast.type_param]) -> list[ast.expr]:
        found: list[ast.expr] = []
        for param in params:
            bound = getattr(param, "bound", None)
            if bound is not None:
                found.append(bound)
            default = getattr(param, "default_value", None)
            if default is not None:
                found.append(default)
        return found


def _chain(node: ast.Attribute) -> tuple[ast.Name, list[str]] | None:
    """``a.b.c`` -> (``a``, [``b``, ``c``]) when the chain is names all the way down."""
    members: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        members.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    return current, members[::-1]


def _last_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


class PythonUseAnalysis:
    """The module bindings, export table and use sites of one parsed Python module."""

    def __init__(self, tree: ast.Module, *, record: bool = False) -> None:
        self.bindings: dict[str, Binding] = {}
        self.stars: list[tuple[int, str]] = []
        #: (line, specifier, conditional) of each star import, in source order: what a star
        #: claim names.
        self.star_sites: list[tuple[int, str, bool]] = []
        self.getattr = False
        #: (owner, use): the declaration each use belongs to, ``None`` for the file.
        self.uses: list[tuple[str | None, UseSite]] = []
        #: Uses refused, by reason; measurement only.
        self.rejected: Counter[str] = Counter()
        #: (line, column, name, decision) for every name read, when asked: what a test
        #: holds against CPython's own symbol table.
        self.decisions: list[tuple[int, int, str, str]] | None = [] if record else None
        #: The module values this file declares, in line order: the parser's symbols.
        self.declarations: list[Declaration] = []
        #: Each module-level statement that owns the uses in it, with the one value it declares
        #: (:meth:`_declares`): what else in the statement belongs to that value (DF3a).
        self.owned: list[tuple[str, ast.stmt]] = []
        #: The models' own fields, in source order, and the members read or set through a
        #: typed parameter or a construction (DF3b).
        self.fields: list[FieldDeclaration] = []
        self.members: list[FieldMember] = []
        #: Every place the module binds each name, once its bindings are gathered.
        self._by_name: dict[str, list[_Site]] = {}
        self._guards, rebinds, global_all = _survey(tree)
        self._module_bindings(tree, rebinds=rebinds)
        self.exports = self._export_table(tree, global_all=global_all)
        self._body(tree.body, [_Scope("module")], None)

    # -- module scope ----------------------------------------------------------

    def _module_bindings(self, tree: ast.Module, *, rebinds: bool) -> None:
        sites = self._by_name
        declared: list[tuple[str, ast.stmt]] = []
        self._sites(tree.body, sites, conditional=False, type_only=False)
        for line, name in _global_rebinds(tree) if rebinds else ():
            sites.setdefault(name, []).append(_Site("other", line, conditional=True))
        stars = [line for line, _ in self.stars]
        for name, found in sites.items():
            binding = _binding_of(name, found)
            if binding.kind != "none" and stars and _star_affected(found, stars):
                binding = Binding("none", reason="star-import")
            self.bindings[name] = binding
            [site, *_] = found
            if binding.kind == "local" and site.kind == "value" and site.statement is not None:
                declared.append((name, site.statement))
        # Whether a decorator is typing's `overload` is read once every binding is known.
        for name, found in sites.items():
            if self.bindings[name].reason != "duplicate":
                continue
            implementation = self._implementation(found)
            if implementation is not None:
                binding = Binding("local", line=implementation)
                if stars and _star_affected(found, stars):
                    binding = Binding("none", reason="star-import")
                self.bindings[name] = binding
        # What a value is spelled with is read once every binding is known.
        self.declarations = sorted(
            (self._declaration(name, statement) for name, statement in declared),
            key=lambda d: (d.line, d.name),
        )

    def _declaration(self, name: str, statement: ast.stmt) -> Declaration:
        """The declaration ``statement`` makes of ``name``: its lines, kind and signature."""
        kind, signature = "variable", name
        annotation = None
        if sys.version_info >= (3, 12) and isinstance(statement, ast.TypeAlias):
            kind, signature = "type-alias", f"type {name}{_type_params_text(statement)}"
        elif isinstance(statement, ast.AnnAssign):
            if statement.value is not None and self._typing(statement.annotation, "TypeAlias"):
                kind, signature = "type-alias", f"type {name}"
            else:
                signature = f"{name}: {ast.unparse(statement.annotation)}"
                annotation = ast.unparse(statement.annotation)
        return Declaration(
            name=name,
            line=statement.lineno,
            end_line=statement.end_lineno or statement.lineno,
            kind=kind,
            signature=signature,
            annotation=annotation if kind == "variable" else None,
        )

    def _implementation(self, sites: list[_Site]) -> int | None:
        """The line of an ``@overload`` group's implementation, which is its one binding.

        Every site is a ``def``; all but one carry a decorator proven to be ``typing``'s or
        ``typing_extensions``' ``overload``; and that one, the implementation, is the last
        binding and unconditional -- so it is what the module binds when its body is done.
        """
        stubs: list[_Site] = []
        rest: list[_Site] = []
        for site in sites:
            statement = site.statement
            if site.kind != "local" or not isinstance(statement, _FUNCTIONS):
                return None
            proven = any(self._typing(d, "overload") for d in statement.decorator_list)
            (stubs if proven else rest).append(site)
        if not stubs or len(rest) != 1:
            return None
        [implementation] = rest
        if implementation.conditional or implementation.line != max(s.line for s in sites):
            return None
        return implementation.line

    def _typing(self, node: ast.expr, name: str) -> bool:
        """Whether ``node`` is ``typing``'s or ``typing_extensions``' own ``name``.

        Proven by this module's binding of what spells it: ``from typing import name`` (under
        any alias), or ``import typing`` (under any alias) and the attribute. A name bound
        twice, by a star import's reach or by anything else, proves nothing.
        """
        if isinstance(node, ast.Name):
            found = self.bindings.get(node.id)
            return (
                found is not None
                and found.kind == "import"
                and found.module in _TYPING
                and found.imported == name
            )
        if (
            isinstance(node, ast.Attribute)
            and node.attr == name
            and isinstance(node.value, ast.Name)
        ):
            found = self.bindings.get(node.value.id)
            return found is not None and found.kind == "namespace" and found.module in _TYPING
        return False

    def _sites(
        self,
        statements: Sequence[ast.stmt],
        sites: dict[str, list[_Site]],
        *,
        conditional: bool,
        type_only: bool,
    ) -> None:
        def other(name: str, line: int) -> None:
            sites.setdefault(name, []).append(_Site("other", line, conditional))

        for statement in statements:
            if isinstance(statement, (*_FUNCTIONS, ast.ClassDef)):
                sites.setdefault(statement.name, []).append(
                    _Site("local", statement.lineno, conditional, statement=statement)
                )
                if isinstance(statement, _FUNCTIONS) and statement.name == "__getattr__":
                    self.getattr = True
                continue
            if isinstance(statement, ast.Import):
                for alias in statement.names:
                    local, binding = _import_binding(alias, statement.lineno)
                    sites.setdefault(local, []).append(
                        _Site(
                            "namespace",
                            statement.lineno,
                            conditional,
                            type_only,
                            module=binding.module,
                            imported=alias.asname,
                            loaded=binding.loaded[0] if binding.loaded else None,
                        )
                    )
                continue
            if isinstance(statement, ast.ImportFrom):
                if statement.module == "__future__":
                    continue
                spec = "." * statement.level + (statement.module or "")
                for alias in statement.names:
                    if alias.name == "*":
                        self.stars.append((statement.lineno, spec))
                        self.star_sites.append((statement.lineno, spec, conditional))
                        continue
                    sites.setdefault(alias.asname or alias.name, []).append(
                        _Site(
                            "import",
                            statement.lineno,
                            conditional,
                            type_only,
                            module=spec,
                            imported=alias.name,
                        )
                    )
                continue
            if isinstance(statement, ast.If):
                for name, line in _stored(statement.test):
                    other(name, line)
                guarded = self._is_guard(statement.test)
                self._sites(statement.body, sites, conditional=True, type_only=guarded)
                self._sites(statement.orelse, sites, conditional=True, type_only=False)
                continue
            if isinstance(statement, _TRY):
                self._sites(statement.body, sites, conditional=True, type_only=type_only)
                for handler in statement.handlers:
                    if handler.name:
                        other(handler.name, handler.lineno)
                    self._sites(handler.body, sites, conditional=True, type_only=type_only)
                self._sites(statement.orelse, sites, conditional=True, type_only=type_only)
                self._sites(statement.finalbody, sites, conditional=True, type_only=type_only)
                continue
            if isinstance(statement, ast.For | ast.AsyncFor | ast.While):
                header = (
                    [statement.target, statement.iter]
                    if isinstance(statement, ast.For | ast.AsyncFor)
                    else [statement.test]
                )
                for part in header:
                    for name, line in _stored(part):
                        other(name, line)
                self._sites(statement.body, sites, conditional=True, type_only=type_only)
                self._sites(statement.orelse, sites, conditional=True, type_only=type_only)
                continue
            if isinstance(statement, ast.With | ast.AsyncWith):
                for item in statement.items:
                    for name, line in _stored(item):
                        other(name, line)
                self._sites(statement.body, sites, conditional=True, type_only=type_only)
                continue
            if isinstance(statement, ast.Match):
                for name, line in _stored(statement.subject):
                    other(name, line)
                for case in statement.cases:
                    for name, line in _stored(case.pattern):
                        other(name, line)
                    if case.guard is not None:
                        for name, line in _stored(case.guard):
                            other(name, line)
                    self._sites(case.body, sites, conditional=True, type_only=type_only)
                continue
            declared = _declared(statement)
            if declared:
                for name in declared:
                    sites.setdefault(name, []).append(
                        _Site("value", statement.lineno, conditional, statement=statement)
                    )
                # a walrus in the value binds at module scope too
                for part in _declaring_parts(statement):
                    for name, line in _stored(part):
                        other(name, line)
                continue
            for name, line in _stored(statement):
                other(name, line)

    def _is_guard(self, test: ast.expr) -> bool:
        """``TYPE_CHECKING`` or ``typing.TYPE_CHECKING``, bound as such by this file."""
        names, modules = self._guards
        if isinstance(test, ast.Name):
            return test.id in names
        return (
            isinstance(test, ast.Attribute)
            and test.attr == "TYPE_CHECKING"
            and isinstance(test.value, ast.Name)
            and test.value.id in modules
        )

    def _export_table(self, tree: ast.Module, *, global_all: bool) -> dict[str, object]:
        names: dict[str, list[str]] = {}
        for name, binding in sorted(self.bindings.items()):
            if binding.kind == "local":
                names[name] = ["local", name, str(binding.line)]
            elif binding.kind == "import":
                names[name] = ["import", binding.module or "", binding.imported or ""]
            elif binding.kind == "namespace":
                names[name] = ["namespace", binding.module or ""]
            else:
                names[name] = ["none", binding.reason or ""]
        table: dict[str, object] = {
            "names": names,
            "star": sorted({spec for _, spec in self.stars}),
        }
        if self.getattr:
            table["getattr"] = True
        # Only a module that says `__all__` pays for the walk that proves nothing else reads it.
        listed = self._dunder_all(tree, global_all)
        if listed is not None:
            table["all"] = listed
        conditional = sorted(
            name for name, found in self._by_name.items() if any(s.conditional for s in found)
        )
        if conditional:
            table["conditional"] = conditional
        return table

    def _dunder_all(self, tree: ast.Module, global_all: bool) -> list[str] | str | None:
        """The module's ``__all__`` as a sorted list of names, ``"dynamic"`` or ``None``.

        A list only when ``__all__`` is bound exactly once, unconditionally, to a list or
        tuple of string literals that are identifiers, and nothing reads it (``__all__ +=``,
        ``.append`` and ``print(__all__)`` all do) or rebinds it from a function. Any other
        way of saying ``__all__`` is ``"dynamic"``; not saying it at all is ``None``.
        """
        found = self._by_name.get("__all__", [])
        if not found and not global_all:
            return None
        listed = _all_literal(found)
        if listed is None or global_all or _reads_all(tree):
            return "dynamic"
        return sorted(set(listed))

    # -- scopes and uses -------------------------------------------------------

    def _body(self, statements: Sequence[ast.stmt], stack: list[_Scope], owner: str | None) -> None:
        for statement in statements:
            self._statement(statement, stack, owner)

    def _statement(self, node: ast.stmt, stack: list[_Scope], owner: str | None) -> None:
        if owner is None and len(stack) == 1:
            owner = self._declares(node)
            if owner is not None:
                self.owned.append((owner, node))
        if isinstance(node, _FUNCTIONS):
            self._function(node, stack, owner)
        elif isinstance(node, ast.ClassDef):
            self._class(node, stack, owner)
        elif sys.version_info >= (3, 12) and isinstance(node, ast.TypeAlias):
            inner = stack
            if node.type_params:
                inner = [*stack, _Scope("typeparams", frozenset(_type_params(node.type_params)))]
                for expr in _type_param_exprs(node.type_params):
                    self._expr(expr, inner, owner, _Ctx(type=True, form="annotation"))
            self._expr(node.value, inner, owner, _Ctx(type=True, form="type-alias"))
        elif isinstance(node, ast.AnnAssign):
            self._expr(node.annotation, stack, owner, _Ctx(type=True, form="annotation"))
            if node.value is not None:
                alias = _last_name(node.annotation) == "TypeAlias"
                ctx = _Ctx(type=True, form="type-alias") if alias else _VALUE
                self._expr(node.value, stack, owner, ctx)
            if not isinstance(node.target, ast.Name):
                self._expr(node.target, stack, owner, _VALUE)
        elif isinstance(node, ast.Import | ast.ImportFrom | ast.Global | ast.Nonlocal):
            return
        elif isinstance(node, ast.Match):
            self._expr(node.subject, stack, owner, _VALUE)
            for case in node.cases:
                self._pattern(case.pattern, stack, owner)
                if case.guard is not None:
                    self._expr(case.guard, stack, owner, _VALUE)
                self._body(case.body, stack, owner)
        else:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.stmt):
                    self._statement(child, stack, owner)
                elif isinstance(child, ast.expr):
                    self._expr(child, stack, owner, _VALUE)
                elif isinstance(child, ast.ExceptHandler):
                    if child.type is not None:
                        self._expr(child.type, stack, owner, _VALUE)
                    self._body(child.body, stack, owner)
                elif isinstance(child, ast.withitem):
                    self._expr(child.context_expr, stack, owner, _VALUE)
                    if child.optional_vars is not None:
                        self._expr(child.optional_vars, stack, owner, _VALUE)

    def _declares(self, node: ast.stmt) -> str | None:
        """The one module value a module-level statement declares, if it declares one."""
        declared = _declared(node)
        if len(declared) != 1:
            return None
        binding = self.bindings.get(declared[0])
        if binding is None or binding.kind != "local" or binding.line != node.lineno:
            return None
        return declared[0]

    def _function(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, stack: list[_Scope], owner: str | None
    ) -> None:
        qualified = f"{owner}.{node.name}" if owner else node.name
        for decorator in node.decorator_list:
            self._expr(decorator, stack, qualified, _Ctx(form="decorator"))
        for default in _defaults(node.args):
            self._expr(default, stack, qualified, _Ctx(form="default"))
        inner = stack
        if sys.version_info >= (3, 12) and node.type_params:
            inner = [*stack, _Scope("typeparams", frozenset(_type_params(node.type_params)))]
            for expr in _type_param_exprs(node.type_params):
                self._expr(expr, inner, qualified, _Ctx(type=True, form="annotation"))
        for annotation in _annotations(node.args):
            self._expr(annotation, inner, qualified, _Ctx(type=True, form="annotation"))
        if node.returns is not None:
            self._expr(node.returns, inner, qualified, _Ctx(type=True, form="annotation"))
        names = _Names()
        for statement in node.body:
            names._one(statement)
        scope = names.scope("function", _parameters(node.args))
        self.members.extend(_field_members(node, qualified))
        self._body(node.body, [*inner, scope], qualified)

    def _class(self, node: ast.ClassDef, stack: list[_Scope], owner: str | None) -> None:
        qualified = f"{owner}.{node.name}" if owner else node.name
        for decorator in node.decorator_list:
            self._expr(decorator, stack, qualified, _Ctx(form="decorator"))
        inner = stack
        if sys.version_info >= (3, 12) and node.type_params:
            inner = [*stack, _Scope("typeparams", frozenset(_type_params(node.type_params)))]
            for expr in _type_param_exprs(node.type_params):
                self._expr(expr, inner, qualified, _Ctx(type=True, form="annotation"))
        for base in node.bases:
            self._expr(base, inner, qualified, _Ctx(form="base"))
        for keyword in node.keywords:
            self._expr(keyword.value, inner, qualified, _Ctx(form="keyword"))
        self.fields.extend(_fields(node, qualified, self._init_binding(node, inner)))
        names = _Names()
        for statement in node.body:
            names._one(statement)
        self._body(node.body, [*inner, _Scope("class", frozenset(names.bound))], qualified)

    def _init_binding(self, node: ast.ClassDef, stack: list[_Scope]) -> str | None:
        """How the class's own initializer binds its fields by keyword, when that is proven.

        ``pydantic``: its one base is pydantic's ``BaseModel`` and it has no decorator;
        ``dataclass``: its one decorator is the ``dataclasses`` (or ``pydantic.dataclasses``)
        ``dataclass``, not ``init=False``. Either is proven by what the name is bound to where
        the class is written, as any use's binding is (an import of the module or of the name,
        bound once, out of a star import's reach). Refused, for the whole class, when it
        defines ``__init__`` or ``__new__``, has a ``model_validator`` or ``root_validator``
        method, or mentions an ``alias_generator``: each can make a keyword mean another field
        (DF3B_PHASE_A.md §13.1). Anything else -- attrs, an ORM model, a ``NamedTuple``, a
        subclass of a model -- binds nothing proven.
        """
        pydantic = (
            len(node.bases) == 1
            and not node.decorator_list
            and self._names(node.bases[0], stack, _PYDANTIC, "BaseModel")
        )
        dataclass = False
        if len(node.decorator_list) == 1:
            decorator = node.decorator_list[0]
            called = decorator.func if isinstance(decorator, ast.Call) else decorator
            dataclass = self._names(called, stack, _DATACLASSES, "dataclass") and not (
                isinstance(decorator, ast.Call)
                and any(
                    k.arg == "init"
                    and not (isinstance(k.value, ast.Constant) and k.value.value is True)
                    for k in decorator.keywords
                )
            )
        if pydantic == dataclass:
            return None
        for statement in node.body:
            if not isinstance(statement, _FUNCTIONS):
                continue
            if statement.name in {"__init__", "__new__"}:
                return None
            for decorator in statement.decorator_list:
                called = decorator.func if isinstance(decorator, ast.Call) else decorator
                if _last_name(called) in _VALIDATORS:
                    return None
        if _mentions_alias_generator(node):
            return None
        return "pydantic" if pydantic else "dataclass"

    def _names(
        self, node: ast.expr, stack: list[_Scope], modules: frozenset[str], name: str
    ) -> bool:
        """Whether ``node`` is ``name`` of one of ``modules``: ``N`` bound by ``from M import
        name``, or ``M.name`` with ``M`` bound by ``import M`` -- resolved where it is written."""
        if isinstance(node, ast.Name):
            found = self._resolve(node.id, stack)
            return (
                isinstance(found, Binding)
                and found.kind == "import"
                and found.module in modules
                and found.imported == name
            )
        if (
            isinstance(node, ast.Attribute)
            and node.attr == name
            and isinstance(node.value, ast.Name)
        ):
            found = self._resolve(node.value.id, stack)
            return (
                isinstance(found, Binding) and found.kind == "namespace" and found.module in modules
            )
        return False

    def _pattern(self, pattern: ast.pattern, stack: list[_Scope], owner: str | None) -> None:
        """A class pattern's class and a value pattern's value are reads; captures bind."""
        for node in ast.walk(pattern):
            if isinstance(node, ast.MatchClass):
                self._expr(node.cls, stack, owner, _Ctx(form="pattern"))
            elif isinstance(node, ast.MatchValue):
                self._expr(node.value, stack, owner, _Ctx(form="pattern"))

    def _expr(self, node: ast.expr, stack: list[_Scope], owner: str | None, ctx: _Ctx) -> None:
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load):
                self._use(node.id, node, stack, owner, ctx, None, called=False)
            return
        if isinstance(node, ast.Attribute):
            found = _chain(node) if isinstance(node.ctx, ast.Load) else None
            if found is not None:
                root, members = found
                self._use(root.id, root, stack, owner, ctx, ".".join(members), called=False)
            else:
                self._expr(node.value, stack, owner, ctx)
            return
        if isinstance(node, ast.Call):
            self._call(node, stack, owner, ctx)
            return
        if isinstance(node, ast.Constant):
            if ctx.type and isinstance(node.value, str):
                self._forward(node, stack, owner, ctx)
            return
        if ctx.type and isinstance(node, ast.Subscript):
            self._subscript(node, stack, owner, ctx)
            return
        if isinstance(node, ast.Lambda):
            for default in _defaults(node.args):
                self._expr(default, stack, owner, _VALUE)
            names = _Names()
            names._one(node.body)
            scope = names.scope("function", _parameters(node.args))
            self._expr(node.body, [*stack, scope], owner, _VALUE)
            return
        if isinstance(node, _COMPREHENSIONS):
            self._comprehension(node, stack, owner)
            return
        if isinstance(node, ast.NamedExpr):
            self._expr(node.value, stack, owner, ctx)
            return
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.expr):
                self._expr(child, stack, owner, ctx)
            elif isinstance(child, ast.keyword):
                self._expr(child.value, stack, owner, ctx)

    def _call(self, node: ast.Call, stack: list[_Scope], owner: str | None, ctx: _Ctx) -> None:
        callee = node.func
        form = "decorator" if ctx.form == "decorator" else "call"
        if isinstance(callee, ast.Name):
            self._use(callee.id, callee, stack, owner, _Ctx(form=form), None, called=True)
        elif isinstance(callee, ast.Attribute) and (found := _chain(callee)) is not None:
            root, members = found
            self._use(root.id, root, stack, owner, _Ctx(form=form), ".".join(members), called=True)
        else:
            self._expr(callee, stack, owner, _VALUE)
        for argument in node.args:
            self._expr(argument, stack, owner, _Ctx(form="argument"))
        for keyword in node.keywords:
            self._expr(keyword.value, stack, owner, _Ctx(form="argument"))

    def _subscript(
        self, node: ast.Subscript, stack: list[_Scope], owner: str | None, ctx: _Ctx
    ) -> None:
        """``list[User]`` is read as a type throughout, but a ``Literal`` holds values."""
        self._expr(node.value, stack, owner, ctx)
        kind = _last_name(node.value)
        if kind == "Literal":
            return
        if kind == "Annotated" and isinstance(node.slice, ast.Tuple) and node.slice.elts:
            first, *metadata = node.slice.elts
            self._expr(first, stack, owner, ctx)
            for item in metadata:
                if not isinstance(item, ast.Constant):
                    self._expr(item, stack, owner, _VALUE)
            return
        self._expr(node.slice, stack, owner, ctx)

    def _forward(
        self, node: ast.Constant, stack: list[_Scope], owner: str | None, ctx: _Ctx
    ) -> None:
        """A string annotation, read as the annotation it spells, at the string's line."""
        text = str(node.value)
        if ctx.depth >= _MAX_FORWARD_DEPTH or len(text) > _MAX_FORWARD:
            return
        try:
            parsed = ast.parse(text.strip(), mode="eval")
        except (SyntaxError, ValueError, RecursionError):
            return
        ast.increment_lineno(parsed, node.lineno - 1)
        self._expr(
            parsed.body,
            stack,
            owner,
            _Ctx(type=True, form="string-annotation", depth=ctx.depth + 1),
        )

    def _comprehension(self, node: ast.expr, stack: list[_Scope], owner: str | None) -> None:
        assert isinstance(node, _COMPREHENSIONS)
        generators = node.generators
        self._expr(generators[0].iter, stack, owner, _VALUE)
        bound: set[str] = set()
        for generator in generators:
            bound.update(name for name, _ in _stored(generator.target))
        inner = [*stack, _Scope("comprehension", frozenset(bound))]
        for index, generator in enumerate(generators):
            if index:
                self._expr(generator.iter, inner, owner, _VALUE)
            for condition in generator.ifs:
                self._expr(condition, inner, owner, _VALUE)
        if isinstance(node, ast.DictComp):
            self._expr(node.key, inner, owner, _VALUE)
            self._expr(node.value, inner, owner, _VALUE)
        else:
            self._expr(node.elt, inner, owner, _VALUE)

    def _resolve(self, name: str, stack: list[_Scope]) -> Binding | str | None:
        """The binding a name read here finds; why it is refused; ``None`` for a builtin."""
        crossed = False
        for scope in reversed(stack):
            if scope.kind == "module":
                return self.bindings.get(name)
            if scope.kind == "class":
                if not crossed and name in scope.bound:
                    return "class-scope"
                continue
            if scope.kind == "typeparams":
                if name in scope.bound:
                    return "type-param"
                continue
            if scope.kind == "comprehension":
                if name in scope.bound:
                    return "shadowed"
                crossed = True
                continue
            if name in scope.globals:
                return self.bindings.get(name)
            if name in scope.imports:
                return scope.imports[name]
            if name in scope.bound:
                return "shadowed"
            crossed = True
        return None

    def _use(
        self,
        name: str,
        node: ast.expr,
        stack: list[_Scope],
        owner: str | None,
        ctx: _Ctx,
        member: str | None,
        *,
        called: bool,
    ) -> None:
        found = self._resolve(name, stack)
        if self.decisions is not None:
            decision = (
                "builtin"
                if found is None
                else found
                if isinstance(found, str)
                else "local-import"
                if found is not self.bindings.get(name)
                else "module"
            )
            self.decisions.append((node.lineno, node.col_offset, name, decision))
        if found is None:
            if self.stars:
                self.rejected["star-import"] += 1
                # Nothing in this module binds it, so only a star could: a claim, not a fact.
                use, form = ("type", ctx.form) if ctx.type else ("value", ctx.form)
                if called and not ctx.type:
                    use = "call"
                elif member and not ctx.type and form == "read":
                    form = "member"
                star = Binding("star", imported=name)
                site = UseSite(name, star, use, form, node.lineno, member or None)
                self.uses.append((owner, site))
            return
        if isinstance(found, str):
            self.rejected[found] += 1
            return
        if found.kind == "none":
            self.rejected[found.reason or "none"] += 1
            return
        use, form = ("type", ctx.form) if ctx.type else ("value", ctx.form)
        if found.kind == "local" and member:
            # A member of this file's own declaration is not followed: the use is of the
            # declaration, as the receiver of whatever the member is.
            member = None
            if not ctx.type:
                form = "receiver"
        elif called and not ctx.type:
            use = "call"
        elif member and not ctx.type and form == "read":
            form = "member"
        self.uses.append((owner, UseSite(name, found, use, form, node.lineno, member or None)))


#: The modules whose ``BaseModel`` and ``dataclass`` bind a model's fields by keyword.
_PYDANTIC = frozenset({"pydantic", "pydantic.v1"})
_DATACLASSES = frozenset({"dataclasses", "pydantic.dataclasses"})
#: A field's keyword is another name when its value or annotation calls with one of these.
_ALIASES = frozenset({"alias", "validation_alias"})
_VALIDATORS = frozenset({"model_validator", "root_validator"})


def _class_var(annotation: ast.expr) -> bool:
    head = annotation.value if isinstance(annotation, ast.Subscript) else annotation
    return _last_name(head) == "ClassVar"


def _fields(node: ast.ClassDef, model: str, init: str | None) -> list[FieldDeclaration]:
    """A class body's own annotated names, each annotated once in it and not ``ClassVar``."""
    annotated = [
        s for s in node.body if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)
    ]
    counts = Counter(s.target.id for s in annotated if isinstance(s.target, ast.Name))
    found: list[FieldDeclaration] = []
    for statement in annotated:
        assert isinstance(statement.target, ast.Name)
        name = statement.target.id
        if counts[name] != 1 or _class_var(statement.annotation):
            continue
        found.append(
            FieldDeclaration(
                model=model,
                name=name,
                line=statement.lineno,
                end_line=statement.end_lineno or statement.lineno,
                signature=f"{name}: {ast.unparse(statement.annotation)}",
                init=init if _keyword_bound(init, statement) else None,
            )
        )
    return found


def _keyword_bound(init: str | None, statement: ast.AnnAssign) -> bool:
    """Whether the initializer ``init`` binds this field by its own name as a keyword."""
    if init is None or not isinstance(statement.target, ast.Name):
        return False
    if init == "pydantic" and statement.target.id.startswith("_"):
        return False  # a private attribute, never an initializer argument
    if _calls_with(statement.value, _ALIASES) or _calls_with(statement.annotation, _ALIASES):
        return False
    value = statement.value
    return not (
        isinstance(value, ast.Call)
        and _last_name(value.func) == "field"
        and any(
            k.arg == "init" and isinstance(k.value, ast.Constant) and k.value.value is False
            for k in value.keywords
        )
    )


def _calls_with(node: ast.AST | None, keywords: frozenset[str]) -> bool:
    return node is not None and any(
        isinstance(n, ast.Call) and any(k.arg in keywords for k in n.keywords)
        for n in ast.walk(node)
    )


def _mentions_alias_generator(node: ast.ClassDef) -> bool:
    """An ``alias_generator`` keyword, assigned name or key in the class keywords or body,
    outside its methods."""
    pending: list[ast.AST] = [
        *node.keywords,
        *(s for s in node.body if not isinstance(s, _FUNCTIONS)),
    ]
    while pending:
        current = pending.pop()
        if isinstance(current, ast.keyword) and current.arg == "alias_generator":
            return True
        if isinstance(current, ast.Name) and current.id == "alias_generator":
            return True
        if isinstance(current, ast.Constant) and current.value == "alias_generator":
            return True
        if isinstance(current, _FUNCTIONS):
            continue
        pending.extend(ast.iter_child_nodes(current))
    return False


def _field_type(annotation: ast.expr | None) -> str | None:
    """The one named type a parameter is annotated with: ``T``, ``"T"``, ``Optional[T]``,
    ``T | None`` or ``Annotated[T, ...]``; anything else names none."""
    if isinstance(annotation, ast.Name):
        return annotation.id
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        return annotation.value if annotation.value.isidentifier() else None
    if isinstance(annotation, ast.Subscript):
        head = _last_name(annotation.value)
        if head == "Optional":
            return _field_type(annotation.slice)
        if (
            head == "Annotated"
            and isinstance(annotation.slice, ast.Tuple)
            and annotation.slice.elts
        ):
            return _field_type(annotation.slice.elts[0])
        return None
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        sides = [annotation.left, annotation.right]
        named = [s for s in sides if not (isinstance(s, ast.Constant) and s.value is None)]
        return _field_type(named[0]) if len(named) == 1 else None
    return None


#: What a function's own walk stops at: a nested declaration's body is its own.
_DECLARATIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_MATCH_NAMED = (ast.MatchAs, ast.MatchStar)


def _field_members(
    function: ast.FunctionDef | ast.AsyncFunctionDef, owner: str
) -> list[FieldMember]:
    """What ``function`` reads through a typed parameter (``p.f``) and sets by constructing a
    class by its bare name (``T(f=...)``), in its own body.

    A typed parameter is annotated with one named type (:func:`_field_type`) and nothing in
    the function binds its name again -- an assignment, a loop or ``with`` target, a lambda
    parameter, ``except ... as``, a pattern capture, ``global``/``nonlocal``, an import or a
    nested declaration. Only the first attribute is read: ``p.a.b`` reads ``a``. Whether the
    name resolves, and to a model that owns the field, is the use's to decide. One walk of the
    function's own nodes, without the bodies of the functions and classes nested in it.
    """
    args = function.args
    arguments = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    every = {a.arg for a in arguments} | {a.arg for a in (args.vararg, args.kwarg) if a is not None}
    typed = {a.arg: t for a in arguments if (t := _field_type(a.annotation))}
    rebound: set[str] = set()
    reads: list[tuple[str, str, int]] = []
    calls: list[ast.Call] = []
    pending = list(ast.iter_child_nodes(function))
    while pending:
        node = pending.pop()
        if isinstance(node, ast.Name):
            if not isinstance(node.ctx, ast.Load):
                rebound.add(node.id)
            continue
        if isinstance(node, ast.Attribute):
            value = node.value
            if isinstance(value, ast.Name) and value.id in typed:
                reads.append((value.id, node.attr, node.lineno))
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and node.keywords and func.id not in every:
                calls.append(node)
        elif isinstance(node, _DECLARATIONS):
            rebound.add(node.name)
            continue
        elif isinstance(node, ast.Lambda):
            rebound |= {a.arg for a in ast.walk(node.args) if isinstance(a, ast.arg)}
        elif isinstance(node, ast.ExceptHandler):
            if node.name:
                rebound.add(node.name)
        elif isinstance(node, ast.alias):
            rebound.add((node.asname or node.name).split(".")[0])
        elif isinstance(node, ast.Global | ast.Nonlocal):
            rebound |= set(node.names)
        elif isinstance(node, _MATCH_NAMED):
            if node.name:
                rebound.add(node.name)
        elif isinstance(node, ast.MatchMapping):
            if node.rest:
                rebound.add(node.rest)
        pending.extend(ast.iter_child_nodes(node))
    found = [
        FieldMember(owner, typed[name], "type", attr, line, "parameter")
        for name, attr, line in reads
        if name not in rebound
    ]
    for call in calls:
        assert isinstance(call.func, ast.Name)
        name = call.func.id
        if name in rebound:
            continue
        for keyword in call.keywords:
            if keyword.arg is not None:
                line = getattr(keyword, "lineno", call.lineno)
                found.append(FieldMember(owner, name, "call", keyword.arg, line, "construction"))
    return found


def field_lists(
    members: Sequence[FieldMember], ambiguous: set[str]
) -> dict[tuple[str | None, str, str], list[list[object]]]:
    """Each use's members, ``[field, line, access]`` once per field at its first line, in line
    order (whole: :func:`~repoatlas.parsers.ts_uses.member_meta` caps what a claim records).

    Keyed as uses are merged -- (owner, name, use), a declaration whose qualified name several
    definitions carry being the file -- so each list rides on the one reference it belongs to.
    """
    first: dict[tuple[str | None, str, str], dict[str, tuple[int, str]]] = {}
    for member in members:
        owner = None if member.owner in ambiguous else member.owner
        seen = first.setdefault((owner, member.name, member.use), {})
        here = (member.line, member.access)
        if member.field not in seen or here < seen[member.field]:
            seen[member.field] = here
    return {
        key: [
            [name, line, access]
            for name, (line, access) in sorted(found.items(), key=lambda kv: (kv[1], kv[0]))
        ]
        for key, found in first.items()
    }


def _binding_of(name: str, sites: list[_Site]) -> Binding:
    """The one binding ``sites`` make of ``name``, or why they make none."""
    if all(s.kind == "namespace" and s.module == name and s.imported is None for s in sites):
        # `import a`, `import a.b`, `import a.c`: one module, however many submodules load.
        loaded = tuple(sorted({s.loaded for s in sites if s.loaded}))
        return Binding(
            "namespace",
            module=name,
            loaded=loaded,
            type_only=all(s.type_only for s in sites),
            bound_at=min(s.line for s in sites),
        )
    if len(sites) > 1:
        return Binding("none", reason="duplicate")
    site = sites[0]
    if site.kind in {"local", "value"}:
        return Binding("local", line=site.line)
    if site.kind == "import":
        return Binding(
            "import",
            module=site.module,
            imported=site.imported,
            type_only=site.type_only,
            bound_at=site.line,
        )
    if site.kind == "namespace":
        return Binding(
            "namespace",
            module=site.module,
            loaded=(site.loaded,) if site.loaded else (),
            type_only=site.type_only,
            bound_at=site.line,
        )
    return Binding("none", reason="not-a-declaration")


def _declared(statement: ast.stmt) -> list[str]:
    """The names a declaring statement declares (see the module docstring), else none."""
    if isinstance(statement, ast.Assign):
        if all(isinstance(target, ast.Name) for target in statement.targets):
            return [target.id for target in statement.targets if isinstance(target, ast.Name)]
        return []
    if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
        return [statement.target.id]
    if (
        sys.version_info >= (3, 12)
        and isinstance(statement, ast.TypeAlias)
        and isinstance(statement.name, ast.Name)
    ):
        return [statement.name.id]
    return []


if sys.version_info >= (3, 12):  # the ``type`` statement is 3.12 syntax

    def _type_params_text(statement: ast.TypeAlias) -> str:
        """``[T, U: int]`` as a ``type`` statement spells its parameters, else nothing."""
        if not statement.type_params:
            return ""
        return "[" + ", ".join(ast.unparse(param) for param in statement.type_params) + "]"


def _declaring_parts(statement: ast.stmt) -> list[ast.expr]:
    """What a declaring statement holds besides its targets: its value and annotation."""
    if isinstance(statement, ast.Assign):
        return [statement.value]
    if isinstance(statement, ast.AnnAssign):
        return [statement.annotation, *([statement.value] if statement.value else [])]
    return []


def _star_affected(sites: list[_Site], stars: list[int]) -> bool:
    """Whether a star import could have rebound this name (see the module docstring)."""
    return any(
        star > site.line or (star < site.line and site.conditional)
        for site in sites
        for star in stars
    )


def _stored(node: ast.AST) -> Iterator[tuple[str, int]]:
    """Every name ``node`` binds in the scope holding it, without entering inner scopes."""
    pending: list[ast.AST] = [node]
    while pending:
        current = pending.pop()
        if isinstance(current, ast.Name):
            if isinstance(current.ctx, ast.Store | ast.Del):
                yield current.id, current.lineno
            continue
        if isinstance(current, (*_FUNCTIONS, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(current, _COMPREHENSIONS):
            for inner in ast.walk(current):
                if isinstance(inner, ast.NamedExpr) and isinstance(inner.target, ast.Name):
                    yield inner.target.id, inner.lineno
            continue
        if isinstance(current, ast.MatchAs | ast.MatchStar) and current.name:
            yield current.name, current.lineno
        elif isinstance(current, ast.MatchMapping) and current.rest:
            yield current.rest, current.lineno
        pending.extend(ast.iter_child_nodes(current))


def _global_rebinds(tree: ast.Module) -> Iterator[tuple[int, str]]:
    """(line, name) for each name a function declares ``global`` and binds."""
    for node in ast.walk(tree):
        if not isinstance(node, _FUNCTIONS):
            continue
        names = _Names()
        for statement in node.body:
            names._one(statement)
        for name in sorted(names.globals):
            if names.sites[name]:
                yield node.lineno, name


def _all_literal(sites: list[_Site]) -> list[str] | None:
    """The names ``__all__`` lists when its one site is an unconditional assignment of a list
    or tuple of string literals that are identifiers; ``None`` for anything else."""
    if len(sites) != 1:
        return None
    [site] = sites
    statement = site.statement
    if site.kind != "value" or site.conditional:
        return None
    if isinstance(statement, ast.Assign):
        [target, *others] = statement.targets
        if others or not isinstance(target, ast.Name) or target.id != "__all__":
            return None
        value: ast.expr | None = statement.value
    elif isinstance(statement, ast.AnnAssign):
        if not isinstance(statement.target, ast.Name) or statement.target.id != "__all__":
            return None
        value = statement.value
    else:
        return None
    if not isinstance(value, ast.List | ast.Tuple):
        return None
    listed: list[str] = []
    for element in value.elts:
        if not (
            isinstance(element, ast.Constant)
            and isinstance(element.value, str)
            and element.value.isidentifier()
        ):
            return None
        listed.append(element.value)
    return listed


def _reads_all(tree: ast.Module) -> bool:
    """Whether any name ``__all__`` is read anywhere in the module, in any scope."""
    return any(
        isinstance(node, ast.Name) and node.id == "__all__" and isinstance(node.ctx, ast.Load)
        for node in ast.walk(tree)
    )


def _survey(tree: ast.Module) -> tuple[tuple[frozenset[str], frozenset[str]], bool, bool]:
    """One walk of the file's statements: its ``TYPE_CHECKING`` guards, whether it says
    ``global``, and whether it says ``global __all__``.

    The guards are the names it binds to ``TYPE_CHECKING`` and to the ``typing`` module
    itself. Without a ``global`` statement no function rebinds a module name, and the walk
    over every function body that would find one is skipped. Imports and ``global`` are
    statements, and statements are only ever held in these lists, so no expression is
    visited.
    """
    names: set[str] = set()
    modules: set[str] = set()
    rebinds = False
    global_all = False
    pending: list[ast.AST] = list(tree.body)
    while pending:
        node = pending.pop()
        for held in _STATEMENT_LISTS:
            inner = getattr(node, held, None)
            if isinstance(inner, list):
                pending.extend(inner)
        if isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module in _TYPING:
                names.update(a.asname or a.name for a in node.names if a.name == "TYPE_CHECKING")
        elif isinstance(node, ast.Import):
            modules.update(a.asname or a.name for a in node.names if a.name in _TYPING)
        elif isinstance(node, ast.Global):
            rebinds = True
            global_all = global_all or "__all__" in node.names
    return (frozenset(names), frozenset(modules)), rebinds, global_all


def python_use_references(
    analysis: PythonUseAnalysis, symbols: Sequence[ParsedSymbol], *, package: bool = False
) -> list[ParsedReference]:
    """The file's ``USES`` references: attributed uses, merged, plus its export table.

    A declaration whose qualified name more than one definition carries cannot be told
    apart from the others, so its uses are the file's. A use of a name by the declaration
    that is that name, or by one of its own members, is containment and is dropped.
    Repeats of one (declaration, target, use) are one reference at the first occurrence,
    with the count kept. A star claim also keeps the earliest line it is read on, since the
    star must precede that read, and names every star import of the module.
    """
    counted = Counter(s.qualified_name for s in symbols if s.kind is not EntityType.MODULE)
    ambiguous = {name for name, count in counted.items() if count > 1}
    merged: dict[tuple[str | None, str, str], tuple[UseSite, int]] = {}
    first_read: dict[tuple[str | None, str, str], int] = {}
    for source, site in analysis.uses:
        owner = None if source in ambiguous else source
        if (
            site.binding.kind == "local"
            and owner is not None
            and (owner == site.name or owner.startswith(site.name + "."))
        ):
            analysis.rejected["self"] += 1
            continue
        key = (owner, site.target_key, site.use)
        first, count = merged.get(key, (site, 0))
        merged[key] = (first, count + 1)
        if site.binding.kind == "star":
            first_read[key] = min(site.line, first_read.get(key, site.line))

    stars = json.dumps(
        [[line, spec, int(conditional)] for line, spec, conditional in analysis.star_sites],
        separators=(",", ":"),
    )
    fields = field_lists(analysis.members, ambiguous)
    references: list[ParsedReference] = []
    for (owner, target, use), (site, count) in merged.items():
        binding = site.binding
        meta = {
            "use": site.use,
            "form": site.form,
            "binding": binding.kind,
            "local": site.name,
            "occurrences": str(count),
        }
        members = fields.get((owner, target, use)) if site.member is None else None
        if members:
            # The model's fields this declaration reads or sets through this use (DF3b):
            # resolved with the use, never copied into its own edge's record.
            meta.update(member_meta(members))
        if binding.kind != "local":
            meta["module"] = binding.module or ""
            if binding.imported:
                meta["imported"] = binding.imported
            if site.member:
                meta["member"] = site.member
            if binding.loaded:
                meta["loaded"] = ",".join(binding.loaded)
            if binding.type_only:
                meta["type_only"] = "true"
            if binding.bound_at is not None:
                meta["bound_at"] = str(binding.bound_at)
        if binding.kind == "star":
            meta["stars"] = stars
            meta["first_line"] = str(first_read[(owner, target, use)])
        references.append(
            ParsedReference(
                edge_type=EdgeType.USES,
                target_kind=EntityType.SYMBOL,
                target_key=target,
                line_start=site.line,
                line_end=site.line,
                source_symbol=owner,
                meta=meta,
            )
        )
    # A package's table is recorded even when empty: ``from pkg import sub`` falls back
    # to the submodule only when the package is known to bind nothing, and a package
    # with no table at all is one that failed to parse.
    exports = analysis.exports
    if not (
        package
        or exports.get("names")
        or exports.get("star")
        or exports.get("getattr")
        or exports.get("all") is not None
    ):
        return references
    references.append(
        ParsedReference(
            edge_type=EdgeType.USES,
            target_kind=EntityType.SYMBOL,
            target_key=USES_EXPORT_TABLE,
            line_start=1,
            line_end=1,
            meta={USES_EXPORTS: json.dumps(exports, sort_keys=True, separators=(",", ":"))},
        )
    )
    return references
