"""Which declaration each Python name is bound to (repoatlas.parsers.py_uses).

Every case states one scoping or binding rule, and what the parser records under it. The
last cases hold the binding model against CPython's own symbol table: a name the model
resolves to the module must be one CPython resolves there too.
"""

from __future__ import annotations

import ast
import json
import symtable
from pathlib import Path
from typing import Any

import pytest
from repoatlas.models.parsing import USES_EXPORT_TABLE, EdgeType
from repoatlas.parsers.py_uses import PythonUseAnalysis
from repoatlas.parsers.python import PythonParser
from repoatlas.parsers.ts_uses import USES_EXPORTS

REPOSITORY = Path(__file__).resolve().parent.parent


def uses(source: str) -> set[tuple[str | None, str, str, str]]:
    """(owner, target, use, form) of every use the parser records."""
    return {
        (r.source_symbol, r.target_key, r.meta["use"], r.meta["form"]) for r in _references(source)
    }


def metas(source: str) -> dict[tuple[str | None, str], dict[str, str]]:
    return {(r.source_symbol, r.target_key): dict(r.meta) for r in _references(source)}


def exports(source: str) -> dict[str, Any]:
    parsed = PythonParser().parse(Path("m.py"), source)
    tables = [r for r in parsed.references if r.target_key == USES_EXPORT_TABLE]
    return json.loads(tables[0].meta[USES_EXPORTS]) if tables else {}


def _references(source: str) -> list[Any]:
    parsed = PythonParser().parse(Path("m.py"), source)
    return [
        r
        for r in parsed.references
        if r.edge_type is EdgeType.USES
        and r.target_key != USES_EXPORT_TABLE
        and r.meta.get("module") != "typing"
    ]


def targets(source: str) -> set[str]:
    return {target for _, target, _, _ in uses(source)}


# -- bindings ----------------------------------------------------------------------------


def test_an_import_records_its_module_name_and_alias() -> None:
    found = metas(
        "from pkg.models import User as LocalUser\n"
        "from .rel import Thing\n"
        "def f(u: LocalUser) -> Thing: ...\n"
    )
    user = found[("f", "LocalUser")]
    assert (user["module"], user["imported"], user["local"], user["binding"]) == (
        "pkg.models",
        "User",
        "LocalUser",
        "import",
    )
    assert user["bound_at"] == "1"
    assert found[("f", "Thing")]["module"] == ".rel"


def test_a_package_relative_submodule_import_names_its_package() -> None:
    found = metas("from . import helper\ndef f():\n    return helper.assist()\n")
    meta = found[("f", "helper.assist")]
    assert (meta["module"], meta["imported"], meta["member"], meta["use"]) == (
        ".",
        "helper",
        "assist",
        "call",
    )


def test_a_module_import_binds_its_head_and_records_what_it_loads() -> None:
    found = metas(
        "import pkg\nimport pkg.service\nimport pkg.models as m\n"
        "def f():\n    pkg.service.run()\n    return m.User\n"
    )
    chain = found[("f", "pkg.service.run")]
    assert (chain["binding"], chain["module"], chain["loaded"], chain["use"]) == (
        "namespace",
        "pkg",
        "pkg.service",
        "call",
    )
    alias = found[("f", "m.User")]
    assert (alias["module"], alias["member"], alias["form"]) == ("pkg.models", "User", "member")


def test_a_name_bound_twice_names_nothing() -> None:
    source = (
        "try:\n    from fast import parse\nexcept ImportError:\n    from slow import parse\n"
        "from pkg import User\nUser = User\n"
        "def f():\n    parse()\n    return User\n"
    )
    assert targets(source) == set()
    assert exports(source)["names"]["parse"] == ["none", "duplicate"]


def test_an_assignment_bound_once_is_a_declaration() -> None:
    source = "LIMIT = 3\ndef f():\n    return LIMIT\n"
    assert uses(source) == {("f", "LIMIT", "value", "read")}
    assert exports(source)["names"]["LIMIT"] == ["local", "LIMIT", "1"]


def test_an_unpacking_assignment_is_no_declaration() -> None:
    source = "LOW, HIGH = 1, 2\ndef f():\n    return LOW\n"
    assert targets(source) == set()
    assert exports(source)["names"]["LOW"] == ["none", "not-a-declaration"]


def test_a_global_rebinding_from_a_function_is_a_second_binding() -> None:
    source = (
        "from pkg import User\n"
        "def reset():\n    global User\n    User = None\n"
        "def f(x: User): ...\n"
    )
    assert targets(source) == set()


