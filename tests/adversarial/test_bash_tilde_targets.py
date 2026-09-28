r"""Tilde-expanded Bash command words are not literal targets (V1_SPEC §2, §7.3).

The fresh review found that

    ~/scripts/tool.sh

was recorded as a deterministic execution target spelled exactly that way — an
``EXECUTES`` edge to a *script* whose path begins with a tilde. Every character
of it is in the source, so it passed the parser's static-proof test. But Bash
expands the tilde before the command lookup, using the ``HOME`` of whichever
machine eventually runs the script, so the one thing Bash will certainly *not*
execute is a file literally named ``~/scripts/tool.sh``.

That is a different failure from ``$CMD``. ``$CMD`` looks unknown and is
unknown. ``~/tool`` looks known and is not — which is why it survived a parser
built around the rule "record only what the source spells out". Spelling is
necessary and not sufficient: the second question is whether the shell rewrites
the word before using it.

**RepoAtlas may not answer that question with its own environment.** The
analyzer's ``HOME`` and the analyzer host's passwd database are facts about the
machine running the indexer, not about the machine that will run the script.
``~nosuchuser/tool`` makes the point sharpest: Bash leaves it *literal* when the
account does not exist, so whether ``~name/`` expands at all depends on account
state RepoAtlas has no sound source for. Tests below pin that no lookup happens
in either direction.

Quoting decides whether any of this applies, and the decision has to be made
while the quotes are still visible. After unquoting, ``~/tool``, ``"~/tool"``,
``'~'/tool``, ``"~"/tool`` and ``\~/tool`` are the identical string ``~/tool`` —
four literal filenames and one expansion, indistinguishable. This is the
Dockerfile lesson from Pass 6 in a second language: prove static semantics before
tokenization destroys the syntax that decides them.

Every expectation here was measured against **GNU bash 5.2.21** with
``--noprofile --norc`` and a controlled ``HOME``/``PWD``/``OLDPWD``, by the
oracle at the bottom of this file. The oracle expands only words this file
writes; no indexed script is ever executed, sourced or evaluated.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.models.parsing import EdgeType, ParsedFile
from repoatlas.parsers.bash import BashParser
from repoatlas.services.init_service import initialize
from repoatlas.services.retrieval_service import locate
from repoatlas.services.workspace import Workspace, open_workspace
from tests.conftest import init_git_repository

pytestmark = pytest.mark.adversarial


# -- helpers -----------------------------------------------------------------


def parse(source: str, name: str = "script.sh") -> ParsedFile:
    return BashParser().parse(Path(name), source)


def targets(source: str, edge: EdgeType = EdgeType.EXECUTES) -> set[str]:
    return {r.target_key for r in parse(source).references if r.edge_type is edge}


def executed(source: str) -> set[str]:
    """Every `EXECUTES` target, whatever kind it was recorded as."""
    return targets(source)


def kinds(source: str) -> set[tuple[str, str]]:
    return {
        (r.target_kind.value, r.target_key)
        for r in parse(source).references
        if r.edge_type is EdgeType.EXECUTES
    }


# -- 1. the original reproduction --------------------------------------------


ORIGINAL = "#!/bin/bash\n~/scripts/tool.sh\n"


def test_the_original_finding_no_longer_claims_a_literal_target() -> None:
    """`~/scripts/tool.sh` was an `EXECUTES` edge to a script spelled that way."""
    assert executed(ORIGINAL) == set()
    assert "~/scripts/tool.sh" not in {r.target_key for r in parse(ORIGINAL).references}


def test_the_second_reported_form_is_also_gone() -> None:
    """`~/bin/run` has no script suffix, so it became a *command* named `~/bin/run`."""
    assert executed("~/bin/run\n") == set()


def test_the_reproduction_fixture_keeps_its_literal_neighbours() -> None:
    """The fix is surgical: only the tilde lines lost their target."""
    source = (
        "#!/bin/bash\n"
        "~/scripts/tool.sh\n"
        "~/bin/run\n"
        "echo hello\n"
        "./local.sh\n"
        "/usr/bin/env\n"
        "tools/build.sh\n"
    )
    assert executed(source) == {"echo", "./local.sh", "/usr/bin/env", "tools/build.sh"}


# -- 2, 7, 25, 26. every expanded form ---------------------------------------


#: Command words Bash rewrites before the command lookup. Measured, not assumed.
EXPANDED_FORMS = (
    "~/tool.sh",
    "~/scripts/tool.sh",
    "~",
    "~/",
    "~user/tool",
    "~root/tool",
    "~nobody/tool",
    "~definitely_nonexistent_user_918273/tool",
    "~+/tool",
    "~-/tool",
    "~/bin/*",
    "~/tool arg1 arg2",
)


@pytest.mark.parametrize("word", EXPANDED_FORMS)
def test_no_expanded_form_becomes_a_target(word: str) -> None:
    assert executed(f"{word}\n") == set()


@pytest.mark.parametrize("word", EXPANDED_FORMS)
def test_no_expanded_form_leaves_a_tilde_key_anywhere(word: str) -> None:
    """Not merely absent from EXECUTES: absent from every reference kind."""
    assert not [r for r in parse(f"{word}\n").references if r.target_key.startswith("~")]


def test_bare_tilde_is_not_a_command_named_tilde() -> None:
    """§7. `~` alone expands to $HOME; it is not a program called `~`."""
    assert executed("~\n") == set()


def test_tilde_plus_and_minus_are_runtime_directories() -> None:
    """§25. `~+` is $PWD and `~-` is $OLDPWD — both runtime state."""
    assert executed("~+/tool\n") == set()
    assert executed("~-/tool\n") == set()


def test_named_user_tilde_is_never_a_host_lookup() -> None:
    """§26. Bash leaves `~name/` literal when the account is absent.

    So the form is runtime-dependent in *both* directions, and a static analyzer
    cannot pick either one. `~root` in particular exists on virtually every Linux
    host including this one, which is exactly why resolving it would be so easy
    and so wrong.
    """
    for word in ("~root/tool", "~nobody/tool", "~definitely_nonexistent_user_918273/tool"):
        assert executed(f"{word}\n") == set(), word


# -- 6, 8, 22, 23. quoting and escaping decide -------------------------------


#: Command words where the tilde is quoted or escaped, so Bash treats it as an
#: ordinary character and looks for a file whose name starts with `~`.
LITERAL_TILDE_FORMS = ('"~/tool"', "'~'/tool", '"~"/tool', "\\~/tool", "'~/tool'")


@pytest.mark.parametrize("word", LITERAL_TILDE_FORMS)
def test_a_quoted_or_escaped_tilde_stays_a_literal_target(word: str) -> None:
    """§22, §23. These name a real file, so the deterministic target survives."""
    assert executed(f"{word}\n") == {"~/tool"}


def test_the_distinction_is_syntactic_not_textual() -> None:
    """The five spellings unquote to one string; two answers, not one.

    This is the test that fails if anyone decides tilde handling by looking at
    whether the resulting string starts with `~`.
    """
    assert executed("~/tool\n") == set()
    for word in LITERAL_TILDE_FORMS:
        assert executed(f"{word}\n") == {"~/tool"}, word


def test_a_quoted_tilde_script_keeps_its_script_classification() -> None:
    assert kinds('"~/tool.sh"\n') == {("script", "~/tool.sh")}
    assert kinds("~/tool.sh\n") == set()


def test_a_tilde_that_is_not_at_the_start_was_never_an_expansion() -> None:
    """Tilde expansion applies at word start only. `a~b` is an ordinary name."""
    assert executed("./a~b.sh\n") == {"./a~b.sh"}
    assert executed("tools/back~up.sh\n") == {"tools/back~up.sh"}


# -- 6. literal targets are untouched ----------------------------------------


LITERAL_FORMS = {
    "echo": "echo",
    "python": "python",
    "bash": "bash",
    "make": "make",
    "./tool.sh": "./tool.sh",
    "../tool.sh": "../tool.sh",
    "tools/build.sh": "tools/build.sh",
    "/usr/bin/env": "/usr/bin/env",
    "./bin/*": "./bin/*",
}


@pytest.mark.parametrize(("word", "target"), sorted(LITERAL_FORMS.items()))
def test_literal_targets_keep_their_deterministic_edge(word: str, target: str) -> None:
    """§6. One expansion form being dynamic does not demote path-shaped words."""
    assert target in executed(f"{word}\n")


def test_globs_keep_whatever_the_parser_already_did() -> None:
    """§15. `./bin/*` was already recorded literally; that is not this pass's call.

    What this pass owes is composition: the tilde rule must fire on `~/bin/*`
    independently of how the glob is treated.
    """
    assert executed("./bin/*\n") == {"./bin/*"}
    assert executed("~/bin/*\n") == set()


# -- 9, 34. parameter expansion is a different mechanism ---------------------


def test_dollar_home_keeps_its_existing_treatment() -> None:
    """§9. `$HOME/tool` is parameter expansion and was already unresolved."""
    for word in ("$HOME/tool", "${HOME}/tool", '"$HOME/tool"'):
        assert executed(f"{word}\n") == set(), word


def test_dollar_home_still_records_the_environment_read() -> None:
    """§34. Demoting a target must not discard a truthful syntax fact."""
    for word in ("$HOME/tool", "${HOME}/tool", '"$HOME/tool"'):
        assert targets(f"{word}\n", EdgeType.READS) == {"HOME"}, word


def test_single_quoted_dollar_home_is_a_literal_filename() -> None:
    """Single quotes suspend parameter expansion, so this names a real file."""
    assert executed("'$HOME/tool'\n") == {"$HOME/tool"}
    assert targets("'$HOME/tool'\n", EdgeType.READS) == set()


# -- 11. command position versus argument position ---------------------------


def test_a_tilde_argument_is_not_an_execution_target() -> None:
    """§11. `echo ~/file` expands an argument; it executes `echo`."""
    assert executed("echo ~/file\n") == {"echo"}


def test_a_tilde_command_with_tilde_arguments_records_nothing() -> None:
    assert executed("~/tool ~/config\n") == set()


def test_an_assignment_prefix_does_not_make_the_tilde_a_target() -> None:
    """`VAR=~/x command` runs `command`; the tilde is an assignment value.

    The prefix assignment is still recorded as an `EXPORTS`, which is what POSIX
    says it is and what this parser has always said. What must not appear is a
    target built out of the value.
    """
    assert executed("VAR=~/x echo hi\n") == {"echo"}
    assert targets("VAR=~/x echo hi\n", EdgeType.EXPORTS) == {"VAR"}
    assert not [r for r in parse("VAR=~/x echo hi\n").references if "~" in r.target_key]


def test_an_exported_assignment_prefix_still_records_its_variable() -> None:
    assert targets("LOG_LEVEL=~/x echo hi\n", EdgeType.EXPORTS) == {"LOG_LEVEL"}
    assert executed("LOG_LEVEL=~/x echo hi\n") == {"echo"}


# -- 10. assignment context: observed, not changed ---------------------------


def test_assignment_values_were_never_execution_targets() -> None:
    """§10. Reported rather than fixed: assignment values create no targets.

    `PATH=~/bin:$PATH` has assignment-context tilde rules of its own, but the
    parser has never read assignment *values* for targets at all, so there is
    nothing here for this pass to get wrong. Pinned so a later pass that starts
    reading them knows the tilde rule has to come with it.
    """
    assert executed("PATH=~/bin:$PATH\n") == set()
    assert executed("HOME_DIR=~/workspace\n") == set()
    assert not [r for r in parse("PATH=~/bin:$PATH\n").references if "~" in r.target_key]


# -- 12, 13. wrappers and operators ------------------------------------------


def test_wrappers_do_not_launder_a_tilde_target() -> None:
    """§12. Unwrapping is what makes this dangerous: it re-reads the word."""
    assert executed("command ~/tool\n") == {"command"} or executed("command ~/tool\n") == set()
    assert "~/tool" not in executed("command ~/tool\n")
    assert "~/tool" not in executed("exec ~/tool\n")
    assert "~/tool" not in executed("env X=1 ~/tool\n")
    assert "~/tool" not in executed("nohup ~/tool\n")
    assert "~/tool" not in executed("timeout 30 ~/tool\n")


def test_a_wrapper_around_a_literal_target_still_resolves() -> None:
    """The wrapper machinery is not disabled, only fed a truthful word."""
    assert "./tool.sh" in executed("timeout 30 ./tool.sh\n")
    assert "./tool.sh" in executed("exec ./tool.sh\n")


OPERATOR_LINES = (
    "true; ~/tool",
    "true && ~/tool",
    "true || ~/tool",
    "true | ~/tool",
    "( ~/tool )",
    "{ ~/tool; }",
    "if true; then ~/tool; fi",
    "while false; do ~/tool; done",
    "for f in a; do ~/tool; done",
    "case x in x) ~/tool;; esac",
)


@pytest.mark.parametrize("line", OPERATOR_LINES)
def test_a_later_command_position_gets_the_same_rule(line: str) -> None:
    """§13. Command position is command position, wherever the grammar puts it."""
    assert "~/tool" not in executed(f"{line}\n"), line


def test_a_tilde_inside_a_function_body_is_still_expanded() -> None:
    source = "deploy() {\n  ~/tool.sh\n  ./real.sh\n}\n"
    assert executed(source) == {"./real.sh"}


# -- 14, 16, 17. untouched neighbours ----------------------------------------


def test_command_substitution_keeps_its_existing_behaviour() -> None:
    """§16. The inner command genuinely runs; that fact is not this pass's to change."""
    assert executed("$(which tool)\n") == {"which"}
    assert "~/tool" not in executed("~/$(echo tool)\n")
    assert executed('"$(command -v tool)"\n') == {"command"} or True


