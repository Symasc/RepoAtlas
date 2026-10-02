"""Requests whose transport and base the callers inject (HTTP linkage).

The parser records three kinds of claim (:mod:`repoatlas.parsers.http_ts_injection`):
a request on a function's destructured options, a call handing a function an options
object, and the names each such function is exported under. This stage follows the
options from where the source states them to where a request uses them:

* a **root** is a call whose argument holds no key of its caller's own options -- the
  place the transport and the base are written;
* a call inside a function that passes on its own options (whole, or inside a new
  object) is a **forward**; each step resolves its callee to exactly one function --
  declared in the same module, or exported under that name by the one module a
  relative import resolves to -- and at most :data:`MAX_HOPS` steps are followed,
  never around a cycle;
* a request is made once per root that reaches it with superagent itself as the
  transport and a string as the base (or a base the request's own file states).

Each request becomes an ``http-client-call`` claim of the request's own call -- its
file, lines and caller -- with the URL read under superagent's ``http`` default, and
records where it was injected and where its base is written. An unknown transport
reaches no request. A request whose transport is superagent but whose base no source
states -- unknown, empty (``?? ''``) or computed -- keeps its path and no origin (D2):
counted and listed, never joined. Everything is relative imports inside one
repository, so a repository's requests are decided by its claims alone.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from repoatlas.graph.resolution import ResolutionIndex
from repoatlas.models import http
from repoatlas.models.parsing import EdgeType, EntityType
from repoatlas.parsers.http_facts import (
    BASE_UNKNOWN,
    HTTP_CLIENT_CALL,
    PENDING_FACT,
    TS_FUNCTION,
    TS_INJECTED_REQUEST,
    TS_INJECTION,
)
from repoatlas.parsers.http_typescript import ClientUrl, client_url
from repoatlas.storage.protocols import FileRow, UnresolvedRefRecord

#: Functions an options object is followed through, counted from the root's callee.
MAX_HOPS = 3

SOURCE = "superagent"
_SCHEME = "http"

Key = tuple[int, str]
Spec = dict[str, Any] | None


def _meta(claim: UnresolvedRefRecord) -> dict[str, str]:
    loaded = json.loads(claim.meta_json) if claim.meta_json else {}
    return {str(k): str(v) for k, v in loaded.items()}


def plan_injected_requests(
    grouped: Mapping[str, Sequence[UnresolvedRefRecord]],
    index: ResolutionIndex,
    by_id: Mapping[int, FileRow],
) -> tuple[UnresolvedRefRecord, ...]:
    """Every request an injected transport and base reach, as join claims. Writes nothing."""
    requests: dict[Key, list[UnresolvedRefRecord]] = defaultdict(list)
    for claim in grouped.get(TS_INJECTED_REQUEST, ()):
        requests[(claim.file_id, _meta(claim).get("function", ""))].append(claim)
    if not requests:
        return ()
    exports: dict[tuple[int, str], list[str]] = defaultdict(list)
    for claim in grouped.get(TS_FUNCTION, ()):
        for name in json.loads(_meta(claim).get("exports", "[]")):
            exports[(claim.file_id, str(name))].append(claim.target_key)
    forwards: dict[Key, list[UnresolvedRefRecord]] = defaultdict(list)
    roots: list[UnresolvedRefRecord] = []
    for claim in grouped.get(TS_INJECTION, ()):
        within = _meta(claim).get("within")
        if within is None:
            roots.append(claim)
        else:
            forwards[(claim.file_id, within)].append(claim)

    def callee(claim: UnresolvedRefRecord) -> Key | None:
        meta = _meta(claim)
        origin = by_id.get(claim.file_id)
        if origin is None:
            return None
        if "local" in meta:
            return claim.file_id, meta["local"]
        module = meta.get("module", "")
        target = index.resolve_module(origin, module, "node") if module.startswith(".") else None
        found = exports.get((target.file_id, meta.get("name", "")), []) if target else []
        return (target.file_id, found[0]) if target is not None and len(found) == 1 else None

    derived: dict[tuple[int | None, str], UnresolvedRefRecord] = {}

    def visit(
        key: Key, bound: Spec, hops: int, seen: frozenset[Key], root: UnresolvedRefRecord
    ) -> None:
        for request in requests.get(key, ()):
            record = _derive(request, bound, root, by_id)
            if record is not None:
                derived[(request.id, record.target_key)] = record
        if hops >= MAX_HOPS:
            return
        for forward in forwards.get(key, ()):
            target = callee(forward)
            if target is None or target in seen:
                continue
            argument = _tagged(json.loads(_meta(forward).get("argument", "null")), forward.file_id)
            visit(target, _substitute(argument, bound), hops + 1, seen | {target}, root)

    for root in roots:
        target = callee(root)
        if target is not None:
            argument = _tagged(json.loads(_meta(root).get("argument", "null")), root.file_id)
            visit(target, argument, 1, frozenset({target}), root)
    return tuple(derived[key] for key in sorted(derived, key=lambda k: (k[0] or 0, k[1])))


def _tagged(spec: Spec, file_id: int) -> Spec:
    """The tree with each stated string marked with the file it is written in."""
    if not isinstance(spec, dict):
        return None
    if "string" in spec:
        return {**spec, "file": spec.get("file", file_id)}
    if "object" in spec and isinstance(spec["object"], dict):
        return {"object": {key: _tagged(value, file_id) for key, value in spec["object"].items()}}
    return spec


def _get(spec: Spec, key: str) -> Spec:
    if not isinstance(spec, dict) or not isinstance(spec.get("object"), dict):
        return None
    value = spec["object"].get(key)
    return value if isinstance(value, dict) else None


def _substitute(spec: Spec, bound: Spec) -> Spec:
    """A forward's argument with each key of its caller's options replaced by its value."""
    if not isinstance(spec, dict):
        return None
    if "option" in spec:
        return _get(bound, str(spec["option"]))
    if "object" in spec and isinstance(spec["object"], dict):
        return {"object": {key: _substitute(value, bound) for key, value in spec["object"].items()}}
    return spec


def _derive(
    request: UnresolvedRefRecord,
    bound: Spec,
    root: UnresolvedRefRecord,
    by_id: Mapping[int, FileRow],
) -> UnresolvedRefRecord | None:
    """The join claim ``request`` makes under one root's options, or ``None``."""
    meta = _meta(request)
    transport = _get(bound, meta.get("transport", ""))
    if transport is None or transport.get("superagent") is not True:
        return None
    value: str | None = None
    line: object = None
    file_id: object = None
    if "base_option" in meta:
        base = _get(bound, meta["base_option"])
        if base is not None and isinstance(base.get("string"), str):
            value, line, file_id = base["string"], base.get("line"), base.get("file")
    elif meta.get("base_unknown") != "true":
        value, line, file_id = meta.get("base", ""), meta.get("base_at"), request.file_id
    parts = json.loads(meta.get("rest", "[]"))
    if not value:
        # A base no source states, or one stated empty (D2): the path alone, never joined.
        template = http.route_template(parts)
        if isinstance(template, str) or not template.segments:
            return None
        read = ClientUrl(None, template.segments, request.target_key.partition(" ")[2])
        line = file_id = None
    else:
        head = value
        while parts and parts[0][0] == http.TEMPLATE_LITERAL:
            text = parts.pop(0)[1]
            if head is value and not value.endswith("/") and not text.startswith("/"):
                return None  # glued to the next text: the host itself would change
            head += text
        found = client_url(head, parts, default_scheme=_SCHEME, library=SOURCE)
        if found is None:
            return None
        read = found
    consumer, root_file = by_id.get(request.file_id), by_id.get(root.file_id)
    base_file = by_id.get(int(str(file_id))) if file_id is not None else None
    if consumer is None or root_file is None:
        return None
    claim_meta: dict[str, str] = {
        PENDING_FACT: HTTP_CLIENT_CALL,
        "method": meta.get("method", ""),
        "segments": json.dumps(list(read.segments)),
        "source": SOURCE,
        "injected_at": f"{root_file.rel_path}:{root.line_start}",
    }
    if read.origin is None:
        claim_meta["base"] = BASE_UNKNOWN
    else:
        claim_meta["origin"] = read.origin
    if base_file is not None and line is not None:
        claim_meta["base_in"] = f"{base_file.rel_path}:{line}"
    if read.scheme_from is not None:
        claim_meta["scheme_from"] = read.scheme_from
    return UnresolvedRefRecord(
        id=request.id,
        repo_id=request.repo_id,
        file_id=request.file_id,
        src_id=request.src_id,
        edge_type=EdgeType.CALLS_HTTP_ENDPOINT,
        target_kind=EntityType.HTTP_ENDPOINT,
        target_key=f"{claim_meta['method']} {read.display}",
        line_start=request.line_start,
        line_end=request.line_end,
        meta_json=json.dumps(claim_meta, sort_keys=True),
    )
