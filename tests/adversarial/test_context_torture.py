"""Directory-context resolution, pushed hard (V1_SPEC §6, ARCHITECTURE §7).

A wrong scope is worse than an error: it answers a question against an arbitrary
subset of repositories and says nothing about having done so. So the assertions
here are mostly negative — *no command may broaden its scope*, and every
unresolvable directory must produce exit 2 rather than a guess.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.errors import ExitCode
from repoatlas.models.context import ContextScope
from repoatlas.services.workspace import Workspace
from tests.adversarial.conftest import REPOSITORIES
from tests.conftest import init_git_repository

pytestmark = pytest.mark.adversarial


def resolve(workspace: Workspace, cwd: Path, explicit: str | None = None):
    return workspace.resolver().resolve(cwd, explicit)


def scope_of(workspace: Workspace, cwd: Path, explicit: str | None = None):
    context = resolve(workspace, cwd, explicit)
    return context.scope, (context.primary.name if context.primary else None)


# -- the two valid implicit contexts -----------------------------------------


def test_the_configured_root_is_global(adversarial: Workspace, adversarial_root: Path) -> None:
    scope, primary = scope_of(adversarial, adversarial_root)
    assert scope is ContextScope.GLOBAL
    assert primary is None


@pytest.mark.parametrize("name", REPOSITORIES)
def test_every_repository_root_resolves_to_itself(
    adversarial: Workspace, adversarial_root: Path, name: str
) -> None:
    assert scope_of(adversarial, adversarial_root / name) == (ContextScope.REPOSITORY, name)


@pytest.mark.parametrize(
    "relative",
    [
        "src",
        "src/deep",
        "src/deep/a/b/c",
        "src/deep/a/b/c/d/e/f/g/h",
    ],
)
def test_depth_never_changes_the_answer(
    adversarial: Workspace, adversarial_root: Path, relative: str
) -> None:
    """Ten directories deep is the same repository as the root of it (§6)."""
    cwd = adversarial_root / "python-service" / relative
    assert scope_of(adversarial, cwd) == (ContextScope.REPOSITORY, "python-service")


def test_a_directory_under_the_root_but_in_no_repository_is_an_error(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """There is deliberately no subtree scope: proximity is not a scope (§6)."""
    from repoatlas.errors import RepoAtlasError

    stray = adversarial_root / "not-a-repository"
    stray.mkdir()
    with pytest.raises(RepoAtlasError) as raised:
        resolve(adversarial, stray)
    assert raised.value.exit_code == ExitCode.CONTEXT


# -- path shapes -------------------------------------------------------------


def test_a_path_with_spaces_resolves(adversarial: Workspace, adversarial_root: Path) -> None:
    spaced = adversarial_root / "python-service" / "a directory with spaces"
    spaced.mkdir()
    assert scope_of(adversarial, spaced) == (ContextScope.REPOSITORY, "python-service")


def test_a_trailing_slash_changes_nothing(adversarial: Workspace, adversarial_root: Path) -> None:
    assert scope_of(adversarial, Path(f"{adversarial_root / 'scripts'}/")) == (
        ContextScope.REPOSITORY,
        "scripts",
    )


def test_dot_and_dotdot_resolve_like_the_directory_they_name(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    inner = adversarial_root / "python-service" / "src"
    assert scope_of(adversarial, inner / ".") == (ContextScope.REPOSITORY, "python-service")
    assert scope_of(adversarial, inner / ".." / "src") == (
        ContextScope.REPOSITORY,
        "python-service",
    )
    # Climbing out of the repository entirely lands on the root, which is global.
    assert scope_of(adversarial, inner / ".." / "..")[0] is ContextScope.GLOBAL


def test_a_relative_path_resolves_against_the_process_directory(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    previous = Path.cwd()
    os.chdir(adversarial_root / "python-service")
    try:
        assert scope_of(adversarial, Path("src/deep")) == (
            ContextScope.REPOSITORY,
            "python-service",
        )
    finally:
        os.chdir(previous)


def test_a_symlink_is_canonicalized_before_comparison(
    adversarial: Workspace, adversarial_root: Path, tmp_path: Path
) -> None:
    link = tmp_path / "link-to-service"
    link.symlink_to(adversarial_root / "python-service", target_is_directory=True)
    assert scope_of(adversarial, link) == (ContextScope.REPOSITORY, "python-service")
    assert scope_of(adversarial, link / "src") == (ContextScope.REPOSITORY, "python-service")


def test_a_sibling_directory_sharing_a_prefix_is_not_inside_the_root(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """`/…/adversarial-repos-old` must not match `/…/adversarial-repos` (§7)."""
    from repoatlas.errors import RepoAtlasError

    decoy = adversarial_root.parent / f"{adversarial_root.name}-old"
    decoy.mkdir()
    with pytest.raises(RepoAtlasError):
        resolve(adversarial, decoy)


# -- nesting -----------------------------------------------------------------


def test_the_closest_managed_repository_wins(
    adversarial: Workspace, adversarial_root: Path, tmp_path: Path
) -> None:
    """A repository inside a repository is a separate repository (§5)."""
    from repoatlas.services.init_service import initialize

    inner = adversarial_root / "python-service" / "tools" / "inner-tool"
    inner.mkdir(parents=True)
    (inner / "tool.py").write_text("def inner_tool():\n    return 1\n")
    init_git_repository(inner)
    initialize(adversarial, adversarial_root, config_path=tmp_path / "renewed.yaml")

    assert scope_of(adversarial, inner) == (ContextScope.REPOSITORY, "inner-tool")
    assert scope_of(adversarial, inner.parent) == (ContextScope.REPOSITORY, "python-service")


# -- unmanaged and unknown ---------------------------------------------------


def test_an_unmanaged_git_repository_is_named_as_such(
    adversarial: Workspace, adversarial_root: Path, tmp_path: Path
) -> None:
    from repoatlas.errors import RepoAtlasError

    outside = tmp_path / "elsewhere" / "unmanaged"
    outside.mkdir(parents=True)
    (outside / "main.py").write_text("x = 1\n")
    init_git_repository(outside)
    with pytest.raises(RepoAtlasError) as raised:
        resolve(adversarial, outside)
    assert raised.value.exit_code == ExitCode.CONTEXT
    assert "not managed by RepoAtlas" in str(raised.value)


def test_an_unrelated_directory_is_an_error(adversarial: Workspace, tmp_path: Path) -> None:
    from repoatlas.errors import RepoAtlasError

    elsewhere = tmp_path / "home" / "user"
    elsewhere.mkdir(parents=True)
    with pytest.raises(RepoAtlasError) as raised:
        resolve(adversarial, elsewhere)
    assert raised.value.exit_code == ExitCode.CONTEXT
    assert "Unable to determine repository." in str(raised.value)


def test_an_unknown_explicit_repository_lists_the_real_ones(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    from repoatlas.errors import RepoAtlasError

    with pytest.raises(RepoAtlasError) as raised:
        resolve(adversarial, adversarial_root, "no-such-repository")
    assert raised.value.exit_code == ExitCode.CONTEXT
    # §6 requires the *user-visible* error to list what is available, which is the
    # rendered form the CLI prints, not the bare exception message.
    rendered = raised.value.render()
    for name in REPOSITORIES:
        assert name in rendered


def test_an_ambiguous_explicit_name_lists_the_candidates_rather_than_choosing(
    adversarial: Workspace, adversarial_root: Path, tmp_path: Path
) -> None:
    """Two repositories can genuinely share a directory name. §6 requires an error
    listing the candidates; picking one would silently answer about the wrong tree.
    """
    from repoatlas.errors import RepoAtlasError
    from repoatlas.services.init_service import initialize

    twin = adversarial_root / "nested" / "scripts"
    twin.mkdir(parents=True)
    (twin / "other.sh").write_text("#!/usr/bin/env bash\necho twin\n")
    init_git_repository(twin)
    initialize(adversarial, adversarial_root, config_path=tmp_path / "twinned.yaml")

    names = [r.name for r in adversarial.repositories.list_all()]
    assert names.count("scripts") == 2, f"the fixture must actually be ambiguous: {names}"

    with pytest.raises(RepoAtlasError) as raised:
        resolve(adversarial, adversarial_root, "scripts")
    assert raised.value.exit_code == ExitCode.CONTEXT
    rendered = raised.value.render()
    assert str(twin) in rendered and str(adversarial_root / "scripts") in rendered


# -- explicit overrides ------------------------------------------------------


@pytest.mark.parametrize("name", REPOSITORIES)
def test_an_explicit_repository_overrides_the_directory(
    adversarial: Workspace, adversarial_root: Path, name: str
) -> None:
    deep = adversarial_root / "python-service" / "src" / "deep" / "a" / "b"
    assert scope_of(adversarial, deep, name) == (ContextScope.REPOSITORY, name)


def test_an_explicit_repository_works_from_outside_the_tree(
    adversarial: Workspace, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    assert scope_of(adversarial, elsewhere, "deployment") == (
        ContextScope.REPOSITORY,
        "deployment",
    )


def test_an_explicit_path_resolves_like_an_explicit_name(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    assert scope_of(adversarial, adversarial_root, str(adversarial_root / "scripts")) == (
        ContextScope.REPOSITORY,
        "scripts",
    )


# -- the repository is gone --------------------------------------------------


def test_a_managed_repository_deleted_from_disk_does_not_widen_scope(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """Resolving a repository whose directory has been removed must stay scoped to
    it. Falling back to global would sync eight repositories the user did not ask
    about."""
    import shutil

    shutil.rmtree(adversarial_root / "ruby-tooling")
    scope, primary = scope_of(adversarial, adversarial_root, "ruby-tooling")
    assert (scope, primary) == (ContextScope.REPOSITORY, "ruby-tooling")


def test_standing_inside_a_moved_repository_resolves_to_where_it_now_is(
    adversarial: Workspace, adversarial_root: Path, tmp_path: Path
) -> None:
    """After a move, the old path is gone and the new one is not managed. Neither
    may resolve to global scope."""
    from repoatlas.errors import RepoAtlasError

    moved = tmp_path / "moved-away"
    (adversarial_root / "unrelated-repo").rename(moved)
    with pytest.raises(RepoAtlasError):
        resolve(adversarial, moved)


# -- every command sees the same scope --------------------------------------


@pytest.fixture
def cli(tmp_path: Path, adversarial_root: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator:
    from repoatlas.cli.main import app
    from typer.testing import CliRunner

    monkeypatch.setenv("REPOATLAS_CONFIG", str(tmp_path / "cli-config.yaml"))
    monkeypatch.setenv("REPOATLAS_DB", str(tmp_path / "cli-state" / "atlas.db"))
    runner = CliRunner()

    def run(directory: Path, *args: str):
        previous = Path.cwd()
        os.chdir(directory)
        try:
            return runner.invoke(app, list(args))
        finally:
            os.chdir(previous)

    assert run(adversarial_root, "init", str(adversarial_root)).exit_code == 0
    yield run


@pytest.mark.parametrize(
    "command",
    [("sync",), ("status",), ("locate", "SERVICE_HOST"), ("ask", "x"), ("debug", "x")],
)
def test_no_command_broadens_scope_from_an_unresolvable_directory(
    cli, tmp_path: Path, command: tuple[str, ...]
) -> None:
    """Whatever else a command needs, it must refuse before doing any of it."""
    elsewhere = tmp_path / "unrelated-cwd"
    elsewhere.mkdir()
    result = cli(elsewhere, *command)
    assert result.exit_code == ExitCode.CONTEXT, result.output
    assert "Unable to determine repository." in result.output


@pytest.mark.parametrize("command", [("sync",), ("status",), ("locate", "SERVICE_HOST")])
def test_a_deep_directory_scopes_every_command_to_one_repository(
    cli, adversarial_root: Path, command: tuple[str, ...]
) -> None:
    deep = adversarial_root / "python-service" / "src" / "deep" / "a" / "b" / "c" / "d"
    result = cli(deep, *command)
    assert result.exit_code == ExitCode.OK, result.output
    for other in ("ruby-tooling", "ambiguous-provider-a", "unrelated-repo"):
        assert other not in result.stdout, (command, other)
