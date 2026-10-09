# ruff: noqa: E501 - the sample configurations hold their lines as a CI system or Helm reads them
"""Invented repositories for DF2's artifact join (DF2_PHASE_A.md): a deployment repository
whose CI releases one chart several ways, the repositories whose CI builds and publishes
the images, and a consumer whose requests name the hosts.

``deployer``'s releases of ``charts/web`` (host ``{{ .Values.ingress.host }}``):

* ``api``        -- a referenced overlay (``env/test/api.yaml``) sets its host; its image is
  the one ``api``'s CircleCI build publishes: linked, ``unknown`` (``github.sha`` against
  ``CIRCLE_SHA1``), tied to ``api/app.py`` through ``deploy/api.Dockerfile`` (E10);
* ``other``      -- the same path at another registry: ``publisher-unresolved``;
* ``old``        -- ``report``'s image at another literal tag: ``tag-unmatched``;
* ``twin``       -- an image two repositories publish: ``artifact-ambiguous``;
* ``noreg``      -- a registry set from a variable: ``registry-unresolved``;
* ``canary``     -- an unread values path: established, never bound; its host is a hole.

and of ``charts/svc`` (host ``{{ .Values.svc }}.svc.example``): ``alpha`` renders
``alpha.svc.example``; ``maybe`` (an unread values path) leaves ``{svc}.svc.example``,
an anchored pattern of an established release that competes with ``alpha`` (E8).
"""

from __future__ import annotations

from textwrap import dedent

WEB_CHART = {
    "charts/web/Chart.yaml": 'apiVersion: v2\nname: web\nversion: 1.0.0\nappVersion: "1.0.0"\n',
    "charts/web/values.yaml": 'image:\n  repository: registry.example:5000/team/api\n  tag: ""\nport: 8080\nservice:\n  port: 80\ningress:\n  host: ""\n',
    "charts/web/templates/_helpers.tpl": dedent(
        """\
        {{- define "web.name" -}}
        {{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
        {{- end }}

        {{- define "web.fullname" -}}
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

        {{- define "web.selectorLabels" -}}
        app.kubernetes.io/name: {{ include "web.name" . }}
        app.kubernetes.io/instance: {{ .Release.Name }}
        {{- end }}
        """
    ),
    "charts/web/templates/deployment.yaml": dedent(
        """\
        apiVersion: apps/v1
        kind: Deployment
        metadata:
          name: {{ include "web.fullname" . }}
        spec:
          selector:
            matchLabels:
              {{- include "web.selectorLabels" . | nindent 6 }}
          template:
            metadata:
              labels:
                {{- include "web.selectorLabels" . | nindent 8 }}
            spec:
              containers:
                - name: {{ .Chart.Name }}
                  image: "{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}"
                  ports:
                    - name: http
                      containerPort: {{ .Values.port }}
        """
    ),
    "charts/web/templates/service.yaml": dedent(
        """\
        apiVersion: v1
        kind: Service
        metadata:
          name: {{ include "web.fullname" . }}
        spec:
          ports:
            - port: {{ .Values.service.port }}
              targetPort: http
              name: http
          selector:
            {{- include "web.selectorLabels" . | nindent 4 }}
        """
    ),
    "charts/web/templates/ingress.yaml": dedent(
        """\
        apiVersion: networking.k8s.io/v1
        kind: Ingress
        metadata:
          name: {{ include "web.fullname" . }}
          annotations:
            alb.ingress.kubernetes.io/listen-ports: '[{"HTTPS":443}]'
        spec:
          ingressClassName: alb
          rules:
            - host: "{{ .Values.ingress.host }}"
              http:
                paths:
                  - path: /*
                    pathType: ImplementationSpecific
                    backend:
                      service:
                        name: {{ include "web.fullname" . }}
                        port:
                          number: {{ .Values.service.port }}
        """
    ),
}

SVC_CHART = {
    path.replace("charts/web/", "charts/svc/"): text.replace("web.", "svc.").replace(
        "name: web\n", "name: svc\n"
    )
    for path, text in WEB_CHART.items()
}
SVC_CHART["charts/svc/templates/ingress.yaml"] = SVC_CHART[
    "charts/svc/templates/ingress.yaml"
].replace('"{{ .Values.ingress.host }}"', '"{{ .Values.svc }}.svc.example"')

DEPLOY = """\
name: deploy
on:
  push:
    branches: [main]
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: helm upgrade --install api charts/web -f charts/web/values.yaml -f env/test/api.yaml --set image.tag=${{ github.sha }}
      - run: helm upgrade --install other charts/web -f env/test/values-other.yaml
      - run: helm upgrade --install old charts/web -f env/test/values-old.yaml
      - run: helm upgrade --install twin charts/web -f env/test/values-twin.yaml --set image.tag=${{ github.sha }}
      - run: helm upgrade --install noreg charts/web -f env/test/values-noreg.yaml --set image.repository=$REGISTRY/team/api
      - run: helm upgrade --install canary charts/web -f env/${{ vars.CANARY }}/api.yaml
      - run: helm upgrade --install alpha charts/svc -f env/test/values-alpha.yaml --set image.tag=${{ github.sha }}
      - run: helm upgrade --install maybe charts/svc -f env/${{ vars.MAYBE }}/svc.yaml
"""

