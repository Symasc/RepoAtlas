"""``ask`` and ``debug`` through the CLI (V1_SPEC §4, §16, §19).

The reasoning backend is scripted, so these run with no Ollama and no network.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.cli.main import app
from repoatlas.errors import ExitCode
from repoatlas.llm.scripted import ScriptedBackend
from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("REPOATLAS_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("REPOATLAS_DB", str(tmp_path / "state" / "atlas.db"))
    yield tmp_path / "config.yaml"


def run_in(directory: Path, *args: str):
    previous = Path.cwd()
    os.chdir(directory)
    try:
        return runner.invoke(app, list(args))
    finally:
        os.chdir(previous)


def scripted(monkeypatch: pytest.MonkeyPatch, *responses: str, available: bool = True):
    """Install a scripted backend in place of the configured provider."""
    backend = ScriptedBackend(list(responses), is_available=available)
    monkeypatch.setattr("repoatlas.services.query_service.build_model", lambda settings: backend)
    return backend


ANSWER = json.dumps(
    {
        "summary": "SERVICE_HOST comes from the Salt deployment layer.",
        "findings": [
            {
                "statement": "The service reads SERVICE_HOST from the environment.",
                "confidence": 0.9,
                "evidence": [
                    {
                        "repository": "python-service",
                        "path": "src/config.py",
                        "line_start": 16,
                        "line_end": 21,
                        "description": "reads SERVICE_HOST",
                    }
                ],
            }
        ],
        "unknowns": ["The pillar value at runtime."],
        "next_check": "Inspect the pillar value used by the Salt state.",
    }
)


@pytest.fixture
def indexed(cli_env: Path, workspace_root: Path) -> Path:
    assert run_in(workspace_root, "init", str(workspace_root)).exit_code == 0
    return workspace_root


def test_ask_returns_a_grounded_answer(indexed: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scripted(monkeypatch, ANSWER)
    result = run_in(indexed, "ask", "Where does SERVICE_HOST come from?")
    assert result.exit_code == ExitCode.OK, result.output
    assert "ANSWER" in result.output
    assert "python-service/src/config.py:16-21" in result.output
    assert "CONFIDENCE" in result.output
    assert "NEXT" in result.output


def test_ask_uses_the_current_repository_as_context(
    indexed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = scripted(monkeypatch, ANSWER)
    deep = indexed / "python-service" / "src" / "deep" / "nested" / "path"
    result = run_in(deep, "ask", "Where is SERVICE_HOST read?")
    assert result.exit_code == ExitCode.OK
    prompt = backend.calls[0][-1].content
    assert "'python-service'" in prompt


def test_debug_separates_facts_from_hypotheses(
    indexed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted(monkeypatch, ANSWER)
    result = run_in(indexed, "debug", "SERVICE_HOST is empty at startup")
    assert result.exit_code == ExitCode.OK, result.output
    assert "FACTS" in result.output
    assert "HYPOTHESES" in result.output
    assert "UNKNOWNS" in result.output
    assert "NEXT" in result.output


def test_debug_uses_the_debug_prompt(indexed: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = scripted(monkeypatch, ANSWER)
    run_in(indexed, "debug", "connection refused while starting worker")
    assert "Symptom under investigation" in backend.calls[0][-1].content


def test_fabricated_citation_is_stripped_before_display(
    indexed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trust rule, enforced end to end through the CLI."""
    fabricated = json.dumps(
        {
            "summary": "It is set in config/secrets.yaml.",
            "findings": [
                {
                    "statement": "The value comes from config/secrets.yaml.",
                    "confidence": 0.99,
                    "evidence": [
                        {
                            "repository": "python-service",
                            "path": "config/secrets.yaml",
                            "line_start": 3,
                            "line_end": 3,
                            "description": "invented",
                        }
                    ],
                }
            ],
            "unknowns": [],
            "next_check": None,
        }
    )
    scripted(monkeypatch, fabricated)
    result = run_in(indexed, "ask", "Where is SERVICE_HOST configured?")
    assert result.exit_code == ExitCode.OK
    assert "INSUFFICIENT EVIDENCE" in result.output
    assert "config/secrets.yaml:3" not in result.output


