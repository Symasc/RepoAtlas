"""The commands of a CI ``run`` script that build, publish or deploy, read from the bash
syntax tree and never run (DF1_PHASE_A.md §4).

A script is read for a handful of **recognised** commands -- ``docker build`` (also
``buildx build`` and ``image build``), ``docker push``, ``docker tag``, ``helm upgrade`` /
``helm install`` and ``kubectl apply`` -- and for **delegation** to a local script
(``./x.sh``, ``bash x.sh``). Every other command is ignored: ``pytest`` is not evidence of
anything here.

**Top level only.** A command is read when it stands at the script's top level, or as a
member of an ``&&`` / ``||`` / ``;`` list there. Inside a loop, a conditional, a ``case``, a
function, a subshell, a ``{ }`` group, a pipeline or a command substitution it is
*nested*: whether and how often it runs depends on what the shell computes. A nested
recognised command is reported as such and binds nothing (``control-flow-unsupported``).

**Words as parts.** An argument is a list of ``[kind, value]`` parts: literal ``text``, a
shell variable ``var`` (``$TAG``, ``${TAG}``), or an ``expr`` the shell or the CI system
computes. CI expressions (GitHub's ``${{ ... }}``, CircleCI's ``<< ... >>``) are replaced
by placeholder words before the script is parsed (:func:`hold_expressions`), and come back
as ``expr`` parts; one that reads ``secrets.`` is ``withheld``. Only the values of the
flags a recognised command is read for are kept -- never the command's text.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from repoatlas.models.container_image import EXPR, TEXT, VAR, WITHHELD
from repoatlas.parsers.bash import _literal_text
from repoatlas.parsers.treesitter import GrammarUnavailable, node_text, parse_tree
from repoatlas.parsers.withhold import secret_bearing

Part = list[str]
Word = list[Part]

DOCKER_BUILD = "docker-build"
DOCKER_PUSH = "docker-push"
DOCKER_TAG = "docker-tag"
HELM = "helm"
KUBECTL_APPLY = "kubectl-apply"

#: Commands per script this reads; the rest is cut and reported.
MAX_COMMANDS = 50
_HOLD = re.compile(r"\$\{\{.*?\}\}|<<.*?>>", re.DOTALL)
_TOKEN = re.compile(r"RAHOLD(\d+)X")
_NESTING = frozenset(
    {
        "for_statement",
        "c_style_for_statement",
        "while_statement",
        "if_statement",
        "case_statement",
        "function_definition",
        "subshell",
        "compound_statement",
        "pipeline",
        "command_substitution",
        "process_substitution",
        "negated_command",
        "test_command",
    }
)
_TRANSPARENT = frozenset({"program", "list", "redirected_statement"})
_SCRIPT_RUNNERS = frozenset({"bash", "sh", "source", "."})


@dataclass(frozen=True, slots=True)
class Command:
    """One command of a script: its words as parts, whether it is nested, its line."""

    words: tuple[Word, ...]
    nested: bool
    line: int

    @property
    def name(self) -> str | None:
        return literal(self.words[0]) if self.words else None


@dataclass
class Script:
    commands: list[Command] = field(default_factory=list)
    truncated: bool = False
    unreadable: bool = False


def hold_expressions(script: str) -> tuple[str, dict[str, Part]]:
    """``script`` with each CI expression replaced by a placeholder word, and the part each
    placeholder stands for."""
    held: dict[str, Part] = {}

    def hold(match: re.Match[str]) -> str:
        token = f"RAHOLD{len(held)}X"
        expression = match.group(0)
        body = expression[3:-2] if expression.startswith("${{") else expression[2:-2]
        held[token] = [WITHHELD, ""] if secret_bearing(body) else [EXPR, body.strip()]
        return token

    return _HOLD.sub(hold, script), held


def literal(word: Word) -> str | None:
    """The word's text when every part of it is literal."""
    if any(kind != TEXT for kind, _ in word):
        return None
    return "".join(value for _, value in word)


def read_script(script: str, *, first_line: int = 1) -> Script:
    """The commands of ``script``, each with its words and whether it is nested."""
    text, held = hold_expressions(script)
    found = Script()
    try:
        tree, source = parse_tree("bash", text)
    except GrammarUnavailable:
        found.unreadable = True
        return found

    def visit(node, nested: bool) -> None:  # type: ignore[no-untyped-def]
        if len(found.commands) >= MAX_COMMANDS:
            found.truncated = True
            return
        if node.type == "command":
            words = tuple(_word(source, child, held) for child in _command_words(node))
            if words:
                found.commands.append(Command(words, nested, first_line + node.start_point[0]))
            # A command inside an argument (a substitution) runs only as the shell computes.
            for child in node.named_children[1:]:
                visit(child, True)
            return
        # The script, an `&&`/`||`/`;` list and a redirection keep their commands where
        # they stand; every other construct makes what it holds nested.
        inner = nested if node.type in _TRANSPARENT else True
        for child in node.named_children:
            visit(child, inner)

    visit(tree.root_node, False)
    return found


