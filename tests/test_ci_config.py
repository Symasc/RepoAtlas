# ruff: noqa: E501 - the sample configurations hold their lines as a CI system or Helm reads them
"""The CI, template and values readers, file by file, on invented configurations
(DF1_PHASE_A.md §3-§7, §9, §11): what each records, what it refuses, and what it never keeps."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from textwrap import dedent

import pytest
import yaml
from repoatlas.parsers.ci_config import (
    CI_HELM_RELEASE,
    CI_IMAGE_BUILD,
    CI_SEMANTICS,
    CI_STEP_UNSUPPORTED,
    MAX_NODES,
    ci_references,
    ci_system,
    declaration_references,
)
from repoatlas.parsers.deploy_compose import compose_references
from repoatlas.parsers.deploy_helm import (
    chart_references,
    configmap_references,
    values_references,
    workload_references,
)
from repoatlas.parsers.structured import StructuredParser
from repoatlas.parsers.withhold import WITHHELD


def claims(path: str, text: str) -> list[dict[str, object]]:
    documents = list(yaml.compose_all(dedent(text), Loader=yaml.SafeLoader))
    found, _ = ci_references(PurePosixPath(path), documents)
    return [
        {"role": r.meta["pending_fact"], "target": r.target_key, "line": r.line_start, **r.meta}
        for r in found
    ]


def builds(found: list[dict[str, object]]) -> list[tuple[str, str]]:
    out = []
    for claim in found:
        if claim["role"] == CI_IMAGE_BUILD:
            out += [(ref["image"], ref["decision"]) for ref in json.loads(str(claim["refs"]))]
    return out


def steps(found: list[dict[str, object]]) -> list[tuple[str, str]]:
    return [(str(c["kind"]), str(c["target"])) for c in found if c["role"] == CI_STEP_UNSUPPORTED]


# ---------------------------------------------------------------- which files
@pytest.mark.parametrize(
    ("path", "system"),
    [
        (".circleci/config.yml", "circleci"),
        (".github/workflows/deploy.yml", "github"),
        (".github/workflows/deploy.yaml", "github"),
        (".github/config.yml", None),
        (".github/dependabot.yml", None),
        ("services/a/.circleci/config.yml", None),
        (".github/workflows/sub/deploy.yml", None),
    ],
)
def test_only_the_ci_systems_own_paths_are_configurations(path: str, system: str | None) -> None:
    assert ci_system(PurePosixPath(path)) == system


def test_a_file_that_looks_like_a_workflow_elsewhere_records_nothing() -> None:
    text = "jobs:\n  deploy:\n    steps:\n      - run: helm upgrade --install decoy charts/x\n"
    assert claims(".github/config.yml", text) == []


# ---------------------------------------------------------------- GitHub Actions
WORKFLOW = """
name: deploy
on:
  push:
    branches: [main]
jobs:
  ship:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: docker/login-action@v3
        with: {username: u, password: "${{ secrets.TOKEN }}"}
      - uses: docker/build-push-action@v6
        with: {push: true, tags: "registry.example:5000/a/api:1.0"}
      - uses: docker/build-push-action@v6
        with: {push: false, tags: "registry.example:5000/a/pr:1.0"}
      - uses: docker/build-push-action@v6
        with: {push: "${{ inputs.publish }}", tags: "registry.example:5000/a/night:1.0"}
      - uses: docker/build-push-action@v6
        with: {tags: "registry.example:5000/a/none:1.0"}
      - uses: docker/metadata-action@v5
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683
      - run: helm upgrade --install api charts/api -f charts/api/values.yaml --set image.tag=${{ github.sha }} -n prod
