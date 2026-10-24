"""Neutral repositories for scoped HTTP declarations (Phase A2, slice S4).

``alpha`` and ``beta`` serve the same two routes (two services behind one request), ``twin`` serves
``GET /v1/items/{item_id}`` from two files (one service answering twice), and ``mounted`` serves
``GET /items/{item_id}`` and ``POST /items``, routes a declaration with base path ``/v1`` mounts
under that prefix. ``caller`` makes requests on bases it never states, each written differently:

* ``client.py``: ``self.base_url`` in a class and in a subclass sharing its prefix, a module
  ``BASE`` read in three functions (the third asks for a method the service does not answer), a
  parameter ``base`` in two functions, a parameter shadowing ``BASE``, and parameters whose paths
  are written with and without a ``/v1`` prefix (``base``, ``root``, ``plain``), twice over
  (``doubled``), or as ``/v10`` (``longer``), for ``mounted``;
* ``other.py``: the same ``BASE`` text in another file;
* ``qualified.py``: ``config.api_base`` on a module binding, and the same text on a parameter;
* ``odd.py``: a base cut at 60 characters, a host written as ``http://{host}``, and a stated origin.

``second`` makes the same ``BASE`` request in a file of the same path, ``webts`` does the same in
TypeScript (``src/api.ts``) and hands its base to a function in options (``src/root.ts``). Every
name is invented.
"""

from __future__ import annotations

from textwrap import dedent

PYPROJECT = '[project]\nname = "{name}"\nversion = "1.0.0"\ndependencies = ["{dep}"]\n'

SERVICE_APP = dedent("""\
    from fastapi import FastAPI

    app = FastAPI()


    @app.get("/v1/items/{item_id}")
    def get_item(item_id: str) -> dict:
        return {"id": item_id}


    @app.post("/v1/items")
    def create_item() -> dict:
        return {}
""")
TWIN_APP = dedent("""\
    from fastapi import FastAPI

    app = FastAPI()


    @app.get("/v1/items/{item_id}")
    def get_item(item_id: str) -> dict:
        return {"id": item_id}
""")

MOUNTED_APP = dedent("""\
    from fastapi import FastAPI

    app = FastAPI()


    @app.get("/items/{item_id}")
    def get_item(item_id: str) -> dict:
        return {"id": item_id}


    @app.post("/items")
    def create_item() -> dict:
        return {}
""")

#: A parameter name of exactly 60 characters: the base text a parser may have cut.
LONG_BASE = "base_" + "x" * 55

CLIENT = dedent("""\
    import os

    import requests

    BASE = os.environ["ITEMS_BASE"]


    class UsersClient:
        def __init__(self, base_url):
            self.base_url = base_url

        def get_item(self, item_id):
            return requests.get(f"{self.base_url}/v1/items/{item_id}", timeout=5).json()

        def make_item(self):
            return requests.post(f"{self.base_url}/v1/items", timeout=5).json()


    class UsersClientV2(UsersClient):
        def get_item(self, item_id):
            return requests.get(f"{self.base_url}/v1/items/{item_id}", timeout=5).json()


    def read_item(item_id):
        return requests.get(f"{BASE}/v1/items/{item_id}", timeout=5).json()


    def write_item():
        return requests.post(f"{BASE}/v1/items", timeout=5).json()


    def list_items():
        return requests.get(f"{BASE}/v1/items", timeout=5).json()


    def shadowed_read(BASE, item_id):
        return requests.get(f"{BASE}/v1/items/{item_id}", timeout=5).json()


    def param_read(base, item_id):
        return requests.get(f"{base}/v1/items/{item_id}", timeout=5).json()


    def param_write(base):
        return requests.post(f"{base}/v1/items", timeout=5).json()


    def prefixed_read(root, item_id):
        return requests.get(f"{root}/items/{item_id}", timeout=5).json()


    def plain_read(plain, item_id):
        return requests.get(f"{plain}/items/{item_id}", timeout=5).json()


    def plain_write(plain):
        return requests.post(f"{plain}/items", timeout=5).json()


    def doubled_read(doubled, item_id):
        return requests.get(f"{doubled}/v1/v1/items/{item_id}", timeout=5).json()


    def longer_read(longer, item_id):
        return requests.get(f"{longer}/v10/items/{item_id}", timeout=5).json()
""")
OTHER = dedent("""\
    import os

    import requests

    BASE = os.environ["ITEMS_BASE"]


    def other_read(item_id):
        return requests.get(f"{BASE}/v1/items/{item_id}", timeout=5).json()
""")
QUALIFIED = dedent("""\
    import requests

    config = None


    def qualified_read(item_id):
        return requests.get(f"{config.api_base}/v1/items/{item_id}", timeout=5).json()


    def shadowed_qualified(config, item_id):
        return requests.get(f"{config.api_base}/v1/items/{item_id}", timeout=5).json()
""")
ODD = dedent("""\
    import requests


    def long_read(LONGNAME, item_id):
        return requests.get(f"{LONGNAME}/v1/items/{item_id}", timeout=5).json()


    def host_read(host, item_id):
        return requests.get(f"http://{host}/v1/items/{item_id}", timeout=5).json()


    def stated_read():
        return requests.get("https://items.example.test/v1/items/1", timeout=5).json()
""").replace("LONGNAME", LONG_BASE)
SECOND = dedent("""\
    import os

    import requests

    BASE = os.environ["ITEMS_BASE"]


    def read_item(item_id):
        return requests.get(f"{BASE}/v1/items/{item_id}", timeout=5).json()
""")
API_TS = dedent("""\
    const BASE = process.env.API_BASE;

    export function listItem(id: string) {
      return fetch(`${BASE}/v1/items/${id}`);
    }

    export function createItem() {
      return fetch(`${BASE}/v1/items`, { method: "POST" });
    }

    export function shadowedItem(BASE: string) {
      return fetch(`${BASE}/v1/items/1`);
    }
""")
ROOT_TS = dedent("""\
    import superagent from 'superagent';

    function unstated({ fetcher, base }: Options) {
      return fetcher.get(`${base}/v1/items/current`);
    }

    unstated({ fetcher: superagent, base: process.env.UNSTATED });
""")


def _service(name: str, app: str = SERVICE_APP) -> dict[str, str]:
    return {
        "pyproject.toml": PYPROJECT.format(name=name, dep="fastapi"),
        f"{name}/__init__.py": "",
        f"{name}/main.py": app,
    }


def repos() -> dict[str, dict[str, str]]:
    return {
        "alpha": _service("alpha"),
        "beta": _service("beta"),
        "mounted": _service("mounted", MOUNTED_APP),
        "twin": {
            "pyproject.toml": PYPROJECT.format(name="twin", dep="fastapi"),
            "twin/__init__.py": "",
            "twin/a.py": TWIN_APP,
            "twin/b.py": TWIN_APP,
        },
        "caller": {
            "pyproject.toml": PYPROJECT.format(name="caller", dep="requests"),
            "client.py": CLIENT,
            "other.py": OTHER,
            "qualified.py": QUALIFIED,
            "odd.py": ODD,
        },
        "second": {
            "pyproject.toml": PYPROJECT.format(name="second", dep="requests"),
            "client.py": SECOND,
        },
        "webts": {
            "package.json": '{"name": "webts", "private": true}\n',
            "src/api.ts": API_TS,
            "src/root.ts": ROOT_TS,
        },
    }
