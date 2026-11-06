"""The one shape every HTTP fact takes on its way out of a parser (HTTP linkage).

Shared by the specification, Python and TypeScript extractors so that "which
library said this" and "can one span prove it" are recorded identically
everywhere. The linker reads exactly two things from ``meta``:

* ``provenance`` — ``exact`` when the reference's span contains every literal
  that determines its target, ``composed`` when the target was assembled from
  literals in different places (a server URL and a path, a router prefix and a
  decorator). A composed fact is stored with no span at all, which leaves it a
  graph fact that nothing can cite, rather than a citation of text that does not
  say what it is quoted for;
* ``source`` — the specification format, framework or client library, kept for
  inspection and never used to decide identity.

**Pending facts.** Some HTTP facts cannot be proven from one file: a NestJS
controller serves nothing until an application registers it, a request made
through an imported helper is only a request once the helper is read, and a
request whose path is a template names a route only once it is matched against
the routes its repository serves. A parser
records such a fact with :func:`pending_reference`, which tags it with the role it
plays under ``pending_fact``. The linker keeps every such reference as a claim —
never interning it and never resolving it as a symbol — and the global pass
(:mod:`repoatlas.graph.http_claims`) decides, from everything indexed, whether it
is proven. A claim that is never proven stays exactly that: a recorded
declaration, not a fact on a shared node.
"""

from __future__ import annotations

import json

from repoatlas.models.parsing import EdgeType, EntityType, ParsedReference

EXACT = "exact"
COMPOSED = "composed"

#: The ``meta`` key that marks a reference as a pending claim for the global pass.
PENDING_FACT = "pending_fact"

#: A route a NestJS controller declares, relative to wherever it is mounted.
NESTJS_ROUTE = "nestjs-route"
#: A class this file declares to be a NestJS module (a declaration, not a claim).
NESTJS_MODULE = "nestjs-module"
#: One entry of a module's ``controllers`` list.
NESTJS_CONTROLLER = "nestjs-controller"
#: One entry of a module's ``imports`` list.
NESTJS_IMPORT = "nestjs-import"
#: The root module of an application ``NestFactory.create`` builds.
NESTJS_ROOT = "nestjs-root"
#: A construct that can change routes from outside the registration it touches.
NESTJS_MARKER = "nestjs-marker"

#: An exported requester factory: which members forward a path to a fetch-like call.
HTTP_REQUESTER = "http-requester"
#: An exported request wrapper, and what it does with its arguments.
HTTP_WRAPPER = "http-wrapper"
#: An exported init helper, and the method it sets.
HTTP_INIT_HELPER = "http-init-helper"
#: A call through an imported helper, with its arguments as far as the file reads them.
HTTP_REQUESTER_CALL = "http-requester-call"
#: A request this file makes whose route one file cannot decide. ``transport`` says
#: how: ``wrapper`` for a call through a proven same-file wrapper whose path is a
#: template, a conditional or a named value, or whose method cannot be read — the
#: global pass matches it against the routes its own repository serves; ``fetch``
#: for a direct ``fetch`` (or alias) with a template URL, which is recorded so the
#: request stays visible and is never matched, because transport code forwards
#: whole paths through ``fetch``.
HTTP_LOCAL_CALL = "http-local-call"
#: A handler served at an absolute endpoint: a route of an application whose published
#: base the source states. One side of the cross-repository join
#: (:mod:`repoatlas.graph.http_crossings`), whatever stated the base: ``identity_source``
#: names it, and nothing downstream reads anything more specific.
HTTP_EXPOSED_ENDPOINT = "http-exposed-endpoint"
#: An identity a deployment resolves -- a name and a port -- that reaches no code: derived by
#: the global pass (:mod:`repoatlas.graph.deploy_identity`), never stored. A request to it is
#: refused as ``http-deployment-source-unresolved``, and a link it could rival is refused.
HTTP_UNSOURCED_IDENTITY = "http-unsourced-identity"
#: The hosts an Ingress template would give under a deployment no local source establishes:
#: literal text and the chart's name, with a hole for each value or item it substitutes.
#: Derived by the global pass, never stored, and never linked: a request it fits is
#: refused as ``http-deployment-binding-unresolved``, and a link it could rival is refused.
HTTP_UNRESOLVED_PATTERN = "http-unresolved-pattern"
#: On an :data:`HTTP_UNRESOLVED_PATTERN` whose chart is tied to its repository's code: the
#: routes that code serves, ``[[method, path, handler id], ...]``, which an exposure of a bound
#: release would carry. Derived by the global pass for the join (DF3c), never stored.
SERVED_ROUTES = "served"
#: A request whose origin and path shape the source states: a literal URL, a module
#: constant, or an f-string on a literal or constant origin. The other side of the join.
#: A request whose path is read but whose host is not carries ``base`` =
#: :data:`BASE_UNKNOWN` and no ``origin``: counted, listed, never joined (D2).
HTTP_CLIENT_CALL = "http-client-call"
#: The ``base`` of an :data:`HTTP_CLIENT_CALL` whose host the source does not state.
BASE_UNKNOWN = "unknown"

