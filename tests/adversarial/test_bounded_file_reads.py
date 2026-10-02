"""Repository content is never read into an unbounded buffer (V1_SPEC §17, ARCHITECTURE.md §12).

`max_file_bytes` was enforced on `len(raw)` *after* `raw` had been read, so the
configured safety cap capped nothing that mattered. Pointing the indexer at a
repository holding a 600 MiB file and a 5 GiB sparse file, with the cap set to
1 MiB, took peak RSS to 10.2 GiB and read 11.2 GiB — from a working tree whose
real contents occupy 635 MiB of disk.

The defect is a *shape*, not a line: a size test downstream of the allocation it
is supposed to prevent. So the fix is a shape too. `RepositoryBoundary` no longer
offers a way to read repository content without naming a limit, and the limit is
enforced twice — once from a `fstat` on the descriptor about to be read, so an
enormous file costs nothing, and once as a hard ceiling on the read itself, so a
file that grows after that `fstat` still cannot exceed it.

The tests that matter here assert **bytes read**, not merely that a marker is
absent from the database. Absence is satisfied by a build that reads the whole
file and then throws it away, which is exactly the build being fixed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from repoatlas import containment
from repoatlas.config.settings import Settings
from repoatlas.containment import ContentStatus, RepositoryBoundary
from repoatlas.services.init_service import initialize
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace
from tests.conftest import git

pytestmark = pytest.mark.adversarial

CAP = 1 << 20  # the cap every test in this file configures
MARKER = "OVERSIZE_CANARY_MARKER_P52"


# == instrumentation =========================================================


@dataclass(slots=True)
class ReadLedger:
    """Every byte `os.read` hands back, attributed to the file it came from.

    Counting at `os.read` rather than at a RepoAtlas function is deliberate: it
    is below every layer, so a reader that quietly grew a second path — or an
    `os.read(fd, st_size)` sized from untrusted metadata — is still counted. The
    ledger cannot be satisfied by discarding a buffer after allocating it.
    """

    per_file: dict[str, int] = field(default_factory=dict)
    total: int = 0
    largest_single_read: int = 0

    def bytes_for(self, name: str) -> int:
        return sum(n for path, n in self.per_file.items() if path.endswith(name))


@pytest.fixture
def ledger() -> Iterator[ReadLedger]:
    book = ReadLedger()
    real_read = os.read

    def counting(fd: int, length: int) -> bytes:
        data = real_read(fd, length)
        if data:
            try:
                target = str(Path(f"/proc/self/fd/{fd}").readlink())
            except OSError:  # pragma: no cover - fd closed under us
                target = "?"
            book.per_file[target] = book.per_file.get(target, 0) + len(data)
            book.total += len(data)
            book.largest_single_read = max(book.largest_single_read, len(data))
        return data

    os.read = counting  # type: ignore[assignment]
    try:
        yield book
    finally:
        os.read = real_read  # type: ignore[assignment]


@dataclass(slots=True)
class ContentReads:
    """Bytes that entered the **content** path, as opposed to the hash path.

    The distinction is the whole architecture. Change detection legitimately
    streams a hundred-gigabyte file in fixed blocks — bounded memory, unbounded
    content — while the content reader must never be handed more than the cap.
    A ledger over `os.read` cannot tell them apart; this one instruments
    `_read_bounded`, which only the content reader calls, so "zero content bytes
    read" is a claim about the thing that allocates.
    """

    per_file: dict[str, int] = field(default_factory=dict)
    total: int = 0
    largest_request: int = 0

    def bytes_for(self, name: str) -> int:
        return sum(n for path, n in self.per_file.items() if path.endswith(name))


@pytest.fixture
def content_reads() -> Iterator[ContentReads]:
    book = ContentReads()
    real_bounded = containment._read_bounded

    def watched(handle: int, limit: int) -> bytes:
        book.largest_request = max(book.largest_request, limit)
        data = real_bounded(handle, limit)
        try:
            target = str(Path(f"/proc/self/fd/{handle}").readlink())
        except OSError:  # pragma: no cover - fd closed under us
            target = "?"
        book.per_file[target] = book.per_file.get(target, 0) + len(data)
        book.total += len(data)
        return data

    containment._read_bounded = watched  # type: ignore[assignment]
    try:
        yield book
    finally:
        containment._read_bounded = real_bounded  # type: ignore[assignment]


def test_the_content_ledger_actually_observes_content_reads(
    tmp_path: Path, content_reads: ContentReads
) -> None:
    """Checked before it is trusted, like the byte ledger above."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "probe.py").write_bytes(b"x" * 5000)
    assert RepositoryBoundary(root).read_content("probe.py", max_bytes=CAP).ok
    assert content_reads.bytes_for("probe.py") == 5000
    assert content_reads.largest_request == CAP + 1


def test_the_content_ledger_does_not_see_the_hash_path(
    tmp_path: Path, content_reads: ContentReads, ledger: ReadLedger
) -> None:
    """The two instruments must disagree, or the distinction they exist to draw
    is not being drawn."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "streamed.py").write_bytes(b"y" * 9000)
    assert RepositoryBoundary(root).read_into("streamed.py", lambda _b: None)
    assert ledger.bytes_for("streamed.py") == 9000
    assert content_reads.bytes_for("streamed.py") == 0


def test_the_ledger_actually_observes_reads(tmp_path: Path, ledger: ReadLedger) -> None:
    """The instrument is checked before it is trusted. A ledger that counted
    nothing would make every assertion below pass for the wrong reason."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "probe.py").write_bytes(b"x" * 5000)
    assert RepositoryBoundary(root).read_content("probe.py", max_bytes=CAP).ok
    assert ledger.bytes_for("probe.py") == 5000


# == 1. the cap, pinned at the boundary ======================================


