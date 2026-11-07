"""An oas3-tools factory that joins its parameter to its own directory, built elsewhere (M7, L1).

``createApp(specPath)`` serves ``path.join(__dirname, specPath)``: the caller hands a string,
which the factory joins to the directory its own module runs from. That directory is the
source's, or the one a tsconfig ``outDir``/``rootDir`` pair compiles it to; a path the two
layouts read to two different documents names none. The controllers are default exports,
imported by default. A call in a test file proves nothing about the application.

The fixture is neutral: one service, a generated document, two default-exported handler
objects, a production entry and an integration test that each call the factory.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from repoatlas.graph.linker import ReferenceLinker
from repoatlas.graph.ts_oas3 import (
    CONTROLLERS_UNREAD,
    CONTROLLERS_UNRESOLVED,
    PROMOTED,
    SPEC_NOT_FOUND,
    SPEC_UNREAD,
)
from repoatlas.models.http_inventory import HttpOperation
from repoatlas.parsers.http_facts import OAS3_APP, OAS3_FACTORY_CALL
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build
from tests.r1_provider_corpus import inventory, providers
from tests.test_http_oas3 import _claims, d

SPEC = "prisma/generated/openapi/openapi.yaml"

DOCUMENT = d("""
    openapi: 3.0.0
    info: {title: widgets, version: '1.0'}
    paths:
      /status:
        get:
          operationId: getStatus
          responses: {'200': {description: up}}
      /widgets:
        get:
          operationId: getAllWidgets
          responses: {'200': {description: all}}
      /widgets/{id}:
        get:
          operationId: getWidgetById
          responses: {'200': {description: one}}
      /orphans:
        get:
          operationId: nobodyHandlesThis
          responses: {'200': {description: none}}
""")

APP = d("""
    import * as express from 'express'
    import path from 'path'
    import { expressAppConfig } from 'oas3-tools'
    import generatedController from './generated/handler/handler'
    import statusController from './controllers/status'

    export function createApp(specPath: string): express.Application {
      const controllers = { ...generatedController, ...statusController }
      const options = { routing: { controllers: controllers } }
      const appConfig = expressAppConfig(path.join(__dirname, specPath), options as any)
      return appConfig.getApp()
    }
""")

MAIN = d(f"""
    import {{ createApp }} from './app'

    const OPENAPI_SPEC_PATH = '../../{SPEC}'

    function main(): void {{
      createApp(OPENAPI_SPEC_PATH)
    }}
    main()
""")

HARNESS = d(f"""
    import {{ createApp }} from '../app'

    const OPENAPI_SPEC_PATH = '../{SPEC}'

    test('serves', () => {{
      createApp(OPENAPI_SPEC_PATH)
    }})
""")

HANDLER = d("""
    import { Request, Response } from 'express'

    const controllers = {
      getAllWidgets: async (req: Request, res: Response): Promise<void> => { res.json([]) },
      getWidgetById: async (req: Request, res: Response): Promise<void> => { res.json({}) },
    }

    export default controllers
""")

STATUS = d("""
    const controllers = {
      getStatus: (req, res): void => { res.json('OK') },
    }

    export default controllers
