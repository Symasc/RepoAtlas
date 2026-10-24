"""The scoped-declaration corpus for the graph explorer's end-to-end run (Phase A2).

Two repositories, every name invented:

* ``itemservice`` serves ``GET /v1/items/{item_id}`` (FastAPI).
* ``itemcaller`` makes the same request from two files, ``client.py`` and ``other.py``, each on
  a module binding ``BASE`` read from the environment. The source never states a host, so the
  join leaves both requests unresolved (``http-base-unknown``) in one worklist group,
  ``{BASE}``. A declaration scoped to ``client.py`` joins only that file's request; the same
  base text in ``other.py`` is outside it.

The two caller files are the ``BASE`` ones of tests/http_scoped_corpus.py, so the join's
decisions for them are the ones the backend tests pin. This module only builds them for the
browser: ``python -m tests.scoped_e2e_corpus <dir>`` indexes them into ``<dir>`` exactly as
``python -m tests.topology_corpus`` does for the linked corpus.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from tests.topology_corpus import open_corpus

SERVICE = "itemservice"
CALLER = "itemcaller"

PYPROJECT = '[project]\nname = "{name}"\nversion = "1.0.0"\ndependencies = ["{dep}"]\n'

SERVICE_APP = dedent("""\
    from fastapi import FastAPI

    app = FastAPI()


    @app.get("/v1/items/{item_id}")
    def get_item(item_id: str) -> dict:
        return {"id": item_id}
""")

CLIENT = dedent("""\
    import os

    import requests

    BASE = os.environ["ITEMS_BASE"]


    def read_item(item_id):
        return requests.get(f"{BASE}/v1/items/{item_id}", timeout=5).json()
""")

OTHER = dedent("""\
    import os

    import requests

    BASE = os.environ["ITEMS_BASE"]


    def other_read(item_id):
        return requests.get(f"{BASE}/v1/items/{item_id}", timeout=5).json()
""")


def repos() -> dict[str, dict[str, str]]:
    return {
        SERVICE: {
            "pyproject.toml": PYPROJECT.format(name=SERVICE, dep="fastapi"),
            f"{SERVICE}/__init__.py": "",
            f"{SERVICE}/main.py": SERVICE_APP,
        },
        CALLER: {
            "pyproject.toml": PYPROJECT.format(name=CALLER, dep="requests"),
            "client.py": CLIENT,
            "other.py": OTHER,
        },
    }


def main(argv: list[str] | None = None) -> None:
    """Build the scoped corpus for the graph explorer's end-to-end run.

    ``python -m tests.scoped_e2e_corpus <dir>`` indexes it into ``<dir>``, which must be absent
    or empty, leaving ``config.yaml`` and ``state/atlas.db`` there for ``repoatlas serve`` (via
    REPOATLAS_CONFIG and REPOATLAS_DB) and for the ``repoatlas sync`` the test runs itself.
    """
    import sys

    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        raise SystemExit("usage: python -m tests.scoped_e2e_corpus <empty directory>")
    root = Path(args[0])
    if root.exists() and any(root.iterdir()):
        raise SystemExit(f"{root} is not empty")
    root.mkdir(parents=True, exist_ok=True)
    open_corpus(root, repos()).close()


if __name__ == "__main__":
    main()