def _boundary(tmp_path: Path, name: str, payload: bytes) -> RepositoryBoundary:
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    (root / name).write_bytes(payload)
    return RepositoryBoundary(root)


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (0, ContentStatus.OK),
        (1, ContentStatus.OK),
        (CAP - 1, ContentStatus.OK),
        (CAP, ContentStatus.OK),
        (CAP + 1, ContentStatus.OVERSIZED),
        (CAP * 4, ContentStatus.OVERSIZED),
    ],
)
def test_the_cap_is_an_inclusive_maximum(
    tmp_path: Path, size: int, expected: ContentStatus
) -> None:
    """`N` is content, `N + 1` is not. Stated as a test rather than left to be
    inferred from a comparison operator, because the boundary between "indexed"
    and "invisible" is the kind of thing that gets flipped by a refactor."""
    boundary = _boundary(tmp_path, "f.bin", b"x" * size)
    found = boundary.read_content("f.bin", max_bytes=CAP)
    assert found.status is expected, (size, found.status)
    if expected is ContentStatus.OK:
        assert found.data is not None and len(found.data) == size
    else:
        assert found.data is None, "an oversized file must yield no bytes at all"


def test_an_oversized_file_yields_no_prefix(tmp_path: Path) -> None:
    """Not a truncated read. A prefix of a file that kept going is not the file,
    and facts derived from one would be true of nothing (§20)."""
    payload = MARKER.encode() + b"y" * (CAP * 2)
    boundary = _boundary(tmp_path, "big.py", payload)
    found = boundary.read_content("big.py", max_bytes=CAP)
    assert found.status is ContentStatus.OVERSIZED
    assert found.data is None
    assert found.size == len(payload)


def test_reading_an_oversized_file_costs_no_content_bytes(
    tmp_path: Path, ledger: ReadLedger
) -> None:
    """The whole point. The size comes from `fstat` on the descriptor that would
    have been read, so the bytes are never requested."""
    boundary = _boundary(tmp_path, "big.py", b"z" * (CAP * 8))
    assert boundary.read_content("big.py", max_bytes=CAP).status is ContentStatus.OVERSIZED
    assert ledger.bytes_for("big.py") == 0


def test_a_permitted_read_stays_within_one_block_of_the_cap(
    tmp_path: Path, ledger: ReadLedger
) -> None:
    """An allowed file is read in blocks, never in one allocation sized from
    `st_size`. `os.read(fd, st_size)` on an untrusted size is the same defect
    wearing a different shape."""
    boundary = _boundary(tmp_path, "atcap.bin", b"q" * CAP)
    assert boundary.read_content("atcap.bin", max_bytes=CAP).ok
    assert ledger.bytes_for("atcap.bin") == CAP
    assert ledger.largest_single_read <= (1 << 20)


@pytest.mark.parametrize("cap", [0, 1, 7])
def test_a_tiny_cap_is_honoured_exactly(tmp_path: Path, cap: int) -> None:
    """§26. A cap of zero is legal and means what it says: only an empty file has
    content within it."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "empty").write_bytes(b"")
    (root / "atcap").write_bytes(b"x" * cap)
    (root / "over").write_bytes(b"x" * (cap + 1))
    boundary = RepositoryBoundary(root)
    assert boundary.read_content("empty", max_bytes=cap).ok
    assert boundary.read_content("atcap", max_bytes=cap).ok
    assert boundary.read_content("over", max_bytes=cap).status is ContentStatus.OVERSIZED


def test_a_negative_cap_is_a_programming_error_not_a_disabled_limit(tmp_path: Path) -> None:
    """§27. The failure mode worth refusing is a limit that silently stops being
    one. Configuration cannot express this — the field is bounded at zero — so
    reaching it means a caller computed it, and the loud answer is the safe one."""
    boundary = _boundary(tmp_path, "f.py", b"x")
    with pytest.raises(ValueError, match="negative"):
        boundary.read_content("f.py", max_bytes=-1)


def test_the_boundary_offers_no_unbounded_content_read(tmp_path: Path) -> None:
    """§29. The design property, asserted rather than trusted: there is exactly
    one way to obtain repository content and it cannot be called without a limit.
    A second, laxer primitive is how this defect comes back."""
    import inspect

    boundary = RepositoryBoundary(tmp_path)
    public = {name for name in dir(boundary) if not name.startswith("_")}
    assert public == {"open", "read_content", "read_into", "readlink", "refused", "root", "stat"}
    assert not hasattr(boundary, "read_bytes")

    signature = inspect.signature(RepositoryBoundary.read_content)
    limit = signature.parameters["max_bytes"]
    assert limit.kind is inspect.Parameter.KEYWORD_ONLY
    assert limit.default is inspect.Parameter.empty, "the limit must not have a default"


# == 2. the races: the size that decided is not the size that arrives ========


def test_a_file_that_grows_past_the_cap_during_the_read_is_stopped_by_the_ceiling(
    tmp_path: Path, ledger: ReadLedger
) -> None:
    """§7, and the reason a pre-read `stat` is not a fix.

    `st_size` says the file fits, so the read begins. Then it grows. Without a
    ceiling on the read itself the loop runs to EOF and the cap is defeated by a
    file that was small at exactly the moment it was measured.
    """
    root = tmp_path / "repo"
    root.mkdir()
    target = root / "grower.bin"
    target.write_bytes(b"a" * 4096)

    real_read = os.read
    grown = {"done": False}

    def growing(fd: int, length: int) -> bytes:
        block = real_read(fd, length)
        if block and not grown["done"]:
            grown["done"] = True
            with target.open("ab") as handle:
                handle.write(b"b" * (CAP * 4))
        return block

    boundary = RepositoryBoundary(root)
    os.read = growing  # type: ignore[assignment]
    try:
        found = boundary.read_content("grower.bin", max_bytes=CAP)
    finally:
        os.read = real_read  # type: ignore[assignment]

    assert grown["done"], "the race must actually have been simulated"
    assert target.stat().st_size > CAP, "the file must really have grown past the cap"
    assert found.data is None, "a file that outgrew the cap must yield no bytes"
    assert found.status in (ContentStatus.OVERSIZED, ContentStatus.CHANGED), found.status
    # And — the part a post-read mutation check cannot give you — the bytes were
    # never held in the first place. Detecting the change *after* buffering four
    # megabytes is a correct answer arrived at by doing the forbidden thing.
    #
    # Measured with the `os.read` ledger rather than the content ledger, because
    # this assertion has to hold for *any* implementation of the reader, including
    # one that stopped routing through the bounded helper.
    read = ledger.bytes_for("grower.bin")
    assert read <= CAP + 1 + (1 << 20), read
    assert read < target.stat().st_size


def test_the_ceiling_holds_when_the_reported_size_is_a_lie(
    tmp_path: Path, ledger: ReadLedger
) -> None:
    """The ceiling must not depend on `st_size` being honest. Here `fstat` is made
    to report a small file while the file on disk is large — a stand-in for a
    filesystem with lazy metadata, and for a repository that arranged both."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "liar.bin").write_bytes(b"L" * (CAP * 6))

    boundary = RepositoryBoundary(root)
    real_fstat = os.fstat

    class SmallStat:
        def __init__(self, real: os.stat_result) -> None:
            self._real = real
            self.st_size = 16

        def __getattr__(self, name: str) -> object:
            return getattr(self._real, name)

    def lying_fstat(fd: int) -> object:
        return SmallStat(real_fstat(fd))

    os.fstat = lying_fstat  # type: ignore[assignment]
    try:
        found = boundary.read_content("liar.bin", max_bytes=CAP)
    finally:
        os.fstat = real_fstat  # type: ignore[assignment]

    assert found.status is ContentStatus.OVERSIZED, found.status
    assert found.data is None
    # The read stopped at the ceiling, not at end of file.
    assert ledger.bytes_for("liar.bin") <= CAP + 1 + (1 << 20)
    assert ledger.bytes_for("liar.bin") < CAP * 6


