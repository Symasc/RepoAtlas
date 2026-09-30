"""Neutral fixtures for Python module values as declarations (V1-V16 of P5).

Nothing here is taken from a real repository or a benchmark task; every file states the
declaring statement, rebinding or boundary it exists to prove.

* ``decl`` is one src-layout project (distribution ``decl-dist``, package ``decl_pkg``):
  ``values.py`` declares a value in every form, next to names bound twice, under a guard,
  by unpacking and by an optional import; ``__init__`` re-exports ``X`` under its own
  name and another; ``rebound.py`` imports ``X`` and rebinds it; ``consumer.py`` reaches
  each of them every way a module attribute is reached; ``calls.py`` and ``caller.py``
  name a module value and a method alike, which the call graph must not notice.
* ``timeouts`` ships ``timeout_config`` (distribution ``timeout-config``); ``client``
  declares it and uses its ``DEFAULT_TIMEOUT``; ``stray`` imports it without declaring it.
"""

from __future__ import annotations

PKG = "src/decl_pkg"

DECL: dict[str, str] = {
    "pyproject.toml": (
        '[project]\nname = "decl-dist"\nversion = "1.0"\n'
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
    ),
    f"{PKG}/__init__.py": "from .values import X\nfrom .values import X as PublicX\n",
    f"{PKG}/values.py": (
        "import sys\n"  # 1
        "from typing import TYPE_CHECKING\n"  # 2
        "X = 1\n"  # 3 V1
        "ANNOTATED: int = 2\n"  # 4 V2
        "BARE: int\n"  # 5 V3
        "LOW, HIGH = 1, 2\n"  # 6 unpacking declares nothing
        "TWICE = 1\n"  # 7 V9
        "TWICE = 2\n"  # 8
        "LATE: int\n"  # 9 V3: an annotation, then
        "LATE = 3\n"  # 10 an assignment: two bindings
        "if TYPE_CHECKING:\n"  # 11
        "    CHECKED = int\n"  # 12 V10: one guarded binding
        "if sys.platform == 'win32':\n"  # 13
        "    SEP = ';'\n"  # 14 V10: one per branch
        "else:\n"  # 15
        "    SEP = ':'\n"  # 16
        "try:\n"  # 17
        "    from fastjson import loads\n"  # 18
        "except ImportError:\n"  # 19
        "    loads = None\n"  # 20 V10: the optional import, as before
        "if sys.version_info >= (3, 12):\n"  # 21
        "    GUARDED = 1\n"  # 22 V10: one guarded binding
        "obj = object()\n"  # 23 V13
        "attr = 3\n"  # 24 V13
        "def local_user():\n"  # 25
        "    return X, ANNOTATED, BARE, LOW, TWICE, LATE, CHECKED, SEP, loads, GUARDED\n"
        "ALIAS = X\n"  # 27 V5: a value of its own, which uses X
    ),
    # V8: imported, then rebound -- no longer the import, and no single declaration
    f"{PKG}/rebound.py": (
        "from .values import X\ndef wrap(value):\n    return value\nX = wrap(X)\n"
    ),
    f"{PKG}/consumer.py": (
        "import decl_pkg.values\n"  # 1
        "from typing import TYPE_CHECKING\n"  # 2
        "from decl_pkg import X, PublicX\n"  # 3
        "from decl_pkg import values as vals\n"  # 4
        "from decl_pkg.values import ANNOTATED, BARE, TWICE, LOW, SEP, obj, ALIAS\n"  # 5
        "from decl_pkg.rebound import X as ReboundX\n"  # 6
        "if TYPE_CHECKING:\n"  # 7
        "    from decl_pkg.values import CHECKED\n"  # 8
        "def v1():\n    return X\n"  # 9 V1 through the package
        "def v2():\n    return ANNOTATED + BARE\n"  # 11 V2, V3
        "def v4():\n    return decl_pkg.values.X\n"  # 13 V4: a module attribute
        "def v4_alias():\n    return vals.ANNOTATED\n"  # 15 V4: through a module alias
        "def v7():\n    return PublicX\n"  # 17 V7
        "def v8():\n    return ReboundX\n"  # 19 V8
        "def v9():\n    return TWICE, LOW, SEP\n"  # 21 V9, unpacking, V10
        "def v10(x: 'CHECKED') -> None:\n    return None\n"  # 23 V10
        "def v13():\n    return obj.attr\n"  # 25 V13: an object's attribute
        "def v5():\n    return ALIAS\n"  # 27 V5: the alias, not what it aliases
    ),
    # The call graph matches callees by name; a module value is none of them.
    f"{PKG}/calls.py": (
        "class C:\n"  # 1
        "    def get(self):\n"  # 2
        "        return 1\n"  # 3
        "handler = print\n"  # 4
        "get = C().get\n"  # 5
        "def run():\n"  # 6
        "    handler('x')\n"  # 7
        "    return get()\n"  # 8
    ),
    f"{PKG}/caller.py": (
        "from decl_pkg.calls import get, handler\ndef remote():\n    handler()\n    return get()\n"
    ),
}

TIMEOUTS: dict[str, str] = {
    "pyproject.toml": (
        '[project]\nname = "timeout-config"\nversion = "1.2.0"\n'
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
    ),
    "src/timeout_config/__init__.py": "",
    "src/timeout_config/config.py": "DEFAULT_TIMEOUT = 30\n",
}

CLIENT: dict[str, str] = {
    "pyproject.toml": '[project]\nname = "client-app"\ndependencies = ["timeout-config>=1.0"]\n',
    "src/client_app/__init__.py": "",
    "src/client_app/http.py": (
        "from timeout_config.config import DEFAULT_TIMEOUT\n"  # 1
        "def make_request():\n    return DEFAULT_TIMEOUT\n"  # 2
        "def fetch():\n    return make_request()\n"  # 4
        "SLOW = DEFAULT_TIMEOUT * 2\n"  # 6 V16: a value derived from the value
        "def slow_request():\n    return SLOW\n"  # 7
    ),
    "tests/test_http.py": (
        "from client_app.http import fetch, slow_request\n"
        "def test_fetch():\n    fetch()\n"
        "def test_slow():\n    slow_request()\n"
    ),
}

STRAY: dict[str, str] = {
    "pyproject.toml": '[project]\nname = "stray-app"\n',
    "src/stray_app/__init__.py": "",
    "src/stray_app/use.py": (
        "from timeout_config.config import DEFAULT_TIMEOUT\n"
        "def stray():\n    return DEFAULT_TIMEOUT\n"
    ),
}

WORKSPACE: dict[str, dict[str, str]] = {
    "decl": DECL,
    "timeouts": TIMEOUTS,
    "client": CLIENT,
    "stray": STRAY,
}
