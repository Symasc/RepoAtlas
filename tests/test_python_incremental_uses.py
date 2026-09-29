"""P20: an edit to a Python project, then a sync, equals a fresh index of the same tree.

Each case edits :data:`tests.python_corpus.PYFIX`, or the projects across repositories of
:mod:`tests.python_packages_corpus`, syncs it incrementally, and holds the result to a
pristine database indexing the edited tree
(:func:`~tests.incremental_corpus.assert_equals_fresh`).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build, uses
from tests.incremental_corpus import (
    add_repo,
    append,
    assert_equals_fresh,
    edit,
    remove_repo,
    sync_all,
)
from tests.python_corpus import PKG, PYFIX, refusals
from tests.python_packages_corpus import (
    SERVICE,
    TWINS,
    UVWS,
    XREPO,
    consumer_uses,
    edges,
)
from tests.python_packages_corpus import uses as repository_uses

R = PKG
REPO = "pyfix"
#: What ``internal-fixture``'s consumers use of it, by target key.
IMPORTED = ("Widget", "PublicWidget", "make", "models.make")


@pytest.fixture
def indexed(workspace: Workspace, tmp_path: Path) -> Workspace:
    return build(workspace, tmp_path, {REPO: PYFIX})


@pytest.fixture
def linked(workspace: Workspace, tmp_path: Path) -> Workspace:
    """A provider, an undeclared shadow of its module, and two repositories declaring it."""
    names = ("provider", "shadow", "consumer", "pinned")
    return build(workspace, tmp_path, {name: XREPO[name] for name in names})


def _replace(root: Path, path: str, old: str, new: str, repo: str = REPO) -> None:
    text = (root / repo / path).read_text(encoding="utf-8")
    assert old in text, f"{old!r} not in {path}"
    edit(root, repo, path, text.replace(old, new))


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


def test_a_provider_renamed_and_back_reaches_every_repository_declaring_it(
    linked: Workspace, settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "repos"
    named, renamed = 'name = "internal-fixture"', 'name = "internal-fixture-renamed"'
    _replace(root, "pyproject.toml", named, renamed, repo="provider")
    work = sync_all(linked, root)
    assert (work.mode, work.packages_invalidated) == ("incremental", 2)
    assert refusals(linked, "consumer", SERVICE) == dict.fromkeys(IMPORTED, "dependency-absent")
    assert not [
        e
        for e in edges(linked)
        if e["target"].startswith("provider:") and not e["source"].startswith("provider:")
    ]
    _equal(linked, settings, tmp_path, "renamed")

    _replace(root, "pyproject.toml", renamed, named, repo="provider")
    sync_all(linked, root)
    for repository in ("consumer", "pinned"):
        assert repository_uses(linked, repository) == consumer_uses(repository)
    _equal(linked, settings, tmp_path, "named")


def test_a_second_provider_appears_and_goes(
    linked: Workspace, settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "repos"
    add_repo(root, "twin", TWINS["twin"])
    work = sync_all(linked, root)
    assert (work.mode, work.packages_invalidated) == ("incremental", 1)
    assert refusals(linked, "consumer", SERVICE) == dict.fromkeys(IMPORTED, "provider-ambiguous")
    # a pin names its provider whoever else provides the name
    assert repository_uses(linked, "pinned") == consumer_uses("pinned")
    _equal(linked, settings, tmp_path, "twin")

    remove_repo(root, "twin")
    sync_all(linked, root)
    assert repository_uses(linked, "consumer") == consumer_uses("consumer")
    _equal(linked, settings, tmp_path, "untwinned")


def test_a_uv_source_retargeted_and_back(
    linked: Workspace, settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "repos"
    _replace(root, "pyproject.toml", "../provider", "../shadow", repo="pinned")
    sync_all(linked, root)
    assert refusals(linked, "pinned", SERVICE) == dict.fromkeys(IMPORTED, "dependency-elsewhere")
    _equal(linked, settings, tmp_path, "retargeted")

    _replace(root, "pyproject.toml", "../shadow", "../provider", repo="pinned")
    sync_all(linked, root)
    assert repository_uses(linked, "pinned") == consumer_uses("pinned")
    _equal(linked, settings, tmp_path, "pinned")


def test_a_declaration_removed_takes_its_uses_with_it(
    linked: Workspace, settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "repos"
    _replace(root, "pyproject.toml", '"internal-fixture>=1.0"', "", repo="consumer")
    work = sync_all(linked, root)
    assert work.mode == "incremental"
    assert refusals(linked, "consumer", SERVICE) == dict.fromkeys(IMPORTED, "dependency-absent")
    _equal(linked, settings, tmp_path, "undeclared")


def test_a_workspace_source_removed_leaves_its_members_to_the_version_rule(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    """Which finds the second `lib` outside the workspace too, and joins neither."""
    uvws = build(workspace, tmp_path, {"uvws": UVWS})
    root = tmp_path / "repos"
    source = "[tool.uv.sources]\nlib = { workspace = true }\n"
    _replace(root, "pyproject.toml", source, "", repo="uvws")
    sync_all(uvws, root)
    app = "packages/app/src/app/__init__.py"
    assert refusals(uvws, "uvws", app) == {"helper": "provider-ambiguous"}
    assert not [e for e in edges(uvws) if e["source"] == f"uvws:{app}#run"]
    _equal(uvws, settings, tmp_path, "unsourced")
