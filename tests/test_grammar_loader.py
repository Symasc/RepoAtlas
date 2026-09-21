"""OFF-01: grammars are pinned, installed artifacts --- never downloaded, never cached.

RepoAtlas used to load TypeScript, JavaScript and Bash grammars through a library
that fetched binaries from GitHub on first use and then ``dlopen``-ed whatever
sat in the user's cache directory. On a fresh install with no network those
parsers silently degraded, and every offline test that passed did so because the
machine's cache happened to be warm. Python-level socket interception could not
see the fetch: it happens in native code.

These tests hold the replacement to what it claims:

* every shipped grammar loads from the distribution ``pyproject.toml`` pins, at
  exactly that version, and from no module or binding that distribution did not
  install (CASES 9, 10);
* nothing in RepoAtlas, its declared dependencies or its lockfile can download a
  grammar, and a full sync never reaches for one (CASE 6);
* the user's cache directory does not matter --- empty, or poisoned with corrupt
  grammar binaries and the downloader's environment variables pointing at them,
  the facts are identical and the directory is never opened (CASE 5, §10);
* an unsupported or missing grammar still fails closed with the fixed diagnostic
  (CASE 12), Python never touches tree-sitter (CASE 4), and the provenance the
  notices file publishes is the provenance the lockfile verifies (CASE 11).

The kernel-level proof --- a clean installed wheel, an empty HOME, and a network
namespace with no route anywhere --- is ``tests/packaging/test_offline_grammars.py``.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import importlib.abc
import importlib.machinery
import importlib.metadata
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest
import tree_sitter
from repoatlas.models.parsing import GRAMMAR_UNAVAILABLE, EntityType, ParsedFile
from repoatlas.parsers import treesitter
from repoatlas.parsers.bash import BashParser
from repoatlas.parsers.grammars import GRAMMARS
from repoatlas.parsers.python import PythonParser
from repoatlas.parsers.treesitter import GrammarUnavailable, load_parser
from repoatlas.parsers.typescript import TypeScriptParser

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "repoatlas"

#: Anything that obtains grammars at run time, or a Ruby grammar (none is shipped).
DOWNLOADERS = ("tree_sitter_language_pack", "tree_sitter_languages", "tree_sitter_ruby")

#: Parser versions are part of fact identity; a grammar pin is parser behaviour.
#: If a pin moves, the matching parser's output must be re-characterized and its
#: version bumped, and this table updated in the same change.
#:
#: The converse is not true, and the TypeScript entries are why: the parser went
#: to 3 when it began extracting HTTP facts, with both grammars sitting still.
#: A parser may outrun its grammar; what this table forbids is a grammar moving
#: underneath a parser that stayed put.
PARSER_FOR_PIN = {
    ("tree-sitter-typescript", "0.23.2"): (TypeScriptParser, "4"),
    ("tree-sitter-javascript", "0.25.0"): (TypeScriptParser, "4"),
    ("tree-sitter-bash", "0.25.1"): (BashParser, "2"),
}


@pytest.fixture(autouse=True)
def _fresh_loader_cache() -> Iterator[None]:
    """``load_parser`` is module-level cached; no test may inherit or leak a parser."""
    load_parser.cache_clear()
    yield
    load_parser.cache_clear()


def _distributions() -> dict[str, str]:
    return {grammar.distribution: grammar.version for grammar in GRAMMARS.values()}


def _lock_packages() -> dict[str, dict[str, object]]:
    return {
        package["name"]: package
        for package in tomllib.loads((ROOT / "uv.lock").read_text())["package"]
    }


def core_requirement() -> str:
    """RepoAtlas's declared requirement on the parser core, whitespace removed.

    Shared with ``tests/packaging`` so the published artifact is checked against
    the same declaration this suite reads, rather than against a second copy of
    the expected version.
    """
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    core = [
        spec
        for spec in declared
        if re.split(r"[<>=!~;\[ ]", spec, maxsplit=1)[0].lower().replace("_", "-") == "tree-sitter"
    ]
    assert len(core) == 1, core
    return core[0].replace(" ", "")


def core_pin() -> str:
    """The single core version that requirement admits, or a failure saying so."""
    requirement = core_requirement()
    match = re.fullmatch(r"tree-sitter==([0-9][0-9A-Za-z.]*)", requirement)
    assert match, f"{requirement!r}: the parser core must be pinned with ==, not a range"
    return match.group(1)


# ---------------------------------------------------------------------------
# CASES 9 and 10: the pinned artifact, and only it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("language", sorted(GRAMMARS))
def test_every_shipped_grammar_loads_from_its_pinned_distribution(language: str) -> None:
    grammar = GRAMMARS[language]
    distribution = importlib.metadata.distribution(grammar.distribution)
    assert distribution.version == grammar.version

    parser = load_parser(language)
    assert parser.language is not None
    tree = parser.parse(b"x\n")
    assert tree.root_node is not None

    owned = {Path(str(distribution.locate_file(f))).resolve() for f in distribution.files or ()}
    module = sys.modules[grammar.module]
    binding = sys.modules[f"{grammar.module}._binding"]
    assert Path(str(module.__file__)).resolve() in owned
    assert Path(str(binding.__file__)).resolve() in owned


def test_pyproject_pins_every_grammar_exactly_and_nothing_else_supplies_one() -> None:
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    pins = {}
    for spec in declared:
        name = re.split(r"[<>=!~;\[ ]", spec, maxsplit=1)[0].lower().replace("_", "-")
        if name.startswith("tree-sitter-"):
            match = re.fullmatch(rf"{re.escape(name)}==([0-9][0-9A-Za-z.]*)", spec.replace(" ", ""))
            assert match, f"{spec!r}: a grammar must be pinned with ==, not a range"
            pins[name] = match.group(1)
    assert pins == _distributions()


def test_nothing_declares_a_ruby_grammar_or_a_downloader() -> None:
    """Ruby has no parser, and adding a grammar back is a decision, not a bump.

    RepoAtlas ships no Ruby grammar: the only offline-installable release,
    ``tree-sitter-ruby`` 0.23.1, predates upstream fix ``ad907a69`` for a heredoc
    scanner overflow that repository content controls, and RepoAtlas reads
    untrusted repositories. Ruby files are chunked for text search and their
    ``Gemfile`` / ``*.gemspec`` dependencies are read; nothing parses them.

    If this fails because a ``tree-sitter-ruby`` release was added: verify it
    contains ``ad907a69``, pin it exactly in ``GRAMMARS`` beside the others, add
    a parser that routes through the shipped loader, and characterize its output
    before trusting a single fact it produces.
    """
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    names = {
        re.split(r"[<>=!~;\[ ]", spec, maxsplit=1)[0].lower().replace("_", "-") for spec in declared
    }
    for forbidden in DOWNLOADERS:
        assert forbidden.replace("_", "-") not in names, forbidden
    assert "ruby" not in GRAMMARS


def test_the_lockfile_pins_and_hashes_every_grammar_artifact() -> None:
    packages = _lock_packages()
    for name in DOWNLOADERS:
        assert name.replace("_", "-") not in packages, name
    for distribution, version in _distributions().items():
        package = packages[distribution]
        assert package["version"] == version, distribution
        sdist = package["sdist"]
        wheels = package["wheels"]
        assert isinstance(sdist, dict) and str(sdist["hash"]).startswith("sha256:"), distribution
        assert isinstance(wheels, list) and wheels, distribution
        assert all(str(wheel["hash"]).startswith("sha256:") for wheel in wheels), distribution


def test_the_parser_core_is_pinned_to_exactly_one_version() -> None:
    """A range here is how a supported grammar becomes an unavailable one.

    The shipped JavaScript and Bash grammars were generated at a newer ABI than
    the shipped TypeScript one, and a core that predates that ABI cannot load
    them at all: it raises, the loader fails closed as it should, and those two
    languages degrade to ``parser grammar unavailable`` on an installation whose
    metadata was perfectly valid --- which is the failure OFF-01 exists to
    remove, arrived at by a resolver instead of by a missing download.

    Admitting exactly one version is what makes that resolution unreachable
    rather than merely unlikely, and the version admitted is the one this
    release characterized its parser output against. Moving it is a parser
    change: re-characterize first.
    """
    pinned = core_pin()
    assert _lock_packages()["tree-sitter"]["version"] == pinned
    assert importlib.metadata.version("tree-sitter") == pinned


def test_the_pinned_core_supports_the_abi_of_every_shipped_grammar() -> None:
    """Read off the installed artifacts, so there is no ABI table to maintain here.

    This does not stand in for the pin above: a correct answer here on this
    machine says nothing about what a user's resolver would install. It is the
    reason the pin is the version it is.
    """
    oldest = tree_sitter.MIN_COMPATIBLE_LANGUAGE_VERSION
    newest = tree_sitter.LANGUAGE_VERSION
    for language in sorted(GRAMMARS):
        grammar = load_parser(language).language
        assert grammar is not None
        supported = range(oldest, newest + 1)
        assert grammar.abi_version in supported, (
            f"{language}: grammar ABI {grammar.abi_version} is outside the pinned core's "
            f"supported range {supported.start}-{supported.stop - 1}"
        )


@pytest.mark.parametrize(("pin", "expected"), sorted(PARSER_FOR_PIN.items()))
def test_grammar_pins_and_parser_versions_move_together(
    pin: tuple[str, str], expected: tuple[type, str]
) -> None:
    distribution, version = pin
    parser, parser_version = expected
    assert _distributions()[distribution] == version, (
        f"{distribution} is no longer pinned at {version}: re-characterize {parser.__name__}'s "
        "output against the new grammar, bump its version, then update PARSER_FOR_PIN"
    )
    assert parser.version == parser_version


# ---------------------------------------------------------------------------
# CASE 6: no path to a download
# ---------------------------------------------------------------------------


def _imported_top_levels(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


def test_no_repoatlas_module_imports_a_grammar_downloader_or_loads_native_code() -> None:
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        imported = _imported_top_levels(path)
        for name in (*DOWNLOADERS, "ctypes", "cffi"):
            if name in imported:
                offenders.append(f"{path.relative_to(ROOT)} imports {name}")
    assert offenders == []


def test_the_loader_is_the_only_code_that_imports_a_grammar() -> None:
    grammar_modules = {grammar.module for grammar in GRAMMARS.values()}
    for path in sorted(PACKAGE.rglob("*.py")):
        imported = _imported_top_levels(path)
        assert not (imported & grammar_modules), path.relative_to(ROOT)
    assert "importlib" in _imported_top_levels(PACKAGE / "parsers" / "treesitter.py")


# The sync harness runs in a child interpreter: an audit hook cannot be removed
# once installed, and this one must not outlive the test.
_SYNC_SCRIPT = r"""
import json, os, sys
from pathlib import Path

