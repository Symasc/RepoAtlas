"""SQL parser (persistence): migrations' schema objects, and the predicates of embedded SQL.

Reads ``.sql`` files with sqlglot, pinned exactly (THIRD_PARTY_NOTICES.md), under its
PostgreSQL dialect. Nothing is executed or connected to, and no other file is read. A
file's dialect is not stated in the file: the global pass decides it from the project the
file belongs to (:mod:`repoatlas.graph.prisma_schema`) and refuses what these facts say
when the project's database is not PostgreSQL. The constructs read here are PostgreSQL's
own syntax, so nothing another dialect means differently is read as them.

**Symbols**, one per statement that defines or changes a schema object, at its lines:

* ``CREATE TYPE t AS ENUM (...)``: a ``class`` ``t`` and a symbol ``t.v`` for each value;
* ``ALTER TYPE t ADD VALUE 'v'`` / ``RENAME VALUE 'a' TO 'b'`` / ``RENAME TO u``: a
  ``class`` ``t`` for the statement, and the value it adds or renames to as ``t.v``. sqlglot
  leaves these statements opaque; a bounded grammar over its tokens reads exactly these
  three forms, and any other ``ALTER TYPE`` is recorded as ``other``;
* ``DROP TYPE t``: a ``class`` ``t``;
* a table definition, and ``ALTER TABLE t ADD COLUMN``: a ``class`` ``t`` and a symbol
  ``t.c`` for each column, with its type and default as written.

``meta["sql"]`` says which element a symbol is. A name is schema-qualified as written
(``alerts.alert_type``); an unquoted identifier is folded to lower case, as PostgreSQL
folds it. A value containing a ``.`` is recorded in its type's ``values`` but is no symbol
of its own. A column typed by a type this file creates exactly once ``REFERENCES`` it.

**Embedded SQL.** :func:`sql_predicates` reads one statement's text, for the code and
configuration readers: the tables it names and its ``=``, ``<>`` and ``IN`` comparisons of a
column with string literals.

**Bounds.** A file over :data:`MAX_TOKENS` tokens is refused; a statement sqlglot cannot
read is skipped, never echoed. sqlglot's own log is silenced: it quotes the source.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from sqlglot import Token, TokenType, exp
from sqlglot.dialects.postgres import Postgres

from repoatlas.models.parsing import (
    ENUM_MEMBER,
    PARSE_BOUND_EXCEEDED,
    PARSE_FAILED,
    EdgeType,
    EntityType,
    ParsedFile,
    ParseDiagnostic,
    ParsedReference,
    ParsedSymbol,
    Severity,
)

#: The most tokens one file, or one embedded statement, is read up to.
MAX_TOKENS = 200_000
#: The longest embedded statement :func:`sql_predicates` reads.
MAX_EMBEDDED = 64 * 1024

# sqlglot logs a statement it falls back on with the statement's text. RepoAtlas never
# echoes source, so its log goes nowhere.
_LOG = logging.getLogger("sqlglot")
_LOG.addHandler(logging.NullHandler())
_LOG.propagate = False

_DIALECT = Postgres()


class SqlParser:
    """SQL files, read with sqlglot's PostgreSQL dialect."""

    name = "sql"
    version = "1"
    language = "SQL"

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() == ".sql"

    def parse(self, path: Path, content: str) -> ParsedFile:
        try:
            tokens = _DIALECT.tokenize(content)
        except Exception:
            return self._file(
                diagnostics=(ParseDiagnostic(severity=Severity.ERROR, message=PARSE_FAILED),)
            )
        if len(tokens) > MAX_TOKENS:
            return self._file(
                diagnostics=(
                    ParseDiagnostic(severity=Severity.ERROR, message=PARSE_BOUND_EXCEEDED),
                )
            )
        reader = _Reader()
        for statement in _statements(tokens):
            try:
                reader.statement(statement, content)
            except Exception:  # noqa: S112 - one statement sqlglot cannot read; the rest count
                continue
        return self._file(symbols=tuple(reader.symbols), references=tuple(reader.references()))

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


def _statements(tokens: Sequence[Token]) -> list[list[Token]]:
    found: list[list[Token]] = [[]]
    for token in tokens:
        if token.token_type is TokenType.SEMICOLON:
            found.append([])
        else:
            found[-1].append(token)
    return [s for s in found if s]


