"""Python routes served through a mount in another module (deployment HTTP roadmap, M1).

A router or blueprint declared in one module is served where another module -- the
application's own, or an application factory -- includes it, with every prefix on the
way. The fixture repositories cover the shapes the work survey found: script-style
imports from a service's own directory, routers mounted in a loop, paths built from
imported constants, application factories, and a package naming itself absolutely.
Everything is asserted as closed sets and exact lines.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from repoatlas.graph.linker import ReferenceLinker, plan_repository_http
from repoatlas.graph.py_routes import (
    CONSTANT_UNRESOLVED,
    NOT_MOUNTED,
    PROMOTED,
    UNREADABLE_MOUNT,
)
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build
from tests.incremental_corpus import assert_equals_fresh, edit, sync_all


def d(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


#: A FastAPI service run as a script from its own `src/`: the shape of the survey's edge.
METERS_APP = d('''
    """meters: readings and alarms."""
    from fastapi import FastAPI

    from api.v1 import alarms as alarms_v1
    from web.admin import admin_router
    from web.legacy import legacy_router
    from web.nested import outer
    from web.readings import readings_router
    from web.spread import spread_router

    OPTIONS = {"prefix": "/spread"}

    app = FastAPI(title="meters")

    routers = [readings_router, alarms_v1.router, outer]
    for router in routers:
        app.include_router(router)

    app.include_router(admin_router, prefix="/admin")
    app.include_router(spread_router, **OPTIONS)
    app.mount("/legacy", legacy_router)


    @app.get("/health")
    def health():
        return {"status": "ok"}


    if __name__ == "__main__":
        import uvicorn

        uvicorn.run(app, host="0.0.0.0", port=5000)
''')

METERS = {
    "meters/src/meters-service.py": METERS_APP,
    "meters/src/service_prefix.py": 'PREFIX = "meters"\nVERSION = "v1"\n',
    "meters/src/web/__init__.py": "",
    "meters/src/web/readings.py": d("""
        from fastapi import APIRouter

        readings_router = APIRouter(prefix="/readings")


        @readings_router.get("")
        def list_readings():
            return []


        @readings_router.get("/{reading_id}")
        def get_reading(reading_id: int):
            return {"id": reading_id}
    """),
    # api/ and api/v1/ are namespace directories: no __init__.py, as script-style code has.
    "meters/src/api/v1/alarms.py": d("""
        from fastapi import APIRouter

        from service_prefix import PREFIX, VERSION

        router = APIRouter()


        @router.get(f"/{PREFIX}/{VERSION}" + "/alarms")
        def list_alarms():
            return []


        @router.post("/alarms/ack")
        def acknowledge():
            return {"ok": True}
    """),
    "meters/src/web/admin.py": d("""
        from fastapi import APIRouter

        admin_router = APIRouter()


        @admin_router.post("/reset")
        def reset():
            return {"reset": True}
    """),
    "meters/src/web/legacy.py": d("""
        from fastapi import APIRouter

        legacy_router = APIRouter()


        @legacy_router.get("/old")
        def old():
            return {}
    """),
    "meters/src/web/spread.py": d("""
        from fastapi import APIRouter

        spread_router = APIRouter()


        @spread_router.get("/thing")
        def thing():
            return {}
    """),
    "meters/src/web/nested.py": d("""
        from fastapi import APIRouter

        from web.inner import inner_router

        outer = APIRouter(prefix="/outer")
        outer.include_router(inner_router, prefix="/in")


        @outer.get("/top")
        def top():
            return {}
    """),
    "meters/src/web/inner.py": d("""
        from fastapi import APIRouter

        inner_router = APIRouter(prefix="/inner")


        @inner_router.get("/x")
        def inner_x():
            return {}
    """),
    "meters/src/web/unused.py": d("""
        from fastapi import APIRouter

        unused_router = APIRouter()


        @unused_router.get("/never")
        def never():
            return {}
    """),
    # A second service in the same repository, with modules of the same names: each
    # script's imports resolve in its own directory.
    "gauges/src/gauges-service.py": d("""
        from fastapi import FastAPI

        from web.readings import readings_router

        app = FastAPI(title="gauges")
        app.include_router(readings_router)
    """),
    "gauges/src/web/readings.py": d("""
        from fastapi import APIRouter

        readings_router = APIRouter(prefix="/gauge-readings")


        @readings_router.get("/latest")
        def latest():
            return {}
    """),
    # A constant bound twice, and one re-exported from another module: neither is read.
    "gauges/src/twice.py": 'PREFIX = "a"\nPREFIX = "b"\n',
    "gauges/src/base_prefix.py": 'ROOT = "base"\n',
    "gauges/src/reexport.py": "from base_prefix import ROOT\n",
    "gauges/src/web/odd.py": d("""
        from fastapi import APIRouter

        from reexport import ROOT
        from twice import PREFIX

        odd_router = APIRouter()


        @odd_router.get(f"/{PREFIX}/twice")
        def bound_twice():
            return {}


        @odd_router.get(f"/{ROOT}/reexported")
        def reexported():
            return {}
    """),
    "gauges/src/odd-service.py": d("""
        from fastapi import FastAPI

        from web.odd import odd_router

        app = FastAPI(title="odd")
        app.include_router(odd_router)
    """),
}

#: A Flask application factory in a package, blueprints in subpackages and modules.
SHOP = {
    "shop/__init__.py": d("""
        from flask import Flask

        DEBUG = False


        def create_app():
            app = Flask(__name__)
            from .orders import orders as orders_bp

            app.register_blueprint(orders_bp, url_prefix="/orders")
            from . import catalog

            app.register_blueprint(catalog.bp)
            from .debug import debug_bp

            if DEBUG:
                app.register_blueprint(debug_bp)
            register_extras(app)
            return app


        def register_extras(app):
            from .extras import extras_bp

            app.register_blueprint(extras_bp)


        def create_admin():
            app = Flask(__name__)
            app = wrap(app)
            from .admin import admin_bp

            app.register_blueprint(admin_bp)
            return app
    """),
    "shop/admin.py": d("""
        from flask import Blueprint

        admin_bp = Blueprint("admin", __name__)


        @admin_bp.route("/admin")
        def admin():
            return {}
    """),
    "shop/orders/__init__.py": d("""
        from flask import Blueprint

        orders = Blueprint("orders", __name__)

        from . import views  # noqa: E402,F401
    """),
    "shop/orders/views.py": d("""
        from . import orders


        @orders.route("/<int:order_id>")
        def get_order(order_id):
            return {}


        @orders.route("/", methods=["GET", "POST"])
        def list_orders():
            return []
    """),
    "shop/catalog.py": d("""
        from flask import Blueprint

        bp = Blueprint("catalog", __name__, url_prefix="/catalog")


        @bp.get("/items")
        def items():
            return []
    """),
    "shop/debug.py": d("""
        from flask import Blueprint

        debug_bp = Blueprint("debug", __name__)


        @debug_bp.route("/debug")
        def debug():
            return {}
    """),
    "shop/extras.py": d("""
        from flask import Blueprint

        extras_bp = Blueprint("extras", __name__)


        @extras_bp.route("/extras")
        def extras():
            return {}
    """),
}

#: A package that names itself absolutely: `uvicorn src.app:app` from the repository root.
PEERS = {
    "src/__init__.py": "",
    "src/app.py": d("""
        from fastapi import FastAPI

        from src.interfaces.peers import peer_router

        app = FastAPI()
        app.include_router(peer_router)
    """),
    "src/interfaces/__init__.py": "",
    "src/interfaces/peers.py": d("""
        from fastapi import APIRouter

        peer_router = APIRouter()


        @peer_router.get("/vpn/{vpn_name}/peer/tag/{tag}")
        def get_peer_by_tag(vpn_name: str, tag: str):
            return {}
    """),
}

#: Another repository with the same module path, never mounted: nothing crosses.
ELSEWHERE = {
    "meters/src/web/readings.py": d("""
        from fastapi import APIRouter

        readings_router = APIRouter()


        @readings_router.get("/elsewhere")
        def elsewhere():
            return {}
    """),
}

WORKSPACE = {"fleet": METERS, "shop": SHOP, "peers": PEERS, "elsewhere": ELSEWHERE}


def line(text: str, needle: str) -> int:
    return next(number for number, row in enumerate(text.splitlines(), 1) if needle in row)


def served(workspace: Workspace) -> set[tuple[str, str, str, str, int | None]]:
    """(repository, file, handler, route, decorator line or None) for every served route."""
    rows = workspace.database.execute(
        """
        SELECT r.slug, f.rel_path, s.qualified_name, d.name, e.line_start
        FROM edges e
        JOIN entities s ON s.id = e.src_id
        JOIN entities d ON d.id = e.dst_id
        JOIN files f ON f.id = s.file_id
        JOIN repositories r ON r.id = s.repo_id
        WHERE e.type = 'SERVES_HTTP_ROUTE'
        """
    ).fetchall()
    return {tuple(row) for row in rows}


def outcomes(workspace: Workspace) -> dict[tuple[str, str, str], str]:
    """Each Python route claim's outcome, by (file, handler, declared route)."""
    plan = ReferenceLinker(workspace.knowledge).prepare_global().http
    rows = workspace.database.execute(
        """
        SELECT u.id, f.rel_path, s.qualified_name, u.target_key FROM unresolved_refs u
        JOIN entities s ON s.id = u.src_id JOIN files f ON f.id = u.file_id
        WHERE u.meta_json LIKE '%"python-route"%'
        """
    ).fetchall()
    return {(row[1], row[2], row[3]): plan.routes[row[0]] for row in rows}


