"""The global pass serves Fastify routes through factories, instances and helper calls (M8, C3).

The parser records, as claims, which functions build an application (factories), which
``const x = F()`` is an instance of one, which call hands a receiver to a function (a helper
call) and which route sits on such a receiver (``test_http_fastify_tracing``). This pass reads
them across modules and serves a route only when every step is proven: the instance's factory,
the helper's target, and a chain of registrations and helper calls back to an application, a
proven instance or a proven entry. An instance is an application rooted at the file that holds
it. Every refusal says why, by a stable ``fastify-...`` code.

The fixtures are the ones frozen in ``M8_PREDICTIONS.md`` (with its Amendment 1): a repository
``trace``, the user's ``example``, ``multi``, ``pos``, the ``direct``/``traced`` pair and ``neg``.
"""

from __future__ import annotations

import json
import textwrap
from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest
from repoatlas.capabilities.service import CapabilityService
from repoatlas.graph.deploy_identity import COMMAND_UNREAD
from repoatlas.graph.http_claims import HttpClaimPlan
from repoatlas.graph.linker import ReferenceLinker
from repoatlas.graph.resolution import ResolutionIndex
from repoatlas.graph.ts_routes import (
    CHAIN_TOO_COMPLEX,
    CHAIN_TOO_DEEP,
    FACTORY_UNRESOLVED,
    HELPER_ACCEPTED,
    HELPER_ARGUMENT_POSITION,
    HELPER_NOT_A_PLUGIN,
    HELPER_UNRESOLVED,
    NOT_REGISTERED,
    PROMOTED,
    UNREADABLE_REGISTRATION,
    FastifyRoutePlan,
    plan_fastify_routes,
)
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.http_inventory import HttpOperationsRequest
from repoatlas.models.impact import ImpactRequest, ImpactResponse
from repoatlas.parsers.http_facts import (
    FASTIFY_HELPER_CALL,
    FASTIFY_INSTANCE,
    FASTIFY_ROUTE,
    FASTIFY_SCOPE,
    FASTIFY_TRACED_ROUTE,
    PENDING_FACT,
)
from repoatlas.parsers.http_fastify import (
    AFTER_LISTEN,
    ENTRY_NOT_CALLED,
    FACTORY_NOT_AWAITED,
    FACTORY_UNPROVEN,
    HELPER_ARGUMENT_UNSUPPORTED,
    HELPER_CONTROL_FLOW,
    HELPER_DYNAMIC,
    HELPER_SHADOWED,
    INSTANCE,
    PLUGIN,
    PROVEN,
    RECEIVER_BEFORE_BINDING,
)
from repoatlas.services.workspace import Workspace
from repoatlas.storage.protocols import FileRow, UnresolvedRefRecord
from tests.impact_corpus import build
from tests.incremental_corpus import assert_equals_fresh, edit, sync_all
from tests.test_deployment_link_join import decided, handler_of, link


