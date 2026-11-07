"""An application subclass defined in one module and built in another (A1, after M6).

``class CustomApp(FastAPI): pass`` in ``base.py`` is offered to the global pass as a claim; a module
that builds an application from it -- ``from base import CustomApp`` then ``app = CustomApp()``
(form a), or ``class App(CustomApp)`` then ``app = App()`` (form b) -- records the routes on that
application as claims of their own, which the global pass serves at the application's root when the
defining module offers the class (at most three classes above the framework's, those of both
modules together). A router or blueprint built from an offered class is a router or blueprint, its
routes served where a mount reaches it. The claim is never a fact until then, and a class that is
not proven is only a diagnostic (``PythonRoutePlan.class_unproven``),
never an outcome, never counted, never declared.

Half of this file is what is refused. Every fixture is read from its syntax tree only, never
imported or run (V1_SPEC §17).
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.graph.linker import ReferenceLinker, claim_role, plan_repository_http
from repoatlas.graph.py_modules import PythonModules
from repoatlas.graph.py_routes import (
    CLASS_NOT_OFFERED,
    CLASS_TOO_DEEP,
    MODULE_UNRESOLVED,
    NOT_MOUNTED,
    PROMOTED,
    WRONG_KIND,
    PythonRoutePlan,
    plan_python_routes,
)
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.http_inventory import HttpOperationsRequest, OperationRole
from repoatlas.models.parsing import EdgeType, EntityType
from repoatlas.parsers.http_facts import (
    PENDING_FACT,
    PYTHON_CLASS,
    PYTHON_CLASS_ROUTE,
    PYTHON_ROUTE,
)
from repoatlas.services.workspace import Workspace
from repoatlas.storage.protocols import UnresolvedRefRecord
from tests.impact_corpus import build
from tests.incremental_corpus import assert_equals_fresh, edit, sync_all
from tests.test_http_python import _http, _served
from tests.test_http_python_routers import line, outcomes, served
from tests.test_http_python_subclasses import claims, lines, read

GUARD = ('if __name__ == "__main__":', "    pass")
USERS = ('@app.get("/users")', "def get_users(): ...")
USERS_AT = '@app.get("/users")'

FASTAPI_BASE = lines("from fastapi import FastAPI", "class CustomApp(FastAPI):", "    pass")
FLASK_BASE = lines("from flask import Flask", "class CustomApp(Flask):", "    pass")
ROUTER_BASE = lines("from fastapi import APIRouter", "class CustomRouter(APIRouter):", "    pass")


def module(*rows: str) -> str:
    """A module of the rows given, run as a script so that its directory is on the import path."""
    return lines(*rows, *GUARD)


FORM_A = module("from base import CustomApp", "app = CustomApp()", *USERS)
FORM_B = module(
    "from base import CustomApp", "class App(CustomApp):", "    pass", "app = App()", *USERS
)
FLASK_A = module(
    "from base import CustomApp",
    "app = CustomApp(__name__)",
    '@app.route("/hello")',
    "def hello(): ...",
    '@app.post("/save")',
    "def save(): ...",
)
FLASK_B = module(
    "from base import CustomApp",
    "class App(CustomApp):",
    "    pass",
    "app = App(__name__)",
    '@app.route("/hello")',
    "def hello(): ...",
    '@app.post("/save")',
    "def save(): ...",
)
FASTAPI_ROUTES = [("get_users", "GET /users", USERS_AT)]
FLASK_ROUTES = [
    ("hello", "GET /hello", '@app.route("/hello")'),
    ("save", "POST /save", '@app.post("/save")'),
]


def files(base: str, app: str) -> dict[str, dict[str, str]]:
    return {"svc": {"src/base.py": base, "src/app.py": app}}


def expected(app: str, routes: list[tuple[str, str, str]]) -> set[tuple[str, str, str, str, int]]:
    return {("svc", "src/app.py", handler, route, line(app, at)) for handler, route, at in routes}


def count(workspace: Workspace, role: str) -> int:
    """How many stored claims carry a role."""
    row = workspace.database.execute(
        "SELECT COUNT(*) FROM unresolved_refs WHERE meta_json LIKE ?", (f'%"{role}"%',)
    ).fetchone()
    return int(row[0])


def class_claims(workspace: Workspace) -> dict[tuple[str, str, str], int]:
    """Each class-route claim's id, by (file, handler, method)."""
    rows = workspace.database.execute(
        """
        SELECT u.id, f.rel_path, s.qualified_name, u.meta_json FROM unresolved_refs u
        JOIN entities s ON s.id = u.src_id JOIN files f ON f.id = u.file_id
        WHERE u.meta_json LIKE '%"python-class-route"%'
        """
    ).fetchall()
    return {(row[1], row[2], json.loads(row[3])["method"]): row[0] for row in rows}


