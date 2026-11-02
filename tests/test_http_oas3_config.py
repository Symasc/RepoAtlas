"""How an oas3-tools application's specification is read, and recorded in the claims (M7, C2).

The parser records the reading; the global pass that follows it is a later step. An
application's claim has exactly one of ``spec``, ``spec_parts``, ``spec_imported_path``,
``spec_param`` (with ``factory`` and ``factory_exports``) or ``spec_refused``. A call of a
function that holds an application records ``oas3-factory-call``, and a module's exported
paths of documents ``ts-path-constants``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from repoatlas.models.parsing import ParsedReference
from repoatlas.parsers.http_facts import (
    OAS3_APP,
    OAS3_FACTORY_CALL,
    PENDING_FACT,
    TS_PATH_CONSTANTS,
)
from tests.test_http_oas3 import HANDLERS, HERE, _claims, d

HEAD = (
    "import path from 'path';\n"
    "import * as oas3 from 'oas3-tools';\n"
    "const handlers = { getStatus: (req, res) => res.end() };\n"
)
OPTIONS = "{ routing: { controllers: handlers } }"
#: Every key an application's claim may record its reading of the specification under.
SPEC_KEYS = (
    "spec",
    "spec_parts",
    "spec_imported_path",
    "spec_param",
    "factory",
    "factory_exports",
    "spec_refused",
)
A_YAML = "path.join(__dirname, '../api/a.yaml')"


def _source(*lines: str) -> str:
    return HEAD + "\n".join(lines) + "\n"


def _app(*lines: str) -> dict[str, str]:
    """The reading an application's claim records, and nothing else of it."""
    [claim] = _claims(_source(*lines), OAS3_APP)
    return {key: value for key, value in claim.meta.items() if key in SPEC_KEYS}


def _call(spec: str) -> str:
    return f"oas3.expressAppConfig({spec}, {OPTIONS});"


def _arguments(claim: ParsedReference) -> list[dict[str, object]]:
    return json.loads(claim.meta["arguments"])


# -- the application ----------------------------------------------------------------------------


def test_a_path_the_file_states_whole_keeps_the_spec_key_and_nothing_else() -> None:
    # P1: it resolved before M7, and its claim is the one it always was
    source = _source(f"const A = {A_YAML};", "const B = A;", _call("B"))
    [claim] = _claims(source, OAS3_APP)
    assert claim.meta == {
        PENDING_FACT: OAS3_APP,
        "spec": "../api/a.yaml",
        "controllers": json.dumps([{"local": "handlers"}], sort_keys=True),
    }
    assert list(claim.meta) == [PENDING_FACT, "spec", "controllers"]


@pytest.mark.parametrize(
    ("lines", "parts"),
    [
        pytest.param(
            ["import { API_DIR } from './dirs';", _call("path.join(__dirname, API_DIR, 'a.yaml')")],
            [["dir"], ["r", "./dirs", "API_DIR"], ["t", "a.yaml"]],
            id="P8-imported-segment",
        ),
        pytest.param(
            [
                "import { SPEC_FILE } from '../config/names';",
                _call("path.join(__dirname, '../api', SPEC_FILE)"),
            ],
            [["dir"], ["t", "../api"], ["r", "../config/names", "SPEC_FILE"]],
            id="P8-own-literals-come-first",
        ),
        pytest.param(
            [
                "import { SPEC_FILE as FILE } from '../config/names';",
                "const ROOT = path.resolve(__dirname, '..');",
                _call("path.join(ROOT, 'api', FILE)"),
            ],
            [["dir"], ["t", ".."], ["t", "api"], ["r", "../config/names", "SPEC_FILE"]],
            id="P8-aliased-import-in-a-nested-anchor",
        ),
    ],
)
def test_segments_read_in_another_module_are_recorded_in_order(
    lines: list[str], parts: list[list[str]]
) -> None:
    assert {key: json.loads(value) for key, value in _app(*lines).items()} == {"spec_parts": parts}


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        pytest.param(
            ["import { SPEC_PATH } from '../config/paths';", _call("SPEC_PATH")],
            [["p", "../config/paths", "SPEC_PATH"]],
            id="P9-imported-path",
        ),
        pytest.param(
            [
                "import { SPEC_PATH as SPEC } from './paths';",
                "const WHICH = SPEC;",
                _call("WHICH"),
            ],
            [["p", "./paths", "SPEC_PATH"]],
            id="P9-aliased-and-through-a-const",
        ),
    ],
)
def test_a_whole_path_imported_from_a_relative_module_is_recorded_unresolved(
    lines: list[str], expected: list[list[str]]
) -> None:
    # resolved by the global pass, never here
    recorded = _app(*lines)
    assert list(recorded) == ["spec_imported_path"]
    assert json.loads(recorded["spec_imported_path"]) == expected


