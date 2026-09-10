"""Which lines came back, which could not, and which did not fit (Slice E).

Three facts, kept apart on purpose, because an agent's next move differs for each:

* **returned** — the complete text is in this response;
* **unavailable** — RepoAtlas cannot prove those lines from the source it used.
  Asking again changes nothing; a sync might;
* **budget_omitted** — RepoAtlas could have supplied them and did not. A larger
  budget or a narrower range returns them.

Folding the last two into one ``missing`` list would tell an agent that widening
its budget might recover a chunk gap, and that re-syncing might recover lines
there simply were too many of.

Underneath sits the source rule. One inspection reads one source: either working
tree bytes proven byte-identical to the indexed file, or the indexed chunks. A
mismatch is never a reason to prefer live data, and the two are never spliced —
a response mixing them would describe a file state that existed neither on disk
nor in the index, with every line looking equally trustworthy.
"""

from __future__ import annotations

from pathlib import Path

from repoatlas.capabilities.evidence_shaping import prefix_lines, provable_span, supply
from repoatlas.capabilities.inspection import (
    indexed_line_map,
    shape_inspection,
    to_ranges,
    worktree_line_map,
)
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import (
    AppliedBudget,
    CapabilityContext,
    CapabilityScope,
    EvidenceBudget,
    RetrievalBreadth,
)
from repoatlas.models.inspection import InspectionSource, InspectRequest, InspectTarget, LineRange
from repoatlas.services.workspace import Workspace
from repoatlas.storage.protocols import ChunkHit

SERVICE = "python-service"
CONFIG = "src/config.py"


def _service(workspace: Workspace) -> CapabilityService:
    return CapabilityService(workspace)


def _repo(slug: str = SERVICE) -> CapabilityContext:
    return CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=slug)


def _budget(**kwargs: int) -> AppliedBudget:
    defaults = {
        "max_items": 20,
        "max_total_chars": 100_000,
        "max_item_chars": 100_000,
        "breadth": RetrievalBreadth.STANDARD,
    }
    defaults.update(kwargs)  # type: ignore[arg-type]
    return AppliedBudget(**defaults)  # type: ignore[arg-type]


def _chunk(line_start: int, line_end: int, text: str, split_total: int = 1) -> ChunkHit:
    return ChunkHit(
        chunk_id=1,
        file_id=1,
        repo_id=1,
        repository="r",
        repository_slug="r",
        path="a.py",
        line_start=line_start,
        line_end=line_end,
        text=text,
        score=0.0,
        split_total=split_total,
    )


def _lines(ranges: tuple[LineRange, ...]) -> set[int]:
    return {n for r in ranges for n in range(r.start, r.end + 1)}


# ---------------------------------------------------------------------------
# Line maps
# ---------------------------------------------------------------------------


def test_a_whole_chunk_contributes_every_line_its_text_holds() -> None:
    assert indexed_line_map([_chunk(4, 6, "a\nb\nc")]) == {4: "a", 5: "b", 6: "c"}


def test_a_split_piece_contributes_nothing(initialized: Workspace) -> None:
    """A piece of a cut span begins or ends mid-line.

    Its metadata carries the whole span's range, truthfully at chunking's
    granularity, but its text covers fewer lines than that. Letting it fill the
    map would authorize citing lines nobody was shown — which is exactly the
    fabrication the supplied-span rule exists to prevent, reached by arithmetic
    rather than by invention.
    """
    del initialized
    assert indexed_line_map([_chunk(1, 3, "a\nb\nc", split_total=2)]) == {}


def test_a_chunk_whose_text_disagrees_with_its_span_contributes_nothing() -> None:
    """An entity signature stored against a multi-line declaration."""
    assert indexed_line_map([_chunk(10, 40, "def load_config() -> Config:")]) == {}


def test_two_chunks_around_a_gap_never_bridge_it() -> None:
    """The defect a line map cannot express.

    Merging by adjacent metadata would produce one citable interval 1-8 over text
    that skipped 4-5. Here the gap is simply two absent keys.
    """
    mapped = indexed_line_map([_chunk(1, 3, "a\nb\nc"), _chunk(6, 8, "f\ng\nh")])
    assert sorted(mapped) == [1, 2, 3, 6, 7, 8]
    assert to_ranges(mapped) == (LineRange(start=1, end=3), LineRange(start=6, end=8))


def test_overlapping_chunks_supply_each_line_once() -> None:
    mapped = indexed_line_map([_chunk(1, 3, "a\nb\nc"), _chunk(2, 4, "b\nc\nd")])
    assert mapped == {1: "a", 2: "b", 3: "c", 4: "d"}


