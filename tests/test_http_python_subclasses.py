"""A module-level class whose one base is a framework application, router or blueprint (M6).

``class CustomRouter(APIRouter): pass`` then ``router = CustomRouter()`` constructs a router,
exactly as ``APIRouter()`` does, when nothing in the class can change what it registers or serves:
one base, which is the framework's own class as this module imports it or a class of this kind
defined above it in the same module (at most three deep); no metaclass, keyword, decorator or type
parameter; a name bound once; a body of ``pass``, a docstring and plain methods that are neither
special nor a member the framework registers or serves through; and an ``__init__`` only when it is
exactly
``super().__init__(*args, **kwargs)``. A name that merely looks like a router is nothing.

Half of this file is what is refused, and every refusal has an internal reason (``class_refusals``,
never stored). The class is read from this module's syntax tree only, never imported or run
(V1_SPEC §17). A subclass defined in one module and constructed in another serves nothing yet.
"""

from __future__ import annotations

import ast
import sys
import textwrap
from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.graph.py_routes import PROMOTED
from repoatlas.models.parsing import EdgeType, EntityType, ParsedReference
from repoatlas.parsers.http_facts import (
    HTTP_CLIENT_CALL,
    HTTP_EXPOSED_ENDPOINT,
    PENDING_FACT,
    PYTHON_CONSTANTS,
    PYTHON_MOUNT,
    PYTHON_RECEIVER,
    PYTHON_ROUTE,
    PYTHON_SCRIPT,
)
from repoatlas.parsers.http_python import PythonHttp
from repoatlas.parsers.interpreter import interpreter_tag
from repoatlas.parsers.python import PythonParser
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace
from repoatlas.sync.planner import facts_version
from tests.impact_corpus import build
from tests.incremental_corpus import assert_equals_fresh, edit, sync_all
from tests.test_http_python import _http, _parse, _served
from tests.test_http_python_routers import line, outcomes, served

_HTTP_KINDS = {EntityType.HTTP_ROUTE, EntityType.HTTP_DEPLOYMENT, EntityType.HTTP_ENDPOINT}
_ROLES = {
    PYTHON_RECEIVER,
    PYTHON_MOUNT,
    PYTHON_ROUTE,
    PYTHON_CONSTANTS,
    PYTHON_SCRIPT,
    HTTP_CLIENT_CALL,
    HTTP_EXPOSED_ENDPOINT,
}

STAR = "from helpers import *"
FASTAPI = "from fastapi import APIRouter, FastAPI"