@pytest.mark.parametrize(
    ("lines", "exports"),
    [
        pytest.param(
            [
                f"export function createApp(spec: string) {{ return {_call('spec')} }}",
                f"createApp({A_YAML});",
            ],
            ["createApp"],
            id="P10-exported-declaration",
        ),
        pytest.param(
            [f"function createApp(spec: string) {{ return {_call('spec')} }}"],
            [],
            id="not-exported",
        ),
        pytest.param(
            [f"export const createApp = (spec: string) => {_call('spec')}"],
            ["createApp"],
            id="exported-arrow",
        ),
        pytest.param(
            [
                f"const createApp = (spec) => {_call('spec')}",
                "export { createApp as boot, createApp as start };",
            ],
            ["boot", "start"],
            id="exported-under-other-names",
        ),
        pytest.param(
            [
                f"function createApp(root, spec) {{ return {_call('spec')} }}",
                "export default createApp;",
            ],
            ["default"],
            id="default-export",
        ),
    ],
)
def test_a_path_that_is_a_parameter_records_the_function_and_where_it_is_exported(
    lines: list[str], exports: list[str]
) -> None:
    recorded = _app(*lines)
    assert list(recorded) == ["spec_param", "factory", "factory_exports"]
    assert recorded["factory"] == "createApp"
    assert json.loads(recorded["factory_exports"]) == exports


def test_the_index_is_the_position_of_the_parameter() -> None:
    recorded = _app(f"function createApp(root: string, spec: string) {{ return {_call('spec')} }}")
    assert recorded["spec_param"] == "1"


@pytest.mark.parametrize(
    ("lines", "code"),
    [
        pytest.param([_call("'api/a.yaml'")], "spec-cwd-relative", id="N1-bare-literal"),
        pytest.param([_call("process.cwd()")], "spec-cwd-relative", id="N2-cwd"),
        pytest.param([_call("path.resolve('api/a.yaml')")], "spec-cwd-relative", id="N3-resolve"),
        pytest.param(
            [
                f"const SPEC = {A_YAML};",
                f"function boot() {{ const SPEC = 'api/a.yaml'; return {_call('SPEC')} }}",
            ],
            "spec-cwd-relative",
            id="N7-inner-const-shadows-the-anchored-one",
        ),
        pytest.param(
            [
                f"function createApp(spec = {A_YAML}) {{ return {_call('spec')} }}",
                "createApp();",
            ],
            "spec-parameter-unsupported",
            id="N14-parameter-default",
        ),
        pytest.param(
            [
                "function createApp(spec: string) {",
                "  spec = 'api/b.yaml';",
                f"  return {_call('spec')}",
                "}",
            ],
            "spec-parameter-unsupported",
            id="N15-parameter-reassigned",
        ),
        pytest.param(
            [
                f"let SPEC = {A_YAML};",
                _call("SPEC"),
            ],
            "spec-mutable-binding",
            id="N5-let",
        ),
        pytest.param(
            [_call("path.join(__dirname, '/etc/a.yaml')")], "spec-absolute", id="N10-absolute"
        ),
    ],
)
def test_a_path_the_source_does_not_state_records_why(lines: list[str], code: str) -> None:
    assert _app(*lines) == {"spec_refused": code}


