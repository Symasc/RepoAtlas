"""Helm charts, Services and release bindings as claims for deployment identities (HTTP linkage).

A Kubernetes Service named ``svc-c7`` is reached at ``http://svc-c7`` (port 80) inside
its cluster. A Helm chart rarely writes that name: it writes
``{{ include "svc-c7.fullname" . }}``, which Helm renders from the chart's name, its
values and the *release* name the deployment chooses. Nothing here renders a template.
Each file records what it states, as claims for the global pass
(:mod:`repoatlas.graph.deploy_identity`), which composes them only where every input is
literal:

* ``Chart.yaml`` -- the chart's ``name`` (:data:`~repoatlas.parsers.http_facts.HELM_CHART`);
* a chart's ``values.yaml`` -- its literal scalar leaves by dotted path, list items as
  ``a.b[0].c``, capped (:data:`~repoatlas.parsers.http_facts.HELM_VALUES`);
* a template under ``templates/`` declaring ``kind: Service`` -- the raw text of its
  ``metadata.name`` and of each ``spec.ports[].port``, with their lines
  (:data:`~repoatlas.parsers.http_facts.HELM_SERVICE`). It is read by lines and
  indentation, because a template is not YAML until rendered; a document holding any
  control action (``if``, ``range``, ``with``, ``define``, ``template``, ``block``) is
  recorded as such and read no further, since it may render differently;
* a template declaring ``kind: Ingress`` -- in a narrow static subset only
  (:data:`~repoatlas.parsers.http_facts.HELM_INGRESS`): no control, or one
  ``if .Values.<path>`` around one ``range .Values.<path>``; each rule's host as literal
  text and the substitutions ``$.Chart.Name``, ``$.Values.<path>`` and the range item's
  ``.<key>``; the scheme from a literal ALB ``listen-ports`` annotation; each path ``/*``
  or ``/`` with no rewrite; and the backend's Service name and port as written. Anything
  else in what it reads makes the document unread, with the reason;
* a ``.tpl`` file under ``templates/`` -- which ``<x>.name`` and ``<x>.fullname`` helpers
  it defines *exactly* as ``helm create`` scaffolds them: the sequence of template
  actions, comments and whitespace aside, must be the scaffold's
  (:data:`~repoatlas.parsers.http_facts.HELM_HELPER`). Anything else defined there is
  not recorded, and an ``include`` of it is unread;
* a ``Tiltfile`` -- each ``helm('<chart dir>', name='<release>', set=[...])`` call with
  literal arguments: the release name a local deployment binds
  (:data:`~repoatlas.parsers.http_facts.HELM_RELEASE`). A ``values=`` file list is
  recorded as such, since those files may override anything.

Parsed, never rendered or run (V1_SPEC §17).
"""

from __future__ import annotations

import ast
import json
import posixpath
import re
from collections import defaultdict
from pathlib import Path, PurePosixPath

import yaml

from repoatlas.models.parsing import (
    EdgeType,
    EntityType,
    ParsedFile,
    ParsedReference,
    ParsedSymbol,
)
from repoatlas.parsers.http_facts import (
    HELM_CHART,
    HELM_HELPER,
    HELM_INGRESS,
    HELM_RELEASE,
    HELM_SERVICE,
    HELM_VALUES,
    pending_reference,
)
from repoatlas.parsers.marks import whole_file_span