#: A Python application, router or blueprint a module constructs: what it is, its own
#: prefix, and where the same module mounts it. Routes and mounts elsewhere name it.
PYTHON_RECEIVER = "python-receiver"
#: A route on a Python router or blueprint, or one whose path names an imported constant:
#: served only where the global pass proves a mount, and the constant's value.
PYTHON_ROUTE = "python-route"
#: A module-level class that subclasses FastAPI's or Flask's application, under rules that leave
#: nothing in it able to change what it registers or serves: its name, kind and depth. Another
#: module that builds an application from the class is served only if its defining module
#: states this claim.
PYTHON_CLASS = "python-class"
#: A route on an application a module builds from a class (or a subclass of a class) it imports
#: from another module. The target key is the receiver's local name; the global pass serves it
#: only if the class is an application subclass its defining module offers
#: (:data:`PYTHON_CLASS`). Never a fact about a route until then.
PYTHON_CLASS_ROUTE = "python-class-route"
#: A router or blueprint mounted on an application or router -- imported, or mounted from
#: inside an application factory -- with the prefix it is mounted at.
PYTHON_MOUNT = "python-mount"
#: A module-level function of a Python module that registers a blueprint or router on its
#: application parameter, or hands that parameter to another such function (R2c-1): which
#: parameter, the registrations (mounts, as :data:`PYTHON_MOUNT` states them, with the
#: application left to the call) and the nested calls. Said only of a function with
#: something to record.
HELPER_ROLE = "python-helper"
#: A call of such a function with an application the module or one of its factories proves
#: (R2c-1): the function, the application and where the call stands. The global pass expands
#: it into mounts of that application.
HELPER_CALL_ROLE = "python-helper-call"
#: A module's string constants that could be part of a path or a URL, for the routes and
#: requests in other modules that import them.
PYTHON_CONSTANTS = "python-constants"

#: A module that runs as a script (``if __name__ == "__main__":``): its directory is
#: first on the import path of the program it starts.
PYTHON_SCRIPT = "python-script"

#: A Fastify application, or a plugin function a module declares (the server is its first
#: parameter): its identity, and the names it is exported under.
FASTIFY_SCOPE = "fastify-scope"
#: A route a Fastify plugin declares on its server, or one whose path names an imported
#: constant: served only where the global pass proves a chain of registrations.
FASTIFY_ROUTE = "fastify-route"
#: ``server.register(plugin, { prefix })``: the plugin, where it comes from, and its prefix.
FASTIFY_REGISTER = "fastify-register"
#: ``const x = F()`` where ``F`` may be a function that returns a Fastify application, or an
#: application built at the top of an entry function: the receiver, the factory it names,
#: and whether the parser proved it or why it refused (M8).
FASTIFY_INSTANCE = "fastify-instance"
#: A call that hands a receiver to a function of the same file or of a relative module (M8).
FASTIFY_HELPER_CALL = "fastify-helper-call"
#: A function a helper call reaches, and the receiver its body reads (M8).
FASTIFY_TRACED_SCOPE = "fastify-traced-scope"
#: A route declared on a receiver a helper call handed over, served only where the global
#: pass proves the chain back to an instance (M8).
FASTIFY_TRACED_ROUTE = "fastify-traced-route"
#: A TypeScript module's exported string constants that could be part of a path or a URL,
#: for the routes and requests in other modules that import them.
TS_CONSTANTS = "ts-constants"
#: A call handing a function an options object whose leaves the source proves --
#: superagent itself, a stated string, a key of the caller's own destructured options --
#: so the global pass can follow a transport and a base into the function.
TS_INJECTION = "ts-injection"
#: ``fetcher.<verb>(`${base}/...`)`` on a function's destructured options: a request whose
#: transport (and base) only the callers that supply them decide.
TS_INJECTED_REQUEST = "ts-injected-request"
#: A module-level function that owns injection claims, and the names it is exported under.
TS_FUNCTION = "ts-function"
#: An OpenAPI 3 document's operations by ``operationId``, and the base paths its root
#: ``servers`` give them: what an application that routes by operationId serves.
OPENAPI_OPERATIONS = "openapi-operations"
#: A module-level ``const`` object literal whose every member is a function: its keys,
#: and the names it is exported under. A controllers object, if anything uses it so.
TS_HANDLER_OBJECT = "ts-handler-object"
#: ``expressAppConfig(spec, { routing: { controllers } })`` from ``oas3-tools``: the
#: specification it reads, relative to its file, and the objects its controllers are.
OAS3_APP = "oas3-app"
#: A call of a function that builds an oas3-tools application from the specification it is
#: handed: the one a file declares itself (always), or one imported by name from a relative
#: module (only when an argument reads as an OpenAPI document). The target key is the callee as
#: written; the arguments are read relative to the caller's file.
OAS3_FACTORY_CALL = "oas3-factory-call"
#: A TypeScript module's exported module-level ``const`` paths that the module alone proves
#: to be an OpenAPI document (``.yaml``, ``.yml`` or ``.json``), for the applications in other
#: modules that import them.
TS_PATH_CONSTANTS = "ts-path-constants"
#: A docker-compose service: the names and container ports other services reach it by,
#: the build that makes its image, and any command or entrypoint it overrides.
COMPOSE_SERVICE = "compose-service"
#: A Dockerfile's final stage: its working directory, what it copies from the build
#: context and where, and the command and entrypoint the image runs.
DOCKERFILE_COMMAND = "dockerfile-command"
#: A Helm chart's ``Chart.yaml``: its name, for the chart directory.
HELM_CHART = "helm-chart"
#: A Helm chart's ``values.yaml``: its literal scalar leaves.
HELM_VALUES = "helm-values"
#: A chart template's ``kind: Service``: its name and ports as written.
HELM_SERVICE = "helm-service"
#: A chart's ``<x>.name`` or ``<x>.fullname`` helper, defined exactly as ``helm create`` does.
HELM_HELPER = "helm-helper"
#: A release of a chart that a deployment file binds by a literal name (a Tiltfile ``helm()``).
HELM_RELEASE = "helm-release"
#: A chart template's ``kind: Ingress``: its wrapping ``if`` and ``range``, its hosts as
#: literal text and substitutions, its scheme and its backend, or why it is not read.
HELM_INGRESS = "helm-ingress"
#: A ``values*.y(a)ml`` file other than a chart's own ``values.yaml``: its literal leaves,
#: credentials withheld -- an overlay a release may name with ``-f``.
HELM_VALUES_FILE = "helm-values-file"
#: A chart template's Deployment, StatefulSet or DaemonSet: its containers' images, ports,
#: commands and environment as written, its labels and selector, or why it is not read.
K8S_WORKLOAD = "k8s-workload"
#: A chart template's ConfigMap: its name and data keys (never their values).
K8S_CONFIGMAP = "k8s-configmap"
#: An Ingress document of any YAML file, read leniently for where it is reached: hosts and
#: TLS hosts as parts (text, chart name, values path, release, or an opaque hole), paths,
#: backends, scheme, guards and holes (repoatlas.parsers.deploy_targets). Never read by the
#: HTTP join, which keeps :data:`HELM_INGRESS`'s strict subset.
DEPLOYMENT_TARGET_INGRESS = "deployment-target-ingress"
#: A chart's ``values.yaml``: the literal ALB ``listen-ports`` annotation of each
#: ``annotations`` mapping in it, by the mapping's dotted path.
DEPLOYMENT_TARGET_VALUES = "deployment-target-values"

