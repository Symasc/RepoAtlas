"""Python module values as declarations: what declares one, and where a use of it lands.

A module-level name one declaring statement binds is a declaration (repoatlas.parsers.
py_uses); a use of it lands on it exactly as a use of a ``def`` or ``class`` does, through
the same imports, re-exports and module attributes, and across repositories only on the
same evidence. The fixtures are in :mod:`tests.python_declarations_corpus`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.indexing.chunking import chunk_content
from repoatlas.models.capability import CapabilityContext, CapabilityScope, Presentation
from repoatlas.models.impact import ImpactRequest
from repoatlas.models.parsing import USES_EXPORT_TABLE, EntityType
from repoatlas.models.search import SearchRequest
from repoatlas.parsers.python import PythonParser
from repoatlas.parsers.ts_uses import USES_EXPORTS
from repoatlas.services.workspace import Workspace
from tests.conftest import PEP_695
from tests.impact_corpus import build, use_edges, uses, verify_steps
from tests.incremental_corpus import assert_equals_fresh, edit, sync_all
from tests.python_corpus import refusals
from tests.python_declarations_corpus import PKG, WORKSPACE

V = f"{PKG}/values.py"
C = f"{PKG}/consumer.py"
OVER = f"{PKG}/overloads.py"


@pytest.fixture
def declared(workspace: Workspace, tmp_path: Path) -> Workspace:
    return build(workspace, tmp_path, WORKSPACE)


def values(source: str, path: str = "m.py") -> dict[str, tuple[int | None, int | None, str | None]]:
    """name -> (first line, last line, signature) of every module value a source declares."""
    parsed = PythonParser().parse(Path(path), source)
    return {
        s.name: (s.line_start, s.line_end, s.signature)
        for s in parsed.symbols
        if s.kind is EntityType.SYMBOL and s.uses_only
    }


# -- what declares a module value ---------------------------------------------------------


def test_a_name_one_declaring_statement_binds_is_a_module_value() -> None:
    """V1-V3: an assignment, an annotated one, a bare annotation, each target of a chain."""
    source = "X = 1\nLIMIT: int = (\n    10\n)\nBARE: dict[str, int]\nA = B = []\nTOKEN = X\n"
    assert values(source) == {
        "X": (1, 1, "X"),
        "LIMIT": (2, 4, "LIMIT: int"),
        "BARE": (5, 5, "BARE: dict[str, int]"),
        "A": (6, 6, "A"),
        "B": (6, 6, "B"),
        "TOKEN": (7, 7, "TOKEN"),
    }


def test_nothing_else_declares_one() -> None:
    """Unpacking, loop, with, except and walrus targets, del, attributes and subscripts."""
    source = (
        "import os\n"
        "LOW, HIGH = 1, 2\n"
        "[FIRST] = [1]\n"
        "for ITEM in range(2):\n    pass\n"
        "with open('f') as HANDLE:\n    pass\n"
        "try:\n    pass\nexcept Exception as ERROR:\n    pass\n"
        "if (FOUND := 3):\n    pass\n"
        "GONE = 1\ndel GONE\n"
        "os.environ['K'] = 'v'\n"
        "os.sep = '/'\n"
        "def f():\n    INNER = 1\n    return INNER\n"
        "class K:\n    FIELD = 1\n"
    )
    assert values(source) == {}


def test_a_name_bound_twice_declares_nothing_whichever_way() -> None:
    """V9 and V10: rebinding, branches, a fallback, a def or class beside it, ``global``."""
    source = (
        "import sys\n"
        "TWICE = 1\nTWICE = 2\n"
        "LATE: int\nLATE = 3\n"
        "if sys.platform == 'win32':\n    SEP = ';'\nelse:\n    SEP = ':'\n"
        "try:\n    from fastjson import loads\nexcept ImportError:\n    loads = None\n"
        "class Config: ...\nConfig = dict\n"
        "def helper(): ...\nhelper = staticmethod(helper)\n"
        "COUNT = 0\ndef bump():\n    global COUNT\n    COUNT = 1\n"
    )
    assert values(source) == {}


def test_one_guarded_binding_is_the_declaration() -> None:
    """V10: under a guard or not, one binding is the one declaration."""
    source = (
        "import sys\nfrom typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    Alias = int\n"
        "if sys.version_info >= (3, 12):\n    GUARDED = 1\n"
        "try:\n    CONNECTED = sys.stdout\nexcept Exception:\n    pass\n"
    )
    assert values(source) == {
        "Alias": (4, 4, "Alias"),
        "GUARDED": (6, 6, "GUARDED"),
        "CONNECTED": (8, 8, "CONNECTED"),
    }


def test_a_star_import_after_a_value_leaves_it_undeclared() -> None:
    """A later star import could rebind it, as it could an import (P1 R4)."""
    assert values("X = 1\nfrom other import *\nY = 2\n") == {"Y": (3, 3, "Y")}


@PEP_695
def test_a_type_alias_is_one_only_where_the_syntax_says_so() -> None:
    """V11, V12: a ``type`` statement, or a ``TypeAlias`` this module proves is typing's."""
    source = (
        "import typing as t\n"
        "from typing_extensions import TypeAlias as _TypeAlias\n"
        "from mylib import TypeAlias\n"
        "Proven: t.TypeAlias = int\n"  # 4
        "Aliased: _TypeAlias = str\n"  # 5
        "Unproven: TypeAlias = float\n"  # 6: not typing's TypeAlias
        "Implicit = int\n"  # 7: an assignment, whatever it holds
        "type Result[T, E: Exception] = T | E\n"  # 8
        "type Plain = Result[int, ValueError]\n"  # 9
    )
    parsed = PythonParser().parse(Path("m.py"), source)
    kinds = {s.name: (s.meta["kind"], s.signature) for s in parsed.symbols if s.uses_only}
    assert kinds == {
        "Proven": ("type-alias", "type Proven"),
        "Aliased": ("type-alias", "type Aliased"),
        "Unproven": ("variable", "Unproven: TypeAlias"),
        "Implicit": ("variable", "Implicit"),
        "Result": ("type-alias", "type Result[T, E: Exception]"),
        "Plain": ("type-alias", "type Plain"),
    }


