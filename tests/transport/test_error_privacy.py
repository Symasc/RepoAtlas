"""An unrecognized failure discloses nothing across the wire either (RCA-03).

The adapter is a transport: it calls
:func:`~repoatlas.capabilities.errors.capability_error` and dumps what comes
back. So there is nothing here that could scrub a path, and nothing here that
should --- a transport that inspected strings for host paths would be a second
privacy policy, drifting against the first and disagreeing with it on the day it
mattered.

What these tests establish is that the boundary below really is the only one
needed: the same failure, raised once and read twice, produces the same envelope
whether a caller holds the service directly or reaches it over MCP. The audit
found the leak by driving the real protocol, so the regression is written the
same way.

The recursive scan at the end is the part that generalizes. Asserting on
``message`` would pass while ``detail`` or ``context`` carried the path, and
``context`` is a free-form object --- so the whole serialized result, both
content blocks included, is walked for host-absolute shapes.
"""

from __future__ import annotations

import contextlib
import json
import re
from typing import Any

import pytest
from repoatlas.capabilities.errors import (
    INTERNAL_ERROR_DETAIL,
    INTERNAL_ERROR_MESSAGE,
    capability_error,
)
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.capability_io import RepositoriesRequest
from tests.transport.conftest import GLOBAL, over_protocol, refusal, repository, structured

from mcp import ClientSession

HOST_PATH = "/tmp/PRIVATE_HOST_ROOT/repos/app/NO_SUCH_FILE_XYZ"  # noqa: S108 - a sentinel that is never opened
SECRET = "RCA03_SECRET_SENTINEL_918273"  # noqa: S105 - a marker, not a secret
SOURCE = "RCA03_SOURCE_SENTINEL_DO_NOT_LEAK"
WINDOWS_PATH = r"C:\Users\PrivateUser\repo\secret.py"
CONFIG_PATH = "/home/private-user/.config/repoatlas/config.yaml"

#: Host-absolute shapes only. A repository-relative ``src/app.py`` is a natural
#: identifier a caller asked about and must keep crossing, so the patterns are
#: anchored on the things that can only be host layout: a leading separator, a
#: drive letter, a UNC prefix.
_ABSOLUTE = (
    re.compile(r"(?<![\w.])/(?:home|tmp|var|Users|root|opt|etc|private)/"),
    re.compile(r"[A-Za-z]:\\\\"),
    re.compile(r"[A-Za-z]:\\"),
    re.compile(r"\\\\\\\\[A-Za-z0-9_.-]+\\\\"),
)


def _host_paths(blob: str) -> list[str]:
    found: list[str] = []
    for pattern in _ABSOLUTE:
        found.extend(pattern.findall(blob))
    return found


def _walk(value: object) -> list[str]:
    """Every string anywhere in a payload, unescaped."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [part for item in value.items() for part in _walk(item)]
    if isinstance(value, (list, tuple)):
        return [part for item in value for part in _walk(item)]
    return [repr(value)]


def _whole_result(capability_service: CapabilityService, tool: str, arguments: Any) -> str:
    """Everything the client receives: structured content and every text block."""

    async def body(session: ClientSession) -> Any:
        return await session.call_tool(tool, arguments)

    result = over_protocol(capability_service, body)
    # Raw values as well as the JSON text: ``json.dumps`` escapes a backslash, so
    # a Windows path searched for only in the serialized form could never match.
    parts = _walk(result.structuredContent)
    for block in result.content:
        text = getattr(block, "text", "")
        parts.append(text)
        with contextlib.suppress(ValueError):
            parts.extend(_walk(json.loads(text)))
    parts.append(json.dumps(result.structuredContent, sort_keys=True))
    return "\u0000".join(parts)


def _envelope(capability_service: CapabilityService, tool: str, arguments: Any) -> dict[str, Any]:
    async def body(session: ClientSession) -> Any:
        return await session.call_tool(tool, arguments)

    return refusal(over_protocol(capability_service, body))


def _exploding(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    def explode(self: object, request: object) -> object:
        raise exc

    monkeypatch.setattr(CapabilityService, "status", explode)


# ---------------------------------------------------------------------------
# The audit reproduction, over the protocol
# ---------------------------------------------------------------------------


def test_the_audit_reproduction_does_not_cross_the_wire(
    service: CapabilityService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CASE 1, through MCP."""
    _exploding(monkeypatch, FileNotFoundError(2, "No such file or directory", HOST_PATH))
    blob = _whole_result(service, "repoatlas_status", {"context": GLOBAL})
    assert HOST_PATH not in blob
    assert "NO_SUCH_FILE_XYZ" not in blob
    assert _host_paths(blob) == []