#: Leaves one values file may contribute.
MAX_VALUES = 256
_NULL_TAG = "tag:yaml.org,2002:null"
_ACTION = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
_COMMENT = re.compile(r"\{\{-?\s*/\*.*?\*/\s*-?\}\}", re.DOTALL)
_CONTROL = re.compile(r"\{\{-?\s*(if|else|range|with|end|define|template|block)\b")
_KEY = re.compile(r"^(\s*)(-\s+)?([A-Za-z_][\w.-]*):(.*)$")
_SPACE = re.compile(r"\s+")
_CONTROL_ACTION = re.compile(r"(if|else|range|with|end|define|template|block)\b")
_VALUES_PATH = re.compile(r"\.Values((?:\.[A-Za-z_][\w-]*)+)")
#: A block mapping key -- annotation keys hold dots and slashes -- and its value, if any.
_FIELD = re.compile(r"""^(\s*)(-\s+)?("[^"]*"|'[^']*'|[A-Za-z0-9_][\w./-]*):(?:\s+(.*))?$""")
_HOST_TEXT = re.compile(r"[a-z0-9.-]*")
_ITEM_KEY = re.compile(r"\.([a-z_][\w-]*(?:\.[A-Za-z_][\w-]*)*)")
_LISTEN_PORTS = "alb.ingress.kubernetes.io/listen-ports"
#: Annotations that rewrite a path, or act on a request, before it reaches the backend.
_REWRITES = re.compile(r"[^/]+/(.*rewrite.*|actions\..+|conditions\..+|app-root|use-regex)")
_INGRESS_PATHS = frozenset({("/*", "ImplementationSpecific"), ("/", "Prefix")})
#: The fields of an Ingress rule this reads, relative to the rule; ``True`` for a leaf.
_RULE_FIELDS: dict[tuple[str, ...], bool] = {
    ("host",): True,
    ("http",): False,
    ("http", "paths"): False,
    ("http", "paths", "[]", "path"): True,
    ("http", "paths", "[]", "pathType"): True,
    ("http", "paths", "[]", "backend"): False,
    ("http", "paths", "[]", "backend", "service"): False,
    ("http", "paths", "[]", "backend", "service", "name"): True,
    ("http", "paths", "[]", "backend", "service", "port"): False,
    ("http", "paths", "[]", "backend", "service", "port", "number"): True,
}

#: The ``helm create`` scaffold's helpers, as their action sequences; ``{chart}`` is the
#: helper's own prefix.
_SCAFFOLD = {
    "name": (
        'define "{chart}.name"',
        'default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-"',
        "end",
    ),
    "fullname": (
        'define "{chart}.fullname"',
        "if .Values.fullnameOverride",
        '.Values.fullnameOverride | trunc 63 | trimSuffix "-"',
        "else",
        "$name := default .Chart.Name .Values.nameOverride",
        "if contains $name .Release.Name",
        '.Release.Name | trunc 63 | trimSuffix "-"',
        "else",
        'printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-"',
        "end",
        "end",
        "end",
    ),
}


def _directory(path: PurePosixPath) -> str:
    return "" if str(path.parent) == "." else str(path.parent)


def _scalar(node: yaml.Node | None) -> str | None:
    if not isinstance(node, yaml.ScalarNode) or node.tag == _NULL_TAG:
        return None
    return str(node.value)


def chart_references(path: PurePosixPath, documents: list[yaml.Node]) -> list[ParsedReference]:
    """``Chart.yaml``: the chart's name, for the chart directory it sits in."""
    if path.name != "Chart.yaml" or not documents:
        return []
    root = documents[0]
    if not isinstance(root, yaml.MappingNode):
        return []
    fields = {str(k.value): v for k, v in root.value if isinstance(k, yaml.ScalarNode)}
    name = _scalar(fields.get("name"))
    if name is None or "apiVersion" not in fields:
        return []
    line = fields["name"].start_mark.line + 1
    return [
        pending_reference(
            EdgeType.REFERENCES,
            EntityType.CONFIG,
            name,
            line,
            line,
            role=HELM_CHART,
            chart=_directory(path),
        )
    ]


def values_references(path: PurePosixPath, documents: list[yaml.Node]) -> list[ParsedReference]:
    """A chart's ``values.yaml``: its literal scalar leaves, by dotted path; a list's items
    by index, as ``a.b[0].c``."""
    if path.name != "values.yaml" or not documents or documents[0] is None:
        return []
    leaves: dict[str, str] = {}
    truncated = False
    stack: list[tuple[str, yaml.Node]] = [("", documents[0])]
    while stack:
        prefix, node = stack.pop()
        if isinstance(node, yaml.MappingNode):
            for key, value in reversed(node.value):
                name = _scalar(key)
                if name is not None and "." not in name:
                    stack.append((f"{prefix}.{name}" if prefix else name, value))
        elif isinstance(node, yaml.SequenceNode) and prefix:
            for index in reversed(range(len(node.value))):
                stack.append((f"{prefix}[{index}]", node.value[index]))
        elif isinstance(node, yaml.ScalarNode) and prefix:
            value = _scalar(node)
            if value is not None:
                if len(leaves) >= MAX_VALUES:
                    truncated = True
                    break
                leaves[prefix] = value
    return [
        pending_reference(
            EdgeType.REFERENCES,
            EntityType.CONFIG,
            "values",
            1,
            1,
            role=HELM_VALUES,
            chart=_directory(path),
            values=json.dumps(leaves, sort_keys=True),
            truncated="true" if truncated else "false",
        )
    ]