def test_a_worktree_map_uses_the_shared_source_line_splitter() -> None:
    """Not ``str.splitlines()``, which breaks on characters no grammar does.

    A form feed inside a source file is an ordinary page break; counting it as a
    line ending would number the whole file one line past what every parser, chunk
    and entity span in the index agrees exists.
    """
    assert worktree_line_map("a\fb\nc") == {1: "a\fb", 2: "c"}
    assert worktree_line_map("a\r\nb\n") == {1: "a", 2: "b"}
    assert worktree_line_map("a\nb\n") == {1: "a", 2: "b"}
    assert worktree_line_map("") == {}


# ---------------------------------------------------------------------------
# The three-way partition
# ---------------------------------------------------------------------------


def test_the_three_coverage_sets_partition_the_request() -> None:
    """Every requested line is in exactly one of them. The core invariant."""
    available = {1: "a", 2: "b", 5: "e", 6: "f"}
    shaped = shape_inspection(available, LineRange(start=1, end=8), _budget())

    returned = _lines(shaped.returned)
    omitted = _lines(shaped.budget_omitted)
    unavailable = _lines(shaped.unavailable)

    assert returned | omitted | unavailable == set(range(1, 9))
    assert returned & omitted == set()
    assert returned & unavailable == set()
    assert omitted & unavailable == set()


def test_a_gap_is_unavailable_and_never_a_budget_omission() -> None:
    shaped = shape_inspection({1: "a", 4: "d"}, LineRange(start=1, end=4), _budget())
    assert _lines(shaped.unavailable) == {2, 3}
    assert shaped.budget_omitted == ()


def test_lines_that_did_not_fit_are_budget_omitted_and_never_unavailable() -> None:
    """CASE 8. Everything was provable; the budget is why the rest is missing."""
    available = {n: f"line {n:03d}" for n in range(1, 101)}
    shaped = shape_inspection(available, LineRange(start=1, end=100), _budget(max_total_chars=60))

    assert shaped.unavailable == ()
    assert _lines(shaped.returned) | _lines(shaped.budget_omitted) == set(range(1, 101))
    assert _lines(shaped.returned)
    assert min(_lines(shaped.budget_omitted)) > max(_lines(shaped.returned))


def test_a_gap_does_not_stop_the_supply_but_a_budget_does() -> None:
    """Skipping a gap is not a choice; stopping for budget is."""
    available = {1: "a", 5: "e", 9: "i"}
    shaped = shape_inspection(available, LineRange(start=1, end=9), _budget())
    assert _lines(shaped.returned) == {1, 5, 9}
    assert len(shaped.segments) == 3


def test_supply_stops_rather_than_packing_a_later_run_into_the_gap() -> None:
    """A budget must not reorder a file.

    The first run is too large to fit, and the two after it would. Admitting them
    would hand a caller reading source in line order the tail of a file before its
    head, presented as though that were the file.
    """
    available = {1: "x" * 500, 3: "y", 5: "z"}
    shaped = shape_inspection(available, LineRange(start=1, end=5), _budget(max_total_chars=10))
    assert shaped.returned == ()
    assert _lines(shaped.budget_omitted) == {1, 3, 5}
    assert _lines(shaped.unavailable) == {2, 4}


def test_max_items_caps_the_number_of_segments() -> None:
    available = {1: "a", 3: "c", 5: "e", 7: "g"}
    shaped = shape_inspection(available, LineRange(start=1, end=7), _budget(max_items=2))
    assert len(shaped.segments) == 2
    assert _lines(shaped.returned) == {1, 3}
    assert _lines(shaped.budget_omitted) == {5, 7}


def test_a_truncated_run_narrows_its_span_with_its_text() -> None:
    """The rule a citation depends on: the span never exceeds the supplied text."""
    available = {n: f"{n:03d}" for n in range(10, 21)}
    shaped = shape_inspection(available, LineRange(start=10, end=20), _budget(max_total_chars=15))

    segment = shaped.segments[0]
    assert segment.line_start == 10
    assert len(segment.text.split("\n")) == segment.line_end - segment.line_start + 1
    assert segment.line_end < 20
    assert segment.truncated


def test_a_partial_line_is_never_supplied() -> None:
    available = {1: "aaaaaaaaaa", 2: "bbbbbbbbbb"}
    shaped = shape_inspection(available, LineRange(start=1, end=2), _budget(max_total_chars=15))
    assert shaped.segments[0].text == "aaaaaaaaaa"
    assert _lines(shaped.returned) == {1}


def test_a_blank_line_costs_nothing_and_still_covers_its_line() -> None:
    """A single blank line is empty text that genuinely covers one line.

    The generic arithmetic refuses to prove a span from empty text, and it is
    right to for retrieved evidence, where empty means "nothing was stored". Here
    the line map is the proof, so a blank line does not silently end a response.
    """
    shaped = shape_inspection({7: ""}, LineRange(start=7, end=7), _budget())
    assert len(shaped.segments) == 1
    assert (shaped.segments[0].line_start, shaped.segments[0].line_end) == (7, 7)
    assert shaped.segments[0].text == ""
    assert _lines(shaped.returned) == {7}