def _name(identifier: exp.Expr | None) -> str | None:
    if not isinstance(identifier, exp.Identifier):
        return None
    text = str(identifier.this)
    return text if identifier.quoted else text.lower()


def _qualified(table: exp.Expr | None) -> str | None:
    """``schema.name`` of a ``Table`` (or of a ``Schema`` around one), as written."""
    if isinstance(table, exp.Schema):
        table = table.this
    if not isinstance(table, exp.Table):
        return None
    name = _name(table.this)
    if name is None:
        return None
    schema = _name(table.args.get("db"))
    return f"{schema}.{name}" if schema else name


def _token_name(tokens: Sequence[Token], at: int) -> tuple[str | None, int]:
    """A possibly schema-qualified name starting at ``tokens[at]``, and where it ends."""
    parts: list[str] = []
    while at < len(tokens) and tokens[at].token_type in {TokenType.IDENTIFIER, TokenType.VAR}:
        token = tokens[at]
        parts.append(token.text if token.token_type is TokenType.IDENTIFIER else token.text.lower())
        at += 1
        if at < len(tokens) and tokens[at].token_type is TokenType.DOT:
            at += 1
            continue
        break
    return (".".join(parts) if parts else None), at


def _word(token: Token | None, *words: str) -> bool:
    return token is not None and token.text.upper() in words