def test_a_global_declaration_reads_the_module_binding() -> None:
    source = "from pkg import User\ndef f():\n    global User\n    return User\n"
    assert targets(source) == {"User"}


# -- scopes (P8) -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "def f(User):\n    return User\n",
        "def f():\n    User = 1\n    return User\n",
        "def f():\n    for User in []:\n        pass\n    return User\n",
        "def f():\n    return [User for User in range(3)]\n",
        "def f():\n    with open('x') as User:\n        return User\n",
        "def f():\n    try:\n        pass\n    except Exception as User:\n        return User\n",
        "def f():\n    User: int\n    return User\n",
        "def f():\n    del User\n",
        "def outer():\n    User = 1\n    def inner():\n        return User\n    return inner\n",
        "def outer():\n    User = 1\n    def inner():\n"
        "        nonlocal User\n        return User\n",
        "def f():\n    return (lambda User: User)(1)\n",
        "def f():\n    match 1:\n        case User:\n            return User\n",
    ],
)
def test_a_name_bound_in_an_enclosing_scope_is_not_the_import(body: str) -> None:
    assert "User" not in targets("from pkg import User\n" + body)


def test_a_class_body_binding_hides_the_import_in_the_body_only() -> None:
    source = (
        "from pkg import User\n"
        "class Holder:\n"
        "    User = None\n"
        "    alias = User\n"
        "    def method(self):\n"
        "        return User\n"
    )
    assert uses(source) == {("Holder.method", "User", "value", "read")}


def test_a_function_import_binds_for_its_own_body_and_nested_scopes() -> None:
    source = (
        "def f():\n    from pkg import User\n    def inner():\n        return User()\n"
        "    return User\n"
    )
    assert uses(source) == {("f.inner", "User", "call", "call"), ("f", "User", "value", "read")}


def test_a_function_import_beside_another_binding_is_no_import() -> None:
    source = (
        "def f(flag):\n    from pkg import User\n    if flag:\n        User = None\n"
        "    return User\n"
    )
    assert targets(source) == set()


# -- TYPE_CHECKING and string annotations (P9) ---------------------------------------------


@pytest.mark.parametrize(
    "guard",
    [
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n",
        "import typing as t\nif t.TYPE_CHECKING:\n",
    ],
)
def test_a_type_checking_import_is_a_type_only_binding(guard: str) -> None:
    source = f'{guard}    from pkg.models import User\ndef f(u: "User") -> list["User"]: ...\n'
    found = metas(source)
    meta = found[("f", "User")]
    assert (meta["use"], meta["form"], meta["type_only"]) == ("type", "string-annotation", "true")


def test_a_string_is_read_as_an_annotation_only_where_it_is_one() -> None:
    source = (
        "from typing import Annotated, Literal, cast\nfrom pkg import User, meta\n"
        'def f(a: Literal["User"], b: Annotated[int, "User", meta]):\n'
        '    return cast("User", a)\n'
    )
    assert ("f", "User", "type", "string-annotation") not in uses(source)
    assert ("f", "meta", "value", "read") in uses(source)


# -- forms (P10) ---------------------------------------------------------------------------


def test_type_value_and_call_forms() -> None:
    source = (
        "from typing import Annotated, TypeAlias\n"
        "from pkg import Base, Result, User, dec, factory, meta\n"
        "@dec\ndef a(x: list[User]) -> User | None: ...\n"
        "@factory(1)\ndef b(x: Annotated[User, meta]) -> Result: ...\n"
        "class C(Base):\n    field: User\n"
        "def d(x):\n    return isinstance(x, User)\n"
        "Pair: TypeAlias = tuple[User, Result]\n"
        "type Alias = User\n"
    )
    assert uses(source) == {
        ("a", "dec", "value", "decorator"),
        ("a", "User", "type", "annotation"),
        ("b", "factory", "call", "decorator"),
        ("b", "User", "type", "annotation"),
        ("b", "meta", "value", "read"),
        ("b", "Result", "type", "annotation"),
        ("C", "Base", "value", "base"),
        ("C", "User", "type", "annotation"),
        ("d", "User", "value", "argument"),
        # a module value's own statement is the value's
        ("Pair", "User", "type", "type-alias"),
        ("Pair", "Result", "type", "type-alias"),
        (None, "User", "type", "type-alias"),
    }


# -- PEP 695 type-parameter scopes (P10-TP) --------------------------------------------------


def test_tp_a_a_generic_functions_parameter_is_not_the_import() -> None:
    assert (
        targets("from provider import T\ndef f[T](x: T) -> T:\n    y: T = x\n    return y\n")
        == set()
    )


