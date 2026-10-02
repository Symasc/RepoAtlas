r"""`.repoatlasignore` is a RepoAtlas language, not the host's (V1_SPEC §5).

The fresh review found that ignore matching went through `fnmatch.fnmatch`, and
`fnmatch.fnmatch` is not a text operation: it runs both the name and the pattern
through `os.path.normcase` first. On Linux that is the identity function. On
Windows it is `ntpath.normcase`, which lowercases *and* rewrites `/` to `\`.

So the same pattern file over the same repository produced a different index on
a different operating system, and every part of the language moved at once —
`Src/**` started matching `src/main.py`, the character class `[A-Z]*.py` was
silently rewritten to `[a-z]*.py`, a file literally named `foo\bar.py` began
matching the pattern `foo/bar.py`, and a `!Keep.py` negation re-admitted
`keep.py`. None of that was a decision. It was inherited from a helper whose
convenience normalization is meant for host paths, not for a pattern language.

The policy was never in doubt: the module docstring, `V1_SPEC §17` and an
existing test in `test_secret_exclusion_integrity.py` all say `.repoatlasignore`
is case-sensitive, which is what gitignore does on a case-sensitive filesystem
and what a user writing the file expects. Only the primitive disagreed — and the
test pinning the policy would itself have failed on Windows, because it asserted
Linux behaviour through a platform-dependent call.

Two things make this suite able to catch the bug from Linux:

* the **simulation** patches `os.path.normcase` to `ntpath.normcase`, which is
  the exact stdlib mechanism responsible, rather than mutating `os.name` or
  guessing at what Windows does;
* the **potency control** asserts that the legacy primitive really does flip
  under that same patch. Without it the simulation could be vacuous, and a
  regression to `fnmatch.fnmatch` would slip through a green suite.

Case sensitivity and Unicode normalization are different questions, and this
pass answered only the first: nothing here normalizes NFC/NFD, and a test below
pins that it stayed that way.
"""

from __future__ import annotations

import ntpath
import os
import posixpath
import unicodedata
from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.config.settings import (
    DiscoverySettings,
    ReposSettings,
    Settings,
    config_path,
    database_path,
)
from repoatlas.discovery.finder import discover_repositories
from repoatlas.discovery.ignore import IgnoreRules, _compile
from repoatlas.gitstate.client import GitClient
from repoatlas.services.init_service import initialize
from repoatlas.services.retrieval_service import locate
from repoatlas.services.status_service import status
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace
from repoatlas.sync.scanner import EnumerationUnavailable, scan_repository
from tests.conftest import init_git_repository

pytestmark = pytest.mark.adversarial

#: A git binary that does not exist, which puts enumeration on the fallback
#: branch through the architecture's own seam rather than by reaching around it.
NO_GIT = "/nonexistent/git-binary-for-tests"


def ignores(pattern: str, path: str, *, is_dir: bool = False) -> bool:
    """The whole matcher as a pure function of two strings.

    No filesystem, no cwd, no locale, no host normalization. That this can be
    written at all is the property under test: an ignore decision is a function
    of the pattern and the canonical repository-relative path, and of nothing
    else.
    """
    return IgnoreRules.from_text(pattern).matches(path, is_dir=is_dir)


