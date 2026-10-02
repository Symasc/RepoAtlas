"""``repoatlas ui`` runs the explorer and the local API together, and stops both.

The lifecycle tests run the real command in a subprocess, with stand-ins for
``node`` and ``pnpm`` on PATH, so they need no Node and download nothing. The
stand-in "Next.js" server does what the real one's proxy does: it reads the
connection file named by ``REPOATLAS_API_CONNECTION_FILE`` and calls the API
with its token. So these tests check the wiring the explorer depends on, the
Ctrl-C path through a real signal, and that a stopped explorer leaves nothing
running --- not even a worker it started.

The rest are the decisions the launcher makes on its own: where the explorer
is, whether it needs installing or building, which port and browser to use, and
what the explorer's environment holds.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest
from repoatlas.api.server import CONNECTION_FILE_ENV, CONTRACT, TOKEN_ENV
from repoatlas.config.settings import Settings
from repoatlas.errors import ExitCode
from repoatlas.ui import launcher
from repoatlas.ui.launcher import UiError

CHECKOUT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")

FAKE_NODE = r'''#!{python}
"""Stands in for Node running Next.js: `--version`, `build`, `start` and `dev`."""
import http.server, json, os, subprocess, sys, time, urllib.request
from pathlib import Path

args = sys.argv[1:]
if args == ["--version"]:
    print(os.environ.get("FAKE_NODE_VERSION", "v22.1.0"))
    sys.exit(0)
cli, command, *rest = args
with Path(os.environ["FAKE_LOG"]).open("a") as log:
    log.write(json.dumps({{
        "node": command,
        "args": rest,
        "dist": os.environ.get("RA_DIST_DIR"),
        "telemetry": os.environ.get("NEXT_TELEMETRY_DISABLED"),
        "token_in_env": "REPOATLAS_API_TOKEN" in os.environ,
    }}) + "\n")
if command == "build":
    out = Path.cwd() / os.environ["RA_DIST_DIR"]
    out.mkdir(exist_ok=True)
    (out / "BUILD_ID").write_text("fake")
    sys.exit(0)
mode = os.environ.get("FAKE_MODE", "serve")
if mode == "fail":
    sys.exit(3)
# A worker of its own, as a real server may start: stopping must reach it too.
worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
Path(os.environ["FAKE_WORKER"]).write_text(str(worker.pid))
port = int(rest[rest.index("--port") + 1])


class Probe(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        record = json.loads(Path(os.environ["REPOATLAS_API_CONNECTION_FILE"]).read_text())
        request = urllib.request.Request(
            record["url"] + "/v1", headers={{"Authorization": "Bearer " + record["token"]}}
        )
        body = urllib.request.urlopen(request, timeout=10).read()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


server = http.server.HTTPServer(("127.0.0.1", port), Probe)
if mode == "exit":
    # Listen long enough to be seen, then stop on its own.
    server.timeout = 0.1
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        server.handle_request()
    sys.exit(0)
server.serve_forever()
'''

FAKE_PNPM = r"""#!{python}
import json, os, sys
from pathlib import Path

with Path(os.environ["FAKE_LOG"]).open("a") as log:
    log.write(json.dumps({{"pnpm": sys.argv[1:]}}) + "\n")
