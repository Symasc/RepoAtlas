"""Turning source text into something that may honestly be cited (RepoAtlas V2).

Every capability that hands an agent bytes it can cite faces the same two
questions, and they have nothing to do with how the bytes were found. *Which
source lines does this text actually cover?* And *what survives when only part of
it fits?* Search asks them about a retrieved candidate; inspection asks them about
a range of a file that was addressed by name. The answers must be the same, or one
capability would license a citation the other would refuse.

So the answers live here, once, below both — and this module knows about neither.
It takes text, the coordinates that text is claimed to occupy, and a character
allowance. It returns what may be supplied and what may be claimed about it. There
is no request type, no retriever, no store and no transport in sight.

The rule it exists to hold
--------------------------

    A citable span covers only complete lines whose text was supplied in full.

Grounding verifies a citation by intersecting it with the spans that were supplied
(V1_SPEC §15). A span wider than the text therefore authorizes citing lines the
agent was never shown — a fabrication RepoAtlas would have licensed itself. So
truncation narrows the line range along with the text, a partial line is never
supplied, and text whose line coverage cannot be established claims no line at all.

How text maps to lines
----------------------
Verified against the index rather than assumed. Every chunk in the store satisfies::

    text == "\\n".join(source_lines[line_start - 1 : line_end])

so ``text.split("\\n")`` yields exactly ``line_end - line_start + 1`` pieces and
piece *k* is source line ``line_start + k``. Note ``split`` and not ``splitlines``:
a span ending in a blank line produces a trailing empty piece that ``splitlines``
discards, which would make the count disagree with the span for a perfectly
ordinary chunk.

Text that does **not** satisfy it is real and is handled rather than special-cased
away: an entity description carries a one-line signature against a multi-line
declared span, and a piece of a split span begins mid-span. Neither proves which
source lines its text covers, so neither gets a line claim. The check is
arithmetic on the text itself, so it needs no metadata and cannot be fooled by a
producer this module has not seen.
"""

from __future__ import annotations

from dataclasses import dataclass

#: How source text was joined back together, and therefore how it splits.
LINE_SEPARATOR = "\n"


@dataclass(frozen=True, slots=True)
class SuppliedText:
    """Text that is about to be handed over, and the claim it may carry.

    ``text`` is exactly what the caller will be shown and exactly what a digest
    will cover. ``line_start``/``line_end`` describe only lines inside ``text``;
    both ``None`` means the file may be claimed and no line may.
    """

    text: str
    line_start: int | None
    line_end: int | None
    #: Something the source held was not supplied.
    truncated: bool


def provable_span(
    text: str,
    line_start: int | None,
    line_end: int | None,
    *,
    split_total: int | None = None,
) -> tuple[int, int] | None:
    """The source lines ``text`` demonstrably covers, or ``None``.

    ``None`` is the honest answer in four situations, and each is a real producer
    rather than a hypothetical:

    * no line is claimed to begin with — a filename match knows nothing about any
      line, so it says nothing about one (V1_SPEC §11);
    * the text is empty — an entity that was reached but has no stored signature
      supplies no line, and arithmetic that "proves" an empty string covers line 1
      would authorize citing a line nobody was shown;
    * ``split_total`` says the span was cut — this is one piece of it, and every
      piece carries the whole span's line range (ARCHITECTURE.md §6);
    * the piece count disagrees with the span — a signature preview, or a span
      whose text and range simply disagree.

    A ``None`` here is not a discarded source. The text is still supplied; it is
    supplied as a claim about the file rather than about lines.

    Why the cut test cannot be left to the count
    --------------------------------------------
    The count test looks like it subsumes the cut test and does not. A one-line
    span cut into pieces gives every piece a one-line range *and* a piece count of
    one whenever that piece holds no newline — which is always, for the minified
    bundles and generated one-liners that reach the cap. The arithmetic agrees
    with itself and the text still covers a fraction of the line it names. A
    two-line span cut just after its first newline does the same with two.

    So the two tests answer different questions — *was anything cut?* and *does
    what is here line up with what is claimed?* — and only the pair is safe.
    :func:`~repoatlas.capabilities.indexed_source.indexed_line_map` has always
    applied both; this is the same rule, stated once, for the capabilities that
    reach source through retrieval rather than through the file.

    ``split_total`` is ``None`` for text that never came from a chunk — an entity
    signature, a ledger reference — where there is no cut to ask about and the
    count test is the whole answer, exactly as before.
    """
    if line_start is None or line_end is None:
        return None
    if not text:
        return None
    if split_total is not None and split_total != 1:
        # A piece of a cut span. Its text does not cover the range it carries,
        # and no arithmetic on the piece can recover which lines it does cover.
        return None
    pieces = text.split(LINE_SEPARATOR)
    if len(pieces) != line_end - line_start + 1:
        return None
    return (line_start, line_end)


def prefix_lines(text: str, limit: int) -> tuple[str, int]:
    """The longest whole-line prefix of ``text`` within ``limit`` characters.

    Returns the prefix and how many lines it holds. A partial line is never
    included: it would be text the caller can read attached to a line number that
    promises the rest of it.
    """
    if limit <= 0:
        return ("", 0)
    if len(text) <= limit:
        return (text, len(text.split(LINE_SEPARATOR)))

    pieces = text.split(LINE_SEPARATOR)
    kept: list[str] = []
    used = 0
    for piece in pieces:
        # Every line after the first costs its separator too.
        cost = len(piece) + (1 if kept else 0)
        if used + cost > limit:
            break
        kept.append(piece)
        used += cost
    if not kept:
        return ("", 0)
    return (LINE_SEPARATOR.join(kept), len(kept))


def supply(
    text: str,
    line_start: int | None,
    line_end: int | None,
    *,
    limit: int,
    split_total: int | None = None,
) -> SuppliedText | None:
    """What may be handed over from ``text`` within ``limit`` characters.

    ``None`` means not one complete line fits. That is a distinct answer from
    supplying nothing: a caller that cannot fit a line must decide what to do about
    it, and silently returning empty text would look like a source that had nothing
    in it.

    The span narrows with the text. If ``text`` proved lines 10-80 and only the
    first 23 fit, the claim is 10-32 — never 10-80, which would let a citation
    reach lines that were cut.
    """
    span = provable_span(text, line_start, line_end, split_total=split_total)
    kept, lines = prefix_lines(text, limit)
    if not kept:
        return None
    if span is None:
        return SuppliedText(
            text=kept, line_start=None, line_end=None, truncated=len(kept) < len(text)
        )
    return SuppliedText(
        text=kept,
        line_start=span[0],
        line_end=span[0] + lines - 1,
        truncated=len(kept) < len(text),
    )
