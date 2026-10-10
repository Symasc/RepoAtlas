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
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

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
    HELM_VALUES_FILE,
    K8S_CONFIGMAP,
    K8S_WORKLOAD,
    pending_reference,
)
from repoatlas.parsers.marks import whole_file_span
from repoatlas.parsers.withhold import guarded

#: Leaves one values file may contribute.
MAX_VALUES = 256
#: Nodes one values document's walk visits: aliases can make a small file a large tree.
MAX_VALUE_NODES = 10_000
#: DF2 (D2-1): a file a release names with ``-f``, read only because it is named.
REFERENCED_VALUES_KEY = "helm-values:referenced"
REFERENCED_VALUES = "referenced-values"
#: Referenced files one repository's releases may have read.
MAX_REFERENCED = 64
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
    extra = {}
    app_version = _scalar(fields.get("appVersion"))
    if app_version is not None:
        extra["app_version"] = app_version
    return [
        pending_reference(
            EdgeType.REFERENCES,
            EntityType.CONFIG,
            name,
            line,
            line,
            role=HELM_CHART,
            chart=_directory(path),
            **extra,
        )
    ]


_VALUES_FILE = re.compile(r"values[\w.-]*\.ya?ml")


def values_references(path: PurePosixPath, documents: list[yaml.Node]) -> list[ParsedReference]:
    """A chart's ``values.yaml`` (:data:`HELM_VALUES`) or any other ``values*.y(a)ml``
    (:data:`HELM_VALUES_FILE`, an overlay a release may name): its literal scalar leaves,
    by dotted path; a list's items by index, as ``a.b[0].c``. A leaf whose path names a
    credential, or whose value carries one, is stored as ``<withheld>``."""
    if not _VALUES_FILE.fullmatch(path.name) or not documents or documents[0] is None:
        return []
    if path.name != "values.yaml":
        return [values_file_reference(path, documents[0])]
    read = read_values(documents[0])
    return [
        pending_reference(
            EdgeType.REFERENCES,
            EntityType.CONFIG,
            "values",
            1,
            1,
            role=HELM_VALUES,
            chart=_directory(path),
            **read.meta(),
        )
    ]


def values_file_reference(
    path: PurePosixPath, document: yaml.Node, **extra: str
) -> ParsedReference:
    """The :data:`HELM_VALUES_FILE` claim of a values overlay: one ``values*.y(a)ml`` the
    index read, or (DF2, ``extra``) a file a release names with ``-f`` whatever its name."""
    return pending_reference(
        EdgeType.REFERENCES,
        EntityType.CONFIG,
        path.as_posix(),
        1,
        1,
        role=HELM_VALUES_FILE,
        **read_values(document).meta(),
        **extra,
    )


def referenced_paths(
    releases: Iterable[tuple[str, int, Mapping[str, str]]],
) -> dict[str, list[str]]:
    """The repository-local files these ``ci-helm-release`` claims name with ``-f`` that no
    ``values*.y(a)ml`` claim covers, each with where it is named (``<CI file>:<line>``);
    at most :data:`MAX_REFERENCED`, in path order. A claim is a top-level helm command, so
    every path here is a proven argument; DF1 left a non-literal or outside path ``null``."""
    named: dict[str, list[str]] = defaultdict(list)
    for where, line, meta in releases:
        for path, _ in json.loads(meta.get("values", "[]")):
            if (
                isinstance(path, str)
                and path
                and not _VALUES_FILE.fullmatch(PurePosixPath(path).name)
                and f"{where}:{line}" not in named[path]
            ):
                named[path].append(f"{where}:{line}")
    return {path: sorted(named[path]) for path in sorted(named)[:MAX_REFERENCED]}


def referenced_values_meta(path: str, content: str, named_by: Sequence[str]) -> str | None:
    """The derived :data:`HELM_VALUES_FILE` claim meta of a referenced file, read as the
    structured parser reads YAML; ``None`` when it is not one YAML mapping document."""
    try:
        documents = list(yaml.compose_all(content, Loader=yaml.SafeLoader))
    except yaml.YAMLError:
        return None
    if not documents or not isinstance(documents[0], yaml.MappingNode):
        return None
    reference = values_file_reference(
        PurePosixPath(path),
        documents[0],
        derived=REFERENCED_VALUES,
        referenced_by=json.dumps(list(named_by)),
    )
    return json.dumps(reference.meta, sort_keys=True)