def _value(text: str) -> str:
    """A YAML value as written, quotes and a trailing comment aside."""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text.split(" #", 1)[0].rstrip()


def service_references(path: PurePosixPath, content: str) -> list[ParsedReference]:
    """Each ``kind: Service`` document of a chart template: its name and ports, as written."""
    if path.parent.name != "templates" or path.suffix not in {".yaml", ".yml"}:
        return []
    found: list[ParsedReference] = []
    lines = content.splitlines()
    starts = [0] + [index + 1 for index, line in enumerate(lines) if line.strip() == "---"]
    ends = [index for index, line in enumerate(lines) if line.strip() == "---"] + [len(lines)]
    for start, end in zip(starts, ends, strict=True):
        document = lines[start:end]
        if not any(re.fullmatch(r"kind:\s*Service\s*", line) for line in document):
            continue
        name: tuple[str, int] | None = None
        ports: list[tuple[str, int]] = []
        section, child, sub, ports_indent = "", -1, "", -1
        for offset, line in enumerate(document):
            match = _KEY.match(line)
            if match is None:
                continue
            lead, dash = len(match.group(1)), bool(match.group(2))
            key, rest, number = match.group(3), match.group(4), start + offset + 1
            if lead == 0 and not dash:
                section, child, sub, ports_indent = key, -1, "", -1
                continue
            if child < 0 and not dash:
                child = lead  # the indentation of the section's own keys
            if section == "metadata" and not dash and lead == child:
                if key == "name" and name is None:
                    name = (_value(rest), number)
            elif section == "spec":
                if not dash and lead == child:
                    sub, ports_indent = key, lead
                elif sub == "ports" and key == "port" and (lead > ports_indent or dash):
                    ports.append((_value(rest), number))
        if name is None:
            continue
        controlled = any(_CONTROL.search(line) for line in document)
        meta = {
            "name": name[0],
            "ports": json.dumps([[value, line] for value, line in ports]),
            "chart": _directory(PurePosixPath(_directory(path))),
        }
        if controlled:
            meta["controlled"] = "true"
        found.append(
            pending_reference(
                EdgeType.REFERENCES,
                EntityType.CONFIG,
                "Service",
                name[1],
                name[1],
                role=HELM_SERVICE,
                **meta,
            )
        )
    return found


def _body(action: str) -> str:
    """An action's text, its trim markers and outer spacing dropped."""
    return action.strip().removeprefix("-").removesuffix("-").strip()


def _wrapper(content: str) -> tuple[str, str, int, int] | str | None:
    """The ``if`` path, ``range`` path, ``range`` line and first ``end`` line of a template
    wrapped in exactly one ``if .Values.<path>`` around one ``range .Values.<path>``;
    ``None`` with no control at all; otherwise why it is not read."""
    blanked = _COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), content)
    controls: list[tuple[str, str, int]] = []
    line, cursor = 1, 0
    for match in _ACTION.finditer(blanked):
        body = _body(match.group(1))
        keyword = _CONTROL_ACTION.match(body)
        if keyword is not None:
            if len(controls) == 4:
                return "its control is not one `if` around one `range`"
            line += blanked.count("\n", cursor, match.start())
            cursor = match.start()
            controls.append((keyword.group(1), body[keyword.end() :].strip(), line))
    if not controls:
        return None
    if [keyword for keyword, _, _ in controls] != ["if", "range", "end", "end"]:
        return "its control is not one `if` around one `range`"
    condition = _VALUES_PATH.fullmatch(controls[0][1])
    iterable = _VALUES_PATH.fullmatch(controls[1][1])
    if condition is None or iterable is None or controls[2][1] or controls[3][1]:
        return "its `if` or `range` does not read one value directly"
    return condition.group(1)[1:], iterable.group(1)[1:], controls[1][2], controls[2][2]


