"""Service identities a docker-compose file declares, tied to code (deployment HTTP roadmap, M3).

A compose service is reached at ``http://<name>:<container port>``. Its identity exposes
the routes of the one application the service's image runs: the Dockerfile's command
names a module, which the final stage's copies map back to a repository file. The
join then links requests to those handlers, inside the service's own repository too.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path, PurePosixPath

import pytest
import yaml
from repoatlas.graph.deploy_identity import (
    COMMAND_UNREAD,
    DOCKERFILE_UNREAD,
    NO_BUILD,
    NO_PORT,
    NO_ROUTES,
    NOT_IN_IMAGE,
    TIED,
    _application,
    _argv,
    _copied,
)
from repoatlas.graph.linker import ReferenceLinker
from repoatlas.parsers.deploy_compose import compose_references, is_compose_file
from repoatlas.parsers.dockerfile import DockerfileParser
from repoatlas.parsers.http_facts import COMPOSE_SERVICE, DOCKERFILE_COMMAND, PENDING_FACT
from repoatlas.services.workspace import Workspace
from tests.http_crossing_corpus import crossings, decided, links
from tests.impact_corpus import build
from tests.incremental_corpus import assert_equals_fresh, edit, sync_all


def d(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def _services(text: str, path: str = "deploy/docker-compose.yml") -> dict[str, dict[str, object]]:
    documents = list(yaml.compose_all(d(text), Loader=yaml.SafeLoader))
    return {
        r.target_key: {
            key: json.loads(value) if key != "image" else value
            for key, value in r.meta.items()
            if key != PENDING_FACT
        }
        for r in compose_references(PurePosixPath(path), documents)
    }


def _command(text: str) -> dict[str, object] | None:
    found = [
        r
        for r in DockerfileParser().parse(Path("Dockerfile"), d(text)).references
        if r.meta.get(PENDING_FACT) == DOCKERFILE_COMMAND
    ]
    if not found:
        return None
    [claim] = found
    return {
        key: json.loads(value) if key != "workdir" else value
        for key, value in claim.meta.items()
        if key != PENDING_FACT
    }


# -- compose services ------------------------------------------------------------------


def test_a_service_records_its_names_ports_build_and_command() -> None:
    services = _services("""
        services:
          orders:
            build:
              context: ..
              dockerfile: orders/Dockerfile
            container_name: orders-api
            hostname: orders.local
            networks:
              backend:
                aliases: [orders-internal]
            expose: ["5000", 5001]
            ports:
              - "127.0.0.1:8080:5002"
              - "8443:5003/tcp"
              - target: 5004
                published: 9000
              - "6000-6005:6000-6005"
            command: python3 -u /srv/app.py
            entrypoint: ["tini", "--"]
          cache:
            image: redis:7
          env:
            build: ${CONTEXT}
            container_name: ${NAME}
            command: ["python3", "$SCRIPT"]
    """)
    assert services["orders"] == {
        "names": ["orders", "orders-api", "orders.local", "orders-internal"],
        "ports": ["5000", "5001", "5002", "5003", "5004"],
        "build": {"context": "", "dockerfile": "orders/Dockerfile"},
        "command": ["python3", "-u", "/srv/app.py"],
        "entrypoint": ["tini", "--"],
    }
    assert services["cache"] == {"names": ["cache"], "ports": [], "image": "redis:7"}
    # compose interpolates ${...} from where it runs: those fields are not read
    assert services["env"] == {"names": ["env"], "ports": [], "command": None}


def test_a_build_outside_the_repository_is_not_read() -> None:
    services = _services("services:\n  a:\n    build: ../../elsewhere\n", "compose.yml")
    assert "build" not in services["a"]


@pytest.mark.parametrize(
    ("name", "compose"),
    [
        ("docker-compose.yml", True),
        ("docker-compose.prod.yaml", True),
        ("compose.yml", True),
        ("compose.override.yaml", True),
        ("docker-compose.yml.jinja", False),
        ("my-compose.yml", False),
        ("values.yaml", False),
    ],
)
def test_compose_files_are_known_by_name(name: str, compose: bool) -> None:
    assert is_compose_file(PurePosixPath(name)) is compose


# -- Dockerfile commands ---------------------------------------------------------------


def test_the_final_stage_s_workdir_copies_and_command_are_recorded() -> None:
    command = _command("""
        FROM node:20 AS assets
        RUN npm run build
        FROM python:3.11-slim
        WORKDIR /srv
        WORKDIR app
        COPY requirements.txt ./
        COPY src /srv/app/src
        COPY --from=assets /dist /srv/app/static
        ADD https://example.com/x.tgz /opt/
        COPY conf/*.ini /etc/app/
        ENTRYPOINT ["tini", "--"]
        CMD python3 -u src/main.py
    """)
    assert command == {
        "workdir": "/srv/app",
        "copies": [
            [["requirements.txt"], "/srv/app", 6, False, True],
            [["src"], "/srv/app/src", 7, False, False],
            [[], "/srv/app/static", 8, True, False],
            [[], "/opt", 9, True, True],
            [[], "/etc/app", 10, True, True],
        ],
        "entrypoint": {"argv": ["tini", "--"], "form": "exec", "line": 11},
        "cmd": {"argv": ["python3", "-u", "src/main.py"], "form": "shell", "line": 12},
    }


@pytest.mark.parametrize(
    ("text", "argv"),
    [
        ("FROM x\nWORKDIR /srv\nCMD python3 app.py && echo done\n", None),  # an operator
        ("FROM x\nWORKDIR /srv\nCMD python3 $APP\n", None),  # computed
        ('FROM x\nCMD ["python3", "$APP"]\n', ["python3", "$APP"]),  # exec form is verbatim
    ],
)
def test_a_command_is_kept_only_as_far_as_it_is_literal(text: str, argv: list[str] | None) -> None:
    command = _command(text)
    assert command is not None and command["cmd"]["argv"] == argv  # type: ignore[index]


def test_a_stage_without_a_command_keeps_its_copies_for_a_service_s_command() -> None:
    expected = {"copies": [[["."], "/srv", 2, False, False]], "entrypoint": None, "cmd": None}
    assert _command("FROM x\nCOPY . /srv\n") == expected
    final = {"copies": [[["."], "/srv", 4, False, False]], "entrypoint": None, "cmd": None}
    assert _command('FROM x AS a\nCMD ["run"]\nFROM y\nCOPY . /srv\n') == final
    assert _command("FROM x\nRUN make\n") is None
    # A stage whose every value is computed states nothing a service could use.
    assert _command("FROM x\nCMD python3 $APP\nCOPY $SRC /srv\n") is None


# -- what the container runs ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "workdir", "paths"),
    [
        (["python3", "-u", "/opt/svc/src/main.py"], None, ["/opt/svc/src/main.py"]),
        (["/usr/local/bin/python", "src/main.py"], "/srv", ["/srv/src/main.py"]),
        (["python3", "src/main.py"], None, None),  # relative, and no working directory
        (["python3", "-m", "app"], "/srv", None),
        (["python3", "-X", "dev", "app.py"], "/srv", None),
        (
            ["uvicorn", "src.app:app", "--host", "127.0.0.1", "--port", "5000"],
            "/srv",
            ["/srv/src/app.py", "/srv/src/app/__init__.py"],
        ),
        (
            ["uvicorn", "--port=5000", "--reload", "app:api"],
            "/srv",
            ["/srv/app.py", "/srv/app/__init__.py"],
        ),
        (["uvicorn", "--app-dir", "src", "app:app"], "/srv", None),
        (["uvicorn", "--factory", "app:create_app"], "/srv", None),
        (["uvicorn", "app:app", "--port"], "/srv", None),
        (["uvicorn", "app"], "/srv", None),
        (
            ["gunicorn", "-b", ":5000", "-w", "4", "wsgi:app"],
            "/srv",
            ["/srv/wsgi.py", "/srv/wsgi/__init__.py"],
        ),
        (["gunicorn", "--chdir", "src", "wsgi:app"], "/srv", None),
        (["gunicorn", "-c", "conf.py", "wsgi:app"], "/srv", None),
        (["./boot.sh"], "/srv", None),
        (["flask", "run"], "/srv", None),
    ],
)
def test_the_module_a_command_runs(
    argv: list[str], workdir: str | None, paths: list[str] | None
) -> None:
    assert _application(argv, workdir) == paths


def _cmd(argv: list[str] | None, form: str = "exec") -> str:
    return json.dumps({"argv": argv, "form": form, "line": 1})


@pytest.mark.parametrize(
    ("service", "image", "expected"),
    [
        ({}, {"cmd": _cmd(["python3", "a.py"])}, (["python3", "a.py"], False)),
        (
            {"command": '["python3", "b.py"]'},
            {"cmd": _cmd(["python3", "a.py"])},
            (["python3", "b.py"], True),
        ),
        (
            {},
            {"entrypoint": _cmd(["tini", "--"]), "cmd": _cmd(["python3", "a.py"])},
            (["tini", "--", "python3", "a.py"], False),
        ),
        (
            {},
            {"entrypoint": _cmd(["python3", "a.py"], "shell"), "cmd": _cmd(["x"])},
            (["python3", "a.py"], False),
        ),
        (
            {"entrypoint": '["python3"]', "command": '["c.py"]'},
            {"cmd": _cmd(["python3", "a.py"])},
            (["python3", "c.py"], True),
        ),
        ({"command": "null"}, {"cmd": _cmd(["python3", "a.py"])}, None),
        ({}, {"cmd": _cmd(None, "shell")}, None),
        ({}, {}, None),
    ],
)
def test_a_service_s_overrides_replace_the_image_s_command(
    service: dict[str, str], image: dict[str, str], expected: object
) -> None:
    assert _argv(service, image) == expected


FILES = {"orders/app.py", "orders/routes.py", "worker.py", "lib/x.py"}


@pytest.mark.parametrize(
    ("container", "copies", "found"),
    [
        ("/srv/orders/app.py", [[["orders"], "/srv/orders", 1, False, False]], "orders/app.py"),
        ("/srv/app.py", [[["orders/app.py"], "/srv/app.py", 1, False, False]], "orders/app.py"),
        ("/srv/app.py", [[["orders/app.py"], "/srv", 1, False, True]], "orders/app.py"),
        ("/srv/orders/app.py", [[["."], "/srv", 1, False, True]], "orders/app.py"),
        # a later copy layers over an earlier one, unless it has no file at that path
        (
            "/srv/x.py",
            [[["orders"], "/srv", 1, False, False], [["lib"], "/srv", 2, False, False]],
            "lib/x.py",
        ),
        (
            "/srv/app.py",
            [[["orders"], "/srv", 1, False, False], [["lib"], "/srv", 2, False, False]],
            "orders/app.py",
        ),
        # content from elsewhere, or an unknown destination, could be the module
        (
            "/srv/orders/app.py",
            [[["orders"], "/srv/orders", 1, False, False], [[], "/srv", 2, True, False]],
            None,
        ),
        ("/srv/orders/app.py", [[["orders"], None, 1, False, False]], None),
        ("/srv/orders/missing.py", [[["orders"], "/srv/orders", 1, False, False]], None),
        ("/srv/x.py", [[["../outside"], "/srv", 1, False, False]], None),
    ],
)
def test_the_repository_file_an_image_layers_at_a_path(
    container: str, copies: list[list[object]], found: str | None
) -> None:
    assert _copied(container, copies, "", FILES) == found


# -- the join -------------------------------------------------------------------------------

ORDERS_APP = d("""
    from fastapi import FastAPI

    from orders.routes import router

    app = FastAPI()
    app.include_router(router, prefix="/orders")


    @app.get("/health")
    def health():
        return {}
""")

ORDERS_ROUTES = d("""
    from fastapi import APIRouter

    router = APIRouter()


    @router.get("/{order_id}")
    def get_order(order_id: str):
        return {}
""")

COMPOSE = d("""
    services:
      orders:
        build:
          context: .
          dockerfile: orders/Dockerfile
        container_name: orders-api
        expose:
          - "5000"
      worker:
        build: .
        command: ["python3", "-u", "/srv/worker.py"]
      cache:
        image: redis:7
      scripted:
        build:
          context: .
          dockerfile: scripted/Dockerfile
        ports: ["8000:5000"]
      missing:
        build:
          context: .
          dockerfile: nowhere/Dockerfile
        expose: ["5000"]
      unmapped:
        build:
          context: .
          dockerfile: unmapped/Dockerfile
        expose: ["5000"]
      noport:
        build:
          context: .
          dockerfile: orders/Dockerfile
      bare:
        build:
          context: .
          dockerfile: bare/Dockerfile
        command: ["uvicorn", "orders.app:app", "--port", "7000"]
        expose: ["7000"]
""")

SHOP = {
    "pyproject.toml": '[project]\nname = "shop"\n',
    "docker-compose.yml": COMPOSE,
    "orders/__init__.py": "",
    "orders/app.py": ORDERS_APP,
    "orders/routes.py": ORDERS_ROUTES,
    "orders/Dockerfile": d("""
        FROM python:3.11-slim
        WORKDIR /srv
        COPY orders /srv/orders
        CMD ["uvicorn", "orders.app:app", "--host", "127.0.0.1", "--port", "5000"]
    """),
    "Dockerfile": (
        'FROM python:3.11-slim\nCOPY worker.py /srv/worker.py\nCMD ["python3", "/srv/worker.py"]\n'
    ),
    "worker.py": d("""
        import os

        import requests

        ORDERS = os.environ.get("ORDERS_URL", "http://orders:5000")


        def fetch_order(order_id):
            return requests.get(f"{ORDERS}/orders/{order_id}", timeout=5)
    """),
    "bare/Dockerfile": "FROM python:3.11-slim\nWORKDIR /srv\nCOPY orders /srv/orders\n",
    "scripted/Dockerfile": 'FROM python:3.11-slim\nCOPY . /srv\nENTRYPOINT ["./boot.sh"]\n',
    "unmapped/Dockerfile": (
        'FROM python:3.11-slim\nWORKDIR /srv\nCMD ["uvicorn", "orders.app:app"]\n'
    ),
}

WEB = {
    "pyproject.toml": '[project]\nname = "web"\n',
    "client.py": d("""
        import requests


        def check():
            return requests.get("http://orders-api:5000/health", timeout=5)
    """),
}


@pytest.fixture
def shop(workspace: Workspace, tmp_path: Path) -> Workspace:
    return build(workspace, tmp_path, {"shop": SHOP, "web": WEB})


def test_a_service_s_identity_reaches_the_application_its_image_runs(shop: Workspace) -> None:
    assert links(shop) == {
        ("check", "shop", "health", "GET http://orders-api:5000/health"),
        ("fetch_order", "shop", "get_order", "GET http://orders:5000/orders/{order_id}"),
    }
    evidence = {
        row["caller"]: {
            k: row["resolution"]["http"][k]
            for k in ("identity_source", "identity_in", "bound_in", "served_at")
        }
        for row in crossings(shop)
    }
    assert evidence == {
        "check": {
            "identity_source": "docker-compose-service",
            "identity_in": "docker-compose.yml:2",
            "bound_in": "orders/Dockerfile:4",
            "served_at": "orders/app.py:9",
        },
        "fetch_order": {
            "identity_source": "docker-compose-service",
            "identity_in": "docker-compose.yml:2",
            "bound_in": "orders/Dockerfile:4",
            "served_at": "orders/routes.py:6",
        },
    }


def _outcomes(workspace: Workspace) -> dict[str, str]:
    plan = ReferenceLinker(workspace.knowledge).prepare_global().http
    rows = workspace.database.execute(
        "SELECT id, target_key FROM unresolved_refs WHERE meta_json LIKE ?",
        (f'%"{COMPOSE_SERVICE}"%',),
    ).fetchall()
    return {row["target_key"]: plan.deployments[row["id"]] for row in rows}


def test_each_service_is_tied_or_says_why_not(shop: Workspace) -> None:
    assert _outcomes(shop) == {
        "orders": TIED,
        "worker": NO_ROUTES,
        "cache": NO_BUILD,
        "scripted": COMMAND_UNREAD,
        "missing": DOCKERFILE_UNREAD,
        "unmapped": NOT_IN_IMAGE,
        "noport": NO_PORT,
        # its Dockerfile runs nothing; the service's command supplies the program
        "bare": TIED,
    }


def test_editing_a_service_s_port_moves_its_identity(
    shop: Workspace, settings, tmp_path: Path
) -> None:
    root = tmp_path / "repos"
    moved = COMPOSE.replace(
        'orders-api\n    expose:\n      - "5000"', 'orders-api\n    expose:\n      - "5001"'
    )
    assert moved != COMPOSE
    edit(root, "shop", "docker-compose.yml", moved)
    sync_all(shop, root)
    assert links(shop) == set()
    assert_equals_fresh(shop, settings, root, tmp_path, "port")


REFUSED = {
    "pyproject.toml": '[project]\nname = "refused"\n',
    "client.py": d("""
        import requests


        def scripted():
            return requests.get("http://scripted:5000/orders/1", timeout=5)


        def missing():
            return requests.get("http://missing:5000/orders/1", timeout=5)


        def cache():
            return requests.get("http://cache:6379/", timeout=5)


        def elsewhere():
            return requests.get("http://elsewhere:5000/orders/1", timeout=5)
    """),
}


def test_a_service_that_reaches_no_code_refuses_its_requests_at_that_hop(
    workspace: Workspace, tmp_path: Path
) -> None:
    built = build(workspace, tmp_path, {"shop": SHOP, "refused": REFUSED})
    outcome = {
        caller: [(decision, evidence.get("source_outcome"), evidence.get("identity_in"))]
        for caller, [(decision, evidence)] in decided(built, "refused").items()
    }
    assert outcome == {
        # the service resolves (name and container port), but its command is a script
        "scripted": [
            ("http-deployment-source-unresolved", COMMAND_UNREAD, "docker-compose.yml:14")
        ],
        # ... or its Dockerfile is not in the repository
        "missing": [
            ("http-deployment-source-unresolved", DOCKERFILE_UNREAD, "docker-compose.yml:19")
        ],
        # an image-only service with no port resolves to no origin at all
        "cache": [("http-service-unmatched", None, None)],
        "elsewhere": [("http-service-unmatched", None, None)],
    }
    [(_, evidence)] = decided(built, "refused")["scripted"]
    assert evidence == {
        "called_at": "client.py:5",
        "deployment": "http://scripted:5000",
        "identity_source": "docker-compose-service",
        "identity_in": "docker-compose.yml:14",
        "source_outcome": COMMAND_UNREAD,
        "provider": "shop",
    }
    assert {caller for caller, *_ in links(built)}.isdisjoint(outcome)
