"""The documented scenario through the real CLI, on the adversarial ecosystem.

`tests/test_e2e.py` proves the §21 walkthrough on the clean fixture set. This file
runs the same commands against the nine-repository tree that contains misleading
prose, ambiguous providers, injection payloads and broken Salt — because the
question is not whether the happy path works but whether the *answers change
correctly* when the tree changes, with all that noise present.

Every command is the installed console script in a separate process: real argv,
real working directory, real exit status.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.errors import ExitCode
from tests.adversarial.conftest import REPOSITORIES, SECRET_SENTINEL
from tests.conftest import git

CLI = Path(sys.executable).parent / "repoatlas"

pytestmark = [
    pytest.mark.adversarial,
    pytest.mark.skipif(not CLI.exists(), reason="the repoatlas console script is not installed"),
]


class Result:
    def __init__(self, done: subprocess.CompletedProcess[str]) -> None:
        self.exit_code = done.returncode
        self.stdout = done.stdout
        self.stderr = done.stderr
        self.output = done.stdout + done.stderr

    def __repr__(self) -> str:  # pragma: no cover - failure messages only
        return f"exit={self.exit_code}\n--stdout--\n{self.stdout}\n--stderr--\n{self.stderr}"


def _cites_line(stdout: str, path: str, line: int) -> bool:
    """Whether rendered evidence cites a span of ``path`` that covers ``line``."""
    for match in re.finditer(rf"{re.escape(path)}:(\d+)(?:-(\d+))?", stdout):
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start <= line <= end:
            return True
    return False


@pytest.fixture
def cli(tmp_path: Path) -> Iterator:
    state = tmp_path / "cli-state"
    state.mkdir()
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(state),
        "REPOATLAS_CONFIG": str(state / "config.yaml"),
        "REPOATLAS_DB": str(state / "atlas.db"),
        # A closed port: any attempt to reach a model fails loudly rather than
        # quietly succeeding on a machine that happens to run Ollama.
    }

    def run(cwd: Path, *args: str) -> Result:
        return Result(
            subprocess.run(
                [str(CLI), *args],
                cwd=cwd,
                env=environment,
                capture_output=True,
                text=True,
                timeout=300,
            )
        )

    yield run


def test_the_full_scenario_with_answers_that_must_change(
    cli, adversarial_root: Path, tmp_path: Path
) -> None:
    """init → inspect → locate → edit → sync → *the answer changes* → branch →
    sync → *the stale answer is gone* → scope errors → explicit override."""
    assert cli(adversarial_root, "init", str(adversarial_root)).exit_code == ExitCode.OK

    listed = cli(adversarial_root, "repos")
    assert listed.exit_code == ExitCode.OK, listed
    for name in REPOSITORIES:
        assert name in listed.stdout

    status = cli(adversarial_root, "status")
    assert status.exit_code == ExitCode.OK, status

    # From ten directories deep, the answer reaches another repository.
    deep = adversarial_root / "python-service" / "src" / "deep" / "a" / "b" / "c" / "d" / "e"
    located = cli(deep, "locate", "Where is SERVICE_HOST configured?")
    assert located.exit_code == ExitCode.OK, located
    assert "python-service/src/config.py:" in located.stdout
    assert "deployment/service/init.sls:" in located.stdout
    # The misleading document names this file; it contains the token nowhere.
    assert "legacy_config.py" not in located.stdout

    # The same question as JSON, which is the shape an agent consumes. The
    # identity is two fields there rather than one rendered path, so the check is
    # on the parsed pairs.
    payload = cli(deep, "locate", "Where is SERVICE_HOST configured?", "--json")
    assert payload.exit_code == ExitCode.OK, payload
    reached = {
        (candidate["repository"], candidate["path"])
        for candidate in json.loads(payload.stdout)["candidates"]
    }
    assert ("python-service", "src/config.py") in reached, reached
    assert ("deployment", "service/init.sls") in reached, reached

    errored = cli(
        adversarial_root / "python-service",
        "locate",
        "service failed to bind to the configured host",
    )
    assert errored.exit_code == ExitCode.OK, errored
    assert "src/app.py:" in errored.stdout

    # Change where the value comes from, and the answer must follow. What must go
    # is the citation of the configuring line; line 4 of init.sls is a comment that
    # still names SERVICE_HOST, and a lexical hit on it is a true mention. Whether
    # that chunk fits the evidence budget depends on every other file in the
    # ecosystem, so the assertion is about the line, not the file (OFF-01 moved it).
    init_sls = adversarial_root / "deployment" / "service" / "init.sls"
    configuring_line = next(
        number
        for number, line in enumerate(init_sls.read_text().splitlines(), start=1)
        if "SERVICE_HOST:" in line
    )
    assert _cites_line(located.stdout, "deployment/service/init.sls", configuring_line), located
    init_sls.write_text(init_sls.read_text().replace("SERVICE_HOST:", "OTHER_HOST:"))
    scoped = cli(adversarial_root / "deployment" / "service", "sync")
    assert scoped.exit_code == ExitCode.OK, scoped
    assert "Repository: deployment" in scoped.stdout
    assert "python-service" not in scoped.stdout

    changed = cli(deep, "locate", "Where is SERVICE_HOST configured?")
    assert changed.exit_code == ExitCode.OK, changed
    assert not _cites_line(changed.stdout, "deployment/service/init.sls", configuring_line), changed
    assert "python-service/src/config.py:" in changed.stdout

    # A branch that removes a symbol; after sync it must be unfindable.
    scripts = adversarial_root / "scripts"
    git(scripts, "checkout", "--quiet", "-b", "trimmed")
    (scripts / "deploy.sh").unlink()
    git(scripts, "add", "-A")
    git(scripts, "commit", "--quiet", "-m", "drop deploy")
    assert cli(scripts, "sync").exit_code == ExitCode.OK
    gone = cli(scripts, "locate", "deploy_helper")
    assert "deploy.sh" not in gone.stdout, gone

    git(scripts, "checkout", "--quiet", "main")
    assert cli(scripts, "sync").exit_code == ExitCode.OK
    restored = cli(scripts, "locate", "deploy_helper")
    assert "deploy.sh" in restored.stdout, restored

    # From the root, everything syncs.
    everything = cli(adversarial_root, "sync")
    assert everything.exit_code == ExitCode.OK, everything
    assert f"Syncing {len(REPOSITORIES)} repositories" in everything.stdout

    # From an unrelated directory, RepoAtlas refuses.
    outside = tmp_path / "home" / "user"
    outside.mkdir(parents=True)
    refused = cli(outside, "sync")
    assert refused.exit_code == ExitCode.CONTEXT, refused
    assert "Unable to determine repository." in refused.output

    # Explicit repository overrides the directory, from anywhere.
    override = cli(outside, "sync", "deployment")
    assert override.exit_code == ExitCode.OK, override
    assert "Repository: deployment" in override.stdout


def test_no_command_ever_prints_secret_content(cli, adversarial_root: Path) -> None:
    """None of these commands mentions the sentinel, so any occurrence in the
    output came from a file RepoAtlas should never have read."""
    assert cli(adversarial_root, "init", str(adversarial_root)).exit_code == ExitCode.OK
    output = "".join(
        cli(adversarial_root, *args).output
        for args in (
            ("sync", "--verbose"),
            ("status", "--verbose"),
            ("repos",),
            ("locate", "credentials", "--json", "--verbose"),
            ("locate", "SERVICE_HOST", "--verbose"),
            ("locate", "private key", "--verbose"),
        )
    )
    assert SECRET_SENTINEL not in output
    for name in (".env", "secret.pem", "private.key", "id_rsa", "id_ed25519"):
        assert name not in output, name


def test_searching_for_a_secret_returns_nothing_and_names_no_secret_file(
    cli, adversarial_root: Path
) -> None:
    """Echoing the user's own query back is not a leak; returning the file is.

    The sentinel appears in the trace only because it was typed. What must not
    appear is any secret file path or any line of secret content.
    """
    assert cli(adversarial_root, "init", str(adversarial_root)).exit_code == ExitCode.OK
    result = cli(adversarial_root / "python-service", "locate", SECRET_SENTINEL, "--json")
    assert result.exit_code == ExitCode.OK, result
    assert json.loads(result.stdout)["candidates"] == []
    for name in (".env", "secret.pem", "private.key", "id_rsa", "id_ed25519"):
        assert name not in result.stdout, name


def test_an_unanswerable_question_says_so_through_the_cli(cli, adversarial_root: Path) -> None:
    assert cli(adversarial_root, "init", str(adversarial_root)).exit_code == ExitCode.OK
    result = cli(adversarial_root / "python-service", "locate", "ZZZ_NO_SUCH_TOKEN_918273")
    assert result.exit_code == ExitCode.OK, result
    assert "config.py" not in result.stdout
    payload = cli(
        adversarial_root / "python-service", "locate", "ZZZ_NO_SUCH_TOKEN_918273", "--json"
    )
    assert json.loads(payload.stdout)["candidates"] == []


def test_json_output_is_machine_readable_for_every_command(cli, adversarial_root: Path) -> None:
    assert cli(adversarial_root, "init", str(adversarial_root)).exit_code == ExitCode.OK
    for args in (
        ("status", "--json"),
        ("repos", "--json"),
        ("locate", "SERVICE_HOST", "--json"),
        ("sync", "--json"),
    ):
        result = cli(adversarial_root, *args)
        assert result.exit_code == ExitCode.OK, result
        json.loads(result.stdout)


def test_the_reasoning_command_is_not_part_of_the_product(cli, adversarial_root: Path) -> None:
    """``ask`` used to exit 4 here, having found no model. There is no command.

    Through the real console script, in an environment that sets no model
    variable --- which is the environment every other test in this file runs in,
    and the point of the removal.
    """
    assert cli(adversarial_root, "init", str(adversarial_root)).exit_code == ExitCode.OK
    result = cli(adversarial_root / "python-service", "ask", "why does startup fail")
    assert result.exit_code != ExitCode.OK, result
    assert result.exit_code != ExitCode.LLM_UNAVAILABLE, result


def test_an_injected_instruction_does_not_change_cli_output(cli, adversarial_root: Path) -> None:
    """The payload asks for `secrets.yaml` and for config.py to go unmentioned."""
    assert cli(adversarial_root, "init", str(adversarial_root)).exit_code == ExitCode.OK
    result = cli(adversarial_root / "python-service", "locate", "SERVICE_HOST")
    assert result.exit_code == ExitCode.OK, result
    assert "src/config.py" in result.stdout
    assert "secrets.yaml" not in result.stdout
