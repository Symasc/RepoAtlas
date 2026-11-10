"""Calls on call results are not flattened into the imported callable's dotted name."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.models.parsing import EdgeType
from repoatlas.parsers.python import PythonParser
from repoatlas.services.workspace import Workspace, open_workspace
from tests.impact_corpus import build


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param("factory().open()\n", ["factory"], id="method-on-call-result"),
        pytest.param("factory.open()\n", ["factory.open"], id="direct-import-member"),
        pytest.param(
            "factory().client().open()\n",
            ["factory"],
            id="chain-of-calls-on-call-results",
        ),
        pytest.param(
            "factory.open().close()\n",
            ["factory.open"],
            id="call-result-after-import-member",
        ),
    ],
)
def test_calls_on_call_results_are_not_flattened(source: str, expected: list[str]) -> None:
    parsed = PythonParser().parse(Path("sample.py"), source)
    calls = [r.target_key for r in parsed.references if r.edge_type is EdgeType.CALLS]
    assert calls == expected


def test_a_decorator_method_on_a_call_result_is_not_a_module_member() -> None:
    parsed = PythonParser().parse(
        Path("sample.py"),
        "@factory().route\ndef endpoint():\n    pass\n\n@factory.route\ndef other():\n    pass\n",
    )
    decorators = [
        r.target_key
        for r in parsed.references
        if r.edge_type is EdgeType.REFERENCES and r.meta.get("role") == "decorator"
    ]
    assert decorators == ["factory.route"]


CHAINED_IMPORT = """\
from external import factory

