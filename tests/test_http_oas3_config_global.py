"""The global pass serves an application's specification through imports and factories (M7, C3).

An oas3-tools application states the path of its OpenAPI document in more forms than
``path.join(__dirname, <literals>)``: a chain of ``const`` names, a template, a name another
module exports, or the parameter of the function that holds it (a factory), whose callers
hand it the path. The parser records each reading as a claim (``test_http_oas3_config``); this
pass reads the claims of the modules they name, and serves the document's operations only when
every step is proven. Every refusal says why, by a ``spec-...`` code.

The fixture is the one pre-registered for the milestone: a repository ``cfg`` with one file for
each case, each with a handler object of its own (``h_<case>``), a decoy document at
``src/api/a.yaml`` that a wrongly anchored path would find, and two further repositories.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.graph.http_claims import HttpClaimPlan
from repoatlas.graph.linker import ReferenceLinker
from repoatlas.graph.resolution import ResolutionIndex
from repoatlas.graph.ts_oas3 import (
    PROMOTED,
    SPEC_NOT_FOUND,
    SPEC_UNREAD,
    SpecRoutedPlan,
    _locate,
    _Refusal,
    plan_spec_routed,
)
from repoatlas.parsers.http_facts import (
    OAS3_APP,
    OAS3_FACTORY_CALL,
    PENDING_FACT,
    TS_PATH_CONSTANTS,
)
from repoatlas.parsers.ts_spec_paths import (
    SPEC_ABSOLUTE,
    SPEC_NOT_A_FILE,
    SPEC_OUTSIDE_REPOSITORY,
)
from repoatlas.services.workspace import Workspace
from repoatlas.storage.protocols import UnresolvedRefRecord
from tests import test_http_oas3_config as config_parser
from tests.impact_corpus import build, make_repo
from tests.incremental_corpus import assert_equals_fresh, edit, fresh_index, sync_all
from tests.test_http_oas3 import _claims, d

PATH = "import path from 'path';"
URL = "import { fileURLToPath } from 'url';"
IMPORT = "import { createApp } from './lib/factory';"
A_YAML = "path.join(__dirname, '../api/a.yaml')"
C_YAML = "path.join(__dirname, '../api/c.yaml')"
ROUTE_A = "GET /a/status"
ROUTE_C = "GET /c/status"


def _document(base: str) -> str:
    return d(f"""
        openapi: 3.0.3
        info: {{title: t, version: 1.0.0}}
        servers:
          - url: {base}
        paths:
          /status:
            get:
              operationId: getStatus
              responses: {{'200': {{description: up}}}}
    """)


def _lines(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def _file(name: str, *lines: str) -> str:
    """A module with the handler object ``h_<name>`` and the statements ``lines``."""
    return _lines(
        "import * as oas3 from 'oas3-tools';",
        f"const h_{name} = {{ getStatus: (req, res) => res.end() }};",
        *lines,
    )


def _call(name: str, spec: str) -> str:
    """``expressAppConfig`` of the document ``spec``, with the controllers ``h_<name>``."""
    return f"oas3.expressAppConfig({spec}, {{ routing: {{ controllers: h_{name} }} }});"


def _factory(name: str, parameters: str = "spec: string", spec: str = "spec") -> str:
    return f"export function createApp({parameters}) {{ return {_call(name, spec)} }}"


def _case(name: str, *lines: str) -> dict[str, str]:
    return {f"src/{name}.ts": _file(name, *lines)}


NAMES_TS = d("""
    export const SPEC_FILE = 'api/a.yaml';
    export const ABS = '/etc/a.yaml';
""")
PATHS_TS = d("""
    import path from 'path';
    export const SPEC_PATH = path.join(__dirname, '../api/a.yaml');
    export let MUTABLE;
    export const API_DIR = path.join(__dirname, '../api');
