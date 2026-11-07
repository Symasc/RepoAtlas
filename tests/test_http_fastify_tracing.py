"""Fastify factories, instances, entry applications and receiver aliases (M8, C1), and the
calls that hand them on and the routes declared on them (M8, C2).

The parser's side of tracing a Fastify application through a function: which functions
are factories, which ``const x = F()`` is an instance of one, which function builds an
application that the file provably runs, and which names are the same receiver; then which
instance the file uses at all, which call hands a receiver to a function, and which route,
registration or helper call sits on a straight line in its receiver's own body. Each is a
claim for the global pass; nothing here serves a route. Every refusal carries a stable
code on the claim, and every shape is asserted as a closed set.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from repoatlas.models.parsing import EdgeType, ParsedReference
from repoatlas.parsers.http_facts import (
    COMPOSED,
    EXACT,
    FASTIFY_HELPER_CALL,
    FASTIFY_INSTANCE,
    FASTIFY_REGISTER,
    FASTIFY_ROUTE,
    FASTIFY_SCOPE,
    FASTIFY_TRACED_ROUTE,
    FASTIFY_TRACED_SCOPE,
    PENDING_FACT,
)
from repoatlas.parsers.http_fastify import (
    AFTER_LISTEN,
    APP,
    CALLEE_BEFORE_BINDING,
    ENTRY,
    ENTRY_NOT_CALLED,
    FACTORY,
    FACTORY_NOT_AWAITED,
    FACTORY_UNPROVEN,
    HELPER_ARGUMENT_UNSUPPORTED,
    HELPER_CALLEE_UNSUPPORTED,
    HELPER_CONTROL_FLOW,
    HELPER_DYNAMIC,
    HELPER_SHADOWED,
    INSTANCE,
    PENDING,
    PLUGIN,
    PROVEN,
    RECEIVER_BEFORE_BINDING,
    REFUSED,
)
from repoatlas.parsers.typescript import TypeScriptParser

IMPORT = "import Fastify from 'fastify';\n"
MAKE = "function make() { const app = Fastify(); return app; }\n"


def _claims(source: str, role: str) -> list[ParsedReference]:
    parsed = TypeScriptParser().parse(Path("app.ts"), textwrap.dedent(source).lstrip("\n"))
    return [r for r in parsed.references if r.meta.get(PENDING_FACT) == role]


def _instances(source: str) -> list[ParsedReference]:
    return _claims(source, FASTIFY_INSTANCE)


def _factories(source: str) -> list[ParsedReference]:
    return [c for c in _claims(source, FASTIFY_SCOPE) if c.meta["kind"] == FACTORY]


def _only(source: str) -> ParsedReference:
    [claim] = _instances(source)
    return claim


def _aliases(claim: ParsedReference) -> list[list[object]]:
    return json.loads(claim.meta["aliases"])


# -- factories ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "is_async"),
    [
        # the `const app` form, with the application used in the body
        (
            "function make() { const app = Fastify(); app.get('/x', h); return app; }",
            "false",
        ),
        ("async function make() { const app = Fastify(); return app; }", "true"),
        ("function make() { return Fastify({ logger: true }); }", "false"),
        ("const make = () => Fastify();", "false"),
        ("const make = async () => Fastify();", "true"),
        ("const make = async () => { const app = Fastify(); return app; };", "true"),
        ("const make = function () { const app = Fastify(); return app; };", "false"),
        ("export function make() { const app = Fastify(); return app; }", "false"),
        # more than one return, every one of them the application
        (
            "function make(f) { const app = Fastify(); if (f) { return app; } return app; }",
            "false",
        ),
    ],
)
def test_a_function_that_hands_back_the_application_it_builds_is_a_factory(
    source: str, is_async: str
) -> None:
    [claim] = _factories(IMPORT + source + "\n")
    assert claim.target_key == "make()"
    assert claim.meta["kind"] == FACTORY
    assert claim.meta["wrapped"] == "false"
    assert claim.meta["is_async"] == is_async


def test_the_fastify_factory_may_be_a_required_one() -> None:
    source = "const fastify = require('fastify');\nfunction make() { return fastify(); }\n"
    assert [c.target_key for c in _factories(source)] == ["make()"]


@pytest.mark.parametrize(
    "source",
    [
        # the application is wrapped in an object
        "function make() { const app = Fastify(); return { app }; }",
        # a conditional return of something else
        "function make(f) { const app = Fastify(); if (f) { return null; } return app; }",
        "function make(f) { const app = Fastify(); return f ? app : other; }",
        # two applications
        "function make() { const a = Fastify(); const b = Fastify(); return a; }",
        "function make() { const a = Fastify(); return Fastify(); }",
        # nothing is returned, or only a bare return
        "function make() { const app = Fastify(); app.listen(); }",
        "function make() { const app = Fastify(); return; }",
        # the application is not a `const` of the body's top level
        "function make() { let app = Fastify(); return app; }",
        "function make(f) { if (f) { const app = Fastify(); return app; } return null; }",
        "function make() { return wrap(Fastify()); }",
        "const make = () => wrap(Fastify());",
        # the name of the application is bound twice in the function's own scope
        "function make() { const app = Fastify(); { const app = 1; } return app; }",
        # a closure over the application, not the application
        "function make() { const app = Fastify(); return () => app; }",
    ],
)
def test_a_function_that_does_not_hand_back_exactly_one_application_is_no_factory(
    source: str,
) -> None:
    text = IMPORT + source + "\nconst server = make();\nserver.listen();\n"
    assert _factories(text) == []
    # a function with a top-level `const app = Fastify()` also yields an entry-app claim
    # (`make>app`, refused as entry-not-called); the instance is the claim for `server`
    [claim] = [c for c in _instances(text) if c.target_key == "server"]
    assert claim.meta["refusal"] == FACTORY_UNPROVEN


def test_a_function_that_can_fall_off_its_end_is_no_factory() -> None:
    source = "function make(c) { const app = Fastify(); if (c) return app; }"
    text = IMPORT + source + "\nconst server = make();\nserver.listen();\n"
    assert _factories(text) == []
    [claim] = [c for c in _instances(text) if c.target_key == "server"]
    assert claim.meta["refusal"] == FACTORY_UNPROVEN


def test_a_function_whose_body_ends_in_the_return_of_the_application_is_a_factory() -> None:
    source = "function make(c) { const app = Fastify(); if (c) return app; return app; }"
    assert [c.target_key for c in _factories(IMPORT + source + "\n")] == ["make()"]


@pytest.mark.parametrize(
    "source",
    [
        # a second Fastify() in a nested function counts: exactly one call in the whole body
        "function make() { const app = Fastify(); const f = () => Fastify(); return app; }",
        # the only Fastify() is in a nested function, whose result is returned through a call
        "function make() { const g = () => Fastify(); return g(); }",
        # known limit: a chain on the Fastify() call is not recognised as the application
        "function make() { const app = Fastify().withTypeProvider(); return app; }",
    ],
)
def test_a_second_or_nested_or_chained_fastify_call_is_no_factory(source: str) -> None:
    text = IMPORT + source + "\nconst server = make();\nserver.listen();\n"
    assert _factories(text) == []
    [claim] = [c for c in _instances(text) if c.target_key == "server"]
    assert claim.meta["refusal"] == FACTORY_UNPROVEN


def test_a_return_inside_a_nested_function_does_not_count_for_the_factory() -> None:
    source = (
        IMPORT + "function make() {\n  const app = Fastify();\n"
        "  const noop = () => { return 1; };\n  return app;\n}\n"
    )
    assert [c.target_key for c in _factories(source)] == ["make()"]


def test_two_factories_may_each_bind_the_application_under_the_same_name() -> None:
    source = (
        IMPORT + "function one() { const app = Fastify(); return app; }\n"
        "async function two() { const app = Fastify(); return app; }\n"
    )
    assert [c.target_key for c in _factories(source)] == ["one()", "two()"]


def test_a_factory_is_a_scope_beside_the_plugin_scope_the_function_already_was() -> None:
    claims = _claims(IMPORT + MAKE, FASTIFY_SCOPE)
    assert sorted((c.target_key, c.meta["kind"]) for c in claims) == [
        ("make", "plugin"),
        ("make()", "factory"),
    ]
    [plugin] = [c for c in claims if c.target_key == "make"]
    assert set(plugin.meta) == {PENDING_FACT, "kind", "wrapped", "exports"}


def test_a_factory_scope_names_the_exports_that_reach_it() -> None:
    source = (
        IMPORT + "export default function make() { const app = Fastify(); return app; }\n"
        "export { make as build };\n"
    )
    [claim] = _factories(source)
    assert sorted(json.loads(claim.meta["exports"])) == [["build", False], ["default", False]]
    named = _factories(IMPORT + "export const build = () => Fastify();\n")
    assert [json.loads(c.meta["exports"]) for c in named] == [[["build", False]]]
    private = _factories(IMPORT + MAKE)
    assert [json.loads(c.meta["exports"]) for c in private] == [[]]


def test_a_factory_is_read_only_in_a_file_that_imports_fastify() -> None:
    source = "function make() { const app = Fastify(); return app; }\nconst server = make();\n"
    assert _factories(source) == []
    assert _instances(source) == []


# -- instances ---------------------------------------------------------------------------


def test_a_local_factory_called_once_is_a_proven_instance() -> None:
    claim = _only(IMPORT + MAKE + "const server = make();\nserver.listen();\n")
    assert claim.target_key == "server"
    assert (claim.line_start, claim.line_end) == (3, 3)
    assert claim.meta == {
        PENDING_FACT: FASTIFY_INSTANCE,
        "kind": INSTANCE,
        "receiver": "server",
        "entry": "",
        "local": "make()",
        "awaited": "false",
        "status": PROVEN,
        "aliases": "[]",
    }


def test_an_awaited_async_factory_is_a_proven_instance() -> None:
    source = (
        IMPORT + "async function make() { const app = Fastify(); return app; }\n"
        "const server = await make();\nserver.listen();\n"
    )
    claim = _only(source)
    assert claim.meta["status"] == PROVEN
    assert claim.meta["awaited"] == "true"
    assert "refusal" not in claim.meta
    wrapped = source.replace("await make();", "(await make());")
    assert _only(wrapped).meta["status"] == PROVEN


def test_a_sync_factory_may_be_awaited_too() -> None:
    source = IMPORT + MAKE + "async function run() {}\nconst server = await make();\n"
    claim = _only(source + "server.listen();\n")
    assert claim.meta["status"] == PROVEN
    assert claim.meta["awaited"] == "true"


@pytest.mark.parametrize(
    ("imports", "call", "module", "name"),
    [
        (
            "import { createServer } from './server';",
            "createServer()",
            "./server",
            "createServer",
        ),
        (
            "import { createServer as make } from '../app';",
            "make()",
            "../app",
            "createServer",
        ),
        ("import createServer from './server';", "createServer()", "./server", "default"),
        (
            "import * as server from './server';",
            "server.createServer()",
            "./server",
            "createServer",
        ),
    ],
)
def test_a_factory_imported_from_a_relative_module_is_left_to_the_global_pass(
    imports: str, call: str, module: str, name: str
) -> None:
    claim = _only(IMPORT + imports + f"\nconst app = {call};\napp.listen();\n")
    assert claim.target_key == "app"
    assert claim.meta == {
        PENDING_FACT: FASTIFY_INSTANCE,
        "kind": INSTANCE,
        "receiver": "app",
        "entry": "",
        "module": module,
        "name": name,
        "awaited": "false",
        "status": PENDING,
        "aliases": "[]",
    }


@pytest.mark.parametrize(
    "source",
    [
        # a package's function is no factory of this repository
        "import { createServer } from 'some-package';\nconst app = createServer();\n",
        "import * as server from 'some-package';\nconst app = server.createServer();\n",
        # nothing names what is called
        "const app = factories[kind]();\n",
        "const app = make.build();\n",
        "const app = (await import('./server')).createServer();\n",
        # not a `const` bound once
        "import { make } from './server';\nlet app = make();\napp = make();\n",
        "import { make } from './server';\nconst app = make();\nfunction f(app) { return app; }\n",
        # not a call
        "import { make } from './server';\nconst app = make;\n",
        "import { make } from './server';\nconst app = new make();\n",
    ],
)
def test_a_call_that_names_no_factory_of_this_file_or_a_relative_module_is_no_instance(
    source: str,
) -> None:
    assert _instances(IMPORT + source) == []


def test_a_name_that_is_not_the_one_factory_is_not_looked_at() -> None:
    source = IMPORT + MAKE + "function make() { return 1; }\nconst server = make();\n"
    assert _instances(source) == []


def test_an_instance_inside_an_entry_function_is_scoped_by_the_function() -> None:
    source = (
        IMPORT + MAKE + "async function start() {\n  const server = make();\n"
        "  server.listen();\n}\nstart();\n"
    )
    claim = _only(source)
    assert claim.target_key == "start>server"
    assert claim.meta["kind"] == INSTANCE
    assert claim.meta["entry"] == "start"
    assert claim.meta["status"] == PROVEN


def test_an_instance_inside_a_function_nobody_calls_is_refused() -> None:
    body = "  const server = make();\n  server.listen();\n"
    claim = _only(IMPORT + MAKE + "function start() {\n" + body + "}\n")
    assert claim.target_key == "start>server"
    assert claim.meta["status"] == REFUSED
    assert claim.meta["refusal"] == ENTRY_NOT_CALLED


def test_the_instance_claims_of_a_file_are_in_source_order_and_bounded() -> None:
    names = [f"s{i}" for i in range(70)]
    source = IMPORT + MAKE + "".join(f"const {name} = make();\n" for name in names)
    source += "".join(f"{name}.listen();\n" for name in names)
    claims = _instances(source)
    assert [c.target_key for c in claims] == names[:64]
    assert [c.line_start for c in claims] == list(range(3, 67))
    assert [c.target_key for c in _instances(source)] == names[:64]


def test_a_used_instance_is_stated_by_a_file_with_or_without_fastify() -> None:
    # the usage gate (C2) holds both ways: an instance nothing uses is no claim, with fastify
    # or without it; a used one is stated either way. Without fastify the file is read for
    # tracing alone, and its factory is no factory there (see below).
    source = MAKE + "const server = make();\n"
    assert _instances(source) == []
    assert _instances(IMPORT + source) == []
    assert [c.target_key for c in _instances(IMPORT + source + "server.listen();\n")] == ["server"]
    [claim] = _instances(source + "server.listen();\n")
    assert claim.target_key == "server"
    assert claim.meta["status"] == REFUSED
    assert claim.meta["refusal"] == FACTORY_UNPROVEN


# -- entry applications ------------------------------------------------------------------

START = "async function start() {\n  const app = Fastify();\n  await app.listen();\n}\n"


@pytest.mark.parametrize(
    "call",
    [
        "start();",
        "void start();",
        "await start();",
        "start().catch(console.error);",
        "await start().catch(console.error);",
        "void start().catch(onError);",
        "if (require.main === module) { start().then(); }",
        "if ((require.main === module)) { start().then(); }",
        # G runs whenever the file is the main module, whatever the else branch does
        "if (require.main === module) { start().then(); } else { module.exports = start; }",
    ],
)
def test_an_entry_function_the_top_level_calls_unconditionally_proves_its_application(
    call: str,
) -> None:
    claim = _only(IMPORT + START + call + "\n")
    assert claim.target_key == "start>app"
    assert (claim.line_start, claim.line_end) == (3, 3)
    assert claim.meta == {
        PENDING_FACT: FASTIFY_INSTANCE,
        "kind": ENTRY,
        "receiver": "app",
        "entry": "start",
        "status": PROVEN,
        "aliases": "[]",
    }


@pytest.mark.parametrize(
    "call",
    [
        "",
        "if (process.env.RUN) { start(); }",
        "if (require.main === module) start();",
        "if (require.main === module) start().then();",
        "if (require?.main === module) { start().then(); }",
        "if (require.main === module) { start().then(ready); }",
        "if (module === require.main) { start().then(); }",
        "if (require.main == module) { start().then(); }",
        "if (require.main === module) { start().then(); report(); }",
        "const require = {}; if (require.main === module) { start().then(); }",
        "const module = {}; if (require.main === module) { start().then(); }",
        "function require() {} if (require.main === module) { start().then(); }",
        "let module = {}; if (require.main === module) { start().then(); }",
        "if (require.main === exports) { start().then(); }",
        "if (process.main === module) { start().then(); }",
        "if (require.cache === module) { start().then(); }",
        "if (require.main === module) { start().finally(); }",
        "if (require.main === module) { start().catch(h).then(); }",
        "if (require.main === module) { start(cfg).then(); }",
        "if (require.main === module) { (start()).then(); }",
        "if (require.main === module) { await start().then(); }",
        "if (require.main === module) { start()?.then(); }",
        "if (require.main === module) { start?.().then(); }",
        "if (require.main === module) { app.start().then(); }",
        "if (require.main === module) { (start().then()); }",
        "if (require.main !== module) { start().then(); }",
        "process.env.RUN && start();",
        "setTimeout(() => start(), 0);",
        "process.on('ready', start);",
        "for (const _ of items) { start(); }",
        "try { start(); } catch (e) { report(e); }",
        "start().then(ready);",
        "(async () => { await start(); })();",
        "export const go = () => start();",
    ],
)
def test_an_entry_function_that_is_not_called_unconditionally_proves_nothing(call: str) -> None:
    claim = _only(IMPORT + START + call + "\n")
    assert claim.meta["status"] == REFUSED
    assert claim.meta["refusal"] == ENTRY_NOT_CALLED
    assert claim.meta["aliases"] == "[]"


@pytest.mark.parametrize(
    ("name", "status"),
    [
        ("a.js", PROVEN),
        ("a.ts", PROVEN),
        ("a.cjs", PROVEN),
        ("a.cts", PROVEN),
        ("a.mjs", REFUSED),
        ("a.mts", REFUSED),
    ],
)
def test_the_commonjs_guard_proves_nothing_in_an_es_module_file(name: str, status: str) -> None:
    source = IMPORT + START + "if (require.main === module) { start().then(); }\n"
    parsed = TypeScriptParser().parse(Path(name), source)
    [claim] = [r for r in parsed.references if r.meta.get(PENDING_FACT) == FASTIFY_INSTANCE]
    assert claim.meta["status"] == status
    if status == REFUSED:
        assert claim.meta["refusal"] == ENTRY_NOT_CALLED


def test_an_entry_function_is_proven_by_any_one_unconditional_call() -> None:
    claim = _only(IMPORT + START + "if (x) { start(); }\nstart();\n")
    assert claim.meta["status"] == PROVEN


def test_an_entry_function_held_by_a_const_is_called_after_its_binding() -> None:
    held = "const start = async () => {\n  const app = Fastify();\n  app.listen();\n};\n"
    assert _only(IMPORT + held + "start();\n").meta["status"] == PROVEN
    claim = _only(IMPORT + "start();\n" + held)
    assert claim.meta["status"] == REFUSED
    assert claim.meta["refusal"] == CALLEE_BEFORE_BINDING


def test_a_function_declaration_may_be_called_before_its_position() -> None:
    claim = _only(IMPORT + "start();\n" + START)
    assert claim.meta["status"] == PROVEN


def test_a_factory_is_no_entry_function() -> None:
    source = IMPORT + MAKE + "make();\n"
    assert _instances(source) == []


def test_a_module_level_application_is_not_an_instance() -> None:
    assert _instances(IMPORT + "const server = Fastify();\n") == []


# -- the try block of an entry function -----------------------------------------------------

INIT = "import { init } from './init';\n"
GUARD = "if (require.main === module) { main().then(); }\n"
HELD = "const server = make();\n"


def _entry_source(body: str) -> str:
    return INIT + IMPORT + MAKE + "async function main() {\n" + body + "}\n" + GUARD


@pytest.mark.parametrize(
    "body",
    [
        "try { const server = make(); init(server); } catch (e) { report(e); }\n",
        "try { const server = make(); return init(server); } catch (e) {}\n",
        "try { const server = make(); await init(server); } finally { done(); }\n",
        "setup();\ntry { const server = make(); init(server); } catch (e) {}\n",
    ],
)
def test_the_try_block_of_an_entry_function_is_on_its_straight_line(body: str) -> None:
    source = _entry_source(body)
    claim = _only(source)
    assert claim.target_key == "main>server"
    assert claim.meta["entry"] == "main"
    assert claim.meta["status"] == PROVEN
    [call] = _helpers(source)
    assert call.meta["owner"] == "main>server"
    assert call.meta["owner_kind"] == INSTANCE
    assert "refusal" not in call.meta


def test_an_entry_application_in_a_try_block_serves_its_traced_routes() -> None:
    body = "  try {\n    const app = Fastify();\n    app.get('/x', h);\n  } catch (e) {}\n"
    source = IMPORT + "async function main() {\n" + body + "}\n" + GUARD
    claim = _only(source)
    assert (claim.target_key, claim.meta["kind"], claim.meta["status"]) == (
        "main>app",
        ENTRY,
        PROVEN,
    )
    [route] = _traced(source)
    assert route.meta["scope"] == "main>app"


@pytest.mark.parametrize(
    "body",
    [
        # the catch, a try nested in the block, a try in a condition, a condition in the block
        HELD + "try { setup(); } catch (e) { init(server); }\n",
        HELD + "try { try { init(server); } catch (e) {} } catch (e) {}\n",
        HELD + "if (ready) { try { init(server); } catch (e) {} }\n",
        HELD + "try { if (ready) { init(server); } } catch (e) {}\n",
    ],
)
def test_a_call_off_the_try_block_of_an_entry_function_is_control_flow(body: str) -> None:
    source = _entry_source(body)
    assert _only(source).meta["status"] == PROVEN
    [call] = _helpers(source)
    assert call.meta["refusal"] == HELPER_CONTROL_FLOW


@pytest.mark.parametrize(
    "body",
    [
        "if (ready) { try { const server = make(); init(server); } catch (e) {} }\n",
        "try { try { const server = make(); init(server); } catch (e) {} } catch (e) {}\n",
        "try { setup(); } catch (e) { const server = make(); init(server); }\n",
    ],
)
def test_an_instance_off_the_try_block_of_an_entry_function_is_no_claim(body: str) -> None:
    assert _instances(_entry_source(body)) == []


def test_a_try_block_at_the_module_level_is_still_control_flow() -> None:
    source = INIT + IMPORT + MAKE + HELD + "try {\n  init(server);\n} catch (e) {}\n"
    assert _only(source).meta["status"] == PROVEN
    [call] = _helpers(source)
    assert call.meta["refusal"] == HELPER_CONTROL_FLOW


def test_a_listen_awaited_in_the_try_block_comes_before_a_later_call() -> None:
    body = (
        "  try {\n    const server = make();\n    await server.listen();\n"
        "    init(server);\n  } catch (e) {}\n"
    )
    [call] = _helpers(_entry_source(body))
    assert call.meta["refusal"] == AFTER_LISTEN


# -- receiver aliases --------------------------------------------------------------------


def test_an_alias_of_an_instance_is_the_same_receiver() -> None:
    source = IMPORT + MAKE + "const a = make();\nconst b = a;\nconst c = b as Fastify;\n"
    assert _aliases(_only(source + "a.listen();\n")) == [["b", 1], ["c", 2]]


def test_aliases_are_followed_for_four_hops_and_no_further() -> None:
    chain = "".join(f"const {new} = {old};\n" for old, new in zip("abcde", "bcdef", strict=True))
    claim = _only(IMPORT + MAKE + "const a = make();\n" + chain + "a.listen();\n")
    assert _aliases(claim) == [["b", 1], ["c", 2], ["d", 3], ["e", 4]]
    assert claim.meta["status"] == PROVEN


def test_a_name_may_alias_the_same_receiver_twice() -> None:
    source = IMPORT + MAKE + "const a = make();\nconst b = a;\nconst c = a;\nconst d = b;\n"
    assert _aliases(_only(source + "a.listen();\n")) == [["b", 1], ["c", 1], ["d", 2]]


def test_an_alias_of_an_entry_application_is_followed() -> None:
    source = IMPORT + START.replace("await app", "const y = app;\n  await app") + "start();\n"
    claim = _only(source)
    assert claim.meta["kind"] == ENTRY
    assert _aliases(claim) == [["y", 1]]


@pytest.mark.parametrize(
    "extra",
    [
        # in another scope than the instance
        "function f() { const b = a; }\n",
        # not a plain name
        "const b = a.child;\nconst c = [a];\nconst d = wrap(a);\nconst e = a || other;\n",
        # a `let` is no alias: it can change
        "let b = a;\n",
        # a name used before its own binding
        "use(b);\nconst b = a;\n",
    ],
)
def test_a_name_that_is_not_a_const_alias_in_the_same_scope_is_not_an_alias(extra: str) -> None:
    claim = _only(IMPORT + MAKE + "const a = make();\n" + extra + "a.listen();\n")
    assert _aliases(claim) == []


def test_a_parameter_of_the_same_name_does_not_hide_a_module_level_alias() -> None:
    source = IMPORT + MAKE + "const a = make();\nconst b = a;\nfunction f(b) { return b; }\n"
    assert _aliases(_only(source + "a.listen();\n")) == [["b", 1]]


def test_an_alias_declared_before_what_it_names_is_a_use_before_the_binding() -> None:
    claim = _only(IMPORT + MAKE + "const b = a;\nconst a = make();\na.listen();\n")
    assert claim.target_key == "a"
    assert claim.meta["status"] == REFUSED
    assert claim.meta["refusal"] == RECEIVER_BEFORE_BINDING
    assert claim.meta["aliases"] == "[]"


# -- order -------------------------------------------------------------------------------


def test_a_receiver_used_before_its_const_is_refused() -> None:
    claim = _only(IMPORT + MAKE + "init(server);\nconst server = make();\nserver.listen();\n")
    assert claim.meta["status"] == REFUSED
    assert claim.meta["refusal"] == RECEIVER_BEFORE_BINDING
    assert claim.meta["aliases"] == "[]"


def test_a_receiver_used_before_its_const_in_an_entry_function_is_refused() -> None:
    source = (
        IMPORT + MAKE + "function start() {\n  init(server);\n  const server = make();\n"
        "  server.listen();\n}\nstart();\n"
    )
    claim = _only(source)
    assert claim.target_key == "start>server"
    assert claim.meta["refusal"] == RECEIVER_BEFORE_BINDING


def test_a_use_inside_a_nested_function_declared_earlier_runs_later() -> None:
    source = (
        IMPORT + MAKE + "function setup() { server.get('/x', h); }\n"
        "const server = make();\nsetup();\n"
    )
    claim = _only(source)
    assert claim.meta["status"] == PROVEN
    assert "refusal" not in claim.meta


def test_a_property_or_key_of_the_same_name_is_not_a_use_of_the_receiver() -> None:
    source = IMPORT + MAKE + "const o = { server: 1 };\nlog(o.server);\nconst server = make();\n"
    assert _instances(source + "server.listen();\n")[-1].meta["status"] == PROVEN


def test_a_const_factory_called_before_its_binding_is_refused() -> None:
    source = (
        IMPORT + "const server = make();\n"
        "const make = () => { const app = Fastify(); return app; };\nserver.listen();\n"
    )
    claim = _only(source)
    assert claim.meta["status"] == REFUSED
    assert claim.meta["refusal"] == CALLEE_BEFORE_BINDING


def test_a_const_factory_called_after_its_binding_is_accepted() -> None:
    source = (
        IMPORT + "const make = () => { const app = Fastify(); return app; };\n"
        "const server = make();\nserver.listen();\n"
    )
    assert _only(source).meta["status"] == PROVEN


def test_a_function_declaration_factory_called_before_its_position_is_accepted() -> None:
    claim = _only(IMPORT + "const server = make();\nserver.listen();\n" + MAKE)
    assert claim.meta["status"] == PROVEN
    assert "refusal" not in claim.meta


# -- other refusals ----------------------------------------------------------------------


def test_an_async_factory_called_without_await_is_refused() -> None:
    source = (
        IMPORT + "async function make() { const app = Fastify(); return app; }\n"
        "const server = make();\nserver.listen();\n"
    )
    claim = _only(source)
    assert claim.meta["status"] == REFUSED
    assert claim.meta["refusal"] == FACTORY_NOT_AWAITED
    assert claim.meta["awaited"] == "false"


def test_a_local_function_that_is_no_factory_is_refused_as_unproven() -> None:
    source = IMPORT + "function createCache() { return new Map(); }\nconst cache = createCache();\n"
    claim = _only(source + "cache.listen();\n")
    assert claim.meta["status"] == REFUSED
    assert claim.meta["refusal"] == FACTORY_UNPROVEN
    assert claim.meta["local"] == "createCache()"


def test_the_first_reason_to_refuse_is_the_one_recorded() -> None:
    # unproven before not awaited before the receiver used early
    source = (
        IMPORT + "async function make() { return Fastify().x; }\n"
        "init(server);\nconst server = make();\nserver.listen();\n"
    )
    assert _only(source).meta["refusal"] == FACTORY_UNPROVEN
    source = (
        IMPORT + "async function make() { const app = Fastify(); return app; }\n"
        "init(server);\nconst server = make();\nserver.listen();\n"
    )
    assert _only(source).meta["refusal"] == FACTORY_NOT_AWAITED


# -- the usage gate (M8, C2) -------------------------------------------------------------

USERS = "import { initUsers } from './users';\n"
SERVER = "const server = make();\n"
#: A file that imports fastify, builds an instance, and has functions to hand it to.
PRELUDE = (
    IMPORT
    + "import { later } from './later';\nimport * as ns from './ns';\n"
    + MAKE
    + "function init(s) {}\n"
    + SERVER
)
_FASTIFY_ROLES = frozenset(
    {
        FASTIFY_INSTANCE,
        FASTIFY_ROUTE,
        FASTIFY_REGISTER,
        FASTIFY_SCOPE,
        FASTIFY_HELPER_CALL,
        FASTIFY_TRACED_ROUTE,
        FASTIFY_TRACED_SCOPE,
    }
)


def _refs(source: str) -> list[ParsedReference]:
    parsed = TypeScriptParser().parse(Path("app.ts"), textwrap.dedent(source).lstrip("\n"))
    return list(parsed.references)


def _roles(source: str) -> set[str]:
    return {r.meta[PENDING_FACT] for r in _refs(source) if PENDING_FACT in r.meta}


def _helpers(source: str) -> list[ParsedReference]:
    return _claims(source, FASTIFY_HELPER_CALL)


def _traced(source: str) -> list[ParsedReference]:
    return _claims(source, FASTIFY_TRACED_ROUTE)


@pytest.mark.parametrize(
    "use",
    [
        "server.get('/x', h);",
        "server.route({ method: 'GET', url: '/x', handler: h });",
        "server.register(plugin);",
        "server.listen();",
        "server.ready();",
        # an argument of a function of this file, of a relative module, of a relative namespace
        "init(server);",
        "later(server);",
        "ns.init(server);",
        # held by an object or an array handed on
        "init({ server });",
        "init([server]);",
    ],
)
def test_an_instance_the_file_serves_from_or_hands_on_is_a_claim(use: str) -> None:
    assert [c.target_key for c in _instances(PRELUDE + use + "\n")] == ["server"]


@pytest.mark.parametrize(
    "other",
    [
        "",
        "log(server);",
        "console.log(server);",
        "other.get('/x', server);",
        "server.close();",
        "server.decorate('x', 1);",
        "const copy = [server];",
        "const o = { server };",
        "const alias = server;",
    ],
)
def test_an_instance_nothing_serves_from_or_hands_on_is_no_claim(other: str) -> None:
    assert _instances(PRELUDE + other + "\n") == []


def test_a_value_a_relative_function_returns_that_nothing_uses_is_no_claim() -> None:
    source = "import { loadConfig } from './config';\nconst cfg = loadConfig();\n"
    assert _instances(source) == []
    assert _instances(IMPORT + source) == []
    assert _instances(source + "const port = cfg.port;\n") == []


def test_a_use_of_an_alias_counts_as_a_use_of_the_instance() -> None:
    claim = _only(PRELUDE + "const alias = server;\nalias.listen();\n")
    assert _aliases(claim) == [["alias", 1]]
    claim = _only(PRELUDE + "const alias = server;\nconst again = alias;\ninit(again);\n")
    assert _aliases(claim) == [["alias", 1], ["again", 2]]


def test_a_use_of_another_binding_of_the_name_is_no_use_of_the_instance() -> None:
    source = (
        IMPORT + MAKE + "function start() {\n  const app = make();\n"
        "  function f(app) { app.listen(); }\n}\nstart();\n"
    )
    assert _instances(source) == []


def test_a_used_instance_is_a_claim_even_when_the_parser_refuses_it() -> None:
    source = IMPORT + "function make() { return 1; }\nconst server = make();\nserver.listen();\n"
    claim = _only(source)
    assert claim.meta["status"] == REFUSED
    assert claim.meta["refusal"] == FACTORY_UNPROVEN


def test_a_client_the_file_builds_from_a_package_records_no_fastify_claim() -> None:
    source = (
        "import axios from 'axios';\nimport { loadConfig } from './config';\n"
        "const client = axios.create();\nconst cfg = loadConfig();\nclient.get('/x', cfg);\n"
    )
    assert _roles(source).isdisjoint(_FASTIFY_ROLES)


# -- a file without fastify, read for tracing --------------------------------------------

TRACED = (
    "import { createClient } from './client';\n"
    "function setup(app) { app.get('/n21', opts); }\n"
    "const client = createClient();\nsetup(client);\n"
)


def test_a_file_without_fastify_is_read_only_when_it_uses_an_instance() -> None:
    unused = TRACED.replace("setup(client);\n", "")
    assert _roles(unused).isdisjoint(_FASTIFY_ROLES)
    assert _roles("function setup(app) { app.get('/x', opts); }\n").isdisjoint(_FASTIFY_ROLES)
    assert _roles(TRACED) & _FASTIFY_ROLES == {
        FASTIFY_INSTANCE,
        FASTIFY_HELPER_CALL,
        FASTIFY_TRACED_ROUTE,
        FASTIFY_TRACED_SCOPE,
    }


def test_a_file_without_fastify_states_the_instance_the_call_the_route_and_the_scope() -> None:
    [instance] = _instances(TRACED)
    assert (instance.line_start, instance.line_end) == (3, 3)
    assert instance.meta == {
        PENDING_FACT: FASTIFY_INSTANCE,
        "kind": INSTANCE,
        "receiver": "client",
        "entry": "",
        "module": "./client",
        "name": "createClient",
        "awaited": "false",
        "status": PENDING,
        "aliases": "[]",
    }
    [call] = _helpers(TRACED)
    assert call.target_key == "setup"
    assert (call.line_start, call.line_end) == (4, 4)
    assert call.meta == {
        PENDING_FACT: FASTIFY_HELPER_CALL,
        "owner": "client",
        "owner_kind": INSTANCE,
        "receiver": "client",
        "arg": "0",
        "local": "setup",
    }
    [route] = _traced(TRACED)
    assert route.target_key == "GET /n21"
    assert route.source_symbol == "setup"
    assert (route.line_start, route.line_end) == (2, 2)
    assert route.meta == {
        PENDING_FACT: FASTIFY_TRACED_ROUTE,
        "method": "GET",
        "path": json.dumps([["t", "/n21"]]),
        "scope": "setup",
        "owner_kind": PLUGIN,
        "provenance": EXACT,
    }
    [scope] = _claims(TRACED, FASTIFY_TRACED_SCOPE)
    assert scope.target_key == "setup"
    assert (scope.line_start, scope.line_end) == (2, 2)
    assert scope.meta == {
        PENDING_FACT: FASTIFY_TRACED_SCOPE,
        "kind": PLUGIN,
        "exports": "[]",
        "receiver": "app",
    }


def test_a_scope_nothing_names_is_not_stated_in_a_file_without_fastify() -> None:
    source = TRACED + "function unrelated(app) { app.get('/o', opts); }\nfunction idle(app) {}\n"
    assert [c.target_key for c in _claims(source, FASTIFY_TRACED_SCOPE)] == ["setup", "unrelated"]


def test_a_file_without_fastify_registers_only_what_an_instance_registers() -> None:
    source = (
        "import { createServer } from './server';\nimport q from './q';\n"
        "function p(app) { app.register(q); }\nconst server = createServer();\n"
        "server.register(q);\np(server);\n"
    )
    [registration] = _claims(source, FASTIFY_REGISTER)
    assert registration.meta["owner"] == "server"
    assert registration.meta["owner_kind"] == INSTANCE
    assert _claims(source, FASTIFY_SCOPE) == []
    assert _claims(source, FASTIFY_ROUTE) == []


# -- a file without fastify that forwards a plugin's server ---------------------------------

FORWARD = (
    "import { Server } from './server';\n"
    "import { initUsers } from './users';\n"
    "export function initAll(server: Server, deps: Deps) {\n"
    "  initUsers(server, deps.users);\n"
    "}\n"
)


def test_a_file_without_fastify_that_forwards_a_plugin_server_is_read_for_tracing() -> None:
    assert _roles(FORWARD) & _FASTIFY_ROLES == {FASTIFY_HELPER_CALL, FASTIFY_TRACED_SCOPE}
    [call] = _helpers(FORWARD)
    assert call.target_key == "initUsers"
    assert call.source_symbol == "initAll"
    assert call.meta == {
        PENDING_FACT: FASTIFY_HELPER_CALL,
        "owner": "initAll",
        "owner_kind": PLUGIN,
        "receiver": "server",
        "arg": "0",
        "module": "./users",
        "name": "initUsers",
    }
    [scope] = _claims(FORWARD, FASTIFY_TRACED_SCOPE)
    assert scope.target_key == "initAll"
    assert scope.meta == {
        PENDING_FACT: FASTIFY_TRACED_SCOPE,
        "kind": PLUGIN,
        "exports": json.dumps([["initAll", False]]),
        "receiver": "server",
    }


@pytest.mark.parametrize(
    "source",
    [
        USERS + "export const initAll = (server) => initUsers(server);\n",
        USERS + "export default function (server) { initUsers(server); }\n",
        USERS + "function initAll(server) { initUsers(server); }\nexport { initAll };\n",
        "import * as users from './users';\n"
        "export function initAll(server) { users.initUsers(server); }\n",
        USERS + "export async function initAll(server) { await initUsers(server); }\n",
    ],
)
def test_each_exported_forwarding_form_is_read(source: str) -> None:
    [call] = _helpers(source)
    assert call.meta["owner_kind"] == PLUGIN
    assert "refusal" not in call.meta
    assert len(_claims(source, FASTIFY_TRACED_SCOPE)) == 1


@pytest.mark.parametrize(
    "source",
    [
        # not exported
        USERS + "function initAll(server) { initUsers(server); }\n",
        # a package's function, or one of this file
        "import { initUsers } from 'some-package';\n"
        "export function initAll(server) { initUsers(server); }\n",
        "function initUsers(s) {}\nexport function initAll(server) { initUsers(server); }\n",
        # not the first parameter, or not the first argument
        USERS + "export function initAll(deps, server) { initUsers(server); }\n",
        USERS + "export function initAll(server, deps) { initUsers(deps, server); }\n",
        # off the straight line
        USERS + "export function initAll(server) { if (ready) { initUsers(server); } }\n",
        USERS + "export function initAll(server) { items.forEach(() => initUsers(server)); }\n",
        USERS + "export function initAll(server) { try { initUsers(server); } catch (e) {} }\n",
        # held in an object
        USERS + "export function initAll(server) { initUsers({ server }); }\n",
    ],
)
def test_a_file_without_fastify_that_forwards_nothing_records_no_fastify_claim(
    source: str,
) -> None:
    assert _roles(source).isdisjoint(_FASTIFY_ROLES)


# -- a name a function parameter shares with a module-level instance ------------------------

#: The shape a project writes: the instance is `server` in the module, and `server` is the
#: parameter of the function that receives it.
HANDLERS = (
    "initializeHandlers(server);\n"
    "function initializeHandlers(server) { initializeUserRoutes(server); }\n"
    'function initializeUserRoutes(app) { app.get("/users", getUsers); }\n'
)


def test_an_instance_is_counted_in_the_module_scope_not_in_the_whole_file() -> None:
    source = (
        IMPORT
        + "function createServer() { const app = Fastify(); return app; }\n"
        + "const server = createServer();\n"
        + HANDLERS
    )
    [instance] = _instances(source)
    assert instance.target_key == "server"
    assert instance.meta == {
        PENDING_FACT: FASTIFY_INSTANCE,
        "kind": INSTANCE,
        "receiver": "server",
        "entry": "",
        "local": "createServer()",
        "awaited": "false",
        "status": PROVEN,
        "aliases": "[]",
    }
    first, second = _helpers(source)
    assert first.target_key == "initializeHandlers"
    assert first.source_symbol is None
    assert first.meta == {
        PENDING_FACT: FASTIFY_HELPER_CALL,
        "owner": "server",
        "owner_kind": INSTANCE,
        "receiver": "server",
        "arg": "0",
        "local": "initializeHandlers",
    }
    assert second.target_key == "initializeUserRoutes"
    assert second.source_symbol == "initializeHandlers"
    assert second.meta == {
        PENDING_FACT: FASTIFY_HELPER_CALL,
        "owner": "initializeHandlers",
        "owner_kind": PLUGIN,
        "receiver": "server",
        "arg": "0",
        "local": "initializeUserRoutes",
    }
    # the file imports fastify, so the plugin's route is a route; the global pass chains it
    [route] = _claims(source, FASTIFY_ROUTE)
    assert route.target_key == "GET /users"
    assert route.meta["scope"] == "initializeUserRoutes"


def test_a_shared_parameter_name_does_not_hide_the_instance_in_a_file_without_fastify() -> None:
    source = "import { createServer } from './server';\nconst server = createServer();\n" + HANDLERS
    [instance] = _instances(source)
    assert instance.meta == {
        PENDING_FACT: FASTIFY_INSTANCE,
        "kind": INSTANCE,
        "receiver": "server",
        "entry": "",
        "module": "./server",
        "name": "createServer",
        "awaited": "false",
        "status": PENDING,
        "aliases": "[]",
    }
    first, second = _helpers(source)
    assert (first.meta["owner"], first.meta["owner_kind"]) == ("server", INSTANCE)
    assert first.meta["local"] == "initializeHandlers"
    assert (second.meta["owner"], second.meta["owner_kind"]) == ("initializeHandlers", PLUGIN)
    assert second.meta["local"] == "initializeUserRoutes"
    assert "refusal" not in first.meta
    assert "refusal" not in second.meta
    [route] = _traced(source)
    assert route.target_key == "GET /users"
    assert route.source_symbol == "initializeUserRoutes"
    assert route.meta == {
        PENDING_FACT: FASTIFY_TRACED_ROUTE,
        "method": "GET",
        "path": json.dumps([["t", "/users"]]),
        "scope": "initializeUserRoutes",
        "owner_kind": PLUGIN,
        "provenance": EXACT,
    }
    scopes = _claims(source, FASTIFY_TRACED_SCOPE)
    assert sorted(c.target_key for c in scopes) == ["initializeHandlers", "initializeUserRoutes"]


def test_n21_with_a_parameter_named_like_the_client_is_still_an_instance() -> None:
    source = (
        "import { createClient } from './client';\n"
        "function setup(c) { c.get('/n21', opts); }\n"
        "const c = createClient();\nsetup(c);\n"
    )
    # an imported factory: the parser leaves it to the global pass (pending, no refusal code)
    [instance] = _instances(source)
    assert instance.target_key == "c"
    assert instance.meta["status"] == PENDING
    assert "refusal" not in instance.meta
    assert (instance.meta["module"], instance.meta["name"]) == ("./client", "createClient")
    [call] = _helpers(source)
    assert (call.meta["owner"], call.meta["owner_kind"], call.meta["receiver"]) == (
        "c",
        INSTANCE,
        "c",
    )
    assert [r.target_key for r in _traced(source)] == ["GET /n21"]
    # the same names with a function of the file that is no factory: refused, with its code
    local = (
        "function createClient() { return {}; }\n"
        "function setup(c) { c.get('/n21', opts); }\n"
        "const c = createClient();\nsetup(c);\n"
    )
    [instance] = _instances(local)
    assert instance.meta["status"] == REFUSED
    assert instance.meta["refusal"] == FACTORY_UNPROVEN


def test_a_name_bound_twice_in_the_module_itself_is_no_instance() -> None:
    source = (
        "import { createClient } from './client';\n"
        "const c = createClient();\nfunction c() {}\nc.get('/x', opts);\n"
    )
    assert _instances(source) == []


def test_a_parameter_s_use_of_the_name_is_no_use_of_the_module_s_instance() -> None:
    source = (
        "import { createClient } from './client';\n"
        "const c = createClient();\nfunction setup(c) { c.get('/x', opts); }\n"
    )
    assert _instances(source) == []


# -- routes on instances, entries and factories --------------------------------------------


def test_a_route_on_an_instance_is_a_traced_route_and_never_a_route() -> None:
    source = IMPORT + MAKE + SERVER + "server.get('/a/:id', h);\n"
    [route] = _traced(source)
    assert route.target_key == "GET /a/:id"
    assert route.source_symbol is None
    assert (route.line_start, route.line_end) == (4, 4)
    assert route.meta == {
        PENDING_FACT: FASTIFY_TRACED_ROUTE,
        "method": "GET",
        "path": json.dumps([["t", "/a/:id"]]),
        "scope": "server",
        "owner_kind": INSTANCE,
        "provenance": EXACT,
    }
    assert _claims(source, FASTIFY_ROUTE) == []
    served = [
        r
        for r in _refs(source)
        if r.edge_type is EdgeType.SERVES_HTTP_ROUTE and PENDING_FACT not in r.meta
    ]
    assert served == []


def test_a_traced_route_names_its_path_as_a_route_does() -> None:
    source = (
        IMPORT
        + "import { BASE } from './base';\n"
        + MAKE
        + SERVER
        + "server.get(`${BASE}/x`, h);\n"
        + "server.route({ method: ['GET', 'HEAD'], url: '/y', handler: h });\n"
    )
    first, *rest = _traced(source)
    assert first.target_key == "GET {BASE}/x"
    assert first.meta["provenance"] == COMPOSED
    assert json.loads(first.meta["path"]) == [["r", "./base", "BASE"], ["t", "/x"]]
    assert [(c.target_key, c.meta["method"]) for c in rest] == [
        ("GET /y", "GET"),
        ("HEAD /y", "HEAD"),
    ]


def test_a_route_on_the_application_a_factory_builds_belongs_to_the_factory() -> None:
    source = IMPORT + (
        "function make() {\n  const app = Fastify();\n  app.get('/health', h);\n  return app;\n}\n"
    )
    [route] = _traced(source)
    assert route.target_key == "GET /health"
    assert route.source_symbol == "make"
    assert route.meta["scope"] == "make()"
    assert route.meta["owner_kind"] == FACTORY


def test_a_route_on_an_entry_application_belongs_to_the_entry() -> None:
    source = IMPORT + START.replace("await app", "app.get('/e', h);\n  await app") + "start();\n"
    [route] = _traced(source)
    assert route.target_key == "GET /e"
    assert route.source_symbol == "start"
    assert route.meta["scope"] == "start>app"
    assert route.meta["owner_kind"] == ENTRY


def test_a_route_on_an_instance_in_an_entry_function_belongs_to_that_instance() -> None:
    source = (
        IMPORT + MAKE + "function start() {\n  const server = make();\n  server.get('/s', h);\n}\n"
        "start();\n"
    )
    [route] = _traced(source)
    assert route.meta["scope"] == "start>server"
    assert route.meta["owner_kind"] == INSTANCE


@pytest.mark.parametrize(
    "form",
    [
        "server.get('/a', h);",
        "await server.get('/a', h);",
        "void server.get('/a', h);",
        "const r = server.get('/a', h);",
        "const r = await server.get('/a', h);",
    ],
)
def test_a_route_in_the_body_of_the_receiver_counts_in_each_straight_form(form: str) -> None:
    assert [c.target_key for c in _traced(IMPORT + MAKE + SERVER + form + "\n")] == ["GET /a"]


@pytest.mark.parametrize(
    "use",
    [
        "function f() { server.get('/a', h); }",
        "server.after(() => { server.get('/a', h); });",
        "server.after(() => server.get('/a', h));",
        "if (flag) { server.get('/a', h); }",
        "for (const x of xs) { server.get('/a', h); }",
        "try { server.get('/a', h); } catch (e) { report(e); }",
        "let r = server.get('/a', h);",
        "r = server.get('/a', h);",
        "server.get('/a', h).then(done);",
        "wrap(server.get('/a', h));",
        "flag && server.get('/a', h);",
    ],
)
def test_a_route_that_is_not_on_a_straight_line_of_the_receivers_body_is_no_route(
    use: str,
) -> None:
    source = IMPORT + MAKE + SERVER + use + "\nserver.listen();\n"
    assert _traced(source) == []
    assert _claims(source, FASTIFY_ROUTE) == []
    assert [c.target_key for c in _instances(source)] == ["server"]


def test_routes_in_nested_code_of_an_entry_or_a_factory_are_no_routes() -> None:
    entry = IMPORT + START.replace("await app", "app.after(() => app.get('/e', h));\n  await app")
    assert _traced(entry + "start();\n") == []
    factory = IMPORT + (
        "function make() {\n  const app = Fastify();\n  if (c) { app.get('/y', h); }\n"
        "  app.after(() => app.get('/x', h));\n  return app;\n}\n"
    )
    assert [c.target_key for c in _factories(factory)] == ["make()"]
    assert _traced(factory) == []


def test_a_plugin_keeps_its_rule_for_routes_but_its_calls_need_the_straight_line() -> None:
    source = (
        "import type { FastifyInstance } from 'fastify';\n"
        "import { initUsers } from './users';\nimport { initOther } from './other';\n"
        "export default async function routes(server: FastifyInstance) {\n"
        "  initUsers(server);\n  if (flag) {\n    initOther(server);\n    server.get('/c', h);\n"
        "  }\n  server.after(() => server.get('/d', h));\n}\n"
    )
    assert [c.target_key for c in _claims(source, FASTIFY_ROUTE)] == ["GET /c", "GET /d"]
    assert _traced(source) == []
    first, second = _helpers(source)
    assert (first.meta["owner"], first.meta["owner_kind"]) == ("routes", PLUGIN)
    assert (first.meta["module"], first.meta["name"]) == ("./users", "initUsers")
    assert "refusal" not in first.meta
    assert second.meta["refusal"] == HELPER_CONTROL_FLOW


# -- helper calls -------------------------------------------------------------------------


def test_a_call_handing_the_instance_to_a_function_of_the_file_is_a_helper_call() -> None:
    source = IMPORT + MAKE + "function initUsers(app) { app.get('/u', h); }\n" + SERVER
    source += "initUsers(server);\n"
    [call] = _helpers(source)
    assert call.target_key == "initUsers"
    assert (call.line_start, call.line_end) == (5, 5)
    assert call.source_symbol is None
    assert call.meta == {
        PENDING_FACT: FASTIFY_HELPER_CALL,
        "owner": "server",
        "owner_kind": INSTANCE,
        "receiver": "server",
        "arg": "0",
        "local": "initUsers",
    }


@pytest.mark.parametrize(
    ("imports", "call", "target"),
    [
        (
            "import { initUsers } from './users';",
            "initUsers(server)",
            {"module": "./users", "name": "initUsers"},
        ),
        (
            "import { setup as initUsers } from '../routes';",
            "initUsers(server)",
            {"module": "../routes", "name": "setup"},
        ),
        (
            "import initUsers from './users';",
            "initUsers(server)",
            {"module": "./users", "name": "default"},
        ),
        (
            "import * as users from './users';",
            "users.initUsers(server)",
            {"module": "./users", "name": "initUsers"},
        ),
    ],
)
def test_a_call_handing_the_instance_to_a_relative_import_names_the_module(
    imports: str, call: str, target: dict[str, str]
) -> None:
    [claim] = _helpers(IMPORT + imports + "\n" + MAKE + SERVER + call + ";\n")
    assert claim.target_key == call.split("(")[0]
    assert claim.meta == {
        PENDING_FACT: FASTIFY_HELPER_CALL,
        "owner": "server",
        "owner_kind": INSTANCE,
        "receiver": "server",
        "arg": "0",
        **target,
    }


def test_the_index_of_the_receiver_among_the_arguments_is_recorded() -> None:
    [claim] = _helpers(IMPORT + USERS + MAKE + SERVER + "initUsers(config, server);\n")
    assert claim.meta["arg"] == "1"
    assert "refusal" not in claim.meta


def test_a_call_that_names_no_function_of_the_file_or_a_relative_module_is_no_helper_call() -> None:
    source = IMPORT + "import { setup } from 'some-package';\n" + MAKE + SERVER
    assert _helpers(source + "setup(server);\nunknown(server);\nserver.listen();\n") == []


def test_an_entry_application_and_a_factory_are_owners_of_the_calls_that_hand_them_on() -> None:
    entry = (
        IMPORT + USERS + "async function start() {\n  const app = Fastify();\n  initUsers(app);\n"
        "  await app.listen();\n}\nstart();\n"
    )
    [claim] = _helpers(entry)
    assert (claim.meta["owner"], claim.meta["owner_kind"]) == ("start>app", ENTRY)
    assert claim.source_symbol == "start"
    assert "refusal" not in claim.meta
    factory = (
        IMPORT + USERS + "function make() {\n  const app = Fastify();\n  initUsers(app);\n"
        "  return app;\n}\n"
    )
    [claim] = _helpers(factory)
    assert (claim.meta["owner"], claim.meta["owner_kind"]) == ("make()", FACTORY)
    assert "refusal" not in claim.meta


def _refusal(source: str) -> str | None:
    [claim] = _helpers(source)
    return claim.meta.get("refusal")


HANDED = IMPORT + USERS + MAKE + SERVER


@pytest.mark.parametrize(
    ("statement", "code"),
    [
        # a condition, a callback, a loop, a try, a nested function, a chain on the call
        ("if (process.env.X) initUsers(server);", HELPER_CONTROL_FLOW),
        ("server.after(() => initUsers(server));", HELPER_CONTROL_FLOW),
        ("for (const x of xs) { initUsers(server); }", HELPER_CONTROL_FLOW),
        ("try { initUsers(server); } catch (e) { report(e); }", HELPER_CONTROL_FLOW),
        ("function boot() { initUsers(server); }", HELPER_CONTROL_FLOW),
        ("initUsers(server).then(done);", HELPER_CONTROL_FLOW),
        # a callee no name gives
        ("helpers[0](server);", HELPER_DYNAMIC),
        # a callee this parser does not follow
        ("obj.initUsers(server);", HELPER_CALLEE_UNSUPPORTED),
        ("this.initUsers(server);", HELPER_CALLEE_UNSUPPORTED),
        ("initUsers.call(null, server);", HELPER_CALLEE_UNSUPPORTED),
        ("initUsers.apply(null, [server]);", HELPER_CALLEE_UNSUPPORTED),
        ("initUsers.bind(null, server);", HELPER_CALLEE_UNSUPPORTED),
        ("(function (s) {})(server);", HELPER_CALLEE_UNSUPPORTED),
        ("const alias = initUsers;\nalias(server);", HELPER_CALLEE_UNSUPPORTED),
        # the receiver inside an object, an array or a spread
        ("initUsers({ server });", HELPER_ARGUMENT_UNSUPPORTED),
        ("initUsers({ server: server });", HELPER_ARGUMENT_UNSUPPORTED),
        ("initUsers([server]);", HELPER_ARGUMENT_UNSUPPORTED),
        ("initUsers(...[server]);", HELPER_ARGUMENT_UNSUPPORTED),
        # the first reason that applies is the one recorded
        ("if (flag) helpers[0]({ server });", HELPER_CONTROL_FLOW),
        ("helpers[0]({ server });", HELPER_DYNAMIC),
        ("obj.initUsers({ server });", HELPER_CALLEE_UNSUPPORTED),
    ],
)
def test_a_helper_call_the_parser_would_not_trust_records_why(statement: str, code: str) -> None:
    assert _refusal(HANDED + statement + "\nserver.listen();\n") == code


def test_a_callee_that_is_dynamic_names_no_target() -> None:
    [claim] = _helpers(HANDED + "helpers[0](server);\nserver.listen();\n")
    assert claim.target_key == "helpers[0]"
    assert not {"local", "module", "name"} & set(claim.meta)


def test_the_receiver_is_found_wherever_among_the_arguments_a_refused_call_puts_it() -> None:
    [claim] = _helpers(HANDED + "initUsers.call(null, server);\nserver.listen();\n")
    assert claim.meta["arg"] == "1"
    assert claim.meta["refusal"] == HELPER_CALLEE_UNSUPPORTED


def test_a_callee_that_is_a_parameter_is_dynamic() -> None:
    source = (
        IMPORT + "function start(init) {\n  const app = Fastify();\n  init(app);\n"
        "  app.listen();\n}\nstart();\n"
    )
    assert _refusal(source) == HELPER_DYNAMIC


def test_a_callee_hidden_by_a_local_name_is_shadowed() -> None:
    source = (
        IMPORT + USERS + "async function start() {\n  const app = Fastify();\n"
        "  const initUsers = () => {};\n  initUsers(app);\n  app.listen();\n}\nstart();\n"
    )
    assert _refusal(source) == HELPER_SHADOWED


def test_a_function_declared_in_the_function_that_calls_it_is_not_followed() -> None:
    source = (
        IMPORT + "async function start() {\n  const app = Fastify();\n"
        "  function inner(a) {}\n  inner(app);\n  app.listen();\n}\nstart();\n"
    )
    assert _refusal(source) == HELPER_CALLEE_UNSUPPORTED


def test_a_receiver_handed_on_before_its_binding_is_refused() -> None:
    source = IMPORT + USERS + MAKE + "initUsers(server);\n" + SERVER + "server.listen();\n"
    assert _refusal(source) == RECEIVER_BEFORE_BINDING
    [instance] = _instances(source)
    assert instance.meta["refusal"] == RECEIVER_BEFORE_BINDING


def test_a_callee_bound_after_the_call_is_refused() -> None:
    source = (
        IMPORT + MAKE + SERVER + "initLater(server);\n"
        "const initLater = (s) => { s.get('/l', h); };\nserver.listen();\n"
    )
    [claim] = _helpers(source)
    assert claim.meta["refusal"] == CALLEE_BEFORE_BINDING
    assert claim.meta["local"] == "initLater"
    declared_first = source.replace("initLater(server);\n", "").replace(
        "server.listen();\n", "initLater(server);\n"
    )
    assert _refusal(declared_first) is None


@pytest.mark.parametrize("started", ["await app.listen();", "await app.ready();"])
def test_a_helper_call_after_the_application_has_started_is_refused(started: str) -> None:
    source = (
        IMPORT + USERS + "async function start() {\n  const app = Fastify();\n"
        f"  {started}\n  initUsers(app);\n}}\nstart();\n"
    )
    assert _refusal(source) == AFTER_LISTEN


def test_a_helper_call_after_an_awaited_listen_on_the_module_s_instance_is_refused() -> None:
    source = HANDED + "await server.listen();\ninitUsers(server);\n"
    assert _refusal(source) == AFTER_LISTEN


def test_a_listen_that_is_not_awaited_or_comes_later_does_not_refuse_the_call() -> None:
    assert _refusal(HANDED + "server.listen();\ninitUsers(server);\n") is None
    assert _refusal(HANDED + "initUsers(server);\nawait server.listen();\n") is None


def test_a_call_that_hands_two_receivers_on_states_each() -> None:
    source = HANDED + "const other = make();\ninitUsers(server, other);\nother.listen();\n"
    first, second = _helpers(source)
    assert (first.meta["owner"], first.meta["arg"]) == ("server", "0")
    assert (second.meta["owner"], second.meta["arg"]) == ("other", "1")


# -- helper calls on the module's own application ------------------------------------------

ROUTES = IMPORT + "import { setupRoutes } from './routes';\n"
APPLICATION = "const app = Fastify();\n"


def test_a_call_handing_the_module_s_application_on_is_a_helper_call() -> None:
    source = ROUTES + APPLICATION + "setupRoutes(app);\napp.listen();\n"
    [call] = _helpers(source)
    assert call.target_key == "setupRoutes"
    assert (call.line_start, call.line_end) == (4, 4)
    assert call.source_symbol is None
    assert call.meta == {
        PENDING_FACT: FASTIFY_HELPER_CALL,
        "owner": "app",
        "owner_kind": APP,
        "receiver": "app",
        "arg": "0",
        "module": "./routes",
        "name": "setupRoutes",
    }
    # the application keeps its routes: served here, never traced
    served = ROUTES + APPLICATION + "app.get('/a', h);\nsetupRoutes(app);\n"
    assert _traced(served) == []
    assert [
        r
        for r in _refs(served)
        if r.edge_type is EdgeType.SERVES_HTTP_ROUTE and PENDING_FACT not in r.meta
    ]
    assert len(_helpers(served)) == 1


@pytest.mark.parametrize(
    ("statement", "code"),
    [
        ("if (flag) setupRoutes(app);", HELPER_CONTROL_FLOW),
        ("function boot() { setupRoutes(app); }", HELPER_CONTROL_FLOW),
        ("app.after(() => setupRoutes(app));", HELPER_CONTROL_FLOW),
        ("setupRoutes(app).then(done);", HELPER_CONTROL_FLOW),
        ("setupRoutes({ app });", HELPER_ARGUMENT_UNSUPPORTED),
        ("helpers[0](app);", HELPER_DYNAMIC),
        ("await app.listen();\nsetupRoutes(app);", AFTER_LISTEN),
        ("await app.ready();\nsetupRoutes(app);", AFTER_LISTEN),
    ],
)
def test_a_helper_call_on_the_module_s_application_the_parser_would_not_trust_records_why(
    statement: str, code: str
) -> None:
    assert _refusal(ROUTES + APPLICATION + statement + "\n") == code


def test_a_listen_that_comes_later_does_not_refuse_a_call_on_the_module_s_application() -> None:
    assert _refusal(ROUTES + APPLICATION + "setupRoutes(app);\nawait app.listen();\n") is None
    assert _refusal(ROUTES + APPLICATION + "app.listen();\nsetupRoutes(app);\n") is None


def test_the_module_s_application_handed_on_before_its_binding_is_refused() -> None:
    source = ROUTES + "setupRoutes(app);\n" + APPLICATION + "app.listen();\n"
    assert _refusal(source) == RECEIVER_BEFORE_BINDING


def test_a_function_of_the_file_bound_after_the_call_is_refused_for_the_application_too() -> None:
    source = (
        IMPORT
        + APPLICATION
        + "setupRoutes(app);\nconst setupRoutes = (a) => { a.get('/l', h); };\napp.listen();\n"
    )
    assert _refusal(source) == CALLEE_BEFORE_BINDING


# -- D6: the application is bound once in the module's own scope, not in the whole file --------
#
# Before D6 the module's application was bound once in the WHOLE file, so a parameter or a
# nested binding of the same name made `server` below no application: no `app` scope claim,
# the top-level route unserved, the helper call recorded nowhere. After D6 the count is the
# module's own (as it already was for a module-level instance), so the application stands.
# Plugin, factory and entry rules are untouched and still count the whole file.


def _served(source: str) -> set[str]:
    """The routes the file itself serves: the application's, never a claim."""
    return {
        r.target_key
        for r in _refs(source)
        if r.edge_type is EdgeType.SERVES_HTTP_ROUTE and PENDING_FACT not in r.meta
    }


