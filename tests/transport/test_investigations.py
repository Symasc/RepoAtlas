"""§7, §27 and §28: an investigation is a task, not a connection or a process.

This is the rule most likely to be quietly broken by a transport, because the
convenient thing is right there: the server already has a connection, the
connection already has a lifetime, and hanging one ledger off it would make
``investigation_id`` optional everywhere and look like a simplification.

It would not be one. A tool launcher starts this process once and keeps it for a
day's worth of unrelated tasks. An ``E1`` scoped to that is "something you were
shown at some point today", and a grounding guarantee scoped to *that* is
materially weaker than one scoped to the question being answered --- while
reading, to the caller, exactly the same.

So: one process serves many investigations at once, they cannot see each other,
and none of them survives the process. The last of those is asserted across a
real restart, since a persistence shortcut is the other convenient thing a
transport could add.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from repoatlas.capabilities.service import CapabilityService
from repoatlas.services.workspace import Workspace
from tests.transport.conftest import (
    GLOBAL,
    over_protocol,
    over_stdio,
    refusal,
    requires_console_script,
    stdio_session,
    structured,
)

from mcp import ClientSession

FIRST = "Where is SERVICE_HOST configured?"
SECOND = "Which ruby gems does the tooling declare?"


def test_two_calls_without_an_id_are_two_investigations(service: CapabilityService) -> None:
    """§27. One connection, two tasks, two ledgers.

    The connection is identical for both calls, so anything derived from it would
    give them the same id. They must differ.
    """

    async def body(session: ClientSession) -> tuple[str, str]:
        first = structured(
            await session.call_tool("repoatlas_search", {"question": FIRST, "context": GLOBAL})
        )
        second = structured(
            await session.call_tool("repoatlas_search", {"question": SECOND, "context": GLOBAL})
        )
        return first["investigation_id"], second["investigation_id"]

    left, right = over_protocol(service, body)
    assert left != right
    assert len(service.sessions) == 2


def test_a_supplied_id_continues_that_investigation_rather_than_starting_one(
    service: CapabilityService,
) -> None:
    """§7. Echoing the id back is the whole of continuing a task."""

    async def body(session: ClientSession) -> tuple[str, str, int]:
        first = structured(
            await session.call_tool("repoatlas_search", {"question": FIRST, "context": GLOBAL})
        )
        investigation = first["investigation_id"]
        followed = structured(
            await session.call_tool(
                "repoatlas_inspect",
                {
                    "context": {"scope": "repository", "repository": "python-service"},
                    "target": {"path": "src/config.py"},
                    "line_start": 1,
                    "line_end": 4,
                    "investigation_id": investigation,
                },
            )
        )
        return investigation, followed["investigation_id"], len(followed["segments"])

    investigation, continued, segments = over_protocol(service, body)
    assert continued == investigation
    assert segments >= 1
    assert len(service.sessions) == 1, "continuing a task must not open a second one"


def test_evidence_stays_inside_the_investigation_that_supplied_it(
    service: CapabilityService,
) -> None:
    """§27. ``E1`` means two different things at once, and neither leaks.

    Both investigations hold an ``E1``, so citing ``E1`` in one of them must
    resolve *that* one's span. The citation below states the other
    investigation's repository and path explicitly, which is the only way to tell
    the difference between correct isolation and a lookup that reached across.
    """

    async def body(session: ClientSession) -> dict[str, Any]:
        first = structured(
            await session.call_tool("repoatlas_search", {"question": FIRST, "context": GLOBAL})
        )
        second = structured(
            await session.call_tool("repoatlas_search", {"question": SECOND, "context": GLOBAL})
        )
        verdict = structured(
            await session.call_tool(
                "repoatlas_verify_grounding",
                {
                    "investigation_id": second["investigation_id"],
                    "answer": {
                        "summary": "s",
                        "claims": [
                            {
                                "statement": "A claim citing the other task's evidence.",
                                "citations": [
                                    {
                                        "evidence_id": "E1",
                                        "repository_slug": first["evidence"][0]["ref"][
                                            "repository_slug"
                                        ],
                                        "path": first["evidence"][0]["ref"]["path"],
                                    }
                                ],
                            }
                        ],
                    },
                },
            )
        )
        return {"first": first, "second": second, "verdict": verdict}

    seen = over_protocol(service, body)
    first_ref = seen["first"]["evidence"][0]["ref"]
    second_ref = seen["second"]["evidence"][0]["ref"]
    assert seen["first"]["investigation_id"] != seen["second"]["investigation_id"]
    assert (first_ref["repository_slug"], first_ref["path"]) != (
        second_ref["repository_slug"],
        second_ref["path"],
    ), "the two tasks must hold different evidence at E1 for this to prove anything"

    verdict = seen["verdict"]
    assert verdict["investigation_id"] == seen["second"]["investigation_id"]
    # The citation named the other investigation's file. It must not ground.
    assert verdict["verdict"] != "grounded"
    assert verdict["findings"], "the mismatch came back as a finding, not silently"


def test_the_three_stateless_capabilities_open_no_investigation(
    service: CapabilityService,
) -> None:
    """§7. Listing, reporting and syncing are not evidence, so they mint nothing."""

    async def body(session: ClientSession) -> None:
        for tool in ("repoatlas_repositories", "repoatlas_status", "repoatlas_sync"):
            structured(await session.call_tool(tool, {"context": GLOBAL}))

    over_protocol(service, body)
    assert len(service.sessions) == 0


def test_the_connection_itself_is_never_an_investigation(service: CapabilityService) -> None:
    """§7. Two clients in turn, and neither inherits anything from the transport.

    A second connection to the same service is a second connection and nothing
    more. If an investigation were derived from one, the first client's evidence
    would be citable by the second.
    """

    async def first_body(session: ClientSession) -> str:
        return structured(
            await session.call_tool("repoatlas_search", {"question": FIRST, "context": GLOBAL})
        )["investigation_id"]

    async def second_body(session: ClientSession) -> str:
        return structured(
            await session.call_tool("repoatlas_search", {"question": FIRST, "context": GLOBAL})
        )["investigation_id"]

    first = over_protocol(service, first_body)
    second = over_protocol(service, second_body)
    assert first != second
    assert len(service.sessions) == 2, "each task kept its own ledger across two connections"


# ---------------------------------------------------------------------------
# §28: a restart loses process-local state, and does not pretend otherwise
# ---------------------------------------------------------------------------


@requires_console_script
def test_an_investigation_does_not_survive_a_server_restart(
    indexed: tuple[Workspace, Path, Path],
) -> None:
    """§28. Two real processes, the same index, and no bridge between them.

    An id from a previous process must be unknown rather than quietly recreated as
    an empty investigation. The empty one would accept the id and then report every
    citation into it as fabricated --- an accusation, where the truth is only that
    the server restarted.
    """
    workspace, _, state = indexed
    workspace.close()

    async def mint(session: ClientSession) -> str:
        return structured(
            await session.call_tool("repoatlas_search", {"question": FIRST, "context": GLOBAL})
        )["investigation_id"]

    investigation = over_stdio(state, mint)
    assert investigation

    async def reuse(session: ClientSession) -> Any:
        return await session.call_tool(
            "repoatlas_verify_grounding",
            {
                "investigation_id": investigation,
                "answer": {
                    "summary": "s",
                    "claims": [
                        {
                            "statement": "A claim from before the restart.",
                            "citations": [{"evidence_id": "E1"}],
                        }
                    ],
                },
            },
        )

    error = refusal(over_stdio(state, reuse))
    assert error["code"] == "UNKNOWN_INVESTIGATION"
    assert error["category"] == "session"
    assert error["retryable"] is False
    # And the remedy is stated rather than left to be inferred.
    assert "new one" in (error["detail"] or "").lower()


@requires_console_script
def test_a_restarted_server_answers_reads_exactly_as_before(
    indexed: tuple[Workspace, Path, Path],
) -> None:
    """The index is durable even though investigations are not.

    Worth pinning beside the test above so that "restart loses state" is not read
    as "restart loses the index", which would be a reason to add the persistence
    §28 forbids.
    """
    workspace, _, state = indexed
    workspace.close()

    async def read(session: ClientSession) -> dict[str, Any]:
        return structured(await session.call_tool("repoatlas_repositories", {"context": GLOBAL}))

    first = over_stdio(state, read)
    second = over_stdio(state, read)
    assert first == second
    assert first["repositories"]


@requires_console_script
def test_two_investigations_interleave_in_one_process(
    indexed: tuple[Workspace, Path, Path],
) -> None:
    """§27, on a real process: A and B advance alternately and stay apart."""
    workspace, _, state = indexed
    workspace.close()

    async def body(session: ClientSession) -> dict[str, Any]:
        a = structured(
            await session.call_tool("repoatlas_search", {"question": FIRST, "context": GLOBAL})
        )
        b = structured(
            await session.call_tool("repoatlas_search", {"question": SECOND, "context": GLOBAL})
        )
        a_again = structured(
            await session.call_tool(
                "repoatlas_relationships",
                {
                    "context": GLOBAL,
                    "target": "env:SERVICE_HOST",
                    "investigation_id": a["investigation_id"],
                },
            )
        )
        b_again = structured(
            await session.call_tool(
                "repoatlas_dependencies",
                {"context": GLOBAL, "limit": 2, "investigation_id": b["investigation_id"]},
            )
        )
        return {"a": a, "b": b, "a_again": a_again, "b_again": b_again}

    seen = over_stdio(state, body)
    assert seen["a"]["investigation_id"] != seen["b"]["investigation_id"]
    assert seen["a_again"]["investigation_id"] == seen["a"]["investigation_id"]
    assert seen["b_again"]["investigation_id"] == seen["b"]["investigation_id"]


@requires_console_script
def test_one_process_serves_a_second_client_without_carrying_anything_over(
    indexed: tuple[Workspace, Path, Path],
) -> None:
    """A reconnecting client gets no magic global investigation."""
    workspace, _, state = indexed
    workspace.close()

    async def main() -> tuple[str, str]:
        async with stdio_session(state) as session:
            first = structured(
                await session.call_tool("repoatlas_search", {"question": FIRST, "context": GLOBAL})
            )["investigation_id"]
        async with stdio_session(state) as session:
            second = structured(
                await session.call_tool("repoatlas_search", {"question": FIRST, "context": GLOBAL})
            )["investigation_id"]
        return first, second

    import anyio

    first, second = anyio.run(main)
    assert first != second
