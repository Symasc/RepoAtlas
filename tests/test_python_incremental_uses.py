"""P20: an edit to a Python project, then a sync, equals a fresh index of the same tree.

Each case edits :data:`tests.python_corpus.PYFIX`, or the projects across repositories of
:mod:`tests.python_packages_corpus`, syncs it incrementally, and holds the result to a
pristine database indexing the edited tree
(:func:`~tests.incremental_corpus.assert_equals_fresh`).
"""

from __future__ import annotations

import shutil
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
    use_edge_rows,
)
from tests.python_corpus import PKG, PYFIX, refusals
from tests.python_legacy_corpus import AUTH_MODELS, AUTH_PACKAGE, LEGACY, pick, service_uses
from tests.python_legacy_corpus import SERVICE as LEGACY_SERVICE
from tests.python_namespace_corpus import NAMESPACES
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


# -- the legacy convention: requirements naming a module a metadata-less repository holds --

LEGACY_MODELS = "auth-provider:fixture_auth/models.py"
AUTH_NAMES = ("User", "make")
#: A metadata-less repository holding other modules, and one nested deep inside it.
BYSTANDER = {"tools/__init__.py": "", "tools/cli.py": "def main():\n    return 0\n"}


@pytest.fixture
def legacy(workspace: Workspace, tmp_path: Path) -> Workspace:
    """``basic`` requires ``fixture-auth``, which ``auth-provider`` alone holds."""
    corpus = {**pick(LEGACY, "auth-provider", "basic"), "bystander": BYSTANDER}
    return build(workspace, tmp_path, corpus)


def _linked(workspace: Workspace) -> bool:
    return repository_uses(workspace, "basic") == service_uses(
        "basic", "auth-provider", "fixture_auth/models.py"
    )


