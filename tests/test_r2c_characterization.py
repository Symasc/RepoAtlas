"""R2c characterization: Flask blueprint registration through application factories and helpers.

Each shape of ``tests/r2c_provider_corpus.py`` is indexed in a workspace of its own. Three kinds of
test read the same observations:

* the BASE characterization: the served-route edges and the plan's code routes equal the measured
  base in ``tests/data/r2c_base_golden.json`` (workspace-dependent row ids left out, the number of
  roots kept);
* pins that hold now and after R2c-1 (R2c-2 shapes stay unserved; the constant-path shapes);
* the R2c-1 predictions, now plain passing tests: the routes G1 serves for the helper shapes.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from repoatlas.graph.linker import plan_repository_http
from repoatlas.parsers.http_facts import HELPER_CALL_ROLE, HELPER_ROLE
from tests.f1_f2_corpus import open_built
from tests.r2c_provider_corpus import guard_repos, repos

GOLDEN_PATH = Path(__file__).parent / "data" / "r2c_base_golden.json"
GOLDEN: dict[str, dict] = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
SHAPES = repos()
#: The shapes of the base golden files plus the G1 shapes (P7, A24, A25), which have no base data.
ALL_SHAPES = {**SHAPES, **guard_repos()}

#: The plan-row fields that do not depend on the workspace's row ids.
PLAN_KEYS = ("body", "declared_at", "line_start", "line_end")

CLAIMS_PATH = Path(__file__).parent / "data" / "r2c_base_claims.json"
CLAIMS: dict[str, list[list[str]]] = json.loads(CLAIMS_PATH.read_text(encoding="utf-8"))

#: The base of the guard shapes (P7, A24..A30) at 3f2f1bf, as ``probe/guard_golden.py`` dumps it:
#: per shape ``{serves, plan, edges, claims}``, every value a list of rows of strings.
GUARD_GOLDEN_PATH = Path(__file__).parent / "data" / "r2c_guard_base_golden.json"
GUARD_GOLDEN: dict[str, dict[str, list[list[str]]]] = json.loads(
    GUARD_GOLDEN_PATH.read_text(encoding="utf-8")
)
GUARD_SHAPES = guard_repos()

#: The ``pending_fact`` values of the claims R2c-1 adds (the helper roles); the claims of the base
#: must be unchanged once these are left out.
HELPER_FACTS = frozenset({HELPER_ROLE, HELPER_CALL_ROLE})

#: Shapes whose BASE observation R2c-1 changes on purpose, each with its predicted addition (it was
#: EMPTY at G0 and was filled at G1 with the six former predictions and p4b). Format:
#: ``{shape name: ["METHOD /route", ...]}``, the routes ADDED to the shape's base served set (and
#: to its plan's code routes). The mechanism in
#: ``test_superseded_shapes_change_only_by_the_predicted_addition`` compares the sorted served
#: routes and the sorted plan bodies to the base golden's plus exactly these additions;
#: ``test_all_edges_are_unchanged_by_the_helper_roles`` allows exactly these routes' edges and
#: nothing else; the exact base equality of ``test_base_characterization`` is skipped for these.
R2C1_SUPERSEDED: dict[str, list[str]] = {
    "p3_helper": ["GET /one"],
    "p4_helper_reads": ["GET /one"],
    "p4b_helper_reads_bound": ["GET /one"],
    "p5_module_helper": ["GET /m/one"],
    "p6_outer_inner": ["GET /one"],
    "a5_cycle": ["GET /one"],
    "a17_wsgi_wrap": ["GET /one"],
}

#: The ``lines`` of the global pass's edge for each superseded route (as the prediction tests pin).
SUPERSEDED_EDGE_LINES: dict[str, list[int | None]] = {
    "GET /one": [6, 6],
    "GET /m/one": [None, None],
}

EDGES_PATH = Path(__file__).parent / "data" / "r2c_base_edges.json"
EDGES: dict[str, list[list[str]]] = json.loads(EDGES_PATH.read_text(encoding="utf-8"))

#: Every stored edge of a repository, all types, as ``probe/edges_golden.py`` dumps them (same
#: columns, same ORDER BY).
EDGES_SQL = """SELECT e.type, s.type, s.key, d.type, d.key, sf.rel_path, df.rel_path,
                      e.line_start, e.line_end, e.parser, e.resolution, e.confidence
               FROM edges e
               JOIN entities s ON s.id = e.src_id
               JOIN entities d ON d.id = e.dst_id
               LEFT JOIN files sf ON sf.id = e.file_id
               LEFT JOIN files df ON df.id = d.file_id
               WHERE e.repo_id = ?
               ORDER BY e.type, s.key, d.key, sf.rel_path, e.line_start, e.parser, e.resolution"""

#: The guard golden's edge columns (``probe/guard_golden.py``): the same rows and ORDER BY as
#: ``EDGES_SQL`` but WITHOUT the source and destination entity types (``s.type``, ``d.type``).
GUARD_EDGES_SQL = """SELECT e.type, s.key, d.key, sf.rel_path, df.rel_path, e.line_start,
                            e.line_end, e.parser, e.resolution, e.confidence
                     FROM edges e
                     JOIN entities s ON s.id = e.src_id
                     JOIN entities d ON d.id = e.dst_id
                     LEFT JOIN files sf ON sf.id = e.file_id
                     LEFT JOIN files df ON df.id = d.file_id
                     WHERE e.repo_id = ?
                     ORDER BY e.type, s.key, d.key, sf.rel_path, e.line_start, e.parser,
                              e.resolution"""

CLAIMS_SQL = """SELECT f.rel_path, u.line_start, u.line_end, u.edge_type, u.target_kind,
                       u.target_key, u.meta_json
                FROM unresolved_refs u LEFT JOIN files f ON f.id = u.file_id WHERE u.repo_id = ?
                ORDER BY f.rel_path, u.line_start, u.edge_type, u.target_key, u.meta_json"""

SERVES_SQL = """SELECT e.parser, f.rel_path, t.name, e.line_start, e.line_end
                FROM edges e JOIN entities t ON t.id = e.dst_id
                LEFT JOIN files f ON f.id = e.file_id
                WHERE e.type = 'SERVES_HTTP_ROUTE' AND e.repo_id = ? ORDER BY t.name"""


@dataclass(frozen=True)
class Observed:
    #: SERVES_HTTP_ROUTE edges: ``{parser, file, route, lines}``, ordered by route.
    serves: tuple[dict, ...]
    #: ``plan.code_routes`` rows: ``{body, declared_at, line_start, line_end, roots, root_paths}``.
    plan: tuple[dict, ...]


def observe_shape(base: Path, name: str) -> Observed:
    workspace = open_built(base, name, {name: ALL_SHAPES[name]})
    try:
        repo = next(r for r in workspace.repositories.list_all() if r.name == name)
        assert repo.id is not None
        edges = workspace.database.execute(SERVES_SQL, (repo.id,)).fetchall()
        serves = tuple(
            {"parser": p, "file": f, "route": t, "lines": [a, b]} for p, f, t, a, b in edges
        )
        rows = []
        for code in plan_repository_http(workspace.knowledge, repo.id).plan.code_routes:
            paths = sorted(
                workspace.database.execute(
                    "SELECT rel_path FROM files WHERE id = ?", (root,)
                ).fetchone()[0]
                for root in code.roots
            )
            rows.append(
                {
                    "body": code.body,
                    "declared_at": code.declared_at,
                    "line_start": code.line_start,
                    "line_end": code.line_end,
                    "roots": len(code.roots),
                    "root_paths": paths,
                }
            )
    finally:
        workspace.close()
    return Observed(serves=serves, plan=tuple(rows))


@pytest.fixture(scope="module")
def observe(tmp_path_factory: pytest.TempPathFactory) -> Callable[[str], Observed]:
    """Observations of a shape, each indexed once on first use."""
    base = tmp_path_factory.mktemp("r2c")
    cache: dict[str, Observed] = {}

    def get(name: str) -> Observed:
        if name not in cache:
            cache[name] = observe_shape(base, name)
        return cache[name]

    return get


def served(obs: Observed) -> list[str]:
    """The served routes, one entry per edge, sorted (a duplicate stays visible)."""
    return sorted(s["route"] for s in obs.serves)


def provenance(obs: Observed, route: str) -> tuple[int | None, int | None]:
    """``(line_start, declared_at)`` of the one plan row of ``route``."""
    rows = [r for r in obs.plan if r["body"] == route]
    assert len(rows) == 1, f"{route}: {len(rows)} plan rows"
    return rows[0]["line_start"], rows[0]["declared_at"]


def root_paths(obs: Observed, route: str) -> list[str]:
    """The root file paths of the one plan row of ``route``."""
    rows = [r for r in obs.plan if r["body"] == route]
    assert len(rows) == 1, f"{route}: {len(rows)} plan rows"
    return rows[0]["root_paths"]


def is_helper_claim(meta_json: str | None) -> bool:
    """True when the stored claim carries one of the helper ``pending_fact`` values."""
    if meta_json is None:
        return False
    try:
        meta = json.loads(meta_json)
    except ValueError:
        return False
    return isinstance(meta, dict) and meta.get("pending_fact") in HELPER_FACTS


def stored_claims(base: Path, name: str) -> list[list[str]]:
    """The raw stored claims of a shape (as ``probe/claims_golden.py`` dumps them), helper claims
    left out."""
    workspace = open_built(base, name, {name: SHAPES[name]})
    try:
        repo = next(r for r in workspace.repositories.list_all() if r.name == name)
        rows = workspace.database.execute(CLAIMS_SQL, (repo.id,)).fetchall()
    finally:
        workspace.close()
    return [[str(x) for x in row] for row in rows if not is_helper_claim(row[6])]


def helper_claims(base: Path, name: str) -> list[tuple[str, str, dict]]:
    """``(file, pending_fact, meta)`` of each helper claim of a shape, read from the live index
    (the shape need not be in the base golden)."""
    workspace = open_built(base, name, {name: ALL_SHAPES[name]})
    try:
        repo = next(r for r in workspace.repositories.list_all() if r.name == name)
        rows = workspace.database.execute(CLAIMS_SQL, (repo.id,)).fetchall()
    finally:
        workspace.close()
    found = []
    for row in rows:
        if is_helper_claim(row[6]):
            meta = json.loads(row[6])
            found.append((row[0], meta["pending_fact"], meta))
    return found


def stored_edges(base: Path, name: str) -> list[list[str]]:
    """Every raw stored edge of a shape (as ``probe/edges_golden.py`` dumps them)."""
    workspace = open_built(base, name, {name: SHAPES[name]})
    try:
        repo = next(r for r in workspace.repositories.list_all() if r.name == name)
        rows = workspace.database.execute(EDGES_SQL, (repo.id,)).fetchall()
    finally:
        workspace.close()
    return [[str(x) for x in row] for row in rows]


def guard_claims(base: Path, name: str) -> list[list[str]]:
    """The raw stored claims of a guard shape, helper claims left out."""
    workspace = open_built(base, name, {name: GUARD_SHAPES[name]})
    try:
        repo = next(r for r in workspace.repositories.list_all() if r.name == name)
        rows = workspace.database.execute(CLAIMS_SQL, (repo.id,)).fetchall()
    finally:
        workspace.close()
    return [[str(x) for x in row] for row in rows if not is_helper_claim(row[6])]


def guard_edges(base: Path, name: str) -> list[list[str]]:
    """Every raw stored edge of a guard shape, in the guard golden's column order."""
    workspace = open_built(base, name, {name: GUARD_SHAPES[name]})
    try:
        repo = next(r for r in workspace.repositories.list_all() if r.name == name)
        rows = workspace.database.execute(GUARD_EDGES_SQL, (repo.id,)).fetchall()
    finally:
        workspace.close()
    return [[str(x) for x in row] for row in rows]


