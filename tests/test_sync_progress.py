"""A sync run reports how far it has got, and the terminal draws it (ARCHITECTURE.md §9).

Two halves. The run's events are asserted against the report the same run
returns, so "617 of 617 files" is a count of the files it actually wrote and
"5 of 5" names the link pass's real stages, in order. The terminal renderer is
asserted on what it writes: one line, redrawn in place, never wider than the
terminal, and gone before anything else is printed.

And the claim the protocol makes about itself: a listener observes the run and
changes nothing, so the facts a watched run writes are the facts an unwatched
one writes.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import TextIO

import pytest
from repoatlas.config.settings import Settings
from repoatlas.rendering.progress import TerminalProgress
from repoatlas.services.init_service import initialize
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace
from repoatlas.sync.progress import LINK_STAGES, SyncProgress
from tests.conftest import fact_snapshot, reindex_from_scratch


class Recorder:
    """Every event, in arrival order."""

    def __init__(self) -> None:
        self.events: list[tuple[object, ...]] = []
        #: The embedding counts alone, typed, for the comparisons below.
        self.embedded: list[tuple[int, int]] = []

    def discovering(self) -> None:
        self.events.append(("discovering",))

    def scanning(self, position: int, total: int, repository: str, hashed: int) -> None:
        self.events.append(("scanning", position, total, repository, hashed))

    def indexing(self, position: int, total: int, repository: str, done: int, files: int) -> None:
        self.events.append(("indexing", position, total, repository, done, files))

    def linking(self, stage: str, position: int, total: int) -> None:
        self.events.append(("linking", stage, position, total))

    def embedding(self, done: int, total: int) -> None:
        self.events.append(("embedding", done, total))
        self.embedded.append((done, total))

    def of(self, kind: str) -> list[tuple[object, ...]]:
        return [event for event in self.events if event[0] == kind]


def _kinds(recorder: Recorder) -> list[object]:
    """The event kinds with consecutive repeats collapsed: the run's phases, in order."""
    kinds: list[object] = []
    for event in recorder.events:
        if not kinds or kinds[-1] != event[0]:
            kinds.append(event[0])
    return kinds


def test_init_reports_discovery_each_repository_and_every_link_stage(
    workspace: Workspace, workspace_root: Path, tmp_path: Path
) -> None:
    recorder = Recorder()
    listener: SyncProgress = recorder
    report, _ = initialize(
        workspace, workspace_root, config_path=tmp_path / "config.yaml", progress=listener
    )

    repositories = [entry for entry in report.repositories if not entry.enumeration_failed]
    assert len(repositories) >= 2
    kinds = _kinds(recorder)
    assert kinds[0] == "discovering"
    assert kinds[1:-2] == ["scanning", "indexing"] * len(repositories)
    assert kinds[-2:] == ["linking", "embedding"]

    # Each repository's file count is the number the report says it indexed, and
    # the count climbs one file at a time from nothing to all of them.
    for position, entry in enumerate(repositories, start=1):
        counts = [e for e in recorder.of("indexing") if e[1] == position]
        assert {e[3] for e in counts} == {entry.repository}
        assert {e[2] for e in counts} == {len(repositories)}
        assert [e[4] for e in counts] == list(range(entry.files_indexed + 1))
        assert {e[5] for e in counts} == {entry.files_indexed}

    assert recorder.of("linking") == [
        ("linking", stage, position, len(LINK_STAGES))
        for position, stage in enumerate(LINK_STAGES, start=1)
    ]

    done = [count for count, _ in recorder.embedded]
    assert done[0] == 0 and done == sorted(done)
    assert recorder.embedded[-1] == (report.embedded_chunks, report.embedded_chunks)


def test_a_repository_scoped_sync_reports_one_repository_and_no_discovery(
    initialized: Workspace, workspace_root: Path
) -> None:
    target = workspace_root / "python-service"
    (target / "src" / "progress_probe.py").write_text("def probe():\n    return 1\n")
    recorder = Recorder()
    context = initialized.resolver().resolve(target)
    report = sync(initialized, context, progress=recorder)

    assert not recorder.of("discovering")
    assert {e[1:4] for e in recorder.of("indexing")} == {(1, 1, report.repositories[0].repository)}
    assert recorder.of("indexing")[-1][4:] == (1, 1)
    assert [e[1] for e in recorder.of("linking")] == list(LINK_STAGES)


def test_a_full_rehash_reports_the_files_it_hashes(
    initialized: Workspace, workspace_root: Path
) -> None:
    recorder = Recorder()
    context = initialized.resolver().resolve(workspace_root / "python-service")
    sync(initialized, context, full=True, progress=recorder)

    hashed = [e[4] for e in recorder.of("scanning")]
    assert hashed[0] == 0
    assert hashed == list(range(len(hashed)))
    assert len(hashed) > 1


def test_a_watched_run_writes_exactly_what_an_unwatched_run_writes(
    workspace: Workspace, workspace_root: Path, tmp_path: Path, settings: Settings
) -> None:
    initialize(workspace, workspace_root, config_path=tmp_path / "config.yaml", progress=Recorder())
    assert fact_snapshot(workspace) == reindex_from_scratch(settings, workspace_root, tmp_path)


# ---------------------------------------------------------------------------
# The terminal renderer
# ---------------------------------------------------------------------------


class Terminal(io.StringIO):
    """A stream that says it is a terminal, with a chosen encoding."""

    def __init__(self, encoding: str = "utf-8") -> None:
        super().__init__()
        self._encoding = encoding

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return self._encoding

    def isatty(self) -> bool:
        return True


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _frames(written: str) -> list[str]:
    return [frame.removesuffix("\x1b[K") for frame in written.split("\r") if frame]


