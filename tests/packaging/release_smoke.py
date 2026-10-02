"""Smoke test of one built distribution, run against the installed copy.

    uv run --isolated --no-project --with dist/<wheel or sdist> \
        tests/packaging/release_smoke.py <version>

The release workflow runs it on the wheel and on the source distribution before
anything is published (not collected by pytest: the name matches no test pattern).
It checks what a user gets from PyPI rather than what the checkout holds: the
metadata names the version being released, the schema migrations, the agent
skills and the exported explorer are inside the package, `init` indexes a
repository and `locate` finds a definition in it, the MCP server answers a
handshake with its tools, and `repoatlas ui` serves the explorer and answers its
pages with no Node, while refusing a call from another site.
"""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from importlib import metadata, resources
from pathlib import Path

import repoatlas
from repoatlas import DISTRIBUTION

SKILLS = 5
TOOLS = 10


def main(expected: str) -> None:
    checkout = Path(__file__).resolve().parents[2]
    installed = Path(repoatlas.__file__).resolve()
    assert not installed.is_relative_to(checkout), f"imported from the checkout: {installed}"

    released = metadata.version(DISTRIBUTION)
    assert released == expected == repoatlas.__version__, (
        f"versions disagree: tag {expected}, metadata {released}, "
        f"repoatlas.__version__ {repoatlas.__version__}"
    )

    package = resources.files("repoatlas")
    migrations = [
        entry.name
        for entry in package.joinpath("storage").joinpath("sqlite").joinpath("migrations").iterdir()
        if entry.name.endswith(".sql")
    ]
    assert migrations, "no schema migrations in the package"
    skills = [
        entry.name
        for entry in package.joinpath("skills").iterdir()
        if entry.name.startswith("repoatlas-")
    ]
    assert len(skills) == SKILLS, f"skills in the package: {sorted(skills)}"
    explorer = package.joinpath("ui").joinpath("explorer")
    assert explorer.joinpath("index.html").is_file(), "no exported explorer in the package"

    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        env = {
            **os.environ,
            "REPOATLAS_CONFIG": str(root / "config.yaml"),
            "REPOATLAS_DB": str(root / "atlas.db"),
            "XDG_CONFIG_HOME": str(root / "xdg-config"),
            "XDG_DATA_HOME": str(root / "xdg-data"),
        }
        repository = root / "repos" / "demo"
        repository.mkdir(parents=True)
        (repository / "app.py").write_text('def greet(name):\n    return "hi " + name\n')
        for command in (
            ["init", "--quiet"],
            ["add", "-A"],
            [
                "-c",
                "user.name=release",
                "-c",
                "user.email=release@example.invalid",
                "commit",
                "--quiet",
                "-m",
                "demo",
            ],
        ):
            subprocess.run(["git", *command], cwd=repository, check=True)

        def repoatlas_cli(*args: str, cwd: Path, stdin: str | None = None) -> str:
            done = subprocess.run(
                [sys.executable, "-m", "repoatlas.cli.main", *args],
                cwd=cwd,
                env=env,
                input=stdin,
                capture_output=True,
                text=True,
                timeout=600,
            )
            assert done.returncode == 0, f"repoatlas {' '.join(args)}: {done.stdout}{done.stderr}"
            return done.stdout

        repoatlas_cli("init", str(root / "repos"), cwd=root)
        located = repoatlas_cli("locate", "greet", cwd=repository)
        assert "app.py" in located, located

        handshake = (
            "\n".join(
                json.dumps(message)
                for message in (
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-06-18",
                            "capabilities": {},
                            "clientInfo": {"name": "release-smoke", "version": "0"},
                        },
                    },
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                )
            )
            + "\n"
        )
        answered = repoatlas_cli("mcp", cwd=root, stdin=handshake)
        replies = {reply.get("id"): reply for reply in map(json.loads, answered.splitlines())}
        server = replies[1]["result"]["serverInfo"]
        assert server["version"] == expected, server
        tools = replies[2]["result"]["tools"]
        assert len(tools) == TOOLS, [tool["name"] for tool in tools]

        asset = explorer_answers(env, cwd=root)

    print(
        f"{DISTRIBUTION} {expected}: {len(migrations)} migrations, {len(skills)} skills, "
        f"init and locate, MCP {server['name']} with {len(tools)} tools, "
        f"explorer served with {asset} and its API door"
    )


def explorer_answers(env: dict[str, str], *, cwd: Path) -> str:
    """`repoatlas ui` serves the packaged explorer and the door its pages call through."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    site = f"http://127.0.0.1:{port}"
    ui = subprocess.Popen(
        [sys.executable, "-m", "repoatlas.cli.main", "ui", "--no-open", "--port", str(port)],
        cwd=cwd,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert ui.stderr is not None
        announced: list[str] = []
        for line in ui.stderr:  # until the announcement, or the end if it never comes
            announced.append(line)
            if "RepoAtlas explorer at" in line:
                break
        assert announced and "RepoAtlas explorer at" in announced[-1], announced

        with urllib.request.urlopen(f"{site}/", timeout=60) as page:  # noqa: S310 - loopback
            html = page.read().decode()
        script = re.search(r'src="(/_next/static/[^"]+\.js)"', html)
        assert script is not None, "the page names no script"
        with urllib.request.urlopen(f"{site}{script.group(1)}", timeout=60) as answer:  # noqa: S310
            assert answer.headers["Content-Type"].startswith("text/javascript")

        def call(origin: str) -> tuple[int, dict[str, object]]:
            request = urllib.request.Request(  # noqa: S310 - loopback
                f"{site}/api/ra/repositories",
                data=json.dumps({"context": {"scope": "global"}}).encode(),
                headers={"Content-Type": "application/json", "Origin": origin},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=60) as answer:  # noqa: S310
                    return answer.status, json.loads(answer.read())
            except urllib.error.HTTPError as refused:
                return refused.code, json.loads(refused.read())

        status, envelope = call(site)
        assert status == 200 and envelope["ok"] is True, envelope
        assert "demo" in json.dumps(envelope["result"]), envelope
        status, envelope = call("http://elsewhere.example")
        assert status == 403, (status, envelope)
    finally:
        ui.send_signal(signal.SIGINT)
        code = ui.wait(timeout=60)
    assert code == 0, f"repoatlas ui exited {code}"
    return script.group(1).rsplit("/", 1)[-1]


if __name__ == "__main__":
    main(sys.argv[1])
