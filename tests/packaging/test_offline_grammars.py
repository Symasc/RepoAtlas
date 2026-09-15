"""OFF-01, end to end: a fresh installation parses offline, with nothing cached.

The defect this guards against was invisible to every in-process test. Grammar
binaries were fetched by native code on first use, so Python-level socket
interception saw nothing, and any machine that had ever had network access
carried a warm cache that made the fetch unnecessary. The only honest proof is
the situation a new user is in:

* the **release artifacts** --- a wheel built from the checkout, and a wheel built
  from the unpacked sdist, which must behave identically (CASE 8);
* installed into a **clean virtual environment** from the lockfile's hashed
  requirements. Installation is the one step allowed to reach a package index;
  that is the network boundary this correction draws;
* run with a **fresh HOME and empty XDG directories**, asserted empty before and
  after, so no cache from the developer machine can be involved (CASE 5);
* inside ``unshare -rn``: a network namespace with loopback only, enforced by the
  kernel, so a download attempted from native code fails as surely as one from
  Python (CASES 1-4, 7).

Marked ``slow`` because installation touches the package index (or uv's cache).
It skips, saying so, only where the kernel refuses an unprivileged network
namespace.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest
from repoatlas.models.parsing import GRAMMAR_UNAVAILABLE, STRUCTURAL_PARSING_DISABLED
from repoatlas.parsers.grammars import GRAMMARS

ROOT = Path(__file__).resolve().parent.parent.parent

pytestmark = pytest.mark.slow


def _unshare_works() -> bool:
    if shutil.which("unshare") is None:
        return False
    probe = subprocess.run(["unshare", "-rn", "true"], capture_output=True, check=False)
    return probe.returncode == 0


requires_isolation = pytest.mark.skipif(
    shutil.which("uv") is None or not _unshare_works(),
    reason="needs uv and an unprivileged network namespace (unshare -rn)",
)

#: Runs inside the namespace, with the fresh venv's interpreter in isolated mode:
#: no PYTHONPATH, no user site, no script directory, so only installed code runs.
_DRIVER = r"""
import importlib.metadata, importlib.util, io, json, os, socket, sqlite3, sys
from contextlib import redirect_stdout
from pathlib import Path

attempts = []
def record(name, original):
    def wrapper(*args, **kwargs):
        attempts.append(name)
        return original(*args, **kwargs)
    return wrapper
socket.socket.connect = record("connect", socket.socket.connect)
socket.socket.connect_ex = record("connect_ex", socket.socket.connect_ex)
socket.create_connection = record("create_connection", socket.create_connection)
socket.getaddrinfo = record("getaddrinfo", socket.getaddrinfo)

import repoatlas
from repoatlas.cli.main import app

root, out = sys.argv[1], Path(sys.argv[2])
buffer = io.StringIO()
with redirect_stdout(buffer):
    try:
        app(["init", root, "--json"], standalone_mode=False)
        exit_code = 0
    except SystemExit as exc:
        exit_code = int(exc.code or 0)
    except Exception as exc:
        exit_code = int(getattr(exc, "exit_code", 1))
report = json.loads(buffer.getvalue())

db = sqlite3.connect(os.environ["REPOATLAS_DB"])
entities = sorted(
    list(row) for row in db.execute(
        "SELECT r.name, f.rel_path, e.type, e.name, e.line_start, e.line_end FROM entities e "
        "JOIN files f ON f.id = e.file_id JOIN repositories r ON r.id = e.repo_id"
    )
)
edges = sorted(
    list(row) for row in db.execute(
        "SELECT e.type, s.key, d.key, e.line_start FROM edges e "
        "JOIN entities s ON s.id = e.src_id JOIN entities d ON d.id = e.dst_id"
    )
)
dependencies = sorted(
    list(row) for row in db.execute("SELECT ecosystem, name, role FROM dependencies")
)
grammars = {}
for name in json.loads(os.environ["OFF01_GRAMMARS"]):
    dist = importlib.metadata.distribution(name)
    files = [str(f) for f in dist.files or ()]
    grammars[name] = {
        "version": dist.version,
        "license_files": sorted(f for f in files if Path(f).name.upper().startswith("LICENSE")),
        "binding_installed": any(f.endswith((".so", ".pyd")) for f in files),
    }
