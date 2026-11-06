"""R2c-2: a two-level module-attribute child (``users.views.bp`` after ``from pkg import users``).

Each fixture of ``tests/r2c2_corpus.py`` is indexed in a workspace of its own. The child is a mount
claim that also states the head's import (``module``, ``head_import``) and the attribute between
(``through``); the global pass serves it only when the export tables of the packages prove both
hops (``repoatlas.graph.py_routes``). The predictions below are the frozen R2c-2 predictions
(R2C2_PHASE_A_2026-11-05.md and its amendments of 2026-11-06), written as literals and never
computed from the product:

* ``EXPECTED``: per fixture the sorted served routes, the number of ``python-mount``,
  ``python-helper`` and ``python-helper-call`` claims, and the sorted codes of the repository's
  diagnostics. Every Flask fixture keeps ``python-not-mounted`` (``pkg/views_b.py`` declares a route
  on a blueprint nothing registers);
* ``SERVED``: the fixtures that serve ``GET /u/two`` (``GET /x/two`` for Q7), the edge, its
  provenance and its roots.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from repoatlas.capabilities.http_repository import repository_inventory
from repoatlas.graph.linker import plan_repository_http
from tests.f1_f2_corpus import open_built
from tests.r2c2_corpus import FIXTURES, fixture_repos

KINDS = ("python-helper-call", "python-helper", "python-mount")
NOT_MOUNTED = "python-not-mounted"
BEFORE_BINDING = "python-helper-before-binding"
CONTROL_FLOW = "python-helper-control-flow"

TWO = "GET /u/two"

#: fixture -> (sorted served routes, python-mount/helper/helper-call claims, sorted diagnostic
#: codes)
EXPECTED: dict[str, tuple[list[str], int, list[str]]] = {
    # Positive: both hops proven by the export tables.
    "Q1": ([TWO], 2, [NOT_MOUNTED]),
    "Q2": ([TWO], 1, [NOT_MOUNTED]),
    "Q3": ([TWO], 1, [NOT_MOUNTED]),
    "Q4": ([TWO], 1, [NOT_MOUNTED]),
    "Q5": ([TWO], 1, [NOT_MOUNTED]),
    "Q6": ([TWO], 1, [NOT_MOUNTED]),
    "Q7": (["GET /x/two"], 1, [NOT_MOUNTED]),
    "Q8": (["GET /items/list"], 1, []),
    "Q8c": (["GET /items/list"], 1, []),
    "Q9": ([TWO], 1, [NOT_MOUNTED]),
    # Q10 and X12 are FAILED predictions (amendment C, 2026-11-06): the helper parameter is named
    # `app`, the module-level application name, so the module-wide binding count of `app` is 2
    # (P5x), the application is not a receiver and no helper-call claim is made. Q10b/X12b rename
    # the parameter.
    "Q10": ([], 1, [NOT_MOUNTED]),
    "Q10b": ([TWO], 2, [NOT_MOUNTED]),
    "X10": ([TWO], 1, [NOT_MOUNTED]),
    # Adversarial: a hop is not proven, or the child is not read: nothing is served for it.
    "X1": ([], 0, [NOT_MOUNTED]),
    "X1b": ([], 0, [NOT_MOUNTED]),
    "X2": ([], 1, [NOT_MOUNTED]),
    "X3": ([], 1, [NOT_MOUNTED]),
    "X4": ([], 1, [NOT_MOUNTED]),
    "X4b": ([], 1, [NOT_MOUNTED]),
    "X5": ([], 1, [NOT_MOUNTED]),
    "X7": ([], 1, [NOT_MOUNTED]),
    "X7b": ([], 1, [NOT_MOUNTED]),
    "X8": ([], 1, [NOT_MOUNTED]),
    "X9": ([], 1, [NOT_MOUNTED]),
    "X11b": (["GET /one"], 2, [NOT_MOUNTED]),
    # Failed prediction, see Q10 (amendment C): parameter named `app`, one claim (the helper).
    "X12": ([], 1, [NOT_MOUNTED]),
    "X12b": ([], 2, [BEFORE_BINDING, NOT_MOUNTED]),
    "X13": ([], 1, [NOT_MOUNTED]),
    "X15": (["GET /one"], 2, [CONTROL_FLOW, NOT_MOUNTED]),
    "X16": ([], 0, []),
    "X16b": ([], 0, []),
    "X17": ([], 1, [NOT_MOUNTED]),
    "X18": ([], 0, [NOT_MOUNTED]),
    "X19": ([], 0, [NOT_MOUNTED]),
    "X20": ([], 1, [NOT_MOUNTED]),
    "X21": ([], 1, [NOT_MOUNTED]),
}

#: The fixtures that serve the two-level route, and the route each serves.
SERVED: dict[str, str] = {
    **{name: TWO for name in ("Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q9", "Q10b", "X10")},
    "Q7": "GET /x/two",
}

SERVES_SQL = """SELECT e.parser, f.rel_path, t.name, e.line_start, e.line_end
                FROM edges e JOIN entities t ON t.id = e.dst_id
                LEFT JOIN files f ON f.id = e.file_id
                WHERE e.type = 'SERVES_HTTP_ROUTE' AND e.repo_id = ? ORDER BY t.name"""

CLAIMS_SQL = "SELECT meta_json FROM unresolved_refs WHERE repo_id = ?"


@dataclass(frozen=True)
class Seen:
    #: SERVES_HTTP_ROUTE edges: ``{parser, file, route, lines}``, ordered by route.
    serves: tuple[dict, ...]
    #: ``plan.code_routes`` rows: ``{body, declared_at, line_start, root_paths}``.
    plan: tuple[dict, ...]
    #: python-mount, python-helper and python-helper-call claims.
    claims: int
    #: Sorted codes of the repository's diagnostics.
    codes: tuple[str, ...]


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_plain(v) for v in value]
    if hasattr(value, "model_dump"):
        return _plain(value.model_dump(mode="json"))
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _plain(dataclasses.asdict(value))
    inner = getattr(value, "value", None)
    if isinstance(inner, str | int):
        return inner
    return str(value)


def _diagnostic_codes(obj: Any, in_diagnostic: bool = False) -> list[str]:
    """Every string ``code`` found under a key that contains ``diagnostic``."""
    found: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "code" and in_diagnostic and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_diagnostic_codes(value, in_diagnostic or "diagnostic" in str(key)))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_diagnostic_codes(item, in_diagnostic))
    return found


def observe_fixture(base: Path, name: str) -> Seen:
    repo_name = name.lower()
    workspace = open_built(base, name, {repo_name: fixture_repos()[repo_name]})
    try:
        repos = list(workspace.repositories.list_all())
        repo = next(r for r in repos if r.name == repo_name)
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
                    "root_paths": paths,
                }
            )
        claims = sum(
            1
            for (meta_json,) in workspace.database.execute(CLAIMS_SQL, (repo.id,)).fetchall()
            if meta_json and any(kind in meta_json for kind in KINDS)
        )
        slugs: dict[int, str] = {r.id: r.slug for r in repos if r.id is not None}
        inventory = repository_inventory(workspace, repo.id, slugs, include_diagnostics=True)
        codes = tuple(sorted(set(_diagnostic_codes(_plain(inventory)))))
    finally:
        workspace.close()
    return Seen(serves=serves, plan=tuple(rows), claims=claims, codes=codes)


@pytest.fixture(scope="module")
def observe(tmp_path_factory: pytest.TempPathFactory) -> Callable[[str], Seen]:
    """Observations of a fixture, each indexed once on first use."""
    base = tmp_path_factory.mktemp("r2c2")
    cache: dict[str, Seen] = {}

    def get(name: str) -> Seen:
        if name not in cache:
            cache[name] = observe_fixture(base, name)
        return cache[name]

    return get


def test_the_predictions_cover_every_fixture() -> None:
    assert sorted(EXPECTED) == sorted(FIXTURES)
    assert set(SERVED) <= set(EXPECTED)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_two_level_outcome(observe: Callable[[str], Seen], name: str) -> None:
    seen = observe(name)
    routes, claims, codes = EXPECTED[name]
    assert (sorted(s["route"] for s in seen.serves), seen.claims, list(seen.codes)) == (
        routes,
        claims,
        codes,
    )


@pytest.mark.parametrize("name", sorted(SERVED))
def test_two_level_served_route(observe: Callable[[str], Seen], name: str) -> None:
    seen = observe(name)
    route = SERVED[name]
    assert seen.serves == (
        {
            "parser": "linker:global",
            "file": "pkg/users/views.py",
            "route": route,
            "lines": [None, None],
        },
    )
    (row,) = [r for r in seen.plan if r["body"] == route]
    assert (row["line_start"], row["declared_at"]) == (None, 6)
    assert row["root_paths"] == ["pkg/app.py"]
