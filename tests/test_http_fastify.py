"""Fastify routes, in their own file and through registered plugins (deployment HTTP roadmap, M2).

An application's own route with a literal path is served by its file. A plugin's routes
are served under the prefixes of the registrations that reach it from an application,
across modules, which the global pass decides. The fixture covers the shapes the work
survey found -- plugins nested two deep, paths built from imported constants -- and the
ways a registration stops being readable. Everything is asserted as closed sets and exact
lines.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from repoatlas.graph.linker import ReferenceLinker, plan_repository_http
from repoatlas.graph.ts_routes import (
    CONSTANT_UNRESOLVED,
    NOT_REGISTERED,
    PROMOTED,
    UNREADABLE_REGISTRATION,
)
from repoatlas.models.parsing import EdgeType, ParsedReference
from repoatlas.parsers.http_facts import FASTIFY_REGISTER, FASTIFY_ROUTE, PENDING_FACT
from repoatlas.parsers.typescript import TypeScriptParser
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build
from tests.incremental_corpus import assert_equals_fresh, edit, sync_all


def d(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def _references(source: str, name: str = "app.ts") -> list[ParsedReference]:
    return list(TypeScriptParser().parse(Path(name), d(source)).references)


def _served(source: str) -> set[str]:
    return {
        r.target_key
        for r in _references(source)
        if r.edge_type is EdgeType.SERVES_HTTP_ROUTE and PENDING_FACT not in r.meta
    }


def _claims(source: str, role: str) -> list[ParsedReference]:
    return [r for r in _references(source) if r.meta.get(PENDING_FACT) == role]


# -- one file ------------------------------------------------------------------------------


def test_an_application_route_is_served_by_its_file_at_the_call() -> None:
    [reference] = [
        r
        for r in _references(
            """\
            import Fastify from 'fastify';

            const server = Fastify();

            server.get('/health', async () => ({ ok: true }));
            """
        )
        if r.edge_type is EdgeType.SERVES_HTTP_ROUTE
    ]
    assert reference.target_key == "GET /health"
    assert (reference.line_start, reference.line_end) == (5, 5)
    assert reference.meta == {"source": "fastify", "provenance": "exact"}


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # route(): a literal method, or a list of them, and a literal url
        (
            "server.route({ method: ['GET', 'HEAD'], url: '/ready', handler: h });",
            {"GET /ready", "HEAD /ready"},
        ),
        ("server.route({ method: 'get', url: '/lower', handler: h });", set()),
        ("server.route({ method: verb, url: '/dynamic', handler: h });", set()),
        ("server.route({ ...base, method: 'GET', url: '/spread' });", set()),
        # a shorthand with options before the handler
        ("server.post('/items', { schema: {} }, h);", {"POST /items"}),
        # one argument is no route
        ("server.get('/alone');", set()),
        ("server.all('/everything', h);", set()),
    ],
)
def test_route_declarations_are_read_only_when_literal(source: str, expected: set[str]) -> None:
    assert _served(f"import Fastify from 'fastify';\nconst server = Fastify();\n{source}\n") == (
        expected
    )


@pytest.mark.parametrize(
    "source",
    [
        # not a Fastify application at all
        "const cache = new Map();\ncache.get('/x', 1);\n",
        "import Fastify from 'my-fastify';\nconst server = Fastify();\nserver.get('/x', h);\n",
        # bound twice
        "import Fastify from 'fastify';\nlet server = Fastify();\nserver = wrap(server);\n"
        "server.get('/x', h);\n",
    ],
)
def test_an_unproven_receiver_serves_nothing(source: str) -> None:
    assert _served(source) == set()
    assert _claims(source, FASTIFY_ROUTE) == []


def test_a_shadowing_parameter_is_its_function_s_server_not_the_application() -> None:
    source = (
        "import Fastify from 'fastify';\nconst server = Fastify();\n"
        "function f(server) { server.get('/x', h); }\n"
    )
    assert _served(source) == set()
    [claim] = _claims(source, FASTIFY_ROUTE)
    assert claim.meta["scope"] == "f"


def test_only_a_plugin_s_first_parameter_is_its_server() -> None:
    source = (
        "import type { FastifyInstance } from 'fastify';\n"
        "export default async function routes(server: FastifyInstance, cache: Cache) {\n"
        "  cache.get('/cached', h);\n"
        "  server.get('/x', h);\n"
        "}\n"
    )
    assert [claim.target_key for claim in _claims(source, FASTIFY_ROUTE)] == ["GET /x"]


def test_plugins_are_read_only_in_a_file_that_imports_fastify() -> None:
    plugin = "export default async function routes(server) {\n  server.get('/x', h);\n}\n"
    assert _claims(plugin, FASTIFY_ROUTE) == []
    typed = "import type { FastifyInstance } from 'fastify';\n" + plugin
    [claim] = _claims(typed, FASTIFY_ROUTE)
    assert claim.target_key == "GET /x"
    assert claim.meta["scope"] == "routes"


@pytest.mark.parametrize(
    ("options", "readable", "prefix"),
    [
        ("", "true", None),
        (", { prefix: '/v1' }", "true", "/v1"),
        (", { prefix: '/v1', logLevel: 'warn' }", "true", "/v1"),
        (", { prefix: '/v1/' }", "false", None),
        (", { prefix: 'v1' }", "false", None),
        (", { prefix: PREFIX }", "false", None),
        (", { ...options }", "false", None),
        (", options", "false", None),
    ],
)
def test_a_registration_is_readable_only_with_a_literal_prefix(
    options: str, readable: str, prefix: str | None
) -> None:
    source = (
        "import Fastify from 'fastify';\nimport routes from './routes';\n"
        f"const server = Fastify();\nserver.register(routes{options});\n"
    )
    [claim] = _claims(source, FASTIFY_REGISTER)
    assert claim.meta["readable"] == readable
    assert claim.meta["prefix"] == ("null" if prefix is None else f'"{prefix}"')
    assert (claim.meta["module"], claim.meta["name"]) == ("./routes", "default")


def test_a_fastify_plugin_given_a_prefix_is_unreadable() -> None:
    source = (
        "import Fastify from 'fastify';\nimport fp from 'fastify-plugin';\n"
        "import routes from './routes';\nconst server = Fastify();\n"
        "server.register(fp(routes), { prefix: '/v1' });\nserver.register(fp(routes));\n"
    )
    first, second = _claims(source, FASTIFY_REGISTER)
    assert (first.meta["wrapped"], first.meta["readable"]) == ("true", "false")
    assert (second.meta["wrapped"], second.meta["readable"]) == ("true", "true")


# -- across modules ------------------------------------------------------------------------

MAIN = d("""
    import Fastify from 'fastify';
    import routes from './routes/index';
    import { adminRoutes } from './routes/admin';
    import legacy from './routes/legacy';
    import spread from './routes/spread';
    import shared from './routes/shared';
    import exact from './routes/exact';
    import odd from './routes/odd';
    import chain from './routes/chain';

    const PREFIX = process.env.PREFIX ?? '/x';
    const options = { prefix: '/o' };

    const server = Fastify({ logger: true });

    server.get('/health', async () => ({ ok: true }));
    server.route({ method: ['GET', 'HEAD'], url: '/ready', handler: async () => 'ok' });
    server.register(routes, { prefix: '/api' });
    server.register(adminRoutes, { prefix: '/admin' });
    server.register(legacy, { prefix: PREFIX });
    server.register(spread, { ...options });
    server.register(shared, { prefix: '/shared' });
    server.register(exact);
    server.register(odd, { prefix: '/odd' });
    server.register(chain);
    server.register(
      async (inner) => {
        inner.get('/inline', async () => 'x');
      },
      { prefix: '/v0' },
    );

    server.listen({ port: 8080 });
