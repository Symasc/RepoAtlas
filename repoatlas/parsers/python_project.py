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
records nothing. Recorded, and nothing else:

* ``name``: the distribution name as written, never normalised and never read as an
  import name -- a distribution is not a module (``internal-auth`` may ship
  ``internal_auth``, or ``auth``, or both);
* ``backend``: ``[build-system] build-backend``;
* ``packages``: hatchling's ``[tool.hatch.build.targets.wheel] packages``, the one
  explicit layout this version reads;
* ``unread``: every *other* layout key present -- setuptools ``package-dir`` and
  ``packages``, poetry ``packages``, flit ``module``, pdm ``package-dir``, uv's build
  backend ``module-root`` and ``module-name``, hatch ``sources`` and ``only-include``. A
  project stating a layout this does not read is refused rather than guessed at from
  its directories;
* ``uv_sources``, ``uv_workspace`` and ``poetry_paths``: where the project says a
  dependency comes from, keyed by the dependency's name as written (a parser does not
  normalise distribution names; resolution does). Recorded for resolution across
  projects; nothing within one project reads them.

Values are kept only in the shapes resolution reads, bounded like every module fact.
The file is ``tomllib``'s to read, so the parser's version carries the interpreter
(:mod:`repoatlas.parsers.interpreter`).
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

from repoatlas.models.parsing import EdgeType, EntityType, ParsedFile, ParsedReference
from repoatlas.parsers.interpreter import interpreter_tag
from repoatlas.parsers.module_facts import MODULE_RESOLUTION, PYTHON_PROJECT

#: Larger than any hand-written project file; beyond it the file records nothing.
_MAX_BYTES = 256 * 1024
_MAX_ENTRIES = 512

#: Layout keys this version does not read, by the table path they sit under.
_UNREAD_LAYOUT: tuple[tuple[tuple[str, ...], str], ...] = (
    (("tool", "setuptools"), "package-dir"),
    (("tool", "setuptools"), "packages"),
    (("tool", "setuptools"), "py-modules"),
    (("tool", "poetry"), "packages"),
    (("tool", "flit", "module"), "name"),
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
    if isinstance(value.get("path"), str):
        return {"path": value["path"], "editable": value.get("editable") is True}
    if value.get("workspace") is True:
        return {"workspace": True}
    kind = next((k for k in ("git", "url", "index") if k in value), "unreadable")
    return {"other": kind}


def _poetry_paths(poetry: dict[str, Any]) -> dict[str, str]:
    tables = [poetry.get("dependencies"), poetry.get("dev-dependencies")]
    groups = poetry.get("group")
    if isinstance(groups, dict):
        tables.extend(g.get("dependencies") for g in groups.values() if isinstance(g, dict))
    found: dict[str, str] = {}
    for table in tables:
        if not isinstance(table, dict):
            continue
        for name, spec in list(table.items())[:_MAX_ENTRIES]:
            if isinstance(spec, dict) and isinstance(spec.get("path"), str):
                found[str(name)] = spec["path"]
    return found


def project_facts(document: dict[str, Any]) -> dict[str, Any] | None:
    """The layout facts a parsed ``pyproject.toml`` states, or ``None`` if it is no project."""
    project = document.get("project")
    poetry = _table(document, ("tool", "poetry"))
    build = document.get("build-system")
    if not isinstance(project, dict) and poetry is None and not isinstance(build, dict):
        return None
    facts: dict[str, Any] = {}
    name = project.get("name") if isinstance(project, dict) else None
    if not isinstance(name, str) and poetry is not None:
        name = poetry.get("name")
    if isinstance(name, str):
        facts["name"] = name
    if isinstance(build, dict) and isinstance(build.get("build-backend"), str):
        facts["backend"] = build["build-backend"]
    wheel = _table(document, ("tool", "hatch", "build", "targets", "wheel"))
    if wheel is not None and "packages" in wheel:
        packages = _strings(wheel["packages"])
        if packages is None:
            facts.setdefault("unread", []).append("tool.hatch.build.targets.wheel.packages")
        else:
            facts["packages"] = packages
    for path, key in _UNREAD_LAYOUT:
        table = _table(document, path)
        if table is not None and key in table:
            facts.setdefault("unread", []).append(".".join((*path, key)))
    sources = _table(document, ("tool", "uv", "sources"))
    if sources:
        facts["uv_sources"] = {
            str(name): _source(value) for name, value in list(sources.items())[:_MAX_ENTRIES]
        }
    workspace = _table(document, ("tool", "uv", "workspace"))
    if workspace is not None:
        facts["uv_workspace"] = {
            "members": _strings(workspace.get("members")) or [],
            "exclude": _strings(workspace.get("exclude")) or [],
        }
    if poetry is not None:
        paths = _poetry_paths(poetry)
        if paths:
            facts["poetry_paths"] = paths
    return facts


class PythonProjectParser:
    """``pyproject.toml``: the module-resolution fact a Python project states, and nothing else.

    Not a language: the file keeps whatever language discovery gives it, so no
    repository reports a new one, and it contributes no symbols, so its chunks are the
    ones it had before it was parsed at all. Its dependencies are the dependency
    layer's.
    """

    name = "python-project"
    # +pyX.Y: read with the running interpreter's `tomllib`.
    version = f"1+{interpreter_tag()}"
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
