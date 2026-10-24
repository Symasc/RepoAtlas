"""Scoped declarations in the store and the export (Phase A2, slice S3).

A scoped declaration (``DeploymentLinkFields.scope``) names the one file (and symbol) of its one
caller whose unstated base it serves. This file pins what the store keeps of it
(``deployment_links.scope_json``, migration 006), what it refuses, and how it travels in the links
export. The corpus is the one of tests/test_http_connections.py (``shop`` and ``orders`` serve
routes; ``buyer`` and ``tools`` make requests).
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from repoatlas.graph import link_exposures
from repoatlas.graph.deploy_identity import ServedRoute
from repoatlas.graph.http_crossings import (
    DEPLOYMENT_KEYS,
    LINKED,
    SERVICE_UNMATCHED,
    UNKNOWN_BASE,
    UNREAD,
    ScopedDeclarations,
    plan_http_crossings,
)
from repoatlas.graph.http_scopes import HTTP_SCOPED_EXPOSURE, FileScope, ScopeSymbol, base_key
from repoatlas.graph.link_exposures import plan_link_exposures, plan_scoped_exposures
from repoatlas.models.deployment_targets import (
    DeclarationScope,
    DeploymentLink,
    DeploymentLinkFields,
    ExportedLink,
    LinksExport,
    ScopeConflictError,
)
from repoatlas.models.parsing import EdgeType, EntityType
from repoatlas.parsers.http_facts import HTTP_CLIENT_CALL, PENDING_FACT
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace
from repoatlas.storage.protocols import FileRow, UnresolvedRefRecord
from repoatlas.storage.sqlite.database import SCHEMA_VERSION
from repoatlas.storage.sqlite.migrator import load_migrations, user_version
from tests.http_connections_corpus import repos
from tests.http_scoped_corpus import LONG_BASE
from tests.http_scoped_corpus import repos as scoped_repos
from tests.impact_corpus import build
from tests.incremental_corpus import sync_all

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


# -- Phase A2, slice S4: the sync joins a request only where a declaration covers it --------------
#
# The corpus is tests/http_scoped_corpus.py: ``alpha`` and ``beta`` serve the same two routes,
# ``twin`` serves one of them twice; ``caller`` (and ``second``, ``webts``) make requests on bases
# the source never states. Declarations are made with ``via="import"``: the sync alone decides them.

NOW = datetime.now(timezone.utc)
Key = tuple[str, str, str]
CLIENT = ("caller", "client.py")


class Decided(NamedTuple):
    """One stored decision of the join, with the request's repository, file and caller."""

    decision: str
    evidence: dict[str, Any]
    chain: list[dict[str, Any]]
    url: str

    @property
    def hops(self) -> dict[str, str]:
        return {hop["kind"]: hop["status"] for hop in self.chain}


@pytest.fixture
def scoped_built(workspace: Workspace, tmp_path: Path) -> Workspace:
    return build(workspace, tmp_path, scoped_repos())


def words(path: str, text: str, symbol: str | None = None, base_path: str = "") -> DeclarationScope:
    """The scope an operator states for the base ``text``: shown by name when that is a short plain
    name, else masked."""
    shown = f"{{{text}}}" if re.fullmatch(r"[A-Za-z_$][\w$]{0,39}", text) else "{…}"
    return DeclarationScope(
        path=path, symbol=symbol, base_key=base_key(text), base_shown=shown, base_path=base_path
    )


def declare(
    workspace: Workspace,
    service: str,
    caller: str,
    path: str,
    text: str,
    *,
    symbol: str | None = None,
    base_path: str = "",
    label: str = "scoped",
) -> int:
    fields = DeploymentLinkFields(
        label=label, callers=[slug(workspace, caller)], scope=words(path, text, symbol, base_path)
    )
    return workspace.deployment_links.create(slug(workspace, service), fields, via="import").id


def insert_raw(workspace: Workspace, service: str, caller: str, scope: DeclarationScope) -> int:
    """A declaration the store would refuse (a second service over one scope), as an old or
    hand-edited index could hold it."""
    now = datetime.now(timezone.utc).isoformat()
    with workspace.database.transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO deployment_links (repository, label, callers_json, scope_json, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                slug(workspace, service),
                "raw",
                json.dumps([slug(workspace, caller)]),
                json.dumps(scope.model_dump(), sort_keys=True),
                now,
                now,
            ),
        )
        return int(cursor.lastrowid or 0)


def resync(workspace: Workspace, tmp_path: Path) -> None:
    sync_all(workspace, tmp_path / "repos")


def decided(workspace: Workspace) -> dict[Key, Decided]:
    """Every decision by (repository name, file, the qualified name of the entity that asks)."""
    rows = workspace.database.execute(
        """SELECT r.name, f.rel_path, e.qualified_name, d.url, d.decision, d.evidence_json,
                  d.chain_json
           FROM http_call_decisions d
           JOIN entities e ON e.id = d.src_id
           JOIN files f ON f.id = d.file_id
           JOIN repositories r ON r.id = d.repo_id"""
    ).fetchall()
    return {
        (name, path, caller): Decided(decision, json.loads(evidence), json.loads(chain), url)
        for name, path, caller, url, decision, evidence, chain in rows
    }


