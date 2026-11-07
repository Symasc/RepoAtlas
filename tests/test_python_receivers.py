"""Which Python names a reference may be bound to: an import, or the scope's own variable.

A call is recorded as an import claim only when its leading name is an import's. These pin the
two halves of that rule: the parser marks a reference whose name its own scope binds
(``LOCAL_RECEIVER``), and the linker binds a name to an import only by what the import
introduces, never by a segment of the module path. Every name here is invented.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.models.parsing import LOCAL_RECEIVER, EdgeType
from repoatlas.parsers.python import PythonParser
from repoatlas.services.workspace import Workspace, open_workspace
from tests.impact_corpus import build


def marked(source: str, key: str) -> bool:
    """Whether the one call or reference named ``key`` is marked as a local receiver."""
    found = [
        r
        for r in PythonParser().parse(Path("sample.py"), source).references
        if r.target_key == key and r.edge_type in {EdgeType.CALLS, EdgeType.REFERENCES}
    ]
    assert len(found) == 1, found
    return LOCAL_RECEIVER in found[0].meta


@pytest.mark.parametrize(
    ("source", "key", "local"),
    [
        pytest.param("def f(x):\n    x.go()\n", "x.go", True, id="parameter"),
        pytest.param("def f():\n    x = make()\n    x.go()\n", "x.go", True, id="assignment"),
        pytest.param(
            "def f():\n    x.go()\n    x = make()\n", "x.go", True, id="bound-later-in-the-scope"
        ),
        pytest.param(
            "def f(rows):\n    for x in rows:\n        x.go()\n", "x.go", True, id="loop-variable"
        ),
        pytest.param("run = lambda x: x.go()\n", "x.go", True, id="lambda-parameter"),
        pytest.param(
            "def f(rows):\n    return [x.go() for x in rows]\n", "x.go", True, id="comprehension"
        ),
        pytest.param(
            "def f():\n    return [x for x in x.items()]\n",
            "x.items",
            False,
            id="first-iterable-is-evaluated-outside-the-comprehension",
        ),
        pytest.param(
            "def f(x):\n    def g():\n        x.go()\n", "x.go", True, id="enclosing-function"
        ),
        pytest.param(
            "class C:\n    x = make()\n    y = x.go()\n", "x.go", True, id="class-body-statement"
        ),
        pytest.param(
            "class C:\n    x = make()\n    def m(self):\n        x.go()\n",
            "x.go",
            False,
            id="a-method-does-not-see-its-class-body",
        ),
        pytest.param(
            "def f():\n    import json\n    json.dumps(1)\n", "json.dumps", False, id="local-import"
        ),
        pytest.param(
            "def f():\n    from m import x\n    x = x.wrap()\n    x.go()\n",
            "x.go",
            False,
            id="imported-and-assigned-stays-a-claim",
        ),
        pytest.param(
            "def f():\n    global x\n    x = make()\n    x.go()\n",
            "x.go",
            False,
            id="global-belongs-to-the-module",
        ),
        pytest.param(
            "@p.deco\ndef g(p):\n    pass\n", "p.deco", False, id="decorator-is-outside-the-def"
        ),
    ],
)
def test_a_reference_is_marked_only_when_its_own_scope_binds_the_receiver(
    source: str, key: str, local: bool
) -> None:
    assert marked(source, key) is local


IMPORTS = """\
import a.b.c
import d as e
import m.n.o as p
from pkg.mod import f
from . import sibling

a.b.c.go()
e.go()
p.go()
f.go()
sibling.go()
c.go()
d.go()
o.go()
mod.go()
pkg.go()
"""

#: ``factory`` is imported and also named by a parameter and a local, and ``server_manager``
#: only shares its name with the module a name was imported from.
SHADOWS = """\
from server_manager import factory


def by_path(server_manager):
    server_manager.add()


def parameter(factory):
    factory.run()


def assigned():
    factory = make()
    factory.run()


def imported():
    factory.run()
"""


@pytest.fixture
def indexed(tmp_path: Path) -> Iterator[Workspace]:
    workspace = open_workspace(
        settings=Settings.model_validate({"storage": {"database": str(tmp_path / "s/atlas.db")}}),
        require_root=False,
    )
    build(workspace, tmp_path, {"app": {"imports.py": IMPORTS, "shadows.py": SHADOWS}})
    yield workspace
    workspace.close()


def call_claims(workspace: Workspace, path: str) -> list[tuple[str, int]]:
    rows = workspace.database.execute(
        """
        SELECT u.target_key, u.line_start FROM unresolved_refs u
        JOIN files f ON f.id = u.file_id
        WHERE f.rel_path = ? AND u.edge_type = 'CALLS' AND u.target_kind = 'symbol'
        ORDER BY u.line_start
        """,
        (path,),
    ).fetchall()
    return [(row["target_key"], row["line_start"]) for row in rows]


def test_an_import_binds_the_names_it_introduces_and_no_segment_of_its_path(
    indexed: Workspace,
) -> None:
    claimed = {key for key, _ in call_claims(indexed, "imports.py")}
    # `import a.b.c` binds `a`; an alias binds the alias; `from m import f` binds `f`; and
    # `from . import sibling` binds `sibling`. `c`, `d`, `o`, `mod` and `pkg` are none of them.
    assert claimed == {"a.b.c.go", "e.go", "p.go", "f.go", "sibling.go"}


def test_only_the_function_that_does_not_shadow_an_import_claims_it(indexed: Workspace) -> None:
    last = SHADOWS.splitlines().index("    factory.run()", 15) + 1
    assert call_claims(indexed, "shadows.py") == [("factory.run", last)]
