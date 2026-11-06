"""Where a TypeScript file says its OpenAPI document is (M7, C1).

Each source ends in ``use(<expression>)``: the expression is what the reader is asked
about, and the call's position is the place that reads it. A path is a ``Spec`` only
when the source states it from an anchor; anything else is a ``Refused`` with the code
that says why, never a guess.
"""

from __future__ import annotations

import pytest
from repoatlas.parsers.http_ts_wrappers import FetchWrappers, Imported, arguments_of
from repoatlas.parsers.http_typescript import binding_counts
from repoatlas.parsers.treesitter import node_text, parse_tree, walk
from repoatlas.parsers.ts_spec_paths import (
    SPEC_ABSOLUTE,
    SPEC_AMBIGUOUS_BINDING,
    SPEC_CWD_RELATIVE,
    SPEC_DEPTH_EXCEEDED,
    SPEC_DYNAMIC,
    SPEC_ENVIRONMENT,
    SPEC_IMPORT_UNRESOLVED,
    SPEC_IMPORT_UNSUPPORTED,
    SPEC_MUTABLE_BINDING,
    SPEC_NOT_A_FILE,
    SPEC_NOT_INDEXED,
    SPEC_OUTSIDE_REPOSITORY,
    SPEC_PARAMETER_UNCALLED,
    SPEC_PARAMETER_UNSUPPORTED,
    SPEC_PROPAGATION_DEPTH,
    SPEC_SHADOWED_ANCHOR,
    SPEC_UNRESOLVED_NAME,
    SPEC_USED_BEFORE_BINDING,
    ImportedPath,
    Param,
    ParamSegment,
    Parts,
    Refused,
    Result,
    Spec,
    SpecPathReader,
)

PATH = "import path from 'node:path';"
URL = "import { fileURLToPath } from 'node:url';"
DIRS = "import { API_DIR } from './dirs';"
BACKSLASH = "\\"


def _reading(*lines: str) -> tuple[Result, bytes]:
    """What the reader says of the argument of ``use(…)`` in ``lines``, and the source bytes
    that the nodes of the answer are read from."""
    source = "\n".join(lines)
    tree, raw = parse_tree("typescript", source)
    root = tree.root_node
    counts = binding_counts(raw, root)
    reader = SpecPathReader(raw, root, counts, FetchWrappers(raw, root, counts))
    for node in walk(root):
        callee = node.child_by_field_name("function") if node.type == "call_expression" else None
        if callee is not None and node_text(raw, callee) == "use":
            arguments = arguments_of(node)
            assert arguments, "use(…) takes one argument"
            return reader.resolve(arguments[0]), raw
    raise AssertionError("the source has no use(…)")


def _read(*lines: str) -> Result:
    """What the reader says of the argument of ``use(…)`` in ``lines``."""
    return _reading(*lines)[0]


