"""P20: an edit to a Python project, then a sync, equals a fresh index of the same tree.

Each case edits :data:`tests.python_corpus.PYFIX`, syncs it incrementally, and holds the
result to a pristine database indexing the edited tree
(:func:`~tests.incremental_corpus.assert_equals_fresh`).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build, uses
from tests.incremental_corpus import append, assert_equals_fresh, edit, sync_all
from tests.python_corpus import PKG, PYFIX, refusals

R = PKG
REPO = "pyfix"


@pytest.fixture
def indexed(workspace: Workspace, tmp_path: Path) -> Workspace:
    return build(workspace, tmp_path, {REPO: PYFIX})


def _replace(root: Path, path: str, old: str, new: str) -> None:
    text = (root / REPO / path).read_text(encoding="utf-8")
    assert old in text, f"{old!r} not in {path}"
    edit(root, REPO, path, text.replace(old, new))


def _remove(root: Path, path: str) -> None:
    (root / REPO / path).unlink()


def _equal(workspace: Workspace, settings: Settings, tmp_path: Path, tag: str) -> None:
    assert_equals_fresh(workspace, settings, tmp_path / "repos", tmp_path, tag)


def test_a_body_edit_resolves_its_own_uses_and_those_landing_in_it(
    indexed: Workspace, settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "repos"
    # Lines move under every later declaration; no name the module binds changes.
    _replace(root, f"{R}/models.py", "    return f\n", "    print(f)\n    return f\n")
    work = sync_all(indexed, root)
    assert work.mode == "incremental"
    assert work.repository == 0, work
    assert work.readers == 0, work
    _equal(indexed, settings, tmp_path, "body")


def test_removing_a_re_export_reaches_its_readers(
    indexed: Workspace, settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "repos"
    _replace(root, f"{R}/__init__.py", "from .models import User as PublicUser\n", "")
    work = sync_all(indexed, root)
    assert work.readers > 0, work
    # `User` is still re-exported; `PublicUser` no longer is.
    assert (f"{R}/reexport_user.py#p5", f"{R}/models.py#User", "type") in uses(indexed)
    assert refusals(indexed, REPO, f"{R}/reexport_user.py") == {"PublicUser": "not-exported"}
    _equal(indexed, settings, tmp_path, "reexport")


def test_a_package_added_beside_a_module_of_its_name_takes_its_place(
    indexed: Workspace, settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "repos"
    edit(root, REPO, f"{R}/service/__init__.py", "def run():\n    return 2\n")
    sync_all(indexed, root)
    assert (f"{R}/mod_alias.py#p4", f"{R}/service/__init__.py#run", "call") in uses(indexed)
    _equal(indexed, settings, tmp_path, "package")


def test_deleting_a_module_removes_the_uses_landing_in_it(
    indexed: Workspace, settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "repos"
    _remove(root, f"{R}/service.py")
    sync_all(indexed, root)
    assert not [u for u in uses(indexed) if u[1].startswith(f"{R}/service.py")]
    _equal(indexed, settings, tmp_path, "delete")


def test_a_layout_change_in_the_project_file_resolves_the_whole_repository(
    indexed: Workspace, settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "repos"
    append(
        root, REPO, "pyproject.toml", '[tool.hatch.build.targets.wheel]\npackages = ["src/other"]\n'
    )
    work = sync_all(indexed, root)
    assert work.repository > 0, work
    assert not [u for u in uses(indexed) if u[0] == f"{R}/abs_user.py#p2"]
    _equal(indexed, settings, tmp_path, "layout")


def test_toggling_type_checking_and_shadowing(
    indexed: Workspace, settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "repos"
    _replace(
        root,
        f"{R}/checking.py",
        "if TYPE_CHECKING:\n    from fixture_pkg.models import User\n",
        "from fixture_pkg.models import User\n",
    )
    _replace(
        root,
        f"{R}/alias_user.py",
        "def p3(u: LocalUser):\n    return u\n",
        "def p3(LocalUser):\n    return LocalUser\n",
    )
    sync_all(indexed, root)
    assert not [u for u in uses(indexed) if u[0] == f"{R}/alias_user.py#p3"]
    _equal(indexed, settings, tmp_path, "toggle")


def test_a_nested_project_splits_the_package_and_rejoins_it(
    indexed: Workspace, settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "repos"
    edit(root, REPO, f"{R}/sub/pyproject.toml", '[project]\nname = "sub"\n')
    sync_all(indexed, root)
    assert not [u for u in uses(indexed) if u[0].startswith(f"{R}/sub/rel.py")]
    _equal(indexed, settings, tmp_path, "split")
    _remove(root, f"{R}/sub/pyproject.toml")
    sync_all(indexed, root)
    assert (f"{R}/sub/rel.py#p1", f"{R}/models.py#User", "type") in uses(indexed)
    _equal(indexed, settings, tmp_path, "rejoin")


def test_moving_a_star_import_after_a_binding_refuses_it(
    indexed: Workspace, settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "repos"
    edit(
        root,
        REPO,
        f"{R}/star_after.py",
        "from fixture_pkg.models import User\nfrom fixture_pkg.starpkg.a import *\n"
        "def p11a(u: User):\n    return u\n",
    )
    sync_all(indexed, root)
    assert not [u for u in uses(indexed) if u[0] == f"{R}/star_after.py#p11a"]
    _equal(indexed, settings, tmp_path, "star")
