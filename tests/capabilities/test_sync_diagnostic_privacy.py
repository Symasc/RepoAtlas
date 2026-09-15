"""What a sync diagnostic is allowed to say (RC audit, RCA-09).

A parse problem is reported to whoever asked for the sync, and a sync response is
a public machine surface. The problem itself is found by somebody else's code --- a
tree-sitter grammar loader, PyYAML, ``configparser``, ``tomllib``, CPython's own
parser --- and every one of those writes a message for a human reading a terminal,
not for a contract. RepoAtlas used to copy those messages through.

What that actually carried, measured rather than imagined:

* a grammar loader names the shared object it could not open, absolute path and
  all;
* ``configparser`` renders the offending source line **verbatim** into
  ``str(exc)`` --- ``"File contains no section headers.\\nfile: '<string>', line: 1\\n
  'SECRET = 1\\n'"``;
* PyYAML's marked errors render a snippet of the source around the failure;
* ``SyntaxError.msg`` quotes the token it tripped on --- ``"unmatched ']'"``,
  ``"f-string: invalid conversion character 'z'"``;
* ``ast.parse`` puts a filename into ``str(exc)``.

So a malformed file in a repository became a way to read that repository back out
through a diagnostic, and a misconfigured host became a way to learn its layout.
This is the third surface in the same family: RCA-03 was the error envelope,
RCA-08 the retrieval trace, and this is the sync report.

Two things are asserted here, and as in both earlier corrections they pull in
opposite directions:

* **nothing a library said crosses** --- no path, no sentinel, no source snippet,
  in any field, for any of the seven shapes below;
* **the diagnostic got more useful, not less** --- it still says which *category*
  of failure occurred, still carries the line when one is deterministically known,
  and now names the repository-relative path, which it never did before. The only
  file identity a caller used to get was the one a library happened to mention,
  which is exactly the wrong way round.

Everything is driven through ``CapabilityService.sync`` over a real repository.
The grammar failure is injected at the shipped grammars' bindings, so production's
own wrapper builds the ``GrammarUnavailable``; the library failures are injected
by writing malformed files, so the libraries produce their real messages.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from repoatlas.capabilities.errors import SyncFailedError, capability_error
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.graph.linker import ReferenceLinker
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.parsing import (
    GRAMMAR_UNAVAILABLE,
    INVALID_CFG,
    INVALID_JSON,
    INVALID_TOML,
    INVALID_YAML,
    PARSE_FAILED,
    STRUCTURAL_PARSING_DISABLED,
    SYNTAX_ERROR,
    Severity,
)
from repoatlas.models.sync import SyncRequest, SyncResponse, SyncStatus
from repoatlas.parsers import treesitter
from repoatlas.services.init_service import initialize
from repoatlas.services.workspace import open_workspace

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)


# --- sentinels, one per shape of private thing a message can carry ---------
POSIX_PATH = "/home/private-user/.cache/repoatlas/private.so"
WINDOWS_PATH = r"C:\Users\PrivateUser\AppData\Local\RepoAtlas\private.dll"
UNC_PATH = r"\\private-server\share\grammar.dll"
SECRET = "RCA09_SECRET_SENTINEL_918273"  # noqa: S105 - a marker, not a secret
CREDENTIAL_URL = "https://private-user:super-secret@example.invalid/schema"
DATABASE_PATH = "/var/lib/repoatlas/private-index.sqlite"

SENTINELS = [
    pytest.param(POSIX_PATH, id="posix-path"),
    pytest.param(WINDOWS_PATH, id="windows-path"),
    pytest.param(UNC_PATH, id="unc-path"),
    pytest.param(SECRET, id="secret"),
    pytest.param(CREDENTIAL_URL, id="credential-url"),
    pytest.param(DATABASE_PATH, id="database-path"),
]

#: Planted inside every malformed file, in the place each library quotes back.
SOURCE = "RCA09_SOURCE_SENTINEL_PRIVATE_CODE"

#: The audit's own grammar reproduction, verbatim.
AUDIT_GRAMMAR_MESSAGE = (
    "cannot load /home/private-user/.cache/repoatlas/PRIVATE_GRAMMAR.so "
    "RCA09_SECRET_SENTINEL_918273"
)

#: One file per library that can speak in a diagnostic, each malformed in the way
#: that makes that library quote the source back.
HOSTILE: dict[str, str | bytes] = {
    "app.ts": "export const x = 1;\n",
    "script.sh": "echo hi\n",
    "lib.rb": "module M\nend\n",
    "setup.cfg": f"{SOURCE} = 1\n",
    "pnpm-lock.yaml": f"a: 1\n  {SOURCE}: [\n",
    "pyproject.toml": f"[project]\nname = {SOURCE}\n",
    "setup.py": f"x = [{SOURCE} {SOURCE}]\n",
    "package.json": '{"name": }\n',
    "broken.py": f"x = [{SOURCE} {SOURCE}]\n",
    "conf.yaml": f"a: 1\n  {SOURCE}: [\n",
}


# ---------------------------------------------------------------------------
# Reading a response the way a leak would actually appear in one
# ---------------------------------------------------------------------------


def walk(value: object) -> list[str]:
    """Every string anywhere in the structure, unescaped.

    ``json.dumps`` escapes a backslash, so a Windows or UNC path searched for in
    serialized text can never match the form it was injected in. A sentinel that
    cannot match is a test that cannot fail, and this suite exists to catch leaks
    of exactly that shape.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [part for item in value.items() for part in walk(item)]
    if isinstance(value, (list, tuple)):
        return [part for item in value for part in walk(item)]
    return [repr(value)]


