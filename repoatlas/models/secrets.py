"""Which names and values may carry a secret, wherever RepoAtlas reads or accepts them.

The parsers withhold such values from the facts they record
(:mod:`repoatlas.parsers.withhold`); a user's deployment link refuses them
(:mod:`repoatlas.models.deployment_targets`). One definition, so both agree.
"""

from __future__ import annotations

import re
from itertools import pairwise

#: Words that name a credential on their own, and pairs that do together. A name is split
#: into words at separators and at camelCase (``rootPassword`` -> root, password), so a
#: credential is found whatever the naming style.
_CREDENTIAL_WORDS = frozenset(
    {
        "password",
        "passwd",
        "passphrase",
        "secret",
        "secrets",
        "token",
        "credential",
        "credentials",
        "apikey",
        "privatekey",
        "accesskey",
    }
)
_CREDENTIAL_PAIRS = frozenset({("api", "key"), ("private", "key"), ("access", "key")})
_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_USERINFO = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^/\s@]+@")
_SECRETS = re.compile(r"\bsecrets\.")


def credential_named(name: str) -> bool:
    """Whether a key path or name names a credential (``db.password``, ``API_TOKEN``,
    ``auth.rootPassword``, ``apiKey``)."""
    words = [word.lower() for word in _WORD.findall(name)]
    if any(word in _CREDENTIAL_WORDS for word in words):
        return True
    return any(pair in _CREDENTIAL_PAIRS for pair in pairwise(words))


def secret_bearing(value: str) -> bool:
    """Whether a value itself may carry a secret: user information in a URL, or a
    reference to the CI system's secrets."""
    return _USERINFO.search(value) is not None or _SECRETS.search(value) is not None
