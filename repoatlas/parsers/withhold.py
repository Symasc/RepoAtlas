"""What a deployment fact may never carry: credentials, and values that look like them.

Shared by the CI, values-file, ConfigMap and compose readers (DF1_PHASE_A.md §11). A
withheld value is stored as :data:`WITHHELD`, so the fact still says *that* the key is
set and where, never what to.

* a key path, ``--set`` path, input name or environment name whose last segments name a
  credential (:func:`credential_named`);
* any URL carrying user information (``scheme://user:secret@host``);
* any GitHub ``secrets.*`` expression.
"""

from __future__ import annotations

import re
from itertools import pairwise

WITHHELD = "<withheld>"

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


def guarded(name: str, value: str) -> str:
    """``value``, or :data:`WITHHELD` when the name or the value may carry a secret."""
    return WITHHELD if credential_named(name) or secret_bearing(value) else value
