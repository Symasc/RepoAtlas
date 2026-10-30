"""Declaration-level uses of module-scope names in TypeScript and JavaScript (``USES``).

A change to an exported constant, type or component reaches the declarations that
*use* it. The import graph records that a file imports a name (``REFERENCES``) and
the call graph that a function calls one (``CALLS``); neither says which declaration
reads a constant, constructs a class, renders a component or names a type. This
module finds those uses, and only the ones it can prove:

* the name is bound at **module scope** -- declared at the top of the file or bound
  by one of its imports -- and nothing on the way down rebinds it: no enclosing
  parameter, variable, function or class name, catch clause, loop head, type
  parameter, ``infer`` or mapped-type key. Scoping is deliberately coarse. A
  ``var`` anywhere in an enclosing function counts as rebinding the name throughout
  that function, so a doubt costs a fact rather than inventing one;
* the name is declared there exactly once. Overload signatures followed by their one
  implementation are one declaration, the implementation; every other repeated name
  (declaration merging, a namespace beside a function) is refused, as are names no
  entity carries: a namespace, a destructured binding, ``import x = require()``.

A use records what kind it is -- ``value``, ``type`` or ``call`` -- the syntax it
came from (``form``), and how the name is bound (``local``, ``import`` or
``namespace``). Which declaration it belongs
to is the walker's decision, made exactly as it attributes calls
(:class:`repoatlas.parsers.typescript._Walker`); :func:`use_references` then drops
self-references and merges repeats.

Not recorded, because another fact already says it: a class or interface heritage
clause (``REFERENCES``), an import or export specifier (the import graph and the
export table below), and a callee this file declares that the call graph attributes
to the same declaration (see the walker for why an imported callee is recorded).

The file's **export table** is recorded as one more fact: which name each export is
bound to -- a local declaration, an import, a namespace -- and which modules it
re-exports wholesale. Only :mod:`repoatlas.graph.uses` reads it, to follow a use
through re-exporting barrels under every name it passes by, so no existing
resolution changes.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

from repoatlas.models.parsing import (
    FIELD,
    USES_EXPORT_TABLE,
    EdgeType,
    EntityType,
    ParsedReference,
    ParsedSymbol,
)
from repoatlas.parsers.treesitter import line_range, node_text, string_literal

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tree_sitter import Node

#: The ``meta`` key marking a reference as a file's export table (see the module docstring).
USES_EXPORTS = "uses_exports"
#: The ``meta`` key of a use's model-field members, ``[[field, line, access], ...]`` (DF3b):
#: what the declaration reads through a parameter typed by the used name, or sets in an
#: object literal (Python: a construction) of that type. Resolved with the use.
FIELD_MEMBERS = "fields"
#: The most distinct fields one use carries: a guard, since each is a written member. Past it the
#: first ones by line are kept and the rest counted (:data:`FIELDS_OMITTED`); the parser warns.
MAX_FIELD_MEMBERS = 256
#: The ``meta`` key counting a use's members past :data:`MAX_FIELD_MEMBERS`, when there are any.
FIELDS_OMITTED = "fields_omitted"
#: The warning a parser records when a use's members were read only in part.
FIELDS_TRUNCATED = f"model-field members read only in part (at most {MAX_FIELD_MEMBERS} per use)"


def member_meta(listed: list[list[object]]) -> dict[str, str]:
    """A use's members, ``[field, line, access]`` in line order, as its claim records them:
    at most :data:`MAX_FIELD_MEMBERS`, and how many more there were, never silently fewer."""
    meta = {FIELD_MEMBERS: json.dumps(listed[:MAX_FIELD_MEMBERS], separators=(",", ":"))}
    if len(listed) > MAX_FIELD_MEMBERS:
        meta[FIELDS_OMITTED] = str(len(listed) - MAX_FIELD_MEMBERS)
    return meta


#: Node kinds a use can be read from.
USE_NODES = frozenset({"identifier", "type_identifier", "shorthand_property_identifier"})

#: Nodes whose parameters (and, for expressions, own name) bind inside them.
_FUNCTIONS = frozenset(
    {
        "function_declaration",
        "generator_function_declaration",
        "function_expression",
        "function",
        "generator_function",
        "arrow_function",
        "method_definition",
        "function_signature",
        "method_signature",
        "abstract_method_signature",
        "call_signature",
        "construct_signature",
        "function_type",
        "constructor_type",
    }
)
#: Function expressions whose own name binds inside their body.
_NAMED_EXPRESSIONS = frozenset({"function_expression", "function", "generator_function", "class"})
_CLASSES = frozenset({"class_declaration", "abstract_class_declaration", "class"})
#: Declarations whose type parameters bind inside them.
_GENERIC = frozenset({*_FUNCTIONS, *_CLASSES, "interface_declaration", "type_alias_declaration"})
_BLOCKS = frozenset({"statement_block", "switch_body", "class_static_block"})
_SCOPES = frozenset(
    {
        *_GENERIC,
        *_BLOCKS,
        "for_statement",
        "for_in_statement",
        "catch_clause",
        "conditional_type",
        "index_signature",
    }
)

#: Subtrees that hold no use this module records (see the module docstring).
_SKIP = frozenset(
    {
        "import_statement",
        "class_heritage",
        "extends_type_clause",
        "jsx_closing_element",
        "comment",
        "string",
        "regex",
    }
)

#: Declarations that name one module-scope entity, by how they overload.
_DECLARED = frozenset(
    {
        "function_declaration",
        "generator_function_declaration",
        "class_declaration",
        "abstract_class_declaration",
        "interface_declaration",
        "type_alias_declaration",
        "enum_declaration",
        "module",
    }
)
_SIGNATURE = "function_signature"
_FUNCTION_BODIES = frozenset({"function_declaration", "generator_function_declaration"})

#: (parent kind, field) pairs where an identifier is a name being bound or written.
_NOT_A_USE = frozenset(
    {
        ("variable_declarator", "name"),
        ("function_declaration", "name"),
        ("generator_function_declaration", "name"),
        ("function_expression", "name"),
        ("function", "name"),
        ("generator_function", "name"),
        ("function_signature", "name"),
        ("class_declaration", "name"),
        ("abstract_class_declaration", "name"),
        ("class", "name"),
        ("interface_declaration", "name"),
        ("type_alias_declaration", "name"),
        ("enum_declaration", "name"),
        ("internal_module", "name"),
        ("module", "name"),
        ("type_parameter", "name"),
        ("mapped_type_clause", "name"),
        ("nested_type_identifier", "name"),
        ("required_parameter", "pattern"),
        ("optional_parameter", "pattern"),
        ("arrow_function", "parameter"),
        ("catch_clause", "parameter"),
        ("for_in_statement", "left"),
        ("assignment_expression", "left"),
        ("augmented_assignment_expression", "left"),
        ("assignment_pattern", "left"),
        ("object_assignment_pattern", "left"),
        ("pair_pattern", "value"),
        ("type_predicate", "name"),
        ("labeled_statement", "label"),
    }
)
#: Parents under which an identifier is always bound, written or merely spelled.
_NOT_A_USE_UNDER = frozenset(
    {
        "array_pattern",
        "rest_pattern",
        "formal_parameters",
        "update_expression",
        "infer_type",
        "import_specifier",
        "export_specifier",
        "namespace_import",
        "namespace_export",
        "import_clause",
        "meta_property",
        "asserts",
    }
)


@dataclass(frozen=True, slots=True)
class Binding:
    """What one module-scope name is bound to."""

    #: ``local`` (declared in this file), ``import``, ``namespace``, or ``none``.
    kind: str
    #: ``local``: the first line of the declaration the name resolves to.
    line: int | None = None
    #: ``import`` / ``namespace``: the module specifier, verbatim.
    module: str | None = None
    #: ``import``: the name the module exports (``default`` for a default import).
    imported: str | None = None
    type_only: bool = False
    #: ``none``: why no use of this name can be resolved.
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class UseSite:
    """One syntactic use of a module-scope name, before it is attributed."""

    name: str
    binding: Binding
    use: str
    form: str
    line: int
    #: A namespace import's member the use reaches (``ns.X``), if it names one.
    member: str | None = None
    #: The model fields read or set through this use of a type (:func:`field_members`).
    members: tuple[tuple[str, int, str], ...] = ()

    @property
    def target_key(self) -> str:
        return f"{self.name}.{self.member}" if self.member else self.name


class UseAnalysis:
    """The module-scope bindings, export table and use sites of one parsed file."""

    def __init__(self, source: bytes, root: Node) -> None:
        self._source = source
        self.bindings: dict[str, Binding] = {}
        #: Keyed by (start byte, end byte, node kind), which the walker looks up.
        self.sites: dict[tuple[int, int, str], UseSite] = {}
        #: Uses refused, by reason; measurement only.
        self.rejected: Counter[str] = Counter()
        self._depth: Counter[str] = Counter()
        #: Model-field members by the type name node they ride on (DF3b).
        self._members = field_members(source, root)
        self._module_bindings(root)
        self.exports = self._export_table(root)
        for index, child in enumerate(root.children):
            self._visit(child, root.field_name_for_child(index), root, typeof=False)

    # -- module scope ----------------------------------------------------------

    def _text(self, node: Node) -> str:
        return node_text(self._source, node)

    def _module_bindings(self, root: Node) -> None:
        declared: dict[str, list[tuple[str, int]]] = {}
        refused: dict[str, str] = {}
        imported: dict[str, Binding] = {}
        for statement in root.named_children:
            if statement.type == "import_statement":
                for name, binding in self._import_bindings(statement):
                    if name in imported:
                        refused[name] = "duplicate"
                    imported[name] = binding
                continue
            for node in _module_declarations(statement):
                self._declare(node, declared, refused)
        for name, forms in declared.items():
            if name in imported:
                refused[name] = "duplicate"
                continue
            kinds = [kind for kind, _ in forms]
            bodies = [line for kind, line in forms if kind != _SIGNATURE]
            if len(forms) == 1:
                self.bindings[name] = Binding("local", line=forms[0][1])
            elif len(bodies) == 1 and kinds.count(_SIGNATURE) == len(forms) - 1:
                # Overload signatures and their implementation: one function.
                self.bindings[name] = Binding("local", line=bodies[0])
            else:
                refused[name] = "duplicate"
        self.bindings.update({n: b for n, b in imported.items() if n not in refused})
        self.bindings.update({n: Binding("none", reason=r) for n, r in refused.items()})

    def _declare(
        self, node: Node, declared: dict[str, list[tuple[str, int]]], refused: dict[str, str]
    ) -> None:
        kind = node.type
        if kind in _DECLARED or kind == _SIGNATURE:
            name = node.child_by_field_name("name")
            if name is not None and name.type in {"identifier", "type_identifier"}:
                form = kind if kind in {_SIGNATURE, *_FUNCTION_BODIES} else "declaration"
                declared.setdefault(self._text(name), []).append((form, line_range(node)[0]))
        elif kind == "internal_module":
            name = node.child_by_field_name("name")
            if name is not None:
                refused[self._text(name).split(".", 1)[0]] = "namespace"
        elif kind in {"lexical_declaration", "variable_declaration"}:
            for declarator in node.named_children:
                if declarator.type != "variable_declarator":
                    continue
                name = declarator.child_by_field_name("name")
                if name is None:
                    continue
                if name.type == "identifier":
                    declared.setdefault(self._text(name), []).append(
                        ("binding", line_range(declarator)[0])
                    )
                else:
                    for bound in self._pattern_names(name):
                        refused[bound] = "destructured"

    def _import_bindings(self, statement: Node) -> list[tuple[str, Binding]]:
        source = statement.child_by_field_name("source")
        specifier = string_literal(self._source, source) if source is not None else None
        whole_type = any(child.type == "type" for child in statement.children)
        found: list[tuple[str, Binding]] = []
        for clause in statement.named_children:
            if clause.type == "import_require_clause":
                name = next((c for c in clause.named_children if c.type == "identifier"), None)
                if name is not None:
                    found.append((self._text(name), Binding("none", reason="require")))
            if clause.type != "import_clause" or not specifier:
                continue
            for child in clause.named_children:
                if child.type == "identifier":
                    found.append(
                        (
                            self._text(child),
                            Binding(
                                "import", module=specifier, imported="default", type_only=whole_type
                            ),
                        )
                    )
                elif child.type == "namespace_import":
                    local = next((c for c in child.named_children if c.type == "identifier"), None)
                    if local is not None:
                        found.append(
                            (
                                self._text(local),
                                Binding("namespace", module=specifier, type_only=whole_type),
                            )
                        )
                elif child.type == "named_imports":
                    for spec in child.named_children:
                        name = spec.child_by_field_name("name")
                        if spec.type != "import_specifier" or name is None:
                            continue
                        alias = spec.child_by_field_name("alias")
                        typed = whole_type or any(c.type == "type" for c in spec.children)
                        found.append(
                            (
                                self._text(alias if alias is not None else name),
                                Binding(
                                    "import",
                                    module=specifier,
                                    imported=self._text(name),
                                    type_only=typed,
                                ),
                            )
                        )
        return found

    # -- the export table ------------------------------------------------------

    def _export_table(self, root: Node) -> dict[str, object]:
        """Exported name -> what it is bound to, and the modules re-exported wholesale."""
        names: dict[str, list[str]] = {}
        star: list[str] = []

        def bind(exported: str, spec: list[str]) -> None:
            # Overload signatures export one name once per signature, bound alike.
            names[exported] = spec if names.get(exported, spec) == spec else ["ambiguous"]

        for statement in root.named_children:
            if statement.type != "export_statement":
                continue
            source = statement.child_by_field_name("source")
            specifier = string_literal(self._source, source) if source is not None else None
            clause = next((c for c in statement.named_children if c.type == "export_clause"), None)
            if source is not None:
                if not specifier:
                    continue
                namespace = next(
                    (c for c in statement.named_children if c.type == "namespace_export"), None
                )
                if clause is not None:
                    for local, exported in self._export_specifiers(clause):
                        bind(exported, ["import", specifier, local])
                elif namespace is not None:
                    name = next(
                        (c for c in namespace.named_children if c.type == "identifier"), None
                    )
                    if name is not None:
                        bind(self._text(name), ["namespace", specifier])
                else:
                    star.append(specifier)
                continue
            if clause is not None:
                for local, exported in self._export_specifiers(clause):
                    bind(exported, self._spec(local))
                continue
            is_default = any(child.type == "default" for child in statement.children)
            declaration = statement.child_by_field_name("declaration")
            value = statement.child_by_field_name("value")
            if declaration is not None:
                declared = self._declared_names(declaration)
                if is_default:
                    bind("default", self._spec(declared[0]) if len(declared) == 1 else ["expr"])
                else:
                    for name in declared:
                        bind(name, self._spec(name))
            elif is_default and value is not None:
                bind(
                    "default",
                    self._spec(self._text(value)) if value.type == "identifier" else ["expr"],
                )
        return {"names": dict(sorted(names.items())), "star": sorted(set(star))}

    def _export_specifiers(self, clause: Node) -> list[tuple[str, str]]:
        pairs = []
        for spec in clause.named_children:
            name = spec.child_by_field_name("name")
            if spec.type != "export_specifier" or name is None:
                continue
            alias = spec.child_by_field_name("alias")
            pairs.append((self._text(name), self._text(alias if alias is not None else name)))
        return pairs

    def _declared_names(self, declaration: Node) -> list[str]:
        if declaration.type in {"lexical_declaration", "variable_declaration"}:
            return [
                self._text(name)
                for declarator in declaration.named_children
                if declarator.type == "variable_declarator"
                and (name := declarator.child_by_field_name("name")) is not None
                and name.type == "identifier"
            ]
        name = declaration.child_by_field_name("name")
        return [self._text(name)] if name is not None else []

    def _spec(self, local: str) -> list[str]:
        binding = self.bindings.get(local)
        if binding is None:
            return ["unknown"]
        if binding.kind == "local":
            return ["local", local, str(binding.line)]
        if binding.kind == "import":
            return ["import", binding.module or "", binding.imported or ""]
        if binding.kind == "namespace":
            return ["namespace", binding.module or ""]
        return ["none", binding.reason or ""]

    # -- scopes and uses -------------------------------------------------------

    def _visit(self, node: Node, field: str | None, parent: Node, *, typeof: bool) -> None:
        pending: list[tuple[Node, str | None, Node, bool, set[str] | None]] = [
            (node, field, parent, typeof, None)
        ]
        while pending:
            current, current_field, current_parent, current_typeof, exiting = pending.pop()
            if exiting is not None:
                for name in exiting:
                    self._depth[name] -= 1
                continue

            kind = current.type
            if kind in _SKIP:
                continue
            if kind in USE_NODES:
                self._use(current, current, current_field, current_parent, typeof=current_typeof)
                continue
            if kind == "nested_identifier":
                # `A.B`: one use of A reaching member B, read in the nested name's own context.
                parts = [
                    c
                    for c in current.named_children
                    if c.type in {"identifier", "property_identifier"}
                ]
                if parts and parts[0].type == "identifier":
                    member = self._text(parts[1]) if len(parts) > 1 else None
                    self._use(
                        parts[0],
                        current,
                        current_field,
                        current_parent,
                        typeof=current_typeof,
                        member=member,
                    )
                continue

            if kind == "export_statement":
                excluded = {"export_clause", "namespace_export", "identifier", "string"}
                for index in range(len(current.children) - 1, -1, -1):
                    child = current.children[index]
                    if child.type not in excluded:
                        pending.append(
                            (
                                child,
                                current.field_name_for_child(index),
                                current,
                                current_typeof,
                                None,
                            )
                        )
                continue

            bound = self._bound_here(current) if kind in _SCOPES else set()
            for name in bound:
                self._depth[name] += 1
            inner = current_typeof or kind == "type_query"
            if bound:
                pending.append((current, current_field, current_parent, inner, bound))
            for index in range(len(current.children) - 1, -1, -1):
                child = current.children[index]
                pending.append((child, current.field_name_for_child(index), current, inner, None))

    def _use(
        self,
        node: Node,
        context: Node,
        field: str | None,
        parent: Node,
        *,
        typeof: bool,
        member: str | None = None,
    ) -> None:
        """Record ``node`` as a use when it is one, read in ``context``'s position."""
        classified = self._classify(context, field, parent, typeof=typeof)
        if classified is None:
            return
        name = self._text(node)
        binding = self.bindings.get(name)
        if binding is None:
            return  # a local, a global, or a name this file never binds
        if self._depth[name]:
            self.rejected["shadowed"] += 1
            return
        if binding.kind == "none":
            self.rejected[binding.reason or "none"] += 1
            return
        use, form = classified
        if binding.kind == "namespace":
            member = member if member is not None else self._member(parent, field)
            if form == "receiver":
                form = "member"
            if self._namespace_callee(parent, field):
                use, form = "call", "call"
        else:
            member = None
        key = (node.start_byte, node.end_byte, node.type)
        self.sites[key] = UseSite(
            name=name,
            binding=binding,
            use=use,
            form=form,
            line=line_range(node)[0],
            member=member,
            members=tuple(self._members.get(key, ())) if member is None else (),
        )

    def _classify(
        self, node: Node, field: str | None, parent: Node, *, typeof: bool
    ) -> tuple[str, str] | None:
        """(use, form) for a name in this position, or ``None`` if it is not a use."""
        kind, above = node.type, parent.type
        if (above, field) in _NOT_A_USE or above in _NOT_A_USE_UNDER:
            return None
        if kind == "type_identifier":
            return "type", "type"
        if kind == "shorthand_property_identifier":
            return "value", "shorthand"
        if above in {"jsx_opening_element", "jsx_self_closing_element"} and field == "name":
            # A lower-case element is intrinsic (`<div>`), never a binding.
            return ("value", "jsx") if self._text(node)[:1].isupper() else None
        if typeof:
            return "type", "typeof"
        if (above, field) == ("nested_type_identifier", "module"):
            return "type", "type"
        if (above, field) == ("call_expression", "function"):
            return "call", "call"
        if (above, field) == ("new_expression", "constructor"):
            return "value", "new"
        if (above, field) == ("member_expression", "object"):
            grand = parent.parent
            if grand is not None and grand.type in {
                "jsx_opening_element",
                "jsx_self_closing_element",
            }:
                return "value", "jsx"
            return "value", "receiver"
        return "value", _FORMS.get(above, "read")

    def _member(self, parent: Node, field: str | None) -> str | None:
        if (parent.type, field) == ("member_expression", "object"):
            prop = parent.child_by_field_name("property")
            if prop is not None and prop.type == "property_identifier":
                return self._text(prop)
        if (parent.type, field) == ("nested_type_identifier", "module"):
            name = parent.child_by_field_name("name")
            return self._text(name) if name is not None else None
        return None

    @staticmethod
    def _namespace_callee(parent: Node, field: str | None) -> bool:
        """``ns.f(...)``: the call graph records it, under the same declaration."""
        if (parent.type, field) != ("member_expression", "object"):
            return False
        grand = parent.parent
        callee = grand.child_by_field_name("function") if grand is not None else None
        return (
            grand is not None
            and grand.type == "call_expression"
            and callee is not None
            and (callee.start_byte, callee.end_byte) == (parent.start_byte, parent.end_byte)
        )

    def _bound_here(self, node: Node) -> set[str]:
        """Names ``node`` binds for everything inside it (see the module docstring)."""
        kind = node.type
        names: set[str] = set()
        if kind in _NAMED_EXPRESSIONS:
            name = node.child_by_field_name("name")
            if name is not None:
                names.add(self._text(name))
        if kind in _GENERIC:
            parameters = node.child_by_field_name("type_parameters")
            for parameter in parameters.named_children if parameters is not None else ():
                name = parameter.child_by_field_name("name")
                if name is not None:
                    names.add(self._text(name))
        if kind in _FUNCTIONS:
            parameters = node.child_by_field_name("parameters")
            for parameter in parameters.named_children if parameters is not None else ():
                pattern = parameter.child_by_field_name("pattern")
                names.update(self._pattern_names(pattern if pattern is not None else parameter))
            single = node.child_by_field_name("parameter")
            if single is not None:
                names.update(self._pattern_names(single))
            body = node.child_by_field_name("body")
            if body is not None:
                names.update(self._hoisted(body))
        if kind in _BLOCKS:
            names.update(self._block_names(node))
        elif kind == "for_statement":
            initializer = node.child_by_field_name("initializer")
            if initializer is not None:
                names.update(self._block_names_of(initializer))
        elif kind == "for_in_statement":
            if node.child_by_field_name("kind") is not None:
                left = node.child_by_field_name("left")
                if left is not None:
                    names.update(self._pattern_names(left))
        elif kind == "catch_clause":
            parameter = node.child_by_field_name("parameter")
            if parameter is not None:
                names.update(self._pattern_names(parameter))
        elif kind == "conditional_type":
            names.update(
                self._text(n)
                for inferred in _descendants(node, "infer_type")
                for n in inferred.named_children
                if n.type == "type_identifier"
            )
        elif kind == "index_signature":
            for clause in node.named_children:
                name = (
                    clause.child_by_field_name("name")
                    if clause.type == "mapped_type_clause"
                    else None
                )
                if name is not None:
                    names.add(self._text(name))
        return names

    def _block_names(self, block: Node) -> set[str]:
        names: set[str] = set()
        for statement in block.named_children:
            if statement.type in {"switch_case", "switch_default"}:
                for inner in statement.named_children:
                    names.update(self._block_names_of(inner))
            else:
                names.update(self._block_names_of(statement))
        return names

    def _block_names_of(self, statement: Node) -> set[str]:
        """Names one statement declares in the block that holds it."""
        names: set[str] = set()
        for node in _module_declarations(statement):
            if node.type in {"lexical_declaration", "variable_declaration"}:
                for declarator in node.named_children:
                    name = declarator.child_by_field_name("name")
                    if declarator.type == "variable_declarator" and name is not None:
                        names.update(self._pattern_names(name))
            else:
                name = node.child_by_field_name("name")
                if name is not None:
                    names.add(self._text(name).split(".", 1)[0])
        return names

    def _hoisted(self, body: Node) -> set[str]:
        """Every ``var`` in a function body, outside nested functions: it binds throughout."""
        names: set[str] = set()
        pending = [body]
        while pending:
            node = pending.pop()
            for child in node.named_children:
                if child.type in _FUNCTIONS or child.type in _CLASSES:
                    continue
                if child.type == "variable_declaration":
                    for declarator in child.named_children:
                        name = declarator.child_by_field_name("name")
                        if declarator.type == "variable_declarator" and name is not None:
                            names.update(self._pattern_names(name))
                pending.append(child)
        return names

    def _pattern_names(self, pattern: Node) -> set[str]:
        """Every name a binding pattern binds; default values are uses, not bindings."""
        kind = pattern.type
        if kind in {"identifier", "shorthand_property_identifier_pattern"}:
            return {self._text(pattern)}
        if kind in {"assignment_pattern", "object_assignment_pattern"}:
            left = pattern.child_by_field_name("left")
            return self._pattern_names(left) if left is not None else set()
        if kind == "pair_pattern":
            value = pattern.child_by_field_name("value")
            return self._pattern_names(value) if value is not None else set()
        if kind in {"object_pattern", "array_pattern", "rest_pattern"}:
            return {name for child in pattern.named_children for name in self._pattern_names(child)}
        if kind in {"required_parameter", "optional_parameter"}:
            inner = pattern.child_by_field_name("pattern")
            return self._pattern_names(inner) if inner is not None else set()
        return set()


