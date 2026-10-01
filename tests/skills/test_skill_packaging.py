"""§29: the skills ship with the thing people install, from one maintained copy.

A skill that exists only in a git checkout is guidance that a user who ran
``pip install repoatlas`` does not have, and its absence is silent: the agent
simply behaves like an agent with no guidance, which looks like a weak model
rather than a missing file.

The canonical skills are at the repository root, where they are vendor-neutral
and where a reader looks for them. The wheel maps those same files into the
installed package instead of keeping second ones, because two hand-maintained copies of
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
from tests.skills.conftest import SKILL_NAMES, SKILL_PATH, skill_paths

ROOT = SKILL_PATH.parent.parent.parent

#: Where the artifact keeps each skill once installed.
INSTALLED_PATHS = {name: f"repoatlas/skills/{name}/SKILL.md" for name in SKILL_NAMES}

#: Where each lives in a source distribution, which mirrors the repository.
SOURCE_PATHS = {name: f"skills/{name}/SKILL.md" for name in SKILL_NAMES}

#: A task skill stays small enough to be read whole whenever it loads. The shared
#: contract is the place for detail.
MAX_SKILL_BYTES = 6_500

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
def test_the_wheel_carries_every_canonical_skill(built: Path) -> None:
    wheels = list(built.glob("*.whl"))
    assert len(wheels) == 1, wheels
    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
        for name, installed in INSTALLED_PATHS.items():
            assert installed in names, [n for n in names if "skill" in n.lower()]
            shipped = archive.read(installed).decode("utf-8")
            assert shipped == skill_paths()[name].read_text(encoding="utf-8"), (
                f"the installed {name} is not the canonical one"
            )


@requires_uv
def test_the_source_distribution_carries_every_canonical_skill(built: Path) -> None:
    archives = list(built.glob("*.tar.gz"))
    assert len(archives) == 1, archives
    with tarfile.open(archives[0]) as archive:
        for name, source in SOURCE_PATHS.items():
            matches = [member for member in archive.getnames() if member.endswith(source)]
            assert len(matches) == 1, archive.getnames()[:20]
            handle = archive.extractfile(matches[0])
            assert handle is not None
            shipped = handle.read().decode("utf-8")
            assert shipped == skill_paths()[name].read_text(encoding="utf-8"), name


@requires_uv
def test_the_artifact_holds_exactly_the_five_skill_documents(built: Path) -> None:
    """One copy of each in the wheel, so there is nothing for a second one to drift from."""
    with zipfile.ZipFile(next(built.glob("*.whl"))) as archive:
        found = sorted(name for name in archive.namelist() if name.endswith("SKILL.md"))
    assert found == sorted(INSTALLED_PATHS.values()), found


def test_the_repository_holds_exactly_the_five_skill_documents() -> None:
    found = sorted(
        path.relative_to(ROOT).as_posix()
        for path in ROOT.rglob("SKILL.md")
        # installed packages are not this repository's guidance (Playwright ships skills)
        if not {".venv", "dist", "node_modules"} & set(path.parts)
    )
    assert found == sorted(SOURCE_PATHS.values()), found


def test_every_task_skill_is_small() -> None:
    for name, path in skill_paths().items():
        size = len(path.read_bytes())
        assert size <= MAX_SKILL_BYTES, f"{name} is {size} bytes"


def test_every_skill_links_the_shared_contract_where_it_is_installed() -> None:
    """The sibling link resolves in the repository, and therefore in the wheel and in
    any install that keeps the five skills side by side, as the wheel does."""
    link = "../repoatlas-code-trace/references/contracts.md"
    for name, path in skill_paths().items():
        assert link in path.read_text(encoding="utf-8"), name
        assert (path.parent / link).resolve().is_file(), name


def test_core_guidance_is_small_and_its_optional_reference_exists() -> None:
    core = SKILL_PATH.read_text(encoding="utf-8")
    assert len(core.encode()) <= 12_000
    assert "references/contracts.md" in core
    assert (SKILL_PATH.parent / "references/contracts.md").is_file()


@requires_uv
def test_optional_contract_reference_ships_with_the_wheel(built: Path) -> None:
    path = "repoatlas/skills/repoatlas-code-trace/references/contracts.md"
    with zipfile.ZipFile(next(built.glob("*.whl"))) as archive:
        assert (
            archive.read(path).decode()
            == (SKILL_PATH.parent / "references/contracts.md").read_text()
        )