""")

TSCONFIG = json.dumps({"include": ["src/**/*"], "compilerOptions": {"outDir": "./build"}})
RELEASE = json.dumps({"extends": "./tsconfig.json", "compilerOptions": {"rootDir": "."}})

SERVICE: dict[str, str] = {
    "package.json": json.dumps({"name": "widgets", "scripts": {"start": "node build/src/main.js"}}),
    "tsconfig.json": TSCONFIG,
    "tsconfig.release.json": RELEASE,
    SPEC: DOCUMENT,
    "src/app.ts": APP,
    "src/main.ts": MAIN,
    "src/__tests__/integrationTests.ts": HARNESS,
    "src/generated/handler/handler.ts": HANDLER,
    "src/controllers/status.ts": STATUS,
}

HANDLER_FILE = "src/generated/handler/handler.ts"
STATUS_FILE = "src/controllers/status.ts"
EXECUTABLE = {
    (HANDLER_FILE, "GET /widgets"),
    (HANDLER_FILE, "GET /widgets/{id}"),
    (STATUS_FILE, "GET /status"),
}


def _variant(drop: tuple[str, ...] = (), **files: str) -> dict[str, str]:
    """The service without the files ``drop`` names, with ``files`` (``a__b`` is ``a/b``)."""
    found = {path: text for path, text in SERVICE.items() if path not in drop}
    found.update({path.replace("__", "/"): text for path, text in files.items()})
    return found


def _index(workspace: Workspace, tmp_path: Path, files: dict[str, str]) -> Workspace:
    return build(workspace, tmp_path, {"widgets": files})


def _rows(workspace: Workspace) -> list[HttpOperation]:
    rows, _ = inventory(workspace, "widgets", include_diagnostics=True)
    return providers(rows)


def _executable(workspace: Workspace) -> set[tuple[str, str]]:
    """``(file, route)`` of every route a handler object serves, join-eligible or not."""
    return {
        (row.path or "", f"{row.method} {row.route}")
        for row in _rows(workspace)
        if row.path and row.path.endswith(".ts") and row.status.value != "unpublished"
    }


def _applications(workspace: Workspace) -> dict[str, tuple[str, str | None]]:
    """Each application's outcome and refusal code, as the global pass decides them."""
    plan = ReferenceLinker(workspace.knowledge).prepare_global().http
    rows = workspace.database.execute(
        "SELECT u.id, f.rel_path FROM unresolved_refs u JOIN files f ON f.id = u.file_id "
        "WHERE u.meta_json LIKE ?",
        (f'%"{OAS3_APP}"%',),
    ).fetchall()
    return {path: (plan.routes[claim], plan.spec_refusals.get(claim)) for claim, path in rows}


def _application(workspace: Workspace) -> tuple[str, str | None]:
    return _applications(workspace)["src/app.ts"]


# -- the parser ----------------------------------------------------------------------------------


def test_the_factory_records_the_parameter_and_the_segments_before_it() -> None:
    [claim] = _claims(APP, OAS3_APP, "app.ts")
    assert claim.meta["spec_param"] == "0"
    assert claim.meta["factory"] == "createApp"
    assert json.loads(claim.meta["factory_exports"]) == ["createApp"]
    assert json.loads(claim.meta["spec_param_dir"]) == []
    assert "spec_refused" not in claim.meta


def test_literal_segments_before_the_parameter_are_recorded_in_order() -> None:
    source = APP.replace("path.join(__dirname, specPath)", "path.join(__dirname, '..', specPath)")
    [claim] = _claims(source, OAS3_APP, "app.ts")
    assert json.loads(claim.meta["spec_param_dir"]) == [".."]


def test_a_whole_parameter_keeps_its_claim_without_a_directory() -> None:
    source = APP.replace("path.join(__dirname, specPath)", "specPath")
    [claim] = _claims(source, OAS3_APP, "app.ts")
    assert claim.meta["spec_param"] == "0" and "spec_param_dir" not in claim.meta


def test_default_imported_controllers_are_recorded_as_their_modules_default_export() -> None:
    [claim] = _claims(APP, OAS3_APP, "app.ts")
    assert json.loads(claim.meta["controllers"]) == [
        {"module": "./generated/handler/handler", "name": "default"},
        {"module": "./controllers/status", "name": "default"},
    ]


