"""R2c synthetic repositories: Flask app-factory blueprint registration (synthetic only).

Each repository is one shape, named after the shape. Sources are the Phase A probe fixtures (P1..A9)
plus the shapes added for the R2c-1 gate (P2b, P4b, P5, P5x, P6, A10..A23). Expectations live in
``tests/test_r2c_characterization.py``, not here. The shapes mirror the flask-realworld structure
(create_app -> register_blueprints(app) -> app.register_blueprint(pkg.module.blueprint)).
"""

from __future__ import annotations

from tests.f1_f2_corpus import python_repo


def _lines(*lines: str) -> str:
    return "\n".join(lines) + "\n"


VIEWS = _lines(
    "from flask import Blueprint",
    "",
    'bp = Blueprint("v", __name__)',
    "",
    "",
    '@bp.route("/one")',
    "def one():",
    "    return {}",
)
VIEWS_B = _lines(
    "from flask import Blueprint",
    "",
    'bp = Blueprint("b", __name__, url_prefix="/bp")',
    "",
    "",
    '@bp.route("/two")',
    "def two():",
    "    return {}",
)
EMPTY = ""
USERS_INIT_BOUND = "from . import views\n"

# P1: one-level module attribute, registered in the factory (control).
P1_APP = _lines(
    "from flask import Flask",
    "",
    "from pkg import views",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    '    app.register_blueprint(views.bp, url_prefix="/c1")',
    "    return app",
)

# P2: two-level chain (user.views.blueprint), factory registration, no helper.
P2_APP = _lines(
    "from flask import Flask",
    "",
    "from pkg import users",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    "    app.register_blueprint(users.views.bp)",
    "    return app",
)

# P3: a factory that calls a same-module helper with the app (the flask-realworld shape, minimal).
P3_APP = _lines(
    "from flask import Flask",
    "",
    "from pkg import views",
    "",
    "",
    "def register_blueprints(app):",
    "    app.register_blueprint(views.bp)",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    "    register_blueprints(app)",
    "    return app",
)

# P4: P3 plus parameter reads that are not registration, and an extension given the app.
P4_APP = _lines(
    "from flask import Flask",
    "",
    "from pkg import users, views",
    "from pkg.ext import cors",
    "",
    "",
    "def register_blueprints(app):",
    '    app.config.get("X", None)',
    "    cors.init_app(app)",
    "    app.register_blueprint(views.bp)",
    "    app.register_blueprint(users.views.bp)",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    "    app.url_map.strict_slashes = False",
    "    register_blueprints(app)",
    "    return app",
)
EXT = _lines(
    "class _Cors:", "    def init_app(self, app):", "        return None", "", "", "cors = _Cors()"
)

# P5: a module-level helper called at module level with the module-level app.
P5_APP = _lines(
    "from flask import Flask",
    "from pkg import views",
    "",
    "app = Flask(__name__)",
    "",
    "",
    "def register(application):",
    '    application.register_blueprint(views.bp, url_prefix="/m")',
    "",
    "",
    "register(app)",
)
# P5x: as P5 but the helper's parameter is itself named ``app``.
P5X_APP = _lines(
    "from flask import Flask",
    "from pkg import views",
    "",
    "app = Flask(__name__)",
    "",
    "",
    "def register(app):",
    '    app.register_blueprint(views.bp, url_prefix="/m")',
    "",
    "",
    "register(app)",
)
# P6: the factory hands the app to an outer helper that hands it to an inner helper.
P6_APP = _lines(
    "from flask import Flask",
    "from pkg import views",
    "",
    "",
    "def inner(app):",
    "    app.register_blueprint(views.bp)",
    "",
    "",
    "def outer(app):",
    "    inner(app)",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    "    outer(app)",
    "    return app",
)

