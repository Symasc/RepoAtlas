"""Deterministic query analysis (ARCHITECTURE.md §10 stage 1).

No LLM is involved. The point is to recover the literal terms a question is really
about, so exact search can find them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_QUOTED = re.compile(r"[\"'`]([^\"'`]{2,})[\"'`]")
_ENV_VAR = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
_DOTTED = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b")
_PATHLIKE = re.compile(r"\b[\w./-]+\.(?:py|ts|tsx|js|sh|rb|sls|ya?ml|json|toml)\b")
_IDENTIFIER = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")

#: Question words and filler that would only add noise to a lexical search.
_STOPWORDS = frozenset(
    {
        "where",
        "what",
        "which",
        "when",
        "why",
        "how",
        "who",
        "the",
        "this",
        "that",
        "these",
        "those",
        "is",
        "are",
        "was",
        "were",
        "does",
        "do",
        "did",
        "can",
        "could",
        "should",
        "would",
        "will",
        "and",
        "or",
        "but",
        "not",
        "for",
        "from",
        "with",
        "into",
        "onto",
        "configured",
        "configure",
        "defined",
        "define",
        "implemented",
        "implement",
        "value",
        "values",
        "come",
        "comes",
        "coming",
        "get",
        "gets",
        "set",
        "code",
        "file",
        "files",
        "repo",
        "repository",
        "service",
        "used",
        "use",
    }
)


@dataclass(frozen=True, slots=True)
class QueryAnalysis:
    """What a question is literally asking about."""

    text: str
    quoted: tuple[str, ...] = ()
    env_vars: tuple[str, ...] = ()
    dotted: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    identifiers: tuple[str, ...] = ()
    terms: tuple[str, ...] = field(default=())

    @property
    def literal_terms(self) -> tuple[str, ...]:
        """Terms that must be matched exactly, never approximated."""
        seen: dict[str, None] = {}
        for term in (*self.quoted, *self.env_vars, *self.paths, *self.dotted):
            seen.setdefault(term, None)
        return tuple(seen)


def analyze(question: str) -> QueryAnalysis:
    """Extract literal search terms from free text."""
    quoted = tuple(m.group(1).strip() for m in _QUOTED.finditer(question))
    env_vars = tuple(dict.fromkeys(_ENV_VAR.findall(question)))
    dotted = tuple(dict.fromkeys(_DOTTED.findall(question)))
    paths = tuple(dict.fromkeys(_PATHLIKE.findall(question)))

    identifiers = tuple(
        dict.fromkeys(
            token for token in _IDENTIFIER.findall(question) if token.lower() not in _STOPWORDS
        )
    )

    terms = tuple(dict.fromkeys((*quoted, *env_vars, *paths, *dotted, *identifiers)))

    return QueryAnalysis(
        text=question,
        quoted=quoted,
        env_vars=env_vars,
        dotted=dotted,
        paths=paths,
        identifiers=identifiers,
        terms=terms,
    )