def d(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def lines(*rows: str) -> str:
    return "\n".join(rows) + "\n"


def cls(*body: str, base: str = "APIRouter", name: str = "C") -> str:
    """A class statement whose body is the rows given, or ``pass``."""
    rows = textwrap.indent("\n".join(body or ("pass",)), "    ")
    return f"class {name}({base}):\n{rows}"


def routed(klass: str, construct: str = "C()", imports: str = FASTAPI) -> str:
    """A router built by ``construct``, with one route, included in an application."""
    return lines(
        imports,
        klass,
        "app = FastAPI()",
        f"router = {construct}",
        '@router.get("/users")',
        "def users(): ...",
        "app.include_router(router)",
    )


def read(source: str) -> PythonHttp:
    return PythonHttp(ast.parse(source))


def claims(source: str, role: str) -> list[ParsedReference]:
    return [r for r in _parse(source).references if r.meta.get(PENDING_FACT) == role]


def receivers(source: str) -> dict[str, str]:
    """Each receiver the module proves, by name, with its kind."""
    return {r.target_key: r.meta["kind"] for r in claims(source, PYTHON_RECEIVER)}


def related(source: str) -> list[ParsedReference]:
    """Every reference the HTTP reader contributes: facts, and the claims the global pass reads."""
    return [
        r
        for r in _parse(source).references
        if r.target_kind in _HTTP_KINDS or r.meta.get(PENDING_FACT) in _ROLES
    ]


def dump(references: list[ParsedReference]) -> list[str]:
    return sorted(r.model_dump_json() for r in references)


def control(source: str, name: str, base: str) -> str:
    """The same text, line for line, with the class made plain and its construction the base's."""
    plain = source.replace(f"class {name}({base}):", f"class {name}:")
    made = plain.replace(f"= {name}(", f"= {base}(")
    assert plain != source and made != plain
    return made


def nothing_built(source: str) -> None:
    """The class built no receiver and nothing is served."""
    assert read(source).subclasses == {}
    assert "router" not in receivers(source)
    assert _served(source) == set()


# -- recognition and serving ----------------------------------------------------------------


def test_a_subclass_of_apirouter_constructs_a_router_receiver() -> None:
    source = lines(
        "from fastapi import APIRouter",
        "class CustomRouter(APIRouter):",
        "    pass",
        "router = CustomRouter()",
        '@router.get("/users")',
        "def get_users(): ...",
    )
    http = read(source)
    [found] = http.subclasses.values()
    assert list(http.subclasses) == ["CustomRouter"]
    assert (found.kind, found.depth, found.base) == ("fastapi-router", 1, None)
    assert http.class_refusals == {}
    [receiver] = claims(source, PYTHON_RECEIVER)
    assert receiver.target_key == "router"
    assert (receiver.meta["kind"], receiver.meta["prefix"]) == ("fastapi-router", '""')
    assert receiver.line_start == line(source, "router = CustomRouter()")
    [route] = claims(source, PYTHON_ROUTE)
    assert route.target_key == "GET /users"
    assert route.meta["router"] == "router"
    plain = control(source, "CustomRouter", "APIRouter")
    assert related(source)
    assert dump(related(source)) == dump(related(plain))


def test_a_subclass_router_included_in_an_application_is_served() -> None:
    source = routed(cls())
    assert _served(source) == {"GET /users"}
    assert receivers(source) == {"app": "fastapi-app", "router": "fastapi-router"}
    assert dump(related(source)) == dump(related(control(source, "C", "APIRouter")))


def test_a_subclass_of_fastapi_is_an_application_that_serves_at_the_root() -> None:
    source = lines(
        FASTAPI,
        cls("pass", base="FastAPI", name="MyApp"),
        "app = MyApp()",
        'api = APIRouter(prefix="/api")',
        '@app.get("/health")',
        "def health(): ...",
        '@api.get("/x")',
        "def x(): ...",
        "app.include_router(api)",
    )
    assert _served(source) == {"GET /health", "GET /api/x"}
    http = read(source)
    assert (http.subclasses["MyApp"].kind, http.subclasses["MyApp"].depth) == ("fastapi-app", 1)
    assert receivers(source)["app"] == "fastapi-app"


def test_a_subclass_of_flask_is_an_application() -> None:
    source = lines(
        "from flask import Flask",
        cls("pass", base="Flask", name="MyApp"),
        "app = MyApp(__name__)",
        '@app.route("/hello")',
        "def hello(): ...",
        '@app.post("/s")',
        "def s(): ...",
    )
    assert _served(source) == {"GET /hello", "POST /s"}
    assert read(source).subclasses["MyApp"].kind == "flask-app"
    assert receivers(source) == {"app": "flask-app"}


def test_a_blueprint_subclass_is_served_at_its_prefix_and_at_a_registered_one() -> None:
    source = lines(
        "from flask import Blueprint, Flask",
        cls("pass", base="Blueprint", name="Pages"),
        "app = Flask(__name__)",
        'pages = Pages("pages", __name__, url_prefix="/pages")',
        '@pages.route("/about")',
        "def about(): ...",
        "app.register_blueprint(pages)",
        'app.register_blueprint(pages, url_prefix="/v2", name="v2")',
    )
    assert _served(source) == {"GET /pages/about", "GET /v2/about"}
    assert read(source).subclasses["Pages"].kind == "flask-blueprint"
    assert receivers(source) == {"app": "flask-app", "pages": "flask-blueprint"}


_CHAIN = lines(cls(name="A"), cls(base="A", name="B"), cls(base="B", name="C"))


def test_a_chain_of_three_classes_in_the_module_is_followed() -> None:
    source = routed(_CHAIN)
    http = read(source)
    assert {n: (s.kind, s.depth, s.base) for n, s in http.subclasses.items()} == {
        "A": ("fastapi-router", 1, None),
        "B": ("fastapi-router", 2, "A"),
        "C": ("fastapi-router", 3, "B"),
    }
    assert http.class_refusals == {}
    assert _served(source) == {"GET /users"}


def test_a_chain_past_three_is_refused_and_so_is_everything_above_it() -> None:
    source = routed(_CHAIN + lines(cls(base="C", name="D"), cls(base="D", name="E")), "E()")
    http = read(source)
    assert set(http.subclasses) == {"A", "B", "C"}
    assert http.class_refusals == {"D": "depth-exceeded", "E": "base-refused"}
    assert receivers(source) == {"app": "fastapi-app"}
    assert _served(source) == set()


@pytest.mark.parametrize(
    ("imports", "base"),
    [
        ("from fastapi import APIRouter as R, FastAPI", "R"),
        ("import fastapi\nfrom fastapi import FastAPI", "fastapi.APIRouter"),
        ("import fastapi as fa\nfrom fastapi import FastAPI", "fa.APIRouter"),
    ],
    ids=["aliased", "module", "aliased-module"],
)
def test_a_base_is_followed_through_the_import_the_module_states(imports: str, base: str) -> None:
    source = routed(cls(base=base), imports=imports)
    assert read(source).subclasses["C"].kind == "fastapi-router"
    assert _served(source) == {"GET /users"}


def test_a_blueprint_is_followed_through_its_module() -> None:
    source = lines(
        "import flask",
        cls("pass", base="flask.Blueprint", name="Pages"),
        "app = flask.Flask(__name__)",
        'pages = Pages("pages", __name__)',
        '@pages.route("/about")',
        "def about(): ...",
        "app.register_blueprint(pages)",
    )
    assert read(source).subclasses["Pages"].kind == "flask-blueprint"
    assert _served(source) == {"GET /about"}


@pytest.mark.parametrize(
    ("imports", "base"),
    [
        (lines(FASTAPI, "Base = APIRouter"), "Base"),
        (lines("from fastapi import FastAPI", "from mylib import APIRouter"), "APIRouter"),
        (
            lines("from fastapi import FastAPI", "from fastapi.routing import APIRouter"),
            "APIRouter",
        ),
    ],
    ids=["alias", "other-library", "submodule"],
)
def test_a_base_that_is_not_the_framework_import_is_not_followed(imports: str, base: str) -> None:
    source = routed(cls(base=base), imports=imports)
    http = read(source)
    assert (http.subclasses, http.class_refusals) == ({}, {})
    assert receivers(source) == {"app": "fastapi-app"}
    assert _served(source) == set()


@pytest.mark.parametrize(
    "head",
    [
        lines("class APIRouter:", "    pass", "router = APIRouter()"),
        lines("class CustomRouter:", "    pass", "router = CustomRouter()"),
        lines("class CustomRouter(object):", "    pass", "router = CustomRouter()"),
        lines("class FastAPIApp(dict):", "    pass", "router = FastAPIApp()"),
        lines(
            "from mylib import BaseRouter",
            "class Router(BaseRouter):",
            "    pass",
            "router = Router()",
        ),
    ],
    ids=["local-apirouter", "plain", "object", "dict", "imported-base"],
)
def test_a_familiar_name_is_never_a_router(head: str) -> None:
    source = head + lines('@router.get("/users")', "def users(): ...")
    http = read(source)
    assert (http.subclasses, http.class_refusals) == ({}, {})
    assert receivers(source) == {}
    assert _http(source) == []


# -- what a class is refused for -----------------------------------------------------------


@pytest.mark.parametrize(
    "bases", ["APIRouter, Mixin", "Mixin, APIRouter"], ids=["framework-first", "framework-last"]
)
def test_more_than_one_base_is_refused(bases: str) -> None:
    source = routed(lines("class Mixin:", "    pass", cls(base=bases)))
    assert read(source).class_refusals == {"C": "multiple-bases"}
    nothing_built(source)


@pytest.mark.parametrize(
    ("bases", "reason"),
    [
        ("APIRouter, metaclass=M", "metaclass"),
        ("APIRouter, flag=True", "class-keywords"),
        ("APIRouter, **options", "class-keywords"),
    ],
    ids=["metaclass", "keyword", "double-star"],
)
def test_a_class_keyword_is_refused(bases: str, reason: str) -> None:
    source = routed(cls(base=bases))
    assert read(source).class_refusals == {"C": reason}
    nothing_built(source)


@pytest.mark.parametrize("decorator", ["@decorate", "@dataclass"])
def test_a_class_decorator_is_refused(decorator: str) -> None:
    source = routed(f"{decorator}\n{cls()}")
    assert read(source).class_refusals == {"C": "class-decorated"}
    nothing_built(source)


@pytest.mark.parametrize(
    ("member", "reason"),
    [
        (name, "overridden-route-method")
        for name in (
            "get",
            "post",
            "api_route",
            "add_api_route",
            "include_router",
            "route",
            "add_url_rule",
            "register_blueprint",
            "register",
            "make_setup_state",
            "url_rule_class",
        )
    ]
    + [
        (name, "overridden-special-method")
        for name in ("_method_route", "__call__", "__getattr__", "__new__", "__init_subclass__")
    ],
)
def test_a_member_the_framework_registers_or_serves_through_is_refused(
    member: str, reason: str
) -> None:
    source = routed(cls(f"def {member}(self, *args, **kwargs): ..."))
    assert read(source).class_refusals == {"C": reason}
    nothing_built(source)


_RATE_ROUTER = """\
class RateRouter(APIRouter):
    def __init__(self, prefix, service_name):
        super().__init__()
        self.service_name = service_name
        self.add_api_route(f"{prefix}/rates", self.get_rates, methods=["GET"])
        self.add_api_route(f"{prefix}/rates", self.set_rates, methods=["PUT"])

    def get_rates(self):
        return {"service": self.service_name, "rates": []}

    def set_rates(self, body: dict):
        return {"service": self.service_name, "accepted": True}
"""


def test_a_router_that_adds_its_routes_at_construction_is_refused() -> None:
    source = routed(_RATE_ROUTER, 'RateRouter("/pricing", "pricing")')
    assert read(source).class_refusals == {"RateRouter": "overridden-init-with-effect"}
    assert receivers(source) == {"app": "fastapi-app"}
    assert _served(source) == set()


@pytest.mark.parametrize(
    "init",
    [
        'def __init__(self, *a, **k):\n    super().__init__(*a, prefix="/x", **k)',
        "def __init__(self, **k):\n    super().__init__(**k)",
        "def __init__(self, *a, **k):\n    super(C, self).__init__(*a, **k)",
        "def __init__(self, *a, **k):\n    super().__init__(*a, **k)\n    self.x = 1",
        "def __init__(self, *a, **k):\n    pass",
        "async def __init__(self, *a, **k):\n    super().__init__(*a, **k)",
        "def __init__(self, *a, **k):\n    super().__init__(*k, **a)",
        "def __init__(self, *a, **k):\n    super().__init__(*a)",
        "def __init__(self, *a, x=1, **k):\n    super().__init__(*a, **k)",
        "def __init__(self, a, *args, **k):\n    super().__init__(*args, **k)",
        "def __init__(self, *a, **k):\n    return super().__init__(*a, **k)",
    ],
    ids=[
        "keyword-added",
        "no-vararg",
        "explicit-super",
        "second-statement",
        "no-call",
        "async",
        "swapped",
        "no-kwargs",
        "keyword-only",
        "extra-parameter",
        "returns",
    ],
)
def test_an_init_that_does_more_than_pass_its_arguments_on_is_refused(init: str) -> None:
    source = routed(cls(init))
    assert read(source).class_refusals == {"C": "overridden-init-with-effect"}
    nothing_built(source)


@pytest.mark.parametrize(
    "init",
    [
        "def __init__(self, *args, **kwargs):\n    super().__init__(*args, **kwargs)",
        'def __init__(self, *args, **kwargs):\n    """Build it."""\n'
        "    super().__init__(*args, **kwargs)",
        "def __init__(self, *args: Any, **kwargs: Any) -> None:\n"
        "    super().__init__(*args, **kwargs)",
        "def __init__(this, *a, **k):\n    super().__init__(*a, **k)",
    ],
    ids=["bare", "docstring", "annotated", "renamed"],
)
def test_an_init_that_only_passes_its_arguments_on_is_accepted(init: str) -> None:
    source = routed(cls(init), 'C(prefix="/users")')
    source = source.replace('@router.get("/users")', '@router.get("/me")')
    assert "C" in read(source).subclasses
    assert read(source).class_refusals == {}
    assert _served(source) == {"GET /users/me"}


def test_a_passthrough_init_is_refused_where_a_star_import_could_rebind_super() -> None:
    init = "def __init__(self, *args, **kwargs):\n    super().__init__(*args, **kwargs)"
    starred = routed(cls(init), imports=lines(STAR, FASTAPI))
    assert read(starred).class_refusals == {"C": "overridden-init-with-effect"}
    nothing_built(starred)
    # The same class with no __init__ is read under the same star.
    plain = routed(cls(), imports=lines(STAR, FASTAPI))
    assert "C" in read(plain).subclasses
    assert _served(plain) == {"GET /users"}
    # And a passthrough where nothing could rebind super is accepted.
    assert "C" in read(routed(cls(init))).subclasses


def test_a_module_that_binds_super_has_no_passthrough_init() -> None:
    init = "def __init__(self, *args, **kwargs):\n    super().__init__(*args, **kwargs)"
    source = routed(cls(init)) + "super = None\n"
    assert read(source).class_refusals == {"C": "overridden-init-with-effect"}


@pytest.mark.parametrize(
    "klass",
    [
        lines(cls(), cls()),
        lines(cls(), "C = make()"),
        lines(cls(), "del C"),
        lines(cls(), "def f():", "    global C"),
        lines(cls(), "import C"),
    ],
    ids=["defined-twice", "assigned", "deleted", "global", "imported"],
)
def test_a_class_name_bound_more_than_once_is_refused(klass: str) -> None:
    source = routed(klass)
    assert read(source).class_refusals == {"C": "class-rebound"}
    nothing_built(source)


def test_a_star_import_after_the_class_leaves_the_module_unproven() -> None:
    source = routed(cls()) + STAR + "\n"
    http = read(source)
    assert (http.subclasses, http.class_refusals) == ({}, {})
    assert _served(source) == set()


@pytest.mark.parametrize(
    "extra",
    ['router = C(prefix="/other")', "def g(router): ..."],
    ids=["constructed-twice", "parameter"],
)
def test_a_receiver_name_bound_twice_is_no_receiver_and_the_class_stands(extra: str) -> None:
    source = routed(cls()) + extra + "\n"
    assert "C" in read(source).subclasses
    assert read(source).class_refusals == {}
    assert receivers(source) == {"app": "fastapi-app"}
    assert _served(source) == set()


@pytest.mark.parametrize(
    ("klass", "construct"),
    [
        ("def make():\n    class C(APIRouter):\n        pass\n    return C()", "make()"),
        ("if True:\n    class C(APIRouter):\n        pass", "C()"),
        ("try:\n    class C(APIRouter):\n        pass\nexcept Exception:\n    pass", "C()"),
        ("class Outer:\n    class C(APIRouter):\n        pass", "Outer.C()"),
    ],
    ids=["function", "if", "try", "nested"],
)
def test_a_class_that_is_not_a_statement_of_the_module_body_is_ignored(
    klass: str, construct: str
) -> None:
    source = routed(klass, construct)
    http = read(source)
    assert (http.subclasses, http.class_refusals) == ({}, {})
    assert receivers(source) == {"app": "fastapi-app"}
    assert _served(source) == set()


def test_a_subclass_with_no_routes_changes_only_what_it_constructs() -> None:
    source = lines(
        FASTAPI,
        cls(),
        "app = FastAPI()",
        'router = C(prefix="/users")',
        "app.include_router(router)",
    )
    assert _served(source) == set()
    assert claims(source, PYTHON_ROUTE) == []
    receiver = dump(claims(source, PYTHON_RECEIVER))
    assert receiver == dump(claims(control(source, "C", "APIRouter"), PYTHON_RECEIVER))
    assert len(receiver) == 2


@pytest.mark.parametrize(
    "escape",
    ["C.get = f", 'setattr(C, "get", f)', "alias = C", "register(C)", "isinstance(x, C)"],
    ids=["attribute", "setattr", "alias", "argument", "isinstance"],
)
def test_a_class_read_other_than_to_build_extend_or_annotate_is_refused(escape: str) -> None:
    source = routed(lines(cls(), escape))
    assert read(source).class_refusals == {"C": "class-escapes"}
    nothing_built(source)


def test_a_class_a_dependent_extends_takes_its_dependents_with_it() -> None:
    source = lines(
        "from fastapi import APIRouter",
        cls(name="A"),
        cls(base="A", name="B"),
        "A.get = f",
        "r = B()",
        '@r.get("/x")',
        "def x(): ...",
    )
    http = read(source)
    assert http.subclasses == {}
    assert http.class_refusals == {"A": "class-escapes", "B": "base-refused"}
    assert receivers(source) == {}


def test_a_class_in_an_annotation_that_calls_nothing_is_not_an_escape() -> None:
    source = lines(
        FASTAPI,
        cls(),
        "app = FastAPI()",
        "router: C = C()",
        '@router.get("/users")',
        "def users(r: C) -> C: ...",
        "app.include_router(router)",
    )
    assert "C" in read(source).subclasses
    assert _served(source) == {"GET /users"}
    called = source.replace("r: C) -> C", "r: Annotated[C, Depends()]) -> C")
    assert called != source
    assert read(called).class_refusals == {"C": "class-escapes"}


def test_a_construction_before_the_class_statement_is_no_receiver() -> None:
    source = lines(
        FASTAPI,
        "app = FastAPI()",
        "router = C()",
        cls(),
        '@router.get("/users")',
        "def users(): ...",
        "app.include_router(router)",
    )
    assert "C" in read(source).subclasses
    assert receivers(source) == {"app": "fastapi-app"}
    assert _served(source) == set()


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        ('prefix = "/x"', "class-body-statement"),
        ("route_class = X", "class-body-statement"),
        ("x: int", "class-body-statement"),
        ("@property\ndef prefix(self): ...", "decorated-member"),
        ("@staticmethod\ndef helper(): ...", "decorated-member"),
    ],
    ids=["prefix", "route-class", "annotation", "property", "staticmethod"],
)
def test_a_class_body_that_could_change_what_it_serves_is_refused(row: str, reason: str) -> None:
    source = routed(cls(row))
    assert read(source).class_refusals == {"C": reason}
    nothing_built(source)


