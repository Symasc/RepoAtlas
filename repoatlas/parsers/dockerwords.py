"""Static-proof classification of Dockerfile operands (V1_SPEC §2, §7.8).

One question, asked in one place: **does the Dockerfile source name this value,
or does something evaluate it later?** Every instruction handler asks it here
instead of inspecting raw strings itself, because the answer is not a property of
the text — it is a property of the text *under that instruction's semantics*.

Docker splits the instructions in two, and the split is the whole design:

**Builder-expanded** — ``ADD``, ``COPY``, ``ENV``, ``EXPOSE``, ``FROM``,
``LABEL``, ``STOPSIGNAL``, ``USER``, ``VOLUME``, ``WORKDIR``, ``ONBUILD``. The
builder substitutes ``$VAR`` before the instruction runs, in shell form **and**
inside a JSON array. ``COPY ["$SRC", "/app/"]`` copies whatever ``$SRC`` names.

**Shell-expanded** — ``RUN``, ``CMD``, ``ENTRYPOINT``. These support variables
too, but the reference is precise about who does it: "substitution is handled by
the command shell rather than the builder". The exec form does not invoke a
shell, so **nothing expands there at all** — every element is the literal string
written down.

That asymmetry is why this module exists rather than a rule that rejects any word
containing ``$``. Such a rule is wrong in both directions:

* it would keep claiming ``COPY ["$SRC", "/app/"]`` names no file when the
  builder plainly resolves it — no, worse: it would *suppress* nothing there and
  suppress the wrong things elsewhere;
* it would refuse ``RUN ["$TOOL", "app.py"]``, where ``$TOOL`` is genuinely the
  program name. Verified against Docker 29.7.2: that instruction fails at build
  time with ``exec: "$TOOL": executable file not found in $PATH``, which is the
  container trying to run a program *called* ``$TOOL``. Recording it is truthful;
  suppressing it would be a cosmetic fix that discards a real fact.

It is also why quote and escape handling is done here rather than by
:func:`shlex.split`. ``shlex`` unescapes ``\\$FOO`` to ``$FOO`` and then nothing
downstream can tell that literal apart from an expansion — the escaped form and
the expanded form arrive spelled identically. Both were checked against a real
build: ``COPY \\$FOO /quux`` copies the file named ``$FOO``, and
``COPY $SRC /app`` copies whatever ``$SRC`` resolves to. Proof has to be decided
while the syntax is still visible, exactly as the Bash parser decides it from the
syntax tree rather than from the text (ARCHITECTURE.md §5).
"""

from __future__ import annotations

import enum
import json
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "BUILDER_EXPANDED",
    "Proof",
    "SourceDomain",
    "Word",
    "exec_form",
    "expanded",
    "source_domain",
    "split_flags",
    "split_words",
    "verbatim",
]

#: Instructions whose operands the *builder* expands, in either form.
BUILDER_EXPANDED = frozenset(
    {
        "ADD",
        "COPY",
        "ENV",
        "EXPOSE",
        "FROM",
        "LABEL",
        "ONBUILD",
        "STOPSIGNAL",
        "USER",
        "VOLUME",
        "WORKDIR",
    }
)

#: Characters that begin a variable name after ``$``.
_NAME_START = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz_")
_NAME_BODY = _NAME_START | frozenset("0123456789")

#: Escapes honoured inside a double-quoted word. Everything else keeps its
#: backslash, which is why this is not simply "the next character".
_DOUBLE_QUOTED_ESCAPES = frozenset('$`"\\\n')


class Proof(enum.StrEnum):
    """Whether the source fixes a value, or something else decides it later."""

    LITERAL = "literal"
    COMPUTED = "computed"


class SourceDomain(enum.StrEnum):
    """Which filesystem a ``COPY``/``ADD`` source operand names.

    Static proof has a second half, and only the first was ever asked. ``COPY``
    proves *what a source is called*; it does not by itself prove *where that
    name is resolved*. Docker's grammar is::

        COPY [--from=<image|stage|context>] <src> ... <dest>

    Without ``--from`` the sources are resolved in the **build context**, which
    is the tree RepoAtlas indexes, so a repository file may genuinely be the
    thing named. With ``--from`` they are resolved in the root filesystem of
    another build stage, another image, or a named build context — none of which
    RepoAtlas has, reads, or can read. A string that happens to be spelled the
    same in both places is a coincidence, not a match.

    Two values, not four, and deliberately. "Named stage", "numeric stage",
    "external image" and "named context" differ in *how Docker resolves them*,
    and RepoAtlas resolves none of them: the fact it needs is the one they share,
    which is that the build context is not where the source lives. A dynamic
    ``--from=${STAGE}`` collapses into the same answer for the same reason — the
    stage is less knowable, not more.
    """

    #: No ``--from``: the sources are build-context paths, so they may name
    #: repository files and are offered to the linker as file references.
    BUILD_CONTEXT = "build-context"

    #: ``--from`` is present: the sources name paths in a filesystem RepoAtlas
    #: does not have. No repository file reference may be emitted for them.
    FOREIGN = "foreign"


