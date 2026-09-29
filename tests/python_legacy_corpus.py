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

``LEGACY``: metadata-less repositories holding modules at their root, and consumers
requiring them by the legacy convention (R1-R14, R23c, R25), each module name standing
for one case: ``fixture_auth`` held once, ``dup_lib`` twice, ``grid_kit`` also shipped
by an explicit project under another name, ``mail_kit`` required by a name an explicit
project provides without shipping it, and so on beside each entry.
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


#: ``fixture_auth``, as a repository with no metadata at all holds it.
AUTH_PACKAGE = {
    "fixture_auth/__init__.py": "from .models import User, make\n",
    "fixture_auth/models.py": AUTH_MODELS,
}
THING = "class Thing:\n    pass\n"


def importing(requirements: str, module: str) -> dict[str, str]:
    """A metadata-less consumer requiring ``requirements`` and using ``module``'s ``Thing``."""
    return {
        "requirements.txt": requirements,
        SERVICE: f"from {module} import Thing\n\n\ndef load() -> Thing:\n    return Thing()\n",
    }


LEGACY: dict[str, dict[str, str]] = {
    # fixture_auth, held by one metadata-less repository
    "auth-provider": AUTH_PACKAGE,
    "basic": consumer("fixture-auth\n"),  # R1
    "capital": consumer("# services\n\nFixture-Auth\n"),  # R2: line 3
    "versioned": consumer("fixture-auth>=1.2\n"),  # R3
    "extras": consumer("fixture-auth[crypto]>=1\n"),  # R4
    "undeclared": consumer("requests\n"),  # R5
    "mismatch": consumer("fixture-client\n"),  # R6
    "fuzzy": consumer("fixtureauth\nfixture-auth-client\nfixture-auth-v2\n"),  # R7b
    "pep503": consumer("Fixture.Auth\nfixture--auth\n"),  # R7c: PEP 503-equal only
    "reexport": consumer(  # R14
        "fixture-auth\n",
        {SERVICE: SERVICE_SOURCE.replace("fixture_auth.models", "fixture_auth")},
    ),
    "casing": consumer(  # LM4: the import's own spelling is looked up, not a lowered one
        "fixture-auth\n", {SERVICE: SERVICE_SOURCE.replace("fixture_auth", "Fixture_Auth")}
    ),
    "devonly": HYBRID["devonly"],  # R22
    "nestedonly": HYBRID["nestedonly"],  # R22
    "vcs": HYBRID["vcs"],  # R23
    "direct": HYBRID["direct"],  # R23b
    "setuppy": HYBRID["setuppy"],  # R24
    # R7: names that look like widget_kit, none of them it
    "lookalikes": {
        "widget_kit_client/__init__.py": THING,
        "widgetkit/__init__.py": THING,
        "widget_kit_v2/__init__.py": THING,
    },
    "widgets": importing("widget-kit\n", "widget_kit"),
    # R8: two repositories hold dup_lib
    "dup-a": {"dup_lib/__init__.py": THING},
    "dup-b": {"dup_lib/__init__.py": THING},
    "dupuser": importing("dup-lib\n", "dup_lib"),
    # R8b: a regular package and a namespace portion
    "half-a": {"half_lib/__init__.py": THING},
    "half-b": {"half_lib/part.py": THING},
    "halfuser": importing("half-lib\n", "half_lib"),
    # R9b: a project ships grid_kit as another distribution; the requirement names neither
    "grid-explicit": {
        "pyproject.toml": '[project]\nname = "grid-core"\n',
        "src/grid_kit/__init__.py": THING,
    },
    "grid-copy": {"grid_kit/__init__.py": THING},
    "griduser": importing("grid-kit\n", "grid_kit"),
    # R10: the distribution the requirement names exists, and ships another module
    "mail-explicit": {
        "pyproject.toml": '[project]\nname = "mail-kit"\n',
        "src/other_mod/__init__.py": THING,
    },
    "mail-copy": {"mail_kit/__init__.py": THING},
    "mailuser": importing("mail-kit\n", "mail_kit"),
    # R10b: ... named by a root setup.py
    "sms-setup": {
        "setup.py": 'from setuptools import setup\n\nsetup(name="sms-kit")\n',
        "sms_kit/__init__.py": THING,
    },
    "sms-copy": {"sms_kit/__init__.py": THING},
    "smsuser": importing("sms-kit\n", "sms_kit"),
    # R11: a module, not a package
    "solo-provider": {"solo_mod.py": THING},
    "solouser": importing("solo-mod\n", "solo_mod"),
    # R12: only under a src/ nothing states is a root
    "src-provider": {"src/src_kit/__init__.py": THING},
    "srcuser": importing("src-kit\n", "src_kit"),
    # R13: a namespace portion
    "ns-provider": {"ns_kit/part.py": THING},
    "nsuser": importing("ns-kit\n", "ns_kit.part"),
    # R25: a regular package under src/ makes src/ the root, and flip_kit no module
    "flip-provider": {"flip_kit/__init__.py": THING, "src/other/__init__.py": ""},
    "flipuser": importing("flip-kit\n", "flip_kit"),
    # R23c: the row a git+https line yields is named `git`, and declares nothing
    "gitlike": {"git/__init__.py": THING},
    "gituser": importing("git+https://example.invalid/org/tool.git\n", "git"),
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
