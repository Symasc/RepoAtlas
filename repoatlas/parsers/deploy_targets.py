"""Where a deployment says it is reached: Ingress hosts as deployment-target claims.

:func:`repoatlas.parsers.deploy_helm.ingress_references` reads an Ingress only in a
narrow subset, because the HTTP join links callers to handlers through it and must never
guess. Saying *where* a system is deployed asks less: which hosts, paths and schemes an
Ingress names, and which of their parts a release or an environment must still supply.
This module reads that, leniently and separately, so the strict reader keeps its subset:

* each ``kind: Ingress`` document of any YAML file -- a chart template or a plain
  manifest -- records each rule's host and each ``spec.tls[].hosts`` entry as *parts*:
  literal text, the chart's name, a ``.Values`` path (a ``range`` item as ``a.b[*].c``),
  the release name or namespace, or an *opaque* hole naming the expression it could not
  read; its paths as written; its backends as written; a literal ALB ``listen-ports``
  annotation; the values path its annotations are copied from (``with .Values.x`` around
  ``toYaml .``); and the ``if`` guards around it, a ``.Values`` truth test as a path and
  anything else as the expression written
  (:data:`~repoatlas.parsers.http_facts.DEPLOYMENT_TARGET_INGRESS`);
* a chart's ``values.yaml`` -- the literal ``listen-ports`` annotation of each
  ``annotations`` mapping in it, by the mapping's dotted path, since
  :func:`~repoatlas.parsers.deploy_helm.read_values` keeps no key holding a dot
  (:data:`~repoatlas.parsers.http_facts.DEPLOYMENT_TARGET_VALUES`).

Nothing is rendered or resolved here; nothing here is a claim that anything is live.
What cannot be read is kept as a named hole, never dropped and never guessed.
"""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath

import yaml

from repoatlas.models.parsing import EdgeType, EntityType, ParsedReference
from repoatlas.parsers.deploy_helm import _ACTION, _COMMENT, _body, _fields, _listen, _value
from repoatlas.parsers.http_facts import (
    DEPLOYMENT_TARGET_INGRESS,
    DEPLOYMENT_TARGET_VALUES,
    pending_reference,
)
from repoatlas.parsers.withhold import credential_named, secret_bearing

_LISTEN_PORTS = "alb.ingress.kubernetes.io/listen-ports"
_KIND_INGRESS = re.compile(r"kind:\s*[\"']?Ingress[\"']?\s*(#.*)?")
_CONTROL = re.compile(r"(if|else\s+if|else|range|with|end|define|block)\b")
_VALUES = re.compile(r"\.Values((?:\.[A-Za-z_][\w-]*)+)")
_FIELD_PATH = re.compile(r"(?:\.[A-Za-z_][\w-]*)+")
_ASSIGN = re.compile(r"(\$[A-Za-z_]\w*)\s*:?=\s*(.+)")
#: Filters that leave a host or path as it is.
_TRANSPARENT = frozenset({"quote", "squote", "trim"})
#: How long an opaque expression may be kept, as written.
_EXPRESSION_LIMIT = 80
#: Hosts, paths and holes one document may record.
_LIMIT = 64
_HOST_TEXT = re.compile(r"[A-Za-z0-9.*-]*")

Part = list[str]


def _short(text: str) -> str:
    text = " ".join(text.split())
    if credential_named(text) or secret_bearing(text):
        return "<withheld>"
    return text if len(text) <= _EXPRESSION_LIMIT else text[: _EXPRESSION_LIMIT - 1] + "…"