def python_plan(workspace: Workspace, slug: str = "svc") -> PythonRoutePlan:
    """The Python route plan of one repository, with the diagnostic field the linker drops."""
    [repo_id] = [
        row[0]
        for row in workspace.database.execute(
            "SELECT id FROM repositories WHERE slug = ?", (slug,)
        ).fetchall()
    ]
    found = plan_repository_http(workspace.knowledge, repo_id)
    grouped: dict[str, list[UnresolvedRefRecord]] = defaultdict(list)
    for claim in found.claims:
        grouped[claim_role(claim) or ""].append(claim)
    by_id = {row.file_id: row for row in found.files}
    facts = [fact for fact in workspace.knowledge.list_module_facts() if fact.file_id in by_id]

    def modules() -> PythonModules:
        return PythonModules(found.index, facts)

    return plan_python_routes(grouped, modules, found.index, by_id)


def nothing_served(source: str) -> None:
    """The module records no class route (and so serves nothing) on its own."""
    assert claims(source, PYTHON_CLASS_ROUTE) == []
    assert _served(source) == set()


# -- recognition and serving -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base", "app", "routes"),
    [(FASTAPI_BASE, FORM_A, FASTAPI_ROUTES), (FLASK_BASE, FLASK_A, FLASK_ROUTES)],
    ids=["fastapi", "flask"],
)
def test_an_application_class_imported_from_another_module_is_served_where_it_is_built(
    workspace: Workspace, tmp_path: Path, base: str, app: str, routes: list[tuple[str, str, str]]
) -> None:
    indexed = build(workspace, tmp_path, files(base, app))
    assert served(indexed) == expected(app, routes)
    # The importer's routes are claims of their own: no python-route claim stands behind them.
    assert count(indexed, PYTHON_ROUTE) == 0
    assert count(indexed, PYTHON_CLASS_ROUTE) == len(routes)
    plan = python_plan(indexed)
    assert set(plan.outcomes.values()) == {PROMOTED}
    assert set(plan.outcomes) == set(plan.promoted) == set(class_claims(indexed).values())
    assert (plan.resolved, plan.unresolved, plan.class_unproven) == (len(routes), 0, {})
    assert outcomes(indexed) == {}


@pytest.mark.parametrize(
    ("base", "app", "routes"),
    [(FASTAPI_BASE, FORM_B, FASTAPI_ROUTES), (FLASK_BASE, FLASK_B, FLASK_ROUTES)],
    ids=["fastapi", "flask"],
)
def test_a_class_of_this_module_extending_an_imported_application_class_is_served(
    workspace: Workspace, tmp_path: Path, base: str, app: str, routes: list[tuple[str, str, str]]
) -> None:
    indexed = build(workspace, tmp_path, files(base, app))
    assert served(indexed) == expected(app, routes)
    assert count(indexed, PYTHON_ROUTE) == 0
    assert set(python_plan(indexed).outcomes.values()) == {PROMOTED}
    http = read(app)
    [found] = http.imported_subclasses.values()
    assert (found.ref.module, found.ref.name, found.depth, found.base) == (
        "base",
        "CustomApp",
        1,
        None,
    )
    # What an imported class is can only be said by the global pass: it never lands among the
    # classes this module proves, and an imported base is no refusal there.
    assert (http.subclasses, http.class_refusals) == ({}, {})