def _app_scopes(source: str) -> list[str]:
    return [c.target_key for c in _claims(source, FASTIFY_SCOPE) if c.meta["kind"] == APP]


def test_d6_a_parameter_named_like_the_application_does_not_hide_it() -> None:
    source = (
        ROUTES
        + "const server = Fastify();\nfunction plugin(server) { server.get('/x', h); }\n"
        + "setupRoutes(server);\n"
    )
    # before D6: no application, no helper call; now the helper call is the application's
    assert _app_scopes(source) == ["server"]
    [call] = _helpers(source)
    assert call.meta["owner"] == "server"
    assert call.meta["owner_kind"] == APP
    assert call.meta["receiver"] == "server"
    assert _instances(source) == []
    # the parameter's own route is still the plugin's, as on master
    [route] = _claims(source, FASTIFY_ROUTE)
    assert route.meta["scope"] == "plugin"
    assert route.target_key == "GET /x"
    assert _served(source) == set()


def test_d6_the_module_level_route_of_such_an_application_is_served() -> None:
    source = IMPORT + (
        "const server = Fastify();\n"
        "function routes(server) { server.get('/x', h); }\n"
        "server.get('/top', h);\n"
        "routes(server);\n"
    )
    # before D6: no `app` scope and nothing served; now the application serves /top
    assert _app_scopes(source) == ["server"]
    assert _served(source) == {"GET /top"}
    # the route in the function that receives it is that function's, never the application's
    [route] = _claims(source, FASTIFY_ROUTE)
    assert (route.target_key, route.meta["scope"]) == ("GET /x", "routes")
    [call] = _helpers(source)
    assert (call.meta["owner"], call.meta["owner_kind"]) == ("server", APP)


