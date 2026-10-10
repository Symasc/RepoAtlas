# ruff: noqa: E501 - the sample configurations hold their lines as a CI system, Helm or compose reads them
"""Invented repositories for DF3a's configuration crossings (DF3A_PHASE_A.md §3, §12).

``shipper`` releases ``charts/app`` twice and ``charts/worker`` once, from GitHub Actions:

* ``app`` (deploy.yml:10) -- ``-f charts/app/values.yaml -f env/prod.yaml`` and four ``--set``s;
  its container's ``env`` and ``envFrom`` hold one variable per precedence case, below;
* ``staged`` (staged.yml:9) -- on two branches, ``--set env.STAGE=${{ github.ref_name }}``:
  two variants, one setter;
* ``worker`` (deploy.yml:11) -- an image whose final stage copies from a build stage, and an
  ``envFrom`` Secret after its ConfigMap.

``builder`` builds and publishes both images (``team/app``, ``team/worker``) from CircleCI;
``app/settings.py`` reads every variable at module scope, ``tools/report.py`` reads two outside
the image. ``namesake`` reads a same-named variable nothing of it configures, at the path the
app image copies. ``composer`` has
two compose services building their own images (mapping and list forms); ``overlaid`` a compose
file beside the override compose merges over it.

What each variable of ``app`` expects (the decision, and why):

====================  =====================================================================
VALUES_DEFAULT        linked: the chart's values.yaml key (values-file)
VALUES_OVERLAY        linked: env/prod.yaml's key, the chart default shadowed (values-file)
SET_LITERAL           linked: deploy.yml:10's ``--set`` (set, literal)
SET_SYMBOLIC          linked: deploy.yml:10's ``--set ...${{ github.sha }}`` (set, symbolic)
SET_SECRET            linked: deploy.yml:10's ``--set ...${{ secrets.APP_TOKEN }}`` (withheld)
LITERAL_VALUE         setter-unresolved (not-indexed): a literal template value
SECRET_REF            setter-unresolved (secret): ``secretKeyRef`` app-secrets/token
CM_REF                linked: app-config's key (configmap-key)
CM_MISSING            setter-unresolved (unread-source): a ConfigMap not in the chart
DEFAULTED             setter-unresolved (values-unresolved): empty, under a template default
DUPLICATE             setter-unresolved (duplicate-entry)
ABSENT                setter-unresolved (not-indexed): no layer sets its path
FROM_EARLY            setter-unresolved (unread-source): outside-config, after it, may override
FROM_LAST             linked: late-config's key, the last source (env-from)
FROM_DIRECT           linked: the direct entry's values key, over app-config (values-file)
ONLY_OUTSIDE          reader-outside-image: read only in tools/report.py
OUTSIDE               linked to app/settings.py; tools/report.py's read listed outside
STAGE                 setter-unresolved (not-indexed) for ``app``; linked for ``staged``
====================  =====================================================================
"""

from __future__ import annotations

from textwrap import dedent

