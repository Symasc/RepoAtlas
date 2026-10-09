# ruff: noqa: E501 - the sample configurations hold their lines as a CI system or Helm reads them
"""DF2's parser additions and evidence units (DF2_PHASE_A.md §6, §10, §15)."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

import yaml
from repoatlas.capabilities.impact import deployment_qualifiers, http_crossing
from repoatlas.discovery.languages import detect_language
from repoatlas.graph import http_chain
from repoatlas.graph.ci_facts import DecisionRows
from repoatlas.models.container_image import EXPR, TEXT, VAR, version_parts
from repoatlas.models.parsing import EdgeType, EntityType
from repoatlas.parsers.ci_config import ci_references
from repoatlas.parsers.ci_shell import read_script
from repoatlas.parsers.deploy_helm import (
    MAX_REFERENCED,
    read_values,
    referenced_paths,
    service_references,
)
from repoatlas.parsers.dockerfile import DockerfileParser
from repoatlas.storage.protocols import FileRow, UnresolvedRefRecord

# -- what may set a variable (E3) ------------------------------------------------------


def test_a_script_s_assignments_are_recorded_by_name_and_env_files_noticed() -> None:
    read = read_script(
        "X=1\nexport TAG=$(date)\ndeclare -x A B=2\nfor V in a b; do :; done\nread -r R\nunset U\n"
        "CIRCLE_SHA1=x helm upgrade a b\necho K=v >> $GITHUB_ENV\n"
    )
    assert read.assigned == {"X", "TAG", "A", "B", "V", "R", "U", "CIRCLE_SHA1"}
    assert read.env_file
    plain = read_script("helm upgrade --install a c --set image.tag=$CIRCLE_SHA1")
    assert (plain.assigned, plain.env_file) == (set(), False)


def claims(path: str, text: str) -> list[dict[str, str]]:
    refs, _ = ci_references(PurePosixPath(path), list(yaml.compose_all(text)))
    return [ref.meta for ref in refs]


def test_a_ci_claim_names_the_symbols_its_scopes_or_script_may_set() -> None:
    circle = """\
version: 2.1
executors:
  ex:
    docker:
      - image: cimg/base:2024.01
        environment:
          FROM_IMAGE: x
jobs:
  build:
    executor: ex
    environment:
      CIRCLE_SHA1: $OTHER
    steps:
      - run: docker build -t r.example:5000/a/b:$CIRCLE_SHA1 -f d/Dockerfile $CTX && docker push r.example:5000/a/b:$CIRCLE_SHA1
      - run:
          command: helm upgrade --install a charts/a --set image.tag=$STEPV --set other=$FROM_IMAGE
          environment:
            STEPV: $Y
      - run: echo 'export Z=1' >> $BASH_ENV
workflows:
  w:
    jobs: [build]
"""
    found = {meta["pending_fact"]: meta for meta in claims(".circleci/config.yml", circle)}
    build, release = found["ci-image-build"], found["ci-helm-release"]
    assert json.loads(build["overridable"]) == ["CIRCLE_SHA1"]
    assert json.loads(release["overridable"]) == ["FROM_IMAGE", "STEPV"]
    assert build["env_written"] == release["env_written"] == "true"
    # an unresolved context is not the repository root
    assert (build["context"], build["context_resolved"]) == ("", "false")
    assert json.loads(build["refs"])[0]["pushed_line"] == 14


def test_a_builtin_build_is_published_at_its_own_step() -> None:
    github = """\
name: b
on: {push: {branches: [main]}}
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: docker/build-push-action@v6
        with:
          push: true
          tags: r.example/a/b:${{ github.sha }}
"""
    [build] = claims(".github/workflows/b.yml", github)
    [ref] = json.loads(build["refs"])
    assert ref["pushed_line"] == 7 and build["context_resolved"] == "true"
    assert json.loads(build["overridable"]) == [] and build["env_written"] == "false"


# -- charts and values (E1, E2) --------------------------------------------------------


def test_a_service_records_its_selector_and_target_ports() -> None:
    template = """\
kind: Service
metadata:
  name: {{ include "x.fullname" . }}
spec:
  ports:
    - port: 80
      targetPort: http
    - port: 9090
  selector:
    {{- include "x.selectorLabels" . | nindent 4 }}
    app.kubernetes.io/component: api
