"""Bash invocation forms (V1_SPEC §7.3, §19).

A wrapper such as ``env`` or ``exec`` stands between the script and the command it
actually runs. Recording only the wrapper is not merely incomplete — it states
that the script executes ``env``, which is a misleading answer to "what does this
script run?". These tests pin both halves: the wrapped target is recovered when it
is statically knowable, and nothing is recovered when it is not.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repoatlas.models.parsing import EdgeType, EntityType, ParsedFile
from repoatlas.parsers.bash import BashParser

pytestmark = pytest.mark.adversarial


def parse(source: str) -> ParsedFile:
    return BashParser().parse(Path("script.sh"), source)


def targets(parsed: ParsedFile, edge: EdgeType, kind: EntityType | None = None) -> set[str]:
    return {
        reference.target_key
        for reference in parsed.references
        if reference.edge_type is edge and (kind is None or reference.target_kind is kind)
    }


# -- wrappers that must be seen through -------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("env LOG_LEVEL=debug ./notify.sh\n", "./notify.sh"),
        ("env ./notify.sh\n", "./notify.sh"),
        ("env -i ./notify.sh\n", "./notify.sh"),
        ("env A=1 B=2 ./notify.sh\n", "./notify.sh"),
        ("exec ./notify.sh\n", "./notify.sh"),
        ("nohup ./notify.sh\n", "./notify.sh"),
        ("command ./notify.sh\n", "./notify.sh"),
    ],
)
def test_a_wrapped_script_invocation_is_recovered(source: str, expected: str) -> None:
    assert expected in targets(parse(source), EdgeType.EXECUTES, EntityType.SCRIPT)


def test_a_wrapped_interpreter_invocation_is_recovered() -> None:
    """The §21 chain uses exactly this form: a wrapper in front of the interpreter."""
    parsed = parse("env PYTHONPATH=/opt python3 ../python-service/src/app.py\n")
    assert "../python-service/src/app.py" in targets(parsed, EdgeType.EXECUTES, EntityType.SCRIPT)


def test_a_wrapped_salt_invocation_is_recovered() -> None:
    parsed = parse("env SALT_ENV=prod salt-call state.apply service\n")
    assert "service" in targets(parsed, EdgeType.REFERENCES, EntityType.SALT_STATE)


def test_the_wrapper_is_not_reported_as_the_executed_command() -> None:
    """`EXECUTES env` answers a question nobody asked and hides the real one."""
    commands = targets(
        parse("env LOG_LEVEL=debug ./notify.sh\n"), EdgeType.EXECUTES, EntityType.COMMAND
    )
    assert "env" not in commands


def test_a_wrapped_plain_command_is_recorded_as_that_command() -> None:
    commands = targets(parse("env LC_ALL=C sort\n"), EdgeType.EXECUTES, EntityType.COMMAND)
    assert commands == {"sort"}


def test_per_command_assignments_export_into_the_child_environment() -> None:
    """`env FOO=1 cmd` and `FOO=1 cmd` both place FOO in the child's environment,
    which is the same relationship `export FOO=1` creates."""
    for source in ("env LOG_LEVEL=debug ./notify.sh\n", "LOG_LEVEL=debug ./notify.sh\n"):
        assert "LOG_LEVEL" in targets(parse(source), EdgeType.EXPORTS), source


# -- wrappers that must NOT be guessed through -------------------------------


@pytest.mark.parametrize(
    "source",
    [
        'env "$WRAPPED"\n',
        "env $WRAPPED\n",
        'env LOG_LEVEL=debug "$SCRIPT"\n',
        'exec "$TARGET"\n',
        'nohup "${BACKGROUND}"\n',
    ],
)
def test_a_computed_wrapped_target_resolves_to_nothing(source: str) -> None:
    parsed = parse(source)
    assert targets(parsed, EdgeType.EXECUTES, EntityType.SCRIPT) == set()


@pytest.mark.parametrize(
    "source",
    [
        "env -u PATH ./notify.sh\n",  # -u consumes the next word
        "env -C /tmp ./notify.sh\n",  # -C consumes the next word
        "env -S './notify.sh a b'\n",  # -S re-splits its argument
        "command -v ./notify.sh\n",  # -v prints a path, it does not run it
        "command -V ./notify.sh\n",
    ],
)
def test_a_flag_that_changes_the_meaning_stops_the_unwrap(source: str) -> None:
    """Guessing which word is the command after an unrecognized flag would invent a
    relationship. The wrapper itself is reported instead, which is true."""
    parsed = parse(source)
    assert targets(parsed, EdgeType.EXECUTES, EntityType.SCRIPT) == set()
    assert targets(parsed, EdgeType.EXECUTES, EntityType.COMMAND)


def test_a_wrapper_with_no_command_at_all_resolves_to_nothing() -> None:
    """Bare `env` prints the environment; it runs nothing."""
    parsed = parse("env\n")
    assert targets(parsed, EdgeType.EXECUTES, EntityType.SCRIPT) == set()
    assert targets(parsed, EdgeType.EXECUTES, EntityType.COMMAND) == {"env"}


def test_sudo_is_deliberately_not_unwrapped() -> None:
    """`sudo` takes flags that consume arguments (`-u user`, `-g group`) and a `--`
    separator, so identifying the command is genuinely ambiguous. It is left alone
    rather than guessed."""
    parsed = parse("sudo -u deploy ./notify.sh\n")
    assert targets(parsed, EdgeType.EXECUTES, EntityType.COMMAND) == {"sudo"}
    assert targets(parsed, EdgeType.EXECUTES, EntityType.SCRIPT) == set()
