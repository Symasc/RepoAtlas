"""DF3c: refused HTTP requests in impact answers (DF3C_PHASE_A.md, §13 for the review amendments).

A refusal records the handlers its link would reach (``would_reach``) only when its own evidence
establishes where the source is; an impact answer lists the refused requests where its walk went,
``at`` (made by a reached caller or in a reached file) or ``toward`` (hypothetical: it would reach
a reached handler if it were linked) -- and never follows one.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any, cast

import pytest
from repoatlas.capabilities import service
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.impact import ImpactRequest, ImpactResponse
from repoatlas.services.workspace import Workspace
from tests.http_crossing_corpus import decided, links
from tests.impact_corpus import build
from tests.incremental_corpus import assert_equals_fresh, edit, fresh_index, sync_all
from tests.test_deploy_ingress import CALLER, chart

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)


def d(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


#: The chart's repository and its one application: GET and POST on one item, a deeper route,
#: and a route that answers the same shape under another name.
DEVICES_APP = d("""
    from fastapi import FastAPI

    app = FastAPI()


    @app.get("/items/{item_id}")
    def get_item(item_id: str):
        return {}


    @app.post("/items/{item_id}")
    def post_item(item_id: str):
        return {}


    @app.get("/items/{item_id}/tags")
    def item_tags(item_id: str):
        return {}
""")
DEVICES = {"pyproject.toml": '[project]\nname = "devices"\n', "app.py": DEVICES_APP}


def unbound(**extra: str) -> dict[str, dict[str, str]]:
    """The devices chart with no release here: every host it gives is a pattern, tied to the
    repository's one application."""
    return {"devices": {**DEVICES, **chart(enabled=None), **extra}, "caller": CALLER}


def impact(workspace: Workspace, seed: str, context: CapabilityContext = GLOBAL) -> ImpactResponse:
    return CapabilityService(workspace).impact(
        ImpactRequest(context=context, seed=seed, max_candidates=100)
    )


def http(response: ImpactResponse) -> list[tuple[str | None, str, str]]:
    return [(d.relation, d.at, d.decision) for d in response.decisions if d.family == "http"]


def key(workspace: Workspace, repository: str, name: str) -> str:
    [(found,)] = workspace.database.execute(
        """SELECT e.key FROM entities e JOIN repositories r ON r.id = e.repo_id
           WHERE r.slug = ? AND e.qualified_name = ? AND e.type NOT IN ('module', 'file')""",
        (repository, name),
    ).fetchall()
    return str(found)


def decisions(workspace: Workspace) -> list[tuple[object, ...]]:
    """Every stored HTTP call decision, id-free: its ref, caller, decision and evidence (where
    would_reach is), and its chain without row ids."""

    def bare(value: object) -> object:
        if isinstance(value, dict):
            return {k: bare(v) for k, v in value.items() if k not in ("entity_id", "repo_id")}
        if isinstance(value, list):
            return [bare(v) for v in value]
        return value

    rows = workspace.database.execute(
        """SELECT d.decision_ref, s.key, d.decision, d.evidence_json, d.chain_json
           FROM http_call_decisions d JOIN entities s ON s.id = d.src_id ORDER BY d.decision_ref"""
    ).fetchall()
    return [(*row[:4], json.dumps(bare(json.loads(row[4])), sort_keys=True)) for row in rows]


def stored(workspace: Workspace) -> dict[str, dict[str, Any]]:
    rows = workspace.database.execute(
        """SELECT s.qualified_name, d.decision, d.evidence_json FROM http_call_decisions d
           JOIN entities s ON s.id = d.src_id"""
    ).fetchall()
    return {row[0]: {"decision": row[1], **json.loads(row[2])} for row in rows}


# -- would_reach, at the join -------------------------------------------------------------------


#: A document in the chart's repository serving the same route: no declaration, no handler.
SPEC = d("""
    openapi: 3.0.3
    info:
      title: devices
      version: "1"
    paths:
      /items/{item_id}:
        get:
          responses:
            "200":
              description: ok
""")