def d(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def line(text: str, needle: str) -> int:
    return next(number for number, row in enumerate(text.splitlines(), 1) if needle in row)


Edge = tuple[str, str, str, int | None]

# -- repository `trace` ---------------------------------------------------------------------

TRACE_SERVER = d("""
    import Fastify from 'fastify';

    export function createServer() {
      const app = Fastify();
      app.get('/health', health);
      return app;
    }

    export async function createAsyncServer() {
      const app = Fastify();
      return app;
    }
""")

HANDLERS = d("""
    import type { FastifyInstance } from 'fastify';
    import { initializeUserRoutes } from './routes/users';
    import { orderPlugin } from './routes/orders';

    export function initializeHandlers(server: FastifyInstance) {
      initializeUserRoutes(server);
      initializeLocal(server);
      server.register(orderPlugin, { prefix: '/orders' });
    }

    function initializeLocal(app: FastifyInstance) {
      app.get('/local', getLocal);
    }
""")

USERS = d("""
    import type { FastifyInstance } from 'fastify';

    export function initializeUserRoutes(app: FastifyInstance) {
      app.get('/users', getUsers);
      app.post('/users', createUser);
    }
""")

ORDERS = d("""
    import type { FastifyInstance } from 'fastify';

    export async function orderPlugin(server: FastifyInstance) {
      server.get('/', list);
      registerItems(server);
    }

    function registerItems(s: FastifyInstance) {
      s.get('/:id', item);
    }
""")

TRACE_MAIN = d("""
    import { createServer } from './server';
    import { initializeHandlers } from './handlers';

    const server = createServer();
    initializeHandlers(server);
    server.listen({ port: 8080 });
""")

PACKAGE = '{"name": "fixture"}\n'

TRACE = {
    "package.json": PACKAGE,
    "src/server.ts": TRACE_SERVER,
    "src/handlers.ts": HANDLERS,
    "src/routes/users.ts": USERS,
    "src/routes/orders.ts": ORDERS,
    "src/main.ts": TRACE_MAIN,
}

TRACE_EDGES: set[Edge] = {
    ("src/routes/users.ts", "initializeUserRoutes", "GET /users", line(USERS, "app.get(")),
    ("src/routes/users.ts", "initializeUserRoutes", "POST /users", line(USERS, "app.post(")),
    ("src/handlers.ts", "initializeLocal", "GET /local", line(HANDLERS, "app.get('/local'")),
    ("src/server.ts", "createServer", "GET /health", line(TRACE_SERVER, "app.get('/health'")),
    ("src/routes/orders.ts", "orderPlugin", "GET /orders", None),
    ("src/routes/orders.ts", "registerItems", "GET /orders/:id", None),
}

# -- a factory with no route of its own ----------------------------------------------------------

PLAIN_SERVER = d("""
    import Fastify from 'fastify';

    export function createServer() {
      const app = Fastify();
      return app;
    }
""")

# -- repository `example`: the user's example ------------------------------------------------------

# The user's example verbatim, with only the import of `createServer` added. The file has no
# fastify import and `server` is handed to the local initializeHandlers, which is called before
# its declaration (hoisted).
EXAMPLE_APP = d("""
    import { createServer } from './server';
    const server = createServer();
    initializeHandlers(server);
    function initializeHandlers(server) { initializeUserRoutes(server); }
    function initializeUserRoutes(app) { app.get("/users", getUsers); }
""")

EXAMPLE = {
    "package.json": PACKAGE,
    "src/server.ts": PLAIN_SERVER,
    "src/app.ts": EXAMPLE_APP,
}

# -- repository `multi` ---------------------------------------------------------------

MULTI_ROUTES = d("""
    import type { FastifyInstance } from 'fastify';

    export function initAdmin(s: FastifyInstance) {
      s.delete('/cache', h);
    }

    export function initPublic(s: FastifyInstance) {
      s.get('/items', h);
    }
""")

MULTI = {
    "package.json": PACKAGE,
    "src/server.ts": PLAIN_SERVER,
    "src/routes.ts": MULTI_ROUTES,
    "src/admin.ts": d("""
        import { createServer } from './server';
        import { initAdmin } from './routes';

        const admin = createServer();
        initAdmin(admin);
        admin.listen({ port: 1 });
    """),
    "src/public.ts": d("""
        import { createServer } from './server';
        import { initPublic } from './routes';

        const pub = createServer();
        initPublic(pub);
        pub.listen({ port: 2 });
    """),
}

# -- repository `pos` -----------------------------------------------------------------

POS_ENTRY = d("""
    import Fastify from 'fastify';

    function initEntry(a) {
      a.get('/entry', h);
    }

    async function start() {
      const app = Fastify();
      initEntry(app);
      await app.listen();
    }

    start();
""")

POS_CYCLE = d("""
    import Fastify from 'fastify';

    function cycA(s) {
      s.register(cycPlugin, { prefix: '/p' });
    }

    function cycPlugin(s) {
      cycA(s);
      s.get('/c', h);
    }

    const server = Fastify();
    cycA(server);
""")

POS_ASYNC = d("""
    import { createAsyncServer } from './server';

    function initAsync(app) {
      app.get('/async', h);
    }

    const sa = await createAsyncServer();
    initAsync(sa);
""")

POS_DEFAULT = d("""
    import makeD from './factory_default';

    function initD(app) {
      app.get('/default', h);
    }

    const sd = makeD();
    initD(sd);
""")

POS_SERVER = PLAIN_SERVER + d("""

    export async function createAsyncServer() {
      const app = Fastify();
      return app;
    }
""")

POS = {
    "package.json": PACKAGE,
    "src/server.ts": POS_SERVER,
    "src/factory_default.ts": d("""
        import Fastify from 'fastify';

        export default function makeD() {
          const app = Fastify();
          return app;
        }
    """),
    "src/entry.ts": POS_ENTRY,
    "src/cyc.ts": POS_CYCLE,
    "src/async.ts": POS_ASYNC,
    "src/dflt.ts": POS_DEFAULT,
}

POS_EDGES: set[Edge] = {
    ("src/entry.ts", "initEntry", "GET /entry", line(POS_ENTRY, "a.get(")),
    ("src/cyc.ts", "cycPlugin", "GET /p/c", None),
    ("src/async.ts", "initAsync", "GET /async", line(POS_ASYNC, "app.get(")),
    ("src/dflt.ts", "initD", "GET /default", line(POS_DEFAULT, "app.get(")),
}

# -- the equivalence pair -------------------------------------------------------------

PAIR_USERS = d("""
    import type { FastifyInstance } from 'fastify';

    export async function initializeUserRoutes(app: FastifyInstance) {
      app.get('/users', getUsers);
    }
""")

DIRECT = {
    "package.json": PACKAGE,
    "src/routes/users.ts": PAIR_USERS,
    "src/main.ts": d("""
        import Fastify from 'fastify';
        import { initializeUserRoutes } from './routes/users';

        const server = Fastify();
        server.register(initializeUserRoutes);
        server.listen({ port: 8080 });
    """),
}

PAIRED = {
    "package.json": PACKAGE,
    "src/routes/users.ts": PAIR_USERS,
    "src/server.ts": PLAIN_SERVER,
    "src/main.ts": d("""
        import { createServer } from './server';
        import { initializeUserRoutes } from './routes/users';

        const server = createServer();
        initializeUserRoutes(server);
        server.listen({ port: 8080 });
    """),
}

# -- repository `neg` -----------------------------------------------------------------

NEG_SERVER = d("""
    import Fastify from 'fastify';

    export function createServer() {
      const app = Fastify();
      return app;
    }

    export async function createAsyncServer() {
      const app = Fastify();
      return app;
    }

    export function createBox() {
      const app = Fastify();
      return { app };
    }

    export function createCache() {
      return new Map();
    }

    export function createClient() {
      return {};
    }
""")

TYPE = "import type { FastifyInstance } from 'fastify';\n"
SERVER = "import { createServer } from './server';\n"


def _case(imports: str, *statements: str) -> str:
    return imports + "\n" + "\n".join(statements) + "\n"


CHAIN13 = TYPE + "".join(
    f"\nexport function h{n}(s: FastifyInstance) {{\n  s.get('/d{n}', r);\n"
    + (f"  h{n + 1}(s);\n" if n < 6 else "")
    + "}\n"
    for n in range(1, 7)
)

NEG = {
    "package.json": PACKAGE,
    "src/server.ts": NEG_SERVER,
    # N1: a helper handed createCache(), which returns new Map()
    "src/n01.ts": _case(
        TYPE + "import { createCache } from './server';\n",
        "function initN1(app: FastifyInstance) {\n  app.get('/n1', h1);\n}",
        "const c1 = createCache();\ninitN1(c1);",
    ),
    # N2: a factory that returns { app }
    "src/n02.ts": _case(
        TYPE + "import { createBox } from './server';\n",
        "function initN2(app: FastifyInstance) {\n  app.get('/n2', h2);\n}",
        "const s2 = createBox();\ninitN2(s2);",
    ),
    # N3: a helper call under a condition
    "src/n03.ts": _case(
        TYPE + SERVER,
        "function initN3(app: FastifyInstance) {\n  app.get('/n3', h3);\n}",
        "const s3 = createServer();\nif (process.env.FLAG) initN3(s3);",
    ),
    # N4: a callee no name gives
    "src/n04.ts": _case(
        TYPE + SERVER,
        "function initN4(app: FastifyInstance) {\n  app.get('/n4', h4);\n}",
        "const helpers = [initN4];\nconst s4 = createServer();\nhelpers[0](s4);",
        "s4.listen({ port: 80 });",
    ),
    # N5: the receiver is the second argument
    "src/n05.ts": _case(
        TYPE + SERVER,
        "function initN5(app: FastifyInstance, config: object) {\n  app.get('/n5', h5);\n}",
        "const config5 = {};\nconst s5 = createServer();\ninitN5(config5, s5);",
    ),
    # N6: bound twice, so no instance
    "src/n06.ts": _case(
        TYPE + SERVER,
        "function initN6(app: FastifyInstance) {\n  app.get('/n6', h6);\n}",
        "let s6 = createServer();\ns6 = createServer();\ninitN6(s6);",
    ),
    # N7: the callee is hidden by a local function of the same name
    "src/n07.ts": _case(
        "import Fastify from 'fastify';\n",
        "function initN7(a) {\n  a.get('/n7', h7);\n}",
        "async function start7() {\n  const app7 = Fastify();\n  const initN7 = () => {};\n"
        "  initN7(app7);\n  app7.listen();\n}",
        "start7();",
    ),
    # N8: handed on before it is bound
    "src/n08.ts": _case(
        TYPE + SERVER,
        "function initN8(app: FastifyInstance) {\n  app.get('/n8', h8);\n}",
        "initN8(s8);\nconst s8 = createServer();",
    ),
    # N9: a module that does not exist
    "src/n09.ts": _case(
        TYPE + SERVER + "import { initN9 } from './missing';\n",
        "const s9 = createServer();\ninitN9(s9);",
    ),
    # N10: through a barrel re-export, with a decoy module that exports the same name
    "src/n10.ts": _case(
        SERVER + "import { initN10 } from './barrel10';\n",
        "const s10 = createServer();\ninitN10(s10);",
    ),
    "src/barrel10.ts": "export { initN10 } from './real10';\n",
    "src/real10.ts": TYPE
    + "\nexport function initN10(app: FastifyInstance) {\n  app.get('/n10', h10);\n}\n",
    "src/decoy10.ts": TYPE
    + "\nexport function initN10(app: FastifyInstance) {\n  app.get('/decoy10', h10);\n}\n",
    # N11: a factory chosen by a computed name
    "src/n11.ts": _case(
        TYPE + SERVER,
        "function initN11(app: FastifyInstance) {\n  app.get('/n11', h11);\n}",
        "const factories = { a: createServer };\nconst kind = 'a';\n"
        "const s11 = factories[kind]();\ninitN11(s11);",
    ),
    # N12: an async factory that is not awaited
    "src/n12.ts": _case(
        TYPE + "import { createAsyncServer } from './server';\n",
        "function initN12(app: FastifyInstance) {\n  app.get('/n12', h12);\n}",
        "const s12 = createAsyncServer();\ninitN12(s12);",
    ),
    # N13: a chain of helpers six deep
    "src/chain13.ts": CHAIN13,
    "src/main13.ts": _case(
        SERVER + "import { h1 } from './chain13';\n", "const s13 = createServer();\nh1(s13);"
    ),
    # N15: handed on after the server started
    "src/n15.ts": _case(
        TYPE + SERVER,
        "function initN15(app: FastifyInstance) {\n  app.get('/n15', h15);\n}",
        "const s15 = createServer();\nawait s15.listen();\ninitN15(s15);",
    ),
    # N16: handed on in a callback
    "src/n16.ts": _case(
        TYPE + SERVER,
        "function initN16(app: FastifyInstance) {\n  app.get('/n16', h16);\n}",
        "const s16 = createServer();\ns16.after(() => initN16(s16));",
    ),
    # N17: the receiver is inside an object
    "src/n17.ts": _case(
        TYPE + SERVER,
        "function initN17(app: FastifyInstance) {\n  app.get('/n17', h17);\n}",
        "const s17 = createServer();\ninitN17({ server: s17 });",
    ),
    # N18: a factory nothing calls, with a route in its body
    "src/n18.ts": _case(
        "import Fastify from 'fastify';\n",
        "export function makeN18() {\n  const app = Fastify();\n  app.get('/n18', h18);\n"
        "  return app;\n}",
    ),
    # N19: an entry function nothing calls
    "src/n19.ts": _case(
        "import Fastify from 'fastify';\n",
        "function initN19(app) {\n  app.get('/n19', h19);\n}",
        "async function start19() {\n  const app19 = Fastify();\n  initN19(app19);\n"
        "  await app19.listen();\n}",
    ),
    # N20: a helper that comes from a package: nothing is recorded
    "src/n20.ts": _case(
        SERVER + "import { initN20 } from 'some-package';\n",
        "const s20 = createServer();\ninitN20(s20);\ns20.listen({ port: 80 });",
    ),
    # N21: a file with no Fastify, whose client factory is no factory
    "src/n21.ts": _case(
        "import { createClient } from './server';\n",
        "function setup(c) {\n  c.get('/n21', opts);\n}",
        "const c = createClient();\nsetup(c);",
    ),
    # a factory imported from a module that does not exist
    "src/x_unresolved.ts": _case(
        "import { makeX } from './nowhere';\n", "const sx = makeX();\nsx.listen({ port: 80 });"
    ),
    # a helper call that names a factory
    "src/x_notplugin.ts": _case(SERVER, "const sy = createServer();\ncreateServer(sy);"),
}

#: path → (the codes of its helper calls, the outcomes of its instances), both sorted.
NEG_CODES: dict[str, tuple[list[str], list[str]]] = {
    "src/n01.ts": ([], [FACTORY_UNPROVEN]),
    "src/n02.ts": ([], [FACTORY_UNPROVEN]),
    "src/n03.ts": ([HELPER_CONTROL_FLOW], [PROVEN]),
    "src/n04.ts": ([HELPER_DYNAMIC], [PROVEN]),
    "src/n05.ts": ([HELPER_ARGUMENT_POSITION], [PROVEN]),
    "src/n06.ts": ([], []),
    "src/n07.ts": ([HELPER_SHADOWED], [PROVEN]),
    "src/n08.ts": ([RECEIVER_BEFORE_BINDING], [RECEIVER_BEFORE_BINDING]),
    "src/n09.ts": ([HELPER_UNRESOLVED], [PROVEN]),
    "src/n10.ts": ([HELPER_UNRESOLVED], [PROVEN]),
    "src/n11.ts": ([], []),
    "src/n12.ts": ([], [FACTORY_NOT_AWAITED]),
    "src/main13.ts": ([HELPER_ACCEPTED], [PROVEN]),
    "src/chain13.ts": ([HELPER_ACCEPTED] * 5, []),
    "src/n15.ts": ([AFTER_LISTEN], [PROVEN]),
    "src/n16.ts": ([HELPER_CONTROL_FLOW], [PROVEN]),
    "src/n17.ts": ([HELPER_ARGUMENT_UNSUPPORTED], [PROVEN]),
    "src/n18.ts": ([], []),
    "src/n19.ts": ([], [ENTRY_NOT_CALLED]),
    "src/n20.ts": ([], [PROVEN]),
    "src/n21.ts": ([], [FACTORY_UNPROVEN]),
    "src/x_unresolved.ts": ([], [FACTORY_UNRESOLVED]),
    "src/x_notplugin.ts": ([HELPER_NOT_A_PLUGIN], [PROVEN]),
}

#: Of the whole repository only N13's first four helpers are served.
NEG_EDGES: set[Edge] = {
    ("src/chain13.ts", f"h{n}", f"GET /d{n}", line(CHAIN13, f"s.get('/d{n}'")) for n in range(1, 5)
}

# -- an exported factory registered as a plugin ---------------------------------------

PLUGIN_FACTORY = d("""
    import Fastify from 'fastify';

    export function makeApp(server) {
      const app = Fastify();
      server.get('/via-plugin', h);
      return app;
    }
""")

FP = {
    "package.json": PACKAGE,
    "src/factory.ts": PLUGIN_FACTORY,
    "src/main.ts": d("""
        import Fastify from 'fastify';
        import { makeApp } from './factory';

        const server = Fastify();
        server.register(makeApp, { prefix: '/f' });
    """),
}

# -- owners that nothing proves -------------------------------------------------------

OWNERS_APP = d("""
    import Fastify from 'fastify';
    import { createCache, createServer } from './server';

    const PREFIXVAR = process.env.P;

    function plug(s) {
      s.get('/plug', h);
    }

    function plug2(s) {
      s.get('/plug2', h);
    }

    const server = Fastify();
    server.register(plug);
    server.register(plug2);

    const bad = createCache();
    bad.register(plug, { prefix: PREFIXVAR });

    const good = createServer();
    good.register(plug2, { prefix: PREFIXVAR });
""")

OWNERS = {"package.json": PACKAGE, "src/server.ts": NEG_SERVER, "src/owners.ts": OWNERS_APP}

# -- a registration cycle -------------------------------------------------------------

CYCLE_APP = d("""
    import Fastify from 'fastify';

    function regA(s) {
      s.register(regB, { prefix: '/b' });
      s.get('/a', h);
    }

    function regB(s) {
      s.register(regA, { prefix: '/a' });
      s.get('/b', h);
    }

    const server = Fastify();
    server.register(regA);
""")

CYCLE = {"package.json": PACKAGE, "src/cycle.ts": CYCLE_APP}


def _fan_distinct(owners: int) -> dict[str, str]:
    """One plugin registered, under a prefix of its own, by each of ``owners`` plugins."""
    rows = ["import Fastify from 'fastify';", "function leaf(s) { s.get('/leaf', h); }"]
    for i in range(owners):
        rows.append(f"function o{i}(s) {{ s.register(leaf, {{ prefix: '/p{i}' }}); }}")
    rows += ["const server = Fastify();"] + [f"server.register(o{i});" for i in range(owners)]
    return {"package.json": PACKAGE, "src/fan.ts": "\n".join(rows) + "\n"}


def _fan_visits(upper: int, lower: int) -> dict[str, str]:
    """One plugin registered, under one prefix, by ``upper`` plugins, each of which every one of
    ``lower`` plugins registers: ``2 * upper * lower`` scopes to walk, one distinct result."""
    rows = ["import Fastify from 'fastify';", "function leaf(s) { s.get('/leaf', h); }"]
    for i in range(upper):
        rows.append(f"function a{i}(s) {{ s.register(leaf, {{ prefix: '/x' }}); }}")
    for j in range(lower):
        body = " ".join(f"s.register(a{i});" for i in range(upper))
        rows.append(f"function b{j}(s) {{ {body} }}")
    rows += ["const server = Fastify();"] + [f"server.register(b{j});" for j in range(lower)]
    return {"package.json": PACKAGE, "src/fan.ts": "\n".join(rows) + "\n"}


# -- reading the result ---------------------------------------------------------------


def _edges(workspace: Workspace, slug: str) -> set[Edge]:
    """(file, handler, route body, first line or None) of every route served by code."""
    rows = workspace.database.execute(
        """
        SELECT f.rel_path, s.qualified_name, d.name, e.line_start
        FROM edges e
        JOIN entities s ON s.id = e.src_id
        JOIN entities d ON d.id = e.dst_id
        JOIN files f ON f.id = s.file_id
        JOIN repositories r ON r.id = s.repo_id
        WHERE e.type = 'SERVES_HTTP_ROUTE' AND r.slug = ? AND f.rel_path LIKE '%.ts'
        """,
        (slug,),
    ).fetchall()
    return {(row[0], row[1], row[2], row[3]) for row in rows}


def _http(workspace: Workspace) -> HttpClaimPlan:
    return ReferenceLinker(workspace.knowledge).prepare_global().http


def _roots(workspace: Workspace, slug: str) -> dict[tuple[str, str], set[str]]:
    """(file, route body) → the files of the applications that serve it."""
    files = {
        row[0]: (row[1], row[2])
        for row in workspace.database.execute(
            "SELECT f.id, f.rel_path, r.slug FROM files f JOIN repositories r ON r.id = f.repo_id"
        )
    }
    found: dict[tuple[str, str], set[str]] = defaultdict(set)
    for route in _http(workspace).code_routes:
        path, owner = files[route.file_id]
        if owner == slug and path.endswith(".ts"):
            found[(path, route.body)] |= {files[root][0] for root in route.roots}
    return dict(found)


Grouped = dict[str, list[UnresolvedRefRecord]]


def _plan(workspace: Workspace) -> tuple[FastifyRoutePlan, Grouped, dict[int, FileRow]]:
    """The plan of the Fastify stage alone, and the claims and files it was made from."""
    knowledge = workspace.knowledge
    files = knowledge.list_file_rows()
    grouped: Grouped = defaultdict(list)
    for claim in knowledge.list_unresolved_refs(exclude_uses=True):
        grouped[json.loads(claim.meta_json or "{}").get(PENDING_FACT, "")].append(claim)
    by_id = {row.file_id: row for row in files}
    index = ResolutionIndex.build(files, knowledge.list_symbols())
    return plan_fastify_routes(grouped, index, by_id), grouped, by_id


def _by_path(
    table: dict[int, str], claims: list[UnresolvedRefRecord], by_id: dict[int, FileRow]
) -> dict[str, list[str]]:
    found: dict[str, list[str]] = defaultdict(list)
    for claim in claims:
        if claim.id in table:
            found[by_id[claim.file_id].rel_path].append(table[claim.id])
    return {path: sorted(codes) for path, codes in found.items()}


def _route_keys(
    table: dict[int, str], grouped: Grouped, by_id: dict[int, FileRow]
) -> dict[tuple[str, str], str]:
    """(file, claimed route) → what ``table`` says of each route claim, either role."""
    claims = [*grouped[FASTIFY_ROUTE], *grouped[FASTIFY_TRACED_ROUTE]]
    return {
        (by_id[claim.file_id].rel_path, claim.target_key): table[claim.id]
        for claim in claims
        if claim.id in table
    }


def _count(grouped: Grouped, by_id: dict[int, FileRow], role: str) -> dict[str, int]:
    found: dict[str, int] = defaultdict(int)
    for claim in grouped[role]:
        found[by_id[claim.file_id].rel_path] += 1
    return dict(found)


def _providers(workspace: Workspace, slug: str) -> set[tuple[str | None, str | None]]:
    """(method, file) of every provider row the inventory lists for the repository."""
    request = HttpOperationsRequest(
        context=CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=slug), limit=200
    )
    response = CapabilityService(workspace).http_operations(request)
    return {(row.method, row.path) for row in response.operations if row.role == "provider"}


