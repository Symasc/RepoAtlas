"""Requests made through a request helper imported from another file (HTTP linkage).

``learning-client.ts`` imports ``createRequester`` and ``jsonBody`` from
``./http`` and calls ``request(schema, "/v1/courses", { ...jsonBody(input) })``.
No single file proves that is ``POST /v1/courses``: the helper file declares what
its exported functions do, the client file records the call, and the global pass
joins them across exactly one import. These tests run the real sync pipeline and
read the stored graph — including what must stay unresolved.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from repoatlas.graph.linker import ReferenceLinker
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace
from tests.conftest import fact_snapshot, git, init_git_repository, reindex_from_scratch
from tests.http_corpus import corpus_settings, http_nodes

HTTP = """\
export type ClientOptions = { baseUrl: string; fetch?: FetchLike };

export function createRequester({ baseUrl, fetch: suppliedFetch }: ClientOptions) {
  const runtimeFetch = (globalThis as { fetch?: FetchLike }).fetch;
  const fetcher = suppliedFetch ?? runtimeFetch;
  const requestFetch: FetchLike = fetcher;
  const normalizedBaseUrl = baseUrl.replace(/\\/$/, "");

  async function send(path: string, init?: RequestInit): Promise<unknown> {
    const response = await requestFetch(`${normalizedBaseUrl}${path}`, {
      ...init,
      headers: { Accept: "application/json", ...init?.headers },
    });
    return response.json();
  }

  return {
    request: async (schema: Schema, path: string, init?: RequestInit) =>
      schema.parse(await send(path, init)),
    requestVoid: async (path: string, init?: RequestInit) => {
      await send(path, init);
    },
  };
}

export function jsonBody(value: unknown, method: "POST" | "PUT" | "PATCH" = "{default}") {
  return { method, body: JSON.stringify(value) };
}

export function authHeaders(token: string) {
  return { Authorization: `Bearer ${token}` };
}
"""

CLIENT = """\
import { authHeaders, createRequester, jsonBody } from "./http";

export function createLearningClient(options: ClientOptions) {
  const { request, requestVoid } = createRequester(options);

  return {
    getPreferences: (auth: string) =>
      request(preferencesSchema, "/v1/me/preferences", { headers: authHeaders(auth) }),

    updatePreferences: (auth: string, input: Preferences) =>
      request(preferencesSchema, "/v1/me/preferences", {
        ...jsonBody(input, "PUT"),
        headers: authHeaders(auth),
      }),

    createCourse: (auth: string, input: CreateCourseRequest) =>
      request(createdCourseSchema, "/v1/courses", {
        ...jsonBody(input),
        headers: authHeaders(auth),
      }),

    getOrganization: (auth: string, organizationId: string) =>
      request(organizationSchema, `/v1/organizations/${organizationId}`, {
        headers: authHeaders(auth),
      }),

    removeCurrentCourse: (auth: string) =>
      request(courseSchema, "/v1/courses/current", { method: "DELETE" }),

    leave: () => requestVoid("/v1/me/membership", { method: "DELETE" }),
  };
}

