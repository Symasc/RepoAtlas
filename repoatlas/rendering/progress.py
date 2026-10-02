"""A sync run's progress, drawn as one line on a terminal (``init`` and ``sync``).

The line is redrawn in place and erased when the run ends, so what the command
prints afterwards --- the report, or ``--json`` --- is exactly what it printed
before there was a progress bar. It is drawn on stderr and only when stderr is a
terminal: a pipe, a file, a CI log and the MCP transport never see it.

It satisfies :class:`repoatlas.sync.progress.SyncProgress` by shape, not by
import: the command line may not reach into ``sync/`` (ARCHITECTURE.md §1).
"""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Callable
from typing import TextIO

#: What each stage of the global link pass is doing, in the words a terminal shows.
LINK_STAGE_LABELS = {
    "index": "reading the index",
    "resolve": "resolving references",
    "uses": "resolving uses",
    "dependencies": "linking packages",
    "swap": "writing the graph",
}

#: Filled bar, empty bar, spinner frames, and the mark a clipped line ends with.
_UNICODE = ("█", "░", "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏", "…")
_ASCII = ("#", "-", "|/-\\", "~")
#: Erase from the cursor to the end of the line.
_ERASE = "\x1b[K"


class TerminalProgress:
    """Draws a sync run's progress on one terminal line, then erases it."""

    def __init__(
        self,
        stream: TextIO,
        *,
        interval: float = 0.08,
        clock: Callable[[], float] = time.monotonic,
        columns: Callable[[], int] | None = None,
    ) -> None:
        self._stream = stream
        self._interval = interval
        self._clock = clock
        self._columns = columns or (lambda: shutil.get_terminal_size((80, 24)).columns)
        self._fill, self._empty, self._spinner, self._ellipsis = _glyphs(stream)
        self._drawn_at: float | None = None
        self._phase: str | None = None
        self._frame = 0
        self._visible = False
        self._broken = False

    @classmethod
    def for_stream(cls, stream: TextIO) -> TerminalProgress | None:
        """A renderer for ``stream`` when it is an interactive terminal, else ``None``."""
        if os.environ.get("TERM") == "dumb":
            return None
        try:
            interactive = stream.isatty()
        except (AttributeError, ValueError):
            return None
        return cls(stream) if interactive else None

    # -- what a run reports ---------------------------------------------------

    def discovering(self) -> None:
        self._show("discover", "Discovering repositories", None, "")

    def scanning(self, position: int, total: int, repository: str, hashed: int) -> None:
        detail = f"scanning, {hashed:,} hashed" if hashed else "scanning"
        self._show(f"scan:{position}", _repository(position, total, repository), None, detail)

    def indexing(self, position: int, total: int, repository: str, done: int, files: int) -> None:
        self._show(
            f"index:{position}",
            _repository(position, total, repository),
            (done, files),
            f"{done:,}/{files:,} files",
        )

    def linking(self, stage: str, position: int, total: int) -> None:
        label = LINK_STAGE_LABELS.get(stage, stage)
        # Entering stage k means k - 1 are finished.
        self._show(
            f"link:{stage}", "Linking", (position - 1, total), f"{label} ({position}/{total})"
        )

    def close(self) -> None:
        """Erase the line, so the next thing printed starts on a clean one."""
        if self._visible:
            self._write("\r" + _ERASE)
            self._visible = False

    # -- drawing --------------------------------------------------------------

    def _show(self, phase: str, label: str, fraction: tuple[int, int] | None, detail: str) -> None:
        if self._broken:
            return
        now = self._clock()
        changed = phase != self._phase
        finished = fraction is not None and fraction[0] >= fraction[1]
        if (
            not changed
            and not finished
            and self._drawn_at is not None
            and now - self._drawn_at < self._interval
        ):
            return
        self._phase = phase
        self._drawn_at = now
        self._frame += 1
        self._write("\r" + self._line(label, fraction, detail) + _ERASE)
        self._visible = True

    def _line(self, label: str, fraction: tuple[int, int] | None, detail: str) -> str:
        """``label  bar  percent  detail``, never as wide as the terminal.

        A line that wrapped could no longer be redrawn in place: the carriage
        return would go back to the start of the last row only.
        """
        width = max(20, self._columns() - 1)
        label = self._clip(label, 28).ljust(20)
        if fraction is None:
            spinner = self._spinner[self._frame % len(self._spinner)]
            return self._clip(f"{label}  {spinner} {detail}", width)
        done, total = fraction
        ratio = 1.0 if total <= 0 else min(1.0, max(0.0, done / total))
        percent = f"{int(ratio * 100):>3}%"
        room = width - len(label) - len(percent) - len(detail) - 5
        bar_width = max(10, min(32, room))
        filled = round(ratio * bar_width)
        bar = self._fill * filled + self._empty * (bar_width - filled)
        return self._clip(f"{label}  {bar} {percent}  {detail}", width)

    def _clip(self, text: str, width: int) -> str:
        return text if len(text) <= width else text[: max(0, width - 1)] + self._ellipsis

    def _write(self, text: str) -> None:
        try:
            self._stream.write(text)
            self._stream.flush()
        except (OSError, ValueError):
            # A terminal that went away is no reason to fail the sync it was showing.
            self._broken = True


def _repository(position: int, total: int, repository: str) -> str:
    return f"[{position}/{total}] {repository}" if total > 1 else repository


def _glyphs(stream: TextIO) -> tuple[str, str, str, str]:
    """Block characters where the terminal can encode them, ASCII where it cannot."""
    encoding = getattr(stream, "encoding", None) or "ascii"
    try:
        "".join(_UNICODE).encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return _ASCII
    return _UNICODE
