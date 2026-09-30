"""Python parser using the stdlib ``ast`` module (V1_SPEC §7.1).

Repository code is **parsed, never imported** (V1_SPEC §17). ``ast.parse`` compiles
to a syntax tree without executing module-level code, which is what makes static
analysis of untrusted repositories safe.

``ast`` is chosen over tree-sitter for Python because it is exact, needs no
dependency, and reports precise line ranges including decorators.
"""

from __future__ import annotations

import ast
from pathlib import Path

from repoatlas.models.parsing import (
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
from repoatlas.parsers.py_uses import PythonUseAnalysis, python_use_references
from repoatlas.parsers.setuptools_project import setup_py_facts, setuptools_reference

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


class _Visitor(ast.NodeVisitor):
    """Single walk collecting symbols and unresolved references."""

    def __init__(self, module_name: str, http: PythonHttp | None = None) -> None:
        self.module_name = module_name
        self._http = http
        self.symbols: list[ParsedSymbol] = []
        self.references: list[ParsedReference] = []
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
            )
        )
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
        self.references.append(
            ParsedReference(
                edge_type=EdgeType.READS,
                target_kind=EntityType.ENV_VAR,
                target_key=var,
                line_start=node.lineno,
                line_end=node.end_lineno or node.lineno,
                source_symbol=self._current_symbol,
                meta={"via": name},
            )
        )

    def visit_Subscript(self, node: ast.Subscript) -> None:
        """Record ``os.environ["X"]`` as an env read."""
        base = _decorator_name(node.value)
        if base and base.endswith("environ"):
            var = _string_value(node.slice)
            if var:
                self.references.append(
                    ParsedReference(
                        edge_type=EdgeType.READS,
                        target_kind=EntityType.ENV_VAR,
                        target_key=var,
                        line_start=node.lineno,
                        line_end=node.end_lineno or node.lineno,
                        source_symbol=self._current_symbol,
                        meta={"via": f"{base}[]"},
                    )
                )
        self.generic_visit(node)

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
    #    value, and the uses of it land on it (repoatlas.parsers.py_uses).
    # +pyX.Y: `ast` is the running interpreter's, and what it accepts moves with the
    #    interpreter (repoatlas.parsers.interpreter), so facts parsed by another one
    #    are never kept.
    version = f"10+{interpreter_tag()}"
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
        except RecursionError:
            # Nesting deeper than the analysis can walk costs the file its uses and its
            # module values, never its other facts.
            uses, values = [], []

        whole_file_start, whole_file_last = whole_file_span(content)
        module_symbol = ParsedSymbol(
            kind=EntityType.MODULE,
            name=module_name,
            qualified_name=module_name,
            line_start=whole_file_start,
            line_end=whole_file_last,
        )

        setuptools = setup_py_facts(tree) if path.name == "setup.py" else None
        return ParsedFile(
            language=self.language,
            parser=self.name,
            parser_version=self.version,
            # Module values last: a name one shares with the module itself names the value.
            symbols=(module_symbol, *visitor.symbols, *values),
            references=(
                *visitor.references,
                *http.deployment_references(),
                *uses,
                *(() if setuptools is None else (setuptools_reference(setuptools, content),)),
            ),
        )
