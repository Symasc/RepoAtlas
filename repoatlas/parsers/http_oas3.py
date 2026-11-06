"""Express applications that route an OpenAPI document by operationId (HTTP linkage).

``oas3-tools`` (2.2.3, read from its source) builds an Express application from a
specification and a controllers object:

    const controllers = { ...generatedController, ...statusController };
    const app = oas3Tools.expressAppConfig(
      path.join(__dirname, '../api/openapi.yaml'), { routing: { controllers } },
    ).getApp();

``express-openapi-validator`` serves every path of the document under each root
server's base path, and the router calls the controllers object's member named by the
operation's ``operationId`` -- its dashes camel-cased, and prefixed with
``<x-swagger-router-controller>_`` when the operation names one. No file proves those
routes alone, so each records its part as a claim (:mod:`repoatlas.graph.ts_oas3`):

* :data:`~repoatlas.parsers.http_facts.OAS3_APP` -- ``expressAppConfig(spec, options)``
  on ``oas3-tools`` (a namespace, default or ``require`` binding of it, or the named
  import), bound once at module level. ``spec`` is read by
  :class:`~repoatlas.parsers.ts_spec_paths.SpecPathReader`, relative to the file that
  states it: the library opens a bare relative path from the process's working directory,
  which the source does not state. Exactly one of these keys records the reading:

  * ``spec`` -- the path, its literal segments joined by ``/`` (``../api/openapi.yaml``);
  * ``spec_parts`` -- a JSON list whose first element is ``["dir"]`` (the directory of this
    file) and whose others are the segments in order, ``["t", literal]`` or
    ``["r", specifier, name]`` for a name the relative module ``specifier`` exports;
  * ``spec_imported_path`` -- the whole path is such a name: JSON
    ``[["p", specifier, name]]``;
  * ``spec_param``, ``factory`` and ``factory_exports`` -- the path is the parameter at that
    index of the module-level function ``factory``, which is exported under the names in
    ``factory_exports`` (a JSON list, empty when it is not); with ``spec_param_dir`` too (a
    JSON list of literal segments) when the parameter is the last segment of an anchored
    ``join``, so that a caller's string is joined to this file's directory after them;
  * ``spec_refused`` -- why the source states no path: a ``spec-...`` code.

  ``options.routing.controllers`` is read only as a handler object, or an object literal
  of spreads of handler objects, each declared in the file or imported from a relative
  module by name or as its default export (later spreads win, as in JavaScript). A
  directory of controllers, or anything else, is recorded as unread;
* :data:`~repoatlas.parsers.http_facts.OAS3_FACTORY_CALL` -- a call of a function the file
  declares that holds an application (always, so a refusal is visible), or of a function
  imported by name from a relative module when an argument reads as a document: a path
  whose last segment is a literal ending ``.yaml``, ``.yml`` or ``.json``, or is imported,
  or a string segment ending so.
  ``local``, or ``module`` and ``name``; and ``arguments``, a JSON list (at most 8) of
  ``{"spec": ...}``, ``{"spec_parts": ...}`` (the list above, nested), ``{"refused": code}``
  or ``{}``, each read relative to the caller's file. An argument that is a string literal, or
  a ``const`` bound to one, also records it as ``segment``: what a ``spec_param_dir`` factory
  joins to its own directory. An argument that is itself a parameter
  is refused as ``spec-propagation-depth``: a second level is not followed;
* :data:`~repoatlas.parsers.http_facts.TS_PATH_CONSTANTS` -- the module's exported
  module-level ``const`` values that the file alone reads as a path ending ``.yaml``,
  ``.yml`` or ``.json``: ``constants``, ``{name: [path, line]}``;
* :data:`~repoatlas.parsers.http_facts.TS_HANDLER_OBJECT` -- each module-level ``const``
  bound once to an object literal whose every member is a function (a method, or a key
  whose value is a function), with its keys, their lines, and the names it is exported
  under. The claim belongs to the ``const``: it is the handler a route is served by.

``__dirname`` is read as the directory of the file that states it: the source is run
where it stands, or built to the same depth.

An argument or an exported value is read only when it is shaped like a path: a name, a
property, a template, a ``+``, or a ``join``/``resolve`` call (a call of an alias of those
is not read), and for a call to an imported function or an exported value only a name that
is a ``const``. An argument of a call of a function the file declares is also read when it
is a string literal, which is refused (``spec-cwd-relative``) so that the refusal is
visible. At most ``_MAX_READS`` of them are read in a file.

Parsed, never executed (V1_SPEC §17).
"""

