"""Brute-force vector search (ARCHITECTURE.md §3, §10).

Scoping happens **before** the dot product: candidate chunks are restricted to the
repositories in scope by SQL, then scored. That is what satisfies "never scan
every repo for every question" (V1_SPEC §20) for the semantic stage as well as the
lexical one.

At the target scale — ~10^5 chunks by 384 dimensions, float16 — the whole matrix is
about 77 MB and a scoped query is single-digit milliseconds, so no index structure
is warranted. The interface hides this completely: ``sqlite-vec`` or FAISS can
replace the implementation without touching a caller.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from repoatlas.storage.protocols import VectorHit
from repoatlas.storage.sqlite.database import Database

#: Vectors are stored as float16 — half the space, and the precision loss is far
#: below the resolution at which cosine ranks differ.
_STORAGE_DTYPE = np.float16


def encode_vector(vector: Sequence[float]) -> bytes:
    return np.asarray(vector, dtype=_STORAGE_DTYPE).tobytes()


def decode_vector(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=_STORAGE_DTYPE)


def _normalize(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return matrix / norms


class SqliteVectorIndex:
    """Vectors keyed by chunk content hash, so identical text is embedded once."""

    def __init__(self, database: Database) -> None:
        self._db = database

    # -- writing -------------------------------------------------------------

    def upsert_many(
        self, model: str, dim: int, vectors: Sequence[tuple[str, Sequence[float]]]
    ) -> int:
        """Store vectors for ``(content_hash, model)`` pairs."""
        if not vectors:
            return 0
        with self._db.transaction() as conn:
            conn.executemany(
                """
                INSERT INTO embeddings (content_hash, model, dim, vector)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(content_hash, model) DO UPDATE SET
                    dim = excluded.dim, vector = excluded.vector
                """,
                [(content_hash, model, dim, encode_vector(v)) for content_hash, v in vectors],
            )
        return len(vectors)

    def missing_hashes(self, model: str, repo_ids: Sequence[int] | None = None) -> list[str]:
        """Chunk hashes with no vector for ``model``.

        This is the whole of "re-embed only what changed": a chunk whose text is
        unchanged keeps its hash across a reparse and is simply not returned here.
        """
        sql = """
            SELECT DISTINCT c.content_hash AS content_hash
            FROM chunks c
            LEFT JOIN embeddings e
                   ON e.content_hash = c.content_hash AND e.model = ?
            WHERE e.content_hash IS NULL AND c.content_hash != ''
        """
        params: list[object] = [model]
        if repo_ids is not None:
            if not repo_ids:
                return []
            placeholders = ",".join("?" * len(repo_ids))
            sql += f" AND c.repo_id IN ({placeholders})"
            params.extend(repo_ids)
        rows = self._db.execute(sql, tuple(params)).fetchall()
        return [str(row["content_hash"]) for row in rows]

    def texts_for_hashes(self, hashes: Sequence[str]) -> list[tuple[str, str]]:
        """One representative text per hash, in hash order."""
        if not hashes:
            return []
        placeholders = ",".join("?" * len(hashes))
        rows = self._db.execute(
            f"""
            SELECT content_hash, MIN(text) AS text
            FROM chunks
            WHERE content_hash IN ({placeholders})
            GROUP BY content_hash
            """,  # noqa: S608 - placeholders are a generated count, values are bound
            tuple(hashes),
        ).fetchall()
        return [(str(row["content_hash"]), str(row["text"])) for row in rows]

    def gc_orphan_vectors(self) -> int:
        """Drop vectors no chunk still refers to.

        A vector is a derived ranking aid, not a provenanced fact, so an orphan
        wastes space but can never become a stale claim.
        """
        with self._db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM embeddings WHERE content_hash NOT IN (SELECT content_hash FROM chunks)"
            )
        return cursor.rowcount or 0

    def delete_model(self, model: str) -> int:
        with self._db.transaction() as conn:
            cursor = conn.execute("DELETE FROM embeddings WHERE model = ?", (model,))
        return cursor.rowcount or 0

    # -- reading -------------------------------------------------------------

    def count(self, model: str | None = None) -> int:
        if model is None:
            row = self._db.execute("SELECT COUNT(*) AS n FROM embeddings").fetchone()
        else:
            row = self._db.execute(
                "SELECT COUNT(*) AS n FROM embeddings WHERE model = ?", (model,)
            ).fetchone()
        return int(row["n"])

    def query(
        self,
        vector: Sequence[float],
        model: str,
        repo_ids: Sequence[int] | None = None,
        limit: int = 30,
        floor: float = 0.0,
    ) -> list[VectorHit]:
        """Nearest chunks by cosine similarity, scoped before scoring."""
        if not len(vector):
            return []

        sql = """
            SELECT c.id AS chunk_id, c.file_id, c.repo_id, c.line_start, c.line_end,
                   c.text, c.split_index, c.split_total,
                   f.rel_path AS path, r.name AS repository, r.slug AS repository_slug,
                   e.vector AS vector
            FROM embeddings e
            JOIN chunks       c ON c.content_hash = e.content_hash
            JOIN files        f ON f.id = c.file_id
            JOIN repositories r ON r.id = c.repo_id
            WHERE e.model = ?
        """
        params: list[object] = [model]
        if repo_ids is not None:
            if not repo_ids:
                return []
            placeholders = ",".join("?" * len(repo_ids))
            sql += f" AND c.repo_id IN ({placeholders})"
            params.extend(repo_ids)

        rows = self._db.execute(sql, tuple(params)).fetchall()
        if not rows:
            return []

        query_vector = np.asarray(vector, dtype=np.float32)
        norm = float(np.linalg.norm(query_vector))
        if norm == 0.0:
            return []
        query_vector = query_vector / norm

        matrix = np.vstack([decode_vector(row["vector"]) for row in rows]).astype(np.float32)
        if matrix.shape[1] != query_vector.shape[0]:
            # A dimension mismatch means the stored vectors are from a different
            # model than the one asking. Returning nothing is correct: they are
            # not comparable, and comparing them anyway would invent a ranking.
            return []

        scores = _normalize(matrix) @ query_vector
        order = np.argsort(-scores)[: max(limit, 0)]

        hits: list[VectorHit] = []
        for index in order:
            score = float(scores[index])
            if score < floor:
                break
            row = rows[int(index)]
            hits.append(
                VectorHit(
                    chunk_id=row["chunk_id"],
                    file_id=row["file_id"],
                    repo_id=row["repo_id"],
                    repository=row["repository"],
                    repository_slug=row["repository_slug"],
                    path=row["path"],
                    line_start=row["line_start"],
                    line_end=row["line_end"],
                    text=row["text"],
                    score=score,
                    split_index=row["split_index"],
                    split_total=row["split_total"],
                )
            )
        return hits
