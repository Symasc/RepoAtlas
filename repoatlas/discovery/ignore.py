"""Ignore rules (V1_SPEC §5, §17).

Precedence, most specific last::

    built-in defaults -> .gitignore (via git ls-files) -> .repoatlasignore -> secrets

Secret patterns are applied last and unconditionally, at enumeration time — before
any content is read — so excluded content cannot reach chunks, embeddings, prompts
or logs (V1_SPEC §17).
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from repoatlas.config.settings import SECRET_PATTERNS

IGNORE_FILENAME = ".repoatlasignore"


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


def is_secret_path(rel_path: str) -> bool:
    """Whether a path matches a known secret pattern (V1_SPEC §17)."""
    name = PurePosixPath(rel_path).name
    return any(fnmatch.fnmatch(name, pattern) for pattern in SECRET_PATTERNS)
