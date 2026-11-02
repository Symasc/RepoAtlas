"""Where a TypeScript file says its OpenAPI document is (M7).

:class:`~repoatlas.parsers.http_oas3.SpecRoutedApps` reads ``path.join(__dirname,
'<literal>', …)`` and nothing else. :class:`SpecPathReader` reads the path expressions
a source states in more forms, each relative to the file that states it, and answers
with one of:

* :class:`Spec` -- the literal segments joined by ``/``, not normalized, as the
  ``spec`` meta of an application has always been: ``../api/openapi.yaml``;
* :class:`Parts` -- segments of which some are read from another module;
* :class:`ImportedPath` -- the whole path is a name another module exports;
* :class:`Param` -- the path is the function parameter at an index;
* :class:`Refused` -- a code that says why the source does not state a path. A
  refusal is a decision, never a guess: nothing here falls back to the process's
  working directory, to an environment setting's default, or to a name it cannot prove.

**An anchor** is where a path starts. ``__dirname`` bound nowhere in the file; a
``const`` of ``path.dirname(fileURLToPath(import.meta.url))``, under any name;
``path.dirname(__filename)``, with ``__filename`` bound nowhere or once to
``fileURLToPath(import.meta.url)``; a ``const`` of any path built from an anchor.
The source is run where it stands, or built to the same depth.

**A path** is an anchor followed by segments, in one of these forms:

* ``join(ANCHOR, …)`` or ``resolve(ANCHOR, …)`` on the ``path`` module -- a namespace,
  default or ``require`` binding of it, a named import (under any alias), or a
  module-level ``const { join } = require('path')``;
* ``${ANCHOR}/rest${SEGMENT}…`` -- the anchor, then text that begins with ``/``;
* ``ANCHOR + '/rest'``, and a left-nested ``+`` of segments;
* ``fileURLToPath(new URL('<literal>', import.meta.url))``.

**A segment** is a string literal, or a ``const`` of one, or a property of a ``const``
object literal the file only reads; in a ``join`` it is also a named import from a
relative module. A name is followed through at most :data:`_MAX_DEPTH` ``const``
bindings, each of which must end before the place that reads it starts: a name read
before its ``const`` runs is not its value.

**A whole value** that is a named import from a relative module, or a plain parameter
of a module-level function (no default, not optional, not a pattern), is answered with
:class:`ImportedPath` or :class:`Param`, also through ``const`` aliases. Any other use
is refused: as an anchor, as a segment of a template or ``+``, a parameter as a segment
at all, or the parameter of a nested function.

**Resolution is lexical** (:meth:`FetchWrappers.lookup`): a name resolves where it is
bound, and only when that is its one binding there. Nothing is read from disk, and the
reader runs only for the expressions it is asked about.

Parsed, never executed (V1_SPEC §17).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

from repoatlas.parsers.http_ts_wrappers import (
    Binding,
    Const,
    FetchWrappers,
    Global,
    Imported,
    Injected,
    Member,
    _parameters,
    arguments_of,
    is_relative,
    operand,
)
from repoatlas.parsers.http_ts_wrappers import Param as Parameter
from repoatlas.parsers.http_typescript import FUNCTION_NODES, binding_counts, static_string
from repoatlas.parsers.treesitter import node_text, string_literal, walk

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tree_sitter import Node

# -- why a path is refused ----------------------------------------------------------------

SPEC_CWD_RELATIVE = "spec-cwd-relative"
SPEC_ABSOLUTE = "spec-absolute"
SPEC_ENVIRONMENT = "spec-environment"
SPEC_DYNAMIC = "spec-dynamic"
SPEC_UNRESOLVED_NAME = "spec-unresolved-name"
SPEC_AMBIGUOUS_BINDING = "spec-ambiguous-binding"
SPEC_MUTABLE_BINDING = "spec-mutable-binding"
SPEC_USED_BEFORE_BINDING = "spec-used-before-binding"
SPEC_SHADOWED_ANCHOR = "spec-shadowed-anchor"
SPEC_DEPTH_EXCEEDED = "spec-depth-exceeded"
SPEC_IMPORT_UNSUPPORTED = "spec-import-unsupported"
SPEC_IMPORT_UNRESOLVED = "spec-import-unresolved"
SPEC_PARAMETER_UNSUPPORTED = "spec-parameter-unsupported"
SPEC_PROPAGATION_DEPTH = "spec-propagation-depth"
SPEC_PARAMETER_UNCALLED = "spec-parameter-uncalled"
SPEC_OUTSIDE_REPOSITORY = "spec-outside-repository"
SPEC_NOT_INDEXED = "spec-not-indexed"
SPEC_NOT_A_FILE = "spec-not-a-file"

# -- what is read -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Spec:
    """A path fully read in this file: its segments joined by ``/``."""

    text: str


@dataclass(frozen=True, slots=True)
class Parts:
    """Segments in order, those that are :class:`Imported` still to be read elsewhere."""

    parts: tuple[str | Imported, ...]


@dataclass(frozen=True, slots=True)
class ImportedPath:
    """The path is ``imported``, a name the relative module ``specifier`` exports."""

    specifier: str
    imported: str


@dataclass(frozen=True, slots=True)
class Param:
    """The path is the parameter at ``index`` of ``function``, a module-level function."""

    index: int
    function: Node


@dataclass(frozen=True, slots=True)
class Refused:
    """Why this source does not state a path: one of the ``SPEC_*`` codes."""

    code: str


Result = Spec | Parts | ImportedPath | Param | Refused

#: A binding of a whole module: a namespace or default import, or ``require``.
_MODULE = "*"
#: How many ``const`` names a value is followed through.
_MAX_DEPTH = 4
#: How many segments a path may have, and how many operands a ``+`` chain.
_MAX_PARTS = 32
#: How deep expressions that build a path may nest in one another.
_MAX_NEST = 32
_PATH_MODULES = frozenset({"path", "node:path"})
_URL_MODULES = frozenset({"url", "node:url"})
_JOINS = frozenset({"join", "resolve"})
_DIRNAME = "dirname"
_FILE_URL = "fileURLToPath"
#: The functions of each module a path is read through.
_OPERATIONS = {
    **dict.fromkeys(_PATH_MODULES, _JOINS | {_DIRNAME}),
    **dict.fromkeys(_URL_MODULES, frozenset({_FILE_URL})),
}
#: What a template string's content is made of.
_TEMPLATE_CONTENT = frozenset({"string_fragment", "escape_sequence", "template_substitution"})
#: What a path expression is: this file's directory, this file, or a file named from it.
_DIR = "directory"
_SELF = "self"
_FILE = "file"
#: Names bound by the module system, and what each one is when nothing binds it.
_ANCHORS = {"__dirname": _DIR, "__filename": _SELF}
#: Refusals an anchor's own binding does not change into :data:`SPEC_SHADOWED_ANCHOR`.
_KEPT = frozenset({SPEC_USED_BEFORE_BINDING, SPEC_DEPTH_EXCEEDED})
#: Operators that fall back to their right operand.
_DEFAULTING = frozenset({"||", "??"})
_DRIVE = re.compile(r"[A-Za-z]:")
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:")


@dataclass(frozen=True, slots=True)
class _Located:
    """A path expression: the segments after the file's directory, and what it names."""

    parts: tuple[str | Imported, ...]
    kind: str = _DIR