@PEP_695
def test_a_type_alias_is_used_and_uses_what_it_names() -> None:
    """V12: the parameters stay the statement's own; the alias owns the rest of it."""
    source = (
        "from pkg import Base\ntype Result[T: Base] = list[T]\ndef f(x: Result[int]) -> None: ...\n"
    )
    parsed = PythonParser().parse(Path("m.py"), source)
    found = {
        (r.source_symbol, r.target_key, r.meta["use"])
        for r in parsed.references
        if r.meta.get("use") and r.target_key != "export-table"
    }
    assert found == {("Result", "Base", "type"), ("f", "Result", "type")}


def test_a_value_is_no_chunk_boundary() -> None:
    """Chunks are cut where they were before module values were declarations."""
    source = (
        "SETTINGS = {\n" + "".join(f"    'k{i}': {i},\n" for i in range(40)) + "}\n"
        "def use():\n    return SETTINGS\n"
    )
    parsed = PythonParser().parse(Path("m.py"), source)
    others = tuple(s for s in parsed.symbols if not s.uses_only)
    assert len(others) < len(parsed.symbols)
    assert chunk_content(source, parsed.symbols) == chunk_content(source, others)


def bound(source: str) -> dict[str, Any]:
    """What the module binds each name to, as its export table records it."""
    parsed = PythonParser().parse(Path("m.py"), source)
    [table] = [r for r in parsed.references if r.target_key == USES_EXPORT_TABLE]
    return json.loads(table.meta[USES_EXPORTS])["names"]


#: Two stubs, then the implementation, each decorator spelled ``{}``.
GROUP = (
    "@{0}\ndef f(x: int) -> int: ...\n@{0}\ndef f(x: str) -> str: ...\ndef f(x):\n    return x\n"
)


@pytest.mark.parametrize(
    ("header", "decorator"),
    [
        ("from typing import overload", "overload"),
        ("from typing import overload as ov", "ov"),
        ("import typing as t", "t.overload"),
        ("from typing_extensions import overload", "overload"),
        ("import typing_extensions", "typing_extensions.overload"),
    ],
)
def test_an_overload_group_is_its_implementation(header: str, decorator: str) -> None:
    """VC4a: stubs whose ``overload`` this module proves is typing's, then one ``def``."""
    assert bound(f"{header}\n{GROUP.format(decorator)}")["f"] == ["local", "f", "6"]