def test_r17_r19_a_legacy_provider_appears_and_goes(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    linked = build(workspace, tmp_path, pick(LEGACY, "basic"))
    root = tmp_path / "repos"
    assert refusals(linked, "basic", LEGACY_SERVICE) == dict.fromkeys(
        AUTH_NAMES, "module-not-found"
    )
    add_repo(root, "auth-provider", AUTH_PACKAGE)
    assert sync_all(linked, root).mode == "incremental"
    assert _linked(linked)
    _equal(linked, settings, tmp_path, "provided")

    remove_repo(root, "auth-provider")
    sync_all(linked, root)
    assert refusals(linked, "basic", LEGACY_SERVICE) == dict.fromkeys(
        AUTH_NAMES, "module-not-found"
    )
    _equal(linked, settings, tmp_path, "unprovided")


def test_r19_a_legacy_package_renamed_and_back(
    legacy: Workspace, settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "repos"
    for path, text in AUTH_PACKAGE.items():
        edit(root, "auth-provider", path.replace("fixture_auth", "renamed_auth"), text)
        (root / "auth-provider" / path).unlink()
    sync_all(legacy, root)
    assert refusals(legacy, "basic", LEGACY_SERVICE) == dict.fromkeys(
        AUTH_NAMES, "module-not-found"
    )
    _equal(legacy, settings, tmp_path, "renamed")

    for path, text in AUTH_PACKAGE.items():
        edit(root, "auth-provider", path, text)
        (root / "auth-provider" / path.replace("fixture_auth", "renamed_auth")).unlink()
    sync_all(legacy, root)
    assert _linked(legacy)
    _equal(legacy, settings, tmp_path, "named")


def test_r18_a_second_legacy_provider_appears_and_goes(
    legacy: Workspace, settings: Settings, tmp_path: Path
) -> None:
    """In a repository of its own, and at the root of one already indexed."""
    root = tmp_path / "repos"
    add_repo(root, "auth-twin", AUTH_PACKAGE)
    sync_all(legacy, root)
    assert refusals(legacy, "basic", LEGACY_SERVICE) == dict.fromkeys(
        AUTH_NAMES, "provider-ambiguous"
    )
    _equal(legacy, settings, tmp_path, "twin")
    remove_repo(root, "auth-twin")
    sync_all(legacy, root)
    assert _linked(legacy)

    edit(root, "bystander", "fixture_auth/__init__.py", "")
    edit(root, "bystander", "fixture_auth/models.py", AUTH_MODELS)
    sync_all(legacy, root)
    assert refusals(legacy, "basic", LEGACY_SERVICE) == dict.fromkeys(
        AUTH_NAMES, "provider-ambiguous"
    )
    _equal(legacy, settings, tmp_path, "beside")
    for path in ("fixture_auth/__init__.py", "fixture_auth/models.py"):
        (root / "bystander" / path).unlink()
    sync_all(legacy, root)
    assert _linked(legacy)
    _equal(legacy, settings, tmp_path, "alone")


def test_r20_a_requirement_renamed_and_back(
    legacy: Workspace, settings: Settings, tmp_path: Path
) -> None:
    root = tmp_path / "repos"
    edit(root, "basic", "requirements.txt", "fixture-client\n")
    assert sync_all(legacy, root).mode == "incremental"
    assert refusals(legacy, "basic", LEGACY_SERVICE) == dict.fromkeys(
        AUTH_NAMES, "dependency-absent"
    )
    _equal(legacy, settings, tmp_path, "unrequired")
    edit(root, "basic", "requirements.txt", "requests\nFixture_Auth\n")
    sync_all(legacy, root)
    assert _linked(legacy)
    _equal(legacy, settings, tmp_path, "required")


@pytest.mark.parametrize(
    ("path", "text"),
    [
        ("setup.py", "from setuptools import setup\n\nsetup(name='auth')\n"),  # not metadata-less
        ("src/other/__init__.py", ""),  # R25: src/ becomes the root
    ],
)
def test_the_provider_s_root_changing_meaning_withdraws_the_link_and_restores_it(
    legacy: Workspace, settings: Settings, tmp_path: Path, path: str, text: str
) -> None:
    root = tmp_path / "repos"
    edit(root, "auth-provider", path, text)
    sync_all(legacy, root)
    assert refusals(legacy, "basic", LEGACY_SERVICE) == dict.fromkeys(
        AUTH_NAMES, "module-not-found"
    )
    _equal(legacy, settings, tmp_path, "rebased")
    (root / "auth-provider" / path).unlink()
    sync_all(legacy, root)
    assert _linked(legacy)
    _equal(legacy, settings, tmp_path, "restored")


def test_r21_a_project_shipping_the_module_appears_and_goes(
    legacy: Workspace, settings: Settings, tmp_path: Path
) -> None:
    """Under another distribution name, from ``src/``: the project decides, and refuses."""
    root = tmp_path / "repos"
    explicit = {
        "pyproject.toml": '[project]\nname = "auth-core"\n',
        **{f"src/{path}": text for path, text in AUTH_PACKAGE.items()},
    }
    add_repo(root, "auth-core", explicit)
    sync_all(legacy, root)
    assert refusals(legacy, "basic", LEGACY_SERVICE) == dict.fromkeys(
        AUTH_NAMES, "dependency-absent"
    )
    _equal(legacy, settings, tmp_path, "shipped")
    remove_repo(root, "auth-core")
    sync_all(legacy, root)
    assert _linked(legacy)
    _equal(legacy, settings, tmp_path, "unshipped")


def test_a_name_nested_below_a_top_level_head_resolves_no_requiring_repository_again(
    legacy: Workspace, settings: Settings, tmp_path: Path
) -> None:
    """``pkg/fixture_auth/`` gives ``pkg``, not ``fixture_auth``; ``fixture_auth/extra.py`` at
    a repository's root gives ``fixture_auth``, and resolves ``basic`` again."""
    root = tmp_path / "repos"
    before = use_edge_rows(legacy, LEGACY_SERVICE)
    assert before
    edit(root, "bystander", "pkg/fixture_auth/__init__.py", "")
    edit(root, "bystander", "pkg/fixture_auth/helpers.py", "def help():\n    return 1\n")
    work = sync_all(legacy, root)
    assert (work.mode, work.package) == ("incremental", 0)
    assert use_edge_rows(legacy, LEGACY_SERVICE) == before
    assert _linked(legacy)
    _equal(legacy, settings, tmp_path, "nested")

    edit(root, "auth-provider", "fixture_auth/extra.py", "def extra():\n    return 2\n")
    work = sync_all(legacy, root)
    assert work.package > 0
    after = use_edge_rows(legacy, LEGACY_SERVICE)
    assert [row[1:] for row in after] == [row[1:] for row in before]
    assert [row[0] for row in after] != [row[0] for row in before]
    _equal(legacy, settings, tmp_path, "top-level")


def test_a_tool_only_project_file_becoming_a_project_withdraws_the_link_and_back(
    legacy: Workspace, settings: Settings, tmp_path: Path
) -> None:
    """Configuration alone states no project: the link stands. Once the file states one, the
    project ships ``fixture_auth`` as another distribution, and the requirement names neither."""
    root = tmp_path / "repos"
    tool = "[tool.ruff]\nline-length = 100\n"
    edit(root, "auth-provider", "pyproject.toml", tool)
    sync_all(legacy, root)
    assert _linked(legacy)
    edit(root, "auth-provider", "pyproject.toml", f'[project]\nname = "auth-lib"\n{tool}')
    sync_all(legacy, root)
    assert refusals(legacy, "basic", LEGACY_SERVICE) == dict.fromkeys(
        AUTH_NAMES, "dependency-absent"
    )
    _equal(legacy, settings, tmp_path, "project")
    edit(root, "auth-provider", "pyproject.toml", tool)
    sync_all(legacy, root)
    assert _linked(legacy)
    _equal(legacy, settings, tmp_path, "configuration")


# -- setuptools metadata: a project's name and layout as setup.cfg / setup.py state them (P3b) --

CFG_FLAT = "[metadata]\nname = fixture-auth\n\n[options]\npackages = find:\n"
CFG_SRC = (
    "[metadata]\nname = fixture-auth\n\n[options]\npackage_dir =\n    = src\n"
    "packages = find:\n\n[options.packages.find]\nwhere = src\n"
)
SRC_PACKAGE = {f"src/{path}": text for path, text in AUTH_PACKAGE.items()}


def _reaches(workspace: Workspace, provider: str, models: str = "fixture_auth/models.py") -> bool:
    return repository_uses(workspace, "basic") == service_uses("basic", provider, models)


def test_s19_a_setup_cfg_name_and_layout_edited_and_back(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    """The layout edit changes no dependency row: only the project's facts say it moved."""
    provider = {"setup.cfg": CFG_FLAT, **AUTH_PACKAGE, **SRC_PACKAGE}
    linked = build(workspace, tmp_path, {**pick(LEGACY, "basic"), "cfg-provider": provider})
    root = tmp_path / "repos"
    assert _reaches(linked, "cfg-provider")
    edit(root, "cfg-provider", "setup.cfg", CFG_SRC)
    assert sync_all(linked, root).mode == "incremental"
    assert _reaches(linked, "cfg-provider", "src/fixture_auth/models.py")
    _equal(linked, settings, tmp_path, "src")
    edit(root, "cfg-provider", "setup.cfg", CFG_FLAT.replace("fixture-auth", "fixture-renamed"))
    sync_all(linked, root)
    assert refusals(linked, "basic", LEGACY_SERVICE) == dict.fromkeys(
        AUTH_NAMES, "dependency-absent"
    )
    _equal(linked, settings, tmp_path, "renamed")
    edit(root, "cfg-provider", "setup.cfg", CFG_FLAT)
    sync_all(linked, root)
    assert _reaches(linked, "cfg-provider")
    _equal(linked, settings, tmp_path, "flat")


def test_s19_a_setup_py_name_computed_and_back(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    static = (
        'from setuptools import setup\n\nsetup(name="fixture-auth", packages=["fixture_auth"])\n'
    )
    provider = {"setup.py": static, **AUTH_PACKAGE}
    linked = build(workspace, tmp_path, {**pick(LEGACY, "basic"), "py-provider": provider})
    root = tmp_path / "repos"
    assert _reaches(linked, "py-provider")
    edit(root, "py-provider", "setup.py", static.replace('"fixture-auth"', "compute()"))
    sync_all(linked, root)
    assert refusals(linked, "basic", LEGACY_SERVICE) == dict.fromkeys(
        AUTH_NAMES, "provider-unnamed"
    )
    _equal(linked, settings, tmp_path, "computed")
    edit(root, "py-provider", "setup.py", static)
    sync_all(linked, root)
    assert _reaches(linked, "py-provider")
    _equal(linked, settings, tmp_path, "static")


def test_s20_a_metadata_less_provider_gains_setup_cfg_and_loses_it(
    legacy: Workspace, settings: Settings, tmp_path: Path
) -> None:
    """The legacy link becomes a named one, and back: the requirement is the same row."""
    root = tmp_path / "repos"
    [before] = [
        e
        for e in edges(legacy)
        if e["source"] == f"basic:{LEGACY_SERVICE}#load" and e["use"] == "call"
    ]
    assert before["basis"] == "python-requirements-legacy"
    edit(root, "auth-provider", "setup.cfg", CFG_FLAT)
    sync_all(legacy, root)
    [after] = [
        e
        for e in edges(legacy)
        if e["source"] == f"basic:{LEGACY_SERVICE}#load" and e["use"] == "call"
    ]
    assert (after["basis"], after["crossing"]["provided_at"]) == (
        "python-dependency",
        "setup.cfg:2",
    )
    _equal(legacy, settings, tmp_path, "explicit")
    (root / "auth-provider" / "setup.cfg").unlink()
    sync_all(legacy, root)
    assert _linked(legacy)
    _equal(legacy, settings, tmp_path, "metadata-less")


def test_s20_a_build_system_pyproject_gains_its_setup_cfg_and_loses_it(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    build_system = (
        '[build-system]\nrequires = ["setuptools"]\nbuild-backend = "setuptools.build_meta"\n'
    )
    provider = {"pyproject.toml": build_system, **SRC_PACKAGE}
    linked = build(workspace, tmp_path, {**pick(LEGACY, "basic"), "bs-provider": provider})
    root = tmp_path / "repos"
    assert refusals(linked, "basic", LEGACY_SERVICE) == dict.fromkeys(
        AUTH_NAMES, "provider-unnamed"
    )
    edit(root, "bs-provider", "setup.cfg", CFG_SRC)
    sync_all(linked, root)
    assert _reaches(linked, "bs-provider", "src/fixture_auth/models.py")
    _equal(linked, settings, tmp_path, "completed")
    (root / "bs-provider" / "setup.cfg").unlink()
    sync_all(linked, root)
    assert refusals(linked, "basic", LEGACY_SERVICE) == dict.fromkeys(
        AUTH_NAMES, "provider-unnamed"
    )
    _equal(linked, settings, tmp_path, "unnamed")


def test_s20_a_second_explicit_provider_appears_and_goes(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    provider = {"setup.cfg": CFG_FLAT, **AUTH_PACKAGE}
    linked = build(workspace, tmp_path, {**pick(LEGACY, "basic"), "cfg-provider": provider})
    root = tmp_path / "repos"
    add_repo(root, "cfg-twin", provider)
    sync_all(linked, root)
    assert refusals(linked, "basic", LEGACY_SERVICE) == dict.fromkeys(
        AUTH_NAMES, "provider-ambiguous"
    )
    _equal(linked, settings, tmp_path, "twins")
    remove_repo(root, "cfg-twin")
    sync_all(linked, root)
    assert _reaches(linked, "cfg-provider")
    _equal(linked, settings, tmp_path, "single")


LIB_CFG = (
    "[metadata]\nname = auth-core\n\n[options]\npackage_dir =\n    = lib\n"
    "packages = find:\npy_modules = lib_tool\n\n[options.packages.find]\nwhere = lib\n"
)


def test_a_layout_rooted_elsewhere_gaining_the_module_withdraws_a_legacy_link_and_back(
    legacy: Workspace, settings: Settings, tmp_path: Path
) -> None:
    """A project named otherwise starts shipping fixture_auth from its own lib/: every
    repository requiring it by the legacy key is resolved again (P3a R21, below a root
    only a setuptools layout states)."""
    root = tmp_path / "repos"
    add_repo(
        root,
        "lib-core",
        {"setup.cfg": LIB_CFG, "lib/other_pkg/__init__.py": "", "lib/lib_tool.py": ""},
    )
    sync_all(legacy, root)
    assert _linked(legacy)
    for path, text in AUTH_PACKAGE.items():
        edit(root, "lib-core", f"lib/{path}", text)
    sync_all(legacy, root)
    assert refusals(legacy, "basic", LEGACY_SERVICE) == dict.fromkeys(
        AUTH_NAMES, "dependency-absent"
    )
    _equal(legacy, settings, tmp_path, "shipped")
    shutil.rmtree(root / "lib-core" / "lib" / "fixture_auth")
    sync_all(legacy, root)
    assert _linked(legacy)
    _equal(legacy, settings, tmp_path, "unshipped")


def test_a_py_modules_edit_below_a_stated_root_withdraws_a_legacy_link_and_back(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    """Only the facts change: lib/fixture_auth.py is there all along, and py_modules starts
    and stops naming it."""
    corpus = {
        **pick(LEGACY, "auth-provider", "basic"),
        "lib-core": {"setup.cfg": LIB_CFG, "lib/lib_tool.py": "", "lib/fixture_auth.py": "x = 1\n"},
    }
    linked = build(workspace, tmp_path, corpus)
    root = tmp_path / "repos"
    assert _linked(linked)
    edit(root, "lib-core", "setup.cfg", LIB_CFG.replace("lib_tool", "lib_tool, fixture_auth"))
    sync_all(linked, root)
    assert not _linked(linked)
    _equal(linked, settings, tmp_path, "named")
    edit(root, "lib-core", "setup.cfg", LIB_CFG)
    sync_all(linked, root)
    assert _linked(linked)
    _equal(linked, settings, tmp_path, "unnamed")


def test_a_setuptools_provider_body_edit_resolves_no_other_repository(
    workspace: Workspace, tmp_path: Path
) -> None:
    provider = {"setup.cfg": CFG_FLAT, **AUTH_PACKAGE}
    linked = build(workspace, tmp_path, {**pick(LEGACY, "basic"), "cfg-provider": provider})
    root = tmp_path / "repos"
    append(root, "cfg-provider", "fixture_auth/models.py", "\n\ndef extra() -> None:\n    pass\n")
    work = sync_all(linked, root)
    assert (work.repository, work.package) == (0, 0)
    edit(root, "cfg-provider", "setup.cfg", CFG_FLAT + "\n[flake8]\nmax-line-length = 100\n")
    work = sync_all(linked, root)
    assert (work.repository, work.package) == (0, 0)


# -- P6 C1: the layouts a pyproject states for itself ------------------------------------

FIND = (
    '[project]\nname = "fixture-auth"\nversion = "1.0"\n\n'
    '[build-system]\nbuild-backend = "setuptools.build_meta"\n\n'
    '[tool.setuptools.packages.find]\nwhere = ["."]\ninclude = ["fixture_auth*"]\n'
)
ONLY_INCLUDE = (
    '[project]\nname = "fixture-auth"\nversion = "1.0"\n\n'
    '[build-system]\nbuild-backend = "hatchling.build"\n\n'
    '[tool.hatch.build]\nonly-include = ["fixture_auth"]\n'
)


def test_n2_a_find_include_and_where_edited_and_back(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    """Only the project's facts change: the module it ships goes, moves to src/, and back."""
    provider = {"pyproject.toml": FIND, **AUTH_PACKAGE, **SRC_PACKAGE}
    linked = build(workspace, tmp_path, {**pick(LEGACY, "basic"), "find-provider": provider})
    root = tmp_path / "repos"
    assert _reaches(linked, "find-provider")
    edit(root, "find-provider", "pyproject.toml", FIND.replace("fixture_auth*", "other*"))
    assert sync_all(linked, root).mode == "incremental"
    assert refusals(linked, "basic", LEGACY_SERVICE) == dict.fromkeys(
        AUTH_NAMES, "module-not-found"
    )
    _equal(linked, settings, tmp_path, "excluded")
    edit(root, "find-provider", "pyproject.toml", FIND.replace('where = ["."]', 'where = ["src"]'))
    sync_all(linked, root)
    assert _reaches(linked, "find-provider", "src/fixture_auth/models.py")
    _equal(linked, settings, tmp_path, "src")
    edit(root, "find-provider", "pyproject.toml", FIND)
    sync_all(linked, root)
    assert _reaches(linked, "find-provider")
    _equal(linked, settings, tmp_path, "flat")


def test_n2_an_only_include_becoming_readable_and_its_package_removed_and_back(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    unread = ONLY_INCLUDE + 'exclude = ["fixture_auth/cache.py"]\n'
    provider = {"pyproject.toml": unread, **AUTH_PACKAGE}
    linked = build(workspace, tmp_path, {**pick(LEGACY, "basic"), "only-provider": provider})
    root = tmp_path / "repos"
    assert refusals(linked, "basic", LEGACY_SERVICE) == dict.fromkeys(
        AUTH_NAMES, "module-not-found"
    )
    edit(root, "only-provider", "pyproject.toml", ONLY_INCLUDE)
    sync_all(linked, root)
    assert _reaches(linked, "only-provider")
    _equal(linked, settings, tmp_path, "read")
    shutil.rmtree(root / "only-provider" / "fixture_auth")
    sync_all(linked, root)
    assert refusals(linked, "basic", LEGACY_SERVICE) == dict.fromkeys(
        AUTH_NAMES, "module-not-found"
    )
    _equal(linked, settings, tmp_path, "gone")
    for path, text in AUTH_PACKAGE.items():
        edit(root, "only-provider", path, text)
    sync_all(linked, root)
    assert _reaches(linked, "only-provider")
    _equal(linked, settings, tmp_path, "back")


def test_n2_an_only_include_starting_to_ship_the_module_withdraws_a_legacy_link_and_back(
    legacy: Workspace, settings: Settings, tmp_path: Path
) -> None:
    """A project named otherwise starts naming fixture_auth in only-include: only its facts
    change, and every repository requiring it by the legacy key is resolved again."""
    root = tmp_path / "repos"
    core = ONLY_INCLUDE.replace("fixture-auth", "auth-core").replace('"fixture_auth"', '"other"')
    add_repo(root, "hatch-core", {"pyproject.toml": core, "other/__init__.py": "", **AUTH_PACKAGE})
    sync_all(legacy, root)
    assert _linked(legacy)
    edit(
        root, "hatch-core", "pyproject.toml", core.replace('["other"]', '["other", "fixture_auth"]')
    )
    sync_all(legacy, root)
    assert refusals(legacy, "basic", LEGACY_SERVICE) == dict.fromkeys(
        AUTH_NAMES, "dependency-absent"
    )
    _equal(legacy, settings, tmp_path, "named")
    edit(root, "hatch-core", "pyproject.toml", core)
    sync_all(legacy, root)
    assert _linked(legacy)
    _equal(legacy, settings, tmp_path, "unnamed")


def test_n2_a_pyproject_find_rooted_elsewhere_starting_to_ship_withdraws_a_legacy_link_and_back(
    legacy: Workspace, settings: Settings, tmp_path: Path
) -> None:
    """The same below a root only the pyproject's own [tool.setuptools] states: lib/."""
    root = tmp_path / "repos"
    core = FIND.replace("fixture-auth", "auth-core").replace('["."]', '["lib"]')
    closed = core.replace("fixture_auth*", "other*")
    add_repo(
        root,
        "find-core",
        {
            "pyproject.toml": closed,
            "lib/other/__init__.py": "",
            **{f"lib/{path}": text for path, text in AUTH_PACKAGE.items()},
        },
    )
    sync_all(legacy, root)
    assert _linked(legacy)
    edit(root, "find-core", "pyproject.toml", core)
    sync_all(legacy, root)
    assert refusals(legacy, "basic", LEGACY_SERVICE) == dict.fromkeys(
        AUTH_NAMES, "dependency-absent"
    )
    _equal(legacy, settings, tmp_path, "found")
    edit(root, "find-core", "pyproject.toml", closed)
    sync_all(legacy, root)
    assert _linked(legacy)
    _equal(legacy, settings, tmp_path, "excluded")


def test_n2_a_body_edit_in_a_pyproject_layout_provider_resolves_no_other_repository(
    workspace: Workspace, tmp_path: Path
) -> None:
    provider = {"pyproject.toml": ONLY_INCLUDE, **AUTH_PACKAGE}
    linked = build(workspace, tmp_path, {**pick(LEGACY, "basic"), "only-provider": provider})
    root = tmp_path / "repos"
    append(root, "only-provider", "fixture_auth/models.py", "\n\ndef extra() -> None:\n    pass\n")
    work = sync_all(linked, root)
    assert (work.repository, work.package) == (0, 0)


# -- P6 N1: namespace portions ------------------------------------------------------------

HA_TEST = "ha:tests/test_const.py#test_domain"
HA_CONST = "ha:custom_components/fixture_ha/const.py#domain"


def _through(workspace: Workspace, source: str) -> list[tuple[str, object]]:
    """Where ``source``'s uses land, and the namespace each was found through."""
    return sorted(
        (e["target"], e.get("namespace_package")) for e in edges(workspace) if e["source"] == source
    )


def test_n1_a_top_level_portion_becoming_a_regular_package_and_back(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    """The same module, reached with the namespace evidence and without it."""
    linked = build(workspace, tmp_path, pick(NAMESPACES, "ha"))
    root = tmp_path / "repos"
    assert _through(linked, HA_TEST) == [(HA_CONST, "custom_components")]
    edit(root, "ha", "custom_components/__init__.py", "")
    sync_all(linked, root)
    assert _through(linked, HA_TEST) == [(HA_CONST, None)]
    _equal(linked, settings, tmp_path, "regular")
    (root / "ha" / "custom_components" / "__init__.py").unlink()
    sync_all(linked, root)
    assert _through(linked, HA_TEST) == [(HA_CONST, "custom_components")]
    _equal(linked, settings, tmp_path, "portion")


def test_n1_a_module_below_a_portion_deleted_and_restored(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    linked = build(workspace, tmp_path, pick(NAMESPACES, "ha"))
    root = tmp_path / "repos"
    const = NAMESPACES["ha"]["custom_components/fixture_ha/const.py"]
    (root / "ha" / "custom_components" / "fixture_ha" / "const.py").unlink()
    sync_all(linked, root)
    assert _through(linked, HA_TEST) == []
    assert refusals(linked, "ha", "tests/test_const.py") == dict.fromkeys(
        ("domain", "other"), "namespace-package"
    )
    _equal(linked, settings, tmp_path, "deleted")
    edit(root, "ha", "custom_components/fixture_ha/const.py", const)
    sync_all(linked, root)
    assert _through(linked, HA_TEST) == [(HA_CONST, "custom_components")]
    _equal(linked, settings, tmp_path, "restored")


def test_n1_a_second_portion_holding_the_module_makes_it_ambiguous_and_back(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    module = "def a() -> None:\n    pass\n"
    project = {
        "pyproject.toml": (
            '[project]\nname = "p"\n\n'
            '[tool.hatch.build.targets.wheel]\npackages = ["lib1/ns", "lib2/ns"]\n'
        ),
        "lib1/ns/a.py": module,
        "app.py": "from ns.a import a\n\n\ndef run() -> None:\n    a()\n",
    }
    linked = build(workspace, tmp_path, {"two-roots": project})
    root = tmp_path / "repos"
    assert _through(linked, "two-roots:app.py#run") == [("two-roots:lib1/ns/a.py#a", "ns")]
    edit(root, "two-roots", "lib2/ns/a.py", module)
    sync_all(linked, root)
    assert refusals(linked, "two-roots", "app.py") == {"a": "module-ambiguous"}
    _equal(linked, settings, tmp_path, "ambiguous")
    (root / "two-roots" / "lib1" / "ns" / "a.py").unlink()
    sync_all(linked, root)
    assert _through(linked, "two-roots:app.py#run") == [("two-roots:lib2/ns/a.py#a", "ns")]
    _equal(linked, settings, tmp_path, "moved")