class Values:
    """A values document's literal leaves, each one's line, and the paths it clears."""

    def __init__(self) -> None:
        self.leaves: dict[str, str] = {}
        self.lines: dict[str, int] = {}
        #: ``[path, "null"]`` for a null, which deletes the key; ``[path, "list"]`` for an
        #: empty list, which empties it. An empty map changes nothing and is not recorded.
        self.cleared: list[list[str]] = []
        self.truncated = False

    def meta(self) -> dict[str, str]:
        return {
            "values": json.dumps(self.leaves, sort_keys=True),
            "truncated": "true" if self.truncated else "false",
            "lines": json.dumps(self.lines, sort_keys=True),
            "cleared": json.dumps(self.cleared),
        }


def read_values(document: yaml.Node) -> Values:
    """Every literal scalar leaf by dotted path (list items as ``a.b[0].c``), capped; a leaf
    whose path names a credential, or whose value carries one, is stored as ``<withheld>``."""
    read = Values()
    stack: list[tuple[str, yaml.Node]] = [("", document)]
    visited = 0
    while stack:
        prefix, node = stack.pop()
        visited += 1
        if visited > MAX_VALUE_NODES:
            read.truncated = True
            break
        if isinstance(node, yaml.MappingNode):
            for key, value in reversed(node.value):
                name = _scalar(key)
                if name is not None and "." not in name:
                    stack.append((f"{prefix}.{name}" if prefix else name, value))
        elif isinstance(node, yaml.SequenceNode) and prefix:
            if not node.value:
                read.cleared.append([prefix, "list"])
            for index in reversed(range(len(node.value))):
                stack.append((f"{prefix}[{index}]", node.value[index]))
        elif isinstance(node, yaml.ScalarNode) and prefix:
            value = _scalar(node)
            if value is None:
                read.cleared.append([prefix, "null"])
                continue
            if len(read.leaves) >= MAX_VALUES:
                read.truncated = True
                break
            read.leaves[prefix] = guarded(prefix, value)
            read.lines[prefix] = node.start_mark.line + 1
        if len(read.cleared) > MAX_VALUES:
            # A clear left out could leave a key looking set: the file is cut short.
            del read.cleared[MAX_VALUES:]
            read.truncated = True
            break
    return read


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
        selector, targets = _service_spec(document)
        meta = {
            "name": name[0],
            "ports": json.dumps([[value, line] for value, line in ports]),
            "chart": _directory(PurePosixPath(_directory(path))),
            # DF2: the workloads it selects, and the container ports it sends to.
            "selector": json.dumps(selector),
            "target_ports": json.dumps(targets),
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


_LABEL = re.compile(r"""^("[^"]*"|'[^']*'|[A-Za-z0-9][\w./-]*):(?:\s+(.*))?$""")
_INCLUDE_LINE = re.compile(r"\{\{-?\s*include\s+\"([\w.-]+)\"\s+\.\s*\|\s*nindent\s+\d+\s*-?\}\}")
_TEMPLATE_COMMENT = re.compile(r"\{\{-?\s*/\*.*?\*/\s*-?\}\}")


def _block(document: list[str], key: str, parent: int) -> tuple[int, list[str]] | None:
    """The lines under ``key:`` among the children of a section at column ``parent``, the
    key's own column and its body; ``None`` when there is no such key."""
    inside, child = False, -1
    for index, line in enumerate(document):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lead = len(line) - len(line.lstrip())
        if lead <= parent:
            if inside and lead <= parent:
                return None
            continue
        if child < 0:
            child = lead
        if lead == child and re.fullmatch(rf"{re.escape(key)}:\s*", stripped):
            body: list[str] = []
            for after in document[index + 1 :]:
                text = after.strip()
                if text and len(after) - len(after.lstrip()) <= child:
                    break
                body.append(after)
            return child, body
    return None


def _service_spec(document: list[str]) -> tuple[list[list[str]] | str | None, list[list[object]]]:
    """A Service document's selector -- ``[key, value]`` for each literal label and
    ``["include", helper]`` for each ``include "<helper>" . | nindent n`` line, ``"unread"``
    for anything else, ``None`` with no selector -- and each port's ``[port, targetPort]``
    as written (``targetPort`` ``None`` when absent)."""
    spec = next((i for i, line in enumerate(document) if re.fullmatch(r"spec:\s*", line)), None)
    if spec is None:
        return None, []
    rest = document[spec + 1 :]
    selector: list[list[str]] | str | None = None
    found = _block(rest, "selector", 0)
    if found is not None:
        selector = []
        for line in found[1]:
            text = _TEMPLATE_COMMENT.sub("", line).strip()
            if not text or text.startswith("#"):
                continue
            label = _LABEL.match(text)
            include = _INCLUDE_LINE.fullmatch(text)
            if include is not None:
                selector.append(["include", include.group(1)])
            elif label is not None and "{{" not in label.group(1):
                selector.append([_value(label.group(1)), _value(label.group(2) or "")])
            else:
                selector = "unread"
                break
    targets: list[list[object]] = []
    ports = _block(rest, "ports", 0)
    if ports is not None:
        items: list[dict[str, str]] = []
        for line in ports[1]:
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            if text.startswith("- "):
                items.append({})
                text = text[2:].strip()
            label = _LABEL.match(text)
            if items and label is not None:
                items[-1][_value(label.group(1))] = _value(label.group(2) or "")
        targets = [[item["port"], item.get("targetPort")] for item in items if "port" in item]
    return selector, targets


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


# ----------------------------------------------------------------------------- workloads
_WORKLOAD_KINDS = frozenset({"Deployment", "StatefulSet", "DaemonSet"})
_CONTAINERS = ("spec", "template", "spec", "containers")
_OPENING = frozenset({"if", "range", "with", "define", "block"})
_IMAGE_TEXT = re.compile(r"[A-Za-z0-9._:/@-]*")
#: The substitutions an image or env value is read through: a value, the chart's own
#: metadata, an optional ``default`` and output filters that do not change the text.
_SUBSTITUTION = re.compile(
    r"(?P<root>\$?\.Values(?:\.[A-Za-z_][\w-]*)+|\.Chart\.(?:Name|AppVersion|Version))"
    r"(?:\s*\|\s*default\s+(?P<default>\$?\.Values(?:\.[A-Za-z_][\w-]*)+|\.Chart\.(?:Name|AppVersion|Version)|\"[^\"]*\"))?"
    r"(?:\s*\|\s*(?:quote|toString))?"
)


def _control_stacks(lines: list[str]) -> list[tuple[tuple[str, str], ...]]:
    """The control actions open at each line, outermost first, and whether the line opens or
    closes one itself (then it is a control line, not content)."""
    blanked = _COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), "\n".join(lines))
    stacks: list[tuple[tuple[str, str], ...]] = []
    stack: list[tuple[str, str]] = []
    for line in blanked.split("\n"):
        stacks.append(tuple(stack))
        for match in _ACTION.finditer(line):
            body = _body(match.group(1))
            keyword = _CONTROL_ACTION.match(body)
            if keyword is None:
                continue
            word = keyword.group(1)
            if word in _OPENING:
                stack.append((word, body[keyword.end() :].strip()))
            elif word == "end" and stack:
                stack.pop()
    return stacks


