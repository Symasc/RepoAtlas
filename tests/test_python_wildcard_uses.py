"""``from x import *`` resolves a name only it could bind to the declaration it proves (M2b).

A name no binding of the module supplies, read in a module with a star import, is a claim naming
the module's stars. The global pass answers it only when every star's module says so: the
literal ``__all__`` (or, without one, the public names) exports it, it is bound once and
unconditionally there, no other star provides a different declaration, and the star is
strictly above the first read. Half of this file is what resolves; the other half is each way
the answer is refused, and that a refusal costs nothing else. The parser's own facts (the star
claim, the export table's ``all`` and ``conditional``) are held beside it, and an edit to any
module a star names is held to a fresh index.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from repoatlas.config.settings import Settings
from repoatlas.models.parsing import USES_EXPORT_TABLE
from repoatlas.parsers import python as python_parser
from repoatlas.parsers.http_facts import PENDING_FACT, PYTHON_MOUNT, PYTHON_ROUTE
from repoatlas.parsers.interpreter import interpreter_tag
from repoatlas.parsers.python import PythonParser
from repoatlas.parsers.ts_uses import USES_EXPORTS
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace
from repoatlas.sync.planner import facts_version
from tests.impact_corpus import build, use_edges, uses
from tests.incremental_corpus import assert_equals_fresh, edit, sync_all
from tests.python_corpus import PKG as PYFIX_PKG
from tests.python_corpus import PYFIX, refusals
from tests.test_http_python import _parse
from tests.test_http_python_routers import served

REPO = "wc"
PKG = "src/wc"
PYPROJECT = (
    '[project]\nname = "wc-dist"\nversion = "1.0"\n'
    '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
)
STAR = "from .x import *\n"
BOTH = "from .x import *\nfrom .y import *\n"
CALL_FOO = "def f():\n    foo()\n"
FOO = "def foo(): ...\n"
OTHER = "def other(): ...\n"


def project(files: dict[str, str]) -> dict[str, str]:
    """A src-layout project (``src/wc``) holding ``files``, beside its empty ``__init__``."""
    return {
        "pyproject.toml": PYPROJECT,
        f"{PKG}/__init__.py": "",
        **{f"{PKG}/{name}": text for name, text in files.items()},
    }


def at(path: str) -> str:
    return f"{PKG}/{path}"


def indexed(workspace: Workspace, tmp_path: Path, files: dict[str, str]) -> Workspace:
    return build(workspace, tmp_path, {REPO: project(files)})


def star_edges(workspace: Workspace) -> list[dict[str, Any]]:
    return [edge for edge in use_edges(workspace) if edge.get("binding") == "star"]


def star_uses(workspace: Workspace) -> set[tuple[str, str, str]]:
    return {(e["source"], e["target"], e["use"]) for e in star_edges(workspace)}


def refused(workspace: Workspace, path: str = "c.py") -> dict[str, str]:
    """Why each Python use of ``src/wc/<path>`` resolved to nothing, by target key."""
    return refusals(workspace, REPO, at(path))


def claims(source: str) -> list[dict[str, str]]:
    """The metadata of every star claim the parser records for ``source``, in order."""
    parsed = PythonParser().parse(Path("c.py"), source)
    return [dict(r.meta) for r in parsed.references if r.meta.get("binding") == "star"]


def table(source: str) -> dict[str, Any]:
    parsed = PythonParser().parse(Path("x.py"), source)
    [found] = [r for r in parsed.references if r.target_key == USES_EXPORT_TABLE]
    return json.loads(found.meta[USES_EXPORTS])


def replace(root: Path, text: str, name: str = "x.py") -> None:
    edit(root, REPO, at(name), text)


# -- what the parser records -----------------------------------------------------------------


def test_a_star_claim_names_every_star_and_the_first_line_the_name_is_read() -> None:
    source = (
        "from .x import *\n"  # 1
        "import sys\n"  # 2
        "if sys.argv:\n"  # 3
        "    from .y import *\n"  # 4
        "def f():\n"  # 5
        "    foo()\n"  # 6
        "    return foo\n"  # 7
    )
    meta = claims(source)[0]
    assert meta["binding"] == "star"
    assert (meta["local"], meta["imported"], meta["module"]) == ("foo", "foo", "")
    assert json.loads(meta["stars"]) == [[1, ".x", 0], [4, ".y", 1]]
    assert meta["first_line"] == "6"
    assert "bound_at" not in meta


def test_the_first_line_is_the_least_over_every_read_the_claim_merges() -> None:
    # A comprehension's first iterable is read before its element, so the line-4 read is
    # visited first; both are one claim, and the star must precede the earlier line.
    source = "from .x import *\nvals = [\n    foo\n    for i in range(3, foo)\n]\n"
    [meta] = [m for m in claims(source) if m["local"] == "foo"]
    assert meta["occurrences"] == "2"
    assert meta["first_line"] == "3"


def test_only_a_name_no_binding_supplies_is_a_star_claim() -> None:
    source = (
        "def g(): ...\n"
        "from .x import *\n"
        "bound = 1\n"
        "def f(p):\n"
        "    g()\n"  # bound before the star: refused outright
        "    bound\n"  # bound after it: stands, a local use
        "    p\n"  # a parameter
        "    unbound\n"
    )
    assert [m["local"] for m in claims(source)] == ["unbound"]


def test_a_module_without_a_star_has_no_star_claim() -> None:
    assert claims("def f():\n    unbound()\n") == []


def test_a_member_chain_and_a_type_use_are_claims_like_any_other() -> None:
    source = "from .x import *\ndef f(u: Foo):\n    return Bar.baz()\n"
    found = {m["local"]: m for m in claims(source)}
    assert (found["Foo"]["use"], found["Foo"]["form"]) == ("type", "annotation")
    assert (found["Bar"]["use"], found["Bar"]["member"]) == ("call", "baz")


def test_a_module_without_all_or_a_conditional_binding_keeps_its_table() -> None:
    assert set(table("import os\nLIMIT = 3\ndef f(): ...\n")) == {"names", "star"}


def test_a_literal_all_is_recorded_sorted_and_unique() -> None:
    assert table('__all__ = ["b", "a", "a"]\na = b = 1\n')["all"] == ["a", "b"]
    assert table('__all__: list[str] = ("z",)\nz = 1\n')["all"] == ["z"]
    assert table("__all__ = []\n")["all"] == []


@pytest.mark.parametrize(
    "source",
    [
        '__all__ = ["a"]\n__all__ += ["b"]\n',
        '__all__ = ["a"]\n__all__.append("b")\n',
        '__all__ = ["a"] + ["b"]\n',
        '__all__ = ["a"]\n__all__ = ["a"]\n',
        'if x:\n    __all__ = ["a"]\n',
        '__all__ = ["a"]\nexec(compile(__all__, "", "eval"))\n',
        '__all__ = list(("a",))\n',
        '__all__ = ["a", 1]\n',
        '__all__ = ["not an identifier"]\n',
        "__all__: list[str]\n",
        '__all__ = x = ["a"]\n',
        '__all__ = ["a"]\ndef f():\n    global __all__\n',
        'def f():\n    global __all__\n    __all__ = ["a"]\n',
    ],
)
def test_any_other_all_is_recorded_as_dynamic(source: str) -> None:
    assert table(source)["all"] == "dynamic"


def test_the_names_with_a_conditional_binding_are_recorded() -> None:
    source = (
        "import sys\n"
        "try:\n    import json\nexcept ImportError:\n    json = None\n"
        "if sys.argv:\n    def a(): ...\n"
        "def b(): ...\n"
    )
    assert table(source)["conditional"] == ["a", "json"]


# -- what resolves ---------------------------------------------------------------------------


def test_t1_a_literal_all_exports_exactly_the_names_it_lists(
    workspace: Workspace, tmp_path: Path
) -> None:
    x = '__all__ = ["foo"]\ndef foo(): ...\ndef bar(): ...\n'
    c = STAR + "def f():\n    foo()\n    bar()\n"
    built = indexed(workspace, tmp_path, {"x.py": x, "c.py": c})
    assert star_uses(built) == {(at("c.py#f"), at("x.py#foo"), "call")}
    assert refused(built) == {"bar": "wildcard-not-exported"}


def test_t2_without_all_every_public_name_is_exported_and_no_underscore_name(
    workspace: Workspace, tmp_path: Path
) -> None:
    x = "def foo(): ...\ndef _hid(): ...\n"
    c = STAR + "def f():\n    foo()\n    _hid()\n"
    built = indexed(workspace, tmp_path, {"x.py": x, "c.py": c})
    assert star_uses(built) == {(at("c.py#f"), at("x.py#foo"), "call")}
    assert refused(built) == {"_hid": "wildcard-not-exported"}


def test_t2_all_exports_an_underscore_name_it_lists(workspace: Workspace, tmp_path: Path) -> None:
    x = '__all__ = ["_hid"]\ndef foo(): ...\ndef _hid(): ...\n'
    c = STAR + "def f():\n    foo()\n    _hid()\n"
    built = indexed(workspace, tmp_path, {"x.py": x, "c.py": c})
    assert star_uses(built) == {(at("c.py#f"), at("x.py#_hid"), "call")}
    assert refused(built) == {"foo": "wildcard-not-exported"}


def test_t3_only_the_names_read_make_edges(workspace: Workspace, tmp_path: Path) -> None:
    x = "".join(f"def n{i:02d}(): ...\n" for i in range(50))
    c = STAR + "def f():\n    n03()\n    n41()\n"
    built = indexed(workspace, tmp_path, {"x.py": x, "c.py": c})
    assert star_uses(built) == {
        (at("c.py#f"), at("x.py#n03"), "call"),
        (at("c.py#f"), at("x.py#n41"), "call"),
    }
    assert refused(built) == {}


def test_t12_a_name_the_module_imports_is_followed_to_its_declaration(
    workspace: Workspace, tmp_path: Path
) -> None:
    files = {
        "core.py": "class App: ...\n",
        "x.py": "from .core import App\n",
        "c.py": STAR + "def f():\n    return App()\n",
    }
    built = indexed(workspace, tmp_path, files)
    assert star_uses(built) == {(at("c.py#f"), at("core.py#App"), "call")}
    [edge] = star_edges(built)
    assert (edge["via"], edge["via_names"]) == ([at("x.py")], ["App"])
    assert edge["defined_at"] == at("core.py")


def test_t15_a_member_chain_is_a_use_of_the_declaration_it_starts_at(
    workspace: Workspace, tmp_path: Path
) -> None:
    x = "class Foo:\n    @staticmethod\n    def bar(): ...\n"
    c = STAR + "def f():\n    return Foo.bar()\n"
    built = indexed(workspace, tmp_path, {"x.py": x, "c.py": c})
    [edge] = star_edges(built)
    assert (edge["source"], edge["target"]) == (at("c.py#f"), at("x.py#Foo"))
    assert (edge["use"], edge["form"], edge["member"]) == ("value", "receiver", "bar")


def test_t16_a_name_in_an_annotation_is_a_type_use(workspace: Workspace, tmp_path: Path) -> None:
    c = STAR + "def f(u: Foo):\n    return u\n"
    built = indexed(workspace, tmp_path, {"x.py": "class Foo: ...\n", "c.py": c})
    assert star_uses(built) == {(at("c.py#f"), at("x.py#Foo"), "type")}


def test_t17_a_builtin_name_is_the_stars_when_the_module_defines_it(
    workspace: Workspace, tmp_path: Path
) -> None:
    c = STAR + "def f():\n    print(1)\n"
    built = indexed(workspace, tmp_path, {"x.py": "def print(*a): ...\n", "c.py": c})
    assert star_uses(built) == {(at("c.py#f"), at("x.py#print"), "call")}


def test_t17_a_builtin_name_no_module_defines_is_not_exported(
    workspace: Workspace, tmp_path: Path
) -> None:
    c = STAR + "def f():\n    print(1)\n"
    built = indexed(workspace, tmp_path, {"x.py": FOO, "c.py": c})
    assert star_uses(built) == set()
    assert refused(built) == {"print": "wildcard-not-exported"}


def test_t18_the_edge_records_the_star_and_how_the_name_was_exported(
    workspace: Workspace, tmp_path: Path
) -> None:
    x = '__all__ = ["foo"]\ndef foo(): ...\n'
    built = indexed(workspace, tmp_path, {"x.py": x, "c.py": STAR + CALL_FOO})
    [edge] = star_edges(built)
    assert edge["binding"] == "star"
    assert (edge["specifier"], edge["local"], edge["imported"]) == (".x", "foo", "foo")
    assert (edge["basis"], edge["defined_at"], edge["name"]) == (
        "python-relative",
        at("x.py"),
        "foo",
    )
    assert edge["wildcard"] == {"line": 1, "export": "all", "stars": 1}


def test_t18_a_name_exported_by_being_public_says_so(workspace: Workspace, tmp_path: Path) -> None:
    built = indexed(workspace, tmp_path, {"x.py": FOO, "c.py": STAR + CALL_FOO})
    [edge] = star_edges(built)
    assert edge["wildcard"] == {"line": 1, "export": "public", "stars": 1}


# -- what is refused -------------------------------------------------------------------------

DYNAMIC = {
    "augmented": '__all__ = ["foo"]\n__all__ += ["bar"]\ndef foo(): ...\n',
    "extend": '__all__ = ["foo"]\n__all__.extend(["bar"])\ndef foo(): ...\n',
    "append": '__all__ = ["foo"]\n__all__.append("bar")\ndef foo(): ...\n',
    "concatenated": '__all__ = ["foo"] + ["bar"]\ndef foo(): ...\n',
    "bound-twice": '__all__ = ["foo"]\n__all__ = ["foo"]\ndef foo(): ...\n',
    "conditional": 'import sys\nif sys.argv:\n    __all__ = ["foo"]\ndef foo(): ...\n',
    "list-call": '__all__ = list(("foo",))\ndef foo(): ...\n',
    "non-string": '__all__ = ["foo", 1]\ndef foo(): ...\n',
    "global": (
        '__all__ = ["foo"]\ndef change():\n    global __all__\n    __all__ = []\ndef foo(): ...\n'
    ),
    "read": '__all__ = ["foo"]\nprint(__all__)\ndef foo(): ...\n',
}


@pytest.mark.parametrize("source", list(DYNAMIC.values()), ids=list(DYNAMIC))
def test_t4_a_dynamic_all_exports_nothing_provable(
    source: str, workspace: Workspace, tmp_path: Path
) -> None:
    built = indexed(workspace, tmp_path, {"x.py": source, "c.py": STAR + CALL_FOO})
    assert star_uses(built) == set()
    assert refused(built) == {"foo": "dynamic-all"}


def test_t5_a_module_getattr_may_supply_a_name_it_does_not_bind(
    workspace: Workspace, tmp_path: Path
) -> None:
    x = "def __getattr__(name): ...\n" + OTHER
    built = indexed(workspace, tmp_path, {"x.py": x, "c.py": STAR + CALL_FOO})
    assert star_uses(built) == set()
    assert refused(built) == {"foo": "dynamic-getattr"}


def test_t5_a_name_the_module_binds_is_not_the_getattrs(
    workspace: Workspace, tmp_path: Path
) -> None:
    x = "def __getattr__(name): ...\n" + FOO
    built = indexed(workspace, tmp_path, {"x.py": x, "c.py": STAR + CALL_FOO})
    assert star_uses(built) == {(at("c.py#f"), at("x.py#foo"), "call")}


NOT_PROVEN = {
    "conditional": "import sys\nif sys.argv:\n    def foo(): ...\n",
    "duplicate": "def foo(): ...\ndef foo(): ...\n",
    "type-checking": (
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from .y import foo\n"
    ),
    "try-import": "try:\n    from .y import foo\nexcept ImportError:\n    foo = None\n",
    "not-a-declaration": "foo, other = 1, 2\n",
    "before-its-own-star": "def foo(): ...\nfrom .z import *\n",
}


@pytest.mark.parametrize("source", list(NOT_PROVEN.values()), ids=list(NOT_PROVEN))
def test_t10_a_conditional_or_repeated_binding_is_not_proven_to_export(
    source: str, workspace: Workspace, tmp_path: Path
) -> None:
    files = {"x.py": source, "y.py": FOO, "z.py": OTHER, "c.py": STAR + CALL_FOO}
    built = indexed(workspace, tmp_path, files)
    assert star_uses(built) == set()
    assert refused(built) == {"foo": "not-proven-export"}


def test_t11_a_star_inside_the_module_defers_the_names_it_does_not_bind(
    workspace: Workspace, tmp_path: Path
) -> None:
    files = {"x.py": "from .z import *\n", "z.py": FOO, "c.py": STAR + CALL_FOO}
    built = indexed(workspace, tmp_path, files)
    assert star_uses(built) == set()
    assert refused(built) == {"foo": "star-reexport-deferred"}


def test_t11_a_name_the_module_binds_after_its_own_star_is_followed(
    workspace: Workspace, tmp_path: Path
) -> None:
    files = {"x.py": "from .z import *\n" + FOO, "z.py": OTHER, "c.py": STAR + CALL_FOO}
    built = indexed(workspace, tmp_path, files)
    assert star_uses(built) == {(at("c.py#f"), at("x.py#foo"), "call")}


def test_t11_a_name_the_module_imports_from_a_module_with_a_star_is_deferred(
    workspace: Workspace, tmp_path: Path
) -> None:
    files = {
        "x.py": "from .w import foo\n",
        "w.py": "from .z import *\n",
        "z.py": FOO,
        "c.py": STAR + CALL_FOO,
    }
    built = indexed(workspace, tmp_path, files)
    assert star_uses(built) == set()
    assert refused(built) == {"foo": "star-reexport-deferred"}


def test_t10_a_re_exported_name_its_source_binds_twice_is_not_proven(
    workspace: Workspace, tmp_path: Path
) -> None:
    files = {
        "x.py": "from .w import foo\n",
        "w.py": "def foo(): ...\ndef foo(): ...\n",
        "c.py": STAR + CALL_FOO,
    }
    built = indexed(workspace, tmp_path, files)
    assert star_uses(built) == set()
    assert refused(built) == {"foo": "not-proven-export"}


def test_a_reason_the_trace_gives_for_a_re_export_passes_through(
    workspace: Workspace, tmp_path: Path
) -> None:
    files = {"x.py": "from nowhere import foo\n", "c.py": STAR + CALL_FOO}
    built = indexed(workspace, tmp_path, files)
    assert star_uses(built) == set()
    assert refused(built) == {"foo": "module-not-found"}


def test_t13_a_name_all_lists_that_the_module_does_not_bind_is_not_its_submodule(
    workspace: Workspace, tmp_path: Path
) -> None:
    files = {
        "pkg/__init__.py": '__all__ = ["sub"]\n',
        "pkg/sub.py": "VALUE = 1\n",
        "c.py": "from .pkg import *\ndef f():\n    sub()\n",
    }
    built = indexed(workspace, tmp_path, files)
    assert star_uses(built) == set()
    assert refused(built) == {"sub": "not-proven-export"}


def test_t14_a_star_module_that_resolves_to_nothing_leaves_every_name_unknown(
    workspace: Workspace, tmp_path: Path
) -> None:
    c = "from tkinter import *\n" + STAR + CALL_FOO
    built = indexed(workspace, tmp_path, {"x.py": FOO, "c.py": c})
    assert star_uses(built) == set()
    assert refused(built) == {"foo": "wildcard-module-unresolved"}


def test_a_name_no_star_module_exports_is_refused(workspace: Workspace, tmp_path: Path) -> None:
    built = indexed(workspace, tmp_path, {"x.py": OTHER, "c.py": STAR + CALL_FOO})
    assert star_uses(built) == set()
    assert refused(built) == {"foo": "wildcard-not-exported"}


def test_a_module_binding_nothing_records_no_export_table(
    workspace: Workspace, tmp_path: Path
) -> None:
    built = indexed(workspace, tmp_path, {"x.py": "# nothing\n", "c.py": STAR + CALL_FOO})
    assert star_uses(built) == set()
    assert refused(built) == {"foo": "no-export-table"}


# -- several stars, and where they stand -----------------------------------------------------


def test_t6_two_modules_defining_the_name_are_ambiguous(
    workspace: Workspace, tmp_path: Path
) -> None:
    files = {"x.py": FOO, "y.py": FOO, "c.py": BOTH + CALL_FOO}
    built = indexed(workspace, tmp_path, files)
    assert star_uses(built) == set()
    assert refused(built) == {"foo": "ambiguous-wildcard"}


def test_t6_the_one_module_defining_the_name_provides_it(
    workspace: Workspace, tmp_path: Path
) -> None:
    files = {"x.py": OTHER, "y.py": FOO, "c.py": BOTH + CALL_FOO}
    built = indexed(workspace, tmp_path, files)
    assert star_uses(built) == {(at("c.py#f"), at("y.py#foo"), "call")}
    [edge] = star_edges(built)
    assert edge["wildcard"] == {"line": 2, "export": "public", "stars": 2}


def test_t6_a_dynamic_module_makes_the_name_unknown_whoever_else_defines_it(
    workspace: Workspace, tmp_path: Path
) -> None:
    files = {"x.py": "__all__ = list()\n", "y.py": FOO, "c.py": BOTH + CALL_FOO}
    built = indexed(workspace, tmp_path, files)
    assert star_uses(built) == set()
    assert refused(built) == {"foo": "dynamic-all"}


def test_t6_stars_leading_to_one_declaration_are_one_edge(
    workspace: Workspace, tmp_path: Path
) -> None:
    files = {
        "z.py": FOO,
        "x.py": "from .z import foo\n",
        "y.py": "from .z import foo\n",
        "c.py": BOTH + CALL_FOO,
    }
    built = indexed(workspace, tmp_path, files)
    assert star_uses(built) == {(at("c.py#f"), at("z.py#foo"), "call")}
    [edge] = star_edges(built)
    # Recorded through the last of the stars that provide it.
    assert edge["wildcard"] == {"line": 2, "export": "public", "stars": 2}
    assert (edge["via"], edge["via_names"], edge["specifier"]) == ([at("y.py")], ["foo"], ".y")


def test_t6_the_first_unknown_in_source_order_is_the_reason(
    workspace: Workspace, tmp_path: Path
) -> None:
    files = {
        "x.py": "def __getattr__(name): ...\n",
        "y.py": "__all__ = list()\n",
        "c.py": BOTH + CALL_FOO,
    }
    built = indexed(workspace, tmp_path, files)
    assert refused(built) == {"foo": "dynamic-getattr"}


def test_t8_a_star_below_the_first_read_does_not_bind_it(
    workspace: Workspace, tmp_path: Path
) -> None:
    c = "def f():\n    foo()\n" + STAR
    built = indexed(workspace, tmp_path, {"x.py": FOO, "c.py": c})
    assert star_uses(built) == set()
    assert refused(built) == {"foo": "wildcard-after-use"}


def test_t8_a_read_on_the_stars_own_line_is_refused(workspace: Workspace, tmp_path: Path) -> None:
    # Lines compare, so a read beside the star is refused: a documented over-refusal.
    built = indexed(workspace, tmp_path, {"x.py": FOO, "c.py": "from .x import *; foo()\n"})
    assert star_uses(built) == set()
    assert refused(built) == {"foo": "wildcard-after-use"}


def test_t8_the_earliest_providing_star_is_the_one_that_must_come_first(
    workspace: Workspace, tmp_path: Path
) -> None:
    files = {"z.py": FOO, "x.py": "from .z import foo\n", "y.py": "from .z import foo\n"}
    c = "from .x import *\ndef f():\n    foo()\nfrom .y import *\n"
    built = indexed(workspace, tmp_path, {**files, "c.py": c})
    assert star_uses(built) == {(at("c.py#f"), at("z.py#foo"), "call")}


def test_t9_a_star_that_may_not_run_does_not_bind(workspace: Workspace, tmp_path: Path) -> None:
    c = "try:\n    from .x import *\nexcept ImportError:\n    pass\n" + CALL_FOO
    built = indexed(workspace, tmp_path, {"x.py": FOO, "c.py": c})
    assert star_uses(built) == set()
    # `ImportError` is a free name the star does not export: refused like any other builtin.
    assert refused(built) == {"foo": "conditional-wildcard", "ImportError": "wildcard-not-exported"}


# -- shadowing: a binding of the module is never the star's ----------------------------------

SHADOWED = {
    "def-before-star": "def foo(): ...\nfrom .x import *\ndef g():\n    return foo()\n",
    "del": "from .x import *\ndef g():\n    return foo()\ndel foo\n",
    "global": (
        "from .x import *\ndef s():\n    global foo\n    foo = 1\ndef g():\n    return foo()\n"
    ),
    "conditional-after": "from .x import *\nif True:\n    foo = 1\ndef g():\n    return foo()\n",
    "bound-twice": "from .x import *\nfoo = 1\nfoo = 2\ndef g():\n    return foo()\n",
}


@pytest.mark.parametrize("source", list(SHADOWED.values()), ids=list(SHADOWED))
def test_t7_a_name_the_module_binds_is_no_star_claim(
    source: str, workspace: Workspace, tmp_path: Path
) -> None:
    built = indexed(workspace, tmp_path, {"x.py": FOO, "c.py": source})
    assert star_uses(built) == set()
    assert refused(built) == {}
    assert claims(source) == []


def test_t7_a_binding_after_the_star_is_the_modules_own(
    workspace: Workspace, tmp_path: Path
) -> None:
    c = "from .x import *\nfoo = 1\ndef g():\n    return foo()\n"
    built = indexed(workspace, tmp_path, {"x.py": FOO, "c.py": c})
    assert star_uses(built) == set()
    assert (at("c.py#g"), at("c.py#foo"), "call") in uses(built)
    assert refused(built) == {}


def test_t20_the_explicit_path_and_the_bindings_a_star_leaves_are_unchanged(
    workspace: Workspace, tmp_path: Path
) -> None:
    built = build(workspace, tmp_path, {"pyfix": PYFIX})
    assert refusals(built, "pyfix", f"{PYFIX_PKG}/star_user.py") == {"User": "star-deferred"}
    assert refusals(built, "pyfix", f"{PYFIX_PKG}/star_before.py") == {}
    assert refusals(built, "pyfix", f"{PYFIX_PKG}/star_after.py") == {}
    kept = (f"{PYFIX_PKG}/star_after.py#p11a", f"{PYFIX_PKG}/models.py#User", "type")
    assert kept in uses(built)
    assert star_edges(built) == []


# -- HTTP is untouched -----------------------------------------------------------------------

_MAIN = (
    "{star}\n"
    "from fastapi import FastAPI\n"
    "app = FastAPI()\n"
    'app.include_router(router, prefix="/api")\n'
    '@router.get("/me")\n'
    "def me(): ...\n"
)
_ROUTES = "from fastapi import APIRouter\nrouter = APIRouter()\n"


def test_t19_a_name_only_a_star_binds_is_a_uses_edge_and_never_an_http_fact(
    workspace: Workspace, tmp_path: Path
) -> None:
    roles = {PYTHON_MOUNT, PYTHON_ROUTE}
    source = _MAIN.format(star="from routes import *")
    assert [r for r in _parse(source).references if r.meta.get(PENDING_FACT) in roles] == []

    files = {"main.py": _MAIN.format(star="from .routes import *"), "routes.py": _ROUTES}
    built = indexed(workspace, tmp_path, files)
    assert star_uses(built) == {
        (at("main.py"), at("routes.py#router"), "value"),
        (at("main.py#me"), at("routes.py#router"), "value"),
    }
    assert served(built) == set()


# -- an edit to any module a star names ------------------------------------------------------


def test_i1_a_module_gaining_the_name_makes_the_edge_appear(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    built = indexed(workspace, tmp_path, {"x.py": OTHER, "c.py": STAR + CALL_FOO})
    root = tmp_path / "repos"
    assert star_uses(built) == set()
    replace(root, OTHER + FOO)
    work = sync_all(built, root)
    assert work.readers > 0, work
    assert star_uses(built) == {(at("c.py#f"), at("x.py#foo"), "call")}
    assert_equals_fresh(built, settings, root, tmp_path, "i1")


def test_i2_dropping_the_name_from_all_removes_the_edge(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    x = '__all__ = ["foo", "bar"]\ndef foo(): ...\ndef bar(): ...\n'
    built = indexed(workspace, tmp_path, {"x.py": x, "c.py": STAR + CALL_FOO})
    root = tmp_path / "repos"
    assert star_uses(built) == {(at("c.py#f"), at("x.py#foo"), "call")}
    replace(root, '__all__ = ["bar"]\ndef foo(): ...\ndef bar(): ...\n')
    sync_all(built, root)
    assert star_uses(built) == set()
    assert refused(built) == {"foo": "wildcard-not-exported"}
    assert_equals_fresh(built, settings, root, tmp_path, "i2")


def test_i2b_a_module_exporting_a_name_it_did_not_makes_the_edge_ambiguous(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    """The edge into ``y`` held while ``x`` excluded the name; it must go when ``x`` exports it."""
    x = '__all__ = ["bar"]\ndef foo(): ...\ndef bar(): ...\n'
    files = {"x.py": x, "y.py": FOO, "c.py": BOTH + CALL_FOO}
    built = indexed(workspace, tmp_path, files)
    root = tmp_path / "repos"
    assert star_uses(built) == {(at("c.py#f"), at("y.py#foo"), "call")}
    replace(root, '__all__ = ["bar", "foo"]\ndef foo(): ...\ndef bar(): ...\n')
    sync_all(built, root)
    assert star_uses(built) == set()
    assert refused(built) == {"foo": "ambiguous-wildcard"}
    assert_equals_fresh(built, settings, root, tmp_path, "i2b")


def test_i3_another_module_defining_the_name_makes_the_edge_ambiguous(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    files = {"x.py": FOO, "y.py": OTHER, "c.py": BOTH + CALL_FOO}
    built = indexed(workspace, tmp_path, files)
    root = tmp_path / "repos"
    assert star_uses(built) == {(at("c.py#f"), at("x.py#foo"), "call")}
    replace(root, OTHER + FOO, "y.py")
    sync_all(built, root)
    assert star_uses(built) == set()
    assert refused(built) == {"foo": "ambiguous-wildcard"}
    assert_equals_fresh(built, settings, root, tmp_path, "i3")


def test_i4_renaming_the_name_in_the_module_removes_the_edge(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    built = indexed(workspace, tmp_path, {"x.py": FOO, "c.py": STAR + CALL_FOO})
    root = tmp_path / "repos"
    replace(root, "def bar(): ...\n")
    sync_all(built, root)
    assert star_uses(built) == set()
    assert refused(built) == {"foo": "wildcard-not-exported"}
    assert_equals_fresh(built, settings, root, tmp_path, "i4")


def test_i5_adding_and_removing_the_star_adds_and_removes_the_edge(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    built = indexed(workspace, tmp_path, {"x.py": FOO, "c.py": CALL_FOO})
    root = tmp_path / "repos"
    assert star_uses(built) == set()
    replace(root, STAR + CALL_FOO, "c.py")
    sync_all(built, root)
    assert star_uses(built) == {(at("c.py#f"), at("x.py#foo"), "call")}
    assert_equals_fresh(built, settings, root, tmp_path, "i5-added")
    replace(root, CALL_FOO, "c.py")
    sync_all(built, root)
    assert star_uses(built) == set()
    assert_equals_fresh(built, settings, root, tmp_path, "i5-removed")


def test_i6_a_star_module_gaining_a_getattr_refuses_a_name_it_does_not_bind(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    files = {"x.py": FOO, "y.py": OTHER, "c.py": BOTH + CALL_FOO}
    built = indexed(workspace, tmp_path, files)
    root = tmp_path / "repos"
    assert star_uses(built) == {(at("c.py#f"), at("x.py#foo"), "call")}
    replace(root, OTHER + "def __getattr__(name): ...\n", "y.py")
    sync_all(built, root)
    assert star_uses(built) == set()
    assert refused(built) == {"foo": "dynamic-getattr"}
    assert_equals_fresh(built, settings, root, tmp_path, "i6-getattr")


def test_i6_a_module_making_all_dynamic_refuses_its_names(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    x = '__all__ = ["foo"]\ndef foo(): ...\n'
    built = indexed(workspace, tmp_path, {"x.py": x, "c.py": STAR + CALL_FOO})
    root = tmp_path / "repos"
    assert star_uses(built) == {(at("c.py#f"), at("x.py#foo"), "call")}
    replace(root, '__all__ = ["foo"]\n__all__ += ["bar"]\ndef foo(): ...\n')
    sync_all(built, root)
    assert star_uses(built) == set()
    assert refused(built) == {"foo": "dynamic-all"}
    assert_equals_fresh(built, settings, root, tmp_path, "i6-all")


def test_i7_a_name_becoming_conditional_in_the_module_refuses_it(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    built = indexed(workspace, tmp_path, {"x.py": FOO, "c.py": STAR + CALL_FOO})
    root = tmp_path / "repos"
    assert star_uses(built) == {(at("c.py#f"), at("x.py#foo"), "call")}
    replace(root, "import sys\nif sys.argv:\n    def foo(): ...\n")
    sync_all(built, root)
    assert star_uses(built) == set()
    assert refused(built) == {"foo": "not-proven-export"}
    assert_equals_fresh(built, settings, root, tmp_path, "i7")


def test_i8_deleting_the_star_module_and_restoring_it(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    built = indexed(workspace, tmp_path, {"x.py": FOO, "c.py": STAR + CALL_FOO})
    root = tmp_path / "repos"
    (root / REPO / at("x.py")).unlink()
    sync_all(built, root)
    assert star_uses(built) == set()
    assert refused(built) == {"foo": "wildcard-module-unresolved"}
    assert_equals_fresh(built, settings, root, tmp_path, "i8-deleted")
    replace(root, FOO)
    sync_all(built, root)
    assert star_uses(built) == {(at("c.py#f"), at("x.py#foo"), "call")}
    assert_equals_fresh(built, settings, root, tmp_path, "i8-restored")


def test_i9_a_body_edit_that_moves_the_declaration_keeps_the_edge(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    x = "def a():\n    return 0\n" + FOO
    built = indexed(workspace, tmp_path, {"x.py": x, "c.py": STAR + CALL_FOO})
    root = tmp_path / "repos"
    before = star_uses(built)
    assert before == {(at("c.py#f"), at("x.py#foo"), "call")}
    replace(root, "def a():\n    print(0)\n    return 0\n" + FOO)
    work = sync_all(built, root)
    assert work.readers == 0, work
    assert star_uses(built) == before
    assert_equals_fresh(built, settings, root, tmp_path, "i9")


def test_i10_an_explicit_import_of_a_renamed_name_is_refused_as_ever(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    c = "from .x import foo\ndef f():\n    foo()\n"
    built = indexed(workspace, tmp_path, {"x.py": FOO, "c.py": c})
    root = tmp_path / "repos"
    assert (at("c.py#f"), at("x.py#foo"), "call") in uses(built)
    replace(root, "def bar(): ...\n")
    sync_all(built, root)
    assert refused(built) == {"foo": "not-exported"}
    assert star_uses(built) == set()
    assert_equals_fresh(built, settings, root, tmp_path, "i10")


def test_i11_an_index_written_before_star_claims_is_reparsed_on_upgrade(
    workspace: Workspace, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Worker processes do not see a monkeypatch: parse in this one.
    monkeypatch.setenv("REPOATLAS_SYNC_WORKERS", "1")
    current = python_parser.python_use_references

    def without_stars(*args: Any, **kwargs: Any) -> list[Any]:
        # The old parser: a name only a star could bind records nothing.
        return [r for r in current(*args, **kwargs) if r.meta.get("binding") != "star"]

    monkeypatch.setattr(python_parser, "python_use_references", without_stars)
    built = indexed(workspace, tmp_path, {"x.py": FOO, "c.py": STAR + CALL_FOO})
    assert star_uses(built) == set()

    live = facts_version(built.registry, built.dependency_registry)
    version = f"{PythonParser.name}:{PythonParser.version}"
    assert version in live, live
    # A literal, not "one less than now": a relative offset would pass with the bump deleted.
    previous = live.replace(version, f"python-ast:20+{interpreter_tag()}")
    assert previous != live
    built.database.execute(
        "UPDATE files SET parser_version = ? WHERE rel_path LIKE '%.py'", (previous,)
    )
    built.database.connection.commit()

    monkeypatch.setattr(python_parser, "python_use_references", current)
    root = tmp_path / "repos"
    report = sync(built, built.resolver().resolve(root))
    assert sum(r.changed for r in report.repositories) >= 1, report
    assert star_uses(built) == {(at("c.py#f"), at("x.py#foo"), "call")}
    assert_equals_fresh(built, settings, root, tmp_path, "i11")


def test_i12_removing_and_restoring_an_explicit_re_export_removes_and_restores_the_edge(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    """A producer's explicit re-export is a dependency: removing it must drop the consumer's edge
    to the original declaration, and restoring it must bring the edge back, fresh-equal."""
    files = {
        "core.py": "class App: ...\n",
        "x.py": "from .core import App\n",
        "c.py": STAR + "def f():\n    App()\n",
    }
    built = indexed(workspace, tmp_path, files)
    root = tmp_path / "repos"
    assert star_uses(built) == {(at("c.py#f"), at("core.py#App"), "call")}
    # A module that binds something still has an export table; an empty file has none, and a name
    # read through a module without a table is refused as unknown (no-export-table), by design.
    replace(root, "import os\n")
    sync_all(built, root)
    assert star_uses(built) == set()
    assert refused(built) == {"App": "wildcard-not-exported"}
    assert_equals_fresh(built, settings, root, tmp_path, "i12-removed")
    replace(root, "from .core import App\n")
    sync_all(built, root)
    assert star_uses(built) == {(at("c.py#f"), at("core.py#App"), "call")}
    assert_equals_fresh(built, settings, root, tmp_path, "i12-restored")


def test_i13_re_pointing_an_explicit_re_export_moves_the_edge_to_the_new_declaration(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    files = {
        "core.py": "class App: ...\n",
        "other.py": "class App: ...\n",
        "x.py": "from .core import App\n",
        "c.py": STAR + "def f():\n    App()\n",
    }
    built = indexed(workspace, tmp_path, files)
    root = tmp_path / "repos"
    assert star_uses(built) == {(at("c.py#f"), at("core.py#App"), "call")}
    replace(root, "from .other import App\n")
    sync_all(built, root)
    assert star_uses(built) == {(at("c.py#f"), at("other.py#App"), "call")}
    assert_equals_fresh(built, settings, root, tmp_path, "i13")


def test_i14_a_second_re_exporter_of_the_name_makes_the_edge_ambiguous(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    """The consumer's edge into ``x``'s re-export must go when ``y`` starts exporting another
    declaration of the same name: a second exporter the consumer also star-imports."""
    files = {
        "core.py": "class App: ...\n",
        "other.py": "class App: ...\n",
        "x.py": "from .core import App\n",
        "y.py": "import os\n",
        "c.py": BOTH + "def f():\n    App()\n",
    }
    built = indexed(workspace, tmp_path, files)
    root = tmp_path / "repos"
    assert star_uses(built) == {(at("c.py#f"), at("core.py#App"), "call")}
    replace(root, "from .other import App\n", "y.py")
    sync_all(built, root)
    assert star_uses(built) == set()
    assert refused(built) == {"App": "ambiguous-wildcard"}
    assert_equals_fresh(built, settings, root, tmp_path, "i14")