def test_a_chain_of_classes_in_the_importer_extends_the_imported_class() -> None:
    source = module(
        "from base import CustomApp",
        "class A(CustomApp):",
        "    pass",
        "class B(A):",
        "    pass",
        "app = B()",
        *USERS,
    )
    http = read(source)
    assert {n: (s.ref.name, s.depth, s.base) for n, s in http.imported_subclasses.items()} == {
        "A": ("CustomApp", 1, None),
        "B": ("CustomApp", 2, "A"),
    }
    [claim] = claims(source, PYTHON_CLASS_ROUTE)
    built = (claim.meta["module"], claim.meta["class"], claim.meta["depth"])
    assert built == ("base", "CustomApp", "2")


def test_a_defining_module_offers_each_subclass_of_every_kind() -> None:
    chain = lines(
        "from fastapi import APIRouter, FastAPI",
        "class A(FastAPI):",
        "    pass",
        "class B(A):",
        "    pass",
        "class R(APIRouter):",
        "    pass",
    )
    offered = {c.target_key: (c.meta["kind"], c.meta["depth"]) for c in claims(chain, PYTHON_CLASS)}
    assert offered == {
        "A": ("fastapi-app", "1"),
        "B": ("fastapi-app", "2"),
        "R": ("fastapi-router", "1"),
    }
    assert {c.target_key: c.meta["kind"] for c in claims(FLASK_BASE, PYTHON_CLASS)} == {
        "CustomApp": "flask-app"
    }
    assert {c.target_key: c.meta["kind"] for c in claims(ROUTER_BASE, PYTHON_CLASS)} == {
        "CustomRouter": "fastapi-router"
    }
    blueprint = lines("from flask import Blueprint", "class P(Blueprint):", "    pass")
    assert {c.target_key: c.meta["kind"] for c in claims(blueprint, PYTHON_CLASS)} == {
        "P": "flask-blueprint"
    }


# -- what is not served, and why ---------------------------------------------------------------


ROUTER_APP = module("from base import CustomRouter", "app = CustomRouter()", *USERS)
FLASK_ROUTE_ON_FASTAPI = module(
    "from base import CustomApp",
    "app = CustomApp()",
    '@app.route("/users")',
    "def get_users(): ...",
)
NO_MODULE = module("from nowhere import CustomApp", "app = CustomApp()", *USERS)
NO_CLASS = module("from base import Missing", "app = Missing()", *USERS)


@pytest.mark.parametrize(
    ("base", "app", "reason"),
    [
        (FASTAPI_BASE, FLASK_ROUTE_ON_FASTAPI, WRONG_KIND),
        (FASTAPI_BASE, NO_MODULE, MODULE_UNRESOLVED),
        (FASTAPI_BASE, NO_CLASS, CLASS_NOT_OFFERED),
    ],
    ids=["wrong-family", "module-unresolved", "class-missing"],
)
def test_a_class_nothing_proves_an_application_is_only_a_diagnostic(
    workspace: Workspace, tmp_path: Path, base: str, app: str, reason: str
) -> None:
    indexed = build(workspace, tmp_path, files(base, app))
    assert served(indexed) == set()
    [claim_id] = class_claims(indexed).values()
    plan = python_plan(indexed)
    assert plan.class_unproven == {claim_id: reason}
    # Never an outcome, never counted, never declared -- here or in the plan the linker keeps.
    assert claim_id not in plan.outcomes and claim_id not in plan.declared
    assert (plan.resolved, plan.unresolved, plan.routes) == (0, 0, [])
    kept = ReferenceLinker(indexed.knowledge).prepare_global().http
    assert claim_id not in kept.routes and claim_id not in kept.declared
    assert outcomes(indexed) == {}


