"""OpenAPI 3 and Swagger 2 documents as static HTTP facts (HTTP linkage).

A specification is the one place a repository states *where* an API is declared
to live, so it is the first-class source for deployments and endpoints. The
questions here are the ones a careless reader gets wrong: which keys are
operations, which base applies to which operation, what a composed identity may
cite, and when a document is not a specification at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from repoatlas.models.parsing import EdgeType, EntityType, ParsedFile
from repoatlas.parsers.http_openapi import MAX_OPERATIONS
from repoatlas.parsers.structured import StructuredParser

_HTTP_KINDS = {EntityType.HTTP_ROUTE, EntityType.HTTP_DEPLOYMENT, EntityType.HTTP_ENDPOINT}

Fact = tuple[str, str, int | None, int | None, str]


def _parse(text: str, name: str = "openapi.yaml") -> ParsedFile:
    return StructuredParser().parse(Path(name), text)


def _facts(parsed: ParsedFile) -> set[Fact]:
    """``(edge, target, line_start, line_end, provenance)`` for every HTTP fact.

    Composed facts carry an anchor line here; the linker is what drops it, so
    the parser test reports the provenance and ignores the anchor.
    """
    found: set[Fact] = set()
    for reference in parsed.references:
        if reference.target_kind not in _HTTP_KINDS:
            continue
        composed = reference.meta["provenance"] == "composed"
        found.add(
            (
                str(reference.edge_type),
                reference.target_key,
                None if composed else reference.line_start,
                None if composed else reference.line_end,
                reference.meta["provenance"],
            )
        )
    return found


def _targets(parsed: ParsedFile, edge: EdgeType) -> set[str]:
    return {r.target_key for r in parsed.references if r.edge_type is edge}


BASIC = """\
openapi: 3.0.3
info:
  title: svc-a
  version: "1"
servers:
  - url: https://api.example.com
paths:
  /hello/me:
    summary: the caller
    parameters: []
    get:
      responses:
        "200":
          description: ok
    post:
      responses:
        "201":
          description: made
"""


def test_an_openapi_3_document_declares_routes_deployment_and_endpoints() -> None:
    assert _facts(_parse(BASIC)) == {
        ("SERVES_HTTP_ROUTE", "GET /hello/me", 8, 11, "exact"),
        ("SERVES_HTTP_ROUTE", "POST /hello/me", 8, 15, "exact"),
        ("DECLARES_HTTP_DEPLOYMENT", "https://api.example.com", 6, 6, "exact"),
        ("EXPOSES_HTTP_ENDPOINT", "GET https://api.example.com/hello/me", None, None, "composed"),
        ("EXPOSES_HTTP_ENDPOINT", "POST https://api.example.com/hello/me", None, None, "composed"),
    }


def test_path_item_fields_that_are_not_operations_are_not_routes() -> None:
    routes = _targets(_parse(BASIC), EdgeType.SERVES_HTTP_ROUTE)
    assert not any(route.split(" ")[0] in {"SUMMARY", "PARAMETERS"} for route in routes)


def test_every_http_fact_targets_a_shared_kind_and_names_no_repository() -> None:
    for reference in _parse(BASIC).references:
        if reference.target_kind in _HTTP_KINDS:
            assert reference.source_symbol is None
            assert "svc-a" not in reference.target_key


def test_the_structured_config_keys_are_still_indexed() -> None:
    """Additive: the specification is still configuration with line provenance."""
    names = {symbol.qualified_name for symbol in _parse(BASIC).symbols}
    assert {"openapi", "servers", "paths", "paths./hello/me.get"} <= names


def test_multiple_servers_yield_one_route_and_one_endpoint_per_server() -> None:
    text = BASIC.replace(
        "  - url: https://api.example.com\n",
        "  - url: https://api.example.com\n  - url: https://api-eu.example.com\n",
    )
    parsed = _parse(text)
    assert _targets(parsed, EdgeType.DECLARES_HTTP_DEPLOYMENT) == {
        "https://api.example.com",
        "https://api-eu.example.com",
    }
    assert {
        "GET https://api.example.com/hello/me",
        "GET https://api-eu.example.com/hello/me",
    } <= _targets(parsed, EdgeType.EXPOSES_HTTP_ENDPOINT)


@pytest.mark.parametrize(
    "server",
    [
        "https://{environment}.example.com",
        "https://api.example.com/{version}",
        "/v1",
        "v1",
        "https://user:secret@api.example.com",
        "https://api.example.com/v1?token=abc",
        "",
    ],
)
def test_an_unusable_server_still_yields_routes_but_no_deployment(server: str) -> None:
    parsed = _parse(BASIC.replace("https://api.example.com", server))
    assert _targets(parsed, EdgeType.SERVES_HTTP_ROUTE) == {"GET /hello/me", "POST /hello/me"}
    assert _targets(parsed, EdgeType.DECLARES_HTTP_DEPLOYMENT) == set()
    assert _targets(parsed, EdgeType.EXPOSES_HTTP_ENDPOINT) == set()


def test_no_servers_means_routes_only() -> None:
    text = "openapi: 3.1.0\npaths:\n  /x:\n    get: {}\n"
    parsed = _parse(text)
    assert _targets(parsed, EdgeType.SERVES_HTTP_ROUTE) == {"GET /x"}
    assert _targets(parsed, EdgeType.EXPOSES_HTTP_ENDPOINT) == set()


OVERRIDES = """\
openapi: 3.0.0
servers:
  - url: https://root.example.com