@pytest.fixture
def trace(workspace: Workspace, tmp_path: Path) -> Workspace:
    return build(workspace, tmp_path, {"trace": TRACE})


@pytest.fixture
def neg(workspace: Workspace, tmp_path: Path) -> Workspace:
    return build(workspace, tmp_path, {"neg": NEG})


# -- repository trace -----------------------------------------------------------------


def test_trace_serves_exactly_six_edges_rooted_at_the_file_that_builds_the_server(
    trace: Workspace,
) -> None:
    assert _edges(trace, "trace") == TRACE_EDGES
    assert len(_edges(trace, "trace")) == 6
    # D2: every route is rooted at the instance's file, never at the factory's, a helper's or a
    # handler's file
    assert _roots(trace, "trace") == {
        (path, route): {"src/main.ts"} for path, _handler, route, _line in TRACE_EDGES
    }


def test_trace_has_the_claims_the_prediction_counts(trace: Workspace) -> None:
    _, grouped, by_id = _plan(trace)
    assert _count(grouped, by_id, FASTIFY_INSTANCE) == {"src/main.ts": 1}
    assert _count(grouped, by_id, FASTIFY_HELPER_CALL) == {
        "src/main.ts": 1,
        "src/handlers.ts": 2,
        "src/routes/orders.ts": 1,
    }
    factories = [
        by_id[claim.file_id].rel_path
        for claim in grouped[FASTIFY_SCOPE]
        if json.loads(claim.meta_json)["kind"] == "factory"
    ]
    assert factories == ["src/server.ts", "src/server.ts"]


