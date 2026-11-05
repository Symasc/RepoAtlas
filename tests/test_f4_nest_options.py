"""F4: the options argument of ``NestFactory.create`` may be a name, but only a provable one.

``NestFactory.create(AppModule, options)`` used to leave the root's prefix unknown (``adapter``)
unless ``options`` was an inline object: a second argument may be an HTTP adapter, and an adapter
can change routes. A name is plain options only when nothing could have made it more than the
object literal it is bound to, by the time the call reads it:

* it is bound once in the file (``counts``), by exactly one declarator, of a ``const``;
* the value is an object literal, not unwrapped, with no spread;
* the declaration's block (or the file) holds the call and the declaration ends before it starts;
* the name is written exactly twice in the file: there, and as this argument.

Every other second argument keeps the reason ``adapter``; no reason is new, and the inline-object,
``setGlobalPrefix`` and ``enableVersioning(options)`` rules are the ones they were.
"""

from __future__ import annotations

import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest
from repoatlas.graph.http_claims import HttpClaimPlan
from repoatlas.graph.linker import plan_repository_http
from repoatlas.models.parsing import ParsedReference
from repoatlas.parsers.typescript import TypeScriptParser
from repoatlas.services.workspace import Workspace
from tests.f1_f2_corpus import NESTAPI, open_built

HEADER = (
    'import { NestFactory } from "@nestjs/core";\n'
    'import { SwaggerModule, DocumentBuilder } from "@nestjs/swagger";\n'
    'import { AppModule } from "./app.module";\n'
)


def root_of(source: str) -> ParsedReference:
    parsed = TypeScriptParser().parse(Path("main.ts"), textwrap.dedent(source))
    [claim] = [r for r in parsed.references if r.meta.get("pending_fact") == "nestjs-root"]
    return claim


def bootstrap(*lines: str, outside: str = "") -> str:
    body = "\n".join(f"  {line}" for line in lines)
    return f"{HEADER}{outside}\nasync function bootstrap() {{\n{body}\n}}\n\nbootstrap();\n"


CREATE_WITH_O = "const app = await NestFactory.create(AppModule, o);"
PREFIX = 'app.setGlobalPrefix("api");'

# -- what is proven -----------------------------------------------------------------------------


def test_a_const_object_in_the_same_function_is_plain_options() -> None:
    claim = root_of(bootstrap("const o = { cors: true };", CREATE_WITH_O, PREFIX))
    assert (claim.meta["prefix_status"], claim.meta["prefix"]) == ("proven", "api")
    assert "reason" not in claim.meta


def test_a_top_level_const_used_inside_the_function_is_plain_options() -> None:
    claim = root_of(
        bootstrap(CREATE_WITH_O, PREFIX, outside="const o = { cors: true, logger: false };\n")
    )
    assert (claim.meta["prefix_status"], claim.meta["prefix"]) == ("proven", "api")


def test_a_const_declared_in_an_enclosing_block_is_plain_options() -> None:
    source = f"""{HEADER}
    function start() {{
      const o = {{ cors: true }};
      return async () => {{
        const app = await NestFactory.create(AppModule, o);
        app.setGlobalPrefix("api");
      }};
    }}
    """
    claim = root_of(source)
    assert (claim.meta["prefix_status"], claim.meta["prefix"]) == ("proven", "api")


#: The shape of the NestJS RealWorld example's ``main.ts``: the options object is a const named
#: ``appOptions``, ``options`` is a different name (the Swagger document's), and the application
#: is documented, which changes no route.
REALWORLD_MAIN = """
import { NestFactory } from '@nestjs/core';
import { ApplicationModule } from './app.module';
import { SwaggerModule, DocumentBuilder } from '@nestjs/swagger';

async function bootstrap() {
  const appOptions = {cors: true};
  const app = await NestFactory.create(ApplicationModule, appOptions);
  app.setGlobalPrefix('api');

  const options = new DocumentBuilder()
    .setTitle('NestJS Realworld Example App')
    .setDescription('The Realworld API description')
    .setVersion('1.0')
    .setBasePath('api')
    .addBearerAuth()
    .build();
  const document = SwaggerModule.createDocument(app, options);
  SwaggerModule.setup('/docs', app, document);

  await app.listen(3000);
}
bootstrap();
"""


def test_the_realworld_example_main_proves_the_api_prefix() -> None:
    claim = root_of(REALWORLD_MAIN)
    assert (claim.meta["prefix_status"], claim.meta["prefix"]) == ("proven", "api")
    assert claim.target_key == "ApplicationModule"


def test_an_inline_object_is_what_it_was() -> None:
    claim = root_of(
        bootstrap("const app = await NestFactory.create(AppModule, { cors: true });", PREFIX)
    )
    assert (claim.meta["prefix_status"], claim.meta["prefix"]) == ("proven", "api")
    bare = root_of(bootstrap("const app = await NestFactory.create(AppModule, { cors: true });"))
    assert (bare.meta["prefix_status"], bare.meta["prefix"]) == ("proven", "")


# -- what is refused (each case is the one condition it breaks) ---------------------------------

