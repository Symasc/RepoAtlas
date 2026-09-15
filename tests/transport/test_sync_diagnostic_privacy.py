"""A sanitized sync diagnostic crossing the wire (RC audit, RCA-09).

The capability suite proves the diagnostic is safe. This one proves the transport
did not undo that and, more to the point, that it was never asked to.

The adapter is thin by design (Slice J), and RCA-03 settled why: a transport that
scanned strings for host paths would be a second privacy rule drifting against the
first, and the direct caller --- who never goes through it --- would silently keep
the defect. So there is no redaction under ``repoatlas/mcp/``, and what is asserted
here is parity: what ``CapabilityService.sync`` decided to say is exactly what the
client receives, in the structured content and in the text block alike.

The text block is checked on its own account. It is a serialization of the same
response, so anything leaking through it reaches a reading model whether or not
the client ever parses ``structuredContent``.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from mcp.types import CallToolResult
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.models.parsing import GRAMMAR_UNAVAILABLE, INVALID_CFG, INVALID_YAML
from repoatlas.services.init_service import initialize
from repoatlas.services.workspace import open_workspace
from tests.capabilities.test_sync_diagnostic_privacy import (
    AUDIT_GRAMMAR_MESSAGE,
    HOSTILE,
    SENTINELS,
    SOURCE,
    walk,
)
from tests.transport.conftest import GLOBAL, over_protocol, structured


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.fixture
def grammar_fails(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str], None]]:
    """Fail grammar loading at each grammar's binding, so production builds the exception."""
    import importlib

    from repoatlas.parsers import treesitter
    from repoatlas.parsers.grammars import GRAMMARS

    def install(message: str) -> None:
        def refuse(*_args: object, **_kwargs: object) -> object:
            raise ImportError(message)

        for grammar in GRAMMARS.values():
            monkeypatch.setattr(importlib.import_module(grammar.module), grammar.function, refuse)
        treesitter.load_parser.cache_clear()

    treesitter.load_parser.cache_clear()
    yield install
    treesitter.load_parser.cache_clear()


@pytest.fixture
def hostile_service(tmp_path: Path) -> Iterator[Callable[[], CapabilityService]]:
    """Build a fresh service over a repository that is about to stop parsing.

    A factory rather than one service, because the first sync is the only one that
    parses anything: the second finds an unchanged tree and reports nothing. The
    parity test needs two syncs that both did the work, so it needs two indexes.
    """
    built: list[CapabilityService] = []

    def make() -> CapabilityService:
        base = tmp_path / f"lab{len(built)}"
        repo = base / "repos" / "app"
        repo.mkdir(parents=True)
        for name in HOSTILE:
            (repo / name).write_text("placeholder\n", encoding="utf-8")
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "a@b.invalid")
        _git(repo, "config", "user.name", "A")
        _git(repo, "config", "commit.gpgsign", "false")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "x")

        settings = Settings.model_validate(
            {
                "storage": {"database": str(base / "state" / "atlas.db")},
                "embeddings": {"provider": "hashing", "dim": 128},
            }
        )
        workspace = open_workspace(settings=settings, require_root=False)
        initialize(workspace, base / "repos", config_path=base / "config.yaml")

        # Only now do the files become unparseable, so the sync under test is the
        # run that meets them.
        for name, text in HOSTILE.items():
            if isinstance(text, bytes):
                (repo / name).write_bytes(text)
            else:
                (repo / name).write_text(text, encoding="utf-8")

        service = CapabilityService(workspace)
        built.append(service)
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
# CASE 9: the transport says exactly what the capability said
# ---------------------------------------------------------------------------


def test_the_wire_carries_the_same_safe_diagnostics_as_the_direct_call(
    hostile_service: Callable[[], CapabilityService], grammar_fails: Callable[[str], None]
) -> None:
    """CASE 9. Semantic parity, not merely "both happen to be safe".

    Two independently safe answers would still be a defect: an agent reading a
    sync over MCP must see the same diagnostics a direct caller sees, or the two
    deployments disagree about what the sync found.
    """
    from repoatlas.models.capability import CapabilityContext, CapabilityScope
    from repoatlas.models.sync import SyncRequest

    grammar_fails(AUDIT_GRAMMAR_MESSAGE)
    direct = hostile_service().sync(
        SyncRequest(context=CapabilityContext(scope=CapabilityScope.GLOBAL), full=True)
    )
    expected = sorted(
        (d.severity.value, d.message, d.line, d.path)
        for view in direct.repositories
        for d in view.diagnostics
    )
    assert expected, "the fixture must produce diagnostics for parity to mean anything"

    wired = sorted(
        (d["severity"], d["message"], d["line"], d["path"])
        for d in wire_diagnostics(call(hostile_service()))
    )
    assert wired == expected