def test_a_route_on_a_router_built_from_an_offered_class_is_a_router_route(
    workspace: Workspace, tmp_path: Path
) -> None:
    # Nothing includes the router: a declared route that is not mounted, as an APIRouter's is.
    indexed = build(workspace, tmp_path, files(ROUTER_BASE, ROUTER_APP))
    assert served(indexed) == set()
    [claim_id] = class_claims(indexed).values()
    plan = python_plan(indexed)
    assert plan.class_unproven == {}
    assert plan.outcomes == {claim_id: NOT_MOUNTED}
    assert plan.declared == {claim_id: "GET /users"}
    assert (plan.resolved, plan.unresolved) == (0, 1)


IMPORT = "from base import CustomApp"
CLASS = ("class App(CustomApp):", "    pass")
IMPORT_AFTER_CLASS = module(*CLASS, IMPORT, "app = App()", *USERS)
IMPORT_AFTER_CONSTRUCTION = module("app = CustomApp()", IMPORT, *USERS)
CONSTRUCTION_BEFORE_CLASS = module(IMPORT, "app = App()", *CLASS, *USERS)


@pytest.mark.parametrize(
    ("late", "ordered"),
    [
        (IMPORT_AFTER_CLASS, FORM_B),
        (IMPORT_AFTER_CONSTRUCTION, FORM_A),
        (CONSTRUCTION_BEFORE_CLASS, FORM_B),
    ],
    ids=["import-after-class", "import-after-construction", "construction-before-class"],
)
def test_an_import_or_class_below_the_statement_that_needs_it_builds_nothing(
    workspace: Workspace, tmp_path: Path, late: str, ordered: str
) -> None:
    nothing_served(late)
    assert len(claims(ordered, PYTHON_CLASS_ROUTE)) == 1
    indexed = build(workspace, tmp_path, files(FASTAPI_BASE, late))
    assert served(indexed) == set()
    assert count(indexed, PYTHON_CLASS_ROUTE) == 0


def test_an_import_below_the_class_is_refused_with_its_reason() -> None:
    assert read(IMPORT_AFTER_CLASS).imported_class_refusals == {"App": "imported-after-class"}


# -- an imported module's attribute -------------------------------------------------------------

PKG_BASE = lines("from fastapi import FastAPI", "class Base(FastAPI):", "    pass")


@pytest.mark.parametrize(
    "app",
    [
        module("import pkg.base as b", "class App(b.Base):", "    pass", "app = App()", *USERS),
        module("import pkg.base as b", "app = b.Base()", *USERS),
        module("from pkg import base", "class App(base.Base):", "    pass", "app = App()", *USERS),
    ],
    ids=["extended", "constructed", "submodule-import"],
)
def test_a_class_read_through_an_imported_module_is_served(
    workspace: Workspace, tmp_path: Path, app: str
) -> None:
    indexed = build(workspace, tmp_path, {"svc": {"src/pkg/base.py": PKG_BASE, "src/app.py": app}})
    assert served(indexed) == expected(app, FASTAPI_ROUTES)


@pytest.mark.parametrize(
    "escape",
    [
        "b.Base.get = f",
        "register(b)",
        "b.Base = Other",
        'setattr(b.Base, "get", f)',
        'b.__dict__["Base"].get = f',
        'b.__setattr__("Base", Other)',
    ],
    ids=[
        "attribute-store",
        "alias-argument",
        "rebound",
        "class-argument",
        "module-dict",
        "module-setattr",
    ],
)
def test_an_imported_module_or_class_read_other_than_to_build_or_extend_builds_nothing(
    workspace: Workspace, tmp_path: Path, escape: str
) -> None:
    escaping = module(
        "import pkg.base as b", "class App(b.Base):", "    pass", escape, "app = App()", *USERS
    )
    plain = escaping.replace(escape + "\n", "")
    assert plain != escaping and len(claims(plain, PYTHON_CLASS_ROUTE)) == 1
    nothing_served(escaping)
    assert read(escaping).imported_class_refusals == {"App": "class-escapes"}
    indexed = build(
        workspace, tmp_path, {"svc": {"src/pkg/base.py": PKG_BASE, "src/app.py": escaping}}
    )
    assert served(indexed) == set()


