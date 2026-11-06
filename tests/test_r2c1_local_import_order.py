"""R2c-1: a helper's local import is bound in statement order.

Python treats a name a function imports as local to the whole function, so a registration that
reads ``views.bp`` above ``from pkg import views`` raises ``UnboundLocalError`` at run time (and
does not fall back to a module-level ``views``). Such a registration is not a mount: the helper
gets no claim and the route stays unserved. With the import first, the route is served.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from repoatlas.parsers.http_facts import HELPER_ROLE
from tests.f1_f2_corpus import open_built, python_repo

VIEWS = """\
from flask import Blueprint

bp = Blueprint("v", __name__)


@bp.route("/one")
def one():
    return "one"
"""

#: LA3: the registration reads ``views`` above the helper's own import of it.
LA3_APP = """\
from flask import Flask


def register_blueprints(app):
    app.register_blueprint(views.bp)
    from pkg import views


def create_app():
    app = Flask(__name__)
    register_blueprints(app)
    return app
"""

#: LA5: as LA3, with a module-level ``views`` the helper must not fall back to.
LA5_APP = """\
from pkg import views
from flask import Flask


def register_blueprints(app):
    app.register_blueprint(views.bp)
    from pkg import views


def create_app():
    app = Flask(__name__)
    register_blueprints(app)
    return app
"""

#: LA4 (control): the helper's import comes before the registration.
LA4_APP = """\
from flask import Flask


def register_blueprints(app):
    from pkg import views
    app.register_blueprint(views.bp)


def create_app():
    app = Flask(__name__)
    register_blueprints(app)
    return app
"""

SERVES_SQL = """SELECT e.parser, f.rel_path, t.name, e.line_start, e.line_end
                FROM edges e JOIN entities t ON t.id = e.dst_id
                LEFT JOIN files f ON f.id = e.file_id
                WHERE e.type = 'SERVES_HTTP_ROUTE' AND e.repo_id = ? ORDER BY t.name"""

CLAIMS_SQL = """SELECT f.rel_path, u.meta_json
                FROM unresolved_refs u LEFT JOIN files f ON f.id = u.file_id
                WHERE u.repo_id = ? ORDER BY f.rel_path, u.line_start, u.edge_type, u.target_key"""


@dataclass(frozen=True)
class Observed:
    #: SERVES_HTTP_ROUTE edges: ``{parser, file, route, lines}``, ordered by route.
    serves: tuple[dict, ...]
    #: ``(file, meta)`` of each ``python-helper`` claim.
    helper_claims: tuple[tuple[str, dict], ...]


def observe(base: Path, name: str, app: str) -> Observed:
    files = {"pkg/__init__.py": "", "pkg/views.py": VIEWS, "pkg/app.py": app}
    workspace = open_built(base, name, {name: python_repo(name, "flask", files)})
    try:
        repo = next(r for r in workspace.repositories.list_all() if r.name == name)
        assert repo.id is not None
        edges = workspace.database.execute(SERVES_SQL, (repo.id,)).fetchall()
        rows = workspace.database.execute(CLAIMS_SQL, (repo.id,)).fetchall()
    finally:
        workspace.close()
    serves = tuple({"parser": p, "file": f, "route": t, "lines": [a, b]} for p, f, t, a, b in edges)
    claims = []
    for path, meta_json in rows:
        if meta_json is None:
            continue
        meta = json.loads(meta_json)
        if isinstance(meta, dict) and meta.get("pending_fact") == HELPER_ROLE:
            claims.append((path, meta))
    return Observed(serves=serves, helper_claims=tuple(claims))


@pytest.mark.parametrize(("name", "app"), [("la3_local_after", LA3_APP), ("la5_shadowed", LA5_APP)])
def test_registration_above_the_local_import_is_not_a_mount(
    tmp_path: Path, name: str, app: str
) -> None:
    obs = observe(tmp_path, name, app)
    assert [s["route"] for s in obs.serves] == []
    assert obs.helper_claims == ()


def test_local_import_before_the_registration_is_a_mount(tmp_path: Path) -> None:
    obs = observe(tmp_path, "la4_local_before", LA4_APP)
    assert obs.serves == (
        {"parser": "linker:global", "file": "pkg/views.py", "route": "GET /one", "lines": [6, 6]},
    )
    assert [path for path, _ in obs.helper_claims] == ["pkg/app.py"]
