"""GraphQL Code Generator configuration (``codegen.yml``, ``codegen.yaml``, ``codegen.json``).

A configuration is the evidence that a file was generated from a schema: it names, for
each output under ``generates``, the schema it is generated from, the plugins that write
it and the options that shape what they write. This module reads that, statically, from
the YAML nodes :class:`~repoatlas.parsers.structured.StructuredParser` already composed.
Nothing is executed, loaded, fetched or resolved here, and no other file is read.

**One claim per output** (``graphql-codegen-output``), carrying:

* ``schema``: every input, root-level first, then the output's own, which GraphQL Code
  Generator merges with the root's. Each is classified as written: ``path`` (a schema
  file), ``glob``, ``url``, ``code`` (a module that builds or loads the schema),
  ``introspection`` (a JSON result), ``computed`` (an environment interpolation) or
  ``other``. A URL is stored with its user information, query and fragment removed;
* ``documents``, ``plugins`` and ``preset``, as written;
* ``options``: the configuration that decides what the plugins *call* things and how
  they write enums -- root ``config``, then the output's, then the ``typescript`` plugin's
  own, each overriding the last;
* ``refused``: why the output cannot be read statically, when it cannot.

The global pass (:mod:`repoatlas.graph.graphql_codegen`) decides, from these and from what
is indexed, which generated declarations come from which schema definitions.

**Bounds.** A configuration over :data:`MAX_BYTES`, or with more than :data:`MAX_ENTRIES`
outputs or entries in any one list, is refused whole with a diagnostic.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import PurePosixPath
from urllib.parse import urlsplit, urlunsplit

import yaml

from repoatlas.models.parsing import (
    PARSE_BOUND_EXCEEDED,
    EdgeType,
    EntityType,
    ParseDiagnostic,
    ParsedReference,
    Severity,
)
from repoatlas.parsers.http_facts import pending_reference
from repoatlas.parsers.marks import node_span

#: The pending-fact role of one configured output.
GRAPHQL_CODEGEN_OUTPUT = "graphql-codegen-output"

CONFIG_NAMES = frozenset({"codegen.yml", "codegen.yaml", "codegen.json"})

MAX_BYTES = 256 * 1024
MAX_ENTRIES = 512

#: Options that change what the generated declarations are called or how enums are
#: written. Anything else a plugin is configured with leaves the binding as it is.
NAMING_OPTIONS = frozenset(
    {
        "namingConvention",
        "typesPrefix",
        "typesSuffix",
        "enumPrefix",
        "enumSuffix",
        "enumValues",
        "enumType",
        "enumsAsTypes",
        "enumsAsConst",
        "constEnums",
        "numericEnums",
    }
)

_SDL_SUFFIXES = (".graphql", ".gql", ".graphqls")
_CODE_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
_GLOB_CHARACTERS = frozenset("*?[{")
#: An extended glob (``@(a|b)``, ``+(a)``, ``!(a)``): a pattern, however few wildcards.
_EXTGLOB = re.compile(r"[@+!*?]\(")


class _Refused(Exception):
    """The configuration is larger than this module reads."""


def codegen_references(
    path: PurePosixPath, documents: Sequence[yaml.Node | None], content: str
) -> tuple[list[ParsedReference], list[ParseDiagnostic]]:
    """One claim per output a GraphQL Code Generator configuration declares."""
    if path.name not in CONFIG_NAMES or not documents:
        return [], []
    root = documents[0]
    if not isinstance(root, yaml.MappingNode):
        return [], []
    if len(content.encode("utf-8", "replace")) > MAX_BYTES:
        return [], [_bound()]
    lines = content.splitlines()
    try:
        return _outputs(root, lines), []
    except _Refused:
        return [], [_bound()]


def _bound() -> ParseDiagnostic:
    return ParseDiagnostic(severity=Severity.WARNING, message=PARSE_BOUND_EXCEEDED)


def _outputs(root: yaml.MappingNode, lines: Sequence[str]) -> list[ParsedReference]:
    fields = _fields(root)
    generates = fields.get("generates")
    if not isinstance(generates, yaml.MappingNode):
        return []
    entries = [(k, v) for k, v in generates.value if isinstance(k, yaml.ScalarNode)]
    _check(entries)
    root_schema = _inputs(fields.get("schema"), "root")
    root_documents = _inputs(fields.get("documents"), "root")
    root_options = _options(fields.get("config"))
    merged = _has_merge(root)

    claims: list[ParsedReference] = []
    for key, value in entries:
        output = str(key.value)
        start, _ = node_span(key, lines)
        _, end = node_span(value, lines)
        own = _fields(value) if isinstance(value, yaml.MappingNode) else {}
        plugins_node = value if isinstance(value, yaml.SequenceNode) else own.get("plugins")
        plugins = _plugins(plugins_node)
        options = {
            **root_options,
            **_options(own.get("config")),
            **_plugin_options(plugins_node, "typescript"),
        }
        refused = None
        if merged or (isinstance(value, yaml.MappingNode) and _has_merge(value)):
            refused = "yaml-merge"
        elif "$" in output:
            refused = "computed-output"
        meta = {
            "output": output,
            "schema": _json([*root_schema, *_inputs(own.get("schema"), "output")]),
            "documents": _json([*root_documents, *_inputs(own.get("documents"), "output")]),
            "plugins": _json([name for name, _ in plugins]),
            "options": _json(options),
        }
        preset = _scalar(own.get("preset"))
        if preset is not None:
            meta["preset"] = preset
        if refused is not None:
            meta["refused"] = refused
        claims.append(
            pending_reference(
                EdgeType.GENERATES,
                EntityType.FILE,
                output,
                start,
                end,
                role=GRAPHQL_CODEGEN_OUTPUT,
                **meta,
            )
        )
    return claims


def _inputs(node: yaml.Node | None, level: str) -> list[dict[str, object]]:
    """A ``schema`` or ``documents`` value: a string, a list, or pointers with options."""
    if node is None:
        return []
    items: list[tuple[yaml.Node, yaml.Node | None]]
    if isinstance(node, yaml.SequenceNode):
        _check(node.value)
        items = []
        for item in node.value:
            if isinstance(item, yaml.MappingNode):
                items.extend((k, v) for k, v in item.value)
            else:
                items.append((item, None))
    elif isinstance(node, yaml.MappingNode):
        _check(node.value)
        items = list(node.value)
    else:
        items = [(node, None)]

    found: list[dict[str, object]] = []
    for pointer, settings in items:
        value = _scalar(pointer)
        line = pointer.start_mark.line + 1
        if value is None:
            found.append({"kind": "other", "value": None, "line": line, "level": level})
            continue
        kind = _classify(value)
        if (
            kind != "url"
            and isinstance(settings, yaml.MappingNode)
            and "loader" in _fields(settings)
        ):
            kind = "code"
        entry: dict[str, object] = {
            "kind": kind,
            "value": redact(value) if kind == "url" else value,
            "line": line,
            "level": level,
        }
        if kind == "glob" and value.startswith("!"):
            entry["value"] = value[1:]
            entry["negated"] = True
        found.append(entry)
    return found


def _classify(value: str) -> str:
    lowered = value.lower()
    if lowered.startswith(("http://", "https://")):
        return "url"
    if "$" in value:
        return "computed"
    if (
        value.startswith("!")
        or any(character in _GLOB_CHARACTERS for character in value)
        or _EXTGLOB.search(value)
    ):
        return "glob"
    if lowered.endswith(_SDL_SUFFIXES):
        return "path"
    if lowered.endswith(".json"):
        return "introspection"
    if lowered.endswith(_CODE_SUFFIXES):
        return "code"
    return "other"


def redact(url: str) -> str:
    """``url`` without its user information, query or fragment: where a schema would be
    fetched from, and nothing that could authenticate the fetch."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url.split(":", 1)[0] + "://"
    host = parts.netloc.rpartition("@")[2]
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def _plugins(node: yaml.Node | None) -> list[tuple[str, yaml.Node | None]]:
    if not isinstance(node, yaml.SequenceNode):
        name = _scalar(node)
        return [(name, None)] if name is not None else []
    _check(node.value)
    found: list[tuple[str, yaml.Node | None]] = []
    for item in node.value:
        if isinstance(item, yaml.MappingNode):
            found.extend((str(k.value), v) for k, v in item.value if isinstance(k, yaml.ScalarNode))
        elif (name := _scalar(item)) is not None:
            found.append((name, None))
    return found


