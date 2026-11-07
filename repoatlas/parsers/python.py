"""Python parser using the stdlib ``ast`` module (V1_SPEC §7.1).

Repository code is **parsed, never imported** (V1_SPEC §17). ``ast.parse`` compiles
to a syntax tree without executing module-level code, which is what makes static
analysis of untrusted repositories safe.

``ast`` is chosen over tree-sitter for Python because it is exact, needs no
dependency, and reports precise line ranges including decorators.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Sequence
from pathlib import Path

from repoatlas.models.parsing import (
    ENUM_MEMBER,
    FIELD,
    PARSE_FAILED,
    SYNTAX_ERROR,
    EdgeType,
    EntityType,
    ParsedFile,
    ParseDiagnostic,
    ParsedReference,
    ParsedSymbol,
    Severity,
)
from repoatlas.parsers.http_python import PythonHttp
from repoatlas.parsers.interpreter import interpreter_tag
from repoatlas.parsers.marks import whole_file_span
from repoatlas.parsers.py_graphql import AriadneFacts, schema_download_references
from repoatlas.parsers.py_persistence import PyPersistence
from repoatlas.parsers.py_uses import PythonUseAnalysis, python_use_references
from repoatlas.parsers.setuptools_project import setup_py_facts, setuptools_reference
from repoatlas.parsers.ts_uses import FIELDS_OMITTED, FIELDS_TRUNCATED

_SUFFIXES = frozenset({".py", ".pyi"})

#: Callables whose first string argument names an environment variable.
_ENV_READ_CALLS = frozenset({"getenv", "environ.get"})


def _decorator_name(node: ast.expr) -> str | None:
    """Dotted name of a decorator expression, when it is a plain reference."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _decorator_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    return None


def _call_name(node: ast.expr) -> str | None:
    """Dotted callee name, e.g. ``os.getenv`` or ``load_config``."""
    return _decorator_name(node)