@pytest.mark.parametrize(
    ("imports", "spread", "recorded"),
    [
        pytest.param(
            "import { handlers } from './handlers'",
            "...handlers",
            [{"module": "./handlers", "name": "handlers"}],
            id="named",
        ),
        pytest.param(
            "import { default as handlers } from './handlers'",
            "...handlers",
            [{"module": "./handlers", "name": "default"}],
            id="named-default",
        ),
        pytest.param("import handlers from 'handlers-package'", "...handlers", None, id="package"),
        pytest.param("import * as handlers from './handlers'", "...handlers", None, id="namespace"),
        pytest.param(
            "import handlers from './handlers'\nconst handlers2 = 1",
            "...handlers, ...other",
            None,
            id="one-unread-spread",
        ),
        pytest.param("import type handlers from './handlers'", "...handlers", None, id="type-only"),
    ],
)
def test_only_relative_controllers_are_read(
    imports: str, spread: str, recorded: list[dict[str, str]] | None
) -> None:
    options = f"{{ routing: {{ controllers: {{ {spread} }} }} }}"
    source = d(f"""
        import path from 'path'
        import {{ expressAppConfig }} from 'oas3-tools'
        {imports}
        expressAppConfig(path.join(__dirname, 'a.yaml'), {options})
    """)
    [claim] = _claims(source, OAS3_APP, "app.ts")
    found = json.loads(claim.meta["controllers"]) if "controllers" in claim.meta else None
    assert found == recorded


def test_the_caller_records_the_string_it_hands_the_factory() -> None:
    [claim] = _claims(MAIN, OAS3_FACTORY_CALL, "main.ts")
    assert (claim.meta["module"], claim.meta["name"]) == ("./app", "createApp")
    assert json.loads(claim.meta["arguments"]) == [
        {"refused": "spec-cwd-relative", "segment": f"../../{SPEC}"}
    ]


@pytest.mark.parametrize(
    ("argument", "segment"),
    [
        pytest.param("'a.yaml'", "a.yaml", id="literal"),
        pytest.param("SPEC", "x/a.yaml", id="const"),
        pytest.param("ALIAS", "x/a.yaml", id="const-alias"),
        pytest.param("'x/' + 'a.yaml'", "x/a.yaml", id="literal-concatenation"),
        pytest.param("LATE", None, id="const-declared-after"),
        pytest.param("MUTABLE", None, id="let"),
        pytest.param("`${SPEC}`", None, id="template-with-a-substitution"),
        pytest.param("process.env.SPEC", None, id="environment"),
    ],
)
def test_a_segment_is_a_literal_or_a_const_bound_to_one(argument: str, segment: str | None) -> None:
    source = d(f"""
        import path from 'path'
        import {{ expressAppConfig }} from 'oas3-tools'
        const h = {{ getStatus: (req, res) => res.end() }}
        const SPEC = 'x/a.yaml'
        const ALIAS = SPEC
        let MUTABLE = 'x/a.yaml'
        export function createApp(spec: string) {{
          return expressAppConfig(path.join(__dirname, spec), {{ routing: {{ controllers: h }} }})
        }}
        createApp({argument})
        const LATE = 'x/a.yaml'
    """)
    [claim] = _claims(source, OAS3_FACTORY_CALL, "app.ts")
    [entry] = json.loads(claim.meta["arguments"])
    assert entry.get("segment") == segment


# -- the service, end to end ---------------------------------------------------------------------


@pytest.fixture
def service(workspace: Workspace, tmp_path: Path) -> Workspace:
    return _index(workspace, tmp_path, SERVICE)


def test_the_production_entry_serves_each_operation_a_controller_handles(
    service: Workspace,
) -> None:
    assert _application(service) == (PROMOTED, None)
    assert _executable(service) == EXECUTABLE


def test_each_executable_route_is_join_eligible_and_cited_at_its_handler_object(
    service: Workspace,
) -> None:
    rows = [r for r in _rows(service) if r.path and r.path.endswith(".ts")]
    assert rows
    for row in rows:
        diagnostic = row.diagnostic
        assert diagnostic is not None
        assert diagnostic.published and diagnostic.join_eligible, row
        assert diagnostic.framework == "oas3-tools"
        assert diagnostic.join_limits == ()


