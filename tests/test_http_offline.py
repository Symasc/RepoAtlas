"""HTTP linkage never touches the network (brief §46, §96).

A feature about URLs is the one most likely to be "helpfully" wired to resolve a
host, fetch a specification's remote ``$ref``, or probe whether an endpoint is
up. Every one of those would turn a repository's text into outbound traffic to a
host the repository chose. So the whole workflow — index, sync, search,
relationships, inspect, verify — runs here with the socket layer rigged to record
and refuse every attempt, over a corpus full of absolute URLs and a remote ``$ref``.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.inspection import InspectRequest, InspectTarget
from repoatlas.models.search import SearchRequest
from repoatlas.models.verification import (
    AgentAnswer,
    AgentCitation,
    AgentClaim,
    VerifyGroundingRequest,
)
from repoatlas.services.sync_service import sync
from tests.conftest import git
from tests.http_corpus import GLOBAL, index_corpus, relationships


def test_the_http_workflow_makes_no_network_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[str] = []

    def refuse(name: str) -> Any:
        def denied(*args: object, **kwargs: object) -> Any:
            attempts.append(name)
            raise OSError(f"network denied: {name}")

        return denied

    monkeypatch.setattr(socket.socket, "connect", refuse("socket.connect"))
    monkeypatch.setattr(socket.socket, "connect_ex", refuse("socket.connect_ex"))
    monkeypatch.setattr(socket, "create_connection", refuse("socket.create_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", refuse("socket.getaddrinfo"))

    repos = tmp_path / "corpus"
    workspace = index_corpus(repos)
    # A remote reference that a resolver would be tempted to follow.
    spec = repos / "repos" / "api-a" / "openapi.yaml"
    assert spec.exists()
    try:
        # An incremental sync over a changed client, not only the initial index.
        client = repos / "repos" / "web" / "src" / "api.ts"
        client.write_text(
            client.read_text(encoding="utf-8")
            + "\nexport async function helloAgain() {\n"
            + '    return fetch("https://api.example.com/hello");\n}\n',
            encoding="utf-8",
        )
        git(repos / "repos" / "web", "add", "-A")
        git(repos / "repos" / "web", "commit", "--quiet", "-m", "call the endpoint again")
        sync(workspace, workspace.resolver().resolve(repos / "repos"))

        service = CapabilityService(workspace)
        searched = service.search(
            SearchRequest(question="GET https://api.example.com/hello", context=GLOBAL)
        )
        handle = next(h.key for h in searched.entities if h.key.startswith("http-endpoint:"))
        related = relationships(
            workspace, handle, investigation_id=searched.investigation_id, service=service
        )
        # The call added before the sync is linked, so the sync really ran here.
        assert "helloAgain" in {edge.source.name for edge in related.edges}
        cited = next(e for e in related.edges if e.evidence_ids)
        evidence_id = cited.evidence_ids[0]
        service.inspect(
            InspectRequest(
                context=GLOBAL,
                target=InspectTarget(evidence_id=evidence_id),
                investigation_id=searched.investigation_id,
            )
        )
        service.verify_grounding(
            VerifyGroundingRequest(
                investigation_id=searched.investigation_id,
                answer=AgentAnswer(
                    claims=(
                        AgentClaim(
                            statement="A client statically calls this endpoint.",
                            citations=(AgentCitation(evidence_id=evidence_id),),
                        ),
                    )
                ),
            )
        )
        for key in ("http-route:GET:/hello", "http-deployment:https://api.example.com"):
            relationships(workspace, key, hops=2)
    finally:
        workspace.close()

    assert attempts == []


def test_a_remote_reference_is_indexed_without_being_fetched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[str] = []

    def denied(*args: object, **kwargs: object) -> Any:
        attempts.append("network")
        raise OSError("network denied")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(socket, "create_connection", denied)

    from repoatlas.parsers.structured import StructuredParser

    parsed = StructuredParser().parse(
        Path("openapi.yaml"),
        "openapi: 3.0.0\nservers:\n  - url: https://api.example.com\npaths:\n"
        "  /remote:\n    $ref: 'https://evil.example/paths.yaml#/remote'\n",
    )
    assert attempts == []
    assert not [r for r in parsed.references if r.target_key.endswith("/remote")]