@dataclass(frozen=True, slots=True)
class Word:
    """One operand, and whether the Dockerfile proves what it is.

    ``value`` is the operand with its quoting and escaping removed — the name a
    target would actually have — and is ``None`` exactly when the value is
    computed. ``raw`` is the source text, kept because several rules must read
    the *spelling* rather than the value: a flag is recognised by its leading
    ``--`` even when its argument is an expansion, and a redirection is
    recognised by its leading ``<`` even though it is not a word at all.
    """

    proof: Proof
    value: str | None
    raw: str

    @property
    def literal(self) -> str | None:
        """The proven value, or ``None`` when the target identity is not proven."""
        return self.value if self.proof is Proof.LITERAL else None


def _literal(value: str, raw: str) -> Word:
    return Word(proof=Proof.LITERAL, value=value, raw=raw)


def _computed(raw: str) -> Word:
    return Word(proof=Proof.COMPUTED, value=None, raw=raw)


def verbatim(text: str) -> Word:
    """A string nothing expands: an exec-form element of RUN/CMD/ENTRYPOINT.

    JSON decoding has already removed the only syntax present, so what is left is
    the literal argument the kernel receives — dollars included.
    """
    return _literal(text, text)


def expanded(text: str) -> Word:
    """Builder environment replacement over an already-unquoted string.

    Used for the JSON form of ``COPY``/``ADD``, where JSON supplied the quoting
    and the builder still substitutes. ``\\$`` is the documented literal-dollar
    escape and is the only one applied: inventing further escapes here could
    corrupt a real path, and every other backslash reaches the filesystem as
    written.
    """
    pieces: list[str] = []
    index = 0
    while index < len(text):
        character = text[index]
        if character == "\\" and index + 1 < len(text) and text[index + 1] == "$":
            pieces.append("$")
            index += 2
            continue
        if character == "$":
            return _computed(text)
        pieces.append(character)
        index += 1
    return _literal("".join(pieces), text)


def exec_form(arguments: str) -> list[str] | None:
    """The JSON argument vector, or ``None`` when this is not one.

    Docker requires a well-formed array of strings and **falls back to shell
    form** when it does not get one — the reference calls this out for Windows
    paths, where an unescaped backslash produces invalid JSON and the instruction
    is then run through a shell. Returning ``None`` reproduces that, rather than
    salvaging a half-parsed array into target keys such as
    ``[src/file with space.txt,`` which the hand-rolled comma split used to
    produce.
    """
    stripped = arguments.strip()
    if not stripped.startswith("[") or not stripped.endswith("]"):
        return None
    try:
        loaded = json.loads(stripped)
    except ValueError:
        return None
    if not isinstance(loaded, list) or not all(isinstance(part, str) for part in loaded):
        return None
    return loaded


def split_flags(arguments: str) -> tuple[list[str], str]:
    """Peel the leading ``--flag`` options off an instruction's arguments.

    Options come before the operands and before any JSON array, so they are
    removed by spelling rather than by value: ``--from=$STAGE`` is an option
    whether or not ``$STAGE`` is knowable, and treating it as an operand would
    put the expression back into the operand list it was removed from.
    """
    flags: list[str] = []
    rest = arguments.lstrip()
    while rest.startswith("--"):
        end = 0
        while end < len(rest) and not rest[end].isspace():
            end += 1
        flags.append(rest[:end])
        rest = rest[end:].lstrip()
    return flags, rest


