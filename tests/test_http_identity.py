"""HTTP route, deployment and endpoint identity (HTTP linkage, semantic model).

Every rule below is a place where a convenient normalization would turn two
different operations into one node — and a node two repositories meet on is the
strongest thing this feature says. So the tests are mostly about what must *not*
be folded: method, scheme, port, trailing slash, parameters. The few things that
are folded (host case, query, fragment) are folded because URL semantics make
them the same request, and each is pinned too.
"""

from __future__ import annotations

import pytest
from repoatlas.graph.keys import global_key, is_positional
from repoatlas.models import http
from repoatlas.models.parsing import EdgeType, EntityType

# -- methods ---------------------------------------------------------------------


def test_the_method_set_is_exactly_the_openapi_operations() -> None:
    assert set(http.HTTP_METHODS) == {
        "GET",
        "PUT",
        "POST",
        "DELETE",
        "OPTIONS",
        "HEAD",
        "PATCH",
        "TRACE",
    }


@pytest.mark.parametrize("value", ["get", "Get", "CONNECT", "FETCH", "", " GET", "GET "])
def test_a_method_is_exact_and_uppercase_or_nothing(value: str) -> None:
    """Folding happens in each extractor, which knows its library's rules."""
    assert http.http_method(value) is None


def test_the_method_is_part_of_route_identity() -> None:
    keys = {http.route_key(http.route_body(method, "/users")) for method in http.HTTP_METHODS}
    assert len(keys) == len(http.HTTP_METHODS)


def test_the_method_is_part_of_endpoint_identity() -> None:
    url = "https://api.example.com/users"
    get = http.endpoint_key(http.endpoint_body("GET", url))
    post = http.endpoint_key(http.endpoint_body("POST", url))
    assert get != post


# -- route paths -------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/hello/me",
        "/Hello/Me",
        "/users/",
        "/users//x",
        "/users/{id}",
        "/users/:id",
        "/user/<int:id>",
        "/caf%C3%A9",
    ],
)
def test_a_route_path_is_kept_byte_exact(path: str) -> None:
    assert http.route_path(path) == path


@pytest.mark.parametrize(
    "path",
    ["", "hello", "users/me", "/a b", "/tab\tx", "/café", "/a\\b", "/a?b", "/a#b"],
)
def test_a_route_path_that_is_not_plain_is_refused(path: str) -> None:
    assert http.route_path(path) is None


def test_trailing_slash_and_case_are_distinct_routes() -> None:
    keys = {
        http.route_key(http.route_body("GET", path)) for path in ("/users", "/users/", "/Users")
    }
    assert len(keys) == 3


def test_parameter_templates_are_not_canonicalized_into_one_pattern() -> None:
    """``{id}``, ``:id``, ``<id>`` and ``42`` are four routes, and stay four.

    A client template is matched to routes by shape (``test_http_templates``); no
    route key is ever folded into a pattern.
    """
    keys = {
        http.route_key(http.route_body("GET", path))
        for path in ("/users/{id}", "/users/:id", "/users/<id>", "/users/42")
    }
    assert len(keys) == 4


def test_an_over_long_path_is_refused() -> None:
    assert http.route_path("/" + "a" * http.MAX_IDENTITY_LENGTH) is None


# -- client targets ----------------------------------------------------------------


def test_a_relative_target_names_a_route_and_no_origin() -> None:
    target = http.client_target("/hello/me")
    assert target == http.HttpTarget(path="/hello/me")
    assert target is not None and target.url is None


def test_an_absolute_target_keeps_scheme_host_port_and_path() -> None:
    target = http.client_target("https://api.example.com:8443/hello/me")
    assert target == http.HttpTarget(path="/hello/me", origin="https://api.example.com:8443")
    assert target is not None and target.url == "https://api.example.com:8443/hello/me"


@pytest.mark.parametrize(
    "value",
    [
        "https://api.example.com/hello/me?source=web",
        "https://api.example.com/hello/me#section",
        "https://api.example.com/hello/me?source=web#section",
    ],
)
def test_query_and_fragment_are_not_part_of_an_absolute_target(value: str) -> None:
    target = http.client_target(value)
    assert target is not None and target.url == "https://api.example.com/hello/me"


