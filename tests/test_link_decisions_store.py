"""``link_decisions``: why a link family did or did not link, stored whole and replaced whole.

The global pass writes every decision with the edges it accounts for, inside one
transaction, so a reader never sees decisions from one pass beside edges from another.
"""

from __future__ import annotations

from pathlib import Path

from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace
from repoatlas.storage.protocols import LinkDecisionRow
from repoatlas.storage.sqlite.database import SCHEMA_VERSION
from repoatlas.storage.sqlite.migrator import load_migrations, user_version
from tests.conftest import fact_snapshot


def _anchor(workspace: Workspace, path: str) -> tuple[int, int, int]:
    row = workspace.database.execute(
        "SELECT f.repo_id, f.id, e.id FROM files f JOIN entities e ON e.file_id = f.id "
        "WHERE f.rel_path = ? AND e.type = 'file'",
        (path,),
    ).fetchone()
    return int(row[0]), int(row[1]), int(row[2])


def _row(
    anchor: tuple[int, int, int], subject: str, decision: str, family: str = "f"
) -> LinkDecisionRow:
    repo_id, file_id, src_id = anchor
    return LinkDecisionRow(
        decision_ref=f"{family}:{subject}",
        family=family,
        repo_id=repo_id,
        file_id=file_id,
        src_id=src_id,
        line_start=2,
        line_end=3,
        subject=subject,
        decision=decision,
        evidence_json='{"read": "test"}',
    )


def test_the_schema_is_the_committed_chain() -> None:
    assert [m.version for m in load_migrations()] == list(range(1, SCHEMA_VERSION + 1))


def test_decisions_are_replaced_whole_and_read_back_in_order(initialized: Workspace) -> None:
    knowledge = initialized.knowledge
    assert user_version(initialized.database.connection) == SCHEMA_VERSION
    anchor = _anchor(initialized, "src/config.py")
    knowledge.replace_link_decisions([_row(anchor, "b", "linked"), _row(anchor, "a", "refused")])
    knowledge.replace_link_decisions(
        [_row(anchor, "c", "linked"), _row(anchor, "d", "refused", family="g")]
    )

    stored = knowledge.list_link_decisions()
    assert [(r.family, r.subject, r.decision, r.path) for r in stored] == [
        ("f", "c", "linked", "src/config.py"),
        ("g", "d", "refused", "src/config.py"),
    ]
    assert [r.subject for r in knowledge.list_link_decisions(family="g")] == ["d"]
    assert knowledge.list_link_decisions(repo_id=anchor[0] + 1_000) == []


def test_a_decision_goes_with_the_file_it_was_anchored_in(
    initialized: Workspace, workspace_root: Path
) -> None:
    """A deleted file takes its decisions with it; the next global pass decides afresh."""
    anchor = _anchor(initialized, "src/config.py")
    initialized.knowledge.replace_link_decisions([_row(anchor, "a", "linked")])
    (workspace_root / "python-service" / "src" / "config.py").unlink()

    sync(initialized, initialized.resolver().resolve(workspace_root / "python-service"))

    assert initialized.knowledge.list_link_decisions() == []
    assert fact_snapshot(initialized)["link_decisions"] == []