def crossings(workspace: Workspace) -> list[tuple[str, str, str, dict[str, Any]]]:
    """(caller, handler, handler's file, the edge's ``http`` record) of every joined request."""
    rows = workspace.database.execute(
        """SELECT s.qualified_name, d.qualified_name, df.rel_path, e.resolution
           FROM edges e
           JOIN entities s ON s.id = e.src_id
           JOIN entities d ON d.id = e.dst_id
           LEFT JOIN files df ON df.id = d.file_id
           WHERE e.type = ?
           ORDER BY s.qualified_name""",
        (str(EdgeType.CALLS_HTTP_HANDLER),),
    ).fetchall()
    return [
        (caller, handler, path, json.loads(text)["http"]) for caller, handler, path, text in rows
    ]


def decision_table(workspace: Workspace) -> list[tuple[str, str, str, str]]:
    """Every decision as stored, by its ref: no row id is in a refusal."""
    rows = workspace.database.execute(
        "SELECT decision_ref, decision, evidence_json, chain_json "
        "FROM http_call_decisions ORDER BY decision_ref"
    ).fetchall()
    return [(ref, decision, evidence, chain) for ref, decision, evidence, chain in rows]


def untouched(before: dict[Key, Decided], after: dict[Key, Decided], *joined: Key) -> None:
    """Every decision but the ones named is what it was: the same decision, evidence and chain."""
    assert before.keys() == after.keys()
    for key, old in before.items():
        if key not in joined:
            assert after[key] == old, key


def test_a_scoped_declaration_joins_only_requests_written_on_its_base_expression(
    scoped_built: Workspace, tmp_path: Path
) -> None:
    """Three requests read the module binding ``BASE``: the declaration for that base in that file
    joins the two the service has a handler for (method and route), and the third, which asks for a
    method it does not answer, is refused ``route-unmatched`` with the source's chain. A request on
    any other base expression of the same file (``self.base_url``, a parameter ``base``, ``root``)
    stays unknown with no mention of the declaration; so does every request of every other file and
    repository. A parameter that shadows ``BASE`` writes the same text, so it is concerned and
    refused ``scope-too-broad``."""
    ws = scoped_built
    before = decided(ws)
    read, write = (*CLIENT, "read_item"), (*CLIENT, "write_item")
    listing = (*CLIENT, "list_items")
    sh = (*CLIENT, "shadowed_read")
    for key in (
        read,
        write,
        listing,
        sh,
        (*CLIENT, "param_read"),
        (*CLIENT, "UsersClient.get_item"),
    ):
        assert before[key].decision == UNKNOWN_BASE, key
    link = declare(ws, "alpha", "caller", "client.py", "BASE")
    resync(ws, tmp_path)

    after = decided(ws)
    for key in (read, write):
        assert after[key].decision == LINKED, key
        assert after[key].evidence["link_id"] == str(link)
    assert after[listing].decision == UNKNOWN_BASE and after[listing].chain == before[listing].chain
    assert after[listing].evidence["declaration_outcome"] == "route-unmatched"
    assert after[listing].evidence["declaration_ids"] == str(link)
    assert after[listing] == before[listing]._replace(
        evidence={
            **before[listing].evidence,
            "declaration_ids": str(link),
            "declaration_outcome": "route-unmatched",
        }
    )
    assert after[sh] == before[sh]._replace(
        evidence={
            **before[sh].evidence,
            "declaration_ids": str(link),
            "declaration_outcome": "scope-too-broad",
        }
    )
    untouched(before, after, read, write, listing, sh)
    assert [(c, h, p) for c, h, p, _ in crossings(ws)] == [
        ("read_item", "get_item", "alpha/main.py"),
        ("write_item", "create_item", "alpha/main.py"),
    ]
    for key in (
        (*CLIENT, "param_read"),
        (*CLIENT, "prefixed_read"),
        (*CLIENT, "UsersClient.get_item"),
    ):
        assert after[key].decision == UNKNOWN_BASE
        assert "declaration_ids" not in after[key].evidence, key


def test_a_scoped_declaration_never_joins_a_request_in_another_file(
    scoped_built: Workspace, tmp_path: Path
) -> None:
    """``other.py`` makes a request on the same text, under the same module binding, as
    ``client.py``. A declaration for one file never reaches the other: each file needs its own."""
    ws = scoped_built
    before = decided(ws)
    other = ("caller", "other.py", "other_read")
    first = declare(ws, "alpha", "caller", "client.py", "BASE")
    resync(ws, tmp_path)
    after = decided(ws)
    assert after[other] == before[other] and after[other].decision == UNKNOWN_BASE
    assert after[(*CLIENT, "read_item")].decision == LINKED

    second = declare(ws, "alpha", "caller", "other.py", "BASE")
    resync(ws, tmp_path)
    final = decided(ws)
    assert final[other].decision == LINKED and final[other].evidence["link_id"] == str(second)
    assert final[(*CLIENT, "read_item")].evidence["link_id"] == str(first)
    sh, listing = (*CLIENT, "shadowed_read"), (*CLIENT, "list_items")
    assert final[sh] == before[sh]._replace(
        evidence={
            **before[sh].evidence,
            "declaration_ids": str(first),
            "declaration_outcome": "scope-too-broad",
        }
    )
    assert final[listing] == before[listing]._replace(
        evidence={
            **before[listing].evidence,
            "declaration_ids": str(first),
            "declaration_outcome": "route-unmatched",
        }
    )
    untouched(
        before,
        final,
        other,
        (*CLIENT, "read_item"),
        (*CLIENT, "write_item"),
        listing,
        sh,
    )