def test_each_application_has_exactly_one_key_for_its_specification() -> None:
    sources = [
        [_call(A_YAML)],
        ["import { D } from './d';", _call("path.join(__dirname, D, 'a.yaml')")],
        ["import { S } from './d';", _call("S")],
        [f"function f(s) {{ return {_call('s')} }}"],
        [_call("'a.yaml'")],
    ]
    for lines in sources:
        recorded = _app(*lines)
        groups = [{"spec"}, {"spec_parts"}, {"spec_imported_path"}, {"spec_refused"}]
        groups.append({"spec_param", "factory", "factory_exports"})
        assert set(recorded) in groups, recorded


@pytest.mark.parametrize(
    ("spec_argument", "options", "spec", "controllers"),
    [
        # the same cases as test_http_oas3's, which must read as they always did
        ("'api/openapi.yaml'", HANDLERS, None, True),
        ("path.join(cwd, 'api.yaml')", HANDLERS, None, True),
        ("path.join(__dirname, '/abs.yaml')", HANDLERS, None, True),
        (HERE, "{ routing: { controllers: path.join(__dirname, 'c') } }", "a.yaml", False),
        (HERE, "{ routing: { controllers: { ...handlers, x: () => 1 } } }", "a.yaml", False),
        (HERE, "{ routing: { controllers: { ...other } } }", "a.yaml", False),
        (HERE, "{ ...defaults, routing: { controllers: handlers } }", "a.yaml", False),
    ],
)
def test_the_specifications_the_first_reader_read_are_read_the_same(
    spec_argument: str, options: str, spec: str | None, controllers: bool
) -> None:
    source = f"""\
        import path from 'path';
        import {{ expressAppConfig }} from 'oas3-tools';

        const handlers = {{ getStatus: (req, res) => res.end() }};
        const other = {{ name: 'x' }};

        export const app = expressAppConfig({spec_argument}, {options});
    """
    [claim] = _claims(source, OAS3_APP)
    assert claim.meta.get("spec") == spec
    assert ("controllers" in claim.meta) == controllers
    if spec is None:
        assert "spec_refused" in claim.meta
    else:
        assert not set(claim.meta) & (set(SPEC_KEYS) - {"spec"})


# -- calls of the functions that hold applications ---------------------------------------------

FACTORY = f"export function createApp(spec: string) {{ return {_call('spec')} }}"


def _calls(source: str, name: str = "caller.ts") -> list[ParsedReference]:
    return _claims(source, OAS3_FACTORY_CALL, name)


def test_a_call_of_the_factory_the_file_declares_records_what_it_is_handed() -> None:
    # P10
    [claim] = _calls(_source(FACTORY, f"createApp({A_YAML});"))
    assert claim.target_key == "createApp"
    assert claim.meta["local"] == "createApp"
    assert "module" not in claim.meta
    assert _arguments(claim) == [{"spec": "../api/a.yaml"}]


def test_each_call_of_a_factory_is_a_claim() -> None:
    # P12
    source = _source(
        FACTORY,
        "createApp(path.join(__dirname, '../api/a.yaml'));",
        "createApp(path.join(__dirname, '../api/c.yaml'));",
    )
    assert [_arguments(claim) for claim in _calls(source)] == [
        [{"spec": "../api/a.yaml"}],
        [{"spec": "../api/c.yaml"}],
    ]


