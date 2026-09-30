"""Python module values as declarations: what declares one, and where a use of it lands.

A module-level name one declaring statement binds is a declaration (repoatlas.parsers.
py_uses); a use of it lands on it exactly as a use of a ``def`` or ``class`` does, through
the same imports, re-exports and module attributes, and across repositories only on the
same evidence. The fixtures are in :mod:`tests.python_declarations_corpus`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.indexing.chunking import chunk_content
from repoatlas.models.capability import CapabilityContext, CapabilityScope, Presentation
from repoatlas.models.impact import ImpactRequest
from repoatlas.models.parsing import EntityType
from repoatlas.parsers.python import PythonParser
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build, use_edges, uses, verify_steps
from tests.incremental_corpus import assert_equals_fresh, edit, sync_all
from tests.python_corpus import refusals
from tests.python_declarations_corpus import PKG, WORKSPACE

V = f"{PKG}/values.py"
C = f"{PKG}/consumer.py"


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
