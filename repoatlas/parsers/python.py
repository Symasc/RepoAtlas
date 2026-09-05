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
    EdgeType,
    EntityType,
    ParsedFile,
    ParseDiagnostic,
    ParsedReference,
    ParsedSymbol,
    Severity,
)
from repoatlas.parsers.marks import whole_file_span

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

    def __init__(self, module_name: str) -> None:
        self.module_name = module_name
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
        # A relative import is recorded verbatim with its level; resolving it to a
        # concrete module is the linker's job, not the parser's.
        module = node.module or ""
        target = ("." * node.level) + module if node.level else module
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
                        "names": ",".join(a.name for a in node.names),
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
    version = "2"  # 2: whole-file spans stop at EOF, and are absent for an empty file
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
                        message=f"syntax error: {exc.msg}",
                        line=exc.lineno,
                    ),
                ),
            )
        except (ValueError, RecursionError) as exc:
            return ParsedFile(
                language=self.language,
                parser=self.name,
                parser_version=self.version,
                diagnostics=(
                    ParseDiagnostic(severity=Severity.ERROR, message=f"parse failed: {exc}"),
                ),
            )

        visitor = _Visitor(module_name)
        visitor.visit(tree)

        whole_file_start, whole_file_last = whole_file_span(content)
        module_symbol = ParsedSymbol(
            kind=EntityType.MODULE,
            name=module_name,
            qualified_name=module_name,
            line_start=whole_file_start,
            line_end=whole_file_last,
        )

        return ParsedFile(
            language=self.language,
            parser=self.name,
            parser_version=self.version,
            symbols=(module_symbol, *visitor.symbols),
            references=tuple(visitor.references),
        )
