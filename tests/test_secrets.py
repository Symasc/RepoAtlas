"""Secret exclusion at every stage that could leak it (V1_SPEC §17).

The guarantee is structural, not a filter applied late: secret files are dropped
**at enumeration**, so nothing downstream ever sees the path, let alone the
content. These tests assert that separately at each stage the content could
otherwise reach — files, chunks, the FTS index, embeddings, evidence, the model
prompt, and operator-visible output — because a single choke point is only a
guarantee if the stages behind it really do depend on it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.cli.main import app
from repoatlas.discovery.ignore import is_secret_path
from repoatlas.llm.scripted import ScriptedBackend
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace
from typer.testing import CliRunner

runner = CliRunner()

#: One canary per pattern in ``SECRET_PATTERNS``. Each value is unique, so a leak
#: identifies which pattern failed rather than merely that something failed.
SECRET_FILES: dict[str, str] = {
    ".env": "SECRET_TOKEN=canary-dotenv-must-not-appear",
    ".env.production": "SERVICE_HOST=canary-dotenv-production-must-not-appear",
    "server.pem": "-----BEGIN CERTIFICATE-----\ncanary-pem-must-not-appear\n",
    "tls.key": "-----BEGIN PRIVATE KEY-----\ncanary-key-must-not-appear\n",
    "id_rsa": "-----BEGIN OPENSSH PRIVATE KEY-----\ncanary-rsa-must-not-appear\n",
    "id_ed25519": "-----BEGIN OPENSSH PRIVATE KEY-----\ncanary-ed25519-must-not-appear\n",
}

CANARIES = tuple(text.split("canary")[1].split("\n")[0] for text in SECRET_FILES.values())


@pytest.fixture
def with_secrets(initialized: Workspace, workspace_root: Path) -> Workspace:
    """Plant every secret pattern in a managed repository, then sync."""
    repo = workspace_root / "python-service"
    for name, text in SECRET_FILES.items():
        (repo / name).write_text(text)
    # Also inside a subdirectory: the patterns match on the file name, not on a
    # path prefix, and a key beside the code it serves is the realistic case.
    (repo / "src" / "signing.pem").write_text("-----BEGIN CERTIFICATE-----\ncanary-nested\n")
    sync(initialized, initialized.resolver().resolve(repo))
    return initialized


def _leaks(haystack: str) -> list[str]:
    return [canary for canary in (*CANARIES, "-nested") if canary in haystack]


# -- the filter itself ------------------------------------------------------


@pytest.mark.parametrize("name", sorted(SECRET_FILES))
def test_every_documented_pattern_is_recognized(name: str) -> None:
    assert is_secret_path(name)
    assert is_secret_path(f"deep/nested/{name}")


@pytest.mark.parametrize(
    "name", [".envrc", "environment.py", "keys.py", "public.pem.md", "id_rsa_helper.sh"]
)
def test_ordinary_files_with_similar_names_are_not_excluded(name: str) -> None:
    """Over-exclusion silently loses real evidence, so the patterns are exact."""
    assert not is_secret_path(name)


# -- stage by stage ---------------------------------------------------------


def test_no_secret_file_is_enumerated(with_secrets: Workspace) -> None:
    rows = with_secrets.database.execute("SELECT rel_path FROM files").fetchall()
    indexed = {row["rel_path"] for row in rows}
    assert indexed.isdisjoint(SECRET_FILES)
    assert "src/signing.pem" not in indexed


def test_no_secret_content_reaches_a_chunk(with_secrets: Workspace) -> None:
    rows = with_secrets.database.execute("SELECT text FROM chunks").fetchall()
    assert _leaks("\n".join(row["text"] for row in rows)) == []


def test_no_secret_content_is_searchable(with_secrets: Workspace) -> None:
    for canary in CANARIES:
        assert with_secrets.search.search_literal(f"canary{canary}") == []
    assert with_secrets.search.search_literal("SECRET_TOKEN") == []


def test_no_secret_content_is_embedded(with_secrets: Workspace) -> None:
    """Vectors are keyed by chunk hash, so a chunk that never existed has none.

    Asserted against the stored text rather than the vectors themselves, because
    an embedding is not reversible and its absence must be shown at the source.
    """
    rows = with_secrets.database.execute(
        "SELECT c.text FROM chunks c JOIN embeddings e ON e.content_hash = c.content_hash"
    ).fetchall()
    assert _leaks("\n".join(row["text"] for row in rows)) == []


def test_no_secret_becomes_an_entity(with_secrets: Workspace) -> None:
    rows = with_secrets.database.execute("SELECT key, name FROM entities").fetchall()
    joined = "\n".join(f"{row['key']} {row['name']}" for row in rows)
    assert _leaks(joined) == []
    # The env-var name in a .env file must not become an interned entity either:
    # it was never read, so RepoAtlas knows nothing about it.
    assert not [row for row in rows if row["key"] == "env:SECRET_TOKEN"]


def test_no_secret_reaches_an_evidence_set(with_secrets: Workspace, workspace_root: Path) -> None:
    context = with_secrets.resolver().resolve(workspace_root / "python-service")
    for question in ("SECRET_TOKEN", "Where are the credentials?", "private key"):
        evidence = with_secrets.retriever().retrieve(question, context)
        assert _leaks("\n".join(c.text for c in evidence.candidates)) == []
        assert not [c for c in evidence.candidates if is_secret_path(c.path)]


def test_secret_files_are_absent_from_a_full_reindex_too(
    with_secrets: Workspace, workspace_root: Path
) -> None:
    """Exclusion is not an incremental-sync artifact."""
    sync(with_secrets, with_secrets.resolver().resolve(workspace_root), full=True)
    rows = with_secrets.database.execute("SELECT rel_path FROM files").fetchall()
    assert {row["rel_path"] for row in rows}.isdisjoint(SECRET_FILES)


# -- the prompt and operator-visible output ---------------------------------


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("REPOATLAS_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("REPOATLAS_DB", str(tmp_path / "state" / "atlas.db"))
    yield tmp_path / "config.yaml"


def _run_in(directory: Path, *args: str):
    previous = Path.cwd()
    os.chdir(directory)
    try:
        return runner.invoke(app, list(args))
    finally:
        os.chdir(previous)


def test_no_secret_reaches_the_model_prompt(
    cli_env: Path, workspace_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The strongest form: inspect every message actually handed to the backend."""
    repo = workspace_root / "python-service"
    for name, text in SECRET_FILES.items():
        (repo / name).write_text(text)

    backend = ScriptedBackend(
        [json.dumps({"summary": "No credentials are indexed.", "hypotheses": [], "unknowns": []})]
    )
    monkeypatch.setattr("repoatlas.services.query_service.build_model", lambda settings: backend)

    assert _run_in(workspace_root, "init", str(workspace_root)).exit_code == 0
    result = _run_in(repo, "ask", "What is SECRET_TOKEN set to?")
    assert result.exit_code == 0

    prompt = "\n".join(message.content for call in backend.calls for message in call)
    assert _leaks(prompt) == []
    assert ".env" not in prompt


def test_no_secret_appears_in_verbose_operator_output(cli_env: Path, workspace_root: Path) -> None:
    """Logging carries paths, line numbers and counts — never content (§12)."""
    repo = workspace_root / "python-service"
    for name, text in SECRET_FILES.items():
        (repo / name).write_text(text)

    assert _run_in(workspace_root, "init", str(workspace_root)).exit_code == 0
    output = "".join(
        _run_in(repo, *args).output
        for args in (
            ("sync", "--verbose"),
            ("status", "--verbose"),
            ("locate", "SECRET_TOKEN", "--verbose"),
            ("locate", "credentials", "--verbose", "--json"),
        )
    )
    assert _leaks(output) == []
