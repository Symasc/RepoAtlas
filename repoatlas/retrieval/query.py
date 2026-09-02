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

#: A scoped npm package: `@company/common`. Unambiguous — English prose does not
#: contain this shape, so its absence from the index is proof the question cannot
#: be answered.
_SCOPED_PACKAGE = re.compile(r"@[a-z0-9][\w.-]*/[a-z0-9][\w.-]*", re.IGNORECASE)

#: A bare dashed package name: `common-lib`, `left-pad`, `shared-utils`. V1_SPEC
#: §14 requires exact search to work for package names, so these are searched
#: literally — but they are **not** definitive (see `QueryAnalysis.definite_terms`),
#: because `auto-scaling` in an English sentence has exactly the same shape.
_DASHED_NAME = re.compile(r"\b[a-z0-9]+(?:[-][a-z0-9]+)+\b", re.IGNORECASE)

#: `snake_case_name`. English does not use underscores inside words, so a token
#: shaped like this is code, and its absence from the index is proof — the same
#: standing an ALL_CAPS environment variable already has.
_SNAKE_NAME = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b")

#: `camelCaseName`. Searched exactly, because V1_SPEC §14 requires symbol names to
#: be findable — but never definitive: `TypeScript`, `RepoAtlas` and `JavaScript`
#: have the same shape, and a case-sensitive miss on one of those must not silence
#: an otherwise answerable question.
_CAMEL_NAME = re.compile(r"\b[a-z][a-z0-9]*(?:[A-Z][a-z0-9]*)+\b")

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
    packages: tuple[str, ...] = ()
    #: Unmistakably code-shaped: `snake_case`, which English never produces.
    code_names: tuple[str, ...] = ()
    #: Only *probably* names — a dashed or camelCase token that prose also produces.
    ambiguous_packages: tuple[str, ...] = ()
    terms: tuple[str, ...] = field(default=())

    @property
    def literal_terms(self) -> tuple[str, ...]:
        """Terms that must be matched exactly, never approximated.

        Package names are included because V1_SPEC §14 names them explicitly among
        the things exact search must work for: splitting `@company/common` into
        `company` and `common` turns a precise lookup into a word search.
        """
        seen: dict[str, None] = {}
        for term in (
            *self.quoted,
            *self.env_vars,
            *self.paths,
            *self.dotted,
            *self.packages,
            *self.code_names,
            *self.ambiguous_packages,
        ):
            seen.setdefault(term, None)
        return tuple(seen)

    @property
    def definite_terms(self) -> tuple[str, ...]:
        """Literals whose absence proves the question is unanswerable.

        A subset of :attr:`literal_terms`. The distinction matters because
        suppression is aggressive: when nothing named exists, the whole associative
        half of the funnel goes silent. That is right for `SERVICE_HOST`, a quoted
        error string, a path or `@company/common` — shapes English prose does not
        produce by accident.

        It is wrong for a bare dashed word: "where is auto-scaling handled?" would
        otherwise return nothing at all merely because no file contains that exact
        hyphenation. Such a token is still searched exactly; it just does not get
        to declare the question unanswerable. A dashed token that *is* the entire
        question is definite, because a one-token query is a lookup, not prose.
        """
        seen: dict[str, None] = {}
        for term in (
            *self.quoted,
            *self.env_vars,
            *self.paths,
            *self.dotted,
            *self.packages,
            *self.code_names,
        ):
            seen.setdefault(term, None)
        if not seen and self.ambiguous_packages and self.text.strip() in self.ambiguous_packages:
            seen.setdefault(self.text.strip(), None)
        return tuple(seen)


def analyze(question: str) -> QueryAnalysis:
    """Extract literal search terms from free text."""
    quoted = tuple(m.group(1).strip() for m in _QUOTED.finditer(question))
    env_vars = tuple(dict.fromkeys(_ENV_VAR.findall(question)))
    dotted = tuple(dict.fromkeys(_DOTTED.findall(question)))
    paths = tuple(dict.fromkeys(_PATHLIKE.findall(question)))

    packages = tuple(dict.fromkeys(_SCOPED_PACKAGE.findall(question)))
    already = " ".join((*paths, *dotted, *env_vars))
    code_names = tuple(
        dict.fromkeys(
            token
            for token in _SNAKE_NAME.findall(question)
            if token not in already and token not in env_vars
        )
    )
    camel = tuple(
        dict.fromkeys(token for token in _CAMEL_NAME.findall(question) if token not in already)
    )
    # A dashed token already covered by a path or a scoped name is not a separate
    # package: `src/left-pad.ts` is one path, not a path and a package.
    covered = " ".join((*paths, *packages))
    dashed = tuple(
        dict.fromkeys(token for token in _DASHED_NAME.findall(question) if token not in covered)
    )
    ambiguous = tuple(dict.fromkeys((*dashed, *camel)))

    identifiers = tuple(
        dict.fromkeys(
            token for token in _IDENTIFIER.findall(question) if token.lower() not in _STOPWORDS
        )
    )

    terms = tuple(
        dict.fromkeys(
            (*quoted, *env_vars, *paths, *dotted, *packages, *code_names, *ambiguous, *identifiers)
        )
    )

    return QueryAnalysis(
        text=question,
        quoted=quoted,
        env_vars=env_vars,
        dotted=dotted,
        paths=paths,
        identifiers=identifiers,
        packages=packages,
        code_names=code_names,
        ambiguous_packages=ambiguous,
        terms=terms,
    )