def global_edge(route: str, lines: list[int | None]) -> dict:
    """The SERVES_HTTP_ROUTE edge the global pass adds for a helper-registered route."""
    return {"parser": "linker:global", "file": "pkg/views.py", "route": route, "lines": lines}


SERVES_TYPE = "SERVES_HTTP_ROUTE"


def _serves_dict(row: list[str]) -> dict:
    """A raw ``EDGES_SQL`` SERVES_HTTP_ROUTE row as the ``{parser, file, route, lines}`` form."""
    _, method, path = row[4].split(":", 2)  # "http-route:GET:/one"
    lines = [None if x == "None" else int(x) for x in (row[7], row[8])]
    return {"parser": row[9], "file": row[5], "route": f"{method} {path}", "lines": lines}


def _route_file(edge: dict) -> tuple[str, str]:
    return edge["route"], edge["file"]


def _key(row: dict) -> str:
    return json.dumps(row, sort_keys=True)


def golden_edges(name: str) -> list[dict]:
    return sorted(GOLDEN[name]["serves"], key=_key)


def golden_plan(name: str) -> list[dict]:
    rows = [
        {**{k: row[k] for k in PLAN_KEYS}, "roots": len(row["roots"])}
        for row in GOLDEN[name]["plan_code_routes"]
    ]
    return sorted(rows, key=_key)


