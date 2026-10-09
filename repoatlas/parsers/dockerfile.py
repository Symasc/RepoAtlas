"""Dockerfile parser (V1_SPEC §7.8).

Dockerfiles are read as text, never built. ``RUN`` bodies are scanned for the
package installations the specification asks for (V1_SPEC §13, "reliable
dependency references found in Dockerfile, Bash and Salt") but nothing in them
is executed.

Which makes target position the hard part, and the invariant this module exists
to hold (ARCHITECTURE.md §5):

    A Dockerfile target is emitted only when the instruction statically names it
    under Docker's semantics. A build or shell expression is not a target name.

``RUN $TOOL`` does not execute a program called ``$TOOL``; it executes a program
whose name this file does not contain. The honest record of that is **nothing** —
not a ``cmd:$TOOL`` node, not a lower confidence on one, and not an
``unresolved_refs`` row carrying the expression, which would put the same
invented target one indirection away.

**But ``$`` does not mean the same thing in every instruction**, so proof is
decided by :mod:`repoatlas.parsers.dockerwords` under each instruction's own
rules rather than by looking for a dollar sign. ``COPY ["$SRC", "/app/"]`` is
computed because the builder expands JSON operands too; ``RUN ["$TOOL", …]`` is
literal because the exec form invokes no shell and Docker really does try to run
a program named ``$TOOL``. Both were checked against a real daemon.
"""

from __future__ import annotations

import json
import posixpath
import re
from pathlib import Path

from repoatlas.models.parsing import (
    EdgeType,
    EntityType,
    ParsedFile,
    ParsedReference,
    ParsedSymbol,
)
from repoatlas.parsers.dockerwords import (
    SourceDomain,
    Word,
    exec_form,
    expanded,
    source_domain,
    split_flags,
    split_words,
    verbatim,
)
from repoatlas.parsers.http_facts import DOCKERFILE_COMMAND, pending_reference
from repoatlas.parsers.marks import split_lines, whole_file_span

_INSTRUCTIONS = frozenset(
    {
        "FROM",
        "RUN",
        "COPY",
        "ADD",
        "ENV",
        "ARG",
        "ENTRYPOINT",
        "CMD",
        "WORKDIR",
        "EXPOSE",
        "USER",
        "VOLUME",
        "LABEL",
        "SHELL",
        "STOPSIGNAL",
        "HEALTHCHECK",
        "ONBUILD",
        "MAINTAINER",
    }
)

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_INSTRUCTION_LINE = re.compile(r"^\s*(?P<keyword>[A-Za-z]+)\s*(?P<rest>.*)$", re.DOTALL)

#: Shell operators after which the next word is again in command position.
_SEPARATORS = frozenset({"&&", "||", ";", "|"})

#: ``pip install x`` / ``npm install y`` / ``gem install z`` inside a RUN body.
_INSTALLERS: dict[str, str] = {
    "pip": "pypi",
    "pip3": "pypi",
    "uv": "pypi",
    "npm": "npm",
    "pnpm": "npm",
    "yarn": "npm",
    "gem": "gem",
    "apt-get": "apt",
    "apt": "apt",
    "apk": "apk",
    "yum": "yum",
    "dnf": "dnf",
}
_INSTALL_VERBS = frozenset({"install", "add"})


class _Instruction:
    __slots__ = ("_operands", "arguments", "keyword", "line_end", "line_start")

    def __init__(self, keyword: str, arguments: str, line_start: int, line_end: int) -> None:
        self.keyword = keyword
        self.arguments = arguments
        self.line_start = line_start
        self.line_end = line_end
        self._operands: tuple[SourceDomain, list[Word]] | None = None

    def operands(self) -> tuple[SourceDomain, list[Word]]:
        """A ``COPY``/``ADD``'s source domain and words: one flag scan, however many
        readers ask."""
        if self._operands is None:
            self._operands = _operands(self.arguments)
        return self._operands


def _logical_lines(content: str) -> list[_Instruction]:
    """Join continuations and drop comments, keeping true line numbers.

    Split with the shared source-grammar splitter so these numbers mean the same
    thing as the whole-file span computed from the same content. ``splitlines()``
    would break on a form feed, and every instruction after one would be reported
    a line too low.
    """
    instructions: list[_Instruction] = []
    buffer: list[str] = []
    start = 0

    for number, raw in enumerate(split_lines(content), start=1):
        stripped = raw.strip()
        if not buffer and (not stripped or stripped.startswith("#")):
            continue
        if not buffer:
            start = number
        continues = stripped.endswith("\\")
        buffer.append(stripped[:-1] if continues else stripped)
        if continues:
            continue

        joined = " ".join(part for part in buffer if part)
        buffer = []
        match = _INSTRUCTION_LINE.match(joined)
        if match is None:
            continue
        keyword = match.group("keyword").upper()
        if keyword not in _INSTRUCTIONS:
            continue
        instructions.append(_Instruction(keyword, match.group("rest").strip(), start, number))

    return instructions


