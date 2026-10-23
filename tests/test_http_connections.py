"""HTTP Connections: the worklist, the preview and operator declarations (Phase A1).

The corpus (tests/http_connections_corpus.py): ``shop`` and ``orders`` serve routes, ``twin`` serves
``shop``'s route twice, ``svc`` is published by its compose file; ``buyer``, ``tools`` and ``batch``
make requests the source cannot link. Each test names the case it pins: the positive cases P1-P10
and forbidden cases F1-F15 of the Phase A design, and the negatives added at review.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from repoatlas.capabilities.errors import InvalidDeploymentLinkError
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.deployment_targets import (
    DeclarationProvenance,
    DeploymentLinkCreateRequest,
    DeploymentLinkDeleteRequest,
    DeploymentLinkFields,
    DeploymentLinkUpdateRequest,
)
from repoatlas.models.http_connections import (
    ConnectionGroup,
    HttpConnectionPreviewRequest,
    HttpConnectionsRequest,
    HttpConnectionsResponse,
    HttpDeclarationHistoryRequest,
)
from repoatlas.models.impact import ImpactRequest
from repoatlas.services.workspace import Workspace, open_workspace
from tests.http_connections_corpus import repos
from tests.impact_corpus import build
from tests.incremental_corpus import sync_all

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)
SHOP_URL = "https://shop.example.test"


@pytest.fixture
def built(workspace: Workspace, tmp_path: Path) -> Workspace:
    return build(workspace, tmp_path, repos())


def service(workspace: Workspace) -> CapabilityService:
    return CapabilityService(workspace)


def slug(workspace: Workspace, name: str) -> str:
    return next(r.slug for r in workspace.repositories.list_all() if r.name == name)


def repository_context(workspace: Workspace, name: str) -> CapabilityContext:
    return CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=slug(workspace, name))


def worklist(workspace: Workspace, **filters: object) -> HttpConnectionsResponse:
    return service(workspace).http_connections(HttpConnectionsRequest(context=GLOBAL, **filters))  # type: ignore[arg-type]


def group_of(response: HttpConnectionsResponse, origin: str) -> ConnectionGroup:
    return next(g for g in response.groups if g.origin == origin)


def declare(
    workspace: Workspace,
    name: str,
    url: str,
    *,
    callers: tuple[str, ...] = (),
    environment: str | None = None,
    group: str | None = None,
    note: str | None = None,
    label: str = "mapped",
) -> int:
    provenance = DeclarationProvenance(via="ui", note=note, request_group=group)
    return (
        service(workspace)
        .deployment_link_create(
            DeploymentLinkCreateRequest(
                context=repository_context(workspace, name),
                link=DeploymentLinkFields(
                    label=label, url=url, callers=list(callers), environment=environment
                ),
                provenance=provenance,
            )
        )
        .link.id
    )


def revoke(workspace: Workspace, name: str, link_id: int, note: str | None = None) -> None:
    service(workspace).deployment_link_delete(
        DeploymentLinkDeleteRequest(
            context=repository_context(workspace, name),
            id=link_id,
            provenance=DeclarationProvenance(via="ui", note=note),
        )
    )


def decisions(workspace: Workspace) -> dict[str, tuple[str, dict[str, object]]]:
    """(decision, evidence) by caller name, with the request's own method and URL."""
    rows = workspace.database.execute(
        """SELECT e.qualified_name, d.decision, d.evidence_json FROM http_call_decisions d
           JOIN entities e ON e.id = d.src_id"""
    ).fetchall()
    return {name: (decision, json.loads(evidence)) for name, decision, evidence in rows}


def decision_table(workspace: Workspace) -> list[tuple[object, ...]]:
    """Every decision, without row ids: a chain names its handler's entity row, which no two
    indexes share, for source-proven links as much as for declared ones."""

    def without_ids(value: object) -> object:
        if isinstance(value, dict):
            return {k: without_ids(v) for k, v in value.items() if k != "entity_id"}
        if isinstance(value, list):
            return [without_ids(v) for v in value]
        return value

    return [
        (ref, decision, evidence, json.dumps(without_ids(json.loads(chain)), sort_keys=True))
        for ref, decision, evidence, chain in workspace.database.execute(
            """SELECT decision_ref, decision, evidence_json, chain_json
               FROM http_call_decisions ORDER BY decision_ref"""
        ).fetchall()
    ]


