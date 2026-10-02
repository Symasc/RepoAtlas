"""Helm charts, Services and release bindings as claims for deployment identities (HTTP linkage).

A Kubernetes Service named ``svc-c7`` is reached at ``http://svc-c7`` (port 80) inside
its cluster. A Helm chart rarely writes that name: it writes
``{{ include "svc-c7.fullname" . }}``, which Helm renders from the chart's name, its
values and the *release* name the deployment chooses. Nothing here renders a template.
Each file records what it states, as claims for the global pass
(:mod:`repoatlas.graph.deploy_identity`), which composes them only where every input is
literal:

* ``Chart.yaml`` -- the chart's ``name`` (:data:`~repoatlas.parsers.http_facts.HELM_CHART`);
* a chart's ``values.yaml`` -- its literal scalar leaves by dotted path, capped
  (:data:`~repoatlas.parsers.http_facts.HELM_VALUES`);
* a template under ``templates/`` declaring ``kind: Service`` -- the raw text of its
  ``metadata.name`` and of each ``spec.ports[].port``, with their lines
  (:data:`~repoatlas.parsers.http_facts.HELM_SERVICE`). It is read by lines and
  indentation, because a template is not YAML until rendered; a document holding any
  control action (``if``, ``range``, ``with``, ``define``, ``template``, ``block``) is
  recorded as such and read no further, since it may render differently;
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
    """A chart's ``values.yaml``: its literal scalar leaves, by dotted path."""
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
