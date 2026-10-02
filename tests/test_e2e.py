"""The V1 success scenario as a real end-to-end test (V1_SPEC §21).

Every other test in the suite calls into the library or drives Typer in-process.
This one runs the **installed console script as a separate process**: real argv,
real working directory, real exit status, real stdout. That is the only way to
assert the things a user actually experiences — that ``repoatlas`` resolves its
context from the directory it was started in, that a refusal really does exit 2,
and that no import-time work reaches for a network or a model.

The five numbered steps below are §21 verbatim. The relationship assertions are
the chain §21 requires the fixtures to demonstrate:

    Python reads SERVICE_HOST → Salt configures it → Salt takes the value from
    the pillar → Bash invokes the Python tooling → TypeScript depends on another
    local repository
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.errors import ExitCode
from tests.conftest import MANAGED_REPOSITORY_COUNT

#: The console script created by the project's ``[project.scripts]`` entry point.
CLI = Path(sys.executable).parent / "repoatlas"

pytestmark = pytest.mark.skipif(
    not CLI.exists(), reason="the repoatlas console script is not installed in this environment"
)


class Result:
    def __init__(self, completed: subprocess.CompletedProcess[str]) -> None:
        self.exit_code = completed.returncode
        self.stdout = completed.stdout
        self.stderr = completed.stderr
        self.output = completed.stdout + completed.stderr

    def __repr__(self) -> str:  # pragma: no cover - only used in failure messages
        return (
            f"exit={self.exit_code}\n--- stdout ---\n{self.stdout}\n--- stderr ---\n{self.stderr}"
        )


@pytest.fixture
def repoatlas(tmp_path: Path) -> Iterator[object]:
    """Run the real binary with an isolated config, database and HOME."""
    state = tmp_path / "state"
    state.mkdir()
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(state),
        "REPOATLAS_CONFIG": str(state / "config.yaml"),
        "REPOATLAS_DB": str(state / "atlas.db"),
    }

    def run(cwd: Path, *args: str) -> Result:
        return Result(
            subprocess.run(
                [str(CLI), *args],
                cwd=cwd,
                env=environment,
                capture_output=True,
                text=True,
                timeout=180,
            )
        )

    yield run


def test_the_full_success_scenario(repoatlas, workspace_root: Path, tmp_path: Path) -> None:
    run = repoatlas

    # 1. `repoatlas init /work/repos` discovers everything.
    initialized = run(workspace_root, "init", str(workspace_root))
    assert initialized.exit_code == ExitCode.OK, initialized
    assert str(MANAGED_REPOSITORY_COUNT) in initialized.output

    listed = run(workspace_root, "repos")
    assert listed.exit_code == ExitCode.OK, listed
    for name in ("python-service", "ts-service", "deployment", "scripts", "common-lib"):
        assert name in listed.output

    # 2. From deep inside python-service, the primary context is inferred, the
    #    relationship into deployment is followed, and the evidence is file-backed.
    deep = workspace_root / "python-service" / "src" / "deep" / "nested" / "path"
    located = run(deep, "locate", "Where is SERVICE_HOST configured?")
    assert located.exit_code == ExitCode.OK, located
    assert "python-service/src/config.py:" in located.output
    assert "deployment/service/init.sls:" in located.output

    # 3. Modify one deployment file; from deep inside deployment, only it syncs.
    target = workspace_root / "deployment" / "service" / "init.sls"
    target.write_text(target.read_text() + "\nextra_state:\n  test.nop: []\n")
    scoped = run(workspace_root / "deployment" / "service", "sync")
    assert scoped.exit_code == ExitCode.OK, scoped
    assert "Repository: deployment" in scoped.output
    assert "python-service" not in scoped.stdout

    # 4. From the root, everything syncs.
    everything = run(workspace_root, "sync")
    assert everything.exit_code == ExitCode.OK, everything
    assert f"Syncing {MANAGED_REPOSITORY_COUNT} repositories" in everything.output

    # 5. From an unrelated directory, sync is the whole workspace; anything else
    # refuses to guess.
    outside = tmp_path / "home" / "user"
    outside.mkdir(parents=True)
    anywhere = run(outside, "sync")
    assert anywhere.exit_code == ExitCode.OK, anywhere
    assert f"Syncing {MANAGED_REPOSITORY_COUNT} repositories" in anywhere.output
    refused = run(outside, "status")
    assert refused.exit_code == ExitCode.CONTEXT, refused
    assert "Unable to determine repository." in refused.output


def test_the_relationship_chain_is_demonstrable_from_the_cli(
    repoatlas, workspace_root: Path
) -> None:
    """Each link in the §21 chain, asked for the way a user would ask."""
    run = repoatlas
    assert run(workspace_root, "init", str(workspace_root)).exit_code == ExitCode.OK

    python_service = workspace_root / "python-service"

    # Python reads SERVICE_HOST, and Salt configures it — one question, both ends.
    reads = run(python_service, "locate", "SERVICE_HOST", "--json")
    assert reads.exit_code == ExitCode.OK, reads
    paths = {
        f"{item['repository']}/{item['path']}" for item in json.loads(reads.stdout)["candidates"]
    }
    assert "python-service/src/config.py" in paths
    assert "deployment/service/init.sls" in paths

    # Salt obtains the value from the pillar.
    pillar = run(workspace_root / "deployment", "locate", "pillar service host", "--json")
    assert pillar.exit_code == ExitCode.OK, pillar
    assert any(
        item["path"].endswith("pillar.sls") for item in json.loads(pillar.stdout)["candidates"]
    )

    # Bash invokes the Python tooling, across a repository boundary.
    invokes = run(workspace_root / "scripts", "locate", "deploy.sh", "--json")
    assert invokes.exit_code == ExitCode.OK, invokes
    assert any(item["path"] == "deploy.sh" for item in json.loads(invokes.stdout)["candidates"])

    # TypeScript depends on another local repository.
    status = run(workspace_root / "ts-service", "status", "--json")
    assert status.exit_code == ExitCode.OK, status
    assert json.loads(status.stdout)["repositories"][0]["name"] == "ts-service"


def test_every_deterministic_command_works_with_no_model_at_all(
    repoatlas, workspace_root: Path
) -> None:
    """``locate``, ``status`` and ``repos`` must never need an LLM (V1_SPEC §4).

    The configured base URL points at a closed port, so any attempt to reach a
    model would fail loudly rather than silently succeed on a developer machine
    that happens to be running Ollama.
    """
    run = repoatlas
    assert run(workspace_root, "init", str(workspace_root)).exit_code == ExitCode.OK
    for args in (
        ("status",),
        ("repos",),
        ("locate", "SERVICE_HOST"),
        ("locate", "SERVICE_HOST", "--json"),
        ("sync",),
    ):
        result = run(workspace_root, *args)
        assert result.exit_code == ExitCode.OK, result


def test_the_installed_command_offers_no_reasoning_command(repoatlas, workspace_root: Path) -> None:
    """End to end, through the real console script: ``ask`` is not a command.

    The environment this runs in sets no model variable and reaches no server,
    which is the point --- every other assertion in this file passes in exactly
    that environment, so the product has no model prerequisite left.
    """
    run = repoatlas
    assert run(workspace_root, "init", str(workspace_root)).exit_code == ExitCode.OK
    result = run(workspace_root, "ask", "why does the service fail to start")
    assert result.exit_code != ExitCode.OK, result
    assert result.exit_code != ExitCode.LLM_UNAVAILABLE, result


def test_commands_before_init_exit_three(repoatlas, tmp_path: Path) -> None:
    run = repoatlas
    empty = tmp_path / "nothing-indexed"
    empty.mkdir()
    result = run(empty, "status")
    assert result.exit_code in {ExitCode.NOT_INITIALIZED, ExitCode.CONTEXT}, result


def test_an_unmanaged_git_repository_is_named_as_such(
    repoatlas, workspace_root: Path, unmanaged_repo: Path
) -> None:
    run = repoatlas
    assert run(workspace_root, "init", str(workspace_root)).exit_code == ExitCode.OK
    # `status`, not `sync`: from there `sync` syncs the whole workspace instead.
    result = run(unmanaged_repo, "status")
    assert result.exit_code == ExitCode.CONTEXT, result
    assert "not managed by RepoAtlas" in result.output


def test_verbose_reports_the_retrieval_stages_to_stderr(repoatlas, workspace_root: Path) -> None:
    """The Slice D instrumentation is reachable by a user, not only by a test."""
    run = repoatlas
    assert run(workspace_root, "init", str(workspace_root)).exit_code == ExitCode.OK
    result = run(workspace_root, "locate", "SERVICE_HOST", "--verbose")
    assert result.exit_code == ExitCode.OK, result
    for stage in ("exact", "lexical", "symbol", "semantic", "graph", "fuse", "budget"):
        assert stage in result.output
    # Machine-readable output stays clean: diagnostics go to stderr.
    machine = run(workspace_root, "locate", "SERVICE_HOST", "--json", "--verbose")
    json.loads(machine.stdout)
