"""Prisma schema parser (persistence).

Reads ``.prisma`` files: the Prisma Schema Language, a small block-structured language
with no maintained Python parser. This is a bounded tokenizer and recursive-descent parser
for the documented grammar -- blocks of ``datasource``, ``generator``, ``model``, ``view``,
``type`` and ``enum``; fields and enum values one per line, each with ``@attribute(...)``
calls; block attributes ``@@attribute(...)``; configuration ``key = value``; ``//``, ``///``
and ``/* */`` comments. Like every
parser here it is a pure function of the path and the content: nothing is generated,
migrated or connected to, and no other file is read.

**Symbols.** Each block is a ``class`` named as written; each field and enum value a
``symbol`` under it (``Listing.phase``, ``ListingPhase.draft``). ``meta["prisma"]`` says
which element a symbol is, and ``meta["db_name"]`` the name the database uses for it: the
``@map`` / ``@@map`` argument when there is one, else the name. A constraint's ``map:``
argument (``@@index([...], map: "...")``, ``@id(map: "...")``) names the constraint, not a
table or column, and is not read as one. A field also records its type, its modifier
(``?`` or ``[]``) and its ``@default`` as written. An enum value is an ``enum-member`` whose
``value`` is its name -- what Prisma Client spells it -- reached through its enum, never by
its bare name. A ``datasource`` records its ``provider`` and the environment variable its
``url`` reads (``env("X")``); a ``generator`` its ``provider`` and ``output``, as written.

**References.** A field typed by a model, view, composite type or enum this file defines
exactly once is a ``REFERENCES`` edge to it. A schema split over several files is a
preview feature this parser does not join; a type defined elsewhere is no reference.

**Bounds.** A file over :data:`MAX_TOKENS` tokens, or nesting brackets deeper than
:data:`MAX_DEPTH`, is refused, as is a syntax error, with RepoAtlas's own wording.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from repoatlas.models.parsing import (
    ENUM_MEMBER,
    PARSE_BOUND_EXCEEDED,
    PARSE_FAILED,
    SYNTAX_ERROR,
    EdgeType,
    EntityType,
    ParsedFile,
    ParseDiagnostic,
    ParsedReference,
    ParsedSymbol,
    Severity,
)
from repoatlas.parsers.marks import line_count

#: The most tokens one file is read up to.
MAX_TOKENS = 50_000
#: The deepest nesting of brackets in an attribute's arguments.
MAX_DEPTH = 32

#: The blocks with fields, and the ``meta["prisma"]`` role each has.
_FIELD_BLOCKS = {"model": "model", "view": "view", "type": "type"}
#: The blocks of ``key = value`` configuration.
_CONFIG_BLOCKS = frozenset({"datasource", "generator"})
#: The scalar types: naming one is no reference.
_SCALARS = frozenset(
    {
        "String",
        "Boolean",
        "Int",
        "BigInt",
        "Float",
        "Decimal",
        "DateTime",
        "Json",
        "Bytes",
        "Unsupported",
    }
)


class _SyntaxError(Exception):
    def __init__(self, line: int) -> None:
        super().__init__(line)
        self.line = line


class _Bound(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _Token:
    kind: str  # name, string, number, punct, newline, eof
    text: str
    line: int


def _tokens(content: str) -> list[_Token]:
    """The file's tokens, comments dropped and newlines kept: fields end at a newline."""
    found: list[_Token] = []
    line, index, size = 1, 0, len(content)
    while index < size:
        char = content[index]
        if char == "\n":
            found.append(_Token("newline", "\n", line))
            line += 1
            index += 1
        elif char in " \t\r\f\v":
            index += 1
        elif content.startswith("//", index):
            end = content.find("\n", index)
            index = size if end == -1 else end
        elif content.startswith("/*", index):
            end = content.find("*/", index + 2)
            if end == -1:
                raise _SyntaxError(line)
            line += content.count("\n", index, end)
            index = end + 2
        elif char == '"':
            start, index, text = line, index + 1, []
            while True:
                if index >= size or content[index] == "\n":
                    raise _SyntaxError(start)
                if content[index] == "\\" and index + 1 < size:
                    text.append(content[index : index + 2])
                    index += 2
                    continue
                if content[index] == '"':
                    index += 1
                    break
                text.append(content[index])
                index += 1
            found.append(_Token("string", _unescape("".join(text)), start))
        elif char.isalpha() or char == "_":
            end = index + 1
            while end < size and (content[end].isalnum() or content[end] == "_"):
                end += 1
            found.append(_Token("name", content[index:end], line))
            index = end
        elif char.isdigit() or (char == "-" and index + 1 < size and content[index + 1].isdigit()):
            end = index + 1
            while end < size and (content[end].isalnum() or content[end] in "._"):
                end += 1
            found.append(_Token("number", content[index:end], line))
            index = end
        elif char in "{}()[],:=?!.@":
            found.append(_Token("punct", char, line))
            index += 1
        else:
            raise _SyntaxError(line)
        if len(found) > MAX_TOKENS:
            raise _Bound
    found.append(_Token("eof", "", line))
    return found


