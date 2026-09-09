"""Storage schema behavior (ARCHITECTURE.md §3)."""

from __future__ import annotations

from pathlib import Path

from repoatlas.config.settings import Settings, load_settings, save_settings
from repoatlas.models.parsing import EdgeType, EntityType
from repoatlas.models.repository import IndexedFile, Repository
from repoatlas.services.workspace import Workspace
from repoatlas.storage.protocols import ChunkRecord, EdgeRecord, EntityRecord, FileFacts


def _seed(workspace: Workspace, tmp_path: Path) -> tuple[int, int]:
    repo_id = workspace.repositories.upsert(Repository(name="demo", path=tmp_path / "demo"))
    file_id = workspace.files.upsert(
        repo_id,
        IndexedFile(rel_path="a.py", size=1, mtime_ns=1, content_hash="h", language="Python"),
    )
    return repo_id, file_id


def test_cascade_removes_every_fact_a_file_asserted(workspace: Workspace, tmp_path: Path) -> None:
    """Deleting a file removes its entities, edges and chunks (V1_SPEC §12)."""
    repo_id, file_id = _seed(workspace, tmp_path)
    workspace.knowledge.replace_file_facts(
        file_id,
        FileFacts(
            entities=(
                EntityRecord(
                    type=EntityType.FUNCTION,
                    key="sym:demo:a.py:f:1",
                    name="f",
                    repo_id=repo_id,
                    file_id=file_id,
                    line_start=1,
                    line_end=2,
                ),
            ),
            chunks=(
                ChunkRecord(
                    file_id=file_id, repo_id=repo_id, line_start=1, line_end=2, text="def f():"
                ),
            ),
        ),
    )
    assert workspace.knowledge.count_entities([repo_id]) == 1

    workspace.files.delete_many([file_id])

    assert workspace.knowledge.count_entities([repo_id]) == 0
    remaining = workspace.database.execute(
        "SELECT COUNT(*) AS n FROM chunks WHERE file_id = ?", (file_id,)
    ).fetchone()
    assert remaining["n"] == 0


def test_fts_rows_are_removed_with_their_chunks(workspace: Workspace, tmp_path: Path) -> None:
    repo_id, file_id = _seed(workspace, tmp_path)
    workspace.knowledge.replace_file_facts(
        file_id,
        FileFacts(
            chunks=(
                ChunkRecord(
                    file_id=file_id,
                    repo_id=repo_id,
                    line_start=1,
                    line_end=1,
                    text="UNIQUE_SEARCH_TOKEN_HERE",
                ),
            )
        ),
    )
    assert workspace.search.search_literal("UNIQUE_SEARCH_TOKEN_HERE")

    workspace.files.delete_many([file_id])
    assert workspace.search.search_literal("UNIQUE_SEARCH_TOKEN_HERE") == []


def test_interning_is_idempotent(workspace: Workspace) -> None:
    record = EntityRecord(type=EntityType.ENV_VAR, key="env:X", name="X")
    first = workspace.knowledge.intern_entity(record)
    second = workspace.knowledge.intern_entity(record)
    assert first == second


def test_orphan_gc_removes_unreferenced_globals(workspace: Workspace) -> None:
    workspace.knowledge.intern_entity(
        EntityRecord(type=EntityType.ENV_VAR, key="env:ORPHAN", name="ORPHAN")
    )
    assert workspace.knowledge.gc_orphan_entities() >= 1
    remaining = workspace.database.execute(
        "SELECT COUNT(*) AS n FROM entities WHERE key = 'env:ORPHAN'"
    ).fetchone()
    assert remaining["n"] == 0


def test_orphan_gc_keeps_referenced_globals(workspace: Workspace, tmp_path: Path) -> None:
    repo_id, file_id = _seed(workspace, tmp_path)
    workspace.knowledge.replace_file_facts(
        file_id,
        FileFacts(
            entities=(
                EntityRecord(
                    type=EntityType.FILE,
                    key="file:demo:a.py",
                    name="a.py",
                    repo_id=repo_id,
                    file_id=file_id,
                ),
            )
        ),
    )
    source = workspace.knowledge.find_file_entity(file_id, "file:demo:a.py")
    assert source is not None
    target = workspace.knowledge.intern_entity(
        EntityRecord(type=EntityType.ENV_VAR, key="env:KEPT", name="KEPT")
    )
    workspace.knowledge.add_edges(
        [
            EdgeRecord(
                src_id=source,
                dst_id=target,
                type=EdgeType.READS,
                repo_id=repo_id,
                file_id=file_id,
                line_start=1,
                line_end=1,
            )
        ]
    )

    workspace.knowledge.gc_orphan_entities()
    kept = workspace.database.execute(
        "SELECT COUNT(*) AS n FROM entities WHERE key = 'env:KEPT'"
    ).fetchone()
    assert kept["n"] == 1


def test_repository_upsert_is_keyed_on_path(workspace: Workspace, tmp_path: Path) -> None:
    first = workspace.repositories.upsert(Repository(name="demo", path=tmp_path / "demo"))
    second = workspace.repositories.upsert(Repository(name="renamed", path=tmp_path / "demo"))
    assert first == second
    stored = workspace.repositories.get_by_path(tmp_path / "demo")
    assert stored is not None
    assert stored.name == "renamed"


def test_edges_provenance_is_recorded(workspace: Workspace, tmp_path: Path) -> None:
    """Every fact must carry repo/file/line provenance (V1_SPEC §11)."""
    repo_id, file_id = _seed(workspace, tmp_path)
    workspace.knowledge.replace_file_facts(
        file_id,
        FileFacts(
            entities=(
                EntityRecord(
                    type=EntityType.FILE,
                    key="file:demo:a.py",
                    name="a.py",
                    repo_id=repo_id,
                    file_id=file_id,
                ),
            )
        ),
    )
    src = workspace.knowledge.find_file_entity(file_id, "file:demo:a.py")
    assert src is not None
    dst = workspace.knowledge.intern_entity(
        EntityRecord(type=EntityType.ENV_VAR, key="env:P", name="P")
    )
    workspace.knowledge.add_edges(
        [
            EdgeRecord(
                src_id=src,
                dst_id=dst,
                type=EdgeType.READS,
                repo_id=repo_id,
                file_id=file_id,
                line_start=7,
                line_end=7,
                parser="python-ast",
            )
        ]
    )
    row = workspace.database.execute(
        "SELECT repo_id, file_id, line_start, parser FROM edges"
    ).fetchone()
    assert row["repo_id"] == repo_id
    assert row["file_id"] == file_id
    assert row["line_start"] == 7
    assert row["parser"] == "python-ast"


def test_settings_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    settings = Settings.model_validate({"repos": {"root": str(tmp_path / "repos")}})
    save_settings(settings, path)
    loaded = load_settings(path)
    assert loaded.root == tmp_path / "repos"
    assert loaded.llm.model == "gpt-oss:20b"
    assert loaded.llm.provider == "ollama"


def test_settings_default_when_absent(tmp_path: Path) -> None:
    assert load_settings(tmp_path / "missing.yaml").root is None
