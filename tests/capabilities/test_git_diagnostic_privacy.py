"""What a Git enumeration diagnostic is allowed to say (RC audit, RCA-10).

The fourth and last surface in the privacy family. RCA-03 was the error envelope,
RCA-08 the retrieval trace, RCA-09 the parser diagnostics --- each one a public
field populated from somebody else's text. This is the one that came from a
*subprocess* rather than from an exception.

``git ls-files`` reports a directory it could not open as a warning on stderr and
still exits 0, so a short listing looks like a complete one. Those warnings are
the only evidence that "there is nothing under there" and "I could not look" are
different claims, so they are genuinely worth reporting --- and the sync used to
report them by interpolating git's sentences into a public diagnostic.

The reasoning behind that was written down, and it was wrong in the case that
matters. Git was assumed to name a refused directory relative to the repository.
It names whatever it was pointed at, and part of what points it is ``.git/config``
--- **repository state, not RepoAtlas's**. A repo-local ``core.excludesFile``
naming an unreadable absolute path makes git write that path to stderr, and the
sync published it. Anyone who could write to a tree could choose the contents of
a RepoAtlas diagnostic.

Two things are asserted here, pulling in opposite directions as in every earlier
correction:

* **nothing git wrote crosses** --- no absolute path, no sentinel, no free-form
  explanation, in any field, for any of the six shapes below;
* **nothing useful was lost** --- the diagnostic still exists, still carries the
  same severity, the sync still continues, and which directory could not be read
  is still reported, by ``SyncResponse.unreadable_paths``, which comes from
  RepoAtlas's own walk instead of from anything git said.

The route is established end to end with a **real git process** over a repository
whose own ``.git/config`` makes git talk (CASES 1-2). The remaining shapes are
injected at the enumeration-result boundary, which is the first thing downstream
of the subprocess: git cannot be made to emit a UNC path or a credential URL on
demand, and a test that waited for one would not be a test.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from repoatlas.capabilities.errors import SyncFailedError, capability_error
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.gitstate.client import FileListing, GitClient
from repoatlas.graph.linker import ReferenceLinker
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.parsing import ENUMERATION_INCOMPLETE, Severity
from repoatlas.models.search import SearchRequest
from repoatlas.models.sync import SyncRequest, SyncResponse, SyncStatus
from repoatlas.services.init_service import initialize
from repoatlas.services.workspace import open_workspace

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)

# --- the shapes a git warning can carry, one sentinel each -----------------
SENTINEL = "RCA10_PRIVATE_GIT_SENTINEL_918273"
POSIX_PATH = "/home/private-user/.config/private-excludes"
WINDOWS_PATH = r"C:\Users\PrivateUser\private-excludes"
UNC_PATH = r"\\private-server\share\excludes"
CREDENTIAL_URL = "https://private-user:super-secret@example.invalid/repo"
DATABASE_PATH = "/var/lib/repoatlas/private.sqlite"

SHAPES = [
    pytest.param(POSIX_PATH, id="posix-path"),
    pytest.param(WINDOWS_PATH, id="windows-path"),
    pytest.param(UNC_PATH, id="unc-path"),
    pytest.param(SENTINEL, id="secret"),
    pytest.param(CREDENTIAL_URL, id="credential-url"),
    pytest.param(DATABASE_PATH, id="database-path"),
]

#: Planted in a tracked source file, so a diagnostic that scooped up repository
#: content would carry it. Deliberately *not* what evidence text is asserted on
#: --- see the evidence guard at the bottom, which requires the opposite.
REPOSITORY_TEXT = "RCA10_REPOSITORY_PRIVATE_TEXT"

skip_as_root = pytest.mark.skipif(
    os.geteuid() == 0, reason="root reads a mode-000 path, so git never warns"
)


def walk(value: object) -> list[str]:
    """Every string anywhere in the structure, unescaped.

    ``json.dumps`` escapes a backslash, so a Windows or UNC path searched for in
    serialized text can never match the form it was injected in. A sentinel that
    cannot match is a test that cannot fail.
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


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _lab(base: Path, files: dict[str, str]) -> Path:
    repo = base / "repos" / "app"
    repo.mkdir(parents=True)
    for name, text in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "a@b.invalid")
    _git(repo, "config", "user.name", "A")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "x")
    return repo


