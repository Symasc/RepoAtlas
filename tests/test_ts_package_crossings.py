"""TypeScript ordinary edges that cross into another repository on a package link.

``app`` declares ``@fixture/contracts`` and ``contracts`` provides it. Of the ordinary edges a
package link lets cross, this file pins the ones the import test (``tests/test_ordinary_imports``)
does not: an ``EXPORTS`` of an imported name, and the calls a package link refuses (a
type-only import, an enum). Each carries the ``crossing`` that proved it; each negative below
builds a repository set in which the proof is missing, and the same file then crosses nothing.

A TypeScript heritage clause is not a recorded use (``repoatlas.parsers.ts_uses`` skips it: the
``REFERENCES`` edge already says it), so no TypeScript construct yields a cross-repository
``REFERENCES`` through a use trace. The ``REFERENCES`` crossing is pinned on the Python side, in
:mod:`tests.test_ordinary_use_trace_guards`.

The fixtures are modelled on :mod:`tests.xrepo_corpus`. Every name here is invented.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from repoatlas.graph.linker import GLOBAL_PARSER
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build
from tests.xrepo_corpus import manifest

PACKAGE = "npm:@fixture/contracts"

CONTRACTS = {
    "package.json": manifest(name="@fixture/contracts", version="1.0.0", main="src/index.ts"),
    "src/index.ts": (
        "export class CourseRole {}\n"
        "export function run() {\n  return 1;\n}\n"
        "export function other() {\n  return 2;\n}\n"
        'export enum Phase {\n  Draft = "draft",\n}\n'
    ),
}
#: A second repository providing the same package name: the name then names two.
CONTRACTS_TWIN = {
    "package.json": manifest(name="@fixture/contracts", version="2.0.0", main="src/index.ts"),
    "src/index.ts": "export class CourseRole {}\nexport function run() {\n  return 3;\n}\n",
}

EXPORTS_FILE, CALLS_FILE, REEXPORT_FILE = "src/exports.ts", "src/calls.ts", "src/reexport.ts"


def _app(*, declared: bool = True) -> dict[str, str]:
    dependencies = {"@fixture/contracts": "^1.0.0"} if declared else {}
    return {
        "package.json": manifest(name="xr-app", private=True, dependencies=dependencies),
        # an export of a name this file imported: a value read and the export itself
        EXPORTS_FILE: (
            'import { CourseRole } from "@fixture/contracts";\n'  # 1
            "export const r = CourseRole;\n"  # 2
            "export { CourseRole };\n"  # 3
        ),
        # a call, a call of a name imported for its type, and a call of an enum
        CALLS_FILE: (
            'import { run } from "@fixture/contracts";\n'  # 1
            'import type { other } from "@fixture/contracts";\n'  # 2
            'import { Phase } from "@fixture/contracts";\n'  # 3
            "export function go() {\n"
            "  run();\n"  # 5
            "  other();\n"  # 6: type-only
            '  return Phase("x");\n'  # 7: an enum is no callee
            "}\n"
        ),
        # a re-export binds no name in this file
        REEXPORT_FILE: 'export { CourseRole } from "@fixture/contracts";\n',
    }


def _crossings(workspace: Workspace, consumer: str = "app") -> list[tuple[str, str, int, Any]]:
    """(file, type, line, resolution) of the consumer's ordinary edges into another repository."""
    rows = workspace.database.execute(
        """
        SELECT sf.rel_path, e.type, e.line_start, d.qualified_name, e.resolution
        FROM edges e JOIN files sf ON sf.id = e.file_id
        JOIN repositories sr ON sr.id = sf.repo_id
        JOIN entities d ON d.id = e.dst_id
        WHERE sr.slug = ? AND d.repo_id IS NOT NULL AND d.repo_id != sr.id
          AND e.parser = ? AND e.type IN ('IMPORTS', 'CALLS', 'REFERENCES', 'EXPORTS')
        ORDER BY sf.rel_path, e.type, e.line_start
        """,
        (consumer, GLOBAL_PARSER),
    ).fetchall()
    return [
        (row[0], row[1], row[2], {"name": row[3], **json.loads(row[4] or "{}")}) for row in rows
    ]


@pytest.fixture
def linked(workspace: Workspace, tmp_path: Path) -> Workspace:
    return build(workspace, tmp_path, {"contracts": CONTRACTS, "app": _app()})


def test_an_export_of_a_package_name_the_file_imported_crosses_once(linked: Workspace) -> None:
    exports = [row for row in _crossings(linked) if row[1] == "EXPORTS"]
    [(path, _, line, record)] = exports
    assert (path, line, record["name"]) == (EXPORTS_FILE, 3, "CourseRole")
    assert (record["use"], record["form"], record["binding"]) == ("value", "export", "import")
    assert record["crossing"]["package"] == PACKAGE
    assert record["crossing"]["provider"] == "contracts"
    assert record["crossing"]["imported_at"] == 1


def test_a_call_crosses_only_as_a_call_of_a_function_or_class_the_file_imported_to_use(
    linked: Workspace,
) -> None:
    """``run()`` crosses. ``other()`` is a call of a name imported for its type, and
    ``Phase("x")`` a call of an enum: neither is a call the use trace proves (M-G3, M-G4)."""
    calls = [row for row in _crossings(linked) if row[1] == "CALLS"]
    [(path, _, line, record)] = calls
    assert (path, line, record["name"]) == (CALLS_FILE, 5, "run")
    assert record["crossing"]["package"] == PACKAGE
    assert record["crossing"]["imported_at"] == 1


def test_a_re_export_crosses_as_an_import_and_never_as_an_export(linked: Workspace) -> None:
    rows = [row for row in _crossings(linked) if row[0] == REEXPORT_FILE]
    assert [(kind, line) for _, kind, line, _ in rows] == [("IMPORTS", 1)]
    assert rows[0][3]["crossing"]["package"] == PACKAGE


def test_the_ordinary_edges_that_cross_are_exactly_these(linked: Workspace) -> None:
    """The four proofs together: nothing else of ``app`` reaches ``contracts``."""
    assert [(path, kind, line) for path, kind, line, _ in _crossings(linked)] == [
        (CALLS_FILE, "CALLS", 5),
        (CALLS_FILE, "IMPORTS", 1),
        (CALLS_FILE, "IMPORTS", 2),
        (CALLS_FILE, "IMPORTS", 3),
        (EXPORTS_FILE, "EXPORTS", 3),
        (EXPORTS_FILE, "IMPORTS", 1),
        (REEXPORT_FILE, "IMPORTS", 1),
    ]


@pytest.mark.parametrize(
    ("providers", "declared", "why"),
    [
        ({"contracts": CONTRACTS}, False, "the package is provided but app does not declare it"),
        (
            {"contracts": CONTRACTS, "contracts-twin": CONTRACTS_TWIN},
            True,
            "two repositories provide the name: neither is chosen",
        ),
    ],
)
def test_without_a_unique_declared_provider_nothing_crosses(
    workspace: Workspace,
    tmp_path: Path,
    providers: dict[str, dict[str, str]],
    declared: bool,
    why: str,
) -> None:
    indexed = build(workspace, tmp_path, {**providers, "app": _app(declared=declared)})
    assert _crossings(indexed) == [], why
