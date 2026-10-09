# ruff: noqa: E501 - the sample templates hold their lines as Helm reads them
"""Which workload container a CI-bound Service reaches, and when M3 still ties a chart
(DF2_PHASE_A.md §3, §15 E2, E5, E9).

``sel`` serves ``GET /v1/items/{item_id}`` from ``app.py``, builds and publishes its image,
and releases one generated chart per case; ``client`` requests each case's host. A case
whose Service reaches exactly one container links; one that does not is refused at the
source hop (``workload_unmatched``), never tied by guess.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build

HELPERS = """\
{{- define "%(n)s.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- define "%(n)s.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%%s-%%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}
{{- define "%(n)s.selectorLabels" -}}
app.kubernetes.io/name: {{ include "%(n)s.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
"""

INGRESS = """\
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {{ include "%(n)s.fullname" . }}
  annotations:
    alb.ingress.kubernetes.io/listen-ports: '[{"HTTPS":443}]'
spec:
  ingressClassName: alb
  rules:
    - host: "{{ .Values.host }}"
      http:
        paths:
          - path: /*
            pathType: ImplementationSpecific
            backend:
              service:
                name: {{ include "%(n)s.fullname" . }}
                port:
                  number: 80
"""

SERVICE = """\
apiVersion: v1
kind: Service
metadata:
  name: {{ include "%(n)s.fullname" . }}
spec:
  ports:
    - port: 80
      targetPort: %(target)s
      name: http
  selector:
%(selector)s
"""

INCLUDE = '    {{- include "%(n)s.selectorLabels" . | nindent 4 }}'
LABELS = '        {{- include "%(n)s.selectorLabels" . | nindent 8 }}'


def container(
    name: str = "app", ports: str = "            - name: http\n              containerPort: 8080\n"
) -> str:
    return (
        f"        - name: {name}\n"
        '          image: "reg.example/s/api:{{ .Values.image.tag }}"\n'
        + ("          ports:\n" + ports if ports else "")
    )


def workload(
    n: str, suffix: str, labels: str, containers: str, wrapper: tuple[str, str] = ("", "")
) -> str:
    return (
        wrapper[0]
        + "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n"
        + f'  name: {{{{ include "{n}.fullname" . }}}}-{suffix}\n'
        + "spec:\n  template:\n    metadata:\n      labels:\n"
        + labels
        + "\n    spec:\n      containers:\n"
        + containers
        + wrapper[1]
    )


def chart(
    n: str, *, selector: str, target: str = "http", workloads: list[str] | None = None
) -> dict[str, str]:
    files = {
        f"charts/{n}/Chart.yaml": f'apiVersion: v2\nname: {n}\nversion: 1.0.0\nappVersion: "1.0.0"\n',
        f"charts/{n}/values.yaml": "image:\n  tag: ''\nhost: ''\n",
        f"charts/{n}/templates/_helpers.tpl": HELPERS % {"n": n},
        f"charts/{n}/templates/ingress.yaml": INGRESS % {"n": n},
        f"charts/{n}/templates/service.yaml": SERVICE
        % {"n": n, "target": target, "selector": selector % {"n": n}},
    }
    for index, text in enumerate(workloads or []):
        files[f"charts/{n}/templates/workload-{index}.yaml"] = text
    return files


def cases() -> dict[str, dict[str, str]]:
    component = INCLUDE + "\n    app.kubernetes.io/component: api"
    return {
        # one of two workloads, by a literal label the other contradicts
        "two": chart(
            "two",
            selector=component,
            workloads=[
                workload(
                    "two",
                    "api",
                    LABELS % {"n": "two"} + "\n        app.kubernetes.io/component: api",
                    container(),
                ),
                workload(
                    "two",
                    "worker",
                    LABELS % {"n": "two"} + "\n        app.kubernetes.io/component: worker",
                    container("worker"),
                ),
            ],
        ),
        # the other neither has the label nor contradicts it: undecided
        "undecided": chart(
            "undecided",
            selector=component,
            workloads=[
                workload(
                    "undecided",
                    "api",
                    LABELS % {"n": "undecided"} + "\n        app.kubernetes.io/component: api",
                    container(),
                ),
                workload("undecided", "worker", LABELS % {"n": "undecided"}, container("worker")),
            ],
        ),
        # a named target port no container port has: the Service sends nowhere
        "noport": chart(
            "noport",
            selector=INCLUDE,
            target="grpc",
            workloads=[workload("noport", "api", LABELS % {"n": "noport"}, container())],
        ),
        # one container, a numbered target port, no port declared: it reaches that container
        "numbered": chart(
            "numbered",
            selector=INCLUDE,
            target="8080",
            workloads=[
                workload("numbered", "api", LABELS % {"n": "numbered"}, container(ports=""))
            ],
        ),
        # two containers declaring the numbered port: which one is not known
        "twins": chart(
            "twins",
            selector=INCLUDE,
            target="8080",
            workloads=[
                workload(
                    "twins", "api", LABELS % {"n": "twins"}, container() + container("sidecar")
                ),
            ],
        ),
        # a workload this does not read (a range around it): no M3 fallback either (E5)
        "unread": chart(
            "unread",
            selector=INCLUDE,
            workloads=[
                workload(
                    "unread",
                    "api",
                    LABELS % {"n": "unread"},
                    container(),
                    ("{{- range .Values.shards }}\n---\n", "{{- end }}\n"),
                ),
            ],
        ),
        # no workload at all: M3 ties the chart to its repository's one application (E5)
        "bare": chart("bare", selector=INCLUDE),
        # literal labels only, under a release name that is not resolved: selection holds (E9)
        "literal": chart(
            "literal",
            selector="    app: literal",
            workloads=[workload("literal", "api", "        app: literal", container())],
        ),
        # an include, under a release name that is not resolved: the helper may read it (E9)
        "nameless": chart(
            "nameless",
            selector=INCLUDE,
            workloads=[workload("nameless", "api", LABELS % {"n": "nameless"}, container())],
        ),
    }


def deploy_workflow() -> str:
    lines = [
        "name: deploy",
        "on:",
        "  push:",
        "    branches: [main]",
        "jobs:",
        "  deploy:",
        "    runs-on: ubuntu-latest",
        "    steps:",
    ]
    for name in cases():
        release = "${{ vars.NAME }}" if name in ("literal", "nameless") else name
        lines.append(
            f"      - run: helm upgrade --install {release} charts/{name} --set host={name}.sel.example --set image.tag=${{{{ github.sha }}}}"
        )
    return "\n".join(lines) + "\n"


IMAGE = """\
name: image
on:
  push:
    branches: [main]
jobs:
  image:
    runs-on: ubuntu-latest
    steps:
      - run: docker build -t reg.example/s/api:${{ github.sha }} . && docker push reg.example/s/api:${{ github.sha }}
"""

APP = """\
from fastapi import FastAPI

app = FastAPI()


@app.get("/v1/items/{item_id}")
def get_item(item_id: str) -> dict:
    return {"id": item_id}
"""


def client() -> str:
    names = list(cases())
    text = "import requests\n\n"
    for name in names:
        text += f'{name.upper()}_URL = "https://{name}.sel.example"\n'
    for name in names:
        text += (
            f"\n\ndef call_{name}(item_id: str) -> dict:\n"
            f'    return requests.get(f"{{{name.upper()}_URL}}/v1/items/{{item_id}}", timeout=5).json()\n'
        )
    return text


def repos() -> dict[str, dict[str, str]]:
    sel: dict[str, str] = {
        ".github/workflows/deploy.yml": deploy_workflow(),
        ".github/workflows/image.yml": IMAGE,
        "Dockerfile": 'FROM python:3.12-slim\nWORKDIR /app\nCOPY app.py /app/app.py\nCMD ["uvicorn", "app:app"]\n',
        "app.py": APP,
    }
    for files in cases().values():
        sel.update(files)
    return {"sel": sel, "client": {"client.py": client()}}


@pytest.fixture
def outcomes(
    workspace: Workspace, tmp_path: Path
) -> dict[str, tuple[str, dict[str, Any], dict[str, Any]]]:
    build(workspace, tmp_path, repos())
    found: dict[str, tuple[str, dict[str, Any], dict[str, Any]]] = {}
    for row in workspace.knowledge.list_http_call_decisions():
        name = str(
            workspace.database.execute(
                "SELECT qualified_name FROM entities WHERE id = ?", (row.src_id,)
            ).fetchone()[0]
        ).removeprefix("call_")
        source = next(h for h in json.loads(row.chain_json) if h["kind"] == "source_binding")
        found[name] = (row.decision, source, json.loads(row.evidence_json))
    return found


def test_a_literal_label_selects_one_workload_and_excludes_the_other(
    outcomes: dict[str, Any],
) -> None:
    decision, _, evidence = outcomes["two"]
    assert decision == "linked"
    assert evidence["workload_in"].startswith("charts/two/templates/workload-0.yaml:")


@pytest.mark.parametrize(
    "case",
    ["undecided", "noport", "twins", "unread", "nameless"],
)
def test_a_service_that_reaches_no_one_container_is_refused_at_the_source(
    outcomes: dict[str, Any], case: str
) -> None:
    decision, source, _ = outcomes[case]
    assert decision == "http-deployment-source-unresolved"
    assert (source["status"], source["reason"]) == ("unresolved", "workload_unmatched")


def test_one_container_and_a_numbered_target_port_link(outcomes: dict[str, Any]) -> None:
    assert outcomes["numbered"][0] == "linked"


def test_m3_ties_only_a_chart_stating_no_workload(outcomes: dict[str, Any]) -> None:
    decision, _, evidence = outcomes["bare"]
    assert decision == "linked"
    assert evidence["source_rule"] == "same-repository"
    assert "build_basis" not in evidence and "version_relation" not in evidence
    # an unread workload is workload evidence: no fallback (E5)
    assert outcomes["unread"][0] == "http-deployment-source-unresolved"


def test_an_unresolved_release_name_blocks_only_what_depends_on_it(
    outcomes: dict[str, Any],
) -> None:
    # the host reads only values: an identity; literal labels select without the name
    assert outcomes["literal"][0] == "linked"
    # an include may read the name: undecided, refused
    assert outcomes["nameless"][0] == "http-deployment-source-unresolved"