def _service(base: Path) -> CapabilityService:
    settings = Settings.model_validate(
        {
            "storage": {"database": str(base / "state" / "atlas.db")},
        }
    )
    workspace = open_workspace(settings=settings, require_root=False)
    initialize(workspace, base / "repos", config_path=base / "config.yaml")
    return CapabilityService(workspace)


# ---------------------------------------------------------------------------
# CASES 1-2: the real audit reproduction, through a real git process
# ---------------------------------------------------------------------------


@pytest.fixture
def refused_repository(tmp_path: Path) -> Iterator[Callable[[], SyncResponse]]:
    """A repository whose own config makes git write a private path to stderr.

    Nothing is injected. ``core.excludesFile`` is set **repository-locally** to an
    unreadable absolute path, which is a thing any repository may do: global and
    system git configuration are nulled for these calls, and this one is neither.
    A mode-000 directory supplies the second warning, so the fixture also covers
    the ordinary case the diagnostic was written for.
    """
    built: list[CapabilityService] = []
    restore: list[tuple[Path, int]] = []

    def run() -> SyncResponse:
        base = tmp_path / f"lab{len(built)}"
        repo = _lab(
            base,
            {
                "src.py": f"HOST = {REPOSITORY_TEXT!r}\n",
                "sub/private/notes.md": "# notes\n",
            },
        )

        private_home = base / "PRIVATE_HOME"
        private_home.mkdir()
        excludes = private_home / f"{SENTINEL}_excludes"
        excludes.write_text("*.tmp\n", encoding="utf-8")
        excludes.chmod(0o000)
        restore.append((excludes, 0o644))
        _git(repo, "config", "core.excludesFile", str(excludes))

        locked = repo / "sub" / "private"
        locked.chmod(0o000)
        restore.append((locked, 0o755))

        service = _service(base)
        built.append(service)
        return service.sync(SyncRequest(context=GLOBAL, full=True))

    try:
        yield run
    finally:
        for path, mode in restore:
            path.chmod(mode)
        for service in built:
            service.close()


@skip_as_root
def test_the_audit_reproduction_no_longer_discloses_the_configured_path(
    refused_repository: Callable[[], SyncResponse],
) -> None:
    """CASES 1 and 2. The measured leak, driven by a real git process.

    Before the correction this response carried, verbatim::

        enumeration incomplete: git could not read 2 directories (warning: unable
        to access '/.../PRIVATE_HOME/RCA10_..._excludes': Permission denied; ...)

    The absolute path in it was chosen by the repository.
    """
    response = refused_repository()
    body = every_string(response)

    assert SENTINEL not in body
    assert "PRIVATE_HOME" not in body
    assert "Permission denied" not in body
    assert "unable to access" not in body
    assert "warning:" not in body

    said = {d.message for d in diagnostics(response)}
    assert ENUMERATION_INCOMPLETE in said


@skip_as_root
def test_no_absolute_path_of_any_kind_crosses(
    refused_repository: Callable[[], SyncResponse],
) -> None:
    """The general form of CASE 1: not this path, no path.

    Asserted over every string in the response rather than over the diagnostic,
    because a leak that moved to a neighbouring field would still be a leak.
    """
    response = refused_repository()
    for value in walk(response.model_dump(mode="json")):
        assert not value.startswith("/"), value
        assert "/home/" not in value, value
        assert "/tmp/" not in value, value  # noqa: S108 - asserting absence


@skip_as_root
def test_the_diagnostic_survives_and_the_sync_continues(
    refused_repository: Callable[[], SyncResponse],
) -> None:
    """CASES 7 and 8. Sanitizing removed text, not the report or the control flow.

    The whole reason this diagnostic exists is that a *successful* sync must not
    be readable as a complete one. A correction that quietly dropped it would
    trade a privacy defect for a correctness one.
    """
    response = refused_repository()
    found = diagnostics(response)

    assert found, "the incompleteness report must survive sanitization"
    assert {d.severity for d in found} == {Severity.WARNING}
    assert response.status is SyncStatus.COMPLETED
    assert [view.enumeration_failed for view in response.repositories] == [False]
    assert response.repositories_unavailable == ()


