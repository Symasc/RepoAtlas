# ruff: noqa: E501 - the sample configurations hold their lines as a CI system or Helm reads them
"""Invented repositories for DF3d's deployment-aware impact (DF3D_PHASE_A.md §7, §11).

``deployer`` releases ``charts/web`` three times and ``charts/pair`` twice, from GitHub Actions:

* ``app-a`` -- ``-f env/a.yaml`` (host ``a.example``, ``env.LIMIT``) and ``--set env.REGION=eu``;
* ``app-b`` -- ``-f env/b.yaml`` (host ``b.example``, its own ``env.LIMIT``); the chart's
  default ``env.REGION`` is effective for it alone;
* ``svc``   -- ``svc``'s image (host ``svc.example``);
* ``pair-a``, ``pair-b`` -- one chart, two workloads (``api``, ``admin``), the Service selecting
  ``component: {{ .Values.component }}``: ``pair-a`` selects ``api``, ``pair-b`` ``admin``.
  ``shared`` is in both containers' environment, ``apiOnly`` in ``api``'s alone.

The shared ConfigMap key ``SORT`` (``charts/web/templates/web-config.yaml``) is effective in
``app-a`` and ``app-b`` alike.

``app`` builds ``team/app``; its ``app/settings.py`` reads every variable, its one FastAPI
application serves ``get_item`` (``/v1/items/{item_id}``, using them), ``get_summary``
(``/v1/summary``, through ``svc``), ``get_cycle`` (``/v1/cycle``, through ``ypkg``) and
``get_limits`` (``/v1/limits``), which its own ``smoke_b`` requests at ``b.example``. A Salt state
sets REGION with no deployment evidence. Its compose service (``http://app:8080``) sets ``CACHE``; its Tilt-bound ``dev`` chart (``dev.example``) states no workload, so its
link records no release. ``ypkg`` depends on ``app`` and ``app`` on it: a package cycle. ``svc``
serves ``get_answer``, which requests ``a.example``. ``consumer`` calls every host, and
``call_either`` one of two literal bases.
"""

from __future__ import annotations

from textwrap import dedent

from tests.artifact_corpus import WEB_CHART


def _with(template: str, after: str, added: str) -> str:
    """``template`` with ``added`` after its one line ``after``."""
    assert template.count(after) == 1, after
    return template.replace(after, after + added)


#: ``charts/web`` with an environment: two values keys and a ConfigMap key.
WEB = {
    **WEB_CHART,
    "charts/web/values.yaml": dedent(
        """\
        image:
          repository: registry.example:5000/team/app
          tag: ""
        port: 8080
        service:
          port: 80
        ingress:
          host: ""
        env:
          LIMIT: "10"
          REGION: "us"
        """
    ),
    "charts/web/templates/deployment.yaml": _with(
        WEB_CHART["charts/web/templates/deployment.yaml"],
        "              containerPort: {{ .Values.port }}\n",
        "          env:\n"
        "            - name: LIMIT\n"
        "              value: {{ .Values.env.LIMIT | quote }}\n"
        "            - name: REGION\n"
        "              value: {{ .Values.env.REGION | quote }}\n"
        "            - name: SORT\n"
        "              valueFrom:\n"
        "                configMapKeyRef:\n"
        "                  name: web-config\n"
        "                  key: SORT\n",
    ),
    "charts/web/templates/web-config.yaml": dedent(
        """\
        apiVersion: v1
        kind: ConfigMap
        metadata:
          name: web-config
        data:
          SORT: "asc"
        """
    ),
}


def _pair_workload(component: str, env: str) -> str:
    return (
        dedent(
            f"""\
        apiVersion: apps/v1
        kind: Deployment
        metadata:
          name: pair-{component}
        spec:
          selector:
            matchLabels:
              component: {component}
          template:
            metadata:
              labels:
                component: {component}
            spec:
              containers:
                - name: {component}
                  image: "{{{{ .Values.image.repository }}}}:{{{{ .Values.image.tag | default .Chart.AppVersion }}}}"
                  ports:
                    - name: http
                      containerPort: 8080
                  env:
        """
        )
        + env
    )


