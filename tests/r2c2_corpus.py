"""R2c-2 synthetic repositories: two-level module-attribute children (``users.views.bp``).

Each fixture is one repository: ``tests.r2c_provider_corpus.BASE_FILES`` plus the overrides listed
here (``B`` is ``pkg/users/__init__.py`` = ``from . import views``), as the R2c-2 probe indexed them
(``probe/r2c2_probe.py`` and ``probe/r2c2_probe_b.py``, copied verbatim; X14, a documented false
positive, stays bench-only). Expectations live in ``tests/test_r2c2_two_level.py``, not here.
"""

from __future__ import annotations

from tests.f1_f2_corpus import python_repo
from tests.r2c_provider_corpus import A20_A_INIT, A20_B_INIT, A20_C, BASE_FILES


def lines(*ls: str) -> str:
    return "\n".join(ls) + "\n"


B = {"pkg/users/__init__.py": "from . import views\n"}
USERS_VIEWS = BASE_FILES["pkg/users/views.py"]  # url_prefix "/u", GET /two declared at line 6

# ---- app texts ----------------------------------------------------------------------------------
P2_APP = lines(
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

Q1_APP = lines(
    "from flask import Flask",
    "",
    "from pkg import users",
    "",
    "",
    "def register_blueprints(app):",
    "    app.register_blueprint(users.views.bp)",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    "    register_blueprints(app)",
    "    return app",
)
Q2_APP = lines(
    "from flask import Flask",
    "from pkg import users",
    "",
    "app = Flask(__name__)",
    "app.register_blueprint(users.views.bp)",
)
Q3_APP = lines(
    "from flask import Flask",
    "",
    "from pkg import users as u",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    "    app.register_blueprint(u.views.bp)",
    "    return app",
)
Q6_APP = P2_APP.replace("from pkg import users", "from . import users")
Q7_APP = P2_APP.replace("users.views.bp)", 'users.views.bp, url_prefix="/x")')
Q8_ITEMS = (
    "from fastapi import APIRouter\n\n"
    'router = APIRouter(prefix="/items")\n\n\n'
    '@router.get("/list")\ndef listing():\n    return []\n'
)
Q8_APP = (
    "from fastapi import FastAPI\nfrom pkg import routers\n\n"
    "app = FastAPI()\napp.include_router(routers.items.router)\n"
)
Q8C_APP = (
    "from fastapi import FastAPI\nfrom pkg.routers import items\n\n"
    "app = FastAPI()\napp.include_router(items.router)\n"
)
Q9_APP = lines(
    "from flask import Flask",
    "",
    "",
    "def create_app():",
    "    from pkg import users",
    "    app = Flask(__name__)",
    "    app.register_blueprint(users.views.bp)",
    "    return app",
)
# X1: module-level rebinding right after the import; X1b: factory-local rebinding after the
# (module) import.
X1_APP = lines(
    "from flask import Flask",
    "",
    "from pkg import users",
    "",
    "users = None",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    "    app.register_blueprint(users.views.bp)",
    "    return app",
)
X1B_APP = lines(
    "from flask import Flask",
    "",
    "from pkg import users",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    "    users = object()",
    "    app.register_blueprint(users.views.bp)",
    "    return app",
)
X5_APP = lines(
    "from flask import Flask",
    "",
    "from pkg import ns",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    "    app.register_blueprint(ns.views.bp)",
    "    return app",
)
X9_APP = lines(
    "from flask import Flask",
    "",
    "from pkg import mod",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    "    app.register_blueprint(mod.sub.bp)",
    "    return app",
)
X11B_APP = lines(
    "from flask import Flask",
    "",
    "from pkg import a, views",
    "",
    "",
    "def register_blueprints(app):",
    "    app.register_blueprint(views.bp)",
    "    app.register_blueprint(a.b.c.bp)",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    "    register_blueprints(app)",
    "    return app",
)
# Q10 (formerly X12 of the 2026-11-05 probe): the import precedes the call, so it works at run time
# (positive control).
Q10_APP = lines(
    "from flask import Flask",
    "",
    "",
    "def register(app):",
    "    app.register_blueprint(users.views.bp)",
    "",
    "",
    "from pkg import users",
    "",
    "app = Flask(__name__)",
    "register(app)",
)
# X12 (corrected 2026-11-06): the call precedes the import, so it raises NameError at run time
# (before-binding).
X12_APP = lines(
    "from flask import Flask",
    "",
    "",
    "def register(app):",
    "    app.register_blueprint(users.views.bp)",
    "",
    "",
    "app = Flask(__name__)",
    "register(app)",
    "from pkg import users",
)
# Q10b (amendment C, 2026-11-06): Q10 with the helper parameter renamed so it does not collide with
# the module-level application name (see the P5x limitation).
Q10B_APP = lines(
    "from flask import Flask",
    "",
    "",
    "def register(application):",
    "    application.register_blueprint(users.views.bp)",
    "",
    "",
    "from pkg import users",
    "",
    "app = Flask(__name__)",
    "register(app)",
)
# X12b (amendment C, 2026-11-06): X12 with the helper parameter renamed (call before the import).
X12B_APP = lines(
    "from flask import Flask",
    "",
    "",
    "def register(application):",
    "    application.register_blueprint(users.views.bp)",
    "",
    "",
    "app = Flask(__name__)",
    "register(app)",
    "from pkg import users",
)
# X19 (2026-11-06): two-level child registered by a helper, the helper-local import follows the
# registration.
X19_APP = lines(
    "from flask import Flask",
    "",
    "",
    "def register_blueprints(app):",
    "    app.register_blueprint(users.views.bp)",
    "    from pkg import users",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    "    register_blueprints(app)",
    "    return app",
)
X15_APP = lines(
    "from flask import Flask",
    "",
    "from pkg import users, views",
    "",
    "DEBUG = True",
    "",
    "",
    "def register_blueprints(app):",
    "    app.register_blueprint(views.bp)",
    "    if DEBUG:",
    "        app.register_blueprint(users.views.bp)",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    "    register_blueprints(app)",
    "    return app",
)
X16_APP = (
    "from fastapi import FastAPI\n\nfrom pkg import subs\n\n"
    'app = FastAPI()\napp.mount("/s", subs.apps.sub)\n'
)
X16B_APP = (
    "from fastapi import FastAPI\n"
    "from pkg import subs\n"
    "\n"
    "\n"
    "def create_app():\n"
    "    app = FastAPI()\n"
    '    app.mount("/s", subs.apps.sub)\n'
    "    return app\n"
)
X18_APP = lines(
    "from flask import Flask",
    "",
    "import pkg.users as users",
    "",
    "",
    "def create_app():",
    "    app = Flask(__name__)",
    "    app.register_blueprint(users.views.bp)",
    "    return app",
)

A20_FILES = {
    "pkg/a/__init__.py": A20_A_INIT,
    "pkg/a/b/__init__.py": A20_B_INIT,
    "pkg/a/b/c.py": A20_C,
}

OTHER_VIEWS = USERS_VIEWS.replace('"/u"', '"/o"')  # X2, X20: pkg/other/views.py
OTHER_USERS_VIEWS = USERS_VIEWS.replace('"/u"', '"/ou"')  # X21: pkg/other/users/views.py


def fx(
    app: str | None, extra: dict[str, str] | None = None, drop: tuple[str, ...] = ()
) -> dict[str, str]:
    files = {k: v for k, v in BASE_FILES.items() if k not in drop}
    files.update(extra or {})
    if app is not None:
        files["pkg/app.py"] = app
    return files


def fastapi_files(app: str, extra: dict[str, str]) -> dict[str, str]:
    """Clean FastAPI repository: only pkg/__init__.py plus the fixture's own files."""
    return {"pkg/__init__.py": "", **extra, "pkg/app.py": app}


#: name -> (dependency, files)
FIXTURES: dict[str, tuple[str, dict[str, str]]] = {
    # Positive
    "Q1": ("flask", fx(Q1_APP, B)),
    "Q2": ("flask", fx(Q2_APP, B)),
    "Q3": ("flask", fx(Q3_APP, B)),
    "Q4": ("flask", fx(P2_APP, {"pkg/users/__init__.py": "from pkg.users import views\n"})),
    "Q5": ("flask", fx(P2_APP, {"pkg/users/__init__.py": "from . import views\n__all__ = []\n"})),
    "Q6": ("flask", fx(Q6_APP, B)),
    "Q7": ("flask", fx(Q7_APP, B)),
    "Q8": (
        "fastapi",
        fastapi_files(
            Q8_APP,
            {"pkg/routers/__init__.py": "from . import items\n", "pkg/routers/items.py": Q8_ITEMS},
        ),
    ),
    "Q8c": (
        "fastapi",
        fastapi_files(
            Q8C_APP,
            {"pkg/routers/__init__.py": "from . import items\n", "pkg/routers/items.py": Q8_ITEMS},
        ),
    ),
    "Q9": ("flask", fx(Q9_APP, B)),
    "Q10": ("flask", fx(Q10_APP, B)),
    "Q10b": ("flask", fx(Q10B_APP, B)),
    "X10": (
        "flask",
        fx(
            P2_APP,
            {
                "pkg/__init__.py": "from . import users\n",
                "pkg/users/__init__.py": "from pkg.users import views\n",
                "pkg/users/views.py": USERS_VIEWS + "from pkg import users\n",
            },
        ),
    ),
    # Adversarial
    "X1": ("flask", fx(X1_APP, B)),
    "X1b": ("flask", fx(X1B_APP, B)),
    "X2": (
        "flask",
        fx(
            P2_APP,
            {
                "pkg/__init__.py": "from . import other as users\n",
                "pkg/other/__init__.py": B["pkg/users/__init__.py"],
                "pkg/other/views.py": OTHER_VIEWS,
                **B,
            },
        ),
    ),
    "X3": ("flask", fx(P2_APP, {"pkg/users/__init__.py": "from .views import bp as views\n"})),
    "X4": ("flask", fx(P2_APP, {"pkg/users/__init__.py": "from .views import *\n"})),
    "X4b": (
        "flask",
        fx(
            P2_APP,
            {
                "pkg/users/__init__.py": "from . import views\nfrom pkg.ext import *\n",
                "pkg/ext/__init__.py": "",
            },
            drop=("pkg/ext.py",),
        ),
    ),
    "X5": ("flask", fx(X5_APP, {"pkg/ns/views.py": USERS_VIEWS})),
    "X7": (
        "flask",
        fx(
            P2_APP,
            {
                "pkg/users/__init__.py": (
                    'import os\nif os.environ.get("X"):\n    from . import views\n'
                )
            },
        ),
    ),
    "X7b": (
        "flask",
        fx(
            P2_APP,
            {
                "pkg/users/__init__.py": (
                    "try:\n    from . import views\nexcept ImportError:\n    views = None\n"
                )
            },
        ),
    ),
    "X8": ("flask", fx(P2_APP, {"pkg/users/__init__.py": "from . import views as v\n"})),
    "X9": ("flask", fx(X9_APP, {"pkg/mod.py": "from pkg.users import views as sub\n", **B})),
    "X11b": ("flask", fx(X11B_APP, A20_FILES)),
    "X12": ("flask", fx(X12_APP, B)),
    "X12b": ("flask", fx(X12B_APP, B)),
    "X13": (
        "flask",
        fx(
            P2_APP,
            {"pkg/__init__.py": "def __getattr__(name):\n    raise AttributeError(name)\n", **B},
        ),
    ),
    "X15": ("flask", fx(X15_APP, B)),
    "X16": (
        "fastapi",
        fastapi_files(
            X16_APP,
            {
                "pkg/subs/__init__.py": "from . import apps\n",
                "pkg/subs/apps.py": "from fastapi import FastAPI\nsub = FastAPI()\n",
            },
        ),
    ),
    "X17": (
        "flask",
        fx(
            P2_APP,
            {"pkg/__init__.py": "from pkg.ext import *\n", "pkg/ext/__init__.py": "", **B},
            drop=("pkg/ext.py",),
        ),
    ),
    "X18": ("flask", fx(X18_APP, B)),
    "X19": ("flask", fx(X19_APP, B)),
    # Amendment B: kill the mutants the frozen set did not.
    "X16b": (
        "fastapi",
        fastapi_files(
            X16B_APP,
            {
                "pkg/subs/__init__.py": "from . import apps\n",
                "pkg/subs/apps.py": "from fastapi import FastAPI\nsub = FastAPI()\n",
            },
        ),
    ),
    # pkg.users imports a *different* module's `views` (pkg.other.views): hop 2 resolves to itself.
    "X20": (
        "flask",
        fx(
            P2_APP,
            {
                **B,
                "pkg/users/__init__.py": "from pkg.other import views\n",
                "pkg/other/__init__.py": "",
                "pkg/other/views.py": OTHER_VIEWS,
            },
        ),
    ),
    # pkg/__init__ rebinds `users` to pkg.other.users: hop 1 resolves to itself.
    "X21": (
        "flask",
        fx(
            P2_APP,
            {
                **B,
                "pkg/__init__.py": "from .other import users\n",
                "pkg/other/__init__.py": "",
                "pkg/other/users/__init__.py": "from . import views\n",
                "pkg/other/users/views.py": OTHER_USERS_VIEWS,
            },
        ),
    ),
}


def fixture_repos() -> dict[str, dict[str, str]]:
    """Every fixture as an indexable repository, named as the probe named it (lower case)."""
    return {
        name.lower(): python_repo(name.lower(), dependency, files)
        for name, (dependency, files) in FIXTURES.items()
    }