# -- what a source states ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lines", "text"),
    [
        pytest.param(
            [
                PATH,
                "const NAME = 'openapi.yaml';",
                "const FILE = NAME;",
                "const SPEC = path.join(__dirname, FILE);",
                "use(SPEC);",
            ],
            "openapi.yaml",
            id="P1-alias-chain",
        ),
        pytest.param(
            [
                "const NAME = 'openapi.yaml';",
                "const SPEC = `${__dirname}/../api/${NAME}`;",
                "use(SPEC);",
            ],
            "../api/openapi.yaml",
            id="P2-template",
        ),
        pytest.param(
            ["use(__dirname + '/../api/openapi.yaml');"],
            "../api/openapi.yaml",
            id="P3-concatenation",
        ),
        pytest.param(
            [
                "const NAME = 'openapi.yaml';",
                "use(__dirname + '/api/' + NAME);",
            ],
            "api/openapi.yaml",
            id="P3-left-nested-concatenation",
        ),
        pytest.param(
            [
                PATH,
                "const ROOT = path.resolve(__dirname, '..');",
                "const SPEC = path.join(ROOT, 'api', 'openapi.yaml');",
                "use(SPEC);",
            ],
            "../api/openapi.yaml",
            id="P4-nested-anchor",
        ),
        pytest.param(
            [
                PATH,
                URL,
                "const __dirname = path.dirname(fileURLToPath(import.meta.url));",
                "use(path.join(__dirname, 'api/openapi.yaml'));",
            ],
            "api/openapi.yaml",
            id="P5-esm-dirname",
        ),
        pytest.param(
            [
                PATH,
                URL,
                "const HERE = path.dirname(fileURLToPath(import.meta.url));",
                "use(path.join(HERE, 'api/openapi.yaml'));",
            ],
            "api/openapi.yaml",
            id="P5-any-name",
        ),
        pytest.param(
            [
                "import * as url from 'url';",
                "import { dirname as up, join as under } from 'path';",
                "const HERE = up(url.fileURLToPath(import.meta.url));",
                "use(under(HERE, 'api/openapi.yaml'));",
            ],
            "api/openapi.yaml",
            id="P5-namespace-and-aliases",
        ),
        pytest.param(
            [
                "const { join, resolve } = require('path');",
                "use(resolve(__dirname, 'api', 'openapi.yaml'));",
            ],
            "api/openapi.yaml",
            id="P6-destructured-require",
        ),
        pytest.param(
            [
                "const path = require('node:path');",
                "use(path.join(__dirname, 'api/openapi.yaml'));",
            ],
            "api/openapi.yaml",
            id="P6-require-binding",
        ),
        pytest.param(
            [
                PATH,
                "const CONFIG = { spec: 'openapi.yaml', other: 1 };",
                "use(path.join(__dirname, CONFIG.spec));",
            ],
            "openapi.yaml",
            id="P7-read-only-property",
        ),
        pytest.param(
            [
                PATH,
                "const API = 'api';",
                "use(path.join(__dirname, API, 'a.yaml'));",
            ],
            "api/a.yaml",
            id="P8-literal-const-segment",
        ),
        pytest.param(
            [PATH, "use(path.join(__dirname, '..', 'api/a.yaml'));"],
            "../api/a.yaml",
            id="anchored-join-is-not-normalized",
        ),
        pytest.param(
            [PATH, "use(path.join(path.dirname(__filename), 'a.yaml'));"],
            "a.yaml",
            id="P13-dirname-of-filename",
        ),
        pytest.param(
            [
                PATH,
                URL,
                "const __filename = fileURLToPath(import.meta.url);",
                "use(path.join(path.dirname(__filename), 'a.yaml'));",
            ],
            "a.yaml",
            id="P13-filename-bound-once",
        ),
        pytest.param(
            [
                URL,
                "use(fileURLToPath(new URL('../api/openapi.yaml', import.meta.url)));",
            ],
            "../api/openapi.yaml",
            id="P14-url-relative-to-this-file",
        ),
        pytest.param(
            [
                URL,
                "const SPEC = fileURLToPath(new URL('a.yaml', import.meta.url));",
                "use(SPEC);",
            ],
            "a.yaml",
            id="P14-const",
        ),
        pytest.param(
            [
                PATH,
                "const A0 = 'a.yaml';",
                "const A1 = A0;",
                "const A2 = A1;",
                "const A3 = A2;",
                "use(path.join(__dirname, A3));",
            ],
            "a.yaml",
            id="four-names-are-followed",
        ),
    ],
)
def test_a_path_stated_from_an_anchor_is_read(lines: list[str], text: str) -> None:
    assert _read(*lines) == Spec(text)


# -- what a source leaves to another module or to a caller --------------------------------------


