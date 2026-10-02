"""Bounded neighbour retrieval: exact, confined before it is cut, and charged.

The change-impact walk reads the graph only through the bounded store queries and
:class:`~repoatlas.graph.neighbourhood.Neighbourhood`. These tests hold three things:

* **exact** -- counts, pages, distributions and representatives equal what a brute
  force over every edge computes with :meth:`Confinement.admits`, for every node,
  rule and confinement the fixtures hold (the shared predicate, stated twice, agrees);
* **confined first** -- a limit falls on edges the walk may follow, never on edges it
  would discard, so a page is full whenever the graph has enough;
* **charged** -- a 5,000-dependent hub costs one query to count and exactly the rows a
  page asks for, and once a budget is spent nothing more is read.

Nothing here asserts time; a hang guard stops a runaway test instead.
"""

from __future__ import annotations

import faulthandler
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from repoatlas.config.settings import Settings
from repoatlas.graph.neighbourhood import Neighbourhood, Work, WorkLimits
from repoatlas.graph.traversal import CROSS_REPOSITORY_EDGES
from repoatlas.models.parsing import EdgeDirection, EdgeType, EntityType
from repoatlas.services.workspace import Workspace, open_workspace
from repoatlas.storage.protocols import Confinement, NeighborEdge, NeighborRow, NeighborRule
from tests.impact_corpus import (
    ALPHA,
    HUB_FILES,
    HUB_USERS_PER_FILE,
    SYMBOLS,
    ZETA,
    build,
    hub_repo,
)
from tests.xrepo_corpus import LINKING

HUB_USERS = HUB_FILES * HUB_USERS_PER_FILE


@pytest.fixture(autouse=True)
def _hang_guard() -> Iterator[None]:
    faulthandler.dump_traceback_later(300, exit=True)
    yield
    faulthandler.cancel_dump_traceback_later()


def _indexed(root: Path, repos: dict[str, dict[str, str]]) -> Workspace:
    settings = Settings.model_validate(
        {
            "storage": {"database": str(root / "state" / "atlas.db")},
        }
    )
    workspace = open_workspace(settings=settings, require_root=False)
    return build(workspace, root, repos)


@pytest.fixture(scope="module")
def mixed(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Workspace]:
    workspace = _indexed(
        tmp_path_factory.mktemp("mixed"), {"symbols": SYMBOLS, "alpha": ALPHA, "zeta": ZETA}
    )
    yield workspace
    workspace.close()


@pytest.fixture(scope="module")
def xrepo(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Workspace]:
    """Repositories joined by package links: the edges that cross only on evidence."""
    workspace = _indexed(tmp_path_factory.mktemp("xrepo"), LINKING)
    yield workspace
    workspace.close()


@pytest.fixture(scope="module")
def hub(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Workspace]:
    workspace = _indexed(tmp_path_factory.mktemp("hub"), {"hub": hub_repo()})
    yield workspace
    workspace.close()


def entity(workspace: Workspace, key: str) -> int:
    [row] = workspace.database.execute("SELECT id FROM entities WHERE key = ?", (key,)).fetchall()
    return row[0]


def repo(workspace: Workspace, slug: str) -> int:
    [row] = workspace.database.execute(
        "SELECT id FROM repositories WHERE slug = ?", (slug,)
    ).fetchall()
    return row[0]


# -- the brute force the bounded queries must equal ------------------------------------


def all_edges(workspace: Workspace) -> list[dict[str, Any]]:
    rows = workspace.database.execute(
        """
        SELECT e.id, e.type, e.src_id, e.dst_id, e.file_id, e.line_start, e.line_end,
               src.repo_id AS src_repo, dst.repo_id AS dst_repo, src.key AS src_key,
               dst.key AS dst_key, src.type AS src_type, dst.type AS dst_type,
               src.file_id AS src_file, dst.file_id AS dst_file,
               af.rel_path AS file_path, sf.rel_path AS src_path, df.rel_path AS dst_path,
               e.resolution AS resolution
        FROM edges e JOIN entities src ON src.id = e.src_id JOIN entities dst ON dst.id = e.dst_id
        LEFT JOIN files af ON af.id = e.file_id LEFT JOIN files sf ON sf.id = src.file_id
        LEFT JOIN files df ON df.id = dst.file_id
        """
    ).fetchall()
    return [dict(row) for row in rows]


def evidenced(resolution: str | None) -> bool:
    """Whether an edge records a package link's evidence for crossing."""
    record = json.loads(resolution) if resolution else {}
    crossing = record.get("crossing") if isinstance(record, dict) else None
    return isinstance(crossing, dict) and crossing.get("package") is not None


