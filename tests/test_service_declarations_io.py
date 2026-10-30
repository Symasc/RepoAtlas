"""Export and import of external services (slice S4): ``repoatlas links export`` and ``import``.

External services travel in the links file under one extra key, ``service_declarations``, which is
written only when there are some: an index without any exports the file it always did, byte for
byte, and an older RepoAtlas (whose model forbids the key) refuses a file that has them. An import
stores the services first, skips what is already recorded word for word, and refuses the whole file
when it holds a redirect or a permitted callback (those are not supported in this version) or a
service that contradicts one already classified. Neutral, invented origins only.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from repoatlas.capabilities.service import CapabilityService
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.deployment_targets import (
    DeploymentLinkFields,
    ExportedLink,
    LinksExport,
)
from repoatlas.models.http_connections import HttpConnectionsRequest
from repoatlas.models.service_declarations import (
    AuthenticationTarget,
    ExternalServiceFields,
    PermittedCallbackFields,
    RedirectFields,
    RedirectSource,
)
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build
from tests.service_declarations_corpus import repos
from typer.testing import Result

AUTH = "https://auth.example.test"
LOGIN = "https://login.example.test"
PORTAL = "http://portal:8080"
REFUSED = "redirects and permitted callbacks are not supported in this version"

#: The fields a link export copied before external services existed (and before scopes).
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


def slug(workspace: Workspace, name: str) -> str:
    return next(r.slug for r in workspace.repositories.list_all() if r.name == name)


def provider(portal: str) -> ExternalServiceFields:
    return ExternalServiceFields(
        origin=AUTH,
        label="Example login",
        provider="Example IdP",
        category="identity",
        note="the token endpoint",
        authenticates=[AuthenticationTarget(repository=portal, environment="prod")],
    )


def hosted() -> ExternalServiceFields:
    return ExternalServiceFields(origin=LOGIN, label="Hosted login")


def audit_rows(workspace: Workspace) -> tuple[int, int]:
    """Rows of the link audit and of the service-declaration audit."""
    links = workspace.database.execute("SELECT COUNT(*) FROM declaration_audit").fetchone()[0]
    services = workspace.database.execute(
        "SELECT COUNT(*) FROM service_declaration_audit"
    ).fetchone()[0]
    return int(links), int(services)


def document(
    links: Sequence[Mapping[str, object]], declarations: Sequence[Mapping[str, object]]
) -> dict[str, object]:
    return {
        "format": "repoatlas/deployment-links/v1",
        "links": links,
        "service_declarations": declarations,
    }


def write(tmp_path: Path, content: dict[str, object], name: str = "in.json") -> Path:
    target = tmp_path / name
    target.write_text(json.dumps(content), encoding="utf-8")
    return target


def links_only_export(workspace: Workspace) -> str:
    """The export as it was written before external services: the same serialisation call over the
    links alone."""
    return (
        LinksExport(
            links=[
                ExportedLink(repository=link.repository, **link.model_dump(include=LINK_FIELDS))
                for link in workspace.deployment_links.list_all()
            ]
        ).model_dump_json(indent=2, exclude_none=True)
        + "\n"
    )


def test_external_services_with_authenticates_round_trip_through_the_file(
    built: Workspace, cli: Cli, tmp_path: Path
) -> None:
    """Export writes the services (every word, the authenticated repository and environment too)
    under ``service_declarations`` with a note on stderr that an older RepoAtlas refuses the file;
    after the index loses them, import stores the same declarations again, says so services first,
    and records each as an import."""
    portal = slug(built, "portal")
    built.deployment_links.create(portal, DeploymentLinkFields(label="prod", url=PORTAL))
    services = [provider(portal), hosted()]
    for fields in services:
        built.service_declarations.create(fields, via="api")
    target = tmp_path / "out.json"

    result = cli("export", target)
    assert result.exit_code == 0, result.output
    assert "older RepoAtlas will refuse the file" in result.output
    assert "external service declarations" in result.output
    exported = json.loads(target.read_text())
    assert exported["format"] == "repoatlas/deployment-links/v1"
    assert [item["origin"] for item in exported["service_declarations"]] == [AUTH, LOGIN]
    assert exported["service_declarations"][0]["authenticates"] == [
        {"repository": portal, "environment": "prod"}
    ]
    assert [link["url"] for link in exported["links"]] == [PORTAL]
    # the file reads back into the same declarations
    assert LinksExport.model_validate_json(target.read_text()).service_declarations == services

    for stored in built.service_declarations.list_all():
        assert built.service_declarations.delete(stored.id)
    for link in built.deployment_links.list_all():
        assert built.deployment_links.delete(link.id)
    result = cli("import", target)
    assert result.exit_code == 0, result.output
    assert "Added 2 external service(s); 0 already recorded." in result.output
    assert "Added 1 link(s); 0 already recorded." in result.output
    # the services are announced, and stored, before the links
    assert result.output.index("external service(s)") < result.output.index("link(s)")
    assert [d.fields for d in built.service_declarations.list_all()] == services
    imported = built.service_declarations.history(limit=2)
    assert [(e.action, e.via) for e in imported] == [("import", "import")] * 2


def test_an_export_with_no_external_service_is_byte_identical_to_the_links_only_export(
    built: Workspace, cli: Cli, tmp_path: Path
) -> None:
    """No ``service_declarations`` key (not an empty list, not null) and no note when there is no
    external service, so the file is the one written before. A redirect or a callback in the store
    (nothing in this version creates one) is not exported either, as no import would take it."""
    portal = slug(built, "portal")
    built.deployment_links.create(portal, DeploymentLinkFields(label="prod", url=PORTAL))
    built.deployment_links.create(portal, DeploymentLinkFields(label="plain"))
    target = tmp_path / "plain.json"

    result = cli("export", target)
    assert result.exit_code == 0, result.output
    text = target.read_text()
    assert text == links_only_export(built)
    assert "service_declarations" not in text and "external service" not in result.output

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
    again = tmp_path / "again.json"
    result = cli("export", again)
    assert result.exit_code == 0, result.output
    assert again.read_text() == text and "external service" not in result.output


def test_importing_the_same_file_twice_adds_nothing(
    built: Workspace, cli: Cli, tmp_path: Path
) -> None:
    portal = slug(built, "portal")
    built.deployment_links.create(portal, DeploymentLinkFields(label="prod", url=PORTAL))
    for fields in (provider(portal), hosted()):
        built.service_declarations.create(fields, via="api")
    target = tmp_path / "out.json"
    assert cli("export", target).exit_code == 0
    stored = built.service_declarations.list_all()
    audited = audit_rows(built)

    result = cli("import", target)
    assert result.exit_code == 0, result.output
    assert "Added 0 external service(s); 2 already recorded." in result.output
    assert "Added 0 link(s); 1 already recorded." in result.output
    assert built.service_declarations.list_all() == stored
    assert audit_rows(built) == audited

    for item in stored:
        assert built.service_declarations.delete(item.id)
    first = cli("import", target)
    assert "Added 2 external service(s); 0 already recorded." in first.output
    assert "Added 0 link(s); 1 already recorded." in first.output
    after_first = audit_rows(built)
    assert after_first[1] == audited[1] + 4  # two revokes, then two imports
    second = cli("import", target)
    assert "Added 0 external service(s); 2 already recorded." in second.output
    assert audit_rows(built) == after_first


def test_a_service_stated_twice_in_a_file_is_stored_once(
    built: Workspace, cli: Cli, tmp_path: Path
) -> None:
    item = json.loads(hosted().model_dump_json())
    target = write(tmp_path, document([], [item, item]))

    result = cli("import", target)
    assert result.exit_code == 0, result.output
    assert "Added 1 external service(s); 1 already recorded." in result.output
    assert [d.fields for d in built.service_declarations.list_all()] == [hosted()]


@pytest.mark.parametrize(
    "refused",
    [
        {
            "kind": "redirect",
            "source": {"form": "origin", "origin": LOGIN, "path": "/authorize"},
            "destination": f"{PORTAL}/auth/callback",
        },
        {
            "kind": "permitted_callback",
            "provider_origin": AUTH,
            "destination": f"{PORTAL}/auth/callback",
        },
    ],
    ids=["redirect", "permitted_callback"],
)
def test_a_file_with_a_redirect_or_a_callback_is_refused_whole_with_nothing_stored(
    built: Workspace, cli: Cli, tmp_path: Path, refused: dict[str, object]
) -> None:
    """The service and the link in the same file are valid and come first, yet none is stored: the
    file is judged whole before anything is written, and no audit row of either kind is added."""
    portal = slug(built, "portal")
    link = {"repository": portal, "label": "prod", "url": PORTAL}
    service = json.loads(hosted().model_dump_json())
    target = write(tmp_path, document([link], [service, refused]))
    audited = audit_rows(built)

    result = cli("import", target)
    assert result.exit_code != 0
    assert REFUSED in result.output
    assert built.service_declarations.list_all() == []
    assert built.deployment_links.list_all() == []
    assert audit_rows(built) == audited
    assert "Added" not in result.output


def test_a_service_that_contradicts_a_classified_origin_refuses_the_file_whole(
    built: Workspace, cli: Cli, tmp_path: Path
) -> None:
    """The index already classifies the origin in other words, and so does the file twice: the
    file is refused, nothing of it is stored, and the classification in the index is kept."""
    portal = slug(built, "portal")
    held = built.service_declarations.create(hosted(), via="api")
    link = {"repository": portal, "label": "prod", "url": PORTAL}
    other = json.loads(ExternalServiceFields(origin=AUTH, label="Example login").model_dump_json())
    clash = json.loads(ExternalServiceFields(origin=LOGIN, label="Another name").model_dump_json())
    audited = audit_rows(built)

    result = cli("import", write(tmp_path, document([link], [other, clash])))
    assert result.exit_code != 0 and "revoke one of them first" in result.output
    assert built.service_declarations.list_all() == [held]
    assert built.deployment_links.list_all() == []
    assert audit_rows(built) == audited

    within = write(tmp_path, document([link], [other, {**other, "label": "Renamed"}]), "two.json")
    result = cli("import", within)
    assert result.exit_code != 0 and "revoke one of them first" in result.output
    assert built.service_declarations.list_all() == [held]
    assert audit_rows(built) == audited


def test_an_imported_service_naming_an_unindexed_repository_is_stored_and_invalidated(
    built: Workspace, cli: Cli, tmp_path: Path
) -> None:
    """The write-time check is the API's. An import bypasses it, and the worklist then says why
    the classification gives no request an identity."""
    gone = ExternalServiceFields(
        origin=AUTH,
        label="Example login",
        authenticates=[AuthenticationTarget(repository="nowhere")],
    )
    result = cli("import", write(tmp_path, document([], [json.loads(gone.model_dump_json())])))
    assert result.exit_code == 0, result.output
    assert [d.fields for d in built.service_declarations.list_all()] == [gone]
    answer = CapabilityService(built).http_connections(
        HttpConnectionsRequest(context=CapabilityContext(scope=CapabilityScope.GLOBAL))
    )
    [view] = answer.service_declarations
    assert (view.state, view.reason, view.basis) == (
        "invalidated",
        "authenticated-service-not-indexed",
        "declared",
    )


class LinksOnlyExport(BaseModel):
    """The links-only field set this file's export had before ``service_declarations``, with the
    same strictness: what an older RepoAtlas reads."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    format: Literal["repoatlas/deployment-links/v1"] = "repoatlas/deployment-links/v1"
    links: list[ExportedLink] = Field(default_factory=list)