factory().open()
factory.open()
"""


@pytest.fixture
def indexed(tmp_path: Path) -> Iterator[Workspace]:
    workspace = open_workspace(
        settings=Settings.model_validate({"storage": {"database": str(tmp_path / "s/atlas.db")}}),
        require_root=False,
    )
    build(workspace, tmp_path, {"app": {"calls.py": CHAINED_IMPORT}})
    yield workspace
    workspace.close()


def test_the_linker_keeps_the_inner_call_but_not_the_call_result_member(
    indexed: Workspace,
) -> None:
    rows = indexed.database.execute(
        """
        SELECT u.target_key, u.line_start FROM unresolved_refs u
        JOIN files f ON f.id = u.file_id
        WHERE f.rel_path = 'calls.py' AND u.edge_type = 'CALLS'
        ORDER BY u.line_start
        """
    ).fetchall()
    assert [(row["target_key"], row["line_start"]) for row in rows] == [
        ("factory", 3),
        ("factory.open", 4),
    ]


@pytest.fixture
def index(tmp_path: Path) -> Iterator[Callable[[dict[str, str]], Workspace]]:
    """Index one repository holding exactly ``files``; every workspace opened is closed."""
    opened: list[Workspace] = []

    def make(files: dict[str, str]) -> Workspace:
        workspace = open_workspace(
            settings=Settings.model_validate(
                {"storage": {"database": str(tmp_path / "s/atlas.db")}}
            ),
            require_root=False,
        )
        opened.append(workspace)
        return build(workspace, tmp_path, {"app": files})

    yield make
    for workspace in opened:
        workspace.close()


def links(
    workspace: Workspace, edge_type: str, source_path: str, source: str | None = None
) -> set[tuple[str, str, str]]:
    """``(source, target file, target name)`` of every resolved edge written in ``source_path``."""
    rows = workspace.database.execute(
        """
        SELECT src.name AS src_name, df.rel_path AS dst_path, dst.name AS dst_name
        FROM edges e
        JOIN entities src ON src.id = e.src_id
        JOIN entities dst ON dst.id = e.dst_id
        JOIN files f ON f.id = e.file_id
        LEFT JOIN files df ON df.id = dst.file_id
        WHERE e.type = ? AND f.rel_path = ?
        """,
        (edge_type, source_path),
    ).fetchall()
    return {
        (row["src_name"], row["dst_path"], row["dst_name"])
        for row in rows
        if source is None or row["src_name"] == source
    }


# A name the module imports `as` something that is also the name of a different class in the
# exporting module: the binding is the alias, the symbol is the original.
ALIASED_BASE = {
    "pkg/__init__.py": "",
    "pkg/base.py": "class BaseClient:\n    pass\n\n\nclass Client(BaseClient):\n    pass\n",
    "pkg/app.py": (
        "from pkg.base import Client as BaseClient\n"
        "\n\n"
        "def make():\n"
        "    return BaseClient()\n"
        "\n\n"
        "class Mine(BaseClient):\n"
        "    pass\n"
    ),
}


def test_an_aliased_from_import_resolves_to_the_imported_name(
    index: Callable[[dict[str, str]], Workspace],
) -> None:
    """``Client as BaseClient`` binds the local name ``BaseClient`` to ``Client``.

    The exporting module also defines a different ``BaseClient``. Looking the local alias up as
    the exported name would link the call and the base class to the wrong class: a false link.
    """
    workspace = index(ALIASED_BASE)
    assert links(workspace, "CALLS", "pkg/app.py", "make") == {("make", "pkg/base.py", "Client")}
    assert links(workspace, "REFERENCES", "pkg/app.py", "Mine") == {
        ("Mine", "pkg/base.py", "Client")
    }
    everything = links(workspace, "CALLS", "pkg/app.py") | links(
        workspace, "REFERENCES", "pkg/app.py"
    )
    assert not any(name == "BaseClient" for _, _, name in everything), everything


def test_two_imports_bound_to_one_alias_refuse_the_call(
    index: Callable[[dict[str, str]], Workspace],
) -> None:
    """``X`` is bound twice, to two different classes: which one is called is not knowable."""
    workspace = index(
        {
            **ALIASED_BASE,
            "pkg/app.py": (
                "from pkg.base import Client as X, BaseClient as X\n"
                "\n\n"
                "def make():\n"
                "    return X()\n"
            ),
        }
    )
    assert links(workspace, "CALLS", "pkg/app.py", "make") == set()


def test_a_fallback_import_pair_is_not_resolved_through_either_reexport(
    index: Callable[[dict[str, str]], Workspace],
) -> None:
    """``try: from fast import run / except ImportError: from slow import run``.

    ``run`` is bound by whichever import succeeds at runtime, which static reading cannot
    decide. Picking the first (or the last) re-export would present one implementation as the
    callee; both are refused.
    """
    workspace = index(
        {
            "fastpkg/__init__.py": "from .impl import run\n",
            "fastpkg/impl.py": "def run(): return 1\n",
            "slowpkg/__init__.py": "from .impl import run\n",
            "slowpkg/impl.py": "def run(): return 2\n",
            "caller.py": (
                "try:\n"
                "    from fastpkg import run\n"
                "except ImportError:\n"
                "    from slowpkg import run\n"
                "\n\n"
                "def go():\n"
                "    return run()\n"
            ),
        }
    )
    assert links(workspace, "CALLS", "caller.py", "go") == set()


def test_a_function_local_aliased_reexport_import_is_linked(
    index: Callable[[dict[str, str]], Workspace],
) -> None:
    """``from pkg import public as call`` inside the function that calls ``call``.

    The module's export table does not know a function-local import. It is the only import of
    the name and is written in the calling function, so it is that call's binding.
    """
    workspace = index(
        {
            "pkg/__init__.py": "from .impl import run as public\n",
            "pkg/impl.py": "def run(): return 1\n",
            "caller.py": "def go():\n    from pkg import public as call\n    return call()\n",
        }
    )
    assert links(workspace, "CALLS", "caller.py", "go") == {("go", "pkg/impl.py", "run")}


def test_a_function_local_fallback_import_pair_is_not_resolved(
    index: Callable[[dict[str, str]], Workspace],
) -> None:
    """The ``try``/``except ImportError`` pair, written inside the calling function.

    Two imports bind ``run``, so neither is the call's: a function scope does not make a
    conditional binding certain.
    """
    workspace = index(
        {
            "fastpkg/__init__.py": "from .impl import run\n",
            "fastpkg/impl.py": "def run(): return 1\n",
            "slowpkg/__init__.py": "from .impl import run\n",
            "slowpkg/impl.py": "def run(): return 2\n",
            "caller.py": (
                "def go():\n"
                "    try:\n"
                "        from fastpkg import run\n"
                "    except ImportError:\n"
                "        from slowpkg import run\n"
                "    return run()\n"
            ),
        }
    )
    assert links(workspace, "CALLS", "caller.py", "go") == set()


def test_an_import_in_another_function_is_not_the_calls_binding(
    index: Callable[[dict[str, str]], Workspace],
) -> None:
    """``setup`` imports ``call`` locally; ``go`` calls a name it never imported."""
    workspace = index(
        {
            "pkg/__init__.py": "from .impl import run as public\n",
            "pkg/impl.py": "def run(): return 1\n",
            "caller.py": (
                "def setup():\n"
                "    from pkg import public as call\n"
                "\n\n"
                "def go():\n"
                "    return call()\n"
            ),
        }
    )
    assert links(workspace, "CALLS", "caller.py", "go") == set()
