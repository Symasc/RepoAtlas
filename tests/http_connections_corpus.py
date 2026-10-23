"""Neutral repositories for the HTTP Connections worklist and declarations.

``shop`` and ``orders`` serve routes; ``twin`` serves ``shop``'s route twice (two handlers);
``svc`` is published by its compose file, so the source links requests to it. ``buyer``, ``tools``
and ``batch`` make the requests the source cannot link: to a named host nothing declares, to
loopback (a different service in each repository), through a base the source does not state, and
into ``svc``'s published origin on a route it does not serve. Every name is invented.
"""

from __future__ import annotations

from textwrap import dedent

PYPROJECT = '[project]\nname = "{name}"\nversion = "1.0.0"\ndependencies = ["{dep}"]\n'

SHOP_APP = dedent("""\
    from fastapi import FastAPI

    app = FastAPI()


    @app.get("/v1/items/{item_id}")
    def get_item(item_id: str) -> dict:
        return {"id": item_id}


    @app.post("/v1/items")
    def create_item() -> dict:
        return {}
""")
ORDERS_APP = dedent("""\
    from fastapi import FastAPI

    app = FastAPI()


    @app.get("/v1/orders/{order_id}")
    def get_order(order_id: str) -> dict:
        return {"id": order_id}
""")
TWIN_APP = dedent("""\
    from fastapi import FastAPI

    app = FastAPI()


    @app.get("/v1/items/{item_id}")
    def get_item(item_id: str) -> dict:
        return {"id": item_id}
""")
COMPOSE = {
    "docker-compose.yml": dedent("""\
        services:
          svc:
            build:
              context: .
              dockerfile: Dockerfile
            ports:
              - "8080:8080"
    """),
    "Dockerfile": (
        "FROM python:3.12-slim\nWORKDIR /srv\nCOPY svc /srv/svc\n"
        'CMD ["uvicorn", "svc.main:app", "--host", "0.0.0.0", "--port", "8080"]\n'
    ),
}
BUYER = dedent("""\
    import os

    import requests


    def fetch_item(item_id: str) -> dict:
        return requests.get(f"https://shop.example.test/v1/items/{item_id}", timeout=5).json()


    def fetch_order(order_id: str) -> dict:
        return requests.get(f"https://shop.example.test/v1/orders/{order_id}", timeout=5).json()


    def make_item() -> dict:
        return requests.post("https://shop.example.test/v1/items", timeout=5).json()


    def fetch_published(item_id: str) -> dict:
        return requests.get(f"http://svc:8080/v1/items/{item_id}", timeout=5).json()


    def fetch_unserved() -> dict:
        return requests.get("http://svc:8080/v1/orders/7", timeout=5).json()


    def fetch_local() -> dict:
        return requests.get("http://localhost:8000/v1/items/1", timeout=5).json()


    def fetch_unstated(item_id: str) -> dict:
        base = os.environ["ITEMS_BASE"]
        return requests.get(f"{base}/v1/items/{item_id}", timeout=5).json()
""")
TOOLS = dedent("""\
    import requests


    def fetch_local_order() -> dict:
        return requests.get("http://localhost:8000/v1/orders/9", timeout=5).json()
""")
BATCH = dedent("""\
    import requests


    def fetch_token_path() -> dict:
        return requests.get(
            "https://shop.example.test/v1/reset/0b6c6e3e8f0a4b8e9d610f6b4c8f2a10", timeout=5
        ).json()
""")


def repos() -> dict[str, dict[str, str]]:
    return {
        "shop": {
            "pyproject.toml": PYPROJECT.format(name="shop", dep="fastapi"),
            "shop/__init__.py": "",
            "shop/main.py": SHOP_APP,
        },
        "orders": {
            "pyproject.toml": PYPROJECT.format(name="orders", dep="fastapi"),
            "orders/__init__.py": "",
            "orders/main.py": ORDERS_APP,
        },
        "twin": {
            "pyproject.toml": PYPROJECT.format(name="twin", dep="fastapi"),
            "twin/__init__.py": "",
            "twin/a.py": TWIN_APP,
            "twin/b.py": TWIN_APP,
        },
        "svc": {
            "pyproject.toml": PYPROJECT.format(name="svc", dep="fastapi"),
            "svc/__init__.py": "",
            "svc/main.py": SHOP_APP,
            **COMPOSE,
        },
        "buyer": {
            "pyproject.toml": PYPROJECT.format(name="buyer", dep="requests"),
            "buyer/__init__.py": "",
            "buyer/client.py": BUYER,
        },
        "tools": {
            "pyproject.toml": PYPROJECT.format(name="tools", dep="requests"),
            "tools/__init__.py": "",
            "tools/client.py": TOOLS,
        },
        "batch": {
            "pyproject.toml": PYPROJECT.format(name="batch", dep="requests"),
            "batch/__init__.py": "",
            "batch/client.py": BATCH,
        },
    }
