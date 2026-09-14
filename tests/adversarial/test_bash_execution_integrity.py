"""Bash execution-target integrity (V1_SPEC §2, §7.3).

The invariant these tests exist to pin:

    A Bash ``EXECUTES`` edge names only a target explicitly recoverable from
    static source syntax. Variable expansion or runtime command construction
    never becomes a deterministic target.

RepoAtlas reads shell scripts; it does not run them. The distinction matters most
in command position, because that is where a shell decides *what runs* — and a
shell decides it at run time from values RepoAtlas cannot know. ``$CMD`` is not a
program named ``$CMD``; it is a program whose name this file does not contain.

Every case below is labelled with what it must resolve to:

``PROVEN TARGET``
    The source contains the target. A deterministic edge is required.
``UNRESOLVED TARGET``
    The shell computes the target. No deterministic edge may exist — and the
    absence is the assertion, not an omission.
``WRAPPER ONLY``
    A command runs another command, but which one is not statically decidable.
    The wrapper is a true fact; the wrapped target is not claimed.
``STATIC COMMAND``
    An ordinary ``PATH`` command named literally in the source.

Each test asserts both halves: what must exist, and what must not.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.models.parsing import EdgeType, EntityType, ParsedFile
from repoatlas.parsers.bash import BashParser
from repoatlas.services.init_service import initialize
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace
from tests.conftest import fact_snapshot, git, init_git_repository

pytestmark = pytest.mark.adversarial


# -- helpers -----------------------------------------------------------------


def parse(source: str, name: str = "script.sh") -> ParsedFile:
    return BashParser().parse(Path(name), source)


def targets(parsed: ParsedFile, edge: EdgeType, kind: EntityType | None = None) -> set[str]:
    return {
        reference.target_key
        for reference in parsed.references
        if reference.edge_type is edge and (kind is None or reference.target_kind is kind)
    }


def commands(source: str) -> set[str]:
    """Every ``EXECUTES`` target recorded as a bare command name."""
    return targets(parse(source), EdgeType.EXECUTES, EntityType.COMMAND)


def scripts(source: str) -> set[str]:
    """Every ``EXECUTES`` target recorded as a script/path."""
    return targets(parse(source), EdgeType.EXECUTES, EntityType.SCRIPT)


def executed(source: str) -> set[str]:
    """Every ``EXECUTES`` target, whatever kind it was recorded as."""
    return targets(parse(source), EdgeType.EXECUTES)


#: Command lines whose target the shell computes at run time. Nothing in this
#: list names a program: each one names a *place* a program name will come from.
DYNAMIC_INVOCATIONS = (
    "$CMD\n",
    '"$CMD"\n',
    "${SCRIPT}\n",
    '"${SCRIPT}"\n',
    "$BASE/$NAME\n",
    '"$BASE/$NAME"\n',
    "$BASE/script.sh\n",
    "${BASE}/script.sh\n",
    "prefix-$VAR\n",
    '"${commands[0]}"\n',
    '"$1"\n',
    "$1\n",
    '"${!INDIRECT}"\n',
    "${TOOL:-./fallback.sh}\n",
    "$(find_tool)\n",
    "`find_tool`\n",
    '"$(dirname "$0")/helper.sh"\n',
)

#: Entity keys that must never exist anywhere, in a parse or in the database.
#: Each one is a shell expansion mistaken for a program name.
FORBIDDEN_KEYS = (
    "$CMD",
    '"$CMD"',
    "${SCRIPT}",
    '"${SCRIPT}"',
    "$DEPLOY_CMD",
    '"$DEPLOY_CMD"',
    "${TOOL}",
    '"${TOOL}"',
    "$BASE/$NAME",
    "$BASE/script.sh",
    "${BASE}/script.sh",
    "prefix-$VAR",
    '"${commands[0]}"',
    '"$1"',
    '"${!INDIRECT}"',
    "$(find_tool)",
    "$WRAPPER",
    '"$RUNNER"',
    "$PROCESSOR",
    '"$PROCESSOR"',
    "$PROBE",
    '"$PROBE"',
    "$CONFIG",
    '"$CONFIG"',
    "$SCRIPT",
    '"$SCRIPT"',
)


# == 1. proven targets =======================================================


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("./deploy.sh\n", "./deploy.sh"),
        ("../scripts/deploy.sh\n", "../scripts/deploy.sh"),
        ("scripts/deploy.sh\n", "scripts/deploy.sh"),
        ("/opt/bin/deploy.sh\n", "/opt/bin/deploy.sh"),
        ("./deploy.sh --verbose\n", "./deploy.sh"),
    ],
)
def test_a_literal_script_path_is_a_proven_target(source: str, expected: str) -> None:
    """PROVEN TARGET — the path is in the file, so the edge is a fact."""
    assert scripts(source) == {expected}


@pytest.mark.parametrize(
    "source", ["make build\n", "systemctl restart web\n", "set -euo pipefail\n"]
)
def test_a_literal_command_name_is_a_static_command(source: str) -> None:
    """STATIC COMMAND — a `PATH` lookup, but the name itself is in the source."""
    assert commands(source) == {source.split()[0]}
    assert scripts(source) == set()


# == 2. dynamic targets: the finding =========================================


@pytest.mark.parametrize("source", DYNAMIC_INVOCATIONS)
def test_a_computed_command_target_resolves_to_nothing(source: str) -> None:
    """UNRESOLVED TARGET — the whole point of this pass.

    The source proves that *an* execution happens. It does not prove *what* is
    executed, so no deterministic target may be recorded (V1_SPEC §2). The
    previous behaviour recorded `cmd:$CMD` at confidence 1.0, which states that a
    program literally named `$CMD` exists and runs.
    """
    assert scripts(source) == set()
    assert not {key for key in executed(source) if "$" in key or "`" in key}


@pytest.mark.parametrize("source", DYNAMIC_INVOCATIONS)
def test_a_computed_target_is_not_downgraded_to_low_confidence(source: str) -> None:
    """UNRESOLVED TARGET — confidence is not permission to invent an entity.

    A half-confident `cmd:$CMD` is the same fabricated node with a smaller number
    attached to it; retrieval would still surface it as a thing that exists.
    """
    for reference in parse(source).references:
        assert "$" not in reference.target_key, reference
        assert "`" not in reference.target_key, reference


def test_an_assignment_is_not_execution_proof() -> None:
    """UNRESOLVED TARGET — the review's headline case.

    `CMD=./rollback.sh` followed by `$CMD` is exactly how a shell script runs a
    script, and RepoAtlas still may not claim it: V1 does no constant propagation,
    so the file, read statically, does not connect the two lines.
    """
    parsed = parse("CMD=./rollback.sh\n$CMD\n")
    assert targets(parsed, EdgeType.EXECUTES) == set()


def test_a_command_substitution_body_is_still_read_literally() -> None:
    """PROVEN TARGET inside, UNRESOLVED TARGET outside.

    `$(find_tool)` runs `find_tool` and then runs whatever it printed. The first
    half is written down; the second half is not.
    """
    parsed = parse("$(find_tool)\n")
    assert targets(parsed, EdgeType.EXECUTES, EntityType.COMMAND) == {"find_tool"}


def test_a_dynamic_invocation_still_records_the_variable_it_reads() -> None:
    """The environment fact survives; only the invented target is dropped.

    `"$DEPLOY_TARGET"` in command position proves the script consults
    DEPLOY_TARGET — which is true, useful, and not a claim about what runs.
    """
    parsed = parse('export DEPLOY_TARGET\n"$DEPLOY_TARGET"\n')
    assert "DEPLOY_TARGET" in targets(parsed, EdgeType.READS)
    assert targets(parsed, EdgeType.EXECUTES) == set()


def test_a_dynamic_invocation_still_records_its_leading_assignments() -> None:
    """`FOO=bar $CMD` puts FOO in the child environment whatever the child is."""
    parsed = parse("LOG_LEVEL=debug $CMD\n")
    assert "LOG_LEVEL" in targets(parsed, EdgeType.EXPORTS)
    assert targets(parsed, EdgeType.EXECUTES) == set()


# == 3. quoting is syntax, not identity ======================================


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("'./quoted-script.sh'\n", "./quoted-script.sh"),
        ('"./quoted-script.sh"\n', "./quoted-script.sh"),
        ('"./scripts/run migration.sh"\n', "./scripts/run migration.sh"),
        ("'./scripts/run migration.sh'\n", "./scripts/run migration.sh"),
        ("./scripts/run\\ migration.sh\n", "./scripts/run migration.sh"),
    ],
)
def test_a_quoted_literal_path_keeps_its_real_identity(source: str, expected: str) -> None:
    """PROVEN TARGET — quotes belong to the shell, not to the filename.

    `cmd:'./deploy.sh'` and `cmd:./deploy.sh` are two different graph nodes for
    one file, and the quoted one joins to nothing.
    """
    assert scripts(source) == {expected}
    assert not {key for key in executed(source) if '"' in key or "'" in key}


def test_a_quoted_path_is_one_target_not_several() -> None:
    """A space inside quotes is part of the name, not a command separator."""
    parsed = parse('"./scripts/run migration.sh"\n')
    assert len(targets(parsed, EdgeType.EXECUTES)) == 1


def test_a_quoted_plain_command_is_unquoted_too() -> None:
    """STATIC COMMAND — `"make"` is `make`."""
    assert commands('"make" build\n') == {"make"}


# == 4. assignment before command ============================================


@pytest.mark.parametrize(
    ("source", "target", "kind"),
    [
        ("FOO=bar ./app\n", "./app", EntityType.SCRIPT),
        ('FOO="a b" ./app\n', "./app", EntityType.SCRIPT),
        ("FOO= ./app\n", "./app", EntityType.SCRIPT),
        ("FOO=bar BAR=baz python app.py\n", "python", EntityType.COMMAND),
    ],
)
def test_a_leading_assignment_is_not_the_command(
    source: str, target: str, kind: EntityType
) -> None:
    """PROVEN TARGET / STATIC COMMAND — `NAME=value` is environment, not argv[0]."""
    assert target in targets(parse(source), EdgeType.EXECUTES, kind)
    assert not {key for key in executed(source) if "=" in key}


def test_leading_assignments_reach_the_child_environment() -> None:
    exported = targets(parse("FOO=bar DEPLOY_ENV=prod python app.py\n"), EdgeType.EXPORTS)
    assert "DEPLOY_ENV" in exported


# == 5. wrapper policy =======================================================
#
# Each wrapper is either unwrapped with a written-down option grammar, or not
# unwrapped at all. There is no third state, and no wrapper is unwrapped by
# resemblance to another one.


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # env
        ("env ./notify.sh\n", "./notify.sh"),
        ("env -i ./notify.sh\n", "./notify.sh"),
        ("env A=1 B=2 ./notify.sh\n", "./notify.sh"),
        ("/usr/bin/env ./notify.sh\n", "./notify.sh"),
        # exec / nohup / command
        ("exec ./notify.sh\n", "./notify.sh"),
        ("exec -l ./notify.sh\n", "./notify.sh"),
        ("nohup ./notify.sh\n", "./notify.sh"),
        ("command ./notify.sh\n", "./notify.sh"),
        ("command -p ./notify.sh\n", "./notify.sh"),
        # timeout: DURATION is one positional operand before the command
        ("timeout 30 ./notify.sh\n", "./notify.sh"),
        ("timeout 1.5m ./notify.sh\n", "./notify.sh"),
        ("timeout --signal=TERM 30 ./notify.sh\n", "./notify.sh"),
        ("timeout -s TERM 30 ./notify.sh\n", "./notify.sh"),
        ("timeout -k 5 30 ./notify.sh\n", "./notify.sh"),
        ("timeout --preserve-status 30 ./notify.sh\n", "./notify.sh"),
        ("timeout --foreground 30 ./notify.sh\n", "./notify.sh"),
        # `--` ends option parsing; DURATION and COMMAND still follow it.
        ("timeout -- 30 ./notify.sh\n", "./notify.sh"),
        ("env -- ./notify.sh\n", "./notify.sh"),
        # nice
        ("nice ./notify.sh\n", "./notify.sh"),
        ("nice -n 10 ./notify.sh\n", "./notify.sh"),
        ("nice --adjustment=10 ./notify.sh\n", "./notify.sh"),
        ("nice --adjustment 10 ./notify.sh\n", "./notify.sh"),
        # time
        ("time ./notify.sh\n", "./notify.sh"),
        ("time -p ./notify.sh\n", "./notify.sh"),
        # setsid
        ("setsid ./notify.sh\n", "./notify.sh"),
        ("setsid -f ./notify.sh\n", "./notify.sh"),
        ("setsid --fork --wait ./notify.sh\n", "./notify.sh"),
    ],
)
def test_a_transparent_wrapper_is_seen_through(source: str, expected: str) -> None:
    """PROVEN TARGET — the wrapper's grammar decides which word is the command."""
    assert expected in scripts(source), source