PAIR = {
    "charts/pair/Chart.yaml": 'apiVersion: v2\nname: pair\nversion: 1.0.0\nappVersion: "1.0.0"\n',
    "charts/pair/values.yaml": dedent(
        """\
        image:
          repository: registry.example:5000/team/app
          tag: ""
        component: api
        host: ""
        shared: "1"
        apiOnly: "2"
        """
    ),
    "charts/pair/templates/api.yaml": _pair_workload(
        "api",
        "            - name: SHARED\n              value: {{ .Values.shared | quote }}\n"
        "            - name: API_ONLY\n              value: {{ .Values.apiOnly | quote }}\n",
    ),
    "charts/pair/templates/admin.yaml": _pair_workload(
        "admin", "            - name: SHARED\n              value: {{ .Values.shared | quote }}\n"
    ),
    "charts/pair/templates/service.yaml": dedent(
        """\
        apiVersion: v1
        kind: Service
        metadata:
          name: pair
        spec:
          ports:
            - port: 80
              targetPort: http
              name: http
          selector:
            component: {{ .Values.component }}
        """
    ),
    "charts/pair/templates/ingress.yaml": dedent(
        """\
        apiVersion: networking.k8s.io/v1
        kind: Ingress
        metadata:
          name: pair
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
                        name: pair
                        port:
                          number: 80
        """
    ),
}

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
      - run: helm upgrade --install app-a charts/web -f charts/web/values.yaml -f env/a.yaml --set image.tag=${{ github.sha }} --set env.REGION=eu
      - run: helm upgrade --install app-b charts/web -f charts/web/values.yaml -f env/b.yaml --set image.tag=${{ github.sha }}
      - run: helm upgrade --install svc charts/web -f charts/web/values.yaml -f env/svc.yaml --set image.tag=${{ github.sha }}
      - run: helm upgrade --install pair-a charts/pair -f charts/pair/values.yaml -f env/pair-a.yaml --set image.tag=${{ github.sha }}
      - run: helm upgrade --install pair-b charts/pair -f charts/pair/values.yaml -f env/pair-b.yaml --set image.tag=${{ github.sha }}
"""

DEPLOYER = {
    **WEB,
    **PAIR,
    ".github/workflows/deploy.yml": DEPLOY,
    "env/a.yaml": 'ingress:\n  host: a.example\nenv:\n  LIMIT: "20"\n',
    "env/b.yaml": 'ingress:\n  host: b.example\nenv:\n  LIMIT: "30"\n',
    "env/svc.yaml": "image:\n  repository: registry.example:5000/team/svc\ningress:\n  host: svc.example\n",
    "env/pair-a.yaml": "component: api\nhost: pair-a.example\n",
    "env/pair-b.yaml": "component: admin\nhost: pair-b.example\n",
}


def _circle(image: str, dockerfile: str) -> str:
    return dedent(
        f"""\
        version: 2.1
        jobs:
          build:
            docker:
              - image: cimg/base:2024.01
            steps:
              - checkout
              - run: docker build -t registry.example:5000/team/{image}:$CIRCLE_SHA1 -f {dockerfile} .
              - run: docker push registry.example:5000/team/{image}:$CIRCLE_SHA1
        workflows:
          build:
            jobs:
              - build
        """
    )


SETTINGS = """\
import os

LIMIT = int(os.getenv("LIMIT", "1"))
REGION = os.getenv("REGION", "us")
SORT = os.getenv("SORT", "asc")
SHARED = os.getenv("SHARED", "0")
API_ONLY = os.getenv("API_ONLY", "0")
CACHE = int(os.getenv("CACHE", "0"))
MAX = 50
"""

MAIN = """\
from fastapi import FastAPI
from ypkg.helper import doubled

from app.settings import API_ONLY, CACHE, LIMIT, MAX, REGION, SHARED, SORT
from app.svc_client import ask_svc

app = FastAPI()


@app.get("/v1/items/{item_id}")
def get_item(item_id: str) -> dict:
    return {
        "id": item_id,
        "limit": min(LIMIT, MAX),
        "region": REGION,
        "sort": SORT,
        "shared": SHARED,
        "api": API_ONLY,
        "cache": CACHE,
    }


@app.get("/v1/summary")
def get_summary() -> dict:
    return {"answer": ask_svc()}


@app.get("/v1/cycle")
def get_cycle() -> dict:
    return {"doubled": doubled()}


@app.get("/v1/limits")
def get_limits() -> dict:
    return {"limit": LIMIT}
"""

#: ``ask_svc`` reads REGION, so app-a's ``--set`` reaches it directly ({app-a}) and again,
#: unconstrained, after the round trip through ``svc``.
SVC_CLIENT = """\
import requests

from app.settings import REGION

SVC_URL = "https://svc.example"


def ask_svc() -> dict:
    return requests.get(f"{SVC_URL}/v1/answers/1", params={"region": REGION}, timeout=5).json()
"""

#: app's own smoke check of app-b's ``/v1/limits``: linked to get_limits through app-b, and -- a
#: literal URL, in the route's own repository -- matched to the route by its path, which states
#: no deployment.
SMOKE = """\
import requests


def smoke_b() -> dict:
    return requests.get("https://b.example/v1/limits", timeout=5).json()