cli = Path.cwd() / "node_modules" / "next" / "dist" / "bin" / "next"
cli.parent.mkdir(parents=True, exist_ok=True)
cli.write_text("// fake")
"""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _executable(path: Path, text: str) -> None:
    path.write_text(text.format(python=sys.executable), encoding="utf-8")
    path.chmod(0o755)


class Rig:
    """A fake explorer directory, fake tools, and an isolated RepoAtlas state."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.frontend = root / "frontend"
        self.frontend.mkdir()
        for name in ("package.json", "pnpm-lock.yaml", "next.config.ts"):
            (self.frontend / name).write_text("{}\n")
        (self.frontend / "app").mkdir()
        (self.frontend / "app" / "page.tsx").write_text("export default 1;\n")
        tools = root / "bin"
        tools.mkdir()
        _executable(tools / "node", FAKE_NODE)
        _executable(tools / "pnpm", FAKE_PNPM)
        self.log = root / "calls.jsonl"
        self.worker = root / "worker.pid"
        self.state = root / "state"
        self.connection = self.state / launcher.CONNECTION_FILE_NAME
        self.port = _free_port()
        self.env = {
            "PATH": f"{tools}:/usr/bin:/bin",
            "HOME": str(root / "home"),
            "REPOATLAS_CONFIG": str(root / "config.yaml"),
            "REPOATLAS_DB": str(self.state / "atlas.db"),
            "PYTHONPATH": str(CHECKOUT),
            TOKEN_ENV: "t" * 40,
            "FAKE_LOG": str(self.log),
            "FAKE_WORKER": str(self.worker),
        }
        (root / "home").mkdir()

    def calls(self) -> list[dict[str, object]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def start(self, *flags: str, frontend: Path | None = None, **env: str) -> Running:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "repoatlas.cli.main",
                "ui",
                *flags,
                "--no-open",
                "--port",
                str(self.port),
                "--frontend",
                str(frontend or self.frontend),
            ],
            env={**self.env, **env},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        return Running(process)


