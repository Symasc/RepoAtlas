"""What a Python project's ``pyproject.toml`` states about where its modules live.

A Python import names a module by a dotted name, and which file that is depends on the
project's *source roots*: ``src/`` in a src layout, the project's own directory in a flat
one, or wherever its build configuration puts them. The ``USES`` stage resolves imports
against those roots (:mod:`repoatlas.graph.py_modules`), and it must take what a project
declares from what was indexed, never from the working tree at link time, so the
declaration is recorded here as one module-resolution fact per file, exactly as
:mod:`repoatlas.parsers.module_facts` records a ``package.json``.

A ``pyproject.toml`` is a Python *project* only when it says so: a ``[project]``,
``[tool.poetry]`` or ``[build-system]`` table. One holding only tool settings (a
``[tool.ruff]`` beside a TypeScript codebase) is configuration, not a project, and
records nothing -- except a uv workspace root with no project of its own, which is
recorded as ``virtual``: it owns no modules, but its members and sources are evidence.
Recorded, and nothing else:

* ``name``: the distribution name as written, never normalised and never read as an
  import name -- a distribution is not a module (``internal-auth`` may ship
  ``internal_auth``, or ``auth``, or both);
* ``backend``: ``[build-system] build-backend``;
* ``dependencies``: with a ``[project]`` table, ``static`` -- its ``dependencies`` are the
  project's, stated or not, and setuptools reads no other source for them -- or, when
  ``dynamic`` names them, ``unread`` if ``[tool.setuptools.dynamic]`` reads them from a file,
  else ``dynamic``: a ``setup.py`` or ``setup.cfg`` may state them;
* ``packages``: hatchling's ``[tool.hatch.build.targets.wheel] packages``, and
  ``flit_module``: flit's ``[tool.flit.module] name``, the module the project ships;
* ``setuptools_layout``: setuptools' ``[tool.setuptools.packages.find]``, in the one
  shape read -- a single ``where`` inside the project, literal ``include`` and ``exclude``
  lists and a boolean ``namespaces`` (true unless stated), with at most a
  ``package-dir`` whose only mapping is the root ``where`` names -- under a setuptools
  backend or none. It is recorded normalised exactly as a ``setup.cfg``'s ``find:``
  layout (:mod:`repoatlas.parsers.setuptools_project`), so both are read alike;
* ``only_include``: a hatchling wheel's ``only-include`` -- the wheel target's, else the
  build's, which every target not stating its own inherits -- when each entry is one
  top-level directory name, neither table states ``packages``, ``sources`` or
  ``exclude``, and every ``force-include`` puts a file that is no module into the wheel.
  The names are the only top-level packages the wheel ships;
* ``unread``: every *other* layout key present -- setuptools ``package-dir``,
  ``packages`` and ``py-modules`` in any other shape, poetry ``packages``, pdm
  ``package-dir``, uv's build backend ``module-root`` and ``module-name``, hatch
  ``sources`` and ``only-include`` in any other shape, and either of the first two
  explicit layouts in a shape it does not take. One of the last two layouts is read only
  when no other layout is stated at all: a project stating a layout this does not read is
  refused rather than guessed at from its directories;
* ``uv_sources`` and ``uv_workspace``: where the project says a dependency comes from,
  keyed by the dependency's name as written (a parser does not normalise distribution
  names; resolution does), and the workspace it roots;
* ``poetry_dependencies``: every name Poetry's own tables declare, as written: a
  declaration in a form resolution does not read, so named rather than taken for one
  it does.

Values are kept only in the shapes resolution reads, bounded like every module fact.
The file is ``tomllib``'s to read, so the parser's version carries the interpreter
(:mod:`repoatlas.parsers.interpreter`).
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any

from repoatlas.models.parsing import EdgeType, EntityType, ParsedFile, ParsedReference
from repoatlas.parsers.interpreter import interpreter_tag
from repoatlas.parsers.module_facts import MODULE_RESOLUTION, PYTHON_PROJECT
from repoatlas.parsers.setuptools_project import SETUPTOOLS_BACKENDS, project_path

#: Larger than any hand-written project file; beyond it the file records nothing.
_MAX_BYTES = 256 * 1024
_MAX_ENTRIES = 512
#: The layout keys each of the two layouts read only when no other is stated covers.
_SETUPTOOLS_FIND = frozenset({"tool.setuptools.package-dir", "tool.setuptools.packages"})
_ONLY_INCLUDE = frozenset(
    {"tool.hatch.build.only-include", "tool.hatch.build.targets.wheel.only-include"}
)
_FIND_KEYS = frozenset({"where", "include", "exclude", "namespaces"})
#: What a ``force-include`` may put in a wheel without shipping a module.
_MODULE_SUFFIXES = (".py", ".pyi", ".so", ".pyd")

#: Layout keys this version does not read, by the table path they sit under.
_UNREAD_LAYOUT: tuple[tuple[tuple[str, ...], str], ...] = (
    (("tool", "setuptools"), "package-dir"),
    (("tool", "setuptools"), "packages"),
    (("tool", "setuptools"), "py-modules"),
    (("tool", "poetry"), "packages"),
    (("tool", "pdm", "build"), "package-dir"),
    (("tool", "pdm", "build"), "includes"),
    (("tool", "uv", "build-backend"), "module-root"),
    (("tool", "uv", "build-backend"), "module-name"),
    (("tool", "hatch", "build"), "packages"),
    (("tool", "hatch", "build"), "sources"),
    (("tool", "hatch", "build"), "only-include"),
    (("tool", "hatch", "build", "targets", "wheel"), "sources"),
    (("tool", "hatch", "build", "targets", "wheel"), "only-include"),
)


def _table(document: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any] | None:
    node: Any = document
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, dict) else None


def _strings(value: Any) -> list[str] | None:
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value[:_MAX_ENTRIES])
    return None


def _source(value: Any) -> dict[str, Any]:
    """What one ``[tool.uv.sources]`` entry says: a path, a workspace, or something else."""
    if not isinstance(value, dict):
        # A list is several sources chosen by marker: no one place is named.
        return {"other": "multiple" if isinstance(value, list) else "unreadable"}
    if "marker" in value:
        return {"other": "marker"}  # a source for some environments only
    if isinstance(value.get("path"), str):
        return {"path": value["path"], "editable": value.get("editable") is True}
    if value.get("workspace") is True:
        return {"workspace": True}
    if "workspace" in value:
        return {"other": "workspace-path"}  # another workspace, by its path
    kind = next((k for k in ("git", "url", "index") if k in value), "unreadable")
    return {"other": kind}


def _poetry_dependencies(poetry: dict[str, Any]) -> list[str]:
    tables = [poetry.get("dependencies"), poetry.get("dev-dependencies")]
    groups = poetry.get("group")
    if isinstance(groups, dict):
        tables.extend(g.get("dependencies") for g in groups.values() if isinstance(g, dict))
    found: set[str] = set()
    for table in tables:
        if isinstance(table, dict):
            found.update(str(name) for name in list(table)[:_MAX_ENTRIES] if name != "python")
    return sorted(found)


def _setuptools_find(document: dict[str, Any], backend: str | None) -> dict[str, Any] | None:
    """``[tool.setuptools.packages.find]`` normalised as a ``setup.cfg``'s ``find:`` layout,
    or ``None`` in any shape not read (see the module docstring)."""
    if backend is not None and backend not in SETUPTOOLS_BACKENDS:
        return None  # another backend builds it: these keys are not its layout
    table = _table(document, ("tool", "setuptools")) or {}
    packages = table.get("packages")
    if not isinstance(packages, dict) or set(packages) != {"find"}:
        return None
    find = packages["find"]
    if not isinstance(find, dict) or not set(find) <= _FIND_KEYS:
        return None
    where = find.get("where", ["."])
    if not (isinstance(where, list) and len(where) == 1 and isinstance(where[0], str)):
        return None
    root = project_path(where[0])
    if root is None:
        return None
    if "package-dir" in table:
        mapping = table["package-dir"]
        if not (
            isinstance(mapping, dict) and set(mapping) == {""} and isinstance(mapping[""], str)
        ):
            return None  # a mapping of another package
        if project_path(mapping[""]) != root:
            return None  # setuptools' own documentation requires `where` to be the root
    include, exclude = _strings(find.get("include", ["*"])), _strings(find.get("exclude", []))
    namespaces = find.get("namespaces", True)
    if include is None or exclude is None or not isinstance(namespaces, bool):
        return None
    return {
        "root": root,
        "packages": "find_namespace" if namespaces else "find",
        "include": [] if include == ["*"] else include,
        "exclude": exclude,
    }


def _only_include(document: dict[str, Any], backend: str | None) -> list[str] | None:
    """The top-level packages a hatchling wheel's ``only-include`` names, or ``None`` in any
    shape not read (see the module docstring)."""
    if backend != "hatchling.build":
        return None
    build = _table(document, ("tool", "hatch", "build")) or {}
    wheel = _table(document, ("tool", "hatch", "build", "targets", "wheel")) or {}
    if any(key in table for table in (build, wheel) for key in ("packages", "sources", "exclude")):
        return None
    for table in (build, wheel):
        forced = table.get("force-include", {})
        if not isinstance(forced, dict):
            return None
        for target in forced.values():
            suffix = PurePosixPath(target).suffix if isinstance(target, str) else ""
            if not suffix or suffix in _MODULE_SUFFIXES:
                return None  # it may put a module, or a directory of them, in the wheel
    entries = _strings(wheel["only-include"] if "only-include" in wheel else build["only-include"])
    if not entries:
        return None
    names = [project_path(entry) for entry in entries]
    if not all(name and "/" not in name and name.isidentifier() for name in names):
        return None  # a nested path ships at that path, and a file is no package
    return [name for name in names if name]


def _explicit_layout(document: dict[str, Any], facts: dict[str, Any]) -> None:
    """Read setuptools' ``find`` or hatchling's ``only-include`` in place of the keys stating
    it, when those are the only layout keys the file states at all."""
    unread = set(facts.get("unread", ()))
    if not unread or "packages" in facts or "flit_module" in facts:
        return
    if unread <= _SETUPTOOLS_FIND:
        layout = _setuptools_find(document, facts.get("backend"))
        if layout is None:
            return
        facts["setuptools_layout"] = layout
    elif unread <= _ONLY_INCLUDE:
        names = _only_include(document, facts.get("backend"))
        if names is None:
            return
        facts["only_include"] = names
    else:
        return
    del facts["unread"]


def project_facts(document: dict[str, Any]) -> dict[str, Any] | None:
    """The layout facts a parsed ``pyproject.toml`` states, or ``None`` if it is no project."""
    project = document.get("project")
    poetry = _table(document, ("tool", "poetry"))
    build = document.get("build-system")
    workspace = _table(document, ("tool", "uv", "workspace"))
    facts: dict[str, Any] = {}
    if not isinstance(project, dict) and poetry is None and not isinstance(build, dict):
        if workspace is None:
            return None
        facts["virtual"] = True
    name = project.get("name") if isinstance(project, dict) else None
    if not isinstance(name, str) and poetry is not None:
        name = poetry.get("name")
    if isinstance(name, str):
        facts["name"] = name
    if isinstance(build, dict) and isinstance(build.get("build-backend"), str):
        facts["backend"] = build["build-backend"]
    if isinstance(project, dict):
        facts["dependencies"] = "static"
        if "dependencies" in (_strings(project.get("dynamic")) or []):
            # setuptools reads them from [tool.setuptools.dynamic] (a file), else setup()
            files = _table(document, ("tool", "setuptools", "dynamic")) or {}
            facts["dependencies"] = "unread" if "dependencies" in files else "dynamic"
    wheel = _table(document, ("tool", "hatch", "build", "targets", "wheel"))
    if wheel is not None and "packages" in wheel:
        packages = _strings(wheel["packages"])
        if packages is None:
            facts.setdefault("unread", []).append("tool.hatch.build.targets.wheel.packages")
        else:
            facts["packages"] = packages
    flit = _table(document, ("tool", "flit", "module"))
    if flit is not None and "name" in flit:
        if isinstance(flit["name"], str):
            facts["flit_module"] = flit["name"]
        else:
            facts.setdefault("unread", []).append("tool.flit.module.name")
    for path, key in _UNREAD_LAYOUT:
        table = _table(document, path)
        if table is not None and key in table:
            facts.setdefault("unread", []).append(".".join((*path, key)))
    _explicit_layout(document, facts)
    sources = _table(document, ("tool", "uv", "sources"))
    if sources:
        facts["uv_sources"] = {
            str(name): _source(value) for name, value in list(sources.items())[:_MAX_ENTRIES]
        }
    if workspace is not None:
        facts["uv_workspace"] = {
            "members": _strings(workspace.get("members")) or [],
            "exclude": _strings(workspace.get("exclude")) or [],
        }
    if poetry is not None:
        declared = _poetry_dependencies(poetry)
        if declared:
            facts["poetry_dependencies"] = declared
    return facts


class PythonProjectParser:
    """``pyproject.toml``: the module-resolution fact a Python project states, and nothing else.

    Not a language: the file keeps whatever language discovery gives it, so no
    repository reports a new one, and it contributes no symbols, so its chunks are the
    ones it had before it was parsed at all. Its dependencies are the dependency
    layer's.
    """

    name = "python-project"
    # 2: flit's module, a virtual workspace root, Poetry's declared names. 3: whether
    # [project] states its dependencies. 4: setuptools' `find` and hatchling's `only-include`
    # layouts. +pyX.Y: read with the running interpreter's `tomllib`.
    version = f"4+{interpreter_tag()}"
    language = ""

    def supports(self, path: Path) -> bool:
        return path.name == "pyproject.toml"

    def parse(self, path: Path, content: str) -> ParsedFile:
        empty = ParsedFile(language=self.language, parser=self.name, parser_version=self.version)
        if len(content.encode("utf-8", "replace")) > _MAX_BYTES:
            return empty
        try:
            document = tomllib.loads(content)
        except (tomllib.TOMLDecodeError, RecursionError):
            # Invalid TOML is already a diagnostic of the dependency layer's.
            return empty
        facts = project_facts(document)
        if facts is None:
            return empty
        return empty.model_copy(
            update={
                "references": (
                    ParsedReference(
                        edge_type=EdgeType.CONFIGURES,
                        target_kind=EntityType.CONFIG,
                        target_key=f"module-resolution:{PYTHON_PROJECT}",
                        line_start=1,
                        line_end=max(1, content.count("\n") + (0 if content.endswith("\n") else 1)),
                        meta={
                            MODULE_RESOLUTION: PYTHON_PROJECT,
                            "facts": json.dumps(facts, sort_keys=True),
                        },
                    ),
                )
            }
        )
