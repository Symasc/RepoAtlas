"""External-service classification (slice S2): the capability, its endpoints, its read-time effect.

A classification says an origin no indexed repository answers is a service outside the index. It is
stored apart from the deployment links and applied when read: a request the join left
``http-service-unmatched`` there is shown as a known external dependency, and its decision, chain,
edges and every impact answer stay the join's. Three corpora, all invented:
``tests/service_declarations_corpus.py`` (a portal, a worker that asks an external provider for a
token and then calls the portal, a web app that opens the portal's login page and the provider's
authorize page), ``tests/http_connections_corpus.py`` (origins the source links, refuses or leaves
unmatched) and ``tests/artifact_corpus.py`` (container images between deployment and source
repositories). Each test names the case it pins: A1 and A4 of the plan's acceptance examples and
N1-N4 and N16 of its negative rules.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest
from pydantic import ValidationError
from repoatlas.api.server import ENDPOINTS, Serving
from repoatlas.capabilities.errors import DeploymentLinkNotFoundError, InvalidDeploymentLinkError
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import Settings
from repoatlas.errors import ContextResolutionError
from repoatlas.graph.deploy_artifacts import CONTAINER_ARTIFACT
from repoatlas.graph.http_declarations import facts_of
from repoatlas.graph.service_declarations import classifiable_origins, classification_check
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.deployment_targets import (
    DeclarationProvenance,
    DeploymentLinkCreateRequest,
    DeploymentLinkFields,
    DeploymentLinkUpdateRequest,
    DeploymentTargetsRequest,
)
from repoatlas.models.http_connections import (
    ConnectionGroup,
    ConnectionRequest,
    HttpConnectionPreviewRequest,
    HttpConnectionsRequest,
    HttpConnectionsResponse,
)
from repoatlas.models.impact import ImpactRequest
from repoatlas.models.service_declaration_io import (
    ServiceDeclarationCreateRequest,
    ServiceDeclarationDeleteRequest,
    ServiceDeclarationHistoryRequest,
    ServiceDeclarationPreviewRequest,
    ServiceDeclarationPreviewResponse,
    ServiceDeclarationResponse,
    ServiceDeclarationUpdateRequest,
)
from repoatlas.models.service_declarations import (
    AuthenticationTarget,
    ExternalServiceFields,
    PermittedCallbackFields,
    RedirectFields,
    RedirectSource,
    ServiceDeclarationView,
)
from repoatlas.services.workspace import Workspace, open_workspace
from tests.artifact_corpus import repos as artifact_repos
from tests.http_connections_corpus import repos as http_repos
from tests.impact_corpus import build
from tests.incremental_corpus import sync_all
from tests.service_declarations_corpus import repos
from tests.transport.test_local_api import post, running

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)
AUTH = "https://auth.example.test"
LOGIN = "https://login.example.test"
PORTAL = "http://portal:8080"
SHOP = "https://shop.example.test"
SVC = "http://svc:8080"
UNMATCHED = "http-service-unmatched"
TOKEN_REQUEST = ("/oauth/token", "POST")

#: One more caller for the container-image corpus: a request to an origin nothing identifies.
EXTERNAL_CALLER = dedent("""\
    import requests


    def fetch_access() -> dict:
        return requests.post("https://auth.example.test/oauth/token", timeout=5).json()
