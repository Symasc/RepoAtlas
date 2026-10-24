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
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from pydantic import ValidationError
from repoatlas.capabilities.errors import InvalidDeploymentLinkError
from repoatlas.capabilities.service import CapabilityService
from repoatlas.config.settings import DECLARATIONS_SLUG
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
from repoatlas.graph.http_declarations import facts_of, scoped_declaration_state
from repoatlas.graph.http_scopes import (
    COVERED,
    HTTP_SCOPED_EXPOSURE,
    SCOPE_TOO_BROAD,
    FileScope,
    ScopeDeclaration,
    ScopeRequest,
    ScopeSymbol,
    base_key,
    base_kind,
    covers,
    level_allowed,
    symbols_overlap,
)
from repoatlas.graph.link_exposures import plan_link_exposures, plan_scoped_exposures
from repoatlas.graph.linker import GLOBAL_PARSER
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.deployment_targets import (
    DeclarationProvenance,
    DeclarationScope,
    DeploymentLink,
    DeploymentLinkCreateRequest,
    DeploymentLinkDeleteRequest,
    DeploymentLinkFields,
    DeploymentLinkUpdateRequest,
    DeploymentTargetsRequest,
    ExportedLink,
    LinksExport,
    ScopeConflictError,
)
from repoatlas.models.http_connections import (
    CandidateService,
    ConnectionGroup,
    ConnectionRequest,
    HttpConnectionPreviewRequest,
    HttpConnectionPreviewResponse,
    HttpConnectionsRequest,
    HttpConnectionsResponse,
    HttpDeclarationHistoryRequest,
    PreviewScope,
)
from repoatlas.models.parsing import EdgeType, EntityType
from repoatlas.parsers.http_facts import HTTP_CLIENT_CALL, PENDING_FACT
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace
from repoatlas.storage.protocols import FileRow, HttpCallDecisionRow, UnresolvedRefRecord
from repoatlas.storage.sqlite.database import SCHEMA_VERSION
from repoatlas.storage.sqlite.deployment_links import _symbols_overlap
from repoatlas.storage.sqlite.migrator import load_migrations, user_version
from tests.http_connections_corpus import repos
from tests.http_scoped_corpus import LONG_BASE, SECRET_BASE, SECRET_TOKEN
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


# -- Phase A2, slice S6: the worklist shows each request's scope and each declaration's state -----
#
# Read side only: nothing here previews or writes a declaration. Declarations are made with
# ``via="import"`` (``declare`` above) and judged by the sync; the worklist is then read.

GLOBAL = CapabilityContext(scope=CapabilityScope.GLOBAL)


def worklist(workspace: Workspace, **filters: Any) -> HttpConnectionsResponse:
    return CapabilityService(workspace).http_connections(
        HttpConnectionsRequest(context=GLOBAL, **filters)
    )


def all_requests(workspace: Workspace) -> list[ConnectionRequest]:
    """Every unresolved request of the worklist, each group opened in turn."""
    found: list[ConnectionRequest] = []
    for group in worklist(workspace).groups:
        found.extend(worklist(workspace, group=group.key).requests)
    return found


def request_in(
    workspace: Workspace,
    requests: list[ConnectionRequest],
    name: str,
    path: str,
    symbol: str | None,
) -> ConnectionRequest:
    place = (slug(workspace, name), path, symbol)
    return next(r for r in requests if (r.site.repository, r.site.path, r.symbol) == place)


def group_with(workspace: Workspace, expression: str) -> ConnectionGroup:
    return next(g for g in worklist(workspace).groups if g.base_expression == expression)


def opened(
    workspace: Workspace, expression: str
) -> tuple[ConnectionGroup, HttpConnectionsResponse]:
    """The group that shows ``expression`` as its base, opened: the group as the worklist lists it
    then, and the whole answer (its requests and candidate services)."""
    key = group_with(workspace, expression).key
    page = worklist(workspace, group=key)
    return next(g for g in page.groups if g.key == key), page


def choices_of(group: ConnectionGroup) -> dict[tuple[str, str, str | None], tuple[int, int]]:
    """(caller, file, symbol) of each scope the group offers, with (covered, too broad)."""
    return {(c.repository, c.path, c.symbol): (c.covered, c.too_broad) for c in group.scope_choices}


def state_of(workspace: Workspace, link_id: int) -> tuple[str, str]:
    view = next(d for d in worklist(workspace).declarations if d.id == link_id)
    return view.state, view.reason


def file_scope_by_sql(workspace: Workspace, repository: str, path: str) -> FileScope:
    """What the join would read of one file, taken from the tables directly: its named entities
    and the ``USES`` pairs its per-file pass asserted there."""
    file_id = workspace.database.execute(
        "SELECT f.id FROM files f JOIN repositories r ON r.id = f.repo_id "
        "WHERE r.slug = ? AND f.rel_path = ?",
        (repository, path),
    ).fetchone()[0]
    symbols = workspace.database.execute(
        "SELECT id, file_id, type, qualified_name FROM entities "
        "WHERE file_id = ? AND qualified_name IS NOT NULL AND qualified_name != ''",
        (file_id,),
    ).fetchall()
    uses = workspace.database.execute(
        "SELECT src_id, dst_id FROM edges WHERE type = 'USES' AND file_id = ? "
        "AND COALESCE(parser, '') != ? AND src_id IN (SELECT id FROM entities WHERE file_id = ?)",
        (file_id, GLOBAL_PARSER, file_id),
    ).fetchall()
    return FileScope.build(
        (ScopeSymbol(*row) for row in symbols), [(user, used) for user, used in uses]
    )


def test_base_unknown_requests_show_their_scope_and_whether_it_can_be_declared(
    scoped_built: Workspace,
) -> None:
    """A request whose base the source does not state shows the qualified name of the function or
    method that makes it, the identity of its base text (a hash, never the text), and that a
    scoped declaration can name it. A request that states its origin is not one of these; a
    transport handed in at run time cannot be named by a symbol; the summary counts the scopable."""
    ws = scoped_built
    requests = all_requests(ws)

    read = request_in(ws, requests, "caller", "client.py", "read_item")
    assert (read.symbol, read.base_key, read.scopable, read.scope_refusal) == (
        "read_item",
        base_key("BASE"),
        True,
        None,
    )
    method = request_in(ws, requests, "caller", "client.py", "UsersClient.get_item")
    assert (method.base_key, method.scopable, method.scope_refusal) == (
        base_key("self.base_url"),
        True,
        None,
    )
    typed = request_in(ws, requests, "webts", "src/api.ts", "listItem")
    assert (typed.base_key, typed.scopable, typed.scope_refusal) == (base_key("BASE"), True, None)

    stated = request_in(ws, requests, "caller", "odd.py", "stated_read")
    assert (stated.base_key, stated.scopable, stated.scope_refusal) == (
        None,
        False,
        "not-base-unknown",
    )
    handed = request_in(ws, requests, "webts", "src/root.ts", "unstated")
    assert (handed.scopable, handed.scope_refusal) == (False, "injected-transport")

    for request in requests:
        assert request.scopable == (request.scope_refusal is None), request
    summary = worklist(ws).summary
    assert summary.scopable == sum(1 for r in requests if r.scopable) > 0
    assert summary.scopable < summary.base_not_stated

    # The text itself is nowhere in the answer: only its hash.
    text = json.dumps(worklist(ws).model_dump(mode="json"))
    assert "self.base_url" not in text.replace("{self.base_url}", "")


def test_a_request_at_module_level_has_no_symbol() -> None:
    """The decision row says what entity asks and its type: a request made by the file itself has
    no symbol to name, one made inside a function or method has its qualified name. A request that
    states its origin, or whose transport is injected, is not scopable and says why."""
    row = HttpCallDecisionRow(
        decision_ref="hcd1-" + "0" * 20,
        repo_id=1,
        file_id=1,
        src_id=1,
        method="GET",
        url="{BASE}/v1/items",
        variant="",
        decision=UNKNOWN_BASE,
        evidence_json="{}",
        chain_json="[]",
        path="client.py",
        src_qualified_name="client.py",
        src_type="file",
    )
    top = facts_of(row, "caller")
    assert top.symbol is None and top.scopable and top.scope_refusal is None
    assert top.base_key == base_key("BASE")

    inside = row.model_copy(update={"src_qualified_name": "Client.get", "src_type": "method"})
    assert facts_of(inside, "caller").symbol == "Client.get"

    injected = row.model_copy(update={"evidence_json": json.dumps({"injected_at": "root.ts:3"})})
    handed = facts_of(injected, "caller")
    assert (handed.scopable, handed.scope_refusal) == (False, "injected-transport")

    stated = row.model_copy(
        update={"url": "https://items.example.test/v1/items", "decision": SERVICE_UNMATCHED}
    )
    fact = facts_of(stated, "caller")
    assert (fact.base_key, fact.scopable, fact.scope_refusal) == (None, False, "not-base-unknown")