def audit_rows(workspace: Workspace) -> list[tuple[object, ...]]:
    return [
        tuple(row)
        for row in workspace.database.execute(
            "SELECT action, subject_id, repository, via, request_group, note "
            "FROM declaration_audit "
            "ORDER BY id"
        ).fetchall()
    ]


def sync(workspace: Workspace, tmp_path: Path) -> None:
    sync_all(workspace, tmp_path / "repos")


def state_of(workspace: Workspace, link_id: int) -> tuple[str, str]:
    view = next(d for d in worklist(workspace).declarations if d.id == link_id)
    return view.state, view.reason


# -- the worklist ---------------------------------------------------------------------------------


def test_every_unresolved_request_is_listed_and_the_counts_reconcile_with_the_decisions(
    built: Workspace,
) -> None:
    """P6: the worklist is the decisions table, grouped; nothing is dropped or counted twice."""
    response = worklist(built)
    table = decisions(built)
    unresolved = [name for name, (decision, _) in table.items() if decision != "linked"]
    assert response.summary.requests == len(table) == 9
    assert response.summary.unresolved == len(unresolved) == 8
    assert sum(g.requests for g in response.groups) == response.summary.unresolved
    assert response.summary.linked_by_source == 1
    assert response.summary.linked_by_declaration == 0
    assert sum(response.summary.by_status.values()) == response.summary.unresolved
    # one group per target: three stated origins and one base the source does not state
    assert {g.origin for g in response.groups} == {
        SHOP_URL,
        "http://localhost:8000",
        "http://svc:8080",
        None,
    }


def test_python_callers_are_labelled_and_filterable_by_language_status_and_caller(
    built: Workspace,
) -> None:
    """P7."""
    assert {g.languages.get("python") for g in worklist(built).groups} == {1, 2, 4}
    assert worklist(built, language="typescript").groups == ()
    only_buyer = worklist(built, caller=slug(built, "buyer"))
    assert sum(g.requests for g in only_buyer.groups) == 6
    refused = worklist(built, status=("http-route-unmatched",))
    assert [g.origin for g in refused.groups] == ["http://svc:8080"]
    assert [g.origin for g in worklist(built, text="localhost").groups] == ["http://localhost:8000"]


def test_a_stated_origin_is_mappable_and_an_unstated_base_is_listed_but_not(
    built: Workspace,
) -> None:
    """Scope of A1: all unresolved requests are shown; only a stated origin may be mapped."""
    response = worklist(built)
    base = next(g for g in response.groups if g.kind == "base-not-stated")
    assert (base.base_expression, base.declarable, base.reasons) == (
        "{base}",
        0,
        {"host_not_stated": 1},
    )
    assert all(s.declarable is False for s in base.samples)
    published = group_of(response, "http://svc:8080")
    # the source published this origin: a request it refused is not the operator's to remap
    assert published.declarable == 0
    assert group_of(response, SHOP_URL).declarable == 4


def test_a_request_shows_its_method_place_reason_and_a_safe_route(built: Workspace) -> None:
    """Safe URL display: method, place, refusal reason; identifier-like segments masked."""
    group = group_of(worklist(built), SHOP_URL)
    page = service(built).http_connections(HttpConnectionsRequest(context=GLOBAL, group=group.key))
    by_route = {r.route: r for r in page.requests}
    assert set(by_route) == {
        "/v1/items/{item_id}",
        "/v1/orders/{order_id}",
        "/v1/items",
        "/v1/reset/:id",
    }
    item = by_route["/v1/items/{item_id}"]
    assert item.method == "GET" and item.reason == "no_identity"
    assert (item.site.repository, item.site.path) == (slug(built, "buyer"), "buyer/client.py")
    assert item.site.line_start == 7
    assert by_route["/v1/items"].method == "POST"
    # the token-like literal is never shown
    assert "0b6c6e3e" not in page.model_dump_json()


def test_the_worklist_never_shows_a_query_credential_or_secret_looking_value(
    built: Workspace,
) -> None:
    """F5 (display): nothing sensitive reaches a group, a request, a declaration or the history."""
    text = worklist(built).model_dump_json()
    for needle in ("0b6c6e3e", "@", "?", "#", "password", "token="):
        assert needle not in text.replace("@declarations", ""), needle


# -- candidates and preview -----------------------------------------------------------------------


