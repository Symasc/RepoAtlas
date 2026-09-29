"""Neutral fixtures for Python uses declared by a root ``requirements.txt`` (P3a).

Nothing here is taken from a real repository; every repository states the declaration,
layout or metadata it exists to prove. A test builds only the repositories it names
(:func:`pick`), so each case sees exactly the providers it is about.

``HYBRID``: consumers whose repository states nothing about packaging, declaring
``fixture-auth`` in their root ``requirements.txt``, and ``explicit-auth``, a project that
names itself ``fixture-auth`` and ships ``fixture_auth`` from ``src/``; ``copy-auth`` holds
the same package with no metadata at all. The requirement names the explicit provider
(R9a); the declarations the root file does not make, or makes in a form not read, name
nothing (R22-R24).
"""

from __future__ import annotations

#: ``User`` on line 1 and ``make`` on line 5 of every copy of the models module.
AUTH_MODELS = "class User:\n    pass\n\n\ndef make() -> User:\n    return User()\n"
SERVICE = "service.py"
SERVICE_SOURCE = (
    "from fixture_auth.models import User, make\n\n\ndef load() -> User:\n    return make()\n"
)
TEST_SOURCE = "from service import load\n\n\ndef test_load():\n    load()\n"


def consumer(requirements: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    """A repository with no packaging metadata, its root requirements, and a service."""
    return {
        "requirements.txt": requirements,
        SERVICE: SERVICE_SOURCE,
        "tests/test_service.py": TEST_SOURCE,
        **(extra or {}),
    }


HYBRID: dict[str, dict[str, str]] = {
    "explicit-auth": {
        "pyproject.toml": '[project]\nname = "fixture-auth"\nversion = "1.0"\n',
        "src/fixture_auth/__init__.py": "from .models import User, make\n",
        "src/fixture_auth/models.py": AUTH_MODELS,
    },
    "copy-auth": {
        "fixture_auth/__init__.py": "from .models import User, make\n",
        "fixture_auth/models.py": AUTH_MODELS,
    },
    # the requirement on line 4, after a comment, a blank line and another requirement
    "consumer": consumer("# pinned by hand\n\nrequests==2.31\nFixture-Auth>=1.2\n"),
    "devonly": consumer("requests\n", {"requirements-dev.txt": "fixture-auth\n"}),
    "nestedonly": consumer("requests\n", {"app/requirements.txt": "fixture-auth\n"}),
    "vcs": consumer("git+https://example.invalid/org/fixture-auth.git#egg=fixture-auth\n"),
    "direct": consumer("fixture-auth @ https://example.invalid/fixture_auth-1.0.whl\n"),
    "setuppy": consumer(
        "fixture-auth\n", {"setup.py": 'from setuptools import setup\n\nsetup(name="app")\n'}
    ),
}


def pick(corpus: dict[str, dict[str, str]], *names: str) -> dict[str, dict[str, str]]:
    return {name: corpus[name] for name in names}


def service_uses(consumer_repo: str, provider: str, models: str) -> set[tuple[str, str, str]]:
    """What ``service.py`` uses of ``fixture_auth.models``, and its own test."""
    here = f"{consumer_repo}:{SERVICE}"
    return {
        (f"{here}#load", f"{provider}:{models}#User", "type"),
        (f"{here}#load", f"{provider}:{models}#make", "call"),
        (f"{consumer_repo}:tests/test_service.py#test_load", f"{here}#load", "call"),
    }
