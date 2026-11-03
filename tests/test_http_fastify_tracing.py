"""Fastify factories, instances, entry applications and receiver aliases (M8, C1).

The parser's side of tracing a Fastify application through a function: which functions
are factories, which ``const x = F()`` is an instance of one, which function builds an
application that the file provably runs, and which names are the same receiver. Each is a
claim for the global pass; nothing here serves a route. Every refusal carries a stable
code on the claim, and every shape is asserted as a closed set.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from repoatlas.models.parsing import ParsedReference
from repoatlas.parsers.http_facts import FASTIFY_INSTANCE, FASTIFY_SCOPE, PENDING_FACT
from repoatlas.parsers.http_fastify import (
    CALLEE_BEFORE_BINDING,
    ENTRY,
    ENTRY_NOT_CALLED,
    FACTORY,
    FACTORY_NOT_AWAITED,
    FACTORY_UNPROVEN,
    INSTANCE,
    PENDING,
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
    text = IMPORT + source + "\nconst server = make();\n"
    assert _factories(text) == []
    # a function with a top-level `const app = Fastify()` also yields an entry-app claim
    # (`make>app`, refused as entry-not-called); the instance is the claim for `server`
    [claim] = [c for c in _instances(text) if c.target_key == "server"]
    assert claim.meta["refusal"] == FACTORY_UNPROVEN


def test_a_function_that_can_fall_off_its_end_is_no_factory() -> None:
    source = "function make(c) { const app = Fastify(); if (c) return app; }"
    text = IMPORT + source + "\nconst server = make();\n"
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
    text = IMPORT + source + "\nconst server = make();\n"
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
    claim = _only(IMPORT + MAKE + "const server = make();\n")
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
        "const server = await make();\n"
    )
    claim = _only(source)
    assert claim.meta["status"] == PROVEN
    assert claim.meta["awaited"] == "true"
    assert "refusal" not in claim.meta
    wrapped = source.replace("await make();", "(await make());")
    assert _only(wrapped).meta["status"] == PROVEN


def test_a_sync_factory_may_be_awaited_too() -> None:
    claim = _only(IMPORT + MAKE + "async function run() {}\nconst server = await make();\n")
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
    claim = _only(IMPORT + imports + f"\nconst app = {call};\n")
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
    source = IMPORT + MAKE + "function start() {\n  const server = make();\n}\n"
    claim = _only(source)
    assert claim.target_key == "start>server"
    assert claim.meta["status"] == REFUSED
    assert claim.meta["refusal"] == ENTRY_NOT_CALLED


def test_the_instance_claims_of_a_file_are_in_source_order_and_bounded() -> None:
    names = [f"s{i}" for i in range(70)]
    source = IMPORT + MAKE + "".join(f"const {name} = make();\n" for name in names)
    claims = _instances(source)
    assert [c.target_key for c in claims] == names[:64]
    assert [c.line_start for c in claims] == list(range(3, 67))
    assert [c.target_key for c in _instances(source)] == names[:64]


def test_only_a_file_that_imports_fastify_states_instances() -> None:
    source = MAKE + "const server = make();\n"
    assert _instances(source) == []
    assert [c.target_key for c in _instances(IMPORT + source)] == ["server"]


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


def test_an_entry_function_is_proven_by_any_one_unconditional_call() -> None:
    claim = _only(IMPORT + START + "if (x) { start(); }\nstart();\n")
    assert claim.meta["status"] == PROVEN


def test_an_entry_function_held_by_a_const_is_called_after_its_binding() -> None:
    held = "const start = async () => {\n  const app = Fastify();\n};\n"
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


# -- receiver aliases --------------------------------------------------------------------


def test_an_alias_of_an_instance_is_the_same_receiver() -> None:
    source = IMPORT + MAKE + "const a = make();\nconst b = a;\nconst c = b as Fastify;\n"
    assert _aliases(_only(source)) == [["b", 1], ["c", 2]]


def test_aliases_are_followed_for_four_hops_and_no_further() -> None:
    chain = "".join(f"const {new} = {old};\n" for old, new in zip("abcde", "bcdef", strict=True))
    claim = _only(IMPORT + MAKE + "const a = make();\n" + chain)
    assert _aliases(claim) == [["b", 1], ["c", 2], ["d", 3], ["e", 4]]
    assert claim.meta["status"] == PROVEN


def test_a_name_may_alias_the_same_receiver_twice() -> None:
    source = IMPORT + MAKE + "const a = make();\nconst b = a;\nconst c = a;\nconst d = b;\n"
    assert _aliases(_only(source)) == [["b", 1], ["c", 1], ["d", 2]]


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
        # bound twice
        "const b = a;\nfunction f(b) { return b; }\n",
        # not a plain name
        "const b = a.child;\nconst c = [a];\nconst d = wrap(a);\nconst e = a || other;\n",
        # a `let` is no alias: it can change
        "let b = a;\n",
        # a name used before its own binding
        "use(b);\nconst b = a;\n",
    ],
)
def test_a_name_that_is_not_a_const_alias_in_the_same_scope_is_not_an_alias(extra: str) -> None:
    claim = _only(IMPORT + MAKE + "const a = make();\n" + extra)
    assert _aliases(claim) == []


def test_an_alias_declared_before_what_it_names_is_a_use_before_the_binding() -> None:
    claim = _only(IMPORT + MAKE + "const b = a;\nconst a = make();\n")
    assert claim.target_key == "a"
    assert claim.meta["status"] == REFUSED
    assert claim.meta["refusal"] == RECEIVER_BEFORE_BINDING
    assert claim.meta["aliases"] == "[]"


# -- order -------------------------------------------------------------------------------


def test_a_receiver_used_before_its_const_is_refused() -> None:
    claim = _only(IMPORT + MAKE + "init(server);\nconst server = make();\n")
    assert claim.meta["status"] == REFUSED
    assert claim.meta["refusal"] == RECEIVER_BEFORE_BINDING
    assert claim.meta["aliases"] == "[]"


def test_a_receiver_used_before_its_const_in_an_entry_function_is_refused() -> None:
    source = (
        IMPORT + MAKE + "function start() {\n  init(server);\n  const server = make();\n}\n"
        "start();\n"
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
    assert _instances(source)[-1].meta["status"] == PROVEN


def test_a_const_factory_called_before_its_binding_is_refused() -> None:
    source = (
        IMPORT + "const server = make();\n"
        "const make = () => { const app = Fastify(); return app; };\n"
    )
    claim = _only(source)
    assert claim.meta["status"] == REFUSED
    assert claim.meta["refusal"] == CALLEE_BEFORE_BINDING


def test_a_const_factory_called_after_its_binding_is_accepted() -> None:
    source = (
        IMPORT + "const make = () => { const app = Fastify(); return app; };\n"
        "const server = make();\n"
    )
    assert _only(source).meta["status"] == PROVEN


def test_a_function_declaration_factory_called_before_its_position_is_accepted() -> None:
    claim = _only(IMPORT + "const server = make();\n" + MAKE)
    assert claim.meta["status"] == PROVEN
    assert "refusal" not in claim.meta


# -- other refusals ----------------------------------------------------------------------


def test_an_async_factory_called_without_await_is_refused() -> None:
    source = (
        IMPORT + "async function make() { const app = Fastify(); return app; }\n"
        "const server = make();\n"
    )
    claim = _only(source)
    assert claim.meta["status"] == REFUSED
    assert claim.meta["refusal"] == FACTORY_NOT_AWAITED
    assert claim.meta["awaited"] == "false"


def test_a_local_function_that_is_no_factory_is_refused_as_unproven() -> None:
    source = IMPORT + "function createCache() { return new Map(); }\nconst cache = createCache();\n"
    claim = _only(source)
    assert claim.meta["status"] == REFUSED
    assert claim.meta["refusal"] == FACTORY_UNPROVEN
    assert claim.meta["local"] == "createCache()"


def test_the_first_reason_to_refuse_is_the_one_recorded() -> None:
    # unproven before not awaited before the receiver used early
    source = (
        IMPORT + "async function make() { return Fastify().x; }\n"
        "init(server);\nconst server = make();\n"
    )
    assert _only(source).meta["refusal"] == FACTORY_UNPROVEN
    source = (
        IMPORT + "async function make() { const app = Fastify(); return app; }\n"
        "init(server);\nconst server = make();\n"
    )
    assert _only(source).meta["refusal"] == FACTORY_NOT_AWAITED
