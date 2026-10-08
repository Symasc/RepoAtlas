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

import re
from collections.abc import Sequence
from typing import Any

import yaml

#: What ends a source line, for every parser whose line numbers come from a
#: grammar rather than from PyYAML. Deliberately **not** ``str.splitlines()``,
#: which also breaks on form feed, vertical tab, the file/group/record separators
#: and U+2028/U+2029/U+0085.
#:
#: A form feed is an ordinary page-break character inside a source file: ``ast``
#: reports the same line number either side of one, and so do the tree-sitter
#: grammars. Counting it as a line break would put the whole-file span one line
#: past the last line any parser, editor or ``wc -l`` agrees exists — the same
#: phantom-line defect this module exists to prevent, with a rarer trigger.
#:
#: **This is not the right splitter for a PyYAML mark.** PyYAML rejects form feed
#: and vertical tab as unacceptable characters, so they never reach mark
#: arithmetic at all; but it *does* treat U+0085, U+2028 and U+2029 as line
#: breaks, exactly as ``str.splitlines()`` does. A line array indexed by a PyYAML
#: mark must therefore be built with ``splitlines()`` — see the note in
#: :mod:`repoatlas.parsers.structured`. The rule both halves obey: **the line
#: model must match whatever produced the line numbers.**
_LINE_BREAK = re.compile(r"\r\n|\r|\n")


def split_lines(content: str) -> list[str]:
    """``content`` as source lines, without their terminators.

    The single splitter behind every line number RepoAtlas computes for itself —
    :func:`line_count`, the whole-file span, and the chunker's line array. They
    share it so that a chunk's ``1-7`` and an entity's ``1-7`` are claims about
    the same seven lines. Two line models is two sets of line numbers, which is
    the defect this module was written to end.
    """
    if not content:
        return []
    lines = _LINE_BREAK.split(content)
    # A trailing separator leaves one empty element that is not a line.
    if lines[-1] == "":
        lines.pop()
    return lines


def line_count(content: str) -> int:
    """The number of source lines in ``content``.

    The authoritative definition of "how many lines does this file have" for
    provenance, so that a citation can never name a line the file does not contain
    (ARCHITECTURE.md §11).

    The rule, and the reason each part of it is there:

    * **a terminal newline ends the final line, it does not create another one.**
      ``"a\nb\n"`` is two lines. Getting this wrong was the defect: five parsers
      computed ``content.count("\n") + 1``, which is the number of line *slots*
      delimited by newlines, not the number of lines that contain anything. Every
      ordinary file — they nearly all end in a newline — got one phantom line;
    * **a genuine trailing blank line is a real line.** ``"a\n\n"`` is two.
      Blank lines are part of the file, and a whole-file span covers them. The bug
      was the phantom position after the last newline, not trailing blanks;
    * **an empty file is zero lines**, which is why a whole-file span over one has
      no line numbers at all (see :func:`whole_file_span`).
    """
    return len(split_lines(content))


def line_at(content: str, index: int) -> int:
    """The 1-based line ``content[index]`` sits on.

    The offset-addressed half of the same model: a manifest parser that locates a
    package by :meth:`str.find` needs a line number for the position it found, and
    it has to be the *same* line number the file's other facts use. Counting only
    ``"\n"`` — which is what these parsers did — puts every dependency in a
    classic-Mac manifest on line 1, and a form feed in a Gemfile put ``rails`` on
    line 3 of a two-line file: a range no line of that file can satisfy.
    """
    if index < 0:
        return 1
    return len(_LINE_BREAK.findall(content, 0, index)) + 1


def line_starts(content: str) -> list[int]:
    """The offset each source line of ``content`` starts at.

    :func:`line_at` for a parser that asks about many offsets: ``bisect_right(starts,
    index)`` is the line ``content[index]`` sits on, under the same line model, without
    rescanning everything before ``index`` on every call. The two agree at every index
    but the ``\\n`` of a ``\\r\\n`` pair, which is no character a span ends on.
    """
    return [0, *(match.end() for match in _LINE_BREAK.finditer(content))]


def whole_file_span(content: str) -> tuple[int | None, int | None]:
    """The span a whole-file entity may claim over ``content``.

    An empty file has **no** lines, so its module/script/template entity gets no
    line numbers: ``(None, None)``. The entity still exists — an empty
    ``__init__.py`` is a real, importable module, and dropping it would strand
    every import of that package — it simply makes no claim about source it does
    not have. That is the same representation the ``file`` entity has always used
    for a fact that is about the file rather than about a range inside it
    (:class:`~repoatlas.models.evidence.Evidence` calls it a path-only citation),
    so nothing downstream needed a new case to understand it.

    Anchoring an empty file at ``1-1`` instead — the previous behaviour — would
    name a line that does not exist, which is the very thing a whole-file span is
    computed carefully to avoid.
    """
    lines = line_count(content)
    return (1, lines) if lines else (None, None)


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