@pytest.mark.parametrize(
    "extra",
    [
        "function routes(server) { server.get('/x', h); }",
        "const wire = (server) => { server.get('/x', h); };",
        "function other() { const server = 1; return server; }",
        "function other() { function server() {} return server; }",
        "function other(a, { server }) { return a + server; }",
    ],
)
def test_d6_a_binding_of_the_name_inside_a_function_does_not_disqualify_the_application(
    extra: str,
) -> None:
    source = IMPORT + "const server = Fastify();\n" + extra + "\nserver.get('/top', h);\n"
    assert _app_scopes(source) == ["server"]
    assert "GET /top" in _served(source)


@pytest.mark.parametrize(
    "extra",
    [
        "let app = 1;",
        "const app = Fastify();",
        "function app() {}",
        "var app;",
    ],
)
def test_d6_a_module_level_double_binding_is_still_no_application(extra: str) -> None:
    source = IMPORT + "const app = Fastify();\n" + extra + "\napp.get('/top', h);\n"
    assert _app_scopes(source) == []
    assert _served(source) == set()


def test_d6_a_double_binding_in_the_module_is_not_cured_by_a_parameter() -> None:
    source = IMPORT + (
        "const app = Fastify();\nlet app = 1;\nfunction routes(app) {}\napp.get('/top', h);\n"
    )
    assert _app_scopes(source) == []
    assert _served(source) == set()