""")

CFG: dict[str, str] = {
    "package.json": '{"name": "cfg"}\n',
    "api/a.yaml": _document("/a"),
    "api/c.yaml": _document("/c"),
    "src/api/a.yaml": _document("/b"),
    "config/names.ts": NAMES_TS,
    "config/paths.ts": PATHS_TS,
    "config/index.ts": "export { SPEC_PATH } from './paths';\n",
    # -- positive: each serves GET /a/status from its own handler ------------------------------
    **_case("p1", PATH, f"const A = {A_YAML};", "const B = A;", _call("p1", "B")),
    **_case("p2", "const SPEC = `${__dirname}/../api/a.yaml`;", _call("p2", "SPEC")),
    **_case("p3", _call("p3", "__dirname + '/../api/a.yaml'")),
    **_case(
        "p4",
        PATH,
        "const ROOT = path.resolve(__dirname, '..');",
        _call("p4", "path.join(ROOT, 'api', 'a.yaml')"),
    ),
    **_case(
        "p5",
        PATH,
        URL,
        "const __dirname = path.dirname(fileURLToPath(import.meta.url));",
        _call("p5", A_YAML),
    ),
    **_case(
        "p6", "const { join } = require('path');", _call("p6", "join(__dirname, '../api/a.yaml')")
    ),
    **_case(
        "p7",
        PATH,
        "const CONFIG = { spec: 'a.yaml', other: 1 };",
        _call("p7", "path.join(__dirname, '../api', CONFIG.spec)"),
    ),
    **_case(
        "p8",
        PATH,
        "import { SPEC_FILE } from '../config/names';",
        _call("p8", "path.join(__dirname, '..', SPEC_FILE)"),
    ),
    "src/p9/app.ts": _file(
        "p9", "import { SPEC_PATH } from '../../config/paths';", _call("p9", "SPEC_PATH")
    ),
    **_case("p10", PATH, _factory("p10"), f"createApp({A_YAML});"),
    "src/lib/factory.ts": _file("p11", _factory("p11")),
    "src/p11.ts": _lines(PATH, IMPORT, f"createApp({A_YAML});"),
    **_case("p12", PATH, _factory("p12"), f"createApp({A_YAML});", f"createApp({C_YAML});"),
    **_case("p13", PATH, _call("p13", "path.join(path.dirname(__filename), '../api/a.yaml')")),
    **_case("p14", URL, _call("p14", "fileURLToPath(new URL('../api/a.yaml', import.meta.url))")),
    # -- negative: no edge, and the code that says why -----------------------------------------
    **_case("n1", _call("n1", "'api/a.yaml'")),
    **_case("n1b", "import { SPEC_FILE } from '../config/names';", _call("n1b", "SPEC_FILE")),
    **_case("n2", _call("n2", "process.cwd()")),
    **_case("n3", PATH, _call("n3", "path.resolve('api/a.yaml')")),
    **_case("n4", PATH, _call("n4", f"process.env.S ?? {A_YAML}")),
    **_case("n5", PATH, f"let SPEC = {A_YAML};", _call("n5", "SPEC")),
    **_case("n6", PATH, f"var SPEC = {A_YAML};", "var SPEC = 'b.yaml';", _call("n6", "SPEC")),
    **_case(
        "n7",
        PATH,
        f"const SPEC = {A_YAML};",
        f"function boot() {{ const SPEC = 'api/a.yaml'; return {_call('n7', 'SPEC')} }}",
    ),
    **_case("n8", PATH, _call("n8", "SPEC"), f"const SPEC = {A_YAML};"),
    **_case(
        "n9",
        PATH,
        f"function load(__dirname: string) {{ return {_call('n9', A_YAML)} }}",
    ),
    **_case("n10", PATH, _call("n10", "path.join(__dirname, '/etc/a.yaml')")),
    **_case("n11", PATH, _call("n11", "path.join(__dirname, '../../other/api/a.yaml')")),
    **_case("n12", "import { SPEC_PATH } from '../config';", _call("n12", "SPEC_PATH")),
    **_case(
        "n13",
        PATH,
        "import { SPEC } from 'config-package';",
        _call("n13", "path.join(__dirname, SPEC)"),
    ),
    **_case(
        "n14",
        PATH,
        f"function createApp(spec = {A_YAML}) {{ return {_call('n14', 'spec')} }}",
        "createApp();",
    ),
    **_case(
        "n15",
        "function createApp(spec: string) {",
        "  spec = 'api/b.yaml';",
        f"  return {_call('n15', 'spec')}",
        "}",
    ),
    **_case(
        "n16",
        PATH,
        _factory("n16"),
        "function boot(s: string) { return createApp(s); }",
        f"boot({A_YAML});",
    ),
    **_case("n17", _factory("n17")),
    **_case(
        "n18",
        PATH,
        "import { readFileSync } from 'fs';",
        _call("n18", "readFileSync(path.join(__dirname, '../api/a.yaml'), 'utf8')"),
    ),
    **_case("n18b", PATH, _call("n18b", "YAML.load(path.join(__dirname, '../api/a.yaml'))")),
    **_case(
        "n19",
        PATH,
        f"const A0 = {A_YAML};",
        *(f"const A{n + 1} = A{n};" for n in range(6)),
        _call("n19", "A6"),
    ),
    **_case(
        "n20",
        PATH,
        "const CONFIG = { spec: 'a.yaml' };",
        "CONFIG.spec = 'b.yaml';",
        _call("n20", "path.join(__dirname, '../api', CONFIG.spec)"),
    ),
    **_case("n21", "import { ABS } from '../config/names';", _call("n21", "ABS")),
    **_case("n22", "import { MUTABLE } from '../config/paths';", _call("n22", "MUTABLE")),
    **_case("n22b", "import { NOPE } from '../config/paths';", _call("n22b", "NOPE")),
    **_case(
        "n23",
        PATH,
        "import { API_DIR } from '../config/paths';",
        _call("n23", "path.join(API_DIR, 'a.yaml')"),
    ),
    # a path that is read and names no document
    **_case("x_missing", PATH, _call("x_missing", "path.join(__dirname, '../api/missing.yaml')")),
}

OTHER: dict[str, str] = {
    "package.json": '{"name": "other"}\n',
    "api/a.yaml": _document("/o"),
}

#: N24: one document, an application whose specification is an environment read, and a file that
#: serves a document through swagger-ui and a directory through express, with no oas3-tools.
LONELY: dict[str, str] = {
    "package.json": '{"name": "lonely"}\n',
    "docs/openapi.yaml": _document("/l"),
    "src/lonely_env.ts": _file("lonely_env", _call("lonely_env", "process.env.SPEC")),
    "src/swagger.ts": _lines(
        "import express from 'express';",
        "import path from 'path';",
        "import swaggerUi from 'swagger-ui-express';",
        "import YAML from 'yamljs';",
        "const h_swagger = { getStatus: (req, res) => res.end() };",
        "const app = express();",
        "app.use('/docs', swaggerUi.serve, "
        "swaggerUi.setup(YAML.load(path.join(__dirname, '../docs/openapi.yaml'))));",
        "app.use(express.static(path.join(__dirname, '../docs')));",
    ),
}

#: Factories whose callers disagree: the refusal of the first call by file path, and the
#: promotion of an application any call of which is served.
ORDER: dict[str, str] = {
    "package.json": '{"name": "order"}\n',
    "api/a.yaml": _document("/a"),
    "src/lib/app.ts": _file("order", _factory("order", "root: string, spec: string")),
    "src/lib/app2.ts": _file("mixed", _factory("mixed")),
    # position 1: a literal no caller states, then a document that is not indexed
    "src/a_first.ts": _lines(
        PATH,
        "import { createApp } from './lib/app';",
        f"createApp({A_YAML}, 'a.yaml');",
    ),
    "src/b_second.ts": _lines(
        PATH,
        "import { createApp } from './lib/app';",
        f"createApp({A_YAML}, path.join(__dirname, '../api/missing.yaml'));",
    ),
    # the first call by file path hands a document that is not indexed; the second is served
    "src/m_bad.ts": _lines(
        PATH,
        "import { createApp } from './lib/app2';",
        "createApp(path.join(__dirname, '../api/missing.yaml'));",
    ),
    "src/m_good.ts": _lines(
        PATH,
        "import { createApp } from './lib/app2';",
        f"createApp({A_YAML});",
    ),
}

#: The roots rule: the handler object, the application (a factory) and its only caller are three
#: files, so a root that is the handler's file or the caller's file is not the application's.
ROOTS: dict[str, str] = {
    "package.json": '{"name": "roots"}\n',
    "api/a.yaml": _document("/r"),
    "src/handlers.ts": "export const h_roots = { getStatus: (req, res) => res.end() };\n",
    "src/lib/app.ts": _lines(
        "import * as oas3 from 'oas3-tools';",
        "import { h_roots } from '../handlers';",
        _factory("roots"),
    ),
    "src/main.ts": _lines(PATH, IMPORT.replace("factory", "app"), f"createApp({A_YAML});"),
}

# -- what is expected ----------------------------------------------------------------------------

Edge = tuple[str, str, str, str, int | None]

#: Each positive case's served route: repository, file, handler, route body, line (None).
SINGLE = (1, 2, 3, 4, 5, 6, 7, 8, 10, 13, 14)
POSITIVE: set[Edge] = {
    *(("cfg", f"src/p{n}.ts", f"h_p{n}", ROUTE_A, None) for n in SINGLE),
    ("cfg", "src/p9/app.ts", "h_p9", ROUTE_A, None),
    ("cfg", "src/lib/factory.ts", "h_p11", ROUTE_A, None),
    ("cfg", "src/p12.ts", "h_p12", ROUTE_A, None),
    ("cfg", "src/p12.ts", "h_p12", ROUTE_C, None),
}
MIXED: Edge = ("order", "src/lib/app2.ts", "h_mixed", ROUTE_A, None)

Outcome = tuple[str, str | None]
#: Every application: its outcome, and the code its path was refused with.
APPLICATIONS: dict[str, Outcome] = {
    **{f"src/p{n}.ts": (PROMOTED, None) for n in (*SINGLE, 12)},
    "src/p9/app.ts": (PROMOTED, None),
    "src/lib/factory.ts": (PROMOTED, None),
    "src/n1.ts": (SPEC_UNREAD, "spec-cwd-relative"),
    "src/n1b.ts": (SPEC_UNREAD, "spec-cwd-relative"),
    "src/n2.ts": (SPEC_UNREAD, "spec-cwd-relative"),
    "src/n3.ts": (SPEC_UNREAD, "spec-cwd-relative"),
    "src/n4.ts": (SPEC_UNREAD, "spec-environment"),
    "src/n5.ts": (SPEC_UNREAD, "spec-mutable-binding"),
    "src/n6.ts": (SPEC_UNREAD, "spec-ambiguous-binding"),
    "src/n7.ts": (SPEC_UNREAD, "spec-cwd-relative"),
    "src/n8.ts": (SPEC_UNREAD, "spec-used-before-binding"),
    "src/n9.ts": (SPEC_UNREAD, "spec-shadowed-anchor"),
    "src/n10.ts": (SPEC_UNREAD, "spec-absolute"),
    "src/n11.ts": (SPEC_NOT_FOUND, "spec-outside-repository"),
    "src/n12.ts": (SPEC_UNREAD, "spec-import-unresolved"),
    "src/n13.ts": (SPEC_UNREAD, "spec-import-unsupported"),
    "src/n14.ts": (SPEC_UNREAD, "spec-parameter-unsupported"),
    "src/n15.ts": (SPEC_UNREAD, "spec-parameter-unsupported"),
    "src/n16.ts": (SPEC_UNREAD, "spec-propagation-depth"),
    "src/n17.ts": (SPEC_UNREAD, "spec-parameter-uncalled"),
    "src/n18.ts": (SPEC_UNREAD, "spec-dynamic"),
    "src/n18b.ts": (SPEC_UNREAD, "spec-dynamic"),
    "src/n19.ts": (SPEC_UNREAD, "spec-depth-exceeded"),
    "src/n20.ts": (SPEC_UNREAD, "spec-mutable-binding"),
    "src/n21.ts": (SPEC_UNREAD, "spec-absolute"),
    "src/n22.ts": (SPEC_UNREAD, "spec-import-unresolved"),
    "src/n22b.ts": (SPEC_UNREAD, "spec-import-unresolved"),
    "src/n23.ts": (SPEC_UNREAD, "spec-import-unsupported"),
    "src/x_missing.ts": (SPEC_NOT_FOUND, "spec-not-indexed"),
    "src/lonely_env.ts": (SPEC_UNREAD, "spec-environment"),
    # the first call by file path states a literal; the later one states a missing document
    "src/lib/app.ts": (SPEC_UNREAD, "spec-dynamic"),
    "src/lib/app2.ts": (PROMOTED, None),
}
PROMOTED_FILES = {path for path, (outcome, _) in APPLICATIONS.items() if outcome == PROMOTED}
NEGATIVE = sorted(set(APPLICATIONS) - PROMOTED_FILES)


# -- reading the result ------------------------------------------------------------------------


@pytest.fixture
def indexed(workspace: Workspace, tmp_path: Path) -> Workspace:
    repositories = {"cfg": CFG, "other": OTHER, "lonely": LONELY, "order": ORDER}
    return build(workspace, tmp_path, repositories)


def _served(workspace: Workspace) -> set[Edge]:
    """(repository, file, handler, route, first line or None) for every route served by code."""
    rows = workspace.database.execute(
        """
        SELECT r.name, f.rel_path, s.qualified_name, d.name, e.line_start
        FROM edges e
        JOIN entities s ON s.id = e.src_id
        JOIN entities d ON d.id = e.dst_id
        JOIN files f ON f.id = s.file_id
        JOIN repositories r ON r.id = f.repo_id
        WHERE e.type = 'SERVES_HTTP_ROUTE' AND f.rel_path LIKE '%.ts'
        """
    ).fetchall()
    return {(row[0], row[1], row[2], row[3], row[4]) for row in rows}


def _cfg(edges: set[Edge]) -> set[Edge]:
    return {edge for edge in edges if edge[0] == "cfg"}


def _http(workspace: Workspace) -> HttpClaimPlan:
    return ReferenceLinker(workspace.knowledge).prepare_global().http


def _spec_plan(workspace: Workspace) -> tuple[SpecRoutedPlan, dict[int, str]]:
    """The plan of the oas3-tools stage alone, and each application claim's file."""
    knowledge = workspace.knowledge
    files = knowledge.list_file_rows()
    grouped: defaultdict[str, list[UnresolvedRefRecord]] = defaultdict(list)
    for claim in knowledge.list_unresolved_refs(exclude_uses=True):
        grouped[json.loads(claim.meta_json or "{}").get(PENDING_FACT, "")].append(claim)
    by_id = {row.file_id: row for row in files}
    index = ResolutionIndex.build(files, knowledge.list_symbols())
    plan = plan_spec_routed(grouped, index, by_id)
    applications = {
        claim.id: by_id[claim.file_id].rel_path
        for claim in grouped[OAS3_APP]
        if claim.id is not None
    }
    return plan, applications


