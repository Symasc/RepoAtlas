"""Neutral fixtures for Python providers setuptools metadata names (P3b).

Nothing here is taken from a real repository; the shapes are the ones real setuptools-era
providers write (a ``src/`` layout by ``find:``, ``py_modules``, a build-system-only
``pyproject.toml`` beside a ``setup.cfg``, a ``setup()`` shim). Every consumer is a
repository with no packaging metadata, declaring its dependencies in its root
``requirements.txt``, as P3a reads it; a test builds only the repositories it names
(:func:`~tests.python_legacy_corpus.pick`).

``fixture_auth`` is shipped under many metadata shapes, each a repository of its own;
``models.User`` is on line 1 and ``models.make`` on line 5 of every copy.
"""

from __future__ import annotations

from tests.python_legacy_corpus import AUTH_MODELS, THING, consumer, importing

INIT = "from .models import User, make\n"
FLAT = {"fixture_auth/__init__.py": INIT, "fixture_auth/models.py": AUTH_MODELS}
SRC = {"src/fixture_auth/__init__.py": INIT, "src/fixture_auth/models.py": AUTH_MODELS}
BUILD_SYSTEM = (
    '[build-system]\nrequires = ["setuptools>=61"]\nbuild-backend = "setuptools.build_meta"\n'
)
CFG_SRC = (
    "[metadata]\nname = fixture-auth\n\n[options]\npackage_dir =\n    = src\n"
    "packages = find:\n\n[options.packages.find]\nwhere = src\n"
)
MAILER = "class Mailer:\n    pass\n"
PY_MODULE = (
    'from setuptools import setup\n\nsetup(name="fixture-mail", py_modules=["fixture_mail"])\n'
)


def loading(module: str, names: str, returned: str) -> str:
    """A service whose ``load`` uses ``names`` from ``module``."""
    return f"from {module} import {names}\n\n\ndef load() -> {returned}:\n    return {returned}()\n"


def mail_consumer(requirement: str) -> dict[str, str]:
    """A metadata-less consumer of ``fixture_mail.Mailer``."""
    return importing(requirement, "fixture_mail") | {
        "service.py": loading("fixture_mail", "Mailer", "Mailer")
    }


