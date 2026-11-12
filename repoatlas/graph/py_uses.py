"""Following a Python use of an imported name to the declaration it names.

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
  reason;
* **wildcard**: a name the importer's own ``from x import *`` is the only thing to bind (a
  star claim, :meth:`PythonUseResolver._star`) resolves to the declaration its stars prove,
  or to nothing, with the reason. Every star of the importer is examined, and each
  module's export table read whatever it answers. A module excludes the name (``__all__``
  omits it; or it starts with ``_`` and there is no ``__all__``), provides it (bound there
  once, unconditionally, and followed as an explicit import is), or leaves it unknown (a
  dynamic ``__all__``, a module ``__getattr__`` or a star of its own that could bind it, a
  conditional or repeated binding, a module that does not resolve). One unknown refuses;
  so do no provider, two different providers, a conditional star, and a star that is not
  strictly above the first read of the name. Stars leading to one declaration are one edge.

Every trace is bounded and remembered exactly as a TypeScript one
(:class:`~repoatlas.graph.uses.BoundedTrace`), so what a use resolves to is a function of
the indexed facts and never of the order uses are read in. A module the importer's own
project does not hold may be another project's -- in the same repository or not -- and
is then reached only on the importer's dependency declaration
(:mod:`repoatlas.graph.py_packages`); the trace continues inside the provider under the
provider's own rules, and never crosses into a third project from there.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from repoatlas.graph.modules import ModuleTarget
from repoatlas.graph.packages import CROSSING
from repoatlas.graph.py_modules import PythonModules
from repoatlas.graph.py_packages import PythonLink, PythonPackageLinks
from repoatlas.graph.resolution import ResolutionIndex
from repoatlas.graph.uses import BoundedTrace, UseDefinition, _through, declared_entity
from repoatlas.storage.protocols import FileRow

#: The prefix every Python reason is counted under, beside the TypeScript ones.
PYTHON = "python"
#: The refusals within the importer's own project that another project may answer.
_LINKABLE = frozenset({"module-not-found", "project-boundary"})
#: What one star import says about a name.
_PROVIDES, _EXCLUDES, _UNKNOWN = "provides", "excludes", "unknown"
#: The reasons a trace gives for a binding that names no declaration, which for a star's
#: name are all one: that nothing proves the module exports a declaration under it.
_NOT_PROVEN = frozenset({"duplicate", "not-a-declaration", "star-import", "no-symbol", "none"})


@dataclass(frozen=True, slots=True)
class _Star:
    """What one star import of the importer says about the name read."""

    line: int
    conditional: bool
    #: ``provides``, ``excludes`` or ``unknown``.
    verdict: str
    #: ``unknown``: why.
    reason: str | None = None
    spec: str = ""
    #: ``provides``: the declaration, the module and package link it was found through, and
    #: whether the module said it by ``__all__`` or by being public.
    found: UseDefinition | None = None
    target: ModuleTarget | None = None
    link: PythonLink | None = None
    export: str = "public"


class PythonUseResolver(BoundedTrace):
    """Resolves Python uses of imported names in one global pass."""

    def __init__(
        self,
        modules: PythonModules,
        index: ResolutionIndex,
        tables: Callable[[int], dict[str, Any] | None],
        links: PythonPackageLinks | None = None,
    ) -> None:
        super().__init__(tables)
        self._modules = modules
        self._index = index
        self._links = links
        #: Uses resolved, by how their module was found; measurement only.
        self.resolved: Counter[str] = Counter()

    def resolve(self, origin: FileRow, meta: dict[str, str]) -> tuple[int, dict[str, Any]] | None:
        """The used entity's id and how it was reached, or ``None`` with the reason counted."""
        if meta.get("binding") == "star":
            return self._star(origin, meta)
        spec = meta.get("module") or ""
        found = self.module_target(origin, spec)
        if isinstance(found, str):
            return self._refused(found)
        link = found if isinstance(found, PythonLink) else None
        target = found.target if isinstance(found, PythonLink) else found
        self._begin(origin)
        binding = meta.get("binding")
        imported = meta.get("imported")
        if binding == "import" and imported:
            definition, reason = self._from_import(target.file, imported, frozenset(), 0)
        elif binding == "namespace":
            definition, reason = UseDefinition(target.file, None, None), None
        else:
            return self._refused("no-name")
        if definition is None:
            return self._refused(reason or "not-exported")
        return self._reached(target, meta, definition, link)

    def module_target(self, origin: FileRow, spec: str) -> ModuleTarget | PythonLink | str:
        """Resolve a module in the importing project or through its proven dependency."""
        target = self._modules.resolve(origin, spec)
        if isinstance(target, str) and target in _LINKABLE and self._links is not None:
            found = self._links.resolve(origin, spec)
            if isinstance(found, PythonLink):
                return found
            if found is not None:
                return found
        return target

    def resolve_module_name(self, module: FileRow, name: str) -> UseDefinition | None:
        """Resolve one name from this module's own export table."""
        self._begin(module)
        found, reason, _ = self._trace(module, name, frozenset(), 0)
        if found is None:
            self._refused(reason or "not-exported")
        return found

    def fromlist_submodule(self, origin: FileRow, spec: str, name: str) -> FileRow | None:
        """The submodule ``from <spec> import <name>`` imports, if the package binds no ``name``.

        Python looks for the attribute first and falls back to the submodule only when the
        package has none: the package's export table must say ``not-exported``, uncut by a
        cycle or a bound. A name it binds, a star or ``__getattr__`` that might, and a ``spec``
        that is no package of the importer's own project all answer nothing."""
        target = self._modules.resolve(origin, spec)
        if isinstance(target, str):
            return None
        return self._unbound_submodule(origin, target.file, name)

    def provider_submodule(self, origin: FileRow, spec: str, name: str) -> PythonLink | None:
        """A fromlist submodule of a proven provider, with the submodule's own evidence."""
        target = self.module_target(origin, spec)
        if not isinstance(target, PythonLink):
            return None
        submodule = self._unbound_submodule(origin, target.target.file, name)
        if submodule is None:
            return None
        child_spec = f"{spec}{'' if spec.endswith('.') else '.'}{name}"
        child = self.module_target(origin, child_spec)
        if not isinstance(child, PythonLink) or child.target.file.file_id != submodule.file_id:
            return None
        return child

    def _unbound_submodule(self, origin: FileRow, module: FileRow, name: str) -> FileRow | None:
        self._begin(origin)
        found, reason, cut = self._trace(module, name, frozenset(), 0)
        if found is not None or reason != "not-exported" or cut:
            return None
        return self._modules.submodule(module, name)

    def has_import_binding(
        self, module: FileRow, local: str, specifier: str, imported: str
    ) -> bool:
        """Whether the module binds ``local`` to this one import, and nothing else."""
        table = self._tables(module.file_id)
        if table is None:
            return False
        binding = table.get("names", {}).get(local)
        return binding == ["import", specifier, imported] and local not in table.get(
            "conditional", ()
        )

    def _reached(
        self,
        target: ModuleTarget,
        meta: dict[str, str],
        found: UseDefinition,
        link: PythonLink | None,
        wildcard: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]] | None:
        """The name's attribute chain followed from where the module answered with it."""
        spec = meta.get("module") or ""
        binding = meta.get("binding")
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
                next_found, reason, dotted = self._attribute(found, part, step, loaded)
                if next_found is None:
                    return self._refused(reason or "not-exported")
                found = next_found
        return self._resolved(target, meta, found, stopped, link, wildcard)

    def _refused(self, why: str) -> None:
        self.unresolved[f"{PYTHON}:{why}"] += 1
        return None

    def _resolved(
        self,
        target: ModuleTarget,
        meta: dict[str, str],
        found: UseDefinition,
        stopped: bool,
        link: PythonLink | None,
        wildcard: dict[str, Any] | None = None,
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
        if link is not None and link.crossing is not None:
            fields[CROSSING] = dict(link.crossing)
            if wildcard is not None:
                # The statement that imports it is the star, which no claim of its own names.
                fields[CROSSING]["imported_at"] = wildcard["line"]
        if wildcard is not None:
            fields["wildcard"] = dict(wildcard)
        entity = found.symbol_id
        if entity is None:
            fields["namespace"] = True
            entity = found.file.file_entity_id
        return entity, fields

    # -- star imports --------------------------------------------------------------

    def _star(self, origin: FileRow, meta: dict[str, str]) -> tuple[int, dict[str, Any]] | None:
        """A name only the importer's star imports could bind: the declaration they prove.

        Every star is examined, none skipped once an answer looks settled, and each module's
        export table is read (and recorded as read) whatever it answers: a later change to a
        module that did not provide the name may make it ambiguous. The precedence is fixed.
        One star that cannot be decided refuses, with the first such reason in source order;
        then no provider, or two different declarations, refuse; then the one declaration
        stands only if no providing star is conditional and the earliest is strictly above
        the first read (lines compare, so a read above or beside the star refuses). Stars
        leading to the same declaration are one edge, recorded through the last of them.
        """
        self._begin(origin)
        name = meta.get("imported") or meta.get("local") or ""
        first = int(meta.get("first_line") or 0)
        stars = json.loads(meta.get("stars") or "[]")
        examined = [
            self._examine(origin, name, line, spec, bool(conditional))
            for line, spec, conditional in stars
        ]
        unknown = next((s for s in examined if s.verdict == _UNKNOWN), None)
        if unknown is not None:
            return self._refused(unknown.reason or "not-proven-export")
        provided = [s for s in examined if s.verdict == _PROVIDES]
        targets = {
            (s.found.file.file_id, s.found.symbol_id) for s in provided if s.found is not None
        }
        if not targets:
            return self._refused("wildcard-not-exported")
        if len(targets) > 1:
            return self._refused("ambiguous-wildcard")
        if any(s.conditional for s in provided):
            return self._refused("conditional-wildcard")
        if min(s.line for s in provided) >= first:
            return self._refused("wildcard-after-use")
        last = provided[-1]
        assert last.found is not None and last.target is not None
        wildcard = {"line": last.line, "export": last.export, "stars": len(examined)}
        reached = {**meta, "module": last.spec, "imported": name}
        return self._reached(last.target, reached, last.found, last.link, wildcard)

    def _examine(
        self, origin: FileRow, name: str, line: int, spec: str, conditional: bool
    ) -> _Star:
        """What ``from <spec> import *``, at ``line`` of the importer, says about ``name``."""
        target = self._modules.resolve(origin, spec)
        link: PythonLink | None = None
        if isinstance(target, str) and target in _LINKABLE and self._links is not None:
            found = self._links.resolve(origin, spec)
            if isinstance(found, PythonLink):
                target, link = found.target, found
        if isinstance(target, str):
            return _Star(line, conditional, _UNKNOWN, "wildcard-module-unresolved")
        self._reading[-1].add(target.file.file_id)
        verdict, reason, found_in, export = self._exported(target.file, name)
        return _Star(line, conditional, verdict, reason, spec, found_in, target, link, export)

    def _exported(
        self, module: FileRow, name: str
    ) -> tuple[str, str | None, UseDefinition | None, str]:
        """Whether a star import of ``module`` binds ``name``: the verdict, the reason when it
        is unknown, the declaration when it provides the name, and the rule that exports it
        (``all`` or ``public``)."""
        table = self._tables(module.file_id)
        if table is None:
            return _UNKNOWN, "no-export-table", None, "public"
        listed = table.get("all")
        if listed == "dynamic":
            return _UNKNOWN, "dynamic-all", None, "public"
        export = "all" if isinstance(listed, list) else "public"
        if isinstance(listed, list):
            if name not in listed:
                return _EXCLUDES, None, None, export
        elif name.startswith("_"):
            return _EXCLUDES, None, None, export
        bound = table.get("names", {}).get(name)
        if bound is None:
            # Only a name the module does not bind can come from its own star or `__getattr__`.
            if table.get("star"):
                return _UNKNOWN, "star-reexport-deferred", None, export
            if table.get("getattr"):
                return _UNKNOWN, "dynamic-getattr", None, export
            if export == "all":
                return _UNKNOWN, "not-proven-export", None, export
            return _EXCLUDES, None, None, export
        if (bound[0] if bound else "") == "none" or name in table.get("conditional", ()):
            return _UNKNOWN, "not-proven-export", None, export
        # The submodule fallback of a from-import is not a star's: the trace alone decides.
        found, reason, _ = self._trace(module, name, frozenset(), 0)
        if found is not None:
            return _PROVIDES, None, found, export
        if reason in _NOT_PROVEN:
            reason = "not-proven-export"
        elif reason == "star-deferred":
            reason = "star-reexport-deferred"
        return _UNKNOWN, reason or "not-proven-export", None, export

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