def _applications(workspace: Workspace) -> dict[str, Outcome]:
    plan, files = _spec_plan(workspace)
    return {
        files[claim_id]: (outcome, plan.spec_refusals.get(claim_id))
        for claim_id, outcome in plan.outcomes.items()
    }


def _claim_counts(workspace: Workspace, role: str, repository: str = "cfg") -> dict[str, int]:
    rows = workspace.database.execute(
        """
        SELECT f.rel_path, COUNT(*) FROM unresolved_refs u
        JOIN files f ON f.id = u.file_id JOIN repositories r ON r.id = f.repo_id
        WHERE r.name = ? AND u.meta_json LIKE ?
        GROUP BY f.rel_path
        """,
        (repository, f'%"{role}"%'),
    ).fetchall()
    return {row[0]: row[1] for row in rows}


def _document_edges(workspace: Workspace, repository: str) -> set[tuple[str, int | None]]:
    """(route, first line or None) of every route the repository's docs/openapi.yaml serves."""
    rows = workspace.database.execute(
        """
        SELECT d.name, e.line_start FROM edges e JOIN entities d ON d.id = e.dst_id
        JOIN files f ON f.id = e.file_id JOIN repositories r ON r.id = f.repo_id
        WHERE e.type = 'SERVES_HTTP_ROUTE' AND r.name = ? AND f.rel_path = 'docs/openapi.yaml'
        """,
        (repository,),
    ).fetchall()
    return {(row[0], row[1]) for row in rows}