def test_candidates_are_services_whose_served_routes_match_never_a_name_guess(
    built: Workspace,
) -> None:
    """F11: a candidate is a hypothesis from route shapes; hostnames and names play no part."""
    group = group_of(worklist(built), SHOP_URL)
    page = service(built).http_connections(HttpConnectionsRequest(context=GLOBAL, group=group.key))
    names = {c.repository: c for c in page.candidates}
    assert set(names) == {slug(built, n) for n in ("shop", "svc", "twin", "orders")}
    shop = names[slug(built, "shop")]
    assert (shop.requests_matched, shop.requests_unique) == (2, 2)
    # the repository called "shop" is not preferred for being called that: order is by matches
    orders = names[slug(built, "orders")]
    assert (orders.requests_matched, orders.requests_unique) == (1, 1)
    assert names[slug(built, "twin")].requests_ambiguous == 1
    # nothing selected, nothing applied
    assert worklist(built).declarations == ()


def test_a_preview_states_each_requests_outcome_before_anything_is_stored(built: Workspace) -> None:
    """The dialog's preview: would-link, no matching route, with the handler each would reach."""
    group = group_of(worklist(built), SHOP_URL)
    preview = service(built).http_connection_preview(
        HttpConnectionPreviewRequest(
            context=GLOBAL, group=group.key, repository=slug(built, "shop")
        )
    )
    assert preview.url == SHOP_URL and preview.routes_served == 2
    assert preview.counts == {"would-link": 2, "no-matching-route": 2}
    linked = {r.route: r for r in preview.rows if r.outcome == "would-link"}
    assert linked["/v1/items/{item_id}"].handler == "shop/main.py:6"
    assert preview.can_create and preview.requirements == ()
    assert audit_rows(built) == [] and worklist(built).declarations == ()


def test_a_service_that_answers_a_route_twice_would_join_nothing(built: Workspace) -> None:
    """Ambiguity handling is preserved: two handlers, no link."""
    group = group_of(worklist(built), SHOP_URL)
    preview = service(built).http_connection_preview(
        HttpConnectionPreviewRequest(
            context=GLOBAL, group=group.key, repository=slug(built, "twin")
        )
    )
    assert preview.counts.get("would-link", 0) == 0 and preview.counts["ambiguous"] == 1
    assert not preview.can_create
    assert "nothing would be joined" in preview.requirements[0]


def test_a_repository_that_serves_no_route_is_not_an_indexed_service(built: Workspace) -> None:
    """A mapping must identify an indexed deployed service, not merely a repository name."""
    group = group_of(worklist(built), SHOP_URL)
    preview = service(built).http_connection_preview(
        HttpConnectionPreviewRequest(
            context=GLOBAL, group=group.key, repository=slug(built, "tools")
        )
    )
    assert not preview.can_create and preview.routes_served == 0
    assert "not an indexed service" in preview.requirements[0]


def test_a_preview_of_an_unknown_group_or_repository_or_an_unstated_base_is_refused(
    built: Workspace,
) -> None:
    group = group_of(worklist(built), SHOP_URL)
    base = next(g for g in worklist(built).groups if g.kind == "base-not-stated")
    with pytest.raises(InvalidDeploymentLinkError, match="not indexed"):
        service(built).http_connection_preview(
            HttpConnectionPreviewRequest(context=GLOBAL, group=group.key, repository="nowhere")
        )
    with pytest.raises(InvalidDeploymentLinkError, match="do not state their origin"):
        service(built).http_connection_preview(
            HttpConnectionPreviewRequest(
                context=GLOBAL, group=base.key, repository=slug(built, "shop")
            )
        )
    with pytest.raises(InvalidDeploymentLinkError, match="no request has that target"):
        service(built).http_connection_preview(
            HttpConnectionPreviewRequest(
                context=GLOBAL, group="hcg1-" + "0" * 20, repository=slug(built, "shop")
            )
        )


def test_the_workspace_view_needs_global_scope(built: Workspace) -> None:
    from repoatlas.capabilities.errors import ContextResolutionError

    with pytest.raises(ContextResolutionError):
        service(built).http_connections(
            HttpConnectionsRequest(context=repository_context(built, "buyer"))
        )


# -- declaring, applying, revoking ----------------------------------------------------------------


