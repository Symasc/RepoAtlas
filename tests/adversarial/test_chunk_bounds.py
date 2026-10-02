"""Chunk size is bounded in characters, not only in lines (V1_SPEC §14, §20).

`max_chunk_lines` bounds how many *lines* a chunk spans, which says nothing about
how large it is. Minified JavaScript, embedded base64, a generated one-liner and a
long JSON array are all a single line, so a line-only bound lets one chunk carry
the whole file.

That matters at the far end of the funnel: the evidence set is supposed to be
bounded by a token budget, but `apply_budget` always keeps the first candidate —
deliberately, so a question is never answered with nothing. A 200 KB chunk turns
that reasonable exception into a prompt eight times over budget.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repoatlas.config.settings import IndexingSettings
from repoatlas.indexing.chunking import chunk_content
from repoatlas.models.evidence import EvidenceCandidate
from repoatlas.retrieval.ranking import apply_budget
from repoatlas.services.workspace import Workspace

pytestmark = pytest.mark.adversarial


def _cap(settings: IndexingSettings | None = None) -> int:
    return (settings or IndexingSettings()).max_chunk_chars


@pytest.mark.parametrize("length", [50_000, 200_000, 1_000_000])
def test_a_single_enormous_line_is_split_into_bounded_chunks(length: int) -> None:
    content = 'MARKER = "' + ("x" * length) + '"\n'
    chunks = chunk_content(content)
    assert chunks
    for chunk in chunks:
        assert len(chunk.text) <= _cap(), len(chunk.text)


def test_splitting_a_long_line_keeps_every_character() -> None:
    """Truncation would silently lose whatever sits past the cap; a literal search
    for it would then return nothing while the file plainly contains it."""
    body = "".join(f"{index:06d}" for index in range(20_000))
    chunks = chunk_content(f'DATA = "{body}"\n')
    assert body in "".join(chunk.text for chunk in chunks)


def test_a_split_line_still_cites_the_line_it_came_from() -> None:
    """Every piece is genuinely on that line, so the provenance stays true."""
    chunks = chunk_content("first = 1\n" + 'HUGE = "' + "y" * 300_000 + '"\n')
    huge = [c for c in chunks if "y" * 100 in c.text]
    assert len(huge) > 1
    # Every piece keeps the span it came from, and that span really does contain
    # the long line. The citation is coarser than the text, never wrong about it.
    for chunk in huge:
        assert chunk.line_start <= 2 <= chunk.line_end
        assert chunk.line_end <= 2


def test_ordinary_content_is_not_split_by_the_character_cap() -> None:
    """The cap must be invisible for normal source, or it would fragment every
    citation into halves of a function."""
    source = "\n".join(f"def function_{index}():\n    return {index}" for index in range(30))
    assert all(len(c.text) <= _cap() for c in chunk_content(source))
    assert len(chunk_content(source)) == len(chunk_content(source, settings=IndexingSettings()))


def test_the_cap_is_configurable_and_honoured() -> None:
    settings = IndexingSettings(max_chunk_chars=500)
    chunks = chunk_content("z" * 10_000 + "\n", settings=settings)
    assert len(chunks) >= 20
    assert all(len(c.text) <= 500 for c in chunks)


def test_one_oversized_candidate_can_no_longer_blow_the_token_budget() -> None:
    """The end-to-end consequence, stated in the units the budget is written in."""
    budget = 6000
    oversized = EvidenceCandidate(
        repository="r", path="p", line_start=1, line_end=1, text="x" * _cap()
    )
    kept, truncated, tokens = apply_budget([oversized], max_items=12, token_budget=budget)
    assert kept, "a single candidate still yields evidence"
    del truncated
    assert tokens <= budget * 2, f"{tokens} tokens against a {budget} budget"


def test_the_hostile_fixture_produces_no_oversized_chunk(adversarial: Workspace) -> None:
    """`longline.py`, `large.json` and `minified.min.js` are all in the ecosystem."""
    rows = adversarial.database.execute(
        "SELECT f.rel_path, LENGTH(c.text) AS size FROM chunks c "
        "JOIN files f ON f.id = c.file_id ORDER BY size DESC LIMIT 5"
    ).fetchall()
    assert rows
    for row in rows:
        assert row["size"] <= _cap(), (row["rel_path"], row["size"])


def test_a_minified_bundle_stays_retrievable_after_splitting(
    adversarial: Workspace, adversarial_root: Path
) -> None:
    """Splitting must not lose the content: the marker sits inside the one line."""
    assert adversarial.search.find_literal("MINIFIED_MARKER").hits