#: Forms read from the node directly around a value use.
_FORMS = {
    "arguments": "argument",
    "template_substitution": "template",
    "spread_element": "spread",
    "decorator": "decorator",
}


def _module_declarations(statement: Node) -> list[Node]:
    """The declaration nodes a statement contributes to the scope that holds it."""
    kind = statement.type
    if kind == "export_statement":
        declaration = statement.child_by_field_name("declaration")
        return _module_declarations(declaration) if declaration is not None else []
    if kind == "ambient_declaration":
        return [node for child in statement.named_children for node in _module_declarations(child)]
    if kind == "expression_statement":
        return [child for child in statement.named_children if child.type == "internal_module"]
    if kind in _DECLARED or kind in {
        _SIGNATURE,
        "internal_module",
        "lexical_declaration",
        "variable_declaration",
    }:
        return [statement]
    return []


def _descendants(node: Node, kind: str) -> list[Node]:
    found, pending = [], [node]
    while pending:
        current = pending.pop()
        for child in current.named_children:
            if child.type == kind:
                found.append(child)
            pending.append(child)
    return found


def use_references(
    raw: list[tuple[str | None, UseSite]],
    symbols: list[ParsedSymbol],
    exports: dict[str, object],
    rejected: Counter[str] | None = None,
) -> list[ParsedReference]:
    """The file's ``USES`` references: attributed uses, merged, plus its export table.

    ``raw`` holds each use with the declaration the walker attributed it to (``None``
    for the file). A declaration whose qualified name more than one definition carries
    cannot be told apart from the others, so its uses are the file's -- except the
    overload case, where the name belongs to the implementation. A use of a name by
    the declaration that is that name, or by one of its own members, is containment
    rather than a use and is dropped. Repeats of one (declaration, target, use) are
    one reference at the first occurrence, with the count kept.
    """
    # A field owns no use, so it never makes a declaration's name ambiguous (DF3b).
    counted = Counter(
        symbol.qualified_name for symbol in symbols if symbol.meta.get("kind") != FIELD
    )
    signatures = Counter(
        symbol.qualified_name for symbol in symbols if symbol.meta.get("kind") == "declaration"
    )
    ambiguous = {
        name for name, count in counted.items() if count > 1 and count - signatures[name] != 1
    }
    merged: dict[tuple[str | None, str, str], tuple[UseSite, int]] = {}
    members: dict[tuple[str | None, str, str], dict[str, tuple[int, str]]] = {}
    for source, site in raw:
        owner = None if source in ambiguous else source
        if (
            site.binding.kind == "local"
            and owner is not None
            and (owner == site.name or owner.startswith(site.name + "."))
        ):
            if rejected is not None:
                rejected["self"] += 1
            continue
        key = (owner, site.target_key, site.use)
        first, count = merged.get(key, (site, 0))
        merged[key] = (first, count + 1)
        seen = members.setdefault(key, {})
        for name, line, access in site.members:
            if name not in seen or (line, access) < seen[name]:
                seen[name] = (line, access)

    references: list[ParsedReference] = []
    for key, (site, count) in merged.items():
        owner, target, _ = key
        binding = site.binding
        meta = {
            "use": site.use,
            "form": site.form,
            "binding": binding.kind,
            "local": site.name,
            "occurrences": str(count),
        }
        if members.get(key):
            # The model's fields this declaration reads or sets through this use (DF3b):
            # resolved with the use, never copied into its own edge's record.
            listed = sorted(members[key].items(), key=lambda kv: (kv[1], kv[0]))
            meta.update(member_meta([[name, line, access] for name, (line, access) in listed]))
        if binding.kind != "local":
            meta["module"] = binding.module or ""
            imported = site.member if binding.kind == "namespace" else binding.imported
            if imported:
                meta["imported"] = imported
            if binding.type_only:
                meta["type_only"] = "true"
        references.append(
            ParsedReference(
                edge_type=EdgeType.USES,
                target_kind=EntityType.SYMBOL,
                target_key=target,
                line_start=site.line,
                line_end=site.line,
                source_symbol=owner,
                meta=meta,
            )
        )
    if exports.get("names") or exports.get("star"):
        references.append(
            ParsedReference(
                edge_type=EdgeType.USES,
                target_kind=EntityType.SYMBOL,
                target_key=USES_EXPORT_TABLE,
                line_start=1,
                line_end=1,
                meta={USES_EXPORTS: json.dumps(exports, sort_keys=True, separators=(",", ":"))},
            )
        )
    return references


