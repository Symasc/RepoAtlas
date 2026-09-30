"""Neutral fixtures for namespace portions (PEP 420) inside a Python project (P6 C2).

Nothing here is taken from a real repository. The two shapes are the ones real projects
have: a portion below a regular package (pallets/flask's ``sansio/``), and one at the top
of a repository with no packaging metadata (a Home Assistant integration's
``custom_components/``, which other such repositories hold too). ``portioned`` names
itself ``fixture-auth`` and keeps ``models`` (``User`` on line 1, ``make`` on line 5) in
its portion; every consumer is a repository with no packaging metadata requiring what it
uses in its root ``requirements.txt`` (:func:`~tests.python_legacy_corpus.consumer`). A
test builds only the repositories it names (:func:`~tests.python_legacy_corpus.pick`).
"""

from __future__ import annotations

from tests.python_legacy_corpus import AUTH_MODELS, SERVICE, SERVICE_SOURCE, consumer

PORTION_MODELS = "src/fixture_auth/sansio/models.py"
#: ``service.py`` importing ``models`` through the portion.
PORTION_SERVICE = SERVICE_SOURCE.replace("fixture_auth.models", "fixture_auth.sansio.models")
#: A Home Assistant-style test: its own integration, and one only another repository holds.
HA_TEST = (
    "from custom_components.fixture_ha.const import domain\n"
    "from custom_components.fixture_other.other import other\n\n\n"
    "def test_domain() -> None:\n    domain()\n    other()\n"
)
HA = {
    "custom_components/fixture_ha/__init__.py": "",
    "custom_components/fixture_ha/const.py": 'def domain() -> str:\n    return "fixture"\n',
    "tests/__init__.py": "",
    "tests/test_const.py": HA_TEST,
}

NAMESPACES: dict[str, dict[str, str]] = {
    # flask's shape: sansio/ has no __init__, inside a regular package
    "portioned": {
        "pyproject.toml": '[project]\nname = "fixture-auth"\nversion = "1.0"\n',
        "src/fixture_auth/__init__.py": "",
        PORTION_MODELS: AUTH_MODELS,
        "src/fixture_auth/app.py": (
            "from .sansio.models import User, make\n\n\ndef build() -> User:\n    return make()\n"
        ),
        "src/fixture_auth/sansio/base.py": (
            "from ..app import build\nfrom .models import User\n\n\n"
            "def rebuild() -> User:\n    return build()\n"
        ),
    },
    "consumer": consumer("fixture-auth\n", {SERVICE: PORTION_SERVICE}),
    "undeclared": consumer("requests\n", {SERVICE: PORTION_SERVICE}),
    # hacs's shape: custom_components/ at the top of a metadata-less repository
    "ha": HA,
    "ha-declaring": {**HA, "requirements.txt": "custom-components\n"},
    # another repository holding the same namespace root
    "ha-other": {
        "custom_components/fixture_other/__init__.py": "",
        "custom_components/fixture_other/other.py": "def other() -> None:\n    pass\n",
    },
    # a repository holding no portion, requiring the namespace's name
    "plain": consumer(
        "custom-components\n",
        {
            SERVICE: (
                "from custom_components.fixture_other.other import other\n\n\n"
                "def load() -> None:\n    other()\n"
            )
        },
    ),
}
