"""FTS5-backed exact search (V1_SPEC §14, ARCHITECTURE.md §3).

FTS provides recall; a literal substring check provides precision. No tokenizer is
exact for every query shape, so a hit is only reported as *exact* once the literal
term has been found in the chunk text.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from repoatlas.models.parsing import EntityType
from repoatlas.storage.protocols import ChunkHit, EntityHit
from repoatlas.storage.sqlite.database import Database

#: Characters FTS5 treats as syntax. Terms are quoted rather than escaped, so any
#: user text is safe to place inside an MATCH expression.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_.@$/-]+")


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
                   f.rel_path AS path, r.name AS repository, bm25(chunks_fts) AS rank
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
                    path=row["path"],
                    line_start=row["line_start"],
                    line_end=row["line_end"],
                    text=text,
                    score=base + (10.0 if literal else 0.0),
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
        """
        if not term:
            return []
        params: list[object] = [f"%{term}%"]
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
                   f.rel_path AS path, r.name AS repository
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
                path=row["path"],
                line_start=row["line_start"],
                line_end=row["line_end"],
                text=row["text"],
                score=20.0,
            )
            for row in rows
            if term in row["text"]
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
            scope += f" AND e.repo_id IN ({placeholders})"
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
                   f.rel_path AS path, r.name AS repository,
                   bm25(entities_fts) AS rank
            FROM entities_fts
            JOIN entities     e ON e.id = entities_fts.rowid
            LEFT JOIN files   f ON f.id = e.file_id
            JOIN repositories r ON r.id = e.repo_id
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
                    repository=row["repository"],
                    path=row["path"],
                    line_start=row["line_start"],
                    line_end=row["line_end"],
                    score=-float(row["rank"]) + (25.0 if exact else 0.0),
                )
            )
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits
