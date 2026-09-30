"""What a ``setup.cfg`` or ``setup.py`` states about a Python project, read statically.

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
* ``requires``: ``true`` when ``[options] install_requires`` is stated in a shape read -- one
  requirement a line, or one alone on the option's line, each where :func:`cfg_values` finds
  it; the requirements themselves are the dependency layer's rows;
* ``unread``: the fields stated in a shape this does not read -- a key spelled another way
  (``Name``, ``package-dir``), a directive (``attr:``, ``file:``), an interpolation, a
  mapping of another package, a ``where`` elsewhere. A field this does not read is never
  guessed at, nor replaced by another source or by the directories on disk.

Where a key is written is found by :func:`cfg_places`, which walks the file as
``configparser`` does -- never by searching the text for the value.

A ``setup.py`` is **read with ``ast``, never run** (V1_SPEC §17), and is setuptools metadata
only when it calls ``setup`` as bound at module level from ``setuptools`` or
``distutils.core`` (``from setuptools import setup``, ``import setuptools`` then
``setuptools.setup``). Its keywords are read only when that is its one ``setup()`` call, an
expression statement of the module itself, with no ``**`` -- ``if cond: setup(...)`` or two
calls may pass either -- and only as literals: ``name`` a string, ``install_requires`` a list
of requirement strings (and no ``requires`` beside it), ``package_dir`` the root
mapping ``{"": "src"}``, ``packages`` a list or ``find_packages(...)`` /
``find_namespace_packages(...)`` with literal ``where``/``include``/``exclude``,
``py_modules`` a list of names. A name bound to a string elsewhere, an f-string, a call, a
computed dictionary: each is **unread**, never evaluated -- there is no constant propagation
here. A ``setup()`` that states no name, no layout and no ``install_requires``, the shim modern
projects keep beside their ``setup.cfg``, records that it is setuptools metadata and nothing
else. Whether ``install_requires`` is read is decided apart from the name and the layout, so
an unread one never makes either unread.
"""

from __future__ import annotations

import ast
import configparser
import json
import re
from pathlib import Path
from typing import Any

from repoatlas.models.parsing import EdgeType, EntityType, ParsedFile, ParsedReference
from repoatlas.parsers.module_facts import MODULE_RESOLUTION, PYTHON_SETUPTOOLS
from repoatlas.parsers.requirements import python_requirement

#: Larger than any hand-written project file; beyond it the file records nothing.
_MAX_BYTES = 256 * 1024
_MAX_ENTRIES = 512
#: The build backends that read setuptools metadata beside a ``pyproject.toml`` (and none
#: named at all: pip then builds with setuptools).
SETUPTOOLS_BACKENDS = frozenset({"setuptools.build_meta", "setuptools.build_meta:__legacy__"})
#: The keys that state where a project's modules live, in ``[options]`` and in ``setup()``.
LAYOUT_KEYS = ("package_dir", "packages", "py_modules")
_FIND_KEYS = ("where", "include", "exclude")
_DIRECTIVES = ("attr:", "file:")
_SECTION = re.compile(r"\[(?P<header>.+)\]")
_OPTION = re.compile(r"(?P<option>.*?)\s*(?P<vi>=|:)\s*(?P<value>.*)$")


def cfg_values(content: str) -> dict[tuple[str, str], tuple[int, list[tuple[int, str]]]]:
    """(section, option as written) -> the line the option is written on, and each line of
    its value that holds text, with that text stripped: the option line's own, then each
    continuation line.

    Walks the file as ``configparser`` reads it: full-line ``#`` and ``;`` comments, blank
    lines, values continued on lines indented deeper than their option, section headers,
    ``=`` or ``:`` between an option and its value. An option written twice keeps its first
    place and value; ``configparser`` refuses such a file anyway.
    """
    values: dict[tuple[str, str], tuple[int, list[tuple[int, str]]]] = {}
    section: str | None = None
    option: str | None = None
    lines: list[tuple[int, str]] | None = None
    indent_level = 0
    for number, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        indent = len(line) - len(line.lstrip())
        if section is not None and option is not None and indent > indent_level:
            if lines is not None:
                lines.append((number, stripped))  # a value continued
            continue
        indent_level = indent
        header = _SECTION.match(stripped)
        if header:
            section, option, lines = header.group("header"), None, None
            continue
        found = _OPTION.match(stripped)
        if found and section is not None:
            key = found.group("option").rstrip()
            option, lines = key, None
            if (section, key) not in values:
                value = found.group("value").strip()
                lines = [(number, value)] if value else []
                values[(section, key)] = (number, lines)
    return values