def _unescape(text: str) -> str:
    try:
        return json.loads(f'"{text}"')
    except ValueError:
        return text


@dataclass(slots=True)
class _Attribute:
    name: str
    #: Positional arguments, then named ones, each as its value (see :meth:`_Parser.value`).
    positional: list[object] = field(default_factory=list)
    named: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class _Entry:
    name: str
    line: int
    type_name: str | None = None
    modifier: str = ""
    attributes: list[_Attribute] = field(default_factory=list)
    value: object = None


@dataclass(slots=True)
class _Block:
    keyword: str
    name: str
    line_start: int
    line_end: int = 0
    entries: list[_Entry] = field(default_factory=list)
    attributes: list[_Attribute] = field(default_factory=list)


class _Call:
    """A function call as an argument value: ``env("X")``, ``now()``, ``dbgenerated("…")``."""

    __slots__ = ("arguments", "name")

    def __init__(self, name: str, arguments: list[object]) -> None:
        self.name = name
        self.arguments = arguments


class _Parser:
    def __init__(self, tokens: list[_Token]) -> None:
        self._tokens = tokens
        self._at = 0

    @property
    def _peek(self) -> _Token:
        return self._tokens[self._at]

    def _next(self) -> _Token:
        token = self._tokens[self._at]
        if token.kind != "eof":
            self._at += 1
        return token

    def _expect(self, kind: str, text: str | None = None) -> _Token:
        token = self._next()
        if token.kind != kind or (text is not None and token.text != text):
            raise _SyntaxError(token.line)
        return token

    def _is(self, text: str) -> bool:
        return self._peek.kind == "punct" and self._peek.text == text

    def _newlines(self) -> None:
        while self._peek.kind == "newline":
            self._next()

    def blocks(self) -> list[_Block]:
        found: list[_Block] = []
        self._newlines()
        while self._peek.kind != "eof":
            keyword = self._expect("name")
            name = self._expect("name")
            block = _Block(keyword=keyword.text, name=name.text, line_start=keyword.line)
            self._newlines()
            self._expect("punct", "{")
            while True:
                self._newlines()
                if self._is("}"):
                    block.line_end = self._next().line
                    break
                if self._peek.kind == "eof":
                    raise _SyntaxError(self._peek.line)
                if self._is("@"):
                    block.attributes.append(self.attribute(block_level=True))
                    continue
                block.entries.append(self.entry(block.keyword))
            found.append(block)
            self._newlines()
        return found

    def entry(self, keyword: str) -> _Entry:
        name = self._expect("name")
        entry = _Entry(name=name.text, line=name.line)
        if keyword in _CONFIG_BLOCKS:
            self._expect("punct", "=")
            entry.value = self.value(0)
        elif keyword != "enum":
            type_name = self._expect("name").text
            if self._is("("):  # Unsupported("…")
                self._next()
                self.arguments(1)
            entry.type_name = type_name
            if self._is("?"):
                self._next()
                entry.modifier = "?"
            elif self._is("["):
                self._next()
                self._expect("punct", "]")
                entry.modifier = "[]"
        while self._is("@"):
            entry.attributes.append(self.attribute(block_level=False))
        if self._peek.kind not in {"newline", "eof"} and not self._is("}"):
            raise _SyntaxError(self._peek.line)
        return entry

    def attribute(self, *, block_level: bool) -> _Attribute:
        self._expect("punct", "@")
        if self._is("@"):
            if not block_level:
                raise _SyntaxError(self._peek.line)
            self._next()
        elif block_level:
            raise _SyntaxError(self._peek.line)
        parts = [self._expect("name").text]
        while self._is("."):
            self._next()
            parts.append(self._expect("name").text)
        attribute = _Attribute(name=".".join(parts))
        if self._is("("):
            self._next()
            for item in self.arguments(1):
                if isinstance(item, tuple):
                    attribute.named[item[0]] = item[1]
                else:
                    attribute.positional.append(item)
        return attribute

    def arguments(self, depth: int) -> list[object]:
        """The arguments up to the closing ``)``, the ``(`` already read."""
        if depth > MAX_DEPTH:
            raise _Bound
        found: list[object] = []
        self._newlines()
        while not self._is(")"):
            if self._peek.kind == "name" and self._tokens[self._at + 1].text == ":":
                key = self._next().text
                self._next()
                found.append((key, self.value(depth)))
            else:
                found.append(self.value(depth))
            self._newlines()
            if self._is(","):
                self._next()
                self._newlines()
            elif not self._is(")"):
                raise _SyntaxError(self._peek.line)
        self._next()
        return found

    def value(self, depth: int) -> object:
        if depth > MAX_DEPTH:
            raise _Bound
        token = self._next()
        if token.kind == "string":
            return token.text
        if token.kind == "number":
            return token.text
        if token.kind == "name":
            name = token.text
            while self._is("."):
                self._next()
                name += "." + self._expect("name").text
            if self._is("("):
                self._next()
                return _Call(name, self.arguments(depth + 1))
            return name
        if token.kind == "punct" and token.text == "[":
            items: list[object] = []
            self._newlines()
            while not self._is("]"):
                items.append(self.value(depth + 1))
                if self._is("("):  # [field(sort: Desc)]
                    self._next()
                    self.arguments(depth + 1)
                self._newlines()
                if self._is(","):
                    self._next()
                    self._newlines()
                elif not self._is("]"):
                    raise _SyntaxError(self._peek.line)
            self._next()
            return items
        raise _SyntaxError(token.line)