@pytest.mark.parametrize(
    ("header", "decorator"),
    [
        ("from mylib import overload", "overload"),
        ("import mylib as typing", "typing.overload"),
        ("from typing import overload as ov", "overload"),
        ("", "overload"),
        ("from typing import overload\noverload = print", "overload"),
        ("import typing\ntyping = None", "typing.overload"),
    ],
    ids=[
        "another-module",
        "another-module-aliased",
        "unbound",
        "unimported",
        "rebound",
        "rebound-module",
    ],
)
def test_an_overload_is_typings_by_its_import_never_by_its_spelling(
    header: str, decorator: str
) -> None:
    """VC4b: a decorator named ``overload`` that no import proves is typing's proves nothing."""
    assert bound(f"{header}\n{GROUP.format(decorator)}")["f"] == ["none", "duplicate"]


STUB = "@overload\ndef f(x: int) -> int: ...\n"
IMPLEMENTATION = "def f(x):\n    return x\n"


@pytest.mark.parametrize(
    ("group", "binding"),
    [
        (f"{IMPLEMENTATION}{STUB}", ["none", "duplicate"]),
        (f"{STUB}{IMPLEMENTATION}{IMPLEMENTATION}", ["none", "duplicate"]),
        (f"{STUB}{STUB}", ["none", "duplicate"]),
        (
            f"{STUB}if sys.version_info >= (3, 12):\n    def f(x):\n        return x\n",
            ["none", "duplicate"],
        ),
        (f"{STUB}{IMPLEMENTATION}f = staticmethod(f)\n", ["none", "duplicate"]),
        (
            "if sys.version_info >= (3, 12):\n    @overload\n    def f(x: int) -> int: ...\n"
            f"{IMPLEMENTATION}",
            ["local", "f", "6"],
        ),
    ],
    ids=[
        "implementation-first",
        "two-implementations",
        "no-implementation",
        "conditional-implementation",
        "rebound-after",
        "conditional-stub",
    ],
)
def test_an_overload_group_has_one_last_unconditional_implementation(
    group: str, binding: list[str]
) -> None:
    """VC4c: whichever ``def`` is not last, not alone or not unconditional is no implementation."""
    assert bound(f"import sys\nfrom typing import overload\n{group}")["f"] == binding


def test_a_star_import_could_rebind_an_overload_group_or_its_overload() -> None:
    """P1 R4 still applies, to the group and to the ``overload`` that proves it."""
    stub = "if sys.version_info >= (3, 12):\n    @overload\n    def f(x: int) -> int: ...\n"
    group = f"{stub}{IMPLEMENTATION}"
    before = f"from other import *\nimport sys\nfrom typing import overload\n{group}"
    assert bound(before)["f"] == ["none", "star-import"]
    after = f"import sys\nfrom typing import overload\n{group}from other import *\n"
    assert bound(after)["overload"] == ["none", "star-import"]
    assert bound(after)["f"] == ["none", "duplicate"]


# -- where a use lands -----------------------------------------------------------------------


