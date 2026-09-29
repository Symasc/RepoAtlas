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
* ``both``: declares ``internal-fixture`` and ``internal-fixture-shadow``, each a project
  shipping ``internal_fixture``: two providers the evidence accepts, joined to neither;
* ``distpkg``: distribution ``fixture-dist`` shipping ``fixture_pkg``; ``rightname``
  declares ``fixture-dist`` and ``wrongname`` declares ``fixture-pkg`` (P13);
* ``unread``: declarations in forms this does not read -- Poetry, a PEP 508 direct
  reference, a git source.

``TWINS`` adds a second provider of ``internal-fixture`` beside ``provider`` and
``consumer`` (P16). ``UVWS`` is one repository holding a uv workspace (P12b by
workspace): a virtual root whose sources every member inherits, a member overriding
them, one the workspace excludes, and a second ``lib`` outside its members, which only
the workspace's own source tells apart.
"""

from __future__ import annotations

import json
from typing import Any

from repoatlas.services.workspace import Workspace
from tests.impact_corpus import label

MODELS = "provider:src/internal_fixture/models.py"
SERVICE = "src/consumer_app/service.py"

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


def _consumer(extra: str = "", *dependencies: str) -> dict[str, str]:
    declared = ", ".join(f'"{d}"' for d in dependencies or ("internal-fixture>=1.0",))
    return {
        "pyproject.toml": (
            f'[project]\nname = "consumer-app"\ndependencies = [{declared}]\n{extra}'
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
    "both": _consumer("", "internal-fixture>=1.0", "internal-fixture-shadow"),
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
    # outside the members: another `lib`, so the name alone names two
    "shared/lib/pyproject.toml": '[project]\nname = "lib"\n',
    "shared/lib/src/lib/__init__.py": "def helper():\n    return 2\n",
    # excluded from the workspace: `workspace = true` has no workspace to name
    "packages/legacy/pyproject.toml": (
        '[project]\nname = "legacy"\ndependencies = ["lib"]\n'
        "[tool.uv.sources]\nlib = { workspace = true }\n"
    ),
    "packages/legacy/src/legacy/__init__.py": (
        "from lib import helper\ndef old():\n    return helper()\n"
    ),
}


def edges(workspace: Workspace) -> list[dict[str, Any]]:
    """Every ``USES`` edge, each end labelled ``repository:path#name``."""
    rows = workspace.database.execute(
        """
        SELECT sr.slug AS s_repo, sf.rel_path AS s_path, s.qualified_name AS s_name,
               s.type AS s_type, dr.slug AS d_repo, df.rel_path AS d_path,
               d.qualified_name AS d_name, d.type AS d_type, e.resolution AS resolution
        FROM edges e
        JOIN entities s ON s.id = e.src_id JOIN entities d ON d.id = e.dst_id
        JOIN files sf ON sf.id = s.file_id JOIN files df ON df.id = d.file_id
        JOIN repositories sr ON sr.id = sf.repo_id JOIN repositories dr ON dr.id = df.repo_id
        WHERE e.type = 'USES'
        """
    ).fetchall()
    return [
        {
            "source": f"{row['s_repo']}:{label(row['s_path'], row['s_name'], row['s_type'])}",
            "target": f"{row['d_repo']}:{label(row['d_path'], row['d_name'], row['d_type'])}",
            **json.loads(row["resolution"] or "{}"),
        }
        for row in rows
    ]


def uses(workspace: Workspace, repository: str) -> set[tuple[str, str, str]]:
    return {
        (e["source"], e["target"], e["use"])
        for e in edges(workspace)
        if e["source"].startswith(f"{repository}:")
    }


def consumer_uses(repository: str) -> set[tuple[str, str, str]]:
    """What a consumer of ``internal-fixture`` uses of it, and its own test."""
    here = f"{repository}:{SERVICE}"
    return {
        (f"{here}#build", f"{MODELS}#Widget", "type"),
        (f"{here}#build", f"{MODELS}#make", "call"),
        (f"{here}#via_module", f"{MODELS}#make", "call"),
        (f"{repository}:tests/test_service.py#test_build", f"{here}#build", "call"),
    }