def test_a_declaration_is_pending_until_the_next_sync_then_joins_what_the_target_serves(
    built: Workspace, tmp_path: Path
) -> None:
    """P1 and the approved apply timing: stored now, joined at the next sync, hops declared."""
    before = decisions(built)
    group = group_of(worklist(built), SHOP_URL)
    link_id = declare(built, "shop", SHOP_URL, group=group.key, note="the public API")

    assert state_of(built, link_id) == ("pending", "applies-at-next-sync")
    assert decisions(built) == before  # nothing changes until a sync

    sync(built, tmp_path)
    after = decisions(built)
    assert after["fetch_item"][0] == after["make_item"][0] == "linked"
    # the request the target does not serve stays refused, and says the declaration was tried
    refused, evidence = after["fetch_order"]
    assert refused == "http-service-unmatched"
    assert evidence["declaration_outcome"] == "route-unmatched"
    assert evidence["declaration_ids"] == str(link_id)
    assert state_of(built, link_id) == ("applied", "")

    chain = {h["kind"]: h["status"] for h in _chain(built, "fetch_item")}
    assert chain["service_identity"] == chain["deployment_binding"] == "declared"
    assert chain["source_binding"] == "declared"
    assert chain["route"] == chain["handler"] == "proven"
    assert after["fetch_item"][1]["link_id"] == str(link_id)
    assert after["fetch_item"][1]["identity_source"] == "deployment-link"

    response = worklist(built)
    assert response.summary.linked_by_declaration == 2 and response.summary.unresolved == 6
    resolved = worklist(built, include_resolved=True)
    group_after = group_of(resolved, SHOP_URL)
    assert group_after.requests == 4 and group_after.statuses["linked"] == 2
    assert [d.state for d in group_after.declarations] == ["applied"]


def test_a_declaration_edited_after_a_sync_is_pending_again_and_the_record_holds_no_clock(
    built: Workspace, tmp_path: Path
) -> None:
    """The pass records which revision of each link it read, not when: two syncs of the same
    links leave the same record, and a change after a sync is pending until the next one."""
    link_id = declare(built, "shop", SHOP_URL)
    sync(built, tmp_path)
    assert state_of(built, link_id) == ("applied", "")
    first = built.knowledge.link_pass_reads()
    sync(built, tmp_path)
    assert built.knowledge.link_pass_reads() == first and list(first or {}) == [link_id]

    service(built).deployment_link_update(
        DeploymentLinkUpdateRequest(
            context=repository_context(built, "shop"),
            id=link_id,
            link=DeploymentLinkFields(label="renamed", url=SHOP_URL),
            provenance=DeclarationProvenance(via="api"),
        )
    )
    assert state_of(built, link_id) == ("pending", "applies-at-next-sync")
    sync(built, tmp_path)
    assert state_of(built, link_id) == ("applied", "")


def _chain(workspace: Workspace, caller: str) -> list[dict[str, str]]:
    [(chain,)] = workspace.database.execute(
        """SELECT d.chain_json FROM http_call_decisions d
           JOIN entities e ON e.id = d.src_id WHERE e.qualified_name = ?""",
        (caller,),
    ).fetchall()
    return json.loads(chain)


def test_revoking_a_declaration_restores_every_decision_byte_for_byte(
    built: Workspace, tmp_path: Path
) -> None:
    """P3: reversible; the history keeps what was stated and what was revoked."""
    before = decision_table(built)
    link_id = declare(built, "shop", SHOP_URL)
    sync(built, tmp_path)
    assert decision_table(built) != before
    revoke(built, "shop", link_id, note="wrong service")
    sync(built, tmp_path)
    assert decision_table(built) == before
    assert [(a, b, d) for a, b, _, d, *_ in audit_rows(built)] == [
        ("create", link_id, "ui"),
        ("revoke", link_id, "ui"),
    ]
    entries = (
        service(built)
        .http_declaration_history(HttpDeclarationHistoryRequest(context=GLOBAL, id=link_id))
        .entries
    )
    assert [e.action for e in entries] == ["revoke", "create"]
    assert entries[0].note == "wrong service" and entries[0].after is None
    assert entries[0].before is not None and entries[0].before["url"] == SHOP_URL


def test_a_preview_agrees_with_what_the_sync_then_decides(built: Workspace, tmp_path: Path) -> None:
    """The preview and the join share one matching rule: no request differs."""
    group = group_of(worklist(built), SHOP_URL)
    preview = service(built).http_connection_preview(
        HttpConnectionPreviewRequest(
            context=GLOBAL, group=group.key, repository=slug(built, "orders")
        )
    )
    declare(built, "orders", SHOP_URL, group=group.key)
    sync(built, tmp_path)
    by_ref = {row[0]: row[1] for row in decision_table(built)}
    for row in preview.rows:
        assert (by_ref[row.decision_ref] == "linked") == (row.outcome == "would-link"), row