# -- model fields (DF3b) --------------------------------------------------------

#: Functions with a body whose parameters and return type a field use is read from.
_FIELD_FUNCTIONS = frozenset(
    {
        "function_declaration",
        "generator_function_declaration",
        "function_expression",
        "function",
        "generator_function",
        "arrow_function",
        "method_definition",
    }
)
_TYPE_PARAMETER_HOLDERS = frozenset(
    {
        *_FIELD_FUNCTIONS,
        "class_declaration",
        "abstract_class_declaration",
        "interface_declaration",
        "type_alias_declaration",
        "function_signature",
        "method_signature",
    }
)
#: (parent kind) under which an identifier named like a parameter binds or writes it.
_BINDING_PARENTS = frozenset(
    {
        "required_parameter",
        "optional_parameter",
        "rest_pattern",
        "array_pattern",
        "object_pattern",
        "pair_pattern",
        "object_assignment_pattern",
        "assignment_pattern",
        "catch_clause",
    }
)

_ASSIGNING = frozenset(
    {"assignment_expression", "augmented_assignment_expression", "for_in_statement"}
)

_Key = tuple[int, int, str]


def field_members(source: bytes, root: Node) -> dict[_Key, list[tuple[str, int, str]]]:
    """The model fields each type annotation's one named type is read or set through.

    Keyed by that type name node, where its use is recorded. Three places name one type
    (``T``, ``T | null``, ``T | undefined``; a return type also ``Promise<T>``), never a
    function's own type parameter:

    * a parameter of a function with a body: ``p.f`` and ``p?.f`` read ``f`` when nothing
      in the function binds or writes ``p`` again, as does ``const {f} = p``; a destructured
      parameter ``({f, g: h}: T)`` reads each key;
    * a function's return type: each identifier key of an object literal it returns;
    * a variable's annotation: each identifier key of the object literal it is given.

    The keys are written members, never matched by name: whether ``T`` resolves to one
    declaration owning such a field is its use's to decide.
    """
    found: dict[_Key, list[tuple[str, int, str]]] = {}

    def add(node: Node | None, name: str, line: int, access: str) -> None:
        if node is not None:
            found.setdefault((node.start_byte, node.end_byte, node.type), []).append(
                (name, line, access)
            )

    for node in _walk(root):
        kind = node.type
        if kind in _FIELD_FUNCTIONS:
            generic = _type_parameters(source, node)
            parameters = node.child_by_field_name("parameters")
            for parameter in parameters.named_children if parameters is not None else ():
                if parameter.type not in {"required_parameter", "optional_parameter"}:
                    continue
                pattern = parameter.child_by_field_name("pattern")
                named = _annotated_type(source, parameter.child_by_field_name("type"))
                if pattern is None or named is None or node_text(source, named) in generic:
                    continue
                if pattern.type == "object_pattern":
                    for key, line in _pattern_keys(source, pattern):
                        add(named, key, line, "parameter")
                    continue
                if pattern.type != "identifier":
                    continue
                name = node_text(source, pattern)
                if _rebinds(source, node, parameter, name):
                    continue
                for inner in _walk(node):
                    if inner.type == "member_expression":
                        target = inner.child_by_field_name("object")
                        prop = inner.child_by_field_name("property")
                        if (
                            target is not None
                            and target.type == "identifier"
                            and node_text(source, target) == name
                            and prop is not None
                            and prop.type == "property_identifier"
                        ):
                            add(named, node_text(source, prop), line_range(inner)[0], "parameter")
                    elif inner.type == "variable_declarator":
                        left = inner.child_by_field_name("name")
                        value = inner.child_by_field_name("value")
                        if (
                            left is not None
                            and left.type == "object_pattern"
                            and value is not None
                            and value.type == "identifier"
                            and node_text(source, value) == name
                        ):
                            for key, line in _pattern_keys(source, left):
                                add(named, key, line, "parameter")
            returned = _returned_type(source, node)
            if returned is not None and node_text(source, returned) not in generic:
                body = node.child_by_field_name("body")
                expression = body is not None and body.type != "statement_block"
                literal = _unwrap(body) if expression else None
                if literal is not None and literal.type == "object":
                    for key, line in _object_keys(source, literal):
                        add(returned, key, line, "construction")
        elif kind == "return_statement":
            argument = _unwrap(next((c for c in node.named_children if c.type != "comment"), None))
            if argument is None or argument.type != "object":
                continue
            owner = node.parent
            while owner is not None and owner.type not in _FIELD_FUNCTIONS:
                owner = owner.parent
            if owner is None:
                continue
            returned = _returned_type(source, owner)
            if returned is not None and node_text(source, returned) not in _type_parameters(
                source, owner
            ):
                for key, line in _object_keys(source, argument):
                    add(returned, key, line, "construction")
        elif kind == "variable_declarator":
            named = _annotated_type(source, node.child_by_field_name("type"))
            value = _unwrap(node.child_by_field_name("value"))
            if (
                named is not None
                and value is not None
                and value.type == "object"
                and node_text(source, named) not in _type_parameters(source, node)
            ):
                for key, line in _object_keys(source, value):
                    add(named, key, line, "construction")
    return found