# -- what is served ---------------------------------------------------------------------------


def test_every_positive_case_serves_its_route_from_its_own_handler(indexed: Workspace) -> None:
    # P1-P14: 15 edges in all (P12 serves two documents); none to the decoy's /b or to /o
    served = _served(indexed)
    assert _cfg(served) == POSITIVE
    assert len(_cfg(served)) == 15
    assert not [edge for edge in served if edge[3] in {"GET /b/status", "GET /o/status"}]


def test_a_factory_called_with_two_documents_is_two_applications_of_one_handler(
    indexed: Workspace,
) -> None:
    # P12
    p12 = {edge for edge in _served(indexed) if edge[1] == "src/p12.ts"}
    assert {edge[3] for edge in p12} == {ROUTE_A, ROUTE_C}
    assert {edge[2] for edge in p12} == {"h_p12"}


def test_a_path_imported_from_another_module_is_anchored_at_that_module(
    indexed: Workspace,
) -> None:
    # P9: config/paths.ts says ../api/a.yaml; read from src/p9/app.ts it would be the /b decoy
    served = _served(indexed)
    assert ("cfg", "src/p9/app.ts", "h_p9", ROUTE_A, None) in served
    assert not [edge for edge in served if edge[1] == "src/p9/app.ts" and edge[3] != ROUTE_A]


