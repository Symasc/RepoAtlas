"""A router included on a sub-application is served nowhere (python-impact, sub-application fix).

``app.mount("/sub", sub)`` serves ``sub``'s routes under ``/sub``, and a router ``sub`` includes
is one of them. Reading the include as a mount on a root application invents a route at the
parent's root, so a router included on an application that another application mounts is served
nowhere. Closed sets, per file and through the global pass, and incremental equals fresh.

The same holds across modules: ``sub`` declared and included-on in ``src/sub.py`` and mounted by
``app.mount("/sub", sub)`` in ``src/main.py`` is a sub-application, whatever import form names it,
however many sites mount it, and in whatever order the claims are read. A mount whose child is not
proven -- a re-export, an unresolvable module -- is read as a mount of every application of that
name in its own repository (never another repository, never a router, never a non-mount claim).

A sub-application mounted at a literal FastAPI prefix by a parent in another module (or in a
factory) is also served under the mount: its own file's edge stays (the child's view), and the
parent's view adds the prefix and the path, with the parent as the application that serves it.
The mounted name may be one top-level assignment away from an import. Everything not proven
(an empty, trailing-slash, relative, imported or computed prefix, an ambiguous or conditional
alias, a mount made on a sub-application) adds nothing.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from repoatlas.graph.http_claims import HttpClaimPlan
from repoatlas.graph.linker import ReferenceLinker, plan_repository_http
from repoatlas.graph.py_routes import SERVED_HERE, UNREADABLE_MOUNT
from repoatlas.models.parsing import ParsedReference
from repoatlas.parsers.http_facts import PENDING_FACT, PYTHON_MOUNT, PYTHON_RECEIVER, PYTHON_ROUTE
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build
from tests.incremental_corpus import assert_equals_fresh, edit, sync_all
from tests.test_http_python import _parse, _served
from tests.test_http_python_routers import line, outcomes, served


def d(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def _source(tail: str, router: str = "") -> str:
    """A file with an application, a router and its one route, then ``tail``."""
    head = (
        "from fastapi import FastAPI, APIRouter\n\n"
        "app = FastAPI()\n"
        f"r = APIRouter({router})\n\n\n"
        '@r.get("/inner")\n'
        "def inner(): ...\n\n"
    )
    return head + d(tail)


def _roles(source: str, role: str) -> list[ParsedReference]:
    return [r for r in _parse(source).references if r.meta.get(PENDING_FACT) == role]


def _receiver(source: str, name: str) -> ParsedReference:
    [found] = [r for r in _roles(source, PYTHON_RECEIVER) if r.target_key == name]
    return found


# -- one file ---------------------------------------------------------------------------------


def test_a_router_included_in_a_mounted_sub_application_is_served_nowhere() -> None:
    source = _source("""
        sub = FastAPI()
        sub.include_router(r)
        app.mount("/sub", sub)
    """)
    assert _served(source) == set()
    assert _receiver(source, "r").meta["usable"] == "false"
    assert _roles(source, PYTHON_ROUTE) == []


def test_a_nested_sub_application_mount_serves_nothing() -> None:
    source = _source("""
        sub1 = FastAPI()
        sub2 = FastAPI()
        sub2.include_router(r)
        app.mount("/a", sub1)
        sub1.mount("/b", sub2)


        @app.get("/outer")
        def outer(): ...
    """)
    assert _served(source) == {"GET /outer"}
    assert _receiver(source, "r").meta["usable"] == "false"


def test_two_mount_sites_serve_nothing() -> None:
    twice = _source("""
        sub = FastAPI()
        sub.include_router(r)
        app.mount("/x", sub)
        app.mount("/y", sub)
    """)
    assert _served(twice) == set()
    other_parent = _source("""
        sub = FastAPI()
        app2 = FastAPI()
        sub.include_router(r)
        app.mount("/x", sub)
        app2.mount("/y", sub)
    """)
    assert _served(other_parent) == set()


def test_a_mounted_sub_application_keeps_its_parents_routes() -> None:
    tail = """
        sub = FastAPI()


        @sub.get("/deep")
        def deep(): ...


        sub.include_router(r)


        @app.get("/outer")
        def outer(): ...


        app.mount("/sub", sub)
    """
    assert _served(_source(tail)) == {"GET /outer"}
    both = _source(d(tail) + "app.include_router(r)\n")
    assert _served(both) == {"GET /outer"}
    assert _receiver(both, "r").meta["usable"] == "false"


def test_a_sub_application_that_is_never_mounted_serves_as_before() -> None:
    source = _source("""
        sub = FastAPI()
        sub.include_router(r)
    """)
    assert _served(source) == {"GET /inner"}
    assert _receiver(source, "r").meta["usable"] == "true"


@pytest.mark.parametrize("router", ["", 'prefix="/r"'])
@pytest.mark.parametrize("include", ["", ', prefix="/v1"'])
def test_a_prefix_on_the_include_is_not_served_under_the_mount(router: str, include: str) -> None:
    source = _source(
        f"""
        sub = FastAPI()
        sub.include_router(r{include})
        app.mount("/sub", sub)
    """,
        router,
    )
    found = _served(source)
    assert found == set()
    assert not {"GET /v1/inner", "GET /inner", "GET /sub/v1/inner", "GET /r/inner"} & found


@pytest.mark.parametrize(
    "mount",
    [
        'PATH = "/sub"\napp.mount(PATH, sub)\n',
        'NAME = "sub"\napp.mount(f"/{NAME}", sub)\n',
        'app.mount(path="/sub", app=sub)\n',
        "app.mount(make_path(), sub)\n",
    ],
)
def test_an_ambiguous_mount_path_still_unserves(mount: str) -> None:
    source = _source("sub = FastAPI()\nsub.include_router(r)\n" + mount)
    assert _served(source) == set()


def test_a_mount_placed_before_the_include_still_unserves() -> None:
    source = _source("""
        sub = FastAPI()
        app.mount("/sub", sub)
        sub.include_router(r)
    """)
    assert _served(source) == set()


# -- an application factory ---------------------------------------------------------------------


def test_a_factory_mounting_an_included_router_is_unreadable() -> None:
    source = _source("""
        def create_app():
            app = FastAPI()
            sub = FastAPI()
            sub.include_router(r)
            app.mount("/sub", sub)
            return app
    """)
    assert _served(source) == set()
    [mount] = [m for m in _roles(source, PYTHON_MOUNT) if m.meta["call"] == "include_router"]
    assert mount.meta["owner"] == "create_app.sub"
    assert mount.meta["readable"] == "false"


def test_a_factory_mounting_a_module_level_sub_application() -> None:
    source = _source("""
        sub = FastAPI()
        sub.include_router(r)


        def create_app():
            app = FastAPI()
            app.mount("/sub", sub)
            return app
    """)
    assert _served(source) == set()
    assert _receiver(source, "sub").meta["usable"] == "false"
    assert _receiver(source, "r").meta["usable"] == "false"


def test_a_factory_parameter_shadowing_the_sub_application_changes_nothing() -> None:
    # The parameter is a second binding of the name, so the module's ``sub`` is no proven
    # application at all: nothing is invented, and the router is not marked unusable.
    source = _source("""
        sub = FastAPI()
        sub.include_router(r)


        def create_app(sub):
            app = FastAPI()
            app.mount("/x", sub)
            return app
    """)
    assert _served(source) == set()
    assert _receiver(source, "r").meta["usable"] == "true"
    assert "sub" not in {c.target_key for c in _roles(source, PYTHON_RECEIVER)}


def test_an_ordinary_factory_include_stays_readable() -> None:
    plain = d("""
        from fastapi import FastAPI
        from svc.routes import users


        def create_app():
            app = FastAPI()
            app.include_router(users)
            return app
    """)
    [mount] = _roles(plain, PYTHON_MOUNT)
    assert (mount.meta["owner"], mount.meta["readable"]) == ("create_app.app", "true")

    beside = d("""
        from fastapi import FastAPI
        from svc.routes import users, other


        def create_app():
            app = FastAPI()
            sub = FastAPI()
            app.include_router(users)
            sub.include_router(other)
            app.mount("/sub", sub)
            return app
    """)
    found = {m.target_key: m.meta["readable"] for m in _roles(beside, PYTHON_MOUNT)}
    assert found == {"users": "true", "other": "false"}


# -- through the global pass --------------------------------------------------------------------

ROUTES = d("""
    from fastapi import APIRouter

    router = APIRouter()


    @router.get("/x")
    def read_x():
        return {}