def test_a_symbol_scope_covers_its_members_and_not_a_symbol_sharing_its_prefix(
    scoped_built: Workspace, tmp_path: Path
) -> None:
    """``UsersClient`` covers ``UsersClient.get_item`` and ``.make_item`` (a receiver base needs a
    symbol), and not ``UsersClientV2.get_item``, whose name only starts like it. The same receiver
    declared for the whole file is too broad: it covers nothing and says so on the request it
    concerns, and the class's own declaration keeps joining its members."""
    ws = scoped_built
    before = decided(ws)
    members = [(*CLIENT, "UsersClient.get_item"), (*CLIENT, "UsersClient.make_item")]
    sibling = (*CLIENT, "UsersClientV2.get_item")
    for key in (*members, sibling):
        assert before[key].decision == UNKNOWN_BASE, key
    by_class = declare(ws, "alpha", "caller", "client.py", "self.base_url", symbol="UsersClient")
    resync(ws, tmp_path)

    after = decided(ws)
    for key in members:
        assert after[key].decision == LINKED and after[key].evidence["link_id"] == str(by_class)
    untouched(before, after, *members)
    assert {caller for caller, *_ in crossings(ws)} == {
        "UsersClient.get_item",
        "UsersClient.make_item",
    }

    whole = declare(ws, "alpha", "caller", "client.py", "self.base_url")
    resync(ws, tmp_path)
    final = decided(ws)
    assert final[sibling].decision == UNKNOWN_BASE and final[sibling].chain == before[sibling].chain
    assert final[sibling].evidence["declaration_outcome"] == "scope-too-broad"
    assert final[sibling].evidence["declaration_ids"] == str(whole)
    for key in members:
        assert final[key].evidence["link_id"] == str(by_class)


def test_a_scoped_declaration_never_joins_another_repositorys_request(
    scoped_built: Workspace, tmp_path: Path
) -> None:
    """``second`` has a file of the same path making the same ``BASE`` request. A declaration names
    its one caller: it joins that repository's request and not the other's, which needs its own."""
    ws = scoped_built
    before = decided(ws)
    theirs = ("second", "client.py", "read_item")
    mine = declare(ws, "alpha", "caller", "client.py", "BASE")
    resync(ws, tmp_path)
    after = decided(ws)
    assert after[(*CLIENT, "read_item")].decision == LINKED
    assert after[theirs] == before[theirs] and after[theirs].decision == UNKNOWN_BASE

    other = declare(ws, "alpha", "second", "client.py", "BASE")
    resync(ws, tmp_path)
    final = decided(ws)
    assert final[theirs].decision == LINKED and final[theirs].evidence["link_id"] == str(other)
    assert final[(*CLIENT, "read_item")].evidence["link_id"] == str(mine)