def test_a_file_shrinking_below_the_cap_is_refused_this_run_not_mis_indexed(
    tmp_path: Path,
) -> None:
    """§12. The pre-read `fstat` says oversized, so nothing is read — and that
    answer stands for this run even though the file is now small. Either policy is
    acceptable provided the next sync converges, which the transition tests below
    prove. What is not acceptable is admitting a size that was never observed."""
    root = tmp_path / "repo"
    root.mkdir()
    target = root / "shrinker.bin"
    target.write_bytes(b"s" * (CAP * 3))
    boundary = RepositoryBoundary(root)

    found = boundary.read_content("shrinker.bin", max_bytes=CAP)
    assert found.status is ContentStatus.OVERSIZED

    target.write_bytes(b"s" * 32)
    settled = boundary.read_content("shrinker.bin", max_bytes=CAP)
    assert settled.ok and settled.data == b"s" * 32


def test_a_small_file_replaced_by_a_huge_one_cannot_bypass_the_bound(
    tmp_path: Path, ledger: ReadLedger
) -> None:
    """§13. The cross-check between containment and this pass: the route stays
    valid, the inode does not. Whatever is on the other end of the descriptor, the
    ceiling is what decides how much of it is allocated."""
    root = tmp_path / "repo"
    root.mkdir()
    target = root / "swap.bin"
    target.write_bytes(b"tiny\n")

    huge = tmp_path / "staged.bin"
    huge.write_bytes(b"H" * (CAP * 5))

    boundary = RepositoryBoundary(root)
    real_open = os.open
    swapped = {"done": False}

    def swapping(*args: object, **kwargs: object) -> int:
        # Replace the file after the boundary has resolved its directory but
        # before it opens the leaf, so the descriptor lands on the new inode.
        if not swapped["done"] and args and args[0] == "swap.bin":
            swapped["done"] = True
            huge.replace(target)
        return real_open(*args, **kwargs)  # type: ignore[arg-type]

    os.open = swapping  # type: ignore[assignment]
    try:
        found = boundary.read_content("swap.bin", max_bytes=CAP)
    finally:
        os.open = real_open  # type: ignore[assignment]

    assert swapped["done"], "the replacement must actually have happened"
    assert found.data is None, found
    assert ledger.bytes_for("swap.bin") <= CAP + 1 + (1 << 20)


# == 3. enormous logical size, cheaply ======================================


def _sparse(path: Path, size: int) -> None:
    """A file that is enormous logically and empty physically.

    The best regression in this file: it makes "did you read the whole thing?"
    answerable in milliseconds and on any disk. Never write to it — materializing
    it would turn a fast test into a slow one and a full filesystem.
    """
    with path.open("wb") as handle:
        handle.truncate(size)


def test_a_five_gigabyte_sparse_file_costs_one_fstat(tmp_path: Path, ledger: ReadLedger) -> None:
    """§14. Five gigabytes of logical size, zero blocks on disk, zero bytes read."""
    root = tmp_path / "repo"
    root.mkdir()
    target = root / "sparse.bin"
    _sparse(target, 5 << 30)
    assert target.stat().st_size == 5 << 30
    assert target.stat().st_blocks * 512 < (1 << 20), "the fixture must stay sparse"

    found = RepositoryBoundary(root).read_content("sparse.bin", max_bytes=CAP)
    assert found.status is ContentStatus.OVERSIZED
    assert found.size == 5 << 30
    assert ledger.bytes_for("sparse.bin") == 0


def test_the_sparse_fixture_is_not_materialized_by_the_test_itself(tmp_path: Path) -> None:
    """The instrument for the instrument. A `_sparse` that silently wrote zeroes
    would make the test above measure nothing while still passing."""
    target = tmp_path / "probe.bin"
    _sparse(target, 2 << 30)
    assert target.stat().st_size == 2 << 30
    assert target.stat().st_blocks == 0


# == 4. containment still decides first =====================================