def _documents(lines: list[str]) -> list[tuple[int, int]]:
    starts = [0] + [index + 1 for index, line in enumerate(lines) if line.strip() == "---"]
    ends = [index for index, line in enumerate(lines) if line.strip() == "---"] + [len(lines)]
    return list(zip(starts, ends, strict=True))


def _substitution_parts(text: str) -> list[list[str]] | None:
    """Literal image text and the substitutions this reads, as parts; ``None`` otherwise."""
    parts: list[list[str]] = []
    cursor = 0
    for match in _ACTION.finditer(text):
        literal = text[cursor : match.start()]
        if literal:
            if not _IMAGE_TEXT.fullmatch(literal):
                return None
            parts.append(["text", literal])
        cursor = match.end()
        read = _SUBSTITUTION.fullmatch(_body(match.group(1)))
        if read is None:
            return None
        part = (
            ["value", read.group("root").lstrip("$").removeprefix(".Values.")]
            if ".Values" in read.group("root")
            else ["chart", read.group("root").removeprefix(".Chart.")]
        )
        if read.group("default"):
            part += ["default", read.group("default")]
        parts.append(part)
    rest = text[cursor:]
    if rest:
        if not _IMAGE_TEXT.fullmatch(rest):
            return None
        parts.append(["text", rest])
    return parts or None