def test_a_declaration_from_the_worklist_must_serve_a_route_one_of_its_requests_matches(
    built: Workspace,
) -> None:
    """F8 and 'an indexed deployed service': otherwise nothing is stored and nothing audited."""
    group = group_of(worklist(built), SHOP_URL)
    for name in ("tools", "twin"):
        with pytest.raises(InvalidDeploymentLinkError):
            declare(built, name, SHOP_URL, group=group.key)
    assert audit_rows(built) == []
    assert built.deployment_links.list_all() == []
    local = group_of(worklist(built), "http://localhost:8000")
    with pytest.raises(InvalidDeploymentLinkError, match="does not name the origin"):
        declare(built, "shop", SHOP_URL, group=local.key)
    assert audit_rows(built) == []


def test_stating_what_is_already_stated_is_one_declaration_and_one_history_row(
    built: Workspace,
) -> None:
    """De-duplication: asking twice stores and audits once."""
    first = declare(built, "shop", SHOP_URL)
    second = declare(built, "shop", SHOP_URL)
    assert first == second and len(built.deployment_links.list_all()) == 1
    assert len(audit_rows(built)) == 1


def test_two_services_at_one_place_for_the_same_callers_are_refused(built: Workspace) -> None:
    """F3 at creation: the join would refuse the ambiguity; the store refuses to create it."""
    first = declare(built, "shop", SHOP_URL)
    with pytest.raises(InvalidDeploymentLinkError, match=f"link {first} already states"):
        declare(built, "orders", SHOP_URL)
    assert len(built.deployment_links.list_all()) == 1 and len(audit_rows(built)) == 1


def test_a_declaration_never_overrides_what_the_source_establishes(
    built: Workspace, tmp_path: Path
) -> None:
    """F1/F2/F14: svc's published origin is the source's; a service declared there changes nothing,
    even one that serves the route svc does not: the refusal says the source decided."""
    before = decisions(built)
    link_id = declare(built, "orders", "http://svc:8080")
    sync(built, tmp_path)
    after = decisions(built)
    assert after["fetch_published"] == before["fetch_published"]  # still the source's link
    assert after["fetch_published"][1].get("link_id") is None
    refused, evidence = after["fetch_unserved"]
    assert refused == "http-route-unmatched"
    assert evidence["declaration_outcome"] == "source-established"
    assert state_of(built, link_id) == ("ineffective", "source-established")
    assert all(h["status"] != "declared" for h in _chain(built, "fetch_published"))


def test_a_declaration_is_never_proof_in_any_hop(built: Workspace, tmp_path: Path) -> None:
    """F2: even confirmed by the operator, identity, binding and source stay declared."""
    service(built).deployment_link_create(
        DeploymentLinkCreateRequest(
            context=repository_context(built, "shop"),
            link=DeploymentLinkFields(label="mine", url=SHOP_URL, verification="user_confirmed"),
        )
    )
    sync(built, tmp_path)
    statuses = {h["kind"]: h["status"] for h in _chain(built, "fetch_item")}
    assert {statuses[k] for k in ("service_identity", "deployment_binding", "source_binding")} == {
        "declared"
    }
    assert "proven" not in {statuses[k] for k in ("service_identity", "deployment_binding")}


def test_an_ambiguous_declaration_is_ineffective_and_says_why(
    built: Workspace, tmp_path: Path
) -> None:
    """F4 and the ineffective state: a service answering twice joins nothing."""
    link_id = declare(built, "twin", SHOP_URL)
    sync(built, tmp_path)
    assert decisions(built)["fetch_item"][0] == "http-service-unmatched"
    assert decisions(built)["fetch_item"][1]["declaration_outcome"] == "ambiguous"
    assert state_of(built, link_id) == ("ineffective", "ambiguous")