def test_trace_lists_a_promoted_traced_route_as_a_provider(trace: Workspace) -> None:
    assert _providers(trace, "trace") == {
        ("GET", "src/routes/users.ts"),
        ("POST", "src/routes/users.ts"),
        ("GET", "src/handlers.ts"),
        ("GET", "src/server.ts"),
        ("GET", "src/routes/orders.ts"),
    }


def test_trace_resolves_the_one_instance_and_every_helper_call(trace: Workspace) -> None:
    plan, grouped, by_id = _plan(trace)
    assert _by_path(plan.instance_outcomes, grouped[FASTIFY_INSTANCE], by_id) == {
        "src/main.ts": [PROVEN]
    }
    assert _by_path(plan.helper_outcomes, grouped[FASTIFY_HELPER_CALL], by_id) == {
        "src/main.ts": [HELPER_ACCEPTED],
        "src/handlers.ts": [HELPER_ACCEPTED] * 2,
        "src/routes/orders.ts": [HELPER_ACCEPTED],
    }
    assert plan.chain_refusals == {}


# -- repository example ---------------------------------------------------------------


def test_example_serves_the_one_route_of_a_file_with_no_fastify_import(
    workspace: Workspace, tmp_path: Path
) -> None:
    indexed = build(workspace, tmp_path, {"example": EXAMPLE})
    assert _edges(indexed, "example") == {
        ("src/app.ts", "initializeUserRoutes", "GET /users", line(EXAMPLE_APP, "app.get("))
    }
    assert _roots(indexed, "example") == {("src/app.ts", "GET /users"): {"src/app.ts"}}
    plan, grouped, by_id = _plan(indexed)
    assert grouped[FASTIFY_ROUTE] == []
    assert [c.target_key for c in grouped[FASTIFY_TRACED_ROUTE]] == ["GET /users"]
    # the instance hands the server to initializeHandlers, which is a plugin scope that hands it
    # on to initializeUserRoutes: two helper calls, both accepted
    calls = sorted(
        (json.loads(c.meta_json)["owner"], json.loads(c.meta_json)["owner_kind"], c.target_key)
        for c in grouped[FASTIFY_HELPER_CALL]
    )
    assert calls == [
        ("initializeHandlers", PLUGIN, "initializeUserRoutes"),
        ("server", INSTANCE, "initializeHandlers"),
    ]
    assert _by_path(plan.helper_outcomes, grouped[FASTIFY_HELPER_CALL], by_id) == {
        "src/app.ts": [HELPER_ACCEPTED] * 2
    }