def cfg_places(content: str) -> dict[tuple[str, str], int]:
    """(section, option as written) -> the line the option is written on (:func:`cfg_values`)."""
    return {key: line for key, (line, _) in cfg_values(content).items()}


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
    requires = _cfg_requires(parser, content)
    if requires is True:
        facts["requires"] = True
    elif requires is False:
        unread.append("options.install_requires")
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


def _requirements(entries: list[str]) -> bool:
    """Whether every entry is one requirement, as the dependency layer reads it."""
    return all("#" not in entry and python_requirement(entry) is not None for entry in entries)


def _cfg_requires(parser: configparser.ConfigParser, content: str) -> bool | None:
    """Whether ``[options] install_requires`` is stated in a shape read (``True``), stated in
    another (``False``: a spelling, a directive, an interpolation, a one-line list setuptools
    would split on ``;``, an entry this does not locate), or not stated (``None``)."""
    if not parser.has_section("options"):
        return None
    keys = list(parser["options"])
    if _variant(keys, "install_requires"):
        return False
    if "install_requires" not in keys:
        return None
    value = parser["options"]["install_requires"]
    stripped = value.strip()
    if "%" in value or stripped.startswith(_DIRECTIVES):
        return False
    if "\n" not in value and any(separator in value for separator in ";,"):  # one line
        return False
    entries = [entry.strip() for entry in value.splitlines() if entry.strip()]
    written = cfg_values(content).get(("options", "install_requires"))
    if written is None or [text for _, text in written[1]] != entries:
        return False
    return _requirements(entries)


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


def _bindings(tree: ast.Module) -> tuple[set[str], set[str], dict[str, str]]:
    """What the module binds at its own level: names of setuptools' ``setup``, names of the
    ``setuptools`` module, and names of its package finders (-> ``find`` or
    ``find_namespace``)."""
    setup: set[str] = set()
    modules: set[str] = set()
    finders: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            for alias in node.names:
                local = alias.asname or alias.name
                if node.module in ("setuptools", "distutils.core") and alias.name == "setup":
                    setup.add(local)
                elif node.module == "setuptools" and alias.name == "find_packages":
                    finders[local] = "find"
                elif node.module == "setuptools" and alias.name == "find_namespace_packages":
                    finders[local] = "find_namespace"
        elif isinstance(node, ast.Import):
            modules.update(
                alias.asname or "setuptools" for alias in node.names if alias.name == "setuptools"
            )
    return setup, modules, finders