def test_d6_a_plugin_named_like_the_application_in_the_module_is_still_no_scope() -> None:
    source = IMPORT + (
        "const server = Fastify();\nconst server = async (s) => { s.get('/x', h); };\n"
        "server.get('/top', h);\n"
    )
    assert _claims(source, FASTIFY_SCOPE) == []
    assert _served(source) == set()


def test_d6_a_function_that_gets_the_application_under_another_name_is_as_before() -> None:
    source = IMPORT + (
        "const app = Fastify();\n"
        "function routes(instance) { instance.get('/x', h); }\n"
        "app.get('/top', h);\n"
        "routes(app);\n"
    )
    assert _app_scopes(source) == ["app"]
    assert _served(source) == {"GET /top"}
    [route] = _claims(source, FASTIFY_ROUTE)
    assert (route.target_key, route.meta["scope"]) == ("GET /x", "routes")
    [call] = _helpers(source)
    assert (call.meta["owner"], call.meta["owner_kind"], call.meta["receiver"]) == (
        "app",
        APP,
        "app",
    )


def test_d6_a_required_application_is_judged_in_the_module_scope_too() -> None:
    source = (
        "const Fastify = require('fastify');\n"
        "const server = Fastify();\n"
        "const direct = require('fastify')({ logger: true });\n"
        "function routes(server, direct) {}\n"
        "server.get('/a', h);\n"
        "direct.get('/b', h);\n"
    )
    assert _app_scopes(source) == ["server", "direct"]
    assert _served(source) == {"GET /a", "GET /b"}