export const VERSION = 1;
"""

PROVEN = {
    ("createLearningClient.getPreferences", "http-route:GET:/v1/me/preferences"),
    ("createLearningClient.updatePreferences", "http-route:PUT:/v1/me/preferences"),
    ("createLearningClient.createCourse", "http-route:POST:/v1/courses"),
    ("createLearningClient.removeCurrentCourse", "http-route:DELETE:/v1/courses/current"),
    ("createLearningClient.leave", "http-route:DELETE:/v1/me/membership"),
}


def _index(tmp_path: Path, files: dict[str, str]) -> tuple[Workspace, Path]:
    from repoatlas.services.init_service import initialize

    root = tmp_path / "repos"
    for name, text in files.items():
        (root / "web" / name).parent.mkdir(parents=True, exist_ok=True)
        (root / "web" / name).write_text(text)
    init_git_repository(root / "web")
    workspace = open_workspace(settings=corpus_settings(tmp_path / "state"), require_root=False)
    initialize(workspace, root, config_path=tmp_path / "config.yaml")
    return workspace, root


Index = Callable[..., tuple[Workspace, Path]]


@pytest.fixture
def index(tmp_path: Path) -> Iterator[Index]:
    """``_index`` for one test; every workspace it opens is closed when the test ends."""
    opened: list[Workspace] = []

    def make(*args: Any, **kwargs: Any) -> tuple[Workspace, Path]:
        indexed = _index(tmp_path, *args, **kwargs)
        opened.append(indexed[0])
        return indexed

    try:
        yield make
    finally:
        for workspace in opened:
            workspace.close()


def _learning(index: Index, default: str = "POST", **extra: str) -> tuple[Workspace, Path]:
    return index(
        {
            "src/http.ts": HTTP.replace("{default}", default),
            "src/learning-client.ts": CLIENT,
            **extra,
        },
    )


def _calls(workspace: Workspace) -> set[tuple[str, str]]:
    rows = workspace.database.execute(
        "SELECT src.qualified_name AS caller, dst.key AS route FROM edges e "
        "JOIN entities src ON src.id = e.src_id JOIN entities dst ON dst.id = e.dst_id "
        "WHERE e.type = 'CALLS_HTTP_ROUTE'"
    ).fetchall()
    return {(row["caller"], row["route"]) for row in rows}


def _outcomes(workspace: Workspace) -> dict[str, str]:
    plan = ReferenceLinker(workspace.knowledge).prepare_global()
    rows = workspace.database.execute(
        "SELECT u.id, src.qualified_name AS caller FROM unresolved_refs u "
        "JOIN entities src ON src.id = u.src_id"
    ).fetchall()
    callers = {row["id"]: row["caller"] for row in rows}
    return {callers[claim]: outcome for claim, outcome in plan.http.requests.items()}


def _commit_and_sync(workspace: Workspace, root: Path, message: str) -> None:
    git(root / "web", "add", "-A")
    git(root / "web", "commit", "--quiet", "-m", message)
    sync(workspace, workspace.resolver().resolve(root))


def test_an_imported_requester_factory_is_read_across_one_import(index: Index) -> None:
    workspace, _ = _learning(index)
    assert _calls(workspace) == PROVEN
    # The injected base names no deployment and no endpoint.
    assert all(key.startswith("http-route:") for key in http_nodes(workspace))


def test_where_each_deciding_literal_is_written_decides_citability(index: Index) -> None:
    workspace, _ = _learning(index)
    rows = workspace.database.execute(
        "SELECT src.qualified_name AS caller, e.line_start, e.line_end, e.parser, f.rel_path "
        "FROM edges e JOIN entities src ON src.id = e.src_id JOIN files f ON f.id = e.file_id "
        "WHERE e.type = 'CALLS_HTTP_ROUTE'"
    ).fetchall()
    lines = {row["caller"]: (row["line_start"], row["line_end"]) for row in rows}
    assert {row["rel_path"] for row in rows} == {"src/learning-client.ts"}
    assert {row["parser"] for row in rows} == {"linker:global"}
    # "PUT" is in the call.
    assert lines["createLearningClient.updatePreferences"] == (11, 14)
    # POST is the helper's default, written in another file: uncitable here.
    assert lines["createLearningClient.createCourse"] == (None, None)


def test_a_template_path_stays_unresolved_and_is_counted(index: Index) -> None:
    workspace, _ = _learning(index)
    assert all(caller != "createLearningClient.getOrganization" for caller, _ in _calls(workspace))
    assert _outcomes(workspace)["createLearningClient.getOrganization"] == (
        "the path is not a literal"
    )


def test_an_imported_wrapper_function_is_read_too(index: Index) -> None:
    workspace, _ = index(
        {
            "src/fetcher.ts": (
                "export async function request(path: string, init?: RequestInit) {\n"
                "  return fetch(path, init);\n"
                "}\n"
            ),
            "src/client.ts": (
                'import { request } from "./fetcher";\n'
                "export function load() {\n"
                '  return request("/v1/direct", { method: "PATCH" });\n'
                "}\n"
            ),
        },
    )
    assert _calls(workspace) == {("load", "http-route:PATCH:/v1/direct")}


def test_only_the_imported_module_is_read_never_a_same_named_one(index: Index) -> None:
    impostor = (
        "export function createRequester() {\n"
        "  return { request: (schema: Schema, path: string) => database.read(path) };\n"
        "}\n"
    )
    workspace, _ = index(
        {
            # A real requester elsewhere, and an impostor where the import points.
            "src/http.ts": HTTP.replace("{default}", "POST"),
            "src/fake/http.ts": impostor,
            "src/fake/client.ts": (
                'import { createRequester } from "./http";\n'
                "export function make(options: Options) {\n"
                "  const { request } = createRequester(options);\n"
                '  return { load: () => request(schema, "/v1/me") };\n'
                "}\n"
            ),
        },
    )
    assert _calls(workspace) == set()


def test_a_re_export_a_package_and_an_unresolved_import_prove_nothing(index: Index) -> None:
    def client(factory: str, specifier: str) -> str:
        return (
            f'import {{ createRequester }} from "{specifier}";\n'
            f"export function {factory}(options: Options) {{\n"
            "  const { request } = createRequester(options);\n"
            '  return { load: () => request(schema, "/v1/me") };\n'
            "}\n"
        )

    workspace, _ = index(
        {
            "src/http.ts": HTTP.replace("{default}", "POST"),
            "src/index.ts": 'export { createRequester } from "./http";\n',
            "src/via-index.ts": client("viaIndex", "./index"),
            "src/via-package.ts": client("viaPackage", "@acme/http"),
            "src/via-missing.ts": client("viaMissing", "./missing"),
        },
    )
    assert _calls(workspace) == set()
    # One import is read, never a re-export behind it; a package is never recorded;
    # a module that does not resolve leaves the claim unresolved.
    assert _outcomes(workspace) == {
        "viaIndex.load": "no request helper of that name is declared in the imported module",
        "viaMissing.load": "import does not resolve",
    }


def test_a_cycle_between_the_two_modules_is_still_one_hop(index: Index) -> None:
    looped = HTTP.replace("{default}", "POST") + (
        'import { VERSION } from "./learning-client";\nexport const SEEN = VERSION;\n'
    )
    workspace, _ = index({"src/http.ts": looped, "src/learning-client.ts": CLIENT})
    assert _calls(workspace) == PROVEN


def test_changing_the_helper_file_moves_the_client_requests(index: Index, tmp_path: Path) -> None:
    workspace, root = _learning(index)

    # The helper's default is written in the helper's file; the client did not change.
    (root / "web/src/http.ts").write_text(HTTP.replace("{default}", "PATCH"))
    _commit_and_sync(workspace, root, "change the default")
    assert ("createLearningClient.createCourse", "http-route:PATCH:/v1/courses") in (
        _calls(workspace)
    )
    assert "http-route:POST:/v1/courses" not in http_nodes(workspace)
    fresh = reindex_from_scratch(corpus_settings(tmp_path / "unused"), root, tmp_path, "patched")
    assert fact_snapshot(workspace) == fresh

    # A member that is no longer a request member takes its requests with it.
    (root / "web/src/http.ts").write_text(
        HTTP.replace("{default}", "PATCH").replace(
            "      await send(path, init);\n", "      await audit(path, init);\n"
        )
    )
    _commit_and_sync(workspace, root, "requestVoid no longer sends")
    assert all(caller != "createLearningClient.leave" for caller, _ in _calls(workspace))

    git(root / "web", "rm", "--quiet", "src/http.ts")
    _commit_and_sync(workspace, root, "remove the helper")
    assert _calls(workspace) == set()
    assert http_nodes(workspace) == set()
    fresh = reindex_from_scratch(corpus_settings(tmp_path / "unused"), root, tmp_path, "removed")
    assert fact_snapshot(workspace) == fresh


def test_a_declaration_is_not_an_unresolved_reference(index: Index) -> None:
    workspace, _ = _learning(index)
    rows = workspace.database.execute("SELECT meta_json FROM unresolved_refs").fetchall()
    roles = [json.loads(row["meta_json"] or "{}").get("pending_fact") for row in rows]
    assert roles.count("http-requester") == 1
    assert roles.count("http-init-helper") == 1
    assert roles.count("http-requester-call") == 6
    # Five calls are proven and the template-path call is not; the declarations are
    # inputs to that decision and are counted as neither.
    decided = ReferenceLinker(workspace.knowledge).prepare_global().http
    assert (decided.resolved, decided.unresolved) == (5, 1)