def test_a_plain_documented_method_does_not_stop_the_class() -> None:
    source = routed(cls('"""A router."""', "def describe(self):\n    return 1", "..."))
    assert read(source).class_refusals == {}
    assert _served(source) == {"GET /users"}


def test_a_route_declared_in_a_class_body_is_never_read() -> None:
    source = lines(
        FASTAPI,
        cls('@router.get("/x")\ndef x(self): ...'),
        "app = FastAPI()",
        "router = C()",
        "app.include_router(router)",
    )
    assert read(source).class_refusals == {"C": "decorated-member"}
    assert _served(source) == set()
    assert [r for r in _http(source) if r.target_key == "GET /x"] == []


def test_a_star_import_after_the_framework_import_unproves_the_base() -> None:
    source = routed(cls(), imports=lines(FASTAPI, STAR))
    http = read(source)
    assert (http.subclasses, http.class_refusals) == ({}, {})
    assert _served(source) == set()
    above = routed(cls(), imports=lines(STAR, FASTAPI))
    assert "C" in read(above).subclasses
    assert _served(above) == {"GET /users"}


_HEAD = lines(
    FASTAPI,
    cls(),
    "app = FastAPI()",
    'users = C(prefix="/users")',
    'items = C(prefix="/items")',
    '@users.get("/me")',
    "def me(): ...",
    '@items.post("")',
    "def create_item(): ...",
    "routers = [users, items]",
)