def _host(text: str, ranged: bool) -> list[list[str]] | None:
    """A host as parts: ``["text", t]``, ``["chart", ""]``, ``["value", path]`` or
    ``["item", key]``; ``None`` when any action is not one of these, or the text is not
    lowercase host characters."""
    parts: list[list[str]] = []
    cursor = 0
    for match in _ACTION.finditer(text):
        parts.append(["text", text[cursor : match.start()]])
        cursor = match.end()
        body = _body(match.group(1))
        value = _VALUES_PATH.fullmatch(body.removeprefix("$")) if body[:1] in "$." else None
        item = _ITEM_KEY.fullmatch(body) if ranged else None
        if body == "$.Chart.Name" or (body == ".Chart.Name" and not ranged):
            parts.append(["chart", ""])
        elif value is not None and (body.startswith("$") or not ranged):
            parts.append(["value", value.group(1)[1:]])
        elif item is not None:
            parts.append(["item", item.group(1)])
        else:
            return None
    parts.append(["text", text[cursor:]])
    parts = [part for part in parts if part != ["text", ""]]
    if not parts or any(k == "text" and not _HOST_TEXT.fullmatch(v) for k, v in parts):
        return None
    return parts


def _fields(
    document: list[str], start: int
) -> tuple[list[tuple[tuple[str, ...], str, int]], list[tuple[tuple[str, ...], str, int]]]:
    """A template document's block mapping keys, each by its path from the root (a list
    item as ``[n]``) with its value as written and its line; and every other non-blank
    line, by the path of the key enclosing it. Read by indentation, since a template is
    not YAML until rendered."""
    fields: list[tuple[tuple[str, ...], str, int]] = []
    other: list[tuple[tuple[str, ...], str, int]] = []
    stack: list[tuple[int, str]] = []
    items: dict[tuple[str, ...], int] = defaultdict(int)
    for offset, line in enumerate(document):
        stripped = line.strip()
        number = start + offset + 1
        if not stripped or stripped.startswith("#") or stripped == "---":
            continue
        match = _FIELD.match(line)
        if match is None:
            column = len(line) - len(line.lstrip())
            while stack and stack[-1][0] >= column:
                stack.pop()
            other.append((tuple(name for _, name in stack), stripped, number))
            continue
        lead = len(match.group(1))
        if match.group(2):
            # A new item: of the sequence under the key it is indented beneath, or at
            # that key's own indentation.
            while stack and stack[-1][0] > lead:
                stack.pop()
            if stack and stack[-1][0] == lead and stack[-1][1].startswith("["):
                stack.pop()
            parent = tuple(name for _, name in stack)
            stack.append((lead, f"[{items[parent]}]"))
            items[parent] += 1
            column = lead + len(match.group(2))
        else:
            column = lead
            while stack and stack[-1][0] >= column:
                stack.pop()
        key = match.group(3)
        if key[:1] in "\"'":
            key = key[1:-1]
        stack.append((column, key))
        fields.append((tuple(name for _, name in stack), _value(match.group(4) or ""), number))
    return fields, other


def _abstract(path: tuple[str, ...]) -> tuple[str, ...]:
    return tuple("[]" if name.startswith("[") else name for name in path)