APP_CHART = {
    "charts/app/Chart.yaml": 'apiVersion: v2\nname: app\nversion: 1.0.0\nappVersion: "1.0.0"\n',
    "charts/app/values.yaml": dedent(
        """\
        image:
          repository: registry.example:5000/team/app
          tag: ""
        env:
          VALUES_DEFAULT: "1"
          VALUES_OVERLAY: "2"
          SET_LITERAL: "3"
          DEFAULTED: ""
          ONLY_OUTSIDE: "4"
          OUTSIDE: "5"
          FROM_DIRECT: "6"
        """
    ),
    "charts/app/templates/deployment.yaml": dedent(
        """\
        apiVersion: apps/v1
        kind: Deployment
        metadata:
          name: app
        spec:
          selector:
            matchLabels:
              app: app
          template:
            metadata:
              labels:
                app: app
            spec:
              containers:
                - name: app
                  image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
                  env:
                    - name: VALUES_DEFAULT
                      value: {{ .Values.env.VALUES_DEFAULT | quote }}
                    - name: VALUES_OVERLAY
                      value: {{ .Values.env.VALUES_OVERLAY | quote }}
                    - name: SET_LITERAL
                      value: {{ .Values.env.SET_LITERAL | quote }}
                    - name: SET_SYMBOLIC
                      value: {{ .Values.env.SET_SYMBOLIC | quote }}
                    - name: SET_SECRET
                      value: {{ .Values.env.SET_SECRET | quote }}
                    - name: LITERAL_VALUE
                      value: "json"
                    - name: SECRET_REF
                      valueFrom:
                        secretKeyRef:
                          name: app-secrets
                          key: token
                    - name: CM_REF
                      valueFrom:
                        configMapKeyRef:
                          name: app-config
                          key: CM_REF
                    - name: CM_MISSING
                      valueFrom:
                        configMapKeyRef:
                          name: elsewhere-config
                          key: CM_MISSING
                    - name: DEFAULTED
                      value: {{ .Values.env.DEFAULTED | default "x" | quote }}
                    - name: DUPLICATE
                      value: "one"
                    - name: DUPLICATE
                      value: "two"
                    - name: ABSENT
                      value: {{ .Values.env.ABSENT | quote }}
                    - name: FROM_DIRECT
                      value: {{ .Values.env.FROM_DIRECT | quote }}
                    - name: ONLY_OUTSIDE
                      value: {{ .Values.env.ONLY_OUTSIDE | quote }}
                    - name: OUTSIDE
                      value: {{ .Values.env.OUTSIDE | quote }}
                    - name: STAGE
                      value: {{ .Values.env.STAGE | quote }}
                  envFrom:
                    - configMapRef:
                        name: app-config
                    - configMapRef:
                        name: outside-config
                    - configMapRef:
                        name: late-config
        """
    ),
    "charts/app/templates/app-config.yaml": dedent(
        """\
        apiVersion: v1
        kind: ConfigMap
        metadata:
          name: app-config
        data:
          CM_REF: "x"
          FROM_EARLY: "y"
          FROM_DIRECT: "z"
        """
    ),
    "charts/app/templates/late-config.yaml": dedent(
        """\
        apiVersion: v1
        kind: ConfigMap
        metadata:
          name: late-config
        data:
          FROM_LAST: "w"
        """
    ),
}