@pytest.mark.parametrize(
    ("lines", "parts"),
    [
        pytest.param(
            [PATH, DIRS, "use(path.join(__dirname, API_DIR, 'a.yaml'));"],
            (Imported("./dirs", "API_DIR"), "a.yaml"),
            id="imported-segment",
        ),
        pytest.param(
            [PATH, DIRS, "use(path.join(__dirname, 'api', API_DIR));"],
            ("api", Imported("./dirs", "API_DIR")),
            id="imported-last-segment",
        ),
        pytest.param(
            [
                PATH,
                "import { API_DIR as DIR } from '../dirs';",
                "const ROOT = path.resolve(__dirname, '..');",
                "use(path.join(ROOT, 'api', DIR, 'v1', 'a.yaml'));",
            ],
            ("..", "api", Imported("../dirs", "API_DIR"), "v1", "a.yaml"),
            id="own-literals-around-an-aliased-import",
        ),
        pytest.param(
            [PATH, DIRS, "const DIR = API_DIR;", "use(path.join(__dirname, DIR, 'a.yaml'));"],
            (Imported("./dirs", "API_DIR"), "a.yaml"),
            id="imported-segment-through-a-const",
        ),
        pytest.param(
            [
                PATH,
                DIRS,
                "const SPEC = path.join(__dirname, 'api', API_DIR);",
                "use(path.join(SPEC, 'a.yaml'));",
            ],
            ("api", Imported("./dirs", "API_DIR"), "a.yaml"),
            id="imported-segment-in-a-nested-anchor",
        ),
    ],
)
def test_a_segment_read_in_another_module_gives_parts_in_order(
    lines: list[str], parts: tuple[str | Imported, ...]
) -> None:
    assert _read(*lines) == Parts(parts)


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        pytest.param(
            ["import { SPEC_PATH } from './config';", "use(SPEC_PATH);"],
            ImportedPath("./config", "SPEC_PATH"),
            id="P9-imported-path",
        ),
        pytest.param(
            ["import { SPEC_PATH as SPEC } from '../config';", "use(SPEC);"],
            ImportedPath("../config", "SPEC_PATH"),
            id="P9-aliased-import",
        ),
        pytest.param(
            ["import { SPEC_PATH } from './config';", "const SPEC = SPEC_PATH;", "use(SPEC);"],
            ImportedPath("./config", "SPEC_PATH"),
            id="P9-through-a-const",
        ),
    ],
)
def test_a_relative_import_used_as_the_whole_path_gives_imported_path(
    lines: list[str], expected: ImportedPath
) -> None:
    assert _read(*lines) == expected


@pytest.mark.parametrize(
    ("lines", "index", "kind", "start"),
    [
        pytest.param(
            ["function load(spec: string) {", "  use(spec);", "}"],
            0,
            "function_declaration",
            "function load",
            id="direct",
        ),
        pytest.param(
            ["function load(root: string, spec: string) {", "  use(spec);", "}"],
            1,
            "function_declaration",
            "function load",
            id="second-parameter",
        ),
        pytest.param(
            ["function load(spec: string, other = 'b') {", "  use(spec);", "}"],
            0,
            "function_declaration",
            "function load",
            id="a-default-on-another-parameter",
        ),
        pytest.param(
            ["function load(spec: string) {", "  const target = spec;", "  use(target);", "}"],
            0,
            "function_declaration",
            "function load",
            id="through-a-local-const",
        ),
        pytest.param(
            [
                "function load(spec: string) {",
                "  const first = spec;",
                "  const second = first;",
                "  use(second);",
                "}",
            ],
            0,
            "function_declaration",
            "function load",
            id="through-two-local-consts",
        ),
        pytest.param(
            ["const load = (spec: string) => {", "  use(spec);", "};"],
            0,
            "arrow_function",
            "(spec: string)",
            id="arrow-bound-to-a-module-const",
        ),
        pytest.param(
            ["const load = spec => {", "  use(spec);", "};"],
            0,
            "arrow_function",
            "spec =>",
            id="arrow-with-one-bare-parameter",
        ),
        pytest.param(
            ["export function load(spec: string) {", "  use(spec);", "}"],
            0,
            "function_declaration",
            "function load",
            id="exported-function-declaration",
        ),
        pytest.param(
            ["export const f = (spec: string) => {", "  use(spec);", "};"],
            0,
            "arrow_function",
            "(spec: string)",
            id="arrow-bound-to-an-exported-const",
        ),
    ],
)
def test_a_plain_parameter_used_as_the_whole_path_gives_param(
    lines: list[str], index: int, kind: str, start: str
) -> None:
    result, raw = _reading(*lines)
    assert isinstance(result, Param)
    assert result.index == index
    assert result.function.type == kind
    assert result.function.start_byte == raw.index(start.encode())


def test_param_carries_the_function_so_that_its_name_can_be_read() -> None:
    result, raw = _reading("function load(spec: string) {", "  use(spec);", "}")
    assert isinstance(result, Param)
    name = result.function.child_by_field_name("name")
    assert name is not None
    assert node_text(raw, name) == "load"