def test_subclass_routers_are_mounted_by_a_comprehension_over_a_list() -> None:
    source = _HEAD + '[app.include_router(r, prefix="/api") for r in routers]\n'
    assert _served(source) == {"GET /api/users/me", "POST /api/items"}
    assert read(source).comprehension_outcomes == [(line(source, "[app.include"), "unrolled")]


def test_a_prefix_composed_from_module_constants_is_read_for_a_subclass() -> None:
    source = lines(
        FASTAPI,
        cls(),
        'BASE = "/v1"',
        'P = f"{BASE}/users"',
        "app = FastAPI()",
        "router = C(prefix=P)",
        '@router.get("/me")',
        "def me(): ...",
        "app.include_router(router)",
    )
    assert _served(source) == {"GET /v1/users/me"}
    imported = lines(FASTAPI, "from conf import PREFIX", cls(), "app = FastAPI()") + lines(
        "router = C(prefix=PREFIX)",
        '@router.get("/me")',
        "def me(): ...",
        "app.include_router(router)",
    )
    http = read(imported)
    assert [reason for _, reason in http.prefix_refusals] == ["prefix-imported"]
    assert _served(imported) == set()
    assert receivers(imported) == {"app": "fastapi-app"}


_FACTORY = lines(
    "from flask import Blueprint, Flask",
    cls(base="Flask", name="App"),
    'bp = Blueprint("bp", __name__)',
    '@bp.route("/a")',
    "def a(): ...",
    "def create_app():",
    "    app = App(__name__)",
    '    app.register_blueprint(bp, url_prefix="/x")',
    "    return app",
)