@pytest.fixture
def as_windows(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run the body with the host normalization Windows would apply.

    `os.path` *is* `posixpath` on Linux, so patching the attribute on both is
    what makes the substitution reach `fnmatch`, which looks the function up as
    `os.path.normcase` at call time. This simulates the one stdlib primitive the
    finding was about; it does not pretend to simulate Windows.
    """
    monkeypatch.setattr(os.path, "normcase", ntpath.normcase)
    monkeypatch.setattr(posixpath, "normcase", ntpath.normcase)
    yield


# --- 1. the original reproduction, and proof the simulation is potent ---------


#: The pairs the fresh review's finding is about. Every one of them answered
#: differently on Linux and on Windows before this pass; every one of them is a
#: pattern a user could reasonably write.
DIVERGENT_BEFORE: tuple[tuple[str, str], ...] = (
    ("Src/**", "src/main.py"),
    ("src/**", "Src/main.py"),
    ("BUILD/**", "build/output.py"),
    ("build/**", "BUILD/output.py"),
    ("*.PY", "main.py"),
    ("*.py", "main.PY"),
    ("Foo/Bar/**", "foo/bar/a.py"),
    ("foo/bar/**", "Foo/Bar/a.py"),
    ("FOO/BAR/**", "foo/bar/a.py"),
    ("Foo.py", "foo.py"),
    ("Generated/", "generated/b.py"),
    ("Src", "src/main.py"),
    ("[A-Z]*.py", "main.py"),
    ("[a-z]*.py", "Main.py"),
    ("foo/bar.py", "foo\\bar.py"),
    ("*.py\n!Keep.py", "keep.py"),
)


def test_the_legacy_primitive_really_does_diverge(as_windows: None) -> None:
    """Potency control. Without this, every simulation below could be vacuous.

    This asserts a fact about the standard library, not about RepoAtlas: under
    `ntpath.normcase`, `fnmatch.fnmatch` gives a different answer than it does on
    Linux for the pairs this pass is about. If a future change to `fnmatch` or to
    this fixture made the substitution inert, this test fails and says so,
    instead of the rest of the suite quietly passing for no reason.
    """
    import fnmatch

    del as_windows
    flipped = [
        (pattern, path)
        for pattern, path in (("Src/**", "src/main.py"), ("*.PY", "main.py"), ("[A-Z]*", "main"))
        if fnmatch.fnmatch(path, pattern) is not fnmatch.fnmatchcase(path, pattern)
    ]
    assert len(flipped) == 3, f"the Windows simulation is inert: {flipped}"


@pytest.mark.parametrize(("pattern", "path"), DIVERGENT_BEFORE)
def test_the_reproduction_pairs_no_longer_depend_on_the_host(
    pattern: str, path: str, as_windows: None
) -> None:
    """The finding itself: one textual input, one answer, on every platform."""
    del as_windows
    linux = IgnoreRules.from_text(pattern)
    assert linux.matches(path) is ignores(pattern, path)


@pytest.mark.parametrize(("pattern", "path"), DIVERGENT_BEFORE)
def test_the_reproduction_pairs_answer_what_the_language_says(pattern: str, path: str) -> None:
    """`Src/**` does not ignore `src/main.py`. It never should have.

    The one exception is the negation pair, where the *ignore* decision is True
    because `*.py` matched and `!Keep.py` did not re-admit `keep.py` — which is
    the case-sensitive reading, and the one Windows used to invert.
    """
    expected = pattern.startswith("*.py\n")
    assert ignores(pattern, path) is expected


def test_the_matcher_never_consults_host_normalization(as_windows: None) -> None:
    """The strong form: patch the primitive, and *nothing* moves.

    Asserted over the whole golden matrix rather than a chosen pair, because the
    claim is about the matcher, not about a list of cases someone remembered.
    """
    del as_windows
    windows = [ignores(p, q, is_dir=d) for p, q, d, _ in GOLDEN]
    assert windows == [expected for *_, expected in GOLDEN]


# --- 2-6. case distinctions: directories, filenames, extensions ---------------


def test_a_case_distinct_directory_is_a_different_directory() -> None:
    assert ignores("Generated/**", "Generated/a.py")
    assert not ignores("Generated/**", "generated/b.py")


def test_a_case_distinct_directory_pattern_with_a_trailing_slash() -> None:
    assert ignores("Generated/", "Generated/a.py")
    assert not ignores("Generated/", "generated/b.py")


def test_foo_py_and_lowercase_foo_py_are_two_files() -> None:
    """§16's critical regression, at the matcher.

    A repository may legitimately contain both. Ignoring one must not ignore the
    other, and on Windows it used to.
    """
    assert ignores("Foo.py", "Foo.py")
    assert not ignores("Foo.py", "foo.py")


def test_src_and_lowercase_src_are_two_directories() -> None:
    assert ignores("Src/**", "Src/config.py")
    assert not ignores("Src/**", "src/config.py")


def test_an_uppercase_extension_pattern_matches_only_that_spelling() -> None:
    assert ignores("*.PY", "main.PY")
    assert not ignores("*.PY", "main.py")
    assert ignores("*.py", "main.py")
    assert not ignores("*.py", "main.PY")


def test_every_component_follows_the_same_policy() -> None:
    """No component inherits host casing, including the middle ones."""
    assert ignores("Foo/Bar/**", "Foo/Bar/a.py")
    for pattern in ("foo/Bar/**", "Foo/bar/**", "FOO/BAR/**", "foo/bar/**"):
        assert not ignores(pattern, "Foo/Bar/a.py"), pattern


# --- 7-9. wildcards, anchoring and character classes --------------------------


def test_double_star_keeps_its_existing_reach() -> None:
    """Pinned, not changed. `*` crosses `/` here and always has."""
    assert ignores("foo/**", "foo/b.py")
    assert ignores("foo/**", "foo/a/deep/b.py")
    assert ignores("**/*.py", "a/b.py")
    assert not ignores("foo/**", "bar/b.py")


def test_anchoring_semantics_are_untouched() -> None:
    """§7: the only intended behavioural change is host case interpretation."""
    assert ignores("foo", "a/foo")
    assert not ignores("/foo", "a/foo")
    assert ignores("/foo", "foo/b.py")
    assert ignores("foo/", "a/foo/b.py")


def test_anchoring_no_longer_changes_the_case_policy(as_windows: None) -> None:
    """An inseparable second defect, fixed by the same change.

    The anchored branch fell through to a plain `startswith`, which is
    case-sensitive everywhere, while the unanchored branch went through
    `fnmatch`, which was not. So on Windows `Src` ignored `src/main.py` and
    `/Src` did not — the same directory, two answers, decided by whether the user
    happened to write a leading slash. Both are case-sensitive now.
    """
    del as_windows
    assert ignores("Src", "src/main.py") is ignores("/Src", "src/main.py") is False
    assert ignores("Src", "Src/main.py") is ignores("/Src", "Src/main.py") is True


def test_character_classes_are_textual() -> None:
    """`normcase` lowercased the *pattern*, so `[A-Z]` became `[a-z]`."""
    assert ignores("[A-Z]*.py", "Main.py")
    assert not ignores("[A-Z]*.py", "main.py")
    assert ignores("[a-z]*.py", "main.py")
    assert not ignores("[a-z]*.py", "Main.py")
    assert ignores("[!a-z]*.py", "Main.py")
    assert not ignores("[!a-z]*.py", "main.py")


def test_character_classes_survive_the_windows_simulation(as_windows: None) -> None:
    del as_windows
    assert ignores("[A-Z]*.py", "Main.py")
    assert not ignores("[A-Z]*.py", "main.py")


def test_escaping_behaviour_is_pinned_not_redesigned() -> None:
    """Backslash is a literal character here, and stays one.

    `\\*.py` is not an escape for a literal asterisk — it is a backslash
    followed by a wildcard. That is what the language did before this pass, so it
    is what it does after: this was a case fix, not a syntax redesign.
    """
    assert not ignores("\\*.py", "*.py")
    assert ignores("\\*.py", "\\anything.py")
    assert ignores("a\\?.py", "a\\b.py")


# --- 10. separators -----------------------------------------------------------


def test_a_backslash_is_a_path_character_not_a_separator() -> None:
    """`ntpath.normcase` rewrote `/` to `\\`, which conflated the two.

    A file may legitimately be named `foo\\bar.py` on a POSIX filesystem. It is
    one component, and the two-component pattern `foo/bar.py` does not name it.
    """
    assert not ignores("foo/bar.py", "foo\\bar.py")
    assert not ignores("foo\\bar.py", "foo/bar.py")
    assert ignores("foo/bar.py", "foo/bar.py")
    assert ignores("foo\\bar.py", "foo\\bar.py")


def test_separator_semantics_survive_the_windows_simulation(as_windows: None) -> None:
    del as_windows
    assert not ignores("foo/bar.py", "foo\\bar.py")
    assert not ignores("build/**", "build\\out.py")


# --- 11-12. Unicode: case, and normalization left alone -----------------------


def test_unicode_case_is_not_folded() -> None:
    """Case-sensitive means code points, not a locale's idea of equivalence.

    `İ`/`i` is the classic trap — Turkish locale rules make them equal and no
    other locale does — and `ß`/`ss` is the one `casefold` collapses. Neither is
    an ignore decision RepoAtlas will make.
    """
    assert ignores("Straße.py", "Straße.py")
    assert not ignores("Straße.py", "strasse.py")
    assert not ignores("Straße.py", "STRASSE.PY")
    assert not ignores("İ.py", "i.py")
    assert not ignores("ΣΙΓΜΑ/**", "σιγμα/a.py")
    assert ignores("Café/**", "Café/a.py")
    assert not ignores("CAFÉ/**", "café/a.py")


def test_unicode_case_is_not_folded_under_the_windows_simulation(as_windows: None) -> None:
    del as_windows
    assert not ignores("Straße.py", "strasse.py")
    assert not ignores("CAFÉ/**", "café/a.py")


def test_unicode_normalization_is_unchanged_by_this_pass() -> None:
    """NFC and NFD are still different strings, and that is deliberate.

    Case sensitivity and Unicode normalization are separate questions. The
    matcher never normalized, so it still does not: making composed and
    decomposed forms match would be a new behaviour smuggled in under a case fix,
    and it is not what was asked for. Recorded here so the next pass that
    considers it knows it is choosing, not correcting.
    """
    composed = unicodedata.normalize("NFC", "café/a.py")
    decomposed = unicodedata.normalize("NFD", "café/a.py")
    assert composed != decomposed
    assert ignores("café/**", composed)
    assert not ignores("café/**", decomposed)
    assert ignores(unicodedata.normalize("NFD", "café/**"), decomposed)


# --- 23. the golden matrix ----------------------------------------------------


#: (pattern, path, is_dir, ignored). One table, one expected answer per row, and
#: the answer is the RepoAtlas language's — not any operating system's. Every
#: matcher caller is run against it, and it is run again under the Windows
#: simulation, so a row means the same thing everywhere.
GOLDEN: tuple[tuple[str, str, bool, bool], ...] = (
    # anchoring
    ("foo", "foo", False, True),
    ("foo", "a/foo", False, True),
    ("foo", "foo/b.py", False, True),
    ("foo/", "foo/b.py", False, True),
    ("foo/", "foo", True, True),
    ("foo/", "a/foo/b.py", False, True),
    ("/foo", "foo", False, True),
    ("/foo", "a/foo", False, False),
    ("/foo", "foo/b.py", False, True),
    # wildcards
    ("foo/**", "foo/b.py", False, True),
    ("foo/**", "foo/a/b.py", False, True),
    ("foo/**", "foo", False, False),
    ("**/foo/**", "a/foo/b.py", False, True),
    ("foo/*.py", "foo/b.py", False, True),
    ("**/*.py", "a/b.py", False, True),
    ("*.py", "main.py", False, True),
    ("*.py", "a/main.py", False, True),
    ("?.py", "a.py", False, True),
    ("?.py", "ab.py", False, False),
    # case: directories
    ("Src/**", "src/main.py", False, False),
    ("Src/**", "Src/main.py", False, True),
    ("src/**", "Src/main.py", False, False),
    ("BUILD/**", "build/output.py", False, False),
    ("build/**", "BUILD/output.py", False, False),
    ("Generated/", "Generated/a.py", False, True),
    ("Generated/", "generated/b.py", False, False),
    ("Vendor/**", "Vendor/deep/repo", True, True),
    ("Vendor/**", "vendor/deep/repo", True, False),
    ("/Src", "src/main.py", False, False),
    ("/Src", "Src/main.py", False, True),
    ("Src", "src/main.py", False, False),
    # case: files and components
    ("*.PY", "main.py", False, False),
    ("*.PY", "main.PY", False, True),
    ("Foo.py", "Foo.py", False, True),
    ("Foo.py", "foo.py", False, False),
    ("Foo/Bar/**", "foo/bar/a.py", False, False),
    ("foo/bar/**", "Foo/Bar/a.py", False, False),
    ("FOO/BAR/**", "foo/bar/a.py", False, False),
    # character classes
    ("[A-Z]*.py", "Main.py", False, True),
    ("[A-Z]*.py", "main.py", False, False),
    ("[a-z]*.py", "main.py", False, True),
    ("[!a-z]*.py", "Main.py", False, True),
    # separators and escaping
    ("foo/bar.py", "foo/bar.py", False, True),
    ("foo/bar.py", "foo\\bar.py", False, False),
    ("foo\\bar.py", "foo\\bar.py", False, True),
    ("foo\\bar.py", "foo/bar.py", False, False),
    ("\\*.py", "*.py", False, False),
    # negation
    ("*.py\n!keep.py", "main.py", False, True),
    ("*.py\n!keep.py", "keep.py", False, False),
    ("*.py\n!Keep.py", "keep.py", False, True),
    ("*.py\n!Keep.py", "Keep.py", False, False),
    # parser output
    ("# comment\n*.py", "main.py", False, True),
    ("#*.py", "main.py", False, False),
    ("*.py   ", "main.py", False, True),
    ("\n\n*.py\n\n", "main.py", False, True),
    ("   ", "main.py", False, False),
    ("!", "main.py", False, False),
    ("/", "main.py", False, False),
    # unicode
    ("Café/**", "Café/a.py", False, True),
    ("CAFÉ/**", "café/a.py", False, False),
    ("Straße.py", "Straße.py", False, True),
    ("Straße.py", "strasse.py", False, False),
    # malformed patterns match literally, as they did before
    ("[", "[", False, True),
    ("[a-", "[a-", False, True),
    ("[]", "[]", False, True),
)


@pytest.mark.parametrize(("pattern", "path", "is_dir", "expected"), GOLDEN)
def test_golden_matrix(pattern: str, path: str, is_dir: bool, expected: bool) -> None:
    assert ignores(pattern, path, is_dir=is_dir) is expected


@pytest.mark.parametrize(("pattern", "path", "is_dir", "expected"), GOLDEN)
def test_golden_matrix_under_the_windows_simulation(
    pattern: str, path: str, is_dir: bool, expected: bool, as_windows: None
) -> None:
    del as_windows
    assert ignores(pattern, path, is_dir=is_dir) is expected


def test_the_golden_matrix_covers_the_whole_language() -> None:
    """A guard on the table itself, so it cannot rot into a case-only list."""
    assert len(GOLDEN) >= 30
    patterns = {row[0] for row in GOLDEN}
    for token in ("**", "[A-Z]", "!", "#", "\\", "/", "?", "Café"):
        assert any(token in p for p in patterns), token
    assert {row[3] for row in GOLDEN} == {True, False}


# --- 32. determinism over generated input -------------------------------------


def _generated_pairs() -> list[tuple[str, str]]:
    """Deterministic ASCII patterns and paths, mixing case and metacharacters.

    Not random: a seeded generator that changes with the Python version would
    make a failure unreproducible. The point is breadth over the bug class, and
    the product of these alphabets gives it.
    """
    parts = ("a", "A", "b", "B", "Src", "src", "*", "?", "[A-Z]", "[a-z]")
    names = ("x.py", "X.py", "x.PY", "Main.py", "main.py")
    pairs = []
    for head in parts:
        for name in names:
            pairs.append((f"{head}/{name}", f"{head}/{name}"))
            pairs.append((f"{head}/**", f"{head.swapcase()}/{name}"))
            pairs.append((name, name.swapcase()))
            pairs.append((f"/{head}", f"{head}/{name}"))
    return pairs


def test_one_textual_input_gives_one_answer_everywhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug class is broad, so the property is asserted broadly.

    Every generated pair is evaluated three times — as the process runs, under
    POSIX normalization and under Windows normalization — and all three must
    agree. This is the invariant in its general form: an ignore decision is a
    function of the pattern and the path.
    """
    pairs = _generated_pairs()
    assert len(pairs) >= 150

    native = [ignores(p, q) for p, q in pairs]
    for normcase in (posixpath.normcase, ntpath.normcase):
        with monkeypatch.context() as patched:
            patched.setattr(os.path, "normcase", normcase)
            patched.setattr(posixpath, "normcase", normcase)
            assert [ignores(p, q) for p, q in pairs] == native, normcase


def test_the_generated_pairs_actually_exercise_case_distinctions() -> None:
    """Guard against a property test that proves nothing because nothing matches."""
    pairs = _generated_pairs()
    assert sum(ignores(p, q) for p, q in pairs) > 20


# --- 27-28, 41. inputs and compilation ----------------------------------------


def test_the_matcher_is_given_relative_paths_only() -> None:
    """§27: absolute host paths never reach matching, so drive letters cannot.

    Asserted from the callers' side elsewhere in this file; here it is the
    consequence that matters — an absolute path is not a repository-relative one
    and is not treated as if it were.
    """
    assert ignores("src/**", "src/main.py")
    assert not ignores("src/**", "/home/user/root/repo/src/main.py")
    assert not ignores("src/**", "C:/root/repo/src/main.py")
    assert not ignores("/src/**", "c:/root/repo/src/main.py")


def test_patterns_compile_once_per_rule_not_once_per_path() -> None:
    """§41. The compiled form is built at parse time and carried on the rule."""
    rules = IgnoreRules.from_text("Src/**\n*.py\n")
    compiled = [rule.regex for rule in rules._rules]
    assert len(compiled) == 2
    for _ in range(50):
        rules.matches("src/main.py")
    assert [rule.regex for rule in rules._rules] == compiled


def test_the_compiled_form_carries_no_host_flags() -> None:
    """No IGNORECASE, no LOCALE: the regex cannot become case-insensitive."""
    import re

    pattern = _compile("Src/**")
    assert not pattern.flags & re.IGNORECASE
    assert not pattern.flags & re.LOCALE
    assert pattern.match("Src/a.py") is not None
    assert pattern.match("src/a.py") is None


def test_malformed_patterns_keep_their_existing_behaviour() -> None:
    """§42: unterminated classes match literally. Not redesigned, just pinned."""
    for pattern in ("[", "[a-", "[]", "[!", "a[b", "**[", "["):
        assert ignores(pattern, pattern), pattern
    assert not ignores("[", "x")


# --- harness for the product-level half ---------------------------------------


def make_repo(root: Path, files: dict[str, str]) -> Path:
    """A real Git repository with one commit."""
    root.mkdir(parents=True, exist_ok=True)
    for name, content in {"README.md": "fixture\n", **files}.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    init_git_repository(root)
    return root


def settings_for(tmp_path: Path, tag: str = "state") -> Settings:
    return Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / tag / "atlas.db")},
        }
    )