def test_a_declaration_whose_origin_is_no_longer_requested_is_invalidated(
    built: Workspace, tmp_path: Path
) -> None:
    """Stale declarations: nothing at the origin any more, so shown invalidated, not applied."""
    link_id = declare(built, "shop", SHOP_URL)
    sync(built, tmp_path)
    assert state_of(built, link_id)[0] == "applied"
    (tmp_path / "repos" / "buyer" / "buyer" / "client.py").write_text("import requests\n")
    (tmp_path / "repos" / "batch" / "batch" / "client.py").write_text("import requests\n")
    sync(built, tmp_path)
    assert state_of(built, link_id) == ("invalidated", "origin-no-longer-requested")


def test_a_declaration_for_a_service_that_stopped_serving_routes_is_invalidated(
    built: Workspace, tmp_path: Path
) -> None:
    link_id = declare(built, "shop", SHOP_URL)
    sync(built, tmp_path)
    (tmp_path / "repos" / "shop" / "shop" / "main.py").write_text("# no routes\n")
    sync(built, tmp_path)
    assert state_of(built, link_id) == ("invalidated", "repository-serves-no-routes")
    assert decisions(built)["fetch_item"][0] == "http-service-unmatched"


# -- loopback --------------------------------------------------------------------------------------


def test_a_loopback_origin_cannot_be_declared_for_every_caller(built: Workspace) -> None:
    """An origin alone does not establish identity across runtime environments."""
    with pytest.raises(ValueError, match="name the caller repositories"):
        DeploymentLinkFields(label="dev", url="http://localhost:8000", environment="dev")
    with pytest.raises(ValueError, match="name it"):
        DeploymentLinkFields(label="dev", url="http://localhost:8000", callers=["buyer"])
    group = group_of(worklist(built), "http://localhost:8000")
    preview = service(built).http_connection_preview(
        HttpConnectionPreviewRequest(
            context=GLOBAL, group=group.key, repository=slug(built, "shop")
        )
    )
    assert not preview.can_create
    assert any("caller repositories" in r for r in preview.requirements)
    assert group.loopback is True


def test_a_loopback_mapping_applies_only_to_the_callers_it_names(
    built: Workspace, tmp_path: Path
) -> None:
    """One origin, two repositories, two different services: the declaration serves one of them."""
    group = group_of(worklist(built), "http://localhost:8000")
    buyer = slug(built, "buyer")
    preview = service(built).http_connection_preview(
        HttpConnectionPreviewRequest(
            context=GLOBAL,
            group=group.key,
            repository=slug(built, "shop"),
            callers=(buyer,),
            environment="dev",
        )
    )
    outcomes = {r.site.repository: r.outcome for r in preview.rows}
    assert outcomes == {buyer: "would-link", slug(built, "tools"): "caller-excluded"}
    assert preview.can_create and any("loopback" in w.lower() for w in preview.warnings)

    link_id = declare(
        built, "shop", "http://localhost:8000", callers=(buyer,), environment="dev", group=group.key
    )
    sync(built, tmp_path)
    after = decisions(built)
    assert after["fetch_local"][0] == "linked"
    assert after["fetch_local_order"][0] == "http-service-unmatched"
    assert after["fetch_local_order"][1]["declaration_outcome"] == "callers-excluded"
    assert state_of(built, link_id) == ("applied", "")


def test_two_loopback_mappings_for_different_callers_coexist(
    built: Workspace, tmp_path: Path
) -> None:
    """Disjoint callers are not a conflict: each repository's localhost is its own."""
    buyer, tools = slug(built, "buyer"), slug(built, "tools")
    first = declare(built, "shop", "http://localhost:8000", callers=(buyer,), environment="dev")
    second = declare(built, "orders", "http://localhost:8000", callers=(tools,), environment="dev")
    sync(built, tmp_path)
    after = decisions(built)
    assert after["fetch_local"][0] == after["fetch_local_order"][0] == "linked"
    assert {state_of(built, first)[0], state_of(built, second)[0]} == {"applied"}
    with pytest.raises(InvalidDeploymentLinkError, match="already states"):
        declare(built, "twin", "http://localhost:8000", callers=(buyer, tools), environment="dev")


# -- history and privacy ---------------------------------------------------------------------------


def test_history_records_create_update_and_revoke_and_is_append_only(built: Workspace) -> None:
    """F10: nothing can rewrite it; a sync adds nothing to it."""
    link_id = declare(built, "shop", SHOP_URL, note="first")
    service(built).deployment_link_update(
        DeploymentLinkUpdateRequest(
            context=repository_context(built, "shop"),
            id=link_id,
            link=DeploymentLinkFields(label="renamed", url=SHOP_URL),
            provenance=DeclarationProvenance(via="api", note="rename"),
        )
    )
    revoke(built, "shop", link_id)
    assert [row[0] for row in audit_rows(built)] == ["create", "update", "revoke"]
    import sqlite3

    for statement in (
        "UPDATE declaration_audit SET note = 'x'",
        "DELETE FROM declaration_audit",
    ):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            built.database.execute(statement)
    assert len(audit_rows(built)) == 3