# Adversarial -----------------------------------------------------------------------------------
# A1: conditional registration in the factory (must not serve).
A1_APP = _lines(
    "from flask import Flask",
    "from pkg import views",
    "",
    "DEBUG = False",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    "    if DEBUG:",
    "        app.register_blueprint(views.bp)",
    "    return app",
)
# A2: dynamic lookup of the blueprint (must not serve).
A2_APP = _lines(
    "from flask import Flask",
    "from pkg import views",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    '    app.register_blueprint(getattr(views, "bp"))',
    "    return app",
)
# A3: helper called with the app and the blueprint as a parameter (must not serve
# unless proven; here the helper receives an unrelated object).
A3_APP = _lines(
    "from flask import Flask",
    "from pkg import views",
    "",
    "",
    "def register(app, other):",
    "    app.register_blueprint(other)",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    "    register(app, views.bp)",
    "    return app",
)
# A4: the helper is rebound before the call (shadowing).
A4_APP = _lines(
    "from flask import Flask",
    "from pkg import views",
    "",
    "",
    "def register_blueprints(app):",
    "    app.register_blueprint(views.bp)",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    "    register_blueprints = lambda a: None",
    "    register_blueprints(app)",
    "    return app",
)
# A5: cyclic helper calls (must terminate, serve once or refuse).
A5_APP = _lines(
    "from flask import Flask",
    "from pkg import views",
    "",
    "",
    "def helper_a(app):",
    "    app.register_blueprint(views.bp)",
    "    helper_b(app)",
    "",
    "",
    "def helper_b(app):",
    "    helper_a(app)",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    "    helper_a(app)",
    "    return app",
)
# A6: two factories register the same blueprint under two prefixes (each app serves its own).
A6_APP = _lines(
    "from flask import Flask",
    "from pkg import views",
    "",
    "",
    "def make_a():",
    "    app = Flask(__name__)",
    '    app.register_blueprint(views.bp, url_prefix="/a")',
    "    return app",
    "",
    "",
    "def make_b():",
    "    app = Flask(__name__)",
    '    app.register_blueprint(views.bp, url_prefix="/b")',
    "    return app",
)
# A7: registration with an unresolved import (must not serve).
A7_APP = _lines(
    "from flask import Flask",
    "from pkg.missing import gone",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    "    app.register_blueprint(gone)",
    "    return app",
)
# A8: Blueprint has its own url_prefix; registration overrides it (Flask semantics).
A8_APP = _lines(
    "from flask import Flask",
    "from pkg import views_b",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    '    app.register_blueprint(views_b.bp, url_prefix="/over")',
    "    return app",
)
# A9: the app is aliased before registration (one-hop alias).
A9_APP = _lines(
    "from flask import Flask",
    "from pkg import views",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    "    alias = app",
    "    alias.register_blueprint(views.bp)",
    "    return app",
)
# A10: a five-deep helper chain.
A10_APP = _lines(
    "from flask import Flask",
    "from pkg import views",
    "",
    "",
    "def h5(app):",
    "    app.register_blueprint(views.bp)",
    "",
    "",
    "def h4(app):",
    "    h5(app)",
    "",
    "",
    "def h3(app):",
    "    h4(app)",
    "",
    "",
    "def h2(app):",
    "    h3(app)",
    "",
    "",
    "def h1(app):",
    "    h2(app)",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    "    h1(app)",
    "    return app",
)
# A11: the helper rebinds its parameter to a fresh app before registering.
A11_APP = _lines(
    "from flask import Flask",
    "from pkg import views",
    "",
    "",
    "def register(app):",
    "    app = Flask(__name__)",
    "    app.register_blueprint(views.bp)",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    "    register(app)",
    "    return app",
)
# A12: the helper call is conditional.
A12_APP = _lines(
    "from flask import Flask",
    "from pkg import views",
    "",
    "ENABLED = False",
    "",
    "",
    "def register_blueprints(app):",
    "    app.register_blueprint(views.bp)",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    "    if ENABLED:",
    "        register_blueprints(app)",
    "    return app",
)
# A13: the app is passed through a star-args call.
A13_APP = _lines(
    "from flask import Flask",
    "from pkg import views",
    "",
    "",
    "def register_blueprints(app):",
    "    app.register_blueprint(views.bp)",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    "    register_blueprints(*[app])",
    "    return app",
)
# A14: the helper is called before the app is constructed.
A14_APP = _lines(
    "from flask import Flask",
    "from pkg import views",
    "",
    "",
    "def register_blueprints(app):",
    "    app.register_blueprint(views.bp)",
    "",
    "",
    "def create_app():",
    "    register_blueprints(app)",
    "    app = Flask(__name__)",
    "    return app",
)
# A15: the helper lives in another module (R2c-1b territory).
A15_SETUP = _lines(
    "from pkg import views",
    "",
    "",
    "def register_blueprints(app):",
    "    app.register_blueprint(views.bp)",
)
A15_APP = _lines(
    "from flask import Flask",
    "from pkg.setup import register_blueprints",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    "    register_blueprints(app)",
    "    return app",
)
# A16: the helper is decorated.
A16_APP = _lines(
    "from flask import Flask",
    "from pkg import views",
    "",
    "",
    "def deco(f):",
    "    return f",
    "",
    "",
    "@deco",
    "def register_blueprints(app):",
    "    app.register_blueprint(views.bp)",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    "    register_blueprints(app)",
    "    return app",
)
# A17: the app's wsgi_app is wrapped before the helper is called.
A17_APP = _lines(
    "from flask import Flask",
    "from pkg import views",
    "",
    "",
    "def mw(wsgi):",
    "    return wsgi",
    "",
    "",
    "def register_blueprints(app):",
    "    app.register_blueprint(views.bp)",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    "    app.wsgi_app = mw(app.wsgi_app)",
    "    register_blueprints(app)",
    "    return app",
)
# A18: P2 with a package __init__ that rebinds ``views`` after importing it.
A18_USERS_INIT = "from . import views\nviews = None\n"
# A19: P2 with a package __init__ that imports only the blueprint from the submodule.
A19_USERS_INIT = "from .views import bp\n"
# A20: a three-level attribute chain a.b.c.bp.
A20_APP = (
    "from flask import Flask\nfrom pkg import a\n\n\ndef create_app():\n"
    "    app = Flask(__name__)\n    app.register_blueprint(a.b.c.bp)\n    return app\n"
)
A20_A_INIT = "from . import b\n"
A20_B_INIT = "from . import c\n"
A20_C = (
    "from flask import Blueprint\n\nbp = Blueprint('c', __name__)\n\n\n"
    "@bp.route('/three')\ndef three():\n    return {}\n"
)
# A21: a route whose path is a constant read from another module.
A21_CONSTS = "P = '/p'\n"
A21_VC = (
    "from flask import Blueprint\nfrom pkg.users import consts\n\n"
    "bp = Blueprint('c', __name__)\n\n\n@bp.route(consts.P)\ndef p():\n    return {}\n"
)
A21_APP = (
    "from flask import Flask\nfrom pkg import vc\n\n\ndef create_app():\n"
    "    app = Flask(__name__)\n    app.register_blueprint(vc.bp)\n    return app\n"
)
# A21b: as A21, but the constant is read through a two-level attribute (users.consts.P).
A21B_CONSTS = "P = '/p2'\n"
A21B_VC = (
    "from flask import Blueprint\nfrom pkg import users\n\n"
    'bp = Blueprint("vc2", __name__)\n\n\n@bp.route(users.consts.P)\ndef p():\n    return {}\n'
)
A21B_APP = (
    "from flask import Flask\nfrom pkg import vc2\n\n\ndef create_app():\n"
    "    app = Flask(__name__)\n    app.register_blueprint(vc2.bp)\n    return app\n"
)
# A22: the helper's positional parameters do not line up with the call (app is the second argument).
A22_APP = _lines(
    "from flask import Flask",
    "from pkg import views",
    "",
    "",
    "def register(app, cfg):",
    "    app.register_blueprint(views.bp)",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    "    cfg = object()",
    "    register(cfg, app)",
    "    return app",
)
# A23: the helper is called before the module that holds the blueprint is bound.
A23_APP = _lines(
    "from flask import Flask",
    "",
    "",
    "def register(application):",
    "    application.register_blueprint(views.bp)",
    "",
    "",
    "app = Flask(__name__)",
    "register(app)",
    "from pkg import views",
)

