"""What a ``setup.cfg`` states about a Python project, read statically, never run.

A setuptools project states its distribution name and where its modules live in
``setup.cfg``, in ``setup.py`` keywords, or in ``pyproject.toml``. The ``USES`` stage
merges every such source of one directory into one project
(:mod:`repoatlas.graph.py_modules`), and it must take them from what was indexed, never
from the working tree at link time, so each file records what it states here as one
module-resolution fact of the kind :data:`~repoatlas.parsers.module_facts.PYTHON_SETUPTOOLS`.

A ``setup.cfg`` is setuptools metadata only when it has a ``[metadata]`` or ``[options]``
section; one holding only tool settings (``[flake8]``, ``[tool:pytest]``,
``[build_sphinx]``) records nothing. It is read with ``configparser`` as setuptools reads
it -- no interpolation, option names as written -- and only in the smallest shapes the
static layout rules need:

* ``name``: ``[metadata] name`` as written, on one line, never normalised and never read as
  an import name -- a distribution is not a module (``PyJWT`` ships ``jwt``);
* ``layout``: from ``[options]`` -- ``package_dir``, only its root mapping (``= src``);
  ``packages``, as ``find:`` or ``find_namespace:`` with ``[options.packages.find]``
  ``include``/``exclude``, or a list; ``py_modules``, a list of names. ``where`` must be the
  ``package_dir`` root, as setuptools' own documentation requires;
* ``unread``: the fields stated in a shape this does not read -- a key spelled another way
  (``Name``, ``package-dir``), a directive (``attr:``, ``file:``), an interpolation, a
  mapping of another package, a ``where`` elsewhere. A field this does not read is never
  guessed at, nor replaced by another source or by the directories on disk.

Where a key is written is found by :func:`cfg_places`, which walks the file as
``configparser`` does -- never by searching the text for the value.
"""

from __future__ import annotations

import configparser
import json
import re
from pathlib import Path
from typing import Any

from repoatlas.models.parsing import EdgeType, EntityType, ParsedFile, ParsedReference
from repoatlas.parsers.module_facts import MODULE_RESOLUTION, PYTHON_SETUPTOOLS

#: Larger than any hand-written project file; beyond it the file records nothing.
_MAX_BYTES = 256 * 1024
_MAX_ENTRIES = 512
#: The keys that state where a project's modules live, in ``[options]`` and in ``setup()``.
LAYOUT_KEYS = ("package_dir", "packages", "py_modules")
_FIND_KEYS = ("where", "include", "exclude")
_DIRECTIVES = ("attr:", "file:")
_SECTION = re.compile(r"\[(?P<header>.+)\]")
_OPTION = re.compile(r"(?P<option>.*?)\s*(?P<vi>=|:)\s*(?P<value>.*)$")


def cfg_places(content: str) -> dict[tuple[str, str], int]:
    """(section, option as written) -> the line the option is written on.

    Walks the file as ``configparser`` reads it: full-line ``#`` and ``;`` comments, blank
    lines, values continued on lines indented deeper than their option, section headers,
    ``=`` or ``:`` between an option and its value.
    """
    places: dict[tuple[str, str], int] = {}
    section: str | None = None
    option: str | None = None
    indent_level = 0
    for number, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        indent = len(line) - len(line.lstrip())
        if section is not None and option is not None and indent > indent_level:
            continue  # a value continued
        indent_level = indent
        header = _SECTION.match(stripped)
        if header:
            section, option = header.group("header"), None
            continue
        found = _OPTION.match(stripped)
        if found and section is not None:
            key = found.group("option").rstrip()
            places.setdefault((section, key), number)
            option = key
    return places


def cfg_list(value: str) -> list[str]:
    """A list option, as setuptools reads one: one entry a line, else comma-separated."""
    chunks = value.splitlines() if "\n" in value else value.split(",")
    return [chunk.strip() for chunk in chunks if chunk.strip()][:_MAX_ENTRIES]


def project_path(path: str) -> str | None:
    """A path inside the project as written, normalised; ``""`` is the project's own
    directory, and ``None`` a path leaving it or naming another machine's."""
    path = path.strip().rstrip("/")
    if path in ("", "."):
        return ""
    if "\\" in path or path.startswith(("/", "~")) or (len(path) > 1 and path[1] == ":"):
        return None
    parts = [part for part in path.split("/") if part not in ("", ".")]
    if ".." in parts:
        return None
    return "/".join(parts)


def dotted(names: list[str]) -> bool:
    """Whether every name is a dotted module name."""
    return all(name and all(part.isidentifier() for part in name.split(".")) for name in names)


def _variant(keys: list[str], canonical: str) -> bool:
    """Whether a key spelled otherwise normalises to ``canonical`` (its case, or dashes)."""
    return any(key != canonical and key.lower().replace("-", "_") == canonical for key in keys)


