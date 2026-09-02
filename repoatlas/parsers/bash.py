"""Bash parser using tree-sitter (V1_SPEC §7.3).

Bash is **first-class code**, not a config format: shell scripts are usually where
a deployment actually decides what runs and with which environment. This parser
therefore extracts the same shape of facts as a programming-language parser —
definitions, invocations, imports (``source``) and environment access.

**Scripts are never executed.** Everything here is a static walk of the syntax
tree; a command's effect is inferred from nothing beyond its written form.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from repoatlas.models.parsing import (
    EdgeType,
    EntityType,
    ParsedFile,
    ParseDiagnostic,
    ParsedReference,
    ParsedSymbol,
    Severity,
)
from repoatlas.parsers.treesitter import (
    GrammarUnavailable,
    has_error,
    line_range,
    node_text,
    parse_tree,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tree_sitter import Node

_SUFFIXES = frozenset({".sh", ".bash", ".zsh", ".ksh"})
_SHEBANGS = ("#!/bin/sh", "#!/bin/bash", "#!/usr/bin/env bash", "#!/usr/bin/env sh", "#!/bin/zsh")

#: An environment variable, by convention: upper snake case, at least two chars.
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]+$")

_SOURCE_COMMANDS = frozenset({"source", "."})
_EXPORT_KEYWORDS = frozenset({"export", "declare", "typeset", "readonly", "local"})
_SALT_COMMANDS = frozenset({"salt", "salt-call", "salt-ssh", "salt-run"})
_SALT_STATE_FUNCTIONS = frozenset({"state.apply", "state.sls", "state.highstate", "state.sls_id"})

#: Interpreters whose script argument is itself a repository file worth linking.
_INTERPRETERS: dict[str, str] = {
    "python": ".py",
    "python2": ".py",
    "python3": ".py",
    "ruby": ".rb",
    "bash": ".sh",
    "sh": ".sh",
    "zsh": ".sh",
}

_SCRIPT_SUFFIXES = frozenset({".sh", ".bash", ".py", ".rb", ".zsh"})

#: Commands that run *another* command, transparently. Recording only the wrapper
#: would state that the script executes ``env``, which hides the relationship the
#: question "what does this script run?" is actually about (V1_SPEC §7.3).
#:
#: ``sudo`` is deliberately absent: its flags consume arguments (``-u user``,
#: ``-g group``) and it accepts ``--``, so identifying the wrapped command means
#: guessing. Everything here takes only flags that stand alone.
_WRAPPER_COMMANDS = frozenset({"env", "exec", "nohup", "command"})

#: Flags a wrapper may carry without changing which word is the command. Anything
#: else — including every flag that consumes the following word — stops the
#: unwrap, because the alternative is inventing a target.
_TRANSPARENT_WRAPPER_FLAGS = frozenset(
    {"-i", "--ignore-environment", "-", "--", "-c", "-l", "-p", "-0", "--null"}
)

#: ``NAME=value`` placed before a command: POSIX puts NAME in that command's
#: environment, which is the relationship ``export NAME=value`` also creates.
_PREFIX_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)


def _looks_like_path(text: str) -> bool:
    return text.startswith(("./", "../", "/")) or (
        "/" in text and not text.startswith("-") and Path(text).suffix in _SCRIPT_SUFFIXES
    )


class _Walker:
    """Collects definitions, invocations and environment access in one pass."""

    def __init__(self, source: bytes) -> None:
        self._source = source
        self.symbols: list[ParsedSymbol] = []
        self.references: list[ParsedReference] = []
        self.exported: set[str] = set()
        self.assigned: set[str] = set()
        self._reads: list[ParsedReference] = []
        self._scope: str | None = None

    def run(self, root: Node) -> None:
        self._visit(root)

    # -- dispatch ------------------------------------------------------------

    def _visit(self, node: Node) -> None:
        kind = node.type
        if kind == "function_definition":
            self._function(node)
            return
        if kind == "declaration_command":
            self._declaration(node)
        elif kind == "variable_assignment":
            self._assignment(node, exported=False)
        elif kind == "command":
            self._command(node)
        elif kind in {"simple_expansion", "expansion"}:
            self._expansion(node)

        for child in node.children:
            self._visit(child)

    # -- definitions ---------------------------------------------------------

    def _function(self, node: Node) -> None:
        name_node = next((c for c in node.children if c.type in {"word", "command_name"}), None)
        if name_node is None:
            for child in node.children:
                self._visit(child)
            return
        name = node_text(self._source, name_node)
        start, end = line_range(node)
        self.symbols.append(
            ParsedSymbol(
                kind=EntityType.FUNCTION,
                name=name,
                qualified_name=name,
                line_start=start,
                line_end=end,
                signature=f"{name}()",
            )
        )
        previous, self._scope = self._scope, name
        for child in node.children:
            self._visit(child)
        self._scope = previous

    # -- assignments and exports ---------------------------------------------

    def _declaration(self, node: Node) -> None:
        keyword = node_text(self._source, node.children[0]) if node.children else ""
        exporting = keyword in _EXPORT_KEYWORDS and keyword != "local"
        for child in node.children:
            if child.type == "variable_assignment":
                self._assignment(child, exported=exporting)
            elif child.type == "variable_name" and exporting:
                self._record_export(node_text(self._source, child), child)

    def _assignment(self, node: Node, *, exported: bool) -> None:
        name_node = node.child_by_field_name("name") or next(
            (c for c in node.children if c.type == "variable_name"), None
        )
        if name_node is None:
            return
        name = node_text(self._source, name_node)
        self.assigned.add(name)
        if exported:
            self._record_export(name, node)

    def _record_export(self, name: str, node: Node) -> None:
        start, end = line_range(node)
        self._export(name, start, end)

    def _export(self, name: str, start: int, end: int) -> None:
        if not _ENV_NAME.match(name):
            return
        self.exported.add(name)
        self.references.append(
            ParsedReference(
                edge_type=EdgeType.EXPORTS,
                target_kind=EntityType.ENV_VAR,
                target_key=name,
                line_start=start,
                line_end=end,
                source_symbol=self._scope,
            )
        )

    # -- environment reads ---------------------------------------------------

    def _expansion(self, node: Node) -> None:
        """``$VAR`` / ``${VAR}`` — a candidate environment read."""
        name_node = next((c for c in node.children if c.type == "variable_name"), None)
        if name_node is None:
            return
        name = node_text(self._source, name_node)
        if not _ENV_NAME.match(name):
            return
        start, end = line_range(node)
        self._reads.append(
            ParsedReference(
                edge_type=EdgeType.READS,
                target_kind=EntityType.ENV_VAR,
                target_key=name,
                line_start=start,
                line_end=end,
                source_symbol=self._scope,
            )
        )

    def finish(self) -> None:
        """Keep only reads the script does not fully determine itself.

        A variable this script assigns without exporting is a local, so reading it
        back says nothing about the environment. A variable it *exports* stays a
        read, because ``export X="${X:-default}"`` genuinely consults the
        environment first — which is the common way a script takes configuration.
        """
        local_only = self.assigned - self.exported
        self.references.extend(r for r in self._reads if r.target_key not in local_only)

    # -- commands ------------------------------------------------------------

    def _command(self, node: Node) -> None:
        name_node = node.child_by_field_name("name") or next(
            (c for c in node.children if c.type == "command_name"), None
        )
        if name_node is None:
            return
        command = node_text(self._source, name_node)
        start, end = line_range(node)
        arguments = [
            node_text(self._source, child)
            for child in node.children
            if child is not name_node and child.type in {"word", "string", "concatenation"}
        ]

        if command in _SOURCE_COMMANDS:
            self._sourced(node, arguments, start, end)
            return

        # `NAME=value cmd` puts NAME in the environment of `cmd` alone. That is the
        # same relationship `export NAME=value` creates, scoped to one command, so
        # it is recorded as one (V1_SPEC §10).
        for child in node.children:
            if child.type == "variable_assignment":
                name_child = child.child_by_field_name("name")
                if name_child is not None:
                    self._export(node_text(self._source, name_child), start, end)

        if Path(command).name in _WRAPPER_COMMANDS and self._unwrapped(
            command, arguments, start, end
        ):
            return

        if self._executed_path(command, start, end):
            # A path invocation is better evidence as a script reference than as a
            # bare command name, so it is recorded once, not twice.
            return

        self.references.append(
            ParsedReference(
                edge_type=EdgeType.EXECUTES,
                target_kind=EntityType.COMMAND,
                target_key=command,
                line_start=start,
                line_end=end,
                source_symbol=self._scope,
            )
        )

        self._interpreted_script(command, arguments, start, end)
        self._salt_invocation(command, arguments, start, end)

    def _unwrapped(self, wrapper: str, arguments: list[str], start: int, end: int) -> bool:
        """Record what ``env``/``exec``/``nohup``/``command`` actually runs.

        Returns whether the wrapped command was identified. When it was not — an
        unrecognized flag, a computed target, or no command at all — nothing is
        recorded here and the caller falls back to reporting the wrapper itself,
        which is a true statement rather than a guess (V1_SPEC §2).
        """
        rest = list(arguments)
        assignments: list[tuple[str, str]] = []

        while rest:
            word = rest[0]
            if word.startswith("-"):
                if word not in _TRANSPARENT_WRAPPER_FLAGS:
                    return False
                rest.pop(0)
                continue
            match = _PREFIX_ASSIGNMENT.match(word)
            if match is None:
                break
            assignments.append((match.group(1), match.group(2)))
            rest.pop(0)

        if not rest:
            return False

        inner, inner_arguments = rest[0].strip("\"'"), rest[1:]
        if not inner or "$" in inner or "`" in inner:
            return False

        for name, _ in assignments:
            self._export(name, start, end)

        if not self._executed_path(inner, start, end):
            self.references.append(
                ParsedReference(
                    edge_type=EdgeType.EXECUTES,
                    target_kind=EntityType.COMMAND,
                    target_key=inner,
                    line_start=start,
                    line_end=end,
                    source_symbol=self._scope,
                    meta={"wrapper": wrapper},
                )
            )
            self._interpreted_script(inner, inner_arguments, start, end)
            self._salt_invocation(inner, inner_arguments, start, end)
        return True

    def _sourced(self, node: Node, arguments: list[str], start: int, end: int) -> None:
        """``source x`` / ``. x`` imports another script."""
        target = self._first_literal(node, arguments)
        if target is None:
            # A computed path cannot be resolved statically; recording a guess
            # would be inventing evidence (V1_SPEC §2).
            return
        self.references.append(
            ParsedReference(
                edge_type=EdgeType.SOURCES,
                target_kind=EntityType.SCRIPT,
                target_key=target,
                line_start=start,
                line_end=end,
                source_symbol=self._scope,
            )
        )

    def _first_literal(self, node: Node, arguments: list[str]) -> str | None:
        for argument in arguments:
            if "$" in argument or "`" in argument:
                return None
            return argument.strip("\"'")
        return None

    def _interpreted_script(self, command: str, arguments: list[str], start: int, end: int) -> None:
        """``python3 x.py`` / ``ruby x.rb`` executes a script in the tree."""
        suffix = _INTERPRETERS.get(Path(command).name)
        if suffix is None:
            return
        for argument in arguments:
            if argument.startswith("-") or "$" in argument:
                continue
            cleaned = argument.strip("\"'")
            if Path(cleaned).suffix != suffix:
                continue
            self.references.append(
                ParsedReference(
                    edge_type=EdgeType.EXECUTES,
                    target_kind=EntityType.SCRIPT,
                    target_key=cleaned,
                    line_start=start,
                    line_end=end,
                    source_symbol=self._scope,
                    meta={"interpreter": command},
                )
            )
            return

    def _salt_invocation(self, command: str, arguments: list[str], start: int, end: int) -> None:
        """``salt-call state.apply service`` references a Salt state."""
        if Path(command).name not in _SALT_COMMANDS:
            return
        words = [a.strip("\"'") for a in arguments if not a.startswith("-")]
        for index, word in enumerate(words):
            if word not in _SALT_STATE_FUNCTIONS:
                continue
            if index + 1 >= len(words):
                return
            state = words[index + 1]
            if "$" in state:
                return
            self.references.append(
                ParsedReference(
                    edge_type=EdgeType.REFERENCES,
                    target_kind=EntityType.SALT_STATE,
                    target_key=state,
                    line_start=start,
                    line_end=end,
                    source_symbol=self._scope,
                    meta={"via": word},
                )
            )
            return

    def _executed_path(self, command: str, start: int, end: int) -> bool:
        """``./deploy.sh`` executes a script rather than a ``PATH`` command."""
        if not _looks_like_path(command) or "$" in command:
            return False
        self.references.append(
            ParsedReference(
                edge_type=EdgeType.EXECUTES,
                target_kind=EntityType.SCRIPT,
                target_key=command,
                line_start=start,
                line_end=end,
                source_symbol=self._scope,
            )
        )
        return True


class BashParser:
    """Deterministic shell structure extraction."""

    name = "tree-sitter-bash"
    version = "1"
    language = "Bash"

    def supports(self, path: Path) -> bool:
        return path.suffix in _SUFFIXES

    def parse(self, path: Path, content: str) -> ParsedFile:
        try:
            tree, source = parse_tree("bash", content)
        except GrammarUnavailable as exc:
            return ParsedFile(
                language=self.language,
                parser=self.name,
                parser_version=self.version,
                diagnostics=(ParseDiagnostic(severity=Severity.ERROR, message=str(exc)),),
            )

        walker = _Walker(source)
        walker.run(tree.root_node)
        walker.finish()

        script = ParsedSymbol(
            kind=EntityType.SCRIPT,
            name=path.name,
            qualified_name=path.as_posix(),
            line_start=1,
            line_end=max(1, content.count("\n") + 1),
        )

        diagnostics = (
            (
                ParseDiagnostic(
                    severity=Severity.WARNING,
                    message="syntax errors recovered; results may be partial",
                ),
            )
            if has_error(tree)
            else ()
        )

        return ParsedFile(
            language=self.language,
            parser=self.name,
            parser_version=self.version,
            symbols=(script, *walker.symbols),
            references=tuple(walker.references),
            diagnostics=diagnostics,
        )


def looks_like_shell(content: str) -> bool:
    """Whether an extension-less file declares a shell interpreter."""
    first = content.split("\n", 1)[0].strip()
    return first.startswith(_SHEBANGS)
