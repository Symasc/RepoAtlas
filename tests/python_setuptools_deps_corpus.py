"""Neutral fixtures for setuptools consumers declaring their dependencies (P3c).

Nothing here is taken from a real repository. The shapes are the ones real setuptools-era
consumers write: a ``setup.cfg`` ``install_requires`` list, a ``setup()`` literal list, a
build-system-only ``pyproject.toml`` beside them, ``extras_require``. Every consumer is a
project of its own whose ``service.py`` uses ``fixture_auth.models``; the provider is an
ordinary PEP 621 project, ``fixture-auth``, shipping ``fixture_auth`` from ``src/``
(:data:`~tests.python_legacy_corpus.HYBRID`'s ``explicit-auth``).
"""

from __future__ import annotations

from tests.python_legacy_corpus import AUTH_MODELS, HYBRID, SERVICE, SERVICE_SOURCE, TEST_SOURCE

PROVIDER = "explicit-auth"
MODELS = "src/fixture_auth/models.py"
BUILD_SYSTEM = (
    '[build-system]\nrequires = ["setuptools>=61"]\nbuild-backend = "setuptools.build_meta"\n'
)
SETUP = "from setuptools import setup\n\n"


def cfg(install_requires: str, before: str = "", after: str = "") -> str:
    """A setup.cfg naming ``fixture-client``, its ``[options]`` stating ``install_requires``."""
    return (
        f"[metadata]\nname = fixture-client\n{before}\n[options]\npy_modules = service\n"
        f"install_requires ={install_requires}\n{after}"
    )


def project(files: dict[str, str]) -> dict[str, str]:
    """A consumer project: its metadata, and a service using fixture_auth with its test."""
    return {SERVICE: SERVICE_SOURCE, "tests/test_service.py": TEST_SOURCE, **files}


#: D1's setup.cfg: the requirement on line 7.
CFG_D1 = cfg("\n    fixture-auth>=1")