diagnostics = sorted(
    [r["repository"], d["path"], d["message"]]
    for r in report["repositories"]
    for d in r["diagnostics"]
)
forbidden = ("tree_sitter_language_pack", "tree_sitter_ruby")
absent = {m: importlib.util.find_spec(m) is None for m in forbidden}
out.write_text(json.dumps({
    "exit": exit_code,
    "repoatlas": repoatlas.__file__,
    "attempts": attempts,
    "diagnostics": diagnostics,
    "entities": entities,
    "edges": edges,
    "dependencies": dependencies,
    "grammars": grammars,
    "absent": absent,
}, indent=1))
"""


def _run(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    done = subprocess.run(
        list(args), cwd=cwd, env=env, capture_output=True, text=True, check=False, timeout=900
    )
    assert done.returncode == 0, f"{args}\n{done.stdout[-2000:]}\n{done.stderr[-4000:]}"
    return done.stdout


def _git_repo(path: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text)
    git = [
        "git",
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "-c",
        "commit.gpgsign=false",
    ]
    _run("git", "init", "-q", cwd=path)
    _run(*git, "add", "-A", cwd=path)
    _run(*git, "commit", "-q", "-m", "init", cwd=path)


@pytest.fixture(scope="module")
def artifacts(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """The wheel from the checkout and the wheel rebuilt from the unpacked sdist."""
    base = tmp_path_factory.mktemp("off01-artifacts")
    _run("uv", "build", "--out-dir", str(base / "dist"), cwd=ROOT)
    unpacked = base / "unpacked"
    unpacked.mkdir()
    with tarfile.open(next((base / "dist").glob("*.tar.gz"))) as archive:
        archive.extractall(unpacked, filter="data")
    (source,) = list(unpacked.iterdir())
    _run("uv", "build", "--wheel", "--out-dir", str(base / "from-sdist"), cwd=source)
    requirements = base / "requirements.txt"
    requirements.write_text(
        _run(
            "uv",
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--format",
            "requirements-txt",
            cwd=ROOT,
        )
    )
    return {
        "direct": next((base / "dist").glob("*.whl")),
        "from-sdist": next((base / "from-sdist").glob("*.whl")),
        "requirements": requirements,
    }


@pytest.fixture(scope="module")
def repositories(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("off01-repos") / "repos"
    _git_repo(
        root / "ts",
        {
            "src/x.ts": "export function x() {\n    return 1;\n}\n",
            "src/view.tsx": "export function View() {\n    return <div>{1}</div>;\n}\n",
            "src/legacy.js": "function legacy() {\n    return 2;\n}\n",
        },
    )
    _git_repo(
        root / "bash", {"hello.sh": "#!/usr/bin/env bash\necho hello\n\ngreet() {\n  echo hi\n}\n"}
    )
    _git_repo(
        root / "ruby",
        {
            "lib/hello.rb": 'def hello\n  "hello"\nend\n',
            "Gemfile": 'source "https://rubygems.org"\ngem "rack", "~> 2.2"\n',
        },
    )
    _git_repo(root / "py", {"pkg/hello.py": 'def hello():\n    return "hello"\n'})
    return root


def _install(artifact: Path, requirements: Path, venv: Path) -> Path:
    _run("uv", "venv", "--python", "3.13", str(venv))
    python = venv / "bin" / "python"
    _run(
        "uv", "pip", "install", "--python", str(python), "--require-hashes", "-r", str(requirements)
    )
    _run("uv", "pip", "install", "--python", str(python), "--no-deps", str(artifact))
    return python


def _index_offline(python: Path, repos: Path, sandbox: Path) -> dict[str, Any]:
    """Index ``repos`` with ``python``'s installation, fresh state, no network."""
    home, cache = sandbox / "home", sandbox / "xdg-cache"
    config, data, state = sandbox / "xdg-config", sandbox / "xdg-data", sandbox / "state"
    for directory in (home, cache, config, data, state):
        directory.mkdir(parents=True)
    (state / "config.yaml").write_text("embeddings:\n  provider: hashing\n  dim: 128\n")
    driver = sandbox / "driver.py"
    driver.write_text(_DRIVER)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "XDG_CACHE_HOME": str(cache),
        "XDG_CONFIG_HOME": str(config),
        "XDG_DATA_HOME": str(data),
        "REPOATLAS_CONFIG": str(state / "config.yaml"),
        "REPOATLAS_DB": str(state / "atlas.db"),
        "OFF01_GRAMMARS": json.dumps(sorted({g.distribution for g in GRAMMARS.values()})),
    }

    # CASE 5 / mutation E: the run must not be able to borrow a warm cache from the
    # developer machine. Everything it could read or write is new, empty, and ours.
    for key in ("HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        assert Path(env[key]).is_relative_to(sandbox), key
        assert env[key] != os.environ.get(key), key
        assert list(Path(env[key]).iterdir()) == [], key

    out = sandbox / "result.json"
    _run(
        "unshare", "-rn", str(python), "-I", str(driver), str(repos), str(out), cwd=sandbox, env=env
    )
    result = json.loads(out.read_text())

    # Nothing was downloaded, cached or left behind for next time.
    assert [str(p) for p in cache.rglob("*")] == []
    assert [str(p) for p in home.rglob("*")] == []
    return result


@requires_isolation
def test_a_fresh_installation_parses_every_shipped_grammar_with_no_network(
    artifacts: dict[str, Path], repositories: Path, tmp_path: Path
) -> None:
    results: dict[str, dict[str, Any]] = {}
    for label in ("direct", "from-sdist"):
        venv = tmp_path / f"venv-{label}"
        python = _install(artifacts[label], artifacts["requirements"], venv)
        result = _index_offline(python, repositories, tmp_path / f"sandbox-{label}")
        results[label] = result

        assert result["exit"] == 0, result
        assert str(result["repoatlas"]).startswith(str(venv)), "imported from the installation"
        assert result["attempts"] == [], "no Python-level network attempt either"
        assert result["absent"] == {"tree_sitter_language_pack": True, "tree_sitter_ruby": True}

        # CASE 9: the installed dependency form is exactly the pinned grammars.
        installed = result["grammars"]
        for grammar in GRAMMARS.values():
            entry = installed[grammar.distribution]
            assert entry["version"] == grammar.version, grammar.distribution
            assert entry["license_files"], grammar.distribution
            assert entry["binding_installed"], grammar.distribution

        entities = {(repo, path, kind, name) for repo, path, kind, name, _, _ in result["entities"]}
        # CASES 1 and 2: TypeScript, TSX, JavaScript and Bash parse structurally.
        assert ("ts", "src/x.ts", "function", "x") in entities
        assert ("ts", "src/view.tsx", "function", "View") in entities
        assert ("ts", "src/legacy.js", "function", "legacy") in entities
        assert ("bash", "hello.sh", "function", "greet") in entities
        # CASE 4: Python, the control.
        assert ("py", "pkg/hello.py", "function", "hello") in entities
        # CASE 3: Ruby is gated, text-indexed, and its Gemfile is still read.
        assert {kind for repo, _, kind, _ in entities if repo == "ruby"} == {"file"}
        assert ["gem", "rack", "declared"] in result["dependencies"]

        # CASE 7: the only diagnostics are Ruby's policy; no grammar is unavailable.
        assert result["diagnostics"] == [
            ["ruby", "Gemfile", STRUCTURAL_PARSING_DISABLED],
            ["ruby", "lib/hello.rb", STRUCTURAL_PARSING_DISABLED],
        ]
        assert GRAMMAR_UNAVAILABLE not in json.dumps(result["diagnostics"])

    # CASE 8: the wheel rebuilt from the sdist is the same product.
    for key in ("entities", "edges", "dependencies", "diagnostics", "grammars"):
        assert results["direct"][key] == results["from-sdist"][key], key
