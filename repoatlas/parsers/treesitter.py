"""Shared tree-sitter machinery (ARCHITECTURE.md §15, V1_SPEC §7).

tree-sitter is chosen for TypeScript/JavaScript and Bash because the
alternative — language-native tooling such as ``tsc`` — means starting a
language runtime next to untrusted repository content. tree-sitter
parses in-process, never evaluates what it reads, and tolerates syntax errors by
producing a partial tree, which is exactly the "diagnostic plus partial results"
behavior V1_SPEC §7 requires.

Grammars are loaded lazily and cached: importing this module must not pay for a
language the current sync never encounters.

**Grammars come only from installed, pinned packages (OFF-01).** The table in
:mod:`repoatlas.parsers.grammars` names each one; :func:`load_parser` opens exactly
that artifact or reports it unavailable. There is no cache directory, no
environment variable, no search path and no fallback that could put a download,
a build or a file some other program left behind between a repository and the
parser code that reads it. An earlier version delegated to a library that fetched
grammar binaries from the network on first use and then trusted whatever sat in
the user's cache directory; that is the path this module no longer has.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import sys
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

from repoatlas.parsers.grammars import GRAMMARS, Grammar

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tree_sitter import Node, Parser, Tree


class GrammarUnavailable(RuntimeError):
    """A grammar could not be loaded. Becomes a diagnostic, never a crash."""


@lru_cache(maxsize=16)
def load_parser(language: str) -> Parser:
    """Cached tree-sitter parser for ``language``, from its pinned installed grammar.

    Fails closed, as :class:`GrammarUnavailable`, when the grammar is not one
    RepoAtlas ships, when its distribution is missing or at any version but the
    pinned one, or when the module that would be imported is not the one that
    distribution installed. The exception text is for logs; every parser turns it
    into a fixed diagnostic.
    """
    grammar = GRAMMARS.get(language)
    if grammar is None:
        raise GrammarUnavailable(f"no grammar is shipped for {language!r}")
    try:
        from tree_sitter import Language, Parser
    except ImportError as exc:  # pragma: no cover - dependency is required
        raise GrammarUnavailable(f"tree-sitter is not installed: {exc}") from exc
    try:
        module = _import_pinned(grammar)
        return Parser(Language(getattr(module, grammar.function)()))
    except GrammarUnavailable:
        raise
    except Exception as exc:
        raise GrammarUnavailable(f"grammar {language!r} unavailable: {exc}") from exc


def _import_pinned(grammar: Grammar) -> ModuleType:
    """Import ``grammar.module``, but only as installed by the pinned distribution.

    Both halves of the module are checked against the distribution's own record of
    the files it installed: the package, **before** it is imported, so a same-named
    module earlier on ``sys.path`` is refused rather than executed; and the native
    binding, after, so the shared object that actually holds the grammar is one the
    installer put there.
    """
    try:
        distribution = importlib.metadata.distribution(grammar.distribution)
    except importlib.metadata.PackageNotFoundError as exc:
        raise GrammarUnavailable(f"{grammar.distribution} is not installed") from exc
    if distribution.version != grammar.version:
        raise GrammarUnavailable(
            f"{grammar.distribution} {distribution.version} is installed, "
            f"{grammar.version} is pinned"
        )
    owned = {
        Path(str(distribution.locate_file(entry))).resolve() for entry in distribution.files or ()
    }
    spec = importlib.util.find_spec(grammar.module)
    if spec is None or spec.origin is None or Path(spec.origin).resolve() not in owned:
        raise GrammarUnavailable(
            f"{grammar.module} is not the module {grammar.distribution} installed"
        )

    module = importlib.import_module(grammar.module)
    binding = sys.modules.get(f"{grammar.module}._binding")
    binding_file = getattr(binding, "__file__", None)
    if binding_file is None or Path(binding_file).resolve() not in owned:
        raise GrammarUnavailable(
            f"{grammar.module} binding is not the one {grammar.distribution} installed"
        )
    return module


#: ASCII carriage return and line feed. Single bytes, which is what makes the
#: substitution in :func:`parse_tree` offset-preserving.
_CR, _LF = 0x0D, 0x0A


def parse_tree(language: str, content: str) -> tuple[Tree, bytes]:
    """Parse ``content`` and return the tree with the exact bytes it indexes.

    tree-sitter offsets are byte offsets, so the encoded source is returned with
    the tree; slicing anything else would misalign on non-ASCII input.

    **Classic-Mac line endings are normalized first.** The grammars for
    TypeScript and Bash do not accept a lone ``\r`` as a line terminator, so
    a file that uses only ``\r`` collapses into a single line as far as tree-sitter
    is concerned. That is not a cosmetic loss: in a seven-line CR-only TypeScript
    file, ``beta`` on lines 5-7 was reported at ``1-1``, and grounding would have
    believed a citation to line 1 for content that is not there — a fabricated
    citation for *real* content, which is worse than the phantom end-of-file line
    this module's line model was written to prevent. Bash fared worse still and
    lost both function definitions outright.

    The rewrite is deliberately confined to the unambiguous case: a file holding at
    least one ``\r`` and **no** ``\n`` at all is a classic-Mac file in which every
    ``\r`` is a terminator. A file that mixes the two is left exactly as it is,
    because there no rule can tell a terminator from a carriage return inside a
    string. Both bytes are one byte wide, so every offset, and therefore every
    slice :func:`node_text` takes, is unchanged.
    """
    source = content.encode("utf-8")
    if _CR in source and _LF not in source:
        source = source.replace(b"\r", b"\n")
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