""")
APP = d("""
    from fastapi import FastAPI

    from src.routes import router

    sub = FastAPI()
    sub.include_router(router)
    app = FastAPI()
""")
MOUNT = 'app.mount("/sub", sub)\n'
ROUTES_FILE = "src/routes.py"


def _files(app: str) -> dict[str, dict[str, str]]:
    return {"svc": {"src/__init__.py": "", ROUTES_FILE: ROUTES, "src/app.py": app}}


def _routes(workspace: Workspace) -> set[str]:
    return {route for *_, route, _ in served(workspace)}


def test_the_mount_of_an_imported_router_on_a_sub_application_is_unreadable(
    workspace: Workspace, tmp_path: Path
) -> None:
    indexed = build(workspace, tmp_path, _files(APP + MOUNT))
    assert served(indexed) == set()
    assert outcomes(indexed)[(ROUTES_FILE, "read_x", "GET /x")] == UNREADABLE_MOUNT


def test_an_imported_router_on_a_sub_application_nobody_mounts_is_served(
    workspace: Workspace, tmp_path: Path
) -> None:
    indexed = build(workspace, tmp_path, _files(APP))
    assert _routes(indexed) == {"GET /x"}


# -- incremental equals fresh --------------------------------------------------------------------


def test_adding_and_removing_the_mount_moves_the_route(
    workspace: Workspace, settings, tmp_path: Path
) -> None:
    indexed = build(workspace, tmp_path, _files(APP))
    root = tmp_path / "repos"
    assert _routes(indexed) == {"GET /x"}

    edit(root, "svc", "src/app.py", APP + MOUNT)
    sync_all(indexed, root)
    assert _routes(indexed) == set()
    assert_equals_fresh(indexed, settings, root, tmp_path, "mounted")

    edit(root, "svc", "src/app.py", APP)
    sync_all(indexed, root)
    assert _routes(indexed) == {"GET /x"}
    assert_equals_fresh(indexed, settings, root, tmp_path, "unmounted")


def test_a_body_edit_in_the_router_module_keeps_it_unserved(
    workspace: Workspace, settings, tmp_path: Path
) -> None:
    indexed = build(workspace, tmp_path, _files(APP + MOUNT))
    root = tmp_path / "repos"
    assert _routes(indexed) == set()

    edit(root, "svc", ROUTES_FILE, ROUTES.replace("return {}", 'return {"x": 1}'))
    sync_all(indexed, root)
    assert _routes(indexed) == set()
    assert_equals_fresh(indexed, settings, root, tmp_path, "body")


# -- a sub-application mounted from another module ----------------------------------------------

READ_X = (ROUTES_FILE, "read_x", "GET /x")
OUTER_DECORATOR = '@app.get("/outer")'


def _sub(name: str = "sub") -> str:
    """``src/sub.py``: a sub-application, with the router of ``src/routes.py`` included on it."""
    return d(f"""
        from fastapi import FastAPI

        from src.routes import router

        {name} = FastAPI()
        {name}.include_router(router)
    """)


def _main(
    imports: str = "from src.sub import sub", mount: str = 'app.mount("/sub", sub)', more: str = ""
) -> str:
    """A module with the root application, its one route, and the lines that mount."""
    return "\n".join(
        [
            "from fastapi import FastAPI",
            imports,
            "",
            "app = FastAPI()",
            mount,
            more,
            "",
            "",
            OUTER_DECORATOR,
            "def outer(): ...",
            "",
        ]
    )


def _svc(
    main: str, *, main_path: str = "src/main.py", extra: dict[str, str] | None = None
) -> dict[str, dict[str, str]]:
    files = {"src/__init__.py": "", ROUTES_FILE: ROUTES, "src/sub.py": _sub(), main_path: main}
    return {"svc": {**files, **(extra or {})}}


def _router_module(name: str, path: str, handler: str) -> str:
    return d(f"""
        from fastapi import APIRouter

        {name} = APIRouter()


        @{name}.get("{path}")
        def {handler}():
            return {{}}
    """)


def _all_routes(workspace: Workspace) -> set[tuple[str, str]]:
    return {(repo, route) for repo, _, _, route, _ in served(workspace)}


def test_a_sub_application_mounted_from_another_module_serves_its_router_nowhere(
    workspace: Workspace, tmp_path: Path
) -> None:
    main = _main()
    indexed = build(workspace, tmp_path, _svc(main))
    assert served(indexed) == {
        ("svc", "src/main.py", "outer", "GET /outer", line(main, OUTER_DECORATOR))
    }
    assert outcomes(indexed)[READ_X] == UNREADABLE_MOUNT


@pytest.mark.parametrize(
    ("imports", "mount"),
    [
        ("from src.sub import sub as child", 'app.mount("/sub", child)'),
        ("import src.sub as m", 'app.mount("/sub", m.sub)'),
    ],
)
def test_the_form_of_the_import_naming_the_sub_application_does_not_matter(
    workspace: Workspace, tmp_path: Path, imports: str, mount: str
) -> None:
    indexed = build(workspace, tmp_path, _svc(_main(imports, mount)))
    assert _routes(indexed) == {"GET /outer"}
    assert outcomes(indexed)[READ_X] == UNREADABLE_MOUNT


def test_a_factory_mounting_the_imported_sub_application_serves_its_router_nowhere(
    workspace: Workspace, tmp_path: Path
) -> None:
    main = d("""
        from fastapi import FastAPI

        from src.sub import sub


        def create_app():
            app = FastAPI()
            app.mount("/sub", sub)
            return app
    """)
    indexed = build(workspace, tmp_path, _svc(main))
    assert _routes(indexed) == set()
    assert outcomes(indexed)[READ_X] == UNREADABLE_MOUNT


@pytest.mark.parametrize(
    "mount",
    [
        'app.mount("/a", sub)\napp.mount("/b", sub)',
        'app2 = FastAPI()\napp.mount("/a", sub)\napp2.mount("/b", sub)',
    ],
)
def test_a_sub_application_mounted_twice_or_by_two_parents_serves_nothing(
    workspace: Workspace, tmp_path: Path, mount: str
) -> None:
    indexed = build(workspace, tmp_path, _svc(_main(mount=mount)))
    assert "GET /x" not in _routes(indexed)
    assert outcomes(indexed)[READ_X] == UNREADABLE_MOUNT


def test_the_router_also_included_on_the_root_application_is_still_served_nowhere(
    workspace: Workspace, tmp_path: Path
) -> None:
    main = _main(
        imports="from src.sub import sub\nfrom src.routes import router",
        more="app.include_router(router)",
    )
    indexed = build(workspace, tmp_path, _svc(main))
    assert _routes(indexed) == {"GET /outer"}
    assert outcomes(indexed)[READ_X] == UNREADABLE_MOUNT


def test_a_nested_sub_application_mounted_across_modules_serves_nothing_below_it(
    workspace: Workspace, tmp_path: Path
) -> None:
    first = d("""
        from fastapi import FastAPI

        from src.b import sub2
        from src.r1 import router1

        sub1 = FastAPI()
        sub1.include_router(router1)
        sub1.mount("/b", sub2)
    """)
    second = d("""
        from fastapi import FastAPI

        from src.r2 import router2

        sub2 = FastAPI()
        sub2.include_router(router2)
    """)
    main = _main(imports="from src.a import sub1", mount='app.mount("/a", sub1)')
    extra = {
        "src/sub.py": "",  # the default sub-application is not part of this case
        "src/a.py": first,
        "src/b.py": second,
        "src/r1.py": _router_module("router1", "/one", "read_one"),
        "src/r2.py": _router_module("router2", "/two", "read_two"),
    }
    indexed = build(workspace, tmp_path, _svc(main, extra=extra))
    assert _routes(indexed) == {"GET /outer"}


def test_a_sub_application_nobody_mounts_is_served_beside_a_mounted_one(
    workspace: Workspace, tmp_path: Path
) -> None:
    other = d("""
        from fastapi import FastAPI

        from src.routes2 import router2

        sub = FastAPI()
        sub.include_router(router2)
    """)
    extra = {"src/other.py": other, "src/routes2.py": _router_module("router2", "/y", "read_y")}
    indexed = build(workspace, tmp_path, _svc(_main(), extra=extra))
    assert _routes(indexed) == {"GET /outer", "GET /y"}


@pytest.mark.parametrize(
    ("init", "imports"),
    [
        ("from src.sub import sub\n", "from src import sub"),
        ("", "from missing.mod import sub"),
    ],
)
def test_a_mount_whose_child_is_not_proven_unserves_the_applications_of_that_name(
    workspace: Workspace, tmp_path: Path, init: str, imports: str
) -> None:
    files = _svc(_main(imports=imports), extra={"src/__init__.py": init})
    indexed = build(workspace, tmp_path, files)
    assert _routes(indexed) == {"GET /outer"}
    assert outcomes(indexed)[READ_X] == UNREADABLE_MOUNT


def test_an_unproven_mount_leaves_a_router_of_that_name_served(
    workspace: Workspace, tmp_path: Path
) -> None:
    metrics = _router_module("metrics", "/m", "read_m")
    wire = d("""
        from fastapi import FastAPI

        from src.metrics_routes import metrics

        root = FastAPI()
        root.include_router(metrics)
    """)
    main = d("""
        from fastapi import FastAPI
        from metrics_lib import metrics, metrics_router

        opts = {}
        app = FastAPI()
        app.mount("/m", metrics)
        app.include_router(metrics_router, **opts)
    """)
    files = {
        "src/__init__.py": "",
        "src/metrics_routes.py": metrics,
        "src/wire.py": wire,
        "src/main.py": main,
    }
    # a router of that name is no application, so the unproven mount does not touch it
    assert _routes(build(workspace, tmp_path, {"svc": files})) == {"GET /m"}


def test_an_unproven_mount_in_one_repository_leaves_another_repositorys_applications(
    workspace: Workspace, tmp_path: Path
) -> None:
    other = {
        "src/__init__.py": "",
        ROUTES_FILE: ROUTES.replace('"/x"', '"/o"').replace("read_x", "read_o"),
        "src/sub.py": _sub(),
    }
    main = _main(imports="from missing.mod import sub")
    indexed = build(workspace, tmp_path, {**_svc(main), "other": other})
    assert _all_routes(indexed) == {("svc", "GET /outer"), ("other", "GET /o")}

    plan = ReferenceLinker(indexed.knowledge).prepare_global().http
    for slug in ("svc", "other"):
        [repo_id] = [
            row[0]
            for row in indexed.database.execute(
                "SELECT id FROM repositories WHERE slug = ?", (slug,)
            ).fetchall()
        ]
        own = plan_repository_http(indexed.knowledge, repo_id).plan
        mine = {(e.src_id, e.target.name, e.line_start) for e in plan.http if e.repo_id == repo_id}
        assert {(e.src_id, e.target.name, e.line_start) for e in own.http} == mine
        assert {k: v for k, v in plan.routes.items() if k in own.routes} == own.routes


def test_an_unresolvable_include_does_not_unserve_an_application_of_its_name(
    workspace: Workspace, tmp_path: Path
) -> None:
    # Only a sub-application ``mount`` is read as a mount of every application of that name.
    # The included ``x`` is declared in its module, but not as an application: an include of
    # it is not a mount, so the application ``x`` elsewhere in the repository stays served.
    xapp = d("""
        x = make_app()
    """)
    other = d("""
        from fastapi import FastAPI

        x = FastAPI()


        @x.get("/z")
        def read_z(): ...
    """)
    wire = d("""
        from fastapi import FastAPI
        from src.xapp import x

        opts = {}
        app = FastAPI()
        app.include_router(x, **opts)
    """)
    files = {"src/__init__.py": "", "src/xapp.py": xapp, "src/other.py": other, "src/wire.py": wire}
    assert _routes(build(workspace, tmp_path, {"svc": files})) == {"GET /z"}


def test_an_unresolved_include_does_not_unserve_a_router_on_an_application_of_its_name(
    workspace: Workspace, tmp_path: Path
) -> None:
    # Same scope limit as above, with a router: ``other`` includes it on its own ``x``, so the
    # router is served there. The unresolved include of a different ``x`` is no mount.
    routes = d("""
        from fastapi import APIRouter

        router = APIRouter()


        @router.get("/x")
        def read_x(): ...
    """)
    other = d("""
        from fastapi import FastAPI

        from src.routes import router

        x = FastAPI()
        x.include_router(router)
    """)
    xapp = d("""
        x = make_app()
    """)
    wire = d("""
        from fastapi import FastAPI

        from src.xapp import x

        opts = {}
        app = FastAPI()
        app.include_router(x, **opts)
    """)
    files = {
        "src/__init__.py": "",
        "src/routes.py": routes,
        "src/other.py": other,
        "src/xapp.py": xapp,
        "src/wire.py": wire,
    }
    assert _routes(build(workspace, tmp_path, {"svc": files})) == {"GET /x"}


@pytest.mark.parametrize("name", ["a_main", "zz_main"])
def test_the_order_the_claims_are_read_in_does_not_change_the_result(
    workspace: Workspace, tmp_path: Path, name: str
) -> None:
    indexed = build(workspace, tmp_path, _svc(_main(), main_path=f"src/{name}.py"))
    assert _routes(indexed) == {"GET /outer"}
    assert outcomes(indexed)[READ_X] == UNREADABLE_MOUNT


def test_mounting_the_sub_application_from_another_module_moves_the_route_incrementally(
    workspace: Workspace, settings, tmp_path: Path
) -> None:
    unmounted = _main(imports="", mount="")
    indexed = build(workspace, tmp_path, _svc(unmounted))
    root = tmp_path / "repos"
    assert _routes(indexed) == {"GET /outer", "GET /x"}

    edit(root, "svc", "src/main.py", _main())
    sync_all(indexed, root)
    assert _routes(indexed) == {"GET /outer"}
    assert_equals_fresh(indexed, settings, root, tmp_path, "mounted")

    edit(root, "svc", "src/main.py", unmounted)
    sync_all(indexed, root)
    assert _routes(indexed) == {"GET /outer", "GET /x"}
    assert_equals_fresh(indexed, settings, root, tmp_path, "unmounted")

    edit(root, "svc", "src/main.py", _main())
    sync_all(indexed, root)
    edit(root, "svc", ROUTES_FILE, ROUTES.replace("return {}", 'return {"x": 1}'))
    sync_all(indexed, root)
    assert _routes(indexed) == {"GET /outer"}
    assert_equals_fresh(indexed, settings, root, tmp_path, "body")

    # the name the mount imports is no longer declared there, and no application has it
    edit(root, "svc", "src/sub.py", _sub("sub2"))
    sync_all(indexed, root)
    assert _routes(indexed) == {"GET /outer", "GET /x"}
    assert_equals_fresh(indexed, settings, root, tmp_path, "renamed")


# -- a mounted sub-application's own routes, under the mount ---------------------------

SUB_DEEP = d("""
    from fastapi import FastAPI

    sub = FastAPI()


    @sub.get("/deep")
    def read_deep(): ...