@pytest.mark.parametrize(
    ("lines", "parts", "index"),
    [
        pytest.param(
            [PATH, "function load(spec: string) {", "  use(path.join(__dirname, spec));", "}"],
            (),
            0,
            id="join-anchor-then-parameter",
        ),
        pytest.param(
            [
                PATH,
                "export function load(root: string, spec: string) {",
                "  use(path.resolve(__dirname, '..', 'api', spec));",
                "}",
            ],
            ("..", "api"),
            1,
            id="resolve-literals-then-the-second-parameter",
        ),
        pytest.param(
            [
                PATH,
                "const API = 'api';",
                "const load = (spec: string) => use(path.join(__dirname, API, spec) as string);",
            ],
            ("api",),
            0,
            id="arrow-const-segment-and-a-cast",
        ),
    ],
)
def test_a_plain_parameter_as_the_last_join_segment_gives_param_segment(
    lines: list[str], parts: tuple[str, ...], index: int
) -> None:
    result = _read(*lines)
    assert isinstance(result, ParamSegment)
    assert (result.parts, result.index) == (parts, index)


# -- what a source does not state ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("lines", "code"),
    [
        # N1: nothing anchors a bare path; the process opens it from its working directory
        pytest.param(["use('api/a.yaml');"], SPEC_CWD_RELATIVE, id="N1-bare-literal"),
        pytest.param(
            ["const SPEC = 'api/a.yaml';", "use(SPEC);"], SPEC_CWD_RELATIVE, id="N1-const-literal"
        ),
        pytest.param(["use(process.cwd());"], SPEC_CWD_RELATIVE, id="N2-cwd"),
        pytest.param(
            [PATH, "use(path.join(process.cwd(), 'a.yaml'));"], SPEC_CWD_RELATIVE, id="N2-cwd-join"
        ),
        pytest.param(
            [PATH, "use(path.resolve('api/a.yaml'));"], SPEC_CWD_RELATIVE, id="N3-resolve-bare"
        ),
        pytest.param(
            [PATH, "use(path.join('api', 'a.yaml'));"], SPEC_CWD_RELATIVE, id="N3-join-bare"
        ),
        # N4: a deployment setting is whatever the deployment says; its default is not read
        pytest.param(
            [PATH, "use(path.join(__dirname, process.env.SPEC ?? 'a.yaml'));"],
            SPEC_ENVIRONMENT,
            id="N4-segment-nullish",
        ),
        pytest.param(
            [PATH, "use(process.env.SPEC || path.join(__dirname, 'a.yaml'));"],
            SPEC_ENVIRONMENT,
            id="N4-default-is-an-anchored-path",
        ),
        pytest.param(
            [
                PATH,
                "const SPEC = process.env['SPEC'] ?? 'a.yaml';",
                "use(path.join(__dirname, SPEC));",
            ],
            SPEC_ENVIRONMENT,
            id="N4-through-a-const",
        ),
        # N5, N6: a binding that is not one fixed value
        pytest.param(
            [PATH, "let SPEC = path.join(__dirname, 'a.yaml');", "use(SPEC);"],
            SPEC_MUTABLE_BINDING,
            id="N5-let",
        ),
        pytest.param(
            [PATH, "var SPEC = path.join(__dirname, 'a.yaml');", "use(SPEC);"],
            SPEC_MUTABLE_BINDING,
            id="N5-var",
        ),
        pytest.param(
            [
                PATH,
                "var SPEC = path.join(__dirname, 'a.yaml');",
                "var SPEC = 'b.yaml';",
                "use(SPEC);",
            ],
            SPEC_AMBIGUOUS_BINDING,
            id="N6-bound-twice",
        ),
        # N7: the nearest binding is the one read
        pytest.param(
            [
                PATH,
                "const SPEC = path.join(__dirname, 'api/a.yaml');",
                "function load() {",
                "  const SPEC = 'api/a.yaml';",
                "  use(SPEC);",
                "}",
            ],
            SPEC_CWD_RELATIVE,
            id="N7-inner-const-shadows-the-anchored-one",
        ),
        # N8: a name read before its const has run is not its value
        pytest.param(
            [PATH, "use(SPEC);", "const SPEC = path.join(__dirname, 'a.yaml');"],
            SPEC_USED_BEFORE_BINDING,
            id="N8-declared-after-the-use",
        ),
        pytest.param(
            [
                PATH,
                "const SPEC = path.join(__dirname, FILE);",
                "const FILE = 'a.yaml';",
                "use(SPEC);",
            ],
            SPEC_USED_BEFORE_BINDING,
            id="N8-declared-after-the-const-that-reads-it",
        ),
        # N9: __dirname is the directory of the file only when nothing else binds it
        pytest.param(
            [
                PATH,
                "function load(__dirname: string) {",
                "  use(path.join(__dirname, 'a.yaml'));",
                "}",
            ],
            SPEC_SHADOWED_ANCHOR,
            id="N9-parameter",
        ),
        pytest.param(
            [PATH, "const __dirname = '/srv/app';", "use(path.join(__dirname, 'a.yaml'));"],
            SPEC_SHADOWED_ANCHOR,
            id="N9-bound-to-a-literal",
        ),
        pytest.param(
            [PATH, "const __dirname = process.cwd();", "use(path.join(__dirname, 'a.yaml'));"],
            SPEC_SHADOWED_ANCHOR,
            id="N9-bound-to-cwd",
        ),
        pytest.param(
            [PATH, "let __dirname = '.';", "use(path.join(__dirname, 'a.yaml'));"],
            SPEC_SHADOWED_ANCHOR,
            id="N9-let",
        ),
        # N10: a segment that starts somewhere else
        pytest.param(
            [PATH, "use(path.join(__dirname, '/etc/a.yaml'));"], SPEC_ABSOLUTE, id="N10-rooted"
        ),
        pytest.param(
            [PATH, f"use(path.join(__dirname, 'api{BACKSLASH}a.yaml'));"],
            SPEC_ABSOLUTE,
            id="N10-backslash",
        ),
        pytest.param(
            [PATH, "use(path.join(__dirname, 'C:/a.yaml'));"], SPEC_ABSOLUTE, id="N10-drive-letter"
        ),
        pytest.param(
            [PATH, "const SEG = '/etc';", "use(path.join(__dirname, SEG, 'a.yaml'));"],
            SPEC_ABSOLUTE,
            id="N10-rooted-const",
        ),
        # N13: another package's value is for a later commit to read
        pytest.param(
            [PATH, "import { SPEC } from 'config';", "use(path.join(__dirname, SPEC));"],
            SPEC_IMPORT_UNSUPPORTED,
            id="N13-named-import",
        ),
        pytest.param(
            [PATH, "import SPEC from 'config';", "use(path.join(__dirname, SPEC));"],
            SPEC_IMPORT_UNSUPPORTED,
            id="N13-default-import",
        ),
        # N18: any call that is not one of the forms
        pytest.param(
            [
                PATH,
                "import { readFileSync } from 'fs';",
                "use(readFileSync(path.join(__dirname, 'a.yaml'), 'utf8'));",
            ],
            SPEC_DYNAMIC,
            id="N18-readFileSync",
        ),
        pytest.param(
            [PATH, "use(YAML.load(path.join(__dirname, 'a.yaml')));"],
            SPEC_DYNAMIC,
            id="N18-YAML-load",
        ),
        pytest.param(
            [PATH, "use(path.join(__dirname, makeName()));"], SPEC_DYNAMIC, id="N18-call-segment"
        ),
        # N20: an object a statement writes to is not the object its literal states
        pytest.param(
            [
                PATH,
                "const CONFIG = { spec: 'a.yaml' };",
                "CONFIG.spec = 'b.yaml';",
                "use(path.join(__dirname, CONFIG.spec));",
            ],
            SPEC_MUTABLE_BINDING,
            id="N20-property-written",
        ),
        # a path that names a directory, not a document
        pytest.param([PATH, "use(path.join(__dirname));"], SPEC_NOT_A_FILE, id="not-a-file-bare"),
        pytest.param([PATH, "use(__dirname);"], SPEC_NOT_A_FILE, id="not-a-file-anchor"),
        pytest.param(
            [PATH, "use(path.join(__dirname, 'sub', '..'));"],
            SPEC_NOT_A_FILE,
            id="not-a-file-normalizes-to-the-anchor",
        ),
        pytest.param(
            [PATH, "use(path.resolve(__dirname, '..'));"], SPEC_NOT_A_FILE, id="not-a-file-parent"
        ),
        pytest.param([PATH, "use(`${__dirname}/`);"], SPEC_NOT_A_FILE, id="not-a-file-template"),
        # a name nothing in the file binds, and a parameter this commit does not read
        pytest.param(
            [PATH, "use(path.join(__dirname, MISSING));"],
            SPEC_UNRESOLVED_NAME,
            id="unresolved-name",
        ),
        pytest.param(
            [
                PATH,
                "function load(spec: string) {",
                "  use(path.join(__dirname, spec, 'a.yaml'));",
                "}",
            ],
            SPEC_PARAMETER_UNSUPPORTED,
            id="parameter-segment-not-last",
        ),
        pytest.param(
            [PATH, "function load(spec = 'a.yaml') {", "  use(path.join(__dirname, spec));", "}"],
            SPEC_PARAMETER_UNSUPPORTED,
            id="parameter-last-segment-with-a-default",
        ),
        pytest.param(
            [PATH, "function load(spec?: string) {", "  use(path.join(__dirname, spec));", "}"],
            SPEC_PARAMETER_UNSUPPORTED,
            id="parameter-last-segment-optional",
        ),
        pytest.param(
            [
                PATH,
                "function outer() {",
                "  function load(spec: string) {",
                "    use(path.join(__dirname, spec));",
                "  }",
                "}",
            ],
            SPEC_PARAMETER_UNSUPPORTED,
            id="parameter-last-segment-of-a-nested-function",
        ),
        pytest.param(
            [PATH, "function load(spec: string) {", "  use(path.join(spec, 'a.yaml'));", "}"],
            SPEC_PARAMETER_UNSUPPORTED,
            id="parameter-as-the-anchor",
        ),
        pytest.param(
            [PATH, "function load(spec: string) {", "  use(path.join('api', spec));", "}"],
            SPEC_CWD_RELATIVE,
            id="parameter-last-segment-unanchored",
        ),
        pytest.param(
            [
                PATH,
                DIRS,
                "function load(spec: string) {",
                "  use(path.join(__dirname, API_DIR, spec));",
                "}",
            ],
            SPEC_IMPORT_UNSUPPORTED,
            id="parameter-last-segment-after-an-imported-one",
        ),
        pytest.param(
            [
                PATH,
                "function load(root: string, spec: string) {",
                "  use(path.join(__dirname, root, spec));",
                "}",
            ],
            SPEC_PARAMETER_UNSUPPORTED,
            id="two-parameter-segments",
        ),
        pytest.param(
            [
                PATH,
                "function load(spec: string) {",
                "  use(`${__dirname}/${spec}`);",
                "}",
            ],
            SPEC_PARAMETER_UNSUPPORTED,
            id="parameter-template-segment",
        ),
        pytest.param(
            [
                PATH,
                "function load(spec: string) {",
                "  const target = spec;",
                "  use(path.join(__dirname, target));",
                "}",
            ],
            SPEC_PARAMETER_UNSUPPORTED,
            id="parameter-alias-segment",
        ),
        pytest.param(
            ["function load(spec = 'a.yaml') {", "  use(spec);", "}"],
            SPEC_PARAMETER_UNSUPPORTED,
            id="parameter-with-a-default",
        ),
        pytest.param(
            ["function load(spec?: string) {", "  use(spec);", "}"],
            SPEC_PARAMETER_UNSUPPORTED,
            id="parameter-optional",
        ),
        pytest.param(
            ["function load({ spec }: { spec: string }) {", "  use(spec);", "}"],
            SPEC_PARAMETER_UNSUPPORTED,
            id="parameter-destructured",
        ),
        pytest.param(
            ["function load(...specs: string[]) {", "  use(specs);", "}"],
            SPEC_PARAMETER_UNSUPPORTED,
            id="parameter-rest",
        ),
        pytest.param(
            [
                "function outer() {",
                "  function load(spec: string) {",
                "    use(spec);",
                "  }",
                "}",
            ],
            SPEC_PARAMETER_UNSUPPORTED,
            id="parameter-of-a-nested-function",
        ),
        # a method or a callback has no name for a caller to call
        pytest.param(
            ["class Loader {", "  load(spec: string) {", "    use(spec);", "  }", "}"],
            SPEC_PARAMETER_UNSUPPORTED,
            id="parameter-of-a-class-method",
        ),
        pytest.param(
            ["const loader = {", "  load(spec: string) {", "    use(spec);", "  },", "};"],
            SPEC_PARAMETER_UNSUPPORTED,
            id="parameter-of-an-object-method",
        ),
        pytest.param(
            ["register((spec: string) => {", "  use(spec);", "});"],
            SPEC_PARAMETER_UNSUPPORTED,
            id="parameter-of-a-module-level-callback",
        ),
        # a `this` parameter is counted in the index, and no caller passes it
        pytest.param(
            ["function load(this: Foo, spec: string) {", "  use(spec);", "}"],
            SPEC_PARAMETER_UNSUPPORTED,
            id="parameter-of-a-function-with-this",
        ),
        # N15: a parameter that is assigned again is not what a caller passed
        pytest.param(
            ["function load(spec: string) {", "  spec = 'b.yaml';", "  use(spec);", "}"],
            SPEC_PARAMETER_UNSUPPORTED,
            id="N15-parameter-reassigned",
        ),
        pytest.param(
            ["function load(spec: string) {", "  spec += '.yaml';", "  use(spec);", "}"],
            SPEC_PARAMETER_UNSUPPORTED,
            id="N15-parameter-augmented",
        ),
        # a name bound twice that is no parameter is still ambiguous
        pytest.param(
            [
                "function load() {",
                "  var spec = 'a.yaml';",
                "  var spec = 'b.yaml';",
                "  use(spec);",
                "}",
            ],
            SPEC_AMBIGUOUS_BINDING,
            id="N15-not-a-parameter-stays-ambiguous",
        ),
        # N13 beyond a segment: a default or namespace import, and a package, are never read
        pytest.param(
            [PATH, "import SPEC from './config';", "use(path.join(__dirname, SPEC));"],
            SPEC_IMPORT_UNSUPPORTED,
            id="relative-default-import",
        ),
        pytest.param(
            ["import SPEC from './config';", "use(SPEC);"],
            SPEC_IMPORT_UNSUPPORTED,
            id="relative-default-import-as-the-path",
        ),
        pytest.param(
            [
                PATH,
                "import { default as SPEC } from './config';",
                "use(path.join(__dirname, SPEC));",
            ],
            SPEC_IMPORT_UNSUPPORTED,
            id="relative-default-import-by-name",
        ),
        pytest.param(
            [
                PATH,
                "import * as config from './config';",
                "use(path.join(__dirname, config.SPEC));",
            ],
            SPEC_IMPORT_UNSUPPORTED,
            id="relative-namespace-import",
        ),
        pytest.param(
            ["import { SPEC } from 'config';", "use(SPEC);"],
            SPEC_IMPORT_UNSUPPORTED,
            id="package-import-as-the-path",
        ),
        # N23: an imported value cannot be the anchor, whatever builds the path from it
        pytest.param(
            [PATH, DIRS, "use(path.join(API_DIR, 'a.yaml'));"],
            SPEC_IMPORT_UNSUPPORTED,
            id="N23-imported-anchor-join",
        ),
        pytest.param(
            [DIRS, "use(`${API_DIR}/a.yaml`);"],
            SPEC_IMPORT_UNSUPPORTED,
            id="N23-imported-anchor-template",
        ),
        pytest.param(
            [DIRS, "use(API_DIR + '/a.yaml');"],
            SPEC_IMPORT_UNSUPPORTED,
            id="N23-imported-anchor-concatenation",
        ),
        pytest.param(
            [PATH, DIRS, "const ROOT = API_DIR;", "use(path.join(ROOT, 'a.yaml'));"],
            SPEC_IMPORT_UNSUPPORTED,
            id="N23-imported-anchor-through-an-alias",
        ),
        # an import is a whole segment of a join, not a piece of text
        pytest.param(
            [DIRS, "use(`${__dirname}/${API_DIR}/a.yaml`);"],
            SPEC_IMPORT_UNSUPPORTED,
            id="imported-template-segment",
        ),
        pytest.param(
            [DIRS, "use(__dirname + '/' + API_DIR);"],
            SPEC_IMPORT_UNSUPPORTED,
            id="imported-concatenated-segment",
        ),
        # forms the reader does not read
        pytest.param(
            [PATH, "use(path.join(__dirname, flag ? 'a.yaml' : 'b.yaml'));"],
            SPEC_DYNAMIC,
            id="conditional",
        ),
        pytest.param(
            [PATH, "use(path.join(__dirname, ...parts));"], SPEC_DYNAMIC, id="spread-argument"
        ),
        pytest.param(
            [PATH, "use(`${__dirname}x/a.yaml`);"], SPEC_DYNAMIC, id="template-glued-to-the-anchor"
        ),
        pytest.param(
            ["use(__dirname + 'x/a.yaml');"], SPEC_DYNAMIC, id="concatenation-glued-to-the-anchor"
        ),
        pytest.param(
            [PATH, "use(path.join(__dirname, 'a', 'b' + SEG));"],
            SPEC_DYNAMIC,
            id="concatenated-segment",
        ),
        pytest.param([PATH, "use(__filename);"], SPEC_DYNAMIC, id="the-file-is-not-a-directory"),
    ],
)
def test_a_path_the_source_does_not_state_is_refused_with_its_reason(
    lines: list[str], code: str
) -> None:
    assert _read(*lines) == Refused(code)


