"""§29: the skill ships with the thing people install, from one maintained copy.

A skill that exists only in a git checkout is guidance that a user who ran
``pip install repoatlas`` does not have, and its absence is silent: the agent
simply behaves like an agent with no guidance, which looks like a weak model
rather than a missing file.

The canonical artifact is at the repository root, where it is vendor-neutral and
where a reader looks for it. The wheel maps that same file into the installed
package instead of keeping a second one, because two hand-maintained copies of
behavioural guidance is precisely how the two come to disagree. These tests build
the real artifacts with the project's own packaging command and read what is
inside them.
"""

from __future__ import annotations

import shutil
import subprocess
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest
from tests.skills.conftest import SKILL_PATH

ROOT = SKILL_PATH.parent.parent.parent

#: Where the artifact keeps the skill once installed.
INSTALLED_PATH = "repoatlas/skills/repoatlas-investigation/SKILL.md"

#: Where it lives in a source distribution, which mirrors the repository.
SOURCE_PATH = "skills/repoatlas-investigation/SKILL.md"

requires_uv = pytest.mark.skipif(
    shutil.which("uv") is None, reason="uv is the project's packaging command and is not installed"
)


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The real wheel and sdist, built the way a release would build them."""
    out = tmp_path_factory.mktemp("dist")
    subprocess.run(
        ["uv", "build", "--out-dir", str(out)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return out


def test_the_packaging_configuration_maps_the_canonical_skill_in() -> None:
    """Read before building, so a misconfiguration names itself.

    A build failure says "the file is not in the wheel". This says which setting
    was supposed to put it there, which is the thing somebody has to fix.
    """
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    wheel = config["tool"]["hatch"]["build"]["targets"]["wheel"]
    # The whole mapping, not just the skill's entry: a force-include is how a tree
    # nobody meant to publish gets into the wheel. The only other entry is the
    # grammar provenance notice (OFF-01).
    assert wheel["force-include"] == {
        "skills": "repoatlas/skills",
        "THIRD_PARTY_NOTICES.md": "repoatlas/THIRD_PARTY_NOTICES.md",
    }


@requires_uv
def test_the_wheel_carries_the_canonical_skill(built: Path) -> None:
    wheels = list(built.glob("*.whl"))
    assert len(wheels) == 1, wheels
    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
        assert INSTALLED_PATH in names, [n for n in names if "skill" in n.lower()]
        shipped = archive.read(INSTALLED_PATH).decode("utf-8")
    assert shipped == SKILL_PATH.read_text(encoding="utf-8"), (
        "the installed skill is not the canonical one"
    )


@requires_uv
def test_the_source_distribution_carries_the_canonical_skill(built: Path) -> None:
    archives = list(built.glob("*.tar.gz"))
    assert len(archives) == 1, archives
    with tarfile.open(archives[0]) as archive:
        matches = [name for name in archive.getnames() if name.endswith(SOURCE_PATH)]
        assert len(matches) == 1, archive.getnames()[:20]
        handle = archive.extractfile(matches[0])
        assert handle is not None
        shipped = handle.read().decode("utf-8")
    assert shipped == SKILL_PATH.read_text(encoding="utf-8")


@requires_uv
def test_the_artifact_holds_exactly_one_skill_document(built: Path) -> None:
    """One copy in the wheel, so there is nothing for a second one to drift from."""
    with zipfile.ZipFile(next(built.glob("*.whl"))) as archive:
        found = [name for name in archive.namelist() if name.endswith("SKILL.md")]
    assert found == [INSTALLED_PATH], found


def test_the_repository_holds_exactly_one_skill_document() -> None:
    found = sorted(
        path.relative_to(ROOT).as_posix()
        for path in ROOT.rglob("SKILL.md")
        # installed packages are not this repository's guidance (Playwright ships skills)
        if not {".venv", "dist", "node_modules"} & set(path.parts)
    )
    assert found == [SOURCE_PATH], found


def test_core_guidance_is_small_and_its_optional_reference_exists() -> None:
    core = SKILL_PATH.read_text(encoding="utf-8")
    assert len(core.encode()) <= 12_000
    assert "references/contracts.md" in core
    assert (SKILL_PATH.parent / "references/contracts.md").is_file()


@requires_uv
def test_optional_contract_reference_ships_with_the_wheel(built: Path) -> None:
    path = "repoatlas/skills/repoatlas-investigation/references/contracts.md"
    with zipfile.ZipFile(next(built.glob("*.whl"))) as archive:
        assert (
            archive.read(path).decode()
            == (SKILL_PATH.parent / "references/contracts.md").read_text()
        )
