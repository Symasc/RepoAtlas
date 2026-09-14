"""A cut span keeps its corrected claim across the protocol (RC audit, RCA-01).

The adapter is a transport and holds no repository intelligence, so in principle
there is nothing here to get wrong. That is exactly why it is worth asserting:
the correction removes a field's *value* rather than the field, and a transport
that helpfully substituted a default for a null would reintroduce the fabrication
at the last hop, where no capability test would ever look.

``python-service/longline.py`` is one ~200 KB line. The chunker cuts it, so no
piece can prove which lines it covers, and the evidence must cross the wire with
its line range absent rather than filled in.
"""

from __future__ import annotations

from typing import Any

from tests.transport.conftest import GLOBAL, over_protocol, structured

from mcp import ClientSession

LONG_LINE_FILE = "longline.py"
LONG_LINE_MARKER = "LONG_LINE_MARKER"


def _search(service: Any, **extra: Any) -> dict[str, Any]:
    async def body(session: ClientSession) -> dict[str, Any]:
        arguments: dict[str, Any] = {"context": GLOBAL, "question": LONG_LINE_MARKER}
        arguments.update(extra)
        return structured(await session.call_tool("repoatlas_search", arguments))

    return over_protocol(service, body)


def _long_line_item(payload: dict[str, Any]) -> dict[str, Any]:
    items = [e for e in payload["evidence"] if e["ref"]["path"] == LONG_LINE_FILE]
    assert items, "the long-line fixture must be retrievable over the protocol"
    return items[0]


def test_the_wire_carries_a_null_line_range_rather_than_a_default(service: Any) -> None:
    """``None`` must arrive as ``null``, not as ``1`` and not as an absent key.

    An absent key reads to a client as "the server does not report this", which
    is a different and weaker statement than "no line was supplied".
    """
    item = _long_line_item(_search(service))
    assert item["ref"]["line_start"] is None
    assert item["ref"]["line_end"] is None
    assert "line_start" in item["ref"]
    assert "line_end" in item["ref"]


def test_the_fragment_itself_still_crosses(service: Any) -> None:
    """The correction withholds a claim, not the evidence."""
    item = _long_line_item(_search(service))
    assert item["text"]
    assert item["id"]


def test_the_location_string_names_no_line_over_the_wire(service: Any) -> None:
    """Clients render ``location``; a line there is a claim whatever ``ref`` says."""
    item = _long_line_item(_search(service))
    assert item["location"].endswith(LONG_LINE_FILE)
    assert ":" not in item["location"].rsplit("/", 1)[-1]


def test_the_internal_cut_provenance_is_not_published(service: Any) -> None:
    """It is the shaper's input, not a caller's field."""
    payload = _search(service)
    assert "split_total" not in repr(payload)
    assert "split_index" not in repr(payload)


def test_a_line_citation_into_the_fragment_is_refused_over_the_protocol(service: Any) -> None:
    """The whole seam, driven through the adapter rather than around it."""

    async def body(session: ClientSession) -> dict[str, Any]:
        found = structured(
            await session.call_tool(
                "repoatlas_search", {"context": GLOBAL, "question": LONG_LINE_MARKER}
            )
        )
        item = _long_line_item(found)
        return structured(
            await session.call_tool(
                "repoatlas_verify_grounding",
                {
                    "investigation_id": found["investigation_id"],
                    "answer": {
                        "summary": "bundle contents",
                        "claims": [
                            {
                                "statement": f"{LONG_LINE_FILE} line 1 ends with a credential",
                                "claimed_confidence": 0.9,
                                "citations": [
                                    {
                                        "evidence_id": item["id"],
                                        "line_start": 1,
                                        "line_end": 1,
                                    }
                                ],
                            }
                        ],
                    },
                },
            )
        )

    verdict = over_protocol(service, body)
    assert verdict["verdict"] == "ungrounded"
    assert verdict["dropped_citations"] == 1
