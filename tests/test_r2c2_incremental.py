"""R2c-2: an incremental sync across an edit of a package's export table is a fresh index.

``p2b_two_level_bound`` (``tests/r2c_provider_corpus.py``) registers ``users.views.bp`` in
``create_app``; the global pass serves ``GET /u/two`` only because ``pkg/users/__init__.py`` says
``from . import views`` (the second hop's proof). That file is not the registration's, nor the
blueprint's: editing it alone must move the route. Emptying it removes the proof, so the route goes;
the baseline restored brings it back; after each sync the index equals a fresh index of the tree.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build
from tests.incremental_corpus import assert_equals_fresh, edit, sync_all
from tests.r2c_provider_corpus import USERS_INIT_BOUND, repos

REPO = "p2b_two_level_bound"
USERS_INIT = "pkg/users/__init__.py"
BASELINE = {"GET /u/two"}

SERVES_SQL = """SELECT t.name FROM edges e JOIN entities t ON t.id = e.dst_id
                WHERE e.type = 'SERVES_HTTP_ROUTE'"""


def served(workspace: Workspace) -> set[str]:
    """Every route a ``SERVES_HTTP_ROUTE`` edge of the workspace states (the global pass's too)."""
    return {row[0] for row in workspace.database.execute(SERVES_SQL).fetchall()}


@pytest.fixture
def indexed(workspace: Workspace, tmp_path: Path) -> Workspace:
    build(workspace, tmp_path, {REPO: repos()[REPO]})
    assert served(workspace) == BASELINE
    return workspace


def test_emptying_the_package_init_removes_the_two_level_route_and_restoring_it_brings_it_back(
    indexed: Workspace, settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "repos"
    assert repos()[REPO][USERS_INIT] == USERS_INIT_BOUND

    edit(root, REPO, USERS_INIT, "")
    sync_all(indexed, root)
    assert served(indexed) == set()
    assert_equals_fresh(indexed, settings, root, tmp_path, "init-emptied")

    # Undo it: the route is served again, and the state is again a fresh index's.
    edit(root, REPO, USERS_INIT, USERS_INIT_BOUND)
    sync_all(indexed, root)
    assert served(indexed) == BASELINE
    assert_equals_fresh(indexed, settings, root, tmp_path, "init-restored")
