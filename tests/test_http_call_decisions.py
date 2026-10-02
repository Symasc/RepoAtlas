"""Every request's decision, stored (deployment HTTP roadmap, M5a; review decisions C1, C2, C4).

The global commit materializes the join's decision for every request in
``http_call_decisions``, replaced together with the HTTP edges: one per request claim,
and one per injected root a claim's request is made from, so no decision overwrites
another. A link's ``CALLS_HTTP_HANDLER`` edge carries its decision's ``decision_ref``,
and a refusal emits no edge. The ref is opaque, and the same for the same request
however the index got there. Each decision keeps its chain of hops, the failing one
marked.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest
from repoatlas.graph.http_chain import (
    NOT_REACHED,
    PROVEN,
    UNRESOLVED,
    Hop,
    chain_from_json,
)
from repoatlas.graph.linker import ReferenceLinker
from repoatlas.services.workspace import Workspace
from repoatlas.storage.protocols import HttpCallDecisionRow
from repoatlas.storage.sqlite.database import open_database
from repoatlas.storage.sqlite.stores import SqliteKnowledgeStore
from tests.http_crossing_corpus import (
    ACCOUNTS,
    ACCOUNTS_BOUND_LINE,
    ACCOUNTS_HOST_LINE,
    LOAD_USER_CALL_LINE,
    WORKSPACE,
)
from tests.impact_corpus import build
from tests.incremental_corpus import edit, fresh_index, sync_all
from tests.test_http_crossings import ALTERNATED
from tests.test_http_ts_injection import MAIN, WEB


def stored(workspace: Workspace) -> list[HttpCallDecisionRow]:
    return workspace.knowledge.list_http_call_decisions()


def handler_refs(workspace: Workspace) -> list[str]:
    rows = workspace.database.execute(
        "SELECT resolution FROM edges WHERE type = 'CALLS_HTTP_HANDLER'"
    ).fetchall()
    return [json.loads(row[0])["http"]["decision_ref"] for row in rows]


def by_caller(workspace: Workspace) -> dict[str, list[HttpCallDecisionRow]]:
    names = {
        row["key"]: row["qualified_name"]
        for row in workspace.database.execute("SELECT key, qualified_name FROM entities")
    }
    found: dict[str, list[HttpCallDecisionRow]] = {}
    for row in stored(workspace):
        found.setdefault(names[row.src_key], []).append(row)
    return found


def statuses(chain: tuple[Hop, ...]) -> list[tuple[str, str]]:
    return [(hop.kind, hop.status) for hop in chain]


@pytest.fixture
def joined(workspace: Workspace, tmp_path: Path) -> Workspace:
    return build(workspace, tmp_path, WORKSPACE)


# -- stored as the join decided ------------------------------------------------------------


def test_every_request_s_decision_is_stored_as_the_join_made_it(joined: Workspace) -> None:
    plan = ReferenceLinker(joined.knowledge).prepare_global().http_crossings
    rows = stored(joined)
    assert sorted((row.decision_ref, row.decision) for row in rows) == sorted(
        (call.ref, call.decision) for call in plan.calls
    )
    assert all(re.fullmatch(r"hcd1-[0-9a-f]{20}", row.decision_ref) for row in rows)
    assert {"linked", "http-route-unmatched", "http-base-unknown"} <= {r.decision for r in rows}
    assert joined.knowledge.http_call_decisions_built()


@pytest.mark.parametrize("repositories", [WORKSPACE, {"accounts": ACCOUNTS, "portal": ALTERNATED}])
def test_each_handler_edge_is_exactly_one_stored_linked_decision(
    workspace: Workspace, tmp_path: Path, repositories: dict[str, dict[str, str]]
) -> None:
    """The invariant: a link is an edge and a linked decision, each naming the other, and
    a refusal never has an edge."""
    built = build(workspace, tmp_path, repositories)
    edges = Counter(handler_refs(built))
    rows = stored(built)
    linked = Counter(row.decision_ref for row in rows if row.decision == "linked")
    assert edges and edges == linked
    assert set(edges).isdisjoint(row.decision_ref for row in rows if row.decision != "linked")


def test_literal_alternatives_are_stored_one_decision_each(
    workspace: Workspace, tmp_path: Path
) -> None:
    built = build(workspace, tmp_path, {"accounts": ACCOUNTS, "portal": ALTERNATED})
    rows = by_caller(built)["pick"]
    assert sorted((row.variant, row.decision) for row in rows) == [
        ("alternative:6", "linked"),
        ("alternative:8", "http-service-unmatched"),
    ]
    assert len({row.decision_ref for row in rows}) == 2
    assert handler_refs(built) == [r.decision_ref for r in rows if r.decision == "linked"]


MIRRORED = MAIN + (
    "\nconst mirrorBaseUrl = 'https://mirror.example/api';\n"
    "const mirrorOpts = { accountsBaseUrl: mirrorBaseUrl, timeoutMs: 3000, fetcher: superagent };\n"
    "\nexport const mirrored = createModels({\n"
    "  accountsOpts: mirrorOpts,\n"
    "  otherOpts: { otherBaseUrl: emptyBaseUrl, fetcher: superagent },\n"
    "});\n"
)


def test_requests_from_two_injected_roots_are_stored_one_decision_each(
    workspace: Workspace, tmp_path: Path
) -> None:
    """One transport injected from two roots with two bases: the claim is one, its
    requests two, and so are their decisions."""
    built = build(
        workspace, tmp_path, {"accounts": ACCOUNTS, "web": {**WEB, "src/main.ts": MIRRORED}}
    )
    rows = by_caller(built)["createUserModel.getUser"]
    assert len({(row.file_id, row.line_start) for row in rows}) == 1
    assert sorted((row.decision, row.url) for row in rows) == [
        ("http-service-unmatched", "https://mirror.example/api/users/{id}"),
        ("linked", "https://accounts.example/api/users/{id}"),
    ]
    assert len({row.decision_ref for row in rows}) == 2
    assert all(row.variant.startswith("root:src/main.ts:") for row in rows)
    assert len({row.variant for row in rows}) == 2


# -- identity and replacement -------------------------------------------------------------


def test_a_decision_ref_is_the_same_after_a_sync_and_in_a_fresh_index(
    joined: Workspace, settings, tmp_path: Path
) -> None:
    before = sorted((row.decision_ref, row.decision) for row in stored(joined))
    root = tmp_path / "repos"
    text = WORKSPACE["portal"]["client.py"]
    edit(root, "portal", "client.py", text + "\n# touched\n")
    sync_all(joined, root)
    edit(root, "portal", "client.py", text)
    sync_all(joined, root)
    assert sorted((row.decision_ref, row.decision) for row in stored(joined)) == before
    with fresh_index(settings, root, tmp_path, "refs") as fresh:
        assert sorted((row.decision_ref, row.decision) for row in stored(fresh)) == before


def test_a_sync_replaces_every_stored_decision(joined: Workspace, tmp_path: Path) -> None:
    root = tmp_path / "repos"
    text = WORKSPACE["portal"]["client.py"]
    moved = text.replace("https://accounts.example/api/users/123", "https://gone.example/users/123")
    assert moved != text
    edit(root, "portal", "client.py", moved)
    sync_all(joined, root)
    plan = ReferenceLinker(joined.knowledge).prepare_global().http_crossings
    assert sorted(row.decision_ref for row in stored(joined)) == sorted(c.ref for c in plan.calls)
    assert Counter(handler_refs(joined)) == Counter(
        row.decision_ref for row in stored(joined) if row.decision == "linked"
    )


TWINNED = MIRRORED.replace("https://mirror.example/api", "https://accounts.example/api")


def test_two_roots_with_the_same_base_are_two_decisions_and_two_links(
    workspace: Workspace, tmp_path: Path
) -> None:
    """The same URL from two roots is still two requests: neither root's decision, with
    its own options and base, overwrites the other's."""
    built = build(
        workspace, tmp_path, {"accounts": ACCOUNTS, "web": {**WEB, "src/main.ts": TWINNED}}
    )
    rows = by_caller(built)["createUserModel.getUser"]
    assert len({row.variant for row in rows}) == 2
    assert {row.url for row in rows} == {"https://accounts.example/api/users/{id}"}
    assert [row.decision for row in rows] == ["linked", "linked"]
    linked = [row.decision_ref for row in stored(built) if row.decision == "linked"]
    assert Counter(handler_refs(built)) == Counter(linked)


