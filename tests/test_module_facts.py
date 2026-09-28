"""What parsing records for module resolution: configuration values and import bindings."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from repoatlas.parsers.module_facts import MODULE_RESOLUTION, module_facts, strip_jsonc
from repoatlas.parsers.structured import StructuredParser
from repoatlas.parsers.typescript import TypeScriptParser


def facts(name: str, content: str) -> dict | None:
    found = module_facts(name, content)
    assert len(found) <= 1
    return json.loads(found[0].meta["facts"]) if found else None


def test_jsonc_comments_and_trailing_commas_are_removed_but_strings_are_kept() -> None:
    text = """{
      // a line comment
      "$schema": "https://example.invalid//schema", /* a block */
      "odd": ",}",
      "list": [1, 2,],
    }"""
    assert json.loads(strip_jsonc(text)) == {
        "$schema": "https://example.invalid//schema",
        "odd": ",}",
        "list": [1, 2],
    }


def test_a_tsconfig_records_extends_include_and_the_options_resolution_reads() -> None:
    content = """{
      // inherited
      "extends": ["./base.json", "@acme/tsconfig"],
      "include": ["src"],
      "compilerOptions": {
        "baseUrl": ".", "rootDir": "src", "outDir": "dist", "strict": true,
        "paths": {"@/*": ["./src/*"], "bad": 3},
      },
    }"""
    assert facts("tsconfig.build.json", content) == {
        "baseUrl": ".",
        "extends": ["./base.json", "@acme/tsconfig"],
        "include": ["src"],
        "outDir": "dist",
        "paths": {"@/*": ["./src/*"]},
        "rootDir": "src",
    }


def test_a_tsconfig_include_that_is_not_all_paths_is_not_recorded() -> None:
    assert facts("tsconfig.json", '{"include": ["src", 7], "compilerOptions": {}}') is None


def test_a_package_manifest_records_its_name_entries_and_workspaces() -> None:
    manifest = {
        "name": "@acme/x",
        "main": "./dist/index.js",
        "types": "./dist/index.d.ts",
        "exports": {".": {"types": "./dist/index.d.ts", "default": "./dist/index.js"}, "./x": 3},
        "workspaces": {"packages": ["packages/*"]},
        "version": "1.0.0",
    }
    assert facts("package.json", json.dumps(manifest)) == {
        "exports": {".": {"default": "./dist/index.js", "types": "./dist/index.d.ts"}},
        "main": "./dist/index.js",
        "name": "@acme/x",
        "types": "./dist/index.d.ts",
        "workspaces": ["packages/*"],
    }


def test_a_pnpm_workspace_records_its_package_globs() -> None:
    assert facts("pnpm-workspace.yaml", 'packages:\n  - "apps/*"\n  - "!apps/old"\n') == {
        "packages": ["apps/*", "!apps/old"]
    }


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("settings.json", '{"paths": {"@/*": ["x"]}}'),
        ("package.json", "not json"),
        ("package.json", '{"version": "1.0.0"}'),
        ("tsconfig.json", json.dumps({"compilerOptions": {"x": "y" * 300_000}})),
        ("pnpm-workspace.yaml", "- a list\n"),
    ],
)
def test_anything_else_records_nothing(name: str, content: str) -> None:
    assert module_facts(name, content) == []


def test_the_structured_parser_keeps_the_fact_even_when_yaml_cannot_read_the_file() -> None:
    parsed = StructuredParser().parse(
        Path("tsconfig.json"), '{\n  "compilerOptions": {"paths": {"@/*": ["./src/*"]}}, // c\n}\n'
    )
    kinds = [r.meta.get(MODULE_RESOLUTION) for r in parsed.references]
    assert kinds == ["tsconfig"]


SOURCE = """import D, { A, B as C, type T } from "./m";
import type { U } from "./u";
import * as NS from "./ns";
import "./side";
export * from "./star";
export * as SN from "./sn";
export { X, Y as Z } from "./y";
export type { W } from "./w";
"""


def test_imports_and_re_exports_record_how_each_name_is_bound() -> None:
    parsed = TypeScriptParser().parse(Path("a.ts"), SOURCE)
    found = {
        r.target_key: {k: v for k, v in r.meta.items() if k != "specifier"}
        for r in parsed.references
        if str(r.edge_type) == "IMPORTS"
    }
    assert found == {
        "./m": {
            "names": "D,A,B as C,type T",
            "default": "D",
            "bindings": "A,B:C,T",
            "type_only": "T",
        },
        "./u": {"names": "U", "bindings": "U", "type_only": "*"},
        "./ns": {"names": "NS", "namespace": "NS"},
        "./side": {},
        "./star": {"role": "re-export", "reexport": "*"},
        "./sn": {"role": "re-export", "reexport": "namespace", "namespace": "SN"},
        "./y": {"role": "re-export", "reexport": "named", "bindings": "X,Y:Z"},
        "./w": {"role": "re-export", "reexport": "named", "bindings": "W", "type_only": "*"},
    }