def test_a_tied_binding_refusal_would_reach_its_method_and_shape_only(
    workspace: Workspace, tmp_path: Path
) -> None:
    elsewhere = {"pyproject.toml": '[project]\nname = "elsewhere"\n', "app.py": DEVICES_APP}
    built = build(
        workspace, tmp_path, {**unbound(**{"openapi.yaml": SPEC}), "elsewhere": elsewhere}
    )
    refusals = stored(built)
    prd = refusals["prd_item"]
    assert prd["decision"] == "http-deployment-binding-unresolved"
    # GET /items/{x} of the chart's own repository: not POST /items/{x}, not GET
    # /items/{x}/tags, not the document's route, not another repository's same route
    assert prd["would_reach"] == [
        {
            "handler": key(built, "devices", "get_item"),
            "route": "GET /items/{item_id}",
            "repository": "devices",
        }
    ]
    assert "would_reach_omitted" not in prd
    # the plain-http request matches no identity: no source, no handler
    assert refusals["plain_item"]["decision"] == "http-service-unmatched"
    assert "would_reach" not in refusals["plain_item"]


#: An Express application whose one route is registered at module scope: its file serves it.
EXPRESS = d("""
    import express from "express";

    const app = express();

    app.get("/items/:itemId", (req, res) => {
      res.json({});
    });

    app.listen(8080);
""")


def test_a_route_its_file_serves_names_no_handler(workspace: Workspace, tmp_path: Path) -> None:
    """A file is no declaration: a tied refusal of the route's shape names no handler, never a
    key that is no entity's."""
    devices = {
        "package.json": '{"name": "devices", "dependencies": {"express": "4.0.0"}}',
        "src/server.ts": EXPRESS,
        **chart(enabled=None),
    }
    built = build(workspace, tmp_path, {"devices": devices, "caller": CALLER})
    [(server,)] = built.database.execute(
        """SELECT s.type FROM edges e JOIN entities s ON s.id = e.src_id
           WHERE e.type = 'SERVES_HTTP_ROUTE'"""
    ).fetchall()
    assert server == "file"
    prd = stored(built)["prd_item"]
    assert (prd["decision"], prd["source_outcome"]) == (
        "http-deployment-binding-unresolved",
        "tied",
    )
    assert "would_reach" not in prd


def test_a_refusal_whose_source_is_not_tied_reaches_nothing(
    workspace: Workspace, tmp_path: Path
) -> None:
    """A deployment repository serving no code: its identity is no proof of a source, and a
    handler of the same shape elsewhere is never named."""
    deploy = {"pyproject.toml": '[project]\nname = "deploy"\n', **chart(enabled=True)}
    built = build(workspace, tmp_path, {"deploy": deploy, "elsewhere": DEVICES, "caller": CALLER})
    refusals = stored(built)
    assert refusals["dev_item"]["decision"] == "http-deployment-source-unresolved"
    assert refusals["prd_item"]["decision"] == "http-deployment-binding-unresolved"
    assert not [name for name, row in refusals.items() if "would_reach" in row]


def test_a_source_unresolved_refusal_gains_no_handler_by_route_shape(
    workspace: Workspace, tmp_path: Path
) -> None:
    """An identity no code is behind: a route of the request's shape exposed by another
    service names nothing."""
    deploy = {"pyproject.toml": '[project]\nname = "deploy"\n', **chart(enabled=True, name="other")}
    asking = CALLER["client.py"] + d("""


        def other_item(item_id):
            return requests.get(f"https://other-dev.int.example.com/items/{item_id}", timeout=5)
    """)
    built = build(
        workspace,
        tmp_path,
        {
            "deploy": deploy,
            "devices": {**DEVICES, **chart(enabled=True)},
            "caller": {**CALLER, "client.py": asking},
        },
    )
    refusals = stored(built)
    assert refusals["dev_item"]["decision"] == "linked"
    assert refusals["other_item"]["decision"] == "http-deployment-source-unresolved"
    assert "would_reach" not in refusals["other_item"]