class _Scope:
    """The template scope at one point: what ``.`` is, the guards around it and the
    variables assigned so far."""

    def __init__(self) -> None:
        #: ``("range" | "with" | "if" | "define", dot, guards, hole)``; ``dot`` is a values
        #: path, ``""`` for the root and ``None`` when not read; ``hole`` what the frame's own
        #: action left unread.
        self.stack: list[
            tuple[str, str | None, list[dict[str, object]], dict[str, object] | None]
        ] = []
        self.variables: dict[str, Part] = {}

    @property
    def dot(self) -> str | None:
        for kind, dot, _, _ in reversed(self.stack):
            if kind in {"range", "with", "define"}:
                return dot
        return ""

    def guards(self) -> list[dict[str, object]]:
        return [guard for _, _, guards, _ in self.stack for guard in guards]

    def holes(self) -> list[dict[str, object]]:
        return [hole for _, _, _, hole in self.stack if hole is not None]

    def ranged(self) -> bool:
        return any(kind == "range" for kind, _, _, _ in self.stack)


def _pipeline(body: str) -> tuple[str, bool]:
    """An action's first command and whether every filter after it is transparent."""
    head, *filters = (piece.strip() for piece in body.split("|"))
    return head, all(f in _TRANSPARENT for f in filters)


def _values_path(expression: str, scope: _Scope) -> str | None:
    """The values path an expression reads directly, ``.`` taken from the scope."""
    expression = expression.strip()
    if expression.startswith("$.Values") or (expression.startswith(".Values") and scope.dot == ""):
        match = _VALUES.fullmatch(expression.removeprefix("$"))
        return match.group(1)[1:] if match else None
    dot = scope.dot
    if dot is None or dot == "":
        return None
    if expression == ".":
        return dot
    if _FIELD_PATH.fullmatch(expression):
        return f"{dot}{expression}"
    return None


def _part(body: str, scope: _Scope) -> Part:
    """One action as a part of a host or path."""
    head, transparent = _pipeline(body)
    if transparent:
        if head in {"$.Chart.Name", "$.Release.Name", "$.Release.Namespace"} or (
            scope.dot == "" and head in {".Chart.Name", ".Release.Name", ".Release.Namespace"}
        ):
            return {
                "Chart.Name": ["chart", ""],
                "Release.Name": ["release", ""],
                "Release.Namespace": ["namespace", ""],
            }[head.lstrip("$.")]
        if head in scope.variables:
            return list(scope.variables[head])
        path = _values_path(head, scope)
        if path is not None:
            return ["value", path]
    return ["opaque", _short(body)]


def _parts(text: str, scope: _Scope) -> list[Part]:
    """A scalar as written, as literal text and parts."""
    text = _value(text)
    parts: list[Part] = []
    cursor = 0
    for match in _ACTION.finditer(text):
        if match.start() > cursor:
            parts.append(["text", text[cursor : match.start()]])
        parts.append(_part(_body(match.group(1)), scope))
        cursor = match.end()
    if cursor < len(text):
        parts.append(["text", text[cursor:]])
    return parts


def _host(text: str, scope: _Scope) -> list[Part]:
    parts = _parts(text, scope)
    return [
        part if part[0] != "text" or _HOST_TEXT.fullmatch(part[1]) else ["opaque", _short(part[1])]
        for part in parts
    ]


def _guard(body: str, scope: _Scope, line: int) -> dict[str, object]:
    head, transparent = _pipeline(body)
    negated = False
    if head.startswith("not "):
        negated, head = True, head[4:].strip()
    path = _values_path(head, scope) if transparent else None
    if path is not None:
        return {"value": path, "negated": negated, "line": line}
    return {"expression": _short(body), "negated": negated, "line": line}