# -- the base ---------------------------------------------------------------------------------


def test_golden_covers_every_shape() -> None:
    assert sorted(GOLDEN) == sorted(SHAPES)
    assert len(SHAPES) == 33


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_base_characterization(observe: Callable[[str], Observed], name: str) -> None:
    if name in R2C1_SUPERSEDED:
        pytest.skip("superseded by R2c-1: checked by the predicted-addition test")
    obs = observe(name)
    assert sorted(obs.serves, key=_key) == golden_edges(name)
    rows = [{k: row[k] for k in (*PLAN_KEYS, "roots")} for row in obs.plan]
    assert sorted(rows, key=_key) == golden_plan(name)


@pytest.mark.skipif(not R2C1_SUPERSEDED, reason="no R2c-1 superseded entries")
@pytest.mark.parametrize("name", sorted(R2C1_SUPERSEDED))
def test_superseded_shapes_change_only_by_the_predicted_addition(
    observe: Callable[[str], Observed], name: str
) -> None:
    added = R2C1_SUPERSEDED[name]
    obs = observe(name)
    base_routes = [s["route"] for s in GOLDEN[name]["serves"]]
    assert served(obs) == sorted(base_routes + added)
    base_bodies = [row["body"] for row in GOLDEN[name]["plan_code_routes"]]
    assert sorted(r["body"] for r in obs.plan) == sorted(base_bodies + added)