""")


@pytest.fixture
def built(workspace: Workspace, tmp_path: Path) -> Workspace:
    return build(workspace, tmp_path, repos())


@pytest.fixture
def built_http(workspace: Workspace, tmp_path: Path) -> Workspace:
    return build(workspace, tmp_path, http_repos())


@pytest.fixture
def built_image(workspace: Workspace, tmp_path: Path) -> Workspace:
    corpus = artifact_repos()
    corpus["extcaller"] = {"extcaller/__init__.py": "", "extcaller/auth.py": EXTERNAL_CALLER}
    return build(workspace, tmp_path, corpus)


def service(workspace: Workspace) -> CapabilityService:
    return CapabilityService(workspace)


def slug(workspace: Workspace, name: str) -> str:
    return next(r.slug for r in workspace.repositories.list_all() if r.name == name)


def repository_context(workspace: Workspace, name: str) -> CapabilityContext:
    return CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=slug(workspace, name))


def sync(workspace: Workspace, tmp_path: Path) -> None:
    sync_all(workspace, tmp_path / "repos")


def worklist(workspace: Workspace, **filters: Any) -> HttpConnectionsResponse:
    return service(workspace).http_connections(HttpConnectionsRequest(context=GLOBAL, **filters))


def group_of(response: HttpConnectionsResponse, origin: str) -> ConnectionGroup:
    return next(g for g in response.groups if g.origin == origin)


def requests_at(
    workspace: Workspace, origin: str
) -> dict[tuple[str, str | None], ConnectionRequest]:
    """Every request at an origin, linked ones too, by route and method."""
    key = group_of(worklist(workspace, include_resolved=True), origin).key
    page = worklist(workspace, include_resolved=True, group=key)
    return {(r.route, r.method): r for r in page.requests}


def stated(view: ServiceDeclarationView) -> ExternalServiceFields:
    assert isinstance(view.fields, ExternalServiceFields)
    return view.fields


def external(
    origin: str = AUTH, label: str = "Example login", **more: Any
) -> ExternalServiceFields:
    return ExternalServiceFields(origin=origin, label=label, **more)


def preview(
    workspace: Workspace,
    fields: ExternalServiceFields,
    group: str | None = None,
    **more: Any,
) -> ServiceDeclarationPreviewResponse:
    return service(workspace).service_declaration_preview(
        ServiceDeclarationPreviewRequest(context=GLOBAL, fields=fields, group=group, **more)
    )


def classify(
    workspace: Workspace,
    fields: ExternalServiceFields,
    *,
    group: str | None = None,
    note: str | None = None,
) -> ServiceDeclarationResponse:
    return service(workspace).service_declaration_create(
        ServiceDeclarationCreateRequest(
            context=GLOBAL,
            fields=fields,
            provenance=DeclarationProvenance(via="ui", note=note, request_group=group),
        )
    )


def declare(workspace: Workspace, name: str, url: str, *, group: str | None = None) -> int:
    """A deployment link, as the Map action makes one."""
    return (
        service(workspace)
        .deployment_link_create(
            DeploymentLinkCreateRequest(
                context=repository_context(workspace, name),
                link=DeploymentLinkFields(label="mapped", url=url),
                provenance=DeclarationProvenance(via="ui", request_group=group),
            )
        )
        .link.id
    )


def decision_table(workspace: Workspace) -> list[tuple[object, ...]]:
    """Every request decision, without row ids: a chain names its handler's entity row."""

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


def edge_count(workspace: Workspace) -> int:
    return int(workspace.database.execute("SELECT COUNT(*) FROM edges").fetchone()[0])


def audit_rows(workspace: Workspace) -> list[tuple[object, ...]]:
    return [
        tuple(row)
        for row in workspace.database.execute(
            "SELECT action, kind, subject_id, via, request_group FROM service_declaration_audit "
            "ORDER BY id"
        ).fetchall()
    ]


def impact_of(workspace: Workspace, handler: str) -> tuple[Any, ...]:
    answer = service(workspace).impact(ImpactRequest(context=GLOBAL, seed=handler))
    return (answer.candidates, answer.decisions, answer.unresolved, answer.summary)


# -- A1: a token request, then an API request ------------------------------------------------------