R = "meters/src/web/readings.py"
A = "meters/src/api/v1/alarms.py"
N = "meters/src/web/nested.py"
EXPECTED = {
    # an application's own route, read in its own file as before
    (
        "fleet",
        "meters/src/meters-service.py",
        "health",
        "GET /health",
        line(METERS_APP, '@app.get("/health")'),
    ),
    # a router's own prefix, mounted in a loop: composed
    ("fleet", R, "list_readings", "GET /readings", None),
    ("fleet", R, "get_reading", "GET /readings/{reading_id}", None),
    # through namespace directories, the path from imported constants: composed
    ("fleet", A, "list_alarms", "GET /meters/v1/alarms", None),
    # nothing adds to it: exact, with the decorator's lines
    (
        "fleet",
        A,
        "acknowledge",
        "POST /alarms/ack",
        line(METERS["meters/src/api/v1/alarms.py"], "@router.post"),
    ),
    # a prefix at the mount
    ("fleet", "meters/src/web/admin.py", "reset", "POST /admin/reset", None),
    # nested: application, outer router, its include of the inner one
    ("fleet", N, "top", "GET /outer/top", None),
    ("fleet", "meters/src/web/inner.py", "inner_x", "GET /outer/in/inner/x", None),
    # the other service's module of the same name, in its own script directory
    ("fleet", "gauges/src/web/readings.py", "latest", "GET /gauge-readings/latest", None),
    # a factory: a subpackage's blueprint at the mount's prefix, a module's at its own
    ("shop", "shop/orders/views.py", "get_order", "GET /orders/<int:order_id>", None),
    ("shop", "shop/orders/views.py", "list_orders", "GET /orders/", None),
    ("shop", "shop/orders/views.py", "list_orders", "POST /orders/", None),
    ("shop", "shop/catalog.py", "items", "GET /catalog/items", None),
    # a package naming itself absolutely, nothing added: exact
    (
        "peers",
        "src/interfaces/peers.py",
        "get_peer_by_tag",
        "GET /vpn/{vpn_name}/peer/tag/{tag}",
        line(PEERS["src/interfaces/peers.py"], "@peer_router.get"),
    ),
}


