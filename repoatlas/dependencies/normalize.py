"""Package-name normalization (V1_SPEC §9).

Cross-repository edges are created on **exact normalized-name equality within one
ecosystem**, never on similarity. Every ecosystem's own equality rule lives here,
so "exact" means what the ecosystem means by it rather than what string equality
happens to give.
"""

from __future__ import annotations

import re

# read below the dependency layer, which the setuptools facts share; re-exported here
from repoatlas.parsers.requirements import python_requirement as python_requirement

PYPI = "pypi"
NPM = "npm"
GEM = "gem"
DOCKER = "docker"

#: Ecosystems in which one *repository* can be said to provide a package.
#:
#: Cross-repository dependency resolution is defined only here (V1_SPEC §9): these
#: are the ecosystems whose manifests name the package the repository itself
#: publishes, so a declaration in one repository can be matched against a
#: provision in another. Outside this set RepoAtlas records declarations and
#: resolutions perfectly well and simply proves nothing about which repository, if
#: any, is behind the name.
REPOSITORY_ECOSYSTEMS = frozenset({PYPI, NPM, GEM})

#: PEP 503: runs of ``-``, ``_`` and ``.`` are equivalent, comparison is lowercase.
_PEP503_SEPARATORS = re.compile(r"[-_.]+")


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