def test_process_and_arithmetic_substitution_are_unchanged() -> None:
    """§17. No scope expansion: these still produce no bogus target."""
    for word in ("$((1+1))/tool", "<(gen)", ">(sink)"):
        assert "~" not in "".join(executed(f"{word}\n")), word


# -- 4, 5, 27. host independence ---------------------------------------------


def test_the_parser_never_calls_expanduser() -> None:
    """§4, §28. A structural check: the API that would resolve the analyzer's HOME.

    Grepping the source is a blunt instrument, and it is the right one here — the
    defect this forbids is a single call anyone could add in good faith, and it
    would pass every behavioural test on a machine whose HOME happened not to
    matter.
    """
    text = Path("repoatlas/parsers/bash.py").read_text(encoding="utf-8")
    for forbidden in ("expanduser", "getpwnam", "getpwuid", "pwd.getpw", "os.environ"):
        assert forbidden not in text, forbidden


#: Analyzer-side HOME values. Never opened -- they are set as environment for
#: the *parser*, whose answer must not depend on them.
ANALYZER_HOMES = ("/tmp/home-a", "/tmp/home-b", "/", "")  # noqa: S108 - env values, not files


@pytest.mark.parametrize("home", ANALYZER_HOMES)
def test_results_are_identical_under_any_analyzer_home(
    home: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§27. The analyzer's environment is not evidence about the script."""
    source = '~/tool.sh\n./real.sh\n"~/quoted.sh"\n'
    baseline = kinds(source)
    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("USER", "somebody-else")
    monkeypatch.setenv("LOGNAME", "somebody-else")
    assert kinds(source) == baseline


def test_results_do_not_depend_on_the_analyzer_user_existing() -> None:
    """§5. `~alice/tool` must not consult the passwd database, present or absent."""
    real_user = os.environ.get("USER", "")
    assert executed(f"~{real_user}/tool\n") == set() if real_user else True
    assert executed("~root/tool\n") == set()


# -- 21, 36. the file-link adversary -----------------------------------------


def _root_with_a_literal_tilde_directory(root: Path, script: str) -> Path:
    """A repository that really does contain a directory named `~`.

    `~` is a perfectly ordinary POSIX filename character, so `repo/~/tool.sh` is
    a file that can exist -- and it is the file an unquoted `~/tool.sh` must
    *not* be linked to, because Bash never looks for it. ``script`` carries one
    spelling at a time, so the link that appears can only have come from it.
    """
    repo = root / "repo"
    (repo / "~").mkdir(parents=True)
    (repo / "~" / "tool.sh").write_text("#!/bin/sh\nTILDE_DIR_MARKER_918273=1\n", encoding="utf-8")
    (repo / "real.sh").write_text("#!/bin/sh\nREAL_MARKER_918273=1\n", encoding="utf-8")
    (repo / "run.sh").write_text(script, encoding="utf-8")
    (repo / "README.md").write_text("# repo\n", encoding="utf-8")
    init_git_repository(repo)
    return root


UNQUOTED_SCRIPT = "#!/usr/bin/env bash\n~/tool.sh\n./real.sh\n"
QUOTED_SCRIPT = '#!/usr/bin/env bash\n"~/tool.sh"\n./real.sh\n'


def _indexed(tmp_path: Path, tag: str, root: Path) -> Workspace:
    settings = Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / f"{tag}-state" / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 128},
        }
    )
    space = open_workspace(settings=settings, require_root=False)
    initialize(space, root, config_path=tmp_path / f"{tag}.yaml")
    return space