class _Reader:
    """One Ingress document read line by line: control actions update the scope; every
    other line is kept with the scope it stands in."""

    def __init__(self) -> None:
        self.scope = _Scope()
        self.at: dict[int, _Scope] = {}
        self.copies: list[tuple[int, int, str | None]] = []

    def _snapshot(self) -> _Scope:
        copy = _Scope()
        copy.stack = list(self.scope.stack)
        copy.variables = dict(self.scope.variables)
        return copy

    def control(self, body: str, line: int) -> None:
        keyword = _CONTROL.match(body)
        assert keyword is not None
        word, rest = keyword.group(1), body[keyword.end() :].strip()
        stack = self.scope.stack
        if word == "end":
            if stack:
                stack.pop()
            return
        if word.startswith("else"):
            if stack and stack[-1][0] == "if":
                _, dot, guards, hole = stack.pop()
                # This branch holds when no earlier one did, and its own test (if any) does.
                guards = [*guards[:-1], {**guards[-1], "negated": not guards[-1]["negated"]}]
                if word != "else":
                    guards.append(_guard(rest, self.scope, line))
                stack.append(("if", dot, guards, hole))
            return
        if word == "if":
            stack.append(("if", None, [_guard(rest, self.scope, line)], None))
        elif word == "range":
            head, _ = _pipeline(rest)
            path = _values_path(head, self.scope) if ":=" not in head else None
            hole = None
            if path is None:
                hole = {"name": _short(rest), "reason": "range", "line": line}
            stack.append(("range", f"{path}[*]" if path is not None else None, [], hole))
        elif word == "with":
            head, _ = _pipeline(rest)
            stack.append(("with", _values_path(head, self.scope), [], None))
        else:
            stack.append(("define", None, [], None))

    def scan(self, document: list[str], start: int) -> list[str]:
        """The document with every line that is only actions blanked, recording each
        line's scope and each ``toYaml`` insertion."""
        kept: list[str] = []
        for offset, raw in enumerate(document):
            number = start + offset + 1
            line = _COMMENT.sub("", raw)
            actions = list(_ACTION.finditer(line))
            bare = not _ACTION.sub("", line).strip() and bool(actions)
            for match in actions:
                body = _body(match.group(1))
                if _CONTROL.match(body):
                    self.control(body, number)
                    continue
                assigned = _ASSIGN.fullmatch(body)
                if assigned is not None:
                    self.scope.variables[assigned.group(1)] = _part(assigned.group(2), self.scope)
                elif bare and body.startswith("toYaml"):
                    head = body.removeprefix("toYaml").split("|")[0].strip()
                    path = _values_path(head, self.scope)
                    indent = len(raw) - len(raw.lstrip())
                    self.copies.append((number, indent, path))
                elif bare and body.startswith(("include", "template")):
                    indent = len(raw) - len(raw.lstrip())
                    self.copies.append((number, indent, None))
            if bare or ("{{" in line and "}}" not in line):
                kept.append("")
                continue
            self.at[number] = self._snapshot()
            kept.append(raw)
        return kept


def _documents(lines: list[str]) -> list[tuple[int, list[str]]]:
    starts = [0] + [index + 1 for index, line in enumerate(lines) if line.strip() == "---"]
    ends = [index for index, line in enumerate(lines) if line.strip() == "---"] + [len(lines)]
    return [(start, lines[start:end]) for start, end in zip(starts, ends, strict=True)]


def _enclosing(
    fields: list[tuple[tuple[str, ...], str, int]], lines: dict[int, str], number: int, indent: int
) -> tuple[str, ...]:
    """The path of the nearest key above a line and less indented than it."""
    for path, _, line in reversed(fields):
        if line >= number:
            continue
        text = lines.get(line, "")
        if len(text) - len(text.lstrip()) < indent:
            return path
    return ()