def test_a_linked_decision_carries_no_would_reach(workspace: Workspace, tmp_path: Path) -> None:
    built = build(
        workspace, tmp_path, {"devices": {**DEVICES, **chart(enabled=True)}, "caller": CALLER}
    )
    assert {caller for caller, *_ in links(built)} == {"dev_item", "public_item"}
    refusals = stored(built)
    assert refusals["dev_item"]["decision"] == "linked"
    assert not [
        name
        for name, row in refusals.items()
        if row["decision"] == "linked" and "would_reach" in row
    ]
    assert "would_reach" in refusals["prd_item"]


def test_several_identities_are_no_proven_source(workspace: Workspace, tmp_path: Path) -> None:
    mirror = {"pyproject.toml": '[project]\nname = "mirror"\n', **chart(enabled=None)}
    built = build(
        workspace,
        tmp_path,
        {"devices": {**DEVICES, **chart(enabled=True)}, "mirror": mirror, "caller": CALLER},
    )
    [(decision, evidence)] = decided(built, "caller")["dev_item"]
    assert decision == "http-provider-ambiguous"
    assert "would_reach" not in evidence


def test_several_routes_of_one_proven_service_reach_each(
    workspace: Workspace, tmp_path: Path
) -> None:
    twice = DEVICES_APP + d("""


        @app.get("/items/{name}")
        def by_name(name: str):
            return {}
    """)
    built = build(
        workspace,
        tmp_path,
        {"devices": {**DEVICES, "app.py": twice, **chart(enabled=True)}, "caller": CALLER},
    )
    [(decision, evidence)] = decided(built, "caller")["dev_item"]
    assert decision == "http-provider-ambiguous"
    reach = cast("list[dict[str, str]]", evidence["would_reach"])
    assert [h["handler"] for h in reach] == [
        key(built, "devices", "by_name"),
        key(built, "devices", "get_item"),
    ]


def test_would_reach_is_capped_in_order_and_counts_the_rest(
    workspace: Workspace, tmp_path: Path
) -> None:
    # the handlers named against their routes' order: kept by handler, not as served
    many = "from fastapi import FastAPI\n\napp = FastAPI()\n" + "".join(
        f'\n\n@app.get("/items/{{k{i}}}")\ndef h{6 - i}(k{i}: str):\n    return {{}}\n'
        for i in range(7)
    )
    built = build(workspace, tmp_path, unbound(**{"app.py": many}))
    prd = stored(built)["prd_item"]
    assert [h["handler"].split(":")[3] for h in prd["would_reach"]] == [
        "h0",
        "h1",
        "h2",
        "h3",
        "h4",
    ]
    assert prd["would_reach_omitted"] == 2


# -- an impact answer -------------------------------------------------------------------------


def test_a_reached_handler_lists_the_refusals_that_would_reach_it_as_hypothetical(
    workspace: Workspace, tmp_path: Path
) -> None:
    built = build(workspace, tmp_path, unbound())
    response = impact(built, key(built, "devices", "get_item"))
    assert response.summary.policy == "impact/7"
    # with no release, every host the Ingress gives is a pattern tied to the one application;
    # plain http matches none
    assert http(response) == [
        ("toward", f"caller/client.py:{line}", "http-deployment-binding-unresolved")
        for line in (5, 9, 13, 21)
    ]
    [toward] = [
        d for d in response.decisions if d.family == "http" and d.at == "caller/client.py:13"
    ]
    assert toward.evidence["hypothetical"] is True
    assert toward.evidence["would_reach"] == [
        {
            "handler": key(built, "devices", "get_item"),
            "route": "GET /items/{item_id}",
            "repository": "devices",
        }
    ]
    assert toward.evidence["source_status"] == "proven"
    assert toward.evidence["request"] == "GET https://devices-prd.int.example.com/items/{item_id}"
    # never followed: no candidate is a caller of the refused requests
    assert not [c for c in response.candidates if c.repository == "caller"]
    # a handler the refusal does not name lists nothing
    assert http(impact(built, key(built, "devices", "post_item"))) == []