#: ``transport`` values of :data:`HTTP_LOCAL_CALL`.
TRANSPORT_WRAPPER = "wrapper"
TRANSPORT_FETCH = "fetch"


def pending_reference(
    edge_type: EdgeType,
    target_kind: EntityType,
    target_key: str,
    line_start: int,
    line_end: int,
    *,
    role: str,
    source_symbol: str | None = None,
    **meta: str,
) -> ParsedReference:
    """A fact only the global pass can prove, kept as a claim until it does."""
    return ParsedReference(
        edge_type=edge_type,
        target_kind=target_kind,
        target_key=target_key,
        line_start=line_start,
        line_end=max(line_start, line_end),
        source_symbol=source_symbol,
        meta={PENDING_FACT: role, **meta},
    )


def local_call_reference(
    callee: str,
    line_start: int,
    line_end: int,
    *,
    transport: str,
    path: dict[str, object],
    method: str | None,
    in_call: bool,
    base: str | None,
    source: str,
    source_symbol: str | None,
) -> ParsedReference:
    """An :data:`HTTP_LOCAL_CALL` claim: a request whose route one file cannot decide.

    ``path`` is the parser-neutral path spec — ``{"string"}``, ``{"parts"}``,
    ``{"alternatives"}`` or ``{"named"}``. The target key is the callee as written,
    as for a call through an imported helper: it names no route, because none is
    known yet.
    """
    meta = {"transport": transport, "path": json.dumps(path, sort_keys=True), "source": source}
    if method is not None:
        meta["method"] = method
        meta["in_call"] = "true" if in_call else "false"
    if base is not None:
        meta["base"] = base
    return pending_reference(
        EdgeType.CALLS_HTTP_ROUTE,
        EntityType.HTTP_ROUTE,
        callee,
        line_start,
        line_end,
        role=HTTP_LOCAL_CALL,
        source_symbol=source_symbol,
        **meta,
    )


def http_reference(
    edge: EdgeType,
    kind: EntityType,
    body: str,
    line_start: int,
    line_end: int,
    *,
    source: str,
    composed: bool,
    source_symbol: str | None = None,
) -> ParsedReference:
    return ParsedReference(
        edge_type=edge,
        target_kind=kind,
        target_key=body,
        line_start=line_start,
        line_end=max(line_start, line_end),
        source_symbol=source_symbol,
        meta={"source": source, "provenance": COMPOSED if composed else EXACT},
    )
