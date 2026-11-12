"""``USES`` edges are never automatically expanded by search or relationships.

Declaration-level uses outnumber every other relationship, and ordinary search must
not expand through them. These tests hold that construction:

* the recorded use claims add only the ordinary edges they trace, each with its trace,
  and every difference in the link account is exactly those edges;
* with the ``USES`` edges removed, search, and ``relationships`` asked for no edge type,
  answer byte for byte the same;
* ``USES`` is nameable, never expanded automatically, and never crosses a repository;
* every query that reads edges is either typed or reviewed here, so no future query
  can take them in by forgetting a type filter.

Ordinary calls, references and exports may read the recorded use claims when resolving a
binding (``ReferenceLinker._resolve_via_use``), so the claims are varied in the first test
(``claims_pair``) and the resulting edges are varied in the others (``pair``).
"""

from __future__ import annotations

import ast
import json
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from repoatlas.capabilities.presentation import present
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.graph.traversal import CROSS_REPOSITORY_EDGES, EXPANSION_EDGES, RELATIONSHIP_EDGES
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.ordinary_outcomes import (
    EXCLUDED_PREFIX,
    LEGACY_ORDINARY,
    LEGACY_UNRESOLVED,
    LEGACY_USES_REMAINDER,
    LINKED,
    ORDINARY_FAMILY,
    ORDINARY_LINKED,
    ORDINARY_NOT_LINKED,
    STANDING_CLAIMS,
    USES_ROWS,
    USES_STAGE,
)
from repoatlas.models.parsing import EdgeType
from repoatlas.models.relationships import RelationshipDirection, RelationshipsRequest
from repoatlas.models.search import SearchRequest
from repoatlas.services.sync_service import sync
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


def _index(opened: list[Workspace], tmp_path: Path, tag: str, classify: bool = False) -> Workspace:
    """Index the corpus; ``classify`` also runs the link pass that writes the ordinary account."""
    settings = Settings.model_validate(
        {
            "storage": {"database": str(tmp_path / tag / "atlas.db")},
        }
    )
    workspace = open_workspace(settings=settings, require_root=False)
    opened.append(workspace)
    build(workspace, tmp_path / tag, {"symbols": SYMBOLS, "app": APP})
    if classify:
        sync(workspace, workspace.resolver().resolve(tmp_path / tag / "repos"), classify=True)
    return workspace


@pytest.fixture
def pair(tmp_path: Path) -> Iterator[tuple[Workspace, Workspace]]:
    """The same graph with and without its ``USES`` edges."""
    opened: list[Workspace] = []
    with_uses = _index(opened, tmp_path, "with")
    without = _index(opened, tmp_path, "without")
    with without.database.transaction() as conn:
        conn.execute("DELETE FROM edges WHERE type = 'USES'")
    yield with_uses, without
    for workspace in opened:
        workspace.close()


@pytest.fixture
def claims_pair(tmp_path: Path) -> Iterator[tuple[Workspace, Workspace]]:
    """The same files indexed with the recorded use claims and without any.

    The second is built with the TypeScript parser's ``use_references`` returning
    nothing, so no ``USES`` claim exists to resolve and none can inform an ordinary one.
    """
    opened: list[Workspace] = []
    with_claims = _index(opened, tmp_path, "claims", classify=True)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("repoatlas.parsers.typescript.use_references", lambda *args, **kwargs: [])
        without = _index(opened, tmp_path, "no-claims", classify=True)
    yield with_claims, without
    for workspace in opened:
        workspace.close()


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


#: One stored edge: type, source key, target key, line, asserting file, parser, resolution,
#: and the slug of the repository it is recorded in.
_EdgeRow = tuple[str, str, str, int | None, str, str, str, str]


def _ordinary_edges(workspace: Workspace) -> Counter[_EdgeRow]:
    """Every stored edge but a ``USES`` one, counted by what identifies it."""
    rows = workspace.database.execute(
        "SELECT e.type, s.key, d.key, e.line_start, COALESCE(f.rel_path, ''), "
        "COALESCE(e.parser, ''), COALESCE(e.resolution, ''), COALESCE(r.slug, '') "
        "FROM edges e JOIN entities s ON s.id = e.src_id JOIN entities d ON d.id = e.dst_id "
        "LEFT JOIN files f ON f.id = e.file_id LEFT JOIN repositories r ON r.id = e.repo_id "
        "WHERE e.type != 'USES'"
    ).fetchall()
    return Counter(cast(_EdgeRow, tuple(row)) for row in rows)


def _traced(resolution: str) -> bool:
    """Whether a resolution record is a use's: it names how the name was used."""
    return bool(resolution) and "use" in json.loads(resolution)


