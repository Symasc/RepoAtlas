"""The work a hostile alias file costs, counted rather than timed.

``test_structured_alias_hostile_input.py`` proves these files *return*. A wall-clock limit cannot
tell "bounded" from "slow on this machine", so these tests count the calls instead: how often the
span calculation looks at a node, how often the key walk is entered, how often a Helm values walk
reads a key. Each count is compared with the size of the graph the file composes to, which is
what a bounded implementation is allowed to cost -- never with the (astronomical) size of the tree
the file spells out.
"""

from __future__ import annotations

import itertools
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
import yaml
from repoatlas.parsers import deploy_helm, marks, structured
from repoatlas.parsers.deploy_helm import MAX_VALUE_NODES, read_values
from repoatlas.parsers.deploy_targets import values_target_references
from repoatlas.parsers.graphql_codegen import _expanded_size
from repoatlas.parsers.structured import StructuredParser, _KeyCollector

pytestmark = pytest.mark.adversarial


def _compose(text: str) -> list[yaml.Node]:
    return list(yaml.compose_all(text, Loader=yaml.SafeLoader))


def _children(node: yaml.Node) -> list[yaml.Node]:
    """Written out here rather than imported, so a patched ``marks.node_children`` cannot
    change what the counts are compared with."""
    if isinstance(node, yaml.MappingNode):
        return [child for pair in node.value for child in pair]
    if isinstance(node, yaml.SequenceNode):
        return list(node.value)
    return []


def _graph_size(documents: list[yaml.Node]) -> tuple[int, int]:
    """Distinct nodes and the child edges between them: what the composed graph holds."""
    seen: set[int] = set()
    edges = 0
    stack = [d for d in documents if d is not None]
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        children = _children(node)
        edges += len(children)
        stack.extend(children)
    return len(seen), edges


def _a1() -> str:
    lines = ["l0: &l0 [x, y, z]"]
    for n in range(1, 10):
        lines.append(f"l{n}: &l{n} [{', '.join([f'*l{n - 1}'] * 9)}]")
    lines.append("config: {projects: [{name: a, big: *l9}]}")
    return "\n".join(lines) + "\n"


def _a3() -> str:
    names = "abcdefghi"
    lol = '"lol"'
    lines = [f"a: &a [{', '.join([lol] * 9)}]"]
    for previous, name in itertools.pairwise(names):
        lines.append(f"{name}: &{name} [{', '.join([f'*{previous}'] * 9)}]")
    lines.append("top: {laughs: *i}")
    return "\n".join(lines) + "\n"


def _a4() -> str:
    items = ", ".join(str(n) for n in range(50_000))
    return "\n".join([f"big: &b [{items}]", *(f"k{n}: *b" for n in range(400))]) + "\n"


INPUTS = {"a1": _a1, "a3": _a3, "a4": _a4}


# -- B1: a node's span is worked out once ---------------------------------------------------