""")

ORDERS = d("""
    import type { FastifyInstance } from 'fastify';
    import { ORDERS } from '../constants';

    const ITEM = ':orderId';

    export default async function orders(server: FastifyInstance) {
      server.get(`/${ORDERS}`, async () => []);
      server.get(`/${ORDERS}/${ITEM}`, async () => ({}));
      server.post('/', async () => ({}));
    }
""")

EXACT = d("""
    import { FastifyInstance } from 'fastify';

    export default async function exact(server: FastifyInstance) {
      server.get('/exact', async () => 'exact');
    }
""")

CONSTANTS = "export const ORDERS = 'orders';\nexport function notConstant() {\n  return 'x';\n}\n"


def _plugin(name: str, path: str, *, wrapped: bool = False) -> str:
    if wrapped:
        return d(f"""
            import fp from 'fastify-plugin';

            export default fp(async (server) => {{
              server.get('{path}', async () => '{name}');
            }});
        """)
    return d(f"""
        import {{ FastifyInstance }} from 'fastify';

        export default async function {name}(server: FastifyInstance) {{
          server.get('{path}', async () => '{name}');
        }}
    """)


ORDERS_REPO = {
    "package.json": '{"name": "orders"}\n',
    "src/main.ts": MAIN,
    "src/constants.ts": CONSTANTS,
    # nested two deep: a plugin registering another, and an inline plugin that shadows
    # its parent's parameter name, as Fastify code often does
    "src/routes/index.ts": d("""
        import { FastifyInstance } from 'fastify';
        import orders from './orders';

        export default async function routes(fastify: FastifyInstance) {
          fastify.register(orders, { prefix: '/v1' });
          fastify.register(async (fastify) => {
            fastify.get('/ping', async () => 'pong');
          });
        }
    """),
    "src/routes/orders.ts": ORDERS,
    "src/routes/admin.ts": d("""
        import { FastifyInstance } from 'fastify';

        export const adminRoutes = async (app: FastifyInstance) => {
          app.delete('/cache', async () => ({}));
        };
    """),
    "src/routes/legacy.ts": _plugin("legacy", "/old"),
    "src/routes/spread.ts": _plugin("spread", "/thing"),
    "src/routes/shared.ts": _plugin("shared", "/shared-thing", wrapped=True),
    "src/routes/exact.ts": EXACT,
    "src/routes/unused.ts": _plugin("unused", "/never"),
    "src/routes/odd.ts": d("""
        import { FastifyInstance } from 'fastify';
        import { notConstant } from '../constants';

        export default async function odd(server: FastifyInstance) {
          server.get(`/${notConstant}`, async () => 'odd');
        }
    """),
    "src/routes/chain.ts": d("""
        import { FastifyInstance } from 'fastify';
        import p0 from './chain/p0';

        export default async function chain(server: FastifyInstance) {
          server.register(p0, { prefix: '/c' });
        }
    """),
}
for _depth in range(7):
    _below = f"import p{_depth + 1} from './p{_depth + 1}';\n" if _depth < 6 else ""
    _register = (
        f"  server.register(p{_depth + 1}, {{ prefix: '/d{_depth + 1}' }});\n" if _depth < 6 else ""
    )
    ORDERS_REPO[f"src/routes/chain/p{_depth}.ts"] = (
        "import { FastifyInstance } from 'fastify';\n"
        + _below
        + f"\nexport default async function p{_depth}(server: FastifyInstance) {{\n"
        + _register
        + f"  server.get('/leaf', async () => {_depth});\n}}\n"
    )

#: Another repository with the same module path, never registered: nothing crosses.
ELSEWHERE = {
    "package.json": '{"name": "elsewhere"}\n',
    "src/routes/unused.ts": _plugin("unused", "/elsewhere"),
}

WORKSPACE = {"orders": ORDERS_REPO, "elsewhere": ELSEWHERE}


def line(text: str, needle: str) -> int:
    return next(number for number, row in enumerate(text.splitlines(), 1) if needle in row)


def served(workspace: Workspace) -> set[tuple[str, str, str, str, int | None]]:
    """(repository, file, handler, route, first line or None) for every served route."""
    rows = workspace.database.execute(
        """
        SELECT r.slug, f.rel_path, s.qualified_name, d.name, e.line_start
        FROM edges e
        JOIN entities s ON s.id = e.src_id
        JOIN entities d ON d.id = e.dst_id
        JOIN files f ON f.id = s.file_id
        JOIN repositories r ON r.id = s.repo_id
        WHERE e.type = 'SERVES_HTTP_ROUTE'
        """
    ).fetchall()
    return {tuple(row) for row in rows}


def outcomes(workspace: Workspace) -> dict[tuple[str, str], str]:
    """Each Fastify route claim's outcome, by (file, declared route)."""
    plan = ReferenceLinker(workspace.knowledge).prepare_global().http
    rows = workspace.database.execute(
        """
        SELECT u.id, f.rel_path, u.target_key FROM unresolved_refs u
        JOIN files f ON f.id = u.file_id
        WHERE u.meta_json LIKE '%"fastify-route"%'
        """
    ).fetchall()
    return {(row[1], row[2]): plan.routes[row[0]] for row in rows}