def test_a1_a_token_request_then_an_api_request_are_two_dependencies_and_no_redirect(
    built: Workspace, tmp_path: Path
) -> None:
    """A1: classifying the provider's origin, with the portal it authenticates for, changes how the
    token request is shown and nothing else: its decision stays unmatched, its chain and every
    other decision, the edges and the impact of the portal's handler are byte-identical, and the
    portal request is the join's own link."""
    portal = slug(built, "portal")
    handler = built.database.execute(
        "SELECT key FROM entities WHERE qualified_name = 'profile' AND key LIKE ?",
        (f"sym:{portal}:%",),
    ).fetchone()[0]
    table, edges, impact = decision_table(built), edge_count(built), impact_of(built, handler)
    assert any(c.name == "refresh_profile" for c in impact[0])

    token_before = requests_at(built, AUTH)[TOKEN_REQUEST]
    api_before = requests_at(built, PORTAL)
    assert token_before.status.value == UNMATCHED
    assert (token_before.identity, token_before.external_service_id) == ("unresolved", None)
    assert {r.status.value for r in api_before.values()} == {"linked"}
    assert {r.identity for r in api_before.values()} == {"identified"}
    assert worklist(built).summary.known_external == 0

    group = group_of(worklist(built), AUTH).key
    held = classify(
        built,
        external(
            provider="Example IdP",
            category="identity",
            authenticates=[AuthenticationTarget(repository=portal)],
        ),
        group=group,
    ).declaration
    assert (held.state, held.reason, held.requests) == ("applied", "", 1)
    assert [t.repository for t in stated(held).authenticates] == [portal]

    token_after = requests_at(built, AUTH)[TOKEN_REQUEST]
    assert (token_after.identity, token_after.external_service_id) == ("known-external", held.id)
    # only the identity moved: the decision, its reason, any declaration and the join are as were
    assert token_after.status.value == UNMATCHED and token_after.joined_by is None
    assert token_after.declaration_ids == () and token_after.reason == token_before.reason
    plain = {"identity": "unresolved", "external_service_id": None}
    assert token_after.model_copy(update=plain) == token_before
    # the API request is not touched, and no redirect is invented between the two requests
    assert requests_at(built, PORTAL) == api_before
    summary = worklist(built).summary
    assert (summary.known_external, summary.unresolved, summary.unresolved_identity) == (1, 2, 1)
    # nothing was joined, mapped or deployed
    assert decision_table(built) == table and edge_count(built) == edges
    assert impact_of(built, handler) == impact
    assert built.deployment_links.list_all() == []
    # the classification is read, never joined: a sync reads and decides nothing from it
    sync(built, tmp_path)
    assert decision_table(built) == table and edge_count(built) == edges
    assert impact_of(built, handler) == impact
    assert requests_at(built, AUTH)[TOKEN_REQUEST] == token_after
    # (S3 adds `redirects == ()` to the request and to this assertion: a request that is followed
    # by another one is not a redirect.)


def test_the_identity_filter_and_the_summary_tell_a_known_external_from_an_unresolved(
    built: Workspace,
) -> None:
    """Known external dependency and unresolved service identity are two different answers."""
    classify(built, external(), group=group_of(worklist(built), AUTH).key)
    known = worklist(built, identity="known-external")
    assert [g.origin for g in known.groups] == [AUTH]
    assert [g.origin for g in worklist(built, identity="unresolved").groups] == [LOGIN]
    identified = worklist(built, identity="identified", include_resolved=True)
    assert [g.origin for g in identified.groups] == [PORTAL]
    # the group says which classification it carries, in any state; the unclassified has none
    carried = group_of(worklist(built), AUTH).external_service
    assert carried is not None and carried.state == "applied"
    assert group_of(worklist(built), LOGIN).external_service is None
    assert [stated(d).origin for d in worklist(built).service_declarations] == [AUTH]


def test_the_preview_states_each_requests_identity_before_and_after_and_stores_nothing(
    built: Workspace,
) -> None:
    group = group_of(worklist(built), AUTH).key
    checked = preview(built, external(), group)
    assert checked.can_create and checked.requirements == ()
    assert (checked.kind, checked.subject, checked.group) == ("external_service", AUTH, group)
    assert checked.counts.model_dump() == {
        "would_be_known_external": 1,
        "stay_identified": 0,
        "redirect_steps_added": 0,
        "requests_reached": 1,
    }
    [row] = checked.rows
    assert (row.route, row.method) == TOKEN_REQUEST
    assert (row.identity_before, row.identity_after, row.steps_added) == (
        "unresolved",
        "known-external",
        0,
    )
    assert built.service_declarations.list_all() == [] and audit_rows(built) == []