def test_a_request_s_ref_does_not_depend_on_another_request(
    workspace: Workspace, tmp_path: Path
) -> None:
    """Two roots alike in all but where they are written: each ref is its own, so a change
    to either root's base leaves the other's ref as it was, whichever was decided first."""
    built = build(
        workspace, tmp_path, {"accounts": ACCOUNTS, "web": {**WEB, "src/main.ts": TWINNED}}
    )

    def refs() -> dict[str, str]:
        rows = by_caller(built)["createUserModel.getUser"]
        return {row.variant: row.decision_ref for row in rows}

    before = refs()
    assert len(before) == 2 and len(set(before.values())) == 2
    root = tmp_path / "repos"
    first = TWINNED.replace(
        "?? 'https://accounts.example/api'", "?? 'https://elsewhere.example/api'"
    )
    for changed in (MIRRORED, first):
        assert changed != TWINNED
        edit(root, "web", "src/main.ts", changed)
        sync_all(built, root)
        after = refs()
        assert len(set(after.values()) & set(before.values())) == 1, changed
    edit(root, "web", "src/main.ts", TWINNED)
    sync_all(built, root)
    assert refs() == before


def test_a_stored_decision_goes_with_its_request_whichever_file_said_so(
    workspace: Workspace, tmp_path: Path
) -> None:
    """Moving an injected root moves its request's variant: the request's own file is
    untouched, and its old decision still does not survive the sync."""
    built = build(
        workspace, tmp_path, {"accounts": ACCOUNTS, "web": {**WEB, "src/main.ts": MIRRORED}}
    )
    before = {row.variant for row in stored(built)}
    moved = MIRRORED.replace("\nexport const mirrored", "\n// moved\nexport const mirrored")
    assert moved != MIRRORED
    edit(tmp_path / "repos", "web", "src/main.ts", moved)
    sync_all(built, tmp_path / "repos")
    plan = ReferenceLinker(built.knowledge).prepare_global().http_crossings
    assert sorted(row.decision_ref for row in stored(built)) == sorted(c.ref for c in plan.calls)
    after = {row.variant for row in stored(built)}
    # The moved root's requests are decided under its new place, and only under it.
    assert len(before - after) == 1 and len(after - before) == 1
    assert len(by_caller(built)["createUserModel.getUser"]) == 2