def every_string(response: SyncResponse) -> str:
    return "\u0000".join(walk(response.model_dump(mode="json")))


def diagnostics(response: SyncResponse) -> list[Any]:
    return [d for view in response.repositories for d in view.diagnostics]


def messages(response: SyncResponse) -> set[str]:
    return {d.message for d in diagnostics(response)}


def by_path(response: SyncResponse) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for diagnostic in diagnostics(response):
        found.setdefault(diagnostic.path or "<repository>", set()).add(diagnostic.message)
    return found


# ---------------------------------------------------------------------------
# A repository full of things RepoAtlas cannot parse
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.fixture
def grammar_fails(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str], None]]:
    """Make tree-sitter grammar loading fail with a message of the test's choosing.

    Patched at each shipped grammar's own binding function rather than at the
    diagnostic, so production's own ``except Exception as exc: raise
    GrammarUnavailable(...)`` wrapper is what builds the exception. A test that
    constructed the wrapper itself would be asserting its own idea of the message.

    The cache is cleared on both sides: it is module-level and would otherwise
    leak a broken parser into whatever runs next.
    """
    import importlib

    from repoatlas.parsers.grammars import GRAMMARS

    def install(message: str) -> None:
        def refuse(*_args: object, **_kwargs: object) -> object:
            raise ImportError(message)

        for grammar in GRAMMARS.values():
            module = importlib.import_module(grammar.module)
            monkeypatch.setattr(module, grammar.function, refuse)
        treesitter.load_parser.cache_clear()

    treesitter.load_parser.cache_clear()
    yield install
    treesitter.load_parser.cache_clear()


@pytest.fixture
def hostile_repository(tmp_path: Path) -> Callable[..., SyncResponse]:
    """Index a repository of unparseable files and return the sync response.

    The files are committed as placeholders and only then made malformed, because
    the run under test has to be the one that parses them: ``initialize`` syncs
    too, and its diagnostics are not what a V2 caller sees.
    """

    def run(
        *, files: dict[str, str | bytes] | None = None, keep: CapabilityService | None = None
    ) -> Any:
        content = HOSTILE if files is None else files
        base = tmp_path / "lab"
        repo = base / "repos" / "app"
        repo.mkdir(parents=True)
        for name in content:
            (repo / name).write_text("placeholder\n", encoding="utf-8")
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "a@b.invalid")
        _git(repo, "config", "user.name", "A")
        _git(repo, "config", "commit.gpgsign", "false")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "x")

        settings = Settings.model_validate(
            {
                "storage": {"database": str(base / "state" / "atlas.db")},
                "embeddings": {"provider": "hashing", "dim": 128},
            }
        )
        workspace = open_workspace(settings=settings, require_root=False)
        initialize(workspace, base / "repos", config_path=base / "config.yaml")

        for name, text in content.items():
            if isinstance(text, bytes):
                (repo / name).write_bytes(text)
            else:
                (repo / name).write_text(text, encoding="utf-8")

        service = CapabilityService(workspace)
        if keep is not None:  # pragma: no cover - not used; keeps the signature honest
            raise AssertionError("one service per workspace")
        try:
            return service.sync(SyncRequest(context=GLOBAL, full=True))
        finally:
            service.close()

    return run


@pytest.fixture
def synced(
    hostile_repository: Callable[..., SyncResponse], grammar_fails: Callable[[str], None]
) -> SyncResponse:
    """The audit's reproduction: every hostile file, and no grammars."""
    grammar_fails(AUDIT_GRAMMAR_MESSAGE)
    return hostile_repository()


# ---------------------------------------------------------------------------
# CASES 1-6: nothing a library said crosses
# ---------------------------------------------------------------------------