def test_a_path_a_caller_hands_over_is_read_from_the_callers_file(indexed: Workspace) -> None:
    # P11: from src/lib/factory.ts the same text would be the /b decoy
    served = _served(indexed)
    assert ("cfg", "src/lib/factory.ts", "h_p11", ROUTE_A, None) in served
    assert not [edge for edge in served if edge[3] == "GET /b/status"]


@pytest.mark.parametrize("path", NEGATIVE)
def test_every_negative_case_serves_nothing(indexed: Workspace, path: str) -> None:
    # N1-N23: the handler beside the application is served by no edge at all
    assert not [edge for edge in _served(indexed) if edge[1] == path]


def test_an_application_in_a_repository_with_one_document_is_served_by_nothing(
    indexed: Workspace, settings: Settings, tmp_path: Path
) -> None:
    # N24: an environment read; and a file with swagger-ui and express.static, no oas3-tools
    assert not [edge for edge in _served(indexed) if edge[0] == "lonely"]
    applications = _claim_counts(indexed, OAS3_APP, "lonely")
    assert applications == {"src/lonely_env.ts": 1}
    # the document's own edges are what the base writes for the same repository without the
    # application: the pass adds none and removes none
    base_root = tmp_path / "base-repos"
    make_repo(base_root, "lonely", {k: v for k, v in LONELY.items() if k != "src/lonely_env.ts"})
    with fresh_index(settings, base_root, tmp_path, "base") as base:
        expected = _document_edges(base, "lonely")
    assert expected
    assert _document_edges(indexed, "lonely") == expected