def _callee(call: ast.Call) -> tuple[str | None, str | None]:
    """A call's callee as (name, the name it is an attribute of): ``setup(...)`` is
    ``("setup", None)``, ``setuptools.setup(...)`` ``("setup", "setuptools")``."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id, None
    if isinstance(func, ast.Attribute):
        return func.attr, func.value.id if isinstance(func.value, ast.Name) else ""
    return None, None


def _literals(node: ast.AST) -> list[str] | None:
    """A literal list or tuple of strings."""
    if isinstance(node, ast.List | ast.Tuple) and all(
        isinstance(element, ast.Constant) and isinstance(element.value, str)
        for element in node.elts
    ):
        return [element.value for element in node.elts][:_MAX_ENTRIES]  # type: ignore[attr-defined]
    return None


def _stated(call: ast.Call) -> set[str]:
    """What a setup() call may state: its name, its layout; ``**`` may state either."""
    keys = {keyword.arg for keyword in call.keywords}
    if None in keys:
        return {"name", "layout"}
    return ({"name"} if "name" in keys else set()) | (
        {"layout"} if keys & set(LAYOUT_KEYS) else set()
    )


def setup_py_facts(tree: ast.Module) -> dict[str, Any] | None:
    """What a ``setup.py``'s one static setup() call states, or ``None`` when the file calls
    no ``setup`` bound from setuptools or distutils."""
    setup, modules, finders = _bindings(tree)
    bound: list[ast.Call] = []
    named: list[ast.Call] = []  # called ``setup``, bound to something else or to nothing
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name, owner = _callee(node)
            if (owner is None and name in setup) or (owner in modules and name == "setup"):
                bound.append(node)
            elif name == "setup":
                named.append(node)
    if not bound:
        return None
    facts: dict[str, Any] = {"metadata": True}
    top = {id(statement.value) for statement in tree.body if isinstance(statement, ast.Expr)}
    requires = _setup_requires(bound, named, top)
    if requires is True:
        facts["requires"] = True
    unread: list[str] = ["setup.install_requires"] if requires is False else []
    stated = set().union(*(_stated(call) for call in (*bound, *named)))
    if not stated:
        if unread:
            facts["unread"] = unread
        return facts  # the shim: a setup() stating no name and no layout
    [call, *others] = bound
    if (
        others
        or any(_stated(c) for c in named)
        or id(call) not in top
        or None in {keyword.arg for keyword in call.keywords}
    ):
        facts["unread"] = sorted([*unread, *(f"setup.{field}" for field in stated)])
        return facts
    keywords = {keyword.arg: keyword.value for keyword in call.keywords}
    name = keywords.get("name")
    if name is not None:
        if isinstance(name, ast.Constant) and isinstance(name.value, str) and name.value:
            facts["name"] = name.value
        else:
            unread.append("setup.name")
    if set(keywords) & set(LAYOUT_KEYS):
        layout = _setup_layout(keywords, finders, modules)
        if layout is None:
            unread.append("setup.layout")
        else:
            facts["layout"] = layout
    if unread:
        facts["unread"] = sorted(unread)
    return facts


def _setup_requires(bound: list[ast.Call], named: list[ast.Call], top: set[int]) -> bool | None:
    """Whether the one static setup() states ``install_requires`` as a literal list of
    requirements (``True``), whether some call may state it otherwise (``False``: computed, a
    ``**``, a second or conditional call, an unbound ``setup``, a ``requires`` beside it whose
    rows the dependency layer reads alike), or whether none does (``None``)."""
    stating = [
        call
        for call in (*bound, *named)
        if any(keyword.arg in (None, "install_requires") for keyword in call.keywords)
    ]
    if not stating:
        return None
    [call, *others] = bound
    if others or stating != [call] or id(call) not in top:
        return False
    keywords = {keyword.arg: keyword.value for keyword in call.keywords}
    if None in keywords or "requires" in keywords:
        return False
    listed = _literals(keywords["install_requires"])
    return listed is not None and _requirements([entry.strip() for entry in listed])


def _setup_layout(
    keywords: dict[str | None, ast.expr], finders: dict[str, str], modules: set[str]
) -> dict[str, Any] | None:
    """The layout setup()'s keywords state, normalised as ``[options]``'s, or ``None``."""
    root = ""
    if "package_dir" in keywords:
        node = keywords["package_dir"]
        if not (
            isinstance(node, ast.Dict)
            and len(node.keys) == 1
            and isinstance(node.keys[0], ast.Constant)
            and node.keys[0].value == ""
            and isinstance(node.values[0], ast.Constant)
            and isinstance(node.values[0].value, str)
        ):
            return None
        found = project_path(node.values[0].value)
        if found is None:
            return None
        root = found
    layout: dict[str, Any] = {"root": root}
    if "packages" in keywords:
        node = keywords["packages"]
        listed = _literals(node)
        if listed is not None:
            if not dotted(listed):
                return None
            layout["packages"] = listed
        elif isinstance(node, ast.Call):
            found = _finder(node, finders, modules)
            if found is None or found[1] != root:
                return None
            mode, _, include, exclude = found
            layout.update(packages=mode, include=include, exclude=exclude)
        else:
            return None
    if "py_modules" in keywords:
        listed = _literals(keywords["py_modules"])
        if listed is None or not all(module.isidentifier() for module in listed):
            return None
        layout["py_modules"] = listed
    return layout


def _finder(
    call: ast.Call, finders: dict[str, str], modules: set[str]
) -> tuple[str, str, list[str], list[str]] | None:
    """(mode, where, include, exclude) of a literal ``find_packages(...)`` call."""
    name, owner = _callee(call)
    if owner is None and name in finders:
        mode = finders[name]
    elif owner in modules and name in ("find_packages", "find_namespace_packages"):
        mode = "find" if name == "find_packages" else "find_namespace"
    else:
        return None
    order = ("where", "exclude", "include")
    if len(call.args) > len(order) or any(isinstance(a, ast.Starred) for a in call.args):
        return None
    given: dict[str, ast.expr] = dict(zip(order, call.args, strict=False))
    for keyword in call.keywords:
        if keyword.arg not in order or keyword.arg in given:
            return None
        given[keyword.arg] = keyword.value
    where = ""
    if "where" in given:
        node = given["where"]
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            return None
        found = project_path(node.value)
        if found is None:
            return None
        where = found
    patterns: dict[str, list[str]] = {}
    for key in ("include", "exclude"):
        if key in given:
            listed = _literals(given[key])
            if listed is None:
                return None
            patterns[key] = listed
    include = patterns.get("include", [])
    return mode, where, [] if include == ["*"] else include, patterns.get("exclude", [])


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
    version = "2"  # 2: whether [options] install_requires is read
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