def test_a_subclass_application_in_a_factory_mounts_as_the_framework_one_does() -> None:
    mounts = claims(_FACTORY, PYTHON_MOUNT)
    [mount] = mounts
    assert (mount.meta["owner"], mount.meta["owner_kind"]) == ("create_app.app", "flask-app")
    assert (mount.meta["name"], mount.meta["readable"], mount.meta["prefix"]) == (
        "bp",
        "true",
        '"/x"',
    )
    assert dump(mounts) == dump(claims(control(_FACTORY, "App", "Flask"), PYTHON_MOUNT))


def test_a_factory_whose_application_class_is_a_parameter_mounts_nothing() -> None:
    parameter = _FACTORY.replace("def create_app():", "def create_app(App):")
    assert parameter != _FACTORY
    assert claims(parameter, PYTHON_MOUNT) == []
    assert read(parameter).class_refusals == {"App": "class-rebound"}


def test_a_subclass_application_publishes_through_flasgger_as_the_framework_one_does() -> None:
    source = lines(
        "from flask import Flask",
        "from flasgger import Swagger",
        cls(base="Flask", name="App"),
        "app = App(__name__)",
        'Swagger(app, template={"host": "a.example", "basePath": "/v1", "schemes": ["https"]})',
        '@app.get("/items/<int:item_id>")',
        "def item(item_id): ...",
    )

    def exposures(text: str) -> list[ParsedReference]:
        return [
            r
            for r in _parse(text).references
            if r.edge_type is EdgeType.EXPOSES_HTTP_ENDPOINT
            or r.meta.get(PENDING_FACT) == HTTP_EXPOSED_ENDPOINT
        ]

    found = exposures(source)
    assert {r.target_key for r in found} == {"GET https://a.example/v1/items/<int:item_id>"}
    assert len(found) == 2
    assert dump(found) == dump(exposures(control(source, "App", "Flask")))