def test_a_classification_is_edited_revoked_and_recorded_and_stating_it_twice_is_one(
    built: Workspace,
) -> None:
    group = group_of(worklist(built), AUTH).key
    first = classify(built, external(), group=group, note="the provider").declaration
    again = classify(built, external(), group=group).declaration
    assert again.id == first.id and len(built.service_declarations.list_all()) == 1
    assert preview(built, external(), group).duplicate_of == first.id
    # a request is known external the moment the classification is stored, with no sync
    assert requests_at(built, AUTH)[TOKEN_REQUEST].identity == "known-external"

    renamed = service(built).service_declaration_update(
        ServiceDeclarationUpdateRequest(
            context=GLOBAL,
            id=first.id,
            fields=external(label="Renamed provider", provider="Example IdP"),
            provenance=DeclarationProvenance(via="api", note="rename"),
        )
    )
    assert stated(renamed.declaration).label == "Renamed provider"
    assert renamed.declaration.state == "applied"
    # what it is about cannot change: another origin is another declaration
    with pytest.raises(InvalidDeploymentLinkError, match="cannot change"):
        service(built).service_declaration_update(
            ServiceDeclarationUpdateRequest(
                context=GLOBAL, id=first.id, fields=external(origin=LOGIN)
            )
        )
    # in other words, the same origin cannot be classified a second time
    with pytest.raises(InvalidDeploymentLinkError, match="already classified"):
        classify(built, external(label="Another label"), group=group)
    conflict = preview(built, external(label="Another label"), group)
    assert conflict.conflicts_with == first.id and not conflict.can_create

    gone = service(built).service_declaration_delete(
        ServiceDeclarationDeleteRequest(
            context=GLOBAL, id=first.id, provenance=DeclarationProvenance(via="ui", note="wrong")
        )
    )
    assert gone.deleted is True
    assert requests_at(built, AUTH)[TOKEN_REQUEST].identity == "unresolved"
    with pytest.raises(DeploymentLinkNotFoundError, match="no service declaration has that id"):
        service(built).service_declaration_delete(
            ServiceDeclarationDeleteRequest(context=GLOBAL, id=first.id)
        )
    assert [row[0] for row in audit_rows(built)] == ["create", "update", "revoke"]
    history = service(built).service_declaration_history(
        ServiceDeclarationHistoryRequest(context=GLOBAL, id=first.id)
    )
    assert [(e.action, e.via, e.subject) for e in history.entries] == [
        ("revoke", "ui", AUTH),
        ("update", "api", AUTH),
        ("create", "ui", AUTH),
    ]
    assert history.entries[0].note == "wrong" and history.entries[0].after is None


def test_a_classification_made_from_a_group_must_name_that_groups_origin(built: Workspace) -> None:
    """The group is the worklist's own: another origin, or one with nothing unmatched, is refused
    and nothing is stored."""
    login = group_of(worklist(built), LOGIN).key
    portal = group_of(worklist(built, include_resolved=True), PORTAL).key
    wrong = preview(built, external(AUTH), login)
    assert not wrong.can_create and "origin of the group" in wrong.requirements[0]
    nothing = preview(built, external(PORTAL), portal)
    assert not nothing.can_create and any("nothing to classify" in r for r in nothing.requirements)
    with pytest.raises(InvalidDeploymentLinkError, match="origin of the group"):
        classify(built, external(AUTH), group=login)
    with pytest.raises(InvalidDeploymentLinkError, match="no request has that target"):
        classify(built, external(AUTH), group="hcg1-" + "0" * 20)
    assert built.service_declarations.list_all() == [] and audit_rows(built) == []


# -- A4: the container image relationship is independent ------------------------------------------


