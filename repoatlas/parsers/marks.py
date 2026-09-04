"""Line provenance from YAML marks (ARCHITECTURE.md §5).

The authoritative source-range calculation for every YAML-shaped parser. It lives
in one place because provenance is a fact about evidence, and a fact computed two
slightly different ways is two facts.

The invariant:

    A deterministic entity or fact may cite only the smallest source span that
    actually establishes it.

**PyYAML marks do not mean what they look like.** A composite node's ``end_mark``
is the position of the *next token*, reached after the scanner has consumed every
blank line and comment in between — so a mapping's end mark points at the line
where the following construct begins, not at the end of its own content:

.. code-block:: yaml

    first_state:          # value mapping: start 2:2 ... end 5:0
      pkg.installed:
        - name: nginx
                          # <- blank
    second_state:         # ...which is *here*

Subtracting one for an end mark in column 0 removes the most obvious symptom and
nothing else: the blank line, the comment, and the neutralized ``{% endif %}``
are all still inside the range. Block scalars have the same problem for a
different reason — ``- contents: |`` ends where the *next* token starts too.

So the end is not taken from the node's own mark at all. It is derived from the
node's **content**: recursively, the last line any leaf of the subtree occupies.
A leaf mark is exact, and a construct that is not part of the state — a sibling,
a comment, a wrapper's ``{% endif %}`` — is by definition not a leaf of it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import yaml


def line_count(content: str) -> int:
    """Lines in ``content``. A trailing newline ends a line; it does not add one."""
    return max(1, len(content.splitlines()))


def diagnostic_line(mark: Any | None, content: str) -> int | None:
    """The 1-based line a parse failure points at, clamped to the file."""
    if mark is None:
        return None
    return min(int(mark.line) + 1, line_count(content))


def node_span(node: yaml.Node, lines: Sequence[str]) -> tuple[int, int]:
    """The 1-based inclusive source range a composed YAML node establishes.

    ``lines`` is the file split into lines — the **raw** file, not a neutralized
    copy, so that content which only looks empty after neutralization still counts
    as content. Jinja neutralization preserves every character position, so mark
    line numbers index the raw lines exactly.
    """
    start = node.start_mark.line + 1
    return start, max(start, _end_line(node, lines))


def _end_line(node: yaml.Node, lines: Sequence[str]) -> int:
    """The last line this node's own content occupies.

    Composite nodes answer through their children, which is the whole trick: the
    next sibling, the trailing comment and the wrapper's closing Jinja are not
    children, so no amount of whitespace between them can pull the range along.
    """
    if isinstance(node, yaml.MappingNode):
        ends = [_end_line(child, lines) for pair in node.value for child in pair]
    elif isinstance(node, yaml.SequenceNode):
        ends = [_end_line(item, lines) for item in node.value]
    else:
        ends = []
    return max(ends) if ends else _leaf_end(node, lines)


def _leaf_end(node: yaml.Node, lines: Sequence[str]) -> int:
    """A scalar, or an empty collection such as ``[]``.

    A plain or quoted scalar's end mark is exact. A **block** scalar's is not — it
    is the next token again — so the tail is trimmed by walking back over blank
    lines. Only blank ones: a ``#`` inside a literal block is content, and the
    trailing-comment case never reaches here because a comment is not a leaf of
    the node above it.
    """
    start = node.start_mark.line + 1
    end = min(node.end_mark.line + 1, max(1, len(lines)))
    if node.end_mark.column == 0 and end > start:
        # The mark sits at the very start of a line, so that line is not covered.
        end -= 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return end