def test_a_truncated_base_expression_is_not_scopable(scoped_built: Workspace) -> None:
    """A base the parser may have cut (60 characters or more) is never compared: the request says
    so, no scope is offered for it, and no service is suggested for it."""
    ws = scoped_built
    requests = all_requests(ws)
    long = request_in(ws, requests, "caller", "odd.py", "long_read")
    assert len(LONG_BASE) == 60
    assert long.scope_refusal == "base-text-truncated" and long.scopable is False

    for group in worklist(ws).groups:
        page = worklist(ws, group=group.key)
        if long.decision_ref in {r.decision_ref for r in page.requests}:
            assert page.candidates == ()
            assert [g.scope_choices for g in page.groups if g.key == group.key] == [()]
            break
    else:
        pytest.fail("the request is in no group")


def test_a_scheme_with_an_unread_host_is_not_scopable(scoped_built: Workspace) -> None:
    """``http://{host}/x`` states a scheme and leaves the host unread: it is not a base text, so no
    scope can name it."""
    ws = scoped_built
    host = request_in(ws, all_requests(ws), "caller", "odd.py", "host_read")
    assert host.scope_refusal == "host-expression" and host.scopable is False
    assert host.base_key is None


def test_scope_choices_are_the_file_and_each_enclosing_symbol_with_their_counts(
    scoped_built: Workspace,
) -> None:
    """Opening a group offers, per file of a caller, the whole file and each dotted prefix of the
    symbol that makes a request, when it names one entity of the file; each with the requests of the
    group it covers and those it would refuse as too broad. A receiver base is covered by no whole
    file; a class covers its methods and not a class whose name only starts like it; a module name
    covers what proves it reads that name. The counts are those of ``http_scopes.covers``. A group
    that is not opened offers nothing."""
    ws = scoped_built
    caller = slug(ws, "caller")
    assert all(g.scope_choices == () for g in worklist(ws).groups)

    group, page = opened(ws, "{self.base_url}")
    assert choices_of(group) == {
        (caller, "client.py", None): (0, 3),
        (caller, "client.py", "UsersClient"): (2, 0),
        (caller, "client.py", "UsersClient.get_item"): (1, 0),
        (caller, "client.py", "UsersClient.make_item"): (1, 0),
        (caller, "client.py", "UsersClientV2"): (1, 0),
        (caller, "client.py", "UsersClientV2.get_item"): (1, 0),
    }

    group, page = opened(ws, "{BASE}")
    second, webts = slug(ws, "second"), slug(ws, "webts")
    assert choices_of(group) == {
        (caller, "client.py", None): (3, 1),
        (caller, "client.py", "read_item"): (1, 0),
        (caller, "client.py", "write_item"): (1, 0),
        (caller, "client.py", "list_items"): (1, 0),
        (caller, "client.py", "shadowed_read"): (1, 0),
        (caller, "other.py", None): (1, 0),
        (caller, "other.py", "other_read"): (1, 0),
        (second, "client.py", None): (1, 0),
        (second, "client.py", "read_item"): (1, 0),
        (webts, "src/api.ts", None): (2, 1),
        (webts, "src/api.ts", "listItem"): (1, 0),
        (webts, "src/api.ts", "createItem"): (1, 0),
        (webts, "src/api.ts", "shadowedItem"): (1, 0),
    }
    # the same numbers, from `covers` over the tables
    for choice in group.scope_choices:
        scope = file_scope_by_sql(ws, choice.repository, choice.path)
        declaration = ScopeDeclaration(
            choice.repository, choice.path, base_key("BASE"), choice.symbol
        )
        verdicts: Counter[str] = Counter()
        for request in page.requests:
            if (request.site.repository, request.site.path) != (choice.repository, choice.path):
                continue
            url, src_id = ws.database.execute(
                "SELECT url, src_id FROM http_call_decisions WHERE decision_ref = ?",
                (request.decision_ref,),
            ).fetchone()
            asked = ScopeRequest(choice.repository, choice.path, url, request.symbol or "", src_id)
            verdicts[covers(declaration, asked, scope)] += 1
        assert (choice.covered, choice.too_broad) == (
            verdicts["covered"],
            verdicts["scope-too-broad"],
        ), choice


def test_candidates_for_a_scope_are_services_whose_routes_match_never_preselected(
    scoped_built: Workspace,
) -> None:
    """Opening a group of an unstated base offers the services whose served routes its requests'
    shapes match, counted as a scoped declaration would be decided: ``alpha`` and ``beta`` serve
    the same routes (tied, neither preferred), ``twin`` answers one route from two files
    (ambiguous, nothing unique), ``mounted`` serves under a ``/v1`` prefix the requests write. A
    repository that serves no route is never offered. The answer chooses none and stores nothing."""
    ws = scoped_built
    alpha, beta = slug(ws, "alpha"), slug(ws, "beta")
    twin, mounted = slug(ws, "twin"), slug(ws, "mounted")
    _, page = opened(ws, "{BASE}")

    names = {c.repository: c for c in page.candidates}
    assert set(names) == {alpha, beta, twin, mounted}
    assert names[alpha].suggested_base_path == names[beta].suggested_base_path == ""
    assert names[mounted].suggested_base_path == "/v1"
    assert (names[alpha].requests_matched, names[alpha].requests_unique) == (
        names[beta].requests_matched,
        names[beta].requests_unique,
    )
    assert names[alpha].requests_unique > 0 and names[alpha].requests_ambiguous == 0
    assert names[mounted].requests_unique > 0 and names[mounted].requests_ambiguous == 0
    assert names[twin].requests_unique == 0 and names[twin].requests_ambiguous > 0
    unique = [c.requests_unique for c in page.candidates]
    assert unique == sorted(unique, reverse=True)

    # a candidate is a hypothesis: nothing in it says chosen, and nothing was stored
    assert set(CandidateService.model_fields) == {
        "repository",
        "routes_served",
        "requests_matched",
        "requests_unique",
        "requests_ambiguous",
        "suggested_base_path",
    }
    assert ws.deployment_links.list_all() == [] and page.declarations == ()


def test_a_scoped_declaration_state_follows_its_scope(
    scoped_built: Workspace, tmp_path: Path
) -> None:
    """A scoped declaration is pending until the next sync, applied once a request is joined
    through it, and ineffective with the pass's reason when a rival service covers the same
    requests; with the rival gone it applies again. The worklist shows its scope (never a URL) and
    attaches it to the group of its base."""
    ws = scoped_built
    link = declare(ws, "alpha", "caller", "client.py", "BASE")
    assert state_of(ws, link) == ("pending", "applies-at-next-sync")
    resync(ws, tmp_path)
    assert state_of(ws, link) == ("applied", "")

    view = next(d for d in worklist(ws).declarations if d.id == link)
    assert view.scope == words("client.py", "BASE") and view.url is None
    assert view.repository == slug(ws, "alpha") and view.callers == (slug(ws, "caller"),)
    assert view.joined == 2
    group = group_with(ws, "{BASE}")
    assert [d.id for d in group.declarations] == [link]
    assert [g.base_expression for g in worklist(ws, declaration_state="applied").groups] == [
        "{BASE}"
    ]
    assert worklist(ws, declaration_state="pending").groups == ()
    assert group_with(ws, "{self.base_url}").declarations == ()

    rival = insert_raw(ws, "beta", "caller", words("client.py", "BASE"))
    assert state_of(ws, rival) == ("pending", "applies-at-next-sync")
    resync(ws, tmp_path)
    assert state_of(ws, link) == ("ineffective", "ambiguous")
    assert state_of(ws, rival) == ("ineffective", "ambiguous")
    assert next(d for d in worklist(ws).declarations if d.id == link).joined == 0

    assert ws.deployment_links.delete(rival)
    resync(ws, tmp_path)
    assert state_of(ws, link) == ("applied", "")