def test_the_document_s_own_routes_stay_published_and_not_join_eligible(
    service: Workspace,
) -> None:
    documented = [r for r in _rows(service) if r.path == SPEC]
    assert {f"{r.method} {r.route}" for r in documented} == {
        "GET /status",
        "GET /widgets",
        "GET /widgets/{id}",
        "GET /orphans",
    }
    for row in documented:
        assert row.diagnostic is not None
        assert row.diagnostic.published and not row.diagnostic.join_eligible
        assert row.diagnostic.join_limits == ("openapi-document",)


def test_an_operation_no_controller_handles_is_served_only_by_the_document(
    service: Workspace,
) -> None:
    assert not any(route == "GET /orphans" for _, route in _executable(service))


def test_the_production_entry_alone_serves_without_the_test_harness(
    workspace: Workspace, tmp_path: Path
) -> None:
    indexed = _index(workspace, tmp_path, _variant(drop=("src/__tests__/integrationTests.ts",)))
    assert _application(indexed) == (PROMOTED, None)
    assert _executable(indexed) == EXECUTABLE


def test_a_test_harness_alone_proves_nothing(workspace: Workspace, tmp_path: Path) -> None:
    indexed = _index(workspace, tmp_path, _variant(drop=("src/main.ts",)))
    assert _application(indexed) == (SPEC_UNREAD, "spec-parameter-test-only")
    assert _executable(indexed) == set()


def test_a_factory_in_a_test_file_is_served_by_its_test_callers(
    workspace: Workspace, tmp_path: Path
) -> None:
    files = {
        "package.json": json.dumps({"name": "widgets"}),
        SPEC: DOCUMENT,
        "src/__tests__/app.ts": APP.replace("'./", "'../"),
        "src/__tests__/run.test.ts": HARNESS.replace("'../app'", "'./app'").replace(
            f"'../{SPEC}'", f"'../../{SPEC}'"
        ),
        HANDLER_FILE: HANDLER,
        STATUS_FILE: STATUS,
    }
    indexed = _index(workspace, tmp_path, files)
    assert _applications(indexed)["src/__tests__/app.ts"] == (PROMOTED, None)


@pytest.mark.parametrize(
    "drop",
    [
        pytest.param(("tsconfig.release.json",), id="out-dir-without-a-root-dir"),
        pytest.param(("tsconfig.json", "tsconfig.release.json"), id="no-tsconfig"),
    ],
)
def test_without_a_declared_layout_the_entry_runs_from_its_source(
    workspace: Workspace, tmp_path: Path, drop: tuple[str, ...]
) -> None:
    # read from src/, ``../../`` leaves the repository; the harness proves nothing
    indexed = _index(workspace, tmp_path, _variant(drop=drop))
    assert _application(indexed) == (SPEC_NOT_FOUND, "spec-outside-repository")
    assert _executable(indexed) == set()


def test_a_layout_whose_root_does_not_hold_the_factory_is_not_read(
    workspace: Workspace, tmp_path: Path
) -> None:
    release = json.dumps({"compilerOptions": {"rootDir": "lib", "outDir": "build"}})
    indexed = _index(workspace, tmp_path, _variant(**{"tsconfig.release.json": release}))
    assert _application(indexed) == (SPEC_NOT_FOUND, "spec-outside-repository")


def test_a_source_and_a_build_layout_reading_one_document_serve_it_once(
    workspace: Workspace, tmp_path: Path
) -> None:
    # src/ and dist/ are equally deep: both read ../prisma/... to the one document
    release = json.dumps({"compilerOptions": {"rootDir": "src", "outDir": "dist"}})
    main = MAIN.replace(f"'../../{SPEC}'", f"'../{SPEC}'")
    files = _variant(**{"tsconfig.release.json": release, "src__main.ts": main})
    indexed = _index(workspace, tmp_path, files)
    assert _application(indexed) == (PROMOTED, None)
    executable = [
        (r.path, f"{r.method} {r.route}")
        for r in _rows(indexed)
        if r.path and r.path.endswith(".ts")
    ]
    assert sorted(executable) == sorted(EXECUTABLE)