def test_claims_golden_covers_every_shape() -> None:
    assert sorted(CLAIMS) == sorted(SHAPES)
    assert len(CLAIMS) == 33


@pytest.mark.parametrize("name", sorted(CLAIMS))
def test_existing_claims_are_unchanged_by_the_helper_roles(tmp_path: Path, name: str) -> None:
    assert stored_claims(tmp_path, name) == CLAIMS[name]


def test_edges_golden_covers_every_shape() -> None:
    assert sorted(EDGES) == sorted(SHAPES)
    assert len(EDGES) == 33


# Every edge of every type is compared and none is excluded: helper facts are claims, not edges,
# so an edge of a type or target R2c-1 adds fails here. After G1 the only allowed additions are the
# R2C1_SUPERSEDED SERVES_HTTP_ROUTE edges (parser ``linker:global``), compared order-insensitively.
@pytest.mark.parametrize("name", sorted(EDGES))
def test_all_edges_are_unchanged_by_the_helper_roles(tmp_path: Path, name: str) -> None:
    stored = stored_edges(tmp_path, name)
    if name not in R2C1_SUPERSEDED:
        assert stored == EDGES[name]
        return
    # A superseded shape: every edge that is not a SERVES_HTTP_ROUTE edge is exact; the
    # SERVES_HTTP_ROUTE edges are the golden's plus exactly one global-pass edge per listed route.
    assert [r for r in stored if r[0] != SERVES_TYPE] == [
        r for r in EDGES[name] if r[0] != SERVES_TYPE
    ]
    expected = [_serves_dict(r) for r in EDGES[name] if r[0] == SERVES_TYPE]
    for route in R2C1_SUPERSEDED[name]:
        expected.append(global_edge(route, SUPERSEDED_EDGE_LINES[route]))
    found = [_serves_dict(r) for r in stored if r[0] == SERVES_TYPE]
    assert sorted(found, key=_route_file) == sorted(expected, key=_route_file)


# -- the guard shapes: base rows unchanged ----------------------------------------------------
# The guard shapes serve nothing at base and must serve nothing after G1, so their edges are
# compared in full (no exclusions, no superseded entries) and their claims minus the helper ones.


def test_guard_golden_covers_every_guard_shape() -> None:
    assert sorted(GUARD_GOLDEN) == sorted(guard_repos())


@pytest.mark.parametrize("name", sorted(GUARD_GOLDEN))
def test_guard_claims_are_unchanged_by_the_helper_roles(tmp_path: Path, name: str) -> None:
    assert guard_claims(tmp_path, name) == GUARD_GOLDEN[name]["claims"]