def test_scoped_declaration_state_takes_the_first_rule_that_holds() -> None:
    """The order of the rules decides which reason is shown when several hold: the service, then
    the caller, then the pass that has not read the declaration, then a joined request, then what
    the index lost, then what the pass declined (the most telling first), else nothing matched."""
    link = DeploymentLink(
        id=7,
        repository="api",
        label="scoped",
        callers=["web"],
        scope=words("client.py", "BASE"),
        created_at=NOW,
        updated_at=NOW,
    )

    def state(**change: Any) -> tuple[str, str]:
        arguments: dict[str, Any] = {
            "pass_reads": {7: NOW.isoformat()},
            "indexed": True,
            "caller_indexed": True,
            "serves_routes": True,
            "found": True,
            "requested": True,
            "joined": 0,
            "declined": {},
        }
        arguments.update(change)
        return scoped_declaration_state(link, **arguments)

    assert state(indexed=False, caller_indexed=False, pass_reads=None, joined=3) == (
        "invalidated",
        "repository-not-indexed",
    )
    assert state(caller_indexed=False, pass_reads=None, joined=3) == (
        "invalidated",
        "caller-not-indexed",
    )
    assert state(pass_reads=None, joined=3, serves_routes=False) == (
        "pending",
        "applies-at-next-sync",
    )
    assert state(pass_reads={7: "an older revision"}) == ("pending", "applies-at-next-sync")
    assert state(joined=2, serves_routes=False, found=False, requested=False) == ("applied", "")
    assert state(serves_routes=False, found=False, requested=False) == (
        "invalidated",
        "repository-serves-no-routes",
    )
    assert state(found=False, requested=False, declined={"ambiguous": 1}) == (
        "invalidated",
        "scope-not-found",
    )
    assert state(requested=False, declined={"ambiguous": 1}) == (
        "invalidated",
        "base-no-longer-requested",
    )
    declined = {
        "scope-unidentified": 1,
        "injected-root-unselected": 1,
        "route-unmatched": 1,
        "scope-too-broad": 1,
        "ambiguous": 1,
    }
    for reason in (
        "ambiguous",
        "scope-too-broad",
        "route-unmatched",
        "scope-unidentified",
        "injected-root-unselected",
    ):
        assert state(declined=declined) == ("ineffective", reason)
        del declined[reason]
    assert state(declined={"callers-excluded": 1}) == ("ineffective", "no-request-matched")
    assert state() == ("ineffective", "no-request-matched")


def test_a_scoped_declaration_whose_symbol_is_gone_is_invalidated(
    scoped_built: Workspace, tmp_path: Path
) -> None:
    """The declaration names ``read_item``; the function is renamed away and the sync runs: the
    symbol is not in the file any more, so the declaration is invalidated with the reason that
    its scope was not found, whatever else the file still does. It is kept, not deleted."""
    ws = scoped_built
    link = declare(ws, "alpha", "caller", "client.py", "BASE", symbol="read_item")
    resync(ws, tmp_path)
    assert state_of(ws, link) == ("applied", "")

    source = tmp_path / "repos" / "caller" / "client.py"
    text = source.read_text()
    assert text.count("def read_item(item_id):") == 1
    source.write_text(text.replace("def read_item(item_id):", "def read_item_renamed(item_id):"))
    resync(ws, tmp_path)

    assert state_of(ws, link) == ("invalidated", "scope-not-found")
    assert [d.id for d in ws.deployment_links.list_all()] == [link]
    assert decided(ws)[(*CLIENT, "read_item_renamed")].decision == UNKNOWN_BASE


def test_a_scoped_declaration_whose_base_is_now_stated_is_invalidated(
    scoped_built: Workspace, tmp_path: Path
) -> None:
    """The declaration names ``read_item`` and the base it reads. The source now states the URL
    there, so no request in that symbol leaves its base unstated: the declaration is invalidated
    with that reason. The other functions that read ``BASE`` do not keep it alive."""
    ws = scoped_built
    link = declare(ws, "alpha", "caller", "client.py", "BASE", symbol="read_item")
    resync(ws, tmp_path)
    assert state_of(ws, link) == ("applied", "")

    source = tmp_path / "repos" / "caller" / "client.py"
    text = source.read_text()
    before = 'def read_item(item_id):\n    return requests.get(f"{BASE}/v1/items/{item_id}"'
    after = (
        'def read_item(item_id):\n    return requests.get(f"https://items.example.test/v1/items/'
        '{item_id}"'
    )
    assert text.count(before) == 1
    source.write_text(text.replace(before, after))
    resync(ws, tmp_path)

    assert decided(ws)[(*CLIENT, "read_item")].decision != UNKNOWN_BASE
    assert decided(ws)[(*CLIENT, "shadowed_read")].decision == UNKNOWN_BASE
    assert state_of(ws, link) == ("invalidated", "base-no-longer-requested")


def test_a_symbol_named_twice_in_a_file_is_not_an_identifiable_scope(
    scoped_built: Workspace, tmp_path: Path
) -> None:
    """A second ``def read_item`` makes the name stand for two entities, so it identifies no scope:
    the declaration on it joins nothing, every request it concerns says ``scope-unidentified`` and
    names the declaration, the declaration is invalidated (its scope is not found), and the worklist
    no longer offers ``read_item`` as a scope while it still offers the file and other symbols."""
    ws = scoped_built
    link = declare(ws, "alpha", "caller", "client.py", "BASE", symbol="read_item")
    resync(ws, tmp_path)
    assert state_of(ws, link) == ("applied", "")

    source = tmp_path / "repos" / "caller" / "client.py"
    source.write_text(
        source.read_text()
        + '\n\ndef read_item(item_id):\n    return requests.get(f"{BASE}/v1/items/{item_id}")\n'
    )
    resync(ws, tmp_path)

    twice = [
        r
        for r in all_requests(ws)
        if r.site.repository == slug(ws, "caller")
        and r.site.path == "client.py"
        and r.symbol == "read_item"
    ]
    assert len(twice) == 2
    for request in twice:
        assert request.status == UNKNOWN_BASE
        assert request.declaration_outcome == "scope-unidentified"
        assert request.declaration_ids == (link,)
    assert state_of(ws, link) == ("invalidated", "scope-not-found")

    group, _ = opened(ws, "{BASE}")
    caller = slug(ws, "caller")
    symbols = {
        c.symbol for c in group.scope_choices if c.repository == caller and c.path == "client.py"
    }
    assert symbols == {None, "write_item", "list_items", "shadowed_read"}


# -- Phase A2, slice S7: the scoped preview ------------------------------------------------------
#
# A preview answers, for the words of a scope and a service, what the sync will decide for each
# request of the group, with the sync's own functions. These tests preview first, then declare
# (``via="import"``: the sync alone decides), sync, and compare request by request.

#: The sync's name for a refusal the preview names in A1's words (route shapes).
SYNC_NAME = {"no-matching-route": "route-unmatched"}


def preview_of(
    workspace: Workspace,
    expression: str,
    service: str,
    caller: str,
    path: str,
    text: str,
    *,
    symbol: str | None = None,
    base_path: str = "",
    **more: Any,
) -> HttpConnectionPreviewResponse:
    """The scoped preview of the group that shows ``expression``: ``service`` behind the base
    ``text`` in ``caller``'s ``path`` (and ``symbol``). A ``caller`` that is no repository's name
    is used as it is."""
    repos = workspace.repositories.list_all()
    scope = PreviewScope(
        caller=next((r.slug for r in repos if r.name == caller), caller),
        path=path,
        symbol=symbol,
        base_key=base_key(text),
        base_path=base_path,
        **more,
    )
    return CapabilityService(workspace).http_connection_preview(
        HttpConnectionPreviewRequest(
            context=GLOBAL,
            group=group_with(workspace, expression).key,
            repository=slug(workspace, service),
            scope=scope,
        )
    )


def places(workspace: Workspace) -> dict[str, tuple[str, str, str | None]]:
    """Where each unresolved request is, by its decision ref: (repository name, file, symbol)."""
    names = {r.slug: r.name for r in workspace.repositories.list_all()}
    return {
        r.decision_ref: (names[r.site.repository], r.site.path, r.symbol)
        for r in all_requests(workspace)
    }


def by_ref(workspace: Workspace) -> dict[str, tuple[str, dict[str, Any]]]:
    """Every stored decision by its ref: the decision and its evidence."""
    rows = workspace.database.execute(
        "SELECT decision_ref, decision, evidence_json FROM http_call_decisions"
    ).fetchall()
    return {ref: (decision, json.loads(evidence)) for ref, decision, evidence in rows}