# R2c-1 G1 regression guards and the claim-recording shape. They are NOT part of ``repos()``: the
# base golden files cover exactly the 33 shapes of ``repos()`` and are frozen (``guard_repos()``).
# P7: a helper with nothing recordable (a read, and a two-level registration): no helper claim.
P7_APP = _lines(
    "from flask import Flask",
    "from pkg import users",
    "",
    "",
    "def helper(app):",
    '    app.config.get("X")',
    "    app.register_blueprint(users.views.bp)",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    "    helper(app)",
    "    return app",
)
# A24: the helper's child is imported inside a conditional (not a top-level statement).
A24_APP = _lines(
    "from flask import Flask",
    "",
    "DEBUG_FLAG = False",
    "",
    "",
    "def register_extras(app):",
    "    if DEBUG_FLAG:",
    "        from pkg.extras import extras_bp",
    "    app.register_blueprint(extras_bp)",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    "    register_extras(app)",
    "    return app",
)
A24_EXTRAS = (
    "from flask import Blueprint\n\nextras_bp = Blueprint('extras', __name__)\n\n\n"
    "@extras_bp.route('/extras')\ndef extras():\n    return {}\n"
)
# A25: the helper is called under a conditional (control flow).
A25_APP = _lines(
    "from flask import Flask",
    "from pkg import views",
    "",
    "DEBUG_FLAG = False",
    "",
    "",
    "def register_views(app):",
    "    app.register_blueprint(views.bp)",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    "    if DEBUG_FLAG:",
    "        register_views(app)",
    "    return app",
)
# A26..A30 (guards for the H-M1, H-M7, H-M9, H-M4 and the position-check mutants). Each is a shape
# whose helper registration must NOT be mounted: expected for ALL of them is served == [] and
# plan == () (no code-route plan row either).
# A26: the registration sits inside an ``if`` in the helper (control flow in the helper body).
# Expected: served [] and plan ().
A26_APP = _lines(
    "from flask import Flask",
    "from pkg import views",
    "",
    "DEBUG = False",
    "",
    "",
    "def register(app):",
    "    if DEBUG:",
    "        app.register_blueprint(views.bp)",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    "    register(app)",
    "    return app",
)
# A27: the registration is made on an alias of the helper's parameter (``q = app``).
# Expected: served [] and plan ().
A27_APP = _lines(
    "from flask import Flask",
    "from pkg import views",
    "",
    "",
    "def register(app):",
    "    q = app",
    "    q.register_blueprint(views.bp)",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    "    register(app)",
    "    return app",
)
# A28a: the call passes the application after a starred argument (``register(*pre, app)``).
# Expected: served [] and plan ().
A28A_APP = _lines(
    "from flask import Flask",
    "from pkg import views",
    "",
    "",
    "def register(app, *more):",
    "    app.register_blueprint(views.bp)",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    "    pre = []",
    "    register(*pre, app)",
    "    return app",
)
# A28b: the application first, then a starred argument (``register(app, *pre)``).
# Expected: served [] and plan ().
A28B_APP = _lines(
    "from flask import Flask",
    "from pkg import views",
    "",
    "",
    "def register(app, *more):",
    "    app.register_blueprint(views.bp)",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    "    pre = []",
    "    register(app, *pre)",
    "    return app",
)
# A28c: the application first, then a double-starred argument (``register(app, **opts)``).
# Expected: served [] and plan ().
A28C_APP = _lines(
    "from flask import Flask",
    "from pkg import views",
    "",
    "",
    "def register(app, **kw):",
    "    app.register_blueprint(views.bp)",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    "    opts = {}",
    "    register(app, **opts)",
    "    return app",
)
# A29: the child is the helper's own parameter (``bp``), and a module-level ``bp`` is imported too.
# Expected: served [] and plan ().
A29_APP = _lines(
    "from flask import Flask",
    "from pkg import views",
    "from pkg.views import bp",
    "",
    "",
    "def register(app, bp):",
    "    app.register_blueprint(bp)",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    "    register(app, views.bp)",
    "    return app",
)
# A30: helper to helper with the application in a different position (``inner(None, app)``).
# Expected: served [] and plan ().
A30_APP = _lines(
    "from flask import Flask",
    "from pkg import views",
    "",
    "",
    "def inner(app, other):",
    "    app.register_blueprint(views.bp)",
    "",
    "",
    "def outer(app):",
    "    inner(None, app)",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    "    outer(app)",
    "    return app",
)