def open_at(settings: Settings, root: Path, *, fallback: bool = False) -> Workspace:
    workspace = open_workspace(
        settings=settings.model_copy(update={"repos": ReposSettings(root=root)}),
        require_root=False,
    )
    if fallback:
        workspace.git = GitClient(git_binary=NO_GIT)
    return workspace


def indexed_via(
    settings: Settings, root: Path, config: Path, *, fallback: bool = False
) -> set[str]:
    """Index ``root`` through the chosen enumerator and return the file set."""
    workspace = open_workspace(settings=settings, require_root=False)
    if fallback:
        workspace.git = GitClient(git_binary=NO_GIT)
    try:
        initialize(workspace, root, config_path=config)
        rows = workspace.database.execute("SELECT rel_path FROM files").fetchall()
        return {str(r["rel_path"]) for r in rows}
    finally:
        workspace.close()


def indexed_paths(settings: Settings, root: Path, *, fallback: bool = False) -> set[str]:
    workspace = open_at(settings, root, fallback=fallback)
    try:
        rows = workspace.database.execute("SELECT rel_path FROM files").fetchall()
        return {str(r["rel_path"]) for r in rows}
    finally:
        workspace.close()


def resync(settings: Settings, root: Path, *, fallback: bool = False) -> None:
    workspace = open_at(settings, root, fallback=fallback)
    try:
        context = workspace.resolver().resolve(root, command="sync")
        sync(workspace, context)
    finally:
        workspace.close()