paths:
  /a:
    get: {}
  /b:
    servers:
      - url: https://path.example.com
    get: {}
    put:
      servers:
        - url: https://op.example.com/v2/
      responses: {}
  /c:
    get:
      servers:
        - url: https://{region}.example.com
"""


def test_the_most_specific_servers_win_and_are_never_bypassed() -> None:
    endpoints = _targets(_parse(OVERRIDES), EdgeType.EXPOSES_HTTP_ENDPOINT)
    assert endpoints == {
        "GET https://root.example.com/a",
        "GET https://path.example.com/b",
        "PUT https://op.example.com/v2/b",
    }
    # `/c` declares its own servers, none usable: no fallback to the root.
    assert not any(endpoint.endswith("/c") for endpoint in endpoints)


def test_every_declared_usable_server_is_a_deployment() -> None:
    assert _targets(_parse(OVERRIDES), EdgeType.DECLARES_HTTP_DEPLOYMENT) == {
        "https://root.example.com",
        "https://path.example.com",
        "https://op.example.com/v2",
    }


def test_a_ref_path_item_is_left_unresolved() -> None:
    text = (
        "openapi: 3.0.0\nservers:\n  - url: https://x.example\npaths:\n"
        "  /remote:\n    $ref: 'https://evil.example/paths.yaml#/x'\n"
        "  /local:\n    $ref: '#/components/pathItems/x'\n    get: {}\n"
        "  /kept:\n    get: {}\n"
    )
    assert _targets(_parse(text), EdgeType.SERVES_HTTP_ROUTE) == {"GET /kept"}


@pytest.mark.parametrize(
    "text",
    [
        # `paths:` alone is ordinary configuration.
        "paths:\n  /x:\n    get: {}\n",
        "name: ci\npaths:\n  /x:\n    get: {}\nservers:\n  - url: https://x.example\n",
        # Not version 3, and not Swagger 2.
        "openapi: 2.0\npaths:\n  /x:\n    get: {}\n",
        "openapi: '4.0'\npaths:\n  /x:\n    get: {}\n",
        "swagger: '1.2'\npaths:\n  /x:\n    get: {}\n",
        # Claims to be both.
        "openapi: 3.0.0\nswagger: '2.0'\npaths:\n  /x:\n    get: {}\n",
        # Signature present but `paths` is not a mapping.
        "openapi: 3.0.0\npaths: [/x]\n",
    ],
)
def test_a_document_without_a_clean_signature_is_not_a_specification(text: str) -> None:
    assert _facts(_parse(text)) == set()


def test_a_path_item_with_a_duplicate_key_is_not_read() -> None:
    text = "openapi: 3.0.0\npaths:\n  /x:\n    get: {}\n    get: {}\n  /y:\n    get: {}\n"
    assert _targets(_parse(text), EdgeType.SERVES_HTTP_ROUTE) == {"GET /y"}


def test_paths_that_are_not_routes_are_skipped() -> None:
    text = "openapi: 3.0.0\npaths:\n  x-internal:\n    get: {}\n  /ok:\n    get: {}\n"
    assert _targets(_parse(text), EdgeType.SERVES_HTTP_ROUTE) == {"GET /ok"}


def test_parameterized_paths_are_kept_as_literal_templates() -> None:
    text = "openapi: 3.0.0\npaths:\n  /users/{id}:\n    get: {}\n"
    assert _targets(_parse(text), EdgeType.SERVES_HTTP_ROUTE) == {"GET /users/{id}"}


def test_documentation_urls_are_not_deployments() -> None:
    """Swagger UI, docs sites and terms links describe an API; they do not host one."""
    text = (
        "openapi: 3.0.0\ninfo:\n  termsOfService: https://docs.example.com/terms\n"
        "externalDocs:\n  url: https://docs.example.com/swagger-ui\n"
        "paths:\n  /docs:\n    get:\n      externalDocs:\n        url: https://docs.example.com\n"
    )
    parsed = _parse(text)
    assert _targets(parsed, EdgeType.DECLARES_HTTP_DEPLOYMENT) == set()
    assert _targets(parsed, EdgeType.SERVES_HTTP_ROUTE) == {"GET /docs"}


SWAGGER = """\
swagger: "2.0"
info:
  title: legacy
  version: "1"