def _walk(node: Node) -> list[Node]:
    found, pending = [], [node]
    while pending:
        current = pending.pop()
        found.append(current)
        pending.extend(reversed(current.children))
    return found


def _annotated_type(source: bytes, annotation: Node | None) -> Node | None:
    """The type name node of a ``: T`` annotation naming one type, else ``None``."""
    if annotation is None:
        return None
    inner = [c for c in annotation.named_children if c.type != "comment"]
    return _named_type(source, inner[0]) if len(inner) == 1 else None


def _named_type(source: bytes, node: Node) -> Node | None:
    if node.type == "type_identifier":
        return node
    if node.type == "union_type":
        parts = _union(node)
        named = [
            p
            for p in parts
            if not (
                p.type in {"literal_type", "predefined_type"}
                and node_text(source, p) in {"null", "undefined"}
            )
        ]
        return named[0] if len(named) == 1 and named[0].type == "type_identifier" else None
    return None


def _union(node: Node) -> list[Node]:
    parts: list[Node] = []
    for child in node.named_children:
        parts.extend(_union(child) if child.type == "union_type" else [child])
    return parts


def _returned_type(source: bytes, function: Node) -> Node | None:
    """The type name node of a return type naming one type, or ``Promise<T>``'s."""
    annotation = function.child_by_field_name("return_type")
    if annotation is None:
        return None
    inner = [c for c in annotation.named_children if c.type != "comment"]
    if len(inner) != 1:
        return None
    node = inner[0]
    if node.type == "generic_type":
        name = node.child_by_field_name("name")
        arguments = node.child_by_field_name("type_arguments")
        listed = [c for c in (arguments.named_children if arguments else []) if c.type != "comment"]
        if name is not None and node_text(source, name) == "Promise" and len(listed) == 1:
            return _named_type(source, listed[0])
        return None
    return _named_type(source, node)


