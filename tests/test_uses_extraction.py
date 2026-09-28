"""Declaration-level uses (``USES``): what is extracted, how it resolves, what is refused.

The workspace tests index fixture A (:mod:`tests.impact_corpus`) and compare the whole
``USES`` graph with a set written out by hand, so a use that appears or disappears
fails. The parser tests pin each syntactic form and each refusal on its own.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repoatlas.graph.linker import GLOBAL_PARSER
from repoatlas.models.parsing import EdgeType
from repoatlas.parsers.treesitter import parse_tree
from repoatlas.parsers.ts_uses import USES_EXPORTS, UseAnalysis
from repoatlas.parsers.typescript import TypeScriptParser
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import SYMBOLS, build, use_edges, uses

LIMIT = "src/limits.ts#LIMIT"

#: Fixture A's complete USES graph: (user, used, kind of use).
EXPECTED = {
    # composition within one file
    ("src/limits.ts#TABLE", LIMIT, "value"),
    ("src/limits.ts#cap", "src/limits.ts#TABLE", "value"),
    ("src/limits.ts#defaultRule", LIMIT, "value"),
    # through the barrel: a renamed re-export, a default re-exported by name, a name
    # imported and exported again, and a wholesale re-export only one side defines
    ("src/check.ts#check", LIMIT, "value"),
    ("src/check.ts#check", "src/limits.ts#defaultRule", "call"),
    ("src/check.ts#check", "src/helper.ts#helperValue", "value"),
    ("src/check.ts#check", "src/stars-a.ts#onlyA", "value"),
    # a namespace member, and a namespace as a whole: the module it names
    ("src/spaces.ts#withMember", LIMIT, "value"),
    ("src/spaces.ts#whole", "src/limits.ts", "value"),
    ("src/area.ts#area", "src/types.ts#Shape", "type"),
    # the unrelated importer reaches only the name it imports
    ("src/other.ts#other", "src/limits.ts#unrelated", "value"),
    # the one use no shadowing covers
    ("src/shadow.ts#byBlock", LIMIT, "value"),
    # a test's anonymous callbacks leave the use to the file
    ("src/limits.test.ts", LIMIT, "value"),
}


@pytest.fixture
def symbols(workspace: Workspace, tmp_path: Path) -> Workspace:
    return build(workspace, tmp_path, {"symbols": SYMBOLS})


def one(workspace: Workspace, source: str, target: str) -> dict:
    found = [e for e in use_edges(workspace) if (e["source"], e["target"]) == (source, target)]
    assert len(found) == 1, found
    return found[0]


def test_fixture_a_uses_are_exactly_the_expected_set(symbols: Workspace) -> None:
    assert uses(symbols) == EXPECTED


def test_a_renamed_re_export_keeps_every_name_it_passes_by(symbols: Workspace) -> None:
    edge = one(symbols, "src/check.ts#check", LIMIT)
    assert edge["parser"] == GLOBAL_PARSER
    assert (edge["local"], edge["imported"], edge["name"]) == ("ceiling", "MAX_ITEMS", "LIMIT")
    assert (edge["via"], edge["via_names"]) == (["src/index.ts"], ["MAX_ITEMS"])
    assert (edge["defined_at"], edge["specifier"], edge["line"]) == ("src/limits.ts", "./index", 3)


def test_a_default_re_exported_by_name_resolves_to_its_declaration(symbols: Workspace) -> None:
    edge = one(symbols, "src/check.ts#check", "src/limits.ts#defaultRule")
    assert (edge["use"], edge["form"], edge["binding"]) == ("call", "call", "import")
    assert (edge["imported"], edge["name"], edge["via_names"]) == ("rule", "defaultRule", ["rule"])


def test_a_name_imported_and_exported_again_is_followed_through_the_import(
    symbols: Workspace,
) -> None:
    edge = one(symbols, "src/check.ts#check", "src/helper.ts#helperValue")
    assert (edge["via"], edge["via_names"]) == (["src/index.ts"], ["helperValue"])


def test_disagreeing_wholesale_re_exports_and_an_expression_default_stay_unresolved(
    symbols: Workspace,
) -> None:
    targets = {e["target"] for e in use_edges(symbols) if e["source"] == "src/check.ts#check"}
    assert not any(t.endswith("#shared") for t in targets)
    assert not [e for e in use_edges(symbols) if e["source"] == "src/spaces.ts#fromExpression"]


def test_a_namespace_as_a_whole_is_a_use_of_its_module(symbols: Workspace) -> None:
    member = one(symbols, "src/spaces.ts#withMember", LIMIT)
    assert (member["binding"], member["form"], member["imported"]) == (
        "namespace",
        "member",
        "LIMIT",
    )
    whole = one(symbols, "src/spaces.ts#whole", "src/limits.ts")
    assert (whole["namespace"], whole["form"]) == (True, "argument")
    assert "imported" not in whole and "name" not in whole


def test_a_type_only_import_is_a_type_use(symbols: Workspace) -> None:
    edge = one(symbols, "src/area.ts#area", "src/types.ts#Shape")
    assert (edge["use"], edge["type_only"]) == ("type", True)


def test_a_same_file_use_is_proven_by_the_file_alone(symbols: Workspace) -> None:
    edge = one(symbols, "src/limits.ts#cap", "src/limits.ts#TABLE")
    assert edge["parser"] == TypeScriptParser.name
    assert (edge["form"], edge["binding"], edge["occurrences"], edge["line"]) == (
        "receiver",
        "local",
        1,
        4,
    )


# -- the parser, form by form ------------------------------------------------------


def parsed_uses(source: str, name: str = "a.tsx") -> list[tuple[str | None, str, str, str]]:
    parsed = TypeScriptParser().parse(Path(name), source)
    return [
        (r.source_symbol, r.target_key, r.meta["use"], r.meta["form"])
        for r in parsed.references
        if r.edge_type is EdgeType.USES and USES_EXPORTS not in r.meta
    ]


def test_each_form_of_use_is_recorded_with_its_syntax() -> None:
    source = (
        'import D from "./d";\n'
        'import * as ns from "./n";\n'
        "export const K = 1;\n"
        "class Box {}\n"
        "function Widget() { return null; }\n"
        "function dec(t: unknown) { return t; }\n"
        "export function f(v = K): ns.T {\n"
        "  const o = { K };\n"
        "  g(K);\n"
        "  const s = `${K}`;\n"
        "  const r = [...D];\n"
        "  const x = <Widget />;\n"
        "  const b = new Box();\n"
        "  return typeof K === 'number' ? ns.v : D.w;\n"
        "}\n"
        "@dec class Decorated {}\n"
        "function g(n: unknown) { return n; }\n"
    )
    assert parsed_uses(source) == [
        ("f", "K", "value", "read"),
        ("f", "ns.T", "type", "type"),
        ("f", "D", "value", "spread"),
        ("f", "Widget", "value", "jsx"),
        ("f", "Box", "value", "new"),
        ("f", "ns.v", "value", "member"),
        ("Decorated", "dec", "value", "decorator"),
    ]


def test_forms_merge_into_the_first_occurrence_and_are_counted() -> None:
    parsed = TypeScriptParser().parse(
        Path("a.ts"), "const K = 1;\nexport function f() {\n  return K + K + [K];\n}\n"
    )
    [use] = [r for r in parsed.references if r.edge_type is EdgeType.USES and "use" in r.meta]
    assert (use.source_symbol, use.target_key, use.line_start, use.meta["occurrences"]) == (
        "f",
        "K",
        3,
        "3",
    )


def test_a_module_binding_owns_its_initializer_and_a_function_value_its_body() -> None:
    source = (
        "const BASE = 1;\n"
        "export const DERIVED = BASE * 2;\n"
        "export const make = () => DERIVED;\n"
        "export default { BASE };\n"
    )
    assert parsed_uses(source, "a.ts") == [
        ("DERIVED", "BASE", "value", "read"),
        ("make", "DERIVED", "value", "read"),
        (None, "BASE", "value", "shorthand"),
    ]


def test_type_level_uses_and_their_bindings() -> None:
    source = (
        "interface Shape { side: number }\n"
        "const unit = 1;\n"
        "export type Area<T extends Shape> = { [K in keyof T]: T[K] };\n"
        "export type Unwrap<X> = X extends Array<infer U> ? U : Shape;\n"
        "export type UnitOf = typeof unit;\n"
    )
    assert parsed_uses(source, "a.ts") == [
        ("Area", "Shape", "type", "type"),
        ("Unwrap", "Shape", "type", "type"),
        ("UnitOf", "unit", "type", "typeof"),
    ]


def test_a_local_callee_stays_a_call_and_an_imported_one_is_also_a_use() -> None:
    source = (
        'import { remote } from "./r";\n'
        "function near() { return 1; }\n"
        "export function f() { near(); remote(); }\n"
        "export const early = near();\n"
    )
    assert parsed_uses(source, "a.ts") == [
        ("f", "remote", "call", "call"),
        ("early", "near", "call", "call"),
    ]


def test_self_uses_and_member_uses_of_the_container_are_dropped() -> None:
    source = (
        "export function loop(n: number): number { return n ? loop(n - 1) : 0; }\n"
        "export class Node { next?: Node; static make() { return new Node(); } }\n"
    )
    assert parsed_uses(source, "a.ts") == []


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        (
            "interface M { a: 1 }\ninterface M { b: 2 }\nexport const u: M = { a: 1, b: 2 };\n",
            "duplicate",
        ),
        ("namespace N { export const n = 1; }\nexport const u = N.n;\n", "namespace"),
        ("const { a } = { a: 1 };\nexport const u = a;\n", "destructured"),
        ('import q = require("q");\nexport const u = q;\n', "require"),
    ],
)
def test_names_no_single_entity_carries_are_refused(source: str, reason: str) -> None:
    assert parsed_uses(source, "a.ts") == []
    tree, raw = parse_tree("typescript", source)
    assert UseAnalysis(raw, tree.root_node).rejected == {reason: 1}


def test_overload_signatures_are_one_function_the_implementation() -> None:
    source = (
        "export function pick(a: string): string;\n"
        "export function pick(a: number): number;\n"
        "export function pick(a: unknown) { return a; }\n"
        "export const chosen = pick;\n"
    )
    parsed = TypeScriptParser().parse(Path("a.ts"), source)
    [use] = [r for r in parsed.references if r.edge_type is EdgeType.USES and "use" in r.meta]
    assert (use.source_symbol, use.target_key) == ("chosen", "pick")
    [table] = [r for r in parsed.references if USES_EXPORTS in r.meta]
    assert '"pick":["local","pick","3"]' in table.meta[USES_EXPORTS]


def test_a_recovered_tree_states_no_uses_at_all() -> None:
    source = "const K = 1;\nexport function f() { return K + ; }\n"
    parsed = TypeScriptParser().parse(Path("a.ts"), source)
    assert not [r for r in parsed.references if r.edge_type is EdgeType.USES]


def test_intrinsic_elements_labels_and_properties_are_not_uses() -> None:
    source = (
        "const div = 1;\n"
        "const key = 2;\n"
        "export function f(o: { key: number }) {\n"
        "  outer: for (;;) { break outer; }\n"
        "  return [<div key={1} />, o.key, { key: 3 }];\n"
        "}\n"
    )
    assert parsed_uses(source) == []
