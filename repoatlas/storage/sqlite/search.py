"""FTS5-backed exact search (V1_SPEC §14, ARCHITECTURE.md §3).

FTS provides recall; a literal substring check provides precision. No tokenizer is
exact for every query shape, so a hit is only reported as *exact* once the literal
term has been found in the chunk text.

Two things exact search must never depend on, and both were once true here:

* **the tokenizer** — hence :meth:`search_literal`, which verifies by substring;
* **the chunk boundary** — hence its seam pass. Chunk size is an evidence-sizing
  decision, and for a while it silently decided discoverability too: a literal
  straddling the character cap existed in no single chunk, so a file the index was
  holding was reported not to contain text it plainly did.

File identity is searchable for the same reason. ``files.rel_path`` is a fact
about the repository, so :meth:`find_files_by_path` retrieves it exactly, rather
than leaving ``src/config.py`` findable only if some chunk happened to mention it.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence

from repoatlas.models.parsing import EntityType
from repoatlas.storage.protocols import ChunkHit, EntityHit, FileHit
from repoatlas.storage.sqlite.database import Database

#: What counts as one searchable token, and it must agree with the tokenizer that
#: **built** the index — ``unicode61 remove_diacritics 2 tokenchars '_.-@$'``.
#:
#: ``\w`` rather than ``A-Za-z0-9_``: for ``str`` patterns Python 3 makes ``\w``
#: Unicode-aware, so this is a strict superset of the old class and every ASCII
#: token splits exactly as before. The old class did not merely *drop* non-ASCII
#: text, which would have been survivable — it **shredded** it, turning
#: ``créer_utilisateur`` into ``cr`` and ``er_utilisateur``: two terms that are not
#: in the source, searched instead of the one that is.
#:
#: Enumerating scripts by hand (``À-ÿ``, ``\u0400-\u04ff``, …) was rejected. Unicode
#: is not a finite checklist of Western alphabets, and a class like that answers
#: "did someone remember this script?" rather than "is this a word character?".
_TOKEN_RE = re.compile(r"[\w.@$/-]+")


def _fts_query(terms: Sequence[str]) -> str:
    """Build a MATCH expression from literal terms.

    Each term is wrapped in double quotes (with internal quotes doubled), making it
    a phrase rather than FTS syntax. Terms are OR-ed for recall; ranking and the
    literal verification below decide what actually counts.
    """
    quoted = [f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms if term]
    return " OR ".join(quoted)


def tokenize_query(text: str) -> list[str]:
    """Extract searchable terms from free text, longest first."""
    seen: dict[str, None] = {}
    for match in _TOKEN_RE.finditer(text):
        token = match.group(0)
        if len(token) > 1:
            seen.setdefault(token, None)
    return sorted(seen, key=len, reverse=True)


#: Escapes for a LIKE pattern. `_` matches any character in LIKE, and it is the
#: most common character in the identifiers this search exists to find exactly, so
#: leaving it unescaped turned a precise lookup into a wildcard one.
_LIKE_ESCAPE = str.maketrans({"\\": "\\\\", "%": "\\%", "_": "\\_"})


def _like_escape(term: str) -> str:
    return term.translate(_LIKE_ESCAPE)


def _normalize_path_term(term: str) -> str:
    """The spelling of a path a user may reasonably type, reduced to one form.

    ``./src/config.py``, ``/src/config.py`` and ``src/config.py`` name the same
    thing; a trailing slash names a directory and never a file. Normalization
    stops here on purpose — nothing here makes matching fuzzier, it only removes
    punctuation that carries no information about *which* file is meant.
    """
    cleaned = term.strip().strip("\"'`")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    cleaned = cleaned.lstrip("/").rstrip("/")
    return "" if cleaned in {"", ".", ".."} else cleaned


def _straddling_reach(
    tail: str, following: Sequence[sqlite3.Row], term: str, width: int
) -> list[int] | None:
    """Chunk ids after the seam that a straddling occurrence of ``term`` reaches.

    Returns ``None`` when no occurrence *crosses* the seam. An occurrence lying
    wholly inside one piece is not this pass's business — the direct pass already
    found it, and reporting it twice would turn one occurrence into two candidates.
    """
    window = tail
    reachable: list[sqlite3.Row] = []
    for piece in following:
        head = piece["head"]
        reachable.append(piece)
        window += head
        if len(window) - len(tail) >= width:
            break

    boundary = len(tail)
    start = window.find(term)
    while start != -1:
        end = start + len(term)
        if start < boundary < end:
            overflow = end - boundary
            covered: list[int] = []
            reached = 0
            for piece in reachable:
                covered.append(piece["chunk_id"])
                reached += piece["size"]
                if reached >= overflow:
                    break
            return covered
        start = window.find(term, start + 1)
    return None


#: Every column a file hit needs, joined to its repository and its file entity.
#: The entity is a LEFT JOIN because the file row is the authority on identity;
#: an entity that has not been written yet must not make the file unfindable.
_FILE_SELECT = """
    SELECT f.id AS file_id, f.repo_id AS repo_id, f.rel_path AS path,
           f.basename AS basename, r.name AS repository, r.slug AS repository_slug,
           e.id AS entity_id
    FROM files f
    JOIN repositories r ON r.id = f.repo_id
    LEFT JOIN entities e ON e.file_id = f.id AND e.type = 'file'