def test_an_older_model_refuses_a_file_with_service_declarations(
    built: Workspace, cli: Cli, tmp_path: Path
) -> None:
    """The format string is unchanged, so only the extra key keeps an older RepoAtlas from reading
    half of a file: its strict model refuses the file with services and reads the one without."""
    assert set(LinksExport.model_fields) - set(LinksOnlyExport.model_fields) == {
        "service_declarations"
    }
    built.deployment_links.create(
        slug(built, "portal"), DeploymentLinkFields(label="prod", url=PORTAL)
    )
    plain = tmp_path / "plain.json"
    assert cli("export", plain).exit_code == 0
    assert LinksOnlyExport.model_validate_json(plain.read_text()).links[0].url == PORTAL

    built.service_declarations.create(hosted(), via="api")
    with_services = tmp_path / "with.json"
    assert cli("export", with_services).exit_code == 0
    with pytest.raises(ValidationError, match="service_declarations"):
        LinksOnlyExport.model_validate_json(with_services.read_text())
    # the current model reads both, with the same format string
    for target in (plain, with_services):
        assert LinksExport.model_validate_json(target.read_text()).format == (
            "repoatlas/deployment-links/v1"
        )


def test_a_service_declaration_that_is_not_valid_is_not_an_export(
    built: Workspace, cli: Cli, tmp_path: Path
) -> None:
    """A loopback origin, a wildcard or an unknown kind is refused by the same models the API uses,
    before the index is opened to write."""
    for bad in (
        {"kind": "external_service", "origin": "http://localhost:8000", "label": "x"},
        {"kind": "external_service", "origin": "https://*.example.test", "label": "x"},
        {"kind": "something_else", "origin": AUTH, "label": "x"},
    ):
        result = cli("import", write(tmp_path, document([], [bad])))
        assert result.exit_code != 0 and "Not an export of deployment links" in result.output
    assert built.service_declarations.list_all() == []