DEPLOYER = {
    **WEB_CHART,
    **SVC_CHART,
    ".github/workflows/deploy.yml": DEPLOY,
    "env/test/api.yaml": "ingress:\n  host: api.test.example\n",
    "env/test/values-other.yaml": "image:\n  repository: registry-two.example:5000/team/api\ningress:\n  host: other.test.example\n",
    "env/test/values-old.yaml": 'image:\n  repository: registry.example:5000/team/report\n  tag: "2.0.0"\ningress:\n  host: old.test.example\n',
    "env/test/values-twin.yaml": "image:\n  repository: registry.example:5000/team/twin\ningress:\n  host: twin.test.example\n",
    "env/test/values-noreg.yaml": "ingress:\n  host: noreg.test.example\n",
    "env/test/values-alpha.yaml": "svc: alpha\n",
}

API_CIRCLE = """\
version: 2.1
jobs:
  build:
    docker:
      - image: cimg/base:2024.01
    steps:
      - checkout
      - run: docker build -t registry.example:5000/team/api:$CIRCLE_SHA1 -f deploy/api.Dockerfile .
      - run: docker push registry.example:5000/team/api:$CIRCLE_SHA1
      - run: docker build -t registry.example:5000/team/twin:$CIRCLE_SHA1 -f deploy/api.Dockerfile .
      - run: docker push registry.example:5000/team/twin:$CIRCLE_SHA1
workflows:
  build:
    jobs:
      - build
"""

API_APP = """\
from fastapi import FastAPI

app = FastAPI()


@app.get("/v1/items/{item_id}")
def get_item(item_id: str) -> dict:
    return {"id": item_id}
"""

API = {
    ".circleci/config.yml": API_CIRCLE,
    "deploy/api.Dockerfile": 'FROM python:3.12-slim\nWORKDIR /app\nCOPY api /app/api\nCMD ["uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8080"]\n',
    "api/__init__.py": "",
    "api/app.py": API_APP,
}

REPORT_CIRCLE = """\
version: 2.1
jobs:
  build:
    docker:
      - image: cimg/base:2024.01
    steps:
      - checkout
      - run: docker build -t registry.example:5000/team/report:1.0.0 .
      - run: docker push registry.example:5000/team/report:1.0.0
workflows:
  build:
    jobs:
      - build
"""

REPORT = {
    ".circleci/config.yml": REPORT_CIRCLE,
    "Dockerfile": 'FROM python:3.12-slim\nCMD ["python", "-m", "report"]\n',
}

ALPHA_IMAGE = """\
name: image
on:
  push:
    branches: [main]
jobs:
  image:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: docker build -t registry.example:5000/team/api-svc:${{ github.sha }} -f deploy/api.Dockerfile . && docker push registry.example:5000/team/api-svc:${{ github.sha }}
"""

CLIENT = """\
import requests

API_URL = "https://api.test.example"
OTHER_URL = "https://other.test.example"
OLD_URL = "https://old.test.example"
TWIN_URL = "https://twin.test.example"
NOREG_URL = "https://noreg.test.example"
ALPHA_URL = "https://alpha.svc.example"


def fetch_item(item_id: str) -> dict:
    return requests.get(f"{API_URL}/v1/items/{item_id}", timeout=5).json()


def fetch_other(item_id: str) -> dict:
    return requests.get(f"{OTHER_URL}/v1/items/{item_id}", timeout=5).json()


def fetch_old(item_id: str) -> dict:
    return requests.get(f"{OLD_URL}/v1/items/{item_id}", timeout=5).json()


def fetch_twin(item_id: str) -> dict:
    return requests.get(f"{TWIN_URL}/v1/items/{item_id}", timeout=5).json()


def fetch_noreg(item_id: str) -> dict:
    return requests.get(f"{NOREG_URL}/v1/items/{item_id}", timeout=5).json()


def fetch_alpha(item_id: str) -> dict:
    return requests.get(f"{ALPHA_URL}/v1/items/{item_id}", timeout=5).json()
"""


def repos() -> dict[str, dict[str, str]]:
    """The corpus: ``deployer``, ``api`` (builds api and twin, and api-svc for alpha),
    ``report`` (builds report:1.0.0 and twin), and ``client``."""
    api = {**API, ".github/workflows/image.yml": ALPHA_IMAGE}
    report = {
        **REPORT,
        ".github/workflows/twin.yml": ALPHA_IMAGE.replace("team/api-svc:", "team/twin:").replace(
            "-f deploy/api.Dockerfile", "-f Dockerfile"
        ),
    }
    deployer = {
        **DEPLOYER,
        "env/test/values-alpha.yaml": "svc: alpha\nimage:\n  repository: registry.example:5000/team/api-svc\n",
    }
    return {
        "deployer": deployer,
        "api": api,
        "report": report,
        "client": {"client/__init__.py": "", "client/calls.py": CLIENT},
    }