""")
SUB_WITH_ROUTER = d("""
    from fastapi import FastAPI

    from src.routes import router

    sub = FastAPI()
    sub.include_router(router, prefix="/v1")


    @sub.get("/deep")
    def read_deep(): ...
""")
DEEP_DECORATOR = '@sub.get("/deep")'
DEEP = ("src/sub.py", "read_deep", "GET /deep")
TWO = d("""
    from fastapi import FastAPI

    sub2 = FastAPI()


    @sub2.get("/two")
    def read_two(): ...
""")
BASE = {"GET /outer", "GET /deep"}


def _deep(main: str, extra: dict[str, str] | None = None) -> dict[str, dict[str, str]]:
    """``src/main.py`` (the given text), and ``src/sub.py`` holding an application with a route."""
    files = {"src/__init__.py": "", "src/sub.py": SUB_DEEP, "src/main.py": main}
    return {"svc": {**files, **(extra or {})}}


def _first(prelude: str = "", argument: str = "sub2") -> str:
    """``src/a.py``: an application that mounts ``src/b.py``'s at ``/b``."""
    return "\n".join(
        [
            "from fastapi import FastAPI",
            "",
            "from src.b import sub2",
            prelude,
            "sub1 = FastAPI()",
            f'sub1.mount("/b", {argument})',
            "",
        ]
    )


