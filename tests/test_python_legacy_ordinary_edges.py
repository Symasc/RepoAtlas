"""Ordinary imports under the legacy requirements convention (P3a), pinned exactly.

A repository that states nothing about packaging requires another one by the name its root
``requirements.txt`` gives, and a metadata-less repository holding the module provides it. The
two names agree only by the **legacy key** (:func:`repoatlas.graph.py_packages.legacy_key`):
ASCII lowercase, then ``_`` for ``-``, and nothing else. A distribution name is not assumed to be
an import name -- ``beautifulsoup4`` is not ``bs4`` -- and PEP 503 normalisation, which also
folds ``.``, is not the convention: ``zope.interface`` is not ``zope_interface``.

The links are ordinary ``IMPORTS`` edges recorded with their ``crossing``; these tests read them
there, beside the ``USES`` tests in :mod:`tests.test_python_legacy`. Every name here is invented.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from repoatlas.graph.linker import GLOBAL_PARSER
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build
from tests.python_legacy_corpus import SERVICE, THING, importing

#: The repository holding the module: no metadata at all, the module at its root.
HOLDER_FILE = "{module}/__init__.py"


def _holder(module: str) -> dict[str, str]:
    return {HOLDER_FILE.format(module=module): THING}


def _ordinary_into(workspace: Workspace, holder: str) -> list[tuple[str, int, str, str | None]]:
    """(type, line, target path, resolution) of ``consumer``'s ordinary edges into ``holder``."""
    rows = workspace.database.execute(
        """
        SELECT e.type, e.line_start, df.rel_path, e.resolution
        FROM edges e JOIN files sf ON sf.id = e.file_id
        JOIN repositories sr ON sr.id = sf.repo_id
        JOIN entities d ON d.id = e.dst_id JOIN files df ON df.id = d.file_id
        JOIN repositories dr ON dr.id = df.repo_id
        WHERE sr.slug = 'consumer' AND dr.slug = ? AND sf.rel_path = ?
          AND e.parser = ? AND e.type IN ('IMPORTS', 'CALLS', 'REFERENCES', 'EXPORTS')
        ORDER BY e.type, e.line_start
        """,
        (holder, SERVICE, GLOBAL_PARSER),
    ).fetchall()
    return [(row[0], row[1], row[2], row[3]) for row in rows]


def test_a_requirement_equal_to_the_import_by_the_legacy_key_links_the_import(
    workspace: Workspace, tmp_path: Path
) -> None:
    """``common-lib`` is required and ``common_lib`` imported: the keys are both ``common-lib``."""
    indexed = build(
        workspace,
        tmp_path,
        {"holder": _holder("common_lib"), "consumer": importing("common-lib\n", "common_lib")},
    )
    imports = [row for row in _ordinary_into(indexed, "holder") if row[0] == "IMPORTS"]
    [(_, line, target, resolution)] = imports
    assert (line, target) == (1, "common_lib/__init__.py")
    assert resolution is not None
    record = json.loads(resolution)
    assert record["basis"] == "python-requirements-legacy"
    crossing = record["crossing"]
    assert crossing["resolved_by"] == "requirements-legacy"
    assert (crossing["requirement"], crossing["import"]) == ("common-lib", "common_lib")
    assert (crossing["legacy_requirement_key"], crossing["legacy_import_key"]) == (
        "common-lib",
        "common-lib",
    )
    assert (crossing["provider"], crossing["declared_at"], crossing["imported_at"]) == (
        "holder",
        "requirements.txt:1",
        1,
    )


@pytest.mark.parametrize(
    ("requirement", "module", "why"),
    [
        ("beautifulsoup4", "bs4", "a distribution name is not assumed to be an import name"),
        ("zope.interface", "zope_interface", "a `.` is not folded: this is not PEP 503"),
        ("common-lib-client", "common_lib", "a longer requirement does not name the module"),
    ],
)
def test_a_requirement_that_the_legacy_key_does_not_equate_links_nothing(
    workspace: Workspace, tmp_path: Path, requirement: str, module: str, why: str
) -> None:
    indexed = build(
        workspace,
        tmp_path,
        {"holder": _holder(module), "consumer": importing(f"{requirement}\n", module)},
    )
    assert _ordinary_into(indexed, "holder") == [], why
