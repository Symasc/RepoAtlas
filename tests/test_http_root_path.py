"""The root path is one canonical representation in preview, candidate lookup and sync (M1).

The parsers record a request to the root as the segments ``("",)``, and endpoint matching
compares segment counts, so ``GET /`` matches a served ``/``. The declaration parser read the same
root as ``()``, so a preview could refuse a declaration the sync would honour. The fixtures are
neutral: every host and name is invented.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace
from typing import Any

import pytest
from repoatlas.capabilities.http_connections import _shortlist
from repoatlas.capabilities.service import CapabilityService
from repoatlas.graph.http_declarations import Target, parse_target
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.deployment_targets import (
    DeclarationProvenance,
    DeploymentLinkCreateRequest,
    DeploymentLinkFields,
)
from repoatlas.models.http_connections import (
    HttpConnectionPreviewRequest,
    HttpConnectionsRequest,
)
from repoatlas.services.workspace import Workspace
from tests.http_connections_corpus import PYPROJECT
from tests.impact_corpus import build
from tests.incremental_corpus import sync_all

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)
ROOT_URL = "https://root.example.test"

ROOT_SERVICE = dedent("""\
    from fastapi import FastAPI

    app = FastAPI()


    @app.get("/")
    def home() -> dict:
        return {}
""")
ROOT_CALLER = dedent("""\
    import requests


    def fetch_root() -> dict:
        return requests.get("https://root.example.test/", timeout=5).json()
""")


@pytest.fixture
def root_built(workspace: Workspace, tmp_path: Path) -> Workspace:
    return build(
        workspace,
        tmp_path,
        {
            "rootsvc": {
                "pyproject.toml": PYPROJECT.format(name="rootsvc", dep="fastapi"),
                "rootsvc/__init__.py": "",
                "rootsvc/main.py": ROOT_SERVICE,
            },
            "rootcaller": {
                "pyproject.toml": PYPROJECT.format(name="rootcaller", dep="requests"),
                "rootcaller/__init__.py": "",
                "rootcaller/client.py": ROOT_CALLER,
            },
        },
    )


def _slug(workspace: Workspace, name: str) -> str:
    return next(r.slug for r in workspace.repositories.list_all() if r.name == name)


def _service(workspace: Workspace) -> CapabilityService:
    return CapabilityService(workspace)


def test_a_root_request_previews_as_the_sync_decides(root_built: Workspace, tmp_path: Path) -> None:
    """R1: a stated origin requests ``/``; the service serves ``/``. The preview must not refuse
    a declaration of that origin that the sync then honours."""
    service = _service(root_built)
    worklist = service.http_connections(HttpConnectionsRequest(context=GLOBAL))
    group = next(g for g in worklist.groups if g.origin == ROOT_URL)
    preview = service.http_connection_preview(
        HttpConnectionPreviewRequest(
            context=GLOBAL, group=group.key, repository=_slug(root_built, "rootsvc")
        )
    )
    assert preview.counts == {"would-link": 1}
    assert preview.can_create and preview.requirements == ()


def test_a_root_declaration_is_linked_by_the_sync_the_preview_said_would_link(
    root_built: Workspace, tmp_path: Path
) -> None:
    """R1, the other half: the declaration the preview offers is the one the sync joins."""
    service = _service(root_built)
    worklist = service.http_connections(HttpConnectionsRequest(context=GLOBAL))
    group = next(g for g in worklist.groups if g.origin == ROOT_URL)
    preview = service.http_connection_preview(
        HttpConnectionPreviewRequest(
            context=GLOBAL, group=group.key, repository=_slug(root_built, "rootsvc")
        )
    )
    service.deployment_link_create(
        DeploymentLinkCreateRequest(
            context=CapabilityContext(
                scope=CapabilityScope.REPOSITORY, repository=_slug(root_built, "rootsvc")
            ),
            link=DeploymentLinkFields(label="mapped", url=ROOT_URL, callers=[]),
            provenance=DeclarationProvenance(via="ui"),
        )
    )
    sync_all(root_built, tmp_path / "repos")
    decided = {
        row[0]: row[1]
        for row in root_built.database.execute(
            "SELECT decision_ref, decision FROM http_call_decisions"
        ).fetchall()
    }
    for row in preview.rows:
        assert (decided[row.decision_ref] == "linked") == (row.outcome == "would-link"), row


def test_parse_target_root_forms() -> None:
    """The root is ``("",)`` however it is stated, as the parsers record it; a base expression
    with no path after it reads no path at all, and is never the root."""
    assert parse_target("https://root.example.test/").segments == ("",)
    assert parse_target("https://root.example.test").segments == ("",)
    assert parse_target("/").segments == ("",)
    assert parse_target("{b}/").segments == ("",)
    assert parse_target("{b}").segments == ()
    assert parse_target("{b}").base_expression == "{b}"


NON_ROOT = [
    ("https://api.example.test/api/users", ("api", "users")),
    ("https://api.example.test/api/users/", ("api", "users", "")),
    ("https://api.example.test:8443/v1/items/{id}", ("v1", "items", None)),
    ("https://api.example.test/a/b/c/d", ("a", "b", "c", "d")),
    ("https://api.example.test/a//b", ("a", "", "b")),
    (
        "https://api.example.test/files/3f2a9c1e-7b4d-4e8a-9c3b-1a2b3c4d5e6f",
        ("files", "3f2a9c1e-7b4d-4e8a-9c3b-1a2b3c4d5e6f"),
    ),
    ("{base}/users", ("users",)),
    ("{base}/users/{id}/posts", ("users", None, "posts")),
    ("{base}/users/", ("users", "")),
    ("{base}/{path}", (None,)),
    ("{base}/v1/{id}/", ("v1", None, "")),
    ("/api/users", ("api", "users")),
    ("/a/", ("a", "")),
    ("/users/{id}", ("users", None)),
    ("users/list", ()),
    ("", ()),
]


@pytest.mark.parametrize(("url", "segments"), NON_ROOT)
def test_non_root_targets_are_unchanged(url: str, segments: tuple[str | None, ...]) -> None:
    """Pinned from the behaviour before the root became ``("",)``: a trailing slash stays an
    empty last segment, a hole stays ``None``, a bare relative path reads nothing."""
    assert parse_target(url).segments == segments


@pytest.mark.parametrize(
    ("segments", "route", "expected"),
    [
        # A route with no path ("GET ") is only reachable through a base that ends in "/" or
        # holds an empty segment; no such base may be offered.
        (("",), "GET ", []),
        (("a", ""), "GET ", []),
        (("a", "", "b"), "GET ", []),
        # The root request still shortlists the service that serves "/", with the root as base.
        (("",), "GET /", [(1, "svc", "")]),
        (("a", ""), "GET /a/", [(1, "svc", "")]),
    ],
)
def test_shortlist_never_offers_an_empty_base_segment(
    segments: tuple[str | None, ...], route: str, expected: list[tuple[int, str, str]]
) -> None:
    # Only a request's target and method are read, so a stand-in is enough.
    member: Any = SimpleNamespace(
        target=Target(None, None, segments), row=SimpleNamespace(method="GET")
    )
    found = _shortlist([member], {"svc": [route]})
    assert found == expected
    for _, _, base in found:
        assert base != "//"
        assert base == "" or "" not in base[1:].split("/"), base


TS_ROOT_SERVICE = dedent("""\
    import Fastify from 'fastify';

    const server = Fastify();

    server.get('/', async () => ({}));

    export default server;