def _edges_to(workspace: Workspace, route: str) -> int:
    rows = workspace.database.execute(
        """
        SELECT COUNT(*)
        FROM edges e
        JOIN entities d ON d.id = e.dst_id
        WHERE e.type = 'SERVES_HTTP_ROUTE' AND d.name = ?
        """,
        (route,),
    ).fetchall()
    return int(rows[0][0])


def _plan(workspace: Workspace) -> HttpClaimPlan:
    return ReferenceLinker(workspace.knowledge).prepare_global().http


def _claim_ids(workspace: Workspace) -> dict[tuple[str, str, str], int]:
    """Each Python route claim's id, by (file, handler, declared route)."""
    rows = workspace.database.execute(
        """
        SELECT u.id, f.rel_path, s.qualified_name, u.target_key FROM unresolved_refs u
        JOIN entities s ON s.id = u.src_id JOIN files f ON f.id = u.file_id
        WHERE u.meta_json LIKE '%"python-route"%'
        """
    ).fetchall()
    return {(row[1], row[2], row[3]): row[0] for row in rows}


def _views(workspace: Workspace) -> dict[str, set[str]]:
    """Each served route body → the files of the applications that serve it."""
    paths = {
        row[0]: row[1]
        for row in workspace.database.execute("SELECT id, rel_path FROM files").fetchall()
    }
    found: dict[str, set[str]] = {}
    for route in _plan(workspace).code_routes:
        found.setdefault(route.body, set()).update(paths[root] for root in route.roots)
    return found