@dataclass(slots=True)
class _Reader:
    symbols: list[ParsedSymbol] = field(default_factory=list)
    #: (column, type name, line): a column typed by a named type.
    _typed: list[tuple[str, str, int]] = field(default_factory=list)

    def statement(self, tokens: list[Token], content: str) -> None:
        first, last = tokens[0].line, tokens[-1].line
        head = [t.text.upper() for t in tokens[:3]]
        if head[:2] == ["ALTER", "TYPE"]:
            self._alter_type(tokens, first, last)
            return
        [parsed] = [e for e in _DIALECT.parser().parse(tokens, content) if e is not None] or [None]
        if isinstance(parsed, exp.Create):
            kind = str(parsed.args.get("kind") or "").upper()
            if kind == "TYPE":
                self._create_type(parsed, tokens, first, last)
            elif kind == "TABLE":
                self._create_table(parsed, tokens, first, last)
        elif isinstance(parsed, exp.Drop) and str(parsed.args.get("kind") or "").upper() == "TYPE":
            for table in parsed.args.get("tables") or [parsed.this]:
                name = _qualified(table)
                if name:
                    self._class(name, first, last, {"sql": "drop-type"}, f"DROP TYPE {name}")
        elif (
            isinstance(parsed, exp.Alter) and str(parsed.args.get("kind") or "").upper() == "TABLE"
        ):
            name = _qualified(parsed.this)
            columns = [a for a in parsed.args.get("actions") or [] if isinstance(a, exp.ColumnDef)]
            if name and columns:
                self._class(name, first, last, {"sql": "alter-table"}, f"ALTER TABLE {name}")
                for column in columns:
                    self._column(name, column, tokens, first, added=True)

    # -- types -------------------------------------------------------------------------

    def _create_type(self, parsed: exp.Create, tokens: list[Token], first: int, last: int) -> None:
        name = _qualified(parsed.this)
        body = parsed.args.get("expression")
        if name is None or not (isinstance(body, exp.DataType) and body.is_type("enum")):
            return
        values = [str(v.this) for v in body.expressions if isinstance(v, exp.Literal)]
        meta = {"sql": "enum-type", "values": json.dumps(values)}
        self._class(name, first, last, meta, f"CREATE TYPE {name} AS ENUM")
        strings = [t for t in tokens if t.token_type is TokenType.STRING]
        for value, token in zip(values, strings, strict=False):
            self._value(name, value, token.line)

    def _alter_type(self, tokens: list[Token], first: int, last: int) -> None:
        name, at = _token_name(tokens, 2)
        if name is None:
            return
        rest = tokens[at:]
        words = [t.text.upper() for t in rest]
        strings = [t for t in rest if t.token_type is TokenType.STRING]
        meta: dict[str, str] = {"sql": "alter-type", "action": "other"}
        added: tuple[str, int] | None = None
        if words[:2] == ["ADD", "VALUE"] and strings:
            meta.update(action="add-value", value=strings[0].text)
            if len(strings) > 1 and any(w in {"BEFORE", "AFTER"} for w in words):
                position = next(w for w in words if w in {"BEFORE", "AFTER"})
                meta[position.lower()] = strings[1].text
            added = (strings[0].text, strings[0].line)
        elif words[:2] == ["RENAME", "VALUE"] and len(strings) == 2 and "TO" in words:
            meta.update(action="rename-value", **{"from": strings[0].text, "to": strings[1].text})
            added = (strings[1].text, strings[1].line)
        elif words[:2] == ["RENAME", "TO"]:
            renamed, _ = _token_name(rest, 2)
            if renamed:
                meta.update(action="rename", to=renamed)
        self._class(name, first, last, meta, f"ALTER TYPE {name}")
        if added is not None:
            self._value(name, added[0], added[1], meta["action"])

    def _value(self, enum: str, value: str, line: int, action: str | None = None) -> None:
        if "." in value or not value:
            return
        meta = {"sql": "enum-value", "kind": ENUM_MEMBER, "value": value}
        if action == "rename-value":
            meta["renamed"] = "true"
        elif action == "add-value":
            meta["added"] = "true"
        self.symbols.append(
            ParsedSymbol(
                kind=EntityType.SYMBOL,
                name=value,
                qualified_name=f"{enum}.{value}",
                line_start=line,
                line_end=line,
                signature=f"'{value}'",
                parent=enum,
                meta=meta,
            )
        )

    # -- tables ------------------------------------------------------------------------

    def _create_table(self, parsed: exp.Create, tokens: list[Token], first: int, last: int) -> None:
        name = _qualified(parsed.this)
        schema = parsed.this
        if name is None or not isinstance(schema, exp.Schema):
            return
        self._class(name, first, last, {"sql": "table"}, f"table {name}")
        for column in schema.expressions:
            if isinstance(column, exp.ColumnDef):
                self._column(name, column, tokens, first)

    def _column(
        self,
        table: str,
        column: exp.ColumnDef,
        tokens: list[Token],
        first: int,
        *,
        added: bool = False,
    ) -> None:
        name = _name(column.this)
        if not name or "." in name:
            return
        kind = column.args.get("kind")
        type_name = _type_name(kind)
        meta = {"sql": "column", "type": type_name or ""}
        if added:
            meta["added"] = "true"
        for constraint in column.constraints:
            default = constraint.kind
            if isinstance(default, exp.DefaultColumnConstraint):
                value = default.this
                if isinstance(value, exp.Literal):
                    meta["default"] = str(value.this)
        line = next(
            (
                t.line
                for t in tokens
                if t.token_type in {TokenType.IDENTIFIER, TokenType.VAR}
                and (t.text if t.token_type is TokenType.IDENTIFIER else t.text.lower()) == name
            ),
            first,
        )
        self.symbols.append(
            ParsedSymbol(
                kind=EntityType.SYMBOL,
                name=name,
                qualified_name=f"{table}.{name}",
                line_start=line,
                line_end=line,
                signature=f"{name} {type_name or ''}".strip(),
                parent=table,
                meta=meta,
            )
        )
        if type_name and _user_defined(kind):
            self._typed.append((f"{table}.{name}", type_name, line))

    def _class(
        self, name: str, first: int, last: int, meta: dict[str, str], signature: str
    ) -> None:
        self.symbols.append(
            ParsedSymbol(
                kind=EntityType.CLASS,
                name=name.rsplit(".", 1)[-1],
                qualified_name=name,
                line_start=first,
                line_end=last,
                signature=signature,
                meta=meta,
            )
        )

    def references(self) -> list[ParsedReference]:
        created = Counter(
            s.qualified_name for s in self.symbols if s.meta.get("sql") == "enum-type"
        )
        holders = Counter(s.qualified_name for s in self.symbols)
        return [
            ParsedReference(
                edge_type=EdgeType.REFERENCES,
                target_kind=EntityType.CLASS,
                target_key=type_name,
                line_start=line,
                line_end=line,
                source_symbol=column,
            )
            for column, type_name, line in self._typed
            if created[type_name] == 1 and holders[type_name] == 1
        ]