def _absolute(text: str) -> bool:
    """Whether ``text`` as a segment starts somewhere other than where it is joined."""
    return text.startswith("/") or "\\" in text or _DRIVE.match(text) is not None


def _is_directory(text: str) -> bool:
    """Whether ``text``, normalized, is the anchor directory or one above it."""
    kept: list[str] = []
    for part in text.split("/"):
        if part in {"", "."}:
            continue
        if part == ".." and kept and kept[-1] != "..":
            kept.pop()
        else:
            kept.append(part)
    return not kept or kept[-1] == ".."


def _enclosing(node: Node) -> Node | None:
    parent = node.parent
    while parent is not None:
        if parent.type in FUNCTION_NODES:
            return parent
        parent = parent.parent
    return None


def _top(node: Node) -> bool:
    """Whether ``node`` is the file, or an ``export`` directly in it."""
    if node.type == "program":
        return True
    parent = node.parent
    return node.type == "export_statement" and parent is not None and parent.type == "program"


def _ordered(value: Node, at: Node) -> bool:
    """Whether the declaration of ``value`` has ended before ``at`` starts."""
    declarator = value.parent
    return declarator is not None and declarator.end_byte <= at.start_byte


def _written(text: str | Imported | Refused) -> str | Refused:
    """``text`` as a piece of a path written out as one string, where another module's
    value cannot be read in."""
    return Refused(SPEC_IMPORT_UNSUPPORTED) if isinstance(text, Imported) else text