# -- repository multi -----------------------------------------------------------------


def test_two_instances_of_one_factory_root_each_route_at_its_own_file(
    workspace: Workspace, tmp_path: Path
) -> None:
    indexed = build(workspace, tmp_path, {"multi": MULTI})
    assert _edges(indexed, "multi") == {
        ("src/routes.ts", "initAdmin", "DELETE /cache", line(MULTI_ROUTES, "s.delete(")),
        ("src/routes.ts", "initPublic", "GET /items", line(MULTI_ROUTES, "s.get(")),
    }
    assert _roots(indexed, "multi") == {
        ("src/routes.ts", "DELETE /cache"): {"src/admin.ts"},
        ("src/routes.ts", "GET /items"): {"src/public.ts"},
    }


# -- repository pos -------------------------------------------------------------------


def test_an_entry_a_cycle_an_awaited_factory_and_a_default_import_are_served(
    workspace: Workspace, tmp_path: Path
) -> None:
    indexed = build(workspace, tmp_path, {"pos": POS})
    assert _edges(indexed, "pos") == POS_EDGES
    # each route is rooted at the file that holds its application, instance or entry
    assert _roots(indexed, "pos") == {
        (path, route): {path} for path, _handler, route, _line in POS_EDGES
    }


def test_a_plugin_that_reaches_itself_is_not_served_under_its_own_cycle(
    workspace: Workspace, tmp_path: Path
) -> None:
    # P-cycle: cycA registers cycPlugin under /p and cycPlugin calls cycA back: /p/c and nothing
    # at /p/p/c; and a register cycle (regA <-> regB) serves each route once, never around the loop
    indexed = build(workspace, tmp_path, {"pos": POS, "cycle": CYCLE})
    routes = {route for _path, _handler, route, _line in _edges(indexed, "pos")}
    assert {"GET /p/c"} <= routes
    assert not {route for route in routes if route.startswith("GET /p/p")}
    assert _edges(indexed, "cycle") == {
        ("src/cycle.ts", "regA", "GET /a", line(CYCLE_APP, "s.get('/a'")),
        ("src/cycle.ts", "regB", "GET /b/b", None),
    }


# -- the equivalence pair, run -------------------------------------------------------


def test_registering_a_plugin_and_handing_it_the_server_serve_the_same_routes(
    workspace: Workspace, tmp_path: Path
) -> None:
    indexed = build(workspace, tmp_path, {"direct": DIRECT, "traced": PAIRED})
    direct, traced = _edges(indexed, "direct"), _edges(indexed, "traced")
    assert (
        direct
        == traced
        == {
            (
                "src/routes/users.ts",
                "initializeUserRoutes",
                "GET /users",
                line(PAIR_USERS, "app.get("),
            )
        }
    )
    assert _roots(indexed, "direct") == _roots(indexed, "traced")
    assert _roots(indexed, "traced") == {("src/routes/users.ts", "GET /users"): {"src/main.ts"}}


# -- repository neg, run --------------------------------------------------------------


def test_every_negative_case_serves_nothing_but_the_first_four_helpers_of_n13(
    neg: Workspace,
) -> None:
    assert _edges(neg, "neg") == NEG_EDGES


def test_every_negative_case_says_why_by_the_code_the_prediction_names(neg: Workspace) -> None:
    plan, grouped, by_id = _plan(neg)
    helpers = _by_path(plan.helper_outcomes, grouped[FASTIFY_HELPER_CALL], by_id)
    instances = _by_path(plan.instance_outcomes, grouped[FASTIFY_INSTANCE], by_id)
    assert {path: helpers.get(path, []) for path in NEG_CODES} == {
        path: sorted(codes) for path, (codes, _) in NEG_CODES.items()
    }
    assert {path: instances.get(path, []) for path in NEG_CODES} == {
        path: sorted(codes) for path, (_, codes) in NEG_CODES.items()
    }
    # nothing outside the table is claimed at all
    assert set(helpers) | set(instances) <= set(NEG_CODES)


def test_a_helper_chain_is_followed_four_calls_deep_and_no_further(neg: Workspace) -> None:
    # N13: /d1 to /d4 are served; /d5 and /d6 say fastify-chain-too-deep and keep the existing
    # outcome string
    plan, grouped, by_id = _plan(neg)
    outcomes = _route_keys(plan.outcomes, grouped, by_id)
    refusals = _route_keys(plan.chain_refusals, grouped, by_id)
    for n in (1, 2, 3, 4):
        assert outcomes[("src/chain13.ts", f"GET /d{n}")] == PROMOTED
    for n in (5, 6):
        assert outcomes[("src/chain13.ts", f"GET /d{n}")] == NOT_REGISTERED
    assert {key: code for key, code in refusals.items() if key[0] == "src/chain13.ts"} == {
        ("src/chain13.ts", "GET /d5"): CHAIN_TOO_DEEP,
        ("src/chain13.ts", "GET /d6"): CHAIN_TOO_DEEP,
    }
    assert set(refusals.values()) == {CHAIN_TOO_DEEP}