def test_the_other_repositorys_document_is_served_by_no_application_of_cfg(
    indexed: Workspace,
) -> None:
    assert not [edge for edge in _served(indexed) if "/o/" in edge[3]]


# -- why not ----------------------------------------------------------------------------------


def test_each_application_is_served_or_says_why_not_by_a_code(indexed: Workspace) -> None:
    assert _applications(indexed) == APPLICATIONS


def test_the_plan_the_linker_makes_has_the_same_outcomes(indexed: Workspace) -> None:
    plan, _ = _spec_plan(indexed)
    http = _http(indexed)
    assert {claim_id: http.routes[claim_id] for claim_id in plan.outcomes} == plan.outcomes


def test_only_an_application_that_is_not_promoted_has_a_refusal_code(indexed: Workspace) -> None:
    plan, files = _spec_plan(indexed)
    assert {files[claim_id] for claim_id in plan.spec_refusals} == set(NEGATIVE)


def test_a_plain_string_another_module_exports_is_a_path_from_the_working_directory(
    indexed: Workspace,
) -> None:
    # the module offers SPEC_FILE only as a string constant, not as the path of a document;
    # ABS is rooted
    found = _applications(indexed)
    assert found["src/n1b.ts"] == (SPEC_UNREAD, "spec-cwd-relative")
    assert found["src/n21.ts"] == (SPEC_UNREAD, "spec-absolute")


def test_the_refusal_of_a_factory_is_the_first_calls_by_file_path(indexed: Workspace) -> None:
    # src/a_first.ts states a literal; src/b_second.ts, after it, a document not indexed
    assert _applications(indexed)["src/lib/app.ts"] == (SPEC_UNREAD, "spec-dynamic")