# -- D6b: the factory import is bound once in the module's own scope, not in the whole file ----
#
# Before D6b the local name of `import fastify from 'fastify'` (or the named, the
# `require('fastify')` factory) had to be bound once in the WHOLE file, so the very common
#     import fastify from 'fastify';  const app = fastify();
#     app.register(async (fastify) => { ... });
# bound `fastify` twice (the import and the parameter): the import was not recorded as the
# factory, `app` was no application, `_app_scopes` was [] and nothing the file serves was read.
# After D6b the import is judged in the module's scope, as D6 did for the application: the
# parameter is a binding of the plugin, not of the module. A true module-level double binding
# (a second import, a module-level const or function of the same name) is still refused.
# Relative imports (named, default, namespace) are judged the same way. The namespace form of
# the `fastify` package, `import * as fastify from 'fastify'`, is not read as a factory by the
# parser (only relative namespaces are), so there is no D6b test for it.


def test_d6b_a_parameter_named_like_the_factory_import_does_not_hide_the_application() -> None:
    source = (
        "import fastify from 'fastify';\n"
        "const app = fastify();\n"
        "app.register(async (fastify) => { app.get('/x', h); });\n"
        "app.get('/top', h);\n"
    )
    # before D6b: no application, nothing served; now `app` is one and serves its own route
    assert _app_scopes(source) == ["app"]
    assert "GET /top" in _served(source)


