"""The facts version tracks how a parse is stored, not the database schema.

A migration that adds a table nothing per-file writes into used to reparse every file of every
index, because the schema version was a component of the facts version. The component is now
``PARSER_FACTS_VERSION``, bumped by hand when what a parse stores per file changes, and
``test_every_schema_version_was_reviewed_for_facts`` makes the next migration say so.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repoatlas.dependencies.registry import DependencyRegistry
from repoatlas.indexing.chunking import CHUNKING_VERSION
from repoatlas.parsers.registry import ParserRegistry
from repoatlas.services.freshness_service import current_facts_version
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace
from repoatlas.storage.sqlite.database import SCHEMA_VERSION
from repoatlas.sync.planner import PARSER_FACTS_VERSION, facts_version
from tests.impact_corpus import build

#: The newest schema version whose migration was reviewed for a change to what a parse stores.
REVIEWED_SCHEMA_VERSION = 7


def test_a_schema_only_bump_does_not_move_the_facts_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = facts_version(ParserRegistry(), DependencyRegistry())

    monkeypatch.setattr("repoatlas.storage.sqlite.database.SCHEMA_VERSION", 999)
    monkeypatch.setattr("repoatlas.sync.planner.SCHEMA_VERSION", 999, raising=False)

    assert facts_version(ParserRegistry(), DependencyRegistry()) == before


def test_the_split_keeps_todays_facts_string() -> None:
    """Existing rows were written as ``schema:5||...``: they must stay current."""
    registry = ParserRegistry()
    dependency_registry = DependencyRegistry()

    assert PARSER_FACTS_VERSION == 5
    assert facts_version(registry, dependency_registry) == (
        f"schema:5||{registry.version}||deps:{dependency_registry.version}"
        f"||chunks:{CHUNKING_VERSION}"
    )


def test_a_parser_facts_bump_reparses_every_file(
    workspace: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Worker processes do not see a monkeypatch; a pool would also refuse itself on the
    # mismatch, but one worker keeps the run in this process outright.
    monkeypatch.setenv("REPOATLAS_SYNC_WORKERS", "1")
    build(
        workspace,
        tmp_path,
        {
            "demo": {
                "pkg/__init__.py": "",
                "pkg/app.py": "def run() -> int:\n    return 1\n",
                "pyproject.toml": '[project]\nname = "demo"\nversion = "1.0"\n',
                "notes.md": "# notes\n",
            }
        },
    )
    root = tmp_path / "repos"
    old = current_facts_version(workspace)
    total = workspace.database.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    assert total > 0

    monkeypatch.setattr("repoatlas.sync.planner.PARSER_FACTS_VERSION", PARSER_FACTS_VERSION + 1)
    new = current_facts_version(workspace)
    assert new != old
    assert new.startswith(f"schema:{PARSER_FACTS_VERSION + 1}||")
    assert PARSER_FACTS_VERSION + 1 == 6

    outdated = sum(
        workspace.files.count_outdated(repo.id, new)
        for repo in workspace.repositories.list_all()
        if repo.id is not None
    )
    assert outdated == total

    report = sync(workspace, workspace.resolver().resolve(root))
    assert sum(r.changed for r in report.repositories) == total, report

    again = sync(workspace, workspace.resolver().resolve(root))
    assert sum(r.changed for r in again.repositories) == 0, again


def test_every_schema_version_was_reviewed_for_facts() -> None:
    assert SCHEMA_VERSION == REVIEWED_SCHEMA_VERSION, (
        "decide whether the new migration changes what a parse stores per file; "
        "if yes bump PARSER_FACTS_VERSION; then update this constant."
    )
