"""Repositories for the cross-repository HTTP join (Python P4), as a test helper.

Three services and one consumer, named for what they are rather than for the public
projects the milestone was validated on:

* ``accounts`` publishes ``https://accounts.example/api`` (a flasgger Swagger 2 template
  with the optional import) and serves users, items and a mounted blueprint;
* ``billing`` publishes ``https://billing.example`` and serves the *same* route as
  ``accounts`` — only the service identity tells them apart;
* ``accounts-mirror`` publishes the very identity ``accounts`` does, with the same route:
  added to a workspace, it makes the join ambiguous;
* ``portal`` requests all of them, in every spelling the join reads and several it must
  refuse, and has a caller of one request and a test of it.

Line numbers matter: the evidence the join records is asserted to the line.
"""

from __future__ import annotations

from typing import Any

from repoatlas.services.workspace import Workspace

ACCOUNTS_APP = """\
from flask import Blueprint, Flask

try:
    from flasgger import Swagger
except ImportError:
    Swagger = None

from store import lookup

app = Flask(__name__)
admin = Blueprint("admin", __name__, url_prefix="/admin")

template = {
    "swagger": "2.0",
    "host": "accounts.example",
    "basePath": "/api",
    "schemes": ["https"],
}
if Swagger:
    Swagger(app, template=template)


@app.route("/users/<user_id>")
def get_user(user_id):
    return lookup(user_id)


@app.route("/users/<user_id>/settings")
def get_settings(user_id):
    return {}


@app.route("/users", methods=["POST"])
def create_user():
    return {}


@app.route("/items/<int:item_id>")
def get_item(item_id):
    return {}


@admin.get("/audit/<entry>")
def audit(entry):
    return entry


app.register_blueprint(admin, url_prefix="/v2")
"""

#: Lines the evidence names in ``ACCOUNTS_APP``.
ACCOUNTS_HOST_LINE = 15
ACCOUNTS_BOUND_LINE = 20
GET_USER_ROUTE_LINE = 23

STORE = """\
def lookup(user_id):
    return {"id": user_id}
"""

ACCOUNTS: dict[str, str] = {"app.py": ACCOUNTS_APP, "store.py": STORE}


def service(host: str, base_path: str, handler: str) -> dict[str, str]:
    """A published service with one ``/users/<user_id>`` route and a health check."""
    return {
        "app.py": f"""\
from flask import Flask
from flasgger import Swagger

app = Flask(__name__)
Swagger(app, template={{"host": "{host}", "basePath": "{base_path}", "schemes": ["https"]}})


@app.route("/users/<user_id>")
def {handler}(user_id):
    return {{}}


@app.route("/health")
def health():
    return "ok"
"""
    }


BILLING = service("billing.example", "/", "billing_user")
MIRROR = service("accounts.example", "/api", "mirror_user")

PORTAL_CLIENT = """\
import os

import requests

from settings import SHARED_BASE

ACCOUNTS = "https://accounts.example/api"
ITEM_URL = "https://accounts.example/api/items/7"
BAD_ITEM_URL = "https://accounts.example/api/items/seven"


def load_user(user_id):
    return requests.get(f"{ACCOUNTS}/users/{user_id}")


def load_literal():
    return requests.get("https://accounts.example/api/users/123?expand=roles")


def load_settings(user_id):
    return requests.get(f"https://accounts.example/api/users/{user_id}/settings")


def spans(a, b):
    return requests.get(f"{ACCOUNTS}/users/{a}/{b}")


def list_users():
    return requests.get(f"{ACCOUNTS}/users")


def remove(user_id):
    return requests.post(f"{ACCOUNTS}/users/{user_id}")


def billing_user(user_id):
    return requests.get(f"https://billing.example/users/{user_id}")


def item():
    return requests.get(ITEM_URL)


def bad_item():
    return requests.get(BAD_ITEM_URL)


def audit(entry):
    return requests.get(f"{ACCOUNTS}/v2/audit/{entry}")


def insecure():
    return requests.get("http://accounts.example/api/users/1")


def unknown():
    return requests.get("https://elsewhere.example/users/1")


def dynamic(base, user_id):
    return requests.get(f"{base}/users/{user_id}")


def from_env(user_id):
    return requests.get(f"{os.environ['ACCOUNTS']}/users/{user_id}")


def shared(user_id):
    return requests.get(f"{SHARED_BASE}/users/{user_id}")
"""

#: Lines the evidence names in ``PORTAL_CLIENT``.
PORTAL_BASE_LINE = 7
LOAD_USER_CALL_LINE = 13