def test_a_scoped_declaration_never_touches_a_request_whose_origin_is_stated_or_unread(
    scoped_built: Workspace, tmp_path: Path
) -> None:
    """A request that states its origin, one the join does not read, and requests whose base text
    cannot be compared (cut at 60 characters, or a host written ``http://{host}``) are left as they
    are: the first two are not looked at, and the last two are named ``scope-unidentified``."""
    ws = scoped_built
    before = decided(ws)
    stated = ("caller", "odd.py", "stated_read")
    assert before[stated].decision not in (LINKED, UNKNOWN_BASE)
    first = declare(ws, "alpha", "caller", "odd.py", LONG_BASE)
    second = declare(ws, "alpha", "caller", "odd.py", "host")
    resync(ws, tmp_path)

    after = decided(ws)
    assert after[stated] == before[stated]
    for name in ("long_read", "host_read"):
        key = ("caller", "odd.py", name)
        assert before[key].decision == UNKNOWN_BASE
        assert after[key].decision == UNKNOWN_BASE and after[key].chain == before[key].chain
        assert after[key].evidence["declaration_outcome"] == "scope-unidentified"
        assert after[key].evidence["declaration_ids"] == f"{first},{second}"
    assert crossings(ws) == []

    # The same through the join itself: a stated origin and an unread claim are not looked at,
    # beside a request the declaration does cover.
    web = FileRow(
        file_id=1,
        file_entity_id=100,
        repo_id=1,
        repo_name="web",
        repo_slug="web",
        repo_path=Path("/web"),
        rel_path="client.py",
    )
    api = web.model_copy(
        update={
            "file_id": 2,
            "file_entity_id": 200,
            "repo_id": 2,
            "repo_name": "api",
            "repo_slug": "api",
            "rel_path": "api.py",
        }
    )
    link = DeploymentLink(
        id=7,
        repository="api",
        label="scoped",
        callers=["web"],
        scope=words("client.py", "BASE"),
        created_at=NOW,
        updated_at=NOW,
    )
    route = ServedRoute(2, 2, 21, "GET /v1/items", 3, 4, frozenset())
    exposures = plan_scoped_exposures([link], {2: [route]}, {"web": 1, "api": 2})
    file_scope = FileScope.build(
        [
            ScopeSymbol(100, 1, "file", "client.py"),
            ScopeSymbol(50, 1, "symbol", "BASE"),
            ScopeSymbol(11, 1, "function", "f"),
        ],
        [(11, 50)],
    )

    def asked(line: int, url: str, **meta: str) -> UnresolvedRefRecord:
        return UnresolvedRefRecord(
            repo_id=1,
            file_id=1,
            src_id=11,
            edge_type=EdgeType.CALLS_HTTP_ENDPOINT,
            target_kind=EntityType.HTTP_ENDPOINT,
            target_key=f"GET {url}",
            line_start=line,
            line_end=line,
            meta_json=json.dumps({PENDING_FACT: HTTP_CLIENT_CALL, "method": "GET", **meta}),
        )

    plan = plan_http_crossings(
        [
            asked(5, "{BASE}/v1/items", segments='["v1", "items"]', base="unknown"),
            asked(6, "{BASE}/", segments="[]", base="unknown"),
            asked(
                7,
                "https://items.example.test/v1/items",
                segments='["v1", "items"]',
                origin="https://items.example.test",
            ),
        ],
        [web, api],
        producer="test",
        scopes=ScopedDeclarations(exposures, {("web", "client.py"): file_scope}),
    )
    by_line = {call.claim.line_start: call for call in plan.calls}
    assert by_line[5].decision == LINKED
    assert by_line[6].decision == UNREAD and by_line[7].decision == SERVICE_UNMATCHED
    for line in (6, 7):
        assert "declaration_ids" not in by_line[line].evidence
        assert by_line[line].edge is None
    assert len(plan.edges) == 1 and plan.edges[0].src_id == 11


def test_a_scoped_declaration_is_never_proof_in_any_hop(
    scoped_built: Workspace, tmp_path: Path
) -> None:
    """A joined request's origin, service identity, deployment binding and source binding are
    ``declared`` at the declaration (never a file, never ``proven``); only the caller, the route and
    the handler rest on source. The edge says the identity came from a link and carries none of
    what a CI-established deployment or a declared identity adds."""
    ws = scoped_built
    link = declare(ws, "alpha", "caller", "client.py", "BASE", label="the items api")
    resync(ws, tmp_path)

    row = decided(ws)[(*CLIENT, "read_item")]
    assert [hop["kind"] for hop in row.chain] == [
        "caller",
        "origin",
        "service_identity",
        "deployment_binding",
        "source_binding",
        "route",
        "handler",
    ]
    assert row.hops == {
        "caller": "proven",
        "origin": "declared",
        "service_identity": "declared",
        "deployment_binding": "declared",
        "source_binding": "declared",
        "route": "proven",
        "handler": "proven",
    }
    place = f"@deployment-links/{link}"
    for hop in row.chain:
        paths = [(p["path"], p["role"], p.get("basis")) for p in hop.get("provenance", [])]
        if hop["status"] == "declared":
            assert paths == [(place, "link_in", "link")], hop
        else:
            assert place not in [path for path, _, _ in paths], hop
    origin = next(hop for hop in row.chain if hop["kind"] == "origin")
    assert origin["detail"] == "the items api: {BASE} in client.py"

    [(_, _, _, record)] = [c for c in crossings(ws) if c[0] == "read_item"]
    assert record["identity_source"] == "deployment-link"
    assert (record["link_id"], record["link_label"]) == (str(link), "the items api")
    assert (record["link_verification"], record["link_scope"]) == ("unverified", "client.py")
    for key in (*DEPLOYMENT_KEYS, "identity_in", "bound_in", "scheme_in", "backend_in"):
        assert key not in record, key
    assert "link_environment" not in record


def test_two_scoped_declarations_naming_different_services_join_nothing(
    scoped_built: Workspace, tmp_path: Path
) -> None:
    """Two declarations cover one request and name two services: the store refuses that, an index
    that holds it (a hand-edited one) joins nothing and says ``ambiguous`` on both requests, naming
    both declarations. Revoking one makes the other join."""
    ws = scoped_built
    before = decided(ws)
    first = declare(ws, "alpha", "caller", "client.py", "BASE")
    with pytest.raises(ScopeConflictError):
        declare(ws, "beta", "caller", "client.py", "BASE")
    second = insert_raw(ws, "beta", "caller", words("client.py", "BASE"))
    resync(ws, tmp_path)

    after = decided(ws)
    for name in ("read_item", "write_item"):
        key = (*CLIENT, name)
        assert after[key].decision == UNKNOWN_BASE and after[key].chain == before[key].chain
        assert after[key].evidence["declaration_outcome"] == "ambiguous"
        assert after[key].evidence["declaration_ids"] == f"{first},{second}"
    assert crossings(ws) == []

    assert ws.deployment_links.delete(second)
    resync(ws, tmp_path)
    final = decided(ws)
    assert final[(*CLIENT, "read_item")].decision == LINKED
    assert final[(*CLIENT, "read_item")].evidence["link_id"] == str(first)


