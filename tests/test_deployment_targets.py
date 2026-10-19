"""Deployment targets: where a repository says it is reached, and the links users state.

Every fixture is synthetic (``*.example.test``). A chart's Ingress is read leniently for
its hosts, separately from the strict subset the HTTP join links through; a target is
resolved under the chart's defaults, each release and each link, and the four states --
configured, conditions, resolved, verified -- stay apart.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path, PurePosixPath

import pytest
from pydantic import ValidationError
from repoatlas.capabilities.errors import DeploymentLinkNotFoundError
from repoatlas.capabilities.service import CapabilityService
from repoatlas.errors import ContextResolutionError
from repoatlas.models.capability import CapabilityContext, CapabilityScope
from repoatlas.models.deployment_conditions import (
    ConditionContext,
    DeploymentCondition,
    evaluate,
)
from repoatlas.models.deployment_targets import (
    DeploymentLinkCreateRequest,
    DeploymentLinkDeleteRequest,
    DeploymentLinkFields,
    DeploymentLinkUpdateRequest,
    DeploymentTarget,
    DeploymentTargetsRequest,
    DeploymentTargetsResponse,
)
from repoatlas.parsers.deploy_helm import ingress_references
from repoatlas.parsers.deploy_targets import ingress_target_references
from repoatlas.parsers.http_facts import DEPLOYMENT_TARGET_INGRESS, PENDING_FACT
from repoatlas.services.workspace import Workspace
from tests.impact_corpus import build


def d(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


#: The shape the strict reader refuses: a version guard with ``fail``, variables, a
#: nested ``range``, annotations copied from values, and a second, redirecting path.
GUARDED_INGRESS = d("""
    {{- if .Values.ingress.enabled -}}
    {{- $fullName := include "web.fullname" . -}}
    {{- $svcPort := .Values.service.port -}}
    {{- if semverCompare ">=1.19-0" .Capabilities.KubeVersion.GitVersion -}}
    apiVersion: networking.k8s.io/v1
    {{- else -}}
    {{- fail "a newer cluster is needed" }}
    {{- end }}
    kind: Ingress
    metadata:
      name: {{ $fullName }}
      labels:
        {{- include "web.labels" . | nindent 4 }}
      {{- with .Values.ingress.annotations }}
      annotations:
        {{- toYaml . | nindent 4 }}
      {{- end }}
    spec:
      rules:
        {{- range .Values.ingress.hosts }}
        - host: "{{ $.Chart.Name }}-{{ $.Values.stage }}.{{ .host }}"
          http:
            paths:
              {{- range .paths }}
              - path: {{ .path }}
                pathType: ImplementationSpecific
                backend:
                  service:
                    name: {{ $fullName }}
                    port:
                      number: {{ $svcPort }}
              - path: {{ .path }}
                pathType: ImplementationSpecific
                backend:
                  service:
                    name: redirect
                    port:
                      name: use-annotation
              {{- end }}
        {{- end }}
      {{- end }}