WORKER_CHART = {
    "charts/worker/Chart.yaml": 'apiVersion: v2\nname: worker\nversion: 1.0.0\nappVersion: "1.0.0"\n',
    "charts/worker/values.yaml": 'image:\n  repository: registry.example:5000/team/worker\n  tag: ""\nthreads: "4"\n',
    "charts/worker/templates/deployment.yaml": dedent(
        """\
        apiVersion: apps/v1
        kind: Deployment
        metadata:
          name: worker
        spec:
          selector:
            matchLabels:
              app: worker
          template:
            metadata:
              labels:
                app: worker
            spec:
              containers:
                - name: worker
                  image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
                  env:
                    - name: THREADS
                      value: {{ .Values.threads | quote }}
                  envFrom:
                    - configMapRef:
                        name: worker-config
                    - secretRef:
                        name: worker-secrets
        """
    ),
    "charts/worker/templates/worker-config.yaml": dedent(
        """\
        apiVersion: v1
        kind: ConfigMap
        metadata:
          name: worker-config
        data:
          WORKER_MODE: "fast"
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
      - run: helm upgrade --install app charts/app -f charts/app/values.yaml -f env/prod.yaml --set image.tag=${{ github.sha }} --set env.SET_LITERAL=on --set env.SET_SYMBOLIC=${{ github.sha }} --set env.SET_SECRET=${{ secrets.APP_TOKEN }}
      - run: helm upgrade --install worker charts/worker --set image.tag=${{ github.sha }}
"""

STAGED = """\
name: staged
on:
  push:
    branches: [main, release]
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - run: helm upgrade --install staged charts/app --set image.tag=${{ github.sha }} --set env.STAGE=${{ github.ref_name }}
"""

SHIPPER = {
    **APP_CHART,
    **WORKER_CHART,
    ".github/workflows/deploy.yml": DEPLOY,
    ".github/workflows/staged.yml": STAGED,
    "env/prod.yaml": 'env:\n  VALUES_OVERLAY: "20"\n',
}

BUILD = """\
version: 2.1
jobs:
  build:
    docker:
      - image: cimg/base:2024.01
    steps:
      - checkout
      - run: docker build -t registry.example:5000/team/app:$CIRCLE_SHA1 -f deploy/app.Dockerfile .
      - run: docker push registry.example:5000/team/app:$CIRCLE_SHA1
      - run: docker build -t registry.example:5000/team/worker:$CIRCLE_SHA1 -f deploy/worker.Dockerfile .
      - run: docker push registry.example:5000/team/worker:$CIRCLE_SHA1
workflows:
  build:
    jobs:
      - build
"""

SETTINGS = """\
import os

VALUES_DEFAULT = os.getenv("VALUES_DEFAULT", "a")
VALUES_OVERLAY = os.getenv("VALUES_OVERLAY", "b")
SET_LITERAL = os.getenv("SET_LITERAL", "c")
SET_SYMBOLIC = os.getenv("SET_SYMBOLIC", "d")
SET_SECRET = os.getenv("SET_SECRET", "e")
LITERAL_VALUE = os.getenv("LITERAL_VALUE", "f")
SECRET_REF = os.getenv("SECRET_REF", "g")
CM_REF = os.getenv("CM_REF", "h")
CM_MISSING = os.getenv("CM_MISSING", "i")
DEFAULTED = os.getenv("DEFAULTED", "j")
DUPLICATE = os.getenv("DUPLICATE", "k")
ABSENT = os.getenv("ABSENT", "l")
FROM_EARLY = os.getenv("FROM_EARLY", "m")
FROM_LAST = os.getenv("FROM_LAST", "n")
FROM_DIRECT = os.getenv("FROM_DIRECT", "o")
OUTSIDE = os.getenv("OUTSIDE", "p")
STAGE = os.getenv("STAGE", "q")
"""

MAIN = """\
from fastapi import FastAPI

from app.settings import VALUES_OVERLAY

app = FastAPI()


@app.get("/items")
def list_items() -> dict:
    return {"limit": VALUES_OVERLAY}
"""

BUILDER = {
    ".circleci/config.yml": BUILD,
    "deploy/app.Dockerfile": 'FROM python:3.12-slim\nWORKDIR /app\nCOPY app /app/app\nCMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]\n',
    "deploy/worker.Dockerfile": 'FROM python:3.12-slim AS build\nWORKDIR /src\nCOPY . /src\n\nFROM python:3.12-slim\nWORKDIR /app\nCOPY --from=build /src/worker /app/worker\nCMD ["python", "-m", "worker.main"]\n',
    "app/__init__.py": "",
    "app/settings.py": SETTINGS,
    "app/main.py": MAIN,
    "tools/report.py": 'import os\n\nONLY_OUTSIDE = os.environ["ONLY_OUTSIDE"]\nOUTSIDE = os.environ["OUTSIDE"]\n',
    "worker/__init__.py": "",
    "worker/main.py": 'import os\n\nTHREADS = int(os.getenv("THREADS", "1"))\nMODE = os.getenv("WORKER_MODE", "slow")\n',
}

#: A same-named read at the very path the app image copies from ``builder``: only the
#: repository it lies in keeps it out of the image.
NAMESAKE = {"app/settings.py": 'import os\n\nOVERLAY = os.getenv("VALUES_OVERLAY", "9")\n'}

COMPOSE = """\
services:
  web:
    build:
      context: .
      dockerfile: Dockerfile
    environment:
      PORT: "8000"
      EMPTY: ""
      PASSED:
      INTERP: "${INTERP:-x}"
  jobs:
    build: .
    environment:
      - JOB_LIMIT=3
      - JOB_PASSED
      - JOB_TWICE=1
      - JOB_TWICE=2
"""

COMPOSER = {
    "docker-compose.yml": COMPOSE,
    "Dockerfile": 'FROM python:3.12-slim\nWORKDIR /srv\nCOPY web /srv/web\nCOPY jobs /srv/jobs\nCMD ["python", "-m", "web.main"]\n',
    "web/__init__.py": "",
    "web/main.py": 'import os\n\nPORT = os.getenv("PORT", "80")\nEMPTY = os.getenv("EMPTY", "e")\nPASSED = os.getenv("PASSED", "p")\nINTERP = os.getenv("INTERP", "i")\n',
    "jobs/__init__.py": "",
    "jobs/run.py": 'import os\n\nJOB_LIMIT = os.getenv("JOB_LIMIT", "1")\nJOB_PASSED = os.getenv("JOB_PASSED", "")\nJOB_TWICE = os.getenv("JOB_TWICE", "0")\n',
}

OVERLAID = {
    "docker-compose.yml": 'services:\n  app:\n    build: .\n    environment:\n      LEVEL: "info"\n',
    "docker-compose.override.yml": 'services:\n  app:\n    environment:\n      LEVEL: "debug"\n',
    "Dockerfile": "FROM python:3.12-slim\nCOPY src /src\n",
    "src/settings.py": 'import os\n\nLEVEL = os.getenv("LEVEL", "warn")\n',
}


def repos() -> dict[str, dict[str, str]]:
    """The corpus: ``shipper``, ``builder``, ``namesake``, ``composer`` and ``overlaid``."""
    return {
        "shipper": SHIPPER,
        "builder": BUILDER,
        "namesake": NAMESAKE,
        "composer": COMPOSER,
        "overlaid": OVERLAID,
    }
