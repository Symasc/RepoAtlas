"""External services, end to end (slice S6): the acceptance examples in one place.

Four contracts over the invented corpus ``tests/service_declarations_corpus.py`` (a portal that a
compose file publishes at ``http://portal:8080``, a worker that asks ``auth.example.test`` for a
token and then calls the portal, a web app):

* the OAuth client_credentials example: two dependencies, an authentication relation that is the
  operator's word, and no redirect anywhere;
* an export says what it holds and what it leaves out;
* an import is judged whole before anything is stored;
* a classification is created, edited, revoked and recorded, and a revoked one leaves no trace in
  the worklist.

The finer cases are in ``test_service_declarations_api.py`` and ``test_service_declarations_io.py``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.deployment_targets import (
    DeclarationProvenance,
    DeploymentLinkFields,
    ExportedLink,
    LinksExport,
)
from repoatlas.models.http_connections import (
    ConnectionGroup,
    ConnectionRequest,
    HttpConnectionsRequest,
    HttpConnectionsResponse,
)
from repoatlas.models.service_declaration_io import (
    ServiceDeclarationCreateRequest,
    ServiceDeclarationDeleteRequest,
    ServiceDeclarationHistoryRequest,
    ServiceDeclarationPreviewRequest,
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
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build
from tests.service_declarations_corpus import repos
from typer.testing import Result

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)
AUTH = "https://auth.example.test"
LOGIN = "https://login.example.test"
PORTAL = "http://portal:8080"
UNMATCHED = "http-service-unmatched"
REFUSED = "redirects and permitted callbacks are not supported in this version"

#: The fields a link export copies (an unscoped link has no scope).
LINK_FIELDS = {
    "label",
    "environment",
    "callers",
    "url",
    "target_id",
    "variables",
    "conditions",
    "verification",
}

Cli = Callable[..., Result]


@pytest.fixture
def built(workspace: Workspace, tmp_path: Path) -> Workspace:
    return build(workspace, tmp_path, repos())


@pytest.fixture
def cli(built: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Cli:
    """``repoatlas links <args>`` against the built index."""
    from repoatlas.cli.main import app
    from typer.testing import CliRunner

    monkeypatch.setenv("REPOATLAS_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("REPOATLAS_DB", str(built.settings.storage.database))
    runner = CliRunner()

    def run(*args: object) -> Result:
        return runner.invoke(app, ["links", *(str(arg) for arg in args)])

    return run


def service(workspace: Workspace) -> CapabilityService:
    return CapabilityService(workspace)


def slug(workspace: Workspace, name: str) -> str:
    return next(r.slug for r in workspace.repositories.list_all() if r.name == name)


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


def every_request(workspace: Workspace) -> list[ConnectionRequest]:
    """All the corpus' requests: the three origins hold every one (checked against the summary)."""
    found = [r for o in (AUTH, LOGIN, PORTAL) for r in requests_at(workspace, o).values()]
    assert len(found) == worklist(workspace).summary.requests
    return found


def stated(view: ServiceDeclarationView) -> ExternalServiceFields:
    assert isinstance(view.fields, ExternalServiceFields)
    return view.fields


def external(label: str = "Example login", **more: Any) -> ExternalServiceFields:
    return ExternalServiceFields(origin=AUTH, label=label, **more)


def classify(
    workspace: Workspace, fields: ExternalServiceFields, *, group: str, note: str | None = None
) -> ServiceDeclarationResponse:
    return service(workspace).service_declaration_create(
        ServiceDeclarationCreateRequest(
            context=GLOBAL,
            fields=fields,
            provenance=DeclarationProvenance(via="ui", note=note, request_group=group),
        )
    )


def decision_table(workspace: Workspace) -> list[tuple[object, ...]]:
    """Every request decision (reference, outcome, evidence, chain), as stored."""
    return [
        tuple(row)
        for row in workspace.database.execute(
            """SELECT decision_ref, decision, evidence_json, chain_json
               FROM http_call_decisions ORDER BY decision_ref"""
        ).fetchall()
    ]


def edges(workspace: Workspace) -> list[tuple[object, ...]]:
    """Every edge of the graph, whole rows, in a fixed order."""
    rows = [tuple(row) for row in workspace.database.execute("SELECT * FROM edges").fetchall()]
    return sorted(rows, key=repr)


def audit_rows(workspace: Workspace) -> tuple[int, int]:
    """Rows of the link audit and of the service-declaration audit."""
    links = workspace.database.execute("SELECT COUNT(*) FROM declaration_audit").fetchone()[0]
    services = workspace.database.execute(
        "SELECT COUNT(*) FROM service_declaration_audit"
    ).fetchone()[0]
    return int(links), int(services)


def audit_actions(workspace: Workspace) -> list[str]:
    """The service-declaration audit's actions, oldest first."""
    return [
        row[0]
        for row in workspace.database.execute(
            "SELECT action FROM service_declaration_audit ORDER BY id"
        ).fetchall()
    ]


# -- the OAuth client_credentials example ---------------------------------------------------------


