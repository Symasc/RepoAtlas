"""Start the explorer and the local API it reads, and stop them together.

``repoatlas ui`` is the launcher ``docs/local-api.md`` used to defer: one command
that serves the index over the local API and runs the explorer's web app against
it, so nobody has to copy a connection file's path from one terminal to another.

What runs where
---------------
The local API runs **in this process**, on a free loopback port, exactly as
``repoatlas serve`` runs it; :func:`repoatlas.api.server.serve` owns it, and
nothing here answers a request. The explorer runs as a **child process**: Node,
running Next.js from the explorer's own ``node_modules``, in a process group of
its own. The child is told where the API's connection file is through
``REPOATLAS_API_CONNECTION_FILE``, the one variable the contract names, so the
token passes through a file readable by its owner only and never through an
argument or the environment.

The two live and die together. When the explorer stops, the API is stopped; when
the API stops --- Ctrl-C, SIGTERM, or a failure --- the explorer's whole process
group is stopped, so nothing is left listening.

An installed RepoAtlas runs no Node at all. Its release build exports the
explorer as files into the package (``repoatlas/ui/explorer``), and the local API
sends them itself, answering the pages' calls at the door the explorer's server
would have kept (:mod:`repoatlas.api.server`). One process, one port, and the
browser still never holds the token. A checkout's own ``frontend/`` wins over an
exported copy, so a developer always runs the explorer they are editing.

What it executes
----------------
Node, pnpm and the commands that open a browser: never a repository's code, and
never git. The explorer is RepoAtlas's own ``frontend/``, found beside the
installed package in a checkout, or named by ``--frontend`` or
``REPOATLAS_FRONTEND_DIR``. Its packages are installed and its production build
made the first time they are needed, and again only when its lockfile or sources
change; installation is the one step that downloads anything, here as everywhere.
An exported explorer is files the API sends, and runs nothing.

The MCP server knows nothing of any of this. Neither transport imports this
module, and nothing it starts is an agent tool.
"""

from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from repoatlas.api import server as api
from repoatlas.config.settings import Settings, database_path, load_settings
from repoatlas.errors import ExitCode, RepoAtlasError

DEFAULT_PORT = 7318
HOST = "127.0.0.1"
#: Names the explorer's directory when it is not the checkout's ``frontend/``.
FRONTEND_ENV = "REPOATLAS_FRONTEND_DIR"
#: Where a release build puts the exported explorer, beside this module.
PACKAGED_DIR = "explorer"
#: The oldest Node.js the explorer builds with: Next.js and pnpm 10 run on it.
MIN_NODE = (20, 19)
#: Where ``repoatlas ui`` keeps its production build. Its own directory, so it
#: never disturbs ``pnpm dev`` (``.next/dev``) or the end-to-end build (``.next-e2e``).
DIST_DIR = ".next-ui"
#: The connection file ``repoatlas ui`` writes when the variable does not name one.
#: Its own, so a ``repoatlas serve`` running beside it keeps its ``api.json``.
CONNECTION_FILE_NAME = "ui-api.json"
#: Written into ``node_modules`` once an install succeeds; newer inputs mean another.
INSTALL_STAMP = ".repoatlas-ui-installed"
#: Seconds the explorer may take to start listening. ``next dev`` compiles lazily,
#: so even it listens within a few seconds; this is for a slow first start.
READY_TIMEOUT = 120.0
#: Seconds a stopped explorer gets to exit before it is killed.
STOP_GRACE = 10.0

#: Top-level entries of the explorer that are not inputs to its production build:
#: installed packages, build output, tests and their output, and tooling configuration.
_NOT_BUILD_INPUTS = frozenset(
    {
        "node_modules",
        ".next",
        DIST_DIR,
        ".next-e2e",
        "out",
        "test-results",
        "playwright-report",
        "e2e",
        "tests",
        "scripts",
        "README.md",
        "tsconfig.tsbuildinfo",
        "next-env.d.ts",
        "vitest.config.ts",
        "playwright.config.ts",
        "eslint.config.mjs",
    }
)


class UiError(RepoAtlasError):
    """The explorer could not be started or stopped running.

    Exit code 2 when the machine is not set up for it (no Node, no explorer, a
    taken port), which is a request the CLI cannot act on as things stand; 1 when
    something that should have worked did not.
    """

    def __init__(self, message: str, *, detail: str | None = None, setup: bool = False) -> None:
        super().__init__(message, detail=detail)
        self.exit_code = ExitCode.CONTEXT if setup else ExitCode.RUNTIME