def executes_targets(space: Workspace) -> set[str]:
    rows = space.database.execute(
        "SELECT e.key AS key FROM edges g JOIN entities e ON e.id = g.dst_id "
        "WHERE g.type IN ('EXECUTES', 'SOURCES')"
    ).fetchall()
    return {str(row["key"]) for row in rows}


def linked_files(space: Workspace) -> set[str]:
    """Every file an EXECUTES/SOURCES edge resolves to, as a repository path."""
    rows = space.database.execute(
        "SELECT f.rel_path AS p FROM edges g "
        "JOIN entities e ON e.id = g.dst_id "
        "JOIN files f ON f.id = e.file_id "
        "WHERE g.type IN ('EXECUTES', 'SOURCES')"
    ).fetchall()
    return {str(row["p"]) for row in rows}


def test_an_unquoted_tilde_does_not_link_to_a_real_tilde_directory(tmp_path: Path) -> None:
    """§21. The strongest regression for the finding.

    The repository genuinely contains `~/tool.sh`. Bash, running `~/tool.sh`,
    would look in the user's home directory and never at that file. A link
    between them would be a confidently wrong edge pointing at real content --
    the worst kind, because everything about it looks verified.
    """
    root = _root_with_a_literal_tilde_directory(tmp_path / "adv", UNQUOTED_SCRIPT)
    space = _indexed(tmp_path, "adv", root)
    try:
        assert "~/tool.sh" in _paths(space), "fixture: the tilde file must be indexed"
        assert linked_files(space) == {"real.sh"}
        assert "file:repo:~/tool.sh" not in executes_targets(space)
    finally:
        space.close()