"""
    [meta] = [
        ref.meta for ref in service_references(PurePosixPath("charts/x/templates/s.yaml"), template)
    ]
    assert json.loads(meta["selector"]) == [
        ["include", "x.selectorLabels"],
        ["app.kubernetes.io/component", "api"],
    ]
    assert json.loads(meta["target_ports"]) == [["80", "http"], ["9090", None]]
    computed = template.replace(
        "    app.kubernetes.io/component: api\n", "    {{- toYaml .Values.labels | nindent 4 }}\n"
    )
    [meta] = [
        ref.meta for ref in service_references(PurePosixPath("charts/x/templates/s.yaml"), computed)
    ]
    assert json.loads(meta["selector"]) == "unread"


def test_a_values_file_records_leaf_lines_and_what_it_clears() -> None:
    read = read_values(yaml.compose("a:\n  b: 1\n  c: null\n  d: []\n  e: {}\nl:\n  - x\n"))
    assert read.lines == {"a.b": 2, "l[0]": 7}
    assert read.cleared == [["a.c", "null"], ["a.d", "list"]]
    # aliases make a small file a large tree: the walk is bounded, and says it was cut
    bomb = "a: &a [1, 1, 1, 1, 1, 1, 1, 1, 1, 1]\n" + "".join(
        f"{c}: &{c} [*{p}, *{p}, *{p}, *{p}, *{p}, *{p}, *{p}, *{p}, *{p}, *{p}]\n"
        for p, c in zip("abcde", "bcdef", strict=True)
    )
    assert read_values(yaml.compose(bomb)).truncated


def test_only_files_a_release_names_and_no_values_star_file_are_referenced() -> None:
    meta = {
        "values": json.dumps(
            [["env/a.yaml", "env/a.yaml"], ["env/values-b.yaml", "x"], [None, "env/<expr>/c.yaml"]]
        )
    }
    found = referenced_paths(
        [("deploy.yml", 5, meta), ("deploy.yml", 9, meta), ("other.yml", 3, meta)]
    )
    assert found == {"env/a.yaml": ["deploy.yml:5", "deploy.yml:9", "other.yml:3"]}
    many = {"values": json.dumps([[f"env/{i:03d}.yaml", "x"] for i in range(MAX_REFERENCED + 5)])}
    assert len(referenced_paths([("deploy.yml", 1, many)])) == MAX_REFERENCED


def test_a_dot_dockerfile_of_any_case_is_a_dockerfile() -> None:
    parser = DockerfileParser()
    for name in ("deploy/api.Dockerfile", "deploy/api.dockerfile", "Dockerfile", "Dockerfile.dev"):
        assert parser.supports(Path(name)) and detect_language(name) == "Dockerfile", name
    assert not parser.supports(Path("deploy/notes.md"))


def test_a_reference_s_tag_and_digest_keep_their_parts() -> None:
    assert version_parts([[TEXT, "r.example:5000/a/b:"], [VAR, "CIRCLE_SHA1"]]) == (
        [[VAR, "CIRCLE_SHA1"]],
        None,
    )
    assert version_parts([[TEXT, "r.example:5000/a/b"]]) == (None, None)
    assert version_parts([[TEXT, "a/b:v"], [EXPR, "github.sha"], [TEXT, "@sha256:ab"]]) == (
        [[TEXT, "v"], [EXPR, "github.sha"]],
        [[TEXT, "sha256:ab"]],
    )


# -- the chain (§6.2) ------------------------------------------------------------------


def test_a_source_place_serializes_exactly_as_before_and_a_declared_one_says_so() -> None:
    plain = http_chain.Provenance(1, "a.py", 3, 3, "called_at")
    assert plain.as_json() == {
        "repo_id": 1,
        "path": "a.py",
        "line_start": 3,
        "line_end": 3,
        "role": "called_at",
    }
    declared = http_chain.Provenance(9, "orbs.yml", 3, 3, "declared_in", "declared")
    assert declared.as_json()["basis"] == "declared"
    chain = (http_chain.Hop(http_chain.DEPLOYMENT_BINDING, http_chain.DECLARED, (declared,)),)
    assert http_chain.chain_from_json(http_chain.chain_json(chain)) == chain


def claim(meta: dict[str, str], repo_id: int = 2) -> UnresolvedRefRecord:
    return UnresolvedRefRecord(
        id=1,
        repo_id=repo_id,
        file_id=10,
        src_id=100,
        edge_type=EdgeType.EXPOSES_HTTP_ENDPOINT,
        target_kind=EntityType.HTTP_ENDPOINT,
        target_key="GET https://a.example/x",
        line_start=4,
        meta_json=json.dumps(meta),
    )


ROW = FileRow(
    file_id=10,
    file_entity_id=11,
    repo_id=2,
    repo_name="api",
    repo_slug="api",
    repo_path=Path("/r/api"),
    rel_path="api/app.py",
)


def test_each_place_is_filed_under_the_repository_and_basis_its_claim_names() -> None:
    meta = {
        "identity_in": "charts/web/templates/ingress.yaml:10",
        "identity_in_repo": "1",
        "binding_in": "ci.yml:12",
        "binding_in_repo": "1",
        "binding_in_basis": "declared",
        "binding_basis": "declared",
        "declared_in": "orbs.yml:3",
        "declared_in_repo": "9",
        "declared_in_basis": "declared",
        "source_in": "deploy/Dockerfile:4; api/app.py",
    }
    evidence = http_chain.identity_evidence(claim(meta), meta, ROW)
    assert [(p.repo_id, p.role) for p in evidence.identity] == [(1, "identity_in")]
    assert [(p.repo_id, p.role, p.basis) for p in evidence.binding] == [
        (1, "binding_in", "declared"),
        (9, "declared_in", "declared"),
    ]
    assert [(p.repo_id, p.path) for p in evidence.source] == [
        (2, "deploy/Dockerfile"),
        (2, "api/app.py"),
    ]
    assert (evidence.binding_status, evidence.source_status) == (
        http_chain.DECLARED,
        http_chain.PROVEN,
    )
    # an M-series claim: every place in its own repository, every hop proven
    plain = http_chain.identity_evidence(
        claim({}), {"identity_in": "c.yaml:1", "binding_in": "Tiltfile:3"}, ROW
    )
    assert {p.repo_id for p in (*plain.identity, *plain.binding)} == {2}
    assert (plain.binding_status, plain.source_status) == (http_chain.PROVEN, http_chain.PROVEN)


def test_two_claims_on_one_line_keep_two_decisions() -> None:
    rows = DecisionRows()
    origin = ROW
    first = claim({}).model_copy(update={"id": 1, "target_key": "a"})
    second = claim({}).model_copy(update={"id": 2, "target_key": "b"})
    assert rows.add("ci_release", origin, first, "s@12", "bound", {}) == "s@12"
    assert rows.add("ci_release", origin, second, "s@12", "bound", {}) == "s@12~2"
    assert rows.add("ci_release", origin, first, "s@12", "bound", {}) == "s@12"
    assert len(rows.rows) == 2


# -- impact (§6.4) ---------------------------------------------------------------------


def test_impact_spells_each_place_in_its_repository_and_says_how_it_is_configured() -> None:
    record = {
        "method": "GET",
        "url": "https://a.example/x",
        "endpoint": "GET https://a.example/x",
        "deployment": "https://a.example",
        "provider": "api",
        "consumer": "client",
        "called_at": "client.py:5",
        "served_at": "api/app.py:8",
        "identity_source": "kubernetes-ingress",
        "deployed_by": "deployer",
        "built_by": "api",
        "binding_basis": "declared",
        "build_basis": "builtin",
        "version_relation": "unknown",
        "identity_in": "charts/web/templates/ingress.yaml:10",
        "binding_in": "ci.yml:12",
        "published_in": "ci.yml:8; ci.yml:9",
        "declared_in": "orbs.yml:3",
    }
    crossing = http_crossing({"http": record}, "client")
    assert crossing is not None
    assert crossing.identity_in == "deployer/charts/web/templates/ingress.yaml:10"
    assert crossing.binding_in == "deployer/ci.yml:12"
    assert crossing.published_in == "api/ci.yml:8; api/ci.yml:9"
    assert crossing.declared_in == "@declarations/orbs.yml:3"
    assert (crossing.binding_basis, crossing.build_basis, crossing.version_relation) == (
        "declared",
        "builtin",
        "unknown",
    )
    qualifiers = deployment_qualifiers(record)
    assert qualifiers == [
        "deployment configured in deployer (declared)",
        "image built in api (builtin)",
        "image version not established",
    ]
    for text in qualifiers:
        assert not any(
            word in text for word in ("deployed", "running", "pushed", "published", "this build")
        )
    # any other link reads exactly as before
    assert deployment_qualifiers({"provider": "api"}) == []
    plain = http_crossing(
        {
            "http": {
                k: v
                for k, v in record.items()
                if k
                not in (
                    "deployed_by",
                    "built_by",
                    "binding_basis",
                    "build_basis",
                    "version_relation",
                    "identity_in",
                    "binding_in",
                    "published_in",
                    "declared_in",
                )
            }
        },
        "client",
    )
    assert plain is not None and plain.deployment_repository is None and plain.identity_in is None


#: ROADMAP §13's wording audit: what configuration never shows as a fact about runtime.
WORDING = (
    "currently deployed",
    "is running",
    "this build",
    "created this record",
    "was pushed",
    "is published",
    "exists in the registry",
    "unpublished",
    "never published",
)


def test_no_reason_or_outcome_text_claims_a_push_a_registry_or_a_running_version() -> None:
    from repoatlas.graph import deploy_identity
    from repoatlas.graph.deploy_artifacts import DOCKERFILE_UNREAD_TIE

    texts = [
        *http_chain.REASONS.values(),
        DOCKERFILE_UNREAD_TIE,
        *(
            getattr(deploy_identity, name)
            for name in (
                "IMAGE_UNREAD",
                "REGISTRY_UNRESOLVED",
                "PUBLISHER_UNRESOLVED",
                "ARTIFACT_AMBIGUOUS",
                "TAG_UNMATCHED",
                "WORKLOAD_UNMATCHED",
                "NAME_DEPENDENT",
            )
        ),
    ]
    for text in texts:
        assert not any(phrase in text.lower() for phrase in WORDING), text