@pytest.mark.parametrize(
    "source",
    [
        "env ./notify.sh\n",
        "timeout 30 ./notify.sh\n",
        "nice -n 10 ./notify.sh\n",
        "time ./notify.sh\n",
    ],
)
def test_an_unwrapped_wrapper_is_not_reported_as_the_command(source: str) -> None:
    """`EXECUTES timeout` answers a question nobody asked and hides the real one."""
    assert commands(source) == set(), source


@pytest.mark.parametrize(
    ("source", "wrapper"),
    [
        # sudo — its options consume arguments (`-u user`, `-g group`) and it
        # accepts `--`; identifying the command means implementing sudo's grammar.
        ("sudo ./restart.sh\n", "sudo"),
        ("sudo -u deploy ./restart.sh\n", "sudo"),
        # xargs — the final argv is built from *stdin*, and `-I`/`-n`/`-a` reshape
        # it. The command word is there, but what xargs does with it is not.
        ("xargs ./process.sh\n", "xargs"),
        ("xargs -I{} ./process.sh {}\n", "xargs"),
        # chroot — the wrapped path resolves inside a *different root*, so
        # `./run.sh` is not this repository's `./run.sh` at all.
        ("chroot /jail ./run.sh\n", "chroot"),
        # stdbuf / ionice — safe in principle, but their attached-value option
        # forms (`-oL`, `-c3`) are grammar no other wrapper needs, for commands
        # that are rare in deployment scripts. Out of V1 scope.
        ("stdbuf -oL ./stream.sh\n", "stdbuf"),
        ("ionice -c 3 ./bulk.sh\n", "ionice"),
    ],
)
def test_a_wrapper_outside_the_policy_is_reported_as_itself(source: str, wrapper: str) -> None:
    """WRAPPER ONLY — the wrapper genuinely runs, so naming it is true.

    Not claiming the wrapped script is a limitation, not a fabrication. The
    difference is that a limitation omits a fact; a fabrication adds one.
    """
    assert commands(source) == {wrapper}, source
    assert scripts(source) == set(), source


