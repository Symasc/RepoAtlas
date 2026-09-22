"""Deterministic query analysis (ARCHITECTURE.md §10 stage 1).

Deterministic throughout: the point is to recover the literal terms a question is
really about, so exact search can find them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from repoatlas.models import http

_QUOTED = re.compile(r"[\"'`]([^\"'`]{2,})[\"'`]")
_ENV_VAR = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
_DOTTED = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b")
#: A file path the question spells out, recognised by its extension.
#:
#: The extensions are every one discovery detects a language for, plus the
#: documentation a repository keeps beside its code. The list used to stop at a
#: handful of source formats, so ``docs/ARCHITECTURE.md`` was never read as a path
#: at all: only its basename reached file lookup, where a basename earns no rank
#: anchor, and the document the user named came seventh behind files that merely
#: mention it. Every indexed text file is inspectable whatever its extension; this
#: only decides which spellings a question is taken to have *named* as a path.
_PATHLIKE = re.compile(
    r"\b[\w./-]+\.(?:"
    r"py|pyi|ts|tsx|mts|cts|js|jsx|mjs|cjs|sh|bash|zsh|rb|rake|gemspec|sls|jinja2?|j2"
    r"|ya?ml|json|toml|md|markdown|mdx|rst|txt"
    r")\b"
)
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

#: ``PascalCaseName``: a capitalised run followed by at least one more. English
#: prose capitalises single words — `Identity`, `Learning`, `API` — and does not
#: weld two of them together, so a *compound* is code and a lone capitalised word
#: is not. That one boundary is why this pattern requires the second run.
#:
#: It exists because `_CAMEL_NAME` anchors on a lowercase first character, so
#: `UsersController` and `RubyTooling` matched no literal pattern at all and
#: reached exact lookup only as ordinary prose identifiers — indistinguishable
#: from `call` or `now`.
#:
#: Searched exactly and **never definitive**, for the same reason `camelCase` is
#: not: `TypeScript` and `RepoAtlas` have this shape too, and a case-sensitive miss
#: on one of those must not silence an otherwise answerable question.
_PASCAL_NAME = re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+\b")

#: A phrase that names what follows as a symbol: "the method named `detail`".
#:
#: Deliberately tiny, and deliberately not an intent classifier. It exists for one
#: distinction the shape rules cannot draw: "Find the symbol named call." is an
#: explicit lookup and "How do the apps call the services?" is prose, and the only
#: thing separating them is that the first one *says* the word is a name.
#:
#: The cue repeats, because English stacks them: "the **symbol named** call". One
#: cue per match would consume ``symbol``, nominate ``named``, and resume past the
#: word actually being pointed at.
#:
#: ``\s+`` after the alternation is what keeps this from firing on a longer word:
#: "which client **methods** exist" offers ``method`` no following space, so it
#: matches nothing.
_NAMING_CUE = re.compile(
    r"(?:\b(?:named|called|symbol|function|method|class|entity|variable)\s+)+"
    r"([A-Za-z_][A-Za-z0-9_]*)\b",
    re.IGNORECASE,
)

#: The cue words themselves, so `symbol named detail` does not nominate `named`.
_CUE_WORDS = frozenset(
    {"named", "called", "symbol", "function", "method", "class", "entity", "variable"}
)

#: A maximal run of word characters, optionally joined by the connectors that
#: appear inside real names. ``\w`` is Unicode-aware for ``str`` patterns, so this
#: matches identifiers and words in every script.
#:
#: It exists **alongside** the ASCII patterns above rather than replacing them.
#: Those encode genuine semantic distinctions — SCREAMING_CASE is an environment
#: variable, ``camelCase`` is a symbol but a weak one, a dashed word might just be
#: English — and each has established behaviour that V1_SPEC §14 depends on.
#: Rewriting them into one permissive ``\w+`` would generalize by discarding the
#: knowledge that makes exact search precise. So this pass is a *supplement*: it is
#: consulted only for tokens the ASCII patterns cannot see at all, which is exactly
#: the set that contains a non-ASCII character.
_WORD_RUN = re.compile(r"\w+(?:[.\-/][\w]+)*")


def _has_non_ascii(token: str) -> bool:
    return not token.isascii()


def _classify_unicode(token: str) -> str:
    """Which literal category a non-ASCII token belongs to.

    The rule is the *shape* rule the ASCII patterns already encode, applied through
    ``str`` methods instead of character ranges — `str.isupper` knows about every
    alphabet's casing, and a hand-written range knows about the ones its author
    remembered. `ÉCHEC_CONNEXION` is an environment variable for the same reason
    `SERVICE_HOST` is, and `créer_utilisateur` is a code name for the same reason
    `create_user` is: English prose does not put underscores inside words.
    """
    if "_" in token:
        return "env_var" if token.isupper() else "code_name"
    if "." in token:
        return "dotted"
    return "identifier"


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
    #: A lone non-ASCII token that is the whole question: a pasted literal.
    pasted_literals: tuple[str, ...] = ()
    #: Tokens the question points at as *names*, without their shape alone saying
    #: so: compound ``PascalCase``, a token a naming phrase introduces, and the
    #: token that **is** the whole question.
    #:
    #: Deliberately separate from :attr:`literal_terms`. These do not widen the
    #: content stage, do not name files, and never decide suppression — they only
    #: qualify a token for exact *entity-name* lookup. See :attr:`symbol_terms`.
    symbol_names: tuple[str, ...] = ()
    #: The pieces of a one-token question that :attr:`symbol_names` carries only
    #: *because* the question is one token — ``api`` and ``config`` of
    #: ``api/config.py``. They are looked up as entity names like any other symbol
    #: name; they are not names the question gave, so nothing reports them as such.
    token_parts: tuple[str, ...] = ()
    terms: tuple[str, ...] = field(default=())
    #: Exact HTTP route, deployment and endpoint keys the question spells out —
    #: ``GET /users`` is ``http-route:GET:/users`` (see :mod:`repoatlas.models.http`).
    #: Looked up only as entity keys: never searched as text, and deliberately not
    #: part of :attr:`literal_terms`, so they neither widen the lexical stages nor
    #: decide suppression.
    http_keys: tuple[str, ...] = field(default=())

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
            *self.pasted_literals,
            *self.ambiguous_packages,
        ):
            seen.setdefault(term, None)
        return tuple(seen)

    @property
    def symbol_terms(self) -> tuple[str, ...]:
        """Tokens that may be looked up as an exact entity name *deterministically*.

        The funnel used to offer every word of a question to exact entity-name
        lookup and mark whatever came back `DETERMINISTIC` — the strongest tier,
        ahead of every contextual candidate, and a graph seed besides. So "How do
        the frontend apps call the Learning and Identity services?" was answered
        with whatever happened to be named ``call`` and ``services``, and "what are
        the API endpoints in Identity right now?" produced fifteen deterministic
        matches for ``now``.

        The words were never the problem. Promoting them was. A question can
        mention ``call`` without asking about a symbol called ``call``, and the
        difference is observable without understanding the sentence:

        * it is **code-shaped** — everything in :attr:`literal_terms`, plus the
          compound ``PascalCase`` that `_CAMEL_NAME` cannot see;
        * a **naming phrase introduces it** — "the symbol named ``call``";
        * it **is** the whole question, which is a lookup rather than prose. That
          rule is not new: :attr:`definite_terms` already makes exactly this
          judgement for a dashed token.

        Everything else is still searched and still returned — a symbol genuinely
        named ``call`` remains findable — as recall rather than as a fact about
        what the question named. No word is excluded, and nothing is demoted for
        being short.
        """
        seen: dict[str, None] = {}
        for term in (*self.literal_terms, *self.symbol_names):
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
            *self.pasted_literals,
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

    # The Unicode supplement. Only tokens the ASCII patterns cannot see are
    # considered, so every established ASCII behaviour above is untouched.
    seen_ascii = " ".join((*quoted, *paths, *dotted, *env_vars, *packages, *code_names, *ambiguous))
    extra_env: list[str] = []
    extra_code: list[str] = []
    extra_dotted: list[str] = []
    extra_ids: list[str] = []
    for token in _WORD_RUN.findall(question):
        if not _has_non_ascii(token) or token in seen_ascii:
            continue
        bucket = _classify_unicode(token)
        if bucket == "env_var":
            extra_env.append(token)
        elif bucket == "code_name":
            extra_code.append(token)
        elif bucket == "dotted":
            extra_dotted.append(token)
        else:
            extra_ids.append(token)

    # A lone non-ASCII token that is the entire question is a pasted literal, not
    # prose — the same judgement `definite_terms` already makes for a dashed token
    # that is the whole query. The asymmetry with ASCII is deliberate and worth
    # naming: a bare ASCII word like `connect` is filtered by a stopword list and
    # shape rules that model *English*, and no such model exists for Greek,
    # Japanese or Arabic. Absent one, a lone foreign token is far more likely to be
    # a pasted error message than a filler word, and treating it as a lookup is
    # what makes `debug "接続できませんでした"` work at all.
    #
    # It cannot manufacture evidence: a literal term is resolved by
    # `search_literal`, which verifies with a Python substring test on the stored
    # chunk text, so a term that is not byte-present yields nothing.
    stripped = question.strip()
    pasted = (
        (stripped,)
        if stripped and _has_non_ascii(stripped) and _WORD_RUN.fullmatch(stripped)
        else ()
    )

    env_vars = tuple(dict.fromkeys((*env_vars, *extra_env)))
    code_names = tuple(dict.fromkeys((*code_names, *extra_code)))
    dotted = tuple(dict.fromkeys((*dotted, *extra_dotted)))
    identifiers = tuple(dict.fromkeys((*identifiers, *extra_ids)))

    terms = tuple(
        dict.fromkeys(
            (*quoted, *env_vars, *paths, *dotted, *packages, *code_names, *ambiguous, *identifiers)
        )
    )

    # Tokens the question points at as names. Three sources, each answering "did
    # the question mean this as an identifier?" without reading the sentence.
    #
    # A cue word is never its own nomination: "the symbol named detail" offers
    # `detail`, not `named`. Nor is a stopword, which is filler wherever it sits.
    cued = tuple(
        dict.fromkeys(
            token
            for token in (match.group(1) for match in _NAMING_CUE.finditer(question))
            if token.lower() not in _CUE_WORDS and token.lower() not in _STOPWORDS
        )
    )
    pascal = tuple(dict.fromkeys(_PASCAL_NAME.findall(question)))
    # A question that is one token is a lookup, not prose — the judgement
    # `definite_terms` already makes for a lone dashed token. This is what keeps
    # `locate config` and `locate UsersController` exact, and it is deliberately
    # limited to the *whole* query: three words of prose are still prose.
    #
    # Its parts come with it. `locate api/config.py` is one token, and `api` and
    # `config` are pieces of the identifier the user typed rather than words
    # lifted out of a sentence — the module inside the named file really is
    # called `config`. Nothing here reaches a multi-word question, where those
    # same pieces would be prose.
    lone = (stripped, *identifiers) if stripped and _WORD_RUN.fullmatch(stripped) else ()
    symbol_names = tuple(dict.fromkeys((*pascal, *cued, *lone)))
    token_parts = tuple(
        dict.fromkeys(part for part in lone[1:] if part not in (stripped, *pascal, *cued))
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
        pasted_literals=pasted,
        symbol_names=symbol_names,
        token_parts=token_parts,
        terms=terms,
        http_keys=http.query_keys(question),
    )
