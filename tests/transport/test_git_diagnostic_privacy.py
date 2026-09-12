"""A sanitized enumeration diagnostic crossing the wire (RC audit, RCA-10).

The capability suite proves the diagnostic is safe. This one proves the transport
did not undo that and, more to the point, that it was never asked to.

The adapter is thin by design (Slice J), and RCA-03 settled why: a transport that
scanned strings for host paths would be a second privacy rule drifting against
the first, and the direct caller --- who never goes through it --- would silently
keep the defect. So there is no redaction under ``repoatlas/mcp/``, and what is
asserted here is parity: what ``CapabilityService.sync`` decided to say is exactly
what the client receives, in the structured content and in the text block alike.

The text block is checked on its own account. It is a serialization of the same
response, so anything leaking through it reaches a reading model whether or not
the client ever parses ``structuredContent``.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from mcp.types import CallToolResult
from repoatlas.capabilities.service import CapabilityService
from repoatlas.gitstate.client import FileListing, GitClient
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.parsing import ENUMERATION_INCOMPLETE
from repoatlas.models.sync import SyncRequest
from tests.capabilities.test_git_diagnostic_privacy import (
    POSIX_PATH,
    REPOSITORY_TEXT,
    SENTINEL,
    SHAPES,
    _lab,
    _service,
    walk,
)
from tests.transport.conftest import GLOBAL, over_protocol, structured

DIRECT = CapabilityContext(scope=CapabilityScope.GLOBAL)

skip_as_root = pytest.mark.skipif(
    os.geteuid() == 0, reason="root reads a mode-000 path, so git never warns"
)


@pytest.fixture
def warned_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., CapabilityService]]:
    """A fresh service per call, over an enumeration that warned.

    A factory rather than one service, because a sync only does the work once: a
    second run over an unchanged tree reports nothing, and the parity test needs
    two syncs that both did it.

    The patch is installed for the lifetime of the fixture rather than around one
    call, because the sync under test happens in the caller --- and, for the MCP
    half, inside the protocol session.
    """
    built: list[CapabilityService] = []
    warnings: list[str] = []

    real = GitClient.list_files

    def warned(self: GitClient, repo_path: Path) -> FileListing | None:
        listing = real(self, repo_path)
        if listing is None:
            return None
        return FileListing(paths=listing.paths, warnings=tuple(warnings))

    monkeypatch.setattr(GitClient, "list_files", warned)

    def make(*lines: str) -> CapabilityService:
        warnings[:] = lines
        base = tmp_path / f"lab{len(built)}"
        repo = _lab(base, {"src.py": f"HOST = {REPOSITORY_TEXT!r}\n"})
        service = _service(base)
        built.append(service)
        (repo / "src.py").write_text(
            f"HOST = {REPOSITORY_TEXT!r}\n\n\ndef load() -> str:\n    return HOST\n",
            encoding="utf-8",
        )
        return service

    try:
        yield make
    finally:
        for service in built:
            service.close()


def call(service: CapabilityService) -> CallToolResult:
    async def body(session: Any) -> Any:
        return await session.call_tool("repoatlas_sync", {"context": GLOBAL, "full": True})

    return over_protocol(service, body)


def text_of(result: CallToolResult) -> str:
    """Every text block the SDK produced, which is what a model actually reads."""
    return " ".join(getattr(block, "text", "") for block in result.content)


def everything(result: CallToolResult) -> str:
    """Structured and text content together, unescaped.

    Joined from raw values rather than from JSON text, so a backslash-bearing path
    is searched for in the form it was injected in.
    """
    payload = result.structuredContent or {}
    return " ".join(walk(payload)) + " " + text_of(result)


def wire_diagnostics(result: CallToolResult) -> list[dict[str, Any]]:
    payload = structured(result)
    return [d for view in payload["repositories"] for d in view["diagnostics"]]


# ---------------------------------------------------------------------------
# CASE 10: the transport says exactly what the capability said
# ---------------------------------------------------------------------------


def test_the_wire_carries_the_same_safe_diagnostics_as_the_direct_call(
    warned_service: Callable[..., CapabilityService],
) -> None:
    """CASE 10. Semantic parity, not merely "both happen to be safe".

    Two independently safe answers would still be a defect: an agent reading a
    sync over MCP must see the same diagnostics a direct caller sees, or the two
    deployments disagree about what the sync found.
    """
    warning = f"warning: unable to access '{POSIX_PATH}': Permission denied"

    direct = warned_service(warning).sync(SyncRequest(context=DIRECT, full=True))
    expected = sorted(
        (d.severity.value, d.message, d.line, d.path)
        for view in direct.repositories
        for d in view.diagnostics
    )
    assert expected, "the fixture must produce a diagnostic for parity to mean anything"

    wired = sorted(
        (d["severity"], d["message"], d["line"], d["path"])
        for d in wire_diagnostics(call(warned_service(warning)))
    )
    assert wired == expected


def test_the_sync_adapter_holds_no_privacy_policy_of_its_own() -> None:
    """The transport is thin, and that is what keeps one rule instead of two."""
    for module in (Path("repoatlas/mcp/server.py"), Path("repoatlas/mcp/tools.py")):
        source = module.read_text(encoding="utf-8")
        for forbidden in (
            "redact",
            "sanitize",
            "scrub",
            "ENUMERATION_INCOMPLETE",
            "stderr=",
            "list_files",
        ):
            assert forbidden not in source, f"{module}: {forbidden}"


# ---------------------------------------------------------------------------
# The shapes, over the wire, in both halves of the result
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape", SHAPES)
def test_no_shape_of_private_text_crosses_the_wire(
    warned_service: Callable[..., CapabilityService], shape: str
) -> None:
    """CASES 2-5, transported, one shape at a time."""
    service = warned_service(f"warning: unable to access '{shape}': Permission denied")
    result = call(service)

    assert result.isError is False
    body = everything(result)
    assert shape not in body
    for fragment in shape.replace("\\", "/").split("/"):
        if len(fragment) > 8:
            assert fragment not in body, fragment
    assert ENUMERATION_INCOMPLETE in {d["message"] for d in wire_diagnostics(result)}


def test_no_git_prose_of_any_kind_crosses_the_wire(
    warned_service: Callable[..., CapabilityService],
) -> None:
    """CASE 6 and CASE 14, transported. Not the path, not the sentence, not the argv."""
    service = warned_service(
        f"warning: unable to access '{POSIX_PATH}': Permission denied",
        f"warning: could not open directory '{REPOSITORY_TEXT}/': Permission denied",
    )
    body = everything(call(service))

    for fragment in (
        "Permission denied",
        "unable to access",
        "could not open directory",
        "warning:",
        REPOSITORY_TEXT,
        SENTINEL,
        "ls-files",
        "--exclude-standard",
        "core.hooksPath",
    ):
        assert fragment not in body, fragment


def test_the_text_block_is_covered_and_not_merely_the_structured_one(
    warned_service: Callable[..., CapabilityService],
) -> None:
    """A guard on the guard above.

    If the SDK ever stopped emitting a text block, every assertion about text
    content would pass by having nothing to search.
    """
    service = warned_service(f"warning: unable to access '{POSIX_PATH}': Permission denied")
    result = call(service)

    text = text_of(result)
    assert text.strip(), "the result carried no text block to check"
    assert ENUMERATION_INCOMPLETE in text


def test_a_clean_enumeration_crosses_with_no_diagnostic_at_all(
    warned_service: Callable[..., CapabilityService],
) -> None:
    """CASE 9, transported. Silence still means the listing was complete."""
    result = call(warned_service())

    assert result.isError is False
    assert wire_diagnostics(result) == []


# ---------------------------------------------------------------------------
# CASE 1 over the wire: the real reproduction, end to end
# ---------------------------------------------------------------------------


@skip_as_root
def test_the_real_audit_reproduction_does_not_cross_the_wire(tmp_path: Path) -> None:
    """CASE 1, transported, with a real git process and no patching at all.

    The repository configures its own ``core.excludesFile``; git writes the path to
    stderr; the sync reports incompleteness. What reaches the client is the
    RepoAtlas sentence and nothing else.
    """
    base = tmp_path / "lab"
    repo = _lab(base, {"src.py": "A = 1\n", "sub/private/notes.md": "# notes\n"})

    private_home = base / "PRIVATE_HOME"
    private_home.mkdir()
    excludes = private_home / f"{SENTINEL}_excludes"
    excludes.write_text("*.tmp\n", encoding="utf-8")
    excludes.chmod(0o000)
    locked = repo / "sub" / "private"

    subprocess.run(
        ["git", "config", "core.excludesFile", str(excludes)],
        cwd=repo,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    locked.chmod(0o000)

    try:
        service = _service(base)
        try:
            result = call(service)
        finally:
            service.close()
    finally:
        locked.chmod(0o755)
        excludes.chmod(0o644)

    body = everything(result)
    assert SENTINEL not in body
    assert "PRIVATE_HOME" not in body
    assert "Permission denied" not in body
    assert str(excludes) not in body
    assert ENUMERATION_INCOMPLETE in {d["message"] for d in wire_diagnostics(result)}
