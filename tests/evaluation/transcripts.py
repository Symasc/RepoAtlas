"""Offline agent-run accounting; no model calls, raw source, or thinking export.

Usage is deduplicated by assistant message id. Missing data stays unknown, and
client tool intervals are never labeled server or model inference time.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

TOKEN_FIELDS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)


class RunManifest(BaseModel):
    """Conditions needed to compare completed agent work rather than tool speed."""

    model_config = ConfigDict(extra="forbid")
    task: str
    arm: str
    repetition: int
    repository_revisions: dict[str, str]
    model: str
    thinking_effort: str | None
    output_requirements: str
    index_state: Literal["cold", "warm", "none"]
    build_revision: str | None = None
    facts_version: str | None = None
    vector_count: int | None = None
    skill_hash: str | None = None
    schema_hash: str | None = None
    setup_seconds: float | None = None
    review_seconds: float | None = None
    correction_seconds: float | None = None
    correctness: dict[str, Any] | None = None


def _elapsed(start: str | None, end: str | None) -> float | None:
    if start is None or end is None:
        return None
    value = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    return value if value >= 0 else None


def normalize(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    messages: dict[str, dict[str, Any]] = {}
    calls: dict[str, dict[str, Any]] = {}
    results: dict[str, dict[str, Any]] = {}
    changed_usage: set[str] = set()
    missing_ids = 0
    prompt_at = final_text_at = None
    for row in records:
        message = row.get("message", {})
        timestamp = row.get("timestamp")
        content = message.get("content", [])
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if message.get("role") == "assistant":
            identity = message.get("id")
            if identity:
                usage = message.get("usage", {}) or {}
                previous = messages.setdefault(identity, {"usage": {}})
                if any(
                    k in previous["usage"] and previous["usage"][k] != v for k, v in usage.items()
                ):
                    changed_usage.add(identity)
                previous["usage"].update(usage)
            else:
                missing_ids += 1
            for item in content:
                if item.get("type") == "text":
                    final_text_at = timestamp
                elif item.get("type") == "tool_use":
                    call = {"tool": item["name"], "requested_at": timestamp}
                    if item["id"] in calls and calls[item["id"]] != call:
                        raise ValueError(
                            "conflicting duplicate tool id; split sessions before accounting"
                        )
                    calls[item["id"]] = call
        elif message.get("role") == "user":
            for item in content:
                if item.get("type") == "text" and prompt_at is None:
                    prompt_at = timestamp
                elif item.get("type") == "tool_result":
                    value = item.get("content")
                    size = (
                        None
                        if value is None
                        else len(
                            (
                                value
                                if isinstance(value, str)
                                else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                            ).encode()
                        )
                    )
                    results[item["tool_use_id"]] = {
                        "responded_at": timestamp,
                        "response_bytes": size,
                        "is_error": item.get("is_error", False),
                    }
    usage_totals = {}
    for field in TOKEN_FIELDS:
        values = [m["usage"].get(field) for m in messages.values()]
        complete = bool(values) and not missing_ids and all(v is not None for v in values)
        usage_totals[field] = {
            "total": sum(values) if complete else None,
            "observed_subtotal": sum(v for v in values if v is not None),
            "messages_missing": sum(v is None for v in values) + missing_ids,
        }
    tool_rows = []
    by_tool: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for identity, call in calls.items():
        result = results.get(identity, {})
        row = {
            "id": identity,
            **call,
            "responded_at": result.get("responded_at"),
            "response_bytes": result.get("response_bytes"),
            "is_error": result.get("is_error"),
            "client_interval_seconds": _elapsed(call["requested_at"], result.get("responded_at")),
        }
        tool_rows.append(row)
        by_tool[call["tool"]].append(row)

    def context(message: dict[str, Any]) -> int | None:
        values = [message["usage"].get(k) for k in TOKEN_FIELDS[:3]]
        return sum(values) if all(v is not None for v in values) else None

    ordered = list(messages.values())
    first = context(ordered[0]) if ordered else None
    last = context(ordered[-1]) if ordered else None
    last_result = max(
        (r["responded_at"] for r in results.values() if r["responded_at"]), default=None
    )
    return {
        "assistant_messages": len(messages),
        "missing_message_id_records": missing_ids,
        "usage_changed_messages": sorted(changed_usage),
        "usage": usage_totals,
        "first_input_context": first,
        "final_input_context": last,
        "context_growth": last - first if first is not None and last is not None else None,
        "prompt_to_last_assistant_text_seconds": _elapsed(prompt_at, final_text_at),
        "last_tool_to_last_assistant_text_seconds": _elapsed(last_result, final_text_at),
        "tool_count": len(calls),
        "tool_counts": dict(Counter(c["tool"] for c in calls.values())),
        "tools": tool_rows,
        "by_tool": {
            name: {
                "calls": len(rows),
                "response_bytes": sum(r["response_bytes"] for r in rows)
                if all(r["response_bytes"] is not None for r in rows)
                else None,
                "summed_client_intervals_seconds": sum(r["client_interval_seconds"] for r in rows)
                if all(r["client_interval_seconds"] is not None for r in rows)
                else None,
            }
            for name, rows in by_tool.items()
        },
        "limitations": [
            "Client intervals may overlap; their sum is not wall time.",
            "Last assistant text is an observed transcript boundary, not verified user delivery.",
            "Model inference, server execution, transport, schema/skill injection and "
            "billing costs "
            "are not separable from these events alone.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transcript", type=Path)
    parser.add_argument(
        "--start", type=int, required=True, help="First benchmark record, zero-based"
    )
    parser.add_argument("--stop", type=int, required=True, help="Exclusive last benchmark record")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.start < 0 or args.stop <= args.start:
        parser.error("require 0 <= start < stop")
    manifest = RunManifest.model_validate_json(args.manifest.read_text())
    raw = args.transcript.read_bytes()
    lines = raw.decode("utf-8").splitlines()
    if args.stop > len(lines):
        parser.error("stop exceeds the transcript record count")
    result = normalize(json.loads(line) for line in lines[args.start : args.stop])
    args.output.write_text(
        json.dumps(
            {
                "source": {
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "record_range": [args.start, args.stop],
                },
                "manifest": manifest.model_dump(),
                "metrics": result,
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