def test_the_sync_adapter_holds_no_privacy_policy_of_its_own() -> None:
    """The transport is thin, and that is what keeps one rule instead of two."""
    for module in (Path("repoatlas/mcp/server.py"), Path("repoatlas/mcp/tools.py")):
        source = module.read_text(encoding="utf-8")
        for forbidden in ("redact", "sanitize", "scrub", "GRAMMAR_UNAVAILABLE"):
            assert forbidden not in source, f"{module}: {forbidden}"


# ---------------------------------------------------------------------------
# CASES 1-6 over the wire, in both halves of the result
# ---------------------------------------------------------------------------


def test_the_grammar_reproduction_does_not_cross_the_wire(
    hostile_service: Callable[[], CapabilityService], grammar_fails: Callable[[str], None]
) -> None:
    """CASE 1, transported. The sync still succeeds; it just says less."""
    grammar_fails(AUDIT_GRAMMAR_MESSAGE)
    result = call(hostile_service())

    assert result.isError is False
    body = everything(result)
    assert "/home/private-user/.cache/repoatlas/PRIVATE_GRAMMAR.so" not in body
    assert "PRIVATE_GRAMMAR" not in body
    assert "RCA09_SECRET_SENTINEL_918273" not in body
    assert GRAMMAR_UNAVAILABLE in {d["message"] for d in wire_diagnostics(result)}


def test_no_repository_source_crosses_the_wire(
    hostile_service: Callable[[], CapabilityService], grammar_fails: Callable[[str], None]
) -> None:
    """CASE 6, transported.

    ``configparser`` and PyYAML quote the offending source line into their
    messages. Over a transport that is the difference between a diagnostic and an
    unadmitted copy of the file.
    """
    grammar_fails(AUDIT_GRAMMAR_MESSAGE)
    result = call(hostile_service())

    assert SOURCE not in everything(result)
    said = {d["message"] for d in wire_diagnostics(result)}
    assert INVALID_CFG in said
    assert INVALID_YAML in said


@pytest.mark.parametrize("sentinel", SENTINELS)
def test_no_shape_of_private_text_crosses_the_wire(
    hostile_service: Callable[[], CapabilityService],
    grammar_fails: Callable[[str], None],
    sentinel: str,
) -> None:
    """CASES 2-5, transported, one shape at a time.

    The Windows and UNC cases are the ones a ``json.dumps`` substring check would
    wave through, so the traversal here is over raw field values.
    """
    grammar_fails(f"cannot load {sentinel}")
    body = everything(call(hostile_service()))

    assert sentinel not in body
    for fragment in sentinel.replace("\\", "/").split("/"):
        if len(fragment) > 8:
            assert fragment not in body, fragment


def test_the_text_block_is_covered_and_not_merely_the_structured_one(
    hostile_service: Callable[[], CapabilityService], grammar_fails: Callable[[str], None]
) -> None:
    """A guard on the guard above.

    If the SDK ever stopped emitting a text block, every assertion about text
    content would pass by having nothing to search.
    """
    grammar_fails(AUDIT_GRAMMAR_MESSAGE)
    result = call(hostile_service())

    text = text_of(result)
    assert text.strip(), "the result carried no text block to check"
    assert GRAMMAR_UNAVAILABLE in text


# ---------------------------------------------------------------------------
# CASE 12 over the wire
# ---------------------------------------------------------------------------


def test_the_wire_still_says_which_file_each_diagnostic_is_about(
    hostile_service: Callable[[], CapabilityService], grammar_fails: Callable[[str], None]
) -> None:
    """CASE 12, transported. Sanitizing removed a leak, not the coordinates.

    The path is the repository-relative identity the sync admitted. A caller can
    open it; that is the whole point of reporting the failure.
    """
    grammar_fails(AUDIT_GRAMMAR_MESSAGE)
    found = wire_diagnostics(call(hostile_service()))

    assert found
    located = {d["path"]: d["message"] for d in found}
    assert "app.ts" in located
    assert "setup.cfg" in located
    for diagnostic in found:
        assert diagnostic["path"] is not None
        assert not Path(diagnostic["path"]).is_absolute()