@pytest.mark.parametrize(
    "imports",
    [
        "import fastify from 'fastify';",
        "import { fastify } from 'fastify';",
        "import { fastify as build } from 'fastify';",
    ],
)
def test_d6b_every_factory_import_form_is_judged_in_the_module_scope(imports: str) -> None:
    local = "build" if "as build" in imports else "fastify"
    source = (
        imports
        + f"\nconst app = {local}();\n"
        + f"app.register(async ({local}) => {{ {local}.get('/x', h); }});\n"
        + "app.get('/top', h);\n"
    )
    assert _app_scopes(source) == ["app"]
    assert "GET /top" in _served(source)


@pytest.mark.parametrize(
    "extra",
    [
        "const fastify = 1;",
        "let fastify;",
        "function fastify() {}",
        "import fastify from 'other';",
    ],
)
def test_d6b_a_module_level_double_binding_of_the_factory_name_is_still_no_application(
    extra: str,
) -> None:
    source = (
        "import fastify from 'fastify';\n"
        + extra
        + "\nconst app = fastify();\napp.get('/top', h);\n"
    )
    assert _app_scopes(source) == []
    assert _served(source) == set()


def test_d6b_a_module_level_double_binding_is_not_cured_by_a_parameter() -> None:
    source = (
        "import fastify from 'fastify';\nconst fastify = 1;\n"
        "const app = fastify();\napp.register(async (fastify) => {});\napp.get('/top', h);\n"
    )
    assert _app_scopes(source) == []
    assert _served(source) == set()


