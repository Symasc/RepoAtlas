"""Bash parser using tree-sitter (V1_SPEC §7.3).

Bash is **first-class code**, not a config format: shell scripts are usually where
a deployment actually decides what runs and with which environment. This parser
therefore extracts the same shape of facts as a programming-language parser —
definitions, invocations, imports (``source``) and environment access.

**Scripts are never executed.** Everything here is a static walk of the syntax
tree; a command's effect is inferred from nothing beyond its written form.

Which makes command position the hard part, and the invariant this module exists
to hold (ARCHITECTURE.md §5):

    A Bash ``EXECUTES`` edge names only a target explicitly recoverable from
    static source syntax. Variable expansion or runtime command construction
    never becomes a deterministic target.

``$CMD`` is not a program called ``$CMD``. It is a program whose name this file
does not contain — and the honest record of that is **nothing**, not a lower
confidence on a fabricated node. Absence of a deterministic target means
unresolved, not low-confidence invention: a half-confident ``cmd:$CMD`` is the
same invented entity with a smaller number on it, and retrieval would still
surface it as a thing that exists.

Proof comes from the syntax tree rather than from pattern-matching the text.
:func:`_literal_text` returns a node's value only when every part of it is
spelled out in the source, and ``None`` the moment an expansion, a command
substitution or an arithmetic evaluation appears — which is also what strips
quoting from targets, since quotes are shell syntax and never part of a
filename.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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
from repoatlas.parsers.marks import whole_file_span
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

#: Flags after which an interpreter's next argument is a *program text*, not a
#: path. ``bash -c "./x.sh"`` does not execute the file ``./x.sh`` in any sense
#: RepoAtlas can verify: the string is shell source, re-parsed at run time with
#: expansion, and reading a path out of it would be a second, unwritten shell
#: parser (V1_SPEC §7.3). The same holds for ``python -c`` and ``ruby -e``.
_INTERPRETER_CODE_FLAGS = frozenset({"-c", "--command", "-e", "--eval"})

#: ``NAME=value`` placed before a command: POSIX puts NAME in that command's
#: environment, which is the relationship ``export NAME=value`` also creates.
_PREFIX_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)

#: Children of a ``command`` node that are not arguments. Redirections in
#: particular carry filenames that are not the command's operands: ``./a.sh >
#: out.txt`` writes to ``out.txt``, it does not pass it to anything.
_NON_ARGUMENT = frozenset(
    {
        "command_name",
        "variable_assignment",
        "file_redirect",
        "heredoc_redirect",
        "herestring_redirect",
        "comment",
    }
)

#: A backslash escape outside quotes: the shell drops the backslash and keeps the
#: character, so ``run\ migration.sh`` names a file with a space in it.
_WORD_ESCAPE = re.compile(r"\\(.)")

#: The only escapes a double-quoted string honours. Everything else stays literal,
#: backslash included — which is why this is not the same expression as above.
_STRING_ESCAPE = re.compile(r'\\([$`"\\\n])')


def _looks_like_path(text: str) -> bool:
    return text.startswith(("./", "../", "/")) or (
        "/" in text and not text.startswith("-") and Path(text).suffix in _SCRIPT_SUFFIXES
    )


# -- static proof ------------------------------------------------------------


def _literal_text(source: bytes, node: Node) -> str | None:
    """The text this node is *proven* to produce, or ``None`` if the shell computes it.

    This is the whole static-target rule, in one function. Every node kind here
    has a value fixed by the source; every other kind — ``simple_expansion``,
    ``expansion``, ``command_substitution``, ``arithmetic_expansion``,
    ``process_substitution``, ``translated_string``, and any ``ERROR`` node —
    resolves at run time, so the answer is ``None`` and the caller records
    nothing.

    Deciding this from the tree rather than by looking for ``$`` in the text is
    what makes it right in both directions: ``"$BASE/script.sh"`` is rejected
    even though it ends in a plausible filename, and ``'./run\\ x.sh'`` is
    accepted with its quoting and escaping removed, because quotes are shell
    syntax and were never part of the filename.
    """
    kind = node.type
    if kind == "word":
        return _WORD_ESCAPE.sub(r"\1", node_text(source, node))
    if kind == "number":
        return node_text(source, node)
    if kind == "raw_string":
        # Single quotes suspend every expansion, so the body is literal as written.
        return node_text(source, node)[1:-1]
    if kind == "string":
        return _literal_string(source, node)
    if kind in {"concatenation", "command_name"}:
        parts: list[str] = []
        for child in node.children:
            piece = _literal_text(source, child)
            if piece is None:
                return None
            parts.append(piece)
        return "".join(parts)
    return None


def _literal_string(source: bytes, node: Node) -> str | None:
    """A double-quoted string, if it contains no expansion."""
    parts: list[str] = []
    for child in node.children:
        if child.type == '"':
            continue
        if child.type != "string_content":
            return None
        parts.append(_STRING_ESCAPE.sub(r"\1", node_text(source, child)))
    return "".join(parts)


# -- wrapper policy ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Wrapper:
    """How to find the command a transparent wrapper runs.

    Every wrapper has its own option grammar, so every wrapper gets its own
    entry: a name is never unwrapped because it resembles one that is. The rule
    is **fail closed** — a token starting with ``-`` that is not written down
    here stops the unwrap, because an unknown flag may consume the next word and
    counting positions past it is guessing.

    ``consuming`` lists flags that take a value, accepted both as ``-s TERM`` and
    ``--signal=TERM``. ``operands`` is how many positional words come *before*
    the command; their values may be computed without affecting anything, since
    position is what identifies the command and position is static.
    """

    standalone: frozenset[str] = frozenset()
    consuming: frozenset[str] = frozenset()
    operands: int = 0
    assignments: bool = False


#: Wrappers RepoAtlas sees through, with the grammar that makes it safe.
#:
#: Decisions for the wrappers that are *not* here, and why:
#:
#: ``sudo``
#:     Its flags consume arguments (``-u user``, ``-g group``), it accepts
#:     ``--``, and its full grammar is large. ``sudo`` genuinely is the executed
#:     command, so ``EXECUTES cmd:sudo`` is a true fact and the wrapped script is
#:     simply not claimed. That is a limitation, not a fabrication.
#: ``xargs``
#:     Builds its final argument vector from *stdin*; ``-I``/``-n``/``-a``
#:     reshape it, and with no command at all it defaults to ``echo``.
#: ``chroot``
#:     The wrapped path resolves inside a **different root**, so ``chroot /jail
#:     ./run.sh`` does not name this repository's ``./run.sh``. Unwrapping it
#:     would produce a confidently wrong cross-file edge.
#: ``stdbuf``, ``ionice``
#:     Safe in principle, but their attached-value option forms (``-oL``,
#:     ``-c3``) need per-flag value-attachment rules no other wrapper needs, for
#:     commands that are rare in deployment scripts. Out of V1 scope.
_WRAPPERS: dict[str, _Wrapper] = {
    # `-u`, `-C` and `-S` are absent on purpose: the first two consume the next
    # word and the third re-splits its argument into a whole command line.
    "env": _Wrapper(
        standalone=frozenset({"-i", "--ignore-environment", "-"}),
        assignments=True,
    ),
    # `-a name` renames argv[0] and consumes the next word.
    "exec": _Wrapper(standalone=frozenset({"-c", "-l"})),
    # nohup takes no options at all.
    "nohup": _Wrapper(),
    # `-v` and `-V` print where a command *would* be found; they run nothing.
    "command": _Wrapper(standalone=frozenset({"-p"})),
    "nice": _Wrapper(consuming=frozenset({"-n", "--adjustment"})),
    # timeout [OPTION]... DURATION COMMAND [ARG]...
    "timeout": _Wrapper(
        standalone=frozenset({"--preserve-status", "--foreground", "-v", "--verbose"}),
        consuming=frozenset({"-s", "--signal", "-k", "--kill-after"}),
        operands=1,
    ),
    "setsid": _Wrapper(standalone=frozenset({"-c", "--ctty", "-f", "--fork", "-w", "--wait"})),
    # `time` is usually the shell's reserved word rather than a binary, so
    # `EXECUTES cmd:time` is the one wrapper record that may name nothing real.
    # Preferring the wrapped command is therefore both truer and more useful.
    # Bash's reserved word accepts only `-p`; anything else means /usr/bin/time,
    # whose own options consume arguments, and the unwrap stops.
    "time": _Wrapper(standalone=frozenset({"-p", "--portability"})),
}


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
        start, end = line_range(node)

        # `NAME=value cmd` puts NAME in the environment of `cmd` alone. That is the
        # same relationship `export NAME=value` creates, scoped to one command, so
        # it is recorded as one (V1_SPEC §10). Recorded before the target is
        # examined, because it is true whether or not the target is knowable:
        # `LOG_LEVEL=debug $CMD` still exports LOG_LEVEL.
        for child in node.children:
            if child.type == "variable_assignment":
                name_child = child.child_by_field_name("name")
                if name_child is not None:
                    self._export(node_text(self._source, name_child), start, end)

        command = _literal_text(self._source, name_node)
        if not command:
            # The shell computes this target at run time. The source proves that
            # *an* execution happens; it does not prove *what* runs, so no
            # resolvable target is recorded — not even a low-confidence one
            # (V1_SPEC §2). The `$VAR` in command position is still recorded as an
            # environment read by `_expansion`, which is a fact this file does
            # support.
            return

        arguments = self._arguments(node)

        if command in _SOURCE_COMMANDS:
            self._sourced(arguments, start, end)
            return

        wrapper = _WRAPPERS.get(Path(command).name)
        if wrapper is not None and self._unwrapped(command, wrapper, arguments, start, end):
            return

        if not self._executed_path(command, start, end):
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

    def _arguments(self, node: Node) -> list[str | None]:
        """This command's arguments, in order, as proven text or ``None``.

        Position is preserved for computed arguments rather than dropping them,
        because several rules count words: ``timeout "$T" ./x.sh`` runs
        ``./x.sh`` regardless of what the duration turns out to be, and
        ``salt-call state.apply "$STATE"`` must not skip forward to the next
        literal word and name the wrong state.
        """
        return [
            _literal_text(self._source, child)
            for child in node.children
            if child.is_named and child.type not in _NON_ARGUMENT
        ]

    def _unwrapped(
        self,
        wrapper: str,
        policy: _Wrapper,
        arguments: list[str | None],
        start: int,
        end: int,
    ) -> bool:
        """Record what a transparent wrapper actually runs.

        Returns whether the wrapped command was identified. When it was not — an
        unrecognized flag, a computed target, or nothing to run — this records
        nothing and the caller falls back to reporting the wrapper itself, which
        is a true statement rather than a guess (V1_SPEC §2).
        """
        rest = list(arguments)
        assignments: list[str] = []

        while rest:
            word = rest[0]
            if word is None:
                break  # computed: it may be a flag or the command; stop deciding
            if word == "--":
                rest.pop(0)
                break
            if word.startswith("-"):
                flag, separator, _ = word.partition("=")
                if separator and flag in policy.consuming:
                    rest.pop(0)
                    continue
                if word in policy.standalone:
                    rest.pop(0)
                    continue
                if word in policy.consuming:
                    if len(rest) < 2:
                        return False
                    del rest[:2]
                    continue
                return False  # unknown flag: fail closed
            match = _PREFIX_ASSIGNMENT.match(word)
            if match is None:
                break
            if not policy.assignments:
                # Only `env` gives `NAME=value` operands their POSIX meaning. For
                # any other wrapper the token is not a command, so making it one
                # would invent `cmd:NAME=value`.
                return False
            assignments.append(match.group(1))
            rest.pop(0)

        if len(rest) <= policy.operands:
            return False
        del rest[: policy.operands]

        inner, inner_arguments = rest[0], rest[1:]
        if not inner or inner.startswith("-"):
            # Still a flag where the command should be: the operand count and the
            # grammar disagree about this line, so stop rather than record a
            # command named `--`.
            return False

        for name in assignments:
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

    def _sourced(self, arguments: list[str | None], start: int, end: int) -> None:
        """``source x`` / ``. x`` imports another script."""
        target = arguments[0] if arguments else None
        if not target:
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

    def _interpreted_script(
        self, command: str, arguments: list[str | None], start: int, end: int
    ) -> None:
        """``python3 x.py`` / ``ruby x.rb`` executes a script in the tree."""
        suffix = _INTERPRETERS.get(Path(command).name)
        if suffix is None:
            return
        for argument in arguments:
            if argument is None:
                continue
            if argument in _INTERPRETER_CODE_FLAGS:
                # Everything after this is a program text, not a path.
                return
            if argument.startswith("-"):
                continue
            if Path(argument).suffix != suffix:
                continue
            self.references.append(
                ParsedReference(
                    edge_type=EdgeType.EXECUTES,
                    target_kind=EntityType.SCRIPT,
                    target_key=argument,
                    line_start=start,
                    line_end=end,
                    source_symbol=self._scope,
                    meta={"interpreter": command},
                )
            )
            return

    def _salt_invocation(
        self, command: str, arguments: list[str | None], start: int, end: int
    ) -> None:
        """``salt-call state.apply service`` references a Salt state."""
        if Path(command).name not in _SALT_COMMANDS:
            return
        words = [a for a in arguments if a is None or not a.startswith("-")]
        for index, word in enumerate(words):
            if word not in _SALT_STATE_FUNCTIONS:
                continue
            if index + 1 >= len(words):
                return
            state = words[index + 1]
            if state is None:
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
        if not _looks_like_path(command):
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
    # 2: whole-file spans stop at EOF and are absent for an empty file; a
    #    classic-Mac (CR-only) file is no longer collapsed into one line.
    version = "2"
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

        whole_file_start, whole_file_last = whole_file_span(content)
        script = ParsedSymbol(
            kind=EntityType.SCRIPT,
            name=path.name,
            qualified_name=path.as_posix(),
            line_start=whole_file_start,
            line_end=whole_file_last,
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