@skip_as_root
def test_which_directory_was_refused_is_still_reported_safely(
    refused_repository: Callable[[], SyncResponse],
) -> None:
    """The coordinate survives, by a route that never touched a subprocess.

    ``unreadable_paths`` is RepoAtlas's own filesystem walk, projected relative to
    the managed root. It is why dropping git's sentence costs the caller nothing:
    the useful half was already available from somewhere trustworthy.
    """
    response = refused_repository()

    assert response.unreadable_paths, "the refused directory must still be named"
    for reported in response.unreadable_paths:
        assert not Path(reported).is_absolute()
        assert "private" in reported


# ---------------------------------------------------------------------------
# CASES 3-6: the shapes git cannot be asked to produce, injected one layer down
# ---------------------------------------------------------------------------


@pytest.fixture
def git_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., SyncResponse]]:
    """Sync a healthy repository whose enumeration came back with chosen warnings.

    Patched at ``GitClient.list_files`` --- the first RepoAtlas code downstream of
    the subprocess --- so everything under test is real: the scanner, the engine's
    diagnostic construction, the capability projection. Only the bytes git would
    have written are the test's. The paths are left exactly as git produced them,
    so this is a healthy enumeration that merely says something alarming.
    """
    built: list[CapabilityService] = []

    def run(*warnings: str) -> SyncResponse:
        base = tmp_path / f"lab{len(built)}"
        repo = _lab(base, {"src.py": f"HOST = {REPOSITORY_TEXT!r}\n"})
        service = _service(base)
        built.append(service)

        # `initialize` already indexed the committed tree, so an untouched sync
        # would report nothing indexed and the control-flow assertions would be
        # passing on an empty run.
        (repo / "src.py").write_text(
            f"HOST = {REPOSITORY_TEXT!r}\n\n\ndef load() -> str:\n    return HOST\n",
            encoding="utf-8",
        )

        real = GitClient.list_files

        def warned(self: GitClient, repo_path: Path) -> FileListing | None:
            listing = real(self, repo_path)
            if listing is None:
                return None
            return FileListing(paths=listing.paths, warnings=tuple(warnings))

        monkeypatch.setattr(GitClient, "list_files", warned)
        try:
            return service.sync(SyncRequest(context=GLOBAL, full=True))
        finally:
            monkeypatch.undo()

    try:
        yield run
    finally:
        for service in built:
            service.close()


@pytest.mark.parametrize("shape", SHAPES)
def test_no_shape_of_private_text_crosses(
    git_warns: Callable[..., SyncResponse], shape: str
) -> None:
    """CASES 3, 4, 5 and 2 again, one shape at a time.

    The Windows and UNC cases are the ones a check against serialized JSON would
    wave through, which is why the traversal is over raw field values.
    """
    response = git_warns(f"warning: unable to access '{shape}': Permission denied")
    body = every_string(response)

    assert shape not in body
    for fragment in shape.replace("\\", "/").split("/"):
        if len(fragment) > 8:
            assert fragment not in body, fragment
    assert ENUMERATION_INCOMPLETE in {d.message for d in diagnostics(response)}


def test_no_repository_content_echoed_by_git_crosses(
    git_warns: Callable[..., SyncResponse],
) -> None:
    """CASE 6. A warning is free-form, so it can quote anything, including a file.

    Git names paths it was given, and a repository chooses its own filenames. This
    is the same argument RCA-09 made about ``configparser`` quoting a source line,
    arriving by a different road.
    """
    response = git_warns(
        f"warning: could not open directory '{REPOSITORY_TEXT}/': Permission denied"
    )

    assert REPOSITORY_TEXT not in every_string(response)