def test_a_scoped_declaration_on_a_service_answering_twice_joins_nothing(
    scoped_built: Workspace, tmp_path: Path
) -> None:
    """``twin`` serves ``GET /v1/items/{item_id}`` from two files: two handlers, no join
    (``ambiguous``). It serves no ``POST /v1/items``: ``route-unmatched``. Both stay unknown with
    the source's chain; a service that answers once joins the same request."""
    ws = scoped_built
    before = decided(ws)
    twin = declare(ws, "twin", "caller", "client.py", "BASE")
    resync(ws, tmp_path)

    after = decided(ws)
    read, write = after[(*CLIENT, "read_item")], after[(*CLIENT, "write_item")]
    assert (read.decision, write.decision) == (UNKNOWN_BASE, UNKNOWN_BASE)
    assert read.evidence["declaration_outcome"] == "ambiguous"
    assert write.evidence["declaration_outcome"] == "route-unmatched"
    assert read.evidence["declaration_ids"] == write.evidence["declaration_ids"] == str(twin)
    assert read.chain == before[(*CLIENT, "read_item")].chain
    assert write.chain == before[(*CLIENT, "write_item")].chain
    assert crossings(ws) == []

    assert ws.deployment_links.delete(twin)
    declare(ws, "alpha", "caller", "client.py", "BASE")
    resync(ws, tmp_path)
    assert decided(ws)[(*CLIENT, "read_item")].decision == LINKED


def test_the_most_specific_covering_declaration_is_the_one_that_joins(
    scoped_built: Workspace, tmp_path: Path
) -> None:
    """When several declarations of one service cover a request, a symbol outranks the whole file,
    a longer symbol outranks a shorter, and then the lower id wins. The edge names the winner's
    words. Each earlier-made (lower id) declaration is the less specific one, so id alone cannot
    be what decides."""
    ws = scoped_built
    whole = declare(ws, "alpha", "caller", "client.py", "BASE")
    by_symbol = declare(ws, "alpha", "caller", "client.py", "BASE", symbol="read_item")
    again = declare(ws, "alpha", "caller", "client.py", "BASE", label="again")
    by_class = declare(ws, "alpha", "caller", "client.py", "self.base_url", symbol="UsersClient")
    by_method = declare(
        ws, "alpha", "caller", "client.py", "self.base_url", symbol="UsersClient.get_item"
    )
    resync(ws, tmp_path)

    after = decided(ws)
    won = {
        "read_item": by_symbol,
        "write_item": whole,
        "UsersClient.get_item": by_method,
        "UsersClient.make_item": by_class,
    }
    assert whole < by_symbol < again < by_class < by_method
    for name, link in won.items():
        assert after[(*CLIENT, name)].decision == LINKED, name
        assert after[(*CLIENT, name)].evidence["link_id"] == str(link), name
    scopes = {caller: record["link_scope"] for caller, _, _, record in crossings(ws)}
    assert scopes == {
        "read_item": "client.py#read_item",
        "write_item": "client.py",
        "UsersClient.get_item": "client.py#UsersClient.get_item",
        "UsersClient.make_item": "client.py#UsersClient",
    }
    assert after[(*CLIENT, "UsersClientV2.get_item")].decision == UNKNOWN_BASE