def blocked(preview: HttpConnectionPreviewResponse, fragment: str) -> None:
    assert not preview.can_create
    assert any(fragment in item for item in preview.requirements), preview.requirements


def test_a_scoped_preview_states_each_covered_requests_outcome_and_the_rest_as_out_of_scope(
    scoped_built: Workspace,
) -> None:
    """Previewing ``alpha`` behind ``BASE`` for the whole of ``caller``'s ``client.py`` lists every
    request of the group: the two the service has a handler for as ``would-link`` with that
    handler, the one it answers no route for as ``no-matching-route``, the shadowed one as
    ``scope-too-broad`` (the sync's refusal), and every request of another file or repository as
    ``out-of-scope``, after the ones the scope reaches. The declaration it would store is echoed
    with the base shown by its name; it states no origin and no URL; nothing is stored."""
    ws = scoped_built
    where = places(ws)
    preview = preview_of(ws, "{BASE}", "alpha", "caller", "client.py", "BASE")

    in_client = ("caller", "client.py")
    expected = {
        (*in_client, "read_item"): "would-link",
        (*in_client, "write_item"): "would-link",
        (*in_client, "list_items"): "no-matching-route",
        (*in_client, "shadowed_read"): "scope-too-broad",
        ("caller", "other.py", "other_read"): "out-of-scope",
        ("second", "client.py", "read_item"): "out-of-scope",
        ("webts", "src/api.ts", "listItem"): "out-of-scope",
        ("webts", "src/api.ts", "createItem"): "out-of-scope",
        ("webts", "src/api.ts", "shadowedItem"): "out-of-scope",
    }
    rows = {where[row.decision_ref]: row for row in preview.rows}
    assert len(rows) == len(preview.rows) == len(expected) and not preview.has_more
    assert {place: row.outcome for place, row in rows.items()} == expected
    assert preview.counts == {
        "would-link": 2,
        "no-matching-route": 1,
        "scope-too-broad": 1,
        "out-of-scope": 5,
    }
    assert [row.outcome for row in preview.rows[:4]].count("out-of-scope") == 0
    assert {row.outcome for row in preview.rows[4:]} == {"out-of-scope"}

    read, write = rows[(*in_client, "read_item")], rows[(*in_client, "write_item")]
    assert read.handler == "alpha/main.py:6"
    assert write.handler is not None and write.handler.startswith("alpha/main.py:")
    assert write.handler != read.handler
    assert all(row.handler is None for row in preview.rows if row.outcome != "would-link")

    assert preview.scope == words("client.py", "BASE")
    assert (preview.origin, preview.url) == (None, None)
    assert preview.repository == slug(ws, "alpha") and preview.routes_served == 2
    assert preview.can_create and preview.requirements == ()
    assert preview.conflicts_with is None and preview.duplicate_of is None
    assert any("not provably" in warning for warning in preview.warnings)
    assert ws.deployment_links.list_all() == [] and audit_count(ws) == 0


def test_a_scoped_preview_agrees_with_what_the_sync_then_decides(
    scoped_built: Workspace, tmp_path: Path
) -> None:
    """Six scopes of the corpus are previewed, declared, and synced. A request the preview marked
    ``would-link`` is linked by the sync through that declaration at the handler the preview named
    (and the sync links no other request); one it marked refused is not linked and the sync
    refuses it with the same reason, naming the declaration; one it marked ``out-of-scope`` is
    not concerned by that declaration. The six reach every verdict: a file, a class, a base path
    that prefixes the route, a request written without the prefix, a service answering twice, and
    a transport handed in at run time."""
    ws = scoped_built
    cases: dict[str, tuple[str, str, str, str, str, str | None, str]] = {
        "file": ("{BASE}", "alpha", "caller", "client.py", "BASE", None, ""),
        "class": (
            "{self.base_url}",
            "alpha",
            "caller",
            "client.py",
            "self.base_url",
            "UsersClient",
            "",
        ),
        "mounted": ("{base}", "mounted", "caller", "client.py", "base", "param_read", "/v1"),
        "unprefixed": ("{root}", "mounted", "caller", "client.py", "root", "prefixed_read", "/v1"),
        "twice": (
            "{config.api_base}",
            "twin",
            "caller",
            "qualified.py",
            "config.api_base",
            None,
            "",
        ),
        "injected": ("{base}", "alpha", "webts", "src/root.ts", "base", None, ""),
    }
    previews = {
        name: preview_of(ws, *words_of[:5], symbol=words_of[5], base_path=words_of[6])
        for name, words_of in cases.items()
    }
    outcomes = {name: {row.outcome for row in p.rows} for name, p in previews.items()}
    assert all(p.rows and not p.has_more for p in previews.values())
    assert {n: p.counts.get("would-link", 0) for n, p in previews.items()} == {
        "file": 2,
        "class": 2,
        "mounted": 1,
        "unprefixed": 0,
        "twice": 0,
        "injected": 0,
    }
    assert "no-matching-route" in outcomes["unprefixed"] and "ambiguous" in outcomes["twice"]
    assert "injected-root-unselected" in outcomes["injected"]
    assert "scope-too-broad" in outcomes["twice"] and "out-of-scope" in outcomes["class"]
    would_link = {
        row.decision_ref for p in previews.values() for row in p.rows if row.outcome == "would-link"
    }

    links = {
        name: declare(ws, service, caller, path, text, symbol=symbol, base_path=prefix)
        for name, (_, service, caller, path, text, symbol, prefix) in cases.items()
    }
    resync(ws, tmp_path)
    after = by_ref(ws)

    assert {ref for ref, (decision, _) in after.items() if decision == LINKED} == would_link
    for name, preview in previews.items():
        link = str(links[name])
        for row in preview.rows:
            decision, evidence = after[row.decision_ref]
            concerned = str(evidence.get("declaration_ids", "")).split(",")
            if row.outcome == "would-link":
                assert decision == LINKED, (name, row)
                assert evidence["link_id"] == link, (name, row)
                assert evidence["served_at"] == row.handler, (name, row)
                continue
            if row.outcome == "out-of-scope":
                # Not linked through THIS declaration. Another independent declaration may
                # legitimately link the same shared-base request, and then its preview says so.
                assert link not in concerned, (name, row)
                if decision == LINKED:
                    assert evidence["link_id"] != link, (name, row)
                    assert row.decision_ref in would_link, (name, row)
                continue
            assert decision != LINKED, (name, row)
            assert link in concerned, (name, row)
            reason = SYNC_NAME.get(row.outcome, row.outcome)
            assert evidence["declaration_outcome"] == reason, (name, row)


def test_a_scoped_preview_refuses_a_base_path_that_is_not_a_prefix() -> None:
    """A base path is empty or ``/`` and literal segments. ``v1`` (no leading ``/``), ``/v1?x`` (a
    query) and ``/v1{a}`` (a hole) are refused with the message the A1 preview gives, whether the
    scope states them or the request does; a valid one is kept, without a trailing ``/``, and the
    request cannot state a second one beside the scope's."""
    message = re.escape("a base path is empty or starts with `/`, with no query or holes")
    words_of_scope: dict[str, Any] = {"caller": "web", "path": "a.py", "base_key": base_key("BASE")}
    asked: dict[str, Any] = {
        "context": GLOBAL,
        "group": "hcg1-" + "0" * 20,
        "repository": "api",
    }
    for value in ("v1", "/v1?x", "/v1{a}"):
        with pytest.raises(ValidationError, match=message):
            PreviewScope(**words_of_scope, base_path=value)
        with pytest.raises(ValidationError, match=message):
            HttpConnectionPreviewRequest(
                **asked, scope=PreviewScope(**words_of_scope), base_path=value
            )
        with pytest.raises(ValidationError, match=message):
            HttpConnectionPreviewRequest(**asked, base_path=value)
    assert PreviewScope(**words_of_scope, base_path="/v1/").base_path == "/v1"
    assert PreviewScope(**words_of_scope).base_path == ""
    scoped = PreviewScope(**words_of_scope, base_path="/v1")
    assert HttpConnectionPreviewRequest(**asked, scope=scoped).scope == scoped
    with pytest.raises(ValidationError, match="states its base path in the scope"):
        HttpConnectionPreviewRequest(**asked, scope=scoped, base_path="/v2")


