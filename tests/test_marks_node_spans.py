"""``NodeSpans`` answers exactly what the recursive span calculation it replaced answered.

A span is a fact about evidence that ends up in citations, so replacing the way it is computed
(recursive and unmemoized, then iterative and memoized) must change no line number of any file
that was ever answerable. Verbatim copies of the code that was replaced are frozen below -- from
the commit before the change -- and every node of a corpus of real files and inline documents is
asked of both: in document order and in reverse (a memo that depends on which node was asked
first would show up as a difference between the two), through one shared ``NodeSpans`` and
through the one-shot :func:`node_span`.

The same corpus pins the key collector: the frozen ``_KeyCollector`` and today's
``collect_config_keys`` produce the same symbols for every document that does not spend the alias
budget.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml
from repoatlas.models.parsing import EntityType, ParsedSymbol
from repoatlas.parsers.jinja import neutralize
from repoatlas.parsers.marks import NodeSpans, node_span
from repoatlas.parsers.structured import collect_config_keys

# -- frozen: marks.py at da561ff ------------------------------------------------------------


def _frozen_node_span(node: yaml.Node, lines: Sequence[str]) -> tuple[int, int]:
    start = node.start_mark.line + 1
    return start, max(start, _frozen_end_line(node, lines))


def _frozen_end_line(node: yaml.Node, lines: Sequence[str]) -> int:
    if isinstance(node, yaml.MappingNode):
        ends = [_frozen_end_line(child, lines) for pair in node.value for child in pair]
    elif isinstance(node, yaml.SequenceNode):
        ends = [_frozen_end_line(item, lines) for item in node.value]
    else:
        ends = []
    return max(ends) if ends else _frozen_leaf_end(node, lines)


def _frozen_leaf_end(node: yaml.Node, lines: Sequence[str]) -> int:
    start = node.start_mark.line + 1
    end = min(node.end_mark.line + 1, max(1, len(lines)))
    if node.end_mark.column == 0 and end > start:
        # The mark sits at the very start of a line, so that line is not covered.
        end -= 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return end


# -- frozen: structured._KeyCollector at da561ff --------------------------------------------

_MAX_DEPTH = 6
_MAX_KEYS = 400


class _FrozenKeyCollector:
    def __init__(
        self,
        *,
        lines: Sequence[str],
        max_depth: int = _MAX_DEPTH,
        max_keys: int = _MAX_KEYS,
    ) -> None:
        self.symbols: list[ParsedSymbol] = []
        self.truncated = False
        self._lines = lines
        self._max_depth = max_depth
        self._max_keys = max_keys

    def walk(self, node: yaml.Node, prefix: str = "", depth: int = 0) -> None:
        if depth > self._max_depth:
            return
        if len(self.symbols) >= self._max_keys:
            self.truncated = True
            return

        if isinstance(node, yaml.MappingNode):
            for key_node, value_node in node.value:
                if not isinstance(key_node, yaml.ScalarNode):
                    continue
                name = key_node.value
                path = f"{prefix}.{name}" if prefix else name
                start, _ = _frozen_node_span(key_node, self._lines)
                _, end = _frozen_node_span(value_node, self._lines)
                self.symbols.append(
                    ParsedSymbol(
                        kind=EntityType.CONFIG,
                        name=name,
                        qualified_name=path,
                        line_start=start,
                        line_end=max(start, end),
                        signature=path,
                        parent=prefix or None,
                    )
                )
                if len(self.symbols) >= self._max_keys:
                    self.truncated = True
                    return
                self.walk(value_node, path, depth + 1)
        elif isinstance(node, yaml.SequenceNode):
            for index, item in enumerate(node.value):
                self.walk(item, f"{prefix}[{index}]", depth + 1)


# -- the corpus -----------------------------------------------------------------------------

_TESTS = Path(__file__).parent
_CORPUS_ROOTS = (_TESTS / "fixtures", _TESTS / "adversarial" / "fixtures" / "repos")
_SUFFIXES = frozenset({".yaml", ".yml", ".json", ".sls"})
#: Past this a file is a lockfile or a generated blob, and the frozen (unmemoized) calculation
#: would be asked about every node of it.
_MAX_FILE_BYTES = 60_000
_LEADING_TABS = re.compile(r"^\t+", re.MULTILINE)

#: Legitimate documents written for the cases real files rarely cover together.
_INLINE = {
    "anchor-reused-across-keys": "base: &b\n  x: 1\n  y: 2\nfirst: *b\nsecond: *b\n",
    "merge-keys": "base: &b {x: 1}\nchild:\n  <<: *b\n  y: 2\n",
    "block-scalars-blank-lines-comments": (
        "a: |\n  one\n\n  two\n\n\n# a comment\nb: >\n  folded\n\n\n  # inside\nc: 1\n"
    ),
    "empty-collections": "e: []\nf: {}\ng:\n  h: []\n  i: {}\n\nj: 1\n",
    "multi-document": "a: 1\n---\nb: 2\n...\n---\nc: &x [1]\nd: *x\n",
    "crlf": "a: |\r\n  one\r\n\r\nb:\r\n  - 1\r\n  - 2\r\n\r\nc: &x [1]\r\nd: *x\r\n",
    "null-values": "a:\nb: ~\nc: null\nd:\n  - ~\n  -\ne: 1\n",
    "complex-key": "? [a, b]\n: value\n? {c: d}\n: other\n",
    "alias-as-last-block-item": "a: &x foo\nb:\n  - 1\n  - *x\n",
    "anchor-and-alias-in-one-subtree": "s:\n  p: &x [1, 2]\n  q: *x\nt: 1\n",
    "nested-anchors": "a: &a\n  b: &b [1]\n  c: *b\nd: *a\n",
    "comment-and-blank-between-siblings": "a:\n  b: 1\n\n  # note\n\nc: 2\n",
    "salt-shaped": (
        "nginx:\n  pkg.installed:\n    - name: nginx\n\n# between\n"
        "service:\n  service.running:\n    - name: nginx\n    - require:\n      - pkg: nginx\n"
    ),
}


def _compose(text: str) -> list[yaml.Node]:
    return [d for d in yaml.compose_all(text, Loader=yaml.SafeLoader) if d is not None]


def _file_corpus() -> list[tuple[str, list[yaml.Node], list[str]]]:
    found = []
    for root in _CORPUS_ROOTS:
        for path in sorted(root.rglob("*")):
            if path.suffix not in _SUFFIXES or not path.is_file():
                continue
            if path.stat().st_size > _MAX_FILE_BYTES:
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            text = content
            if path.suffix == ".sls":
                text = neutralize(content)
            elif path.suffix == ".json":
                text = _LEADING_TABS.sub(lambda m: "    " * len(m.group(0)), content)
            try:
                documents = _compose(text)
            except (yaml.YAMLError, RecursionError):
                continue
            # Raw lines, indexed by marks taken from the neutralized text: as the parsers do.
            found.append((str(path.relative_to(_TESTS)), documents, content.splitlines()))
    return found


CORPUS: list[tuple[str, list[yaml.Node], list[str]]] = [
    *_file_corpus(),
    *((f"inline:{name}", _compose(text), text.splitlines()) for name, text in _INLINE.items()),
]


def _distinct_nodes(documents: list[yaml.Node]) -> tuple[list[yaml.Node], dict[int, set[int]]]:
    """Every distinct node in document order (parents first), and each one's parents."""
    order: list[yaml.Node] = []
    parents: dict[int, set[int]] = {}
    seen: set[int] = set()
    stack = list(reversed(documents))
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        order.append(node)
        if isinstance(node, yaml.MappingNode):
            children = [child for pair in node.value for child in pair]
        elif isinstance(node, yaml.SequenceNode):
            children = list(node.value)
        else:
            children = []
        for child in children:
            parents.setdefault(id(child), set()).add(id(node))
        stack.extend(reversed(children))
    return order, parents


