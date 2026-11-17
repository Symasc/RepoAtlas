"""A YAML alias makes the composed document a graph, and these parsers read it as a tree: a few
lines can stand for billions of nodes, and a cycle for an endless one. No YAML, JSON or SLS file
may make the structured-config or Salt parser hang, run out of memory or raise -- and an alias
that is only reused (not a bomb) must still cost the file nothing it had before.

Public entry points only; the work counts are pinned in ``test_yaml_alias_work_bounds.py``.
"""

from __future__ import annotations

import itertools
import threading
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
import yaml
from repoatlas.models.parsing import PARSE_BOUND_EXCEEDED, EdgeType, ParsedFile, Severity
from repoatlas.parsers.ci_config import ci_references
from repoatlas.parsers.dashboards import dashboard_references
from repoatlas.parsers.deploy_helm import MAX_VALUE_DEPTH, read_values
from repoatlas.parsers.deploy_targets import values_target_references
from repoatlas.parsers.graphql_codegen import GRAPHQL_CODEGEN_OUTPUT, codegen_references
from repoatlas.parsers.http_facts import PENDING_FACT
from repoatlas.parsers.salt import SaltParser
from repoatlas.parsers.structured import StructuredParser

pytestmark = pytest.mark.adversarial

#: Seconds any one parse may take. Generous: the point is "returns", not "is fast".
LIMIT = 30