def test_a_scoped_preview_request_names_its_caller_once_and_never_the_base_text() -> None:
    """``callers`` is empty or the scope's caller; the scope names a base by its key, so the text
    (or any other word) is not accepted; a request without a scope is the A1 request unchanged."""
    scope = PreviewScope(caller="web", path="a.py", base_key=base_key("BASE"))
    asked: dict[str, Any] = {
        "context": GLOBAL,
        "group": "hcg1-" + "0" * 20,
        "repository": "api",
    }
    assert HttpConnectionPreviewRequest(**asked, scope=scope).callers == ()
    assert HttpConnectionPreviewRequest(**asked, scope=scope, callers=("web",)).callers == ("web",)
    with pytest.raises(ValidationError, match="names its one caller"):
        HttpConnectionPreviewRequest(**asked, scope=scope, callers=("other",))
    with pytest.raises(ValidationError, match="names its one caller"):
        HttpConnectionPreviewRequest(**asked, scope=scope, callers=("web", "other"))
    for refused in ("BASE", "self.base_url", "hbk1-" + "A" * 20, "hbk1-" + "0" * 19):
        with pytest.raises(ValidationError):
            PreviewScope(caller="web", path="a.py", base_key=refused)
    with_text: dict[str, Any] = {"caller": "web", "path": "a.py", "base_key": base_key("BASE")}
    with pytest.raises(ValidationError):
        PreviewScope(**with_text, base_text="BASE")
    plain = HttpConnectionPreviewRequest(**asked, callers=("a", "b"), base_path="/v1")
    assert plain.scope is None and plain.callers == ("a", "b") and plain.base_path == "/v1"


def test_a_scoped_preview_shows_a_dotted_base_as_the_masked_name(scoped_built: Workspace) -> None:
    """``self.base_url`` is not a name: the declaration the preview echoes shows it as ``{…}``,
    and the text is nowhere in the answer, not even in a requirement that refuses a shown form
    that names it. The shown form the group has is accepted when the request states it; a plain
    name (``BASE``) is shown by its name. The worklist's own display of the group (A1) is
    unchanged."""
    ws = scoped_built
    assert group_with(ws, "{self.base_url}").base_expression == "{self.base_url}"
    args = ("{self.base_url}", "alpha", "caller", "client.py", "self.base_url")

    preview = preview_of(ws, *args, symbol="UsersClient")
    assert preview.scope is not None and preview.scope.base_shown == "{…}"
    assert preview.can_create and preview.counts == {"would-link": 2, "out-of-scope": 1}
    text = preview.model_dump_json()
    assert "base_url" not in text and "self." not in text

    stated = preview_of(ws, *args, symbol="UsersClient", base_shown="{…}")
    assert stated.can_create and stated.scope == preview.scope

    raw = preview_of(ws, *args, symbol="UsersClient", base_shown="{self.base_url}")
    blocked(raw, "is shown as {…}")
    assert raw.scope is not None and raw.scope.base_shown == "{…}"
    assert "base_url" not in raw.model_dump_json()

    plain = preview_of(ws, "{BASE}", "alpha", "caller", "client.py", "BASE")
    assert plain.scope is not None and plain.scope.base_shown == "{BASE}"
    named = preview_of(ws, "{BASE}", "alpha", "caller", "client.py", "BASE", base_shown="{BASE}")
    assert named.can_create
    blocked(
        preview_of(ws, "{BASE}", "alpha", "caller", "client.py", "BASE", base_shown="{other}"),
        "is shown as {BASE}",
    )


def test_a_scoped_preview_blocks_what_the_sync_could_not_decide(scoped_built: Workspace) -> None:
    """Each requirement of a scoped declaration blocks it, with its own words: the caller is not
    indexed; the path is not a file of it; the symbol is not exactly one entity of the file; no
    request in that file leaves that base unstated (the scope cannot be identified); a receiver
    base for a whole file; a service that serves no route, or answers none of the covered requests
    once; a base key or shown base that is not the group's; and words that are not a scope at all.
    A service that is not indexed, the declarations repository and a group that states its origin
    are refused outright, as for a URL."""
    ws = scoped_built
    base = ("{BASE}", "alpha", "caller", "client.py", "BASE")
    assert preview_of(ws, *base).can_create

    ghost = preview_of(ws, "{BASE}", "alpha", "ghost", "client.py", "BASE")
    blocked(ghost, "ghost is not an indexed repository")
    blocked(preview_of(ws, "{BASE}", "alpha", "caller", "missing.py", "BASE"), "is not a file of")
    blocked(preview_of(ws, *base, symbol="nothing_here"), "not exactly one")
    blocked(
        preview_of(ws, "{BASE}", "alpha", "caller", "odd.py", "BASE"),
        "the scope cannot be identified",
    )
    receiver = preview_of(ws, "{self.base_url}", "alpha", "caller", "client.py", "self.base_url")
    blocked(receiver, "names no single binding in a whole file")
    assert receiver.counts == {"scope-too-broad": 3}
    blocked(preview_of(ws, "{BASE}", "second", "caller", "client.py", "BASE"), "serves no route")
    twin = preview_of(ws, "{BASE}", "twin", "caller", "client.py", "BASE")
    blocked(twin, "nothing would be joined")
    assert twin.counts["ambiguous"] == 1 and twin.counts["no-matching-route"] == 2
    blocked(
        preview_of(ws, "{BASE}", "alpha", "caller", "client.py", "somethingelse"),
        "does not name the base expression of this group",
    )

    invalid = preview_of(ws, "{BASE}", "alpha", "caller", "/etc/passwd", "BASE")
    blocked(invalid, "a scope path is relative")
    assert invalid.scope is None and invalid.rows == ()

    scope = PreviewScope(caller=slug(ws, "caller"), path="client.py", base_key=base_key("BASE"))
    service = CapabilityService(ws)
    for repository in ("nowhere", DECLARATIONS_SLUG):
        with pytest.raises(InvalidDeploymentLinkError, match="not indexed"):
            service.http_connection_preview(
                HttpConnectionPreviewRequest(
                    context=GLOBAL,
                    group=group_with(ws, "{BASE}").key,
                    repository=repository,
                    scope=scope,
                )
            )
    stated = next(g for g in worklist(ws).groups if g.kind == "stated-origin")
    with pytest.raises(InvalidDeploymentLinkError, match="do not leave a base expression"):
        service.http_connection_preview(
            HttpConnectionPreviewRequest(
                context=GLOBAL, group=stated.key, repository=slug(ws, "alpha"), scope=scope
            )
        )
    assert ws.deployment_links.list_all() == [] and audit_count(ws) == 0


def test_a_scoped_preview_sees_the_declarations_already_stored(scoped_built: Workspace) -> None:
    """Over a scope another service already holds, the preview names the declaration that holds it
    (``conflicts_with``) and blocks, and each request both would cover is ``ambiguous`` as the join
    leaves it, whatever the new service answers; a narrower symbol of the same file conflicts
    too; another file does not. The same service again is a duplicate, not a conflict."""
    ws = scoped_built
    mine = declare(ws, "alpha", "caller", "client.py", "BASE")

    again = preview_of(ws, "{BASE}", "alpha", "caller", "client.py", "BASE")
    assert again.duplicate_of == mine and again.conflicts_with is None and again.can_create
    assert again.counts["would-link"] == 2

    rival = preview_of(ws, "{BASE}", "beta", "caller", "client.py", "BASE")
    blocked(rival, "already states another service")
    assert rival.conflicts_with == mine and rival.duplicate_of is None
    assert rival.counts == {"ambiguous": 3, "scope-too-broad": 1, "out-of-scope": 5}

    narrow = preview_of(ws, "{BASE}", "beta", "caller", "client.py", "BASE", symbol="read_item")
    assert narrow.conflicts_with == mine and not narrow.can_create
    assert narrow.counts == {"ambiguous": 1, "out-of-scope": 8}

    elsewhere = preview_of(ws, "{BASE}", "beta", "caller", "other.py", "BASE")
    assert elsewhere.conflicts_with is None and elsewhere.can_create
    assert elsewhere.counts == {"would-link": 1, "out-of-scope": 8}