class PrismaParser:
    """Prisma schema files, read by a bounded parser of the documented grammar."""

    name = "prisma"
    version = "1"
    language = "Prisma"

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() == ".prisma"

    def parse(self, path: Path, content: str) -> ParsedFile:
        try:
            blocks = _Parser(_tokens(content)).blocks()
        except _SyntaxError as error:
            line = max(1, min(error.line, line_count(content)))
            return self._file(
                diagnostics=(
                    ParseDiagnostic(severity=Severity.ERROR, message=SYNTAX_ERROR, line=line),
                )
            )
        except _Bound:
            return self._file(
                diagnostics=(
                    ParseDiagnostic(severity=Severity.ERROR, message=PARSE_BOUND_EXCEEDED),
                )
            )
        except Exception:
            return self._file(
                diagnostics=(ParseDiagnostic(severity=Severity.ERROR, message=PARSE_FAILED),)
            )
        symbols, references = _facts(blocks)
        return self._file(symbols=tuple(symbols), references=tuple(references))

    def _file(
        self,
        *,
        symbols: tuple[ParsedSymbol, ...] = (),
        references: tuple[ParsedReference, ...] = (),
        diagnostics: tuple[ParseDiagnostic, ...] = (),
    ) -> ParsedFile:
        return ParsedFile(
            language=self.language,
            parser=self.name,
            parser_version=self.version,
            symbols=symbols,
            references=references,
            diagnostics=diagnostics,
        )