@pytest.mark.parametrize("value", ["/search?q=abc", "/search#top", "/search?q=1#x"])
def test_query_and_fragment_are_not_part_of_a_relative_target(value: str) -> None:
    assert http.client_target(value) == http.HttpTarget(path="/search")


def test_scheme_and_host_case_fold_under_url_semantics() -> None:
    target = http.client_target("HTTPS://API.Example.COM/Hello")
    assert target is not None
    assert target.url == "https://api.example.com/Hello"


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("https://api.example.com/users", "http://api.example.com/users"),
        ("https://api.example.com:8443/users", "https://api.example.com/users"),
        ("https://api.example.com:443/users", "https://api.example.com/users"),
        ("https://www.example.com/users", "https://example.com/users"),
        ("https://api.example.com/users", "https://api.example.com/users/"),
    ],
)
def test_nothing_else_is_assumed_equivalent(left: str, right: str) -> None:
    a, b = http.client_target(left), http.client_target(right)
    assert a is not None and b is not None
    assert a.url != b.url


def test_an_empty_absolute_path_is_the_root() -> None:
    target = http.client_target("https://api.example.com")
    assert target is not None and target.url == "https://api.example.com/"


def test_a_bracketed_ipv6_host_is_kept() -> None:
    target = http.client_target("http://[::1]:8000/api")
    assert target is not None and target.url == "http://[::1]:8000/api"


def test_localhost_is_an_ordinary_static_target() -> None:
    target = http.client_target("http://localhost:8000/api")
    assert target is not None and target.url == "http://localhost:8000/api"


@pytest.mark.parametrize(
    "value",
    [
        "https://user:secret@api.example.com/hello",
        "https://token@api.example.com/hello",
        "https://:pw@api.example.com/hello",
    ],
)
def test_a_credential_bearing_url_is_refused_entirely(value: str) -> None:
    """Refused, not stripped: nothing from a literal carrying a secret is promoted."""
    assert http.client_target(value) is None
    assert http.deployment_base(value) is None


def test_a_secret_in_the_query_never_reaches_an_identity() -> None:
    target = http.client_target("https://api.example.com/v1/items?api_key=sk-live-123&token=abc")
    assert target is not None and target.url is not None
    key = http.endpoint_key(http.endpoint_body("GET", target.url))
    assert "sk-live-123" not in key and "token" not in key and "?" not in key


@pytest.mark.parametrize(
    "value",
    [
        "",
        "hello/me",
        "./hello",
        "//api.example.com/hello",
        "ftp://api.example.com/x",
        "mailto:a@example.com",
        "javascript:alert(1)",
        "https:/api.example.com/x",
        "https://",
        "https://api.example.com:0/x",
        "https://api.example.com:65536/x",
        "https://api.example.com:08443/x",
        "https://api.example.com:port/x",
        "https://api.exa mple.com/x",
        "https://bücher.example/x",
        "https://api.example.com/café",
        "https://%61pi.example.com/x",
        "https://api.example.com\\x",
        "https://[::1/x",
    ],
)
def test_a_target_that_cannot_be_read_with_certainty_is_refused(value: str) -> None:
    assert http.client_target(value) is None


# -- deployments and composition ---------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://api.example.com", "https://api.example.com"),
        ("https://api.example.com/", "https://api.example.com"),
        ("https://api.example.com/v1", "https://api.example.com/v1"),
        ("https://api.example.com/v1/", "https://api.example.com/v1"),
        ("https://API.example.com:8443/v1", "https://api.example.com:8443/v1"),
    ],
)
def test_a_deployment_base_loses_one_trailing_slash_and_nothing_else(
    value: str, expected: str
) -> None:
    assert http.deployment_base(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "https://{environment}.example.com",
        "https://api.example.com/{version}",
        "/v1",
        "",
        "https://api.example.com/v1?x=1",
        "https://api.example.com/v1#frag",
        "https://u:p@api.example.com",
    ],
)
def test_a_base_that_is_not_static_and_absolute_is_refused(value: str) -> None:
    """Server variables fail closed; a relative server names no deployment."""
    assert http.deployment_base(value) is None