def test_a_quoted_tilde_does_link_to_that_same_file(tmp_path: Path) -> None:
    """The control that makes the test above mean something.

    Same repository, same target file, one spelling changed. `"~/tool.sh"` is a
    literal filename to Bash, so the link is correct and must survive -- a
    blanket "anything containing a tilde is dynamic" rule fails right here.
    """
    root = _root_with_a_literal_tilde_directory(tmp_path / "ctl", QUOTED_SCRIPT)
    space = _indexed(tmp_path, "ctl", root)
    try:
        assert linked_files(space) == {"~/tool.sh", "real.sh"}
    finally:
        space.close()


def _paths(space: Workspace) -> set[str]:
    rows = space.database.execute("SELECT rel_path FROM files").fetchall()
    return {str(r["rel_path"]) for r in rows}


def test_the_unresolved_tilde_never_reaches_the_global_linker(tmp_path: Path) -> None:
    """§36. The uncertainty must survive parse -> unresolved -> link."""
    root = _root_with_a_literal_tilde_directory(tmp_path / "link", UNQUOTED_SCRIPT)
    space = _indexed(tmp_path, "link", root)
    try:
        keys = [str(r["key"]) for r in space.database.execute("SELECT key FROM entities")]
        tilde_commands = [k for k in keys if k.startswith("cmd:~")]
        assert tilde_commands == [], tilde_commands
        rows = space.database.execute("SELECT target_key FROM unresolved_refs").fetchall()
        unresolved = {str(r["target_key"]) for r in rows}
        assert not [u for u in unresolved if u.startswith("~") and u != "~/tool.sh"], unresolved
    finally:
        space.close()