@pytest.mark.parametrize("name", sorted(GUARD_GOLDEN))
def test_guard_edges_are_unchanged_by_the_helper_roles(tmp_path: Path, name: str) -> None:
    assert guard_edges(tmp_path, name) == GUARD_GOLDEN[name]["edges"]


# -- pins that hold now and after R2c-1 ------------------------------------------------------


def test_a21_constant_path_is_served_by_the_global_pass(observe: Callable[[str], Observed]) -> None:
    obs = observe("a21_constant_path")
    assert obs.serves == (
        {"parser": "linker:global", "file": "pkg/vc.py", "route": "GET /p", "lines": [None, None]},
    )
    assert provenance(obs, "GET /p") == (None, 7)
    (row,) = obs.plan
    assert row["roots"] == 1
    assert row["root_paths"] == ["pkg/app.py"]


def test_a21b_two_level_constant_path_is_refused_entirely(
    observe: Callable[[str], Observed],
) -> None:
    obs = observe("a21b_two_level_constant_path")
    assert obs.serves == ()
    assert [r for r in obs.plan if "/p2" in r["body"]] == []
    assert obs.plan == ()


@pytest.mark.parametrize(
    "name",
    [
        "p2_two_level",
        "p2b_two_level_bound",
        "a18_init_rebound",
        "a19_init_from_views_only",
        "a20_three_levels",
    ],
)
def test_r2c2_shape_stays_unserved(observe: Callable[[str], Observed], name: str) -> None:
    obs = observe(name)
    assert served(obs) == []
    assert obs.plan == ()


@pytest.mark.parametrize("name", ["p4_helper_reads", "p4b_helper_reads_bound"])
def test_r2c2_two_level_route_stays_unserved(observe: Callable[[str], Observed], name: str) -> None:
    obs = observe(name)
    assert "GET /u/two" not in served(obs)
    assert all(r["body"] != "GET /u/two" for r in obs.plan)


@pytest.mark.parametrize(
    ("name", "routes"),
    [
        ("p1_one_level", ["GET /c1/one"]),
        ("a6_two_factories", ["GET /a/one", "GET /b/one"]),
        ("a8_url_prefix", ["GET /over/two"]),
    ],
)
def test_one_level_registration_is_unchanged(
    observe: Callable[[str], Observed], name: str, routes: list[str]
) -> None:
    obs = observe(name)
    assert served(obs) == routes
    assert sorted(r["body"] for r in obs.plan) == routes


# -- nothing is served: true on the base, and R2c-1 must keep it so --------------------------
# (The brief lists these among the strict-xfail predictions, but "nothing served" already holds on
# the base, so a strict xfail would XPASS and fail. They are plain asserts: the guard that G1 does
# not start to serve a route it must refuse.)


@pytest.mark.parametrize(
    "name",
    [
        "p5x_param_named_app",
        "a1_conditional",
        "a2_dynamic",
        "a3_param_other",
        "a4_shadow",
        "a7_unresolved",
        "a9_alias",
        "a10_chain5",
        "a11_rebound_param",
        "a12_conditional_call",
        "a13_star_args",
        "a14_before_construction",
        "a15_imported_helper",
        "a16_decorated_helper",
        "a22_positional_mismatch",
        "a23_call_before_child_bound",
    ],
)
def test_refused_shape_serves_nothing(observe: Callable[[str], Observed], name: str) -> None:
    obs = observe(name)
    assert served(obs) == []
    assert obs.plan == ()


# -- R2c-1 G1: the former predictions, now plain tests ----------------------------------------


def test_p3_helper_serves_one(observe: Callable[[str], Observed]) -> None:
    obs = observe("p3_helper")
    assert served(obs) == ["GET /one"]
    assert obs.serves == (global_edge("GET /one", [6, 6]),)
    assert provenance(obs, "GET /one") == (6, 6)
    assert root_paths(obs, "GET /one") == ["pkg/app.py"]


def test_p4_helper_reads_serves_one_and_not_the_two_level_route(
    observe: Callable[[str], Observed],
) -> None:
    obs = observe("p4_helper_reads")
    assert served(obs) == ["GET /one"]
    assert obs.serves == (global_edge("GET /one", [6, 6]),)
    assert provenance(obs, "GET /one") == (6, 6)
    assert root_paths(obs, "GET /one") == ["pkg/app.py"]