@pytest.mark.parametrize(
    ("base", "path", "expected"),
    [
        # base without a trailing slash
        ("https://api.example.com/v1", "/users", "https://api.example.com/v1/users"),
        # base with a trailing slash
        ("https://api.example.com/v1/", "/users", "https://api.example.com/v1/users"),
        # root route
        ("https://api.example.com", "/", "https://api.example.com/"),
        ("https://api.example.com/v1", "/", "https://api.example.com/v1/"),
        # nested base path
        ("https://api.example.com/api/v2", "/users/me", "https://api.example.com/api/v2/users/me"),
        # origin only
        ("https://api.example.com", "/hello/me", "https://api.example.com/hello/me"),
    ],
)
def test_composition_appends_and_never_resolves(base: str, path: str, expected: str) -> None:
    """``urljoin`` would drop ``/v1``; OpenAPI appends the path to the server URL."""
    deployment = http.deployment_base(base)
    assert deployment is not None
    assert http.compose(deployment, path) == expected


def test_a_composed_endpoint_equals_the_same_absolute_call() -> None:
    """The meeting point of scenario B, stated as identity rather than as a graph."""
    deployment = http.deployment_base("https://api.example.com")
    assert deployment is not None
    declared = http.endpoint_key(http.endpoint_body("GET", http.compose(deployment, "/hello")))
    target = http.client_target("https://api.example.com/hello")
    assert target is not None and target.url is not None
    called = http.endpoint_key(http.endpoint_body("GET", target.url))
    assert declared == called


# -- keys --------------------------------------------------------------------------


def test_the_natural_key_shapes() -> None:
    assert http.route_key("GET /hello/me") == "http-route:GET:/hello/me"
    assert (
        http.deployment_key("https://api.example.com/v1")
        == "http-deployment:https://api.example.com/v1"
    )
    assert (
        http.endpoint_key("GET https://api.example.com/hello/me")
        == "http-endpoint:GET:https://api.example.com/hello/me"
    )


def test_the_graph_builds_the_same_keys_for_interned_http_nodes() -> None:
    assert global_key(EntityType.HTTP_ROUTE, "GET /x") == "http-route:GET:/x"
    assert global_key(EntityType.HTTP_DEPLOYMENT, "https://a.example") == (
        "http-deployment:https://a.example"
    )
    assert global_key(EntityType.HTTP_ENDPOINT, "POST https://a.example/x") == (
        "http-endpoint:POST:https://a.example/x"
    )


def test_http_keys_are_not_positional() -> None:
    """They name an identity, not a source position, so they survive a sync."""
    assert not is_positional("http-route:GET:/x")
    assert not is_positional("http-endpoint:GET:https://a.example/x")


def test_a_key_carries_no_repository() -> None:
    """Otherwise two repositories could never meet on one node."""
    key = http.route_key(http.route_body("GET", "/hello"))
    assert key == "http-route:GET:/hello"


@pytest.mark.parametrize(
    ("validator", "body", "expected"),
    [
        (http.canonical_route, "GET /hello", "GET /hello"),
        (http.canonical_route, "get /hello", None),
        (http.canonical_route, "GET hello", None),
        (http.canonical_route, "GET /a?b", None),
        (http.canonical_route, "GET", None),
        (http.canonical_deployment, "https://api.example.com/v1", "https://api.example.com/v1"),
        (http.canonical_deployment, "https://api.example.com/v1/", None),
        (http.canonical_deployment, "https://API.example.com", None),
        (http.canonical_deployment, "https://u:p@api.example.com", None),
        (http.canonical_endpoint, "GET https://a.example/x", "GET https://a.example/x"),
        (http.canonical_endpoint, "GET https://a.example/x?token=1", None),
        (http.canonical_endpoint, "GET https://a.example", None),
        (http.canonical_endpoint, "GET /x", None),
        (http.canonical_endpoint, "FETCH https://a.example/x", None),
    ],
)
def test_only_a_canonical_body_survives_revalidation(
    validator: object, body: str, expected: str | None
) -> None:
    assert callable(validator)
    assert validator(body) == expected


# -- query analysis ------------------------------------------------------------------