def test_nothing_requested_outside_the_range_is_ever_returned() -> None:
    available = {n: str(n) for n in range(1, 21)}
    shaped = shape_inspection(available, LineRange(start=5, end=8), _budget())
    assert _lines(shaped.returned) == {5, 6, 7, 8}


# ---------------------------------------------------------------------------
# Source selection, end to end
# ---------------------------------------------------------------------------


def test_an_unchanged_working_tree_is_verified_and_used(initialized: Workspace) -> None:
    """CASE 3. Hash equality, not a clean-looking tree, is what admits live text."""
    response = _service(initialized).inspect(
        InspectRequest(context=_repo(), target=InspectTarget(path=CONFIG))
    )
    assert response.coverage.source is InspectionSource.VERIFIED_WORKTREE
    assert response.coverage.worktree_matches_index is True
    assert response.coverage.unavailable == ()
    assert response.file.line_count is not None


def test_an_edited_working_tree_falls_back_and_leaks_nothing(
    initialized: Workspace, workspace_root: Path
) -> None:
    """CASE 4. The most important negative in this slice.

    Not one byte of a file whose hash disagrees may appear anywhere — not as text,
    not as a line number, not as a count, not in a diagnostic. The only thing the
    caller learns is that the comparison failed.
    """
    edited = workspace_root / SERVICE / CONFIG
    indexed_text = edited.read_text(encoding="utf-8")
    marker = "A_LINE_THE_INDEX_HAS_NEVER_SEEN = 1"
    edited.write_text(indexed_text + f"\n{marker}\n", encoding="utf-8")

    response = _service(initialized).inspect(
        InspectRequest(context=_repo(), target=InspectTarget(path=CONFIG))
    )

    assert response.coverage.source is InspectionSource.INDEXED_CHUNKS
    assert response.coverage.worktree_matches_index is False
    assert marker not in response.model_dump_json()
    # And the live file's extra lines never become a length either.
    assert response.file.line_count is None
    assert response.coverage.requested is not None
    assert response.coverage.requested.end <= len(indexed_text.split("\n"))


def test_declining_the_working_tree_reads_the_index(initialized: Workspace) -> None:
    """``allow_worktree=False`` is how a caller asks what was *indexed*."""
    response = _service(initialized).inspect(
        InspectRequest(context=_repo(), target=InspectTarget(path=CONFIG), allow_worktree=False)
    )
    assert response.coverage.source is InspectionSource.INDEXED_CHUNKS
    # Never evaluated is a different fact from mismatched, and is reported as one.
    assert response.coverage.worktree_matches_index is None


def test_a_chunk_gap_is_reported_rather_than_bridged(initialized: Workspace) -> None:
    """CASE 7, on the real index.

    Blank-only spans are not stored as chunks, so a real file read from the index
    has real gaps. No returned span may cross one.
    """
    response = _service(initialized).inspect(
        InspectRequest(context=_repo(), target=InspectTarget(path=CONFIG), allow_worktree=False)
    )
    assert response.coverage.unavailable, "this fixture was expected to have chunk gaps"

    missing = _lines(response.coverage.unavailable)
    for segment in response.segments:
        assert segment.ref.line_start is not None and segment.ref.line_end is not None
        spanned = set(range(segment.ref.line_start, segment.ref.line_end + 1))
        assert not (spanned & missing), f"{segment.location} bridges a gap"


def test_one_inspection_never_mixes_two_sources(
    initialized: Workspace, workspace_root: Path
) -> None:
    """Verified lines and stale indexed lines never appear in one response.

    A composite would be a file state that never existed anywhere, and nothing in
    the response would mark which half was which.
    """
    service = _service(initialized)
    verified = service.inspect(InspectRequest(context=_repo(), target=InspectTarget(path=CONFIG)))
    assert verified.coverage.source is InspectionSource.VERIFIED_WORKTREE
    verified_text = {segment.ref.line_start: segment.text for segment in verified.segments}

    edited = workspace_root / SERVICE / CONFIG
    edited.write_text(edited.read_text(encoding="utf-8") + "\nADDED = 2\n", encoding="utf-8")

    stale = service.inspect(InspectRequest(context=_repo(), target=InspectTarget(path=CONFIG)))
    assert stale.coverage.source is InspectionSource.INDEXED_CHUNKS
    # Every segment of the second response carries the same indexed hash, which is
    # what "one snapshot" means; none of them came from the edited file.
    hashes = {segment.ref.file_content_hash for segment in stale.segments}
    assert len(hashes) == 1
    assert hashes == {stale.file.indexed_content_hash}
    assert "ADDED = 2" not in stale.model_dump_json()
    del verified_text