@pytest.mark.parametrize(
    ("source", "wrapper"),
    [
        ("env -u PATH ./notify.sh\n", "env"),  # -u consumes the next word
        ("env -C /tmp ./notify.sh\n", "env"),  # -C consumes the next word
        ("env -S './notify.sh a b'\n", "env"),  # -S re-splits its argument
        ("env -0\n", "env"),  # -0 prints; it runs nothing
        ("command -v ./notify.sh\n", "command"),  # -v prints a path
        ("command -V ./notify.sh\n", "command"),
        ("exec -a other ./notify.sh\n", "exec"),  # -a consumes the next word
        ("nohup -x ./notify.sh\n", "nohup"),  # nohup has no options
        ("timeout --unknown 30 ./notify.sh\n", "timeout"),
        ("timeout -x 30 ./notify.sh\n", "timeout"),
        ("nice -10 ./notify.sh\n", "nice"),  # obsolete adjustment form
        ("nice --unknown ./notify.sh\n", "nice"),
        ("time -o out.txt ./notify.sh\n", "time"),  # /usr/bin/time, not the keyword
        ("setsid --unknown ./notify.sh\n", "setsid"),
        # `--` after the duration: the operand count and the grammar disagree
        # about this line, so it is not read as a command named `--`.
        ("timeout 30 -- ./notify.sh\n", "timeout"),
    ],
)
def test_an_unrecognized_wrapper_flag_fails_closed(source: str, wrapper: str) -> None:
    """WRAPPER ONLY — an unknown flag may consume the next word, so counting
    positions past it is guessing. Unwrapping stops and the wrapper is reported."""
    assert scripts(source) == set(), source
    assert commands(source) == {wrapper}, source


