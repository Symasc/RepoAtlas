"""Where a Python web application is published, and what that exposes (Python P4).

A Flask application's routes become *endpoints* — method plus absolute URL — only
when its own module states where it is published: a flasgger Swagger 2 template,
bound to that exact application, stating ``host``, ``schemes`` and ``basePath``.
Everything else is refused, and most of this file is refusals: a published identity
is what lets a request in another repository reach a handler, so one read wrongly is
a false cross-repository link.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from repoatlas.models.parsing import EdgeType, ParsedReference
from repoatlas.parsers.http_facts import HTTP_EXPOSED_ENDPOINT, PENDING_FACT
from repoatlas.parsers.python import PythonParser
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build

_APP = """\
from flask import Flask
try:
    from flasgger import Swagger
except ImportError:
    Swagger = None

app = Flask(__name__)
{template}
if Swagger:
    {call}


@app.route("/users/<user_id>")
def get_user(user_id):
    return user_id
"""

_TEMPLATE = (
    'template = {"swagger": "2.0", "host": "accounts.example", "basePath": "/", '
    '"schemes": ["https"]}'
)
_CALL = "Swagger(app, template=template)"


def _references(source: str) -> list[ParsedReference]:
    return list(PythonParser().parse(Path("app.py"), textwrap.dedent(source)).references)


def _app(template: str = _TEMPLATE, call: str = _CALL) -> str:
    return _APP.format(template=template, call=call)


def _exposed(source: str) -> set[str]:
    return {
        r.target_key
        for r in _references(source)
        if r.edge_type is EdgeType.EXPOSES_HTTP_ENDPOINT and PENDING_FACT not in r.meta
    }


def _claims(source: str) -> list[ParsedReference]:
    return [r for r in _references(source) if r.meta.get(PENDING_FACT) == HTTP_EXPOSED_ENDPOINT]


def _deployments(source: str) -> set[str]:
    return {
        r.target_key
        for r in _references(source)
        if r.edge_type is EdgeType.DECLARES_HTTP_DEPLOYMENT
    }


def test_a_bound_template_publishes_the_app_and_exposes_each_route() -> None:
    source = _app()
    assert _deployments(source) == {"https://accounts.example"}
    assert _exposed(source) == {"GET https://accounts.example/users/<user_id>"}
    [claim] = _claims(source)
    assert claim.source_symbol == "get_user"
    # The decorator's own span: the lines that state the method and the route.
    assert (claim.line_start, claim.line_end) == (13, 13)
    assert claim.meta == {
        PENDING_FACT: HTTP_EXPOSED_ENDPOINT,
        "method": "GET",
        "route": "/users/<user_id>",
        "deployment": "https://accounts.example",
        "identity_source": "flasgger-swagger2",
        "identity_at": "8",
        "bound_at": "10",
        "source": "flask",
    }


def test_the_endpoint_fact_is_composed_and_the_deployment_is_declared_by_the_file() -> None:
    references = _references(_app())
    [exposed] = [
        r
        for r in references
        if r.edge_type is EdgeType.EXPOSES_HTTP_ENDPOINT and PENDING_FACT not in r.meta
    ]
    assert exposed.meta == {"source": "flask", "provenance": "composed"}
    [deployment] = [r for r in references if r.edge_type is EdgeType.DECLARES_HTTP_DEPLOYMENT]
    assert deployment.source_symbol is None
    assert deployment.meta == {"source": "flask", "provenance": "composed"}


def test_the_route_fact_is_unchanged_beside_the_exposure() -> None:
    served = [r for r in _references(_app()) if r.edge_type is EdgeType.SERVES_HTTP_ROUTE]
    assert [(r.target_key, r.meta) for r in served] == [
        ("GET /users/<user_id>", {"source": "flask", "provenance": "exact"})
    ]


def test_a_plain_import_and_an_inline_template_publish_too() -> None:
    source = """\
    from flask import Flask
    from flasgger import Swagger

    app = Flask(__name__)
    Swagger(app, template={"host": "a.example", "basePath": "/v1", "schemes": ["https"]})

    @app.get("/items/<int:item_id>")
    def item(item_id): ...
    """
    assert _exposed(source) == {"GET https://a.example/v1/items/<int:item_id>"}


def test_every_scheme_is_its_own_deployment_and_http_is_never_https() -> None:
    template = 'template = {"host": "a.example", "basePath": "/", "schemes": ["http", "https"]}'
    source = _app(template=template)
    assert _deployments(source) == {"http://a.example", "https://a.example"}
    assert _exposed(source) == {
        "GET http://a.example/users/<user_id>",
        "GET https://a.example/users/<user_id>",
    }


def test_the_base_path_the_mount_and_the_rule_compose_in_that_order() -> None:
    source = """\
    from flask import Blueprint, Flask
    from flasgger import Swagger

    app = Flask(__name__)
    admin = Blueprint("admin", __name__, url_prefix="/admin")
    Swagger(app, template={"host": "a.example", "basePath": "/api", "schemes": ["https"]})


    @admin.get("/audit/<entry>")
    def audit(entry): ...


    app.register_blueprint(admin, url_prefix="/v2")
    """
    [claim] = _claims(source)
    assert claim.target_key == "GET https://a.example/api/v2/audit/<entry>"
    assert claim.meta["route"] == "/v2/audit/<entry>"
    assert claim.meta["deployment"] == "https://a.example/api"
    assert claim.source_symbol == "audit"


def test_every_declared_method_is_exposed_on_its_own() -> None:
    source = _APP.format(template=_TEMPLATE, call=_CALL).replace(
        '@app.route("/users/<user_id>")', '@app.route("/users/<user_id>", methods=["GET", "PUT"])'
    )
    assert _exposed(source) == {
        "GET https://accounts.example/users/<user_id>",
        "PUT https://accounts.example/users/<user_id>",
    }


def test_only_the_application_the_template_is_bound_to_is_published() -> None:
    source = _app() + textwrap.dedent(
        """\

        other = Flask("other")


        @other.route("/users/<user_id>")
        def other_user(user_id): ...
        """
    )
    assert {c.source_symbol for c in _claims(source)} == {"get_user"}


def test_a_fastapi_application_is_never_published_by_a_flask_template() -> None:
    source = """\
    from fastapi import FastAPI
    from flasgger import Swagger

    app = FastAPI()
    Swagger(app, template={"host": "a.example", "basePath": "/", "schemes": ["https"]})

    @app.get("/x")
    def x(): ...
    """
    assert _claims(source) == []
    assert _deployments(source) == set()


@pytest.mark.parametrize(
    ("template", "call"),
    [
        # Missing any of the three fields: configuration could supply it.
        ('template = {"host": "a.example", "basePath": "/"}', _CALL),
        ('template = {"host": "a.example", "schemes": ["https"]}', _CALL),
        ('template = {"basePath": "/", "schemes": ["https"]}', _CALL),
        # Not literal, not Swagger 2, not one reading.
        ('template = {"host": HOST, "basePath": "/", "schemes": ["https"]}', _CALL),
        ('template = {"host": "a.example", "basePath": "/", "schemes": SCHEMES}', _CALL),
        ('template = {"host": "a.example", "basePath": "/", "schemes": []}', _CALL),
        ('template = {"host": "a.example", "basePath": "/", "schemes": ["ftp"]}', _CALL),
        ('template = {"host": "a.example/x", "basePath": "/", "schemes": ["https"]}', _CALL),
        ('template = {"host": "a.example", "basePath": "v1", "schemes": ["https"]}', _CALL),
        ('template = {"host": "{env}.example", "basePath": "/", "schemes": ["https"]}', _CALL),
        (
            'template = {"openapi": "3.0.2", "host": "a.example", "basePath": "/", '
            '"schemes": ["https"]}',
            _CALL,
        ),
        (
            'template = {"swagger": "3.0", "host": "a.example", "basePath": "/", '
            '"schemes": ["https"]}',
            _CALL,
        ),
        (
            'template = {"host": "a.example", "host": "b.example", "basePath": "/", '
            '"schemes": ["https"]}',
            _CALL,
        ),
        ('template = {**BASE, "host": "a.example", "basePath": "/", "schemes": ["https"]}', _CALL),
        ("template = make_template()", _CALL),
        # Bound more than once.
        (_TEMPLATE + "\ntemplate = {}", _CALL),
        # The call does not bind the template to this application readably.
        (_TEMPLATE, "Swagger(app, template_file='spec.yml')"),
        (_TEMPLATE, "Swagger(app)"),
        (_TEMPLATE, "Swagger(template=template)"),
        (_TEMPLATE, "Swagger(app=app, template=template)"),
        (_TEMPLATE, "Swagger(app, **options)"),
        (_TEMPLATE, "Swagger(app, None, None, template)"),
        (_TEMPLATE, "Swagger(app, template=template)\n    Swagger(app, template=template)"),
    ],
)
def test_an_identity_that_is_not_stated_whole_publishes_nothing(template: str, call: str) -> None:
    source = _app(template=template, call=call)
    assert _deployments(source) == set()
    assert _claims(source) == []


@pytest.mark.parametrize(
    "fallback",
    [
        "Swagger = make_swagger()",  # a callable: not provably flasgger's
        "from other import Swagger",
    ],
)
def test_an_optional_import_whose_fallback_is_callable_proves_nothing(fallback: str) -> None:
    source = _app().replace("    Swagger = None", f"    {fallback}")
    assert _claims(source) == []


def test_a_fallback_outside_an_import_error_handler_proves_nothing() -> None:
    source = _app().replace("except ImportError:", "except Exception:")
    assert _claims(source) == []


def test_a_swagger_call_inside_a_function_makes_every_binding_unknowable() -> None:
    source = _app() + textwrap.dedent(
        """\

        def rebuild():
            return Swagger(
                app, template={"host": "b.example", "basePath": "/", "schemes": ["https"]}
            )
        """
    )
    assert _claims(source) == []


def test_a_route_without_a_published_application_exposes_nothing() -> None:
    source = """\
    from flask import Flask
    app = Flask(__name__)

    @app.route("/users/<user_id>")
    def get_user(user_id): ...
    """
    references = _references(source)
    assert [r.target_key for r in references if r.edge_type is EdgeType.SERVES_HTTP_ROUTE] == [
        "GET /users/<user_id>"
    ]
    assert [r for r in references if r.edge_type is EdgeType.EXPOSES_HTTP_ENDPOINT] == []


def test_a_dynamic_route_is_still_unread_when_the_app_is_published() -> None:
    source = _app() + textwrap.dedent(
        """\

        ROUTE = "/legacy" if Swagger else "/"


        @app.route(ROUTE)
        def landing(): ...
        """
    )
    assert {c.source_symbol for c in _claims(source)} == {"get_user"}


def test_claim_meta_is_plain_strings_for_the_linker() -> None:
    [claim] = _claims(_app())
    assert all(isinstance(value, str) for value in claim.meta.values())
    json.dumps(claim.meta)


def test_a_sync_stores_the_exposure_on_the_shared_node_and_keeps_the_claim(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, {"accounts": {"app.py": textwrap.dedent(_app())}})
    rows = workspace.database.execute(
        """
        SELECT e.type, s.type AS s_type, s.qualified_name AS s_name, d.key, e.line_start
        FROM edges e JOIN entities s ON s.id = e.src_id JOIN entities d ON d.id = e.dst_id
        WHERE e.type IN ('EXPOSES_HTTP_ENDPOINT', 'DECLARES_HTTP_DEPLOYMENT')
        ORDER BY e.type
        """
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        (
            "DECLARES_HTTP_DEPLOYMENT",
            "file",
            "app.py",
            "http-deployment:https://accounts.example",
            None,
        ),
        (
            "EXPOSES_HTTP_ENDPOINT",
            "function",
            "get_user",
            "http-endpoint:GET:https://accounts.example/users/<user_id>",
            None,
        ),
    ]
    [claim] = workspace.database.execute(
        "SELECT target_key, line_start, meta_json FROM unresolved_refs WHERE meta_json LIKE ?",
        (f'%"{HTTP_EXPOSED_ENDPOINT}"%',),
    ).fetchall()
    assert claim["target_key"] == "GET https://accounts.example/users/<user_id>"
    assert claim["line_start"] == 13
    assert json.loads(claim["meta_json"])["identity_at"] == "8"
