"""Service declarations in the store (external services, redirects, permitted callbacks; slice S1).

Pins what migration 007 creates and leaves alone, what the models refuse, and what the store keeps:
an idempotent create, an update that records only a change, an identity that cannot change, one
audit row per create, update, revoke and import, and an audit table nothing can rewrite. A sync
reads and writes none of it. Neutral hosts only: ``auth.example.test``, ``login.example.test`` and
the compose origin ``http://portal:8080``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from repoatlas.models.deployment_targets import (
    MAX_LINK_CALLERS,
    DeclarationProvenance,
    DeploymentLinkFields,
)
from repoatlas.models.service_declarations import (
    SERVICE_DECLARATION_FIELDS,
    AuthenticationTarget,
    ExternalServiceFields,
    PermittedCallbackFields,
    RedirectFields,
    RedirectSource,
    ServiceDeclaration,
    ServiceDeclarationConflictError,
    ServiceDeclarationFields,
    ServiceDeclarationIdentityError,
    ServiceDeclarationLimitError,
)
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace
from repoatlas.storage.sqlite import service_declarations as store_module
from repoatlas.storage.sqlite.database import SCHEMA_VERSION
from repoatlas.storage.sqlite.migrator import load_migrations, user_version
from repoatlas.storage.sqlite.service_declarations import (
    MAX_SERVICE_DECLARATIONS,
    SqliteServiceDeclarationStore,
)
from tests.http_connections_corpus import repos
from tests.impact_corpus import build
from tests.incremental_corpus import append

AUTH = "https://auth.example.test"
LOGIN = "https://login.example.test"
PORTAL = "http://portal:8080"
WILDCARD = "https://*.example.test"
LOCAL = "http://localhost:9000"
GROUP = "hcg1-0123456789abcdef0123"


def external(
    origin: str = AUTH, label: str = "Example login", **more: Any
) -> ExternalServiceFields:
    return ExternalServiceFields(origin=origin, label=label, **more)


def origin_redirect(
    origin: str = LOGIN,
    path: str = "/authorize",
    destination: str | None = f"{PORTAL}/auth/callback",
    **more: Any,
) -> RedirectFields:
    return RedirectFields(
        source=RedirectSource(form="origin", origin=origin, path=path),
        destination=destination,
        **more,
    )


def route_redirect(
    repository: str = "portal",
    route: str = "GET /login",
    destination: str | None = f"{LOGIN}/authorize",
    **more: Any,
) -> RedirectFields:
    return RedirectFields(
        source=RedirectSource(form="route", repository=repository, route=route),
        destination=destination,
        **more,
    )


def callback(
    provider: str = AUTH, destination: str = f"{PORTAL}/auth/callback", **more: Any
) -> PermittedCallbackFields:
    return PermittedCallbackFields(provider_origin=provider, destination=destination, **more)


def audit_rows(workspace: Workspace) -> list[tuple[object, ...]]:
    sql = "SELECT * FROM service_declaration_audit ORDER BY id"
    return [tuple(row) for row in workspace.database.execute(sql).fetchall()]


def link_audit_rows(workspace: Workspace) -> list[tuple[object, ...]]:
    sql = "SELECT * FROM declaration_audit ORDER BY id"
    return [tuple(row) for row in workspace.database.execute(sql).fetchall()]


def declaration_rows(workspace: Workspace) -> list[tuple[object, ...]]:
    sql = "SELECT * FROM service_declarations ORDER BY id"
    return [tuple(row) for row in workspace.database.execute(sql).fetchall()]


def stated(declaration: ServiceDeclaration) -> dict[str, object]:
    words: dict[str, object] = json.loads(declaration.fields.model_dump_json())
    return words


def one_of_each() -> list[ServiceDeclarationFields]:
    return [external(), origin_redirect(), route_redirect(), callback()]


# -- the models --------------------------------------------------------------------------------


def test_origins_and_paths_are_kept_in_one_canonical_spelling() -> None:
    assert external(origin="https://Auth.Example.Test/").origin == AUTH
    assert external(origin=PORTAL).origin == PORTAL
    assert callback(provider="https://Auth.Example.Test").provider_origin == AUTH
    for path in ("", "/"):
        assert RedirectSource(form="origin", origin=LOGIN, path=path).path == ""
    assert RedirectSource(form="origin", origin=LOGIN, path="/authorize/").path == "/authorize"
    # A destination may lie anywhere, this machine included: it is text, never contacted.
    assert origin_redirect(destination="http://localhost:3000/cb").destination is not None
    assert origin_redirect(destination=None).destination is None


def test_words_are_stripped_and_blank_optional_words_are_absent() -> None:
    fields = external(label="  Example login  ", provider="   ", category=" identity ", note="\n")
    assert (fields.label, fields.provider, fields.category, fields.note) == (
        "Example login",
        None,
        "identity",
        None,
    )
    assert external(note="line one\n\tline two").note == "line one\n\tline two"


def test_authenticated_repositories_are_sorted_and_named_once() -> None:
    fields = external(
        authenticates=[
            AuthenticationTarget(repository="worker"),
            AuthenticationTarget(repository="portal", environment="prod"),
            AuthenticationTarget(repository="portal"),
        ]
    )
    assert [(t.repository, t.environment) for t in fields.authenticates] == [
        ("portal", None),
        ("portal", "prod"),
        ("worker", None),
    ]
    with pytest.raises(ValidationError, match="named once"):
        external(authenticates=[AuthenticationTarget(repository="portal")] * 2)


def test_identity_keys_tell_what_a_declaration_is_about() -> None:
    assert external().identity_key == AUTH
    assert origin_redirect().identity_key == f"origin {LOGIN}/authorize"
    assert origin_redirect(path="").identity_key == f"origin {LOGIN}"
    assert route_redirect().identity_key == "route portal GET /login"
    assert callback().identity_key == f"callback {AUTH}"
    # What a person changes without changing what it is about does not move the key.
    assert origin_redirect(destination=None).identity_key == origin_redirect().identity_key
    assert external(label="Other").identity_key == external().identity_key


TOO_MANY = [AuthenticationTarget(repository=f"r{n}") for n in range(MAX_LINK_CALLERS)]

REFUSED: dict[str, Callable[[], object]] = {
    "origin with a query": lambda: external(origin=f"{AUTH}?tenant=a"),
    "origin with a fragment": lambda: external(origin=f"{AUTH}#top"),
    "origin with credentials": lambda: external(origin="https://user:pw@auth.example.test"),
    "origin with a wildcard host": lambda: external(origin=WILDCARD),
    "origin with a template hole": lambda: external(origin="https://{tenant}.example.test"),
    "origin with a path": lambda: external(origin=f"{AUTH}/oauth"),
    "origin that is not http": lambda: external(origin="ftp://auth.example.test"),
    "origin that is loopback": lambda: external(origin=LOCAL),
    "empty label": lambda: external(label="   "),
    "label with a control character": lambda: external(label="bad\x00label"),
    "secret-bearing note": lambda: external(note="see https://user:pw@host.example.test/x"),
    "secret-bearing provider": lambda: external(provider="${{ secrets.PROVIDER }}"),
    "secret-bearing category": lambda: external(category="${{ secrets.CATEGORY }}"),
    "too long a provider": lambda: external(provider="p" * 101),
    "too many authenticated repositories": lambda: external(
        authenticates=[*TOO_MANY, AuthenticationTarget(repository="one-more")]
    ),
    "authenticated repository with whitespace": lambda: AuthenticationTarget(repository="a b"),
    "source origin with a wildcard host": lambda: origin_redirect(origin=WILDCARD),
    "source origin that is loopback": lambda: origin_redirect(origin="http://127.0.0.1:8000"),
    "source path with a hole": lambda: origin_redirect(path="/auth/{tenant}"),
    "source path with a parameter": lambda: origin_redirect(path="/users/:id"),
    "source path with a wildcard": lambda: origin_redirect(path="/auth/*"),
    "source path with a query": lambda: origin_redirect(path="/authorize?x=1"),
    "source path with an empty segment": lambda: origin_redirect(path="/auth//x"),
    "source path without a leading slash": lambda: origin_redirect(path="authorize"),
    "route that names no method": lambda: route_redirect(route="/login"),
    "route that is not canonical": lambda: route_redirect(route="get /login"),
    "route source that also names an origin": lambda: RedirectSource(
        form="route", repository="portal", route="GET /login", origin=LOGIN
    ),
    "origin source that also names a route": lambda: RedirectSource(
        form="origin", origin=LOGIN, route="GET /login"
    ),
    "origin source without an origin": lambda: RedirectSource(form="origin"),
    "destination with a query": lambda: origin_redirect(destination=f"{LOGIN}/cb?next=/home"),
    "destination with a fragment": lambda: origin_redirect(destination=f"{LOGIN}/cb#frag"),
    "destination with credentials": lambda: origin_redirect(
        destination="https://user:pw@login.example.test/cb"
    ),
    "destination with a wildcard host": lambda: origin_redirect(destination=f"{WILDCARD}/cb"),
    "destination with a template hole": lambda: origin_redirect(destination=f"{LOGIN}/{{next}}"),
    "destination that is relative": lambda: origin_redirect(destination="/auth/callback"),
    "destination that is empty": lambda: origin_redirect(destination=""),
    "secret-bearing condition": lambda: origin_redirect(condition="run ${{ secrets.FLAG }}"),
    "secret-bearing redirect note": lambda: origin_redirect(note="see https://u:p@x.example.test"),
    "condition with a control character": lambda: origin_redirect(condition="a\x07b"),
    "callback provider with a wildcard host": lambda: callback(provider=WILDCARD),
    "callback provider that is loopback": lambda: callback(provider=LOCAL),
    "callback destination with a fragment": lambda: callback(destination=f"{PORTAL}/cb#frag"),
    "callback destination with a query": lambda: callback(destination=f"{PORTAL}/cb?next=1"),
    "callback destination with a wildcard": lambda: callback(destination=f"{WILDCARD}/cb"),
    "callback without a destination": lambda: PermittedCallbackFields(provider_origin=AUTH),  # type: ignore[call-arg]
    "callback with a secret-bearing note": lambda: callback(note="${{ secrets.NOTE }}"),
}


@pytest.mark.parametrize("make", REFUSED.values(), ids=list(REFUSED))
def test_a_refused_declaration_is_never_stored_and_leaves_no_audit_row(
    workspace: Workspace, make: Callable[[], Any]
) -> None:
    store = workspace.service_declarations
    with pytest.raises(ValidationError):
        store.create(make())
    assert store.list_all() == []
    assert audit_rows(workspace) == []
    assert declaration_rows(workspace) == []


def test_no_method_status_or_delivery_field_exists_anywhere() -> None:
    """A declaration states no HTTP method, status code, method preservation or delivery: none of
    them is known from it. No model has such a field and none accepts one."""
    classes = (
        ExternalServiceFields,
        RedirectFields,
        RedirectSource,
        PermittedCallbackFields,
        AuthenticationTarget,
        ServiceDeclaration,
    )
    words = ("method", "status", "deliver", "preserv", "response")
    for model in classes:
        for name in model.model_fields:
            assert not any(word in name for word in words), f"{model.__name__}.{name}"
    for extra in ("method", "status_code", "preserves_method", "delivered"):
        more: dict[str, Any] = {extra: "GET"}
        with pytest.raises(ValidationError):
            external(**more)
        with pytest.raises(ValidationError):
            origin_redirect(**more)
        with pytest.raises(ValidationError):
            callback(**more)
        with pytest.raises(ValidationError):
            RedirectSource(form="origin", origin=LOGIN, **more)
        with pytest.raises(ValidationError):
            AuthenticationTarget(repository="portal", **more)


def test_the_models_are_frozen() -> None:
    for fields in one_of_each():
        with pytest.raises(ValidationError):
            fields.note = "changed"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        origin_redirect().source.path = "/other"  # type: ignore[misc]


def test_the_union_reads_each_kind_into_its_class() -> None:
    for fields in one_of_each():
        for read in (
            SERVICE_DECLARATION_FIELDS.validate_python(fields.model_dump()),
            SERVICE_DECLARATION_FIELDS.validate_json(fields.model_dump_json()),
        ):
            assert type(read) is type(fields) and read == fields
    with pytest.raises(ValidationError):
        SERVICE_DECLARATION_FIELDS.validate_python({"kind": "other", "origin": AUTH})
    with pytest.raises(ValidationError):
        SERVICE_DECLARATION_FIELDS.validate_python({"origin": AUTH, "label": "no kind"})


# -- the store ----------------------------------------------------------------------------------


def test_the_workspace_carries_the_store(workspace: Workspace) -> None:
    assert isinstance(workspace.service_declarations, SqliteServiceDeclarationStore)
    assert workspace.service_declarations.list_all() == []


def test_every_kind_round_trips_through_the_store(workspace: Workspace) -> None:
    store = workspace.service_declarations
    fields = [
        external(
            provider="ExampleIdP",
            category="identity",
            note="first line\nsecond line",
            authenticates=[AuthenticationTarget(repository="portal", environment="prod")],
        ),
        origin_redirect(condition="after a successful login", label="login hop"),
        route_redirect(destination=None),
        callback(label="portal callback", note="allow-listed by the provider"),
    ]
    created = [store.create(item) for item in fields]

    assert [item.id for item in created] == [1, 2, 3, 4]
    assert [item.fields for item in created] == fields
    assert [item.kind for item in created] == [
        "external_service",
        "redirect",
        "redirect",
        "permitted_callback",
    ]
    assert store.list_all() == created
    assert [item.fields for item in store.list_all("redirect")] == fields[1:3]
    assert store.get(created[2].id) == created[2]
    assert store.get(99) is None
    sql = "SELECT kind, identity_key FROM service_declarations ORDER BY id"
    assert [tuple(row) for row in workspace.database.execute(sql)] == [
        (item.kind, item.identity_key) for item in created
    ]


def test_stating_what_is_already_stated_returns_it_and_writes_nothing(
    workspace: Workspace,
) -> None:
    store = workspace.service_declarations
    first = [store.create(item) for item in one_of_each()]
    rows, declarations = audit_rows(workspace), declaration_rows(workspace)
    assert len(rows) == 4

    again = [store.create(item) for item in one_of_each()]

    assert again == first
    assert audit_rows(workspace) == rows
    assert declaration_rows(workspace) == declarations


def test_an_origin_classified_in_other_words_is_refused_not_overwritten(
    workspace: Workspace,
) -> None:
    store = workspace.service_declarations
    first = store.create(external())
    rows = audit_rows(workspace)
    message = f"external service #{first.id}"

    for other in (external(label="Another label"), external(note="another note")):
        with pytest.raises(ServiceDeclarationConflictError, match=message) as raised:
            store.create(other)
        assert raised.value.declaration_id == first.id
    assert store.list_all() == [first] and audit_rows(workspace) == rows
    assert store.create(external(origin=LOGIN)).id != first.id


def test_redirects_from_one_source_to_different_places_are_separate_declarations(
    workspace: Workspace,
) -> None:
    """Conditional destinations: one source, several statements, none replaced by another."""
    store = workspace.service_declarations
    failed = f"{PORTAL}/auth/failed"
    first = store.create(origin_redirect(condition="signed in"))
    second = store.create(origin_redirect(destination=failed, condition="denied"))
    third = store.create(origin_redirect(condition="signed in, second factor"))
    unknown = store.create(origin_redirect(destination=None))

    assert len({first.id, second.id, third.id, unknown.id}) == 4
    assert {item.identity_key for item in store.list_all()} == {first.identity_key}
    assert len(audit_rows(workspace)) == 4
    # A callback is likewise one per statement, not one per provider.
    assert store.create(callback()).id != store.create(callback(destination=f"{PORTAL}/cb")).id


def test_an_unchanged_update_writes_nothing(workspace: Workspace) -> None:
    store = workspace.service_declarations
    created = [store.create(item) for item in one_of_each()]
    rows, declarations = audit_rows(workspace), declaration_rows(workspace)

    for item, fields in zip(created, one_of_each(), strict=True):
        assert store.update(item.id, fields) == item

    assert audit_rows(workspace) == rows
    assert declaration_rows(workspace) == declarations


def test_a_changed_update_keeps_the_identity_and_records_before_and_after(
    workspace: Workspace,
) -> None:
    store = workspace.service_declarations
    first = store.create(external())

    provenance = DeclarationProvenance(via="ui", note="renamed it", request_group=GROUP)
    changed = store.update(
        first.id, external(label="Renamed", note="now with a note"), provenance=provenance
    )

    assert changed is not None and changed.id == first.id
    assert changed.fields == external(label="Renamed", note="now with a note")
    assert changed.created_at == first.created_at and changed.updated_at >= first.updated_at
    latest = store.history(declaration_id=first.id)[0]
    assert (latest.action, latest.kind, latest.subject, latest.subject_id) == (
        "update",
        "external_service",
        AUTH,
        first.id,
    )
    assert (latest.via, latest.note, latest.request_group) == ("ui", "renamed it", GROUP)
    assert latest.before == stated(first) and latest.after == stated(changed)
    assert latest.before is not None and latest.before["label"] == "Example login"
    assert latest.after is not None and latest.after["label"] == "Renamed"

    # What is not part of the identity may change: a destination, a condition.
    redirect = store.create(origin_redirect())
    moved = store.update(redirect.id, origin_redirect(destination=None, condition="sometimes"))
    assert moved is not None and moved.fields.kind == "redirect" and moved.id == redirect.id
    permitted = store.create(callback())
    other = store.update(permitted.id, callback(destination=f"{PORTAL}/auth/other"))
    assert other is not None and other.fields == callback(destination=f"{PORTAL}/auth/other")


@pytest.mark.parametrize(
    ("held", "attempt"),
    [
        pytest.param(external(), external(origin=LOGIN), id="another origin"),
        pytest.param(external(), origin_redirect(), id="external service to redirect"),
        pytest.param(external(), callback(), id="external service to callback"),
        pytest.param(origin_redirect(), external(origin=LOGIN), id="redirect to external service"),
        pytest.param(origin_redirect(), origin_redirect(path="/other"), id="another source path"),
        pytest.param(
            origin_redirect(),
            origin_redirect(origin="https://other.example.test"),
            id="another source origin",
        ),
        pytest.param(origin_redirect(), route_redirect(), id="origin source to route source"),
        pytest.param(
            route_redirect(), route_redirect(route="POST /login"), id="another source route"
        ),
        pytest.param(
            route_redirect(), route_redirect(repository="other"), id="another source repository"
        ),
        pytest.param(callback(), callback(provider=LOGIN), id="another provider origin"),
        pytest.param(callback(), origin_redirect(), id="callback to redirect"),
    ],
)
def test_what_a_declaration_is_about_cannot_change(
    workspace: Workspace, held: ServiceDeclarationFields, attempt: ServiceDeclarationFields
) -> None:
    store = workspace.service_declarations
    created = store.create(held)
    rows, declarations = audit_rows(workspace), declaration_rows(workspace)

    with pytest.raises(ServiceDeclarationIdentityError, match="revoke the declaration"):
        store.update(created.id, attempt)

    assert store.get(created.id) == created
    assert audit_rows(workspace) == rows and declaration_rows(workspace) == declarations


def test_create_update_and_revoke_each_write_one_audit_row(workspace: Workspace) -> None:
    store = workspace.service_declarations
    changes = [
        (external(), external(label="Renamed")),
        (origin_redirect(), origin_redirect(destination=None)),
        (callback(), callback(note="allowed by the provider")),
    ]
    count = 0
    for before, after in changes:
        created = store.create(before)
        count += 1
        assert len(audit_rows(workspace)) == count
        assert store.update(created.id, after) is not None
        count += 1
        assert len(audit_rows(workspace)) == count
        assert store.delete(created.id) is True
        count += 1
        assert len(audit_rows(workspace)) == count

        entries = store.history(declaration_id=created.id)
        assert [entry.action for entry in entries] == ["revoke", "update", "create"]
        assert {entry.kind for entry in entries} == {created.kind}
        assert {entry.subject_id for entry in entries} == {created.id}
        assert [entry.before is None for entry in entries] == [False, False, True]
        assert [entry.after is None for entry in entries] == [True, False, False]


def test_a_revoke_keeps_what_was_stated_and_an_id_is_not_reused(workspace: Workspace) -> None:
    store = workspace.service_declarations
    first = store.create(external())

    assert store.delete(first.id) is True

    assert store.get(first.id) is None and store.list_all() == []
    revoke, create = store.history(declaration_id=first.id)
    assert (revoke.action, create.action) == ("revoke", "create")
    assert revoke.before == stated(first) and revoke.after is None
    assert create.before is None and create.after == stated(first)

    rows = audit_rows(workspace)
    assert store.delete(first.id) is False
    assert store.update(first.id, external()) is None
    assert audit_rows(workspace) == rows

    again = store.create(external())
    assert again.id > first.id
    assert [entry.action for entry in store.history(declaration_id=first.id)] == [
        "revoke",
        "create",
    ]


def test_an_import_is_recorded_as_one_and_a_repeated_import_adds_nothing(
    workspace: Workspace,
) -> None:
    store = workspace.service_declarations

    first = store.create(external(), via="import")
    assert store.create(external(), via="import") == first

    (entry,) = store.history()
    assert (entry.action, entry.via, entry.subject_id) == ("import", "import", first.id)
    assert entry.before is None and entry.after == stated(first)

    provenance = DeclarationProvenance(via="api", note="why", request_group=GROUP)
    store.create(origin_redirect(), provenance=provenance)
    latest = store.history(limit=1)[0]
    assert (latest.action, latest.via, latest.note, latest.request_group) == (
        "create",
        "api",
        "why",
        GROUP,
    )


def test_history_is_newest_first_and_can_be_narrowed(workspace: Workspace) -> None:
    store = workspace.service_declarations
    ext = store.create(external())
    redirect = store.create(origin_redirect())
    store.update(ext.id, external(label="Renamed"))

    assert [(e.kind, e.action) for e in store.history()] == [
        ("external_service", "update"),
        ("redirect", "create"),
        ("external_service", "create"),
    ]
    assert [e.action for e in store.history(declaration_id=ext.id)] == ["update", "create"]
    assert [e.subject_id for e in store.history(kind="redirect")] == [redirect.id]
    assert [e.id for e in store.history(limit=1)] == [3]


def test_the_store_keeps_at_most_the_limit(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert MAX_SERVICE_DECLARATIONS == 500
    monkeypatch.setattr(store_module, "MAX_SERVICE_DECLARATIONS", 3)
    store = workspace.service_declarations
    held = [store.create(external(origin=f"https://svc{n}.example.test")) for n in range(3)]
    rows = audit_rows(workspace)

    with pytest.raises(ServiceDeclarationLimitError, match="at most 3"):
        store.create(external(origin="https://svc9.example.test"))
    with pytest.raises(ServiceDeclarationLimitError):
        store.create(origin_redirect())

    assert store.list_all() == held and audit_rows(workspace) == rows
    # Stating what is held again is not a new declaration, so the limit does not refuse it.
    assert store.create(external(origin="https://svc0.example.test")) == held[0]
    # A revoked declaration frees its place.
    assert store.delete(held[0].id)
    assert store.create(origin_redirect()).fields == origin_redirect()


def test_a_change_and_its_audit_row_land_together_or_not_at_all(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = workspace.service_declarations
    kept = store.create(external())
    rows = audit_rows(workspace)

    def refuse(*_: object, **__: object) -> None:
        raise RuntimeError("the audit row cannot be written")

    monkeypatch.setattr(SqliteServiceDeclarationStore, "_audit", staticmethod(refuse))
    with pytest.raises(RuntimeError):
        store.create(external(origin=LOGIN))
    with pytest.raises(RuntimeError):
        store.update(kept.id, external(label="Renamed"))
    with pytest.raises(RuntimeError):
        store.delete(kept.id)

    assert store.list_all() == [kept]
    assert audit_rows(workspace) == rows


# -- the tables ---------------------------------------------------------------------------------


def test_the_audit_table_refuses_update_and_delete(workspace: Workspace) -> None:
    store = workspace.service_declarations
    store.create(external())
    rows = audit_rows(workspace)
    connection = workspace.database.connection

    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        connection.execute("UPDATE service_declaration_audit SET note = 'rewritten'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        connection.execute("DELETE FROM service_declaration_audit")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        connection.execute("DELETE FROM service_declaration_audit WHERE id = 1")

    assert audit_rows(workspace) == rows
    # Revoking the declaration does not reach the audit rows either.
    assert store.delete(1)
    assert len(audit_rows(workspace)) == len(rows) + 1


def test_the_tables_hold_only_the_kinds_actions_and_origins_they_allow(
    workspace: Workspace,
) -> None:
    connection = workspace.database.connection
    declare = (
        "INSERT INTO service_declarations (kind, identity_key, fields_json, created_at, "
        "updated_at) VALUES (?, ?, '{}', 't', 't')"
    )
    audit = (
        "INSERT INTO service_declaration_audit (at, action, kind, subject_id, subject, via) "
        "VALUES ('t', ?, ?, 1, 's', ?)"
    )

    connection.execute(declare, ("external_service", "https://x.example.test"))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(declare, ("external_service", "https://x.example.test"))
    # Only an external service is unique by its identity.
    connection.execute(declare, ("redirect", "origin https://x.example.test"))
    connection.execute(declare, ("redirect", "origin https://x.example.test"))
    connection.execute(declare, ("permitted_callback", "https://x.example.test"))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(declare, ("bogus", "x"))

    connection.execute(audit, ("create", "redirect", "ui"))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(audit, ("rewrite", "redirect", "ui"))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(audit, ("create", "bogus", "ui"))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(audit, ("create", "redirect", "sync"))
    for via in ("ui", "api", "cli", "import"):
        connection.execute(audit, ("update", "external_service", via))
    connection.rollback()


def test_migration_007_leaves_the_v6_tables_unchanged(workspace: Workspace) -> None:
    """Migration 007 only creates: applied to a version-6 schema holding rows it leaves every
    existing object, and every existing row, as it was, and adds exactly the two tables, their
    indexes and the audit triggers. A fresh index is at user version 7."""
    migrations = load_migrations()
    newest = migrations[-1]
    assert newest.version == SCHEMA_VERSION == 7
    assert [migration.version for migration in migrations] == list(range(1, 8))
    for statement in newest.statements:
        code = "\n".join(
            line for line in statement.splitlines() if not line.lstrip().startswith("--")
        )
        assert code.lstrip().upper().startswith("CREATE "), statement

    old = sqlite3.connect(":memory:")
    for migration in migrations[:-1]:
        for statement in migration.statements:
            old.execute(statement)
    old.execute(
        "INSERT INTO deployment_links (repository, label, url, created_at, updated_at) "
        "VALUES ('r', 'old', 'https://x.example.test', 't', 't')"
    )
    old.execute(
        "INSERT INTO declaration_audit (at, action, kind, subject_id, repository, via) "
        "VALUES ('t', 'create', 'deployment_link', 1, 'r', 'api')"
    )

    def objects() -> dict[tuple[str, str], str | None]:
        found = old.execute("SELECT type, name, sql FROM sqlite_master")
        return {(kind, name): sql for kind, name, sql in found}

    def contents() -> dict[str, list[tuple[object, ...]]]:
        return {
            table: old.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()  # noqa: S608
            for table in ("deployment_links", "declaration_audit")
        }

    before_objects, before_rows = objects(), contents()
    for statement in newest.statements:
        old.execute(statement)
    after_objects = objects()

    assert {key: after_objects[key] for key in before_objects} == before_objects
    assert contents() == before_rows and before_rows["deployment_links"]
    added = {name for (_, name) in after_objects.keys() - before_objects.keys()}
    assert added == {
        "service_declarations",
        "service_declaration_audit",
        "idx_service_declarations_external",
        "idx_service_declarations_kind",
        "idx_service_declaration_audit_subject",
        "service_declaration_audit_no_update",
        "service_declaration_audit_no_delete",
    }
    old.close()

    assert user_version(workspace.database.connection) == SCHEMA_VERSION == 7
    tables = workspace.database.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    assert {"service_declarations", "service_declaration_audit"} <= {r["name"] for r in tables}
    info = workspace.database.execute("PRAGMA table_info(service_declarations)")
    assert [row[1] for row in info] == [
        "id",
        "kind",
        "identity_key",
        "fields_json",
        "created_at",
        "updated_at",
    ]


# -- a sync and the index ------------------------------------------------------------------------


@pytest.fixture
def built(workspace: Workspace, tmp_path: Path) -> Workspace:
    return build(workspace, tmp_path, repos())


def slug(workspace: Workspace, name: str) -> str:
    return next(r.slug for r in workspace.repositories.list_all() if r.name == name)


def test_a_sync_writes_nothing_to_either_audit_table(built: Workspace, tmp_path: Path) -> None:
    """Both audit tables, and the declarations themselves, are as the operator left them after
    a sync that read a changed file and syncs that read none."""
    for item in one_of_each():
        built.service_declarations.create(item)
    link = DeploymentLinkFields(label="prod", url="https://shop.example.test")
    built.deployment_links.create(slug(built, "shop"), link)
    snapshot = (audit_rows(built), link_audit_rows(built), declaration_rows(built))
    assert len(snapshot[0]) == 4 and len(snapshot[1]) == 1

    root = tmp_path / "repos"
    append(root, "shop", "shop/main.py", "\n# touched\n")
    edited = sync(built, built.resolver().resolve(root))
    assert sum(r.changed for r in edited.repositories) >= 1
    for _ in range(2):
        sync(built, built.resolver().resolve(root))

    assert (audit_rows(built), link_audit_rows(built), declaration_rows(built)) == snapshot


def test_a_fresh_index_is_at_version_7_and_two_syncs_change_nothing(
    built: Workspace, tmp_path: Path
) -> None:
    """The schema bump moves no facts version: with declarations of every kind stored, two syncs
    of the unchanged tree report 0 changed files."""
    assert user_version(built.database.connection) == SCHEMA_VERSION == 7
    for item in one_of_each():
        built.service_declarations.create(item)

    root = tmp_path / "repos"
    for _ in range(2):
        report = sync(built, built.resolver().resolve(root))
        assert sum(r.changed for r in report.repositories) == 0, report
