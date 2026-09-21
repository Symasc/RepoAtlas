"""Methods a factory returns as function-valued properties (TypeScript structure).

``return { createCourse: async (auth, input) => request(…) }`` defines a method of
the factory as surely as ``return { async createCourse(auth, input) { … } }``
does, and the second form was always indexed. These tests pin the first form to
the same identity rules — and pin what stays unchanged: every other object
property, every object that is not returned, and every existing method.
"""

from __future__ import annotations

import textwrap
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from repoatlas.models.parsing import EdgeType, EntityType, ParsedFile, ParsedSymbol
from repoatlas.parsers.typescript import TypeScriptParser
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace
from tests.conftest import fact_snapshot, git, init_git_repository, reindex_from_scratch
from tests.http_corpus import corpus_settings

LEARNING = """\
import { authHeaders, createRequester, jsonBody } from "./http";

export function createLearningClient(options: ClientOptions) {
  const { request } = createRequester(options);

  return {
    getPreferences: (auth: CallAuth): Promise<Preferences> =>
      request(preferencesSchema, "/v1/me/preferences", { headers: authHeaders(auth) }),

    createCourse: async (auth: CallAuth, input: CreateCourseRequest) =>
      request(createdCourseSchema, "/v1/courses", {
        ...jsonBody(input),
        headers: authHeaders(auth),
      }),
  };
}
"""


def _parse(source: str, name: str = "client.ts") -> ParsedFile:
    return TypeScriptParser().parse(Path(name), textwrap.dedent(source))


def _methods(source: str) -> dict[str, ParsedSymbol]:
    return {s.qualified_name: s for s in _parse(source).symbols if s.kind is EntityType.METHOD}


def test_returned_arrow_properties_are_methods_of_the_factory() -> None:
    methods = _methods(LEARNING)
    assert set(methods) == {
        "createLearningClient.getPreferences",
        "createLearningClient.createCourse",
    }
    create = methods["createLearningClient.createCourse"]
    assert (create.name, create.parent) == ("createCourse", "createLearningClient")
    # The span is the whole property: key, parameters and body.
    assert (create.line_start, create.line_end) == (10, 14)
    assert create.signature == ("method createCourse(auth: CallAuth, input: CreateCourseRequest)")
    preferences = methods["createLearningClient.getPreferences"]
    assert (preferences.line_start, preferences.line_end) == (7, 8)


def test_calls_inside_a_returned_method_belong_to_that_method() -> None:
    calls = {
        (r.target_key, r.source_symbol)
        for r in _parse(LEARNING).references
        if r.edge_type is EdgeType.CALLS
    }
    assert ("request", "createLearningClient.createCourse") in calls
    assert ("jsonBody", "createLearningClient.createCourse") in calls
    assert ("request", "createLearningClient.getPreferences") in calls
    # The factory keeps the call it makes itself.
    assert ("createRequester", "createLearningClient") in calls


def test_every_function_value_form_is_a_method() -> None:
    source = """\
    export function make() {
      return {
        arrow: (x: number) => x,
        bare: x => x,
        asynchronous: async () => 1,
        expression: function () { return 1; },
        named: function inner(y: string) { return y; },
        "quoted": () => 2,
        async shorthand(z: number) { return z; },
      };
    }
    """
    methods = _methods(source)
    assert set(methods) == {
        "make.arrow",
        "make.bare",
        "make.asynchronous",
        "make.expression",
        "make.named",
        "make.quoted",
        "make.shorthand",
    }
    assert methods["make.bare"].signature == "method bare(x)"
    assert methods["make.named"].name == "named"


def test_nested_returned_objects_keep_their_ownership() -> None:
    source = """\
    export function createClient() {
      return {
        courses: {
          list: () => 1,
          detail: (id: string) => id,
        },
        list: () => 2,
      };
    }
    """
    assert set(_methods(source)) == {
        "createClient.courses.list",
        "createClient.courses.detail",
        "createClient.list",
    }
    assert _methods(source)["createClient.courses.list"].parent == "createClient.courses"


def test_the_same_property_in_two_factories_is_two_methods() -> None:
    source = """\
    export function createIdentityClient() {
      return { listCourses: () => 1 };
    }
    export function createLearningClient() {
      return { listCourses: () => 2 };
    }
    """
    methods = _methods(source)
    assert set(methods) == {
        "createIdentityClient.listCourses",
        "createLearningClient.listCourses",
    }
    assert {m.line_start for m in methods.values()} == {2, 5}


def test_expression_bodied_factories_and_class_methods_return_objects_too() -> None:
    source = """\
    export const makeClient = (options: Options) => ({
      ping: () => options,
    });
    class Registry {
      handlers() {
        return { run: () => 1 };
      }
    }
    """
    assert set(_methods(source)) == {
        "makeClient.ping",
        "Registry.handlers",
        "Registry.handlers.run",
    }


