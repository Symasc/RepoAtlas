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
#: A request whose origin and path shape the source states: a literal URL, a module
#: constant, or an f-string on a literal or constant origin. The other side of the join.
HTTP_CLIENT_CALL = "http-client-call"

#: A Python application, router or blueprint a module constructs: what it is, its own
#: prefix, and where the same module mounts it. Routes and mounts elsewhere name it.
PYTHON_RECEIVER = "python-receiver"
#: A route on a Python router or blueprint, or one whose path names an imported constant:
#: served only where the global pass proves a mount, and the constant's value.
PYTHON_ROUTE = "python-route"
#: A router or blueprint mounted on an application or router -- imported, or mounted from
#: inside an application factory -- with the prefix it is mounted at.
PYTHON_MOUNT = "python-mount"
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