def setup_cfg_facts(content: str) -> dict[str, Any] | None:
    """What a ``setup.cfg`` states about its project, or ``None`` when it is no setuptools
    metadata (or no valid configuration: the dependency layer diagnoses that)."""
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str  # type: ignore[assignment,method-assign] # as written
    try:
        parser.read_string(content)
    except (configparser.Error, ValueError):
        return None
    if not parser.has_section("metadata") and not parser.has_section("options"):
        return None
    facts: dict[str, Any] = {"metadata": True}
    unread: list[str] = []
    if parser.has_section("metadata"):
        keys = list(parser["metadata"])
        if _variant(keys, "name"):
            unread.append("metadata.name")
        elif "name" in keys:
            value = parser["metadata"]["name"].strip()
            if (
                value
                and "\n" not in value
                and "%" not in value
                and not value.startswith(_DIRECTIVES)
                and ("metadata", "name") in cfg_places(content)
            ):
                facts["name"] = value
            else:
                unread.append("metadata.name")
    if parser.has_section("options"):
        keys = list(parser["options"])
        stated = {key: parser["options"][key] for key in LAYOUT_KEYS if key in keys}
        if any(_variant(keys, key) for key in LAYOUT_KEYS):
            unread.append("options.layout")
        elif stated:
            layout = _layout(parser, stated)
            if layout is None:
                unread.append("options.layout")
            else:
                facts["layout"] = layout
    if unread:
        facts["unread"] = unread
    return facts


def _layout(parser: configparser.ConfigParser, raw: dict[str, str]) -> dict[str, Any] | None:
    """The layout ``[options]`` states, normalised, or ``None`` in a shape not read."""
    if any("%" in value or value.strip().startswith(_DIRECTIVES) for value in raw.values()):
        return None
    root = ""
    if "package_dir" in raw:
        mapping: dict[str, str] = {}
        for line in cfg_list(raw["package_dir"]):
            key, separator, value = line.partition("=")
            if not separator:
                return None
            mapping[key.strip()] = value.strip()
        found = project_path(mapping.get("", "")) if set(mapping) == {""} else None
        if found is None:
            return None  # a mapping of another package, or a root leaving the project
        root = found
    layout: dict[str, Any] = {"root": root}
    if "packages" in raw:
        value = raw["packages"].strip()
        if value in ("find:", "find_namespace:"):
            find: dict[str, list[str]] = {}
            if parser.has_section("options.packages.find"):
                section = parser["options.packages.find"]
                keys = list(section)
                if any(_variant(keys, key) for key in _FIND_KEYS):
                    return None
                for key in _FIND_KEYS:
                    if key in keys:
                        if "%" in section[key]:
                            return None
                        find[key] = cfg_list(section[key])
            where = project_path(find["where"][0]) if find.get("where") else ""
            if where != root:
                return None
            layout["packages"] = value.removesuffix(":")
            layout["include"] = find.get("include", [])
            layout["exclude"] = find.get("exclude", [])
        else:
            listed = cfg_list(value)
            if not dotted(listed):
                return None
            layout["packages"] = listed
    if "py_modules" in raw:
        modules = cfg_list(raw["py_modules"])
        if not all(module.isidentifier() for module in modules):
            return None
        layout["py_modules"] = modules
    return layout


def setuptools_reference(facts: dict[str, Any], content: str) -> ParsedReference:
    """The module-resolution claim of one setuptools source."""
    return ParsedReference(
        edge_type=EdgeType.CONFIGURES,
        target_kind=EntityType.CONFIG,
        target_key=f"module-resolution:{PYTHON_SETUPTOOLS}",
        line_start=1,
        line_end=max(1, content.count("\n") + (0 if content.endswith("\n") else 1)),
        meta={MODULE_RESOLUTION: PYTHON_SETUPTOOLS, "facts": json.dumps(facts, sort_keys=True)},
    )


class SetuptoolsProjectParser:
    """``setup.cfg``: the module-resolution fact its setuptools metadata states, and nothing
    else.

    Not a language, exactly as :class:`~repoatlas.parsers.python_project.PythonProjectParser`
    is not: the file keeps whatever language discovery gives it and contributes no symbols,
    so its chunks are the ones it had before it was parsed at all. Its dependencies are the
    dependency layer's.
    """

    name = "setuptools-project"
    version = "1"
    language = ""

    def supports(self, path: Path) -> bool:
        return path.name == "setup.cfg"

    def parse(self, path: Path, content: str) -> ParsedFile:
        empty = ParsedFile(language=self.language, parser=self.name, parser_version=self.version)
        if len(content.encode("utf-8", "replace")) > _MAX_BYTES:
            return empty
        facts = setup_cfg_facts(content)
        if facts is None:
            return empty
        return empty.model_copy(update={"references": (setuptools_reference(facts, content),)})