@pytest.mark.parametrize("name", sorted(INPUTS))
def test_b1_the_span_calculation_looks_at_each_node_at_most_twice(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = INPUTS[name]()
    nodes, _ = _graph_size(_compose(text))
    calls = 0
    real = marks.node_children

    def counting(node: yaml.Node) -> list[yaml.Node]:
        nonlocal calls
        calls += 1
        return real(node)

    monkeypatch.setattr(marks, "node_children", counting)
    StructuredParser().parse(Path("settings.yaml"), text)

    assert 0 < calls <= 2 * nodes


# -- B2: the key walk's total entries are bounded by the graph and the alias budget ---------


@pytest.mark.parametrize("name", ["a1", "a3"])
def test_b2_the_key_walk_is_entered_a_bounded_number_of_times(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = INPUTS[name]()
    documents = _compose(text)
    nodes, edges = _graph_size(documents)
    calls = 0
    real = _KeyCollector.walk

    def counting(self: _KeyCollector, *args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        real(self, *args, **kwargs)

    monkeypatch.setattr(_KeyCollector, "walk", counting)
    StructuredParser().parse(Path("settings.yaml"), text)

    # The file really does spend the whole alias budget: this is the bounded regime.
    assert calls > structured._MAX_ALIAS_WALKS
    assert calls <= nodes + 8 * edges + structured._MAX_ALIAS_WALKS + 1 + len(documents)


# -- B3: a values walk's key reads --------------------------------------------------------


def test_b3_a_values_mapping_holding_itself_costs_a_bounded_number_of_key_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fan_out = 50
    text = "m: &m {" + ", ".join(f"k{n}: *m" for n in range(fan_out)) + "}\n"
    document = yaml.compose(text, Loader=yaml.SafeLoader)
    nodes, edges = _graph_size([document])
    assert nodes > 0
    calls = 0
    real = deploy_helm._scalar

    def counting(node: yaml.Node | None) -> str | None:
        nonlocal calls
        calls += 1
        return real(node)

    monkeypatch.setattr(deploy_helm, "_scalar", counting)
    read = read_values(document)

    assert read.truncated
    assert 0 < calls <= edges + 2 * MAX_VALUE_NODES + fan_out


def _wide_mapping(width: int) -> str:
    """``x``: one mapping of ``width`` keys, every one of them dotted -- so a values walk reads
    each key and descends into none."""
    return "x: &x {" + ", ".join(f'"a.{n}": 1' for n in range(width)) + "}\n"


def test_b3_repeated_visits_of_a_wide_mapping_stop_at_the_repeat_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The visit cap counts nodes popped, not the work each pop does: 200 aliases of one
    # 2000-key mapping are 200 visits but 400,000 key reads. Only the repeat budget stops it.
    width = 2_000
    text = _wide_mapping(width) + "y: [" + ", ".join(["*x"] * 200) + "]\n"
    document = yaml.compose(text, Loader=yaml.SafeLoader)
    _, edges = _graph_size([document])
    calls = 0
    real = deploy_helm._scalar

    def counting(node: yaml.Node | None) -> str | None:
        nonlocal calls
        calls += 1
        return real(node)

    monkeypatch.setattr(deploy_helm, "_scalar", counting)
    read = read_values(document)

    assert read.truncated
    assert 0 < calls <= edges + 2 * MAX_VALUE_NODES + width


class _CountingList(list):  # type: ignore[type-arg]
    """A list that counts how often it is iterated: how often a walk reads a mapping's pairs."""

    def __init__(self, items: Any) -> None:
        super().__init__(items)
        self.iterations = 0

    def __iter__(self) -> Any:
        self.iterations += 1
        return super().__iter__()


def test_b3_the_listen_ports_walk_stops_at_the_repeat_budget() -> None:
    width = 2_000
    aliases = ", ".join(f"p{n}: *x" for n in range(200))
    text = _wide_mapping(width) + "y: {" + aliases + "}\n"
    documents = _compose(text)
    root = documents[0]
    wide = root.value[0][1]
    assert isinstance(wide, yaml.MappingNode)
    assert len(wide.value) == width
    # Every alias is the one node the walk then counts.
    y = root.value[1][1]
    assert len(y.value) == 200
    assert all(value is wide for _, value in y.value)
    counter = _CountingList(wide.value)
    wide.value = counter

    values_target_references(PurePosixPath("values.yaml"), documents)

    assert 0 < counter.iterations <= MAX_VALUE_NODES // width + 2


# -- B4: the expanded size of a configuration -----------------------------------------------


def test_b4_a_document_without_aliases_is_its_node_count() -> None:
    root = yaml.compose("a: [x, y]\nb: {c: d}\n", Loader=yaml.SafeLoader)
    nodes, _ = _graph_size([root])
    assert nodes == 9
    assert _expanded_size(root, 1_000) == 9


def test_b4_an_alias_counts_every_time_it_is_written_out() -> None:
    root = yaml.compose("l0: &l0 [x, x]\nl1: &l1 [*l0, *l0]\n", Loader=yaml.SafeLoader)
    l1 = root.value[1][1]
    # One list, and two copies of (one list and two scalars).
    assert _expanded_size(l1, 1_000) == 7
    # The whole document: its own node, both keys, and each list as above (3 and 7).
    assert _expanded_size(root, 1_000) == 1 + 1 + 3 + 1 + 7


def test_b4_a_document_that_contains_itself_has_no_size() -> None:
    root = yaml.compose("a: &a [*a]\n", Loader=yaml.SafeLoader)
    assert _expanded_size(root, 1_000) is None


def test_b4_the_count_stops_one_past_the_limit() -> None:
    wide = ", ".join(["x"] * 4)
    lines = [f"l0: &l0 [{wide}]"]
    lines += [f"l{n}: &l{n} [{', '.join([f'*l{n - 1}'] * 4)}]" for n in range(1, 8)]
    root = yaml.compose("\n".join(lines) + "\n", Loader=yaml.SafeLoader)

    assert _expanded_size(root, 100) == 101

    small = yaml.compose("a: [x, y]\nb: {c: d}\n", Loader=yaml.SafeLoader)
    assert _expanded_size(small, 9) == 9
    assert _expanded_size(small, 8) == 9
    assert _expanded_size(small, 3) == 4


# -- B5: one memo serves every value that aliases the same subtree ---------------------------


def test_b5_values_aliasing_one_large_anchor_share_one_span_calculation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A new ``NodeSpans`` per value would return the same lines and lose only the memo: each
    # of the keys would then work the whole anchor out again. Leaves are counted on the module
    # function every ``NodeSpans`` calls, so a fresh instance is counted too. Keys stay under
    # the 400-key cap (1 + 300), so the walk reaches every alias and asks for its value's span.
    items = 2_000
    aliases = 300
    assert aliases + 1 < structured._MAX_KEYS
    text = (
        f"a: &a [{', '.join(str(n) for n in range(items))}]\n"
        + "\n".join(f"k{n}: *a" for n in range(aliases))
        + "\n"
    )
    nodes, _ = _graph_size(_compose(text))
    assert nodes > items
    computed = 0
    real = marks._leaf_end

    def counting(node: yaml.Node, lines: Any) -> int:
        nonlocal computed
        computed += 1
        return real(node, lines)

    monkeypatch.setattr(marks, "_leaf_end", counting)
    parsed = StructuredParser().parse(Path("settings.yaml"), text)

    # Every key was listed, so every value span was asked for.
    assert {s.qualified_name for s in parsed.symbols} >= {f"k{n}" for n in range(aliases)}
    assert 0 < computed <= nodes + aliases + items