def test_a_scoped_declaration_with_a_base_path_matches_the_prefixed_route(
    scoped_built: Workspace, tmp_path: Path
) -> None:
    """The base path is the prefix the declared service mounts its routes under: ``mounted`` serves
    ``/items/{item_id}`` and ``/items``, so with ``/v1`` it serves ``/v1/items/{item_id}``. The
    request is matched as it writes its path, never with the prefix added or removed: the request
    written ``/v1/items/{item_id}`` joins, with the deployment ``{base}/v1``, its own URL and the
    endpoint the service gives; the request written ``/items/{item_id}`` stays unknown and is
    refused ``route-unmatched`` with the source's chain. A declaration with no base path joins the
    requests written as the service serves them."""
    ws = scoped_built
    before = decided(ws)
    prefixed = declare(
        ws, "mounted", "caller", "client.py", "base", symbol="param_read", base_path="/v1"
    )
    unprefixed = declare(
        ws, "mounted", "caller", "client.py", "root", symbol="prefixed_read", base_path="/v1"
    )
    plain_get = declare(ws, "mounted", "caller", "client.py", "plain", symbol="plain_read")
    plain_post = declare(ws, "mounted", "caller", "client.py", "plain", symbol="plain_write")
    resync(ws, tmp_path)

    after = decided(ws)
    joined = {"param_read": prefixed, "plain_read": plain_get, "plain_write": plain_post}
    for name, link in joined.items():
        assert before[(*CLIENT, name)].decision == UNKNOWN_BASE, name
        assert after[(*CLIENT, name)].decision == LINKED, name
        assert after[(*CLIENT, name)].evidence["link_id"] == str(link), name
    refused = after[(*CLIENT, "prefixed_read")]
    assert refused.decision == UNKNOWN_BASE
    assert refused.chain == before[(*CLIENT, "prefixed_read")].chain
    assert refused.evidence["declaration_outcome"] == "route-unmatched"
    assert refused.evidence["declaration_ids"] == str(unprefixed)
    untouched(before, after, *((*CLIENT, name) for name in (*joined, "prefixed_read")))

    records = {c: (h, p, r) for c, h, p, r in crossings(ws)}
    assert set(records) == set(joined)
    handler, path, record = records["param_read"]
    assert (handler, path) == ("get_item", "mounted/main.py")
    assert record["deployment"] == "{base}/v1"
    assert record["url"] == "{base}/v1/items/{item_id}"
    assert record["endpoint"] == "GET {base}/v1/items/{item_id}"
    for name, url, endpoint in (
        ("plain_read", "{plain}/items/{item_id}", "GET {plain}/items/{item_id}"),
        ("plain_write", "{plain}/items", "POST {plain}/items"),
    ):
        record = records[name][2]
        assert (record["deployment"], record["url"], record["endpoint"]) == (
            "{plain}",
            url,
            endpoint,
        )


def test_a_base_path_matches_whole_segments_and_is_never_added_to_the_request(
    scoped_built: Workspace, tmp_path: Path
) -> None:
    """With ``/v1`` as the base path of ``mounted``, a request that writes the prefix twice
    (``/v1/v1/items/{item_id}``) is not at a route the service serves, and neither is one that
    writes ``/v10/items/{item_id}``: the prefix covers whole segments only. Both stay unknown with
    the source's chain and say ``route-unmatched``; nothing is joined."""
    ws = scoped_built
    before = decided(ws)
    doubled = declare(
        ws, "mounted", "caller", "client.py", "doubled", symbol="doubled_read", base_path="/v1"
    )
    longer = declare(
        ws, "mounted", "caller", "client.py", "longer", symbol="longer_read", base_path="/v1"
    )
    resync(ws, tmp_path)

    after = decided(ws)
    for name, link in (("doubled_read", doubled), ("longer_read", longer)):
        key = (*CLIENT, name)
        assert before[key].decision == UNKNOWN_BASE, name
        assert after[key].decision == UNKNOWN_BASE and after[key].chain == before[key].chain, name
        assert after[key].evidence["declaration_outcome"] == "route-unmatched", name
        assert after[key].evidence["declaration_ids"] == str(link), name
    assert crossings(ws) == []


def test_a_scoped_declaration_on_an_injected_transport_request_joins_nothing(
    scoped_built: Workspace, tmp_path: Path
) -> None:
    """A request made by a function that is handed its transport and base in options is derived per
    root, so one declaration cannot say which root it speaks of: whole-file or by symbol, it joins
    nothing, and the request says ``injected-root-unselected``."""
    ws = scoped_built
    before = decided(ws)
    key = ("webts", "src/root.ts", "unstated")
    assert before[key].decision == UNKNOWN_BASE and "injected_at" in before[key].evidence
    whole = declare(ws, "alpha", "webts", "src/root.ts", "base")
    named = declare(ws, "alpha", "webts", "src/root.ts", "base", symbol="unstated")
    resync(ws, tmp_path)

    after = decided(ws)
    assert after[key].decision == UNKNOWN_BASE and after[key].chain == before[key].chain
    assert after[key].evidence["declaration_outcome"] == "injected-root-unselected"
    assert after[key].evidence["declaration_ids"] == f"{whole},{named}"
    assert after[key].evidence["injected_at"] == before[key].evidence["injected_at"]
    assert crossings(ws) == []