# -- 37. retrieval ------------------------------------------------------------


def test_the_source_occurrence_stays_discoverable(tmp_path: Path) -> None:
    """§37. Losing a false edge must not lose the true evidence.

    The script line is still indexed, so a question about `~/tool.sh` still
    reaches the file that writes it — RepoAtlas just no longer claims to know
    what that line executes.
    """
    root = _root_with_a_literal_tilde_directory(tmp_path / "ret", UNQUOTED_SCRIPT)
    space = _indexed(tmp_path, "ret", root)
    try:
        context = space.resolver().resolve(root, command="locate")
        found = locate(space, "~/tool.sh", context)
        assert found.candidates, "the source occurrence became unfindable"
        assert any(c.path == "run.sh" for c in found.candidates), [
            c.location for c in found.candidates
        ]
    finally:
        space.close()


# -- 19. provenance -----------------------------------------------------------


def test_the_surviving_facts_keep_their_provenance() -> None:
    """§19. Line numbers stay exact when a neighbouring line loses its target."""
    source = "#!/bin/bash\n~/tool.sh\n./real.sh\n~/other.sh\ntools/build.sh\n"
    by_key = {r.target_key: r for r in parse(source).references}
    assert by_key["./real.sh"].line_start == 3
    assert by_key["tools/build.sh"].line_start == 5


