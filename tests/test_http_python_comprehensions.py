"""A router registration written as a comprehension statement is read as its one-line loop.

``[app.include_router(r, prefix="/api") for r in routers]`` is ``for r in routers:
app.include_router(r, prefix="/api")``, and goes through the same helpers: same list rule, same
loop-variable rule, same judgement of the owner, the prefix and the child afterwards. Half of this
file is what a comprehension is refused for (a filter, a generator, a dynamic or imported
collection, a variable used twice), and refusing is silent: nothing is mounted, nothing is marked
unreadable, nothing is stored. The one change to the loop itself is that a literal list read other
than as a loop's iterable (``routers.pop()``) is no longer unrolled.
"""

from __future__ import annotations

import ast
import json
import textwrap
from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.graph.py_routes import NOT_MOUNTED
from repoatlas.models.parsing import EdgeType, EntityType, ParsedReference
from repoatlas.parsers import http_python
from repoatlas.parsers.http_facts import (
    HTTP_CLIENT_CALL,
    HTTP_EXPOSED_ENDPOINT,
    PENDING_FACT,
    PYTHON_CONSTANTS,
    PYTHON_MOUNT,
    PYTHON_RECEIVER,
    PYTHON_ROUTE,
    PYTHON_SCRIPT,
)
from repoatlas.parsers.http_python import PythonHttp
from repoatlas.parsers.interpreter import interpreter_tag
from repoatlas.parsers.python import PythonParser
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace
from repoatlas.sync.planner import facts_version
from tests.impact_corpus import build
from tests.incremental_corpus import assert_equals_fresh, edit, sync_all
from tests.test_http_python import _http, _parse, _served
from tests.test_http_python_routers import line, outcomes, served
from tests.test_http_refusals_impact import impact, key

_HTTP_KINDS = {EntityType.HTTP_ROUTE, EntityType.HTTP_DEPLOYMENT, EntityType.HTTP_ENDPOINT}
_ROLES = {
    PYTHON_RECEIVER,
    PYTHON_MOUNT,
    PYTHON_ROUTE,
    PYTHON_CONSTANTS,
    PYTHON_SCRIPT,
    HTTP_CLIENT_CALL,
    HTTP_EXPOSED_ENDPOINT,
}

STAR = "from helpers import *"