#: The files every shape shares; a shape's own files replace or extend them.
BASE_FILES = {
    "pkg/__init__.py": EMPTY,
    "pkg/views.py": VIEWS,
    "pkg/views_b.py": VIEWS_B,
    "pkg/users/__init__.py": EMPTY,
    "pkg/users/views.py": VIEWS_B.replace('"/bp"', '"/u"'),
    "pkg/ext.py": EXT,
}


def _materialize(
    shapes: dict[str, tuple[str, dict[str, str]]],
) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for name, (app_source, extra) in shapes.items():
        files = {**BASE_FILES, **extra}
        files["pkg/app.py"] = app_source
        out[name] = python_repo(name, "flask", files)
    return out


def guard_repos() -> dict[str, dict[str, str]]:
    """The shapes added at G1 (P7, A24..A30); none is in the base golden files."""
    return _materialize(
        {
            "p7_helper_no_registration": (P7_APP, {}),
            "a24_conditional_child_import": (A24_APP, {"pkg/extras.py": A24_EXTRAS}),
            "a25_conditional_helper_call": (A25_APP, {}),
            "a26_nested_registration": (A26_APP, {}),
            "a27_param_alias": (A27_APP, {}),
            "a28a_starred_call": (A28A_APP, {}),
            "a28b_starred_tail": (A28B_APP, {}),
            "a28c_double_star": (A28C_APP, {}),
            "a29_param_child": (A29_APP, {}),
            "a30_helper_to_helper_position": (A30_APP, {}),
        }
    )