@pytest.mark.parametrize(
    ("source", "wrapper"),
    [
        ("env\n", "env"),
        ("nohup\n", "nohup"),
        ("timeout 30\n", "timeout"),  # a duration and nothing to run
        ("nice\n", "nice"),
        ("time\n", "time"),
    ],
)
def test_a_wrapper_with_nothing_to_wrap_is_reported_as_itself(source: str, wrapper: str) -> None:
    assert commands(source) == {wrapper}, source
    assert scripts(source) == set(), source


@pytest.mark.parametrize(
    "source",
    [
        'env "$WRAPPED"\n',
        "env $WRAPPED\n",
        'env LOG_LEVEL=debug "$SCRIPT"\n',
        'exec "$TARGET"\n',
        'nohup "${BACKGROUND}"\n',
        'timeout 30 "$CMD"\n',
        'nice -n 10 "$CMD"\n',
        'time "$CMD"\n',
        'setsid "$CMD"\n',
    ],
)
def test_a_computed_target_behind_a_wrapper_stays_unresolved(source: str) -> None:
    """UNRESOLVED TARGET — a known wrapper does not make an unknown target known."""
    assert scripts(source) == set(), source
    assert not {key for key in executed(source) if "$" in key}, source


def test_a_computed_operand_does_not_block_a_proven_target() -> None:
    """PROVEN TARGET — `timeout "$T" ./x.sh` computes the *duration*, not the
    command. Position is what identifies the command, and position is static."""
    assert scripts('timeout "$TIMEOUT" ./migrate.sh\n') == {"./migrate.sh"}


def test_wrapped_assignments_reach_the_child_environment() -> None:
    for source in ("env LOG_LEVEL=debug ./notify.sh\n", "LOG_LEVEL=debug ./notify.sh\n"):
        assert "LOG_LEVEL" in targets(parse(source), EdgeType.EXPORTS), source


def test_a_non_env_wrapper_does_not_accept_assignment_operands() -> None:
    """WRAPPER ONLY — only `env` gives `NAME=value` operands their POSIX meaning;
    for anything else the token is not a command and must not become one."""
    assert commands("nohup A=1 ./notify.sh\n") == {"nohup"}
    assert scripts("nohup A=1 ./notify.sh\n") == set()


def test_a_wrapped_plain_command_is_recorded_as_that_command() -> None:
    assert commands("env LC_ALL=C sort\n") == {"sort"}
    assert commands("timeout 30 systemctl restart web\n") == {"systemctl"}


def test_a_wrapped_interpreter_invocation_is_recovered() -> None:
    """The §21 chain uses exactly this form: a wrapper before the interpreter."""
    parsed = parse("env PYTHONPATH=/opt python3 ../python-service/src/app.py\n")
    assert "../python-service/src/app.py" in targets(parsed, EdgeType.EXECUTES, EntityType.SCRIPT)