def test_a_mounted_sub_applications_own_route_is_served_under_the_mount(
    workspace: Workspace, tmp_path: Path
) -> None:
    main = _main()
    indexed = build(workspace, tmp_path, _deep(main))
    assert served(indexed) == {
        ("svc", "src/main.py", "outer", "GET /outer", line(main, OUTER_DECORATOR)),
        ("svc", "src/sub.py", "read_deep", "GET /deep", line(SUB_DEEP, DEEP_DECORATOR)),
        ("svc", "src/sub.py", "read_deep", "GET /sub/deep", None),
    }
    # The route the child's file states is one edge, not two.
    assert _edges_to(indexed, "GET /deep") == 1


def test_each_view_carries_its_own_paths(workspace: Workspace, tmp_path: Path) -> None:
    indexed = build(workspace, tmp_path, _deep(_main()))
    views = _views(indexed)
    assert views["GET /sub/deep"] == {"src/main.py"}
    assert views["GET /deep"] == {"src/sub.py"}
    assert views["GET /outer"] == {"src/main.py"}
    assert _plan(indexed).promoted[_claim_ids(indexed)[DEEP]] == ("GET /sub/deep",)


@pytest.mark.parametrize("guard", ["", 'if __name__ == "__main__":\n    uvicorn.run(sub)\n'])
def test_an_unmounted_application_keeps_only_its_own_route(
    workspace: Workspace, tmp_path: Path, guard: str
) -> None:
    source = "import uvicorn\n" + SUB_DEEP + (f"\n\n{guard}" if guard else "")
    indexed = build(workspace, tmp_path, {"svc": {"src/__init__.py": "", "src/sub.py": source}})
    assert served(indexed) == {
        ("svc", "src/sub.py", "read_deep", "GET /deep", line(source, DEEP_DECORATOR))
    }
    plan = _plan(indexed)
    claim = _claim_ids(indexed)[DEEP]
    assert plan.routes[claim] == SERVED_HERE
    assert claim not in plan.declared