@pytest.mark.parametrize(
    ("label", "leak"),
    [
        ("absolute posix path", HOST_PATH),
        ("secret", SECRET),
        ("repository source", SOURCE),
        ("windows path", WINDOWS_PATH),
        ("config path", CONFIG_PATH),
    ],
)
def test_no_sentinel_survives_the_transport(
    service: CapabilityService, monkeypatch: pytest.MonkeyPatch, label: str, leak: str
) -> None:
    """CASES 2-5, through MCP."""
    _exploding(monkeypatch, RuntimeError(leak))
    assert leak not in _whole_result(service, "repoatlas_status", {"context": GLOBAL}), label


def test_direct_and_mcp_produce_the_same_safe_envelope(
    service: CapabilityService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CASE 6. The adapter adds no policy of its own, and needs none."""
    exc = FileNotFoundError(2, "No such file or directory", HOST_PATH)
    direct = capability_error(exc).model_dump(mode="json")

    _exploding(monkeypatch, exc)
    over_the_wire = _envelope(service, "repoatlas_status", {"context": GLOBAL})

    assert over_the_wire == direct
    assert over_the_wire["message"] == INTERNAL_ERROR_MESSAGE
    assert over_the_wire["detail"] == INTERNAL_ERROR_DETAIL
    assert over_the_wire["context"] == {}


def test_the_text_block_says_the_same_thing_as_the_structured_one(
    service: CapabilityService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both are rendered from one payload; a client reading either is safe."""
    _exploding(monkeypatch, RuntimeError(f"{HOST_PATH} {SECRET}"))

    async def body(session: ClientSession) -> Any:
        return await session.call_tool("repoatlas_status", {"context": GLOBAL})

    result = over_protocol(service, body)
    assert result.isError is True
    for block in result.content:
        assert json.loads(block.text) == result.structuredContent


# ---------------------------------------------------------------------------
# Recognized failures still cross whole
# ---------------------------------------------------------------------------


def test_a_mapped_error_keeps_its_code_and_words(service: CapabilityService) -> None:
    """CASE 7, over the protocol."""
    error = _envelope(service, "repoatlas_repositories", {"context": repository("no-such-repo")})
    assert error["code"] in {"REPOSITORY_NOT_FOUND", "REPOSITORY_AMBIGUOUS"}
    assert error["message"]
    assert error["message"] != INTERNAL_ERROR_MESSAGE


def test_mapped_errors_do_not_collapse_into_one_code(service: CapabilityService) -> None:
    """The flattening mutation, asserted over the wire."""
    codes = {
        _envelope(service, "repoatlas_repositories", {"context": repository("nope")})["code"],
        _envelope(service, "repoatlas_relationships", {"context": GLOBAL, "target": "env:NOPE"})[
            "code"
        ],
        _envelope(
            service,
            "repoatlas_verify_grounding",
            {"investigation_id": "never-issued", "answer": {"summary": "s", "claims": []}},
        )["code"],
    }
    assert len(codes) == 3
    assert "INTERNAL_ERROR" not in codes


# ---------------------------------------------------------------------------
# Validation stays useful
# ---------------------------------------------------------------------------


def test_a_malformed_request_keeps_its_field_locations(service: CapabilityService) -> None:
    """CASE 10, first half. The caller has to be able to fix the call."""
    error = _envelope(
        service,
        "repoatlas_inspect",
        {"context": GLOBAL, "target": {"path": "a.py"}, "line_start": -5},
    )
    assert error["code"] == "INVALID_REQUEST"
    fields = [item["field"] for item in error["context"]["fields"]]
    assert any("line_start" in field for field in fields)
    assert error["detail"]


def test_a_hostile_value_is_not_echoed_back(service: CapabilityService) -> None:
    """CASE 10, second half.

    Pydantic puts the offending ``input`` in every error it reports, and the
    adapter drops it. The value here is a caller's own argument, so echoing it
    discloses nothing the caller did not send --- but a caller that pasted a
    secret or a file into a request should not have it read back out of an error
    by whatever is logging the transport.
    """
    hostile = f"{CONFIG_PATH}#{SECRET}"
    error = _envelope(
        service,
        "repoatlas_inspect",
        {"context": GLOBAL, "target": {"path": hostile}, "line_start": "not-a-number"},
    )
    assert error["code"] == "INVALID_REQUEST"
    blob = json.dumps(error, sort_keys=True)
    assert SECRET not in blob
    assert CONFIG_PATH not in blob
    # Still useful: the field that was wrong is named.
    assert any("line_start" in item["field"] for item in error["context"]["fields"])


# ---------------------------------------------------------------------------
# The deliberate opt-in is not collateral damage
# ---------------------------------------------------------------------------


def test_host_paths_still_cross_when_they_were_asked_for(service: CapabilityService) -> None:
    """CASE 11.

    ``include_host_paths`` is a separate, deliberate API. A correction that
    censored it would have fixed a leak by breaking a feature, and the difference
    between the two is exactly what this whole change is about: what RepoAtlas
    chose to say versus what an exception said on its behalf.
    """

    async def body(session: ClientSession) -> Any:
        return structured(
            await session.call_tool(
                "repoatlas_repositories", {"context": GLOBAL, "include_host_paths": True}
            )
        )

    payload = over_protocol(service, body)
    paths = [item.get("host_path") for item in payload["repositories"]]
    assert any(path for path in paths), "the opt-in must still return host paths"
    assert all(path is None or path.startswith("/") for path in paths)


def test_host_paths_stay_absent_without_the_opt_in(service: CapabilityService) -> None:
    """The other half: the default is still off."""

    async def body(session: ClientSession) -> Any:
        return structured(await session.call_tool("repoatlas_repositories", {"context": GLOBAL}))

    payload = over_protocol(service, body)
    assert all(item.get("host_path") is None for item in payload["repositories"])


def test_the_direct_capability_opt_in_is_unaffected(service: CapabilityService) -> None:
    response = service.repositories(
        RepositoriesRequest(
            context=CapabilityContext(scope=CapabilityScope.GLOBAL), include_host_paths=True
        )
    )
    assert any(item.host_path for item in response.repositories)


# ---------------------------------------------------------------------------
# Ordinary responses are untouched
# ---------------------------------------------------------------------------


def test_successful_calls_are_unaffected(service: CapabilityService) -> None:
    """CASE 12. The correction is at the error boundary and nowhere else."""

    async def body(session: ClientSession) -> Any:
        found = structured(
            await session.call_tool(
                "repoatlas_search", {"context": GLOBAL, "question": "SERVICE_HOST"}
            )
        )
        seen = structured(
            await session.call_tool(
                "repoatlas_inspect",
                {
                    "context": repository("python-service"),
                    "target": {"path": "src/config.py"},
                },
            )
        )
        return found, seen

    found, seen = over_protocol(service, body)
    assert found["evidence"]
    assert seen["segments"]
    assert INTERNAL_ERROR_MESSAGE not in json.dumps(found)
    assert INTERNAL_ERROR_MESSAGE not in json.dumps(seen)


# ---------------------------------------------------------------------------
# The generalizing scan
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        FileNotFoundError(2, "No such file or directory", HOST_PATH),
        RuntimeError(f"opening {CONFIG_PATH} failed"),
        RuntimeError(WINDOWS_PATH),
        OSError(13, "Permission denied", "/var/lib/repoatlas/atlas.db"),
        ValueError(f"bad row in {SOURCE}: {SECRET}"),
    ],
)
def test_no_host_absolute_shape_survives_any_unexpected_failure(
    service: CapabilityService, monkeypatch: pytest.MonkeyPatch, exc: BaseException
) -> None:
    """CASE 13. Recursive over the whole result, not just ``message``."""
    _exploding(monkeypatch, exc)
    blob = _whole_result(service, "repoatlas_status", {"context": GLOBAL})
    assert _host_paths(blob) == [], blob


def test_the_scan_would_actually_catch_something(service: CapabilityService) -> None:
    """A guard on the guard.

    A pattern set that matched nothing would make every test above vacuous, so
    it is pointed at text that must trip it.
    """
    assert _host_paths(f'{{"message": "{HOST_PATH}"}}')
    assert _host_paths(json.dumps({"message": WINDOWS_PATH}))
    assert _host_paths(json.dumps({"message": CONFIG_PATH}))
    # And must not trip on a repository-relative identifier.
    assert _host_paths(json.dumps({"message": "src/app.py is not indexed"})) == []
    assert _host_paths(json.dumps({"message": "app/src/config.py:18"})) == []
