"""Fixtures for the adversarial ecosystem (tests/adversarial/fixtures/GROUND_TRUTH.md).

Nine synthetic repositories with deliberate relationships *and* deliberate
misinformation. Ground truth is written by hand in `GROUND_TRUTH.md`; RepoAtlas
output never defines an expectation here.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.services.workspace import Workspace, open_workspace
from tests.conftest import init_git_repository

ADVERSARIAL_ROOT = Path(__file__).parent / "fixtures" / "repos"
GROUND_TRUTH = Path(__file__).parent / "fixtures" / "GROUND_TRUTH.md"

#: Every repository in the ecosystem. All are managed.
REPOSITORIES = (
    "python-service",
    "ts-service",
    "common-lib",
    "deployment",
    "scripts",
    "ruby-tooling",
    "ambiguous-provider-a",
    "ambiguous-provider-b",
    "unrelated-repo",
)

#: Sentinels planted inside fake secret files. None may appear downstream.
SECRET_SENTINEL = "REPOATLAS_FAKE_SECRET_SENTINEL_918273"  # noqa: S105 - a marker, not a secret

#: Markers inside content that ignore rules must keep out of the index.
IGNORED_MARKERS = (
    "IGNORED_MARKER_MUST_NOT_BE_INDEXED",
    "GITIGNORED_MARKER_MUST_NOT_BE_INDEXED",
    "VENDORED_MARKER_MUST_NOT_BE_INDEXED",
    "NODE_MODULES_MARKER_MUST_NOT_BE_INDEXED",
    "BUILD_MARKER_MUST_NOT_BE_INDEXED",
    "CACHE_MARKER_MUST_NOT_BE_INDEXED",
)

#: Identifiers that appear nowhere in the ecosystem, by construction.
ABSENT_IDENTIFIERS = (
    "ZZZ_NO_SUCH_TOKEN_918273",
    "nonexistent_function_918273",
    "@company/absent-918273",
)


#: Fixture files written when the ecosystem is materialized rather than stored
#: on disk. `python-service/.gitignore` excludes `gitignored/`, and git applies a
#: nested ignore file to the **enclosing** repository too — so a checked-in copy
#: would vanish from a fresh clone, and the test asserting that its marker never
#: reaches the index would then pass for the wrong reason.
GENERATED_FILES: dict[str, str] = {
    "python-service/gitignored/g.py": "GITIGNORED_MARKER_MUST_NOT_BE_INDEXED = 1\n",
}


def copy_ecosystem(source: Path, destination: Path) -> Path:
    """Materialize the ecosystem at ``destination`` with git history in each repo."""
    shutil.copytree(source, destination)
    for relative, content in GENERATED_FILES.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    for name in REPOSITORIES:
        init_git_repository(destination / name)
    return destination


@pytest.fixture
def adversarial_root(tmp_path: Path) -> Path:
    """A copy of the ecosystem with real git history in every repository."""
    return copy_ecosystem(ADVERSARIAL_ROOT, tmp_path / "adversarial-repos")


@pytest.fixture
def adversarial_settings(tmp_path: Path) -> Settings:
    """Deterministic embeddings; the real model is exercised separately."""
    return Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / "adv-state" / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 128},
        }
    )


@pytest.fixture
def adversarial(
    adversarial_settings: Settings, adversarial_root: Path, tmp_path: Path
) -> Iterator[Workspace]:
    """The ecosystem, discovered and fully indexed."""
    from repoatlas.services.init_service import initialize

    workspace = open_workspace(settings=adversarial_settings, require_root=False)
    initialize(workspace, adversarial_root, config_path=tmp_path / "adv-config.yaml")
    yield workspace
    workspace.close()