"""


def _repo_scope(
    alias: str, repo_ids: Sequence[int] | None, params: list[object]
) -> tuple[str, bool]:
    """Append a repository scope to ``params``. ``False`` means "search nothing"."""
    if repo_ids is None:
        return "", True
    if not repo_ids:
        return "", False
    placeholders = ",".join("?" * len(repo_ids))
    params.extend(repo_ids)
    return f" AND {alias}.repo_id IN ({placeholders})", True


def _file_hit(row: sqlite3.Row, term: str, specificity: int) -> FileHit:
    return FileHit(
        file_id=row["file_id"],
        repo_id=row["repo_id"],
        repository=row["repository"],
        repository_slug=row["repository_slug"],
        path=row["path"],
        entity_id=row["entity_id"],
        term=term,
        specificity=specificity,
        score=100.0 - specificity,
    )


class SqliteSearchIndex:
    """Lexical search over chunks and entity names."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def search_text(
        self,
        query: str,
        repo_ids: Sequence[int] | None = None,
        limit: int = 50,
    ) -> list[ChunkHit]:
        terms = tokenize_query(query)
        if not terms:
            return []
        match_expr = _fts_query(terms)
        if not match_expr:
            return []

        params: list[object] = [match_expr]
        scope = ""
        if repo_ids is not None:
            if not repo_ids:
                return []
            placeholders = ",".join("?" * len(repo_ids))
            scope = f" AND c.repo_id IN ({placeholders})"
            params.extend(repo_ids)
        params.append(limit)

        rows = self._db.execute(
            f"""
            SELECT c.id AS chunk_id, c.file_id, c.repo_id, c.line_start, c.line_end, c.text,
                   c.split_index, c.split_total,
                   f.rel_path AS path, r.name AS repository, r.slug AS repository_slug,
                   bm25(chunks_fts) AS rank
            FROM chunks_fts
            JOIN chunks       c ON c.id = chunks_fts.rowid
            JOIN files        f ON f.id = c.file_id
            JOIN repositories r ON r.id = c.repo_id
            WHERE chunks_fts MATCH ?{scope}
            ORDER BY rank
            LIMIT ?
            """,  # noqa: S608 - placeholders are generated counts, values are bound
            tuple(params),
        ).fetchall()

        hits: list[ChunkHit] = []
        for row in rows:
            text = row["text"]
            # Exactness verification: bm25 is negative-better; convert to a positive
            # score and boost hits whose literal term genuinely appears.
            literal = any(term in text for term in terms)
            base = -float(row["rank"])
            hits.append(
                ChunkHit(
                    chunk_id=row["chunk_id"],
                    file_id=row["file_id"],
                    repo_id=row["repo_id"],
                    repository=row["repository"],
                    repository_slug=row["repository_slug"],
                    path=row["path"],
                    line_start=row["line_start"],
                    line_end=row["line_end"],
                    text=text,
                    score=base + (10.0 if literal else 0.0),
                    split_index=row["split_index"],
                    split_total=row["split_total"],
                )
            )
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits

    def search_literal(
        self,
        term: str,
        repo_ids: Sequence[int] | None = None,
        limit: int = 50,
    ) -> list[ChunkHit]:
        """Chunks that literally contain ``term``.

        Guarantees exactness where the tokenizer cannot: used for quoted strings and
        environment-variable names, which must never be approximated.

        Two passes, because one chunk is not the same thing as one region of a
        file. The first finds ``term`` inside a stored chunk. The second finds it
        across a **seam** — the point where a span too large for the character cap
        was cut into pieces. Without the second pass the cap decided what was
        findable: a marker straddling offset 8000 lived in no single chunk, and
        exact search reported it absent from a file that contained it.
        """
        if not term:
            return []

        direct = self._literal_chunks(term, repo_ids, limit)
        if len(direct) >= limit:
            return direct[:limit]

        seen = {hit.chunk_id for hit in direct}
        seam = self._seam_chunks(term, repo_ids, limit - len(direct), seen)
        return [*direct, *seam]

    def _literal_chunks(
        self, term: str, repo_ids: Sequence[int] | None, limit: int
    ) -> list[ChunkHit]:
        """Chunks whose own stored text contains ``term``."""
        params: list[object] = [f"%{_like_escape(term)}%"]
        scope = ""
        if repo_ids is not None:
            if not repo_ids:
                return []
            placeholders = ",".join("?" * len(repo_ids))
            scope = f" AND c.repo_id IN ({placeholders})"
            params.extend(repo_ids)
        params.append(limit)

        rows = self._db.execute(
            f"""
            SELECT c.id AS chunk_id, c.file_id, c.repo_id, c.line_start, c.line_end, c.text,
                   c.split_index, c.split_total,
                   f.rel_path AS path, r.name AS repository, r.slug AS repository_slug
            FROM chunks c
            JOIN files        f ON f.id = c.file_id
            JOIN repositories r ON r.id = c.repo_id
            WHERE c.text LIKE ? ESCAPE '\\'{scope}
            LIMIT ?
            """,  # noqa: S608 - placeholders are generated counts, values are bound
            tuple(params),
        ).fetchall()

        return [
            ChunkHit(
                chunk_id=row["chunk_id"],
                file_id=row["file_id"],
                repo_id=row["repo_id"],
                repository=row["repository"],
                repository_slug=row["repository_slug"],
                path=row["path"],
                line_start=row["line_start"],
                line_end=row["line_end"],
                text=row["text"],
                score=20.0,
                split_index=row["split_index"],
                split_total=row["split_total"],
            )
            for row in rows
            if term in row["text"]
        ]

    def _seam_chunks(
        self,
        term: str,
        repo_ids: Sequence[int] | None,
        limit: int,
        exclude: set[int],
    ) -> list[ChunkHit]:
        """Matches that begin in one piece of a split span and end in the next.

        The search window is derived from the query, not from a constant: an
        occurrence of a term of length *L* that starts inside piece *i* reaches at
        most ``L - 1`` characters into what follows it, so ``L - 1`` characters
        from each side of a seam is exactly enough and never more. That is what
        makes the guarantee independent of the chunk size — there is no overlap
        constant to be wrong about, and no length of literal for which the answer
        quietly becomes "not found".

        Only spans that were actually cut are read (``split_total > 1``), and only
        the two windows of each piece, so an index with no oversized content pays
        one index probe.
        """
        width = len(term) - 1
        if width <= 0 or limit <= 0:
            return []

        params: list[object] = [width, width]
        scope = ""
        if repo_ids is not None:
            if not repo_ids:
                return []
            placeholders = ",".join("?" * len(repo_ids))
            scope = f" AND c.repo_id IN ({placeholders})"
            params.extend(repo_ids)

        rows = self._db.execute(
            f"""
            SELECT c.id AS chunk_id, c.file_id, c.span_index, c.split_index,
                   length(c.text) AS size,
                   substr(c.text, 1, ?)  AS head,
                   substr(c.text, -(?)) AS tail
            FROM chunks c
            WHERE c.split_total > 1{scope}
            ORDER BY c.file_id, c.span_index, c.split_index
            """,  # noqa: S608 - placeholders are generated counts, values are bound
            tuple(params),
        ).fetchall()
        if not rows:
            return []

        spans: dict[tuple[int, int], list[sqlite3.Row]] = {}
        for row in rows:
            spans.setdefault((row["file_id"], row["span_index"]), []).append(row)

        wanted: list[list[int]] = []
        for pieces in spans.values():
            for index, piece in enumerate(pieces[:-1]):
                if piece["chunk_id"] in exclude:
                    continue
                covered = _straddling_reach(piece["tail"], pieces[index + 1 :], term, width)
                if covered is None:
                    continue
                wanted.append([piece["chunk_id"], *covered])
                if len(wanted) >= limit:
                    break
            if len(wanted) >= limit:
                break

        return self._stitched_hits(wanted, term)

    def _stitched_hits(self, groups: list[list[int]], term: str) -> list[ChunkHit]:
        """Rebuild each seam match from the pieces it actually spans.

        The text handed back is the concatenation of those pieces, which is a real
        contiguous region of the file: every piece of a span carries that span's
        line range, so joining them is honest about provenance in a way that
        inventing a line number for the seam would not be (V1_SPEC §11).
        """
        if not groups:
            return []

        ids = sorted({chunk_id for group in groups for chunk_id in group})
        placeholders = ",".join("?" * len(ids))
        rows = self._db.execute(
            f"""
            SELECT c.id AS chunk_id, c.file_id, c.repo_id, c.line_start, c.line_end, c.text,
                   c.split_index, c.split_total,
                   f.rel_path AS path, r.name AS repository, r.slug AS repository_slug
            FROM chunks c
            JOIN files        f ON f.id = c.file_id
            JOIN repositories r ON r.id = c.repo_id
            WHERE c.id IN ({placeholders})
            """,  # noqa: S608 - placeholders are generated counts, values are bound
            tuple(ids),
        ).fetchall()
        by_id = {row["chunk_id"]: row for row in rows}

        hits: list[ChunkHit] = []
        for group in groups:
            parts = [by_id[chunk_id] for chunk_id in group if chunk_id in by_id]
            if not parts:
                continue
            head = parts[0]
            text = "".join(part["text"] for part in parts)
            if term not in text:
                continue
            hits.append(
                ChunkHit(
                    chunk_id=head["chunk_id"],
                    file_id=head["file_id"],
                    repo_id=head["repo_id"],
                    repository=head["repository"],
                    repository_slug=head["repository_slug"],
                    path=head["path"],
                    line_start=head["line_start"],
                    line_end=head["line_end"],
                    text=text,
                    score=20.0,
                    # The head piece's position, not a recomputed one. A stitched
                    # hit is a contiguous region rebuilt from *some* pieces of a
                    # cut span, so it inherits the span's ``split_total`` and is
                    # correctly marked as not proving line coverage.
                    split_index=head["split_index"],
                    split_total=head["split_total"],
                )
            )
        return hits

    def find_files_by_path(
        self,
        terms: Sequence[str],
        repo_ids: Sequence[int] | None = None,
        limit: int = 50,
    ) -> list[FileHit]:
        """Indexed files whose own name or path is exactly one of ``terms``.

        The name a file has on disk is a fact about the repository, and this is
        where it becomes retrievable. Without it, ``locate src/config.py`` searched
        only *content*, found no chunk that mentioned the string, and answered that
        nothing matched — about a file the index was holding.

        Matching is exact in every shape it accepts, and deliberately nothing more:
        the whole relative path, that path qualified by the repository's own
        identity (``svc/src/config.py`` — the spelling RepoAtlas itself prints in
        a citation, so a user pasting one back gets what they cited), a trailing
        sub-path on a segment boundary, or the basename. ``onfig.py`` matches
        nothing; this is not a substring search, and turning it into one would
        replace a false negative with a confident guess.

        Ambiguity is returned rather than resolved. Three files named
        ``config.py`` produce three hits, because choosing one of them would be
        RepoAtlas inventing the answer to a question the repository leaves open
        (V1_SPEC §14).
        """
        basenames: list[str] = []
        paths: list[str] = []
        for raw in dict.fromkeys(terms):
            term = _normalize_path_term(raw)
            if not term:
                continue
            bucket = paths if "/" in term else basenames
            if term not in bucket:
                bucket.append(term)
        if not basenames and not paths:
            return []

        found: dict[tuple[str, str], FileHit] = {}
        for hit in (
            *self._files_by_basename(basenames, repo_ids, limit),
            *self._files_by_path(paths, repo_ids, limit),
            *self._files_by_qualified_path(paths, repo_ids, limit),
        ):
            key = (hit.repository, hit.path)
            current = found.get(key)
            if current is None or hit.specificity < current.specificity:
                found[key] = hit

        ordered = sorted(
            found.values(), key=lambda hit: (hit.specificity, hit.repository, hit.path)
        )
        return ordered[:limit]

    def _files_by_basename(
        self, terms: Sequence[str], repo_ids: Sequence[int] | None, limit: int
    ) -> list[FileHit]:
        if not terms:
            return []
        placeholders = ",".join("?" * len(terms))
        params: list[object] = list(terms)
        scope, scoped = _repo_scope("f", repo_ids, params)
        if not scoped:
            return []
        params.append(limit)

        rows = self._db.execute(
            f"{_FILE_SELECT} WHERE f.basename IN ({placeholders}){scope} LIMIT ?",
            tuple(params),
        ).fetchall()
        # The basename is the least specific exact match there is: it says the file
        # is called that, not where it lives.
        return [_file_hit(row, row["basename"], 2) for row in rows]

    def _files_by_path(
        self, terms: Sequence[str], repo_ids: Sequence[int] | None, limit: int
    ) -> list[FileHit]:
        if not terms:
            return []
        # Two predicates per term, and the order matters for cost as well as for
        # correctness. `basename = ?` is an index seek that narrows to the handful
        # of files whose last segment could match; the rest is checked on those
        # rows. Without it the path form is a scan of every indexed file — 3.7 ms
        # at 2,000 files, and linear from there.
        #
        # `substr` rather than LIKE: a path term is matched character for
        # character, and the segment check is what keeps `a/config.py` from
        # matching `data/config.py`.
        clause = " OR ".join(
            """(f.basename = ?
                AND (f.rel_path = ?
                     OR (length(f.rel_path) > length(?)
                         AND substr(f.rel_path, -length(?)) = ?
                         AND substr(f.rel_path, length(f.rel_path) - length(?), 1) = '/')))"""
            for _ in terms
        )
        params: list[object] = []
        for term in terms:
            params.extend([term.rsplit("/", 1)[-1], term, term, term, term, term])
        scope, scoped = _repo_scope("f", repo_ids, params)
        if not scoped:
            return []
        params.append(limit)

        rows = self._db.execute(
            f"{_FILE_SELECT} WHERE ({clause}){scope} LIMIT ?",
            tuple(params),
        ).fetchall()

        hits: list[FileHit] = []
        for row in rows:
            path = row["path"]
            term = next(
                (t for t in terms if path == t or path.endswith(f"/{t}")),
                None,
            )
            if term is None:  # pragma: no cover - the SQL predicate guarantees one
                continue
            hits.append(_file_hit(row, term, 0 if path == term else 1))
        return hits

    def _files_by_qualified_path(
        self, terms: Sequence[str], repo_ids: Sequence[int] | None, limit: int
    ) -> list[FileHit]:
        """Files named as ``<repository identity>/<relative path>``.

        This is the spelling RepoAtlas prints in every citation, so it is the
        spelling a user is most likely to paste back — and it is the only one that
        can name *one* file in a workspace where several repositories hold the
        same relative path. Without it, ``other/src/config.py`` asked from inside
        ``svc`` fell back to the basename ``config.py``, where the
        primary-repository bias then quietly answered with ``svc`` — inferring a
        repository the user had just stated (V1_SPEC §6).

        Matched by equality on the whole qualified name, never by suffix: a
        qualified path is complete by definition, and accepting a partial one
        would reintroduce the ambiguity it exists to remove. Narrowed by
        ``basename`` first for the same reason the unqualified form is — that
        predicate is an index seek, and the concatenation is not.
        """
        # A qualified path needs at least a repository segment and a path segment;
        # a term with one slash could be either shape, so both are tried and the
        # more specific hit wins in the caller.
        wanted = [term for term in terms if "/" in term]
        if not wanted:
            return []

        clause = " OR ".join("(f.basename = ? AND r.slug || '/' || f.rel_path = ?)" for _ in wanted)
        params: list[object] = []
        for term in wanted:
            params.extend([term.rsplit("/", 1)[-1], term])
        scope, scoped = _repo_scope("f", repo_ids, params)
        if not scoped:
            return []
        params.append(limit)

        rows = self._db.execute(
            f"{_FILE_SELECT} WHERE ({clause}){scope} LIMIT ?",
            tuple(params),
        ).fetchall()

        hits: list[FileHit] = []
        for row in rows:
            qualified = f"{row['repository_slug']}/{row['path']}"
            term = next((t for t in wanted if t == qualified), None)
            if term is None:  # pragma: no cover - the SQL predicate guarantees one
                continue
            # As specific as an unqualified whole path, and for the same reason:
            # it names exactly one file. It is not *more* specific, because
            # specificity here grades how exactly a file was identified, and both
            # spellings identify it completely.
            hits.append(_file_hit(row, term, 0))
        return hits

    def find_entities_by_name(
        self,
        names: Sequence[str],
        repo_ids: Sequence[int] | None = None,
        limit: int = 50,
    ) -> list[EntityHit]:
        """Entities whose name, qualified name or key matches ``names`` exactly.

        Deterministic by construction — no tokenizer, no ranking function. This is
        what guarantees that an environment variable, a package name or a symbol
        the question spells out is *found*, rather than left to whether FTS
        happened to tokenize it the same way (V1_SPEC §10, §14).
        """
        wanted = [name for name in names if name]
        if not wanted:
            return []

        keys = [*wanted, *(f"env:{n}" for n in wanted), *(f"module:{n}" for n in wanted)]
        name_placeholders = ",".join("?" * len(wanted))
        key_placeholders = ",".join("?" * len(keys))
        params: list[object] = [*wanted, *wanted, *keys]

        scope = ""
        if repo_ids is not None:
            if not repo_ids:
                return []
            repo_placeholders = ",".join("?" * len(repo_ids))
            scope = f" AND (e.repo_id IN ({repo_placeholders}) OR e.repo_id IS NULL)"
            params.extend(repo_ids)
        params.append(limit)

        rows = self._db.execute(
            f"""
            SELECT e.id AS entity_id, e.type, e.name, e.qualified_name,
                   e.line_start, e.line_end,
                   f.rel_path AS path, r.name AS repository, r.slug AS repository_slug
            FROM entities e
            LEFT JOIN files        f ON f.id = e.file_id
            LEFT JOIN repositories r ON r.id = e.repo_id
            WHERE (e.name IN ({name_placeholders})
                   OR e.qualified_name IN ({name_placeholders})
                   OR e.key IN ({key_placeholders})){scope}
            LIMIT ?
            """,  # noqa: S608 - placeholders are generated counts, values are bound
            tuple(params),
        ).fetchall()

        return [
            EntityHit(
                entity_id=row["entity_id"],
                type=EntityType(row["type"]),
                name=row["name"],
                qualified_name=row["qualified_name"],
                repository=row["repository"] or "",
                repository_slug=row["repository_slug"] or "",
                path=row["path"],
                line_start=row["line_start"],
                line_end=row["line_end"],
                score=100.0,
            )
            for row in rows
        ]

    def search_entities(
        self,
        query: str,
        repo_ids: Sequence[int] | None = None,
        types: Sequence[EntityType] | None = None,
        limit: int = 50,
    ) -> list[EntityHit]:
        terms = tokenize_query(query)
        if not terms:
            return []
        match_expr = _fts_query(terms)
        if not match_expr:
            return []

        params: list[object] = [match_expr]
        scope = ""
        if repo_ids is not None:
            if not repo_ids:
                return []
            placeholders = ",".join("?" * len(repo_ids))
            # ``OR e.repo_id IS NULL`` keeps interned entities in scope: an
            # environment variable is global by construction, and scoping it out
            # would hide exactly the hub that connects two repositories.
            scope += f" AND (e.repo_id IN ({placeholders}) OR e.repo_id IS NULL)"
            params.extend(repo_ids)
        if types:
            type_placeholders = ",".join("?" * len(types))
            scope += f" AND e.type IN ({type_placeholders})"
            params.extend(str(t) for t in types)
        params.append(limit)

        rows = self._db.execute(
            f"""
            SELECT e.id AS entity_id, e.type, e.name, e.qualified_name,
                   e.line_start, e.line_end,
                   f.rel_path AS path, r.name AS repository, r.slug AS repository_slug,
                   bm25(entities_fts) AS rank
            FROM entities_fts
            JOIN entities          e ON e.id = entities_fts.rowid
            LEFT JOIN files        f ON f.id = e.file_id
            LEFT JOIN repositories r ON r.id = e.repo_id
            WHERE entities_fts MATCH ?{scope}
            ORDER BY rank
            LIMIT ?
            """,  # noqa: S608 - placeholders are generated counts, values are bound
            tuple(params),
        ).fetchall()

        hits: list[EntityHit] = []
        for row in rows:
            name = row["name"]
            exact = any(term == name for term in terms)
            hits.append(
                EntityHit(
                    entity_id=row["entity_id"],
                    type=EntityType(row["type"]),
                    name=name,
                    qualified_name=row["qualified_name"],
                    # Interned entities — environment variables, packages — belong
                    # to no repository. They are still first-class searchable
                    # entities (V1_SPEC §10) and are the hubs graph expansion
                    # travels through, so they must not be joined away.
                    repository=row["repository"] or "",
                    repository_slug=row["repository_slug"] or "",
                    path=row["path"],
                    line_start=row["line_start"],
                    line_end=row["line_end"],
                    score=-float(row["rank"]) + (25.0 if exact else 0.0),
                )
            )
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits
