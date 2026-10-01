"""OpenAPI 3 and Swagger 2 documents as static HTTP facts (HTTP linkage).

A specification in a repository proves one thing: **this repository statically
declares these operations at these base locations**. It does not prove that the
deployment is live, that this repository's code implements the operations, or
that nothing else sits behind the same URL. The facts below say "declares" and
nothing stronger, and :mod:`repoatlas.models.http` explains why the identities
they point at are shared nodes rather than repository-to-repository edges.

**Recognition is by signature, never by shape.** A document is read as a
specification only when its root says ``openapi: 3.x`` or ``swagger: "2.0"``.
``paths:`` on its own is ordinary configuration, and treating every such file as
an API would invent routes out of a CI config.

The three facts a specification yields, and how much of each one source span can
prove:

* **route** — a ``paths`` key and one of its operation keys. One contiguous span,
  from the path key to the operation key, contains every literal that decides
  it, so it is citable;
* **deployment** — an OpenAPI 3 ``servers[].url`` is one literal on one line and
  is citable. A Swagger 2 base is composed from ``schemes``, ``host`` and
  ``basePath``, which live on different lines, so it is recorded as *composed*;
* **endpoint** — always a composition of a deployment and a route, so always
  composed.

A composed fact still becomes a graph edge. What it does not get is a span: the
linker stores it without one, which leaves it uncitable while its constituents
stay citable on their own lines. Stitching two distant lines into one "span"
would cite text that was never written together.

An OpenAPI 3 document also records, as one claim, each operation's ``operationId``
(and ``x-swagger-router-controller``) and the base paths its root ``servers`` give
it: an ``oas3-tools`` application serves the document through handlers named by them
(:mod:`repoatlas.graph.ts_oas3`). The claim is a table, not a fact.

**Fail closed.** Nothing is fetched and nothing is guessed: a ``$ref`` path item
is left unresolved, a server URL with a variable (``https://{env}.example.com``)
names no deployment, a relative server names none either, and a level whose
``servers`` list is declared but unusable yields no endpoints rather than falling
back to a base the document overrode.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass

import yaml

from repoatlas.models import http
from repoatlas.models.parsing import EdgeType, EntityType, ParsedReference
from repoatlas.parsers.http_facts import OPENAPI_OPERATIONS, pending_reference
from repoatlas.parsers.marks import node_span

#: The operation fields of an OpenAPI path item, and nothing else: ``parameters``,
#: ``summary`` and ``servers`` are path-item fields too, and none is an operation.
_OPERATIONS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

_OPENAPI_3 = re.compile(r"3\.\d+(?:\.\d+)?")
_SWAGGER_2 = "2.0"
_SWAGGER_SCHEMES = frozenset({"http", "https"})

#: Bound on what one document may contribute (V1_SPEC §20). A generated
#: specification with thousands of operations is still indexed up to here, and
#: the parser says where it stopped.
MAX_OPERATIONS = 1000

_NULL_TAG = "tag:yaml.org,2002:null"

OPENAPI_3 = "openapi3"
SWAGGER_2 = "swagger2"


@dataclass(frozen=True, slots=True)
class _Field:
    key: yaml.Node
    value: yaml.Node


@dataclass(frozen=True, slots=True)
class _Base:
    """A deployment one level of the document declares."""

    deployment: str
    line_start: int
    line_end: int
    #: Whether a single span states the whole base (OpenAPI 3 ``url``) or it was
    #: assembled from several fields (Swagger 2 ``schemes`` + ``host`` + ``basePath``).
    composed: bool


def _fields(node: yaml.Node | None) -> dict[str, _Field] | None:
    """A mapping's scalar-keyed fields, or ``None`` when it is not a clean mapping.

    A key written twice is ambiguous — YAML loaders disagree on which one wins —
    so a mapping with a duplicate is not read at all.
    """
    if not isinstance(node, yaml.MappingNode):
        return None
    fields: dict[str, _Field] = {}
    for key, value in node.value:
        if not isinstance(key, yaml.ScalarNode):
            continue
        if key.value in fields:
            return None
        fields[key.value] = _Field(key=key, value=value)
    return fields


def _scalar(node: yaml.Node | None) -> str | None:
    if not isinstance(node, yaml.ScalarNode) or node.tag == _NULL_TAG:
        return None
    return str(node.value)


class _Collector:
    def __init__(self, lines: Sequence[str], source: str) -> None:
        self._lines = lines
        self._source = source
        self._seen: dict[tuple[str, str, str, int, int], ParsedReference] = {}
        #: Claims for the global pass, recorded as they are.
        self.claims: list[ParsedReference] = []
        self.operations = 0
        self.truncated = False

    @property
    def references(self) -> list[ParsedReference]:
        return [*self._seen.values(), *self.claims]

    def span(self, node: yaml.Node) -> tuple[int, int]:
        return node_span(node, self._lines)

    def add(
        self,
        edge: EdgeType,
        kind: EntityType,
        body: str,
        line_start: int,
        line_end: int,
        *,
        composed: bool,
    ) -> None:
        identity = (str(edge), str(kind), body, line_start, line_end)
        if identity in self._seen:
            return
        self._seen[identity] = ParsedReference(
            edge_type=edge,
            target_kind=kind,
            target_key=body,
            line_start=line_start,
            line_end=max(line_start, line_end),
            meta={"source": self._source, "provenance": "composed" if composed else "exact"},
        )

    def deployments(self, bases: Sequence[_Base]) -> None:
        for base in bases:
            self.add(
                EdgeType.DECLARES_HTTP_DEPLOYMENT,
                EntityType.HTTP_DEPLOYMENT,
                base.deployment,
                base.line_start,
                base.line_end,
                composed=base.composed,
            )


def specification_references(
    document: yaml.Node, lines: Sequence[str]
) -> tuple[list[ParsedReference], bool]:
    """The HTTP facts one composed document declares, and whether it was cut.

    Empty for anything that is not an OpenAPI 3 or Swagger 2 document.
    """
    root = _fields(document)
    if root is None:
        return [], False
    openapi = _scalar(root["openapi"].value) if "openapi" in root else None
    swagger = _scalar(root["swagger"].value) if "swagger" in root else None
    if openapi is not None and swagger is not None:
        # A document claiming both is not reliably either.
        return [], False
    if openapi is not None and _OPENAPI_3.fullmatch(openapi):
        collector = _Collector(lines, OPENAPI_3)
        _openapi3(root, collector)
    elif swagger == _SWAGGER_2:
        collector = _Collector(lines, SWAGGER_2)
        _swagger2(root, collector)
    else:
        return [], False
    return collector.references, collector.truncated


def _operations(
    root: dict[str, _Field], collector: _Collector
) -> list[tuple[str, str, int, int, dict[str, _Field], dict[str, _Field]]]:
    """Every readable ``(method, path, span, path item, operation)`` in order."""
    paths = _fields(root["paths"].value) if "paths" in root else None
    if paths is None:
        return []
    found: list[tuple[str, str, int, int, dict[str, _Field], dict[str, _Field]]] = []
    for name, field in paths.items():
        path = http.route_path(name)
        item = _fields(field.value)
        if path is None or item is None:
            continue
        if "$ref" in item:
            # Its operations live somewhere this parser will not follow. Local or
            # remote, a reference is left unresolved rather than half-read.
            continue
        path_line = field.key.start_mark.line + 1
        for method_name in item:
            if method_name not in _OPERATIONS:
                continue
            operation = _fields(item[method_name].value)
            if operation is None:
                continue
            if collector.operations >= MAX_OPERATIONS:
                collector.truncated = True
                return found
            collector.operations += 1
            operation_line = item[method_name].key.start_mark.line + 1
            found.append((method_name.upper(), path, path_line, operation_line, item, operation))
    return found


def _openapi3(root: dict[str, _Field], collector: _Collector) -> None:
    root_bases = _servers(root.get("servers"), collector)
    collector.deployments(root_bases or [])

    named: list[list[object]] = []
    for method, path, path_line, operation_line, item, operation in _operations(root, collector):
        named_by = operation.get("operationId")
        operation_id = _scalar(named_by.value) if named_by is not None else None
        if named_by is not None and operation_id:
            controller = operation.get("x-swagger-router-controller")
            named.append(
                [
                    method,
                    path,
                    operation_id,
                    _scalar(controller.value) if controller is not None else None,
                    named_by.key.start_mark.line + 1,
                ]
            )
        collector.add(
            EdgeType.SERVES_HTTP_ROUTE,
            EntityType.HTTP_ROUTE,
            http.route_body(method, path),
            path_line,
            operation_line,
            composed=False,
        )
        path_bases = _servers(item.get("servers"), collector)
        operation_bases = _servers(operation.get("servers"), collector)
        collector.deployments(path_bases or [])
        collector.deployments(operation_bases or [])
        # The most specific level that declares servers wins, and a level that
        # declares them is never silently ignored in favour of an outer one.
        if operation_bases is not None:
            bases = operation_bases
        elif path_bases is not None:
            bases = path_bases
        else:
            bases = root_bases or []
        _endpoints(collector, method, path, bases, operation_line)
    if named:
        lines = [int(str(operation[4])) for operation in named]
        collector.claims.append(
            pending_reference(
                EdgeType.REFERENCES,
                EntityType.CONFIG,
                OPENAPI_OPERATIONS,
                min(lines),
                max(lines),
                role=OPENAPI_OPERATIONS,
                operations=json.dumps(named),
                bases=json.dumps(_validator_bases(root.get("servers"))),
            )
        )


def _validator_bases(field: _Field | None) -> list[str] | None:
    """The base paths ``express-openapi-validator`` serves the paths under, or ``None``.

    Its own reading (``BasePath.findUrlPath``): each root server URL's path -- what
    follows its first ``/`` after any ``://``, else ``/`` -- with one trailing slash
    dropped; no servers is one empty base. A server with ``variables``, or a path with
    a placeholder or a query, is expanded or matched in ways not read here: ``None``.
    """
    if field is None:
        return [""]
    node = field.value
    if not isinstance(node, yaml.SequenceNode):
        return None
    bases: list[str] = []
    for item in node.value:
        server = _fields(item)
        url = _scalar(server["url"].value) if server is not None and "url" in server else None
        if server is None or url is None or "variables" in server:
            return None
        start = url.find("://")
        rest = url[start + 3 :] if start != -1 else url
        slash = rest.find("/")
        path = rest[slash:] if slash != -1 else "/"
        if any(mark in path for mark in "{}?#"):
            return None
        base = path.removesuffix("/")
        if base not in bases:
            bases.append(base)
    return bases or [""]


def _servers(field: _Field | None, collector: _Collector) -> list[_Base] | None:
    """The usable bases one level declares.

    ``None`` when the level declares no ``servers`` at all, so an outer level
    applies. An empty list when it declares them and none is usable — relative,
    templated, credential-bearing — which yields no endpoint rather than a
    fallback the document itself overrode. OpenAPI treats an absent or empty
    root ``servers`` as ``/``, which is relative and names no deployment, so both
    read as nothing to compose with.
    """
    if field is None:
        return None
    node = field.value
    if not isinstance(node, yaml.SequenceNode):
        return []
    bases: list[_Base] = []
    for item in node.value:
        server = _fields(item)
        if server is None or "url" not in server:
            continue
        url = _scalar(server["url"].value)
        deployment = http.deployment_base(url) if url is not None else None
        if deployment is None:
            continue
        start = server["url"].key.start_mark.line + 1
        _, end = collector.span(server["url"].value)
        bases.append(_Base(deployment, start, end, composed=False))
    return bases


def _swagger2(root: dict[str, _Field], collector: _Collector) -> None:
    root_schemes = _schemes(root.get("schemes"))
    anchor = root["host"].key.start_mark.line + 1 if "host" in root else 1

    def bases_for(schemes: Sequence[str]) -> list[_Base]:
        bases: list[_Base] = []
        for scheme in schemes:
            deployment = _swagger_base(root, scheme)
            if deployment is not None:
                bases.append(_Base(deployment, anchor, anchor, composed=True))
        return bases

    collector.deployments(bases_for(root_schemes))
    for method, path, path_line, operation_line, _item, operation in _operations(root, collector):
        collector.add(
            EdgeType.SERVES_HTTP_ROUTE,
            EntityType.HTTP_ROUTE,
            http.route_body(method, path),
            path_line,
            operation_line,
            composed=False,
        )
        # An operation's own `schemes` replaces the document's (Swagger 2.0).
        own = operation.get("schemes")
        bases = bases_for(_schemes(own) if own is not None else root_schemes)
        collector.deployments(bases)
        _endpoints(collector, method, path, bases, operation_line)


def _schemes(field: _Field | yaml.Node | None) -> list[str]:
    node = field.value if isinstance(field, _Field) else field
    if not isinstance(node, yaml.SequenceNode):
        return []
    found: list[str] = []
    for item in node.value:
        scheme = _scalar(item)
        if scheme in _SWAGGER_SCHEMES and scheme not in found:
            found.append(scheme)
    return found


def _swagger_base(root: dict[str, _Field], scheme: str) -> str | None:
    """``scheme://host`` + ``basePath``, or ``None`` when any part is unusable.

    No ``host`` means the API is served from wherever the specification itself
    was fetched from, which is not knowable from the repository.
    """
    host = _scalar(root["host"].value) if "host" in root else None
    base_path = _scalar(root["basePath"].value) if "basePath" in root else "/"
    if host is None or base_path is None:
        return None
    return http.swagger2_base(scheme, host, base_path)


def _endpoints(
    collector: _Collector, method: str, path: str, bases: Sequence[_Base], anchor: int
) -> None:
    for base in bases:
        collector.add(
            EdgeType.EXPOSES_HTTP_ENDPOINT,
            EntityType.HTTP_ENDPOINT,
            http.endpoint_body(method, http.compose(base.deployment, path)),
            anchor,
            anchor,
            composed=True,
        )
