"""Kubernetes Services from Helm charts, tied to code (deployment HTTP roadmap, M3, decision D4).

A chart's Service is reached at ``http://<name>`` (port 80) or ``http://<name>:<port>``.
Its name is usually ``{{ include "<chart>.fullname" . }}``: read only when the chart's
helper is the ``helm create`` scaffold, with a release name a Tiltfile binds literally.
The identity exposes the routes of its repository's one application.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path, PurePosixPath

import pytest
import yaml
from repoatlas.graph.deploy_identity import (
    CONTROLLED,
    NAME_UNREAD,
    NOT_ONE_APPLICATION,
    NOTHING_SERVED,
    RELEASE_UNBOUND,
    TIED,
    helm_fullname,
    helm_name,
)
from repoatlas.graph.linker import ReferenceLinker
from repoatlas.parsers.deploy_helm import (
    HelmHelpersParser,
    TiltfileParser,
    chart_references,
    scaffold_helpers,
    service_references,
    values_references,
)
from repoatlas.parsers.http_facts import HELM_SERVICE, PENDING_FACT
from repoatlas.services.workspace import Workspace
from tests.http_crossing_corpus import crossings, decided, links
from tests.impact_corpus import build
from tests.incremental_corpus import assert_equals_fresh, edit, sync_all


def d(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def _documents(text: str) -> list[yaml.Node]:
    return list(yaml.compose_all(d(text), Loader=yaml.SafeLoader))


def _meta(references: list) -> list[dict[str, object]]:
    return [
        {"key": r.target_key, "line": r.line_start}
        | {k: v for k, v in r.meta.items() if k != PENDING_FACT}
        for r in references
    ]


HELPERS = d("""
    {{/*
    Expand the name of the chart.
    */}}
    {{- define "devices.name" -}}
    {{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
    {{- end }}

    {{/*
    Create a default fully qualified app name.
    */}}
    {{- define "devices.fullname" -}}
    {{- if .Values.fullnameOverride }}
    {{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
    {{- else }}
    {{- $name := default .Chart.Name .Values.nameOverride }}
    {{- if contains $name .Release.Name }}
    {{- .Release.Name | trunc 63 | trimSuffix "-" }}
    {{- else }}
    {{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
    {{- end }}
    {{- end }}
    {{- end }}

    {{- define "devices.labels" -}}
    app: {{ include "devices.name" . }}
    {{- end }}
""")

SERVICE = d("""
    apiVersion: v1
    kind: Service
    metadata:
      name: {{ include "devices.fullname" . }}
      labels:
        {{- include "devices.labels" . | nindent 4 }}
    spec:
      type: ClusterIP
      ports:
        - port: {{ .Values.service.port }}
          targetPort: 8080
          name: http
""")


# -- what each file states -------------------------------------------------------------


def test_a_chart_and_its_values() -> None:
    chart = chart_references(
        PurePosixPath("charts/devices/Chart.yaml"), _documents("apiVersion: v2\nname: devices\n")
    )
    assert _meta(chart) == [{"key": "devices", "line": 2, "chart": "charts/devices"}]
    assert chart_references(PurePosixPath("Chart.yaml"), _documents("name: x\n")) == []
    values = values_references(
        PurePosixPath("charts/devices/values.yaml"),
        _documents(
            "service:\n  port: 80\n  type: ClusterIP\nnameOverride: ''\nlist: [1, 2]\n"
            "ingress:\n  groups:\n    - name: internal\n      host: int.example.com\n"
            "    - name: public\n"
        ),
    )
    [found] = _meta(values)
    # A list's items by index, as a `range` iterates them.
    assert json.loads(str(found["values"])) == {
        "nameOverride": "",
        "service.port": "80",
        "service.type": "ClusterIP",
        "list[0]": "1",
        "list[1]": "2",
        "ingress.groups[0].name": "internal",
        "ingress.groups[0].host": "int.example.com",
        "ingress.groups[1].name": "public",
    }


def test_a_service_template_records_its_name_and_ports_as_written() -> None:
    [service] = _meta(
        service_references(PurePosixPath("charts/devices/templates/service.yaml"), SERVICE)
    )
    assert service == {
        "key": "Service",
        "line": 4,
        "name": '{{ include "devices.fullname" . }}',
        "ports": json.dumps([["{{ .Values.service.port }}", 10]]),
        "chart": "charts/devices",
    }


def test_ports_listed_at_the_key_s_own_indentation_are_read() -> None:
    template = (
        "kind: Service\nmetadata:\n  name: api\n"
        "spec:\n  ports:\n  - port: 8080\n    targetPort: 80\n"
    )
    [service] = _meta(service_references(PurePosixPath("c/templates/svc.yaml"), template))
    assert service["ports"] == json.dumps([["8080", 6]])


def test_a_conditional_service_is_marked_and_other_documents_are_skipped() -> None:
    template = (
        "{{- if .Values.admin.enabled }}\nkind: Service\nmetadata:\n  name: admin\n"
        "spec:\n  ports:\n    - port: 80\n{{- end }}\n---\nkind: Deployment\nmetadata:\n  name: x\n"
    )
    [service] = _meta(service_references(PurePosixPath("c/templates/admin.yaml"), template))
    assert (service["name"], service["controlled"]) == ("admin", "true")
    assert service_references(PurePosixPath("c/manifests/svc.yaml"), SERVICE) == []


def test_only_the_scaffold_s_helpers_are_recognised() -> None:
    assert scaffold_helpers(HELPERS) == [("devices.name", 4), ("devices.fullname", 11)]
    changed = HELPERS.replace('printf "%s-%s"', 'printf "%s--%s"')
    assert scaffold_helpers(changed) == [("devices.name", 4)]
    padded = HELPERS.replace('{{- define "devices.name" -}}', '{{- define "devices.name" -}}\nx-')
    assert [name for name, _ in scaffold_helpers(padded)] == ["devices.fullname"]
    parsed = HelmHelpersParser().parse(Path("charts/devices/templates/_helpers.tpl"), HELPERS)
    assert {r.target_key: r.meta["chart"] for r in parsed.references} == {
        "devices.name": "charts/devices",
        "devices.fullname": "charts/devices",
    }


def test_a_tiltfile_binds_a_release_name_literally() -> None:
    source = d("""
        docker_build('devices', '.')
        k8s_yaml(helm('charts/devices', name='devices', set=['stage=dev']))
        k8s_yaml(helm('charts/devices', name='edge', values=['edge.yaml']))
        k8s_yaml(helm('charts/devices'))
        k8s_yaml(helm('charts/devices', name=NAME))
        k8s_yaml(helm('../elsewhere', name='x'))
        k8s_yaml(helm('charts/devices', name='computed', set=[setting]))
    """)
    parsed = TiltfileParser().parse(Path("deploy/Tiltfile"), source)
    assert [
        (r.target_key, r.line_start, {k: v for k, v in r.meta.items() if k != PENDING_FACT})
        for r in parsed.references
    ] == [
        ("devices", 2, {"chart": "deploy/charts/devices", "set": '["stage=dev"]'}),
        ("edge", 3, {"chart": "deploy/charts/devices", "values_files": "true"}),
        ("x", 6, {"chart": "elsewhere"}),
        ("computed", 7, {"chart": "deploy/charts/devices", "set": "null"}),
    ]


@pytest.mark.parametrize(
    ("release", "values", "fullname", "name"),
    [
        ("devices", {}, "devices", "devices"),
        ("devices-dev", {}, "devices-dev", "devices"),
        ("prod", {}, "prod-devices", "devices"),
        ("prod", {"nameOverride": "api"}, "prod-api", "api"),
        ("prod", {"fullnameOverride": "edge-api"}, "edge-api", "devices"),
        ("prod", {"fullnameOverride": "", "nameOverride": ""}, "prod-devices", "devices"),
        ("r" * 70, {}, "r" * 63, "devices"),
        ("x" * 62 + "-", {}, "x" * 62, "devices"),
    ],
)
def test_the_scaffold_names_as_helm_documents_them(
    release: str, values: dict[str, str], fullname: str, name: str
) -> None:
    assert helm_fullname("devices", release, values) == fullname
    assert helm_name("devices", values) == name


# -- the join -------------------------------------------------------------------------------

CHART = {
    "charts/devices/Chart.yaml": "apiVersion: v2\nname: devices\nversion: 1.0.0\n",
    "charts/devices/values.yaml": "service:\n  port: 80\nnameOverride: ''\n",
    "charts/devices/templates/_helpers.tpl": HELPERS,
    "charts/devices/templates/service.yaml": SERVICE,
    "charts/devices/templates/admin.yaml": (
        "{{- if .Values.admin }}\nkind: Service\nmetadata:\n  name: devices-admin\n"
        "spec:\n  ports:\n    - port: 80\n{{- end }}\n"
    ),
    "charts/devices/templates/custom.yaml": (
        'kind: Service\nmetadata:\n  name: {{ include "devices.custom" . }}\n'
        "spec:\n  ports:\n    - port: 80\n"
    ),
    "charts/devices/templates/release.yaml": (
        "kind: Service\nmetadata:\n  name: {{ .Release.Name }}\nspec:\n  ports:\n    - port: 80\n"
    ),
    "charts/unbound/Chart.yaml": "apiVersion: v2\nname: unbound\n",
    "charts/unbound/templates/service.yaml": (
        "kind: Service\nmetadata:\n  name: unbound\nspec:\n  ports:\n    - port: 8080\n"
    ),
    "Tiltfile": d("""
        docker_build('devices', '.')
        k8s_yaml(helm('charts/devices', name='devices', set=['stage=dev']))
        k8s_yaml(helm('charts/devices', name='edge', set=['fullnameOverride=devices-edge']))
        k8s_yaml(helm('charts/devices', name='staging', values=['staging.yaml']))
    """),
}

DEVICES = {
    "pyproject.toml": '[project]\nname = "devices"\n',
    "app.py": d("""
        from fastapi import FastAPI

        app = FastAPI()


        @app.get("/items/{item_id}")
        def get_item(item_id: str):
            return {}


        @app.get("/health")
        def health():
            return {}
    """),
    # A specification document serves the same routes; it is not a second application.
    "openapi.yaml": d("""
        openapi: 3.0.3
        info:
          title: devices
          version: "1"
        paths:
          /items/{item_id}:
            get:
              responses:
                "200":
                  description: ok
    """),
    **CHART,
}

PORTAL = {
    "pyproject.toml": '[project]\nname = "portal"\n',
    "client.py": d("""
        import requests

        DEVICES = "http://devices"


        def load_item(item_id):
            return requests.get(f"{DEVICES}/items/{item_id}", timeout=5)


        def edge_health():
            return requests.get("http://devices-edge:80/health", timeout=5)
    """),
}

_APP = (
    "from fastapi import FastAPI\n\napp = FastAPI()\n\n\n"
    "@app.get('{route}')\ndef f():\n    return 1\n"
)

TWO_APPS = {
    "pyproject.toml": '[project]\nname = "twoapps"\n',
    "public.py": _APP.format(route="/a"),
    "admin.py": _APP.format(route="/b"),
    **{
        path.replace("devices", "twoapps"): text.replace("devices", "twoapps")
        for path, text in CHART.items()
    },
}


# One traced application beside a NestJS controller, which names no constructing module.
MIXED = {
    "pyproject.toml": '[project]\nname = "mixed"\n',
    "package.json": '{"name": "mixed", "dependencies": {"@nestjs/core": "10.0.0"}}\n',
    "public.py": _APP.format(route="/a"),
    "src/main.ts": d("""
        import { NestFactory } from "@nestjs/core";
        import { AppModule } from "./app.module";

        async function bootstrap() {
          const app = await NestFactory.create(AppModule);
          await app.listen(3000);
        }
    """),
    "src/app.module.ts": d("""
        import { Module } from "@nestjs/common";
        import { StatusController } from "./status.controller";

        @Module({ controllers: [StatusController] })
        export class AppModule {}
    """),
    "src/status.controller.ts": d("""
        import { Controller, Get } from "@nestjs/common";

        @Controller("status")
        export class StatusController {
          @Get()
          status() {}
        }
    """),
    **{
        path.replace("devices", "mixed"): text.replace("devices", "mixed")
        for path, text in CHART.items()
    },
}


@pytest.fixture
def cluster(workspace: Workspace, tmp_path: Path) -> Workspace:
    return build(
        workspace,
        tmp_path,
        {"devices": DEVICES, "portal": PORTAL, "twoapps": TWO_APPS, "mixed": MIXED},
    )


def test_a_service_s_identity_reaches_its_repository_s_one_application(cluster: Workspace) -> None:
    assert links(cluster) == {
        ("load_item", "devices", "get_item", "GET http://devices/items/{item_id}"),
        ("edge_health", "devices", "health", "GET http://devices-edge:80/health"),
    }
    evidence = {
        row["caller"]: {
            k: row["resolution"]["http"][k] for k in ("identity_source", "identity_in", "bound_in")
        }
        for row in crossings(cluster)
    }
    assert evidence == {
        "load_item": {
            "identity_source": "kubernetes-service",
            "identity_in": "charts/devices/templates/service.yaml:4",
            "bound_in": "Tiltfile:2",
        },
        "edge_health": {
            "identity_source": "kubernetes-service",
            "identity_in": "charts/devices/templates/service.yaml:4",
            "bound_in": "Tiltfile:3",
        },
    }


def _outcomes(workspace: Workspace) -> dict[tuple[str, str], str]:
    plan = ReferenceLinker(workspace.knowledge).prepare_global().http
    rows = workspace.database.execute(
        """
        SELECT u.id, r.slug, f.rel_path FROM unresolved_refs u
        JOIN files f ON f.id = u.file_id JOIN repositories r ON r.id = u.repo_id
        WHERE u.meta_json LIKE ?
        """,
        (f'%"{HELM_SERVICE}"%',),
    ).fetchall()
    return {(row["slug"], row["rel_path"]): plan.deployments[row["id"]] for row in rows}


def test_each_service_is_tied_or_says_why_not(cluster: Workspace) -> None:
    outcomes = _outcomes(cluster)
    assert {k: v for k, v in outcomes.items() if k[0] == "devices"} == {
        ("devices", "charts/devices/templates/service.yaml"): TIED,
        ("devices", "charts/devices/templates/admin.yaml"): CONTROLLED,
        ("devices", "charts/devices/templates/custom.yaml"): NAME_UNREAD,
        ("devices", "charts/devices/templates/release.yaml"): NAME_UNREAD,
        ("devices", "charts/unbound/templates/service.yaml"): RELEASE_UNBOUND,
    }
    assert outcomes[("twoapps", "charts/twoapps/templates/service.yaml")] == NOT_ONE_APPLICATION
    assert outcomes[("mixed", "charts/mixed/templates/service.yaml")] == NOT_ONE_APPLICATION


def test_a_specification_document_is_not_a_second_application(cluster: Workspace) -> None:
    served = cluster.database.execute(
        """
        SELECT COUNT(*) FROM edges e JOIN files f ON f.id = e.file_id
        WHERE e.type = 'SERVES_HTTP_ROUTE' AND f.rel_path = 'openapi.yaml'
        """
    ).fetchone()[0]
    assert served > 0
    assert _outcomes(cluster)[("devices", "charts/devices/templates/service.yaml")] == TIED


def test_only_literal_releases_without_values_files_give_identities(cluster: Workspace) -> None:
    plan = ReferenceLinker(cluster.knowledge).prepare_global().http
    assert {json.loads(e.meta_json or "{}")["deployment"] for e in plan.exposed} == {
        "http://devices",
        "http://devices:80",
        "http://devices-edge",
        "http://devices-edge:80",
    }


def test_a_chart_in_a_repository_serving_nothing_has_no_routes(
    workspace: Workspace, tmp_path: Path
) -> None:
    build(
        workspace, tmp_path, {"deploy": {"pyproject.toml": '[project]\nname = "deploy"\n', **CHART}}
    )
    outcome = _outcomes(workspace)[("deploy", "charts/devices/templates/service.yaml")]
    assert outcome == NOTHING_SERVED


def test_editing_the_release_name_moves_the_identity(
    cluster: Workspace, settings, tmp_path: Path
) -> None:
    root = tmp_path / "repos"
    tiltfile = str(CHART["Tiltfile"]).replace("name='devices'", "name='prod'")
    edit(root, "devices", "Tiltfile", tiltfile)
    sync_all(cluster, root)
    # release `prod` does not contain the chart's name: the Service is `prod-devices`
    assert {caller for caller, *_ in links(cluster)} == {"edge_health"}
    assert_equals_fresh(cluster, settings, root, tmp_path, "release")


CALLER = {
    "pyproject.toml": '[project]\nname = "caller"\n',
    "client.py": d("""
        import requests


        def load_item(item_id):
            return requests.get(f"http://devices/items/{item_id}", timeout=5)
    """),
}


def test_a_bound_service_whose_repository_serves_nothing_refuses_at_the_source(
    workspace: Workspace, tmp_path: Path
) -> None:
    deploy = {"pyproject.toml": '[project]\nname = "deploy"\n', **CHART}
    built = build(workspace, tmp_path, {"deploy": deploy, "caller": CALLER})
    assert decided(built, "caller") == {
        "load_item": [
            (
                "http-deployment-source-unresolved",
                {
                    "called_at": "client.py:5",
                    "deployment": "http://devices",
                    "identity_source": "kubernetes-service",
                    "identity_in": "charts/devices/templates/service.yaml:4",
                    "bound_in": "Tiltfile:2",
                    "source_outcome": NOTHING_SERVED,
                    "provider": "deploy",
                },
            )
        ]
    }
    assert links(built) == set()


def test_an_unbound_release_resolves_no_identity_to_refuse_against(
    workspace: Workspace, tmp_path: Path
) -> None:
    unbound = {path: text for path, text in CHART.items() if path != "Tiltfile"}
    built = build(
        workspace,
        tmp_path,
        {"deploy": {"pyproject.toml": '[project]\nname = "deploy"\n', **unbound}, "caller": CALLER},
    )
    assert [d for d, _ in decided(built, "caller")["load_item"]] == ["http-service-unmatched"]


def test_an_identity_that_reaches_no_code_makes_a_shared_origin_ambiguous(
    workspace: Workspace, tmp_path: Path
) -> None:
    """A second chart binds the same Service names in a repository with no code: either
    could be what the request reaches, so neither link is made."""
    mirror = {"pyproject.toml": '[project]\nname = "mirror"\n', **CHART}
    built = build(workspace, tmp_path, {"devices": DEVICES, "portal": PORTAL, "mirror": mirror})
    assert links(built) == set()
    load = decided(built, "portal")["load_item"]
    assert [d for d, _ in load] == ["http-provider-ambiguous"]
    assert load[0][1]["provider"] == "devices; mirror"
    assert load[0][1]["source_outcome"] == NOTHING_SERVED