@pytest.mark.parametrize(
    "escape",
    ["base.CustomApp.get = f", "base.CustomApp = Other", 'base.__dict__["CustomApp"].get = f'],
    ids=["attribute-store", "rebound", "module-dict"],
)
@pytest.mark.parametrize("form", ["constructed", "extended"])
def test_a_class_changed_through_the_module_that_also_defines_it_builds_nothing(
    workspace: Workspace, tmp_path: Path, escape: str, form: str
) -> None:
    head = (
        ["app = CustomApp()"]
        if form == "constructed"
        else ["class App(CustomApp):", "    pass", "app = App()"]
    )
    escaping = module("import base", "from base import CustomApp", escape, *head, *USERS)
    plain = escaping.replace(escape + "\n", "")
    assert plain != escaping and len(claims(plain, PYTHON_CLASS_ROUTE)) == 1
    nothing_served(escaping)
    if form == "extended":
        assert read(escaping).imported_class_refusals == {"App": "class-escapes"}
    indexed = build(workspace, tmp_path, files(FASTAPI_BASE, escaping))
    assert served(indexed) == set()


BESIDE_BASE = ("import base", "from base import CustomApp")
BESIDE_DOTTED = ("import a.b", "from a.b import CustomApp")


@pytest.mark.parametrize(
    ("imports", "escape", "defined_in"),
    [
        (BESIDE_BASE, 'getattr(base, "CustomApp").get = f', "src/base.py"),
        (BESIDE_BASE, 'vars(base)["CustomApp"].get = f', "src/base.py"),
        (BESIDE_DOTTED, "a.b.CustomApp.get = f", "src/a/b.py"),
        (("from base import CustomApp",), 'x = "CustomApp"', "src/base.py"),
    ],
    ids=["getattr", "vars", "dotted-path", "string"],
)
@pytest.mark.parametrize("form", ["constructed", "extended"])
def test_a_class_changed_through_another_spelling_of_its_module_builds_nothing(
    workspace: Workspace,
    tmp_path: Path,
    imports: tuple[str, ...],
    escape: str,
    defined_in: str,
    form: str,
) -> None:
    head = (
        ["app = CustomApp()"]
        if form == "constructed"
        else ["class App(CustomApp):", "    pass", "app = App()"]
    )
    escaping = module(*imports, escape, *head, *USERS)
    plain = escaping.replace(escape + "\n", "")
    assert plain != escaping and len(claims(plain, PYTHON_CLASS_ROUTE)) == 1
    nothing_served(escaping)
    if form == "extended":
        assert read(escaping).imported_class_refusals == {"App": "class-escapes"}
    sources = {defined_in: FASTAPI_BASE, "src/app.py": escaping}
    assert served(build(workspace, tmp_path, {"svc": sources})) == set()


@pytest.mark.parametrize(
    ("imports", "reads", "defined_in"),
    [
        (BESIDE_BASE, ("name = base.__name__", "helper = base.helper"), "src/base.py"),
        (BESIDE_DOTTED, ("name = a.b.__name__", "helper = a.b.helper"), "src/a/b.py"),
    ],
    ids=["beside-base", "dotted-path"],
)
def test_a_module_read_that_names_no_class_keeps_the_claim(
    workspace: Workspace,
    tmp_path: Path,
    imports: tuple[str, ...],
    reads: tuple[str, ...],
    defined_in: str,
) -> None:
    source = module(*imports, *reads, "app = CustomApp()", *USERS)
    assert len(claims(source, PYTHON_CLASS_ROUTE)) == 1
    sources = {defined_in: FASTAPI_BASE, "src/app.py": source}
    indexed = build(workspace, tmp_path, {"svc": sources})
    assert served(indexed) == expected(source, FASTAPI_ROUTES)


def test_a_module_attribute_that_is_not_the_class_may_be_read_freely() -> None:
    source = module(
        "import pkg.base as b",
        "name = b.__name__",
        "class App(b.Base):",
        "    pass",
        "app = App()",
        *USERS,
    )
    assert len(claims(source, PYTHON_CLASS_ROUTE)) == 1


