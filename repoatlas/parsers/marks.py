"""Line provenance from YAML marks (ARCHITECTURE.md §5).

Both YAML-based parsers report positions taken from PyYAML marks, and a mark can
legitimately sit at end-of-stream — one line past the last line of the file. A
line number that does not exist in the file is a fabricated citation, however
small, so it is clamped here rather than at each call site.
"""

from __future__ import annotations

from typing import Any


def line_count(content: str) -> int:
    """Lines in ``content``. A trailing newline ends a line; it does not add one."""
    return max(1, len(content.splitlines()))


def diagnostic_line(mark: Any | None, content: str) -> int | None:
    """The 1-based line a parse failure points at, clamped to the file."""
    if mark is None:
        return None
    return min(int(mark.line) + 1, line_count(content))