# ---------------------------------------------------------------------------
# Finding what to run
# ---------------------------------------------------------------------------


def locate_frontend(
    explicit: Path | None, environ: Mapping[str, str], *, sources_only: bool = False
) -> Path:
    """The explorer's directory: ``--frontend``, else the variable, else the
    checkout's ``frontend/``, else the exported copy installed with RepoAtlas.

    Either the explorer's sources (:func:`is_sources`) or an export of them
    (:func:`is_exported`), which ``sources_only`` refuses: only sources build or
    hot-reload.
    """
    if explicit is not None:
        chosen, source = explicit, "--frontend"
    elif environ.get(FRONTEND_ENV):
        chosen, source = Path(environ[FRONTEND_ENV]), FRONTEND_ENV
    else:
        here = Path(__file__).resolve().parent
        chosen, source = here.parents[1] / "frontend", None
        if not is_sources(chosen) and not sources_only and is_exported(here / PACKAGED_DIR):
            chosen = here / PACKAGED_DIR
    chosen = chosen.expanduser().resolve()
    if is_sources(chosen) or (is_exported(chosen) and not sources_only):
        return chosen
    if source is not None:
        needs = "package.json and next.config.ts"
        raise UiError(
            f"{chosen} is not the RepoAtlas explorer (named by {source})",
            detail=(
                f"It needs the explorer's {needs}"
                + ("." if sources_only else ", or an exported explorer's index.html and _next/.")
            ),
            setup=True,
        )
    if sources_only:
        raise UiError(
            "the explorer's sources are not part of this installation",
            detail=(
                "`--dev` runs them with Node. Run it from a RepoAtlas checkout (see "
                "docs/installation.md), or name the checkout's frontend/ with --frontend "
                f"or {FRONTEND_ENV}."
            ),
            setup=True,
        )
    raise UiError(
        "the explorer is not part of this installation",
        detail=(
            "A release of RepoAtlas includes the explorer; this one was built without it. "
            "Install a release (`uv tool install symasc-repoatlas`), run from a checkout "
            f"(see docs/installation.md), or name the explorer with --frontend or {FRONTEND_ENV}."
        ),
        setup=True,
    )


def is_sources(directory: Path) -> bool:
    """Whether ``directory`` is the explorer's sources, which Node builds and runs."""
    return (directory / "package.json").is_file() and (directory / "next.config.ts").is_file()


def is_exported(directory: Path) -> bool:
    """Whether ``directory`` is the explorer exported as files (``pnpm build:static``)."""
    return (directory / "index.html").is_file() and (directory / "_next").is_dir()