def _ingress(
    kept: list[str], document: list[str], start: int, chart: str | None, reader: _Reader
) -> ParsedReference | None:
    """One document, ``kept`` as the file's reader left it (lines of actions alone blanked)
    and ``document`` as written."""
    kind_line = next(
        (start + i + 1 for i, line in enumerate(kept) if _KIND_INGRESS.fullmatch(line.strip())),
        None,
    )
    if kind_line is None:
        return None
    end = start + len(document)
    fields, other = _fields(kept, start)
    raw = {start + i + 1: line for i, line in enumerate(document)}
    scope_at = reader.at
    empty = _Scope()
    holes: list[dict[str, object]] = [*scope_at.get(kind_line, empty).holes()]

    hosts: list[dict[str, object]] = []
    tls: list[dict[str, object]] = []
    paths: list[dict[str, object]] = []
    backends: list[list[object]] = []
    listen: list[list[object]] | None = None
    listen_line = 0
    annotations_from: str | None = None
    annotations_unread = False
    default_backend = False
    rule_paths: dict[str, list[dict[str, object]]] = {}
    path_items: dict[tuple[str, str], dict[str, tuple[str, int]]] = {}
    for path, text, line in fields:
        scope = scope_at.get(line, empty)
        if path[:2] == ("metadata", "annotations") and len(path) == 3:
            if path[2] == _LISTEN_PORTS:
                listen, listen_line = _listen(_value(text)), line
                if listen is None:
                    holes.append(
                        {"name": _LISTEN_PORTS, "reason": "listen-ports-not-literal", "line": line}
                    )
        elif path[:2] == ("spec", "defaultBackend") or path[:2] == ("spec", "backend"):
            default_backend = True
        elif path[:2] == ("spec", "rules") and len(path) >= 4:
            rule = path[2]
            if path[3:] == ("host",) and len(hosts) < _LIMIT:
                hosts.append(
                    {
                        "rule": rule,
                        "parts": _host(text, scope),
                        "line": line,
                        "guards": scope.guards(),
                    }
                )
            elif len(path) >= 7 and path[3:5] == ("http", "paths"):
                field = path[6:]
                item = path_items.setdefault((rule, path[5]), {})
                if field in {("path",), ("pathType",)}:
                    item[field[0]] = (text, line)
                elif field == ("backend", "service", "name") or field == ("backend", "serviceName"):
                    item["service"] = (text, line)
                elif field in {
                    ("backend", "service", "port", "number"),
                    ("backend", "service", "port", "name"),
                    ("backend", "servicePort"),
                }:
                    item["port"] = (text, line)
        elif path[:2] == ("spec", "tls") and len(path) == 5 and path[3] == "hosts":
            tls.append({"parts": _host(text, scope_at.get(line, empty)), "line": line})
        elif path[:2] == ("spec", "tls") and len(path) == 4 and path[3] == "hosts":
            # A flow sequence, ``hosts: [a.example, b.example]``: each entry is a host.
            flow = text.strip()
            if flow.startswith("[") and flow.endswith("]"):
                for listed_host in flow[1:-1].split(","):
                    named = listed_host.strip().strip("\"'")
                    if named and len(tls) < _LIMIT:
                        tls.append({"parts": _host(named, scope_at.get(line, empty)), "line": line})
    for path, text, line in other:
        if path[:2] == ("spec", "tls") and len(path) >= 4 and path[3] == "hosts":
            listed = text.removeprefix("-").strip()
            if listed and len(tls) < _LIMIT:
                tls.append({"parts": _host(listed, scope_at.get(line, empty)), "line": line})
    for number, indent, values_path in reader.copies:
        if not start < number <= end:
            continue
        enclosing = _enclosing(fields, raw, number, indent)
        if enclosing == ("metadata", "annotations"):
            if values_path is not None:
                annotations_from = values_path
            else:
                annotations_unread = True
    for (rule, _), item in path_items.items():
        scope = scope_at.get(item.get("path", ("", 0))[1], empty)
        written = item.get("path")
        entry: dict[str, object] = {
            "parts": _parts(written[0], scope) if written else [],
            "type": _value(item["pathType"][0]) if "pathType" in item else None,
            "line": written[1] if written else None,
        }
        listed = rule_paths.setdefault(rule, [])
        if entry["parts"] not in [p["parts"] for p in listed] and len(paths) < _LIMIT:
            listed.append(entry)
        if "service" in item:
            backend = [
                _short(_value(item["service"][0])),
                _short(_value(item["port"][0])) if "port" in item else None,
                item["service"][1],
            ]
            if backend[:2] not in [b[:2] for b in backends] and len(backends) < _LIMIT:
                backends.append(backend)
    for host in hosts:
        host["paths"] = rule_paths.get(str(host["rule"]), [])
        del host["rule"]
    ranged = any(scope.ranged() for line, scope in scope_at.items() if start < line <= end)
    meta = {
        "source": "chart-template" if chart is not None else "manifest",
        "chart": chart or "",
        "hosts": json.dumps(hosts),
        "tls": json.dumps(tls),
        "backends": json.dumps(backends),
        "listen": json.dumps(listen),
        "listen_at": str(listen_line),
        "annotations_from": json.dumps(annotations_from),
        "annotations_unread": "true" if annotations_unread else "false",
        "default_backend": "true" if default_backend else "false",
        "guards": json.dumps(_document_guards(reader, kind_line)),
        "holes": json.dumps(holes[:_LIMIT]),
        "ranged": "true" if ranged else "false",
    }
    return pending_reference(
        EdgeType.REFERENCES,
        EntityType.CONFIG,
        "Ingress",
        kind_line,
        kind_line,
        role=DEPLOYMENT_TARGET_INGRESS,
        **meta,
    )


