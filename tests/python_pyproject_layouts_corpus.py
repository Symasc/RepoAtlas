"""Neutral fixtures for the layouts a ``pyproject.toml`` states for itself (P6 C1).

Nothing here is taken from a real repository. The two shapes read are the ones real
providers write: setuptools' ``[tool.setuptools.packages.find]`` (psf/httpbin's form) and
a hatchling wheel's ``only-include`` (requests-cache's). Every other shape here is one
that must stay refused. Each provider names itself ``fixture-auth`` and holds
``fixture_auth`` (``models.User`` on line 1, ``models.make`` on line 5); every consumer
is a repository with no packaging metadata requiring it in its root ``requirements.txt``
(:func:`~tests.python_legacy_corpus.consumer`). A test builds only the repositories it
names (:func:`~tests.python_legacy_corpus.pick`).
"""

from __future__ import annotations

from tests.python_legacy_corpus import consumer
from tests.python_setuptools_corpus import FLAT, SRC

#: ``[project] name`` on line 2 of every provider's pyproject.
PROJECT = '[project]\nname = "fixture-auth"\nversion = "1.0"\n\n'
SETUPTOOLS = (
    '[build-system]\nrequires = ["setuptools>=61"]\nbuild-backend = "setuptools.build_meta"\n\n'
)
HATCHLING = '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n\n'
FIND_FLAT = '[tool.setuptools.packages.find]\nwhere = ["."]\ninclude = ["fixture_auth*"]\n'
FIND_SRC = (
    '[tool.setuptools]\npackage-dir = {"" = "src"}\n\n'
    '[tool.setuptools.packages.find]\nwhere = ["src"]\n'
)
ONLY = '[tool.hatch.build]\nonly-include = ["fixture_auth"]\n'
#: A project's own tests, beside the package it ships: never shipped by ``only-include``.
LOCAL_TESTS = {
    "tests/__init__.py": "",
    "tests/helpers.py": "def helper() -> None:\n    pass\n",
    "tests/test_models.py": (
        "from tests.helpers import helper\nfrom fixture_auth.models import make\n\n\n"
        "def test_make() -> None:\n    helper()\n    make()\n"
    ),
}
#: A module of the provider's own using its package by its absolute name.
APP = {
    "app.py": (
        "from fixture_auth.models import User, make\n\n\ndef build() -> User:\n    return make()\n"
    )
}
CFG_SRC = (
    "[metadata]\nname = fixture-auth\n\n[options]\npackage_dir =\n    = src\n"
    "packages = find:\n\n[options.packages.find]\nwhere = src\n"
)

PYPROJECT_LAYOUTS: dict[str, dict[str, str]] = {
    "consumer": consumer("fixture-auth\n"),
    "undeclared": consumer("requests\n"),  # P1: the provider exists, no declaration
    # setuptools' find in the pyproject, flat, as psf/httpbin states it
    "find-flat": {"pyproject.toml": PROJECT + SETUPTOOLS + FIND_FLAT, **FLAT, **APP},
    # from src/, by package-dir and where
    "find-src": {"pyproject.toml": PROJECT + SETUPTOOLS + FIND_SRC, **SRC},
    # from src/, by where alone: setuptools fills package_dir from a single where
    "find-where": {
        "pyproject.toml": PROJECT
        + SETUPTOOLS
        + '[tool.setuptools.packages.find]\nwhere = ["src"]\n',
        **SRC,
    },
    # P2: declared, and the find excludes the module
    "find-excluded": {
        "pyproject.toml": PROJECT + SETUPTOOLS + FIND_FLAT.replace("fixture_auth*", "other*"),
        **FLAT,
    },
    # P6: two where are not read, and src/ is never discovered instead
    "find-two-where": {
        "pyproject.toml": (
            PROJECT + SETUPTOOLS + '[tool.setuptools.packages.find]\nwhere = ["src", "lib"]\n'
        ),
        **SRC,
        "src/app.py": APP["app.py"],
    },
    # P5: the pyproject finds flat, its setup.cfg finds in src/: one layout, stated twice
    "find-conflict": {
        "pyproject.toml": PROJECT + SETUPTOOLS + FIND_FLAT,
        "setup.cfg": CFG_SRC,
        **FLAT,
        **SRC,
        **APP,
    },
    # ... and the same layout stated by both: read, from the pyproject
    "find-agree": {
        "pyproject.toml": SETUPTOOLS + FIND_SRC + "namespaces = false\n",
        "setup.cfg": CFG_SRC,
        **SRC,
    },
    # the pyproject's find defaults to namespaces, as find_namespace: -- a setup.cfg find:
    # is another layout
    "find-namespaces-differ": {
        "pyproject.toml": SETUPTOOLS + FIND_SRC,
        "setup.cfg": CFG_SRC,
        **SRC,
    },
    # another backend builds it: [tool.setuptools] is not its layout
    "find-hatchling": {"pyproject.toml": PROJECT + HATCHLING + FIND_FLAT, **FLAT},
    # hatchling's only-include, as requests-cache states it, beside local tests
    "only-include": {
        "pyproject.toml": (
            PROJECT
            + HATCHLING
            + ONLY
            + 'force-include = {"README.md" = "fixture_auth/README.md"}\n'
        ),
        "README.md": "fixture\n",
        **FLAT,
        **LOCAL_TESTS,
    },
    # the wheel target's own only-include overrides the build's
    "only-include-wheel": {
        "pyproject.toml": (
            PROJECT + HATCHLING + '[tool.hatch.build]\nonly-include = ["other"]\n\n'
            '[tool.hatch.build.targets.wheel]\nonly-include = ["fixture_auth"]\n'
        ),
        **FLAT,
    },
    # a nested path ships at that path: not read
    "only-include-nested": {
        "pyproject.toml": PROJECT
        + HATCHLING
        + ONLY.replace('"fixture_auth"', '"src/fixture_auth"'),
        **SRC,
    },
    # a force-include that may put a module in the wheel: not read
    "only-include-forced": {
        "pyproject.toml": (
            PROJECT + HATCHLING + ONLY + 'force-include = {"extra.py" = "fixture_auth/extra.py"}\n'
        ),
        "extra.py": "",
        **FLAT,
    },
    # an exclude that may take part of it out: not read
    "only-include-exclude": {
        "pyproject.toml": PROJECT + HATCHLING + ONLY + 'exclude = ["fixture_auth/models.py"]\n',
        **FLAT,
    },
    # P4's second provider of the same distribution and module
    "only-include-twin": {"pyproject.toml": PROJECT + HATCHLING + ONLY, **FLAT},
}