def test_oauth_client_credentials_is_two_dependencies_an_operator_relation_and_no_redirect(
    built: Workspace,
) -> None:
    """The worker asks the provider for a token, then calls the portal. Classifying the provider
    (it authenticates for the portal) changes how the token request is shown and nothing else:
    the relation is the operator's statement, no redirect is stored or invented, and no step, edge
    or handler link is added for the provider's origin."""
    worker, portal = slug(built, "worker"), slug(built, "portal")

    def dependencies() -> dict[str, ConnectionRequest]:
        found = {
            origin: request
            for origin in (AUTH, LOGIN, PORTAL)
            for request in requests_at(built, origin).values()
            if request.site.repository == worker
        }
        assert len(found) == 2  # one request per origin: no origin holds two of the worker's
        return found

    # before: two dependencies, an unmatched one and a linked one
    before = dependencies()
    assert set(before) == {AUTH, PORTAL}
    token_before, api_before = before[AUTH], before[PORTAL]
    assert (token_before.method, token_before.route) == ("POST", "/oauth/token")
    assert token_before.status.value == UNMATCHED
    assert (token_before.identity, token_before.external_service_id) == ("unresolved", None)
    assert api_before.status.value == "linked" and api_before.identity == "identified"
    table, graph = decision_table(built), edges(built)
    assert worklist(built).summary.known_external == 0

    # the preview adds no step: there is no redirect for it to add
    group = group_of(worklist(built), AUTH).key
    fields = external(
        provider="Example IdP",
        category="identity",
        authenticates=[AuthenticationTarget(repository=portal)],
    )
    checked = service(built).service_declaration_preview(
        ServiceDeclarationPreviewRequest(context=GLOBAL, fields=fields, group=group)
    )
    assert checked.can_create and checked.counts.redirect_steps_added == 0
    assert [row.steps_added for row in checked.rows] == [0]
    assert built.service_declarations.list_all() == []

    held = classify(built, fields, group=group).declaration
    assert (held.state, held.reason, held.requests) == ("applied", "", 1)

    # after: the auth request is known external, and only its identity moved
    after = dependencies()
    assert set(after) == {AUTH, PORTAL}
    token_after = after[AUTH]
    assert (token_after.identity, token_after.external_service_id) == ("known-external", held.id)
    plain = {"identity": "unresolved", "external_service_id": None}
    assert token_after.model_copy(update=plain) == token_before  # status, reason, join: as were
    assert token_after.status.value == UNMATCHED and token_after.joined_by is None
    assert token_after.reason == token_before.reason
    # ... the same stored decision and chain, for it and for every other request
    row_before = next(r for r in table if r[0] == token_before.decision_ref)
    row_after = next(r for r in decision_table(built) if r[0] == token_after.decision_ref)
    assert row_after == row_before
    # the portal request is untouched
    assert after[PORTAL] == api_before

    # the relation is listed, and it is the operator's word, never the source's
    [view] = worklist(built).service_declarations
    assert view.id == held.id and view.basis == "declared" and held.basis == "declared"
    assert [(t.repository, t.environment) for t in stated(view).authenticates] == [(portal, None)]
    carried = group_of(worklist(built), AUTH).external_service
    assert carried is not None and carried.basis == "declared"

    # zero redirects: none stored, no step, no edge, no handler link, the same join
    assert [d.kind for d in built.service_declarations.list_all()] == ["external_service"]
    assert built.service_declarations.list_all("redirect") == []
    assert built.service_declarations.list_all("permitted_callback") == []
    assert decision_table(built) == table
    assert edges(built) == graph
    assert built.deployment_links.list_all() == []

    # the summary: the portal is identified, the token request known external, the authorize page
    # of the web app still unresolved
    summary = worklist(built).summary
    assert summary.known_external == 1
    assert summary.unresolved_identity == 1
    open_unknown = [
        r
        for r in every_request(built)
        if r.status.value != "linked" and r.identity != "known-external"
    ]
    assert summary.unresolved_identity == len(open_unknown)
    assert [r.route for r in open_unknown] == ["/authorize"]


# -- export completeness --------------------------------------------------------------------------