def test_a_method_and_path_name_one_route() -> None:
    assert http.query_keys("Who calls GET /hello/me?") == ("http-route:GET:/hello/me",)


def test_a_method_and_url_name_one_endpoint() -> None:
    assert http.query_keys("GET https://api.example.com/hello/me") == (
        "http-endpoint:GET:https://api.example.com/hello/me",
    )


def test_a_lowercase_method_in_prose_is_still_read() -> None:
    assert http.query_keys("where do we post /users") == ("http-route:POST:/users",)


def test_a_bare_path_is_looked_up_under_every_method() -> None:
    keys = http.query_keys("/hello/me")
    assert keys == tuple(f"http-route:{method}:/hello/me" for method in http.HTTP_METHODS)


def test_a_bare_url_is_looked_up_as_every_endpoint_and_as_a_deployment() -> None:
    keys = http.query_keys("who calls https://api.example.com/hello/me")
    assert set(keys) == {
        *(
            f"http-endpoint:{method}:https://api.example.com/hello/me"
            for method in http.HTTP_METHODS
        ),
        "http-deployment:https://api.example.com/hello/me",
    }


def test_query_keys_drop_the_query_string() -> None:
    assert http.query_keys("GET https://api.example.com/search?q=abc") == (
        "http-endpoint:GET:https://api.example.com/search",
    )


@pytest.mark.parametrize(
    "question",
    [
        "where is SERVICE_HOST configured?",
        "how does deploy.sh work",
        "src/app.py",
        "GET",
        "https://user:pw@api.example.com/x",
        "fetch the config",
    ],
)
def test_a_question_that_names_no_http_target_generates_no_key(question: str) -> None:
    assert http.query_keys(question) == ()


def test_query_keys_are_bounded() -> None:
    question = " ".join(f"/p{index}" for index in range(100))
    assert len(http.query_keys(question)) == http.MAX_QUERY_KEYS


def test_query_keys_are_deterministic() -> None:
    question = "GET /a and https://x.example/b and /c"
    assert http.query_keys(question) == http.query_keys(question)


# -- the public vocabulary -----------------------------------------------------------

#: Every value published before this feature, in its published order. Appending is
#: additive for a consumer; inserting or renaming is not.
_ENTITY_VALUES_BEFORE = (
    "repository",
    "file",
    "module",
    "function",
    "class",
    "method",
    "symbol",
    "script",
    "command",
    "package",
    "salt_state",
    "template",
    "config",
    "env_var",
    "service",
    "test",
)
_EDGE_VALUES_BEFORE = (
    "CONTAINS",
    "DEFINES",
    "IMPORTS",
    "EXPORTS",
    "REFERENCES",
    "CALLS",
    "DEPENDS_ON",
    "EXECUTES",
    "SOURCES",
    "INCLUDES",
    "REQUIRES",
    "WATCHES",
    "READS",
    "WRITES",
    "CONFIGURES",
    "MANAGES",
    "GENERATES",
)


def test_entity_types_are_appended_and_existing_values_are_unchanged() -> None:
    values = tuple(member.value for member in EntityType)
    assert values[: len(_ENTITY_VALUES_BEFORE)] == _ENTITY_VALUES_BEFORE
    assert values[len(_ENTITY_VALUES_BEFORE) :] == (
        "http_route",
        "http_deployment",
        "http_endpoint",
    )


def test_edge_types_are_appended_and_existing_values_are_unchanged() -> None:
    values = tuple(member.value for member in EdgeType)
    assert values[: len(_EDGE_VALUES_BEFORE)] == _EDGE_VALUES_BEFORE
    assert values[len(_EDGE_VALUES_BEFORE) :] == (
        "SERVES_HTTP_ROUTE",
        "CALLS_HTTP_ROUTE",
        "DECLARES_HTTP_DEPLOYMENT",
        "EXPOSES_HTTP_ENDPOINT",
        "CALLS_HTTP_ENDPOINT",
        # A declaration's use of a module-scope name (change impact).
        "USES",
        # A request reaching the handler another repository serves it with.
        "CALLS_HTTP_HANDLER",
        # One enum member mapped to another by an explicit typed mapping.
        "MAPS_TO",
    )