def test_the_level_rule_the_preview_states_is_the_one_covers_applies() -> None:
    """A receiver base (``self.x``, ``cls.x``, ``this.x``) needs a symbol: ``level_allowed`` says
    so for the preview's requirement and ``covers`` refuses the same declaration at a whole file
    and covers it at the symbol, so the two cannot drift. Other bases are allowed at either level
    (the join then asks for the evidence they need)."""
    scope = FileScope.build([ScopeSymbol(11, 1, "function", "f")], [(11, 11)])
    for text in ("self.base", "cls.base", "this.base", "self['base']"):
        assert base_kind(text) == "receiver"
        asked = ScopeRequest("r", "a.py", "{" + text + "}/x", "f", 11)
        assert not level_allowed(base_kind(text), None)
        assert covers(ScopeDeclaration("r", "a.py", base_key(text), None), asked, scope) == (
            SCOPE_TOO_BROAD
        )
        assert level_allowed(base_kind(text), "f")
        assert covers(ScopeDeclaration("r", "a.py", base_key(text), "f"), asked, scope) == COVERED
    for text in ("BASE", "config.api_base"):
        assert level_allowed(base_kind(text), None) and level_allowed(base_kind(text), "f")


def test_the_preview_and_the_store_overlap_symbols_alike() -> None:
    """The preview's ``conflicts_with`` and the store's refusal of a second service over a scope
    ask the same question about two symbols."""
    names = [None, "A", "A.b", "A.b.c", "AB", "B", "A.bc"]
    for one in names:
        for other in names:
            assert symbols_overlap(one, other) == _symbols_overlap(one, other), (one, other)
    assert symbols_overlap("A", "A.b") and not symbols_overlap("A", "AB")


# -- Phase A2, slice S8: the write path ----------------------------------------------------------
#
# Over the API a scoped declaration is made from a worklist group, and the write runs the scoped
# preview (``http_connections.preview``, the function the worklist shows) again inside one read
# snapshot: a declaration the preview would block is not stored. These tests write through
# ``deployment_link_create``/``update``/``delete``, as the API does, and read what is left.


def repository_context(workspace: Workspace, name: str) -> CapabilityContext:
    return CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=slug(workspace, name))


def write_fields(
    workspace: Workspace,
    caller: str,
    scope: DeclarationScope,
    *,
    label: str = "mapped",
) -> DeploymentLinkFields:
    return DeploymentLinkFields(label=label, callers=[slug(workspace, caller)], scope=scope)


def declare_over_api(
    workspace: Workspace,
    served_by: str,
    group: str | None,
    caller: str,
    scope: DeclarationScope,
    *,
    label: str = "mapped",
    note: str | None = None,
) -> DeploymentLink:
    """Create the declaration as the API does: ``served_by`` is the repository in scope, ``group``
    the worklist group the operator made it from (``None``: none)."""
    return (
        CapabilityService(workspace)
        .deployment_link_create(
            DeploymentLinkCreateRequest(
                context=repository_context(workspace, served_by),
                link=write_fields(workspace, caller, scope, label=label),
                provenance=DeclarationProvenance(via="ui", note=note, request_group=group),
            )
        )
        .link
    )


def update_over_api(
    workspace: Workspace,
    served_by: str,
    link_id: int,
    group: str | None,
    fields: DeploymentLinkFields,
) -> DeploymentLink:
    return (
        CapabilityService(workspace)
        .deployment_link_update(
            DeploymentLinkUpdateRequest(
                context=repository_context(workspace, served_by),
                id=link_id,
                link=fields,
                provenance=DeclarationProvenance(via="ui", request_group=group),
            )
        )
        .link
    )


def test_a_scoped_declaration_without_a_worklist_group_is_refused_over_the_api(
    scoped_built: Workspace,
) -> None:
    """Over the API a scope is named from a worklist group: with no provenance, or provenance that
    names no group, the write is refused whatever the words say, and nothing is stored or
    recorded. With the group of its base the same declaration is stored, and the history keeps the
    group it was made from."""
    ws = scoped_built
    scope = words("client.py", "BASE")
    for provenance in (
        None,
        DeclarationProvenance(via="api"),
        DeclarationProvenance(via="ui", note="by hand"),
    ):
        with pytest.raises(
            InvalidDeploymentLinkError, match="a scoped declaration is made from the worklist"
        ):
            CapabilityService(ws).deployment_link_create(
                DeploymentLinkCreateRequest(
                    context=repository_context(ws, "alpha"),
                    link=write_fields(ws, "caller", scope),
                    provenance=provenance,
                )
            )
    assert ws.deployment_links.list_all() == [] and audit_count(ws) == 0

    group = group_with(ws, "{BASE}").key
    made = declare_over_api(ws, "alpha", group, "caller", scope)
    assert made.scope == scope and made.url is None and made.repository == slug(ws, "alpha")
    [entry] = ws.deployment_links.history(link_id=made.id)
    assert (entry.action, entry.request_group) == ("create", group)


def test_a_receiver_rooted_base_needs_a_symbol_scope(scoped_built: Workspace) -> None:
    """``self.base_url`` names no single binding in a whole file: declared for ``client.py`` it is
    refused at write with the preview's reason, and nothing is stored or recorded. Declared for the
    class that holds it, the same base is stored."""
    ws = scoped_built
    group = group_with(ws, "{self.base_url}").key
    with pytest.raises(InvalidDeploymentLinkError, match="names no single binding in a whole file"):
        declare_over_api(ws, "alpha", group, "caller", words("client.py", "self.base_url"))
    assert ws.deployment_links.list_all() == [] and audit_count(ws) == 0

    by_class = words("client.py", "self.base_url", symbol="UsersClient")
    made = declare_over_api(ws, "alpha", group, "caller", by_class)
    assert made.scope == by_class and audit_count(ws) == 1


def test_a_file_scope_without_binding_evidence_is_refused_at_write(
    scoped_built: Workspace,
) -> None:
    """``BASE`` is a module binding that the file's functions read: declared for the whole file it
    is stored. ``base`` is only ever a parameter, so nothing in the file binds it and no request is
    shown to read one binding of it: the same whole-file declaration is refused (nothing would be
    joined), nothing is stored or recorded, and the function that takes it as a parameter can be
    named as a symbol instead."""
    ws = scoped_built
    bound = declare_over_api(
        ws, "alpha", group_with(ws, "{BASE}").key, "caller", words("client.py", "BASE")
    )
    assert bound.scope is not None and bound.scope.symbol is None
    assert audit_count(ws) == 1

    group = group_with(ws, "{base}").key
    with pytest.raises(InvalidDeploymentLinkError, match="nothing would be joined"):
        declare_over_api(ws, "alpha", group, "caller", words("client.py", "base"))
    assert [link.id for link in ws.deployment_links.list_all()] == [bound.id]
    assert audit_count(ws) == 1

    named = declare_over_api(
        ws, "alpha", group, "caller", words("client.py", "base", symbol="param_read")
    )
    assert named.scope is not None and named.scope.symbol == "param_read"
    assert audit_count(ws) == 2


def test_a_declaration_whose_base_key_or_shown_base_disagrees_with_its_group_is_refused(
    scoped_built: Workspace,
) -> None:
    """The scope's words are checked against the group the operator says they were made from. A
    base key that is not the group's base (another base of the same file, one nothing requests, a
    receiver) is refused with the preview's first requirement, word for word. A shown base that is
    not the one the group has (``{other}``, or the masked ``{…}`` for a plain name) is refused
    saying how the group's base is shown. Nothing is stored or recorded by any of them; the
    declaration with the group's own key and shown base is."""
    ws = scoped_built
    group = group_with(ws, "{BASE}").key
    for text in ("base", "nothing_reads_this", "self.base_url"):
        scope = words("client.py", text)
        expected = preview_of(
            ws, "{BASE}", "alpha", "caller", "client.py", text, base_shown=scope.base_shown
        )
        assert not expected.can_create and expected.requirements
        with pytest.raises(InvalidDeploymentLinkError) as refused:
            declare_over_api(ws, "alpha", group, "caller", scope)
        assert refused.value.message == expected.requirements[0], text

    for shown in ("{other}", "{…}"):
        disagreeing = DeclarationScope(
            path="client.py", symbol=None, base_key=base_key("BASE"), base_shown=shown
        )
        with pytest.raises(InvalidDeploymentLinkError) as refused:
            declare_over_api(ws, "alpha", group, "caller", disagreeing)
        assert refused.value.message == "the base of this group is shown as {BASE}", shown
    assert ws.deployment_links.list_all() == [] and audit_count(ws) == 0

    made = declare_over_api(ws, "alpha", group, "caller", words("client.py", "BASE"))
    assert made.scope == words("client.py", "BASE") and audit_count(ws) == 1