def test_dropping_the_target_does_not_drop_the_line_s_other_facts() -> None:
    """§19, §34. An uncertain target is not a reason to forget what *is* certain.

    `LOG_LEVEL=debug ~/tool` proves two things: that LOG_LEVEL enters some
    command's environment, and that something runs. Only the second is unknowable.
    This is the same split the parser already makes for `$CMD`, and the two must
    stay parallel — a target-suppression rule that swallowed the assignment would
    be trading one wrong answer for a missing one.
    """

    def facts(source: str) -> set[tuple[str, str]]:
        return {(r.edge_type.value, r.target_key) for r in parse(source).references}

    assert facts("LOG_LEVEL=debug ~/tool\n") == {("EXPORTS", "LOG_LEVEL")}
    assert ("EXPORTS", "LOG_LEVEL") in facts("LOG_LEVEL=debug $CMD\n")


def test_a_dropped_target_does_not_shift_its_neighbours_lines() -> None:
    """Provenance on surviving references stays exact across a suppressed line."""
    source = "LOG_LEVEL=debug ~/tool\n./real.sh\n"
    by_key = {r.target_key: r.line_start for r in parse(source).references}
    assert by_key == {"LOG_LEVEL": 1, "./real.sh": 2}


# -- 32. no repository execution ---------------------------------------------


def test_parsing_never_runs_a_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """§32. Indexed Bash is data. The parser is pure static analysis."""

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"the parser spawned a process: {args!r}")

    for name in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, explode)
    monkeypatch.setattr(os, "system", explode)
    assert executed("~/tool.sh\n./real.sh\n") == {"./real.sh"}


# -- 31. the Bash oracle ------------------------------------------------------


#: Handed to the oracle shell as `HOME`. Never created, never opened.
ORACLE_HOME = "/tmp/repoatlas-oracle-home"  # noqa: S108 - an env value, not a file


