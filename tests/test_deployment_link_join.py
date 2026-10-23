"""A user's deployment link in the HTTP join: a fallback for what the source leaves unlinked.

``shop`` serves ``GET /v1/items/{item_id}`` and states no deployment ``buyer`` can reach;
``buyer`` requests ``https://shop.example.test``. Recording where ``shop`` is deployed links the
request at the next sync, its identity, binding and source hops ``declared`` on the link;
removing the link takes it back. A link never overrides what the source establishes, and
links nothing when two of its repository's handlers would answer.
"""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.deployment_targets import (
    DeploymentLinkCreateRequest,
    DeploymentLinkDeleteRequest,
    DeploymentLinkFields,
    DeploymentTargetsRequest,
)
from repoatlas.models.impact import ImpactRequest
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build
from tests.incremental_corpus import sync_all

APP = dedent("""\
    from fastapi import FastAPI

    app = FastAPI()


    @app.get("/v1/items/{item_id}")
    def get_item(item_id: str) -> dict:
        return {"id": item_id}
""")
PYPROJECT = '[project]\nname = "{name}"\nversion = "1.0.0"\ndependencies = ["fastapi"]\n'
#: A chart whose Ingress host no release sets: its target needs a link's value.
CHART = {
    "charts/shop/Chart.yaml": "apiVersion: v2\nname: shop\nversion: 1.0.0\n",
    "charts/shop/values.yaml": 'host: ""\n',
    "charts/shop/templates/ingress.yaml": dedent("""\
        apiVersion: networking.k8s.io/v1
        kind: Ingress
        metadata:
          name: shop
        spec:
          tls:
            - hosts:
                - {{ .Values.host }}
          rules:
            - host: {{ .Values.host }}
              http:
                paths:
                  - path: /
                    pathType: Prefix
                    backend:
                      service:
                        name: shop
                        port:
                          number: 80
    """),
}
COMPOSE = {
    "docker-compose.yml": dedent("""\
        services:
          svc:
            build:
              context: .
              dockerfile: Dockerfile
            ports:
              - "8080:8080"
    """),
    "Dockerfile": (
        "FROM python:3.12-slim\nWORKDIR /srv\nCOPY svc /srv/svc\n"
        'CMD ["uvicorn", "svc.main:app", "--host", "0.0.0.0", "--port", "8080"]\n'
    ),
}
BUYER = dedent("""\
    import requests


    def fetch(item_id: str) -> dict:
        return requests.get(f"https://shop.example.test/v1/items/{item_id}", timeout=5).json()


    def fetch_svc(item_id: str) -> dict:
        return requests.get(f"http://svc:8080/v1/items/{item_id}", timeout=5).json()
""")


def repos() -> dict[str, dict[str, str]]:
    return {
        "shop": {
            "pyproject.toml": PYPROJECT.format(name="shop"),
            "shop/__init__.py": "",
            "shop/main.py": APP,
            **CHART,
        },
        "twin": {
            "pyproject.toml": PYPROJECT.format(name="twin"),
            "twin/__init__.py": "",
            "twin/a.py": APP,
            "twin/b.py": APP,
        },
        "svc": {
            "pyproject.toml": PYPROJECT.format(name="svc"),
            "svc/__init__.py": "",
            "svc/main.py": APP,
            **COMPOSE,
        },
        "buyer": {
            "pyproject.toml": PYPROJECT.format(name="buyer").replace("fastapi", "requests"),
            "buyer/__init__.py": "",
            "buyer/client.py": BUYER,
        },
    }


def _context(workspace: Workspace, name: str) -> CapabilityContext:
    slug = next(r.slug for r in workspace.repositories.list_all() if r.name == name)
    return CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=slug)


def link(workspace: Workspace, name: str, **fields: Any) -> int:
    return (
        CapabilityService(workspace)
        .deployment_link_create(
            DeploymentLinkCreateRequest(
                context=_context(workspace, name),
                link=DeploymentLinkFields(label="production", **fields),
            )
        )
        .link.id
    )


def decided(workspace: Workspace, caller: str) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    [(decision, chain, evidence)] = workspace.database.execute(
        """SELECT d.decision, d.chain_json, d.evidence_json FROM http_call_decisions d
           JOIN entities e ON e.id = d.src_id WHERE e.qualified_name = ?""",
        (caller,),
    ).fetchall()
    return str(decision), json.loads(chain), json.loads(evidence)


def handler_of(workspace: Workspace, caller: str) -> str | None:
    found = workspace.database.execute(
        """SELECT f.rel_path, r.slug FROM edges x JOIN entities s ON s.id = x.src_id
           JOIN entities h ON h.id = x.dst_id JOIN files f ON f.id = h.file_id
           JOIN repositories r ON r.id = f.repo_id
           WHERE x.type = 'CALLS_HTTP_HANDLER' AND s.qualified_name = ?""",
        (caller,),
    ).fetchall()
    return f"{found[0][1]}/{found[0][0]}" if found else None