def _wrapper_of(stack: tuple[tuple[str, str], ...]) -> str | bool | None:
    """A document's wrapper: ``None`` for none, the ``if`` value path for one
    ``if .Values.<path>``, ``False`` for anything else."""
    if not stack:
        return None
    if len(stack) == 1 and stack[0][0] == "if":
        value = _VALUES_PATH.fullmatch(stack[0][1])
        return value.group(1)[1:] if value is not None else False
    return False


def workload_references(path: PurePosixPath, content: str) -> list[ParsedReference]:
    """Each Deployment, StatefulSet or DaemonSet document of a chart template: what its
    containers state in the subset this reads (DF1_PHASE_A.md §7), or why it is not read.

    A document wrapped in one ``if .Values.<path>`` is read under that condition; a
    ``range`` or ``with`` around it, or more than one action, leaves it unread. Inside, a
    control action enclosing ``containers`` leaves the document unread; one enclosing a
    container's image, command, args, port or environment entry leaves that field unread,
    with the reason. Environment values are never kept: an entry records its name and
    where its value comes from (``value`` and the ``.Values`` path it reads, or a
    ConfigMap's or Secret's name and key)."""
    if path.parent.name != "templates" or path.suffix not in {".yaml", ".yml"}:
        return []
    lines = content.splitlines()
    stacks = _control_stacks(lines)
    found: list[ParsedReference] = []
    for start, end in _documents(lines):
        document = lines[start:end]
        kind_at = next(
            (
                i
                for i, line in enumerate(document)
                if (m := re.fullmatch(r"kind:\s*(\w+)\s*", line)) and m.group(1) in _WORKLOAD_KINDS
            ),
            None,
        )
        if kind_at is None:
            continue
        kind = document[kind_at].split(":", 1)[1].strip()
        line = start + kind_at + 1
        base = stacks[start + kind_at]
        wrapper = _wrapper_of(base)
        meta: dict[str, str] = {"kind": kind, "chart": _directory(PurePosixPath(_directory(path)))}
        if wrapper is False:
            meta["unread"] = "its control is not one `if .Values.<path>` around the document"
        else:
            if isinstance(wrapper, str):
                meta["if"] = wrapper
            read = _workload(document, start, stacks, len(base))
            if isinstance(read, str):
                meta["unread"] = read
            else:
                meta.update(read)
        name = meta.pop("name", kind)
        found.append(
            pending_reference(
                EdgeType.REFERENCES, EntityType.CONFIG, name, line, line, role=K8S_WORKLOAD, **meta
            )
        )
    return found


