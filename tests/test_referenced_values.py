# ruff: noqa: E501 - the sample configurations hold their lines as a CI system reads them
"""A values file read only because a release names it (DF2_PHASE_A.md §15, E1).

``-f env/test/api.yaml`` makes that file an overlay; no parser can tell from the file alone.
It is read at the end of its repository's sync, from the release claims stored then: only
files a top-level release names, that the sync admitted, read as the parse read them. Its
claims are replaced whole on every sync, so a reference added, changed or removed, and a
named file edited, deleted or renamed, give exactly what a fresh index gives.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from repoatlas.config.settings import Settings
from repoatlas.parsers.deploy_helm import REFERENCED_VALUES, REFERENCED_VALUES_KEY
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace
from tests.conftest import fact_snapshot
from tests.impact_corpus import build

WORKFLOW = """\
name: deploy
on:
  push:
    branches: [main]
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - run: helm upgrade --install api charts/api -f env/test/api.yaml -f env/test/values-extra.yaml
      - run: |
          for e in qa; do
            helm upgrade --install api-$e charts/api -f env/test/looped.yaml
          done
      - run: helm upgrade --install hidden charts/api -f env/test/ignored.yaml
"""

FILES = {
    "charts/api/Chart.yaml": "apiVersion: v2\nname: api\nversion: 1.0.0\n",
    "charts/api/values.yaml": "ingress:\n  host: ''\n",
    ".github/workflows/deploy.yml": WORKFLOW,
    "env/test/api.yaml": "ingress:\n  host: api.test.example\nauth:\n  password: hunter2\n",
    "env/test/values-extra.yaml": "replicas: 2\n",
    "env/test/looped.yaml": "ingress:\n  host: looped.example\n",
    "env/test/unused.yaml": "ingress:\n  host: unused.example\n",
    "env/test/ignored.yaml": "ingress:\n  host: ignored.example\n",
    ".gitignore": "env/test/ignored.yaml\n",
}


def derived(workspace: Workspace) -> dict[str, dict[str, Any]]:
    return {
        path: json.loads(meta)
        for _, path, meta in workspace.knowledge.list_facts_of_kind(REFERENCED_VALUES_KEY)
    }


def state(workspace: Workspace) -> tuple[object, ...]:
    """Every fact, with the derived claims' content and the stored decisions in full."""
    decisions = workspace.database.execute(
        "SELECT family, subject, decision, evidence_json FROM link_decisions ORDER BY family, subject"
    ).fetchall()
    return (fact_snapshot(workspace), derived(workspace), [tuple(r) for r in decisions])


def fresh(settings: Settings, root: Path, tmp_path: Path, tag: str) -> tuple[object, ...]:
    from repoatlas.services.init_service import initialize

    other = open_workspace(
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
        initialize(other, root, config_path=tmp_path / f"{tag}-config.yaml")
        return state(other)
    finally:
        other.close()


def test_only_a_file_a_release_names_is_read_and_its_credentials_withheld(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(workspace, tmp_path, {"deployer": FILES})
    found = derived(workspace)
    # values-extra.yaml is a values*.y(a)ml its own parse reads; looped.yaml is named only
    # inside a loop; unused.yaml by no one; ignored.yaml is not admitted
    assert list(found) == ["env/test/api.yaml"]
    meta = found["env/test/api.yaml"]
    assert meta["derived"] == REFERENCED_VALUES
    assert json.loads(meta["referenced_by"]) == [".github/workflows/deploy.yml:9"]
    assert json.loads(meta["values"]) == {
        "ingress.host": "api.test.example",
        "auth.password": "<withheld>",
    }
    assert json.loads(meta["lines"]) == {"ingress.host": 2, "auth.password": 4}


def test_every_change_to_a_reference_or_a_named_file_equals_a_fresh_index(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    build(workspace, tmp_path, {"deployer": FILES})
    root = tmp_path / "repos"
    repo = root / "deployer"
    workflow = repo / ".github/workflows/deploy.yml"

    def step(tag: str) -> None:
        sync(workspace, workspace.resolver().resolve(repo))
        assert state(workspace) == fresh(settings, root, tmp_path, tag), tag

    # the named file edited
    (repo / "env/test/api.yaml").write_text("ingress:\n  host: api2.test.example\n")
    step("edited")
    assert json.loads(derived(workspace)["env/test/api.yaml"]["values"]) == {
        "ingress.host": "api2.test.example"
    }
    # the reference moved to another file
    workflow.write_text(WORKFLOW.replace("-f env/test/api.yaml", "-f env/test/unused.yaml"))
    step("moved")
    assert list(derived(workspace)) == ["env/test/unused.yaml"]
    # the named file renamed away: named, but no longer there
    shutil.move(repo / "env/test/unused.yaml", repo / "env/test/elsewhere.yaml")
    step("renamed")
    assert derived(workspace) == {}
    # the reference back, then removed
    workflow.write_text(WORKFLOW)
    step("restored")
    assert list(derived(workspace)) == ["env/test/api.yaml"]
    workflow.write_text(WORKFLOW.replace(" -f env/test/api.yaml", ""))
    step("removed")
    assert derived(workspace) == {}
    # the named file deleted while still named
    workflow.write_text(WORKFLOW)
    (repo / "env/test/api.yaml").unlink()
    step("deleted")
    assert derived(workspace) == {}


def test_a_file_named_only_inside_the_repository_is_read(
    workspace: Workspace, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.yaml"
    outside.write_text("ingress:\n  host: outside.example\n")
    files = {
        **FILES,
        ".github/workflows/deploy.yml": WORKFLOW.replace(
            "-f env/test/api.yaml", "-f env/test/link.yaml"
        ),
    }
    build(workspace, tmp_path, {"deployer": files})
    repo = tmp_path / "repos" / "deployer"
    (repo / "env/test/link.yaml").symlink_to(outside)
    sync(workspace, workspace.resolver().resolve(repo))
    assert "env/test/link.yaml" not in derived(workspace)