SETUPTOOLS: dict[str, dict[str, str]] = {
    "consumer": consumer("fixture-auth\n"),
    "other-consumer": consumer("fixture-other\n"),
    # S1: setup.cfg names it and finds it flat; the name is on line 3, a comment spelling
    # "name = fixture-auth" on line 1 (SM11)
    "cfg-flat": {
        "setup.cfg": (
            "# name = fixture-auth, as it was once published\n[metadata]\n"
            "name = fixture-auth\n\n[options]\npackages = find:\n"
        ),
        **FLAT,
    },
    # S1b: a name and nothing stating what it ships -- a setuptools-only project
    "cfg-name-only": {"setup.cfg": "[metadata]\nname = fixture-auth\n", **FLAT},
    # S1c: the same beside a build-system-only pyproject, from src/
    "bs-name-only": {
        "pyproject.toml": BUILD_SYSTEM,
        "setup.cfg": "[metadata]\nname = fixture-auth\n",
        **SRC,
    },
    "copy-auth": FLAT,  # a metadata-less copy
    # S2: src/ by package_dir, find: and where
    "cfg-src": {"setup.cfg": CFG_SRC, **SRC},
    # S3: a distribution shipping a module of another name
    "cfg-distname": {
        "setup.cfg": "[metadata]\nname = fixture-distribution\n[options]\npackages = find:\n",
        "special_import/__init__.py": INIT,
        "special_import/models.py": AUTH_MODELS,
    },
    "special-consumer": consumer(
        "fixture-distribution\n",
        {
            "service.py": (
                "from special_import.models import User, make\n\n\n"
                "def load() -> User:\n    return make()\n"
            )
        },
    ),
    # S4: setup.py alone, a literal call over several lines: the name is on line 5 (SM11)
    "py-flat": {
        "setup.py": (
            'from setuptools import setup\n\nsetup(\n    version="1.0",\n'
            '    name="fixture-auth",\n    packages=["fixture_auth"],\n)\n'
        ),
        **FLAT,
    },
    # S5: setup.py, src/ by package_dir and find_packages(where=)
    "py-src": {
        "setup.py": (
            "from setuptools import find_packages, setup\n\n"
            'setup(name="fixture-auth", package_dir={"": "src"}, '
            'packages=find_packages(where="src"))\n'
        ),
        **SRC,
    },
    # S6: setup.py py_modules; its variant also holds a package of the same name (SM12)
    "py-module": {
        "setup.py": PY_MODULE,
        "fixture_mail.py": MAILER,
    },
    "py-module-dup": {
        "setup.py": PY_MODULE,
        "fixture_mail.py": MAILER,
        "fixture_mail/__init__.py": MAILER,
    },
    "mail-consumer": mail_consumer("fixture-mail\n"),
    # S7: a setup() shim beside the setup.cfg that states the project
    "shim-cfg": {
        "setup.py": "import setuptools\n\nsetuptools.setup()\n",
        "setup.cfg": CFG_SRC,
        **SRC,
    },
    # S8: a build-system-only pyproject beside a setup.cfg: one project
    "bs-cfg": {
        "pyproject.toml": BUILD_SYSTEM,
        "setup.cfg": CFG_SRC,
        "setup.py": "from setuptools import setup\nsetup()\n",
        **SRC,
    },
    # S9: names that disagree -- [project] against setup.cfg, and setup.cfg against setup.py
    "conflict-pep621": {
        "pyproject.toml": '[project]\nname = "fixture-auth"\n' + BUILD_SYSTEM,
        "setup.cfg": "[metadata]\nname = fixture-other\n[options]\npackages = find:\n",
        **FLAT,
    },
    "conflict-cfgpy": {
        "setup.cfg": "[metadata]\nname = fixture-auth\n[options]\npackages = find:\n",
        "setup.py": 'from setuptools import setup\n\nsetup(name="fixture-other")\n',
        **FLAT,
    },
    # S10: layouts that disagree, each with a copy to find
    "conflict-layout": {
        "setup.cfg": CFG_SRC,
        "setup.py": 'from setuptools import setup\n\nsetup(packages=["fixture_auth"])\n',
        **SRC,
        **FLAT,
    },
    # S11: a name setup() computes overrides the static one; a name bound elsewhere
    "dyn-name": {
        "setup.cfg": "[metadata]\nname = fixture-auth\n[options]\npackages = find:\n",
        "setup.py": "from setuptools import setup\n\nsetup(name=compute())\n",
        **FLAT,
    },
    "const-name": {
        "setup.py": (
            'from setuptools import setup\n\nNAME = "fixture-auth"\n'
            'setup(name=NAME, packages=["fixture_auth"])\n'
        ),
        **FLAT,
    },
    # S11/SM1b: a name bound elsewhere, beside a setup.cfg stating the same one: setup()
    # would pass it, and it is still never evaluated
    "const-name-cfg": {
        "setup.cfg": "[metadata]\nname = fixture-auth\n[options]\npackages = find:\n",
        "setup.py": 'from setuptools import setup\n\nNAME = "fixture-auth"\nsetup(name=NAME)\n',
        **FLAT,
    },
    # S12: a layout setup() computes: never discovered instead
    "dyn-layout": {
        "setup.py": (
            'from setuptools import find_packages, setup\n\nEXCLUDED = ["tests"]\n'
            'setup(name="fixture-auth", packages=find_packages(exclude=EXCLUDED))\n'
        ),
        **FLAT,
    },
    # S14: the distribution the requirement names ships another package
    "cfg-other": {
        "setup.cfg": "[metadata]\nname = fixture-auth\n[options]\npackages = find:\n",
        "other_pkg/__init__.py": THING,
    },
    # S15: a name that shares nothing with the module
    "zebra": {
        "setup.cfg": "[metadata]\nname = Zebra-Tools\n[options]\npackages = find:\n",
        "quokka/__init__.py": INIT,
        "quokka/models.py": AUTH_MODELS,
    },
    "zoo": consumer(
        "Zebra-Tools\n",
        {
            "service.py": (
                "from quokka.models import User, make\n\n\ndef load() -> User:\n    return make()\n"
            )
        },
    ),
    # S22/SM4: named fixture-auth, fixture_auth/ on disk but excluded; it ships special_import
    "spelled": {
        "setup.cfg": (
            "[metadata]\nname = fixture-auth\n[options]\npackages = find:\n"
            "[options.packages.find]\nexclude = fixture_auth\n"
        ),
        "special_import/__init__.py": THING,
        **FLAT,
    },
    # S16: the root's find: at . never ships a package a nested pyproject project owns
    "outer": {
        "setup.cfg": "[metadata]\nname = outer-kit\n[options]\npackages = find:\n",
        "outer_pkg/__init__.py": THING,
        "inner/pyproject.toml": '[project]\nname = "inner-kit"\n',
        "inner/__init__.py": THING,
    },
    "outer-consumer": importing("outer-kit\n", "inner"),
    # SM5 / R1: a setup.cfg in a nested directory is no project of its own
    "nested-app": {
        "examples/app/setup.cfg": "[metadata]\nname = fixture-auth\n[options]\npackages = find:\n",
        "examples/app/fixture_auth/__init__.py": INIT,
        "examples/app/fixture_auth/models.py": AUTH_MODELS,
        "tool.py": "print('tool')\n",
    },
    # S17: find_namespace: with a namespace top-level
    "ns-cfg": {
        "setup.cfg": (
            "[metadata]\nname = fixture-ns\n[options]\npackage_dir = = src\n"
            "packages = find_namespace:\n[options.packages.find]\nwhere = src\n"
        ),
        "src/ns_kit/part.py": THING,
    },
    "ns-consumer": importing("fixture-ns\n", "ns_kit.part"),
    # S21: find_namespace: with a regular package, and a data directory in it
    "ns-regular": {
        "setup.cfg": CFG_SRC.replace("find:", "find_namespace:"),
        **SRC,
        "src/fixture_auth/templates/base.txt": "{{ x }}\n",
    },
    # S23: py_modules under package_dir = src, from setup.cfg
    "cfg-module-src": {
        "setup.cfg": (
            "[metadata]\nname = fixture-mail\n[options]\npackage_dir =\n    = src\n"
            "py_modules =\n    fixture_mail\n"
        ),
        "src/fixture_mail.py": MAILER,
    },
    # S24: a setup() inside a function beside a PEP 621 project: as P2 reads it
    "pep621-shim": {
        "pyproject.toml": '[project]\nname = "fixture-auth"\n' + BUILD_SYSTEM,
        "setup.py": (
            "from setuptools import setup\n\n\ndef run(binary):\n"
            "    setup(cmdclass={}, ext_modules=[])\n\n\nrun(True)\n"
        ),
        **SRC,
    },
    # S25: a setup.cfg of tool settings only
    "tool-cfg": {
        "pyproject.toml": '[project]\nname = "fixture-auth"\n' + BUILD_SYSTEM,
        "setup.cfg": "[flake8]\nmax-line-length = 100\n",
        **SRC,
    },
    # S26: where is not the package_dir root
    "where-elsewhere": {
        "setup.cfg": (
            "[metadata]\nname = fixture-auth\n[options]\npackage_dir = = src\npackages = find:\n"
        ),
        **SRC,
    },
    # another backend builds it: its setup.cfg is not its metadata
    "flit-cfg": {
        "pyproject.toml": (
            '[project]\nname = "fixture-auth"\n[build-system]\nrequires = ["flit_core"]\n'
            'build-backend = "flit_core.buildapi"\n[tool.flit.module]\nname = "fixture_auth"\n'
        ),
        "setup.cfg": "[metadata]\nname = fixture-other\n[options]\npackages = find:\n",
        **FLAT,
    },
}