def test_a_reached_caller_lists_its_own_refusal_and_never_its_neighbours(
    workspace: Workspace, tmp_path: Path
) -> None:
    built = build(
        workspace, tmp_path, {"devices": {**DEVICES, **chart(enabled=True)}, "caller": CALLER}
    )
    # dev_item is linked: nothing. prd_item, in the same file, is not reached by it.
    assert http(impact(built, key(built, "caller", "dev_item"))) == []
    response = impact(built, key(built, "caller", "prd_item"))
    assert http(response) == [("at", "caller/client.py:13", "http-deployment-binding-unresolved")]
    [at] = [d for d in response.decisions if d.family == "http"]
    assert at.evidence["reached"] == "caller"
    assert "would_reach" not in at.evidence and "hypothetical" not in at.evidence
    # a reached file lists the requests made in it, as reached by file
    whole = impact(built, "file:caller:client.py")
    assert whole.seed.status == "resolved"
    assert http(whole) == [
        ("at", "caller/client.py:13", "http-deployment-binding-unresolved"),
        ("at", "caller/client.py:17", "http-service-unmatched"),
        ("at", "caller/client.py:21", "http-deployment-binding-unresolved"),
    ]
    # the file's own entity is what the walk reached, not the callers in it
    assert {d.evidence["reached"] for d in whole.decisions if d.family == "http"} == {"file"}


def test_a_refusal_both_at_and_toward_is_listed_once_as_at(
    workspace: Workspace, tmp_path: Path
) -> None:
    probe = d("""
        import requests

        from app import get_item


        def probe(item_id):
            get_item(item_id)
            return requests.get(f"https://devices-prd.int.example.com/items/{item_id}", timeout=5)
    """)
    built = build(workspace, tmp_path, unbound(**{"probe.py": probe}))
    response = impact(built, key(built, "devices", "get_item"))
    assert ("at", "devices/probe.py:8", "http-deployment-binding-unresolved") in http(response)
    assert [r for r in http(response) if r[1] == "devices/probe.py:8"] == [
        ("at", "devices/probe.py:8", "http-deployment-binding-unresolved")
    ]


def test_the_http_cap_is_its_own_and_ordered(workspace: Workspace, tmp_path: Path) -> None:
    calls = "import requests\n\n\ndef many(i):\n" + "".join(
        f'    requests.get(f"https://unknown-{n}.example.com/x/{{i}}", timeout=5)\n'
        for n in range(13)
    )
    built = build(
        workspace,
        tmp_path,
        {"caller": {"pyproject.toml": '[project]\nname = "caller"\n', "many.py": calls}},
    )
    response = impact(built, key(built, "caller", "many"))
    listed = http(response)
    assert [line for _, line, _ in listed] == [f"caller/many.py:{n}" for n in range(5, 15)]
    assert {relation for relation, _, _ in listed} == {"at"}
    assert response.decisions_omitted.get("http") == 3


def test_a_repository_scope_lists_only_requests_made_inside_it(
    workspace: Workspace, tmp_path: Path
) -> None:
    built = build(workspace, tmp_path, unbound())
    devices = CapabilityContext(scope=CapabilityScope.REPOSITORY, repository="devices")
    assert http(impact(built, key(built, "devices", "get_item"), devices)) == []
    assert len(http(impact(built, key(built, "devices", "get_item")))) == 4


def test_link_decisions_are_written_as_before(workspace: Workspace, tmp_path: Path) -> None:
    built = build(workspace, tmp_path, unbound())
    for decision in impact(built, key(built, "devices", "get_item")).model_dump(mode="json")[
        "decisions"
    ]:
        assert ("relation" in decision) == (decision["family"] == "http")


