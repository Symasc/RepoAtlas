"""``USES`` is added beside the graph, never into it: search answers exactly as before.

Declaration-level uses outnumber every other relationship, and ordinary search must
not change because they exist. That holds by construction, and these tests hold the
construction:

* indexing with and without them yields the same facts in every other table and edge
  type, so nothing that existed before was changed to make room for them;
* search, and ``relationships`` asked for no edge type, answer byte for byte the same;
* ``USES`` is nameable, never expanded automatically, and never crosses a repository;
* every query that reads edges is either typed or reviewed here, so no future query
  can take them in by forgetting a type filter.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from repoatlas.capabilities.presentation import present
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.graph.traversal import CROSS_REPOSITORY_EDGES, EXPANSION_EDGES, RELATIONSHIP_EDGES
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.parsing import EdgeType
from repoatlas.models.relationships import RelationshipDirection, RelationshipsRequest
from repoatlas.models.search import SearchRequest
from repoatlas.services.workspace import Workspace, open_workspace
from tests.conftest import fact_snapshot
from tests.impact_corpus import SYMBOLS, build

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)
REPOATLAS = Path(__file__).resolve().parents[1] / "repoatlas"

#: A second repository with components, environment reads and a service boundary.
APP = {
    "package.json": json.dumps({"name": "app", "private": True}),
    "src/config.ts": "export const API_URL = process.env.API_URL ?? 'http://localhost';\n",
    "src/client.ts": (
        'import { API_URL } from "./config";\n'
        "export async function load(id: string) {\n"
        "  return fetch(`${API_URL}/items/${id}`);\n"
        "}\n"
    ),
    "src/Item.tsx": (
        'import { load } from "./client";\n'
        "export function Item({ id }: { id: string }) {\n"
        "  void load(id);\n"
        "  return <span>{id}</span>;\n"
        "}\n"
    ),
    "src/List.tsx": (
        'import { Item } from "./Item";\nexport const List = () => <ul><Item id="1" /></ul>;\n'
    ),
}

QUESTIONS = (
    "LIMIT",
    "where is the item limit defined",
    "cap",
    "check",
    "Shape",
    "defaultRule",
    "src/limits.ts",
    "API_URL",
    "load",
    "Item component",
    "what reads API_URL",
)


def _no_uses(*_: object) -> list[object]:
    return []


@pytest.fixture
def pair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Workspace, Workspace]]:
    """The same repositories indexed twice: as shipped, and with ``USES`` switched off."""
    opened = []

    def index(tag: str) -> Workspace:
        settings = Settings.model_validate(
            {
                "storage": {"database": str(tmp_path / tag / "atlas.db")},
                "embeddings": {"provider": "hashing", "dim": 128},
            }
        )
        workspace = open_workspace(settings=settings, require_root=False)
        opened.append(workspace)
        return build(workspace, tmp_path / tag, {"symbols": SYMBOLS, "app": APP})

    with_uses = index("with")
    monkeypatch.setattr("repoatlas.parsers.typescript.use_references", _no_uses)
    without = index("without")
    yield with_uses, without
    for workspace in opened:
        workspace.close()


def _without_uses(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        **snapshot,
        "edges": [row for row in snapshot["edges"] if row[0] != "USES"],
        "unresolved_refs": [row for row in snapshot["unresolved_refs"] if row[2] != "USES"],
    }


#: What differs between two builds of the same facts: when each was built, and the
#: investigation each answer opened.
_WHEN = frozenset({"investigation_id", "last_indexed_at"})


def _stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in value.items() if k not in _WHEN}
    if isinstance(value, list):
        return [_stable(v) for v in value]
    return value


def _answer(payload: dict[str, Any]) -> str:
    return json.dumps(_stable(payload), sort_keys=True)


def test_uses_were_actually_indexed(pair: tuple[Workspace, Workspace]) -> None:
    with_uses, without = pair
    count = "SELECT COUNT(*) FROM edges WHERE type = 'USES'"
    assert with_uses.database.execute(count).fetchone()[0] > 10
    assert without.database.execute(count).fetchone()[0] == 0


def test_every_other_fact_is_unchanged(pair: tuple[Workspace, Workspace]) -> None:
    with_uses, without = pair
    assert _without_uses(fact_snapshot(with_uses)) == _without_uses(fact_snapshot(without))
    # and the edges USES added carry no resolution or producer another edge uses
    for workspace in (with_uses, without):
        assert not workspace.database.execute(
            "SELECT 1 FROM edges WHERE type != 'USES' AND resolution LIKE '%\"use\":%'"
        ).fetchall()


def test_search_answers_byte_for_byte_the_same(pair: tuple[Workspace, Workspace]) -> None:
    services = [CapabilityService(workspace) for workspace in pair]
    for question in QUESTIONS:
        request = SearchRequest(question=question, context=GLOBAL)
        first, second = (present(request, service.search(request)) for service in services)
        assert _answer(first) == _answer(second), question


def test_relationships_asked_for_no_type_answer_the_same(pair: tuple[Workspace, Workspace]) -> None:
    with_uses, _ = pair
    keys = [
        row[0]
        for row in with_uses.database.execute(
            "SELECT DISTINCT d.key FROM edges e JOIN entities d ON d.id = e.dst_id "
            "WHERE e.type = 'USES' ORDER BY d.key"
        ).fetchall()
    ]
    assert len(keys) >= 8
    services = [CapabilityService(workspace) for workspace in pair]
    for key in keys:
        for hops in (1, 2):
            request = RelationshipsRequest(context=GLOBAL, target=key, hops=hops)
            first, second = (present(request, s.relationships(request)) for s in services)
            assert _answer(first) == _answer(second), key


def test_an_explicit_request_for_uses_returns_them(pair: tuple[Workspace, Workspace]) -> None:
    with_uses, _ = pair
    [key] = [
        row[0]
        for row in with_uses.database.execute(
            "SELECT key FROM entities WHERE qualified_name = 'LIMIT' AND type = 'symbol'"
        ).fetchall()
    ]
    request = RelationshipsRequest(
        context=GLOBAL,
        target=key,
        edge_types=(EdgeType.USES,),
        direction=RelationshipDirection.INCOMING,
    )
    payload = present(request, CapabilityService(with_uses).relationships(request))
    text = json.dumps(payload)
    assert '"USES"' in text and "withMember" in text and "check" in text


def test_uses_is_nameable_never_expanded_and_never_crossing() -> None:
    assert EdgeType.USES in RELATIONSHIP_EDGES
    assert EdgeType.USES not in EXPANSION_EDGES
    assert EdgeType.USES not in CROSS_REPOSITORY_EDGES


# -- the audit: every read of the edges table ------------------------------------

#: Store methods that read ``edges``, and why each cannot take in a type it did not ask for.
REVIEWED_EDGE_READS = {
    "replace_file_facts": "maintenance: deletes one file's facts",
    "delete_edges_by_parser": "maintenance: deletes one producer's edges",
    "list_exported_names": "typed: EXPORTS",
    "list_served_routes": "typed: SERVES_HTTP_ROUTE",
    "list_http_edges": "typed: the HTTP edge types",
    "neighbors": "typed by its caller: the audit below requires edge_types",
    "expand_neighbors": "typed by its caller: the audit below requires edge_types",
    "gc_orphan_entities": "maintenance: reference check, no answer",
    "count_edges": "status total: counts every indexed relationship, reads none",
    "_count": "status total: the row count behind count_edges and its siblings",
    "_impact_scope": "typed: the edge types of an impact rule, confined in SQL",
}


def _functions_reading_edges(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            text = ast.get_source_segment(path.read_text(), node) or ""
            if any(s in text for s in ("FROM edges", "JOIN edges", "{table}")) and (
                "edges" in text
            ):
                found.add(node.name)
    return found


def test_every_query_reading_edges_is_reviewed() -> None:
    stores = REPOATLAS / "storage/sqlite/stores.py"
    assert _functions_reading_edges(stores) <= set(REVIEWED_EDGE_READS)
    others = [
        path
        for path in REPOATLAS.rglob("*.py")
        if path != stores and ("FROM edges" in path.read_text() or "JOIN edges" in path.read_text())
    ]
    assert others == []


def test_every_neighbour_query_names_its_edge_types() -> None:
    calls = []
    for path in REPOATLAS.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"neighbors", "expand_neighbors"}
            ):
                named = any(k.arg == "edge_types" for k in node.keywords) or len(node.args) >= 2
                calls.append((path.name, node.lineno, named))
    assert calls and all(named for _, _, named in calls), calls
