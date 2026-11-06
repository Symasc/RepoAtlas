"""R2c-1 G1: an incremental sync across an edit of the P3 helper shape is a fresh index.

``p3_helper`` (``tests/r2c_provider_corpus.py``) registers its blueprint in a same-module helper
that the factory calls with the application: ``pkg/app.py`` is the helper's file, ``pkg/views.py``
the blueprint's. The global pass expands the helper's claim at the factory's call, so what the
route is served at is decided by two files and by claims stored in the first. Each edit below
rewrites one file of the indexed repository; after the sync the index must equal a fresh index of
the edited tree (the invariant), and must serve what the edited tree means. The baseline is then
restored and synced again: the route must come back, and the index must again equal a fresh one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.parsers.http_facts import HELPER_CALL_ROLE, HELPER_ROLE
from repoatlas.parsers.interpreter import interpreter_tag
from repoatlas.parsers.python import PythonParser
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace
from repoatlas.sync.planner import facts_version
from tests.impact_corpus import build
from tests.incremental_corpus import assert_equals_fresh, edit, sync_all
from tests.r2c_provider_corpus import P3_APP, VIEWS, repos

REPO = "p3_helper"
APP = "pkg/app.py"
VIEWS_PATH = "pkg/views.py"
BASELINE = {"GET /one"}

SERVES_SQL = """SELECT t.name FROM edges e JOIN entities t ON t.id = e.dst_id
                WHERE e.type = 'SERVES_HTTP_ROUTE'"""


def served(workspace: Workspace) -> set[str]:
    """Every route a ``SERVES_HTTP_ROUTE`` edge of the workspace states (the global pass's too)."""
    return {row[0] for row in workspace.database.execute(SERVES_SQL).fetchall()}


def helper_claims(workspace: Workspace) -> int:
    """How many stored claims are the helper's (``python-helper`` and ``python-helper-call``)."""
    return workspace.database.execute(
        "SELECT COUNT(*) FROM unresolved_refs WHERE meta_json LIKE '%python-helper%'"
    ).fetchone()[0]


@dataclass(frozen=True)
class Edit:
    """One rewrite of one file of the indexed repository, and what the edited tree serves."""

    path: str
    text: str
    expected: set[str]
    #: ``False``: the edited tree has nothing for a helper claim to record, so none may remain.
    helper_claimed: bool | None = None


# The registration line is the whole of the helper's body: the edit that removes it leaves `pass`
# (an empty body would be a syntax error, which is another test's subject, not this one's).
REGISTRATION = "    app.register_blueprint(views.bp)\n"
ROUTE = '@bp.route("/one")\ndef one():\n    return {}\n'

assert REGISTRATION in P3_APP
assert ROUTE in VIEWS

EDITS: dict[str, Edit] = {
    "registration-removed": Edit(APP, P3_APP.replace(REGISTRATION, "    pass\n"), set(), False),
    "url-prefix-added": Edit(
        APP,
        P3_APP.replace("views.bp)", 'views.bp, url_prefix="/z")'),
        {"GET /z/one"},
    ),
    "helper-renamed-everywhere": Edit(
        APP, P3_APP.replace("register_blueprints", "register_routes"), BASELINE
    ),
    "helper-renamed-at-the-def-only": Edit(
        APP, P3_APP.replace("def register_blueprints(app):", "def register_routes(app):"), set()
    ),
    "route-deleted": Edit(VIEWS_PATH, VIEWS.replace(ROUTE, ""), set()),
}


def baseline_text(path: str) -> str:
    return P3_APP if path == APP else VIEWS


@pytest.fixture
def indexed(workspace: Workspace, tmp_path: Path) -> Workspace:
    build(workspace, tmp_path, {REPO: repos()[REPO]})
    assert served(workspace) == BASELINE
    return workspace


@pytest.mark.parametrize("case", sorted(EDITS))
def test_an_edit_of_the_helper_shape_equals_a_fresh_index(
    indexed: Workspace, settings: Settings, tmp_path: Path, case: str
) -> None:
    change = EDITS[case]
    root = tmp_path / "repos"

    edit(root, REPO, change.path, change.text)
    sync_all(indexed, root)
    assert served(indexed) == change.expected
    if change.helper_claimed is False:
        assert helper_claims(indexed) == 0
    assert_equals_fresh(indexed, settings, root, tmp_path, f"{case}-edited")

    # Undo it: the route is served again, and the state is again a fresh index's.
    edit(root, REPO, change.path, baseline_text(change.path))
    sync_all(indexed, root)
    assert served(indexed) == BASELINE
    assert_equals_fresh(indexed, settings, root, tmp_path, f"{case}-restored")


def test_an_index_written_before_the_helper_reader_is_reparsed_on_upgrade(
    indexed: Workspace, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The helper reader changed what a parse stores, so the parser version moved (28 to 29). An
    index of the old parser (no helper claim, no route promoted from one) is reparsed on the next
    sync and ends equal to a fresh index. Worker processes do not see a monkeypatch: parse here."""
    monkeypatch.setenv("REPOATLAS_SYNC_WORKERS", "1")
    root = tmp_path / "repos"
    assert helper_claims(indexed) > 0

    live = facts_version(indexed.registry, indexed.dependency_registry)
    version = f"{PythonParser.name}:{PythonParser.version}"
    assert version in live, live
    # A literal, not "one less than now": a relative offset would pass with the bump deleted.
    previous = live.replace(version, f"{PythonParser.name}:28+{interpreter_tag()}")
    assert previous != live
    # The old parser stored no helper claim, so the global pass promoted nothing from one.
    indexed.database.execute(
        "DELETE FROM unresolved_refs WHERE meta_json LIKE ? OR meta_json LIKE ?",
        (f"%{HELPER_ROLE}%", f"%{HELPER_CALL_ROLE}%"),
    )
    indexed.database.execute(
        "DELETE FROM edges WHERE type = 'SERVES_HTTP_ROUTE' AND parser = 'linker:global'"
    )
    indexed.database.execute(
        "UPDATE files SET parser_version = ? WHERE rel_path LIKE '%.py'", (previous,)
    )
    indexed.database.connection.commit()
    assert helper_claims(indexed) == 0
    assert served(indexed) == set()

    report = sync(indexed, indexed.resolver().resolve(root))
    assert sum(r.changed for r in report.repositories) >= 1, report
    assert helper_claims(indexed) > 0
    assert served(indexed) == BASELINE
    assert_equals_fresh(indexed, settings, root, tmp_path, "upgraded")


def test_every_edit_rewrites_a_file_of_the_shape() -> None:
    files = repos()[REPO]
    assert all(change.path in files for change in EDITS.values())
    assert all(change.text != baseline_text(change.path) for change in EDITS.values())