def test_tp_b_a_generic_class_base_resolves_but_its_parameter_does_not() -> None:
    source = (
        "from provider import T, Base\n"
        "class C[T](Base[T]):\n    def m(self, x: T) -> T:\n        return x\n"
    )
    assert uses(source) == {("C", "Base", "value", "base")}


def test_tp_c_bounds_and_defaults_resolve_while_the_parameter_stays_local() -> None:
    source = (
        "from provider import T, Bound, Fallback\n"
        "def g[T: Bound](x: T) -> T: ...\n"
        "class D[T = Fallback]:\n    item: T\n"
        "def h[T: (Bound, Fallback)](x: T) -> T: ...\n"
    )
    assert uses(source) == {
        ("g", "Bound", "type", "annotation"),
        ("D", "Fallback", "type", "annotation"),
        ("h", "Bound", "type", "annotation"),
        ("h", "Fallback", "type", "annotation"),
    }


def test_tp_d_a_generic_alias_parameter_is_not_the_import() -> None:
    # Apart: both are the file's, so together one use would hide the other.
    assert uses("from provider import T\ntype Alias[T] = list[T]\n") == set()
    assert uses("from provider import T\ntype Other = T\n") == {(None, "T", "type", "type-alias")}


def test_tp_e_decorators_and_defaults_are_outside_the_scope() -> None:
    source = (
        "from provider import T, deco, DEFAULT\n"
        "@deco\ndef h[T](x: T = DEFAULT) -> T: ...\n"
        "@T\nclass E[T]:\n    item: T\n"
        "def k[T](x=T) -> T: ...\n"
    )
    assert uses(source) == {
        ("h", "deco", "value", "decorator"),
        ("h", "DEFAULT", "value", "default"),
        ("E", "T", "value", "decorator"),
        ("k", "T", "value", "default"),
    }


def test_tp_f_variadic_and_parameter_specs_are_local_too() -> None:
    source = (
        "from provider import Ts, P\n"
        "def f[*Ts, **P](*args: *Ts, **kw: P.kwargs) -> tuple[*Ts]: ...\n"
    )
    assert targets(source) == set()


# -- star imports (P11) --------------------------------------------------------------------


def test_p11a_a_binding_after_an_unconditional_star_stands() -> None:
    source = "from pkg.a import *\nfrom pkg.models import User\ndef f(u: User): ...\n"
    assert targets(source) == {"User"}


def test_p11b_a_binding_a_star_follows_is_refused() -> None:
    source = "from pkg.models import User\nfrom pkg.a import *\ndef f(u: User): ...\n"
    assert targets(source) == set()
    assert exports(source)["names"]["User"] == ["none", "star-import"]


def test_p11c_a_conditional_binding_after_a_star_is_refused() -> None:
    source = (
        "from pkg.a import *\nimport sys\nif sys.version_info > (3,):\n"
        "    from pkg.models import User\ndef f(u: User): ...\n"
    )
    assert "User" not in targets(source)


def test_p11_the_star_is_recorded_and_other_bindings_keep_their_identity() -> None:
    source = "from .a import *\nfrom .b import *\nfrom .models import Thing\n"
    table = exports(source)
    assert table["star"] == [".a", ".b"]
    assert table["names"]["Thing"] == ["import", ".models", "Thing"]


# -- calls (P21) -----------------------------------------------------------------------------


def test_p21_a_proven_local_call_is_recorded() -> None:
    source = "def helper():\n    return 1\ndef main():\n    return helper()\n"
    assert uses(source) == {("main", "helper", "call", "call")}


def test_p21_an_imported_callee_is_never_a_same_named_method() -> None:
    source = (
        "from worker import run\n"
        "class Job:\n    def run(self):\n        return self.run()\n"
        "def main():\n    run()\n"
    )
    found = metas(source)
    assert set(found) == {("main", "run")}
    assert (found[("main", "run")]["module"], found[("main", "run")]["use"]) == ("worker", "call")


def test_p21_shadowed_and_recursive_calls_record_nothing() -> None:
    source = (
        "def helper():\n    return 1\n"
        "def main(helper):\n    return helper()\n"
        "def loop(n):\n    return loop(n - 1)\n"
    )
    assert uses(source) == set()


def test_p21_a_member_of_a_local_class_is_a_use_of_the_class() -> None:
    source = (
        "class Maker:\n    @staticmethod\n    def make(): ...\ndef f():\n    return Maker.make()\n"
    )
    assert uses(source) == {("f", "Maker", "value", "receiver")}


# -- attribution ---------------------------------------------------------------------------


