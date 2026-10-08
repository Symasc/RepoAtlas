"""A TypeScript enum's string-valued members are declarations, reached through their enum.

A generated enum shares its members' string values with the schema it was generated from,
so each member is a symbol carrying the value it stands for. What must not change: a use of
``Stage.Packed`` still lands on the enum (the use is of the enum's name), chunks are cut
where they were, and no bare name ever resolves to a member.
"""

from __future__ import annotations

import json
from pathlib import Path

from repoatlas.config.settings import IndexingSettings
from repoatlas.indexing.chunking import chunk_content
from repoatlas.models.parsing import ENUM_MEMBER
from repoatlas.parsers.typescript import TypeScriptParser
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build, use_edges

SOURCE = (
    "export enum Stage {\n"  # 1
    "  Packed = 'PACKED',\n"  # 2
    '  Sent = "SENT",\n'  # 3
    "  'Quoted' = 'QUOTED',\n"  # 4
    "  Bare,\n"  # 5
    "  Count = 2,\n"  # 6
    "  Escaped = 'a\\'b',\n"  # 7
    "  Template = `T`,\n"  # 8
    "}\n"  # 9
    "export function Packed(): number {\n"  # 10
    "  return 1;\n"  # 11
    "}\n"  # 12
)


def test_string_valued_members_are_symbols_under_their_enum() -> None:
    parsed = TypeScriptParser().parse(Path("stage.ts"), SOURCE)

    members = [s for s in parsed.symbols if s.meta.get("kind") == ENUM_MEMBER]
    assert [
        (s.qualified_name, s.name, s.parent, s.line_start, s.line_end, s.meta["value"])
        for s in members
    ] == [
        ("Stage.Packed", "Packed", "Stage", 2, 2, "PACKED"),
        ("Stage.Sent", "Sent", "Stage", 3, 3, "SENT"),
        ("Stage.Quoted", "Quoted", "Stage", 4, 4, "QUOTED"),
    ]
    assert members[0].signature == "Packed = 'PACKED'"


def test_members_cut_no_chunk() -> None:
    """Only top-level declarations bound chunks; a member sits inside its enum's."""
    parsed = TypeScriptParser().parse(Path("stage.ts"), SOURCE)
    without = tuple(s for s in parsed.symbols if s.meta.get("kind") != ENUM_MEMBER)
    settings = IndexingSettings()

    assert chunk_content(SOURCE, parsed.symbols, settings) == chunk_content(
        SOURCE, without, settings
    )


FILES = {
    "package.json": json.dumps({"name": "stages", "private": True}),
    "src/stage.ts": SOURCE,
    "src/use.ts": (
        'import { Stage, Packed } from "./stage";\n'  # 1
        "export function shipped(s: Stage): boolean {\n"  # 2
        "  return s === Stage.Packed && Packed() > 0;\n"  # 3
        "}\n"  # 4
    ),
    "src/local.ts": (
        "export enum Lane {\n"  # 1
        "  Fast = 'FAST',\n"  # 2
        "}\n"  # 3
        "export function pick(): number {\n"  # 4
        "  return Fast();\n"  # 5  names no member: a member is no bare name
        "}\n"
    ),
}


def _edges_into(workspace: Workspace, qualified: str) -> set[tuple[str, str]]:
    rows = workspace.database.execute(
        """
        SELECT e.type AS type, s.qualified_name AS source
        FROM edges e JOIN entities d ON d.id = e.dst_id JOIN entities s ON s.id = e.src_id
        WHERE d.qualified_name = ? AND e.type != 'DEFINES'
        """,
        (qualified,),
    ).fetchall()
    return {(r["type"], r["source"]) for r in rows}


def test_a_member_is_reached_through_its_enum_and_never_by_its_bare_name(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, {"stages": FILES})

    # The use of `Stage.Packed` is a use of `Stage`, as it was before members existed,
    # and the imported `Packed` is the function, never the member of the same name.
    uses = {(u["source"], u["target"]) for u in use_edges(workspace)}
    assert ("src/use.ts#shipped", "src/stage.ts#Stage") in uses
    assert ("src/use.ts#shipped", "src/stage.ts#Packed") in uses
    assert not [u for u in uses if u[1].endswith(("#Stage.Packed", "#Lane.Fast"))]
    # The imported function still resolves: the member of the same name is no rival.
    assert ("CALLS", "shipped") in _edges_into(workspace, "Packed")
    # No edge of any kind lands on a member by its name, here or across files.
    assert _edges_into(workspace, "Stage.Packed") == set()
    assert _edges_into(workspace, "Lane.Fast") == set()