def _workload(
    document: list[str], start: int, stacks: list[tuple[tuple[str, str], ...]], base: int
) -> dict[str, str] | str:
    fields, other = _fields(document, start)

    def controlled(line: int) -> bool:
        return len(stacks[line - 1]) > base or bool(_CONTROL.search(document[line - 1 - start]))

    if any(len(stacks[line - 1]) < base for _, _, line in fields):
        return "its `if` does not enclose it"
    containers_line = next((line for path_, _, line in fields if path_ == _CONTAINERS), None)
    if containers_line is None:
        return "it states no containers"
    if controlled(containers_line):
        return "a control action encloses its containers"
    # An action line that is not control can add fields where it stands (toYaml, include):
    # it leaves unread the read field it sits under; at a container's own level it may add
    # or override any field, so the container is unread. A sequence item written at its
    # key's indentation (`command:` then `- x`) belongs to the key just above it.
    touched: dict[str, set[str]] = defaultdict(set)
    for path_, text, line in other:
        if path_[: len(_CONTAINERS)] != _CONTAINERS or len(path_) < 5 or "{{" not in text:
            continue
        alone = re.fullmatch(r"\{\{(.*?)\}\}", text)
        if alone and _CONTROL_ACTION.match(_body(alone.group(1))):
            continue
        index, below = path_[4], path_[5:]
        if not below and text.lstrip().startswith("-"):
            above = [
                p[5] for p, _, ln in fields if p[:5] == path_[:5] and len(p) == 6 and ln < line
            ]
            below = (above[-1],) if above else ()
        touched[index].add(below[0] if below else "*")
    containers: dict[str, dict[str, Any]] = {}
    labels: list[list[object]] = []
    selector: list[list[object]] = []
    name = ""
    for path_, text, line in fields:
        if path_ == ("metadata", "name"):
            name = text
        elif path_[:3] == ("spec", "selector", "matchLabels") and len(path_) == 4:
            selector.append([path_[3], text])
        elif path_[:4] == ("spec", "template", "metadata", "labels") and len(path_) == 5:
            labels.append([path_[4], text])
        if path_[: len(_CONTAINERS)] != _CONTAINERS or len(path_) < 6:
            continue
        container = containers.setdefault(path_[4], {"env": [], "env_from": [], "ports": []})
        field = path_[5:]
        unread = controlled(line)
        if field == ("name",):
            container["name"] = text
        elif field == ("image",):
            parts = None if unread else _substitution_parts(text)
            if parts is None:
                container["image_unread"] = (
                    "a control action encloses it"
                    if unread
                    else "it is not literal text and the substitutions this reads"
                )
            else:
                container["image"] = parts
            container["image_line"] = line
        elif field in (("command",), ("args",)):
            container[field[0]] = (
                text if text and not unread else ("unread" if unread or not text else text)
            )
        elif len(field) >= 3 and field[0] == "ports" and field[2] in ("containerPort", "name"):
            port = _entry(container["ports"], field[1])
            port[field[2]] = "unread" if unread else text
        elif len(field) >= 3 and field[0] == "env":
            entry = _entry(container["env"], field[1])
            entry.setdefault("line", line)
            if unread:
                entry["source"] = "conditional"
            elif field[2:] == ("name",):
                entry["name"] = text
            elif field[2:] == ("value",):
                read = (
                    _SUBSTITUTION.fullmatch(_body(m.group(1)))
                    if (m := re.fullmatch(r"\{\{(.*?)\}\}", text))
                    else None
                )
                entry.setdefault("source", "value")
                if read is not None and ".Values" in read.group("root"):
                    entry["values"] = read.group("root").lstrip("$").removeprefix(".Values.")
                    if read.group("default"):
                        # The template's own value when the path renders empty (DF3a, §12.2).
                        entry["default"] = "true"
                elif "{{" in text:
                    entry["source"] = "unread"
            elif (
                len(field) == 5
                and field[2] == "valueFrom"
                and field[3] in ("configMapKeyRef", "secretKeyRef")
            ):
                entry.setdefault("source", field[3])
                # The ConfigMap's or Secret's own name and key, beside the variable's name.
                entry[f"ref_{field[4]}"] = text
            elif (
                len(field) == 4
                and field[2] == "valueFrom"
                and field[3] in ("configMapKeyRef", "secretKeyRef")
            ):
                if not text:
                    # The block form: its name and key follow, each on a line of its own.
                    entry.setdefault("source", field[3])
                else:
                    # The flow form, `configMapKeyRef: {name: x, key: y}`, when literal.
                    reference = _flow_reference(text)
                    entry["source"] = field[3] if reference is not None else "unread"
                    entry.update(reference or {})
        elif len(field) >= 3 and field[0] == "envFrom":
            source = _entry(container["env_from"], field[1])
            source.setdefault("line", line)
            if unread:
                source["source"] = "conditional"
            elif (
                len(field) == 4 and field[2] in ("configMapRef", "secretRef") and field[3] == "name"
            ):
                source.setdefault("source", field[2])
                source["name"] = text
    for path_, text, _ in other:
        alone = re.fullmatch(
            r"\{\{-?\s*include\s+\"([\w.-]+)\"\s+\.\s*\|\s*nindent\s+\d+\s*-?\}\}", text
        )
        if alone is None:
            continue
        if path_ == ("spec", "selector", "matchLabels"):
            selector.append(["include", alone.group(1)])
        elif path_ == ("spec", "template", "metadata", "labels"):
            labels.append(["include", alone.group(1)])
    for index, body in containers.items():
        added = touched.get(index, set())
        if "*" in added:
            body["unread"] = "an action at its level may add or override any field"
        if "image" in added and "image" in body:
            body["image_unread"] = "an action may change it"
            body.pop("image")
        for name in ("command", "args"):
            if name in added:
                body[name] = "unread"
        for name in ("env", "envFrom", "ports"):
            if name in added:
                body[f"{name}_open"] = "an action may add entries"
    ordered = [
        {
            **body,
            "env": [e for _, e in sorted(body["env"])],
            "env_from": [e for _, e in sorted(body["env_from"])],
            "ports": [e for _, e in sorted(body["ports"])],
        }
        for _, body in sorted(containers.items(), key=lambda item: int(item[0][1:-1]))
    ]
    return {
        "name": name,
        "containers": json.dumps(ordered, sort_keys=True),
        "labels": json.dumps(labels),
        "selector": json.dumps(selector),
    }