@pytest.mark.parametrize(
    "nested",
    [
        "function other() { const fastify = 1; return fastify; }",
        "function other() { function fastify() {} return fastify; }",
        "const wire = (fastify) => { fastify.get('/x', h); };",
        "function other(a, { fastify }) { return a + fastify; }",
    ],
)
def test_d6b_a_nested_binding_of_the_factory_name_does_not_disqualify_the_import(
    nested: str,
) -> None:
    source = (
        "import fastify from 'fastify';\nconst app = fastify();\n"
        + nested
        + "\napp.get('/top', h);\n"
    )
    assert _app_scopes(source) == ["app"]
    assert "GET /top" in _served(source)


def test_d6b_a_required_factory_is_recognised_with_or_without_a_shadowing_parameter() -> None:
    plain = "const fastify = require('fastify');\nconst app = fastify();\napp.get('/top', h);\n"
    assert _app_scopes(plain) == ["app"]
    assert _served(plain) == {"GET /top"}
    # before D6b the parameter made the required factory name a second binding: no application
    shadowed = (
        "const fastify = require('fastify');\nconst app = fastify();\n"
        "function plugin(fastify) { fastify.get('/x', h); }\n"
        "app.register(async (fastify) => {});\napp.get('/top', h);\n"
    )
    assert _app_scopes(shadowed) == ["app"]
    assert "GET /top" in _served(shadowed)


