"""The HTTP linkage fixture corpus, as a test helper (HTTP linkage).

Kept apart from ``tests/conftest.py`` because these repositories are indexed on
their own: the general fixture corpora feed checksummed evaluations, and nothing
HTTP-shaped may leak into them.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable
from pathlib import Path

from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.parsing import EntityType
from repoatlas.models.relationships import (
    EntityRef,
    RelationshipDirection,
    RelationshipsRequest,
    RelationshipsResponse,
)
from repoatlas.services.workspace import Workspace, open_workspace
from tests.conftest import init_git_repository

HTTP_FIXTURES = Path(__file__).parent / "fixtures" / "http"
HTTP_REPOSITORIES = ("api-a", "api-b", "api-c", "api-d", "decoys", "pyclient", "web")

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)

HTTP_EDGE_TYPES = frozenset(
    {
        "SERVES_HTTP_ROUTE",
        "CALLS_HTTP_ROUTE",
        "DECLARES_HTTP_DEPLOYMENT",
        "EXPOSES_HTTP_ENDPOINT",
        "CALLS_HTTP_ENDPOINT",
    }
)
HTTP_ENTITY_TYPES = frozenset({"http_route", "http_deployment", "http_endpoint"})


def copy_corpus(root: Path, names: Iterable[str] = HTTP_REPOSITORIES) -> Path:
    """Copy the named fixture repositories under ``root`` and give them history."""
    root.mkdir(parents=True, exist_ok=True)
    for name in names:
        shutil.copytree(HTTP_FIXTURES / name, root / name)
        init_git_repository(root / name)
    return root


def corpus_settings(state: Path) -> Settings:
    return Settings.model_validate(
        {
            "storage": {"database": str(state / "atlas.db")},
            "embeddings": {"provider": "hashing", "dim": 128},
        }
    )


def index_corpus(tmp: Path, names: Iterable[str] = HTTP_REPOSITORIES) -> Workspace:
    """A workspace over a fresh copy of the corpus, fully indexed."""
    from repoatlas.services.init_service import initialize

    root = copy_corpus(tmp / "repos", names)
    workspace = open_workspace(settings=corpus_settings(tmp / "state"), require_root=False)
    initialize(workspace, root, config_path=tmp / "config.yaml")
    return workspace


def end(ref: EntityRef) -> str:
    """One end of an edge, spelled for a closed expectation set.

    A shared HTTP node is named by its natural key. A repository-owned end is named
    by where it lives — ``web/src/api.ts::helloAbsolute`` — rather than by its
    positional ``sym:`` key, which embeds a line number the assertion does not care
    about.
    """
    if ref.repository_slug is None:
        return ref.key
    location = f"{ref.repository_slug}/{ref.path}"
    return location if ref.type is EntityType.FILE else f"{location}::{ref.name}"


def relationships(
    workspace: Workspace,
    target: str,
    *,
    direction: RelationshipDirection = RelationshipDirection.BOTH,
    hops: int = 1,
    cross_repository: bool = False,
    investigation_id: str | None = None,
    service: CapabilityService | None = None,
) -> RelationshipsResponse:
    """Ask ``relationships``. Pass ``service`` to stay in its investigations."""
    return (service or CapabilityService(workspace)).relationships(
        RelationshipsRequest(
            context=GLOBAL,
            target=target,
            direction=direction,
            hops=hops,
            cross_repository=cross_repository,
            investigation_id=investigation_id,
        )
    )


def edge_set(response: RelationshipsResponse) -> set[tuple[str, str, str]]:
    return {(end(e.source), str(e.type), end(e.target)) for e in response.edges}


def stored_http_edges(workspace: Workspace) -> set[tuple[str, str, str]]:
    """Every HTTP edge in the index, by natural keys, straight from storage."""
    rows = workspace.database.execute(
        "SELECT src.key AS src, e.type AS type, dst.key AS dst FROM edges e "
        "JOIN entities src ON src.id = e.src_id JOIN entities dst ON dst.id = e.dst_id "
        "WHERE e.type IN (?, ?, ?, ?, ?)",
        tuple(sorted(HTTP_EDGE_TYPES)),
    ).fetchall()
    return {(row["src"], row["type"], row["dst"]) for row in rows}


def entity_key(workspace: Workspace, repository: str, name: str) -> str:
    """The key of the one repository-owned entity with this name."""
    rows = workspace.database.execute(
        "SELECT e.key FROM entities e JOIN repositories r ON r.id = e.repo_id "
        "WHERE r.slug = ? AND e.name = ? AND e.key LIKE 'sym:%'",
        (repository, name),
    ).fetchall()
    assert len(rows) == 1, (repository, name, [row["key"] for row in rows])
    return str(rows[0]["key"])


def http_nodes(workspace: Workspace) -> set[str]:
    rows = workspace.database.execute(
        "SELECT key FROM entities WHERE type IN (?, ?, ?)",
        tuple(sorted(HTTP_ENTITY_TYPES)),
    ).fetchall()
    return {row["key"] for row in rows}