def test_the_returned_character_total_never_exceeds_the_budget(initialized: Workspace) -> None:
    service = _service(initialized)
    for ceiling in (1, 20, 60, 200, 5_000):
        response = service.inspect(
            InspectRequest(
                context=_repo(),
                target=InspectTarget(path=CONFIG),
                budget=EvidenceBudget(max_total_chars=ceiling),
            )
        )
        total = sum(len(segment.text) for segment in response.segments)
        assert total <= ceiling, ceiling
        assert total <= response.budget.max_total_chars


def test_every_returned_span_is_exactly_as_wide_as_its_text(initialized: Workspace) -> None:
    """Supplied-span safety, over every source and budget combination."""
    service = _service(initialized)
    for allow in (True, False):
        for ceiling in (30, 120, 100_000):
            response = service.inspect(
                InspectRequest(
                    context=_repo(),
                    target=InspectTarget(path=CONFIG),
                    allow_worktree=allow,
                    budget=EvidenceBudget(max_total_chars=ceiling),
                )
            )
            for segment in response.segments:
                assert segment.ref.line_start is not None
                assert segment.ref.line_end is not None
                claimed = segment.ref.line_end - segment.ref.line_start + 1
                assert len(segment.text.split("\n")) == claimed, segment.location


# ---------------------------------------------------------------------------
# Files with no textual coverage
# ---------------------------------------------------------------------------


def test_an_indexed_file_with_no_text_succeeds_with_truthful_coverage(
    initialized: Workspace, workspace_root: Path
) -> None:
    """Distinguishable from "not indexed", and safely so.

    The index already exposes that this file exists — it has a row, a size and a
    hash. Pretending otherwise would deny a fact RepoAtlas publishes elsewhere,
    while claiming text for it would invent one.
    """
    from repoatlas.services.sync_service import sync

    binary = workspace_root / SERVICE / "src" / "blob.bin"
    binary.write_bytes(b"\x00\x01\x02PK\x03\x04" + bytes(range(256)) * 4)
    assert initialized.root is not None
    sync(initialized, initialized.resolver().resolve(workspace_root))

    response = _service(initialized).inspect(
        InspectRequest(context=_repo(), target=InspectTarget(path="src/blob.bin"))
    )
    assert response.segments == ()
    assert response.file.path == "src/blob.bin"
    assert response.file.indexed_content_hash
    assert response.file.line_count is None
    assert response.coverage.source is InspectionSource.INDEXED_CHUNKS


def test_a_binary_file_is_never_decoded_into_lines(
    initialized: Workspace, workspace_root: Path
) -> None:
    """The indexer's verdict on these exact bytes is the verdict inspection uses.

    A file with a null byte that nonetheless decodes as UTF-8 is the case that
    matters: its worktree bytes hash equal to the index, so the hash gate passes,
    and only applying the indexer's own binary predicate stops line numbers being
    invented for something the index stored no text for.
    """
    from repoatlas.services.sync_service import sync

    sneaky = workspace_root / SERVICE / "src" / "sneaky.bin"
    sneaky.write_bytes(b"\x00import os\nSTOLEN = 1\n")
    sync(initialized, initialized.resolver().resolve(workspace_root))

    response = _service(initialized).inspect(
        InspectRequest(context=_repo(), target=InspectTarget(path="src/sneaky.bin"))
    )
    assert response.coverage.source is InspectionSource.INDEXED_CHUNKS
    # The bytes did match; they are simply not text the index holds.
    assert response.coverage.worktree_matches_index is True
    assert response.segments == ()
    assert "STOLEN" not in response.model_dump_json()


# ---------------------------------------------------------------------------
# The shared shaping helper still means what search needs it to
# ---------------------------------------------------------------------------


def test_the_shared_helper_proves_only_what_the_text_covers() -> None:
    assert provable_span("a\nb\nc", 10, 12) == (10, 12)
    assert provable_span("a\nb", 10, 12) is None
    assert provable_span("", 10, 10) is None
    assert provable_span("a", None, None) is None


def test_the_shared_helper_never_supplies_a_partial_line() -> None:
    assert prefix_lines("aaa\nbbb", 5) == ("aaa", 1)
    assert prefix_lines("aaa\nbbb", 7) == ("aaa\nbbb", 2)
    assert prefix_lines("aaaaa", 3) == ("", 0)


def test_the_shared_helper_narrows_the_span_with_the_text() -> None:
    piece = supply("a\nb\nc\nd", 10, 13, limit=3)
    assert piece is not None
    assert (piece.line_start, piece.line_end) == (10, 11)
    assert piece.text == "a\nb"
    assert piece.truncated
    assert supply("aaaaa", 1, 1, limit=2) is None