def test_every_use_of_a_module_value_and_no_other(declared: Workspace) -> None:
    """V1-V4, V6-V10, V13, V14 and the call graph's names, as one edge set."""
    assert uses(declared) == {
        # the module's own uses; LOW, TWICE, LATE, SEP and loads are no declarations
        (f"{V}#local_user", f"{V}#X", "value"),
        (f"{V}#local_user", f"{V}#ANNOTATED", "value"),
        (f"{V}#local_user", f"{V}#BARE", "value"),
        (f"{V}#local_user", f"{V}#CHECKED", "value"),
        (f"{V}#local_user", f"{V}#GUARDED", "value"),
        # V1 and V7 through the package, V2 and V3 directly, V4 as module attributes
        (f"{C}#v1", f"{V}#X", "value"),
        (f"{C}#v7", f"{V}#X", "value"),
        (f"{C}#v2", f"{V}#ANNOTATED", "value"),
        (f"{C}#v2", f"{V}#BARE", "value"),
        (f"{C}#v4", f"{V}#X", "value"),
        (f"{C}#v4_alias", f"{V}#ANNOTATED", "value"),
        # V10: a TYPE_CHECKING declaration, used in a string annotation
        (f"{C}#v10", f"{V}#CHECKED", "type"),
        # V13: the object, never an attribute of it
        (f"{C}#v13", f"{V}#obj", "value"),
        # V8: `rebound.py` rebinds X, so only its own call remains
        (f"{PKG}/rebound.py", f"{PKG}/rebound.py#wrap", "call"),
        # V5: the alias is its own declaration, and uses what it aliases
        (f"{V}#ALIAS", f"{V}#X", "value"),
        (f"{C}#v5", f"{V}#ALIAS", "value"),
        # a declaration over several lines, at its first, owning its statement
        (f"{V}#SPREAD", f"{V}#X", "value"),
        (f"{C}#v_spread", f"{V}#SPREAD", "value"),
        # module values called: USES lands on them; CALLS is untouched (below). The
        # statement declaring `get` is `get`'s.
        (f"{PKG}/calls.py#get", f"{PKG}/calls.py#C", "call"),
        (f"{PKG}/calls.py#run", f"{PKG}/calls.py#handler", "call"),
        (f"{PKG}/calls.py#run", f"{PKG}/calls.py#get", "call"),
        (f"{PKG}/caller.py#remote", f"{PKG}/calls.py#handler", "call"),
        (f"{PKG}/caller.py#remote", f"{PKG}/calls.py#get", "call"),
        # V14 across repositories, and the rest of the chain V16 walks
        (
            "src/client_app/http.py#make_request",
            "src/timeout_config/config.py#DEFAULT_TIMEOUT",
            "value",
        ),
        ("src/client_app/http.py#fetch", "src/client_app/http.py#make_request", "call"),
        ("src/client_app/http.py#SLOW", "src/timeout_config/config.py#DEFAULT_TIMEOUT", "value"),
        ("src/client_app/http.py#slow_request", "src/client_app/http.py#SLOW", "value"),
        ("tests/test_http.py#test_fetch", "src/client_app/http.py#fetch", "call"),
        ("tests/test_http.py#test_slow", "src/client_app/http.py#slow_request", "call"),
        # VC4: an overloaded function, in its module and from another (lines below)
        (f"{OVER}#local", f"{OVER}#parse", "call"),
        (f"{PKG}/overload_user.py#remote", f"{OVER}#parse", "call"),
    }


def test_a_use_lands_on_the_declaration_line_through_its_hops(declared: Workspace) -> None:
    """V6, V7 and V13: the declaration's own line, every re-export hop, the object as receiver."""
    edges = {(e["source"], e["target"]): e for e in use_edges(declared)}
    lines = dict(
        declared.database.execute(
            "SELECT e.qualified_name, e.line_start FROM entities e JOIN files f "
            "ON f.id = e.file_id WHERE f.rel_path = ? AND e.type = 'symbol'",
            (V,),
        ).fetchall()
    )
    assert lines["X"] == 3 and lines["obj"] == 23 and lines["CHECKED"] == 12
    assert lines["SPREAD"] == 28
    spread = edges[(f"{V}#SPREAD", f"{V}#X")]
    assert (spread["line"], edges[(f"{C}#v_spread", f"{V}#SPREAD")]["line"]) == (29, 31)
    v1, v7 = edges[(f"{C}#v1", f"{V}#X")], edges[(f"{C}#v7", f"{V}#X")]
    assert (v1["via"], v1["via_names"], v1["name"], v1["line"]) == (
        [f"{PKG}/__init__.py"],
        ["X"],
        "X",
        10,
    )
    assert (v7["via"], v7["via_names"], v7["imported"]) == (
        [f"{PKG}/__init__.py"],
        ["PublicX"],
        "PublicX",
    )
    v13 = edges[(f"{C}#v13", f"{V}#obj")]
    assert (v13["member"], v13["form"], v13["defined_at"]) == ("attr", "receiver", V)
    assert (f"{C}#v13", f"{V}#attr") not in edges


def test_an_overloaded_function_is_used_at_its_implementation(declared: Workspace) -> None:
    """VC4: in its own module and from another, the use lands on the last ``def``."""
    rows = declared.database.execute(
        """
        SELECT s.qualified_name, d.line_start, e.line_start FROM edges e
        JOIN entities s ON s.id = e.src_id JOIN entities d ON d.id = e.dst_id
        JOIN files df ON df.id = d.file_id WHERE e.type = 'USES' AND df.rel_path = ?
        """,
        (OVER,),
    ).fetchall()
    assert sorted(tuple(r) for r in rows) == [("local", 6, 9), ("remote", 6, 3)]


def test_what_stays_refused(declared: Workspace) -> None:
    """V8, V9, unpacking, V10's branches, V15: each with its reason."""
    refused = refusals(declared, "decl", C)
    assert {name: refused[name] for name in ("ReboundX", "TWICE", "LOW", "SEP")} == {
        "ReboundX": "duplicate",
        "TWICE": "duplicate",
        "LOW": "not-a-declaration",
        "SEP": "duplicate",
    }
    assert refusals(declared, "stray", "src/stray_app/use.py") == {
        "DEFAULT_TIMEOUT": "dependency-absent"
    }


