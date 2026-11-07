"""An application built inside an entry function, from classes other modules define, that a helper
hands every router of a literal list to.

``main()`` binds ``app`` once per case of an exhaustive ``match`` (the last case raises), each to an
application class a package re-exports -- one extending the other, both with an ``__init__`` of
their own -- and hands it to ``setup_routes``, which includes each router of ``ROUTERS`` in a
loop. The routers are built from a router class of another module whose ``__init__`` keeps its own
state behind a property. Every step is read from the syntax tree only, never imported or run
(V1_SPEC §17); half of this file is what is refused.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from repoatlas.parsers.http_facts import HELPER_CALL_ROLE, HELPER_ROLE, PENDING_FACT
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build
from tests.test_http_python import _parse
from tests.test_http_python_routers import line, served


def d(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


APP = d("""
    from service import AuthApi, OpenApi
    from interfaces.items import item_router
    from interfaces.users import user_router

    ROUTERS = [item_router, user_router]


    def setup_routes(app: OpenApi) -> None:
        for router in ROUTERS:
            app.include_router(router)


    def main(mode: str) -> None:
        match mode:
            case "auth":
                app = AuthApi(client="reader")
            case "open":
                app = OpenApi(level="read")
            case _:
                raise ValueError(mode)
        setup_routes(app)


    if __name__ == "__main__":
        main("open")
""")

SERVICE = d("""
    from .auth import AuthApi
    from .open import OpenApi
""")

OPEN = d("""
    from fastapi import Depends, FastAPI


    class OpenApi(FastAPI):
        def __init__(self, level=None, **kwargs):
            self._level = level
            extra = kwargs.pop("dependencies", [])
            super().__init__(dependencies=[*extra, Depends(self._check)], **kwargs)

        def _check(self):
            return self._level
""")

AUTH = d("""
    from contextlib import asynccontextmanager

    from .open import OpenApi


    class AuthApi(OpenApi):
        def __init__(self, client):
            self._client = client
            super().__init__(level="full", lifespan=self._lifespan)

        @asynccontextmanager
        async def _lifespan(self, _):
            yield
""")

ROUTER = d("""
    from fastapi import APIRouter


    class ManagedRouter(APIRouter):
        def __init__(self):
            super().__init__()
            self._store = None

        @property
        def store(self):
            return self._store

        @store.setter
        def store(self, value):
            self._store = value
""")

ITEMS = d("""
    from interfaces.router import ManagedRouter

    item_router = ManagedRouter()


    @item_router.get("/items/{item_id}")
    def get_item(item_id: str):
        return {}
""")

USERS = d("""
    from interfaces.router import ManagedRouter

    user_router = ManagedRouter()


    @user_router.post("/users")
    def create_user():
        return {}
""")


def files(app: str = APP) -> dict[str, dict[str, str]]:
    return {
        "svc": {
            "src/app.py": app,
            "src/service/__init__.py": SERVICE,
            "src/service/open.py": OPEN,
            "src/service/auth.py": AUTH,
            "src/interfaces/router.py": ROUTER,
            "src/interfaces/items.py": ITEMS,
            "src/interfaces/users.py": USERS,
        }
    }


EXPECTED = {
    (
        "svc",
        "src/interfaces/items.py",
        "get_item",
        "GET /items/{item_id}",
        line(ITEMS, "@item_router.get("),
    ),
    (
        "svc",
        "src/interfaces/users.py",
        "create_user",
        "POST /users",
        line(USERS, "@user_router.post("),
    ),
}


def test_routers_handed_to_an_entry_function_s_application_are_served(
    workspace: Workspace, tmp_path: Path
) -> None:
    indexed = build(workspace, tmp_path, files())
    assert served(indexed) == EXPECTED


def test_the_helper_unrolls_its_loop_and_the_call_names_the_classes_of_each_case() -> None:
    claims = {
        r.meta[PENDING_FACT]: r
        for r in _parse(APP).references
        if r.meta.get(PENDING_FACT) in {HELPER_ROLE, HELPER_CALL_ROLE}
    }
    helper, call = claims[HELPER_ROLE], claims[HELPER_CALL_ROLE]
    assert helper.target_key == "setup_routes"
    assert '"name": "item_router"' in helper.meta["mounts"]
    assert '"name": "user_router"' in helper.meta["mounts"]
    assert (call.target_key, call.meta["owner"], call.meta["owner_kind"]) == (
        "setup_routes",
        "main.app",
        "",
    )
    assert '"class": "AuthApi"' in call.meta["owner_classes"]
    assert '"class": "OpenApi"' in call.meta["owner_classes"]
    assert "refusal" not in call.meta


@pytest.mark.parametrize(
    ("old", "new"),
    [
        # the match is not exhaustive: `app` may be unbound
        ("        case _:\n            raise ValueError(mode)\n", ""),
        # a case neither binds `app` nor raises
        ("            raise ValueError(mode)", "            pass"),
        # a case binds it to a router, which is no application
        (
            'app = OpenApi(level="read")',
            "app = ManagedRouter()",
        ),
        # the helper's loop reads no literal list
        ("for router in ROUTERS:", "for router in list(ROUTERS):"),
        # a class is read other than to build or extend it
        ("    setup_routes(app)\n", "    setup_routes(app)\n    OpenApi.get = None\n"),
    ],
    ids=["not-exhaustive", "branch-falls-through", "router-branch", "dynamic-loop", "escapes"],
)
def test_an_application_or_registration_that_is_not_proven_serves_nothing(
    workspace: Workspace, tmp_path: Path, old: str, new: str
) -> None:
    assert old in APP
    app = APP.replace(old, new)
    if "ManagedRouter()" in app:
        app = "from interfaces.router import ManagedRouter\n" + app
    indexed = build(workspace, tmp_path, files(app))
    assert served(indexed) == set()


def test_a_router_class_whose_init_names_a_prefix_serves_nothing(
    workspace: Workspace, tmp_path: Path
) -> None:
    named = files()
    named["svc"]["src/interfaces/router.py"] = ROUTER.replace(
        "super().__init__()", 'super().__init__(prefix="/v1")'
    )
    indexed = build(workspace, tmp_path, named)
    assert served(indexed) == set()