ORDERS_FILE = "src/routes/orders.ts"
EXPECTED = {
    # the application's own routes, read in its file: exact
    ("orders", "src/main.ts", "src/main.ts", "GET /health", line(MAIN, "server.get('/health'")),
    ("orders", "src/main.ts", "src/main.ts", "GET /ready", line(MAIN, "server.route(")),
    ("orders", "src/main.ts", "src/main.ts", "HEAD /ready", line(MAIN, "server.route(")),
    # an inline plugin under a prefix: composed
    ("orders", "src/main.ts", "src/main.ts", "GET /v0/inline", None),
    # two registrations deep, the path from an imported and a local constant: composed
    ("orders", ORDERS_FILE, "orders", "GET /api/v1/orders", None),
    ("orders", ORDERS_FILE, "orders", "GET /api/v1/orders/:orderId", None),
    # a `/` route under a prefix is the prefix itself
    ("orders", ORDERS_FILE, "orders", "POST /api/v1", None),
    # an inline plugin shadowing its parent's parameter
    ("orders", "src/routes/index.ts", "routes", "GET /api/ping", None),
    # a named export registered under a prefix
    ("orders", "src/routes/admin.ts", "adminRoutes", "DELETE /admin/cache", None),
    # nothing adds to it: exact, with the call's lines
    ("orders", "src/routes/exact.ts", "exact", "GET /exact", line(EXACT, "server.get(")),
    # a chain at most four registrations below the application
    ("orders", "src/routes/chain/p0.ts", "p0", "GET /c/leaf", None),
    ("orders", "src/routes/chain/p1.ts", "p1", "GET /c/d1/leaf", None),
    ("orders", "src/routes/chain/p2.ts", "p2", "GET /c/d1/d2/leaf", None),
    ("orders", "src/routes/chain/p3.ts", "p3", "GET /c/d1/d2/d3/leaf", None),
}


