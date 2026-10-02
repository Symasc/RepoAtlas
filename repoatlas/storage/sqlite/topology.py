"""SQLite reads behind the topology capabilities.

Every method is one statement, however many rows it returns. A directory's links
are read as two joins — the edges leaving entities under the path and the edges
arriving at them — and never as a query per child, per file or per entity, so
the number of statements a topology answer costs is the same for a repository of
ten files and one of ten thousand.

Paths are matched as exact, case-sensitive ranges over ``idx_files_repo_path``:
``prefix/`` up to (but not including) ``prefix0``, ``0`` being the character
after ``/``. ``LIKE`` would be neither: it folds ASCII case and treats ``_`` and
``%`` inside a path as wildcards. A file search, which does want case folded,
uses ``instr`` over ``lower()`` on both sides, so a ``%`` typed into it is a
percent sign.
"""

from __future__ import annotations

from collections.abc import Sequence

from repoatlas.models.parsing import EdgeType, EntityType
from repoatlas.storage.protocols import (
    CrossingEdgeRow,
    FileMatchRow,
    LinkEdgeRow,
    NameMatchRow,
    ReferenceRow,
    StoredEdge,
    TopologyDeclarationRow,
    TopologyFileRow,
)
from repoatlas.storage.sqlite.database import Database

_STORED_EDGE_SELECT = """
    SELECT e.id AS edge_id, e.type AS type, e.src_id AS src_id, e.dst_id AS dst_id,
           s.key AS src_key, d.key AS dst_key,
           s.repo_id AS src_repo_id, d.repo_id AS dst_repo_id,
           COALESCE(r.slug, r.name) AS repository_slug,
           e.file_id AS file_id, f.rel_path AS path,
           e.line_start AS line_start, e.line_end AS line_end,
           e.parser AS parser, e.confidence AS confidence, e.resolution AS resolution
    FROM edges e
    JOIN entities s ON s.id = e.src_id
    JOIN entities d ON d.id = e.dst_id
    LEFT JOIN files f ON f.id = e.file_id
    LEFT JOIN repositories r ON r.id = e.repo_id
"""


def _placeholders(values: Sequence[object]) -> str:
    return ",".join("?" * len(values))


def path_range(prefix: str) -> tuple[str, str] | None:
    """The half-open ``rel_path`` range holding everything beneath ``prefix``.

    ``None`` for the repository root, which holds everything. Public so the one
    place that computes the bound is the one place a test can check it.
    """
    if not prefix:
        return None
    return (f"{prefix}/", f"{prefix}0")