def _within(seconds: float, call: Callable[[], Any]) -> Any:
    """``call()``'s result, failing if it has not returned in ``seconds``. The call runs in a
    daemon thread, so one that never returns fails the test instead of hanging the run."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = call()
        except BaseException as exc:  # re-raised below, on the test's thread
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), "did not return: unbounded walk"
    if "error" in box:
        raise box["error"]
    return box["value"]


def _check_lines(parsed: ParsedFile, text: str) -> None:
    """Every line number a parse cites is a line the file has."""
    count = len(text.splitlines())
    for symbol in parsed.symbols:
        assert symbol.line_start is not None
        assert symbol.line_end is not None
        assert 1 <= symbol.line_start <= symbol.line_end <= count
    for reference in parsed.references:
        assert 1 <= reference.line_start <= reference.line_end <= count
    for diagnostic in parsed.diagnostics:
        if diagnostic.line is not None:
            assert 1 <= diagnostic.line <= count


def _parse(name: str, text: str) -> ParsedFile:
    parsed = _within(LIMIT, lambda: StructuredParser().parse(Path(name), text))
    _check_lines(parsed, text)
    return parsed


def _parse_salt(name: str, text: str) -> ParsedFile:
    parsed = _within(LIMIT, lambda: SaltParser().parse(Path(name), text))
    _check_lines(parsed, text)
    return parsed


def _spans(parsed: ParsedFile) -> list[tuple[str, int | None, int | None]]:
    return [(s.qualified_name, s.line_start, s.line_end) for s in parsed.symbols]


def _compose(text: str) -> list[yaml.Node]:
    return list(yaml.compose_all(text, Loader=yaml.SafeLoader))


def _messages(parsed: ParsedFile) -> list[str]:
    return [d.message for d in parsed.diagnostics]


def _alias_warning(parsed: ParsedFile) -> bool:
    return any(d.severity == Severity.WARNING and "alias" in d.message for d in parsed.diagnostics)


def _fan_out_lists(levels: int, width: int = 9) -> list[str]:
    """``l0`` a flat list, and each ``l{n}`` a list of ``width`` aliases of ``l{n-1}``."""
    lines = ["l0: &l0 [x, y, z]"]
    for n in range(1, levels + 1):
        lines.append(f"l{n}: &l{n} [{', '.join([f'*l{n - 1}'] * width)}]")
    return lines


# -- A1: a multiplying alias chain reached from a later key (RR1) ---------------------------


def test_a1_multiplying_aliases_keep_every_key_and_say_they_stopped() -> None:
    lines = [*_fan_out_lists(9), "config: {projects: [{name: a, big: *l9}]}"]
    text = "\n".join(lines) + "\n"
    parsed = _parse("settings.yaml", text)

    assert _spans(parsed) == [
        *((f"l{n}", n + 1, n + 1) for n in range(10)),
        ("config", 11, 11),
        ("config.projects", 11, 11),
        ("config.projects[0].name", 11, 11),
        ("config.projects[0].big", 11, 11),
    ]
    assert _alias_warning(parsed)
    assert not any("truncated at 400 keys" in m for m in _messages(parsed))


# -- A2: an alias that contains itself (RR2) ------------------------------------------------


def test_a2_a_list_holding_itself_is_one_key_with_no_diagnostic() -> None:
    parsed = _parse("loop.yaml", "a: &a [*a]\n")
    assert _spans(parsed) == [("a", 1, 1)]
    assert parsed.diagnostics == ()


def test_a2_a_block_list_holding_itself_spans_its_own_content() -> None:
    parsed = _parse("loop.yaml", "a: &a\n  - x\n  - *a\nb: 1\n")
    assert _spans(parsed) == [("a", 1, 2), ("b", 4, 4)]


# -- A3: the billion laughs -----------------------------------------------------------------


def test_a3_billion_laughs_returns_with_every_key_and_the_alias_warning() -> None:
    nine = ", ".join(['"lol"'] * 9)
    lines = [f"a: &a [{nine}]"]
    names = "abcdefghi"
    for previous, name in itertools.pairwise(names):
        lines.append(f"{name}: &{name} [{', '.join([f'*{previous}'] * 9)}]")
    lines.append("top: {laughs: *i}")
    parsed = _parse("laughs.yaml", "\n".join(lines) + "\n")

    assert _spans(parsed) == [
        *((name, n, n) for n, name in enumerate(names, start=1)),
        ("top", 10, 10),
        ("top.laughs", 10, 10),
    ]
    assert _alias_warning(parsed)


# -- A4: one big list aliased by many keys --------------------------------------------------


def test_a4_a_big_list_aliased_four_hundred_times_hits_the_key_cap() -> None:
    items = ", ".join(str(n) for n in range(50_000))
    lines = [f"big: &b [{items}]", *(f"k{n}: *b" for n in range(400))]
    parsed = _parse("many.yaml", "\n".join(lines) + "\n")

    assert _spans(parsed) == [
        ("big", 1, 1),
        *((f"k{n}", n + 2, n + 2) for n in range(399)),
    ]
    assert "document truncated at 400 keys" in _messages(parsed)


# -- A5: the same, through the Salt parser --------------------------------------------------


def test_a5_salt_pillar_data_aliased_by_many_states_stays_bounded() -> None:
    lines = ["big: &b"]
    lines += [f"  k{n}: 1" for n in range(500)]
    lines += [f"s{n}: *b" for n in range(2000)]
    parsed = _parse_salt("pillar.sls", "\n".join(lines) + "\n")
    assert len(parsed.symbols) < 100_000
    # The file's states are all still there: only what the aliases re-listed was cut.
    assert {f"s{n}" for n in range(2000)} <= {s.qualified_name for s in parsed.symbols}


# -- A6: dashboards -------------------------------------------------------------------------


def test_a6_a_panel_nested_in_itself_returns_without_a_reference() -> None:
    text = '{"panels": [&p {"title": "t", "panels": [*p]}]}'
    document = yaml.compose(text, Loader=yaml.SafeLoader)
    found = _within(LIMIT, lambda: dashboard_references(PurePosixPath("d.json"), [document]))
    assert found == []


def test_a6_a_panel_nested_in_itself_is_read_once() -> None:
    document = yaml.compose(
        '{"panels": [&p {"title": "t", "targets": [{"rawSql": "SELECT a FROM t WHERE b = 1"}], '
        '"panels": [*p]}]}',
        Loader=yaml.SafeLoader,
    )
    found = _within(LIMIT, lambda: dashboard_references(PurePosixPath("d.json"), [document]))
    assert len(found) == 1
    assert found[0].meta["library"] == "dashboard"


# -- A7, A8: GraphQL Code Generator ---------------------------------------------------------


def test_a7_an_exponential_enum_values_alias_refuses_the_configuration() -> None:
    width = 128
    lines = [f"l0: &l0 [{', '.join(['x'] * width)}]"]
    for n in range(1, 4):
        lines.append(f"l{n}: &l{n} [{', '.join([f'*l{n - 1}'] * width)}]")
    lines += [
        "generates:",
        "  out.ts:",
        "    plugins: [typescript]",
        "    config:",
        "      enumValues: *l3",
    ]
    parsed = _parse("codegen.yml", "\n".join(lines) + "\n")

    assert [r for r in parsed.references if r.edge_type == EdgeType.GENERATES] == []
    assert PARSE_BOUND_EXCEEDED in _messages(parsed)
    assert PARSE_BOUND_EXCEEDED == "parse bound exceeded"


def _graphql_config(projects: int, outputs: int) -> str:
    """``projects`` projects, each with the one ``codegen`` extension of ``outputs`` outputs."""
    lines = ["x-codegen: &cg", "  generates:"]
    lines += [f"    ./o{n}.ts: {{plugins: [typescript]}}" for n in range(outputs)]
    lines.append("projects:")
    lines += [
        f"  p{n}: {{schema: s{n}.graphql, extensions: {{codegen: *cg}}}}" for n in range(projects)
    ]
    return "\n".join(lines) + "\n"


def _codegen_claims(text: str) -> tuple[list[Any], list[Any]]:
    documents = _compose(text)
    return _within(
        LIMIT,
        lambda: codegen_references(PurePosixPath(".graphqlrc.yml"), documents, text),
    )


def test_a8_projects_times_outputs_beyond_the_file_bound_is_refused() -> None:
    claims, diagnostics = _codegen_claims(_graphql_config(projects=100, outputs=100))
    assert claims == []
    assert [d.message for d in diagnostics] == [PARSE_BOUND_EXCEEDED]


def test_a8_control_a_small_project_set_is_read_in_full() -> None:
    claims, diagnostics = _codegen_claims(_graphql_config(projects=3, outputs=3))
    assert diagnostics == []
    # One claim per project, and one per output of each project's extension: 3 + 3 * 3.
    assert len(claims) == 12
    assert {c.meta[PENDING_FACT] for c in claims} == {GRAPHQL_CODEGEN_OUTPUT}


# -- A9: a CI file nested past the interpreter's recursion limit ----------------------------


def _chained_flow_lists(blocks: int, depth: int) -> str:
    lines = []
    for n in range(blocks):
        inner = "x" if n == 0 else f"*b{n - 1}"
        lines.append(f"b{n}: &b{n} {'[' * depth}{inner}{']' * depth}")
    return "\n".join(lines) + "\n"


def test_a9_deeply_chained_ci_lists_are_read_in_part_and_say_so() -> None:
    path = ".github/workflows/ci.yml"
    text = _chained_flow_lists(blocks=5, depth=250)

    documents = _within(LIMIT, lambda: _compose(text))
    _claims, truncated = _within(LIMIT, lambda: ci_references(PurePosixPath(path), documents))
    assert truncated is True

    parsed = _parse(path, text)
    assert any("CI configuration read only in part" in m for m in _messages(parsed))


# -- A10, A11: Helm values ------------------------------------------------------------------


def test_a10_a_values_mapping_holding_itself_is_cut_at_the_depth_bound() -> None:
    document = yaml.compose("m: &m {x: 1, a: *m, b: *m}\n", Loader=yaml.SafeLoader)
    read = _within(LIMIT, lambda: read_values(document))

    assert read.truncated
    assert read.leaves
    assert max(path.count(".") for path in read.leaves) <= MAX_VALUE_DEPTH


def _self_referencing_mapping(fan_out: int) -> str:
    return "m: &m {" + ", ".join(f"k{n}: *m" for n in range(fan_out)) + "}\n"


def test_a11_a_values_mapping_holding_itself_thousands_of_times_returns() -> None:
    document = yaml.compose(_self_referencing_mapping(5000), Loader=yaml.SafeLoader)

    read = _within(LIMIT, lambda: read_values(document))
    assert read.truncated
    found = _within(
        LIMIT, lambda: values_target_references(PurePosixPath("values.yaml"), [document])
    )
    assert found == []


# -- A12: nesting deeper than the interpreter can compose -----------------------------------


def test_a12_flow_nesting_past_the_recursion_limit_is_a_warning_not_a_crash() -> None:
    text = "a: " + "[" * 3000 + "]" * 3000

    parsed = _parse("deep.yaml", text)
    assert parsed.symbols == ()
    assert any(d.severity == Severity.WARNING for d in parsed.diagnostics)


def test_a12_the_same_in_a_salt_state_file() -> None:
    text = "a: " + "[" * 3000 + "]" * 3000

    parsed = _parse_salt("deep.sls", text)
    assert parsed.symbols == ()
    assert any(d.severity == Severity.WARNING for d in parsed.diagnostics)