def test_an_export_says_how_many_redirects_and_callbacks_it_leaves_out(
    built: Workspace, cli: Cli, tmp_path: Path
) -> None:
    """The file is still the links-only file and the exit code 0, but a stored redirect and a
    stored permitted callback are counted on stderr, so an export is never taken for a backup that
    restores them. None stored: no such note."""
    built.deployment_links.create(
        slug(built, "portal"), DeploymentLinkFields(label="prod", url=PORTAL)
    )
    quiet = cli("export", tmp_path / "quiet.json")
    assert quiet.exit_code == 0, quiet.output
    assert "declaration(s)" not in quiet.output

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
    target = tmp_path / "out.json"

    result = cli("export", target)
    assert result.exit_code == 0, result.output
    assert target.read_text() == links_only_export(built)
    assert "2 redirect or permitted-callback declaration(s)" in result.output
    assert "does not restore them" in result.output
    assert "external service" not in result.output


def test_a_file_with_a_valid_service_then_a_redirect_stores_nothing(
    built: Workspace, cli: Cli, tmp_path: Path
) -> None:
    """Import is atomic: the valid service comes first and the redirect after it, yet the file is
    refused whole, no declaration is stored and neither audit table gains a row."""
    service = json.loads(hosted().model_dump_json())
    redirect = {
        "kind": "redirect",
        "source": {"form": "origin", "origin": LOGIN, "path": "/authorize"},
        "destination": f"{PORTAL}/auth/callback",
    }
    audited = audit_rows(built)

    result = cli("import", write(tmp_path, document([], [service, redirect])))
    assert result.exit_code != 0
    assert REFUSED in result.output
    assert built.service_declarations.list_all() == []
    assert audit_rows(built) == audited
    assert audited[1] == 0