def test_the_mount_claim_carries_the_literal_path_beside_its_unchanged_fields() -> None:
    [claim] = _roles(_main(), PYTHON_MOUNT)
    assert claim.meta["mount_prefix"] == '"/sub"'
    assert (claim.meta["readable"], claim.meta["prefix"]) == ("false", "null")
    assert (claim.meta["call"], claim.meta["owner"], claim.meta["owner_kind"]) == (
        "mount",
        "app",
        "fastapi-app",
    )


def test_a_literal_route_on_a_fastapi_application_is_a_served_here_claim() -> None:
    [claim] = _roles(SUB_DEEP, PYTHON_ROUTE)
    assert claim.target_key == "GET /deep"
    assert claim.meta["served_here"] == "true"
    assert (claim.meta["router"], claim.meta["family"]) == ("sub", "fastapi")
    assert claim.meta["path"] == '[["t", "/deep"]]'


def test_a_flask_applications_literal_route_gets_no_claim() -> None:  # N16
    flask = d("""
        from flask import Flask

        app = Flask(__name__)


        @app.route("/x")
        def x(): ...
    """)
    assert _served(flask) == {"GET /x"}
    assert _roles(flask, PYTHON_ROUTE) == []


def test_a_sub_application_mounted_at_two_paths_is_served_at_each(
    workspace: Workspace, tmp_path: Path
) -> None:
    main = _main(mount='app.mount("/a", sub)\napp.mount("/b", sub)')
    indexed = build(workspace, tmp_path, _deep(main))
    assert _routes(indexed) == BASE | {"GET /a/deep", "GET /b/deep"}
    assert _edges_to(indexed, "GET /deep") == 1


def test_a_nested_sub_application_is_served_under_every_mount_above_it(
    workspace: Workspace, tmp_path: Path
) -> None:
    main = _main(imports="from src.a import sub1", mount='app.mount("/a", sub1)')
    extra = {"src/sub.py": "", "src/a.py": _first(), "src/b.py": TWO}
    indexed = build(workspace, tmp_path, _deep(main, extra))
    assert _routes(indexed) == {"GET /outer", "GET /two", "GET /b/two", "GET /a/b/two"}
    views = _views(indexed)
    assert views["GET /two"] == {"src/b.py"}
    assert views["GET /b/two"] == {"src/a.py"}
    assert views["GET /a/b/two"] == {"src/main.py"}


def test_a_factory_parent_serves_the_sub_application_under_the_mount(
    workspace: Workspace, tmp_path: Path
) -> None:
    main = d("""
        from fastapi import FastAPI

        from src.sub import sub


        def create_app():
            app = FastAPI()
            app.mount("/sub", sub)
            return app
    """)
    indexed = build(workspace, tmp_path, _deep(main))
    assert _routes(indexed) == {"GET /deep", "GET /sub/deep"}
    assert _views(indexed)["GET /sub/deep"] == {"src/main.py"}


# -- one assignment away from the import ----------------------------------------------


@pytest.mark.parametrize(
    "imports",
    ["from src.sub import sub\ns = sub", "import src.sub as m\ns = m.sub"],
)
def test_an_alias_of_an_imported_application_is_mounted_as_the_application_is(
    workspace: Workspace, tmp_path: Path, imports: str
) -> None:
    main = _main(imports=imports, mount='app.mount("/s", s)')
    [claim] = _roles(main, PYTHON_MOUNT)
    assert (claim.target_key, claim.meta["module"], claim.meta["mount_prefix"]) == (
        "sub",
        "src.sub",
        '"/s"',
    )
    indexed = build(workspace, tmp_path, _deep(main))
    assert _routes(indexed) == BASE | {"GET /s/deep"}


