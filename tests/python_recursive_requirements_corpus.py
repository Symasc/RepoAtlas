"""Neutral fixtures for Python uses declared through a root ``requirements.txt``'s ``-r`` (P3d).

Nothing here is taken from a real repository. Every consumer is a repository stating nothing
about packaging (:func:`tests.python_legacy_corpus.consumer`): its ``service.py`` imports
``fixture_auth.models``, and its root ``requirements.txt`` reaches the requirement, or does
not, through local includes -- whatever the included files are called. The providers are
P3a's: ``explicit-auth``, a project named ``fixture-auth`` shipping ``fixture_auth`` from
``src/``, and ``auth-provider``, a repository holding it with no metadata at all; ``twin-auth``
is a second project of that name. ``fixture-other`` names nothing indexed: a decoy.
"""

from __future__ import annotations

from tests.python_legacy_corpus import AUTH_MODELS, AUTH_PACKAGE, HYBRID, consumer

AUTH = "fixture-auth\n"
OTHER = "fixture-other\n"

INCLUDES: dict[str, dict[str, str]] = {
    "explicit-auth": HYBRID["explicit-auth"],
    "auth-provider": AUTH_PACKAGE,
    "twin-auth": {
        "pyproject.toml": '[project]\nname = "Fixture_Auth"\n',
        "src/fixture_auth/__init__.py": "from .models import User, make\n",
        "src/fixture_auth/models.py": AUTH_MODELS,
    },
    # R1: one include
    "one-include": consumer("-r requirements/base.txt\n", {"requirements/base.txt": AUTH}),
    # R2: root -> base -> common, the leaf on line 2
    "nested": consumer(
        "-r requirements/base.txt\n",
        {
            "requirements/base.txt": "-r common.txt\n",
            "requirements/common.txt": "requests\nfixture-auth>=1\n",
        },
    ),
    # R3: -r base.txt in requirements/prod.txt is requirements/base.txt, never the root's
    "relative": consumer(
        "-r requirements/prod.txt\n",
        {
            "requirements/prod.txt": "-r base.txt\n",
            "requirements/base.txt": AUTH,
            "base.txt": OTHER,
        },
    ),
    # R4: -r ../common.txt in requirements/prod.txt is the root's common.txt
    "parent": consumer(
        "-r requirements/prod.txt\n",
        {
            "requirements/prod.txt": "-r ../common.txt\n",
            "common.txt": AUTH,
            "requirements/common.txt": OTHER,
        },
    ),
    # R5: the include on line 1, the requirement on line 8 of the file it reaches
    "leaf-line": consumer(
        "-r deps/extra.txt\n# nothing else\n",
        {"deps/extra.txt": "# deps\n\nrequests\n\n# auth\nflask\nsix\nfixture-auth==2.0\n"},
    ),
    # R6: root -> a -> b -> a
    "cycle": consumer(
        "-r a.txt\n", {"a.txt": "-r b.txt\nrequests\n", "b.txt": "-r a.txt\nfixture-auth\n"}
    ),
    # R7: an include to no file, beside a requirement the root states itself
    "missing-and-root": consumer("-r requirements/missing.txt\nfixture-auth\n"),
    # ... and with nothing else: a file of that name elsewhere is not it
    "missing-only": consumer(
        "-r requirements/missing.txt\nrequests\n", {"other/missing.txt": AUTH}
    ),
    # R8: above the repository, into another one; outside.txt here is not ../../outside.txt
    "escape": consumer(
        "-r ../../outside.txt\n-r ../sibling/requirements.txt\nrequests\n",
        {"outside.txt": AUTH},
    ),
    "sibling": {"requirements.txt": AUTH},
    # R9: a URL and a variable
    "remote": consumer(
        "-r https://example.invalid/requirements.txt\n-r ${REQS}/base.txt\nrequests\n",
        {"base.txt": AUTH},
    ),
    # R10: one file included twice
    "twice": consumer(
        "-r requirements/base.txt\nrequests\n-r requirements/base.txt\n",
        {"requirements/base.txt": AUTH},
    ),
    # R11: the first requirement pip reads names it, and every one is read for an @
    "include-first": consumer(
        "-r requirements/base.txt\nfixture-auth>=1\n",
        {"requirements/base.txt": "fixture-auth>=2\n"},
    ),
    "direct-first": consumer(
        "fixture-auth>=1\n-r requirements/base.txt\n",
        {"requirements/base.txt": "fixture-auth>=2\n"},
    ),
    "direct-reference": consumer(
        "fixture-auth>=1\n-r requirements/base.txt\n",
        {"requirements/base.txt": "fixture-auth @ https://example.invalid/fixture_auth-1.0.whl\n"},
    ),
    # R12: the legacy convention, required through an include
    "legacy-included": consumer(
        "-r requirements/base.txt\n", {"requirements/base.txt": "requests\nfixture-auth\n"}
    ),
    # R18: the root points at a.txt, stating another name; b.txt, beside it, states the one
    "retarget": consumer(
        "-r requirements/a.txt\n", {"requirements/a.txt": OTHER, "requirements/b.txt": AUTH}
    ),
    # +U: files the root does not reach declare nothing, whatever they are called
    "unreached": consumer(
        "requests\n",
        {"requirements/dev.txt": AUTH, "LICENSE.txt": AUTH, "requirements-dev.txt": AUTH},
    ),
}