# -- the importer's own escapes ------------------------------------------------------------------


@pytest.mark.parametrize(
    "escape",
    ["App.get = f", "register(App)", "alias = App", "isinstance(x, App)"],
    ids=["attribute", "argument", "alias", "isinstance"],
)
def test_a_class_of_the_importer_read_other_than_to_build_extend_or_annotate_builds_nothing(
    workspace: Workspace, tmp_path: Path, escape: str
) -> None:
    source = module(IMPORT, *CLASS, escape, "app = App()", *USERS)
    nothing_served(source)
    assert read(source).imported_class_refusals == {"App": "class-escapes"}
    assert read(source).imported_subclasses == {}
    indexed = build(workspace, tmp_path, files(FASTAPI_BASE, source))
    assert served(indexed) == set()


@pytest.mark.parametrize(
    "escape",
    ["CustomApp.get = f", "register(CustomApp)", "isinstance(x, CustomApp)"],
    ids=["attribute", "argument", "isinstance"],
)
@pytest.mark.parametrize("form", ["constructed", "extended"])
def test_an_imported_class_read_other_than_to_build_or_extend_builds_nothing(
    escape: str, form: str
) -> None:
    head = (
        ["app = CustomApp()"]
        if form == "constructed"
        else ["class App(CustomApp):", "    pass", "app = App()"]
    )
    source = module("from base import CustomApp", escape, *head, *USERS)
    nothing_served(source)
    if form == "extended":
        assert read(source).imported_class_refusals == {"App": "class-escapes"}


def test_a_class_a_dependent_extends_takes_its_dependents_with_it() -> None:
    source = module(
        "from base import CustomApp",
        "class A(CustomApp):",
        "    pass",
        "class B(A):",
        "    pass",
        "A.get = f",
        "app = B()",
        *USERS,
    )
    http = read(source)
    assert http.imported_subclasses == {}
    assert http.imported_class_refusals == {"A": "class-escapes", "B": "base-refused"}
    nothing_served(source)


def test_a_class_in_an_annotation_that_calls_nothing_is_not_an_escape() -> None:
    source = module(
        "from base import CustomApp",
        "app: CustomApp = CustomApp()",
        USERS_AT,
        "def get_users(a: CustomApp) -> CustomApp: ...",
    )
    assert len(claims(source, PYTHON_CLASS_ROUTE)) == 1


@pytest.mark.parametrize(
    ("klass", "reason"),
    [
        ("class App(CustomApp, Mixin):\n    pass", "multiple-bases"),
        ("class App(CustomApp, metaclass=M):\n    pass", "metaclass"),
        ("@decorate\nclass App(CustomApp):\n    pass", "class-decorated"),
        ("class App(CustomApp):\n    def get(self, *a, **k): ...", "overridden-route-method"),
        ("class App(CustomApp):\n    prefix = '/x'", "class-body-statement"),
        ("class App(CustomApp[int]):\n    pass", "dynamic-base"),
    ],
    ids=["bases", "metaclass", "decorated", "route-method", "body", "subscript"],
)
def test_a_class_of_the_importer_is_held_to_the_rules_of_a_local_one(
    klass: str, reason: str
) -> None:
    source = module("from base import CustomApp", klass, "app = App()", *USERS)
    http = read(source)
    assert http.imported_class_refusals == {"App": reason}
    assert (http.imported_subclasses, http.subclasses, http.class_refusals) == ({}, {}, {})
    nothing_served(source)


USERS_ON_C = ('@c.get("/x")', "def x(): ...")


@pytest.mark.parametrize(
    "imports",
    ["from fastapi.routing import APIRouter", "from flask import Flask as F"],
    ids=["framework-submodule", "framework-alias"],
)
def test_a_framework_import_is_not_another_modules_class(imports: str) -> None:
    name = imports.rsplit(" ", 1)[1]
    source = lines(imports, f"class C({name}):", "    pass", "c = C()", *USERS_ON_C)
    http = read(source)
    assert http.imported_subclasses == {} and http.imported_class_refusals == {}


