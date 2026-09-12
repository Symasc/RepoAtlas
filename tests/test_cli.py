"""CLI behavior and the V1_SPEC §21 success scenario, driven through Typer."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.cli.main import app
from repoatlas.errors import ExitCode
from tests.conftest import MANAGED_REPOSITORY_COUNT
from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Isolate config and database into the temp directory."""
    config = tmp_path / "config.yaml"
    monkeypatch.setenv("REPOATLAS_CONFIG", str(config))
    monkeypatch.setenv("REPOATLAS_DB", str(tmp_path / "state" / "atlas.db"))
    yield config


def run_in(directory: Path, *args: str):
    """Invoke the CLI with the process CWD set, as a user would."""
    previous = Path.cwd()
    os.chdir(directory)
    try:
        return runner.invoke(app, list(args))
    finally:
        os.chdir(previous)


def test_init_discovers_everything(cli_env: Path, workspace_root: Path) -> None:
    result = run_in(workspace_root, "init", str(workspace_root))
    assert result.exit_code == ExitCode.OK, result.output
    assert f"Repositories discovered: {MANAGED_REPOSITORY_COUNT}" in result.output
    assert cli_env.exists()


def test_repos_lists_managed_repositories(cli_env: Path, workspace_root: Path) -> None:
    run_in(workspace_root, "init", str(workspace_root))
    result = run_in(workspace_root, "repos")
    assert result.exit_code == ExitCode.OK
    assert "python-service" in result.output
    assert "Python" in result.output


def test_status_at_root_is_global(cli_env: Path, workspace_root: Path) -> None:
    run_in(workspace_root, "init", str(workspace_root))
    result = run_in(workspace_root, "status")
    assert result.exit_code == ExitCode.OK
    assert f"Root: {workspace_root}" in result.output
    assert "Repositories:" in result.output


def test_status_inside_repository_is_scoped(cli_env: Path, workspace_root: Path) -> None:
    run_in(workspace_root, "init", str(workspace_root))
    result = run_in(workspace_root / "python-service" / "src", "status")
    assert result.exit_code == ExitCode.OK
    assert "Repository: python-service" in result.output


def test_locate_from_deep_inside_returns_file_backed_evidence(
    cli_env: Path, workspace_root: Path
) -> None:
    """The headline scenario from V1_SPEC §21."""
    run_in(workspace_root, "init", str(workspace_root))
    deep = workspace_root / "python-service" / "src" / "deep" / "nested" / "path"
    result = run_in(deep, "locate", "Where is SERVICE_HOST configured?")

    assert result.exit_code == ExitCode.OK, result.output
    assert "EVIDENCE" in result.output
    assert "python-service/src/config.py:" in result.output


def test_locate_needs_no_reasoning_model(cli_env: Path, workspace_root: Path) -> None:
    """No LLM is installed in CI; locate must still work fully."""
    run_in(workspace_root, "init", str(workspace_root))
    result = run_in(workspace_root, "locate", "SERVICE_HOST")
    assert result.exit_code == ExitCode.OK
    assert "EVIDENCE" in result.output


def test_locate_reports_insufficient_evidence(cli_env: Path, workspace_root: Path) -> None:
    run_in(workspace_root, "init", str(workspace_root))
    result = run_in(workspace_root, "locate", "ZZZ_NO_SUCH_TOKEN_QQQ")
    assert result.exit_code == ExitCode.OK
    assert "INSUFFICIENT EVIDENCE" in result.output


def test_sync_inside_repository_syncs_only_it(cli_env: Path, workspace_root: Path) -> None:
    run_in(workspace_root, "init", str(workspace_root))
    (workspace_root / "deployment" / "service" / "init.sls").write_text("changed: true\n")

    deep = workspace_root / "deployment" / "service"
    result = run_in(deep, "sync")
    assert result.exit_code == ExitCode.OK
    assert "Repository: deployment" in result.output
    assert "python-service" not in result.output


def test_sync_at_root_syncs_everything(cli_env: Path, workspace_root: Path) -> None:
    run_in(workspace_root, "init", str(workspace_root))
    result = run_in(workspace_root, "sync")
    assert result.exit_code == ExitCode.OK
    assert f"Syncing {MANAGED_REPOSITORY_COUNT} repositories" in result.output