def _document_guards(reader: _Reader, kind_line: int) -> list[dict[str, object]]:
    """The guards around the document's ``kind:`` line: what must hold for it to render."""
    scope = reader.at.get(kind_line)
    return scope.guards() if scope is not None else []


def ingress_target_references(path: PurePosixPath, content: str) -> list[ParsedReference]:
    """Each ``kind: Ingress`` document of a YAML file, read leniently for where it is
    reached (:data:`DEPLOYMENT_TARGET_INGRESS`)."""
    if path.suffix not in {".yaml", ".yml"} or "Ingress" not in content:
        return []
    chart: str | None = None
    if path.parent.name == "templates":
        parent = path.parent.parent
        chart = "" if str(parent) == "." else parent.as_posix()
    lines = content.splitlines()
    # One reader for the whole file: a `range` or `if` may enclose several documents.
    reader = _Reader()
    kept = reader.scan(lines, 0)
    found: list[ParsedReference] = []
    for start, document in _documents(lines):
        reference = _ingress(kept[start : start + len(document)], document, start, chart, reader)
        if reference is not None:
            found.append(reference)
    return found


def values_target_references(
    path: PurePosixPath, documents: list[yaml.Node]
) -> list[ParsedReference]:
    """A chart's ``values.yaml``: the literal ``listen-ports`` annotation of each
    ``annotations`` mapping, by that mapping's dotted path (:data:`DEPLOYMENT_TARGET_VALUES`)."""
    if path.name != "values.yaml" or not documents or documents[0] is None:
        return []
    found: dict[str, list[list[object]] | None] = {}
    lines: dict[str, int] = {}
    stack: list[tuple[str, yaml.Node]] = [("", documents[0])]
    visited = 0
    while stack and visited < 10_000 and len(found) < _LIMIT:
        prefix, node = stack.pop()
        visited += 1
        if not isinstance(node, yaml.MappingNode):
            continue
        for key, value in node.value:
            name = key.value if isinstance(key, yaml.ScalarNode) else None
            if not isinstance(name, str):
                continue
            if name == _LISTEN_PORTS and prefix.rsplit(".", 1)[-1] == "annotations":
                text = value.value if isinstance(value, yaml.ScalarNode) else ""
                found[prefix] = _listen(str(text))
                lines[prefix] = key.start_mark.line + 1
            elif "." not in name:
                stack.append((f"{prefix}.{name}" if prefix else name, value))
    if not found:
        return []
    parent = path.parent
    return [
        pending_reference(
            EdgeType.REFERENCES,
            EntityType.CONFIG,
            "values",
            min(lines.values()),
            min(lines.values()),
            role=DEPLOYMENT_TARGET_VALUES,
            chart="" if str(parent) == "." else parent.as_posix(),
            listen=json.dumps(found, sort_keys=True),
            lines=json.dumps(lines, sort_keys=True),
        )
    ]