# -- incremental equals fresh -------------------------------------------------------------------


def test_every_change_to_a_route_a_binding_a_source_or_a_caller_equals_a_fresh_index(
    workspace: Workspace, settings: Settings, tmp_path: Path
) -> None:
    built = build(workspace, tmp_path, unbound())
    root = tmp_path / "repos"

    def reach() -> list[str]:
        return [h["handler"] for h in stored(built)["prd_item"].get("would_reach", [])]

    def step(tag: str) -> None:
        sync_all(built, root)
        assert_equals_fresh(built, settings, root, tmp_path, tag)
        # the facts snapshot holds no HTTP call decision: compare them, would_reach included
        with fresh_index(settings, root, tmp_path, f"{tag}-http") as fresh:
            assert decisions(built) == decisions(fresh)

    assert reach() == [key(built, "devices", "get_item")]
    # a route of the same shape added
    edit(
        root,
        "devices",
        "app.py",
        DEVICES_APP + '\n\n@app.get("/items/{key}")\ndef by_key(key: str):\n    return {}\n',
    )
    step("route-added")
    assert reach() == [key(built, "devices", "by_key"), key(built, "devices", "get_item")]
    # the handler renamed, and the added one removed
    edit(root, "devices", "app.py", DEVICES_APP.replace("def get_item", "def fetch_item"))
    step("route-renamed")
    assert reach() == [key(built, "devices", "fetch_item")]
    # the caller now asks for a shape no route serves
    edit(
        root,
        "caller",
        "client.py",
        CALLER["client.py"].replace(
            "devices-prd.int.example.com/items/{item_id}",
            "devices-prd.int.example.com/other/{item_id}",
        ),
    )
    step("caller-edited")
    assert reach() == []
    edit(root, "caller", "client.py", CALLER["client.py"])
    # the source untied: the application gone, the chart left
    edit(root, "devices", "app.py", "")
    step("source-untied")
    assert reach() == []
    edit(root, "devices", "app.py", DEVICES_APP)
    step("source-tied")
    assert reach() == [key(built, "devices", "get_item")]
    # the Ingress host changed: the request fits no pattern, and names nothing
    ingress = "charts/devices/templates/ingress.yaml"
    edit(
        root,
        "devices",
        ingress,
        chart(enabled=None)[ingress].replace('"{{ $.Chart', '"api-{{ $.Chart'),
    )
    step("host-changed")
    assert stored(built)["prd_item"]["decision"] == "http-service-unmatched"
    assert reach() == []
    edit(root, "devices", ingress, chart(enabled=None)[ingress])
    # the binding established: the request links, and its refusal is gone
    edit(
        root,
        "devices",
        "Tiltfile",
        chart(enabled=True)["Tiltfile"].replace("stage=dev", "stage=prd"),
    )
    step("bound")
    assert stored(built)["prd_item"]["decision"] == "linked"
    assert "would_reach" not in stored(built)["prd_item"]


@pytest.mark.parametrize(("repository", "name"), [("devices", "get_item"), ("caller", "prd_item")])
def test_an_answer_is_the_same_walk_with_or_without_its_http_decisions(
    workspace: Workspace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repository: str,
    name: str,
) -> None:
    """The decisions are read after the walk, from its answer: without them nothing else changes."""
    built = build(workspace, tmp_path, unbound())
    seed = key(built, repository, name)
    dropped = ("decisions", "decisions_omitted", "investigation_id", "context")

    def answer() -> dict[str, Any]:
        return {
            k: v for k, v in impact(built, seed).model_dump(mode="json").items() if k not in dropped
        }

    with_http = impact(built, seed)
    assert [d for d in with_http.decisions if d.family == "http"]
    found = answer()
    monkeypatch.setattr(service, "http_decisions_near", lambda *_, **__: ((), 0))
    assert answer() == found
    assert all(c.step.edge != "CALLS_HTTP_HANDLER" for c in with_http.candidates)