def test_a_sync_appends_nothing_to_the_history(built: Workspace, tmp_path: Path) -> None:
    """Audit events are separate from join outcomes."""
    declare(built, "shop", SHOP_URL)
    count = len(audit_rows(built))
    for _ in range(2):
        sync(built, tmp_path)
    assert len(audit_rows(built)) == count == 1


def test_a_note_or_url_carrying_a_credential_or_query_is_refused_and_nothing_is_stored(
    built: Workspace,
) -> None:
    """F5 on every input: the UI, the API and the history refuse them."""
    for url in (
        "https://user:pw@shop.example.test",
        "https://shop.example.test?token=abc",
        "https://shop.example.test/{id}",
        "https://shop.example.test#frag",
    ):
        with pytest.raises(ValueError):
            DeploymentLinkFields(label="x", url=url)
    with pytest.raises(ValueError):
        DeclarationProvenance(note="use https://u:p@h/x")
    assert audit_rows(built) == [] and built.deployment_links.list_all() == []


def test_the_history_holds_no_secret_and_does_not_travel_with_the_export(built: Workspace) -> None:
    """Existing export/import carries declarations, not activity."""
    from repoatlas.models.deployment_targets import LinksExport

    declare(built, "shop", SHOP_URL, note="because")
    exported = LinksExport(
        links=[
            {  # type: ignore[list-item]
                "repository": link.repository,
                **link.model_dump(include=set(DeploymentLinkFields.model_fields)),
            }
            for link in built.deployment_links.list_all()
        ]
    ).model_dump_json()
    assert "because" not in exported and "declaration_audit" not in exported
    assert '"callers"' in exported