def _command_words(node):  # type: ignore[no-untyped-def]
    for child in node.named_children:
        if child.type in (
            "variable_assignment",
            "file_redirect",
            "herestring_redirect",
            "heredoc_redirect",
        ):
            continue
        yield child


def _word(source: bytes, node, held: Mapping[str, Part]) -> Word:  # type: ignore[no-untyped-def]
    parts: Word = []
    for kind, value in _parts(source, node):
        if kind == TEXT:
            cursor = 0
            for match in _TOKEN.finditer(value):
                if match.start() > cursor:
                    parts.append([TEXT, value[cursor : match.start()]])
                parts.append(list(held.get(match.group(0), [EXPR, ""])))
                cursor = match.end()
            if cursor < len(value):
                parts.append([TEXT, value[cursor:]])
        else:
            parts.append([kind, value])
    merged: Word = []
    for part in parts:
        if merged and part[0] == TEXT and merged[-1][0] == TEXT:
            merged[-1] = [TEXT, merged[-1][1] + part[1]]
        else:
            merged.append(part)
    return merged


def _parts(source: bytes, node) -> list[Part]:  # type: ignore[no-untyped-def]
    kind = node.type
    if kind in ("word", "number", "raw_string", "command_name"):
        text = _literal_text(source, node)
        return [[TEXT, text]] if text is not None else [[EXPR, ""]]
    if kind == "simple_expansion":
        name = next(
            (
                c
                for c in node.named_children
                if c.type in ("variable_name", "special_variable_name")
            ),
            None,
        )
        return [[VAR, node_text(source, name)]] if name is not None else [[EXPR, ""]]
    if kind == "expansion":
        children = node.named_children
        if len(children) == 1 and children[0].type == "variable_name":
            return [[VAR, node_text(source, children[0])]]
        return [[EXPR, ""]]
    if kind in ("string", "concatenation"):
        found: list[Part] = []
        for child in node.named_children if kind == "concatenation" else node.children:
            if child.type in ('"',):
                continue
            if child.type == "string_content":
                found.append([TEXT, node_text(source, child)])
            else:
                found.extend(_parts(source, child))
        return found
    return [[EXPR, ""]]


def recognised(command: Command) -> str | None:
    """Which recognised command this is, if any."""
    words = [literal(word) for word in command.words]
    name, rest = words[0], words[1:]
    if name == "docker":
        if rest[:1] == ["build"] or rest[:2] in (["buildx", "build"], ["image", "build"]):
            return DOCKER_BUILD
        if rest[:1] == ["push"]:
            return DOCKER_PUSH
        if rest[:1] == ["tag"]:
            return DOCKER_TAG
    if name == "helm" and rest[:1] in (["upgrade"], ["install"]):
        return HELM
    if name == "kubectl" and rest[:1] == ["apply"]:
        return KUBECTL_APPLY
    return None


def delegated(command: Command) -> str | None:
    """The local script a top-level command hands the step to: ``./x``, or ``bash x.sh`` /
    ``sh x`` / ``source x`` with a relative path; ``None`` otherwise."""
    name = command.name
    if name is None:
        return None
    if name.startswith("./"):
        return name
    if name in _SCRIPT_RUNNERS and len(command.words) > 1:
        target = literal(command.words[1])
        if (
            target
            and not target.startswith(("-", "/"))
            and ("/" in target or target.endswith(".sh"))
        ):
            return target
    return None


def flag_values(command: Command, short: str | None, long: str) -> list[Word]:
    """Every value given to ``short`` / ``long`` (``-t x``, ``--tag x``, ``--tag=x``)."""
    found: list[Word] = []
    words = command.words
    for index, word in enumerate(words):
        text = literal(word)
        if text is not None and text in (short, long) and index + 1 < len(words):
            found.append(words[index + 1])
        elif word and word[0][0] == TEXT and word[0][1].startswith(long + "="):
            head = word[0][1][len(long) + 1 :]
            found.append(([[TEXT, head]] if head else []) + [list(p) for p in word[1:]])
    return found


def positionals(command: Command, start: int, valued: Sequence[str]) -> list[Word]:
    """The positional words from ``start``, skipping flags and the values of ``valued``."""
    found: list[Word] = []
    index = start
    words = command.words
    while index < len(words):
        text = literal(words[index])
        if text is not None and text.startswith("-"):
            index += 2 if text in valued else 1
            continue
        found.append(words[index])
        index += 1
    return found


def has_flag(command: Command, *flags: str) -> bool:
    return any(literal(word) in flags for word in command.words)