@pytest.fixture
def indexed(workspace: Workspace, tmp_path: Path) -> Workspace:
    return build(workspace, tmp_path, WORKSPACE)


def test_routes_are_served_exactly_where_a_proven_registration_chain_reaches(
    indexed: Workspace,
) -> None:
    assert served(indexed) == EXPECTED


def test_what_is_not_served_says_why(indexed: Workspace) -> None:
    found = outcomes(indexed)
    assert found[("src/routes/legacy.ts", "GET /old")] == UNREADABLE_REGISTRATION
    assert found[("src/routes/spread.ts", "GET /thing")] == UNREADABLE_REGISTRATION
    assert found[("src/routes/shared.ts", "GET /shared-thing")] == UNREADABLE_REGISTRATION
    assert found[("src/routes/unused.ts", "GET /never")] == NOT_REGISTERED
    assert found[("src/routes/odd.ts", "GET /{notConstant}")] == CONSTANT_UNRESOLVED
    assert found[("src/routes/unused.ts", "GET /elsewhere")] == NOT_REGISTERED
    # deeper than the bound
    assert found[("src/routes/chain/p5.ts", "GET /leaf")] == NOT_REGISTERED
    assert found[(ORDERS_FILE, "GET /{ORDERS}")] == PROMOTED


def test_a_repository_plans_its_own_routes_alone(indexed: Workspace) -> None:
    """The inventory's replan of one repository is exactly its share of the global plan."""
    plan = ReferenceLinker(indexed.knowledge).prepare_global().http
    for slug in ("orders", "elsewhere"):
        [repo_id] = [
            row[0]
            for row in indexed.database.execute(
                "SELECT id FROM repositories WHERE slug = ?", (slug,)
            ).fetchall()
        ]
        own = plan_repository_http(indexed.knowledge, repo_id).plan
        mine = {(e.src_id, e.target.name, e.line_start) for e in plan.http if e.repo_id == repo_id}
        assert {(e.src_id, e.target.name, e.line_start) for e in own.http} == mine
        assert {k: v for k, v in plan.routes.items() if k in own.routes} == own.routes


# -- incremental equals fresh --------------------------------------------------------------


def test_editing_a_registration_prefix_moves_the_routes(
    indexed: Workspace, settings, tmp_path: Path
) -> None:
    root = tmp_path / "repos"
    edit(root, "orders", "src/main.ts", MAIN.replace("prefix: '/api'", "prefix: '/rest'"))
    sync_all(indexed, root)
    routes = {route for *_, route, _ in served(indexed)}
    assert "GET /rest/v1/orders" in routes and "GET /api/v1/orders" not in routes
    assert_equals_fresh(indexed, settings, root, tmp_path, "prefix")


def test_editing_an_imported_constant_moves_the_route(
    indexed: Workspace, settings, tmp_path: Path
) -> None:
    root = tmp_path / "repos"
    edit(root, "orders", "src/constants.ts", CONSTANTS.replace("'orders'", "'purchases'"))
    sync_all(indexed, root)
    routes = {route for *_, route, _ in served(indexed)}
    assert "GET /api/v1/purchases" in routes and "GET /api/v1/orders" not in routes
    assert_equals_fresh(indexed, settings, root, tmp_path, "constant")


def test_a_body_edit_in_a_plugin_module_changes_no_route(
    indexed: Workspace, settings, tmp_path: Path
) -> None:
    root = tmp_path / "repos"
    before = served(indexed)
    edit(root, "orders", ORDERS_FILE, ORDERS.replace("async () => []", "async () => [].concat([])"))
    work = sync_all(indexed, root)
    assert served(indexed) == before
    assert work.repositories_invalidated == 0
    assert_equals_fresh(indexed, settings, root, tmp_path, "body")