def _entry(listed: list[tuple[int, dict[str, Any]]], index: str) -> dict[str, Any]:
    """The list item ``index`` (``[n]``) of ``listed``, created when new."""
    number = int(index[1:-1])
    for position, entry in listed:
        if position == number:
            return entry
    entry: dict[str, Any] = {}
    listed.append((number, entry))
    return entry


def configmap_references(path: PurePosixPath, content: str) -> list[ParsedReference]:
    """Each ConfigMap document of a chart template: its name and data keys with their
    lines -- never their values -- or why it is not read."""
    if path.parent.name != "templates" or path.suffix not in {".yaml", ".yml"}:
        return []
    lines = content.splitlines()
    stacks = _control_stacks(lines)
    found: list[ParsedReference] = []
    for start, end in _documents(lines):
        document = lines[start:end]
        kind_at = next(
            (i for i, line in enumerate(document) if re.fullmatch(r"kind:\s*ConfigMap\s*", line)),
            None,
        )
        if kind_at is None:
            continue
        line = start + kind_at + 1
        base = stacks[start + kind_at]
        wrapper = _wrapper_of(base)
        meta: dict[str, str] = {"chart": _directory(PurePosixPath(_directory(path)))}
        fields, other = _fields(document, start)
        name = next((text for p, text, _ in fields if p == ("metadata", "name")), "")
        keys = [[p[1], ln] for p, _, ln in fields if len(p) == 2 and p[0] == "data"]
        inner = any(len(stacks[ln - 1]) > len(base) for p, _, ln in fields if p[:1] == ("data",))
        if wrapper is False:
            meta["unread"] = "its control is not one `if .Values.<path>` around the document"
        elif inner or any(p[:1] == ("data",) for p, _, _ in other):
            meta["unread"] = "an action stands among its data"
        elif "{{" in name:
            meta["unread"] = "its name is not literal"
        else:
            if isinstance(wrapper, str):
                meta["if"] = wrapper
            meta["keys"] = json.dumps(keys)
        found.append(
            pending_reference(
                EdgeType.REFERENCES,
                EntityType.CONFIG,
                name or "ConfigMap",
                line,
                line,
                role=K8S_CONFIGMAP,
                **meta,
            )
        )
    return found


def _flow_reference(text: str) -> dict[str, str] | None:
    """``{name: x, key: y}`` written literally: a ConfigMap's or Secret's name and key."""
    if not (text.startswith("{") and text.endswith("}")) or "{{" in text or len(text) > 256:
        return None
    try:
        found = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(found, dict) or set(found) != {"name", "key"}:
        return None
    if not all(isinstance(value, str) for value in found.values()):
        return None
    return {"ref_name": found["name"], "ref_key": found["key"]}