""")
TS_ROOT_CALLER = dedent("""\
    export async function fetchRoot(): Promise<unknown> {
      const response = await fetch('https://root.example.test/');
      return response.json();
    }
""")


def test_a_typescript_root_request_previews_as_the_sync_decides(
    tmp_path: Path, workspace: Workspace
) -> None:
    """R1 for TypeScript: fetch of the stated origin's root against a Fastify ``GET /``. The
    preview and the sync must agree on the same row, as they do for Python."""
    built = build(
        workspace,
        tmp_path,
        {
            "tsroot": {"package.json": '{"name": "tsroot"}\n', "src/server.ts": TS_ROOT_SERVICE},
            "tscaller": {
                "package.json": '{"name": "tscaller"}\n',
                "src/client.ts": TS_ROOT_CALLER,
            },
        },
    )
    service = _service(built)
    worklist = service.http_connections(HttpConnectionsRequest(context=GLOBAL))
    group = next(g for g in worklist.groups if g.origin == ROOT_URL)
    preview = service.http_connection_preview(
        HttpConnectionPreviewRequest(
            context=GLOBAL, group=group.key, repository=_slug(built, "tsroot")
        )
    )
    service.deployment_link_create(
        DeploymentLinkCreateRequest(
            context=CapabilityContext(
                scope=CapabilityScope.REPOSITORY, repository=_slug(built, "tsroot")
            ),
            link=DeploymentLinkFields(label="mapped", url=ROOT_URL, callers=[]),
            provenance=DeclarationProvenance(via="ui"),
        )
    )
    sync_all(built, tmp_path / "repos")
    decided = {
        row[0]: row[1]
        for row in built.database.execute(
            "SELECT decision_ref, decision FROM http_call_decisions"
        ).fetchall()
    }
    assert preview.rows, "the TypeScript root request must reach the preview"
    for row in preview.rows:
        assert (decided[row.decision_ref] == "linked") == (row.outcome == "would-link"), row
