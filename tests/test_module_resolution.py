"""Workspace-aware module resolution: aliases, workspace packages and barrels.

:class:`~repoatlas.graph.modules.ModuleResolver` answers one question from indexed
facts: which file a specifier names. It serves declaration uses and package links
only. The import,
reference and call edges the global pass writes are resolved exactly as before, and
the last tests here pin that.

One pnpm monorepo carries every shape resolution has to get right, and three more
repositories check that a package name never resolves into another repository. Each
test asserts both what resolves and what must not.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from repoatlas.graph.linker import GLOBAL_PARSER
from repoatlas.graph.modules import ModuleResolver, ModuleTarget
from repoatlas.graph.resolution import ResolutionIndex
from repoatlas.services.init_service import initialize
from repoatlas.services.workspace import Workspace
from repoatlas.storage.protocols import FileRow
from tests.conftest import init_git_repository

SHAPES_INDEX = """export * from "./circle";
export * from "./square";
export { polygonArea as area, Polygon } from "./polygon";
export * as units from "./units";
export * from "./deep";
export * from "./loop-a";
export * from "./dup-a";
export * from "./dup-b";
"""

MONO = {
    "pnpm-workspace.yaml": 'packages:\n  - "apps/*"\n  - "packages/*"\n  - "!packages/ignored"\n',
    "package.json": '{"name": "mono", "private": true}\n',
    "tsconfig.base.json": '{\n  // shared options\n  "compilerOptions": {"strict": true,},\n}\n',
    # a package whose entry is build output, mapped back through its own tsconfig
    "packages/shapes/package.json": json.dumps(
        {"name": "@acme/shapes", "main": "./dist/index.js", "types": "./dist/index.d.ts"}
    ),
    "packages/shapes/tsconfig.json": json.dumps(
        {
            "extends": "../../tsconfig.base.json",
            "compilerOptions": {"outDir": "dist", "rootDir": "src"},
        }
    ),
    "packages/shapes/src/index.ts": SHAPES_INDEX,
    "packages/shapes/src/circle.ts": (
        "export const circleSchema = { kind: 'circle' };\n"
        "export function circleArea(r: number) {\n  return r * r;\n}\n"
    ),
    "packages/shapes/src/square.ts": (
        "export const squareSchema = { kind: 'square' };\nexport type Square = { side: number };\n"
    ),
    "packages/shapes/src/polygon.ts": (
        "export function polygonArea() {\n  return 0;\n}\nexport class Polygon {}\n"
    ),
    "packages/shapes/src/units.ts": "export const cm = 1;\n",
    "packages/shapes/src/deep/index.ts": 'export * from "./leaf";\n',
    "packages/shapes/src/deep/leaf.ts": "export const deepLeaf = 3;\n",
    "packages/shapes/src/loop-a.ts": 'export * from "./loop-b";\nexport const loopA = 1;\n',
    "packages/shapes/src/loop-b.ts": 'export * from "./loop-a";\nexport const loopB = 2;\n',
    "packages/shapes/src/dup-a.ts": "export const twin = 'a';\n",
    "packages/shapes/src/dup-b.ts": "export const twin = 'b';\n",
    # source and exports: conditions that must agree, and a wildcard subpath
    "packages/colors/package.json": json.dumps(
        {
            "name": "@acme/colors",
            "exports": {
                ".": {"types": "./dist/main.d.ts", "default": "./dist/main.js"},
                "./tokens/*": "./src/tokens/*.ts",
            },
        }
    ),
    "packages/colors/tsconfig.json": json.dumps(
        {"compilerOptions": {"outDir": "./dist", "rootDir": "./src"}}
    ),
    "packages/colors/src/main.ts": "export const red = '#f00';\n",
    "packages/colors/src/tokens/spacing.ts": "export const gap = 4;\n",
    # build output with no tsconfig saying where its source is: never guessed
    "packages/nomap/package.json": json.dumps({"name": "@acme/nomap", "main": "dist/index.js"}),
    "packages/nomap/src/index.ts": "export const value = 1;\n",
    # two entry fields that name different source files: ambiguous
    "packages/conflict/package.json": json.dumps(
        {"name": "@acme/conflict", "main": "./dist/a.js", "types": "./dist/b.d.ts"}
    ),
    "packages/conflict/tsconfig.json": json.dumps(
        {"compilerOptions": {"outDir": "dist", "rootDir": "src"}}
    ),
    "packages/conflict/src/a.ts": "export const z = 1;\n",
    "packages/conflict/src/b.ts": "export const z = 2;\n",
    # excluded from the workspace, and a name two members share
    "packages/ignored/package.json": json.dumps({"name": "@acme/ignored", "main": "index.ts"}),
    "packages/ignored/index.ts": "export const x = 1;\n",
    "packages/twin-one/package.json": json.dumps({"name": "@acme/twin", "main": "index.ts"}),
    "packages/twin-one/index.ts": "export const y = 1;\n",
    "packages/twin-two/package.json": json.dumps({"name": "@acme/twin", "main": "index.ts"}),
    "packages/twin-two/index.ts": "export const y = 2;\n",
    # an application: paths inherited through extends, baseUrl, and a nested project
    "apps/web/package.json": json.dumps({"name": "@acme/web"}),
    "apps/web/tsconfig.json": (
        '{"extends": "./tsconfig.paths.json", "compilerOptions": {"baseUrl": "."}}'
    ),
    "apps/web/tsconfig.paths.json": json.dumps(
        {
            "compilerOptions": {
                "paths": {
                    "@/*": ["./src/*"],
                    "~lib": ["./src/lib/index.ts"],
                    "@gen/*": ["./dist/gen/*"],
                }
            }
        }
    ),
    "apps/web/src/api/client.ts": "export function fetchShape() {\n  return 1;\n}\n",
    "apps/web/src/lib/index.ts": "export const libValue = 1;\n",
    "apps/web/src/util/helpers.ts": "export const helper = 1;\n",
    "apps/web/src/components/button.ts": "export const Button = 'button';\n",
    "apps/web/src/components/index.ts": 'export { Button } from "./button";\n',
    "apps/web/dist/gen/types.ts": "export const generated = 1;\n",
    "apps/web/src/app.ts": """import { circleSchema, area, type Square } from "@acme/shapes";
