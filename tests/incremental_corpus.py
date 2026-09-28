"""Neutral fixtures for incremental ``USES`` resolution: an edit, then a sync, then a fresh index.

Each case edits one tree, syncs it incrementally, and compares the result with a pristine
database indexing the same tree. Nothing here is taken from a real repository or a benchmark
task; every file states the relationship it exists to prove.

* ``chain`` (I0): fourteen files each re-exporting the next with ``export *``, the last
  declaring ``Deep``. ``shallow.ts`` imports it seven hops from the end, ``deep.ts``
  thirteen -- one past :data:`~repoatlas.graph.modules.MAX_REEXPORT_HOPS` -- so ``deep``'s
  use resolves to nothing however the pass reached it. ``deep.ts`` sorts first, so a fresh
  index resolves it before ``shallow.ts``, and re-indexing it alone reverses that order.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from repoatlas.config.settings import Settings
from repoatlas.services.init_service import initialize
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace
from tests.impact_corpus import use_edges

CHAIN_LENGTH = 14


def chain_repo() -> dict[str, str]:
    files = {
        f"chain/c{i:02d}.ts": f'export * from "./c{i + 1:02d}";\n' for i in range(CHAIN_LENGTH - 1)
    }
    files[f"chain/c{CHAIN_LENGTH - 1:02d}.ts"] = "export const Deep = 13;\n"
    files["deep.ts"] = (
        'import { Deep } from "./chain/c00";\nexport function useDeep() {\n  return Deep;\n}\n'
    )
    files["shallow.ts"] = (
        'import { Deep } from "./chain/c07";\nexport function useShallow() {\n  return Deep;\n}\n'
    )
    return files


def use_snapshot(workspace: Workspace) -> list[str]:
    """Every ``USES`` edge, both ends labelled and its resolution decoded, in a stable order."""
    return sorted(json.dumps(edge, sort_keys=True) for edge in use_edges(workspace))


def edit(root: Path, repo: str, path: str, text: str) -> None:
    """Replace one file's content."""
    target = root / repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def append(root: Path, repo: str, path: str, text: str) -> None:
    target = root / repo / path
    target.write_text(target.read_text(encoding="utf-8") + text, encoding="utf-8")


def sync_all(workspace: Workspace, root: Path) -> None:
    """One ordinary sync of everything under the managed root."""
    sync(workspace, workspace.resolver().resolve(root))


@contextmanager
def fresh_index(settings: Settings, root: Path, tmp_path: Path, tag: str) -> Iterator[Workspace]:
    """A pristine database indexing the tree as it is now: what an incremental sync must equal."""
    fresh = open_workspace(
        settings=settings.model_copy(
            update={
                "storage": settings.storage.model_copy(
                    update={"database": tmp_path / tag / "atlas.db"}
                )
            }
        ),
        require_root=False,
    )
    try:
        initialize(fresh, root, config_path=tmp_path / f"{tag}-config.yaml")
        yield fresh
    finally:
        fresh.close()