def test_aliases_on_both_hops_of_a_nested_mount_are_followed(
    workspace: Workspace, tmp_path: Path
) -> None:
    main = _main(imports="from src.a import sub1\nalias1 = sub1", mount='app.mount("/a", alias1)')
    extra = {
        "src/sub.py": "",
        "src/a.py": _first(prelude="alias2 = sub2", argument="alias2"),
        "src/b.py": TWO,
    }
    indexed = build(workspace, tmp_path, _deep(main, extra))
    assert _routes(indexed) == {"GET /outer", "GET /two", "GET /b/two", "GET /a/b/two"}


def test_an_alias_does_not_change_what_the_sub_applications_own_includes_do(
    workspace: Workspace, tmp_path: Path
) -> None:
    main = _main(imports="from src.sub import sub\ns = sub", mount='app.mount("/s", s)')
    extra = {"src/sub.py": SUB_WITH_ROUTER, ROUTES_FILE: ROUTES}
    indexed = build(workspace, tmp_path, _deep(main, extra))
    assert _routes(indexed) == BASE | {"GET /s/deep"}
    assert not {"GET /x", "GET /v1/x", "GET /s/v1/x"} & _routes(indexed)
    assert outcomes(indexed)[READ_X] == UNREADABLE_MOUNT


def test_adding_and_removing_the_alias_mount_moves_the_routes_incrementally(
    workspace: Workspace, settings, tmp_path: Path
) -> None:
    imports = "from src.sub import sub\ns = sub"
    unmounted = _main(imports=imports, mount="")
    mounted = _main(imports=imports, mount='app.mount("/s", s)')
    indexed = build(workspace, tmp_path, _deep(unmounted))
    root = tmp_path / "repos"
    assert _routes(indexed) == BASE

    edit(root, "svc", "src/main.py", mounted)
    sync_all(indexed, root)
    assert _routes(indexed) == BASE | {"GET /s/deep"}
    assert_equals_fresh(indexed, settings, root, tmp_path, "mounted")

    edit(root, "svc", "src/sub.py", SUB_DEEP.replace('"/deep"', '"/deeper"'))
    sync_all(indexed, root)
    assert _routes(indexed) == {"GET /outer", "GET /deeper", "GET /s/deeper"}
    assert_equals_fresh(indexed, settings, root, tmp_path, "child edited")

    edit(root, "svc", "src/sub.py", SUB_DEEP)
    edit(root, "svc", "src/main.py", unmounted)
    sync_all(indexed, root)
    assert _routes(indexed) == BASE
    assert_equals_fresh(indexed, settings, root, tmp_path, "unmounted")


def test_mounting_the_alias_of_a_local_sub_application_serves_it_nowhere() -> None:  # T12
    tail = """
        sub = FastAPI()
        sub.include_router(r)


        @sub.get("/deep")
        def deep(): ...


        s = sub


        @app.get("/outer")
        def outer(): ...


    """
    source = _source(d(tail) + 'app.mount("/s", s)\n')
    assert _served(source) == {"GET /outer"}
    assert _receiver(source, "sub").meta["usable"] == "false"
    assert "GET /deep" in _served(_source(tail))


# -- what is not proven stays as it was ------------------------------------------

_STAR = d("""
    from src.sub import *
    from fastapi import FastAPI

    s = sub
    app = FastAPI()
    app.mount("/s", s)
""")


@pytest.mark.parametrize(
    ("imports", "mount", "more"),
    [
        # N1: two bindings of the alias, in either order
        ("from src.sub import sub\nfrom src.other import other\ns = sub\ns = other", "s", ""),
        ("from src.sub import sub\nfrom src.other import other\ns = other\ns = sub", "s", ""),
        # N2: rebound after the mount
        ("from src.sub import sub\ns = sub", "s", "s = None"),
        ("from src.sub import sub\ns = sub", "s", "for s in ():\n    pass"),
        # N3: shadowed by a parameter, or by a function's local
        ("from src.sub import sub\ns = sub", "s", "def helper(s): ..."),
        ("from src.sub import sub\ns = sub", "s", "def helper():\n    s = 1"),
        # N4: bound under a condition
        ("from src.sub import sub\nFLAG = True\nif FLAG:\n    s = sub", "s", ""),
        # N7: an alias of an alias
        ("from src.sub import sub\nt = sub\ns = t", "s", ""),
        # N9: before the import, and after the mount
        ("s = sub\nfrom src.sub import sub", "s", ""),
        ("from src.sub import sub", "s", "s = sub"),
    ],
)
def test_an_alias_that_is_not_proven_mounts_nothing(
    workspace: Workspace, tmp_path: Path, imports: str, mount: str, more: str
) -> None:
    main = _main(imports=imports, mount=f'app.mount("/s", {mount})', more=more)
    assert _roles(main, PYTHON_MOUNT) == []
    indexed = build(workspace, tmp_path, _deep(main))
    assert _routes(indexed) == BASE


def test_a_wildcard_import_makes_no_alias(workspace: Workspace, tmp_path: Path) -> None:  # N8
    assert _roles(_STAR, PYTHON_MOUNT) == []
    indexed = build(workspace, tmp_path, _deep(_STAR))
    assert "GET /deep" in _routes(indexed)
    assert "GET /s/deep" not in _routes(indexed)