"""


def test_the_builtin_action_configures_publication_only_from_a_literal_push() -> None:
    found = claims(".github/workflows/deploy.yml", WORKFLOW)
    assert builds(found) == [
        ("registry.example:5000/a/api:1.0", "publish-configured"),
        ("registry.example:5000/a/pr:1.0", "built-not-published"),
        ("registry.example:5000/a/night:1.0", "publication-unresolved"),
        ("registry.example:5000/a/none:1.0", "built-not-published"),
    ]
    assert {c["basis"] for c in found if c["role"] == CI_IMAGE_BUILD} == {"builtin"}


def test_unknown_and_sha_pinned_actions_are_unsupported_whatever_the_job() -> None:
    found = claims(".github/workflows/deploy.yml", WORKFLOW)
    assert steps(found) == [
        ("action", "docker/metadata-action@v5"),
        ("action", "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683"),
    ]
    # an unsupported action's inputs are never kept
    assert all("with" not in c and "password" not in json.dumps(c) for c in found)


def test_a_release_records_chart_values_settings_and_its_branch_domain() -> None:
    [release] = [
        c for c in claims(".github/workflows/deploy.yml", WORKFLOW) if c["role"] == CI_HELM_RELEASE
    ]
    assert release["target"] == "api" and release["chart"] == "charts/api"
    assert json.loads(str(release["values"])) == [
        ["charts/api/values.yaml", "charts/api/values.yaml"]
    ]
    assert json.loads(str(release["set"])) == [["image.tag", [["expr", "github.sha"]]]]
    assert json.loads(str(release["domains"])) == {"branches": ["main"]}


def test_on_is_read_as_a_key_never_as_a_boolean() -> None:
    text = WORKFLOW.replace(
        "on:\n  push:\n    branches: [main]", "'on': {push: {branches: [main]}}"
    )
    assert [
        c["domains"]
        for c in claims(".github/workflows/deploy.yml", text)
        if c["role"] == CI_HELM_RELEASE
    ] == ['{"branches": ["main"]}']


def test_a_reusable_workflow_definition_proves_no_release_and_its_call_is_unsupported() -> None:
    definition = """
    on: {workflow_call: {inputs: {release: {type: string}}}}
    jobs:
      deploy:
        steps:
          - run: helm upgrade --install ${{ inputs.release }} charts/api -f ${{ inputs.values }}
          - run: echo done
    """
    assert [
        (c["role"], c["kind"]) for c in claims(".github/workflows/deploy-one.yml", definition)
    ] == [(CI_STEP_UNSUPPORTED, "caller-dependent")]
    caller = "on: push\njobs:\n  promote:\n    uses: ./.github/workflows/deploy-one.yml\n"
    assert steps(claims(".github/workflows/promote.yml", caller)) == [
        ("reusable-workflow", "./.github/workflows/deploy-one.yml")
    ]


def test_publication_is_job_scoped_and_follows_a_tag_alias() -> None:
    text = """
    on: push
    jobs:
      a:
        steps:
          - run: docker build -t r.example/a/one:1 .
          - run: |
              docker tag r.example/a/one:1 r.example/a/one:latest
              docker push r.example/a/one:latest
          - run: docker build -t r.example/a/two:1 .
      b:
        steps:
          - run: docker push r.example/a/two:1
    """
    assert builds(claims(".github/workflows/x.yml", text)) == [
        ("r.example/a/one:1", "publish-configured"),
        ("r.example/a/two:1", "built-not-published"),
    ]


def test_a_nested_recognised_command_binds_nothing_and_a_script_is_named_only() -> None:
    text = """
    on: push
    jobs:
      a:
        steps:
          - run: |
              for env in qa prd; do
                helm upgrade --install api-$env charts/api
              done
          - run: ./scripts/deploy.sh prod --token abc
    """
    found = claims(".github/workflows/x.yml", text)
    assert steps(found) == [("control-flow", "control-flow"), ("script", "./scripts/deploy.sh")]
    assert not any(c["role"] == CI_HELM_RELEASE for c in found)
    assert "abc" not in json.dumps(found)


def test_a_credential_setting_and_a_literal_login_are_never_kept() -> None:
    text = """
    on: push
    jobs:
      a:
        steps:
          - run: docker login -u bench -p CANARY_LOGIN registry.example
          - run: helm upgrade --install api charts/api --set auth.token=CANARY_SET --set url=https://u:CANARY_URL@db
    """
    found = claims(".github/workflows/x.yml", text)
    dumped = json.dumps(found)
    assert "CANARY" not in dumped
    [release] = [c for c in found if c["role"] == CI_HELM_RELEASE]
    assert json.loads(str(release["set"])) == [
        ["auth.token", [["withheld", ""]]],
        ["url", [["withheld", ""]]],
    ]


# ---------------------------------------------------------------- CircleCI
CIRCLE = """
version: 2.1
orbs:
  helm-deploy: sample-org/helm-deploy@1.0.0
  docker: other-org/docker@2.0.0
  images:
    jobs:
      build:
        parameters:
          name: {type: string, default: api}
        steps:
          - checkout
          - run: docker build -t r.example/a/<< parameters.name >>:$CIRCLE_SHA1 .
          - run: docker push r.example/a/<< parameters.name >>:$CIRCLE_SHA1
jobs:
  test:
    steps:
      - docker/install
      - run: pytest
workflows:
  release:
    jobs:
      - images/build
      - test
      - helm-deploy/deploy:
          requires: [images/build]
          chart: charts/api
          release-name: $CIRCLE_PROJECT_REPONAME
          token: SECRET_PARAM
          filters: {branches: {only: [main, release]}}
      - docker/publish:
          filters: {branches: {only: /.*/}}