def test_a_wrapped_salt_invocation_is_recovered() -> None:
    parsed = parse("env SALT_ENV=prod salt-call state.apply service\n")
    assert "service" in targets(parsed, EdgeType.REFERENCES, EntityType.SALT_STATE)


# == 6. shell -c =============================================================


@pytest.mark.parametrize(
    ("source", "shell"),
    [
        ("sh -c './x.sh'\n", "sh"),
        ('sh -c "./x.sh"\n', "sh"),
        ("bash -c './x.sh'\n", "bash"),
        ('bash -c "./x.sh"\n', "bash"),
        ('zsh -c "./x.sh"\n', "zsh"),
        ('bash -c "./x.sh --flag && ./y.sh"\n', "bash"),
    ],
)
def test_a_shell_c_payload_is_code_not_a_script_path(source: str, shell: str) -> None:
    """STATIC COMMAND — the shell is executed; its argument is a program *text*.

    `bash -c "./x.sh"` used to produce a deterministic edge to `./x.sh` while
    `sh -c './x.sh'` produced none, purely because tree-sitter gives single- and
    double-quoted strings different node types. Both now report the shell alone:
    the payload is arbitrary shell source, and parsing it out of a string would be
    a second, unwritten shell parser (V1_SPEC §7.3).
    """
    assert commands(source) == {shell}, source
    assert scripts(source) == set(), source


def test_an_interpreter_c_payload_is_not_mistaken_for_a_script() -> None:
    """`python -c "run app.py"` executes code that mentions a filename."""
    assert scripts('python -c "run app.py"\n') == set()
    assert commands('python -c "run app.py"\n') == {"python"}


def test_a_shell_running_a_literal_script_file_still_resolves() -> None:
    """PROVEN TARGET — no `-c`, so the argument is a path, not a program text."""
    assert scripts("bash ./x.sh\n") == {"./x.sh"}
    assert scripts("sh scripts/setup.sh\n") == {"scripts/setup.sh"}


# == 7. interpreters =========================================================


@pytest.mark.parametrize(
    ("source", "interpreter", "script"),
    [
        ("python app.py\n", "python", "app.py"),
        ("python3 app.py\n", "python3", "app.py"),
        ("python3 -u app.py\n", "python3", "app.py"),
        ("ruby tool.rb\n", "ruby", "tool.rb"),
        ("bash script.sh\n", "bash", "script.sh"),
        ("sh script.sh\n", "sh", "script.sh"),
    ],
)
def test_an_interpreter_and_its_literal_script_are_both_recorded(
    source: str, interpreter: str, script: str
) -> None:
    """PROVEN TARGET — the file names both the interpreter and the file it runs."""
    assert commands(source) == {interpreter}
    assert scripts(source) == {script}


def test_an_absolute_interpreter_path_keeps_its_script() -> None:
    """PROVEN TARGET — `/usr/bin/python3 app.py` used to record the interpreter
    path and silently drop `app.py`, losing the only cross-file half of the fact."""
    assert "app.py" in scripts("/usr/bin/python3 app.py\n")


@pytest.mark.parametrize(
    "source",
    ['python "$SCRIPT"\n', "python3 ${ENTRYPOINT}\n", 'ruby "$TASK"\n', 'bash "$STAGE"\n'],
)
def test_an_interpreter_with_a_computed_script_resolves_no_script(source: str) -> None:
    """UNRESOLVED TARGET — the interpreter is proven, the script is not."""
    assert scripts(source) == set(), source
    assert commands(source) == {source.split()[0]}, source


# == 8. source / . ===========================================================


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("source ./env.sh\n", "./env.sh"),
        (". ./env.sh\n", "./env.sh"),
        ("source '../shared/common.sh'\n", "../shared/common.sh"),
        ('source "./config/prod.sh"\n', "./config/prod.sh"),
    ],
)
def test_a_literal_sourced_path_resolves(source: str, expected: str) -> None:
    """PROVEN TARGET."""
    assert targets(parse(source), EdgeType.SOURCES) == {expected}


@pytest.mark.parametrize(
    "source",
    [
        'source "$CONFIG"\n',
        "source $CONFIG\n",
        '. "$CONFIG"\n',
        ". $CONFIG\n",
        'source "${BASE}/env.sh"\n',
        "source $(config_path)\n",
    ],
)
def test_a_computed_sourced_path_resolves_to_nothing(source: str) -> None:
    """UNRESOLVED TARGET — a file named `$CONFIG` does not exist."""
    assert targets(parse(source), EdgeType.SOURCES) == set(), source


# == 9. pipelines, substitution, subshells, eval ==============================


def test_each_pipeline_stage_is_evaluated_independently() -> None:
    """PROVEN TARGET per stage — a pipeline is several commands, not one."""
    parsed = parse("cat input | ./processor.sh | tee output\n")
    assert targets(parsed, EdgeType.EXECUTES, EntityType.SCRIPT) == {"./processor.sh"}
    assert targets(parsed, EdgeType.EXECUTES, EntityType.COMMAND) == {"cat", "tee"}


