"""Calls on call results are not flattened into the imported callable's dotted name."""

from __future__ import annotations

from collections.abc import Iterator
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