def _string_value(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


#: Base classes whose class-level string assignments are members.
_ENUM_BASES = frozenset({"Enum", "StrEnum", "IntEnum"})


class _Visitor(ast.NodeVisitor):
    """Single walk collecting symbols and unresolved references."""

    def __init__(self, module_name: str, http: PythonHttp | None = None) -> None:
        self.module_name = module_name
        self._http = http
        self.symbols: list[ParsedSymbol] = []
        self.references: list[ParsedReference] = []
        #: Each environment read outside every declaration, with the node read: the module
        #: value whose statement holds it owns it, once the module's values are known.
        self.module_reads: list[tuple[int, ast.expr]] = []
        self._scope: list[str] = []

    # -- scope helpers -------------------------------------------------------

    @property
    def _qualifier(self) -> str:
        return ".".join(self._scope)

    def _qualified(self, name: str) -> str:
        return f"{self._qualifier}.{name}" if self._scope else name

    @property
    def _current_symbol(self) -> str | None:
        return self._qualifier or None

    # -- imports -------------------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.references.append(
                ParsedReference(
                    edge_type=EdgeType.IMPORTS,
                    target_kind=EntityType.MODULE,
                    target_key=alias.name,
                    line_start=node.lineno,
                    line_end=node.end_lineno or node.lineno,
                    source_symbol=self._current_symbol,
                    meta={"alias": alias.asname} if alias.asname else {},
                )
            )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """``from x import y`` — and the form where ``y`` is the only module named.

        A relative import is recorded verbatim with its level; resolving it to a
        concrete module is the linker's job, not the parser's. What the parser
        must not do is *lose* the module, and one form used to::

            from . import helper      module=None  level=1  names=['helper']
            from .. import app        module=None  level=2  names=['app']

        ``ast`` puts nothing in ``module`` here, so recording ``node.module`` and
        the level produced the bare prefix ``"."`` — which the linker dutifully
        resolved to the package's ``__init__.py``, while ``helper`` survived only
        as a string in ``meta``. ``pkg/sub/helper.py`` got no inbound import at
        all, and "who imports helper?" answered nothing.

        So when ``module`` is absent and the level is relative, each imported name
        is *also* emitted as a candidate module ``"." * level + name`` — the same
        spelling ``from .helper import X`` already produces, which is what lets
        the two ways of writing one import produce one set of facts.

        **Candidate, not claim.** The parser cannot see the filesystem, so it
        cannot know whether ``helper`` is a submodule or a name that
        ``__init__.py`` happens to define. It says only "this could name a module
        here"; :meth:`ResolutionIndex.resolve_relative_submodule` decides, and
        emits nothing when no such module exists. Unknown is a fine answer;
        inventing ``CONSTANT.py`` is not.

        The package reference itself is kept, because importing the package is
        also what Python does. Its ``names`` are dropped in this form only: they
        are no longer names *of* that module, they are the candidates above, and
        leaving them would point a later ``helper.assist()`` at ``__init__.py``.
        """
        module = node.module or ""
        prefix = "." * node.level
        target = prefix + module
        submodules = not module and bool(node.level)
        if target:
            self.references.append(
                ParsedReference(
                    edge_type=EdgeType.IMPORTS,
                    target_kind=EntityType.MODULE,
                    target_key=target,
                    line_start=node.lineno,
                    line_end=node.end_lineno or node.lineno,
                    source_symbol=self._current_symbol,
                    meta={
                        "level": str(node.level),
                        **({} if submodules else {"names": ",".join(a.name for a in node.names)}),
                    },
                )
            )
        if submodules:
            for alias in node.names:
                # `from . import *` binds whatever the package exports. There is
                # no module named `*`, and treating one as named would be the
                # fabrication this whole change exists to avoid.
                if alias.name == "*":
                    continue
                self.references.append(
                    ParsedReference(
                        edge_type=EdgeType.IMPORTS,
                        target_kind=EntityType.MODULE,
                        target_key=prefix + alias.name,
                        line_start=node.lineno,
                        line_end=node.end_lineno or node.lineno,
                        source_symbol=self._current_symbol,
                        meta={
                            "level": str(node.level),
                            # Read by the linker: resolve this only if `prefix`
                            # really is a package. Without it, `from . import x`
                            # in a file that is in no package at all would find
                            # any `x.py` beside it.
                            "package": prefix,
                            **({"alias": alias.asname} if alias.asname else {}),
                        },
                    )
                )
        self.generic_visit(node)

    # -- definitions ---------------------------------------------------------

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        kind = EntityType.METHOD if self._in_class() else EntityType.FUNCTION
        qualified = self._qualified(node.name)
        self.symbols.append(
            ParsedSymbol(
                kind=kind,
                name=node.name,
                qualified_name=qualified,
                line_start=node.lineno,
                line_end=node.end_lineno or node.lineno,
                signature=self._signature(node),
                parent=self._current_symbol,
                meta={"async": "true"} if isinstance(node, ast.AsyncFunctionDef) else {},
            )
        )
        for decorator in node.decorator_list:
            name = _decorator_name(decorator)
            if name:
                self.references.append(
                    ParsedReference(
                        edge_type=EdgeType.REFERENCES,
                        target_kind=EntityType.SYMBOL,
                        target_key=name,
                        line_start=decorator.lineno,
                        line_end=decorator.end_lineno or decorator.lineno,
                        source_symbol=qualified,
                        meta={"role": "decorator"},
                    )
                )
        if self._http is not None and not self._scope:
            # Routes are read on module-level functions only: a decorator inside a
            # factory or a class names an application this module does not prove.
            self.references.extend(self._http.route_references(node, qualified))
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qualified = self._qualified(node.name)
        self.symbols.append(
            ParsedSymbol(
                kind=EntityType.CLASS,
                name=node.name,
                qualified_name=qualified,
                line_start=node.lineno,
                line_end=node.end_lineno or node.lineno,
                signature=f"class {node.name}",
                parent=self._current_symbol,
                meta=_field_types(node),
            )
        )
        if any(
            (_decorator_name(base) or "").rsplit(".", 1)[-1] in _ENUM_BASES for base in node.bases
        ):
            self._enum_members(node, qualified)
        for base in node.bases:
            name = _decorator_name(base)
            if name:
                self.references.append(
                    ParsedReference(
                        edge_type=EdgeType.REFERENCES,
                        target_kind=EntityType.CLASS,
                        target_key=name,
                        line_start=base.lineno,
                        line_end=base.end_lineno or base.lineno,
                        source_symbol=qualified,
                        meta={"role": "base"},
                    )
                )
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def _enum_members(self, node: ast.ClassDef, enum: str) -> None:
        """An enum's members with a plain string value, as ``Enum.MEMBER``: what the member
        *is* at run time, which a generated enum shares with the schema it came from. A
        member is reached through its enum, never by its bare name."""
        for statement in node.body:
            if not (
                isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
            ):
                continue
            name = statement.targets[0].id
            if name.startswith("_"):
                continue
            self.symbols.append(
                ParsedSymbol(
                    kind=EntityType.SYMBOL,
                    name=name,
                    qualified_name=f"{enum}.{name}",
                    line_start=statement.lineno,
                    line_end=statement.end_lineno or statement.lineno,
                    signature=f"{name} = {statement.value.value!r}",
                    parent=enum,
                    meta={"kind": ENUM_MEMBER, "value": statement.value.value},
                )
            )

    # -- calls and environment variables -------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node.func)
        if name:
            self.references.append(
                ParsedReference(
                    edge_type=EdgeType.CALLS,
                    target_kind=EntityType.SYMBOL,
                    target_key=name,
                    line_start=node.lineno,
                    line_end=node.end_lineno or node.lineno,
                    source_symbol=self._current_symbol,
                )
            )
            self._maybe_env_read(node, name)
        if self._http is not None:
            self.references.extend(self._http.call_references(node, self._current_symbol))
        self.generic_visit(node)

    def _maybe_env_read(self, node: ast.Call, name: str) -> None:
        """Record ``os.getenv("X")`` / ``os.environ.get("X")`` as an env read."""
        tail = name.split(".")[-1]
        is_getenv = tail == "getenv"
        is_environ_get = name.endswith("environ.get")
        if not (is_getenv or is_environ_get):
            return
        if not node.args:
            return
        var = _string_value(node.args[0])
        if not var:
            # A non-literal name cannot be resolved deterministically, so nothing is
            # recorded. Guessing here would be inventing evidence.
            return
        self._env_read(node, var, {"via": name})

    def visit_Subscript(self, node: ast.Subscript) -> None:
        """Record ``os.environ["X"]`` as an env read."""
        base = _decorator_name(node.value)
        if base and base.endswith("environ"):
            var = _string_value(node.slice)
            if var:
                self._env_read(node, var, {"via": f"{base}[]"})
        self.generic_visit(node)

    def _env_read(self, node: ast.expr, var: str, meta: dict[str, str]) -> None:
        if self._current_symbol is None:
            self.module_reads.append((len(self.references), node))
        self.references.append(
            ParsedReference(
                edge_type=EdgeType.READS,
                target_kind=EntityType.ENV_VAR,
                target_key=var,
                line_start=node.lineno,
                line_end=node.end_lineno or node.lineno,
                source_symbol=self._current_symbol,
                meta=meta,
            )
        )

    # -- helpers -------------------------------------------------------------

    def _in_class(self) -> bool:
        return bool(self._scope)

    @staticmethod
    def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
        args = [a.arg for a in node.args.posonlyargs + node.args.args]
        if node.args.vararg:
            args.append(f"*{node.args.vararg.arg}")
        args.extend(a.arg for a in node.args.kwonlyargs)
        if node.args.kwarg:
            args.append(f"**{node.args.kwarg.arg}")
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        return f"{prefix} {node.name}({', '.join(args)})"