# -- a mounted sub-application ----------------------------------------------------------------


def test_an_application_mounted_as_a_sub_application_is_not_served_at_its_root() -> None:
    mounted = module(
        "from fastapi import FastAPI",
        "from base import CustomApp",
        "main = FastAPI()",
        "app = CustomApp()",
        'main.mount("/sub", app)',
        *USERS,
    )
    nothing_served(mounted)
    assert len(claims(mounted.replace('main.mount("/sub", app)\n', ""), PYTHON_CLASS_ROUTE)) == 1


def test_a_receiver_mounted_on_an_imported_class_application_is_not_served_at_its_root() -> None:
    mounted = module(
        "from fastapi import FastAPI",
        "from base import CustomApp",
        "app = CustomApp()",
        "sub = FastAPI()",
        'app.mount("/sub", sub)',
        '@sub.get("/users")',
        "def get_users(): ...",
    )
    assert _served(mounted) == set()
    plain = mounted.replace('app.mount("/sub", sub)\n', "")
    assert plain != mounted and _served(plain) == {"GET /users"}


# -- the claims ------------------------------------------------------------------------------


def test_the_claims_are_symbol_references_of_their_own_role_and_never_an_http_fact() -> None:
    [route] = claims(FORM_A, PYTHON_CLASS_ROUTE)
    assert route.meta[PENDING_FACT] == "python-class-route"
    assert route.edge_type is EdgeType.REFERENCES
    assert route.target_kind is EntityType.SYMBOL
    assert route.target_key == "app"
    assert route.source_symbol == "get_users"
    assert (route.line_start, route.line_end) == (line(FORM_A, USERS_AT),) * 2
    assert {k: route.meta[k] for k in ("module", "class", "depth", "family", "method")} == {
        "module": "base",
        "class": "CustomApp",
        "depth": "0",
        "family": "either",
        "method": "GET",
    }
    assert json.loads(route.meta["path"]) == [["t", "/users"]]
    assert route.meta["provenance"] == "exact"
    assert _http(FORM_A) == []
    assert claims(FORM_A, PYTHON_ROUTE) == []
    [offered] = claims(FASTAPI_BASE, PYTHON_CLASS)
    assert (offered.edge_type, offered.target_kind) == (EdgeType.REFERENCES, EntityType.SYMBOL)
    assert (offered.target_key, offered.line_start) == ("CustomApp", 2)
    assert (offered.meta["kind"], offered.meta["depth"]) == ("fastapi-app", "1")
    assert _http(FASTAPI_BASE) == []


# -- depth ---------------------------------------------------------------------------------------


def chain(base_depth: int, local_depth: int) -> tuple[str, str]:
    """A defining module with ``base_depth`` classes above FastAPI, and an importer with
    ``local_depth`` more above the last of them, building an application from the top."""
    rows = ["from fastapi import FastAPI", "class C1(FastAPI):", "    pass"]
    for k in range(2, base_depth + 1):
        rows += [f"class C{k}(C{k - 1}):", "    pass"]
    top = f"C{base_depth}"
    head = [f"from base import {top}"]
    last = top
    for k in range(1, local_depth + 1):
        head += [f"class L{k}({last}):", "    pass"]
        last = f"L{k}"
    return lines(*rows), module(*head, f"app = {last}()", *USERS)


@pytest.mark.parametrize(
    ("base_depth", "local_depth"),
    [(1, 0), (3, 0), (2, 1), (1, 2), (1, 3), (3, 1), (2, 2)],
)
def test_the_classes_of_both_modules_together_are_at_most_three(
    workspace: Workspace, tmp_path: Path, base_depth: int, local_depth: int
) -> None:
    base, app = chain(base_depth, local_depth)
    indexed = build(workspace, tmp_path, files(base, app))
    [claim_id] = class_claims(indexed).values()
    plan = python_plan(indexed)
    if base_depth + local_depth <= 3:
        assert served(indexed) == expected(app, FASTAPI_ROUTES)
        assert plan.class_unproven == {}
    else:
        assert served(indexed) == set()
        assert plan.class_unproven == {claim_id: CLASS_TOO_DEEP}
        assert plan.outcomes == {}


