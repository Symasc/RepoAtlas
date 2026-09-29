"""Python uses declared by a repository's root ``requirements.txt`` (P3a).

A repository stating nothing about packaging declares its dependencies in its root
``requirements.txt`` and nowhere else. Every repository is indexed whole; each case names
the declaration or the provider that must decide it, and what it resolves to -- or why it
refuses. The fixtures are in :mod:`tests.python_legacy_corpus`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build
from tests.python_corpus import refusals
from tests.python_legacy_corpus import HYBRID, SERVICE, pick, service_uses
from tests.python_packages_corpus import edges, uses

EXPLICIT_MODELS = "src/fixture_auth/models.py"
NAMES = ("User", "make")


@pytest.fixture
def hybrid(workspace: Workspace, tmp_path: Path) -> Workspace:
    return build(workspace, tmp_path, HYBRID)


def test_r9a_a_root_requirement_names_the_explicit_provider(hybrid: Workspace) -> None:
    """Not the metadata-less copy of the same package, which no metadata names."""
    assert uses(hybrid, "consumer") == service_uses("consumer", "explicit-auth", EXPLICIT_MODELS)
    [edge] = [
        e
        for e in edges(hybrid)
        if e["source"] == f"consumer:{SERVICE}#load"
        and e["target"] == f"explicit-auth:{EXPLICIT_MODELS}#make"
    ]
    assert edge["basis"] == "python-dependency"
    assert edge["crossing"] == {
        "package": "pypi:fixture-auth",
        "provider": "explicit-auth",
        "provided_at": "pyproject.toml:2",
        "entry": EXPLICIT_MODELS,
        "entry_fields": "src-layout",
        "subpath": "models",
        "declared_at": "requirements.txt:4",
        "constraint": ">=1.2",
        "imported_at": 1,
    }
    assert not [
        e
        for e in edges(hybrid)
        if e["target"].startswith("copy-auth:") and not e["source"].startswith("copy-auth:")
    ]


@pytest.mark.parametrize(
    ("repository", "why"),
    [
        ("devonly", "dependency-absent"),  # R22: requirements-dev.txt declares nothing here
        ("nestedonly", "dependency-absent"),  # R22: nor does a requirements.txt below the root
        ("vcs", "dependency-absent"),  # R23: a git+https line is read as a row named `git`
        ("direct", "dependency-unread"),  # R23b: `name @ url` names it in a form not read
        ("setuppy", "dependency-absent"),  # R24: a root setup.py is metadata this does not read
    ],
)
def test_only_the_root_requirements_file_of_a_metadata_less_repository_declares(
    workspace: Workspace, tmp_path: Path, repository: str, why: str
) -> None:
    linked = build(workspace, tmp_path, pick(HYBRID, "explicit-auth", repository))
    assert refusals(linked, repository, SERVICE) == dict.fromkeys(NAMES, why)
    assert not [
        e
        for e in edges(linked)
        if e["source"].startswith(f"{repository}:") and e["target"].startswith("explicit-auth:")
    ]