def find_node(which: Callable[[str], str | None] = shutil.which) -> str:
    """Node.js on PATH, at least version :data:`MIN_NODE`."""
    node = which("node")
    if node is None:
        raise UiError(
            f"the explorer needs Node.js {_spelled(MIN_NODE)} or later, and `node` is not on PATH",
            detail="Install Node.js from https://nodejs.org (or with nvm), then run this again.",
            setup=True,
        )
    try:
        reported = subprocess.run(  # noqa: S603 - the Node on PATH, asked its version
            [node, "--version"], capture_output=True, text=True, timeout=15, check=False
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise UiError(f"could not run {node}", detail=str(exc), setup=True) from exc
    version = _version(reported)
    if version is None or version < MIN_NODE:
        raise UiError(
            f"the explorer needs Node.js {_spelled(MIN_NODE)} or later; {node} is "
            f"{reported or 'of an unknown version'}",
            setup=True,
        )
    return node


def _version(reported: str) -> tuple[int, int] | None:
    """``(major, minor)`` from ``node --version`` (``v20.19.0``)."""
    parts = reported.removeprefix("v").split(".")
    if len(parts) < 2 or not (parts[0].isdigit() and parts[1].isdigit()):
        return None
    return int(parts[0]), int(parts[1])


def _spelled(version: tuple[int, int]) -> str:
    return ".".join(map(str, version))


def package_manager(which: Callable[[str], str | None] = shutil.which) -> list[str]:
    """How to run pnpm: on PATH, else through Node's corepack."""
    pnpm = which("pnpm")
    if pnpm is not None:
        return [pnpm]
    corepack = which("corepack")
    if corepack is not None:
        return [corepack, "pnpm"]
    raise UiError(
        "the explorer's packages are installed with pnpm, and neither `pnpm` nor "
        "`corepack` is on PATH",
        detail="Install it with `npm install -g pnpm@10` or `corepack enable`, then try again.",
        setup=True,
    )


def next_cli(frontend: Path) -> Path:
    return frontend / "node_modules" / "next" / "dist" / "bin" / "next"


def needs_install(frontend: Path) -> bool:
    """Whether the explorer's packages are missing or older than what declares them."""
    stamp = frontend / "node_modules" / INSTALL_STAMP
    if not stamp.is_file() or not next_cli(frontend).is_file():
        return True
    declared = max(_mtime(frontend / "package.json"), _mtime(frontend / "pnpm-lock.yaml"))
    return declared > _mtime(stamp)


def needs_build(frontend: Path) -> bool:
    """Whether there is no production build, or a source has changed since it was made."""
    build = frontend / DIST_DIR / "BUILD_ID"
    if not build.is_file():
        return True
    return newest_build_input(frontend) > _mtime(build)


def newest_build_input(frontend: Path) -> float:
    """The latest modification time among the files a production build reads.

    Stat only: nothing is opened. A false "changed" costs a rebuild; a false
    "unchanged" would serve an explorer older than its source, so everything
    outside :data:`_NOT_BUILD_INPUTS` counts, dot-files such as ``.env.local``
    included, and only dot-directories (caches) are passed over.
    """
    newest = 0.0
    for entry in frontend.iterdir():
        if entry.name in _NOT_BUILD_INPUTS or (entry.is_dir() and entry.name.startswith(".")):
            continue
        if entry.is_file():
            newest = max(newest, _mtime(entry))
            continue
        for directory, subdirectories, files in os.walk(entry):
            subdirectories[:] = [name for name in subdirectories if not name.startswith(".")]
            for name in files:
                newest = max(newest, _mtime(Path(directory) / name))
    return newest


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def ensure_port_free(port: int) -> None:
    """Refuse a port something already listens on, before anything is started."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((HOST, port))
        except OSError as exc:
            raise UiError(
                f"port {port} is already in use",
                detail=(
                    "Another explorer may already be running. Stop it, or choose another "
                    "port with --port."
                ),
                setup=True,
            ) from exc


def connection_file(settings: Settings, environ: Mapping[str, str]) -> Path:
    """``REPOATLAS_API_CONNECTION_FILE``, or :data:`CONNECTION_FILE_NAME` beside the index."""
    override = environ.get(api.CONNECTION_FILE_ENV)
    if override:
        return Path(override).expanduser()
    return database_path(settings).parent / CONNECTION_FILE_NAME


def explorer_environment(
    environ: Mapping[str, str], connection: Path, *, production: bool
) -> dict[str, str]:
    """The explorer's environment: where the API's connection file is, and no telemetry.

    The token is not in it. The explorer reads the token from the connection file,
    so a token this process was given in ``REPOATLAS_API_TOKEN`` is withheld.
    """
    environment = {key: value for key, value in environ.items() if key != api.TOKEN_ENV}
    environment[api.CONNECTION_FILE_ENV] = str(connection)
    # RepoAtlas makes no network request of its own, and neither does its explorer.
    environment["NEXT_TELEMETRY_DISABLED"] = "1"
    if production:
        environment["RA_DIST_DIR"] = DIST_DIR
    else:
        environment.pop("RA_DIST_DIR", None)
    return environment


# ---------------------------------------------------------------------------
# Opening a browser
# ---------------------------------------------------------------------------


def browser_command(
    url: str,
    environ: Mapping[str, str],
    which: Callable[[str], str | None] = shutil.which,
    platform: str = sys.platform,
) -> list[str] | None:
    """The command that opens ``url`` in a graphical browser here, or ``None``.

    ``BROWSER`` first, because an editor sets it to reach the browser on the
    machine in front of the user (VS Code does, over SSH and WSL). Then the
    platform's own opener. Never a console browser: one would take over the
    terminal this command is reporting in.
    """
    chosen = environ.get("BROWSER", "").split(os.pathsep)[0].strip()
    if chosen:
        words = shlex.split(chosen)
        if any("%s" in word for word in words):
            return [word.replace("%s", url) for word in words]
        return [*words, url]
    if platform == "darwin":
        return ["open", url]
    if platform == "win32":
        return ["cmd", "/c", "start", "", url]
    if environ.get("WSL_DISTRO_NAME") or environ.get("WSL_INTEROP"):
        for opener in ("wslview", "explorer.exe"):
            found = which(opener)
            if found is not None:
                return [found, url]
    if environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY"):
        found = which("xdg-open")
        if found is not None:
            return [found, url]
    return None


def open_browser(url: str, environ: Mapping[str, str] | None = None) -> bool:
    """Open ``url`` without waiting for the browser, and say whether an opener was found."""
    command = browser_command(url, os.environ if environ is None else environ)
    if command is None:
        return False
    try:
        subprocess.Popen(  # noqa: S603 - an opener chosen above, given only the URL
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# Running it
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Explorer:
    """How to start the explorer's server, once it is installed and built."""

    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]
    port: int

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}"