def d(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


HEAD = d("""
    from fastapi import APIRouter, FastAPI
    app = FastAPI()
    users = APIRouter(prefix="/users")
    items = APIRouter(prefix="/items")
    @users.get("/me")
    def me(): ...
    @items.post("")
    def create_item(): ...
    routers = [users, items]
""")

#: The same routers with no module-level application: for sources that build their own.
FACTORY_HEAD = d("""
    from fastapi import APIRouter, FastAPI
    users = APIRouter(prefix="/users")
    items = APIRouter(prefix="/items")
    @users.get("/me")
    def me(): ...
    @items.post("")
    def create_item(): ...
    ROUTERS = [users, items]
""")

MOUNT = '[app.include_router(r, prefix="/api") for r in routers]'
API_ROUTES = {"GET /api/users/me", "POST /api/items"}


def _outcomes(source: str) -> list[tuple[int, str]]:
    return PythonHttp(ast.parse(textwrap.dedent(source))).comprehension_outcomes


def _facts(source: str) -> list[ParsedReference]:
    return [r for r in _http(source) if PENDING_FACT not in r.meta]


def _claims(source: str, role: str) -> list[ParsedReference]:
    return [r for r in _parse(source).references if r.meta.get(PENDING_FACT) == role]


def _related(source: str) -> list[ParsedReference]:
    """Every reference the HTTP reader contributes: facts, and the claims the global pass reads."""
    return [
        r
        for r in _parse(source).references
        if r.target_kind in _HTTP_KINDS or r.meta.get(PENDING_FACT) in _ROLES
    ]


def _dump(references: list[ParsedReference]) -> list[str]:
    return sorted(r.model_dump_json() for r in references)


def _mounts_of(source: str, name: str) -> str:
    [receiver] = [r for r in _claims(source, PYTHON_RECEIVER) if r.target_key == name]
    return receiver.meta["mounts"]


def _all_lines(text: str, needle: str) -> list[int]:
    return [number for number, row in enumerate(text.splitlines(), 1) if needle in row]


def _nothing_mounted(source: str) -> None:
    assert _served(source) == set()
    assert _claims(source, PYTHON_MOUNT) == []


# -- the shape ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [MOUNT, "{app.include_router(r, prefix='/api') for r in routers}"],
    ids=["list", "set"],
)
def test_a_comprehension_mounts_each_router_with_its_prefix(statement: str) -> None:
    source = HEAD + statement + "\n"
    assert _served(source) == API_ROUTES
    routes = {r.target_key: r for r in _facts(source) if r.edge_type is EdgeType.SERVES_HTTP_ROUTE}
    me, create = routes["GET /api/users/me"], routes["POST /api/items"]
    for found in (me, create):
        assert found.meta == {"source": "fastapi", "provenance": "composed"}
    assert (me.source_symbol, create.source_symbol) == ("me", "create_item")
    assert (me.line_start, me.line_end) == (line(HEAD, "@users.get"),) * 2
    assert (create.line_start, create.line_end) == (line(HEAD, "@items.post"),) * 2
    assert _mounts_of(source, "users") == '["/api"]'
    assert _mounts_of(source, "items") == '["/api"]'
    assert _claims(source, PYTHON_MOUNT) == []
    assert _outcomes(source) == [(line(source, "include_router"), "unrolled")]


_FASTAPI_CALL = 'app.include_router(r, prefix="/api")'
_IMPORTED = d("""
    from fastapi import FastAPI
    from svc.routes import items, users

    app = FastAPI()
""")
_QUALIFIED = d("""
    from fastapi import FastAPI
    from svc import routes

    app = FastAPI()
""")
_FLASK = d("""
    from flask import Blueprint, Flask
    app = Flask(__name__)
    pages = Blueprint("pages", __name__, url_prefix="/pages")
    admin = Blueprint("admin", __name__)
    @pages.route("/about")
    def about(): ...
    @admin.route("/panel")
    def panel(): ...
""")
_FACTORY = FACTORY_HEAD + "def create_app():\n    app = FastAPI()\n"

_TAGGED = 'app.include_router(r, prefix="/api", tags=["x"])'
_FLASK_CALL = 'app.register_blueprint(r, url_prefix="/v2")'
_ATTRIBUTES = "[routes.users, routes.items]"

#: name -> (source before, the registration call, the iterable, source after, indent)
_TWINS = {
    "named-list": (HEAD, _FASTAPI_CALL, "routers", "", ""),
    "inline-tuple": (HEAD, _FASTAPI_CALL, "(users, items)", "", ""),
    "prefix-and-tags": (HEAD, _TAGGED, "routers", "", ""),
    "imported-elements": (_IMPORTED, _FASTAPI_CALL, "[users, items]", "", ""),
    "module-attribute-elements": (_QUALIFIED, _FASTAPI_CALL, _ATTRIBUTES, "", ""),
    "flask-blueprint": (_FLASK, _FLASK_CALL, "(pages, admin)", "", ""),
    "factory": (_FACTORY, _FASTAPI_CALL, "ROUTERS", "    return app\n", "    "),
}