def test_a_value_across_repositories_records_what_proved_it(declared: Workspace) -> None:
    """V14: exactly the crossing a class across repositories records."""
    [edge] = [
        e
        for e in use_edges(declared)
        if e["target"].endswith("#DEFAULT_TIMEOUT") and e["source"].endswith("#make_request")
    ]
    assert edge["basis"] == "python-dependency"
    assert edge["crossing"] == {
        "package": "pypi:timeout-config",
        "provider": "timeouts",
        "provided_at": "pyproject.toml:2",
        "entry": "src/timeout_config/config.py",
        "entry_fields": "src-layout",
        "subpath": "config",
        "declared_at": "pyproject.toml:3",
        "constraint": ">=1.0",
        "imported_at": 1,
    }


def test_search_names_no_module_value_and_impact_does(declared: Workspace) -> None:
    """Retrieval answers as before: the value's source line, never its declaration record.

    Impact is the one that names it, as it names a function.
    """
    service = CapabilityService(declared)
    context = CapabilityContext(scope=CapabilityScope.GLOBAL)
    found = service.search(SearchRequest(context=context, question="DEFAULT_TIMEOUT"))
    assert any("DEFAULT_TIMEOUT = 30" in item.text for item in found.evidence)
    assert not [
        item.entity.key
        for item in found.evidence
        if item.entity is not None and item.entity.type is EntityType.SYMBOL
    ]
    seed = service.impact(ImpactRequest(context=context, seed="DEFAULT_TIMEOUT")).seed
    assert (seed.status, seed.type, seed.path, seed.lines) == (
        "resolved",
        EntityType.SYMBOL,
        "src/timeout_config/config.py",
        (1, 1),
    )


def test_the_call_graph_is_what_it_was(declared: Workspace) -> None:
    """A module value is no callee a name matches, in its file or through an import."""
    rows = declared.database.execute(
        """
        SELECT sf.rel_path || '#' || s.qualified_name, df.rel_path || '#' || d.qualified_name
        FROM edges e JOIN entities s ON s.id = e.src_id JOIN entities d ON d.id = e.dst_id
        JOIN files sf ON sf.id = s.file_id JOIN files df ON df.id = d.file_id
        WHERE e.type = 'CALLS' AND sf.rel_path IN (?, ?)
        """,
        (f"{PKG}/calls.py", f"{PKG}/caller.py"),
    ).fetchall()
    # master's edges, name-matched to the method as they were
    assert sorted(tuple(r) for r in rows) == [
        (f"{PKG}/caller.py#remote", f"{PKG}/calls.py#C.get"),
        (f"{PKG}/calls.py#run", f"{PKG}/calls.py#C.get"),
        (f"{PKG}/calls.py#{PKG}/calls.py", f"{PKG}/calls.py#C"),
    ]


def test_a_changed_value_reaches_its_users_and_their_tests(declared: Workspace) -> None:
    """V16: no special case -- the walk follows USES from the value as from any declaration."""
    [key] = declared.database.execute(
        "SELECT key FROM entities WHERE type = 'symbol' AND qualified_name = 'DEFAULT_TIMEOUT'"
    ).fetchone()
    request = ImpactRequest(
        context=CapabilityContext(scope=CapabilityScope.GLOBAL),
        seed=key,
        presentation=Presentation.FULL,
    )
    response = CapabilityService(declared).impact(request)
    verify_steps(declared, response)
    chains = {c.name: c.chain for c in response.candidates}
    crossed = "DEFAULT_TIMEOUT -> make_request (uses, timeouts -> client via pypi:timeout-config)"
    assert chains["make_request"] == crossed
    assert chains["fetch"] == f"{crossed} -> fetch (calls)"
    # through a value derived from it, which owns its own statement's use
    slow = "DEFAULT_TIMEOUT -> SLOW (uses, timeouts -> client via pypi:timeout-config)"
    assert chains["slow_request"] == f"{slow} -> slow_request (uses)"
    assert sorted(t.name for t in response.tests) == ["test_fetch", "test_slow"]


# -- incremental == fresh ----------------------------------------------------------------------