def uses_from_files(edges: list[dict[str, Any]]) -> set[tuple[int, int]]:
    """(used entity, file of the user) for every USES edge."""
    return {(e["dst_id"], e["src_file"]) for e in edges if e["type"] == "USES"}


def brute(
    edges: list[dict[str, Any]],
    near: int,
    rule: NeighborRule,
    confinement: Confinement,
    uses_from: set[tuple[int, int]],
) -> list[dict[str, Any]]:
    """Every relationship the rule admits from ``near``, in natural order, far end marked."""
    out = rule.outgoing
    kinds = {str(t) for t in rule.edge_types}
    constrained = {str(k): {str(t) for t in types} for k, types in rule.far_types}
    side = "dst" if out else "src"
    found = []
    for e in edges:
        if e["type"] not in kinds or e["src_id"] == e["dst_id"]:
            continue
        if (e["src_id"] if out else e["dst_id"]) != near:
            continue
        if e["type"] in constrained and e[f"{side}_type"] not in constrained[e["type"]]:
            continue
        if (
            rule.uses_replace_file_facts
            and e["type"] in {"REFERENCES", "CALLS"}
            and e[f"{side}_type"] == "file"
            and (near, e[f"{side}_file"]) in uses_from
        ):
            continue
        neighbour = NeighborEdge(
            src_id=e["src_id"],
            dst_id=e["dst_id"],
            type=EdgeType(e["type"]),
            src_repo_id=e["src_repo"],
            dst_repo_id=e["dst_repo"],
            evidenced=evidenced(e["resolution"]),
        )
        if not confinement.admits(neighbour, outgoing=out):
            continue
        found.append(
            {
                **e,
                "far": e[f"{side}_id"],
                "far_key": e[f"{side}_key"],
                "far_repo": e[f"{side}_repo"],
                "far_path": e[f"{side}_path"],
            }
        )
    return sorted(
        found,
        key=lambda f: (
            f["type"],
            f["far_key"],
            f["file_path"] or "",
            f["line_start"] or 0,
            f["line_end"] or 0,
            f["id"],
        ),
    )


def confinements(workspace: Workspace, homes: tuple[str, ...] = ()) -> list[Confinement]:
    """Every confinement shape, homed in each repository (or only in ``homes``)."""
    repos = [
        r[0]
        for r in workspace.database.execute("SELECT id, slug FROM repositories").fetchall()
        if not homes or r[1] in homes
    ]
    uses = frozenset({EdgeType.USES})
    found = [
        Confinement(),
        Confinement(crossing=CROSS_REPOSITORY_EDGES),
        Confinement(evidenced=uses),
    ]
    for repo_id in repos:
        found.append(Confinement(home_repo_id=repo_id))
        found.append(Confinement(crossing=CROSS_REPOSITORY_EDGES, home_repo_id=repo_id))
        found.append(
            Confinement(
                crossing=CROSS_REPOSITORY_EDGES, home_repo_id=repo_id, crossing_moves_home=True
            )
        )
        # the change-impact walk's own confinement, in each scope it can be given
        walk = Confinement(
            crossing=CROSS_REPOSITORY_EDGES,
            evidenced=uses,
            home_repo_id=repo_id,
            crossing_moves_home=True,
        )
        found.append(walk)
        found.append(walk.model_copy(update={"scope_repo_ids": frozenset({repo_id})}))
    return found


def rules(kinds: list[EdgeType]) -> list[NeighborRule]:
    """Every single type both ways, and the multi-type, constrained and replacing rules."""
    found = [
        NeighborRule(edge_types=(kind,), direction=direction)
        for kind in kinds
        for direction in (EdgeDirection.OUT, EdgeDirection.IN)
    ]
    for direction in (EdgeDirection.OUT, EdgeDirection.IN):
        found.append(
            NeighborRule(
                edge_types=tuple(kinds),
                direction=direction,
                far_types=((EdgeType.EXPORTS, (EntityType.ENV_VAR,)),),
                uses_replace_file_facts=True,
            )
        )
    return found


#: The homes each corpus is checked from: all of them, or the XR provider and a consumer.
HOMES = {"mixed": (), "xrepo": ("contracts", "app")}