def _ids(corpus: list[tuple[str, list[yaml.Node], list[str]]]) -> list[str]:
    return [name for name, _, _ in corpus]


@pytest.mark.parametrize(("name", "documents", "lines"), CORPUS, ids=_ids(CORPUS))
def test_c1_node_spans_answer_what_the_recursive_calculation_answered(
    name: str, documents: list[yaml.Node], lines: list[str]
) -> None:
    nodes, _ = _distinct_nodes(documents)
    expected = [_frozen_node_span(node, lines) for node in nodes]

    forward = NodeSpans(lines)
    for node, want in zip(nodes, expected, strict=True):
        where = f"{name}: forward, line {node.start_mark.line + 1}"
        assert forward.span(node) == want, where

    reverse = NodeSpans(lines)
    for node, want in reversed(list(zip(nodes, expected, strict=True))):
        where = f"{name}: reverse, line {node.start_mark.line + 1}"
        assert reverse.span(node) == want, where

    for node, want in zip(nodes, expected, strict=True):
        where = f"{name}: node_span, line {node.start_mark.line + 1}"
        assert node_span(node, lines) == want, where


def test_c1_the_corpus_is_large_enough_and_has_shared_nodes() -> None:
    documents = sum(len(docs) for _, docs, _ in CORPUS)
    assert documents >= 10

    shared = 0
    for _, docs, _ in CORPUS:
        _, parents = _distinct_nodes(docs)
        shared += sum(1 for owners in parents.values() if len(owners) >= 2)
    assert shared >= 1
    # The inline documents alone cover aliases, so this does not depend on the fixture files.
    inline_shared = 0
    for name, docs, _ in CORPUS:
        if name.startswith("inline:"):
            _, parents = _distinct_nodes(docs)
            inline_shared += sum(1 for owners in parents.values() if len(owners) >= 2)
    assert inline_shared >= 1


_KeyRow = tuple[str, int | None, int | None, str | None, str | None]


def _key_rows(symbols: Sequence[ParsedSymbol]) -> list[_KeyRow]:
    return [(s.qualified_name, s.line_start, s.line_end, s.parent, s.signature) for s in symbols]


@pytest.mark.parametrize(("name", "documents", "lines"), CORPUS, ids=_ids(CORPUS))
def test_c2_the_key_collector_lists_what_it_listed(
    name: str, documents: list[yaml.Node], lines: list[str]
) -> None:
    for document in documents:
        old = _FrozenKeyCollector(lines=lines)
        old.walk(document)
        symbols, truncated = collect_config_keys(document, lines=lines)

        assert _key_rows(symbols) == _key_rows(old.symbols), name
        assert truncated == old.truncated, name