schemes:
  - https
host: api.example.com
basePath: /v1
paths:
  /users:
    get:
      responses:
        "200":
          description: ok
"""


def test_swagger_2_keeps_the_route_and_the_base_path_apart() -> None:
    """The route is the ``paths`` key; the deployment carries ``basePath``."""
    assert _facts(_parse(SWAGGER)) == {
        ("SERVES_HTTP_ROUTE", "GET /users", 10, 11, "exact"),
        ("DECLARES_HTTP_DEPLOYMENT", "https://api.example.com/v1", None, None, "composed"),
        ("EXPOSES_HTTP_ENDPOINT", "GET https://api.example.com/v1/users", None, None, "composed"),
    }


def test_swagger_2_emits_one_deployment_per_scheme_and_prefers_none() -> None:
    parsed = _parse(SWAGGER.replace("  - https\n", "  - http\n  - https\n  - ws\n"))
    assert _targets(parsed, EdgeType.DECLARES_HTTP_DEPLOYMENT) == {
        "http://api.example.com/v1",
        "https://api.example.com/v1",
    }
    assert _targets(parsed, EdgeType.EXPOSES_HTTP_ENDPOINT) == {
        "GET http://api.example.com/v1/users",
        "GET https://api.example.com/v1/users",
    }


def test_swagger_2_default_base_path_is_the_root() -> None:
    parsed = _parse(SWAGGER.replace("basePath: /v1\n", ""))
    assert _targets(parsed, EdgeType.EXPOSES_HTTP_ENDPOINT) == {"GET https://api.example.com/users"}


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("host: api.example.com\n", ""),
        ("schemes:\n  - https\n", ""),
        ("host: api.example.com", "host: user:pw@api.example.com"),
        ("host: api.example.com", "host: api.example.com/v2"),
        ("basePath: /v1", "basePath: v1"),
    ],
)
def test_an_incomplete_swagger_base_yields_routes_only(old: str, new: str) -> None:
    parsed = _parse(SWAGGER.replace(old, new))
    assert _targets(parsed, EdgeType.SERVES_HTTP_ROUTE) == {"GET /users"}
    assert _targets(parsed, EdgeType.DECLARES_HTTP_DEPLOYMENT) == set()
    assert _targets(parsed, EdgeType.EXPOSES_HTTP_ENDPOINT) == set()


def test_a_swagger_operation_can_replace_the_schemes() -> None:
    text = SWAGGER.replace("    get:\n", "    get:\n      schemes: [http]\n")
    assert _targets(_parse(text), EdgeType.EXPOSES_HTTP_ENDPOINT) == {
        "GET http://api.example.com/v1/users"
    }


def test_a_json_specification_is_read_with_line_provenance() -> None:
    document = {
        "openapi": "3.0.1",
        "servers": [{"url": "https://api.example.com"}],
        "paths": {"/hello": {"get": {"responses": {}}}},
    }
    text = json.dumps(document, indent=2)
    facts = _facts(_parse(text, "openapi.json"))
    assert ("DECLARES_HTTP_DEPLOYMENT", "https://api.example.com", 5, 5, "exact") in facts
    assert ("SERVES_HTTP_ROUTE", "GET /hello", 9, 10, "exact") in facts


def test_a_generated_specification_is_bounded_and_says_where_it_stopped() -> None:
    paths = "".join(f"  /p{index}:\n    get: {{}}\n" for index in range(MAX_OPERATIONS + 5))
    parsed = _parse(f"openapi: 3.0.0\npaths:\n{paths}")
    assert len(_targets(parsed, EdgeType.SERVES_HTTP_ROUTE)) == MAX_OPERATIONS
    assert any("HTTP operations" in d.message for d in parsed.diagnostics)


def test_duplicate_declarations_in_one_document_are_one_fact() -> None:
    text = BASIC.replace(
        "  - url: https://api.example.com\n",
        "  - url: https://api.example.com\n  - url: https://api.example.com/\n",
    )
    endpoints = [
        r for r in _parse(text).references if r.edge_type is EdgeType.EXPOSES_HTTP_ENDPOINT
    ]
    assert len({r.target_key for r in endpoints}) == len(endpoints)


def test_a_lockfile_is_still_skipped() -> None:
    parsed = StructuredParser().parse(Path("package-lock.json"), '{"openapi": "3.0.0"}')
    assert parsed.references == ()