@pytest.fixture
def indexed(workspace: Workspace, tmp_path: Path) -> Workspace:
    return build(workspace, tmp_path, WORKSPACE)


def test_routes_are_served_exactly_where_a_proven_mount_chain_reaches(indexed: Workspace) -> None:
    assert served(indexed) == EXPECTED


def test_what_is_not_served_says_why(indexed: Workspace) -> None:
    found = outcomes(indexed)
    assert found[("meters/src/web/legacy.py", "old", "GET /old")] == UNREADABLE_MOUNT
    assert found[("meters/src/web/spread.py", "thing", "GET /thing")] == UNREADABLE_MOUNT
    assert found[("meters/src/web/unused.py", "never", "GET /never")] == NOT_MOUNTED
    assert found[("gauges/src/web/odd.py", "bound_twice", "GET /{twice.PREFIX}/twice")] == (
        CONSTANT_UNRESOLVED
    )
    assert found[("gauges/src/web/odd.py", "reexported", "GET /{reexport.ROOT}/reexported")] == (
        CONSTANT_UNRESOLVED
    )
    # a conditional mount, and a mount on a parameter, are not read
    assert found[("shop/debug.py", "debug", "GET /debug")] == NOT_MOUNTED
    assert found[("shop/extras.py", "extras", "GET /extras")] == NOT_MOUNTED
    # nor one on a factory's application it rebinds
    assert found[("shop/admin.py", "admin", "GET /admin")] == NOT_MOUNTED
    # another repository's module of the same path is never resolved to
    assert found[("meters/src/web/readings.py", "elsewhere", "GET /elsewhere")] == NOT_MOUNTED
    assert found[(R, "list_readings", "GET ")] == PROMOTED