def _plugin_options(node: yaml.Node | None, plugin: str) -> dict[str, object]:
    for name, settings in _plugins(node):
        if name == plugin and settings is not None:
            return _options(settings)
    return {}


def _options(node: yaml.Node | None) -> dict[str, object]:
    if not isinstance(node, yaml.MappingNode):
        return {}
    return {
        key: _value(value)
        for key, value in (
            (str(k.value), v) for k, v in node.value if isinstance(k, yaml.ScalarNode)
        )
        if key in NAMING_OPTIONS
    }


def _value(node: yaml.Node, depth: int = 0) -> object:
    """A plain rendering of an option's value: enough to say what it is, bounded."""
    if depth > 4:
        raise _Refused
    if isinstance(node, yaml.ScalarNode):
        return str(node.value)
    if isinstance(node, yaml.SequenceNode):
        _check(node.value)
        return [_value(item, depth + 1) for item in node.value]
    if isinstance(node, yaml.MappingNode):
        _check(node.value)
        return {str(k.value): _value(v, depth + 1) for k, v in node.value}
    return None


def _fields(node: yaml.MappingNode) -> dict[str, yaml.Node]:
    return {str(k.value): v for k, v in node.value if isinstance(k, yaml.ScalarNode)}


def _has_merge(node: yaml.MappingNode) -> bool:
    """A YAML merge key: its fields come from elsewhere, which is not read here."""
    return any(isinstance(k, yaml.ScalarNode) and k.tag.endswith(":merge") for k, _ in node.value)


def _scalar(node: yaml.Node | None) -> str | None:
    if isinstance(node, yaml.ScalarNode) and node.value != "":
        return str(node.value)
    return None


def _check(items: Sequence[object]) -> None:
    if len(items) > MAX_ENTRIES:
        raise _Refused


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