# -- bounds -------------------------------------------------------------------------------------


def test_five_names_deep_is_refused_and_four_are_not() -> None:
    chain = [PATH, "const A0 = 'a.yaml';", *(f"const A{i} = A{i - 1};" for i in range(1, 5))]
    assert _read(*chain, "use(path.join(__dirname, A3));") == Spec("a.yaml")
    assert _read(*chain, "use(path.join(__dirname, A4));") == Refused(SPEC_DEPTH_EXCEEDED)


@pytest.mark.adversarial
def test_a_five_thousand_hop_alias_chain_is_refused() -> None:
    chain = [PATH, "const A0 = 'a.yaml';", *(f"const A{i} = A{i - 1};" for i in range(1, 5000))]
    assert _read(*chain, "use(path.join(__dirname, A4999));") == Refused(SPEC_DEPTH_EXCEEDED)


@pytest.mark.adversarial
def test_a_five_thousand_segment_join_is_refused() -> None:
    segments = ", ".join(["'a'"] * 5000)
    source = [PATH, f"use(path.join(__dirname, {segments}));"]
    assert _read(*source) == Refused(SPEC_DEPTH_EXCEEDED)


@pytest.mark.adversarial
def test_a_five_thousand_term_concatenation_is_refused() -> None:
    assert _read("use(__dirname" + " + '/a'" * 5000 + ");") == Refused(SPEC_DEPTH_EXCEEDED)


