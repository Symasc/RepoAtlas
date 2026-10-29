"""Integration of M6 A1 (applications built from an imported class) with defect 9.1 (sub-application
mounts): an application built from an imported class that mounts a sub-application serves the
sub-application's routes under the mount, and its own paths only at the parent's root. Names the
parent reaches through a one-hop alias are followed as the module states them.
"""

from __future__ import annotations

from pathlib import Path

from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build
from tests.test_http_python import _served
from tests.test_http_python_subclasses_cross import USERS, module
from tests.test_http_python_routers import served
from tests.test_http_python_subapps import _views

BASE = module("from fastapi import FastAPI", "class CustomApp(FastAPI):", "    pass")
SUB = "\n".join(
    [
        "from fastapi import APIRouter, FastAPI",
        "",
        "sub = FastAPI()",
        "router = APIRouter()",
        "",
        "",
        '@sub.get("/deep")',
        "def read_deep(): ...",
        "",
        "",
        '@router.get("/x")',
        "def read_x(): ...",
        "",
        "sub.include_router(router)",
        "",
    ]
)


def routes(workspace: Workspace) -> set[str]:
    return {row[3] for row in served(workspace)}


def roots(workspace: Workspace, route: str) -> set[str]:
    """The files whose application serves ``route``, by the views each one carries."""
    return _views(workspace).get(route, set())


def test_a_sub_application_mounted_by_an_imported_class_application_serves_under_the_mount(
    workspace: Workspace, tmp_path: Path
) -> None:
    app = "\n".join(
        [
            "from base import CustomApp",
            "from sub import sub",
            "",
            "app = CustomApp()",
            'app.mount("/sub", sub)',
            "",
        ]
    )
    indexed = build(workspace, tmp_path, {"svc": {"src/base.py": BASE, "src/sub.py": SUB, "src/app.py": app}})
    # The sub-application's own path is served under the mount. A router included in the same
    # file as the mounted sub-application keeps the per-file view at its own root (a residual of
    # defect 9.1, pre-existing on master 0a7c376): GET /x is rooted at sub.py only, never at the
    # parent, and no GET /sub/x is served.
    assert routes(indexed) == {"GET /deep", "GET /sub/deep", "GET /x"}
    assert roots(indexed, "GET /x") == {"src/sub.py"}
    assert "GET /sub/x" not in routes(indexed)


def test_an_application_built_from_an_imported_class_mounts_nothing_at_its_root(
    workspace: Workspace, tmp_path: Path
) -> None:
    # The class application is mounted by a local sub-application under another path: its routes
    # are served nowhere at the root. (Same-module mount rule.)
    app = module(
        "from fastapi import FastAPI",
        "from base import CustomApp",
        "app = CustomApp()",
        "main = FastAPI()",
        'main.mount("/s", app)',
        *USERS,
    )
    indexed = build(workspace, tmp_path, {"svc": {"src/base.py": BASE, "src/app.py": app}})
    assert _served(app) == set()
    assert "GET /users" not in routes(indexed)


def test_an_alias_of_the_class_application_mounted_is_not_served_at_the_root(
    workspace: Workspace, tmp_path: Path
) -> None:
    # Correction 1: the mounted name is a one-hop alias of the class-built application, bound before
    # the mount: it is followed, so the application is no longer served at the root.
    app = module(
        "from fastapi import FastAPI",
        "from base import CustomApp",
        "app = CustomApp()",
        "alias = app",
        "main = FastAPI()",
        'main.mount("/s", alias)',
        *USERS,
    )
    indexed = build(workspace, tmp_path, {"svc": {"src/base.py": BASE, "src/app.py": app}})
    assert "GET /users" not in routes(indexed)


def test_a_sub_application_mounted_through_an_alias_is_served_under_the_mount(
    workspace: Workspace, tmp_path: Path
) -> None:
    # Correction 2 with a one-hop alias of the imported sub-application on the mount line.
    app = "\n".join(
        [
            "from base import CustomApp",
            "from sub import sub",
            "",
            "app = CustomApp()",
            "child = sub",
            'app.mount("/sub", child)',
            "",
        ]
    )
    indexed = build(workspace, tmp_path, {"svc": {"src/base.py": BASE, "src/sub.py": SUB, "src/app.py": app}})
    assert routes(indexed) == {"GET /deep", "GET /sub/deep", "GET /x"}
    assert roots(indexed, "GET /sub/deep") == {"src/app.py"}


def test_an_unproven_sub_application_mounted_by_an_imported_class_application_serves_no_mount(
    workspace: Workspace, tmp_path: Path
) -> None:
    # The sub-application is not imported from a proven module: nothing under the mount is served.
    app = "\n".join(
        [
            "from base import CustomApp",
            "from missing import sub",
            "",
            "app = CustomApp()",
            'app.mount("/sub", sub)',
            "",
        ]
    )
    indexed = build(workspace, tmp_path, {"svc": {"src/base.py": BASE, "src/sub.py": SUB, "src/app.py": app}})
    assert "GET /sub/deep" not in routes(indexed)
    assert "GET /sub/x" not in routes(indexed)