def image_state(workspace: Workspace) -> dict[str, Any]:
    """Where each repository is deployed and what runs it, the container-image decisions, the
    request decisions and the links."""
    answers = {
        name: service(workspace).deployment_targets(
            DeploymentTargetsRequest(context=repository_context(workspace, name))
        )
        for name in ("deployer", "api", "report", "client")
    }
    return {
        "targets": {
            name: (answer.targets, answer.links, answer.diagnostics)
            for name, answer in answers.items()
        },
        "images": [
            (row.decision_ref, row.subject, row.decision, row.evidence_json)
            for row in workspace.knowledge.list_link_decisions(family=CONTAINER_ARTIFACT)
        ],
        "requests": decision_table(workspace),
        "links": workspace.deployment_links.list_all(),
    }


def test_a4_a_deployment_that_runs_another_repositorys_image_is_independent_of_declarations(
    built_image: Workspace, tmp_path: Path
) -> None:
    """A4: the deployment repository that consumes an image built elsewhere keeps its targets, its
    `runs` and its container-image decisions whatever is declared about external services,
    redirects and callbacks, and after a sync."""
    before = image_state(built_image)
    assert len(before["images"]) == 6
    assert any(row[2] == "linked" for row in before["images"])

    group = group_of(worklist(built_image), AUTH).key
    classify(
        built_image,
        external(authenticates=[AuthenticationTarget(repository=slug(built_image, "api"))]),
        group=group,
    )
    # redirects and permitted callbacks have no endpoint yet: stored as an import stores them
    built_image.service_declarations.create(
        RedirectFields(
            source=RedirectSource(form="origin", origin=LOGIN, path="/authorize"),
            destination=f"{PORTAL}/auth/callback",
        ),
        via="api",
    )
    built_image.service_declarations.create(
        PermittedCallbackFields(provider_origin=AUTH, destination=f"{PORTAL}/auth/callback"),
        via="api",
    )
    assert [d.kind for d in built_image.service_declarations.list_all()] == [
        "external_service",
        "redirect",
        "permitted_callback",
    ]
    assert image_state(built_image) == before
    sync(built_image, tmp_path)
    assert image_state(built_image) == before
    assert built_image.deployment_links.list_all() == []


# -- N1: the source already says something about the origin ----------------------------------------


def test_n1_an_origin_the_source_links_or_refuses_cannot_be_classified(
    built_http: Workspace,
) -> None:
    """N1: a published service (one request linked, one refused as an unserved route) is the
    source's to identify. Both decisions are listed in the requirements, nothing is stored, and an
    origin the join left unmatched is the control that can be."""
    group = group_of(worklist(built_http), SVC).key
    checked = preview(built_http, external(SVC), group)
    assert not checked.can_create
    text = " | ".join(checked.requirements)
    assert "are linked" in text and "are http-route-unmatched" in text
    assert checked.counts.would_be_known_external == 0
    assert {r.identity_after for r in checked.rows} == {r.identity_before for r in checked.rows}
    with pytest.raises(InvalidDeploymentLinkError, match="nothing to classify"):
        classify(built_http, external(SVC), group=group)
    assert built_http.service_declarations.list_all() == [] and audit_rows(built_http) == []

    control = preview(built_http, external(SHOP), group_of(worklist(built_http), SHOP).key)
    assert control.can_create and control.counts.would_be_known_external == 4


def test_n1_an_origin_with_an_unresolved_source_or_an_ambiguous_provider_is_refused_too(
    built_image: Workspace,
) -> None:
    """N1 on the container-image corpus: linked, source-unresolved and provider-ambiguous."""
    for origin, status in (
        ("https://api.test.example", "linked"),
        ("https://other.test.example", "http-deployment-source-unresolved"),
        ("https://alpha.svc.example", "http-provider-ambiguous"),
    ):
        checked = preview(built_image, external(origin))
        assert not checked.can_create, origin
        assert any(f"are {status}" in r for r in checked.requirements), (origin, status)
        with pytest.raises(InvalidDeploymentLinkError, match=f"are {status}"):
            classify(built_image, external(origin))
    assert built_image.service_declarations.list_all() == [] and audit_rows(built_image) == []


# -- N2: loopback ---------------------------------------------------------------------------------


