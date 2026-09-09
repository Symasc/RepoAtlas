-- RepoAtlas schema v4 — exact retrieval integrity (V1_SPEC §14, ARCHITECTURE.md §10).
--
-- Two defects shared one visible failure: RepoAtlas reported that something did
-- not exist while the index was holding it. This migration adds the two pieces of
-- structure that make each of them impossible.
--
-- 1. File identity is searchable.
--
-- `files.rel_path` was only ever read back by path, never searched, so the exact
-- name a file has on disk was not a retrievable fact: `src/config.py` was indexed
-- and then reported absent. `basename` is generated rather than written by the
-- indexer, so it cannot drift from `rel_path` — there is no code path that could
-- update one without the other — and it is indexed, so a filename lookup is a
-- seek rather than a scan of every indexed file.
ALTER TABLE files ADD COLUMN basename TEXT
    GENERATED ALWAYS AS (
        substr(rel_path, length(rtrim(rel_path, replace(rel_path, '/', ''))) + 1)
    ) VIRTUAL;

CREATE INDEX idx_files_basename ON files (basename);

-- 2. A chunk knows where it sits in the span it was cut from.
--
-- Content with no line structure — a minified bundle, a base64 blob — is bounded
-- at a fixed character count, and a literal straddling one of those cuts existed
-- in no single chunk. Exact search therefore returned nothing for text the file
-- plainly contained, and the cap (an evidence-sizing decision) silently decided
-- what was discoverable.
--
-- These columns make the cut reversible. `span_index` identifies the line span
-- within its file, `split_index` the piece within that span, and `split_total`
-- how many pieces the span became. Search stitches adjacent pieces back across a
-- seam, so exactness stops depending on where the cap happened to fall. The
-- defaults describe an unsplit chunk, which is what every ordinary chunk is.
ALTER TABLE chunks ADD COLUMN span_index  INTEGER NOT NULL DEFAULT 0;
ALTER TABLE chunks ADD COLUMN split_index INTEGER NOT NULL DEFAULT 0;
ALTER TABLE chunks ADD COLUMN split_total INTEGER NOT NULL DEFAULT 1;

-- Seam recovery reads only split spans, in span order.
--
-- `split_total` leads deliberately. A *partial* index (`WHERE split_total > 1`)
-- describes the same rows and is far smaller, but SQLite will not choose one
-- without a constraint on its leading column, and without table statistics it has
-- no way to know the partial index is nearly empty — measured, it fell back to
-- scanning every chunk and computing `substr` over each one (7.2 ms per literal
-- search at 50k chunks, against 0.25 ms here, growing with the index). Leading on
-- the predicate's own column makes `split_total > 1` a range seek the planner
-- takes on its own, with no `INDEXED BY` hint tying the query to an index name.
CREATE INDEX idx_chunks_split
    ON chunks (split_total, repo_id, file_id, span_index, split_index);
