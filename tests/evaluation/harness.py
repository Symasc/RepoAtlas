"""Builds the indexed ecosystem the evaluation runs against.

Shared by the pytest evaluation tests and the standalone runner, so the numbers
in a report and the numbers in CI come from the same index.
"""

from __future__ import annotations

import contextlib
import tempfile
from collections.abc import Iterator
from pathlib import Path

from repoatlas.config.settings import Settings
from repoatlas.services.init_service import initialize
from repoatlas.services.workspace import Workspace, open_workspace


def build(root_source: Path, destination: Path) -> tuple[Workspace, Path]:
    """Copy the fixture ecosystem, give it git history, and index it."""
    from tests.adversarial.conftest import copy_ecosystem

    root = copy_ecosystem(root_source, destination / "repos")

    settings = Settings.model_validate(
        {"storage": {"database": str(destination / "state" / "atlas.db")}}
    )
    workspace = open_workspace(settings=settings, require_root=False)
    initialize(workspace, root, config_path=destination / "config.yaml")
    return workspace, root


@contextlib.contextmanager
def evaluation_workspace() -> Iterator[tuple[Workspace, Path]]:
    from tests.adversarial.conftest import ADVERSARIAL_ROOT

    with tempfile.TemporaryDirectory() as raw:
        workspace, root = build(ADVERSARIAL_ROOT, Path(raw))
        try:
            yield workspace, root
        finally:
            workspace.close()