def test_the_grammar_reproduction_no_longer_discloses_the_loader_path(
    synced: SyncResponse,
) -> None:
    """CASE 1. The grammar route the previous audit named as concrete.

    The sync still completes and the failure is still reported --- what changed is
    that the report no longer describes the operator's filesystem.
    """
    assert synced.status is SyncStatus.COMPLETED
    assert synced.files_indexed > 0

    body = every_string(synced)
    assert "/home/private-user/.cache/repoatlas/PRIVATE_GRAMMAR.so" not in body
    assert "PRIVATE_GRAMMAR" not in body
    assert "cannot load" not in body
    assert GRAMMAR_UNAVAILABLE in messages(synced)


def test_the_secret_sentinel_no_longer_crosses(synced: SyncResponse) -> None:
    """CASE 2."""
    assert SECRET not in every_string(synced)


def test_no_repository_source_is_echoed_back_through_a_diagnostic(
    synced: SyncResponse,
) -> None:
    """CASE 6, and the sharpest half of the finding.

    ``configparser`` and PyYAML quote the offending source line into their
    messages, so a malformed file used to read part of itself back out through the
    sync report --- an alternate supply route with none of the admission,
    truncation or ledgering that evidence goes through.

    The file itself is still indexed and still searchable on the ordinary path.
    The rule is about the diagnostic, not about the repository.
    """
    body = every_string(synced)

    assert SOURCE not in body
    assert INVALID_CFG in messages(synced), "setup.cfg must still report a failure"
    assert INVALID_YAML in messages(synced), "pnpm-lock.yaml must still report a failure"


@pytest.mark.parametrize("sentinel", SENTINELS)
def test_no_shape_of_private_text_crosses(
    hostile_repository: Callable[..., SyncResponse],
    grammar_fails: Callable[[str], None],
    sentinel: str,
) -> None:
    """CASES 1-5 generalized: every shape, over every field of the response.

    Parametrized because the failure mode being guarded is *partial* coverage: a
    fix that handles POSIX paths and lets a backslash through would pass a suite
    written around one Linux-shaped path, and that is the mistake RCA-03 found in
    its own tests.
    """
    grammar_fails(f"cannot load {sentinel}")
    response = hostile_repository()
    body = every_string(response)

    assert sentinel not in body
    for fragment in sentinel.replace("\\", "/").split("/"):
        if len(fragment) > 8:
            assert fragment not in body, fragment


def test_no_exception_class_name_or_traceback_crosses(synced: SyncResponse) -> None:
    """CASES 13 and 14. Not the text, and not the wrapper around it either.

    A class name is a smaller leak than a path and it is still a leak: it names
    the library RepoAtlas is built on, and it is the shape a sanitization decays
    into when someone decides a little context would be harmless.
    """
    said = "\u0000".join(sorted(messages(synced)))

    for name in (
        "ImportError",
        "GrammarUnavailable",
        "SyntaxError",
        "TOMLDecodeError",
        "YAMLError",
        "JSONDecodeError",
        "configparser",
        "tomllib",
        "pyyaml",
        "tree_sitter",
        "Error(",
    ):
        assert name not in said, name

    # Chained causes and tracebacks are checked over the whole response: unlike a
    # library's name, none of them has any business in any field at all.
    body = every_string(synced)
    for shape in ("Traceback", "The above exception", "During handling of", 'File "'):
        assert shape not in body, shape


def test_no_diagnostic_message_is_built_from_an_exception() -> None:
    """The structural half, so the guarantee cannot erode quietly.

    Absence in one response proves today's behaviour. This proves the shape: no
    diagnostic anywhere is interpolated from an exception, so the property holds
    for failures this suite has never produced --- and for the ones a future
    library version invents.
    """
    unsafe = ("message=str(exc)", "message=repr(exc)", "{exc}", "{exc!r}", "exc.args")
    roots = [Path("repoatlas/parsers"), Path("repoatlas/dependencies")]

    for root in roots:
        for module in sorted(root.rglob("*.py")):
            source = module.read_text(encoding="utf-8")
            for index, line in enumerate(source.splitlines(), start=1):
                if "ParseDiagnostic(" not in source:
                    continue
                for pattern in unsafe:
                    assert pattern not in line, f"{module}:{index} interpolates an exception"


# ---------------------------------------------------------------------------
# CASE 7 and CASE 12: the diagnostic got more useful, not less
# ---------------------------------------------------------------------------


def test_every_failure_category_is_still_distinguishable(synced: SyncResponse) -> None:
    """CASE 7. One message per kind of failure, not one message for all of them.

    "The grammar is missing", "this is not valid TOML" and "this file has a syntax
    error" call for three different actions. Collapsing them into "parse failed"
    would trade a privacy defect for a diagnostic one, which is not a trade.
    """
    found = messages(synced)

    for expected in (
        GRAMMAR_UNAVAILABLE,
        SYNTAX_ERROR,
        PARSE_FAILED,
        INVALID_JSON,
        INVALID_YAML,
        INVALID_TOML,
        INVALID_CFG,
        "YAML parse failed",
    ):
        assert expected in found, expected
    assert len(found) >= 8