"""


def test_an_inline_orb_is_source_and_a_named_orb_is_unsupported_with_its_parameters() -> None:
    found = claims(".circleci/config.yml", CIRCLE)
    assert builds(found) == [("r.example/a/api:<var:CIRCLE_SHA1>", "publish-configured")]
    assert steps(found) == [
        ("orb-command", "other-org/docker@2.0.0 install"),
        ("orb-job", "sample-org/helm-deploy@1.0.0 deploy"),
        ("orb-job", "other-org/docker@2.0.0 publish"),
    ]
    deploy = next(c for c in found if c.get("orb_job") == "deploy")
    parameters = json.loads(str(deploy["parameters"]))
    assert parameters["chart"] == [["text", "charts/api"]]
    assert parameters["token"] == [["withheld", ""]]
    assert json.loads(str(deploy["requires"])) == ["images/build"]
    assert json.loads(str(deploy["domains"])) == {"branches": ["main", "release"]}
    publish = next(c for c in found if c.get("orb_job") == "publish")
    assert json.loads(str(publish["domains"])) == {}  # a regex filter is no domain


def test_a_dynamic_configuration_is_recorded_and_not_read() -> None:
    text = "version: 2.1\nsetup: true\nworkflows:\n  w:\n    jobs: [a]\njobs:\n  a:\n    steps:\n      - run: docker build -t r.example/x:1 .\n"
    assert [(c["kind"]) for c in claims(".circleci/config.yml", text)] == ["dynamic-config"]


# ---------------------------------------------------------------- the YAML
def test_merges_take_local_keys_first() -> None:
    text = """
    defaults: &defaults
      steps:
        - run: docker build -t r.example/merged:1 .
    on: push
    jobs:
      a:
        <<: *defaults
        steps:
          - run: docker build -t r.example/local:1 .
    """
    assert [image for image, _ in builds(claims(".github/workflows/x.yml", text))] == [
        "r.example/local:1"
    ]


def test_an_alias_cycle_is_cut_and_reported() -> None:
    text = "on: push\njobs: &jobs\n  a:\n    steps: *jobs\n"
    documents = list(yaml.compose_all(text, Loader=yaml.SafeLoader))
    _, truncated = ci_references(PurePosixPath(".github/workflows/x.yml"), documents)
    assert truncated


def test_the_node_budget_bounds_a_huge_configuration() -> None:
    steps_text = "".join(f"      - run: echo {i}\n" for i in range(MAX_NODES))
    text = f"on: push\njobs:\n  a:\n    steps:\n{steps_text}"
    documents = list(yaml.compose_all(text, Loader=yaml.SafeLoader))
    _, truncated = ci_references(PurePosixPath(".github/workflows/x.yml"), documents)
    assert truncated


def test_a_malformed_workflow_is_a_warning_not_a_claim() -> None:
    parsed = StructuredParser().parse(Path(".github/workflows/x.yml"), "on: [push\njobs: {")
    assert not [r for r in parsed.references if r.meta.get("pending_fact", "").startswith("ci-")]
    assert parsed.diagnostics


# ---------------------------------------------------------------- declarations
def test_a_declaration_file_yields_one_claim_per_mapping_valid_or_not() -> None:
    text = """
    repoatlas: ci-semantics/v1
    orbs:
      - orb: sample-org/helm-deploy@1.0.0
        job: deploy
        helm-release:
          chart: {parameter: chart}
          set: {parameter: overrides, format: comma-separated}
      - orb: sample-org/helm-deploy@1
        job: deploy
        helm-release: {chart: {parameter: chart}}
      - orb: sample-org/other@1.0.0
        job: deploy
        helm-release: {chart: {parameter: chart}, rollout: {parameter: x}}
    projects:
      sample-intake: {circleci-project: sample-intake}
    """
    found = declaration_references(list(yaml.compose_all(dedent(text), Loader=yaml.SafeLoader)))
    assert [(r.meta["kind"], bool(r.meta["invalid"])) for r in found] == [
        ("orb", False),
        ("orb", True),
        ("orb", True),
        ("project", False),
    ]
    assert all(r.meta["pending_fact"] == CI_SEMANTICS for r in found)


def test_a_file_without_the_header_declares_nothing() -> None:
    text = "orbs:\n  - orb: a/b@1.0.0\n    job: x\n    helm-release: {chart: {parameter: c}}\n"
    assert declaration_references(list(yaml.compose_all(text, Loader=yaml.SafeLoader))) == []


# ---------------------------------------------------------------- templates and values
DEPLOYMENT = """
{{- if .Values.api.enabled }}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "api.fullname" . }}
spec:
  selector:
    matchLabels:
      {{- include "api.selectorLabels" . | nindent 6 }}
  template:
    metadata:
      labels:
        {{- include "api.selectorLabels" . | nindent 8 }}
    spec:
      containers:
        - name: api
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}"
          ports:
            - name: http
              containerPort: 8080
          env:
            - name: LIMIT
              value: {{ .Values.env.LIMIT | quote }}
            - name: SORT
              valueFrom:
                configMapKeyRef: {name: settings, key: SORT}
            {{- if .Values.debug }}
            - name: DEBUG
              value: "1"
            {{- end }}
          envFrom:
            - configMapRef:
                name: settings
          resources:
            {{- toYaml .Values.resources | nindent 12 }}
        - name: helper
          image: {{ include "api.image" . }}
{{- end }}
"""


def test_a_workload_is_read_under_one_if_with_field_level_refusals() -> None:
    [claim] = workload_references(PurePosixPath("charts/api/templates/deployment.yaml"), DEPLOYMENT)
    meta = claim.meta
    assert meta["if"] == "api.enabled" and "unread" not in meta
    api, helper = json.loads(meta["containers"])
    assert api["image"] == [
        ["value", "image.repository"],
        ["text", ":"],
        ["value", "image.tag", "default", ".Chart.AppVersion"],
    ]
    assert [(e.get("name"), e.get("source")) for e in api["env"]] == [
        ("LIMIT", "value"),
        ("SORT", "configMapKeyRef"),
        (None, "conditional"),
    ]
    assert api["env"][0]["values"] == "env.LIMIT"
    assert [e["name"] for e in api["env_from"]] == ["settings"]
    assert "image_unread" in helper
    assert json.loads(meta["selector"]) == [["include", "api.selectorLabels"]]


@pytest.mark.parametrize(
    ("wrapper", "reason"),
    [
        ("{{- range .Values.apps }}", "its control is not one"),
        ("{{- with .Values.api }}", "its control is not one"),
    ],
)
def test_a_range_or_with_around_a_workload_leaves_it_unread(wrapper: str, reason: str) -> None:
    text = DEPLOYMENT.replace("{{- if .Values.api.enabled }}", wrapper)
    [claim] = workload_references(PurePosixPath("charts/api/templates/deployment.yaml"), text)
    assert str(claim.meta["unread"]).startswith(reason)


def test_control_around_the_containers_leaves_the_workload_unread() -> None:
    text = DEPLOYMENT.replace(
        "      containers:\n", "      {{- if .Values.on }}\n      containers:\n", 1
    ).replace('{{- end }}\n"""', '{{- end }}\n{{- end }}\n"""')
    [claim] = workload_references(PurePosixPath("charts/api/templates/deployment.yaml"), text)
    assert claim.meta.get("unread") or json.loads(claim.meta["containers"])[0].get("image_unread")


def test_a_configmap_records_keys_never_values() -> None:
    text = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: settings\ndata:\n  SORT: desc\n  TOKEN: CANARY\n"
    [claim] = configmap_references(PurePosixPath("charts/api/templates/configmap.yaml"), text)
    assert json.loads(claim.meta["keys"]) == [["SORT", 6], ["TOKEN", 7]]
    assert "CANARY" not in json.dumps(claim.meta) and "desc" not in json.dumps(claim.meta)


def test_values_files_withhold_credentials_and_an_overlay_is_its_own_claim() -> None:
    text = "image:\n  tag: '1.0'\nauth:\n  rootPassword: CANARY\ndb:\n  url: postgres://u:CANARY@db\nsecrets:\n  region: eu\n"
    documents = list(yaml.compose_all(text, Loader=yaml.SafeLoader))
    [own] = values_references(PurePosixPath("charts/api/values.yaml"), documents)
    [overlay] = values_references(PurePosixPath("environments/values-prod.yaml"), documents)
    assert (
        own.meta["pending_fact"] == "helm-values"
        and overlay.meta["pending_fact"] == "helm-values-file"
    )
    leaves = json.loads(overlay.meta["values"])
    assert leaves == {
        "image.tag": "1.0",
        "auth.rootPassword": WITHHELD,
        "db.url": WITHHELD,
        "secrets.region": WITHHELD,
    }
    assert values_references(PurePosixPath("environments/query.yaml"), documents) == []


def test_a_chart_records_its_app_version_and_compose_its_environment_names() -> None:
    [chart] = chart_references(
        PurePosixPath("charts/api/Chart.yaml"),
        list(
            yaml.compose_all(
                "apiVersion: v2\nname: api\nappVersion: '2.1'\n", Loader=yaml.SafeLoader
            )
        ),
    )
    assert chart.meta["app_version"] == "2.1"
    compose = (
        "services:\n  api:\n    build: .\n    environment:\n"
        "      URL: https://u:CANARY@db\n      LIMIT: '5'\n"
        "  worker:\n    image: w\n    environment:\n      - MODE=fast\n"
    )
    found = compose_references(
        PurePosixPath("docker-compose.yml"), list(yaml.compose_all(compose, Loader=yaml.SafeLoader))
    )
    assert [json.loads(r.meta.get("environment", "[]")) for r in found] == [
        [["URL", 5], ["LIMIT", 6]],
        [["MODE", 10]],
    ]
    assert "CANARY" not in json.dumps([r.meta for r in found])