@pytest.mark.parametrize("corpus", ["mixed", "xrepo"])
def test_every_bounded_query_equals_the_brute_force(
    corpus: str, request: pytest.FixtureRequest
) -> None:
    mixed: Workspace = request.getfixturevalue(corpus)
    knowledge = mixed.knowledge
    edges = all_edges(mixed)
    uses_from = uses_from_files(edges)
    kinds = sorted({EdgeType(e["type"]) for e in edges}, key=str)
    nodes = sorted({e["src_id"] for e in edges} | {e["dst_id"] for e in edges})
    checked = 0
    for confinement in confinements(mixed, HOMES[corpus]):
        for rule in rules(kinds):
            counts = knowledge.impact_counts(nodes, rule, confinement)
            for near in nodes:
                expected = brute(edges, near, rule, confinement, uses_from)
                if not expected:
                    assert near not in counts
                    continue
                checked += 1
                count = counts[near]
                assert count.far == len({f["far"] for f in expected})
                assert count.relationships == len(
                    {
                        (f["far"], f["type"], f["file_id"], f["line_start"], f["line_end"])
                        for f in expected
                    }
                )
                # pages of two, followed by cursor, are the brute force in order
                paged: list[NeighborRow] = []
                after = None
                while True:
                    page = knowledge.impact_page(near, rule, confinement, after=after, limit=2)
                    paged.extend(page)
                    if len(page) < 2:
                        break
                    after = page[-1].cursor
                assert [r.edge_id for r in paged] == [f["id"] for f in expected]
                # every far file is one group, and together they hold every far entity
                groups, partial = knowledge.impact_distribution(
                    near, rule, confinement, max_groups=1000
                )
                by_file: dict[tuple[int | None, str | None], set[int]] = {}
                for f in expected:
                    by_file.setdefault((f["far_repo"], f["far_path"]), set()).add(f["far"])
                assert not partial
                assert {(g.repo_id, g.path): g.far for g in groups} == {
                    k: len(v) for k, v in by_file.items()
                }
                # one representative per file: the first of that file in natural order
                firsts = knowledge.impact_rows_for(
                    near, rule, confinement, groups=list(by_file), per_group=1
                )
                first_of: dict[tuple[int | None, str | None], int] = {}
                for f in expected:
                    first_of.setdefault((f["far_repo"], f["far_path"]), f["id"])
                assert sorted(r.edge_id for r in firsts) == sorted(first_of.values())
    assert checked > 100


def test_confinement_applies_before_the_limit(mixed: Workspace) -> None:
    knowledge = mixed.knowledge
    shared = entity(mixed, "env:SHARED_URL")
    reads = NeighborRule(edge_types=(EdgeType.READS,), direction=EdgeDirection.IN)
    everywhere = knowledge.impact_page(shared, reads, Confinement(), after=None, limit=10)
    assert [r.far_key for r in everywhere] == [
        "file:alpha:src/config.ts",
        "file:zeta:src/config.ts",
    ]
    # alpha's reader sorts first; standing in zeta, the one row a page of one holds is zeta's
    home = Confinement(home_repo_id=repo(mixed, "zeta"))
    [only] = knowledge.impact_page(shared, reads, home, after=None, limit=1)
    assert only.far_key == "file:zeta:src/config.ts"
    assert knowledge.impact_counts([shared], reads, home)[shared].far == 1


def test_a_repository_edge_crosses_only_when_its_type_may(mixed: Workspace) -> None:
    knowledge = mixed.knowledge
    zeta = entity(mixed, "repo:zeta")
    depends = NeighborRule(edge_types=(EdgeType.DEPENDS_ON,), direction=EdgeDirection.OUT)

    def far(confinement: Confinement) -> set[str]:
        return {
            r.far_key
            for r in knowledge.impact_page(zeta, depends, confinement, after=None, limit=10)
        }

    assert far(Confinement()) == {"pkg:npm:alpha"}
    assert far(Confinement(crossing=CROSS_REPOSITORY_EDGES)) == {"pkg:npm:alpha", "repo:alpha"}
    home = Confinement(crossing=CROSS_REPOSITORY_EDGES, home_repo_id=repo(mixed, "zeta"))
    assert far(home) == {"pkg:npm:alpha"}
    # the change-impact walk's mode: a direct crossing edge of an admitted type moves home
    moving = home.model_copy(update={"crossing_moves_home": True})
    assert far(moving) == {"pkg:npm:alpha", "repo:alpha"}


# -- fixture B: the 5,000-dependent hub ----------------------------------------------

USES_IN = NeighborRule(edge_types=(EdgeType.USES,), direction=EdgeDirection.IN)
OPEN = Confinement()


def test_a_hub_is_counted_exactly_by_one_query(hub: Workspace) -> None:
    node = entity(hub, "sym:hub:core/hub.ts:HUB:1")
    work = Work()
    counts = Neighbourhood(hub.knowledge, work).counts([node], USES_IN, OPEN)
    assert counts is not None
    assert (counts[node].far, counts[node].relationships) == (HUB_USERS, HUB_USERS)
    assert (work.queries, work.rows) == (1, 1)


