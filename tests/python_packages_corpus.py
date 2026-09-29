"""Neutral fixtures for Python uses across projects (P12b-P17, P20 across projects).

Nothing here is taken from a real repository or a benchmark task; every repository states
the declaration, pin or layout it exists to prove.

``XREPO`` is one workspace of sibling repositories:

* ``provider``: distribution ``internal-fixture``, shipping ``internal_fixture`` from
  ``src/``; its ``__init__`` re-exports ``Widget`` under two names (P17);
* ``shadow``: distribution ``internal-fixture-shadow``, also shipping
  ``internal_fixture``, declared by no one -- a module name shared is no evidence;
* ``consumer``: declares ``internal-fixture>=1.0`` by version (P14), and a test calling
  its service (P18 across repositories);
* ``pinned``: the same, pinned by a uv ``path`` to ``../provider`` (P15);
* ``mispinned`` / ``unpinned``: a uv ``path`` to ``../shadow``, and to nowhere: refused,
  never answered by the name (P15);
* ``distpkg``: distribution ``fixture-dist`` shipping ``fixture_pkg``; ``rightname``
  declares ``fixture-dist`` and ``wrongname`` declares ``fixture-pkg`` (P13);
* ``unread``: declarations in forms this does not read -- Poetry, a PEP 508 direct
  reference, a git source.

``TWINS`` adds a second provider of ``internal-fixture`` beside ``provider`` and
``consumer`` (P16). ``UVWS`` is one repository holding a uv workspace (P12b by
workspace): a virtual root whose sources every member inherits, a member overriding
them, and one the workspace excludes.
"""

from __future__ import annotations

PROVIDER: dict[str, str] = {
    "pyproject.toml": (
        '[project]\nname = "internal-fixture"\nversion = "1.2.0"\n'
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n'
    ),
    "src/internal_fixture/__init__.py": (
        "from .models import Widget\nfrom .models import Widget as PublicWidget\n"
    ),
    "src/internal_fixture/models.py": (
        "class Widget:\n    pass\n"  # 1
        "def make() -> Widget:\n    return Widget()\n"  # 3
    ),
}

SHADOW: dict[str, str] = {
    "pyproject.toml": '[project]\nname = "internal-fixture-shadow"\nversion = "9.0"\n',
    "src/internal_fixture/__init__.py": "class Widget:\n    pass\n",
}


def _consumer(extra: str = "", dependency: str = "internal-fixture>=1.0") -> dict[str, str]:
    return {
        "pyproject.toml": (
            f'[project]\nname = "consumer-app"\ndependencies = ["{dependency}"]\n{extra}'
        ),
        "src/consumer_app/__init__.py": "",
        "src/consumer_app/service.py": (
            "from internal_fixture import Widget, PublicWidget\n"
            "from internal_fixture.models import make\n"
            "import internal_fixture.models as models\n"
            "def build(w: Widget, p: PublicWidget) -> Widget:\n"
            "    return make()\n"
            "def via_module():\n"
            "    return models.make()\n"
        ),
        "tests/test_service.py": (
            "from consumer_app.service import build\ndef test_build():\n    build(None, None)\n"
        ),
    }


DISTPKG: dict[str, str] = {
    "pyproject.toml": '[project]\nname = "fixture-dist"\n',
    "src/fixture_pkg/__init__.py": "class Thing:\n    pass\n",
}


def _importer(dependency: str) -> dict[str, str]:
    return {
        "pyproject.toml": f'[project]\nname = "importer"\ndependencies = ["{dependency}"]\n',
        "src/importer/__init__.py": "",
        "src/importer/use.py": "from fixture_pkg import Thing\ndef use(t: Thing):\n    return t\n",
    }


UNREAD: dict[str, str] = {
    "pyproject.toml": (
        '[project]\nname = "unread"\n'
        'dependencies = ["fixture-dist @ file:../distpkg", "internal-fixture"]\n'
        "[tool.uv.sources]\n"
        'internal-fixture = { git = "https://example.invalid/internal-fixture" }\n'
    ),
    "src/unread/__init__.py": "",
    "src/unread/use.py": (
        "from fixture_pkg import Thing\nfrom internal_fixture import Widget\n"
        "def use(t: Thing, w: Widget):\n    return t\n"
    ),
}

POETRY: dict[str, str] = {
    "pyproject.toml": (
        '[tool.poetry]\nname = "poetic"\n'
        '[tool.poetry.dependencies]\npython = "^3.12"\ninternal-fixture = "^1.0"\n'
        '[build-system]\nrequires = ["poetry-core"]\nbuild-backend = "poetry.core.masonry.api"\n'
    ),
    "poetic/__init__.py": "",
    "poetic/use.py": "from internal_fixture import Widget\ndef use(w: Widget):\n    return w\n",
}

XREPO: dict[str, dict[str, str]] = {
    "provider": PROVIDER,
    "shadow": SHADOW,
    "consumer": _consumer(),
    "pinned": _consumer('[tool.uv.sources]\ninternal-fixture = { path = "../provider" }\n'),
    "mispinned": _consumer('[tool.uv.sources]\ninternal-fixture = { path = "../shadow" }\n'),
    "unpinned": _consumer('[tool.uv.sources]\ninternal-fixture = { path = "../nowhere" }\n'),
    "distpkg": DISTPKG,
    "rightname": _importer("fixture-dist"),
    "wrongname": _importer("fixture-pkg"),
    "unread": UNREAD,
    "poetic": POETRY,
}

TWINS: dict[str, dict[str, str]] = {
    "provider": PROVIDER,
    "twin": {**PROVIDER, "pyproject.toml": '[project]\nname = "Internal_Fixture"\n'},
    "consumer": _consumer(),
}

UVWS: dict[str, str] = {
    # a virtual root: no project of its own, sources every member inherits
    "pyproject.toml": (
        '[tool.uv.workspace]\nmembers = ["packages/*"]\nexclude = ["packages/legacy"]\n'
        "[tool.uv.sources]\nlib = { workspace = true }\n"
    ),
    "packages/lib/pyproject.toml": '[project]\nname = "lib"\n',
    "packages/lib/src/lib/__init__.py": "def helper():\n    return 1\n",
    "packages/app/pyproject.toml": '[project]\nname = "app"\ndependencies = ["lib"]\n',
    "packages/app/src/app/__init__.py": "from lib import helper\ndef run():\n    return helper()\n",
    # overrides the inherited source with its own path
    "packages/tool/pyproject.toml": (
        '[project]\nname = "tool"\ndependencies = ["lib"]\n'
        '[tool.uv.sources]\nlib = { path = "../lib" }\n'
    ),
    "packages/tool/src/tool/__init__.py": (
        "from lib import helper\ndef go():\n    return helper()\n"
    ),
    # excluded from the workspace: `workspace = true` has no workspace to name
    "packages/legacy/pyproject.toml": (
        '[project]\nname = "legacy"\ndependencies = ["lib"]\n'
        "[tool.uv.sources]\nlib = { workspace = true }\n"
    ),
    "packages/legacy/src/legacy/__init__.py": (
        "from lib import helper\ndef old():\n    return helper()\n"
    ),
}