def test_an_import_is_recorded_as_an_import_and_importing_twice_adds_nothing(
    built: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """'Record imported actions': the CLI's import writes action ``import``; repeats are skipped."""
    from repoatlas.cli.main import app
    from repoatlas.models.deployment_targets import ExportedLink, LinksExport
    from typer.testing import CliRunner

    monkeypatch.setenv("REPOATLAS_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("REPOATLAS_DB", str(built.settings.storage.database))
    exported = tmp_path / "links.json"
    exported.write_text(
        LinksExport(
            links=[
                ExportedLink(
                    repository=slug(built, "shop"), label="from file", url=SHOP_URL, callers=[]
                )
            ]
        ).model_dump_json()
    )
    runner = CliRunner()
    for _ in range(2):
        result = runner.invoke(app, ["links", "import", str(exported)])
        assert result.exit_code == 0, result.output
    assert [(a, v) for a, _, _, v, *_ in audit_rows(built)] == [("import", "import")]


def test_the_cli_export_carries_callers_so_a_loopback_declaration_imports_again(
    built: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A loopback link is valid only with its callers; an export without them cannot be imported."""
    from repoatlas.cli.main import app
    from repoatlas.models.deployment_targets import LinksExport
    from typer.testing import CliRunner

    monkeypatch.setenv("REPOATLAS_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("REPOATLAS_DB", str(built.settings.storage.database))
    declare(
        built,
        "shop",
        "http://localhost:8000",
        callers=(slug(built, "buyer"),),
        environment="dev",
    )
    target = tmp_path / "out.json"
    result = CliRunner().invoke(app, ["links", "export", str(target)])
    assert result.exit_code == 0, result.output
    links = LinksExport.model_validate_json(target.read_text()).links
    assert [link.callers for link in links] == [[slug(built, "buyer")]]


def test_the_history_endpoint_reads_newest_first_and_filters_by_repository(
    built: Workspace,
) -> None:
    first = declare(built, "shop", SHOP_URL)
    second = declare(built, "orders", "https://orders.example.test")
    everything = service(built).http_declaration_history(
        HttpDeclarationHistoryRequest(context=GLOBAL)
    )
    assert [e.subject_id for e in everything.entries] == [second, first]
    only = service(built).http_declaration_history(
        HttpDeclarationHistoryRequest(context=GLOBAL, repository=slug(built, "shop"))
    )
    assert [e.subject_id for e in only.entries] == [first]


# -- impact ----------------------------------------------------------------------------------------


def test_impact_labels_an_operator_declared_crossing_and_leaves_the_rest_as_it_was(
    built: Workspace, tmp_path: Path
) -> None:
    """Impact: operator-declared crossings are stated as such; source-proven ones are not."""
    declare(built, "shop", SHOP_URL)
    sync(built, tmp_path)
    handler = built.database.execute(
        "SELECT key FROM entities WHERE qualified_name = 'get_item' AND key LIKE 'sym:shop:%'"
    ).fetchone()[0]
    answer = service(built).impact(ImpactRequest(context=GLOBAL, seed=handler))
    caller = next(c for c in answer.candidates if c.name == "fetch_item")
    assert caller.step.http is not None and caller.step.http.operator_declared is True
    assert caller.step.http.declaration == "link 1: mapped, unconfirmed"
    assert caller.step.http.identity_source == "deployment-link"
    assert "operator-declared" in answer.model_dump_json()

    svc_handler = built.database.execute(
        "SELECT key FROM entities WHERE qualified_name = 'get_item' AND key LIKE 'sym:svc:%'"
    ).fetchone()[0]
    sourced = service(built).impact(ImpactRequest(context=GLOBAL, seed=svc_handler))
    crossing = next(c for c in sourced.candidates if c.name == "fetch_published").step.http
    assert (
        crossing is not None
        and crossing.operator_declared is False
        and crossing.declaration is None
    )


def test_an_incremental_sync_with_declarations_equals_a_fresh_index_with_the_same_declarations(
    built: Workspace, tmp_path: Path
) -> None:
    """Incremental/fresh: the declarations are state outside the index, so a fresh index that
    holds the same ones decides exactly the same, after a change to a caller."""
    declare(built, "shop", SHOP_URL)
    sync(built, tmp_path)
    client = tmp_path / "repos" / "buyer" / "buyer" / "client.py"
    client.write_text(client.read_text() + "\n\ndef extra() -> dict:\n    return {}\n")
    sync(built, tmp_path)

    fresh = open_workspace(
        settings=Settings.model_validate(
            {"storage": {"database": str(tmp_path / "fresh/atlas.db")}}
        ),
        require_root=False,
    )
    try:
        from repoatlas.services.init_service import initialize

        initialize(fresh, tmp_path / "repos", config_path=tmp_path / "fresh-config.yaml")
        for link in built.deployment_links.list_all():
            fresh.deployment_links.create(
                link.repository,
                DeploymentLinkFields.model_validate(
                    link.model_dump(include=set(DeploymentLinkFields.model_fields))
                ),
                via="import",
            )
        sync(fresh, tmp_path)
        assert decision_table(built) == decision_table(fresh)
    finally:
        fresh.close()


# -- agents: read-only discovery through the existing HTTP capability -----------------------------


def test_unresolved_requests_are_discoverable_through_http_operations_without_a_route_prefix(
    built: Workspace,
) -> None:
    """MCP: no new tool and no write; the existing capability lists every repository's refused
    requests when asked for decisions, each with its method, place and the join's reason."""
    from repoatlas.models.http_inventory import HttpOperationsRequest

    answer = service(built).http_operations(
        HttpOperationsRequest(
            context=GLOBAL,
            discover=True,
            role="caller",  # type: ignore[arg-type]
            crossing=("http-service-unmatched",),  # type: ignore[arg-type]
        )
    )
    crossings = [c for row in answer.operations for c in row.crossings]
    assert {c.status.value for c in crossings} == {"http-service-unmatched"}
    assert len(crossings) == 6
    assert {row.repository for row in answer.operations} == {
        slug(built, "buyer"),
        slug(built, "tools"),
        slug(built, "batch"),
    }
    assert audit_rows(built) == [] and built.deployment_links.list_all() == []
    with pytest.raises(ValueError, match="route_prefix or a crossing filter"):
        HttpOperationsRequest(context=GLOBAL, discover=True)


def test_no_mcp_tool_can_declare_or_revoke(built: Workspace) -> None:
    """F9: the write path is the API's and the CLI's; the agent surface has none."""
    from repoatlas.mcp.tools import TOOLS

    names = {tool.name for tool in TOOLS}
    assert not {n for n in names if "link" in n or "declar" in n or "connection" in n}
