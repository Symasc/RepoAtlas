"""Following a Python use of an imported name to the declaration it names (``USES`` only).

A use (:mod:`repoatlas.parsers.py_uses`) carries the module its binding imports, the name
it imports from there, and the attribute chain it reaches through. The module is resolved
inside the importer's own project (:mod:`repoatlas.graph.py_modules`); the name is then
followed through each module's export table, under Python's own rules:

* **from-import**: ``from M import N`` is attribute ``N`` of ``M`` if ``M`` binds one,
  and otherwise, when ``M`` is a package, its submodule ``N`` -- which the statement
  itself imports. Inside a package's ``__init__``, ``from . import N`` is always the
  submodule: the package is not yet bound when its own body runs;
* **re-exports**: a name a module binds by importing it is followed to where that import
  leads, under every name it passes by, however many ``__init__`` files deep;
* **attribute chains**: ``pkg.a.b`` follows ``a`` in ``pkg`` as an attribute, or as the
  submodule ``pkg.a`` only when this file's own ``import pkg.a`` loaded it; a chain that
  reaches a declaration stops there, and the use is of that declaration;
* a name only a star import could bind, a module ``__getattr__``, and a name the module
  binds by anything but ``def``, ``class`` or an import resolve to nothing, each with its
  reason.

Every trace is bounded and remembered exactly as a TypeScript one
(:class:`~repoatlas.graph.uses.BoundedTrace`), so what a use resolves to is a function of
the indexed facts and never of the order uses are read in. No use resolves outside its
own project here: another project -- in the same repository or not -- is reached only
on dependency evidence, which this version does not read.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from typing import Any

from repoatlas.graph.modules import ModuleTarget
from repoatlas.graph.py_modules import PythonModules
from repoatlas.graph.resolution import ResolutionIndex
from repoatlas.graph.uses import BoundedTrace, UseDefinition, _through, declared_entity
from repoatlas.storage.protocols import FileRow

#: The prefix every Python reason is counted under, beside the TypeScript ones.
PYTHON = "python"


class PythonUseResolver(BoundedTrace):
    """Resolves the Python ``USES`` claims of one global pass."""

    def __init__(
        self,
        modules: PythonModules,
        index: ResolutionIndex,
        tables: Callable[[int], dict[str, Any] | None],
    ) -> None:
        super().__init__(tables)
        self._modules = modules
        self._index = index
        #: Uses resolved, by how their module was found; measurement only.
        self.resolved: Counter[str] = Counter()

    def resolve(self, origin: FileRow, meta: dict[str, str]) -> tuple[int, dict[str, Any]] | None:
        """The used entity's id and how it was reached, or ``None`` with the reason counted."""
        spec = meta.get("module") or ""
        target = self._modules.resolve(origin, spec)
        if isinstance(target, str):
            return self._refused(target)
        self._begin(origin)
        binding = meta.get("binding")
        imported = meta.get("imported")
        if binding == "import" and imported:
            found, reason = self._from_import(target.file, imported, frozenset(), 0)
        elif binding == "namespace":
            found, reason = UseDefinition(target.file, None, None), None
        else:
            return self._refused("no-name")
        if found is None:
            return self._refused(reason or "not-exported")
        member = meta.get("member")
        stopped = False
        if member:
            dotted = spec if binding == "namespace" and not spec.startswith(".") else None
            loaded = set(filter(None, (meta.get("loaded") or "").split(",")))
            parts = member.split(".")
            for index, part in enumerate(parts):
                if found.symbol_id is not None:
                    stopped = index < len(parts)
                    break
                step = f"{dotted}.{part}" if dotted is not None else None
                found, reason, dotted = self._attribute(found, part, step, loaded)
                if found is None:
                    return self._refused(reason or "not-exported")
        return self._resolved(target, meta, found, stopped)

    def _refused(self, why: str) -> None:
        self.unresolved[f"{PYTHON}:{why}"] += 1
        return None

    def _resolved(
        self, target: ModuleTarget, meta: dict[str, str], found: UseDefinition, stopped: bool
    ) -> tuple[int, dict[str, Any]]:
        self.resolved[target.basis] += 1
        fields: dict[str, Any] = {
            "basis": target.basis,
            "specifier": meta.get("module") or "",
            **dict(target.detail),
            "local": meta.get("local"),
            "imported": meta.get("imported"),
            "member": meta.get("member"),
            "defined_at": found.file.rel_path,
            "name": found.name,
            "via": list(found.via),
            "via_names": list(found.via_names),
        }
        if stopped and meta.get("use") != "type":
            # The chain reached a declaration before its end: the use is of that
            # declaration, as the receiver of the rest.
            fields.update({"use": "value", "form": "receiver"})
        entity = found.symbol_id
        if entity is None:
            fields["namespace"] = True
            entity = found.file.file_entity_id
        return entity, fields

    # -- attributes ----------------------------------------------------------------

    def _from_import(
        self, module: FileRow, name: str, visiting: frozenset[tuple[int, str]], depth: int
    ) -> tuple[UseDefinition | None, str | None]:
        """``from module import name``: its attribute, else its submodule."""
        found, reason, _ = self._trace(module, name, visiting, depth)
        if found is not None or reason != "not-exported":
            return found, reason
        sub = self._modules.submodule(module, name)
        if sub is None:
            return None, reason
        return UseDefinition(sub, None, None), None

    def _attribute(
        self, current: UseDefinition, part: str, dotted: str | None, loaded: set[str]
    ) -> tuple[UseDefinition | None, str | None, str | None]:
        """One step of an attribute chain from a module; the dotted name reached, if known."""
        module = current.file
        found, reason, _ = self._trace(module, part, frozenset(), 0)
        if found is not None:
            # Reached through the module's own binding: where it leads is its import's
            # business, so no later step may assume a submodule of it was loaded.
            return (
                UseDefinition(
                    found.file,
                    found.symbol_id,
                    found.name,
                    (*current.via, *found.via),
                    (*current.via_names, *found.via_names),
                ),
                None,
                None,
            )
        if reason != "not-exported":
            return None, reason, None
        if dotted is None or dotted not in loaded:
            return None, "attribute-unproven", None
        sub = self._modules.submodule(module, part)
        if sub is None:
            return None, "not-exported", None
        return UseDefinition(sub, None, None, current.via, current.via_names), None, dotted

    # -- the export-table walk -------------------------------------------------------

    def _trace_once(
        self, module: FileRow, name: str, visiting: frozenset[tuple[int, str]], depth: int
    ) -> tuple[UseDefinition | None, str | None, bool]:
        table = self._tables(module.file_id)
        if table is None:
            return None, "no-export-table", False
        spec = table.get("names", {}).get(name)
        if spec is not None:
            return self._follow(module, name, spec, visiting, depth)
        if table.get("star"):
            return None, "star-deferred", False
        if table.get("getattr"):
            return None, "dynamic-getattr", False
        return None, "not-exported", False

    def _follow(
        self,
        module: FileRow,
        exported: str,
        spec: list[str],
        visiting: frozenset[tuple[int, str]],
        depth: int,
    ) -> tuple[UseDefinition | None, str | None, bool]:
        kind = spec[0] if spec else ""
        if kind == "local" and len(spec) == 3 and spec[2].isdigit():
            symbol = declared_entity(
                self._index.symbols_by_file.get(module.file_id), spec[1], int(spec[2])
            )
            if symbol is None:
                return None, "no-symbol", False
            return UseDefinition(module, symbol, spec[1]), None, False
        if kind in {"import", "namespace"} and len(spec) >= 2:
            target = self._modules.resolve(module, spec[1])
            if isinstance(target, str):
                return None, target, False
            if kind == "namespace":
                return (
                    UseDefinition(target.file, None, None, (module.rel_path,), (exported,)),
                    None,
                    False,
                )
            if len(spec) != 3:
                return None, "unsupported", False
            if target.file.file_id == module.file_id:
                # `from . import sub` inside the package's own `__init__`.
                sub = self._modules.submodule(module, spec[2])
                if sub is None:
                    return None, "not-exported", False
                return UseDefinition(sub, None, None, (module.rel_path,), (exported,)), None, False
            found, reason, cut = self._trace(target.file, spec[2], visiting, depth + 1)
            if found is None and reason == "not-exported" and not cut:
                sub = self._modules.submodule(target.file, spec[2])
                if sub is not None:
                    found, reason = UseDefinition(sub, None, None), None
            return (None if found is None else _through(module, exported, found)), reason, cut
        if kind == "none":
            return None, (spec[1] if len(spec) > 1 and spec[1] else "none"), False
        return None, kind or "unsupported", False
