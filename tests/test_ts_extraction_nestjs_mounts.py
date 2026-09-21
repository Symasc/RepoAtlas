"""NestJS routes on the shared HTTP graph, only once their mount is proven.

A controller-local route becomes ``SERVES_HTTP_ROUTE`` when an application built
by ``NestFactory.create`` is proven to register the controller and its prefix is
known. An unknown — a prefix, an unreadable module, a routing construct — is never
read as "no effect", and it withholds only what it can reach. These tests run the
real sync pipeline over small NestJS repositories and read the stored graph.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.graph.linker import ReferenceLinker
from repoatlas.models.inspection import InspectRequest, InspectTarget
from repoatlas.models.search import SearchRequest
from repoatlas.services.sync_service import sync
from repoatlas.services.workspace import Workspace, open_workspace
from tests.conftest import fact_snapshot, git, init_git_repository, reindex_from_scratch
from tests.http_corpus import GLOBAL, corpus_settings, entity_key, http_nodes, relationships

MAIN = """\
import {{ NestFactory }} from "@nestjs/core";
import {{ AppModule }} from "./app.module";

async function bootstrap() {{
  const app = await NestFactory.create<Application>({root});
  {configure}
  await app.listen(3000);
}}

void bootstrap();
"""

APP_MODULE = """\
import {{ Module }} from "@nestjs/common";
import {{ ConfigModule }} from "@nestjs/config";
import {{ HealthController }} from "./health.controller";
import {{ UsersModule }} from "./users/users.module";
{extra_imports}
@Module({{
  imports: [ConfigModule.forRoot({{ isGlobal: true }}), {imports}],
  controllers: [HealthController],
}})
export class AppModule {{}}
"""

HEALTH = """\
import { Controller, Get } from "@nestjs/common";
import { MessagePattern } from "@nestjs/microservices";

@Controller()
export class HealthController {
  @Get("health")
  check() {}

  @MessagePattern("health.check")
  rpc() {}
}
"""

USERS_MODULE = """\
import {{ Module }} from "@nestjs/common";
import {{ UsersController }} from "./users.controller";

@Module({metadata})
export class UsersModule {{}}
"""

USERS = """\
import { Controller, Get, Post } from "@nestjs/common";
import { MessagePattern } from "@nestjs/microservices";

@Controller("v1")
export class UsersController {
  @Get("me")
  me() {}

  @Post("me/session")
  session() {}

  @MessagePattern("users.byId")
  byId() {}
}
"""

ORPHAN = """\
import { Controller, Get } from "@nestjs/common";

