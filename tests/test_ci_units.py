"""DF1's pure readers on invented inputs: image references, the action tables, the shell
reading of a CI ``run`` script, and what a fact may never carry (DF1_PHASE_A.md §3-§5, §11)."""

from __future__ import annotations

import pytest
from repoatlas.models.container_image import (
    EXPR,
    IMAGE_UNRESOLVED,
    REGISTRY_UNRESOLVED,
    RESOLVED,
    TEXT,
    VAR,
    parse_reference,
)
from repoatlas.models.container_image import (
    WITHHELD as WITHHELD_PART,
)
from repoatlas.parsers.ci_semantics import BUILTIN_BUILD, INERT_ACTION, UNSUPPORTED, classify_action
from repoatlas.parsers.ci_shell import (
    DOCKER_BUILD,
    HELM,
    delegated,
    flag_values,
    read_script,
    recognised,
)
from repoatlas.parsers.withhold import WITHHELD, credential_named, guarded, secret_bearing


def text(value: str) -> list[list[str]]:
    return [[TEXT, value]]


# ---------------------------------------------------------------- image references
@pytest.mark.parametrize(
    ("reference", "host", "port", "path", "tag", "digest"),
    [
        (
            "registry.sample.example:5000/sample/api:1.0",
            "registry.sample.example",
            5000,
            "sample/api",
            "1.0",
            None,
        ),
        ("localhost:5000/api", "localhost", 5000, "api", None, None),
        (
            "ghcr.io/org/team/api@sha256:" + "a" * 64,
            "ghcr.io",
            None,
            "org/team/api",
            None,
            "sha256:" + "a" * 64,
        ),
        (
            "registry.sample.example/api:2@sha256:" + "b" * 64,
            "registry.sample.example",
            None,
            "api",
            "2",
            "sha256:" + "b" * 64,
        ),
    ],
)
def test_a_reference_splits_by_the_grammar_not_by_colons(
    reference, host, port, path, tag, digest
) -> None:
    found = parse_reference(text(reference))
    assert (found.state, found.host, found.port, found.path, found.tag, found.digest) == (
        RESOLVED,
        host,
        port,
        path,
        tag,
        digest,
    )
    assert found.join_key() == (f"{host}:{port}/{path}" if port else f"{host}/{path}")


def test_an_implied_registry_is_docker_hub_and_flagged() -> None:
    found = parse_reference(text("python:3.12-slim"))
    assert (found.host, found.path, found.tag, found.registry_implicit) == (
        "docker.io",
        "library/python",
        "3.12-slim",
        True,
    )
    assert parse_reference(text("org/app")).path == "org/app"


def test_a_variable_tag_keeps_the_image_and_marks_the_version() -> None:
    found = parse_reference(
        [[TEXT, "registry.sample.example:5000/sample/api:"], [VAR, "CIRCLE_SHA1"]]
    )
    assert found.state == RESOLVED and found.tag is None and found.version_unresolved
    assert found.join_key() == "registry.sample.example:5000/sample/api"


def test_an_unresolved_registry_is_never_dropped_to_make_a_key() -> None:
    found = parse_reference([[VAR, "REGISTRY"], [TEXT, "/ns-b/svc:1"]])
    assert found.state == REGISTRY_UNRESOLVED and found.join_key() is None
    assert found.path == "ns-b/svc"


@pytest.mark.parametrize(
    "parts",
    [
        [[EXPR, "steps.meta.outputs.tags"]],
        [[TEXT, "ghcr.io/"], [EXPR, "github.repository"], [TEXT, ":latest"]],
        text("Registry.example/UPPER/case"),
        text("registry.example:99999/api"),
    ],
)
def test_an_unresolved_or_invalid_image_has_no_key(parts) -> None:
    found = parse_reference(parts)
    assert found.state == IMAGE_UNRESOLVED and found.join_key() is None


def test_two_registries_with_one_path_are_two_images() -> None:
    one = parse_reference(text("registry.sample.example:5000/sample/api"))
    two = parse_reference(text("registry-two.sample.example:5000/sample/api"))
    assert one.join_key() != two.join_key()