@pytest.mark.parametrize("case", list(_TWINS))
def test_a_comprehension_reads_exactly_as_its_one_line_for_loop(case: str) -> None:
    before, call, iterable, after, indent = _TWINS[case]
    comprehension = before + f"{indent}[{call} for r in {iterable}]\n" + after
    loop = before + f"{indent}for r in {iterable}: {call}\n" + after
    read, twin = _related(comprehension), _related(loop)
    assert twin
    # A loop that mounted nothing would make this vacuous.
    assert [
        r
        for r in twin
        if r.edge_type is EdgeType.SERVES_HTTP_ROUTE or r.meta.get(PENDING_FACT) == PYTHON_MOUNT
    ]
    assert _dump(read) == _dump(twin)
    assert _outcomes(comprehension) == [(line(comprehension, f"[{call}"), "unrolled")]
    assert _outcomes(loop) == []


# -- what a comprehension is refused for ----------------------------------------------------


def test_an_if_filter_refuses_the_comprehension() -> None:
    source = HEAD + "[app.include_router(r) for r in routers if r is not None]\n"
    assert _served(source) == set()
    assert _mounts_of(source, "users") == "[]"
    assert _mounts_of(source, "items") == "[]"
    # Refusing is not "unreadable": the routers stay usable for a mount elsewhere.
    [users] = [r for r in _claims(source, PYTHON_RECEIVER) if r.target_key == "users"]
    assert users.meta["usable"] == "true"
    assert _outcomes(source) == [(line(source, "include_router"), "conditional-comprehension")]


@pytest.mark.parametrize(
    ("statement", "recorded"),
    [
        ("(app.include_router(r) for r in routers)", "generator-not-eager"),
        ("list(app.include_router(r) for r in routers)", None),
    ],
    ids=["statement", "argument"],
)
def test_a_generator_expression_registers_nothing(statement: str, recorded: str | None) -> None:
    source = HEAD + statement + "\n"
    _nothing_mounted(source)
    expected = [] if recorded is None else [(line(source, "include_router"), recorded)]
    assert _outcomes(source) == expected


_DYNAMIC = {
    "call": "[app.include_router(r) for r in get_routers()]",
    "concatenation": "[app.include_router(r) for r in routers + []]",
    "starred": "[app.include_router(r) for r in [*routers]]",
    "set-display": "[app.include_router(r) for r in {users, items}]",
    "annotated": "ROUTERS: list = [users, items]\n[app.include_router(r) for r in ROUTERS]",
    "bound-twice": "routers = [users]\n[app.include_router(r) for r in routers]",
    "appended-to": "routers.append(items)\n[app.include_router(r) for r in routers]",
    "aliased": "alias = routers\n[app.include_router(r) for r in routers]",
}


@pytest.mark.parametrize("case", list(_DYNAMIC))
def test_a_dynamic_iterable_registers_nothing(case: str) -> None:
    source = HEAD + _DYNAMIC[case] + "\n"
    _nothing_mounted(source)
    assert _outcomes(source) == [(line(source, "[app.include_router"), "dynamic-collection")]


_ROUTES = d("""
    from fastapi import APIRouter

    users = APIRouter(prefix="/users")
    items = APIRouter(prefix="/items")
    extra = APIRouter(prefix="/extra")


    @users.get("/me")
    def me():
        return {}


    @items.post("")
    def create_item():
        return {}


    @extra.get("/more")
    def more():
        return {}
""")
_APP = d("""
    from fastapi import FastAPI

    from src.routes import extra, items, users

    app = FastAPI()

    MOUNT
""")
_LIST = '[app.include_router(r, prefix="/api") for r in [users, items]]'


def _app(mount: str) -> str:
    return _APP.replace("MOUNT", mount)


def _service(mount: str = _LIST, routes: str = _ROUTES) -> dict[str, dict[str, str]]:
    files = {"src/__init__.py": "", "src/routes.py": routes, "src/app.py": _app(mount)}
    return {"svc": files}


_ME = ("svc", "src/routes.py", "me", "GET /api/users/me", None)
_CREATE = ("svc", "src/routes.py", "create_item", "POST /api/items", None)
_MORE = ("svc", "src/routes.py", "more", "GET /api/extra/more", None)


