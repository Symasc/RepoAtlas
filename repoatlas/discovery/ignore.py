"""Ignore rules (V1_SPEC §5, §17).

Two systems live here, and keeping them apart is the point:

**User preference** — ``.repoatlasignore``, in :class:`IgnoreRules`. gitignore-like
semantics, including negation, and **case-sensitive on every platform**, because
that is what gitignore does on a case-sensitive filesystem and what the user
wrote the file expecting.

This is a RepoAtlas language, so its meaning comes from RepoAtlas and not from
the host: an ignore decision is a function of the pattern and the canonical
repository-relative path, and of nothing else. Separators are ``/``, a backslash
is an ordinary path character, and matching consults no filesystem, no locale
and no platform path conventions. See :func:`_compile` for what that cost, and
what it replaced.

**Security policy** — the built-in obvious-secret filenames, in
:mod:`repoatlas.security`. Not a preference: it cannot be negated, cannot be
configured off, and is matched **case-insensitively**, because ``.ENV`` is a
dotenv file and a rule that says otherwise is not a security boundary. It lives
one layer down because enumeration is not its only caller — the git working-tree
comparison must refuse to read these files too.

Precedence at enumeration is therefore: the secret policy first and
unconditionally, then built-in directory exclusions, ``.gitignore`` (via
``git ls-files``) and ``.repoatlasignore``. Applying the security check first is
not an optimization — it is what makes "a user cannot re-admit a secret"
structural rather than a happy accident of ordering (ARCHITECTURE.md §12).

Every check runs on the path **before any content is read**, so an excluded file
is never opened, parsed, chunked, embedded, retrieved, serialized into a prompt,
or logged.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from repoatlas.security import is_secret_path

IGNORE_FILENAME = ".repoatlasignore"

#: Ceiling on a ``.repoatlasignore``. A pattern file is tens of lines; a megabyte
#: of them is already far past anything meaningful. It is read under a bound
#: because it is the *earliest* content read of a sync — before any file has been
#: classified — so an unbounded read here would exhaust memory before the size cap
#: protecting everything else was ever consulted. Deliberately its own constant
#: rather than ``indexing.max_file_bytes``: this file is parsed for patterns and
#: never indexed, so raising the content cap must not silently raise this one.
MAX_IGNORE_BYTES = 1 << 20

#: Re-exported so ``discovery`` stays the natural place to ask about ignoring,
#: while the policy itself lives below every layer that must obey it.
__all__ = ["IGNORE_FILENAME", "MAX_IGNORE_BYTES", "IgnoreRules", "is_secret_path"]


def _compile(pattern: str) -> re.Pattern[str]:
    """One glob pattern as a host-independent regular expression.

    This exists because ``fnmatch.fnmatch`` does not. It runs both the name and
    the pattern through ``os.path.normcase``, which is ``ntpath.normcase`` on
    Windows — and that does two things to a ``.repoatlasignore`` pattern:
    lowercases it, and rewrites ``/`` to ``\\``. The same pattern file and the
    same repository therefore produced *different indexes on different
    operating systems*: ``Src/**`` ignored ``src/main.py`` on Windows and not on
    Linux, ``[A-Z]*.py`` silently became ``[a-z]*.py``, a file literally named
    ``foo\\bar.py`` matched the pattern ``foo/bar.py``, and a ``!Keep.py``
    negation re-admitted ``keep.py``. None of that was chosen; it was inherited.

    ``fnmatch.translate`` is the half of ``fnmatch`` that is pure text: glob
    syntax to regex, no ``os`` module anywhere in it. Compiling its output
    ourselves keeps the wildcard language exactly as it was — ``*``, ``?``,
    ``[...]``, ``[!...]``, an unterminated ``[`` matched literally — and drops
    only the host normalization. No ``re.IGNORECASE``, no ``re.LOCALE``: the
    answer is a function of the two strings and nothing else.

    Compiled once per rule when the pattern file is parsed, not once per
    candidate path. The set is bounded by ``MAX_IGNORE_BYTES``.
    """
    return re.compile(fnmatch.translate(pattern))


@dataclass(frozen=True, slots=True)
class _Rule:
    pattern: str
    #: ``pattern`` compiled. Carried on the rule so that matching a repository's
    #: worth of paths compiles nothing, and so that the only matching primitive
    #: in this module is one that cannot consult the host.
    regex: re.Pattern[str]
    negated: bool
    directory_only: bool
    anchored: bool


def _parse_line(line: str) -> _Rule | None:
    stripped = line.rstrip("\n").rstrip()
    if not stripped or stripped.startswith("#"):
        return None
    negated = stripped.startswith("!")
    if negated:
        stripped = stripped[1:]
    directory_only = stripped.endswith("/")
    stripped = stripped.rstrip("/")
    anchored = stripped.startswith("/")
    stripped = stripped.lstrip("/")
    if not stripped:
        return None
    return _Rule(
        pattern=stripped,
        regex=_compile(stripped),
        negated=negated,
        directory_only=directory_only,
        anchored=anchored,
    )


class IgnoreRules:
    """gitignore-like matching for ``.repoatlasignore`` plus secret patterns."""

    def __init__(self, rules: list[_Rule] | None = None) -> None:
        self._rules = rules or []

    @classmethod
    def from_text(cls, text: str) -> IgnoreRules:
        rules = [rule for line in text.splitlines() if (rule := _parse_line(line)) is not None]
        return cls(rules)

    @classmethod
    def load(cls, directory: Path) -> IgnoreRules:
        """Read ``.repoatlasignore`` from ``directory``, if present.

        This is the file at the **workspace root**, above every repository, so it
        is the operator's own configuration rather than repository content and is
        not subject to the containment boundary. It is still read under a byte
        ceiling: the root is a directory full of untrusted repositories, the size
        of anything in it is not RepoAtlas's to assume, and a bound costs nothing.
        """
        target = directory / IGNORE_FILENAME
        if not target.is_file():
            return cls()
        try:
            with target.open("rb") as handle:
                raw = handle.read(MAX_IGNORE_BYTES + 1)
        except OSError:
            return cls()
        if len(raw) > MAX_IGNORE_BYTES:
            return cls()
        return cls.from_text(raw.decode("utf-8", errors="replace"))

    def matches(self, rel_path: str, *, is_dir: bool = False) -> bool:
        """Whether ``rel_path`` (POSIX, repository-relative) is ignored."""
        decision = False
        for rule in self._rules:
            if self._rule_matches(rule, rel_path, is_dir=is_dir):
                decision = not rule.negated
        return decision

    @staticmethod
    def _rule_matches(rule: _Rule, rel_path: str, *, is_dir: bool) -> bool:
        pattern = rule.pattern
        parts = PurePosixPath(rel_path).parts

        if rule.directory_only:
            # ``generated/`` ignores the directory *and everything beneath it*, so
            # a file path matches when any ancestor component matches. The final
            # component only counts when the path is itself a directory.
            ancestors = parts if is_dir else parts[:-1]
            if rule.anchored or "/" in pattern:
                prefix = pattern.rstrip("/")
                return rel_path == prefix or rel_path.startswith(f"{prefix}/")
            return any(rule.regex.match(part) is not None for part in ancestors)

        if rule.anchored or "/" in pattern:
            if rule.regex.match(rel_path) is not None:
                return True
            # An anchored pattern also covers everything beneath a matching path.
            return rel_path.startswith(f"{pattern}/")

        # Unanchored patterns match any path component, as gitignore does.
        return any(rule.regex.match(part) is not None for part in parts)