def test_the_size_bound_never_admits_what_containment_refuses(tmp_path: Path) -> None:
    """§15. The order is: prove the object is ours, then bound how much of it we
    read. A size check that opened the file itself would be a second, weaker
    admission path — the exact shape the containment pass removed."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_bytes(b"OUTSIDE = 1\n")

    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "ok.py").write_bytes(b"A = 1\n")
    (root / "link").symlink_to(outside)

    boundary = RepositoryBoundary(root)
    for escape in ("link/secret.py", "../outside/secret.py", "/etc/passwd", ""):
        found = boundary.read_content(escape, max_bytes=CAP)
        assert found.status is ContentStatus.REFUSED, (escape, found.status)
        assert found.data is None
    assert boundary.read_content("src/ok.py", max_bytes=CAP).data == b"A = 1\n"


def test_a_symlink_to_a_large_outside_file_is_refused_before_its_size_matters(
    tmp_path: Path, ledger: ReadLedger
) -> None:
    """§31, the cross-check the containment pass earns here.

    A route out of the tree pointing at something enormous is the case where the
    two boundaries could plausibly be confused: refusing it "because it is too
    large" would be the right outcome for the wrong reason, and would quietly
    admit the same file the moment somebody raised the cap. Containment decides
    first, and the size never enters the argument.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    huge = outside / "host.bin"
    _sparse(huge, 4 << 30)
    small = outside / "creds.txt"
    small.write_text("OUTSIDE_CANARY = 1\n", encoding="utf-8")

    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_bytes(b"A = 1\n")
    (root / "src" / "linked_huge").symlink_to(huge)
    (root / "escape").symlink_to(outside)

    boundary = RepositoryBoundary(root)
    for route in ("src/linked_huge", "escape/host.bin", "escape/creds.txt"):
        found = boundary.read_content(route, max_bytes=CAP)
        assert found.status is ContentStatus.REFUSED, (route, found.status)
        # Not OVERSIZED: the size of a file we may not reach is not our answer.
        assert found.size is None, route
    # Raising the cap past the outside file changes nothing.
    assert boundary.read_content("escape/creds.txt", max_bytes=1 << 30).status is (
        ContentStatus.REFUSED
    )
    assert ledger.bytes_for("host.bin") == 0
    assert ledger.bytes_for("creds.txt") == 0
    assert boundary.read_content("src/app.py", max_bytes=CAP).data == b"A = 1\n"


def test_a_fifo_is_refused_rather_than_read_under_a_cap(tmp_path: Path) -> None:
    """A cap makes an unbounded read finite, which is exactly what would make it
    tempting to relax the regular-file rule. A FIFO is still not a file."""
    root = tmp_path / "repo"
    root.mkdir()
    os.mkfifo(root / "pipe")
    found = RepositoryBoundary(root).read_content("pipe", max_bytes=CAP)
    assert found.status is ContentStatus.REFUSED
    assert found.data is None