def test_n2_a_loopback_origin_is_never_an_external_service(built_http: Workspace) -> None:
    """N2: localhost is a different program in each repository: refused by the model, by the
    wire, and, were one ever stored, by the check; and never offered."""
    for origin in ("http://localhost:8000", "http://localhost:3000"):
        with pytest.raises(ValidationError, match="names this machine"):
            external(origin)
    with pytest.raises(ValidationError):
        ServiceDeclarationCreateRequest.model_validate(
            {
                "context": {"scope": "global"},
                "fields": {
                    "kind": "external_service",
                    "origin": "http://localhost:8000",
                    "label": "x",
                },
            }
        )
    facts = [facts_of(row, "") for row in built_http.knowledge.list_http_call_decisions()]
    assert any(f.target.origin == "http://localhost:8000" and f.status == UNMATCHED for f in facts)
    # an unmatched request at localhost exists, yet only the shop is offered
    assert classifiable_origins(facts, {}) == {SHOP}
    forced = ExternalServiceFields.model_construct(origin="http://localhost:8000", label="x")
    refusal = classification_check(forced, facts, [], {})
    assert any("names this machine" in r for r in refusal.requirements)


# -- N3: an origin a deployment link names ---------------------------------------------------------


def test_n3_an_origin_a_deployment_link_names_cannot_be_classified(built_http: Workspace) -> None:
    link_id = declare(built_http, "shop", SHOP)
    group = group_of(worklist(built_http), SHOP).key
    checked = preview(built_http, external(SHOP), group)
    assert not checked.can_create
    assert any(f"deployment link #{link_id}" in r for r in checked.requirements)
    with pytest.raises(InvalidDeploymentLinkError, match=f"deployment link #{link_id}"):
        classify(built_http, external(SHOP), group=group)
    assert built_http.service_declarations.list_all() == [] and audit_rows(built_http) == []


def test_n3_a_classified_origin_cannot_be_mapped_or_declared_as_a_deployment(
    built_http: Workspace,
) -> None:
    """N3: once an origin is classified the Map preview and deployment_link_create/update refuse
    it, naming the classification; revoke it and they behave as they always did."""
    group = group_of(worklist(built_http), SHOP).key

    def map_preview() -> Any:
        return service(built_http).http_connection_preview(
            HttpConnectionPreviewRequest(
                context=GLOBAL, group=group, repository=slug(built_http, "shop")
            )
        )

    baseline = map_preview()
    assert baseline.can_create
    held = classify(built_http, external(SHOP, "Shop API"), group=group).declaration.id
    text = re.escape(f"classified as external service #{held}; revoke it first")

    mapped = map_preview()
    assert not mapped.can_create and any(re.search(text, r) for r in mapped.requirements)
    for request_group in (None, group):
        with pytest.raises(InvalidDeploymentLinkError, match=text):
            declare(built_http, "shop", SHOP, group=request_group)
    with pytest.raises(InvalidDeploymentLinkError, match=text):
        declare(built_http, "shop", SHOP + "/v1")  # a path under the origin is the same origin
    other = declare(built_http, "orders", "https://orders.example.test")
    with pytest.raises(InvalidDeploymentLinkError, match=text):
        service(built_http).deployment_link_update(
            DeploymentLinkUpdateRequest(
                context=repository_context(built_http, "orders"),
                id=other,
                link=DeploymentLinkFields(label="moved", url=SHOP),
            )
        )
    assert [link.url for link in built_http.deployment_links.list_all()] == [
        "https://orders.example.test"
    ]

    service(built_http).service_declaration_delete(
        ServiceDeclarationDeleteRequest(context=GLOBAL, id=held)
    )
    assert map_preview() == baseline
    assert declare(built_http, "shop", SHOP, group=group) > other


# -- N4: an import bypasses the write checks ------------------------------------------------------