def test_properties_that_are_not_readable_methods_are_left_alone() -> None:
    source = """\
    export function make(options: Options, name: string) {
      return {
        [name]: () => 1,
        ["computed"]: () => 2,
        baseUrl: options.baseUrl,
        count: 1,
        config: { retries: 3 },
        dup: () => 1,
        dup: () => 2,
        ...options.extra,
      };
    }
    """
    assert _methods(source) == {}


def test_objects_that_are_not_returned_by_a_named_function_define_nothing() -> None:
    source = """\
    export function make() {
      const client = { list: () => 1 };
      register({ handler: () => 2 });
      const items = rows.map((row) => ({ run: () => row }));
      const inner = function () {
        return { hidden: () => 3 };
      };
      return client;
    }
    const table = { top: () => 4 };
    """
    # `inner` is a named function value, so what *it* returns is its own.
    assert set(_methods(source)) == {"make.inner.hidden"}


def test_existing_classes_and_object_methods_keep_their_identities() -> None:
    source = """\
    export class Service {
      detail(id: string) {
        return { id };
      }
    }
    export function createIdentityClient() {
      async function request(path: string) {
        return fetch(path);
      }
      return {
        async me() {
          return request("/v1/me");
        },
      };
    }
    """
    symbols = {(s.kind, s.qualified_name, s.line_start, s.line_end) for s in _parse(source).symbols}
    assert (EntityType.METHOD, "Service.detail", 2, 4) in symbols
    assert (EntityType.FUNCTION, "createIdentityClient.request", 7, 9) in symbols
    assert (EntityType.METHOD, "createIdentityClient.me", 11, 13) in symbols
    assert len([s for s in symbols if s[1] == "createIdentityClient.me"]) == 1


# -- through the index --------------------------------------------------------------------


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


def _method_keys(workspace: Workspace) -> set[str]:
    rows = workspace.database.execute("SELECT key FROM entities WHERE type = 'method'").fetchall()
    return {row["key"] for row in rows}


def _commit_and_sync(workspace: Workspace, root: Path, message: str) -> None:
    git(root / "web", "add", "-A")
    git(root / "web", "commit", "--quiet", "-m", message)
    sync(workspace, workspace.resolver().resolve(root))


def test_returned_methods_are_indexed_and_follow_their_source(index: Index, tmp_path: Path) -> None:
    workspace, root = index({"src/learning-client.ts": LEARNING})
    assert _method_keys(workspace) == {
        "sym:web:src/learning-client.ts:createLearningClient.getPreferences:7",
        "sym:web:src/learning-client.ts:createLearningClient.createCourse:10",
    }

    # An edit below the factory moves nothing: the identities are stable.
    (root / "web/src/learning-client.ts").write_text(LEARNING + "\nexport const VERSION = 1;\n")
    _commit_and_sync(workspace, root, "append")
    assert len(_method_keys(workspace)) == 2
    assert "sym:web:src/learning-client.ts:createLearningClient.createCourse:10" in (
        _method_keys(workspace)
    )

    # Removing a property removes exactly its method.
    (root / "web/src/learning-client.ts").write_text(
        LEARNING.replace(
            "    getPreferences: (auth: CallAuth): Promise<Preferences> =>\n"
            '      request(preferencesSchema, "/v1/me/preferences", '
            "{ headers: authHeaders(auth) }),\n\n",
            "",
        )
    )
    _commit_and_sync(workspace, root, "drop a method")
    assert {key.rsplit(":", 2)[-2] for key in _method_keys(workspace)} == {
        "createLearningClient.createCourse"
    }
    fresh = reindex_from_scratch(corpus_settings(tmp_path / "unused"), root, tmp_path, "fresh")
    assert fact_snapshot(workspace) == fresh

    # Deleting the file removes them all.
    git(root / "web", "rm", "--quiet", "src/learning-client.ts")
    _commit_and_sync(workspace, root, "delete")
    assert _method_keys(workspace) == set()


def test_a_method_is_never_the_target_of_a_bare_call_with_its_name(index: Index) -> None:
    """A property is not in lexical scope: ``createCourse()`` names the import."""
    workspace, _ = index(
        {
            "src/api.ts": "export function createCourse() {\n  return 1;\n}\n",
            "src/client.ts": (
                'import { createCourse } from "./api";\n'
                "export function makeClient() {\n"
                "  return {\n"
                "    createCourse: () => createCourse(),\n"
                "  };\n"
                "}\n"
            ),
        },
    )
    rows = workspace.database.execute(
        "SELECT src.key AS src, dst.key AS dst FROM edges e "
        "JOIN entities src ON src.id = e.src_id JOIN entities dst ON dst.id = e.dst_id "
        "WHERE e.type = 'CALLS'"
    ).fetchall()
    assert {(row["src"], row["dst"]) for row in rows} == {
        ("sym:web:src/client.ts:makeClient.createCourse:4", "sym:web:src/api.ts:createCourse:1")
    }