def _user_defined(kind: exp.Expr | None) -> bool:
    return isinstance(kind, exp.DataType) and kind.this == exp.DataType.Type.USERDEFINED


def _type_name(kind: exp.Expr | None) -> str | None:
    """A column type as written: a built-in's name, or a user-defined type's qualified name."""
    if not isinstance(kind, exp.DataType):
        return None
    if _user_defined(kind):
        named = kind.args.get("kind")
        parts: list[str | None] = []
        while isinstance(named, exp.Dot):
            parts.insert(0, _name(named.expression))
            named = named.this
        parts.insert(0, _name(named))
        return ".".join(p for p in parts if p) if all(parts) else None
    return kind.sql(dialect="postgres").lower()


# -- embedded SQL ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Predicate:
    """A comparison of one column with string literals, in one statement."""

    #: The table the column belongs to, when the statement says so; ``None`` when it names
    #: several tables and the column is not qualified by one.
    table: str | None
    column: str
    #: ``=``, ``<>`` or ``in`` (``not in`` is ``<>`` over several values); ``set`` for an
    #: ``UPDATE``'s assignment.
    op: str
    values: tuple[str, ...]


def sql_predicates(text: str) -> tuple[Predicate, ...] | None:
    """The comparisons of columns with string literals in one statement, or ``None`` when
    the text is not one statement sqlglot reads."""
    if len(text) > MAX_EMBEDDED:
        return None
    try:
        tokens = _DIALECT.tokenize(text)
        if len(tokens) > MAX_TOKENS:
            return None
        parsed = [e for e in _DIALECT.parser().parse(tokens, text) if e is not None]
    except Exception:
        return None
    if len(parsed) != 1 or isinstance(parsed[0], exp.Command):
        return None
    found: list[Predicate] = []
    for node in parsed[0].find_all(exp.EQ, exp.NEQ, exp.In):
        if isinstance(node, exp.In):
            column, values = node.this, node.expressions
            op = "<>" if isinstance(node.parent, exp.Not) else "in"
        else:
            column, value = node.this, node.expression
            if isinstance(value, exp.Column) and not isinstance(column, exp.Column):
                column, value = value, column
            values = [value]
            op = "=" if isinstance(node, exp.EQ) else "<>"
            if isinstance(node.parent, exp.Update):
                op = "set"
        literals = [_literal(v) for v in values]
        if not isinstance(column, exp.Column) or not literals or None in literals:
            continue
        name = _name(column.this)
        if not name:
            continue
        found.append(
            Predicate(
                table=_owner(column, node),
                column=name,
                op=op,
                values=tuple(v for v in literals if v is not None),
            )
        )
    return tuple(found)


def _literal(node: exp.Expr) -> str | None:
    while isinstance(node, exp.Cast | exp.Paren):
        node = node.this
    if isinstance(node, exp.Literal) and node.is_string:
        return str(node.this)
    return None


def _owner(column: exp.Column, node: exp.Expr) -> str | None:
    """The table a column belongs to: by its qualifier, else the one table of its query."""
    scope = node.find_ancestor(exp.Select, exp.Update, exp.Delete)
    if scope is None:
        return None
    tables: dict[str, str] = {}
    for table in _scope_tables(scope):
        name = _qualified(table)
        if name is None:
            continue
        tables[_name(table.args.get("alias") and table.args["alias"].this) or name] = name
        tables.setdefault(name.rsplit(".", 1)[-1], name)
    qualifier = _name(column.args.get("table"))
    if qualifier is not None:
        return tables.get(qualifier)
    distinct = set(tables.values())
    return next(iter(distinct)) if len(distinct) == 1 else None


def _scope_tables(scope: exp.Expr) -> list[exp.Table]:
    """The tables a query reads directly: its ``FROM``, joins and target, not subqueries'."""
    found: list[exp.Table] = []
    candidates: list[exp.Expr] = []
    if isinstance(scope, exp.Select):
        source = scope.args.get("from_") or scope.args.get("from")
        if source is not None:
            candidates.append(source.this)
        candidates += [j.this for j in scope.args.get("joins") or []]
    else:
        candidates.append(scope.this)
    for candidate in candidates:
        if isinstance(candidate, exp.Table):
            found.append(candidate)
    return found