# -- the global pass, and incremental equals fresh ---------------------------------------------

ROUTES = d("""
    from fastapi import APIRouter


    class C(APIRouter):
        pass


    router = C(prefix="/users")


    @router.get("/me")
    def me():
        return {}
""")
MAIN = d("""
    from fastapi import FastAPI

    from routes import router

    app = FastAPI()
    app.include_router(router, prefix="/api")

    if __name__ == "__main__":
        pass
""")
FILES = {"svc": {"src/routes.py": ROUTES, "src/main.py": MAIN}}
ME = ("svc", "src/routes.py", "me", "GET /api/users/me", None)


def swapped(text: str, old: str, new: str) -> str:
    assert old in text
    return text.replace(old, new)


def test_a_subclass_router_is_served_through_another_module(
    workspace: Workspace, tmp_path: Path
) -> None:
    indexed = build(workspace, tmp_path, FILES)
    assert served(indexed) == {ME}
    assert outcomes(indexed)[("src/routes.py", "me", "GET /me")] == PROMOTED


def test_a_subclass_defined_in_another_module_serves_nothing_yet(
    workspace: Workspace, tmp_path: Path
) -> None:
    base = d("""
        from fastapi import APIRouter


        class CustomRouter(APIRouter):
            pass
    """)
    routes = d("""
        from base import CustomRouter

        router = CustomRouter()


        @router.get("/users")
        def get_users():
            return []
    """)
    files = {"svc": {"src/base.py": base, "src/routes.py": routes, "src/main.py": MAIN}}
    indexed = build(workspace, tmp_path, files)
    assert "CustomRouter" in read(base).subclasses
    assert served(indexed) == set()
    assert [key for key in outcomes(indexed) if key[1] == "get_users"] == []