def test_a_file_scoped_bare_name_joins_only_where_the_module_binding_is_used(
    scoped_built: Workspace, tmp_path: Path
) -> None:
    """A bare name for the whole file covers a request only when the request's own code is proven
    to use the file's one binding of that name. In Python a parameter ``BASE`` hides the module's;
    in TypeScript so does a parameter. The shadowing request stays unknown and says
    ``scope-too-broad``; the requests that read the binding join."""
    ws = scoped_built
    before = decided(ws)
    py = declare(ws, "alpha", "caller", "client.py", "BASE")
    ts = declare(ws, "alpha", "webts", "src/api.ts", "BASE")
    resync(ws, tmp_path)

    after = decided(ws)
    api = ("webts", "src/api.ts")
    joined = [(*CLIENT, "read_item"), (*CLIENT, "write_item")]
    joined += [(*api, "listItem"), (*api, "createItem")]
    for key in joined:
        assert before[key].decision == UNKNOWN_BASE, key
        assert after[key].decision == LINKED, key
    assert after[(*CLIENT, "read_item")].evidence["link_id"] == str(py)
    assert after[(*api, "listItem")].evidence["link_id"] == str(ts)
    for key, link in (((*CLIENT, "shadowed_read"), py), ((*api, "shadowedItem"), ts)):
        assert before[key].decision == UNKNOWN_BASE, key
        assert after[key].decision == UNKNOWN_BASE and after[key].chain == before[key].chain
        assert after[key].evidence["declaration_outcome"] == "scope-too-broad", key
        assert after[key].evidence["declaration_ids"] == str(link), key
    untouched(
        before,
        after,
        *joined,
        (*CLIENT, "list_items"),
        (*CLIENT, "shadowed_read"),
        (*api, "shadowedItem"),
    )


def test_a_file_scoped_qualified_expression_needs_the_same_binding_evidence(
    scoped_built: Workspace, tmp_path: Path
) -> None:
    """``config.api_base`` is covered for the file only where the request's code uses the module's
    ``config``: the function that reads it joins, and the function with a parameter ``config``,
    which writes the same text, does not."""
    ws = scoped_built
    before = decided(ws)
    link = declare(ws, "alpha", "caller", "qualified.py", "config.api_base")
    resync(ws, tmp_path)

    after = decided(ws)
    bound = ("caller", "qualified.py", "qualified_read")
    hidden = ("caller", "qualified.py", "shadowed_qualified")
    assert before[bound].decision == before[hidden].decision == UNKNOWN_BASE
    assert after[bound].decision == LINKED and after[bound].evidence["link_id"] == str(link)
    assert after[hidden].decision == UNKNOWN_BASE
    assert after[hidden].chain == before[hidden].chain
    assert after[hidden].evidence["declaration_outcome"] == "scope-too-broad"
    [record] = [r for c, _, _, r in crossings(ws) if c == "qualified_read"]
    assert record["url"] == "{…}/v1/items/{item_id}"


def test_an_imported_scoped_declaration_is_decided_by_the_sync_alone(
    scoped_built: Workspace, tmp_path: Path
) -> None:
    """A declaration that arrives by import has no worklist group and no preview behind it: it is
    recorded as an import, nothing changes until the next sync, and that sync decides it from the
    declaration's own words and the index alone."""
    ws = scoped_built
    before = decided(ws)
    link = ws.deployment_links.create(
        slug(ws, "alpha"),
        DeploymentLinkFields(
            label="imported",
            callers=[slug(ws, "caller")],
            scope=words("client.py", "BASE"),
        ),
        via="import",
    )
    [entry] = ws.deployment_links.history(link_id=link.id)
    assert (entry.action, entry.via, entry.request_group) == ("import", "import", None)
    assert decided(ws) == before

    resync(ws, tmp_path)
    after = decided(ws)
    assert after[(*CLIENT, "read_item")].decision == LINKED
    assert after[(*CLIENT, "read_item")].evidence["link_id"] == str(link.id)
    assert len(ws.deployment_links.history(link_id=link.id)) == 1


def test_a_sync_without_scoped_declarations_is_unchanged(
    scoped_built: Workspace, tmp_path: Path
) -> None:
    """Without a scoped declaration that concerns a request, a sync decides every request exactly
    as it did: an ordinary URL link, declarations about a file that is not there, a base no request
    reads, a caller or a service that is not indexed -- none changes a stored decision, none adds a
    word of evidence -- and revoking them all restores the same table."""
    ws = scoped_built
    before = decision_table(ws)
    assert all("declaration" not in evidence for _, _, evidence, _ in before)
    assert not any(decision == LINKED for _, decision, _, _ in before)
    resync(ws, tmp_path)
    assert decision_table(ws) == before

    alpha, caller = slug(ws, "alpha"), slug(ws, "caller")
    store = ws.deployment_links
    scope = words("client.py", "BASE")
    plain = DeploymentLinkFields(label="plain", url="https://alpha.example.test")
    # a service that is not indexed, and a caller that is not
    no_service = DeploymentLinkFields(label="gone", callers=[caller], scope=scope)
    no_caller = DeploymentLinkFields(label="ghost", callers=["ghost"], scope=scope)
    made = [
        store.create(alpha, plain).id,
        declare(ws, "alpha", "caller", "missing.py", "BASE"),
        declare(ws, "alpha", "caller", "client.py", "nothing_reads_this"),
        store.create("nowhere", no_service).id,
        store.create(alpha, no_caller).id,
    ]
    resync(ws, tmp_path)
    assert decision_table(ws) == before
    assert crossings(ws) == []

    for link in made:
        assert ws.deployment_links.delete(link)
    resync(ws, tmp_path)
    assert decision_table(ws) == before