def test_a_traced_route_nothing_proves_is_a_diagnostic_and_no_provider_row(neg: Workspace) -> None:
    # N18 (a factory nothing calls) and N21 (a file with no Fastify at all)
    plan, grouped, by_id = _plan(neg)
    traced = _route_keys(plan.traced_outcomes, grouped, by_id)
    assert traced[("src/n18.ts", "GET /n18")] == NOT_REGISTERED
    assert traced[("src/n21.ts", "GET /n21")] == NOT_REGISTERED
    # not an outcome, not declared, not counted as unresolved
    claims = {c.id for c in grouped[FASTIFY_TRACED_ROUTE] if c.id in plan.traced_outcomes}
    assert claims and not claims & set(plan.outcomes)
    assert not claims & set(plan.declared)
    ordinary = [c for c in grouped[FASTIFY_ROUTE] if c.id in plan.outcomes]
    assert plan.resolved + plan.unresolved == len(ordinary) + len(
        [c for c in grouped[FASTIFY_TRACED_ROUTE] if c.id in plan.promoted]
    )
    listed = {path for _method, path in _providers(neg, "neg")}
    assert not listed & {"src/n18.ts", "src/n21.ts"}


# -- an exported factory is still a plugin --------------------------------------------


def test_an_exported_factory_registered_as_a_plugin_resolves_exactly_as_it_did(
    workspace: Workspace, tmp_path: Path
) -> None:
    # makeApp is both a factory and a plugin of the same exported name: the factory is indexed
    # apart, so the registration still finds one plugin of that name and serves its route
    indexed = build(workspace, tmp_path, {"fp": FP})
    # makeApp is a proven factory: it builds one application, binds it and returns it
    _, grouped, by_id = _plan(indexed)
    factories = [
        by_id[claim.file_id].rel_path
        for claim in grouped[FASTIFY_SCOPE]
        if json.loads(claim.meta_json)["kind"] == "factory"
    ]
    assert factories == ["src/factory.ts"]
    assert _edges(indexed, "fp") == {("src/factory.ts", "makeApp", "GET /f/via-plugin", None)}
    assert _roots(indexed, "fp") == {("src/factory.ts", "GET /f/via-plugin"): {"src/main.ts"}}


# -- the owner of a registration or a call --------------------------------------------


def test_only_a_proven_owner_serves_or_blocks(workspace: Workspace, tmp_path: Path) -> None:
    indexed = build(workspace, tmp_path, {"owners": OWNERS})
    # an unreadable registration on an instance that is no instance blocks nothing; on a proven
    # one it blocks the plugin
    assert _edges(indexed, "owners") == {
        ("src/owners.ts", "plug", "GET /plug", line(OWNERS_APP, "s.get('/plug'"))
    }
    plan, grouped, by_id = _plan(indexed)
    outcomes = _route_keys(plan.outcomes, grouped, by_id)
    assert outcomes[("src/owners.ts", "GET /plug")] == PROMOTED
    assert outcomes[("src/owners.ts", "GET /plug2")] == UNREADABLE_REGISTRATION


# -- bounds ---------------------------------------------------------------------------


def test_a_route_reached_through_more_than_sixty_four_applications_is_still_served(
    workspace: Workspace, tmp_path: Path
) -> None:
    # only the visit bound limits the work: 65 owners are 1 + 65 + 65 scopes, one prefix each
    owners = 65
    indexed = build(workspace, tmp_path, {"fan": _fan_distinct(owners)})
    plan, grouped, by_id = _plan(indexed)
    assert {edge[2] for edge in _edges(indexed, "fan")} == {
        f"GET /p{i}/leaf" for i in range(owners)
    }
    assert _route_keys(plan.chain_refusals, grouped, by_id) == {}
    assert _route_keys(plan.outcomes, grouped, by_id)[("src/fan.ts", "GET /leaf")] == PROMOTED


@pytest.mark.parametrize(("lower", "served"), [(20, True), (26, False)])
def test_a_route_whose_walk_visits_more_than_1024_scopes_is_not_served(
    workspace: Workspace, tmp_path: Path, lower: int, served: bool
) -> None:
    # 20 plugins register the leaf, and each is registered by `lower` plugins: 1 + 20 + 2 * 20 *
    # lower scopes (821 for 20, 1061 for 26) with one distinct result either way. The prediction's
    # 64 x 64 fan-out is the same shape, larger.
    indexed = build(workspace, tmp_path, {"fan": _fan_visits(20, lower)})
    plan, grouped, by_id = _plan(indexed)
    refusals = _route_keys(plan.chain_refusals, grouped, by_id)
    if served:
        assert {edge[2] for edge in _edges(indexed, "fan")} == {"GET /x/leaf"}
        assert refusals == {}
    else:
        assert _edges(indexed, "fan") == set()
        assert refusals == {("src/fan.ts", "GET /leaf"): CHAIN_TOO_COMPLEX}


def test_a_64_by_64_fan_out_is_refused_as_too_complex(workspace: Workspace, tmp_path: Path) -> None:
    # the prediction's fan-out: the shape of the 20 x 26 case above, 64 x 64 (8257 scopes)
    indexed = build(workspace, tmp_path, {"fan": _fan_visits(64, 64)})
    plan, grouped, by_id = _plan(indexed)
    assert _edges(indexed, "fan") == set()
    assert _route_keys(plan.chain_refusals, grouped, by_id) == {
        ("src/fan.ts", "GET /leaf"): CHAIN_TOO_COMPLEX
    }


# -- incremental equals fresh ---------------------------------------------------------


def test_dropping_a_helper_s_route_drops_that_edge_only(
    trace: Workspace, settings, tmp_path: Path
) -> None:
    # I1
    root = tmp_path / "repos"
    dropped = USERS.replace("  app.post('/users', createUser);\n", "")
    edit(root, "trace", "src/routes/users.ts", dropped)
    sync_all(trace, root)
    assert _edges(trace, "trace") == {e for e in TRACE_EDGES if e[2] != "POST /users"}
    assert_equals_fresh(trace, settings, root, tmp_path, "route")


def test_an_imported_factory_that_returns_an_object_unserves_everything_it_reached(
    trace: Workspace, settings, tmp_path: Path
) -> None:
    # I2
    root = tmp_path / "repos"
    tail = "\n}\n\nexport async"
    boxed = TRACE_SERVER.replace("return app;" + tail, "return { app };" + tail)
    assert boxed != TRACE_SERVER
    edit(root, "trace", "src/server.ts", boxed)
    sync_all(trace, root)
    assert _edges(trace, "trace") == set()
    plan, grouped, by_id = _plan(trace)
    assert _by_path(plan.instance_outcomes, grouped[FASTIFY_INSTANCE], by_id) == {
        "src/main.ts": [FACTORY_UNPROVEN],
        "src/server.ts": [ENTRY_NOT_CALLED],
    }
    assert_equals_fresh(trace, settings, root, tmp_path, "factory")