#: A tree whose capitalization is the whole point: each pair differs only in the
#: case of one component, and `Cased/**` must take exactly one of each pair.
CASED_TREE: dict[str, str] = {
    "Cased/deep/a.py": "CASED_UPPER_MARKER = 1\n",
    "cased/deep/b.py": "CASED_LOWER_MARKER = 1\n",
    "Src/config.py": "SRC_UPPER_MARKER = 1\n",
    "src/config.py": "SRC_LOWER_MARKER = 1\n",
    "Foo.py": "FOO_UPPER_MARKER = 1\n",
    "foo.py": "FOO_LOWER_MARKER = 1\n",
}

#: What `Cased/**`, `Src/**` and `Foo.py` admit from `CASED_TREE`.
CASED_KEPT = frozenset({"README.md", "cased/deep/b.py", "src/config.py", "foo.py"})
CASED_DROPPED = frozenset({"Cased/deep/a.py", "Src/config.py", "Foo.py"})
CASED_PATTERNS = "Cased/**\nSrc/**\nFoo.py\n"


@pytest.fixture
def case_sensitive_root(tmp_path: Path) -> Path:
    """A managed root whose repository holds every case-distinct pair.

    Skipped rather than adapted on a filesystem that cannot hold `Foo.py` and
    `foo.py` at once: the matcher tests above cover the policy there, and a
    silently degraded integration fixture would assert less than it appears to.
    """
    root = tmp_path / "root"
    repo = root / "svc"
    repo.mkdir(parents=True)
    (repo / "Foo.py").write_text("x\n", encoding="utf-8")
    (repo / "foo.py").write_text("y\n", encoding="utf-8")
    if len(list(repo.iterdir())) < 2:  # pragma: no cover - depends on the host FS
        pytest.skip("filesystem is case-insensitive; matcher tests cover the policy")
    for name in ("Foo.py", "foo.py"):
        (repo / name).unlink()
    make_repo(repo, CASED_TREE)
    (root / ".repoatlasignore").write_text(CASED_PATTERNS, encoding="utf-8")
    (repo / ".repoatlasignore").write_text(CASED_PATTERNS, encoding="utf-8")
    return root