class Running:
    """The command under test, with its stderr read as it arrives."""

    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.process = process
        self.lines: queue.Queue[str] = queue.Queue()
        self.seen: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.process.stderr is not None
        for line in self.process.stderr:
            self.lines.put(line)

    def wait_for(self, text: str, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        while not any(text in line for line in self.seen):
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"never printed {text!r}: {self.seen}"
            try:
                self.seen.append(self.lines.get(timeout=remaining))
            except queue.Empty:
                continue

    def finish(self, timeout: float = 30.0) -> int:
        try:
            code = self.process.wait(timeout=timeout)
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait()
        time.sleep(0.1)
        while not self.lines.empty():
            self.seen.append(self.lines.get())
        return code

    @property
    def output(self) -> str:
        return "".join(self.seen)


def _gone(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        # A killed worker is reparented and reaped by init; until then it is a zombie.
        status = Path(f"/proc/{pid}/stat")
        if status.exists() and status.read_text().split(") ", 1)[-1].startswith("Z"):
            return True
        time.sleep(0.1)
    return False


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def test_ui_installs_builds_serves_the_api_and_stops_everything_on_ctrl_c(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    running = rig.start()
    running.wait_for("RepoAtlas explorer at")

    # The explorer reached the API through the connection file alone.
    with urllib.request.urlopen(f"http://127.0.0.1:{rig.port}/probe", timeout=30) as answer:
        index = json.loads(answer.read())
    assert index["ok"] is True and index["result"]["contract"] == CONTRACT
    worker = int(rig.worker.read_text())
    assert rig.connection.exists()

    running.process.send_signal(signal.SIGINT)
    assert running.finish() == 0, running.output
    assert not _listening(rig.port)
    assert not rig.connection.exists()
    assert _gone(worker), "the explorer's worker outlived it"

    calls = rig.calls()
    pnpm = [call["pnpm"] for call in calls if "pnpm" in call]
    assert pnpm == [["install", "--frozen-lockfile", "--config.confirm-modules-purge=false"]]
    node = [call for call in calls if "node" in call]
    assert [call["node"] for call in node] == ["build", "start"]
    assert node[1]["args"] == ["--hostname", "127.0.0.1", "--port", str(rig.port)]
    assert {call["dist"] for call in node} == {launcher.DIST_DIR}
    assert {call["telemetry"] for call in node} == {"1"}
    assert not any(call["token_in_env"] for call in node)
    assert "t" * 40 not in running.output

    # Installed and built: the next launch starts straight away.
    again = rig.start()
    again.wait_for("RepoAtlas explorer at")
    again.process.send_signal(signal.SIGINT)
    assert again.finish() == 0, again.output
    assert "Installing" not in again.output and "Building" not in again.output
    assert [call.get("node") for call in rig.calls()[len(calls) :]] == ["start"]


def test_ui_stops_the_api_when_the_explorer_stops_on_its_own(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    running = rig.start(FAKE_MODE="exit")
    running.wait_for("RepoAtlas explorer at")
    assert running.finish() == ExitCode.RUNTIME, running.output
    assert "the explorer stopped (exit code 0)" in running.output
    assert not rig.connection.exists()
    assert _gone(int(rig.worker.read_text())), "the explorer's worker outlived it"


def test_ui_reports_an_explorer_that_never_started(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    running = rig.start(FAKE_MODE="fail")
    assert running.finish() == ExitCode.RUNTIME, running.output
    assert "the explorer stopped (exit code 3)" in running.output
    assert "RepoAtlas explorer at" not in running.output
    assert not rig.connection.exists()


def test_ui_refuses_a_node_older_than_the_explorer_needs(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    running = rig.start(FAKE_NODE_VERSION="v20.11.0")
    assert running.finish() == ExitCode.CONTEXT, running.output
    assert "Node.js 22 or later" in running.output and "v20.11.0" in running.output
    assert rig.calls() == []


def test_ui_refuses_a_port_already_in_use(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", rig.port))
        taken.listen()
        running = rig.start()
        assert running.finish() == ExitCode.CONTEXT, running.output
    assert f"port {rig.port} is already in use" in running.output
    assert rig.calls() == []


def test_prepare_installs_and_builds_then_exits_without_serving(tmp_path: Path) -> None:
    """What an installer runs. The port is not its business: a dev server may hold it."""
    rig = Rig(tmp_path)
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", rig.port))
        taken.listen()
        running = rig.start("--prepare")
        assert running.finish() == 0, running.output
    assert "Start it with `repoatlas ui`" in running.output
    assert [call.get("node") or "pnpm" for call in rig.calls()] == ["pnpm", "build"]
    assert not rig.connection.exists()


def _exported(directory: Path) -> Path:
    """The layout ``pnpm build:static`` writes, as far as the launcher looks."""
    (directory / "_next" / "static").mkdir(parents=True)
    (directory / "index.html").write_text("<!doctype html><title>explorer</title>")
    return directory


def test_ui_serves_an_exported_explorer_itself_and_runs_no_node(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    running = rig.start(frontend=_exported(tmp_path / "out"))
    running.wait_for("RepoAtlas explorer at")
    with urllib.request.urlopen(f"http://127.0.0.1:{rig.port}/", timeout=30) as page:
        assert page.read() == b"<!doctype html><title>explorer</title>"
    # A call as the page makes one: same origin, no token.
    call = urllib.request.Request(
        f"http://127.0.0.1:{rig.port}/api/ra/repositories",
        data=json.dumps({"context": {"scope": "global"}}).encode(),
        headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{rig.port}"},
        method="POST",
    )
    with urllib.request.urlopen(call, timeout=30) as answer:  # noqa: S310 - loopback http, above
        assert json.loads(answer.read())["ok"] is True
    assert rig.connection.exists()

    running.process.send_signal(signal.SIGINT)
    assert running.finish() == 0, running.output
    assert not _listening(rig.port)
    assert not rig.connection.exists()
    assert rig.calls() == [], "node or pnpm ran"
    assert "t" * 40 not in running.output


def test_an_exported_explorer_needs_no_preparing_and_cannot_hot_reload(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    out = _exported(tmp_path / "out")
    prepared = rig.start("--prepare", frontend=out)
    assert prepared.finish() == 0, prepared.output
    assert "Start it with `repoatlas ui`" in prepared.output
    dev = rig.start("--dev", frontend=out)
    assert dev.finish() == ExitCode.CONTEXT, dev.output
    assert "is not the RepoAtlas explorer" in dev.output
    assert rig.calls() == []


# ---------------------------------------------------------------------------
# The launcher's own decisions
# ---------------------------------------------------------------------------


def test_the_explorer_is_found_in_the_checkout_by_default() -> None:
    found = launcher.locate_frontend(None, {})
    assert found == CHECKOUT / "frontend"


def test_a_named_directory_must_be_the_explorer(tmp_path: Path) -> None:
    with pytest.raises(UiError) as refused:
        launcher.locate_frontend(tmp_path, {})
    assert refused.value.exit_code == ExitCode.CONTEXT
    assert "--frontend" in refused.value.message
    with pytest.raises(UiError) as named:
        launcher.locate_frontend(None, {launcher.FRONTEND_ENV: str(tmp_path)})
    assert launcher.FRONTEND_ENV in named.value.message


def test_an_installation_without_the_explorer_says_how_to_get_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(launcher, "__file__", str(tmp_path / "site" / "repoatlas" / "ui" / "x.py"))
    with pytest.raises(UiError) as refused:
        launcher.locate_frontend(None, {})
    assert "not part of this installation" in refused.value.message
    assert refused.value.detail is not None and "--frontend" in refused.value.detail


def test_an_installed_explorer_is_used_when_there_is_no_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "site" / "repoatlas" / "ui"
    monkeypatch.setattr(launcher, "__file__", str(package / "launcher.py"))
    installed = _exported(package / launcher.PACKAGED_DIR)
    assert launcher.locate_frontend(None, {}) == installed
    with pytest.raises(UiError) as refused:
        launcher.locate_frontend(None, {}, sources_only=True)
    assert "sources are not part of this installation" in refused.value.message


def test_a_checkouts_explorer_wins_over_an_exported_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "repoatlas" / "ui"
    monkeypatch.setattr(launcher, "__file__", str(package / "launcher.py"))
    _exported(package / launcher.PACKAGED_DIR)
    sources = tmp_path / "frontend"
    sources.mkdir()
    for name in ("package.json", "next.config.ts"):
        (sources / name).write_text("{}\n")
    assert launcher.locate_frontend(None, {}) == sources


def test_node_and_pnpm_are_required_with_a_way_to_get_them() -> None:
    with pytest.raises(UiError) as no_node:
        launcher.find_node(lambda name: None)
    assert "not on PATH" in no_node.value.message
    assert launcher.package_manager(lambda name: f"/x/{name}") == ["/x/pnpm"]
    corepack_only = {"corepack": "/x/corepack"}
    assert launcher.package_manager(corepack_only.get) == ["/x/corepack", "pnpm"]
    with pytest.raises(UiError):
        launcher.package_manager(lambda name: None)


def _stamp(path: Path, when: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    os.utime(path, (when, when))


def test_packages_are_installed_again_only_when_what_declares_them_changes(tmp_path: Path) -> None:
    for name in ("package.json", "pnpm-lock.yaml"):
        _stamp(tmp_path / name, 1000)
    assert launcher.needs_install(tmp_path)
    _stamp(launcher.next_cli(tmp_path), 1500)
    _stamp(tmp_path / "node_modules" / launcher.INSTALL_STAMP, 2000)
    assert not launcher.needs_install(tmp_path)
    _stamp(tmp_path / "pnpm-lock.yaml", 3000)
    assert launcher.needs_install(tmp_path)


def test_the_explorer_is_rebuilt_only_when_a_build_input_changes(tmp_path: Path) -> None:
    _stamp(tmp_path / "app" / "page.tsx", 1000)
    _stamp(tmp_path / "package.json", 1000)
    assert launcher.needs_build(tmp_path)
    _stamp(tmp_path / launcher.DIST_DIR / "BUILD_ID", 2000)
    assert not launcher.needs_build(tmp_path)

    # Not inputs: tests, their output, docs, installed packages, other builds.
    for unrelated in (
        "tests/a.test.ts",
        "e2e/b.spec.ts",
        "README.md",
        "node_modules/x/index.js",
        ".next/dev/x",
        "test-results/r.txt",
        "lib/.cache/x",
    ):
        _stamp(tmp_path / unrelated, 3000)
    assert not launcher.needs_build(tmp_path)

    for step, source in enumerate(("components/c/x.tsx", ".env.local", "next.config.ts")):
        changed = 4000 + 2000 * step
        _stamp(tmp_path / source, changed)
        assert launcher.needs_build(tmp_path), source
        _stamp(tmp_path / launcher.DIST_DIR / "BUILD_ID", changed + 1000)
        assert not launcher.needs_build(tmp_path)


def test_the_connection_file_is_the_launchers_own_unless_one_is_named(tmp_path: Path) -> None:
    settings = Settings.model_validate({"storage": {"database": str(tmp_path / "db" / "atlas.db")}})
    assert launcher.connection_file(settings, {}) == tmp_path / "db" / "ui-api.json"
    named = tmp_path / "elsewhere.json"
    assert launcher.connection_file(settings, {CONNECTION_FILE_ENV: str(named)}) == named


def test_the_explorer_environment_names_the_file_and_withholds_the_token(tmp_path: Path) -> None:
    given = {TOKEN_ENV: "secret" * 8, "RA_DIST_DIR": "mine", "KEEP": "1"}
    production = launcher.explorer_environment(given, tmp_path / "c.json", production=True)
    assert TOKEN_ENV not in production
    assert production[CONNECTION_FILE_ENV] == str(tmp_path / "c.json")
    assert production["NEXT_TELEMETRY_DISABLED"] == "1"
    assert production["RA_DIST_DIR"] == launcher.DIST_DIR
    assert production["KEEP"] == "1"
    development = launcher.explorer_environment(given, tmp_path / "c.json", production=False)
    assert "RA_DIST_DIR" not in development


@pytest.mark.parametrize(
    ("environ", "platform", "tools", "expected"),
    [
        ({"BROWSER": "/vscode/browser.sh"}, "linux", {}, ["/vscode/browser.sh", "URL"]),
        ({"BROWSER": "firefox --new-tab %s:lynx"}, "linux", {}, ["firefox", "--new-tab", "URL"]),
        ({}, "darwin", {}, ["open", "URL"]),
        (
            {"WSL_DISTRO_NAME": "Ubuntu", "DISPLAY": ":0"},
            "linux",
            {"explorer.exe": "/w/e"},
            ["/w/e", "URL"],
        ),
        (
            {"WSL_DISTRO_NAME": "Ubuntu"},
            "linux",
            {"wslview": "/u/wslview", "explorer.exe": "/w/e"},
            ["/u/wslview", "URL"],
        ),
        ({"WAYLAND_DISPLAY": "wayland-0"}, "linux", {"xdg-open": "/u/xdg"}, ["/u/xdg", "URL"]),
        ({}, "linux", {"xdg-open": "/u/xdg", "lynx": "/u/lynx"}, None),
    ],
)
def test_a_browser_is_opened_the_way_this_machine_opens_one(
    environ: dict[str, str], platform: str, tools: dict[str, str], expected: list[str] | None
) -> None:
    assert launcher.browser_command("URL", environ, tools.get, platform) == expected


def test_ui_is_a_command_line_tool_and_no_agent_tool() -> None:
    """The MCP server knows nothing of the launcher: no tool, no import."""
    from repoatlas.mcp import tools

    names = {definition.name for definition in tools.TOOLS}
    assert not any("ui" in name.split("_") for name in names)
    for module in (CHECKOUT / "repoatlas" / "mcp").rglob("*.py"):
        assert "repoatlas.ui" not in module.read_text(encoding="utf-8"), module