def _has_heredoc(words: list[Word]) -> bool:
    """Whether this instruction redirects a here-document into its command.

    The real command then lives in the body, which is shell source this parser
    does not read — and the introducer itself is a redirection operator, not a
    program. Recording it produced ``cmd:<<EOF``, a node naming nothing at all.
    Fail closed: the instruction contributes no target.
    """
    return any(word.raw.startswith("<<") for word in words)


def _operands(arguments: str) -> tuple[SourceDomain, list[Word]]:
    """A ``COPY``/``ADD``'s source domain and its words, sources then destination: one
    flag scan per instruction, shared by every reader of it."""
    flags, rest = split_flags(arguments)
    argv = exec_form(rest)
    words = (
        [expanded(element) for element in argv]
        if argv is not None
        else [word for word in split_words(rest) if not word.raw.startswith("--")]
    )
    return source_domain(flags), words


class _Runs:
    """What one stage says about the program its image runs, for deployment identities.

    Only literal values are kept. ``WORKDIR`` resolves against the one before it; a
    ``COPY``/``ADD`` records its literal sources and its destination (relative to the
    working directory then), and one whose content comes from elsewhere -- ``--from``,
    a URL, a computed source -- is recorded as *foreign*, so it can still shadow a path.
    ``CMD`` and ``ENTRYPOINT`` keep their argument vector: the exec form verbatim, the
    shell form only when every word is literal and no operator joins them.
    """

    def __init__(self) -> None:
        self.workdir: str | None = None
        self.copies: list[list[object]] = []
        self.entrypoint: dict[str, object] | None = None
        self.cmd: dict[str, object] | None = None
        #: The line of the last instruction read here: where a claim without a command sits.
        self.last = 0

    def observe(self, instruction: _Instruction) -> None:
        keyword = instruction.keyword
        if keyword in {"WORKDIR", "COPY", "ADD", "CMD", "ENTRYPOINT"}:
            self.last = instruction.line_start
        if keyword == "WORKDIR":
            words = split_words(instruction.arguments)
            value = words[0].literal if len(words) == 1 else None
            if value is None:
                self.workdir = None
            elif value.startswith("/"):
                self.workdir = posixpath.normpath(value)
            else:
                self.workdir = (
                    posixpath.normpath(posixpath.join(self.workdir, value))
                    if self.workdir is not None
                    else None
                )
        elif keyword in {"COPY", "ADD"}:
            self.copies.append(self._copy(instruction, *instruction.operands()))
        elif keyword in {"CMD", "ENTRYPOINT"}:
            argv = exec_form(instruction.arguments)
            form = "exec"
            if argv is None:
                form = "shell"
                words = split_words(instruction.arguments, substitution=True)
                literal = [word.literal for word in words]
                if any(value is None for value in literal) or any(
                    word.raw in _SEPARATORS or word.raw.startswith(("<", ">")) for word in words
                ):
                    argv = None
                else:
                    argv = [str(value) for value in literal]
            recorded = {"argv": argv, "form": form, "line": instruction.line_start}
            if keyword == "CMD":
                self.cmd = recorded
            else:
                self.entrypoint = recorded

    def _copy(
        self, instruction: _Instruction, domain: SourceDomain, words: list[Word]
    ) -> list[object]:
        destination = words[-1].literal if len(words) >= 2 else None
        if destination is not None and not destination.startswith("/"):
            destination = (
                posixpath.join(self.workdir, destination) if self.workdir is not None else None
            )
        into = bool(words) and words[-1].raw.endswith(("/", "/."))
        if destination is not None:
            destination = posixpath.normpath(destination)
        sources = [word.literal for word in words[:-1]]
        foreign = (
            domain is SourceDomain.FOREIGN
            or not sources
            or any(
                source is None or "://" in source or any(c in source for c in "*?[")
                for source in sources
            )
        )
        literal = [] if foreign else [str(source) for source in sources]
        return [literal, destination, instruction.line_start, foreign, into]

    def claim(self) -> ParsedReference | None:
        # Kept without a command too, since a compose service's ``command`` may supply
        # it -- but only when the stage states something literal: a stage whose every
        # value is computed, empty or malformed names nothing, and records nothing.
        commands = [found for found in (self.cmd, self.entrypoint) if found and found["argv"]]
        copied = [copy for copy in self.copies if not copy[3]]
        if not commands and not copied and self.workdir is None:
            return None
        meta = {
            "copies": json.dumps(self.copies),
            "entrypoint": json.dumps(self.entrypoint),
            "cmd": json.dumps(self.cmd),
        }
        if self.workdir is not None:
            meta["workdir"] = self.workdir
        lines = [int(str(found["line"])) for found in (self.cmd, self.entrypoint) if found]
        line = max(lines) if lines else self.last
        return pending_reference(
            EdgeType.REFERENCES,
            EntityType.CONFIG,
            "command",
            line,
            line,
            role=DOCKERFILE_COMMAND,
            **meta,
        )