@pytest.mark.parametrize(
    ("call", "arguments"),
    [
        pytest.param("createApp()", [], id="no-argument"),
        pytest.param(
            "createApp('api/a.yaml')", [{"refused": "spec-cwd-relative"}], id="a-literal-is-refused"
        ),
        pytest.param("createApp(1, 2)", [{}, {}], id="positions-are-kept"),
        pytest.param(
            "createApp(path.join('api', 'a.yaml'))", [{"refused": "spec-cwd-relative"}], id="N3"
        ),
        pytest.param(
            "createApp(...rest)", [{"refused": "spec-dynamic"}], id="spread-has-no-position"
        ),
        pytest.param(
            "createApp(path.join(__dirname, 'a.yaml'), ...rest, path.join(__dirname, 'b.yaml'))",
            [{"spec": "a.yaml"}, {"refused": "spec-dynamic"}, {"refused": "spec-dynamic"}],
            id="spread-in-the-middle",
        ),
        pytest.param(
            "createApp(path.join(__dirname, '/etc/a.yaml'))",
            [{"refused": "spec-absolute"}],
            id="refusal-is-visible",
        ),
    ],
)
def test_a_call_of_a_declared_factory_is_recorded_whatever_it_is_handed(
    call: str, arguments: list[dict[str, object]]
) -> None:
    [claim] = _calls(_source(FACTORY, f"{call};"))
    assert _arguments(claim) == arguments


def test_an_argument_that_is_an_imported_path_is_refused_as_unsupported() -> None:
    # the whole argument is a name another module exports: its value is not read in here
    source = _source(FACTORY, "import { SPEC_PATH } from './paths';", "createApp(SPEC_PATH);")
    [claim] = _calls(source)
    assert claim.meta["local"] == "createApp"
    assert _arguments(claim) == [{"refused": "spec-import-unsupported"}]


def test_an_imported_path_handed_to_an_imported_function_is_stopped_by_the_gate() -> None:
    # the gate reads only a ``const`` there, so the name never reaches the entry: it stays {}
    source = _source(
        IMPORT, "import { SPEC_PATH } from './paths';", f"createApp(SPEC_PATH, {A_YAML});"
    )
    [claim] = _calls(source)
    assert claim.meta["module"] == "./lib/factory"
    assert _arguments(claim) == [{}, {"spec": "../api/a.yaml"}]


def test_an_argument_that_is_a_parameter_is_not_followed_a_second_level() -> None:
    # N16: boot hands its own parameter on
    boot = "function boot(s: string) { return createApp(s); }"
    source = _source(FACTORY, boot, f"boot({A_YAML});")
    [claim] = _calls(source)
    assert claim.meta["local"] == "createApp"
    assert _arguments(claim) == [{"refused": "spec-propagation-depth"}]


def test_a_function_that_holds_no_application_is_no_factory() -> None:
    source = _source("function helper(s: string) { return s; }", f"helper({A_YAML});")
    assert _calls(source) == []


def test_a_name_bound_again_where_it_is_called_is_no_factory() -> None:
    source = _source(FACTORY, f"function f(createApp) {{ return createApp({A_YAML}); }}")
    assert _calls(source) == []


def test_the_arguments_of_a_call_are_at_most_eight() -> None:
    source = _source(FACTORY, f"createApp({A_YAML}, 1, 2, 3, 4, 5, 6, 7, 8, 9);")
    [claim] = _calls(source)
    assert _arguments(claim) == [{"spec": "../api/a.yaml"}, *[{}] * 7]


def test_a_factory_call_in_a_function_has_the_function_as_its_symbol() -> None:
    source = _source(FACTORY, f"export function run() {{ return createApp({A_YAML}); }}")
    [claim] = _calls(source)
    assert claim.source_symbol == "run"


IMPORT = "import { createApp } from './lib/factory';"
PARTS_ARGUMENT = "path.join(__dirname, DIR, 'a.yaml')"


def test_a_call_of_a_factory_imported_by_name_records_the_module_and_the_name() -> None:
    # P11
    source = _source(IMPORT, f"createApp({A_YAML});")
    [claim] = _calls(source, "p11.ts")
    assert claim.target_key == "createApp"
    assert claim.meta["module"] == "./lib/factory"
    assert claim.meta["name"] == "createApp"
    assert "local" not in claim.meta
    assert _arguments(claim) == [{"spec": "../api/a.yaml"}]
    assert _claims(source, OAS3_APP, "p11.ts") == []