def _ingress(
    document: list[str], start: int, wrapper: tuple[str, str, int, int] | None
) -> dict[str, str] | str:
    """What an Ingress document states that this reads, or why it is not read."""
    fields, other = _fields(document, start)
    if wrapper is not None and not all(wrapper[2] < line < wrapper[3] for _, _, line in fields):
        return "its `if` and `range` do not enclose it"
    for path, text, _ in other:
        # A line that is not a key can add a field this reads where it stands; elsewhere
        # (labels, tls) it changes nothing read. The wrapper's own lines are read above.
        alone = re.fullmatch(r"\{\{(.*?)\}\}", text)
        if alone is not None and _CONTROL_ACTION.match(_body(alone.group(1))):
            continue
        if path in {(), ("metadata",), ("spec",)} or path[:2] in {
            ("metadata", "annotations"),
            ("spec", "rules"),
        }:
            return "an action stands where it may add a field this reads"
    ranged = wrapper is not None
    listen: list[list[object]] | None = None
    scheme_at = 0
    rules: dict[str, tuple[str, int] | None] = {}
    paths: dict[tuple[str, str], dict[str, tuple[str, int]]] = defaultdict(dict)
    for path, text, line in fields:
        if path[:2] == ("metadata", "annotations"):
            if len(path) == 2:
                if text:
                    return "its annotations are not a block of literal keys"
                continue
            key = path[2]
            if _REWRITES.fullmatch(key):
                return f"its annotation `{key}` rewrites or acts on a request"
            if key == _LISTEN_PORTS and len(path) == 3:
                listen, scheme_at = _listen(text), line
                if listen is None:
                    return "its listen-ports annotation is not literal JSON of HTTP or HTTPS ports"
            continue
        if path[:2] != ("spec", "rules"):
            continue
        if len(path) == 2:
            if text:
                return "its rules are not a block list"
            continue
        rule, shape = path[2], _abstract(path[3:])
        leaf = _RULE_FIELDS.get(shape)
        if leaf is None or leaf != bool(text):
            return f"its rule field `{'.'.join(shape)}` is not one this reads"
        rules.setdefault(rule, None)
        if shape == ("host",):
            rules[rule] = (text, line)
        elif leaf:
            paths[(rule, path[5])][shape[-1]] = (text, line)
    if listen is None:
        return "it has no listen-ports annotation, so its scheme is not read"
    if not rules:
        return "it has no rules"
    hosts: list[list[object]] = []
    backends: list[list[object]] = []
    for rule, host in rules.items():
        if host is None:
            return "a rule without a host answers any host"
        text, line = host
        parts = _host(text, ranged)
        if parts is None:
            return "a host is not literal text and the substitutions this reads"
        hosts.append([parts, line])
        served = [fields for (owner, _), fields in paths.items() if owner == rule]
        if not served:
            return "a rule serves no path"
        for path_fields in served:
            shape = (path_fields.get("path", ("", 0))[0], path_fields.get("pathType", ("", 0))[0])
            if shape not in _INGRESS_PATHS:
                return "a path is not `/*` (ImplementationSpecific) or `/` (Prefix)"
            if "name" not in path_fields or "number" not in path_fields:
                return "a backend is not a Service by name and port number"
            backend = [path_fields["name"][0], path_fields["number"][0], path_fields["name"][1]]
            if backend not in backends:
                backends.append(backend)
    meta = {
        "hosts": json.dumps(hosts),
        "listen": json.dumps(listen),
        "scheme_at": str(scheme_at),
        "backends": json.dumps(backends),
    }
    if wrapper is not None:
        meta["if"], meta["range"] = wrapper[0], wrapper[1]
    return meta


def _listen(text: str) -> list[list[object]] | None:
    """An ALB ``listen-ports`` annotation's ``[scheme, port]`` pairs, when literal."""
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, list) or not decoded:
        return None
    found: list[list[object]] = []
    for entry in decoded:
        if not isinstance(entry, dict) or len(entry) != 1:
            return None
        ((scheme, port),) = entry.items()
        if scheme not in {"HTTP", "HTTPS"} or type(port) is not int or not 0 < port < 65536:
            return None
        found.append([scheme.lower(), port])
    return found


def ingress_references(path: PurePosixPath, content: str) -> list[ParsedReference]:
    """Each ``kind: Ingress`` document of a chart template: what it states in the subset
    this reads, or why it is not read."""
    if path.parent.name != "templates" or path.suffix not in {".yaml", ".yml"}:
        return []
    lines = content.splitlines()
    starts = [0] + [index + 1 for index, line in enumerate(lines) if line.strip() == "---"]
    ends = [index for index, line in enumerate(lines) if line.strip() == "---"] + [len(lines)]
    wrapper: tuple[str, str, int, int] | str | None = None
    found: list[ParsedReference] = []
    for start, end in zip(starts, ends, strict=True):
        document = lines[start:end]
        kind = next(
            (i for i, line in enumerate(document) if re.fullmatch(r"kind:\s*Ingress\s*", line)),
            None,
        )
        if kind is None:
            continue
        if not found:
            wrapper = _wrapper(content)
        read = wrapper if isinstance(wrapper, str) else _ingress(document, start, wrapper)
        meta = {"unread": read} if isinstance(read, str) else read
        found.append(
            pending_reference(
                EdgeType.REFERENCES,
                EntityType.CONFIG,
                "Ingress",
                start + kind + 1,
                start + kind + 1,
                role=HELM_INGRESS,
                chart=_directory(PurePosixPath(_directory(path))),
                **meta,
            )
        )
    return found


def _actions(text: str) -> list[str]:
    """The template actions of ``text``, comments dropped, trim markers and spacing
    normalised; ``[]`` with a non-blank text between two actions marks it as ``"text"``."""
    text = _COMMENT.sub("", text)
    found: list[str] = []
    cursor = 0
    for match in _ACTION.finditer(text):
        if text[cursor : match.start()].strip():
            found.append("text")
        body = match.group(1).strip()
        body = body.removeprefix("-").removesuffix("-").strip()
        found.append(_SPACE.sub(" ", body))
        cursor = match.end()
    if text[cursor:].strip():
        found.append("text")
    return found


