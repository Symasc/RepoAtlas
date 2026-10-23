"""Scoped declarations in the store and the export (Phase A2, slice S3).

A scoped declaration (``DeploymentLinkFields.scope``) names the one file (and symbol) of its one
caller whose unstated base it serves. This file pins what the store keeps of it
(``deployment_links.scope_json``, migration 006), what it refuses, and how it travels in the links
export. The corpus is the one of tests/test_http_connections.py (``shop`` and ``orders`` serve
routes; ``buyer`` and ``tools`` make requests).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from repoatlas.graph.http_scopes import base_key
from repoatlas.models.deployment_targets import (
    DeclarationScope,
    DeploymentLinkFields,
    ExportedLink,
    LinksExport,
    ScopeConflictError,
)
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace
from repoatlas.storage.sqlite.database import SCHEMA_VERSION
from repoatlas.storage.sqlite.migrator import load_migrations, user_version
from tests.http_connections_corpus import repos
from tests.impact_corpus import build

SHOP_URL = "https://shop.example.test"

#: What the A1 export copied from each stored link: every field but ``scope``.
A1_EXPORTED_FIELDS = {
    "label",
    "environment",
    "callers",
    "url",
    "target_id",
    "variables",
    "conditions",
    "verification",
}


@pytest.fixture
def built(workspace: Workspace, tmp_path: Path) -> Workspace:
    return build(workspace, tmp_path, repos())


def slug(workspace: Workspace, name: str) -> str:
    return next(r.slug for r in workspace.repositories.list_all() if r.name == name)


def scope_of(
    path: str = "src/api.py",
    symbol: str | None = "Client",
    base: str = "baseUrl",
    base_path: str = "",
) -> DeclarationScope:
    return DeclarationScope(
        path=path,
        symbol=symbol,
        base_key=base_key(base),
        base_shown="{baseUrl}",
        base_path=base_path,
    )


def scoped(caller: str, label: str = "scoped", **scope_words: str | None) -> DeploymentLinkFields:
    return DeploymentLinkFields(label=label, callers=[caller], scope=scope_of(**scope_words))  # type: ignore[arg-type]


def audit_count(workspace: Workspace) -> int:
    return int(workspace.database.execute("SELECT COUNT(*) FROM declaration_audit").fetchone()[0])


def stored_fields(workspace: Workspace) -> list[tuple[str, dict[str, object]]]:
    """Every link as (repository, what it states), without ids and clocks."""
    return [
        (link.repository, link.model_dump(include=set(DeploymentLinkFields.model_fields)))
        for link in workspace.deployment_links.list_all()
    ]


def a1_export_text(workspace: Workspace) -> str:
    """The export as A1 wrote it: no ``scope`` is copied, the same serialisation call."""
    return (
        LinksExport(
            links=[
                ExportedLink(
                    repository=link.repository,
                    **link.model_dump(include=A1_EXPORTED_FIELDS),
                )
                for link in workspace.deployment_links.list_all()
            ]
        ).model_dump_json(indent=2, exclude_none=True)
        + "\n"
    )


def test_a_scoped_link_round_trips_through_the_store(built: Workspace) -> None:
    """The scope is stored as JSON in ``scope_json`` and read back whole by every read; an
    ordinary link stores NULL and reads back with no scope; stating the same scope twice is one
    link; the history keeps the scope in what it recorded."""
    store = built.deployment_links
    shop, buyer = slug(built, "shop"), slug(built, "buyer")
    fields = scoped(buyer)
    assert fields.scope is not None

    link = store.create(shop, fields)
    assert link.scope == fields.scope and link.callers == [buyer] and link.url is None
    raw = built.database.execute(
        "SELECT scope_json FROM deployment_links WHERE id = ?", (link.id,)
    ).fetchone()[0]
    assert raw == json.dumps(fields.scope.model_dump(), sort_keys=True)
    assert store.get(link.id) is not None and store.get(link.id).scope == fields.scope  # type: ignore[union-attr]
    assert [item.scope for item in store.list(shop)] == [fields.scope]
    assert [item.scope for item in store.list_all()] == [fields.scope]

    assert store.create(shop, fields).id == link.id and len(store.list_all()) == 1

    plain = store.create(shop, DeploymentLinkFields(label="plain", url=SHOP_URL))
    assert plain.scope is None
    assert (
        built.database.execute(
            "SELECT scope_json FROM deployment_links WHERE id = ?", (plain.id,)
        ).fetchone()[0]
        is None
    )
    other = store.create(shop, scoped(buyer, path="src/other.py"))
    assert other.id not in (link.id, plain.id) and other.scope is not None
    assert other.scope.path == "src/other.py"

    after = store.history(link_id=link.id)[0].after
    assert after is not None and after["scope"] == fields.scope.model_dump()


def test_the_store_refuses_overlapping_scoped_links_naming_different_services(
    built: Workspace,
) -> None:
    """One caller's base over one file cannot be two services: another repository, or the same
    repository at another base path, over an equal, enclosing, enclosed or whole-file symbol is
    refused with a message naming the link that holds it. Other files, bases, callers and sibling
    symbols, and the same service again, are not conflicts. A refusal stores and records nothing."""
    store = built.deployment_links
    shop, orders = slug(built, "shop"), slug(built, "orders")
    buyer, tools = slug(built, "buyer"), slug(built, "tools")
    first = store.create(shop, scoped(buyer, symbol="Client"))
    rows = audit_count(built)

    for symbol in ("Client", "Client.get", None):
        with pytest.raises(ScopeConflictError, match=f"link {first.id} already states"):
            store.create(orders, scoped(buyer, symbol=symbol))
    with pytest.raises(ScopeConflictError, match=f"link {first.id} already states"):
        store.create(shop, scoped(buyer, symbol="Client", base_path="/v2"))
    holder = store.create(orders, scoped(buyer, path="src/b.py", symbol=None))
    with pytest.raises(ScopeConflictError, match=f"link {holder.id} already states"):
        store.create(shop, scoped(buyer, path="src/b.py", symbol="Anything.inside"))
    assert len(store.list_all()) == 2 and audit_count(built) == rows + 1

    allowed = [
        (orders, scoped(buyer, path="src/elsewhere.py")),
        (orders, scoped(buyer, base="otherBase")),
        (orders, scoped(tools)),
        (orders, scoped(buyer, symbol="ClientTwo")),
        (shop, scoped(buyer, label="same service, another label")),
    ]
    for repository, fields in allowed:
        assert store.create(repository, fields).id not in (first.id, holder.id)
    assert len(store.list_all()) == 2 + len(allowed)


def test_the_store_refuses_a_scope_change_on_update(built: Workspace) -> None:
    """A stored scope may not change in any word (the base path included), nor be added to an
    ordinary link or removed from a scoped one; the link and its history stay as they were. The
    same scope under another label is an ordinary update."""
    store = built.deployment_links
    shop, buyer = slug(built, "shop"), slug(built, "buyer")
    link = store.create(shop, scoped(buyer))
    plain = store.create(shop, DeploymentLinkFields(label="plain", callers=[buyer]))
    rows = audit_count(built)
    message = "a scope cannot change: revoke the declaration and declare it anew"

    for scope_word in (
        {"base_path": "/v2"},
        {"path": "src/other.py"},
        {"symbol": "Other"},
        {"symbol": None},
        {"base": "otherBase"},
    ):
        with pytest.raises(ScopeConflictError, match=message):
            store.update(link.id, scoped(buyer, **scope_word))  # type: ignore[arg-type]
    with pytest.raises(ScopeConflictError, match=message):
        store.update(link.id, DeploymentLinkFields(label="scoped", callers=[buyer]))
    with pytest.raises(ScopeConflictError, match=message):
        store.update(plain.id, scoped(buyer, label="plain"))
    assert store.get(link.id) == link and store.get(plain.id) == plain
    assert audit_count(built) == rows

    renamed = store.update(link.id, scoped(buyer, label="renamed"))
    assert renamed is not None and renamed.label == "renamed" and renamed.scope == link.scope
    assert audit_count(built) == rows + 1


def test_a_scoped_export_round_trips_and_an_unscoped_export_is_byte_identical(
    built: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a scoped link the file the CLI writes equals the A1 serialisation of the same links
    and has no ``scope`` key, and the CLI says nothing about scopes. With one, the scope is in the
    file, a note on stderr says an older RepoAtlas will refuse it, and importing the file into an
    index without the links stores the same declarations again."""
    from repoatlas.cli.main import app
    from typer.testing import CliRunner

    monkeypatch.setenv("REPOATLAS_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("REPOATLAS_DB", str(built.settings.storage.database))
    store = built.deployment_links
    shop, buyer = slug(built, "shop"), slug(built, "buyer")
    store.create(shop, DeploymentLinkFields(label="prod", url=SHOP_URL))
    store.create(
        shop,
        DeploymentLinkFields(
            label="dev", url="http://localhost:8000", callers=[buyer], environment="dev"
        ),
    )
    store.create(shop, DeploymentLinkFields(label="plain"))
    runner = CliRunner()
    target = tmp_path / "plain.json"

    result = runner.invoke(app, ["links", "export", str(target)])
    assert result.exit_code == 0, result.output
    text = target.read_text()
    assert text == a1_export_text(built)
    assert '"scope"' not in text and "scoped declaration" not in result.output

    scoped_link = store.create(shop, scoped(buyer))
    target = tmp_path / "with_scope.json"
    result = runner.invoke(app, ["links", "export", str(target)])
    assert result.exit_code == 0, result.output
    assert "scoped declaration" in result.output and "older RepoAtlas will refuse" in result.output
    text = target.read_text()
    exported = LinksExport.model_validate_json(text)
    assert [link.scope for link in exported.links] == [None, None, None, scoped_link.scope]
    assert text.count('"scope"') == 1

    before = stored_fields(built)
    for link in store.list_all():
        assert store.delete(link.id)
    result = runner.invoke(app, ["links", "import", str(target)])
    assert result.exit_code == 0, result.output
    assert "Added 4 link(s); 0 already recorded." in result.output
    assert stored_fields(built) == before
    result = runner.invoke(app, ["links", "import", str(target)])
    assert "Added 0 link(s); 4 already recorded." in result.output


def test_migration_006_reparses_nothing(built: Workspace, tmp_path: Path) -> None:
    """What this asserts, and no more. (1) The newest migration is one statement that only adds
    ``deployment_links.scope_json``; applied to a version-5 schema holding a link it leaves that
    link's scope NULL and its other columns as they were. (2) A freshly built index is at user
    version 6 and has the column. (3) Two syncs of the unchanged tree after the build report 0
    changed files: the facts version is untouched by the schema bump, so no file is parsed again.
    It does not open a version-5 index file: SQLite cannot drop the column from a built one."""
    migrations = load_migrations()
    newest = migrations[-1]
    assert newest.version == SCHEMA_VERSION == 6
    assert len(newest.statements) == 1
    assert (
        newest.statements[0]
        .rstrip()
        .endswith("ALTER TABLE deployment_links ADD COLUMN scope_json TEXT;")
    )

    old = sqlite3.connect(":memory:")
    for migration in migrations[:-1]:
        for statement in migration.statements:
            old.execute(statement)
    old.execute(
        "INSERT INTO deployment_links (repository, label, url, created_at, updated_at) "
        "VALUES ('r', 'old', 'https://x.example.test', 't', 't')"
    )
    before = old.execute("SELECT * FROM deployment_links").fetchone()
    old.execute(newest.statements[0])
    after = old.execute("SELECT * FROM deployment_links").fetchone()
    assert after[: len(before)] == before and after[len(before) :] == (None,)
    old.close()

    assert user_version(built.database.connection) == 6
    columns = [row[1] for row in built.database.execute("PRAGMA table_info(deployment_links)")]
    assert "scope_json" in columns

    root = tmp_path / "repos"
    for _ in range(2):
        report = sync(built, built.resolver().resolve(root))
        assert sum(r.changed for r in report.repositories) == 0, report