""")

GUARDED_VALUES = d("""
    service:
      port: 8080
    ingress:
      enabled: true
      annotations:
        alb.ingress.kubernetes.io/listen-ports: '[{"HTTPS":443}, {"HTTP":80}]'
        alb.ingress.kubernetes.io/scheme: internal
      hosts:
        - host: apps.example.test
          paths:
            - path: /*
""")

CHART = "apiVersion: v2\nname: web\nversion: 0.1.0\n"


def guarded_chart(values: str = GUARDED_VALUES, **extra: str) -> dict[str, str]:
    return {
        "charts/web/Chart.yaml": CHART,
        "charts/web/values.yaml": values,
        "charts/web/templates/ingress.yaml": GUARDED_INGRESS,
        **extra,
    }


def _context(workspace: Workspace, name: str) -> CapabilityContext:
    slug = next(r.slug for r in workspace.repositories.list_all() if r.name == name)
    return CapabilityContext(scope=CapabilityScope.REPOSITORY, repository=slug)


def targets(workspace: Workspace, name: str, **fields: object) -> DeploymentTargetsResponse:
    return CapabilityService(workspace).deployment_targets(
        DeploymentTargetsRequest(context=_context(workspace, name), **fields)  # type: ignore[arg-type]
    )


def only(response: DeploymentTargetsResponse) -> DeploymentTarget:
    [target] = [t for t in response.targets if t.kind == "ingress"]
    return target


# -- the lenient reader ---------------------------------------------------------------


def test_the_strict_reader_still_refuses_what_the_lenient_one_reads() -> None:
    path = PurePosixPath("charts/web/templates/ingress.yaml")
    [strict] = ingress_references(path, GUARDED_INGRESS)
    assert "unread" in strict.meta

    [lenient] = ingress_target_references(path, GUARDED_INGRESS)
    meta = lenient.meta
    assert meta[PENDING_FACT] == DEPLOYMENT_TARGET_INGRESS
    assert meta["source"] == "chart-template"
    assert meta["chart"] == "charts/web"
    [host] = json.loads(meta["hosts"])
    assert host["parts"] == [
        ["chart", ""],
        ["text", "-"],
        ["value", "stage"],
        ["text", "."],
        ["value", "ingress.hosts[*].host"],
    ]
    assert host["paths"] == [
        {
            "parts": [["value", "ingress.hosts[*].paths[*].path"]],
            "type": "ImplementationSpecific",
            "line": 25,
        }
    ]
    # The version check's `if`/`else` ends before `kind:`; only `ingress.enabled` guards it.
    assert [g["value"] for g in json.loads(meta["guards"])] == ["ingress.enabled"]
    assert json.loads(meta["annotations_from"]) == "ingress.annotations"
    assert json.loads(meta["backends"]) == [
        ["{{ $fullName }}", "{{ $svcPort }}", 29],
        ["redirect", "use-annotation", 36],
    ]


def test_a_plain_manifest_with_tls_and_a_literal_scheme() -> None:
    manifest = d("""
        apiVersion: networking.k8s.io/v1
        kind: Ingress
        metadata:
          name: api
        spec:
          tls:
            - hosts:
                - api.example.test
              secretName: api-tls
          rules:
            - host: api.example.test
              http:
                paths:
                  - path: /v2
                    pathType: Prefix
                    backend:
                      service:
                        name: api
                        port:
                          number: 80
    """)
    [found] = ingress_target_references(PurePosixPath("deploy/ingress.yaml"), manifest)
    assert found.meta["source"] == "manifest"
    assert json.loads(found.meta["tls"]) == [{"parts": [["text", "api.example.test"]], "line": 8}]
    [host] = json.loads(found.meta["hosts"])
    assert host["parts"] == [["text", "api.example.test"]]
    assert host["paths"][0]["parts"] == [["text", "/v2"]]


def test_what_is_not_read_is_named_never_dropped() -> None:
    template = d("""
        {{- range $name, $host := .Values.hosts }}
        ---
        kind: Ingress
        spec:
          rules:
            - host: {{ printf "%s.example.test" $name }}
        {{- end }}
    """)
    [found] = ingress_target_references(PurePosixPath("c/templates/i.yaml"), template)
    [host] = json.loads(found.meta["hosts"])
    assert host["parts"] == [["opaque", 'printf "%s.example.test" $name']]
    [hole] = json.loads(found.meta["holes"])
    assert hole["reason"] == "range"


def test_a_credential_in_an_expression_is_withheld() -> None:
    template = d("""
        kind: Ingress
        spec:
          rules:
            - host: {{ .Values.auth.password | b64enc }}.example.test
    """)
    [found] = ingress_target_references(PurePosixPath("c/templates/i.yaml"), template)
    [host] = json.loads(found.meta["hosts"])
    assert host["parts"][0] == ["opaque", "<withheld>"]


def test_files_with_no_ingress_record_nothing() -> None:
    assert ingress_target_references(PurePosixPath("c/templates/svc.yaml"), "kind: Service\n") == []
    assert ingress_target_references(PurePosixPath("README.md"), "kind: Ingress\n") == []


# -- resolution -----------------------------------------------------------------------


def test_chart_defaults_leave_an_unset_value_a_named_hole(
    workspace: Workspace, tmp_path: Path
) -> None:
    built = build(workspace, tmp_path, {"web": guarded_chart()})
    target = only(targets(built, "web"))

    assert target.path == "charts/web/templates/ingress.yaml"
    assert (
        target.pattern
        == "https://{chart}-{stage}.{ingress.hosts[0].host}{ingress.hosts[0].paths[0].path}"
    )
    assert target.schemes == ["https", "http"]
    assert target.scheme_basis == "listen-ports annotation in values `ingress.annotations`"
    assert [(g.expression, g.state) for g in target.guards] == [(".Values.ingress.enabled", "met")]
    [defaults] = target.resolutions
    assert defaults.basis == "chart-defaults"
    assert defaults.state == "partial"
    assert defaults.url is None
    assert defaults.rendered == "https://web-{stage}.apps.example.test/*"
    by_name = {v.name: v for v in defaults.variables}
    assert by_name["chart"].value == "web" and by_name["chart"].basis == "chart"
    assert by_name["stage"].value is None and by_name["stage"].basis is None
    assert by_name["ingress.hosts[0].host"].basis == "chart-defaults"
    assert defaults.verification == "unverified"


def test_a_link_binding_the_hole_resolves_it_and_stays_unverified(
    workspace: Workspace, tmp_path: Path
) -> None:
    built = build(workspace, tmp_path, {"web": guarded_chart()})
    target = only(targets(built, "web"))
    service = CapabilityService(built)
    created = service.deployment_link_create(
        DeploymentLinkCreateRequest(
            context=_context(built, "web"),
            link=DeploymentLinkFields(
                label="development",
                environment="dev",
                target_id=target.target_id,
                variables={"stage": "dev"},
                conditions=[DeploymentCondition(kind="branch", value="develop")],
            ),
        )
    ).link

    response = targets(built, "web", branch="develop")
    resolved = only(response).resolutions[-1]
    assert resolved.basis == "link" and resolved.link_id == created.id
    assert resolved.url == "https://web-dev.apps.example.test/"
    assert resolved.origin == "https://web-dev.apps.example.test"
    assert resolved.state == "resolved"
    assert resolved.conditions == "match"
    assert resolved.verification == "unverified"
    stage = next(v for v in resolved.variables if v.name == "stage")
    assert (stage.value, stage.basis) == ("dev", "configured")

    assert only(targets(built, "web", branch="main")).resolutions[-1].conditions == "no_match"
    # Absent a branch the caller or the checkout states, a condition is unknown.
    assert response.branch_basis == "request"


def test_a_release_supplies_values_and_a_ci_branch_variable_is_context(
    workspace: Workspace, tmp_path: Path
) -> None:
    tilt = "k8s_yaml(helm('charts/web', name='web', set=['stage=local']))\n"
    workflow = d("""
        on:
          push:
            branches: [main]
        jobs:
          deploy:
            runs-on: ubuntu-latest
            steps:
              - run: helm upgrade --install web charts/web --set stage=$GITHUB_REF_NAME
    """)
    built = build(
        workspace,
        tmp_path,
        {"web": guarded_chart(**{"Tiltfile": tilt, ".github/workflows/deploy.yml": workflow})},
    )
    resolutions = only(targets(built, "web", branch="main")).resolutions
    by_basis = {(r.basis, r.label.split(" (")[0]): r for r in resolutions}
    tilt_release = by_basis[("release", "release `web`")]
    assert tilt_release.url in {
        "https://web-local.apps.example.test/",
        "https://web-main.apps.example.test/",
    }
    urls = sorted(r.url for r in resolutions if r.basis == "release")
    assert urls == ["https://web-local.apps.example.test/", "https://web-main.apps.example.test/"]
    ci = next(r for r in resolutions if r.url == "https://web-main.apps.example.test/")
    assert ci.conditions == "match"
    stage = next(v for v in ci.variables if v.name == "stage")
    assert stage.basis == "context"

    elsewhere = only(targets(built, "web", branch="feature-x")).resolutions
    ci = next(r for r in elsewhere if r.basis == "release" and "deploy.yml" in r.label)
    assert ci.conditions == "no_match"
    assert ci.url == "https://web-feature-x.apps.example.test/"


def test_an_unmet_guard_is_reported_not_hidden(workspace: Workspace, tmp_path: Path) -> None:
    values = GUARDED_VALUES.replace("enabled: true", "enabled: false")
    built = build(workspace, tmp_path, {"web": guarded_chart(values)})
    target = only(targets(built, "web"))
    assert [g.state for g in target.guards] == ["unmet"]
    assert target.resolutions[0].enabled == "unmet"


def test_a_credential_value_is_never_shown(workspace: Workspace, tmp_path: Path) -> None:
    template = GUARDED_INGRESS.replace("$.Values.stage", "$.Values.apiToken")
    values = GUARDED_VALUES + "apiToken: s3cr3t-value\n"
    built = build(
        workspace,
        tmp_path,
        {"web": {**guarded_chart(values), "charts/web/templates/ingress.yaml": template}},
    )
    response = targets(built, "web")
    assert "s3cr3t" not in response.model_dump_json()
    token = next(v for v in only(response).resolutions[0].variables if v.name == "apiToken")
    assert token.withheld and token.value is None


def test_two_differently_structured_repositories(workspace: Workspace, tmp_path: Path) -> None:
    manifest = d("""
        apiVersion: networking.k8s.io/v1
        kind: Ingress
        metadata:
          name: api
          annotations:
            alb.ingress.kubernetes.io/listen-ports: '[{"HTTP": 8080}]'
        spec:
          rules:
            - host: api.example.test
              http:
                paths:
                  - path: /
                    pathType: Prefix
                    backend:
                      service:
                        name: api
                        port:
                          number: 80
    """)
    built = build(
        workspace, tmp_path, {"web": guarded_chart(), "api": {"k8s/ingress.yaml": manifest}}
    )
    target = only(targets(built, "api"))
    [declared] = target.resolutions
    assert (declared.basis, declared.url) == ("declared", "http://api.example.test:8080/")
    assert only(targets(built, "web")).resolutions[0].url is None


def test_an_openapi_server_is_a_target(workspace: Workspace, tmp_path: Path) -> None:
    spec = d("""
        openapi: 3.0.0
        info: {title: t, version: "1"}
        servers:
          - url: https://svc.example.test/v1
        paths: {}
    """)
    built = build(workspace, tmp_path, {"svc": {"openapi.yaml": spec}})
    response = targets(built, "svc")
    [target] = response.targets
    assert target.kind == "openapi-server"
    assert target.resolutions[0].url == "https://svc.example.test/v1"


# -- links ----------------------------------------------------------------------------


def test_links_are_created_updated_deleted_and_survive_a_sync(
    workspace: Workspace, tmp_path: Path
) -> None:
    built = build(workspace, tmp_path, {"web": guarded_chart()})
    context = _context(built, "web")
    service = CapabilityService(built)
    link = service.deployment_link_create(
        DeploymentLinkCreateRequest(
            context=context,
            link=DeploymentLinkFields(label="staging", url="https://staging.example.test/"),
        )
    ).link
    assert link.verification == "unverified" and link.verified_at is None

    confirmed = service.deployment_link_update(
        DeploymentLinkUpdateRequest(
            context=context,
            id=link.id,
            link=DeploymentLinkFields(
                label="staging",
                url="https://staging.example.test/",
                verification="user_confirmed",
            ),
        )
    ).link
    assert confirmed.verified_at is not None

    own = next(t for t in targets(built, "web").targets if t.kind == "link")
    assert own.resolutions[0].verification == "user_confirmed"
    assert own.resolutions[0].url == "https://staging.example.test/"

    from tests.incremental_corpus import sync_all

    sync_all(built, tmp_path / "repos")
    assert [x.id for x in targets(built, "web").links] == [link.id]

    assert service.deployment_link_delete(
        DeploymentLinkDeleteRequest(context=context, id=link.id)
    ).deleted
    with pytest.raises(DeploymentLinkNotFoundError):
        service.deployment_link_delete(DeploymentLinkDeleteRequest(context=context, id=link.id))


def test_a_link_to_a_target_no_longer_stated_is_detached(
    workspace: Workspace, tmp_path: Path
) -> None:
    built = build(workspace, tmp_path, {"web": guarded_chart()})
    service = CapabilityService(built)
    service.deployment_link_create(
        DeploymentLinkCreateRequest(
            context=_context(built, "web"),
            link=DeploymentLinkFields(
                label="old", target_id="dt_0123456789abcdef", url="https://old.example.test"
            ),
        )
    )
    [detached] = [t for t in targets(built, "web").targets if t.detached]
    assert detached.kind == "link"
    assert detached.target_id == "dt_0123456789abcdef"


def test_a_link_of_another_repository_is_not_found(workspace: Workspace, tmp_path: Path) -> None:
    built = build(workspace, tmp_path, {"web": guarded_chart(), "api": {"README.md": "x\n"}})
    service = CapabilityService(built)
    link = service.deployment_link_create(
        DeploymentLinkCreateRequest(
            context=_context(built, "web"), link=DeploymentLinkFields(label="x")
        )
    ).link
    with pytest.raises(DeploymentLinkNotFoundError):
        service.deployment_link_delete(
            DeploymentLinkDeleteRequest(context=_context(built, "api"), id=link.id)
        )


def test_duplicate_urls_are_marked(workspace: Workspace, tmp_path: Path) -> None:
    built = build(workspace, tmp_path, {"web": guarded_chart()})
    target = only(targets(built, "web"))
    service = CapabilityService(built)
    for label in ("a", "b"):
        service.deployment_link_create(
            DeploymentLinkCreateRequest(
                context=_context(built, "web"),
                link=DeploymentLinkFields(
                    label=label, target_id=target.target_id, variables={"stage": "dev"}
                ),
            )
        )
    first, second = only(targets(built, "web")).resolutions[1:]
    assert first.duplicate_of is None
    assert second.duplicate_of == f"{target.target_id}/1"


def test_deployment_calls_need_one_repository(workspace: Workspace, tmp_path: Path) -> None:
    built = build(workspace, tmp_path, {"web": guarded_chart()})
    with pytest.raises(ContextResolutionError):
        CapabilityService(built).deployment_targets(
            DeploymentTargetsRequest(context=CapabilityContext(scope=CapabilityScope.GLOBAL))
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pw@x.example.test/",
        "https://x.example.test/?token=1",
        "https://x.example.test/#a",
        "ftp://x.example.test/",
        "x.example.test",
        "https://{stage}.example.test/",
    ],
)
def test_a_link_url_that_could_carry_a_secret_or_is_not_a_place_is_refused(url: str) -> None:
    with pytest.raises(ValidationError):
        DeploymentLinkFields(label="x", url=url)


# -- conditions -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("conditions", "context", "expected"),
    [
        ([], {}, "none"),
        ([{"kind": "branch", "value": "main"}], {"branch": "main"}, "match"),
        ([{"kind": "branch", "value": "main"}], {"branch": "dev"}, "no_match"),
        ([{"kind": "branch", "value": "main"}], {}, "unknown"),
        (
            [{"kind": "branch", "op": "glob", "value": "release-*"}],
            {"branch": "release-2"},
            "match",
        ),
        (
            [{"kind": "branch", "value": "main"}, {"kind": "branch", "value": "dev"}],
            {"branch": "main"},
            "conflict",
        ),
        (
            [{"kind": "branch", "value": "main"}, {"kind": "branch", "op": "glob", "value": "r-*"}],
            {},
            "conflict",
        ),
        (
            [{"kind": "environment", "value": "prod"}, {"kind": "branch", "value": "main"}],
            {"branch": "main"},
            "unknown",
        ),
        (
            [{"kind": "environment", "value": "prod"}, {"kind": "branch", "value": "main"}],
            {"branch": "dev"},
            "no_match",
        ),
        ([{"kind": "pull_request", "value": "true"}], {"pull_request": True}, "match"),
        (
            [{"kind": "variable", "name": "region", "value": "west"}],
            {"variables": {"region": "west"}},
            "match",
        ),
    ],
)
def test_conditions(
    conditions: list[dict[str, str]], context: dict[str, object], expected: str
) -> None:
    parsed = [DeploymentCondition(**c) for c in conditions]
    assert evaluate(parsed, ConditionContext(**context)) == expected  # type: ignore[arg-type]


def test_a_variable_condition_must_name_its_variable() -> None:
    with pytest.raises(ValidationError):
        DeploymentCondition(kind="variable", value="x")
    with pytest.raises(ValidationError):
        DeploymentCondition(kind="branch", name="x", value="main")