"""

#: A dev release of ``app``'s own chart, bound by Tilt: no workload, so the M3 tie and no
#: release recorded on the link.
DEV_CHART = {
    path.replace("charts/web/", "charts/dev/"): text.replace("web.", "dev.").replace(
        "name: web\n", "name: dev\n"
    )
    for path, text in WEB_CHART.items()
    if not path.endswith("deployment.yaml")
}

APP = {
    "pyproject.toml": '[project]\nname = "app"\nversion = "1.0.0"\ndependencies = ["fastapi", "requests", "ypkg"]\n',
    ".circleci/config.yml": _circle("app", "deploy/app.Dockerfile"),
    "deploy/app.Dockerfile": 'FROM python:3.12-slim\nWORKDIR /app\nCOPY app /app/app\nCMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]\n',
    "docker-compose.yml": 'services:\n  app:\n    build:\n      context: .\n      dockerfile: deploy/app.Dockerfile\n    ports:\n      - "8080:8080"\n    environment:\n      CACHE: "5"\n',
    "app/__init__.py": "",
    "app/settings.py": SETTINGS,
    "app/main.py": MAIN,
    "app/svc_client.py": SVC_CLIENT,
    "app/smoke.py": SMOKE,
    # a Salt state's context sets REGION: configuration with no deployment evidence
    "salt/app.sls": "app_config:\n  file.managed:\n    - name: /etc/app.conf\n    - context:\n        REGION: 'eu'\n",
    **DEV_CHART,
    "Tiltfile": "k8s_yaml(helm('charts/dev', name='dev', set=['ingress.host=dev.example']))\n",
}

YPKG = {
    "pyproject.toml": '[project]\nname = "ypkg"\nversion = "1.0.0"\ndependencies = ["app"]\n',
    "ypkg/__init__.py": "",
    "ypkg/helper.py": "from app.settings import LIMIT\n\n\ndef doubled() -> int:\n    return LIMIT * 2\n",
}

SVC_MAIN = """\
from fastapi import FastAPI

from svc.client import fetch_item

app = FastAPI()


@app.get("/v1/answers/{n}")
def get_answer(n: str) -> dict:
    return {"item": fetch_item(n)}
"""

SVC = {
    ".circleci/config.yml": _circle("svc", "deploy/svc.Dockerfile"),
    "deploy/svc.Dockerfile": 'FROM python:3.12-slim\nWORKDIR /app\nCOPY svc /app/svc\nCMD ["uvicorn", "svc.main:app", "--host", "0.0.0.0", "--port", "8080"]\n',
    "svc/__init__.py": "",
    "svc/main.py": SVC_MAIN,
    "svc/client.py": 'import requests\n\nAPP_URL = "https://a.example"\n\n\ndef fetch_item(n: str) -> dict:\n    return requests.get(f"{APP_URL}/v1/items/{n}", timeout=5).json()\n',
}

CLIENT = """\
import os

import requests

A_URL = "https://a.example"
B_URL = "https://b.example"
DEV_URL = "https://dev.example"
PAIR_A_URL = "https://pair-a.example"
PAIR_B_URL = "https://pair-b.example"
COMPOSE_URL = "http://app:8080"
if os.getenv("STAGE") == "a":
    EITHER_URL = "https://a.example"
else:
    EITHER_URL = "https://b.example"


def call_a(item_id: str) -> dict:
    return requests.get(f"{A_URL}/v1/items/{item_id}", timeout=5).json()


def call_b(item_id: str) -> dict:
    return requests.get(f"{B_URL}/v1/items/{item_id}", timeout=5).json()


def call_summary_b() -> dict:
    return requests.get(f"{B_URL}/v1/summary", timeout=5).json()


def call_cycle_a() -> dict:
    return requests.get(f"{A_URL}/v1/cycle", timeout=5).json()


def call_cycle_b() -> dict:
    return requests.get(f"{B_URL}/v1/cycle", timeout=5).json()


def call_dev(item_id: str) -> dict:
    return requests.get(f"{DEV_URL}/v1/items/{item_id}", timeout=5).json()


def call_pair_a(item_id: str) -> dict:
    return requests.get(f"{PAIR_A_URL}/v1/items/{item_id}", timeout=5).json()


def call_pair_b(item_id: str) -> dict:
    return requests.get(f"{PAIR_B_URL}/v1/items/{item_id}", timeout=5).json()


def call_either(item_id: str) -> dict:
    return requests.get(f"{EITHER_URL}/v1/items/{item_id}", timeout=5).json()


def call_compose(item_id: str) -> dict:
    return requests.get(f"{COMPOSE_URL}/v1/items/{item_id}", timeout=5).json()
"""

CONSUMER = {"client.py": CLIENT}


def repos() -> dict[str, dict[str, str]]:
    """The corpus: ``deployer``, ``app``, ``ypkg``, ``svc`` and ``consumer``."""
    return {"deployer": DEPLOYER, "app": APP, "ypkg": YPKG, "svc": SVC, "consumer": CONSUMER}