def test_invalid_model_output_is_repaired(indexed: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = scripted(monkeypatch, "I cannot produce JSON.", ANSWER)
    result = run_in(indexed, "ask", "Where is SERVICE_HOST?")
    assert result.exit_code == ExitCode.OK
    assert "ANSWER" in result.output
    assert len(backend.calls) == 2


def test_persistently_invalid_output_is_not_rendered(
    indexed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted(monkeypatch, "nope", "still nope")
    result = run_in(indexed, "ask", "Where is SERVICE_HOST?")
    assert result.exit_code == ExitCode.OK
    assert "INSUFFICIENT EVIDENCE" in result.output


def test_unavailable_model_exits_with_code_four(
    indexed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted(monkeypatch, ANSWER, available=False)
    result = run_in(indexed, "ask", "anything")
    assert result.exit_code == ExitCode.LLM_UNAVAILABLE
    assert "repoatlas locate" in result.output


def test_no_llm_flag_gives_evidence_only_output(
    indexed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--no-llm must not consult the model at all (V1_SPEC §4)."""
    backend = scripted(monkeypatch, ANSWER)
    result = run_in(indexed, "ask", "Where is SERVICE_HOST?", "--no-llm")
    assert result.exit_code == ExitCode.OK
    assert "EVIDENCE" in result.output
    assert backend.calls == []
    assert backend.remaining == 1


def test_no_llm_works_when_no_model_is_available(
    indexed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted(monkeypatch, ANSWER, available=False)
    result = run_in(indexed, "debug", "something broke", "--no-llm")
    assert result.exit_code == ExitCode.OK
    assert "EVIDENCE" in result.output


def test_json_output_is_the_validated_answer(
    indexed: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripted(monkeypatch, ANSWER)
    result = run_in(indexed, "ask", "Where is SERVICE_HOST?", "--json")
    assert result.exit_code == ExitCode.OK
    payload = json.loads(result.output)
    assert payload["summary"]
    assert payload["findings"][0]["evidence"][0]["repository"] == "python-service"


def test_verbose_reports_grounding(indexed: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scripted(monkeypatch, ANSWER)
    result = run_in(indexed, "ask", "Where is SERVICE_HOST?", "--verbose")
    assert "grounding: all citations verified" in result.output


def test_verbose_reports_dropped_citations(indexed: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A rising drop rate is a regression signal, so it is reported."""
    fabricated = json.dumps(
        {
            "summary": "s",
            "findings": [
                {
                    "statement": "invented",
                    "confidence": 0.9,
                    "evidence": [
                        {
                            "repository": "ghost-repo",
                            "path": "a.py",
                            "line_start": 1,
                            "line_end": 1,
                            "description": "d",
                        }
                    ],
                }
            ],
            "unknowns": [],
            "next_check": None,
        }
    )
    scripted(monkeypatch, fabricated)
    result = run_in(indexed, "ask", "Where is SERVICE_HOST?", "--verbose")
    assert "1 citation(s) dropped" in result.output
    assert "1 finding(s) demoted" in result.output


def test_reasoning_respects_explicit_repository(
    indexed: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backend = scripted(monkeypatch, ANSWER)
    elsewhere = tmp_path / "outside"
    elsewhere.mkdir()
    result = run_in(elsewhere, "ask", "Where is SERVICE_HOST?", "--repo", "deployment")
    assert result.exit_code == ExitCode.OK
    assert "'deployment'" in backend.calls[0][-1].content


def test_reasoning_outside_managed_tree_errors_on_context(
    indexed: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Context resolution comes first: no model is consulted (V1_SPEC §6)."""
    backend = scripted(monkeypatch, ANSWER)
    elsewhere = tmp_path / "nowhere"
    elsewhere.mkdir()
    result = run_in(elsewhere, "ask", "anything")
    assert result.exit_code == ExitCode.CONTEXT
    assert backend.calls == []