def _mount_claims(workspace: Workspace) -> list[tuple[str, dict[str, str]]]:
    rows = workspace.database.execute(
        """
        SELECT f.rel_path, u.meta_json FROM unresolved_refs u JOIN files f ON f.id = u.file_id
        WHERE u.meta_json LIKE '%"python-mount"%' ORDER BY u.line_start, u.target_key
        """
    ).fetchall()
    return [(row[0], json.loads(row[1])) for row in rows]


def _outcome_of(workspace: Workspace, handler: str) -> str:
    [found] = [v for (_, name, _), v in outcomes(workspace).items() if name == handler]
    return found


def test_imported_routers_in_a_local_list_are_mounted_across_modules(
    workspace: Workspace, tmp_path: Path
) -> None:
    indexed = build(workspace, tmp_path, _service())
    assert served(indexed) == {_ME, _CREATE}
    claims = _mount_claims(indexed)
    assert [path for path, _ in claims] == ["src/app.py"] * 2
    assert {meta["name"] for _, meta in claims} == {"users", "items"}
    for _, meta in claims:
        assert meta["module"] == "src.routes"
        assert meta["readable"] == "true"
        assert meta["prefix"] == '"/api"'
        assert meta["owner"] == "app"
        assert meta["owner_kind"] == "fastapi-app"
        assert meta["call"] == "include_router"
    assert _outcome_of(indexed, "more") == NOT_MOUNTED


@pytest.mark.parametrize("form", ["comprehension", "loop"])
def test_an_imported_collection_is_refused(form: str, workspace: Workspace, tmp_path: Path) -> None:
    routes = _ROUTES + "\nrouters = [users, items]\n"
    call = 'app.include_router(r, prefix="/api")'
    mount = f"[{call} for r in routers]" if form == "comprehension" else f"for r in routers: {call}"
    files = _service(mount, routes)
    files["svc"]["src/app.py"] = _app(mount).replace(
        "from src.routes import extra, items, users", "from src.routes import routers"
    )
    indexed = build(workspace, tmp_path, files)
    assert _mount_claims(indexed) == []
    assert served(indexed) == set()
    assert _outcome_of(indexed, "me") == NOT_MOUNTED
    assert _outcome_of(indexed, "create_item") == NOT_MOUNTED
    if form == "comprehension":
        app_source = files["svc"]["src/app.py"]
        found = [(line(app_source, "include_router"), "collection-imported")]
        assert _outcomes(app_source) == found


_BLOCKS = {
    "if": "if FLAG:\n    {m}",
    "try": "try:\n    {m}\nexcept Exception:\n    pass",
    "with": "with ctx:\n    {m}",
    "for": "for _ in range(1):\n    {m}",
    "while": "while FLAG:\n    {m}\n    break",
    "match": "match FLAG:\n    case _:\n        {m}",
    "factory-if": (
        "def create_app():\n    app = FastAPI()\n    if FLAG:\n        {m}\n    return app"
    ),
    "factory-nested-function": (
        "def create_app():\n    app = FastAPI()\n    def inner():\n        {m}\n    return app"
    ),
    "class-body": "class C:\n    {m}",
    "assignment": "x = {m}",
    "argument": "print({m})",
}


@pytest.mark.parametrize("case", list(_BLOCKS))
def test_a_comprehension_in_a_block_is_not_read(case: str) -> None:
    source = HEAD + _BLOCKS[case].replace("{m}", "[app.include_router(r) for r in routers]") + "\n"
    _nothing_mounted(source)
    assert _outcomes(source) == []


@pytest.mark.parametrize(
    ("call", "reason"),
    [
        ("app.include_router(r, prefix=r.prefix)", "loop-variable-reused"),
        ("app.include_router(r, dependencies=[r])", "loop-variable-reused"),
        ("app.include_router(users, prefix=r)", "loop-variable-not-first"),
        ("r.include_router(users)", "loop-variable-not-first"),
        ("app.include_router(router=r)", "loop-variable-not-first"),
        ("app.include_router(x := r)", "loop-variable-not-first"),
    ],
)
def test_a_misused_loop_variable_refuses(call: str, reason: str) -> None:
    source = HEAD + f"[{call} for r in routers]\n"
    _nothing_mounted(source)
    assert _outcomes(source) == [(line(source, "include_router"), reason)]