def prepare(
    *,
    port: int,
    dev: bool,
    rebuild: bool,
    frontend: Path | None,
    connection: Path,
    announce: Callable[[str], None],
    environ: Mapping[str, str] | None = None,
    check_port: bool = True,
) -> Explorer:
    """Check the machine, install and build what is missing, and say how to start it."""
    environment = os.environ if environ is None else environ
    directory = locate_frontend(frontend, environment, sources_only=True)
    node = find_node()
    if check_port:
        ensure_port_free(port)
    env = explorer_environment(environment, connection, production=not dev)

    if needs_install(directory):
        announce("Installing the explorer's packages (once; this downloads them)...")
        # Exactly the lockfile. And without a prompt: pnpm otherwise stops to ask
        # before replacing a node_modules laid out for another location or version,
        # and with no terminal to ask on, it fails instead.
        install = ["install", "--frozen-lockfile", "--config.confirm-modules-purge=false"]
        # Corepack asks before fetching pnpm; this was the request to fetch it.
        quiet = {**env, "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0"}
        _run([*package_manager(), *install], directory, quiet, "install")
        (directory / "node_modules" / INSTALL_STAMP).touch()
    cli = str(next_cli(directory))
    if not dev and (rebuild or needs_build(directory)):
        announce("Building the explorer (once per change to it)...")
        _run([node, cli, "build"], directory, env, "build")

    mode = "dev" if dev else "start"
    return Explorer(
        argv=(node, cli, mode, "--hostname", HOST, "--port", str(port)),
        cwd=directory,
        env=env,
        port=port,
    )


def _run(argv: Sequence[str], cwd: Path, env: Mapping[str, str], what: str) -> None:
    try:
        completed = subprocess.run(list(argv), cwd=cwd, env=dict(env), check=False)  # noqa: S603
    except OSError as exc:
        raise UiError(f"could not run the explorer's {what}", detail=str(exc)) from exc
    if completed.returncode != 0:
        raise UiError(
            f"the explorer's {what} failed (exit code {completed.returncode})",
            detail="Its output is above.",
        )


class _Running:
    """The explorer's server process, and its process group."""

    def __init__(self, explorer: Explorer) -> None:
        self.process = subprocess.Popen(  # noqa: S603 - Node, running the explorer's own Next.js
            list(explorer.argv),
            cwd=explorer.cwd,
            env=dict(explorer.env),
            # Its own group, so Ctrl-C reaches this process alone and stopping
            # the group stops whatever the server started too.
            start_new_session=os.name == "posix",
        )

    def stop(self) -> None:
        """SIGTERM to the whole group, then SIGKILL if it outlives the grace period.

        The group is signalled even when its leader has already exited, because a
        worker the leader started may not have.
        """
        if os.name != "posix":
            if self.process.poll() is None:
                self.process.terminate()
                self._wait_or_kill()
            return
        self._signal_group(signal.SIGTERM)
        self._wait_or_kill()

    def _wait_or_kill(self) -> None:
        try:
            self.process.wait(timeout=STOP_GRACE)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                self._signal_group(signal.SIGKILL)
            else:
                self.process.kill()
            self.process.wait()

    def _signal_group(self, signum: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.process.pid, signum)