def test_a_factory_imported_under_another_name_is_recorded_by_the_name_it_exports() -> None:
    source = _source("import { createApp as build } from '../factory';", f"build({A_YAML});")
    [claim] = _calls(source)
    assert claim.target_key == "build"
    assert (claim.meta["module"], claim.meta["name"]) == ("../factory", "createApp")


def test_a_path_with_a_segment_from_another_module_is_recorded_as_parts() -> None:
    source = _source(IMPORT, "import { DIR } from './dirs';", f"createApp({PARTS_ARGUMENT});")
    [claim] = _calls(source)
    assert _arguments(claim) == [{"spec_parts": [["dir"], ["r", "./dirs", "DIR"], ["t", "a.yaml"]]}]


@pytest.mark.parametrize(
    ("argument", "recorded"),
    [
        pytest.param("path.join(__dirname, 'a.yaml')", True, id="yaml"),
        pytest.param("path.join(__dirname, 'a.yml')", True, id="yml"),
        pytest.param("path.join(__dirname, 'a.json')", True, id="json"),
        pytest.param("path.join(__dirname, 'a.txt')", False, id="another-suffix"),
        pytest.param("path.join(__dirname, 'yaml')", False, id="a-name-ending-yaml-without-a-dot"),
        pytest.param("path.join(__dirname, 'a.yaml.bak')", False, id="suffix-not-last"),
        pytest.param("path.join(__dirname, 'api')", False, id="a-directory"),
        pytest.param("path.join(__dirname, DIR, 'a.txt')", False, id="parts-ending-in-a-literal"),
        pytest.param("path.join(__dirname, 'api', API_FILE)", True, id="parts-ending-imported"),
        pytest.param("path.join(__dirname, DIR, 'a.yaml')", True, id="parts-ending-yaml"),
        pytest.param("path.join('api', 'a.yaml')", False, id="refused"),
        pytest.param("API_FILE", False, id="a-whole-imported-name"),
        pytest.param("'a.yaml'", False, id="not-shaped-like-a-path"),
        pytest.param("req", False, id="not-a-const"),
    ],
)
def test_a_call_of_an_imported_function_is_recorded_when_an_argument_is_a_document(
    argument: str, recorded: bool
) -> None:
    source = _source(
        IMPORT,
        "import { DIR, API_FILE } from './names';",
        f"createApp({argument});",
    )
    assert len(_calls(source)) == int(recorded)


def test_a_document_in_any_argument_position_records_every_argument() -> None:
    source = _source(IMPORT, f"createApp(1, {A_YAML}, 2);")
    [claim] = _calls(source)
    assert _arguments(claim) == [{}, {"spec": "../api/a.yaml"}, {}]


def test_a_path_held_by_a_const_is_read() -> None:
    source = _source(IMPORT, f"const SPEC = {A_YAML};", "createApp(SPEC);")
    [claim] = _calls(source)
    assert _arguments(claim) == [{"spec": "../api/a.yaml"}]


@pytest.mark.parametrize(
    "lines",
    [
        pytest.param(["import { createApp } from 'some-package';"], id="a-package"),
        pytest.param(["import createApp from './factory';"], id="a-default-import"),
        pytest.param(
            ["import * as lib from './factory';", "const createApp = lib.createApp;"], id="alias"
        ),
        pytest.param(["const createApp = require('./factory');"], id="require"),
        pytest.param(["import type { createApp } from './factory';"], id="a-type"),
    ],
)
def test_only_a_function_imported_by_name_from_a_relative_module_is_followed(
    lines: list[str],
) -> None:
    assert _calls(_source(*lines, f"createApp({A_YAML});")) == []


def test_a_call_of_an_imported_name_bound_again_where_it_is_called_is_not_followed() -> None:
    source = _source(IMPORT, f"function f(createApp) {{ return createApp({A_YAML}); }}")
    assert _calls(source) == []


def test_a_member_call_is_not_a_factory_call() -> None:
    source = _source("import * as lib from './factory';", f"lib.createApp({A_YAML});")
    assert _calls(source) == []


