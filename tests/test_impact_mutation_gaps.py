"""Guarantees the change-impact mutation run found unpinned (topology/impact/mutate.py).

Each test here is the focused kill for one mutation that the milestone's other suites let
survive. The code already held each guarantee; nothing pinned it.
"""

from __future__ import annotations

import json
from pathlib import Path

from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.impact import ImpactRequest
from repoatlas.parsers.ts_uses import USES_EXPORTS
from repoatlas.parsers.typescript import TypeScriptParser
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)


def export_table(source: str) -> dict:
    parsed = TypeScriptParser().parse(Path("a.ts"), source)
    [table] = [r for r in parsed.references if USES_EXPORTS in r.meta]
    return json.loads(table.meta[USES_EXPORTS])


def test_m15_one_name_exported_twice_bound_differently_is_ambiguous() -> None:
    """Resolving it to either binding would name a declaration the module never settled on."""
    table = export_table(
        "const a = 1;\nconst b = 2;\nexport { a as x };\nexport { b as x };\nexport { a as y };\n"
    )
    assert table["names"]["x"] == ["ambiguous"]
    assert table["names"]["y"] == ["local", "a", "1"]


def test_m23_a_member_s_key_extends_its_parent_s_with_a_dot(
    workspace: Workspace, tmp_path: Path
) -> None:
    """``StoreFactory`` shares a prefix with ``Store`` and is no member of it."""
    files = {
        "package.json": json.dumps({"name": "prefix", "private": True}),
        "src/s.ts": (
            "export class Store {\n  get() {\n    return 1;\n  }\n}\n"
            "export function StoreFactory() {\n  return 2;\n}\n"
        ),
        "src/u.ts": 'import { StoreFactory } from "./s";\nexport const made = StoreFactory();\n',
    }
    build(workspace, tmp_path, {"prefix": files})
    response = CapabilityService(workspace).impact(
        ImpactRequest(context=GLOBAL, seed="sym:prefix:src/s.ts:Store:1")
    )
    assert response.seed.members == 1
    assert [c.entity for c in response.candidates] == []
    # the sibling's own users are its own
    sibling = CapabilityService(workspace).impact(
        ImpactRequest(context=GLOBAL, seed="sym:prefix:src/s.ts:StoreFactory:6")
    )
    assert [c.entity for c in sibling.candidates] == ["sym:prefix:src/u.ts:made:2"]
