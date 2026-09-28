"""Dockerfile computed-target integrity (V1_SPEC §2, §7.8).

The invariant these tests exist to pin:

    RepoAtlas may emit a deterministic Dockerfile target only when the Dockerfile
    source statically names that target **under that instruction's semantics**.

``RUN $TOOL`` does not name a program called ``$TOOL``. It names a place a
program name will come from, and the honest record of that is nothing — not a
``cmd:$TOOL`` entity, not a lower confidence on one, and not an
``unresolved_refs`` row carrying the expression. All three are the same invented
node with a different label on it.

**The hard part is that ``$`` does not mean the same thing everywhere**, so a
blanket "contains ``$`` ⇒ dynamic" rule would be its own false-negative bug.
Docker's own reference splits the instructions in two:

* ``ADD``, ``COPY``, ``ENV``, ``EXPOSE``, ``FROM``, ``LABEL``, ``STOPSIGNAL``,
  ``USER``, ``VOLUME``, ``WORKDIR`` and ``ONBUILD`` are expanded **by the
  builder**, in shell form *and* in JSON form;
* ``RUN``, ``CMD`` and ``ENTRYPOINT`` support variables too, but "substitution is
  handled by the command shell rather than the builder" — and the exec form
  never invokes a shell, so **nothing expands there at all**.

Both halves were verified against Docker 29.7.2 rather than assumed; the
experiments are recorded in ARCHITECTURE.md. The consequence is the case this
suite cares about most: ``RUN ["$TOOL", "app.py"]`` really does try to execute a
program whose name is the four characters ``$TOOL``, so recording it is truthful,
while ``RUN $TOOL`` is computed and recording it is not.

Every case below is labelled with what it must resolve to:

``STATIC``
    The instruction names the target. A deterministic fact is required.
``COMPUTED``
    Build or shell evaluation decides the target. No entity, no edge and no
    standing claim may name it — the absence *is* the assertion.

Each test asserts both halves: what must exist, and what must not.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.config.settings import Settings
from repoatlas.models.parsing import USES_EXPORT_TABLE, EdgeType, EntityType, ParsedFile
from repoatlas.parsers.dockerfile import DockerfileParser
from repoatlas.parsers.dockerwords import Proof, split_words
from repoatlas.services.init_service import initialize
from repoatlas.services.retrieval_service import locate
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace
from tests.conftest import fact_snapshot, git, init_git_repository, reindex_from_scratch

pytestmark = pytest.mark.adversarial


# -- helpers -----------------------------------------------------------------


def parse(source: str, name: str = "Dockerfile") -> ParsedFile:
    return DockerfileParser().parse(Path(name), source)


def targets(parsed: ParsedFile, edge: EdgeType, kind: EntityType | None = None) -> set[str]:
    return {
        reference.target_key
        for reference in parsed.references
        if reference.edge_type is edge and (kind is None or reference.target_kind is kind)
    }


def executed(source: str) -> set[str]:
    """Every ``EXECUTES`` target key the parser records."""
    return targets(parse(source), EdgeType.EXECUTES)


def images(source: str) -> set[str]:
    """Every base-image ``DEPENDS_ON`` target key."""
    return {
        reference.target_key
        for reference in parse(source).references
        if reference.edge_type is EdgeType.DEPENDS_ON
        and reference.meta.get("ecosystem") == "docker"
    }


def files(source: str) -> set[str]:
    """Every ``REFERENCES`` target key recorded as a repository file."""
    return targets(parse(source), EdgeType.REFERENCES, EntityType.FILE)


def stage_refs(source: str) -> set[str]:
    """Every ``REFERENCES`` target key recorded as a build stage."""
    return {
        reference.target_key
        for reference in parse(source).references
        if reference.meta.get("role") == "stage"
    }


def all_keys(source: str) -> set[str]:
    """Every target key of every kind, for "nothing anywhere names this" checks."""
    return {reference.target_key for reference in parse(source).references}


# -- §1, §6 shell-form RUN: the review's first fabrication --------------------

#: ``RUN`` bodies whose executable the *shell* decides at run time. Not one of
#: these names a program; each names a place a program name will come from.
COMPUTED_RUN = (
    "RUN $TOOL\n",
    "RUN $TOOL --version\n",
    'RUN "$TOOL"\n',
    'RUN "$TOOL" --version\n',
    "RUN ${TOOL}\n",
    'RUN "${TOOL}" --flag\n',
    "RUN ${TOOL:-default}\n",
    "RUN ${TOOL-default}\n",
    "RUN ${TOOL:+alt}\n",
    "RUN ${TOOL#prefix}\n",
    "RUN ${TOOL%postfix}\n",
    "RUN $BASE/$NAME\n",
    "RUN ${BASE}/bin/tool\n",
    "RUN prefix-$TOOL\n",
    "RUN $TOOL-suffix\n",
    "RUN ${BASE}suffix\n",
    "RUN $(find_tool)\n",
    "RUN $(find_tool) --version\n",
    "RUN `find_tool`\n",
    "RUN `find_tool` --version\n",
    "RUN $((1 + 1))\n",
    'RUN "$TOOL" \\\n    --version\n',
    "RUN $TOOL \\\n    --version\n",
)


@pytest.mark.parametrize("source", COMPUTED_RUN)
def test_a_computed_run_target_produces_no_command_fact(source: str) -> None:
    """COMPUTED. The shell picks the program; the Dockerfile does not name it."""
    assert executed(source) == set()


@pytest.mark.parametrize("source", COMPUTED_RUN)
def test_a_computed_run_target_is_not_recorded_under_any_edge(source: str) -> None:
    """COMPUTED. Not as a package, not as a file, not under any other kind either."""
    assert all_keys(source) == set()


#: ``RUN`` bodies whose executable is spelled out in the source.
STATIC_RUN = (
    ("RUN ./build.sh\n", "build.sh"),
    ("RUN python app.py\n", "python"),
    ("RUN /usr/bin/python3 app.py\n", "python3"),
    ("RUN './deploy.sh'\n", "deploy.sh"),
    ('RUN "./deploy.sh"\n', "deploy.sh"),
    ("RUN make build\n", "make"),
    ("RUN python \\\n    app.py\n", "python"),
)


@pytest.mark.parametrize(("source", "expected"), STATIC_RUN)
def test_a_static_run_target_is_recorded(source: str, expected: str) -> None:
    """STATIC. The source contains the command, so the fact is required."""
    assert executed(source) == {expected}


def test_a_static_command_keeps_its_fact_when_only_its_argument_is_computed() -> None:
    """STATIC command, COMPUTED argument. ``echo`` is still named by the source."""
    assert executed('RUN echo "$TOOL"\n') == {"echo"}
    assert executed("RUN echo $TOOL\n") == {"echo"}
    assert executed("RUN python app.py --config $CONFIG\n") == {"python"}


def test_quoting_is_removed_from_a_static_target_rather_than_kept_in_the_key() -> None:
    """STATIC. Quotes are shell syntax and were never part of the name."""
    assert "'./deploy.sh'" not in executed("RUN './deploy.sh'\n")
    assert '"./deploy.sh"' not in executed('RUN "./deploy.sh"\n')


def test_a_single_quoted_dollar_names_a_program_whose_name_contains_a_dollar() -> None:
    """STATIC. Single quotes suspend expansion, so the shell execs ``$TOOL``.

    This is the direction a blanket ``$``-rejection rule gets wrong. The rule is
    *proof*, not spelling: here the source really does name the target.
    """
    assert executed("RUN '$TOOL' --version\n") == {"$TOOL"}


def test_an_escaped_dollar_names_a_program_whose_name_contains_a_dollar() -> None:
    """STATIC. ``\\$`` is the documented literal-dollar escape."""
    assert executed("RUN \\$TOOL --version\n") == {"$TOOL"}


def test_every_segment_of_a_chained_run_is_classified_on_its_own() -> None:
    """One computed segment does not suppress its literal neighbours."""
    assert executed("RUN apt-get update && $TOOL install && pip install x\n") == {
        "apt-get",
        "pip",
    }


def test_a_computed_command_between_literals_contributes_nothing() -> None:
    assert executed("RUN ./a.sh | $FILTER | ./b.sh\n") == {"a.sh", "b.sh"}


# -- §7, §19 exec / JSON form ------------------------------------------------


def test_a_static_exec_form_run_records_its_command() -> None:
    """STATIC. Unchanged behaviour, and the reason exec form is not simply muted."""
    assert executed('RUN ["python3", "app.py"]\n') == {"python3"}


def test_exec_form_performs_no_substitution_so_a_dollar_is_part_of_the_name() -> None:
    """STATIC, and deliberately so.

    Docker's exec form does not invoke a shell, and the builder does not expand
    ``RUN``/``CMD``/``ENTRYPOINT``. Verified against Docker 29.7.2: this exact
    instruction fails at run time with ``exec: "$TOOL": executable file not
    found``, which is the container trying to run a program *named* ``$TOOL``.
    Suppressing it would be a cosmetic fix that discards a true fact.
    """
    assert executed('RUN ["$TOOL", "app.py"]\n') == {"$TOOL"}
    assert executed('ENTRYPOINT ["$RUNNER", "app.py"]\n') == {"$RUNNER"}


def test_exec_form_claims_only_the_first_element() -> None:
    """No shell means no operators: ``&&`` in an argv is an argument."""
    assert executed('RUN ["./a.sh", "&&", "./b.sh"]\n') == {"a.sh"}


def test_exec_form_with_only_flags_names_no_command() -> None:
    """``CMD ["--serve"]`` supplies arguments to an ENTRYPOINT; it runs nothing."""
    assert executed('CMD ["--serve"]\n') == set()


def test_shell_quote_stripping_is_not_reused_inside_a_json_array() -> None:
    """A quote inside a JSON string is data, not syntax."""
    assert executed('RUN ["say\'hi", "x"]\n') == {"say'hi"}


def test_invalid_json_falls_back_to_shell_form_as_docker_does() -> None:
    """Docker treats an unparseable array as shell form; so must the parser.

    Single quotes are not JSON, so this line reaches ``/bin/sh`` intact and the
    shell tries to run a command word of ``[python3,``. Reading it that way is
    ugly and correct; reading it as an exec form — which the hand-rolled array
    splitter did — invents an execution of ``python3`` that never happens.
    """
    assert "python3" not in executed("RUN ['python3', 'app.py']\n")
    assert executed("RUN ['python3', 'app.py']\n") == {"[python3,"}


def test_a_json_array_that_is_not_a_string_vector_is_not_an_exec_form() -> None:
    assert "1" not in executed("RUN [1, 2]\n")


# -- §8 CMD and ENTRYPOINT ---------------------------------------------------


def test_a_static_entrypoint_and_cmd_are_recorded_in_both_forms() -> None:
    """STATIC."""
    assert executed('ENTRYPOINT ["python3", "app.py"]\n') == {"python3"}
    assert executed("ENTRYPOINT python3 app.py\n") == {"python3"}
    assert executed("CMD ./serve.sh\n") == {"serve.sh"}


def test_a_computed_entrypoint_or_cmd_names_nothing() -> None:
    """COMPUTED. The instruction guarantees *an* execution, not *what* runs."""
    assert executed("ENTRYPOINT $RUNNER app.py\n") == set()
    assert executed("CMD $RUNNER app.py\n") == set()
    assert executed("ENTRYPOINT ${RUNNER} app.py\n") == set()


def test_a_static_cmd_with_a_computed_argument_keeps_its_command() -> None:
    """STATIC command, COMPUTED argument."""
    assert executed('CMD echo "$MESSAGE"\n') == {"echo"}


# -- §9 HEALTHCHECK ----------------------------------------------------------


def test_healthcheck_never_produces_a_command_target() -> None:
    """Not modelled at all today — a limitation, and it must stay a safe one.

    The point of pinning it is the second case: whatever HEALTHCHECK support may
    later be added, ``$PROBE`` must not arrive with it. Option operands such as
    ``--interval=30s`` must not be mistaken for command targets either.
    """
    assert all_keys("HEALTHCHECK CMD curl http://localhost/health\n") == set()
    assert all_keys("HEALTHCHECK CMD $PROBE\n") == set()
    assert all_keys("HEALTHCHECK --interval=30s --timeout=3s CMD curl -f http://x/\n") == set()
    assert all_keys('HEALTHCHECK CMD ["curl", "-f", "http://x/"]\n') == set()
    assert all_keys("HEALTHCHECK NONE\n") == set()


# -- §10, §11, §12 FROM ------------------------------------------------------


def test_a_static_from_names_its_image() -> None:
    """STATIC."""
    assert images("FROM python:3.13\n") == {"python:3.13"}
    assert images("FROM registry/org/image:tag\n") == {"registry/org/image:tag"}
    assert images("FROM --platform=linux/amd64 python:3.13\n") == {"python:3.13"}


#: ``FROM`` lines whose image the *builder* resolves from ARG values.
COMPUTED_FROM = (
    "ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\n",
    "ARG BASE_IMAGE\nFROM $BASE_IMAGE\n",
    "ARG TAG\nFROM image:${TAG}\n",
    "ARG TAG\nFROM image:$TAG\n",
    "FROM ${REGISTRY}/org/image:${TAG}\n",
    "FROM ${BASE_IMAGE:-python:3.13}\n",
    "FROM --platform=$TARGETPLATFORM $BASE\n",
)


@pytest.mark.parametrize("source", COMPUTED_FROM)
def test_a_computed_from_names_no_image(source: str) -> None:
    """COMPUTED. No package entity, and no standing claim carrying the expression."""
    assert images(source) == set()
    assert not any("$" in key for key in all_keys(source))


def test_a_computed_from_still_starts_a_stage() -> None:
    """The stage is a definite fact; only the image identity is unknown."""
    parsed = parse("ARG BASE_IMAGE\nFROM ${BASE_IMAGE} AS builder\n")
    builder = next(s for s in parsed.symbols if s.name == "builder")
    assert builder.meta["stage"] == "1"
    assert "image" not in builder.meta


def test_a_static_from_records_its_image_on_the_stage_symbol() -> None:
    parsed = parse("FROM python:3.13 AS builder\n")
    builder = next(s for s in parsed.symbols if s.name == "builder")
    assert builder.meta["image"] == "python:3.13"


def test_no_constant_propagation_is_performed() -> None:
    """An ARG default is not a proven image (§11): V1 says unknown, deliberately."""
    source = "ARG BASE=python:3.13\nFROM $BASE\n"
    assert images(source) == set()
    assert "python:3.13" not in all_keys(source)


def test_a_static_stage_base_stays_a_stage_reference() -> None:
    """STATIC. ``FROM builder`` continues a stage; it is not an image pull."""
    source = "FROM python:3.13 AS builder\nFROM builder\n"
    assert stage_refs(source) == {"builder"}
    assert images(source) == {"python:3.13"}


def test_a_computed_stage_base_names_no_stage_and_no_image() -> None:
    """COMPUTED."""
    source = "FROM python:3.13 AS builder\nARG STAGE\nFROM ${STAGE}\n"
    assert stage_refs(source) == set()
    assert images(source) == {"python:3.13"}
    assert not any("$" in key or "{" in key for key in all_keys(source))


def test_a_computed_stage_alias_does_not_become_a_stage_named_by_the_expression() -> None:
    parsed = parse("FROM python:3.13 AS $ALIAS\n")
    assert not any("$" in symbol.name for symbol in parsed.symbols)


# -- §13, §15 COPY and ADD ---------------------------------------------------


def test_a_static_copy_source_is_recorded() -> None:
    """STATIC."""
    assert files("COPY src/app.py /app/app.py\n") == {"src/app.py"}
    assert files("ADD assets/logo.png /srv/logo.png\n") == {"assets/logo.png"}
    assert files("COPY a.py b.py /app/\n") == {"a.py", "b.py"}


def test_a_static_copy_source_survives_json_form_with_spaces() -> None:
    """STATIC. The JSON form exists precisely for paths containing whitespace."""
    assert files('COPY ["src/file with space.txt", "/app/"]\n') == {"src/file with space.txt"}


def test_a_static_copy_source_survives_shell_quoting_and_escaping() -> None:
    assert files('COPY "src/file with space.txt" /app/\n') == {"src/file with space.txt"}
    assert files("COPY src/file\\ with\\ space.txt /app/\n") == {"src/file with space.txt"}


#: ``COPY``/``ADD`` sources the *builder* resolves before the copy happens.
COMPUTED_COPY = (
    "COPY $SRC_DIR /app\n",
    "COPY ${SRC_DIR} /app\n",
    "COPY ${SRC_DIR}/file /app\n",
    "COPY ${BASE}/config.yaml /app/\n",
    "COPY $BASE/$NAME /app\n",
    "COPY ${SRC:-src} /app\n",
    'COPY ["$SRC_DIR", "/app/"]\n',
    "ADD $ASSETS /srv/\n",
    'ADD ["${ASSETS}", "/srv/"]\n',
)


@pytest.mark.parametrize("source", COMPUTED_COPY)
def test_a_computed_copy_source_produces_no_file_fact(source: str) -> None:
    """COMPUTED, in shell form *and* JSON form — the builder expands both."""
    assert files(source) == set()
    assert all_keys(source) == set()


def test_no_partial_prefix_is_salvaged_from_a_computed_source() -> None:
    """``${BASE}/config.yaml`` is not a claim about a file called ``config.yaml``."""
    assert all_keys("COPY ${BASE}/config.yaml /app/\n") == set()
    assert "config.yaml" not in all_keys("COPY ${BASE}/config.yaml /app/\n")


def test_an_escaped_dollar_copy_source_names_a_dollar_bearing_file() -> None:
    """STATIC. Verified against Docker 29.7.2: this copies the file named ``$FOO``."""
    assert files("COPY \\$FOO /quux\n") == {"$FOO"}
    assert files('COPY ["\\\\$FOO", "/quux"]\n') == {"$FOO"}


def test_a_single_quoted_copy_source_suspends_builder_expansion() -> None:
    """STATIC. Also verified against a real build: it copies the file ``$FOO``.

    Single quotes suspend expansion for the builder exactly as they do for the
    shell, which is the second case a spelling-based rule would get backwards.
    """
    assert files("COPY '$FOO' /quux\n") == {"$FOO"}


def test_a_computed_destination_does_not_suppress_a_literal_source() -> None:
    """The destination produces no fact today; the source still must."""
    assert files("COPY app.py $APP_HOME/\n") == {"app.py"}


def test_a_computed_destination_produces_no_fact_of_its_own() -> None:
    assert "$APP_HOME/" not in all_keys("COPY app.py $APP_HOME/\n")


def test_one_computed_source_does_not_suppress_its_literal_siblings() -> None:
    assert files("COPY a.py $SRC b.py /app/\n") == {"a.py", "b.py"}


# -- §14 COPY --from ---------------------------------------------------------


def test_a_static_copy_from_names_no_stage_bearing_expression() -> None:
    """STATIC stage. The stage is definite; it is simply not modelled as a target."""
    assert "builder" not in stage_refs("COPY --from=builder /out/app /app/app\n")


def test_a_computed_copy_from_creates_no_stage_or_image_target() -> None:
    """COMPUTED."""
    source = "COPY --from=$STAGE /out/app /app/app\n"
    assert not any("$" in key for key in all_keys(source))
    assert "$STAGE" not in all_keys(source)


def test_a_computed_source_under_copy_from_is_still_suppressed() -> None:
    assert files("COPY --from=builder $OUT /app/app\n") == set()


def test_a_copy_from_source_is_no_longer_recorded_as_a_repository_file() -> None:
    """The behaviour this pass pinned, inverted once it was acted on.

    ``/out/app`` lives in the *builder stage*, not in the build context, so
    calling it a repository file reference was wrong. Pass 6 recorded the
    behaviour rather than changing it, because it is wrong for a
    static-modelling reason that has nothing to do with computed targets. The
    namespace is now part of the proof, so it is gone — for the literal stage and
    the computed one alike, which is the same answer for a stronger reason.
    """
    assert files("COPY --from=builder /out/app /app/app\n") == set()
    assert files("COPY --from=$STAGE /out/app /app/app\n") == set()


def test_a_remote_add_source_is_still_recorded_as_a_repository_file() -> None:
    """Pinned, not endorsed — the same separate finding, for remote ``ADD``.

    A URL is statically named, so it is not a fabricated *identity*; it is a
    correctly-read value filed under the wrong kind. Reported separately.
    """
    assert files("ADD https://example.com/x.tar.gz /srv/\n") == {"https://example.com/x.tar.gz"}


# -- §16 ARG and ENV evidence is not collateral damage -----------------------


def test_suppressing_a_fake_target_does_not_delete_the_variable_evidence() -> None:
    """The Dockerfile still proves these variables exist and are declared here."""
    source = "ARG BASE_IMAGE\nARG SRC_DIR\nARG TOOL\nFROM ${BASE_IMAGE}\nRUN $TOOL\n"
    assert targets(parse(source), EdgeType.EXPORTS, EntityType.ENV_VAR) == {
        "BASE_IMAGE",
        "SRC_DIR",
        "TOOL",
    }


def test_an_env_assignment_whose_value_is_computed_still_names_the_variable() -> None:
    """The *name* is static syntax; only the value is computed."""
    assert targets(parse("ENV HOST=$OTHER\n"), EdgeType.EXPORTS, EntityType.ENV_VAR) == {"HOST"}


def test_a_computed_variable_name_is_not_interned_as_a_variable() -> None:
    assert targets(parse("ENV $NAME=x\n"), EdgeType.EXPORTS, EntityType.ENV_VAR) == set()


# -- §18, §21 substitution and heredocs --------------------------------------


def test_a_command_substitution_yields_no_nested_target() -> None:
    """COMPUTED. The inner command is not modelled in this pass either."""
    assert all_keys("RUN $(find_tool) --version\n") == set()
    assert all_keys("RUN `find_tool` --version\n") == set()


def test_a_heredoc_run_produces_no_target_at_all() -> None:
    """The body is shell source this parser does not read. Fail closed."""
    assert all_keys("RUN <<EOF\necho hello\nEOF\n") == set()
    assert all_keys("RUN <<-EOF\n./build.sh\nEOF\n") == set()
    assert all_keys('RUN <<"EOT"\necho "$TOOL"\nEOT\n') == set()


def test_a_heredoc_redirect_does_not_become_a_command_named_by_the_operator() -> None:
    """The pre-fix parser recorded ``cmd:<<EOF``, which names nothing at all."""
    assert not any(key.startswith("<") for key in all_keys("RUN <<EOF\necho hi\nEOF\n"))


def test_a_plain_redirect_does_not_disturb_a_literal_command() -> None:
    assert executed("RUN echo hi > /out.txt\n") == {"echo"}


# -- §17 expansion forms, exhaustively ---------------------------------------

#: Every expansion form the parser must treat as computed in command position.
EXPANSION_FORMS = (
    "$VAR",
    "${VAR}",
    "${VAR:-default}",
    "${VAR-default}",
    "${VAR:+alt}",
    "${VAR+alt}",
    "${VAR#prefix}",
    "${VAR%postfix}",
    "${VAR##prefix}",
    "${VAR%%postfix}",
    "$BASE/$NAME",
    "prefix-$VAR",
    "${BASE}suffix",
)


@pytest.mark.parametrize("form", EXPANSION_FORMS)
def test_no_expansion_form_becomes_a_command_key(form: str) -> None:
    assert executed(f"RUN {form}\n") == set()


@pytest.mark.parametrize("form", EXPANSION_FORMS)
def test_no_expansion_form_becomes_a_copy_source_key(form: str) -> None:
    assert files(f"COPY {form} /app/\n") == set()


@pytest.mark.parametrize("form", EXPANSION_FORMS)
def test_no_expansion_form_becomes_an_image_key(form: str) -> None:
    assert images(f"FROM {form}\n") == set()


# -- §20 line continuations --------------------------------------------------


def test_a_continuation_does_not_hide_dynamic_syntax() -> None:
    assert executed('RUN "$TOOL" \\\n    --version\n') == set()
    assert files("COPY \\\n    $SRC \\\n    /app/\n") == set()


def test_a_continuation_keeps_the_true_line_span_of_a_static_target() -> None:
    parsed = parse("# comment\nRUN python \\\n    app.py\n")
    reference = next(r for r in parsed.references if r.target_key == "python")
    assert (reference.line_start, reference.line_end) == (2, 3)


def test_a_suppressed_target_does_not_shift_the_provenance_of_a_later_one() -> None:
    parsed = parse("RUN $TOOL \\\n    --version\nRUN ./build.sh\n")
    reference = next(r for r in parsed.references if r.target_key == "build.sh")
    assert (reference.line_start, reference.line_end) == (3, 3)


# -- §22, §23 ONBUILD and SHELL ----------------------------------------------


def test_onbuild_is_not_modelled_and_therefore_fabricates_nothing() -> None:
    assert all_keys("ONBUILD RUN ./build.sh\n") == set()
    assert all_keys("ONBUILD RUN $TOOL\n") == set()
    assert all_keys("ONBUILD COPY $SRC /app/\n") == set()


def test_shell_is_not_modelled_and_therefore_fabricates_nothing() -> None:
    assert all_keys('SHELL ["/bin/bash", "-c"]\n') == set()
    assert all_keys("SHELL $MYSHELL\n") == set()


# -- installer packages ------------------------------------------------------


def test_a_computed_package_name_is_not_installed_by_anything() -> None:
    """COMPUTED. ``requests`` survives because it is independently literal (§1)."""
    parsed = parse("RUN pip install $PKG requests\n")
    installed = targets(parsed, EdgeType.DEPENDS_ON, EntityType.PACKAGE)
    assert installed == {"requests"}


def test_a_computed_installer_claims_neither_a_command_nor_a_package() -> None:
    assert all_keys("RUN $PIP install requests\n") == set()


def test_a_computed_install_verb_claims_no_package() -> None:
    assert targets(parse("RUN pip $VERB requests\n"), EdgeType.DEPENDS_ON) == set()
    assert executed("RUN pip $VERB requests\n") == {"pip"}


# -- §24 confidence is not a way out -----------------------------------------


def test_no_computed_target_survives_at_a_reduced_confidence() -> None:
    """A half-confident ``cmd:$TOOL`` is the same invented node with a smaller number."""
    source = (
        "ARG BASE_IMAGE\nARG SRC_DIR\nARG TOOL\n"
        "FROM ${BASE_IMAGE}\nCOPY $SRC_DIR /app\nRUN $TOOL --version\n"
    )
    for reference in parse(source).references:
        assert "$" not in reference.target_key
        assert "{" not in reference.target_key


# -- the review's own reproduction, end to end -------------------------------

REVIEW_DOCKERFILE = """\
ARG BASE_IMAGE
ARG SRC_DIR
ARG TOOL

FROM ${BASE_IMAGE}

COPY $SRC_DIR /app

RUN $TOOL --version
"""

#: The exact keys the fresh review found in a real index. All must be absent.
FABRICATED_KEYS = ("cmd:$TOOL", "package:${BASE_IMAGE}", "file:$SRC_DIR")

#: The raw expressions, in every spelling a target key could carry them.
FABRICATED_EXPRESSIONS = ("$TOOL", "${BASE_IMAGE}", "$SRC_DIR", "${TOOL}", "$BASE_IMAGE")


def test_the_review_dockerfile_asserts_no_computed_target_at_the_parser() -> None:
    assert all_keys(REVIEW_DOCKERFILE) == {"BASE_IMAGE", "SRC_DIR", "TOOL"}


# -- §26, §27, §28, §29, §30 through the real indexer ------------------------

STATIC_DOCKERFILE = """\
FROM python:3.13 AS builder

COPY src/app.py /app/app.py
RUN ./build.sh
RUN pip install requests

FROM builder
COPY --from=builder /out/app /app/app
ENTRYPOINT ["python3", "/app/app.py"]
"""

BUILD_SCRIPT = "#!/bin/sh\necho building\n"
APP_SOURCE = "def main() -> None:\n    print('DOCKER_TARGET_APP_MARKER')\n"


@pytest.fixture
def docker_root(tmp_path: Path) -> Path:
    """A managed root holding nothing but the Dockerfile repository.

    Deliberately not the shared fixture root: the sweeps below assert that
    *nothing anywhere* names an expression, and nine unrelated repositories would
    turn that into a claim about them instead.
    """
    root = tmp_path / "docker-root"
    root.mkdir()
    return root


@pytest.fixture
def docker_repo(docker_root: Path) -> Path:
    """One repository with a static Dockerfile and the files it names."""
    repo = docker_root / "docker-repo"
    (repo / "src").mkdir(parents=True)
    (repo / "Dockerfile").write_text(STATIC_DOCKERFILE)
    (repo / "build.sh").write_text(BUILD_SCRIPT)
    (repo / "src" / "app.py").write_text(APP_SOURCE)
    init_git_repository(repo)
    return repo


@pytest.fixture
def indexed(workspace: Workspace, docker_root: Path, docker_repo: Path) -> Iterator[Workspace]:
    index(workspace, docker_root)
    yield workspace


def index(workspace: Workspace, root: Path) -> None:
    """Discover and index ``root``, writing the settings file beside it.

    ``config_path`` is not optional in a test. ``initialize`` persists settings,
    and omitting it writes a real config into the developer's XDG directory
    pointing at a temporary path that stops existing when the test ends.
    """
    initialize(workspace, root, config_path=root.parent / "repoatlas-config.yaml")


def resync(workspace: Workspace, root: Path) -> None:
    """A plain incremental sync over the whole managed set."""
    sync(workspace, workspace.resolver().resolve(root))


def rows(workspace: Workspace, query: str) -> list[sqlite3.Row]:
    return list(workspace.database.execute(query).fetchall())


def entity_keys(workspace: Workspace) -> set[str]:
    return {row[0] for row in rows(workspace, "SELECT key FROM entities")}


def claim_keys(workspace: Workspace) -> set[str]:
    return {row[0] for row in rows(workspace, "SELECT target_key FROM unresolved_refs")}


def edge_pairs(workspace: Workspace) -> set[tuple[str, str, str]]:
    return {
        (row[0], row[1], row[2])
        for row in rows(
            workspace,
            "SELECT e.type, src.key, dst.key FROM edges e "
            "JOIN entities src ON src.id = e.src_id "
            "JOIN entities dst ON dst.id = e.dst_id",
        )
    }


def rewrite(repo: Path, source: str) -> None:
    (repo / "Dockerfile").write_text(source)
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "rewrite")


def test_a_static_dockerfile_still_produces_its_facts(indexed: Workspace) -> None:
    """STATIC. The whole point of not fixing this with a blanket rule."""
    keys = entity_keys(indexed)
    assert "cmd:pip" in keys
    assert "cmd:python3" in keys
    assert any(key.startswith("pkg:") or "python:3.13" in key for key in keys | claim_keys(indexed))
    assert "python:3.13" in claim_keys(indexed) | keys


#: The stage a fact belongs to is its source entity, so every Dockerfile edge
#: below hangs off a stage symbol rather than off the file.
BUILDER = "sym:docker-repo:Dockerfile:builder:1"
APP_FILE = "file:docker-repo:src/app.py"


def test_the_static_dockerfile_links_to_the_files_it_names(indexed: Workspace) -> None:
    pairs = edge_pairs(indexed)
    assert ("REFERENCES", BUILDER, APP_FILE) in pairs
    assert ("EXECUTES", BUILDER, "cmd:build.sh") in pairs
    assert ("EXECUTES", BUILDER, "cmd:pip") in pairs


def test_the_review_dockerfile_leaves_no_fabricated_row_anywhere(
    workspace: Workspace, docker_root: Path, docker_repo: Path
) -> None:
    """§26. The database sweep, against the review's own three keys."""
    rewrite(docker_repo, REVIEW_DOCKERFILE)
    index(workspace, docker_root)

    keys = entity_keys(workspace)
    claims = claim_keys(workspace)
    for fabricated in FABRICATED_KEYS:
        assert fabricated not in keys
    for expression in FABRICATED_EXPRESSIONS:
        assert expression not in claims
        assert not any(expression in key for key in keys)
    # Not a blanket ban on `$`: the assertion is that no *Dockerfile* target key
    # carries an expression the builder or the shell would have evaluated. The one
    # other claim is `src/app.py`'s export table, a fact about the Python module's own
    # bindings that names no target at all.
    assert claims - {USES_EXPORT_TABLE} == set()


def test_the_variables_themselves_remain_indexed(
    workspace: Workspace, docker_root: Path, docker_repo: Path
) -> None:
    """§16. The ARG declarations are real facts and survive the suppression."""
    rewrite(docker_repo, REVIEW_DOCKERFILE)
    index(workspace, docker_root)
    keys = entity_keys(workspace)
    assert {"env:BASE_IMAGE", "env:SRC_DIR", "env:TOOL"} <= keys


def test_retrieval_surfaces_no_computed_target_evidence(
    workspace: Workspace, docker_root: Path, docker_repo: Path
) -> None:
    """§27. Queries for the expressions find no fabricated Dockerfile entity.

    Retrieval may still reach the Dockerfile itself — the line ``RUN $TOOL`` is
    real text in a real file, and finding it is correct. What must not exist is a
    *graph node* named by the expression, because that is what makes an invented
    target citable as a deterministic fact.
    """
    rewrite(docker_repo, REVIEW_DOCKERFILE)
    index(workspace, docker_root)

    for expression in ("$TOOL", "${BASE_IMAGE}", "$SRC_DIR"):
        found = locate(workspace, expression, workspace.resolver().resolve(docker_repo))
        for candidate in found.candidates:
            assert candidate.path == "Dockerfile", candidate.path
        named = {row[0] for row in rows(workspace, "SELECT name FROM entities")}
        assert expression not in named


def test_retrieval_still_finds_static_dockerfile_facts(
    indexed: Workspace, docker_repo: Path
) -> None:
    """§27. The other half: literal targets must keep surfacing."""
    context = indexed.resolver().resolve(docker_repo)
    for query in ("python:3.13", "build.sh", "src/app.py"):
        found = locate(indexed, query, context)
        assert found.candidates, f"no evidence for {query!r}"


# -- §29 stale-fact cleanup, in both directions ------------------------------

TRANSITIONS = (
    ("RUN ./deploy.sh\n", "RUN $DEPLOY\n", "cmd:deploy.sh", "$DEPLOY"),
    ("FROM python:3.13\n", "ARG B\nFROM $B\n", None, "$B"),
    ("FROM scratch\nCOPY src/app.py /app/\n", "FROM scratch\nCOPY $SRC /app/\n", None, "$SRC"),
)


@pytest.mark.parametrize(("literal", "dynamic", "entity", "expression"), TRANSITIONS)
def test_a_literal_target_that_becomes_computed_is_removed(
    workspace: Workspace,
    docker_root: Path,
    docker_repo: Path,
    literal: str,
    dynamic: str,
    entity: str | None,
    expression: str,
) -> None:
    rewrite(docker_repo, literal)
    index(workspace, docker_root)
    if entity is not None:
        assert entity in entity_keys(workspace)

    rewrite(docker_repo, dynamic)
    resync(workspace, docker_root)

    if entity is not None:
        assert entity not in entity_keys(workspace)
    assert expression not in entity_keys(workspace) | claim_keys(workspace)


@pytest.mark.parametrize(("literal", "dynamic", "entity", "expression"), TRANSITIONS)
def test_a_computed_target_that_becomes_literal_is_restored(
    workspace: Workspace,
    docker_root: Path,
    docker_repo: Path,
    literal: str,
    dynamic: str,
    entity: str | None,
    expression: str,
) -> None:
    rewrite(docker_repo, dynamic)
    index(workspace, docker_root)
    assert expression not in entity_keys(workspace) | claim_keys(workspace)

    rewrite(docker_repo, literal)
    resync(workspace, docker_root)
    if entity is not None:
        assert entity in entity_keys(workspace)


@pytest.mark.parametrize(("literal", "dynamic", "entity", "expression"), TRANSITIONS)
def test_a_target_transition_leaves_the_same_state_as_a_clean_reindex(
    workspace: Workspace,
    docker_root: Path,
    docker_repo: Path,
    settings: Settings,
    tmp_path: Path,
    literal: str,
    dynamic: str,
    entity: str | None,
    expression: str,
) -> None:
    """§30. Incremental equality, over the transition that changes the facts."""
    rewrite(docker_repo, literal)
    index(workspace, docker_root)
    rewrite(docker_repo, dynamic)
    resync(workspace, docker_root)
    incremental = fact_snapshot(workspace)

    clean = reindex_from_scratch(settings, docker_root, tmp_path)
    assert incremental == clean


# -- §28 graph and linker ----------------------------------------------------


def test_a_cross_repository_file_reference_survives_and_then_disappears(
    workspace: Workspace, docker_root: Path, docker_repo: Path
) -> None:
    """§28. A literal path links; the same path behind a variable does not."""
    rewrite(docker_repo, "FROM scratch\nCOPY src/app.py /app/app.py\n")
    index(workspace, docker_root)
    stage = "sym:docker-repo:Dockerfile:stage1:1"
    assert ("REFERENCES", stage, APP_FILE) in edge_pairs(workspace)

    rewrite(docker_repo, "FROM scratch\nCOPY $SRC /app/app.py\n")
    resync(workspace, docker_root)
    assert ("REFERENCES", stage, APP_FILE) not in edge_pairs(workspace)
    assert "$SRC" not in claim_keys(workspace)

    rewrite(docker_repo, "FROM scratch\nCOPY src/app.py /app/app.py\n")
    resync(workspace, docker_root)
    assert ("REFERENCES", stage, APP_FILE) in edge_pairs(workspace)


# -- §26 the comprehensive fixture -------------------------------------------

COMPREHENSIVE = """\
ARG BASE_IMAGE
ARG SRC_DIR
ARG TOOL
ARG STAGE

FROM python:3.13 AS builder
COPY src/app.py /app/app.py
COPY $SRC_DIR /app/dynamic
COPY ${SRC_DIR}/nested /app/nested
ADD assets.tar.gz /srv/
ADD $ASSETS /srv/dynamic
RUN ./build.sh
RUN $TOOL --version
RUN "$TOOL" --version
RUN echo "$TOOL"
RUN pip install requests $PKG
RUN $(discover) --version
RUN <<EOF
./hidden.sh
EOF

FROM ${BASE_IMAGE}
COPY --from=builder /out/app /app/app
COPY --from=$STAGE /out/other /app/other
CMD echo "$MESSAGE"
ENTRYPOINT $RUNNER app.py
HEALTHCHECK CMD $PROBE
ONBUILD RUN $TOOL
SHELL ["/bin/bash", "-c"]
"""


def test_the_comprehensive_fixture_leaves_no_expression_in_any_target(
    workspace: Workspace, docker_root: Path, docker_repo: Path
) -> None:
    """§26. One sweep over every instruction family at once."""
    rewrite(docker_repo, COMPREHENSIVE)
    index(workspace, docker_root)

    docker_targets = {
        row[0]
        for row in rows(
            workspace,
            "SELECT dst.key FROM edges e JOIN entities dst ON dst.id = e.dst_id "
            "JOIN files f ON f.id = e.file_id WHERE f.rel_path = 'Dockerfile'",
        )
    } | {
        row[0]
        for row in rows(
            workspace,
            "SELECT u.target_key FROM unresolved_refs u JOIN files f ON f.id = u.file_id "
            "WHERE f.rel_path = 'Dockerfile'",
        )
    }
    fabricated = {key for key in docker_targets if "$" in key or "{" in key or key.startswith("<")}
    assert fabricated == set()


def test_the_comprehensive_fixture_keeps_every_literal_target(
    workspace: Workspace, docker_root: Path, docker_repo: Path
) -> None:
    """The false-negative half: suppression must not take the true facts with it."""
    rewrite(docker_repo, COMPREHENSIVE)
    index(workspace, docker_root)
    keys = entity_keys(workspace) | claim_keys(workspace)
    for expected in ("cmd:build.sh", "cmd:echo", "cmd:pip", "python:3.13", "requests"):
        assert expected in keys, expected


# -- §32 the Bash hardening must not reopen ----------------------------------


def test_the_bash_parser_still_refuses_a_computed_command() -> None:
    """Shared helpers must not have loosened the neighbouring parser."""
    from repoatlas.parsers.bash import BashParser

    parsed = BashParser().parse(Path("x.sh"), '$CMD\n"$TOOL" --version\n./real.sh\n')
    keys = {r.target_key for r in parsed.references if r.edge_type is EdgeType.EXECUTES}
    assert keys == {"./real.sh"}


# -- the classifier's own fail-closed paths ----------------------------------


def test_a_backslash_inside_double_quotes_escapes_only_what_the_shell_escapes() -> None:
    """``\\$`` is an escape inside double quotes; ``\\q`` is two literal characters."""
    assert executed('RUN "a\\$TOOL"\n') == {"a$TOOL"}
    assert executed('RUN "a\\qb"\n') == {"a\\qb"}


def test_a_dollar_that_begins_no_name_is_treated_as_computed() -> None:
    """A limitation, stated rather than hidden.

    Docker reads a trailing ``$`` as a literal dollar, so ``RUN tool$`` really
    does name ``tool$``. The classifier fails closed on it instead: losing a
    target on a spelling almost nobody writes is the safe side of the trade, and
    the alternative is a rule that has to decide which lone dollars are real.
    """
    assert executed("RUN tool$\n") == set()
    assert files("COPY a$ /app/\n") == set()


def test_an_unterminated_expansion_stays_computed_to_the_end_of_the_word() -> None:
    """No closing brace means no way to know where the operand ended."""
    assert executed("RUN ${UNTERMINATED\n") == set()
    assert executed("RUN $(unterminated\n") == set()
    assert executed("RUN `unterminated\n") == set()


def test_an_expansion_containing_spaces_stays_one_operand() -> None:
    """Word boundaries must survive a value that does not.

    ``${VAR:-a b}`` is one computed source. A scan that stopped at the space
    would leave ``b}`` behind as a second, literal operand — and since the last
    operand is the destination, the *real* destination would be read as a source.
    """
    assert files("COPY ${SRC:-a b} /app/\n") == set()
    assert files("COPY ${SRC:-a b} x.py /app/\n") == {"x.py"}


def test_trailing_whitespace_produces_no_empty_operand() -> None:
    assert executed("RUN ./build.sh   \n") == {"build.sh"}
    assert files("COPY src/app.py /app/   \n") == {"src/app.py"}


def test_an_unbalanced_quote_does_not_raise() -> None:
    """The parser is fed untrusted repository content and must never abort a sync."""
    assert executed('RUN "./build.sh\n') == {"build.sh"}
    assert executed("RUN './build.sh\n") == {"build.sh"}


def test_malformed_instructions_are_survived_without_facts() -> None:
    """Repository content is untrusted, so every degenerate form must be inert."""
    for source in (
        "FROM\n",
        "FROM --platform=linux/amd64\n",
        "ENV\n",
        "ARG\n",
        "RUN []\n",
        "CMD []\n",
        "ENTRYPOINT []\n",
        "COPY\n",
        "RUN\n",
        "RUN    \n",
    ):
        assert all_keys(source) == set(), source


def test_an_installer_flag_before_the_verb_does_not_hide_the_packages() -> None:
    assert targets(parse("RUN pip -q install requests\n"), EdgeType.DEPENDS_ON) == {"requests"}


def test_a_heredoc_cmd_or_entrypoint_produces_no_target_either() -> None:
    assert all_keys("CMD <<EOF\n./run.sh\nEOF\n") == set()
    assert all_keys("ENTRYPOINT <<EOF\n$TOOL\nEOF\n") == set()


# -- the classifier, asked directly ------------------------------------------


def test_the_classifier_reports_proof_rather_than_a_value_it_cannot_prove() -> None:
    """§4. One place answers "does the source name this", and it answers in kind."""
    literal, computed = split_words("./build.sh $TOOL")
    assert (literal.proof, literal.literal, literal.raw) == (
        Proof.LITERAL,
        "./build.sh",
        "./build.sh",
    )
    assert (computed.proof, computed.literal, computed.raw) == (Proof.COMPUTED, None, "$TOOL")


def test_the_classifier_ignores_surrounding_whitespace() -> None:
    assert [word.literal for word in split_words("  a   b  ")] == ["a", "b"]
    assert split_words("   ") == []


def test_backticks_are_substitution_only_where_the_shell_reads_them() -> None:
    """A backtick is command substitution in ``RUN``; in ``COPY`` it is a filename."""
    assert split_words("`x`", substitution=True)[0].literal is None
    assert split_words("`x`")[0].literal == "`x`"