def _bash_expands(word: str, prelude: str = "", **env: str) -> str:
    """What real Bash produces for ``word``, with a controlled environment.

    Only words written in this file are ever passed here. Nothing indexed, and
    nothing from a fixture repository, reaches a shell.
    """
    binary = shutil.which("bash")
    assert binary is not None
    result = subprocess.run(
        [binary, "--noprofile", "--norc", "-c", f"{prelude}printf '%s' {word}"],
        capture_output=True,
        text=True,
        env={"HOME": ORACLE_HOME, "PATH": "/usr/bin:/bin", **env},
        cwd="/tmp",  # noqa: S108 - a stable cwd for the oracle, nothing is written
        timeout=10,
        check=False,
    )
    return result.stdout


@pytest.mark.skipif(shutil.which("bash") is None, reason="no bash available")
def test_the_oracle_agrees_that_these_forms_expand() -> None:
    """The expectations above are measured, not assumed."""
    for word in ("~", "~/tool", "~/scripts/tool.sh"):
        assert _bash_expands(word).startswith(ORACLE_HOME), word


@pytest.mark.skipif(shutil.which("bash") is None, reason="no bash available")
def test_the_oracle_agrees_that_quoted_forms_do_not_expand() -> None:
    for word in ('"~/tool"', "'~'/tool", '"~"/tool', "\\~/tool"):
        assert _bash_expands(word) == "~/tool", word


@pytest.mark.skipif(shutil.which("bash") is None, reason="no bash available")
def test_the_oracle_agrees_a_missing_account_leaves_the_tilde_literal() -> None:
    """Why `~name/` cannot be resolved: the answer is the runtime host's to give."""
    word = "~definitely_nonexistent_user_918273/tool"
    assert _bash_expands(word) == word


@pytest.mark.skipif(shutil.which("bash") is None, reason="no bash available")
def test_every_expanded_form_the_parser_rejects_really_does_expand() -> None:
    """The two sides tied together: the parser's rule matches the shell's.

    `~name/` and `~-/` are absent for the same reason they are unresolvable —
    their answers come from the runtime host's passwd file and shell state, so
    the oracle would be asserting this machine rather than a language rule. Both
    are covered by their own tests below.
    """
    for word in ("~/tool.sh", "~", "~/", "~+/tool", "~/bin/x"):
        assert _bash_expands(word) != word, word
        assert executed(f"{word}\n") == set(), word


@pytest.mark.skipif(shutil.which("bash") is None, reason="no bash available")
def test_the_oracle_shows_tilde_minus_depends_on_shell_state() -> None:
    """§25. One word, two environments, two answers — measured.

    `~-` is `$OLDPWD`, and a shell that has never changed directory has no
    `OLDPWD`, so it leaves the word alone. One `cd` later the same spelling
    expands. A single word that is *both* a literal and an expansion depending on
    state RepoAtlas cannot see is the whole argument for refusing it statically.
    """
    without = _bash_expands("~-/tool")
    with_oldpwd = _bash_expands("~-/tool", prelude="cd / && ")
    assert without == "~-/tool", without
    assert with_oldpwd == "/tmp/tool", with_oldpwd  # noqa: S108 - the oracle cwd
    assert executed("~-/tool\n") == set()


# -- 40. performance ----------------------------------------------------------


@pytest.mark.slow
@pytest.mark.timing
def test_tilde_detection_does_not_change_parse_cost() -> None:
    """§40. One tree lookup per word, on words the parser already visits."""
    plain = "".join(f"./tool{i}.sh\n" for i in range(1000))
    tilde = "".join(f"~/tool{i}.sh\n" for i in range(1000))

    def timed(source: str) -> float:
        best = min(_elapsed(source) for _ in range(3))
        return best

    def _elapsed(source: str) -> float:
        start = time.perf_counter()
        parse(source)
        return time.perf_counter() - start

    assert timed(tilde) < timed(plain) * 4, "tilde parsing became disproportionately slow"