def test_a_router_rebound_after_the_comprehension_is_not_served() -> None:
    source = HEAD + MOUNT + '\nusers = APIRouter(prefix="/other")\n'
    assert _served(source) == {"POST /api/items"}
    assert [r for r in _claims(source, PYTHON_RECEIVER) if r.target_key == "users"] == []


def test_a_loop_variable_shadowing_a_module_name_refuses() -> None:
    source = (
        HEAD
        + 'router = APIRouter(prefix="/r")\n@router.get("/x")\ndef x(): ...\n'
        + "[app.include_router(router) for router in routers]\n"
    )
    _nothing_mounted(source)
    assert [r for r in _claims(source, PYTHON_RECEIVER) if r.target_key == "router"] == []
    assert _outcomes(source) == [(line(source, "[app.include"), "loop-variable-rebound")]


def test_two_comprehensions_mount_one_router_at_both_prefixes() -> None:
    first = '[app.include_router(a, prefix="/v1") for a in [users]]'
    second = '[app.include_router(b, prefix="/v2") for b in [users]]'
    source = HEAD + first + "\n" + second + "\n"
    assert _served(source) == {"GET /v1/users/me", "GET /v2/users/me"}
    assert _mounts_of(source, "users") == '["/v1", "/v2"]'

    again = '[app.include_router(r, prefix="/v1") for r in [users]]'
    other = '[app.include_router(r, prefix="/v2") for r in [users]]'
    reused = HEAD + again + "\n" + other + "\n"
    _nothing_mounted(reused)
    lines = _all_lines(reused, "[app.include_router")
    assert _outcomes(reused) == [(n, "loop-variable-rebound") for n in lines]
    assert len(lines) == 2

    both = HEAD + MOUNT + "\napp.include_router(users)\n"
    assert _served(both) == {"GET /users/me", "GET /api/users/me", "POST /api/items"}


def _factory_source(variant: str) -> str:
    local = "    routers = [users, items]\n" if variant == "local" else ""
    name = "routers" if variant == "local" else "ROUTERS"
    return (
        FACTORY_HEAD
        + "def create_app():\n    app = FastAPI()\n"
        + local
        + f'    [app.include_router(r, prefix="/api") for r in {name}]\n'
        + "    return app\n"
    )


@pytest.mark.parametrize("variant", ["module", "local"])
def test_a_comprehension_in_a_factory_is_a_factory_mount(variant: str) -> None:
    source = _factory_source(variant)
    claims = _claims(source, PYTHON_MOUNT)
    assert sorted(c.target_key for c in claims) == ["items", "users"]
    for claim in claims:
        assert "module" not in claim.meta
        assert claim.meta["owner"] == "create_app.app"
        assert claim.meta["readable"] == "true"
        assert claim.meta["prefix"] == '"/api"'
    assert _served(source) == set()
    assert _outcomes(source) == [(line(source, "include_router"), "unrolled")]


@pytest.mark.parametrize("variant", ["module", "local"])
def test_a_factory_comprehension_serves_the_routes_across_the_workspace(
    variant: str, workspace: Workspace, tmp_path: Path
) -> None:
    indexed = build(workspace, tmp_path, {"svc": {"app.py": _factory_source(variant)}})
    assert {route for _, _, _, route, _ in served(indexed)} == API_ROUTES