@Controller("v1/orphans")
export class OrphanController {
  @Get()
  list() {}
}
"""

#: A file that imports a construct able to re-route modules it never mentions.
ROUTING = 'import { RouterModule } from "@nestjs/core";\nexport { RouterModule };\n'
#: A file that imports a construct able to set any application's prefix.
CONFIGURING = 'import { ApplicationConfig } from "@nestjs/core";\nexport { ApplicationConfig };\n'

PROVEN = {
    ("HealthController.check", "http-route:GET:/health"),
    ("UsersController.me", "http-route:GET:/v1/me"),
    ("UsersController.session", "http-route:POST:/v1/me/session"),
}


def _api(
    root: Path,
    *,
    configure: str = "",
    main_root: str = "AppModule",
    imports: str = "UsersModule",
    extra_imports: str = "",
    users_metadata: str = "{ controllers: [UsersController] }",
    extra: dict[str, str] | None = None,
) -> Path:
    repo = root / "api"
    files = {
        "package.json": json.dumps({"name": "api", "version": "1.0.0"}),
        "src/main.ts": MAIN.format(root=main_root, configure=configure),
        "src/app.module.ts": APP_MODULE.format(imports=imports, extra_imports=extra_imports),
        "src/health.controller.ts": HEALTH,
        "src/users/users.module.ts": USERS_MODULE.format(metadata=users_metadata),
        "src/users/users.controller.ts": USERS,
        "src/orphan.controller.ts": ORPHAN,
        **(extra or {}),
    }
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    return repo


def _index(tmp_path: Path, **kwargs: Any) -> tuple[Workspace, Path]:
    from repoatlas.services.init_service import initialize

    root = tmp_path / "repos"
    repo = _api(root, **kwargs)
    init_git_repository(repo)
    workspace = open_workspace(settings=corpus_settings(tmp_path / "state"), require_root=False)
    initialize(workspace, root, config_path=tmp_path / "config.yaml")
    return workspace, root


Index = Callable[..., tuple[Workspace, Path]]


@pytest.fixture
def index(tmp_path: Path) -> Iterator[Index]:
    """``_index`` for one test; every workspace it opens is closed when the test ends."""
    opened: list[Workspace] = []

    def make(*args: Any, **kwargs: Any) -> tuple[Workspace, Path]:
        indexed = _index(tmp_path, *args, **kwargs)
        opened.append(indexed[0])
        return indexed

    try:
        yield make
    finally:
        for workspace in opened:
            workspace.close()


def _served(workspace: Workspace) -> set[tuple[str, str]]:
    rows = workspace.database.execute(
        "SELECT src.qualified_name AS handler, dst.key AS route FROM edges e "
        "JOIN entities src ON src.id = e.src_id JOIN entities dst ON dst.id = e.dst_id "
        "WHERE e.type = 'SERVES_HTTP_ROUTE'"
    ).fetchall()
    return {(row["handler"], row["route"]) for row in rows}


def _pending_routes(workspace: Workspace) -> int:
    rows = workspace.database.execute("SELECT meta_json FROM unresolved_refs").fetchall()
    return sum(
        1
        for row in rows
        if json.loads(row["meta_json"] or "{}").get("pending_fact") == "nestjs-route"
    )


def _outcomes(workspace: Workspace) -> dict[str, str]:
    """Why each declared route was, or was not, published — keyed by its handler."""
    plan = ReferenceLinker(workspace.knowledge).prepare_global()
    rows = workspace.database.execute(
        "SELECT u.id, src.qualified_name AS handler FROM unresolved_refs u "
        "JOIN entities src ON src.id = u.src_id"
    ).fetchall()
    handlers = {row["id"]: row["handler"] for row in rows}
    return {handlers[claim]: outcome for claim, outcome in plan.http.routes.items()}


def _commit(repo: Path, message: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", message)


def _sync(workspace: Workspace, root: Path) -> None:
    sync(workspace, workspace.resolver().resolve(root))


def _equals_full_reindex(workspace: Workspace, root: Path, tmp_path: Path, tag: str) -> None:
    fresh = reindex_from_scratch(corpus_settings(tmp_path / "unused"), root, tmp_path, tag)
    assert fact_snapshot(workspace) == fresh


# -- proven -------------------------------------------------------------------------------


def test_a_proven_application_publishes_every_route_it_registers(index: Index) -> None:
    workspace, _ = index()
    assert _served(workspace) == PROVEN

    rows = workspace.database.execute(
        "SELECT src.qualified_name AS handler, e.line_start, e.line_end, e.parser, "
        "r.slug, f.rel_path FROM edges e JOIN entities src ON src.id = e.src_id "
        "JOIN repositories r ON r.id = e.repo_id JOIN files f ON f.id = e.file_id "
        "WHERE e.type = 'SERVES_HTTP_ROUTE'"
    ).fetchall()
    by_handler = {row["handler"]: row for row in rows}
    # Repository-owned, in the controller's own file.
    assert {(row["slug"], row["rel_path"]) for row in rows} == {
        ("api", "src/health.controller.ts"),
        ("api", "src/users/users.controller.ts"),
    }
    # One decorator states the whole of `GET /health`, so it is citable there.
    health = by_handler["HealthController.check"]
    assert (health["line_start"], health["line_end"]) == (6, 6)
    # `GET /v1/me` is composed from two decorators and a proven mount: uncitable.
    me = by_handler["UsersController.me"]
    assert (me["line_start"], me["line_end"]) == (None, None)
    assert {row["parser"] for row in rows} == {"linker:global"}


def test_message_handlers_and_unregistered_controllers_stay_off_the_graph(
    index: Index,
) -> None:
    workspace, _ = index()
    handlers = {handler for handler, _ in _served(workspace)}
    assert not handlers & {"HealthController.rpc", "UsersController.byId", "OrphanController.list"}
    # The orphan's declaration is still recorded, and says why it is not a fact.
    assert _pending_routes(workspace) == 4
    assert _outcomes(workspace)["OrphanController.list"] == (
        "not registered by a proven application"
    )


def test_registration_entries_become_references(index: Index) -> None:
    workspace, _ = index()
    rows = workspace.database.execute(
        "SELECT src.qualified_name AS src, dst.qualified_name AS dst FROM edges e "
        "JOIN entities src ON src.id = e.src_id JOIN entities dst ON dst.id = e.dst_id "
        "WHERE e.type = 'REFERENCES' AND e.parser = 'linker:global'"
    ).fetchall()
    assert {(row["src"], row["dst"]) for row in rows} == {
        ("bootstrap", "AppModule"),
        ("AppModule", "HealthController"),
        ("AppModule", "UsersModule"),
        ("UsersModule", "UsersController"),
    }


def test_a_literal_global_prefix_is_composed_into_every_route(index: Index) -> None:
    workspace, _ = index(configure='app.setGlobalPrefix("api");')
    assert _served(workspace) == {
        ("HealthController.check", "http-route:GET:/api/health"),
        ("UsersController.me", "http-route:GET:/api/v1/me"),
        ("UsersController.session", "http-route:POST:/api/v1/me/session"),
    }


def test_a_package_module_handed_nothing_blocks_nothing(index: Index) -> None:
    workspace, _ = index(
        extra_imports='import { MetricsModule } from "@acme/metrics";\n',
        imports="UsersModule, MetricsModule, MetricsModule.forService('api')",
    )
    assert _served(workspace) == PROVEN


def test_two_applications_in_one_repository_each_serve_their_own(index: Index) -> None:
    admin = {
        "admin/package.json": json.dumps({"name": "admin"}),
        "admin/src/main.ts": (
            'import { NestFactory } from "@nestjs/core";\n'
            'import { AdminModule } from "./admin.module";\n'
            "async function start() {\n"
            "  const app = await NestFactory.create(AdminModule);\n"
            '  app.setGlobalPrefix("admin");\n'
            "}\n"
        ),
        "admin/src/admin.module.ts": (
            'import { Module } from "@nestjs/common";\n'
            'import { AuditController } from "./audit.controller";\n'
            "@Module({ controllers: [AuditController] })\nexport class AdminModule {}\n"
        ),
        "admin/src/audit.controller.ts": (
            'import { Controller, Get } from "@nestjs/common";\n'
            '@Controller("audit")\nexport class AuditController {\n  @Get()\n  list() {}\n}\n'
        ),
    }
    workspace, _ = index(extra=admin)
    assert _served(workspace) == PROVEN | {("AuditController.list", "http-route:GET:/admin/audit")}


# -- withheld: what an unknown can reach ------------------------------------------------


@pytest.mark.parametrize(
    ("configure", "reason"),
    [
        ("app.setGlobalPrefix(process.env.PREFIX);", "prefix not proven: global prefix"),
        ("app.enableVersioning();", "prefix not proven: versioning"),
        ("configure(app);", "prefix not proven: application escapes"),
    ],
)
def test_an_unproven_prefix_withholds_every_route_of_that_application(
    index: Index, configure: str, reason: str
) -> None:
    workspace, _ = index(configure=configure)
    assert _served(workspace) == set()
    assert http_nodes(workspace) == set()
    assert _pending_routes(workspace) == 4
    assert _outcomes(workspace)["UsersController.me"] == reason


def test_a_dynamically_registered_root_proves_nothing(index: Index) -> None:
    workspace, _ = index(main_root="AppModule.forRoot()")
    assert _served(workspace) == set()
    assert _outcomes(workspace)["HealthController.check"] == (
        "not registered by a proven application"
    )


def test_an_unresolved_in_repository_import_withholds_the_whole_application(
    index: Index,
) -> None:
    workspace, _ = index(
        extra_imports='import { BillingModule } from "./billing/billing.module";\n',
        imports="UsersModule, BillingModule",
    )
    assert _served(workspace) == set()
    assert (
        _outcomes(workspace)["HealthController.check"] == "import 'BillingModule' does not resolve"
    )


def test_unreadable_metadata_on_the_path_withholds_the_whole_application(
    index: Index,
) -> None:
    workspace, _ = index(users_metadata="{ ...BASE, controllers: [UsersController] }")
    assert _served(workspace) == set()


def test_a_registered_router_module_withholds_the_whole_application(index: Index) -> None:
    workspace, _ = index(
        extra_imports='import { RouterModule } from "@nestjs/core";\n',
        imports="UsersModule, RouterModule.register([{ path: 'x', module: UsersModule }])",
    )
    assert _served(workspace) == set()


def test_a_module_handed_to_a_dynamic_module_withholds_only_its_own_controllers(
    index: Index,
) -> None:
    workspace, _ = index(
        extra_imports='import { AuthModule } from "@acme/auth";\n',
        imports="UsersModule, AuthModule.forRootAsync({ imports: [UsersModule] })",
    )
    assert _served(workspace) == {("HealthController.check", "http-route:GET:/health")}
    assert _outcomes(workspace)["UsersController.me"] == (
        "its module is handed to a dynamic module that may re-prefix it"
    )


def test_router_module_in_the_package_withholds_its_applications(index: Index) -> None:
    workspace, _ = index(
        extra={"src/routing.ts": ROUTING},
    )
    assert _served(workspace) == set()
    assert _outcomes(workspace)["HealthController.check"] == (
        "RouterModule is imported in this package"
    )


def test_router_module_in_another_package_does_not_touch_this_one(index: Index) -> None:
    workspace, _ = index(
        extra={
            "tools/package.json": json.dumps({"name": "tools"}),
            "tools/src/routing.ts": ROUTING,
        },
    )
    assert _served(workspace) == PROVEN


def test_application_config_anywhere_in_the_repository_withholds_every_application(
    index: Index,
) -> None:
    workspace, _ = index(
        extra={
            "tools/package.json": json.dumps({"name": "tools"}),
            "tools/src/prefix.ts": CONFIGURING,
        },
    )
    assert _served(workspace) == set()


# -- lifecycle ----------------------------------------------------------------------------


def test_unregistering_a_module_withdraws_its_routes_and_registering_restores_them(
    index: Index,
    tmp_path: Path,
) -> None:
    workspace, root = index()
    repo = root / "api"

    (repo / "src/app.module.ts").write_text(
        APP_MODULE.format(imports="", extra_imports="").replace(
            'import { UsersModule } from "./users/users.module";\n', ""
        )
    )
    _commit(repo, "unregister users")
    _sync(workspace, root)
    assert _served(workspace) == {("HealthController.check", "http-route:GET:/health")}
    assert "http-route:GET:/v1/me" not in http_nodes(workspace)
    assert _pending_routes(workspace) == 4
    _equals_full_reindex(workspace, root, tmp_path, "unregistered")

    (repo / "src/app.module.ts").write_text(
        APP_MODULE.format(imports="UsersModule", extra_imports="")
    )
    _commit(repo, "register users again")
    _sync(workspace, root)
    assert _served(workspace) == PROVEN
    _equals_full_reindex(workspace, root, tmp_path, "registered")


def test_deleting_a_controller_leaves_no_obsolete_route(index: Index, tmp_path: Path) -> None:
    workspace, root = index()
    repo = root / "api"
    git(repo, "rm", "--quiet", "src/users/users.controller.ts")
    _commit(repo, "remove the users controller")
    _sync(workspace, root)

    assert _served(workspace) == {("HealthController.check", "http-route:GET:/health")}
    assert http_nodes(workspace) == {"http-route:GET:/health"}
    _equals_full_reindex(workspace, root, tmp_path, "deleted")


def test_changing_the_prefix_moves_every_route(index: Index, tmp_path: Path) -> None:
    workspace, root = index()
    repo = root / "api"
    (repo / "src/main.ts").write_text(
        MAIN.format(root="AppModule", configure='app.setGlobalPrefix("v2");')
    )
    _commit(repo, "prefix the application")
    _sync(workspace, root)

    assert {route for _, route in _served(workspace)} == {
        "http-route:GET:/v2/health",
        "http-route:GET:/v2/v1/me",
        "http-route:POST:/v2/v1/me/session",
    }
    assert http_nodes(workspace) == {route for _, route in _served(workspace)}
    _equals_full_reindex(workspace, root, tmp_path, "prefixed")


# -- through the public capabilities ------------------------------------------------------


def test_a_proven_route_leads_to_its_handler_and_where_it_is_written(index: Index) -> None:
    workspace, _ = index()
    response = relationships(workspace, "http-route:GET:/v1/me")
    [edge] = [e for e in response.edges if str(e.type) == "SERVES_HTTP_ROUTE"]
    handler = edge.source
    assert handler.key == entity_key(workspace, "api", "me")
    assert (handler.repository_slug, handler.path) == ("api", "src/users/users.controller.ts")
    assert (handler.line_start, handler.line_end) == (7, 7)
    assert edge.target.key == "http-route:GET:/v1/me"


def test_an_unproven_controller_is_found_by_search_and_read_by_inspect(index: Index) -> None:
    workspace, _ = index()
    service = CapabilityService(workspace)
    controller = entity_key(workspace, "api", "OrphanController")
    handler = entity_key(workspace, "api", "list")

    found = service.search(SearchRequest(question="OrphanController", context=GLOBAL))
    keys = {item.entity.key for item in found.evidence if item.entity is not None}
    keys |= {handle.key for handle in found.entities}
    assert controller in keys

    inspected = service.inspect(
        InspectRequest(context=GLOBAL, target=InspectTarget(entity_key=handler))
    )
    text = "\n".join(segment.text for segment in inspected.segments)
    # The declaration is readable verbatim: controller path, method decorator, handler.
    assert '@Controller("v1/orphans")' in text
    assert "@Get()" in text
    assert "list() {}" in text


def test_an_unproven_handler_has_no_route_among_its_relationships(index: Index) -> None:
    workspace, _ = index()
    response = relationships(workspace, entity_key(workspace, "api", "list"))
    assert all(str(e.type) != "SERVES_HTTP_ROUTE" for e in response.edges)