def test_a_hardlinked_file_is_still_refused(tmp_path: Path) -> None:
    """The containment policy and the size bound compose; neither weakens the
    other."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "real.py").write_bytes(b"R = 1\n")
    os.link(root / "real.py", root / "alias.py")
    boundary = RepositoryBoundary(root)
    assert boundary.read_content("real.py", max_bytes=CAP).status is ContentStatus.REFUSED
    assert boundary.read_content("alias.py", max_bytes=CAP).status is ContentStatus.REFUSED


# == 5. through the real indexing path ======================================


def _settings(tmp_path: Path, cap: int = CAP, tag: str = "state") -> Settings:
    return Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / tag / "atlas.db")},
            "indexing": {"max_file_bytes": cap},
        }
    )


def _repo(root: Path, name: str = "svc") -> Path:
    repo = root / name
    repo.mkdir(parents=True, exist_ok=True)
    return repo


def _commit(repo: Path) -> None:
    if not (repo / ".git").exists():
        git(repo, "init", "--quiet", "--initial-branch=main")
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "--allow-empty", "-m", "state")


def _indexed(tmp_path: Path, root: Path, cap: int = CAP) -> Workspace:
    space = open_workspace(settings=_settings(tmp_path, cap), require_root=False)
    initialize(space, root, config_path=tmp_path / "config.yaml")
    return space


def _resync(space: Workspace, root: Path, name: str = "svc") -> None:
    sync(space, space.resolver().resolve(root / name))


def _files(space: Workspace) -> dict[str, dict[str, Any]]:
    rows = space.database.execute(
        "SELECT f.rel_path, f.size, f.is_binary, f.parser, f.content_hash, "
        "  (SELECT COUNT(*) FROM chunks c WHERE c.file_id = f.id) AS chunks, "
        "  (SELECT COUNT(*) FROM entities e WHERE e.file_id = f.id) AS entities "
        "FROM files f"
    ).fetchall()
    return {str(r["rel_path"]): dict(r) for r in rows}


@pytest.fixture
def mixed_repo(tmp_path: Path) -> Path:
    """One repository holding every size that matters, plus a sparse monster."""
    root = tmp_path / "repos"
    repo = _repo(root)
    (repo / "small.py").write_text(f"SMALL = '{MARKER}_SMALL'\n", encoding="utf-8")
    (repo / "empty.py").write_text("", encoding="utf-8")
    (repo / "onebyte.py").write_bytes(b"x")
    for name, size in (("under.py", CAP - 1), ("exact.py", CAP), ("over.py", CAP + 1)):
        head = f"TAG = '{MARKER}_{name.split('.')[0].upper()}'\n".encode()
        (repo / name).write_bytes(head + b"#" * (size - len(head)))
        assert (repo / name).stat().st_size == size
    # Modest on purpose. Every tracked file is streamed once by the git
    # working-tree comparison whatever its size (a bounded-memory hash path,
    # see §9), so a five-gigabyte fixture here would buy nothing over the
    # boundary-level test above and cost the suite minutes of I/O.
    _sparse(repo / "sparse.bin", 64 << 20)
    (repo / "deep" / "a" / "b").mkdir(parents=True)
    _sparse(repo / "deep" / "a" / "b" / "buried.bin", 32 << 20)
    _commit(repo)
    return root


def test_a_real_sync_reads_no_oversized_content(
    tmp_path: Path, mixed_repo: Path, content_reads: ContentReads
) -> None:
    """The original reproduction as a permanent test, measured where it matters:
    every file past the cap contributes zero bytes to the content path, whether it
    is one byte over or ninety-six megabytes over, and whether it sits at the root
    or four directories down."""
    space = _indexed(tmp_path, mixed_repo)
    try:
        for name in ("sparse.bin", "buried.bin", "over.py"):
            assert content_reads.bytes_for(name) == 0, name
        # ...while everything at or below the cap is read exactly once.
        assert content_reads.bytes_for("exact.py") == CAP
        assert content_reads.bytes_for("under.py") == CAP - 1
        assert content_reads.bytes_for("small.py") == len(
            (mixed_repo / "svc" / "small.py").read_bytes()
        )
        # No single request ever exceeds the ceiling, however large the file.
        assert content_reads.largest_request == CAP + 1
    finally:
        space.close()


def test_the_cap_boundary_decides_what_becomes_content(tmp_path: Path, mixed_repo: Path) -> None:
    """§3 and §16 end to end: which files have chunks, and which are known only
    as paths."""
    space = _indexed(tmp_path, mixed_repo)
    try:
        rows = _files(space)
        for name in ("small.py", "onebyte.py", "under.py", "exact.py"):
            assert rows[name]["chunks"] > 0, name
            assert rows[name]["parser"] == "python-ast", name
        for name in ("over.py", "sparse.bin", "deep/a/b/buried.bin"):
            assert rows[name]["chunks"] == 0, name
            assert rows[name]["parser"] is None, name
            # Known to exist — deletion tracking needs the row — but only as a path.
            assert rows[name]["entities"] == 1, name
        assert rows["empty.py"]["chunks"] == 0
    finally:
        space.close()


def test_no_oversized_content_reaches_search_or_chunks(tmp_path: Path, mixed_repo: Path) -> None:
    space = _indexed(tmp_path, mixed_repo)
    try:
        assert space.search.search_literal(f"{MARKER}_OVER") == []
        assert space.search.search_literal(f"{MARKER}_SMALL") != []
        assert space.search.search_literal(f"{MARKER}_EXACT") != []
        texts = [
            str(r["text"]) for r in space.database.execute("SELECT text FROM chunks").fetchall()
        ]
        assert not any(f"{MARKER}_OVER" in t for t in texts)
    finally:
        space.close()


def test_an_oversized_file_still_gets_a_real_content_hash(tmp_path: Path, mixed_repo: Path) -> None:
    """§9. The row's hash is the planner's sole authority on whether a file
    changed, and the planner computes it by streaming. Storing anything else —
    a metadata digest, a constant — would make every forced rehash conclude the
    file had changed, forever."""
    import hashlib

    space = _indexed(tmp_path, mixed_repo)
    try:
        stored = _files(space)["over.py"]["content_hash"]
    finally:
        space.close()
    expected = hashlib.blake2b(
        (mixed_repo / "svc" / "over.py").read_bytes(), digest_size=32
    ).hexdigest()
    assert stored == expected


# == 6. transitions: what a file was must not outlive what it is =============


STALE = "OVERSIZE_STALE_SECRET_P52"


def test_a_file_that_becomes_oversized_loses_every_fact_it_asserted(
    tmp_path: Path, content_reads: ContentReads
) -> None:
    """§17, the transition that matters most.

    A file is indexed while small, so its contents are entities, edges, chunks
    and FTS rows. It then grows past the cap.
    "Skipped for size" must not quietly mean "kept whatever we had", or the index
    would keep serving content from a file RepoAtlas has decided it may not read.
    """
    root = tmp_path / "repos"
    repo = _repo(root)
    (repo / "app.py").write_text(
        f"SECRET = '{STALE}'\n\n\ndef connect():\n    return SECRET\n", encoding="utf-8"
    )
    _commit(repo)

    space = _indexed(tmp_path, root)
    try:
        assert space.search.search_literal(STALE) != []
        before = _files(space)["app.py"]
        assert before["chunks"] > 0 and before["entities"] > 1

        texts = [
            str(r["text"]) for r in space.database.execute("SELECT text FROM chunks").fetchall()
        ]
        assert texts, "there must be chunks to lose"

        (repo / "app.py").write_bytes(f"SECRET = '{STALE}'\n".encode() + b"#" * (CAP * 2))
        _commit(repo)
        _resync(space, root)

        after = _files(space)["app.py"]
        assert after["chunks"] == 0, "chunks from the small version must be gone"
        assert after["entities"] == 1, "only the file-identity entity may remain"
        assert after["parser"] is None
        assert space.search.search_literal(STALE) == [], "FTS must not still serve it"
        assert (
            space.database.execute(
                "SELECT COUNT(*) AS n FROM chunks WHERE text IN "  # noqa: S608
                f"({','.join('?' * len(texts))})",
                tuple(texts),
            ).fetchone()["n"]
            == 0
        ), "no chunk of the small version may survive"
        assert content_reads.bytes_for("app.py") == len(
            f"SECRET = '{STALE}'\n\n\ndef connect():\n    return SECRET\n".encode()
        ), "the oversized version must contribute nothing to the content path"
    finally:
        space.close()


def test_an_oversized_file_that_shrinks_becomes_indexable(tmp_path: Path) -> None:
    """The reverse transition. A file refused for size is refused for *now*, not
    forever: the cap is a policy about the current bytes, not a quarantine."""
    root = tmp_path / "repos"
    repo = _repo(root)
    (repo / "app.py").write_bytes(b"#" * (CAP * 2))
    _commit(repo)

    space = _indexed(tmp_path, root)
    try:
        assert _files(space)["app.py"]["chunks"] == 0

        (repo / "app.py").write_text(f"VALUE = '{MARKER}_SHRUNK'\n", encoding="utf-8")
        _commit(repo)
        _resync(space, root)

        row = _files(space)["app.py"]
        assert row["chunks"] > 0
        assert row["parser"] == "python-ast"
        assert space.search.search_literal(f"{MARKER}_SHRUNK") != []
    finally:
        space.close()


def test_a_file_that_grows_past_the_cap_between_scan_and_read(
    tmp_path: Path, content_reads: ContentReads
) -> None:
    """§11 end to end. The scanner records a size that permits the file; by the
    time the indexer opens it, it does not. The reader's own `fstat` — taken on
    the descriptor it will read, not on the path the scanner saw — is what makes
    the stale size harmless."""
    root = tmp_path / "repos"
    repo = _repo(root)
    (repo / "grower.py").write_text(f"TAG = '{MARKER}_GROWER'\n", encoding="utf-8")
    (repo / "other.py").write_text("OTHER = 1\n", encoding="utf-8")
    _commit(repo)

    space = open_workspace(settings=_settings(tmp_path), require_root=False)
    grown = {"done": False}
    real_scan = containment.RepositoryBoundary.read_content

    def growing(self, rel_path: str, *, max_bytes: int):
        if rel_path == "grower.py" and not grown["done"]:
            grown["done"] = True
            with (repo / "grower.py").open("ab") as handle:
                handle.write(b"#" * (CAP * 2))
        return real_scan(self, rel_path, max_bytes=max_bytes)

    containment.RepositoryBoundary.read_content = growing  # type: ignore[assignment]
    try:
        initialize(space, root, config_path=tmp_path / "config.yaml")
    finally:
        containment.RepositoryBoundary.read_content = real_scan  # type: ignore[assignment]

    try:
        assert grown["done"], "the race must actually have been simulated"
        assert (repo / "grower.py").stat().st_size > CAP
        assert content_reads.bytes_for("grower.py") == 0
        assert _files(space)["grower.py"]["chunks"] == 0
        assert space.search.search_literal(f"{MARKER}_GROWER") == []
        # The rest of the repository is unaffected: this is a per-file decision.
        assert _files(space)["other.py"]["chunks"] > 0
    finally:
        space.close()


# == 7. nothing oversized reaches the model =================================


def test_no_oversized_content_reaches_retrieved_evidence(tmp_path: Path) -> None:
    """§23. The question names the marker directly, which is as hard as a user can
    push retrieval at it. Knowing the *path* exists is permitted by the current
    model; carrying the file's bytes into the evidence is not."""
    from repoatlas.services.retrieval_service import locate

    root = tmp_path / "repos"
    repo = _repo(root)
    (repo / "notes.py").write_text("NOTE = 1\n", encoding="utf-8")
    (repo / "big.py").write_bytes(f"HUGE = '{MARKER}_PROMPT'\n".encode() + b"#" * (CAP * 2))
    _commit(repo)

    space = _indexed(tmp_path, root)
    try:
        context = space.resolver().resolve(root / "svc")
        evidence = locate(space, f"What is {MARKER}_PROMPT in big.py?", context)
        supplied = "\n".join(candidate.text for candidate in evidence.candidates)
        assert f"{MARKER}_PROMPT" not in supplied
    finally:
        space.close()


