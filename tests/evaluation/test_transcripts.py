"""Accounting must survive streaming duplication, missing data and overlapping tools."""

import hashlib
import json
import sys

import pytest
from tests.evaluation.transcripts import main, normalize


def event(second, role, content, **fields):
    return {
        "timestamp": f"2026-09-22T00:00:{second:02d}+00:00",
        "message": {"role": role, "content": content, **fields},
    }


def usage(n):
    return dict(
        input_tokens=n,
        cache_creation_input_tokens=20,
        cache_read_input_tokens=100,
        output_tokens=10,
    )


def test_duplicate_stream_records_are_one_usage_and_tools_may_overlap():
    rows = [
        event(0, "user", "task"),
        event(
            1,
            "assistant",
            [{"type": "tool_use", "id": "a", "name": "Bash"}],
            id="m1",
            usage=usage(5),
        ),
        event(
            1,
            "assistant",
            [{"type": "tool_use", "id": "b", "name": "RepoAtlas"}],
            id="m1",
            usage=usage(5),
        ),
        event(4, "user", [{"type": "tool_result", "tool_use_id": "a", "content": "é"}]),
        event(
            5,
            "user",
            [{"type": "tool_result", "tool_use_id": "b", "content": "x", "is_error": True}],
        ),
        event(9, "assistant", [{"type": "text", "text": "answer"}], id="m2", usage=usage(15)),
    ]
    result = normalize(rows)
    assert result["assistant_messages"] == 2
    assert result["usage"]["input_tokens"]["total"] == 20
    assert result["context_growth"] == 10 and result["final_input_context"] == 135
    assert result["prompt_to_last_assistant_text_seconds"] == 9
    assert result["last_tool_to_last_assistant_text_seconds"] == 4
    assert result["tool_count"] == 2
    assert result["by_tool"]["Bash"]["response_bytes"] == 2
    assert sum(t["client_interval_seconds"] for t in result["tools"]) == 7
    assert result["tools"][1]["is_error"]


def test_missing_usage_and_tool_result_are_unknown_not_zero():
    result = normalize(
        [
            event(0, "user", "task"),
            event(1, "assistant", [{"type": "tool_use", "id": "a", "name": "read"}], id="m1"),
        ]
    )
    assert result["usage"]["input_tokens"]["total"] is None
    assert result["final_input_context"] is None
    assert result["by_tool"]["read"]["response_bytes"] is None
    assert result["tools"][0]["client_interval_seconds"] is None
    assert result["prompt_to_last_assistant_text_seconds"] is None


def test_updated_usage_uses_last_values_and_flags_the_change():
    result = normalize(
        [
            event(1, "assistant", [], id="m1", usage=usage(1)),
            event(2, "assistant", [], id="m1", usage=usage(2)),
        ]
    )
    assert result["usage"]["input_tokens"]["total"] == 2
    assert result["usage_changed_messages"] == ["m1"]


def test_conflicting_tool_ids_require_session_boundaries():
    with pytest.raises(ValueError, match="split sessions"):
        normalize(
            [
                event(1, "assistant", [{"type": "tool_use", "id": "a", "name": "read"}], id="m1"),
                event(2, "assistant", [{"type": "tool_use", "id": "a", "name": "search"}], id="m2"),
            ]
        )


def test_cli_preserves_source_identity_and_refuses_out_of_range_boundaries(tmp_path, monkeypatch):
    transcript = tmp_path / "session.jsonl"
    manifest = tmp_path / "run.json"
    output = tmp_path / "metrics.json"
    transcript.write_text(
        "\n".join(
            json.dumps(row)
            for row in (
                event(0, "user", "unrelated earlier task"),
                event(1, "user", "benchmark task"),
                event(3, "assistant", "answer", id="m1", usage=usage(1)),
            )
        )
    )
    manifest.write_text(
        json.dumps(
            {
                "task": "example",
                "arm": "direct",
                "repetition": 1,
                "repository_revisions": {"app": "abc123"},
                "model": "test-model",
                "thinking_effort": None,
                "output_requirements": "source citations",
                "index_state": "none",
            }
        )
    )
    arguments = [
        "transcripts",
        str(transcript),
        "--start",
        "1",
        "--stop",
        "3",
        "--manifest",
        str(manifest),
        "--output",
        str(output),
    ]
    monkeypatch.setattr(sys, "argv", arguments)
    main()
    result = json.loads(output.read_text())
    assert result["source"] == {
        "sha256": hashlib.sha256(transcript.read_bytes()).hexdigest(),
        "record_range": [1, 3],
    }
    assert result["metrics"]["prompt_to_last_assistant_text_seconds"] == 2
    assert "unrelated earlier task" not in output.read_text()
    arguments[5] = "4"
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