def test_an_alias_of_an_unresolvable_import_blocks_the_applications_of_its_name(
    workspace: Workspace, tmp_path: Path
) -> None:  # N5
    main = _main(imports="from missing.mod import sub\ns = sub", mount='app.mount("/s", s)')
    [claim] = _roles(main, PYTHON_MOUNT)
    assert (claim.target_key, claim.meta["module"]) == ("sub", "missing.mod")
    indexed = build(workspace, tmp_path, _deep(main))
    assert _routes(indexed) == BASE
    assert outcomes(indexed)[DEEP] == SERVED_HERE


def test_a_mount_with_dynamic_arguments_proves_no_path(
    workspace: Workspace, tmp_path: Path
) -> None:  # N6
    main = _main(mount='opts = {}\napp.mount("/s", sub, **opts)')
    [claim] = _roles(main, PYTHON_MOUNT)
    assert "mount_prefix" not in claim.meta
    assert claim.meta["prefix"] == "null"
    extra = {"src/sub.py": SUB_WITH_ROUTER, ROUTES_FILE: ROUTES}
    indexed = build(workspace, tmp_path, _deep(main, extra))
    assert _routes(indexed) == BASE
    assert outcomes(indexed)[READ_X] == UNREADABLE_MOUNT

    computed = _main(imports="import src.sub as m", mount='app.mount("/s", getattr(m, "sub"))')
    assert _roles(computed, PYTHON_MOUNT) == []


def test_an_alias_of_a_router_is_not_an_include() -> None:  # N10
    source = d("""
        from fastapi import FastAPI
        from src.routes import router

        app = FastAPI()
        r = router
        app.include_router(r)
    """)
    assert _roles(source, PYTHON_MOUNT) == []
    [claim] = _roles(source.replace("include_router(r)", "include_router(router)"), PYTHON_MOUNT)
    assert claim.meta["call"] == "include_router"


@pytest.mark.parametrize(
    ("imports", "mount"),
    [
        ("from src.sub import sub", 'app.mount("", sub)'),
        ("from src.sub import sub", 'app.mount("/sub/", sub)'),
        ("from src.sub import sub", 'app.mount("sub", sub)'),
        ("from src.sub import sub\nfrom src.consts import PREFIX", "app.mount(PREFIX, sub)"),
        ("from src.sub import sub", "app.mount(make_path(), sub)"),
    ],
)
def test_a_mount_path_that_is_no_literal_fastapi_prefix_proves_no_mount_path(
    workspace: Workspace, tmp_path: Path, imports: str, mount: str
) -> None:  # N11
    main = _main(imports=imports, mount=mount)
    [claim] = _roles(main, PYTHON_MOUNT)
    assert "mount_prefix" not in claim.meta
    indexed = build(workspace, tmp_path, _deep(main, {"src/consts.py": 'PREFIX = "/sub"\n'}))
    assert _routes(indexed) == BASE
    assert _edges_to(indexed, "GET /deep") == 1


@pytest.mark.parametrize("owner", ["module", "factory"])
def test_an_owner_mounted_in_its_own_file_serves_none_of_its_childrens_paths(
    workspace: Workspace, tmp_path: Path, owner: str
) -> None:  # N12
    if owner == "module":
        first = d("""
            from fastapi import FastAPI

            from src.b import sub2

            app = FastAPI()
            sub1 = FastAPI()
            sub1.mount("/b", sub2)
            app.mount("/a", sub1)
        """)
    else:
        first = d("""
            from fastapi import FastAPI

            from src.b import sub2


            def create_app():
                app = FastAPI()
                sub1 = FastAPI()
                sub1.mount("/b", sub2)
                app.mount("/a", sub1)
                return app
        """)
    [claim] = _roles(first, PYTHON_MOUNT)
    assert "mount_prefix" not in claim.meta
    files = {"svc": {"src/__init__.py": "", "src/a.py": first, "src/b.py": TWO}}
    indexed = build(workspace, tmp_path, files)
    assert _routes(indexed) == {"GET /two"}


def test_a_path_without_its_leading_slash_is_not_glued_to_the_mount(
    workspace: Workspace, tmp_path: Path
) -> None:  # N14
    sub = SUB_DEEP.replace('"/deep"', '"deep"')
    indexed = build(workspace, tmp_path, _deep(_main(), {"src/sub.py": sub}))
    assert "GET /subdeep" not in _routes(indexed)
    found = [outcome for key, outcome in outcomes(indexed).items() if key[1] == "read_deep"]
    assert found == [SERVED_HERE]


def test_applications_mounting_each_other_are_not_walked_round(
    workspace: Workspace, tmp_path: Path
) -> None:  # N15
    first = d("""
        from fastapi import FastAPI

        from src.b import b

        a = FastAPI()
        a.mount("/a", b)
    """)
    second = d("""
        from fastapi import FastAPI

        from src.a import a

        b = FastAPI()
        b.mount("/b", a)


        @b.get("/p")
        def read_p(): ...
    """)
    files = {"svc": {"src/__init__.py": "", "src/a.py": first, "src/b.py": second}}
    indexed = build(workspace, tmp_path, files)
    assert _routes(indexed) == {"GET /p", "GET /a/p"}
