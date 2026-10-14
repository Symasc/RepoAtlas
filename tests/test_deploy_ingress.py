"""Ingress hosts from Helm charts, and the refusals they explain (deployment HTTP roadmap, M4).

An Ingress template is read in a narrow static subset only (review decision D5): one
``if .Values.<path>`` around one ``range .Values.<path>``; hosts of literal text, the
chart's name, values and the range item's fields; the scheme from a literal ALB
``listen-ports`` annotation; a catch-all path; a backend that is the chart's own Service.
A release a Tiltfile binds renders it, or does not. Every read Ingress also gives a host
pattern for the deployments no local source establishes, which explains a refusal and
never links.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path, PurePosixPath

import pytest
from repoatlas.graph.deploy_identity import (
    BACKEND_UNMATCHED,
    BINDING_UNRESOLVED,
    NOT_RENDERED,
    NOTHING_SERVED,
    RENDERED,
)
from repoatlas.graph.linker import ReferenceLinker
from repoatlas.parsers.deploy_helm import ingress_references
from repoatlas.parsers.http_facts import HELM_INGRESS, HTTP_UNRESOLVED_PATTERN, PENDING_FACT
from repoatlas.services.workspace import Workspace
from tests.http_crossing_corpus import crossings, decided, links
from tests.impact_corpus import build
from tests.incremental_corpus import assert_equals_fresh, edit, sync_all
from tests.test_deploy_helm import HELPERS, SERVICE


def d(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


INGRESS = d("""
    {{- if .Values.ingress.enabled }}
    {{- range .Values.ingress.groups }}
    ---
    apiVersion: networking.k8s.io/v1
    kind: Ingress
    metadata:
      name: {{ include "devices.fullname" $ }}-{{ .name }}
      annotations:
        alb.ingress.kubernetes.io/group.name: {{ .name }}
        alb.ingress.kubernetes.io/listen-ports: '[{"HTTPS":443}]'
    spec:
      ingressClassName: alb
      rules:
        - host: "{{ $.Chart.Name }}-{{ $.Values.stage }}.{{ .host }}"
          http:
            paths:
              - path: /*
                pathType: ImplementationSpecific
                backend:
                  service:
                    name: {{ include "devices.fullname" $ }}
                    port:
                      number: {{ $.Values.service.port }}
    {{- end }}
    {{- end }}
""")

VALUES = d("""
    stage: dev
    service:
      port: 80
    ingress:
      enabled: true
      groups:
        - name: internal
          host: int.example.com
        - name: public
          host: example.com
""")

APP = {
    "pyproject.toml": '[project]\nname = "devices"\n',
    "app.py": d("""
        from fastapi import FastAPI

        app = FastAPI()


        @app.get("/items/{item_id}")
        def get_item(item_id: str):
            return {}
    """),
}

CALLER = {
    "pyproject.toml": '[project]\nname = "caller"\n',
    "client.py": d("""
        import requests


        def dev_item(item_id):
            return requests.get(f"https://devices-dev.int.example.com/items/{item_id}", timeout=5)


        def public_item(item_id):
            return requests.get(f"https://devices-dev.example.com/items/{item_id}", timeout=5)


        def prd_item(item_id):
            return requests.get(f"https://devices-prd.int.example.com/items/{item_id}", timeout=5)


        def plain_item(item_id):
            return requests.get(f"http://devices-dev.int.example.com/items/{item_id}", timeout=5)


        def regional_item(i):
            return requests.get(f"https://devices-prd.eu.int.example.com/items/{i}", timeout=5)
    """),
}


def chart(*, enabled: bool | None, ingress: str = INGRESS, name: str = "devices") -> dict[str, str]:
    """A scaffolded chart with a Service and an Ingress; its one Tilt release turns the
    Ingress off, leaves it on, or there is no release (``None``)."""
    files = {
        f"charts/{name}/Chart.yaml": f"apiVersion: v2\nname: {name}\n",
        f"charts/{name}/values.yaml": VALUES,
        f"charts/{name}/templates/_helpers.tpl": HELPERS.replace("devices", name),
        f"charts/{name}/templates/service.yaml": SERVICE.replace("devices", name),
        f"charts/{name}/templates/ingress.yaml": ingress.replace("devices", name),
    }
    if enabled is not None:
        settings = ["stage=dev"] + ([] if enabled else ["ingress.enabled=false"])
        files["Tiltfile"] = f"k8s_yaml(helm('charts/{name}', name='{name}', set={settings!r}))\n"
    return files


def _read(template: str) -> dict[str, str]:
    [found] = ingress_references(PurePosixPath("charts/devices/templates/ingress.yaml"), template)
    return {k: v for k, v in found.meta.items() if k != PENDING_FACT}


def _ingress_outcomes(workspace: Workspace) -> list[str]:
    plan = ReferenceLinker(workspace.knowledge).prepare_global().http
    rows = workspace.database.execute(
        "SELECT id FROM unresolved_refs WHERE meta_json LIKE ?", (f'%"{HELM_INGRESS}"%',)
    ).fetchall()
    return [plan.deployments[row["id"]] for row in rows]


def _decisions(workspace: Workspace) -> dict[str, list[str]]:
    return {
        caller: [decision for decision, _ in entries]
        for caller, entries in decided(workspace, "caller").items()
    }


# -- what a template states ---------------------------------------------------------------


def test_an_ingress_in_the_subset_records_its_hosts_scheme_and_backend() -> None:
    assert _read(INGRESS) == {
        "chart": "charts/devices",
        "if": "ingress.enabled",
        "range": "ingress.groups",
        "hosts": json.dumps(
            [
                [
                    [
                        ["chart", ""],
                        ["text", "-"],
                        ["value", "stage"],
                        ["text", "."],
                        ["item", "host"],
                    ],
                    14,
                ]
            ]
        ),
        "listen": json.dumps([["https", 443]]),
        "scheme_at": "10",
        "backends": json.dumps(
            [['{{ include "devices.fullname" $ }}', "{{ $.Values.service.port }}", 21]]
        ),
    }


def test_an_unwrapped_ingress_reads_values_from_the_root() -> None:
    template = (
        INGRESS.replace(
            "{{- if .Values.ingress.enabled }}\n{{- range .Values.ingress.groups }}\n", ""
        )
        .replace("{{- end }}\n{{- end }}\n", "")
        .replace("{{ .host }}", "{{ .Values.domain }}")
        .replace("{{ .name }}", "web")
        .replace('[{"HTTPS":443}]', '[{"HTTP":80},{"HTTPS":443}]')
        .replace("path: /*", "path: /")
        .replace("ImplementationSpecific", "Prefix")
    )
    read = _read(template)
    assert "if" not in read and "range" not in read
    [[parts, _]] = json.loads(read["hosts"])
    assert parts[-1] == ["value", "domain"]
    assert json.loads(read["listen"]) == [["http", 80], ["https", 443]]


_GROUP = "    alb.ingress.kubernetes.io/group.name"
_RULE = '    - host: "{{ $.Chart.Name }}-{{ $.Values.stage }}.{{ .host }}"\n      http:\n'


@pytest.mark.parametrize(
    ("old", "new", "reason"),
    [
        ("{{- end }}\n{{- end }}", "{{- else }}\n{{- end }}\n{{- end }}", "not one `if`"),
        (
            "{{- range .Values.ingress.groups }}\n",
            "{{- range .Values.ingress.groups }}\n{{- range .Values.extra }}\n",
            "not one `if`",
        ),
        (
            "  annotations:\n",
            "  {{- with .Values.labels }}{{- end }}\n  annotations:\n",
            "not one `if`",
        ),
        (
            "{{- range .Values.ingress.groups }}",
            "{{- range $g := .Values.ingress.groups }}",
            "directly",
        ),
        ("{{ .host }}", "{{ .host | lower }}", "a host is not"),
        ("{{ .host }}", '{{ printf "%s" .host }}', "a host is not"),
        ("{{ $.Values.stage }}", "{{ .Values.stage }}", "a host is not"),
        ("{{ $.Chart.Name }}", "{{ $.Release.Name }}", "a host is not"),
        ('"{{ $.Chart.Name }}', '"*.{{ $.Chart.Name }}', "a host is not"),
        (
            "    alb.ingress.kubernetes.io/listen-ports: '[{\"HTTPS\":443}]'\n",
            "",
            "no listen-ports",
        ),
        ("'[{\"HTTPS\":443}]'", "'{{ $.Values.listen }}'", "not literal JSON"),
        ("'[{\"HTTPS\":443}]'", "'[{\"TCP\":443}]'", "not literal JSON"),
        ("path: /*", "path: /api/*", "a path is not"),
        ("ImplementationSpecific", "Prefix", "a path is not"),
        (
            _GROUP,
            f"    nginx.ingress.kubernetes.io/rewrite-target: /\n{_GROUP}",
            "rewrites or acts",
        ),
        (
            _GROUP,
            f"    alb.ingress.kubernetes.io/actions.forward: '{{}}'\n{_GROUP}",
            "rewrites or acts",
        ),
        ("number: {{ $.Values.service.port }}", "name: http", "rule field"),
        (
            "  rules:\n",
            "  rules:\n    {{- toYaml $.Values.rules | nindent 4 }}\n",
            "an action stands",
        ),
        (
            "  annotations:\n",
            "  annotations:\n    {{- toYaml $.Values.annotations | nindent 4 }}\n",
            "an action stands",
        ),
        (_RULE, "    - http:\n", "without a host"),
    ],
)
def test_an_ingress_outside_the_subset_is_unread_with_its_reason(
    old: str, new: str, reason: str
) -> None:
    assert old in INGRESS
    read = _read(INGRESS.replace(old, new))
    assert set(read) == {"chart", "unread"}
    assert reason in read["unread"]


# -- identities, patterns and the join ----------------------------------------------------


def test_a_rendered_host_reaches_its_repository_s_one_application(
    workspace: Workspace, tmp_path: Path
) -> None:
    built = build(
        workspace, tmp_path, {"devices": {**APP, **chart(enabled=True)}, "caller": CALLER}
    )
    assert {(caller, url) for caller, _, _, url in links(built)} == {
        ("dev_item", "GET https://devices-dev.int.example.com/items/{item_id}"),
        ("public_item", "GET https://devices-dev.example.com/items/{item_id}"),
    }
    [evidence] = [
        row["resolution"]["http"] for row in crossings(built) if row["caller"] == "dev_item"
    ]
    assert {k: evidence[k] for k in ("identity_source", "identity_in", "scheme_in")} == {
        "identity_source": "kubernetes-ingress",
        "identity_in": "charts/devices/templates/ingress.yaml:14",
        "scheme_in": "charts/devices/templates/ingress.yaml:10",
    }
    assert evidence["backend_in"] == "charts/devices/templates/ingress.yaml:21"
    assert evidence["bound_in"] == "Tiltfile:1"
    decisions = _decisions(built)
    # Another stage of the same chart: its own pattern, no deployment here resolves it.
    assert decisions["prd_item"] == ["http-deployment-binding-unresolved"]
    # The listener is HTTPS only; an item's host may span labels.
    assert decisions["plain_item"] == ["http-service-unmatched"]
    assert decisions["regional_item"] == ["http-deployment-binding-unresolved"]
    [(_, prd)] = decided(built, "caller")["prd_item"]
    assert prd["pattern"] == "https://devices-{stage}.{ingress.groups[].host}"
    assert prd["unresolved"] == "stage, ingress.groups[].host"
    assert prd["binding_outcome"] == "rendered"
    assert prd["source_outcome"] == "tied"


def test_a_release_that_disables_the_ingress_renders_no_identity(
    workspace: Workspace, tmp_path: Path
) -> None:
    built = build(
        workspace, tmp_path, {"devices": {**APP, **chart(enabled=False)}, "caller": CALLER}
    )
    assert links(built) == set()
    assert _ingress_outcomes(built) == [NOT_RENDERED]
    [(decision, evidence)] = decided(built, "caller")["dev_item"]
    assert decision == "http-deployment-binding-unresolved"
    assert evidence == {
        "called_at": "client.py:5",
        "pattern": "https://devices-{stage}.{ingress.groups[].host}",
        "identity_source": "kubernetes-ingress",
        "identity_in": "charts/devices/templates/ingress.yaml:14",
        "scheme_in": "charts/devices/templates/ingress.yaml:10",
        "backend_in": "charts/devices/templates/ingress.yaml:21",
        "unresolved": "stage, ingress.groups[].host",
        "bound_in": "Tiltfile:1",
        "binding_outcome": NOT_RENDERED,
        "source_outcome": "tied",
        "provider": "devices",
        # DF3c: the source is tied, so the one route of the request's method and shape
        "would_reach": [
            {
                "handler": "sym:devices:app.py:get_item:7",
                "route": "GET /items/{item_id}",
                "repository": "devices",
            }
        ],
    }


def test_an_ingress_whose_repository_serves_nothing_refuses_at_the_source(
    workspace: Workspace, tmp_path: Path
) -> None:
    deploy = {"pyproject.toml": '[project]\nname = "deploy"\n', **chart(enabled=True)}
    built = build(workspace, tmp_path, {"deploy": deploy, "caller": CALLER})
    assert links(built) == set()
    [(decision, evidence)] = decided(built, "caller")["dev_item"]
    assert decision == "http-deployment-source-unresolved"
    assert evidence["identity_source"] == "kubernetes-ingress"
    assert evidence["source_outcome"] == NOTHING_SERVED
    assert evidence["bound_in"] == "Tiltfile:1"
    # The binding hop fails first for a stage no deployment here sets; the source too.
    [(decision, evidence)] = decided(built, "caller")["prd_item"]
    assert decision == "http-deployment-binding-unresolved"
    assert evidence["source_outcome"] == NOTHING_SERVED


def test_another_chart_s_pattern_makes_a_rendered_host_ambiguous(
    workspace: Workspace, tmp_path: Path
) -> None:
    """A second repository's chart of the same name could serve the same host under a
    deployment no local source establishes: neither is chosen."""
    mirror = {"pyproject.toml": '[project]\nname = "mirror"\n', **chart(enabled=None)}
    built = build(
        workspace,
        tmp_path,
        {"devices": {**APP, **chart(enabled=True)}, "mirror": mirror, "caller": CALLER},
    )
    assert links(built) == set()
    [(decision, evidence)] = decided(built, "caller")["dev_item"]
    assert decision == "http-provider-ambiguous"
    assert evidence["provider"] == "devices; mirror"
    assert evidence["binding_outcome"] == "no release of its chart is bound by a literal name"


def test_a_hole_inside_a_label_never_spans_a_dot(workspace: Workspace, tmp_path: Path) -> None:
    template = INGRESS.replace("{{ .host }}", "int.example.com")
    built = build(
        workspace,
        tmp_path,
        {"devices": {**APP, **chart(enabled=False, ingress=template)}, "caller": CALLER},
    )
    decisions = _decisions(built)
    assert decisions["prd_item"] == ["http-deployment-binding-unresolved"]
    assert decisions["regional_item"] == ["http-service-unmatched"]


@pytest.mark.parametrize(
    ("host", "decision"),
    [
        # No chart name, no literal label: it would fit every host, so it is not used.
        ('"{{ .host }}"', "http-service-unmatched"),
        ('"{{ $.Values.stage }}.{{ .host }}"', "http-service-unmatched"),
        # A literal label anchors it.
        ('"{{ $.Values.stage }}.int.{{ .host }}"', "http-deployment-binding-unresolved"),
    ],
)
def test_a_pattern_needs_the_chart_s_name_or_a_literal_label(
    workspace: Workspace, tmp_path: Path, host: str, decision: str
) -> None:
    template = INGRESS.replace('"{{ $.Chart.Name }}-{{ $.Values.stage }}.{{ .host }}"', host)
    caller = {
        **CALLER,
        "client.py": d("""
            import requests


            def anywhere(item_id):
                return requests.get(f"https://prd.int.example.org/items/{item_id}", timeout=5)
        """),
    }
    built = build(
        workspace,
        tmp_path,
        {"devices": {**APP, **chart(enabled=False, ingress=template)}, "caller": caller},
    )
    assert _decisions(built) == {"anywhere": [decision]}


def test_a_backend_naming_another_service_gives_no_identity_or_pattern(
    workspace: Workspace, tmp_path: Path
) -> None:
    template = INGRESS.replace('name: {{ include "devices.fullname" $ }}\n', "name: payments\n")
    built = build(
        workspace,
        tmp_path,
        {"devices": {**APP, **chart(enabled=True, ingress=template)}, "caller": CALLER},
    )
    assert _ingress_outcomes(built) == [BACKEND_UNMATCHED]
    assert links(built) == set()
    assert set(_decisions(built)["dev_item"]) == {"http-service-unmatched"}
    plan = ReferenceLinker(built.knowledge).prepare_global().http
    roles = {json.loads(c.meta_json or "{}")[PENDING_FACT] for c in plan.identities}
    assert HTTP_UNRESOLVED_PATTERN not in roles


def test_an_http_listener_gives_an_http_origin(workspace: Workspace, tmp_path: Path) -> None:
    template = INGRESS.replace('[{"HTTPS":443}]', '[{"HTTP":80}]')
    built = build(
        workspace,
        tmp_path,
        {"devices": {**APP, **chart(enabled=True, ingress=template)}, "caller": CALLER},
    )
    assert {caller for caller, *_ in links(built)} == {"plain_item"}
    assert _decisions(built)["dev_item"] == ["http-service-unmatched"]


@pytest.mark.parametrize(
    ("overrides", "linked", "binding"),
    [
        # A scalar: the value at its path, and the host renders with it.
        (["stage=prd"], {"prd_item"}, RENDERED),
        # An object: everything under its path goes with it, `ingress.enabled` included.
        (["stage=dev", "ingress=off"], set(), NOT_RENDERED),
        # A list: its items go, so the `range` has nothing to iterate.
        (["stage=dev", "ingress.groups=none"], set(), BINDING_UNRESOLVED),
        # A path under a scalar: the scalar goes, so the host's `stage` reads nothing.
        (["stage.name=dev"], set(), BINDING_UNRESOLVED),
        # Index syntax is not read, so neither is the release.
        (["stage=dev", "ingress.groups[0].host=example.org"], set(), BINDING_UNRESOLVED),
    ],
)
def test_a_release_s_setting_replaces_what_the_values_held_at_above_and_under_its_path(
    workspace: Workspace, tmp_path: Path, overrides: list[str], linked: set[str], binding: str
) -> None:
    files = {
        **chart(enabled=None),
        "Tiltfile": f"k8s_yaml(helm('charts/devices', name='devices', set={overrides!r}))\n",
    }
    built = build(workspace, tmp_path, {"devices": {**APP, **files}, "caller": CALLER})
    assert {caller for caller, *_ in links(built)} == linked
    [(decision, evidence)] = decided(built, "caller")["dev_item"]
    assert decision == "http-deployment-binding-unresolved"
    assert evidence["binding_outcome"] == binding


def test_enabling_the_ingress_links_as_a_fresh_index_does(
    workspace: Workspace, settings, tmp_path: Path
) -> None:
    built = build(
        workspace, tmp_path, {"devices": {**APP, **chart(enabled=False)}, "caller": CALLER}
    )
    assert links(built) == set()
    root = tmp_path / "repos"
    edit(root, "devices", "Tiltfile", chart(enabled=True)["Tiltfile"])
    sync_all(built, root)
    assert {caller for caller, *_ in links(built)} == {"dev_item", "public_item"}
    assert_equals_fresh(built, settings, root, tmp_path, "ingress")


def test_a_pattern_of_many_holes_is_matched_in_bounded_time(
    workspace: Workspace, tmp_path: Path
) -> None:
    """Each hole may stand for any run of characters; a matcher that backtracks over the
    ways to split a long label between twelve holes would not finish."""
    holes = "".join("{{ ." + key + " }}" for key in "abcdefghijkl")
    template = INGRESS.replace(
        '"{{ $.Chart.Name }}-{{ $.Values.stage }}.{{ .host }}"', f'"x{holes}.example.com"'
    )
    label = "x" + "a" * 1500
    caller = {
        **CALLER,
        "client.py": (
            "import requests\n\n\ndef far(i):\n"
            f'    return requests.get(f"https://{label}.example.org/items/{{i}}", timeout=5)\n'
            "\n\ndef near(i):\n"
            f'    return requests.get(f"https://{label}.example.com/items/{{i}}", timeout=5)\n'
        ),
    }
    built = build(
        workspace,
        tmp_path,
        {"devices": {**APP, **chart(enabled=False, ingress=template)}, "caller": caller},
    )
    assert _decisions(built) == {
        "far": ["http-service-unmatched"],
        "near": ["http-deployment-binding-unresolved"],
    }


def _stored_chain(workspace: Workspace, caller: str) -> list[tuple[str, str, list, str, str]]:
    """One caller's stored decision chain: each hop's kind, status, places, reason, detail."""
    from repoatlas.graph.http_chain import chain_from_json

    keys = {
        row["key"]
        for row in workspace.database.execute(
            "SELECT key FROM entities WHERE qualified_name = ?", (caller,)
        )
    }
    [row] = [r for r in workspace.knowledge.list_http_call_decisions() if r.src_key in keys]
    return [
        (
            hop.kind,
            hop.status,
            [(p.path, p.line_start, p.role) for p in hop.provenance],
            hop.reason,
            hop.detail,
        )
        for hop in chain_from_json(row.chain_json)
    ]


def test_an_unrendered_ingress_s_chain_stops_at_the_deployment_binding(
    workspace: Workspace, tmp_path: Path
) -> None:
    built = build(
        workspace, tmp_path, {"devices": {**APP, **chart(enabled=False)}, "caller": CALLER}
    )
    chain = _stored_chain(built, "dev_item")
    assert [(kind, status) for kind, status, *_ in chain] == [
        ("caller", "proven"),
        ("origin", "proven"),
        ("service_identity", "proven"),
        ("deployment_binding", "unresolved"),
        ("source_binding", "proven"),
        ("route", "not_reached"),
        ("handler", "not_reached"),
    ]
    _, _, identity, _, pattern = chain[2]
    assert identity == [
        ("charts/devices/templates/ingress.yaml", 14, "identity_in"),
        ("charts/devices/templates/ingress.yaml", 10, "scheme_in"),
        ("charts/devices/templates/ingress.yaml", 21, "backend_in"),
    ]
    assert pattern == "https://devices-{stage}.{ingress.groups[].host}"
    _, _, binding, reason, detail = chain[3]
    assert (binding, reason) == ([("Tiltfile", 1, "binding_in")], "binding_unresolved")
    assert detail == f"stage, ingress.groups[].host; {NOT_RENDERED}"
    # The tie is that every route comes from one application: its module, with no line.
    assert chain[4][2] == [("app.py", None, "source_in")]


@pytest.mark.parametrize(
    ("enabled", "status", "failed", "reason"),
    [
        (False, "http-deployment-binding-unresolved", 3, "binding_unresolved"),
        (True, "http-deployment-source-unresolved", 4, "source_unresolved"),
    ],
)
def test_the_inventory_lists_a_deployment_refusal_as_itself(
    workspace: Workspace, tmp_path: Path, enabled: bool, status: str, failed: int, reason: str
) -> None:
    """Never folded into service-unmatched: the crossing keeps its own decision, and the
    hop that failed keeps its reason even where another repository's places are withheld."""
    from repoatlas.capabilities.service import CapabilityService
    from repoatlas.models.capability import CapabilityContext, CapabilityScope
    from repoatlas.models.http_inventory import CrossingStatus, HttpOperationsRequest

    provider = (
        {**APP, **chart(enabled=False)}
        if not enabled
        else {"pyproject.toml": '[project]\nname = "deploy"\n', **chart(enabled=True)}
    )
    built = build(workspace, tmp_path, {"devices": provider, "caller": CALLER})
    response = CapabilityService(built).http_operations(
        HttpOperationsRequest(
            context=CapabilityContext(scope=CapabilityScope.REPOSITORY, repository="caller"),
            crossing=(CrossingStatus(status),),
        )
    )
    rows = [row for row in response.operations if row.entity and ":dev_item:" in row.entity]
    [crossing] = [c for row in rows for c in row.crossings]
    assert crossing.status.value == status
    hop = crossing.chain[failed]
    assert (hop.status, hop.reason) == ("unresolved", reason)
    assert set(response.summary.by_crossing) == {status}


def test_an_ingress_reaching_no_code_has_a_chain_that_stops_at_the_source(
    workspace: Workspace, tmp_path: Path
) -> None:
    deploy = {"pyproject.toml": '[project]\nname = "deploy"\n', **chart(enabled=True)}
    built = build(workspace, tmp_path, {"deploy": deploy, "caller": CALLER})
    chain = _stored_chain(built, "dev_item")
    assert [(kind, status) for kind, status, *_ in chain][2:] == [
        ("service_identity", "proven"),
        ("deployment_binding", "proven"),
        ("source_binding", "unresolved"),
        ("route", "not_reached"),
        ("handler", "not_reached"),
    ]
    assert chain[3][2] == [("Tiltfile", 1, "binding_in")]
    assert (chain[4][3], chain[4][4]) == ("source_unresolved", NOTHING_SERVED)