def _pattern_keys(source: bytes, pattern: Node) -> list[tuple[str, int]]:
    keys: list[tuple[str, int]] = []
    for child in pattern.named_children:
        if child.type == "shorthand_property_identifier_pattern":
            keys.append((node_text(source, child), line_range(child)[0]))
        elif child.type == "pair_pattern":
            key = child.child_by_field_name("key")
            if key is not None and key.type == "property_identifier":
                keys.append((node_text(source, key), line_range(key)[0]))
        elif child.type == "object_assignment_pattern":
            left = child.child_by_field_name("left")
            if left is not None and left.type == "shorthand_property_identifier_pattern":
                keys.append((node_text(source, left), line_range(left)[0]))
    return keys


def _object_keys(source: bytes, literal: Node) -> list[tuple[str, int]]:
    keys: list[tuple[str, int]] = []
    for child in literal.named_children:
        if child.type == "pair":
            key = child.child_by_field_name("key")
            if key is not None and key.type == "property_identifier":
                keys.append((node_text(source, key), line_range(key)[0]))
        elif child.type == "shorthand_property_identifier":
            keys.append((node_text(source, child), line_range(child)[0]))
    return keys


def _unwrap(node: Node | None) -> Node | None:
    while node is not None and node.type == "parenthesized_expression":
        inner = [c for c in node.named_children if c.type != "comment"]
        node = inner[0] if len(inner) == 1 else None
    return node