def test_a_page_of_a_hub_fetches_exactly_what_it_asks_for(hub: Workspace) -> None:
    node = entity(hub, "sym:hub:core/hub.ts:HUB:1")
    work = Work()
    neighbourhood = Neighbourhood(hub.knowledge, work)
    page = neighbourhood.page(node, USES_IN, OPEN, limit=50)
    assert len(page) == 50 and (work.queries, work.rows) == (1, 50)
    # the whole hub, page by page: every dependent once, in natural order
    seen = list(page)
    while True:
        page = neighbourhood.page(node, USES_IN, OPEN, after=seen[-1].cursor, limit=500)
        seen.extend(page)
        if len(page) < 500:
            break
    assert len({r.far_id for r in seen}) == HUB_USERS == len(seen)
    keys = [(str(r.type), r.far_key, r.file_path or "", r.line_start or 0, r.edge_id) for r in seen]
    assert keys == sorted(keys)
    assert work.rows == HUB_USERS and work.queries == 1 + 10


def test_a_hub_s_spread_is_bounded_and_says_so(hub: Workspace) -> None:
    node = entity(hub, "sym:hub:core/hub.ts:HUB:1")
    work = Work()
    neighbourhood = Neighbourhood(hub.knowledge, work)
    groups, partial = neighbourhood.distribution(node, USES_IN, OPEN, max_groups=10)
    assert partial and len(groups) == 10
    assert {g.far for g in groups} == {HUB_USERS_PER_FILE}
    chosen = [(g.repo_id, g.path) for g in groups[:6]]
    representatives = neighbourhood.rows_for(node, USES_IN, OPEN, groups=chosen, per_group=1)
    assert len(representatives) == 6
    assert {r.far_path for r in representatives} == {g.path for g in groups[:6]}
    assert (work.queries, work.rows) == (2, 16)


def test_a_spent_row_budget_reads_nothing_more(hub: Workspace) -> None:
    node = entity(hub, "sym:hub:core/hub.ts:HUB:1")
    work = Work(limits=WorkLimits(rows=100))
    neighbourhood = Neighbourhood(hub.knowledge, work)
    page = neighbourhood.page(node, USES_IN, OPEN, limit=500)
    assert len(page) == 100 and work.exhausted == "rows"
    assert neighbourhood.page(node, USES_IN, OPEN, after=page[-1].cursor, limit=500) == []
    assert neighbourhood.rows_for(node, USES_IN, OPEN, groups=[(None, "x")], per_group=1) == []
    assert (work.queries, work.rows) == (1, 100)


def test_a_spent_query_budget_reads_nothing_more(hub: Workspace) -> None:
    node = entity(hub, "sym:hub:core/hub.ts:HUB:1")
    work = Work(limits=WorkLimits(queries=2))
    neighbourhood = Neighbourhood(hub.knowledge, work)
    assert neighbourhood.counts([node], USES_IN, OPEN) is not None
    assert len(neighbourhood.page(node, USES_IN, OPEN, limit=5)) == 5
    assert neighbourhood.counts([node], USES_IN, OPEN) is None
    assert neighbourhood.page(node, USES_IN, OPEN, limit=5) == []
    assert neighbourhood.distribution(node, USES_IN, OPEN, max_groups=3) == ([], True)
    assert (work.queries, work.exhausted) == (2, "queries")


def test_expansions_are_charged_against_their_own_limit() -> None:
    work = Work(limits=WorkLimits(expansions=2))
    assert [work.expand() for _ in range(3)] == [True, True, False]
    assert (work.expanded, work.exhausted) == (2, "expansions")


#: A call in a module-scope initializer: the call graph gives it to the file, and the
#: binding it initializes uses the callee too.
INITIALIZER = {
    "package.json": json.dumps({"name": "initializer", "private": True}),
    "src/s.ts": "export function make() {\n  return 1;\n}\n",
    "src/u.ts": 'import { make } from "./s";\nexport const made = make();\n',
}


def test_a_declaration_use_replaces_its_file_s_module_scope_call(tmp_path: Path) -> None:
    workspace = _indexed(tmp_path, {"initializer": INITIALIZER})
    try:
        callee = entity(workspace, "sym:initializer:src/s.ts:make:1")

        def far(replace: bool) -> set[tuple[str, str]]:
            rule = NeighborRule(
                edge_types=(EdgeType.CALLS, EdgeType.REFERENCES, EdgeType.USES),
                direction=EdgeDirection.IN,
                uses_replace_file_facts=replace,
            )
            return {
                (str(r.type), r.far_key)
                for r in workspace.knowledge.impact_page(
                    callee, rule, Confinement(), after=None, limit=100
                )
            }

        kept, replaced = far(False), far(True)
        assert kept == {
            ("CALLS", "file:initializer:src/u.ts"),
            ("USES", "sym:initializer:src/u.ts:made:2"),
        }
        assert replaced == {("USES", "sym:initializer:src/u.ts:made:2")}
    finally:
        workspace.close()