def test_renaming_an_exported_helper_leaves_its_call_unresolved(
    trace: Workspace, settings, tmp_path: Path
) -> None:
    # I3
    root = tmp_path / "repos"
    renamed = USERS.replace("initializeUserRoutes", "initializeUsers")
    edit(root, "trace", "src/routes/users.ts", renamed)
    sync_all(trace, root)
    assert _edges(trace, "trace") == {e for e in TRACE_EDGES if e[0] != "src/routes/users.ts"}
    plan, grouped, by_id = _plan(trace)
    helpers = _by_path(plan.helper_outcomes, grouped[FASTIFY_HELPER_CALL], by_id)
    assert helpers["src/handlers.ts"] == sorted([HELPER_ACCEPTED, HELPER_UNRESOLVED])
    assert_equals_fresh(trace, settings, root, tmp_path, "rename")


def test_moving_a_helper_call_into_a_condition_unserves_its_routes(
    trace: Workspace, settings, tmp_path: Path
) -> None:
    # I4
    root = tmp_path / "repos"
    moved = HANDLERS.replace(
        "  initializeLocal(server);\n", "  if (process.env.LOCAL) initializeLocal(server);\n"
    )
    edit(root, "trace", "src/handlers.ts", moved)
    sync_all(trace, root)
    assert _edges(trace, "trace") == {e for e in TRACE_EDGES if e[2] != "GET /local"}
    plan, grouped, by_id = _plan(trace)
    helpers = _by_path(plan.helper_outcomes, grouped[FASTIFY_HELPER_CALL], by_id)
    assert HELPER_CONTROL_FLOW in helpers["src/handlers.ts"]
    assert_equals_fresh(trace, settings, root, tmp_path, "condition")


# -- what a client sees of a traced service (M8 review: exposure, dedup, link check) --------

# A compose service per repository, as the equivalence pair frozen in ``M8_PREDICTIONS.md`` has
# it: one service named for the repository, publishing 8080, whose image runs the server file.
COMPOSE_SERVICE_TEXT = (
    "services:\n  {name}:\n    build:\n      context: .\n      dockerfile: Dockerfile\n"
    '    ports:\n      - "8080:8080"\n'
)
SERVER_DOCKERFILE = (
    'FROM node:22-slim\nWORKDIR /app\nCOPY . /app\nCMD ["npx", "tsx", "src/main.ts"]\n'
)

# A factory that returns an object holding the application: the instance it builds is unproven.
UNPROVEN_SERVER = d("""
    import Fastify from 'fastify';

    export function createBox() {
      const app = Fastify();
      return { app };
    }
""")

UNPROVEN = {
    "package.json": PACKAGE,
    "src/routes/users.ts": PAIR_USERS,
    "src/server.ts": UNPROVEN_SERVER,
    "src/main.ts": d("""
        import { createBox } from './server';
        import { initializeUserRoutes } from './routes/users';

        const server = createBox();
        initializeUserRoutes(server);
        server.listen({ port: 8080 });
    """),
}

# One helper that registers GET /shared, reached from two proven instances in two files.
SHARED = d("""
    import type { FastifyInstance } from 'fastify';

    export function registerShared(app: FastifyInstance) {
      app.get('/shared', getShared);
    }
""")


def _entry(name: str, port: int) -> str:
    return (
        "import { createServer } from './server';\n"
        "import { registerShared } from './shared';\n\n"
        f"const {name} = createServer();\n"
        f"registerShared({name});\n"
        f"{name}.listen({{ port: {port} }});\n"
    )


DEDUP = {
    "package.json": PACKAGE,
    "src/server.ts": PLAIN_SERVER,
    "src/shared.ts": SHARED,
    "src/a.ts": _entry("first", 1),
    "src/b.ts": _entry("second", 2),
}
DEDUP_EDGE: Edge = ("src/shared.ts", "registerShared", "GET /shared", line(SHARED, "app.get("))

#: The decision of a request to a service whose compose file the join reads but whose command
#: it cannot tie to code (``repoatlas.graph.http_crossings.SOURCE_UNRESOLVED``).
SOURCE_UNRESOLVED_DECISION = "http-deployment-source-unresolved"


def _deployed(files: dict[str, str], service: str) -> dict[str, str]:
    """``files`` plus a compose file declaring ``service`` and a Dockerfile running the server."""
    return {
        **files,
        "docker-compose.yml": COMPOSE_SERVICE_TEXT.format(name=service),
        "Dockerfile": SERVER_DOCKERFILE,
    }


CLIENT_PROJECT = '[project]\nname = "client"\nversion = "1.0.0"\ndependencies = ["requests"]\n'


def _client(*services: str) -> dict[str, str]:
    """A Python repository whose ``fetch_<service>`` requests ``http://<service>:8080/users``."""
    calls = "import requests\n" + "".join(
        f"\n\ndef fetch_{service}() -> dict:\n"
        f'    return requests.get("http://{service}:8080/users", timeout=5).json()\n'
        for service in services
    )
    return {"pyproject.toml": CLIENT_PROJECT, "client/__init__.py": "", "client/calls.py": calls}


def _edge_count(workspace: Workspace, slug: str, edge_type: str) -> int:
    """How many edges of the type leave an entity of the repository."""
    row = workspace.database.execute(
        """
        SELECT COUNT(*) FROM edges e
        JOIN entities s ON s.id = e.src_id
        JOIN repositories r ON r.id = s.repo_id
        WHERE e.type = ? AND r.slug = ?
        """,
        (edge_type, slug),
    ).fetchone()
    return int(row[0])


def _served_count(workspace: Workspace, slug: str, body: str) -> int:
    """How many ``SERVES_HTTP_ROUTE`` edges the repository's code has to the route ``body``."""
    row = workspace.database.execute(
        """
        SELECT COUNT(*) FROM edges e
        JOIN entities s ON s.id = e.src_id
        JOIN entities d ON d.id = e.dst_id
        JOIN repositories r ON r.id = s.repo_id
        WHERE e.type = 'SERVES_HTTP_ROUTE' AND r.slug = ? AND d.name = ?
        """,
        (slug, body),
    ).fetchone()
    return int(row[0])


def _file_id(workspace: Workspace, slug: str, path: str) -> int:
    row = workspace.database.execute(
        "SELECT f.id FROM files f JOIN repositories r ON r.id = f.repo_id "
        "WHERE r.slug = ? AND f.rel_path = ?",
        (slug, path),
    ).fetchone()
    return int(row[0])


def _impact(workspace: Workspace, slug: str, name: str, path: str) -> ImpactResponse:
    """The impact of changing the declaration ``name`` of ``path``, seeded by its key."""
    row = workspace.database.execute(
        "SELECT key FROM entities WHERE qualified_name = ? AND key LIKE ?",
        (name, f"sym:{slug}:{path}:%"),
    ).fetchone()
    assert row is not None, (slug, name)
    request = ImpactRequest(context=CapabilityContext(scope=CapabilityScope.GLOBAL), seed=row[0])
    return CapabilityService(workspace).impact(request)


def _reached(response: ImpactResponse) -> set[tuple[str | None, str]]:
    """(path, name) of everything an impact answer reaches, tests included."""
    return {(item.path, item.name) for item in (*response.candidates, *response.tests)}


def _kinds(chain: list[dict[str, Any]]) -> list[tuple[Any, Any]]:
    return [(hop["kind"], hop["status"]) for hop in chain]