def test_thirty_two_segments_are_read_and_thirty_three_are_not() -> None:
    def join(count: int) -> str:
        return "use(path.join(__dirname, " + ", ".join(["'a'"] * count) + "));"

    assert _read(PATH, join(32)) == Spec("/".join(["a"] * 32))
    assert _read(PATH, join(33)) == Refused(SPEC_DEPTH_EXCEEDED)


# -- the codes ----------------------------------------------------------------------------------


def test_the_refusal_codes_are_the_frozen_strings() -> None:
    assert [
        SPEC_CWD_RELATIVE,
        SPEC_ABSOLUTE,
        SPEC_ENVIRONMENT,
        SPEC_DYNAMIC,
        SPEC_UNRESOLVED_NAME,
        SPEC_AMBIGUOUS_BINDING,
        SPEC_MUTABLE_BINDING,
        SPEC_USED_BEFORE_BINDING,
        SPEC_SHADOWED_ANCHOR,
        SPEC_DEPTH_EXCEEDED,
        SPEC_IMPORT_UNSUPPORTED,
        SPEC_IMPORT_UNRESOLVED,
        SPEC_PARAMETER_UNSUPPORTED,
        SPEC_PROPAGATION_DEPTH,
        SPEC_PARAMETER_UNCALLED,
        SPEC_OUTSIDE_REPOSITORY,
        SPEC_NOT_INDEXED,
        SPEC_NOT_A_FILE,
    ] == [
        "spec-cwd-relative",
        "spec-absolute",
        "spec-environment",
        "spec-dynamic",
        "spec-unresolved-name",
        "spec-ambiguous-binding",
        "spec-mutable-binding",
        "spec-used-before-binding",
        "spec-shadowed-anchor",
        "spec-depth-exceeded",
        "spec-import-unsupported",
        "spec-import-unresolved",
        "spec-parameter-unsupported",
        "spec-propagation-depth",
        "spec-parameter-uncalled",
        "spec-outside-repository",
        "spec-not-indexed",
        "spec-not-a-file",
    ]
