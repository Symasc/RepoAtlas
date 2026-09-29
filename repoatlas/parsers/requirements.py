"""How a PEP 508 requirement string is read: its distribution name and the rest.

Below both the dependency layer, which records each requirement a manifest states (its
``python`` module, which re-exports this through ``normalize``), and the setuptools facts,
which record whether a
``setup.cfg`` or ``setup()`` states its ``install_requires`` as requirements at all
(:mod:`repoatlas.parsers.setuptools_project`): the two must read a requirement alike, and
a language parser never imports the dependency layer.
"""

from __future__ import annotations

import re

#: ``foo>=1.0``, ``foo[extra]~=2``, ``foo (>=1)`` — the name is what precedes these.
_PYTHON_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


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