# -- the inventory --------------------------------------------------------------------------------


def providers(workspace: Workspace) -> list[tuple[str | None, str, str | None, int | None]]:
    context = CapabilityContext(scope=CapabilityScope.REPOSITORY, repository="svc")
    response = CapabilityService(workspace).http_operations(
        HttpOperationsRequest(context=context, role=OperationRole.PROVIDER)
    )
    return [(r.method, r.route, r.path, r.line_start) for r in response.operations]


def test_a_promoted_class_route_is_listed_as_a_provider(
    workspace: Workspace, tmp_path: Path
) -> None:
    indexed = build(workspace, tmp_path, files(FASTAPI_BASE, FORM_A))
    assert providers(indexed) == [("GET", "/users", "src/app.py", line(FORM_A, USERS_AT))]


def test_a_class_route_nothing_proves_is_no_provider_and_not_declared(
    workspace: Workspace, tmp_path: Path
) -> None:
    indexed = build(workspace, tmp_path, files(FASTAPI_BASE, NO_CLASS))
    assert providers(indexed) == []


# -- incremental equals fresh ---------------------------------------------------------------------

REFUSED_BASE = lines(
    "from fastapi import FastAPI",
    "class CustomApp(FastAPI):",
    "    def get(self, *args, **kwargs):",
    "        return None",
)


def test_editing_only_the_defining_module_unserves_and_serves_again(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    indexed = build(workspace, tmp_path, files(FASTAPI_BASE, FORM_A))
    root = tmp_path / "repos"
    here = expected(FORM_A, FASTAPI_ROUTES)
    assert served(indexed) == here
    assert_equals_fresh(indexed, settings, root, tmp_path, "cross-built")

    edit(root, "svc", "src/base.py", REFUSED_BASE)
    sync_all(indexed, root)
    assert served(indexed) == set()
    [claim_id] = class_claims(indexed).values()
    assert python_plan(indexed).class_unproven == {claim_id: CLASS_NOT_OFFERED}
    assert_equals_fresh(indexed, settings, root, tmp_path, "cross-refused")

    edit(root, "svc", "src/base.py", FASTAPI_BASE)
    sync_all(indexed, root)
    assert served(indexed) == here
    assert_equals_fresh(indexed, settings, root, tmp_path, "cross-reverted")


def test_editing_the_importer_into_a_late_import_unserves_it(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    indexed = build(workspace, tmp_path, files(FASTAPI_BASE, FORM_B))
    root = tmp_path / "repos"
    assert served(indexed) == expected(FORM_B, FASTAPI_ROUTES)

    late = module(
        "class App(CustomApp):", "    pass", "from base import CustomApp", "app = App()", *USERS
    )
    edit(root, "svc", "src/app.py", late)
    sync_all(indexed, root)
    assert served(indexed) == set()
    assert_equals_fresh(indexed, settings, root, tmp_path, "cross-late")


# -- the defining module's own escapes ----------------------------------------------------------


@pytest.mark.parametrize("app", [FORM_A, FORM_B], ids=["constructed", "extended"])
@pytest.mark.parametrize(
    "escape", ["register(CustomApp)", "CustomApp.get = f"], ids=["argument", "attribute"]
)
def test_a_class_that_escapes_in_its_defining_module_serves_nothing_in_the_importer(
    workspace: Workspace, tmp_path: Path, escape: str, app: str
) -> None:
    base = FASTAPI_BASE + escape + "\n"
    assert claims(base, PYTHON_CLASS) == []
    assert len(claims(FASTAPI_BASE, PYTHON_CLASS)) == 1
    indexed = build(workspace, tmp_path, files(base, app))
    assert served(indexed) == set()
    [claim_id] = class_claims(indexed).values()
    assert python_plan(indexed).class_unproven == {claim_id: CLASS_NOT_OFFERED}