class SqliteTopologyStore:
    def __init__(self, database: Database) -> None:
        self._db = database

    def crossing_edges(self, *, exclude: Sequence[EdgeType], limit: int) -> list[CrossingEdgeRow]:
        """Edges whose endpoints belong to two different repositories, up to ``limit``."""
        rows = self._db.execute(
            f"""
            SELECT e.id, e.type, s.repo_id, d.repo_id, s.key, d.key, f.rel_path, e.line_start
            FROM edges e
            JOIN entities s ON s.id = e.src_id
            JOIN entities d ON d.id = e.dst_id
            LEFT JOIN files f ON f.id = e.file_id
            WHERE s.repo_id IS NOT NULL
              AND d.repo_id IS NOT NULL
              AND s.repo_id <> d.repo_id
              AND e.type NOT IN ({_placeholders(exclude)})
            LIMIT ?
            """,  # noqa: S608 - placeholders only
            (*[str(t) for t in exclude], limit),
        ).fetchall()
        return [CrossingEdgeRow(*row) for row in rows]

    def stored_edges(self, edge_ids: Sequence[int]) -> list[StoredEdge]:
        if not edge_ids:
            return []
        rows = self._db.execute(
            f"{_STORED_EDGE_SELECT} WHERE e.id IN ({_placeholders(edge_ids)})",
            tuple(edge_ids),
        ).fetchall()
        return [StoredEdge.model_validate(dict(row)) for row in rows]

    def stored_edges_from(self, src_ids: Sequence[int], edge_type: EdgeType) -> list[StoredEdge]:
        """Every stored edge of one type leaving these entities (``idx_edges_src``)."""
        if not src_ids:
            return []
        rows = self._db.execute(
            f"""
            {_STORED_EDGE_SELECT}
            WHERE e.src_id IN ({_placeholders(src_ids)}) AND e.type = ?
            ORDER BY e.id
            """,
            (*src_ids, str(edge_type)),
        ).fetchall()
        return [StoredEdge.model_validate(dict(row)) for row in rows]

    def file_at(self, repo_id: int, path: str) -> TopologyFileRow | None:
        row = self._db.execute(
            "SELECT id, rel_path, language FROM files WHERE repo_id = ? AND rel_path = ?",
            (repo_id, path),
        ).fetchone()
        return None if row is None else TopologyFileRow(*row)

    def files_under(self, repo_id: int, prefix: str) -> list[TopologyFileRow]:
        bounds = path_range(prefix)
        if bounds is None:
            rows = self._db.execute(
                "SELECT id, rel_path, language FROM files WHERE repo_id = ? ORDER BY rel_path",
                (repo_id,),
            ).fetchall()
        else:
            rows = self._db.execute(
                """
                SELECT id, rel_path, language FROM files
                WHERE repo_id = ? AND rel_path >= ? AND rel_path < ?
                ORDER BY rel_path
                """,
                (repo_id, *bounds),
            ).fetchall()
        return [TopologyFileRow(*row) for row in rows]

    def declaration_counts(self, file_ids: Sequence[int]) -> dict[int, int]:
        if not file_ids:
            return {}
        rows = self._db.execute(
            f"""
            SELECT file_id, COUNT(*) FROM entities
            WHERE file_id IN ({_placeholders(file_ids)}) AND type <> ?
            GROUP BY file_id
            """,  # noqa: S608 - placeholders only
            (*file_ids, str(EntityType.FILE)),
        ).fetchall()
        return {int(row[0]): int(row[1]) for row in rows}

    def declarations_in(self, file_id: int) -> list[TopologyDeclarationRow]:
        rows = self._db.execute(
            """
            SELECT id, key, type, name, qualified_name, line_start, line_end
            FROM entities
            WHERE file_id = ? AND type <> ?
            ORDER BY COALESCE(line_start, 0), key, id
            """,
            (file_id, str(EntityType.FILE)),
        ).fetchall()
        return [TopologyDeclarationRow(*row) for row in rows]

    def edges_under(
        self,
        repo_id: int,
        *,
        prefix: str | None,
        file_id: int | None,
        outgoing: bool,
        exclude: Sequence[EdgeType],
        limit: int,
    ) -> list[LinkEdgeRow]:
        """Edges at entities under one directory (``prefix``) or in one file.

        ``outgoing`` reads the edges those entities are the source of, otherwise
        the ones they are the target of. The far end is reported wherever it is.
        """
        near, far = ("src_id", "dst_id") if outgoing else ("dst_id", "src_id")
        params: list[object] = [repo_id]
        if file_id is not None:
            where = "sf.id = ?"
            params.append(file_id)
        else:
            bounds = path_range(prefix or "")
            if bounds is None:
                where = "1 = 1"
            else:
                where = "sf.rel_path >= ? AND sf.rel_path < ?"
                params.extend(bounds)
        params.extend(str(t) for t in exclude)
        params.append(limit)
        rows = self._db.execute(
            f"""
            SELECT e.id, e.type, s.id, sf.rel_path, e.line_start,
                   d.repo_id, df.rel_path, d.type, d.key
            FROM files sf
            JOIN entities s ON s.file_id = sf.id
            JOIN edges e ON e.{near} = s.id
            JOIN entities d ON d.id = e.{far}
            LEFT JOIN files df ON df.id = d.file_id
            WHERE sf.repo_id = ? AND {where}
              AND e.type NOT IN ({_placeholders(exclude)})
            LIMIT ?
            """,  # noqa: S608 - near/far are two fixed column names, never input
            tuple(params),
        ).fetchall()
        return [LinkEdgeRow(*row) for row in rows]

    def repository_edges_under(
        self,
        repo_id: int,
        *,
        prefix: str | None,
        file_id: int | None,
        exclude: Sequence[EdgeType],
        limit: int,
    ) -> list[LinkEdgeRow]:
        """Edges leaving this repository's own entity, asserted by files under a path.

        A repository entity has no file of its own, so the edges it is the source
        of — a manifest's dependency on a package, say — belong to the child that
        holds the file asserting them. Read through ``idx_edges_file``.
        """
        params: list[object] = [repo_id]
        if file_id is not None:
            where = "af.id = ?"
            params.append(file_id)
        else:
            bounds = path_range(prefix or "")
            if bounds is None:
                where = "1 = 1"
            else:
                where = "af.rel_path >= ? AND af.rel_path < ?"
                params.extend(bounds)
        params.append(repo_id)
        params.extend(str(t) for t in exclude)
        params.append(limit)
        rows = self._db.execute(
            f"""
            SELECT e.id, e.type, s.id, af.rel_path, e.line_start,
                   d.repo_id, df.rel_path, d.type, d.key
            FROM files af
            JOIN edges e ON e.file_id = af.id
            JOIN entities s ON s.id = e.src_id
            JOIN entities d ON d.id = e.dst_id
            LEFT JOIN files df ON df.id = d.file_id
            WHERE af.repo_id = ? AND {where}
              AND s.file_id IS NULL AND s.repo_id = ?
              AND e.type NOT IN ({_placeholders(exclude)})
            LIMIT ?
            """,  # noqa: S608 - a fixed clause and placeholders only
            tuple(params),
        ).fetchall()
        return [LinkEdgeRow(*row) for row in rows]

    def files_matching(
        self, repo_ids: Sequence[int], terms: Sequence[str], *, limit: int
    ) -> list[FileMatchRow]:
        """Files in these repositories whose path holds every term, best first.

        Case is folded by SQLite's ``lower()`` on both sides, so the query and the
        path agree on what folding means. Ranked by how the file's own name holds
        the last term (exactly, as a prefix, anywhere, not at all), then by the
        shortest path. ``total`` counts every match, before the limit.
        """
        if not repo_ids or not terms:
            return []
        last = terms[-1]
        contains = " AND ".join("instr(lower(f.rel_path), lower(?)) > 0" for _ in terms)
        rows = self._db.execute(
            f"""
            SELECT f.repo_id, f.rel_path, f.basename, f.language, COUNT(*) OVER () AS total
            FROM files f
            JOIN repositories r ON r.id = f.repo_id
            WHERE f.repo_id IN ({_placeholders(repo_ids)}) AND {contains}
            ORDER BY
                CASE
                    WHEN lower(f.basename) = lower(?) THEN 0
                    WHEN instr(lower(f.basename), lower(?)) = 1 THEN 1
                    WHEN instr(lower(f.basename), lower(?)) > 0 THEN 2
                    ELSE 3
                END,
                length(f.rel_path),
                COALESCE(r.slug, r.name),
                f.rel_path
            LIMIT ?
            """,  # noqa: S608 - placeholders and a fixed clause per term only
            (*repo_ids, *terms, last, last, last, limit),
        ).fetchall()
        return [FileMatchRow(*row) for row in rows]

    def names_matching(
        self,
        repo_ids: Sequence[int],
        terms: Sequence[str],
        *,
        types: Sequence[EntityType],
        limit: int,
    ) -> list[NameMatchRow]:
        """Entities of ``types`` whose qualified name holds every term, best first.

        In these repositories, or in none: an entity no repository owns (an
        environment variable) is in every scope. Case is folded by ``lower()`` on
        both sides, never by ``LIKE``. Ranked by how the entity's own name holds the
        last term (exactly, as a prefix, anywhere, not at all), then by the shortest
        name, then by where it is written. ``total`` counts every match.
        """
        if not repo_ids or not terms or not types:
            return []
        last = terms[-1]
        named = "lower(COALESCE(e.qualified_name, e.name))"
        contains = " AND ".join(f"instr({named}, lower(?)) > 0" for _ in terms)
        rows = self._db.execute(
            f"""
            SELECT e.key, e.type, e.name, e.qualified_name, e.repo_id, f.rel_path,
                   e.line_start, e.line_end, COUNT(*) OVER () AS total
            FROM entities e
            LEFT JOIN files f ON f.id = e.file_id
            LEFT JOIN repositories r ON r.id = e.repo_id
            WHERE e.type IN ({_placeholders(types)})
              AND (e.repo_id IN ({_placeholders(repo_ids)}) OR e.repo_id IS NULL)
              AND {contains}
            ORDER BY
                CASE
                    WHEN lower(e.name) = lower(?) THEN 0
                    WHEN instr(lower(e.name), lower(?)) = 1 THEN 1
                    WHEN instr(lower(e.name), lower(?)) > 0 THEN 2
                    ELSE 3
                END,
                length(e.name),
                COALESCE(r.slug, r.name, ''),
                COALESCE(f.rel_path, ''),
                COALESCE(e.line_start, 0),
                e.key
            LIMIT ?
            """,  # noqa: S608 - placeholders and a fixed clause per term only
            (*[str(t) for t in types], *repo_ids, *terms, last, last, last, limit),
        ).fetchall()
        return [NameMatchRow(*row) for row in rows]

    def references_in(
        self, file_id: int, *, exclude: Sequence[EdgeType], limit: int
    ) -> list[ReferenceRow]:
        """Located edges asserted by one file, in source order (``idx_edges_file``).

        Read by the asserting file rather than by where the source entity lives:
        an edge's lines are lines of the file that asserts it.
        """
        rows = self._db.execute(
            f"""
            SELECT e.line_start, e.line_end, e.type, s.key,
                   d.key, d.type, d.name, d.repo_id, df.rel_path, d.line_start, d.line_end
            FROM edges e
            JOIN entities s ON s.id = e.src_id
            JOIN entities d ON d.id = e.dst_id
            LEFT JOIN files df ON df.id = d.file_id
            WHERE e.file_id = ?
              AND e.line_start IS NOT NULL
              AND e.type NOT IN ({_placeholders(exclude)})
            ORDER BY e.line_start, e.line_end, e.type, d.key, s.key
            LIMIT ?
            """,  # noqa: S608 - placeholders only
            (file_id, *[str(t) for t in exclude], limit),
        ).fetchall()
        return [ReferenceRow(*row) for row in rows]