def test_a_scoped_crossing_records_no_base_expression_text(
    scoped_built: Workspace, tmp_path: Path
) -> None:
    """The declaration keeps the base by hash and shows it as ``{…}`` unless it is a plain name.
    What a join records -- the edge, the decision's evidence and its chain -- shows the same: the
    URL is the shown base and the path as written, and the text of ``self.base_url`` is nowhere."""
    ws = scoped_built
    link = declare(ws, "alpha", "caller", "client.py", "self.base_url", symbol="UsersClient")
    resync(ws, tmp_path)

    joined = [
        (key, row)
        for key, row in decided(ws).items()
        if row.decision == LINKED and key[2].startswith("UsersClient.")
    ]
    assert {key[2] for key, _ in joined} == {"UsersClient.get_item", "UsersClient.make_item"}
    records = [r for c, _, _, r in crossings(ws) if c.startswith("UsersClient.")]
    assert {r["url"] for r in records} == {"{…}/v1/items/{item_id}", "{…}/v1/items"}
    assert {r["deployment"] for r in records} == {"{…}"}
    texts = [json.dumps(record, sort_keys=True) for record in records]
    for _, row in joined:
        texts += [json.dumps(row.evidence, sort_keys=True), json.dumps(row.chain, sort_keys=True)]
    raw_edges = ws.database.execute(
        "SELECT resolution FROM edges WHERE type = ?", (str(EdgeType.CALLS_HTTP_HANDLER),)
    ).fetchall()
    texts += [row[0] for row in raw_edges]
    stored = ws.database.execute(
        "SELECT scope_json FROM deployment_links WHERE id = ?", (link,)
    ).fetchone()[0]
    history = [entry.model_dump(mode="json") for entry in ws.deployment_links.history()]
    texts += [stored, json.dumps(history)]
    for text in texts:
        assert "base_url" not in text and "self." not in text, text


def test_plan_scoped_exposures_states_each_route_with_the_words_of_the_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One claim per route of the serving repository, with the keys a link's exposure carries and
    the words of the scope, the shown base and the path prefix; a link whose service or caller is
    not indexed, with no scope, or with a route no line states adds none; the number is capped.
    The prefix is the service's: the target is the shown base, the prefix and the route as the
    code serves it, and ``route`` stays that route alone."""
    link = DeploymentLink(
        id=7,
        repository="api",
        label="the api",
        environment="staging",
        callers=["web"],
        scope=words("client.py", "BASE", "read_item", "/api/v1"),
        created_at=NOW,
        updated_at=NOW,
    )
    routes = [
        ServedRoute(2, 2, 21, "GET /items/{item_id}", 3, 4, frozenset()),
        ServedRoute(2, 2, 22, "POST /items", None, None, frozenset(), declared_at=9),
        ServedRoute(2, 2, 23, "GET /lost", None, None, frozenset()),
    ]
    ids = {"web": 1, "api": 2}
    claims = plan_scoped_exposures([link], {2: routes}, ids)
    # The target is the shown base, the service's route prefix and the route as the code serves it.
    assert [c.target_key for c in claims] == [
        "GET {BASE}/api/v1/items/{item_id}",
        "POST {BASE}/api/v1/items",
    ]
    meta = json.loads(claims[0].meta_json or "{}")
    assert meta == {
        PENDING_FACT: HTTP_SCOPED_EXPOSURE,
        "method": "GET",
        "route": "/items/{item_id}",
        "deployment": "{BASE}/api/v1",
        "identity_source": "deployment-link",
        "source": "deployment-link",
        "scope": "link:7",
        "link_id": "7",
        "link_in": "@deployment-links/7",
        "link_label": "the api",
        "link_callers": '["web"]',
        "link_verification": "unverified",
        "link_environment": "staging",
        "scope_path": "client.py",
        "scope_symbol": "read_item",
        "scope_base_key": base_key("BASE"),
        "base_shown": "{BASE}",
        "base_path": "/api/v1",
    }
    assert (claims[0].repo_id, claims[0].src_id, claims[0].line_start) == (2, 21, 3)
    assert plan_scoped_exposures([link], {2: routes}, {"api": 2}) == []
    assert plan_scoped_exposures([link], {2: routes}, {"web": 1}) == []
    plain = DeploymentLink(
        id=8,
        repository="api",
        label="url",
        url="https://api.example.test",
        created_at=NOW,
        updated_at=NOW,
    )
    assert plan_scoped_exposures([plain], {2: routes}, ids) == []
    monkeypatch.setattr(link_exposures, "MAX_LINK_EXPOSURES", 1)
    assert len(plan_scoped_exposures([link], {2: routes}, ids)) == 1


def test_plan_link_exposures_ignores_a_scoped_link() -> None:
    """A scoped declaration states no URL: the ordinary fallback gives it no exposure, whatever the
    repository serves."""
    link = DeploymentLink(
        id=7,
        repository="api",
        label="scoped",
        callers=["web"],
        scope=words("client.py", "BASE"),
        created_at=NOW,
        updated_at=NOW,
    )
    routes = [ServedRoute(2, 2, 21, "GET /v1/items", 3, 4, frozenset())]
    assert plan_link_exposures([link], {}, routes, {"web": 1, "api": 2}) == []