def test_an_update_of_a_scoped_declaration_is_revalidated(
    scoped_built: Workspace, tmp_path: Path
) -> None:
    """An update that names the group and keeps the scope is checked like a creation. While the
    service answers the covered requests it is stored and recorded as an update. After the service
    stops serving its routes and a sync, the same update is refused with the preview's reason, and
    the link and its history stay as they were."""
    ws = scoped_built
    scope = words("client.py", "BASE")
    group = group_with(ws, "{BASE}").key
    link = declare_over_api(ws, "alpha", group, "caller", scope, label="first")

    renamed = update_over_api(
        ws, "alpha", link.id, group, write_fields(ws, "caller", scope, label="renamed")
    )
    assert renamed.label == "renamed" and renamed.scope == scope
    assert [e.action for e in ws.deployment_links.history(link_id=link.id)] == ["update", "create"]

    (tmp_path / "repos" / "alpha" / "alpha" / "main.py").write_text("# no routes\n")
    resync(ws, tmp_path)
    with pytest.raises(InvalidDeploymentLinkError, match="serves no route"):
        update_over_api(
            ws, "alpha", link.id, group, write_fields(ws, "caller", scope, label="refused")
        )
    stored = ws.deployment_links.get(link.id)
    assert stored is not None and stored.label == "renamed" and stored.scope == scope
    assert audit_count(ws) == 2


def test_an_update_cannot_add_or_remove_a_scope(scoped_built: Workspace) -> None:
    """A scope is what the declaration was made about, its caller included: an update that gives a
    plain link a scope, takes it from a scoped one, changes any word of it (the base path, the
    symbol) or names another caller is refused, with or without a group, and the links and the
    history stay as they were. The same scope under another label is an ordinary update."""
    ws = scoped_built
    scope = words("client.py", "BASE")
    group = group_with(ws, "{BASE}").key
    caller = slug(ws, "caller")
    scoped_link = declare_over_api(ws, "alpha", group, "caller", scope)
    plain = (
        CapabilityService(ws)
        .deployment_link_create(
            DeploymentLinkCreateRequest(
                context=repository_context(ws, "alpha"),
                link=DeploymentLinkFields(label="plain", callers=[caller]),
                provenance=DeclarationProvenance(via="ui"),
            )
        )
        .link
    )
    assert plain.scope is None
    held = [ws.deployment_links.get(scoped_link.id), ws.deployment_links.get(plain.id)]
    rows = audit_count(ws)

    other_base_path = words("client.py", "BASE", base_path="/v2")
    other_symbol = words("client.py", "BASE", symbol="read_item")
    attempts = [
        (plain.id, write_fields(ws, "caller", scope, label="plain")),
        (scoped_link.id, DeploymentLinkFields(label="mapped", callers=[caller])),
        (scoped_link.id, write_fields(ws, "caller", other_base_path)),
        (scoped_link.id, write_fields(ws, "caller", other_symbol)),
        (scoped_link.id, write_fields(ws, "second", scope)),
    ]
    for link_id, fields in attempts:
        for named in (group, None):
            with pytest.raises(InvalidDeploymentLinkError, match="a scope cannot change"):
                update_over_api(ws, "alpha", link_id, named, fields)
    assert [ws.deployment_links.get(scoped_link.id), ws.deployment_links.get(plain.id)] == held
    assert audit_count(ws) == rows

    relabelled = update_over_api(
        ws, "alpha", scoped_link.id, group, write_fields(ws, "caller", scope, label="relabelled")
    )
    assert relabelled.label == "relabelled" and relabelled.scope == scope
    assert audit_count(ws) == rows + 1


def test_overlapping_scoped_declarations_naming_different_services_are_refused(
    scoped_built: Workspace,
) -> None:
    """One caller's base over one file cannot be two services. With ``alpha`` behind ``BASE`` in
    ``client.py``, the preview names that declaration as the conflict and the write is refused for
    another service over the whole file, over a symbol inside it, and for the same service at
    another base path; nothing is stored or recorded. Another file of the same base is not one."""
    ws = scoped_built
    group = group_with(ws, "{BASE}").key
    first = declare_over_api(ws, "alpha", group, "caller", words("client.py", "BASE"))
    rows = audit_count(ws)

    for served_by, scope in (
        ("beta", words("client.py", "BASE")),
        ("beta", words("client.py", "BASE", symbol="read_item")),
        ("alpha", words("client.py", "BASE", base_path="/v2")),
    ):
        preview = preview_of(
            ws,
            "{BASE}",
            served_by,
            "caller",
            "client.py",
            "BASE",
            symbol=scope.symbol,
            base_path=scope.base_path,
        )
        assert preview.conflicts_with == first.id and not preview.can_create
        with pytest.raises(InvalidDeploymentLinkError):
            declare_over_api(ws, served_by, group, "caller", scope)
    assert [link.id for link in ws.deployment_links.list_all()] == [first.id]
    assert audit_count(ws) == rows

    elsewhere = declare_over_api(ws, "beta", group, "caller", words("other.py", "BASE"))
    assert elsewhere.scope is not None and elsewhere.scope.path == "other.py"
    assert audit_count(ws) == rows + 1


def test_scoped_history_records_create_update_and_revoke_with_the_scope(
    scoped_built: Workspace,
) -> None:
    """The history of a scoped declaration keeps the scope in what each row recorded: the creation
    after it (with the group and the note), the update before and after it with the label that
    changed, the revocation before it and nothing after. The scope is its words only: the base key
    and the masked name, never a URL."""
    ws = scoped_built
    scope = words("client.py", "BASE")
    group = group_with(ws, "{BASE}").key
    alpha, caller = slug(ws, "alpha"), slug(ws, "caller")
    link = declare_over_api(ws, "alpha", group, "caller", scope, label="first", note="why")
    update_over_api(ws, "alpha", link.id, group, write_fields(ws, "caller", scope, label="second"))
    CapabilityService(ws).deployment_link_delete(
        DeploymentLinkDeleteRequest(
            context=repository_context(ws, "alpha"),
            id=link.id,
            provenance=DeclarationProvenance(via="ui", note="wrong service"),
        )
    )
    assert ws.deployment_links.list_all() == []

    entries = (
        CapabilityService(ws)
        .http_declaration_history(HttpDeclarationHistoryRequest(context=GLOBAL, id=link.id))
        .entries
    )
    assert [e.action for e in entries] == ["revoke", "update", "create"]
    revoke, update, create = entries
    shown = scope.model_dump()
    assert create.before is None and create.after is not None
    assert create.after["scope"] == shown and create.after["label"] == "first"
    assert (create.request_group, create.note) == (group, "why")
    assert update.before is not None and update.after is not None
    assert update.before["scope"] == update.after["scope"] == shown
    assert (update.before["label"], update.after["label"]) == ("first", "second")
    assert update.request_group == group
    assert revoke.before is not None and revoke.after is None
    assert revoke.before["scope"] == shown and revoke.before["label"] == "second"
    assert revoke.note == "wrong service"
    assert shown["base_shown"] == "{BASE}" and shown["base_key"] == base_key("BASE")
    for entry in entries:
        assert (entry.repository, entry.subject_id, entry.via) == (alpha, link.id, "ui")
        for fields in (entry.before, entry.after):
            if fields is not None:
                assert fields["url"] is None and fields["callers"] == [caller]


