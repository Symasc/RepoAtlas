"""NestJS declarations, as one file states them (HTTP linkage).

A NestJS route is a fact only once an application is proven to mount its
controller, and that is decided across files. So a single file yields
*declarations*: pending claims the global pass reads together. These tests pin
what one file may declare, and — as carefully — what it may not: a lookalike
decorator, a message handler, a path this cannot read.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from repoatlas.models.parsing import EdgeType, EntityType, ParsedReference
from repoatlas.parsers.http_nestjs import LIMITATION, nest_path
from repoatlas.parsers.typescript import TypeScriptParser

COMMON = (
    "import { Controller, Get, Post, Put, Patch, Delete, Options, Head, All, Module, Version, "
    'forwardRef } from "@nestjs/common";\n'
)
CORE = 'import { NestFactory } from "@nestjs/core";\n'
CREATE = "const app = await NestFactory.create(AppModule);\n"


def _claims(source: str, role: str, name: str = "app.ts") -> list[ParsedReference]:
    parsed = TypeScriptParser().parse(Path(name), textwrap.dedent(source))
    return [r for r in parsed.references if r.meta.get("pending_fact") == role]


def _routes(source: str, name: str = "app.ts") -> dict[str, str | None]:
    """Controller-local route → the handler that declares it."""
    return {r.target_key: r.source_symbol for r in _claims(source, "nestjs-route", name)}


def _http_facts(source: str) -> list[ParsedReference]:
    parsed = TypeScriptParser().parse(Path("app.ts"), textwrap.dedent(source))
    return [
        r
        for r in parsed.references
        if r.target_kind
        in {EntityType.HTTP_ROUTE, EntityType.HTTP_DEPLOYMENT, EntityType.HTTP_ENDPOINT}
    ]


# -- composition ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fragments", "expected"),
    [
        (("v1/courses", ":courseId"), "/v1/courses/:courseId"),
        (("v1", ""), "/v1"),
        (("", "health"), "/health"),
        (("", ""), "/"),
        (("/v1/", "/me"), "/v1/me"),
        (("v1/", ""), "/v1"),
        (("api", "v1", "me"), "/api/v1/me"),
        (("/", "v1"), "/v1"),
    ],
)
def test_fragments_compose_the_way_nestjs_composes_them(
    fragments: tuple[str, ...], expected: str
) -> None:
    assert nest_path(*fragments) == expected


@pytest.mark.parametrize("fragment", ["*", "a/*", "(.*)", "a+", "{id}", "[x]", "a?"])
def test_a_pattern_is_not_a_route_identity(fragment: str) -> None:
    assert nest_path("v1", fragment) is None


# -- routes -------------------------------------------------------------------------------


def test_a_controller_prefix_and_a_method_path_declare_one_local_route() -> None:
    [claim] = _claims(
        COMMON
        + """
        @Controller("v1/courses")
        export class CoursesController {
          @Get(":courseId")
          detail() {}
        }
        """,
        "nestjs-route",
    )
    assert claim.edge_type is EdgeType.SERVES_HTTP_ROUTE
    assert claim.target_kind is EntityType.HTTP_ROUTE
    assert claim.target_key == "GET /v1/courses/:courseId"
    assert claim.source_symbol == "CoursesController.detail"
    # The span is the method decorator, where the method and its path are written.
    assert (claim.line_start, claim.line_end) == (5, 5)
    assert claim.meta == {
        "pending_fact": "nestjs-route",
        "source": "nestjs",
        "provenance": "composed",
        "method": "GET",
        "controller": "CoursesController",
        "controller_path": "v1/courses",
        "method_path": ":courseId",
        "limitation": LIMITATION,
    }


@pytest.mark.parametrize(
    ("controller", "method", "route", "provenance"),
    [
        ("@Controller()", "@Get()", "GET /", "exact"),
        ("@Controller()", '@Get("health")', "GET /health", "exact"),
        ('@Controller("v1")', "@Get()", "GET /v1", "composed"),
        ('@Controller({ path: "v1" })', '@Get("me")', "GET /v1/me", "composed"),
        ("@Controller('v1/')", "@Get('/me/')", "GET /v1/me", "composed"),
    ],
)
def test_omitted_paths_follow_nestjs(
    controller: str, method: str, route: str, provenance: str
) -> None:
    [claim] = _claims(
        COMMON + f"{controller}\nclass C {{\n  {method}\n  h() {{}}\n}}\n", "nestjs-route"
    )
    assert claim.target_key == route
    assert claim.meta["provenance"] == provenance


@pytest.mark.parametrize(
    ("decorator", "method"),
    [
        ("Get", "GET"),
        ("Post", "POST"),
        ("Put", "PUT"),
        ("Patch", "PATCH"),
        ("Delete", "DELETE"),
        ("Options", "OPTIONS"),
        ("Head", "HEAD"),
    ],
)
def test_every_method_decorator_maps_to_its_method(decorator: str, method: str) -> None:
    source = COMMON + f'@Controller("x")\nclass C {{\n  @{decorator}(":id")\n  h() {{}}\n}}\n'
    assert _routes(source) == {f"{method} /x/:id": "C.h"}


def test_all_has_no_method_identity_and_declares_nothing() -> None:
    assert _routes(COMMON + '@Controller("x")\nclass C {\n  @All("y")\n  h() {}\n}\n') == {}


def test_route_parameters_are_kept_exactly_as_written() -> None:
    source = (
        COMMON
        + '@Controller("v1/courses/:courseId/roster")\nclass R {\n'
        + '  @Post(":studentId/remove")\n  remove() {}\n}\n'
    )
    assert _routes(source) == {"POST /v1/courses/:courseId/roster/:studentId/remove": "R.remove"}


def test_every_handler_of_every_controller_in_a_file_is_declared() -> None:
    source = (
        COMMON
        + """
        @Controller("v1")
        export class UsersController {
          @Get("me")
          me() {}

          @Post("me/session")
          @HttpCode(200)
          session() {}

          helper() {}
        }

        @Controller("v1/devices")
        export class DevicesController {
          @Get()
          list() {}

          @Delete(":deviceId")
          revoke() {}
        }
        """
    )
    assert _routes(source) == {
        "GET /v1/me": "UsersController.me",
        "POST /v1/me/session": "UsersController.session",
        "GET /v1/devices": "DevicesController.list",
        "DELETE /v1/devices/:deviceId": "DevicesController.revoke",
    }


def test_decorators_imported_under_other_names_are_still_nestjs() -> None:
    source = (
        'import { Controller as Api, Get as Read } from "@nestjs/common";\n'
        '@Api("v1")\nexport class C {\n  @Read("x")\n  h() {}\n}\n'
    )
    assert _routes(source) == {"GET /v1/x": "C.h"}


def test_a_javascript_controller_is_read_the_same_way() -> None:
    source = (
        'import { Controller, Get } from "@nestjs/common";\n'
        '@Controller("v1")\nexport class C {\n  @Get("x")\n  h() {}\n}\n'
    )
    assert _routes(source, "app.js") == {"GET /v1/x": "C.h"}


# -- what is not a route ----------------------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [
        # A local function that happens to be called Get and Controller.
        "function Controller(p?: string) { return () => {}; }\n"
        "function Get(p?: string) { return () => {}; }\n",
        # The same names from somewhere that is not NestJS.
        'import { Controller, Get } from "./decorators";\n',
        'import { Controller, Get } from "@nestjs/commonx";\n',
        # A namespace import is not read.
        'import * as Controller from "@nestjs/common";\nimport * as Get from "@nestjs/common";\n',
        # A type-only import binds no value.
        'import type { Controller, Get } from "@nestjs/common";\n',
    ],
)
def test_a_lookalike_decorator_declares_nothing(header: str) -> None:
    source = header + '@Controller("v1")\nclass C {\n  @Get("x")\n  h() {}\n}\n'
    assert _routes(source) == {}


def test_a_nestjs_name_bound_twice_is_not_proven() -> None:
    source = (
        'import { Controller, Get } from "@nestjs/common";\n'
        "function wrap(Get: unknown) { return Get; }\n"
        '@Controller("v1")\nclass C {\n  @Get("x")\n  h() {}\n}\n'
    )
    assert _routes(source) == {}


def test_http_decorators_outside_a_controller_declare_nothing() -> None:
    source = (
        COMMON
        + '@Injectable()\nclass S {\n  @Get("x")\n  h() {}\n}\n'
        + 'class P {\n  @Get("y")\n  h() {}\n}\n'
    )
    assert _routes(source) == {}


@pytest.mark.parametrize(
    ("controller", "method"),
    [
        ("@Controller(prefix)", '@Get("x")'),
        ('@Controller(["a", "b"])', '@Get("x")'),
        ('@Controller({ path: "v1", version: V2 })', '@Get("x")'),
        ('@Controller({ path: "v1", version: [] })', '@Get("x")'),
        ('@Controller({ path: "v1", version: ["1", ["2"]] })', '@Get("x")'),
        ('@Controller({ path: "v1", version: "" })', '@Get("x")'),
        ('@Controller({ path: "v1", host: "admin.example.com" })', '@Get("x")'),
        ('@Controller("v1")', '@Get("x")\n  @Version(v)'),
        ('@Controller("v1")', '@Get("x")\n  @Version("1", "2")'),
        ('@Controller("v1")', '@Get("x")\n  @Version("1")\n  @Version("2")'),
        ('@Controller("v1", "extra")', '@Get("x")'),
        ('@Controller("v1")', "@Get(path)"),
        ('@Controller("v1")', '@Get(["a", "b"])'),
        ('@Controller("v1")', "@Get(`x/${id}`)"),
        ('@Controller("v1")', '@Get("files/*")'),
        ('@Controller("v1")', '@Get("a/(.*)")'),
        ('@Controller("v1")\n@Version("2")', '@Get("x")'),
    ],
)
def test_a_path_that_cannot_be_read_declares_nothing(controller: str, method: str) -> None:
    source = COMMON + f"{controller}\nclass C {{\n  {method}\n  h() {{}}\n}}\n"
    assert _routes(source) == {}


NEUTRAL = 'import { VERSION_NEUTRAL } from "@nestjs/common";\n'


@pytest.mark.parametrize(
    ("controller", "method", "version"),
    [
        ('@Controller("v1")', '@Get("x")', None),
        ('@Controller({ path: "v1", version: "2" })', '@Get("x")', ["2"]),
        ('@Controller({ path: "v1", version: ["1", "2", "1"] })', '@Get("x")', ["1", "2"]),
        ('@Controller({ path: "v1", version: VERSION_NEUTRAL })', '@Get("x")', [None]),
        # a handler's own version replaces its controller's
        ('@Controller({ path: "v1", version: "2" })', '@Get("x")\n  @Version("3")', ["3"]),
        (
            '@Controller("v1")',
            '@Get("x")\n  @Version([VERSION_NEUTRAL, "4"])',
            [None, "4"],
        ),
    ],
)
def test_a_stated_version_is_recorded_on_the_route(
    controller: str, method: str, version: list[str | None] | None
) -> None:
    source = COMMON + NEUTRAL + f"{controller}\nclass C {{\n  {method}\n  h() {{}}\n}}\n"
    [claim] = _claims(source, "nestjs-route")
    assert claim.target_key == "GET /v1/x"
    recorded = claim.meta.get("version")
    assert (json.loads(recorded) if recorded is not None else None) == version


def test_a_neutral_version_must_be_the_nestjs_one() -> None:
    source = (
        COMMON
        + 'import { VERSION_NEUTRAL } from "./versions";\n'
        + '@Controller({ path: "v1", version: VERSION_NEUTRAL })\n'
        + 'class C {\n  @Get("x")\n  h() {}\n}\n'
    )
    assert _routes(source) == {}


@pytest.mark.parametrize("modifier", ["static h() {}", "get h() { return 1; }", "*h() {}"])
def test_accessors_generators_and_static_methods_are_never_handlers(modifier: str) -> None:
    source = COMMON + f'@Controller("v1")\nclass C {{\n  @Get("x")\n  {modifier}\n}}\n'
    assert _routes(source) == {}


def test_a_message_handler_is_never_an_http_route() -> None:
    source = (
        COMMON
        + 'import { MessagePattern, EventPattern, Payload } from "@nestjs/microservices";\n'
        + """
        @Controller("v1")
        export class UsersController {
          @MessagePattern(IDENTITY_PATTERNS.getUserById)
          getUserById(@Payload() payload: GetUserByIdRequest) {}

          @MessagePattern("users.get")
          byLiteral() {}

          @EventPattern("users.created")
          created() {}

          @Get("me")
          me() {}
        }
        """
    )
    assert _routes(source) == {"GET /v1/me": "UsersController.me"}
    assert [r.target_key for r in _http_facts(source)] == ["GET /v1/me"]


def test_a_hybrid_handler_declares_only_its_http_route() -> None:
    source = (
        COMMON
        + 'import { MessagePattern } from "@nestjs/microservices";\n'
        + '@Controller()\nclass H {\n  @MessagePattern("health.check")\n'
        + '  @Get("health")\n  check() {}\n}\n'
    )
    assert _routes(source) == {"GET /health": "H.check"}


def test_no_nestjs_route_reaches_a_shared_node_from_one_file() -> None:
    source = COMMON + '@Controller("v1")\nclass C {\n  @Get("x")\n  h() {}\n}\n'
    [fact] = _http_facts(source)
    # A pending claim, not a route fact: it carries the role the linker keeps.
    assert fact.meta["pending_fact"] == "nestjs-route"


# -- modules ------------------------------------------------------------------------------


def _entries(source: str, role: str) -> list[tuple[str, dict[str, str]]]:
    return [
        (r.target_key, {k: v for k, v in r.meta.items() if k != "pending_fact"})
        for r in _claims(source, role)
    ]


def test_a_module_declares_itself_its_controllers_and_its_imports() -> None:
    source = (
        COMMON
        + 'import { UsersController } from "./users.controller";\n'
        + 'import { DatabaseModule } from "../database/database.module";\n'
        + 'import { ConfigModule } from "@nestjs/config";\n'
        + """
        @Controller("v1")
        class LocalController {
          @Get("x")
          h() {}
        }

        @Module({
          imports: [
            DatabaseModule,
            ConfigModule.forRoot({ isGlobal: true }),
            forwardRef(() => DatabaseModule),
          ],
          controllers: [UsersController, LocalController, ...more],
          providers: [UsersService],
        })
        export class UsersModule {}
        """
    )
    [module] = _claims(source, "nestjs-module")
    assert (module.target_key, module.source_symbol) == ("UsersModule", "UsersModule")
    assert module.meta["readable"] == "true"
    # A spread entry in `controllers` is never registered as far as this can prove.
    assert _entries(source, "nestjs-controller") == [
        ("UsersController", {"specifier": "./users.controller", "imported": "UsersController"}),
        ("LocalController", {"local": "true"}),
    ]
    imports = _entries(source, "nestjs-import")
    assert imports[0] == (
        "DatabaseModule",
        {
            "kind": "module",
            "specifier": "../database/database.module",
            "imported": "DatabaseModule",
        },
    )
    target, dynamic = imports[1]
    assert target == "ConfigModule.forRoot({ isGlobal: true })"
    assert dynamic["kind"] == "dynamic"
    assert (dynamic["callee"], dynamic["callee_specifier"]) == ("ConfigModule", "@nestjs/config")
    assert json.loads(dynamic["arguments"]) == {}
    # `forwardRef(() => X)` names X exactly as X would.
    assert imports[2][0] == "DatabaseModule"
    assert imports[2][1]["kind"] == "module"


def test_a_module_handed_to_a_dynamic_module_is_recorded_as_handed() -> None:
    source = (
        COMMON
        + 'import { AuthorizationModule } from "@lvl/auth";\n'
        + 'import { CommandsModule } from "./commands.module";\n'
        + "@Module({\n"
        + "  imports: [AuthorizationModule.forRootAsync({ imports: [CommandsModule] })],\n"
        + "})\nexport class AppModule {}\n"
    )
    [(_, meta)] = _entries(source, "nestjs-import")
    assert meta["kind"] == "dynamic"
    assert json.loads(meta["arguments"]) == {
        "CommandsModule": {"specifier": "./commands.module", "imported": "CommandsModule"}
    }


def test_a_constant_holding_a_dynamic_module_is_read_as_that_dynamic_module() -> None:
    source = (
        COMMON
        + "@Module({})\nclass CoreModule {}\n"
        + "const CORE = CoreModule.forRoot();\n"
        + "@Module({ imports: [CORE] })\nexport class CommandsModule {}\n"
    )
    [(target, meta)] = _entries(source, "nestjs-import")
    assert target == "CORE"
    assert (meta["kind"], meta["const"], meta["callee"], meta["callee_local"]) == (
        "dynamic",
        "CORE",
        "CoreModule",
        "true",
    )


@pytest.mark.parametrize(
    ("imports", "kind"),
    [
        ("[...SHARED]", "unknown"),
        ("SHARED", "unknown"),
        ("[RouterModule.register([{ path: 'admin', module: AdminModule }])]", "router"),
    ],
)
def test_an_import_entry_that_cannot_be_read_is_recorded_not_dropped(
    imports: str, kind: str
) -> None:
    source = (
        COMMON
        + 'import { RouterModule } from "@nestjs/core";\n'
        + f"@Module({{ imports: {imports} }})\nexport class AppModule {{}}\n"
    )
    assert [meta["kind"] for _, meta in _entries(source, "nestjs-import")] == [kind]


def test_unreadable_module_metadata_is_declared_unreadable() -> None:
    source = COMMON + "@Module({ ...BASE, controllers: [X] })\nexport class AppModule {}\n"
    [module] = _claims(source, "nestjs-module")
    assert module.meta["readable"] == "false"
    assert _claims(source, "nestjs-controller") == []


# -- application roots --------------------------------------------------------------------


def _root(body: str, header: str = COMMON + CORE) -> ParsedReference:
    [claim] = _claims(header + 'import { AppModule } from "./app.module";\n' + body, "nestjs-root")
    return claim


def test_an_application_root_and_its_proven_prefix() -> None:
    claim = _root(
        """
        async function bootstrap() {
          const app = await NestFactory.create<NestExpressApplication>(AppModule);
          app.enableCors(corsOptions());
          app.setGlobalPrefix("api");
          app.connectMicroservice(options, { inheritAppConfig: true });
          await app.startAllMicroservices();
          await app.listen(3000);
        }
        """
    )
    assert claim.edge_type is EdgeType.REFERENCES
    assert claim.target_kind is EntityType.CLASS
    assert (claim.target_key, claim.source_symbol) == ("AppModule", "bootstrap")
    assert claim.meta == {
        "pending_fact": "nestjs-root",
        "kind": "module",
        "specifier": "./app.module",
        "imported": "AppModule",
        "prefix_status": "proven",
        "prefix": "api",
    }


@pytest.mark.parametrize(
    "create",
    [
        "const app = await NestFactory.create(AppModule);",
        "const app = await NestFactory.create<X>(AppModule, { cors: true });",
        "const app = NestFactory.create(AppModule);",
    ],
)
def test_an_application_without_a_prefix_has_an_empty_proven_prefix(create: str) -> None:
    claim = _root(f"async function main() {{\n  {create}\n  await app.listen(1);\n}}\n")
    assert (claim.meta["prefix_status"], claim.meta["prefix"]) == ("proven", "")


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (
            CREATE + "app.setGlobalPrefix(PREFIX);",
            "global prefix",
        ),
        (
            CREATE + 'app.setGlobalPrefix("api", { exclude: ["health"] });',
            "global prefix",
        ),
        (
            CREATE + 'app.setGlobalPrefix("a");\napp.setGlobalPrefix("b");',
            "global prefix",
        ),
        (
            CREATE + "app.enableVersioning({ type: 1 });",
            "versioning",
        ),
        (
            CREATE + "app.enableVersioning({ type: VersioningType.HEADER, header: 'v' });",
            "versioning",
        ),
        # only the type differs from URI versioning: a header names no path
        (CREATE + "app.enableVersioning({ type: VersioningType.MEDIA_TYPE });", "versioning"),
        (CREATE + "app.enableVersioning({ type: VersioningType.URI, extractor });", "versioning"),
        (CREATE + 'app.enableVersioning({ defaultVersion: "1" });', "versioning"),
        (CREATE + "app.enableVersioning({ type: VersioningType.URI, prefix: P });", "versioning"),
        (
            CREATE + "app.enableVersioning({ type: VersioningType.URI, defaultVersion: V });",
            "versioning",
        ),
        (CREATE + "app.enableVersioning(options);", "versioning"),
        (CREATE + "app.enableVersioning();\napp.enableVersioning();", "versioning"),
        (
            CREATE + "configure(app);",
            "application escapes",
        ),
        (CREATE + "return app;", "application escapes"),
        (
            CREATE + "holder = { app };",
            "application escapes",
        ),
        (
            CREATE + "const http = app.getHttpAdapter;",
            "application escapes",
        ),
        ("let app = await NestFactory.create(AppModule);", "unbound application"),
        ("await (await NestFactory.create(AppModule)).listen(1);", "unbound application"),
        ("const app = await NestFactory.create(AppModule, new ExpressAdapter(server));", "adapter"),
    ],
)
def test_anything_that_could_change_a_route_makes_the_prefix_unknown(
    body: str, reason: str
) -> None:
    claim = _root(f"async function main() {{\n{body}\n}}\n", COMMON + CORE + VERSIONING)
    assert (claim.meta["prefix_status"], claim.meta["reason"]) == ("unknown", reason)
    assert "versioning" not in claim.meta


VERSIONING = 'import { VersioningType, VERSION_NEUTRAL } from "@nestjs/common";\n'


@pytest.mark.parametrize(
    ("call", "versioning"),
    [
        ("app.enableVersioning();", {"prefix": "v", "default": None}),
        (
            'app.enableVersioning({ type: VersioningType.URI, defaultVersion: "1" });',
            {"prefix": "v", "default": ["1"]},
        ),
        (
            "app.enableVersioning({ type: VersioningType.URI, prefix: false, "
            'defaultVersion: [VERSION_NEUTRAL, "2"] });',
            {"prefix": "", "default": [None, "2"]},
        ),
        (
            'app.enableVersioning({ type: VersioningType.URI, prefix: "version-" });',
            {"prefix": "version-", "default": None},
        ),
    ],
)
def test_uri_versioning_is_recorded_with_the_prefix(call: str, versioning: object) -> None:
    claim = _root(
        f'async function main() {{\n{CREATE}app.setGlobalPrefix("api");\n{call}\n}}\n',
        COMMON + CORE + VERSIONING,
    )
    assert (claim.meta["prefix_status"], claim.meta["prefix"]) == ("proven", "api")
    assert json.loads(claim.meta["versioning"]) == versioning


SWAGGER = 'import { SwaggerModule } from "@nestjs/swagger";\n'
DOCUMENTED = (
    "const document = SwaggerModule.createDocument(app, config);\n"
    'SwaggerModule.setup("docs", app, document, { swaggerOptions: {} });\n'
)


def test_documenting_the_application_with_swagger_changes_no_route() -> None:
    claim = _root(
        f'async function main() {{\n{CREATE}app.setGlobalPrefix("api");\n{DOCUMENTED}}}\n',
        COMMON + CORE + SWAGGER,
    )
    assert (claim.meta["prefix_status"], claim.meta["prefix"]) == ("proven", "api")


@pytest.mark.parametrize(
    ("header", "body"),
    [
        # not the NestJS one
        ('import { SwaggerModule } from "./swagger";\n', DOCUMENTED),
        ('import * as swagger from "@nestjs/swagger";\n', "swagger.SwaggerModule.setup('d', app);"),
        # the NestJS one, doing something other than documenting
        (SWAGGER, "SwaggerModule.patch(app);"),
        (SWAGGER, "configure(SwaggerModule.createDocument, app);"),
    ],
)
def test_handing_the_application_to_anything_else_still_escapes(header: str, body: str) -> None:
    claim = _root(f"async function main() {{\n{CREATE}{body}\n}}\n", COMMON + CORE + header)
    assert (claim.meta["prefix_status"], claim.meta["reason"]) == (
        "unknown",
        "application escapes",
    )


def test_a_versioning_type_that_is_not_nestjs_proves_nothing() -> None:
    claim = _root(
        f"async function main() {{\n{CREATE}"
        "app.enableVersioning({ type: VersioningType.URI });\n}\n",
        COMMON + CORE + 'import { VersioningType } from "./versioning";\n',
    )
    assert (claim.meta["prefix_status"], claim.meta["reason"]) == ("unknown", "versioning")


def test_a_dynamic_root_is_recorded_as_dynamic() -> None:
    claim = _root(
        "async function main() {\n"
        "  const authz = await NestFactory.create(AuthzHttpModule.forRoot(), { httpsOptions });\n"
        "}\n"
    )
    assert (claim.target_key, claim.meta["kind"]) == ("AuthzHttpModule.forRoot()", "dynamic")


def test_microservices_and_testing_modules_are_not_http_roots() -> None:
    source = (
        COMMON
        + CORE
        + 'import { Test } from "@nestjs/testing";\n'
        + "async function main() {\n"
        + "  const ms = await NestFactory.createMicroservice(AppModule, {});\n"
        + "  const ref = await Test.createTestingModule({ controllers: [C] }).compile();\n"
        + "}\n"
    )
    assert _claims(source, "nestjs-root") == []


def test_a_factory_that_is_not_nestjs_is_not_a_root() -> None:
    source = 'import { NestFactory } from "./fake";\nconst app = NestFactory.create(AppModule);\n'
    assert _claims(source, "nestjs-root") == []


# -- routing markers ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "markers"),
    [
        ('import { RouterModule } from "@nestjs/core";\n', ["router"]),
        ('import { ApplicationConfig } from "@nestjs/core";\n', ["global"]),
        ('import * as core from "@nestjs/core";\n', ["global"]),
        ('import core from "@nestjs/core";\n', ["global"]),
        ('export * from "@nestjs/core";\n', ["global"]),
        ('export { RouterModule } from "@nestjs/core";\n', ["router"]),
        ('const core = require("@nestjs/core");\n', ["global"]),
        ('const core = await import("@nestjs/core");\n', ["global"]),
        # destructured by name, a require takes what a named import takes
        ('const { NestFactory } = require("@nestjs/core");\n', []),
        ('const { NestFactory: Factory } = await import("@nestjs/core");\n', []),
        ('const { RouterModule } = require("@nestjs/core");\n', ["router"]),
        ('const { NestFactory, ApplicationConfig } = require("@nestjs/core");\n', ["global"]),
        ('const { NestFactory, ...rest } = require("@nestjs/core");\n', ["global"]),
        ('const { ["Application" + "Config"]: c } = require("@nestjs/core");\n', ["global"]),
        ('const NestFactory = require("@nestjs/core").NestFactory;\n', ["global"]),
        ('configure(require("@nestjs/core"));\n', ["global"]),
        (
            'import { RouterModule, ApplicationConfig } from "@nestjs/core";\n',
            ["global", "router"],
        ),
        ('import { NestFactory, Reflector, APP_GUARD } from "@nestjs/core";\n', []),
        ('import type { RouterModule } from "@nestjs/core";\n', []),
        ('import { RouterModule } from "./router";\n', []),
    ],
)
def test_constructs_that_can_change_other_routes_are_marked(
    source: str, markers: list[str]
) -> None:
    assert sorted(r.meta["marker"] for r in _claims(source, "nestjs-marker")) == markers
