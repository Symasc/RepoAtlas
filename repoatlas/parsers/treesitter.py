"""Shared tree-sitter machinery (ARCHITECTURE.md §15, V1_SPEC §7).

tree-sitter is chosen for TypeScript/JavaScript, Bash and Ruby because the
alternative — language-native tooling such as ``tsc`` or ``ripper`` — means
starting a language runtime next to untrusted repository content. tree-sitter
parses in-process, never evaluates what it reads, and tolerates syntax errors by
producing a partial tree, which is exactly the "diagnostic plus partial results"
behavior V1_SPEC §7 requires.

Grammars are loaded lazily and cached: importing this module must not pay for a
language the current sync never encounters.
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tree_sitter import Node, Parser, Tree


class GrammarUnavailable(RuntimeError):
    """A grammar could not be loaded. Becomes a diagnostic, never a crash."""


@lru_cache(maxsize=16)
def load_parser(language: str) -> Parser:
    """Cached tree-sitter parser for ``language``."""
    try:
        from tree_sitter_language_pack import get_parser
    except ImportError as exc:  # pragma: no cover - dependency is required
        raise GrammarUnavailable(f"tree-sitter is not installed: {exc}") from exc
    try:
        return cast("Parser", get_parser(cast(Any, language)))
    except Exception as exc:
        raise GrammarUnavailable(f"grammar {language!r} unavailable: {exc}") from exc


def parse_tree(language: str, content: str) -> tuple[Tree, bytes]:
    """Parse ``content`` and return the tree with the exact bytes it indexes.

    tree-sitter offsets are byte offsets, so the encoded source is returned with
    the tree; slicing anything else would misalign on non-ASCII input.
    """
    source = content.encode("utf-8")
    return load_parser(language).parse(source), source


def node_text(source: bytes, node: Node) -> str:
    """Source text of ``node``, decoded leniently."""
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def line_range(node: Node) -> tuple[int, int]:
    """1-based inclusive line range of ``node`` (tree-sitter rows are 0-based)."""
    return node.start_point[0] + 1, node.end_point[0] + 1


def walk(node: Node) -> Iterator[Node]:
    """Every node in the subtree, parents before children."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def named_child(node: Node, field: str) -> Node | None:
    return node.child_by_field_name(field)


def field_text(source: bytes, node: Node, field: str) -> str | None:
    child = node.child_by_field_name(field)
    return None if child is None else node_text(source, child)


def string_literal(source: bytes, node: Node) -> str | None:
    """Contents of a quoted string node, or ``None`` when it is not a literal.

    An interpolated or computed string yields ``None``: the value is not knowable
    without executing the program, and guessing it would be inventing evidence.
    """
    text = node_text(source, node)
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'", "`"}:
        inner = text[1:-1]
        if "${" in inner or "#{" in inner:
            return None
        return inner
    return None


def has_error(tree: Tree) -> bool:
    """Whether the grammar recovered from at least one syntax error."""
    return tree.root_node.has_error
