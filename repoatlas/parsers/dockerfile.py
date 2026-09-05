"""Dockerfile parser (V1_SPEC §7.8).

Dockerfiles are read as text, never built. ``RUN`` bodies are scanned for the
package installations the specification asks for (V1_SPEC §13, "reliable
dependency references found in Dockerfile, Bash and Salt") but nothing in them
is executed.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from repoatlas.models.parsing import (
    EdgeType,
    EntityType,
    ParsedFile,
    ParsedReference,
    ParsedSymbol,
)
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
_JSON_ARRAY = re.compile(r"^\s*\[.*\]\s*$", re.DOTALL)

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
    __slots__ = ("arguments", "keyword", "line_end", "line_start")

    def __init__(self, keyword: str, arguments: str, line_start: int, line_end: int) -> None:
        self.keyword = keyword
        self.arguments = arguments
        self.line_start = line_start
        self.line_end = line_end


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


def _split(arguments: str) -> list[str]:
    """Best-effort shell split that never raises on unbalanced quotes."""
    try:
        return shlex.split(arguments)
    except ValueError:
        return arguments.split()


def _json_array(arguments: str) -> list[str] | None:
    if not _JSON_ARRAY.match(arguments):
        return None
    inner = arguments.strip()[1:-1]
    return [part.strip().strip('"').strip("'") for part in inner.split(",") if part.strip()]


class DockerfileParser:
    """Deterministic Dockerfile instruction extraction."""

    name = "dockerfile"
    version = "2"  # 2: whole-file spans stop at EOF, and are absent for an empty file
    language = "Dockerfile"

    def supports(self, path: Path) -> bool:
        name = path.name
        return name.startswith(("Dockerfile", "Containerfile")) or path.suffix == ".dockerfile"

    def parse(self, path: Path, content: str) -> ParsedFile:
        symbols: list[ParsedSymbol] = []
        references: list[ParsedReference] = []
        stage: str | None = None
        stage_index = 0
        stages: set[str] = set()

        for instruction in _logical_lines(content):
            handler = getattr(self, f"_{instruction.keyword.lower()}", None)
            if instruction.keyword == "FROM":
                stage_index += 1
                stage = self._from(instruction, symbols, references, stage_index, stages)
                if stage is not None:
                    stages.add(stage)
                continue
            if handler is not None:
                handler(instruction, references, stage)

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
        parts = _split(instruction.arguments)
        parts = [p for p in parts if not p.startswith("--")]
        if not parts:
            return None
        image = parts[0]
        alias = parts[2] if len(parts) >= 3 and parts[1].lower() == "as" else f"stage{index}"

        symbols.append(
            ParsedSymbol(
                kind=EntityType.CONFIG,
                name=alias,
                qualified_name=alias,
                line_start=instruction.line_start,
                line_end=instruction.line_end,
                signature=f"FROM {image}",
                meta={"image": image, "stage": str(index)},
            )
        )
        # ``FROM builder`` continues an earlier stage; it is not an image pull.
        base_is_stage = image in stages
        references.append(
            ParsedReference(
                edge_type=EdgeType.REFERENCES if base_is_stage else EdgeType.DEPENDS_ON,
                target_kind=EntityType.CONFIG if base_is_stage else EntityType.PACKAGE,
                target_key=image,
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
        parts = _split(arguments)
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
        array = _json_array(instruction.arguments)
        commands = array if array is not None else _split(instruction.arguments)
        self._commands(commands, instruction, references, stage)
        if array is None:
            self._installed_packages(commands, instruction, references, stage)

    def _entrypoint(
        self, instruction: _Instruction, references: list[ParsedReference], stage: str | None
    ) -> None:
        array = _json_array(instruction.arguments)
        self._commands(
            array if array is not None else _split(instruction.arguments),
            instruction,
            references,
            stage,
        )

    _cmd = _entrypoint

    def _commands(
        self,
        parts: list[str],
        instruction: _Instruction,
        references: list[ParsedReference],
        stage: str | None,
    ) -> None:
        """First word of each shell-separated segment is an executed command."""
        expect_command = True
        for part in parts:
            if part in {"&&", "||", ";", "|"}:
                expect_command = True
                continue
            if not expect_command:
                continue
            expect_command = False
            if not part or part.startswith("-"):
                continue
            references.append(
                ParsedReference(
                    edge_type=EdgeType.EXECUTES,
                    target_kind=EntityType.COMMAND,
                    target_key=Path(part).name if "/" in part else part,
                    line_start=instruction.line_start,
                    line_end=instruction.line_end,
                    source_symbol=stage,
                    meta={"via": instruction.keyword},
                )
            )

    def _installed_packages(
        self,
        parts: list[str],
        instruction: _Instruction,
        references: list[ParsedReference],
        stage: str | None,
    ) -> None:
        """``RUN pip install foo bar`` names dependencies (V1_SPEC §13)."""
        index = 0
        while index < len(parts):
            ecosystem = _INSTALLERS.get(parts[index])
            if ecosystem is None or index + 1 >= len(parts):
                index += 1
                continue
            cursor = index + 1
            while cursor < len(parts) and parts[cursor].startswith("-"):
                cursor += 1
            if cursor >= len(parts) or parts[cursor] not in _INSTALL_VERBS:
                index += 1
                continue
            cursor += 1
            while cursor < len(parts) and parts[cursor] not in {"&&", "||", ";", "|"}:
                package = parts[cursor]
                cursor += 1
                if package.startswith("-") or package.startswith("."):
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
                        meta={"ecosystem": ecosystem, "via": parts[index]},
                    )
                )
            index = cursor

    def _copy(
        self, instruction: _Instruction, references: list[ParsedReference], stage: str | None
    ) -> None:
        parts = [p for p in _split(instruction.arguments) if not p.startswith("--")]
        for source in parts[:-1]:
            references.append(
                ParsedReference(
                    edge_type=EdgeType.REFERENCES,
                    target_kind=EntityType.FILE,
                    target_key=source,
                    line_start=instruction.line_start,
                    line_end=instruction.line_end,
                    source_symbol=stage,
                    meta={"via": instruction.keyword},
                )
            )

    _add = _copy

    def _workdir(
        self, instruction: _Instruction, references: list[ParsedReference], stage: str | None
    ) -> None:
        return None
