"""``install.sh`` takes a fresh checkout to a working installation, and is safe to rerun.

Run against a copy of the script in a scratch checkout, with a scratch HOME, and
with stand-ins for ``uv``, ``claude``, ``node`` and ``pnpm`` that record how
they were called. So these tests download nothing and touch no real
configuration, and they assert the exact commands the installer issues: which is
what decides whether an agent finds the server afterwards.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

CHECKOUT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="the installer is bash")

#: Every stand-in appends its name and arguments, one call per line.
RECORDER = """#!/usr/bin/env bash
printf '%s %s\\n' "$(basename "$0")" "$*" >> "$CALLS"
"""

UV = (
    RECORDER
    + """
case "$1" in
  --version) echo "uv 0.0.0-test" ;;
  sync)
    mkdir -p .venv/bin
    cat > .venv/bin/repoatlas <<'STUB'
#!/usr/bin/env bash
printf 'repoatlas %s\\n' "$*" >> "$CALLS"
case "$1" in
  repos) [ -e "$STATE/initialized" ] || exit 3 ;;
  init) touch "$STATE/initialized" ;;
esac
STUB
    chmod +x .venv/bin/repoatlas
    ;;
esac
"""
)

CLAUDE = (
    RECORDER
    + """
if [ "$1 $2" = "mcp get" ]; then [ -e "$STATE/registered" ] || exit 1; fi
if [ "$1 $2" = "mcp add" ]; then touch "$STATE/registered"; fi
"""
)

NODE = RECORDER + '[ "$1" = "--version" ] && echo "${NODE_VERSION:-v22.5.0}"\n'


class Sandbox:
    def __init__(self, root: Path, *, tools: tuple[str, ...]) -> None:
        self.checkout = root / "checkout"
        self.checkout.mkdir()
        shutil.copy2(CHECKOUT / "install.sh", self.checkout / "install.sh")
        shutil.copytree(CHECKOUT / "skills", self.checkout / "skills")
        self.home = root / "home"
        self.home.mkdir()
        self.state = root / "state"
        self.state.mkdir()
        self.calls_file = root / "calls.log"
        self.bin = root / "bin"
        self.bin.mkdir()
        scripts = {"uv": UV, "claude": CLAUDE, "node": NODE, "pnpm": RECORDER}
        for name in tools:
            path = self.bin / name
            path.write_text(scripts[name])
            path.chmod(0o755)
        self.repoatlas = self.checkout / ".venv" / "bin" / "repoatlas"
        self.on_path = self.home / ".local" / "bin" / "repoatlas"

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        git = shutil.which("git")
        assert git is not None
        tools = self.bin / "git"
        if not tools.exists():
            tools.symlink_to(git)
        return subprocess.run(
            ["bash", str(self.checkout / "install.sh"), *args],
            cwd=self.home,
            env={
                "PATH": f"{self.bin}:/bin",
                "HOME": str(self.home),
                "CALLS": str(self.calls_file),
                "STATE": str(self.state),
                **env,
            },
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def calls(self) -> list[str]:
        if not self.calls_file.exists():
            return []
        return self.calls_file.read_text().splitlines()


SKILLS = sorted(path.name for path in (CHECKOUT / "skills").glob("repoatlas-*") if path.is_dir())


def test_a_fresh_install_does_every_step_and_says_what_is_next(tmp_path: Path) -> None:
    sandbox = Sandbox(tmp_path, tools=("uv", "claude", "node", "pnpm"))
    repos = tmp_path / "repos"
    repos.mkdir()
    result = sandbox.run("--yes", "--root", str(repos))
    assert result.returncode == 0, result.stdout + result.stderr

    calls = sandbox.calls()
    assert "uv sync --locked" in calls
    assert "repoatlas ui --prepare" in calls
    assert f"claude mcp add --scope user repoatlas -- {sandbox.repoatlas} mcp" in calls
    assert f"repoatlas init {repos}" in calls

    assert sandbox.on_path.is_symlink()
    assert sandbox.on_path.readlink() == sandbox.repoatlas
    linked = sandbox.home / ".claude" / "skills"
    assert sorted(path.name for path in linked.iterdir()) == SKILLS
    for name in SKILLS:
        assert (linked / name).readlink() == sandbox.checkout / "skills" / name

    # ~/.local/bin is not on this PATH, and the installer says how to put it there.
    assert f'export PATH="{sandbox.home}/.local/bin:$PATH"' in result.stdout
    assert "repoatlas ui" in result.stdout and "Restart Claude Code" in result.stdout


def test_running_it_again_changes_nothing(tmp_path: Path) -> None:
    sandbox = Sandbox(tmp_path, tools=("uv", "claude", "node", "pnpm"))
    repos = tmp_path / "repos"
    repos.mkdir()
    assert sandbox.run("--yes", "--root", str(repos)).returncode == 0
    before = len(sandbox.calls())

    again = sandbox.run("--yes")
    assert again.returncode == 0, again.stdout + again.stderr
    later = sandbox.calls()[before:]
    assert not any(call.startswith("claude mcp add") for call in later)
    assert not any(call.startswith("repoatlas init") for call in later)
    assert "already linked" in again.stdout
    assert "already has an MCP server named repoatlas" in again.stdout
    assert "a workspace is already indexed" in again.stdout


def test_what_it_did_not_create_is_left_alone(tmp_path: Path) -> None:
    sandbox = Sandbox(tmp_path, tools=("uv", "claude", "node", "pnpm"))
    sandbox.on_path.parent.mkdir(parents=True)
    sandbox.on_path.write_text("someone else's repoatlas\n")
    skill = sandbox.home / ".claude" / "skills" / SKILLS[0]
    skill.mkdir(parents=True)

    result = sandbox.run("--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    assert sandbox.on_path.read_text() == "someone else's repoatlas\n"
    assert skill.is_dir() and not skill.is_symlink()
    assert result.stdout.count("left alone") == 2


def test_the_optional_steps_can_be_skipped(tmp_path: Path) -> None:
    sandbox = Sandbox(tmp_path, tools=("uv", "claude", "node", "pnpm"))
    result = sandbox.run("--yes", "--no-explorer", "--no-agent")
    assert result.returncode == 0, result.stdout + result.stderr
    calls = sandbox.calls()
    assert not any(call.startswith(("claude", "repoatlas ui", "node")) for call in calls)
    assert not (sandbox.home / ".claude").exists()
    assert "repoatlas init <directory>" in result.stdout


def test_missing_optional_tools_are_explained_not_fatal(tmp_path: Path) -> None:
    sandbox = Sandbox(tmp_path, tools=("uv",))
    result = sandbox.run("--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "needs Node.js 22 or later, and node is not on PATH" in result.stdout
    assert f"command {sandbox.repoatlas}, argument: mcp" in result.stdout
    assert "repoatlas ui --prepare" not in sandbox.calls()


def test_an_old_node_is_named(tmp_path: Path) -> None:
    sandbox = Sandbox(tmp_path, tools=("uv", "node", "pnpm"))
    result = sandbox.run("--yes", "--no-agent", NODE_VERSION="v20.11.0")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "node is v20.11.0" in result.stdout
    assert "repoatlas ui --prepare" not in sandbox.calls()


def test_without_uv_and_without_yes_nothing_is_downloaded(tmp_path: Path) -> None:
    sandbox = Sandbox(tmp_path, tools=())
    result = sandbox.run()
    assert result.returncode == 1
    assert "uv is required" in result.stderr
    assert sandbox.calls() == []
