"""Package-name normalization (V1_SPEC §9).

Cross-repository edges are created on **exact normalized-name equality within one
ecosystem**, never on similarity. Every ecosystem's own equality rule lives here,
so "exact" means what the ecosystem means by it rather than what string equality
happens to give.
"""

from __future__ import annotations

import re

PYPI = "pypi"
NPM = "npm"
GEM = "gem"
DOCKER = "docker"

#: PEP 503: runs of ``-``, ``_`` and ``.`` are equivalent, comparison is lowercase.
_PEP503_SEPARATORS = re.compile(r"[-_.]+")

#: ``foo>=1.0``, ``foo[extra]~=2``, ``foo (>=1)`` — the name is what precedes these.
_PYTHON_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def normalize(ecosystem: str, name: str) -> str:
    """Canonical form of ``name`` for equality within ``ecosystem``."""
    cleaned = name.strip()
    if ecosystem == PYPI:
        return _PEP503_SEPARATORS.sub("-", cleaned).lower()
    if ecosystem == NPM:
        # npm names are lowercase by registry rule; the ``@scope/`` prefix is part
        # of the identity and is deliberately preserved.
        return cleaned.lower()
    if ecosystem == GEM:
        return cleaned.lower()
    return cleaned


def python_requirement(specifier: str) -> tuple[str, str | None] | None:
    """Split a PEP 508 requirement into its name and the rest as a constraint."""
    text = specifier.strip()
    if not text or text.startswith("#"):
        return None
    text = text.split(";", 1)[0].strip()  # drop environment markers
    match = _PYTHON_NAME.match(text)
    if match is None:
        return None
    name = match.group(1)
    remainder = text[match.end() :].strip()
    if remainder.startswith("["):
        closing = remainder.find("]")
        if closing != -1:
            remainder = remainder[closing + 1 :].strip()
    return name, remainder or None