root, state, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
watched = [Path(p).resolve() for p in json.loads(os.environ["OFF01_WATCHED"])]
forbidden = set(json.loads(os.environ["OFF01_FORBIDDEN"]))
# Events that reach, or resolve a name for, another host. Not socket.__new__ or
# gethostname, which name this machine and open nothing.
NETWORK = {
    "socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex",
    "socket.gethostbyaddr", "socket.getnameinfo", "socket.sendto", "socket.sendmsg",
}
events = []

def _under_watched(value):
    try:
        path = Path(os.fsdecode(value)).resolve()
    except Exception:
        return False
    return any(path == w or w in path.parents for w in watched)

def hook(event, args):
    touches_path = event in ("open", "os.listdir", "os.scandir", "ctypes.dlopen")
    if touches_path and args and _under_watched(args[0]):
        events.append([event, os.fsdecode(args[0])])
    elif event == "ctypes.dlopen" and args and args[0] is not None:
        # dlopen(None) is ctypes opening the running interpreter itself on import;
        # any named library is native code arriving from somewhere, and is recorded.
        events.append([event, str(args[0])])
    elif event in NETWORK:
        events.append([event, repr(args)[:120]])
    elif event == "import" and args and str(args[0]).split(".")[0] in forbidden:
        events.append([event, str(args[0])])