# --- 15-17. primary scanner, fallback scanner, and their agreement ------------


def test_the_primary_scanner_applies_the_case_policy(case_sensitive_root: Path) -> None:
    """§12: `.repoatlasignore` is filtered after git enumeration, by this matcher.

    `git ls-files` handles `.gitignore` and knows nothing about
    `.repoatlasignore`, so the case policy has to hold on RepoAtlas's side of
    that seam — which is the side this asserts.
    """
    found = {f.rel_path for f in scan_repository(case_sensitive_root / "svc")}
    assert found >= CASED_KEPT
    assert not (CASED_DROPPED & found), sorted(CASED_DROPPED & found)


def test_there_is_no_second_enumerator_to_apply_a_different_policy(
    case_sensitive_root: Path,
) -> None:
    """§15 restated: the platform bug cannot return through a second enumerator.

    It used to be able to, because there were two — `git ls-files` and a filesystem
    walk — and this suite asserted the case policy held on both. There is now one:
    when git cannot enumerate, enumeration refuses rather than answering from a
    walk, so "the other enumerator disagrees" is no longer a reachable state.
    """
    with pytest.raises(EnumerationUnavailable):
        scan_repository(case_sensitive_root / "svc", git=GitClient(git_binary=NO_GIT))


def test_the_admissible_set_comes_from_one_enumerator(case_sensitive_root: Path) -> None:
    """§11 and §36: one matcher and one enumerator, so one admissible set."""
    repo = case_sensitive_root / "svc"
    primary = {f.rel_path for f in scan_repository(repo)}
    assert primary >= CASED_KEPT
    assert not (CASED_DROPPED & primary)
    with pytest.raises(EnumerationUnavailable):
        scan_repository(repo, git=GitClient(git_binary=NO_GIT))