def test_a_declaration_carried_by_two_definitions_gives_its_uses_to_the_file() -> None:
    source = (
        "from pkg import User\n"
        "class Box:\n"
        "    @property\n    def item(self) -> User: ...\n"
        "    @item.setter\n    def item(self, value: User) -> None: ...\n"
    )
    assert uses(source) == {(None, "User", "type", "annotation")}


def test_the_export_table_holds_every_module_binding() -> None:
    source = (
        "import os\nimport pkg.sub\nfrom . import helper\nfrom .models import User as Public\n"
        "LIMIT = 3\nclass Thing: ...\ndef __getattr__(name): ...\n"
    )
    table = exports(source)
    assert table["names"] == {
        "LIMIT": ["local", "LIMIT", "5"],
        "Public": ["import", ".models", "User"],
        "Thing": ["local", "Thing", "6"],
        "__getattr__": ["local", "__getattr__", "7"],
        "helper": ["import", ".", "helper"],
        "os": ["namespace", "os"],
        "pkg": ["namespace", "pkg"],
    }
    assert table["getattr"] is True


# -- CPython's own symbol table -------------------------------------------------------------

#: Sources whose every scoping rule above must agree with ``symtable``.
ORACLE_SOURCES = (
    "from pkg import User, run\n"
    "def a(User):\n    return User\n"
    "def b():\n    return [User for User in range(3)] + [User]\n"
    "def c():\n    run()\n    def d():\n        return User\n    return d\n"
    "class K:\n    User = 1\n    def m(self):\n        return User\n"
    "def e():\n    from pkg import Thing\n    return Thing\n"
    "def g[T](x: T) -> T:\n    return User\n"
    "def h():\n    global User\n    return User\n"
    "def i():\n    return (lambda v: run(v))(User)\n",
)


def _function_tables(table: symtable.SymbolTable) -> dict[tuple[int, str], symtable.SymbolTable]:
    found: dict[tuple[int, str], symtable.SymbolTable] = {}
    pending = [table]
    counts: dict[tuple[int, str], int] = {}
    while pending:
        current = pending.pop()
        if current.get_type() == symtable.SymbolTableType.FUNCTION:
            key = (current.get_lineno(), current.get_name())
            counts[key] = counts.get(key, 0) + 1
            found[key] = current
        pending.extend(current.get_children())
    return {key: value for key, value in found.items() if counts[key] == 1}


Scoped = ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.GeneratorExp
Span = tuple[tuple[int, int], tuple[int, int]]


def _body_span(node: Scoped) -> Span:
    first: ast.AST
    last: ast.AST
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        first, last = node.body[0], node.body[-1]
    else:
        first = last = node.body if isinstance(node, ast.Lambda) else node
    start = (getattr(first, "lineno", 0), getattr(first, "col_offset", 0))
    end = (getattr(last, "end_lineno", 0) or 0, getattr(last, "end_col_offset", 0) or 0)
    return start, end


def disagreements(source: str) -> list[str]:
    """Names the model resolves to the module, or to a function's import, that CPython does not."""
    tree = ast.parse(source)
    analysis = PythonUseAnalysis(tree, record=True)
    tables = _function_tables(symtable.symtable(source, "<oracle>", "exec"))
    scopes = [
        (_body_span(node), node)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.GeneratorExp)
    ]
    found: list[str] = []
    assert analysis.decisions is not None
    for line, column, name, decision in analysis.decisions:
        if decision not in {"module", "local-import"}:
            continue
        inside = [(span, node) for span, node in scopes if span[0] <= (line, column) < span[1]]
        if not inside:
            continue
        _, node = max(inside, key=lambda item: item[0][0])
        name_of = (
            "lambda"
            if isinstance(node, ast.Lambda)
            else "genexpr"
            if isinstance(node, ast.GeneratorExp)
            else node.name
        )
        table = tables.get((node.lineno, name_of))
        if table is None:
            continue
        try:
            symbol = table.lookup(name)
        except KeyError:
            continue
        agrees = (
            symbol.is_global()
            if decision == "module"
            else symbol.is_local() and symbol.is_imported()
        ) or (decision == "local-import" and symbol.is_free())
        if not agrees:
            found.append(f"{line}:{column} {name} {decision}")
    return found


def test_the_oracle_sources_agree_with_cpython() -> None:
    for source in ORACLE_SOURCES:
        assert disagreements(source) == []


def test_every_repository_source_agrees_with_cpython() -> None:
    """RepoAtlas's own tree: every module-scope resolution is one CPython makes too."""
    failures: dict[str, list[str]] = {}
    for path in sorted((REPOSITORY / "repoatlas").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        found = disagreements(source)
        if found:
            failures[str(path.relative_to(REPOSITORY))] = found
    assert failures == {}