_UNSUPPORTED = {
    "multi-generator": (HEAD, "[app.include_router(r) for _ in () for r in routers]"),
    "nested-comprehension": (
        HEAD,
        "[[app.include_router(r) for r in g] for g in [routers]]",
    ),
    "dict-comprehension": (HEAD, "{id(r): app.include_router(r) for r in routers}"),
    "tuple-target": (HEAD, "[app.include_router(r) for r, _ in [(users, 1)]]"),
    "async-comprehension": (
        FACTORY_HEAD,
        "async def create_app():\n    app = FastAPI()\n"
        "    [app.include_router(r) async for r in ROUTERS]\n    return app",
    ),
    "boolean-element": (HEAD, "[app.include_router(r) or None for r in routers]"),
    "subscript-owner": (HEAD, "[apps[0].include_router(r) for r in routers]"),
}
_REASONS = {
    "multi-generator": "multi-generator",
    "nested-comprehension": "not-a-registration",
    "dict-comprehension": "dict-comprehension",
    "tuple-target": "unsupported-target",
    "async-comprehension": "async-comprehension",
    "boolean-element": "not-a-registration",
    "subscript-owner": "not-a-registration",
}


@pytest.mark.parametrize("case", list(_UNSUPPORTED))
def test_unsupported_shapes_are_refused_with_their_reason(case: str) -> None:
    head, statement = _UNSUPPORTED[case]
    source = head + statement + "\n"
    _nothing_mounted(source)
    assert _mounts_of(source, "users") == "[]"
    [(_, reason)] = _outcomes(source)
    assert reason == _REASONS[case]


def test_a_star_import_composes_with_the_comprehension() -> None:
    plain = HEAD + MOUNT + "\n"
    before = f"{STAR}; " + plain
    assert _served(before) == API_ROUTES
    assert _dump(_related(before)) == _dump(_related(plain))
    after = plain + f"\n{STAR}\n"
    assert _served(after) == set()
    assert {r.meta.get(PENDING_FACT) for r in _related(after)} <= {PYTHON_SCRIPT}


def test_a_list_read_other_than_by_a_loop_is_not_unrolled() -> None:
    loop = HEAD + "routers.pop()\nfor r in routers:\n    app.include_router(r, prefix='/api')\n"
    assert _served(loop) == set()
    comprehension = HEAD + "routers.pop()\n" + MOUNT + "\n"
    assert _served(comprehension) == set()
    assert _outcomes(comprehension) == [(line(comprehension, "[app.include"), "dynamic-collection")]
    # The same loop without the read is what it was.
    untouched = HEAD + "for r in routers:\n    app.include_router(r, prefix='/api')\n"
    assert _served(untouched) == API_ROUTES


def test_a_flask_blueprint_comprehension_registers_each() -> None:
    source = _FLASK + "[app.register_blueprint(bp) for bp in (pages, admin)]\n"
    assert _served(source) == {"GET /pages/about", "GET /panel"}


def test_a_comprehension_on_an_unproven_receiver_mounts_nothing() -> None:
    source = d("""
        from fastapi import FastAPI
        from svc_app import make_app
        from src.routes import items, users
        app = make_app()
        [app.include_router(r) for r in [users, items]]
    """)
    _nothing_mounted(source)
    assert _outcomes(source) == [(line(source, "include_router"), "unrolled")]


# -- end to end: incremental equals fresh ---------------------------------------------------