def test_an_export_holds_the_links_and_the_service_and_counts_what_it_leaves_out(
    built: Workspace, cli: Cli, tmp_path: Path
) -> None:
    """With a link, a classified service, a redirect and a permitted callback stored: exit 0, a
    file that is the links-only file plus the one external service (the redirect and the callback
    are not in it), and a note on stderr that counts the two it leaves out."""
    portal = slug(built, "portal")
    built.deployment_links.create(portal, DeploymentLinkFields(label="prod", url=PORTAL))
    classified = external(
        provider="Example IdP",
        authenticates=[AuthenticationTarget(repository=portal, environment="prod")],
    )
    built.service_declarations.create(classified, via="api")
    built.service_declarations.create(
        RedirectFields(
            source=RedirectSource(form="origin", origin=LOGIN, path="/authorize"),
            destination=f"{PORTAL}/auth/callback",
        ),
        via="api",
    )
    built.service_declarations.create(
        PermittedCallbackFields(provider_origin=AUTH, destination=f"{PORTAL}/auth/callback"),
        via="api",
    )
    stored = built.service_declarations.list_all()
    assert [d.kind for d in stored] == ["external_service", "redirect", "permitted_callback"]
    target = tmp_path / "out.json"

    result = cli("export", target)

    assert result.exit_code == 0, result.output
    text = target.read_text()
    exported = json.loads(text)
    # the links-only serialisation of the same links, plus the one key for the service
    links_only = json.loads(
        LinksExport(
            links=[
                ExportedLink(repository=link.repository, **link.model_dump(include=LINK_FIELDS))
                for link in built.deployment_links.list_all()
            ]
        ).model_dump_json(exclude_none=True)
    )
    assert exported.pop("service_declarations") == [
        json.loads(classified.model_dump_json(exclude_none=True))
    ]
    assert exported == links_only
    assert [link["url"] for link in exported["links"]] == [PORTAL]
    # the service is exported, word for word; the redirect and the callback are not in the file
    assert LinksExport.model_validate_json(text).service_declarations == [classified]
    assert "redirect" not in text and "permitted_callback" not in text
    assert LOGIN not in text and "/auth/callback" not in text
    # what was left out is counted, not dropped silently
    assert "Wrote 1 link(s) and 1 external service(s)" in result.output
    assert "2 redirect or permitted-callback declaration(s)" in result.output
    assert "does not restore them" in result.output
    # an export reads the index and changes nothing in it
    assert built.service_declarations.list_all() == stored


# -- import atomicity -----------------------------------------------------------------------------


def test_a_file_with_a_valid_service_then_a_redirect_is_refused_with_nothing_stored(
    built: Workspace, cli: Cli, tmp_path: Path
) -> None:
    """The valid service comes first and the redirect after it, with a valid link in the same file:
    the whole file is refused, no declaration is stored, neither audit table gains a row and the
    links are as they were."""
    portal = slug(built, "portal")
    built.deployment_links.create(portal, DeploymentLinkFields(label="prod", url=PORTAL))
    links_before = built.deployment_links.list_all()
    audited = audit_rows(built)
    new_link = {"repository": portal, "label": "staging", "url": "http://portal:9090"}
    hosted_fields = ExternalServiceFields(origin=LOGIN, label="Hosted login")
    hosted = json.loads(hosted_fields.model_dump_json())
    redirect = {
        "kind": "redirect",
        "source": {"form": "origin", "origin": LOGIN, "path": "/authorize"},
        "destination": f"{PORTAL}/auth/callback",
    }
    document = {
        "format": "repoatlas/deployment-links/v1",
        "links": [new_link],
        "service_declarations": [hosted, redirect],
    }
    target = tmp_path / "in.json"
    target.write_text(json.dumps(document), encoding="utf-8")

    result = cli("import", target)

    assert result.exit_code != 0
    assert REFUSED in result.output
    assert "Added" not in result.output
    assert built.service_declarations.list_all() == []
    assert built.deployment_links.list_all() == links_before
    assert audit_rows(built) == audited


# -- lifecycle ------------------------------------------------------------------------------------


def test_a_classification_is_created_edited_revoked_and_recorded_and_then_leaves_no_trace(
    built: Workspace,
) -> None:
    group = group_of(worklist(built), AUTH).key
    assert group_of(worklist(built), AUTH).external_service is None

    created = classify(built, external(), group=group, note="the provider").declaration
    assert (created.state, created.requests) == ("applied", 1)
    assert requests_at(built, AUTH)[("/oauth/token", "POST")].identity == "known-external"

    renamed = service(built).service_declaration_update(
        ServiceDeclarationUpdateRequest(
            context=GLOBAL,
            id=created.id,
            fields=external(label="Renamed provider"),
            provenance=DeclarationProvenance(via="api", note="rename"),
        )
    )
    updated = renamed.declaration
    assert updated.id == created.id and stated(updated).label == "Renamed provider"
    carried = group_of(worklist(built), AUTH).external_service
    assert carried is not None and carried.id == created.id
    assert stated(carried).label == "Renamed provider"

    revoked = service(built).service_declaration_delete(
        ServiceDeclarationDeleteRequest(
            context=GLOBAL, id=created.id, provenance=DeclarationProvenance(via="ui", note="wrong")
        )
    )
    assert revoked.deleted is True

    # recorded, oldest first: create, update, revoke (the history lists the newest first)
    assert audit_actions(built) == ["create", "update", "revoke"]
    history = service(built).service_declaration_history(
        ServiceDeclarationHistoryRequest(context=GLOBAL, id=created.id)
    )
    assert [e.action for e in reversed(history.entries)] == ["create", "update", "revoke"]
    assert history.entries[0].note == "wrong" and history.entries[0].after is None

    # gone from the worklist: no group carries it, no request is known external
    assert built.service_declarations.list_all() == []
    after = worklist(built)
    assert after.service_declarations == ()
    assert group_of(after, AUTH).external_service is None
    token = requests_at(built, AUTH)[("/oauth/token", "POST")]
    assert (token.identity, token.external_service_id) == ("unresolved", None)
    assert token.status.value == UNMATCHED
    assert (after.summary.known_external, after.summary.unresolved_identity) == (0, 2)
    assert worklist(built, identity="known-external").groups == ()