def _table(snapshot: dict[str, object], name: str) -> list[tuple[Any, ...]]:
    """One table of a ``fact_snapshot``, as rows."""
    return cast(list[tuple[Any, ...]], snapshot[name])


def _claims(rows: list[tuple[Any, ...]], outcome: str | None = None) -> Counter[tuple[str, str]]:
    """Claims by (repository, edge type) over the rows of ``ordinary_outcomes``, optionally
    only those of one outcome."""
    counted: Counter[tuple[str, str]] = Counter()
    for slug, _language, edge_type, _kind, row_outcome, _subreason, claims in rows:
        if outcome is None or row_outcome == outcome:
            counted[(slug, edge_type)] += claims
    return counted


def test_use_claims_only_add_traced_edges(claims_pair: tuple[Workspace, Workspace]) -> None:
    """The recorded use claims are the only thing that differs, and what they add is
    exactly the ordinary edges they trace.

    ``W+`` is the index with the claims, ``W-`` the same files without. Everything the
    claims cannot touch is equal. A claim ``W-`` leaves open and ``W+`` links is an edge
    in ``W+ - W-``, and each such edge is a call, reference or export that names its use in
    its resolution; the account of ordinary claims differs by those edges and by the
    ``USES`` stage's own figures, and by nothing else.
    """
    with_claims, without = claims_pair
    plus, minus = fact_snapshot(with_claims), fact_snapshot(without)

    # Untouched: what was indexed, and what was claimed that is not a use.
    for table in ("repositories", "files", "entities", "dependencies", "chunks"):
        assert plus[table] == minus[table], table
    claims_plus, claims_minus = _table(plus, "unresolved_refs"), _table(minus, "unresolved_refs")
    assert any(row[2] == "USES" for row in claims_plus)
    assert not any(row[2] == "USES" for row in claims_minus)
    assert [r for r in claims_plus if r[2] != "USES"] == [r for r in claims_minus if r[2] != "USES"]

    # Edges: the claims only add, and what they add is traced.
    plus_edges, minus_edges = _ordinary_edges(with_claims), _ordinary_edges(without)
    assert not minus_edges - plus_edges
    added = plus_edges - minus_edges
    assert added, "the claims linked nothing: the test would pass on any build"
    for edge_type, _src, _dst, _line, _path, _parser, resolution, _slug in added:
        assert edge_type in {"CALLS", "REFERENCES", "EXPORTS"}, edge_type
        assert _traced(resolution), (edge_type, resolution)
    traced = Counter({row: n for row, n in plus_edges.items() if _traced(row[6])})
    assert traced == added
    gained = sum(added.values())

    # Outcomes: the same claims in total, and the linked ones rise by the added edges.
    outcomes_plus = _table(plus, "ordinary_outcomes")
    outcomes_minus = _table(minus, "ordinary_outcomes")
    account_plus = dict(_table(plus, "ordinary_accounting"))
    account_minus = dict(_table(minus, "ordinary_accounting"))
    # Not vacuous: the ledger exists only when the link pass ran classified.
    assert _claims(outcomes_minus, LINKED), "no account written: build not classified"
    assert account_plus and account_minus
    assert any(r[0] == ORDINARY_FAMILY for r in _table(minus, "link_decisions"))
    assert _claims(outcomes_plus) == _claims(outcomes_minus)
    added_by_site: Counter[tuple[str, str]] = Counter()
    for row, count in added.items():
        added_by_site[(row[7], row[0])] += count
    assert _claims(outcomes_plus, LINKED) == _claims(outcomes_minus, LINKED) + added_by_site
    assert sorted(r for r in outcomes_plus if r[4] == "excluded") == sorted(
        r for r in outcomes_minus if r[4] == "excluded"
    )

    # The account: every figure equal but the ones the added edges and the USES stage own.
    uses_rows = account_plus[USES_ROWS]
    remainder = account_plus[LEGACY_USES_REMAINDER]
    assert uses_rows > 0
    assert account_minus[USES_ROWS] == 0 and account_minus[LEGACY_USES_REMAINDER] == 0
    assert account_plus == {
        **account_minus,
        # claims that linked, and so no longer stand open
        ORDINARY_LINKED: account_minus[ORDINARY_LINKED] + gained,
        ORDINARY_NOT_LINKED: account_minus[ORDINARY_NOT_LINKED] - gained,
        LEGACY_ORDINARY: account_minus[LEGACY_ORDINARY] - gained,
        # the USES stage: its claims, and what it left open
        STANDING_CLAIMS: account_minus[STANDING_CLAIMS] + uses_rows,
        USES_ROWS: uses_rows,
        f"{EXCLUDED_PREFIX}{USES_STAGE}": uses_rows,
        LEGACY_USES_REMAINDER: remainder,
        LEGACY_UNRESOLVED: account_minus[LEGACY_UNRESOLVED] - gained + remainder,
    }

    # Decisions: every open site `W+` reports `W-` reports too, and the ones only `W-`
    # reports are ordinary sites of an edge the claims added.
    def sites(snapshot: dict[str, object]) -> Counter[tuple[Any, ...]]:
        # without the evidence column, whose count of claims a shared site may change
        return Counter(
            (r[0], r[1], r[2], r[4], r[5], r[6], r[7], r[8])
            for r in _table(snapshot, "link_decisions")
        )

    reported_plus, reported_minus = sites(plus), sites(minus)
    assert not reported_plus - reported_minus
    closed = reported_minus - reported_plus
    added_sites = {(row[7], row[4], row[3], row[0]) for row in added}
    for family, subject, _decision, slug, path, line, _end, _src in closed:
        assert family == ORDINARY_FAMILY
        assert (slug, path, line, subject.split(":")[0]) in added_sites, subject
    assert sum(closed.values()) <= gained


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
    "list_served_routes": "typed: SERVES_HTTP_ROUTE",
    "list_code_served_routes": "typed: SERVES_HTTP_ROUTE, the routes a link may be offered at",
    "list_route_servers": "typed: SERVES_HTTP_ROUTE, the routes deployment identities expose",
    "list_env_reads": "typed: READS to environment variables, the readers DF3a's crossings reach",
    "reads_in": "typed: READS, whether a repository reads a seed_repository's variable",
    "list_http_edges": "typed: the HTTP edge types",
    "neighbors": "typed by its caller: the audit below requires edge_types",
    "expand_neighbors": "typed by its caller: the audit below requires edge_types",
    "gc_orphan_entities": "maintenance: reference check, no answer",
    "count_edges": "status total: counts every indexed relationship, reads none",
    "_count": "status total: the row count behind count_edges and its siblings",
    "_impact_scope": "typed: the edge types of an impact rule, confined in SQL",
    "count_whole_module_uses": "typed: USES, counted for the impact package summary",
    "delete_use_edges": "maintenance: deletes the USES stage's edges for some files",
    "count_use_edges": "typed: USES, counted for the sync report's totals",
    "use_consumers_landing_in": "typed: USES, which files' uses land in a file being rewritten",
    "list_uses_targets": "typed: USES, the declarations a typed mapping or comparison uses",
    "package_crossing_ids": (
        "typed by its caller: ids of package-proven crossings a walk withheld; "
        "counted, never an answer"
    ),
}


