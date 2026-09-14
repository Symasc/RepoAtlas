"""HTTP facts across sync: change, deletion, arrival order (brief §51-54, §83-84).

A shared node is where two repositories meet, so a stale edge onto one is a
stale *connection*: a client left attached to an endpoint its provider no longer
declares would read as a live link. Each test changes one thing, syncs
incrementally, asserts the exact neighbourhood of the nodes involved, and then
proves the whole state equals a from-scratch index of the same tree — the
invariant every other sync test in this project rests on.
"""

from __future__ import annotations

from pathlib import Path

from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace
from tests.conftest import fact_snapshot, git, init_git_repository, reindex_from_scratch
from tests.http_corpus import corpus_settings, edge_set, http_nodes, relationships

SPEC = """\
openapi: 3.0.3
info:
  title: svc
  version: "1"
servers:
  - url: https://api.example.com
paths:
  {path}:
    get:
      responses:
        "200":
          description: ok
"""
APP = (
    "from fastapi import FastAPI\n\napp = FastAPI()\n\n\n"
    '@app.get("/hello")\ndef hello():\n    return {}\n'
)
CLIENT = 'export async function call() {{\n    return fetch("https://api.example.com{path}");\n}}\n'

HELLO = "http-endpoint:GET:https://api.example.com/hello"
GOODBYE = "http-endpoint:GET:https://api.example.com/goodbye"


def _commit(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", message)


def _setup(tmp_path: Path) -> tuple[Workspace, Path]:
    from repoatlas.services.init_service import initialize

    root = tmp_path / "repos"
    (root / "svc" / "app").mkdir(parents=True)
    (root / "svc" / "openapi.yaml").write_text(SPEC.format(path="/hello"))
    (root / "svc" / "app" / "main.py").write_text(APP)
    (root / "web" / "src").mkdir(parents=True)
    (root / "web" / "src" / "call.ts").write_text(CLIENT.format(path="/hello"))
    init_git_repository(root / "svc")
    init_git_repository(root / "web")
    workspace = open_workspace(settings=corpus_settings(tmp_path / "state"), require_root=False)
    initialize(workspace, root, config_path=tmp_path / "config.yaml")
    return workspace, root


def _sync(workspace: Workspace, root: Path) -> None:
    sync(workspace, workspace.resolver().resolve(root))


def _equals_full_reindex(workspace: Workspace, root: Path, tmp_path: Path, tag: str) -> None:
    fresh = reindex_from_scratch(corpus_settings(tmp_path / "unused"), root, tmp_path, tag)
    assert fact_snapshot(workspace) == fresh


def test_a_provider_change_detaches_the_provider_and_leaves_the_client_where_it_was(
    tmp_path: Path,
) -> None:
    workspace, root = _setup(tmp_path)
    try:
        assert edge_set(relationships(workspace, HELLO)) == {
            ("svc/openapi.yaml", "EXPOSES_HTTP_ENDPOINT", HELLO),
            ("web/src/call.ts::call", "CALLS_HTTP_ENDPOINT", HELLO),
        }

        (root / "svc" / "openapi.yaml").write_text(SPEC.format(path="/goodbye"))
        _commit(root / "svc", "rename the operation")
        _sync(workspace, root)

        # The old provider edge is gone; the client's call is unchanged and is now
        # the only thing attached to the old endpoint.
        assert edge_set(relationships(workspace, HELLO)) == {
            ("web/src/call.ts::call", "CALLS_HTTP_ENDPOINT", HELLO),
        }
        assert edge_set(relationships(workspace, GOODBYE)) == {
            ("svc/openapi.yaml", "EXPOSES_HTTP_ENDPOINT", GOODBYE),
        }
        _equals_full_reindex(workspace, root, tmp_path, "after-provider")

        (root / "web" / "src" / "call.ts").write_text(CLIENT.format(path="/goodbye"))
        _commit(root / "web", "follow the rename")
        _sync(workspace, root)

        assert edge_set(relationships(workspace, GOODBYE)) == {
            ("svc/openapi.yaml", "EXPOSES_HTTP_ENDPOINT", GOODBYE),
            ("web/src/call.ts::call", "CALLS_HTTP_ENDPOINT", GOODBYE),
        }
        # Nothing refers to the old endpoint any more, so it no longer exists.
        assert HELLO not in http_nodes(workspace)
        _equals_full_reindex(workspace, root, tmp_path, "after-client")
    finally:
        workspace.close()


def test_deleting_the_specification_removes_its_facts_and_keeps_the_source_route(
    tmp_path: Path,
) -> None:
    workspace, root = _setup(tmp_path)
    try:
        (root / "svc" / "openapi.yaml").unlink()
        _commit(root / "svc", "drop the specification")
        _sync(workspace, root)

        nodes = http_nodes(workspace)
        assert "http-deployment:https://api.example.com" not in nodes
        # The route the framework decorator serves does not depend on the spec.
        assert edge_set(relationships(workspace, "http-route:GET:/hello")) == {
            ("svc/app/main.py::hello", "SERVES_HTTP_ROUTE", "http-route:GET:/hello"),
            ("web/src/call.ts::call", "CALLS_HTTP_ROUTE", "http-route:GET:/hello"),
        }
        # The endpoint survives only as what the client calls.
        assert edge_set(relationships(workspace, HELLO)) == {
            ("web/src/call.ts::call", "CALLS_HTTP_ENDPOINT", HELLO),
        }
        _equals_full_reindex(workspace, root, tmp_path, "after-delete")
    finally:
        workspace.close()


def test_a_second_provider_arriving_later_joins_the_same_node(tmp_path: Path) -> None:
    workspace, root = _setup(tmp_path)
    try:
        other = root / "svc2"
        other.mkdir()
        (other / "openapi.yaml").write_text(SPEC.format(path="/hello"))
        init_git_repository(other)
        _sync(workspace, root)

        sources = {source for source, _, _ in edge_set(relationships(workspace, HELLO))}
        assert sources == {"svc/openapi.yaml", "svc2/openapi.yaml", "web/src/call.ts::call"}
        _equals_full_reindex(workspace, root, tmp_path, "after-arrival")
    finally:
        workspace.close()


def test_repositories_arriving_in_reverse_order_build_the_same_graph(tmp_path: Path) -> None:
    """Determinism: shared identities do not depend on who registered first."""
    from repoatlas.services.init_service import initialize
    from tests.http_corpus import HTTP_REPOSITORIES, copy_corpus

    root = tmp_path / "repos"
    names = sorted(HTTP_REPOSITORIES, reverse=True)
    copy_corpus(root, names[:1])
    workspace = open_workspace(settings=corpus_settings(tmp_path / "state"), require_root=False)
    try:
        initialize(workspace, root, config_path=tmp_path / "config.yaml")
        for name in names[1:]:
            copy_corpus(root, [name])
            _sync(workspace, root)
        _equals_full_reindex(workspace, root, tmp_path, "forward")
    finally:
        workspace.close()


def test_resyncing_an_unchanged_tree_changes_nothing(tmp_path: Path) -> None:
    workspace, root = _setup(tmp_path)
    try:
        before = fact_snapshot(workspace)
        _sync(workspace, root)
        _sync(workspace, root)
        assert fact_snapshot(workspace) == before
    finally:
        workspace.close()