def test_at_most_a_bounded_number_of_arguments_are_read_in_a_file() -> None:
    calls = [f"createApp({A_YAML});"] * 300
    source = _source(IMPORT, *calls)
    assert 0 < len(_calls(source)) < 300


# -- the exported paths of documents ------------------------------------------------------------


def _constants(*lines: str, name: str = "paths.ts") -> dict[str, list[object]] | None:
    source = "import path from 'path';\n" + "\n".join(lines) + "\n"
    claims = _claims(source, TS_PATH_CONSTANTS, name)
    if not claims:
        return None
    [claim] = claims
    return json.loads(claim.meta["constants"])


def test_a_module_offers_the_exported_paths_that_it_reads_as_documents() -> None:
    claims = _claims(
        d("""
            import path from 'path';
            export const SPEC_PATH = path.join(__dirname, '../api/a.yaml');
            export let MUTABLE;
            export const API_DIR = path.join(__dirname, '../api');
        """),
        TS_PATH_CONSTANTS,
        "paths.ts",
    )
    [claim] = claims
    assert json.loads(claim.meta["constants"]) == {"SPEC_PATH": ["../api/a.yaml", 2]}
    assert claim.target_key == TS_PATH_CONSTANTS
    assert (claim.line_start, claim.line_end) == (2, 2)


@pytest.mark.parametrize(
    ("value", "offered"),
    [
        pytest.param("path.join(__dirname, 'a.yaml')", True, id="yaml"),
        pytest.param("path.join(__dirname, 'a.yml')", True, id="yml"),
        pytest.param("path.join(__dirname, 'a.json')", True, id="json"),
        pytest.param("path.join(__dirname, 'a.txt')", False, id="another-suffix"),
        pytest.param("path.join(__dirname, 'yml')", False, id="a-name-ending-yml-without-a-dot"),
        pytest.param("path.join(__dirname, 'api')", False, id="a-directory"),
        pytest.param("path.join(__dirname, DIR, 'a.yaml')", False, id="a-segment-from-elsewhere"),
        pytest.param("DOC", True, id="a-const-alias"),
        pytest.param("`${__dirname}/api/a.yaml`", True, id="template"),
        pytest.param("__dirname + '/api/a.yaml'", True, id="concatenation"),
        pytest.param("'api/a.yaml'", False, id="bare-literal"),
        pytest.param("path.join('api', 'a.yaml')", False, id="bare-join"),
        pytest.param("{ spec: 'a.yaml' }", False, id="an-object"),
        pytest.param("load('a.yaml')", False, id="a-call-of-something-else"),
    ],
)
def test_the_suffix_of_the_path_decides_what_a_module_offers(value: str, offered: bool) -> None:
    constants = _constants(
        "import { DIR } from './dirs';",
        "const DOC = path.join(__dirname, 'doc.yaml');",
        f"export const X = {value};",
    )
    assert (constants is not None) == offered
    if offered:
        assert constants is not None and list(constants) == ["X"]


def test_only_exported_module_level_consts_that_are_bound_once_are_offered() -> None:
    constants = _constants(
        "const PRIVATE = path.join(__dirname, 'private.yaml');",
        "const KEPT = path.join(__dirname, 'kept.yaml');",
        "export { KEPT, KEPT as RENAMED };",
        "export const ONCE = path.join(__dirname, 'once.yaml');",
        "export const TWICE = path.join(__dirname, 'twice.yaml');",
        "function other() { const TWICE = 1; return TWICE; }",
        "export let LET = path.join(__dirname, 'let.yaml');",
        "export default path.join(__dirname, 'default.yaml');",
        "function f() { return path.join(__dirname, 'in-function.yaml'); }",
    )
    assert constants is not None
    assert {name: value[0] for name, value in constants.items()} == {
        "KEPT": "kept.yaml",
        "RENAMED": "kept.yaml",
        "ONCE": "once.yaml",
    }


def test_a_constant_imported_from_another_module_is_not_offered() -> None:
    constants = _constants(
        "import { SPEC_PATH } from './other';",
        "export const SPEC = SPEC_PATH;",
        "export { SPEC_PATH };",
    )
    assert constants is None


