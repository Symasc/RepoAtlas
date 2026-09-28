"""Neutral change-impact fixtures, shared by the ``USES`` and impact tests.

Each corpus is a small repository written out file by file, with every relationship
it is meant to prove stated next to the source that proves it. Nothing here is taken
from a real repository or a benchmark task.

Fixture A (symbol propagation) exercises how a declaration's use of a name survives
the ways TypeScript lets a name travel: composition inside one file, a named export,
a renaming barrel, an alias import, a default, two wholesale re-exports that disagree,
a namespace import used with and without a member, a type-only import, an unrelated
importer of the same file, and every kind of shadowing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from repoatlas.services.init_service import initialize
from repoatlas.services.workspace import Workspace
from tests.conftest import init_git_repository

SYMBOLS: dict[str, str] = {
    "package.json": json.dumps({"name": "symbols", "private": True}),
    # composition within one file, and a default that names a declaration
    "src/limits.ts": (
        "export const LIMIT = 10;\n"  # 1
        "const TABLE = { max: LIMIT };\n"  # 2  TABLE uses LIMIT
        "export function cap(n: number) {\n"  # 3
        "  return Math.min(n, TABLE.max);\n"  # 4  cap uses TABLE as a receiver
        "}\n"  # 5
        "export const unrelated = 1;\n"  # 6
        "export default function defaultRule() {\n"  # 7
        "  return LIMIT;\n"  # 8  defaultRule uses LIMIT
        "}\n"
    ),
    "src/helper.ts": "export const helperValue = 2;\n",
    "src/stars-a.ts": "export const shared = 1;\nexport const onlyA = 1;\n",
    "src/stars-b.ts": "export const shared = 2;\n",
    "src/expr.ts": "export default { anonymous: true };\n",
    "src/types.ts": "export interface Shape {\n  side: number;\n}\n",
    # a barrel: a renamed re-export, a default re-exported by name, a name imported
    # and exported again, and two wholesale re-exports that both define `shared`
    "src/index.ts": (
        'export { LIMIT as MAX_ITEMS } from "./limits";\n'
        'export { default as rule } from "./limits";\n'
        'import { helperValue } from "./helper";\n'
        "export { helperValue };\n"
        'export * from "./stars-a";\n'
        'export * from "./stars-b";\n'
    ),
    # through the barrel, under an alias
    "src/check.ts": (
        'import { MAX_ITEMS as ceiling, rule, helperValue, shared, onlyA } from "./index";\n'
        "export function check(n: number) {\n"  # 2
        "  return n < ceiling && rule() && helperValue + shared + onlyA;\n"  # 3
        "}\n"
    ),
    # a namespace import, with and without a member; a default that is an expression
    "src/spaces.ts": (
        'import * as L from "./limits";\n'
        'import anonymous from "./expr";\n'
        "export const withMember = L.LIMIT;\n"  # 3
        "export const whole = Object.keys(L);\n"  # 4
        "export const fromExpression = anonymous;\n"  # 5
    ),
    # a type-only import
    "src/area.ts": (
        'import type { Shape } from "./types";\n'
        "export function area(s: Shape): number {\n"  # 2
        "  return s.side * s.side;\n"
        "}\n"
    ),
    # an unrelated importer of the file that defines LIMIT
    "src/other.ts": (
        'import { unrelated } from "./limits";\nexport const other = unrelated + 1;\n'
    ),
    # a test: its callbacks are anonymous, so its uses are the file's
    "src/limits.test.ts": (
        'import { LIMIT } from "./limits";\n'
        'describe("limits", () => {\n'
        '  it("caps", () => {\n'
        "    expect(LIMIT).toBe(10);\n"
        "  });\n"
        "});\n"
    ),
    # every kind of shadowing, and one use that escapes it
    "src/shadow.ts": (
        'import { LIMIT } from "./limits";\n'
        "export function byParameter(LIMIT: number) {\n"  # 2
        "  return LIMIT;\n"
        "}\n"
        "export function byBlock() {\n"  # 5
        "  {\n"
        "    const LIMIT = 3;\n"
        "    void LIMIT;\n"
        "  }\n"
        "  return LIMIT;\n"  # 10  a use: the block's LIMIT ended with the block
        "}\n"
        "export function byVar(flag: boolean) {\n"  # 12
        "  if (flag) {\n"
        "    var LIMIT = 1;\n"  # hoisted: binds LIMIT throughout byVar
        "  }\n"
        "  return LIMIT;\n"
        "}\n"
        "export const byCallback = [1].map((LIMIT) => LIMIT);\n"  # 18
        "export function byCatch() {\n"  # 19
        "  try {\n"
        "    return 0;\n"
        "  } catch (LIMIT) {\n"
        "    return LIMIT;\n"
        "  }\n"
        "}\n"
    ),
}


def make_repo(root: Path, name: str, files: dict[str, str]) -> Path:
    repo = root / name
    for relative, content in files.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    init_git_repository(repo)
    return repo


def build(workspace: Workspace, tmp_path: Path, repos: dict[str, dict[str, str]]) -> Workspace:
    root = tmp_path / "repos"
    for name, files in repos.items():
        make_repo(root, name, files)
    initialize(workspace, root, config_path=tmp_path / "config.yaml")
    return workspace


def label(path: str | None, qualified: str | None, entity_type: str) -> str:
    """``path#qualified`` for a declaration, the bare path for a file."""
    return (path or "") if entity_type == "file" else f"{path}#{qualified}"


def use_edges(workspace: Workspace) -> list[dict[str, Any]]:
    """Every ``USES`` edge, with both ends labelled and its resolution decoded."""
    rows = workspace.database.execute(
        """
        SELECT s.type AS s_type, sf.rel_path AS s_path, s.qualified_name AS s_name,
               d.type AS d_type, df.rel_path AS d_path, d.qualified_name AS d_name,
               e.line_start AS line, e.resolution AS resolution, e.parser AS parser
        FROM edges e
        JOIN entities s ON s.id = e.src_id
        JOIN entities d ON d.id = e.dst_id
        LEFT JOIN files sf ON sf.id = s.file_id
        LEFT JOIN files df ON df.id = d.file_id
        WHERE e.type = 'USES'
        """
    ).fetchall()
    return [
        {
            "source": label(row["s_path"], row["s_name"], row["s_type"]),
            "target": label(row["d_path"], row["d_name"], row["d_type"]),
            "line": row["line"],
            "parser": row["parser"],
            **json.loads(row["resolution"] or "{}"),
        }
        for row in rows
    ]


def uses(workspace: Workspace) -> set[tuple[str, str, str]]:
    """(user, used, kind of use) for every ``USES`` edge."""
    return {(edge["source"], edge["target"], edge["use"]) for edge in use_edges(workspace)}