class SpecPathReader:
    """The path one file's source states for a specification, from the expression."""

    def __init__(
        self, source: bytes, root: Node, counts: Counter[str], wrappers: FetchWrappers
    ) -> None:
        self._source = source
        self._root = root
        self._counts = counts
        self._wrappers = wrappers
        self._scopes: dict[tuple[int, int], Counter[str]] = {}
        #: Local name → ``(module, imported name or _MODULE)``, built on first use.
        self._table: dict[str, tuple[str, str]] | None = None

    def resolve(self, node: Node) -> Result:
        """The path ``node`` states, relative to this file, or why it states none."""
        whole = self._whole(node, 0)
        if whole is not None:
            return whole
        found = self._path(node, 0, 0)
        if isinstance(found, Refused):
            return found
        if found.kind == _SELF:
            return Refused(SPEC_DYNAMIC)
        parts = tuple(part for part in found.parts if part)
        literal = [part for part in parts if isinstance(part, str)]
        if len(literal) < len(parts):
            return Parts(parts)
        text = "/".join(literal)
        if _is_directory(text):
            return Refused(SPEC_NOT_A_FILE)
        return Spec(text)

    def _whole(self, node: Node, depth: int) -> Param | ImportedPath | None:
        """``node`` as a name that is, alone, a parameter or a relative import: directly, or
        through ``const`` aliases. ``None`` for anything else, which :meth:`_path` reads."""
        node = operand(node)
        if node.type != "identifier" or node_text(self._source, node) in _ANCHORS:
            return None
        value = self._value(node, depth)
        if isinstance(value, Parameter):
            return Param(value.index, value.function)
        if isinstance(value, Imported):
            return ImportedPath(value.specifier, value.imported)
        if isinstance(value, (Refused, Global)):
            return None
        return self._whole(value, depth + 1)

    # -- paths ---------------------------------------------------------------------------

    def _path(self, node: Node, depth: int, nest: int) -> _Located | Refused:
        """``node`` as a path that starts at an anchor. ``depth`` counts the names followed
        and ``nest`` the expressions entered."""
        if nest > _MAX_NEST:
            return Refused(SPEC_DEPTH_EXCEEDED)
        node = operand(node)
        if self._environment(node):
            return Refused(SPEC_ENVIRONMENT)
        literal = self._literal(node)
        if literal is not None:
            if isinstance(literal, Refused):
                return literal
            return Refused(SPEC_ABSOLUTE if _absolute(literal) else SPEC_CWD_RELATIVE)
        if node.type == "identifier":
            return self._name(node, depth, nest)
        if node.type == "member_expression":
            value = self._node(node, depth)
            if isinstance(value, Global):
                return Refused(SPEC_UNRESOLVED_NAME)
            return value if isinstance(value, Refused) else self._path(value, depth + 1, nest + 1)
        if node.type == "call_expression":
            return self._call(node, depth, nest)
        if node.type == "template_string":
            return self._template(node, depth, nest)
        if node.type == "binary_expression":
            return self._plus(node, depth, nest)
        return Refused(SPEC_DYNAMIC)

    def _name(self, node: Node, depth: int, nest: int) -> _Located | Refused:
        """A name as a path: an anchor itself, or the path its ``const`` is."""
        name = node_text(self._source, node)
        value = self._node(node, depth)
        if isinstance(value, Global):
            if name in _ANCHORS:
                return _Located((), _ANCHORS[name])
            return Refused(SPEC_UNRESOLVED_NAME)
        found = value if isinstance(value, Refused) else self._path(value, depth + 1, nest + 1)
        if name not in _ANCHORS:
            return found
        # An anchor's name bound in the file is an anchor only when it is bound to one.
        if isinstance(found, Refused) and found.code in _KEPT:
            return found
        if isinstance(found, _Located) and found.kind == _ANCHORS[name] and not found.parts:
            return found
        return Refused(SPEC_SHADOWED_ANCHOR)

    def _call(self, node: Node, depth: int, nest: int) -> _Located | Refused:
        callee = node.child_by_field_name("function")
        callee = operand(callee) if callee is not None else None
        if callee is None:
            return Refused(SPEC_DYNAMIC)
        if self._is_cwd(callee):
            return Refused(SPEC_CWD_RELATIVE)
        arguments = arguments_of(node)
        operation = self._operation(callee)
        if arguments is None or operation is None:
            return Refused(SPEC_DYNAMIC)
        if operation in _JOINS:
            return self._join(arguments, depth, nest)
        if operation == _DIRNAME:
            return self._dirname(arguments, depth, nest)
        return self._file_url(arguments)

    def _join(self, arguments: list[Node], depth: int, nest: int) -> _Located | Refused:
        """``join(ANCHOR, segment, …)``, or ``resolve``."""
        if len(arguments) > _MAX_PARTS + 1:
            return Refused(SPEC_DEPTH_EXCEEDED)
        if not arguments:
            return Refused(SPEC_CWD_RELATIVE)
        base = self._path(arguments[0], depth, nest + 1)
        if isinstance(base, Refused):
            return base
        if base.kind != _DIR:
            return Refused(SPEC_DYNAMIC)
        parts = list(base.parts)
        for argument in arguments[1:]:
            segment = self._segment(argument, depth)
            if isinstance(segment, Refused):
                return segment
            parts.append(segment)
        if len(parts) > _MAX_PARTS:
            return Refused(SPEC_DEPTH_EXCEEDED)
        return _Located(tuple(parts))

    def _dirname(self, arguments: list[Node], depth: int, nest: int) -> _Located | Refused:
        """``dirname`` of this file's own path: the anchor."""
        if len(arguments) != 1:
            return Refused(SPEC_DYNAMIC)
        inner = self._path(arguments[0], depth, nest + 1)
        if isinstance(inner, Refused):
            return inner
        if inner.kind != _SELF:
            return Refused(SPEC_DYNAMIC)
        return _Located(())

    def _file_url(self, arguments: list[Node]) -> _Located | Refused:
        """``fileURLToPath(import.meta.url)``, or of ``new URL('<literal>', import.meta.url)``."""
        if len(arguments) != 1:
            return Refused(SPEC_DYNAMIC)
        argument = operand(arguments[0])
        if self._is_meta_url(argument):
            return _Located((), _SELF)
        if argument.type != "new_expression":
            return Refused(SPEC_DYNAMIC)
        constructor = argument.child_by_field_name("constructor")
        inner = arguments_of(argument)
        if (
            constructor is None
            or inner is None
            or len(inner) != 2
            or node_text(self._source, operand(constructor)) != "URL"
            or not isinstance(self._wrappers.lookup("URL", constructor), Global)
            or not self._is_meta_url(inner[1])
        ):
            return Refused(SPEC_DYNAMIC)
        literal = self._literal(operand(inner[0]))
        if literal is None:
            return Refused(SPEC_DYNAMIC)
        if isinstance(literal, Refused):
            return literal
        # A URL is a path only when it is a plain relative one: no scheme, no query, and
        # no escapes, which ``fileURLToPath`` would decode.
        if _absolute(literal) or _SCHEME.match(literal):
            return Refused(SPEC_ABSOLUTE)
        if any(char in literal for char in "%?#"):
            return Refused(SPEC_DYNAMIC)
        return _Located((literal,), _FILE)

    def _template(self, node: Node, depth: int, nest: int) -> _Located | Refused:
        """``${ANCHOR}/rest${SEGMENT}…``."""
        content = [child for child in node.children if child.type in _TEMPLATE_CONTENT]
        if len(content) > 2 * _MAX_PARTS + 1:
            return Refused(SPEC_DEPTH_EXCEEDED)
        if not content or any(child.type == "escape_sequence" for child in content):
            return Refused(SPEC_DYNAMIC)
        if content[0].type != "template_substitution":
            text = node_text(self._source, content[0])
            return Refused(SPEC_ABSOLUTE if _absolute(text) else SPEC_CWD_RELATIVE)
        first = self._substituted(content[0])
        if first is None:
            return Refused(SPEC_DYNAMIC)
        anchor = self._path(first, depth, nest + 1)
        if isinstance(anchor, Refused):
            return anchor
        if anchor.kind != _DIR:
            return Refused(SPEC_DYNAMIC)
        pieces: list[str] = []
        for index, child in enumerate(content[1:]):
            if child.type == "string_fragment":
                pieces.append(node_text(self._source, child))
                continue
            expression = self._substituted(child)
            if index == 0 or expression is None:  # glued to the anchor, or unreadable
                return Refused(SPEC_DYNAMIC)
            segment = _written(self._segment(expression, depth))
            if isinstance(segment, Refused):
                return segment
            pieces.append(segment)
        return self._glue(anchor, pieces)

    def _plus(self, node: Node, depth: int, nest: int) -> _Located | Refused:
        """``ANCHOR + '/rest'``, and a left-nested ``+`` of segments."""
        leaves = self._leaves(node)
        if leaves is None:
            return Refused(SPEC_DEPTH_EXCEEDED)
        if len(leaves) < 2:
            return Refused(SPEC_DYNAMIC)
        anchor = self._path(leaves[0], depth, nest + 1)
        if isinstance(anchor, Refused):
            return anchor
        if anchor.kind != _DIR:
            return Refused(SPEC_DYNAMIC)
        pieces: list[str] = []
        for index, leaf in enumerate(leaves[1:]):
            literal = self._literal(leaf)
            if isinstance(literal, Refused):
                return literal
            if literal is not None:
                pieces.append(literal)
                continue
            # The first piece after the anchor carries the ``/``; any later one is a segment.
            text = _written(self._text(leaf, depth) if index == 0 else self._segment(leaf, depth))
            if isinstance(text, Refused):
                return text
            pieces.append(text)
        return self._glue(anchor, pieces)

    def _glue(self, anchor: _Located, pieces: list[str]) -> _Located | Refused:
        """``anchor`` and the text after it, which begins with ``/``, as one more segment."""
        rest = "".join(pieces)
        if not rest:
            return anchor
        if not rest.startswith("/"):
            return Refused(SPEC_DYNAMIC)
        part = rest[1:]
        if _absolute(part):
            return Refused(SPEC_ABSOLUTE)
        if len(anchor.parts) >= _MAX_PARTS:
            return Refused(SPEC_DEPTH_EXCEEDED)
        return _Located((*anchor.parts, part))

    def _leaves(self, node: Node) -> list[Node] | None:
        """The operands of a ``+`` chain, left to right, or ``None`` when it is too long.

        Iterative: a chain a source writes thousands of terms long is a tree as deep.
        """
        leaves: list[Node] = []
        pending = [node]
        plus = 0
        while pending:
            current = operand(pending.pop())
            operator = current.child_by_field_name("operator")
            left = current.child_by_field_name("left")
            right = current.child_by_field_name("right")
            if (
                current.type == "binary_expression"
                and operator is not None
                and node_text(self._source, operator) == "+"
                and left is not None
                and right is not None
            ):
                plus += 1
                if plus > _MAX_PARTS:
                    return None
                pending.append(right)
                pending.append(left)
            else:
                leaves.append(current)
        return leaves

    # -- segments ------------------------------------------------------------------------

    def _segment(self, node: Node, depth: int) -> str | Imported | Refused:
        """A segment joined onto an anchor: it never starts anywhere else."""
        text = self._text(node, depth)
        if isinstance(text, str) and _absolute(text):
            return Refused(SPEC_ABSOLUTE)
        return text

    def _text(self, node: Node, depth: int) -> str | Imported | Refused:
        """A string literal, or the one a ``const`` name or read-only property is; a named
        import from a relative module is read in another file."""
        node = operand(node)
        if self._environment(node):
            return Refused(SPEC_ENVIRONMENT)
        literal = self._literal(node)
        if literal is not None:
            return literal
        if node.type not in {"identifier", "member_expression"}:
            return Refused(SPEC_DYNAMIC)
        value = self._value(node, depth)
        if isinstance(value, Global):
            return Refused(SPEC_UNRESOLVED_NAME)
        if isinstance(value, Imported):
            return value
        if isinstance(value, Parameter):
            return Refused(SPEC_PARAMETER_UNSUPPORTED)
        return value if isinstance(value, Refused) else self._text(value, depth + 1)

    def _literal(self, node: Node) -> str | Refused | None:
        """A string literal's value; a refusal for one with a backslash, which is no segment."""
        text = static_string(self._source, node)
        if text is not None:
            return text
        if node.type in {"string", "template_string"}:
            raw = string_literal(self._source, node)
            if raw is not None and "\\" in raw:
                return Refused(SPEC_ABSOLUTE)
        return None

    def _substituted(self, substitution: Node) -> Node | None:
        """The expression of a ``${…}`` of a template."""
        inner = [child for child in substitution.named_children if child.type != "comment"]
        return inner[0] if len(inner) == 1 else None

    # -- names ---------------------------------------------------------------------------

    def _value(self, node: Node, depth: int) -> Node | Global | Refused | Imported | Parameter:
        """The expression a name or a property of a ``const`` object is, one name further.

        :class:`Global` when nothing in the file binds the name. A name that is a named
        import from a relative module is its :class:`Imported`; a plain parameter of a
        module-level function is its :class:`Parameter`. Any other import or parameter is
        refused.
        """
        if depth >= _MAX_DEPTH:
            return Refused(SPEC_DEPTH_EXCEEDED)
        if node.type == "member_expression":
            return self._property(node, depth)
        found = self._binding(node)
        if isinstance(found, (Refused, Global)):
            return found
        if isinstance(found, Const):
            if not _ordered(found.value, node):
                return Refused(SPEC_USED_BEFORE_BINDING)
            return found.value
        if isinstance(found, Parameter):
            return found if self._plain(found) else Refused(SPEC_PARAMETER_UNSUPPORTED)
        if isinstance(found, Injected):
            return Refused(SPEC_PARAMETER_UNSUPPORTED)
        if isinstance(found, Imported):
            # ``import { default as X }`` is a default import by another name
            if is_relative(found.specifier) and found.imported != "default":
                return found
            return Refused(SPEC_IMPORT_UNSUPPORTED)
        return Refused(SPEC_DYNAMIC)

    def _node(self, node: Node, depth: int) -> Node | Global | Refused:
        """:meth:`_value`, for a place that reads the expression itself: an import or a
        parameter is not one."""
        value = self._value(node, depth)
        if isinstance(value, Imported):
            return Refused(SPEC_IMPORT_UNSUPPORTED)
        if isinstance(value, Parameter):
            return Refused(SPEC_PARAMETER_UNSUPPORTED)
        return value

    def _plain(self, found: Parameter) -> bool:
        """Whether a parameter is a plain one of a module-level function: required, with no
        default, so that what a caller passes is its value. A function with a ``this``
        parameter is none: that one is counted in the index, but no caller passes it."""
        if not self._declared(found.function):
            return False
        parameters = _parameters(found.function)
        if any(self._this(parameter) for parameter in parameters):
            return False
        parameter = parameters[found.index]
        default = parameter.child_by_field_name("value")
        return parameter.type != "optional_parameter" and default is None

    def _this(self, parameter: Node) -> bool:
        pattern = parameter.child_by_field_name("pattern")
        return pattern is not None and (
            pattern.type == "this" or node_text(self._source, pattern) == "this"
        )

    def _declared(self, function: Node) -> bool:
        """Whether a function is declared at the top of the file, where its callers are
        calls of its name: a function declaration, or a function value of a ``const`` --
        either one exported or not. A method or a callback has no name to call."""
        parent = function.parent
        if function.type in {"function_declaration", "generator_function_declaration"}:
            return parent is not None and _top(parent)
        if function.type not in {"arrow_function", "function_expression", "function"}:
            return False
        if parent is None or parent.type != "variable_declarator":
            return False
        if parent.child_by_field_name("value") != function:
            return False
        declaration = parent.parent
        if declaration is None or declaration.type != "lexical_declaration":
            return False
        kind = declaration.child_by_field_name("kind")
        return (
            kind is not None
            and node_text(self._source, kind) == "const"
            and declaration.parent is not None
            and _top(declaration.parent)
        )

    def _property(self, node: Node, depth: int) -> Node | Refused:
        """``C.key`` for ``const C = { key: … }`` the file only ever reads."""
        owner = node.child_by_field_name("object")
        owner = operand(owner) if owner is not None else None
        if owner is None or owner.type != "identifier":
            return Refused(SPEC_DYNAMIC)
        held = self._node(owner, depth)
        if isinstance(held, Refused):
            return held
        if isinstance(held, Global):
            return Refused(SPEC_UNRESOLVED_NAME)
        if operand(held).type != "object":
            return Refused(SPEC_DYNAMIC)
        found = self._wrappers._const_property(node)
        if found is not None:
            return found
        if not self._wrappers._only_read(node_text(self._source, owner)):
            return Refused(SPEC_MUTABLE_BINDING)
        return Refused(SPEC_DYNAMIC)

    def _binding(self, node: Node) -> Binding | Refused:
        name = node_text(self._source, node)
        found = self._wrappers.lookup(name, node)
        return found if found is not None else Refused(self._unbound(name, node))

    def _unbound(self, name: str, node: Node) -> str:
        """Why a name has no one binding where ``node`` reads it."""
        scope = self._scope(name, node)
        counts = self._counts if scope is None else self._scope_counts(scope)
        if (
            scope is not None
            and counts[name] > 1
            and isinstance(self._wrappers._direct(scope, name), (Parameter, Injected))
        ):  # a parameter that is bound again is reassigned, so it is no argument
            return SPEC_PARAMETER_UNSUPPORTED
        if counts[name] > 1:
            return SPEC_AMBIGUOUS_BINDING
        if scope is None and name in self._imports():
            return SPEC_IMPORT_UNSUPPORTED
        for declarator in walk(self._root if scope is None else scope):
            if declarator.type != "variable_declarator":
                continue
            pattern = declarator.child_by_field_name("name")
            declaration = declarator.parent
            if (
                pattern is None
                or declaration is None
                or pattern.type != "identifier"
                or node_text(self._source, pattern) != name
            ):
                continue
            kind = declaration.child_by_field_name("kind")
            if declaration.type == "variable_declaration" or (
                kind is not None and node_text(self._source, kind) != "const"
            ):
                return SPEC_MUTABLE_BINDING
        return SPEC_DYNAMIC

    def _scope(self, name: str, node: Node) -> Node | None:
        """The innermost function that binds ``name`` around ``node``; ``None`` for the file."""
        scope = _enclosing(node)
        while scope is not None:
            if self._scope_counts(scope)[name]:
                return scope
            scope = _enclosing(scope)
        return None

    def _scope_counts(self, scope: Node) -> Counter[str]:
        key = (scope.start_byte, scope.end_byte)
        found = self._scopes.get(key)
        if found is None:
            found = binding_counts(self._source, scope)
            self._scopes[key] = found
        return found

    def _environment(self, node: Node) -> bool:
        """``process.env.NAME``, alone or with a default: a setting, never its default."""
        node = operand(node)
        while node.type == "binary_expression":
            operator = node.child_by_field_name("operator")
            left = node.child_by_field_name("left")
            if operator is None or left is None:
                return False
            if node_text(self._source, operator) not in _DEFAULTING:
                return False
            node = operand(left)
        return self._wrappers._setting(node)

    # -- the path and url modules ------------------------------------------------------------

    def _operation(self, callee: Node) -> str | None:
        """``join``, ``resolve``, ``dirname`` or ``fileURLToPath``, when ``callee`` is one."""
        module: str | None = None
        name: str | None = None
        if callee.type == "identifier":
            bound = self._module_of(callee)
            if bound is not None and bound[1] != _MODULE:
                module, name = bound
        elif callee.type == "member_expression":
            owner = callee.child_by_field_name("object")
            prop = callee.child_by_field_name("property")
            bound = self._module_of(operand(owner)) if owner is not None else None
            if bound is not None and bound[1] == _MODULE and prop is not None:
                module, name = bound[0], node_text(self._source, prop)
        if module is None or name is None or name not in _OPERATIONS.get(module, frozenset()):
            return None
        return name

    def _module_of(self, node: Node) -> tuple[str, str] | None:
        """``(module, imported name or _MODULE)`` for a name that is a module, or a member of
        one, at its binding: an import, or ``require``."""
        if node.type != "identifier":
            return None
        name = node_text(self._source, node)
        imported = self._imports().get(name)
        if imported is not None:
            return imported if self._wrappers.module_binding(name, node) else None
        found = self._wrappers.lookup(name, node)
        if not isinstance(found, (Const, Member)):
            return None
        module = self._required(found.value)
        if module is None or not _ordered(found.value, node):
            return None
        return (module, _MODULE) if isinstance(found, Const) else (module, found.member)

    def _required(self, value: Node) -> str | None:
        """The module ``require('<module>')`` loads."""
        value = operand(value)
        callee = value.child_by_field_name("function") if value.type == "call_expression" else None
        if callee is None or node_text(self._source, callee) != "require":
            return None
        arguments = arguments_of(value) or []
        if len(arguments) != 1 or self._counts["require"]:
            return None
        return string_literal(self._source, arguments[0])

    def _imports(self) -> dict[str, tuple[str, str]]:
        """Each name an import of the file binds, with its module and imported name."""
        if self._table is None:
            table: dict[str, tuple[str, str]] = {}
            for statement in self._root.named_children:
                if statement.type == "import_statement":
                    self._import(statement, table)
            self._table = table
        return self._table

    def _import(self, statement: Node, table: dict[str, tuple[str, str]]) -> None:
        if any(child.type == "type" for child in statement.children):
            return
        source_node = statement.child_by_field_name("source")
        module = string_literal(self._source, source_node) if source_node is not None else None
        clause = next((c for c in statement.children if c.type == "import_clause"), None)
        if module is None or clause is None:
            return
        for child in clause.named_children:
            if child.type == "identifier":
                table[node_text(self._source, child)] = (module, _MODULE)
            elif child.type == "namespace_import":
                name = next((c for c in child.named_children if c.type == "identifier"), None)
                if name is not None:
                    table[node_text(self._source, name)] = (module, _MODULE)
            elif child.type == "named_imports":
                for specifier in child.named_children:
                    if specifier.type != "import_specifier" or any(
                        c.type == "type" for c in specifier.children
                    ):
                        continue
                    imported = specifier.child_by_field_name("name")
                    alias = specifier.child_by_field_name("alias") or imported
                    if imported is not None and alias is not None:
                        table[node_text(self._source, alias)] = (
                            module,
                            node_text(self._source, imported),
                        )

    def _is_cwd(self, callee: Node) -> bool:
        """``process.cwd``: the directory the process was started in."""
        if callee.type != "member_expression":
            return False
        owner = callee.child_by_field_name("object")
        prop = callee.child_by_field_name("property")
        return (
            owner is not None
            and prop is not None
            and operand(owner).type == "identifier"
            and node_text(self._source, operand(owner)) == "process"
            and node_text(self._source, prop) == "cwd"
            and isinstance(self._wrappers.lookup("process", owner), Global)
        )

    def _is_meta_url(self, node: Node) -> bool:
        """``import.meta.url``: where this file was loaded from."""
        node = operand(node)
        if node.type != "member_expression":
            return False
        owner = node.child_by_field_name("object")
        prop = node.child_by_field_name("property")
        return (
            owner is not None
            and prop is not None
            and operand(owner).type == "meta_property"
            and node_text(self._source, operand(owner)) == "import.meta"
            and node_text(self._source, prop) == "url"
        )
