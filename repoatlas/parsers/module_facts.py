"""Module-resolution facts from the files that declare them (workspace import resolution).

A bare import specifier -- ``@acme/contracts`` or ``@/api/client`` -- names a file
only through configuration: a workspace declares which directories are packages, a
package manifest names its entry points, and a ``tsconfig`` maps path aliases. The
global link pass needs those values, and it must take them from what was indexed,
never from the working tree at link time, so they are recorded here, when the file
is parsed, as one claim per file.

Recorded, and nothing else:

* ``tsconfig*.json`` / ``jsconfig*.json``: ``extends``, ``include`` and the
  ``compilerOptions`` ``baseUrl``, ``paths``, ``rootDir`` and ``outDir``. These files
  allow comments and trailing commas, which are stripped before reading;
* ``package.json``: ``name``, the entry fields ``exports``, ``source``, ``types``,
  ``typings``, ``module`` and ``main``, and ``workspaces``;
* ``pnpm-workspace.yaml``: ``packages``.

Values are kept only in the shapes resolution reads (strings, lists of strings, and
``exports``/``paths`` mappings), and a document too large to be configuration
contributes nothing, so one generated file cannot swamp the claims table.
"""

from __future__ import annotations

import json
import re
from typing import Any

import yaml

from repoatlas.models.parsing import EdgeType, EntityType, ParsedReference

#: The ``meta`` key that marks a reference as a module-resolution fact.
MODULE_RESOLUTION = "module_resolution"
TSCONFIG = "tsconfig"
PACKAGE = "package"
WORKSPACE = "workspace"
#: A Python project's ``pyproject.toml`` (repoatlas.parsers.python_project).
PYTHON_PROJECT = "python-project"
#: A setuptools ``setup.cfg`` or ``setup.py`` (repoatlas.parsers.setuptools_project).
PYTHON_SETUPTOOLS = "python-setuptools"
#: A requirements file a repository's root ``requirements.txt`` reaches through ``-r``, the
#: root included (repoatlas.parsers.requirements): recorded by the sync for the files it
#: reaches, never by a parser for a file of some name (repoatlas.graph.py_requirements).
PYTHON_REQUIREMENTS = "python-requirements"

#: Larger than any hand-written configuration; beyond it a file contributes nothing.
_MAX_BYTES = 256 * 1024
_MAX_ENTRIES = 512
_TSCONFIG = re.compile(r"^(tsconfig|jsconfig)(\.[\w.-]+)?\.json$")
_ENTRY_FIELDS = ("source", "types", "typings", "module", "main")
_COMPILER_OPTIONS = ("baseUrl", "rootDir", "outDir")


def kind_for(name: str) -> str | None:
    """Which module-resolution fact a file of this name declares, if any."""
    if _TSCONFIG.match(name):
        return TSCONFIG
    if name == "package.json":
        return PACKAGE
    if name == "pnpm-workspace.yaml":
        return WORKSPACE
    return None


def strip_jsonc(text: str) -> str:
    """JSON with comments and trailing commas, as ``tsconfig`` allows, made plain JSON.

    String-aware, so ``//`` in a ``$schema`` URL and ``,}`` inside a value are kept.
    """
    out: list[str] = []
    pending_comma = False
    index, length = 0, len(text)
    while index < length:
        char = text[index]
        if char == '"':
            end = index + 1
            while end < length and text[end] != '"':
                end += 2 if text[end] == "\\" else 1
            if pending_comma:
                out.append(",")
                pending_comma = False
            out.append(text[index : end + 1])
            index = end + 1
        elif text.startswith("//", index):
            newline = text.find("\n", index)
            index = length if newline == -1 else newline
        elif text.startswith("/*", index):
            close = text.find("*/", index + 2)
            index = length if close == -1 else close + 2
        elif char == ",":
            if pending_comma:
                out.append(",")
            pending_comma = True
            index += 1
        elif char.isspace():
            out.append(char)
            index += 1
        else:
            # A comma directly before a closing bracket is the trailing comma JSON forbids.
            if pending_comma and char not in "}]":
                out.append(",")
            pending_comma = False
            out.append(char)
            index += 1
    return "".join(out)


def _strings(value: Any) -> list[str] | None:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value[:_MAX_ENTRIES])
    return None


def _exports(value: Any, depth: int = 0) -> Any:
    """An ``exports`` value with only string leaves kept; ``None`` when nothing is left."""
    if isinstance(value, str):
        return value
    if depth > 4:
        return None
    if isinstance(value, list):
        kept = [v for v in (_exports(item, depth + 1) for item in value[:_MAX_ENTRIES]) if v]
        return kept or None
    if isinstance(value, dict):
        kept = {}
        for key, item in list(value.items())[:_MAX_ENTRIES]:
            leaf = _exports(item, depth + 1)
            if leaf is not None:
                kept[str(key)] = leaf
        return kept or None
    return None


def _tsconfig(document: dict[str, Any]) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    extends = _strings(document.get("extends"))
    if extends:
        facts["extends"] = extends
    include = _strings(document.get("include"))
    if include:
        facts["include"] = include
    options = document.get("compilerOptions")
    if isinstance(options, dict):
        for key in _COMPILER_OPTIONS:
            if isinstance(options.get(key), str):
                facts[key] = options[key]
        paths = options.get("paths")
        if isinstance(paths, dict):
            mapped = {
                str(k): targets
                for k, v in list(paths.items())[:_MAX_ENTRIES]
                if (targets := _strings(v))
            }
            if mapped:
                facts["paths"] = mapped
    return facts


def _package(document: dict[str, Any]) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    if isinstance(document.get("name"), str):
        facts["name"] = document["name"]
    for key in _ENTRY_FIELDS:
        if isinstance(document.get(key), str):
            facts[key] = document[key]
    exports = _exports(document.get("exports"))
    if exports is not None:
        facts["exports"] = exports
    workspaces = document.get("workspaces")
    if isinstance(workspaces, dict):
        workspaces = workspaces.get("packages")
    globs = _strings(workspaces)
    if globs:
        facts["workspaces"] = globs
    return facts


def module_facts(name: str, content: str) -> list[ParsedReference]:
    """The module-resolution claim a file of this name makes, as at most one reference."""
    kind = kind_for(name)
    if kind is None or len(content.encode("utf-8", "replace")) > _MAX_BYTES:
        return []
    try:
        if kind == WORKSPACE:
            document = yaml.safe_load(content)
        else:
            document = json.loads(strip_jsonc(content) if kind == TSCONFIG else content)
    except (json.JSONDecodeError, yaml.YAMLError, RecursionError):
        return []
    if not isinstance(document, dict):
        return []
    if kind == TSCONFIG:
        facts = _tsconfig(document)
    elif kind == PACKAGE:
        facts = _package(document)
    else:
        globs = _strings(document.get("packages"))
        facts = {"packages": globs} if globs else {}
    if not facts:
        return []
    return [
        ParsedReference(
            edge_type=EdgeType.CONFIGURES,
            target_kind=EntityType.CONFIG,
            target_key=f"module-resolution:{kind}",
            line_start=1,
            line_end=max(1, content.count("\n") + (0 if content.endswith("\n") else 1)),
            meta={MODULE_RESOLUTION: kind, "facts": json.dumps(facts, sort_keys=True)},
        )
    ]