def test_an_oversized_file_produces_no_partial_facts(tmp_path: Path) -> None:
    """§20. The failure this guards against is subtle: read the first `cap` bytes,
    decode them, and index the result as though the file ended there. Every fact
    so produced is true of a file that does not exist."""
    root = tmp_path / "repos"
    repo = _repo(root)
    body = "def visible_head():\n    return 1\n\n\n" + "# " + "p" * 200 + "\n" * 1
    (repo / "big.py").write_bytes(body.encode() + b"#" * (CAP * 2))
    _commit(repo)

    space = _indexed(tmp_path, root)
    try:
        names = {
            str(r["name"])
            for r in space.database.execute(
                "SELECT e.name FROM entities e JOIN files f ON f.id = e.file_id "
                "WHERE f.rel_path = 'big.py'"
            ).fetchall()
        }
        assert names == {"big.py"}, names
        assert "visible_head" not in names
        assert space.search.search_literal("visible_head") == []
    finally:
        space.close()


def test_a_large_unicode_file_is_refused_without_a_decode_attempt(tmp_path: Path) -> None:
    """A cap counts bytes, and a decode boundary is where a truncated multi-byte
    sequence would surface. Neither may happen: the file is refused before any of
    it is turned into text."""
    root = tmp_path / "repos"
    repo = _repo(root)
    # Four-byte code points, so any byte-position truncation lands mid-sequence.
    (repo / "uni.py").write_text("X = '" + ("\U0001f600" * (CAP // 2)) + "'\n", encoding="utf-8")
    assert (repo / "uni.py").stat().st_size > CAP
    (repo / "small_uni.py").write_text(f"Y = 'créer_{MARKER}_UNI'\n", encoding="utf-8")
    _commit(repo)

    space = _indexed(tmp_path, root)
    try:
        rows = _files(space)
        assert rows["uni.py"]["chunks"] == 0
        assert rows["uni.py"]["is_binary"] == 0
        # Unicode indexing is unaffected for files within the cap (Pass 3).
        assert rows["small_uni.py"]["chunks"] > 0
        assert space.search.search_literal(f"créer_{MARKER}_UNI") != []
    finally:
        space.close()


def test_a_large_binary_file_is_flagged_binary_without_being_buffered(
    tmp_path: Path, content_reads: ContentReads
) -> None:
    """§21. The `is_binary` flag survives the fix. It is taken from the 8 KiB
    prefix that passes through the change-detection hash anyway, so a huge binary
    is still recorded truthfully at no cost in retained memory."""
    root = tmp_path / "repos"
    repo = _repo(root)
    (repo / "blob.bin").write_bytes(b"\x00\x01\x02\x03" * (CAP // 2))
    (repo / "small.bin").write_bytes(b"\x00\x01\x02\x03" * 8)
    _commit(repo)

    space = _indexed(tmp_path, root)
    try:
        rows = _files(space)
        assert rows["blob.bin"]["is_binary"] == 1
        assert rows["blob.bin"]["chunks"] == 0
        assert rows["small.bin"]["is_binary"] == 1
        assert content_reads.bytes_for("blob.bin") == 0
    finally:
        space.close()


# == 8. the surrounding guarantees still hold ===============================


def test_status_is_current_when_a_file_is_intentionally_skipped(tmp_path: Path) -> None:
    """§18. "Current" means current *relative to the configured indexing policy*,
    not "every byte in the repository is indexed". A cap that permanently marked
    the workspace stale would make freshness meaningless — the condition would
    never clear, and there is nothing for a user to do about it."""
    from repoatlas.services.status_service import status

    root = tmp_path / "repos"
    repo = _repo(root)
    (repo / "app.py").write_text("A = 1\n", encoding="utf-8")
    (repo / "big.bin").write_bytes(b"#" * (CAP * 2))
    _commit(repo)

    space = _indexed(tmp_path, root)
    try:
        report = status(space, space.resolver().resolve(root / "svc"))
        assert report.index_state == "current", report.index_state
    finally:
        space.close()


def test_the_operator_is_told_which_file_was_skipped_and_why(tmp_path: Path) -> None:
    """§24. A file silently missing from the index is a support ticket. The
    diagnostic names the repository-relative path, the observed size and the
    configured limit — and nothing else: no absolute host path, no content."""
    root = tmp_path / "repos"
    repo = _repo(root)
    (repo / "big.py").write_bytes(f"TAG = '{MARKER}_DIAG'\n".encode() + b"#" * (CAP * 2))
    _commit(repo)

    space = open_workspace(settings=_settings(tmp_path), require_root=False)
    try:
        report, _ = initialize(space, root, config_path=tmp_path / "config.yaml")
        messages = [d.message for r in report.repositories for d in r.diagnostics]
        skipped = [m for m in messages if "exceeds" in m]
        assert skipped, messages
        assert str(CAP) in skipped[0]
        assert f"{MARKER}_DIAG" not in skipped[0], "a diagnostic must not quote content"
        assert str(tmp_path) not in skipped[0], "no absolute host path"
    finally:
        space.close()


def test_an_incremental_sync_across_the_cap_matches_a_full_reindex(tmp_path: Path) -> None:
    """§32.20. The equivalence invariant (ARCHITECTURE.md §9) has to survive a
    file crossing the cap in both directions — that is where an incremental run
    could plausibly retain something a fresh index would never create."""
    from tests.conftest import fact_snapshot, reindex_from_scratch

    root = tmp_path / "repos"
    repo = _repo(root)
    (repo / "keep.py").write_text("KEEP = 1\n", encoding="utf-8")
    (repo / "swing.py").write_text("def small():\n    return 1\n", encoding="utf-8")
    _commit(repo)

    settings = _settings(tmp_path)
    space = open_workspace(settings=settings, require_root=False)
    try:
        initialize(space, root, config_path=tmp_path / "config.yaml")

        (repo / "swing.py").write_bytes(b"#" * (CAP * 2))
        _commit(repo)
        _resync(space, root)

        (repo / "swing.py").write_text("def back_again():\n    return 2\n", encoding="utf-8")
        _commit(repo)
        _resync(space, root)

        incremental = fact_snapshot(space)
    finally:
        space.close()

    assert incremental == reindex_from_scratch(settings, root, tmp_path, tag="fresh")


def test_an_untracked_oversized_file_is_bounded_too(tmp_path: Path) -> None:
    """`git ls-files` reports untracked-not-ignored files, so they are indexed —
    and an attacker who can write into a working tree need not commit."""
    root = tmp_path / "repos"
    repo = _repo(root)
    (repo / "tracked.py").write_text("T = 1\n", encoding="utf-8")
    _commit(repo)
    (repo / "untracked_big.py").write_bytes(
        f"TAG = '{MARKER}_UNTRACKED'\n".encode() + b"#" * (CAP * 2)
    )

    space = _indexed(tmp_path, root)
    try:
        rows = _files(space)
        assert "untracked_big.py" in rows, "untracked files are in scope"
        assert rows["untracked_big.py"]["chunks"] == 0
        assert space.search.search_literal(f"{MARKER}_UNTRACKED") == []
    finally:
        space.close()


def test_an_enormous_repoatlasignore_cannot_exhaust_memory_before_the_cap_applies(
    tmp_path: Path, content_reads: ContentReads
) -> None:
    """The other unbounded reader, found by auditing for the same shape.

    `.repoatlasignore` is repository-controlled content read at the very start of
    a scan — before any file has been classified, and before `max_file_bytes` has
    been consulted about anything. An unbounded read there exhausts memory ahead
    of every protection this pass adds to the path below it.
    """
    from repoatlas.discovery.ignore import MAX_IGNORE_BYTES

    root = tmp_path / "repos"
    repo = _repo(root)
    (repo / "app.py").write_text(f"A = '{MARKER}_IGNORE_OK'\n", encoding="utf-8")
    (repo / ".repoatlasignore").write_bytes(b"# " + b"z" * (MAX_IGNORE_BYTES * 2))
    _commit(repo)

    space = _indexed(tmp_path, root)
    try:
        assert content_reads.bytes_for(".repoatlasignore") == 0
        # An unreadable rule file means no rules, never partial rules: the rest of
        # the repository is still indexed.
        assert space.search.search_literal(f"{MARKER}_IGNORE_OK") != []
    finally:
        space.close()


def test_a_normal_repoatlasignore_still_works(tmp_path: Path) -> None:
    """The control. A bound that broke ignore handling would satisfy the test
    above and quietly index everything a user asked to exclude."""
    root = tmp_path / "repos"
    repo = _repo(root)
    (repo / "app.py").write_text(f"A = '{MARKER}_KEPT'\n", encoding="utf-8")
    (repo / "vendor.py").write_text(f"V = '{MARKER}_EXCLUDED'\n", encoding="utf-8")
    (repo / ".repoatlasignore").write_text("vendor.py\n", encoding="utf-8")
    _commit(repo)

    space = _indexed(tmp_path, root)
    try:
        assert space.search.search_literal(f"{MARKER}_KEPT") != []
        assert space.search.search_literal(f"{MARKER}_EXCLUDED") == []
        assert "vendor.py" not in _files(space)
    finally:
        space.close()


# == 9. runtime memory, measured rather than argued =========================


_RSS_PROBE = """
import json, os, resource, sys
from pathlib import Path
from repoatlas.config.settings import Settings
from repoatlas.services.init_service import initialize
from repoatlas.services.workspace import open_workspace

root, state, cap = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
baseline = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
settings = Settings.model_validate({
    "storage": {"database": str(state / "atlas.db")},
    "indexing": {"max_file_bytes": cap},
})
space = open_workspace(settings=settings, require_root=False)
initialize(space, root, config_path=state / "config.yaml")
peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
chunks = space.database.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
space.close()
print(json.dumps({"baseline_kib": baseline, "peak_kib": peak, "chunks": chunks}))
"""


@pytest.mark.slow
def test_peak_memory_does_not_scale_with_the_size_of_a_refused_file(tmp_path: Path) -> None:
    """§25.B. The structural tests count bytes; this one watches the process.

    Two runs of the same code over the same repository, differing only in whether
    a 256 MiB file is present. Before the fix the difference was the file; after
    it, the difference is noise. Reported as a delta over each run's own baseline,
    because absolute RSS is mostly the interpreter.
    """
    root = tmp_path / "repos"
    repo = _repo(root)
    (repo / "app.py").write_text("A = 1\n", encoding="utf-8")
    _commit(repo)

    probe = tmp_path / "probe.py"
    probe.write_text(_RSS_PROBE, encoding="utf-8")

    def run(tag: str) -> dict[str, int]:
        state = tmp_path / tag
        state.mkdir(exist_ok=True)
        completed = subprocess.run(
            [sys.executable, str(probe), str(root), str(state), str(CAP)],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).parent.parent.parent,
        )
        return json.loads(completed.stdout.strip().splitlines()[-1])

    without = run("without")

    big = repo / "huge.bin"
    with big.open("wb") as handle:
        block = b"# padding for a large file\n" * 4096
        for _ in range((256 << 20) // len(block)):
            handle.write(block)
    assert big.stat().st_size > (200 << 20)
    _commit(repo)

    with_big = run("with")

    without_delta = (without["peak_kib"] - without["baseline_kib"]) / 1024
    with_delta = (with_big["peak_kib"] - with_big["baseline_kib"]) / 1024
    growth = with_delta - without_delta

    assert with_big["chunks"] == without["chunks"], "the huge file must add no chunks"
    assert growth < 32, (
        f"peak RSS grew {growth:.1f} MiB when a {big.stat().st_size / (1 << 20):.0f} MiB "
        f"file was added ({without_delta:.1f} -> {with_delta:.1f} MiB over baseline)"
    )


# == 10. the audit, as a test rather than a one-off sweep ===================


#: Every module that may read from a file descriptor, and the reason it is safe.
#: A new entry here is a deliberate act; an unlisted one fails the test below.
READERS = {
    "repoatlas/containment.py": "the bounded content reader and the streaming hash reader",
    "repoatlas/gitstate/client.py": "streams a tracked file into a git blob hash, in blocks",
    "repoatlas/discovery/ignore.py": "the workspace-root ignore file, under its own ceiling",
    "repoatlas/config/settings.py": "the operator's own configuration file",
    "repoatlas/storage/sqlite/migrator.py": "migration SQL shipped inside the package",
    "repoatlas/llm/prompts/loader.py": "prompt templates shipped inside the package",
    "repoatlas/sync/lock.py": "RepoAtlas's own writer-lock file",
    # Opens no path at all: it wraps an already-open descriptor (the process's own
    # stdout) so the MCP protocol can write to it, and reads from it never. It is
    # listed because the sweep below cannot tell a descriptor from a path, and
    # because `CapabilityService.open` reads as `.open` to a name-based check.
    "repoatlas/mcp/server.py": "wraps the process's own stdout descriptor; writes only",
    # Writes its connection file through the descriptor `mkstemp` returns, and reads
    # it back under `CONNECTION_FILE_CEILING` before removing it on exit. Its
    # `CapabilityService.open` reads as `.open`, as in the MCP server.
    "repoatlas/api/server.py": "its own connection file, read back under a small ceiling",
}


def test_no_module_reads_a_file_without_being_on_the_audit_list() -> None:
    """§30. The defect was one call in one module, but the *class* of defect is
    "somebody read a file". Enumerating the readers turns the next one into a test
    failure at the moment it is written, rather than a finding in the next review.
    """
    import ast

    package = Path(__file__).parent.parent.parent / "repoatlas"
    offenders: dict[str, list[str]] = {}
    for path in sorted(package.rglob("*.py")):
        rel = path.relative_to(package.parent).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            rendered = ast.unparse(node.func)
            reads = rendered.endswith((".read_bytes", ".read_text", "os.read", ".read"))
            opens = rendered.endswith(".open") and "boundary" not in rendered
            # A bare `open(...)` renders as exactly "open" and so ends with
            # neither, which left the most ordinary way to read a file the one
            # way past this sweep. Named here with the descriptor-level calls for
            # the same reason: the check is only worth having if the obvious
            # spelling is the one it catches.
            bare = rendered in {"open", "os.open", "os.fdopen"}
            if reads or opens or bare:
                found.append(rendered)
        if found and rel not in READERS:
            offenders[rel] = sorted(set(found))
    assert not offenders, (
        f"these modules read files and are not on the audit list in this test: {offenders}"
    )


def test_the_indexer_has_no_route_to_an_unbounded_read() -> None:
    """The narrower claim, checked at the one layer that handles repository
    content in bulk: nothing under `sync/` or `indexing/` can obtain bytes except
    through the boundary's limited reader."""
    import ast

    package = Path(__file__).parent.parent.parent / "repoatlas"
    # RepoAtlas's own writer-lock file, which it wrote itself and whose size it
    # chose. Named rather than pattern-matched, so adding a second exception is a
    # decision somebody has to make in this file.
    exempt = {package / "sync" / "lock.py"}
    for area in ("sync", "indexing", "parsers", "dependencies", "graph"):
        for path in sorted((package / area).rglob("*.py")):
            if path in exempt:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    rendered = ast.unparse(node.func)
                    assert not rendered.endswith((".read_bytes", ".read_text")), (
                        f"{path.relative_to(package.parent)}: {rendered}"
                    )