PORTAL: dict[str, str] = {
    "client.py": PORTAL_CLIENT,
    "settings.py": 'SHARED_BASE = "https://accounts.example/api"\n',
    "shadow.py": (
        "import requests\n"
        'BASE = "https://accounts.example/api"\n'
        "def configure():\n"
        '    BASE = "https://staging.example"\n'
        "    return BASE\n"
        "def load(user_id):\n"
        '    return requests.get(f"{BASE}/users/{user_id}")\n'
    ),
    "views.py": (
        "from client import load_user\n\n\ndef render_profile(user_id):\n"
        "    return load_user(user_id)\n"
    ),
    "tests/test_portal.py": (
        'from client import load_user\n\n\ndef test_load_user():\n    assert load_user("1")\n'
    ),
}

#: What the join decides for each request ``portal/client.py`` makes, by caller.
DECISIONS = {
    "load_user": "linked",
    "load_literal": "linked",
    "load_settings": "linked",
    "spans": "http-route-unmatched",
    "list_users": "http-route-unmatched",
    "remove": "http-route-unmatched",
    "billing_user": "linked",
    "item": "linked",
    "bad_item": "http-route-unmatched",
    "audit": "linked",
    "insecure": "http-service-unmatched",
    "unknown": "http-service-unmatched",
    # A parameter, an environment variable and an imported base: the path is read, the
    # host is not -- counted, never joined (D2).
    "dynamic": "http-base-unknown",
    "from_env": "http-base-unknown",
    "shared": "http-base-unknown",
}

#: The joins in the workspace of ``accounts``, ``billing`` and ``portal``: (consumer
#: caller, provider repository, provider handler, endpoint).
LINKS = {
    ("load_user", "accounts", "get_user", "GET https://accounts.example/api/users/<user_id>"),
    ("load_literal", "accounts", "get_user", "GET https://accounts.example/api/users/<user_id>"),
    (
        "load_settings",
        "accounts",
        "get_settings",
        "GET https://accounts.example/api/users/<user_id>/settings",
    ),
    ("billing_user", "billing", "billing_user", "GET https://billing.example/users/<user_id>"),
    ("item", "accounts", "get_item", "GET https://accounts.example/api/items/<int:item_id>"),
    ("audit", "accounts", "audit", "GET https://accounts.example/api/v2/audit/<entry>"),
}

WORKSPACE = {"accounts": ACCOUNTS, "billing": BILLING, "portal": PORTAL}


def crossings(workspace: Workspace) -> list[dict[str, Any]]:
    """Every ``CALLS_HTTP_HANDLER`` edge, both ends named, its evidence decoded."""
    import json

    rows = workspace.database.execute(
        """
        SELECT s.qualified_name AS caller, sr.slug AS consumer, d.qualified_name AS handler,
               dr.slug AS provider, f.rel_path AS asserted_by, e.line_start, e.line_end,
               e.parser, e.resolution
        FROM edges e
        JOIN entities s ON s.id = e.src_id JOIN entities d ON d.id = e.dst_id
        LEFT JOIN repositories sr ON sr.id = s.repo_id
        LEFT JOIN repositories dr ON dr.id = d.repo_id
        LEFT JOIN files f ON f.id = e.file_id
        WHERE e.type = 'CALLS_HTTP_HANDLER'
        ORDER BY sr.slug, e.line_start
        """
    ).fetchall()
    return [{**dict(row), "resolution": json.loads(row["resolution"])} for row in rows]


def links(workspace: Workspace) -> set[tuple[str, str, str, str]]:
    return {
        (row["caller"], row["provider"], row["handler"], row["resolution"]["http"]["endpoint"])
        for row in crossings(workspace)
    }


def decided(workspace: Workspace, repository: str) -> dict[str, list[tuple[str, dict[str, str]]]]:
    """Every request claim one repository makes, by caller: its decision and the evidence
    the join kept for it, one entry per claim (literal alternatives are separate claims)."""
    from repoatlas.graph.linker import ReferenceLinker
    from repoatlas.parsers.http_facts import HTTP_CLIENT_CALL

    plan = ReferenceLinker(workspace.knowledge).prepare_global().http_crossings
    rows = workspace.database.execute(
        """
        SELECT u.id, s.qualified_name FROM unresolved_refs u
        JOIN entities s ON s.id = u.src_id JOIN repositories r ON r.id = u.repo_id
        WHERE r.slug = ? AND u.meta_json LIKE ?
        """,
        (repository, f'%"{HTTP_CLIENT_CALL}"%'),
    ).fetchall()
    found: dict[str, list[tuple[str, dict[str, str]]]] = {}
    for row in rows:
        found.setdefault(row["qualified_name"], []).append(
            (plan.decisions[row["id"]], plan.evidence[row["id"]])
        )
    return {caller: sorted(entries, key=str) for caller, entries in found.items()}