sys.addaudithook(hook)
sys.path.insert(0, os.environ["OFF01_ROOT"])

from repoatlas.config.settings import Settings
from repoatlas.services.init_service import initialize
from repoatlas.services.workspace import open_workspace
from tests.conftest import fact_snapshot

settings = Settings.model_validate(
    {
        "storage": {"database": str(state / "atlas.db")},
        "embeddings": {"provider": "hashing", "dim": 128},
    }
)
workspace = open_workspace(settings=settings, require_root=False)
report, _ = initialize(workspace, root, config_path=state / "config.yaml")
snapshot = fact_snapshot(workspace)
workspace.close()
diagnostics = sorted({(d.path, d.message) for r in report.repositories for d in r.diagnostics})
result = {"snapshot": snapshot, "events": events, "diagnostics": diagnostics}
out.write_text(json.dumps(result, default=str))
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture(scope="module")
def polyglot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("polyglot") / "repos"
    repo = root / "polyglot"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "service.ts").write_text(
        "import { host } from './config';\nexport function serve(): string {\n  return host();\n}\n"
    )
    (repo / "src" / "config.ts").write_text(
        "export function host(): string {\n  return process.env.SERVICE_HOST ?? '';\n}\n"
    )
    (repo / "src" / "view.tsx").write_text(
        "export function View() {\n  return <div>{1}</div>;\n}\n"
    )
    (repo / "src" / "legacy.js").write_text(
        "function legacy() {\n  return 1;\n}\nmodule.exports = legacy;\n"
    )
    (repo / "deploy.sh").write_text(
        '#!/usr/bin/env bash\nsource ./env.sh\ndeploy() {\n  echo "$SERVICE_HOST"\n}\n'
    )
    (repo / "env.sh").write_text("export SERVICE_HOST=localhost\n")
    (repo / "tool.rb").write_text("def tool\n  ENV['SERVICE_HOST']\nend\n")
    (repo / "app.py").write_text(
        "import os\n\n\ndef app():\n    return os.environ['SERVICE_HOST']\n"
    )
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "x")
    return root