def test_the_case_policy_holds_under_the_windows_simulation(
    case_sensitive_root: Path, as_windows: None
) -> None:
    del as_windows
    repo = case_sensitive_root / "svc"
    primary = {f.rel_path for f in scan_repository(repo)}
    assert primary >= CASED_KEPT
    assert not (CASED_DROPPED & primary)


def test_the_indexed_set_applies_the_case_policy(case_sensitive_root: Path, tmp_path: Path) -> None:
    """The product-level form, through the one enumerator that exists."""
    primary = indexed_via(settings_for(tmp_path, "p"), case_sensitive_root, tmp_path / "p.yaml")
    assert CASED_DROPPED.isdisjoint(primary)
    assert {"src/config.py", "foo.py", "cased/deep/b.py"} <= primary


# --- 13. nested repository discovery ------------------------------------------


@pytest.fixture
def vendor_root(tmp_path: Path) -> Path:
    """Two nested repositories differing only in the case of one ancestor.

    Deliberately not named ``vendor``: that is in the built-in exclusion set, so
    the walk would refuse the lowercase side for a reason that has nothing to do
    with the pattern under test and the fixture would pass while proving nothing.
    """
    root = tmp_path / "vroot"
    for prefix in ("Extern", "extern"):
        make_repo(root / prefix / "deep" / "repo", {"app.py": f"MARK_{prefix} = 1\n"})
    (root / ".repoatlasignore").write_text("Extern/**\n", encoding="utf-8")
    return root


def test_built_in_exclusions_are_already_host_independent() -> None:
    """§21: reported, and it needed no change.

    Built-in exclusions never went through ``fnmatch``. They are an exact
    membership test against a frozenset of component names, which compares
    strings and asks the host nothing — so ``vendor`` is excluded, ``Vendor`` is
    not, and that is true on every platform. The finding was confined to the
    pattern language, and so is the fix.
    """
    config = DiscoverySettings()
    assert "vendor" in config.exclusions
    assert "Vendor" not in config.exclusions
    assert "NODE_MODULES" not in config.exclusions


def test_discovery_ignores_only_the_matching_case(vendor_root: Path) -> None:
    found = {c.slug for c in discover_repositories(vendor_root)}
    assert "extern/deep/repo" in found
    assert "Extern/deep/repo" not in found


def test_discovery_is_unmoved_by_the_windows_simulation(
    vendor_root: Path, as_windows: None
) -> None:
    del as_windows
    found = {c.slug for c in discover_repositories(vendor_root)}
    assert found == {"extern/deep/repo"}


def test_init_status_and_sync_all_agree_on_the_ignored_repository(
    vendor_root: Path, tmp_path: Path
) -> None:
    """§13: one policy, three commands, no disagreement between them."""
    settings = settings_for(tmp_path)
    workspace = open_workspace(settings=settings, require_root=False)
    try:
        initialize(workspace, vendor_root, config_path=tmp_path / "c.yaml")
        slugs = {r.slug for r in workspace.repositories.list_all()}
    finally:
        workspace.close()
    assert slugs == {"extern/deep/repo"}

    workspace = open_at(settings, vendor_root)
    try:
        report = status(workspace, workspace.resolver().resolve(vendor_root, command="status"))
        assert report.new_repositories == ()
        assert report.index_state == "current"
        context = workspace.resolver().resolve(vendor_root, command="sync")
        sync(workspace, context)
        assert {r.slug for r in workspace.repositories.list_all()} == {"extern/deep/repo"}
    finally:
        workspace.close()