def _renderer(stream: io.StringIO, clock: Clock | None = None, columns: int = 100):
    return TerminalProgress(stream, clock=clock or Clock(), columns=lambda: columns)  # type: ignore[arg-type]


def test_progress_is_drawn_only_on_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TERM", raising=False)
    assert TerminalProgress.for_stream(io.StringIO()) is None
    assert TerminalProgress.for_stream(Terminal()) is not None
    monkeypatch.setenv("TERM", "dumb")
    assert TerminalProgress.for_stream(Terminal()) is None


def test_the_bar_shows_the_repository_its_share_and_the_count() -> None:
    stream = Terminal()
    progress = _renderer(stream)
    progress.indexing(2, 4, "lvl", 500, 1000)
    (frame,) = _frames(stream.getvalue())
    assert frame.startswith("[2/4] lvl")
    assert " 50%" in frame and "500/1,000 files" in frame
    assert frame.count("█") == frame.count("░")


def test_a_link_stage_counts_the_stages_before_it_as_done() -> None:
    stream = Terminal()
    progress = _renderer(stream)
    progress.linking("uses", 3, 5)
    (frame,) = _frames(stream.getvalue())
    assert frame.startswith("Linking")
    assert " 40%" in frame and "resolving uses (3/5)" in frame


def test_updates_are_throttled_but_a_new_phase_and_the_last_file_always_draw() -> None:
    stream = Terminal()
    clock = Clock()
    progress = _renderer(stream, clock)
    for done in range(10):
        progress.indexing(1, 1, "api", done, 10)
    assert len(_frames(stream.getvalue())) == 1

    progress.indexing(1, 1, "api", 10, 10)
    progress.linking("index", 1, 5)
    clock.now += 1
    progress.linking("index", 1, 5)
    frames = _frames(stream.getvalue())
    assert len(frames) == 4
    assert "10/10 files" in frames[1]
    assert frames[2].startswith("Linking")


def test_a_line_never_reaches_the_terminal_edge() -> None:
    stream = Terminal()
    clock = Clock()
    progress = _renderer(stream, clock, columns=40)
    progress.indexing(12, 40, "a-repository-with-a-very-long-name-indeed", 3, 1_000_000)
    clock.now += 1
    progress.scanning(13, 40, "x" * 80, 123_456)
    clock.now += 1
    progress.embedding(1, 3)
    frames = _frames(stream.getvalue())
    assert len(frames) == 3
    assert all(len(frame) <= 39 for frame in frames), frames


def test_close_erases_the_line_and_only_once_something_was_drawn() -> None:
    stream = Terminal()
    progress = _renderer(stream)
    progress.close()
    assert stream.getvalue() == ""
    progress.discovering()
    progress.close()
    assert stream.getvalue().endswith("\r\x1b[K")
    written = len(stream.getvalue())
    progress.close()
    assert len(stream.getvalue()) == written


def test_a_terminal_that_cannot_encode_blocks_gets_ascii() -> None:
    stream = Terminal(encoding="ascii")
    progress = _renderer(stream)
    progress.embedding(1, 2)
    (frame,) = _frames(stream.getvalue())
    frame.encode("ascii")
    assert "#" in frame and "-" in frame


def test_a_terminal_that_went_away_does_not_fail_the_run() -> None:
    class Gone(Terminal):
        def write(self, text: str) -> int:
            raise OSError("terminal closed")

    progress = _renderer(Gone())
    progress.indexing(1, 1, "api", 1, 2)
    progress.indexing(1, 1, "api", 2, 2)
    progress.close()


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------


@pytest.fixture
def on_a_terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolated state, and a progress line drawn on the command's own stderr.

    The runner's stderr is not a terminal, so the line is forced on: the point is
    to see where in the command's output it is drawn and erased.
    """
    from repoatlas.cli import main

    monkeypatch.setenv("REPOATLAS_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("REPOATLAS_DB", str(tmp_path / "state" / "atlas.db"))

    def always(cls: type[TerminalProgress], stream: TextIO) -> TerminalProgress:
        return cls(stream)

    monkeypatch.setattr(main.TerminalProgress, "for_stream", classmethod(always))


@pytest.mark.usefixtures("on_a_terminal")
def test_init_draws_its_progress_then_erases_it_before_the_report(workspace_root: Path) -> None:
    from repoatlas.cli.main import app
    from typer.testing import CliRunner

    result = CliRunner().invoke(app, ["init", str(workspace_root)])
    assert result.exit_code == 0, result.output
    drawn, erased, report = result.output.rpartition("\r\x1b[K")
    assert erased
    assert "files" in drawn and "Linking" in drawn
    assert report.startswith("Root: ")
    assert "\r" not in report and "\x1b" not in report


@pytest.mark.usefixtures("on_a_terminal")
def test_a_failing_sync_erases_its_progress_before_the_error(
    workspace_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import repoatlas.services
    from repoatlas.cli.main import app
    from repoatlas.errors import RepoAtlasError
    from typer.testing import CliRunner

    runner = CliRunner()
    assert runner.invoke(app, ["init", str(workspace_root)]).exit_code == 0

    def failing(workspace: object, context: object, *, full: bool, progress: SyncProgress) -> None:
        progress.indexing(1, 1, "python-service", 1, 2)
        raise RepoAtlasError("the sync went wrong")

    monkeypatch.setattr(repoatlas.services, "sync", failing)
    result = runner.invoke(app, ["sync", "python-service"])
    assert result.exit_code == 1
    assert "1/2 files\x1b[K\r\x1b[Kthe sync went wrong" in result.output