def test_sync_outside_managed_tree_errors(
    cli_env: Path, workspace_root: Path, tmp_path: Path
) -> None:
    """Must error and ask, never silently sync everything (V1_SPEC §6)."""
    run_in(workspace_root, "init", str(workspace_root))
    outside = tmp_path / "home-of-user"
    outside.mkdir()

    result = run_in(outside, "sync")
    assert result.exit_code == ExitCode.CONTEXT
    assert "Unable to determine repository." in result.output
    assert "repoatlas sync" in result.output


def test_sync_in_unmanaged_git_repository_errors(
    cli_env: Path, workspace_root: Path, unmanaged_repo: Path
) -> None:
    run_in(workspace_root, "init", str(workspace_root))
    result = run_in(unmanaged_repo, "sync")
    assert result.exit_code == ExitCode.CONTEXT
    assert "This Git repository is not managed by RepoAtlas." in result.output


def test_sync_explicit_repository_overrides_cwd(cli_env: Path, workspace_root: Path) -> None:
    run_in(workspace_root, "init", str(workspace_root))
    result = run_in(workspace_root / "python-service", "sync", "deployment")
    assert result.exit_code == ExitCode.OK
    assert "Repository: deployment" in result.output


def test_commands_before_init_exit_with_code_three(cli_env: Path, tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    result = run_in(work, "status")
    assert result.exit_code == ExitCode.NOT_INITIALIZED
    assert "not initialized" in result.output.lower()


def test_a_removed_reasoning_command_is_not_reachable(cli_env: Path, workspace_root: Path) -> None:
    """``ask`` exited 4 when no model was reachable. There is no model and no
    command: RepoAtlas supplies evidence and an external agent reasons over it.

    Asserted as a usage error rather than as exit 4, because a product that still
    answered ``ask`` with "no reasoning model is available" would be describing a
    subsystem it no longer has.
    """
    run_in(workspace_root, "init", str(workspace_root))
    result = run_in(workspace_root, "ask", "why?")
    assert result.exit_code != ExitCode.OK
    assert result.exit_code != ExitCode.LLM_UNAVAILABLE
    assert "reasoning model" not in result.output.lower()


def test_json_output_is_valid(cli_env: Path, workspace_root: Path) -> None:
    run_in(workspace_root, "init", str(workspace_root))
    result = run_in(workspace_root, "status", "--json")
    assert result.exit_code == ExitCode.OK
    payload = json.loads(result.output)
    assert payload["scope"] == "global"
    assert payload["repository_count"] == MANAGED_REPOSITORY_COUNT


def test_locate_json_output_carries_provenance(cli_env: Path, workspace_root: Path) -> None:
    run_in(workspace_root, "init", str(workspace_root))
    result = run_in(workspace_root, "locate", "SERVICE_HOST", "--json")
    payload = json.loads(result.output)
    assert payload["candidates"]
    for candidate in payload["candidates"]:
        assert candidate["repository"]
        assert candidate["path"]
        assert candidate["line_start"] >= 1


def test_verbose_prints_context_reason(cli_env: Path, workspace_root: Path) -> None:
    run_in(workspace_root, "init", str(workspace_root))
    result = run_in(workspace_root / "scripts", "status", "--verbose")
    assert "context:" in result.output


def test_end_to_end_success_scenario(cli_env: Path, workspace_root: Path) -> None:
    """The full V1_SPEC §21 walkthrough, in order."""
    assert run_in(workspace_root, "init", str(workspace_root)).exit_code == 0

    deep = workspace_root / "python-service" / "src" / "deep" / "nested" / "path"
    located = run_in(deep, "locate", "Where is SERVICE_HOST configured?")
    assert located.exit_code == 0
    assert "python-service/src/config.py:" in located.output

    # Modify a deployment file; sync from deep inside deployment syncs only it.
    target = workspace_root / "deployment" / "service" / "init.sls"
    target.write_text(target.read_text() + "\nextra_state:\n  test.nop: []\n")
    scoped = run_in(workspace_root / "deployment" / "service", "sync")
    assert scoped.exit_code == 0
    assert "Repository: deployment" in scoped.output

    # From the root, everything syncs.
    everything = run_in(workspace_root, "sync")
    assert everything.exit_code == 0
    assert f"Syncing {MANAGED_REPOSITORY_COUNT} repositories" in everything.output

    # From outside, RepoAtlas refuses to guess.
    outside = workspace_root.parent / "elsewhere-entirely"
    outside.mkdir(exist_ok=True)
    refused = run_in(outside, "sync")
    assert refused.exit_code == ExitCode.CONTEXT
    assert "Unable to determine repository." in refused.output