def wait_until_listening(port: int, process: subprocess.Popen[bytes], timeout: float) -> bool:
    """Whether the explorer accepted a connection before it exited or the time ran out."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            with socket.create_connection((HOST, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def run(
    explorer: Explorer,
    *,
    settings: Settings,
    connection: Path,
    announce: Callable[[str], None],
    open_url: Callable[[str], bool] | None = None,
    ready_timeout: float = READY_TIMEOUT,
) -> None:
    """Serve the API here and the explorer beside it until either stops.

    Returns when stopped by Ctrl-C or SIGTERM. Raises :class:`UiError` when the
    explorer stopped on its own or never started listening, after stopping the
    API, so a failure never leaves one half running.
    """
    failures: list[str] = []
    stopping = threading.Event()
    started: list[_Running] = []

    def supervise(running: _Running, serving: api.Serving) -> None:
        if wait_until_listening(explorer.port, running.process, ready_timeout):
            announce(f"RepoAtlas explorer at {explorer.url}  (Ctrl-C stops it)")
            if open_url is not None and not open_url(explorer.url):
                announce("Open that address in your browser.")
        elif running.process.poll() is None:
            failures.append(f"the explorer did not start listening within {ready_timeout:.0f}s")
            running.stop()
        code = running.process.wait()
        if not stopping.is_set() and not failures:
            failures.append(f"the explorer stopped (exit code {code})")
        serving.stop()

    def ready(serving: api.Serving) -> None:
        announce(f"RepoAtlas local API at {serving.url}")
        try:
            running = _Running(explorer)
        except OSError as exc:
            raise UiError("could not start the explorer", detail=str(exc)) from exc
        started.append(running)
        threading.Thread(
            target=supervise, args=(running, serving), name="explorer", daemon=True
        ).start()

    try:
        api.serve(port=0, settings=settings, connection_file=connection, ready=ready)
    finally:
        stopping.set()
        for running in started:
            running.stop()
    if failures:
        raise UiError(failures[0], detail="Its output is above.")


def serve_exported(
    directory: Path,
    *,
    port: int,
    settings: Settings,
    connection: Path,
    announce: Callable[[str], None],
    open_url: Callable[[str], bool] | None = None,
) -> None:
    """The exported explorer and the API it reads, from one server here, until stopped.

    Returns when stopped by Ctrl-C or SIGTERM. Nothing else is started.
    """

    def ready(serving: api.Serving) -> None:
        announce(f"RepoAtlas explorer at {serving.url}  (Ctrl-C stops it)")
        if open_url is not None and not open_url(serving.url):
            announce("Open that address in your browser.")

    api.serve(
        host=HOST,
        port=port,
        settings=settings,
        connection_file=connection,
        ready=ready,
        explorer=directory,
    )


def launch(
    *,
    port: int = DEFAULT_PORT,
    dev: bool = False,
    rebuild: bool = False,
    open_in_browser: bool = True,
    frontend: Path | None = None,
    prepare_only: bool = False,
    announce: Callable[[str], None],
) -> None:
    """``repoatlas ui``: prepare the explorer, then run it and the API until stopped.

    With ``prepare_only``, stop once it is installed and built: what an installer
    runs, so the first ``repoatlas ui`` starts at once. An exported explorer
    needs neither, and is served by the API itself.
    """
    settings = load_settings()
    connection = connection_file(settings, os.environ)
    directory = locate_frontend(frontend, os.environ, sources_only=dev and not prepare_only)
    explorer: Explorer | None = None
    if is_sources(directory):
        explorer = prepare(
            port=port,
            dev=dev and not prepare_only,
            rebuild=rebuild,
            frontend=directory,
            connection=connection,
            announce=announce,
            check_port=not prepare_only,
        )
    elif not prepare_only:
        ensure_port_free(port)
    if prepare_only:
        announce(f"The explorer is ready in {directory}. Start it with `repoatlas ui`.")
        return
    if settings.root is None:
        announce(
            "No workspace is indexed yet. Run `repoatlas init <directory>`; "
            "the explorer shows it once there is one."
        )
    open_url = open_browser if open_in_browser else None
    if explorer is None:
        serve_exported(
            directory,
            port=port,
            settings=settings,
            connection=connection,
            announce=announce,
            open_url=open_url,
        )
    else:
        run(
            explorer,
            settings=settings,
            connection=connection,
            announce=announce,
            open_url=open_url,
        )