def _rebinds(source: bytes, scope: Node, parameter: Node, name: str) -> bool:
    """Whether anything in ``scope`` but ``parameter`` binds or writes ``name``."""
    for node in _walk(scope):
        if node.start_byte >= parameter.start_byte and node.end_byte <= parameter.end_byte:
            continue
        if node.type not in {"identifier", "shorthand_property_identifier_pattern"}:
            continue
        if node_text(source, node) != name or node.parent is None:
            continue
        parent = node.parent
        above = parent.type
        if above in _BINDING_PARENTS or above == "update_expression":
            return True
        if above == "variable_declarator" and parent.child_by_field_name("name") == node:
            return True
        if (
            above
            in {
                "function_declaration",
                "generator_function_declaration",
                "class_declaration",
                "function_expression",
                "class",
            }
            and parent.child_by_field_name("name") == node
        ):
            return True
        if above in _ASSIGNING and parent.child_by_field_name("left") == node:
            return True
        if above == "arrow_function" and parent.child_by_field_name("parameter") == node:
            return True
    return False


def _type_parameters(source: bytes, node: Node) -> set[str]:
    """The type parameters ``node`` and every declaration around it bind."""
    names: set[str] = set()
    current: Node | None = node
    while current is not None:
        if current.type in _TYPE_PARAMETER_HOLDERS:
            params = current.child_by_field_name("type_parameters")
            for child in params.named_children if params is not None else ():
                name = child.child_by_field_name("name")
                if name is not None:
                    names.add(node_text(source, name))
        current = current.parent
    return names