def test_a_dynamic_pipeline_stage_does_not_poison_its_neighbours() -> None:
    """UNRESOLVED for one stage, STATIC COMMAND for the other."""
    parsed = parse('cat input | "$PROCESSOR" | tee output\n')
    assert targets(parsed, EdgeType.EXECUTES, EntityType.COMMAND) == {"cat", "tee"}
    assert targets(parsed, EdgeType.EXECUTES, EntityType.SCRIPT) == set()


def test_a_literal_command_substitution_resolves() -> None:
    """PROVEN TARGET — `$(./probe.sh)` really does run `./probe.sh`."""
    assert scripts("RESULT=$(./probe.sh)\n") == {"./probe.sh"}


def test_a_computed_command_substitution_resolves_to_nothing() -> None:
    """UNRESOLVED TARGET."""
    assert executed('RESULT=$("$PROBE")\n') == set()


@pytest.mark.parametrize("source", ["( ./deploy.sh )\n", "{ ./deploy.sh; }\n"])
def test_a_grouped_command_keeps_its_proven_target(source: str) -> None:
    assert scripts(source) == {"./deploy.sh"}


@pytest.mark.parametrize("source", ['( "$CMD" )\n', '{ "$CMD"; }\n'])
def test_a_grouped_dynamic_command_stays_unresolved(source: str) -> None:
    assert executed(source) == set(), source


@pytest.mark.parametrize("source", ['eval "./deploy.sh"\n', 'eval "$COMMAND"\n', "eval $CMD\n"])
def test_eval_never_reaches_through_to_its_argument(source: str) -> None:
    """STATIC COMMAND — `eval` runs; what it builds at run time is text.

    `eval "./deploy.sh"` looks like proof and is not: the shell re-parses that
    string with expansion, so the same syntax with a different variable in scope
    runs something else entirely.
    """
    assert commands(source) == {"eval"}, source
    assert scripts(source) == set(), source


# == 10. indirect wrappers ===================================================


@pytest.mark.parametrize(
    "source",
    [
        "WRAPPER=env\n$WRAPPER ./deploy.sh\n",
        'RUNNER=timeout\n"$RUNNER" 30 ./deploy.sh\n',
        "S=sudo\n$S ./deploy.sh\n",
    ],
)
def test_a_computed_wrapper_name_resolves_to_nothing_at_all(source: str) -> None:
    """UNRESOLVED TARGET — V1 does no constant propagation, so the command word is
    unknown; and because it is unknown, so is whether it wraps anything. Neither
    the wrapper nor the argument may become an edge."""
    assert executed(source) == set(), source


# == 11. malformed input =====================================================


@pytest.mark.parametrize(
    "source",
    [
        './deploy.sh "unterminated\n',
        "./deploy.sh $(unterminated\n",
        "cat input |\n",
        "deploy() {\n  ./inner.sh\n",
        "./deploy.sh > \n",
        "if [ -f x ]; then\n",
        "${\n",
        '"\n',
        "$(\n",
        "`\n",
    ],
)
def test_malformed_shell_fabricates_no_targets(source: str) -> None:
    """Broken syntax must lose facts, never invent them."""
    for reference in parse(source).references:
        assert "$" not in reference.target_key, (source, reference)
        assert "`" not in reference.target_key, (source, reference)
        assert '"' not in reference.target_key, (source, reference)
        assert "{" not in reference.target_key, (source, reference)


def test_a_truncated_function_still_yields_its_inner_literal_command() -> None:
    """Conservative, not destructive: recovery keeps what is unambiguous."""
    assert "./inner.sh" in scripts("deploy() {\n  ./inner.sh\n")


# == 12. the graph, not just the parser ======================================


#: Scripts that sit beside `run.sh` so its literal references have something real
#: to resolve to. Without them a missing edge would prove nothing — a target that
#: is not in the tree produces no edge whether or not the parser claimed it.
SIBLING_SCRIPTS = (
    "deploy.sh",
    "rollback.sh",
    "migrate.sh",
    "restart.sh",
    "notify.sh",
    "env.sh",
)


def _bash_root(root: Path, script: str) -> Path:
    """A two-repository ecosystem whose Bash script is the only thing that varies.

    `repo-a/scripts/run.sh` is the file under test; its siblings and
    `repo-b/tool.py` are real targets for it to reach — or fail to reach.
    """
    (root / "repo-a" / "scripts").mkdir(parents=True)
    (root / "repo-a" / "scripts" / "run.sh").write_text(script, encoding="utf-8")
    for name in SIBLING_SCRIPTS:
        # Inert on purpose: they exist to be *resolved to*, and any command
        # they ran would show up in the same edge set the assertions read.
        (root / "repo-a" / "scripts" / name).write_text(f"#!/bin/sh\n# {name}\n", encoding="utf-8")
    (root / "repo-a" / "README.md").write_text("# repo-a\n", encoding="utf-8")
    (root / "repo-b").mkdir(parents=True)
    (root / "repo-b" / "tool.py").write_text("def main() -> None:\n    pass\n", encoding="utf-8")
    for name in ("repo-a", "repo-b"):
        init_git_repository(root / name)
    return root