def test_a_module_with_no_document_path_records_no_claim() -> None:
    assert _constants("export const PORT = 3000;", "export const NAME = 'a.yaml';") is None


def test_a_module_offers_at_most_sixty_four_paths_in_line_order() -> None:
    declarations = [f"export const C{n:02d} = path.join(__dirname, '{n}.yaml');" for n in range(70)]
    constants = _constants(*declarations)
    assert constants is not None
    assert len(constants) == 64
    assert list(constants) == sorted(constants)
    assert "C63" in constants and "C64" not in constants


def test_a_path_that_reads_an_unresolved_name_is_offered_by_nothing() -> None:
    assert _constants("export const X = path.join(__dirname, UNKNOWN, 'a.yaml');") is None


# -- the fixture ---------------------------------------------------------------------------------

FIXTURE: dict[str, str] = {
    "config/paths.ts": d("""
        import path from 'path';
        export const SPEC_PATH = path.join(__dirname, '../api/a.yaml');
        export let MUTABLE;
        export const API_DIR = path.join(__dirname, '../api');
    """),
    "config/names.ts": d("""
        export const SPEC_FILE = 'api/a.yaml';
        export const ABS = '/etc/a.yaml';
    """),
    "config/index.ts": "export { SPEC_PATH } from './paths';\n",
    "src/lib/factory.ts": _source(FACTORY),
    "src/p9/app.ts": _source("import { SPEC_PATH } from '../../config/paths';", _call("SPEC_PATH")),
    "src/p10.ts": _source(FACTORY, f"createApp({A_YAML});"),
    "src/p11.ts": _source(IMPORT, f"createApp({A_YAML});"),
    "src/p12.ts": _source(
        FACTORY,
        "createApp(path.join(__dirname, '../api/a.yaml'));",
        "createApp(path.join(__dirname, '../api/c.yaml'));",
    ),
    "src/n14.ts": _source(
        f"function createApp(spec = {A_YAML}) {{ return {_call('spec')} }}", "createApp();"
    ),
    "src/n16.ts": _source(
        FACTORY, "function boot(s: string) { return createApp(s); }", f"boot({A_YAML});"
    ),
    "src/n17.ts": _source(FACTORY),
}


def _count(role: str) -> dict[str, int]:
    found = {name: len(_claims(text, role, Path(name).name)) for name, text in FIXTURE.items()}
    return {name: count for name, count in found.items() if count}


def test_the_fixture_has_the_factory_calls_the_plan_counts() -> None:
    # P10 1, P11 1, P12 2, N14 1, N16 1 (refused), N17 0
    assert _count(OAS3_FACTORY_CALL) == {
        "src/p10.ts": 1,
        "src/p11.ts": 1,
        "src/p12.ts": 2,
        "src/n14.ts": 1,
        "src/n16.ts": 1,
    }


def test_the_fixture_offers_one_module_of_paths_and_only_the_one_path() -> None:
    assert _count(TS_PATH_CONSTANTS) == {"config/paths.ts": 1}
    [claim] = _claims(FIXTURE["config/paths.ts"], TS_PATH_CONSTANTS, "paths.ts")
    assert list(json.loads(claim.meta["constants"])) == ["SPEC_PATH"]


def test_the_fixture_has_one_application_claim_per_call_of_the_library() -> None:
    assert _count(OAS3_APP) == {
        "src/lib/factory.ts": 1,
        "src/p9/app.ts": 1,
        "src/p10.ts": 1,
        "src/p12.ts": 1,
        "src/n14.ts": 1,
        "src/n16.ts": 1,
        "src/n17.ts": 1,
    }


def test_the_fixture_refusal_of_a_second_level_is_visible_in_the_claim() -> None:
    [claim] = _claims(FIXTURE["src/n16.ts"], OAS3_FACTORY_CALL, "n16.ts")
    assert _arguments(claim) == [{"refused": "spec-propagation-depth"}]