def _facts(blocks: list[_Block]) -> tuple[list[ParsedSymbol], list[ParsedReference]]:
    symbols: list[ParsedSymbol] = []
    references: list[ParsedReference] = []
    typed = Counter(b.name for b in blocks if b.keyword in _FIELD_BLOCKS or b.keyword == "enum")
    names = Counter(b.name for b in blocks)
    for block in blocks:
        meta: dict[str, str] = {}
        if block.keyword in _CONFIG_BLOCKS:
            meta["prisma"] = block.keyword
            for entry in block.entries:
                if entry.name == "provider" and isinstance(entry.value, str):
                    meta["provider"] = entry.value
                elif entry.name == "output" and isinstance(entry.value, str):
                    meta["output"] = entry.value
                elif entry.name == "url" and isinstance(entry.value, _Call):
                    argument = entry.value.arguments[:1]
                    if entry.value.name == "env" and argument and isinstance(argument[0], str):
                        meta["url_env"] = argument[0]
        elif block.keyword in _FIELD_BLOCKS or block.keyword == "enum":
            meta["prisma"] = _FIELD_BLOCKS.get(block.keyword, "enum")
            meta["db_name"] = _mapped(block.attributes, "map") or block.name
        else:
            continue
        symbols.append(
            ParsedSymbol(
                kind=EntityType.CLASS,
                name=block.name,
                qualified_name=block.name,
                line_start=block.line_start,
                line_end=block.line_end or block.line_start,
                signature=f"{block.keyword} {block.name}",
                meta=meta,
            )
        )
        if block.keyword in _CONFIG_BLOCKS:
            continue
        for entry in block.entries:
            qualified = f"{block.name}.{entry.name}"
            mapped = _mapped(entry.attributes, "map") or entry.name
            if block.keyword == "enum":
                entry_meta = {
                    "prisma": "enum-value",
                    "db_name": mapped,
                    "kind": ENUM_MEMBER,
                    "value": entry.name,
                }
                signature = entry.name
            else:
                entry_meta = {
                    "prisma": "field",
                    "db_name": mapped,
                    "type": entry.type_name or "",
                    "modifier": entry.modifier,
                }
                default = next((a for a in entry.attributes if a.name == "default"), None)
                if default is not None and default.positional:
                    entry_meta["default"] = _text(default.positional[0])
                signature = f"{entry.name} {entry.type_name}{entry.modifier}"
            symbols.append(
                ParsedSymbol(
                    kind=EntityType.SYMBOL,
                    name=entry.name,
                    qualified_name=qualified,
                    line_start=entry.line,
                    line_end=entry.line,
                    signature=signature,
                    parent=block.name,
                    meta=entry_meta,
                )
            )
            named = entry.type_name
            if named and named not in _SCALARS and typed[named] == 1 and names[named] == 1:
                references.append(
                    ParsedReference(
                        edge_type=EdgeType.REFERENCES,
                        target_kind=EntityType.CLASS,
                        target_key=named,
                        line_start=entry.line,
                        line_end=entry.line,
                        source_symbol=qualified,
                    )
                )
    return symbols, references


def _mapped(attributes: list[_Attribute], name: str) -> str | None:
    """The database name a ``@map`` / ``@@map`` states: its first argument, or ``name:``."""
    for attribute in attributes:
        if attribute.name != name:
            continue
        value = attribute.positional[0] if attribute.positional else attribute.named.get("name")
        if isinstance(value, str):
            return value
    return None


def _text(value: object) -> str:
    if isinstance(value, _Call):
        return f"{value.name}({', '.join(_text(a) for a in value.arguments)})"
    if isinstance(value, list):
        return "[" + ", ".join(_text(v) for v in value) + "]"
    if isinstance(value, tuple):
        return f"{value[0]}: {_text(value[1])}"
    return str(value)