class PythonParser:
    """Deterministic Python structure extraction."""

    name = "python-ast"
    # 2: whole-file spans stop at EOF, and are absent for an empty file.
    # 3: `from . import helper` names the submodule it imports, instead of only
    #    the package prefix. The bump is load-bearing: the Python file that lost
    #    the edge is byte-identical afterwards, so only a facts-version change
    #    makes an ordinary `sync` reparse it and produce the edge (V1_SPEC §12).
    # 4: routes a proven FastAPI or Flask application serves, and requests a
    #    proven `requests` or `httpx` call makes, become HTTP facts.
    # 5: declarations' uses of the names they import or the module declares, and the
    #    file's export table (repoatlas.parsers.py_uses).
    # 6: a setup.py records what its one static setup() call states about its project
    #    (repoatlas.parsers.setuptools_project). 7: and whether it reads the call's
    #    install_requires.
    # 8: a Flask application's routes are exposed at the base its flasgger Swagger 2
    #    template publishes, with a claim for the cross-repository HTTP join
    #    (repoatlas.parsers.http_python).
    # 9: and each request whose origin and path shape it states — a literal, a module
    #    constant, an f-string on a known origin — is the join's other claim.
    # 10: a module-level name one declaring statement binds is a declaration, a module
    #    value, and the uses of it land on it (repoatlas.parsers.py_uses); the uses in
    #    its own statement are its; a `type` statement or a proven `TypeAlias` is a type
    #    alias; an `@overload` group is its implementation.
    # 11: a route on a router or blueprint, on an imported receiver, or with a path
    #    naming an imported constant; a mount of an imported router or from inside an
    #    application factory; the module's receivers and path-like constants: claims the
    #    global pass serves routes from (repoatlas.graph.py_routes).
    # 12: a request on an environment default (os.environ.get / os.getenv with a literal
    #    URL) is read as that URL, and one whose path is read but whose host is not
    #    records a claim with no origin (D2: counted, never joined).
    # 13: a request on a base one module-level if/elif/else binds to a literal in every
    #    branch records one claim per literal, each decided on its own.
    # 14: Ariadne bindables, their resolvers and schema construction
    #    (repoatlas.parsers.py_graphql).
    # 15: an Enum's members with a plain string value are symbols (``Enum.MEMBER``) that
    #    record the value, which is what a generated enum shares with its schema.
    # 16: a class records the type each of its annotated fields names (``fields``);
    #    database clients, queries, typed mappings and comparisons
    #    (repoatlas.parsers.py_persistence).
    # 17: an environment read at module scope inside the statement that declares one module
    #    value is that value's (``owner: value``), as the uses in its statement are (DF3a);
    #    any other module-scope read stays the file's.
    # +pyX.Y: `ast` is the running interpreter's, and what it accepts moves with the
    #    interpreter (repoatlas.parsers.interpreter), so facts parsed by another one
    #    are never kept.
    # 18: a model's own fields are symbols (``Model.field``, kind ``field``, with ``init`` when its
    #    class's initializer is proven to bind it by keyword), and a use of a class carries the
    #    fields read through a parameter it annotates or set by constructing it (DF3b,
    #    repoatlas.parsers.py_uses).
    # 19: a request made on a `requests.Session()` or `httpx.Client(...)` the module binds once
    #    (lexically) is a join claim: a literal absolute URL as before, a rooted URL appended to
    #    a base the source states, and one on a base it does not state a claim with no origin
    #    (D2); each carries the client's name and line (repoatlas.parsers.http_python).
    # 20: a star import makes unproven only the names it could rebind, never the whole module
    #    (repoatlas.parsers.http_python).
    # 21: a name only a star import binds is a use claim naming the module's stars, and the
    #    export table records a literal or dynamic __all__ and conditional bindings
    #    (repoatlas.parsers.py_uses).
    # 22: a list or set comprehension statement mounting each element of a literal list is read
    #    as the equivalent for loop, and a literal list read other than as a loop's iterable is
    #    no longer unrolled (repoatlas.parsers.http_python).
    # 23: a route path, router prefix or mount prefix may use a module constant composed by an
    #    f-string or `+` from literals, earlier module constants and (for paths) imported
    #    constants; composed constants are offered for the global pass
    #    (repoatlas.parsers.http_python, repoatlas.graph.py_routes).
    # 24: a request's URL and an httpx client's base_url may use module constants composed by an
    #    f-string or + (and literal constants in the host, the port or the head); a host that is a
    #    value keeps the path after it (repoatlas.parsers.http_python).
    # 25: a router or blueprint mounted on a sub-application -- an application this module, or
    #    one of its factories, mounts on another -- is served nowhere, and a mount of an imported
    #    router on one is recorded unreadable (repoatlas.parsers.http_python).
    # 26: an application another module mounts at a literal FastAPI prefix is also served under
    #    it: a literal route on a FastAPI application is a `served_here` claim, an application
    #    `mount` carries `mount_prefix`, and the mounted name may be one assignment away from an
    #    import (repoatlas.parsers.http_python, repoatlas.graph.py_routes).
    # 27: a module-level class whose one base is FastAPI's or Flask's application, router or
    #    blueprint, directly or through at most three such classes in the same module, constructs a
    #    receiver of that kind when nothing in the class can change what it registers or serves
    #    (repoatlas.parsers.http_python).
    # 28: a module-level class whose one base is FastAPI's or Flask's application is offered as a
    #    claim; a module that builds an application from a class it imports -- or from a class of
    #    its own that extends one -- records the routes on it as claims the global pass serves
    #    when the defining module offers the class (repoatlas.parsers.http_python,
    #    repoatlas.graph.py_routes).
    # 29: a same-module function that registers a blueprint or router on its first-class
    #    application parameter, or hands that parameter to another such function, is a claim
    #    (``python-helper``); a call of one with a proven application of the module or of a
    #    factory is a claim (``python-helper-call``), which the global pass expands into the
    #    application's mounts (R2c-1, repoatlas.parsers.http_python, repoatlas.graph.py_routes);
    #    helper-local imports are bound in statement order (LA3/LA5 fix).
    # 30: a registration's child two attributes from a name the module imports from a package
    #    (``users.views.bp`` after ``from pkg import users``) is a mount claim that also states
    #    the head's import (``module``, ``head_import``) and the attribute between (``through``);
    #    the global pass serves it only when the packages' export tables prove each step a
    #    submodule (R2c-2, repoatlas.parsers.http_python, repoatlas.graph.py_routes).
    # 31: a subclass may declare properties, private methods and an ``__init__`` that calls the
    #    base's once and touches nothing it serves through; router, blueprint and imported-base
    #    subclasses are offered too; a function builds an application from an imported class or
    #    once per branch of an exhaustive ``if``/``match``; a helper's loop over a literal list
    #    is unrolled (repoatlas.parsers.http_python, repoatlas.graph.py_routes).
    version = f"31+{interpreter_tag()}"
    language = "Python"

    def supports(self, path: Path) -> bool:
        return path.suffix in _SUFFIXES

    def parse(self, path: Path, content: str) -> ParsedFile:
        module_name = path.stem
        try:
            tree = ast.parse(content, filename=str(path))
        except SyntaxError as exc:
            # A malformed file yields a diagnostic and no facts — never an aborted
            # sync, and never guessed structure (V1_SPEC §7).
            return ParsedFile(
                language=self.language,
                parser=self.name,
                parser_version=self.version,
                diagnostics=(
                    ParseDiagnostic(
                        severity=Severity.ERROR,
                        # `exc.msg` is CPython's, and CPython quotes the source in
                        # it --- "unmatched ']'", "invalid conversion character
                        # 'z'". The line number is a number and stays.
                        message=SYNTAX_ERROR,
                        line=exc.lineno,
                    ),
                ),
            )
        except (ValueError, RecursionError):
            return ParsedFile(
                language=self.language,
                parser=self.name,
                parser_version=self.version,
                diagnostics=(ParseDiagnostic(severity=Severity.ERROR, message=PARSE_FAILED),),
            )

        http = PythonHttp(tree)
        visitor = _Visitor(module_name, http)
        visitor.visit(tree)
        try:
            analysis = PythonUseAnalysis(tree)
            uses = python_use_references(
                analysis,
                visitor.symbols,
                package=path.name in {"__init__.py", "__init__.pyi"},
            )
            values = [
                ParsedSymbol(
                    kind=EntityType.SYMBOL,
                    name=value.name,
                    qualified_name=value.name,
                    line_start=value.line,
                    line_end=value.end_line,
                    signature=value.signature,
                    meta={"kind": value.kind},
                )
                for value in analysis.declarations
            ]
            declared = {value.name for value in analysis.declarations}
            owners = [(name, statement) for name, statement in analysis.owned if name in declared]
            fields = [
                ParsedSymbol(
                    kind=EntityType.SYMBOL,
                    name=field.name,
                    qualified_name=f"{field.model}.{field.name}",
                    line_start=field.line,
                    line_end=field.end_line,
                    signature=field.signature,
                    parent=field.model,
                    meta={"kind": FIELD, **({"init": field.init} if field.init else {})},
                )
                for field in analysis.fields
            ]
        except RecursionError:
            # Nesting deeper than the analysis can walk costs the file its uses and its
            # module values, never its other facts.
            uses, values, owners, fields = [], [], [], []

        whole_file_start, whole_file_last = whole_file_span(content)
        module_symbol = ParsedSymbol(
            kind=EntityType.MODULE,
            name=module_name,
            qualified_name=module_name,
            line_start=whole_file_start,
            line_end=whole_file_last,
        )

        setuptools = setup_py_facts(tree) if path.name == "setup.py" else None
        owned = _owned_reads(visitor, owners)
        return ParsedFile(
            language=self.language,
            parser=self.name,
            parser_version=self.version,
            # Module values last: a name one shares with the module itself names the value.
            symbols=(module_symbol, *visitor.symbols, *values, *fields),
            references=(
                *owned,
                *http.deployment_references(),
                *http.claim_references(),
                *AriadneFacts(tree).references(),
                *schema_download_references(tree),
                *PyPersistence(tree).references(),
                *uses,
                *(() if setuptools is None else (setuptools_reference(setuptools, content),)),
            ),
            diagnostics=_truncated_members(uses),
        )