def _replace(root: Path, path: str, old: str, new: str) -> None:
    text = (root / "decl" / path).read_text(encoding="utf-8")
    assert old in text, f"{old!r} not in {path}"
    edit(root, "decl", path, text.replace(old, new))


def test_a_value_edit_resolves_only_what_lands_on_it(
    declared: Workspace, settings: Settings, tmp_path: Path
) -> None:
    """The value changes, the declaration does not: no reader of the table is invalidated."""
    root = tmp_path / "repos"
    _replace(root, V, "X = 1\n", "X = 2\n")
    work = sync_all(declared, root)
    assert (work.mode, work.repository, work.readers) == ("incremental", 0, 0), work
    assert (f"{C}#v1", f"{V}#X", "value") in uses(declared)
    assert_equals_fresh(declared, settings, root, tmp_path, "value")


EDITS = {
    # a name becomes declared: its readers resolve again, and land
    "declared": ("LOW, HIGH = 1, 2\n", "LOW = 1\nHIGH = 2\n", (f"{C}#v9", f"{V}#LOW"), True),
    # removed
    "removed": ("ANNOTATED: int = 2\n", "", (f"{C}#v2", f"{V}#ANNOTATED"), False),
    # renamed: the re-export and every reader lose it
    "renamed": ("X = 1\n", "RENAMED = 1\n", (f"{C}#v1", f"{V}#X"), False),
    # the annotation changes, the declaration does not
    "annotation": (
        "ANNOTATED: int = 2\n",
        "ANNOTATED: float = 2\n",
        (f"{C}#v2", f"{V}#ANNOTATED"),
        True,
    ),
    # a second binding: no declaration left, wherever it was reached from
    "duplicated": ("attr = 3\n", "attr = 3\nX = 5\n", (f"{C}#v4", f"{V}#X"), False),
    # the second binding goes: a declaration again
    "deduplicated": ("TWICE = 2\n", "", (f"{C}#v9", f"{V}#TWICE"), True),
}


@pytest.mark.parametrize("tag", sorted(EDITS))
def test_a_declaration_edit_equals_a_fresh_index(
    declared: Workspace, settings: Settings, tmp_path: Path, tag: str
) -> None:
    old, new, (source, target), lands = EDITS[tag]
    root = tmp_path / "repos"
    _replace(root, V, old, new)
    sync_all(declared, root)
    assert any(u[:2] == (source, target) for u in uses(declared)) is lands
    assert_equals_fresh(declared, settings, root, tmp_path, tag)


@pytest.mark.parametrize(
    ("old", "new", "line"),
    [
        # a third stub: the implementation moves, and its readers follow it
        ("def parse(x):\n", "@overload\ndef parse(x: bytes) -> bytes: ...\ndef parse(x):\n", 8),
        # typing's no longer: no group, no implementation
        ("from typing import overload\n", "from mylib import overload\n", None),
    ],
    ids=["stub-added", "unproven"],
)
def test_an_overload_edit_equals_a_fresh_index(
    declared: Workspace, settings: Settings, tmp_path: Path, old: str, new: str, line: int | None
) -> None:
    root = tmp_path / "repos"
    _replace(root, OVER, old, new)
    work = sync_all(declared, root)
    assert work.landed + work.readers > 0, work
    [landed] = declared.database.execute(
        "SELECT max(d.line_start) FROM edges e JOIN entities s ON s.id = e.src_id "
        "JOIN entities d ON d.id = e.dst_id WHERE e.type = 'USES' AND s.qualified_name = 'remote' "
        "AND d.qualified_name = 'parse'"
    ).fetchone()
    assert landed == line
    assert_equals_fresh(declared, settings, root, tmp_path, "overload")


@pytest.mark.parametrize(
    ("old", "new", "lands"),
    [
        ("from .values import X as PublicX\n", "", False),
        ("from .values import X as PublicX\n", "from .values import ANNOTATED as PublicX\n", False),
    ],
    ids=["removed", "realiased"],
)
def test_a_re_export_edit_equals_a_fresh_index(
    declared: Workspace, settings: Settings, tmp_path: Path, old: str, new: str, lands: bool
) -> None:
    root = tmp_path / "repos"
    _replace(root, f"{PKG}/__init__.py", old, new)
    work = sync_all(declared, root)
    assert work.readers > 0, work
    assert ((f"{C}#v7", f"{V}#X", "value") in uses(declared)) is lands
    assert_equals_fresh(declared, settings, root, tmp_path, "reexport")