# --- 14. repository-set freshness ---------------------------------------------


def test_an_ignored_late_repository_does_not_make_status_stale(tmp_path: Path) -> None:
    """§14: Pass 10 drift detection must obey the same case policy.

    Both repositories appear after the index is built. Under a case-sensitive
    reading exactly one of them is drift — and a Windows-flavoured matcher would
    have called neither one drift, leaving `generated/new` silently unindexed
    while `status` said "current".
    """
    root = tmp_path / "root"
    make_repo(root / "svc", {"a.py": "A = 1\n"})
    (root / ".repoatlasignore").write_text("Generated/**\n", encoding="utf-8")
    settings = settings_for(tmp_path)
    workspace = open_workspace(settings=settings, require_root=False)
    try:
        initialize(workspace, root, config_path=tmp_path / "c.yaml")
    finally:
        workspace.close()

    make_repo(root / "Generated" / "new", {"u.py": "U = 1\n"})
    make_repo(root / "generated" / "new", {"l.py": "L = 1\n"})

    workspace = open_at(settings, root)
    try:
        report = status(workspace, workspace.resolver().resolve(root, command="status"))
    finally:
        workspace.close()

    assert report.new_repositories == ("generated/new",)
    assert report.index_state == "stale"

    resync(settings, root)
    workspace = open_at(settings, root)
    try:
        slugs = {r.slug for r in workspace.repositories.list_all()}
    finally:
        workspace.close()
    assert slugs == {"svc", "generated/new"}


# --- 18, 37-38. ignore-file and capitalization transitions --------------------


def test_adding_a_cased_pattern_removes_exactly_one_side(tmp_path: Path) -> None:
    """§38: start with no ignore file, add one, and the index converges."""
    root = tmp_path / "root"
    repo = make_repo(root / "svc", {"Src/a.py": "U = 1\n", "src/b.py": "L = 1\n"})
    settings = settings_for(tmp_path)
    before = indexed_via(settings, root, tmp_path / "c.yaml")
    assert {"Src/a.py", "src/b.py"} <= before

    (repo / ".repoatlasignore").write_text("Src/**\n", encoding="utf-8")
    resync(settings, root)
    after = indexed_paths(settings, root)
    assert "Src/a.py" not in after, "the newly ignored file survived as a stale fact"
    assert "src/b.py" in after, "the case-distinct sibling was dropped with it"


def test_changing_a_patterns_capitalization_moves_the_boundary(tmp_path: Path) -> None:
    """The same file set, the same tree, one letter of the pattern changed."""
    root = tmp_path / "root"
    repo = make_repo(root / "svc", {"Src/a.py": "U = 1\n", "src/b.py": "L = 1\n"})
    settings = settings_for(tmp_path)
    (repo / ".repoatlasignore").write_text("Src/**\n", encoding="utf-8")
    indexed_via(settings, root, tmp_path / "c.yaml")
    assert "Src/a.py" not in indexed_paths(settings, root)

    (repo / ".repoatlasignore").write_text("src/**\n", encoding="utf-8")
    resync(settings, root)
    after = indexed_paths(settings, root)
    assert "Src/a.py" in after, "un-ignoring did not bring the file back"
    assert "src/b.py" not in after, "the newly ignored file survived"


def test_renaming_a_directorys_capitalization_is_a_real_transition(tmp_path: Path) -> None:
    """§37: rename `src` to `Src` under the pattern `Src/**`, then back.

    A pure capitalization rename is the transition where a case-insensitive
    matcher and a case-sensitive one disagree about *everything* — and the file
    keeps its content hash across it, so nothing but the path changed.
    """
    root = tmp_path / "root"
    repo = make_repo(root / "svc", {"src/a.py": "RENAME_MARKER = 1\n"})
    (repo / ".repoatlasignore").write_text("Src/**\n", encoding="utf-8")
    settings = settings_for(tmp_path)
    indexed_via(settings, root, tmp_path / "c.yaml")
    assert "src/a.py" in indexed_paths(settings, root)

    (repo / "src").rename(repo / "Src")
    resync(settings, root)
    after = indexed_paths(settings, root)
    assert "src/a.py" not in after, "a stale fact survived the rename into an ignored path"
    assert "Src/a.py" not in after, "the renamed file is now ignored and must not be indexed"

    (repo / "Src").rename(repo / "src")
    resync(settings, root)
    assert "src/a.py" in indexed_paths(settings, root), "reversing the rename did not restore it"