def test_a_scoped_declaration_stores_and_shows_no_base_expression_text_beyond_the_masked_name(
    scoped_built: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``vault.py`` writes a secret-like literal inside the call that is its request's base. The
    operator declares that base for its function, over the API, and updates the declaration. The
    literal, and the call it is in, are in none of: the stored ``scope_json``, every
    ``declaration_audit`` row, the worklist (the list, each group opened, the history) and the
    preview, and the links export. What is kept is the hash of the text and the masked name."""
    from repoatlas.cli.main import app
    from typer.testing import CliRunner

    ws = scoped_built
    asking = request_in(ws, all_requests(ws), "caller", "vault.py", "vault_read")
    assert asking.scopable and asking.base_key == base_key(SECRET_BASE)
    group = next(
        g.key
        for g in worklist(ws).groups
        if asking.decision_ref in {r.decision_ref for r in worklist(ws, group=g.key).requests}
    )
    scope = words("vault.py", SECRET_BASE, symbol="vault_read")
    assert scope.base_shown == "{…}"

    preview = CapabilityService(ws).http_connection_preview(
        HttpConnectionPreviewRequest(
            context=GLOBAL,
            group=group,
            repository=slug(ws, "alpha"),
            scope=PreviewScope(
                caller=slug(ws, "caller"),
                path="vault.py",
                symbol="vault_read",
                base_key=base_key(SECRET_BASE),
            ),
        )
    )
    assert preview.can_create and preview.counts == {"would-link": 1}
    link = declare_over_api(ws, "alpha", group, "caller", scope, note="the vault client")
    update_over_api(
        ws, "alpha", link.id, group, write_fields(ws, "caller", scope, label="relabelled")
    )

    monkeypatch.setenv("REPOATLAS_CONFIG", str(tmp_path / "config.yaml"))
    monkeypatch.setenv("REPOATLAS_DB", str(ws.settings.storage.database))
    target = tmp_path / "links.json"
    result = CliRunner().invoke(app, ["links", "export", str(target)])
    assert result.exit_code == 0, result.output

    history = CapabilityService(ws).http_declaration_history(
        HttpDeclarationHistoryRequest(context=GLOBAL)
    )
    opened_groups = [worklist(ws, group=g.key).model_dump_json() for g in worklist(ws).groups]
    stored = [row[0] for row in ws.database.execute("SELECT scope_json FROM deployment_links")]
    audited = [
        " ".join(str(value) for value in row)
        for row in ws.database.execute("SELECT * FROM declaration_audit")
    ]
    kept = {
        "scope_json": "\n".join(stored),
        "audit": "\n".join(audited),
        "worklist": worklist(ws).model_dump_json() + "".join(opened_groups),
        "history": history.model_dump_json(),
        "preview": preview.model_dump_json(),
        "export": target.read_text(encoding="utf-8"),
    }
    assert [e.action for e in history.entries] == ["update", "create"]
    for name, text in kept.items():
        assert SECRET_TOKEN not in text and "vault_url" not in text, name
    # not empty-handed: the declaration is there, by its hash and its masked name
    assert [json.loads(raw)["base_shown"] for raw in stored] == ["{…}"] and len(audited) == 2
    for name in ("scope_json", "audit", "export"):
        assert base_key(SECRET_BASE) in kept[name], name
    for name in ("audit", "export"):
        assert "{…}" in kept[name], name
    assert "vault_read" in kept["worklist"]


def test_a_scoped_declaration_is_not_a_deployment_place(
    scoped_built: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scoped declaration names a base in a caller's file, not a place its repository is
    deployed. Beside a link that states a URL, it is kept by the store and listed where
    declarations are, but the targets are built from the URL link alone and the Deployments answer
    lists the URL link alone: no target is made for it, and no resolution names it."""
    from repoatlas.graph.deployment_targets import build_targets as real_build_targets

    ws = scoped_built
    api = CapabilityService(ws)
    place = api.deployment_link_create(
        DeploymentLinkCreateRequest(
            context=repository_context(ws, "alpha"),
            link=DeploymentLinkFields(label="prod", url="https://alpha.example.test"),
        )
    ).link
    scoped_link = declare_over_api(
        ws, "alpha", group_with(ws, "{BASE}").key, "caller", words("client.py", "BASE")
    )
    assert {link.id for link in ws.deployment_links.list(slug(ws, "alpha"))} == {
        place.id,
        scoped_link.id,
    }

    built_from: list[list[int]] = []

    def spy(*args: Any, **kwargs: Any) -> Any:
        # (repository, claims, files, edges, links, context, ...): the fifth is the links
        built_from.append([link.id for link in args[4]])
        return real_build_targets(*args, **kwargs)

    monkeypatch.setattr("repoatlas.capabilities.service.build_targets", spy)
    response = api.deployment_targets(
        DeploymentTargetsRequest(context=repository_context(ws, "alpha"))
    )
    assert built_from == [[place.id]]
    assert [link.id for link in response.links] == [place.id]
    assert [t.pattern for t in response.targets if t.kind == "link"] == [
        "https://alpha.example.test"
    ]
    assert f"link_{scoped_link.id}" not in {t.target_id for t in response.targets}
    assert all(
        resolution.link_id != scoped_link.id
        for target in response.targets
        for resolution in target.resolutions
    )


def test_a_scoped_update_without_a_worklist_group_is_refused(scoped_built: Workspace) -> None:
    """An update of a scoped declaration is revalidated like its creation, so it names the worklist
    group, over the API as at creation. The same scope, caller and a new label with provenance that
    names no group is refused; the stored label is unchanged and the history keeps its one create
    row."""
    ws = scoped_built
    scope = words("client.py", "BASE")
    group = group_with(ws, "{BASE}").key
    link = declare_over_api(ws, "alpha", group, "caller", scope, label="first")
    assert audit_count(ws) == 1

    with pytest.raises(InvalidDeploymentLinkError, match="updated from the worklist"):
        update_over_api(
            ws, "alpha", link.id, None, write_fields(ws, "caller", scope, label="renamed")
        )
    stored = ws.deployment_links.get(link.id)
    assert stored is not None and stored.label == "first" and stored.scope == scope
    assert audit_count(ws) == 1
    assert [e.action for e in ws.deployment_links.history(link_id=link.id)] == ["create"]


def test_an_imported_scoped_declaration_whose_base_key_matches_no_request_joins_nothing(
    scoped_built: Workspace, tmp_path: Path
) -> None:
    """An import never passes the preview or the service, so nothing there checks that its base key
    names a base some request writes. The sync's join does: the declaration is read, covers no
    request (every request of its file is out of its scope), and joins and mentions nothing -- the
    stored decisions are byte for byte those of a sync without it. A declaration that does match,
    made beside it, still joins, so the first was read by the same join and refused by it."""
    ws = scoped_built
    before = decision_table(ws)
    assert not any(decision == LINKED for _, decision, _, _ in before)

    nothing = DeclarationScope(path="client.py", base_key="hbk1-" + "0" * 20, base_shown="{…}")
    stray = ws.deployment_links.create(
        slug(ws, "alpha"),
        DeploymentLinkFields(label="stray", callers=[slug(ws, "caller")], scope=nothing),
        via="import",
    ).id
    resync(ws, tmp_path)

    assert decision_table(ws) == before
    assert crossings(ws) == []
    for key, call in decided(ws).items():
        assert call.decision != LINKED, key
        assert call.evidence.get("link_id") != str(stray), key
        outcome = call.evidence.get("declaration_outcome")
        assert outcome is None or outcome in {
            SCOPE_TOO_BROAD,
            "scope-unidentified",
            "injected-root-unselected",
        }, key

    real = declare(ws, "alpha", "caller", "client.py", "BASE")
    resync(ws, tmp_path)
    joined = decided(ws)
    assert joined[(*CLIENT, "read_item")].decision == LINKED
    assert joined[(*CLIENT, "read_item")].evidence["link_id"] == str(real)
    assert all(call.evidence.get("link_id") != str(stray) for call in joined.values())


def test_an_imported_scoped_declaration_with_a_base_key_is_refused_only_by_the_join(
    scoped_built: Workspace, tmp_path: Path
) -> None:
    """The base key is one a request really writes (``BASE``), but the path is wrong: a file the
    caller does not have, and a file of the caller whose requests write other bases. The store
    accepts both as imports (each keeps its one ``import`` history row), so only the join refuses
    them: it concerns no request, joins nothing and leaves every stored decision as it was. The
    same words on the right file still join, so the key was a live one."""
    ws = scoped_built
    before = decision_table(ws)

    wrong = [
        declare(ws, "alpha", "caller", "missing.py", "BASE"),
        declare(ws, "alpha", "caller", "qualified.py", "BASE"),
    ]
    for link_id in wrong:
        assert [e.action for e in ws.deployment_links.history(link_id=link_id)] == ["import"]
    resync(ws, tmp_path)

    assert decision_table(ws) == before
    assert crossings(ws) == []
    assert not any(call.decision == LINKED for call in decided(ws).values())

    real = declare(ws, "alpha", "caller", "client.py", "BASE")
    resync(ws, tmp_path)
    joined = decided(ws)
    assert joined[(*CLIENT, "read_item")].decision == LINKED
    assert joined[(*CLIENT, "read_item")].evidence["link_id"] == str(real)
    assert all(
        call.evidence.get("link_id") not in {str(link_id) for link_id in wrong}
        for call in joined.values()
    )