def scaffold_helpers(content: str) -> list[tuple[str, int]]:
    """``<x>.name`` and ``<x>.fullname`` defined exactly as ``helm create`` does, with lines."""
    actions = _actions(content)
    # Comments become as many newlines as they span, so every action keeps its line.
    blanked = _COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), content)
    lines = [blanked.count("\n", 0, match.start()) + 1 for match in _ACTION.finditer(blanked)]
    found: list[tuple[str, int]] = []
    index = 0
    position = 0  # index among the real actions, for their lines
    while index < len(actions):
        action = actions[index]
        defined = re.fullmatch(r'define "([\w.-]+)\.(name|fullname)"', action)
        if defined is not None:
            chart, kind = defined.group(1), defined.group(2)
            expected = tuple(step.format(chart=chart) for step in _SCAFFOLD[kind])
            if tuple(actions[index : index + len(expected)]) == expected:
                found.append((f"{chart}.{kind}", lines[position] if position < len(lines) else 1))
        if action != "text":
            position += 1
        index += 1
    return found


class HelmHelpersParser:
    """``.tpl`` files under a chart's ``templates/``: the scaffold helpers they define."""

    name = "helm-helpers"
    version = "1"
    language = "Helm template"

    def supports(self, path: Path) -> bool:
        return path.suffix == ".tpl" and path.parent.name == "templates"

    def parse(self, path: Path, content: str) -> ParsedFile:
        chart = _directory(PurePosixPath(_directory(PurePosixPath(path.as_posix()))))
        references = [
            pending_reference(
                EdgeType.REFERENCES,
                EntityType.CONFIG,
                helper,
                line,
                line,
                role=HELM_HELPER,
                chart=chart,
            )
            for helper, line in scaffold_helpers(content)
        ]
        start, last = whole_file_span(content)
        module = ParsedSymbol(
            kind=EntityType.CONFIG,
            name=path.name,
            qualified_name=path.as_posix(),
            line_start=start,
            line_end=last,
        )
        return ParsedFile(
            language=self.language,
            parser=self.name,
            parser_version=self.version,
            symbols=(module,),
            references=tuple(references),
        )


class TiltfileParser:
    """A Tiltfile's ``helm()`` calls: the release names a local deployment binds."""

    name = "tiltfile"
    version = "1"
    language = "Starlark"

    def supports(self, path: Path) -> bool:
        return path.name == "Tiltfile"

    def parse(self, path: Path, content: str) -> ParsedFile:
        directory = _directory(PurePosixPath(path.as_posix()))
        references: list[ParsedReference] = []
        try:
            tree = ast.parse(content)
        except SyntaxError:
            tree = None
        for node in ast.walk(tree) if tree is not None else ():
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "helm"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                continue
            keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
            name = keywords.get("name")
            if not (isinstance(name, ast.Constant) and isinstance(name.value, str)):
                continue
            chart = posixpath.normpath(posixpath.join(directory or ".", node.args[0].value))
            if chart == ".." or chart.startswith(("../", "/")):
                continue
            meta = {"chart": "" if chart == "." else chart}
            settings = keywords.get("set")
            if settings is not None:
                listed = settings.elts if isinstance(settings, ast.List | ast.Tuple) else None
                literal = [
                    item.value
                    for item in listed or []
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)
                ]
                meta["set"] = json.dumps(
                    literal if listed is not None and len(literal) == len(listed) else None
                )
            if "values" in keywords:
                meta["values_files"] = "true"
            references.append(
                pending_reference(
                    EdgeType.REFERENCES,
                    EntityType.CONFIG,
                    name.value,
                    node.lineno,
                    node.end_lineno or node.lineno,
                    role=HELM_RELEASE,
                    **meta,
                )
            )
        start, last = whole_file_span(content)
        module = ParsedSymbol(
            kind=EntityType.CONFIG,
            name=path.name,
            qualified_name=path.as_posix(),
            line_start=start,
            line_end=last,
        )
        return ParsedFile(
            language=self.language,
            parser=self.name,
            parser_version=self.version,
            symbols=(module,),
            references=tuple(references),
        )