def test_a_factory_with_one_served_call_is_promoted_whatever_the_others_say(
    indexed: Workspace,
) -> None:
    # src/m_bad.ts sorts before src/m_good.ts and hands a document that is not indexed
    assert _applications(indexed)["src/lib/app2.ts"] == (PROMOTED, None)
    assert MIXED in _served(indexed)
    assert {edge for edge in _served(indexed) if edge[0] == "order"} == {MIXED}


def test_a_factory_nothing_calls_is_uncalled_and_a_second_level_is_not_followed(
    indexed: Workspace,
) -> None:
    # N17, N16
    found = _applications(indexed)
    assert found["src/n17.ts"] == (SPEC_UNREAD, "spec-parameter-uncalled")
    assert found["src/n16.ts"] == (SPEC_UNREAD, "spec-propagation-depth")


def test_a_path_that_names_no_document_is_not_found_but_every_other_refusal_is_unread(
    indexed: Workspace,
) -> None:
    found = _applications(indexed)
    assert {path for path, (outcome, _) in found.items() if outcome == SPEC_NOT_FOUND} == {
        "src/n11.ts",
        "src/x_missing.ts",
    }


@pytest.mark.parametrize(
    ("directory", "segments", "expected"),
    [
        pytest.param("src", ["../api/a.yaml"], "api/a.yaml", id="normalized"),
        pytest.param("", ["api/a.yaml"], "api/a.yaml", id="the-repository-root"),
        pytest.param("src/p9", ["..", "../api", "a.yaml"], "api/a.yaml", id="several-segments"),
        pytest.param("src", ["/etc/a.yaml"], _Refusal(SPEC_ABSOLUTE), id="rooted"),
        pytest.param("src", ["api\\a.yaml"], _Refusal(SPEC_ABSOLUTE), id="backslash"),
        pytest.param("src", ["C:/a.yaml"], _Refusal(SPEC_ABSOLUTE), id="drive"),
        pytest.param("src", ["..", "/etc"], _Refusal(SPEC_ABSOLUTE), id="absolute-comes-first"),
        pytest.param("src", ["api/"], _Refusal(SPEC_NOT_A_FILE), id="trailing-slash"),
        pytest.param("src", [".."], _Refusal(SPEC_NOT_A_FILE), id="the-parent"),
        pytest.param("src", [], _Refusal(SPEC_NOT_A_FILE), id="no-segment"),
        pytest.param("src", ["../.."], _Refusal(SPEC_NOT_A_FILE), id="not-a-file-before-outside"),
        pytest.param(
            "src", ["../../other/a.yaml"], _Refusal(SPEC_OUTSIDE_REPOSITORY), id="outside"
        ),
    ],
)
def test_a_path_is_checked_absolute_then_not_a_file_then_outside(
    directory: str, segments: list[str], expected: str | _Refusal
) -> None:
    assert _locate(directory, segments) == expected


# -- roots ------------------------------------------------------------------------------------


def test_every_route_keeps_the_file_of_the_application_as_its_root(indexed: Workspace) -> None:
    # P11: src/lib/factory.ts holds the application; src/p11.ts only calls it
    paths = {row[0]: row[1] for row in indexed.database.execute("SELECT id, rel_path FROM files")}
    found: dict[tuple[str, str], list[set[str]]] = defaultdict(list)
    for route in _http(indexed).code_routes:
        file = paths[route.file_id]
        if file.endswith(".ts"):
            found[(file, route.body)].append({paths[root] for root in route.roots})
    expected = {(edge[1], edge[3]): [{edge[1]}] for edge in (*POSITIVE, MIXED)}
    assert dict(found) == expected
    assert found[("src/lib/factory.ts", ROUTE_A)] == [{"src/lib/factory.ts"}]


def test_a_route_is_rooted_at_the_application_not_at_its_handler_or_its_caller(
    workspace: Workspace, tmp_path: Path
) -> None:
    # the handler object (src/handlers.ts), the factory with expressAppConfig (src/lib/app.ts)
    # and the call that hands over the document (src/main.ts) are three different files
    indexed = build(workspace, tmp_path, {"roots": ROOTS})
    route = "GET /r/status"
    assert {edge[1] for edge in _served(indexed) if edge[3] == route} == {"src/handlers.ts"}
    paths = {row[0]: row[1] for row in indexed.database.execute("SELECT id, rel_path FROM files")}
    roots = [
        {paths[root] for root in code_route.roots}
        for code_route in _http(indexed).code_routes
        if code_route.body == route
    ]
    assert roots == [{"src/lib/app.ts"}]