REFUSED = {
    # not a name at all
    "call": bootstrap("const app = await NestFactory.create(AppModule, buildOptions());"),
    "new": bootstrap("const app = await NestFactory.create(AppModule, new ExpressAdapter(s));"),
    "member": bootstrap("const app = await NestFactory.create(AppModule, config.options);"),
    # (a)/(g) the name resolves through an import, or to nothing in the file
    "import": bootstrap(CREATE_WITH_O, outside='import { o } from "./options";\n'),
    "unbound": bootstrap(CREATE_WITH_O),
    # (c) the kind
    "let": bootstrap("let o = { cors: true };", CREATE_WITH_O),
    "var": bootstrap("var o = { cors: true };", CREATE_WITH_O),
    # (d) the value
    "call-value": bootstrap("const o = build();", CREATE_WITH_O),
    "alias-value": bootstrap("const o = base;", CREATE_WITH_O),
    "adapter-value": bootstrap("const o = new ExpressAdapter(s);", CREATE_WITH_O),
    "as-const": bootstrap("const o = { cors: true } as const;", CREATE_WITH_O),
    "parenthesized": bootstrap("const o = ({ cors: true });", CREATE_WITH_O),
    "spread": bootstrap("const o = { ...base };", CREATE_WITH_O),
    "spread-with-keys": bootstrap("const o = { cors: true, ...base };", CREATE_WITH_O),
    # (b) bound more than once in the file, with nothing else to tell
    "destructured-elsewhere": bootstrap(
        "const o = { cors: true };",
        CREATE_WITH_O,
        outside="function other(x) {\n  const { o } = x;\n}\n",
    ),
    "const-twice": bootstrap(
        "const o = { cors: true };",
        CREATE_WITH_O,
        outside="function other() {\n  const o = { cors: false };\n}\n",
    ),
    # (f) written anywhere but the declaration and the argument
    "property-assigned": bootstrap("const o = { cors: true };", "o.patch = x;", CREATE_WITH_O),
    "passed-on": bootstrap("const o = { cors: true };", "configure(o);", CREATE_WITH_O),
    "reassigned": bootstrap("const o = { cors: true };", "o = { ...base };", CREATE_WITH_O),
    "read-later": bootstrap("const o = { cors: true };", CREATE_WITH_O, "log(o);"),
    # (e) the declaration's block holds the call, and ends before it
    "inner-block": bootstrap("if (flag) {", "  const o = { cors: true };", "}", CREATE_WITH_O),
    "other-function": bootstrap(
        CREATE_WITH_O, outside="function other() {\n  const o = { cors: true };\n}\n"
    ),
    "declared-after": bootstrap(CREATE_WITH_O, "const o = { cors: true };"),
    "exported": bootstrap(CREATE_WITH_O, outside="export const o = { cors: true };\n"),
}


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_any_other_second_argument_leaves_the_prefix_unknown_as_an_adapter(case: str) -> None:
    claim = root_of(REFUSED[case])
    assert (claim.meta["prefix_status"], claim.meta["reason"]) == ("unknown", "adapter"), case
    assert "prefix" not in claim.meta


def test_a_refused_options_argument_is_refused_whatever_the_prefix_says() -> None:
    """The refusal is the root's, not the prefix rule's: a literal prefix does not rescue it."""
    claim = root_of(bootstrap("let o = { cors: true };", CREATE_WITH_O, PREFIX))
    assert (claim.meta["prefix_status"], claim.meta["reason"]) == ("unknown", "adapter")


def test_a_third_argument_is_still_refused_as_arguments() -> None:
    claim = root_of(bootstrap("const app = await NestFactory.create(AppModule, {}, extra);"))
    assert (claim.meta["prefix_status"], claim.meta["reason"]) == ("unknown", "arguments")


# -- through the index --------------------------------------------------------------------------

NAMED_OPTIONS = NESTAPI["src/main.ts"].replace(
    "const app = await NestFactory.create(AppModule);",
    "const appOptions = { cors: true };\n"
    "  const app = await NestFactory.create(AppModule, appOptions);",
)
MUTABLE_OPTIONS = NAMED_OPTIONS.replace("const appOptions", "let appOptions")


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Workspace]:
    repos = {
        "named": {**NESTAPI, "src/main.ts": NAMED_OPTIONS},
        "mutable": {**NESTAPI, "src/main.ts": MUTABLE_OPTIONS},
    }
    workspace = open_built(tmp_path_factory.mktemp("f4"), "options", repos)
    yield workspace
    workspace.close()


def plan_of(workspace: Workspace, name: str) -> HttpClaimPlan:
    repo = next(r for r in workspace.repositories.list_all() if r.name == name)
    assert repo.id is not None
    return plan_repository_http(workspace.knowledge, repo.id).plan


def test_a_named_options_object_lets_the_application_serve_its_routes(built: Workspace) -> None:
    plan = plan_of(built, "named")
    assert list(plan.roots.values()) == ["proven"]
    assert sorted(route.body for route in plan.code_routes) == [
        "GET /api/v1/users",
        "POST /api/v1/users",
    ]


def test_a_mutable_options_name_serves_nothing(built: Workspace) -> None:
    plan = plan_of(built, "mutable")
    assert "proven" not in plan.roots.values()
    assert plan.code_routes == ()