DYNAMIC_SCRIPT = """\
#!/usr/bin/env bash
CMD=./rollback.sh
$CMD
DEPLOY_CMD="./deploy.sh"
"$DEPLOY_CMD"
TOOL="./tool"
"${TOOL}" run
TARGET=../../repo-b/tool.py
python "$TARGET"
source "$CONFIG"
"""

LITERAL_SCRIPT = """\
#!/usr/bin/env bash
./rollback.sh
./deploy.sh
timeout 30 ./migrate.sh
sudo ./restart.sh
python ../../repo-b/tool.py
source ./env.sh
"""


def _workspace(tmp_path: Path, tag: str, root: Path) -> Workspace:
    settings = Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / f"{tag}-state" / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 128},
        }
    )
    space = open_workspace(settings=settings, require_root=False)
    initialize(space, root, config_path=tmp_path / f"{tag}.yaml")
    return space


def entity_keys(space: Workspace) -> list[str]:
    return [str(row["key"]) for row in space.database.execute("SELECT key FROM entities")]


def executes_targets(space: Workspace, repo: str) -> set[str]:
    """Every entity an `EXECUTES` or `SOURCES` edge from ``repo`` points at."""
    rows = space.database.execute(
        "SELECT e.key AS key FROM edges g "
        "JOIN entities e ON e.id = g.dst_id "
        "JOIN repositories r ON r.id = g.repo_id "
        "WHERE g.type IN ('EXECUTES', 'SOURCES') AND r.name = ?",
        (repo,),
    ).fetchall()
    return {str(row["key"]) for row in rows}


def unresolved_targets(space: Workspace) -> set[str]:
    rows = space.database.execute("SELECT target_key FROM unresolved_refs").fetchall()
    return {str(row["target_key"]) for row in rows}


def test_no_expansion_ever_becomes_a_graph_entity(tmp_path: Path) -> None:
    """The finding, checked where it does damage: the database.

    A parser assertion proves the reference was not emitted. This proves nothing
    downstream reconstructed it — interning, claim resolution, or the global link
    pass — which is the only check a user's `locate` actually depends on.
    """
    root = _bash_root(tmp_path / "dyn", DYNAMIC_SCRIPT)
    space = _workspace(tmp_path, "dyn", root)
    try:
        keys = entity_keys(space)
        assert not [key for key in keys if "$" in key or "`" in key], keys
        for forbidden in FORBIDDEN_KEYS:
            assert f"cmd:{forbidden}" not in keys
            assert forbidden not in unresolved_targets(space)
    finally:
        space.close()


def test_a_dynamic_script_asserts_no_execution_relationship(tmp_path: Path) -> None:
    """The guard is surgical, not a mute button.

    `python "$TARGET"` proves the interpreter and nothing else, so `cmd:python`
    survives and every path target — including `repo-b/tool.py`, which is really
    there and would resolve if it were claimed — does not.
    """
    root = _bash_root(tmp_path / "dyn2", DYNAMIC_SCRIPT)
    space = _workspace(tmp_path, "dyn2", root)
    try:
        assert executes_targets(space, "repo-a") == {"cmd:python"}
    finally:
        space.close()


def test_a_literal_script_still_asserts_what_it_proves(tmp_path: Path) -> None:
    """The guard must not be a mute button: literal targets still resolve."""
    root = _bash_root(tmp_path / "lit", LITERAL_SCRIPT)
    space = _workspace(tmp_path, "lit", root)
    try:
        found = executes_targets(space, "repo-a")
        assert "cmd:sudo" in found
        assert any(key.endswith("repo-b:tool.py") for key in found), found
    finally:
        space.close()


def test_a_literal_cross_repository_script_link_is_preserved(tmp_path: Path) -> None:
    """§28 — a literal `../../repo-b/tool.py` crosses the repository boundary."""
    root = _bash_root(tmp_path / "xrepo", LITERAL_SCRIPT)
    space = _workspace(tmp_path, "xrepo", root)
    try:
        rows = space.database.execute(
            "SELECT COUNT(*) AS n FROM edges g "
            "JOIN entities dst ON dst.id = g.dst_id "
            "JOIN repositories src ON src.id = g.repo_id "
            "WHERE g.type = 'EXECUTES' AND src.name = 'repo-a' "
            "AND dst.key LIKE 'file:%repo-b%'"
        ).fetchone()
        assert int(rows["n"]) >= 1
    finally:
        space.close()


# == 13. incremental transitions =============================================


def _sync_all(space: Workspace, root: Path) -> None:
    sync(space, space.resolver().resolve(root))