def test_p5_module_helper_serves_one_under_its_prefix(
    observe: Callable[[str], Observed],
) -> None:
    obs = observe("p5_module_helper")
    assert served(obs) == ["GET /m/one"]
    assert obs.serves == (global_edge("GET /m/one", [None, None]),)
    assert provenance(obs, "GET /m/one") == (None, 6)
    assert root_paths(obs, "GET /m/one") == ["pkg/app.py"]


def test_p6_outer_inner_serves_one(observe: Callable[[str], Observed]) -> None:
    obs = observe("p6_outer_inner")
    assert served(obs) == ["GET /one"]
    assert obs.serves == (global_edge("GET /one", [6, 6]),)
    assert provenance(obs, "GET /one") == (6, 6)
    assert root_paths(obs, "GET /one") == ["pkg/app.py"]


def test_a5_cycle_serves_one_exactly_once(observe: Callable[[str], Observed]) -> None:
    obs = observe("a5_cycle")
    assert served(obs) == ["GET /one"]
    assert obs.serves == (global_edge("GET /one", [6, 6]),)
    assert [r["body"] for r in obs.plan] == ["GET /one"]
    assert provenance(obs, "GET /one") == (6, 6)
    assert root_paths(obs, "GET /one") == ["pkg/app.py"]


def test_a17_wsgi_wrap_serves_one(observe: Callable[[str], Observed]) -> None:
    obs = observe("a17_wsgi_wrap")
    assert served(obs) == ["GET /one"]
    assert obs.serves == (global_edge("GET /one", [6, 6]),)
    assert provenance(obs, "GET /one") == (6, 6)
    assert root_paths(obs, "GET /one") == ["pkg/app.py"]


# -- R2c-1 G1: plain tests (not xfail) --------------------------------------------------------


def test_p4b_helper_reads_bound_serves_one(observe: Callable[[str], Observed]) -> None:
    obs = observe("p4b_helper_reads_bound")
    assert served(obs) == ["GET /one"]
    assert obs.serves == (global_edge("GET /one", [6, 6]),)
    assert provenance(obs, "GET /one") == (6, 6)
    assert root_paths(obs, "GET /one") == ["pkg/app.py"]


def test_a_helper_claim_is_recorded_only_when_it_has_something_to_record(
    observe: Callable[[str], Observed], tmp_path: Path
) -> None:
    # P3: one helper with a registration and one call of it, both in the application's file.
    p3 = helper_claims(tmp_path, "p3_helper")
    assert [(path, fact) for path, fact, _ in p3 if fact == HELPER_ROLE] == [
        ("pkg/app.py", HELPER_ROLE)
    ]
    assert [(path, fact) for path, fact, _ in p3 if fact == HELPER_CALL_ROLE] == [
        ("pkg/app.py", HELPER_CALL_ROLE)
    ]
    assert len(p3) == 2

    # P4: the helper records the one-level child only; the two-level one is not a mount.
    p4 = helper_claims(tmp_path, "p4_helper_reads")
    (helper,) = [meta for _, fact, meta in p4 if fact == HELPER_ROLE]
    mounts = json.loads(helper["mounts"])
    assert len(mounts) == 1
    assert (mounts[0]["module"], mounts[0]["name"]) == ("pkg.views", "bp")
    assert "users.views" not in json.dumps(helper)

    # P7: nothing recordable (a read and a two-level registration): no helper claim, no call claim.
    assert helper_claims(tmp_path, "p7_helper_no_registration") == []
    assert served(observe("p7_helper_no_registration")) == []


# -- R2c-1 G1 regression guards: control flow around the child import or the call -------------


@pytest.mark.parametrize("name", ["a24_conditional_child_import", "a25_conditional_helper_call"])
def test_helper_regression_guards_a24_a25(observe: Callable[[str], Observed], name: str) -> None:
    assert served(observe(name)) == []


@pytest.mark.parametrize(
    "name",
    [
        "a26_nested_registration",
        "a27_param_alias",
        "a28a_starred_call",
        "a28b_starred_tail",
        "a28c_double_star",
        "a29_param_child",
        "a30_helper_to_helper_position",
    ],
)
def test_guard_fixtures_serve_nothing(observe: Callable[[str], Observed], name: str) -> None:
    obs = observe(name)
    assert served(obs) == []
    assert obs.plan == ()