def test_two_layouts_reading_two_documents_name_none(workspace: Workspace, tmp_path: Path) -> None:
    # ``../openapi.yaml`` is openapi.yaml from src/, runtime/openapi.yaml from runtime/src/
    release = json.dumps({"compilerOptions": {"rootDir": ".", "outDir": "runtime"}})
    main = MAIN.replace(f"'../../{SPEC}'", "'../openapi.yaml'")
    files = _variant(
        drop=("src/__tests__/integrationTests.ts",),
        **{
            "tsconfig.release.json": release,
            "src__main.ts": main,
            "openapi.yaml": DOCUMENT,
            "runtime__openapi.yaml": DOCUMENT,
        },
    )
    indexed = _index(workspace, tmp_path, files)
    assert _application(indexed) == (SPEC_NOT_FOUND, "spec-layout-ambiguous")
    assert _executable(indexed) == set()


@pytest.mark.parametrize(
    ("argument", "code"),
    [
        pytest.param(f"path.join(__dirname, '../../{SPEC}')", "spec-absolute", id="anchored-path"),
        pytest.param("'/etc/openapi.yaml'", "spec-absolute", id="absolute-segment"),
        pytest.param("'../../../../openapi.yaml'", "spec-outside-repository", id="escapes"),
        pytest.param("'../../missing.yaml'", "spec-not-indexed", id="missing-document"),
    ],
)
def test_a_segment_that_names_no_document_says_why(
    workspace: Workspace, tmp_path: Path, argument: str, code: str
) -> None:
    main = d(f"""
        import path from 'path'
        import {{ createApp }} from './app'
        const OPENAPI_SPEC_PATH = {argument}
        createApp(OPENAPI_SPEC_PATH)
    """)
    files = _variant(drop=("src/__tests__/integrationTests.ts",), **{"src__main.ts": main})
    indexed = _index(workspace, tmp_path, files)
    assert _application(indexed)[1] == code
    assert _executable(indexed) == set()


@pytest.mark.parametrize(
    ("files", "outcome"),
    [
        pytest.param(
            {"src__controllers__status.ts": STATUS.replace("export default controllers", "")},
            CONTROLLERS_UNRESOLVED,
            id="no-default-export",
        ),
        pytest.param(
            {
                "src__controllers__status.ts": STATUS.replace(
                    "export default controllers", "export { controllers }"
                )
            },
            CONTROLLERS_UNRESOLVED,
            id="only-a-named-export",
        ),
        pytest.param(
            {
                "src__controllers__status.ts": STATUS.replace(
                    "getStatus: (req, res): void => { res.json('OK') },",
                    "getStatus: (req, res): void => { res.json('OK') },\n  name: 'x',",
                )
            },
            CONTROLLERS_UNRESOLVED,
            id="not-a-handler-object",
        ),
        pytest.param(
            {
                "src__app.ts": APP.replace(
                    "from './controllers/status'", "from 'status-controllers'"
                )
            },
            CONTROLLERS_UNREAD,
            id="a-package-default-import",
        ),
    ],
)
def test_a_controller_link_that_is_not_proven_serves_nothing(
    workspace: Workspace, tmp_path: Path, files: dict[str, str], outcome: str
) -> None:
    indexed = _index(workspace, tmp_path, _variant(**files))
    assert _application(indexed) == (outcome, None)
    assert _executable(indexed) == set()


def test_a_default_imported_factory_is_still_not_followed(
    workspace: Workspace, tmp_path: Path
) -> None:
    main = MAIN.replace("import { createApp } from './app'", "import createApp from './app'")
    app = APP + "export default createApp\n"
    files = _variant(
        drop=("src/__tests__/integrationTests.ts",), **{"src__main.ts": main, "src__app.ts": app}
    )
    indexed = _index(workspace, tmp_path, files)
    assert _application(indexed) == (SPEC_UNREAD, "spec-parameter-uncalled")
    assert _executable(indexed) == set()
