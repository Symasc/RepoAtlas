"""A migration that stores no new per-file fact must not reparse an index.

The facts version used to contain the database schema version, so adding any table at all (even
one no parse writes into) made every file of every existing index read as indexed by an older build
and reparsed on the next sync. The probe migration (:mod:`tests.adversarial.migration_probe`) is
exactly that kind: a column on ``repositories``, an index, a data-only tail.

Two things are pinned here, and the second is what makes the first safe. After the upgrade a sync
changes no file and ``status`` reports nothing stale. And the facts that sync leaves in place are
the facts a fresh reindex of the same tree writes: not reparsing is only correct if what was kept
is what reparsing would have produced.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
from repoatlas.services.freshness_service import current_facts_version
from repoatlas.services.status_service import status
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace
from repoatlas.storage.sqlite.migrator import user_version
from tests.adversarial import migration_probe
from tests.adversarial.migration_probe import PROBE
from tests.impact_corpus import build

pytestmark = pytest.mark.adversarial

STALE = "stale (indexed by an older build)"

CORPUS = {
    "lib": {
        "pyproject.toml": '[project]\nname = "mylib"\nversion = "1.0"\ndependencies = ["attrs"]\n',
        "mylib/__init__.py": "",
        "mylib/core.py": "def helper(n: int) -> int:\n    return n + 1\n",
    },
    "app": {
        "pyproject.toml": (
            '[project]\nname = "myapp"\nversion = "1.0"\ndependencies = ["mylib", "requests>=2"]\n'
        ),
        "app/__init__.py": "",
        "app/main.py": (
            "import os\n"
            "from mylib.core import helper\n"
            "\n"
            "\n"
            "def run() -> int:\n"
            '    return helper(int(os.environ.get("APP_LIMIT", "1")))\n'
        ),
        "web/util.ts": "export function shout(s: string): string {\n  return s.toUpperCase();\n}\n",
        "web/index.ts": (
            'import { shout } from "./util";\n'
            "\n"
            "export function main(): string {\n"
            '  return shout("hi");\n'
            "}\n"
        ),
        "notes.md": "# notes\n\nSome words about the app.\n",
    },
}


def fingerprint(workspace: Workspace) -> Counter[tuple]:
    """Every per-file fact and every edge, id-free, as a multiset.

    Row ids are allocation order and ``indexed_at`` and ``mtime_ns`` are when, not what; every
    ``*_id`` is replaced by the key or the ``slug:path`` it names, so two indexes that hold the same
    facts compare equal however they were reached.
    """
    queries = {
        "files": (
            "SELECT r.slug, f.rel_path, f.content_hash, f.language, f.parser, f.parser_version, "
            "f.is_binary FROM files f JOIN repositories r ON r.id = f.repo_id"
        ),
        "entities": (
            "SELECT e.type, e.key, r.slug || ':' || f.rel_path, e.name, e.qualified_name, "
            "e.line_start, e.line_end, e.signature, e.meta_json FROM entities e "
            "LEFT JOIN files f ON f.id = e.file_id "
            "LEFT JOIN repositories r ON r.id = f.repo_id"
        ),
        "edges": (
            "SELECT e.type, s.key, d.key, r.slug || ':' || f.rel_path, e.line_start, e.line_end, "
            "e.parser, e.resolution, e.confidence FROM edges e "
            "JOIN entities s ON s.id = e.src_id JOIN entities d ON d.id = e.dst_id "
            "LEFT JOIN files f ON f.id = e.file_id "
            "LEFT JOIN repositories r ON r.id = f.repo_id"
        ),
        "unresolved_refs": (
            "SELECT r.slug || ':' || f.rel_path, s.key, u.edge_type, u.target_kind, u.target_key, "
            "u.line_start, u.line_end, u.confidence, u.meta_json FROM unresolved_refs u "
            "LEFT JOIN files f ON f.id = u.file_id "
            "LEFT JOIN repositories r ON r.id = f.repo_id "
            "LEFT JOIN entities s ON s.id = u.src_id"
        ),
        "chunks": (
            "SELECT r.slug || ':' || f.rel_path, c.line_start, c.line_end, c.text, c.span_index, "
            "c.split_index, c.split_total FROM chunks c JOIN files f ON f.id = c.file_id "
            "JOIN repositories r ON r.id = f.repo_id"
        ),
        "dependencies": (
            "SELECT r.slug || ':' || f.rel_path, d.ecosystem, d.name, d.normalized_name, d.role, "
            "d.constraint_text, d.version, d.group_name, d.line_start, d.line_end, d.parser "
            "FROM dependencies d JOIN files f ON f.id = d.file_id "
            "JOIN repositories r ON r.id = f.repo_id"
        ),
    }
    rows: Counter[tuple] = Counter()
    for table, sql in queries.items():
        for row in workspace.database.execute(sql).fetchall():
            rows[(table, *tuple(row))] += 1
    return rows


def test_a_migration_with_no_new_file_facts_keeps_every_file_current(
    workspace: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build(workspace, tmp_path, CORPUS)
    root = tmp_path / "repos"
    total = workspace.database.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    assert total > 0
    workspace.close()

    migration_probe.patch(monkeypatch.setattr)
    upgraded = open_workspace(settings=workspace.settings, require_root=False)
    try:
        assert user_version(upgraded.database.connection) == PROBE.version

        report = sync(upgraded, upgraded.resolver().resolve(root))
        assert [r.changed for r in report.repositories] == [0] * len(CORPUS), report
        assert [r.new for r in report.repositories] == [0] * len(CORPUS), report

        facts = current_facts_version(upgraded)
        repositories = upgraded.repositories.list_all()
        assert len(repositories) == len(CORPUS)
        assert [
            upgraded.files.count_outdated(repo.id, facts)
            for repo in repositories
            if repo.id is not None
        ] == [0] * len(CORPUS)
        reported = status(upgraded, upgraded.resolver().resolve(root))
        states = [r.index_state for r in reported.repositories]
        assert STALE not in states, states

        incremental = fingerprint(upgraded)
        filled = {key[0] for key in incremental}
        assert {"files", "entities", "edges", "chunks", "dependencies"} <= filled, filled

        # A fresh reindex of the same tree: every file's facts are dropped (the delete cascades)
        # and the next sync writes them all again.
        with upgraded.database.transaction() as conn:
            conn.execute("DELETE FROM files")
        reindexed = sync(upgraded, upgraded.resolver().resolve(root))
        assert sum(r.new for r in reindexed.repositories) == total, reindexed

        assert fingerprint(upgraded) == incremental
    finally:
        upgraded.close()