from __future__ import annotations

import json
from collections import Counter
from typing import TYPE_CHECKING

from repoatlas.models.parsing import EdgeType, EntityType, ParsedReference
from repoatlas.parsers.http_facts import (
    OAS3_APP,
    OAS3_FACTORY_CALL,
    TS_HANDLER_OBJECT,
    TS_PATH_CONSTANTS,
    pending_reference,
)
from repoatlas.parsers.http_ts_wrappers import (
    Const,
    FetchWrappers,
    Imported,
    arguments_of,
    operand,
)
from repoatlas.parsers.http_typescript import static_string
from repoatlas.parsers.treesitter import line_range, node_text, string_literal, walk
from repoatlas.parsers.ts_spec_paths import (
    _JOINS,
    SPEC_DYNAMIC,
    SPEC_IMPORT_UNSUPPORTED,
    SPEC_PARAMETER_UNSUPPORTED,
    SPEC_PROPAGATION_DEPTH,
    ImportedPath,
    Param,
    ParamSegment,
    Parts,
    Refused,
    Result,
    Spec,
    SpecPathReader,
    _ordered,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tree_sitter import Node

OAS3_TOOLS = "oas3-tools"
FACTORY = "expressAppConfig"
_FUNCTION_VALUES = frozenset({"arrow_function", "function_expression", "function"})
_DECLARED = frozenset({"function_declaration", "generator_function_declaration"})
_NAMES = frozenset({"identifier", "shorthand_property_identifier"})
_ACCESSORS = frozenset({"get", "set"})
#: A binding of a whole module: a namespace or default import, or ``require``.
_MODULE = "*"
#: How many ``const`` names a value is followed through.
_MAX_DEPTH = 4
#: Keys one handler object may have and still be recorded.
_MAX_KEYS = 256
#: Arguments of a call one claim records.
_MAX_ARGUMENTS = 8
#: Path expressions a file has read for its calls, and for its exported constants, each.
_MAX_READS = 256
#: Exported paths one module offers.
_MAX_CONSTANTS = 64
#: What a document's path ends with.
_DOCUMENT_SUFFIXES = (".yaml", ".yml", ".json")


class SpecRoutedApps:
    """What one file states about oas3-tools applications and their controllers."""

    def __init__(
        self, source: bytes, root: Node, counts: Counter[str], wrappers: FetchWrappers
    ) -> None:
        self._source = source
        self._root = root
        self._counts = counts
        self._wrappers = wrappers
        self._reader = SpecPathReader(source, root, counts, wrappers)
        #: Path expressions read for calls, so far.
        self._reads = 0
        #: Module-level function name → function, for those that hold an application.
        self._holders: dict[str, Node] | None = None
        #: Local name → ``(module, imported name or _MODULE)``, for oas3-tools.
        self._packages: dict[str, tuple[str, str]] = {}
        #: Local name → ``(relative specifier, imported name)``.
        self._relative: dict[str, tuple[str, str]] = {}
        #: Local name → relative specifier, for a default import: read only as controllers.
        self._defaults: dict[str, str] = {}
        #: Handler object name → ``(keys and their lines, first line, last line)``.
        self._objects: dict[str, tuple[dict[str, int], int, int]] = {}
        self._exports: dict[str, list[str]] = {}
        self._top_level(root)

    # -- the module ------------------------------------------------------------------

    def _top_level(self, root: Node) -> None:
        aliases: list[tuple[str, str]] = []
        for statement in root.named_children:
            if statement.type == "import_statement":
                self._import(statement)
            elif statement.type in {"lexical_declaration", "variable_declaration"}:
                self._declaration(statement, exported=False)
            elif statement.type == "export_statement":
                aliases.extend(self._export(statement))
        for local, exported in aliases:
            if local in self._objects:
                self._exports.setdefault(local, []).append(exported)

    def _bind(self, local: str, module: str, imported: str) -> None:
        if self._counts[local] != 1:
            return
        if module == OAS3_TOOLS:
            self._packages[local] = (module, imported)
        elif module.startswith(".") and imported != _MODULE:
            self._relative[local] = (module, imported)

    def _import(self, statement: Node) -> None:
        if any(child.type == "type" for child in statement.children):
            return
        source_node = statement.child_by_field_name("source")
        module = string_literal(self._source, source_node) if source_node is not None else None
        clause = next((c for c in statement.children if c.type == "import_clause"), None)
        if module is None or clause is None:
            return
        for child in clause.named_children:
            if child.type == "identifier":
                local = node_text(self._source, child)
                self._bind(local, module, _MODULE)
                if module.startswith(".") and self._counts[local] == 1:
                    self._defaults[local] = module
            elif child.type == "namespace_import":
                name = next((c for c in child.named_children if c.type == "identifier"), None)
                if name is not None:
                    self._bind(node_text(self._source, name), module, _MODULE)
            elif child.type == "named_imports":
                for specifier in child.named_children:
                    if specifier.type != "import_specifier" or any(
                        c.type == "type" for c in specifier.children
                    ):
                        continue
                    imported = specifier.child_by_field_name("name")
                    alias = specifier.child_by_field_name("alias") or imported
                    if imported is not None and alias is not None:
                        self._bind(
                            node_text(self._source, alias),
                            module,
                            node_text(self._source, imported),
                        )

    def _declaration(self, statement: Node, *, exported: bool) -> list[tuple[str, str]]:
        kind = statement.child_by_field_name("kind")
        constant = kind is not None and node_text(self._source, kind) == "const"
        found: list[tuple[str, str]] = []
        for declarator in statement.named_children:
            name_node = declarator.child_by_field_name("name")
            value = declarator.child_by_field_name("value")
            if declarator.type != "variable_declarator" or name_node is None or value is None:
                continue
            if name_node.type != "identifier":
                continue
            name = node_text(self._source, name_node)
            value = operand(value)
            required = self._required(value)
            if required is not None:
                self._bind(name, required, _MODULE)
                continue
            members = self._members(value) if constant and self._counts[name] == 1 else None
            if members is not None:
                start, end = line_range(declarator)
                self._objects[name] = (members, start, end)
                if exported:
                    found.append((name, name))
        return found

    def _required(self, value: Node) -> str | None:
        """The module ``require('<module>')`` loads, for a call bound to a name."""
        callee = value.child_by_field_name("function") if value.type == "call_expression" else None
        if callee is None or node_text(self._source, callee) != "require":
            return None
        arguments = arguments_of(value) or []
        if len(arguments) != 1 or self._counts["require"]:
            return None
        return string_literal(self._source, arguments[0])

    def _export(self, statement: Node) -> list[tuple[str, str]]:
        declaration = statement.child_by_field_name("declaration")
        default = any(child.type == "default" for child in statement.children)
        if declaration is not None:
            if declaration.type == "lexical_declaration" and not default:
                return self._declaration(declaration, exported=True)
            return []
        value = statement.child_by_field_name("value")
        if value is not None:
            value = operand(value)
            if default and value.type == "identifier":
                return [(node_text(self._source, value), "default")]
            return []
        if statement.child_by_field_name("source") is not None:
            return []
        found: list[tuple[str, str]] = []
        for clause in statement.named_children:
            if clause.type != "export_clause":
                continue
            for specifier in clause.named_children:
                name = specifier.child_by_field_name("name")
                alias = specifier.child_by_field_name("alias") or name
                if specifier.type == "export_specifier" and name is not None and alias is not None:
                    found.append((node_text(self._source, name), node_text(self._source, alias)))
        return found

    def _members(self, node: Node) -> dict[str, int] | None:
        """Each key of an object literal whose every member is a function, and its line."""
        if node.type != "object":
            return None
        keys: dict[str, int] = {}
        for entry in node.named_children:
            if entry.type == "comment":
                continue
            if entry.type == "pair":
                key = self._wrappers.key(entry)
                value = entry.child_by_field_name("value")
                if key is None or value is None or operand(value).type not in _FUNCTION_VALUES:
                    return None
            elif entry.type == "method_definition":
                name = entry.child_by_field_name("name")
                if name is None or any(c.type in _ACCESSORS for c in entry.children):
                    return None
                if name.type == "property_identifier":
                    key = node_text(self._source, name)
                elif name.type == "string":
                    key = string_literal(self._source, name)
                else:
                    key = None
                if key is None:
                    return None
            else:
                return None  # a spread or a shorthand: a member not proven a function
            keys[key] = line_range(entry)[0]
        return keys if 0 < len(keys) <= _MAX_KEYS else None

    # -- calls ---------------------------------------------------------------------------

    def call_references(self, node: Node, source_symbol: str | None) -> list[ParsedReference]:
        callee = node.child_by_field_name("function")
        if callee is None:
            return []
        callee = operand(callee)
        if not self._is_factory(callee):
            return self._factory_call(node, callee, source_symbol)
        arguments = arguments_of(node)
        if arguments is None or len(arguments) != 2:
            return []
        meta = self._specification(self._reader.resolve(arguments[0]))
        controllers = self._controllers(arguments[1])
        if controllers is not None:
            meta["controllers"] = json.dumps(controllers, sort_keys=True)
        start, end = line_range(node)
        return [
            pending_reference(
                EdgeType.REFERENCES,
                EntityType.CONFIG,
                FACTORY,
                start,
                end,
                role=OAS3_APP,
                source_symbol=source_symbol,
                **meta,
            )
        ]

    def _package(self, node: Node) -> tuple[str, str] | None:
        """What a name refers to among oas3-tools, at its module-level binding."""
        if node.type != "identifier":
            return None
        name = node_text(self._source, node)
        bound = self._packages.get(name)
        return bound if bound is not None and self._wrappers.module_binding(name, node) else None

    def _is_factory(self, callee: Node) -> bool:
        if callee.type == "identifier":
            return self._package(callee) == (OAS3_TOOLS, FACTORY)
        if callee.type != "member_expression":
            return False
        owner = callee.child_by_field_name("object")
        prop = callee.child_by_field_name("property")
        if owner is None or prop is None or node_text(self._source, prop) != FACTORY:
            return False
        return self._package(operand(owner)) == (OAS3_TOOLS, _MODULE)

    def _specification(self, result: Result) -> dict[str, str]:
        """The one claim key, or keys, that record how an application's path was read."""
        if isinstance(result, Spec):
            return {"spec": result.text}
        if isinstance(result, Parts):
            return {"spec_parts": json.dumps(_parts(result))}
        if isinstance(result, ImportedPath):
            return {"spec_imported_path": json.dumps([["p", result.specifier, result.imported]])}
        if isinstance(result, Param | ParamSegment):
            return self._parameter(result)
        return {"spec_refused": result.code}

    def _parameter(self, result: Param | ParamSegment) -> dict[str, str]:
        """The path is the parameter of a function bound once at module level, by name."""
        function = result.function
        if function.type in _DECLARED:
            name_node = function.child_by_field_name("name")
        else:  # a function value of a ``const``: the reader proved the shape
            declarator = function.parent
            name_node = declarator.child_by_field_name("name") if declarator is not None else None
        if name_node is None or name_node.type != "identifier":
            return {"spec_refused": SPEC_PARAMETER_UNSUPPORTED}
        name = node_text(self._source, name_node)
        if self._counts[name] != 1:
            return {"spec_refused": SPEC_PARAMETER_UNSUPPORTED}
        found = {
            "spec_param": str(result.index),
            "factory": name,
            "factory_exports": json.dumps(self._function_exports(name)),
        }
        if isinstance(result, ParamSegment):
            found["spec_param_dir"] = json.dumps(list(result.parts))
        return found

    def _function_exports(self, name: str) -> list[str]:
        """The names the module-level function or ``const`` ``name`` is exported under."""
        found: list[str] = []
        for statement in self._root.named_children:
            if statement.type != "export_statement":
                continue
            default = any(child.type == "default" for child in statement.children)
            declaration = statement.child_by_field_name("declaration")
            value = statement.child_by_field_name("value")
            if declaration is not None:
                if self._declares(declaration, name):
                    found.append("default" if default else name)
            elif value is not None:
                value = operand(value)
                if (
                    default
                    and value.type == "identifier"
                    and node_text(self._source, value) == name
                ):
                    found.append("default")
            elif statement.child_by_field_name("source") is None:
                for clause in statement.named_children:
                    if clause.type != "export_clause":
                        continue
                    for specifier in clause.named_children:
                        local = specifier.child_by_field_name("name")
                        alias = specifier.child_by_field_name("alias") or local
                        if (
                            specifier.type == "export_specifier"
                            and local is not None
                            and alias is not None
                            and node_text(self._source, local) == name
                        ):
                            found.append(node_text(self._source, alias))
        return list(dict.fromkeys(found))

    def _declares(self, declaration: Node, name: str) -> bool:
        """Whether a declaration is the function, or the ``const``, ``name``."""
        if declaration.type in _DECLARED:
            declared = declaration.child_by_field_name("name")
            return declared is not None and node_text(self._source, declared) == name
        if declaration.type != "lexical_declaration":
            return False
        for declarator in declaration.named_children:
            declared = declarator.child_by_field_name("name")
            if (
                declarator.type == "variable_declarator"
                and declared is not None
                and declared.type == "identifier"
                and node_text(self._source, declared) == name
            ):
                return True
        return False

    # -- calls of the functions that hold applications -------------------------------------

    def _holder_functions(self) -> dict[str, Node]:
        """Each module-level function, bound once, that holds a call of ``expressAppConfig``."""
        if self._holders is None:
            holders: dict[str, Node] = {}
            if self._packages:
                for name, function in self._declared_functions():
                    if self._counts[name] == 1 and any(
                        self._is_application(node) for node in walk(function)
                    ):
                        holders[name] = function
            self._holders = holders
        return self._holders

    def _declared_functions(self) -> list[tuple[str, Node]]:
        """The functions declared at the top of the file: a declaration, or a ``const``'s value."""
        found: list[tuple[str, Node]] = []
        for statement in self._root.named_children:
            if statement.type == "export_statement":
                statement = statement.child_by_field_name("declaration") or statement
            if statement.type in _DECLARED:
                name = statement.child_by_field_name("name")
                if name is not None:
                    found.append((node_text(self._source, name), statement))
                continue
            kind = statement.child_by_field_name("kind")
            if statement.type != "lexical_declaration" or kind is None:
                continue
            if node_text(self._source, kind) != "const":
                continue
            for declarator in statement.named_children:
                name = declarator.child_by_field_name("name")
                value = declarator.child_by_field_name("value")
                if (
                    declarator.type == "variable_declarator"
                    and name is not None
                    and name.type == "identifier"
                    and value is not None
                    and operand(value).type in _FUNCTION_VALUES
                ):
                    found.append((node_text(self._source, name), operand(value)))
        return found

    def _is_application(self, node: Node) -> bool:
        """Whether ``node`` is a call of ``expressAppConfig`` that is an application."""
        callee = node.child_by_field_name("function") if node.type == "call_expression" else None
        if callee is None or not self._is_factory(operand(callee)):
            return False
        arguments = arguments_of(node)
        return arguments is not None and len(arguments) == 2

    def _factory_call(
        self, node: Node, callee: Node, source_symbol: str | None
    ) -> list[ParsedReference]:
        """A call of a function that holds an application, with what it is handed."""
        if callee.type != "identifier":
            return []
        name = node_text(self._source, callee)
        local = name in self._holder_functions()
        imported = self._relative.get(name)
        if not (local or imported) or not self._wrappers.module_binding(name, callee):
            return []
        container = node.child_by_field_name("arguments")
        if container is None or container.type != "arguments":
            return []
        entries: list[dict[str, object]] = []
        document = False
        spread = False
        for argument in [c for c in container.named_children if c.type != "comment"]:
            if len(entries) == _MAX_ARGUMENTS:
                break
            spread = spread or argument.type == "spread_element"
            if spread:  # from a spread on, no argument has a known position
                entries.append({"refused": SPEC_DYNAMIC})
                continue
            result = self._read(argument, proven=not local)
            entry = _entry(result)
            segment = self._segment(argument)
            if segment is not None:
                entry["segment"] = segment
                document = document or segment.endswith(_DOCUMENT_SUFFIXES)
            document = document or _is_document(result)
            entries.append(entry)
        if local:
            meta = {"local": name}
        elif imported is not None and document:
            meta = {"module": imported[0], "name": imported[1]}
        else:
            return []
        start, end = line_range(node)
        return [
            pending_reference(
                EdgeType.REFERENCES,
                EntityType.SYMBOL,
                name,
                start,
                end,
                role=OAS3_FACTORY_CALL,
                source_symbol=source_symbol,
                arguments=json.dumps(entries, sort_keys=True),
                **meta,
            )
        ]

    def _segment(self, node: Node, depth: int = 0) -> str | None:
        """A string literal, or the one a ``const`` bound before ``node`` is, through at most
        :data:`_MAX_DEPTH` names."""
        node = operand(node)
        text = static_string(self._source, node)
        if text is not None or node.type != "identifier" or depth >= _MAX_DEPTH:
            return text
        binding = self._wrappers.lookup(node_text(self._source, node), node)
        if not isinstance(binding, Const) or not _ordered(binding.value, node):
            return None
        return self._segment(binding.value, depth + 1)

    def _read(self, node: Node, *, proven: bool) -> Result | None:
        """What the reader says of an argument shaped like a path, or ``None`` for another."""
        if self._reads >= _MAX_READS or not self._shaped(node, proven=proven):
            return None
        self._reads += 1
        return self._reader.resolve(node)

    def _shaped(self, node: Node, *, proven: bool) -> bool:
        """Whether ``node`` is worth reading: a name, a property, a template, a ``+`` or a
        ``join``/``resolve`` call. With ``proven``, a name must also be a ``const`` here, so
        that reading it never has to search the file for why it is not. Without it (a call
        of a function the file declares) a string literal is read too, so that its refusal is
        recorded."""
        node = operand(node)
        if node.type in {"identifier", "member_expression"}:
            if not proven:
                return True
            owner: Node | None = node
            if node.type == "member_expression":
                owner = node.child_by_field_name("object")
            owner = operand(owner) if owner is not None else None
            return (
                owner is not None
                and owner.type == "identifier"
                and isinstance(self._wrappers.lookup(node_text(self._source, owner), owner), Const)
            )
        if node.type == "template_string":
            return True
        if node.type == "string":
            return not proven
        if node.type == "binary_expression":
            operator = node.child_by_field_name("operator")
            return operator is not None and node_text(self._source, operator) == "+"
        if node.type == "call_expression":
            callee = node.child_by_field_name("function")
            callee = operand(callee) if callee is not None else None
            if callee is not None and callee.type == "member_expression":
                callee = callee.child_by_field_name("property")
            return (
                callee is not None
                and callee.type in {"identifier", "property_identifier"}
                and node_text(self._source, callee) in _JOINS
            )
        return False

    def _object(self, node: Node, depth: int = 0) -> Node | None:
        """The object literal a value is, through ``const`` names."""
        node = operand(node)
        if node.type in _NAMES and depth < _MAX_DEPTH:
            binding = self._wrappers.lookup(node_text(self._source, node), node)
            return self._object(binding.value, depth + 1) if isinstance(binding, Const) else None
        return node if node.type == "object" else None

    def _entries(self, node: Node) -> dict[str, Node] | None:
        """An object literal's entries by key, or ``None`` when a key is not provable."""
        entries: dict[str, Node] = {}
        for entry in node.named_children:
            if entry.type == "comment":
                continue
            if entry.type == "shorthand_property_identifier":
                entries[node_text(self._source, entry)] = entry
            elif entry.type == "pair":
                key = self._wrappers.key(entry)
                value = entry.child_by_field_name("value")
                if key is None or value is None:
                    return None
                entries[key] = value
            else:
                return None
        return entries

    def _controllers(self, options: Node) -> list[dict[str, str]] | None:
        """``options.routing.controllers`` as handler objects, in spread order."""
        found: Node | None = options
        for key in ("routing", "controllers"):
            literal = self._object(found) if found is not None else None
            entries = self._entries(literal) if literal is not None else None
            found = entries.get(key) if entries is not None else None
        if found is None:
            return None
        own = self._handler(found)
        if own is not None:
            return [own]
        literal = self._object(found)
        if literal is None:
            return None
        refs: list[dict[str, str]] = []
        for entry in literal.named_children:
            if entry.type == "comment":
                continue
            inner = next((c for c in entry.named_children if c.type != "comment"), None)
            ref = self._handler(inner) if entry.type == "spread_element" and inner else None
            if ref is None:
                return None
            refs.append(ref)
        return refs or None

    def _handler(self, node: Node) -> dict[str, str] | None:
        """A handler object declared here, or one imported by name from a relative module."""
        node = operand(node)
        if node.type not in _NAMES:
            return None
        name = node_text(self._source, node)
        if not self._wrappers.module_binding(name, node):
            return None
        if name in self._objects:
            return {"local": name}
        if name in self._relative:
            module, imported = self._relative[name]
            return {"module": module, "name": imported}
        if name in self._defaults:
            return {"module": self._defaults[name], "name": "default"}
        return None

    # -- claims --------------------------------------------------------------------------

    def references(self) -> list[ParsedReference]:
        """Each handler object, owned by its ``const``, with its keys and exports; and the
        paths of documents the module exports."""
        found = [
            pending_reference(
                EdgeType.REFERENCES,
                EntityType.SYMBOL,
                name,
                start,
                end,
                role=TS_HANDLER_OBJECT,
                source_symbol=name,
                keys=json.dumps(keys, sort_keys=True),
                exports=json.dumps(self._exports.get(name, [])),
            )
            for name, (keys, start, end) in self._objects.items()
        ]
        offered = self._path_constants()
        if offered:
            found.append(
                pending_reference(
                    EdgeType.REFERENCES,
                    EntityType.CONFIG,
                    TS_PATH_CONSTANTS,
                    offered[0][0],
                    offered[-1][0],
                    role=TS_PATH_CONSTANTS,
                    constants=json.dumps(
                        {name: [spec, line] for line, name, spec in offered}, sort_keys=True
                    ),
                )
            )
        return found

    def _path_constants(self) -> list[tuple[int, str, str]]:
        """``(line, exported name, path)`` of each exported module-level ``const`` that the
        file reads as a path ending in a document suffix, by line."""
        declared: dict[str, Node] = {}
        offered: dict[str, str] = {}  # exported name → the ``const`` it is
        for statement in self._root.named_children:
            exported = statement.type == "export_statement"
            if exported:
                if any(child.type == "default" for child in statement.children):
                    continue
                if statement.child_by_field_name("source") is None:
                    for clause in statement.named_children:
                        if clause.type == "export_clause":
                            self._offer(clause, offered)
                statement = statement.child_by_field_name("declaration") or statement
            kind = statement.child_by_field_name("kind")
            if statement.type != "lexical_declaration" or kind is None:
                continue
            if node_text(self._source, kind) != "const":
                continue
            for declarator in statement.named_children:
                name = declarator.child_by_field_name("name")
                if declarator.type != "variable_declarator" or name is None:
                    continue
                if name.type != "identifier" or self._counts[node_text(self._source, name)] != 1:
                    continue
                declared[node_text(self._source, name)] = declarator
                if exported:
                    offered[node_text(self._source, name)] = node_text(self._source, name)
        found: list[tuple[int, str, str]] = []
        reads = 0
        for exported_name, local in offered.items():
            declarator = declared.get(local)
            value = declarator.child_by_field_name("value") if declarator is not None else None
            if declarator is None or value is None or reads >= _MAX_READS:
                continue
            if not self._shaped(value, proven=True):
                continue
            reads += 1
            result = self._reader.resolve(value)
            if isinstance(result, Spec) and result.text.endswith(_DOCUMENT_SUFFIXES):
                found.append((line_range(declarator)[0], exported_name, result.text))
        return sorted(found)[:_MAX_CONSTANTS]

    def _offer(self, clause: Node, offered: dict[str, str]) -> None:
        """``export { local as exported }``: the exported names of a clause, by local name."""
        for specifier in clause.named_children:
            local = specifier.child_by_field_name("name")
            alias = specifier.child_by_field_name("alias") or local
            if specifier.type == "export_specifier" and local is not None and alias is not None:
                offered[node_text(self._source, alias)] = node_text(self._source, local)


def _parts(parts: Parts) -> list[list[str]]:
    """The segments of a path as the claim records them, after the file's own directory."""
    return [
        ["dir"],
        *(
            ["r", part.specifier, part.imported] if isinstance(part, Imported) else ["t", part]
            for part in parts.parts
        ),
    ]


def _is_document(result: Result | None) -> bool:
    """Whether a path ends in a document's suffix, or in a name another module exports."""
    if isinstance(result, Spec):
        return result.text.endswith(_DOCUMENT_SUFFIXES)
    if isinstance(result, Parts):
        last = result.parts[-1]
        return isinstance(last, Imported) or last.endswith(_DOCUMENT_SUFFIXES)
    return False


def _entry(result: Result | None) -> dict[str, object]:
    """One argument of a call as the claim records it."""
    if isinstance(result, Spec):
        return {"spec": result.text}
    if isinstance(result, Parts):
        return {"spec_parts": _parts(result)}
    if isinstance(result, Param):  # a second level of parameters is not followed
        return {"refused": SPEC_PROPAGATION_DEPTH}
    if isinstance(result, ImportedPath):  # the other module's value is not read in here
        return {"refused": SPEC_IMPORT_UNSUPPORTED}
    if isinstance(result, Refused):
        return {"refused": result.code}
    return {}