def test_a_repository_plans_its_own_routes_alone(indexed: Workspace) -> None:
    """The inventory's replan of one repository is exactly its share of the global plan."""
    plan = ReferenceLinker(indexed.knowledge).prepare_global().http
    for slug in ("fleet", "shop", "peers", "elsewhere"):
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


def test_a_router_nested_deeper_than_the_bound_is_not_served(
    workspace: Workspace, tmp_path: Path
) -> None:
    files = {
        "chain/src/chain-service.py": d("""
        from fastapi import FastAPI

        from r0 import r0

        app = FastAPI()
        app.include_router(r0)
    """)
    }
    for depth in range(7):
        below = depth + 1
        child = f"from r{below} import r{below}\n" if depth < 6 else ""
        include = f'r{depth}.include_router(r{below}, prefix="/d{below}")\n' if depth < 6 else ""
        files[f"chain/src/r{depth}.py"] = (
            "from fastapi import APIRouter\n"
            + child
            + f"\nr{depth} = APIRouter()\n"
            + include
            + f'\n\n@r{depth}.get("/leaf")\ndef leaf{depth}():\n    return {{}}\n'
        )
    indexed = build(workspace, tmp_path, {"chain": files})
    routes = {route for _, _, _, route, _ in served(indexed)}
    assert routes == {
        "GET /leaf",
        "GET /d1/leaf",
        "GET /d1/d2/leaf",
        "GET /d1/d2/d3/leaf",
        "GET /d1/d2/d3/d4/leaf",
    }


# -- incremental equals fresh --------------------------------------------------------------


def test_editing_a_mount_prefix_moves_the_routes(
    indexed: Workspace, settings, tmp_path: Path
) -> None:
    root = tmp_path / "repos"
    edit(
        root,
        "fleet",
        "meters/src/meters-service.py",
        METERS_APP.replace('prefix="/admin"', 'prefix="/ops"'),
    )
    sync_all(indexed, root)
    routes = {route for *_, route, _ in served(indexed)}
    assert "POST /ops/reset" in routes and "POST /admin/reset" not in routes
    assert_equals_fresh(indexed, settings, root, tmp_path, "prefix")


def test_editing_an_imported_constant_moves_the_route(
    indexed: Workspace, settings, tmp_path: Path
) -> None:
    root = tmp_path / "repos"
    edit(root, "fleet", "meters/src/service_prefix.py", 'PREFIX = "meters"\nVERSION = "v2"\n')
    sync_all(indexed, root)
    routes = {route for *_, route, _ in served(indexed)}
    assert "GET /meters/v2/alarms" in routes and "GET /meters/v1/alarms" not in routes
    assert_equals_fresh(indexed, settings, root, tmp_path, "constant")


def test_removing_a_mount_unserves_the_router(indexed: Workspace, settings, tmp_path: Path) -> None:
    root = tmp_path / "repos"
    edit(
        root,
        "fleet",
        "meters/src/meters-service.py",
        METERS_APP.replace("[readings_router, alarms_v1.router, outer]", "[outer]"),
    )
    sync_all(indexed, root)
    routes = {route for *_, route, _ in served(indexed)}
    assert not {"GET /readings", "GET /meters/v1/alarms"} & routes
    assert "GET /outer/top" in routes
    assert_equals_fresh(indexed, settings, root, tmp_path, "unmount")


def test_a_body_edit_in_a_router_module_changes_no_route(
    indexed: Workspace, settings, tmp_path: Path
) -> None:
    root = tmp_path / "repos"
    before = served(indexed)
    edit(
        root,
        "fleet",
        R,
        METERS[R].replace("    return []", "    return [] if True else None"),
    )
    work = sync_all(indexed, root)
    assert served(indexed) == before
    assert work.repositories_invalidated == 0
    assert_equals_fresh(indexed, settings, root, tmp_path, "body")