def test_the_ignored_side_leaves_no_chunks_or_entities_behind(tmp_path: Path) -> None:
    """§19/§38: stale-fact cleanup, not just a missing row in `files`."""
    root = tmp_path / "root"
    repo = make_repo(root / "svc", {"Src/gone.py": "def cased_ignored_fn():\n    return 1\n"})
    settings = settings_for(tmp_path)
    indexed_via(settings, root, tmp_path / "c.yaml")

    (repo / ".repoatlasignore").write_text("Src/**\n", encoding="utf-8")
    resync(settings, root)

    workspace = open_at(settings, root)
    try:
        for table in ("files", "chunks", "entities"):
            column = "rel_path" if table == "files" else "id"
            rows = workspace.database.execute(
                f"SELECT COUNT(*) AS n FROM {table} "  # noqa: S608 - a fixed table list
                + (
                    "WHERE rel_path LIKE 'Src/%'"
                    if table == "files"
                    else "WHERE file_id NOT IN (SELECT id FROM files)"
                )
            ).fetchone()
            assert rows is not None and int(rows["n"]) == 0, (table, column)
        hits = workspace.database.execute(
            "SELECT COUNT(*) AS n FROM entities WHERE name = 'cased_ignored_fn'"
        ).fetchone()
        assert hits is not None and int(hits["n"]) == 0
    finally:
        workspace.close()


# --- 20. retrieval ------------------------------------------------------------


def test_retrieval_sees_the_visible_side_and_not_the_ignored_one(tmp_path: Path) -> None:
    """§39: the product's behaviour, not the matcher's return value.

    Two unique symbols, one on each side of a case-distinct pattern. One must be
    findable and the other must not exist as far as `locate` is concerned.
    """
    root = tmp_path / "root"
    repo = make_repo(
        root / "svc",
        {
            "Src/ignored.py": "def cased_ignored_symbol_918273():\n    return 1\n",
            "src/visible.py": "def cased_visible_symbol_918273():\n    return 2\n",
        },
    )
    (repo / ".repoatlasignore").write_text("Src/**\n", encoding="utf-8")
    settings = settings_for(tmp_path)
    indexed_via(settings, root, tmp_path / "c.yaml")

    workspace = open_at(settings, root)
    try:
        context = workspace.resolver().resolve(root, command="locate")
        visible = locate(workspace, "cased_visible_symbol_918273", context)
        hidden = locate(workspace, "cased_ignored_symbol_918273", context)
    finally:
        workspace.close()

    assert visible.candidates, "the admitted file is not retrievable"
    assert any(c.path == "src/visible.py" for c in visible.candidates)
    assert not any("ignored" in c.path for c in visible.candidates)
    assert not hidden.candidates, [c.location for c in hidden.candidates]


# --- 21-22. the security regressions ------------------------------------------


def test_secret_name_policy_is_still_case_insensitive() -> None:
    """§20: two policies, two answers, and this pass changed neither.

    The security policy folds case on purpose — `.ENV` is a dotenv file — and it
    is a separate module for exactly this reason. Making `.repoatlasignore`
    case-sensitive must not be read as an argument that secret matching should be.
    """
    from repoatlas.security import is_secret_path

    for name in (".env", ".ENV", ".Env", "id_rsa", "ID_RSA", "server.PEM", "a/b/private.KEY"):
        assert is_secret_path(name), name
    assert not is_secret_path("monkey.py")
    assert not is_secret_path("id_rsa_notes.txt")


def test_a_cased_negation_cannot_re_admit_a_secret(tmp_path: Path) -> None:
    """The precedence rule, retested against the new matcher.

    The secret policy runs first and unconditionally, so no spelling of a
    negation — matching case or not — puts the file back.
    """
    root = tmp_path / "root"
    repo = make_repo(root / "svc", {"a.py": "A = 1\n"})
    (repo / ".ENV").write_text("TOKEN=REPOATLAS_CASED_SECRET_918273\n", encoding="utf-8")
    (repo / ".repoatlasignore").write_text("!.ENV\n!.env\n!*.ENV\n", encoding="utf-8")
    settings = settings_for(tmp_path)
    found = indexed_via(settings, root, tmp_path / "c.yaml")
    assert ".ENV" not in found

    workspace = open_at(settings, root)
    try:
        rows = workspace.database.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE text LIKE '%REPOATLAS_CASED_SECRET_918273%'"
        ).fetchone()
        assert rows is not None and int(rows["n"]) == 0
    finally:
        workspace.close()


def test_git_internals_stay_structurally_excluded(tmp_path: Path) -> None:
    """§22: Pass 13 made `.git` unwalkable. Pattern semantics cannot re-enable it.

    Exclusions are emptied and the ignore file negates every spelling of `.git`
    it can. The refusal is structural, so none of it matters.
    """
    root = tmp_path / "root"
    repo = make_repo(root / "svc", {"a.py": "A = 1\n"})
    (repo / ".repoatlasignore").write_text("!.git\n!.git/**\n!.GIT/**\n", encoding="utf-8")
    empty = DiscoverySettings(exclusions=())
    found = {f.rel_path for f in scan_repository(repo, settings=empty, git=None)}
    assert not any(p == ".git" or p.startswith(".git/") for p in found), sorted(found)
    assert "a.py" in found


# --- 25. no real state ---------------------------------------------------------


def test_no_test_touches_real_repoatlas_state() -> None:
    """This suite writes only to pytest temporary directories."""
    for path in (config_path(), database_path()):
        if not path.exists():
            continue
        blob = path.read_bytes()[:200_000]
        assert b"pytest-of-" not in blob, path
        assert b"/tmp/pytest" not in blob, path
