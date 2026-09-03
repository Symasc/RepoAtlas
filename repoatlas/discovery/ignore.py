"""Ignore rules (V1_SPEC §5, §17).

Two systems live here, and keeping them apart is the point:

**User preference** — ``.repoatlasignore``, in :class:`IgnoreRules`. gitignore-like
semantics, including negation, and **case-sensitive**, because that is what
gitignore does on a case-sensitive filesystem and what the user wrote the file
expecting.

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
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from repoatlas.security import is_secret_path

IGNORE_FILENAME = ".repoatlasignore"

#: Re-exported so ``discovery`` stays the natural place to ask about ignoring,
#: while the policy itself lives below every layer that must obey it.
__all__ = ["IGNORE_FILENAME", "IgnoreRules", "is_secret_path"]


@dataclass(frozen=True, slots=True)
class _Rule:
    pattern: str
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
        pattern=stripped, negated=negated, directory_only=directory_only, anchored=anchored
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
        """Read ``.repoatlasignore`` from ``directory``, if present."""
        target = directory / IGNORE_FILENAME
        if not target.is_file():
            return cls()
        try:
            return cls.from_text(target.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return cls()

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
            return any(fnmatch.fnmatch(part, pattern) for part in ancestors)

        if rule.anchored or "/" in pattern:
            if fnmatch.fnmatch(rel_path, pattern):
                return True
            # An anchored pattern also covers everything beneath a matching path.
            return rel_path.startswith(f"{pattern}/")

        # Unanchored patterns match any path component, as gitignore does.
        return any(fnmatch.fnmatch(part, pattern) for part in parts)