def _rewrite(root: Path, script: str) -> None:
    path = root / "repo-a" / "scripts" / "run.sh"
    path.write_text(script, encoding="utf-8")
    git(root / "repo-a", "add", "-A")
    git(root / "repo-a", "commit", "--quiet", "-m", "rewrite")


def _clean_snapshot(tmp_path: Path, tag: str, root: Path) -> dict[str, object]:
    space = _workspace(tmp_path, f"clean-{tag}", root)
    try:
        return fact_snapshot(space)
    finally:
        space.close()


@pytest.mark.parametrize(
    ("literal", "dynamic", "proven"),
    [
        ("./deploy.sh\n", '"$DEPLOY_CMD"\n', "deploy.sh"),
        ("source ./env.sh\n", 'source "$CONFIG"\n', "env.sh"),
        ("python ../../repo-b/tool.py\n", 'python "$TARGET"\n', "tool.py"),
        ("timeout 30 ./migrate.sh\n", 'timeout 30 "$MIGRATION"\n', "migrate.sh"),
        ("env ./notify.sh\n", 'env "$NOTIFIER"\n', "notify.sh"),
    ],
)
def test_a_literal_target_that_becomes_dynamic_leaves_no_stale_edge(
    tmp_path: Path, literal: str, dynamic: str, proven: str
) -> None:
    """§29 — the edge must disappear when the proof does, and come back with it.

    ``proven`` is matched against the resolved entity key, which names the file
    the reference reached (``file:repo-a:scripts/deploy.sh``) rather than the text
    that was written. That is the point: the assertion is about the relationship,
    not about the spelling.
    """
    tag = proven.replace(".", "_")
    root = _bash_root(tmp_path / f"inc-{tag}", literal)
    space = _workspace(tmp_path, f"inc-{tag}", root)
    try:

        def proven_present() -> bool:
            return any(proven in key for key in executes_targets(space, "repo-a"))

        assert proven_present(), literal

        _rewrite(root, dynamic)
        _sync_all(space, root)
        assert not proven_present(), dynamic
        assert not [key for key in entity_keys(space) if "$" in key]

        _rewrite(root, literal)
        _sync_all(space, root)
        assert proven_present(), literal
    finally:
        space.close()


def test_an_incremental_bash_transition_equals_a_clean_reindex(tmp_path: Path) -> None:
    """§28 — incremental state must be indistinguishable from a fresh index."""
    root = _bash_root(tmp_path / "equiv", LITERAL_SCRIPT)
    space = _workspace(tmp_path, "equiv", root)
    try:
        _rewrite(root, DYNAMIC_SCRIPT)
        _sync_all(space, root)
        assert fact_snapshot(space) == _clean_snapshot(tmp_path, "dyn", root)

        _rewrite(root, LITERAL_SCRIPT)
        _sync_all(space, root)
        assert fact_snapshot(space) == _clean_snapshot(tmp_path, "lit", root)
    finally:
        space.close()


# == 14. retrieval ===========================================================


def test_an_assignment_does_not_make_a_script_look_executed(tmp_path: Path) -> None:
    """§27 — `CMD=./rollback.sh` + `$CMD` must not answer "what executes it?".

    The literal text is still findable, because it is genuinely in the file. What
    must not exist is a *relationship* claiming the script runs it.
    """
    from repoatlas.services.retrieval_service import locate

    root = _bash_root(tmp_path / "ret", DYNAMIC_SCRIPT)
    space = _workspace(tmp_path, "ret", root)
    try:
        rows = space.database.execute(
            "SELECT e.key AS key FROM edges g JOIN entities e ON e.id = g.dst_id "
            "WHERE g.type = 'EXECUTES'"
        ).fetchall()
        assert not [row for row in rows if "rollback" in str(row["key"])]

        # The text is still there, so retrieval still finds the file — what must
        # not exist is the graph edge that would let an answer say "run.sh
        # executes rollback.sh".
        assert space.search.search_literal("rollback.sh")
        context = space.resolver().resolve(root, command="locate")
        evidence = locate(space, "What executes rollback.sh?", context)
        assert evidence.candidates
    finally:
        space.close()


def test_the_forbidden_keys_are_absent_from_the_whole_adversarial_ecosystem(
    adversarial: Workspace,
) -> None:
    """A sweep over the real fixture set, not a constructed one."""
    keys = entity_keys(adversarial)
    assert not [key for key in keys if "$" in key or "`" in key], [
        key for key in keys if "$" in key or "`" in key
    ]
    assert not [key for key in keys if key.startswith("cmd:") and ('"' in key or "'" in key)]


def test_command_entity_metadata_never_carries_an_expansion(adversarial: Workspace) -> None:
    """The meta blob travels into evidence; a fabricated target there is the same
    fabrication one indirection away."""
    rows = adversarial.database.execute(
        "SELECT meta_json FROM unresolved_refs WHERE meta_json IS NOT NULL"
    ).fetchall()
    for row in rows:
        meta = json.loads(str(row["meta_json"]))
        for value in meta.values():
            assert "$" not in str(value), meta
