"""Which TypeScript names a reference may be bound to: an import, or the scope's own variable.

The same two halves as the Python rule (tests/test_python_receivers.py): the parser marks a
call whose leading name a parameter, local or loop variable shadows (``LOCAL_RECEIVER``), and
the linker binds a name to an import only by the local names the import introduces, never by a
segment of the module path. Every name here is invented.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.models.parsing import LOCAL_RECEIVER, EdgeType
from repoatlas.parsers.typescript import TypeScriptParser
from repoatlas.services.workspace import Workspace, open_workspace
from tests.impact_corpus import build

HEAD = 'import { x } from "./x";\nimport type { T } from "./t";\n'


def marked(source: str, key: str) -> bool:
    """Whether the one call named ``key`` is marked as a local receiver."""
    found = [
        r
        for r in TypeScriptParser().parse(Path("sample.ts"), HEAD + source).references
        if r.target_key == key and r.edge_type is EdgeType.CALLS
    ]
    assert len(found) == 1, found
    return LOCAL_RECEIVER in found[0].meta


@pytest.mark.parametrize(
    ("source", "key", "local"),
    [
        pytest.param("function f(x: T) { x.go(); }", "x.go", True, id="parameter"),
        pytest.param("const f = (x: T) => x.go();", "x.go", True, id="arrow-parameter"),
        pytest.param("function f() { const x = make(); x.go(); }", "x.go", True, id="const"),
        pytest.param("function f() { var x = make(); x.go(); }", "x.go", True, id="var"),
        pytest.param(
            "function f(rows: T[]) { for (const x of rows) { x.go(); } }",
            "x.go",
            True,
            id="loop-variable",
        ),
        pytest.param(
            "function f() { try { run(); } catch (x) { x.go(); } }", "x.go", True, id="catch"
        ),
        pytest.param("function f({ x }: T) { x.go(); }", "x.go", True, id="destructured"),
        pytest.param("function f(x: T) { x.a.b.go(); }", "x.a.b.go", True, id="deep-chain"),
        pytest.param("function f(x: T) { x.a().go(); }", "x.a().go", True, id="call-chain"),
        pytest.param("function f() { x.go(); }", "x.go", False, id="the-import-itself"),
        pytest.param(
            "function f(y: T) { x.go(); }", "x.go", False, id="another-name-is-the-parameter"
        ),
        pytest.param(
            "function f(x: T) { function g() { x.go(); } }", "x.go", True, id="enclosing-function"
        ),
    ],
)
def test_a_call_is_marked_only_when_its_own_scope_binds_the_receiver(
    source: str, key: str, local: bool
) -> None:
    assert marked(source, key) is local


IMPORTS = """\
import fs, { readFile as read, stat } from "node:fs";
const { open, close: shut } = require("node:fs");
import * as ns from "./ns-file";
import "./side-effect";
const lib = require("./lib-file");

fs.go();
read.go();
stat.go();
open.go();
shut.go();
ns.go();
lib.go();
ns_file.go();
lib_file.go();
node.go();
"""

#: ``logger`` is imported and also named by a parameter; ``log_file`` only shares its name
#: with the module the import came from.
SHADOWS = """\
import { logger } from "./log_file";

export function byPath(log_file: unknown) {
  log_file.add();
}

export function parameter(logger: any) {
  logger.run();
}

export function local() {
  const logger = make();
  logger.run();
}

export function imported() {
  logger.run();
}
"""

FILES = {"imports.ts": IMPORTS, "shadows.ts": SHADOWS, "log_file.ts": "export const logger = 1;"}


@pytest.fixture
def indexed(tmp_path: Path) -> Iterator[Workspace]:
    workspace = open_workspace(
        settings=Settings.model_validate({"storage": {"database": str(tmp_path / "s/atlas.db")}}),
        require_root=False,
    )
    build(workspace, tmp_path, {"app": FILES})
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
    claimed = {key for key, _ in call_claims(indexed, "imports.ts")}
    # `ns_file`, `lib_file` and `node` are segments of a specifier, bound by no import. A
    # renamed import (`read`) and a renamed destructured property (`shut`) are not traced.
    assert claimed == {"fs.go", "stat.go", "open.go", "ns.go", "lib.go"}


def test_only_the_function_that_does_not_shadow_an_import_claims_it(indexed: Workspace) -> None:
    last = SHADOWS.splitlines().index("  logger.run();", 15) + 1
    assert call_claims(indexed, "shadows.ts") == [("logger.run", last)]