def _truncated_members(uses: Sequence[ParsedReference]) -> tuple[ParseDiagnostic, ...]:
    """A warning when a use named more model-field members than its claim records (DF3b)."""
    if any(FIELDS_OMITTED in use.meta for use in uses):
        return (ParseDiagnostic(severity=Severity.WARNING, message=FIELDS_TRUNCATED),)
    return ()


def _owned_reads(visitor: _Visitor, owners: list[tuple[str, ast.stmt]]) -> list[ParsedReference]:
    """The visitor's references, each module-scope environment read inside a statement that
    declares one module value given to that value (``owner: value``), as the uses in its
    statement are (:class:`~repoatlas.parsers.py_uses.PythonUseAnalysis`). A read outside
    every such statement -- in an ``if`` test, a call at module level, ``a = b = f()`` --
    stays the file's."""
    references = list(visitor.references)
    for index, node in visitor.module_reads:
        start = (node.lineno, node.col_offset)
        end = (node.end_lineno or node.lineno, node.end_col_offset or node.col_offset)
        holding = [
            name
            for name, statement in owners
            if (statement.lineno, statement.col_offset) <= start
            and end
            <= (
                statement.end_lineno or statement.lineno,
                statement.end_col_offset or statement.col_offset,
            )
        ]
        if len(holding) == 1:
            read = references[index]
            references[index] = read.model_copy(
                update={"source_symbol": holding[0], "meta": {**read.meta, "owner": "value"}}
            )
    return references


def _field_types(node: ast.ClassDef) -> dict[str, str]:
    """``{"fields": {name: type}}`` for a class's annotated fields whose annotation names one
    type -- ``T``, ``Optional[T]``, ``T | None`` or ``"T"`` -- else nothing."""
    fields: dict[str, str] = {}
    for statement in node.body:
        if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            named = _one_type(statement.annotation)
            if named:
                fields[statement.target.id] = named
    return {"fields": json.dumps(fields, sort_keys=True)} if fields else {}


def _one_type(annotation: ast.expr) -> str | None:
    if isinstance(annotation, ast.Name):
        return annotation.id
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        return annotation.value if annotation.value.isidentifier() else None
    if (
        isinstance(annotation, ast.Subscript)
        and isinstance(annotation.value, ast.Name | ast.Attribute)
        and (
            annotation.value.id if isinstance(annotation.value, ast.Name) else annotation.value.attr
        )
        == "Optional"
    ):
        return _one_type(annotation.slice)
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        sides = [annotation.left, annotation.right]
        named = [s for s in sides if not (isinstance(s, ast.Constant) and s.value is None)]
        return _one_type(named[0]) if len(named) == 1 else None
    return None