def test_editing_the_prefix_of_a_subclass_router_moves_its_route(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    indexed = build(workspace, tmp_path, FILES)
    root = tmp_path / "repos"
    assert served(indexed) == {ME}

    edit(root, "svc", "src/routes.py", swapped(ROUTES, 'prefix="/users"', 'prefix="/people"'))
    sync_all(indexed, root)
    assert served(indexed) == {("svc", "src/routes.py", "me", "GET /api/people/me", None)}
    assert_equals_fresh(indexed, settings, root, tmp_path, "subclass-prefix")


def test_renaming_the_base_unserves_the_subclass_router(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    indexed = build(workspace, tmp_path, FILES)
    root = tmp_path / "repos"
    assert served(indexed) == {ME}

    renamed = swapped(ROUTES, "class C(APIRouter):", "class C(APIRouterX):")
    edit(root, "svc", "src/routes.py", renamed)
    sync_all(indexed, root)
    assert served(indexed) == set()
    assert_equals_fresh(indexed, settings, root, tmp_path, "subclass-renamed")


def test_overriding_a_registration_method_unserves_and_reverting_serves_again(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    indexed = build(workspace, tmp_path, FILES)
    root = tmp_path / "repos"
    assert served(indexed) == {ME}

    overridden = swapped(
        ROUTES,
        "class C(APIRouter):\n    pass",
        "class C(APIRouter):\n    def get(self, *args, **kwargs):\n        return None",
    )
    edit(root, "svc", "src/routes.py", overridden)
    sync_all(indexed, root)
    assert served(indexed) == set()
    assert_equals_fresh(indexed, settings, root, tmp_path, "subclass-overridden")

    edit(root, "svc", "src/routes.py", ROUTES)
    sync_all(indexed, root)
    assert served(indexed) == {ME}
    assert_equals_fresh(indexed, settings, root, tmp_path, "subclass-reverted")


def test_deleting_the_class_unserves_the_route_its_construction_named(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    indexed = build(workspace, tmp_path, FILES)
    root = tmp_path / "repos"
    assert served(indexed) == {ME}

    edit(root, "svc", "src/routes.py", swapped(ROUTES, "class C(APIRouter):\n    pass\n\n\n", ""))
    sync_all(indexed, root)
    assert served(indexed) == set()
    assert_equals_fresh(indexed, settings, root, tmp_path, "subclass-deleted")


def test_an_index_written_before_the_rule_is_reparsed_on_upgrade(
    workspace: Workspace, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Worker processes do not see a monkeypatch: parse in this one.
    monkeypatch.setenv("REPOATLAS_SYNC_WORKERS", "1")
    current = PythonHttp._classes
    # The old reader: a subclass of a framework class is no constructor.
    monkeypatch.setattr(PythonHttp, "_classes", lambda self, tree, counts: None)
    indexed = build(workspace, tmp_path, FILES)
    assert served(indexed) == set()

    live = facts_version(indexed.registry, indexed.dependency_registry)
    version = f"{PythonParser.name}:{PythonParser.version}"
    assert version in live, live
    # A literal, not "one less than now": a relative offset would pass with the bump deleted.
    previous = live.replace(version, f"python-ast:24+{interpreter_tag()}")
    assert previous != live
    indexed.database.execute(
        "UPDATE files SET parser_version = ? WHERE rel_path LIKE '%.py'", (previous,)
    )
    indexed.database.connection.commit()

    monkeypatch.setattr(PythonHttp, "_classes", current)
    root = tmp_path / "repos"
    report = sync(indexed, indexed.resolver().resolve(root))
    assert sum(r.changed for r in report.repositories) >= 1, report
    assert served(indexed) == {ME}
    assert_equals_fresh(indexed, settings, root, tmp_path, "subclass-upgraded")


# -- execution and bounds ----------------------------------------------------------------------

_BOOM = lines(
    "import pathlib",
    "from fastapi import APIRouter",
    "class Quiet(APIRouter):",
    "    pass",
    "class Loud(APIRouter):",
    "    def __init__(self, *args, **kwargs):",
    '        pathlib.Path(__file__).with_name("EXECUTED").write_text("ran")',
    "        raise SystemExit(1)",
    "router = Loud()",
    'pathlib.Path(__file__).with_name("EXECUTED").write_text("ran")',
    "raise SystemExit(1)",
)


def test_parsing_a_subclass_never_runs_it(workspace: Workspace, tmp_path: Path) -> None:
    parsed = PythonParser().parse(Path("boom.py"), _BOOM)
    assert [r for r in parsed.references if r.meta.get(PENDING_FACT) == PYTHON_RECEIVER] == []
    assert read(_BOOM).subclasses.keys() == {"Quiet"}
    build(workspace, tmp_path, {"boomrepo": {"boom.py": _BOOM}})
    assert list(tmp_path.rglob("EXECUTED")) == []
    assert "boom" not in sys.modules


def test_a_very_long_chain_and_a_very_wide_class_are_bounded() -> None:
    rows = ["from fastapi import APIRouter", "class C1(APIRouter):", "    pass"]
    for k in range(2, 5001):
        rows += [f"class C{k}(C{k - 1}):", "    pass"]
    http = read(lines(*rows))
    assert set(http.subclasses) == {"C1", "C2", "C3"}
    assert http.class_refusals["C4"] == "depth-exceeded"
    assert len(http.class_refusals) == 4997
    assert {http.class_refusals[f"C{k}"] for k in range(5, 5001)} == {"base-refused"}

    bases = ", ".join(["APIRouter", *(f"B{k}" for k in range(5000))])
    wide = lines("from fastapi import APIRouter", f"class W({bases}):", "    pass")
    assert read(wide).class_refusals == {"W": "multiple-bases"}