def test_editing_the_comprehension_list_adds_and_removes_mounts(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    indexed = build(workspace, tmp_path, _service())
    root = tmp_path / "repos"
    assert served(indexed) == {_ME, _CREATE}

    edit(root, "svc", "src/app.py", _app('[app.include_router(r, prefix="/api") for r in [users]]'))
    sync_all(indexed, root)
    assert served(indexed) == {_ME}
    assert_equals_fresh(indexed, settings, root, tmp_path, "comp-remove")

    three = '[app.include_router(r, prefix="/api") for r in [users, items, extra]]'
    edit(root, "svc", "src/app.py", _app(three))
    sync_all(indexed, root)
    assert served(indexed) == {_ME, _CREATE, _MORE}
    assert_equals_fresh(indexed, settings, root, tmp_path, "comp-add")


def test_turning_the_comprehension_lazy_or_conditional_unserves_incrementally(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    indexed = build(workspace, tmp_path, _service())
    root = tmp_path / "repos"
    assert served(indexed) == {_ME, _CREATE}

    lazy = '(app.include_router(r, prefix="/api") for r in [users, items])'
    edit(root, "svc", "src/app.py", _app(lazy))
    sync_all(indexed, root)
    assert served(indexed) == set()
    assert_equals_fresh(indexed, settings, root, tmp_path, "comp-lazy")

    edit(root, "svc", "src/app.py", _app(_LIST))
    sync_all(indexed, root)
    assert served(indexed) == {_ME, _CREATE}

    filtered = '[app.include_router(r, prefix="/api") for r in [users, items] if r]'
    edit(root, "svc", "src/app.py", _app(filtered))
    sync_all(indexed, root)
    assert served(indexed) == set()
    assert_equals_fresh(indexed, settings, root, tmp_path, "comp-if")


def test_a_body_edit_in_a_router_module_changes_no_route(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    indexed = build(workspace, tmp_path, _service())
    root = tmp_path / "repos"
    before = served(indexed)
    assert before == {_ME, _CREATE}
    edited = _ROUTES.replace("def me():\n    return {}", 'def me():\n    return {"edited": True}')
    assert edited != _ROUTES
    edit(root, "svc", "src/routes.py", edited)
    work = sync_all(indexed, root)
    assert served(indexed) == before
    assert work.repositories_invalidated == 0
    assert_equals_fresh(indexed, settings, root, tmp_path, "comp-body")


def test_an_index_written_before_the_rule_is_reparsed_on_upgrade(
    workspace: Workspace, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Worker processes do not see a monkeypatch: parse in this one.
    monkeypatch.setenv("REPOATLAS_SYNC_WORKERS", "1")
    current = http_python._comprehension_loop

    def refuse(*args: object) -> tuple[str, None, None] | None:
        # The old reader: every comprehension statement that mounts is simply not read.
        found = current(*args)  # type: ignore[arg-type]
        return None if found is None else ("dynamic-collection", None, None)

    monkeypatch.setattr(http_python, "_comprehension_loop", refuse)
    indexed = build(workspace, tmp_path, _service())
    assert served(indexed) == set()

    live = facts_version(indexed.registry, indexed.dependency_registry)
    version = f"{PythonParser.name}:{PythonParser.version}"
    assert version in live, live
    # A literal, not "one less than now": a relative offset would pass with the bump deleted.
    previous = live.replace(version, f"python-ast:21+{interpreter_tag()}")
    assert previous != live
    indexed.database.execute(
        "UPDATE files SET parser_version = ? WHERE rel_path LIKE '%.py'", (previous,)
    )
    indexed.database.connection.commit()

    monkeypatch.setattr(http_python, "_comprehension_loop", current)
    root = tmp_path / "repos"
    report = sync(indexed, indexed.resolver().resolve(root))
    assert sum(r.changed for r in report.repositories) >= 1, report
    assert served(indexed) == {_ME, _CREATE}
    assert_equals_fresh(indexed, settings, root, tmp_path, "comp-upgraded")


@pytest.mark.parametrize(
    ("mount", "reaches"),
    [
        (_LIST, True),
        ('(app.include_router(r, prefix="/api") for r in [users, items])', False),
    ],
    ids=["list", "generator"],
)
def test_impact_from_a_comprehension_mounted_handler_reaches_its_route(
    mount: str, reaches: bool, workspace: Workspace, tmp_path: Path
) -> None:
    indexed = build(workspace, tmp_path, _service(mount))
    response = impact(indexed, key(indexed, "svc", "me"))
    reached = {(c.name, c.step.edge if c.step else None) for c in response.candidates}
    assert (("GET /api/users/me", EdgeType.SERVES_HTTP_ROUTE) in reached) is reaches