def test_every_file_diagnostic_names_the_file_it_is_about(synced: SyncResponse) -> None:
    """CASE 12. The identity RepoAtlas admitted, not the one a library mentioned.

    Before the correction the only file hint a caller got was whatever the
    exception happened to say --- ``ast.parse`` names a file, ``tomllib`` names
    none --- which is both unreliable and, on some paths, absolute. The path is now
    stamped from what the sync admitted.
    """
    located = by_path(synced)

    assert "app.ts" in located and GRAMMAR_UNAVAILABLE in located["app.ts"]
    assert "script.sh" in located and GRAMMAR_UNAVAILABLE in located["script.sh"]
    # Ruby is gated before any grammar is consulted (OFF-01), so it reports the
    # policy, not the injected loader failure --- and still names its file.
    assert "lib.rb" in located and STRUCTURAL_PARSING_DISABLED in located["lib.rb"]
    assert "setup.cfg" in located and INVALID_CFG in located["setup.cfg"]
    assert "pyproject.toml" in located and INVALID_TOML in located["pyproject.toml"]
    assert "pnpm-lock.yaml" in located and INVALID_YAML in located["pnpm-lock.yaml"]
    assert "package.json" in located and INVALID_JSON in located["package.json"]

    for diagnostic in diagnostics(synced):
        assert diagnostic.path is not None, diagnostic.message
        assert not Path(diagnostic.path).is_absolute()
        assert ".." not in Path(diagnostic.path).parts


def test_the_line_number_survives_where_it_was_known(synced: SyncResponse) -> None:
    """A line is a number, so it is the one thing an exception may still supply."""
    located = {(d.path, d.message): d.line for d in diagnostics(synced)}

    assert located[("broken.py", SYNTAX_ERROR)] == 1
    assert located[("package.json", INVALID_JSON)] == 1
    assert located[("conf.yaml", "YAML parse failed")] == 2


# ---------------------------------------------------------------------------
# CASE 8: diagnostics that were always safe are untouched
# ---------------------------------------------------------------------------


def test_diagnostics_that_never_quoted_a_library_are_unchanged(
    hostile_repository: Callable[..., SyncResponse],
) -> None:
    """CASE 8. The correction targets exception-derived detail and nothing else.

    Each of these is RepoAtlas's own sentence over its own observations, and each
    should read exactly as it did before.
    """
    response = hostile_repository(
        files={
            "binary.py": b"\xff\xfe not utf-8",
            "setup.py": "from setuptools import setup\nsetup(install_requires=COMPUTED)\n",
        }
    )
    found = messages(response)

    assert "not valid UTF-8" in found
    assert any(
        "is computed, not a literal list" in message and "install_requires" in message
        for message in found
    ), found


# ---------------------------------------------------------------------------
# CASES 10 and 11: the two boundaries stay apart
# ---------------------------------------------------------------------------


def test_a_parser_diagnostic_is_still_not_a_failed_sync(synced: SyncResponse) -> None:
    """CASE 10. Sanitizing changed what is said, never what happens.

    V1_SPEC §7 makes a parse problem a diagnostic and never an aborted sync. Ten
    unparseable files in one repository and the sync still completes, still
    indexes them, and still reports every one.
    """
    assert synced.status is SyncStatus.COMPLETED
    assert synced.files_indexed == len(HOSTILE)
    assert len(diagnostics(synced)) >= len(HOSTILE)
    assert {d.severity for d in diagnostics(synced)} == {Severity.ERROR, Severity.WARNING}


def test_a_real_sync_failure_is_still_sync_failed(
    initialized: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CASE 11. A fatal failure did not become a tidy repository diagnostic.

    The two are answered by different machinery on purpose --- one is a field of a
    successful response, the other is RCA-03's error envelope --- and a correction
    that shares wording between them must not start sharing the routing.
    """
    service = CapabilityService(initialized)

    def failing_commit(self: ReferenceLinker, plan: object) -> object:
        raise RuntimeError(f"injected failure naming {POSIX_PATH} {SECRET}")

    monkeypatch.setattr(ReferenceLinker, "commit_global", failing_commit)
    try:
        with pytest.raises(SyncFailedError) as raised:
            service.sync(SyncRequest(context=GLOBAL, full=True))
    finally:
        monkeypatch.undo()
        service.close()

    error = capability_error(raised.value)
    assert error.code == "SYNC_FAILED"
    envelope = "\u0000".join(walk(error.model_dump(mode="json")))
    assert POSIX_PATH not in envelope
    assert SECRET not in envelope