class DockerfileParser:
    """Deterministic Dockerfile instruction extraction."""

    name = "dockerfile"
    # 3: computed targets — builder or shell expansions — no longer become
    #    deterministic or unresolved target names.
    # 4: `COPY --from` sources name another stage, image or named context, not
    #    the build context, so they no longer become repository file references.
    #    The bump is load-bearing: the Dockerfile that produced a false edge is
    #    byte-identical afterwards, so only a facts-version change makes an
    #    ordinary `sync` reparse it and drop the edge (V1_SPEC §12).
    # 5: the final stage's working directory, copies and command, as a claim for
    #    deployment identities (repoatlas.graph.deploy_identity).
    # 6: no such claim for a stage that states nothing literal (every value
    #    computed, empty or malformed).
    # 7: ``<name>.Dockerfile`` is a Dockerfile whatever the suffix's case, as Docker reads
    #    ``-f deploy/api.Dockerfile``: a CI build naming one is tied to its code (DF2).
    version = "7"
    language = "Dockerfile"

    def supports(self, path: Path) -> bool:
        name = path.name
        return (
            name.startswith(("Dockerfile", "Containerfile")) or path.suffix.lower() == ".dockerfile"
        )

    def parse(self, path: Path, content: str) -> ParsedFile:
        symbols: list[ParsedSymbol] = []
        references: list[ParsedReference] = []
        stage: str | None = None
        stage_index = 0
        stages: set[str] = set()

        runs = _Runs()
        for instruction in _logical_lines(content):
            handler = getattr(self, f"_{instruction.keyword.lower()}", None)
            if instruction.keyword == "FROM":
                stage_index += 1
                stage = self._from(instruction, symbols, references, stage_index, stages)
                if stage is not None:
                    stages.add(stage)
                runs = _Runs()
                continue
            runs.observe(instruction)
            if handler is not None:
                handler(instruction, references, stage)
        command = runs.claim()
        if command is not None:
            references.append(command)

        whole_file_start, whole_file_last = whole_file_span(content)
        image = ParsedSymbol(
            kind=EntityType.CONFIG,
            name=path.name,
            qualified_name=path.as_posix(),
            line_start=whole_file_start,
            line_end=whole_file_last,
        )
        return ParsedFile(
            language=self.language,
            parser=self.name,
            parser_version=self.version,
            symbols=(image, *symbols),
            references=tuple(references),
        )

    # -- instructions --------------------------------------------------------

    def _from(
        self,
        instruction: _Instruction,
        symbols: list[ParsedSymbol],
        references: list[ParsedReference],
        index: int,
        stages: set[str],
    ) -> str | None:
        """``FROM`` names a base image, and always begins a stage.

        The two facts are independent, which is why a computed image does not
        take the stage with it: ``FROM ${BASE_IMAGE} AS builder`` proves a stage
        called ``builder`` exists and proves nothing about what it is built on.

        V1 performs **no constant propagation**, so even ``ARG BASE=python:3.13``
        followed by ``FROM $BASE`` yields no image. Doing that correctly needs
        rules for ARG scope, redeclaration, build-time overrides, defaults, stage
        boundaries and the automatic platform args; guessing at it would put a
        fabricated image in the graph whenever any of them applied.
        """
        _flags, rest = split_flags(instruction.arguments)
        words = [word for word in split_words(rest) if not word.raw.startswith("--")]
        if not words:
            return None

        image = words[0]
        aliased = len(words) >= 3 and (words[1].literal or "").lower() == "as"
        alias = (words[2].literal if aliased else None) or f"stage{index}"

        meta = {"stage": str(index)}
        if image.literal is not None:
            meta["image"] = image.literal
        symbols.append(
            ParsedSymbol(
                kind=EntityType.CONFIG,
                name=alias,
                qualified_name=alias,
                line_start=instruction.line_start,
                line_end=instruction.line_end,
                # The raw text, quoted rather than resolved: a signature reports
                # what the line says, and for a computed image that is all there
                # is to report.
                signature=f"FROM {image.raw}",
                meta=meta,
            )
        )

        if image.literal is None:
            return alias
        # ``FROM builder`` continues an earlier stage; it is not an image pull.
        base_is_stage = image.literal in stages
        references.append(
            ParsedReference(
                edge_type=EdgeType.REFERENCES if base_is_stage else EdgeType.DEPENDS_ON,
                target_kind=EntityType.CONFIG if base_is_stage else EntityType.PACKAGE,
                target_key=image.literal,
                line_start=instruction.line_start,
                line_end=instruction.line_end,
                source_symbol=alias,
                meta={"role": "stage"} if base_is_stage else {"ecosystem": "docker"},
            )
        )
        return alias

    def _env(
        self, instruction: _Instruction, references: list[ParsedReference], stage: str | None
    ) -> None:
        for name in self._assigned_names(instruction.arguments):
            references.append(
                ParsedReference(
                    edge_type=EdgeType.EXPORTS,
                    target_kind=EntityType.ENV_VAR,
                    target_key=name,
                    line_start=instruction.line_start,
                    line_end=instruction.line_end,
                    source_symbol=stage,
                    meta={"via": instruction.keyword},
                )
            )

    _arg = _env

    def _assigned_names(self, arguments: str) -> list[str]:
        """The variables this ``ENV``/``ARG`` declares.

        A declared *name* is static syntax — it sits to the left of the ``=`` and
        no expansion produces it — so ``ENV HOST=$OTHER`` still proves that
        ``HOST`` exists and is set here. Only the value is computed, and the value
        is not recorded. A computed name (``ENV $NAME=x``) fails the name pattern
        and is dropped, as it already was.
        """
        parts = [word.literal or word.raw for word in split_words(arguments)]
        if not parts:
            return []
        if "=" not in parts[0]:
            # Legacy single-variable form: ``ENV KEY value`` / ``ARG KEY``.
            return [parts[0]] if _ENV_NAME.match(parts[0]) else []
        return [
            name for part in parts if "=" in part and _ENV_NAME.match(name := part.split("=", 1)[0])
        ]

    def _run(
        self, instruction: _Instruction, references: list[ParsedReference], stage: str | None
    ) -> None:
        argv = exec_form(instruction.arguments)
        if argv is not None:
            self._exec_command(argv, instruction, references, stage)
            return
        words = split_words(instruction.arguments, substitution=True)
        if _has_heredoc(words):
            return
        self._shell_commands(words, instruction, references, stage)
        self._installed_packages(words, instruction, references, stage)

    def _entrypoint(
        self, instruction: _Instruction, references: list[ParsedReference], stage: str | None
    ) -> None:
        argv = exec_form(instruction.arguments)
        if argv is not None:
            self._exec_command(argv, instruction, references, stage)
            return
        words = split_words(instruction.arguments, substitution=True)
        if _has_heredoc(words):
            return
        self._shell_commands(words, instruction, references, stage)

    _cmd = _entrypoint

    def _exec_command(
        self,
        argv: list[str],
        instruction: _Instruction,
        references: list[ParsedReference],
        stage: str | None,
    ) -> None:
        """The exec form runs exactly one program: ``argv[0]``, verbatim.

        No shell means no operators and no substitution, so the remaining
        elements are arguments however they are spelled — ``["./a.sh", "&&",
        "./b.sh"]`` runs ``./a.sh`` and passes it two arguments — and a ``$`` in
        ``argv[0]`` is part of the program's name rather than a reference to one.
        """
        if not argv:
            return
        self._record_command(verbatim(argv[0]), instruction, references, stage)

    def _shell_commands(
        self,
        words: list[Word],
        instruction: _Instruction,
        references: list[ParsedReference],
        stage: str | None,
    ) -> None:
        """First word of each shell-separated segment is an executed command."""
        expect_command = True
        for word in words:
            if word.raw in _SEPARATORS:
                expect_command = True
                continue
            if not expect_command:
                continue
            expect_command = False
            self._record_command(word, instruction, references, stage)

    def _record_command(
        self,
        word: Word,
        instruction: _Instruction,
        references: list[ParsedReference],
        stage: str | None,
    ) -> None:
        name = word.literal
        if name is None:
            # The builder or the shell decides what runs. The source proves that
            # *an* execution happens; it does not prove *what* runs, so nothing
            # resolvable is recorded — not even at a reduced confidence, which
            # would be the same invented node with a smaller number on it
            # (V1_SPEC §2).
            return
        if not name or name.startswith("-") or word.raw.startswith(("<", ">")):
            return
        references.append(
            ParsedReference(
                edge_type=EdgeType.EXECUTES,
                target_kind=EntityType.COMMAND,
                target_key=Path(name).name if "/" in name else name,
                line_start=instruction.line_start,
                line_end=instruction.line_end,
                source_symbol=stage,
                meta={"via": instruction.keyword},
            )
        )

    def _installed_packages(
        self,
        words: list[Word],
        instruction: _Instruction,
        references: list[ParsedReference],
        stage: str | None,
    ) -> None:
        """``RUN pip install foo bar`` names dependencies (V1_SPEC §13).

        Each of the three positions must be proven on its own. A computed
        installer (``$PIP install requests``) means the ecosystem is unknown, so
        nothing is claimed; a computed verb means the operation is unknown; a
        computed package name is skipped while its literal siblings are kept,
        because ``requests`` in ``pip install $PKG requests`` is independently
        named by the source.
        """
        index = 0
        while index < len(words):
            installer = words[index].literal
            if installer is None:
                # A computed installer: the ecosystem is unknowable, so nothing
                # after it can be attributed to one.
                index += 1
                continue
            ecosystem = _INSTALLERS.get(installer)
            if ecosystem is None or index + 1 >= len(words):
                index += 1
                continue
            cursor = index + 1
            while cursor < len(words) and (words[cursor].literal or "").startswith("-"):
                cursor += 1
            if cursor >= len(words) or words[cursor].literal not in _INSTALL_VERBS:
                index += 1
                continue
            cursor += 1
            while cursor < len(words) and words[cursor].raw not in _SEPARATORS:
                package = words[cursor].literal
                cursor += 1
                if package is None or package.startswith(("-", ".")):
                    continue
                references.append(
                    ParsedReference(
                        edge_type=EdgeType.DEPENDS_ON,
                        target_kind=EntityType.PACKAGE,
                        target_key=package,
                        line_start=instruction.line_start,
                        line_end=instruction.line_end,
                        source_symbol=stage,
                        confidence=0.9,
                        meta={"ecosystem": ecosystem, "via": installer},
                    )
                )
            index = cursor

    def _copy(
        self,
        instruction: _Instruction,
        references: list[ParsedReference],
        stage: str | None,
    ) -> None:
        """``COPY``/``ADD`` sources, when the build context proves which files they are.

        Two things must both be proven before a source is a repository file, and
        only the first used to be asked.

        *What is it called* — both forms are expanded by the builder, so both are
        classified the same way; the JSON form exists to quote paths containing
        whitespace, not to suppress substitution. A computed source names no
        file: ``${BASE}/x.yaml`` is not partial evidence about ``x.yaml``, it is a
        path this file does not contain.

        *Where is that name resolved* — without ``--from`` the answer is the
        build context, which is this tree. With it, the answer is the root of
        another stage, image or named context, and a repository file that happens
        to share the spelling is a different file. ``COPY --from=builder
        assets/main.js /app/`` named ``assets/main.js`` in the *builder stage*,
        and RepoAtlas resolved it against the working tree at confidence 1.0 —
        a deterministic fact that was simply untrue, and one citation
        verification could not catch because both the Dockerfile line and the
        repository file genuinely exist. The instruction is still parsed, still
        chunked and still retrievable as text; what it no longer does is claim a
        file it never named.
        """
        domain, words = instruction.operands()
        if domain is SourceDomain.FOREIGN:
            return None
        for source in words[:-1]:
            if source.literal is None:
                continue
            references.append(
                ParsedReference(
                    edge_type=EdgeType.REFERENCES,
                    target_kind=EntityType.FILE,
                    target_key=source.literal,
                    line_start=instruction.line_start,
                    line_end=instruction.line_end,
                    source_symbol=stage,
                    meta={"via": instruction.keyword},
                )
            )

    _add = _copy