#: The topology store's reads. Its aggregates take every non-structural type on
#: purpose, ``USES`` included, and report each under its own type: counted, never
#: summed into another type, never expanded. None of them answers a search.
REVIEWED_TOPOLOGY_EDGE_READS = {
    "crossing_edges": "per-type counts: edges between two repositories",
    "edges_under": "per-type counts: edges at the entities under one path",
    "repository_edges_under": "per-type counts: a repository entity's edges, by asserting file",
    "stored_edges": "exact: the stored edges an edge_ref's lookup matched, by id",
    "stored_edges_from": "typed: one edge type leaving the entities an edge_ref names",
    "references_in": "per-line: one file's located edges, each listed under its own type",
}

_EDGE_READ_MARKERS = ("FROM edges", "JOIN edges", "{table}")


def _functions_reading_edges(path: Path, markers: tuple[str, ...] = _EDGE_READ_MARKERS) -> set[str]:
    tree = ast.parse(path.read_text())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            text = ast.get_source_segment(path.read_text(), node) or ""
            if any(s in text for s in markers) and ("edges" in text):
                found.add(node.name)
    return found


def test_every_query_reading_edges_is_reviewed() -> None:
    stores = REPOATLAS / "storage/sqlite/stores.py"
    topology = REPOATLAS / "storage/sqlite/topology.py"
    assert _functions_reading_edges(stores) <= set(REVIEWED_EDGE_READS)
    # its shared SELECT reads edges too, so a function using it is a reader
    shared = (*_EDGE_READ_MARKERS, "_STORED_EDGE_SELECT")
    assert _functions_reading_edges(topology, shared) == set(REVIEWED_TOPOLOGY_EDGE_READS)
    others = [
        path
        for path in REPOATLAS.rglob("*.py")
        if path not in {stores, topology}
        and ("FROM edges" in path.read_text() or "JOIN edges" in path.read_text())
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