def test_an_index_no_global_pass_has_written_says_so(tmp_path: Path) -> None:
    knowledge = SqliteKnowledgeStore(open_database(tmp_path / "atlas.db"))
    assert not knowledge.http_call_decisions_built()
    assert knowledge.list_http_call_decisions() == []
    knowledge.replace_http_call_decisions([])
    assert knowledge.http_call_decisions_built()


# -- the chain ----------------------------------------------------------------------------


def test_a_link_s_chain_proves_every_hop_where_the_source_does(joined: Workspace) -> None:
    [row] = by_caller(joined)["load_user"]
    chain = chain_from_json(row.chain_json)
    assert statuses(chain) == [
        ("caller", PROVEN),
        ("origin", PROVEN),
        ("service_identity", PROVEN),
        ("deployment_binding", PROVEN),
        ("source_binding", PROVEN),
        ("route", PROVEN),
        ("handler", PROVEN),
    ]
    places = {hop.kind: [(p.path, p.line_start, p.role) for p in hop.provenance] for hop in chain}
    assert places["caller"] == [("client.py", LOAD_USER_CALL_LINE, "called_at")]
    # A specification's host states the deployment where it states the identity.
    assert places["service_identity"] == [("app.py", ACCOUNTS_HOST_LINE, "identity_at")]
    assert places["deployment_binding"] == places["service_identity"]
    assert places["source_binding"] == [("app.py", ACCOUNTS_BOUND_LINE, "bound_at")]
    assert chain[-1].entity_id is not None


def test_a_refusal_s_chain_marks_the_hop_that_failed_and_cites_nothing_after(
    joined: Workspace,
) -> None:
    callers = by_caller(joined)
    [spans] = callers["spans"]
    chain = chain_from_json(spans.chain_json)
    assert statuses(chain)[-2:] == [("route", UNRESOLVED), ("handler", NOT_REACHED)]
    assert chain[-2].reason == "route_unmatched"
    [dynamic] = callers["dynamic"]
    chain = chain_from_json(dynamic.chain_json)
    assert statuses(chain) == [
        ("caller", PROVEN),
        ("origin", UNRESOLVED),
        *[(kind, NOT_REACHED) for kind in ("service_identity", "deployment_binding")],
        *[(kind, NOT_REACHED) for kind in ("source_binding", "route", "handler")],
    ]
    assert chain[1].reason == "host_not_stated"
    assert all(not hop.provenance for hop in chain if hop.status == NOT_REACHED)