DEPS: dict[str, dict[str, str]] = {
    PROVIDER: HYBRID[PROVIDER],
    # D13: a second provider of the same distribution
    "twin-auth": {
        "pyproject.toml": '[project]\nname = "Fixture_Auth"\n',
        "src/fixture_auth/__init__.py": "from .models import User, make\n",
        "src/fixture_auth/models.py": AUTH_MODELS,
    },
    # CM8: the distribution fixture-auth ships special_import, never "fixture_auth"
    "dist-other": {
        "pyproject.toml": '[project]\nname = "fixture-auth"\n',
        "src/special_import/__init__.py": "from .models import User, make\n",
        "src/special_import/models.py": AUTH_MODELS,
    },
    "copy-auth": {
        "fixture_auth/__init__.py": "from .models import User, make\n",
        "fixture_auth/models.py": AUTH_MODELS,
    },
    "cfg-consumer": project({"setup.cfg": CFG_D1}),  # D1
    # CM8: declares fixture-auth, imports the special_import that distribution ships
    "cfg-special": {
        **project({"setup.cfg": CFG_D1}),
        SERVICE: SERVICE_SOURCE.replace("fixture_auth", "special_import"),
    },
    # D2: description, keywords and a comment mention it first; the entry is on line 10
    "cfg-mentions": project(
        {
            "setup.cfg": cfg(
                "\n    fixture-auth>=1",
                before="description = fixture-auth integration\nkeywords = fixture-auth\n"
                "# fixture-auth>=1 was pinned here\n",
            )
        }
    ),
    # D3: fixture-auth-extra first; fixture-auth on line 8
    "cfg-overlap": project({"setup.cfg": cfg("\n    fixture-auth-extra\n    fixture-auth")}),
    # D4: a literal list; the requirement's Constant on line 6
    "py-consumer": project(
        {
            "setup.py": SETUP
            + 'setup(\n    name="fixture-client",\n    install_requires=[\n'
            + '        "fixture-auth>=1",\n    ],\n    py_modules=["service"],\n)\n'
        }
    ),
    # D5: computed in setup.py, which overrides a setup.cfg stating it: nothing is read
    "py-dynamic": project(
        {
            "setup.py": SETUP + 'REQS = ["fixture-auth"]\nsetup(install_requires=REQS)\n',
            "setup.cfg": CFG_D1,
        }
    ),
    # D6: [project] states its dependencies; the setup.cfg beside it is not read
    "pep621-and-cfg": project(
        {
            "pyproject.toml": BUILD_SYSTEM
            + '[project]\nname = "fixture-client"\ndependencies = ["fixture-auth"]\n',
            "setup.cfg": CFG_D1,
        }
    ),
    # D6: ... even stating none
    "pep621-none-cfg": project(
        {
            "pyproject.toml": BUILD_SYSTEM + '[project]\nname = "fixture-client"\n',
            "setup.cfg": CFG_D1,
        }
    ),
    # D6b: dynamic dependencies: the setup.cfg states them
    "pep621-dynamic-cfg": project(
        {
            "pyproject.toml": BUILD_SYSTEM
            + '[project]\nname = "fixture-client"\ndynamic = ["dependencies"]\n',
            "setup.cfg": CFG_D1,
        }
    ),
    # a build-system-only pyproject completed by its setup.cfg (the corpus shape)
    "bs-cfg": project({"pyproject.toml": BUILD_SYSTEM, "setup.cfg": CFG_D1}),
    # D7: setup.py's install_requires is the field: setup.cfg's is overridden whole
    "py-and-cfg": project(
        {"setup.py": SETUP + 'setup(install_requires=["fixture-auth"])\n', "setup.cfg": CFG_D1}
    ),
    "py-other-cfg": project(
        {"setup.py": SETUP + 'setup(install_requires=["fixture-other"])\n', "setup.cfg": CFG_D1}
    ),
    # D7 / CM10: a setup.py shim states nothing; setup.cfg does
    "shim-cfg": project({"setup.py": SETUP + "setup()\n", "setup.cfg": CFG_D1}),
    # D8: the winning source also states a direct reference to it
    "cfg-direct": project(
        {
            "setup.cfg": cfg(
                "\n    fixture-auth>=1\n    fixture-auth @ https://example.invalid/a.whl"
            )
        }
    ),
    # D9: extras only: setup.py states the field (empty) and an extra; setup.cfg an extra (CM5)
    "extras-only": project(
        {
            "setup.py": SETUP
            + 'setup(install_requires=[], extras_require={"auth": ["fixture-auth"]})\n',
            "setup.cfg": "[metadata]\nname = fixture-client\n\n[options]\npy_modules = service\n"
            "\n[options.extras_require]\nauth =\n    fixture-auth\n",
        }
    ),
    # D5 / CM4: one element computed leaves the whole list unread, the literal one too
    "py-partial": project(
        {
            "setup.py": SETUP
            + 'V = "1"\nsetup(install_requires=["fixture-auth", f"fixture-db>={V}"])\n'
        }
    ),
    # D10: install_requires and an extra both state it: the requirement on line 7
    "extras-too": project(
        {
            "setup.cfg": cfg(
                "\n    fixture-auth", after="\n[options.extras_require]\nauth = fixture-auth\n"
            )
        }
    ),
    # D11: a marker, as a pyproject's would be read; one line split on ";" is not read
    "cfg-marker": project({"setup.cfg": cfg("\n    fixture-auth ; python_version < '3.12'")}),
    "cfg-marker-one-line": project({"setup.cfg": cfg(" fixture-auth; python_version < '3.12'")}),
    # D12: a nested setup.cfg-only directory is no project: its requirement is not the
    # enclosing project's
    "nested-cfg": {
        "setup.cfg": "[metadata]\nname = outer\n",
        "app/setup.cfg": CFG_D1,
        f"app/{SERVICE}": SERVICE_SOURCE,
    },
    # CM7: two projects in one repository; only the root one declares fixture-auth
    "two-projects": {
        "setup.cfg": CFG_D1,
        SERVICE: SERVICE_SOURCE,
        "lib/pyproject.toml": '[project]\nname = "lib"\n',
        "lib/src/lib/__init__.py": "",
        f"lib/src/lib/{SERVICE}": SERVICE_SOURCE,
    },
}