# ---------------------------------------------------------------- the action tables
@pytest.mark.parametrize(
    ("reference", "kind"),
    [
        ("actions/checkout@v4", INERT_ACTION),
        ("actions/checkout@v4.1.2", INERT_ACTION),
        ("docker/login-action@v3", INERT_ACTION),
        ("docker/build-push-action@v6", BUILTIN_BUILD),
        ("docker/build-push-action@v5.3.0", BUILTIN_BUILD),
        # an unlisted major, a SHA, a branch, another owner's setup action: no semantics
        ("actions/checkout@v7", UNSUPPORTED),
        ("docker/build-push-action@v4", UNSUPPORTED),
        ("actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683", UNSUPPORTED),
        ("docker/build-push-action@master", UNSUPPORTED),
        ("pypa/gh-action-pypi-publish@release/v1", UNSUPPORTED),
        ("astral-sh/setup-uv@v5", UNSUPPORTED),
        ("docker://alpine:3", UNSUPPORTED),
        ("./.github/actions/deploy", UNSUPPORTED),
    ],
)
def test_actions_are_recognised_by_exact_revision_only(reference: str, kind: str) -> None:
    assert classify_action(reference) == kind


# ---------------------------------------------------------------- shell
def test_top_level_commands_and_list_members_are_read_nested_ones_are_marked() -> None:
    script = read_script(
        "docker build -t r.example/a:$TAG .\n"
        "docker push r.example/a:$TAG && echo pushed || true\n"
        "for e in qa prd; do\n  helm upgrade --install a-$e charts/a\ndone\n"
        "X=$(docker build .)\n"
        "docker run --rm a | helm upgrade --install b charts/b\n",
        first_line=10,
    )
    read = [(recognised(c), c.nested, c.line) for c in script.commands if recognised(c)]
    assert read == [
        (DOCKER_BUILD, False, 10),
        ("docker-push", False, 11),
        (HELM, True, 13),
        (DOCKER_BUILD, True, 15),
        (HELM, True, 16),
    ]


def test_ci_expressions_become_parts_and_a_secret_one_withheld() -> None:
    [command] = read_script(
        "helm upgrade --install a charts/a --set image.tag=${{ github.sha }},db.pw=${{ secrets.DB }}"  # noqa: E501 - sample text, kept on one line to read as written
    ).commands
    [value] = flag_values(command, None, "--set")
    assert value == [
        [TEXT, "image.tag="],
        [EXPR, "github.sha"],
        [TEXT, ",db.pw="],
        [WITHHELD_PART, ""],
    ]


def test_delegation_names_only_a_local_script() -> None:
    commands = read_script(
        "./scripts/deploy.sh prod\nbash scripts/x.sh\nbash -c 'echo'\npython deploy.py\n"
    ).commands
    assert [delegated(c) for c in commands] == ["./scripts/deploy.sh", "scripts/x.sh", None, None]


# ---------------------------------------------------------------- withholding
@pytest.mark.parametrize(
    ("name", "credential"),
    [
        ("db.password", True),
        ("auth.rootPassword", True),
        ("API_TOKEN", True),
        ("secrets.awsRegion", True),
        ("openaiApiKey", True),
        ("api_key", True),
        ("clientSecret", True),
        ("QUERY_PAGE_LIMIT", False),
        ("maxTokens", False),
        ("image.repository", False),
        ("keyring", False),
    ],
)
def test_a_credential_is_named_whatever_the_naming_style(name: str, credential: bool) -> None:
    assert credential_named(name) is credential


def test_userinfo_and_secret_references_are_withheld_whatever_the_name() -> None:
    assert secret_bearing("postgresql://intake:hunter2@db:5432/intake")
    assert secret_bearing("${{ secrets.TOKEN }}")
    assert not secret_bearing("https://query.test.sample.example")
    assert guarded("env.URL", "https://u:p@host") == WITHHELD
    assert guarded("env.URL", "https://host") == "https://host"