def source_domain(flags: Sequence[str]) -> SourceDomain:
    """Where this ``COPY``/``ADD`` resolves its sources, decided from its flags.

    Asked once, of the flags :func:`split_flags` already separated, so no handler
    inspects a raw line for ``--from`` and no operand can be classified by a
    different rule than its siblings. Every source operand of one instruction
    shares one domain: ``--from`` is an instruction-level flag, so
    ``COPY --from=builder a.txt b.txt /dest/`` relocates *both* sources.

    Matched on the flag's **name**, so neither ``--chown=1000 --from=builder``
    nor a ``--from`` whose value is an expansion can slip past by position or by
    being unknowable. Docker documents ``--from`` for ``COPY`` only; ``ADD``
    shares this classifier anyway, because if a Dockerfile writes ``ADD --from``
    the conservative reading costs a reference RepoAtlas cannot justify and the
    permissive one invents the same false fact this exists to prevent.
    """
    for flag in flags:
        name = flag.split("=", 1)[0]
        if name == "--from":
            return SourceDomain.FOREIGN
    return SourceDomain.BUILD_CONTEXT


def split_words(arguments: str, *, substitution: bool = False) -> list[Word]:
    """Tokenize shell-form arguments, classifying each word as it is built.

    Quoting rules follow the shell the builder emulates: single quotes suspend
    expansion entirely, double quotes suspend word splitting but not expansion,
    and a backslash escapes the next character. Any expansion reached outside
    single quotes makes the whole word computed — including the tail, because a
    word is one target and half a target is not a smaller claim, it is a
    different and false one (``${BASE}/config.yaml`` is not evidence about a file
    called ``config.yaml``).

    ``substitution`` adds backtick command substitution, which is shell syntax
    and therefore applies to ``RUN``/``CMD``/``ENTRYPOINT`` shell form only. For
    a builder-expanded instruction a backtick is an ordinary filename character,
    and rejecting it there would suppress a target the source does name.
    """
    words: list[Word] = []
    index = 0
    length = len(arguments)

    while index < length:
        while index < length and arguments[index].isspace():
            index += 1
        if index >= length:
            break

        start = index
        pieces: list[str] = []
        computed = False

        while index < length and not arguments[index].isspace():
            character = arguments[index]

            if character == "'":
                index += 1
                while index < length and arguments[index] != "'":
                    pieces.append(arguments[index])
                    index += 1
                index += 1  # closing quote, or end of input on an unbalanced word
                continue

            if character == '"':
                index += 1
                while index < length and arguments[index] != '"':
                    inner = arguments[index]
                    if inner == "\\" and index + 1 < length:
                        following = arguments[index + 1]
                        if following in _DOUBLE_QUOTED_ESCAPES:
                            pieces.append(following)
                            index += 2
                            continue
                        pieces.append(inner)
                        index += 1
                        continue
                    if inner == "$" or (substitution and inner == "`"):
                        computed = True
                        index = _skip_expansion(arguments, index, substitution=substitution)
                        continue
                    pieces.append(inner)
                    index += 1
                index += 1
                continue

            if character == "\\" and index + 1 < length:
                pieces.append(arguments[index + 1])
                index += 2
                continue

            if character == "$" or (substitution and character == "`"):
                computed = True
                index = _skip_expansion(arguments, index, substitution=substitution)
                continue

            pieces.append(character)
            index += 1

        raw = arguments[start:index]
        words.append(_computed(raw) if computed else _literal("".join(pieces), raw))

    return words


def _skip_expansion(text: str, index: int, *, substitution: bool) -> int:
    """Consume one expansion, returning the index just past it.

    The value is already known to be unusable, so this exists only to keep *word
    boundaries* right: ``${VAR:-a b}`` is one operand, and a scan that stopped at
    the space would turn one computed word into a computed word plus a spurious
    literal ``b}``. Operand position is what identifies a ``COPY`` destination or
    a ``FROM`` alias, and position must survive a value that does not.
    """
    length = len(text)
    if text[index] == "`" and substitution:
        index += 1
        while index < length and text[index] != "`":
            index += 1
        return min(index + 1, length)

    index += 1  # the dollar
    if index >= length:
        return index
    opener = text[index]
    if opener in {"{", "("}:
        closer = "}" if opener == "{" else ")"
        depth = 0
        while index < length:
            if text[index] == opener:
                depth += 1
            elif text[index] == closer:
                depth -= 1
                if depth == 0:
                    return index + 1
            index += 1
        return index
    if opener in _NAME_START:
        while index < length and text[index] in _NAME_BODY:
            index += 1
        return index
    # A `$` that begins no name — `tool$`, `a$-b` — is a literal dollar to both
    # the builder and the shell, so marking it computed loses a target rather
    # than inventing one. Failing closed on the rarer spelling is the trade this
    # module exists to make.
    return index
