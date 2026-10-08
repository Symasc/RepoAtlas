"""A definition's parser facts (``ParsedSymbol.meta``) are indexed, not dropped.

The global link pass resolves some claims against facts a parser stated about a
definition — what kind of declaration it is, what it was generated from — so those
facts have to survive into the index exactly as stated, and an incremental sync has to
leave them exactly as a full reindex would.
"""

from __future__ import annotations

from pathlib import Path

from repoatlas.config.settings import Settings
from repoatlas.models.parsing import EntityType
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace
from tests.conftest import fact_snapshot, git, reindex_from_scratch

CONFIG = "src/config.py"


def _repo_id(workspace: Workspace, name: str) -> int:
    row = workspace.database.execute(
        "SELECT id FROM repositories WHERE name = ?", (name,)
    ).fetchone()
    assert row is not None
    return int(row["id"])


def _meta(workspace: Workspace, qualified_name: str) -> dict[str, str]:
    symbols = workspace.knowledge.list_symbols(
        [EntityType.FUNCTION], _repo_id(workspace, "python-service")
    )
    return next(s.meta for s in symbols if s.qualified_name == qualified_name)


def test_a_definitions_parser_facts_are_indexed(initialized: Workspace) -> None:
    assert _meta(initialized, "reload_config") == {"async": "true"}
    stored = initialized.database.execute(
        "SELECT meta_json FROM entities WHERE qualified_name = 'reload_config'"
    ).fetchone()
    # Sorted JSON: the same facts are the same bytes on every build.
    assert stored["meta_json"] == '{"async": "true"}'


def test_a_definition_with_no_facts_stores_none(initialized: Workspace) -> None:
    row = initialized.database.execute(
        "SELECT meta_json FROM entities WHERE type = 'function' AND meta_json IS NULL LIMIT 1"
    ).fetchone()
    assert row is not None
    symbols = initialized.knowledge.list_symbols([EntityType.FUNCTION])
    assert any(s.meta == {} for s in symbols)


def test_an_edited_fact_syncs_exactly_as_a_reindex_writes_it(
    initialized: Workspace, workspace_root: Path, settings: Settings, tmp_path: Path
) -> None:
    repo = workspace_root / "python-service"
    source = (repo / CONFIG).read_text(encoding="utf-8")
    assert "async def reload_config" in source
    (repo / CONFIG).write_text(source.replace("async def reload_config", "def reload_config"))
    git(repo, "commit", "-qam", "reload synchronously")

    sync(initialized, initialized.resolver().resolve(repo))

    assert _meta(initialized, "reload_config") == {}
    assert fact_snapshot(initialized) == reindex_from_scratch(settings, workspace_root, tmp_path)