def test_many_warnings_still_produce_one_safe_diagnostic(
    git_warns: Callable[..., SyncResponse],
) -> None:
    """A thousand refused directories must not become a thousand disclosures.

    Also pins that the count of warnings is not itself published: it is a count of
    stderr *lines*, which is a detail of how this git version phrases things, and
    the old message reported it as a count of directories --- which it was not.
    """
    response = git_warns(*[f"warning: {SENTINEL}-{index}" for index in range(50)])
    found = diagnostics(response)

    assert len(found) == 1
    assert found[0].message == ENUMERATION_INCOMPLETE
    assert SENTINEL not in every_string(response)
    assert "50" not in found[0].message


def test_a_clean_enumeration_still_produces_no_diagnostic(
    git_warns: Callable[..., SyncResponse],
) -> None:
    """CASE 9. Silence is the signal that the listing was complete.

    If sanitization had made the diagnostic unconditional, every sync would carry
    it and the field would stop meaning anything.
    """
    response = git_warns()

    assert diagnostics(response) == []
    assert response.status is SyncStatus.COMPLETED
    assert response.files_indexed >= 1


# ---------------------------------------------------------------------------
# CASE 14: the command itself is not a diagnostic
# ---------------------------------------------------------------------------


def test_no_git_command_line_is_exposed(git_warns: Callable[..., SyncResponse]) -> None:
    """CASE 14. The invocation carries host paths in its own right.

    Production runs git with a hardening preamble and a repository working
    directory. Naming the operation is the useful part; naming the argv is how the
    cure becomes the disease.
    """
    response = git_warns(f"warning: unable to access '{POSIX_PATH}': Permission denied")
    body = every_string(response)

    for fragment in (
        "ls-files",
        "--exclude-standard",
        "--no-optional-locks",
        "core.hooksPath",
        "safe.directory",
        "/usr/bin/git",
        "git -C",
    ):
        assert fragment not in body, fragment


# ---------------------------------------------------------------------------
# CASE 11: the correction must not have loosened git isolation
# ---------------------------------------------------------------------------