def test_d6b_a_required_factory_bound_twice_in_the_module_is_still_no_application() -> None:
    source = (
        "const fastify = require('fastify');\nlet fastify;\n"
        "const app = fastify();\napp.get('/top', h);\n"
    )
    assert _app_scopes(source) == []
    assert _served(source) == set()


@pytest.mark.parametrize(
    ("imports", "call", "module", "name"),
    [
        ("import { createServer } from './server';", "createServer()", "./server", "createServer"),
        ("import createServer from './server';", "createServer()", "./server", "default"),
        (
            "import * as server from './server';",
            "server.createServer()",
            "./server",
            "createServer",
        ),
    ],
)
def test_d6b_a_parameter_named_like_a_relative_import_does_not_hide_it(
    imports: str, call: str, module: str, name: str
) -> None:
    local = "server" if "* as server" in imports else "createServer"
    source = IMPORT + imports + f"\nconst app = {call};\n"
    source += f"function f({local}) {{}}\napp.listen();\n"
    claim = _only(source)
    # before D6b: no instance claim at all; now the global pass is left to resolve it
    assert claim.target_key == "app"
    assert (claim.meta["module"], claim.meta["name"]) == (module, name)
    assert claim.meta["status"] == PENDING


# -- registrations on the new receivers ---------------------------------------------------

PLUGIN_IMPORT = IMPORT + "import plugin from './plugin';\n"


def test_a_registration_on_an_instance_names_the_instance_as_owner() -> None:
    source = PLUGIN_IMPORT + MAKE + SERVER + "server.register(plugin, { prefix: '/p' });\n"
    [claim] = _claims(source, FASTIFY_REGISTER)
    assert claim.meta == {
        PENDING_FACT: FASTIFY_REGISTER,
        "owner": "server",
        "owner_kind": INSTANCE,
        "module": "./plugin",
        "name": "default",
        "wrapped": "false",
        "readable": "true",
        "prefix": '"/p"',
    }


def test_a_registration_on_an_entry_application_or_a_factory_names_it_as_owner() -> None:
    entry = PLUGIN_IMPORT + START.replace("await app", "app.register(plugin);\n  await app")
    [claim] = _claims(entry + "start();\n", FASTIFY_REGISTER)
    assert (claim.meta["owner"], claim.meta["owner_kind"]) == ("start>app", ENTRY)
    factory = PLUGIN_IMPORT + (
        "function make() {\n  const app = Fastify();\n  app.register(plugin);\n  return app;\n}\n"
    )
    [claim] = _claims(factory, FASTIFY_REGISTER)
    assert (claim.meta["owner"], claim.meta["owner_kind"]) == ("make()", FACTORY)


def test_a_registration_that_is_not_on_a_straight_line_is_recorded_nowhere() -> None:
    source = (
        PLUGIN_IMPORT + MAKE + SERVER + "server.after(() => server.register(plugin));\n"
        "if (flag) { server.register(plugin); }\nserver.listen();\n"
    )
    assert _claims(source, FASTIFY_REGISTER) == []


# -- the per-file bounds ---------------------------------------------------------------------


def test_a_file_states_at_most_256_helper_calls_in_the_order_it_makes_them() -> None:
    source = IMPORT + "import { init } from './init';\n" + MAKE + SERVER
    source += "init(server);\n" * 300
    claims = _helpers(source)
    assert len(claims) == 256
    assert [c.line_start for c in claims] == list(range(5, 5 + 256))


def test_a_file_states_at_most_256_traced_routes_in_the_order_it_declares_them() -> None:
    source = IMPORT + MAKE + SERVER + "".join(f"server.get('/r{i}', h);\n" for i in range(300))
    claims = _traced(source)
    assert [c.target_key for c in claims] == [f"GET /r{i}" for i in range(256)]


# -- D6b: the module's binding is read where it is the module's -------------------------------
#
# D6b made a factory import eligible when its name is bound once in the module's own scope, so a
# parameter of the same name no longer disqualifies it. But two call sites still read that name
# without asking what it means where it is called: `_fastify_call` took any call of the factory
# name for `Fastify(...)`, and `_factory_callee` took any call of an imported name (or of a
# relative namespace's member) for the imported factory. Before the fix, a function taking the
# factory's name as a parameter, `function make(fastify) { const app = fastify(); ... }`, was a
# factory scope with its routes traced, an entry function was a proven entry application, and
# `main(createServer)` / `main(lib)` recorded an instance of the imported factory. After the fix
# the name means the parameter inside the function that binds it, so none of these is recorded.
# The same files with a distinct parameter name behave as D6b intends (the controls).

SHADOW_IMPORT = "import fastify from 'fastify';\n"
FACTORY_SHADOW = (
    "function make(fastify) {\n  const app = fastify();\n  app.get('/x', h);\n  return app;\n}\n"
)
ENTRY_SHADOW = (
    "async function start(fastify) {\n  const app = fastify();\n  app.get('/e', h);\n"
    "  await app.listen();\n}\nstart();\n"
)
SERVER_IMPORT = "import { initA } from './a';\nimport { createServer } from './server';\n"
LIB_IMPORT = "import { initA } from './a';\nimport * as lib from './lib';\n"


def test_d6b_lexical_a_function_taking_the_factory_name_as_a_parameter_is_no_factory() -> None:
    source = SHADOW_IMPORT + FACTORY_SHADOW
    assert _factories(source) == []
    assert _traced(source) == []
    # the control: the same function with a distinct parameter name is a factory with its route
    control = source.replace("make(fastify)", "make(options)")
    [scope] = _factories(control)
    assert scope.target_key == "make()"
    [route] = _traced(control)
    assert (route.target_key, route.meta["scope"]) == ("GET /x", "make()")


def test_d6b_lexical_a_local_function_that_shadows_the_factory_is_no_proven_instance() -> None:
    source = SHADOW_IMPORT + FACTORY_SHADOW + "const server = make();\nserver.listen();\n"
    # `make` is a local function that is no factory: the instance is refused, never proven.
    # Before the fix `make` was a factory and this instance was PROVEN.
    claim = _only(source)
    assert claim.meta["status"] == REFUSED
    assert claim.meta["refusal"] == FACTORY_UNPROVEN
    assert _factories(source) == []


def test_d6b_lexical_an_entry_taking_the_factory_name_as_a_parameter_records_no_entry() -> None:
    source = SHADOW_IMPORT + ENTRY_SHADOW
    # the actual recorded outcome is no claim at all: the call `fastify()` inside `start` is
    # no `Fastify(...)`, so there is no entry application (neither proven nor refused), and
    # its route is no traced route. Before the fix this was `start>app` with status PROVEN.
    assert _instances(source) == []
    assert _traced(source) == []
    # the control: a distinct parameter name leaves `fastify()` the imported factory
    control = source.replace("start(fastify)", "start(options)")
    claim = _only(control)
    assert claim.target_key == "start>app"
    assert (claim.meta["kind"], claim.meta["status"]) == (ENTRY, PROVEN)
    assert [r.target_key for r in _traced(control)] == ["GET /e"]


def test_d6b_lexical_a_parameter_named_like_an_imported_factory_records_no_instance() -> None:
    body = "async function main({0}) {{\n  const server = createServer();\n  initA(server);\n}}\n"
    source = SHADOW_IMPORT + SERVER_IMPORT + body.format("createServer") + "main(x);\n"
    # before the fix: a PENDING instance of `./server`'s `createServer`; now none
    assert _instances(source) == []
    # the control: a distinct parameter name is the D6b case, an instance left to the global pass
    control = SHADOW_IMPORT + SERVER_IMPORT + body.format("build") + "main(x);\n"
    claim = _only(control)
    assert claim.target_key == "main>server"
    assert (claim.meta["module"], claim.meta["name"]) == ("./server", "createServer")
    assert (claim.meta["kind"], claim.meta["status"]) == (INSTANCE, PENDING)


def test_d6b_lexical_a_parameter_named_like_a_relative_namespace_records_no_instance() -> None:
    body = (
        "async function main({0}) {{\n  const server = lib.createServer();\n  initA(server);\n}}\n"
    )
    source = SHADOW_IMPORT + LIB_IMPORT + body.format("lib") + "main(x);\n"
    # before the fix: a PENDING instance of `./lib`'s `createServer`; now none
    assert _instances(source) == []
    control = SHADOW_IMPORT + LIB_IMPORT + body.format("other") + "main(x);\n"
    claim = _only(control)
    assert claim.target_key == "main>server"
    assert (claim.meta["module"], claim.meta["name"]) == ("./lib", "createServer")
    assert (claim.meta["kind"], claim.meta["status"]) == (INSTANCE, PENDING)


def test_d6b_lexical_a_parameter_named_require_builds_no_factory() -> None:
    read = "const f = require('fastify');\n"  # makes the file one that reads Fastify
    body = "function make({0}) {{\n  const app = require('fastify')();\n  return app;\n}}\n"
    assert _factories(read + body.format("require")) == []
    # the control: with the global `require`, `make` is a factory
    assert [c.target_key for c in _factories(read + body.format(""))] == ["make()"]


FACTORY_EXPRESSION = "const make = function {0}() {{ const app = fastify(); return app; }};\n"


def test_d6b_lexical_a_named_function_expression_named_like_the_factory_is_no_factory() -> None:
    # the name of a function expression is bound inside the expression, so the `fastify()` in
    # its body calls the function itself, not the import
    source = SHADOW_IMPORT + FACTORY_EXPRESSION.format("fastify")
    assert _factories(source) == []
    # the control: a distinct inner name leaves `fastify()` the import, and `make` a factory
    control = SHADOW_IMPORT + FACTORY_EXPRESSION.format("build")
    assert [c.target_key for c in _factories(control)] == ["make()"]


def test_d6b_lexical_a_function_expression_named_like_the_factory_is_no_proven_instance() -> None:
    use = "const server = make();\nserver.listen();\n"
    source = SHADOW_IMPORT + FACTORY_EXPRESSION.format("fastify") + use
    # `make` is a local function that is no factory: the instance is refused, never proven.
    # Before the fix `make` was a factory and this instance was PROVEN.
    claim = _only(source)
    assert claim.meta["status"] == REFUSED
    assert claim.meta["refusal"] == FACTORY_UNPROVEN
    assert claim.meta["local"] == "make()"
    # the control: a distinct inner name makes `make` a factory with a proven instance
    control = _only(SHADOW_IMPORT + FACTORY_EXPRESSION.format("build") + use)
    assert control.meta["status"] == PROVEN
    assert "refusal" not in control.meta