# -- claims -----------------------------------------------------------------------------------


def test_the_fixture_has_the_claims_the_plan_counts(indexed: Workspace) -> None:
    # factory calls: P10 1, P11 1, P12 2, N14 1, N16 1 (refused), N17 0
    assert _claim_counts(indexed, OAS3_FACTORY_CALL) == {
        "src/p10.ts": 1,
        "src/p11.ts": 1,
        "src/p12.ts": 2,
        "src/n14.ts": 1,
        "src/n16.ts": 1,
    }
    assert sum(_claim_counts(indexed, OAS3_FACTORY_CALL).values()) == 6
    assert _claim_counts(indexed, TS_PATH_CONSTANTS) == {"config/paths.ts": 1}
    [claim] = _claims(PATHS_TS, TS_PATH_CONSTANTS, "paths.ts")
    assert list(json.loads(claim.meta["constants"])) == ["SPEC_PATH"]
    assert set(_claim_counts(indexed, OAS3_APP).values()) == {1}


# -- incremental ------------------------------------------------------------------------------


def _moved(edges: set[Edge], path: str, route: str) -> set[Edge]:
    return {(*edge[:3], route, None) if edge[1] == path else edge for edge in edges}


def test_editing_the_path_a_module_exports_moves_the_applications_that_import_it(
    indexed: Workspace, settings: Settings, tmp_path: Path
) -> None:
    # I1: P9 follows config/paths.ts to api/c.yaml
    root = tmp_path / "repos"
    edit(root, "cfg", "config/paths.ts", PATHS_TS.replace("a.yaml", "c.yaml"))
    sync_all(indexed, root)
    assert _cfg(_served(indexed)) == _moved(POSITIVE, "src/p9/app.ts", ROUTE_C)
    assert_equals_fresh(indexed, settings, root, tmp_path, "paths")


def test_editing_the_argument_of_a_caller_moves_the_factory_it_calls(
    indexed: Workspace, settings: Settings, tmp_path: Path
) -> None:
    # I2: P11's caller hands api/c.yaml
    root = tmp_path / "repos"
    edit(root, "cfg", "src/p11.ts", _lines(PATH, IMPORT, f"createApp({C_YAML});"))
    sync_all(indexed, root)
    assert _cfg(_served(indexed)) == _moved(POSITIVE, "src/lib/factory.ts", ROUTE_C)
    assert_equals_fresh(indexed, settings, root, tmp_path, "caller")


def test_deleting_a_module_a_path_segment_is_read_from_leaves_it_unresolved(
    indexed: Workspace, settings: Settings, tmp_path: Path
) -> None:
    # I3: P8 reads SPEC_FILE from config/names.ts
    root = tmp_path / "repos"
    (root / "cfg" / "config" / "names.ts").unlink()
    sync_all(indexed, root)
    assert _cfg(_served(indexed)) == {edge for edge in POSITIVE if edge[1] != "src/p8.ts"}
    assert _applications(indexed)["src/p8.ts"] == (SPEC_UNREAD, "spec-import-unresolved")
    assert_equals_fresh(indexed, settings, root, tmp_path, "names")


# -- limits the pass records, not ones it removes ---------------------------------------------


def test_a_parameter_read_in_a_closure_is_the_parameter_of_the_function_that_holds_it() -> None:
    recorded = config_parser._app(
        "function createApp(spec: string) {",
        f"  const build = () => {config_parser._call('spec')}",
        "  return build();",
        "}",
    )
    assert list(recorded) == ["spec_param", "factory", "factory_exports"]
    assert (recorded["spec_param"], recorded["factory"]) == ("0", "createApp")


def test_a_js_factory_with_a_default_parameter_reads_as_dynamic() -> None:
    # a limit, recorded: the JavaScript grammar writes a default as an assignment pattern, which
    # no binding of a parameter covers, so the name is not found where it is bound
    source = config_parser._source(
        f"function createApp(spec = {A_YAML}) {{ return {config_parser._call('spec')} }}",
        "createApp();",
    )
    [claim] = _claims(source, OAS3_APP, "app.js")
    assert claim.meta["spec_refused"] == "spec-dynamic"