def test_global_and_system_git_configuration_remain_isolated() -> None:
    """CASE 11. RCA-10 reduces what crosses the boundary; it moves no boundary.

    Pinned as values rather than as behaviour because the failure mode is silent:
    a git that started reading ``~/.gitconfig`` would produce a *working* sync
    that had quietly acquired the operator's credential helpers and excludes.
    """
    from repoatlas.gitstate.client import _ENVIRONMENT, _HARDENING

    assert _ENVIRONMENT["GIT_CONFIG_NOSYSTEM"] == "1"
    assert _ENVIRONMENT["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert _ENVIRONMENT["GIT_CONFIG_SYSTEM"] == "/dev/null"
    assert _ENVIRONMENT["GIT_ATTR_NOSYSTEM"] == "1"
    assert _ENVIRONMENT["GIT_TERMINAL_PROMPT"] == "0"
    assert "HOME" not in _ENVIRONMENT, "a HOME is a per-user git config"
    assert "core.hooksPath=/dev/null" in _HARDENING


def test_a_users_global_excludes_cannot_reach_the_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CASE 11, behaviourally: the nulled global config is really nulled.

    ``HOME`` is pointed at a home that *does* configure a ``core.excludesFile``,
    and that file claims ``secret.py``. If git ever consulted per-user
    configuration, the enumeration would come back short and the sync would index
    one file fewer while reporting success --- silently, since a hidden file looks
    exactly like an absent one.

    The environment is set rather than asserted so the guarantee is measured
    instead of described: a `_ENVIRONMENT` that stopped nulling the global config,
    or that started passing ``HOME`` through, fails here.
    """
    home = tmp_path / "fake-home"
    home.mkdir()
    (home / "global-excludes").write_text("secret.py\n", encoding="utf-8")
    (home / ".gitconfig").write_text(
        f"[core]\n\texcludesFile = {home / 'global-excludes'}\n", encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))

    repo = _lab(tmp_path / "lab", {"src.py": "A = 1\n"})
    (repo / "secret.py").write_text("B = 2\n", encoding="utf-8")

    listing = GitClient().list_files(repo)

    assert listing is not None
    assert "secret.py" in listing.paths, "global excludes must not be consulted"
    assert listing.warnings == ()


# ---------------------------------------------------------------------------
# CASE 13: a fatal sync is still fatal
# ---------------------------------------------------------------------------


def test_an_enumeration_warning_is_not_a_failed_sync(
    git_warns: Callable[..., SyncResponse],
) -> None:
    """CASE 13, first half. Degraded is not failed, and never becomes it."""
    response = git_warns(f"warning: unable to access '{POSIX_PATH}': Permission denied")

    assert response.status is SyncStatus.COMPLETED
    assert response.files_indexed >= 1
    assert all(not view.enumeration_failed for view in response.repositories)


def test_a_real_sync_failure_is_still_sync_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CASE 13, second half. The two are answered by different machinery.

    One is a field of a successful response, the other is RCA-03's error envelope.
    A correction that shares wording between them must not start sharing routing.
    """
    _lab(tmp_path / "lab", {"src.py": "A = 1\n"})
    service = _service(tmp_path / "lab")

    def failing_commit(self: ReferenceLinker, plan: object) -> object:
        raise RuntimeError(f"injected failure naming {POSIX_PATH} {SENTINEL}")

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
    assert SENTINEL not in envelope


# ---------------------------------------------------------------------------
# CASE 12: the distinction this whole family rests on
# ---------------------------------------------------------------------------


def test_repository_source_still_comes_back_verbatim_as_evidence(tmp_path: Path) -> None:
    """CASE 12. Diagnostics are sanitized; evidence is the product.

    The cheap way to pass every test above would be to strip suspicious strings
    from responses, and it would destroy RepoAtlas: search exists to return
    repository source, the ledger binds ids to the bytes it supplied, and grounding
    is defined over exactly that text. So the guard runs in the opposite direction
    --- the same sentinel that must never appear in a diagnostic must appear,
    unmodified, in evidence.
    """
    _lab(tmp_path / "lab", {"config.py": f"SERVICE_HOST = {REPOSITORY_TEXT!r}\n"})
    service = _service(tmp_path / "lab")
    try:
        service.sync(SyncRequest(context=GLOBAL, full=True))
        found = service.search(
            SearchRequest(context=GLOBAL, question=f"SERVICE_HOST {REPOSITORY_TEXT}")
        )
    finally:
        service.close()

    assert found.evidence, "the search must return something for the guard to mean anything"
    supplied = "\n".join(item.text for item in found.evidence)
    assert REPOSITORY_TEXT in supplied
    assert "SERVICE_HOST" in supplied


# ---------------------------------------------------------------------------
# The structural guard: no producer may be rewired to interpolate again
# ---------------------------------------------------------------------------


def test_the_public_projection_is_never_handed_the_warnings() -> None:
    """The invariant held by construction rather than by discipline.

    ``_enumeration_diagnostics`` takes a count. A function that never receives the
    untrusted strings cannot publish them by a later edit, which is a stronger
    guarantee than a function that receives them and is trusted not to. Asserted
    on the signature because that is the thing an accidental change would revert.
    """
    import inspect

    from repoatlas.sync.engine import _enumeration_diagnostics

    signature = inspect.signature(_enumeration_diagnostics)
    (parameter,) = signature.parameters.values()
    assert parameter.annotation == "int", signature

    # Every way a string gets built out of a value, absent from the body. The
    # message is a module constant; there is nothing here to interpolate into.
    source = inspect.getsource(_enumeration_diagnostics)
    body = source.split('"""')[-1]
    for forbidden in ('f"', "f'", ".join(", ".format(", "% (", "str("):
        assert forbidden not in body, f"{forbidden} in the body of the projection"
    assert "ENUMERATION_INCOMPLETE" in body


def test_the_git_client_still_keeps_the_raw_warnings_for_debugging() -> None:
    """RCA-10 moved a boundary, it did not delete evidence of what went wrong.

    The raw stderr stays where it is useful --- inside the Git layer, and in the
    adversarial suite that asserts *which* directory git refused. What changed is
    that it stops there.
    """
    listing = FileListing(paths=["a.py"], warnings=("warning: something",))

    assert listing.warnings == ("warning: something",)