import * as shapes from "@acme/shapes";
import "@acme/shapes";
import { units } from "@acme/shapes";
import { deepLeaf } from "@acme/shapes";
import { loopB } from "@acme/shapes";
import { twin } from "@acme/shapes";
import { circleArea, circleArea as areaOf } from "@acme/shapes";
import { red } from "@acme/colors";
import { gap } from "@acme/colors/tokens/spacing";
import { fetchShape } from "@/api/client";
import { libValue } from "~lib";
import { helper } from "src/util/helpers";
import { Button } from "./components";
import { value } from "@acme/nomap";
import { x } from "@acme/ignored";
import { y } from "@acme/twin";
import { z } from "@acme/conflict";
import { generated } from "@gen/types";
import React from "react";

export function run(side: Square) {
  circleArea(1);
  areaOf(2);
  return [circleSchema, area, shapes, units, deepLeaf, loopB, twin, red, gap, fetchShape, libValue,
          helper, Button, value, x, y, z, generated, React, side];
}
""",
    "apps/web/src/legacy/tsconfig.json": json.dumps(
        {"compilerOptions": {"paths": {"@/*": ["./old/*"]}}}
    ),
    "apps/web/src/legacy/old/thing.ts": "export const thing = 1;\n",
    # a namespace whose local name happens to equal an exported name
    "apps/web/src/namespaced.ts": (
        'import * as squareSchema from "@acme/shapes";\nexport const s = squareSchema;\n'
    ),
    "apps/web/src/legacy/use.ts": 'import { thing } from "@/thing";\nexport const used = thing;\n',
}

#: A barrel re-exporting many modules, each name imported by exactly one consumer.
FANOUT = 30


def big_barrel() -> dict[str, str]:
    files = {
        "packages/big/package.json": json.dumps({"name": "@acme/big", "types": "src/index.ts"}),
        "packages/big/src/index.ts": "".join(f'export * from "./m{i}";\n' for i in range(FANOUT)),
    }
    for i in range(FANOUT):
        files[f"packages/big/src/m{i}.ts"] = f"export const name{i} = {i};\n"
        files[f"apps/web/src/consumers/c{i}.ts"] = (
            f'import {{ name{i} }} from "@acme/big";\nexport const use{i} = name{i};\n'
        )
    return files


def common_repo(export: str, consumer: bool) -> dict[str, str]:
    files = {
        "pnpm-workspace.yaml": "packages:\n  - packages/*\n  - apps/*\n",
        "package.json": '{"private": true}',
        "packages/common-lib/package.json": json.dumps(
            {"name": "common-lib", "main": "src/index.ts"}
        ),
        "packages/common-lib/src/index.ts": f"export const {export} = 1;\n",
    }
    if consumer:
        files["apps/use/package.json"] = json.dumps({"name": "use"})
        files["apps/use/src/use.ts"] = (
            'import { fromA, fromB } from "common-lib";\nexport const both = [fromA, fromB];\n'
        )
    return files


def make_repo(root: Path, name: str, files: dict[str, str]) -> Path:
    repo = root / name
    for relative, content in files.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    init_git_repository(repo)
    return repo


@pytest.fixture
def indexed(workspace: Workspace, tmp_path: Path) -> Workspace:
    root = tmp_path / "repos"
    make_repo(root, "mono", {**MONO, **big_barrel()})
    make_repo(root, "team-a", common_repo("fromA", consumer=True))
    make_repo(root, "team-b", common_repo("fromB", consumer=False))
    make_repo(
        root,
        "loner",
        {
            "package.json": json.dumps({"name": "loner", "dependencies": {"common-lib": "1.0.0"}}),
            "src/main.ts": 'import { fromA } from "common-lib";\nexport const a = fromA;\n',
        },
    )
    initialize(workspace, root, config_path=tmp_path / "config.yaml")
    return workspace


APP = "apps/web/src/app.ts"


def resolver(workspace: Workspace) -> tuple[ModuleResolver, dict[tuple[str, str], FileRow]]:
    """A resolver built exactly as the global pass builds it, over the indexed store."""
    knowledge = workspace.knowledge
    rows = knowledge.list_file_rows()
    index = ResolutionIndex.build(rows, knowledge.list_symbols())
    built = ModuleResolver(
        index,
        knowledge.list_unresolved_refs(),
    )
    return built, {(row.repo_slug, row.rel_path): row for row in rows}


def resolved(target: ModuleTarget | None) -> tuple[str, str, dict[str, Any]] | None:
    if target is None:
        return None
    return target.file.rel_path, target.basis, dict(target.detail)


def test_a_workspace_package_resolves_to_its_entry_mapped_from_build_output(
    indexed: Workspace,
) -> None:
    modules, files = resolver(indexed)
    assert resolved(modules.resolve(files["mono", APP], "@acme/shapes")) == (
        "packages/shapes/src/index.ts",
        "workspace-package",
        {
            "entry": "types,main",
            "manifest": "packages/shapes/package.json",
            "mapped": "outDir->rootDir",
            "package": "@acme/shapes",
        },
    )


def test_exports_conditions_and_a_wildcard_subpath(indexed: Workspace) -> None:
    modules, files = resolver(indexed)
    app = files["mono", APP]
    assert resolved(modules.resolve(app, "@acme/colors")) == (
        "packages/colors/src/main.ts",
        "workspace-package",
        {
            "entry": "exports.default,exports.types",
            "manifest": "packages/colors/package.json",
            "mapped": "outDir->rootDir",
            "package": "@acme/colors",
        },
    )
    target = modules.resolve(app, "@acme/colors/tokens/spacing")
    assert target is not None
    assert target.file.rel_path == "packages/colors/src/tokens/spacing.ts"


def test_tsconfig_paths_through_extends_an_exact_key_and_base_url(indexed: Workspace) -> None:
    modules, files = resolver(indexed)
    app = files["mono", APP]
    assert resolved(modules.resolve(app, "@/api/client")) == (
        "apps/web/src/api/client.ts",
        "tsconfig-paths",
        {"pattern": "@/*", "tsconfig": "apps/web/tsconfig.paths.json"},
    )
    assert resolved(modules.resolve(app, "~lib")) == (
        "apps/web/src/lib/index.ts",
        "tsconfig-paths",
        {"pattern": "~lib", "tsconfig": "apps/web/tsconfig.paths.json"},
    )
    assert resolved(modules.resolve(app, "src/util/helpers")) == (
        "apps/web/src/util/helpers.ts",
        "tsconfig-base-url",
        {"tsconfig": "apps/web/tsconfig.json"},
    )
    assert resolved(modules.resolve(app, "./components")) == (
        "apps/web/src/components/index.ts",
        "relative",
        {},
    )


def test_the_nearest_tsconfig_decides(indexed: Workspace) -> None:
    modules, files = resolver(indexed)
    target = modules.resolve(files["mono", "apps/web/src/legacy/use.ts"], "@/thing")
    assert target is not None
    assert target.file.rel_path == "apps/web/src/legacy/old/thing.ts"


@pytest.mark.parametrize(
    ("specifier", "why"),
    [
        ("@acme/nomap", "main is build output and no tsconfig says where its source is"),
        ("@acme/ignored", "the package is excluded from the workspace"),
        ("@acme/twin", "two workspace members share the name"),
        ("@acme/conflict", "main and types lead to different source files"),
        ("@gen/types", "the alias points into build output, which is not indexed"),
        ("react", "a third-party package"),
    ],
)
def test_what_cannot_be_resolved_exactly_stays_unresolved(
    indexed: Workspace, specifier: str, why: str
) -> None:
    modules, files = resolver(indexed)
    assert modules.resolve(files["mono", APP], specifier) is None, why


def test_a_package_name_never_resolves_into_another_repository(indexed: Workspace) -> None:
    modules, files = resolver(indexed)
    own = modules.resolve(files["team-a", "apps/use/src/use.ts"], "common-lib")
    assert own is not None
    assert (own.file.repo_slug, own.file.rel_path) == ("team-a", "packages/common-lib/src/index.ts")
    # the repository with no workspace resolves nothing, however the name is spelled
    assert modules.resolve(files["loner", "src/main.ts"], "common-lib") is None


def test_the_resolver_reads_indexed_facts_never_the_working_tree(
    indexed: Workspace, tmp_path: Path
) -> None:
    paths = tmp_path / "repos" / "mono" / "apps" / "web" / "tsconfig.paths.json"
    paths.write_text(json.dumps({"compilerOptions": {"paths": {"@/*": ["./nowhere/*"]}}}))
    modules, files = resolver(indexed)
    target = modules.resolve(files["mono", APP], "@/api/client")
    assert target is not None
    assert target.file.rel_path == "apps/web/src/api/client.ts"


# --- the import graph is resolved exactly as before ----------------------------


def global_edges(workspace: Workspace, path: str) -> set[tuple[Any, ...]]:
    rows = workspace.database.execute(
        """
        SELECT e.type, e.line_start, e.resolution, df.rel_path
        FROM edges e
        JOIN entities dst ON dst.id = e.dst_id
        JOIN files sf ON sf.id = e.file_id
        LEFT JOIN files df ON df.id = dst.file_id
        WHERE sf.rel_path = ? AND e.parser = ? AND e.type != 'USES'
        """,
        (path, GLOBAL_PARSER),
    ).fetchall()
    return {tuple(row) for row in rows}


def test_alias_workspace_and_barrel_imports_add_no_import_graph_edges(indexed: Workspace) -> None:
    """The resolver serves declaration uses and package links, and nothing else.

    Every import in ``app.ts`` above resolves through the resolver, yet the global
    pass links the file exactly as it did before the resolver existed: its one
    literal relative import, recorded with no resolution. Workspace, alias and barrel
    imports gain no import, reference or call edge, so search expansion and the
    relationships walk see the graph they always saw.
    """
    assert global_edges(indexed, APP) == {("IMPORTS", 14, None, "apps/web/src/components/index.ts")}
    for i in range(FANOUT):
        assert global_edges(indexed, f"apps/web/src/consumers/c{i}.ts") == set()
    assert global_edges(indexed, "apps/use/src/use.ts") == set()
    recorded = indexed.database.execute(
        "SELECT COUNT(*) FROM edges WHERE resolution IS NOT NULL AND type != 'USES'"
    ).fetchone()[0]
    assert recorded == 0


def _extends_cycle() -> tuple[ModuleResolver, dict[str, FileRow]]:
    """Two tsconfigs, in ``one/`` and ``two/``, extend each other; only ``one`` sets baseUrl."""
    from repoatlas.models.parsing import EdgeType, EntityType
    from repoatlas.parsers.module_facts import MODULE_RESOLUTION, TSCONFIG
    from repoatlas.storage.protocols import UnresolvedRefRecord

    paths = ["one/tsconfig.json", "two/tsconfig.json", "one/app.ts", "two/app.ts", "one/lib/x.ts"]
    rows = {
        path: FileRow(
            file_id=i + 1,
            file_entity_id=100 + i,
            repo_id=1,
            repo_name="r",
            repo_slug="r",
            repo_path=Path("/nowhere/r"),
            rel_path=path,
        )
        for i, path in enumerate(paths)
    }
    facts = {
        "one/tsconfig.json": {"extends": ["../two/tsconfig.json"], "baseUrl": "."},
        "two/tsconfig.json": {"extends": ["../one/tsconfig.json"]},
    }
    claims = [
        UnresolvedRefRecord(
            repo_id=1,
            file_id=rows[config].file_id,
            src_id=rows[config].file_entity_id,
            edge_type=EdgeType.CONFIGURES,
            target_kind=EntityType.CONFIG,
            target_key=f"module-resolution:{TSCONFIG}",
            meta_json=json.dumps({MODULE_RESOLUTION: TSCONFIG, "facts": json.dumps(value)}),
        )
        for config, value in facts.items()
    ]
    return ModuleResolver(ResolutionIndex.build(list(rows.values()), []), claims), rows


def test_an_extends_cycle_gives_each_config_its_own_chain_whatever_was_asked_first() -> None:
    """``two`` inherits ``one``'s baseUrl through the cycle, even after ``one`` was asked about.

    Asking about ``one`` first walks ``two`` with ``one`` already open, where it reads an
    empty parent; that cut-short answer must not become ``two``'s own.
    """
    expected = ("one/lib/x.ts", "tsconfig-base-url", {"tsconfig": "two/tsconfig.json"})

    modules, rows = _extends_cycle()
    assert resolved(modules.resolve(rows["two/app.ts"], "lib/x")) == expected

    modules, rows = _extends_cycle()
    assert resolved(modules.resolve(rows["one/app.ts"], "lib/x"))
    assert resolved(modules.resolve(rows["two/app.ts"], "lib/x")) == expected