def test_n4_an_imported_classification_that_conflicts_is_ineffective_with_a_reason(
    built_http: Workspace,
) -> None:
    """N4: the write checks do not run for an import, so the worklist judges it on every read:
    a published service the source identifies, an origin a link states, and an origin nothing
    requests any more. None gives a request an identity, none crashes the worklist."""
    declare(built_http, "shop", SHOP)
    store = built_http.service_declarations
    for origin in (SVC, SHOP, "https://unused.example.test"):
        store.create(external(origin, f"imported {origin}"), via="import")
    judged = {
        stated(d).origin: (d.state, d.reason, d.requests)
        for d in worklist(built_http).service_declarations
    }
    assert judged == {
        SVC: ("ineffective", "source-identifies-origin", 0),
        SHOP: ("ineffective", "origin-declared-as-deployment", 0),
        "https://unused.example.test": ("invalidated", "origin-no-longer-requested", 0),
    }
    response = worklist(built_http, include_resolved=True)
    assert response.summary.known_external == 0
    assert {r.identity for g in response.groups for r in g.samples} <= {"unresolved", "identified"}
    carried = group_of(response, SHOP).external_service
    assert carried is not None and carried.reason == "origin-declared-as-deployment"
    assert worklist(built_http, identity="known-external").groups == ()
    assert [r.identity for r in requests_at(built_http, SHOP).values()] == ["unresolved"] * 4
    # a preview of the conflicting origin still answers, and says why it cannot be stored again
    assert not preview(built_http, external(SVC, "other words")).can_create


def test_n4_a_classification_whose_authenticated_repository_is_gone_is_invalidated(
    built: Workspace,
) -> None:
    """The reason set's `authenticated-service-not-indexed`: the classification states a
    repository this index does not hold, so it gives no request an identity until it does."""
    built.service_declarations.create(
        external(authenticates=[AuthenticationTarget(repository="nowhere")]), via="import"
    )
    [held] = worklist(built).service_declarations
    assert (held.state, held.reason) == ("invalidated", "authenticated-service-not-indexed")
    assert requests_at(built, AUTH)[TOKEN_REQUEST].identity == "unresolved"


def test_an_authentication_target_must_be_an_indexed_repository_at_write_time(
    built: Workspace,
) -> None:
    """Amendment 2: the write refuses an authenticated repository the index does not hold (a
    preview lists it, a create stores nothing and writes no audit row, an update keeps what was
    stored), and accepts an indexed one. An import still bypasses the write, and the read-time
    invalidation above stays."""
    reason = "nowhere is not an indexed repository: an authentication target is an application "
    reason += "service or deployment the index holds"
    unindexed = external(authenticates=[AuthenticationTarget(repository="nowhere")])
    checked = preview(built, unindexed)
    assert not checked.can_create and checked.requirements == (reason,)
    with pytest.raises(InvalidDeploymentLinkError, match="nowhere is not an indexed repository"):
        classify(built, unindexed)
    assert built.service_declarations.list_all() == [] and audit_rows(built) == []

    # a mix names only the one the index does not hold
    portal = slug(built, "portal")
    mixed = external(
        authenticates=[
            AuthenticationTarget(repository=portal),
            AuthenticationTarget(repository="nowhere"),
        ]
    )
    assert preview(built, mixed).requirements == (reason,)

    indexed = external(authenticates=[AuthenticationTarget(repository=portal, environment="prod")])
    assert preview(built, indexed).can_create
    held = classify(built, indexed).declaration
    assert (held.state, held.reason) == ("applied", "")
    assert held.basis == "declared"  # an operator's statement, never source evidence
    assert [t.repository for t in stated(held).authenticates] == [portal]
    assert [row[0] for row in audit_rows(built)] == ["create"]

    # an update that names an unindexed repository is refused and the stored one is unchanged
    with pytest.raises(InvalidDeploymentLinkError, match="nowhere is not an indexed repository"):
        service(built).service_declaration_update(
            ServiceDeclarationUpdateRequest(context=GLOBAL, id=held.id, fields=unindexed)
        )
    [stored] = built.service_declarations.list_all()
    assert stored.fields == indexed
    assert [row[0] for row in audit_rows(built)] == ["create"]


# -- N16: a workspace view -------------------------------------------------------------------------