def test_a_factory_returning_an_object_leaves_the_instance_unproven_and_serves_nothing(
    workspace: Workspace, tmp_path: Path
) -> None:
    indexed = build(workspace, tmp_path, {"unproven": _deployed(UNPROVEN, "unproven")})
    plan, grouped, by_id = _plan(indexed)
    instances = _by_path(plan.instance_outcomes, grouped[FASTIFY_INSTANCE], by_id)
    assert instances.get("src/main.ts") == [FACTORY_UNPROVEN]
    assert _edges(indexed, "unproven") == set()
    assert _edge_count(indexed, "unproven", "SERVES_HTTP_ROUTE") == 0
    # nothing serves a route, so nothing is exposed: no claim of the join, no stored edge
    repository = indexed.repositories.get_by_slug("unproven")
    assert repository is not None
    http_plan = _http(indexed)
    assert [row for row in http_plan.code_routes if row.repo_id == repository.id] == []
    assert [claim for claim in http_plan.exposed if claim.repo_id == repository.id] == []
    assert _edge_count(indexed, "unproven", "EXPOSES_HTTP_ENDPOINT") == 0


def test_a_compose_service_running_node_ties_no_code_so_the_source_alone_links_nothing(
    workspace: Workspace, tmp_path: Path
) -> None:
    # The deployment tie reads the commands of python, uvicorn and gunicorn only
    # (deploy_identity._application); a Dockerfile that runs node reaches no code, for a
    # registered plugin and a handed server alike. So a client reaches a TypeScript service by
    # the source alone in none of them, and every link below is a user's deployment link.
    repositories = {
        "direct": _deployed(DIRECT, "direct"),
        "traced": _deployed(PAIRED, "traced"),
        "unproven": _deployed(UNPROVEN, "unproven"),
        "client": _client("direct", "traced", "unproven"),
    }
    built = build(workspace, tmp_path, repositories)
    assert sorted(_http(built).deployments.values()) == [COMMAND_UNREAD] * 3
    for service in ("direct", "traced", "unproven"):
        assert decided(built, f"fetch_{service}")[0] == SOURCE_UNRESOLVED_DECISION
        assert handler_of(built, f"fetch_{service}") is None


def test_registering_a_plugin_and_handing_it_the_server_link_the_same_client(
    workspace: Workspace, tmp_path: Path
) -> None:
    repositories = {
        "direct": _deployed(DIRECT, "direct"),
        "traced": _deployed(PAIRED, "traced"),
        "client": _client("direct", "traced"),
    }
    built = build(workspace, tmp_path, repositories)
    for service in ("direct", "traced"):
        link(built, service, url=f"http://{service}:8080")
    sync_all(built, tmp_path / "repos")
    # the control first: a plugin registered on a plain instance must link, or the fixture is wrong
    direct_decision, direct_chain, direct_evidence = decided(built, "fetch_direct")
    assert direct_decision == "linked"
    assert handler_of(built, "fetch_direct") == "direct/src/routes/users.ts"
    traced_decision, traced_chain, traced_evidence = decided(built, "fetch_traced")
    assert traced_decision == direct_decision
    assert handler_of(built, "fetch_traced") == "traced/src/routes/users.ts"
    assert _kinds(traced_chain) == _kinds(direct_chain)
    assert traced_evidence["identity_source"] == direct_evidence["identity_source"]
    assert traced_evidence["identity_source"] == "deployment-link"


def test_a_client_of_an_unproven_service_is_not_linked_where_the_proven_control_is(
    workspace: Workspace, tmp_path: Path
) -> None:
    repositories = {
        "unproven": _deployed(UNPROVEN, "unproven"),
        "traced": _deployed(PAIRED, "traced"),
        "client": _client("unproven", "traced"),
    }
    built = build(workspace, tmp_path, repositories)
    # a user states where each is deployed: only a repository whose code serves the route can join
    for service in ("unproven", "traced"):
        link(built, service, url=f"http://{service}:8080")
    sync_all(built, tmp_path / "repos")

    # the control: the proven instance's route is served, so the request links and impact follows
    assert decided(built, "fetch_traced")[0] == "linked"
    assert handler_of(built, "fetch_traced") == "traced/src/routes/users.ts"
    traced = _impact(built, "traced", "initializeUserRoutes", "src/routes/users.ts")
    assert traced.seed.status == "resolved"
    assert "fetch_traced" in {name for _path, name in _reached(traced)}

    # the unproven instance serves and exposes nothing, so even a stated deployment links nothing
    assert _edges(built, "unproven") == set()
    assert _edge_count(built, "unproven", "SERVES_HTTP_ROUTE") == 0
    assert _edge_count(built, "unproven", "EXPOSES_HTTP_ENDPOINT") == 0
    repository = built.repositories.get_by_slug("unproven")
    assert repository is not None
    assert [c for c in _http(built).exposed if c.repo_id == repository.id] == []
    decision, _chain, evidence = decided(built, "fetch_unproven")
    assert decision == SOURCE_UNRESOLVED_DECISION
    assert "declaration_ids" not in evidence
    assert handler_of(built, "fetch_unproven") is None
    unproven = _impact(built, "unproven", "initializeUserRoutes", "src/routes/users.ts")
    assert unproven.seed.status == "resolved"
    reached = _reached(unproven)
    assert not {name for _path, name in reached if name.startswith("fetch_")}
    assert not [path for path, _name in reached if path and path.endswith("calls.py")]


def test_one_helper_reached_from_two_instances_is_served_once_and_rooted_at_both(
    workspace: Workspace, tmp_path: Path
) -> None:
    indexed = build(workspace, tmp_path, {"dedup": DEDUP})
    assert _edges(indexed, "dedup") == {DEDUP_EDGE}
    # one edge, not one per application (the set above would hide a duplicate)
    assert _served_count(indexed, "dedup", "GET /shared") == 1
    served = [row for row in _http(indexed).code_routes if row.body == "GET /shared"]
    assert len(served) == 1
    assert _roots(indexed, "dedup") == {("src/shared.ts", "GET /shared"): {"src/a.ts", "src/b.ts"}}


def test_the_order_files_are_indexed_in_does_not_change_what_is_served_or_rooted(
    workspace: Workspace, tmp_path: Path
) -> None:
    # `reversed` is the same repository with its files written, and so first indexed, in the
    # opposite order; its a.ts then arrives last, by a sync, so its file IDs run the other way
    others = {path: text for path, text in DEDUP.items() if path != "src/a.ts"}
    built = build(
        workspace,
        tmp_path,
        {"dedup": DEDUP, "reversed": dict(reversed(others.items()))},
    )
    root = tmp_path / "repos"
    edit(root, "reversed", "src/a.ts", DEDUP["src/a.ts"])
    sync_all(built, root)
    forward = _file_id(built, "dedup", "src/a.ts") < _file_id(built, "dedup", "src/b.ts")
    backward = _file_id(built, "reversed", "src/a.ts") < _file_id(built, "reversed", "src/b.ts")
    assert forward != backward
    assert _edges(built, "reversed") == _edges(built, "dedup") == {DEDUP_EDGE}
    assert _served_count(built, "reversed", "GET /shared") == 1
    assert _roots(built, "reversed") == _roots(built, "dedup")
    assert _roots(built, "reversed") == {("src/shared.ts", "GET /shared"): {"src/a.ts", "src/b.ts"}}