def _poison(cache: Path) -> dict[str, str]:
    """A grammar cache laid out the way the old downloader laid it out, all corrupt."""
    libs = cache / "tree-sitter-language-pack" / "v1.16.1" / "libs"
    libs.mkdir(parents=True)
    for name in ("typescript", "tsx", "javascript", "bash", "ruby"):
        (libs / f"libtree_sitter_{name}.so").write_bytes(
            b"\x7fELF-not-really OFF01 poisoned grammar"
        )
    (libs.parent / "manifest.json").write_text('{"poisoned": true}')
    return {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(cache.rglob("*"))
        if p.is_file()
    }


def _sync_in_child(
    root: Path, state: Path, cache: Path, extra_env: dict[str, str]
) -> dict[str, object]:
    state.mkdir(parents=True)
    out = state / "result.json"
    script = state / "sync.py"
    script.write_text(_SYNC_SCRIPT)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(state / "home"),
        "XDG_CACHE_HOME": str(cache),
        "XDG_CONFIG_HOME": str(state / "config"),
        "XDG_DATA_HOME": str(state / "data"),
        "OFF01_ROOT": str(ROOT),
        "OFF01_WATCHED": json.dumps([str(cache), *extra_env.values()]),
        "OFF01_FORBIDDEN": json.dumps(DOWNLOADERS),
        **extra_env,
    }
    done = subprocess.run(
        [sys.executable, str(script), str(root), str(state), str(out)],
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(out.read_text())


def test_facts_are_identical_with_an_empty_or_a_poisoned_grammar_cache(
    polyglot: Path, tmp_path: Path
) -> None:
    """CASES 5 and 6, and §10: the user's cache directory decides nothing.

    One sync with an empty ``XDG_CACHE_HOME``; one with a cache poisoned the way the
    old downloader would have found it --- corrupt grammar binaries under its
    versioned layout, with its own environment variables pointing straight at
    them. If any of that were consulted, loading would fail or differ. The facts
    must be identical, no watched path may be opened, listed or ``dlopen``-ed, no
    downloader may be imported, no socket may be touched, and neither directory
    may be written.
    """
    empty = tmp_path / "empty-cache"
    empty.mkdir()
    clean = _sync_in_child(polyglot, tmp_path / "clean", empty, {})

    poisoned = tmp_path / "poisoned-cache"
    before = _poison(poisoned)
    libs = poisoned / "tree-sitter-language-pack" / "v1.16.1" / "libs"
    dirty = _sync_in_child(
        polyglot,
        tmp_path / "dirty",
        poisoned,
        {
            "TREE_SITTER_LANGUAGE_PACK_CACHE_DIR": str(poisoned),
            "TREE_SITTER_LANGUAGE_PACK_LIBS_DIR": str(libs),
        },
    )

    assert clean["snapshot"] == dirty["snapshot"]
    assert clean["diagnostics"] == dirty["diagnostics"]
    assert clean["events"] == [] and dirty["events"] == [], (clean["events"], dirty["events"])
    assert list(empty.rglob("*")) == []
    after = {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(poisoned.rglob("*"))
        if p.is_file()
    }
    assert after == before

    entities = {tuple(row) for row in clean["snapshot"]["entities"]}  # type: ignore[index]
    names = {row[2] for row in entities}
    assert {"serve", "host", "View", "legacy", "deploy", "app"} <= names, names
    assert "tool" not in names, "Ruby has no structural parser"
    messages = {message for _, message in clean["diagnostics"]}  # type: ignore[misc]
    assert GRAMMAR_UNAVAILABLE not in messages


# ---------------------------------------------------------------------------
# The origin checks, and CASE 12: failing closed still means something
# ---------------------------------------------------------------------------


class _RefuseReal(importlib.abc.MetaPathFinder):
    """Makes the shadow the only ``tree_sitter_bash`` the import system can see first."""

    def __init__(self, shadow_root: Path) -> None:
        self._finder = importlib.machinery.PathFinder
        self._root = str(shadow_root)

    def find_spec(
        self, fullname: str, path: object, target: object = None
    ) -> importlib.machinery.ModuleSpec | None:
        if fullname == "tree_sitter_bash":
            return self._finder.find_spec(fullname, [self._root])
        return None


def test_a_shadowing_grammar_module_is_refused_before_it_is_imported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-named package found first on the import path is never executed."""
    shadow = tmp_path / "shadow"
    (shadow / "tree_sitter_bash").mkdir(parents=True)
    executed = tmp_path / "executed"
    (shadow / "tree_sitter_bash" / "__init__.py").write_text(
        "from pathlib import Path\n"
        f"Path({executed.as_posix()!r}).write_text('ran')\n"
        "def language():\n    raise SystemExit\n"
    )
    monkeypatch.delitem(sys.modules, "tree_sitter_bash", raising=False)
    monkeypatch.delitem(sys.modules, "tree_sitter_bash._binding", raising=False)
    finder = _RefuseReal(shadow)
    sys.meta_path.insert(0, finder)
    try:
        with pytest.raises(GrammarUnavailable):
            load_parser("bash")
    finally:
        sys.meta_path.remove(finder)
    assert not executed.exists()


def test_a_binding_the_distribution_did_not_install_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    importlib.import_module("tree_sitter_bash")
    impostor = type(sys)("tree_sitter_bash._binding")
    impostor.__file__ = "/tmp/elsewhere/_binding.abi3.so"  # noqa: S108 - a path that is never opened
    monkeypatch.setitem(sys.modules, "tree_sitter_bash._binding", impostor)
    with pytest.raises(GrammarUnavailable):
        load_parser("bash")


def test_a_version_other_than_the_pin_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    real = importlib.metadata.distribution

    class Drifted:
        def __init__(self, inner: importlib.metadata.Distribution) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> object:
            return getattr(self._inner, name)

        @property
        def version(self) -> str:
            return "99.0.0"

    def distribution(name: str) -> object:
        found = real(name)
        return Drifted(found) if name == "tree-sitter-bash" else found

    monkeypatch.setattr(importlib.metadata, "distribution", distribution)
    with pytest.raises(GrammarUnavailable):
        load_parser("bash")
    parsed = BashParser().parse(Path("deploy.sh"), "deploy() {\n  echo hi\n}\n")
    assert [d.message for d in parsed.diagnostics] == [GRAMMAR_UNAVAILABLE]
    assert parsed.symbols == ()


def test_a_missing_grammar_distribution_is_a_fixed_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = importlib.metadata.distribution
    sentinel = "OFF01_PRIVATE_HOST_PATH_/home/someone/.cache/grammar.so"

    def distribution(name: str) -> importlib.metadata.Distribution:
        if name == "tree-sitter-typescript":
            raise importlib.metadata.PackageNotFoundError(sentinel)
        return real(name)

    monkeypatch.setattr(importlib.metadata, "distribution", distribution)
    parsed = TypeScriptParser().parse(Path("svc.ts"), "export const x = 1;\n")
    assert [d.message for d in parsed.diagnostics] == [GRAMMAR_UNAVAILABLE]
    assert sentinel not in parsed.model_dump_json()


@pytest.mark.parametrize("language", ["ruby", "cobol", "", "../typescript"])
def test_a_grammar_that_is_not_shipped_is_unavailable(language: str) -> None:
    with pytest.raises(GrammarUnavailable):
        load_parser(language)


def test_python_never_touches_tree_sitter(monkeypatch: pytest.MonkeyPatch) -> None:
    """CASE 4: the control. Python parsing needs no grammar at all."""

    def refuse(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Python parsing reached the grammar loader")

    monkeypatch.setattr(treesitter, "load_parser", refuse)
    monkeypatch.setattr(treesitter, "parse_tree", refuse)
    parsed: ParsedFile = PythonParser().parse(Path("app.py"), "def app():\n    return 1\n")
    assert any(s.kind is EntityType.FUNCTION and s.name == "app" for s in parsed.symbols)


# ---------------------------------------------------------------------------
# CASE 11: the published provenance is the verified provenance
# ---------------------------------------------------------------------------


def _notice_rows() -> dict[str, list[str]]:
    rows = {}
    for line in (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8").splitlines():
        cells = [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
        if len(cells) == 8 and cells[1].startswith("tree-sitter-"):
            rows[cells[1]] = cells
    return rows


def test_the_notices_publish_exactly_the_pinned_grammars_with_verified_hashes() -> None:
    rows = _notice_rows()
    packages = _lock_packages()
    assert set(rows) == set(_distributions())
    for grammar in GRAMMARS.values():
        _, distribution, version, upstream, tag, commit, license_id, sdist = rows[
            grammar.distribution
        ]
        assert (distribution, version, upstream, tag, commit, license_id) == (
            grammar.distribution,
            grammar.version,
            grammar.upstream,
            grammar.tag,
            grammar.commit,
            grammar.license,
        )
        assert f"sha256:{sdist}" == packages[grammar.distribution]["sdist"]["hash"]  # type: ignore[index]


def test_every_installed_grammar_carries_the_license_the_notices_reproduce() -> None:
    notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    for distribution in _distributions():
        installed = importlib.metadata.distribution(distribution)
        licenses = [
            f for f in installed.files or () if Path(str(f)).name.upper().startswith("LICENSE")
        ]
        assert licenses, f"{distribution} ships no license file"
        text = Path(str(installed.locate_file(licenses[0]))).read_text(encoding="utf-8")
        assert "MIT License" in text
        copyright_line = next(line for line in text.splitlines() if line.startswith("Copyright"))
        section = notices.split(f"### {distribution} {installed.version}", 1)
        assert len(section) == 2, f"no license section for {distribution}"
        assert copyright_line in section[1].split("### ", 1)[0], distribution