def test_n16_the_service_declaration_endpoints_need_global_scope(built: Workspace) -> None:
    inside = repository_context(built, "portal")
    asked = service(built)
    calls = [
        lambda: asked.service_declaration_preview(
            ServiceDeclarationPreviewRequest(context=inside, fields=external())
        ),
        lambda: asked.service_declaration_create(
            ServiceDeclarationCreateRequest(context=inside, fields=external())
        ),
        lambda: asked.service_declaration_update(
            ServiceDeclarationUpdateRequest(context=inside, id=1, fields=external())
        ),
        lambda: asked.service_declaration_delete(
            ServiceDeclarationDeleteRequest(context=inside, id=1)
        ),
        lambda: asked.service_declaration_history(ServiceDeclarationHistoryRequest(context=inside)),
    ]
    for call in calls:
        with pytest.raises(ContextResolutionError):
            call()
    assert built.service_declarations.list_all() == [] and audit_rows(built) == []


# -- the local API ---------------------------------------------------------------------------------


@pytest.fixture
def api(tmp_path: Path) -> Iterator[Serving]:
    settings = Settings.model_validate({"storage": {"database": str(tmp_path / "state/atlas.db")}})
    opened = open_workspace(settings=settings, require_root=False)
    try:
        build(opened, tmp_path, repos())
    finally:
        opened.close()
    with running(settings, tmp_path / "api.json") as serving:
        yield serving


def test_the_local_api_serves_the_five_service_declaration_endpoints(api: Serving) -> None:
    served = [endpoint.name for endpoint in ENDPOINTS]
    assert served[-5:] == [
        "service_declarations/preview",
        "service_declarations/create",
        "service_declarations/update",
        "service_declarations/delete",
        "service_declarations/history",
    ]
    everywhere = {"scope": "global"}
    groups = post(api, "http_connections", {"context": everywhere}).result["groups"]
    group = next(g["key"] for g in groups if g["origin"] == AUTH)
    fields = {"kind": "external_service", "origin": AUTH, "label": "Example login"}

    checked = post(
        api,
        "service_declarations/preview",
        {"context": everywhere, "fields": fields, "group": group},
    ).result
    assert checked["can_create"] is True and checked["counts"]["would_be_known_external"] == 1
    empty = post(api, "service_declarations/history", {"context": everywhere}).result
    assert empty["entries"] == []

    created = post(
        api,
        "service_declarations/create",
        {
            "context": everywhere,
            "fields": fields,
            "provenance": {"via": "ui", "request_group": group},
        },
    ).result["declaration"]
    assert (created["state"], created["reason"], created["requests"]) == ("applied", "", 1)
    listed = post(
        api, "http_connections", {"context": everywhere, "group": group, "limit": 10}
    ).result
    assert listed["summary"]["known_external"] == 1
    assert [(r["status"], r["identity"]) for r in listed["requests"]] == [
        (UNMATCHED, "known-external")
    ]
    carried = next(g for g in listed["groups"] if g["origin"] == AUTH)["external_service"]
    assert carried["id"] == created["id"]

    updated = post(
        api,
        "service_declarations/update",
        {"context": everywhere, "id": created["id"], "fields": {**fields, "label": "Renamed"}},
    ).result["declaration"]
    assert updated["fields"]["label"] == "Renamed"
    entries = post(
        api, "service_declarations/history", {"context": everywhere, "id": created["id"]}
    ).result["entries"]
    assert [e["action"] for e in entries] == ["update", "create"]

    inside = {"scope": "repository", "repository": "portal"}
    narrow = post(api, "service_declarations/create", {"context": inside, "fields": fields})
    assert narrow.status == 400
    loopback = {**fields, "origin": "http://localhost:8000"}
    machine = post(api, "service_declarations/create", {"context": everywhere, "fields": loopback})
    assert machine.status == 400

    deleted = post(
        api, "service_declarations/delete", {"context": everywhere, "id": created["id"]}
    ).result
    assert deleted["deleted"] is True
    again = post(api, "service_declarations/delete", {"context": everywhere, "id": created["id"]})
    assert again.status == 404
    after = post(api, "http_connections", {"context": everywhere}).result
    assert after["summary"]["known_external"] == 0 and after["service_declarations"] == []
