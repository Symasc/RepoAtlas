"""Incremental ``USES`` resolution: an edit then a sync equals a fresh index of the same tree.

The fixtures and the comparison are in :mod:`tests.incremental_corpus`.
"""

from __future__ import annotations

from pathlib import Path

from repoatlas.config.settings import Settings
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build, uses
from tests.incremental_corpus import append, chain_repo, fresh_index, sync_all, use_snapshot

SHALLOW = ("shallow.ts#useShallow", "chain/c13.ts#Deep", "value")
DEEP = ("deep.ts#useDeep", "chain/c13.ts#Deep", "value")


def test_i0_a_use_gets_the_same_answer_whatever_was_resolved_before_it(
    settings: Settings, workspace: Workspace, tmp_path: Path
) -> None:
    """Thirteen hops is one too many, whether or not seven of them were traced before."""
    build(workspace, tmp_path, {"chain": chain_repo()})
    root = tmp_path / "repos"
    found = uses(workspace)
    assert SHALLOW in found
    assert DEEP not in found

    # Re-indexing deep.ts alone puts its claim after shallow.ts's, so the pass now reaches
    # the seven hops shallow.ts already traced before it reaches deep.ts's own thirteen.
    append(root, "chain", "deep.ts", "// touched\n")
    sync_all(workspace, root)

    incremental = use_snapshot(workspace)
    with fresh_index(settings, root, tmp_path, "fresh") as fresh:
        assert incremental == use_snapshot(fresh)
    assert DEEP not in uses(workspace)