def test_a_link_joins_what_the_source_leaves_unlinked_until_it_is_removed(
    workspace: Workspace, tmp_path: Path
) -> None:
    built = build(workspace, tmp_path, repos())
    before, _, _ = decided(built, "fetch")
    assert before != "linked" and handler_of(built, "fetch") is None

    stated = link(built, "shop", url="https://shop.example.test")
    # stored now, joined at the next sync: the link pass is part of a sync
    assert handler_of(built, "fetch") is None
    sync_all(built, tmp_path / "repos")

    decision, chain, evidence = decided(built, "fetch")
    assert decision == "linked"
    assert handler_of(built, "fetch") == "shop/shop/main.py"
    statuses = {hop["kind"]: hop["status"] for hop in chain}
    assert statuses["service_identity"] == statuses["deployment_binding"] == "declared"
    assert statuses["source_binding"] == "declared"
    assert statuses["route"] == statuses["handler"] == "proven"
    [place] = next(h for h in chain if h["kind"] == "service_identity")["provenance"]
    assert (place["path"], place["basis"], place["line_start"]) == (
        f"@deployment-links/{stated}",
        "link",
        None,
    )
    assert evidence["identity_source"] == "deployment-link"
    assert evidence["link_id"] == str(stated)

    # impact follows it from the handler to the request
    shop_handler = built.database.execute(
        "SELECT key FROM entities WHERE qualified_name = 'get_item' AND key LIKE 'sym:shop:%'"
    ).fetchone()[0]
    impact = CapabilityService(built).impact(
        ImpactRequest(context=CapabilityContext(scope=CapabilityScope.GLOBAL), seed=shop_handler)
    )
    assert "fetch" in {c.name for c in impact.candidates}

    CapabilityService(built).deployment_link_delete(
        DeploymentLinkDeleteRequest(context=_context(built, "shop"), id=stated)
    )
    sync_all(built, tmp_path / "repos")
    assert decided(built, "fetch")[0] == before
    assert handler_of(built, "fetch") is None


def test_a_link_naming_a_stated_target_joins_at_the_url_its_values_resolve(
    workspace: Workspace, tmp_path: Path
) -> None:
    built = build(workspace, tmp_path, repos())
    response = CapabilityService(built).deployment_targets(
        DeploymentTargetsRequest(context=_context(built, "shop"))
    )
    [target] = [t for t in response.targets if t.kind == "ingress"]
    link(built, "shop", target_id=target.target_id, variables={"host": "shop.example.test"})
    sync_all(built, tmp_path / "repos")
    assert decided(built, "fetch")[0] == "linked"
    assert handler_of(built, "fetch") == "shop/shop/main.py"


def test_a_link_never_overrides_what_the_source_establishes(
    workspace: Workspace, tmp_path: Path
) -> None:
    """``svc``'s compose service publishes ``http://svc:8080``: the source links
    ``fetch_svc`` there. A link saying ``shop`` is at that origin changes nothing."""
    built = build(workspace, tmp_path, repos())
    assert handler_of(built, "fetch_svc") == "svc/svc/main.py"
    link(built, "shop", url="http://svc:8080")
    sync_all(built, tmp_path / "repos")
    decision, chain, _ = decided(built, "fetch_svc")
    assert decision == "linked" and handler_of(built, "fetch_svc") == "svc/svc/main.py"
    assert all(hop["status"] != "declared" for hop in chain)


def test_a_link_whose_repository_answers_twice_links_nothing(
    workspace: Workspace, tmp_path: Path
) -> None:
    built = build(workspace, tmp_path, repos())
    before = decided(built, "fetch")[0]
    link(built, "twin", url="https://shop.example.test")
    sync_all(built, tmp_path / "repos")
    assert decided(built, "fetch")[0] == before
    assert handler_of(built, "fetch") is None


def test_links_export_and_import_outlive_the_index(
    workspace: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The links are the one thing a user writes into the index: exported, they come back
    after the index loses them, and importing them twice adds nothing."""
    from repoatlas.cli.main import app
    from typer.testing import CliRunner

    built = build(workspace, tmp_path, repos())
    stated = link(built, "shop", url="https://shop.example.test", environment="prod")
    monkeypatch.setenv("REPOATLAS_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("REPOATLAS_DB", str(tmp_path / "state" / "atlas.db"))
    runner = CliRunner()
    exported = tmp_path / "links.json"

    result = runner.invoke(app, ["links", "export", str(exported)])
    assert result.exit_code == 0, result.output
    document = json.loads(exported.read_text())
    assert document["format"] == "repoatlas/deployment-links/v1"
    [entry] = document["links"]
    assert (entry["repository"], entry["url"], entry["environment"]) == (
        "shop",
        "https://shop.example.test",
        "prod",
    )

    CapabilityService(built).deployment_link_delete(
        DeploymentLinkDeleteRequest(context=_context(built, "shop"), id=stated)
    )
    assert built.deployment_links.list("shop") == []
    result = runner.invoke(app, ["links", "import", str(exported)])
    assert result.exit_code == 0 and "Added 1 link(s); 0 already recorded." in result.output
    [restored] = built.deployment_links.list("shop")
    assert (restored.url, restored.environment) == ("https://shop.example.test", "prod")
    result = runner.invoke(app, ["links", "import", str(exported)])
    assert "Added 0 link(s); 1 already recorded." in result.output

    exported.write_text('{"format": "something-else", "links": []}')
    assert runner.invoke(app, ["links", "import", str(exported)]).exit_code != 0