def repos() -> dict[str, dict[str, str]]:
    #: shape name -> (pkg/app.py text, files that replace or extend the shared base)
    shapes: dict[str, tuple[str, dict[str, str]]] = {
        "p1_one_level": (P1_APP, {}),
        "p2_two_level": (P2_APP, {}),
        "p2b_two_level_bound": (P2_APP, {"pkg/users/__init__.py": USERS_INIT_BOUND}),
        "p3_helper": (P3_APP, {}),
        "p4_helper_reads": (P4_APP, {}),
        "p4b_helper_reads_bound": (P4_APP, {"pkg/users/__init__.py": USERS_INIT_BOUND}),
        "p5_module_helper": (P5_APP, {}),
        "p5x_param_named_app": (P5X_APP, {}),
        "p6_outer_inner": (P6_APP, {}),
        "a1_conditional": (A1_APP, {}),
        "a2_dynamic": (A2_APP, {}),
        "a3_param_other": (A3_APP, {}),
        "a4_shadow": (A4_APP, {}),
        "a5_cycle": (A5_APP, {}),
        "a6_two_factories": (A6_APP, {}),
        "a7_unresolved": (A7_APP, {}),
        "a8_url_prefix": (A8_APP, {}),
        "a9_alias": (A9_APP, {}),
        "a10_chain5": (A10_APP, {}),
        "a11_rebound_param": (A11_APP, {}),
        "a12_conditional_call": (A12_APP, {}),
        "a13_star_args": (A13_APP, {}),
        "a14_before_construction": (A14_APP, {}),
        "a15_imported_helper": (A15_APP, {"pkg/setup.py": A15_SETUP}),
        "a16_decorated_helper": (A16_APP, {}),
        "a17_wsgi_wrap": (A17_APP, {}),
        "a18_init_rebound": (P2_APP, {"pkg/users/__init__.py": A18_USERS_INIT}),
        "a19_init_from_views_only": (P2_APP, {"pkg/users/__init__.py": A19_USERS_INIT}),
        "a20_three_levels": (
            A20_APP,
            {
                "pkg/a/__init__.py": A20_A_INIT,
                "pkg/a/b/__init__.py": A20_B_INIT,
                "pkg/a/b/c.py": A20_C,
            },
        ),
        "a21_constant_path": (A21_APP, {"pkg/users/consts.py": A21_CONSTS, "pkg/vc.py": A21_VC}),
        "a21b_two_level_constant_path": (
            A21B_APP,
            {"pkg/users/consts.py": A21B_CONSTS, "pkg/vc2.py": A21B_VC},
        ),
        "a22_positional_mismatch": (A22_APP, {}),
        "a23_call_before_child_bound": (A23_APP, {}),
    }
    return _materialize(shapes)
